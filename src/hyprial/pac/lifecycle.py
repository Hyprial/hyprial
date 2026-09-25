"""Run-owned actor reconciliation over the existing hyprial lifecycle path.

PAC persists direction and operation identity before calling the runtime port.
The port is deliberately the same start/down control plane used by the daemon;
this module is a reconciler, not another process launcher.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any, Callable, Protocol
from uuid import uuid4

import yaml

from .errors import PAC_GRAPH_NOT_FOUND, PAC_GRAPH_NOT_OWNER, PAC_NODE_NOT_FOUND, PacError
from .migrations import unrewritten_owners_note
from .journal import append_event
from .reactor import (
    NullSender,
    PacReactor,
    PlannedNotification,
    planned_to_json,
)
from .store import NodeRow, PacGraphStore


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    harness: str
    provider: str | None
    model: str | None
    cwd: str | None
    args: tuple[str, ...]
    tier: str | None = None
    probes: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "args": list(self.args),
            "tier": self.tier,
            "probes": list(self.probes),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> LaunchSpec:
        return cls(
            harness=str(value["harness"]),
            provider=value.get("provider"),
            model=value.get("model"),
            cwd=value.get("cwd"),
            args=tuple(value.get("args", [])),
            tier=value.get("tier"),
            probes=tuple(value.get("probes", [])),
        )


@dataclass(frozen=True, slots=True)
class ResolvedLaunch:
    spec: LaunchSpec
    digest: str


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    present: bool
    identity_marker: str | None = None
    harness: str | None = None
    operation_id: str | None = None


class ActorRuntime(Protocol):
    def observe(self, actor_name: str) -> RuntimeObservation: ...

    def start(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation: ...

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation: ...


class LaunchResolver(Protocol):
    def __call__(self, launch_ref: str, node: NodeRow) -> ResolvedLaunch: ...


class FileLaunchResolver:
    """Resolve a versioned JSON/YAML launch reference without storing its body."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def __call__(self, launch_ref: str, node: NodeRow) -> ResolvedLaunch:
        raw_path = launch_ref.split("#", 1)[0]
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.root / path
        payload = path.read_bytes()
        fragment = launch_ref.partition("#")[2]
        if fragment.startswith("sha256=") and hashlib.sha256(payload).hexdigest() != fragment[7:]:
            raise ValueError("immutable launch reference digest mismatch")
        value = yaml.safe_load(payload)
        if not isinstance(value, dict):
            raise ValueError("launch_ref must resolve to an object")
        allowed = {"harness", "provider", "model", "cwd", "args", "tier"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown launch fields: {', '.join(sorted(unknown))}")
        harness = value.get("harness")
        args = value.get("args", [])
        required_tier = node.requires.get("tier") if node.requires is not None else None
        if required_tier is None and (not isinstance(harness, str) or not harness):
            raise ValueError("launch spec requires harness when node requires.tier is absent")
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise ValueError("launch args must be strings")
        for key in ("provider", "model", "cwd", "tier"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"launch {key} must be a string")
        probes: tuple[dict[str, Any], ...] = ()
        if required_tier is not None:
            # #425 defines requires.tier as a minimum: keep declared matrix
            # ordering, but accept a higher-tier candidate.  Selection is
            # static (2026-09-21): neither the diagnostic mark nor a liveness
            # probe can reorder or skip the pool, so ``probes`` stays empty
            # and a marked provider is still launched.
            from hyprial.dispatch.matrix import resolve_minimum_tier

            choice = resolve_minimum_tier(required_tier)
            harness = choice.harness
            provider = choice.provider
            model = choice.model
        else:
            provider = value.get("provider")
            model = value.get("model")
        return ResolvedLaunch(
            LaunchSpec(
                harness=str(harness),
                provider=provider,
                model=model,
                cwd=value.get("cwd"),
                args=tuple(args),
                tier=required_tier or value.get("tier"),
                probes=probes,
            ),
            hashlib.sha256(payload).hexdigest(),
        )


def now_ms() -> int:
    return time_ns() // 1_000_000


def request_actor_stop(store: PacGraphStore, graph_id: str, actor_name: str, *, actor: str) -> None:
    """Owner-only early stop; actor flag is never used as an intent bit."""
    db = store.write()
    try:
        graph = store.graph(graph_id)
        if graph is None:
            raise PacError(PAC_GRAPH_NOT_FOUND, f"graph {graph_id!r} not found")
        if graph["created_by"] != actor:
            note = unrewritten_owners_note(store._db, graph_id)
            raise PacError(
                PAC_GRAPH_NOT_OWNER,
                "only the graph owner can stop its actor"
                + (f"; {note}" if note else ""),
            )
        node = next((item for item in store.nodes(graph_id) if item.actor_name == actor_name), None)
        if node is None:
            raise PacError(PAC_NODE_NOT_FOUND, f"actor {actor_name!r} not found on {graph_id!r}")
        row = db.execute(
            "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
            (graph_id, node.node_id),
        ).fetchone()
        at = now_ms()
        if row is None:
            db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,updated_at) "
                "VALUES (?,?,0,'down','done',?,?,?)",
                (graph_id, node.node_id, str(uuid4()), f"pac-down:{uuid4().hex}", at),
            )
        elif not (row["desired"] == "down" and row["op"] in {"pending", "done"}):
            db.execute(
                "UPDATE actor_activations SET desired='down',op='pending',effect_id=?,"
                "operation_id=?,updated_at=? WHERE graph_id=? AND node_id=?",
                (str(uuid4()), f"pac-down:{uuid4().hex}", at, graph_id, node.node_id),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def request_actor_wake(store: PacGraphStore, graph_id: str, actor_name: str) -> None:
    """Routine-owned wake: one explicit new up intent after a completed stop/loss."""
    db = store.write()
    try:
        graph = store.graph(graph_id)
        node = next((item for item in store.nodes(graph_id) if item.actor_name == actor_name), None)
        if graph is None or graph["closed_at"] is not None or node is None:
            raise KeyError((graph_id, actor_name))
        row = db.execute(
            "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
            (graph_id, node.node_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("actor has not been activated")
        at = now_ms()
        marker = f"pac:{graph_id}:{node.node_id}:{int(row['incarnation']) + 1}:{uuid4().hex}"
        db.execute(
            "UPDATE actor_activations SET desired='up',op='pending',effect_id=?,operation_id=?,"
            "identity_marker=?,daemon_epoch=NULL,launch_digest=NULL,launch_json=NULL,updated_at=? "
            "WHERE graph_id=? AND node_id=?",
            (str(uuid4()), f"pac-start:{uuid4().hex}", marker, at, graph_id, node.node_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


class ActorCoordinator:
    """Reconcile current activation direction against one observed connector."""

    def __init__(
        self,
        store: PacGraphStore,
        runtime: ActorRuntime,
        *,
        daemon_epoch: str,
        resolver: LaunchResolver,
        sender: Any | None = None,
        clock: Any = now_ms,
        on_skip: Callable[[str, str, str, dict[str, Any]], None] | None = None,
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
        self.reactor = PacReactor(store, sender=sender if sender is not None else NullSender(), clock=clock)

    def reconcile_all(self) -> None:
        graph_ids = [row[0] for row in self.store._db.execute("SELECT graph_id FROM graphs")]
        for graph_id in graph_ids:
            self.reactor.tick_clocks(graph_id)
            for node in self.store.nodes(graph_id):
                if node.kind == "actor":
                    self.reconcile(graph_id, node.node_id)

    def _ready(self, graph_id: str, node_id: str) -> bool:
        from .workflow_graph import actor_ready, managed_graph

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

    def _request_down(self, graph_id: str, node_id: str) -> dict[str, Any]:
        db = self.store.write()
        try:
            row = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node_id),
            ).fetchone()
            assert row is not None
            if not (row["desired"] == "down" and row["op"] == "pending"):
                db.execute(
                    "UPDATE actor_activations SET desired='down',op='pending',effect_id=?,operation_id=?,"
                    "updated_at=? WHERE graph_id=? AND node_id=?",
                    (str(uuid4()), f"pac-down:{uuid4().hex}", int(self.clock()), graph_id, node_id),
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

    def reconcile(self, graph_id: str, node_id: str) -> None:
        graph = self.store.graph(graph_id)
        node = self.store.node(graph_id, node_id)
        if graph is None or node is None or node.kind != "actor" or node.actor_name is None:
            return
        activation = self._activation(graph_id, node_id)
        if activation is None:
            if graph["activated_at"] is None or graph["closed_at"] is not None or not self._ready(graph_id, node_id):
                return
            activation = self._prepare_up(graph_id, node)
        elif graph["closed_at"] is not None and activation["desired"] != "down":
            activation = self._request_down(graph_id, node_id)

        observation = self.runtime.observe(node.actor_name)
        marker = activation["identity_marker"]
        if observation.present and observation.identity_marker != marker:
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
            self._reconcile_down(graph_id, node, activation, observation)
        else:
            self._reconcile_up(graph_id, node, activation, observation)

    def _resolve_and_store(self, graph_id: str, node: NodeRow, activation: dict[str, Any]) -> LaunchSpec:
        if activation["launch_json"]:
            return LaunchSpec.from_json(json.loads(activation["launch_json"]))
        assert node.launch_ref is not None
        resolved = self.resolver(node.launch_ref, node)
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
            self._complete_up(graph_id, node, activation)
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
        self._complete_up(graph_id, node, activation)

    def _complete_up(self, graph_id: str, node: NodeRow, activation: dict[str, Any]) -> None:
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
        self, graph_id: str, node: NodeRow, activation: dict[str, Any], observation: RuntimeObservation
    ) -> None:
        if activation["op"] == "done":
            return
        launch = LaunchSpec.from_json(json.loads(activation["launch_json"])) if activation["launch_json"] else LaunchSpec("unknown", None, None, None, ())
        if observation.present:
            observation = self.runtime.stop(
                node.actor_name or "",
                launch,
                operation_id=activation["operation_id"],
                identity_marker=activation["identity_marker"],
            )
        if not observation.present:
            self._complete_down(graph_id, node, activation)
        else:
            self._skip(
                graph_id,
                node.node_id,
                "still_present_after_stop",
                operationId=activation["operation_id"],
            )

    def _skip(self, graph_id: str, node_id: str, reason: str, **detail: Any) -> None:
        if self.on_skip is not None:
            self.on_skip(graph_id, node_id, reason, detail)

    def _complete_down(self, graph_id: str, node: NodeRow, activation: dict[str, Any], *, stale: bool = False) -> None:
        db = self.store.write()
        inserted: list[PlannedNotification] = []
        try:
            graph = self.store.graph(graph_id)
            current = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node.node_id),
            ).fetchone()
            if graph is None or current is None or current["operation_id"] != activation["operation_id"] or current["op"] == "done":
                db.rollback()
                return
            at = int(self.clock())
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
        stale_operation = f"pac-stale-down:{uuid4().hex}"
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
