"""SQLite authority for actor-owned self-drive routines.

The registry actor is the only writer. Projection readers and the external
effect pump use separate connections, so the connection-local lock below is
not a business-state lock. Accepted cycles and their effects are committed in
one transaction: a full in-memory queue or a daemon restart cannot lose work.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    name TEXT PRIMARY KEY,
    yaml_text TEXT NOT NULL,
    owner TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_due_ms INTEGER NOT NULL,
    source_error_streak INTEGER NOT NULL DEFAULT 0,
    outcomes TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS in_flight (
    routine TEXT NOT NULL,
    task_uuid TEXT NOT NULL,
    run_id TEXT NOT NULL,
    target TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    PRIMARY KEY (routine, task_uuid)
);
CREATE TABLE IF NOT EXISTS routine_cycles (
    correlation_id TEXT PRIMARY KEY,
    routine_name TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL,
    version INTEGER NOT NULL,
    tasks_json TEXT NOT NULL DEFAULT '[]',
    outcomes_json TEXT NOT NULL DEFAULT '[]',
    pending_status INTEGER NOT NULL DEFAULT 0,
    pending_start INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routine_effects (
    effect_id TEXT PRIMARY KEY,
    parent_correlation_id TEXT NOT NULL,
    routine_name TEXT NOT NULL,
    generation INTEGER NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS routine_effects_parent
ON routine_effects(parent_correlation_id);
CREATE INDEX IF NOT EXISTS routine_effects_routine
ON routine_effects(routine_name);
"""


@dataclass(frozen=True, slots=True)
class RoutineRow:
    name: str
    yaml_text: str
    owner: str
    enabled: bool
    next_due_ms: int
    source_error_streak: int
    outcomes: str
    created_at_ms: int
    version: int = 1


@dataclass(frozen=True, slots=True)
class InFlightRow:
    routine: str
    task_uuid: str
    run_id: str
    target: str
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class RoutineCycleRow:
    correlation_id: str
    routine_name: str
    generation: int
    version: int
    tasks_json: str = "[]"
    outcomes_json: str = "[]"
    pending_status: int = 0
    pending_start: int = 0


@dataclass(frozen=True, slots=True)
class RoutineEffectRow:
    effect_id: str
    parent_correlation_id: str
    routine_name: str
    generation: int
    version: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RoutineSnapshot:
    routine: RoutineRow
    in_flight: tuple[InFlightRow, ...]


class RoutineStore:
    """One SQLite connection; callers must not share it between actors."""

    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        with self._db:
            self._db.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in self._db.execute("PRAGMA table_info(routines)")
            }
            if "version" not in columns:
                self._db.execute(
                    "ALTER TABLE routines ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def apply(
        self,
        *,
        routine: RoutineRow | None = None,
        remove_routine: str | None = None,
        clear_work_for: str | None = None,
        cycle: RoutineCycleRow | None = None,
        remove_cycle: str | None = None,
        put_in_flight: tuple[InFlightRow, ...] = (),
        remove_in_flight: tuple[tuple[str, str], ...] = (),
        add_effects: tuple[RoutineEffectRow, ...] = (),
        delete_effects: tuple[str, ...] = (),
    ) -> None:
        """Atomically apply one actor transition and its durable effects."""

        with self._lock, self._db:
            if remove_routine is not None:
                self._db.execute("DELETE FROM routines WHERE name = ?", (remove_routine,))
                self._db.execute("DELETE FROM in_flight WHERE routine = ?", (remove_routine,))
                self._db.execute(
                    "DELETE FROM routine_cycles WHERE routine_name = ?", (remove_routine,)
                )
                self._db.execute(
                    "DELETE FROM routine_effects WHERE routine_name = ?", (remove_routine,)
                )
            if clear_work_for is not None:
                self._db.execute(
                    "DELETE FROM routine_cycles WHERE routine_name = ?", (clear_work_for,)
                )
                self._db.execute(
                    "DELETE FROM routine_effects WHERE routine_name = ?", (clear_work_for,)
                )
            if routine is not None:
                self._db.execute(
                    """INSERT INTO routines
                       (name, yaml_text, owner, enabled, next_due_ms,
                        source_error_streak, outcomes, created_at_ms, version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           yaml_text = excluded.yaml_text,
                           owner = excluded.owner,
                           enabled = excluded.enabled,
                           next_due_ms = excluded.next_due_ms,
                           source_error_streak = excluded.source_error_streak,
                           outcomes = excluded.outcomes,
                           version = excluded.version""",
                    (
                        routine.name,
                        routine.yaml_text,
                        routine.owner,
                        int(routine.enabled),
                        routine.next_due_ms,
                        routine.source_error_streak,
                        routine.outcomes,
                        routine.created_at_ms,
                        routine.version,
                    ),
                )
            if remove_cycle is not None:
                self._db.execute(
                    "DELETE FROM routine_cycles WHERE correlation_id = ?",
                    (remove_cycle,),
                )
            if cycle is not None:
                self._db.execute(
                    """INSERT INTO routine_cycles
                       (correlation_id, routine_name, generation, version,
                        tasks_json, outcomes_json, pending_status, pending_start)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(correlation_id) DO UPDATE SET
                           generation = excluded.generation,
                           version = excluded.version,
                           tasks_json = excluded.tasks_json,
                           outcomes_json = excluded.outcomes_json,
                           pending_status = excluded.pending_status,
                           pending_start = excluded.pending_start""",
                    (
                        cycle.correlation_id,
                        cycle.routine_name,
                        cycle.generation,
                        cycle.version,
                        cycle.tasks_json,
                        cycle.outcomes_json,
                        cycle.pending_status,
                        cycle.pending_start,
                    ),
                )
            for item in put_in_flight:
                self._db.execute(
                    """INSERT INTO in_flight
                       (routine, task_uuid, run_id, target, started_at_ms)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(routine, task_uuid) DO UPDATE SET
                           run_id = excluded.run_id,
                           target = excluded.target,
                           started_at_ms = excluded.started_at_ms""",
                    (
                        item.routine,
                        item.task_uuid,
                        item.run_id,
                        item.target,
                        item.started_at_ms,
                    ),
                )
            for routine_name, task_uuid in remove_in_flight:
                self._db.execute(
                    "DELETE FROM in_flight WHERE routine = ? AND task_uuid = ?",
                    (routine_name, task_uuid),
                )
            for effect_id in delete_effects:
                self._db.execute(
                    "DELETE FROM routine_effects WHERE effect_id = ?", (effect_id,)
                )
            for effect in add_effects:
                payload = dict(effect.payload)
                payload["generation"] = effect.generation
                self._db.execute(
                    """INSERT OR REPLACE INTO routine_effects
                       (effect_id, parent_correlation_id, routine_name,
                        generation, version, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        effect.effect_id,
                        effect.parent_correlation_id,
                        effect.routine_name,
                        effect.generation,
                        effect.version,
                        json.dumps(payload, sort_keys=True),
                    ),
                )

    def upsert_routine(
        self,
        *,
        name: str,
        yaml_text: str,
        owner: str,
        enabled: bool,
        next_due_ms: int,
        source_error_streak: int = 0,
        outcomes: str = "[]",
        created_at_ms: int,
        version: int = 1,
    ) -> None:
        self.apply(
            routine=RoutineRow(
                name,
                yaml_text,
                owner,
                enabled,
                next_due_ms,
                source_error_streak,
                outcomes,
                created_at_ms,
                version,
            )
        )

    def remove_routine(self, name: str) -> bool:
        if self.get_routine(name) is None:
            return False
        self.apply(remove_routine=name)
        return True

    def put_in_flight(
        self,
        *,
        routine: str,
        task_uuid: str,
        run_id: str,
        target: str,
        started_at_ms: int,
    ) -> None:
        self.apply(
            put_in_flight=(
                InFlightRow(routine, task_uuid, run_id, target, started_at_ms),
            )
        )

    def remove_in_flight(self, *, routine: str, task_uuid: str) -> None:
        self.apply(remove_in_flight=((routine, task_uuid),))

    def get_routine(self, name: str) -> RoutineRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM routines WHERE name = ?", (name,)
            ).fetchone()
            return None if row is None else self._routine_row(row)

    def list_routines(self) -> tuple[RoutineRow, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM routines ORDER BY created_at_ms"
            ).fetchall()
            return tuple(self._routine_row(row) for row in rows)

    def snapshot(self, name: str) -> RoutineSnapshot | None:
        snapshots = self._snapshots("WHERE r.name = ?", (name,))
        return snapshots[0] if snapshots else None

    def snapshots(self) -> tuple[RoutineSnapshot, ...]:
        return self._snapshots("", ())

    def _snapshots(
        self, where: str, params: tuple[object, ...]
    ) -> tuple[RoutineSnapshot, ...]:
        """Materialize projection rows with one SQLite snapshot statement."""

        with self._lock:
            rows = self._db.execute(
                "SELECT r.*, i.task_uuid AS i_task_uuid, i.run_id AS i_run_id, "
                "i.target AS i_target, i.started_at_ms AS i_started_at_ms "
                "FROM routines r LEFT JOIN in_flight i ON i.routine = r.name "
                f"{where} ORDER BY r.created_at_ms, i.rowid",
                params,
            ).fetchall()
        grouped: dict[str, tuple[RoutineRow, list[InFlightRow]]] = {}
        for row in rows:
            name = str(row["name"])
            if name not in grouped:
                grouped[name] = (self._routine_row(row), [])
            if row["i_task_uuid"] is not None:
                grouped[name][1].append(
                    InFlightRow(
                        routine=name,
                        task_uuid=str(row["i_task_uuid"]),
                        run_id=str(row["i_run_id"]),
                        target=str(row["i_target"]),
                        started_at_ms=int(row["i_started_at_ms"]),
                    )
                )
        return tuple(
            RoutineSnapshot(routine, tuple(in_flight))
            for routine, in_flight in grouped.values()
        )

    def in_flight(self, *, routine: str) -> tuple[InFlightRow, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM in_flight WHERE routine = ? ORDER BY rowid",
                (routine,),
            ).fetchall()
            return tuple(
                InFlightRow(
                    routine=str(row["routine"]),
                    task_uuid=str(row["task_uuid"]),
                    run_id=str(row["run_id"]),
                    target=str(row["target"]),
                    started_at_ms=int(row["started_at_ms"]),
                )
                for row in rows
            )

    def cycle_for_routine(self, routine_name: str) -> RoutineCycleRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM routine_cycles WHERE routine_name = ?",
                (routine_name,),
            ).fetchone()
            return None if row is None else self._cycle_row(row)

    def cycle(self, correlation_id: str) -> RoutineCycleRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM routine_cycles WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
            return None if row is None else self._cycle_row(row)

    def effect(self, effect_id: str) -> RoutineEffectRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM routine_effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            return None if row is None else self._effect_row(row)

    def pending_effects(self, *, limit: int = 256) -> tuple[RoutineEffectRow, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM routine_effects ORDER BY rowid LIMIT ?", (limit,)
            ).fetchall()
            return tuple(self._effect_row(row) for row in rows)

    def max_version(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM routines"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def rebase_pending_generation(self, generation: int) -> None:
        """A restarted actor adopts, rather than duplicates, durable effects."""

        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT effect_id, payload_json FROM routine_effects"
            ).fetchall()
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                payload["generation"] = generation
                self._db.execute(
                    "UPDATE routine_effects SET generation = ?, payload_json = ? "
                    "WHERE effect_id = ?",
                    (generation, json.dumps(payload, sort_keys=True), str(row["effect_id"])),
                )
            self._db.execute(
                "UPDATE routine_cycles SET generation = ?", (generation,)
            )

    @staticmethod
    def _routine_row(row: sqlite3.Row) -> RoutineRow:
        return RoutineRow(
            name=str(row["name"]),
            yaml_text=str(row["yaml_text"]),
            owner=str(row["owner"]),
            enabled=bool(row["enabled"]),
            next_due_ms=int(row["next_due_ms"]),
            source_error_streak=int(row["source_error_streak"]),
            outcomes=str(row["outcomes"]),
            created_at_ms=int(row["created_at_ms"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _cycle_row(row: sqlite3.Row) -> RoutineCycleRow:
        return RoutineCycleRow(
            correlation_id=str(row["correlation_id"]),
            routine_name=str(row["routine_name"]),
            generation=int(row["generation"]),
            version=int(row["version"]),
            tasks_json=str(row["tasks_json"]),
            outcomes_json=str(row["outcomes_json"]),
            pending_status=int(row["pending_status"]),
            pending_start=int(row["pending_start"]),
        )

    @staticmethod
    def _effect_row(row: sqlite3.Row) -> RoutineEffectRow:
        return RoutineEffectRow(
            effect_id=str(row["effect_id"]),
            parent_correlation_id=str(row["parent_correlation_id"]),
            routine_name=str(row["routine_name"]),
            generation=int(row["generation"]),
            version=int(row["version"]),
            payload=json.loads(str(row["payload_json"])),
        )
