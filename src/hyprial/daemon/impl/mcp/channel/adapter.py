"""ClaudeChannelAdapter: per-session daemon adapter for the channel edge."""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from hyprial import __version__
from hyprial.kernel import (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
    ipc_errors,
)

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
    DSH_INTERACTIVE = "dsh_interactive"
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
        turn_signal_dir: Path | None = None,
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
        #: cursor -> ordered member ids of the snapshot that cursor froze.
        self._batch_members: dict[str, tuple[str, ...]] = {}
        #: Newest cursor this session holds; successors branch from it.
        self._chain_head: str | None = None
        #: Cursor whose text a real model turn demonstrably took.
        self._consumed_cursor: str | None = None
        self.watch = InboxWatch(
            poll_interval_ms=poll_interval_ms,
            heartbeat_interval_ms=heartbeat_interval_ms,
        )
        # Deliveries the coordinator tracked when the in-flight list was sent:
        # only those may be completed against its answer.
        self._tracked_before_list: tuple[str, ...] = ()
        # Model-turn state (wp5c.md §4.2).  The open turn survives a child
        # restart on disk; the injected ids live only in this process.
        self.turn_signal_dir = turn_signal_dir
        self.open_turn: str | None = None
        self._turn_origin = "pulse"
        self._turn_started = False
        self.turn_injected: set[str] = set()
        self.owed_settle: set[str] = set()
        self._turn_lock = anyio.Lock()
        self._load_open_turn()

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
        """Turn one list answer into one batch edge, then wake what is due.

        ``message.pending.list`` is an observation/doorbell here.  The sole
        consuming read is ``message.pending.batch``, called exactly at the
        injection edge, and its ``delivery.text`` is the whole injection.
        """

        self._complete_out_of_band(
            self._tracked_before_list, _pending_ids(response)
        )
        if change is None:
            await self.coordinator.dispatch_due()
            return
        if change.rewake:
            # A daemon epoch/generation change re-announces the batch already
            # in flight, by replaying its exact cursor (never a fresh capture
            # of a changed count).  The generation fence makes this one
            # explicit pre-consumption replay per generation.
            await self._capture_batch(replay=self._chain_head)
            if self._chain_head is not None:
                self.coordinator.rearm_batch(
                    self.actor,
                    cursor=self._chain_head,
                    generation=str(response.get("daemonEpoch") or "recovery"),
                )
        elif change.new:
            await self._capture_batch(successor=self._consumed_cursor)
        await self.coordinator.dispatch_due()

    async def _capture_batch(
        self, *, replay: str | None = None, successor: str | None = None
    ) -> None:
        params: dict[str, Any] = {}
        if replay is not None:
            params["cursor"] = replay
        elif successor is not None:
            params["afterCursor"] = successor
        result = await self._tool_call("message.pending.batch", params, mutation=False)
        self._remember_batch(result)

    def _remember_batch(self, result: dict[str, Any]) -> bool:
        """Accept one daemon batch as this session's outstanding edge."""

        cursor = result.get("cursor")
        items = result.get("items")
        delivery = result.get("delivery")
        if not isinstance(cursor, str) or not cursor:
            return False
        if not isinstance(items, list) or not isinstance(delivery, dict):
            return False
        text = delivery.get("text")
        members = tuple(
            str(item["messageId"])
            for item in items
            if isinstance(item, dict) and isinstance(item.get("messageId"), str)
        )
        for item in items:
            if not isinstance(item, dict) or not isinstance(
                item.get("messageId"), str
            ):
                self._count_invalid_row()
        if not isinstance(text, str) or not text or not members:
            return False
        existing = self.coordinator.batch_members(self.actor, cursor=cursor)
        if existing:
            # Same snapshot re-announced: keep the accepted text/id set.
            members = existing
        self._batch_members[cursor] = members
        self._chain_head = cursor
        for message_id in members:
            self._message_deliveries[message_id] = cursor
        self.coordinator.enqueue_batch(
            self.actor, cursor=cursor, message_ids=members, text=text
        )
        return True

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
        # The start signal may trail the first fetched read: report the turn
        # start before the daemon stamps the fetch (a late start misses the
        # reminder window).  A start that cannot be confirmed fails the read.
        async with self._turn_lock:
            if self.open_turn is None:
                self._begin_turn(uuid4().hex, "read")
            await self._ensure_started(self._tool_call)
        tracked = self.coordinator.pending_deliveries(self.actor)
        # The tool read is itself a consumption edge: replay the cursor this
        # session already holds (exact same text/ids) or capture the first
        # batch.  The answer is the shared schema; nothing is re-rendered.
        params: dict[str, Any] = (
            {"cursor": self._chain_head} if self._chain_head is not None else {}
        )
        result = await self._tool_call("message.pending.batch", params, mutation=False)
        self._note_injected(result)
        # A tool read can be the first to see a restarted daemon.
        self.watch.observe(result)
        cursor = result.get("cursor")
        if isinstance(cursor, str) and cursor:
            members = self._batch_members.get(
                cursor, self.coordinator.batch_members(self.actor, cursor=cursor)
            )
            if members:
                self._batch_members[cursor] = tuple(members)
                self._chain_head = cursor
                for message_id in members:
                    self._message_deliveries[message_id] = cursor
                if not self.coordinator.observe_batch_consumed(
                    self.actor, cursor=cursor
                ):
                    self._remember_batch(result)
                    self.coordinator.observe_batch_consumed(
                        self.actor, cursor=cursor
                    )
                self._consumed_cursor = cursor
        self._complete_out_of_band(tracked, _pending_ids(result, key="items"))
        return result

    async def harness_reply(
        self, message_id: str, summary: str, message: str
    ) -> dict[str, Any]:
        result = await self._tool_call(
            "message.reply",
            {"messageId": message_id, "summary": summary, "message": message},
            mutation=True,
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

    # -- model turns (wp5c.md §4) ------------------------------------------

    async def apply_turn_start(self, report_id: str) -> None:
        """Handle one ``UserPromptSubmit`` pulse; raises when unconfirmed."""

        async with self._turn_lock:
            if self.open_turn is not None and self.open_turn != report_id:
                if self._turn_origin == "read":
                    # The late start of a turn ``harness_read`` already opened.
                    self._turn_origin = "pulse"
                    self._save_open_turn()
                    return
                # A started turn that never saw Stop (StopFailure): close it
                # failed -- nothing it injected is settled -- and open this one.
                await self._call(
                    "session.turn.ended",
                    {"reportIds": [self.open_turn], "status": "failed"},
                    mutation=True,
                )
                self._clear_open_turn()
                self.turn_injected.clear()
            if self.open_turn is None:
                self._begin_turn(report_id, "pulse")
            await self._ensure_started(self._call)

    async def apply_turn_end(self) -> bool:
        """Close the open turn under its own reportId; False when none is open.

        Raises when the daemon did not confirm: the pulse and the on-disk
        state stay and the next round retries the same reportId.
        """

        async with self._turn_lock:
            if self.open_turn is None:
                return False
            await self._ensure_started(self._call)
            await self._call(
                "session.turn.ended", {"reportIds": [self.open_turn]}, mutation=True
            )
            self.owed_settle |= self.turn_injected
            self.turn_injected = set()
            self._clear_open_turn()
            return True

    async def settle_owed(self) -> None:
        """Do the daemon's settle hint for each id a finished turn injected.

        ``ack`` acks, ``keep`` writes nothing, an exception leaves the id for
        the next round.  Nothing is ever replied on the model's behalf.
        """

        for message_id in sorted(self.owed_settle):
            try:
                hint = await self._call(
                    "message.settle_hint", {"messageId": message_id}, mutation=False
                )
                if hint.get("hint") == "ack":
                    try:
                        result = await self._call(
                            "message.ack", {"messageId": message_id}, mutation=True
                        )
                    except DaemonRequestRejected as error:
                        if error.code != ipc_errors.MESSAGE_ACK_UNAVAILABLE:
                            raise
                    else:
                        self._complete_if_terminal(message_id, result)
                        if result.get("acknowledged") is not True:
                            continue
                elif hint.get("hint") != "keep":
                    continue
            except DaemonRequestRejected as error:
                if error.code == SESSION_SUPERSEDED_CODE:
                    self.watch.observe_rejection(error.code)
                    return
                continue
            except _DAEMON_CONTACT_ERRORS:
                return
            self.owed_settle.discard(message_id)

    def _begin_turn(self, report_id: str, origin: str) -> None:
        self.open_turn, self._turn_origin, self._turn_started = report_id, origin, False
        self._save_open_turn()

    async def _ensure_started(self, call: Any) -> None:
        if self.open_turn is not None and not self._turn_started:
            await call("session.turn.started", {"reportId": self.open_turn}, mutation=True)
            self._turn_started = True
            self._save_open_turn()

    def _note_injected(self, result: dict[str, Any]) -> None:
        """Remember what this turn's batch read put in front of the model."""

        for row in result.get("items", ()):
            if isinstance(row, dict) and row.get("kind") != "notice":
                message_id = row.get("messageId")
                if isinstance(message_id, str):
                    self.turn_injected.add(message_id)

    def _open_turn_path(self) -> Path | None:
        directory = self.turn_signal_dir
        return None if directory is None else directory / "state" / "open-turn"

    def _save_open_turn(self) -> None:
        path = self._open_turn_path()
        if path is None or self.open_turn is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        scratch = path.with_name(f"open-turn.{secrets.token_hex(4)}.tmp")
        state = {"reportId": self.open_turn, "origin": self._turn_origin}
        scratch.write_text(
            json.dumps({**state, "started": self._turn_started}), encoding="utf-8"
        )
        os.replace(scratch, path)

    def _clear_open_turn(self) -> None:
        self.open_turn, self._turn_started = None, False
        path = self._open_turn_path()
        if path is not None:
            path.unlink(missing_ok=True)

    def _load_open_turn(self) -> None:
        path = self._open_turn_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path else {}
            report_id = state.get("reportId")
        except (OSError, ValueError, AttributeError):
            return
        if isinstance(report_id, str) and report_id:
            self.open_turn = report_id
            self._turn_origin = "read" if state.get("origin") == "read" else "pulse"
            self._turn_started = state.get("started") is True

    # -- coordinator bookkeeping -------------------------------------------

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
        """Complete members whose messages left the daemon queue out of band.

        The inbox is daemon-authoritative: ``hyprial ack``, another session, or
        a harness auto-ack may consume a pending message without this session's
        harness_reply/harness_ack ever seeing it.  Only cursors tracked before
        the daemon read are eligible, so a batch enqueued concurrently with the
        read can never be completed against a stale pending list.
        """

        present = set(current)
        for cursor in tracked:
            members = self.coordinator.batch_members(self.actor, cursor=cursor)
            for message_id in members:
                if message_id not in present:
                    self.coordinator.complete(self.actor, message_id)
                    self._message_deliveries.pop(message_id, None)
            if not self.coordinator.batch_members(self.actor, cursor=cursor):
                self._batch_members.pop(cursor, None)
        self._message_deliveries = {
            message_id: cursor
            for message_id, cursor in self._message_deliveries.items()
            if cursor in self._batch_members
            or self.coordinator.batch_members(self.actor, cursor=cursor)
        }


def _pending_ids(
    result: dict[str, Any], *, key: str = "messages"
) -> tuple[str, ...]:
    """Message ids one pending-list or batch answer names (invalid rows skipped)."""

    rows = result.get(key, ())
    if not isinstance(rows, list):
        return ()
    return tuple(
        str(row["messageId"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("messageId"), str)
    )


def _now_ms() -> int:
    return int(time.monotonic() * 1000)
