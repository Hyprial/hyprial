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
    cwd_policy: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "args": list(self.args),
            "tier": self.tier,
            "probes": list(self.probes),
            **(
                {"cwdPolicy": self.cwd_policy}
                if self.cwd_policy is not None
                else {}
            ),
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
            cwd_policy=value.get("cwdPolicy"),
        )


@dataclass(frozen=True, slots=True)
class WorkerCleanupComplete:
    """One durable transition of a worker cleanup intent to ``complete``.

    Built inside the transaction that flips the row and published only after
    the caller's commit, so the fact and its hook event agree.
    """

    graph_id: str
    actor_node: str
    actor_name: str
    operation_id: str
    at_ms: int


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
    pinned: bool = False
    interactive_online: bool = False


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
        allowed = {
            "harness",
            "provider",
            "model",
            "cwd",
            "cwdPolicy",
            "args",
            "tier",
        }
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
        for key in ("provider", "model", "cwd", "cwdPolicy", "tier"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"launch {key} must be a string")
        if value.get("cwdPolicy") not in (None, "require", "create"):
            raise ValueError("launch cwdPolicy must be require or create")
        if value.get("cwdPolicy") is not None and value.get("cwd") is None:
            raise ValueError("launch cwdPolicy requires cwd")
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
                cwd_policy=value.get("cwdPolicy"),
            ),
            hashlib.sha256(payload).hexdigest(),
        )


def now_ms() -> int:
    return time_ns() // 1_000_000


# A connector that remains present after three ordinary lifecycle down effects
# is no longer treated as eventually consistent.  The durable intent moves to
# attention; attention stays retryable on the fallback sweep until reclaimed.
CLEANUP_ATTEMPT_LIMIT = 3

CLEANUP_RETRY_SEPARATOR = ":retry:"

CLEANUP_RETRY_BASE_MS = 300_000
CLEANUP_RETRY_CAP_MS = 6 * 3600 * 1000
CLEANUP_RETRIES_PER_SWEEP = 2


def cleanup_retry_delay_ms(attempts: int) -> int:
    """Backoff before the fallback sweep re-admits a failed cleanup intent.

    Each retry of a present worker submits a fresh lifecycle deactivate that
    can hold an actor worker for the whole lifecycle budget, so a degraded
    lifecycle plane must not be retried at sweep cadence.  The delay doubles
    per counted attempt from five minutes and is capped at six hours; a
    first evaluation (no attempts) is not delayed.  At most
    ``CLEANUP_RETRIES_PER_SWEEP`` (2) retries may be in flight on the graph
    authority's four effect threads, so retries alone can never hold more
    than two.  First evaluations, worktree cleanup and expansion preflight
    are not counted and can still fill the pool.
    """

    if attempts <= 0:
        return 0
    return min(CLEANUP_RETRY_BASE_MS * 2 ** min(attempts - 1, 32), CLEANUP_RETRY_CAP_MS)


def throttle_cleanup_retries(
    rows: Any, jobs: list[tuple[str, str]], now_ms: int, active: Any = frozenset()
) -> list[tuple[str, str]]:
    """Admit only due cleanup retries, bounded across in-flight work.

    A row whose open cleanup intent already counted failed attempts waits
    ``cleanup_retry_delay_ms(attempts)`` after its last update.  Retry jobs
    still queued or running (``active``: the actor-service queue plus the
    graph authority's effect pool, where production work actually runs) use
    up ``CLEANUP_RETRIES_PER_SWEEP`` first; the remaining slots go to the
    oldest due retries.  The actor sweep runs on the 30 s running tier as
    well as the 300 s fallback tier, so without the in-flight count a
    degraded lifecycle plane could keep all four graph-authority effect
    threads busy with retry stops.  First evaluations and every other job
    pass through unchanged, so the cap bounds retries only; it does not
    reserve effect threads for open graphs.
    """

    retries: set[tuple[str, str]] = set()
    due: list[tuple[int, tuple[str, str]]] = []
    for row in rows:
        if row["cleanup_state"] not in ("pending", "attention"):
            continue
        attempts = int(row["cleanup_attempts"] or 0)
        if attempts <= 0:
            continue
        job = (str(row["graph_id"]), str(row["node_id"]))
        retries.add(job)
        updated_at = int(row["cleanup_updated_at"] or 0)
        if job not in active and updated_at + cleanup_retry_delay_ms(attempts) <= now_ms:
            due.append((updated_at, job))
    slots = max(0, CLEANUP_RETRIES_PER_SWEEP - len(retries & set(active)))
    admitted = {job for _at, job in sorted(due)[:slots]}
    return [job for job in jobs if job not in retries or job in admitted]


def record_cleanup_reconcile_failure(
    store: PacGraphStore, graph_id: str, node_id: str, *, at_ms: int
) -> bool:
    """Count one failed reconcile against an open cleanup intent.

    A reconcile that raises before any cleanup outcome is written used to
    leave the intent untouched, so it stayed the oldest due retry and held a
    sweep slot every 30 s.  Counting the attempt and moving ``updated_at``
    gives it the same ``cleanup_retry_delay_ms`` backoff as any other
    failure.  State and reason are left alone, so a ``worker_in_use`` hold
    keeps its meaning; only a failure, never the in-use guard itself, counts.
    """

    db = store.write()
    try:
        changed = db.execute(
            "UPDATE workflow_worker_cleanup_intents SET attempts=attempts+1,"
            "updated_at=? WHERE graph_id=? AND actor_node=? "
            "AND state IN ('pending','attention')",
            (at_ms, graph_id, node_id),
        ).rowcount
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return bool(changed)


WORKER_IN_USE = "worker_in_use"

HOLD_PINNED_WORKERS = True
HOLD_INTERACTIVE_WORKERS = True


def worker_in_use(observation: RuntimeObservation) -> tuple[str, ...]:
    """The one policy point for "a person is using this workflow worker".

    R&D default pending Allen's ruling (2026-10-18): an agent pinned to an
    adapter, or online with a registered interactive session, is never
    stopped by terminal cleanup nor removed by PAC GC; cleanup parks it as
    ``worker_in_use`` attention and GC skips it as ``worker-in-use``.  The
    two switches above change that ruling in one place.  Returns the active
    signals; empty means the worker may be reclaimed.
    """

    signals: list[str] = []
    if HOLD_PINNED_WORKERS and observation.pinned:
        signals.append("pinned")
    if HOLD_INTERACTIVE_WORKERS and observation.interactive_online:
        signals.append("interactive_online")
    return tuple(signals)


def cleanup_stop_operation(operation_id: str, attempts: int) -> str:
    """Lifecycle operation id for one stop attempt of a cleanup intent.

    The lifecycle manager replays the terminal result of a reused operation
    id, so every retry after a failed or unsettled attempt needs a fresh id.
    The intent keeps its immutable base id; attempt ``n>0`` appends
    ``:retry:<n>``.
    """

    if attempts <= 0:
        return operation_id
    return f"{operation_id}{CLEANUP_RETRY_SEPARATOR}{attempts}"


def cleanup_operation_base(operation_id: str | None) -> str | None:
    """Return the cleanup intent id that one stop attempt id belongs to."""

    if operation_id is None:
        return None
    return operation_id.split(CLEANUP_RETRY_SEPARATOR, 1)[0]


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
