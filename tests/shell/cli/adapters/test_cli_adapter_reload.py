"""CLI seams for adapter reload: the subcommand and add's best-effort hook."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_adapter_reload_forwards_to_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del timeout, restore_wait
        calls.append((method, params))
        return {
            "ok": True,
            "added": ["lark-new"],
            "updated": [],
            "removed": [],
            "removedRunning": [],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["adapter", "reload", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.reload", {})]
    assert json.loads(result.stdout)["added"] == ["lark-new"]


def _run_add(monkeypatch: pytest.MonkeyPatch, home: Path) -> Any:
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    secret_file = home / "secret.txt"
    secret_file.write_text("cli-secret\n", encoding="utf-8")
    return CliRunner().invoke(
        _cli_root_app,
        [
            "adapter",
            "add",
            "feishu",
            "--app-id",
            "cli_app_123",
            "--route",
            "dm=oc_native_1",
            "--secret-file",
            str(secret_file),
            "--json",
        ],
    )


def test_adapter_add_asks_the_running_daemon_to_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str,
        params: dict[str, Any],
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del restore_wait
        calls.append((method, params))
        return {
            "ok": True,
            "added": ["feishu"],
            "updated": [],
            "removed": [],
            "removedRunning": [],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = _run_add(monkeypatch, home)

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.reload", {})]
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["daemonReload"]["added"] == ["feishu"]


def test_adapter_add_still_succeeds_when_no_daemon_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"

    def unavailable(
        method: str,
        params: dict[str, Any],
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del timeout, restore_wait
        raise CliError("DAEMON_UNAVAILABLE", "not running")

    monkeypatch.setattr(_cli_support, "_daemon_request", unavailable)

    result = _run_add(monkeypatch, home)

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["daemonReload"] is None
    # The config write itself is intact either way.
    channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
    assert channels["gateways"][0]["name"] == "feishu"
