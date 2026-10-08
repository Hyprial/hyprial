"""Frozen service-connect desired-state records and SQLite helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hyprial.kernel import DesiredStateError

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")

_SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_connections (
    name TEXT PRIMARY KEY,
    local_port INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_registry (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    space_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    cached_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_registry_entries (
    ordinal INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    device_id TEXT NOT NULL,
    remote_port INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    local_port INTEGER,
    usage TEXT,
    env_json TEXT NOT NULL,
    auth TEXT NOT NULL,
    guide TEXT NOT NULL
);
"""


def _record(value: object, label: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise DesiredStateError(f"{label} must be an object")
    actual = set(value)
    missing = keys - actual
    unknown = actual - keys
    if missing or unknown:
        raise DesiredStateError(
            f"{label} fields mismatch; missing={sorted(missing)!r}, unknown={sorted(unknown)!r}"
        )
    return dict(value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DesiredStateError(f"{label} must be a non-empty string")
    return value


def _name(value: object, label: str) -> str:
    value = _string(value, label)
    if _NAME.fullmatch(value) is None:
        raise DesiredStateError(f"{label} must be a safe service name")
    return value


def _port(value: object, label: str, *, allow_zero: bool = True) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    lower = 0 if allow_zero else 1
    if not lower <= value <= 65535:
        raise ValueError(f"{label} must be {lower}..65535")
    return value


def _local_entry_port(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer or null")
    if not 1024 <= value <= 65535:
        raise ValueError(f"{label} must be 1024..65535")
    return value


@dataclass(frozen=True, slots=True)
class ServiceConnection:
    name: str
    local_port: int

    @classmethod
    def from_json(cls, value: object, label: str = "service connection") -> "ServiceConnection":
        record = _record(value, label, frozenset({"name", "localPort"}))
        return cls(_name(record["name"], f"{label}.name"), _port(record["localPort"], f"{label}.localPort"))

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "localPort": self.local_port}


@dataclass(frozen=True, slots=True)
class ServiceRegistryEntry:
    name: str
    device_id: str
    remote_port: int
    protocol: str
    local_port: int | None
    usage: str | None
    env: tuple[tuple[str, str], ...]
    auth: str
    guide: str

    @classmethod
    def from_json(cls, value: object, label: str = "service registry entry") -> "ServiceRegistryEntry":
        fields = frozenset({"name", "deviceId", "remotePort", "protocol", "localPort", "usage", "env", "auth", "guide"})
        record = _record(value, label, fields)
        if type(record["remotePort"]) is not int or not 1 <= record["remotePort"] <= 65535:
            raise ValueError(f"{label}.remotePort must be 1..65535")
        if record["protocol"] not in {"tcp", "http"}:
            raise ValueError(f"{label}.protocol must be tcp or http")
        usage = record["usage"]
        if usage is not None and not isinstance(usage, str):
            raise TypeError(f"{label}.usage must be a string or null")
        env = record["env"]
        if not isinstance(env, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise TypeError(f"{label}.env must be a string mapping")
        return cls(
            _name(record["name"], f"{label}.name"),
            _name(record["deviceId"], f"{label}.deviceId"),
            record["remotePort"],
            record["protocol"],
            _local_entry_port(record["localPort"], f"{label}.localPort"),
            usage,
            tuple(sorted(env.items())),
            _string(record["auth"], f"{label}.auth"),
            _string(record["guide"], f"{label}.guide"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "deviceId": self.device_id,
            "remotePort": self.remote_port,
            "protocol": self.protocol,
            "localPort": self.local_port,
            "usage": self.usage,
            "env": dict(self.env),
            "auth": self.auth,
            "guide": self.guide,
        }


@dataclass(frozen=True, slots=True)
class ServiceRegistry:
    schema_version: int
    space_id: str
    owner: str
    cached_at_ms: int = field(compare=False)
    entries: tuple[ServiceRegistryEntry, ...]

    @classmethod
    def from_json(cls, value: object, label: str = "serviceRegistry") -> "ServiceRegistry":
        record = _record(value, label, frozenset({"schemaVersion", "spaceId", "owner", "cachedAtMs", "entries"}))
        if record["schemaVersion"] != 1:
            raise ValueError(f"{label}.schemaVersion must be 1")
        if type(record["cachedAtMs"]) is not int or record["cachedAtMs"] < 0:
            raise TypeError(f"{label}.cachedAtMs must be a non-negative integer")
        entries = record["entries"]
        if not isinstance(entries, list):
            raise TypeError(f"{label}.entries must be an array")
        parsed = tuple(ServiceRegistryEntry.from_json(item, f"{label}.entries[{i}]") for i, item in enumerate(entries))
        if len({entry.name for entry in parsed}) != len(parsed):
            raise ValueError(f"{label}.entries contains duplicate names")
        return cls(1, _string(record["spaceId"], f"{label}.spaceId"), _string(record["owner"], f"{label}.owner"), record["cachedAtMs"], parsed)

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "spaceId": self.space_id,
            "owner": self.owner,
            "cachedAtMs": self.cached_at_ms,
            "entries": [entry.to_json() for entry in self.entries],
        }


ServiceRegistryCache = ServiceRegistry


def _tables_exist(db: sqlite3.Connection) -> bool:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('service_connections','service_registry','service_registry_entries')"
    ).fetchall()
    return len(rows) == 3


def read_service_state(db: sqlite3.Connection, document: dict[str, Any]) -> None:
    if not _tables_exist(db):
        return
    connections = [
        {"name": row["name"], "localPort": row["local_port"]}
        for row in db.execute("SELECT name, local_port FROM service_connections ORDER BY name")
    ]
    if connections:
        document["serviceConnections"] = connections
    registry = db.execute("SELECT * FROM service_registry WHERE id=1").fetchone()
    if registry is None:
        return
    entries = []
    for row in db.execute("SELECT * FROM service_registry_entries ORDER BY ordinal"):
        entries.append({
            "name": row["name"], "deviceId": row["device_id"], "remotePort": row["remote_port"],
            "protocol": row["protocol"], "localPort": row["local_port"], "usage": row["usage"],
            "env": json.loads(row["env_json"]), "auth": row["auth"], "guide": row["guide"],
        })
    document["serviceRegistry"] = {
        "schemaVersion": registry["schema_version"], "spaceId": registry["space_id"],
        "owner": registry["owner"], "cachedAtMs": registry["cached_at_ms"], "entries": entries,
    }


def delete_service_state(db: sqlite3.Connection) -> None:
    for table in ("service_connections", "service_registry", "service_registry_entries"):
        db.execute(f'DELETE FROM "{table}"')


def insert_service_state(db: sqlite3.Connection, document: Mapping[str, Any]) -> None:
    for connection in document.get("serviceConnections", []):
        db.execute("INSERT INTO service_connections(name, local_port) VALUES(?, ?)", (connection["name"], connection["localPort"]))
    registry = document.get("serviceRegistry")
    if registry is None:
        return
    db.execute(
        "INSERT INTO service_registry(id, schema_version, space_id, owner, cached_at_ms) VALUES(1, ?, ?, ?, ?)",
        (registry["schemaVersion"], registry["spaceId"], registry["owner"], registry["cachedAtMs"]),
    )
    db.executemany(
        "INSERT INTO service_registry_entries(ordinal, name, device_id, remote_port, protocol, local_port, usage, env_json, auth, guide) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (ordinal, entry["name"], entry["deviceId"], entry["remotePort"], entry["protocol"], entry["localPort"], entry["usage"], json.dumps(entry["env"], sort_keys=True, separators=(",", ":")), entry["auth"], entry["guide"])
            for ordinal, entry in enumerate(registry["entries"])
        ],
    )
