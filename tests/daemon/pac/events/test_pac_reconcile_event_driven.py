"""Event-driven PAC actor reconcile: targeted wakes, one cheap fallback sweep.

The 2026-10-13 storm was caused by ``submit_tick`` re-deriving every actor of
every open graph once per second.  These tests pin the two halves of the fix:

* a committed graph event wakes exactly the affected actor with **no**
  maintenance tick (RED before the event hook);
* with no event and the fallback period not elapsed, **nothing** is enqueued
  (RED before the fallback gate), yet a committed in-transition row still
  progresses.

The queue-policy probes in ``tests/daemon/pac/clock`` force the sweep on every
tick; these tests drive the real cadence seam instead.

Decision (0.5.5.3, release run 42891 shard 1): the without-maintenance-tick
test read the activation row right after the worker start was observed, but
the event flow commits ``op='done'`` after ``runtime.start`` returns, so an
immediate read raced the settlement and failed on CI as ``('up', 'pending')``.
The assertion now awaits the settled row through the file's bounded
``_wait_until`` and still demands exactly ``('up', 'done')``; the sweep
forbidden on the event path stays forbidden.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import LaunchSpec, RuntimeObservation
from hyprial.daemon.impl.pac.actors.daemon import PacActorService
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.edits import (
    add_node,
    close_graph,
    create_graph,
)
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import (
    PacGraphStore,
    default_database_path,
)

OWNER = "user:event-owner"


class _FakeRuntime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[str] = []
        self.stops: list[str] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.starts.append(actor_name)
        observation = RuntimeObservation(
            True, identity_marker, launch.harness, operation_id
        )
        self.running[actor_name] = observation
        return observation

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.stops.append(actor_name)
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", "event-owner")
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "daemon.sock"))
    return state


def _seed_graph(store: PacGraphStore, state: Path, name: str) -> tuple[str, str]:
    """One actor node, no predecessors, graph left inactive."""

    graph_id = create_graph(store, name=name, created_by=OWNER)["graphId"]
    node_id = "worker"
    add_node(
        store,
        state,
        graph_id=graph_id,
        node_id=node_id,
        owner=OWNER,
        brief_ref=f"brief#{name}",
        kind="actor",
        actor_name=f"{name}-worker",
        launch_ref="launch/worker.yaml",
        expect_version=1,
    )
    return graph_id, node_id


def _write_launch(reference_root: Path) -> None:
    launch = reference_root / "launch" / "worker.yaml"
    launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text("harness: codex\nargs: [--headless]\n", encoding="utf-8")


def _wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "event-driven reconcile did not reach the expected state"


def test_event_reconciles_actor_without_any_maintenance_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before the event hook: only the tick supplied actors."""

    state = _state(tmp_path, monkeypatch)
    _write_launch(tmp_path)
    store = PacGraphStore(default_database_path(state))
    authority = PacGraphAuthority(default_database_path(state), NullSender())
    runtime = _FakeRuntime()
    service: PacActorService | None = None
    swept: list[int] = []
    try:
        graph_id, node_id = _seed_graph(store, state, "event-wake")

        service = PacActorService(
            state_dir=state,
            reference_root=tmp_path,
            runtime=runtime,
            sender=NullSender(),
            daemon_epoch="event-epoch",
            logger=lambda *args, **kwargs: None,
            workers=1,
            graph_authority=authority,
        )
        # Prove the event path, not the sweep: any maintenance scan is a fault.
        def forbidden_scan(*args: Any, **kwargs: Any) -> None:
            swept.append(1)
            raise AssertionError("maintenance sweep ran on the event path")

        service._scan_and_admit = forbidden_scan

        # The ONLY supply: graph activation is a committed event.
        authority.activate_graph(graph_id, actor=OWNER)
        _wait_until(lambda: runtime.starts == ["event-wake-worker"])
        assert swept == []
        assert runtime.starts == ["event-wake-worker"]
        assert runtime.observe("event-wake-worker").present is True

        def settled_activation() -> tuple[str, str]:
            row = store._db.execute(
                "SELECT desired,op FROM actor_activations "
                "WHERE graph_id=? AND node_id=?",
                (graph_id, node_id),
            ).fetchone()
            return tuple(row)

        _wait_until(lambda: settled_activation() == ("up", "done"))
        assert settled_activation() == ("up", "done")
    finally:
        if service is not None:
            assert service.close(3.0)
        assert authority.close(3.0)
        store.close()


def test_no_events_and_fallback_not_elapsed_enqueue_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before the fallback gate: every tick re-derived every actor.

    The graph carries a committed in-transition activation, so an ungated
    sweep *would* enqueue it.  With the fallback period not elapsed and no
    event, the cadence must enqueue nothing (⒜ idle rate ≈ 0).
    """

    state = _state(tmp_path, monkeypatch)
    store = PacGraphStore(default_database_path(state))
    events: list[tuple[Any, ...]] = []
    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=_FakeRuntime(),
        sender=NullSender(),
        daemon_epoch="idle-epoch",
        logger=lambda *args, **fields: events.append(args + (fields,)),
        workers=0,
    )
    try:
        graph_id, node_id = _seed_graph(store, state, "idle-graph")
        db = store.write()
        try:
            db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
                "identity_marker,updated_at) VALUES (?,?,0,'up','pending',?,?,?,0)",
                (
                    graph_id,
                    node_id,
                    "effect:idle",
                    "operation:idle",
                    f"pac:{graph_id}:{node_id}:1:idle",
                ),
            )
            db.execute(
                "UPDATE graphs SET activated_at=1 WHERE graph_id=?", (graph_id,)
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

        service._actor_fallback_due = time.monotonic() + 10_000.0
        service._actor_running_due = time.monotonic() + 10_000.0
        for _ in range(5):
            service._scan_and_admit()
        assert list(service._actor_queue.queue) == [], (
            "the fallback gate enqueued an actor with no event and no due sweep"
        )
        assert list(service._active_actors) == []

        # No silent work loss: when the fallback *is* due, the committed
        # in-transition row is supplied.
        service._actor_fallback_due = 0.0
        service._actor_running_due = 0.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == [(graph_id, node_id)]
    finally:
        assert service.close()
        store.close()


def test_closed_graph_cleanup_progresses_through_the_fallback_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No silent work loss: an actionable closed-graph row is still swept."""

    state = _state(tmp_path, monkeypatch)
    store = PacGraphStore(default_database_path(state))
    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=_FakeRuntime(),
        sender=NullSender(),
        daemon_epoch="closed-epoch",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    try:
        graph_id, node_id = _seed_graph(store, state, "closed-graph")
        db = store.write()
        db.execute(
            "INSERT INTO actor_activations "
            "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
            "identity_marker,updated_at) VALUES (?,?,0,'up','done',?,?,?,0)",
            (
                graph_id,
                node_id,
                "effect:closed",
                "operation:closed",
                f"pac:{graph_id}:{node_id}:1:closed",
            ),
        )
        db.commit()
        close_graph(store, graph_id, actor=OWNER)

        service._actor_fallback_due = 0.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == [(graph_id, node_id)]
    finally:
        assert service.close()
        store.close()


def test_running_actor_is_re_observed_on_the_idle_tier(
    tmp_path: Path,
) -> None:
    """A running actor has no event for a silent harness death.

    The harness-outcome write is keyed by ``message_id``, so an idle death
    emits nothing; the running tier is the only periodic observer for it.
    The settled re-poll must therefore stay -- on the cheap tier, not on the
    every-tick supply that saturated the mailbox on 2026-10-13.
    """

    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))
    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=_FakeRuntime(),
        sender=NullSender(),
        daemon_epoch="running-tier-epoch",
        logger=lambda *args, **fields: None,
        workers=0,
    )
    try:
        graph_id, node_id = _seed_graph(store, state, "running-tier")
        db = store.write()
        try:
            db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
                "identity_marker,updated_at) VALUES (?,?,0,'up','done',?,?,?,0)",
                (
                    graph_id,
                    node_id,
                    "effect:running",
                    "operation:running",
                    f"pac:{graph_id}:{node_id}:1:running",
                ),
            )
            db.execute(
                "UPDATE graphs SET activated_at=1 WHERE graph_id=?", (graph_id,)
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

        # No event and neither tier due: a settled-running actor is not
        # re-supplied (this is the cheap idle state).
        service._actor_fallback_due = time.monotonic() + 10_000.0
        service._actor_running_due = time.monotonic() + 10_000.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == []

        # The dirty-set tier alone must not re-supply it...
        service._actor_fallback_due = 0.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == []

        # ...but the running tier does, so a silent harness death is bounded
        # by PAC_RECONCILE_RUNNING_REFRESH_SECONDS.
        service._actor_fallback_due = time.monotonic() + 10_000.0
        service._actor_running_due = 0.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == [(graph_id, node_id)]
    finally:
        assert service.close()
        store.close()


def test_actor_event_wake_is_coalesced_and_absent_from_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⒝: a burst of events on one actor yields one queue slot, no queue_full."""

    state = _state(tmp_path, monkeypatch)
    store = PacGraphStore(default_database_path(state))
    graph_id, node_id = _seed_graph(store, state, "coalesce-graph")
    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=_FakeRuntime(),
        sender=NullSender(),
        daemon_epoch="coalesce-epoch",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    try:
        assert service.note_actor_dirty(graph_id, node_id) == 1
        assert service.note_actor_dirty(graph_id, node_id) == 0
        assert list(service._actor_queue.queue) == [(graph_id, node_id)]
    finally:
        assert service.close()
        store.close()


def test_dropped_event_signal_never_raises_into_a_graph_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake must be best-effort; the fallback sweep is the net."""

    state = _state(tmp_path, monkeypatch)
    store = PacGraphStore(default_database_path(state))
    authority = PacGraphAuthority(default_database_path(state), NullSender())

    def broken_wake(graph_id: str, node_id: str | None = None) -> int:
        raise RuntimeError("injected wake failure")

    try:
        authority.attach_reconciler(
            _FakeRuntime(), tmp_path, "broken-epoch", on_actor_wake=broken_wake
        )
        graph_id, _node_id = _seed_graph(store, state, "broken-wake")
        assert authority.wake_actor(graph_id) == 0
        assert authority.activate_graph(graph_id, actor=OWNER)["ok"] is True
    finally:
        assert authority.close(3.0)
        store.close()
