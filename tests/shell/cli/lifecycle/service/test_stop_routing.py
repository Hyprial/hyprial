"""D2 (launchd acceptance, 2026-11-16): stop follows who supervises the daemon.

``hyprial daemon stop`` used to route through the manager whenever a unit file
was installed, so an installed-but-not-loaded unit beside a detached daemon
(reachable through ``HYPRIAL_DAEMON_AUTOSTART=0`` ``daemon start``) neither
booted anything out nor sent an IPC shutdown: ~102 s later it failed with
``DAEMON_STOP_TIMEOUT`` claiming a socket closed that never did. The real
``ServiceManager`` over a recording runner proves the four combinations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.shell.impl.cli.commands.common import daemon_stop
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.commands.daemon import service as service_module
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

DAEMON_PID = 77
OTHER_JOB_PID = 88


class ManagerFake:
    """A launchctl/systemctl that answers for one unit, loaded or not."""

    def __init__(self, platform: str, unit_path: Path, *, loaded: bool, job_pid: int) -> None:
        self.platform = platform
        self.unit_path = unit_path
        self.loaded = loaded
        self.job_pid = job_pid
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[0] == "loginctl":
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:3] == ("systemctl", "--user", "show"):
            if not self.loaded:
                return subprocess.CompletedProcess(
                    command, 4, "LoadState=not-found\n", "Unit could not be found."
                )
            active, sub = ("active", "running") if self.job_pid else ("inactive", "dead")
            return subprocess.CompletedProcess(
                command,
                0,
                f"LoadState=loaded\nActiveState={active}\nSubState={sub}\n"
                f"MainPID={self.job_pid}\nNRestarts=0\nExecMainStatus=0\n"
                f"FragmentPath={self.unit_path}\n",
                "",
            )
        if command[:2] == ("launchctl", "print"):
            if not self.loaded:
                label = command[2].rsplit("/", 1)[1]
                return subprocess.CompletedProcess(
                    command,
                    113,
                    "",
                    f'Could not find service "{label}" in domain for user gui: 501',
                )
            body = f"path = {self.unit_path}\n"
            if self.job_pid:
                body += f"state = running\n\tpid = {self.job_pid}\n"
            return subprocess.CompletedProcess(command, 0, body, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            c
            for c in self.commands
            if c[:3] != ("systemctl", "--user", "show")
            and c[:2] != ("launchctl", "print")
            and c[0] != "loginctl"
        ]


@pytest.fixture
def stop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = _cli_get_services()
    state = tmp_path / "state"
    state.mkdir()
    (state / "daemon.lock").touch()
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(
        services, "_daemon_probe", lambda **_k: {"pid": DAEMON_PID, "epoch": "e-1"}
    )
    monkeypatch.setattr(
        services, "_stopped_process", lambda pid, identity: pid == DAEMON_PID
    )
    import hyprial.daemon

    monkeypatch.setattr(hyprial.daemon, "read_process_identity", lambda _pid: None)
    ipc: list[tuple[Any, ...]] = []

    def request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        ipc.append((method, *args))
        return {
            "ok": True,
            "stopping": True,
            "pid": DAEMON_PID,
            "epoch": "e-1",
            "stateDir": str(state),
            "lockPath": str(state / "daemon.lock"),
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    def build(platform: str, *, installed: bool, loaded: bool, job_pid: int):  # type: ignore[no-untyped-def]
        config = ServiceConfig(
            executable=tmp_path / "bin" / "hyprial",
            home=tmp_path / "account",
            hyprial_home=tmp_path / "home",
            state_dir=state,
        )
        probe = ServiceManager(config, platform=platform, uid=501, username="user")
        runner = ManagerFake(platform, probe.unit_path, loaded=loaded, job_pid=job_pid)
        manager = ServiceManager(
            config, platform=platform, runner=runner, uid=501, username="user"
        )
        if installed:
            manager.unit_path.parent.mkdir(parents=True)
            manager.unit_path.write_text(manager._rendered(), encoding="utf-8")
        monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
        return manager, runner

    return services, ipc, build


STOP = {
    "systemd": ("systemctl", "--user", "stop"),
    "launchd": ("launchctl", "bootout"),
}


def _stop_command(manager: ServiceManager, platform: str) -> tuple[str, ...]:
    if platform == "systemd":
        return (*STOP[platform], manager.unit_path.name)
    return (*STOP[platform], f"gui/501/{manager.unit_path.stem}")


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_installed_unit_with_a_supervised_daemon_stops_through_the_manager(
    stop_env, platform: str
) -> None:
    _services, ipc, build = stop_env
    manager, runner = build(platform, installed=True, loaded=True, job_pid=DAEMON_PID)

    result = daemon_stop._stop_daemon_for_operator()

    assert runner.mutations() == [_stop_command(manager, platform)]
    assert ipc == []
    assert result["pid"] == DAEMON_PID
    assert result["teardownCompleted"] is True


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_installed_unit_with_a_detached_daemon_stops_over_ipc(
    stop_env, platform: str
) -> None:
    _services, ipc, build = stop_env
    _manager, runner = build(platform, installed=True, loaded=False, job_pid=0)

    result = daemon_stop._stop_daemon_for_operator()

    assert ipc == [("shutdown",)]
    assert runner.mutations() == []
    assert result["pid"] == DAEMON_PID
    assert result["teardownCompleted"] is True
    assert "supervisor" not in result


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_a_loaded_unit_beside_a_detached_daemon_is_stopped_so_it_cannot_relaunch(
    stop_env, platform: str
) -> None:
    _services, ipc, build = stop_env
    manager, runner = build(
        platform, installed=True, loaded=True, job_pid=OTHER_JOB_PID
    )

    result = daemon_stop._stop_daemon_for_operator()

    assert runner.mutations() == [_stop_command(manager, platform)]
    assert ipc == [("shutdown",)]
    assert result["pid"] == DAEMON_PID
    assert result["teardownCompleted"] is True


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_no_unit_and_a_detached_daemon_stops_over_ipc(stop_env, platform: str) -> None:
    _services, ipc, build = stop_env
    _manager, runner = build(platform, installed=False, loaded=False, job_pid=0)

    result = daemon_stop._stop_daemon_for_operator()

    assert ipc == [("shutdown",)]
    assert runner.mutations() == []
    assert result["teardownCompleted"] is True


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_a_loaded_job_without_a_unit_file_is_never_stopped_through_the_manager(
    stop_env, platform: str
) -> None:
    _services, ipc, build = stop_env
    _manager, runner = build(platform, installed=False, loaded=True, job_pid=DAEMON_PID)

    result = daemon_stop._stop_daemon_for_operator()

    assert ipc == [("shutdown",)]
    assert runner.mutations() == []
    assert result["teardownCompleted"] is True


def test_the_epoch_fence_travels_with_the_ipc_stop_of_a_detached_daemon(
    stop_env,
) -> None:
    services, ipc, build = stop_env
    _manager, runner = build("systemd", installed=True, loaded=False, job_pid=0)

    daemon_stop._stop_daemon_for_operator(expected_epoch="e-1")

    assert len(ipc) == 1 and ipc[0][0] == "shutdown"
    assert {"expectedEpoch": "e-1"} in ipc[0]
    assert runner.mutations() == []

    with pytest.raises(services.CliError) as raised:
        daemon_stop._stop_daemon_for_operator(expected_epoch="another")
    assert raised.value.code == "DAEMON_EPOCH_MISMATCH"
    assert len(ipc) == 1


def test_a_detached_daemon_that_survives_the_shutdown_is_reported_as_a_fact(
    stop_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, ipc, build = stop_env
    _manager, runner = build("launchd", installed=True, loaded=False, job_pid=0)
    monkeypatch.setattr(services, "_stopped_process", lambda pid, identity: False)

    result = daemon_stop._stop_daemon_for_operator()

    assert ipc == [("shutdown",)]
    assert runner.mutations() == []
    assert result["teardownCompleted"] is True
    assert result["survivingPid"] == DAEMON_PID
