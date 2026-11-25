"""Parsing a cell into a structure: the design example, compatibility, problems."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from hyprial.daemon.impl.cells.params import PARAM_REGISTRY
from hyprial.daemon.impl.cells.structure import (
    CELL_CONNECTIONS_PARAM,
    CELL_ENTRY_PARAM,
    CELL_FAILOPEN_PARAM,
    CELL_MEMBERS_PARAM,
    STRUCTURE_PROBLEM_CODES,
    CellStructure,
    StructureConnection,
    StructureMember,
    StructureProblem,
    parse_structure,
)

from tests.daemon.cells.structure.structure_support import (
    AGENT_URI,
    design_cell,
    structure_cell,
)


def codes(structure: CellStructure) -> list[str]:
    return [problem.code for problem in structure.problems]


def test_design_example_parses_with_no_problems() -> None:
    structure = parse_structure(design_cell())
    assert structure.problems == ()
    assert structure.entry == "router"
    assert structure.fail_open == "coder"
    assert [member.name for member in structure.members] == [
        "coder",
        "lark",
        "router",
        "worker",
    ]
    by_name = {member.name: member for member in structure.members}
    assert by_name["router"].role == "harness"
    assert by_name["router"].kind == "jev"
    assert by_name["router"].model is None
    assert by_name["router"].adapter is None
    assert by_name["worker"].kind == "python-worker"
    assert by_name["coder"].kind == "claude"
    assert by_name["coder"].model == "claude-opus-5-5"
    assert by_name["lark"].role == "adapter"
    assert by_name["lark"].adapter == "team-bot"
    assert by_name["lark"].kind is None
    assert structure.connections == (
        StructureConnection("router", frozenset({"code"}), "coder"),
        StructureConnection("router", frozenset({"quick"}), "worker"),
        StructureConnection(
            "coder", frozenset({"user"}), "lark", route="project-chat"
        ),
    )
    assert structure.member("lark") is by_name["lark"]
    assert structure.member("ghost") is None
    assert [m.name for m in structure.harness_members] == ["coder", "router", "worker"]
    assert [m.name for m in structure.adapter_members] == ["lark"]


def test_parsed_objects_refuse_mutation() -> None:
    structure = parse_structure(design_cell())
    with pytest.raises(FrozenInstanceError):
        structure.members = ()
    with pytest.raises(FrozenInstanceError):
        structure.connections = ()
    with pytest.raises(FrozenInstanceError):
        structure.problems = ()
    with pytest.raises(FrozenInstanceError):
        structure.entry = "worker"
    with pytest.raises(FrozenInstanceError):
        structure.connections[0].on = frozenset({"x"})


def test_direct_construction_copies_its_collections() -> None:
    labels = {"code"}
    members = [StructureMember("a", "harness")]
    connections = [StructureConnection("a", labels, "a")]
    problems = [StructureProblem("members-shape", "bad")]
    structure = CellStructure("c.example", members, "a", connections, None, problems)
    labels.add("extra")
    members.append(StructureMember("b", "harness"))
    connections.append(StructureConnection("b", {"y"}, "b"))
    problems.append(StructureProblem("entry-type", "bad"))
    assert structure.connections[0].on == frozenset({"code"})
    assert structure.members == (StructureMember("a", "harness"),)
    assert structure.connections == (StructureConnection("a", {"code"}, "a"),)
    assert structure.problems == (StructureProblem("members-shape", "bad"),)


def test_direct_construction_refuses_wrong_field_types() -> None:
    with pytest.raises(TypeError, match="on"):
        StructureConnection("a", "code", "b")
    with pytest.raises(TypeError, match="members"):
        StructureConnection("a", {"code"}, 5)
    with pytest.raises(TypeError, match="labels"):
        StructureConnection("a", set(), "b")
    with pytest.raises(TypeError, match="route"):
        StructureConnection("a", {"code"}, "b", route=3)
    with pytest.raises(TypeError, match="name"):
        StructureMember("", "harness")
    with pytest.raises(TypeError, match="kind"):
        StructureMember("a", "harness", kind=3)
    with pytest.raises(TypeError, match="StructureMember"):
        CellStructure("c.example", ["a"], "a", (), None)
    with pytest.raises(TypeError, match="cell_id"):
        CellStructure("", (), None, (), None)
    with pytest.raises(TypeError, match="code"):
        StructureProblem(7, "detail")


def test_single_harness_member_without_entry_is_the_entry() -> None:
    structure = parse_structure(structure_cell(((AGENT_URI, "harness"),)))
    assert codes(structure) == []
    assert structure.entry == AGENT_URI
    assert structure.fail_open is None
    assert structure.connections == ()
    member = structure.member(AGENT_URI)
    assert member is not None
    assert member.kind is None
    assert member.model is None


def test_no_harness_members_means_no_entry_and_is_legal() -> None:
    structure = parse_structure(structure_cell((("lark", "adapter"),)))
    assert codes(structure) == []
    assert structure.entry is None
    structure = parse_structure(structure_cell())
    assert codes(structure) == []
    assert structure.entry is None
    assert structure.members == ()


def test_harness_member_without_members_entry_keeps_kind_none() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness")),
            {
                "cell.members": {"b": {"kind": "python-worker"}},
                "cell.entry": "a",
            },
        )
    )
    assert codes(structure) == []
    assert structure.member("a") is not None
    assert structure.member("a").kind is None
    assert structure.member("b") is not None
    assert structure.member("b").kind == "python-worker"


def test_agent_address_ref_stays_an_opaque_member_name() -> None:
    structure = parse_structure(
        structure_cell(
            ((AGENT_URI, "harness"),),
            {
                "cell.members": {AGENT_URI: {"kind": "claude"}},
                "cell.entry": AGENT_URI,
                "cell.connections": [
                    {"from": AGENT_URI, "on": "done", "to": AGENT_URI}
                ],
            },
        )
    )
    assert codes(structure) == []
    assert structure.entry == AGENT_URI
    assert structure.member(AGENT_URI).kind == "claude"
    assert structure.connections == (
        StructureConnection(AGENT_URI, frozenset({"done"}), AGENT_URI),
    )


def test_several_harness_members_without_entry_have_no_entry() -> None:
    structure = parse_structure(
        structure_cell((("a", "harness"), ("b", "harness")))
    )
    assert codes(structure) == []
    assert structure.entry is None


def test_label_list_covers_every_label() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"),),
            {
                "cell.connections": [
                    {"from": "a", "on": ["code", "review"], "to": "a"}
                ]
            },
        )
    )
    assert codes(structure) == []
    assert structure.connections == (
        StructureConnection("a", frozenset({"code", "review"}), "a"),
    )


BAD_PARAMS: list[tuple[dict[str, Any], str]] = [
    ({"cell.members": ["x"]}, "members-shape"),
    ({"cell.members": {"a": "jev"}}, "members-item-shape"),
    ({"cell.members": {"a": {"kind": 3}}}, "members-field-type"),
    ({"cell.members": {"ghost": {"kind": "jev"}}}, "members-unknown-member"),
    ({"cell.entry": 7}, "entry-type"),
    ({"cell.entry": "ghost"}, "entry-unknown-member"),
    ({"cell.entry": "lark"}, "entry-not-harness"),
    ({"cell.connections": {"from": "a"}}, "connections-shape"),
    ({"cell.connections": [{"from": "a", "on": "x"}]}, "connection-shape"),
    ({"cell.connections": [{"from": "a", "on": 5, "to": "a"}]}, "connection-label-type"),
    (
        {"cell.connections": [{"from": "a", "on": "x", "to": "ghost"}]},
        "connection-unknown-member",
    ),
    (
        {
            "cell.connections": [
                {"from": "a", "on": "user", "to": "lark", "route": 3}
            ]
        },
        "connection-route-type",
    ),
    (
        {"cell.connections": [{"from": "a", "on": "x", "to": "a", "route": "r"}]},
        "connection-route-misplaced",
    ),
    (
        {
            "cell.connections": [
                {"from": "a", "on": "x", "to": "a"},
                {"from": "a", "on": "x", "to": "lark"},
            ]
        },
        "connection-duplicate",
    ),
    ({"cell.failOpen": 4}, "fail-open-type"),
    ({"cell.failOpen": "ghost"}, "fail-open-unknown-member"),
    ({"cell.members": {"a": {"allow": "Read"}}}, "members-policy-type"),
    (
        {"cell.members": {"a": {"allow": ["Read"], "deny": ["Read"]}}},
        "members-policy-overlap",
    ),
]


@pytest.mark.parametrize(
    ("params", "expected"), BAD_PARAMS, ids=[code for _, code in BAD_PARAMS]
)
def test_bad_data_reports_its_distinguishable_code(
    params: dict[str, Any], expected: str
) -> None:
    members = (("a", "harness"), ("lark", "adapter"))
    structure = parse_structure(structure_cell(members, params))
    assert codes(structure) == [expected]
    assert [member.name for member in structure.members] == ["a", "lark"]


def test_bad_connection_is_ignored_and_the_others_survive() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("lark", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": "first", "to": "lark"},
                    {"from": "ghost", "on": "x", "to": "a"},
                    {"from": "a", "on": "second", "to": "lark"},
                ]
            },
        )
    )
    assert codes(structure) == ["connection-unknown-member"]
    assert [(c.from_member, sorted(c.on), c.to) for c in structure.connections] == [
        ("a", ["first"], "lark"),
        ("a", ["second"], "lark"),
    ]


def test_invalid_entry_falls_back_to_the_single_harness_member() -> None:
    structure = parse_structure(
        structure_cell((("a", "harness"), ("lark", "adapter")), {"cell.entry": 7})
    )
    assert codes(structure) == ["entry-type"]
    assert structure.entry == "a"


def test_invalid_fail_open_reads_as_absent() -> None:
    structure = parse_structure(
        structure_cell((("a", "harness"),), {"cell.failOpen": "ghost"})
    )
    assert codes(structure) == ["fail-open-unknown-member"]
    assert structure.fail_open is None


def test_every_problem_code_is_exercised_above() -> None:
    assert set(STRUCTURE_PROBLEM_CODES) == {code for _, code in BAD_PARAMS}


def test_the_four_keys_are_declared_in_the_param_registry() -> None:
    assert PARAM_REGISTRY.get("cell.members") == CELL_MEMBERS_PARAM
    assert PARAM_REGISTRY.get("cell.entry") == CELL_ENTRY_PARAM
    assert CELL_MEMBERS_PARAM.type == "object"
    assert CELL_ENTRY_PARAM.type == "string"
    assert CELL_CONNECTIONS_PARAM.type == "any"
    assert CELL_FAILOPEN_PARAM.type == "string"
