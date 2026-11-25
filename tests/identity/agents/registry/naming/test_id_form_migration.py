"""UUID addressing §2.4 (identity part of P4): the one-time open migration.

Runs only when ``id_form_minting()`` is on and is idempotent: stored
name-form agent URIs become the minted id form, URI-keyed binding fences
follow, and ``agent_secret_grants`` and ``retired_session_refs`` gain a
backfilled ``agent_id``.  Retired refs of earlier incarnations stay fenced by
their incarnation, and no used name is recorded.  Homes stay keyed by the
immutable actor: no ``agent-home:<agent_id>`` row is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents import addressing

from .conftest import NODE, OWNER


@pytest.fixture(autouse=True)
def _seeded_before_the_flip(switch_off: None) -> None:
    """Every scenario seeds the pre-flip store; a test flips the switch itself."""


def _open(tmp_path: Path, *, home: bool = False) -> AgentRegistry:
    return AgentRegistry(
        tmp_path / "state" / "agents.sqlite3", owner=OWNER, machine=NODE,
        hyprial_home=(tmp_path / "hyprial") if home else None,
    )


def _resources(registry: AgentRegistry) -> dict[str, tuple[int, str, dict]]:
    return {
        str(row["resource_key"]): (
            int(row["active"]), str(row["resource_token"]), json.loads(str(row["payload"]))
        )
        for row in registry._db.execute("SELECT * FROM lifecycle_resources")
    }


def _seed(tmp_path: Path, *, home: bool = False) -> None:
    registry = _open(tmp_path, home=home)
    try:
        foo = registry.create("foo")
        registry.create_transfer_hosted("tx", pinned_owner="alice")
        registry.record_external_binding(
            "foo", harness="pi", runtime="headless", session_id="s-1"
        )
        registry._db.execute(
            "INSERT INTO agent_secret_grants (agent, entity_token, grant_id, source, "
            "entry_id, field_name, environment_names, revision) "
            "VALUES ('foo', ?, 'g1', 'agent-private', 'e1', NULL, '[\"TOKEN\"]', 1)",
            (foo.entity_token,),
        )
        registry._db.commit()
        registry.record_session_ref("foo", foo.entity_token, "ref-live")
        registry.retire_session_refs(
            "foo", foo.entity_token, ["ref-old"], reason="test", destroy_attempt="t1",
        )
    finally:
        registry.close()


def test_switch_off_open_changes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    registry = _open(tmp_path)
    try:
        assert registry.require("foo").uri == f"agent:{OWNER}:{NODE}:foo"
        columns = {row[1] for row in registry._db.execute("PRAGMA table_info(agent_secret_grants)")}
        assert "agent_id" not in columns
    finally:
        registry.close()


def test_switch_on_open_rewrites_uris_and_follows_the_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    registry = _open(tmp_path)
    try:
        foo = registry.require("foo")
        tx = registry.require("tx")
        assert foo.uri == f"agent:{OWNER}:{NODE}:{foo.agent_id}"
        assert tx.uri == f"agent:alice:{NODE}:{tx.agent_id}"
        assert registry.local_actor(f"agent:{OWNER}:{NODE}:foo") == "foo"
        resources = _resources(registry)
        assert f"binding:agent:{OWNER}:{NODE}:foo" not in resources
        assert registry.lifecycle_binding("foo")["actor"] == foo.uri
        assert resources["agent-record:foo"][2] == foo.to_json()
        grant = registry._db.execute(
            "SELECT agent_id FROM agent_secret_grants WHERE grant_id = 'g1'"
        ).fetchone()
        assert grant["agent_id"] == foo.agent_id
        retired = registry._db.execute(
            "SELECT actor, agent_id FROM retired_session_refs WHERE session_ref = 'ref-old'"
        ).fetchone()
        assert (retired["actor"], retired["agent_id"]) == (foo.uri, foo.agent_id)
        assert registry.is_session_ref_retired("foo", "ref-old")
    finally:
        registry.close()


def test_switch_on_migration_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path, home=True)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    first = _open(tmp_path, home=True)
    try:
        before = (_resources(first), first.list())
    finally:
        first.close()
    second = _open(tmp_path, home=True)
    try:
        assert (_resources(second), second.list()) == before
    finally:
        second.close()


def test_switch_on_writes_no_id_keyed_home_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path, home=True)
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    registry = _open(tmp_path, home=True)
    try:
        homes = sorted(
            key for key in _resources(registry)
            if key.startswith(("agent-home:", "agent-home-effect:"))
        )
        assert homes == ["agent-home:foo", "agent-home:tx"]
        assert registry.home_receipt("foo").actor == "foo"
    finally:
        registry.close()


def test_switch_on_keeps_fencing_refs_of_an_incarnation_destroyed_before_the_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _open(tmp_path)
    try:
        first = registry.create("foo")
        registry.retire_session_refs(
            "foo", first.entity_token, ["ref-0"], reason="destroy", destroy_attempt="d0",
        )
        registry.destroy("foo")
        registry.create("foo")
        assert registry.is_session_ref_retired("foo", "ref-0")
    finally:
        registry.close()
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    reopened = _open(tmp_path)
    try:
        live = reopened.require("foo")
        assert live.agent_id != first.agent_id
        assert reopened.is_session_ref_retired("foo", "ref-0")
        assert reopened.is_session_ref_retired(f"agent:{OWNER}:{NODE}:foo", "ref-0")
        assert reopened.is_session_ref_retired(live.uri, "ref-0")
        assert reopened.release_retired_session_ref(f"agent:{OWNER}:{NODE}:foo", "ref-0")
        assert not reopened.is_session_ref_retired("foo", "ref-0")
    finally:
        reopened.close()


@pytest.mark.usefixtures("id_form")
def test_retired_refs_of_a_destroyed_id_form_agent_stay_fenced(
    registry: AgentRegistry,
) -> None:
    foo = registry.create("foo")
    registry.retire_session_refs(
        "foo", foo.entity_token, ["ref-1"], reason="destroy", destroy_attempt="d1",
    )
    registry.destroy("foo")
    assert registry.is_session_ref_retired("foo", "ref-1")
    assert registry.is_session_ref_retired(foo.uri, "ref-1")
    assert registry.release_retired_session_ref(foo.uri, "ref-1")
    assert not registry.is_session_ref_retired("foo", "ref-1")


def _insert_legacy_ref(registry: AgentRegistry, token: str) -> None:
    registry._db.execute(
        "INSERT INTO retired_session_refs (actor, entity_token, session_ref, "
        "retired_at_ms, reason, destroy_attempt) VALUES (?, ?, 'ref-0', 7, 'destroy', 'd0')",
        (f"agent:{OWNER}:{NODE}:foo", token),
    )
    registry._db.commit()
    registry._refresh_retired_session_refs_locked()


@pytest.mark.parametrize("token", ["", "legacy"])
def test_untokened_refs_of_a_destroyed_name_stay_fenced_and_the_name_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    seeded = _open(tmp_path)
    try:
        _insert_legacy_ref(seeded, token)
    finally:
        seeded.close()
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    registry = _open(tmp_path)
    try:
        assert registry.is_session_ref_retired(f"agent:{OWNER}:{NODE}:foo", "ref-0")
        foo = registry.create("foo")
        assert foo.uri == f"agent:{OWNER}:{NODE}:{foo.agent_id}"
        assert registry.is_session_ref_retired("foo", "ref-0")
    finally:
        registry.close()


@pytest.mark.parametrize("token", ["", "legacy"])
def test_untokened_refs_of_a_live_name_stay_fenced_by_name_after_the_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    seeded = _open(tmp_path)
    try:
        seeded.create("foo")
        _insert_legacy_ref(seeded, token)
        assert seeded.is_session_ref_retired("foo", "ref-0")
    finally:
        seeded.close()
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    registry = _open(tmp_path)
    try:
        foo = registry.require("foo")
        assert foo.uri == f"agent:{OWNER}:{NODE}:{foo.agent_id}"
        assert registry.is_session_ref_retired("foo", "ref-0")
        assert registry.release_retired_session_ref(f"agent:{OWNER}:{NODE}:foo", "ref-0")
        assert not registry.is_session_ref_retired("foo", "ref-0")
    finally:
        registry.close()
