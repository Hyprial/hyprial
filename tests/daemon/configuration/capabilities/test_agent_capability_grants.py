"""Capability ledger journeys through DaemonApplication, without workers.

The module explicitly re-exports the shared app_factory pytest fixture.
"""

from tests.support.agent_destroy import destroy_agent

from contextlib import closing
import sqlite3

import pytest

import hyprial.identity.impl.agents.addressing as addressing
from hyprial.daemon.impl.application import DaemonRequestError
from tests.daemon.organizations.invite import support

app_factory = support.app_factory


def resident(app):
    return app.handle("agent.create", {"name": "guest"})


def grant(app, **overrides):
    return app.handle("agent.grant", {
        "actor": "guest", "grantId": "g1", "capability": "send-to",
        "scope": '["agent:visitor:elsewhere:helper"]', "revision": 1,
        **overrides,
    })["grant"]


def journal(app):
    return app.handle("agent.grants", {"actor": "guest", "audit": True})["journal"]


@pytest.mark.parametrize("destroy", ["forward", "forward-switch-off", "compensating"])
def test_ledger_revision_revoke_restart_destroy(app_factory, monkeypatch, destroy):
    """Re-creating the destroyed name starts with no grants and a new token.

    Every destroy path leaves the name free (owner decision 2026-11-19).  The
    grant journal is an audit read by name: the new incarnation's journal
    shows the earlier rows, each carrying its own entity token.
    """

    if destroy == "forward-switch-off":
        monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    app = app_factory()
    resident(app)
    first = grant(app, grantedBy="user:forged")
    assert first["grantedBy"] == "user:host-owner"
    assert journal(app)[-1]["action"] == "grant"
    before = journal(app)
    with pytest.raises(DaemonRequestError, match="revision must increase"):
        grant(app)
    assert journal(app) == before
    second = grant(app, revision=2, scope='["user:visitor"]')
    assert second["revision"] == 2
    assert second["entityToken"] == first["entityToken"]
    app._close()
    app = app_factory()
    assert app.handle("agent.grants", {})["grants"] == [second]
    assert app.handle("agent.revoke", {"actor": "guest", "grantId": "g1"})["revoked"]
    assert journal(app)[-1]["action"] == "revoke"
    assert journal(app)[-1]["revision"] == 2
    before = journal(app)
    assert not app.handle("agent.revoke", {"actor": "guest", "grantId": "g1"})["revoked"]
    assert journal(app) == before
    assert app.handle("agent.grants", {})["grants"] == []
    grant(app, grantId="g2")
    before = journal(app)
    if destroy == "compensating":
        guest = app.agents.require("guest")
        settled = app.agents.settle_destroy(
            f"agent-capability-compensation:{guest.entity_token}:test",
            guest.actor,
            expected_entity_token=guest.entity_token,
        )
        assert settled.disposition == "destroyed"
    else:
        assert destroy_agent(app, "guest")["destroyed"]
    assert journal(app) == before
    assert app.handle("agent.grants", {})["grants"] == []
    resident(app)
    assert app.handle("agent.grants", {"actor": "guest"})["grants"] == []
    assert journal(app) == before
    successor = grant(app, grantId="g3")
    assert successor["entityToken"] != first["entityToken"]


@pytest.mark.parametrize("capability,scope,bad", [
    ("agent-home", "self", "other"),
    ("isolation", "directory", "host"),
    ("org-context", "accepted", "pending"),
    ("see-actors", '["agent:visitor:node:actor"]', '["actor"]'),
    ("send-to", '["user:visitor"]', '["route:outside"]'),
    ("tool-surface", '["harness_read"]', '[]'),
    ("shared-path", '{"path":"/srv/shared","mode":"ro"}', '{"path":"relative","mode":"ro"}'),
])
def test_scope_closed_schema(app_factory, capability, scope, bad):
    app = app_factory()
    resident(app)
    with pytest.raises(DaemonRequestError):
        grant(app, capability=capability, scope=bad)
    assert app.handle("agent.grants", {})["grants"] == []
    assert journal(app) == []
    assert grant(app, capability=capability, scope=scope)["scope"] == scope
    if capability == "agent-home":
        with pytest.raises(DaemonRequestError, match="intrinsic"):
            app.handle("agent.revoke", {"actor": "guest", "grantId": "g1"})
        assert len(app.handle("agent.grants", {})["grants"]) == 1


def test_stale_incarnation_is_not_an_active_grant(app_factory):
    app = app_factory()
    resident(app)
    original = grant(app)
    # Persist a broken FK as a fault injection, not a new rotation endpoint.
    # Read through the real daemon query: token mismatch must be fail-closed.
    with sqlite3.connect(app._agent_registry.database) as db:
        db.execute("UPDATE agent_capability_grants SET entity_token='stale'")
    assert app.handle("agent.grants", {})["grants"] == []
    assert app.handle("agent.grants", {"actor": "guest"})["grants"] == []
    assert journal(app)[-1]["entityToken"] == original["entityToken"]


def test_grant_leaves_no_partial_state_when_journal_write_fails(app_factory):
    app = app_factory()
    database = app._agent_registry.database
    resident(app)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON agent_grant_journal "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises((DaemonRequestError, sqlite3.IntegrityError)):
        grant(app)
    assert app.handle("agent.grants", {})["grants"] == []
    assert journal(app) == []


def test_owner_rename_preserves_historical_attribution(app_factory):
    """L3 retires startup owner_migration rewrites; grants and journal stay immutable."""
    app = app_factory()
    app.handle("agent.create", {"name": "native"})
    resident(app)
    grant(app)
    before = journal(app)
    app._close()
    app = app_factory(owner="renamed-host")
    assert journal(app) == before
    assert app.handle("agent.grants", {})["grants"][0]["grantedBy"] == "user:host-owner"


def test_the_retired_channel_capability_is_refused(app_factory):
    app = app_factory()
    resident(app)
    with pytest.raises(DaemonRequestError) as failure:
        grant(app, capability="channel", scope='["support-bot"]')
    assert failure.value.code == "INVALID_ARGUMENT"
    assert "unknown capability" in str(failure.value)
    assert app.handle("agent.grants", {})["grants"] == []
    assert journal(app) == []


def _store_unknown_row(app, capability, grant_id="old-row"):
    with closing(sqlite3.connect(app._agent_registry.database)) as db, db:
        db.execute(
            "INSERT INTO agent_capability_grants SELECT actor, entity_token, ?, "
            "?, '[\"support-bot\"]', granted_by, 1 FROM agent_capability_grants "
            "WHERE grant_id='g1'", (grant_id, capability),
        )
        db.execute(
            'INSERT INTO agent_grant_journal (at_ms, actor, entity_token, action, grant_id, '
            'capability, scope, "by", revision, note) SELECT 0, actor, entity_token, \'grant\', '
            "grant_id, capability, scope, granted_by, revision, NULL "
            "FROM agent_capability_grants WHERE grant_id=?", (grant_id,),
        )


@pytest.mark.parametrize("capability", ["channel", "never-a-capability"])
def test_a_stored_unknown_capability_grant_is_ignored_and_revocable(app_factory, capability):
    app = app_factory()
    resident(app)
    kept = grant(app)
    _store_unknown_row(app, capability)
    assert app.handle("agent.grants", {})["grants"] == [kept]
    assert app.handle("agent.grants", {"actor": "guest"})["grants"] == [kept]
    [found] = [row["grantId"] for row in journal(app)
               if row["action"] == "grant" and row["capability"] == capability]
    assert found == "old-row"
    assert app.handle("agent.revoke", {"actor": "guest", "grantId": "old-row"})["revoked"]
    last = journal(app)[-1]
    assert (last["action"], last["grantId"], last["capability"]) == ("revoke", "old-row", capability)
    assert last["scope"] == '["support-bot"]'
    assert app.handle("agent.grants", {})["grants"] == [kept]
    with closing(sqlite3.connect(app._agent_registry.database)) as db:
        assert db.execute("SELECT count(*) FROM agent_capability_grants").fetchone()[0] == 1
