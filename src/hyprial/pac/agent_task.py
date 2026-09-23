"""PAC backend for the frozen ``agent.task`` daemon contract.

One task is one activated PAC graph.  Contract-only opaque JSON, target
bindings, typed activity, results, and the durable dispatch outbox are
projections linked to that graph in ``pac-graph.sqlite3``.  No workflow store,
registry, or executor is imported here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from time import time_ns
from typing import Protocol
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.contracts.agent_task import (
    AgentTaskActivity,
    AgentTaskResultProjection,
    AgentTaskRunProjection,
    AgentTaskStartInput,
    AgentTaskTargetProjection,
    NAMESPACE,
    OPERATIONS,
    PROTOCOL_VERSION,
    canonical_json,
    sha256_digest,
)
from hyprial.dispatch.identity import DISPATCH_SERVICE_ACTOR_NAME
from hyprial.uri import agent_uri_actor

from .journal import append_event
from .store import PacGraphStore, connect, default_database_path


class AgentTaskDeliveryIo(Protocol):
    """The existing daemon PAC delivery seam, typed structurally."""

    def deliver(
        self,
        *,
        effect_id: str,
        sender: str,
        target: str,
        conversation_id: str,
        text: str,
    ) -> object: ...


class PacAgentTaskError(RuntimeError):
    """One frozen daemon-wire refusal produced by the PAC adapter."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data: dict[str, object] = {
            "retryable": retryable,
            "details": dict(details or {}),
        }


def _wall_ms() -> int:
    return time_ns() // 1_000_000


def _fail(code: str, message: str, **details: object) -> None:
    raise PacAgentTaskError(code, message, details=details)


def _request_digest(request: AgentTaskStartInput) -> str:
    return sha256_digest(
        {
            "metadata": request.metadata,
            "payload": request.payload,
            "targets": [
                {
                    "targetRef": target.target_ref,
                    "target": target.target,
                    "role": target.role,
                    "delegates": list(target.delegates),
                }
                for target in request.targets
            ],
            "completion": request.completion,
        }
    )


def _dispatch_text(request: AgentTaskStartInput, assigned_target: str) -> str:
    return canonical_json(
        {
            "schemaVersion": "hyprial.agent-task.dispatch/v1",
            "externalRef": request.external_ref,
            "metadata": request.metadata,
            "payload": request.payload,
            "targets": [
                {
                    "targetRef": target.target_ref,
                    "target": target.target,
                    "role": target.role,
                    "delegates": list(target.delegates),
                }
                for target in request.targets
            ],
            "completion": {"kind": "result.submitted"},
            "assignedTarget": assigned_target,
        }
    )


@contextmanager
def _projection_database(path: Path):
    """Open a mechanically read-only PAC projection transaction."""

    if not path.is_file():
        _fail(
            ipc_errors.SERVICE_BINDING_NOT_FOUND,
            "the PAC agent.task service store is not provisioned",
        )
    db = connect(path, read_only=True)
    try:
        db.execute("BEGIN")
        yield db
    finally:
        db.rollback()
        db.close()


class PacAgentTaskProjection:
    """Read-only frozen response projections over the PAC graph database."""

    def __init__(self, database: Path, service_actor: str) -> None:
        self.database = Path(database)
        self.service_actor = service_actor

    def run(
        self, run_id: str, *, created: bool | None = None
    ) -> AgentTaskRunProjection | None:
        with _projection_database(self.database) as db:
            row = db.execute(
                """SELECT * FROM pac_agent_task_runs
                   WHERE graph_id=? AND service_actor=? AND namespace=?""",
                (run_id, self.service_actor, NAMESPACE),
            ).fetchone()
            if row is None:
                return None
            return AgentTaskRunProjection(
                run_id=run_id,
                external_ref=str(row["external_ref"]),
                state=str(row["state"]),
                targets=self._targets(db, run_id, include_result=False),
                last_event_id=row["last_event_id"],
                created=created,
            )

    def result(self, run_id: str) -> AgentTaskResultProjection | None:
        with _projection_database(self.database) as db:
            row = db.execute(
                """SELECT external_ref FROM pac_agent_task_runs
                   WHERE graph_id=? AND service_actor=? AND namespace=?""",
                (run_id, self.service_actor, NAMESPACE),
            ).fetchone()
            if row is None:
                return None
            return AgentTaskResultProjection(
                run_id=run_id,
                external_ref=str(row["external_ref"]),
                targets=self._targets(db, run_id, include_result=True),
            )

    @staticmethod
    def _targets(
        db: sqlite3.Connection, run_id: str, *, include_result: bool
    ) -> tuple[AgentTaskTargetProjection, ...]:
        output: list[AgentTaskTargetProjection] = []
        for row in db.execute(
            "SELECT * FROM pac_agent_task_targets "
            "WHERE graph_id=? ORDER BY ordinal",
            (run_id,),
        ):
            result = None
            if include_result:
                stored = db.execute(
                    """SELECT * FROM pac_agent_task_results
                       WHERE graph_id=? AND target_ref=?""",
                    (run_id, str(row["target_ref"])),
                ).fetchone()
                if stored is not None:
                    result = {
                        "resultRef": str(stored["result_ref"]),
                        "messageId": str(stored["message_id"]),
                        "payload": json.loads(str(stored["payload_json"])),
                        "artifacts": json.loads(str(stored["artifact_refs_json"])),
                        "submittedAt": str(stored["submitted_at"]),
                    }
            output.append(
                AgentTaskTargetProjection(
                    target_ref=str(row["target_ref"]),
                    target=str(row["target"]),
                    conversation_id=str(row["conversation_id"]),
                    attempts=int(row["attempts"]),
                    state=str(row["state"]),
                    result_ref=row["result_ref"],
                    result=result,
                )
            )
        return tuple(output)


class PacAgentTaskService:
    """Six-operation facade whose sole durable authority is a PAC graph."""

    def __init__(
        self,
        *,
        state_dir: Path,
        service_actor: str,
        delivery_io: AgentTaskDeliveryIo | None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if agent_uri_actor(service_actor) is None:
            raise PacAgentTaskError(
                ipc_errors.SERVICE_BINDING_NOT_FOUND,
                "the daemon-managed mfu-coordinator service actor is not registered",
            )
        self.database = default_database_path(state_dir)
        # Every PAC principal this service writes (creator, activator, node
        # owner, flag setter, closer) is this full URI: schema 8 authorizes
        # exact principal URIs, and a short name would put the graph in the
        # mixed-legacy era the migration exists to leave behind.
        self.service_actor = service_actor
        self.delivery_io = delivery_io
        self.clock_ms = clock_ms or _wall_ms
        # Provision/migrate before a read-only projection is allowed to open.
        PacGraphStore(self.database).close()
        self.projection = PacAgentTaskProjection(self.database, service_actor)

    def agent_task_capabilities(self) -> dict[str, object]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "namespace": NAMESPACE,
            "operations": list(OPERATIONS),
            "features": {
                "externalRefIdempotency": True,
                "typedActivity": True,
                "explicitFinalResult": True,
                "durableResult": True,
                "multiTarget": True,
            },
            "serviceIdentity": {
                "actorName": DISPATCH_SERVICE_ACTOR_NAME,
                "actorUri": self.service_actor,
                "binding": "daemon-managed",
            },
        }

    def agent_task_start(
        self, *, request: AgentTaskStartInput, caller: str
    ) -> dict[str, object]:
        store = PacGraphStore(self.database)
        created = False
        run_id: str
        try:
            db = store.write()
            try:
                existing = db.execute(
                    """SELECT graph_id, request_digest FROM pac_agent_task_runs
                       WHERE service_actor=? AND namespace=? AND external_ref=?""",
                    (self.service_actor, NAMESPACE, request.external_ref),
                ).fetchone()
                if existing is not None:
                    run_id = str(existing["graph_id"])
                    if str(existing["request_digest"]) != request.request_digest:
                        _fail(
                            ipc_errors.EXTERNAL_REF_CONFLICT,
                            "externalRef already reserved with different input: "
                            f"{request.external_ref}",
                            runId=run_id,
                        )
                    if request.request_digest != _request_digest(request):
                        _fail(
                            ipc_errors.INVALID_REQUEST,
                            "requestDigest does not match the canonical request",
                        )
                    db.rollback()
                else:
                    if request.request_digest != _request_digest(request):
                        _fail(
                            ipc_errors.INVALID_REQUEST,
                            "requestDigest does not match the canonical request",
                        )
                    run_id = f"run-{uuid4().hex[:12]}"
                    self._create_graph(db, run_id, request, caller)
                    db.commit()
                    created = True
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
        finally:
            store.close()

        self._deliver_pending(run_id)
        projection = self.projection.run(run_id, created=created)
        if projection is None:  # pragma: no cover - guarded by one database
            _fail(
                ipc_errors.PROTOCOL_ERROR,
                f"agent.task externalRef points to missing PAC graph: {run_id}",
            )
        return projection.to_payload()

    def _create_graph(
        self,
        db: sqlite3.Connection,
        run_id: str,
        request: AgentTaskStartInput,
        caller: str,
    ) -> None:
        at = self.clock_ms()
        version = 1 + len(request.targets)
        db.execute(
            """INSERT INTO graphs
               (graph_id,name,version,created_by,created_at,activated_at,activated_by)
               VALUES (?,?,?,?,?,?,?)""",
            (
                run_id,
                f"agent-task-{request.external_ref}",
                version,
                self.service_actor,
                at,
                at,
                self.service_actor,
            ),
        )
        append_event(
            db,
            graph_id=run_id,
            version=1,
            type="structure_changed",
            at=at,
            data={"operation": "create", "resync": True},
        )
        db.execute(
            """INSERT INTO pac_agent_task_runs
               (graph_id,service_actor,namespace,external_ref,request_digest,
                caller,metadata_json,payload_json,completion_json,state,created_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,'reserved',?)""",
            (
                run_id,
                self.service_actor,
                NAMESPACE,
                request.external_ref,
                request.request_digest,
                caller,
                canonical_json(request.metadata),
                canonical_json(request.payload),
                canonical_json(request.completion),
                at,
            ),
        )
        for ordinal, target in enumerate(request.targets):
            node_id = f"target-{ordinal + 1}"
            conversation_id = f"at-{run_id}-{target.target_ref}"
            db.execute(
                """INSERT INTO nodes
                   (graph_id,node_id,owner,brief_ref,kind)
                   VALUES (?,?,?,?,'task')""",
                (
                    run_id,
                    node_id,
                    self.service_actor,
                    f"agent-task:{request.external_ref}#{target.target_ref}",
                ),
            )
            append_event(
                db,
                graph_id=run_id,
                version=ordinal + 2,
                type="structure_changed",
                at=at,
                data={"operation": "add_node", "resync": True},
            )
            db.execute(
                """INSERT INTO pac_agent_task_targets
                   (graph_id,ordinal,target_ref,node_id,target,role,
                    delegates_json,conversation_id,attempts,state)
                   VALUES (?,?,?,?,?,?,?,?,1,'dispatching')""",
                (
                    run_id,
                    ordinal,
                    target.target_ref,
                    node_id,
                    target.target,
                    target.role,
                    canonical_json(target.delegates),
                    conversation_id,
                ),
            )
            db.execute(
                """INSERT INTO pac_agent_task_dispatches
                   (graph_id,target_ref,effect_id,text)
                   VALUES (?,?,?,?)""",
                (
                    run_id,
                    target.target_ref,
                    f"pac-agent-task:{run_id}:{target.target_ref}",
                    _dispatch_text(request, target.target),
                ),
            )
        append_event(
            db,
            graph_id=run_id,
            version=version,
            type="graph_activated",
            at=at,
            data={"at": at, "by": self.service_actor},
        )

    def _deliver_pending(self, run_id: str) -> None:
        store = PacGraphStore(self.database)
        try:
            rows = store._db.execute(  # noqa: SLF001 - same PAC store boundary
                """SELECT d.*,t.target,t.conversation_id
                   FROM pac_agent_task_dispatches d
                   JOIN pac_agent_task_targets t
                     ON t.graph_id=d.graph_id AND t.target_ref=d.target_ref
                   WHERE d.graph_id=? AND d.message_id IS NULL
                   ORDER BY t.ordinal""",
                (run_id,),
            ).fetchall()
        finally:
            store.close()
        if not rows:
            return
        if self.delivery_io is None:
            raise PacAgentTaskError(
                ipc_errors.TRANSPORT_ERROR,
                "PAC agent.task delivery is unavailable",
                retryable=True,
                details={"runId": run_id},
            )
        for row in rows:
            try:
                delivered = self.delivery_io.deliver(
                    effect_id=str(row["effect_id"]),
                    sender=self.service_actor,
                    target=str(row["target"]),
                    conversation_id=str(row["conversation_id"]),
                    text=str(row["text"]),
                )
                message_id = getattr(delivered, "message_id", None)
                if not isinstance(message_id, str) or not message_id:
                    raise RuntimeError("delivery returned no message id")
            except Exception as error:
                raise PacAgentTaskError(
                    ipc_errors.TRANSPORT_ERROR,
                    f"PAC agent.task delivery failed: {error}",
                    retryable=True,
                    details={"runId": run_id, "effectId": str(row["effect_id"])},
                ) from error
            store = PacGraphStore(self.database)
            try:
                db = store.write()
                try:
                    db.execute(
                        """UPDATE pac_agent_task_dispatches
                           SET message_id=?, delivered_at_ms=?
                           WHERE effect_id=? AND message_id IS NULL""",
                        (message_id, self.clock_ms(), str(row["effect_id"])),
                    )
                    db.execute(
                        """UPDATE pac_agent_task_targets SET state='running'
                           WHERE graph_id=? AND target_ref=? AND state='dispatching'""",
                        (run_id, str(row["target_ref"])),
                    )
                    pending = db.execute(
                        """SELECT 1 FROM pac_agent_task_dispatches
                           WHERE graph_id=? AND message_id IS NULL LIMIT 1""",
                        (run_id,),
                    ).fetchone()
                    if pending is None:
                        db.execute(
                            """UPDATE pac_agent_task_runs SET state='running'
                               WHERE graph_id=? AND state='reserved'""",
                            (run_id,),
                        )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            finally:
                store.close()

    def agent_task_status(self, *, run_id: str) -> dict[str, object]:
        projection = self.projection.run(run_id)
        if projection is None:
            _fail(
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {run_id}",
            )
        return projection.to_payload()

    def agent_task_result(
        self, *, run_id: str, target_ref: str | None
    ) -> dict[str, object]:
        projection = self.projection.result(run_id)
        if projection is None:
            _fail(
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {run_id}",
            )
        if target_ref is not None:
            target = next(
                (item for item in projection.targets if item.target_ref == target_ref),
                None,
            )
            if target is None:
                _fail(
                    ipc_errors.TARGET_NOT_FOUND,
                    f"no targetRef {target_ref} in run {run_id}",
                )
            if target.result is None:
                _fail(
                    ipc_errors.RESULT_NOT_READY,
                    f"target {target_ref} has no submitted result",
                )
        return projection.to_payload()

    def agent_task_cancel(
        self, *, run_id: str, caller: str, reason: str | None
    ) -> dict[str, object]:
        del caller  # authorization is the daemon's caller-to-service binding.
        store = PacGraphStore(self.database)
        try:
            db = store.write()
            try:
                run = self._run_row(db, run_id)
                if run is None:
                    _fail(
                        ipc_errors.RUN_NOT_FOUND,
                        f"no run in this service/namespace scope: {run_id}",
                    )
                state = str(run["state"])
                if state == "cancelled":
                    db.rollback()
                elif state in {"completed", "failed"}:
                    _fail(
                        ipc_errors.RUN_NOT_CANCELLABLE,
                        f"run {run_id} is already {state}",
                    )
                else:
                    graph = db.execute(
                        "SELECT * FROM graphs WHERE graph_id=?", (run_id,)
                    ).fetchone()
                    if graph is None or graph["closed_at"] is not None:
                        _fail(
                            ipc_errors.PROTOCOL_ERROR,
                            f"active agent.task PAC graph is missing or closed: {run_id}",
                        )
                    at = self.clock_ms()
                    db.execute(
                        """UPDATE pac_agent_task_targets SET state='cancelled'
                           WHERE graph_id=? AND state!='completed'""",
                        (run_id,),
                    )
                    db.execute(
                        """UPDATE pac_agent_task_runs
                           SET state='cancelled',cancel_reason=?,finished_at_ms=?
                           WHERE graph_id=?""",
                        (reason, at, run_id),
                    )
                    db.execute(
                        """UPDATE graphs SET closed_at=?,closed_by=?
                           WHERE graph_id=? AND closed_at IS NULL""",
                        (at, self.service_actor, run_id),
                    )
                    append_event(
                        db,
                        graph_id=run_id,
                        version=int(graph["version"]),
                        type="graph_closed",
                        at=at,
                        data={"at": at, "by": self.service_actor},
                    )
                    db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
        finally:
            store.close()
        projection = self.projection.run(run_id)
        if projection is None:  # pragma: no cover - guarded by transaction
            _fail(ipc_errors.PROTOCOL_ERROR, f"cancelled PAC graph vanished: {run_id}")
        return projection.to_payload()

    def agent_task_observe(
        self,
        *,
        activity: AgentTaskActivity,
        submitter: str,
        message_id: str | None = None,
    ) -> dict[str, object]:
        store = PacGraphStore(self.database)
        created = False
        try:
            db = store.write()
            try:
                run = self._run_row(db, activity.run_id)
                if run is None:
                    _fail(
                        ipc_errors.RUN_NOT_FOUND,
                        "no run in this service/namespace scope: "
                        f"{activity.run_id}",
                    )
                target = db.execute(
                    """SELECT * FROM pac_agent_task_targets
                       WHERE graph_id=? AND target_ref=?""",
                    (activity.run_id, activity.target_ref),
                ).fetchone()
                if target is None:
                    _fail(
                        ipc_errors.TARGET_NOT_FOUND,
                        f"no targetRef {activity.target_ref} in run {activity.run_id}",
                    )
                delegates = tuple(json.loads(str(target["delegates_json"])))
                if submitter != str(target["target"]) and submitter not in delegates:
                    _fail(
                        ipc_errors.CALLER_NOT_AUTHORIZED,
                        "submitter is neither the assigned target nor a scoped delegate",
                    )
                if activity.conversation_id != str(target["conversation_id"]):
                    _fail(
                        ipc_errors.INVALID_REQUEST,
                        "activity conversationId does not match target",
                    )

                result_ref = (
                    str(activity.payload["resultRef"])
                    if activity.kind == "result.submitted"
                    else None
                )
                supplied_result_digest = (
                    str(activity.payload["resultDigest"])
                    if activity.kind == "result.submitted"
                    else None
                )
                computed_result_digest = (
                    sha256_digest(
                        {
                            "result": activity.payload["result"],
                            "artifactRefs": activity.payload["artifactRefs"],
                        }
                    )
                    if activity.kind == "result.submitted"
                    else None
                )
                existing_result = db.execute(
                    """SELECT * FROM pac_agent_task_results
                       WHERE graph_id=? AND target_ref=?""",
                    (activity.run_id, activity.target_ref),
                ).fetchone()
                if existing_result is not None:
                    if (
                        result_ref == str(existing_result["result_ref"])
                        and computed_result_digest
                        == str(existing_result["result_digest"])
                        and supplied_result_digest == computed_result_digest
                    ):
                        db.rollback()
                        return self._observed(activity.event_id, created=False)
                    _fail(
                        ipc_errors.RESULT_REF_CONFLICT,
                        "target already has a different result",
                    )

                existing_event = db.execute(
                    """SELECT event_digest FROM pac_agent_task_events
                       WHERE event_id=?""",
                    (activity.event_id,),
                ).fetchone()
                if existing_event is not None:
                    if str(existing_event["event_digest"]) == activity.event_digest:
                        db.rollback()
                        return self._observed(activity.event_id, created=False)
                    _fail(
                        ipc_errors.INVALID_REQUEST,
                        "eventId is already used by different activity",
                    )
                if str(run["state"]) == "cancelled":
                    _fail(
                        ipc_errors.INVALID_REQUEST,
                        "cancelled runs do not accept new activity",
                    )
                graph = db.execute(
                    "SELECT * FROM graphs WHERE graph_id=?", (activity.run_id,)
                ).fetchone()
                if graph is None or graph["closed_at"] is not None:
                    _fail(
                        ipc_errors.PROTOCOL_ERROR,
                        f"active agent.task PAC graph is missing or closed: {activity.run_id}",
                    )
                if (
                    activity.kind == "result.submitted"
                    and supplied_result_digest != computed_result_digest
                ):
                    _fail(
                        ipc_errors.INVALID_REQUEST,
                        "resultDigest does not match the canonical result",
                    )
                persisted_message_id = message_id or activity.event_id
                db.execute(
                    """INSERT INTO pac_agent_task_events
                       (event_id,event_digest,graph_id,target_ref,conversation_id,
                        kind,submitter,at,payload_json,message_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        activity.event_id,
                        activity.event_digest,
                        activity.run_id,
                        activity.target_ref,
                        activity.conversation_id,
                        activity.kind,
                        submitter,
                        activity.at,
                        canonical_json(activity.payload),
                        persisted_message_id,
                    ),
                )
                if activity.kind == "result.submitted":
                    assert result_ref is not None
                    assert computed_result_digest is not None
                    flag_event_id = f"agent-task-flag:{activity.event_id}"
                    at = self.clock_ms()
                    db.execute(
                        """INSERT INTO flag_events
                           (event_id,graph_id,version,node_id,action,actor,at,reason_ref)
                           VALUES (?,?,?,?, 'set', ?,?,?)""",
                        (
                            flag_event_id,
                            activity.run_id,
                            int(graph["version"]),
                            str(target["node_id"]),
                            self.service_actor,
                            at,
                            result_ref,
                        ),
                    )
                    db.execute(
                        """UPDATE nodes
                           SET flag=1,flag_set_by=?,flag_set_at=?,flag_reason_ref=?
                           WHERE graph_id=? AND node_id=? AND flag=0""",
                        (
                            self.service_actor,
                            at,
                            result_ref,
                            activity.run_id,
                            str(target["node_id"]),
                        ),
                    )
                    if db.execute("SELECT changes()").fetchone()[0] != 1:
                        _fail(
                            ipc_errors.PROTOCOL_ERROR,
                            "typed result target PAC flag is already set",
                        )
                    append_event(
                        db,
                        graph_id=activity.run_id,
                        version=int(graph["version"]),
                        type="flag_set",
                        at=at,
                        event_id=flag_event_id,
                        data={
                            "nodeId": str(target["node_id"]),
                            "action": "set",
                            "actor": self.service_actor,
                            "reasonRef": result_ref,
                            "typedEventId": activity.event_id,
                        },
                    )
                    db.execute(
                        """INSERT INTO pac_agent_task_results
                           (graph_id,target_ref,result_ref,result_digest,message_id,
                            payload_json,artifact_refs_json,submitted_at,
                            activity_event_id,flag_event_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            activity.run_id,
                            activity.target_ref,
                            result_ref,
                            computed_result_digest,
                            persisted_message_id,
                            canonical_json(activity.payload["result"]),
                            canonical_json(activity.payload["artifactRefs"]),
                            activity.at,
                            activity.event_id,
                            flag_event_id,
                        ),
                    )
                    db.execute(
                        """UPDATE pac_agent_task_targets
                           SET state='completed',result_ref=?
                           WHERE graph_id=? AND target_ref=?""",
                        (result_ref, activity.run_id, activity.target_ref),
                    )
                else:
                    state = (
                        "waiting"
                        if activity.kind in {"question", "blocked"}
                        else "running"
                    )
                    db.execute(
                        """UPDATE pac_agent_task_targets SET state=?
                           WHERE graph_id=? AND target_ref=? AND state!='completed'""",
                        (state, activity.run_id, activity.target_ref),
                    )
                self._sync_run_state(
                    db,
                    activity.run_id,
                    activity.event_id,
                    graph_version=int(graph["version"]),
                )
                db.commit()
                created = True
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
        finally:
            store.close()
        return self._observed(activity.event_id, created=created)

    def _sync_run_state(
        self,
        db: sqlite3.Connection,
        run_id: str,
        last_event_id: str,
        *,
        graph_version: int,
    ) -> None:
        states = {
            str(row["state"])
            for row in db.execute(
                "SELECT state FROM pac_agent_task_targets WHERE graph_id=?",
                (run_id,),
            )
        }
        state = (
            "completed"
            if states == {"completed"}
            else "failed"
            if states and states <= {"completed", "failed"} and "failed" in states
            else "waiting"
            if "waiting" in states
            else "running"
        )
        finished_at: int | None = None
        if state == "completed":
            finished_at = self.clock_ms()
            changed = db.execute(
                """UPDATE graphs SET closed_at=?,closed_by=?
                   WHERE graph_id=? AND closed_at IS NULL""",
                (finished_at, self.service_actor, run_id),
            )
            if changed.rowcount == 1:
                append_event(
                    db,
                    graph_id=run_id,
                    version=graph_version,
                    type="graph_closed",
                    at=finished_at,
                    data={"at": finished_at, "by": self.service_actor},
                )
        db.execute(
            """UPDATE pac_agent_task_runs
               SET state=?,last_event_id=?,
                   finished_at_ms=COALESCE(?,finished_at_ms)
               WHERE graph_id=? AND state!='cancelled'""",
            (state, last_event_id, finished_at, run_id),
        )

    def _run_row(
        self, db: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        return db.execute(
            """SELECT * FROM pac_agent_task_runs
               WHERE graph_id=? AND service_actor=? AND namespace=?""",
            (run_id, self.service_actor, NAMESPACE),
        ).fetchone()

    @staticmethod
    def _observed(event_id: str, *, created: bool) -> dict[str, object]:
        return {"accepted": True, "created": created, "eventId": event_id}


__all__ = [
    "AgentTaskDeliveryIo",
    "PacAgentTaskError",
    "PacAgentTaskProjection",
    "PacAgentTaskService",
]
