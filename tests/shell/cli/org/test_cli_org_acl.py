"""CLI shape of ``hyprial org acl grant|revoke|ls`` (frozen interface).

Behavior lives behind the daemon IPC (see
``tests/daemon/org/acl_entry_gate/``); these tests pin the command surface
the dispatch froze — ``org acl grant <org> <agent> [--until <moment>]`` /
``revoke <org> <agent>`` / ``ls <org> [--json]`` — using the established
org-CLI test seam: the daemon transport is replaced by a recorder and the
assertions check the mapped IPC method, the frozen parameters, and the
one-JSON-object ``--json`` discipline.

Red against base 381acfa10: the ``org acl`` command group does not exist,
so every invocation exits 2 with ``No such command`` instead of 0.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

AGENT = "agent:alice:node-a:gate-deputy"
UNTIL = "2100-01-01T00:00:00+00:00"
# The daemon IPC freezes the deadline as epoch milliseconds (PR #1305):
# the CLI parses ``--until`` and sends ``untilMs``, never the raw string.
UNTIL_MS = int(datetime.fromisoformat(UNTIL).timestamp() * 1000)


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_SENDER_AUTH_MODE", raising=False)


def _recorder(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    def request(method: str, params: dict[str, Any] | None = None, **_kwargs: Any):
        observed.update(method=method, params=params)
        return answer

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return observed


def test_acl_grant_maps_org_agent_and_until_to_daemon_ipc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    observed = _recorder(monkeypatch, {"org": "group-acme", "agent": AGENT})

    result = CliRunner().invoke(
        _cli_root_app,
        ["org", "acl", "grant", "group-acme", AGENT, "--until", UNTIL, "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert observed["method"] == "org.acl.grant"
    assert observed["params"]["org"] == "group-acme"
    assert observed["params"]["agent"] == AGENT
    assert observed["params"]["untilMs"] == UNTIL_MS


def test_acl_grant_without_until_sends_no_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    observed = _recorder(monkeypatch, {"org": "group-acme", "agent": AGENT})

    result = CliRunner().invoke(
        _cli_root_app, ["org", "acl", "grant", "group-acme", AGENT, "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert observed["method"] == "org.acl.grant"
    assert observed["params"]["org"] == "group-acme"
    assert observed["params"]["agent"] == AGENT
    assert observed["params"].get("untilMs") is None


def test_acl_revoke_maps_to_daemon_ipc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    observed = _recorder(monkeypatch, {"org": "group-acme", "agent": AGENT})

    result = CliRunner().invoke(
        _cli_root_app, ["org", "acl", "revoke", "group-acme", AGENT, "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert observed["method"] == "org.acl.revoke"
    assert observed["params"]["org"] == "group-acme"
    assert observed["params"]["agent"] == AGENT


def test_acl_ls_maps_to_daemon_ipc_and_emits_one_json_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    observed = _recorder(monkeypatch, {"org": "group-acme", "grants": []})

    result = CliRunner().invoke(_cli_root_app, ["org", "acl", "ls", "group-acme", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert observed["method"] == "org.acl.ls"
    assert observed["params"]["org"] == "group-acme"


def test_acl_commands_reject_a_non_group_org_name_before_ipc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every org command validates the group-NAME spelling before IPC; the
    acl family inherits the same rule (an ``org-acme`` org is not a group).
    """

    _env(monkeypatch, tmp_path)

    def unreachable(*_args: Any, **_kwargs: Any):
        raise AssertionError("invalid org names must not reach IPC")

    monkeypatch.setattr(_cli_support, "_daemon_request", unreachable)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "acl", "grant", "org-acme", AGENT, "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
