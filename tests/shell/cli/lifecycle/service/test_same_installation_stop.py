"""N1: ``daemon stop`` stops a module-form unit by custody, and says when it cannot.

A unit installed as ``<venv>/bin/python -m hyprial.cli daemon run`` is this
installation's even when the CLI computes ``<venv>/bin/python3``: the stop
goes through the manager, so KeepAlive cannot restart the daemon. A unit that
is genuinely foreign is not touched; the IPC stop still happens and the
warning on stderr says a supervisor may restart the daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager
from hyprial.shell.impl.cli.commands.common import daemon_stop
from hyprial.shell.impl.cli.commands.daemon import service as service_module

from .test_stop_routing import DAEMON_PID, ManagerFake, stop_env  # noqa: F401

MODULE_ARGS = ("-m", "hyprial.cli", "daemon", "run")


def _manager(tmp_path: Path, executable: Path, platform: str, runner=None) -> ServiceManager:  # type: ignore[no-untyped-def]
    return ServiceManager(
        ServiceConfig(
            executable=executable,
            home=tmp_path / "account",
            hyprial_home=tmp_path / "home",
            state_dir=tmp_path / "state",
            daemon_args=MODULE_ARGS,
        ),
        platform=platform,
        runner=runner,
        uid=501,
        username="user",
    )


@pytest.fixture
def interpreters(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("#!stub\n")
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(base)
    (bin_dir / "python3").symlink_to("python")
    return {"base": base, "module": bin_dir / "python", "console": bin_dir / "python3"}


def _install_unit(tmp_path: Path, written_by: Path, used_by: Path, platform: str, monkeypatch):  # type: ignore[no-untyped-def]
    probe = _manager(tmp_path, written_by, platform)
    runner = ManagerFake(platform, probe.unit_path, loaded=True, job_pid=DAEMON_PID)
    probe.unit_path.parent.mkdir(parents=True)
    probe.unit_path.write_text(probe._rendered(), encoding="utf-8")
    manager = _manager(tmp_path, used_by, platform, runner)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    return manager, runner


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_stop_by_the_console_script_spelling_goes_through_the_manager(
    stop_env, tmp_path: Path, interpreters, monkeypatch, capsys, platform: str  # noqa: F811
) -> None:
    _services, ipc, _build = stop_env
    manager, runner = _install_unit(
        tmp_path, interpreters["module"], interpreters["console"], platform, monkeypatch
    )

    result = daemon_stop._stop_daemon_for_operator()

    verb = ("systemctl", "--user", "stop") if platform == "systemd" else ("launchctl", "bootout")
    assert [c[: len(verb)] for c in runner.mutations()] == [verb]
    assert ipc == []
    assert result["supervisor"] == {"platform": platform, "unit": manager.status().unit}
    assert result["teardownCompleted"] is True
    assert "not attributable" not in capsys.readouterr().err


@pytest.mark.parametrize("platform", ["systemd", "launchd"])
def test_a_genuinely_foreign_unit_is_not_stopped_and_the_stop_says_so(
    stop_env, tmp_path: Path, interpreters, monkeypatch, capsys, platform: str  # noqa: F811
) -> None:
    _services, ipc, _build = stop_env
    _manager_unused, runner = _install_unit(
        tmp_path, interpreters["base"], interpreters["console"], platform, monkeypatch
    )

    result = daemon_stop._stop_daemon_for_operator()

    assert runner.mutations() == []
    assert ipc == [("shutdown",)]
    assert result["teardownCompleted"] is True
    assert "not attributable" in capsys.readouterr().err
