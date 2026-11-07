"""Final outbox attempt and recipient-online wake regressions."""

from __future__ import annotations

import time
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.runtime import _stale_fence_rejection
from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.service.policy import OFFLINE_RECHECK_INTERVAL_MS
from hyprial.daemon.impl.inbox.actor.custody import DeliveryCustody
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import (
    BoolMutationCompleted,
    PinnedEventClaim,
    SubmissionCompleted,
    SubmissionProjection,
    WakeOutboxRecipientCommand,
)
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.links.wake import RecipientWakeCoordinator
from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.daemon.impl.transport.presence_actor import (
    ActorOnlineTransition,
    PresenceProjection,
)
from hyprial.daemon.impl.transport.zenoh.presence import LivelinessDirectory

SENDER = "agent:owner:node-a:sender"
RECIPIENT = "agent:owner:node-b:recipient"
OTHER_RECIPIENT = "agent:owner:node-c:other"
EARLY_RECIPIENT = "agent:owner:node-a:early"


def _message(
    message_id: str,
    recipient: str = RECIPIENT,
    *,
    expires_at_ms: int | None = None,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-final-attempt",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message":"keep trying"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
        expires_at_ms=expires_at_ms,
    )


def _advance_beyond_old_attempt_budget(service: object, message_id: str) -> None:
    for _ in range(6):
        due = service.outbox_item(message_id).next_attempt_ms  # type: ignore[attr-defined]
        service.retry_due(now_ms=due)  # type: ignore[attr-defined]
    assert service.outbox_item(message_id).attempts == 6  # type: ignore[attr-defined]


def test_reported_timeline_last_round_delivers_when_recipient_is_online(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        transport.set_online(RECIPIENT, True)
        service.submit(_message("final-online"), now_ms=0, defer_direct=True)
        _advance_beyond_old_attempt_budget(service, "final-online")
        transport.confirm_direct("final-online")

        [result] = service.retry_due(
            now_ms=service.outbox_item("final-online").next_attempt_ms
        )

        assert result.accepted is True
        assert result.queued is False
        assert result.code is None
        assert transport.delivered == ["final-online"] * 7
        assert service.outbox_count() == 0
        assert service.dlq_count() == 0
    finally:
        service.close()


class _Events:
    def publish(self, _event: object) -> None:
        return None


def test_actor_authority_last_round_matches_the_service_contract(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    database = tmp_path / "authority.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        now_ms = time.time_ns() // 1_000_000
        transport.set_online(RECIPIENT, True)
        facade.submit(_message("authority-final"), now_ms=now_ms, defer_direct=True)
        _advance_beyond_old_attempt_budget(facade, "authority-final")
        transport.confirm_direct("authority-final")

        [result] = facade.retry_due(
            now_ms=facade.outbox_item("authority-final").next_attempt_ms
        )

        assert result.accepted is True
        assert result.queued is False
        assert result.code is None
        assert transport.delivered == ["authority-final"] * 7
        assert facade.outbox_count() == 0
        assert facade.dlq_count() == 0
    finally:
        facade.close()


def test_offline_row_stays_parked_until_ttl(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        service.submit(_message("final-offline"), now_ms=0)
        held = service.outbox_item("final-offline")
        assert held.attempts == 0
        [result] = service.retry_due(now_ms=held.expires_at_ms)

        assert result.accepted is False
        assert result.code == "TTL_EXPIRED"
        assert transport.delivered == []
        assert transport.custody_transfers == []
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
    finally:
        service.close()


def test_expired_row_is_never_delivered(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        service.submit(_message("expired", expires_at_ms=500), now_ms=0)
        transport.set_online(RECIPIENT, True)
        transport.confirm_direct("expired")

        [result] = service.retry_due(now_ms=500)

        assert result.accepted is False
        assert result.code == "TTL_EXPIRED"
        assert transport.delivered == []
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
    finally:
        service.close()


def test_wake_updates_only_live_delayed_rows_for_one_recipient(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "wake.sqlite3", transport)
    try:
        service.submit(_message("target"), now_ms=0)
        service.submit(_message("expired", expires_at_ms=200), now_ms=0)
        service.submit(_message("other", OTHER_RECIPIENT), now_ms=0)
        assert service.wake_outbox_recipient(RECIPIENT, now_ms=250)
        items = {
            item.message.message_id: item.next_attempt_ms
            for item in service.outbox_items()
        }
        # The untouched recipient's row keeps its submit-time offline
        # recheck (PR #1463 finding 1): no longer parked at the full TTL,
        # still not advanced by a wake that names another recipient.
        assert items == {
            "target": 250,
            "expired": 200,
            "other": OFFLINE_RECHECK_INTERVAL_MS,
        }
    finally:
        service.close()


class _Registration:
    def close(self) -> None:
        return None


class _PresenceSession:
    def __init__(self) -> None:
        self._live: set[str] = set()
        self._actor_callback: Callable[[TransportSample], None] | None = None
        self._rebuild: Callable[[], None] | None = None

    def on_rebuild(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._rebuild = callback
        return lambda: None

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> _Registration:
        del history
        if key_expr.endswith("/liveliness/actor/*"):
            self._actor_callback = callback
        return _Registration()

    def get_liveliness(self, key: str, *, timeout: float = 1.0):
        return [TransportSample(key, b"")] if key in self._live else []

    def actor_event(self, actor: str, *, kind: str = "put") -> None:
        key = KeySpace().actor_liveliness(actor)
        if kind == "delete":
            self._live.discard(key)
        else:
            self._live.add(key)
        assert self._actor_callback is not None
        self._actor_callback(
            TransportSample(KeySpace().actor_liveliness(actor), b"", kind=kind)
        )

    def rebuild(self) -> None:
        assert self._rebuild is not None
        self._rebuild()


def _wait(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _retry_due_yielding_stale_rounds(
    inbox: DeliveryCustodyFacade, *, now_ms: int, rounds: int = 20
) -> list[object]:
    """Call retry_due the way the daemon pump does while other writers run.

    retry_due reads the generation/version fence and then submits, so a wake
    command committing in between gets this round rejected as STALE_COMMAND.
    The pump treats that as the expected loser of the race and re-reads the
    fence (daemon/runtime.py _stale_fence_rejection); a test that races the
    live wake worker must do the same instead of asserting on one round.
    """

    for _ in range(rounds):
        try:
            return inbox.retry_due(now_ms=now_ms)
        except Exception as error:
            if not _stale_fence_rejection(error):
                raise
            time.sleep(0.005)
    raise AssertionError(f"retry_due stayed stale for {rounds} rounds")


def test_late_presence_binding_replays_once_and_reset_fences_old_generation() -> None:
    session = _PresenceSession()
    directory = LivelinessDirectory(session)  # type: ignore[arg-type]
    transitions: list[ActorOnlineTransition] = []
    try:
        session.actor_event(RECIPIENT)
        _wait(lambda: directory.actor_online(RECIPIENT))
        stop = directory.bind_actor_online(transitions.append)
        _wait(lambda: len(transitions) == 1)
        first_generation = transitions[0].generation

        session.actor_event(RECIPIENT)
        time.sleep(0.03)
        assert len(transitions) == 1
        assert transitions[0].actor == RECIPIENT

        session.rebuild()
        assert not directory.actor_online(RECIPIENT)
        session.actor_event(RECIPIENT)
        _wait(lambda: len(transitions) == 2)
        assert transitions[-1].generation == first_generation + 1
        assert transitions[-1].sequence == 1
        stop()
    finally:
        directory.close()


class _WakeSink:
    def __init__(self, router: CorrelatedInboxEventRouter) -> None:
        self.router = router
        self.commands: list[WakeOutboxRecipientCommand] = []

    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        return PortAdmission.ACCEPTED

    def settle(self, index: int = -1) -> None:
        command = self.commands[index]
        self.router.publish(
            BoolMutationCompleted(
                command.correlation_id,
                1,
                len(self.commands),
                "wake_outbox_recipient",
                True,
            )
        )


class _EvictingCompletionSink(_WakeSink):
    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        self.router.publish(
            BoolMutationCompleted(
                command.correlation_id,
                1,
                1,
                "wake_outbox_recipient",
                True,
            )
        )
        self.router.publish(
            SubmissionCompleted(
                "unrelated-completion",
                1,
                2,
                SubmissionProjection("unrelated-message", True),
            )
        )
        return PortAdmission.ACCEPTED


def test_wake_completion_is_pinned_until_waiting_owner_releases_it() -> None:
    projection = PresenceProjection(1, actors=frozenset({RECIPIENT}))
    router = CorrelatedInboxEventRouter(capacity=1)
    sink = _EvictingCompletionSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT,)),
        lambda: projection,
        completion_timeout=0.01,
    )
    coordinator.submit(ActorOnlineTransition(1, RECIPIENT, 1))
    _wait(lambda: len(sink.commands) == 1)
    assert coordinator.close(1.0)
    assert len(sink.commands) == 1


def test_pinned_completion_cells_fail_bounded_when_capacity_is_owned() -> None:
    router = CorrelatedInboxEventRouter(capacity=1)
    assert router.claim_pinned("first") is PinnedEventClaim.OWNER
    assert router.claim_pinned("second") is PinnedEventClaim.FULL
    assert not router.claim("ordinary")
    router.release_pinned("first")
    assert router.claim_pinned("second") is PinnedEventClaim.OWNER
    router.release_pinned("second")


class _FirstOverloadedWakeSink(_WakeSink):
    def __init__(self, router: CorrelatedInboxEventRouter) -> None:
        super().__init__(router)
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        if len(self.commands) == 1:
            self.entered.set()
            assert self.release.wait(1.0)
            return PortAdmission.OVERLOADED
        self.router.publish(
            BoolMutationCompleted(
                command.correlation_id,
                1,
                len(self.commands),
                "wake_outbox_recipient",
                True,
            )
        )
        return PortAdmission.ACCEPTED


def test_new_ready_intent_cannot_promote_an_older_successor() -> None:
    projection = {
        "value": PresenceProjection(
            1,
            actors=frozenset({RECIPIENT}),
            online_sequence=1,
        )
    }
    ticks = iter((1, 2, 3, 4))
    router = CorrelatedInboxEventRouter()
    sink = _FirstOverloadedWakeSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT,)),
        lambda: projection["value"],
        completion_timeout=0.01,
        clock_ms=lambda: next(ticks),
    )
    coordinator.submit(ActorOnlineTransition(1, RECIPIENT, 1))
    assert sink.entered.wait(1.0)
    projection["value"] = replace(projection["value"], online_sequence=2)
    coordinator.submit(ActorOnlineTransition(1, RECIPIENT, 2))
    sink.release.set()
    _wait(
        lambda: coordinator._pending[RECIPIENT].phase.value == "ready"  # noqa: SLF001
    )
    projection["value"] = replace(projection["value"], online_sequence=3)
    coordinator.submit(ActorOnlineTransition(1, RECIPIENT, 3))
    assert coordinator.close(1.0)
    assert [command.now_ms for command in sink.commands] == [1, 3]


class _HoldingWakeRouter(CorrelatedInboxEventRouter):
    def __init__(self) -> None:
        super().__init__()
        self.held: list[BoolMutationCompleted] = []

    def publish(self, event: object) -> None:
        if (
            isinstance(event, BoolMutationCompleted)
            and event.operation == "wake_outbox_recipient"
            and not self.held
        ):
            self.held.append(event)
            return
        super().publish(event)

    def release_held(self) -> None:
        [event] = self.held
        super().publish(event)


class _RecordingWakeSink:
    def __init__(self, inner: DeliveryCustodyCoordinator) -> None:
        self.inner = inner
        self.commands: list[WakeOutboxRecipientCommand] = []

    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        return self.inner.submit(command)


def test_new_online_sequence_retains_successor_after_old_sql_completion(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    database = tmp_path / "wake-successor.sqlite3"
    router = _HoldingWakeRouter()
    inbox_coordinator = DeliveryCustodyCoordinator(database, transport, router)
    inbox = DeliveryCustodyFacade(inbox_coordinator, database)
    sink = _RecordingWakeSink(inbox_coordinator)
    projection = {
        "value": PresenceProjection(
            1,
            actors=frozenset({RECIPIENT}),
            online_sequence=1,
        )
    }
    wakes = RecipientWakeCoordinator(
        sink,
        router,
        inbox,
        lambda: projection["value"],
        completion_timeout=0.01,
    )
    now_ms = time.time_ns() // 1_000_000
    try:
        first = replace(
            _message("successor-first"),
            created_at_ms=now_ms,
            expires_at_ms=now_ms + 60_000,
        )
        inbox.submit(first, now_ms=now_ms, defer_direct=True)
        inbox.retry_due(now_ms=now_ms)
        wakes.submit(ActorOnlineTransition(1, RECIPIENT, 1))
        _wait(lambda: len(router.held) == 1)

        second = replace(
            _message("successor-second"),
            created_at_ms=now_ms + 1,
            expires_at_ms=now_ms + 60_000,
        )
        inbox.submit(second, now_ms=now_ms + 1, defer_direct=True)
        inbox.retry_due(now_ms=now_ms + 1)
        second_scheduled = inbox.outbox_item(second.message_id).next_attempt_ms
        projection["value"] = PresenceProjection(
            1,
            actors=frozenset({RECIPIENT}),
            online_sequence=2,
        )
        wakes.submit(ActorOnlineTransition(1, RECIPIENT, 2))
        assert wakes.pending_count == 2

        router.release_held()
        _wait(lambda: len(sink.commands) == 2)
        _wait(
            lambda: inbox.outbox_item(second.message_id).next_attempt_ms
            < second_scheduled
        )
        assert sink.commands[0].correlation_id != sink.commands[1].correlation_id
    finally:
        assert wakes.close(1.0)
        inbox.close()


class _RecipientPages:
    def __init__(self, recipients: tuple[str, ...]) -> None:
        self.recipients = recipients

    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[str, ...]:
        return tuple(item for item in self.recipients if after is None or item > after)[
            :limit
        ]


class _BlockedRecipientPages(_RecipientPages):
    def __init__(self, recipients: tuple[str, ...]) -> None:
        super().__init__(recipients)
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[str, ...]:
        self.calls += 1
        if self.calls == 2:
            assert after == EARLY_RECIPIENT
            self.blocked.set()
            assert self.release.wait(1.0)
        return super().outbox_recipient_page(after=after, limit=limit)


def test_stalled_wake_does_not_block_unrelated_presence_and_joins_late() -> None:
    session = _PresenceSession()
    directory = LivelinessDirectory(session)  # type: ignore[arg-type]
    router = CorrelatedInboxEventRouter()
    sink = _WakeSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT, OTHER_RECIPIENT)),
        directory.presence_projection,
        completion_timeout=0.01,
    )
    stop = directory.bind_actor_online(coordinator.submit)
    try:
        session.actor_event(RECIPIENT)
        _wait(lambda: len(sink.commands) == 1)
        first_correlation = sink.commands[0].correlation_id

        session.actor_event(OTHER_RECIPIENT)
        _wait(lambda: set(directory.online_actors()) == {RECIPIENT, OTHER_RECIPIENT})
        assert not coordinator.close(0.02)
        assert len(sink.commands) == 1

        sink.settle(0)
        _wait(lambda: len(sink.commands) == 2)
        assert sink.commands[0].correlation_id == first_correlation
        sink.settle(1)
        assert coordinator.close(1.0)
    finally:
        stop()
        directory.close()


def test_overflow_recovers_online_recipients_by_bounded_page() -> None:
    generation = 4
    projection = {
        "value": PresenceProjection(
            generation,
            actors=frozenset({RECIPIENT, OTHER_RECIPIENT}),
        )
    }
    router = CorrelatedInboxEventRouter()
    sink = _WakeSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT, OTHER_RECIPIENT)),
        lambda: projection["value"],
        capacity=1,
        page_size=1,
        completion_timeout=0.01,
    )
    coordinator.submit(ActorOnlineTransition(generation, RECIPIENT))
    _wait(lambda: len(sink.commands) == 1)
    coordinator.submit(ActorOnlineTransition(generation, OTHER_RECIPIENT))
    assert coordinator.pending_count == 2
    assert not coordinator.close(0.02)

    sink.settle(0)
    for _ in range(3):
        _wait(lambda: len(sink.commands) >= 2)
        latest = len(sink.commands) - 1
        if sink.commands[latest].recipient == OTHER_RECIPIENT:
            sink.settle(latest)
            break
        sink.settle(latest)
        _wait(lambda: len(sink.commands) > latest + 1)
    assert OTHER_RECIPIENT in {command.recipient for command in sink.commands}
    assert coordinator.close(1.0)


def test_same_generation_new_overflow_fences_blocked_old_page() -> None:
    actors = (EARLY_RECIPIENT, RECIPIENT, OTHER_RECIPIENT)
    projection = {
        "value": PresenceProjection(
            1,
            actors=frozenset(actors),
            online_sequence=1,
        )
    }
    router = CorrelatedInboxEventRouter()
    sink = _WakeSink(router)
    pages = _BlockedRecipientPages(actors)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        pages,
        lambda: projection["value"],
        capacity=1,
        page_size=2,
        completion_timeout=0.01,
    )
    settled: set[int] = set()
    try:
        coordinator.submit(ActorOnlineTransition(1, RECIPIENT, 1))
        _wait(lambda: len(sink.commands) == 1)
        coordinator.submit(ActorOnlineTransition(1, OTHER_RECIPIENT, 1))
        sink.settle(0)
        settled.add(0)

        _wait(lambda: len(sink.commands) == 2)
        assert sink.commands[1].recipient == EARLY_RECIPIENT
        sink.settle(1)
        settled.add(1)
        assert pages.blocked.wait(1.0)

        projection["value"] = PresenceProjection(
            1,
            actors=frozenset(actors),
            online_sequence=2,
        )
        coordinator.submit(ActorOnlineTransition(1, OTHER_RECIPIENT, 2))
        coordinator.submit(ActorOnlineTransition(1, EARLY_RECIPIENT, 2))
        pages.release.set()

        deadline = time.monotonic() + 2.0
        while sum(
            command.recipient == EARLY_RECIPIENT for command in sink.commands
        ) < 2:
            for index, _command in tuple(enumerate(sink.commands)):
                if index not in settled:
                    sink.settle(index)
                    settled.add(index)
            assert time.monotonic() < deadline
            time.sleep(0.005)
        for index, _command in tuple(enumerate(sink.commands)):
            if index not in settled:
                sink.settle(index)
                settled.add(index)
        while coordinator.pending_count:
            for index, _command in tuple(enumerate(sink.commands)):
                if index not in settled:
                    sink.settle(index)
                    settled.add(index)
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert coordinator.close(1.0)
    finally:
        pages.release.set()
        projection["value"] = PresenceProjection(2)
        for index, _command in tuple(enumerate(sink.commands)):
            if index not in settled:
                sink.settle(index)
        coordinator.close(1.0)


def test_overflow_state_stays_at_capacity_plus_one_scan_intent() -> None:
    actors = tuple(f"agent:owner:node:{index:03d}" for index in range(100))
    projection = {
        "value": PresenceProjection(1, actors=frozenset(actors))
    }
    router = CorrelatedInboxEventRouter()
    sink = _WakeSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages(actors),
        lambda: projection["value"],
        capacity=1,
        page_size=4,
        completion_timeout=0.01,
    )
    for actor in actors:
        coordinator.submit(ActorOnlineTransition(1, actor))
    _wait(lambda: len(sink.commands) == 1)
    assert coordinator.pending_count == 2

    projection["value"] = PresenceProjection(2)
    sink.settle(0)
    assert coordinator.close(1.0)


class _OverloadOnceSink(_WakeSink):
    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        return (
            PortAdmission.OVERLOADED
            if len(self.commands) == 1
            else PortAdmission.ACCEPTED
        )


def test_overload_retries_same_wake_correlation() -> None:
    generation = 1
    projection = PresenceProjection(generation, actors=frozenset({RECIPIENT}))
    router = CorrelatedInboxEventRouter()
    sink = _OverloadOnceSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT,)),
        lambda: projection,
        completion_timeout=0.01,
    )
    coordinator.submit(ActorOnlineTransition(generation, RECIPIENT))
    _wait(lambda: len(sink.commands) == 2)
    assert sink.commands[0] == sink.commands[1]
    sink.settle(1)
    assert coordinator.close(1.0)


class _UndeliveredOnceSink(_WakeSink):
    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.commands.append(command)
        if len(self.commands) == 1:
            self.router.publish(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="inbox",
                    generation=2,
                    version=0,
                    code="ACTOR_RESTARTED_UNDELIVERED",
                    detail="accepted wake never began",
                    admission=PortAdmission.CLOSING,
                )
            )
        return PortAdmission.ACCEPTED


def test_never_begun_generation_fault_retries_exact_wake() -> None:
    projection = PresenceProjection(2, actors=frozenset({RECIPIENT}))
    router = CorrelatedInboxEventRouter()
    sink = _UndeliveredOnceSink(router)
    coordinator = RecipientWakeCoordinator(
        sink,
        router,
        _RecipientPages((RECIPIENT,)),
        lambda: projection,
        completion_timeout=0.01,
    )
    coordinator.submit(ActorOnlineTransition(2, RECIPIENT))
    _wait(lambda: len(sink.commands) == 2)
    assert sink.commands[0] == sink.commands[1]
    sink.settle(1)
    assert coordinator.close(1.0)


def test_outbox_recipient_pages_are_bounded_and_stable(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    database = tmp_path / "recipient-pages.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    inbox = DeliveryCustodyFacade(coordinator, database)
    try:
        now_ms = time.time_ns() // 1_000_000
        for message_id, recipient in (
            ("page-b", RECIPIENT),
            ("page-c", OTHER_RECIPIENT),
        ):
            inbox.submit(
                replace(
                    _message(message_id, recipient),
                    created_at_ms=now_ms,
                    expires_at_ms=now_ms + 60_000,
                ),
                now_ms=now_ms,
                defer_direct=True,
            )
        first = inbox.outbox_recipient_page(limit=1)
        second = inbox.outbox_recipient_page(after=first[-1], limit=1)
        assert first + second == tuple(sorted({RECIPIENT, OTHER_RECIPIENT}))
    finally:
        inbox.close()


def test_production_presence_progresses_while_inbox_wake_is_stalled(
    tmp_path: Path, monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = DeliveryCustody._handle

    def stalled(self: DeliveryCustody, command: object) -> None:
        if isinstance(command, WakeOutboxRecipientCommand):
            entered.set()
            assert release.wait(1.0)
        original(self, command)

    monkeypatch.setattr(DeliveryCustody, "_handle", stalled)
    transport = MemoryDeliveryTransport()
    database = tmp_path / "production-wake.sqlite3"
    events = CorrelatedInboxEventRouter()
    inbox_coordinator = DeliveryCustodyCoordinator(database, transport, events)
    inbox = DeliveryCustodyFacade(inbox_coordinator, database)
    session = _PresenceSession()
    directory = LivelinessDirectory(session)  # type: ignore[arg-type]
    wakes = RecipientWakeCoordinator(
        inbox_coordinator,
        events,
        inbox,
        directory.presence_projection,
        completion_timeout=0.01,
    )
    stop = directory.bind_actor_online(wakes.submit)
    try:
        now_ms = time.time_ns() // 1_000_000
        message = replace(
            _message("production-wake"),
            created_at_ms=now_ms,
            expires_at_ms=now_ms + 60_000,
        )
        inbox.submit(message, now_ms=now_ms, defer_direct=True)
        inbox.retry_due(now_ms=now_ms)
        scheduled = inbox.outbox_item("production-wake").next_attempt_ms
        session.actor_event(RECIPIENT)
        assert entered.wait(1.0)

        session.actor_event(OTHER_RECIPIENT)
        _wait(lambda: OTHER_RECIPIENT in directory.online_actors())
        assert inbox.outbox_item("production-wake").next_attempt_ms == scheduled

        release.set()
        _wait(lambda: inbox.outbox_item("production-wake").next_attempt_ms < scheduled)
    finally:
        release.set()
        stop()
        assert wakes.close(1.0)
        directory.close()
        inbox.close()


def test_online_transition_wake_delivers_on_next_pump(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    database = tmp_path / "online-next-pump.sqlite3"
    events = CorrelatedInboxEventRouter()
    inbox_coordinator = DeliveryCustodyCoordinator(database, transport, events)
    inbox = DeliveryCustodyFacade(inbox_coordinator, database)
    session = _PresenceSession()
    directory = LivelinessDirectory(session)  # type: ignore[arg-type]
    wakes = RecipientWakeCoordinator(
        inbox_coordinator,
        events,
        inbox,
        directory.presence_projection,
        completion_timeout=0.01,
    )
    stop = directory.bind_actor_online(wakes.submit)
    try:
        now_ms = time.time_ns() // 1_000_000
        message = replace(
            _message("online-next-pump"),
            created_at_ms=now_ms,
            expires_at_ms=now_ms + 60_000,
        )
        inbox.submit(message, now_ms=now_ms, defer_direct=True)
        inbox.retry_due(now_ms=now_ms)
        scheduled = inbox.outbox_item(message.message_id).next_attempt_ms
        transport.set_online(RECIPIENT, True)
        transport.confirm_direct(message.message_id)

        session.actor_event(RECIPIENT)
        _wait(lambda: inbox.outbox_item(message.message_id).next_attempt_ms < scheduled)
        due = inbox.outbox_item(message.message_id).next_attempt_ms
        [result] = _retry_due_yielding_stale_rounds(inbox, now_ms=due)

        assert result.accepted and not result.queued
        assert transport.delivered == [message.message_id]
        assert inbox.outbox_count() == 0
    finally:
        stop()
        assert wakes.close(1.0)
        directory.close()
        inbox.close()


def test_presence_observer_exception_does_not_kill_presence_owner() -> None:
    session = _PresenceSession()
    directory = LivelinessDirectory(session)  # type: ignore[arg-type]

    def fail(_transition: ActorOnlineTransition) -> None:
        raise RuntimeError("injected observer failure")

    session.actor_event(RECIPIENT)
    _wait(lambda: directory.actor_online(RECIPIENT))
    # The replay exception is contained at binding just like later events.
    stop = directory.bind_actor_online(fail)
    try:
        session.actor_event(OTHER_RECIPIENT)
        _wait(lambda: directory.actor_online(OTHER_RECIPIENT))
    finally:
        stop()
        directory.close()
