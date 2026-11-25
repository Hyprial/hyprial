"""``outbox list`` omits removed external ``holdReason`` even for link-held rows (T2b)."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, OutboxItem
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


def _item(message_id: str) -> OutboxItem:
    return OutboxItem(
        message=InboxMessage(
            message_id=message_id,
            conversation_id="c",
            sender="agent:op:node-1:a",
            recipient="agent:op:node-1:b",
            payload=b'{"message":"hi"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        ),
        attempts=0,
        next_attempt_ms=5,
        expires_at_ms=10,
    )


class _Inbox:
    def outbox_items(self) -> list[OutboxItem]:
        return [_item("held"), _item("free")]


def test_outbox_list_omits_hold_reason_on_external_entries(
    tmp_path: Path,
) -> None:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-1",
        **daemon_dependencies(),
    )
    app._inbox = _Inbox()  # type: ignore[assignment]

    try:
        entries = {e["messageId"]: e for e in app._ipc_outbox_list()["entries"]}
    finally:
        app._close()
        app._close_home_lease()

    assert "holdReason" not in entries["held"]
    assert "holdReason" not in entries["free"]
    assert entries["held"]["failedAttempts"] == entries["held"]["attempts"] == 0
