"""Legacy reads answer byte-identically after a granted body is released.

``message.read_body``, ``message.history``, ``message.pending.list`` and
``message.send.lookup`` are recorded before any drop, then the body is granted
and released and every read is repeated: with the resolver bound the answers
are equal, and ``message.send.lookup`` is equal even without a resolver
because it compares digests.  With no grant nothing is dropped at all.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace

from hyprial.daemon.impl.delivery import (
    MIN_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
    BodyRef,
    payload_sha256,
)
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.send_lookup import key_lookup_send, lookup_send
from hyprial.kernel import DaemonRequestError
from tests.daemon.delivery.message.test_message_pending_wait import (
    READER,
    SESSION,
    _message,
    _reader,
    app as app,
    owned_applications as owned_applications,
)

FAR = MIN_BODY_RETENTION_MS + 60_000


class _Resolver:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def resolve(self, ref: BodyRef) -> bytes | None:
        return self.bodies.get(ref.ref)


def _reads(app, message_id: str) -> dict:
    read = app.handle("message.read_body", {**SESSION, "messageId": message_id})
    history = app.handle(
        "message.history", {**SESSION, "conversationId": "conversation-1"}
    )
    pending = app.handle("message.pending.list", dict(SESSION))
    return {"read": read, "history": history, "pending": pending}


def _release_all(app, resolver: _Resolver) -> int:
    page = app._inbox.interaction_rows_after(SOURCE_INBOX, 0)
    for row in page.rows:
        ref = f"rec-{row.message.message_id}"
        resolver.bodies[ref] = row.message.payload
        assert app._inbox.grant_body_release(
            BodyGrant(SOURCE_INBOX, row.position, row.payload_sha256, ref,
                      retention_ms=MIN_BODY_RETENTION_MS)
        )
    app._inbox.prune_inbox(now_ms=time.time_ns() // 1_000_000 + FAR)
    with closing(sqlite3.connect(app._inbox._database)) as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM inbox WHERE body_dropped_at_ms IS NOT NULL"
        ).fetchone()[0])


def _seed(app) -> None:
    reader = _reader(app)
    for message_id in ("m-1", "m-2"):
        app._inbox.receive(_message(message_id, recipient=reader))
    app.handle("message.read_body", {**SESSION, "messageId": "m-1"})
    assert app._inbox.ack(reader, "m-1").acknowledged
    app._inbox.receive(_message("m-3", recipient=reader))


def test_reads_are_identical_after_release_with_the_resolver_bound(app) -> None:
    _seed(app)
    before = _reads(app, "m-1")
    resolver = _Resolver()
    app._inbox.bind_body_retention(resolver=resolver)

    assert _release_all(app, resolver) == 1
    assert _reads(app, "m-1") == before


def test_without_a_resolver_the_released_body_is_unavailable_not_wrong(app) -> None:
    _seed(app)
    before = _reads(app, "m-1")
    assert _release_all(app, _Resolver()) == 1

    try:
        app.handle("message.read_body", {**SESSION, "messageId": "m-1"})
    except DaemonRequestError as error:
        assert error.code == "MESSAGE_BODY_UNAVAILABLE"
    else:
        raise AssertionError("a residue body must not be served")
    pending = app.handle("message.pending.list", dict(SESSION))
    assert pending == before["pending"]
    history = app.handle(
        "message.history", {**SESSION, "conversationId": "conversation-1"}
    )
    assert [entry["messageId"] for entry in history["entries"]] == [
        entry["messageId"] for entry in before["history"]["entries"]
    ]


def test_without_a_grant_every_read_is_unchanged_by_the_sweep(app) -> None:
    _seed(app)
    before = _reads(app, "m-1")
    app._inbox.prune_inbox(now_ms=time.time_ns() // 1_000_000 + FAR)
    assert _reads(app, "m-1") == before


def _sender_copy_db(tmp_path):
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    rows = {}
    for text in ("hello", None):
        message_id = f"m-{text}"
        target = f"agent:op:node-1:{message_id}"
        message = replace(
            _message(message_id),
            payload=json.dumps({"message": text}).encode() if text else b'{"other":1}',
            sender=READER,
            recipient=target,
            idempotency_key=f"key-{message_id}:{target}:0",
        )
        assert service.submit(message).accepted
        rows[message_id] = message
    return service, rows


def _lookup(db, message_id: str, text: str) -> dict:
    return lookup_send(
        db, sender=READER, conversation_id="conversation-1",
        target=f"agent:op:node-1:{message_id}", text=text,
        idempotency_key=f"key-{message_id}",
    )


def test_send_lookup_after_drop_answers_from_the_text_digest(tmp_path) -> None:
    service, rows = _sender_copy_db(tmp_path)
    try:
        with closing(sqlite3.connect(service.database)) as db:
            before = {
                (message_id, text): _lookup(db, message_id, text)
                for message_id in rows
                for text in ("hello", "other text")
            }
        assert before[("m-hello", "hello")]["status"] == "accepted"
        assert before[("m-hello", "other text")]["reason"] == "request-mismatch"
        assert before[("m-None", "hello")]["reason"] == "invalid-record"

        with closing(sqlite3.connect(service.database)) as db:
            positions = dict(db.execute(
                "SELECT message_id, rowid FROM sender_copies"
            ).fetchall())
        for message_id, message in rows.items():
            assert service.grant_body_release(
                BodyGrant(SOURCE_SENDER_COPY, positions[message_id],
                          payload_sha256(message.payload), f"rec-{message_id}"),
                retention_ms=MIN_BODY_RETENTION_MS,
            )
        service._db.execute("DELETE FROM outbox")
        service._db.commit()
        now = time.time_ns() // 1_000_000
        service.prune_inbox(now_ms=now)
        service.prune_inbox(now_ms=now + FAR)

        with closing(sqlite3.connect(service.database)) as db:
            dropped = db.execute(
                "SELECT COUNT(*) FROM sender_copies WHERE body_dropped_at_ms IS NOT NULL"
            ).fetchone()[0]
            after = {key: _lookup(db, *key) for key in before}
        assert dropped == 2
        assert after == before
    finally:
        service.close()


def test_key_lookup_still_returns_accepted_after_body_release(tmp_path) -> None:
    """release/v0.5.5.4's key lookup answers from the digest once the body is gone.

    ``message.send.key_lookup`` (gui-live #2401) used to parse the sender
    copy's JSON body for ``message``; after the cell-model body release that
    body is the residue only, so an accepted send would read back as an
    invalid record.  It now shares ``lookup_send``'s digest rule.
    """

    service, rows = _sender_copy_db(tmp_path)
    try:
        with closing(sqlite3.connect(service.database)) as db:
            before = {
                message_id: key_lookup_send(
                    db, sender=READER, operation_key=f"key-{message_id}"
                )
                for message_id in rows
            }
        assert before["m-hello"] == {
            "status": "accepted", "commandId": "key-m-hello",
            "conversationId": "conversation-1", "messageId": "m-hello",
        }
        assert before["m-None"] == {"status": "unknown", "reason": "invalid-record"}

        with closing(sqlite3.connect(service.database)) as db:
            positions = dict(db.execute(
                "SELECT message_id, rowid FROM sender_copies"
            ).fetchall())
        for message_id, message in rows.items():
            assert service.grant_body_release(
                BodyGrant(SOURCE_SENDER_COPY, positions[message_id],
                          payload_sha256(message.payload), f"rec-{message_id}"),
                retention_ms=MIN_BODY_RETENTION_MS,
            )
        service._db.execute("DELETE FROM outbox")
        service._db.commit()
        now = time.time_ns() // 1_000_000
        service.prune_inbox(now_ms=now)
        service.prune_inbox(now_ms=now + FAR)

        with closing(sqlite3.connect(service.database)) as db:
            released = dict(db.execute(
                "SELECT message_id, payload FROM sender_copies"
                " WHERE body_dropped_at_ms IS NOT NULL"
            ).fetchall())
            after = {
                message_id: key_lookup_send(
                    db, sender=READER, operation_key=f"key-{message_id}"
                )
                for message_id in rows
            }
        assert set(released) == set(rows)
        assert all(b'"message"' not in bytes(payload) for payload in released.values())
        assert after == before
    finally:
        service.close()


def test_key_lookup_rejects_an_empty_text_after_body_release(tmp_path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    target = "agent:op:node-1:m-empty"
    message = replace(
        _message("m-empty"),
        payload=json.dumps({"message": ""}).encode(),
        sender=READER, recipient=target,
        idempotency_key=f"key-m-empty:{target}:0",
    )
    try:
        assert service.submit(message).accepted
        with closing(sqlite3.connect(service.database)) as db:
            (position,) = db.execute("SELECT rowid FROM sender_copies").fetchone()
        assert service.grant_body_release(
            BodyGrant(SOURCE_SENDER_COPY, position,
                      payload_sha256(message.payload), "rec-m-empty"),
            retention_ms=MIN_BODY_RETENTION_MS,
        )
        service._db.execute("DELETE FROM outbox")
        service._db.commit()
        now = time.time_ns() // 1_000_000
        service.prune_inbox(now_ms=now)
        service.prune_inbox(now_ms=now + FAR)
        with closing(sqlite3.connect(service.database)) as db:
            assert db.execute(
                "SELECT body_dropped_at_ms IS NOT NULL FROM sender_copies"
            ).fetchone()[0] == 1
            assert key_lookup_send(db, sender=READER, operation_key="key-m-empty") == {
                "status": "unknown", "reason": "invalid-record",
            }
    finally:
        service.close()
