from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import ActorState, AdmissionResult
from hyprial.kernel import PortCommandRejected
from hyprial.biz.impl.routine.contracts.ports import RoutineSourceQueryCompleted, RoutineTimerElapsedCommand
from hyprial.biz.impl.routine.execution.registry import (
    RoutineAlarmEffect,
    RoutinePacEffect,
    RoutineRegistry,
    RoutineSourceQueryEffect,
)
from hyprial.biz.impl.routine.execution.service import RoutineService, RoutineServiceError
from hyprial.biz.impl.routine.execution.source import SourceTask
from hyprial.biz.impl.routine.storage.store import RoutineCycleRow, RoutineRow, RoutineStore


VALID = """
version: 1
name: actor-routine
schedule: {interval: 60s}
source: {kind: taskwarrior, filter: "+selfdrive -blocked"}
policy:
  routes:
    - {tag: "route:self", target: "self"}
    - {tag: "route:human", escalate_to: "user:allen"}
  default: escalate
  task_template: "【{{nonce}}】{{task.description}}"
limits:
  max_in_flight: 3
  circuit_breaker: {window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


SCHEDULED = """
version: 2
mode: scheduled
name: actor-routine
schedule: {interval: 60s}
actor: agent:o:n:duty
task: Check progress and dispatch necessary workflows.
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


class Clock:
    now_ms = 0


class Alarm:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
    ) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((to, text))


class IdempotentPac:
    """The PAC dispatch seam, keyed the way ``pac_dispatch`` keys it.

    Idempotency is a property of the durable key (routine + task uuid), not
    of any bounded scan: ``newer_graphs`` exists so a test can bury the
    accepted graph under a thousand later ones and still demand exactly one.
    Graph ids are minted in PAC's real shape (``routine-<name>-<hex>``),
    because the U3 migration keys on the retired ``run-*`` shape -- a fake
    that kept minting ``run-0`` would have its own rows deleted at the next
    open and pass for the wrong reason.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.by_key: dict[tuple[str, str, int | None], str] = {}
        self.statuses: dict[str, dict[str, object]] = {}
        self.closed: list[str] = []
        self.accepted = threading.Event()
        self.release = threading.Event()
        self.block_after_accept = False
        self.newer_graphs: list[str] = []

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
        role: str = "dispatch",
        occurrence_slot_ms: int | None = None,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, object]:
        del task_text, escalate_to, timeout_seconds, sender, rearm_after_created_at_ms
        key = (routine_name, task_uuid, occurrence_slot_ms)
        graph_id = self.by_key.get(key)
        if graph_id is None:
            graph_id = f"routine-{routine_name}-{len(self.created):08x}"
            self.by_key[key] = graph_id
            self.created.append(graph_id)
            self.statuses[graph_id] = {"state": "running", "target": target}
            self.accepted.set()
            if self.block_after_accept:
                assert self.release.wait(5.0)
        return {"graphId": graph_id, "state": self.statuses[graph_id]["state"]}

    def status(self, *, graph_id: str) -> dict[str, object]:
        return self.statuses[graph_id]

    def close(self, *, graph_id: str, actor: str) -> None:
        del actor
        self.closed.append(graph_id)

    def rearm_boundary(self, *, routine_name: str) -> int:
        del routine_name
        return len(self.created)


def wait_for(predicate, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_removal_reservation_fences_cadence_before_pause(tmp_path: Path) -> None:
    clock = Clock()
    source_calls: list[str] = []

    def source(query: str) -> list[SourceTask]:
        source_calls.append(query)
        return []

    service = RoutineService(
        pac=IdempotentPac(),
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=source,
    )
    try:
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        service.reserve_remove(
            name="actor-routine", reservation_id="remove-1"
        )
        clock.now_ms = 100_000
        assert service.submit_timer().value == "accepted"
        time.sleep(0.1)
        assert source_calls == []
        assert service._projection.cycle_for_routine("actor-routine") is None
        assert service.status(name="actor-routine")["enabled"] is True
    finally:
        service.close()


def test_reserved_delete_is_fenced_to_original_registration(tmp_path: Path) -> None:
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=Alarm(),
        state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    try:
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        original = service.status(name="actor-routine")["registrationId"]
        assert isinstance(original, str) and original
        service.reserve_remove(
            name="actor-routine", reservation_id="remove-1"
        )
        with sqlite3.connect(tmp_path / "routines.sqlite3") as db:
            db.execute(
                "UPDATE routines SET registration_id=? WHERE name=?",
                ("successor-registration", "actor-routine"),
            )
        with pytest.raises(RoutineServiceError) as caught:
            service.remove(
                name="actor-routine", reservation_id="remove-1"
            )
        assert caught.value.code == "ROUTINE_COORDINATOR_CONFLICT"
        assert service.status(name="actor-routine")["registrationId"] == (
            "successor-registration"
        )
    finally:
        service.close()


def test_pre_fence_reservation_schema_backfills_registration(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    store.upsert_routine(
        name="first",
        yaml_text=VALID.replace("actor-routine", "first"),
        owner="user:test",
        enabled=True,
        next_due_ms=60_000,
        created_at_ms=0,
    )
    with sqlite3.connect(tmp_path / "routines.sqlite3") as db:
        db.execute(
            "UPDATE routines SET registration_id=? WHERE name=?",
            ("registration-1", "first"),
        )
    store.reserve_removal("first", "remove-1", "registration-1")
    store.close()
    with sqlite3.connect(tmp_path / "routines.sqlite3") as db:
        db.execute(
            "ALTER TABLE routine_removal_reservations DROP COLUMN registration_id"
        )

    reopened = RoutineStore(tmp_path / "routines.sqlite3")
    try:
        reservation = reopened.removal_reservation("first")
        assert reservation is not None
        assert reservation.reservation_id == "remove-1"
        assert reservation.registration_id == "registration-1"
    finally:
        reopened.close()


@pytest.mark.parametrize("mutation", ["pause", "remove"])
def test_blocked_source_does_not_block_mutation_and_late_completion_is_stale(
    tmp_path: Path, mutation: str
) -> None:
    clock = Clock()
    started = threading.Event()
    release = threading.Event()
    pac = IdempotentPac()

    def blocked_source(_filter: str) -> list[SourceTask]:
        started.set()
        assert release.wait(3.0)
        return [SourceTask("u1", "late", ("selfdrive", "route:self"))]

    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=blocked_source,
    )
    service.add(yaml_text=VALID, owner="agent:o:n:duty")
    clock.now_ms = 100_000
    service.submit_timer()
    assert started.wait(1.0)
    before = time.monotonic()
    getattr(service, mutation)(name="actor-routine")
    assert time.monotonic() - before < 0.5
    release.set()
    time.sleep(0.1)
    assert pac.created == []
    if mutation == "pause":
        assert service.status(name="actor-routine")["enabled"] is False
    else:
        assert service.list() == {"routines": []}
    service.close()


def test_blocked_source_does_not_starve_another_routine_effect(
    tmp_path: Path,
) -> None:
    clock = Clock()
    blocked = threading.Event()
    release = threading.Event()
    other_ran = threading.Event()

    def source(filter_text: str) -> list[SourceTask]:
        if filter_text == "+selfdrive -blocked":
            blocked.set()
            assert release.wait(3.0)
        else:
            other_ran.set()
        return []

    service = RoutineService(
        pac=IdempotentPac(), alarm=Alarm(), state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms, source_query=source,
    )
    try:
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        service.add(
            yaml_text=VALID.replace("actor-routine", "other-routine").replace(
                "+selfdrive -blocked", "+other"
            ),
            owner="agent:o:n:duty",
        )
        clock.now_ms = 100_000
        service.submit_timer()
        assert blocked.wait(1.0)
        assert other_ran.wait(1.0), "unrelated routine waited behind blocked source"
    finally:
        release.set()
        service.close()


class _StaleRefillSchedule:
    """Hold real refill/source boundaries to reproduce the publication race."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.arm = threading.Event()
        self.snapshot = threading.Event()
        self.published = threading.Event()
        self.second_refill = threading.Event()
        self.quiescent = threading.Event()
        self.release_all = threading.Event()
        self.refills_after_snapshot = 0
        self.delivered_ids: list[str] = []
        self.errors: list[str] = []

        original_source = RoutineRegistry._source_completed
        original_execute = RoutineService._execute_effect
        original_refill = RoutineService._refill_effects

        def source(registry: RoutineRegistry, event: RoutineSourceQueryCompleted) -> None:
            first = not self.published.is_set()
            if first:
                self._gate(self.snapshot, "pre-commit snapshot")
            original_source(registry, event)
            if first:
                self.published.set()
                self._gate(self.second_refill, "post-alarm refill")

        def execute(service: RoutineService, effect: object) -> object:
            self.quiescent.clear()
            if isinstance(effect, RoutineAlarmEffect):
                self.delivered_ids.append(effect.effect_id)
            result = original_execute(service, effect)
            if isinstance(effect, RoutineSourceQueryEffect):
                self.arm.set()
            return result

        def refill(service: RoutineService) -> None:
            original_refill(service)
            if self.snapshot.is_set():
                self.refills_after_snapshot += 1
                if self.refills_after_snapshot >= 2:
                    self.second_refill.set()
            if (
                self.second_refill.is_set()
                and service._projection.pending_effects() == ()
                and service._effects.empty()
            ):
                self.quiescent.set()

        monkeypatch.setattr(RoutineRegistry, "_source_completed", source)
        monkeypatch.setattr(RoutineService, "_execute_effect", execute)
        monkeypatch.setattr(RoutineService, "_refill_effects", refill)

    def _gate(self, event: threading.Event, label: str) -> None:
        deadline = time.monotonic() + 5.0
        while not event.wait(0.005):
            if self.release_all.is_set():
                return
            if time.monotonic() >= deadline:
                self.errors.append(label)
                raise AssertionError(f"gate timeout: {label}")

    def attach(self, service: RoutineService, monkeypatch: pytest.MonkeyPatch) -> None:
        original_pending = service._projection.pending_effects

        def pending(*, limit: int = 256, now_ms: int | None = None):
            rows = original_pending(limit=limit, now_ms=now_ms)
            if self.arm.is_set() and not self.snapshot.is_set():
                self.snapshot.set()
                self._gate(self.published, "publication after snapshot")
            return rows

        monkeypatch.setattr(service._projection, "pending_effects", pending)

    def settled(self, service: RoutineService) -> bool:
        with service._effect_ids_lock:
            no_reserved_effects = not service._effect_ids
        return (
            self.second_refill.is_set()
            and self.quiescent.is_set()
            and no_reserved_effects
            and service._projection.pending_effects() == ()
            and service._effects.empty()
        )


@pytest.mark.parametrize("task_count", [1, 20], ids=["one-task", "twenty-task"])
def test_stale_refill_does_not_duplicate_published_alarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_count: int
) -> None:
    schedule = _StaleRefillSchedule(monkeypatch)
    clock = Clock()
    alarm = Alarm(delay=0.002)
    tasks = [
        SourceTask(f"u{index}", f"human-{index}", ("selfdrive", "route:human"))
        for index in range(task_count)
    ]
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=alarm,
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: tasks,
        mailbox_capacity=1 if task_count == 20 else 128,
    )
    try:
        schedule.attach(service, monkeypatch)
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        clock.now_ms = 100_000
        assert service.submit_timer().value == "accepted"
        wait_for(lambda: schedule.settled(service))
        status = service.status(name="actor-routine")
        expected = task_count + (1 if task_count >= 5 else 0)
        assert not schedule.errors
        assert len(alarm.calls) == expected
        assert len(schedule.delivered_ids) == expected
        assert len(set(schedule.delivered_ids)) == expected
        assert service._projection.pending_effects() == ()
        assert status["enabled"] == (task_count < 5)
    finally:
        schedule.release_all.set()
        service.close()


def test_effect_queue_full_replays_every_durable_alarm(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    tasks = [
        SourceTask(f"u{index}", f"human-{index}", ("selfdrive", "route:human"))
        for index in range(20)
    ]
    alarm = Alarm(delay=0.002)
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=alarm,
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: tasks,
        mailbox_capacity=1,
    )
    request.addfinalizer(service.close)
    store = RoutineStore(tmp_path / "routines.sqlite3")
    request.addfinalizer(store.close)
    delivered_ids: list[str] = []
    original_execute = service._execute_effect

    def execute(effect: object) -> object:
        if isinstance(effect, RoutineAlarmEffect):
            delivered_ids.append(effect.effect_id)
        return original_execute(effect)

    monkeypatch.setattr(service, "_execute_effect", execute)
    service.add(yaml_text=VALID, owner="agent:o:n:duty")
    clock.now_ms = 100_000
    assert service.submit_timer().value == "accepted"
    # 20 route alarms plus one breaker alarm; the in-memory queue holds only 2.
    def settled() -> bool:
        with service._effect_ids_lock:
            no_reserved_effects = not service._effect_ids
        return (
            len(alarm.calls) >= 21
            and no_reserved_effects
            and store.pending_effects() == ()
            and service._effects.empty()
        )

    wait_for(settled)
    assert len(alarm.calls) == 21
    assert len(delivered_ids) == 21
    assert len(set(delivered_ids)) == 21
    assert sum("circuit breaker" not in text for _, text in alarm.calls) == 20
    assert sum("circuit breaker" in text for _, text in alarm.calls) == 1
    assert service.status(name="actor-routine")["enabled"] is False


@pytest.mark.parametrize(
    ("permanent", "retained_for_retry"),
    [
        pytest.param(True, False, id="permanent-refusal"),
        pytest.param(False, True, id="transient-timeout"),
    ],
)
def test_start_failure_alarm_retries_only_transient_delivery_failures(
    tmp_path: Path,
    permanent: bool,
    retained_for_retry: bool,
) -> None:
    class StartFailurePac(IdempotentPac):
        def start_idempotent(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("synthetic start refusal")

    class DeliveryAlarm:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def escalate(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(kwargs)
            return SimpleNamespace(status="failed", permanent=permanent)

    clock = Clock()
    alarm = DeliveryAlarm()
    service = RoutineService(
        pac=StartFailurePac(),
        alarm=alarm,
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [
            SourceTask("u1", "human task", ("route:self",))
        ],
    )
    try:
        service.add(yaml_text=VALID, owner="agent:o:n:duty")
        clock.now_ms = 100_000
        assert service.submit_timer().value == "accepted"
        wait_for(lambda: len(alarm.calls) == 1)

        def alarm_completion_settled() -> bool:
            rows = [
                row
                for row in service._projection.pending_effects()
                if row.payload.get("kind") == "RoutineAlarmEffect"
            ]
            return not rows or rows[0].attempts >= 1

        wait_for(alarm_completion_settled)
        pending = [
            row
            for row in service._projection.pending_effects()
            if row.payload.get("kind") == "RoutineAlarmEffect"
        ]
        assert bool(pending) is retained_for_retry
        if pending:
            assert pending[0].attempts == 1
    finally:
        service.close()


def test_completion_overload_retries_until_actor_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [],
    )
    service.add(yaml_text=VALID, owner="agent:o:n:duty")
    original = service._runtime.tell
    overloads = {"remaining": 3}

    def flaky_tell(handle, command):
        if isinstance(command, RoutineSourceQueryCompleted) and overloads["remaining"]:
            overloads["remaining"] -= 1
            return AdmissionResult.OVERLOADED
        return original(handle, command)

    monkeypatch.setattr(service._runtime, "tell", flaky_tell)
    clock.now_ms = 100_000
    service.submit_timer()
    wait_for(lambda: overloads["remaining"] == 0)
    assert overloads["remaining"] == 0
    service.close()


def test_remote_start_accepted_completion_lost_restart_is_still_exactly_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "routines.sqlite3"
    pac = IdempotentPac()
    pac.block_after_accept = True
    effect = RoutinePacEffect(
        effect_id="routine-io-stable",
        parent_correlation_id="cycle-stable",
        generation=1,
        version=1,
        routine_name="actor-routine",
        task_uuid="u1",
        operation="pac.start",
        target="agent:o:n:duty",
        task_text="do it",
        escalate_to="user:allen",
        timeout_seconds=60.0,
        sender="agent:o:n:duty",
    )
    seed = RoutineStore(database)
    seed.apply(
        routine=RoutineRow(
            "actor-routine", VALID, "agent:o:n:duty", True, 999_999, 0, "[]", 0, 1
        ),
        cycle=RoutineCycleRow(
            "cycle-stable", "actor-routine", 1, 1, pending_start=1
        ),
        add_effects=(RoutineRegistry.effect_row(effect),),
    )
    seed.close()

    first = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    assert pac.accepted.wait(1.0)
    started_close = time.monotonic()
    first.close()
    assert time.monotonic() - started_close < 2.5
    # The accepted run is now older than >1000 unrelated runs. No bounded
    # list scan participates in idempotency: external_ref is the authority.
    pac.newer_graphs.extend(f"newer-{index}" for index in range(1001))
    pac.release.set()
    first._effect_thread.join(timeout=1.0)
    pac.block_after_accept = False

    second = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    wait_for(lambda: bool(second.status(name="actor-routine")["inFlight"]))
    [graph_id] = pac.created
    assert second.status(name="actor-routine")["inFlight"] == [
        {"taskUuid": "u1", "runId": graph_id, "target": "agent:o:n:duty"}
    ]
    second.close()


def test_actor_restart_closes_old_writer_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    original_close = RoutineStore.close

    def tracking_close(store: RoutineStore) -> None:
        closed.append(id(store))
        original_close(store)

    monkeypatch.setattr(RoutineStore, "close", tracking_close)
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=Alarm(),
        state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    original_writer = id(service._writer_store)
    assert service.submit(object()).value == "accepted"
    wait_for(
        lambda: service._runtime.snapshot(service._handle).state is ActorState.RUNNING
        and service._runtime.snapshot(service._handle).generation > 1
    )
    assert original_writer in closed
    assert service.add(yaml_text=VALID, owner="agent:o:n:duty")["name"] == "actor-routine"
    service.close()


def test_timer_sequence_is_generation_fenced_not_store_version_fenced(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    service = RoutineService(
        pac=IdempotentPac(),
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: 0,
        source_query=lambda _filter: [],
        event_sink=events.append,
    )
    service.add(yaml_text=VALID, owner="agent:o:n:duty")
    # Mutations advance the Store epoch independently of timer sequence.
    service.pause(name="actor-routine")
    service.resume(name="actor-routine")
    generation = service._runtime.snapshot(service._handle).generation
    assert service.submit(RoutineTimerElapsedCommand("timer-1", generation, 1, 0)).value == "accepted"
    wait_for(lambda: any(getattr(item, "correlation_id", None) == "timer-1" for item in events))
    assert not any(
        isinstance(item, PortCommandRejected) and item.correlation_id == "timer-1"
        for item in events
    )
    assert service.submit(RoutineTimerElapsedCommand("timer-stale", generation, 1, 0)).value == "accepted"
    wait_for(
        lambda: any(getattr(item, "correlation_id", None) == "timer-stale" for item in events)
    )
    rejected = next(
        item
        for item in events
        if isinstance(item, PortCommandRejected) and item.correlation_id == "timer-stale"
    )
    assert rejected.code == "ROUTINE_STALE_TIMER"
    service.close()


def test_busy_tick_does_not_orphan_the_in_flight_cycle(tmp_path: Path) -> None:
    """A cycle that outlives tick(s) must still settle on completion.

    Field report (infra-ops, 0.5.5.2): a scheduled routine whose cycle was
    still running took the ``cycle-busy`` branch, which bumped
    ``routine.version``.  The completion fence then required
    ``row.version == cycle.version``, so the completion was dropped, the
    cycle was orphaned, and every later slot reported ``skipped cycle-busy``:
    ``routine.version`` 32461 -> 32469 while the cycle stayed at 32461.
    """

    clock = Clock()
    pac = IdempotentPac()
    # Hold the pac.start completion inside the cycle window: the cycle exists
    # from timer admission until that completion settles, so this is exactly
    # the window a cadence tick must not invalidate.
    pac.block_after_accept = True
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: pytest.fail("scheduled mode queries no source"),
    )
    try:
        service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
        clock.now_ms = 60_000
        service.submit_timer()
        assert pac.accepted.wait(1.0)
        cycle = service._projection.cycle_for_routine("actor-routine")
        assert cycle is not None

        for tick in (120_000, 180_000):
            clock.now_ms = tick
            service.submit_timer()
            wait_for(
                lambda tick=tick: service._projection.get_routine(
                    "actor-routine"
                ).next_due_ms
                == tick + 60_000
            )
            # The busy branch moved the schedule cursor only; the same cycle
            # is still the one in flight.
            still = service._projection.cycle_for_routine("actor-routine")
            assert still is not None
            assert still.correlation_id == cycle.correlation_id

        pac.release.set()
        # The in-flight cycle settles instead of being dropped as stale.
        wait_for(
            lambda: service._projection.cycle_for_routine("actor-routine") is None
        )
        wait_for(lambda: bool(service.status(name="actor-routine")["inFlight"]))
        [graph_id] = pac.created
        pac.statuses[graph_id]["state"] = "done"

        # ... and the next slot runs.
        clock.now_ms = 240_000
        service.submit_timer()
        wait_for(lambda: len(pac.created) == 2)
        assert service.status(name="actor-routine")["inFlight"] == [
            {
                "taskUuid": "scheduled:240000",
                "runId": pac.created[1],
                "target": "agent:o:n:duty",
            }
        ]
    finally:
        pac.release.set()
        service.close()
