"""Cell member reconcile effect journal persistence and fail-closed legacy handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle_manager import (
    LifecyclePorts,
    LifecycleProcessManager,
    LifecycleState,
    _LifecycleStore,
)
from hyprial.daemon.impl.lifecycle_manager.steps import (
    CELL_MEMBER_ENSURE,
    CELL_MEMBER_POLICY_APPLY,
    CELL_MEMBER_STOP,
    cell_member_effect_operation_id,
)
from hyprial.daemon.impl.state_db import StateDatabase

from ..support import TERMINAL_WAIT_SECONDS


def test_cell_member_effects_persist_fenced_and_legacy_operations_fail_closed(
    tmp_path: Path,
) -> None:
    state_db = StateDatabase(tmp_path / "state.sqlite3")
    store = _LifecycleStore(state_db)
    receipt = store.record_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=3,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-1",
        attempt_token="att-1",
    )
    assert receipt.correlation_id == "corr-1"
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=3,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
        )
        == "prepared"
    )
    store.complete_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=3,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-1",
        attempt_token="att-1",
        succeeded=True,
        resource_token="tok-1",
    )
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=3,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
        )
        == "completed"
    )
    assert CELL_MEMBER_STOP == "cell.member.stop"
    assert CELL_MEMBER_POLICY_APPLY == "cell.member.policy.apply"
    assert (
        cell_member_effect_operation_id("c.test", "cr-1", 3, "w1")
        == "cell-reconcile:c.test:cr-1:3:w1"
    )

    with state_db.transaction() as db:
        db.execute(
            "INSERT INTO lifecycle_operations("
            "operation_id, request_digest, request_json, state, error, error_code, updated_at) "
            "VALUES('legacy-op-1', 'd', '{\"kind\":\"legacy_unknown\"}', 'running', NULL, NULL, 1.0)"
        )
    failed = store.fail_closed_legacy_operations()
    assert failed == ("legacy-op-1",)
    assert store.state("legacy-op-1") is LifecycleState.FAILED
    assert store.result("legacy-op-1").error_code == "LEGACY_OPERATION_UNSUPPORTED"

    with state_db.transaction() as db:
        db.execute(
            "INSERT INTO lifecycle_operations("
            "operation_id, request_digest, request_json, state, error, error_code, updated_at) "
            "VALUES('legacy-op-2', 'd2', '{\"kind\":\"legacy_unknown\"}', 'compensating', NULL, NULL, 1.0)"
        )
    router = CorrelationEventRouter()
    ports = LifecyclePorts(agent=object(), session=object(), harness=object(), route=object())  # type: ignore[arg-type]
    manager = LifecycleProcessManager(state_db, ports, router)
    try:
        settled = manager.wait("legacy-op-2", TERMINAL_WAIT_SECONDS)
        assert settled.state is LifecycleState.FAILED
        assert settled.error_code == "LEGACY_OPERATION_UNSUPPORTED"
        assert not manager.crashed
    finally:
        assert manager.drain(TERMINAL_WAIT_SECONDS)
        router.close()


def test_same_fence_stop_then_ensure_stays_pending_and_validates_receipt(
    tmp_path: Path,
) -> None:
    state_db = StateDatabase(tmp_path / "state.sqlite3")
    store = _LifecycleStore(state_db)

    stop_receipt = store.record_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=4,
        member="w1",
        effect_name=CELL_MEMBER_STOP,
        correlation_id="corr-stop",
        attempt_token="att-stop",
    )
    assert stop_receipt.attempt_token == "att-stop"
    stop_id = cell_member_effect_operation_id("c.test", "cr-1", 4, "w1", CELL_MEMBER_STOP)
    assert store.state(stop_id) is LifecycleState.RUNNING
    assert store.pending() == ()

    with pytest.raises(RuntimeError):
        store.complete_cell_member_effect(
            cell_id="c.test",
            epoch="cr-1",
            revision=4,
            member="w1",
            effect_name=CELL_MEMBER_STOP,
            correlation_id="corr-stop",
            attempt_token="wrong-attempt",
            succeeded=True,
        )

    store.complete_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=4,
        member="w1",
        effect_name=CELL_MEMBER_STOP,
        correlation_id="corr-stop",
        attempt_token="att-stop",
        succeeded=True,
    )
    assert store.pending() == ()
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=4,
            member="w1",
            effect_name=CELL_MEMBER_STOP,
        )
        == "completed"
    )

    ensure_receipt = store.record_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=4,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-ensure",
        attempt_token="att-ensure",
        ordinal=1,
    )
    assert ensure_receipt.attempt_token == "att-ensure"
    ensure_id = cell_member_effect_operation_id("c.test", "cr-1", 4, "w1", CELL_MEMBER_ENSURE)
    assert store.pending() == ()
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=4,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
        )
        == "prepared"
    )
    assert store.state(ensure_id) is LifecycleState.RUNNING

    with pytest.raises(RuntimeError):
        store.complete_cell_member_effect(
            cell_id="c.test",
            epoch="cr-1",
            revision=4,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
            correlation_id="wrong-corr",
            attempt_token="att-ensure",
            succeeded=False,
        )

    store.complete_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=4,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-ensure",
        attempt_token="att-ensure",
        succeeded=True,
    )
    assert store.pending() == ()
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=4,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
        )
        == "completed"
    )


def test_read_start_failures_skips_legacy_malformed_rows_fail_closed(
    tmp_path: Path,
) -> None:
    state_db = StateDatabase(tmp_path / "state.sqlite3")
    store = _LifecycleStore(state_db)
    legacy_payload = json.dumps(
        {
            "operationId": "old-start-1",
            "kind": "create",
            "target": {
                "agentName": "old-worker",
                "actor": "agent:alice:mac:old-worker",
                "harness": {
                    "provider": "claude",
                    "name": "old-worker",
                    "headless": True,
                    "executionRuntime": "smolvm",
                },
                "route": {
                    "route_id": "agent:alice:mac:old-worker",
                    "liveliness_key": "l",
                    "inbox_key": "i",
                },
                "session": None,
            },
            "source": None,
            "observedEntityToken": "tok-old",
        }
    )
    valid_payload = json.dumps(
        {
            "operationId": "new-start-1",
            "kind": "create",
            "target": {
                "agentName": "worker",
                "actor": "agent:alice:mac:worker",
                "harness": {
                    "provider": "claude",
                    "name": "worker",
                    "headless": True,
                },
                "route": {
                    "route_id": "agent:alice:mac:worker",
                    "liveliness_key": "l",
                    "inbox_key": "i",
                },
                "session": None,
            },
            "source": None,
            "observedEntityToken": "tok-new",
        }
    )
    with state_db.transaction() as db:
        db.execute(
            "INSERT INTO lifecycle_operations("
            "operation_id, request_digest, request_json, state, error, error_code, updated_at) "
            "VALUES('old-start-1', 'd1', ?, 'compensated', 'smolvm failed', 'HARNESS_START_FAILED', 10.0)",
            (legacy_payload,),
        )
        db.execute(
            "INSERT INTO lifecycle_operations("
            "operation_id, request_digest, request_json, state, error, error_code, updated_at) "
            "VALUES('broken-json', 'd2', '{not-json', 'compensated', 'broken', 'HARNESS_START_FAILED', 11.0)"
        )
        db.execute(
            "INSERT INTO lifecycle_operations("
            "operation_id, request_digest, request_json, state, error, error_code, updated_at) "
            "VALUES('new-start-1', 'd3', ?, 'compensated', 'new failed', 'HARNESS_START_FAILED', 12.0)",
            (valid_payload,),
        )
    failures = store.read_start_failures()
    assert list(failures.keys()) == ["agent:alice:mac:worker"]
    assert failures["agent:alice:mac:worker"][0].operation_id == "new-start-1"


def test_complete_cell_member_effect_requires_non_empty_attempt_token_and_exact_match(
    tmp_path: Path,
) -> None:
    state_db = StateDatabase(tmp_path / "state.sqlite3")
    store = _LifecycleStore(state_db)
    store.record_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=7,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-7",
        attempt_token="att-7",
    )

    for bad_token in (None, "", "   "):
        with pytest.raises((ValueError, RuntimeError)):
            store.complete_cell_member_effect(
                cell_id="c.test",
                epoch="cr-1",
                revision=7,
                member="w1",
                effect_name=CELL_MEMBER_ENSURE,
                correlation_id="corr-7",
                attempt_token=bad_token,
                succeeded=True,
            )
        assert (
            store.cell_member_effect_status(
                cell_id="c.test",
                epoch="cr-1",
                revision=7,
                member="w1",
                effect_name=CELL_MEMBER_ENSURE,
            )
            == "prepared"
        )

    for succeeded in (True, False):
        with pytest.raises(RuntimeError):
            store.complete_cell_member_effect(
                cell_id="c.test",
                epoch="cr-1",
                revision=7,
                member="w1",
                effect_name=CELL_MEMBER_ENSURE,
                correlation_id="corr-7",
                attempt_token="att-wrong",
                succeeded=succeeded,
            )
        assert (
            store.cell_member_effect_status(
                cell_id="c.test",
                epoch="cr-1",
                revision=7,
                member="w1",
                effect_name=CELL_MEMBER_ENSURE,
            )
            == "prepared"
        )

    store.complete_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=7,
        member="w1",
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-7",
        attempt_token="att-7",
        succeeded=True,
        resource_token="tok-7",
    )
    assert (
        store.cell_member_effect_status(
            cell_id="c.test",
            epoch="cr-1",
            revision=7,
            member="w1",
            effect_name=CELL_MEMBER_ENSURE,
        )
        == "completed"
    )


def test_a_running_cell_member_effect_is_never_lifecycle_manager_work(tmp_path: Path) -> None:
    state_db = StateDatabase(tmp_path / "state.sqlite3")
    store = _LifecycleStore(state_db)
    store.record_cell_member_effect(
        cell_id="c.test",
        epoch="cr-1",
        revision=5,
        member="agent://foo",
        effect_name=CELL_MEMBER_POLICY_APPLY,
        correlation_id="corr-5",
        attempt_token="att-5",
    )
    effect_id = cell_member_effect_operation_id(
        "c.test", "cr-1", 5, "agent://foo", CELL_MEMBER_POLICY_APPLY
    )
    assert store.pending() == ()

    router = CorrelationEventRouter()
    ports = LifecyclePorts(agent=object(), session=object(), harness=object(), route=object())  # type: ignore[arg-type]
    manager = LifecycleProcessManager(state_db, ports, router)
    try:
        manager.recover()
        assert not manager.crashed
        assert store.state(effect_id) is LifecycleState.FAILED
        assert store.result(effect_id).error_code == "LIFECYCLE_FAIL_CLOSED"
    finally:
        assert manager.drain(TERMINAL_WAIT_SECONDS)
        router.close()


def test_a_new_attempt_at_one_fence_re_prepares_a_completed_ensure(tmp_path: Path) -> None:
    store = _LifecycleStore(StateDatabase(tmp_path / "state.sqlite3"))
    fence = {"cell_id": "c.test", "epoch": "cr-1", "revision": 4, "member": "w1"}

    store.record_cell_member_effect(
        **fence,
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-1",
        attempt_token="att-1",
    )
    store.complete_cell_member_effect(
        **fence,
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-1",
        attempt_token="att-1",
        succeeded=True,
        resource_token="digest-old",
    )
    same = store.record_cell_member_effect(
        **fence,
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-dup",
        attempt_token="att-1",
    )
    assert same.attempt_token == "att-1"
    assert store.cell_member_effect_status(**fence, effect_name=CELL_MEMBER_ENSURE) == (
        "completed"
    )

    again = store.record_cell_member_effect(
        **fence,
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-2",
        attempt_token="att-2",
    )
    assert (again.correlation_id, again.attempt_token) == ("corr-2", "att-2")
    assert store.cell_member_effect_status(**fence, effect_name=CELL_MEMBER_ENSURE) == (
        "prepared"
    )
    store.complete_cell_member_effect(
        **fence,
        effect_name=CELL_MEMBER_ENSURE,
        correlation_id="corr-2",
        attempt_token="att-2",
        succeeded=False,
        error="launch exploded",
    )
    assert store.cell_member_effect_status(**fence, effect_name=CELL_MEMBER_ENSURE) == "failed"
