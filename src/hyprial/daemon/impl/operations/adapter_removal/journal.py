"""Persist deletion preimages before effects, using the existing StateDatabase.

Prepared operations recover by compensation. After the durable commit boundary
only cleanup may continue; an interrupted process must never replay stop.
This journal stores configuration bytes and registration/pin preimages, but
only a digest of the credential file. It neither performs effects nor admits
business requests. The domain owner must recover before opening adapter use.
Transactions are short and never span process effects or file operations.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import time

from hyprial.daemon.impl.adapters.spec import AdapterSpec
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.operations.management import AdapterRegistrySnapshot
from hyprial.daemon.impl.state_db import StateDatabase


class RemovalPhase(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    COMPENSATED = "compensated"


_TRANSITIONS = frozenset({
    (RemovalPhase.PREPARED, RemovalPhase.COMMITTED),
    (RemovalPhase.PREPARED, RemovalPhase.COMPENSATING),
    (RemovalPhase.COMMITTED, RemovalPhase.COMPLETED),
    (RemovalPhase.COMPENSATING, RemovalPhase.COMPENSATED),
})


class RemovalJournalConflict(RuntimeError):
    """An operation identity, target reservation or phase cannot be replaced."""


@dataclass(frozen=True, slots=True)
class RemovalIntent:
    operation_id: str
    adapter: str
    caller: str
    epoch: str
    target_digest: str
    channels_before: bytes
    channels_after: bytes
    registry: AdapterRegistrySnapshot
    agent_entity_token: str | None
    credential_ref: str
    credential_digest: str | None
    remove_credential: bool

    def encode(self) -> str:
        for name in ("operation_id", "adapter", "caller", "epoch", "credential_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError(f"removal {name} must be non-empty and bounded")
        if self.registry.harness.name != self.adapter:
            raise ValueError("removal registry snapshot does not match the adapter")
        spec = self.registry.harness.spec
        if spec is not None and spec.name != self.adapter:
            raise ValueError("removal registration does not match the adapter")
        for label, digest in (("target", self.target_digest), ("credential", self.credential_digest)):
            if label == "credential" and digest is None:
                continue
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("removal digests must be SHA-256")
        for actor in (self.registry.agent_pin, self.registry.harness.legacy_pin):
            if actor is not None and (not isinstance(actor, str) or not actor or len(actor) > 4096):
                raise ValueError("removal pin preimages must be bounded actor identifiers")
        if self.registry.agent_pin is None:
            if self.agent_entity_token is not None:
                raise ValueError("unbound removal must not carry an Agent entity token")
        elif not isinstance(self.agent_entity_token, str) or not self.agent_entity_token or len(self.agent_entity_token) > 1024:
            raise ValueError("pinned removal requires its Agent entity token")
        if not isinstance(self.remove_credential, bool):
            raise ValueError("removal credential policy must be a boolean")
        if not all(isinstance(value, bytes) for value in (self.channels_before, self.channels_after)):
            raise ValueError("removal channels preimages must be bytes")
        if len(self.channels_before) + len(self.channels_after) > 2 * 1024 * 1024:
            raise ValueError("removal channels preimages exceed the size limit")
        value = {
            "schemaVersion": 1,
            "operationId": self.operation_id, "adapter": self.adapter,
            "caller": self.caller, "epoch": self.epoch, "targetDigest": self.target_digest,
            "channelsBefore": base64.b64encode(self.channels_before).decode("ascii"),
            "channelsAfter": base64.b64encode(self.channels_after).decode("ascii"),
            "registration": None if spec is None else AdapterSpec.from_json(spec.to_json()).to_json(),
            "legacyPin": self.registry.harness.legacy_pin, "agentPin": self.registry.agent_pin,
            "agentEntityToken": self.agent_entity_token,
            "credentialRef": self.credential_ref, "credentialDigest": self.credential_digest,
            "removeCredential": self.remove_credential,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def decode(cls, payload: str) -> RemovalIntent:
        value = json.loads(payload)
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise ValueError("removal intent schema is not supported")
        spec = value["registration"]
        intent = cls(
            operation_id=value["operationId"], adapter=value["adapter"],
            caller=value["caller"], epoch=value["epoch"], target_digest=value["targetDigest"],
            channels_before=base64.b64decode(value["channelsBefore"], validate=True),
            channels_after=base64.b64decode(value["channelsAfter"], validate=True),
            registry=AdapterRegistrySnapshot(HarnessAdapterRegistrationProjection(
                value["adapter"], None if spec is None else AdapterSpec.from_json(spec), value["legacyPin"],
            ), value["agentPin"]),
            agent_entity_token=value["agentEntityToken"],
            credential_ref=value["credentialRef"], credential_digest=value["credentialDigest"],
            remove_credential=value["removeCredential"],
        )
        if intent.encode() != payload:
            raise ValueError("removal intent is not canonical")
        return intent


@dataclass(frozen=True, slots=True)
class RemovalRecord:
    intent: RemovalIntent
    phase: RemovalPhase


_COLUMNS = "operation_id, adapter, request_json, request_digest, phase"
_PENDING = "phase NOT IN ('completed', 'compensated')"


def _record(row) -> RemovalRecord | None:
    if row is None:
        return None
    operation_id, adapter, payload, digest, phase = row
    if hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise ValueError("removal intent checksum mismatch")
    intent = RemovalIntent.decode(payload)
    if intent.operation_id != operation_id or intent.adapter != adapter:
        raise ValueError("removal intent identity mismatch")
    return RemovalRecord(intent, RemovalPhase(phase))


class AdapterRemovalJournal:
    def __init__(self, state_db: StateDatabase, *, capacity: int = 128) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("removal journal capacity must be positive")
        self._state_db = state_db
        self._capacity = capacity
        with state_db.transaction(schema="""
            CREATE TABLE IF NOT EXISTS adapter_removal_intents (
                operation_id TEXT PRIMARY KEY,
                adapter TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('prepared', 'committed', 'compensating', 'completed', 'compensated')),
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS adapter_removal_active_target
                ON adapter_removal_intents(adapter) WHERE phase NOT IN ('completed', 'compensated');
        """):
            pass

    def prepare(self, intent: RemovalIntent) -> RemovalRecord:
        payload = intent.encode()
        intent = RemovalIntent.decode(payload)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._state_db.transaction() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM adapter_removal_intents WHERE operation_id = ?",
                (intent.operation_id,),
            ).fetchone()
            existing = _record(row)
            if existing is not None:
                if existing.intent.encode() != payload:
                    raise RemovalJournalConflict("removal operation has different bound inputs")
                return existing
            target = connection.execute(
                f"SELECT operation_id FROM adapter_removal_intents WHERE adapter = ? AND {_PENDING}",
                (intent.adapter,),
            ).fetchone()
            if target is not None:
                raise RemovalJournalConflict("adapter has an unsettled removal")
            count = connection.execute(
                f"SELECT count(*) FROM adapter_removal_intents WHERE {_PENDING}",
            ).fetchone()[0]
            if count >= self._capacity:
                raise RemovalJournalConflict("removal intent capacity is full")
            connection.execute(
                "INSERT INTO adapter_removal_intents VALUES (?, ?, ?, ?, ?, ?)",
                (intent.operation_id, intent.adapter, payload, digest, RemovalPhase.PREPARED.value, time.time()),
            )
        return RemovalRecord(intent, RemovalPhase.PREPARED)

    def get(self, operation_id: str) -> RemovalRecord | None:
        with self._state_db.read() as connection:
            return _record(connection.execute(
                f"SELECT {_COLUMNS} FROM adapter_removal_intents WHERE operation_id = ?", (operation_id,),
            ).fetchone())

    def pending(self) -> tuple[RemovalRecord, ...]:
        with self._state_db.read() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM adapter_removal_intents WHERE {_PENDING} ORDER BY rowid",
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def advance(self, operation_id: str, expected: RemovalPhase, target: RemovalPhase) -> RemovalRecord:
        if not isinstance(expected, RemovalPhase) or not isinstance(target, RemovalPhase) or (expected, target) not in _TRANSITIONS:
            raise RemovalJournalConflict("removal phase transition is not permitted")
        with self._state_db.transaction() as connection:
            current = _record(connection.execute(
                f"SELECT {_COLUMNS} FROM adapter_removal_intents WHERE operation_id = ?", (operation_id,),
            ).fetchone())
            if current is None:
                raise RemovalJournalConflict("removal intent is missing")
            if current.phase is target:
                return current
            if current.phase is not expected:
                raise RemovalJournalConflict("removal phase changed")
            connection.execute(
                "UPDATE adapter_removal_intents SET phase = ?, updated_at = ? WHERE operation_id = ? AND phase = ?",
                (target.value, time.time(), operation_id, expected.value),
            )
        return RemovalRecord(current.intent, target)
