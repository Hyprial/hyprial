"""Body release through the real inbox actor: grants, sweep, residue, rows.

A body is dropped only after a grant names its exact row and payload, the
row is acknowledged (or, for a sender copy, settled) and the retention has
passed.  Without a grant nothing changes, which keeps phase A behaviour
identical until the conversation recorder issues grants.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from hyprial.daemon.impl.delivery import (
    DEFAULT_BODY_RETENTION_MS,
    MIN_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
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
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX, MessageClass
from tests.support.actor_hosts import new_actor_host

READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"
HOUR = MIN_BODY_RETENTION_MS
DAY = 86_400_000


class _Events:
    def publish(self, event: object) -> None:
        pass


class _Resolver:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.asked: list[BodyRef] = []

    def resolve(self, ref: BodyRef) -> bytes | None:
        self.asked.append(ref)
        return self.bodies.get(ref.ref)


def _message(message_id: str, *, text: str | None = None, **fields) -> InboxMessage:
    body = {"message": text or f"body of {message_id}", "replyTo": "m-0",
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


def _row(facade: DeliveryCustodyFacade, message_id: str) -> sqlite3.Row:
    with closing(sqlite3.connect(facade._database)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()


def _later() -> int:
    return time.time_ns() // 1_000_000 + HOUR + 60_000


def _received_and_acked(
    facade: DeliveryCustodyFacade, message_id: str, *, now_ms: int | None = None,
    **fields,
):
    message = _message(message_id, **fields)
    assert facade.receive(message, now_ms=now_ms).accepted
    assert facade.ack(READER, message_id).acknowledged
    return message


def _grant(facade, row, *, ref="rec-1", retention_ms=HOUR) -> BodyGrant:
    return BodyGrant(row.source, row.position, row.payload_sha256, ref,
                     retention_ms=retention_ms)


def test_without_a_grant_nothing_is_ever_dropped(facade) -> None:
    message = _received_and_acked(facade, "m-1")
    facade.prune_inbox(now_ms=_later() + 400 * 86_400_000)

    row = _row(facade, "m-1")
    assert bytes(row["payload"]) == message.payload
    assert row["body_dropped_at_ms"] is None
    assert row["body_payload_sha256"] is None


def test_a_granted_acked_body_becomes_residue_after_retention(facade) -> None:
    message = _received_and_acked(facade, "m-1")
    page = facade.interaction_rows_after(SOURCE_INBOX, 0)
    (item,) = page.rows
    assert item.message.message_id == "m-1"
    assert item.payload_sha256 == payload_sha256(message.payload)
    assert page.cursor == item.position
    assert facade.interaction_row_at(SOURCE_INBOX, item.position) == item

    assert facade.grant_body_release(_grant(facade, item)) is True
    granted = _row(facade, "m-1")
    assert granted["body_ref"] == "rec-1"
    assert granted["body_class"] == "interaction"
    assert granted["body_payload_sha256"] == item.payload_sha256

    acked_at = int(granted["acknowledged_at_ms"])
    facade.prune_inbox(now_ms=acked_at + HOUR - 1)
    assert bytes(_row(facade, "m-1")["payload"]) == message.payload

    facade.prune_inbox(now_ms=acked_at + HOUR)
    dropped = _row(facade, "m-1")
    assert json.loads(dropped["payload"]) == {
        "replyTo": "m-0", "origin": {"kind": "agent"},
    }
    assert dropped["body_dropped_at_ms"] == acked_at + HOUR
    assert dropped["body_text_sha256"] == text_sha256(message.payload)
    assert dropped["body_payload_sha256"] == item.payload_sha256
    assert facade.interaction_rows_after(SOURCE_INBOX, 0).rows == ()


def test_an_unacked_granted_row_keeps_its_body(facade) -> None:
    message = _message("m-1")
    assert facade.receive(message).accepted
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    assert facade.grant_body_release(_grant(facade, item))

    facade.prune_inbox(now_ms=time.time_ns() // 1_000_000 + 1)
    assert bytes(_row(facade, "m-1")["payload"]) == message.payload


def test_grants_are_refused_for_a_wrong_key_or_a_missing_row(facade) -> None:
    _received_and_acked(facade, "m-1")
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows

    assert not facade.grant_body_release(
        BodyGrant(SOURCE_INBOX, item.position, "0" * 64, "rec")
    )
    assert not facade.grant_body_release(
        BodyGrant(SOURCE_INBOX, item.position + 10, item.payload_sha256, "rec")
    )
    facade.prune_inbox(now_ms=_later())
    assert _row(facade, "m-1")["body_dropped_at_ms"] is None


def test_a_system_row_is_fed_with_its_class_and_granted_on_the_code_default(
    facade,
) -> None:
    """Plan slice S1: SYSTEM rows are recorded, so they are fed and granted.

    The grant moves the row off the residue clock, and Q9 still holds: a
    SYSTEM grant takes the code default whatever retention it names.
    """

    system_id = f"{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}"
    message = _received_and_acked(facade, system_id)
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    assert item.message.message_id == system_id
    assert item.message_class is MessageClass.SYSTEM
    assert facade.interaction_row_at(SOURCE_INBOX, item.position) == item

    assert facade.grant_body_release(_grant(facade, item, retention_ms=HOUR))
    granted = _row(facade, system_id)
    assert granted["body_class"] == "system"
    assert granted["body_retention_ms"] == DEFAULT_BODY_RETENTION_MS
    acked_at = int(granted["acknowledged_at_ms"])

    facade.prune_inbox(now_ms=acked_at + DEFAULT_BODY_RETENTION_MS - 1)
    assert bytes(_row(facade, system_id)["payload"]) == message.payload

    facade.prune_inbox(now_ms=acked_at + DEFAULT_BODY_RETENTION_MS)
    dropped = _row(facade, system_id)
    assert dropped["body_dropped_at_ms"] == acked_at + DEFAULT_BODY_RETENTION_MS
    assert dropped["body_payload_sha256"] == item.payload_sha256
    assert dropped["body_ref"] == "rec-1"


def test_a_grant_does_not_follow_a_reused_message_id(facade) -> None:
    _received_and_acked(facade, "m-1")
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    assert facade.grant_body_release(_grant(facade, item))
    reused = _received_and_acked(
        facade, "m-1", text="the reused id's new body", now_ms=_later() + 2 * DAY,
    )

    facade.prune_inbox(now_ms=_later() + 2 * DAY)
    row = _row(facade, "m-1")
    assert int(row["arrival_id"]) != item.position
    assert bytes(row["payload"]) == reused.payload
    assert row["body_dropped_at_ms"] is None


def test_retention_comes_from_the_bound_source_and_is_clamped(facade) -> None:
    _received_and_acked(facade, "m-1")
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    facade.bind_body_retention(retention_ms=lambda _subject: 1)
    assert facade.grant_body_release(_grant(facade, item, retention_ms=None))
    assert _row(facade, "m-1")["body_retention_ms"] == HOUR

    facade.bind_body_retention(retention_ms=lambda _subject: 2 * HOUR)
    assert facade.grant_body_release(_grant(facade, item, retention_ms=None))
    assert _row(facade, "m-1")["body_retention_ms"] == 2 * HOUR

    facade.bind_body_retention(retention_ms=None)
    assert facade.grant_body_release(_grant(facade, item, retention_ms=None))
    assert _row(facade, "m-1")["body_retention_ms"] == 604_800_000


def test_a_retention_source_of_the_wrong_arity_is_refused_at_bind(facade) -> None:
    facade.bind_body_retention(retention_ms=lambda _subject: 2 * HOUR)
    with pytest.raises(TypeError, match="retention source"):
        facade.bind_body_retention(retention_ms=lambda: HOUR)
    with pytest.raises(TypeError, match="retention source"):
        facade.bind_body_retention(retention_ms=lambda _subject, _extra: HOUR)
    with pytest.raises(TypeError, match="retention source"):
        facade.bind_body_retention(retention_ms=2 * HOUR)
    _received_and_acked(facade, "m-1")
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    assert facade.grant_body_release(_grant(facade, item, retention_ms=None))
    assert _row(facade, "m-1")["body_retention_ms"] == 2 * HOUR


def test_retained_body_hydrates_only_with_a_matching_resolver(facade) -> None:
    message = _received_and_acked(facade, "m-1")
    (item,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    assert facade.retained_body("m-1") == facade.harness_failure_original("m-1")
    assert facade.grant_body_release(_grant(facade, item))
    facade.prune_inbox(now_ms=_later())

    assert facade.retained_body("m-1") is None
    resolver = _Resolver()
    facade.bind_body_retention(resolver=resolver)
    assert facade.retained_body("m-1") is None
    resolver.bodies["rec-1"] = b'{"message":"tampered"}'
    assert facade.retained_body("m-1") is None
    resolver.bodies["rec-1"] = message.payload
    hydrated = facade.retained_body("m-1")
    assert hydrated is not None and hydrated.payload == message.payload
    assert resolver.asked[-1] == BodyRef(
        SOURCE_INBOX, item.position, "m-1", item.payload_sha256, "rec-1",
    )


def test_a_settled_sender_copy_is_released_after_retention(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        message = _message("m-out", sender=READER, recipient=DRIVER)
        assert service.submit(message).accepted
        with closing(sqlite3.connect(service.database)) as db:
            db.row_factory = sqlite3.Row
            copy = db.execute(
                "SELECT rowid, payload FROM sender_copies WHERE message_id = 'm-out'"
            ).fetchone()
        grant = BodyGrant(SOURCE_SENDER_COPY, int(copy["rowid"]),
                          payload_sha256(bytes(copy["payload"])), "rec-out",
                          retention_ms=HOUR)
        assert service.grant_body_release(grant, retention_ms=HOUR)
        while service.outbox_count():
            service._db.execute("DELETE FROM outbox")
            service._db.commit()
        settle_at = time.time_ns() // 1_000_000
        service.prune_inbox(now_ms=settle_at)
        service.prune_inbox(now_ms=settle_at + HOUR - 1)
        with closing(sqlite3.connect(service.database)) as db:
            db.row_factory = sqlite3.Row
            kept = db.execute("SELECT * FROM sender_copies").fetchone()
        assert kept["body_settled_at_ms"] == settle_at
        assert bytes(kept["payload"]) == message.payload
        service.prune_inbox(now_ms=settle_at + HOUR)
        with closing(sqlite3.connect(service.database)) as db:
            db.row_factory = sqlite3.Row
            dropped = db.execute("SELECT * FROM sender_copies").fetchone()
        assert dropped["body_dropped_at_ms"] == settle_at + HOUR
        assert json.loads(dropped["payload"]) == {
            "replyTo": "m-0", "origin": {"kind": "agent"},
        }
    finally:
        service.close()


def test_the_schema_migration_is_idempotent_and_adds_the_grant_ledger(tmp_path) -> None:
    path = tmp_path / "inbox.sqlite3"
    InboxService(path, MemoryDeliveryTransport()).close()
    InboxService(path, MemoryDeliveryTransport()).close()
    with closing(sqlite3.connect(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        inbox = {row[1] for row in db.execute("PRAGMA table_info(inbox)")}
        copies = {row[1] for row in db.execute("PRAGMA table_info(sender_copies)")}
        dlq = {row[1] for row in db.execute("PRAGMA table_info(dlq)")}
    body = {"body_class", "body_ref", "body_dropped_at_ms", "body_payload_sha256",
            "body_text_sha256", "body_retention_ms"}
    assert "body_release" in tables
    assert body <= inbox
    assert body | {"body_settled_at_ms"} <= copies
    assert not {column for column in dlq if column.startswith("body_")}
