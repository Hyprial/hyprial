"""Private instance-scoped durable journal and effect ledger for the Tryx child."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from hyprial.kernel import lock_exclusive, unlock


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;  -- durability: FULL -- preserve the WhatsApp effect ledger across power loss
CREATE TABLE IF NOT EXISTS native_events(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(message_id, payload_json)
);
CREATE TABLE IF NOT EXISTS checkpoints(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  sequence INTEGER NOT NULL,
  journal_id TEXT NOT NULL,
  envelope_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions(
  node_id TEXT PRIMARY KEY,
  request_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('unknown','accepted','failed-final','failed-retryable')),
  provider_message_id TEXT,
  definitely_not_sent INTEGER NOT NULL CHECK(definitely_not_sent IN (0,1)),
  reason_code TEXT
);
CREATE TABLE IF NOT EXISTS metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effect_fence(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  instance_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  account_jid TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','blocked')),
  reason_code TEXT
);
"""


class NativeStore:
    def __init__(self, directory: Path, *, writer_descriptor: int | None = None) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "hyprial-whatsapp.sqlite3"
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            self._db.executescript(_SCHEMA)
        except BaseException:
            self._db.close()
            raise
        self._guard = threading.RLock()
        self._writer_descriptor = writer_descriptor

    @classmethod
    def for_worker_session(cls, session_dir: Path) -> NativeStore:
        durable = cls.durable_directory(session_dir)
        durable.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = durable / ".writer.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            lock_exclusive(descriptor, blocking=False)
        except BaseException:
            os.close(descriptor)
            raise
        target = durable / "hyprial-whatsapp.sqlite3"
        try:
            if not target.exists():
                cls._migrate_legacy_database(session_dir, target)
            return cls(durable, writer_descriptor=descriptor)
        except BaseException:
            unlock(descriptor)
            os.close(descriptor)
            raise

    @staticmethod
    def durable_directory(session_dir: Path) -> Path:
        if session_dir.name != "native-session" or not session_dir.parent.name.isdigit():
            raise ValueError("worker session path is not generation scoped")
        return session_dir.parent.parent / "durable"

    @classmethod
    def migrate_named_database(cls, session_dir: Path, name: str) -> Path:
        durable = cls.durable_directory(session_dir)
        target = durable / name
        if not target.exists():
            cls._migrate_legacy_database(session_dir, target, name=name)
        return target

    @classmethod
    def _migrate_legacy_database(
        cls,
        session_dir: Path,
        target: Path,
        *,
        name: str = "hyprial-whatsapp.sqlite3",
    ) -> None:
        instance_dir = cls.durable_directory(session_dir).parent
        candidates = sorted(
            (
                path / "native-session" / name
                for path in instance_dir.iterdir()
                if path.is_dir() and path.name.isdigit()
            ),
            key=lambda path: int(path.parents[1].name),
            reverse=True,
        ) if instance_dir.exists() else []
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_db = sqlite3.connect(source)
        target_db = sqlite3.connect(target)
        try:
            source_db.backup(target_db)
        except BaseException:
            target_db.close()
            target.unlink(missing_ok=True)
            raise
        finally:
            source_db.close()
            try:
                target_db.close()
            except BaseException:
                pass

    def append_event(self, payload: Mapping[str, object]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        message_id = payload.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("native event requires message id")
        with self._guard:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR IGNORE INTO native_events(message_id,payload_json) VALUES(?,?)",
                    (message_id, encoded),
                )
                row = self._db.execute(
                    "SELECT sequence FROM native_events WHERE message_id=? AND payload_json=?",
                    (message_id, encoded),
                ).fetchone()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return f"wa:{row[0]}"

    def history(self, after: str | None, limit: int) -> list[dict[str, object]]:
        sequence = self._cursor(after) if after else 0
        with self._guard:
            rows = self._db.execute(
                "SELECT sequence,payload_json FROM native_events WHERE sequence>? ORDER BY sequence LIMIT ?",
                (sequence, limit),
            ).fetchall()
        return [
            {"cursor": f"wa:{row[0]}", "event": json.loads(row[1])} for row in rows
        ]

    def commit_checkpoint(self, cursor: str, journal_id: str, digest: str) -> bool:
        sequence = self._cursor(cursor)
        with self._guard:
            current = self._db.execute(
                "SELECT sequence,journal_id,envelope_digest FROM checkpoints WHERE singleton=1"
            ).fetchone()
            if current and sequence < current[0]:
                return False
            if current and sequence == current[0]:
                return (journal_id, digest) == (current[1], current[2])
            self._db.execute(
                "INSERT INTO checkpoints(singleton,sequence,journal_id,envelope_digest) VALUES(1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence,journal_id=excluded.journal_id,envelope_digest=excluded.envelope_digest",
                (sequence, journal_id, digest),
            )
        return True

    def begin_submission(self, node_id: str, request_digest: str) -> dict[str, object] | None:
        with self._guard:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT request_digest,state,provider_message_id,definitely_not_sent,reason_code FROM submissions WHERE node_id=?",
                    (node_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != request_digest:
                        raise ValueError("submission node replay changed request")
                    self._db.execute("COMMIT")
                    return self._submission(row[1:])
                self._db.execute(
                    "INSERT INTO submissions VALUES(?,?, 'unknown',NULL,0,'WHATSAPP_SUBMIT_OUTCOME_UNKNOWN')",
                    (node_id, request_digest),
                )
                self._db.execute("COMMIT")
                return None
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def settle_submission(
        self,
        node_id: str,
        state: str,
        provider_message_id: str | None,
        definitely_not_sent: bool,
        reason_code: str | None,
    ) -> dict[str, object]:
        with self._guard:
            self._db.execute(
                "UPDATE submissions SET state=?,provider_message_id=?,definitely_not_sent=?,reason_code=? WHERE node_id=?",
                (state, provider_message_id, int(definitely_not_sent), reason_code, node_id),
            )
        return self.submission(node_id)

    def note_submission_candidate(self, node_id: str, provider_message_id: str) -> None:
        if not provider_message_id:
            raise ValueError("native submission candidate must not be empty")
        with self._guard:
            changed = self._db.execute(
                "UPDATE submissions SET provider_message_id=? WHERE node_id=? AND state='unknown'",
                (provider_message_id, node_id),
            ).rowcount
        if changed != 1:
            raise KeyError(node_id)

    def settle_native_evidence(self, provider_message_id: str, evidence: str) -> bool:
        if evidence not in {"receipt", "echo", "tryx-send-result"}:
            raise ValueError("unsupported native submission evidence")
        with self._guard:
            changed = self._db.execute(
                "UPDATE submissions SET state='accepted',reason_code=NULL,definitely_not_sent=0 "
                "WHERE provider_message_id=? AND state='unknown'",
                (provider_message_id,),
            ).rowcount
        return changed > 0

    def submission(
        self, node_id: str, request_digest: str | None = None
    ) -> dict[str, object]:
        with self._guard:
            row = self._db.execute(
                "SELECT request_digest,state,provider_message_id,definitely_not_sent,reason_code "
                "FROM submissions WHERE node_id=?",
                (node_id,),
            ).fetchone()
        if row is None:
            raise KeyError(node_id)
        if request_digest is not None and row[0] != request_digest:
            raise ValueError("submission node replay changed request")
        return self._submission(row[1:])

    def set_metadata(self, key: str, value: str) -> None:
        with self._guard:
            self._db.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def metadata(self, key: str) -> str | None:
        with self._guard:
            row = self._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def activate_effects(
        self,
        *,
        instance_id: str,
        generation: int,
        account_jid: str,
    ) -> str | None:
        with self._guard:
            row = self._db.execute(
                "SELECT instance_id,generation,account_jid,state,reason_code "
                "FROM effect_fence WHERE singleton=1"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO effect_fence VALUES(1,?,?,?,'active',NULL)",
                    (instance_id, generation, account_jid),
                )
                return None
            if row[0] != instance_id or row[2] != account_jid:
                return "WHATSAPP_EFFECT_SCOPE_MISMATCH"
            if row[3] == "blocked":
                return str(row[4] or "WHATSAPP_EFFECT_FENCED")
            if generation < int(row[1]):
                return "WHATSAPP_EFFECT_GENERATION_STALE"
            self._db.execute(
                "UPDATE effect_fence SET generation=? WHERE singleton=1",
                (generation,),
            )
        return None

    def fence_effects(
        self,
        *,
        instance_id: str,
        generation: int,
        account_jid: str,
        reason_code: str,
    ) -> None:
        if not reason_code:
            raise ValueError("effect fence reason must not be empty")
        with self._guard:
            row = self._db.execute(
                "SELECT instance_id,generation,account_jid FROM effect_fence WHERE singleton=1"
            ).fetchone()
            if row is not None and (
                row[0] != instance_id or row[2] != account_jid or generation < int(row[1])
            ):
                raise ValueError("effect fence scope mismatch")
            self._db.execute(
                "INSERT INTO effect_fence VALUES(1,?,?,?,'blocked',?) "
                "ON CONFLICT(singleton) DO UPDATE SET generation=excluded.generation,"
                "state='blocked',reason_code=excluded.reason_code",
                (instance_id, generation, account_jid, reason_code),
            )

    def effect_fence(
        self,
        *,
        instance_id: str,
        generation: int,
        account_jid: str,
    ) -> str | None:
        with self._guard:
            row = self._db.execute(
                "SELECT instance_id,generation,account_jid,state,reason_code "
                "FROM effect_fence WHERE singleton=1"
            ).fetchone()
        if row is None:
            return "WHATSAPP_EFFECT_NOT_ACTIVATED"
        if row[0] != instance_id or row[2] != account_jid:
            return "WHATSAPP_EFFECT_SCOPE_MISMATCH"
        if row[3] == "blocked":
            return str(row[4] or "WHATSAPP_EFFECT_FENCED")
        if generation != int(row[1]):
            return "WHATSAPP_EFFECT_GENERATION_STALE"
        return None

    def close(self) -> None:
        with self._guard:
            self._db.close()
            descriptor, self._writer_descriptor = self._writer_descriptor, None
            if descriptor is not None:
                try:
                    unlock(descriptor)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _cursor(value: str) -> int:
        if not value.startswith("wa:") or not value[3:].isdigit():
            raise ValueError("invalid WhatsApp cursor")
        return int(value[3:])

    @staticmethod
    def _submission(row: tuple[object, ...]) -> dict[str, object]:
        state = row[0]
        return {
            "partIndex": 0,
            "state": state,
            "providerMessageId": row[1] if state == "accepted" else None,
            "definitelyNotSent": bool(row[2]),
            "reasonCode": row[3],
        }


__all__ = ["NativeStore"]
