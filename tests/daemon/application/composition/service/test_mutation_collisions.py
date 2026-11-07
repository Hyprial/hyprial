"""All mutation entry points must preserve a foreign loaded job."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
)


def collision_manager(tmp_path, platform, local, commands):
    home = tmp_path / "home"
    home.mkdir()

    def runner(command, **_kwargs):
        command = tuple(command)
        commands.append(command)
        if command[:2] == ("launchctl", "print"):
            stdout = "path = /foreign/daemon.plist\nstate = running\npid = 5252\n"
        elif command[:3] == ("systemctl", "--user", "show"):
            stdout = (
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=5252\nFragmentPath=/foreign/hyprial.service\n"
            )
        else:
            stdout = "no\n"
        return CompletedProcess(command, 0, stdout, "")

    manager = ServiceManager(
        ServiceConfig(Path("/opt/hyprial"), home, home / ".hyprial", home / "state"),
        platform=platform, runner=runner, uid=501, sleep=lambda _: None,
    )
    if local != "absent":
        manager.unit_path.parent.mkdir(parents=True)
        text = manager._rendered()
        if local == "drift":
            text += "\n"
        manager.unit_path.write_text(text)
    return manager


def snapshot(home):
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


def mutations(commands):
    return [c for c in commands if not (
        c[:2] in (("launchctl", "print"), ("loginctl", "show-user"))
        or c[:3] == ("systemctl", "--user", "show")
    )]


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
@pytest.mark.parametrize("local", ["absent", "matching", "drift"])
@pytest.mark.parametrize("operation", ["install", "uninstall", "restart", "reconcile"])
def test_all_mutations_refuse_collision_before_any_side_effect(
    tmp_path, monkeypatch, platform, local, operation,
):
    commands = []
    manager = collision_manager(tmp_path, platform, local, commands)
    before = snapshot(manager.config.home)
    effects = []
    # These private helpers must only be reachable after the shared guard.
    for name in ("_load", "_unload", "_backup_existing", "_atomic_write"):
        monkeypatch.setattr(manager, name, lambda *a, _name=name: effects.append(_name))
    manager.handoff_standalone = lambda: effects.append("handoff")
    try:
        with pytest.raises(ServiceOwnershipError) as refused:
            if operation == "restart":
                manager.restart(lambda: 5252, attempts=1)
            elif operation == "reconcile":
                manager.reconcile(lambda: 5252, stop_stray=effects.append)
            else:
                getattr(manager, operation)()
        assert refused.value.reason == (
            "supervisor-file-missing" if local == "absent" else "supervisor-label-foreign"
        )
    finally:
        assert mutations(commands) == []
        assert effects == []
        assert snapshot(manager.config.home) == before


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_cli_restart_wrapper_keeps_collision_guard(tmp_path, monkeypatch, platform):
    from types import SimpleNamespace

    from hyprial.shell.impl.cli.commands.common.support import CliError
    from hyprial.shell.impl.cli.commands.daemon import service as cli

    monkeypatch.setattr(cli, "get_services", lambda: SimpleNamespace(CliError=CliError))
    commands = []
    manager = collision_manager(tmp_path, platform, "matching", commands)
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    with pytest.raises(CliError):
        cli.restart_supervised_daemon(5252)
    assert mutations(commands) == []


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
@pytest.mark.parametrize("local", ["absent", "matching", "drift"])
def test_doctor_fix_refuses_before_stopping_or_writing(tmp_path, monkeypatch, platform, local):
    from hyprial.shell.impl.cli.commands.daemon import service as cli

    commands = []
    manager = collision_manager(tmp_path, platform, local, commands)
    before = snapshot(manager.config.home)
    stopped = []
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "_serving_daemon_pid", lambda: 5252)
    monkeypatch.setattr(cli, "_stop_stray_daemon", stopped.append)
    result = cli.daemon_service_doctor_check(serving_pid=5252, fix=True)
    assert result["status"] == "fail"
    assert result["metrics"]["reason"] in (
        "supervisor-file-missing", "supervisor-label-foreign",
    )
    assert stopped == []
    assert mutations(commands) == []
    assert snapshot(manager.config.home) == before
