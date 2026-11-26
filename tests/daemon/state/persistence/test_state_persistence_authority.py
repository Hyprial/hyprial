"""Shared desired/journal writer authority and late completion custody."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import ActorState, AdmissionResult
from hyprial.daemon.impl.desired_state import (
    DesiredState, DesiredStateStore, InteractiveSession,
)
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import SessionMutationCompleted
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.state_persistence import (
    DesiredQuery,
    JournalQuery,
    RecordSessionAgentEffects,
    SetMailboxRole,
    StateCostOrigin,
    StatePersistenceAuthority,
    StatePersistenceTimeout,
    StatePersistenceVersionConflict,
)


def _authority(tmp_path: Path, actor_host, **kwargs) -> StatePersistenceAuthority:
    state_db = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    store = DesiredStateStore(tmp_path / "desired.json", state_db)
    return StatePersistenceAuthority(store, actor_host=actor_host, **kwargs)


def test_bound_launch_survives_production_journal_admission_and_reload(tmp_path, actor_host):
    from dataclasses import replace
    from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
    from hyprial.daemon.impl.state_persistence.commands import ReserveLifecycle
    from tests.daemon.lifecycle.process.manager.support import _spec

    authority = _authority(tmp_path, actor_host)
    reference = {"bindingRef": "binding-local", "bindingRevision": 2}
    target = _spec("bound-launch")
    target = replace(target, harness=replace(target.harness, model_binding=reference))
    operation = LifecycleOperation.create("bound-operation", target)
    try:
        from hyprial.daemon.impl.desired_state_io import DesiredStateIoRequest, DesiredStateOperation
        DesiredStateIoRequest("bound-io", 1, 1, DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
                              args=(operation,), context=("lifecycle", operation))
        authority.call_settled(ReserveLifecycle("bound-reserve", operation))
        reference["bindingRevision"] = 9
        restored = authority.journal.load(operation.operation_id)
        assert restored.target.harness.model_binding == {"bindingRef": "binding-local", "bindingRevision": 2}
        with pytest.raises(TypeError):
            restored.target.harness.model_binding["bindingRevision"] = 8
        launch = HarnessLaunchSpec("pi", "bound-launch", True, model_binding={"bindingRef": "binding-local", "bindingRevision": 2})
        projection = authority.projection()
        authority.settled_desired.compare_and_save(replace(projection.desired, schema_version=4), expected_version=projection.version)
        DesiredStateIoRequest("bound-upsert", 1, 1, DesiredStateOperation.UPSERT_HARNESS, args=(launch,))
        from types import MappingProxyType
        with pytest.raises(TypeError, match="immutable"):
            DesiredStateIoRequest("raw-map", 1, 1, DesiredStateOperation.UPSERT_HARNESS,
                                  args=(MappingProxyType({"mutable": []}),))
        authority.settled_desired.upsert_harness(launch)
        saved = authority.settled_desired.load().harnesses[0]
        assert saved.model_binding == launch.model_binding
        with pytest.raises(TypeError):
            saved.model_binding["bindingRevision"] = 8
    finally:
        assert authority.close()


def test_start_failures_are_readable_through_the_production_journal_port(tmp_path, actor_host):
    from dataclasses import replace
    from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState
    from tests.daemon.lifecycle.process.manager.support import _spec

    authority = _authority(tmp_path, actor_host)
    try:
        assert authority.journal.read_start_failures() == {}
        target = _spec("bad-model")
        failed = replace(LifecycleOperation.create("bad-model-start", target),
                         observed_entity_token="incarnation-1")
        authority.journal.reserve(failed)
        authority.journal.set_state(failed.operation_id, LifecycleState.COMPENSATED,
                                    "configured Pi model is unavailable", "HARNESS_START_FAILED")
        rows = authority.journal.read_start_failures()
        assert rows[target.actor][0] == failed
        assert rows[target.actor][1] == "configured Pi model is unavailable"
        authority.journal.reserve(LifecycleOperation.create("retry", target))
        assert authority.journal.read_start_failures() == {}
    finally:
        assert authority.close()


def test_harness_restore_settlement_survives_authority_restart(
    tmp_path: Path,
    actor_host,
) -> None:
    authority = _authority(tmp_path, actor_host)
    try:
        desired = authority.settled_desired
        assert desired.harness_restore_settlement("restore-1") is None
        assert desired.record_harness_restore_settlement(
            "restore-1", 2, 1, 0, 1
        ) == (2, 1, 0, 1)
        assert desired.harness_restore_settlement("restore-1") == (2, 1, 0, 1)
        with pytest.raises(DesiredStateError, match="changed its result"):
            desired.record_harness_restore_settlement("restore-1", 2, 2, 0, 0)
    finally:
        assert authority.close()

    restarted = _authority(tmp_path, actor_host)
    try:
        assert restarted.settled_desired.harness_restore_settlement(
            "restore-1"
        ) == (2, 1, 0, 1)
    finally:
        assert restarted.close()


def test_desired_and_journal_calls_share_one_owner_thread(tmp_path: Path, actor_host, monkeypatch) -> None:
    authority = _authority(tmp_path, actor_host)
    threads: list[int] = []
    desired = authority._desired_store
    journal = authority._journal_store
    original_desired = desired.set_mailbox_role
    original_journal = journal.next_generation

    def desired_write(enabled: bool):
        threads.append(threading.get_ident())
        return original_desired(enabled)

    def journal_write():
        threads.append(threading.get_ident())
        return original_journal()

    monkeypatch.setattr(desired, "set_mailbox_role", desired_write)
    monkeypatch.setattr(journal, "next_generation", journal_write)
    try:
        assert authority.desired.set_mailbox_role(True).as_mailbox is True
        assert authority.desired.load().as_mailbox is True
        assert authority.journal.next_generation() == 1
        assert len(threads) == 2 and len(set(threads)) == 1
        assert threads[0] != threading.get_ident()
    finally:
        assert authority.close()


def test_timeout_keeps_late_state_completion_retrievable(tmp_path: Path, actor_host, monkeypatch) -> None:
    authority = _authority(tmp_path, actor_host, call_timeout=0.02)
    entered = threading.Event()
    release = threading.Event()
    original = authority._desired_store.set_mailbox_role

    def slow_write(enabled: bool):
        entered.set()
        assert release.wait(3.0)
        return original(enabled)

    monkeypatch.setattr(authority._desired_store, "set_mailbox_role", slow_write)
    try:
        with pytest.raises(StatePersistenceTimeout) as caught:
            authority.desired.set_mailbox_role(True)
        assert entered.is_set()
        release.set()
        correlation = caught.value.correlation_id
        for _ in range(300):
            completion = authority.result(correlation)
            if completion is not None:
                break
            threading.Event().wait(0.01)
        assert completion is not None and completion.error is None
        assert authority._pending == {}
        assert authority.desired.load().as_mailbox is True
    finally:
        release.set()
        assert authority.close()


def test_call_settled_joins_exact_accepted_mutation(
    tmp_path: Path, actor_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path, actor_host, call_timeout=0.02)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = authority._desired_store.set_mailbox_role

    def slow_write(enabled: bool):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3.0)
        return original(enabled)

    monkeypatch.setattr(authority._desired_store, "set_mailbox_role", slow_write)
    results: list[object] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(authority.call_settled(
                SetMailboxRole("internal-mailbox-role", True)
            ))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert entered.wait(1.0)
        time.sleep(0.06)
        assert worker.is_alive()
        assert len(authority._pending) == 1
        assert not next(iter(authority._pending.values())).abandoned
        release.set()
        worker.join(3.0)
        assert not worker.is_alive()
        assert errors == []
        assert calls == 1
        assert results and results[0].as_mailbox is True
        assert authority._pending == {}
    finally:
        release.set()
        worker.join(3.0)
        assert authority.close()


def test_state_cost_totals_survive_generation_restart(
    tmp_path: Path, actor_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path, actor_host)
    original_record = authority.command_costs.record
    calls = 0

    def crash_after_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        original_record(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("injected state generation crash")

    monkeypatch.setattr(
        authority.command_costs, "record", crash_after_record
    )
    first_generation = authority._cell.snapshot().generation
    try:
        assert authority._cell.tell(
            RecordSessionAgentEffects(
                "cost-crash", (), StateCostOrigin.IPC
            ),
        ) is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 2.0
        while True:
            snapshot = authority._cell.snapshot()
            if (
                snapshot.generation > first_generation
                and snapshot.state is ActorState.RUNNING
            ):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        authority.settled_desired.record_session_agent_effects(
            (), _cost_origin=StateCostOrigin.IPC
        )
        row = authority.command_costs.snapshot()[
            "RecordSessionAgentEffects/ipc"
        ]
        assert row["calls"] == 2
        assert row["errors"] == 0
        assert row["threadCpuMs"] >= 0
    finally:
        assert authority.close()


def test_state_cost_records_command_error(
    tmp_path: Path, actor_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path, actor_host)

    def fail(_effects):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(
        authority._desired_store, "record_session_agent_effects", fail
    )
    try:
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            authority.settled_desired.record_session_agent_effects(
                (), _cost_origin=StateCostOrigin.IPC
            )
        row = authority.command_costs.snapshot()[
            "RecordSessionAgentEffects/ipc"
        ]
        assert row["calls"] == 1
        assert row["errors"] == 1
    finally:
        assert authority.close()


def test_state_command_rejects_caller_closure(tmp_path: Path, actor_host) -> None:
    authority = _authority(tmp_path, actor_host)
    try:
        with pytest.raises(TypeError, match="executable callbacks"):
            authority.call_desired(DesiredQuery.HARNESS_LIFECYCLE_RECEIPT, lambda: True)
        assert authority.projection().version == 0
    finally:
        assert authority.close()


def test_state_read_catalogue_rejects_wrong_arity_before_mailbox(tmp_path: Path, actor_host) -> None:
    authority = _authority(tmp_path, actor_host)
    try:
        with pytest.raises(TypeError, match="one string key"):
            authority.call_desired(DesiredQuery.HARNESS_LIFECYCLE_RECEIPT)
        with pytest.raises(TypeError, match="requires 3 string arguments"):
            authority.call_journal(JournalQuery.EFFECT_DONE, "operation", "step")
        with pytest.raises(TypeError, match="do not accept keyword"):
            authority.call_journal(JournalQuery.PENDING, arbitrary="value")
        assert authority.projection().version == 0
    finally:
        assert authority.close()


def test_desired_compare_and_save_fences_stale_projection(tmp_path: Path, actor_host) -> None:
    authority = _authority(tmp_path, actor_host)
    try:
        initial = authority.projection()
        authority.desired.set_mailbox_role(True)
        with pytest.raises(StatePersistenceVersionConflict):
            authority.desired.compare_and_save(
                initial.desired, expected_version=initial.version
            )
        assert authority.desired.load().as_mailbox is True
    finally:
        assert authority.close()


def test_session_normalization_fences_carrier_and_prefers_canonical_row(
    tmp_path: Path,
    actor_host,
) -> None:
    authority = _authority(tmp_path, actor_host)
    bare = InteractiveSession("agent:foo", "/tmp", ("codex",), "channel", "lease-1")
    canonical = InteractiveSession(
        "agent:owner:node:foo", "/new", ("codex",), "channel", "lease-2"
    )
    try:
        authority.desired.compare_and_save(
            DesiredState(interactive_sessions=(bare, canonical)),
            expected_version=authority.projection().version,
        )
        authority.desired.normalize_interactive_sessions(
            rewrites=((bare.actor, bare.session_ref, canonical.actor),)
        )
        assert authority.desired.load().interactive_sessions == (canonical,)
        authority.desired.normalize_interactive_sessions(
            rewrites=((canonical.actor, "stale-lease", "agent:elsewhere"),)
        )
        assert authority.desired.load().interactive_sessions == (canonical,)
    finally:
        assert authority.close()


def test_session_mutation_and_receipt_retirement_use_typed_state_commands(
    tmp_path: Path,
    actor_host,
) -> None:
    authority = _authority(tmp_path, actor_host)
    payload = RegisterSessionCommand(
        "session-register", "agent:owner:node:foo", "/tmp", ("codex",),
        "codex-carrier", "session-1", manage_agent=False,
    )
    request = LifecycleMutationRequest(
        "session-register", "attempt-1", "operation-1", None, payload,
    )
    try:
        provenance, _superseded = authority.desired.apply_session_lifecycle(request)
        assert authority.desired.load().interactive_sessions[0].session_ref == "session-1"
        assert authority.desired.retire_lifecycle_receipt(
            "session", request.attempt_token, provenance.resource_token
        )
        assert authority.desired.confirm_lifecycle_receipt_retired(
            "session", request.attempt_token, provenance.resource_token
        )
        assert not authority.desired.load().lifecycle_receipts
    finally:
        assert authority.close()


def test_shared_domain_receipt_and_journal_retire_in_one_writer_transaction(
    tmp_path: Path,
    actor_host,
) -> None:
    authority = _authority(tmp_path, actor_host)
    payload = RegisterSessionCommand(
        "shared-register", "agent:owner:node:foo", "/tmp", ("codex",),
        "codex-carrier", "session-1", manage_agent=False,
    )
    request = LifecycleMutationRequest(
        "shared-register", "shared-attempt", "operation-1", None, payload,
    )
    try:
        provenance, _ = authority.desired.apply_session_lifecycle(request)
        authority.journal.prepare_effect(
            "operation-1", 0, "session.register", "forward",
            request.correlation_id, request.attempt_token,
        )
        authority.journal.complete_effect(
            "operation-1", "session.register", "forward",
            request.correlation_id, provenance=provenance,
        )
        assert not authority.journal.retire_shared_receipt(
            "session", "operation-1", "session.register", "forward",
            request.attempt_token, "wrong-token",
        )
        assert authority.desired.load().lifecycle_receipts
        assert authority.journal.retire_shared_receipt(
            "session", "operation-1", "session.register", "forward",
            request.attempt_token, provenance.resource_token,
        )
        assert not authority.desired.load().lifecycle_receipts
        with authority.state_db.read() as db:
            retired = db.execute(
                "SELECT receipt_retired FROM lifecycle_effects WHERE operation_id=?",
                ("operation-1",),
            ).fetchone()[0]
        assert retired == 1
    finally:
        assert authority.close()


def test_session_actor_uses_shared_desired_authority(tmp_path: Path, actor_host) -> None:
    authority = _authority(tmp_path, actor_host)
    events: list[object] = []
    session = SessionActor(authority.desired, daemon_epoch="epoch", event_sink=events.append)
    try:
        command = RegisterSessionCommand(
            "session-through-state", "agent:owner:node:foo", "/tmp",
            ("codex",), "codex-carrier", "session-1", manage_agent=False,
        )
        assert session.submit(command) is PortAdmission.ACCEPTED
        deadline = time.monotonic() + 3.0
        while not any(isinstance(item, SessionMutationCompleted) for item in events):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert session.read_session(command.actor).session_ref == "session-1"
        assert authority.desired.load().interactive_sessions[0].actor == command.actor
    finally:
        assert session.drain(3.0).complete
        assert authority.close()


def test_harness_lifecycle_receipt_and_running_row_share_one_commit(
    tmp_path: Path,
    actor_host,
) -> None:
    authority = _authority(tmp_path, actor_host)
    request = LifecycleMutationRequest(
        "harness-start", "harness-attempt", "harness-operation", None,
        EnsureHarnessCommand(
            "harness-start", HarnessLaunchProjection("codex", "worker", True)
        ),
    )
    try:
        provenance, _replayed = authority.desired.apply_harness_lifecycle(request)
        assert not authority.desired.load().harnesses
        spec = HarnessLaunchSpec("codex", "worker", True)
        assert authority.desired.confirm_harness_lifecycle(
            request.attempt_token, provenance.resource_token, spec
        )
        state = authority.desired.load()
        assert state.harnesses[0].status == "running"
        assert state.lifecycle_receipts[0].completed
    finally:
        assert authority.close()
