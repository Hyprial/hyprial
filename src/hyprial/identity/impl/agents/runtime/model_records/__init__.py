"""Atomic user Secret values and owner-selected model service routing.

The reserved schema marker cannot be a legacy field name. A configured record
is read and replaced under one process lock; no second file can disagree with
the value. Grant route_required is checked separately at each resolution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .connections import (
    ConnectionConflict, InvalidConnection as InvalidConnection, ModelConnection, configuration, integer,
    validate_endpoint, validate_key,
)

from hyprial.kernel import ProcessFileLock, read_private_file, write_private_file_atomic

SCHEMA = "hyprial-model-secret/v1"
CONNECTION_SCHEMA = "hyprial-model-secret/v2"
LIMIT = 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ModelServiceRoute:
    endpoint: str
    protocol: str
    vendor_id: str

    def __post_init__(self) -> None:
        if self.vendor_id != "deepseek" or self.protocol != "openai-completions":
            raise ValueError("unsupported model service routing")
        validate_endpoint(self.endpoint)

    def document(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "protocol": self.protocol, "vendorId": self.vendor_id}


@dataclass(frozen=True, slots=True)
class ModelSecretRecord:
    values: dict[str, str] = field(repr=False)
    routes: dict[str, ModelServiceRoute | ModelConnection] = field(default_factory=dict)
    configured: bool = False
    schema: str = SCHEMA

    def encode(self) -> bytes:
        document = self.values if not self.configured else {
            "$schema": self.schema, "values": self.values,
            "routes": {name: route.document() for name, route in self.routes.items()},
        }
        payload = (json.dumps(document, sort_keys=True) + "\n").encode()
        if len(payload) > LIMIT:
            raise ValueError("model secret record exceeds limit")
        return payload


def validate_values(values: object) -> dict[str, str]:
    if not isinstance(values, Mapping) or any(
        not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(value, str)
        for name, value in values.items()
    ):
        raise ValueError("invalid model secret fields")
    return dict(values)


def decode(document: object) -> ModelSecretRecord:
    if not isinstance(document, dict):
        raise ValueError("invalid model secret record")
    if "$schema" not in document:
        return ModelSecretRecord(validate_values(document))
    if (set(document) != {"$schema", "values", "routes"}
            or not isinstance(document["$schema"], str)
            or document["$schema"] not in {SCHEMA, CONNECTION_SCHEMA}):
        raise ValueError("unknown model secret schema")
    values = validate_values(document["values"])
    raw_routes = document["routes"]
    if not isinstance(raw_routes, dict) or not raw_routes or not set(raw_routes) <= values.keys():
        raise ValueError("invalid model secret routes")
    routes = {}
    for name, raw in raw_routes.items():
        if document["$schema"] == CONNECTION_SCHEMA and isinstance(raw, dict) and "version" in raw:
            routes[name] = ModelConnection.decode(raw)
            continue
        if not isinstance(raw, dict) or set(raw) != {"endpoint", "protocol", "vendorId"}:
            raise ValueError("invalid model service route")
        routes[name] = ModelServiceRoute(raw["endpoint"], raw["protocol"], raw["vendorId"])
    return ModelSecretRecord(values, routes, configured=True, schema=document["$schema"])


def read(path: Path) -> ModelSecretRecord:
    data, _ = read_private_file(path, max_bytes=LIMIT)
    return decode(json.loads(data))


def write(path: Path, values: Mapping[str, str], *, route_field: str | None = None,
          route: ModelServiceRoute | None = None, create_only: bool = False,
          connection: Mapping[str, object] | None = None, expected_revision: int | None = None) -> None:
    updates = validate_values(values)
    if connection is not None:
        connection = configuration(connection)
        expected_revision = integer(expected_revision)
        if route is not None:
            raise ValueError("invalid model connection revision")
    with ProcessFileLock(path.parent / ".write.lock") as lock:
        lock.acquire(timeout=5.0)
        try:
            previous = read(path)
        except FileNotFoundError:
            previous = ModelSecretRecord({})
        else:
            if create_only:
                raise FileExistsError(path)
        configured = previous.configured or route is not None or connection is not None
        schema = previous.schema
        merged = {**previous.values, **updates} if configured else updates
        routes = dict(previous.routes)
        if connection is not None:
            validate_key(merged.get(route_field))
            old = routes.get(route_field)
            revision = old.config_revision if isinstance(old, ModelConnection) else 0
            if expected_revision != revision:
                raise ConnectionConflict("model connection revision mismatch")
            unchanged = isinstance(old, ModelConnection) and old.configuration() == connection
            routes[route_field] = ModelConnection(
                connection["endpoint"], connection["protocol"], connection["profile"],
                old.connection_ref if isinstance(old, ModelConnection) else uuid4().hex,
                revision if unchanged else revision + 1,
                connection["preset"]["id"], connection["preset"]["version"],
            )
            schema = CONNECTION_SCHEMA
        if route is not None:
            if isinstance(routes.get(route_field), ModelConnection):
                raise ConnectionConflict("model connection cannot be downgraded")
            if route_field not in merged or not merged[route_field]:
                raise ValueError("model service route requires a saved field")
            routes[route_field] = route
        for name, saved_route in routes.items():
            if isinstance(saved_route, ModelConnection) and (name in updates or name == route_field):
                validate_key(merged.get(name))
        record = ModelSecretRecord(merged, routes, configured, schema)
        write_private_file_atomic(path, record.encode(), exclusive=create_only)
