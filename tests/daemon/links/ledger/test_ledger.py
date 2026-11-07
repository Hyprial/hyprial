"""L1-L13: durable link ledger inside the inbox SQLite authority (WP2 §3.9).

Every test drives a real SQLite database through the production inbox
service (and, for the command path, the actor facade).  L14 (crash recovery
through composition's ``LinkInboxPort``) is not here: it needs the
composition adapter, which WP2-B has not delivered.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.inbox.contracts.api as inbox_api
import hyprial.daemon.impl.inbox.contracts.ports as inbox_ports
from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService

A = "agent:o:n1:a"
B = "agent:o:n1:b"
TTL = 60_000
NOW = 1_700_000_000_000


def _open(database: Path) -> tuple[InboxService, MemoryDeliveryTransport]:
    transport = MemoryDeliveryTransport()
    transport.set_online(A, True)
    transport.set_online(B, True)
    return InboxService(database, transport, node_id="n1"), transport


def _message(
    message_id: str,
    *,
    sender: str = A,
    recipient: str = B,
    intent: str = "request",
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> InboxMessage:
    import json

    payload = json.dumps(body if body is not None else {"text": "hi"}).encode()
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=sender,
        recipient=recipient,
        payload=payload,
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
        idempotency_key=idempotency_key,
        expires_at_ms=NOW + TTL,
    )


def _outbox_row(database: Path, message_id: str) -> sqlite3.Row | None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()


def _inbox_row(database: Path, message_id: str) -> sqlite3.Row | None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()


def _ledger_rows(database: Path) -> dict[tuple[str, str], sqlite3.Row]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM link_ledger").fetchall()
    return {(str(r["actor"]), str(r["message_id"])): r for r in rows}


def _out_open(message_id: str, *, sender: str = A, peer: str = B, state: str = "open") -> Any:
    return inbox_api.LinkLedgerOpen(
        actor=sender,
        message_id=message_id,
        peer=peer,
        intent="request",
        reply_to=None,
        state=state,
        opened_at_ms=NOW,
        expires_at_ms=NOW + TTL,
    )


def _update(**fields: Any) -> Any:
    return inbox_ports.UpdateLinkLedgerCommand(
        correlation_id="test:link-ledger", now_ms=NOW, **fields
    )


def test_l1_migration_adds_column_and_tables_idempotently(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE outbox (message_id TEXT PRIMARY KEY, "
            "conversation_id TEXT NOT NULL, sender TEXT NOT NULL, "
            "recipient TEXT NOT NULL, payload BLOB NOT NULL, intent TEXT NOT NULL, "
            "lifecycle TEXT NOT NULL, idempotency_key TEXT, "
            "created_at_ms INTEGER NOT NULL, message_expires_at_ms INTEGER, "
            "expires_at_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "next_attempt_ms INTEGER NOT NULL, last_error TEXT)"
        )
        connection.commit()
    service, _transport = _open(database)
    try:
        columns = {
            str(row[1])
            for row in service._db.execute("PRAGMA table_info(outbox)").fetchall()
        }
        assert "link_hold" in columns
        tables = {
            str(row[0])
            for row in service._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"link_ledger", "link_task_bindings"} <= tables
        service._create_link_ledger_schema()  # reopening must not raise
    finally:
        service.close()


def test_l2_held_submit_is_queued_without_io_and_records_ledger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        result = service.submit(
            _message("m1"),
            now_ms=NOW,
            link_hold="LINK_MINTING",
            link_open=_out_open("m1", state="held"),
        )
        assert result.accepted is True
        assert result.queued is True
        assert transport.delivered == []
        row = _outbox_row(database, "m1")
        assert row is not None
        assert str(row["link_hold"]) == "LINK_MINTING"
        assert int(row["next_attempt_ms"]) == int(row["expires_at_ms"])
        ledger = InboxReadProjection(database).link_ledger_snapshot(NOW)
        match = [r for r in ledger if r.message_id == "m1"]
        assert len(match) == 1
        assert match[0].state == "held"
        assert match[0].direction == "out"
        held = InboxReadProjection(database).link_held_outbox()
        assert [item.message.message_id for item in held] == ["m1"]
        assert held[0].link_hold == "LINK_MINTING"
    finally:
        service.close()


def test_l3_retry_and_wake_skip_a_held_row(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        service.submit(
            _message("m1"), now_ms=NOW, link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        assert service.retry_due(now_ms=NOW + 1) == []
        assert transport.delivered == []
        assert service.wake_outbox_recipient(B, now_ms=NOW + 1) is False
        assert service.retry_due(now_ms=NOW + 2) == []
        assert _outbox_row(database, "m1") is not None
    finally:
        service.close()


def test_l4_expiry_moves_a_held_row_to_the_dlq(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        service.submit(
            _message("m1"), now_ms=NOW, link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        outcomes = service.retry_due(now_ms=NOW + TTL)
        assert [o.code for o in outcomes] == ["TTL_EXPIRED"]
        assert transport.delivered == []
        assert _outbox_row(database, "m1") is None
        row = _ledger_rows(database)[(A, "m1")]
        assert str(row["state"]) == "released"
    finally:
        service.close()


def test_l5_release_clears_the_hold_and_the_send_stamps_sent_at_ms(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        transport.confirm_direct("m1")
        service.submit(
            _message("m1"), now_ms=NOW, link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        result = service.update_link_ledger(
            _update(release=("m1",))
        )
        assert result.released == ("m1",)
        row = _outbox_row(database, "m1")
        assert row is not None
        assert row["link_hold"] is None
        assert int(row["next_attempt_ms"]) == NOW
        assert _ledger_rows(database)[(A, "m1")]["state"] == "open"
        service.retry_due(now_ms=NOW)
        assert transport.delivered == ["m1"]
        sent_at = _ledger_rows(database)[(A, "m1")]["sent_at_ms"]
        assert sent_at is not None and int(sent_at) >= NOW
    finally:
        service.close()


def test_l6_reason_update_and_fail_terminalize_the_row(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.submit(
            _message("m1"), now_ms=NOW, link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        service.update_link_ledger(_update(reasons={"m1": "LINK_PEER_FULL"}))
        assert str(_outbox_row(database, "m1")["link_hold"]) == "LINK_PEER_FULL"
        result = service.update_link_ledger(
            _update(fail={"m1": "LINK_RECLAIMED"})
        )
        assert result.failed == ("m1",)
        assert _outbox_row(database, "m1") is None
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            dlq = connection.execute(
                "SELECT reason FROM dlq WHERE message_id = ?", ("m1",)
            ).fetchone()
        assert dlq is not None and str(dlq["reason"]) == "LINK_RECLAIMED"
        assert _ledger_rows(database)[(A, "m1")]["state"] == "released"
    finally:
        service.close()


def test_l7_release_is_conditional_on_liveness_and_ttl(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.submit(
            _message("m1"), now_ms=NOW, link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        expired = service.update_link_ledger(
            inbox_ports.UpdateLinkLedgerCommand(
                correlation_id="test:expire", now_ms=NOW + TTL, release=("m1",)
            )
        )
        assert expired.released == ()
        assert expired.expired == ("m1",)
        assert _outbox_row(database, "m1") is not None
        missing = service.update_link_ledger(_update(release=("nope",)))
        assert missing.released == ()
        assert missing.expired == ()
    finally:
        service.close()


def test_l8_reply_release_closes_and_acks_the_original(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.receive(_message("orig", sender=B, recipient=A), now_ms=NOW)
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'orig', 'in', ?, 'request', NULL, 'open', ?, ?)""",
            (A, B, NOW, NOW + TTL),
        )
        service._db.commit()
        reply = _message(
            "reply1",
            sender=A,
            recipient=B,
            intent="reply",
            body={"replyTo": "orig"},
        )
        service.submit(
            reply,
            now_ms=NOW,
            link_hold="LINK_MINTING",
            link_open=inbox_api.LinkLedgerOpen(
                actor=A,
                message_id="reply1",
                peer=B,
                intent="reply",
                reply_to="orig",
                state="held",
                opened_at_ms=NOW,
                expires_at_ms=NOW + TTL,
            ),
        )
        result = service.update_link_ledger(_update(release=("reply1",)))
        assert result.released == ("reply1",)
        inbound = _inbox_row(database, "orig")
        assert inbound is not None and bool(inbound["consumed"]) is True
        assert _ledger_rows(database)[(A, "orig")]["state"] == "closed"
        assert int(_ledger_rows(database)[(A, "orig")]["sample_pending"]) == 1
    finally:
        service.close()


def test_l8b_held_reply_without_an_out_row_settles_its_original_on_release(
    tmp_path: Path,
) -> None:
    """Rework 1 (P1): the association lives on the outbox row, not the ledger.

    ``message.reply`` parks a held reply in the outbox and books no ledger
    ``out`` row, so release must derive the association from the row itself
    and settle the original in the same transaction: the question is
    consumed, its ``in`` row is closed with a pending sample, and the hold
    column is cleared -- one command, one transaction, all three effects.
    """

    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.receive(_message("orig", sender=B, recipient=A), now_ms=NOW)
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'orig', 'in', ?, 'request', NULL, 'open', ?, ?)""",
            (A, B, NOW, NOW + TTL),
        )
        service._db.commit()
        reply = _message(
            "reply1",
            sender=A,
            recipient=B,
            intent="reply",
            body={"replyTo": "orig"},
            idempotency_key="reply:orig",
        )
        # No ``link_open``: the reply path books no ``out`` row (§2.4 reply 5).
        service.submit(reply, now_ms=NOW, link_hold="LINK_PEER_FULL")
        assert _ledger_rows(database).get((A, "reply1")) is None

        result = service.update_link_ledger(_update(release=("reply1",)))

        assert result.released == ("reply1",)
        inbound = _inbox_row(database, "orig")
        assert inbound is not None and bool(inbound["consumed"]) is True
        settled = _ledger_rows(database)[(A, "orig")]
        assert str(settled["state"]) == "closed"
        assert int(settled["sample_pending"]) == 1
        assert _outbox_row(database, "reply1")["link_hold"] is None
    finally:
        service.close()


def test_l8c_held_reply_with_a_settled_original_release_fails_the_reply(
    tmp_path: Path,
) -> None:
    """Rework 1 (B5): an original that is no longer pending fails the row."""

    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.receive(_message("orig", sender=B, recipient=A), now_ms=NOW)
        service.ack(A, "orig")
        reply = _message(
            "reply1",
            sender=A,
            recipient=B,
            intent="reply",
            body={"replyTo": "orig"},
            idempotency_key="reply:orig",
        )
        service.submit(reply, now_ms=NOW, link_hold="LINK_PEER_FULL")

        result = service.update_link_ledger(_update(release=("reply1",)))

        assert result.failed == ("reply1",)
        assert result.released == ()
        assert _outbox_row(database, "reply1") is None
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            dlq = connection.execute(
                "SELECT reason FROM dlq WHERE message_id = ?", ("reply1",)
            ).fetchone()
        assert dlq is not None
        assert str(dlq["reason"]) == "MESSAGE_REPLY_UNAVAILABLE"
    finally:
        service.close()


def test_l9_direct_reply_close_marks_the_inbound_row(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        transport.confirm_direct("reply1")
        service.receive(_message("orig", sender=B, recipient=A), now_ms=NOW)
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'orig', 'in', ?, 'request', NULL, 'open', ?, ?)""",
            (A, B, NOW, NOW + TTL),
        )
        service._db.commit()
        service.submit(
            _message(
                "reply1", sender=A, recipient=B, intent="reply",
                body={"replyTo": "orig"},
            ),
            now_ms=NOW,
            link_reply_close=True,
        )
        assert _ledger_rows(database)[(A, "orig")]["state"] == "closed"
        assert int(_ledger_rows(database)[(A, "orig")]["sample_pending"]) == 1
    finally:
        service.close()


class _SinkGate:
    def __init__(self) -> None:
        self.injected_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.discards: list[dict[str, Any]] = []

    def admit_send(self, **kwargs: Any) -> Any:  # pragma: no cover - never asked
        raise AssertionError("admit_send must not be asked by the inbox")

    def sender_known(self, **kwargs: Any) -> bool:
        return True

    def admit_dispatch(self, **kwargs: Any) -> bool:
        return True

    def settle_hint(self, **kwargs: Any) -> str:
        return "keep"

    def injected(self, **kwargs: Any) -> None:
        self.injected_calls.append(kwargs)

    def reply_received(self, **kwargs: Any) -> None:
        self.reply_calls.append(kwargs)

    def discard(self, **kwargs: Any) -> None:
        self.discards.append(kwargs)


def test_l10_receive_closes_the_outbound_row_and_notifies_the_sink(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        gate = _SinkGate()
        service._link_admission.bind(gate)  # type: ignore[arg-type]
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'req1', 'out', ?, 'request', NULL, 'open', ?, ?)""",
            (A, B, NOW, NOW + TTL),
        )
        service._db.commit()
        service.receive(
            _message(
                "reply1", sender=B, recipient=A, intent="reply",
                body={"replyTo": "req1"},
            ),
            now_ms=NOW + 5,
        )
        row = _ledger_rows(database)[(A, "req1")]
        assert str(row["state"]) == "closed"
        assert int(row["event_at_ms"]) == NOW + 5
        assert int(row["sample_pending"]) == 1
        assert len(gate.reply_calls) == 1
        assert gate.reply_calls[0]["reply_to"] == "req1"

        wrong = _message(
            "reply2", sender="agent:o:n1:c", recipient=A, intent="reply",
            body={"replyTo": "req1"},
        )
        service.receive(wrong, now_ms=NOW + 6)
        assert _ledger_rows(database)[(A, "req1")]["state"] == "closed"
    finally:
        service.close()


def test_l11_injection_writes_an_inbound_row_and_notifies_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        gate = _SinkGate()
        service._link_admission.bind(gate)  # type: ignore[arg-type]
        service.receive(_message("in1", sender=B, recipient=A), now_ms=NOW)
        fetched = service.fetch_pending(A, now_ms=NOW + 3)
        assert [m.message_id for m in fetched] == ["in1"]
        rows = _ledger_rows(database)
        assert rows[(A, "in1")]["direction"] == "in"
        assert int(rows[(A, "in1")]["opened_at_ms"]) == NOW
        assert len(gate.injected_calls) == 1
        assert gate.injected_calls[0]["received_at_ms"] == NOW
        fetched_again = service.fetch_pending_admitted(A, now_ms=NOW + 4)
        assert [m.message_id for m in fetched_again] != []
        assert len(gate.injected_calls) == 1  # INSERT OR IGNORE, one callback
    finally:
        service.close()


def test_l12_drain_acks_in_one_transaction_and_cancel_terminalizes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, transport = _open(database)
    try:
        service.receive(_message("m1", sender=B, recipient=A), now_ms=NOW)
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'm1', 'in', ?, 'request', NULL, 'open', ?, ?)""",
            (A, B, NOW, NOW + TTL),
        )
        service._db.commit()
        service.submit(_message("m2"), now_ms=NOW)  # ordinary retry row
        result = service.update_link_ledger(
            _update(drain={A: ("m1",)}, cancel_outbound={"m2": "LINK_RECLAIMED"})
        )
        assert result.drained == ((A, "m1"),)
        assert result.cancelled == ("m2",)
        inbound = _inbox_row(database, "m1")
        assert inbound is not None and bool(inbound["consumed"]) is True
        assert _ledger_rows(database)[(A, "m1")]["state"] == "drained"
        assert _outbox_row(database, "m2") is None
    finally:
        service.close()


def test_l13_reads_and_purge_respect_liveness(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.receive(_message("waiting", sender=B, recipient=A), now_ms=NOW)
        service._db.execute(
            """INSERT INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, 'drained1', 'in', ?, 'request', NULL, 'drained', ?, ?)""",
            (A, B, NOW, NOW + 10),
        )
        service._db.commit()
        live = InboxReadProjection(database).link_inbound_live(A, B)
        assert [row.message_id for row in live] == ["waiting"]
        assert live[0].injected is False
        assert live[0].received_at_ms == NOW
        assert InboxReadProjection(database).link_inbound_live(A, "agent:o:n1:c") == ()
        service.update_link_ledger(
            inbox_ports.UpdateLinkLedgerCommand(
                correlation_id="test:purge",
                now_ms=NOW + 20,
                purge_before_ms=NOW + 20,
            )
        )
        assert (A, "drained1") not in _ledger_rows(database)
        assert [
            row.message_id
            for row in InboxReadProjection(database).link_inbound_live(A, B)
        ] == ["waiting"]
    finally:
        service.close()


def test_r22_drain_books_a_durable_terminal_without_a_ledger_row(
    tmp_path: Path,
) -> None:
    """E26-08: a reclaimed link's request that was never injected has no
    ledger row.  Draining it must still write the inbox terminal and a
    ``drained`` ledger row in the same transaction, and a restart must keep
    both (otherwise the request can be re-injected).
    """

    database = tmp_path / "inbox.sqlite3"
    service, _transport = _open(database)
    try:
        service.receive(_message("old1", sender=B, recipient=A), now_ms=NOW)
        # The request waited in the inbox; the carrier never pulled it, so the
        # ledger has no inbound row to update.
        assert _ledger_rows(database) == {}
        result = service.update_link_ledger(_update(drain={A: ("old1",)}))
        assert result.drained == ((A, "old1"),)
        inbound = _inbox_row(database, "old1")
        assert inbound is not None
        assert bool(inbound["consumed"]) is True
        assert inbound["acknowledged_at_ms"] == NOW
        ledger = _ledger_rows(database)[(A, "old1")]
        assert ledger["direction"] == "in"
        assert ledger["state"] == "drained"
        assert int(ledger["event_at_ms"]) == NOW
        # A repeat drain is idempotent: same terminal, no second accounting.
        again = service.update_link_ledger(_update(drain={A: ("old1",)}))
        assert again.drained == ((A, "old1"),)
        assert _ledger_rows(database)[(A, "old1")]["state"] == "drained"
    finally:
        service.close()

    # Restart against the same file: the durable facts alone stay terminal.
    reopened, _transport = _open(database)
    try:
        inbound = _inbox_row(database, "old1")
        assert inbound is not None and bool(inbound["consumed"]) is True
        assert inbound["acknowledged_at_ms"] is not None
        assert _ledger_rows(database)[(A, "old1")]["state"] == "drained"
        assert InboxReadProjection(database).link_inbound_live(A, B) == ()
    finally:
        reopened.close()


def test_l5_actor_facade_command_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The actor path runs the same command through its mailbox."""

    import hyprial.daemon.impl.inbox.actor.worker as worker_module

    clock = [NOW]
    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(
            time_ns=lambda: clock[0] * 1_000_000,
            monotonic=time.monotonic,
            sleep=time.sleep,
        ),
    )
    database = tmp_path / "actor.sqlite3"
    from hyprial.daemon.impl.inbox import (
        DeliveryCustodyCoordinator,
        DeliveryCustodyFacade,
    )

    class _Events:
        def publish(self, _event: object) -> None:
            return None

    transport = MemoryDeliveryTransport()
    transport.set_online(B, True)
    transport.confirm_direct("m1")
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        facade.submit(
            _message("m1"),
            now_ms=NOW,
            link_hold="LINK_MINTING",
            link_open=_out_open("m1"),
        )
        assert facade.update_link_ledger(_update(release=("m1",))).released == ("m1",)
        assert facade.outbox_item_for_message("m1").link_hold is None
    finally:
        facade.close()
