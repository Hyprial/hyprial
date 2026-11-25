"""Cell builders for the structure tests (no OrgFS)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.kernel import subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE

OWNER = ALICE
AGENT_ID = "a.x6qg5s2n46p7qli5msgy5ko6by"
AGENT_URI = "agent:alice:mac:worker"

DESIGN_MEMBERS: tuple[tuple[str, str], ...] = (
    ("router", "harness"),
    ("worker", "harness"),
    ("coder", "harness"),
    ("lark", "adapter"),
)

DESIGN_PARAMS: dict[str, Any] = {
    "cell.members": {
        "router": {"kind": "jev"},
        "worker": {"kind": "python-worker"},
        "coder": {"kind": "claude", "model": "claude-opus-5-5"},
        "lark": {"adapter": "team-bot"},
    },
    "cell.entry": "router",
    "cell.connections": [
        {"from": "router", "on": "code", "to": "coder"},
        {"from": "router", "on": "quick", "to": "worker"},
        {"from": "coder", "on": "user", "to": "lark", "route": "project-chat"},
    ],
    "cell.failOpen": "coder",
}


def structure_cell(
    members: Sequence[tuple[str, str]] = (),
    params: Mapping[str, Any] | None = None,
    *,
    agent_id: str = AGENT_ID,
    authority: str = OWNER,
) -> Cell:
    """One agent cell with *members* as (ref, role) pairs and *params*."""

    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=authority,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        members=tuple(CellMember(ref, role) for ref, role in members),
        params=dict(params or {}),
    )


def design_cell() -> Cell:
    """The full §5.1 example document as a cell."""

    return structure_cell(DESIGN_MEMBERS, DESIGN_PARAMS)
