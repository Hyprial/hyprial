"""``outbox list`` publishes a held row's link hold under ``holdReason`` (WP2 §3.6)."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, OutboxItem
from hyprial.daemon.impl.links.ports import HOLD_REASON_KEY
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


def _item(message_id: str, link_hold: str | None) -> OutboxItem:
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
        link_hold=link_hold,
    )


class _Inbox:
    def outbox_items(self) -> list[OutboxItem]:
        return [_item("held", ipc_errors.LINK_PEER_FULL), _item("free", None)]


def test_outbox_list_exposes_the_link_hold_reason_only_for_held_rows(
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

    assert HOLD_REASON_KEY == "holdReason"
    assert entries["held"][HOLD_REASON_KEY] == ipc_errors.LINK_PEER_FULL
    assert HOLD_REASON_KEY not in entries["free"]
    assert entries["held"]["failedAttempts"] == entries["held"]["attempts"] == 0
