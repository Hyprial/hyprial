"""``hyprial gui`` never installs or re-homes an OS service (owner line, 2026-11-16).

Decision: the GUI's ensure hook is driven here through the real launch
primitive with the Z-8 fakes (``FakeServiceManager`` records, a real
``ServiceManager`` runs over ``FakeRunner``); no test reaches launchctl,
systemctl or a real unit directory.  The suite pins both service opt-outs to
"0", so each test opts in the way the Z-8c tests do, which makes the
auto-install path reachable and the ``autoinstall=False`` line observable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.daemon import service as service_module
from hyprial.shell.impl.cli.commands.gui import commands
from tests.shell.cli.daemon.startup.test_daemon_autoinstall import (
    FakeServiceManager,
    _opt_in,
)
from tests.shell.cli.daemon.test_supervisor_routing import _manager

RUNNING = {"running": True, "pid": 321, "epoch": "epoch-1"}


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    return services


def _daemon_down(monkeypatch: pytest.MonkeyPatch, services: Any) -> None:
    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )


def _daemon_up(monkeypatch: pytest.MonkeyPatch, services: Any) -> None:
    monkeypatch.setattr(services, "_daemon_probe", lambda **_k: dict(RUNNING))


def _record_spawns(monkeypatch: pytest.MonkeyPatch, services: Any) -> list[dict[str, Any]]:
    spawned: list[dict[str, Any]] = []

    def spawn(**kwargs: Any) -> DaemonLaunchResult:
        spawned.append(kwargs)
        return DaemonLaunchResult.launched({"running": True, "pid": 5, "epoch": "cli"})

    monkeypatch.setattr(services, "_launch_daemon_process_locked", spawn)
    return spawned


def _forbid_spawns(monkeypatch: pytest.MonkeyPatch, services: Any) -> None:
    def forbidden(**_kwargs: Any) -> DaemonLaunchResult:
        raise AssertionError("the GUI must not spawn a detached daemon here")

    monkeypatch.setattr(services, "_launch_daemon_process_locked", forbidden)


def _gui_start(services: Any) -> Any:
    return commands._daemon_starter(services, json_output=True)()


def _assert_manager_untouched(manager: FakeServiceManager) -> None:
    assert manager.install_calls == 0
    assert not manager.unit_path.exists()
    assert manager.runner.commands == []


def test_no_unit_and_daemon_down_starts_detached_without_touching_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _daemon_down(monkeypatch, services)
    spawned = _record_spawns(monkeypatch, services)

    launched = _gui_start(services)

    assert len(spawned) == 1
    assert spawned[0]["listen"] is None and spawned[0]["connect"] is None
    assert launched.running and launched.pid == 5
    _assert_manager_untouched(manager)


def test_no_unit_and_daemon_running_detached_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _daemon_up(monkeypatch, services)

    launched = _gui_start(services)

    assert launched.running and launched.pid == RUNNING["pid"]
    assert launched.epoch == RUNNING["epoch"]
    _assert_manager_untouched(manager)


def test_installed_unit_and_daemon_down_starts_through_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager, runner = _manager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _daemon_down(monkeypatch, services)
    _forbid_spawns(monkeypatch, services)
    monkeypatch.setattr(
        services, "_wait_for_daemon", lambda **_k: {"running": True, "pid": 77, "epoch": "e-1"}
    )

    launched = _gui_start(services)

    assert runner.mutations() == [("systemctl", "--user", "start", manager.unit_path.name)]
    assert launched.running and launched.pid == 77


def test_installed_unit_and_daemon_running_sends_nothing_to_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager, runner = _manager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _daemon_up(monkeypatch, services)
    _forbid_spawns(monkeypatch, services)

    launched = _gui_start(services)

    assert launched.running and launched.pid == RUNNING["pid"]
    assert runner.mutations() == []
