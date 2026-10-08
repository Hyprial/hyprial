"""ClaudeChannelAdapter: per-session daemon adapter for the channel edge."""
from __future__ import annotations

import logging
import os
import secrets
import time
from enum import StrEnum
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from hyprial import __version__
from hyprial.kernel import (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
)
from hyprial.kernel import session_fetch_params

from hyprial.daemon.impl.inbox_watch import (
    Heartbeat,
    InboxChange,
    InboxWatch,
    ListInbox,
    Ok,
    Outcome,
    Quiet,
    Refresh,
    Register,
    Rejected,
    Request,
    Sleep,
    Unreachable,
    Wait,
)
from hyprial.daemon.impl.mcp.api  import (
    SESSION_SUPERSEDED_CODE,
    DaemonDisconnected,
    DaemonRequestRejected,
)
from hyprial.daemon.impl.mcp.channel.ownership import _DAEMON_CONTACT_ERRORS
from hyprial.daemon.impl.mcp.channel.ownership import (
    _read_process_identity as process_birth_identity,
)
from hyprial.daemon.impl.mcp.proxy  import StatelessDaemonProxy
from hyprial.daemon.impl.mcp.wake  import WakeCoordinator

from hyprial.daemon.impl.mcp.channel.notifier import (
    CHANNEL_CAPABILITY,
)

_logger = logging.getLogger(__name__)

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
    """Bind one CC session to daemon truth and its ACK-aware wake coordinator.

    The inbox loop itself -- register, refresh after a restart, list, hold on
    the doorbell, decide what is new and what to re-wake -- is the shared
    :class:`InboxWatch` core (``contract/inbox-watch``).  This adapter only
    performs the requests the core asks for and turns its answers into
    WakeCoordinator calls.  The poll and heartbeat loops (``loops.py``) and
    the MCP tool calls all run on one anyio event loop, which is the
    serialization the core requires.
    """

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
        poll_interval_ms: int = 500,
        heartbeat_interval_ms: int = int(CHANNEL_HEARTBEAT_INTERVAL_SECONDS * 1000),
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
        self.watch = InboxWatch(
            poll_interval_ms=poll_interval_ms,
            heartbeat_interval_ms=heartbeat_interval_ms,
        )
        # Deliveries the coordinator tracked when the in-flight list was sent:
        # only those may be completed against its answer.
        self._tracked_before_list: tuple[str, ...] = ()

    # -- the requests the core asks for ------------------------------------

    @property
    def started_once(self) -> bool:
        return self.watch.registered

    async def perform(self, action: Request) -> Outcome:
        """Perform one core request; the error becomes an outcome."""

        try:
            return Ok(await self._request(action))
        except DaemonRequestRejected as error:
            return Rejected(error.code)
        except _DAEMON_CONTACT_ERRORS:
            return Unreachable()

    async def _request(self, action: Request) -> dict[str, Any]:
        if isinstance(action, Register):
            return await self._register()
        if isinstance(action, Refresh):
            return await self._call("session.refresh", {"channelLeaseToken": self._lease_token}, mutation=False)
        if isinstance(action, Heartbeat):
            return await self._call("session.heartbeat", {"channelLeaseToken": self._lease_token}, mutation=False)
        if isinstance(action, ListInbox):
            self._tracked_before_list = self.coordinator.pending_deliveries(self.actor)
            return await self._call("message.pending.list", {}, mutation=False)
        assert isinstance(action, Wait)
        params: dict[str, Any] = {"knownMessageIds": list(action.known_message_ids)}
        if action.hold_ms is not None:
            params["holdMs"] = action.hold_ms
        return await self._call("message.pending.wait", params, mutation=False)

    async def _register(self) -> dict[str, Any]:
        harness_pid = os.getppid()
        harness_identity = process_birth_identity(harness_pid)
        return await self._call(
            "session.register",
            {
                "cwd": self.cwd,
                "command": list(self.command),
                "source": "claude-channel",
                "runtime": RuntimeKind.CLAUDE_INTERACTIVE,
                "channelConfirmed": True,
                "channelBuildVersion": __version__,
                "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
                "turnReporting": True,
                "ownerFence": self.owner_fence,
                "channelLeaseToken": self._lease_token,
                **(
                    {
                        "processPid": harness_pid,
                        "processIdentity": harness_identity,
                    }
                    if harness_pid > 1 and harness_identity is not None
                    else {}
                ),
                **(
                    {"tmuxSession": self.tmux_session}
                    if self.tmux_session is not None
                    else {}
                ),
            },
            mutation=True,
        )

    async def _call(self, method: str, params: dict[str, Any], *, mutation: bool) -> dict[str, Any]:
        return await self.proxy.call(
            actor=self.actor,
            session_ref=self.session_ref,
            method=method,
            params=params,
            mutation=mutation,
        )

    async def _tool_call(self, method: str, params: dict[str, Any], *, mutation: bool) -> dict[str, Any]:
        """A tool's request: its verdict on the session counts like a lane's."""

        try:
            return await self._call(method, params, mutation=mutation)
        except DaemonRequestRejected as error:
            self.watch.observe_rejection(error.code)
            raise

    async def apply(self, change: InboxChange | None, response: dict[str, Any]) -> None:
        """Turn one list answer into coordinator state, then wake what is due."""

        delivery_ids = self._remember_messages(response, observed=False)
        # Every listed row is (re-)enqueued -- idempotent, and it heals a key
        # the coordinator lost; out-of-band completion stays keyed on the
        # deliveries tracked before the read.
        self._complete_out_of_band(self._tracked_before_list, delivery_ids)
        if change is not None and change.rewake:
            self.coordinator.rearm(
                self.actor,
                (
                    self._message_deliveries[row["messageId"]]
                    for row in change.rewake
                    if row["messageId"] in self._message_deliveries
                ),
            )
        await self.coordinator.dispatch_due()

    # -- one-shot helpers (startup, recovery, tests) -----------------------

    async def start(self) -> dict[str, Any]:
        """Register and read the inbox once: the child's first contact."""

        return await self.poll_once()

    async def poll_once(self, *, force_rewake: bool = False) -> dict[str, Any]:
        """Drive the poll lane until one list completes; errors propagate.

        Registration or an owed refresh runs first when the core asks for
        it.  ``force_rewake`` re-notifies the backlog (a lifecycle recovery).
        """

        if force_rewake:
            self.watch.request_rewake("recovery")
        else:
            self.watch.list_now()
        while True:
            action = self.watch.poll_next(_now_ms())
            if isinstance(action, Quiet):
                raise DaemonRequestRejected(SESSION_SUPERSEDED_CODE, "session superseded")
            if isinstance(action, Sleep):
                # Registration is backing off after a failure: report the
                # daemon as unreachable rather than sleep inside a tool call.
                raise DaemonDisconnected("daemon unreachable; registration retry pending")
            try:
                response = await self._request(action)
            except DaemonRequestRejected as error:
                self.watch.poll_done(action, Rejected(error.code), _now_ms())
                raise
            except _DAEMON_CONTACT_ERRORS:
                self.watch.poll_done(action, Unreachable(), _now_ms())
                raise
            change = self.watch.poll_done(action, Ok(response), _now_ms())
            if isinstance(action, ListInbox):
                await self.apply(change, response)
                if not self.watch.refresh_owed:
                    return response
                # The list saw a new daemon: refresh, then list once more so
                # the owed re-wake rides on this same call.
                self.watch.list_now()

    async def stop(self) -> dict[str, Any]:
        return await self._call("session.unregister", {}, mutation=True)

    async def delivery_edge(self, delivery_id: str) -> bool:
        enqueued = self.coordinator.enqueue(self.actor, delivery_id)
        await self.coordinator.dispatch_due()
        return enqueued

    # -- MCP tools ----------------------------------------------------------

    async def harness_read(self) -> dict[str, Any]:
        tracked = self.coordinator.pending_deliveries(self.actor)
        result = await self._tool_call(
            "message.pending.list", session_fetch_params(), mutation=False
        )
        # A tool read can be the first to see a restarted daemon.
        self.watch.observe(result)
        delivery_ids = self._remember_messages(result, observed=True)
        self._complete_out_of_band(tracked, delivery_ids)
        self.coordinator.observe_read(self.actor, delivery_ids)
        return result

    async def harness_reply(self, message_id: str, message: str) -> dict[str, Any]:
        result = await self._tool_call(
            "message.reply", {"messageId": message_id, "message": message}, mutation=True
        )
        self._complete_if_terminal(message_id, result)
        return result

    async def harness_ack(self, message_id: str) -> dict[str, Any]:
        result = await self._tool_call("message.ack", {"messageId": message_id}, mutation=True)
        self._complete_if_terminal(message_id, result)
        return result

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, Any]:
        """Forward Stop pulses through the current interactive-session fence."""

        return await self._call(
            "session.turn.ended", {"reportIds": list(report_ids)}, mutation=True
        )

    # -- coordinator bookkeeping -------------------------------------------

    def _remember_messages(
        self, result: dict[str, Any], *, observed: bool
    ) -> tuple[str, ...]:
        messages = result.get("messages", ())
        if not isinstance(messages, list):
            raise TypeError("daemon pending response messages must be an array")
        delivery_ids: list[str] = []
        for value in messages:
            # Row by row: one row the coordinator cannot accept is skipped
            # and counted, never allowed to stop the rows after it from
            # waking (review 844 on #1152).
            # Rows without a usable messageId are already counted by the
            # core (deliver.invalid); count here only what the core accepted.
            if not isinstance(value, dict):
                continue
            message_id = value.get("messageId")
            delivery_id = value.get("deliveryId")
            if not isinstance(message_id, str) or not message_id:
                continue
            if not isinstance(delivery_id, str):
                self._count_invalid_row()
                continue
            if not observed:
                origin = value.get("origin")
                try:
                    self.coordinator.enqueue(
                        self.actor,
                        delivery_id,
                        message_id=message_id,
                        origin=origin if isinstance(origin, dict) else None,
                    )
                except (TypeError, ValueError):
                    self._count_invalid_row()
                    continue
            self._message_deliveries[message_id] = delivery_id
            delivery_ids.append(delivery_id)
        return tuple(delivery_ids)

    def _count_invalid_row(self) -> None:
        counters = self.watch.counters
        counters["deliver.invalid"] = counters.get("deliver.invalid", 0) + 1

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


def _now_ms() -> int:
    return int(time.monotonic() * 1000)
