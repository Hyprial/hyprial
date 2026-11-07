"""Capability ledger journeys through DaemonApplication, without workers."""

import sqlite3
import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition import DomainCommandError
from hyprial.kernel import ipc_errors
from tests.daemon.organizations.invite import test_host_invite

app_factory = test_host_invite.app_factory
home_files = test_host_invite.home_files


def invite(app):
    return app.handle("agent.host-invite", {"name": "guest", "owner": "visitor"})


def grant(app, **overrides):
    return app.handle("agent.grant", {
        "actor": "guest", "grantId": "g1", "capability": "send-to",
        "scope": '["agent:visitor:elsewhere:helper"]', "revision": 1,
        **overrides,
    })["grant"]


def journal(app):
    return app.handle("agent.grants", {"actor": "guest", "audit": True})["journal"]


def test_invitation_journal(app_factory):
    app = app_factory()
    # Ordinary creation does not fabricate a host invitation.
    app.handle("agent.create", {"name": "native"})
    assert app.handle("agent.grants", {"actor": "native", "audit": True})["journal"] == []
    invite(app)
    rows = journal(app)
    assert len(rows) == 1
    assert rows[0]["action"] == "host-invite"
    assert rows[0]["by"] == "user:host-owner"
    assert rows[0]["note"] == "ownerAsserted=host"
    assert rows[0]["atMs"] > 0
    # The frozen registry seam may be called again by an integration; it does
    # not append another invitation. Creation/assertions above use production IPC.
    app._agent_registry.record_host_invite("guest", by="user:host-owner", note="ownerAsserted=host")
    assert journal(app) == rows


def test_ledger_revision_revoke_restart_destroy(app_factory):
    app = app_factory()
    invite(app)
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
    assert app.handle("agent.destroy", {"name": "guest"})["destroyed"]
    assert journal(app) == before
    assert app.handle("agent.grants", {})["grants"] == []
    invite(app)
    assert app.handle("agent.grants", {"actor": "guest"})["grants"] == []
    assert journal(app)[:-1] == before
    assert journal(app)[-1]["entityToken"] != first["entityToken"]


@pytest.mark.parametrize("capability,scope,bad", [
    ("agent-home", "self", "other"),
    ("isolation", "directory", "host"),
    ("org-context", "accepted", "pending"),
    ("see-actors", '["agent:visitor:node:actor"]', '["actor"]'),
    ("send-to", '["user:visitor"]', '["route:outside"]'),
    ("tool-surface", '["harness_read"]', '[]'),
    ("channel", '["support-bot"]', '[""]'),
    ("shared-path", '{"path":"/srv/shared","mode":"ro"}', '{"path":"relative","mode":"ro"}'),
])
def test_scope_closed_schema(app_factory, capability, scope, bad):
    app = app_factory()
    invite(app)
    with pytest.raises(DaemonRequestError):
        grant(app, capability=capability, scope=bad)
    assert app.handle("agent.grants", {})["grants"] == []
    assert len(journal(app)) == 1
    assert grant(app, capability=capability, scope=scope)["scope"] == scope
    if capability == "agent-home":
        with pytest.raises(DaemonRequestError, match="intrinsic"):
            app.handle("agent.revoke", {"actor": "guest", "grantId": "g1"})
        assert len(app.handle("agent.grants", {})["grants"]) == 1


def test_stale_incarnation_is_not_an_active_grant(app_factory):
    app = app_factory()
    invite(app)
    original = grant(app)
    # Persist a broken FK as a fault injection, not a new rotation endpoint.
    # Read through the real daemon query: token mismatch must be fail-closed.
    with sqlite3.connect(app._agent_registry.database) as db:
        db.execute("UPDATE agent_capability_grants SET entity_token='stale'")
    assert app.handle("agent.grants", {})["grants"] == []
    assert app.handle("agent.grants", {"actor": "guest"})["grants"] == []
    assert journal(app)[-1]["entityToken"] == original["entityToken"]


def test_invite_and_grant_roll_back_if_journal_write_fails(app_factory):
    app = app_factory()
    database = app._agent_registry.database
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON agent_grant_journal "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    before = home_files(app.hyprial_home)
    with pytest.raises(DaemonRequestError) as failed_invite:
        invite(app)
    assert failed_invite.value.code == ipc_errors.AGENT_CREATE_COMMIT_FAILED
    assert app.agents.get("guest") is None
    assert home_files(app.hyprial_home) == before
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER reject_audit")
    invite(app)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON agent_grant_journal "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises((DaemonRequestError, sqlite3.IntegrityError)):
        grant(app)
    assert app.handle("agent.grants", {})["grants"] == []
    assert len(journal(app)) == 1


def test_invite_commit_failure_waits_for_home_cleanup(app_factory, monkeypatch):
    app = app_factory()
    with sqlite3.connect(app._agent_registry.database) as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON agent_grant_journal "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    provisioner = app._agent_registry.home_provisioner
    original_cleanup = provisioner.cleanup
    entered = threading.Event()
    release = threading.Event()

    def held_cleanup(receipt, *, expected_token):
        entered.set()
        assert release.wait(5.0)
        return original_cleanup(receipt, expected_token=expected_token)

    monkeypatch.setattr(provisioner, "cleanup", held_cleanup)
    outcomes: list[DaemonRequestError] = []

    def run_invite():
        try:
            invite(app)
        except DaemonRequestError as error:
            outcomes.append(error)

    caller = threading.Thread(target=run_invite)
    caller.start()
    try:
        assert entered.wait(3.0)
        assert caller.is_alive(), "invite returned before accepted home cleanup"
        assert app.agents.get("guest") is None
        assert any(
            path.name == "home-receipt.json"
            for path in (app.hyprial_home / "agents/guest").rglob("*")
        )
    finally:
        release.set()
        caller.join(5.0)
    assert len(outcomes) == 1
    assert outcomes[0].code == ipc_errors.AGENT_CREATE_COMMIT_FAILED
    assert not (app.hyprial_home / "agents/guest").exists()
    with sqlite3.connect(app._agent_registry.database) as db:
        assert db.execute(
            "SELECT 1 FROM lifecycle_resources WHERE resource_key=?",
            ("agent-home-effect:create:guest",),
        ).fetchone() is None


def test_invite_rollback_pending_replays_exact_entity_after_restart(
    app_factory, monkeypatch,
):
    app = app_factory()
    database = app._agent_registry.database
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON agent_grant_journal "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    def busy_settlement(*_args, **_kwargs):
        raise DomainCommandError("PORT_OVERLOADED", "synthetic cleanup overload")

    monkeypatch.setattr(app.agents, "settle_destroy", busy_settlement)
    with pytest.raises(DaemonRequestError) as pending:
        invite(app)
    assert pending.value.code == ipc_errors.AGENT_CREATE_ROLLBACK_PENDING
    assert "synthetic cleanup overload" in str(pending.value)
    with sqlite3.connect(database) as db:
        claim = db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
            ("agent-home-effect:create:guest",),
        ).fetchone()
        record = db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
            ("agent-record:guest",),
        ).fetchone()
        assert claim is not None and record is not None
        import json
        token = json.loads(record[0])["entityToken"]
    receipt_path = app.hyprial_home / "agents/guest/state/home-receipt.json"
    assert receipt_path.exists()
    app._close()
    reopened = app_factory()
    settled = reopened.agents.settle_destroy(
        "retry-original-guest-rollback", "guest",
        expected_entity_token=token,
    )
    assert settled.disposition == "destroyed"
    assert not receipt_path.exists()
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT 1 FROM lifecycle_resources WHERE resource_key=?",
            ("agent-home-effect:create:guest",),
        ).fetchone() is None
        db.execute("DROP TRIGGER reject_audit")
    successor = invite(reopened)["agent"]
    assert successor["actor"] == "guest"
    assert reopened.agents.require("guest").entity_token != token


def test_owner_rename_preserves_historical_attribution(app_factory):
    app = app_factory()
    app.handle("agent.create", {"name": "native"})
    invite(app)
    grant(app)
    before = journal(app)
    app._close()
    app = app_factory(owner="renamed-host")
    assert journal(app) == before
    assert app.handle("agent.grants", {})["grants"][0]["grantedBy"] == "user:renamed-host"
