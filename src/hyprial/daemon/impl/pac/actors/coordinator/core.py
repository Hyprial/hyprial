"""Coordinator core: construction, reconciliation and cleanup rows."""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import uuid4

from hyprial.daemon.impl.pac.storage.journal  import append_event
from hyprial.daemon.impl.pac.graphs.reactor  import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store  import NodeRow, PacGraphStore
from hyprial.daemon.impl.pac.actors.coordinator.types import (
    ActorRuntime,
    LaunchResolver,
    RuntimeObservation,
    now_ms,
)


class _CoordinatorCore:
    """Reconcile current activation direction against one observed connector."""

    def __init__(
        self,
        store: PacGraphStore,
        runtime: ActorRuntime,
        *,
        daemon_epoch: str,
        resolver: LaunchResolver,
        sender: Any | None = None,
        defer_notifications: bool = False,
        clock: Any = now_ms,
        on_skip: Callable[[str, str, str, dict[str, Any]], None] | None = None,
        logger: Any = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        # Told why a reconcile left an activation where it was.  Both paths
        # below used to return silently: a closed graph whose workers were
        # reclaimed only 73 s later left no line saying why (2026-09-24).
        self.on_skip = on_skip
        self.daemon_epoch = daemon_epoch
        self.resolver = resolver
        self.sender = sender
        self.clock = clock
        self.reactor = PacReactor(
            store,
            sender=(None if defer_notifications else sender if sender is not None else NullSender()),
            clock=clock,
            logger=logger,
        )

    def reconcile_all(self) -> None:
        graph_ids = [row[0] for row in self.store._db.execute("SELECT graph_id FROM graphs")]
        for graph_id in graph_ids:
            self.reactor.tick_clocks(graph_id)
            for node in self.store.nodes(graph_id):
                if node.kind == "actor":
                    self.reconcile(graph_id, node.node_id)

    def _ready(self, graph_id: str, node_id: str) -> bool:
        from hyprial.daemon.impl.pac.workflows.graphs  import actor_ready, managed_graph

        if managed_graph(self.store, graph_id):
            return actor_ready(self.store, graph_id, node_id, now_ms=int(self.clock()))
        predecessors = [
            edge.from_node
            for edge in self.store.edges(graph_id)
            if edge.kind == "forward" and edge.to_node == node_id
        ]
        nodes = {node.node_id: node for node in self.store.nodes(graph_id)}
        return all(nodes[item].flag for item in predecessors)

    def _activation(self, graph_id: str, node_id: str) -> dict[str, Any] | None:
        row = self.store._db.execute(
            "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
            (graph_id, node_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def _prepare_up(self, graph_id: str, node: NodeRow) -> dict[str, Any]:
        db = self.store.write()
        try:
            existing = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if existing is None:
                effect = str(uuid4())
                marker = f"pac:{graph_id}:{node.node_id}:1:{effect}"
                db.execute(
                    "INSERT INTO actor_activations "
                    "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,identity_marker,updated_at) "
                    "VALUES (?,?,0,'up','pending',?,?,?,?)",
                    (graph_id, node.node_id, effect, f"pac-start:{uuid4().hex}", marker, int(self.clock())),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        activation = self._activation(graph_id, node.node_id)
        assert activation is not None
        return activation

    def _request_down(
        self, graph_id: str, node_id: str, *, operation_id: str | None = None
    ) -> dict[str, Any]:
        db = self.store.write()
        try:
            row = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node_id),
            ).fetchone()
            assert row is not None
            if not (
                row["desired"] == "down"
                and row["op"] == "pending"
                and (operation_id is None or row["operation_id"] == operation_id)
            ):
                db.execute(
                    "UPDATE actor_activations SET desired='down',op='pending',effect_id=?,operation_id=?,"
                    "updated_at=? WHERE graph_id=? AND node_id=?",
                    (
                        str(uuid4()),
                        operation_id or f"pac-down:{uuid4().hex}",
                        int(self.clock()),
                        graph_id,
                        node_id,
                    ),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        activation = self._activation(graph_id, node_id)
        assert activation is not None
        return activation

    def _known_markers(self, graph_id: str, node_id: str) -> set[str]:
        markers: set[str] = set()
        for row in self.store._db.execute(
            "SELECT data_json FROM journal WHERE graph_id=? AND type='actor_up'", (graph_id,)
        ):
            data = json.loads(row[0])
            if data.get("nodeId") == node_id and isinstance(data.get("identityMarker"), str):
                markers.add(data["identityMarker"])
        return markers

    @staticmethod
    def _observation_document(observation: RuntimeObservation) -> dict[str, Any]:
        return {
            "present": observation.present,
            "identityMarker": observation.identity_marker,
            "harness": observation.harness,
            "operationId": observation.operation_id,
            "agentEntityToken": observation.agent_entity_token,
        }

    def _cleanup_attention(
        self,
        graph_id: str,
        node: NodeRow,
        intent: dict[str, Any],
        reason: str,
        observation: dict[str, Any],
    ) -> None:
        db = self.store.write()
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM workflow_worker_cleanup_intents "
                "WHERE graph_id=? AND actor_node=?",
                (graph_id, node.node_id),
            ).fetchone()
            if (
                graph is None
                or current is None
                or current["operation_id"] != intent["operation_id"]
                or current["state"] == "complete"
            ):
                db.rollback()
                return
            at = int(self.clock())
            observation_json = json.dumps(
                observation, ensure_ascii=False, sort_keys=True
            )
            changed = current["state"] != "attention" or current[
                "attention_reason"
            ] != reason
            db.execute(
                "UPDATE workflow_worker_cleanup_intents SET state='attention',"
                "attention_reason=?,last_observation_json=?,updated_at=? "
                "WHERE graph_id=? AND actor_node=?",
                (reason, observation_json, at, graph_id, node.node_id),
            )
            if changed:
                append_event(
                    db,
                    graph_id=graph_id,
                    version=graph["version"],
                    type="workflow_changed",
                    at=at,
                    data={
                        "workerCleanup": "attention",
                        "actorNode": node.node_id,
                        "actorName": node.actor_name,
                        "operationId": current["operation_id"],
                        "ageMs": max(0, at - int(current["created_at"])),
                        "reason": reason,
                        "lastObservation": observation,
                    },
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._skip(
            graph_id,
            node.node_id,
            reason,
            actorName=node.actor_name,
            operationId=intent["operation_id"],
            ageMs=max(0, int(self.clock()) - int(intent["created_at"])),
            lastObservation=observation,
        )

    def _settle_cleanup_effect_failure(
        self,
        graph_id: str,
        node_id: str,
        *,
        operation_id: str | None,
        reason: str,
        error_type: str,
        present: bool,
        detail: str,
    ) -> bool:
        """Commit a deferred observe/stop failure on the PAC writer."""

        node = self.store.node(graph_id, node_id)
        intent = self.store.workflow_worker_cleanup_intent(graph_id, node_id)
        if node is None or intent is None or intent["state"] != "pending":
            return False
        if operation_id is not None and intent["operation_id"] != operation_id:
            return False
        self._cleanup_attention(
            graph_id,
            node,
            intent,
            reason,
            {
                "errorType": error_type,
                "detail": detail[:500],
                **({"present": True} if present else {}),
            },
        )
        return True

    def _complete_cleanup_without_activation(
        self,
        graph_id: str,
        node: NodeRow,
        intent: dict[str, Any],
        observation: RuntimeObservation,
    ) -> None:
        db = self.store.write()
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM workflow_worker_cleanup_intents "
                "WHERE graph_id=? AND actor_node=?",
                (graph_id, node.node_id),
            ).fetchone()
            if (
                graph is None
                or current is None
                or current["operation_id"] != intent["operation_id"]
                or current["state"] != "pending"
            ):
                db.rollback()
                return
            at = int(self.clock())
            db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
                "identity_marker,daemon_epoch,updated_at) "
                "VALUES (?,?,0,'down','done',?,?,NULL,?,?)",
                (
                    graph_id,
                    node.node_id,
                    str(uuid4()),
                    current["operation_id"],
                    self.daemon_epoch,
                    at,
                ),
            )
            self._complete_cleanup_row(
                db, graph, node, current, at, observation=observation
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _complete_cleanup_row(
        self,
        db: Any,
        graph: dict[str, Any],
        node: NodeRow,
        intent: Any,
        at: int,
        *,
        observation: RuntimeObservation,
    ) -> None:
        document = self._observation_document(observation)
        db.execute(
            "UPDATE workflow_worker_cleanup_intents SET state='complete',"
            "attention_reason=NULL,last_observation_json=?,updated_at=? "
            "WHERE graph_id=? AND actor_node=? AND operation_id=?",
            (
                json.dumps(document, ensure_ascii=False, sort_keys=True),
                at,
                graph["graph_id"],
                node.node_id,
                intent["operation_id"],
            ),
        )
        db.execute(
            "UPDATE workflow_worker_receipts SET state='down' "
            "WHERE graph_id=? AND actor_node=?",
            (graph["graph_id"], node.node_id),
        )
        append_event(
            db,
            graph_id=graph["graph_id"],
            version=graph["version"],
            type="workflow_changed",
            at=at,
            data={
                "workerCleanup": "complete",
                "actorNode": node.node_id,
                "actorName": node.actor_name,
                "operationId": intent["operation_id"],
                "ageMs": max(0, at - int(intent["created_at"])),
                "lastObservation": document,
                "workerState": "stopped",
            },
        )
