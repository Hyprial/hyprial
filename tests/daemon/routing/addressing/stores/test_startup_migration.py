"""UUID addressing P4, external stores: the daemon start runs the migration.

A home written before the id-form flip holds ``agent:op:node-1:worker`` in
the Lark reply routes, the PAC graph, the routine store and the org ACL
grants.  With the switch on, the daemon start opens the identity registry
(whose migration rewrites ``agents.uri`` to the ``a.`` id URI) and only then
migrates those stores to the registry's official URI.  The PAC store
migrates after the legacy workflow cutover (its schema upgrade may read the
sealed workflow database) and before the link service and the PAC authority
read it, in the same start even when that start also upgrades the PAC schema;
the routine store migrates before the routine runtime opens it.  A second
start is a no-op; with the switch off nothing is touched and no marker is
written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import hyprial.identity.impl.agents.addressing as addressing
from hyprial.daemon.impl.application.actors import stored_agent_uris
from hyprial.daemon.impl.org.network.state import add_org_acl_grant, read_org_acl_grants
from hyprial.daemon.impl.adapters.lark.state.agent_uris import MIGRATION_MARKER_KEY
from hyprial.daemon.impl.pac.graphs import authority
from hyprial.daemon.impl.pac.storage import agent_uris as pac_agent_uris
from hyprial.daemon.impl.pac.storage import legacy
from hyprial.daemon.impl.pac.storage.migrations import SCHEMA_VERSION
from hyprial.identity import AgentRegistry
from hyprial.kernel import canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import routines as routine_wiring
from tests.support.runtime.cell_delivery.composition_probe import _probe
from tests.daemon.routing.addressing.stores.test_store_migrations import (
    ID_URI,
    NAME_URI,
    _seed_adapters,
    _seed_pac,
    _seed_routines,
)
from tests.support.pac.migrations import strip_held_v20

ORG = "group-acme"


def _shutdown(app) -> None:
    app._close()
    app.agents.close()
    close_lease = getattr(app, "_close_home_lease", None)
    if close_lease is not None:
        close_lease()


def _value(path: Path, query: str) -> object:
    db = sqlite3.connect(path)
    try:
        return db.execute(query).fetchone()[0]
    finally:
        db.close()


def _seed_pre_flip_home(monkeypatch, tmp_path: Path) -> None:
    """Write the home as a pre-flip daemon did: the switch stays off after."""

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app, _captured, _links = _probe(monkeypatch, tmp_path)
    try:
        assert app.agents.create("worker").uri == NAME_URI
    finally:
        _shutdown(app)
    state, home = tmp_path / "state", tmp_path / "home"
    _seed_adapters(state / "adapters.sqlite3")
    _seed_pac(state / "pac-graph.sqlite3")
    _seed_routines(state / "routines.sqlite3")
    add_org_acl_grant(home, ORG, NAME_URI, granted_at_ms=1)


def _stored(tmp_path: Path) -> dict[str, object]:
    state = tmp_path / "state"
    return {
        "reply": _value(state / "adapters.sqlite3",
                        "SELECT actor_key FROM reply_routes WHERE message_id = 'm-1'"),
        "pacOwner": _value(state / "pac-graph.sqlite3",
                           "SELECT owner FROM nodes WHERE node_id = 'n-1'"),
        "pacCreator": _value(state / "pac-graph.sqlite3", "SELECT created_by FROM graphs"),
        "routineOwner": _value(state / "routines.sqlite3",
                               "SELECT owner FROM routines WHERE name = 'duty'"),
        "orgAcl": sorted(read_org_acl_grants(tmp_path / "home")[ORG]),
    }


def _downgrade_pac_schema(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "state" / "pac-graph.sqlite3")
    try:
        with db:
            # One step behind the current schema: the cutover's upgrade adds
            # the held column, so a successful start must apply it and a
            # failed cutover must leave the store unopened at v19.
            strip_held_v20(db)
    finally:
        db.close()


def test_daemon_start_migrates_stores_after_the_registry_open(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_pre_flip_home(monkeypatch, tmp_path)
    _downgrade_pac_schema(tmp_path)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    order: list[str] = []
    results: list[object] = []
    open_registry = AgentRegistry.__init__
    workflow_cutover = legacy.cutover
    migrate = stored_agent_uris.migrate_stored_agent_uris
    open_authority = authority.PacGraphAuthority.__init__
    migrate_routines = routine_wiring.migrate_stored_routine_agent_uris

    def registry_init(self, *args, **kwargs):
        order.append("identity-registry-open")
        open_registry(self, *args, **kwargs)

    def cutover(*args, **kwargs):
        order.append("workflow-cutover")
        return workflow_cutover(*args, **kwargs)

    def stores(app, **kwargs):
        order.append("stores-migration")
        results.append(migrate(app, **kwargs))
        return results[-1]

    def authority_init(self, *args, **kwargs):
        order.append("pac-authority-open")
        open_authority(self, *args, **kwargs)

    def routine_store(deps):
        order.append("routine-store-migration")
        results.append(migrate_routines(deps))
        return results[-1]

    monkeypatch.setattr(AgentRegistry, "__init__", registry_init)
    monkeypatch.setattr(stored_agent_uris, "migrate_stored_agent_uris", stores)
    monkeypatch.setattr(legacy, "cutover", cutover)
    monkeypatch.setattr(authority.PacGraphAuthority, "__init__", authority_init)
    monkeypatch.setattr(routine_wiring, "migrate_stored_routine_agent_uris", routine_store)

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    try:
        agent = app.agents.require("worker")
        id_uri = canonical_agent_uri("op", "node-1", agent.agent_id)
        assert agent.uri == id_uri
        app._start_runtime()
    finally:
        _shutdown(app)

    assert order.index("identity-registry-open") < order.index("stores-migration")
    assert order.index("workflow-cutover") < order.index("stores-migration")
    assert order.index("stores-migration") < order.index("pac-authority-open")
    assert order.index("stores-migration") < order.index("routine-store-migration")
    assert results == [{"adapters": 1, "pac": 2, "orgAcl": 1}, 5]
    assert _value(tmp_path / "state" / "pac-graph.sqlite3", "PRAGMA user_version") == (
        SCHEMA_VERSION
    )
    assert _stored(tmp_path) == {
        "reply": id_uri,
        "pacOwner": id_uri,
        "pacCreator": id_uri,
        "routineOwner": id_uri,
        "orgAcl": [id_uri],
    }

    results.clear()
    app, _captured, _links = _probe(monkeypatch, tmp_path)
    try:
        app._start_runtime()
    finally:
        _shutdown(app)
    assert results == [{}, None]
    assert _stored(tmp_path)["reply"] == id_uri


def test_daemon_start_with_the_switch_off_touches_no_store(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_pre_flip_home(monkeypatch, tmp_path)
    before = _stored(tmp_path)

    app, _captured, _links = _probe(monkeypatch, tmp_path)
    try:
        assert stored_agent_uris.migrate_stored_agent_uris(app) is None
        app._start_runtime()
    finally:
        _shutdown(app)

    assert _stored(tmp_path) == before == {
        "reply": NAME_URI,
        "pacOwner": NAME_URI,
        "pacCreator": NAME_URI,
        "routineOwner": NAME_URI,
        "orgAcl": [NAME_URI],
    }
    assert not (tmp_path / "home" / "state" / "org" / "org-acl-grants.agent-uri-migrated.json").exists()
    assert _value(
        tmp_path / "state" / "pac-graph.sqlite3",
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agent_uri_migration'",
    ) == 0
    assert _value(
        tmp_path / "state" / "routines.sqlite3",
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'routine_store_markers'",
    ) == 0
    assert _value(
        tmp_path / "state" / "adapters.sqlite3",
        f"SELECT COUNT(*) FROM meta WHERE key = '{MIGRATION_MARKER_KEY}'",
    ) == 0


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("agent:op:node-2:worker", True),
        ("agent:op:node-1:squire", True),
        ("agent:other:node-1:worker", True),
        ("agent:op:node-1:ghost", True),
        ("user:op", True),
        (NAME_URI, False),
    ],
)
def test_the_shared_mapping_answers_only_live_local_agents(
    tmp_path: Path, monkeypatch, value: str, kept: bool
) -> None:
    _seed = _probe(monkeypatch, tmp_path)[0]
    try:
        _seed.agents.create("worker")
    finally:
        _shutdown(_seed)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    app = _probe(monkeypatch, tmp_path)[0]
    try:
        mapping = stored_agent_uris.stored_agent_uri_mapping(app)
        official = mapping(value)
        id_uri = app.agents.require("worker").uri
        assert mapping(id_uri) is None
    finally:
        _shutdown(app)
    assert official == (None if kept else id_uri)


def test_a_failing_store_is_logged_and_left_for_the_next_start(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_adapters(tmp_path / "state" / "adapters.sqlite3")
    _seed_pac(tmp_path / "state" / "pac-graph.sqlite3")
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pac_agent_uris, "migrate_pac_agent_uris", broken)
    logged: list[tuple[str, str, dict[str, object]]] = []
    app = SimpleNamespace(
        node_id="node-1",
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        _agent_address=lambda name, owner=None: ID_URI if name == "worker" else name,
        _log=lambda level, _component, event, **fields: logged.append(
            (level, event, fields)
        ),
    )

    assert stored_agent_uris.migrate_stored_agent_uris(app) == {"adapters": 1}
    assert [(level, event) for level, event, _ in logged] == [
        ("error", stored_agent_uris.FAILED_EVENT),
        ("info", stored_agent_uris.MIGRATED_EVENT),
    ]
    assert logged[0][2]["store"] == "pac"
    assert logged[1][2] == {"stores": {"adapters": 1}}
    assert _value(
        tmp_path / "state" / "pac-graph.sqlite3",
        "SELECT owner FROM nodes WHERE node_id = 'n-1'",
    ) == NAME_URI


def test_a_failing_routine_store_is_logged_and_left_for_the_next_start(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_routines(tmp_path / "routines.sqlite3")

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(routine_wiring, "migrate_routine_agent_uris", broken)
    logged: list[tuple[str, str, dict[str, object]]] = []
    deps = SimpleNamespace(
        state_dir=tmp_path,
        stored_agent_uri={NAME_URI: ID_URI}.get,
        clock_ms=lambda: 1,
        logger=SimpleNamespace(
            log=lambda level, event, **fields: logged.append((level, event, fields))
        ),
    )

    assert routine_wiring.migrate_stored_routine_agent_uris(deps) is None
    assert [(level, event) for level, event, _ in logged] == [
        ("error", stored_agent_uris.FAILED_EVENT)
    ]
    assert logged[0][2]["store"] == "routines"
    assert "database is locked" in str(logged[0][2]["error"])
    assert _value(
        tmp_path / "routines.sqlite3",
        "SELECT owner FROM routines WHERE name = 'duty'",
    ) == NAME_URI
    assert _value(
        tmp_path / "routines.sqlite3",
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'routine_store_markers'",
    ) == 0


def test_a_failed_workflow_cutover_leaves_the_pac_store_unopened(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_adapters(tmp_path / "state" / "adapters.sqlite3")
    _seed_pac(tmp_path / "state" / "pac-graph.sqlite3")
    _downgrade_pac_schema(tmp_path)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    app = SimpleNamespace(
        node_id="node-1",
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        _agent_address=lambda name, owner=None: ID_URI if name == "worker" else name,
        _log=lambda *_args, **_fields: None,
    )

    assert stored_agent_uris.migrate_stored_agent_uris(app, pac=False) == {"adapters": 1}
    pac = tmp_path / "state" / "pac-graph.sqlite3"
    assert _value(pac, "PRAGMA user_version") == SCHEMA_VERSION - 1
    assert _value(pac, "SELECT owner FROM nodes WHERE node_id = 'n-1'") == NAME_URI
    assert _value(
        pac, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agent_uri_migration'"
    ) == 0
