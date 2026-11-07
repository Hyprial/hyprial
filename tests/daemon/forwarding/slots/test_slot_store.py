"""Slot store: fixed endpoints, persisted assignment, growth on exhaustion.

Pins the two restart acceptance facts of the slot design (§5): the
connect set is a pure function of home and pool size, and a reloaded
store hands every peer the slot it held before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.forwarding.slots import (
    SLOT_POOL_ENV,
    SlotAssignments,
    slot_endpoints,
    slot_pool_size,
    slot_socket_paths,
)
from hyprial.daemon.impl.forwarding.slots.store import SLOTS_STATE_RELPATH


def test_endpoints_are_fixed_and_membership_free(tmp_path: Path) -> None:
    endpoints = slot_endpoints(tmp_path, 3)
    assert endpoints == (
        f"unixsock-stream/{tmp_path}/state/tailcat/slots/slot-00",
        f"unixsock-stream/{tmp_path}/state/tailcat/slots/slot-01",
        f"unixsock-stream/{tmp_path}/state/tailcat/slots/slot-02",
    )
    # The up request's paths are the same sockets, same order.
    assert tuple(
        endpoint.removeprefix("unixsock-stream/") for endpoint in endpoints
    ) == slot_socket_paths(tmp_path, 3)


def test_assignment_survives_a_reload(tmp_path: Path) -> None:
    store = SlotAssignments(tmp_path, base_size=4)
    slot_a, grown_a = store.assign("alice/dev-a")
    slot_b, grown_b = store.assign("bob/dev-b")
    assert (slot_a, grown_a) == (0, False)
    assert (slot_b, grown_b) == (1, False)
    assert store.assign("alice/dev-a") == (0, False)

    reloaded = SlotAssignments(tmp_path, base_size=4)
    assert reloaded.slot_for("alice/dev-a") == 0
    assert reloaded.slot_for("bob/dev-b") == 1
    assert reloaded.endpoints() == store.endpoints()


def test_release_frees_the_slot_for_the_next_peer(tmp_path: Path) -> None:
    store = SlotAssignments(tmp_path, base_size=4)
    store.assign("alice/dev-a")
    store.assign("bob/dev-b")
    assert store.release("alice/dev-a") == 0
    assert store.release("alice/dev-a") is None
    assert store.assign("carol/dev-c") == (0, False)


def test_exhaustion_grows_by_one_base_step_and_persists(tmp_path: Path) -> None:
    store = SlotAssignments(tmp_path, base_size=2)
    store.assign("a/1")
    store.assign("b/2")
    slot, grown = store.assign("c/3")
    assert (slot, grown) == (2, True)
    assert store.pool_size == 4
    assert len(store.endpoints()) == 4

    # A restart keeps the grown pool: the connect set must not shrink
    # under a live assignment.
    reloaded = SlotAssignments(tmp_path, base_size=2)
    assert reloaded.pool_size == 4
    assert reloaded.slot_for("c/3") == 2


def test_a_corrupt_state_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / SLOTS_STATE_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    store = SlotAssignments(tmp_path, base_size=4)
    assert store.assignments == {}
    assert store.assign("alice/dev-a") == (0, False)


def test_duplicate_slots_in_the_file_keep_first_only(tmp_path: Path) -> None:
    path = tmp_path / SLOTS_STATE_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"v": 1, "poolSize": 4, "assignments": {"a/1": 0, "b/2": 0, "c/3": 7}}
        ),
        encoding="utf-8",
    )
    store = SlotAssignments(tmp_path, base_size=4)
    assert store.slot_for("a/1") == 0
    assert store.slot_for("b/2") is None
    # A slot above the recorded pool size keeps the pool large enough.
    assert store.slot_for("c/3") == 7
    assert store.pool_size == 8


def test_pool_size_env_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SLOT_POOL_ENV, raising=False)
    assert slot_pool_size() == 32
    monkeypatch.setenv(SLOT_POOL_ENV, "8")
    assert slot_pool_size() == 8
    monkeypatch.setenv(SLOT_POOL_ENV, "0")
    with pytest.raises(ValueError):
        slot_pool_size()
