"""With the per-user service installed, start/stop go through the supervisor.

launchd ``KeepAlive`` / systemd ``Restart=always`` relaunch within seconds of
any exit, so a CLI spawn beside them is a second launcher and an IPC shutdown
alone is undone.  Without an installed service the CLI spawn / IPC stop stays.
Windows has no supported per-user supervisor in 0.5.5.3.4: init reports the
default registration as skipped (not failed); an explicit ``--service`` errors.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common import daemon_stop
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.commands.daemon import service as service_module
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class FakeRunner:
    """A ``ServiceCommandRunner`` that answers ``systemctl`` like an idle unit."""

    def __init__(self, unit_path: Path) -> None:
        self.unit_path = unit_path
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        command = tuple(command)
        self.commands.append(command)
        stdout = ""
        returncode = 0
        if command[:3] == ("systemctl", "--user", "show"):
            stdout = (
                "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
                f"MainPID=0\nNRestarts=0\nExecMainStatus=0\nFragmentPath={self.unit_path}\n"
            )
        elif command[0] == "loginctl":
            returncode = 1
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            command
            for command in self.commands
            if command[:3] != ("systemctl", "--user", "show") and command[0] != "loginctl"
        ]


def _manager(tmp_path: Path, *, installed: bool = True) -> tuple[ServiceManager, FakeRunner]:
    config = ServiceConfig(
        executable=tmp_path / "bin" / "hyprial",
        home=tmp_path / "account",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )
    probe = ServiceManager(config, platform="systemd", uid=1000, username="user")
    runner = FakeRunner(probe.unit_path)
    manager = ServiceManager(
        config, platform="systemd", runner=runner, uid=1000, username="user"
    )
    if installed:
        manager.unit_path.parent.mkdir(parents=True)
        manager.unit_path.write_text(manager._rendered(), encoding="utf-8")
    return manager, runner


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    return services


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def spawn(**_kwargs: Any) -> DaemonLaunchResult:
        raise AssertionError("the CLI spawned a daemon beside the installed supervisor")

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process_locked", spawn)


def test_start_with_installed_service_asks_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, runner = _manager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _no_spawn(monkeypatch)
    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    waited: list[dict[str, Any]] = []

    def wait(**kwargs: Any) -> dict[str, Any]:
        waited.append(kwargs)
        return {"running": True, "pid": 77, "epoch": "e-1"}

    monkeypatch.setattr(services, "_wait_for_daemon", wait)

    launched = services._launch_daemon_process(
        ready_timeout=3.0, expected_identity_generation=4
    )

    assert runner.mutations() == [("systemctl", "--user", "start", "hyprial.service")]
    assert launched.running and launched.pid == 77
    assert waited[0]["timeout"] == 3.0
    assert "process" not in waited[0]


def test_start_without_installed_service_keeps_the_cli_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, runner = _manager(tmp_path, installed=False)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    spawned: list[dict[str, Any]] = []

    def spawn(**kwargs: Any) -> DaemonLaunchResult:
        spawned.append(kwargs)
        return DaemonLaunchResult.launched({"running": True, "pid": 5})

    monkeypatch.setattr(services, "_launch_daemon_process_locked", spawn)
    services._launch_daemon_process(ready_timeout=1.0, expected_identity_generation=2)
    assert runner.commands == []
    assert spawned == [
        {
            "ready_timeout": 1.0,
            "listen": None,
            "connect": None,
            "expected_identity_generation": 2,
        }
    ]


def test_endpoint_overrides_cannot_reach_a_supervised_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, runner = _manager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _no_spawn(monkeypatch)
    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    with pytest.raises(services.CliError) as raised:
        services._launch_daemon_process(ready_timeout=1.0, listen="tcp/0.0.0.0:1")
    assert raised.value.code == "SERVICE_MANAGED_ENDPOINTS"
    assert runner.mutations() == []


def test_daemon_stop_with_installed_service_stops_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, runner = _manager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(
        services, "_daemon_probe", lambda **_k: {"pid": 77, "epoch": "e-1"}
    )

    def request(method: str, *_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"IPC {method} would be undone by Restart=always")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(services, "_stopped_process", lambda pid, identity: pid == 77)
    import hyprial.daemon

    monkeypatch.setattr(hyprial.daemon, "read_process_identity", lambda _pid: None)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "daemon.lock").touch()  # released by the stopped daemon

    result = daemon_stop._stop_daemon_for_operator()

    assert runner.mutations() == [("systemctl", "--user", "stop", "hyprial.service")]
    assert result["pid"] == 77
    assert result["supervisor"] == {"platform": "systemd", "unit": "hyprial.service"}
    assert result["teardownCompleted"] is True

    with pytest.raises(services.CliError) as raised:
        daemon_stop._stop_daemon_for_operator(expected_epoch="another")
    assert raised.value.code == "DAEMON_EPOCH_MISMATCH"


@pytest.mark.parametrize(
    "loaded,start,stop",
    [
        (
            False,
            ("launchctl", "bootstrap", "gui/501", "/LaunchAgents/x.plist"),
            None,
        ),
        (
            True,
            ("launchctl", "kickstart", "gui/501/com.hyprial.hyprial.daemon"),
            ("launchctl", "bootout", "gui/501/com.hyprial.hyprial.daemon"),
        ),
    ],
)
def test_launchd_routes_bootstrap_kickstart_and_bootout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services, loaded, start, stop
) -> None:
    runner = FakeRunner(tmp_path / "unused")
    manager = SimpleNamespace(platform="launchd", uid=501, runner=runner)
    status = SimpleNamespace(
        platform="launchd",
        unit="com.hyprial.hyprial.daemon",
        unit_path=Path("/LaunchAgents/x.plist"),
        loaded=loaded,
    )
    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    monkeypatch.setattr(services, "_wait_for_daemon", lambda **_k: {"running": True, "pid": 9})
    service_module.start_supervised_daemon((manager, status), idle_budget=1.0)
    service_module.stop_supervised_daemon((manager, status))
    assert runner.commands == [start] + ([stop] if stop else [])


def test_a_failing_supervisor_command_is_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services
) -> None:
    def runner(command, *, timeout=None):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 5, "", "Input/output error")

    manager = SimpleNamespace(platform="systemd", uid=1000, runner=runner)
    status = SimpleNamespace(platform="systemd", unit="hyprial.service", loaded=True)
    monkeypatch.setattr(services, "_daemon_probe", lambda **_k: None)
    with pytest.raises(services.CliError) as raised:
        service_module.stop_supervised_daemon((manager, status))
    assert raised.value.code == "SERVICE_MANAGER_FAILED"


# --- Windows: default registration skipped, explicit --service refused --------


def _unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    def platform() -> str:
        raise service_module.ServicePlatformUnsupported("win32")

    monkeypatch.setattr(service_module, "_service_platform", platform)


def test_windows_init_reports_default_service_registration_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    _unsupported(monkeypatch)

    def request(method: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        if method == "org.fetch":
            return {"receivedCount": 0}
        return {"daemon": {"running": True}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    service = json.loads(result.stdout)["service"]
    assert service["ok"] is True
    assert service["skipped"] is True
    assert service["reason"] == "service-platform-unsupported"
    assert service["platform"] == "win32"
    assert "hyprial daemon start" in service["hint"]


def test_windows_init_with_explicit_service_errors_before_touching_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    _unsupported(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, ["init", "--service", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "SERVICE_PLATFORM_UNSUPPORTED"
    assert payload["data"] == {"platform": "win32"}
    assert not home.exists()


def test_an_unsupervised_daemon_does_not_wait_forever_behind_a_healthy_owner(
    tmp_path: Path,
) -> None:
    """Review P0: only a supervisor-relaunched daemon waits while the holder is
    healthy; a CLI-spawned/manual loser exits after the bounded first attempt
    instead of lingering as a second process."""

    from hyprial.daemon import DaemonStateOwnershipFence
    from hyprial.shell.impl.cli.commands.daemon import commands as daemon_commands

    services = SimpleNamespace(
        _daemon_probe=lambda timeout=0.5: {"daemon": {"pid": 4242}},
        CliError=_cli_get_services().CliError,
    )
    holder = DaemonStateOwnershipFence.acquire(tmp_path)
    try:
        with pytest.raises(services.CliError) as raised:
            daemon_commands._acquire_daemon_fence(
                services,
                tmp_path,
                timeout=0.0,
                sleep=lambda _s: pytest.fail("waited behind a healthy owner"),
                wait_while_healthy=False,
            )
        assert raised.value.code == "DAEMON_OWNERSHIP_BUSY"
    finally:
        holder.close()
