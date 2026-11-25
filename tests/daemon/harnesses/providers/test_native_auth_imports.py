"""Import provenance belongs to the current native file set, never a timestamp guess."""

import json
import os

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR, _files, _target, provider_app as provider_app,
)
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def _login(app, harness, names, content=b"test-only-original", **params):
    return app.handle("agent.provider.login", {
        "actor": ACTOR, "provider": harness,
        "files": _files(*((name, content) for name in names)), **params,
    })


def _record(app, harness):
    return app.handle("agent.provider.auth.list", {"actor": ACTOR, "harness": harness})["records"][0]


@pytest.mark.parametrize("harness,names", [
    ("claude", [".credentials.json"]), ("codex", ["auth.json"]),
    ("pi", ["auth.json", "models-store.json"]),
    ("agy", ["antigravity-oauth-token"]),
])
def test_import_time_survives_read_reuse_and_changes_only_after_replacement(provider_app, monkeypatch, harness, names):
    app = provider_app
    at = [1000]
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: at[0])
    _login(app, harness, names)
    row = _record(app, harness)
    assert row["importedAt"] == 1000
    assert row["startupConfirmedAt"] is None
    assert "test-only" not in json.dumps(row)
    at[0] = 2000
    _login(app, harness, names)
    assert _record(app, harness)["importedAt"] == 1000
    with pytest.raises(DaemonRequestError):
        _login(app, harness, names, b"test-only-replacement")
    assert _record(app, harness)["importedAt"] == 1000
    _login(app, harness, names, b"test-only-replacement", force=True)
    assert _record(app, harness)["importedAt"] == 2000
    app.handle("agent.provider.logout", {"actor": ACTOR, "provider": harness})
    assert app.handle("agent.provider.auth.list", {"harness": harness})["records"] == []
    at[0] = 3000
    _login(app, harness, names)
    assert _record(app, harness)["importedAt"] == 3000


def test_same_size_external_change_cannot_reuse_an_import_receipt(provider_app, monkeypatch):
    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    _login(app, "codex", ["auth.json"], b"old-secret")
    assert _record(app, "codex")["importedAt"] == 1000
    target = _target(app, "codex", "auth.json")
    old = target.stat()
    target.write_bytes(b"new-secret")
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    row = _record(app, "codex")
    assert row["state"] == "stored"
    assert row["importedAt"] is None
    assert row["startupConfirmedAt"] is None
    assert "secret" not in json.dumps(row)


def test_partial_write_residue_never_reuses_import_receipt(provider_app, monkeypatch):
    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    _login(app, "pi", ["auth.json", "models-store.json"])
    assert _record(app, "pi")["importedAt"] == 1000
    target = _target(app, "pi", "auth.json.previous")
    target.write_bytes(b"old-secret")
    target.chmod(0o600)
    row = _record(app, "pi")
    assert row["state"] == "partial"
    assert row["importedAt"] is None


def test_preexisting_native_files_do_not_gain_provenance_from_mtime(provider_app, monkeypatch):
    app = provider_app
    from hyprial.identity import install_native_credential_set
    from tests.daemon.harnesses.providers.test_daemon_provider_login import _agent_home

    install_native_credential_set(_agent_home(app), "codex", [("auth.json", b"legacy")])
    assert _record(app, "codex")["state"] == "stored"
    assert _record(app, "codex")["importedAt"] is None
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 2000)
    _login(app, "codex", ["auth.json"], b"legacy")
    assert _record(app, "codex")["importedAt"] == 2000


def test_record_persists_in_registry_and_not_in_agent_home(provider_app, monkeypatch):
    from hyprial.identity import AgentRegistry, inspect_native_credentials
    from tests.daemon.harnesses.providers.test_daemon_provider_login import _agent_home

    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    _login(app, "codex", ["auth.json"])
    root = _target(app, "codex", "auth.json").parent
    assert {p.name for p in root.iterdir()} == {"auth.json"}
    registry = AgentRegistry(app._agent_registry.database, owner=app.owner, machine=app.node_id,
                             hyprial_home=app.hyprial_home)
    try:
        row = inspect_native_credentials(_agent_home(app), "codex",
            import_record=registry.native_credential_import_record(ACTOR))
        assert row["importedAt"] == 1000
    finally:
        registry.close()


def test_receipt_write_failure_is_not_success_or_old_provenance(provider_app, monkeypatch):
    import sqlite3
    from hyprial.identity.impl.agents.home.native_imports import NativeImportRecord

    app = provider_app
    _login(app, "codex", ["auth.json"])

    def fail(*args):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(NativeImportRecord, "write", fail)
    with pytest.raises(DaemonRequestError) as refused:
        _login(app, "codex", ["auth.json"], b"test-only-new", force=True)
    assert refused.value.code == "DAEMON_ERROR"
    assert _record(app, "codex")["importedAt"] is None
    assert _target(app, "codex", "auth.json").read_bytes() == b"test-only-new"


def test_crash_between_file_install_and_record_commit_stays_unknown(provider_app, monkeypatch):
    from hyprial.identity.impl.agents.home import native_credential

    class Crash(BaseException):
        pass

    app = provider_app
    _login(app, "codex", ["auth.json"])
    write = native_credential._write_plan

    def crash(*args):
        write(*args)
        raise Crash()

    monkeypatch.setattr(native_credential, "_write_plan", crash)
    with pytest.raises(Crash):
        _login(app, "codex", ["auth.json"], b"test-only-new", force=True)
    assert _record(app, "codex")["importedAt"] is None


def test_rolled_back_install_preserves_matching_old_record(provider_app, monkeypatch):
    from pathlib import Path
    from hyprial.identity.impl.agents.home import native_credential

    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    _login(app, "pi", ["auth.json", "models-store.json"])
    replace_file = native_credential.os.replace

    def fail_last(source, destination, *args, **kwargs):
        if Path(source).name == "models-store.json.incoming":
            raise OSError(5, "injected write failure")
        return replace_file(source, destination, *args, **kwargs)

    monkeypatch.setattr(native_credential.os, "replace", fail_last)
    with pytest.raises(DaemonRequestError):
        _login(app, "pi", ["auth.json", "models-store.json"], b"test-only-new", force=True)
    assert _record(app, "pi")["importedAt"] == 1000


def test_same_payload_with_wrong_entity_or_home_resource_does_not_match(provider_app):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    assert _record(app, "codex")["importedAt"] is not None
    with app._agent_registry._db:
        app._agent_registry._db.execute("UPDATE native_auth_imports SET resource_token='obsolete'")
    assert _record(app, "codex")["importedAt"] is None


@pytest.mark.parametrize("harness,names", [
    ("claude", [".credentials.json"]), ("codex", ["auth.json"]),
    ("pi", ["auth.json", "models-store.json"]),
    ("agy", ["antigravity-oauth-token"]),
])
def test_large_replacement_preserves_login_without_reusing_import_time(provider_app, harness, names):
    app = provider_app
    _login(app, harness, names)
    assert _record(app, harness)["importedAt"] is not None
    payload = b"x" * (1024 * 1024 + 1)
    result = _login(app, harness, names, payload, force=True)
    assert all(item["bytes"] == len(payload) for item in result["files"])
    assert all(_target(app, harness, name).read_bytes() == payload for name in names)
    assert _record(app, harness)["state"] == "stored"
    assert _record(app, harness)["importedAt"] is None
    assert app._agent_registry._db.execute("SELECT count(*) FROM native_auth_imports").fetchone()[0] == 0


def test_digest_read_is_bounded_after_external_replacement(provider_app):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    target = _target(app, "codex", "auth.json")
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    assert _record(app, "codex")["importedAt"] is None


@pytest.mark.parametrize("harness,names", [
    ("claude", [".credentials.json"]), ("codex", ["auth.json"]),
    ("pi", ["auth.json", "models-store.json"]),
    ("agy", ["antigravity-oauth-token"]),
])
def test_authority_rotation_revokes_import_receipt_but_alias_changes_preserve_it(provider_app, harness, names):
    app = provider_app
    registry = app._agent_registry
    _login(app, harness, names)
    old_time = _record(app, harness)["importedAt"]
    original = registry.require(ACTOR)
    registry.update(ACTOR, display_name="renamed")
    assert _record(app, harness)["importedAt"] == old_time
    rotated = registry.rotate_secret_authority(ACTOR)
    assert rotated.entity_token != original.entity_token
    assert registry._db.execute("SELECT count(*) FROM native_auth_imports WHERE actor=?", (ACTOR,)).fetchone()[0] == 0
    assert registry._db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _record(app, harness)["importedAt"] is None


def test_failed_authority_rotation_rolls_back_import_receipt_revocation(provider_app, monkeypatch):
    app = provider_app
    registry = app._agent_registry
    _login(app, "codex", ["auth.json"])
    original = registry.require(ACTOR)
    old_time = _record(app, "codex")["importedAt"]

    def fail(*args):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(registry, "_record_external_resource_locked", fail)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        registry.rotate_secret_authority(ACTOR)
    assert registry.require(ACTOR).entity_token == original.entity_token
    assert _record(app, "codex")["importedAt"] == old_time
    assert registry._db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_registry_record_cannot_be_applied_to_another_home(provider_app, tmp_path):
    from hyprial.identity import NativeCredentialError, install_native_credential_set

    app = provider_app
    context = app._agent_registry.native_credential_import_record(ACTOR)
    other = tmp_path / "other-home"
    other.mkdir(mode=0o700)
    with pytest.raises(NativeCredentialError):
        install_native_credential_set(other, "codex", [("auth.json", b"test-only")], import_record=context)
    assert list(other.iterdir()) == []
    assert app._agent_registry._db.execute("SELECT count(*) FROM native_auth_imports").fetchone()[0] == 0


def test_old_record_context_refuses_a_changed_home_resource_before_writing(provider_app, monkeypatch):
    from dataclasses import replace
    from hyprial.identity import NativeCredentialError, install_native_credential_set
    from tests.daemon.harnesses.providers.test_daemon_provider_login import _agent_home

    app = provider_app
    _login(app, "codex", ["auth.json"])
    home = _agent_home(app)
    context = app._agent_registry.native_credential_import_record(ACTOR)
    receipt = app._agent_registry.home_receipt(ACTOR, require_ready=False)
    monkeypatch.setattr(app._agent_registry, "home_receipt", lambda *args, **kwargs: replace(receipt, resource_token="replacement"))
    with pytest.raises(NativeCredentialError):
        install_native_credential_set(home, "codex", [("auth.json", b"test-only-new")], force=True, import_record=context)
    assert (home / "secrets/native/codex/auth.json").read_bytes() == b"test-only-original"
