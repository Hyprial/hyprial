from __future__ import annotations

import threading
import time
import sqlite3
import pytest
from pathlib import Path
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand, CreateAgentCommand, ReleaseAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.desired_state import DesiredStateStore, InteractiveSession
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle.atomic_lifecycle_ports import AtomicLifecycleDomainPort
from hyprial.daemon.impl.composition import CorrelatedDomainEvents
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
import hyprial.daemon.impl.harnesses.actor as harness_actor_module
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection, HarnessTimerCompleted, HarnessTimerElapsedCommand, RemoveHarnessCommand
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, UnregisterSessionCommand

from ..support import (
    DOMAIN_EVENT_WAIT_SECONDS,
    HARNESS_PORT_WAIT_SECONDS,
    PER_STEP_WAIT_SECONDS,
    TERMINAL_WAIT_SECONDS,
    _Events,
    _Launcher,
    _Process,
    _agent_request,
    _request,
)


def test_agent_lifecycle_receipt_retirement_is_actor_owned(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.actor.ports import (
        AgentLifecycleReceiptCompleted,
        ConfirmAgentLifecycleReceiptCommand,
        RetireAgentLifecycleReceiptCommand,
    )

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="owner", machine="node")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    try:
        request = _agent_request("receipt-owner", 0, CreateAgentCommand)
        assert actor.submit(request) is PortAdmission.ACCEPTED
        created = events.wait(request.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
        token = created.provenance.resource_token

        retire = RetireAgentLifecycleReceiptCommand("retire", request.attempt_token, token)
        assert actor.submit(retire) is PortAdmission.ACCEPTED
        retired = events.wait_event(AgentLifecycleReceiptCompleted, "retire")
        assert retired.matched and retired.operation == "retire"

        confirm = ConfirmAgentLifecycleReceiptCommand("confirm", request.attempt_token, token)
        assert actor.submit(confirm) is PortAdmission.ACCEPTED
        confirmed = events.wait_event(AgentLifecycleReceiptCompleted, "confirm")
        assert confirmed.matched and confirmed.operation == "confirm"
        replay = ConfirmAgentLifecycleReceiptCommand("confirm-again", request.attempt_token, token)
        assert actor.submit(replay) is PortAdmission.ACCEPTED
        assert not events.wait_event(AgentLifecycleReceiptCompleted, "confirm-again").matched
        assert not registry.retire_lifecycle_receipt(request.attempt_token, token)
    finally:
        assert actor.drain(TERMINAL_WAIT_SECONDS).complete
        registry.close()


def test_agent_binding_receipt_replays_after_process_rebuild(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="owner", machine="node")
    registry.create("foo")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    actor_uri = registry.require("foo").uri
    correlation = "lifecycle:bind:forward:0"
    request = _request(
        "bind", 0, BindAgentCommand(correlation, actor_uri, "codex", "headless")
    )
    assert actor.submit(request) is PortAdmission.ACCEPTED
    first = events.wait(correlation, DOMAIN_EVENT_WAIT_SECONDS)
    token = first.provenance.resource_token
    assert actor.read_binding(actor_uri) is not None
    assert actor.drain(TERMINAL_WAIT_SECONDS).complete
    registry.close()

    rebuilt_registry = AgentRegistry(database, owner="owner", machine="node")
    rebuilt_events = _Events()
    rebuilt = AgentActor(rebuilt_registry, event_sink=rebuilt_events)
    assert rebuilt.submit(request) is PortAdmission.ACCEPTED
    replay = rebuilt_events.wait(correlation, DOMAIN_EVENT_WAIT_SECONDS)
    assert replay.provenance.resource_token == token
    assert replay.provenance.changed
    assert rebuilt.read_binding(actor_uri) is not None

    release = _request(
        "release",
        0,
        ReleaseAgentCommand("lifecycle:release:forward:0", actor_uri),
    )
    assert rebuilt.submit(release) is PortAdmission.ACCEPTED
    released = rebuilt_events.wait(release.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert released.provenance.resource_token == token
    assert rebuilt.read_binding(actor_uri) is None
    restore = _request(
        "restore-binding",
        0,
        BindAgentCommand(
            "lifecycle:restore-binding:forward:0",
            actor_uri,
            "codex",
            "headless",
        ),
        expected=token,
    )
    assert rebuilt.submit(restore) is PortAdmission.ACCEPTED
    restored = rebuilt_events.wait(restore.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert restored.provenance.resource_token == token
    assert rebuilt.read_binding(actor_uri) is not None
    assert rebuilt.drain(TERMINAL_WAIT_SECONDS).complete
    rebuilt_registry.close()


def test_receipt_retire_is_idempotent_across_process_crash(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="owner", machine="node")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    request = _agent_request("create", 0, CreateAgentCommand)
    assert actor.submit(request) is PortAdmission.ACCEPTED
    completed = events.wait(request.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    token = completed.provenance.resource_token
    assert registry.retire_lifecycle_receipt(request.attempt_token, token)
    assert actor.drain(TERMINAL_WAIT_SECONDS).complete
    registry.close()

    rebuilt = AgentRegistry(database, owner="owner", machine="node")
    assert rebuilt.retire_lifecycle_receipt(request.attempt_token, token)
    rebuilt.confirm_lifecycle_receipt_retired(request.attempt_token, token)
    rebuilt.confirm_lifecycle_receipt_retired(request.attempt_token, token)
    rebuilt.close()


def test_legacy_agent_resource_key_migrates_with_receipt_fence(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="owner", machine="node")
    events = _Events()
    actor = AgentActor(registry, event_sink=events)
    request = _agent_request("legacy-key", 0, CreateAgentCommand)
    assert actor.submit(request) is PortAdmission.ACCEPTED
    first = events.wait(request.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert actor.drain(TERMINAL_WAIT_SECONDS).complete
    registry.close()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE lifecycle_resources SET resource_key = 'agent:foo' "
            "WHERE resource_key = 'agent-record:foo'"
        )
        connection.execute(
            "UPDATE lifecycle_receipts SET resource_key = 'agent:foo' "
            "WHERE attempt_token = ?",
            (request.attempt_token,),
        )

    rebuilt_registry = AgentRegistry(database, owner="owner", machine="node")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT resource_key FROM lifecycle_resources"
        ).fetchall() == [("agent-record:foo",)]
        assert connection.execute(
            "SELECT resource_key FROM lifecycle_receipts WHERE attempt_token = ?",
            (request.attempt_token,),
        ).fetchone() == ("agent-record:foo",)
    rebuilt_events = _Events()
    rebuilt = AgentActor(rebuilt_registry, event_sink=rebuilt_events)
    assert rebuilt.submit(request) is PortAdmission.ACCEPTED
    replay = rebuilt_events.wait(request.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert replay.provenance.resource_token == first.provenance.resource_token
    assert rebuilt.drain(TERMINAL_WAIT_SECONDS).complete
    rebuilt_registry.close()


def test_session_receipt_and_token_live_in_desired_state(tmp_path: Path) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
        ConfirmLifecycleReceiptCommand, LifecycleReceiptCompleted,
        RetireLifecycleReceiptCommand,
    )

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = SessionActor(store, daemon_epoch="epoch", event_sink=events)
    correlation = "lifecycle:session-create:forward:0"
    register = _request(
        "session-create",
        0,
        RegisterSessionCommand(
            correlation,
            "agent:owner:node:foo",
            "/tmp",
            ("codex",),
            "codex-carrier",
            "session-a",
            manage_agent=False,
        ),
    )
    assert actor.submit(register) is PortAdmission.ACCEPTED
    created = events.wait(correlation, DOMAIN_EVENT_WAIT_SECONDS)
    token = created.provenance.resource_token
    state = store.load()
    assert state.interactive_sessions[0].session_ref == "session-a"
    assert state.lifecycle_receipts[0].provenance.resource_token == token
    retire = RetireLifecycleReceiptCommand("session-retire", register.attempt_token, token)
    assert actor.submit(retire) is PortAdmission.ACCEPTED
    retired = events.wait_event(LifecycleReceiptCompleted, "session-retire")
    assert retired.domain == "session" and retired.matched
    confirm = ConfirmLifecycleReceiptCommand("session-confirm", register.attempt_token, token)
    assert actor.submit(confirm) is PortAdmission.ACCEPTED
    assert events.wait_event(LifecycleReceiptCompleted, "session-confirm").matched
    assert not store.load().lifecycle_receipts

    unregister = _request(
        "session-remove",
        0,
        UnregisterSessionCommand(
            "lifecycle:session-remove:forward:0",
            "agent:owner:node:foo",
            "session-a",
            manage_agent=False,
        ),
    )
    assert actor.submit(unregister) is PortAdmission.ACCEPTED
    removed = events.wait(unregister.correlation_id, DOMAIN_EVENT_WAIT_SECONDS)
    assert removed.provenance.resource_token == token
    assert not store.load().interactive_sessions

    restore = _request(
        "session-restore",
        0,
        RegisterSessionCommand(
            "lifecycle:session-restore:forward:0",
            "agent:owner:node:foo",
            "/tmp",
            ("codex",),
            "codex-carrier",
            "session-a",
            manage_agent=False,
        ),
        expected=token,
    )
    assert actor.submit(restore) is PortAdmission.ACCEPTED
    assert events.wait(restore.correlation_id, DOMAIN_EVENT_WAIT_SECONDS).provenance.resource_token == token
    store.claim_interactive(
        InteractiveSession(
            actor="agent:owner:node:foo",
            cwd="/external",
            command=("codex", "resume"),
            source="codex-carrier",
            session_ref="session-external",
        )
    )
    stale = _request(
        "session-stale-remove",
        0,
        UnregisterSessionCommand(
            "lifecycle:session-stale-remove:forward:0",
            "agent:owner:node:foo",
            "session-external",
            manage_agent=False,
        ),
        expected=token,
    )
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    assert not events.wait(stale.correlation_id, DOMAIN_EVENT_WAIT_SECONDS).provenance.changed
    assert store.load().interactive_sessions[0].session_ref == "session-external"
    assert actor.drain(TERMINAL_WAIT_SECONDS).complete


def test_harness_actor_commits_desired_resource_and_receipt_together(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
        ConfirmLifecycleReceiptCommand, LifecycleReceiptCompleted,
        RetireLifecycleReceiptCommand,
    )

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(), event_sink=events, desired_state=store
    )
    spec = HarnessLaunchProjection("codex", "foo", True)
    ensure = _request(
        "harness-create",
        0,
        EnsureHarnessCommand("lifecycle:harness-create:forward:0", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    created = events.wait(ensure.correlation_id)
    token = created.provenance.resource_token
    state = store.load()
    assert state.harnesses[0].name == "foo"
    assert state.lifecycle_receipts[0].provenance.resource_token == token
    retire = RetireLifecycleReceiptCommand("harness-retire", ensure.attempt_token, token)
    assert actor.submit(retire) is PortAdmission.ACCEPTED
    retired = events.wait_event(LifecycleReceiptCompleted, "harness-retire")
    assert retired.domain == "harness" and retired.matched
    confirm = ConfirmLifecycleReceiptCommand("harness-confirm", ensure.attempt_token, token)
    assert actor.submit(confirm) is PortAdmission.ACCEPTED
    assert events.wait_event(LifecycleReceiptCompleted, "harness-confirm").matched
    assert not store.load().lifecycle_receipts

    remove = _request(
        "harness-remove",
        0,
        RemoveHarnessCommand(
            "lifecycle:harness-remove:forward:0", "codex", "foo"
        ),
    )
    assert actor.submit(remove) is PortAdmission.ACCEPTED
    removed = events.wait(remove.correlation_id)
    assert removed.provenance.resource_token == token
    assert not store.load().harnesses
    restore = _request(
        "harness-restore",
        0,
        EnsureHarnessCommand("lifecycle:harness-restore:forward:0", spec),
        expected=token,
    )
    assert actor.submit(restore) is PortAdmission.ACCEPTED
    assert events.wait(restore.correlation_id).provenance.resource_token == token
    external = HarnessLaunchSpec("codex", "foo", True, args=("--external",))
    store.upsert_harness(external)
    stale = _request(
        "harness-stale-remove",
        0,
        RemoveHarnessCommand(
            "lifecycle:harness-stale-remove:forward:0", "codex", "foo"
        ),
        expected=token,
    )
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    assert not events.wait(stale.correlation_id).provenance.changed
    assert store.load().harnesses[0].args == ("--external",)
    actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_pending_receipt_survives_process_rebuild(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    spec = HarnessLaunchProjection("codex", "rebuild", True)
    ensure = _request(
        "rebuild",
        0,
        EnsureHarnessCommand("lifecycle:rebuild:forward:0", spec),
    )
    provenance, replayed = store.apply_harness_lifecycle(ensure)
    assert provenance.changed and not replayed
    assert not store.load().lifecycle_receipts[0].completed

    rebuilt_events = _Events()
    rebuilt = HarnessRuntimeActor(
        _Launcher(), event_sink=rebuilt_events, desired_state=store
    )
    assert rebuilt.submit(ensure) is PortAdmission.ACCEPTED
    completed = rebuilt_events.wait(ensure.correlation_id, TERMINAL_WAIT_SECONDS)
    assert completed.provenance.changed
    assert store.load().lifecycle_receipts[0].completed
    assert rebuilt.close_runtime(TERMINAL_WAIT_SECONDS)


def test_inflight_lifecycle_replay_joins_without_superseding_start(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            entered.set()
            assert release.wait(PER_STEP_WAIT_SECONDS)
            process = _Process()
            self.processes.append(process)
            return process

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _BlockingLauncher()
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=store,
    )
    request = _request(
        "slow-replay",
        0,
        EnsureHarnessCommand(
            "slow-replay-payload",
            HarnessLaunchProjection("codex", "slow-replay", True),
        ),
    )
    assert actor.submit(request) is PortAdmission.ACCEPTED
    assert entered.wait(PER_STEP_WAIT_SECONDS)
    assert actor.submit(request) is PortAdmission.ACCEPTED
    release.set()
    completed = events.wait(request.correlation_id, TERMINAL_WAIT_SECONDS)
    assert isinstance(completed, LifecycleMutationCompleted)
    assert launcher.start_calls == 1
    assert actor.read_harness("codex:slow-replay").running  # type: ignore[union-attr]
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_close_rolls_back_incomplete_ensure_and_gcs_receipt(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(start_failures=100),
        event_sink=events,
        desired_state=store,
        failure_budget=3,
        lifecycle_retry_base_seconds=1.0,
    )
    spec = HarnessLaunchProjection("codex", "closing", True)
    ensure = _request(
        "closing",
        0,
        EnsureHarnessCommand("lifecycle:closing:forward:0", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while True:
        receipts = store.load().lifecycle_receipts
        if receipts and receipts[0].attempts == 1:
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    failed = events.wait_failed(ensure.correlation_id)
    assert failed.code == "HARNESS_RUNTIME_CLOSING"
    assert failed.rolled_back
    state = store.load()
    assert not state.harnesses
    assert not state.lifecycle_receipts


def test_close_owns_terminal_failure_when_count_completion_arrives_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = HarnessRuntimeActor._continue_lifecycle_rejection

    def hold_failure_count(self, *args):
        entered.set()
        assert release.wait(PER_STEP_WAIT_SECONDS)
        return original(self, *args)

    monkeypatch.setattr(
        HarnessRuntimeActor, "_continue_lifecycle_rejection", hold_failure_count
    )
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(start_failures=100), event_sink=events, desired_state=store,
        failure_budget=3, lifecycle_retry_base_seconds=1.0,
    )
    request = _request(
        "ordered-close", 0,
        EnsureHarnessCommand(
            "ordered-close-payload", HarnessLaunchProjection("codex", "closing", True)
        ),
    )
    closed = []
    closer = threading.Thread(
        target=lambda: closed.append(actor.close_runtime(TERMINAL_WAIT_SECONDS)),
        name="test-ordered-harness-close", daemon=True,
    )
    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        assert entered.wait(PER_STEP_WAIT_SECONDS)
        assert store.load().lifecycle_receipts[0].attempts == 1
        closer.start()
        deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
        while True:
            with actor._generation_lock:
                closing = actor._closing
            if closing:
                break
            assert time.monotonic() < deadline
            time.sleep(0.001)
        # Resume the real failure continuation only after close has claimed
        # termination. No fabricated mutation or completion is injected.
        release.set()
        closer.join(TERMINAL_WAIT_SECONDS)
        assert not closer.is_alive() and closed == [True]
        failed = events.wait_failed(request.correlation_id)
        assert failed.code == "HARNESS_RUNTIME_CLOSING"
        assert failed.rolled_back
        assert not store.load().harnesses
        assert not store.load().lifecycle_receipts
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(TERMINAL_WAIT_SECONDS)
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_recent_duplicate_is_ignored_and_evicted_stale_delete_remains_safe(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=store)
    spec = HarnessLaunchProjection("codex", "dedupe", True)
    ensure = _request(
        "dedupe-create",
        0,
        EnsureHarnessCommand("dedupe-payload", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    completion = events.wait(ensure.correlation_id)
    token = completion.provenance.resource_token
    assert store.retire_lifecycle_receipt("harness", ensure.attempt_token, token)
    store.confirm_lifecycle_receipt_retired("harness", ensure.attempt_token, token)
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    # Absence of a duplicate effect is asserted only after an observable
    # barrier: the probe tick is queued behind the resubmitted ensure in
    # the same mailbox, so once its HarnessTimerCompleted arrives the
    # duplicate has been fully processed (and silently dropped).  A fixed
    # sleep could return before the actor ran the resubmit at all,
    # passing the count assertions below vacuously.
    probe = HarnessTimerElapsedCommand(
        "dedupe-ignore-probe", actor.generation, 1, 0
    )
    assert actor.submit(probe) is PortAdmission.ACCEPTED
    events.wait_event(HarnessTimerCompleted, "dedupe-ignore-probe", PER_STEP_WAIT_SECONDS)
    assert launcher.start_calls == 1
    assert sum(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == ensure.correlation_id
        for item in events._events
    ) == 1

    stale = _request(
        "stale-delete-after-eviction",
        0,
        RemoveHarnessCommand("stale-delete-payload", "codex", "dedupe"),
        expected=token,
    )
    replacement = HarnessLaunchSpec("codex", "dedupe", True, args=("--new",))
    store.upsert_harness(replacement)
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    first = events.wait(stale.correlation_id)
    assert not first.provenance.changed
    stale_token = first.provenance.resource_token
    assert store.retire_lifecycle_receipt(
        "harness", stale.attempt_token, stale_token
    )
    store.confirm_lifecycle_receipt_retired(
        "harness", stale.attempt_token, stale_token
    )
    capacity = harness_actor_module._LIFECYCLE_SETTLED_CAPACITY
    for index in range(capacity + 1):
        actor._remember_settled_lifecycle(f"evict-{index}")
    assert stale.attempt_token not in actor._settled_lifecycle_attempts
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
    while sum(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == stale.correlation_id
        for item in events._events
    ) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert store.load().harnesses == (replacement,)
    replay_receipt = store.load().lifecycle_receipts[0]
    assert store.retire_lifecycle_receipt(
        "harness", stale.attempt_token, replay_receipt.provenance.resource_token
    )
    store.confirm_lifecycle_receipt_retired(
        "harness", stale.attempt_token, replay_receipt.provenance.resource_token
    )
    assert not store.load().lifecycle_receipts
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


@pytest.mark.parametrize("fault_boundary", ["before", "after"])
def test_lifecycle_continuation_redelivery_respects_native_admission_phase(
    tmp_path: Path,
    fault_boundary: str,
) -> None:
    faulted = threading.Event()
    redelivered = threading.Event()
    native_entered = threading.Event()
    release_native = threading.Event()

    class _FaultingContinuationActor(HarnessRuntimeActor):
        continuation_calls = 0

        def _continue_lifecycle_apply(self, request, result):  # type: ignore[no-untyped-def]
            self.continuation_calls += 1
            if self.continuation_calls > 1:
                redelivered.set()
            return super()._continue_lifecycle_apply(request, result)

        def _begin_start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            lifecycle = bool(kwargs.get("allow_lifecycle"))
            if lifecycle and not faulted.is_set():
                faulted.set()
                if fault_boundary == "before":
                    raise RuntimeError("fault before native admission")
                super()._begin_start(*args, **kwargs)
                raise RuntimeError("fault after native admission")
            return super()._begin_start(*args, **kwargs)

    class _HeldLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            native_entered.set()
            assert release_native.wait(TERMINAL_WAIT_SECONDS)
            process = _Process(stop_failures=self.stop_failures)
            self.processes.append(process)
            return process

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _HeldLauncher()
    actor = _FaultingContinuationActor(
        launcher, event_sink=events, desired_state=store
    )
    ensure = _request(
        f"phase-{fault_boundary}",
        0,
        EnsureHarnessCommand(
            f"phase-{fault_boundary}-payload",
            HarnessLaunchProjection("codex", f"phase-{fault_boundary}", True),
        ),
    )
    try:
        assert actor.submit(ensure) is PortAdmission.ACCEPTED
        assert faulted.wait(PER_STEP_WAIT_SECONDS)
        assert redelivered.wait(PER_STEP_WAIT_SECONDS)
        assert native_entered.wait(PER_STEP_WAIT_SECONDS)
        harness_id = f"codex:phase-{fault_boundary}"
        assert actor._records[harness_id].generation == 1
        assert launcher.start_calls == 1
        release_native.set()
        completed = events.wait(ensure.correlation_id)
        assert completed.attempt_token == ensure.attempt_token
        assert launcher.start_calls == 1
        assert sum(
            isinstance(item, LifecycleMutationCompleted)
            and item.correlation_id == ensure.correlation_id
            for item in events._events
        ) == 1
        assert not any(
            isinstance(item, PortCommandRejected)
            and item.code == "HARNESS_START_SUPERSEDED"
            for item in events._events
        )
        receipts = store.load().lifecycle_receipts
        assert len(receipts) == 1
        assert receipts[0].attempt_token == ensure.attempt_token
        assert receipts[0].completed
    finally:
        release_native.set()
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_terminal_receipt_replays_after_waiter_timeout_without_io(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class _SlowLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            assert release.wait(PER_STEP_WAIT_SECONDS)
            process = _Process()
            self.processes.append(process)
            return process

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _SlowLauncher()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=store)
    request = _request(
        "timeout-replay",
        0,
        EnsureHarnessCommand(
            "timeout-payload",
            HarnessLaunchProjection("codex", "timeout-replay", True),
        ),
    )
    assert actor.submit(request) is PortAdmission.ACCEPTED
    # Semantic absence window, not a budget: nothing can legitimately
    # publish while the launcher is blocked in start(), and the only
    # async producer (the process-I/O pool) is the blocked thread
    # itself, so 20 ms gives a wrongful emit ample scheduling slack
    # without slowing the suite.
    time.sleep(0.02)
    assert not events._events
    release.set()
    first = events.wait(request.correlation_id)
    assert not first.replayed
    persisted = store.harness_lifecycle_receipt(request.attempt_token)
    assert persisted is not None and persisted.completed
    assert actor.submit(request) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
    while sum(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == request.correlation_id
        for item in events._events
    ) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    replay = [
        item
        for item in events._events
        if isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == request.correlation_id
    ][-1]
    assert replay.replayed
    assert replay.generation == first.generation
    assert replay.version == first.version
    assert replay.provenance == first.provenance
    assert launcher.start_calls == 1
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_port_waiter_timeout_then_same_attempt_replays_to_new_waiter(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class _SlowLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            assert release.wait(PER_STEP_WAIT_SECONDS)
            process = _Process()
            self.processes.append(process)
            return process

    store = DesiredStateStore(tmp_path / "desired.json")
    domain_events = CorrelatedDomainEvents()
    launcher = _SlowLauncher()
    actor = HarnessRuntimeActor(
        launcher, event_sink=domain_events, desired_state=store
    )
    router = CorrelationEventRouter()
    port = AtomicLifecycleDomainPort(
        domain="harness",
        router=router,
        generation=lambda: actor.generation,
        version=lambda: actor.version,
        submit_domain=actor.submit,
        wait_domain=lambda correlation, expected: domain_events.wait(
            correlation, expected, timeout=HARNESS_PORT_WAIT_SECONDS  # type: ignore[arg-type]
        ),
        retire_receipt=lambda attempt, token: store.retire_lifecycle_receipt(
            "harness", attempt, token
        ),
        confirm_receipt_retired=lambda attempt, token: (
            store.confirm_lifecycle_receipt_retired("harness", attempt, token)
        ),
        release_replay_claim=actor.release_lifecycle_replay,
    )
    request = _request(
        "port-timeout",
        0,
        EnsureHarnessCommand(
            "port-timeout-payload",
            HarnessLaunchProjection("codex", "port-timeout", True),
        ),
    )
    generation = port.generation
    version = port.version
    first_waiter = router.register(
        request.correlation_id,
        attempt_token=request.attempt_token,
        generation=generation,
        versions=frozenset({version, version + 1}),
    )
    assert port.submit(request) is PortAdmission.ACCEPTED
    # Deliberately tiny timeout: timing out IS the stimulus under test
    # (the launcher is still blocked, so this wait cannot complete), and
    # the test asserts the TimeoutError.  Not a settle budget.
    with pytest.raises(TimeoutError):
        first_waiter.wait(0.02)
    release.set()
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while True:
        receipt = store.harness_lifecycle_receipt(request.attempt_token)
        if receipt is not None and receipt.completed:
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    # Two gates, not one.  The durable-receipt poll above is the admission
    # criterion for the ACTOR's replay path -- but this resubmission goes
    # through the PORT, and AtomicLifecycleDomainPort.submit silently
    # dedupes (returns ACCEPTED without enqueueing) while the first
    # attempt is still in its _pending set.  The worker clears that entry
    # only after its own wait_domain settles the first completion, so the
    # resubmit must also wait for the port's dedupe gate to open -- and
    # that span includes the worker's port-level wait, hence the terminal
    # budget.  (A settle sleep here would be guessing at exactly this.)
    deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
    while request.attempt_token in port._pending:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    generation = port.generation
    version = port.version
    retry_waiter = router.register(
        request.correlation_id,
        attempt_token=request.attempt_token,
        generation=generation,
        versions=frozenset({version, version + 1}),
    )
    assert port.submit(request) is PortAdmission.ACCEPTED
    replay = retry_waiter.wait(TERMINAL_WAIT_SECONDS)
    assert isinstance(replay, LifecycleMutationCompleted)
    assert replay.replayed
    assert launcher.start_calls == 1
    token = replay.provenance.resource_token
    assert port.retire_receipt(request.attempt_token, token)
    port.confirm_receipt_retired(request.attempt_token, token)
    assert port.drain(TERMINAL_WAIT_SECONDS)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    domain_events.close()
    router.close()


def test_terminal_receipt_replays_in_fresh_process(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    first_events = _Events()
    first_launcher = _Launcher()
    first = HarnessRuntimeActor(
        first_launcher, event_sink=first_events, desired_state=store
    )
    request = _request(
        "fresh-terminal-replay",
        0,
        EnsureHarnessCommand(
            "fresh-terminal-payload",
            HarnessLaunchProjection("codex", "fresh-terminal", True),
        ),
    )
    assert first.submit(request) is PortAdmission.ACCEPTED
    original = first_events.wait(request.correlation_id)
    assert first.close_runtime(TERMINAL_WAIT_SECONDS)

    rebuilt_events = _Events()
    rebuilt_launcher = _Launcher()
    rebuilt = HarnessRuntimeActor(
        rebuilt_launcher, event_sink=rebuilt_events, desired_state=store
    )
    assert rebuilt.submit(request) is PortAdmission.ACCEPTED
    replay = rebuilt_events.wait(request.correlation_id)
    assert replay.replayed
    assert replay.correlation_id == original.correlation_id
    assert replay.provenance == original.provenance
    assert rebuilt_launcher.start_calls == 0
    assert rebuilt.close_runtime(TERMINAL_WAIT_SECONDS)


def test_replay_capacity_never_evicts_active_committed_claim(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(),
        event_sink=events,
        desired_state=store,
        lifecycle_replay_capacity=2,
    )
    requests: list[LifecycleMutationRequest] = []
    completions: list[LifecycleMutationCompleted] = []
    for index in range(3):
        request = _request(
            f"cap-{index}",
            0,
            EnsureHarnessCommand(
                f"cap-payload-{index}",
                HarnessLaunchProjection("codex", f"cap-{index}", True),
            ),
        )
        requests.append(request)
        assert actor.submit(request) is PortAdmission.ACCEPTED
        completions.append(events.wait(request.correlation_id))

    assert actor.submit(requests[0]) is PortAdmission.ACCEPTED
    assert actor.submit(requests[1]) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
    while sum(
        isinstance(item, LifecycleMutationCompleted) and item.replayed
        for item in events._events
    ) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert actor._lifecycle_replay_claims == {
        requests[0].attempt_token,
        requests[1].attempt_token,
    }

    assert actor.submit(requests[2]) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while not any(
        isinstance(item, PortCommandRejected)
        and item.correlation_id == requests[2].correlation_id
        and item.admission is PortAdmission.OVERLOADED
        for item in events._events
    ):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert requests[0].attempt_token in actor._lifecycle_replay_claims
    before = sum(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == requests[0].correlation_id
        for item in events._events
    )
    assert actor.submit(requests[0]) is PortAdmission.ACCEPTED
    # Same mailbox-barrier pattern as the dedupe test above: the probe
    # tick is processed strictly after the resubmitted request, so the
    # count assertion below cannot pass before the actor had its chance
    # to (wrongly) emit a second completion.
    probe = HarnessTimerElapsedCommand(
        "replay-claim-probe", actor.generation, 1, 0
    )
    assert actor.submit(probe) is PortAdmission.ACCEPTED
    events.wait_event(HarnessTimerCompleted, "replay-claim-probe", PER_STEP_WAIT_SECONDS)
    assert sum(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == requests[0].correlation_id
        for item in events._events
    ) == before

    first = completions[0]
    assert store.retire_lifecycle_receipt(
        "harness", requests[0].attempt_token, first.provenance.resource_token
    )
    store.confirm_lifecycle_receipt_retired(
        "harness", requests[0].attempt_token, first.provenance.resource_token
    )
    actor.release_lifecycle_replay(requests[0].attempt_token)
    assert actor.submit(requests[2]) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
    while not any(
        isinstance(item, LifecycleMutationCompleted)
        and item.correlation_id == requests[2].correlation_id
        and item.replayed
        for item in events._events
    ):
        assert time.monotonic() < deadline
        time.sleep(0.005)

    for request, completion in zip(requests[1:], completions[1:], strict=True):
        assert store.retire_lifecycle_receipt(
            "harness", request.attempt_token, completion.provenance.resource_token
        )
        store.confirm_lifecycle_receipt_retired(
            "harness", request.attempt_token, completion.provenance.resource_token
        )
        actor.release_lifecycle_replay(request.attempt_token)
    assert not store.load().lifecycle_receipts
    assert not actor._lifecycle_replay_claims
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
