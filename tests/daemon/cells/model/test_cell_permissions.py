"""The cell permission surface and the phase A write rule."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellError, CellMember, CellScope
from hyprial.daemon.impl.cells.permissions import (
    PERMISSION_ACTIONS,
    CellPermissions,
    permits,
    require_authority,
)
from hyprial.kernel import keyed_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE


def _cell(permissions: CellPermissions) -> Cell:
    return Cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org("acme"),
        binding=CellBinding.keyed("eng"),
        members=(CellMember("agent:bob:mac:w"),),
        permissions=permissions,
    )


def test_actions_are_ordered_read_write_admin() -> None:
    assert PERMISSION_ACTIONS == ("read", "write", "admin")


def test_the_authority_holds_every_action() -> None:
    cell = _cell(CellPermissions())
    for action in PERMISSION_ACTIONS:
        assert permits(cell, ALICE, action)
    assert not permits(cell, "user:bob", "read")


def test_higher_grants_imply_lower_ones() -> None:
    cell = _cell(CellPermissions.of(admin=("user:carol",), write=("user:dan",)))
    assert permits(cell, "user:carol", "read")
    assert permits(cell, "user:carol", "write")
    assert permits(cell, "user:dan", "read")
    assert not permits(cell, "user:dan", "admin")


def test_wildcard_and_members_tokens() -> None:
    cell = _cell(CellPermissions.of(read=("*",), write=("@members",)))
    assert permits(cell, "user:anyone", "read")
    assert not permits(cell, "user:anyone", "write")
    assert permits(cell, "agent:bob:mac:w", "write")


def test_unknown_action_and_bad_principals_are_refused() -> None:
    with pytest.raises(CellError):
        permits(_cell(CellPermissions()), ALICE, "delete")
    with pytest.raises(CellError):
        CellPermissions.of(read=("",))
    with pytest.raises(CellError):
        CellPermissions({"post": frozenset({"*"})})


def test_permissions_round_trip_through_json() -> None:
    permissions = CellPermissions.of(read=("*", "user:b"), admin=("user:c",))
    assert CellPermissions.from_json(permissions.to_json()) == permissions
    assert permissions.to_json() == {"admin": ["user:c"], "read": ["*", "user:b"]}


def test_only_the_authority_may_author_a_cell_in_phase_a() -> None:
    cell = _cell(CellPermissions.of(admin=("user:carol",)))
    require_authority(cell, ALICE)
    with pytest.raises(CellError) as raised:
        require_authority(cell, "user:carol")
    assert raised.value.code == "not-authority"
