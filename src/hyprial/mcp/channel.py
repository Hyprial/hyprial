"""Claude Code Channel wake edge and per-session daemon adapter."""

from __future__ import annotations

import ctypes
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from hyprial import __version__
from hyprial.contracts.channel import (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
)
from hyprial.contracts.session import session_fetch_params
from hyprial.daemon.desired_state import DesiredState, InteractiveSession
from hyprial.log import Logger

from .api import SESSION_SUPERSEDED_CODE, DaemonDisconnected, DaemonRequestRejected
from .proxy import StatelessDaemonProxy
from .wake import WakeAttempt, WakeCommand, WakeCoordinator, WakeStatus

CHANNEL_CAPABILITY = "claude/channel"
CHANNEL_NOTIFICATION = "notifications/claude/channel"

_logger = logging.getLogger(__name__)
_PROCESS_POPEN = subprocess.Popen
_PROC_ROOT = Path("/proc")
_OWNER_IDENTITY_MISMATCH_GRACE_SECONDS = 10.0
_DARWIN_PROC_PIDTBSDINFO = 3


class _DarwinProcBsdInfo(ctypes.Structure):
    """Stable prefix of Darwin's ``proc_bsdinfo`` including process birth."""

    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


class _OwnerProcessStatus(StrEnum):
    ALIVE = "alive"
    PID_MISSING = "pid-missing"
    IDENTITY_MISMATCH = "identity-mismatch"
    UNKNOWN = "unknown"


# Daemon-contact failures the channel poll loop must survive without crashing.
# A daemon restart (the deploy path) drops the Unix socket and, for a moment
# while the new daemon boots, can answer with an error envelope surfaced as
# RuntimeError (see mcp.unix). A version-skewed daemon can also return a
# response shape this child cannot parse -- _remember_messages raises TypeError
# on a non-array messages field and coordinator.enqueue raises ValueError on an
# empty deliveryId. None of these may propagate out of the poll task: Claude
# Code cannot respawn a dead stdio MCP child, so an unguarded raise here would
# orphan the channel and force a full session restart -- the exact failure this
# loop exists to prevent. Cancellation is a BaseException and is never caught
# here, so shutdown still propagates cleanly.
_DAEMON_CONTACT_ERRORS = (
    DaemonDisconnected,
    RuntimeError,
    OSError,
    TimeoutError,
    TypeError,
    ValueError,
)


@runtime_checkable
class ChannelNotifier(Protocol):
    async def notify(self, method: str, params: dict[str, Any]) -> None: ...


class StdioChannelNotifier:
    """Serialize unsolicited channel notifications on the SDK stdio stream."""

    def __init__(self, write_stream: Any) -> None:
        self._write_stream = write_stream

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_stream.send(
            SessionMessage(
                types.JSONRPCNotification(
                    jsonrpc="2.0",
                    method=method,
                    params=params,
                )
            )
        )


class ClaudeChannelDriver:
    """Write a minimal formal edge to Claude's local stdio MCP connection."""

    def __init__(self, notifier: ChannelNotifier) -> None:
        self._notifier = notifier

    async def wake(self, command: WakeCommand) -> WakeAttempt:
        try:
            await self._notifier.notify(
                CHANNEL_NOTIFICATION,
                {
                    "content": command.prompt,
                    "meta": {"delivery_id": command.delivery_id},
                },
            )
        # anyio's stream errors do not inherit from OSError, so the tuple below
        # used to let BrokenResourceError straight through -- out of the wake
        # call, out of dispatch_due, out of poll_once, and out of the poll task,
        # which killed the whole channel session. That escape is what red-lined
        # test_held_open_client_session_survives_daemon_restart on CI run 776
        # (task 2681). The invariant it breaks is already stated at the top of
        # this module: an unguarded raise here orphans the channel, and Claude
        # Code cannot respawn a dead stdio MCP child.
        #
        # What this except now swallows that it did not before, one at a time:
        #   BrokenResourceError -- every receiver of the stdio stream is gone,
        #       or the pipe broke. That is precisely what ConnectionError
        #       already means on this line; anyio simply does not spell it as
        #       an OSError.
        #   ClosedResourceError -- *our* end was closed, which happens only
        #       while the session is being torn down. It is the symmetric half
        #       of the same window: which of the two a racing wake observes
        #       depends on the order the two ends close in, and neither order
        #       is under this code's control. Catching one and not the other
        #       would leave the same crash reachable from the other direction.
        #
        # Deliberately still fatal, because these are defects in us rather than
        # verdicts about the transport:
        #   BusyResourceError -- two tasks sending concurrently, which breaks
        #       the serialization this notifier exists to provide. Swallowing
        #       it would turn interleaved frames into a silent retry.
        #   EndOfStream -- receive-side only; send never raises it, and if that
        #       ever changes we want to hear about it.
        except (
            ConnectionError,
            OSError,
            RuntimeError,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as error:
            # BrokenResourceError carries no message, so str() is "". A detail
            # of "" says "failed, no reason given" -- the same output a genuine
            # empty reason would produce. Fall back to the class name so the
            # two stay distinguishable.
            return WakeAttempt(WakeStatus.FAILED, str(error) or type(error).__name__)
        # This is transport acceptance only. WakeCoordinator deliberately
        # retains the durable key and exposes SIGNALLED instead of completion.
        return WakeAttempt(WakeStatus.ACCEPTED)


class RuntimeKind(StrEnum):
    CLAUDE_INTERACTIVE = "claude_interactive"
    CLAUDE_MANAGED_HEADLESS = "claude_managed_headless"
    LEGACY = "legacy"
def _install_legacy_discover_handler(lowlevel: Server[Any]) -> None:
    """Pin ``server/discover`` to handshake-era protocol versions.

    CC >= 2.1.227 only treats a connected server as a development channel
    when the connection is NOT in the modern protocol era — the modern wire
    (2026-07-28) has no unsolicited-notification path for stdio.  The MCP
    SDK's default discover handler advertises 2026-07-28, and CC upgrades
    the connection into that era, which silently disables every channel
    notification (the E2E-008 / #94 root cause, confirmed live: a server
    answering discover with handshake versions gets ``Channel notifications
    registered`` on CC 2.1.235).  The channel mechanism structurally
    requires the legacy notification path, so pinning handshake versions is
    the contract, not a workaround.
    """

    async def discover(
        ctx: Any, _params: types.RequestParams
    ) -> types.DiscoverResult:
        return types.DiscoverResult(
            supported_versions=list(HANDSHAKE_PROTOCOL_VERSIONS),
            capabilities=lowlevel.get_capabilities(
                protocol_version=ctx.protocol_version
            ),
            instructions=lowlevel.instructions,
        )

    lowlevel.add_request_handler("server/discover", types.RequestParams, discover)


def channel_initialization_options(
    server: Server[Any] | None = None,
) -> InitializationOptions:
    """Advertise the CC preview capability in ``experimental``, never extensions.

    ``claude/channel/permission`` is deliberately NOT declared: live
    mutation on CC 2.1.235 proved channel notifications register and wakes
    land with only ``claude/channel`` plus a legacy-era connection (the
    dual-cap filter in the CC binary gates the channel-permission flow,
    which this server does not implement — declaring it would be dishonest
    metadata).  The #94 blocker was the era, not the capability set: on a
    modern-era connection CC reads capabilities from the ``server/discover``
    result (which carries no ``experimental``), so the skip log blaming a
    missing capability was the era defect masquerading as one.
    """

    lowlevel = server or Server("harness-bridge", version=__version__)
    options = lowlevel.create_initialization_options(
        experimental_capabilities={CHANNEL_CAPABILITY: {}}
    )
    # The SDK's capability model defaults extensions to an empty mapping.  Set
    # it to null so exclude-none wire serialization cannot mis-advertise the
    # Claude preview contract as a standard MCP extension.
    capabilities = options.capabilities.model_copy(update={"extensions": None})
    return options.model_copy(update={"capabilities": capabilities})


class ClaudeChannelAdapter:
    """Bind one CC session to daemon truth and its ACK-aware wake coordinator."""

    def __init__(
        self,
        proxy: StatelessDaemonProxy,
        coordinator: WakeCoordinator,
        *,
        actor: str,
        session_ref: str,
        cwd: str,
        command: tuple[str, ...],
        owner_fence: bool = False,
        lease_token: str | None = None,
        tmux_session: str | None = None,
    ) -> None:
        if not actor or not session_ref or not cwd or not command:
            raise ValueError(
                "channel adapter requires actor, session_ref, cwd and command"
            )
        self.proxy = proxy
        self.coordinator = coordinator
        self.actor = actor
        self.session_ref = session_ref
        self.cwd = cwd
        self.command = command
        self.owner_fence = owner_fence
        self.tmux_session = tmux_session
        self._lease_token = lease_token or secrets.token_urlsafe(32)
        self._message_deliveries: dict[str, str] = {}
        self._daemon_epoch: str | None = None
        # Check-and-act on the daemon generation is one step, not two: the
        # poll and heartbeat loops are sibling tasks and both observe the
        # epoch, so without this two of them can pass the "changed?" test and
        # issue redundant refreshes -- or, worse, one can consume the change
        # while the other is mid-flight.
        self._generation_lock = anyio.Lock()
        self._started = False

    async def start(self) -> dict[str, Any]:
        registration = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="session.register",
            params={
                "cwd": self.cwd,
                "command": list(self.command),
                "source": "claude-channel",
                "runtime": RuntimeKind.CLAUDE_INTERACTIVE,
                "channelConfirmed": True,
                "channelBuildVersion": __version__,
                "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
                "ownerFence": self.owner_fence,
                "channelLeaseToken": self._lease_token,
                **(
                    {"tmuxSession": self.tmux_session}
                    if self.tmux_session is not None
                    else {}
                ),
            },
            mutation=True,
        )
        self._remember_daemon_epoch(registration)
        self._started = True
        return await self.poll_once()

    @property
    def started_once(self) -> bool:
        return self._started

    async def stop(self) -> dict[str, Any]:
        return await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="session.unregister",
            params={},
            mutation=True,
        )

    async def delivery_edge(self, delivery_id: str) -> bool:
        enqueued = self.coordinator.enqueue(self.actor, delivery_id)
        await self.coordinator.dispatch_due()
        return enqueued

    async def poll_once(self, *, force_rewake: bool = False) -> dict[str, Any]:
        tracked = self.coordinator.pending_deliveries(self.actor)
        result, generation_refreshed = await self._read_daemon(fetched=False)
        delivery_ids = self._remember_messages(result, observed=False)
        self._complete_out_of_band(tracked, delivery_ids)
        if force_rewake or generation_refreshed:
            self.coordinator.rearm(self.actor, delivery_ids)
        await self.coordinator.dispatch_due()
        return result

    async def harness_read(self) -> dict[str, Any]:
        tracked = self.coordinator.pending_deliveries(self.actor)
        result, _generation_refreshed = await self._read_daemon(fetched=True)
        delivery_ids = self._remember_messages(result, observed=True)
        self._complete_out_of_band(tracked, delivery_ids)
        self.coordinator.observe_read(self.actor, delivery_ids)
        return result

    async def harness_reply(self, message_id: str, message: str) -> dict[str, Any]:
        result = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="message.reply",
            params={"messageId": message_id, "message": message},
            mutation=True,
        )
        self._complete_if_terminal(message_id, result)
        return result

    async def harness_ack(self, message_id: str) -> dict[str, Any]:
        result = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="message.ack",
            params={"messageId": message_id},
            mutation=True,
        )
        self._complete_if_terminal(message_id, result)
        return result

    async def _read_daemon(self, *, fetched: bool) -> tuple[dict[str, Any], bool]:
        result = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="message.pending.list",
            params=session_fetch_params() if fetched else {},
            mutation=False,
        )
        refreshed = await self._refresh_changed_daemon_generation(result)
        return result, refreshed

    async def refresh_generation(self) -> dict[str, Any]:
        """Confirm the persisted session by ref without changing ownership."""

        refreshed = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="session.refresh",
            params={"channelLeaseToken": self._lease_token},
            mutation=False,
        )
        self._remember_daemon_epoch(refreshed)
        return refreshed

    async def recover_generation(self) -> dict[str, Any]:
        """Refresh the fenced lease and immediately re-wake existing backlog."""

        await self.refresh_generation()
        return await self.poll_once(force_rewake=True)

    async def heartbeat(self) -> dict[str, Any]:
        """Renew the daemon's monotonic operational-liveness lease.

        The opaque token prevents unrelated local IPC clients from renewing a
        session by accident. It is not a security boundary against another
        process running as the same OS user.
        """

        result = await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method="session.heartbeat",
            params={"channelLeaseToken": self._lease_token},
            mutation=False,
        )
        # ⛔ NOT `_remember_daemon_epoch`: the heartbeat is a SIBLING task of
        # the poll loop and sees the same epoch.  Silently storing it made the
        # heartbeat CONSUME the restart signal -- the poll would then compare
        # the new epoch against the new epoch, find no change, and never
        # confirm the session against the daemon that replaced the one it
        # registered with.  Whichever observer reached the new daemon first
        # decided whether the refresh happened at all, which is why the
        # failure was intermittent and why no timeout could see it: the event
        # simply never occurred (CI 16430 / 16581 / 16644).
        await self._refresh_changed_daemon_generation(result)
        return result

    def _remember_daemon_epoch(self, result: dict[str, Any]) -> None:
        epoch = result.get("daemonEpoch")
        if isinstance(epoch, str) and epoch:
            self._daemon_epoch = epoch

    async def _refresh_changed_daemon_generation(self, result: dict[str, Any]) -> bool:
        # The MCP server can receive a tool call immediately after initialize,
        # concurrently with the poll task's one-time registration. That call is
        # not allowed to turn an absent desired session into a refresh loop.
        if not self._started:
            return False
        epoch = result.get("daemonEpoch")
        if not isinstance(epoch, str) or not epoch:
            return False
        async with self._generation_lock:
            # Re-read under the lock: a sibling observer may have refreshed
            # this same generation while we waited for it.
            if epoch == self._daemon_epoch:
                return False
            refreshed = await self.refresh_generation()
            refreshed_epoch = refreshed.get("daemonEpoch")
            if not isinstance(refreshed_epoch, str) or not refreshed_epoch:
                raise RuntimeError("session.refresh response omitted daemonEpoch")
            self._daemon_epoch = refreshed_epoch
        return True

    def _remember_messages(
        self, result: dict[str, Any], *, observed: bool
    ) -> tuple[str, ...]:
        messages = result.get("messages", ())
        if not isinstance(messages, list):
            raise TypeError("daemon pending response messages must be an array")
        delivery_ids: list[str] = []
        for value in messages:
            if not isinstance(value, dict):
                continue
            message_id = value.get("messageId")
            delivery_id = value.get("deliveryId")
            if not isinstance(message_id, str) or not isinstance(delivery_id, str):
                continue
            self._message_deliveries[message_id] = delivery_id
            delivery_ids.append(delivery_id)
            if not observed:
                self.coordinator.enqueue(self.actor, delivery_id, message_id=message_id)
        return tuple(delivery_ids)

    def _complete_if_terminal(self, message_id: str, result: dict[str, Any]) -> None:
        terminal = result.get("ok") is True and result.get("acknowledged") is True
        if not terminal:
            return
        delivery_id = self._message_deliveries.pop(message_id, None)
        if delivery_id is not None:
            self.coordinator.complete(self.actor, delivery_id)

    def _complete_out_of_band(
        self, tracked: tuple[str, ...], current: tuple[str, ...]
    ) -> None:
        """Complete keys whose deliveries left the daemon queue out of band.

        The inbox is daemon-authoritative: `hyprial ack`, another session, or a
        harness auto-ack may consume a pending message without this session's
        harness_reply/harness_ack ever seeing it.  Only keys tracked before the
        daemon read are eligible, so a delivery enqueued concurrently with the
        read can never be completed against a stale pending list.
        """

        remaining = set(current)
        for delivery_id in tracked:
            if delivery_id not in remaining:
                self.coordinator.complete(self.actor, delivery_id)
        self._message_deliveries = {
            message_id: delivery_id
            for message_id, delivery_id in self._message_deliveries.items()
            if delivery_id in remaining
        }


@dataclass(frozen=True, slots=True)
class _SessionStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


def _poll_backoff(base: float, failures: int, *, cap: float = 5.0) -> float:
    """Grow the retry delay while the daemon is unreachable, capped and bounded."""

    if failures <= 0:
        return base
    return min(cap, base * float(2 ** min(failures - 1, 6)))


def signal_channel_recovery(path: Path) -> None:
    """Atomically coalesce one Claude lifecycle recovery pulse on disk."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


def _consume_channel_recovery(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


async def _run_channel_poll_loop(
    adapter: ClaudeChannelAdapter,
    *,
    poll_interval: float,
    recovery_signal: Path | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Poll daemon-authoritative pending work without ever crashing the child.

    The session is registered once at child startup. After contact loss, the
    child uses ``session.refresh``: a CAS-like confirmation that the persisted
    actor/sessionRef/source still belongs to this exact Channel. A daemon restart
    resets ephemeral routing and presence, so that guarded refresh restores the
    route without invoking last-writer-wins registration. Heartbeat runs in a
    separate task, so slow message polling or a blocked stdio notification
    cannot starve operational liveness. Its opaque token is a same-user misuse
    fence, not hostile-local authentication; ordinary tool IPC and persisted
    desired state do not renew it as side effects.
    ``StatelessDaemonProxy`` reconnects per call, and ``poll_once`` prunes wake
    keys for deliveries that vanished across the restart, so read/reply/ack
    self-heal against the new generation with no Claude Code session restart.

    The one failure that must *not* trigger refresh or registration is a supersede
    verdict (``SESSION_SUPERSEDED``): a newer interactive session now owns this
    actor. Last-writer registration there would steal the actor back and flap
    delivery between the two children. On that verdict the loop returns and goes
    quiet -- it stops refreshing and polling but does not itself tear the child down.
    Whether the child then exits is decided solely by parent-CC liveness
    (``_watch_parent``), never by the daemon's arbitration, so a restart race can
    never terminate the child whose Claude Code parent is still alive.
    """

    started = False
    refresh_required = False
    failures = 0
    while True:
        try:
            lifecycle_recovered = _consume_channel_recovery(recovery_signal)
            if not started:
                # start() performs session.register (last-writer-wins) then an
                # initial poll that re-enqueues pending work and prunes stale keys.
                await adapter.start()
                started = True
            elif refresh_required:
                # After contact loss, prove that this exact persisted session
                # still owns the actor. Never use last-writer registration here:
                # a newer session may have superseded this child during outage.
                await adapter.refresh_generation()
                await adapter.poll_once(force_rewake=True)
                refresh_required = False
            elif lifecycle_recovered:
                # SessionStart (startup/resume/clear/compact) and a failed model
                # turn both pulse this path. Re-read daemon truth and bypass only
                # the old notification delay; reply/ack remains mandatory.
                await adapter.poll_once(force_rewake=True)
            else:
                await adapter.poll_once()
            failures = 0
        except DaemonRequestRejected as error:
            if error.code == SESSION_SUPERSEDED_CODE:
                # Terminal ownership verdict: a newer session owns this actor.
                # Go quiet -- never re-register (that is the delivery-flap steal).
                if on_error is not None:
                    on_error(error, failures + 1)
                _logger.info(
                    "harness channel for actor %s was superseded by a newer "
                    "session; going quiet (no re-register): %s",
                    adapter.actor,
                    error,
                )
                return
            started = started or getattr(adapter, "started_once", False)
            refresh_required = started
            failures += 1
            _report_poll_failure(error, failures, on_error)
        except _DAEMON_CONTACT_ERRORS as error:
            started = started or getattr(adapter, "started_once", False)
            refresh_required = started
            failures += 1
            _report_poll_failure(error, failures, on_error)
        await anyio.sleep(_poll_backoff(poll_interval, failures))


async def _run_channel_heartbeat_loop(
    adapter: ClaudeChannelAdapter,
    *,
    heartbeat_interval: float = CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Renew operational liveness independently of polling and stdio writes."""

    if heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    failures = 0
    while True:
        if not adapter.started_once:
            await anyio.sleep(min(heartbeat_interval, 0.1))
            continue
        try:
            await adapter.heartbeat()
            failures = 0
        except DaemonRequestRejected as error:
            if error.code == SESSION_SUPERSEDED_CODE:
                if on_error is not None:
                    on_error(error, failures + 1)
                return
            failures += 1
            _report_poll_failure(error, failures, on_error)
        except _DAEMON_CONTACT_ERRORS as error:
            failures += 1
            _report_poll_failure(error, failures, on_error)
        await anyio.sleep(heartbeat_interval)


async def _run_channel_daemon_loops(
    adapter: ClaudeChannelAdapter,
    *,
    poll_interval: float,
    recovery_signal: Path | None = None,
    heartbeat_interval: float = CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> None:
    """Run polling and heartbeat as sibling tasks with independent backpressure."""

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            lambda: _run_channel_poll_loop(
                adapter,
                poll_interval=poll_interval,
                recovery_signal=recovery_signal,
                on_error=on_error,
            )
        )
        tasks.start_soon(
            lambda: _run_channel_heartbeat_loop(
                adapter,
                heartbeat_interval=heartbeat_interval,
                on_error=on_error,
            )
        )


def _report_poll_failure(
    error: BaseException,
    failures: int,
    on_error: Callable[[BaseException, int], None] | None,
) -> None:
    if on_error is not None:
        on_error(error, failures)
    else:
        _logger.warning(
            "harness channel daemon contact failed (attempt %d); the stdio "
            "child stays up and refreshes its fenced lease on reconnect: %s",
            failures,
            error,
        )


async def _watch_parent(
    *,
    getppid: Callable[[], int],
    poll_interval: float,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    signal_process: Callable[[int, int], None] = os.kill,
    read_identity: Callable[[int], str | None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Return ``True`` once the parent Claude Code process dies (orphaning).

    The stdio child does *not* reliably see EOF when Claude Code exits: a real
    pipe's write end can be held open by an inherited fd (a sibling MCP child of
    the same Claude Code session), and empirically the orphaned child then
    lingers forever -- exactly the accumulation this reaper exists to prevent.
    Managed launches pass the long-lived launcher PID plus its process-birth
    marker.  That owner exists for exactly the Claude wait and remains
    observable even if this child starts only after it has been reparented; the
    marker fences PID reuse.  A direct ``hyprial mcp claude-channel`` launch remains
    compatible by falling back to ``os.getppid()`` drift.  Both paths key on
    process liveness rather than registration recency, so supersede alone never
    tears down a child whose owner is still alive.
    """

    if (owner_pid is None) != (owner_identity is None):
        raise ValueError("channel owner pid and identity must be provided together")
    resolved_read_identity = (
        _read_process_identity if read_identity is None else read_identity
    )
    if owner_pid is not None and owner_identity is not None:
        mismatch_since: float | None = None
        while True:
            status = _owner_process_status(
                owner_pid,
                owner_identity,
                signal_process=signal_process,
                read_identity=resolved_read_identity,
            )
            if status is _OwnerProcessStatus.PID_MISSING:
                _logger.warning(
                    "harness channel owner fence failed reason=%s pid=%d; "
                    "shutting down the orphaned stdio child",
                    status,
                    owner_pid,
                )
                return True
            if status is _OwnerProcessStatus.IDENTITY_MISMATCH:
                now = monotonic()
                if mismatch_since is None:
                    mismatch_since = now
                mismatch_age = max(0.0, now - mismatch_since)
                if mismatch_age >= _OWNER_IDENTITY_MISMATCH_GRACE_SECONDS:
                    _logger.warning(
                        "harness channel owner fence failed reason=%s pid=%d "
                        "durationMs=%d; shutting down the orphaned stdio child",
                        status,
                        owner_pid,
                        int(mismatch_age * 1000),
                    )
                    return True
            else:
                # A matching identity or an unreadable/permission-denied probe
                # breaks the continuous mismatch window. UNKNOWN is fail-safe:
                # an observation gap must never advance a possibly-live owner
                # toward reaping.
                mismatch_since = None
            await anyio.sleep(poll_interval)

    original_ppid = getppid()
    if original_ppid <= 1:
        _logger.info(
            "harness channel started after its parent process had already "
            "exited (ppid %d); shutting down the orphaned stdio child",
            original_ppid,
        )
        return True
    while True:
        await anyio.sleep(poll_interval)
        if getppid() != original_ppid:
            _logger.info(
                "harness channel parent process exited (ppid %d -> %d); shutting "
                "down the orphaned stdio child",
                original_ppid,
                getppid(),
            )
            return True


def _read_darwin_process_identity(pid: int) -> str | None:
    """Read one macOS process birth time without the sandboxed ``ps`` CLI."""

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = library.proc_pidinfo
    except (OSError, AttributeError):
        return None
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _DarwinProcBsdInfo()
    try:
        read = proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except (OSError, ValueError):
        return None
    if (
        read != ctypes.sizeof(info)
        or info.pid != pid
        or info.start_sec <= 0
    ):
        return None
    return f"darwin-starttime:{info.start_sec}:{info.start_usec}"


#: Component schemes a process-birth marker may carry. A marker is one or more
#: ``scheme:value`` components joined by ``;``; a single-component marker is
#: indistinguishable from the historical single-scheme strings, so markers
#: written by older builds parse in the same grammar.
_KNOWN_IDENTITY_SCHEMES = frozenset(
    {"proc-starttime", "darwin-starttime", "ps-lstart"}
)

#: The ps fallback prints a local-time string, so its environment is pinned:
#: an unpinned ``lstart`` changes with the reader's TZ/LC, and the launcher
#: and the Claude-Code-spawned child do not share one environment. Pinning
#: makes the component a function of the process birth alone.
_PS_IDENTITY_ENV = {"PATH": os.defpath, "TZ": "UTC0", "LC_ALL": "C"}


def _identity_components(marker: str) -> dict[str, str]:
    """Split a marker into comparable ``scheme -> value`` components.

    A piece without a recognized scheme opaques the whole marker: comparing
    two unknown formats component-wise would invent evidence either way.
    """

    components: dict[str, str] = {}
    for piece in marker.split(";"):
        scheme, separator, value = piece.partition(":")
        if not separator or scheme not in _KNOWN_IDENTITY_SCHEMES:
            return {"raw": marker}
        components[scheme] = value
    return components


def _read_proc_process_identity(pid: int) -> str | None:
    """Read the procfs starttime component (Linux)."""

    stat_path = _PROC_ROOT / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # A Linux host without procfs can still use the portable fallback.
        # If procfs exists but this PID vanished between kill(0) and read,
        # retain the child for this observation; the next liveness probe
        # will produce PID_MISSING.
        return None
    except (OSError, UnicodeError):
        # Permission denial and unreadable procfs are deliberately
        # fail-safe. Do not switch marker formats mid-process and mistake
        # that format change for PID reuse.
        return None
    # /proc/<pid>/stat field 2 (comm) may contain spaces or ')'. Split
    # after its final ')' so remainder[19] is field 22, starttime.
    comm_end = stat.rfind(")")
    remainder = stat[comm_end + 2 :].split() if comm_end >= 0 else []
    if len(remainder) <= 19 or not remainder[19].isdigit():
        return None
    return f"proc-starttime:{remainder[19]}"


def _read_ps_process_identity(pid: int) -> str | None:
    """Read the portable ps component under a pinned TZ/LC environment."""

    try:
        process = _PROCESS_POPEN(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_PS_IDENTITY_ENV,
        )
        stdout, _ = process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    except OSError:
        return None
    if process.returncode != 0:
        return None
    identity = stdout.strip()
    return f"ps-lstart:{identity}" if identity else None


def _resolve_tmux_pane_owner(
    tmux_session: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    read_identity: Callable[[int], str | None] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[int, str] | None:
    """Resolve the owner fence of a detached-tmux TUI to its pane process.

    A detached-tmux launcher exits right after spawn, so its PID can never be
    the owner fence: the pane's top process lives exactly as long as the tmux
    session and takes that role instead.  Any lookup failure degrades to the
    getppid compatibility watch rather than fencing a live session to death.
    """

    resolved_read_identity = (
        _read_process_identity if read_identity is None else read_identity
    )
    tmux_bin = which("tmux")
    if tmux_bin is None:
        return None
    try:
        result = run(
            [
                tmux_bin,
                "display-message",
                "-p",
                "-t",
                tmux_session,
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        pid = int(str(result.stdout).strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    identity = resolved_read_identity(pid)
    if identity is None:
        return None
    return pid, identity


def _read_process_identity(pid: int) -> str | None:
    """Return a multi-component process-birth marker for PID reuse fencing.

    Every component the platform offers is recorded, so a reader that later
    loses one source (a sandboxed ps spawn, a transiently unreadable procfs)
    still matches on the surviving component instead of flipping the whole
    marker format mid-watch and mimicking PID reuse.
    """

    if pid <= 0:
        return None
    components: list[str] = []
    if sys.platform.startswith("linux"):
        proc_identity = _read_proc_process_identity(pid)
        if proc_identity is not None:
            # procfs answered; keep the historical single-component marker
            # and skip the ps spawn entirely.
            return proc_identity
    if sys.platform == "darwin":
        native_identity = _read_darwin_process_identity(pid)
        if native_identity is not None:
            components.append(native_identity)
    ps_identity = _read_ps_process_identity(pid)
    if ps_identity is not None:
        components.append(ps_identity)
    return ";".join(components) if components else None


def _owner_process_status(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] = os.kill,
    read_identity: Callable[[int], str | None] = _read_process_identity,
) -> _OwnerProcessStatus:
    """Classify a safe owner-fence observation without exposing its marker.

    Comparison is component-wise so markers can cross formats (an older
    launcher wrote a single-scheme string; a newer child may read several
    components). Any shared component with an equal value proves the same
    process. A shared component whose values ALL differ is positive PID-reuse
    evidence (IDENTITY_MISMATCH). No shared component -- like an unreadable
    marker -- is NO evidence: UNKNOWN, fail-safe, and never on the reaping
    path. "cannot read" and "read and it differs" must stay distinct
    verdicts; confusing them is what both killed live owners (format skew
    read as mismatch) and kept orphans alive (unreadable read as mismatch's
    opposite)."""

    try:
        signal_process(pid, 0)
    except ProcessLookupError:
        return _OwnerProcessStatus.PID_MISSING
    except (PermissionError, OSError):
        return _OwnerProcessStatus.UNKNOWN
    observed_identity = read_identity(pid)
    if observed_identity is None:
        return _OwnerProcessStatus.UNKNOWN
    expected_components = _identity_components(expected_identity)
    observed_components = _identity_components(observed_identity)
    shared = expected_components.keys() & observed_components.keys()
    for scheme in shared:
        if expected_components[scheme] == observed_components[scheme]:
            return _OwnerProcessStatus.ALIVE
    if shared:
        return _OwnerProcessStatus.IDENTITY_MISMATCH
    return _OwnerProcessStatus.UNKNOWN


def _owner_process_alive(
    pid: int,
    expected_identity: str,
    *,
    signal_process: Callable[[int, int], None] = os.kill,
    read_identity: Callable[[int], str | None] = _read_process_identity,
) -> bool:
    """Check owner liveness without mistaking PID reuse for the original owner.

    Permission denial and an unreadable birth marker are fail-safe: retain the
    channel rather than risk killing one whose owner may still be alive.
    """

    return _owner_process_status(
        pid,
        expected_identity,
        signal_process=signal_process,
        read_identity=read_identity,
    ) in {_OwnerProcessStatus.ALIVE, _OwnerProcessStatus.UNKNOWN}


async def run_channel_session(
    read_stream: Any,
    write_stream: Any,
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    cwd: str,
    command: tuple[str, ...],
    logger: Logger | None = None,
    poll_interval: float = 0.5,
    recovery_signal: Path | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
    getppid: Callable[[], int] | None = None,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    parent_poll_interval: float = 1.0,
    on_parent_death: Callable[[], None] | None = None,
    tmux_session: str | None = None,
) -> None:
    """Run the channel MCP server, its resilient poll loop, and a parent watch.

    Factored out of ``serve_channel_stdio`` so the exact production wiring -- the
    same server, coordinator, adapter and poll loop -- can be driven over
    in-memory streams in tests, including a daemon restart mid-session and a
    parent-death teardown (``getppid``/``on_parent_death`` are injectable for
    that; production defaults to real ``os.getppid`` and ``os._exit``).

    Three concurrent concerns, deliberately independent:

    * ``run_server`` -- the stdio MCP server; its return (a clean stream close)
      tears the session down.
    * ``poll_daemon`` -- the daemon poll loop; it may go quiet on a supersede
      verdict but never tears the session down itself.
    * ``watch_parent`` -- reaps the child when its managed launch owner (or the
      direct parent on the compatibility path) dies. It keys on process
      liveness so it can never fire while that owner is alive. On owner death it
      best-effort unregisters and then
      *force-exits the process*: cancelling the task group is not enough because
      the mcp stdio reader thread can stay blocked on a pipe that never sees EOF
      (an inherited write-end), keeping the process alive indefinitely.
    """

    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if parent_poll_interval <= 0:
        raise ValueError("parent_poll_interval must be positive")
    if owner_pid is None and tmux_session is not None:
        # Detached-tmux launches outlive their launcher, so the launcher PID
        # can never be the owner fence here. The pane's top process lives
        # exactly as long as the tmux session does; when the lookup fails the
        # getppid compatibility watch below still applies.
        resolved_owner = _resolve_tmux_pane_owner(tmux_session)
        if resolved_owner is not None:
            owner_pid, owner_identity = resolved_owner
    resolved_getppid = os.getppid if getppid is None else getppid
    resolved_on_parent_death = (
        (lambda: os._exit(0)) if on_parent_death is None else on_parent_death
    )
    session = InteractiveSession(
        actor=actor,
        cwd=cwd,
        command=command,
        source="claude-channel",
        session_ref=session_ref,
        runtime=RuntimeKind.CLAUDE_INTERACTIVE,
        channel_confirmed=True,
    )
    coordinator = WakeCoordinator(
        _SessionStore(DesiredState(interactive_sessions=(session,))),
        ClaudeChannelDriver(StdioChannelNotifier(write_stream)),
        logger=logger,
    )
    adapter = ClaudeChannelAdapter(
        proxy,
        coordinator,
        actor=actor,
        session_ref=session_ref,
        cwd=cwd,
        command=command,
        owner_fence=owner_pid is not None and owner_identity is not None,
        tmux_session=tmux_session,
    )
    # Import lazily to keep the server's TYPE_CHECKING-only adapter edge and
    # avoid a module initialization cycle.
    from .server import create_channel_mcp_server

    mcp_server = create_channel_mcp_server(adapter)
    lowlevel = mcp_server._lowlevel_server
    _install_legacy_discover_handler(lowlevel)
    initialized = anyio.Event()

    async def on_initialized(_ctx: Any, _params: types.NotificationParams) -> None:
        initialized.set()

    lowlevel.add_notification_handler(
        "notifications/initialized", types.NotificationParams, on_initialized
    )

    async with anyio.create_task_group() as tasks:

        async def run_server() -> None:
            try:
                await lowlevel.run(
                    read_stream,
                    write_stream,
                    channel_initialization_options(lowlevel),
                )
            finally:
                tasks.cancel_scope.cancel()

        async def poll_daemon() -> None:
            await initialized.wait()
            try:
                await _run_channel_daemon_loops(
                    adapter,
                    poll_interval=poll_interval,
                    recovery_signal=recovery_signal,
                    on_error=on_error,
                )
            finally:
                # Best-effort daemon-side cleanup; a daemon that is down at
                # shutdown must not turn teardown into a crash. A superseded child
                # unregisters no-op (the fence is ref-guarded), so this never
                # clobbers the new owner's registration.
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(1.0):
                        try:
                            await adapter.stop()
                        except _DAEMON_CONTACT_ERRORS:
                            pass

        async def watch_parent() -> None:
            orphaned = await _watch_parent(
                getppid=resolved_getppid,
                poll_interval=parent_poll_interval,
                owner_pid=owner_pid,
                owner_identity=owner_identity,
            )
            if not orphaned:
                return
            # Best-effort: drop this session's registration so the daemon does not
            # keep routing to a dead child, then force-exit. adapter.stop() is
            # ref-guarded on the daemon side, so an already-superseded child's
            # unregister is a no-op and never clobbers the new owner.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(1.0):
                    try:
                        await adapter.stop()
                    except _DAEMON_CONTACT_ERRORS:
                        pass
            resolved_on_parent_death()

        tasks.start_soon(run_server)
        tasks.start_soon(poll_daemon)
        tasks.start_soon(watch_parent)


async def serve_channel_stdio(
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    cwd: str,
    command: tuple[str, ...],
    logger: Logger | None = None,
    poll_interval: float = 0.5,
    recovery_signal: Path | None = None,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    tmux_session: str | None = None,
) -> None:
    """Run one CC-owned stdio MCP child and poll daemon-authoritative pending work.

    A daemon restart (the deploy path) drops the child's Unix socket to the
    daemon but never the Claude Code <-> child stdio transport, so the child
    stays alive, reconnects to the new daemon and re-registers -- no Claude Code
    session restart or ``/mcp reconnect`` required. The transport stays stdio
    deliberately: Claude Code channels (the wake edge) are stdio-only, so moving
    the coordinator channel to HTTP would silently delete wake.
    """

    async with stdio_server() as (read_stream, write_stream):
        await run_channel_session(
            read_stream,
            write_stream,
            proxy,
            actor=actor,
            session_ref=session_ref,
            cwd=cwd,
            command=command,
            logger=logger,
            poll_interval=poll_interval,
            recovery_signal=recovery_signal,
            owner_pid=owner_pid,
            owner_identity=owner_identity,
            tmux_session=tmux_session,
        )
