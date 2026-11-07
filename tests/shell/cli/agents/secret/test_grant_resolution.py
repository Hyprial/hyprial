"""CLI writes and grants share the start-time resolver's value authority."""

from __future__ import annotations

import hmac
import secrets
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.identity import AgentRegistry, SecretResolutionError, SecretResolver, SecretSource
from hyprial.shell.impl.cli.commands.agent import secret as secret_commands
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli


def _matches(resolver: SecretResolver, expected: str) -> bool:
    matches = hmac.compare_digest(resolver.resolve("worker", "routes").value, expected)
    if not matches:
        pytest.fail("resolved secret does not match", pytrace=False)
    return matches


def test_cli_set_grant_rotate_remove_resolves_same_store(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner", machine="node", hyprial_home=home,
    )
    registry.create("worker")
    resolver = SecretResolver(home, registry)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: home)
    monkeypatch.setattr(secret_commands, "_local_operator_identity", lambda: "user:owner")

    def grant_request(method, params):
        assert method == "agent.secret.grant"
        registry.grant_secret(
            params["actor"], grant_id=params["grantId"],
            source=SecretSource(params["source"]), entry_id=params["entryId"],
            field_name=params["fieldName"], environment_names=tuple(params["environmentNames"]),
            revision=params["revision"],
        )
        return {"granted": True}

    # Exercise the real registry at the daemon IPC seam; never connect to a daemon.
    monkeypatch.setattr(_cli_support, "_daemon_request", grant_request)
    runner = CliRunner()
    values = [secrets.token_hex(24), secrets.token_hex(24)]
    base = ["--agent", "worker", "--service", "cloudflare-routes"]
    field = ["--field", "CLOUDFLARE_API_TOKEN"]
    try:
        written = runner.invoke(
            cli.app, ["agent", "secret", "set", *base, *field, "--no-validate", "--json"],
            input=values[0] + "\n",
        )
        assert written.exit_code == 0
        granted = runner.invoke(cli.app, [
            "agent", "secret", "grant", "worker", "--grant-id", "routes",
            "--source", "agent-private", "--entry-id", "cloudflare-routes",
            "--field-name", "CLOUDFLARE_API_TOKEN", "--environment-name", "CLOUDFLARE_API_TOKEN",
            "--revision", "1", "--json",
        ])
        assert granted.exit_code == 0
        assert _matches(resolver, values[0])
        rotated = runner.invoke(
            cli.app, ["agent", "secret", "rotate", *base, *field, "--no-validate", "--json"],
            input=values[1] + "\n",
        )
        assert rotated.exit_code == 0
        assert _matches(resolver, values[1])
        removed = runner.invoke(cli.app, ["agent", "secret", "remove", *base, "--json"])
        assert removed.exit_code == 0
        with pytest.raises(SecretResolutionError) as error:
            resolver.resolve("worker", "routes")
        assert error.value.category == "missing"
        assert "hyprial agent secret set --agent worker" in str(error.value)
        # Execute the exact repair command from the error; do not reconstruct it.
        command = str(error.value).split("stdin to: ", 1)[1].split(". For a validated", 1)[0]
        repaired = runner.invoke(cli.app, shlex.split(command)[1:], input=values[1] + "\n")
        assert repaired.exit_code == 0
        assert _matches(resolver, values[1])
        outputs = (
            written.output, granted.output, rotated.output, removed.output,
            repaired.output, str(error.value),
        )
        assert all(value not in output for value in values for output in outputs)
    finally:
        registry.close()
