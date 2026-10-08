"""Data-only contracts shared by PAC expansion preflight and publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSpec


@dataclass(frozen=True)
class PreparedExpansion:
    """Validated immutable input handed from preflight to the graph writer."""

    parent_graph_id: str
    planner_node_id: str
    planner_request_id: str
    placeholder_node_id: str
    child_graph_id: str
    expansion_digest: str
    captured_input_token: str
    effective_limits: dict[str, Any]
    policy_snapshot: dict[str, Any]
    spec: WorkflowSpec
    prepared_artifacts: dict[str, Any]
    worktrees: tuple[dict[str, Any], ...]


__all__ = ["PreparedExpansion"]
