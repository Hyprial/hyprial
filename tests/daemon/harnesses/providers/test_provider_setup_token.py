"""Provider login is the only default designation surface; synthetic values only."""
import base64
import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.identity import AgentSecretStore, resolve_agent_secrets
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.harnesses.providers.test_daemon_provider_login import provider_app as provider_app
from tests.support.runtime._owned_applications import owned_applications as owned_applications

FIELD = "CLAUDE_CODE_OAUTH_TOKEN"


def login(app, **extra):
    return app.handle("agent.provider.login", {
        "actor": "worker", "provider": "claude", "setupToken": secrets.token_hex(24),
        "makeDefault": True, **extra,
    })


def test_setup_token_cli_single_store_and_clear(provider_app, monkeypatch):
    app = provider_app
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **_kwargs: app.handle(method, {} if params is None else params))
    runner = CliRunner()
    token = secrets.token_hex(24)
    result = runner.invoke(cli.app, ["agent", "provider", "login", "claude", "--setup-token",
        "--actor", "worker", "--default", "--json"], input=token + "\n")
    assert result.exit_code == 0, result.output
    assert token not in result.output
    default = app.handle("agent.provider.default", {})
    assert default["credentialSource"] == "setup-token"
    assert default["defaultCredential"]["ownerActor"] == "worker"
    owner_home = Path(app._agent_registry.home_receipt("worker").path)
    assert AgentSecretStore(owner_home).get("claude", FIELD).value == token
    assert not (owner_home / "secrets").exists()
    for actor in ("resident", "wf-default"):
        if actor == "resident":
            app.handle("agent.create", {"name": actor, "preferredHarness": "claude"})
        else:
            app._ensure_agent(actor, harness="claude", interactive=False)
        grant, = app._agent_registry.secret_inventory(actor)
        assert grant.owner_actor == "worker"
        resolved, = resolve_agent_secrets(registry=app._agent_registry,
            hyprial_home=app.hyprial_home, agent_name=actor)
        assert resolved.value == token
    cleared = app.handle("agent.provider.default", {"clear": True})
    assert cleared["provider"] is None
    assert app._agent_registry.default_claude_credential() is None
    app._ensure_agent("wf-after-clear", harness="claude", interactive=False)
    assert not app._agent_registry.secret_inventory("wf-after-clear")
    assert app._agent_registry.secret_inventory("wf-default")


def test_native_default_seeds_pac_and_cli(provider_app):
    app = provider_app
    result = app.handle("agent.provider.login", {"actor": "worker", "provider": "claude",
        "files": [{"filename": ".credentials.json", "contentBase64": base64.b64encode(b'{"synthetic":true}').decode()}],
        "makeDefault": True})
    assert result["default"]["credentialSource"] == "native"
    app._ensure_agent("wf-native", harness="claude", interactive=False)
    app.handle("agent.create", {"name": "cli-native", "preferredHarness": "claude"})
    for actor in ("wf-native", "cli-native"):
        root = Path(app._agent_registry.home_receipt(actor).path)
        assert (root / "secrets/native/claude/.credentials.json").is_file()
        assert not app._agent_registry.secret_inventory(actor)


def test_default_token_requires_operator(provider_app):
    with pytest.raises(DaemonRequestError) as caught:
        provider_app.handle("agent.provider.login", {"actor": "worker", "provider": "claude",
            "setupToken": secrets.token_hex(24), "makeDefault": True}, _trusted_message_origin="worker")
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"


def test_removed_secret_default_surface(provider_app, monkeypatch):
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    result = CliRunner().invoke(cli.app, ["agent", "secret", "default", "--help"])
    assert result.exit_code != 0
    with pytest.raises(DaemonRequestError) as caught:
        provider_app.handle("agent.secret.default", {"harness": "claude"})
    assert caught.value.code == "METHOD_NOT_FOUND"


@pytest.mark.parametrize("args,input_value", [(["--source", "/unused"], "synthetic"),
    ([], "\n"), ([], "a b"), ([], "a\x00b"), ([], "x" * 16384 + "\n\nignored")])
def test_setup_token_rejects_bad_input(provider_app, monkeypatch, args, input_value):
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **_kwargs: pytest.fail("must reject before IPC"))
    result = CliRunner().invoke(cli.app, ["agent", "provider", "login", "claude", "--setup-token",
        "--actor", "worker", "--json", *args], input=input_value)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("extra", [{"provider": "pi"}, {"setupToken": ""},
    {"setupToken": "two words"}, {"setupToken": "a\x00b"}, {"files": []}])
def test_setup_token_ipc_rejects_invalid_inputs_before_storage(provider_app, extra):
    with pytest.raises(DaemonRequestError):
        login(provider_app, **extra)
    home = Path(provider_app._agent_registry.home_receipt("worker").path)
    assert not (home / "secrets.sqlite").exists()
    assert provider_app.handle("agent.provider.default", {})["provider"] is None


def test_setup_token_replace_is_explicit_and_existing_grant_survives(provider_app):
    app = provider_app
    login(app)
    app._ensure_agent("wf-consumer", harness="claude", interactive=False)
    before = app._agent_registry.secret_inventory("wf-consumer")
    with pytest.raises(DaemonRequestError):
        login(app)
    login(app, force=True)
    app._ensure_agent("wf-consumer", harness="claude", interactive=False)
    assert app._agent_registry.secret_inventory("wf-consumer") == before
    assert len(app._agent_registry.secret_inventory("worker")) == 1


@pytest.mark.parametrize("native", [False, True])
def test_default_excludes_visitors_and_other_harnesses(provider_app, native):
    app = provider_app
    if native:
        app.handle("agent.provider.login", {"actor": "worker", "provider": "claude",
            "files": [{"filename": ".credentials.json", "contentBase64": base64.b64encode(b'{}').decode()}],
            "makeDefault": True})
    else:
        login(app)
    app.handle("agent.host-invite", {"name": "visitor", "owner": "visitor-owner", "preferredHarness": "claude"})
    app.handle("agent.create", {"name": "pi-user", "preferredHarness": "pi"})
    for actor in ("visitor", "pi-user"):
        assert not app._agent_registry.secret_inventory(actor)
        assert not (Path(app._agent_registry.home_receipt(actor).path) / "secrets/native/claude").exists()


def test_legacy_default_read_has_explicit_migration_hint(provider_app, monkeypatch):
    app = provider_app
    app.handle("agent.provider.default", {"provider": "claude"})
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **_kwargs: app.handle(method, {} if params is None else params))
    result = CliRunner().invoke(cli.app, ["agent", "provider", "default", "--json"])
    assert result.exit_code == 0
    assert "--default --actor OWNER" in json.loads(result.stdout)["hint"]
    app._ensure_agent("wf-legacy", harness="claude", interactive=False)
    assert not app._agent_registry.secret_inventory("wf-legacy")
