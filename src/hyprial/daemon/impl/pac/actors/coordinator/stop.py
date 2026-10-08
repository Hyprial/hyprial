"""Coordinator down-path: stop, lost and observation completion."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.pac.storage.journal  import append_event
from hyprial.daemon.impl.pac.graphs.reactor  import PlannedNotification, planned_to_json
from hyprial.daemon.impl.pac.storage.store  import NodeRow
from hyprial.daemon.impl.pac.actors.coordinator.types import LaunchSpec, RuntimeObservation


class _CoordinatorDown:
    def _complete_lost(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        observation: RuntimeObservation,
    ) -> None:
        """Commit loss, terminal direction, flag reset, and alerts atomically."""
        db = self.store.write()
        inserted: list[PlannedNotification] = []
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if (
                graph is None
                or current is None
                or current["operation_id"] != activation["operation_id"]
                or current["desired"] != "up"
                or current["op"] != "done"
            ):
                db.rollback()
                return
            at = int(self.clock())
            event_id = str(uuid4())
            db.execute(
                "UPDATE actor_activations SET desired='down',op='done',daemon_epoch=?,updated_at=? "
                "WHERE graph_id=? AND node_id=?",
                (self.daemon_epoch, at, graph_id, node.node_id),
            )
            append_event(
                db,
                graph_id=graph_id,
                version=graph["version"],
                type="actor_lost",
                at=at,
                event_id=event_id,
                data={
                    "nodeId": node.node_id,
                    "actorName": node.actor_name,
                    "incarnation": current["incarnation"],
                    "effectId": current["effect_id"],
                    "operationId": current["operation_id"],
                    "identityMarker": observation.identity_marker,
                    "daemonEpoch": self.daemon_epoch,
                },
            )
            refreshed = self.store.node(graph_id, node.node_id)
            if refreshed is not None and refreshed.flag:
                flag_event_id = str(uuid4())
                db.execute(
                    "INSERT INTO flag_events(event_id,graph_id,version,node_id,action,actor,at,reason_ref) "
                    "VALUES (?,?,?,?, 'reset','reactor',?,?)",
                    (
                        flag_event_id,
                        graph_id,
                        graph["version"],
                        node.node_id,
                        at,
                        current["effect_id"],
                    ),
                )
                db.execute(
                    "UPDATE nodes SET flag=0,flag_set_by=NULL,flag_set_at=NULL,flag_reason_ref=NULL "
                    "WHERE graph_id=? AND node_id=?",
                    (graph_id, node.node_id),
                )
                append_event(
                    db,
                    graph_id=graph_id,
                    version=graph["version"],
                    type="flag_reset",
                    at=at,
                    event_id=flag_event_id,
                    data={
                        "nodeId": node.node_id,
                        "action": "reset",
                        "actor": "reactor",
                        "reasonRef": current["effect_id"],
                    },
                )
                # The flag flip is the system's ("reactor" stays the recorded
                # actor); no principal caused it, so what it notifies comes
                # from the graph's creator (Allen, 2026-09-25).
                planned = self.reactor._plan_reset(
                    graph_id, node.node_id, flag_event_id, graph["created_by"]
                )
                inserted.extend(
                    self.reactor._insert_notifications(
                        planned,
                        at,
                        db=db,
                        graph_id=graph_id,
                        version=graph["version"],
                    )
                )
            alert = PlannedNotification(
                event_id=event_id,
                edge=f"actor:{node.node_id}:actor_lost:{current['operation_id']}",
                kind="actor_alert",
                recipient=node.owner,
                node_id=node.node_id,
                round_no=None,
                text=(
                    f"actor_lost:{node.actor_name} graph={graph_id} "
                    f"node={node.node_id}"
                ),
                sender=graph["created_by"],
                plan={
                    "graphId": graph_id,
                    "version": graph["version"],
                    "nodeId": node.node_id,
                },
            )
            db.execute(
                "INSERT OR IGNORE INTO notifications "
                "(event_id,edge,kind,recipient,round_no,text,sender,at,plan_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    alert.edge,
                    alert.kind,
                    alert.recipient,
                    None,
                    alert.text,
                    alert.sender,
                    at,
                    json.dumps(alert.plan, sort_keys=True),
                ),
            )
            append_event(
                db,
                graph_id=graph_id,
                version=graph["version"],
                type="notification_planned",
                at=at,
                data={
                    **planned_to_json(alert),
                    "sender": alert.sender,
                    "at": at,
                    "messageId": None,
                    "deliveredAt": None,
                },
            )
            inserted.append(alert)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self.reactor._deliver(graph_id, tuple(inserted))

    def _record_observation(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        kind: str,
        observation: RuntimeObservation,
        *,
        notify: bool = False,
    ) -> None:
        db = self.store.write()
        try:
            graph = self.store.graph(graph_id)
            assert graph is not None
            at = int(self.clock())
            append_event(
                db, graph_id=graph_id, version=graph["version"], type=kind, at=at,
                data={
                    "nodeId": node.node_id, "actorName": node.actor_name,
                    "incarnation": activation["incarnation"], "effectId": activation["effect_id"],
                    "operationId": activation["operation_id"], "identityMarker": observation.identity_marker,
                    "daemonEpoch": self.daemon_epoch,
                },
            )
            db.execute(
                "UPDATE actor_activations SET daemon_epoch=?,updated_at=? WHERE graph_id=? AND node_id=?",
                (self.daemon_epoch, at, graph_id, node.node_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        if notify:
            self._alert(graph_id, node, self._activation(graph_id, node.node_id) or activation, kind, observation, append=False)

    def _alert(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        kind: str,
        observation: RuntimeObservation,
        *,
        detail: str | None = None,
        append: bool = True,
    ) -> None:
        at = int(self.clock())
        event_id = str(uuid4())
        text = f"{kind}:{node.actor_name} graph={graph_id} node={node.node_id}"
        if detail:
            text += f" ({detail})"
        item = PlannedNotification(
            event_id=event_id,
            edge=f"actor:{node.node_id}:{kind}:{activation['operation_id']}",
            kind="launch_failed" if kind == "launch_failed" else "actor_alert",
            recipient=node.owner,
            node_id=node.node_id,
            round_no=None,
            text=text,
            sender=(self.store.graph(graph_id) or {})["created_by"],
            plan={"graphId": graph_id, "version": (self.store.graph(graph_id) or {})["version"], "nodeId": node.node_id},
        )
        db = self.store.write()
        try:
            graph = self.store.graph(graph_id)
            assert graph is not None
            if append:
                append_event(
                    db, graph_id=graph_id, version=graph["version"], type=kind, at=at,
                    event_id=event_id,
                    data={
                        "nodeId": node.node_id, "actorName": node.actor_name,
                        "incarnation": activation["incarnation"], "effectId": activation["effect_id"],
                        "operationId": activation["operation_id"], "identityMarker": observation.identity_marker,
                        **({"error": detail} if detail else {}),
                    },
                )
            db.execute(
                "INSERT OR IGNORE INTO notifications "
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

    def _retire_stale(
        self,
        graph_id: str,
        node: NodeRow,
        activation: dict[str, Any],
        observation: RuntimeObservation,
    ) -> None:
        launch = LaunchSpec.from_json(json.loads(activation["launch_json"])) if activation["launch_json"] else LaunchSpec(observation.harness or "unknown", None, None, None, ())
        # A delayed effect completion or daemon restart must retry the same
        # lifecycle operation, rather than minting a second stop identity.
        marker = observation.identity_marker or "missing-marker"
        stale_key = hashlib.sha256(
            f"{graph_id}\0{node.node_id}\0{marker}".encode()
        ).hexdigest()[:24]
        stale_operation = f"pac-stale-down:{stale_key}"
        result = self.runtime.stop(
            node.actor_name or "", launch, operation_id=stale_operation,
            identity_marker=observation.identity_marker or "",
        )
        if not result.present:
            db = self.store.write()
            try:
                graph = self.store.graph(graph_id)
                assert graph is not None
                append_event(
                    db, graph_id=graph_id, version=graph["version"], type="actor_down", at=int(self.clock()),
                    data={
                        "nodeId": node.node_id, "actorName": node.actor_name,
                        "incarnation": activation["incarnation"], "operationId": stale_operation,
                        "identityMarker": observation.identity_marker, "staleIncarnation": True,
                    },
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
