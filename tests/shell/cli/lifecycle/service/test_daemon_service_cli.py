from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.commands.daemon.service as service_module
from hyprial.daemon.impl.operations import service as ops_service_module
from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from hyprial.kernel import ipc_errors


@pytest.fixture(autouse=True)
def initialized_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path / "state")


def status(**overrides: object) -> SimpleNamespace:
    values = {
        "platform": "launchd",
        "unit": "com.hyprial.hyprial.daemon",
        "unit_path": Path("/tmp/com.hyprial.hyprial.daemon.plist"),
        "installed": True,
        "loaded": True,
        "running": True,
        "pid": 4202,
        "boot_persistent": None,
        "changed": None,
        "attributed": True,
        "template_matches": True,
        "serving_pid": 4202,
        "supervision": "supervised",
        "relaunch_count": 1,
        "last_exit_code": 0,
        "ownership_busy": False,
        "ownership_reason": None,
        "backup_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_daemon_service_status_is_json_and_reports_supervision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        inspect=lambda serving_pid=None: status(serving_pid=serving_pid)
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(service_module, "_serving_daemon_pid", lambda: 4202)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "service", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["supervision"] == "supervised"
    assert payload["templateMatches"] is True
    assert payload["pid"] == 4202


def test_daemon_service_install_requires_yes_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    manager = SimpleNamespace(install=lambda: called.append("install") or status())
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(service_module, "_service_is_interactive", lambda: False)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "service", "install", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "CONFIRMATION_REQUIRED"
    assert called == []


def test_daemon_service_install_waits_for_supervisor_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    manager = SimpleNamespace(
        install=lambda: calls.append("install") or status(changed=True),
        wait_for_custody=lambda serving_pid: (
            calls.append("wait") or status(changed=True, serving_pid=serving_pid())
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(service_module, "_serving_daemon_pid", lambda: 4202)

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "service", "install", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["supervision"] == "supervised"
    assert calls == ["install", "wait"]


def test_daemon_service_install_fails_if_custody_is_not_established(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        install=lambda: status(changed=True),
        wait_for_custody=lambda _serving_pid: status(
            changed=True,
            serving_pid=9001,
            supervision="lost",
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "service", "install", "--yes", "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "SERVICE_SUPERVISION_LOST"
    assert payload["data"]["supervision"] == "lost"


def test_daemon_service_unsupported_platform_is_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported():
        raise service_module.ServicePlatformUnsupported("win32")

    monkeypatch.setattr(service_module, "_service_manager", unsupported)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "service", "status", "--json"])
    mutation = CliRunner().invoke(_cli_root_app, ["daemon", "service", "install", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "SERVICE_PLATFORM_UNSUPPORTED"
    assert mutation.exit_code == 1
    assert json.loads(mutation.stdout)["code"] == "SERVICE_PLATFORM_UNSUPPORTED"


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_restart_fails_closed_when_manager_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    """A configured supervisor whose manager binary is unavailable is lost
    supervision, never the unmanaged direct stop/start path.

    contract/daemon-lifecycle/service-supervision.md §Restart custody: every
    missing executable uses the lost-supervision result. 9240bca713 routed
    the missing binary through the shared manager constructor into the
    ``unsupervised`` restart fallback and was rejected by revSG2.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service_module, "_service_platform", lambda: platform)
    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda name: None if name in {"launchctl", "systemctl"} else "/usr/bin/env",
    )

    def unavailable(
        self: ServiceManager,
        command: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(ops_service_module.ServiceManager, "_run", unavailable)

    with pytest.raises(_cli_get_services().CliError) as caught:
        service_module.restart_supervised_daemon(9001)

    assert caught.value.code == "SERVICE_SUPERVISION_LOST"
    assert caught.value.data["restart"] == {
        "supervision": "lost",
        "reason": "manager-command-unavailable",
    }


def test_restart_refuses_to_launch_directly_after_supervisor_loses_custody(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = tmp_path / "service.plist"
    unit.write_text("owned", encoding="utf-8")
    restarted: list[bool] = []
    manager = SimpleNamespace(
        unit_path=unit,
        inspect=lambda serving_pid=None: status(
            unit_path=unit,
            serving_pid=serving_pid,
            supervision="lost",
        ),
        restart=lambda _serving_pid: restarted.append(True),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    with pytest.raises(_cli_get_services().CliError) as caught:
        service_module.restart_supervised_daemon(9001)

    assert caught.value.code == "SERVICE_SUPERVISION_LOST"
    assert caught.value.data["restart"]["supervision"] == "lost"
    assert restarted == []


def test_supervised_legacy_probe_cannot_double_the_hard_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real ping/ps probe cannot turn a 1s supervisor cap into 2s."""

    now = [0.0]
    clock = SimpleNamespace(
        monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    calls: list[tuple[str, float]] = []
    before = status(
        unit_path=tmp_path / "service.plist",
        pid=4101,
        serving_pid=4101,
    )

    def timed_out_legacy_transport(
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float,
        **_kwargs: object,
    ) -> dict[str, object]:
        del params
        calls.append((method, timeout))
        clock.sleep(timeout)
        if method == "ping":
            raise _cli_get_services().CliError(
                ipc_errors.METHOD_NOT_FOUND,
                "old daemon does not implement ping",
            )
        assert method == "ps"
        raise ipc_errors.DaemonUnavailableError("legacy daemon is not ready")

    def restart(serving_pid, *, timeout_seconds: float):  # type: ignore[no-untyped-def]
        assert timeout_seconds == 1.0
        assert serving_pid(timeout_seconds) is None
        failure = RuntimeError("service custody wait reached its wall-clock deadline")
        failure.reason = "supervisor-start-hard-cap"  # type: ignore[attr-defined]
        raise failure

    manager = SimpleNamespace(
        inspect=lambda serving_pid=None: before,
        restart=restart,
    )
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(_cli_support, "_daemon_request", timed_out_legacy_transport)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    with pytest.raises(_cli_get_services().CliError) as caught:
        service_module.restart_supervised_daemon(4101, timeout_seconds=1.0)

    assert caught.value.code == ipc_errors.DAEMON_START_TIMEOUT
    assert now[0] <= 1.0 + 0.5
    assert sum(timeout for _method, timeout in calls) <= 1.0
    assert [method for method, _timeout in calls] == ["ping"]
    assert [timeout for _method, timeout in calls] == pytest.approx([1.0])


def test_restart_reports_typed_reason_for_unsafe_unit_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = tmp_path / "service.plist"
    unit.write_text("owned", encoding="utf-8")
    manager = SimpleNamespace(
        unit_path=unit,
        inspect=lambda serving_pid=None: status(
            unit_path=unit,
            serving_pid=serving_pid,
            attributed=False,
            supervision="lost",
            ownership_reason="unit-owner-mismatch",
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    with pytest.raises(_cli_get_services().CliError) as caught:
        service_module.restart_supervised_daemon(9001)

    assert caught.value.code == "SERVICE_OWNERSHIP_REFUSED"
    assert caught.value.data["restart"] == {
        "supervision": "lost",
        "reason": "unit-owner-mismatch",
    }
    assert caught.value.data["ownershipReason"] == "unit-owner-mismatch"


def test_loaded_label_without_file_blocks_restart_and_has_doctor_fix_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_unit = tmp_path / "missing.plist"
    manager = SimpleNamespace(
        unit_path=missing_unit,
        inspect=lambda serving_pid=None: status(
            unit_path=missing_unit,
            installed=False,
            loaded=True,
            running=True,
            serving_pid=serving_pid,
            attributed=False,
            template_matches=False,
            supervision="lost",
            ownership_reason="supervisor-file-missing",
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    with pytest.raises(_cli_get_services().CliError) as caught:
        service_module.restart_supervised_daemon(9001)
    check = service_module.daemon_service_doctor_check(serving_pid=9001, fix=False)

    assert caught.value.code == "SERVICE_SUPERVISION_LOST"
    assert caught.value.data["restart"] == {
        "supervision": "lost",
        "reason": "supervisor-file-missing",
    }
    assert check["status"] == "fail"
    assert "missing" in check["detail"]
    assert "unload" in check["action"]["description"].lower()


@pytest.mark.parametrize(
    ("platform", "returncode", "stderr", "expected", "expected_status"),
    [
        (
            "launchd",
            113,
            'Could not find service "com.hyprial.hyprial.daemon" in domain for user gui: 501',
            # Not installed is a warn, not "no opinion": the daemon is running
            # unsupervised, which only costs restart resilience.
            "unsupervised",
            "warn",
        ),
        (
            "systemd",
            4,
            "Unit hyprial.service could not be found.",
            "unsupervised",
            "warn",
        ),
        (
            "systemd",
            5,
            # A headless caller (no login session) cannot reach the user bus.
            # That is an environment gap with a known remedy, so it must not be
            # reported with the same reason as a genuine manager failure.
            "Failed to connect to bus: No medium found",
            "user-bus-unavailable",
            # "cannot ask" is not "asked and broken": the fourth state must not
            # aggregate as a pass, but it is also not a failure verdict.
            "unknown",
        ),
        (
            "systemd",
            1,
            "Failed to connect to bus: No such file or directory",
            "user-bus-address-unreachable",
            # Both "no session" and "address points nowhere" are "cannot ask".
            "unknown",
        ),
    ],
)
def test_service_doctor_distinguishes_missing_service_from_manager_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    returncode: int,
    stderr: str,
    expected: str | None,
    expected_status: str | None,
) -> None:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path,
        hyprial_home=tmp_path / "hyprial",
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
    # Keep the bus probe deterministic: this case is about the supervisor's
    # answer, not about whether a session bus exists on the test host.
    monkeypatch.setattr(
        ops_service_module,
        "user_bus_state",
        lambda *a, **k: (
            "address-unreachable"
            if "No such file or directory" in stderr
            else ("no-session" if "Failed to connect to bus" in stderr else "ready")
        ),
    )

    check = service_module.daemon_service_doctor_check(serving_pid=4101, fix=False)

    if expected is None:
        assert check is None
    else:
        assert check is not None
        assert check["status"] == expected_status
        assert check["metrics"]["reason"] == expected


def test_service_doctor_reports_lost_loop_and_template_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        inspect=lambda serving_pid=None: status(
            serving_pid=serving_pid,
            supervision="lost",
            template_matches=False,
            relaunch_count=19,
            ownership_busy=True,
        )
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    check = service_module.daemon_service_doctor_check(serving_pid=9001, fix=False)

    assert check["status"] == "fail"
    assert check["metrics"]["supervision"] == "lost"
    assert check["metrics"]["templateMatches"] is False
    assert check["metrics"]["ownershipBusy"] is True
    assert check["metrics"]["relaunchCount"] == 19


def test_service_doctor_separates_never_installed_from_linger_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Both are warns, but the remedy differs: installing the unit vs enabling
    # lingering.  Sharing one reason string would send half the readers to the
    # wrong command.
    def check_for(*, installed: bool, boot_persistent: bool | None):
        manager = SimpleNamespace(
            inspect=lambda serving_pid=None: status(
                # Lingering is a systemd concept; state the platform so the
                # case tests the rule rather than the default fixture value.
                platform="systemd",
                installed=installed,
                loaded=installed,
                running=installed,
                boot_persistent=boot_persistent,
                supervision="supervised" if installed else "unsupervised",
            )
        )
        monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
        return service_module.daemon_service_doctor_check(serving_pid=1, fix=False)

    never_installed = check_for(installed=False, boot_persistent=None)
    assert never_installed is not None
    assert never_installed["status"] == "warn"
    assert never_installed["metrics"]["reason"] == "unsupervised"
    assert "service install" in never_installed["action"]["command"]

    linger_off = check_for(installed=True, boot_persistent=False)
    assert linger_off is not None
    assert linger_off["status"] == "warn"
    assert linger_off["metrics"]["reason"] == "linger-off"
    assert "enable-linger" in linger_off["action"]["command"]
