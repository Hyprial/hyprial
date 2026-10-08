"""Coordinator up-path: activation resolution and start completion."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from hyprial.identity import PacError, WORKFLOW_WORKER_RECEIPT_MISMATCH
from hyprial.daemon.impl.pac.storage.journal  import append_event
from hyprial.daemon.impl.pac.graphs.reactor  import PlannedNotification, planned_to_json
from hyprial.daemon.impl.pac.storage.store  import NodeRow
from hyprial.daemon.impl.pac.actors.coordinator.types import (
    CLEANUP_ATTEMPT_LIMIT,
    LaunchSpec,
    RuntimeObservation,
)


class _CoordinatorUp:
    def reconcile(self, graph_id: str, node_id: str) -> None:
        graph = self.store.graph(graph_id)
        node = self.store.node(graph_id, node_id)
        if graph is None or node is None or node.kind != "actor" or node.actor_name is None:
            return
        cleanup = self.store.workflow_worker_cleanup_intent(graph_id, node_id)
        receipt = self.store.workflow_worker_receipt(graph_id, node_id)
        if cleanup is not None and cleanup["state"] in {"complete", "attention"}:
            return
        activation = self._activation(graph_id, node_id)
        if activation is None:
            if cleanup is not None:
                try:
                    observation = self.runtime.observe(node.actor_name)
                except Exception as error:  # noqa: BLE001 - durable outcome
                    self._cleanup_attention(
                        graph_id,
                        node,
                        cleanup,
                        "lifecycle_manager_unavailable",
                        {
                            "errorType": type(error).__name__,
                            "detail": str(error)[:500],
                        },
                    )
                    return
                if observation.present:
                    self._cleanup_attention(
                        graph_id,
                        node,
                        cleanup,
                        "unknown_identity",
                        self._observation_document(observation),
                    )
                else:
                    self._complete_cleanup_without_activation(
                        graph_id, node, cleanup, observation
                    )
                return
            if graph["activated_at"] is None or graph["closed_at"] is not None or not self._ready(graph_id, node_id):
                return
            activation = self._prepare_up(graph_id, node)
        elif graph["closed_at"] is not None and activation["desired"] != "down":
            if receipt is not None and cleanup is None:
                return
            activation = self._request_down(
                graph_id,
                node_id,
                operation_id=(cleanup["operation_id"] if cleanup else None),
            )

        try:
            observation = self.runtime.observe(node.actor_name)
        except Exception as error:  # noqa: BLE001 - durable cleanup outcome
            if cleanup is None:
                raise
            self._cleanup_attention(
                graph_id,
                node,
                cleanup,
                "lifecycle_manager_unavailable",
                {
                    "errorType": type(error).__name__,
                    "detail": str(error)[:500],
                },
            )
            return
        marker = activation["identity_marker"]
        if (
            cleanup is not None
            and (observation.present or observation.retirement_pending)
            and receipt is not None
            and receipt["agent_entity_token"] is not None
            and observation.agent_entity_token != receipt["agent_entity_token"]
        ):
            self._cleanup_attention(
                graph_id,
                node,
                cleanup,
                (
                    "unknown_identity"
                    if observation.agent_entity_token is None
                    else "entity_token_mismatch"
                ),
                self._observation_document(observation),
            )
            return
        if (
            observation.present or observation.retirement_pending
        ) and observation.identity_marker != marker:
            if cleanup is not None:
                self._cleanup_attention(
                    graph_id,
                    node,
                    cleanup,
                    (
                        "unknown_identity"
                        if observation.identity_marker is None
                        else "marker_mismatch"
                    ),
                    self._observation_document(observation),
                )
                return
            if observation.identity_marker in self._known_markers(graph_id, node_id):
                self._retire_stale(graph_id, node, activation, observation)
            elif activation["daemon_epoch"] != self.daemon_epoch:
                self._record_observation(
                    graph_id, node, activation, "actor_unowned", observation, notify=True
                )
            else:
                self._skip(
                    graph_id,
                    node_id,
                    "marker_mismatch",
                    desired=activation["desired"],
                    expected=marker,
                    observed=observation.identity_marker,
                )
            return
        if activation["desired"] == "down":
            self._reconcile_down(
                graph_id, node, activation, observation, cleanup=cleanup
            )
        else:
            self._reconcile_up(graph_id, node, activation, observation)

    def _validate_receipt_launch(
        self, graph_id: str, node: NodeRow, launch_digest: str | None
    ) -> None:
        from hyprial.daemon.impl.pac.workflows.graphs  import managed_graph

        if not managed_graph(self.store, graph_id):
            return
        receipt = self.store.workflow_worker_receipt(graph_id, node.node_id)
        if receipt is not None and (
            launch_digest is not None
            and receipt["launch_digest"] != launch_digest
        ):
            raise PacError(
                WORKFLOW_WORKER_RECEIPT_MISMATCH,
                "workflow worker receipt launch digest mismatch",
            )
        roster = self.store.workflow_roster(graph_id)
        if roster is None:  # historical graph whose artifacts were not derivable
            return
        if receipt is None:
            raise PacError(
                WORKFLOW_WORKER_RECEIPT_MISMATCH,
                "workflow worker receipt launch digest mismatch",
            )

    def _resolve_and_store(self, graph_id: str, node: NodeRow, activation: dict[str, Any]) -> LaunchSpec:
        if activation["launch_json"]:
            self._validate_receipt_launch(
                graph_id, node, activation["launch_digest"]
            )
            return LaunchSpec.from_json(json.loads(activation["launch_json"]))
        assert node.launch_ref is not None
        resolved = self.resolver(node.launch_ref, node)
        self._validate_receipt_launch(graph_id, node, resolved.digest)
        db = self.store.write()
        try:
            current = db.execute(
                "SELECT operation_id,desired,op FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if current is None or tuple(current) != (activation["operation_id"], "up", "pending"):
                raise RuntimeError("activation changed while launch reference resolved")
            db.execute(
                "UPDATE actor_activations SET launch_digest=?,launch_json=?,updated_at=? "
                "WHERE graph_id=? AND node_id=?",
                (resolved.digest, json.dumps(resolved.spec.to_json(), sort_keys=True), int(self.clock()), graph_id, node.node_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return resolved.spec

    def _reconcile_up(
        self, graph_id: str, node: NodeRow, activation: dict[str, Any], observation: RuntimeObservation
    ) -> None:
        if activation["op"] == "done":
            if activation["identity_marker"] is None:
                return
            if observation.present:
                if activation["daemon_epoch"] != self.daemon_epoch:
                    self._record_observation(
                        graph_id, node, activation, "actor_restored", observation
                    )
            else:
                # Absence is meaningful in every daemon epoch. The previous
                # epoch guard made this branch unreachable after a successful
                # start in the current process and left the actor flag stuck.
                self._complete_lost(graph_id, node, activation, observation)
            return
        if observation.present:
            self._complete_up(graph_id, node, activation, observation)
            return
        try:
            launch = self._resolve_and_store(graph_id, node, activation)
            observation = self.runtime.start(
                node.actor_name or "",
                launch,
                operation_id=activation["operation_id"],
                identity_marker=activation["identity_marker"],
            )
            if not observation.present or observation.identity_marker != activation["identity_marker"]:
                raise RuntimeError("start settled without the activation identity marker")
        except Exception as error:  # noqa: BLE001 - failure is a durable lifecycle outcome
            self._launch_failed(graph_id, node, activation, error)
            return
        self._complete_up(graph_id, node, activation, observation)

    def _complete_up(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        observation: RuntimeObservation,
    ) -> None:
        self._validate_receipt_launch(
            graph_id, node, activation["launch_digest"]
        )
        db = self.store.write()
        inserted: list[PlannedNotification] = []
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if graph is None or current is None or current["operation_id"] != activation["operation_id"]:
                db.rollback()
                return
            if graph["closed_at"] is not None or current["desired"] != "up":
                db.execute(
                    "UPDATE actor_activations SET desired='down',op='pending',effect_id=?,operation_id=?,updated_at=? "
                    "WHERE graph_id=? AND node_id=?",
                    (str(uuid4()), f"pac-down:{uuid4().hex}", int(self.clock()), graph_id, node.node_id),
                )
                db.commit()
                return
            if current["op"] == "done":
                db.commit()
                return
            at = int(self.clock())
            incarnation = int(current["incarnation"]) + 1
            receipt = db.execute(
                "SELECT agent_entity_token FROM workflow_worker_receipts "
                "WHERE graph_id=? AND actor_node=?",
                (graph_id, node.node_id),
            ).fetchone()
            if receipt is not None and observation.agent_entity_token is not None:
                if (
                    receipt["agent_entity_token"] is not None
                    and receipt["agent_entity_token"]
                    != observation.agent_entity_token
                ):
                    raise PacError(
                        WORKFLOW_WORKER_RECEIPT_MISMATCH,
                        "workflow worker receipt agent entity token mismatch",
                    )
                db.execute(
                    "UPDATE workflow_worker_receipts SET agent_entity_token=? "
                    "WHERE graph_id=? AND actor_node=? "
                    "AND agent_entity_token IS NULL",
                    (observation.agent_entity_token, graph_id, node.node_id),
                )
            db.execute(
                "UPDATE actor_activations SET incarnation=?,op='done',daemon_epoch=?,updated_at=? "
                "WHERE graph_id=? AND node_id=?",
                (incarnation, self.daemon_epoch, at, graph_id, node.node_id),
            )
            append_event(
                db, graph_id=graph_id, version=graph["version"], type="actor_up", at=at,
                data={
                    "nodeId": node.node_id, "actorName": node.actor_name,
                    "incarnation": incarnation, "effectId": current["effect_id"],
                    "operationId": current["operation_id"], "identityMarker": current["identity_marker"],
                    "daemonEpoch": self.daemon_epoch,
                    "launchRef": node.launch_ref, "launchDigest": current["launch_digest"],
                    "launch": json.loads(current["launch_json"]) if current["launch_json"] else None,
                },
            )
            if not node.flag:
                event_id = str(uuid4())
                db.execute(
                    "INSERT INTO flag_events(event_id,graph_id,version,node_id,action,actor,at,reason_ref) "
                    "VALUES (?,?,?,?, 'set','reactor',?,?)",
                    (event_id, graph_id, graph["version"], node.node_id, at, current["effect_id"]),
                )
                db.execute(
                    "UPDATE nodes SET flag=1,flag_set_by='reactor',flag_set_at=?,flag_reason_ref=? "
                    "WHERE graph_id=? AND node_id=?",
                    (at, current["effect_id"], graph_id, node.node_id),
                )
                append_event(
                    db, graph_id=graph_id, version=graph["version"], type="flag_set", at=at,
                    event_id=event_id,
                    data={"nodeId": node.node_id, "action": "set", "actor": "reactor", "reasonRef": current["effect_id"]},
                )
                planned = self.reactor._plan_set(graph_id, node.node_id, event_id, graph["created_by"])
                inserted = self.reactor._insert_notifications(
                    planned, at, db=db, graph_id=graph_id, version=graph["version"]
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self.reactor._deliver(graph_id, tuple(inserted))

    def _reconcile_down(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        observation: RuntimeObservation,
        *,
        cleanup: dict[str, Any] | None = None,
    ) -> None:
        if activation["op"] == "done":
            if cleanup is not None:
                if observation.present or observation.retirement_pending:
                    activation = self._request_down(
                        graph_id,
                        node.node_id,
                        operation_id=cleanup["operation_id"],
                    )
                else:
                    self._complete_down(
                        graph_id,
                        node,
                        activation,
                        cleanup=cleanup,
                        observation=observation,
                    )
                    return
            else:
                return
        if cleanup is not None and activation["operation_id"] != cleanup["operation_id"]:
            activation = self._request_down(
                graph_id,
                node.node_id,
                operation_id=cleanup["operation_id"],
            )
        launch = LaunchSpec.from_json(json.loads(activation["launch_json"])) if activation["launch_json"] else LaunchSpec("unknown", None, None, None, ())
        if observation.present or observation.retirement_pending:
            try:
                observation = self.runtime.stop(
                    node.actor_name or "",
                    launch,
                    operation_id=activation["operation_id"],
                    identity_marker=activation["identity_marker"],
                )
            except Exception as error:  # noqa: BLE001 - durable cleanup outcome
                if cleanup is None:
                    raise
                self._cleanup_attention(
                    graph_id,
                    node,
                    cleanup,
                    (
                        "stop_timeout"
                        if isinstance(error, TimeoutError)
                        else "lifecycle_manager_unavailable"
                    ),
                    {
                        "errorType": type(error).__name__,
                        "detail": str(error)[:500],
                        "present": True,
                    },
                )
                return
        if not observation.present and not observation.retirement_pending:
            self._complete_down(
                graph_id,
                node,
                activation,
                cleanup=cleanup,
                observation=observation,
            )
            return
        if cleanup is not None:
            db = self.store.write()
            try:
                current = db.execute(
                    "SELECT * FROM workflow_worker_cleanup_intents "
                    "WHERE graph_id=? AND actor_node=?",
                    (graph_id, node.node_id),
                ).fetchone()
                if (
                    current is None
                    or current["operation_id"] != cleanup["operation_id"]
                    or current["state"] != "pending"
                ):
                    db.rollback()
                    return
                attempts = int(current["attempts"]) + 1
                at = int(self.clock())
                document = self._observation_document(observation)
                db.execute(
                    "UPDATE workflow_worker_cleanup_intents SET attempts=?,"
                    "last_observation_json=?,updated_at=? "
                    "WHERE graph_id=? AND actor_node=?",
                    (
                        attempts,
                        json.dumps(document, ensure_ascii=False, sort_keys=True),
                        at,
                        graph_id,
                        node.node_id,
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
            if attempts >= CLEANUP_ATTEMPT_LIMIT:
                refreshed = self.store.workflow_worker_cleanup_intent(
                    graph_id, node.node_id
                )
                assert refreshed is not None
                self._cleanup_attention(
                    graph_id,
                    node,
                    refreshed,
                    "stop_timeout",
                    self._observation_document(observation),
                )
                return
        self._skip(
            graph_id,
            node.node_id,
            "still_present_after_stop",
            operationId=activation["operation_id"],
        )

    def _skip(self, graph_id: str, node_id: str, reason: str, **detail: Any) -> None:
        if self.on_skip is not None:
            self.on_skip(graph_id, node_id, reason, detail)

    def _complete_down(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        *,
        stale: bool = False,
        cleanup: dict[str, Any] | None = None,
        observation: RuntimeObservation | None = None,
    ) -> None:
        db = self.store.write()
        inserted: list[PlannedNotification] = []
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if graph is None or current is None or current["operation_id"] != activation["operation_id"]:
                db.rollback()
                return
            at = int(self.clock())
            if current["op"] != "done":
                db.execute(
                    "UPDATE actor_activations SET op='done',daemon_epoch=?,updated_at=? WHERE graph_id=? AND node_id=?",
                    (self.daemon_epoch, at, graph_id, node.node_id),
                )
                append_event(
                    db, graph_id=graph_id, version=graph["version"], type="actor_down", at=at,
                    data={
                        "nodeId": node.node_id, "actorName": node.actor_name,
                        "incarnation": current["incarnation"], "effectId": current["effect_id"],
                        "operationId": current["operation_id"], "identityMarker": current["identity_marker"],
                        "daemonEpoch": self.daemon_epoch,
                        **({"staleIncarnation": True} if stale else {}),
                    },
                )
            if cleanup is not None:
                cleanup_row = db.execute(
                    "SELECT * FROM workflow_worker_cleanup_intents "
                    "WHERE graph_id=? AND actor_node=?",
                    (graph_id, node.node_id),
                ).fetchone()
                if (
                    cleanup_row is not None
                    and cleanup_row["state"] == "pending"
                    and cleanup_row["operation_id"] == cleanup["operation_id"]
                ):
                    self._complete_cleanup_row(
                        db,
                        graph,
                        node,
                        cleanup_row,
                        at,
                        observation=observation or RuntimeObservation(False),
                    )
            refreshed = self.store.node(graph_id, node.node_id)
            if refreshed is not None and refreshed.flag:
                event_id = str(uuid4())
                db.execute(
                    "INSERT INTO flag_events(event_id,graph_id,version,node_id,action,actor,at,reason_ref) "
                    "VALUES (?,?,?,?, 'reset','reactor',?,?)",
                    (event_id, graph_id, graph["version"], node.node_id, at, current["effect_id"]),
                )
                db.execute(
                    "UPDATE nodes SET flag=0,flag_set_by=NULL,flag_set_at=NULL,flag_reason_ref=NULL "
                    "WHERE graph_id=? AND node_id=?",
                    (graph_id, node.node_id),
                )
                append_event(
                    db, graph_id=graph_id, version=graph["version"], type="flag_reset", at=at,
                    event_id=event_id,
                    data={"nodeId": node.node_id, "action": "reset", "actor": "reactor", "reasonRef": current["effect_id"]},
                )
                planned = self.reactor._plan_reset(graph_id, node.node_id, event_id, graph["created_by"])
                inserted = self.reactor._insert_notifications(
                    planned, at, db=db, graph_id=graph_id, version=graph["version"]
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self.reactor._deliver(graph_id, tuple(inserted))

    def _launch_failed(self, graph_id: str, node: NodeRow, activation: dict[str, Any], error: Exception) -> None:
        """Commit failed outcome, terminal activation and owner outbox together."""
        at = int(self.clock())
        event_id = str(uuid4())
        graph = self.store.graph(graph_id)
        assert graph is not None
        text = f"launch_failed:{node.actor_name} graph={graph_id} node={node.node_id} ({error})"
        item = PlannedNotification(
            event_id=event_id,
            edge=f"actor:{node.node_id}:launch_failed:{activation['operation_id']}",
            kind="launch_failed",
            recipient=node.owner,
            node_id=node.node_id,
            round_no=None,
            text=text,
            sender=graph["created_by"],
            plan={"graphId": graph_id, "version": graph["version"], "nodeId": node.node_id},
        )
        db = self.store.write()
        try:
            current = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if current is None or current["operation_id"] != activation["operation_id"] or current["op"] != "pending":
                db.rollback()
                return
            db.execute(
                "UPDATE actor_activations SET op='done',identity_marker=NULL,daemon_epoch=?,updated_at=? "
                "WHERE graph_id=? AND node_id=?",
                (self.daemon_epoch, at, graph_id, node.node_id),
            )
            append_event(
                db, graph_id=graph_id, version=graph["version"], type="launch_failed", at=at,
                event_id=event_id,
                data={
                    "nodeId": node.node_id, "actorName": node.actor_name,
                    "incarnation": current["incarnation"], "effectId": current["effect_id"],
                    "operationId": current["operation_id"], "identityMarker": None,
                    "daemonEpoch": self.daemon_epoch,
                    "error": str(error),
                },
            )
            db.execute(
                "INSERT INTO notifications "
                "(event_id,edge,kind,recipient,round_no,text,sender,at,plan_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, item.edge, item.kind, item.recipient, None, text, item.sender, at, json.dumps(item.plan, sort_keys=True)),
            )
            append_event(
                db, graph_id=graph_id, version=graph["version"], type="notification_planned", at=at,
                data={**planned_to_json(item), "sender": item.sender, "at": at, "messageId": None, "deliveredAt": None},
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self.reactor._deliver(graph_id, (item,))
