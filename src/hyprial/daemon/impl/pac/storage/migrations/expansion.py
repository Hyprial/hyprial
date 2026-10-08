"""Schema-18 PAC expansion custody.

SQLite cannot add composite foreign keys or table checks with ``ALTER TABLE``.
The authoritative binding table therefore owns the composite references while
small triggers fence the additive columns on the pre-existing tables.
"""

from __future__ import annotations

import sqlite3


def _upgrade_v17_to_v18(db: sqlite3.Connection) -> None:
    """Add immutable parent/child binding, outcome, and worktree custody."""

    for column in (
        "parent_graph_id TEXT",
        "parent_node_id TEXT",
        "expansion_deadline_ms INTEGER",
    ):
        db.execute(f"ALTER TABLE workflow_graphs ADD COLUMN {column}")
    db.execute(
        "ALTER TABLE workflow_nodes ADD COLUMN node_kind TEXT NOT NULL DEFAULT 'task'"
    )
    for column in ("graph_id TEXT", "node_id TEXT", "expansion_digest TEXT"):
        db.execute(f"ALTER TABLE workflow_outcome_receipts ADD COLUMN {column}")
    for column in (
        "expansion_text TEXT",
        "expansion_digest TEXT",
        "attempt_no INTEGER NOT NULL DEFAULT 0",
    ):
        db.execute(f"ALTER TABLE remote_workflow_outbox ADD COLUMN {column}")

    db.execute(
        """
        CREATE TABLE workflow_expansions (
            parent_graph_id TEXT NOT NULL,
            parent_node_id TEXT NOT NULL,
            child_graph_id TEXT NOT NULL UNIQUE,
            planner_node_id TEXT NOT NULL,
            planner_request_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation = 1),
            expansion_digest TEXT NOT NULL,
            limits_json TEXT NOT NULL,
            accepted_at INTEGER NOT NULL,
            deadline_ms INTEGER NOT NULL CHECK(deadline_ms >= accepted_at),
            PRIMARY KEY(parent_graph_id, parent_node_id),
            FOREIGN KEY(parent_graph_id, parent_node_id)
                REFERENCES workflow_nodes(graph_id, node_id),
            FOREIGN KEY(child_graph_id) REFERENCES workflow_graphs(graph_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE workflow_worktrees (
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            actor_node TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK(state IN ('planned','prepared','cleanup_pending','removed',
                                'retained','attention')),
            reason TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(graph_id, node_id),
            FOREIGN KEY(graph_id, node_id)
                REFERENCES workflow_nodes(graph_id, node_id),
            FOREIGN KEY(graph_id, actor_node) REFERENCES nodes(graph_id, node_id),
            UNIQUE(plan_json)
        )
        """
    )
    db.execute(
        "CREATE INDEX workflow_graphs_parent "
        "ON workflow_graphs(parent_graph_id,parent_node_id)"
    )
    db.execute(
        "CREATE UNIQUE INDEX workflow_graphs_one_child "
        "ON workflow_graphs(parent_graph_id,parent_node_id) "
        "WHERE parent_graph_id IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX workflow_worktrees_state "
        "ON workflow_worktrees(state,updated_at)"
    )
    db.execute(
        "CREATE UNIQUE INDEX workflow_worktrees_path "
        "ON workflow_worktrees(json_extract(plan_json,'$.path'))"
    )
    db.execute(
        "CREATE UNIQUE INDEX workflow_worktrees_operation "
        "ON workflow_worktrees(json_extract(plan_json,'$.operationId'))"
    )

    db.execute(
        """
        CREATE TRIGGER workflow_graph_expansion_insert_guard
        BEFORE INSERT ON workflow_graphs
        WHEN (NEW.parent_graph_id IS NULL) != (NEW.parent_node_id IS NULL)
          OR (NEW.parent_graph_id IS NULL) != (NEW.expansion_deadline_ms IS NULL)
          OR (NEW.parent_graph_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM workflow_nodes
                WHERE graph_id=NEW.parent_graph_id AND node_id=NEW.parent_node_id
                  AND node_kind='expansion'))
        BEGIN
            SELECT RAISE(ABORT, 'invalid workflow expansion parent');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_graph_expansion_identity_immutable
        BEFORE UPDATE OF parent_graph_id,parent_node_id,expansion_deadline_ms
        ON workflow_graphs
        WHEN NEW.parent_graph_id IS NOT OLD.parent_graph_id
          OR NEW.parent_node_id IS NOT OLD.parent_node_id
          OR NEW.expansion_deadline_ms IS NOT OLD.expansion_deadline_ms
        BEGIN
            SELECT RAISE(ABORT, 'workflow expansion graph identity is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_node_kind_insert_guard
        BEFORE INSERT ON workflow_nodes
        WHEN NEW.node_kind NOT IN ('task','report','approval','end','expansion')
        BEGIN
            SELECT RAISE(ABORT, 'invalid workflow node kind');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_node_kind_immutable
        BEFORE UPDATE OF node_kind ON workflow_nodes
        WHEN NEW.node_kind IS NOT OLD.node_kind
        BEGIN
            SELECT RAISE(ABORT, 'workflow node kind is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_expansion_identity_immutable
        BEFORE UPDATE OF parent_graph_id,parent_node_id,child_graph_id,
                         planner_node_id,planner_request_id,generation,
                         expansion_digest,limits_json,accepted_at,deadline_ms
        ON workflow_expansions
        WHEN NEW.parent_graph_id IS NOT OLD.parent_graph_id
          OR NEW.parent_node_id IS NOT OLD.parent_node_id
          OR NEW.child_graph_id IS NOT OLD.child_graph_id
          OR NEW.planner_node_id IS NOT OLD.planner_node_id
          OR NEW.planner_request_id IS NOT OLD.planner_request_id
          OR NEW.generation IS NOT OLD.generation
          OR NEW.expansion_digest IS NOT OLD.expansion_digest
          OR NEW.limits_json IS NOT OLD.limits_json
          OR NEW.accepted_at IS NOT OLD.accepted_at
          OR NEW.deadline_ms IS NOT OLD.deadline_ms
        BEGIN
            SELECT RAISE(ABORT, 'workflow expansion identity is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_expansion_binding_guard
        BEFORE INSERT ON workflow_expansions
        WHEN NOT EXISTS (
            SELECT 1 FROM workflow_graphs child
            WHERE child.graph_id=NEW.child_graph_id
              AND child.parent_graph_id=NEW.parent_graph_id
              AND child.parent_node_id=NEW.parent_node_id
              AND child.expansion_deadline_ms=NEW.deadline_ms)
        BEGIN
            SELECT RAISE(ABORT, 'workflow expansion binding disagreement');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_worktree_plan_immutable
        BEFORE UPDATE OF graph_id,node_id,actor_node,plan_json ON workflow_worktrees
        WHEN NEW.graph_id IS NOT OLD.graph_id
          OR NEW.node_id IS NOT OLD.node_id
          OR NEW.actor_node IS NOT OLD.actor_node
          OR NEW.plan_json IS NOT OLD.plan_json
        BEGIN
            SELECT RAISE(ABORT, 'workflow worktree identity is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_outcome_expansion_insert_guard
        BEFORE INSERT ON workflow_outcome_receipts
        WHEN (NEW.graph_id IS NULL) != (NEW.node_id IS NULL)
          OR (NEW.graph_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM workflow_nodes
                WHERE graph_id=NEW.graph_id AND node_id=NEW.node_id))
        BEGIN
            SELECT RAISE(ABORT, 'invalid workflow outcome identity');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_outcome_expansion_identity_immutable
        BEFORE UPDATE OF graph_id,node_id,expansion_digest
        ON workflow_outcome_receipts
        WHEN NEW.graph_id IS NOT OLD.graph_id
          OR NEW.node_id IS NOT OLD.node_id
          OR NEW.expansion_digest IS NOT OLD.expansion_digest
        BEGIN
            SELECT RAISE(ABORT, 'workflow outcome identity is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER remote_workflow_expansion_insert_guard
        BEFORE INSERT ON remote_workflow_outbox
        WHEN (NEW.expansion_text IS NULL) != (NEW.expansion_digest IS NULL)
          OR NEW.attempt_no < 0
        BEGIN
            SELECT RAISE(ABORT, 'invalid remote expansion custody');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER remote_workflow_expansion_update_guard
        BEFORE UPDATE OF expansion_text,expansion_digest,attempt_no
        ON remote_workflow_outbox
        WHEN (
            (NEW.expansion_text IS NOT OLD.expansion_text
             OR NEW.expansion_digest IS NOT OLD.expansion_digest)
            AND NEW.attempt_no != OLD.attempt_no + 1
        ) OR NEW.attempt_no < OLD.attempt_no
        BEGIN
            SELECT RAISE(ABORT, 'remote expansion custody is immutable');
        END
        """
    )


__all__ = ["_upgrade_v17_to_v18"]
