"""Compile a validated workflow into one atomically published PAC graph."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from hyprial.uri import canonical_agent_uri
from hyprial.contracts import ipc_errors

from .errors import PacError
from .journal import append_event
from .principal import parse_principal
from .store import PacGraphStore
from .workflow_schema import WorkflowSpec


def _artifact(directory: Path, filename: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / filename
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise PacError(
                "WORKFLOW_ARTIFACT_CHANGED", f"immutable artifact changed: {target}"
            )
        return target
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def read_specification(row: dict[str, Any] | Any) -> dict[str, Any]:
    path = Path(row["specification_ref"])
    if path.is_symlink():
        raise PacError(
            "WORKFLOW_ARTIFACT_CHANGED", "workflow specification is a symlink"
        )
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PacError("WORKFLOW_ARTIFACT_UNAVAILABLE", str(error)) from error
    if sha256(payload).hexdigest() != row["specification_digest"]:
        raise PacError(
            "WORKFLOW_ARTIFACT_CHANGED", "workflow specification digest mismatch"
        )
    return json.loads(payload)


def replay_graph(
    store: PacGraphStore,
    spec: WorkflowSpec,
    *,
    sender: str,
    operation_key: str,
    routine_name: str | None = None,
    task_key: str | None = None,
) -> str | None:
    existing = store.graph_by_operation_key(f"workflow:{operation_key}")
    if existing is None:
        return None
    row = store._db.execute(
        "SELECT * FROM workflow_graphs WHERE graph_id=?", (existing["graph_id"],)
    ).fetchone()
    if (
        existing["created_by"] != sender
        or row is None
        or row["specification_digest"]
        != sha256(spec.canonical.encode("utf-8")).hexdigest()
        or row["routine_name"] != routine_name
        or row["task_key"] != task_key
    ):
        raise PacError(
            "WORKFLOW_OPERATION_CONFLICT",
            "operation key belongs to a different request",
        )
    return str(existing["graph_id"])


def compile_workflow(
    store: PacGraphStore,
    spec: WorkflowSpec,
    *,
    sender: str,
    machine: str,
    local_owner: str,
    operation_key: str,
    at: int,
    routine_name: str | None = None,
    task_key: str | None = None,
) -> str:
    """Store all structure and activation together before any external effect."""
    parse_principal(sender)
    if (
        not isinstance(operation_key, str)
        or not operation_key
        or len(operation_key) > 400
        or any(ord(c) < 32 for c in operation_key)
    ):
        raise PacError(
            ipc_errors.INVALID_ARGUMENT,
            "operation key must be nonempty, single-line and at most 400 characters",
        )
    existing = replay_graph(
        store,
        spec,
        sender=sender,
        operation_key=operation_key,
        routine_name=routine_name,
        task_key=task_key,
    )
    if existing:
        return existing
    payload = spec.canonical.encode("utf-8")
    digest = sha256(payload).hexdigest()
    # Artifacts precede the DB publication. An interrupted write cannot leave
    # an activated graph pointing to an unfinished file.
    directory = store.path.parent / "workflow-artifacts" / digest
    reference = _artifact(directory, "specification.json", payload)
    launches = {}
    for node in spec.nodes:
        if node.worker and node.worker not in launches:
            launches[node.worker] = _artifact(
                directory,
                f"launch-{node.worker}.json",
                json.dumps(node.launch, sort_keys=True).encode(),
            )
    db = store.write()
    try:
        existing = replay_graph(
            store,
            spec,
            sender=sender,
            operation_key=operation_key,
            routine_name=routine_name,
            task_key=task_key,
        )
        if existing:
            db.commit()
            return existing
        graph_id = f"wf-{uuid4().hex}"
        db.execute(
            "INSERT INTO graphs(graph_id,name,version,created_by,created_at,operation_key,activated_at,activated_by) "
            "VALUES (?,?,1,?,?,?,?,?)",
            (graph_id, spec.name, sender, at, f"workflow:{operation_key}", at, sender),
        )
        db.execute(
            "INSERT INTO workflow_graphs(graph_id,specification_ref,specification_digest,on_failure,state,routine_name,task_key) "
            "VALUES (?,?,?,?,'running',?,?)",
            (graph_id, str(reference), digest, spec.on_failure, routine_name, task_key),
        )
        workers = {}
        for node in spec.nodes:
            if node.worker and node.worker not in workers:
                actor_node = f"_actor.{node.worker}"
                actor_name = (
                    f"{graph_id}-{sha256(node.worker.encode()).hexdigest()[:12]}"
                )
                workers[node.worker] = (
                    actor_node,
                    canonical_agent_uri(local_owner, machine, actor_name),
                )
                db.execute(
                    "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind,actor_name,launch_ref) "
                    "VALUES (?,?,?,?,'actor',?,?)",
                    (
                        graph_id,
                        actor_node,
                        sender,
                        f"workflow:{graph_id}#{actor_node}",
                        actor_name,
                        str(launches[node.worker])
                        + "#sha256="
                        + sha256(launches[node.worker].read_bytes()).hexdigest(),
                    ),
                )
        for node in spec.nodes:
            actor_node, owner = (
                workers[node.worker] if node.worker else (None, node.owner)
            )
            deadline = node.deadline_ms
            db.execute(
                "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind) VALUES (?,?,?,?,?)",
                (
                    graph_id,
                    node.id,
                    owner,
                    f"workflow:{graph_id}#{node.id}",
                    "end" if node.kind == "end" else "task",
                ),
            )
            db.execute(
                "INSERT INTO workflow_nodes"
                "(graph_id,node_id,actor_node,deadline_ms,timeout_ms) "
                "VALUES (?,?,?,?,?)",
                (graph_id, node.id, actor_node, deadline, node.timeout_ms),
            )
            if deadline is not None:
                db.execute(
                    "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind,deadline_ms,guarded_by_node_id) "
                    "VALUES (?,?,?,?,'clock',?,?)",
                    (
                        graph_id,
                        f"_deadline.{node.id}",
                        spec.escalate_to or sender,
                        f"workflow:{graph_id}#{node.id}:deadline",
                        deadline,
                        node.id,
                    ),
                )
            if actor_node:
                db.execute(
                    "INSERT INTO edges(graph_id,from_node,to_node,kind) VALUES (?,?,?,'forward')",
                    (graph_id, actor_node, node.id),
                )
        for source, target, kind in spec.edges:
            db.execute(
                "INSERT INTO edges(graph_id,from_node,to_node,kind) VALUES (?,?,?,?)",
                (graph_id, source, target, kind),
            )
        append_event(
            db,
            graph_id=graph_id,
            version=1,
            type="structure_changed",
            at=at,
            data={
                "workflow": spec.name,
                "specificationDigest": digest,
                "onFailure": spec.on_failure,
            },
        )
        append_event(
            db,
            graph_id=graph_id,
            version=1,
            type="graph_activated",
            at=at,
            data={"at": at, "by": sender},
        )
        db.commit()
        return graph_id
    except BaseException:
        db.rollback()
        raise


def managed_graph(store: PacGraphStore, graph_id: str) -> bool:
    return (
        store._db.execute(
            "SELECT 1 FROM workflow_graphs WHERE graph_id=?", (graph_id,)
        ).fetchone()
        is not None
    )


def input_token(
    store: PacGraphStore, graph_id: str, node_id: str, *, ignore_actor: bool = False
) -> str | None:
    """Bind completion to the transitive input facts, including set/reset ABA.

    A predecessor's old flag remains historical evidence after its own inputs
    change, but cannot authorize new downstream work until its owner reviews
    those new inputs. We never reset somebody else's flag automatically.
    """
    return input_token_from_connection(store._db, graph_id, node_id, ignore_actor=ignore_actor)


def input_token_from_connection(db, graph_id: str, node_id: str, *, ignore_actor: bool = False) -> str | None:
    """Use the caller's pinned snapshot for both projection and write fences."""
    cache: dict[tuple[str, bool], str | None] = {}

    def visit(current: str, skip_actor: bool = False) -> str | None:
        key = (current, skip_actor)
        if key in cache:
            return cache[key]
        cache[key] = None
        predecessors = db.execute(
            "SELECT n.* FROM edges e JOIN nodes n ON n.graph_id=e.graph_id AND n.node_id=e.from_node "
            "WHERE e.graph_id=? AND e.to_node=? AND e.kind='forward' ORDER BY n.node_id",
            (graph_id, current),
        ).fetchall()
        facts = []
        for row in predecessors:
            if row["kind"] == "actor":
                if not skip_actor and not row["flag"]:
                    return None
                # Runtime availability admits work; it is not a business
                # input whose later shutdown invalidates completed results.
                continue
            if not row["flag"]:
                return None
            projection = db.execute(
                "SELECT input_token FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                (graph_id, row["node_id"]),
            ).fetchone()
            ancestor = None
            if projection is not None:
                ancestor = visit(row["node_id"], True)
                if ancestor is None or projection["input_token"] != ancestor:
                    return None
            sequence = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM flag_events WHERE graph_id=? AND node_id=?",
                (graph_id, row["node_id"]),
            ).fetchone()[0]
            facts.append((row["node_id"], sequence, ancestor))
        own_reset = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM flag_events WHERE graph_id=? AND node_id=? AND action='reset'",
            (graph_id, current),
        ).fetchone()[0]
        token = sha256(
            json.dumps([graph_id, current, facts, own_reset]).encode()
        ).hexdigest()
        cache[key] = token
        return token

    return visit(node_id, ignore_actor)


def actor_ready(
    store: PacGraphStore, graph_id: str, actor_node: str, *, now_ms: int
) -> bool:
    """A shared worker starts when ANY assigned executable step is ready."""
    return any(
        (row["deadline_ms"] is None or now_ms <= row["deadline_ms"])
        and input_token(store, graph_id, row["node_id"], ignore_actor=True) is not None
        for row in store._db.execute(
            "SELECT node_id,deadline_ms FROM workflow_nodes WHERE graph_id=? AND actor_node=? AND state IN ('pending','requested')",
            (graph_id, actor_node),
        )
    )


def validate_completion(
    store: PacGraphStore,
    graph_id: str,
    node_id: str,
    request_id: str | None,
    *,
    at: int,
) -> None:
    """Called inside the flag transaction; direct CLI writes share the fence."""
    row = store._db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
        (graph_id, node_id),
    ).fetchone()
    if row is None:
        return
    if (
        not request_id
        or request_id != row["request_id"]
        or row["state"] != "requested"
        or row["input_token"] != input_token(store, graph_id, node_id)
    ):
        raise PacError(
            "WORKFLOW_REQUEST_STALE",
            "complete only the currently requested activation; inspect workflow status",
        )
    if row["deadline_ms"] is not None and at > row["deadline_ms"]:
        raise PacError("WORKFLOW_DEADLINE_EXPIRED", "the fixed deadline has passed")
