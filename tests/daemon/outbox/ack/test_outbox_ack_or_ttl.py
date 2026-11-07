"""ACK-or-TTL retry behavior shared by the legacy and actor authorities."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.service.policy import RetryPolicy, online_retry_next_attempt_ms
from hyprial.daemon.impl.runtime import settlement
from hyprial.daemon.impl.runtime.reconcile import _BridgeReconcileMixin
from hyprial.kernel import DEFAULT_POLICIES, EXTERNAL_IO

SENDER = "agent:owner:node-a:sender"
RECIPIENT = "agent:owner:node-b:recipient"
OTHER_RECIPIENT = "agent:owner:node-c:recipient"
ONLINE_RETRY_MAX_SECONDS = DEFAULT_POLICIES[EXTERNAL_IO].max_backoff
RETRY_CLAIM_LIMIT = 32


@pytest.mark.parametrize("attempts", [1025, 10_000, 1_000_000])
@pytest.mark.parametrize("remaining_ms", [1_000, 60_000])
def test_online_retry_large_persisted_attempts_keep_the_capped_schedule(
    attempts: int, remaining_ms: int,
) -> None:
    now = 1_000
    arguments = {
        "message_id": "long-held-online-message",
        "now_ms": now,
        "expires_at_ms": now + remaining_ms,
    }
    due = online_retry_next_attempt_ms(attempts=attempts, **arguments)
    assert due == online_retry_next_attempt_ms(attempts=64, **arguments)
    assert now < due <= min(arguments["expires_at_ms"], now + ONLINE_RETRY_MAX_SECONDS * 1000)


class _Events:
    def publish(self, _event: object) -> None:
        return None


class _LostAckTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.confirmation_queries = 0

    def confirm_fetch(self, message: InboxMessage) -> bool:
        del message
        self.confirmation_queries += 1
        return False


@dataclass
class _Engine:
    authority: Any
    transport: _LostAckTransport
    database: Path
    clock: list[int]


def _engine(
    kind: str,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: list[int] | None = None,
) -> _Engine:
    selected_clock = clock or [time.time_ns() // 1_000_000]
    transport = _LostAckTransport()
    policy = RetryPolicy()
    if kind == "legacy":
        authority: Any = InboxService(database, transport, retry_policy=policy)
    else:
        import hyprial.daemon.impl.inbox.actor.worker as worker_module

        monkeypatch.setattr(
            worker_module,
            "time",
            SimpleNamespace(
                time_ns=lambda: selected_clock[0] * 1_000_000,
                monotonic=time.monotonic,
                sleep=time.sleep,
            ),
        )
        coordinator = DeliveryCustodyCoordinator(
            database,
            transport,
            _Events(),
            retry_policy=policy,
        )
        authority = DeliveryCustodyFacade(coordinator, database)
    return _Engine(authority, transport, database, selected_clock)


def _message(
    message_id: str,
    *,
    recipient: str = RECIPIENT,
    expires_at_ms: int,
    intent: str = "request",
    idempotency_key: str | None = None,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-ack-or-ttl",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message":"retain until ack or ttl"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
        idempotency_key=idempotency_key,
        expires_at_ms=expires_at_ms,
    )


@pytest.fixture(params=("legacy", "actor"))
def engine(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Engine:
    database = tmp_path / f"{request.param}.sqlite3"
    value = _engine(request.param, database, monkeypatch)
    try:
        yield value
    finally:
        value.authority.close()


def _row(database: Path, table: str, message_id: str) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT * FROM {table} WHERE message_id = ?",  # noqa: S608
            (message_id,),
        ).fetchone()
    assert row is not None
    return row


def test_offline_pumps_do_not_spend_attempts_and_online_wake_advances(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + 10_000
    message = _message("offline-park", expires_at_ms=expires)
    engine.authority.submit(message, now_ms=start, defer_direct=True)

    for now_ms in (start + 1_000, start + 2_000, start + 3_000):
        engine.clock[0] = now_ms
        engine.authority.retry_due(now_ms=now_ms)

    item = engine.authority.outbox_item(message.message_id)
    assert item.attempts == 0
    assert item.next_attempt_ms == expires
    assert engine.authority.dlq_count() == 0
    assert engine.transport.delivered == []

    wake_ms = start + 4_000
    assert engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=wake_ms)
    assert engine.authority.outbox_item(message.message_id).next_attempt_ms == wake_ms


def test_online_lost_ack_uses_registry_curve_with_jitter_bounds(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + 60_000
    message = _message("registry-interval", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)

    delays: list[int] = []
    for attempt in range(1, 9):
        due = engine.authority.outbox_item(message.message_id).next_attempt_ms
        engine.clock[0] = due
        [result] = engine.authority.retry_due(now_ms=due)
        assert result.accepted is True and result.queued is True
        item = engine.authority.outbox_item(message.message_id)
        delay = item.next_attempt_ms - due
        delays.append(delay)
        policy = DEFAULT_POLICIES[EXTERNAL_IO]
        nominal = min(policy.max_backoff, policy.base_backoff * 2 ** (attempt - 1))
        if nominal == policy.max_backoff:
            lower = nominal * (1 - policy.jitter_ratio)
            upper = nominal
        else:
            lower = nominal * (1 - policy.jitter_ratio)
            upper = min(
                policy.max_backoff,
                nominal * (1 + policy.jitter_ratio),
            )
        assert int(lower * 1_000) <= delay <= ceil(upper * 1_000)

    assert delays == sorted(delays)
    assert int(
        ONLINE_RETRY_MAX_SECONDS * (1 - policy.jitter_ratio) * 1_000
    ) <= delays[-1] < int(ONLINE_RETRY_MAX_SECONDS * 1_000)


def test_actor_offline_reply_expires_and_settles_its_queued_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node-a reply cannot outlive its TTL while node-b is down."""

    start = time.time_ns() // 1_000_000
    engine = _engine(
        "actor",
        tmp_path / "offline-reply.sqlite3",
        monkeypatch,
        clock=[start],
    )
    reply = _message(
        "offline-reply",
        expires_at_ms=start + 1_000,
        intent="reply",
        idempotency_key="reply:inbound-request",
    )
    try:
        queued = engine.authority.submit(reply, now_ms=start, defer_direct=True)
        assert queued.accepted is True and queued.queued is True

        engine.clock[0] = start + 1_000
        [expired] = engine.authority.retry_due(now_ms=start + 1_000)

        assert expired.accepted is False
        assert expired.queued is False
        assert expired.code == "TTL_EXPIRED"
        assert engine.authority.outbox_count() == 0
        assert engine.authority.dlq_count() == 1
        joined = engine.authority.submit(reply, now_ms=start + 2_000)
        assert joined.accepted is False
        assert joined.queued is False
        assert joined.code == "TTL_EXPIRED"
    finally:
        engine.authority.close()


def test_retry_claim_is_bounded_per_round(engine: _Engine) -> None:
    start = engine.clock[0]
    engine.transport.set_online(RECIPIENT, True)
    for index in range(RETRY_CLAIM_LIMIT + 8):
        engine.authority.submit(
            _message(f"claim-{index}", expires_at_ms=start + 60_000),
            now_ms=start,
            defer_direct=True,
        )

    results = engine.authority.retry_due(now_ms=start)

    assert len(results) == RETRY_CLAIM_LIMIT
    assert len(engine.transport.delivered) == RETRY_CLAIM_LIMIT


def test_retry_due_excludes_an_actor_claim_while_io_is_in_flight(
    tmp_path: Path,
) -> None:
    class _BlockingTransport(_LostAckTransport):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def deliver(self, message: InboxMessage) -> bool:
            self.entered.set()
            assert self.release.wait(2.0)
            return super().deliver(message)

    start = time.time_ns() // 1_000_000
    transport = _BlockingTransport()
    transport.set_online(RECIPIENT, True)
    database = tmp_path / "claimed.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    authority = DeliveryCustodyFacade(coordinator, database)
    message = _message("claimed", expires_at_ms=start + 60_000)
    authority.submit(message, now_ms=start, defer_direct=True)
    errors: list[BaseException] = []

    def retry() -> None:
        try:
            authority.retry_due(now_ms=start)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    thread = threading.Thread(target=retry)
    thread.start()
    try:
        assert transport.entered.wait(1.0)
        assert authority.next_retry_due_ms() is None
    finally:
        transport.release.set()
        thread.join(2.0)
        authority.close()
    assert not thread.is_alive()
    assert errors == []


def test_empty_retry_rounds_are_floored_over_half_a_second() -> None:
    class _DueButEmpty:
        def __init__(self) -> None:
            self.rounds = 0

        def next_retry_due_ms(self) -> int:
            return 0

        def retry_due(self) -> list[object]:
            self.rounds += 1
            return []

    inbox = _DueButEmpty()
    pump = SimpleNamespace(
        inbox=inbox,
        _retry_pump_halt=threading.Event(),
        _retry_pump_kick=threading.Event(),
        _clock_ms=lambda: 1_000,
        _logger=None,
    )
    thread = threading.Thread(
        target=_BridgeReconcileMixin._retry_pump_run,
        args=(pump,),
    )
    thread.start()
    time.sleep(0.5)
    pump._retry_pump_halt.set()
    pump._retry_pump_kick.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert 1 <= inbox.rounds <= 3


def test_early_retry_kick_recomputes_without_polling_future_work() -> None:
    class _FutureDue:
        def __init__(self) -> None:
            self.rounds = 0

        def next_retry_due_ms(self) -> int:
            return 60_000

        def retry_due(self) -> list[object]:
            self.rounds += 1
            return []

    inbox = _FutureDue()
    pump = SimpleNamespace(
        inbox=inbox,
        _retry_pump_halt=threading.Event(),
        _retry_pump_kick=threading.Event(),
        _clock_ms=lambda: 1_000,
        _logger=None,
    )
    thread = threading.Thread(
        target=_BridgeReconcileMixin._retry_pump_run,
        args=(pump,),
    )
    thread.start()
    for _ in range(3):
        time.sleep(0.03)
        pump._retry_pump_kick.set()
    time.sleep(0.03)
    pump._retry_pump_halt.set()
    pump._retry_pump_kick.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert inbox.rounds == 0


def test_online_refusal_is_bounded_over_the_default_hold_window(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + DEFAULT_HOLD_TTL_MS
    message = _message("bounded-refusal", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)
    minimum_ceiling_delay = ONLINE_RETRY_MAX_SECONDS * (
        1 - DEFAULT_POLICIES[EXTERNAL_IO].jitter_ratio
    )
    maximum_attempts = ceil((DEFAULT_HOLD_TTL_MS / 1000) / minimum_ceiling_delay) + 6

    for _ in range(maximum_attempts + 1):
        if engine.authority.outbox_count() == 0:
            break
        due = engine.authority.outbox_item(message.message_id).next_attempt_ms
        engine.clock[0] = due
        engine.authority.retry_due(now_ms=due)

    assert engine.authority.outbox_count() == 0
    assert len(engine.transport.delivered) <= maximum_attempts


def test_explicit_twelve_hour_deadline_outlives_the_retry_counter_overflow(
    engine: _Engine,
) -> None:
    """A producer deadline can outlive even the old thirty-minute default."""
    start = engine.clock[0]
    window_ms = 12 * 60 * 60 * 1000
    expires = start + window_ms
    message = _message("explicit-long-hold", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    assert engine.authority.outbox_item(message.message_id).expires_at_ms == expires
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)
    minimum_delay = ONLINE_RETRY_MAX_SECONDS * (1 - DEFAULT_POLICIES[EXTERNAL_IO].jitter_ratio)
    maximum_attempts = ceil(window_ms / 1000 / minimum_delay) + 6
    results = []
    for _ in range(maximum_attempts + 1):
        if engine.authority.outbox_count() == 0:
            break
        due = engine.authority.outbox_item(message.message_id).next_attempt_ms
        engine.clock[0] = due
        results = engine.authority.retry_due(now_ms=due)
    assert engine.authority.outbox_count() == 0
    assert engine.clock[0] == expires
    assert len(engine.transport.delivered) > 1024
    assert results[-1].code == "TTL_EXPIRED"


def test_service_and_actor_online_retry_schedules_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = time.time_ns() // 1_000_000
    engines = [
        _engine(kind, tmp_path / f"parity-{kind}.sqlite3", monkeypatch, clock=[start])
        for kind in ("legacy", "actor")
    ]
    schedules: list[list[int]] = []
    try:
        for candidate in engines:
            message = _message("schedule-parity", expires_at_ms=start + 60_000)
            candidate.transport.set_online(RECIPIENT, True)
            candidate.authority.submit(message, now_ms=start, defer_direct=True)
            candidate.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)
            schedule: list[int] = []
            for _ in range(8):
                due = candidate.authority.outbox_item(message.message_id).next_attempt_ms
                candidate.clock[0] = due
                candidate.authority.retry_due(now_ms=due)
                schedule.append(
                    candidate.authority.outbox_item(message.message_id).next_attempt_ms
                    - due
                )
            schedules.append(schedule)
    finally:
        for candidate in engines:
            candidate.authority.close()

    assert schedules[0] == schedules[1]


def test_online_lost_ack_retries_beyond_old_budget_without_confirmation(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + 10_000
    message = _message("lost-ack", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)

    rounds = 3
    for _ in range(rounds):
        due = engine.authority.outbox_item(message.message_id).next_attempt_ms
        engine.clock[0] = due
        [result] = engine.authority.retry_due(now_ms=due)
        assert result.accepted is True and result.queued is True
        assert engine.authority.outbox_count() == 1
        assert engine.authority.dlq_count() == 0

    item = engine.authority.outbox_item(message.message_id)
    assert item.attempts == rounds
    assert item.confirmation_started_at_ms is None
    assert item.confirmation_attempts == 0
    assert engine.transport.confirmation_queries == 0
    assert engine.transport.delivered == [message.message_id] * rounds


def test_online_wake_advances_outbox_and_custody_only_for_live_target(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    future = start + 10_000
    expired = start + 50
    messages = (
        _message("outbox-target", expires_at_ms=future),
        _message("outbox-other", recipient=OTHER_RECIPIENT, expires_at_ms=future),
        _message("outbox-expired", expires_at_ms=expired),
    )
    custody = (
        _message("custody-target", expires_at_ms=future),
        _message("custody-other", recipient=OTHER_RECIPIENT, expires_at_ms=future),
        _message("custody-expired", expires_at_ms=expired),
    )
    for message in messages:
        engine.authority.submit(message, now_ms=start, defer_direct=True)
    for message in custody:
        assert engine.authority.accept_custody(
            message, mailbox_node="node-mailbox", now_ms=start
        ).acknowledged
    with closing(sqlite3.connect(engine.database)) as connection:
        connection.execute("UPDATE outbox SET next_attempt_ms = expires_at_ms")
        connection.execute("UPDATE custody SET next_attempt_ms = expires_at_ms")
        connection.commit()

    wake_ms = start + 100
    assert engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=wake_ms)

    assert int(_row(engine.database, "outbox", "outbox-target")["next_attempt_ms"]) == wake_ms
    assert int(_row(engine.database, "custody", "custody-target")["next_attempt_ms"]) == wake_ms
    assert int(_row(engine.database, "outbox", "outbox-other")["next_attempt_ms"]) == future
    assert int(_row(engine.database, "custody", "custody-other")["next_attempt_ms"]) == future
    assert int(_row(engine.database, "outbox", "outbox-expired")["next_attempt_ms"]) == expired
    assert int(_row(engine.database, "custody", "custody-expired")["next_attempt_ms"]) == expired


def test_expired_row_is_never_delivered_and_records_ttl_reason(engine: _Engine) -> None:
    start = engine.clock[0]
    expires = start + 100
    message = _message("expired", expires_at_ms=expires)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.transport.set_online(RECIPIENT, True)
    engine.transport.confirm_direct(message.message_id)

    engine.clock[0] = expires
    [result] = engine.authority.retry_due(now_ms=expires)

    assert result.accepted is False
    assert result.code == "TTL_EXPIRED"
    assert engine.transport.delivered == []
    [record] = engine.authority.delivery_status_records(
        SENDER, message_id=message.message_id
    )
    assert record.state.value == "expired"
    assert record.reason == "TTL_EXPIRED"


def test_ack_records_the_canonical_reason(engine: _Engine) -> None:
    start = engine.clock[0]
    message = _message("acked", expires_at_ms=start + 10_000)
    engine.transport.set_online(RECIPIENT, True)
    engine.transport.confirm_direct(message.message_id)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)

    engine.clock[0] = start
    [result] = engine.authority.retry_due(now_ms=start)

    assert result.accepted is True and result.queued is False
    [record] = engine.authority.delivery_status_records(
        SENDER, message_id=message.message_id
    )
    assert record.state.value == "fetched"
    assert record.reason == "ACK_RECEIVED"


def test_all_inbox_read_paths_hide_expired_rows_before_sweep(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + 100
    message = _message("expired-inbox-read", expires_at_ms=expires)
    assert engine.authority.receive(message, now_ms=start).acknowledged

    assert engine.authority.pending_messages(RECIPIENT, now_ms=expires) == ()
    assert engine.authority.dispatchable_messages(RECIPIENT, now_ms=expires) == ()
    assert engine.authority.fetch_pending(RECIPIENT, now_ms=expires) == ()


def test_retry_due_time_covers_outbox_and_custody(engine: _Engine) -> None:
    start = engine.clock[0]
    outbox = _message("due-outbox", expires_at_ms=start + 20_000)
    custody = _message("due-custody", expires_at_ms=start + 20_000)
    engine.authority.submit(outbox, now_ms=start, defer_direct=True)
    assert engine.authority.accept_custody(
        custody, mailbox_node="node-mailbox", now_ms=start
    ).acknowledged
    with closing(sqlite3.connect(engine.database)) as connection:
        connection.execute(
            "UPDATE outbox SET next_attempt_ms = ? WHERE message_id = ?",
            (start + 9_000, outbox.message_id),
        )
        connection.execute(
            "UPDATE custody SET next_attempt_ms = ? WHERE message_id = ?",
            (start + 3_000, custody.message_id),
        )
        connection.commit()

    assert engine.authority.next_retry_due_ms() == start + 3_000


def test_retry_pump_wait_is_due_aware_and_idle_bound_uses_external_io_policy() -> None:
    class _Due:
        def __init__(self, due_ms: int | None) -> None:
            self.due_ms = due_ms

        def next_retry_due_ms(self) -> int | None:
            return self.due_ms

    policy = DEFAULT_POLICIES[EXTERNAL_IO]
    idle_wait = settlement._retry_pump_wait_seconds(_Due(None), now_ms=1_000)
    assert ceil(policy.max_backoff / idle_wait) <= 1
    assert idle_wait == pytest.approx(policy.max_backoff)
    assert settlement._RETRY_PUMP_IDLE_SECONDS == pytest.approx(
        policy.max_backoff
    )
    assert settlement._retry_pump_wait_seconds(
        _Due(2_500), now_ms=1_000
    ) == pytest.approx(1.5)
    assert settlement._retry_pump_wait_seconds(
        _Due(20_000), now_ms=1_000
    ) == pytest.approx(policy.max_backoff)


def test_attempts_past_the_curve_ceiling_never_delete_before_ttl(
    engine: _Engine,
) -> None:
    """Only ACK or TTL delete mail: the attempt count is never a third reason."""

    start = engine.clock[0]
    minimum_ceiling_delay = ONLINE_RETRY_MAX_SECONDS * (
        1 - DEFAULT_POLICIES[EXTERNAL_IO].jitter_ratio
    )
    rounds = ceil((DEFAULT_HOLD_TTL_MS / 1000) / minimum_ceiling_delay) + 6
    # A producer deadline longer than the entire default retry window.
    expires = start + int((rounds + 1) * ONLINE_RETRY_MAX_SECONDS * 1000)
    message = _message("past-curve-ceiling", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    engine.authority.submit(message, now_ms=start, defer_direct=True)
    engine.authority.wake_outbox_recipient(RECIPIENT, now_ms=start)
    for _ in range(rounds):
        due = engine.authority.outbox_item(message.message_id).next_attempt_ms
        assert due < expires
        engine.clock[0] = due
        [result] = engine.authority.retry_due(now_ms=due)
        assert result.accepted is True and result.queued is True

    assert engine.authority.outbox_count() == 1
    assert engine.authority.dlq_count() == 0
    assert engine.authority.outbox_item(message.message_id).attempts == rounds
    assert len(engine.transport.delivered) == rounds

    engine.clock[0] = expires
    [result] = engine.authority.retry_due(now_ms=expires)
    assert result.accepted is False
    assert result.code == "TTL_EXPIRED"
    assert engine.authority.outbox_count() == 0


def test_custody_attempts_past_the_curve_ceiling_never_delete_before_ttl(
    engine: _Engine,
) -> None:
    start = engine.clock[0]
    expires = start + 2 * 60 * 60 * 1_000
    message = _message("custody-past-curve-ceiling", expires_at_ms=expires)
    engine.transport.set_online(RECIPIENT, True)
    assert engine.authority.accept_custody(
        message,
        mailbox_node="mailbox-node",
        now_ms=start,
    ).acknowledged

    for _ in range(300):
        row = _row(engine.database, "custody", message.message_id)
        due = int(row["next_attempt_ms"])
        assert due < expires
        engine.clock[0] = due
        [result] = engine.authority.retry_due(now_ms=due)
        assert result.accepted is True and result.queued is True

    row = _row(engine.database, "custody", message.message_id)
    assert int(row["attempts"]) == 300
    assert engine.authority.custody_count() == 1
    assert engine.authority.dlq_count() == 0

    engine.clock[0] = expires
    [expired] = engine.authority.retry_due(now_ms=expires)
    assert expired.accepted is False
    assert expired.code == "TTL_EXPIRED"
    assert engine.authority.custody_count() == 0



def test_new_work_after_an_empty_round_is_served_within_the_floor() -> None:
    """Production shape: with the due reader, an empty round floors the pump,
    and work that becomes due during that floor is served right after it, not
    at the idle ceiling (guards the #1246 retry-pump question)."""

    class _NewWorkArrives:
        def __init__(self) -> None:
            self.rounds = 0
            self.due_ms: int | None = 0

        def next_retry_due_ms(self) -> int | None:
            return self.due_ms

        def retry_due(self) -> list[object]:
            self.rounds += 1
            self.due_ms = None  # nothing left: an empty round
            return []

    inbox = _NewWorkArrives()
    pump = SimpleNamespace(
        inbox=inbox,
        _retry_pump_halt=threading.Event(),
        _retry_pump_kick=threading.Event(),
        _clock_ms=lambda: 1_000,
        _logger=None,
    )
    thread = threading.Thread(
        target=_BridgeReconcileMixin._retry_pump_run,
        args=(pump,),
    )
    thread.start()
    try:
        deadline = time.monotonic() + 1.0
        while inbox.rounds < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert inbox.rounds == 1
        inbox.due_ms = 0  # new work is due now, and its arrival kicks the pump
        pump._retry_pump_kick.set()
        deadline = time.monotonic() + 1.5  # floor 0.25 s; idle ceiling 10 s
        while inbox.rounds < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert inbox.rounds == 2, "new work waited for the idle ceiling"
    finally:
        pump._retry_pump_halt.set()
        pump._retry_pump_kick.set()
        thread.join(2.0)
    assert not thread.is_alive()
