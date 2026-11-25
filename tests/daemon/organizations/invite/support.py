"""Shared daemon harness for the hosted-registry upgrade and owner-migration tests.

Identity, home provisioning and SQLite are real; only network presence and the
inactive harness status surface are doubles.  The host-invite feature these
helpers grew up with was deleted (owner ruling 2026-11-21); the registry seam
``create_transfer_hosted`` still writes hosted rows for these tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import LEGACY_BUSINESS_TEXT_FILES
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from tests.identity.agents.registry.entity.support import StubPresence
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

TABLES = (
    "agents",
    "agent_secret_grants",
    "lifecycle_resources",
    "lifecycle_receipts",
)


def database_rows(database: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in TABLES
        }
    finally:
        connection.close()


def home_files(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def app_factory(tmp_path, own_daemon_application):
    class InactiveHarnesses:
        def status(self):
            return ()

    def create(root: Path | None = None, *, owner="host-owner"):
        root = root or tmp_path
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        app = own_daemon_application(
            DaemonApplication(
                state_dir=root / "state",
                socket_path=root / "daemon.sock",
                owner=owner,
                node_id="host-node",
                hyprial_home=home,
            **_deep_module_daemon_dependencies())
        )
        app._inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport())
        app._presence = StubPresence()
        app._harnesses = InactiveHarnesses()
        return app

    return create


def alias_state(root: Path) -> dict:
    """Logical database contents plus home bytes/modes, excluding SQLite journals."""
    result = {}
    for path in sorted(root.rglob("*.sqlite3")):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            result[str(path.relative_to(root))] = {
                table: sorted(db.execute(
                    'SELECT * FROM "' + table.replace('"', '""') + '"'
                ).fetchall(), key=repr) for (table,) in tables
            }
        finally:
            db.close()
    result["home"] = {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in (root / "home").rglob("*") if path.is_file()
    }
    result["state_text"] = {
        name: (path.stat().st_mode, path.read_bytes())
        for name in LEGACY_BUSINESS_TEXT_FILES
        if (path := root / "state" / name).is_file()
    }
    return result
