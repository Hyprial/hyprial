from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial import __version__
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION
from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.kernel.impl.log import Logger
from hyprial.daemon.impl.inbox_watch import Heartbeat, Ok
from hyprial.daemon.impl.mcp import (
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    StatelessDaemonProxy,
    WakeCommand,
    WakeCoordinator,
    WakeStatus,
    channel_initialization_options,
    create_channel_mcp_server,
)


@dataclass
class FakeStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class FakeConnection:
    def __init__(
        self, responses: list[dict[str, Any]], calls: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self.responses = responses
        self.calls = calls

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return self.responses.pop(0)


class FakeFactory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self):  # type: ignore[no-untyped-def]
        connection = FakeConnection(self.responses, self.calls)

        class ConnectionContext:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return connection

            async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
                return None

        return ConnectionContext()


class RecordingChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("stdio channel closed")
        self.notifications.append((method, params))


def batch_answer(
    messages: list[dict[str, Any]],
    *,
    cursor: str = "batch-1",
    text: str = "[Harness Network inbox batch cursor=batch-1]\nfirst",
) -> dict[str, Any]:
    """One daemon batch answer: the schema + the one injection text."""

    return {
        "ok": True,
        "schemaVersion": 1,
        "format": "inbox-batch-v1",
        "cursor": cursor,
        "daemonEpoch": "epoch-1",
        "actor": "alice",
        "itemCount": len(messages),
        "totalBodyBytes": 0,
        "inlineLimitBytes": 16384,
        "items": messages,
        "delivery": {
            "mode": "inline",
            "text": text,
            "spillPath": None,
            "spillBytes": None,
            "spillSha256": None,
        },
    }


@pytest.mark.anyio
async def test_channel_notification_exposes_message_provenance() -> None:
    channel = RecordingChannel()
    outcome = await ClaudeChannelDriver(channel).wake(
        WakeCommand(
            kind="formal",
            actor="agent:op:node-1:reader",
            delivery_id="delivery-1",
            cwd="/work",
            command=("claude",),
            prompt="read pending",
            origin={
                "via": "operator",
                "onBehalfOf": "agent:op:node-1:worker",
            },
        )
    )

    assert outcome.status is WakeStatus.ACCEPTED
    meta = channel.notifications[0][1]["meta"]
    # Claude Code accepts only string meta values (they become <channel>
    # tag attributes); an object here was rejected and the wake lost.
    assert all(isinstance(value, str) for value in meta.values()), meta
    assert meta["delivery_id"] == "delivery-1"
    assert json.loads(meta["origin"]) == {
        "via": "operator",
        "onBehalfOf": "agent:op:node-1:worker",
    }


def interactive(actor: str = "alice") -> InteractiveSession:
    return InteractiveSession(
        actor=actor,
        cwd=f"/work/{actor}",
        command=("claude", "--resume", actor),
        source="claude-channel",
    )


def test_channel_capability_is_experimental_not_standard_extensions() -> None:
    options = channel_initialization_options()
    wire = options.model_dump(by_alias=True, exclude_none=True)

    assert wire["capabilities"]["experimental"] == {"claude/channel": {}}
    assert "extensions" not in wire["capabilities"]


@pytest.mark.anyio
async def test_channel_discover_stays_on_handshake_era() -> None:
    """server/discover must never advertise the modern era.

    CC >= 2.1.227 excludes modern-era connections from channel candidacy
    (no unsolicited-notification path), so the override installed by the
    channel serve path must advertise handshake versions only (#94).
    """

    from types import SimpleNamespace

    from mcp import types as mcp_types
    from mcp.server.lowlevel import Server as LowlevelServer
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    from hyprial.daemon.impl.mcp.channel.adapter import _install_legacy_discover_handler

    server = LowlevelServer("harness-bridge", version=__version__)
    _install_legacy_discover_handler(server)
    entry = server.get_request_handler("server/discover")
    assert entry is not None
    result = await entry.handler(
        SimpleNamespace(protocol_version="2025-11-25"),
        mcp_types.RequestParams(),
    )
    assert "2026-07-28" not in result.supported_versions
    assert result.supported_versions == list(HANDSHAKE_PROTOCOL_VERSIONS)


@pytest.mark.anyio
async def test_enveloped_discover_then_initialize_succeeds_on_the_channel() -> None:
    """Claude Code 2.1.286+ opens with a 2026-07-28-enveloped server/discover,
    then falls back to initialize on the same stdio connection. The channel's
    serve path must still complete that handshake (it was refused with -32022
    when the dual-era loop locked the connection to the modern era)."""

    import anyio
    from mcp import types as mcp_types
    from mcp.server.lowlevel import Server as LowlevelServer
    from mcp.shared.message import SessionMessage

    from hyprial.daemon.impl.mcp.channel.adapter import (
        _install_legacy_discover_handler,
        channel_initialization_options,
    )
    from hyprial.daemon.impl.mcp.channel.session import serve_handshake_era

    server = LowlevelServer("harness-bridge", version=__version__)
    _install_legacy_discover_handler(server)
    to_server_send, to_server_receive = anyio.create_memory_object_stream[Any](16)
    from_server_send, from_server_receive = anyio.create_memory_object_stream[Any](16)

    def request(request_id: int, method: str, params: dict[str, Any]) -> SessionMessage:
        return SessionMessage(
            mcp_types.JSONRPCRequest(
                jsonrpc="2.0", id=request_id, method=method, params=params
            )
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            serve_handshake_era,
            server,
            to_server_receive,
            from_server_send,
            channel_initialization_options(server),
        )
        await to_server_send.send(
            request(
                1,
                "server/discover",
                {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "claude-code",
                            "version": "2.1.290",
                        },
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            )
        )
        with anyio.fail_after(5):
            discovered = await from_server_receive.receive()
        assert discovered.message.id == 1
        await to_server_send.send(
            request(
                2,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "2.1.290"},
                },
            )
        )
        with anyio.fail_after(5):
            initialized = await from_server_receive.receive()
        message = initialized.message
        assert message.id == 2
        assert isinstance(message, mcp_types.JSONRPCResponse), message
        assert message.result["capabilities"]["experimental"] == {"claude/channel": {}}
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_zero_turn_channel_wake_has_only_delivery_identity_and_read_instruction() -> (
    None
):
    channel = RecordingChannel()
    driver = ClaudeChannelDriver(channel)
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))), driver
    )
    coordinator.enqueue("alice", "delivery-zero-turn")

    outcome = await coordinator.dispatch_due()

    assert outcome[0].status == WakeStatus.SIGNALLED
    assert channel.notifications[0][0] == "notifications/claude/channel"
    params = channel.notifications[0][1]
    assert params["meta"] == {"delivery_id": "delivery-zero-turn"}
    assert "delivery-zero-turn" in params["content"]
    assert "alice" in params["content"]
    assert "harness_read" in params["content"]
    assert "preview" not in params["content"].lower()
    assert "message" not in params


@pytest.mark.anyio
async def test_empty_inbox_on_start_never_starts_a_turn() -> None:
    factory = FakeFactory([{"ok": True}, {"ok": True}, batch_answer([])])
    proxy = StatelessDaemonProxy(factory)
    channel = RecordingChannel()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(channel),
    )
    adapter = ClaudeChannelAdapter(
        proxy,
        coordinator,
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()

    assert [method for method, _params in factory.calls] == [
        "session.register",
        "session.turn.refresh",
        "message.pending.list",
    ]
    registration = factory.calls[0][1]
    assert registration["channelBuildVersion"] == __version__
    assert registration["channelProtocolVersion"] == CHANNEL_PROTOCOL_VERSION
    assert registration["turnReporting"] is True
    assert registration["ownerFence"] is False
    assert len(registration["channelLeaseToken"]) >= 32
    assert "fetched" not in factory.calls[2][1]
    assert channel.notifications == []


@pytest.mark.anyio
async def test_heartbeat_reuses_registration_misuse_fence() -> None:
    factory = FakeFactory([{"ok": True, "alive": True, "daemonEpoch": "epoch"}])
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        WakeCoordinator(
            FakeStore(DesiredState(interactive_sessions=(interactive(),))),
            ClaudeChannelDriver(RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
        lease_token="fixed-private-channel-lease-token-0001",
    )

    outcome = await adapter.perform(Heartbeat())

    assert isinstance(outcome, Ok) and outcome.response["alive"] is True
    assert factory.calls == [
        (
            "session.heartbeat",
            {
                "channelLeaseToken": "fixed-private-channel-lease-token-0001",
                "actor": "alice",
                "sessionRef": "session-1",
            },
        )
    ]


@pytest.mark.anyio
async def test_daemon_epoch_change_uses_guarded_refresh_not_registration() -> None:
    factory = FakeFactory(
        [
            {"ok": True, "daemonEpoch": "epoch-old"},
            {"ok": True},
            {"ok": True, "messages": [], "daemonEpoch": "epoch-old"},
            {"ok": True, "messages": [], "daemonEpoch": "epoch-new"},
            {
                "ok": True,
                "refreshed": True,
                "daemonEpoch": "epoch-new",
                "channelCurrentThisGeneration": True,
            },
            {"ok": True},
            {"ok": True, "messages": [], "daemonEpoch": "epoch-new"},
        ]
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        WakeCoordinator(
            FakeStore(DesiredState(interactive_sessions=(interactive(),))),
            ClaudeChannelDriver(RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()
    await adapter.poll_once()

    # The list that saw the new daemon owes a refresh (never a register),
    # and the owed re-wake rides on one more list in the same call.
    assert [method for method, _params in factory.calls] == [
        "session.register",
        "session.turn.refresh",
        "message.pending.list",
        "message.pending.list",
        "session.refresh",
        "session.turn.refresh",
        "message.pending.list",
    ]
    assert factory.calls[4][1]["actor"] == "alice"
    assert factory.calls[4][1]["sessionRef"] == "session-1"


@pytest.mark.anyio
async def test_restart_resume_resignals_daemon_pending_without_preview() -> None:
    batch_text = "[Harness Network inbox batch cursor=delivery-resume]\nsummary only"
    factory = FakeFactory(
        [
            {"ok": True},
            {"ok": True},
            {
                "ok": True,
                "messages": [
                    {
                        "messageId": "message-1",
                        "deliveryId": "delivery-resume",
                        "preview": "SECRET BODY MUST NOT LEAK",
                    }
                ],
            },
            batch_answer(
                [{"messageId": "message-1", "deliveryId": "delivery-resume"}],
                cursor="delivery-resume",
                text=batch_text,
            ),
        ]
    )
    channel = RecordingChannel()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(channel),
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-resumed",
        cwd="/work/alice",
        command=("claude", "--resume", "session-resumed"),
    )

    await adapter.start()

    assert len(channel.notifications) == 1
    assert channel.notifications[0][1]["content"] == batch_text
    wire = str(channel.notifications[0])
    assert "delivery-resume" in wire
    assert "SECRET BODY MUST NOT LEAK" not in wire


@pytest.mark.anyio
async def test_recovered_session_drains_backlog_from_one_batch_wake_without_new_message() -> (
    None
):
    """Recovery itself re-wakes one turn; that turn replays the whole batch."""

    pending = [
        {"messageId": f"message-{index}", "deliveryId": f"delivery-{index}"}
        for index in range(1, 4)
    ]
    recovered_text = "[Harness Network inbox batch cursor=batch-recovered]\n3 pending items"
    factory = FakeFactory(
        [
            {"ok": True},
            {"ok": True},
            {"ok": True, "messages": []},
            {"ok": True, "messages": pending},
            batch_answer(pending, cursor="batch-recovered", text=recovered_text),
            {"ok": True, "messages": pending},
            batch_answer(pending, cursor="batch-recovered", text=recovered_text),
            batch_answer(pending, cursor="batch-recovered", text=recovered_text),
            batch_answer(pending, cursor="batch-recovered", text=recovered_text),
            *[
                {"ok": True, "acknowledged": True, "messageId": item["messageId"]}
                for item in pending
            ],
            {"ok": True, "messages": []},
        ]
    )
    channel = RecordingChannel()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(channel),
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-recovered",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()
    assert channel.notifications == []

    # The session is open but its model cannot consume. Three deliveries arrive
    # and become one outstanding batch edge; none is acknowledged yet.
    await adapter.poll_once()
    assert len(channel.notifications) == 1
    assert channel.notifications[0][1]["meta"] == {"delivery_id": "batch-recovered"}
    assert "3 pending" in channel.notifications[0][1]["content"]

    # No fourth delivery is injected to prod the session. The recovery signal
    # alone replays the exact cursor and emits one fresh batch edge.
    await adapter.poll_once(force_rewake=True)
    assert len(channel.notifications) == 2
    assert channel.notifications[-1][1]["meta"] == {"delivery_id": "batch-recovered"}
    assert "3 pending" in channel.notifications[-1][1]["content"]

    batch = await adapter.harness_read()
    assert [item["messageId"] for item in batch["items"]] == [
        "message-1",
        "message-2",
        "message-3",
    ]
    pending_calls = [
        params for method, params in factory.calls if method == "message.pending.list"
    ]
    assert all("fetched" not in params for params in pending_calls)

    for item in pending:
        await adapter.harness_ack(item["messageId"])
    assert coordinator.pending_count == 0

    # Once every recovered item is consumed, later polling cannot resurrect a
    # wake edge (and therefore cannot start another turn for the same backlog).
    await adapter.poll_once()
    assert len(channel.notifications) == 2


@pytest.mark.anyio
async def test_channel_wake_logs_stable_message_id_not_epoch_delivery_id(
    tmp_path: Path,
) -> None:
    factory = FakeFactory(
        [
            {"ok": True},
            {"ok": True},
            {
                "ok": True,
                "messages": [
                    {
                        "messageId": "message-stable-1",
                        "deliveryId": "epoch-7:0:message-stable-1",
                    }
                ],
            },
        ]
    )
    factory.responses.append(
        batch_answer(
            [
                {
                    "messageId": "message-stable-1",
                    "deliveryId": "epoch-7:0:message-stable-1",
                }
            ],
            cursor="epoch-7:0:message-stable-1",
            text="[Harness Network inbox batch cursor=epoch-7:0:message-stable-1]",
        )
    )
    logger = Logger.worker(tmp_path, runtime="claude", name="alice")
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(RecordingChannel()),
        logger=logger,
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-trajectory",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()

    record = json.loads(logger.path.read_text().splitlines()[-1])
    assert record["event"] == "wake.signalled"
    # A batch wake is correlated by its first member and its cursor.
    assert record["messageId"] == "message-stable-1"
    assert record["correlationId"] == "message-stable-1"
    assert record["deliveryId"] == "epoch-7:0:message-stable-1"


@pytest.mark.anyio
async def test_read_then_successful_ack_is_the_only_completion_path() -> None:
    factory = FakeFactory(
        [
            {"ok": True},  # session.turn.started: harness_read opens the turn
            batch_answer(
                [{"messageId": "message-1", "deliveryId": "delivery-1"}],
                cursor="delivery-1",
                text="[Harness Network inbox batch cursor=delivery-1]",
            ),
            {"ok": True, "acknowledged": True, "messageId": "message-1"},
        ]
    )
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(RecordingChannel()),
    )
    coordinator.enqueue_batch(
        "alice",
        cursor="delivery-1",
        message_ids=("message-1",),
        text="[Harness Network inbox batch cursor=delivery-1]",
    )
    await coordinator.dispatch_due()
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.harness_read()
    assert coordinator.pending_count == 1

    await adapter.harness_ack("message-1")
    assert coordinator.pending_count == 0


@pytest.mark.anyio
async def test_out_of_band_cli_ack_completes_wake_key_and_stops_repush() -> None:
    """`hyprial message ack` consumes daemon-side; the session's wake key must not orphan."""

    factory = FakeFactory(
        [
            {"ok": True},
            {"ok": True},
            {
                "ok": True,
                "messages": [{"messageId": "message-1", "deliveryId": "message-1"}],
            },
            batch_answer(
                [{"messageId": "message-1", "deliveryId": "message-1"}],
                cursor="cursor-1",
                text="[Harness Network inbox batch cursor=cursor-1]",
            ),
            {"ok": True, "messages": []},
        ]
    )
    channel = RecordingChannel()
    now = {"value": 0.0}
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(channel),
        clock=lambda: now["value"],
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()
    assert coordinator.pending_count == 1
    assert len(channel.notifications) == 1

    # The message left the daemon pending queue out of band (CLI ack) while
    # this session never called harness_reply/harness_ack.  Long past every
    # backoff, the poll must complete the key instead of re-pushing it.
    now["value"] = 3600.0
    await adapter.poll_once()

    assert coordinator.pending_count == 0
    assert len(channel.notifications) == 1


@pytest.mark.anyio
async def test_reconcile_only_completes_disappeared_deliveries() -> None:
    both = [
        {"messageId": "message-1", "deliveryId": "message-1"},
        {"messageId": "message-2", "deliveryId": "message-2"},
    ]
    factory = FakeFactory(
        [
            {"ok": True},
            {"ok": True},
            {"ok": True, "messages": both},
            batch_answer(both, cursor="cursor-both"),
            {
                "ok": True,
                "messages": [{"messageId": "message-2", "deliveryId": "message-2"}],
            },
            {"ok": True, "acknowledged": True, "messageId": "message-2"},
        ]
    )
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(interactive(),))),
        ClaudeChannelDriver(RecordingChannel()),
    )
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        coordinator,
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
    )

    await adapter.start()
    # One batch record claims both members; settlement stays per id.
    assert coordinator.pending_count == 1
    assert coordinator.batch_members("alice", cursor="cursor-both") == (
        "message-1",
        "message-2",
    )

    await adapter.poll_once()
    assert coordinator.pending_count == 1
    assert coordinator.batch_members("alice", cursor="cursor-both") == ("message-2",)

    # The surviving delivery still completes through its own terminal ack.
    await adapter.harness_ack("message-2")
    assert coordinator.pending_count == 0


@pytest.mark.anyio
async def test_channel_stdio_server_keeps_all_harness_tools_in_same_process() -> None:
    factory = FakeFactory([{"ok": True}, {"ok": True}, batch_answer([])])
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        WakeCoordinator(
            FakeStore(DesiredState(interactive_sessions=(interactive(),))),
            ClaudeChannelDriver(RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-fixed",
        cwd="/work/alice",
        command=("claude",),
    )
    server = create_channel_mcp_server(adapter)

    options = channel_initialization_options(server._lowlevel_server)
    assert options.capabilities.experimental == {"claude/channel": {}}
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        initialized = await client.initialize()
        assert initialized.server_info.version == __version__
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("harness_read", {})

    assert names == {
        "harness_ack",
        "harness_progress",
        "harness_read",
        "harness_read_body",
        "harness_reply",
        "harness_send",
        "harness_targets",
        "harness_whoami",
        "orgfs_create",
        "orgfs_export",
        "orgfs_history",
        "orgfs_import",
        "orgfs_invite",
        "orgfs_join",
        "orgfs_ls",
        "orgfs_members",
        "orgfs_mkdir",
        "orgfs_move",
        "orgfs_purge_status",
        "orgfs_read",
        "orgfs_read_at",
        "orgfs_remove",
        "orgfs_remove_member",
        "orgfs_resolve",
        "orgfs_restore",
        "orgfs_spaces",
        "orgfs_stat",
        "orgfs_stat_at",
        "orgfs_status",
        "orgfs_trash",
        "orgfs_watch",
        "orgfs_write",
    }
    assert result.is_error is not True
    assert factory.calls[0][0] == "session.turn.started"
    method, params = factory.calls[1]
    assert method == "message.pending.batch"
    assert params["actor"] == "alice"
    assert params["sessionRef"] == "session-fixed"
    assert "fetched" not in params


@pytest.mark.anyio
async def test_mutating_orgfs_p2_surfaces_are_not_mcp_tools() -> None:
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(FakeFactory([])),
        WakeCoordinator(
            FakeStore(DesiredState(interactive_sessions=(interactive(),))),
            ClaudeChannelDriver(RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-fixed",
        cwd="/work/alice",
        command=("claude",),
    )
    server = create_channel_mcp_server(adapter)

    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert names.isdisjoint(
        {
            "orgfs_checkout",
            "orgfs_purge",
            "orgfs_purge_plan",
            "orgfs_serve",
            "orgfs_unban",
        }
    )


@pytest.mark.anyio
async def test_harness_reply_preserves_already_submitted_result_fields() -> None:
    reply_result = {
        "ok": False,
        "messageId": "message-1",
        "replyMessageId": "reply-1",
        "replied": False,
        "acknowledged": False,
        "queued": True,
        "code": "REPLY_ALREADY_SUBMITTED",
        "retryable": False,
        "settled": False,
    }
    factory = FakeFactory([reply_result])
    adapter = ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        WakeCoordinator(
            FakeStore(DesiredState(interactive_sessions=(interactive(),))),
            ClaudeChannelDriver(RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-fixed",
        cwd="/work/alice",
        command=("claude",),
    )
    server = create_channel_mcp_server(adapter)

    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "harness_reply",
            {"messageId": "message-1", "summary": "different answer", "message": "different answer"},
        )

    assert result.is_error is not True
    assert result.structured_content == reply_result
    assert factory.calls == [
        (
            "message.reply",
            {
                "actor": "alice",
                "sessionRef": "session-fixed",
                "messageId": "message-1",
                "message": "different answer",
                "summary": "different answer",
            },
        )
    ]


# -- the payload Claude Code accepts (inbox-watch delivery cases D1/D2) ------
#
# Claude Code drops a notification it rejects AND the stdio connection with
# it (production 0.5.0, 2026-10-05: a nested meta.origin), so the driver
# repairs a payload before the write instead of letting one wake kill the
# channel -- and instead of refusing it, which would leave the wake at the
# head of its queue, retried forever, blocking every wake behind it.


def _wire_bytes(params: dict[str, Any]) -> bytes:
    """Serialize exactly as the MCP stdio writer does, then encode for stdout."""

    from mcp import types

    return (
        types.JSONRPCNotification(
            jsonrpc="2.0", method="notifications/claude/channel", params=params
        )
        .model_dump_json(by_alias=True, exclude_none=True)
        .encode("utf-8")
    )


def _wake(*, prompt: str = "read pending", origin: dict[str, Any] | None = None) -> WakeCommand:
    return WakeCommand(
        kind="formal",
        actor="agent:op:node-1:reader",
        delivery_id="delivery-1",
        cwd="/work",
        command=("claude",),
        prompt=prompt,
        origin=origin,
    )


@pytest.mark.anyio
async def test_d1_a_nested_origin_with_unicode_and_numbers_is_sent_as_strings() -> None:
    origin = {
        "via": "adapter",
        "sender": {"displayName": "林懿伦", "rank": 3},
        "attempt": 2,
    }
    channel = RecordingChannel()
    driver = ClaudeChannelDriver(channel)

    outcome = await driver.wake(_wake(origin=origin))

    assert outcome.status is WakeStatus.ACCEPTED
    [(_method, params)] = channel.notifications
    assert all(isinstance(value, str) for value in params["meta"].values())
    assert json.loads(params["meta"]["origin"]) == origin
    assert _wire_bytes(params)
    assert driver.repaired_payloads == 0


@pytest.mark.anyio
async def test_d2_a_lone_surrogate_is_repaired_and_the_wake_still_goes_out() -> None:
    channel = RecordingChannel()
    driver = ClaudeChannelDriver(channel)
    # Reproduced by review 831: the old serializer let this through and the
    # stdio writer then raised while encoding -- the channel died anyway.
    outcome = await driver.wake(_wake(origin={"displayName": "bad \ud83d end"}))

    assert outcome.status is WakeStatus.ACCEPTED
    [(_method, params)] = channel.notifications
    assert _wire_bytes(params)  # encodes: no UnicodeEncodeError on stdout
    assert "�" in params["meta"]["origin"]
    assert driver.repaired_payloads == 1


@pytest.mark.parametrize(
    ("params", "sent", "problems"),
    [
        # Documented: meta is optional and empty content is legal.
        ({"content": ""}, {"content": ""}, ()),
        # Documented: non-identifier keys would be dropped silently; drop
        # them here, visibly.
        (
            {"content": "x", "meta": {"delivery_id": "d", "bad-key": "v"}},
            {"content": "x", "meta": {"delivery_id": "d"}},
            ("meta key 'bad-key' is not an identifier",),
        ),
        # A non-string value is what Claude Code rejects: serialize it.
        (
            {"content": "x", "meta": {"n": 1}},
            {"content": "x", "meta": {"n": "1"}},
            ("meta.n was int",),
        ),
        ({"content": "x", "meta": None}, {"content": "x"}, ()),
        # The one defect no repair can fix: nothing is written.
        ({"content": None, "meta": {}}, None, ("content is not a string",)),
    ],
)
def test_repairs_follow_the_documented_payload_rules(
    params: dict[str, Any], sent: dict[str, Any] | None, problems: tuple[str, ...]
) -> None:
    from hyprial.daemon.impl.mcp.channel.notifier import repair_channel_params

    assert repair_channel_params(params) == (sent, problems)
