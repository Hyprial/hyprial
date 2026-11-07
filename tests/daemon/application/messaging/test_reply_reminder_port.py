"""WP7 P1-P3: the inbox-backed reminder port and notice summary persistence.

Review R1: the system notice's ``summary`` must survive the SQLite round trip,
including old-database migration, reopen, the single-process read and the
read-only authority projection.  Review R2: a failed write must not suppress a
later retry of the same completed event.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService

NODE = "wp7-node"
A_URI = "agent:wp7-owner:wp7-node:worker"
X_URI = "agent:wp7-owner:wp7-node:peer"
TEXT = "1 条请求未回复：r1；回复用 reply，无需回复用 ack"


class LoopbackTransport:
    def __init__(self) -> None:
        self.service: InboxService | None = None

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _inbox(tmp_path: Path, name: str = "inbox.sqlite3") -> InboxService:
    transport = LoopbackTransport()
    service = InboxService(tmp_path / name, transport)
    transport.bind(service)
    return service


def _receive(
    inbox: InboxService,
    message_id: str,
    *,
    recipient: str = A_URI,
    sender: str = X_URI,
    intent: str = "request",
) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="conv-wp7",
        sender=sender,
        recipient=recipient,
        payload=b'{"message":"please answer"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox.receive(message)
    return message


def _stamp(inbox: InboxService, message_id: str, at_ms: int) -> None:
    with inbox._db:
        inbox._db.execute(
            "UPDATE inbox SET fetched_at_ms = ? WHERE message_id = ?",
            (at_ms, message_id),
        )


def _expire(inbox: InboxService, message_id: str, at_ms: int) -> None:
    with inbox._db:
        inbox._db.execute(
            "UPDATE inbox SET expires_at_ms = ? WHERE message_id = ?",
            (at_ms, message_id),
        )


def _notice_port(inbox: Any, *, clock_ms: Any = None) -> Any:
    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        InboxReplyReminders,
    )

    return InboxReplyReminders(
        inbox, node_id=NODE, clock_ms=clock_ms or (lambda: 500)
    )


def test_p1_unreplied_requests_respects_window_kind_and_pending_state(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    _receive(inbox, "r1")
    _receive(inbox, "r2")
    _receive(inbox, "r3", intent="reply")
    _receive(inbox, "r4")
    _receive(inbox, "r5")
    _receive(inbox, "r6")
    _receive(inbox, "r7")
    _receive(inbox, "r8")
    inbox.ack(A_URI, "r2")
    _stamp(inbox, "r4", 150)
    _stamp(inbox, "r5", 50)
    _stamp(inbox, "r6", 100)
    _stamp(inbox, "r7", 150)
    _expire(inbox, "r7", 1)
    _stamp(inbox, "r8", 150)
    inbox.settle_harness_failure(
        A_URI,
        "r8",
        "HARNESS_FAILED",
        permanent=True,
        max_attempts=1,
        backoff_ms=(1,),
        now_ms=10,
    )

    port = _notice_port(inbox)

    opened = port.unreplied_requests(
        A_URI,
        delivery_ids=("r1", "r2", "r3"),
        fetched_since_ms=100,
        fetched_until_ms=200,
        since_inclusive=False,
    )
    closed = port.unreplied_requests(
        A_URI,
        delivery_ids=("r1", "r2", "r3"),
        fetched_since_ms=100,
        fetched_until_ms=200,
        since_inclusive=True,
    )
    no_window = port.unreplied_requests(
        A_URI,
        delivery_ids=("r1", "r2", "r3"),
        fetched_since_ms=None,
        fetched_until_ms=200,
        since_inclusive=False,
    )

    assert opened == ("r1", "r4")
    assert closed == ("r1", "r4", "r6")
    assert no_window == ("r1",)
    # The closed window includes a fetch exactly at the window start but not
    # one before it; the open window excludes it.
    assert "r5" not in closed and "r7" not in closed and "r8" not in closed
    # The original requests stay durable; the read never acks or replies.
    # ``r3`` is a reply and stays pending (it is only excluded from the
    # reminder candidates by intent).
    assert [m.message_id for m in inbox.pending_messages(A_URI)] == [
        "r1",
        "r3",
        "r4",
        "r5",
        "r6",
    ]


def test_p2_notice_summary_round_trips_everywhere(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    port = _notice_port(inbox)

    assert port.post_reply_reminder(A_URI, ("r1",), TEXT) is True

    expected_id = str(
        uuid5(NAMESPACE_URL, f"hyprial:reply-reminder:{A_URI}:r1")
    )
    notices = inbox.system_notices(A_URI)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.message_id == expected_id
    assert notice.conversation_id == expected_id
    assert notice.sender == f"system:hyprial:{NODE}"
    assert notice.recipient == A_URI
    assert notice.intent == "system"
    assert notice.lifecycle is DeliveryLifecycle.ONLINE_ONLY
    assert notice.idempotency_key == f"reply-reminder:{expected_id}"
    assert notice.summary == TEXT
    body = json.loads(notice.payload)
    assert body["reason"] == "reply-reminder"
    assert body["systemNotice"] is True
    assert body["openRequests"] == ["r1"]

    # Same group again: idempotent, still one row.
    assert port.post_reply_reminder(A_URI, ("r1",), TEXT) is True
    assert len(inbox.system_notices(A_URI)) == 1

    # Drain returns the summary and removes the row; re-posting writes again
    # (this is exactly why the consumer keeps its own reminded set).
    drained = inbox.drain_system_notices(A_URI)
    assert len(drained) == 1 and drained[0].summary == TEXT
    assert inbox.system_notices(A_URI) == ()
    assert port.post_reply_reminder(A_URI, ("r1",), TEXT) is True
    assert len(inbox.system_notices(A_URI)) == 1


def test_p2_summary_survives_reopen_and_authority_read(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "roundtrip.sqlite3")
    assert _notice_port(inbox).post_reply_reminder(A_URI, ("r1",), TEXT) is True
    inbox.close()

    reopened = _inbox(tmp_path, "roundtrip.sqlite3")
    notices = reopened.system_notices(A_URI)
    assert len(notices) == 1 and notices[0].summary == TEXT

    from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection

    authority = InboxReadProjection(tmp_path / "roundtrip.sqlite3")
    authority_notices = authority.system_notices(A_URI)
    assert len(authority_notices) == 1
    assert authority_notices[0].summary == TEXT


def test_p2_old_database_is_migrated_to_carry_summary(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE system_notices (
               message_id TEXT PRIMARY KEY,
               conversation_id TEXT NOT NULL,
               sender TEXT NOT NULL,
               recipient TEXT NOT NULL,
               payload BLOB NOT NULL,
               intent TEXT NOT NULL,
               lifecycle TEXT NOT NULL,
               idempotency_key TEXT,
               created_at_ms INTEGER NOT NULL
           )"""
    )
    connection.execute(
        """INSERT INTO system_notices(
               message_id, conversation_id, sender, recipient, payload,
               intent, lifecycle, idempotency_key, created_at_ms
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "legacy-notice",
            "legacy-notice",
            f"system:hyprial:{NODE}",
            A_URI,
            b'{"message":"legacy"}',
            "system",
            "online_only",
            None,
            1,
        ),
    )
    connection.commit()
    connection.close()

    inbox = _inbox(tmp_path, "old.sqlite3")
    legacy = inbox.system_notices(A_URI)
    assert len(legacy) == 1
    assert legacy[0].summary is None  # old row keeps NULL, no invented value
    assert _notice_port(inbox).post_reply_reminder(A_URI, ("r1",), TEXT) is True
    fresh = [n for n in inbox.system_notices(A_URI) if n.message_id != "legacy-notice"]
    assert len(fresh) == 1 and fresh[0].summary == TEXT


def test_p3_fetched_between_matches_on_service_and_facade(tmp_path: Path) -> None:
    database = tmp_path / "both.sqlite3"
    inbox = _inbox(tmp_path, "both.sqlite3")
    _receive(inbox, "r1")
    _receive(inbox, "r2")
    _stamp(inbox, "r1", 150)
    _stamp(inbox, "r2", 250)

    coordinator = DeliveryCustodyCoordinator(
        database, MemoryDeliveryTransport(), _Events()
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        service_ids = inbox.fetched_between(
            A_URI, since_ms=100, until_ms=200, since_inclusive=False
        )
        facade_ids = facade.fetched_between(
            A_URI, since_ms=100, until_ms=200, since_inclusive=False
        )
        assert service_ids == facade_ids == ("r1",)
        assert inbox.fetched_between(
            A_URI, since_ms=250, until_ms=250, since_inclusive=True
        ) == ("r2",)
        assert inbox.fetched_between(
            A_URI, since_ms=250, until_ms=250, since_inclusive=False
        ) == ()
    finally:
        facade.close()


class _Events:
    def publish(self, _event: object) -> None:
        return None


class _FlakyPostPort:
    """First post fails; the replay of the same completed event succeeds."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.posts = 0

    def unreplied_requests(self, actor: str, **kwargs: Any) -> tuple[str, ...]:
        return self._inner.unreplied_requests(actor, **kwargs)

    def post_reply_reminder(
        self, actor: str, request_ids: Any, text: str
    ) -> bool:
        self.posts += 1
        if self.posts == 1:
            return False
        return self._inner.post_reply_reminder(actor, request_ids, text)

    def log(self, level: str, event: str, **fields: Any) -> None:
        return None


def test_r2_interactive_replay_does_not_lose_the_fetched_request(
    tmp_path: Path,
) -> None:
    from hyprial.biz.impl.hooks.reminder import ReplyReminderConsumer
    from hyprial.kernel import HookEvent, turn_event_payload

    inbox = _inbox(tmp_path)
    _receive(inbox, "r1")
    _stamp(inbox, "r1", 150)
    flaky = _FlakyPostPort(_notice_port(inbox))
    consumer = ReplyReminderConsumer(flaky)

    def completed(seq: int, ended_at_ms: int) -> HookEvent:
        return HookEvent(
            event="turn.completed",
            kind="observe",
            emitted_at_ms=ended_at_ms,
            seq=seq,
            source="daemon.turns",
            actor=A_URI,
            payload=turn_event_payload(
                delivery_id="",
                sender=X_URI,
                conversation="conv",
                started_at_ms=ended_at_ms,
                ended_at_ms=ended_at_ms,
                status="completed",
                tool_names=(),
                output="",
                prompt="",
                tool_calls=(),
                fidelity="observed",
            ),
        )

    # First completion establishes the window start; nothing is fetched then.
    consumer.observe(completed(1, 100))
    # Second completion's window covers the fetch at 150; the first post fails.
    replay = completed(2, 200)
    consumer.observe(replay)
    assert flaky.posts == 1
    assert inbox.system_notices(A_URI) == ()

    # Replaying the same completed event reuses the window and retries.
    consumer.observe(replay)
    notices = inbox.system_notices(A_URI)
    assert len(notices) == 1
    assert notices[0].summary == TEXT
    assert json.loads(notices[0].payload)["openRequests"] == ["r1"]


class _SpyInbox:
    """Forwards to a real inbox and records every public call by name."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def _recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return _recorded


def _state(inbox: InboxService) -> tuple[Any, ...]:
    with inbox._db:
        rows = inbox._db.execute(
            "SELECT message_id, fetched_at_ms, acked_at_ms FROM inbox "
            "ORDER BY message_id"
        ).fetchall() if _has_acked_column(inbox) else inbox._db.execute(
            "SELECT message_id, fetched_at_ms FROM inbox ORDER BY message_id"
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def _has_acked_column(inbox: InboxService) -> bool:
    columns = {row[1] for row in inbox._db.execute("PRAGMA table_info(inbox)")}
    return "acked_at_ms" in columns


def test_r3_read_only_guard_notice_and_lookup_never_touch_custody(
    tmp_path: Path,
) -> None:
    """Review R3 (side-effect guard): querying and writing a notice only reads
    pending/fetched rows and writes the system notice.  No ack, no failure
    settle, no fetch, no retry: nothing that frees capacity or feeds a
    controller sample."""

    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        open_requests_by_sender,
    )

    inbox = _inbox(tmp_path)
    _receive(inbox, "r1")
    _stamp(inbox, "r1", 150)
    before = _state(inbox)
    spy = _SpyInbox(inbox)
    port = _notice_port(spy)

    assert port.unreplied_requests(
        A_URI,
        delivery_ids=("r1",),
        fetched_since_ms=100,
        fetched_until_ms=200,
        since_inclusive=False,
    ) == ("r1",)
    assert port.post_reply_reminder(A_URI, ("r1",), TEXT) is True
    assert open_requests_by_sender(spy, recipient=A_URI, senders=(X_URI,)) == {
        X_URI: ("r1",)
    }

    assert set(spy.calls) <= {
        "pending_messages",
        "fetched_between",
        "receive_system_notice",
    }
    assert _state(inbox) == before
    assert [m.message_id for m in inbox.pending_messages(A_URI)] == ["r1"]


def test_r3_reclaim_drain_then_old_completed_yields_no_notice(
    tmp_path: Path,
) -> None:
    """Review R3 (recycle drain): the request was acked (drained) before the
    old turn.completed is processed; no notice and no open-request field."""

    from hyprial.biz.impl.hooks.reminder import ReplyReminderConsumer
    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        open_requests_by_sender,
    )
    from hyprial.kernel import HookEvent, turn_event_payload

    inbox = _inbox(tmp_path)
    _receive(inbox, "r1")
    _stamp(inbox, "r1", 150)
    inbox.ack(A_URI, "r1")
    consumer = ReplyReminderConsumer(_notice_port(inbox))

    def event(name: str, seq: int, at_ms: int, delivery_id: str) -> HookEvent:
        return HookEvent(
            event=name,
            kind="observe",
            emitted_at_ms=at_ms,
            seq=seq,
            source="daemon.turns",
            actor=A_URI,
            payload=turn_event_payload(
                delivery_id=delivery_id,
                sender=X_URI,
                conversation="conv",
                started_at_ms=at_ms,
                ended_at_ms=at_ms,
                status="completed",
                tool_names=(),
                output="",
                prompt="",
                tool_calls=(),
                fidelity="observed",
            ),
        )

    consumer.observe(event("turn.started", 1, 100, ""))
    consumer.observe(event("turn.completed", 2, 200, "r1"))

    assert inbox.system_notices(A_URI) == ()
    assert open_requests_by_sender(inbox, recipient=A_URI, senders=(X_URI,)) == {}
    assert inbox.pending_messages(A_URI) == ()


def test_r3_owner_request_is_reminded_without_a_link_file(tmp_path: Path) -> None:
    """Review R3 (owner priority): a pending request from the owner is a
    reminder candidate and shows in the field; nothing filters by link files."""

    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        open_requests_by_sender,
    )

    owner = "user:wp7-owner"
    inbox = _inbox(tmp_path)
    _receive(inbox, "o1", sender=owner)
    _stamp(inbox, "o1", 150)
    port = _notice_port(inbox)

    assert port.unreplied_requests(
        A_URI,
        delivery_ids=("o1",),
        fetched_since_ms=None,
        fetched_until_ms=200,
        since_inclusive=False,
    ) == ("o1",)
    assert port.unreplied_requests(
        A_URI,
        delivery_ids=(),
        fetched_since_ms=100,
        fetched_until_ms=200,
        since_inclusive=False,
    ) == ("o1",)
    assert open_requests_by_sender(inbox, recipient=A_URI, senders=(owner,)) == {
        owner: ("o1",)
    }
    assert port.post_reply_reminder(A_URI, ("o1",), TEXT) is True
    assert len(inbox.system_notices(A_URI)) == 1


def test_a2_window_edges_recipient_and_id_dedup(tmp_path: Path) -> None:
    """Review A2: a fetch exactly at the window end counts, one later does
    not, another recipient's request never counts, and an id found both as
    injected (I1) and fetched (I2) appears once."""

    other = "agent:wp7-owner:wp7-node:other"
    inbox = _inbox(tmp_path)
    _receive(inbox, "e1")  # fetched exactly at end
    _receive(inbox, "e2")  # fetched after end
    _receive(inbox, "e3", recipient=other)  # different recipient
    _receive(inbox, "e4")  # both injected and fetched
    _stamp(inbox, "e1", 200)
    _stamp(inbox, "e2", 201)
    _stamp(inbox, "e3", 150)
    _stamp(inbox, "e4", 150)
    port = _notice_port(inbox)

    result = port.unreplied_requests(
        A_URI,
        delivery_ids=("e4",),
        fetched_since_ms=100,
        fetched_until_ms=200,
        since_inclusive=False,
    )

    assert result == ("e1", "e4")
    assert len(result) == len(set(result))
    assert "e2" not in result and "e3" not in result


def test_a2_first_fetch_stamp_is_not_rewritten(tmp_path: Path) -> None:
    """Review A2: ``fetched_at_ms`` is the first-fetch stamp; a later fetch
    leaves it alone."""

    inbox = _inbox(tmp_path)
    _receive(inbox, "r1")
    inbox.fetch_pending(A_URI, now_ms=1_000)
    inbox.fetch_pending(A_URI, now_ms=2_000)
    with inbox._db:
        (stamp,) = inbox._db.execute(
            "SELECT fetched_at_ms FROM inbox WHERE message_id = 'r1'"
        ).fetchone()
    assert stamp == 1_000
