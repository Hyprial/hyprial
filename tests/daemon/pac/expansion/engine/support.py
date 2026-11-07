"""Slice-local fixtures for the expansion engine tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from tests.support.pac.migrations import strip_tick_v19

from hyprial.daemon.impl.pac.contracts.workflow import WorkflowNode, WorkflowSpec
from hyprial.daemon.impl.pac.storage.store import PacGraphStore


USER = "user:test"
AGENT = "agent:test:machine:worker"


def node(
    node_id: str,
    *,
    kind: str = "task",
    owner: str | None = AGENT,
    worker: str | None = None,
    role: str = "execute",
    after: tuple[str, ...] = (),
) -> tuple[WorkflowNode, tuple[tuple[str, str, str], ...]]:
    value = WorkflowNode(
        id=node_id,
        kind=kind,
        task=f"do {node_id}",
        owner=owner,
        worker=worker,
        launch=None,
        timeout_ms=60_000,
        deadline_ms=None,
        role=role,
        first_output_eta=None,
        human_gates=None,
    )
    return value, tuple((source, node_id, "forward") for source in after)


def specification(
    name: str,
    *values: tuple[WorkflowNode, tuple[tuple[str, str, str], ...]],
    on_failure: str = "terminate",
) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        summary="",
        nodes=tuple(value[0] for value in values),
        edges=tuple(edge for value in values for edge in value[1]),
        on_failure=on_failure,
        escalate_to=None,
    )


def store(tmp_path: Path) -> PacGraphStore:
    return PacGraphStore(tmp_path / "pac.sqlite3")


def strip_v18(db: sqlite3.Connection) -> None:
    """Construct a genuine v17 fixture from a current temporary database."""

    strip_tick_v19(db)
    for trigger in (
        "workflow_graph_expansion_insert_guard",
        "workflow_graph_expansion_identity_immutable",
        "workflow_node_kind_insert_guard",
        "workflow_node_kind_immutable",
        "workflow_expansion_identity_immutable",
        "workflow_expansion_binding_guard",
        "workflow_worktree_plan_immutable",
        "workflow_outcome_expansion_insert_guard",
        "workflow_outcome_expansion_identity_immutable",
        "remote_workflow_expansion_insert_guard",
        "remote_workflow_expansion_update_guard",
    ):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in (
        "workflow_graphs_parent",
        "workflow_graphs_one_child",
        "workflow_worktrees_state",
        "workflow_worktrees_path",
        "workflow_worktrees_operation",
    ):
        db.execute(f"DROP INDEX IF EXISTS {index}")
    db.execute("DROP TABLE IF EXISTS workflow_worktrees")
    db.execute("DROP TABLE IF EXISTS workflow_expansions")
    db.execute("ALTER TABLE workflow_nodes DROP COLUMN node_kind")
    for column in ("expansion_digest", "node_id", "graph_id"):
        db.execute(
            f"ALTER TABLE workflow_outcome_receipts DROP COLUMN {column}"
        )
    for column in ("attempt_no", "expansion_digest", "expansion_text"):
        db.execute(f"ALTER TABLE remote_workflow_outbox DROP COLUMN {column}")
    for column in (
        "expansion_deadline_ms",
        "parent_node_id",
        "parent_graph_id",
    ):
        db.execute(f"ALTER TABLE workflow_graphs DROP COLUMN {column}")
    db.execute("PRAGMA user_version = 17")
