from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
    ServiceTemplates,
)


def test_launchd_template_keeps_daemon_alive_and_preserves_isolated_state(
    tmp_path: Path,
) -> None:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "hyprial home",
        state_dir=tmp_path / "state & logs",
    )

    rendered = ServiceTemplates().render_launchd(config)

    assert "<key>RunAtLoad</key>" in rendered
    assert "<key>KeepAlive</key>" in rendered
    assert "HYPRIAL_HOME" in rendered
    assert "HARNESS_STATE_DIR" in rendered
    assert "state &amp; logs" in rendered
    assert "<string>daemon</string>" in rendered
    assert "<string>run</string>" in rendered


def test_systemd_template_restarts_and_uses_control_group_shutdown(
    tmp_path: Path,
) -> None:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "hyprial home",
        state_dir=tmp_path / "state",
    )

    rendered = ServiceTemplates().render_systemd(config)

    assert "Restart=always" in rendered
    assert "KillMode=control-group" in rendered
    assert "WantedBy=default.target" in rendered
    assert 'Environment="HYPRIAL_HOME=' in rendered
    assert 'Environment="HARNESS_STATE_DIR=' in rendered
    assert " daemon run" in rendered


class FakeSystemd:
    def __init__(self) -> None:
        self.loaded = False
        self.linger = False
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        self.commands.append(command)
        if command[:3] == ("systemctl", "--user", "show"):
            if not self.loaded:
                return completed(
                    command,
                    4,
                    stderr="Unit hyprial.service could not be found.",
                )
            return completed(
                command,
                stdout=(
                    "LoadState=loaded\nActiveState=active\n"
                    "SubState=running\nMainPID=5252\n"
                ),
            )
        if command[:2] == ("loginctl", "show-user"):
            return completed(command, stdout="yes\n" if self.linger else "no\n")
        if command[:2] == ("loginctl", "enable-linger"):
            self.linger = True
        if "enable" in command:
            self.loaded = True
        if "disable" in command:
            self.loaded = False
        return completed(command)


def completed(
    command: tuple[str, ...],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_systemd_manager_install_and_uninstall_are_idempotent(tmp_path: Path) -> None:
    runner = FakeSystemd()
    handoffs: list[str] = []
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=tmp_path,
            hyprial_home=tmp_path / ".hyprial",
            state_dir=tmp_path / "state",
        ),
        platform="systemd",
        service_home=tmp_path,
        runner=runner,
        handoff_standalone=lambda: handoffs.append("handoff"),
        username="allen",
    )

    first = manager.install()
    second = manager.install()
    removed = manager.uninstall()
    removed_again = manager.uninstall()

    assert first.running and first.changed is True and first.boot_persistent is True
    assert second.running and second.changed is False
    assert handoffs == ["handoff"]
    assert removed.installed is False and removed.changed is True
    assert removed_again.installed is False and removed_again.changed is False


class FakeLaunchd:
    def __init__(self) -> None:
        self.loaded = False
        self.bootstrap_attempts = 0

    def __call__(self, command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        if command[:2] == ("launchctl", "print"):
            if not self.loaded:
                return completed(
                    command,
                    113,
                    stderr=(
                        'Could not find service "com.hyprial.hyprial.daemon" '
                        "in domain for user gui: 501"
                    ),
                )
            return completed(command, stdout="state = running\n\tpid = 4242\n")
        if command[:2] == ("launchctl", "bootstrap"):
            self.bootstrap_attempts += 1
            if self.bootstrap_attempts == 1:
                return subprocess.CompletedProcess(
                    command, 5, "", "Bootstrap failed: 5: Input/output error"
                )
            self.loaded = True
        if command[:2] == ("launchctl", "bootout"):
            self.loaded = False
        return completed(command)


def test_launchd_manager_install_and_uninstall_are_idempotent(tmp_path: Path) -> None:
    runner = FakeLaunchd()
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=tmp_path,
            hyprial_home=tmp_path / ".hyprial",
            state_dir=tmp_path / "state",
        ),
        platform="launchd",
        service_home=tmp_path,
        runner=runner,
        uid=501,
        sleep=lambda _seconds: None,
    )

    first = manager.install()
    second = manager.install()
    removed = manager.uninstall()
    removed_again = manager.uninstall()

    assert first.running and first.pid == 4242 and first.changed is True
    assert runner.bootstrap_attempts == 2
    assert second.changed is False
    assert removed.installed is False and removed.loaded is False
    assert removed.changed is True and removed_again.changed is False


def _recording_runner(commands: list[tuple[str, ...]]):
    def runner(command, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        commands.append(tuple(command))
        return completed(tuple(command))

    return runner


def test_install_refuses_a_hyprial_home_outside_the_operating_home(
    tmp_path: Path,
) -> None:
    """A supervisor unit is only ever written under the operating home.

    Regression (2026-10-30): ``hyprial init`` installs autostart by default, so
    any caller holding a disposable ``HYPRIAL_HOME`` -- this repository's own
    daemon tests -- and an unchanged ``HOME`` wrote a boot-persistent launchd job
    pointing at the throwaway home into the developer's real
    ``~/Library/LaunchAgents``, where it outlived the test.
    """

    operating_home = tmp_path / "operating-home"
    operating_home.mkdir()
    disposable = tmp_path / "disposable"
    disposable.mkdir()
    commands: list[tuple[str, ...]] = []
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=disposable / "hyprial",
            state_dir=disposable / "state",
        ),
        platform="launchd",
        service_home=operating_home,
        runner=_recording_runner(commands),
        uid=501,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.install()

    assert refused.value.reason == "hyprial-home-outside-operating-home"
    assert not manager.unit_path.exists()
    # The refusal comes before any supervisor command: no probe, no bootstrap.
    assert commands == []


def test_uninstall_refuses_a_hyprial_home_outside_the_operating_home(
    tmp_path: Path,
) -> None:
    """Uninstall is the same write class: it must not unload a foreign job."""

    operating_home = tmp_path / "operating-home"
    operating_home.mkdir()
    disposable = tmp_path / "disposable"
    disposable.mkdir()
    commands: list[tuple[str, ...]] = []
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=disposable / "hyprial",
            state_dir=disposable / "state",
        ),
        platform="launchd",
        service_home=operating_home,
        runner=_recording_runner(commands),
        uid=501,
        sleep=lambda _seconds: None,
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(
        manager.templates.render_launchd(manager.config), encoding="utf-8"
    )

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.uninstall()

    assert refused.value.reason == "hyprial-home-outside-operating-home"
    assert manager.unit_path.is_file()
    assert commands == []


def _print_output(*, path: str, pid: int = 4242) -> str:
    return (
        "gui/501/com.hyprial.hyprial.daemon = {\n"
        "\tactive count = 1\n"
        f"\tpath = {path}\n"
        "\ttype = LaunchAgent\n"
        "\tstate = running\n"
        f"\tpid = {pid}\n"
        "}\n"
    )


def test_a_loaded_label_from_another_home_is_not_this_homes_supervisor(
    tmp_path: Path,
) -> None:
    """The launchd label is machine-global; the unit file is per home.

    A loaded ``com.hyprial.hyprial.daemon`` whose plist lives in another home
    (the operator's real one, or a leaked test unit) says nothing about THIS
    home.  Reporting it as this home's supervisor made ``doctor`` fail with
    "the loaded supervisor service file is missing" for every other home on the
    machine (2026-10-30).
    """

    operating_home = tmp_path / "operating-home"
    foreign = Path("/Users/operator/Library/LaunchAgents/com.hyprial.hyprial.daemon.plist")

    def runner(command, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        return completed(
            tuple(command), stdout=_print_output(path=str(foreign))
        )

    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=operating_home / ".hyprial",
            state_dir=operating_home / ".hyprial" / "state",
        ),
        platform="launchd",
        service_home=operating_home,
        runner=runner,
        uid=501,
        sleep=lambda _seconds: None,
    )

    status = manager.status()
    inspected = manager.inspect(serving_pid=None)

    assert status.loaded is False
    assert status.installed is False
    # "no supervisor here", not "our supervisor lost its file".
    assert inspected.supervision == "unsupervised"


class FakeForeignSystemd:
    """A user unit loaded from another home's unit directory."""

    def __init__(self, fragment: Path) -> None:
        self.fragment = fragment
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("loginctl", "show-user"):
            return completed(command, stdout="yes\n")
        return completed(
            command,
            stdout=(
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=5252\n"
                f"FragmentPath={self.fragment}\n"
            ),
        )


def test_a_loaded_systemd_unit_from_another_home_is_not_this_homes_supervisor(
    tmp_path: Path,
) -> None:
    operating_home = tmp_path / "operating-home"
    foreign = Path("/home/operator/.config/systemd/user/hyprial.service")
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=operating_home / ".hyprial",
            state_dir=operating_home / ".hyprial" / "state",
        ),
        platform="systemd",
        service_home=operating_home,
        runner=FakeForeignSystemd(foreign),
        username="allen",
    )

    status = manager.status()

    assert status.loaded is False
    assert status.installed is False
    # A foreign unit is neither this home's supervisor nor its running daemon.
    assert status.running is False
    assert status.pid is None


def _collision_manager(
    tmp_path: Path, commands: list[tuple[str, ...]], *, local_unit: bool
):
    """A home with no (or a matching) unit file, and a label loaded elsewhere.

    Mirrors the reviewer repro for 0633c00d0: HYPRIAL_HOME is *inside* the
    operating home, so the home guard passes, and the supervisor reports that
    the machine-global label was loaded from another unit file.
    """

    operating_home = tmp_path / "home"
    foreign = tmp_path / "operator" / "Library" / "LaunchAgents"
    foreign.mkdir(parents=True)
    foreign_plist = foreign / "com.hyprial.hyprial.daemon.plist"

    def runner(command, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        commands.append(tuple(command))
        return completed(tuple(command), stdout=_print_output(path=str(foreign_plist)))

    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=operating_home / ".hyprial",
            state_dir=operating_home / ".hyprial" / "state",
        ),
        platform="launchd",
        service_home=operating_home,
        runner=runner,
        uid=501,
        sleep=lambda _seconds: None,
    )
    if local_unit:
        manager.unit_path.parent.mkdir(parents=True)
        manager.unit_path.write_text(
            manager.templates.render_launchd(manager.config), encoding="utf-8"
        )
    return manager, foreign_plist


def _mutations(commands: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        command
        for command in commands
        if command[:2]
        not in {("launchctl", "print"), ("systemctl", "--user"), ("loginctl", "show-user")}
    ]


def test_install_refuses_a_label_loaded_from_another_unit_file(
    tmp_path: Path,
) -> None:
    """A foreign loaded label is a collision, not an absent supervisor.

    ``status()`` must not call another home's job this home's supervisor, but
    ``install()`` still has to refuse: writing our unit and bootstrapping it
    under the same machine-global label collides with the loaded job and then
    reports that job's pid as our custody.
    """

    commands: list[tuple[str, ...]] = []
    manager, foreign_plist = _collision_manager(
        tmp_path, commands, local_unit=False
    )

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.install()

    assert refused.value.reason == "supervisor-file-missing"
    assert not manager.unit_path.exists()
    assert _mutations(commands) == []
    assert foreign_plist.is_file() is False


def test_install_refuses_a_foreign_label_instead_of_overwriting_its_own_file(
    tmp_path: Path,
) -> None:
    """Even with our file on disk, a foreign loaded job is not ours to replace."""

    commands: list[tuple[str, ...]] = []
    manager, _foreign = _collision_manager(tmp_path, commands, local_unit=True)
    before = manager.unit_path.read_text(encoding="utf-8")

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.install()

    assert refused.value.reason == "supervisor-label-foreign"
    assert manager.unit_path.read_text(encoding="utf-8") == before
    assert _mutations(commands) == []


def test_uninstall_refuses_a_label_loaded_from_another_unit_file(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    manager, _foreign = _collision_manager(tmp_path, commands, local_unit=False)

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.uninstall()

    assert refused.value.reason == "supervisor-file-missing"
    assert _mutations(commands) == []


class FakeCollidingSystemd:
    """A user unit loaded from another home's directory; records every command."""

    def __init__(self, fragment: Path) -> None:
        self.fragment = fragment
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        command = tuple(command)
        self.commands.append(command)
        if command[:2] == ("loginctl", "show-user"):
            return completed(command, stdout="no\n")
        return completed(
            command,
            stdout=(
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=5252\n"
                f"FragmentPath={self.fragment}\n"
            ),
        )


def test_systemd_install_refuses_a_foreign_unit_without_enabling_it(
    tmp_path: Path,
) -> None:
    operating_home = tmp_path / "home"
    runner = FakeCollidingSystemd(
        Path("/home/operator/.config/systemd/user/hyprial.service")
    )
    manager = ServiceManager(
        ServiceConfig(
            executable=Path("/opt/hyprial/bin/hyprial"),
            home=operating_home,
            hyprial_home=operating_home / ".hyprial",
            state_dir=operating_home / ".hyprial" / "state",
        ),
        platform="systemd",
        service_home=operating_home,
        runner=runner,
        username="allen",
    )

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.install()

    assert refused.value.reason == "supervisor-file-missing"
    assert not manager.unit_path.exists()
    assert [
        command
        for command in runner.commands
        if command[:2] in {("systemctl", "--user"), ("loginctl", "enable-linger")}
        and command[-2:] != ["--property=Linger", "--value"]
        and "show" not in command
    ] == []


def test_restart_refuses_a_label_loaded_from_another_unit_file(
    tmp_path: Path,
) -> None:
    """``launchctl kickstart`` addresses the shared label, not our file.

    With a local unit file present (so ``attributed`` is true) the parent
    restarted whatever the machine-global label pointed at -- another home's
    daemon.  That is a mutation against a service this home does not own.
    """

    commands: list[tuple[str, ...]] = []
    manager, _foreign = _collision_manager(tmp_path, commands, local_unit=True)

    with pytest.raises(ServiceOwnershipError) as refused:
        manager.restart(lambda: 4242)

    assert refused.value.reason == "supervisor-label-foreign"
    assert not any(command[:2] == ("launchctl", "kickstart") for command in commands)
    assert _mutations(commands) == []
