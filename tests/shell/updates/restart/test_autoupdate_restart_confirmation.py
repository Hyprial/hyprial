"""The timer installs; a person restarts (Allen, 2026-09-23).

Before: the timer restarted the daemon only after a 3-second squire receipt
for a pre-restart notice.  On 2026-09-23 15:17 the notice arrived 0.28 s after
that window closed, the restart was blocked -- and then forgotten: the next
run found the new version "already current" and never restarted, leaving the
daemon on old code indefinitely.

Now: the timer installs and records a pending restart; the owner is told in
the ordinary post-upgrade alert; ``hyprial daemon upgrade apply`` applies it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli

import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.autoupdate import read_last_run, write_last_run
from hyprial.daemon.impl.autoupdate.alert import UPGRADE_AWAITING_RESTART
from tests.shell.cli.installation.restart.support import _fake_upgrade
from tests.shell.upgrades.test_upgrade_selfcheck import _notify
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.pending_restart as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _pending(tmp_path: Path) -> Path:
    return tmp_path / "state" / _cli_owner_1.PENDING_RESTART_FILE


@pytest.fixture(autouse=True)
def _locked_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


def test_the_timer_installs_without_restarting_and_records_the_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)

    result = _cli_get_services()._perform_upgrade(force=False, restart=False, awaiting_confirmation=True)

    assert result["upgraded"] is True
    assert result["restartRequired"] is True
    assert result["restart"]["awaitingConfirmation"] is True
    assert result["restart"]["attempted"] is False
    assert "hyprial daemon upgrade apply" in result["restart"]["reason"]
    assert not any(event.startswith("shutdown") for event in events)
    pending = json.loads(_pending(tmp_path).read_text(encoding="utf-8"))
    assert pending["tag"] == "v0.4.0.dev2"
    assert pending["commit"] == "d" * 40
    assert pending["before"] == {
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.0.dev1",
    }


def test_restart_with_nothing_pending_does_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "apply", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restarted"] is False
    assert payload["reason"] == "no pending restart"
    assert events == []


def test_restart_applies_the_pending_restart_and_clears_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)
    _cli_get_services()._perform_upgrade(force=False, restart=False, awaiting_confirmation=True)
    assert _pending(tmp_path).exists()

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "apply", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["confirmedRestart"] is True
    assert payload["restart"]["restarted"] is True
    assert payload["restart"]["before"] == {
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.0.dev1",
    }
    assert "upgradeSteps" in payload
    assert any(event.startswith("shutdown") for event in events)
    assert not _pending(tmp_path).exists()


def test_autoupdate_status_drops_stale_wait_after_confirmed_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_upgrade(monkeypatch, tmp_path)
    _cli_get_services()._perform_upgrade(force=False, restart=False, awaiting_confirmation=True)
    write_last_run(
        tmp_path / "state",
        {
            "ok": True,
            "restartRequired": True,
            "restart": {
                "awaitingConfirmation": True,
                "reason": "restart waits for confirmation",
            },
        },
    )

    restarted = CliRunner().invoke(
        _cli_root_app, ["daemon", "upgrade", "apply", "--json"]
    )

    assert restarted.exit_code == 0, restarted.output
    record = read_last_run(tmp_path / "state")
    assert record is not None
    assert record["restartRequired"] is False
    assert record["restart"]["awaitingConfirmation"] is False
    assert record["restartConfirmation"]["via"] == "hyprial daemon upgrade apply"
    assert "waits for confirmation" not in json.dumps(record)

    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cli.ipc_errors.DaemonUnavailableError("status probe unavailable")
        ),
    )
    status = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "status", "--json"])
    assert status.exit_code == 0, status.output
    last_run = json.loads(status.stdout)["lastRun"]
    assert last_run["restartRequired"] is False
    assert last_run["restartConfirmation"]["newPid"] == 4202
    assert "waits for confirmation" not in json.dumps(last_run)


def test_restart_skips_a_daemon_that_was_already_restarted_after_the_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any daemon started after the install already runs the installed code."""

    events = _fake_upgrade(monkeypatch, tmp_path)
    _pending(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _pending(tmp_path).write_text(
        json.dumps(
            {
                "tag": "v0.4.0.dev2",
                "commit": "d" * 40,
                "version": "0.4.0.dev2",
                "before": {"pid": 9999, "version": "0.4.0.dev1"},
                "resolved": {},
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "apply", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restarted"] is False
    assert payload["reason"] == "daemon was already restarted after the install"
    assert not any(event.startswith("shutdown") for event in events)
    assert not _pending(tmp_path).exists()


def test_the_owner_alert_says_what_to_run(tmp_path: Path) -> None:
    outcome, recorder = _notify(tmp_path, ok=True, action=UPGRADE_AWAITING_RESTART)

    assert outcome.delivered is True
    text = recorder.sent[0]
    assert "hyprial daemon upgrade apply" in text
    assert "仍在旧版本" in text
    assert "失败" not in text


def test_apply_calls_the_shared_upgrade_step_runner(monkeypatch, tmp_path) -> None:
    from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands

    _fake_upgrade(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        upgrade_commands,
        "_run_upgrade_steps",
        lambda services: calls.append("run") or {"steps": [], "pending": []},
    )
    _cli_get_services()._perform_upgrade(force=False, restart=False, awaiting_confirmation=True)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "apply", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == ["run"]
