"""Inbox TTL: ``expires_at_ms`` stamped at receive, sweep on the daemon tick.

The defect these pin: the inbox had no time-based purge, so messages nobody
ever consumed -- a recipient URI mismatch after a node rename, a
decommissioned actor, a consumer that stopped polling -- accumulated forever.
The fix aligns the inbox with the outbox/custody hold contract
(``DEFAULT_HOLD_TTL_MS``): every received row gets an
``expires_at_ms`` counted from THIS node's clock (``received_at_ms``, section
6 of docs/notes/design-delivery-pull-model.md), and the daemon's periodic
reconcile evicts unconsumed rows past the deadline.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    HoldPolicy,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)

TTL_MS = DEFAULT_HOLD_TTL_MS


def _message(
    message_id: str = "msg-1",
    *,
    sender: str = "agent:alice:mac:sender",
    recipient: str = "agent:bob:pc:worker",
    created_at_ms: int = 0,
    intent: str = "request",
    origin_node: str | None = None,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="dm:alice:bob",
        sender=sender,
        recipient=recipient,
        payload=b"opaque-frame",
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=None,
        created_at_ms=created_at_ms,
        origin_node=origin_node,
    )


@pytest.fixture
def service(tmp_path: Path) -> InboxService:
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    yield inbox
    inbox.close()


# --- stamping -----------------------------------------------------------------


def test_receive_stamps_expires_at_ms_from_local_clock(service: InboxService) -> None:
    service.receive(_message(), now_ms=5_000)

    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT received_at_ms, expires_at_ms FROM inbox WHERE message_id = 'msg-1'"
    ).fetchone()
    assert row["received_at_ms"] == 5_000
    # The deadline is this node's receive clock plus the hold TTL -- never
    # derived from the sender's ``created_at_ms`` (foreign clock, section 6).
    assert row["expires_at_ms"] == 5_000 + TTL_MS


def test_receive_stamps_expires_at_ms_with_configured_hold_policy(
    tmp_path: Path,
) -> None:
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        hold_policy=HoldPolicy(ttl_ms=60_000),
    )
    try:
        inbox.receive(_message(), now_ms=1_000)
        row = inbox._db.execute(  # type: ignore[attr-defined]
            "SELECT expires_at_ms FROM inbox WHERE message_id = 'msg-1'"
        ).fetchone()
        assert row["expires_at_ms"] == 1_000 + 60_000
    finally:
        inbox.close()


def test_receive_honors_later_per_message_expiry_without_changing_ordinary_ttl(
    service: InboxService,
) -> None:
    service.receive(_message("ordinary"), now_ms=0)
    service.receive(
        replace(_message("fixed"), expires_at_ms=2 * TTL_MS), now_ms=0
    )

    pruned = service.prune_inbox(now_ms=TTL_MS + 1)

    assert [item.message_id for item in pruned] == ["ordinary"]
    assert [item.message_id for item in service.pending_all()] == ["fixed"]
    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT message_expires_at_ms, expires_at_ms FROM inbox "
        "WHERE message_id='fixed'"
    ).fetchone()
    assert tuple(row) == (2 * TTL_MS, 2 * TTL_MS)


def test_refresh_hold_preserves_fixed_message_expiry(service: InboxService) -> None:
    service.receive(
        replace(_message("fixed"), expires_at_ms=2 * TTL_MS), now_ms=0
    )

    assert service.refresh_hold("fixed", now_ms=TTL_MS)
    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT expires_at_ms FROM inbox WHERE message_id='fixed'"
    ).fetchone()
    assert row["expires_at_ms"] == 2 * TTL_MS


def test_outbox_and_custody_do_not_cap_or_lose_producer_expiry(
    service: InboxService,
) -> None:
    deadline = 4 * TTL_MS
    outbound = replace(_message("outbound"), expires_at_ms=deadline)
    assert service.submit(outbound, now_ms=0).accepted
    outbox = service.outbox_item("outbound")
    assert outbox.message.expires_at_ms == deadline
    assert outbox.expires_at_ms == deadline

    custody = replace(_message("custody"), expires_at_ms=deadline)
    assert service.accept_custody(custody, mailbox_node="mailbox", now_ms=0).acknowledged
    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT message_expires_at_ms, expires_at_ms FROM custody "
        "WHERE message_id='custody'"
    ).fetchone()
    assert tuple(row) == (deadline, deadline)


# --- sweep --------------------------------------------------------------------


def test_expired_unconsumed_rows_are_swept(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)  # expires at TTL_MS

    pruned = service.prune_inbox(now_ms=TTL_MS + 1)

    assert len(pruned) == 1
    assert pruned[0].message_id == "msg-1"
    assert pruned[0].reason == "TTL_EXPIRED"
    assert service.pending_messages("agent:bob:pc:worker") == ()


def test_not_yet_expired_rows_are_retained(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)  # expires at TTL_MS

    pruned = service.prune_inbox(now_ms=TTL_MS - 1)

    assert pruned == ()
    assert [m.message_id for m in service.pending_all()] == ["msg-1"]


def test_pending_messages_hides_expired_rows_before_the_sweep(
    service: InboxService,
) -> None:
    message = _message()
    service.receive(message, now_ms=0)

    assert service.pending_messages(message.recipient, now_ms=TTL_MS) == ()


def test_dispatchable_messages_hides_expired_rows_before_the_sweep(
    service: InboxService,
) -> None:
    message = _message()
    service.receive(message, now_ms=0)

    assert service.dispatchable_messages(message.recipient, now_ms=TTL_MS) == ()


def test_fetch_pending_does_not_fetch_expired_rows_before_the_sweep(
    service: InboxService,
) -> None:
    message = _message()
    service.receive(message, now_ms=0)

    assert service.fetch_pending(message.recipient, now_ms=TTL_MS) == ()
    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT fetched_at_ms FROM inbox WHERE message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert row is not None and row["fetched_at_ms"] is None


def test_row_at_exact_deadline_is_swept(service: InboxService) -> None:
    """The sweep predicate is ``expires_at_ms <= now``: a row whose deadline
    equals the sweep instant exactly is expired at that instant, not one
    millisecond away.  This pins the fine-grained ``==`` boundary that the
    strictly-before / strictly-after tests above do not cover.
    """

    service.receive(_message(), now_ms=0)  # expires_at_ms == TTL_MS exactly

    pruned = service.prune_inbox(now_ms=TTL_MS)

    assert [item.message_id for item in pruned] == ["msg-1"]
    assert service.pending_messages("agent:bob:pc:worker") == ()


def test_sweep_is_idempotent(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)
    service.prune_inbox(now_ms=TTL_MS + 1)

    assert service.prune_inbox(now_ms=TTL_MS + 1) == ()


def test_acknowledged_rows_are_never_swept(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)
    result = service.ack("agent:bob:pc:worker", "msg-1")
    assert result.acknowledged

    # Long past the TTL: the retention contract keeps consumed rows until
    # their message id is reused, so the sweep must leave them alone.
    assert service.prune_inbox(now_ms=TTL_MS * 100) == ()
    assert service.is_acknowledged("msg-1")


def test_null_expires_at_ms_rows_are_never_swept(service: InboxService) -> None:
    # A legacy row the backfill never touched: NULL deadline means "unknown",
    # and guessing an expiry for it would be worse than keeping it.
    service._db.execute(  # type: ignore[attr-defined]
        """INSERT INTO inbox (
               message_id, conversation_id, sender, recipient, payload, intent,
               lifecycle, idempotency_key, created_at_ms, received_at_ms,
               expires_at_ms, consumed
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)""",
        (
            "legacy-msg",
            "dm:alice:bob",
            "agent:alice:mac:sender",
            "agent:bob:pc:worker",
            b"legacy",
            "request",
            DeliveryLifecycle.DURABLE_SERVICE.value,
            None,
            0,
            0,
        ),
    )
    service._db.commit()  # type: ignore[attr-defined]

    assert service.prune_inbox(now_ms=TTL_MS * 100) == ()
    assert service.pending_count("agent:bob:pc:worker") == 1


def test_sweep_mixed_consumed_and_unconsumed(service: InboxService) -> None:
    service.receive(_message("kept", recipient="agent:carol:pc:consumer"), now_ms=0)
    service.ack("agent:carol:pc:consumer", "kept")
    service.receive(_message("stale", recipient="agent:carol:pc:consumer"), now_ms=0)

    pruned = service.prune_inbox(now_ms=TTL_MS + 1)

    assert [item.message_id for item in pruned] == ["stale"]
    assert service.is_acknowledged("kept")


def test_prune_reports_every_evicted_row(service: InboxService) -> None:
    service.receive(_message("first"), now_ms=0)
    service.receive(_message("second"), now_ms=1_000)

    pruned = service.prune_inbox(now_ms=TTL_MS + 1_001)

    assert [item.message_id for item in pruned] == ["first", "second"]
    assert all(item.reason == "TTL_EXPIRED" for item in pruned)
    assert [item.received_at_ms for item in pruned] == [0, 1_000]


def test_prune_notifies_original_sender_once_without_message_body(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,
        node_id="recipient-node",
    )
    message = _message(
        "msg-unread",
        created_at_ms=1_000,
        origin_node="sender-origin-node",
    )
    try:
        inbox.receive(message, now_ms=5_000)

        pruned = inbox.prune_inbox(now_ms=5_000 + TTL_MS)
        assert [item.message_id for item in pruned] == [message.message_id]
        assert inbox.prune_inbox(now_ms=5_000 + TTL_MS) == ()

        [(target_node, notice)] = transport.notices
        assert target_node == "sender-origin-node"
        assert notice.recipient == message.sender
        assert notice.sender == "system:hyprial:recipient-node"
        assert notice.intent == "system"
        assert notice.lifecycle is DeliveryLifecycle.ONLINE_ONLY
        assert notice.conversation_id == message.conversation_id
        body = json.loads(notice.payload)
        assert body == {
            "message": (
                "The message you sent to agent:bob:pc:worker at "
                f"1970-01-01T00:00:01.000Z expired unread after {TTL_MS} ms. "
                "Message ID: msg-unread. Conversation ID: dm:alice:bob."
            ),
            "originalMessageId": "msg-unread",
            "reason": "TTL_EXPIRED",
            "systemNotice": True,
        }
        assert message.payload not in notice.payload
    finally:
        inbox.close()


def test_prune_notice_falls_back_to_canonical_sender_node(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    inbox = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        inbox.receive(_message("msg-fallback"), now_ms=0)

        inbox.prune_inbox(now_ms=TTL_MS)

        [(target_node, _notice)] = transport.notices
        assert target_node == "mac"
    finally:
        inbox.close()


@pytest.mark.parametrize(
    ("message", "fetch_first"),
    [
        (_message("msg-fetched"), True),
        (_message("msg-system", intent="system"), False),
        (
            _message(
                "msg-system-reply",
                sender="system:hyprial:sender-node",
                intent="reply",
            ),
            False,
        ),
        (_message("workflow-request-delivery"), False),
    ],
)
def test_prune_notice_exclusions(
    tmp_path: Path,
    message: InboxMessage,
    fetch_first: bool,
) -> None:
    transport = MemoryDeliveryTransport()
    inbox = InboxService(tmp_path / message.message_id / "inbox.sqlite3", transport)
    try:
        inbox.receive(message, now_ms=0)
        if fetch_first:
            assert inbox.fetch_pending(message.recipient, now_ms=1) == (message,)

        pruned = inbox.prune_inbox(now_ms=TTL_MS)

        assert [item.message_id for item in pruned] == [message.message_id]
        assert transport.notices == []
    finally:
        inbox.close()


class _OfflineNoticeTransport(MemoryDeliveryTransport):
    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        self.notices.append((node, message))
        return False


def test_offline_sender_notice_attempt_is_logged_once(tmp_path: Path) -> None:
    transport = _OfflineNoticeTransport()
    inbox = InboxService(
        tmp_path / "state" / "inbox.sqlite3",
        transport,
        node_id="recipient-node",
    )
    message = _message("msg-offline", origin_node="offline-sender-node")
    try:
        inbox.receive(message, now_ms=0)

        pruned = inbox.prune_inbox(now_ms=TTL_MS)

        assert [item.message_id for item in pruned] == [message.message_id]
        assert len(transport.notices) == 1
        records = [
            json.loads(line)
            for line in (tmp_path / "state" / "logs" / "daemon.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        attempts = [
            record
            for record in records
            if record["event"] == "inbox.expired_sender_notice"
        ]
        assert len(attempts) == 1
        assert attempts[0]["level"] == "info"
        assert attempts[0]["messageId"] == message.message_id
        assert attempts[0]["targetNode"] == "offline-sender-node"
        assert attempts[0]["delivered"] is False
    finally:
        inbox.close()


# --- terminally settled rows (zombie eviction) --------------------------------
#
# A row whose harness delivery failed terminally used to be EXEMPT from the
# sweep (NOT EXISTS terminal = 1), so it accumulated forever even though every
# consumer path already ignored it.  The sweep now evicts it once past its
# hold deadline and reports the eviction as TERMINAL_SETTLED; the settlement
# row stays as the durable tombstone.


def test_prune_evicts_terminally_settled_rows(service: InboxService) -> None:
    # The zombie shape observed in production: a permanent failure minted the
    # tombstone long ago and the dead row's hold deadline has long passed.
    service.receive(_message("zombie"), now_ms=0)  # expires at TTL_MS
    settlement = service.settle_harness_failure(
        "agent:bob:pc:worker",
        "zombie",
        "PROVIDER_USAGE_LIMIT",
        permanent=True,
        max_attempts=3,
        backoff_ms=(1_000,),
        now_ms=10,
    )
    assert settlement.terminal
    # The same tombstone still inside its hold window keeps its row: the
    # deadline, not the tombstone, decides when the row goes.
    service.receive(_message("recent"), now_ms=TTL_MS + 1)
    recent = service.settle_harness_failure(
        "agent:bob:pc:worker",
        "recent",
        "PROVIDER_USAGE_LIMIT",
        permanent=True,
        max_attempts=3,
        backoff_ms=(1_000,),
        now_ms=TTL_MS + 2,
    )
    assert recent.terminal

    pruned = service.prune_inbox(now_ms=TTL_MS + 3)

    assert [item.message_id for item in pruned] == ["zombie"]
    assert pruned[0].reason == "TERMINAL_SETTLED"
    # Only the dead inbox row goes: the settlement tombstone and the DLQ
    # record stay, so the failure remains auditable after the eviction.
    assert service.harness_failure_settlement("zombie") == settlement
    assert service.dlq_count() == 2
    retained = service._db.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS n FROM inbox WHERE message_id = 'recent'"
    ).fetchone()
    assert retained["n"] == 1

    # Once its own deadline passes, the retained tombstoned row goes the same
    # way -- the rule itself is the migration; no backfill script is needed.
    pruned_later = service.prune_inbox(now_ms=2 * TTL_MS + 2)
    assert [item.message_id for item in pruned_later] == ["recent"]
    assert pruned_later[0].reason == "TERMINAL_SETTLED"
    assert service.harness_failure_settlement("recent") == recent


def test_prune_keeps_retrying_and_acknowledged_rows(service: InboxService) -> None:
    # Mid-retry is kept, but the mechanism is the deadline itself, not a
    # sweep-side exemption: settle_harness_failure pushed the hold out to
    # next_attempt_ms + TTL, so the row simply is not expired yet.  (The old
    # terminal-only exemption is what created the zombies; do not re-add a
    # settlement-side exemption in either polarity.)
    service.receive(_message("retrying"), now_ms=0)  # expires at TTL_MS
    settlement = service.settle_harness_failure(
        "agent:bob:pc:worker",
        "retrying",
        "HARNESS_TRANSIENT_FAILURE",
        permanent=False,
        max_attempts=3,
        backoff_ms=(5_000,),
        now_ms=10,
    )
    assert not settlement.terminal
    assert settlement.next_attempt_ms == 5_010
    # Acknowledged rows keep the retention contract: never swept.
    service.receive(_message("acked"), now_ms=0)
    assert service.ack("agent:bob:pc:worker", "acked").acknowledged

    # Past the original TTL and past the backoff instant, one millisecond
    # before the extended deadline.
    pruned = service.prune_inbox(now_ms=TTL_MS + 5_009)

    assert pruned == ()
    assert [
        message.message_id
        for message in service.dispatchable_messages(
            "agent:bob:pc:worker", now_ms=TTL_MS + 5_009
        )
    ] == ["retrying"]
    assert service.is_acknowledged("acked")


def test_prune_sweeps_abandoned_retry_as_ttl_expired(service: InboxService) -> None:
    # If the retry never lands, the extended deadline still passes and the
    # sweep reaps the row as INBOX_TTL_EXPIRED -- the settlement remains, so
    # this does not create a second zombie class.
    service.receive(_message("abandoned"), now_ms=0)
    settlement = service.settle_harness_failure(
        "agent:bob:pc:worker",
        "abandoned",
        "HARNESS_TRANSIENT_FAILURE",
        permanent=False,
        max_attempts=3,
        backoff_ms=(5_000,),
        now_ms=10,
    )
    assert not settlement.terminal

    pruned = service.prune_inbox(now_ms=TTL_MS + 5_010)  # extended deadline

    assert [item.message_id for item in pruned] == ["abandoned"]
    assert pruned[0].reason == "TTL_EXPIRED"
    assert service.harness_failure_settlement("abandoned") == settlement


# --- refresh_hold (#276: TTL never refreshed on dispatch/activity) -----------


def test_refresh_hold_pushes_deadline_out_from_now(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)  # expires_at_ms == TTL_MS

    refreshed = service.refresh_hold("msg-1", now_ms=TTL_MS - 1)  # still in-flight

    assert refreshed is True
    row = service._db.execute(  # type: ignore[attr-defined]
        "SELECT expires_at_ms FROM inbox WHERE message_id = 'msg-1'"
    ).fetchone()
    # A fresh full-TTL window from the refresh instant, not from receipt.
    assert row["expires_at_ms"] == (TTL_MS - 1) + TTL_MS


def test_refresh_hold_prevents_prune_within_the_new_window(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)  # would expire at TTL_MS

    service.refresh_hold("msg-1", now_ms=TTL_MS - 1)  # in-flight just before the old deadline
    # Past the ORIGINAL deadline but well inside the refreshed one.
    pruned = service.prune_inbox(now_ms=TTL_MS + 1)

    assert pruned == ()
    assert service.pending_count("agent:bob:pc:worker") == 1


def test_refresh_hold_is_noop_on_consumed_row(service: InboxService) -> None:
    service.receive(_message(), now_ms=0)
    service.ack("agent:bob:pc:worker", "msg-1")

    assert service.refresh_hold("msg-1", now_ms=1_000) is False


def test_refresh_hold_is_noop_on_unknown_row(service: InboxService) -> None:
    assert service.refresh_hold("no-such-message", now_ms=1_000) is False


def test_refresh_hold_does_not_make_ttl_unbounded(service: InboxService) -> None:
    """The boundary the fix must not blur: a row that stops getting
    refreshed (the worker that held it went silent/died) still expires —
    exactly ``durable_ttl_ms`` after its LAST refresh, not never."""
    service.receive(_message(), now_ms=0)
    service.refresh_hold("msg-1", now_ms=1_000)  # last sign of life at t=1000

    # Nothing refreshes it again; real time keeps passing.
    assert service.prune_inbox(now_ms=1_000 + TTL_MS - 1) == ()  # not yet
    pruned = service.prune_inbox(now_ms=1_000 + TTL_MS)  # exactly at the new deadline

    assert [item.message_id for item in pruned] == ["msg-1"]
    assert pruned[0].reason == "TTL_EXPIRED"
    assert service.pending_messages("agent:bob:pc:worker") == ()


# --- migration ----------------------------------------------------------------


def _legacy_inbox_table(connection: sqlite3.Connection) -> None:
    """The pre-TTL inbox schema, exactly as it shipped before this fix."""

    connection.executescript(
        """
        CREATE TABLE inbox (
            arrival_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            conversation_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            payload BLOB NOT NULL,
            intent TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            idempotency_key TEXT,
            created_at_ms INTEGER NOT NULL,
            received_at_ms INTEGER NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0,
            acknowledged_at_ms INTEGER,
            fetched_at_ms INTEGER,
            origin_node TEXT
        );
        """
    )


def _insert_legacy_row(
    connection: sqlite3.Connection, message_id: str, received_at_ms: int
) -> None:
    connection.execute(
        """INSERT INTO inbox (
               message_id, conversation_id, sender, recipient, payload, intent,
               lifecycle, idempotency_key, created_at_ms, received_at_ms
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            "dm:alice:bob",
            "agent:alice:mac:sender",
            "agent:bob:pc:worker",
            b"legacy",
            "request",
            DeliveryLifecycle.DURABLE_SERVICE.value,
            None,
            received_at_ms - 100,  # foreign sender clock, must not be used
            received_at_ms,
        ),
    )
    connection.commit()


def test_legacy_schema_backfills_expires_at_ms_and_sweeps(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    connection = sqlite3.connect(database)
    _legacy_inbox_table(connection)
    _insert_legacy_row(connection, "orphan-1", received_at_ms=0)
    _insert_legacy_row(connection, "fresh-1", received_at_ms=5_000)
    connection.close()

    # Opening the service migrates the table (ALTER + one-time backfill).
    service = InboxService(database, MemoryDeliveryTransport(), node_id="node-bob")
    try:
        rows = {
            str(row["message_id"]): int(row["expires_at_ms"])
            for row in service._db.execute(  # type: ignore[attr-defined]
                "SELECT message_id, expires_at_ms FROM inbox"
            ).fetchall()
        }
        # Backfilled from this node's own clock (received_at_ms), never from
        # the sender's created_at_ms.
        assert rows == {"orphan-1": TTL_MS, "fresh-1": 5_000 + TTL_MS}

        # The old orphan is now swept on the regular cadence; the fresh row
        # survives.
        pruned = service.prune_inbox(now_ms=TTL_MS + 1)
        assert [item.message_id for item in pruned] == ["orphan-1"]
        assert [m.message_id for m in service.pending_all()] == ["fresh-1"]
    finally:
        service.close()


def test_legacy_schema_adds_nullable_producer_expiry_without_promoting_ttl(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    seed = InboxService(database, MemoryDeliveryTransport(), node_id="node-bob")
    seed.receive(_message("legacy"), now_ms=4_000)
    seed.close()

    connection = sqlite3.connect(database)
    for table in ("outbox", "inbox", "custody"):
        connection.execute(f"ALTER TABLE {table} DROP COLUMN message_expires_at_ms")
    connection.commit()
    connection.close()

    service = InboxService(database, MemoryDeliveryTransport(), node_id="node-bob")
    try:
        row = service._db.execute(  # type: ignore[attr-defined]
            "SELECT message_expires_at_ms, expires_at_ms FROM inbox "
            "WHERE message_id='legacy'"
        ).fetchone()
        assert row["message_expires_at_ms"] is None
        assert row["expires_at_ms"] == 4_000 + TTL_MS
        for table in ("outbox", "inbox", "custody"):
            columns = {
                item["name"]
                for item in service._db.execute(  # type: ignore[attr-defined]
                    f"PRAGMA table_info({table})"
                )
            }
            assert "message_expires_at_ms" in columns
    finally:
        service.close()


def test_backfill_runs_once_and_does_not_rewrite_deadlines(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    connection = sqlite3.connect(database)
    _legacy_inbox_table(connection)
    _insert_legacy_row(connection, "orphan-1", received_at_ms=0)
    connection.close()

    first = InboxService(database, MemoryDeliveryTransport(), node_id="node-bob")
    first.close()

    # A second open must not shift the deadline (backfill is NULL-only).
    second = InboxService(database, MemoryDeliveryTransport(), node_id="node-bob")
    try:
        row = second._db.execute(  # type: ignore[attr-defined]
            "SELECT expires_at_ms FROM inbox WHERE message_id = 'orphan-1'"
        ).fetchone()
        assert row["expires_at_ms"] == TTL_MS
    finally:
        second.close()
