"""Lifecycle state writes stay bounded as retired resource history grows."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
)
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
    StoredLifecycleReceipt,
    StoredLifecycleResource,
)
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority
from hyprial.kernel import HarnessLaunchSpec, LifecycleMutationRequest, MutationProvenance


def _resource(index: int, *, active: bool = False) -> StoredLifecycleResource:
    return StoredLifecycleResource(
        domain="harness",
        resource_key=f"harness:pi:retired-{index:04d}",
        resource_token=f"resource-{index:04d}",
        active=active,
        payload={},
    )


def _completed_receipt(resource: StoredLifecycleResource) -> StoredLifecycleReceipt:
    return StoredLifecycleReceipt(
        domain="harness",
        attempt_token="completed-attempt",
        operation_id="completed-operation",
        resource_key=resource.resource_key,
        expected_resource_token=None,
        provenance=MutationProvenance(True, True, resource.resource_token),
        completed=True,
    )


def _authority_with_resources(path: Path, count: int) -> StatePersistenceAuthority:
    state_db = StateDatabase(path / "state.sqlite3")
    store = DesiredStateStore(path / "desired.json", state_db)
    resources = tuple(_resource(index) for index in range(count))
    store.save(
        replace(
            store.load(),
            lifecycle_resources=resources,
            lifecycle_receipts=(_completed_receipt(resources[0]),),
        )
    )
    return StatePersistenceAuthority(store)


def _changed_rows(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    changes: list[int] = []
    original = StateDatabase.transaction

    @contextlib.contextmanager
    def counted(self, schema=None):  # type: ignore[no-untyped-def]
        with original(self, schema) as db:
            before = db.total_changes
            yield db
            changes.append(db.total_changes - before)

    monkeypatch.setattr(StateDatabase, "transaction", counted)
    return changes


def _write_cost(
    path: Path, monkeypatch: pytest.MonkeyPatch, resources: int
) -> tuple[list[int], list[int]]:
    authority = _authority_with_resources(path, resources)
    loads: list[int] = []
    original_load = authority._desired_store.load

    def counted_load():  # type: ignore[no-untyped-def]
        state = original_load()
        loads.append(len(state.lifecycle_resources))
        return state

    monkeypatch.setattr(authority._desired_store, "load", counted_load)
    changes = _changed_rows(monkeypatch)
    request = LifecycleMutationRequest(
        "apply-correlation",
        "apply-attempt",
        "apply-operation",
        None,
        EnsureHarnessCommand(
            "apply-correlation", HarnessLaunchProjection("pi", "new-worker", True)
        ),
    )
    try:
        authority.desired.apply_harness_lifecycle(request)
        authority.desired.retire_lifecycle_receipt(
            "harness", "completed-attempt", "resource-0000"
        )
        authority.desired.confirm_lifecycle_receipt_retired(
            "harness", "completed-attempt", "resource-0000"
        )
    finally:
        assert authority.close()
        monkeypatch.undo()
    return changes, loads


def test_lifecycle_write_cost_is_bounded_by_changed_rows_not_retired_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_changes, small_loads = _write_cost(tmp_path / "small", monkeypatch, 100)
    large_changes, large_loads = _write_cost(tmp_path / "large", monkeypatch, 2000)

    assert len(small_changes) == len(large_changes) == 3
    assert max(large_changes) <= max(small_changes) * 3, (small_changes, large_changes)
    assert small_loads == large_loads == [], (small_loads, large_loads)


def test_acknowledged_lifecycle_write_is_already_in_projection(tmp_path: Path) -> None:
    authority = _authority_with_resources(tmp_path, 100)
    request = LifecycleMutationRequest(
        "projection-correlation",
        "projection-attempt",
        "projection-operation",
        None,
        EnsureHarnessCommand(
            "projection-correlation",
            HarnessLaunchProjection("pi", "projection-worker", True),
        ),
    )
    try:
        provenance, replayed = authority.desired.apply_harness_lifecycle(request)
        assert not replayed
        projected = authority.projection().desired
        assert any(
            receipt.attempt_token == request.attempt_token
            and receipt.provenance.resource_token == provenance.resource_token
            for receipt in projected.lifecycle_receipts
        )

        assert authority.desired.retire_lifecycle_receipt(
            "harness", "completed-attempt", "resource-0000"
        )
        assert next(
            receipt
            for receipt in authority.projection().desired.lifecycle_receipts
            if receipt.attempt_token == "completed-attempt"
        ).retired

        assert authority.desired.confirm_lifecycle_receipt_retired(
            "harness", "completed-attempt", "resource-0000"
        )
        assert all(
            receipt.attempt_token != "completed-attempt"
            for receipt in authority.projection().desired.lifecycle_receipts
        )
    finally:
        assert authority.close()


def test_retired_resource_cleanup_keeps_every_live_reference_and_margin(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(
        tmp_path / "desired.json", StateDatabase(tmp_path / "state.sqlite3")
    )
    retired = tuple(_resource(index) for index in range(300))
    receipt_resource = StoredLifecycleResource(
        "harness", "harness:pi:receipt-backed", "receipt-resource", False, {}
    )
    harness_resource = StoredLifecycleResource(
        "harness", "harness:pi:live-worker", "harness-resource", False, {}
    )
    active_resource = StoredLifecycleResource(
        "harness", "harness:pi:active-worker", "active-resource", True, {}
    )
    receipt = StoredLifecycleReceipt(
        "harness",
        "live-attempt",
        "live-operation",
        receipt_resource.resource_key,
        None,
        MutationProvenance(True, True, receipt_resource.resource_token),
        completed=True,
    )
    store.save(
        replace(
            store.load(),
            harnesses=(HarnessLaunchSpec("pi", "live-worker", True),),
            lifecycle_resources=(
                *retired,
                receipt_resource,
                harness_resource,
                active_resource,
            ),
            lifecycle_receipts=(receipt,),
        )
    )

    counts: list[int] = []
    while (count := store.collect_retired_lifecycle_resources()) > 0:
        counts.append(count)
    removed = sum(counts)
    assert counts, "the retired rows must require at least one collection pass"
    assert max(counts) <= 4, f"a pass exceeded the per-pass cap: {counts}"
    assert len(counts) > 1, f"the fixture must span multiple capped passes: {counts}"

    state = store.load()
    keys = {resource.resource_key for resource in state.lifecycle_resources}
    assert receipt_resource.resource_key in keys
    assert harness_resource.resource_key in keys
    assert active_resource.resource_key in keys
    assert len(keys & {resource.resource_key for resource in retired}) == 128
    assert removed == len(retired) - 128
    assert store.collect_retired_lifecycle_resources() == 0
