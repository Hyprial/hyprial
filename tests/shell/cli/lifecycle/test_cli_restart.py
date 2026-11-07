"""``hyprial daemon restart`` safety and reporting contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.autoupdate import read_last_run, write_last_run
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.pending_restart as _cli_owner_2
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


BEFORE = {
    "daemon": {
        "running": True,
        "pid": 4101,
        "epoch": "old-epoch",
        "version": "0.4.5.dev1",
    },
    "connectors": [],
    "agents": [],
}
AFTER = {
    "running": True,
    "pid": 4202,
    "epoch": "new-epoch",
    "version": "0.4.5.dev1",
    "phase": "reconciled",
}


def _fake_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    status: dict[str, Any] | None = BEFORE,
) -> list[str]:
    events: list[str] = []
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path / "state")
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    missing_unit = tmp_path / "unconfigured-service.plist"
    manager = SimpleNamespace(
        unit_path=missing_unit,
        inspect=lambda serving_pid=None: SimpleNamespace(
            installed=False,
            loaded=False,
            serving_pid=serving_pid,
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    def request(method: str, **_kwargs: Any) -> dict[str, Any]:
        assert method == "ps"
        if status is None:
            raise ipc_errors.DaemonUnavailableError("not running")
        return status

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_gracefully",
        lambda: events.append("stop") or {"ok": True, "pid": 4101},
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda *, ready_timeout: events.append(f"launch:{ready_timeout:g}")
        or DaemonLaunchResult.launched(AFTER),
    )
    return events


def test_restart_without_in_flight_turns_uses_supervisor_when_it_owns_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    events = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service_module,
        "restart_supervised_daemon",
        lambda before_pid, *, timeout_seconds: events.append(
            f"supervisor:{before_pid}:{timeout_seconds:g}"
        )
        or DaemonLaunchResult.launched(AFTER),
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["supervision"] == "supervised"
    assert payload["after"]["pid"] == 4202
    assert events == [f"supervisor:4101:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}"]


def test_restart_without_in_flight_turns_restarts_with_fresh_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_restart(monkeypatch, tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restarted"] is True
    assert payload["started"] is True
    assert payload["before"] == BEFORE["daemon"]
    assert payload["after"] == {
        "running": True,
        "pid": 4202,
        "epoch": "new-epoch",
        "version": "0.4.5.dev1",
    }
    assert payload["interruptedTurns"] == []
    assert payload["elapsedSeconds"] >= 0
    assert events == ["stop", f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}"]


def test_restart_refuses_noninteractive_in_flight_turns_before_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = {
        **BEFORE,
        "connectors": [
            {"name": "worker-a", "runtime": "codex", "inFlight": 1},
            {"name": "idle", "runtime": "claude", "inFlight": 0},
        ],
        "agents": [
            {
                "name": "worker-a",
                "actor": "agent:owner:node:worker-a",
                "runtime": "codex",
            }
        ],
    }
    events = _fake_restart(monkeypatch, tmp_path, status=status)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "RESTART_WOULD_INTERRUPT"
    assert payload["data"]["interruptedTurns"] == [
        {
            "actor": "agent:owner:node:worker-a",
            "harness": "codex",
            "inFlight": 1,
        }
    ]
    assert events == []


def test_restart_yes_proceeds_and_reports_interrupted_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = {
        **BEFORE,
        "connectors": [
            {"name": "worker-a", "runtime": "pi", "inFlight": 2},
        ],
        "agents": [],
    }
    events = _fake_restart(monkeypatch, tmp_path, status=status)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["interruptedTurns"] == [
        {"actor": "worker-a", "harness": "pi", "inFlight": 2}
    ]
    assert events == ["stop", f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}"]


def test_restart_interactive_confirms_before_interrupting_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = {
        **BEFORE,
        "connectors": [
            {"name": "worker-a", "runtime": "claude", "inFlight": 1},
        ],
        "agents": [],
    }
    events = _fake_restart(monkeypatch, tmp_path, status=status)
    monkeypatch.setattr(_cli_get_services(), "_restart_is_interactive", lambda: True)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Restart and interrupt these active turns?" in result.output
    assert events == ["stop", f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}"]


def test_restart_starts_when_daemon_is_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_restart(monkeypatch, tmp_path, status=None)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restarted"] is False
    assert payload["started"] is True
    assert payload["before"] == {"running": False}
    assert payload["after"]["pid"] == 4202
    assert events == [f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}"]


def test_restart_confirms_pending_autoupdate_and_annotates_last_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_restart(monkeypatch, tmp_path)
    pending = {
        "tag": "v0.4.5.dev1",
        "commit": "d" * 40,
        "before": {"pid": 4101, "version": "0.4.5.dev0"},
    }
    _cli_owner_2._write_pending_restart(pending)
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

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert not _cli_owner_2._pending_restart_path().exists()
    record = read_last_run(tmp_path / "state")
    assert record is not None
    assert record["restartRequired"] is False
    assert record["restart"]["awaitingConfirmation"] is False
    confirmation = record["restartConfirmation"]
    assert confirmation["via"] == "hyprial daemon restart"
    assert confirmation["newPid"] == 4202
    assert confirmation["newEpoch"] == "new-epoch"
    assert isinstance(confirmation["restartedAt"], str)


def test_restart_survivor_uses_existing_alert_logic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_restart(monkeypatch, tmp_path)
    stop_error = _cli_get_services().CliError(
        "DAEMON_STOP_SURVIVOR", "old daemon survived", {"oldPid": 4101}
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_gracefully",
        lambda: (_ for _ in ()).throw(stop_error),
    )
    monkeypatch.setattr(_cli_get_services(), "_describe_survivor", lambda _pid: "survivor detail")
    monkeypatch.setattr(
        _cli_get_services(),
        "write_failure_marker",
        lambda **_kwargs: events.append("marker") or tmp_path / "marker.json",
    )

    class Alert:
        def to_json(self) -> dict[str, Any]:
            return {"delivered": True}

    monkeypatch.setattr(
        _cli_get_services(),
        "notify_upgrade_failure",
        lambda **_kwargs: events.append("alert") or Alert(),
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "record_alert_outcome",
        lambda *_args: events.append("record-alert"),
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["survivingOldProcess"] is True
    assert payload["survivorAlert"] == {"delivered": True}
    assert events == [
        "marker",
        "alert",
        "record-alert",
        f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}",
    ]
