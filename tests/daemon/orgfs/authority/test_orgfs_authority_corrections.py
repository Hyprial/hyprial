# Decisions:
# - Package S (2026-11-25): the journal space-authority, replica-authority
#   and content-purge corrections were deleted with those authorities.  What
#   stays: the facade's command loop bounds total custody across a restart,
#   and the public checkout goes through the lifecycle command.
"""Custody and lifecycle-command corrections of the OrgFS facade and runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace


def _wait_for(predicate) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _restart(cell) -> int:
    generation = cell.snapshot().generation
    assert cell.tell(object()) is AdmissionResult.ACCEPTED
    _wait_for(lambda: cell.snapshot().generation > generation)
    return cell.snapshot().generation


def test_facade_total_custody_bounds_deferred_and_rebases_restart(monkeypatch) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.space_state._SPACE_STATE_CAPACITY', 2)
    facade = LocalOrgFs()
    space = facade.create_space("bounded facade")
    facade.mkdir(space.space_id, "seed")
    owner = facade._space_state_owners[space.space_id]
    owner.effects._capacity = 1
    entered = threading.Event()
    release = threading.Event()
    original = facade._execute_space_state

    def execute(space_id, command):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(space_id, command)

    facade._execute_space_state = execute
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(facade.mkdir, space.space_id, "first")
            assert entered.wait(1)
            second = pool.submit(facade.mkdir, space.space_id, "second")
            _wait_for(lambda: len(owner.deferred) == 1)
            with pytest.raises(OrgFsError) as rejected:
                facade.mkdir(space.space_id, "third")
            assert rejected.value.code == "resource-exhausted"
            generation = _restart(owner.runtime)
            assert len(owner.commands) == 2
            _wait_for(
                lambda: [item.generation for item in owner.deferred] == [generation]
            )
            assert [item.generation for item in owner.deferred] == [generation]
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        assert facade.resolve(space.space_id, "first")
        assert facade.resolve(space.space_id, "second")
        with pytest.raises(OrgFsError, match="unknown-doc"):
            facade.resolve(space.space_id, "third")
    finally:
        release.set()
        assert facade.close_effects(timeout=2)


def test_public_checkout_uses_lifecycle_command(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-checkout", author="user:owner")
    observed: list[object] = []
    original = runtime._ask_lifecycle
    runtime._ask_lifecycle = lambda operation: observed.append(operation) or {
        "spaceId": "space",
        "enabled": True,
    }
    try:
        assert runtime.checkout("space", True)["enabled"] is True
        assert observed == [_CheckoutSpace("space", True)]
    finally:
        runtime._ask_lifecycle = original
        runtime.close()
