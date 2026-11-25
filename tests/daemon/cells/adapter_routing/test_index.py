"""Unit tests for the read-only adapter routing reverse index (I6-3)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.cells.adapter_routing import (
    AdapterRoutingIndex,
    build_adapter_routing_index,
)
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.ports import CellIndex
from hyprial.kernel import subject_cell_id
from tests.daemon.cells.params.param_support import org_cell
from tests.daemon.cells.runtime.cell_runtime_support import authority_for

OWNER = authority_for("user:alice")


def _agent_cell(
    agent_id: str,
    *,
    name: str,
    members: tuple[CellMember, ...],
    params: dict[str, object],
    retired: bool = False,
) -> Cell:
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        name=name,
        members=members,
        params=params,
        retired=retired,
    )


def test_empty_index_resolves_unmatched() -> None:
    index = build_adapter_routing_index(CellIndex())
    assert isinstance(index, AdapterRoutingIndex)
    assert index.adapters == ()
    assert index.conflicts == ()
    assert index.lookup("lark-main") is None
    assert index.is_conflicted("lark-main") is False
    resolution = index.resolve("lark-main")
    assert resolution.status == "unmatched"
    assert resolution.target is None
    assert resolution.candidates == ()


def test_single_adapter_binding_and_routes_are_indexed() -> None:
    agent_id = "a.alpha00000000000000000001"
    cell = _agent_cell(
        agent_id,
        name="alpha",
        members=(
            CellMember("planner", "harness"),
            CellMember("feishu", "adapter"),
        ),
        params={
            "cell.entry": "planner",
            "cell.members": {
                "planner": {"kind": "claude"},
                "feishu": {"adapter": "lark-work"},
            },
            "cell.connections": [
                {"from": "planner", "on": "user", "to": "feishu", "route": "ops-chat"},
                {"from": "planner", "on": "user", "to": "feishu", "route": "alerts"},
            ],
        },
    )
    dept_cell = org_cell("sp-dept-1", authority=OWNER)

    index = build_adapter_routing_index(
        CellIndex(1, {cell.cell_id: cell, dept_cell.cell_id: dept_cell})
    )

    assert index.adapters == ("lark-work",)
    assert index.conflicts == ()
    target = index.lookup("lark-work")
    assert target is not None
    assert target.adapter == "lark-work"
    assert target.cell_id == cell.cell_id
    assert target.agent_id == agent_id
    assert target.cell_name == "alpha"
    assert target.member_name == "feishu"
    assert target.routes == ("alerts", "ops-chat")
    assert index.resolve("lark-work").status == "matched"


def test_cross_cell_adapter_conflict_fails_closed_until_retired() -> None:
    c1 = _agent_cell(
        "a.alpha00000000000000000001",
        name="alpha",
        members=(CellMember("feishu", "adapter"),),
        params={"cell.members": {"feishu": {"adapter": "lark-shared"}}},
    )
    c2 = _agent_cell(
        "a.beta000000000000000000001",
        name="beta",
        members=(CellMember("bot", "adapter"),),
        params={"cell.members": {"bot": {"adapter": "lark-shared"}}},
    )

    conflicted = build_adapter_routing_index(CellIndex(1, {c1.cell_id: c1, c2.cell_id: c2}))
    assert conflicted.is_conflicted("lark-shared") is True
    assert conflicted.lookup("lark-shared") is None
    resolution = conflicted.resolve("lark-shared")
    assert resolution.status == "conflict"
    assert resolution.target is None
    assert [t.cell_id for t in resolution.candidates] == sorted([c1.cell_id, c2.cell_id])
    assert len(conflicted.conflicts) == 1
    assert conflicted.conflicts[0].adapter == "lark-shared"

    resolved = build_adapter_routing_index(
        CellIndex(2, {c1.cell_id: c1, c2.cell_id: replace(c2, retired=True)})
    )
    assert resolved.is_conflicted("lark-shared") is False
    assert resolved.lookup("lark-shared") is not None
    assert resolved.lookup("lark-shared").cell_id == c1.cell_id
    assert resolved.conflicts == ()


def test_same_cell_multiple_adapter_members_are_deterministic_and_immutable() -> None:
    c1 = _agent_cell(
        "a.alpha00000000000000000001",
        name="alpha",
        members=(
            CellMember("in-a", "adapter"),
            CellMember("in-b", "adapter"),
            CellMember("worker", "harness"),
        ),
        params={
            "cell.members": {
                "in-a": {"adapter": "lark-one"},
                "in-b": {"adapter": "lark-one"},
                "worker": {"adapter": "ignored-because-harness"},
            },
            "cell.connections": [
                {"from": "worker", "on": "user", "to": "in-b", "route": "r-b"},
                {"from": "worker", "on": "user", "to": "in-a", "route": "r-a"},
            ],
        },
    )

    index = build_adapter_routing_index(CellIndex(1, {c1.cell_id: c1}))
    assert index.is_conflicted("lark-one") is False
    assert index.lookup("ignored-because-harness") is None
    targets = index.targets_for("lark-one")
    assert [t.member_name for t in targets] == ["in-a", "in-b"]
    resolution = index.resolve("lark-one")
    assert resolution.status == "matched"
    assert resolution.target is not None
    assert resolution.target.cell_id == c1.cell_id
    assert resolution.target.routes == ("r-a", "r-b")

    with pytest.raises(TypeError):
        index.by_adapter["lark-one"] = ()  # type: ignore[index]
