"""Saved entries and grant revocation are separate, non-secret operator operations."""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import SecretResolver
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app

LIST = "agent.secret.provider-list"
REVOKE = "agent.secret.revoke"
SENTINEL = "private-value-must-never-appear"


def invoke(app, method=LIST, params=None, origin="ipc"):
    return METHOD_REGISTRY.invoke(
        app, method, {} if params is None else params, trusted_message_origin=origin,
    )


def save(app, entry="deepseek"):
    resolver = SecretResolver(app.hyprial_home, app._agent_registry)
    return resolver.write_user_provider(entry, {"DEEPSEEK_API_KEY": SENTINEL})


def test_list_includes_saved_but_ungranted_entries_without_values_or_other_custody(app):
    save(app, "second")
    save(app, "first")
    for filename in ("login.json", "lark-test.json"):
        (app.hyprial_home / "secrets" / filename).write_text("not-readable-json")
    result = invoke(app)
    assert result == {"ok": True, "entries": [
        {"entryId": "first", "fieldNames": ["DEEPSEEK_API_KEY"]},
        {"entryId": "second", "fieldNames": ["DEEPSEEK_API_KEY"]},
    ]}
    assert app.handle("agent.secret.list", {})["grants"] == []
    assert SENTINEL not in json.dumps(result)
    assert str(app.hyprial_home) not in json.dumps(result)


def test_empty_list_does_not_create_secret_directories(app):
    assert invoke(app) == {"ok": True, "entries": []}
    assert not (app.hyprial_home / "secrets").exists()


@pytest.mark.parametrize("damage", ["json", "mode", "link", "hardlink", "directory", "large", "field"])
def test_unreadable_entry_fails_without_secret_or_private_path(app, tmp_path, damage):
    path = save(app)
    if damage == "json":
        path.write_text(SENTINEL)
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "link":
        target = tmp_path / "outside.json"
        path.replace(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        (tmp_path / "alias.json").hardlink_to(path)
    elif damage == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif damage == "large":
        path.write_text(json.dumps({"key": "x" * (1024 * 1024)}))
    else:
        path.write_text(json.dumps({"../invalid": SENTINEL}))
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app)
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert SENTINEL not in str(caught.value)
    assert str(path) not in str(caught.value)


def test_linked_catalog_directory_is_not_followed(app, tmp_path):
    path = save(app)
    directory = path.parent
    target = tmp_path / "outside"
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app)
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert (target / path.name).exists()


@pytest.mark.parametrize("gate", ["owner-keys", "bad"])
def test_catalog_does_not_bypass_another_or_damaged_authority(app, gate):
    save(app)
    settings = app.hyprial_home / "settings.json"
    settings.write_text(json.dumps({"providerKeys": {"authority": gate}}))
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app)
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED


@pytest.mark.parametrize("origin", [None, "worker"])
def test_catalog_requires_local_ipc_origin(app, origin):
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app, origin=origin)
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_catalog_refuses_interactive_peer_before_reading(app, monkeypatch):
    app._ipc_peer.pid = 123
    monkeypatch.setattr(app, "_interactive_identity_binding_peer", lambda pid: True)
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app)
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_revoke_exposes_existing_handler_without_deleting_saved_entry(app):
    save(app)
    app._agent_registry.create("worker")
    app.handle("agent.secret.grant", {
        "actor": "worker", "grantId": "model", "source": "user-provider",
        "entryId": "deepseek", "fieldName": "DEEPSEEK_API_KEY",
        "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
    })
    assert invoke(app, REVOKE, {"actor": "worker", "grantId": "model"}) == {"ok": True, "revoked": True}
    assert invoke(app, REVOKE, {"actor": "worker", "grantId": "model"}) == {"ok": True, "revoked": False}
    assert app.handle("agent.secret.list", {"actor": "worker"})["grants"] == []
    assert invoke(app)["entries"] == [{"entryId": "deepseek", "fieldNames": ["DEEPSEEK_API_KEY"]}]


def test_catalog_and_revoke_http_contracts_are_explicit():
    read = METHOD_REGISTRY.get(LIST).spec
    revoke = METHOD_REGISTRY.get(REVOKE).spec
    assert read.http_exposed and read.audience == ("local-operator",)
    assert not read.mutates and read.retry == "safe"
    assert read.params_schema["additionalProperties"] is False
    assert revoke.http_exposed and revoke.mutates and revoke.retry == "never"
    assert revoke.http_binding.identity.mode == "none"
    assert revoke.params_schema["properties"]["expectedAgent"]["x-hyprial-http-forbidden"] is True
