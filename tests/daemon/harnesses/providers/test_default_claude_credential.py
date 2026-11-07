"""Real daemon application / agent-owner dispatch for credential references."""
from pathlib import Path
import json
import secrets

import pytest
from typer.testing import CliRunner

from hyprial.identity import AgentSecretStore
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.harnesses.providers.test_daemon_provider_login import provider_app as provider_app
from tests.support.runtime._owned_applications import owned_applications as owned_applications

FIELD = "CLAUDE_CODE_OAUTH_TOKEN"


def seed(app):
    store = AgentSecretStore(Path(app._agent_registry.home_receipt("worker").path))
    store.set(service="claude", field=FIELD, value=secrets.token_hex(24), actor="user:owner", via="cli", at_ms=1)


def test_default_ipc_cli_create_and_pac_ensure(provider_app, monkeypatch):
    app = provider_app
    seed(app)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params, **_kwargs: app.handle(method, params))
    runner = CliRunner()
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    result = runner.invoke(cli.app, ["agent", "provider", "login", "claude", "--actor", "worker", "--setup-token", "--force", "--default", "--json"], input=secrets.token_hex(24))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["default"]["defaultCredential"]["ownerActor"] == "worker"
    app.handle("agent.create", {"name": "resident", "preferredHarness": "claude"})
    app._ensure_agent("wf-synthetic", harness="claude", interactive=False)
    for actor in ("resident", "wf-synthetic"):
        result = runner.invoke(cli.app, ["agent", "secret", "ls", actor, "--json"])
        assert result.exit_code == 0, result.output
        grant, = json.loads(result.stdout)["grants"]
        assert grant["source"] == "agent-shared"
        assert grant["ownerActor"] == "worker"
    assert app.agents.require("wf-synthetic").capabilities["creationOrigin"] == "managed-launch"
    assert "creationOrigin" not in app.agents.require("resident").capabilities
    app._ensure_agent("resident", harness="claude", interactive=True)
    assert "creationOrigin" not in app.agents.require("resident").capabilities
    assert app.agents.require("wf-synthetic").config is None
    assert app.agents.require("resident").config is not None
    app.handle("agent.secret.revoke", {"actor": "wf-synthetic", "grantId": "default-claude"})
    app._ensure_agent("wf-synthetic", harness="claude", interactive=False)
    assert app.handle("agent.secret.list", {"actor": "wf-synthetic"})["grants"] == []


def test_worker_cannot_change_default(provider_app):
    seed(provider_app)
    with pytest.raises(DaemonRequestError) as caught:
        provider_app.handle("agent.provider.login", {"actor": "worker", "provider": "claude", "setupToken": secrets.token_hex(24), "makeDefault": True},
                            _trusted_message_origin="worker")
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"


def test_shared_grant_cli_and_invalid_default(provider_app, monkeypatch):
    app = provider_app
    seed(app)
    app.handle("agent.create", {"name": "consumer"})
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params, **_kwargs: app.handle(method, params))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["agent", "secret", "grant", "consumer", "--source", "agent-shared",
        "--owner-agent", "worker", "--entry-id", "claude", "--field-name", FIELD,
        "--environment-name", FIELD, "--grant-id", "custom", "--revision", "1", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["grant"]["ownerActor"] == "worker"
    with pytest.raises(DaemonRequestError):
        app.handle("agent.provider.login", {"actor": "worker", "provider": "claude", "setupToken": "", "makeDefault": True})
    assert app.handle("agent.provider.default", {})["provider"] is None
