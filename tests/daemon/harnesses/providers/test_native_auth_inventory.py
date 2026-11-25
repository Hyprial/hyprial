"""Read installed native files through the registered, owner-scoped method."""

import json
from dataclasses import replace

import pytest

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR, PAYLOAD, _files, _target, provider_app as provider_app,
)
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def test_import_read_replace_remove_returns_metadata_without_credentials(provider_app, monkeypatch, caplog):
    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    assert app.handle("agent.provider.auth.list", {}) == {"ok": True, "records": []}
    app.handle("agent.provider.login", {
        "actor": ACTOR, "provider": "claude", "files": _files((".credentials.json", PAYLOAD)),
    })
    record = app.handle("agent.provider.auth.list", {})["records"][0]
    assert record == {
        "actor": app.agents.require(ACTOR).uri,
        "displayName": app.agents.require(ACTOR).display_name or app.agents.require(ACTOR).actor,
        "harness": "claude", "state": "stored", "files": [".credentials.json"],
        "missingFiles": [], "errorCode": None, "importedAt": 1000, "startupConfirmedAt": None,
    }
    assert "test-only" not in json.dumps(record)
    assert "test-only" not in caplog.text
    assert str(app.hyprial_home) not in json.dumps(record)
    assert app.handle("agent.provider.auth.list", {"actor": ACTOR, "harness": "codex"})["records"] == []
    app.handle("agent.provider.logout", {"actor": ACTOR, "provider": "claude"})
    assert app.handle("agent.provider.auth.list", {})["records"] == []


def test_partial_pi_and_unsafe_files_are_not_empty_or_usable(provider_app):
    app = provider_app
    app.handle("agent.provider.login", {"actor": ACTOR, "provider": "pi", "files": _files(
        ("auth.json", b"synthetic-auth"), ("models-store.json", b"synthetic-models"),
    )})
    _target(app, "pi", "models-store.json").unlink()
    row = app.handle("agent.provider.auth.list", {"actor": ACTOR})["records"][0]
    assert row["state"] == "partial"
    assert row["files"] == ["auth.json"]
    assert row["missingFiles"] == ["models-store.json"]
    auth_target = _target(app, "pi", "auth.json")
    auth_target.chmod(0o644)
    row = app.handle("agent.provider.auth.list", {})["records"][0]
    assert row["state"] == "partial"
    assert row["files"] == ["auth.json"]
    auth_target.unlink()
    auth_target.mkdir()
    row = app.handle("agent.provider.auth.list", {})["records"][0]
    assert row["state"] == "unavailable"
    assert row["errorCode"] == "PROVIDER_UNSAFE_PATH"
    assert row["files"] == []


def test_inventory_rejects_worker_and_invalid_filter_before_filesystem_access(provider_app, monkeypatch):
    app = provider_app
    monkeypatch.setattr(app.agents, "list", lambda: pytest.fail("unauthorized inventory read"))
    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.provider.auth.list", {}, _trusted_message_origin="worker")
    assert refused.value.code == "CALLER_NOT_AUTHORIZED"
    with pytest.raises(DaemonRequestError) as invalid:
        app.handle("agent.provider.auth.list", {"harness": "../claude"})
    assert invalid.value.code == "INVALID_ARGUMENT"


def test_inventory_spec_is_read_only_http_with_no_caller_from_body():
    spec = next(spec for spec in METHOD_REGISTRY.specs if spec.name == "agent.provider.auth.list")
    assert spec.mutates is False
    assert spec.retry == "safe"
    assert spec.audience == ("local-operator",)
    assert spec.http_exposed
    assert spec.http_binding.identity.mode == "none"


def test_inventory_filters_foreign_agents_and_refuses_explicit_foreign_target(provider_app, monkeypatch):
    app = provider_app
    agent = app.agents.require(ACTOR)
    foreign = replace(agent, owner="another-owner")
    monkeypatch.setattr(app.agents, "list", lambda: [foreign])
    monkeypatch.setattr(app.agents, "require", lambda actor: foreign)
    monkeypatch.setattr(app._agent_registry, "home_receipt", lambda *args, **kwargs: pytest.fail("foreign home accessed"))
    assert app.handle("agent.provider.auth.list", {})["records"] == []
    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.provider.auth.list", {"actor": ACTOR})
    assert refused.value.code == "TARGET_NOT_AUTHORIZED"


def test_empty_file_and_symlink_remain_visible_without_reading_target(provider_app, tmp_path):
    app = provider_app
    app.handle("agent.provider.login", {"actor": ACTOR, "provider": "codex", "files": _files(("auth.json", b"synthetic"))})
    target = _target(app, "codex", "auth.json")
    target.write_bytes(b"")
    assert app.handle("agent.provider.auth.list", {"harness": "codex"})["records"][0]["state"] == "partial"
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"must-not-read")
    target.symlink_to(outside)
    row = app.handle("agent.provider.auth.list", {"harness": "codex"})["records"][0]
    assert row["state"] == "unavailable"
    assert row["errorCode"] == "PROVIDER_UNSAFE_PATH"
    assert "must-not-read" not in json.dumps(row)
    assert str(outside) not in json.dumps(row)


def test_http_import_list_replace_remove_uses_real_application(provider_app, tmp_path):
    from hyprial.shell.impl.request_http.server import RequestHttpServer
    from tests.shell.request_http.test_agent_reference import _config, _fake_request_once, _post

    app = provider_app
    calls = []
    discovery = _fake_request_once([])

    def dispatch(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return discovery(method, params, **kwargs)
        calls.append(method)
        return app.handle(method, params)

    server = RequestHttpServer(_config(tmp_path), request_once=dispatch)
    server.start()
    try:
        status, _ = _post(server, "agent.provider.auth.list", {}, token=None)
        assert status == 401
        assert calls == []
        for harness, names in (("claude", [".credentials.json"]), ("codex", ["auth.json"]), ("pi", ["auth.json", "models-store.json"])):
            for content in (b"synthetic-original", b"synthetic-replacement"):
                status, result = _post(server, "agent.provider.login", {
                    "actor": ACTOR, "provider": harness, "force": True,
                    "files": _files(*((name, content) for name in names)),
                })
                assert status == 200, result
                status, result = _post(server, "agent.provider.auth.list", {"actor": ACTOR, "harness": harness})
                assert status == 200, result
                assert result["result"]["records"][0]["state"] == "stored"
                assert "synthetic-" not in json.dumps(result)
            status, result = _post(server, "agent.provider.logout", {"actor": ACTOR, "provider": harness})
            assert status == 200, result
            status, result = _post(server, "agent.provider.auth.list", {"actor": ACTOR, "harness": harness})
            assert status == 200
            assert result["result"]["records"] == []
    finally:
        server.shutdown()
