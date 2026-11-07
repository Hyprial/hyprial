"""Hosted foreign-owner entity contract (Allen 2026-09-07, decision A)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._base import Agent, AgentError, AgentNotFoundError
from hyprial.identity.impl.agents.registry._core import AgentRegistry


# Exact agents DDL from origin/intg/transfer@73b0b936, before hosted_by.
# Do not build the old database from the current schema: that misses upgrades.
OLD_AGENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    actor TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    machine TEXT NOT NULL,
    uri TEXT NOT NULL UNIQUE,
    cwd TEXT,
    provider TEXT,
    model TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    harness_args TEXT NOT NULL DEFAULT '{}',
    preferred_harness TEXT,
    last_harness TEXT,
    last_session_id TEXT,
    created_at_ms INTEGER NOT NULL
);
"""


@pytest.fixture(params=[False, True], ids=["fresh", "upgraded"])
def registry(tmp_path: Path, request: pytest.FixtureRequest):
    database = tmp_path / "agents.sqlite3"
    if request.param:
        with sqlite3.connect(database) as db:
            db.executescript(OLD_AGENTS_SCHEMA)
            db.execute(
                "INSERT INTO agents (actor, owner, machine, uri, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                ("native", "target-owner", "target-node",
                 "agent:target-owner:target-node:native", 1),
            )
    result = AgentRegistry(database, owner="target-owner", machine="target-node")
    try:
        yield result
    finally:
        result.close()


def hosted_record(*, owner: str = "source-owner", machine: str = "target-node") -> Agent:
    return Agent(
        actor="worker", owner=owner, machine=machine,
        uri=f"agent:{owner}:{machine}:worker", hosted_by="transfer-receive",
        created_at_ms=123,
    )


def seed_storage(registry: AgentRegistry, record: Agent) -> None:
    """Storage/upgrade fixture, not an authorized application write path.

    Exercise reserved authorities and foreign-machine records with actual DDL,
    without adding a production squire setup or save-authorization backdoor.
    """
    with sqlite3.connect(registry.database) as db:
        db.execute("DELETE FROM agents WHERE actor = ?", (record.actor,))
        db.execute(*registry._insert_statement(record))


def test_explicit_migration_defaults_native_rows_to_null(registry: AgentRegistry) -> None:
    native = registry.get("native") or registry.create("native")
    assert native.hosted_by is None
    with sqlite3.connect(registry.database) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(agents)")}
        assert columns["hosted_by"][2] == "TEXT"
        assert db.execute("SELECT hosted_by FROM agents").fetchall() == [(None,)]
    # Opening an already upgraded database is idempotent and preserves data.
    reopened = AgentRegistry(registry.database, owner=registry.owner, machine=registry.machine)
    try:
        assert reopened.require("native") == native
    finally:
        reopened.close()


@pytest.mark.parametrize("authority", ["transfer-receive", "squire-container"])
def test_hosting_authority_persists_in_real_database(
    registry: AgentRegistry, authority: str,
) -> None:
    record = replace(hosted_record(), hosted_by=authority)
    seed_storage(registry, record)
    assert registry.require("worker") == record
    assert Agent.from_json(record.to_json(), "agent") == record
    registry.update("worker", model="updated-model")
    assert registry.require("worker").hosted_by == authority
    with sqlite3.connect(registry.database) as db:
        assert db.execute(
            "SELECT owner, machine, uri, hosted_by FROM agents WHERE actor='worker'"
        ).fetchone() == (record.owner, record.machine, record.uri, authority)


@pytest.mark.parametrize(
    ("owner", "machine"),
    [("source-owner", "target-node"), ("target-owner", "other-node"),
     ("source-owner", "other-node")],
)
def test_only_exact_hosted_uri_resolves(
    registry: AgentRegistry, owner: str, machine: str,
) -> None:
    record = hosted_record(owner=owner, machine=machine)
    assert registry.local_actor(record.uri) is None  # acceptance (a), also (e)
    with pytest.raises(AgentNotFoundError):
        registry.require(record.uri)
    # A local entity sharing the short name grants no foreign-URI authority.
    registry.create("worker")
    assert registry.local_actor(record.uri) is None
    # Nor does a stored foreign URI without an explicit hosting authority.
    seed_storage(registry, replace(record, hosted_by=None))
    assert registry.local_actor(record.uri) is None
    seed_storage(registry, record)
    assert registry.local_actor(record.uri) == "worker"  # acceptance (b)
    assert registry.require(record.uri) == record
    assert registry.uri_for("worker") == record.uri
    assert registry.uri_for(record.uri) == record.uri
    assert registry.local_actor("agent:unrelated:other-node:worker") is None
    assert registry.destroy(record.uri)
    assert registry.local_actor(record.uri) is None  # acceptance (c)
    with pytest.raises(AgentNotFoundError):
        registry.require(record.uri)


def test_create_rejects_foreign_uri_without_minting_a_native_alias(
    registry: AgentRegistry,
) -> None:
    # Acceptance (d): create must validate before normalize_actor loses owner.
    with pytest.raises(AgentError, match="cannot create foreign agent"):
        registry.create("agent:source-owner:target-node:worker")
    assert registry.get("worker") is None
    assert registry.local_actor("agent:source-owner:target-node:worker") is None


def test_receive_authority_creation_is_insert_only(registry: AgentRegistry) -> None:
    from hyprial.identity.impl.agents.registry._base import AgentExistsError

    record = registry.create_transfer_hosted("worker", pinned_owner="source-owner")
    assert record.uri == "agent:source-owner:target-node:worker"
    assert record.hosted_by == "transfer-receive"
    assert registry.require(record.uri) == record
    with pytest.raises(AgentExistsError):
        registry.create_transfer_hosted("worker", pinned_owner="other-owner")
    assert registry.require(record.uri) == record


def test_native_name_resolution_is_unchanged(registry: AgentRegistry) -> None:
    uri = "agent:target-owner:target-node:worker"
    assert registry.local_actor(uri) == "worker"
    assert registry.local_actor("worker") == "worker"
    assert registry.uri_for("worker") == uri
    assert registry.local_actor("agent:worker") is None
    assert registry.local_actor("invalid name") is None


def test_native_creation_uses_uri_leaf_parser_at_runtime(
    registry: AgentRegistry,
) -> None:
    native = registry.create("worker")

    assert native.actor == "worker"
    assert native.uri == "agent:target-owner:target-node:worker"


def test_transfer_hosted_creation_uses_uri_leaf_composer_at_runtime(
    registry: AgentRegistry,
) -> None:
    hosted = registry.create_transfer_hosted("worker", pinned_owner="source-owner")

    assert hosted.actor == "worker"
    assert hosted.uri == "agent:source-owner:target-node:worker"


def test_uri_for_keeps_stored_hosted_address_before_canonical_fallback(
    registry: AgentRegistry,
) -> None:
    hosted = hosted_record()
    seed_storage(registry, hosted)

    assert hosted.uri != "agent:target-owner:target-node:worker"
    assert registry.uri_for("worker") == hosted.uri
    assert registry.uri_for("unregistered") == (
        "agent:target-owner:target-node:unregistered"
    )


def test_destroy_hosted_row_cascades_pins_and_rejects_other_uris(
    registry: AgentRegistry,
) -> None:
    record = registry.create_transfer_hosted("worker", pinned_owner="source-owner")
    registry.pin("test-adapter", record.uri)
    assert not registry.destroy("agent:unrelated:target-node:worker")
    assert registry.pins() == {"test-adapter": record.uri}
    assert registry.destroy(record.uri)
    assert registry.pins() == {}
    assert registry.get("worker") is None
    assert registry.local_actor(record.uri) is None


def test_save_is_not_a_hosting_authority(registry: AgentRegistry) -> None:
    record = hosted_record()
    with pytest.raises(AgentError, match="save cannot authorize"):
        registry.save(record)
    assert registry.get("worker") is None
    native = registry.create("worker")
    with pytest.raises(AgentError, match="save cannot authorize"):
        registry.save(record)
    assert registry.require("worker") == native
    registry.destroy("worker")
    hosted = registry.create_transfer_hosted("worker", pinned_owner="source-owner")
    for changed in (
        replace(hosted, hosted_by=None),
        replace(hosted, hosted_by="squire-container"),
        replace(hosted, owner="another-owner"),
    ):
        with pytest.raises(AgentError, match="save cannot authorize"):
            registry.save(changed)
    assert registry.require(hosted.uri) == hosted
    assert registry.save(replace(hosted, cwd="/updated")).hosted_by == "transfer-receive"


def test_legacy_json_is_not_a_hosting_authority(tmp_path: Path) -> None:
    import json

    legacy = tmp_path / "agents"
    legacy.mkdir()
    path = legacy / "worker.json"
    path.write_text(json.dumps(hosted_record().to_json()))
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="target-owner", machine="target-node")
    try:
        assert registry.list() == ()
        assert registry.imported_legacy == ()
        assert path.exists()
    finally:
        registry.close()


def test_hosting_authority_enum_enforced(registry: AgentRegistry) -> None:
    with pytest.raises(AgentError, match="invalid hosting authority"):
        replace(hosted_record(), hosted_by="agent-create")
    registry.create("native-for-enum")
    with sqlite3.connect(registry.database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE agents SET hosted_by='agent-create'")
