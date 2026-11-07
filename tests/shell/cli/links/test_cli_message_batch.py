"""Behavioral coverage for the canonical ``hyprial message batch`` surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ACTOR = "agent:op:node-1:reader"
BATCH_TEXT = "[Harness Network inbox batch cursor=cli]\nBYTE-EXACT-cli-秘密"
BATCH_RESPONSE: dict[str, Any] = {
    "ok": True,
    "schemaVersion": 1,
    "format": "inbox-batch-v1",
    "cursor": "cursor-cli",
    "daemonEpoch": "epoch-cli",
    "actor": ACTOR,
    "itemCount": 1,
    "totalBodyBytes": 4,
    "inlineLimitBytes": 16384,
    "items": [{"messageId": "m-cli", "deliveryId": "m-cli", "body": "body"}],
    "delivery": {
        "mode": "inline",
        "text": BATCH_TEXT,
        "spillPath": None,
        "spillBytes": None,
        "spillSha256": None,
    },
}


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


def _record(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        return BATCH_RESPONSE

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def test_human_batch_output_is_exact_daemon_delivery_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = CliRunner().invoke(
        _cli_root_app,
        ["message", "batch", "--from", ACTOR, "--after-cursor", "cursor-before"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.pending.batch",
            {"from": ACTOR, "afterCursor": "cursor-before"},
        )
    ]
    assert result.stdout == BATCH_TEXT + "\n"
    assert result.stderr == ""


def test_json_batch_output_is_one_unmodified_cli_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = CliRunner().invoke(
        _cli_root_app,
        ["message", "batch", "--from", ACTOR, "--cursor", "cursor-cli", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("message.pending.batch", {"from": ACTOR, "cursor": "cursor-cli"})
    ]
    assert json.loads(result.stdout) == BATCH_RESPONSE
    assert result.stdout.count("\n") == 1
    assert result.stderr == ""


def test_top_level_inbox_alias_is_absent() -> None:
    result = CliRunner().invoke(_cli_root_app, ["inbox", "batch", "--from", ACTOR])

    assert result.exit_code == 2
    assert "No such command 'inbox'" in result.output
