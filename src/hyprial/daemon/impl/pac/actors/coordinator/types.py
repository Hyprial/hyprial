"""Launch vocabulary and actor lifecycle helpers for PAC reconciliation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any, Protocol
from uuid import uuid4

import yaml

from hyprial.identity import (
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_NOT_OWNER,
    PAC_NODE_NOT_FOUND,
    PacError,
)
from hyprial.daemon.impl.pac.storage.migrations  import unrewritten_owners_note
from hyprial.daemon.impl.pac.storage.store  import NodeRow, PacGraphStore


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
    agent_entity_token: str | None = None
    retirement_pending: bool = False


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
            from hyprial.daemon.impl.dispatch.matrix import resolve_minimum_tier

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


# A connector that remains present after three ordinary lifecycle down effects
# is no longer treated as eventually consistent.  The durable intent moves to
# attention and requires an operator to resolve the identity/process ambiguity.
CLEANUP_ATTEMPT_LIMIT = 3


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
