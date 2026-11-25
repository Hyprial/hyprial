"""Slice 7: the daemon no longer composes P2 custody, and legacy data survives.

Decisions:

- The fixture is a constructed (not started) ``DaemonApplication`` with temp
  directories and a loopback endpoint, as in
  ``tests/daemon/teleport/wiring/test_daemon_composition.py``; on top of it the
  teleport target is started with a recording session, which runs the
  startup staging purge.  Booting the full startup graph needs a live
  transport and harness actors.
- "No P2 composition" is observed from outside: a fresh home gets no
  ``transfer_p2_*`` table, the application's class hierarchy and attributes
  hold nothing from a legacy module, and no product code still names the P2
  restore/shutdown hooks.
- The pre-existing legacy state is a literal copy of the historical schema
  (it must not depend on the code being deleted).  Historical tables are only
  no longer used: rows, tables and files stay byte for byte.
"""

from __future__ import annotations

import ast
import socket
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

from ..target.support import RecordingSession
from .support import legacy_name, product_modules

NODE = "node-legacy"
OWNER = "owner"
LEGACY_DATABASE = "lifecycle-operations.sqlite3"

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_p2_admissions (
    operation_id TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('source', 'target')),
    owner TEXT NOT NULL,
    source_node TEXT NOT NULL,
    source_agent_id TEXT NOT NULL,
    source_entity_token TEXT NOT NULL,
    target_node TEXT NOT NULL,
    target_actor TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    cancelled_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS transfer_p2_steps (
 operation_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 actor TEXT NOT NULL,
 phase TEXT NOT NULL,
 step TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),
 entity_token TEXT NOT NULL,
 receipt TEXT NOT NULL,
 needs_attention INTEGER NOT NULL CHECK(needs_attention IN (0,1)),
 PRIMARY KEY(operation_id,generation)
);
CREATE TABLE IF NOT EXISTS transfer_p2_attention (
 event_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 principal TEXT NOT NULL,
 action TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 recorded_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_p2_revision (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS transfer_p2_source_custody
ON transfer_p2_admissions(owner, source_node, source_agent_id, source_entity_token)
WHERE role = 'source' AND cancelled_at_ms IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS transfer_p2_target_name
ON transfer_p2_admissions(target_node, target_actor)
WHERE role = 'target' AND cancelled_at_ms IS NULL;
INSERT OR IGNORE INTO transfer_p2_revision VALUES (1,7);
"""

LEGACY_TRIGGERS = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_revision_{event.lower()}
AFTER {event} ON {table}
BEGIN UPDATE transfer_p2_revision SET version=version+1 WHERE singleton=1; END;
"""
    for table in ("transfer_p2_admissions", "transfer_p2_steps")
    for event in ("INSERT", "UPDATE", "DELETE")
)

LEGACY_ROWS = """
INSERT INTO transfer_p2_admissions VALUES (
 'op-legacy', '{}', 'source', 'owner', 'node-legacy', 'a-1', 'tok-1',
 'node-b', 'w1', 1000, 2000, NULL, NULL
);
INSERT INTO transfer_p2_steps VALUES (
 'op-legacy', 1, 'w1', 'quiesce', 'stopped', 'fp', 1, 'tok-1', '{}', 1
);
INSERT INTO transfer_p2_attention VALUES (
 'ev-1', 'op-legacy', 1, 'owner', 'override', 'operator', 3000
);
"""

LEGACY_FILES = {
    ("home", "secrets/transfer/signing-key.json"): b'{"legacy": "signing"}\n',
    ("home", "secrets/transfer/trusted-source-keys.json"): b'{"legacy": "trust"}\n',
    ("state", "transfer-landing/0123abcd/manifest.json"): b'{"legacy": "landing"}\n',
}

P2_WIRING_NAMES = frozenset(
    {
        "_transfer_p2",
        "_transfer_source",
        "_transfer_source_state",
        "_hydrate_transfer_source",
        "_filter_transfer_restore_specs",
        "_retry_transfer_restore",
        "_reconcile_transfer_quiesce",
        "_start_transfer_source",
        "_make_transfer_authority",
    }
)


def _application(tmp_path: Path) -> DaemonApplication:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        hyprial_home=tmp_path / "home",
        zenoh_listen=(endpoint,),
        zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    return app


def _compose(tmp_path: Path, request: pytest.FixtureRequest) -> DaemonApplication:
    app = _application(tmp_path)

    def close() -> None:
        app._close()
        app._close_home_lease()

    request.addfinalizer(close)
    app._start_teleport_target(RecordingSession())
    return app


def _dump(database: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(database)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'transfer_p2_%' ORDER BY name"
            )
        ]
        dump: dict[str, list[tuple]] = {"__objects__": [(name,) for name in names]}
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'transfer_p2_%'"
        )
        for (table,) in tables.fetchall():
            dump[table] = sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
        return dump
    finally:
        connection.close()


def _seed(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    database = state / LEGACY_DATABASE
    connection = sqlite3.connect(database)
    try:
        connection.executescript(LEGACY_SCHEMA + LEGACY_TRIGGERS + LEGACY_ROWS)
        connection.commit()
    finally:
        connection.close()
    for (root, relative), content in LEGACY_FILES.items():
        path = tmp_path / root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return database


def test_fresh_home_gets_no_p2_table(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    _compose(tmp_path, request)
    created: list[str] = []
    for database in sorted(tmp_path.rglob("*.sqlite3")):
        names = _dump(database)["__objects__"]
        created.extend(f"{database.relative_to(tmp_path)}:{name}" for (name,) in names)
    assert created == []


def test_application_composes_nothing_from_legacy_modules(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    app = _compose(tmp_path, request)
    bases = sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in type(app).__mro__
        if legacy_name(cls.__module__)
    )
    held = sorted(
        f"{attribute}: {type(value).__module__}.{type(value).__qualname__}"
        for attribute, value in vars(app).items()
        if legacy_name(type(value).__module__)
    )
    assert bases == []
    assert held == []
    assert sorted(P2_WIRING_NAMES & set(vars(app))) == []


def test_no_product_code_names_a_p2_restore_or_shutdown_hook() -> None:
    hits: list[str] = []
    for name, module in sorted(product_modules().items()):
        for node in ast.walk(module.parse()):
            if isinstance(node, ast.Attribute) and node.attr in P2_WIRING_NAMES:
                hits.append(f"  {name}:{node.lineno} .{node.attr}")
            elif isinstance(node, ast.FunctionDef) and node.name in P2_WIRING_NAMES:
                hits.append(f"  {name}:{node.lineno} def {node.name}")
            elif isinstance(node, ast.Constant) and node.value in P2_WIRING_NAMES:
                hits.append(f"  {name}:{node.lineno} {node.value!r}")
    assert hits == [], "P2 restore/shutdown wiring still present:\n" + "\n".join(hits)


def test_pre_existing_legacy_state_is_left_untouched(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    database = _seed(tmp_path)
    before = _dump(database)
    assert before["transfer_p2_admissions"]
    app = _compose(tmp_path, request)
    app._close()
    assert database.exists()
    assert _dump(database) == before
    for (root, relative), content in LEGACY_FILES.items():
        assert (tmp_path / root / relative).read_bytes() == content, relative
