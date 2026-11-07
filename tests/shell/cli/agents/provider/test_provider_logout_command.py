"""``hyprial agent provider logout`` and the revoke path ``login`` names.

Allen's identity rule (2026-10-14): the help and the output of ``login`` say
whose credential this is and how to take it back. ``logout`` is that path; it
reads no host file and sends only the actor and the provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'


class _Captured:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, method: str, params: object = None, **_kwargs: object) -> object:
        self.calls.append((method, params))
        if method == "agent.provider.logout":
            return {
                "ok": True,
                "actor": "worker",
                "provider": "claude",
                "removed": ["/agent/home/secrets/native/claude/.credentials.json"],
                "absent": [],
            }
        return {
            "ok": True,
            "actor": "worker",
            "provider": "claude",
            "files": [{"path": "/agent/home/x", "bytes": len(PAYLOAD)}],
        }


@pytest.fixture
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    return home


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    captured = _Captured()
    monkeypatch.setattr(_cli_support, "_daemon_request", captured)
    return captured


def _invoke(*arguments: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(_cli_root_app, ["agent", "provider", *arguments])


def test_logout_sends_only_actor_and_provider(
    operator_home: Path, capture: _Captured
) -> None:
    result = _invoke("logout", "claude", "--actor", "worker", "--json")

    assert result.exit_code == 0, result.output
    assert capture.calls == [
        ("agent.provider.logout", {"actor": "worker", "provider": "claude"})
    ]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["removed"] == [
        "/agent/home/secrets/native/claude/.credentials.json"
    ]


def test_login_receipt_names_the_revoke_command(
    operator_home: Path, capture: _Captured
) -> None:
    source = operator_home / ".claude" / ".credentials.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(PAYLOAD)

    result = _invoke("login", "claude", "--actor", "worker", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["revoke"] == "hyprial agent provider logout claude --actor worker"
    assert "accessToken" not in result.stdout
    assert "test-only" not in result.stdout + result.stderr


def test_login_help_names_the_owner_and_the_revoke_path() -> None:
    result = CliRunner().invoke(_cli_root_app, ["agent", "provider", "login", "--help"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert "logged in as" in text
    assert "hyprial agent provider logout" in text


def test_the_argument_is_a_harness_not_a_model_vendor() -> None:
    for command in ("login", "logout"):
        result = CliRunner().invoke(_cli_root_app, ["agent", "provider", command, "--help"])
        text = " ".join(result.stdout.split())
        assert "Harness: claude, codex, or pi" in text
        assert "Model vendor" not in text
