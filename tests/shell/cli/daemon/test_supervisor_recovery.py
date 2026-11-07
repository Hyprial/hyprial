"""Supervisor-routed stops must stay recoverable and keep their fences.

``launchctl bootout`` unloads the LaunchAgent, so the next ``daemon restart``
(or upgrade restart) finds an installed, owned, *unloaded* unit: that is a
supervisor start, not lost custody.  The stop budget in the unit templates
must cover the daemon's own teardown, or SIGKILL truncates the drain.  The
identity-switch stop refuses an unknown pid *before* the supervisor acts and
reads the process identity while the process still exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.operations.service import ServiceStatus
from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS, ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.commands.daemon import service as service_module
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SERVICES_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "hyprial" / "daemon" / "impl" / "services"
)


class _Runner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        self.commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")


def _launchd(*, loaded: bool) -> tuple[Any, Any, _Runner]:
    runner = _Runner()
    status = ServiceStatus(
        platform="launchd",
        unit="com.hyprial.hyprial.daemon",
        unit_path=Path("/LaunchAgents/x.plist"),
        installed=True,
        loaded=loaded,
        running=False,
        attributed=True,
    )
    manager = SimpleNamespace(
        platform="launchd",
        uid=501,
        runner=runner,
        inspect=lambda **_k: status,
        restart=lambda *_a, **_k: pytest.fail("restart of an unloaded unit"),
    )
    return manager, status, runner


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    return services


def test_restart_after_a_launchd_stop_bootstraps_the_unloaded_unit(
    monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, _status, runner = _launchd(loaded=False)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(service_module, "_serving_daemon_pid", lambda *_a, **_k: None)
    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    monkeypatch.setattr(
        services, "_wait_for_daemon", lambda **_k: {"running": True, "pid": 31, "epoch": "e"}
    )

    launched = service_module.restart_supervised_daemon(None, timeout_seconds=5.0)

    assert launched is not None and launched.pid == 31
    assert runner.commands == [("launchctl", "bootstrap", "gui/501", "/LaunchAgents/x.plist")]


def test_an_unloaded_unit_with_a_serving_daemon_is_still_lost_custody(
    monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, _status, runner = _launchd(loaded=False)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    monkeypatch.setattr(service_module, "_serving_daemon_pid", lambda *_a, **_k: 44)

    with pytest.raises(services.CliError) as raised:
        service_module.restart_supervised_daemon(None)
    assert raised.value.code == "SERVICE_SUPERVISION_LOST"
    assert runner.commands == []


def test_identity_switch_stop_refuses_an_unknown_pid_before_the_supervisor_acts(
    monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, status, runner = _launchd(loaded=True)
    monkeypatch.setattr(services, "_daemon_probe", lambda **_k: None)

    with pytest.raises(services.CliError) as raised:
        service_module.stop_supervised_daemon((manager, status), require_known_pid=True)
    assert raised.value.code == "DAEMON_STOP_IDENTITY_UNKNOWN"
    assert runner.commands == []  # the daemon was not stopped


def test_supervisor_stop_reads_the_process_identity_before_signalling(
    monkeypatch: pytest.MonkeyPatch, services
) -> None:
    manager, status, runner = _launchd(loaded=True)
    monkeypatch.setattr(services, "_daemon_probe", lambda **_k: {"pid": 77, "epoch": "e"})

    def identity(pid: int) -> str:
        assert runner.commands == [], "identity read after the process was stopped"
        return f"start-time-of-{pid}"

    result = service_module.stop_supervised_daemon(
        (manager, status), require_known_pid=True, read_identity=identity
    )

    assert result["processIdentity"] == "start-time-of-77"
    assert runner.commands == [("launchctl", "bootout", "gui/501/com.hyprial.hyprial.daemon")]


def test_unit_stop_budgets_cover_the_daemon_teardown() -> None:
    plist = (SERVICES_DIR / "launchd.plist.template").read_text(encoding="utf-8")
    unit = (SERVICES_DIR / "systemd.service.template").read_text(encoding="utf-8")
    exit_timeout = re.search(r"<key>ExitTimeOut</key>\s*<integer>(\d+)</integer>", plist)
    stop_timeout = re.search(r"^TimeoutStopSec=(\d+)$", unit, re.MULTILINE)
    assert exit_timeout and stop_timeout
    budget = service_module.SUPERVISOR_EXIT_TIMEOUT_SECONDS
    assert int(exit_timeout.group(1)) == int(stop_timeout.group(1)) == budget
    assert budget >= TEARDOWN_BUDGETED_SECONDS + 15
    assert service_module.SUPERVISOR_STOP_TIMEOUT_SECONDS > budget


def test_daemon_run_never_holds_the_identity_lock_across_a_fence_wait(
    tmp_path: Path,
) -> None:
    """Review P3: a daemon waiting behind daemon.lock used to sit on the
    identity lock, so every login meanwhile was IDENTITY_TRANSACTION_BUSY."""

    from contextlib import contextmanager

    from hyprial.daemon import DaemonStateOwnershipFence
    from hyprial.shell.impl.cli.commands.daemon import commands as daemon_commands

    holder = DaemonStateOwnershipFence.acquire(tmp_path)
    events: list[str] = []

    @contextmanager
    def yielded():  # type: ignore[no-untyped-def]
        events.append("released")
        yield
        events.append("reacquired")

    def sleep(_seconds: float) -> None:
        assert events[-1] == "released", "slept while holding the identity lock"
        events.append("slept")
        if events.count("slept") == 3:
            holder.close()

    services = SimpleNamespace(
        _daemon_probe=lambda timeout=0.5: {"daemon": {"pid": 4242}},
        CliError=_cli_get_services().CliError,
    )
    fence = daemon_commands._acquire_daemon_fence(
        services,
        tmp_path,
        timeout=0.0,
        sleep=sleep,
        log=lambda _event: None,
        yield_identity=yielded,
    )
    try:
        assert events.count("slept") == 3
        assert events[-1] == "reacquired"
    finally:
        fence.close()
