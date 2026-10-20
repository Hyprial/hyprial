"""Coordinator core: construction, reconciliation and cleanup rows.

Terminal worker cleanup converges instead of parking (2026-10-18, Allen:
"PAC should reclaim workers automatically; no agent may be left behind"):

* ``attention`` is a visible projection, not a terminal state.  Every failed
  evaluation counts one attempt, and the fallback sweep keeps re-evaluating
  the intent until it reaches ``complete``.
* A closed managed graph whose receipt has no cleanup intent (closed before
  schema 16 introduced intents) gets the same intent ``close_workflow``
  would have written, then follows the ordinary cleanup path.
* An observed process is this actor's own stale incarnation when its strict
  PAC marker names exactly this graph and actor node, or when it carries no
  marker and its agent entity token equals the receipt's token.  Cleanup
  stops such a process with its observed fence; a marker naming another
  graph or node, or a foreign entity token, still fails closed.
* An activation without a marker (a launch that reported failure) proves
  nothing during cleanup: an unmarked process no longer "matches" it, and a
  NULL receipt token never matches an observed token.
* A marker-proven incarnation whose receipt never recorded a token (launch
  reported failure while the process came up) adopts the observed token, so
  the PAC GC can later prove the agent incarnation and remove it.  Adoption
  widens trust from "may stop" to "GC may delete", so it additionally needs
  the node's journal to show that marker was issued here: an event carrying
  it as ``identityMarker``, or a ``launch_failed`` whose incarnation + 1 and
  effect id rebuild it (the ``_prepare_up`` marker shape).  A marker that
  only names this node is still stopped but lends no token.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import uuid4

from hyprial.daemon.impl.pac.storage.journal  import append_event
from hyprial.kernel import (
    HookEventPublisher,
    worker_cleanup_complete_event_payload,
)
from hyprial.daemon.impl.pac.graphs.reactor  import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store  import NodeRow, PacGraphStore
from hyprial.daemon.impl.pac.actors.coordinator.types import (
    ActorRuntime,
    LaunchResolver,
    RuntimeObservation,
    WorkerCleanupComplete,
    cleanup_operation_base,
    now_ms,
)
from hyprial.daemon.impl.pac.actors.marker import (
    pac_worker_fields,
    pac_worker_marker,
)

OPEN_CLEANUP = frozenset({"pending", "attention"})


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
        publisher: HookEventPublisher | None = None,
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
        self.publisher = publisher
        self.reactor = PacReactor(
            store,
            sender=(None if defer_notifications else sender if sender is not None else NullSender()),
            clock=clock,
            logger=logger,
            publisher=publisher,
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
    def _own_incarnation(
        graph_id: str,
        node_id: str,
        receipt: Any,
        observation: RuntimeObservation,
    ) -> bool:
        marker = pac_worker_marker(observation.identity_marker)
        if observation.identity_marker is not None:
            if marker is None:
                return False
            marker_graph, marker_node, _incarnation, _effect = pac_worker_fields(marker)
            return marker_graph == graph_id and marker_node == node_id
        return (
            receipt is not None
            and receipt["agent_entity_token"] is not None
            and observation.agent_entity_token == receipt["agent_entity_token"]
        )

    def _issued_marker(self, graph_id: str, node_id: str, marker: str) -> bool:
        for row in self.store._db.execute(
            "SELECT type,data_json FROM journal WHERE graph_id=? "
            "AND type IN ('actor_up','launch_failed','actor_lost','actor_down',"
            "'actor_restored','actor_unowned')",
            (graph_id,),
        ):
            data = json.loads(row["data_json"])
            if data.get("nodeId") != node_id:
                continue
            if data.get("identityMarker") == marker:
                return True
            if (
                row["type"] == "launch_failed"
                and isinstance(data.get("incarnation"), int)
                and data.get("effectId")
                and marker
                == f"pac:{graph_id}:{node_id}:{data['incarnation'] + 1}:{data['effectId']}"
            ):
                return True
        return False

    def _adopt_receipt_token(
        self, graph_id: str, node_id: str, observation: RuntimeObservation
    ) -> None:
        if observation.agent_entity_token is None:
            return
        if not self._issued_marker(
            graph_id, node_id, str(observation.identity_marker)
        ):
            return
        db = self.store.write()
        try:
            db.execute(
                "UPDATE workflow_worker_receipts SET agent_entity_token=? "
                "WHERE graph_id=? AND actor_node=? AND agent_entity_token IS NULL",
                (observation.agent_entity_token, graph_id, node_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _backfill_cleanup_intent(
        self, graph_id: str, receipt: Any
    ) -> dict[str, Any] | None:
        db = self.store.write()
        try:
            graph = self.store.graph(graph_id)
            if graph is None or graph["closed_at"] is None:
                db.rollback()
                return None
            at = int(self.clock())
            operation_id = f"pac-cleanup:{uuid4().hex}"
            inserted = db.execute(
                "INSERT OR IGNORE INTO workflow_worker_cleanup_intents "
                "(graph_id,actor_node,actor_name,actor_uri,operation_id,state,"
                "attempts,created_at,updated_at) VALUES (?,?,?,?,?,'pending',0,?,?)",
                (
                    graph_id,
                    receipt["actor_node"],
                    receipt["actor_name"],
                    receipt["actor_uri"],
                    operation_id,
                    at,
                    at,
                ),
            )
            if inserted.rowcount:
                append_event(
                    db,
                    graph_id=graph_id,
                    version=graph["version"],
                    type="workflow_changed",
                    at=at,
                    data={
                        "workerCleanup": "pending",
                        "actorNode": receipt["actor_node"],
                        "actorName": receipt["actor_name"],
                        "operationId": operation_id,
                    },
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return self.store.workflow_worker_cleanup_intent(
            graph_id, str(receipt["actor_node"])
        )

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
        *,
        count_attempt: bool = True,
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
                "attention_reason=?,last_observation_json=?,updated_at=?,"
                "attempts=attempts+? "
                "WHERE graph_id=? AND actor_node=?",
                (
                    reason,
                    observation_json,
                    at,
                    1 if count_attempt else 0,
                    graph_id,
                    node.node_id,
                ),
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
        if node is None or intent is None or intent["state"] not in OPEN_CLEANUP:
            return False
        if (
            operation_id is not None
            and intent["operation_id"] != cleanup_operation_base(operation_id)
        ):
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
                or current["state"] not in OPEN_CLEANUP
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
            record = self._complete_cleanup_row(
                db, graph, node, current, at, observation=observation
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._publish_cleanup_complete(record)

    def _publish_cleanup_complete(
        self, record: WorkerCleanupComplete | None
    ) -> None:
        """Publish one worker cleanup fact after its transaction committed."""

        if record is None or self.publisher is None:
            return
        self.publisher.publish(
            "worker.cleanup.complete",
            payload=worker_cleanup_complete_event_payload(
                graph_id=record.graph_id,
                actor_node=record.actor_node,
                actor_name=record.actor_name,
                operation_id=record.operation_id,
                at_ms=record.at_ms,
            ),
            emitted_at_ms=record.at_ms,
            graph_id=record.graph_id,
        )

    def _complete_cleanup_row(
        self,
        db: Any,
        graph: dict[str, Any],
        node: NodeRow,
        intent: Any,
        at: int,
        *,
        observation: RuntimeObservation,
    ) -> WorkerCleanupComplete:
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
        return WorkerCleanupComplete(
            graph_id=str(graph["graph_id"]),
            actor_node=str(node.node_id),
            actor_name=str(node.actor_name),
            operation_id=str(intent["operation_id"]),
            at_ms=int(at),
        )
