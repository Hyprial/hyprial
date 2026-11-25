"""N1: a module-form unit is recognised through the console script's spelling.

The unit is installed with ``<venv>/bin/python`` (module form); the CLI later
runs as a console script whose shebang is ``<venv>/bin/python3``, so it
computes that spelling. Both must be the same installation; the base CPython
(the venv's resolved target) must stay a different one (D1-4).
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
)

MODULE_ARGS = ("-m", "hyprial.cli", "daemon", "run")
JOB_PID = 4242


class Supervisor:
    """A stateful launchctl/systemctl whose loaded unit runs as ``JOB_PID``."""

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.loaded = False
        self.unit_path = ""
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[0] == "loginctl":
            return subprocess.CompletedProcess(command, 0, "yes\n", "")
        if command[:2] == ("launchctl", "bootstrap") or command[:3] == (
            "systemctl",
            "--user",
            "enable",
        ):
            self.loaded = True
        if command[:2] == ("launchctl", "bootout") or command[:3] == (
            "systemctl",
            "--user",
            "disable",
        ):
            self.loaded = False
        if command[:3] == ("systemctl", "--user", "show"):
            if not self.loaded:
                return subprocess.CompletedProcess(
                    command, 4, "LoadState=not-found\n", "not found"
                )
            return subprocess.CompletedProcess(
                command,
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                f"MainPID={JOB_PID}\nNRestarts=0\nExecMainStatus=0\n"
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
            return subprocess.CompletedProcess(
                command,
                0,
                f"path = {self.unit_path}\nstate = running\n\tpid = {JOB_PID}\n\truns = 1\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            c
            for c in self.commands
            if c[:2] != ("launchctl", "print")
            and c[:3] != ("systemctl", "--user", "show")
            and c[0] != "loginctl"
        ]


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("#!stub\n")
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(base)
    (bin_dir / "python3").symlink_to("python")
    return {
        "base": base,
        "module": bin_dir / "python",
        "console": bin_dir / "python3",
        "root": tmp_path,
    }


def _manager(
    root: Path, executable: Path, platform: str, runner: Supervisor
) -> ServiceManager:
    manager = ServiceManager(
        ServiceConfig(
            executable=executable,
            home=root / "account",
            hyprial_home=root / "account" / ".hyprial",
            state_dir=root / "state",
            path_env="/usr/bin",
            proxy_env={},
            daemon_args=MODULE_ARGS,
        ),
        platform=platform,
        service_home=root / "account",
        runner=runner,
        uid=501,
        username="user",
        sleep=lambda _seconds: None,
    )
    runner.unit_path = str(manager.unit_path)
    return manager


@pytest.fixture(params=["launchd", "systemd"])
def installed(request, layout):  # type: ignore[no-untyped-def]
    platform = request.param
    runner = Supervisor(platform)
    installer = _manager(layout["root"], layout["module"], platform, runner)
    installer.install()
    runner.commands.clear()
    return platform, runner, layout


def test_status_through_the_other_spelling_is_attributed_and_supervised(
    installed,
) -> None:
    platform, runner, layout = installed
    cli = _manager(layout["root"], layout["console"], platform, runner)

    status = cli.inspect(serving_pid=JOB_PID)

    assert status.attributed is True
    assert status.supervision == "supervised"
    assert status.loaded is True and status.pid == JOB_PID


def test_uninstall_through_the_other_spelling_is_allowed(installed) -> None:
    platform, runner, layout = installed
    cli = _manager(layout["root"], layout["console"], platform, runner)

    cli.uninstall()

    assert runner.loaded is False
    assert not cli.unit_path.exists()


def test_reinstall_through_the_other_spelling_is_allowed_and_idempotent(
    installed,
) -> None:
    platform, runner, layout = installed
    cli = _manager(layout["root"], layout["console"], platform, runner)

    first = cli.install()
    again = cli.install()

    assert first.loaded is True
    assert again.changed is False
    assert cli.inspect(serving_pid=JOB_PID).attributed is True


def test_the_launchd_scanner_claims_a_unit_copy_under_the_other_spelling(
    layout,
) -> None:
    runner = Supervisor("launchd")
    installer = _manager(layout["root"], layout["module"], "launchd", runner)
    cli = _manager(layout["root"], layout["console"], "launchd", runner)
    directory = installer.unit_path.parent
    directory.mkdir(parents=True)
    (directory / "com.example.copy.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.example.copy",
                "ProgramArguments": [str(layout["module"]), *MODULE_ARGS],
            }
        )
    )

    assert {p.name for _label, p in cli._launchd_jobs()} == {"com.example.copy.plist"}


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_reinstalling_with_the_base_interpreter_is_still_refused(
    layout, platform: str
) -> None:
    runner = Supervisor(platform)
    _manager(layout["root"], layout["module"], platform, runner).install()
    runner.commands.clear()
    base = _manager(layout["root"], layout["base"], platform, runner)

    assert base.inspect(serving_pid=JOB_PID).attributed is False
    with pytest.raises(ServiceOwnershipError):
        base.install()
    with pytest.raises(ServiceOwnershipError):
        base.uninstall()
    assert runner.mutations() == []
