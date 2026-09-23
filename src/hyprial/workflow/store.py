"""SQLite persistence for PAC workflow runs (design-pac-workflow §4.4).

Recovery philosophy: a run resumes from **persisted state**, never by
replaying start order (the Claude Code mid-fan-out replay cost the design
explicitly avoids).  Every target transition is upserted as it happens, so a
daemon restart loses at most the in-flight tick — deadlines are wall-clock
and honestly keep burning while the daemon is down.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.assign import WORKFLOW_TERMINAL_STATES
from hyprial.assign_store import AssignStoreMixin
from hyprial.contracts.agent_task import (
    AgentTaskActivity,
    AgentTaskResultProjection,
    AgentTaskRunProjection,
    AgentTaskStartInput,
    AgentTaskTargetProjection,
    NAMESPACE as AGENT_TASK_NAMESPACE,
    canonical_json,
)

from .executor import RunState, TargetRuntime, TargetState, WorkflowRun
from .schema import WorkflowSchemaError, load_workflow_text


@dataclass(frozen=True, slots=True)
class PersistedRun:
    run: WorkflowRun
    yaml_text: str
    version: int = 1


@dataclass(frozen=True, slots=True)
class RunSchemaRejection:
    """A stored run whose spec no longer validates under the current schema.

    Bare-name ``report_to`` / ``escalate_to`` addresses were writable before
    2026-09-14; after the load-time validation they must not silently resume
    (the run would keep escalating into unreadable stores) nor crash recovery.
    The run stays stored and NOT resumed, and every rejection is surfaced so
    an operator can fix or cancel it.
    """

    run_id: str
    state: str
    error: str


@dataclass(frozen=True, slots=True)
class PersistedEffectBatch:
    correlation_id: str
    final_kind: str
    final_payload: dict[str, Any]
    effects: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PersistedExternalRef:
    external_ref: str
    request_digest: str
    run_id: str


class WorkflowExternalRefConflict(RuntimeError):
    """The same external reference was reused for a different request."""

    def __init__(self, external_ref: str, existing_run_id: str) -> None:
        super().__init__(f"externalRef conflict: {external_ref}")
        self.external_ref = external_ref
        self.existing_run_id = existing_run_id


@dataclass(frozen=True, slots=True)
class AgentTaskStartWrite:
    service_actor: str
    caller: str
    request: AgentTaskStartInput


@dataclass(frozen=True, slots=True)
class AgentTaskActivityWrite:
    activity: AgentTaskActivity
    submitter: str
    message_id: str


@dataclass(frozen=True, slots=True)
class PersistedAgentTaskRef:
    service_actor: str
    namespace: str
    external_ref: str
    request_digest: str
    run_id: str


@dataclass(frozen=True, slots=True)
class PersistedAgentTaskTarget:
    target_ref: str
    target: str
    conversation_id: str
    delegates: tuple[str, ...]
    state: str
    result_ref: str | None


@dataclass(frozen=True, slots=True)
class PersistedAgentTaskResult:
    result_ref: str
    result_digest: str
    message_id: str


class AgentTaskExternalRefConflict(RuntimeError):
    def __init__(self, external_ref: str, existing_run_id: str) -> None:
        super().__init__(f"agent.task externalRef conflict: {external_ref}")
        self.external_ref = external_ref
        self.existing_run_id = existing_run_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    yaml_text TEXT NOT NULL,
    sender TEXT NOT NULL,
    nonce TEXT NOT NULL,
    state TEXT NOT NULL,
    report_text TEXT,
    dispatch_warnings TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS targets (
    run_id TEXT NOT NULL,
    target TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    deadline_ms INTEGER,
    next_attempt_ms INTEGER,
    last_message_id TEXT,
    reply_excerpt TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    extend_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, target)
);
CREATE INDEX IF NOT EXISTS targets_run ON targets(run_id);

CREATE TABLE IF NOT EXISTS workflow_effect_batches (
    correlation_id TEXT PRIMARY KEY,
    final_kind TEXT NOT NULL,
    final_payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_effects (
    effect_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (correlation_id) REFERENCES workflow_effect_batches(correlation_id)
);
CREATE INDEX IF NOT EXISTS workflow_effects_batch
ON workflow_effects(correlation_id);
CREATE TABLE IF NOT EXISTS workflow_node_deliveries (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    recipient TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_node_deliveries_target
ON workflow_node_deliveries(run_id, target_ref);
CREATE TABLE IF NOT EXISTS workflow_external_refs (
    external_ref TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_external_refs_run
ON workflow_external_refs(run_id);
CREATE TABLE IF NOT EXISTS agent_task_runs (
    run_id TEXT PRIMARY KEY,
    service_actor TEXT NOT NULL,
    namespace TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    caller TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    completion_json TEXT NOT NULL,
    state TEXT NOT NULL,
    last_event_id TEXT,
    cancel_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    UNIQUE(service_actor, namespace, external_ref)
);
CREATE TABLE IF NOT EXISTS agent_task_targets (
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    target TEXT NOT NULL,
    role TEXT NOT NULL,
    delegates_json TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    result_ref TEXT,
    PRIMARY KEY(run_id, target_ref),
    UNIQUE(run_id, target),
    UNIQUE(run_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS agent_task_events (
    event_id TEXT PRIMARY KEY,
    event_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    submitter TEXT NOT NULL,
    at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    message_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_task_events_run ON agent_task_events(run_id);
CREATE TABLE IF NOT EXISTS agent_task_results (
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    message_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifact_refs_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(run_id, target_ref),
    UNIQUE(run_id, target_ref, result_ref)
);
"""


class WorkflowStore(AssignStoreMixin):
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3 connections do not serialize concurrent Python callers when
        # check_same_thread=False. This lock protects the connection object
        # only; it is not a workflow/business-state ownership lock (the actor
        # remains the sole writer and transition authority).
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._db:
            self._db.executescript(_SCHEMA)
            self._migrate()
        self._install_assign_schema()

    def _migrate(self) -> None:
        """Forward-only additions; old workflow databases remain readable."""
        run_columns = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(runs)")
        }
        target_columns = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(targets)")
        }
        if "dispatch_warnings" not in run_columns:
            self._db.execute(
                "ALTER TABLE runs ADD COLUMN dispatch_warnings TEXT NOT NULL DEFAULT '[]'"
            )
        if "version" not in run_columns:
            self._db.execute(
                "ALTER TABLE runs ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
        if "delivered" not in target_columns:
            self._db.execute(
                "ALTER TABLE targets ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0"
            )
        if "extend_count" not in target_columns:
            self._db.execute(
                "ALTER TABLE targets ADD COLUMN extend_count INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self._db_lock:
            self._db.close()

    # ── writes ───────────────────────────────────────────────────────────

    def record_node_delivery(self, *, run_id: str, target_ref: str,
                             message_id: str, recipient: str, recorded_at_ms: int) -> None:
        # Registry writes this before retiring the effect. Replay is idempotent;
        # a mutable alias is never resolved again when reading historical work.
        with self._db_lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO workflow_node_deliveries VALUES (?, ?, ?, ?, ?)",
                (message_id, run_id, target_ref, recipient, recorded_at_ms),
            )

    def node_deliveries(self, run_id: str, target_ref: str) -> list[dict[str, Any]]:
        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM workflow_node_deliveries WHERE run_id = ? AND target_ref = ? "
                "ORDER BY recorded_at_ms, rowid", (run_id, target_ref),
            ).fetchall()
        return [{"deliveryId": row["message_id"], "actor": row["recipient"],
                 "recordedAtMs": row["recorded_at_ms"]} for row in rows]

    def save_run(
        self,
        run: WorkflowRun,
        *,
        yaml_text: str,
        created_at_ms: int,
        finished_at_ms: int | None = None,
        version: int = 1,
        effect_batch: PersistedEffectBatch | None = None,
        completed_effect_id: str | None = None,
        external_ref: str | None = None,
        request_digest: str | None = None,
        agent_task_start: AgentTaskStartWrite | None = None,
        agent_task_activity: AgentTaskActivityWrite | None = None,
        agent_task_cancel: bool = False,
        agent_task_cancel_reason: str | None = None,
    ) -> None:
        """Upsert the run row and every target row (called after each transition batch).

        ``created_at_ms`` is written only on first insert (the ON CONFLICT
        clause deliberately leaves it alone); ``finished_at_ms`` is stamped
        once the run reaches a terminal state and never un-stamped.
        """
        with self._db_lock, self._db:
            self._db.execute(
                """INSERT INTO runs (run_id, name, yaml_text, sender, nonce, state,
                                     report_text, created_at_ms, finished_at_ms, version, dispatch_warnings)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       state = excluded.state,
                       report_text = excluded.report_text,
                       dispatch_warnings = excluded.dispatch_warnings,
                       version = excluded.version,
                       finished_at_ms = COALESCE(excluded.finished_at_ms, runs.finished_at_ms)""",
                (
                    run.run_id,
                    run.spec.name,
                    yaml_text,
                    run.sender,
                    run.nonce,
                    str(run.state),
                    run.report_text,
                    created_at_ms,
                    finished_at_ms,
                    version,
                    json.dumps(run.dispatch_warnings, ensure_ascii=False),
                ),
            )
            # Release inside the same transaction that writes the terminal
            # state: one point covering every caller (five sites persist a
            # terminal transition, and hooking each would be five chances to
            # miss one -- and a missed release keeps an actor looking busy
            # forever).  Doing it here rather than before the transaction also
            # keeps save_run a single commit; an extra commit ahead of it
            # changes when this lock is released, which is observable to
            # anything racing the pump.
            if str(run.state) in WORKFLOW_TERMINAL_STATES:
                self._release_assigns_in_transaction(
                    assign_kind="workflow",
                    assign_ref=run.run_id,
                    released_at_ms=(
                        finished_at_ms
                        if finished_at_ms is not None
                        else created_at_ms
                    ),
                )
            for target in run.targets:
                self._db.execute(
                    """INSERT INTO targets (run_id, target, conversation_id, state, attempts,
                                            deadline_ms, next_attempt_ms, last_message_id, reply_excerpt,
                                            delivered, extend_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(run_id, target) DO UPDATE SET
                           state = excluded.state,
                           attempts = excluded.attempts,
                           deadline_ms = excluded.deadline_ms,
                           next_attempt_ms = excluded.next_attempt_ms,
                           last_message_id = excluded.last_message_id,
                           reply_excerpt = excluded.reply_excerpt,
                           delivered = excluded.delivered,
                           extend_count = excluded.extend_count""",
                    (
                        run.run_id,
                        target.name,
                        target.conversation_id,
                        str(target.state),
                        target.attempts,
                        target.deadline_ms,
                        target.next_attempt_ms,
                        target.last_message_id,
                        target.reply_excerpt,
                        int(target.delivered),
                        target.extend_count,
                    ),
                )
            if completed_effect_id is not None:
                self._db.execute(
                    "DELETE FROM workflow_effects WHERE effect_id = ?",
                    (completed_effect_id,),
                )
            if effect_batch is not None:
                self._save_effect_batch(effect_batch)
            if external_ref is not None:
                if request_digest is None:
                    raise ValueError("request_digest is required with external_ref")
                existing = self._db.execute(
                    "SELECT request_digest, run_id FROM workflow_external_refs "
                    "WHERE external_ref = ?",
                    (external_ref,),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_digest"]) != request_digest:
                        raise WorkflowExternalRefConflict(
                            external_ref, str(existing["run_id"])
                        )
                    if str(existing["run_id"]) != run.run_id:
                        raise WorkflowExternalRefConflict(
                            external_ref, str(existing["run_id"])
                        )
                else:
                    try:
                        self._db.execute(
                            "INSERT INTO workflow_external_refs "
                            "(external_ref, request_digest, run_id) VALUES (?, ?, ?)",
                            (external_ref, request_digest, run.run_id),
                        )
                    except sqlite3.IntegrityError as error:
                        raced = self._db.execute(
                            "SELECT run_id FROM workflow_external_refs "
                            "WHERE external_ref = ?",
                            (external_ref,),
                        ).fetchone()
                        raise WorkflowExternalRefConflict(
                            external_ref,
                            "unknown" if raced is None else str(raced["run_id"]),
                        ) from error
            if agent_task_start is not None:
                self._save_agent_task_start(run, created_at_ms, agent_task_start)
            if agent_task_activity is not None:
                self._save_agent_task_activity(
                    run,
                    finished_at_ms=finished_at_ms,
                    write=agent_task_activity,
                )
            if agent_task_cancel:
                self._save_agent_task_cancel(
                    run,
                    reason=agent_task_cancel_reason,
                    finished_at_ms=finished_at_ms,
                )
            self._sync_agent_task_runtime_locked(
                run, finished_at_ms=finished_at_ms
            )

    # ── reads ────────────────────────────────────────────────────────────

    def _save_agent_task_start(
        self,
        run: WorkflowRun,
        created_at_ms: int,
        write: AgentTaskStartWrite,
    ) -> None:
        request = write.request
        existing = self._db.execute(
            """SELECT run_id, request_digest FROM agent_task_runs
               WHERE service_actor = ? AND namespace = ? AND external_ref = ?""",
            (write.service_actor, AGENT_TASK_NAMESPACE, request.external_ref),
        ).fetchone()
        if existing is not None:
            raise AgentTaskExternalRefConflict(
                request.external_ref, str(existing["run_id"])
            )
        self._db.execute(
            """INSERT INTO agent_task_runs
               (run_id, service_actor, namespace, external_ref, request_digest,
                caller, metadata_json, payload_json, completion_json, state,
                created_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
            (
                run.run_id,
                write.service_actor,
                AGENT_TASK_NAMESPACE,
                request.external_ref,
                request.request_digest,
                write.caller,
                canonical_json(request.metadata),
                canonical_json(request.payload),
                canonical_json(request.completion),
                created_at_ms,
            ),
        )
        by_target = {target.name: target for target in run.targets}
        for target in request.targets:
            runtime = by_target[target.target]
            self._db.execute(
                """INSERT INTO agent_task_targets
                   (run_id, target_ref, target, role, delegates_json,
                    conversation_id, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    target.target_ref,
                    target.target,
                    target.role,
                    canonical_json(target.delegates),
                    runtime.conversation_id,
                    _agent_task_target_state(runtime.state),
                ),
            )

    def _save_agent_task_activity(
        self,
        run: WorkflowRun,
        *,
        finished_at_ms: int | None,
        write: AgentTaskActivityWrite,
    ) -> None:
        activity = write.activity
        self._db.execute(
            """INSERT INTO agent_task_events
               (event_id, event_digest, run_id, target_ref, conversation_id,
                kind, submitter, at, payload_json, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                activity.event_id,
                activity.event_digest,
                activity.run_id,
                activity.target_ref,
                activity.conversation_id,
                activity.kind,
                write.submitter,
                activity.at,
                canonical_json(activity.payload),
                write.message_id,
            ),
        )
        if activity.kind == "result.submitted":
            payload = activity.payload
            self._db.execute(
                """INSERT INTO agent_task_results
                   (run_id, target_ref, result_ref, result_digest, message_id,
                    payload_json, artifact_refs_json, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    activity.run_id,
                    activity.target_ref,
                    str(payload["resultRef"]),
                    str(payload["resultDigest"]),
                    write.message_id,
                    canonical_json(payload["result"]),
                    canonical_json(payload["artifactRefs"]),
                    activity.at,
                ),
            )
            target_state = "completed"
            result_ref: str | None = str(payload["resultRef"])
        else:
            target_state = (
                "waiting" if activity.kind in {"question", "blocked"} else "running"
            )
            result_ref = None
        self._db.execute(
            """UPDATE agent_task_targets
               SET state = ?, result_ref = COALESCE(?, result_ref)
               WHERE run_id = ? AND target_ref = ?""",
            (target_state, result_ref, activity.run_id, activity.target_ref),
        )
        self._sync_agent_task_run_state(
            run.run_id,
            last_event_id=activity.event_id,
            finished_at_ms=finished_at_ms,
        )

    def _save_agent_task_cancel(
        self,
        run: WorkflowRun,
        *,
        reason: str | None,
        finished_at_ms: int | None,
    ) -> None:
        self._db.execute(
            """UPDATE agent_task_targets SET state = 'cancelled'
               WHERE run_id = ? AND state != 'completed'""",
            (run.run_id,),
        )
        self._db.execute(
            """UPDATE agent_task_runs
               SET state = 'cancelled', cancel_reason = ?, finished_at_ms = ?
               WHERE run_id = ?""",
            (reason, finished_at_ms, run.run_id),
        )

    def _sync_agent_task_run_state(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        finished_at_ms: int | None = None,
    ) -> None:
        states = {
            str(row["state"])
            for row in self._db.execute(
                "SELECT state FROM agent_task_targets WHERE run_id = ?", (run_id,)
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
        self._db.execute(
            """UPDATE agent_task_runs
               SET state = ?, last_event_id = COALESCE(?, last_event_id),
                   finished_at_ms = CASE WHEN ? IS NULL THEN finished_at_ms ELSE ? END
               WHERE run_id = ? AND state != 'cancelled'""",
            (state, last_event_id, finished_at_ms, finished_at_ms, run_id),
        )

    def sync_agent_task_runtime(self, run: WorkflowRun) -> None:
        """Actor-owned generic transitions refresh attempts/failure projection."""

        with self._db_lock, self._db:
            self._sync_agent_task_runtime_locked(run)

    def _sync_agent_task_runtime_locked(
        self, run: WorkflowRun, *, finished_at_ms: int | None = None
    ) -> None:
        if self._db.execute(
            "SELECT 1 FROM agent_task_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone() is None:
            return
        target_refs = {
            str(row["target"]): str(row["target_ref"])
            for row in self._db.execute(
                "SELECT target, target_ref FROM agent_task_targets WHERE run_id = ?",
                (run.run_id,),
            )
        }
        for runtime in run.targets:
            target_ref = target_refs.get(runtime.name)
            if target_ref is None:
                continue
            mapped = _agent_task_target_state(runtime.state)
            self._db.execute(
                """UPDATE agent_task_targets SET state = ?
                   WHERE run_id = ? AND target_ref = ?
                     AND state NOT IN ('completed', 'cancelled')
                     AND (? IN ('failed', 'cancelled') OR state != 'waiting')""",
                (mapped, run.run_id, target_ref, mapped),
            )
        self._sync_agent_task_run_state(
            run.run_id, finished_at_ms=finished_at_ms
        )

    def load_run(self, run_id: str) -> PersistedRun | None:
        with self._db_lock:
            row = self._db.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._materialize(row)

    def cancel_unparseable_run(self, run_id: str) -> bool:
        """Force-cancel a stored run whose spec no longer validates.

        A schema-broken run cannot be materialized, so there is no
        ``WorkflowRun`` to hand to ``save_run``; update the raw row directly.
        Cancel is the operator escape hatch (2026-09-14 S3): cancelled is
        terminal, so nothing will resume the run, and the operator is no
        longer trapped with SQL as the only way out.
        """

        with self._db_lock, self._db:
            cursor = self._db.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (str(RunState.CANCELLED), run_id),
            )
            return cursor.rowcount == 1

    def load_open_runs(self) -> tuple[PersistedRun, ...]:
        runs, _ = self.load_open_runs_report()
        return runs

    def load_open_runs_report(
        self,
    ) -> tuple[tuple[PersistedRun, ...], tuple[RunSchemaRejection, ...]]:
        """Open runs split into resumable and schema-rejected.

        A rejected row is NEVER silently dropped: the caller must surface it
        (log + visible status) so unfinished runs with bad addresses do not
        disappear from recovery the way bare-name notices disappeared from
        readers.
        """

        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM runs WHERE state = ?", (str(RunState.RUNNING),)
            ).fetchall()
            runs: list[PersistedRun] = []
            rejections: list[RunSchemaRejection] = []
            for row in rows:
                try:
                    runs.append(self._materialize(row))
                except WorkflowSchemaError as error:
                    rejections.append(
                        RunSchemaRejection(
                            run_id=str(row["run_id"]),
                            state=str(row["state"]),
                            error=str(error),
                        )
                    )
            return tuple(runs), tuple(rejections)

    def list_runs(self, *, limit: int = 50) -> tuple[PersistedRun, ...]:
        runs, _ = self.list_runs_report(limit=limit)
        return runs

    def list_runs_report(
        self, *, limit: int = 50
    ) -> tuple[tuple[PersistedRun, ...], tuple[RunSchemaRejection, ...]]:
        """Newest runs plus schema rejections, so listing cannot hide them."""

        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM runs ORDER BY created_at_ms DESC LIMIT ?", (limit,)
            ).fetchall()
            runs: list[PersistedRun] = []
            rejections: list[RunSchemaRejection] = []
            for row in rows:
                try:
                    runs.append(self._materialize(row))
                except WorkflowSchemaError as error:
                    rejections.append(
                        RunSchemaRejection(
                            run_id=str(row["run_id"]),
                            state=str(row["state"]),
                            error=str(error),
                        )
                    )
            return tuple(runs), tuple(rejections)

    def max_version(self) -> int:
        with self._db_lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM runs"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def resolve_external_ref(self, external_ref: str) -> PersistedExternalRef | None:
        with self._db_lock:
            row = self._db.execute(
                "SELECT external_ref, request_digest, run_id "
                "FROM workflow_external_refs WHERE external_ref = ?",
                (external_ref,),
            ).fetchone()
            if row is None:
                return None
            return PersistedExternalRef(
                external_ref=str(row["external_ref"]),
                request_digest=str(row["request_digest"]),
                run_id=str(row["run_id"]),
            )

    def resolve_agent_task_external_ref(
        self, service_actor: str, external_ref: str
    ) -> PersistedAgentTaskRef | None:
        with self._db_lock:
            row = self._db.execute(
                """SELECT service_actor, namespace, external_ref,
                          request_digest, run_id
                   FROM agent_task_runs
                   WHERE service_actor = ? AND namespace = ? AND external_ref = ?""",
                (service_actor, AGENT_TASK_NAMESPACE, external_ref),
            ).fetchone()
            if row is None:
                return None
            return PersistedAgentTaskRef(
                service_actor=str(row["service_actor"]),
                namespace=str(row["namespace"]),
                external_ref=str(row["external_ref"]),
                request_digest=str(row["request_digest"]),
                run_id=str(row["run_id"]),
            )

    def is_agent_task_run(self, run_id: str) -> bool:
        with self._db_lock:
            return self._db.execute(
                "SELECT 1 FROM agent_task_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is not None

    def agent_task_target(
        self, run_id: str, target_ref: str
    ) -> PersistedAgentTaskTarget | None:
        with self._db_lock:
            row = self._db.execute(
                """SELECT target_ref, target, conversation_id, delegates_json,
                          state, result_ref
                   FROM agent_task_targets WHERE run_id = ? AND target_ref = ?""",
                (run_id, target_ref),
            ).fetchone()
            if row is None:
                return None
            return PersistedAgentTaskTarget(
                target_ref=str(row["target_ref"]),
                target=str(row["target"]),
                conversation_id=str(row["conversation_id"]),
                delegates=tuple(json.loads(str(row["delegates_json"]))),
                state=str(row["state"]),
                result_ref=row["result_ref"],
            )

    def agent_task_event_digest(self, event_id: str) -> str | None:
        with self._db_lock:
            row = self._db.execute(
                "SELECT event_digest FROM agent_task_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return None if row is None else str(row["event_digest"])

    def agent_task_result_record(
        self, run_id: str, target_ref: str
    ) -> PersistedAgentTaskResult | None:
        with self._db_lock:
            row = self._db.execute(
                """SELECT result_ref, result_digest, message_id
                   FROM agent_task_results WHERE run_id = ? AND target_ref = ?""",
                (run_id, target_ref),
            ).fetchone()
            if row is None:
                return None
            return PersistedAgentTaskResult(
                result_ref=str(row["result_ref"]),
                result_digest=str(row["result_digest"]),
                message_id=str(row["message_id"]),
            )

    def read_agent_task_run(
        self, run_id: str, service_actor: str, *, created: bool | None = None
    ) -> AgentTaskRunProjection | None:
        with self._db_lock:
            run = self._db.execute(
                """SELECT * FROM agent_task_runs
                   WHERE run_id = ? AND service_actor = ? AND namespace = ?""",
                (run_id, service_actor, AGENT_TASK_NAMESPACE),
            ).fetchone()
            if run is None:
                return None
            return AgentTaskRunProjection(
                run_id=run_id,
                external_ref=str(run["external_ref"]),
                state=str(run["state"]),
                targets=self._agent_task_target_projections(run_id, include_result=False),
                last_event_id=run["last_event_id"],
                created=created,
            )

    def read_agent_task_result(
        self, run_id: str, service_actor: str
    ) -> AgentTaskResultProjection | None:
        with self._db_lock:
            run = self._db.execute(
                """SELECT external_ref FROM agent_task_runs
                   WHERE run_id = ? AND service_actor = ? AND namespace = ?""",
                (run_id, service_actor, AGENT_TASK_NAMESPACE),
            ).fetchone()
            if run is None:
                return None
            return AgentTaskResultProjection(
                run_id=run_id,
                external_ref=str(run["external_ref"]),
                targets=self._agent_task_target_projections(run_id, include_result=True),
            )

    def _agent_task_target_projections(
        self, run_id: str, *, include_result: bool
    ) -> tuple[AgentTaskTargetProjection, ...]:
        generic = {
            str(row["target"]): row
            for row in self._db.execute(
                """SELECT target, attempts FROM targets
                   WHERE run_id = ? ORDER BY rowid""",
                (run_id,),
            )
        }
        output: list[AgentTaskTargetProjection] = []
        for row in self._db.execute(
            "SELECT * FROM agent_task_targets WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ):
            result = None
            if include_result:
                stored = self._db.execute(
                    """SELECT * FROM agent_task_results
                       WHERE run_id = ? AND target_ref = ?""",
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
            runtime = generic.get(str(row["target"]))
            output.append(
                AgentTaskTargetProjection(
                    target_ref=str(row["target_ref"]),
                    target=str(row["target"]),
                    conversation_id=str(row["conversation_id"]),
                    attempts=0 if runtime is None else int(runtime["attempts"]),
                    state=str(row["state"]),
                    result_ref=row["result_ref"],
                    result=result,
                )
            )
        return tuple(output)

    def load_effect_batches(self) -> tuple[PersistedEffectBatch, ...]:
        import json

        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM workflow_effect_batches ORDER BY rowid"
            ).fetchall()
            batches: list[PersistedEffectBatch] = []
            for row in rows:
                effects = self._db.execute(
                    "SELECT payload_json FROM workflow_effects "
                    "WHERE correlation_id = ? ORDER BY rowid",
                    (str(row["correlation_id"]),),
                ).fetchall()
                batches.append(
                    PersistedEffectBatch(
                        correlation_id=str(row["correlation_id"]),
                        final_kind=str(row["final_kind"]),
                        final_payload=json.loads(str(row["final_payload_json"])),
                        effects=tuple(
                            json.loads(str(effect["payload_json"]))
                            for effect in effects
                        ),
                    )
                )
            return tuple(batches)

    def pending_effect_payloads(self, *, limit: int = 256) -> tuple[dict[str, Any], ...]:
        import json

        with self._db_lock:
            rows = self._db.execute(
                "SELECT payload_json FROM workflow_effects ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(json.loads(str(row["payload_json"])) for row in rows)

    def delete_effect_batch(self, correlation_id: str) -> None:
        with self._db_lock, self._db:
            self._db.execute(
                "DELETE FROM workflow_effects WHERE correlation_id = ?",
                (correlation_id,),
            )
            self._db.execute(
                "DELETE FROM workflow_effect_batches WHERE correlation_id = ?",
                (correlation_id,),
            )

    def delete_effect(self, effect_id: str) -> None:
        with self._db_lock, self._db:
            self._db.execute(
                "DELETE FROM workflow_effects WHERE effect_id = ?", (effect_id,)
            )

    def save_effect_batch(self, batch: PersistedEffectBatch) -> None:
        with self._db_lock, self._db:
            self._save_effect_batch(batch)

    def _save_effect_batch(self, batch: PersistedEffectBatch) -> None:
        import json

        self._db.execute(
            "INSERT OR REPLACE INTO workflow_effect_batches "
            "(correlation_id, final_kind, final_payload_json) VALUES (?, ?, ?)",
            (
                batch.correlation_id,
                batch.final_kind,
                json.dumps(batch.final_payload, sort_keys=True),
            ),
        )
        for effect in batch.effects:
            self._db.execute(
                "INSERT OR REPLACE INTO workflow_effects "
                "(effect_id, correlation_id, generation, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(effect["effect_id"]),
                    batch.correlation_id,
                    int(effect["generation"]),
                    json.dumps(effect, sort_keys=True),
                ),
            )

    def _materialize(self, row: sqlite3.Row) -> PersistedRun:
        yaml_text = str(row["yaml_text"])
        spec = load_workflow_text(yaml_text, label=f"stored run {row['run_id']}")
        run = WorkflowRun(
            run_id=str(row["run_id"]),
            spec=spec,
            sender=str(row["sender"]),
            nonce=str(row["nonce"]),
            state=RunState(str(row["state"])),
            report_text=row["report_text"],
            dispatch_warnings=tuple(json.loads(row["dispatch_warnings"])),
        )
        target_rows = self._db.execute(
            "SELECT * FROM targets WHERE run_id = ? ORDER BY rowid", (run.run_id,)
        ).fetchall()
        for target_row in target_rows:
            run.targets.append(
                TargetRuntime(
                    name=str(target_row["target"]),
                    conversation_id=str(target_row["conversation_id"]),
                    state=TargetState(str(target_row["state"])),
                    attempts=int(target_row["attempts"]),
                    deadline_ms=target_row["deadline_ms"],
                    next_attempt_ms=target_row["next_attempt_ms"],
                    last_message_id=target_row["last_message_id"],
                    reply_excerpt=target_row["reply_excerpt"],
                    delivered=bool(target_row["delivered"]),
                    extend_count=int(target_row["extend_count"]),
                )
            )
        return PersistedRun(
            run=run,
            yaml_text=yaml_text,
            version=int(row["version"]),
        )


def _agent_task_target_state(state: TargetState) -> str:
    return {
        TargetState.PENDING: "reserved",
        TargetState.DISPATCHED: "running",
        TargetState.BACKOFF: "waiting",
        TargetState.DONE: "completed",
        TargetState.TIMED_OUT: "failed",
        TargetState.ESCALATED: "failed",
    }[state]
