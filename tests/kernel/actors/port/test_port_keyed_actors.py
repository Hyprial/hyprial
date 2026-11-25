# Decision record (file header; no inline comments by repository rule):
# - Keyed spawn is a mutex claim over an existing actor table. A starting or
#   live pair rejects another spawn immediately with ActorSpawnRefused carrying
#   code `key-in-use`; lookup is the only path to an existing keyed handle.
# - These tests parameterize both ABI backends and cover every state and
#   operation cell in the keyed state table, including shutdown arbitration,
#   failure cleanup and factory execution counts.
# - On an ordinary native build the conformance-only holds and fault
#   injection do not exist: the native variants of the pump-start tests then
#   assert that the three test environment variables are ignored, or skip
#   with the conformance cases KEY-12 to KEY-14 named as the native coverage.
# - The pump-start interleave window is pinned at the port too (the fourth review;
#   the fifth review follow-up): the native engine exposes conformance-only holds
#   (HYPRIAL_ACTOR_TEST_PUMP_START_HOLD after pump establishment and
#   HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD right after the spawnability gate, plus
#   `_test_pump_start_hold_reached` / `_test_release_pump_start_hold` on the
#   raw engine); the reference backend is held inside its install critical
#   section by patching `Thread.start` for the actor thread, where find, a
#   second spawn and shutdown serialize on the engine lock and observe only
#   the fully installed actor. Concurrent operations handshake with an
#   arrival event plus an instantaneous not-completed check and are judged
#   by their post-release results; no fixed-duration "still blocked" window
#   is used as evidence. A pump-start failure racing a shutdown (one
#   lifecycle decision, no registry residue) is covered on both backends.
#   Keyless compatibility is pinned directly: the keyless handle
#   repr/hash keep the exact origin/dev two-field values, and the structural
#   event log keeps the exact origin/dev field set.
"""Keyed actor port contract tests."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from hyprial.kernel.impl.actor_runtime import (
    ActorEvent,
    ActorHandle,
    ActorSpec,
    ActorState,
    AdmissionResult,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.port import ActorEngineConfig, ActorHost, ActorSpawnRefused, create_actor_host


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _config(event_sink: Callable[[ActorEvent], None] | None = None, max_restarts: int = 2) -> ActorEngineConfig:
    return ActorEngineConfig(
        policies={
            STATE_AUTHORITY: SupervisionPolicy(
                max_restarts=max_restarts,
                restart_window=10.0,
                base_backoff=0.05,
                max_backoff=0.05,
                jitter_ratio=0.0,
            )
        },
        event_sink=event_sink,
    )


@pytest.fixture(params=["reference-abi", "native"])
def host(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[ActorHost]:
    monkeypatch.setenv("HYPRIAL_ACTOR_BACKEND", request.param)
    made = create_actor_host(_config())
    try:
        yield made
    finally:
        made.shutdown(5.0)


def _native_without_conformance_hooks(host: ActorHost) -> bool:
    native = host.engine._module.__name__.endswith("._core.actors")
    return native and not hasattr(host.engine._native, "_test_registry_counts")


def _echo(log: list[object] | None = None) -> Callable[[object], object]:
    def handle(command: object) -> object:
        if log is not None:
            log.append(command)
        return command

    return handle


def _spec(
    name: str,
    builds: list[int] | None = None,
    *,
    group: str | None = None,
    key: str | None = None,
    factory: Callable[[], Callable[[object], object]] | None = None,
    capacity: int = 128,
    profile: str = STATE_AUTHORITY,
) -> ActorSpec:
    if factory is None:
        def factory() -> Callable[[object], object]:
            if builds is not None:
                builds.append(1)
            return _echo()
    return ActorSpec(
        name=name,
        handler_factory=factory,
        mailbox_capacity=capacity,
        supervision_profile=profile,
        group=group,
        key=key,
    )


def test_keyed_spawn_returns_an_ordinary_handle_and_lookup_is_explicit(host: ActorHost) -> None:
    builds: list[int] = []
    first = host.spawn(_spec("first", builds, group="cell", key="c.1"))
    assert isinstance(first.handle, ActorHandle)
    assert len(builds) == 1
    with pytest.raises(ActorSpawnRefused, match="key-in-use") as refused:
        host.spawn(_spec("second", builds, group="cell", key="c.1"))
    assert refused.value.code == "key-in-use"
    found = host.find("cell", "c.1")
    assert found is not None
    assert found.handle == first.handle
    assert len(builds) == 1


def test_starting_key_rejects_without_running_second_factory(host: ActorHost) -> None:
    started = threading.Event()
    release = threading.Event()
    second_builds: list[int] = []

    def first_factory() -> Callable[[object], object]:
        started.set()
        assert release.wait(5.0)
        return _echo()

    def second_factory() -> Callable[[object], object]:
        second_builds.append(1)
        return _echo()

    result: list[object] = []

    def run() -> None:
        result.append(host.spawn(_spec("first", group="cell", key="c.2", factory=first_factory)))

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(3.0)
    with pytest.raises(ActorSpawnRefused, match="key-in-use"):
        host.spawn(_spec("second", group="cell", key="c.2", factory=second_factory))
    assert second_builds == []
    release.set()
    thread.join(5.0)
    assert len(result) == 1


def test_stopping_restart_pending_and_quarantined_keep_the_original_handle(host: ActorHost) -> None:
    def fail_on(command: object) -> object:
        if command == "fail":
            raise RuntimeError("failure")
        return command

    pending = host.spawn(_spec("pending", group="cell", key="c.pending", factory=lambda: fail_on, profile=STATE_AUTHORITY))
    assert pending.tell("fail") is AdmissionResult.ACCEPTED
    _wait_until(lambda: pending.snapshot().state is ActorState.RESTART_PENDING)
    assert host.find("cell", "c.pending").handle == pending.handle
    with pytest.raises(ActorSpawnRefused, match="key-in-use"):
        host.spawn(_spec("pending-2", group="cell", key="c.pending"))

    quarantine = host.spawn(_spec("quarantine", group="cell", key="c.quarantine", factory=lambda: fail_on))
    for _ in range(3):
        assert quarantine.tell("fail") is AdmissionResult.ACCEPTED
        _wait_until(lambda: quarantine.snapshot().state in {ActorState.RESTART_PENDING, ActorState.QUARANTINED})
        _wait_until(lambda: quarantine.snapshot().state is ActorState.RUNNING or quarantine.snapshot().state is ActorState.QUARANTINED)
        if quarantine.snapshot().state is ActorState.QUARANTINED:
            break
    assert quarantine.snapshot().state is ActorState.QUARANTINED
    assert host.find("cell", "c.quarantine").handle == quarantine.handle
    with pytest.raises(ActorSpawnRefused, match="key-in-use"):
        host.spawn(_spec("quarantine-2", group="cell", key="c.quarantine"))

    release = threading.Event()
    entered = threading.Event()
    stopping = host.spawn(_spec("stopping", group="cell", key="c.stopping", factory=lambda: (lambda command: (entered.set(), release.wait(5.0), command)[-1])))
    assert stopping.tell("block") is AdmissionResult.ACCEPTED
    assert entered.wait(3.0)
    assert stopping.stop(0.05) is False
    assert host.find("cell", "c.stopping").handle == stopping.handle
    with pytest.raises(ActorSpawnRefused, match="key-in-use"):
        host.spawn(_spec("stopping-2", group="cell", key="c.stopping"))
    release.set()
    assert stopping.stop(5.0) is True


def test_confirmed_stop_releases_key_and_new_actor_has_empty_mailbox(host: ActorHost) -> None:
    release = threading.Event()
    entered = threading.Event()

    def blocked(command: object) -> object:
        if command == "block":
            entered.set()
            release.wait(5.0)
        return command

    first = host.spawn(_spec("first", group="cell", key="c.4", factory=lambda: blocked))
    first.tell("block")
    assert entered.wait(3.0)
    assert first.tell("one") is AdmissionResult.ACCEPTED
    assert first.tell("two") is AdmissionResult.ACCEPTED
    assert first.stop(0.05) is False
    release.set()
    assert first.stop(5.0) is True
    assert host.find("cell", "c.4") is None
    second = host.spawn(_spec("second", group="cell", key="c.4"))
    assert second.handle != first.handle
    assert second.snapshot().queued == 0


def test_supervision_restart_keeps_key_and_handle(host: ActorHost) -> None:
    def handler(command: object) -> object:
        if command == "fail":
            raise RuntimeError("restart")
        return command

    actor = host.spawn(_spec("restart", group="cell", key="c.5", factory=lambda: handler))
    assert actor.tell("fail") is AdmissionResult.ACCEPTED
    _wait_until(lambda: actor.snapshot().generation == 2)
    found = host.find("cell", "c.5")
    assert found is not None
    assert found.handle == actor.handle


def test_factory_reentry_is_key_in_use_and_outer_result_controls_cleanup(host: ActorHost) -> None:
    nested: list[str] = []
    def success_factory() -> Callable[[object], object]:
        try:
            host.spawn(_spec("inner", group="cell", key="c.6", factory=success_factory))
        except Exception as exc:
            nested.append(str(exc))
        return _echo()
    outer = host.spawn(_spec("outer", group="cell", key="c.6", factory=success_factory))
    assert outer.handle.key == "c.6"
    assert nested == ["key-in-use"]

    def fail_factory() -> Callable[[object], object]:
        try:
            host.spawn(_spec("inner-fail", group="cell", key="c.6-fail", factory=fail_factory))
        except Exception as exc:
            nested.append(str(exc))
        raise RuntimeError("outer failure")
    with pytest.raises(RuntimeError, match="outer failure"):
        host.spawn(_spec("outer-fail", group="cell", key="c.6-fail", factory=fail_factory))
    assert host.find("cell", "c.6-fail") is None


def test_eight_threads_have_one_success_and_seven_key_in_use(host: ActorHost) -> None:
    barrier = threading.Barrier(8)
    builds: list[int] = []
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def run() -> None:
        barrier.wait()
        try:
            cell = host.spawn(_spec("raced", builds, group="cell", key="c.7"))
            item = ("ok", cell.handle.actor_id)
        except Exception as exc:
            item = (type(exc).__name__, str(exc))
        with lock:
            outcomes.append(item)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert len(outcomes) == 8
    assert len(builds) == 1
    assert sum(kind == "ok" for kind, _ in outcomes) == 1
    assert sum(message == "key-in-use" for _, message in outcomes) == 7


@pytest.mark.parametrize("backend", ["reference-abi", "native"])
def test_events_and_keyless_handle_shape(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[ActorEvent] = []
    monkeypatch.setenv("HYPRIAL_ACTOR_BACKEND", backend)
    made = create_actor_host(_config(events.append))
    try:
        keyed = made.spawn(_spec("keyed-event", group="cell", key="c.8"))
        plain = made.spawn(_spec("plain-event"))
        keyed.tell("go")
        plain.tell("go")
        _wait_until(lambda: len(events) >= 2)
        assert next(event for event in events if event.handle.name == "keyed-event").handle.key == "c.8"
        plain_event = next(event for event in events if event.handle.name == "plain-event")
        assert plain_event.handle == ActorHandle(plain.handle.actor_id, plain.handle.name)
    finally:
        made.shutdown(5.0)


def test_invalid_find_keys_are_value_error(host: ActorHost) -> None:
    for group, key in ((1, "k"), ("g", 2), (None, "k"), ("g", None), ("", "k")):
        with pytest.raises(ValueError):
            host.find(group, key)


def test_shutdown_during_factory_start_has_one_lifecycle_decision(host: ActorHost) -> None:
    started = threading.Event()
    release = threading.Event()
    result: list[BaseException | object] = []

    def spawn_blocked() -> None:
        try:
            result.append(host.spawn(_spec("blocked", group="cell", key="c.10", factory=factory)))
        except BaseException as exc:
            result.append(exc)

    def factory() -> Callable[[object], object]:
        started.set()
        release.wait(5.0)
        return _echo()

    thread = threading.Thread(target=spawn_blocked)
    thread.start()
    assert started.wait(3.0)
    assert host.shutdown(1.0) is True
    release.set()
    thread.join(5.0)
    assert len(result) == 1
    assert isinstance(result[0], ActorSpawnRefused)

    second_host = create_actor_host(_config())
    started = threading.Event()
    release = threading.Event()
    installed: list[object] = []

    def install_factory() -> Callable[[object], object]:
        started.set()
        assert release.wait(5.0)
        return _echo()

    def spawn_installed() -> None:
        try:
            installed.append(second_host.spawn(_spec("installed", group="cell", key="c.10b", factory=install_factory)))
        except BaseException as exc:
            installed.append(exc)

    thread = threading.Thread(target=spawn_installed)
    thread.start()
    assert started.wait(3.0)
    release.set()
    thread.join(5.0)
    assert len(installed) == 1
    assert not isinstance(installed[0], BaseException)
    assert second_host.shutdown(1.0) is True


def test_pump_start_failure_is_test_only_and_releases_key(host: ActorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    if _native_without_conformance_hooks(host):
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_HOLD", "1")
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD", "1")
        host.shutdown(5.0)
        ordinary = create_actor_host(_config())
        try:
            builds: list[int] = []
            spawned = ordinary.spawn(_spec("ordinary-build", builds, group="cell", key="c.ordinary"))
            found = ordinary.find("cell", "c.ordinary")
            assert found is not None
            assert found.handle == spawned.handle
            assert len(builds) == 1
        finally:
            assert ordinary.shutdown(5.0) is True
        return
    if host.engine._module.__name__.endswith("._core.actors"):
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        host.shutdown(5.0)
        host = create_actor_host(_config())
    else:
        import threading as threading_module
        real_start = threading_module.Thread.start

        def fail_ref_start(thread: threading_module.Thread, *args: object, **kwargs: object) -> object:
            if thread.name == "ref-pump-fail":
                raise RuntimeError("injected pump start failure")
            return real_start(thread, *args, **kwargs)

        monkeypatch.setattr(threading_module.Thread, "start", fail_ref_start)
    try:
        native = host.engine._module.__name__.endswith("._core.actors")
        with pytest.raises((host.engine._module.ActorSpawnError, RuntimeError), match="injected pump start failure") as failed:
            host.spawn(_spec("pump-fail", group="cell", key="c.11"))
        message = str(failed.value)
        if native:
            assert type(failed.value).__name__ == "ActorSpawnError"
            assert message.startswith("engine failed to start actor")
            assert message.endswith(": injected pump start failure")
        else:
            assert type(failed.value).__name__ == "RuntimeError"
            assert message == "injected pump start failure"
        assert host.find("cell", "c.11") is None
        fresh = host.spawn(_spec("pump-fresh", group="cell", key="c.11"))
        assert fresh.handle.key == "c.11"
        assert host.find("cell", "c.11").handle == fresh.handle
    finally:
        host.shutdown(5.0)


def test_pump_start_phase_stays_invisible_until_adjudicated(host: ActorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    if _native_without_conformance_hooks(host):
        pytest.skip("native pump-start holds exist only in the conformance build; KEY-12 to KEY-14 cover native")
    native = host.engine._module.__name__.endswith("._core.actors")
    held = threading.Event()
    release = threading.Event()
    if native:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_HOLD", "1")
        host.shutdown(5.0)
        host = create_actor_host(_config())
    else:
        real_start = threading.Thread.start

        def held_start(thread: threading.Thread, *args: object, **kwargs: object) -> object:
            if thread.name == "ref-held-port":
                started = real_start(thread, *args, **kwargs)
                held.set()
                assert release.wait(5.0)
                return started
            return real_start(thread, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", held_start)
    spawned: list[object] = []

    def run() -> None:
        try:
            spawned.append(host.spawn(_spec("held-port", group="cell", key="c.12")))
        except BaseException as exc:
            spawned.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    if native:
        _wait_until(lambda: host.engine._native._test_pump_start_hold_reached())
    else:
        assert held.wait(5.0)
    finder_done = threading.Event()
    finder_arrived = threading.Event()
    found: list[object] = []

    def run_find() -> None:
        finder_arrived.set()
        found.append(host.find("cell", "c.12"))
        finder_done.set()

    if native:
        assert host.find("cell", "c.12") is None
        with pytest.raises(ActorSpawnRefused, match="key-in-use"):
            host.spawn(_spec("second", group="cell", key="c.12"))
        counts = host.engine._native._test_registry_counts()
        assert (counts[0], counts[1]) == (0, 1)
    else:
        finder = threading.Thread(target=run_find)
        finder.start()
        assert finder_arrived.wait(5.0)
        assert not finder_done.is_set()
        assert found == []
    if native:
        host.engine._native._test_release_pump_start_hold()
    release.set()
    thread.join(5.0)
    assert not thread.is_alive()
    assert not isinstance(spawned[0], BaseException)
    assert host.find("cell", "c.12").handle == spawned[0].handle
    if not native:
        assert finder_done.wait(5.0)
        assert found[0] is not None
        assert found[0].handle == spawned[0].handle
    assert host.shutdown(5.0) is True
    assert host.find("cell", "c.12") is None


def test_shutdown_during_pump_start_returns_refusal_or_normal_stop(host: ActorHost, monkeypatch: pytest.MonkeyPatch) -> None:
    if _native_without_conformance_hooks(host):
        pytest.skip("native pump-start holds exist only in the conformance build; KEY-12 to KEY-14 cover native")
    native = host.engine._module.__name__.endswith("._core.actors")
    held = threading.Event()
    release = threading.Event()
    if native:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_HOLD", "1")
        host.shutdown(5.0)
        host = create_actor_host(_config())
    else:
        real_start = threading.Thread.start

        def held_start(thread: threading.Thread, *args: object, **kwargs: object) -> object:
            if thread.name == "ref-held-shutdown":
                started = real_start(thread, *args, **kwargs)
                held.set()
                assert release.wait(5.0)
                return started
            return real_start(thread, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", held_start)
    spawned: list[object] = []

    def run() -> None:
        try:
            spawned.append(host.spawn(_spec("held-shutdown", group="cell", key="c.13")))
        except BaseException as exc:
            spawned.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    if native:
        _wait_until(lambda: host.engine._native._test_pump_start_hold_reached())
    else:
        assert held.wait(5.0)
    if native:
        assert host.shutdown(5.0) is True
        host.engine._native._test_release_pump_start_hold()
        release.set()
        thread.join(5.0)
        assert isinstance(spawned[0], ActorSpawnRefused)
        assert "refused" in str(spawned[0])
    else:
        shutdown_arrived = threading.Event()
        shutdown_done = threading.Event()
        shutdown_ok: list[bool] = []

        def run_shutdown() -> None:
            shutdown_arrived.set()
            shutdown_ok.append(host.shutdown(30.0))
            shutdown_done.set()

        stopper = threading.Thread(target=run_shutdown)
        stopper.start()
        assert shutdown_arrived.wait(5.0)
        assert not shutdown_done.is_set()
        release.set()
        thread.join(5.0)
        stopper.join(5.0)
        assert not isinstance(spawned[0], BaseException)
        assert shutdown_ok == [True]
    assert host.find("cell", "c.13") is None


def test_pump_start_failure_and_shutdown_arbitrate_without_residue(
    host: ActorHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _native_without_conformance_hooks(host):
        pytest.skip("native pump-start holds exist only in the conformance build; KEY-12 to KEY-14 cover native")
    native = host.engine._module.__name__.endswith("._core.actors")
    held = threading.Event()
    arrived = threading.Event()
    shutdown_done = threading.Event()
    gate = threading.Event()
    guard = 30.0
    if native:
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD", "1")
        monkeypatch.setenv("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE", "1")
        host.shutdown(5.0)
        host = create_actor_host(_config())
    else:
        real_start = threading.Thread.start

        def fail_after_park(parked: threading.Thread, *args: object, **kwargs: object) -> object:
            if parked.name == "ref-race-fail":
                held.set()
                assert gate.wait(guard)
                raise RuntimeError("injected pump start failure")
            return real_start(parked, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", fail_after_park)
    spawned: list[object] = []

    def run() -> None:
        try:
            spawned.append(host.spawn(_spec("race-fail", group="cell", key="c.race")))
        except BaseException as exc:
            spawned.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    if native:
        _wait_until(lambda: host.engine._native._test_pump_start_hold_reached(), timeout=guard)
    else:
        assert held.wait(guard)
    results: list[bool] = []

    def run_shutdown() -> None:
        arrived.set()
        results.append(host.shutdown(guard))
        shutdown_done.set()

    stopper = threading.Thread(target=run_shutdown)
    stopper.start()
    assert arrived.wait(guard)
    if native:
        assert shutdown_done.wait(guard)
        assert results == [True]
        host.engine._native._test_release_pump_start_hold()
    else:
        assert not shutdown_done.is_set()
        gate.set()
    thread.join(guard)
    stopper.join(guard)
    assert not thread.is_alive()
    assert not stopper.is_alive()
    assert results == [True]
    failed = spawned[0]
    assert isinstance(failed, BaseException)
    if native:
        assert type(failed).__name__ == "ActorSpawnError"
        message = str(failed)
        assert message.startswith("engine failed to start actor")
        assert message.endswith(": injected pump start failure")
        assert host.engine._native._test_registry_counts() == (0, 0, 0)
    else:
        assert type(failed).__name__ == "RuntimeError"
        assert str(failed) == "injected pump start failure"
    assert host.find("cell", "c.race") is None


def test_keyless_handle_repr_and_hash_keep_the_origin_dev_shape() -> None:
    keyless = ActorHandle("act-00000001", "worker")
    assert repr(keyless) == "ActorHandle(actor_id='act-00000001', name='worker')"
    assert keyless.__hash__() == hash(("act-00000001", "worker"))
    assert keyless == ActorHandle("act-00000001", "worker")
    keyed = ActorHandle("act-00000001", "worker", "cell", "c.1")
    assert repr(keyed) == (
        "ActorHandle(actor_id='act-00000001', name='worker', group='cell', key='c.1')"
    )
    assert keyed.__hash__() == hash(("act-00000001", "worker", "cell", "c.1"))
    assert keyed != keyless


@pytest.mark.parametrize("backend", ["reference-abi", "native"])
def test_keyless_event_log_field_set_is_exactly_the_origin_dev_fields(
    backend: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_ACTOR_BACKEND", backend)
    made = create_actor_host(_config())
    try:
        with caplog.at_level(logging.INFO, logger="hyprial.actor_runtime"):
            keyed = made.spawn(_spec("keyed-log", group="cell", key="c.log"))
            plain = made.spawn(_spec("plain-log"))
            keyed.tell("go")
            plain.tell("go")
            _wait_until(
                lambda: len([r for r in caplog.records if hasattr(r, "actor_event")]) >= 2
            )
    finally:
        made.shutdown(5.0)
    payloads = {
        record.actor_event["actorName"]: record.actor_event
        for record in caplog.records
        if hasattr(record, "actor_event")
    }
    assert set(payloads) >= {"keyed-log", "plain-log"}
    base = {"kind", "actorId", "actorName", "generation", "commandType", "code", "restartDelay"}
    assert set(payloads["plain-log"]) == base
    assert set(payloads["keyed-log"]) == base | {"actorGroup", "actorKey"}
    assert payloads["keyed-log"]["actorGroup"] == "cell"
    assert payloads["keyed-log"]["actorKey"] == "c.log"
