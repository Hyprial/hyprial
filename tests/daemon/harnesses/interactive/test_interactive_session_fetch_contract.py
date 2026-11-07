"""Shared IPC consumption contract for every interactive session carrier."""

from __future__ import annotations

import time
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

from hyprial.kernel import SESSION_CARRIER_SOURCES, session_fetch_params
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RetryPolicy,
)
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class CommitWithoutReceiptTransport(MemoryDeliveryTransport):
    """Leave sender outbox and recipient inbox rows side by side."""

    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        # Read paths hide rows past their TTL by wall clock: receive at "now".
        self.service.receive(message, now_ms=time.time_ns() // 1_000_000)
        # Keep the original request in the reproduced split inbox/outbox state.
        # Correlated replies are not the subject of this contract and may settle.
        return message.intent == "reply"


class EmptyPresence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False


def carrier_app(
    tmp_path: Path,
    source: str,
    *,
    alarms: list[tuple[str, str]] | None = None,
    sender: str = "channel:lark:test-app",
) -> tuple[DaemonApplication, InboxService, str, InboxMessage]:
    actor_name = f"{source}-worker"
    actor = f"agent:op:node-1:{actor_name}"
    transport = CommitWithoutReceiptTransport()
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,
        retry_policy=RetryPolicy(),
        interactive_recipient=lambda recipient: recipient == actor,
        alarm_human_delivery=(
            None
            if alarms is None
            else lambda alarm, _text: (
                not alarms.append((alarm.message_id, alarm.reason))
            )
        ),
    )
    transport.service = inbox
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = EmptyPresence()  # type: ignore[assignment]
    # Explicit premise (wp2.md §4.9 option a): this test is about outbox
    # retirement, not links, so a permissive gate keeps harness_reply
    # delivering instead of holding on an unbound slot (ruling 54).
    app.link_admission.bind(PermissiveLinkGate())
    app.handle(
        "session.register",
        {
            "actor": actor_name,
            "sessionRef": f"session-{source}",
            "cwd": str(tmp_path),
            "command": [source],
            "source": source,
            "runtime": f"{source.split('-', 1)[0]}_interactive",
        },
    )
    # Read paths hide rows past their TTL by wall clock, so seed at "now".
    now_ms = time.time_ns() // 1_000_000
    message = InboxMessage(
        message_id=f"message-{source}",
        conversation_id="conversation-carrier",
        sender=sender,
        recipient=actor,
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=now_ms,
    )
    assert inbox.submit(message, now_ms=now_ms).queued
    return app, inbox, actor_name, message


def close(app: DaemonApplication, inbox: InboxService) -> None:
    inbox.close()
    app.agents.close()


class DaemonConnection:
    def __init__(self, app: DaemonApplication) -> None:
        self.app = app

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.app.handle(method, params)


class DaemonFactory:
    def __init__(self, app: DaemonApplication) -> None:
        self.app = app

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[DaemonConnection]:
        yield DaemonConnection(self.app)


@pytest.mark.parametrize("source", sorted(SESSION_CARRIER_SOURCES))
def test_explicit_fetch_retires_sender_outbox_for_every_carrier(
    tmp_path: Path, source: str
) -> None:
    app, inbox, actor, message = carrier_app(tmp_path, source)
    params = {"actor": actor, "sessionRef": f"session-{source}"}
    try:
        observed = app.handle("message.pending.list", params)
        assert [item["messageId"] for item in observed["messages"]] == [
            message.message_id
        ]
        assert inbox.outbox_count() == 1

        fetched = app.handle(
            "message.pending.list", {**params, **session_fetch_params()}
        )

        assert [item["messageId"] for item in fetched["messages"]] == [
            message.message_id
        ]
        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 0
    finally:
        close(app, inbox)


@pytest.mark.parametrize("source", sorted(SESSION_CARRIER_SOURCES))
def test_ack_retires_sender_outbox_for_every_carrier(
    tmp_path: Path, source: str
) -> None:
    app, inbox, actor, message = carrier_app(tmp_path, source)
    try:
        acknowledged = app.handle(
            "message.ack",
            {
                "actor": actor,
                "sessionRef": f"session-{source}",
                "messageId": message.message_id,
            },
        )

        assert acknowledged["acknowledged"] is True
        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 0
    finally:
        close(app, inbox)


@pytest.mark.anyio
@pytest.mark.parametrize("source", sorted(SESSION_CARRIER_SOURCES))
@pytest.mark.parametrize("consume_via", ("harness_reply", "harness_ack", "hyprial_ack"))
async def test_completion_mode_never_changes_sender_outbox_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    consume_via: str,
) -> None:
    app, inbox, actor, message = carrier_app(
        tmp_path,
        source,
        # Foreign-node canonical sender: canonical URIs that name THIS node
        # without a claimant fail message.reply loudly (TARGET_UNRESOLVED),
        # bare node-style senders fail loudly too (TARGET_IS_NODE), and this
        # test only needs a reply that retires from the outbox.
        sender="agent:op:sender-node:dispatcher",
    )
    try:
        assert {item.message.message_id for item in inbox.outbox_items()} == {
            message.message_id
        }

        if consume_via in {"harness_reply", "harness_ack"}:
            server = create_mcp_server(
                StatelessDaemonProxy(DaemonFactory(app)),
                actor=actor,
                session_ref=f"session-{source}",
            )
            async with (
                InMemoryTransport(server) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as client,
            ):
                await client.initialize()
                result = await client.call_tool(
                    consume_via,
                    (
                        {
                            "messageId": message.message_id,
                            "summary": "done",
                            "message": "done",
                        }
                        if consume_via == "harness_reply"
                        else {"messageId": message.message_id}
                    ),
                )
            assert result.is_error is not True
            payload = result.structured_content or json.loads(result.content[0].text)
            assert payload["acknowledged"] is True
        else:
            monkeypatch.setattr(
                _cli_support,
                "_daemon_request",
                lambda method, params=None: app.handle(method, params or {}),
            )
            result = CliRunner().invoke(
                _cli_root_app,
                ["message", "ack", message.message_id, "--from", actor, "--json"],
            )
            assert result.exit_code == 0, result.output
            assert json.loads(result.stdout)["acknowledged"] is True

        assert message.message_id not in {
            item.message.message_id for item in inbox.outbox_items()
        }
        inbox.retry_due(now_ms=1_000)
        assert inbox.dlq_count() == 0
        assert inbox.system_notices(message.sender) == ()
    finally:
        close(app, inbox)


@pytest.mark.parametrize("source", sorted(SESSION_CARRIER_SOURCES))
def test_unfetched_message_still_alarms_for_every_carrier(
    tmp_path: Path, source: str
) -> None:
    alarms: list[tuple[str, str]] = []
    app, inbox, _actor, message = carrier_app(tmp_path, source, alarms=alarms)
    try:
        inbox.retry_due(
            now_ms=inbox.outbox_item(message.message_id).expires_at_ms
        )

        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 1
        assert alarms == [(message.message_id, "TTL_EXPIRED")]
    finally:
        close(app, inbox)


def test_headless_daemon_completion_remains_a_noop_safe_retirement(
    tmp_path: Path,
) -> None:
    transport = CommitWithoutReceiptTransport()
    inbox = InboxService(tmp_path / "headless.sqlite3", transport)
    transport.service = inbox
    message = InboxMessage(
        message_id="message-headless",
        conversation_id="conversation-headless",
        sender="channel:lark:test-app",
        recipient="agent:op:node-1:wk-retry",
        payload=b"work",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )
    try:
        assert inbox.submit(message, now_ms=0).queued
        assert inbox.ack(message.recipient, message.message_id).acknowledged
        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 0
    finally:
        inbox.close()


def test_closed_interactive_carrier_sources_include_dsh_tui() -> None:
    # dsh-tui is a CLI-hosted interactive carrier: its MCP bridge joins the
    # same closed register/refresh/heartbeat protocol as the other TUIs.
    assert SESSION_CARRIER_SOURCES == {
        "claude-channel",
        "dsh-channel",
        "pi-extension",
        "codex-app-server",
    }
