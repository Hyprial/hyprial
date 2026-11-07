from __future__ import annotations

import threading
import time
from pathlib import Path
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand, CreateAgentCommand, DestroyAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
import hyprial.daemon.impl.harnesses.actor as harness_actor_module
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection, HarnessTimerCompleted, HarnessTimerElapsedCommand, RemoveHarnessCommand
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import LifecycleMutationFailed
from hyprial.kernel import LifecycleMutationCompleted

from ..support import (
    DOMAIN_EVENT_WAIT_SECONDS,
    PER_STEP_WAIT_SECONDS,
    TERMINAL_WAIT_SECONDS,
    _Events,
    _Launcher,
    _agent_request,
    _request,
)


def test_agent_token_sequence_restore_and_external_replacement_fence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="owner", machine="node")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)

    create_a = _agent_request("create-a", 0, CreateAgentCommand)
    assert actor.submit(create_a) is PortAdmission.ACCEPTED
    made_a = events.wait(create_a.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    token_a = made_a.provenance.resource_token
    assert made_a.provenance.created_by_operation

    destroy_b = _agent_request("destroy-b", 0, DestroyAgentCommand)
    assert actor.submit(destroy_b) is PortAdmission.ACCEPTED
    removed_b = events.wait(destroy_b.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert removed_b.provenance.resource_token == token_a
    assert registry.get("foo") is None

    restore_b = _agent_request(
        "restore-b", 0, CreateAgentCommand, expected=token_a
    )
    assert actor.submit(restore_b) is PortAdmission.ACCEPTED
    restored = events.wait(restore_b.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert restored.provenance.resource_token == token_a
    assert registry.get("foo") is not None

    destroy_c = _agent_request("destroy-c", 0, DestroyAgentCommand)
    assert actor.submit(destroy_c) is PortAdmission.ACCEPTED
    token_before_c = events.wait(destroy_c.correlation_id, DOMAIN_EVENT_WAIT_SECONDS).provenance.resource_token
    create_c = _agent_request("create-c", 0, CreateAgentCommand)
    assert actor.submit(create_c) is PortAdmission.ACCEPTED
    token_c = events.wait(create_c.correlation_id, DOMAIN_EVENT_WAIT_SECONDS).provenance.resource_token
    assert token_c != token_before_c

    registry.destroy("foo")
    registry.create("foo", model="external-replacement")
    stale = _agent_request(
        "stale-delete", 0, DestroyAgentCommand, expected=token_c
    )
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    stale_event = events.wait(stale.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert not stale_event.provenance.changed
    assert registry.require("foo").model == "external-replacement"

    assert actor.drain(TERMINAL_WAIT_SECONDS).complete
    registry.close()


def test_dead_connector_rebind_rotates_durable_binding_generation(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="owner", machine="node")
    registry.create("foo")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    actor_uri = registry.require("foo").uri

    first_correlation = "lifecycle:bind-pi:forward:0"
    first = _request(
        "bind-pi",
        0,
        BindAgentCommand(first_correlation, actor_uri, "pi", "headless", "s1"),
    )
    assert actor.submit(first) is PortAdmission.ACCEPTED
    first_result = events.wait(first_correlation, DOMAIN_EVENT_WAIT_SECONDS)
    assert registry.lifecycle_binding(actor_uri)["harness"] == "pi"  # type: ignore[index]
    assert actor.drain(TERMINAL_WAIT_SECONDS).complete

    # Runtime liveness is generation-local and can vanish on crash while the
    # durable lifecycle resource survives.  A later explicit bind must replace
    # that stale resource instead of returning changed=false.
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    second_correlation = "lifecycle:bind-claude:forward:1"
    second = _request(
        "bind-claude",
        1,
        BindAgentCommand(
            second_correlation, actor_uri, "claude", "headless", "s2"
        ),
    )
    assert actor.submit(second) is PortAdmission.ACCEPTED
    second_result = events.wait(second_correlation, DOMAIN_EVENT_WAIT_SECONDS)
    assert second_result.provenance.changed
    assert (
        second_result.provenance.resource_token
        != first_result.provenance.resource_token
    )
    binding = registry.lifecycle_binding(actor_uri)
    assert binding is not None
    assert binding["harness"] == "claude"
    projected = registry.require("foo")
    assert projected.last_harness == "claude"
    assert projected.last_session_id == "s2"

    assert actor.drain(TERMINAL_WAIT_SECONDS).complete
    registry.close()


def test_harness_internal_effect_retries_start_and_remove_once(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher(start_failures=1, stop_failures=1)
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=store,
        failure_budget=3,
        lifecycle_retry_base_seconds=0.3,
    )
    spec = HarnessLaunchProjection("codex", "retry", True)
    ensure = _request(
        "retry-ensure",
        0,
        EnsureHarnessCommand("lifecycle:retry-ensure:forward:0", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while True:
        receipts = store.load().lifecycle_receipts
        if receipts and receipts[0].attempts == 1:
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert actor.submit(
        HarnessTimerElapsedCommand("timer-during-custody", actor.generation, 1, 0)
    ) is PortAdmission.ACCEPTED
    # The tick's disposition is observable: _on_reconcile always emits
    # HarnessTimerCompleted for this correlation once the command is
    # processed, so wait for that event instead of sleeping a fixed
    # wall-clock window (a sleep could return before the actor even ran
    # the reconcile, passing the next assertion vacuously).  The assert
    # must still land before the retry timer re-drives the ensure (the
    # test configures lifecycle_retry_base_seconds=0.3, ~6x the
    # scheduling slack this wait needs in the healthy case).
    events.wait_event(HarnessTimerCompleted, "timer-during-custody", PER_STEP_WAIT_SECONDS)
    assert launcher.start_calls == 1
    assert events.wait(ensure.correlation_id, TERMINAL_WAIT_SECONDS).provenance.changed
    assert launcher.start_calls == 2

    remove = _request(
        "retry-remove",
        0,
        RemoveHarnessCommand(
            "lifecycle:retry-remove:forward:0", "codex", "retry"
        ),
    )
    assert actor.submit(remove) is PortAdmission.ACCEPTED
    assert events.wait(remove.correlation_id, TERMINAL_WAIT_SECONDS).provenance.changed
    assert launcher.processes[0].stop_calls == 2
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_permanent_failure_rolls_back_and_is_terminal(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher(start_failures=5)
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=store,
        failure_budget=2,
    )
    spec = HarnessLaunchProjection("codex", "permanent", True)
    ensure = _request(
        "permanent",
        0,
        EnsureHarnessCommand("lifecycle:permanent:forward:0", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    failed = events.wait_failed(ensure.correlation_id, TERMINAL_WAIT_SECONDS)
    assert failed.rolled_back
    state = store.load()
    assert not state.harnesses
    assert not state.lifecycle_receipts
    assert actor.read_harness("codex:permanent") is None

    # The lifecycle manager may have stopped waiting before the terminal
    # failure reached it and will re-admit this exact durable attempt.  A
    # settled failure must replay just like a settled success; silently
    # accepting the duplicate leaves the manager waiting until its global
    # operation deadline.
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    probe = HarnessTimerElapsedCommand(
        "permanent-failure-replay-probe", actor.generation, actor.version, 0
    )
    assert actor.submit(probe) is PortAdmission.ACCEPTED
    events.wait_event(
        HarnessTimerCompleted,
        probe.correlation_id,
        PER_STEP_WAIT_SECONDS,
    )
    failures = [
        event
        for event in events._events
        if isinstance(event, LifecycleMutationFailed)
        and event.correlation_id == ensure.correlation_id
    ]
    assert failures == [failed, failed]
    assert launcher.start_calls == 2
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_new_bootstrap_operation_restarts_existing_desired_harness(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    spec = HarnessLaunchProjection("codex", "bootstrap", True)
    first_events = _Events()
    first = HarnessRuntimeActor(
        _Launcher(), event_sink=first_events, desired_state=store
    )
    first_request = _request(
        "bootstrap-first",
        0,
        EnsureHarnessCommand("bootstrap-first", spec),
    )
    assert first.submit(first_request) is PortAdmission.ACCEPTED
    assert first_events.wait(first_request.correlation_id).provenance.changed
    assert first.close_runtime(TERMINAL_WAIT_SECONDS)

    rebuilt_events = _Events()
    rebuilt_launcher = _Launcher()
    rebuilt = HarnessRuntimeActor(
        rebuilt_launcher, event_sink=rebuilt_events, desired_state=store
    )
    bootstrap = _request(
        "bootstrap-new-generation",
        0,
        EnsureHarnessCommand("bootstrap-new-generation", spec),
    )
    assert rebuilt.submit(bootstrap) is PortAdmission.ACCEPTED
    completion = rebuilt_events.wait(bootstrap.correlation_id)
    assert not completion.provenance.changed
    assert rebuilt_launcher.start_calls == 1
    assert rebuilt.read_harness("codex:bootstrap").running  # type: ignore[union-attr]
    assert rebuilt.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_settled_lifecycle_cache_is_ordered_and_bounded(
    tmp_path: Path,
) -> None:
    actor = HarnessRuntimeActor(
        _Launcher(),
        event_sink=_Events(),
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
    )
    capacity = harness_actor_module._LIFECYCLE_SETTLED_CAPACITY
    for index in range(capacity + 200):
        actor._remember_settled_lifecycle(f"attempt-{index}")
    assert len(actor._settled_lifecycle_attempts) == capacity
    assert len(actor._settled_lifecycle_order) == capacity
    assert "attempt-0" not in actor._settled_lifecycle_attempts
    assert f"attempt-{capacity + 199}" in actor._settled_lifecycle_attempts
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_delayed_persistence_completion_has_one_harness_handoff_and_launch(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _DelayedCompletionActor(HarnessRuntimeActor):
        def _on_desired_completed(self, completion):  # type: ignore[no-untyped-def]
            if not entered.is_set():
                entered.set()
                assert release.wait(TERMINAL_WAIT_SECONDS)
            return super()._on_desired_completed(completion)

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher()
    actor = _DelayedCompletionActor(
        launcher, event_sink=events, desired_state=store
    )
    ensure = _request(
        "single-persistence-handoff",
        0,
        EnsureHarnessCommand(
            "single-persistence-handoff-payload",
            HarnessLaunchProjection("codex", "single-handoff", True),
        ),
    )
    try:
        assert actor.submit(ensure) is PortAdmission.ACCEPTED
        assert entered.wait(PER_STEP_WAIT_SECONDS)
        persistence = actor._persistence
        assert persistence is not None
        deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
        while persistence._effects.snapshot().completion_retries < 3:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert actor._runtime.snapshot(actor._handle).queued == 0

        release.set()
        events.wait(ensure.correlation_id)
        probe = HarnessTimerElapsedCommand(
            "single-handoff-probe", actor.generation, actor.version, 0
        )
        assert actor.submit(probe) is PortAdmission.ACCEPTED
        events.wait_event(
            HarnessTimerCompleted,
            probe.correlation_id,
            PER_STEP_WAIT_SECONDS,
        )
        assert launcher.start_calls == 1
        assert sum(
            isinstance(item, LifecycleMutationCompleted)
            and item.correlation_id == ensure.correlation_id
            for item in events._events
        ) == 1
        receipts = store.load().lifecycle_receipts
        assert len(receipts) == 1
        assert receipts[0].attempt_token == ensure.attempt_token
    finally:
        release.set()
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_failed_native_generation_cannot_authorize_next_correlation(
    tmp_path: Path,
) -> None:
    second_continuation_faulted = threading.Event()

    class _RetryBoundaryActor(HarnessRuntimeActor):
        begin_attempts = 0

        def _begin_start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("allow_lifecycle"):
                self.begin_attempts += 1
                if self.begin_attempts == 2:
                    second_continuation_faulted.set()
                    raise RuntimeError("retry fault before native admission")
            return super()._begin_start(*args, **kwargs)

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher(start_failures=1)
    actor = _RetryBoundaryActor(
        launcher,
        event_sink=events,
        desired_state=store,
        lifecycle_retry_base_seconds=0.001,
    )
    ensure = _request(
        "native-failure-then-pre-admission-fault",
        0,
        EnsureHarnessCommand(
            "native-failure-then-pre-admission-fault-payload",
            HarnessLaunchProjection("codex", "correlation-phase", True),
        ),
    )
    try:
        assert actor.submit(ensure) is PortAdmission.ACCEPTED
        assert second_continuation_faulted.wait(TERMINAL_WAIT_SECONDS)
        completed = events.wait(ensure.correlation_id)
        assert completed.attempt_token == ensure.attempt_token
        assert actor.begin_attempts == 3
        assert launcher.start_calls == 2
        assert sum(
            isinstance(item, LifecycleMutationCompleted)
            and item.correlation_id == ensure.correlation_id
            for item in events._events
        ) == 1
        receipts = store.load().lifecycle_receipts
        assert len(receipts) == 1
        assert receipts[0].attempt_token == ensure.attempt_token
        assert receipts[0].completed
        assert receipts[0].attempts == 1
        assert not actor._lifecycle_pending
        assert not actor._lifecycle_native_admitted
    finally:
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
