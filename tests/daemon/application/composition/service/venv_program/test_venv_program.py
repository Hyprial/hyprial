"""D1 (launchd acceptance, 2026-11-16): a venv unit runs the venv's own python.

A venv's ``bin/python`` is a symlink to the base CPython outside the venv.
The unit must render that unresolved path (site-packages visible), the
attribution check and launchd job scanner must agree with the render, and an
install whose custody is never established must not leave a loaded unit.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.operations.service import ServiceConfig, ServiceManager

MODULE_ARGS = ("-m", "hyprial.cli", "daemon", "run")


def _venv(root: Path, name: str, base: Path) -> Path:
    python = root / name / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(base)
    return python


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("#!stub\n")
    return {
        "base": base,
        "venv": _venv(tmp_path, "venv", base),
        "other": _venv(tmp_path, "other-venv", base),
    }


def _config(tmp_path: Path, executable: Path, args: tuple[str, ...] = MODULE_ARGS):
    return ServiceConfig(
        executable=executable,
        home=tmp_path / "account",
        hyprial_home=tmp_path / "account" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env="/usr/bin",
        proxy_env={},
        daemon_args=args,
    )


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_the_venv_interpreter_is_rendered_unresolved(
    tmp_path: Path, layout: dict[str, Path], platform: str
) -> None:
    manager = ServiceManager(
        _config(tmp_path, layout["venv"]),
        platform=platform,
        service_home=tmp_path / "account",
        uid=501,
    )
    rendered = manager._rendered()

    assert str(layout["venv"]) in rendered
    assert str(layout["base"]) not in rendered
    if platform == "launchd":
        arguments = plistlib.loads(rendered.encode())["ProgramArguments"]
        assert arguments == [str(layout["venv"]), *MODULE_ARGS]


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_render_then_attribution_round_trips_and_rejects_another_venv(
    tmp_path: Path, layout: dict[str, Path], platform: str
) -> None:
    def manager(executable: Path) -> ServiceManager:
        return ServiceManager(
            _config(tmp_path, executable),
            platform=platform,
            service_home=tmp_path / "account",
            uid=501,
        )

    rendered = manager(layout["venv"])._rendered()

    assert manager(layout["venv"])._is_attributed(rendered) is True
    assert manager(layout["other"])._is_attributed(rendered) is False
    assert manager(layout["base"])._is_attributed(rendered) is False


def test_the_launchd_scanner_claims_a_venv_unit_only_for_that_venv(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    owner = ServiceManager(
        _config(tmp_path, layout["venv"]),
        platform="launchd",
        service_home=tmp_path / "account",
        uid=501,
    )
    foreign_config = _config(tmp_path, layout["other"])
    foreign = ServiceManager(
        foreign_config, platform="launchd", service_home=tmp_path / "account", uid=501
    )
    directory = owner.unit_path.parent
    directory.mkdir(parents=True)
    (directory / "com.example.copy.plist").write_text(
        owner._rendered(), encoding="utf-8"
    )

    claimed = {path.name for _label, path in owner._launchd_jobs()}
    assert "com.example.copy.plist" in claimed
    assert {path.name for _label, path in foreign._launchd_jobs()} == set()


def test_a_console_script_symlink_still_matches_its_resolved_script(
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools" / "bin" / "hyprial"
    script.parent.mkdir(parents=True)
    script.write_text("#!stub\n")
    link = tmp_path / "local" / "bin" / "hyprial"
    link.parent.mkdir(parents=True)
    link.symlink_to(script)
    manager = ServiceManager(
        _config(tmp_path, script, ("daemon", "run")),
        platform="launchd",
        service_home=tmp_path / "account",
        uid=501,
    )
    operator = plistlib.dumps(
        {
            "Label": "com.example.operator",
            "ProgramArguments": [str(link), "daemon", "run"],
        }
    ).decode()

    assert manager._is_attributed(operator, label="com.example.operator") is True


class LaunchdFake:
    """A stateful launchctl: a loaded job runs, but no daemon ever serves."""

    def __init__(self) -> None:
        self.loaded = False
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("launchctl", "bootstrap"):
            self.loaded = True
        if command[:2] == ("launchctl", "bootout"):
            self.loaded = False
        if command[:2] == ("launchctl", "print"):
            label = command[2].rsplit("/", 1)[1]
            if not self.loaded:
                return subprocess.CompletedProcess(
                    command,
                    113,
                    "",
                    f'Could not find service "{label}" in domain for user gui: 501',
                )
            plist = (Path(self.unit_path)).as_posix()
            return subprocess.CompletedProcess(
                command,
                0,
                f"path = {plist}\nstate = spawn scheduled\n\truns = 7\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    unit_path = ""

    def mutations(self) -> list[tuple[str, ...]]:
        return [c for c in self.commands if c[:2] != ("launchctl", "print")]


def _launchd_manager(tmp_path: Path, executable: Path) -> tuple[ServiceManager, LaunchdFake]:
    runner = LaunchdFake()
    manager = ServiceManager(
        _config(tmp_path, executable),
        platform="launchd",
        service_home=tmp_path / "account",
        runner=runner,
        uid=501,
        sleep=lambda _seconds: None,
    )
    runner.unit_path = str(manager.unit_path)
    return manager, runner


def test_lost_custody_after_a_fresh_install_unloads_and_removes_the_unit(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    manager, runner = _launchd_manager(tmp_path, layout["venv"])

    state = manager.capture_unit_state()
    manager.install()
    observed = manager.wait_for_custody(lambda: None, attempts=3)

    assert observed.supervision == "lost"
    assert manager.unit_path.is_file() and runner.loaded is True

    assert manager.revert_install(state) == "removed"

    assert runner.loaded is False
    assert not manager.unit_path.exists()
    unit = "gui/501/com.hyprial.hyprial.daemon"
    assert runner.mutations() == [
        ("launchctl", "bootstrap", "gui/501", str(manager.unit_path)),
        ("launchctl", "bootout", unit),
    ]


def test_lost_custody_after_replacing_a_unit_restores_the_previous_one(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    manager, runner = _launchd_manager(tmp_path, layout["venv"])
    previous = manager._rendered().replace("ThrottleInterval", "ThrottleIntervalOld")
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(previous, encoding="utf-8")
    runner.loaded = True
    state = manager.capture_unit_state()
    assert state == (previous, True)

    manager.install()
    assert manager.unit_path.read_text(encoding="utf-8") != previous
    runner.commands.clear()

    assert manager.revert_install(state) == "restored"

    assert manager.unit_path.read_text(encoding="utf-8") == previous
    assert runner.loaded is True
    assert [c[:2] for c in runner.mutations()] == [
        ("launchctl", "bootout"),
        ("launchctl", "bootstrap"),
    ]


def test_reverting_an_install_that_changed_nothing_touches_nothing(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    manager, runner = _launchd_manager(tmp_path, layout["venv"])
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(manager._rendered(), encoding="utf-8")
    runner.loaded = True
    state = manager.capture_unit_state()
    runner.commands.clear()

    assert manager.revert_install(state) == "unchanged"
    assert runner.mutations() == []
