"""Typed transaction outbox. Only flag_set/flag_reset describe flag mutations.

Business tables remain authoritative for pending work. This append-only journal
orders facts for subscribers; transport acknowledgement is not task completion.
Lifecycle types reserve the envelope, not a lifecycle implementation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .errors import PAC_EVENT_ENVELOPE_INVALID, PAC_EVENT_TYPE_UNKNOWN, PacError

EVENT_TYPES = (
    "flag_set", "flag_reset", "notification_planned", "delivery_changed",
    "structure_changed", "graph_activated", "graph_closed", "launch_failed",
    "actor_up", "actor_down", "actor_restored", "actor_lost", "actor_unowned",
    "migration_baseline", "workflow_changed",
)

JOURNAL_SCHEMA = f"""
CREATE TABLE journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id),
    version INTEGER NOT NULL,
    type TEXT NOT NULL CHECK(type IN ({','.join(repr(t) for t in EVENT_TYPES)})),
    at INTEGER NOT NULL,
    data_json TEXT NOT NULL
)
"""


def require_event_type(kind: str) -> None:
    if not isinstance(kind, str):
        raise PacError(PAC_EVENT_ENVELOPE_INVALID, "event type must be a string")
    if kind not in EVENT_TYPES:
        raise PacError(PAC_EVENT_TYPE_UNKNOWN, f"unknown PAC event type: {kind}")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Validated tagged envelope; payload is data, never an implicit flag action.

    Lifecycle payloads remain opaque/reserved until the real lifecycle adapter
    exists. This type deliberately does not invent start/down response shapes.
    """

    seq: int
    event_id: str
    graph_id: str
    version: int
    type: str
    at: int
    data: dict[str, Any]
    journal_id: str | None = None

    def __post_init__(self) -> None:
        require_event_type(self.type)

    @classmethod
    def from_json(cls, document: dict[str, Any]) -> EventEnvelope:
        if not isinstance(document, dict):
            raise PacError(PAC_EVENT_ENVELOPE_INVALID, "event envelope must be a JSON object")
        kind = document.get("type")
        require_event_type(kind)
        integers = ("seq", "version", "at")
        strings = ("eventId", "graphId")
        valid = (
            document.get("schemaVersion") == 1
            and not isinstance(document.get("schemaVersion"), bool)
            and all(isinstance(document.get(key), int) and not isinstance(document[key], bool) for key in integers)
            and document["seq"] > 0 and document["version"] > 0
            and all(isinstance(document.get(key), str) and document[key] for key in strings)
            and isinstance(document.get("data"), dict)
            and ("journalId" not in document or
                 (isinstance(document["journalId"], str) and bool(document["journalId"])))
        )
        if not valid:
            raise PacError(PAC_EVENT_ENVELOPE_INVALID, "invalid PAC event envelope")
        return cls(seq=document["seq"], event_id=document["eventId"], graph_id=document["graphId"],
                   version=document["version"], type=kind, at=document["at"],
                   data=document["data"], journal_id=document.get("journalId"))

    def to_json(self) -> dict[str, Any]:
        return {"schemaVersion": 1, "seq": self.seq, "eventId": self.event_id,
                "graphId": self.graph_id, "version": self.version, "type": self.type,
                "at": self.at, "data": self.data,
                **({"journalId": self.journal_id} if self.journal_id is not None else {})}


def activation_id(graph_id: str, node_id: str, round_no: int | None) -> str:
    """Opaque assignment identity: repeated requests in one round coalesce."""
    return str(uuid5(NAMESPACE_URL, json.dumps(["pac-assignment", graph_id, node_id, round_no])))


def append_event(
    db: sqlite3.Connection, *, graph_id: str, version: int, type: str,
    at: int, data: dict[str, Any], event_id: str | None = None,
) -> int:
    """Append inside the caller's business transaction; never commit here."""
    if not db.in_transaction:
        raise RuntimeError("journal writes require a business transaction")
    require_event_type(type)
    if not isinstance(data, dict):
        raise PacError(PAC_EVENT_ENVELOPE_INVALID, "event payload must be a JSON object")
    cursor = db.execute(
        "INSERT INTO journal (event_id, graph_id, version, type, at, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id or str(uuid4()), graph_id, version, type, at,
         json.dumps(data, ensure_ascii=False, sort_keys=True)),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def envelope(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["data_json"])
    except (TypeError, ValueError):
        raise PacError(PAC_EVENT_ENVELOPE_INVALID, "event payload is not valid JSON") from None
    return EventEnvelope.from_json({
        "schemaVersion": 1, "seq": row["seq"], "eventId": row["event_id"],
        "graphId": row["graph_id"], "version": row["version"],
        "type": row["type"], "at": row["at"], "data": data,
    }).to_json()
