"""cli_upgrade_restart: launch scenarios."""

from __future__ import annotations
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.autoupdate.alert import AlertOutcome
from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceStatus,
)
from hyprial.kernel import ipc_errors

from .support import (
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.daemon_start as _cli_owner_2
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_upgrade_success_restarts_running_daemon_and_reports_pid_and_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["upgraded"] is True
    assert payload["restartRequired"] is False
    assert "upgradeSteps" in payload
    assert payload["restart"] == {
        "attempted": True,
        "restarted": True,
        "supervision": "unsupervised",
        "reason": "resolved tag changed; running daemon restarted",
        "before": {"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"},
        "after": {"pid": 4202, "epoch": "new", "version": "0.4.0.dev2"},
        "confirmed": True,
    }
    assert events == [
        "ping:15",
        "prime",
        "shutdown:2",
        f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}",
        "ping:2",
    ]


def test_upgrade_no_restart_is_an_explicit_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--no-restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["upgraded"] is True
    assert payload["restartRequired"] is True
    assert payload["restart"] == {
        "attempted": False,
        "restarted": False,
        "supervision": "unsupervised",
        "reason": "restart disabled by --no-restart",
        "before": {"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"},
    }
    assert events == ["ping:15", "prime"]


def test_upgrade_skips_restart_and_explains_when_no_daemon_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path, daemon_running=False)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["upgraded"] is True
    assert payload["restartRequired"] is False
    assert payload["restart"] == {
        "attempted": False,
        "restarted": False,
        "supervision": "unsupervised",
        "reason": "daemon was not running; restart skipped",
    }
    assert events == ["ping:15", "prime"]


def test_upgrade_restart_failure_is_loud_without_hiding_completed_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_launch(*, ready_timeout: float) -> dict[str, Any]:
        del ready_timeout
        raise _cli_get_services().CliError("DAEMON_START_FAILED", "fake daemon would not start")

    events = _fake_upgrade(monkeypatch, tmp_path, launch=fail_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert "upgrade completed" in payload["error"]
    assert "fake daemon would not start" in payload["error"]
    assert payload["data"]["upgradeCompleted"] is True
    assert payload["data"]["upgraded"] is True
    assert payload["data"]["resolvedTag"] == "v0.4.0.dev2"
    assert payload["data"]["resolvedCommit"] == "d" * 40
    assert payload["data"]["restart"] == {
        "attempted": True,
        "restarted": False,
        "supervision": "unsupervised",
        "reason": "daemon restart failed: fake daemon would not start",
        "before": {"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"},
    }
    assert events == ["ping:15", "prime", "shutdown:2"]


@pytest.mark.parametrize(
    ("platform", "returncode", "stderr"),
    [
        ("launchd", 5, "Bootstrap failed: 5: Input/output error"),
        ("systemd", 5, "Failed to connect to bus: No medium found"),
    ],
)
def test_upgrade_refuses_unmanaged_fallback_when_manager_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    returncode: int,
    stderr: str,
) -> None:
    import hyprial.daemon.impl.operations.service as backend_service_module
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    # The probe failure is the subject here. Since #2145 a failed
    # ``systemctl --user`` is first classified by the *host's* user-bus state,
    # so on a machine with no user bus (macOS, headless CI) the reason would be
    # `user-bus-unavailable` instead of `manager-command-failed`. Pin the state
    # to "ready" -- "the supervisor was asked and failed" -- so the assertion
    # does not depend on where the test runs; the no-session/address-unreachable
    # branches keep their own cases in
    # tests/daemon/application/composition/service/test_service_supervision.py.
    monkeypatch.setattr(
        backend_service_module, "user_bus_state", lambda *a, **k: "ready"
    )

    events = _fake_upgrade(monkeypatch, tmp_path)
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path,
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )

    def runner(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        if command[:3] == ("systemctl", "--user", "show") or command[:2] == (
            "launchctl",
            "print",
        ):
            return subprocess.CompletedProcess(command, returncode, "", stderr)
        if command[:2] == ("loginctl", "show-user"):
            return subprocess.CompletedProcess(command, 0, "yes\n", "")
        raise AssertionError(f"unexpected manager command: {command}")

    manager = ServiceManager(
        config,
        platform=platform,  # type: ignore[arg-type]
        service_home=tmp_path,
        runner=runner,
        uid=501,
        username="allen",
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert payload["data"]["restart"]["supervision"] == "lost"
    assert payload["data"]["restart"]["supervisionReason"] == "manager-command-failed"
    assert events == ["ping:15", "prime"]


def test_upgrade_reports_lost_when_supervisor_ownership_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    events = _fake_upgrade(monkeypatch, tmp_path)

    def refuse(_before_pid: int | None, *, timeout_seconds: float) -> None:
        assert timeout_seconds == _cli_owner_1.DAEMON_START_HARD_CAP_SECONDS
        raise _cli_get_services().CliError(
            "SERVICE_OWNERSHIP_REFUSED",
            "the installed supervisor file is foreign",
        )

    monkeypatch.setattr(service_module, "restart_supervised_daemon", refuse)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert payload["data"]["restart"]["supervision"] == "lost"
    assert events == ["ping:15", "prime"]


@pytest.mark.parametrize("stage", ["inspect", "restart"])
@pytest.mark.parametrize(
    ("manager_error", "supervision_reason"),
    [
        (RuntimeError("service command failed (5)"), "manager-command-failed"),
        (
            subprocess.TimeoutExpired(("launchctl", "kickstart"), 5),
            "manager-command-timeout",
        ),
        (FileNotFoundError("launchctl not found"), "manager-command-unavailable"),
    ],
)
def test_upgrade_maps_every_manager_command_failure_to_lost_supervision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    manager_error: Exception,
    supervision_reason: str,
) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    events = _fake_upgrade(monkeypatch, tmp_path)
    unit = tmp_path / "service.plist"
    unit.write_text("owned", encoding="utf-8")

    def inspect(*, serving_pid: int | None = None):  # type: ignore[no-untyped-def]
        if stage == "inspect":
            raise manager_error
        return ServiceStatus(
            platform="launchd",
            unit="com.hyprial.hyprial.daemon",
            unit_path=unit,
            installed=True,
            loaded=True,
            running=True,
            pid=serving_pid,
            attributed=True,
            template_matches=True,
            serving_pid=serving_pid,
            supervision="supervised",
        )

    def restart(  # type: ignore[no-untyped-def]
        _serving_pid, *, timeout_seconds: float
    ):
        assert timeout_seconds == _cli_owner_1.DAEMON_START_HARD_CAP_SECONDS
        raise manager_error

    manager = SimpleNamespace(unit_path=unit, inspect=inspect, restart=restart)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert payload["data"]["restart"]["supervision"] == "lost"
    assert (
        payload["data"]["restart"]["supervisionReason"] == supervision_reason
    )
    assert events == ["ping:15", "prime"]


def test_upgrade_does_not_fall_back_when_loaded_supervisor_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    events = _fake_upgrade(monkeypatch, tmp_path)
    missing_unit = tmp_path / "missing.plist"
    manager = SimpleNamespace(
        unit_path=missing_unit,
        inspect=lambda serving_pid=None: ServiceStatus(
            platform="launchd",
            unit="com.hyprial.hyprial.daemon",
            unit_path=missing_unit,
            installed=False,
            loaded=True,
            running=True,
            pid=4101,
            attributed=False,
            template_matches=False,
            serving_pid=serving_pid,
            supervision="lost",
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert payload["data"]["restart"]["supervision"] == "lost"
    assert (
        payload["data"]["restart"]["supervisionReason"]
        == "supervisor-file-missing"
    )
    assert events == ["ping:15", "prime"]


def test_upgrade_restart_failure_alert_is_wired_to_this_launch_and_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.mcp.channel import ownership as channel

    class FailedDaemonProcess:
        pid = 44710

        @staticmethod
        def poll() -> None:
            return None

    def fail_launch(*, ready_timeout: float) -> None:
        assert ready_timeout == _cli_owner_1.DAEMON_START_HARD_CAP_SECONDS
        log_path = tmp_path / "state" / "logs" / "daemon.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "level": "warn",
                    "component": "daemon",
                    "event": "daemon.start.failed",
                    "phase": "actor-runtime",
                    "errorType": "DomainCommandError",
                    "error": "fixture connector conflict",
                }
            )
            + "\n",
            "utf-8",
        )
        error = _cli_get_services().CliError(
            ipc_errors.DAEMON_START_TIMEOUT,
            "daemon did not become ready within 15s; the daemon process "
            "(pid 44710) was still running when the wait ended",
        )
        raise _cli_owner_2._launch_process_error(
            error,
            FailedDaemonProcess(),  # type: ignore[arg-type]
            "proc-starttime:123",
        )

    monkeypatch.setattr(
        channel,
        "_owner_process_status",
        lambda *_args: channel._OwnerProcessStatus.ALIVE,
    )
    delivered: dict[str, Any] = {}

    def notify(**kwargs: Any) -> AlertOutcome:
        assert (tmp_path / "state" / "upgrade-restart-failed.json").exists()
        delivered.update(kwargs)
        return AlertOutcome(attempted=True, delivered=True, message_id="om_fixture")

    monkeypatch.setattr(_cli_get_services(), "notify_upgrade_failure", notify)
    _fake_upgrade(monkeypatch, tmp_path, launch=fail_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    assert "phase=actor-runtime" in delivered["detail"]
    assert "errorType=DomainCommandError" in delivered["detail"]
    assert "error=fixture connector conflict" in delivered["detail"]
    assert "pid 44710 仍在(可能在失败后的 shutdown 中)" in delivered["detail"]
