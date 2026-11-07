"""Outbox prune: delete only provably dead letters, never in-flight entries.

The production backlog this pins: legacy ``channel:``/``lark:``/``adapter:``
recipients queued before the send-time address allowlist existed can never
be delivered, yet they retry forever behind ``ok: true``.  Prune moves those
(and TTL-expired rows) to the DLQ and must leave deliverable, unexpired
entries untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import (
    DaemonApplication,
    _undeliverable_outbox_recipient,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def _message(message_id: str, recipient: str, created_at_ms: int = 0) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conv-prune",
        sender="node-1",
        recipient=recipient,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


class _Presence:
    def __init__(self, actors: tuple[str, ...] = ()) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def application(tmp_path: Path, service: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-1",
    **_deep_module_daemon_dependencies())
    app._inbox = service
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def test_recipient_scheme_allowlist_matches_send_time_rule() -> None:
    # C1 (2026-08-22): the predicate now mirrors the CURRENT send gate —
    # host-class rows (TARGET_IS_NODE) are dead since the gate rejects node
    # sends outright; the pre-migration reason to keep them is gone.
    assert _undeliverable_outbox_recipient("channel:hyprial")
    assert not _undeliverable_outbox_recipient("channel:lark:squire")
    assert _undeliverable_outbox_recipient("lark:ops-bot")
    assert _undeliverable_outbox_recipient("adapter:lark-main")
    assert not _undeliverable_outbox_recipient("user:allen")
    assert _undeliverable_outbox_recipient("peer-node")
    assert not _undeliverable_outbox_recipient("agent:allen:mac:hq-adjutant")


def test_prune_moves_dead_letters_to_dlq_and_keeps_in_flight(
    tmp_path: Path,
) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    # Offline deliverable recipients queue; legacy schemes queued the same way
    # before the send-time allowlist rejected them.
    for message_id, recipient in (
        ("msg-channel", "channel:hyprial"),
        ("msg-lark", "lark:ops-bot"),
        ("msg-adapter", "adapter:lark-main"),
        ("msg-agent", "agent:allen:mac:hq-adjutant"),
        ("msg-bare", "peer-node"),
    ):
        result = service.submit(_message(message_id, recipient), now_ms=1_000)
        assert result.accepted and result.queued
    assert service.outbox_count() == 5

    pruned = service.prune_outbox(
        undeliverable=_undeliverable_outbox_recipient, now_ms=2_000
    )

    assert sorted(item.message_id for item in pruned) == [
        "msg-adapter",
        "msg-bare",
        "msg-channel",
        "msg-lark",
    ]
    assert {item.reason for item in pruned} == {"UNDELIVERABLE_SCHEME"}
    assert service.outbox_count() == 1
    assert service.dlq_count() == 4
    remaining = {item.message.message_id for item in service.outbox_items()}
    # C1: only the canonical agent URI survives; the bare node row is dead
    # under the current send gate and joined the legacy schemes in the DLQ.
    assert remaining == {"msg-agent"}
    service.close()


def test_prune_expires_ttl_but_spares_unexpired_retries(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    service.submit(_message("msg-old", "peer-node"), now_ms=0)
    service.submit(_message("msg-live", "peer-node-2"), now_ms=10_000)

    just_past_first_ttl = service.durable_ttl_ms + 1
    pruned = service.prune_outbox(
        undeliverable=lambda _recipient: False, now_ms=just_past_first_ttl
    )

    assert [item.message_id for item in pruned] == ["msg-old"]
    assert pruned[0].reason == "TTL_EXPIRED"
    remaining = {item.message.message_id for item in service.outbox_items()}
    assert remaining == {"msg-live"}
    assert service.dlq_count() == 1
    service.close()


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    service.submit(_message("msg-channel", "channel:hyprial"), now_ms=0)
    service.submit(_message("msg-bare", "peer-node"), now_ms=0)

    pruned = service.prune_outbox(
        undeliverable=_undeliverable_outbox_recipient, now_ms=1, dry_run=True
    )

    # C1: the bare node row is dry-run flagged too (host = dead under the
    # current send gate); dry-run still deletes nothing.
    assert sorted(item.message_id for item in pruned) == ["msg-bare", "msg-channel"]
    assert service.outbox_count() == 2
    assert service.dlq_count() == 0
    service.close()


def test_daemon_outbox_prune_and_list_wire_shapes(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app = application(tmp_path, service)
    closed = False

    def close_application() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            app._close()
        finally:
            app._close_home_lease()

    request.addfinalizer(close_application)
    # The daemon path prunes against wall-clock now; seed unexpired entries.
    now = time.time_ns() // 1_000_000
    service.submit(_message("msg-channel", "channel:hyprial", now), now_ms=now)
    service.submit(_message("msg-bare", "peer-node", now), now_ms=now)

    listed = app.handle("outbox.list", {})
    assert listed["ok"] is True
    flagged = {
        entry["messageId"]: entry["undeliverableScheme"]
        for entry in listed["entries"]
    }
    # C1: msg-bare (a bare node address) is flagged like the legacy scheme.
    assert flagged == {"msg-channel": True, "msg-bare": True}

    preview = app.handle("outbox.prune", {"dryRun": True})
    assert preview["ok"] is True and preview["dryRun"] is True
    assert preview["prunedCount"] == 2
    # dry-run deletes nothing: remainingCount stays physical.
    assert preview["remainingCount"] == 2

    result = app.handle("outbox.prune", {})
    assert result["ok"] is True and result["dryRun"] is False
    assert [item["messageId"] for item in result["pruned"]] == ["msg-channel", "msg-bare"]
    assert result["pruned"][0]["reason"] == "UNDELIVERABLE_SCHEME"
    assert result["remainingCount"] == 0
    assert service.dlq_count() == 2
    # The full application owner drains its asynchronous log before the
    # persisted-record assertions below inspect the file.
    close_application()
    records = [
        json.loads(line)
        for line in (tmp_path / "state" / "logs" / "daemon.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    pruned_channel = [record for record in records if record["event"] == "outbox.pruned"]
    assert len(pruned_channel) == 2
    pruned = next(r for r in records if r["event"] == "outbox.pruned" and r["recipient"] == "channel:hyprial")
    assert pruned["messageId"] == "msg-channel"
    assert pruned["correlationId"] == "msg-channel"
    assert pruned["node"] == "delivery-terminal"
    assert pruned["component"] == "daemon"
    assert pruned["recipient"] == "channel:hyprial"
    assert pruned["reason"] == "UNDELIVERABLE_SCHEME"
    assert pruned["attempts"] == 0
    assert isinstance(pruned["ts"], str)
