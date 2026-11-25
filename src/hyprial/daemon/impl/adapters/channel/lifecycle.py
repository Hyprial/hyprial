"""Generation-fenced shared channel lifecycle and health projection."""

from __future__ import annotations

import sqlite3

from hyprial.daemon.impl.adapters.contracts import AdapterCloseOutcome, AdapterHealth


class LifecycleConflict(ValueError):
    pass


class ChannelLifecycle:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        for statement in (
            """CREATE TABLE IF NOT EXISTS adapter_channel_lifecycle (
                instance_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'configured','starting','online','degraded','reconnecting','stopped','blocked'
                )),
                capabilities_revision TEXT NOT NULL,
                admission_open INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS adapter_channel_pending (
                instance_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                operation_id TEXT NOT NULL,
                unknown INTEGER NOT NULL,
                PRIMARY KEY(instance_id,operation_id)
            )""",
        ):
            connection.execute(statement)
        connection.commit()

    def start(self, instance_id: str, *, generation: int, session_id: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT generation,state FROM adapter_channel_lifecycle WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if row is not None and int(row[0]) > generation:
                raise LifecycleConflict("ADAPTER_GENERATION_STALE")
            self.connection.execute(
                "INSERT INTO adapter_channel_lifecycle VALUES (?, ?, ?, 'online','initial:v1',1) "
                "ON CONFLICT(instance_id) DO UPDATE SET generation=excluded.generation,"
                "session_id=excluded.session_id,state='online',admission_open=1",
                (instance_id, generation, session_id),
            )

    def _require_generation(self, instance_id: str, generation: int) -> None:
        row = self.connection.execute(
            "SELECT generation FROM adapter_channel_lifecycle WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None or int(row[0]) != generation:
            raise LifecycleConflict("ADAPTER_GENERATION_STALE")

    def record_pending(
        self,
        instance_id: str,
        *,
        generation: int,
        operation_id: str,
        unknown: bool,
    ) -> None:
        self._require_generation(instance_id, generation)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO adapter_channel_pending VALUES (?,?,?,?)",
                (instance_id, generation, operation_id, int(unknown)),
            )
            self.connection.execute(
                "UPDATE adapter_channel_lifecycle SET state='degraded' WHERE instance_id=?",
                (instance_id,),
            )

    def health(self, instance_id: str) -> AdapterHealth:
        row = self.connection.execute(
            "SELECT state,capabilities_revision FROM adapter_channel_lifecycle WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            raise LifecycleConflict("ADAPTER_INSTANCE_NOT_FOUND")
        pending, unknown = self.connection.execute(
            "SELECT count(*),coalesce(sum(unknown),0) FROM adapter_channel_pending "
            "WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        reasons = ("retained-unknown",) if int(unknown) else ()
        return AdapterHealth(
            state=str(row[0]),  # type: ignore[arg-type]
            capabilities_revision=str(row[1]),
            pending_count=int(pending),
            unknown_count=int(unknown),
            reason_codes=reasons,
        )

    def close(
        self, instance_id: str, *, generation: int, deadline_ms: int
    ) -> AdapterCloseOutcome:
        del deadline_ms
        self._require_generation(instance_id, generation)
        pending, unknown = self.connection.execute(
            "SELECT count(*),coalesce(sum(unknown),0) FROM adapter_channel_pending "
            "WHERE instance_id=? AND generation=?",
            (instance_id, generation),
        ).fetchone()
        with self.connection:
            self.connection.execute(
                "UPDATE adapter_channel_lifecycle SET state='stopped',admission_open=0 "
                "WHERE instance_id=? AND generation=?",
                (instance_id, generation),
            )
        retained = int(pending)
        return AdapterCloseOutcome(
            state="retained-pending" if retained else "closed",
            retained_ingress=0,
            retained_deliveries=retained,
            reason_code="retained-unknown" if int(unknown) else None,
        )


__all__ = ["ChannelLifecycle", "LifecycleConflict"]
