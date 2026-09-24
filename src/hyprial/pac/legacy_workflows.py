"""One-way cutover of legacy runs and a read-only history outlet.

Cutover terminates tracking, never launches, adopts or signals an actor. The
original rows are retained and the prior states are recorded before mutation.
Sealing triggers also reject writers from a previously running old process.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from time import time_ns
from typing import Any

from .errors import PacError

TERMINAL_RUNS = {"completed", "cancelled", "failed"}
TERMINAL_TARGETS = {
    "completed",
    "succeeded",
    "rejected",
    "done",
    "timed_out",
    "escalated",
    "cancelled",
    "failed",
}

_CUTOVER_REASON = (
    "workflow execution retired; unfinished tracking terminated, no task replay"
)
_EFFECT_BATCHES = "workflow_effect_batches"
_EFFECTS = "workflow_effects"
_ARCHIVED_EFFECT_BATCHES = "pac_archived_workflow_effect_batches"
_ARCHIVED_EFFECTS = "pac_archived_workflow_effects"
_SAFETY_MARKER = "pac_cutover_safety"


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _effect_columns(db: sqlite3.Connection, table: str) -> list[str]:
    columns = [
        row[1] for row in db.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    ]
    if not columns:
        raise PacError("WORKFLOW_ARCHIVE_SCHEMA_INVALID", f"{table} is missing")
    return columns


def _ensure_effect_archive_tables(db: sqlite3.Connection) -> None:
    """Clone the two legacy effect shapes, keeping the child FK in the archive."""

    source_columns = {
        table: _effect_columns(db, table) for table in (_EFFECT_BATCHES, _EFFECTS)
    }
    if "correlation_id" not in source_columns[_EFFECTS]:
        raise PacError(
            "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
            "workflow_effects has no correlation_id column",
        )

    for source, archive in (
        (_EFFECT_BATCHES, _ARCHIVED_EFFECT_BATCHES),
        (_EFFECTS, _ARCHIVED_EFFECTS),
    ):
        existing = _tables(db)
        if archive not in existing:
            info = db.execute(f"PRAGMA table_info({_quote(source)})").fetchall()
            definitions: list[str] = []
            primary_key = [row[1] for row in info if row[5]]
            for row in info:
                _, name, declared_type, not_null, default, primary = row
                definition = _quote(name)
                if declared_type:
                    definition += f" {declared_type}"
                if primary and len(primary_key) == 1:
                    definition += " PRIMARY KEY"
                if not_null:
                    definition += " NOT NULL"
                if default is not None:
                    definition += f" DEFAULT {default}"
                definitions.append(definition)
            if len(primary_key) > 1:
                definitions.append(
                    "PRIMARY KEY ("
                    + ", ".join(_quote(name) for name in primary_key)
                    + ")"
                )
            if source == _EFFECTS:
                definitions.append(
                    f"FOREIGN KEY ({_quote('correlation_id')}) "
                    f"REFERENCES {_quote(_ARCHIVED_EFFECT_BATCHES)}({_quote('correlation_id')})"
                )
            db.execute(
                f"CREATE TABLE {_quote(archive)} ({', '.join(definitions)})"
            )
        else:
            archive_columns = _effect_columns(db, archive)
            if archive_columns != source_columns[source]:
                raise PacError(
                    "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
                    f"{archive} does not match {source}",
                )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote(_ARCHIVED_EFFECTS + '_batch')} "
        f"ON {_quote(_ARCHIVED_EFFECTS)}({_quote('correlation_id')})"
    )


def _copy_and_empty_effect_tables(db: sqlite3.Connection) -> None:
    _ensure_effect_archive_tables(db)
    for source, archive in (
        (_EFFECT_BATCHES, _ARCHIVED_EFFECT_BATCHES),
        (_EFFECTS, _ARCHIVED_EFFECTS),
    ):
        columns = _effect_columns(db, source)
        quoted_columns = ", ".join(_quote(column) for column in columns)
        db.execute(
            f"INSERT INTO {_quote(archive)} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM {_quote(source)}"
        )
        source_rows = db.execute(
            f"SELECT {quoted_columns} FROM {_quote(source)} ORDER BY rowid"
        ).fetchall()
        archive_rows = db.execute(
            f"SELECT {quoted_columns} FROM {_quote(archive)} ORDER BY rowid"
        ).fetchall()
        if source_rows != archive_rows:
            raise PacError(
                "WORKFLOW_ARCHIVE_VERIFY_FAILED",
                f"{source} did not match its archive",
            )
    # The child must go first while foreign keys are enabled.
    db.execute(f"DELETE FROM {_quote(_EFFECTS)}")
    db.execute(f"DELETE FROM {_quote(_EFFECT_BATCHES)}")


def _drop_effect_seals_for_repair(db: sqlite3.Connection) -> None:
    for table in (_EFFECT_BATCHES, _EFFECTS):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"DROP TRIGGER IF EXISTS "
                f"{_quote(f'pac_sealed_{table}_{operation}')}"
            )


def _cancelled_entry(
    db: sqlite3.Connection,
    table: str,
    rowid: int,
    prior: dict[str, Any],
    *,
    at: int,
) -> dict[str, Any]:
    current_row = db.execute(
        f"SELECT * FROM {_quote(table)} WHERE rowid=?", (rowid,)
    ).fetchone()
    if current_row is None:
        raise PacError(
            "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
            f"cancelled {table} row disappeared",
        )
    current = dict(current_row)
    run_id = current.get("run_id")
    if run_id is None:
        raise PacError("WORKFLOW_ARCHIVE_SCHEMA_INVALID", f"{table} has no run_id")
    if current.get("state") != "cancelled":
        raise PacError(
            "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
            f"{table} row {run_id} was not cancelled",
        )
    return {
        "table": table,
        "runId": run_id,
        "sender": prior.get("sender")
        if table == "runs"
        else prior.get("caller"),
        "name": prior.get("name") if table == "runs" else None,
        "priorState": prior.get("state"),
        "state": current.get("state"),
        "finishedAtMs": current.get("finished_at_ms", at),
        "reason": _CUTOVER_REASON,
    }


def _reconstruct_cancelled(db: sqlite3.Connection, *, at: int) -> list[dict[str, Any]]:
    if "pac_prior_states" not in _tables(db):
        raise PacError(
            "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
            "pac_prior_states is required to repair a sealed cutover",
        )
    cancelled: list[dict[str, Any]] = []
    for table in ("runs", "agent_task_runs"):
        if table not in _tables(db):
            continue
        rows = db.execute(
            "SELECT row_key, prior_json FROM pac_prior_states "
            "WHERE table_name=? ORDER BY row_key",
            (table,),
        ).fetchall()
        for row in rows:
            prior = json.loads(row[1])
            if not isinstance(prior, dict) or prior.get("state") in (
                None,
                *TERMINAL_RUNS,
            ):
                continue
            cancelled.append(
                _cancelled_entry(db, table, int(row[0]), prior, at=at)
            )
    cancelled.sort(key=lambda item: (item["table"], item["runId"]))
    return cancelled


def _seal_tables(db: sqlite3.Connection) -> None:
    # Quote identifiers, including future legacy tables. No table or trigger
    # name comes from a user-controlled SQL fragment.
    for table in _tables(db):
        quoted = _quote(table)
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = _quote(f"pac_sealed_{table}_{operation}")
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE {operation} ON {quoted} "
                "BEGIN SELECT RAISE(ABORT,'WORKFLOW_RETIRED: legacy archive is read-only'); END"
            )


def cutover(state_dir: Path, *, at: int | None = None) -> dict[str, Any]:
    """Atomically seal legacy writers, including on a fresh home.

    A fresh home gets an explicitly empty write-barrier database. Otherwise an
    older daemon could recreate its tables and dispatch again after downgrade.
    No historical records are fabricated to build that barrier.
    """
    path = state_dir / "workflows.sqlite3"
    source_present = path.exists()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    at = time_ns() // 1_000_000 if at is None else at
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        tables = _tables(db)
        cutover_row = (
            db.execute("SELECT * FROM pac_cutover WHERE id=1").fetchone()
            if "pac_cutover" in tables
            else None
        )
        if _SAFETY_MARKER in tables:
            safety = db.execute(
                f"SELECT version, cancelled_json FROM {_quote(_SAFETY_MARKER)} WHERE id=1"
            ).fetchone()
            if safety is not None:
                if safety[0] != 2:
                    raise PacError(
                        "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
                        f"unsupported { _SAFETY_MARKER } version {safety[0]}",
                    )
                cancelled = json.loads(safety[1])
                if not isinstance(cancelled, list):
                    raise PacError(
                        "WORKFLOW_ARCHIVE_SCHEMA_INVALID",
                        "pac_cutover_safety.cancelled_json is not a list",
                    )
                db.rollback()
                row = db.execute(
                    "SELECT * FROM pac_cutover WHERE id=1"
                ).fetchone()
                return {
                    "present": bool(row["source_present"])
                    if "source_present" in row.keys()
                    else True,
                    "terminated": len(cancelled),
                    "sealed": True,
                    "at": row["at"],
                    "cancelled": cancelled,
                }
        repair = cutover_row is not None
        if repair:
            # Version-1 cutovers sealed the live effect tables before this
            # archive existed.  Only remove this module's own named seals;
            # transaction rollback restores them if repair fails.
            _drop_effect_seals_for_repair(db)
            at = cutover_row["at"]
        # Execute DDL statements individually: executescript would commit
        # before the seal and expose an unprotected legacy write window.
        statement = ""
        for line in (
            Path(__file__)
            .with_name("legacy_barrier.sql")
            .read_text()
            .splitlines(keepends=True)
        ):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        tables = _tables(db)
        db.execute(
            "CREATE TABLE IF NOT EXISTS pac_cutover(id INTEGER PRIMARY KEY CHECK(id=1),at INTEGER NOT NULL,terminated INTEGER NOT NULL,reason TEXT NOT NULL,source_present INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS pac_prior_states(table_name TEXT NOT NULL,row_key TEXT NOT NULL,prior_json TEXT NOT NULL,PRIMARY KEY(table_name,row_key))"
        )
        terminated = 0
        cancelled: list[dict[str, Any]] = []
        for table in (
            ()
            if repair
            else ("runs", "targets", "agent_task_runs", "agent_task_targets")
        ):
            if table not in tables:
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if "state" not in columns:
                raise PacError(
                    "WORKFLOW_ARCHIVE_SCHEMA_INVALID", f"{table} has no state column"
                )
            terminal = TERMINAL_TARGETS if "targets" in table else TERMINAL_RUNS
            for row in db.execute(
                f'SELECT rowid AS archive_rowid,* FROM "{table}"'
            ).fetchall():
                if row["state"] in terminal:
                    continue
                prior = dict(row)
                rowid = prior.pop("archive_rowid")
                db.execute(
                    "INSERT INTO pac_prior_states VALUES (?,?,?)",
                    (table, str(rowid), json.dumps(prior, sort_keys=True)),
                )
                update = "state='cancelled'"
                if "finished_at_ms" in columns:
                    update += ",finished_at_ms=?"
                    args = (at, rowid)
                else:
                    args = (rowid,)
                db.execute(f'UPDATE "{table}" SET {update} WHERE rowid=?', args)
                if table in ("runs", "agent_task_runs"):
                    terminated += 1
                    cancelled.append(
                        _cancelled_entry(db, table, rowid, prior, at=at)
                    )
        # Shared assignment rows remain preserved in the archive; retire only
        # rows whose legacy run has a known terminal record.
        if not repair and "actor_assign" in tables and "runs" in tables:
            db.execute(
                "UPDATE actor_assign SET released_at_ms=? WHERE released_at_ms IS NULL AND assign_kind='workflow' AND assign_ref IN (SELECT run_id FROM runs WHERE state IN ('completed','cancelled','failed'))",
                (at,),
            )
        cancelled.sort(key=lambda item: (item["table"], item["runId"]))
        if repair:
            cancelled = _reconstruct_cancelled(db, at=at)
            terminated = len(cancelled)
        else:
            db.execute(
                "INSERT INTO pac_cutover VALUES (1,?,?,?,?)",
                (
                    at,
                    terminated,
                    _CUTOVER_REASON,
                    int(source_present),
                ),
            )
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {_quote(_SAFETY_MARKER)}("
            "id INTEGER PRIMARY KEY CHECK(id=1),"
            "version INTEGER NOT NULL, cancelled_json TEXT NOT NULL)"
        )
        db.execute(
            f"INSERT INTO {_quote(_SAFETY_MARKER)} VALUES (1,2,?)",
            (json.dumps(cancelled, sort_keys=True, separators=(",", ":")),),
        )
        _copy_and_empty_effect_tables(db)
        _seal_tables(db)
        db.commit()
        return {
            "present": source_present,
            "terminated": terminated,
            "sealed": True,
            "at": at,
            "cancelled": cancelled,
        }
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class LegacyWorkflowHistory:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "workflows.sqlite3"

    def _open(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def list(self, *, limit: int = 50, viewer: str | None = None) -> dict[str, Any]:
        db = self._open()
        if db is None:
            return {"runs": []}
        try:
            if "runs" not in _tables(db):
                return {"runs": []}
            rows = db.execute(
                "SELECT run_id,name,state,sender,created_at_ms,finished_at_ms FROM runs "
                "WHERE (? IS NULL OR sender=?) ORDER BY created_at_ms DESC LIMIT ?",
                (viewer, viewer, min(max(limit, 1), 500)),
            ).fetchall()
            return {
                "runs": [
                    {
                        **dict(row),
                        "runId": row["run_id"],
                        "backend": "legacy-archive",
                        "readOnly": True,
                    }
                    for row in rows
                ]
            }
        finally:
            db.close()

    def status(self, run_id: str, *, viewer: str | None = None) -> dict[str, Any]:
        db = self._open()
        if db is None:
            raise PacError("WORKFLOW_NOT_FOUND", "legacy workflow not found")
        try:
            db.execute("BEGIN")
            tables = _tables(db)
            row = (
                db.execute(
                    "SELECT rowid AS archive_rowid,* FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if "runs" in tables
                else None
            )
            if row is None or (viewer is not None and row["sender"] != viewer):
                raise PacError(
                    "WORKFLOW_NOT_FOUND",
                    "legacy workflow not found or not owned by caller",
                )
            result = dict(row)
            rowid = result.pop("archive_rowid")
            prior = (
                db.execute(
                    "SELECT prior_json FROM pac_prior_states WHERE table_name='runs' AND row_key=?",
                    (str(rowid),),
                ).fetchone()
                if "pac_prior_states" in tables
                else None
            )
            targets = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM targets WHERE run_id=?", (run_id,)
                    )
                ]
                if "targets" in tables
                else []
            )
            for target in targets:
                target["conversationId"] = target.get("conversation_id")
                target["replyExcerpt"] = target.get("reply_excerpt")
                target["deadlineMs"] = target.get("deadline_ms")
                target["extendCount"] = target.get("extend_count", 0)
            deliveries = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM workflow_node_deliveries WHERE run_id=?",
                        (run_id,),
                    )
                ]
                if "workflow_node_deliveries" in tables
                else []
            )
            return {
                **result,
                "createdAtMs": result.get("created_at_ms"),
                "deliveries": [
                    {
                        "deliveryId": d["message_id"],
                        "actor": d["recipient"],
                        "targetRef": d["target_ref"],
                    }
                    for d in deliveries
                ],
                "runId": run_id,
                "backend": "legacy-archive",
                "readOnly": True,
                "targets": targets,
                "priorState": json.loads(prior[0])["state"] if prior else row["state"],
                "cutoverReason": "unfinished tracking terminated without replay"
                if prior
                else None,
            }
        finally:
            db.close()
