from __future__ import annotations

import threading
import time
import pytest
from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig

from ..support import (
    _DesiredState,
    _Launcher,
    _Process,
    _STATUS_SCHEMA,
    _gateway,
)


def test_start_and_stop_commit_desired_intent_inside_actor_effect() -> None:
    desired = _DesiredState()
    launcher = _Launcher()
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=desired,
    )
    try:
        assert runtime.start("a") is True
        assert desired.active == {"a"}
        assert desired.operations == [("activate", "a")]
        assert runtime.remove("a") is True
        assert desired.active == set()
        assert desired.operations == [("activate", "a"), ("deactivate", "a")]
    finally:
        runtime.stop()


def test_stop_failure_keeps_desired_intent_and_reconcile_can_recover() -> None:
    desired = _DesiredState()
    fail_stop = True

    class FailingStopProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            nonlocal fail_stop
            del timeout
            if fail_stop:
                raise RuntimeError("injected stop failure")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            self.calls += 1
            return FailingStopProcess(self.calls)

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=desired,
        retry_interval=0.0,
    )
    try:
        assert runtime.start("a") is True
        with pytest.raises(RuntimeError, match="injected stop failure"):
            runtime.remove("a")
        assert desired.active == {"a"}
        assert ("deactivate", "a") not in desired.operations
        status = runtime.status("a")[0]
        assert status["desired"] is True
        assert status["processRunning"] is True

        fail_stop = False
        assert runtime.remove("a") is True
        assert desired.active == set()
    finally:
        fail_stop = False
        runtime.stop()


def test_start_persistence_failure_stops_uncommitted_process() -> None:
    process = _Process(71)

    class Desired(_DesiredState):
        def activate(self, name: str) -> bool:
            del name
            raise OSError("injected desired persistence failure")

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return process

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=Desired(),
    )
    try:
        with pytest.raises(AdapterStartError, match="persistence failure"):
            runtime.start("a")
        assert process.stopped is True
        assert runtime._processes == {}
        status = runtime.status("a")[0]
        assert status["desired"] is False
        assert status["processRunning"] is False
    finally:
        runtime.stop()


def test_stop_effect_completion_reassociates_after_actor_generation_restart() -> None:
    desired = _DesiredState()
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class BlockingStopProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            stop_entered.set()
            assert release_stop.wait(3.0)
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            self.calls += 1
            return BlockingStopProcess(self.calls)

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=desired,
    )
    assert runtime.start("a") is True
    failures: list[BaseException] = []

    def stop() -> None:
        try:
            runtime.remove("a")
        except BaseException as error:
            failures.append(error)

    caller = threading.Thread(target=stop)
    caller.start()
    assert stop_entered.wait(1.0)
    handle = runtime._handle
    assert handle is not None
    assert runtime._runtime.tell(handle, object()).value == "accepted"
    deadline = time.monotonic() + 2.0
    while runtime.generation == 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.generation > 1
    release_stop.set()
    caller.join(2.0)
    # `generation > 1` is bumped at the START of the restart
    # (begin_generation); the old mailbox is closed and the new generation
    # is not admitting yet, so status()/stop() answer "adapter runtime is
    # closing" (AdmissionResult.CLOSED via _submit) if they land in that
    # window -- which a loaded machine widens (CI #1530, card 2389ace2).
    # Wait for the milestone the calls actually need -- the real call
    # succeeding -- and nothing else: a probe message to the LIVE
    # generation is not harmless (an unknown object crashes it again).
    deadline = time.monotonic() + 2.0
    while True:
        try:
            runtime.status("a")
            break
        except RuntimeError as error:
            if "closing" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        assert failures and "generation restarted" in str(failures[0])
        # Wait for the values this test actually asserts, not for a
        # predecessor of them.  Waiting on `desired.active` (the store) and
        # then asserting `runtime.status(...)` (the projection) leaves the
        # gap between the two unwaited: the store empties first, and on a
        # loaded machine the projection has not caught up when the assert
        # runs.  That produced an intermittent `assert True is False` on CI
        # while passing every time locally -- it needs the machine to be slow
        # enough to lose the race, which is the shape that reads as "flaky on
        # Linux only".
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            status = runtime.status("a")[0]
            if (
                not desired.active
                and status["desired"] is False
                and status["processRunning"] is False
            ):
                break
            time.sleep(0.01)
        assert desired.active == set()
        status = runtime.status("a")[0]
        assert status["desired"] is False
        assert status["processRunning"] is False
    finally:
        release_stop.set()
        runtime.stop()


def test_shutdown_fences_blocked_spawn_and_terminates_its_caller() -> None:
    entered = threading.Event()
    release = threading.Event()
    processes: list[_Process] = []

    class BlockedLauncher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            entered.set()
            assert release.wait(3.0)
            process = _Process(41)
            processes.append(process)
            return process

    runtime = AdapterRuntime(
        BlockedLauncher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
    )
    failures: list[BaseException] = []

    def start() -> None:
        try:
            runtime.start("a")
        except BaseException as error:
            failures.append(error)

    caller = threading.Thread(target=start)
    caller.start()
    assert entered.wait(1.0)

    began = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out|did not drain"):
        runtime.stop(timeout=0.2)
    assert time.monotonic() - began < 1.0
    caller.join(1.0)
    assert caller.is_alive() is False
    assert failures and "stopped before completion" in str(failures[0])

    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not processes:
        time.sleep(0.01)
    assert len(processes) == 1
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not processes[0].stopped:
        time.sleep(0.01)
    assert processes[0].stopped is True
    assert runtime._processes == {}
    assert runtime.stop() is True


def test_shutdown_reports_blocked_process_stop_until_custody_releases() -> None:
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class BlockedStopProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            stop_entered.set()
            assert release_stop.wait(3.0)
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return BlockedStopProcess(91)

    runtime = AdapterRuntime(
        Launcher(), ChannelConfiguration(gateways=(_gateway("a"),))
    )
    assert runtime.start("a") is True
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out|did not drain"):
        runtime.stop(timeout=0.2)
    assert time.monotonic() - began < 1.0
    assert stop_entered.is_set()
    release_stop.set()
    assert runtime.stop(timeout=2.0) is True


def test_a_worker_that_refused_to_stop_stays_registered_desired_and_running() -> None:
    """A failed rollback must degrade the entry to unsettled, never to absent.

    The effect side already keeps a worker that refused to stop, but actor
    completion then discarded the desired entry and the observation. The
    projection therefore reported "should not be running" while the process
    ran, and reconcile adopted nothing because desired was empty -- the worker
    was stranded, reachable by neither reconcile nor shutdown.
    """

    stop_allowed = threading.Event()

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            if not stop_allowed.is_set():
                raise RuntimeError("worker refused to stop")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(77)

    class RefusingDesiredState:
        def activate(self, name: str) -> bool:
            del name
            raise RuntimeError("desired persistence failed")

        def deactivate(self, name: str) -> bool:
            del name
            return True

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=RefusingDesiredState(),
    )

    with pytest.raises(Exception):
        runtime.start("a")

    # The worker is alive, so every record of it must survive.
    [status] = runtime.status("a")
    assert status["processRunning"] is True, "the process really is still running"
    assert status["desired"] is True, "desired must not be discarded for a live worker"
    assert "a" in runtime.gateway_names

    # A structured signal, not just a transient error string.
    assert runtime.last_errors.get("a")

    # Reconcile must not spawn a second worker on top of the live one.
    spawned_before = runtime.status("a")[0]["pid"]
    runtime.reconcile()
    assert runtime.status("a")[0]["pid"] == spawned_before

    # Survives a refresh: one healthy probe used to clear the only signal.
    runtime.reconcile()
    assert runtime.last_errors.get("a"), "the stranded-worker signal vanished"

    # Once the worker can be stopped, the entry is reclaimable.
    stop_allowed.set()
    assert runtime.stop(timeout=2.0) is True


def test_shutdown_does_not_report_success_while_a_worker_is_still_held(
) -> None:
    """Success must mean the registry is empty, and a retry must reclaim it.

    Not clearing the registry first was only half the fix: shutdown still
    returned true, so the caller was told shutdown had finished while an
    adapter was running, and a second stop only waited on already-finished
    pending work instead of retrying the worker it was holding. Retention with
    no path back is not retention.
    """

    stop_allowed = threading.Event()
    calls: list[str] = []

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            calls.append("stop")
            if not stop_allowed.is_set():
                raise RuntimeError("shutdown stop failed")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(82)

    runtime = AdapterRuntime(
        Launcher(), ChannelConfiguration(gateways=(_gateway("a"),))
    )
    assert runtime.start("a") is True

    with pytest.raises(RuntimeError, match="still holds a"):
        runtime.stop(timeout=0.5)
    assert runtime._processes, "custody must survive a refused stop"
    assert calls == ["stop"]

    # The second public stop retries the worker it is holding.
    stop_allowed.set()
    assert runtime.stop(timeout=2.0) is True
    assert len(calls) == 2, "a second stop must retry, not just drain pending"
    assert not runtime._processes


def test_the_unsettled_code_is_public_and_survives_a_healthy_refresh() -> None:
    """A caller needs a code to branch on, not just prose in `error`."""

    stop_allowed = threading.Event()

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            if not stop_allowed.is_set():
                raise RuntimeError("worker refused to stop")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(83)

    class RefusingDesiredState:
        def activate(self, name: str) -> bool:
            del name
            raise RuntimeError("desired persistence failed")

        def deactivate(self, name: str) -> bool:
            del name
            return True

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=RefusingDesiredState(),
    )
    with pytest.raises(Exception):
        runtime.start("a")

    [status] = runtime.status("a")
    assert status["unsettled"] == "WORKER_STOP_FAILED"

    runtime.reconcile()
    [status] = runtime.status("a")
    assert status["unsettled"] == "WORKER_STOP_FAILED", "a healthy probe cleared it"

    stop_allowed.set()
    assert runtime.stop(timeout=2.0) is True


def test_discard_stop_failure_keeps_the_worker_registered() -> None:
    """Direct _ProcessEffects seam: there is no public stale-discard entry.

    A previous attempt tested this through reload_gateways and passed against
    the unfixed code -- measuring showed stop was never called, so the test
    asserted a tautology. This drives the operation itself, and the old
    pop-before-stop implementation fails it deterministically.
    """

    from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects

    results: list[object] = []
    calls: list[str] = []
    stop_allowed = threading.Event()

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            calls.append("stop")
            if not stop_allowed.is_set():
                raise RuntimeError("discard stop failed")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(90)

    class NullDesired:
        def activate(self, name: str) -> bool:
            del name
            return False

        def deactivate(self, name: str) -> bool:
            del name
            return False

    done = threading.Event()

    def completion(result: object) -> None:
        results.append(result)
        done.set()

    effects = _ProcessEffects(
        Launcher(), completion, NullDesired(), start_confirm_timeout=1.0
    )
    try:
        token = "attempt-1"
        effects.submit(
            correlation_id="c1",
            generation=1,
            version=1,
            name="a",
            operation="spawn",
            gateway=_gateway("a"),
            attempt_token=token,
        )
        assert done.wait(3.0)
        done.clear()

        effects.submit(
            correlation_id="c2",
            generation=1,
            version=2,
            name="a",
            operation="discard",
            payload=token,
            attempt_token=token,
        )
        assert done.wait(3.0)

        failure = results[-1]
        assert getattr(failure, "succeeded") is False
        assert getattr(failure, "code") == "WORKER_STOP_FAILED"
        assert effects.retained() == ("a",), "a live worker must stay registered"
        assert calls == ["stop"]
    finally:
        stop_allowed.set()
        effects.stop_retained(1.0)
        effects.close(1.0)


def test_a_second_stop_does_not_race_an_in_flight_stop() -> None:
    """Drain before retrying: two stops on one worker is a different bug."""

    release = threading.Event()
    entered = threading.Event()
    calls: list[str] = []

    class BlockingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            calls.append("stop")
            entered.set()
            assert release.wait(3.0)
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return BlockingProcess(85)

    runtime = AdapterRuntime(
        Launcher(), ChannelConfiguration(gateways=(_gateway("a"),))
    )
    assert runtime.start("a") is True

    # The exact message depends on which guard trips first; the point here is
    # that the first stop does not complete while the worker is blocked.
    with pytest.raises(RuntimeError):
        runtime.stop(timeout=0.2)
    assert entered.is_set()

    second = threading.Thread(
        target=lambda: runtime.stop(timeout=0.2), daemon=True
    )
    second.start()
    try:
        time.sleep(0.15)
        assert calls == ["stop"], "a second stop must not re-enter a live stop"
    finally:
        # Both stops above are expected to fail, so nothing has torn this
        # runtime down. Leaving it alive leaks its actor and effect threads
        # into the rest of the suite, where they perturb unrelated
        # restart/reassociation tests -- close what you open.
        release.set()
        second.join(timeout=3.0)
        for _ in range(5):
            try:
                if runtime.stop(timeout=2.0):
                    break
            except RuntimeError:
                time.sleep(0.05)


def test_shutdown_result_reports_failure_and_code_for_a_stranded_worker() -> None:
    """Pin the _IoResult contract itself.

    A previous submission claimed reverting this reddened a test; it did not.
    The mutation actually run was the stop() raise, a different change, so the
    result contract was never covered. This drives the effects seam so the
    succeeded/code fields are asserted directly.
    """

    from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects

    results: list[object] = []
    done = threading.Event()
    stop_allowed = threading.Event()

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            if not stop_allowed.is_set():
                raise RuntimeError("shutdown stop failed")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(86)

    class NullDesired:
        def activate(self, name: str) -> bool:
            del name
            return False

        def deactivate(self, name: str) -> bool:
            del name
            return False

    effects = _ProcessEffects(
        Launcher(),
        lambda result: (results.append(result), done.set()),
        NullDesired(),
        start_confirm_timeout=1.0,
    )
    try:
        effects.submit(
            correlation_id="s1",
            generation=1,
            version=1,
            name="a",
            operation="spawn",
            gateway=_gateway("a"),
            attempt_token="t1",
        )
        assert done.wait(3.0)
        done.clear()

        effects.submit(
            correlation_id="s2",
            generation=1,
            version=1,
            name="*",
            operation="shutdown",
            payload=time.monotonic() + 1.0,
        )
        assert done.wait(3.0)

        shutdown = results[-1]
        assert getattr(shutdown, "succeeded") is False
        assert getattr(shutdown, "code") == "WORKER_STOP_FAILED"
        assert getattr(shutdown, "value") == ("a",)
    finally:
        stop_allowed.set()
        effects.stop_retained(1.0)
        effects.close(1.0)


def test_a_partial_settlement_keeps_the_still_held_worker_running() -> None:
    """Settlement cardinality: all released, all held, and one of each.

    The single-adapter case hid this. Clearing every gateway and hardcoding
    processRunning=False meant a mixed result reported the worker that was
    still refusing -- still registered, still running -- as stopped, which is
    the exact false-negative this whole line of work exists to remove.
    """

    allow = {"a": threading.Event(), "b": threading.Event()}

    class Refusing(_Process):
        def __init__(self, pid: int, key: str) -> None:
            super().__init__(pid)
            self.key = key

        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            if not allow[self.key].is_set():
                raise RuntimeError(f"{self.key} refused to stop")
            super().stop()

    made: list[Refusing] = []

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            key = gateway.name
            process = Refusing(90 + len(made), key)
            made.append(process)
            return process

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"), _gateway("b"))),
    )
    assert runtime.start("a") is True
    assert runtime.start("b") is True

    with pytest.raises(RuntimeError):
        runtime.stop(timeout=0.5)

    # Mixed: a releases, b keeps refusing.
    allow["a"].set()
    with pytest.raises(RuntimeError, match="still holds b"):
        runtime.stop(timeout=1.0)

    [status_a] = runtime.status("a")
    [status_b] = runtime.status("b")

    # Full schema on both, not just the fields that once went missing.
    assert _STATUS_SCHEMA <= set(status_a), sorted(_STATUS_SCHEMA - set(status_a))
    assert _STATUS_SCHEMA <= set(status_b), sorted(_STATUS_SCHEMA - set(status_b))

    # The released one is settled.
    assert status_a["processRunning"] is False
    assert "unsettled" not in status_a
    assert "pid" not in status_a

    # The still-held one must keep telling the truth.
    assert status_b["processRunning"] is True, "a held worker is still running"
    assert status_b["unsettled"] == "WORKER_STOP_FAILED"
    assert "pid" in status_b, "a running worker keeps its pid"

    # The last worker releasing flips it to stopped and clears the debt.
    allow["b"].set()
    assert runtime.stop(timeout=2.0) is True
    [final_b] = runtime.status("b")
    assert _STATUS_SCHEMA <= set(final_b)
    assert final_b["processRunning"] is False
    assert "unsettled" not in final_b
    assert "pid" not in final_b
