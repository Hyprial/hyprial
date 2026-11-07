"""``hyprial daemon start`` reuses the one launch primitive (R5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PING = {
    "running": True,
    "pid": 4242,
    "epoch": "new-epoch",
    "phase": "serving",
}


def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path / "state")


def test_daemon_start_uses_the_shared_launch_primitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated(monkeypatch, tmp_path)
    calls: list[float] = []

    def launch(*, ready_timeout: float) -> DaemonLaunchResult:
        calls.append(ready_timeout)
        return DaemonLaunchResult.launched(PING)

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "start", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["alreadyRunning"] is False
    assert payload["pid"] == 4242
    assert calls == [_cli_owner_1.DAEMON_START_IDLE_BUDGET_SECONDS]


def test_daemon_start_reports_an_existing_daemon_without_relaunching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda *, ready_timeout: DaemonLaunchResult.existing(PING),
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "start", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["started"] is False
    assert payload["alreadyRunning"] is True
    assert payload["pid"] == 4242


def test_daemon_run_help_names_the_foreground_process_manager_use() -> None:
    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--help"])

    assert result.exit_code == 0
    # Rich hard-wraps at 80 columns, so compare on collapsed whitespace.
    help_text = " ".join(result.output.split())
    assert "in the foreground" in help_text
    assert "process manager" in help_text
