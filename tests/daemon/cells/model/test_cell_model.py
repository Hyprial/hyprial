"""Cell model: kinds, scopes, bindings, members and first-class params."""

from __future__ import annotations

from typing import get_args

import pytest

from hyprial.daemon.impl.cells.model import (
    CELL_KINDS,
    EAGER_KINDS,
    Cell,
    CellBinding,
    CellError,
    CellKind,
    CellMember,
    CellScope,
    default_scope,
)
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.permissions import CellPermissions
from hyprial.kernel import keyed_cell_id, mint_cell_id, subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, authority_for

BOB = authority_for("user:bob")

AGENT_ID = "a." + "b" * 26


def _cell(**overrides: object) -> Cell:
    fields: dict[str, object] = {
        "cell_id": keyed_cell_id("agent", ALICE, "eng"),
        "kind": "agent",
        "authority": ALICE,
        "scope": CellScope.of_org("acme"),
        "binding": CellBinding.keyed("eng"),
    }
    fields.update(overrides)
    return Cell(**fields)  # type: ignore[arg-type]


def test_phase_a_kinds_and_eager_subset() -> None:
    assert CELL_KINDS == frozenset({"agent", "org"})
    assert EAGER_KINDS == CELL_KINDS
    assert default_scope("agent") == CellScope.of_org()
    with pytest.raises(CellError) as raised:
        default_scope("org")
    assert raised.value.code == "invalid-cell"


def test_department_is_not_a_cell_kind() -> None:
    assert "department" not in CELL_KINDS
    assert "department" not in get_args(CellKind)
    with pytest.raises(CellError) as refused:
        default_scope("department")
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        CellSpec(kind="department", binding=CellBinding.keyed("eng"))
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        _cell(
            cell_id=keyed_cell_id("department", ALICE, "eng"),
            kind="department",
            scope=CellScope.of_org(),
            binding=CellBinding.keyed("eng"),
        )
    assert refused.value.code == "invalid-cell"


def test_graph_is_not_a_cell_kind() -> None:
    assert "graph" not in CELL_KINDS
    assert "graph" not in get_args(CellKind)
    with pytest.raises(CellError) as refused:
        default_scope("graph")
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        CellSpec(kind="graph", binding=CellBinding.keyed("digest"))
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        _cell(
            cell_id=keyed_cell_id("graph", ALICE, "digest"),
            kind="graph",
            scope=CellScope.owner(),
            binding=CellBinding.keyed("digest"),
        )
    assert refused.value.code == "invalid-cell"


def test_routine_is_not_a_cell_kind() -> None:
    assert "routine" not in CELL_KINDS
    assert "routine" not in get_args(CellKind)
    with pytest.raises(CellError) as refused:
        default_scope("routine")
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        CellSpec(kind="routine", binding=CellBinding.keyed("agent-default"))
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        _cell(
            cell_id=keyed_cell_id("routine", ALICE, "agent-default"),
            kind="routine",
            scope=CellScope.of_org(),
            binding=CellBinding.keyed("agent-default"),
        )
    assert refused.value.code == "invalid-cell"


def test_conversation_is_not_a_cell_kind() -> None:
    assert "conversation" not in CELL_KINDS
    assert "conversation" not in get_args(CellKind)
    with pytest.raises(CellError) as refused:
        default_scope("conversation")
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        CellSpec(kind="conversation", binding=CellBinding.keyed("t.x\0conv-1"))
    assert refused.value.code == "invalid-cell"
    with pytest.raises(CellError) as refused:
        _cell(
            cell_id=keyed_cell_id("conversation", ALICE, "t.x\0conv-1"),
            kind="conversation",
            scope=CellScope.owner(),
            binding=CellBinding.keyed("t.x\0conv-1"),
        )
    assert refused.value.code == "invalid-cell"


def test_binding_derives_the_cell_id() -> None:
    assert CellBinding.keyed("eng").derive("org", ALICE) == (
        keyed_cell_id("org", ALICE, "eng")
    )
    assert CellBinding.subject(AGENT_ID).derive("agent", ALICE) == (
        subject_cell_id("agent", AGENT_ID)
    )
    assert CellBinding.minted().derive("agent", ALICE) is None


def test_cell_id_must_match_its_binding() -> None:
    with pytest.raises(CellError) as raised:
        _cell(cell_id=keyed_cell_id("agent", BOB, "eng"))
    assert raised.value.code == "id-mismatch"
    minted = mint_cell_id()
    assert _cell(cell_id=minted, binding=CellBinding.minted()).cell_id == minted


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "workflow-definition"),
        ("authority", "alice"),
        ("cell_id", "c.short"),
        ("revision", 0),
    ],
)
def test_invalid_fields_are_refused(field: str, value: object) -> None:
    with pytest.raises(CellError) as raised:
        _cell(**{field: value})
    assert raised.value.code in {"invalid-cell", "id-mismatch"}


@pytest.mark.parametrize(
    "authority",
    ["user:alice", "user:u.alice", "user:u.ou_short", "agent:u.ou_" + "a" * 32, "user:" + "u." + "A" * 32],
)
def test_only_a_user_id_authority_is_accepted(authority: str) -> None:
    with pytest.raises(CellError) as raised:
        _cell(authority=authority, binding=CellBinding.minted(), cell_id=mint_cell_id())
    assert raised.value.code == "invalid-cell"
    assert "user:u.<sub>" in raised.value.message


def test_a_document_in_the_old_username_spelling_no_longer_decodes() -> None:
    from hyprial.daemon.impl.cells.codec import decode, encode

    text = encode(_cell(cell_id=mint_cell_id(), binding=CellBinding.minted()))
    old = text.replace(ALICE, "user:alice")
    assert old != text
    with pytest.raises(CellError) as raised:
        decode(old)
    assert raised.value.code == "invalid-cell"


def test_owner_scope_takes_no_org_and_org_scope_names_one_or_all() -> None:
    assert CellScope.owner().org is None
    assert CellScope.of_org().org is None
    assert CellScope.of_org("acme").org == "acme"
    with pytest.raises(CellError):
        CellScope("owner", "acme")
    with pytest.raises(CellError):
        CellScope("space", None)  # type: ignore[arg-type]


def test_members_are_normalised_unique_and_ordered() -> None:
    cell = _cell(
        members=(
            CellMember("agent:alice:mac:b"),
            CellMember("agent:alice:mac:a", "lead"),
            CellMember("agent:alice:mac:b"),
        )
    )
    assert [member.ref for member in cell.members] == [
        "agent:alice:mac:a",
        "agent:alice:mac:b",
    ]
    assert cell.has_member("agent:alice:mac:a")
    assert not cell.has_member("agent:alice:mac:c")
    with pytest.raises(CellError):
        CellMember("")
    with pytest.raises(CellError):
        CellMember("agent:alice:mac:a", "Lead Role")


def test_params_are_a_first_class_frozen_json_map() -> None:
    params = {"delivery.bodyRetentionMs": 3_600_000, "custom.policy": {"k": 2}}
    cell = _cell(params=params)
    params["delivery.bodyRetentionMs"] = 1
    assert cell.params["delivery.bodyRetentionMs"] == 3_600_000
    with pytest.raises(TypeError):
        cell.params["x"] = 1  # type: ignore[index]
    assert cell.param("custom.policy") == {"k": 2}
    assert cell.param("missing", 7) == 7
    with pytest.raises(CellError):
        _cell(params={"": 1})
    with pytest.raises(CellError):
        _cell(params={"x": float("nan")})
    with pytest.raises(CellError):
        _cell(params={"x": object()})


def test_revised_bumps_revision_and_keeps_identity() -> None:
    cell = _cell(permissions=CellPermissions.of(read=("*",)))
    revised = cell.revised(params={"a": 1}, now_ms=5)
    assert revised.revision == cell.revision + 1
    assert revised.cell_id == cell.cell_id and revised.params == {"a": 1}
    assert revised.permissions == cell.permissions
    assert revised.updated_at_ms == 5
    retired = revised.revised(retired=True, now_ms=6)
    assert retired.retired and retired.revision == revised.revision + 1


def test_cells_are_hashable_and_equal_cells_hash_equal() -> None:
    first = Cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.keyed("eng"),
        params={"nested": {"a": [1]}},
    )
    second = Cell(
        cell_id=first.cell_id,
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.keyed("eng"),
        params={"nested": {"a": [1]}},
    )
    assert first == second and hash(first) == hash(second)
    assert len({first, second}) == 1
