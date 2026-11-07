from __future__ import annotations

import ast
import threading
import time
import sys
import traceback
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import ActorState, AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.composition import (
    CorrelatedDomainEvents,
    DomainCommandError,
    HarnessPortClient,
)
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.processes.process_owner import ProcessOwner
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessSessionRefsReconciled,
    RefreshHarnessProjectionsCommand,
    RemoveHarnessCommand,
)
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from tests.support.gates.session_ref_authority_gate import session_ref_store_writers


SOURCE_ROOT = Path(__file__).resolve().parents[4] / 'src/hyprial'
ACTOR_SOURCE = SOURCE_ROOT / "daemon/impl/harnesses/actor/behaviors/reconcile.py"
STATE_SOURCE = SOURCE_ROOT / "daemon/impl/state_persistence/ports.py"
EXPECTED_WRITER = (
    "daemon/impl/state_persistence/ports.py:_Generation.__call__",
)
EXPECTED_SUBMITTER = ("_ReconcileBehavior._complete_session_ref_reconcile",)


def _logical_submitters(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    return tuple(
        f"{owner.name}.{method.name}"
        for owner in ast.walk(tree)
        if isinstance(owner, ast.ClassDef)
        for method in owner.body
        if isinstance(method, ast.FunctionDef)
        for call in ast.walk(method)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_persist_desired"
        and call.args
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "SYNC_HARNESS_SESSION_REFS"
    )


def test_harness_actor_is_the_only_session_ref_store_writer() -> None:
    assert session_ref_store_writers(SOURCE_ROOT) == EXPECTED_WRITER
    assert _logical_submitters(ACTOR_SOURCE.read_text()) == EXPECTED_SUBMITTER


def test_reverse_mutation_missing_actor_session_ref_writer_is_rejected() -> None:
    source = STATE_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace(
        "self.desired.sync_harness_session_refs(",
        "self.desired.ignored_sync_harness_session_refs(",
        1,
    )
    assert mutated != source
    assert session_ref_store_writers(
        SOURCE_ROOT, source_overrides={STATE_SOURCE: mutated}
    ) != EXPECTED_WRITER


def test_reverse_mutation_missing_logical_harness_submitter_is_rejected() -> None:
    source = ACTOR_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace(
        "DesiredStateOperation.SYNC_HARNESS_SESSION_REFS,",
        "DesiredStateOperation.IGNORED_SYNC_HARNESS_SESSION_REFS,",
        1,
    )
    assert mutated != source
    assert _logical_submitters(mutated) != EXPECTED_SUBMITTER


class _SessionProcess:
    def __init__(self, session_ref: str | None) -> None:
        self.running = True
        self.session_ref = session_ref
        self.pid = None

    def stop(self) -> None:
        self.running = False


class _SessionLauncher:
    def __init__(self) -> None:
        self.specs: list[HarnessLaunchSpec] = []
        self.processes: list[_SessionProcess] = []

    def start(self, spec: HarnessLaunchSpec) -> _SessionProcess:
        self.specs.append(spec)
        process = _SessionProcess(spec.session_ref)
        self.processes.append(process)
        return process


@pytest.mark.parametrize("restart_guardian", [False, True])
def test_reconcile_joins_exact_native_ref_before_durable_reply(
    tmp_path: Path, monkeypatch, restart_guardian: bool,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(HarnessLaunchSpec("pi", "worker", True))
    events = CorrelatedDomainEvents()
    launcher = _SessionLauncher()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=store)
    harnesses = HarnessPortClient(actor, events)
    harnesses.restore(store.load().harnesses)
    launcher.processes[0].session_ref = "fresh-native-ref"
    entered = threading.Event()
    release = threading.Event()
    original = ProcessOwner._read_facts
    read_threads: list[str] = []

    def held_read(owner: ProcessOwner):
        read_threads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(3)
        return original(owner)

    monkeypatch.setattr(ProcessOwner, "_read_facts", held_read)
    replies: list[dict[tuple[str, str], str]] = []
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            replies.append(harnesses.reconcile_session_refs())
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=reconcile, daemon=True)
    waiter.start()
    try:
        assert entered.wait(2)
        assert waiter.is_alive()
        assert store.load().harnesses[0].session_ref is None
        assert all("process-native-io" in name for name in read_threads)
        if restart_guardian:
            previous = actor._handler_generation
            crash_once = True

            def fail_once(_event: object) -> None:
                nonlocal crash_once
                if crash_once:
                    crash_once = False
                    raise RuntimeError("synthetic harness guardian restart")

            actor.subscribe_events(fail_once)
            assert actor._admit(
                RefreshHarnessProjectionsCommand("guardian-probe")
            ) is AdmissionResult.ACCEPTED
            deadline = time.monotonic() + 2
            while actor._handler_generation == previous or (
                actor._runtime.snapshot(actor._handle).state is not ActorState.RUNNING
            ):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert waiter.is_alive()
        release.set()
        waiter.join(3)
        assert not waiter.is_alive()
        assert errors == []
        assert replies == [{("pi", "worker"): "fresh-native-ref"}]
        assert store.load().harnesses[0].session_ref == "fresh-native-ref"
    finally:
        release.set()
        waiter.join(3)
        harnesses.stop(2.0)


def test_timed_out_session_ref_edge_keeps_native_custody_without_late_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(HarnessLaunchSpec("pi", "worker", True))
    launcher = _SessionLauncher()
    actor = HarnessRuntimeActor(launcher, desired_state=store)
    facade = HarnessRuntimeFacade(actor, reply_timeout=0.03)
    facade.restore(store.load().harnesses)
    launcher.processes[0].session_ref = "late-ref"
    entered = threading.Event()
    release = threading.Event()
    original = ProcessOwner._read_facts

    def held_read(owner: ProcessOwner):
        entered.set()
        assert release.wait(3)
        return original(owner)

    monkeypatch.setattr(ProcessOwner, "_read_facts", held_read)
    try:
        with pytest.raises(TimeoutError, match="reply deadline elapsed"):
            facade.reconcile_session_refs()
        assert entered.is_set()
        assert store.load().harnesses[0].session_ref is None
        release.set()
        deadline = time.monotonic() + 2
        while actor._io.in_flight:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert store.load().harnesses[0].session_ref is None
    finally:
        release.set()
        facade.stop(2.0)


def test_concurrent_ref_reconcile_cannot_resurrect_ref_across_restart(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    initial = HarnessLaunchSpec("pi", "worker", True, args=("--old",))
    store.upsert_harness(initial)
    events = CorrelatedDomainEvents()
    launcher = _SessionLauncher()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=store)
    harnesses = HarnessPortClient(actor, events)
    harnesses.restore(store.load().harnesses)
    launcher.processes[0].session_ref = "stale-native-ref"

    remove = LifecycleMutationRequest(
        "restart-remove",
        "restart-remove-attempt",
        "operator-restart",
        None,
        RemoveHarnessCommand("restart-remove-payload", "pi", "worker"),
    )
    barrier = threading.Barrier(2)
    reconcile_errors: list[BaseException] = []

    def reconcile() -> None:
        barrier.wait()
        try:
            harnesses.reconcile_session_refs()
        except BaseException as error:
            reconcile_errors.append(error)

    def remove_for_restart() -> None:
        barrier.wait()
        assert actor.submit(remove) is PortAdmission.ACCEPTED

    reconcile_thread = threading.Thread(target=reconcile)
    remove_thread = threading.Thread(target=remove_for_restart)
    reconcile_thread.start()
    remove_thread.start()
    reconcile_thread.join(3.0)
    remove_thread.join(3.0)
    reconcile_frame = sys._current_frames().get(reconcile_thread.ident)
    assert not reconcile_thread.is_alive(), (
        "".join(traceback.format_stack(reconcile_frame))
        if reconcile_frame is not None else "thread exited during capture"
    )
    assert not remove_thread.is_alive()
    assert reconcile_errors == []
    removed = events.wait(
        remove.correlation_id, LifecycleMutationCompleted, timeout=3.0
    )
    assert store.load().harnesses == ()

    fresh = HarnessLaunchProjection("pi", "worker", True, args=("--fresh",))
    ensure = LifecycleMutationRequest(
        "restart-ensure",
        "restart-ensure-attempt",
        "operator-restart",
        removed.provenance.resource_token,
        EnsureHarnessCommand("restart-ensure-payload", fresh),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    events.wait(ensure.correlation_id, LifecycleMutationCompleted, timeout=3.0)
    [persisted] = store.load().harnesses
    assert persisted.args == ("--fresh",)
    assert persisted.session_ref is None
    harnesses.stop(2.0)

    restarted_events = CorrelatedDomainEvents()
    restarted_launcher = _SessionLauncher()
    restarted_actor = HarnessRuntimeActor(
        restarted_launcher,
        event_sink=restarted_events,
        desired_state=store,
    )
    restarted = HarnessPortClient(restarted_actor, restarted_events)
    restarted.restore(store.load().harnesses)
    [restored] = restarted_launcher.specs
    assert restored.args == ("--fresh",)
    assert restored.session_ref is None
    restarted.stop(2.0)


def test_ref_observation_captured_before_remove_settles_after_process_closes(tmp_path, monkeypatch):
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(HarnessLaunchSpec("pi", "worker", True))
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(_SessionLauncher(), event_sink=events, desired_state=store)
    harnesses = HarnessPortClient(actor, events)
    harnesses.restore(store.load().harnesses)
    entered, release = threading.Event(), threading.Event()
    original = ProcessOwner.observe_facts
    replies, errors = [], []

    def captured(owner):
        entered.set()
        assert release.wait(3)
        return original(owner)

    monkeypatch.setattr(ProcessOwner, "observe_facts", captured)

    def reconcile():
        try:
            replies.append(harnesses.reconcile_session_refs())
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=reconcile)
    remove = LifecycleMutationRequest(
        "remove-before-observation", "remove-before-observation-attempt", "operator", None,
        RemoveHarnessCommand("remove-before-observation-payload", "pi", "worker"),
    )
    try:
        waiter.start()
        assert entered.wait(2)
        assert actor.submit(remove) is PortAdmission.ACCEPTED
        events.wait(remove.correlation_id, LifecycleMutationCompleted, timeout=3)
        assert store.load().harnesses == ()
        release.set()
        waiter.join(3)
        assert not waiter.is_alive()
        assert errors == []
        assert replies == [{}]
        assert store.load().harnesses == ()
    finally:
        release.set()
        waiter.join(3)
        harnesses.stop(2)


@pytest.mark.parametrize("completion_order", ["normal", "replay-first", "cancel-retry"])
def test_ref_reobservation_during_remove_keeps_its_completion(
    tmp_path, monkeypatch, completion_order,
):
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(HarnessLaunchSpec("pi", "worker", True))
    events = CorrelatedDomainEvents()
    launcher = _SessionLauncher()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=store)
    harnesses = HarnessPortClient(actor, events, timeout=2)
    harnesses.restore(store.load().harnesses)
    facade = HarnessRuntimeFacade(actor, reply_timeout=2)
    emitted = []
    actor.subscribe_events(emitted.append)
    launcher.processes[0].session_ref = "old-native-ref"
    observed, release_observation = threading.Event(), threading.Event()
    stopping, release_stop = threading.Event(), threading.Event()
    reobserving = threading.Event()
    original_observe = ProcessOwner.observe_facts
    original_stop = _SessionProcess.stop
    original_completed = actor._on_call_completed
    completions = []
    observations = []
    replies, errors = [], []

    def completed(event):
        if event.correlation_id.startswith("harness:session-refs:"):
            completions.append(event)
        original_completed(event)

    def observe(owner):
        observations.append(owner.process_token)
        if len(observations) > 1:
            reobserving.set()
            return original_observe(owner)
        facts = original_observe(owner)
        observed.set()
        assert release_observation.wait(3)
        return facts

    def stop(process):
        stopping.set()
        assert release_stop.wait(6)
        original_stop(process)

    monkeypatch.setattr(ProcessOwner, "observe_facts", observe)
    monkeypatch.setattr(_SessionProcess, "stop", stop)
    monkeypatch.setattr(actor, "_on_call_completed", completed)

    def reconcile():
        try:
            client = facade if completion_order == "cancel-retry" else harnesses
            replies.append(client.reconcile_session_refs())
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=reconcile)
    remove = LifecycleMutationRequest(
        "remove-during-observation", "remove-during-observation-attempt", "operator", None,
        RemoveHarnessCommand("remove-during-observation-payload", "pi", "worker"),
    )
    try:
        waiter.start()
        assert observed.wait(2)
        assert actor.submit(remove) is PortAdmission.ACCEPTED
        assert stopping.wait(2)
        # The fleet generation changed but removal is still in flight. The
        # first observation must be replaced by a second native observation.
        release_observation.set()
        assert reobserving.wait(2)
        if completion_order == "replay-first":
            # Replaying the first receipt must not consume the second
            # observation's pending call or publish a stale session ref.
            assert actor._admit(completions[0]) is AdmissionResult.ACCEPTED
        if completion_order == "cancel-retry":
            # The facade times out after the second observation was admitted.
            # It cancels the public edge, not the accepted native Stop/read.
            waiter.join(3)
            assert not waiter.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
            assert actor._io.in_flight > 0
        release_stop.set()
        events.wait(remove.correlation_id, LifecycleMutationCompleted, timeout=3)
        waiter.join(3)
        assert not waiter.is_alive()
        if completion_order == "cancel-retry":
            deadline = time.monotonic() + 2
            while actor._io.in_flight:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            assert replies == []
            assert not any(isinstance(event, HarnessSessionRefsReconciled) for event in emitted)
        else:
            assert errors == []
            assert len(replies) == 1
        assert store.load().harnesses == ()
    finally:
        release_observation.set()
        release_stop.set()
        waiter.join(3)
        harnesses.stop(2)


def test_ref_native_failure_rejects_the_public_call(tmp_path, monkeypatch):
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(HarnessLaunchSpec("pi", "worker", True))
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(_SessionLauncher(), event_sink=events, desired_state=store)
    harnesses = HarnessPortClient(actor, events, timeout=2)
    harnesses.restore(store.load().harnesses)

    def fail_observation(owner):
        raise RuntimeError("native facts unavailable")

    monkeypatch.setattr(ProcessOwner, "observe_facts", fail_observation)
    try:
        with pytest.raises(DomainCommandError, match="native facts unavailable") as failure:
            harnesses.reconcile_session_refs()
        assert failure.value.code == "HARNESS_IO_FAILED"
        assert store.load().harnesses[0].session_ref is None
    finally:
        harnesses.stop(2)
