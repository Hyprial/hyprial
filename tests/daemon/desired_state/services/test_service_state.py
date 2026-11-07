from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import (
    DesiredStateStore,
    ServiceRegistry,
)
from hyprial.kernel import DesiredStateError


def _store(tmp_path: Path) -> DesiredStateStore:
    return DesiredStateStore(tmp_path / "desired.json")


def test_service_connection_and_registry_round_trip_without_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_service_connection("api", 0)
    store.set_service_registry(
        {
            "schemaVersion": 1,
            "spaceId": "space-1",
            "owner": "owner-1",
            "cachedAtMs": 10,
            "entries": [
                {
                    "name": "api",
                    "deviceId": "device-1",
                    "remotePort": 80,
                    "protocol": "http",
                    "localPort": 18080,
                    "usage": "base-url",
                    "env": {"BASE_URL": "http://127.0.0.1:{port}"},
                    "auth": "use app key",
                    "guide": "guide",
                }
            ],
        }
    )

    state = store.load()
    assert state.service_connections[0].to_json() == {"name": "api", "localPort": 0}
    assert state.service_registry is not None
    assert state.service_registry.entries[0].device_id == "device-1"
    assert "address" not in json.dumps(state.to_json())
    assert "serverPublic" not in json.dumps(state.to_json())

    reopened = DesiredStateStore(tmp_path / "desired.json")
    assert reopened.load() == state


def test_empty_registry_is_distinct_from_absent_and_remove_preserves_other_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.set_mailbox_role(True)
    store.set_service_connection("api", 18080)
    store.set_service_registry(
        {
            "schemaVersion": 1,
            "spaceId": "space-1",
            "owner": "owner-1",
            "cachedAtMs": 10,
            "entries": [],
        }
    )
    store.remove_service_connection("api")
    state = store.load()
    assert state.as_mailbox is True
    assert state.service_connections == ()
    assert state.service_registry is not None
    assert state.service_registry.entries == ()

    store.set_service_registry(None)
    assert store.load().service_registry is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda store: store.set_service_connection("api", True),
        lambda store: store.set_service_connection("api", -1),
        lambda store: store.set_service_registry(
            {"schemaVersion": 1, "spaceId": "s", "owner": "o", "cachedAtMs": True, "entries": []}
        ),
        lambda store: store.set_service_registry(
            {"schemaVersion": 1, "spaceId": "s", "owner": "o", "cachedAtMs": 1, "entries": [{"name": "api"}]}
        ),
    ],
)
def test_service_state_rejects_bool_invalid_or_incomplete_values(tmp_path: Path, mutator) -> None:
    with pytest.raises((TypeError, ValueError, DesiredStateError)):
        mutator(_store(tmp_path))


def test_old_database_without_service_tables_reads_empty_service_state(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE root_state (id INTEGER PRIMARY KEY, schema_version INTEGER, as_mailbox INTEGER)")
        db.execute("INSERT INTO root_state VALUES (1, 1, 0)")
        db.execute("CREATE TABLE harnesses (harness TEXT, name TEXT)")
        db.commit()

    state = DesiredStateStore(tmp_path / "desired.json").load()
    assert state.service_connections == ()
    assert state.service_registry is None


def test_service_record_is_frozen_and_copy_isolated() -> None:
    source = {
        "schemaVersion": 1,
        "spaceId": "s",
        "owner": "o",
        "cachedAtMs": 1,
        "entries": [],
    }
    cache = ServiceRegistry.from_json(source)
    source["entries"].append({})
    assert cache.entries == ()
    with pytest.raises((AttributeError, TypeError)):
        cache.entries += ()  # type: ignore[misc]
