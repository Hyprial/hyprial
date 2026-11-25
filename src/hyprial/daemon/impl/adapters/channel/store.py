"""SQLite custody for canonical chat ingress and native aliases."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from hyprial.daemon.impl.adapters.contracts import (
    CustodyReceipt,
    IngressObservation,
    NativeAlias,
    SourceProvenance,
)


_EVENT_NAMESPACE = uuid.UUID("a2a6e7f4-2c2d-4bfb-a7c8-4513911b38e4")
_ALIAS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS native_alias_claims (
    platform TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    instance_generation INTEGER NOT NULL,
    account_scope TEXT NOT NULL,
    chat_scope TEXT NOT NULL,
    platform_resource TEXT NOT NULL,
    semantic_kind TEXT NOT NULL,
    alias_kind TEXT NOT NULL,
    normalized_id TEXT NOT NULL,
    normalized_revision TEXT NOT NULL,
    ingress_id TEXT NOT NULL REFERENCES ingress_records(ingress_id),
    PRIMARY KEY(platform,instance_id,instance_generation,account_scope,chat_scope,
                platform_resource,semantic_kind,alias_kind,normalized_id,
                normalized_revision)
)"""


class ChannelConflict(ValueError):
    pass


class ChannelStoreFault(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngressCapture:
    ingress_id: str
    canonical_event_id: str
    state: str
    observation_count: int
    receipt: CustodyReceipt


@dataclass(frozen=True, slots=True)
class CanonicalCommit:
    canonical_event_id: str
    replay: bool


@dataclass(frozen=True, slots=True)
class QuarantineResolution:
    quarantine_id: str
    ingress_id: str
    state: str
    version: int
    replay: bool


def _alias_key(alias: NativeAlias) -> tuple[object, ...]:
    return (
        alias.platform or "",
        alias.instance_id,
        alias.instance_generation,
        alias.account_scope,
        alias.chat_scope or "",
        alias.provider_resource,
        alias.semantic_kind,
        alias.alias_kind,
        alias.normalized_id,
        alias.normalized_revision,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class ChannelStore:
    """The single daemon writer for ingress custody and alias ownership."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._initialize()

    def _initialize(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS ingress_records (
                ingress_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL UNIQUE,
                instance_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('journaled','quarantined','canonical')),
                envelope_digest TEXT NOT NULL,
                raw_bytes BLOB NOT NULL,
                observation_count INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ingress_observations (
                observation_id TEXT PRIMARY KEY,
                ingress_id TEXT NOT NULL REFERENCES ingress_records(ingress_id),
                observation_digest TEXT NOT NULL,
                raw_safe_ref TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                received_at_ms INTEGER NOT NULL
            )""",
            _ALIAS_TABLE_SQL,
            """CREATE TABLE IF NOT EXISTS ingress_quarantine (
                quarantine_id TEXT PRIMARY KEY,
                ingress_id TEXT NOT NULL UNIQUE REFERENCES ingress_records(ingress_id),
                state TEXT NOT NULL CHECK(state IN (
                    'pending','conflict','correlated-echo','released-human','rejected'
                )),
                version INTEGER NOT NULL,
                reason TEXT NOT NULL,
                evidence_digest TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS canonicalize_outbox (
                ingress_id TEXT PRIMARY KEY REFERENCES ingress_records(ingress_id),
                operation_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('pending','settled'))
            )""",
            """CREATE TABLE IF NOT EXISTS canonical_chat_events (
                ingress_id TEXT PRIMARY KEY REFERENCES ingress_records(ingress_id),
                canonical_event_id TEXT NOT NULL UNIQUE,
                provenance_bytes BLOB NOT NULL,
                provenance_digest TEXT NOT NULL
            )""",
        )
        for statement in statements:
            self.connection.execute(statement)
        columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(native_alias_claims)"
            ).fetchall()
        }
        if "platform" not in columns or "chat_scope" not in columns:
            self.connection.execute(
                "ALTER TABLE native_alias_claims RENAME TO native_alias_claims_legacy"
            )
            self.connection.execute(_ALIAS_TABLE_SQL)
            legacy_rows = self.connection.execute(
                "SELECT * FROM native_alias_claims_legacy"
            )
            self.connection.executemany(
                "INSERT INTO native_alias_claims (platform,instance_id,"
                "instance_generation,account_scope,chat_scope,platform_resource,"
                "semantic_kind,alias_kind,normalized_id,normalized_revision,ingress_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ("", row[0], row[1], row[2], "", *row[3:])
                    for row in legacy_rows
                ),
            )
            self.connection.execute("DROP TABLE native_alias_claims_legacy")
        self.connection.commit()

    def capture(
        self,
        observation: IngressObservation,
        raw_payload: bytes,
        *,
        fail_after_aliases: int | None = None,
    ) -> IngressCapture:
        if not isinstance(raw_payload, bytes):
            raise TypeError("raw_payload must be bytes")
        aliases = tuple(observation.aliases)
        if not aliases:
            raise ChannelConflict("MISSING_STABLE_ALIAS")
        duplicate_keys = [_alias_key(alias) for alias in aliases]
        if len(set(duplicate_keys)) != len(duplicate_keys):
            raise ChannelConflict("DUPLICATE_ALIAS_IN_OBSERVATION")

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT ingress_id,observation_digest FROM ingress_observations "
                "WHERE observation_id=?",
                (observation.observation_id,),
            ).fetchone()
            if prior is not None:
                if str(prior[1]) != observation.observation_digest:
                    raise ChannelConflict("OBSERVATION_REPLAY_CONFLICT")
                result = self._capture_result(str(prior[0]), replay=True)
                self.connection.commit()
                return result

            owners: set[str] = set()
            for key in duplicate_keys:
                row = self.connection.execute(
                    "SELECT ingress_id FROM native_alias_claims WHERE "
                    "platform=? AND instance_id=? AND instance_generation=? AND "
                    "account_scope=? AND chat_scope=? AND platform_resource=? AND "
                    "semantic_kind=? AND alias_kind=? AND normalized_id=? AND "
                    "normalized_revision=?",
                    key,
                ).fetchone()
                if row is not None:
                    owners.add(str(row[0]))

            conflict = len(owners) > 1
            if len(owners) == 1:
                ingress_id = next(iter(owners))
                row = self.connection.execute(
                    "SELECT canonical_event_id,state FROM ingress_records WHERE ingress_id=?",
                    (ingress_id,),
                ).fetchone()
                assert row is not None
                canonical_event_id = str(row[0])
                state = str(row[1])
            else:
                ingress_id = f"ingress:{uuid.uuid4()}"
                canonical_event_id = str(uuid.uuid5(_EVENT_NAMESPACE, ingress_id))
                stable = any(alias.alias_kind != "manual-release" for alias in aliases)
                state = "quarantined" if conflict or not stable else "journaled"
                envelope_digest = hashlib.sha256(
                    b"hyprial-ingress-envelope-v1\0"
                    + observation.observation_digest.encode("utf-8")
                    + b"\0"
                    + raw_payload
                ).hexdigest()
                self.connection.execute(
                    "INSERT INTO ingress_records VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ingress_id,
                        canonical_event_id,
                        aliases[0].instance_id,
                        1,
                        state,
                        envelope_digest,
                        raw_payload,
                        0,
                        observation.received_at_ms,
                    ),
                )

            self.connection.execute(
                "INSERT INTO ingress_observations VALUES (?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    ingress_id,
                    observation.observation_digest,
                    observation.raw_safe_ref,
                    observation.source_kind,
                    observation.received_at_ms,
                ),
            )
            inserted = 0
            for alias, key in zip(aliases, duplicate_keys, strict=True):
                if self.alias_owner(alias) is None:
                    self.connection.execute(
                        "INSERT INTO native_alias_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (*key, ingress_id),
                    )
                    inserted += 1
                    if fail_after_aliases is not None and inserted >= fail_after_aliases:
                        raise ChannelStoreFault("injected-alias-write-fault")

            self.connection.execute(
                "UPDATE ingress_records SET observation_count=observation_count+1, "
                "version=version+1 WHERE ingress_id=?",
                (ingress_id,),
            )
            if state == "quarantined":
                reason = "alias-conflict" if conflict else "missing-stable-alias"
                evidence = hashlib.sha256(
                    _canonical({"owners": sorted(owners), "aliases": duplicate_keys})
                ).hexdigest()
                self.connection.execute(
                    "INSERT OR IGNORE INTO ingress_quarantine VALUES (?,?, 'pending',1,?,?)",
                    (f"quarantine:{ingress_id}", ingress_id, reason, evidence),
                )
            else:
                self.connection.execute(
                    "INSERT OR IGNORE INTO canonicalize_outbox VALUES (?,?,'pending')",
                    (ingress_id, f"canonicalize:{ingress_id}"),
                )
            result = self._capture_result(ingress_id, replay=False)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def _capture_result(self, ingress_id: str, *, replay: bool) -> IngressCapture:
        row = self.connection.execute(
            "SELECT canonical_event_id,state,envelope_digest,observation_count,created_at_ms "
            "FROM ingress_records WHERE ingress_id=?",
            (ingress_id,),
        ).fetchone()
        if row is None:
            raise ChannelConflict("INGRESS_NOT_FOUND")
        receipt = CustodyReceipt(
            journal_id=ingress_id,
            envelope_digest=str(row[2]),
            committed_at_ms=int(row[4]),
        )
        return IngressCapture(
            ingress_id=ingress_id,
            canonical_event_id=str(row[0]),
            state=str(row[1]),
            observation_count=int(row[3]),
            receipt=receipt,
        )

    def alias_owner(self, alias: NativeAlias) -> str | None:
        row = self.connection.execute(
            "SELECT ingress_id FROM native_alias_claims WHERE "
            "platform=? AND instance_id=? AND instance_generation=? AND "
            "account_scope=? AND chat_scope=? AND platform_resource=? AND "
            "semantic_kind=? AND alias_kind=? AND normalized_id=? AND "
            "normalized_revision=?",
            _alias_key(alias),
        ).fetchone()
        return None if row is None else str(row[0])

    def resolve_native_parent(
        self,
        canonical_event_id: str,
        *,
        platform: str,
        instance_id: str,
        instance_generation: int,
        account_scope: str,
        native_chat_id: str,
    ) -> str | None:
        """Resolve one canonical parent through its generation-fenced message alias."""

        rows = self.connection.execute(
            "SELECT a.normalized_id FROM native_alias_claims a "
            "JOIN ingress_records i ON i.ingress_id=a.ingress_id "
            "WHERE i.canonical_event_id=? AND i.state!='quarantined' "
            "AND a.platform=? AND a.instance_id=? AND a.instance_generation=? "
            "AND a.account_scope=? AND a.chat_scope=? AND a.semantic_kind='create' "
            "AND a.alias_kind='message'",
            (
                canonical_event_id,
                platform,
                instance_id,
                instance_generation,
                account_scope,
                native_chat_id,
            ),
        ).fetchall()
        if len(rows) != 1:
            return None
        value = str(rows[0][0])
        return value or None

    def commit_canonical(
        self, ingress_id: str, provenance: SourceProvenance
    ) -> CanonicalCommit:
        if provenance.ingress_id != ingress_id:
            raise ChannelConflict("CANONICAL_PROVENANCE_INGRESS_MISMATCH")
        payload = _canonical(dataclasses.asdict(provenance))
        digest = hashlib.sha256(b"hyprial-chat-provenance-v1\0" + payload).hexdigest()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT canonical_event_id,provenance_digest FROM canonical_chat_events "
                "WHERE ingress_id=?",
                (ingress_id,),
            ).fetchone()
            if row is not None:
                if str(row[1]) != digest:
                    raise ChannelConflict("CANONICAL_PROVENANCE_CONFLICT")
                self.connection.commit()
                return CanonicalCommit(str(row[0]), True)
            source = self.connection.execute(
                "SELECT canonical_event_id,state FROM ingress_records WHERE ingress_id=?",
                (ingress_id,),
            ).fetchone()
            if source is None:
                raise ChannelConflict("INGRESS_NOT_FOUND")
            if str(source[1]) == "quarantined":
                raise ChannelConflict("INGRESS_QUARANTINED")
            self.connection.execute(
                "INSERT INTO canonical_chat_events VALUES (?,?,?,?)",
                (ingress_id, str(source[0]), payload, digest),
            )
            self.connection.execute(
                "UPDATE ingress_records SET state='canonical',version=version+1 WHERE ingress_id=?",
                (ingress_id,),
            )
            self.connection.execute(
                "UPDATE canonicalize_outbox SET state='settled' WHERE ingress_id=?",
                (ingress_id,),
            )
            self.connection.commit()
            return CanonicalCommit(str(source[0]), False)
        except BaseException:
            self.connection.rollback()
            raise

    def resolve_quarantine(
        self,
        quarantine_id: str,
        *,
        expected_version: int,
        action: str,
        evidence_digest: str,
    ) -> QuarantineResolution:
        if action not in ("correlate-echo", "release-human", "reject"):
            raise ChannelConflict("QUARANTINE_ACTION_INVALID")
        target_state = {
            "correlate-echo": "correlated-echo",
            "release-human": "released-human",
            "reject": "rejected",
        }[action]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT ingress_id,state,version,evidence_digest FROM ingress_quarantine "
                "WHERE quarantine_id=?",
                (quarantine_id,),
            ).fetchone()
            if row is None:
                raise ChannelConflict("QUARANTINE_NOT_FOUND")
            ingress_id, state, version, prior_evidence = (
                str(row[0]), str(row[1]), int(row[2]), str(row[3])
            )
            if version != expected_version:
                raise ChannelConflict("QUARANTINE_VERSION_CONFLICT")
            if state != "pending":
                if state == target_state and prior_evidence == evidence_digest:
                    self.connection.commit()
                    return QuarantineResolution(
                        quarantine_id, ingress_id, state, version, True
                    )
                raise ChannelConflict("QUARANTINE_TERMINAL")
            self.connection.execute(
                "UPDATE ingress_quarantine SET state=?,version=version+1,evidence_digest=? "
                "WHERE quarantine_id=? AND version=? AND state='pending'",
                (target_state, evidence_digest, quarantine_id, expected_version),
            )
            if action == "release-human":
                namespace = self.connection.execute(
                    "SELECT platform,instance_id,instance_generation,account_scope,"
                    "chat_scope,platform_resource "
                    "FROM native_alias_claims WHERE ingress_id=? ORDER BY alias_kind LIMIT 1",
                    (ingress_id,),
                ).fetchone()
                if namespace is None:
                    raise ChannelConflict("INGRESS_NOT_FOUND")
                self.connection.execute(
                    "INSERT OR IGNORE INTO native_alias_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(namespace[0]), str(namespace[1]), int(namespace[2]),
                        str(namespace[3]), str(namespace[4]), str(namespace[5]),
                        "create", "manual-release", quarantine_id, "s:0", ingress_id,
                    ),
                )
                self.connection.execute(
                    "UPDATE ingress_records SET state='journaled',version=version+1 "
                    "WHERE ingress_id=?",
                    (ingress_id,),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO canonicalize_outbox VALUES (?,?,'pending')",
                    (ingress_id, f"canonicalize:{ingress_id}"),
                )
            self.connection.commit()
            return QuarantineResolution(
                quarantine_id, ingress_id, target_state, version + 1, False
            )
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def checkpoint_allowed(receipt: CustodyReceipt, envelope_digest: str) -> bool:
        return receipt.envelope_digest == envelope_digest

    def canonicalize_count(self, ingress_id: str) -> int:
        return int(
            self.connection.execute(
                "SELECT count(*) FROM canonicalize_outbox WHERE ingress_id=?", (ingress_id,)
            ).fetchone()[0]
        )

    def counts(self) -> dict[str, int]:
        tables = {
            "ingress": "ingress_records",
            "observations": "ingress_observations",
            "aliases": "native_alias_claims",
            "canonicalize": "canonicalize_outbox",
            "quarantine": "ingress_quarantine",
        }
        return {
            name: int(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for name, table in tables.items()
        }


__all__ = [
    "CanonicalCommit",
    "ChannelConflict",
    "ChannelStore",
    "ChannelStoreFault",
    "IngressCapture",
    "QuarantineResolution",
]
