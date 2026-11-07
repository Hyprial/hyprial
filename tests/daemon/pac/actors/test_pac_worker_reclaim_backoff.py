"""Cleanup retries back off and are bounded per fallback sweep.

Every retry of a present worker submits a fresh lifecycle deactivate that
may block an actor worker for up to the lifecycle budget.  When the
lifecycle plane degrades, an unbounded sweep would occupy all four actor
workers every cadence.  A failed intent therefore waits an exponential
delay keyed by its attempts (capped), and one sweep admits at most a fixed
number of due retries, oldest first.  First evaluations (attempts 0) are
not throttled.  The sweep runs on the real PacActorService over SQLite.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import (
    CLEANUP_RETRIES_PER_SWEEP,
    CLEANUP_RETRY_BASE_MS,
    CLEANUP_RETRY_CAP_MS,
    cleanup_retry_delay_ms,
)
from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime, PacActorService
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from tests.daemon.pac.events.test_pac_scan_actor_boundary import _insert_actor_case


def test_retry_delay_doubles_per_attempt_up_to_the_cap() -> None:
    assert cleanup_retry_delay_ms(0) == 0
    assert cleanup_retry_delay_ms(1) == CLEANUP_RETRY_BASE_MS
    assert cleanup_retry_delay_ms(2) == 2 * CLEANUP_RETRY_BASE_MS
    assert cleanup_retry_delay_ms(3) == 4 * CLEANUP_RETRY_BASE_MS
    assert cleanup_retry_delay_ms(64) == CLEANUP_RETRY_CAP_MS
    assert CLEANUP_RETRY_CAP_MS == 6 * 3600 * 1000
    assert CLEANUP_RETRIES_PER_SWEEP == 2


def _set_attempts(store: PacGraphStore, graph_id: str, attempts: int, updated_at: int) -> None:
    with store._db:
        store._db.execute(
            "UPDATE workflow_worker_cleanup_intents SET attempts=?,updated_at=? "
            "WHERE graph_id=?",
            (attempts, updated_at, graph_id),
        )


@pytest.fixture
def sweep(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))

    def admitted(busy: tuple[tuple[str, str], ...] = ()) -> list[tuple[str, str]]:
        service = PacActorService(
            state_dir=state,
            reference_root=tmp_path,
            runtime=DaemonActorRuntime(SimpleNamespace()),
            sender=NullSender(),
            daemon_epoch="backoff",
            logger=lambda *args, **kwargs: None,
            workers=0,
        )
        try:
            service._active_actors.update(busy)
            service._scan_and_admit(force_actors=True)
            return list(service._actor_queue.queue)
        finally:
            assert service.close()

    try:
        yield store, admitted
    finally:
        store.close()


def test_failed_intent_waits_for_its_backoff(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    fresh = _insert_actor_case(
        store, "fresh-failure", graph_state="closed", desired="down", op="pending",
        receipt=True, cleanup="attention",
    )
    _set_attempts(store, fresh[0], 2, now)
    due = _insert_actor_case(
        store, "due-failure", graph_state="closed", desired="down", op="pending",
        receipt=True, cleanup="attention",
    )
    _set_attempts(store, due[0], 2, now - 2 * CLEANUP_RETRY_BASE_MS - 1000)
    first = _insert_actor_case(
        store, "first-evaluation", graph_state="closed", desired="down",
        op="pending", receipt=True, cleanup="pending",
    )

    jobs = admitted()

    assert due in jobs
    assert first in jobs
    assert fresh not in jobs


def test_one_sweep_admits_a_bounded_number_of_retries_oldest_first(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    retries = []
    for index in range(CLEANUP_RETRIES_PER_SWEEP + 3):
        case = _insert_actor_case(
            store, f"retry-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="attention",
        )
        _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS + index)
        retries.append(case)
    first = [
        _insert_actor_case(
            store, f"first-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="pending",
        )
        for index in range(CLEANUP_RETRIES_PER_SWEEP + 3)
    ]

    jobs = admitted()

    assert [job for job in jobs if job in retries] == retries[:CLEANUP_RETRIES_PER_SWEEP]
    assert all(job in jobs for job in first)


def test_a_pending_intent_with_failed_attempts_also_backs_off(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    waiting = _insert_actor_case(
        store, "pending-retry", graph_state="closed", desired="down",
        op="pending", receipt=True, cleanup="pending",
    )
    _set_attempts(store, waiting[0], 1, now)

    assert waiting not in admitted()


def test_retry_admission_follows_age_not_name(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    cases = []
    for index in range(CLEANUP_RETRIES_PER_SWEEP + 2):
        case = _insert_actor_case(
            store, f"age-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="attention",
        )
        _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS - index)
        cases.append(case)
    oldest = set(cases[-CLEANUP_RETRIES_PER_SWEEP:])

    jobs = admitted()

    assert {job for job in jobs if job in cases} == oldest


def test_retries_already_in_flight_count_against_the_sweep_cap(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    cases = []
    for index in range(CLEANUP_RETRIES_PER_SWEEP + 2):
        case = _insert_actor_case(
            store, f"busy-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="attention",
        )
        _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS + index)
        cases.append(case)
    in_flight = tuple(cases[-2:])
    for graph_id, _node in in_flight:
        _set_attempts(store, graph_id, 1, now)

    jobs = admitted(busy=in_flight)

    fresh = [job for job in jobs if job in cases]
    assert fresh == cases[: CLEANUP_RETRIES_PER_SWEEP - len(in_flight)]


def test_in_flight_retries_do_not_also_take_a_fresh_slot(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    cases = []
    for index in range(CLEANUP_RETRIES_PER_SWEEP + 2):
        case = _insert_actor_case(
            store, f"oldest-busy-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="attention",
        )
        _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS + index)
        cases.append(case)
    in_flight = tuple(cases[:1])

    jobs = admitted(busy=in_flight)

    fresh = [job for job in jobs if job in cases]
    assert fresh == cases[1:CLEANUP_RETRIES_PER_SWEEP]


def test_a_persistently_failing_reconcile_backs_off_and_frees_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose reconcile keeps raising must advance its own backoff.

    Before the fix the authority only logged ``pac.actor.reconcile_failed``;
    the intent kept its old ``updated_at``, stayed the oldest due retry on
    every 30 s sweep, and four such rows held the cap so a healthy due
    retry was never admitted.  Rounds run on a simulated sweep clock; the
    failure itself is injected into the real authority's coordinator.
    """

    import hyprial.daemon.impl.pac.graphs.authority.generation as generation
    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from tests.daemon.pac.actors.test_pac_worker_reclaim import _Runtime, _wait_for

    state = tmp_path / "state"
    authority = PacGraphAuthority(default_database_path(state), NullSender())
    authority.attach_reconciler(_Runtime(), tmp_path, "epoch")
    service = GraphWorkflowService(
        state_dir=state, owner="test", machine="machine", sender=NullSender(),
        graph_authority=authority, start_thread=True,
    )
    graphs = []
    try:
        for index in range(CLEANUP_RETRIES_PER_SWEEP + 1):
            graph_id = service.start(
                yaml_text="version: 2\nname: fail\nnodes:\n  - id: work\n    task: work\n",
                sender="user:test",
                operation_key=f"persistent-{index}",
            )["graphId"]
            service.cancel(run_id=graph_id, actor="user:test")
            graphs.append(graph_id)
    finally:
        assert service.close()
    failing, healthy = set(graphs[:-1]), graphs[-1]
    now = time.time_ns() // 1_000_000
    store = PacGraphStore(default_database_path(state))
    try:
        for offset, graph_id in enumerate(graphs):
            _set_attempts(store, graph_id, 1, now - 3 * CLEANUP_RETRY_BASE_MS + offset)
        nodes = {
            graph_id: next(n.node_id for n in store.nodes(graph_id) if n.kind == "actor")
            for graph_id in graphs
        }
    finally:
        store.close()

    calls: dict[str, int] = {graph_id: 0 for graph_id in failing}
    real = generation.ActorCoordinator.reconcile

    def failing_reconcile(self, graph_id, node_id):
        if graph_id in failing:
            calls[graph_id] += 1
            raise RuntimeError("persistent reconcile failure")
        return real(self, graph_id, node_id)

    monkeypatch.setattr(generation.ActorCoordinator, "reconcile", failing_reconcile)
    sweeps = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=DaemonActorRuntime(SimpleNamespace()),
        sender=NullSender(),
        daemon_epoch="persistent",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    clock = [now]
    sweeps._clock_ms = lambda: clock[0]
    admissions = {graph_id: 0 for graph_id in graphs}
    try:
        for _round in range(20):
            sweeps._scan_and_admit(force_actors=True)
            jobs = list(sweeps._actor_queue.queue)
            while not sweeps._actor_queue.empty():
                sweeps._actor_queue.get_nowait()
            for graph_id, node_id in jobs:
                if graph_id not in admissions:
                    continue
                admissions[graph_id] += 1
                if graph_id in failing:
                    before = calls[graph_id]
                    assert authority.reconcile_actor(graph_id, node_id).value == "accepted"
                    _wait_for(lambda g=graph_id, b=before: calls[g] > b)
                    time.sleep(0.05)
            with sweeps._lock:
                sweeps._active_actors.clear()
            clock[0] += 30_000
    finally:
        assert sweeps.close()
        assert authority.close(3.0)

    assert admissions[healthy] >= 1, admissions
    assert all(admissions[graph_id] <= 2 for graph_id in failing), admissions
    store = PacGraphStore(default_database_path(state), read_only=True)
    try:
        for graph_id in failing:
            intent = store.workflow_worker_cleanup_intent(graph_id, nodes[graph_id])
            assert intent["attempts"] >= 2, intent
    finally:
        store.close()


def test_a_reconcile_that_raises_before_any_effect_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.pac.graphs.authority.generation as generation
    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from tests.daemon.pac.actors.test_pac_worker_reclaim import _Runtime, _wait_for

    state = tmp_path / "state"
    authority = PacGraphAuthority(default_database_path(state), NullSender())
    authority.attach_reconciler(_Runtime(), tmp_path, "epoch")
    service = GraphWorkflowService(
        state_dir=state, owner="test", machine="machine", sender=NullSender(),
        graph_authority=authority, start_thread=True,
    )
    try:
        graph_id = service.start(
            yaml_text="version: 2\nname: early\nnodes:\n  - id: work\n    task: work\n",
            sender="user:test",
            operation_key="early-failure",
        )["graphId"]
        service.cancel(run_id=graph_id, actor="user:test")
    finally:
        assert service.close()
    store = PacGraphStore(default_database_path(state))
    try:
        _set_attempts(store, graph_id, 1, 0)
        node_id = next(n.node_id for n in store.nodes(graph_id) if n.kind == "actor")
    finally:
        store.close()

    def early_failure(self, command):
        raise RuntimeError("reconcile failed before any effect")

    monkeypatch.setattr(generation._Generation, "_reconcile_actor", early_failure)

    def attempts() -> int:
        read = PacGraphStore(default_database_path(state), read_only=True)
        try:
            return int(read.workflow_worker_cleanup_intent(graph_id, node_id)["attempts"])
        finally:
            read.close()

    try:
        assert authority.reconcile_actor(graph_id, node_id).value == "accepted"
        _wait_for(lambda: attempts() == 2)
    finally:
        assert authority.close(3.0)


class _BlockingObserve:
    """Runtime port whose observe holds an authority effect thread."""

    def __init__(self) -> None:
        import threading

        self.release = threading.Event()

    def observe(self, _actor_name: str):
        from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation

        self.release.wait(10)
        return RuntimeObservation(False)

    def start(self, *_args, **_kwargs):
        raise AssertionError("cleanup never starts a worker")

    def stop(self, *_args, **_kwargs):
        raise AssertionError("absent workers are never stopped")


def test_the_cap_counts_retries_running_inside_the_graph_authority(
    tmp_path: Path,
) -> None:
    """In production the actor service only hands a job to the authority.

    The actor worker returns at once and forgets the job, while the observe
    or stop runs on one of the authority's effect threads.  The cap must
    count those authority-side effects, or a degraded lifecycle plane fills
    the whole effect pool with cleanup retries.
    """

    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    state = tmp_path / "state"
    runtime = _BlockingObserve()
    authority = PacGraphAuthority(default_database_path(state), NullSender())
    service = GraphWorkflowService(
        state_dir=state, owner="test", machine="machine", sender=NullSender(),
        graph_authority=authority, start_thread=True,
    )
    graphs = []
    try:
        for index in range(CLEANUP_RETRIES_PER_SWEEP + 2):
            graph_id = service.start(
                yaml_text="version: 2\nname: cap\nnodes:\n  - id: work\n    task: work\n",
                sender="user:test",
                operation_key=f"authority-cap-{index}",
            )["graphId"]
            service.cancel(run_id=graph_id, actor="user:test")
            graphs.append(graph_id)
    finally:
        assert service.close()
    now = time.time_ns() // 1_000_000
    store = PacGraphStore(default_database_path(state))
    try:
        for offset, graph_id in enumerate(graphs):
            _set_attempts(store, graph_id, 1, now - 3 * CLEANUP_RETRY_BASE_MS + offset)
    finally:
        store.close()

    sweeps = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=runtime,
        sender=NullSender(),
        daemon_epoch="authority-cap",
        logger=lambda *args, **kwargs: None,
        workers=0,
        graph_authority=authority,
    )
    clock = [now]
    sweeps._clock_ms = lambda: clock[0]

    def sweep() -> list[tuple[str, str]]:
        sweeps._scan_and_admit(force_actors=True)
        jobs = [job for job in sweeps._actor_queue.queue if job[0] in graphs]
        while not sweeps._actor_queue.empty():
            sweeps._actor_queue.get_nowait()
        with sweeps._lock:
            sweeps._active_actors.clear()
        return jobs

    try:
        first = sweep()
        assert 0 < len(first) <= CLEANUP_RETRIES_PER_SWEEP
        for graph_id, node_id in first:
            assert authority.reconcile_actor(graph_id, node_id).value == "accepted"
        deadline = time.monotonic() + 3
        while authority.actor_effect_stats()["inFlight"] < len(first):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        waiting = [graph_id for graph_id in graphs if graph_id not in {g for g, _n in first}]
        store = PacGraphStore(default_database_path(state))
        try:
            for graph_id in waiting:
                _set_attempts(store, graph_id, 1, now - 20 * CLEANUP_RETRY_BASE_MS)
        finally:
            store.close()
        clock[0] += 30_000
        second = sweep()

        in_flight = len(first)
        assert in_flight + len([j for j in second if j not in first]) <= (
            CLEANUP_RETRIES_PER_SWEEP
        ), (first, second)
    finally:
        runtime.release.set()
        assert sweeps.close()
        assert authority.close(3.0)


def test_non_retry_work_in_flight_takes_no_retry_slot(sweep) -> None:
    store, admitted = sweep
    now = time.time_ns() // 1_000_000
    retries = []
    for index in range(CLEANUP_RETRIES_PER_SWEEP + 1):
        case = _insert_actor_case(
            store, f"slot-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="attention",
        )
        _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS + index)
        retries.append(case)
    other_work = tuple(
        _insert_actor_case(
            store, f"other-{index}", graph_state="closed", desired="down",
            op="pending", receipt=True, cleanup="pending",
        )
        for index in range(CLEANUP_RETRIES_PER_SWEEP + 1)
    )

    jobs = admitted(busy=other_work)

    assert [job for job in jobs if job in retries] == retries[:CLEANUP_RETRIES_PER_SWEEP]


def test_the_cap_counts_authority_effects_while_the_actor_service_is_busy(
    tmp_path: Path,
) -> None:
    """Kills review mutation Y3: merge authority keys even when the actor
    service still holds unrelated in-flight work."""

    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))
    now = time.time_ns() // 1_000_000
    try:
        retries = []
        for index in range(CLEANUP_RETRIES_PER_SWEEP + 2):
            case = _insert_actor_case(
                store, f"mixed-{index}", graph_state="closed", desired="down",
                op="pending", receipt=True, cleanup="attention",
            )
            _set_attempts(store, case[0], 1, now - 10 * CLEANUP_RETRY_BASE_MS + index)
            retries.append(case)
        unrelated = _insert_actor_case(
            store, "unrelated-open", graph_state="open", desired="up", op="pending",
        )
    finally:
        store.close()
    running = frozenset(retries[-CLEANUP_RETRIES_PER_SWEEP:])

    class _Authority:
        def actor_effect_keys(self):
            return running

    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=DaemonActorRuntime(SimpleNamespace()),
        sender=NullSender(),
        daemon_epoch="mixed",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    service._graph_authority = _Authority()
    try:
        service._active_actors.add(unrelated)
        service._scan_and_admit(force_actors=True)
        jobs = list(service._actor_queue.queue)
    finally:
        assert service.close()
    assert [job for job in jobs if job in retries] == []
