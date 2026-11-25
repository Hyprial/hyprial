"""Ungranted SYSTEM bodies become residue after ack + the code default (plan Q18).

A SYSTEM row (``message_class_of`` is SYSTEM: a dispatch id, a PAC reply
key, a daemon notice pair) that no record granted has no hydration.  Once
it has been acknowledged (an inbox row) or settled (a sender copy) for
``DEFAULT_BODY_RETENTION_MS`` the sweep replaces its body by the residue.
Since plan slice S1 the recorder grants SYSTEM rows it records; a granted
row leaves this sweep and is dropped by the grant regime instead.  Every read is unchanged up to the cutoff; after it
``message.read_body`` answers the existing ``MESSAGE_BODY_UNAVAILABLE`` for
a missing body, and ``system_notices`` keep their own prune.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.delivery import (
    DEFAULT_BODY_RETENTION_MS,
    SOURCE_SENDER_COPY,
    SWEEP_BATCH,
    BodyGrant,
    BodyRef,
    payload_sha256,
    text_sha256,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.send_lookup import lookup_send
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX, DaemonRequestError
from tests.daemon.delivery.message.test_message_pending_wait import (
    READER,
    SESSION,
    _reader,
    app as app,
    owned_applications as owned_applications,
)
from tests.support.actor_hosts import new_actor_host

DRIVER = "agent:op:node-1:driver"
CUTOFF = DEFAULT_BODY_RETENTION_MS


class _Events:
    def publish(self, event: object) -> None:
        pass


class _Resolver:
    def __init__(self) -> None:
        self.asked: list[BodyRef] = []

    def resolve(self, ref: BodyRef) -> bytes | None:
        self.asked.append(ref)
        return b"{}"


def _dispatch_id() -> str:
    return f"{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}"


def _message(message_id: str, **fields) -> InboxMessage:
    body = {"message": f"body of {message_id}", "replyTo": "m-0",
            "origin": {"kind": "agent"}}
    return InboxMessage(**{
        "message_id": message_id,
        "conversation_id": "conversation-1",
        "sender": DRIVER,
        "recipient": READER,
        "payload": json.dumps(body).encode(),
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1_000,
        **fields,
    })


@pytest.fixture
def facade(tmp_path: Path) -> Iterator[DeliveryCustodyFacade]:
    path = tmp_path / "inbox.sqlite3"
    authority = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            path, MemoryDeliveryTransport(), _Events(),
            actor_host=new_actor_host(),
        ),
        path,
    )
    try:
        yield authority
    finally:
        authority.close()


def _rows(database: Path, table: str = "inbox") -> dict[str, dict]:
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        return {
            str(row["message_id"]): dict(row)
            for row in db.execute(f"SELECT * FROM {table}")
        }


def _acked(facade: DeliveryCustodyFacade, message: InboxMessage) -> int:
    assert facade.receive(message).accepted
    assert facade.ack(READER, message.message_id).acknowledged
    return int(_rows(facade._database)[message.message_id]["acknowledged_at_ms"])


def test_a_system_body_is_kept_until_the_cutoff_then_becomes_residue(facade) -> None:
    system = _message(_dispatch_id())
    reply = _message(
        "m-pac-reply", intent="reply",
        idempotency_key=f"reply:{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}",
    )
    interaction = _message("m-interaction")
    acks = [_acked(facade, item) for item in (system, reply, interaction)]
    acked = max(acks)
    before = _rows(facade._database)

    facade.prune_inbox(now_ms=min(acks) + CUTOFF - 1)
    assert _rows(facade._database) == before

    facade.prune_inbox(now_ms=acked + CUTOFF)
    after = _rows(facade._database)
    for message in (system, reply):
        row = after[message.message_id]
        assert json.loads(row["payload"]) == {
            "replyTo": "m-0", "origin": {"kind": "agent"},
        }
        assert row["body_dropped_at_ms"] == acked + CUTOFF
        assert row["body_class"] == "system"
        assert row["body_text_sha256"] == text_sha256(message.payload)
        assert row["body_payload_sha256"] is None
        assert row["body_ref"] is None
    kept = after[interaction.message_id]
    assert bytes(kept["payload"]) == interaction.payload
    assert kept["body_dropped_at_ms"] is None
    assert kept["body_payload_sha256"] is None


def test_a_system_residue_is_never_hydrated(facade) -> None:
    system = _message(_dispatch_id())
    acked = _acked(facade, system)
    resolver = _Resolver()
    facade.bind_body_retention(resolver=resolver)
    assert facade.retained_body(system.message_id) is not None

    facade.prune_inbox(now_ms=acked + CUTOFF)

    assert facade.retained_body(system.message_id) is None
    assert resolver.asked == []
    assert facade.interaction_rows_after("inbox", 0).rows == ()


def test_an_unacked_system_row_keeps_its_body(facade) -> None:
    system = _message(_dispatch_id(), expires_at_ms=10 ** 15)
    assert facade.receive(system).accepted
    received = int(_rows(facade._database)[system.message_id]["received_at_ms"])

    facade.prune_inbox(now_ms=received + 4 * CUTOFF)
    row = _rows(facade._database)[system.message_id]
    assert bytes(row["payload"]) == system.payload
    assert row["body_dropped_at_ms"] is None


def test_a_full_batch_of_interaction_rows_cannot_starve_a_system_row(facade) -> None:
    for index in range(SWEEP_BATCH + 6):
        _acked(facade, _message(f"m-{index:03d}"))
    system = _message(_dispatch_id())
    acked = _acked(facade, system)

    facade.prune_inbox(now_ms=acked + CUTOFF)
    facade.prune_inbox(now_ms=acked + CUTOFF)

    rows = _rows(facade._database)
    assert rows[system.message_id]["body_dropped_at_ms"] == acked + CUTOFF
    assert all(
        row["body_dropped_at_ms"] is None
        for message_id, row in rows.items()
        if message_id != system.message_id
    )


def test_system_notices_keep_their_own_prune(facade) -> None:
    notice = _message("n-1", intent="system", sender="system:hyprial:node-1")
    facade.receive_system_notice(notice)
    database = facade._database
    before = _rows(database, "system_notices")
    assert before

    facade.prune_inbox(now_ms=notice.created_at_ms + 10 ** 12 + CUTOFF)
    assert _rows(database, "system_notices") == before


def _read(app, message_id: str) -> dict:
    try:
        return {"result": app.handle(
            "message.read_body", {**SESSION, "messageId": message_id}
        )}
    except DaemonRequestError as error:
        return {"error": {"code": error.code, "message": str(error),
                          "data": error.data}}


def _surface(app, message_id: str) -> dict:
    return {
        "read": _read(app, message_id),
        "history": app.handle(
            "message.history", {**SESSION, "conversationId": "conversation-1"}
        ),
        "pending": app.handle("message.pending.list", dict(SESSION)),
    }


def test_reads_are_unchanged_before_the_cutoff_and_unavailable_after(app) -> None:
    reader = _reader(app)
    system = _message(_dispatch_id(), recipient=reader)
    assert app._inbox.receive(system).accepted
    app.handle("message.read_body", {**SESSION, "messageId": system.message_id})
    assert app._inbox.ack(reader, system.message_id).acknowledged
    app._inbox.receive(
        _message("m-pending", recipient=reader, expires_at_ms=10 ** 15)
    )
    acked = int(
        _rows(app._inbox._database)[system.message_id]["acknowledged_at_ms"]
    )
    before = _surface(app, system.message_id)
    assert "result" in before["read"]

    app._inbox.prune_inbox(now_ms=acked + CUTOFF - 1)
    assert _surface(app, system.message_id) == before

    app._inbox.prune_inbox(now_ms=acked + CUTOFF)
    after = _surface(app, system.message_id)
    missing = _read(app, "m-never-delivered")
    assert missing["error"]["code"] == "MESSAGE_BODY_UNAVAILABLE"
    assert after["read"] == {"error": {
        **missing["error"], "data": {"messageId": system.message_id},
    }}
    assert after["pending"] == before["pending"]
    assert after["history"] == {**before["history"], "entries": [
        {**entry, "message": ""}
        if entry["messageId"] == system.message_id else entry
        for entry in before["history"]["entries"]
    ]}
    assert any(
        entry["messageId"] == system.message_id and entry["message"]
        for entry in before["history"]["entries"]
    )


def _lookup(db, message: InboxMessage) -> dict:
    return lookup_send(
        db, sender=READER, conversation_id="conversation-1",
        target=message.recipient, text=f"body of {message.message_id}",
        idempotency_key="key-system",
    )


def test_a_settled_system_sender_copy_becomes_residue_after_the_cutoff(
    tmp_path: Path,
) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        target = "agent:op:node-1:pac-graph"
        system = replace(
            _message(_dispatch_id()), sender=READER, recipient=target,
            idempotency_key=f"key-system:{target}:0",
        )
        interaction = replace(
            _message("m-out"), sender=READER, recipient=target,
        )
        for message in (system, interaction):
            assert service.submit(message).accepted
        with closing(sqlite3.connect(service.database)) as db:
            looked_up = _lookup(db, system)
        assert looked_up["status"] == "accepted"
        service._db.execute("DELETE FROM outbox")
        service._db.commit()
        settle_at = 2_000_000_000_000
        service.prune_inbox(now_ms=settle_at)
        settled = _rows(service.database, "sender_copies")
        assert settled[system.message_id]["body_settled_at_ms"] == settle_at
        assert bytes(settled[system.message_id]["payload"]) == system.payload

        service.prune_inbox(now_ms=settle_at + CUTOFF - 1)
        assert _rows(service.database, "sender_copies") == settled

        service.prune_inbox(now_ms=settle_at + CUTOFF)
        after = _rows(service.database, "sender_copies")
        row = after[system.message_id]
        assert json.loads(row["payload"]) == {
            "replyTo": "m-0", "origin": {"kind": "agent"},
        }
        assert row["body_dropped_at_ms"] == settle_at + CUTOFF
        assert row["body_payload_sha256"] is None
        assert bytes(after["m-out"]["payload"]) == interaction.payload
        assert after["m-out"]["body_dropped_at_ms"] is None
        with closing(sqlite3.connect(service.database)) as db:
            assert _lookup(db, system) == looked_up
    finally:
        service.close()


def test_an_undecodable_row_is_set_aside_and_the_prune_keeps_going(facade) -> None:
    poison = _message(_dispatch_id())
    system = _message(_dispatch_id())
    acked = max(_acked(facade, item) for item in (poison, system))
    expiring = _message("m-expiring", expires_at_ms=acked + 1)
    assert facade.receive(expiring).accepted
    with closing(sqlite3.connect(facade._database)) as db, db:
        db.execute(
            "UPDATE inbox SET lifecycle = 'bogus' WHERE message_id = ?",
            (poison.message_id,),
        )

    facade.prune_inbox(now_ms=acked + CUTOFF)
    facade.prune_inbox(now_ms=acked + CUTOFF)

    rows = _rows(facade._database)
    assert bytes(rows[poison.message_id]["payload"]) == poison.payload
    assert rows[poison.message_id]["body_dropped_at_ms"] is None
    assert rows[poison.message_id]["body_class"] == "undecodable"
    assert rows[system.message_id]["body_dropped_at_ms"] == acked + CUTOFF
    assert expiring.message_id not in rows


def test_an_undecodable_sender_copy_is_set_aside(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        target = "agent:op:node-1:pac-graph"
        poison, due, system = (
            replace(_message(_dispatch_id()), sender=READER, recipient=target)
            for _ in range(3)
        )
        for message in (poison, due, system):
            assert service.submit(message).accepted
        service._db.execute("DELETE FROM outbox")
        service._db.execute(
            "UPDATE sender_copies SET lifecycle = 'bogus' WHERE message_id = ?",
            (poison.message_id,),
        )
        service._db.commit()
        settle_at = 2_000_000_000_000
        service.prune_inbox(now_ms=settle_at)
        service._db.execute(
            "UPDATE sender_copies SET lifecycle = 'bogus' WHERE message_id = ?",
            (due.message_id,),
        )
        service._db.commit()

        service.prune_inbox(now_ms=settle_at + CUTOFF)
        service.prune_inbox(now_ms=settle_at + CUTOFF)

        rows = _rows(service.database, "sender_copies")
        for message in (poison, due):
            assert bytes(rows[message.message_id]["payload"]) == message.payload
            assert rows[message.message_id]["body_class"] == "undecodable"
            assert rows[message.message_id]["body_dropped_at_ms"] is None
        assert rows[system.message_id]["body_dropped_at_ms"] == settle_at + CUTOFF
    finally:
        service.close()


def test_a_granted_system_sender_copy_leaves_the_residue_sweep(tmp_path: Path) -> None:
    """Plan slice S1: a recorded SYSTEM copy is dropped by its grant, not as residue."""

    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        target = "agent:op:node-1:pac-graph"
        system = replace(_message(_dispatch_id()), sender=READER, recipient=target)
        assert service.submit(system).accepted
        service._db.execute("DELETE FROM outbox")
        service._db.commit()
        settle_at = 2_000_000_000_000
        service.prune_inbox(now_ms=settle_at)
        stamped = _rows(service.database, "sender_copies")[system.message_id]
        assert (stamped["body_class"], stamped["body_settled_at_ms"]) == ("system", settle_at)

        digest = payload_sha256(system.payload)
        grant = BodyGrant(
            SOURCE_SENDER_COPY, _rowid(service, system.message_id), digest, "rec1 s p k"
        )
        assert service.grant_body_release(grant, retention_ms=60_000, now_ms=settle_at + 1)
        granted = _rows(service.database, "sender_copies")[system.message_id]
        assert granted["body_retention_ms"] == CUTOFF
        assert granted["body_payload_sha256"] == digest

        service.prune_inbox(now_ms=settle_at + CUTOFF - 1)
        assert _rows(service.database, "sender_copies")[system.message_id] == granted

        service.prune_inbox(now_ms=settle_at + CUTOFF)
        dropped = _rows(service.database, "sender_copies")[system.message_id]
        assert dropped["body_dropped_at_ms"] == settle_at + CUTOFF
        assert dropped["body_payload_sha256"] == digest
        assert dropped["body_ref"] == "rec1 s p k"
    finally:
        service.close()


def _rowid(service: InboxService, message_id: str) -> int:
    with closing(sqlite3.connect(service.database)) as db:
        return int(db.execute(
            "SELECT rowid FROM sender_copies WHERE message_id = ?", (message_id,)
        ).fetchone()[0])
