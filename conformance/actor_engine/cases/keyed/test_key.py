# Decision record (file header; no inline comments by repository rule):
# - Keyed spawn is a mutex claim over one (group, key) pair. The first caller
#   claims the pair before factory construction, every later caller receives
#   key-in-use immediately, and no caller waits for another start.
# - KEY-01..KEY-11 cover the state-by-operation table in spec section 3.5 for
#   both ABI backends. Keyed spawn returns the same actor-id shape as keyless
#   spawn. lookup is the only way to resolve an existing keyed handle.
# - KEY-12..KEY-16 pin the pump-start interleave window (the fourth review blocking
#   item; the fifth review follow-up). The native backend exposes conformance-only
#   holds (`_test_pump_start_hold_reached` / `_test_release_pump_start_hold`)
#   at two one-shot park points selected at engine construction:
#   HYPRIAL_ACTOR_TEST_PUMP_START_HOLD parks one keyed spawn after the pump
#   is established and registered but before the installation adjudication
#   (GIL released, no engine lock held); HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD
#   parks right after the spawnability gate, before the pump is established,
#   so a shutdown completing there must arbitrate against the establishment
#   failure instead of the gate refusal (KEY-14). The reference backend
#   serializes record installation and thread start under one engine lock,
#   so its pump-start point is held by patching `Thread.start` for the
#   spawned actor thread: a find or second spawn issued there serializes on
#   the engine lock and observes only the fully installed actor, while
#   shutdown lands after the install and follows the normal stop path.
# - Interleave evidence is event-driven (design decision 2026-11-20):
#   every case first asserts its hold or block point was actually reached,
#   concurrent operations handshake with an arrival event plus an
#   instantaneous not-completed check, and the verdict is the post-release
#   result — no fixed-duration "still blocked" window is used as evidence.
# - Linearization: a keyed spawn linearizes at installation. The returned id
#   states that the creation was installed live; a concurrent stop or
#   shutdown may already have terminated the actor by the time the caller
#   observes the id, exactly like a keyless spawn. A start that never
#   installed never returns an id. Registry readouts after a shutdown
#   assert only the two I2 responsibilities (key index empty; cancelled or
#   failed starts leave no actor and no pump registration); whether a
#   normally-lived actor's pump registration survives shutdown is
#   origin/dev pre-existing behaviour and stays unasserted (spec 3.5 note).
"""Keyed actor state and operation conformance cases."""

from __future__ import annotations

import threading
from typing import Any, Callable

import pytest

from support.helpers import (
    WAIT,
    Actor,
    Block,
    DeadLetters,
    EventLog,
    Harness,
    Recorder,
    wait_until,
)

case = pytest.mark.case


class _PumpStartHold:
    """One keyed spawn held at its pump-start point, backend-aware.

    The native engine is constructed with the hold armed; the reference
    backend holds the spawner inside its install critical section by
    patching ``Thread.start`` for the actor thread named ``thread_name``.
    """

    def __init__(self, backend: object, monkeypatch: pytest.MonkeyPatch, thread_name: str) -> None:
        self.held = threading.Event()
        self.release_gate = threading.Event()
        self.native = not backend.__name__.endswith("reference_abi")
        if self.native:
            monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_HOLD", "1")
            return
        real_start = threading.Thread.start

        def held_start(thread: threading.Thread, *args: object, **kwargs: object) -> object:
            if thread.name == thread_name:
                started = real_start(thread, *args, **kwargs)
                self.held.set()
                assert self.release_gate.wait(WAIT)
                return started
            return real_start(thread, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", held_start)

    def wait_held(self, harness: Harness) -> None:
        if self.native:
            assert wait_until(lambda: harness.engine._test_pump_start_hold_reached())
            return
        assert self.held.wait(WAIT)

    def release_held(self, harness: Harness) -> None:
        if self.native:
            harness.engine._test_release_pump_start_hold()
        self.release_gate.set()


def _spawn_in_thread(
    harness: Harness, name: str, group: str, key: str, factory: Any
) -> tuple[threading.Thread, list[object]]:
    result: list[object] = []

    def run() -> None:
        try:
            result.append(_spawn_args(harness, name, group, key, factory))
        except BaseException as exc:
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _spawn(
    harness: Harness,
    group: str,
    key: str,
    name: str,
    factory: Any = None,
    *,
    profile: str = "fast",
    event_sink: Callable | None = None,
    dead_letters: DeadLetters | None = None,
) -> Actor:
    actor_id = harness.engine.spawn(
        name,
        factory if factory is not None else harness.factory(),
        16,
        profile,
        dead_letters.sink if dead_letters is not None else None,
        event_sink,
        group,
        key,
    )
    assert isinstance(actor_id, str)
    return Actor(actor_id, name)


def _spawn_args(harness: Harness, name: str, group: str, key: str, factory: Any) -> str:
    actor_id = harness.engine.spawn(name, factory, 16, "fast", None, None, group, key)
    assert isinstance(actor_id, str)
    return actor_id


def _shutdown_has_drained(engine: object) -> bool:
    """Wait until a started shutdown passed its lifecycle drain mark.

    A keyless probe spawn whose factory raises leaves no record on either
    backend, and once the engine is draining the same probe is refused with
    RuntimeError, so the flip from ActorSpawnError to RuntimeError observes
    the drain without side effects.
    """

    def probe() -> object:
        raise RuntimeError("drain probe")

    def refused() -> bool:
        try:
            engine.spawn("drain-probe", probe, 16, "fast", None, None, None, None)
        except RuntimeError:
            return True
        except BaseException:
            return False
        return False

    return wait_until(refused)


@case("KEY-01")
def test_idle_key_is_claimed_and_keyed_spawn_keeps_the_actor_id_shape(hp: Harness) -> None:
    builds = Recorder()
    actor = _spawn(hp, "cell", "c.1", "first", hp.factory(builds))
    assert isinstance(actor.actor_id, str)
    assert len(builds) == 1
    assert hp.engine.lookup("cell", "c.1") == (actor.actor_id, "first")


@case("KEY-02")
def test_starting_key_rejects_immediately_without_running_the_second_factory(
    hp: Harness,
) -> None:
    started = threading.Event()
    release = threading.Event()
    second_builds = Recorder()

    def first_factory() -> object:
        started.set()
        assert release.wait(10.0)
        return lambda command: command

    def second_factory() -> object:
        second_builds.append(1)
        return lambda command: command

    result: list[str] = []
    errors: list[tuple[str, str]] = []

    def start_first() -> None:
        try:
            result.append(_spawn_args(hp, "first", "cell", "c.2", first_factory))
        except BaseException as exc:
            errors.append((type(exc).__name__, str(exc)))

    thread = threading.Thread(target=start_first)
    thread.start()
    assert started.wait(10.0)
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.2")
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn_args(hp, "second", "cell", "c.2", second_factory)
    assert len(second_builds) == 0
    release.set()
    thread.join(10.0)
    assert not thread.is_alive()
    assert errors == []
    assert len(result) == 1


@case("KEY-03")
def test_live_key_spawn_is_rejected_and_lookup_returns_the_live_actor(
    hp: Harness,
) -> None:
    actor = _spawn(hp, "cell", "c.3", "running")
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.3", "second")
    assert hp.engine.lookup("cell", "c.3") == (actor.actor_id, actor.name)

    def fail_on(command: object) -> object:
        if command == "fail":
            raise RuntimeError("pending")
        return command

    pending = _spawn(hp, "cell", "c.3-pending", "pending", lambda: fail_on, profile="slow")
    assert hp.tell(pending, "fail") == 0
    assert hp.wait_state(pending, "restart_pending")
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.3-pending", "second-pending")
    assert hp.engine.lookup("cell", "c.3-pending") == (pending.actor_id, pending.name)

    quarantined = _spawn(hp, "cell", "c.3-quarantined", "quarantined", lambda: fail_on, profile="never")
    assert hp.tell(quarantined, "fail") == 0
    assert hp.wait_state(quarantined, "quarantined")
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.3-quarantined", "second-quarantined")
    assert hp.engine.lookup("cell", "c.3-quarantined") == (quarantined.actor_id, quarantined.name)


@case("KEY-04")
def test_stopping_key_is_rejected_then_confirmed_stop_releases_it(hp: Harness) -> None:
    release = threading.Event()
    entered = threading.Event()
    actor = _spawn(hp, "cell", "c.4", "stopping")
    assert hp.tell(actor, Block(release, entered)) == 0
    assert entered.wait(10.0)
    assert hp.stop(actor, 0.05) is False
    assert hp.state(actor) == "stopping"
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.4", "during-stop")
    assert hp.engine.lookup("cell", "c.4") == (actor.actor_id, actor.name)
    release.set()
    assert hp.stop(actor, 10.0) is True
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.4")
    fresh = _spawn(hp, "cell", "c.4", "fresh")
    assert fresh.actor_id != actor.actor_id


@case("KEY-05")
def test_confirmed_stop_and_supervision_restart_have_distinct_key_results(hp: Harness) -> None:
    def failing(command: object) -> object:
        if command == "fail":
            raise RuntimeError("restart")
        return command

    restarted = _spawn(hp, "cell", "c.5-restart", "restart", lambda: failing)
    assert hp.tell(restarted, "fail") == 0
    assert hp.wait_state(restarted, "restart_pending")
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.5-restart", "during-restart")
    assert hp.engine.lookup("cell", "c.5-restart") == (restarted.actor_id, restarted.name)
    assert wait_until(lambda: hp.generation(restarted) == 2)
    assert hp.engine.lookup("cell", "c.5-restart") == (restarted.actor_id, restarted.name)


@case("KEY-06")
def test_factory_and_pump_failures_release_the_key(
    hp: Harness, make_harness, backend: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_factory() -> object:
        raise RuntimeError("factory-failure")

    with pytest.raises(hp.module.ActorSpawnError) as caught:
        _spawn(hp, "cell", "c.6-factory", "bad", bad_factory)
    assert isinstance(caught.value.__cause__, RuntimeError)
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.6-factory")
    fresh = _spawn(hp, "cell", "c.6-factory", "fresh")
    assert hp.engine.lookup("cell", "c.6-factory") == (fresh.actor_id, fresh.name)

    hp.close(5.0)
    if backend.__name__.endswith("reference_abi"):
        hp = make_harness()
        hp.engine._test_pump_start_failure = True
    else:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        hp = Harness(backend, kind="python")
    try:
        with pytest.raises(hp.module.ActorSpawnError, match="injected pump start failure") as caught:
            _spawn(hp, "cell", "c.6-pump", "pump-fail")
        injected = str(caught.value)
        assert injected.startswith("engine failed to start actor")
        assert injected.endswith(": injected pump start failure")
        with pytest.raises(KeyError):
            hp.engine.lookup("cell", "c.6-pump")
        assert hp.engine._test_registry_counts() == (0, 0, 0)
        fresh = _spawn(hp, "cell", "c.6-pump", "pump-fresh")
        assert hp.engine.lookup("cell", "c.6-pump") == (fresh.actor_id, fresh.name)
        other = _spawn(hp, "cell", "c.6-other", "pump-other")
        assert hp.engine.lookup("cell", "c.6-other") == (other.actor_id, other.name)
    finally:
        hp.close(5.0)


@case("KEY-06")
def test_shutdown_decides_starting_and_installed_key_lifecycles(
    make_harness,
) -> None:
    starting = make_harness()
    started = threading.Event()
    release = threading.Event()
    result: list[object] = []

    def blocked_factory() -> object:
        started.set()
        release.wait(10.0)
        return lambda command: command

    def spawn_blocked() -> None:
        try:
            result.append(_spawn_args(starting, "blocked", "cell", "c.shutdown-b", blocked_factory))
        except BaseException as exc:
            result.append(exc)

    thread = threading.Thread(target=spawn_blocked)
    thread.start()
    assert started.wait(10.0)
    assert starting.engine.shutdown(5.0) is True
    release.set()
    thread.join(10.0)
    assert isinstance(result[0], RuntimeError)
    with pytest.raises(KeyError):
        starting.engine.lookup("cell", "c.shutdown-b")

    installed = make_harness()
    actor = _spawn(installed, "cell", "c.shutdown-c", "installed")
    assert installed.engine.shutdown(5.0) is True
    with pytest.raises(KeyError):
        installed.engine.lookup("cell", "c.shutdown-c")
    del actor


@case("KEY-07")
def test_reentrant_factory_gets_key_in_use_and_outer_start_can_succeed_or_fail(
    hp: Harness,
) -> None:
    nested: list[tuple[str, str]] = []
    def successful_factory() -> object:
        try:
            _spawn_args(hp, "inner", "cell", "c.7-success", successful_factory)
        except BaseException as exc:
            nested.append((type(exc).__name__, str(exc)))
        return lambda command: command

    actor_id = _spawn_args(hp, "outer", "cell", "c.7-success", successful_factory)
    assert isinstance(actor_id, str)
    assert nested == [("ActorSpawnError", "key-in-use")]

    def failing_factory() -> object:
        try:
            _spawn_args(hp, "inner-fail", "cell", "c.7-fail", failing_factory)
        except BaseException as exc:
            nested.append((type(exc).__name__, str(exc)))
        raise RuntimeError("outer-failure")

    with pytest.raises(hp.module.ActorSpawnError) as caught:
        _spawn_args(hp, "outer-fail", "cell", "c.7-fail", failing_factory)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert nested[-1] == ("ActorSpawnError", "key-in-use")
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.7-fail")


@case("KEY-08")
@pytest.mark.parametrize("group,key", [("cell", ""), ("", "c.8"), (None, "c.8"), (1, "c.8")])
def test_invalid_key_forms_are_value_error_on_spawn_and_lookup(
    hp: Harness, group: object, key: object
) -> None:
    with pytest.raises(ValueError):
        hp.engine.spawn("invalid", hp.factory(), 16, "fast", None, None, group, key)
    with pytest.raises(ValueError):
        hp.engine.lookup(group, key)


@case("KEY-09")
def test_eight_concurrent_first_spawns_have_one_success_and_seven_refusals(hp: Harness) -> None:
    barrier = threading.Barrier(8)
    builds = Recorder()
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def factory() -> object:
        builds.append(1)
        return lambda command: command

    def run() -> None:
        barrier.wait()
        try:
            actor_id = _spawn_args(hp, "raced", "cell", "c.9", factory)
            outcome = ("ok", actor_id)
        except BaseException as exc:
            outcome = (type(exc).__name__, str(exc))
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert len(outcomes) == 8
    assert len(builds) == 1
    assert sum(kind == "ok" for kind, _ in outcomes) == 1
    assert sum(message == "key-in-use" for _, message in outcomes) == 7


@case("KEY-10")
def test_keyed_events_append_the_pair_and_keyless_events_keep_eight_fields(hp: Harness) -> None:
    keyed_events = EventLog()
    plain_events = EventLog()
    keyed = _spawn(hp, "cell", "c.10", "keyed-events", event_sink=keyed_events.sink)
    plain_id = hp.engine.spawn("plain-events", hp.factory(), 16, "fast", None, plain_events.sink)
    plain = Actor(plain_id, "plain-events")
    assert hp.tell(keyed, "ping") == 0
    assert hp.tell(plain, "ping") == 0
    assert wait_until(lambda: len(keyed_events) > 0 and len(plain_events) > 0)
    assert keyed_events.items()[0][8:] == ("cell", "c.10")
    assert plain_events.items()[0][8:] == ()
    assert len(plain_events.items()[0]) == 8


@case("KEY-11")
def test_confirmed_stop_dead_letters_and_respawn_starts_empty(hp: Harness) -> None:
    dead = DeadLetters()
    release = threading.Event()
    entered = threading.Event()
    actor = _spawn(hp, "cell", "c.11", "mailbox", dead_letters=dead)
    assert hp.tell(actor, Block(release, entered)) == 0
    assert entered.wait(10.0)
    assert hp.tell(actor, "one") == 0
    assert hp.tell(actor, "two") == 0
    assert hp.stop(actor, 0.05) is False
    release.set()
    assert dead.wait_len(2)
    assert [(message, reason) for message, reason in dead.items()] == [("one", "stopped"), ("two", "stopped")]
    assert hp.stop(actor, 10.0) is True
    fresh = _spawn(hp, "cell", "c.11", "mailbox")
    assert fresh.actor_id != actor.actor_id
    assert hp.snapshot(fresh)[2] == 0


@case("KEY-12")
def test_pump_start_hold_keeps_lookup_absent_and_second_spawn_refused(
    make_harness, backend: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold = _PumpStartHold(backend, monkeypatch, "ref-held-pump")
    held_hp = make_harness()
    engine = held_hp.engine
    second_builds = Recorder()

    def second_factory() -> object:
        second_builds.append(1)
        return lambda command: command

    thread, result = _spawn_in_thread(held_hp, "held-pump", "cell", "c.12", held_hp.factory())
    hold.wait_held(held_hp)
    if hold.native:
        with pytest.raises(KeyError):
            engine.lookup("cell", "c.12")
        with pytest.raises(held_hp.module.ActorSpawnError, match="key-in-use"):
            _spawn_args(held_hp, "second", "cell", "c.12", second_factory)
        assert len(second_builds) == 0
        counts = engine._test_registry_counts()
        assert (counts[0], counts[1]) == (0, 1)
        assert counts[2] == 1
        hold.release_held(held_hp)
        thread.join(WAIT)
    else:
        lookup_arrived = threading.Event()
        lookup_done = threading.Event()
        lookup_result: list[object] = []

        def run_lookup() -> None:
            lookup_arrived.set()
            try:
                lookup_result.append(engine.lookup("cell", "c.12"))
            except BaseException as exc:
                lookup_result.append(exc)
            lookup_done.set()

        second_arrived = threading.Event()
        second_done = threading.Event()
        second_result: list[object] = []

        def run_second() -> None:
            second_arrived.set()
            try:
                second_result.append(_spawn_args(held_hp, "second", "cell", "c.12", second_factory))
            except BaseException as exc:
                second_result.append(exc)
            second_done.set()

        lookup_thread = threading.Thread(target=run_lookup)
        lookup_thread.start()
        assert lookup_arrived.wait(WAIT)
        second_thread = threading.Thread(target=run_second)
        second_thread.start()
        assert second_arrived.wait(WAIT)
        assert not lookup_done.is_set()
        assert not second_done.is_set()
        hold.release_held(held_hp)
        thread.join(WAIT)
        lookup_thread.join(WAIT)
        second_thread.join(WAIT)
        assert isinstance(lookup_result[0], tuple)
        assert lookup_result == [(result[0], "held-pump")]
        assert isinstance(second_result[0], held_hp.module.ActorSpawnError)
        assert str(second_result[0]) == "key-in-use"
        assert len(second_builds) == 0
        counts = engine._test_registry_counts()
        assert (counts[0], counts[1]) == (1, 1)
    assert isinstance(result[0], str)
    assert engine.lookup("cell", "c.12") == (result[0], "held-pump")
    assert held_hp.close(5.0) is True
    counts = engine._test_registry_counts()
    assert (counts[0], counts[1]) == (1, 0)
    with pytest.raises(KeyError):
        engine.lookup("cell", "c.12")


@case("KEY-13")
def test_shutdown_during_pump_start_has_one_lifecycle_decision(
    make_harness, backend: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold = _PumpStartHold(backend, monkeypatch, "ref-held-shutdown")
    engine_hp = make_harness()
    engine = engine_hp.engine
    thread, result = _spawn_in_thread(engine_hp, "held-shutdown", "cell", "c.13", engine_hp.factory())
    hold.wait_held(engine_hp)
    if hold.native:
        assert engine.shutdown(5.0) is True
        hold.release_held(engine_hp)
        thread.join(WAIT)
        assert isinstance(result[0], RuntimeError)
        assert "refused" in str(result[0])
    else:
        shutdown_arrived = threading.Event()
        shutdown_done = threading.Event()
        shutdown_ok: list[bool] = []

        def run_shutdown() -> None:
            shutdown_arrived.set()
            shutdown_ok.append(engine.shutdown(WAIT))
            shutdown_done.set()

        shutdown_thread = threading.Thread(target=run_shutdown)
        shutdown_thread.start()
        assert shutdown_arrived.wait(WAIT)
        assert not shutdown_done.is_set()
        hold.release_held(engine_hp)
        thread.join(WAIT)
        shutdown_thread.join(WAIT)
        assert isinstance(result[0], str)
        assert shutdown_ok == [True]
    if hold.native:
        assert engine._test_registry_counts() == (0, 0, 0)
    else:
        counts = engine._test_registry_counts()
        assert (counts[0], counts[1]) == (1, 0)
    with pytest.raises(KeyError):
        engine.lookup("cell", "c.13")

    winner_hp = make_harness()
    winner_hold = _PumpStartHold(backend, monkeypatch, "ref-held-install")
    thread, result = _spawn_in_thread(winner_hp, "held-install", "cell", "c.13b", winner_hp.factory())
    winner_hold.wait_held(winner_hp)
    winner_hold.release_held(winner_hp)
    thread.join(WAIT)
    assert isinstance(result[0], str)
    installed = result[0]
    assert winner_hp.engine.lookup("cell", "c.13b") == (installed, "held-install")
    assert winner_hp.engine.shutdown(5.0) is True
    counts = winner_hp.engine._test_registry_counts()
    assert (counts[0], counts[1]) == (1, 0)
    with pytest.raises(KeyError):
        winner_hp.engine.lookup("cell", "c.13b")
    with pytest.raises(RuntimeError, match="refused"):
        winner_hp.engine.spawn("after-install-stop", winner_hp.factory(), 16, "fast", None, None, "cell", "c.13b")


@case("KEY-14")
def test_pump_start_failure_and_shutdown_arbitrate_without_residue(
    make_harness, backend: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    if backend.__name__.endswith("reference_abi"):
        failed_hp = make_harness()
        failed_hp.engine._test_pump_start_failure = True
    else:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        failed_hp = make_harness()
    with pytest.raises(failed_hp.module.ActorSpawnError, match="injected pump start failure") as caught:
        _spawn(failed_hp, "cell", "c.14", "pump-fail")
    injected = str(caught.value)
    assert injected.startswith("engine failed to start actor")
    assert injected.endswith(": injected pump start failure")
    assert failed_hp.engine._test_registry_counts() == (0, 0, 0)
    with pytest.raises(KeyError):
        failed_hp.engine.lookup("cell", "c.14")
    assert failed_hp.engine.shutdown(5.0) is True
    assert failed_hp.engine._test_registry_counts() == (0, 0, 0)

    blocked_hp = make_harness()
    if backend.__name__.endswith("reference_abi"):
        blocked_hp.engine._test_pump_start_failure = True
    started = threading.Event()
    release = threading.Event()

    def blocked_factory() -> object:
        started.set()
        assert release.wait(WAIT)
        return lambda command: command

    thread, result = _spawn_in_thread(blocked_hp, "blocked-fail", "cell", "c.14b", blocked_factory)
    assert started.wait(WAIT)
    assert blocked_hp.engine.shutdown(5.0) is True
    release.set()
    thread.join(WAIT)
    assert isinstance(result[0], RuntimeError)
    assert "refused" in str(result[0])
    if backend.__name__.endswith("reference_abi"):
        assert blocked_hp.engine._test_pump_start_failure is True
    assert blocked_hp.engine._test_registry_counts() == (0, 0, 0)
    with pytest.raises(KeyError):
        blocked_hp.engine.lookup("cell", "c.14b")

    if backend.__name__.endswith("reference_abi"):
        race_hp = make_harness()
        race_held = threading.Event()
        race_arrived = threading.Event()
        race_done = threading.Event()
        race_gate = threading.Event()
        real_start = threading.Thread.start

        def fail_after_park(parked: threading.Thread, *args: object, **kwargs: object) -> object:
            if parked.name == "ref-race-fail":
                race_held.set()
                assert race_gate.wait(WAIT)
                raise RuntimeError("injected pump start failure")
            return real_start(parked, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", fail_after_park)
    else:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD", "1")
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        race_hp = make_harness()
    race_engine = race_hp.engine
    thread, result = _spawn_in_thread(race_hp, "race-fail", "cell", "c.14c", race_hp.factory())
    race_shutdown_ok: list[bool] = []
    if backend.__name__.endswith("reference_abi"):
        assert race_held.wait(WAIT)

        def run_race_shutdown() -> None:
            race_arrived.set()
            race_shutdown_ok.append(race_engine.shutdown(WAIT))
            race_done.set()

        stopper = threading.Thread(target=run_race_shutdown)
        stopper.start()
        assert race_arrived.wait(WAIT)
        assert not race_done.is_set()
        race_gate.set()
        thread.join(WAIT)
        stopper.join(WAIT)
        assert not stopper.is_alive()
    else:
        assert wait_until(race_engine._test_pump_start_hold_reached)
        race_shutdown_ok.append(race_engine.shutdown(5.0))
        race_engine._test_release_pump_start_hold()
        thread.join(WAIT)
    assert not thread.is_alive()
    assert isinstance(result[0], race_hp.module.ActorSpawnError)
    message = str(result[0])
    assert message.startswith("engine failed to start actor")
    assert message.endswith(": injected pump start failure")
    assert race_shutdown_ok == [True]
    assert race_engine._test_registry_counts() == (0, 0, 0)
    with pytest.raises(KeyError):
        race_engine.lookup("cell", "c.14c")
    with pytest.raises(RuntimeError, match="refused"):
        _spawn_args(race_hp, "after-race", "cell", "c.14c", race_hp.factory())


@case("KEY-15")
def test_confirmed_stop_and_same_key_spawn_race_in_both_orders(hp: Harness) -> None:
    release = threading.Event()
    entered = threading.Event()
    actor = _spawn(hp, "cell", "c.15-a", "stop-then-spawn")
    assert hp.tell(actor, Block(release, entered)) == 0
    assert entered.wait(WAIT)
    assert hp.stop(actor, 0.05) is False
    assert hp.state(actor) == "stopping"
    with pytest.raises(hp.module.ActorSpawnError, match="key-in-use"):
        _spawn(hp, "cell", "c.15-a", "during-stop")
    release.set()
    assert hp.stop(actor, WAIT) is True
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.15-a")
    fresh = _spawn(hp, "cell", "c.15-a", "after-stop")
    assert fresh.actor_id != actor.actor_id
    assert hp.engine.lookup("cell", "c.15-a") == (fresh.actor_id, "after-stop")
    assert hp.snapshot(fresh)[2] == 0

    for attempt in range(3):
        key = f"c.15-r{attempt}"
        race_release = threading.Event()
        race_entered = threading.Event()
        raced = _spawn(hp, "cell", key, f"raced-{attempt}")
        assert hp.tell(raced, Block(race_release, race_entered)) == 0
        assert race_entered.wait(WAIT)
        assert hp.stop(raced, 0.05) is False
        race_release.set()
        try:
            second_id = _spawn_args(hp, "second", "cell", key, hp.factory())
            second = Actor(second_id, "second")
        except hp.module.ActorSpawnError as exc:
            assert str(exc) == "key-in-use"
            assert hp.stop(raced, WAIT) is True
            second = _spawn(hp, "cell", key, "after-race")
        assert second.actor_id != raced.actor_id
        assert hp.engine.lookup("cell", key) == (second.actor_id, second.name)
        assert hp.snapshot(second)[2] == 0

    assert hp.close(5.0) is True
    counts = hp.engine._test_registry_counts()
    assert (counts[0], counts[1]) == (8, 0)
    with pytest.raises(RuntimeError):
        _spawn(hp, "cell", "c.15-a", "after-shutdown")


@case("KEY-16")
def test_shutdown_while_stopping_confirms_the_stop_and_releases_the_pair(hp: Harness) -> None:
    release = threading.Event()
    entered = threading.Event()
    actor = _spawn(hp, "cell", "c.16", "stopping-shutdown")
    assert hp.tell(actor, Block(release, entered)) == 0
    assert entered.wait(WAIT)
    assert hp.stop(actor, 0.05) is False
    assert hp.state(actor) == "stopping"
    shutdown_arrived = threading.Event()
    shutdown_done = threading.Event()
    shutdown_ok: list[bool] = []

    def run_shutdown() -> None:
        shutdown_arrived.set()
        shutdown_ok.append(hp.engine.shutdown(WAIT))
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=run_shutdown)
    shutdown_thread.start()
    assert shutdown_arrived.wait(WAIT)
    assert _shutdown_has_drained(hp.engine)
    assert not shutdown_done.is_set()
    release.set()
    shutdown_thread.join(WAIT)
    assert not shutdown_thread.is_alive()
    assert shutdown_ok == [True]
    with pytest.raises(KeyError):
        hp.engine.lookup("cell", "c.16")
    counts = hp.engine._test_registry_counts()
    assert (counts[0], counts[1]) == (1, 0)
    with pytest.raises(RuntimeError):
        _spawn(hp, "cell", "c.16", "after-shutdown")
    assert hp.engine.shutdown(5.0) is True
