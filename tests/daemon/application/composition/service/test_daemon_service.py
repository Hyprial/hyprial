from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.operations.service import (
    ServiceConfig,
    ServiceManager,
    ServiceOwnershipError,
    ServiceTemplates,
)
from hyprial.daemon.impl.operations.service import identity


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


_DEFAULT_PATH_ENV = "/Users/tester/.local/bin:/usr/bin:/bin"

_GOLDEN_LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hyprial.hyprial.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/hyprial/bin/hyprial</string>
    <string>daemon</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{home}</string>
    <key>PATH</key>
    <string>{path}</string>
    <key>HYPRIAL_HOME</key>
    <string>{hyprial_home}</string>
    <key>HARNESS_STATE_DIR</key>
    <string>{state_dir}</string>
    <key>HYPRIAL_SERVICE_MANAGED</key>
    <string>1</string>
    <key>HYPRIAL_SERVICE_MANAGER</key>
    <string>launchd</string>
    <key>HYPRIAL_SERVICE_UNIT</key>
    <string>com.hyprial.hyprial.daemon</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>ExitTimeOut</key>
  <integer>120</integer>
  <key>StandardOutPath</key>
  <string>{stdout_log}</string>
  <key>StandardErrorPath</key>
  <string>{stderr_log}</string>
</dict>
</plist>
"""

_GOLDEN_SYSTEMD = """[Unit]
Description=Harness Bridge daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="/opt/hyprial/bin/hyprial" daemon run
Environment="HOME={home}"
Environment="PATH={path}"
Environment="HYPRIAL_HOME={hyprial_home}"
Environment="HARNESS_STATE_DIR={state_dir}"
Environment="HYPRIAL_SERVICE_MANAGED=1"
Environment="HYPRIAL_SERVICE_MANAGER=systemd"
Environment="HYPRIAL_SERVICE_UNIT=hyprial.service"
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=120
StandardOutput=append:"{stdout_log}"
StandardError=append:"{stderr_log}"

[Install]
WantedBy=default.target
"""


def _identity_config(
    tmp_path: Path, *, hyprial_home: Path | None = None
) -> ServiceConfig:
    home = tmp_path / "home"
    return ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=home,
        hyprial_home=(
            home / ".hyprial" if hyprial_home is None else hyprial_home
        ),
        state_dir=(home / ".hyprial" if hyprial_home is None else hyprial_home)
        / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={},
    )


def test_default_home_render_is_byte_identical(tmp_path: Path) -> None:
    """The default home keeps today's bytes, names, and unit paths exactly."""

    config = _identity_config(tmp_path)
    templates = ServiceTemplates()
    fields = {
        "home": str(config.home.resolve()),
        "path": _DEFAULT_PATH_ENV,
        "hyprial_home": str(config.hyprial_home.resolve()),
        "state_dir": str(config.state_dir.resolve()),
        "stdout_log": str(
            (config.state_dir / "daemon-service.stdout.log").resolve()
        ),
        "stderr_log": str(
            (config.state_dir / "daemon-service.stderr.log").resolve()
        ),
    }

    assert templates.render_launchd(config) == _GOLDEN_LAUNCHD.format(**fields)
    assert templates.render_systemd(config) == _GOLDEN_SYSTEMD.format(**fields)
    assert identity.launchd_label(config) == "com.hyprial.hyprial.daemon"
    assert identity.systemd_unit(config) == "hyprial.service"
    launchd_manager = ServiceManager(
        config, platform="launchd", service_home=config.home
    )
    systemd_manager = ServiceManager(
        config, platform="systemd", service_home=config.home
    )
    assert launchd_manager.unit_path == (
        config.home / "Library" / "LaunchAgents"
        / "com.hyprial.hyprial.daemon.plist"
    )
    assert systemd_manager.unit_path == (
        config.home / ".config" / "systemd" / "user" / "hyprial.service"
    )


def test_nondefault_home_derives_deterministic_names_and_marks_the_unit(
    tmp_path: Path,
) -> None:
    hyprial_home = tmp_path / "home" / ".hyprial-acceptance"
    config = _identity_config(tmp_path, hyprial_home=hyprial_home)
    expected_id = hashlib.sha256(
        os.fsencode(os.path.realpath(hyprial_home))
    ).hexdigest()[:12]
    label = f"com.hyprial.hyprial.daemon.{expected_id}"
    unit = f"hyprial-{expected_id}.service"

    assert identity.home_unit_id(hyprial_home) == expected_id
    assert len(expected_id) == 12
    assert set(expected_id) <= set("0123456789abcdef")
    assert identity.home_unit_id(hyprial_home) == identity.home_unit_id(
        hyprial_home
    )
    assert identity.launchd_label(config) == label
    assert identity.systemd_unit(config) == unit
    launchd_manager = ServiceManager(
        config, platform="launchd", service_home=config.home
    )
    systemd_manager = ServiceManager(
        config, platform="systemd", service_home=config.home
    )
    assert launchd_manager.unit_path.name == f"{label}.plist"
    assert systemd_manager.unit_path.name == unit

    launchd_rendered = ServiceTemplates().render_launchd(config)
    assert f"<string>{label}</string>" in launchd_rendered
    systemd_rendered = ServiceTemplates().render_systemd(config)
    assert f'Environment="HYPRIAL_SERVICE_UNIT={unit}"' in systemd_rendered
    assert f"<string>{hyprial_home.resolve()}</string>" in launchd_rendered


def test_canonicalization_collapses_symlink_trailing_slash_and_dot_segments(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-home"
    target.mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(target)

    base = identity.home_unit_id(target)

    assert identity.home_unit_id(link) == base
    assert identity.home_unit_id(Path(f"{target}/")) == base
    assert identity.home_unit_id(Path(f"{target}/.")) == base
    assert identity.home_unit_id(Path(f"{target}/./sub/..")) == base


def test_case_variant_spelling_is_a_distinct_unit_id() -> None:
    """G-M1 (locked limitation): realpath does not normalize case of existing
    directories, so two spellings of one case-insensitive directory are two
    ids and two units. Always spell the same home the same way."""

    assert identity.home_unit_id(Path("/Users/tester/Home")) != identity.home_unit_id(
        Path("/Users/tester/home")
    )


def test_proxy_variables_render_into_both_units_with_escaping(
    tmp_path: Path,
) -> None:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={
            "HTTP_PROXY": "http://user:pa%25ss@proxy:8080",
            "HTTPS_PROXY": "https://proxy:8443",
            "NO_PROXY": "localhost,.internal & <local> 'quoted'",
        },
    )
    templates = ServiceTemplates()

    launchd = templates.render_launchd(config)
    systemd = templates.render_systemd(config)

    assert "<key>HTTP_PROXY</key>\n    <string>http://user:pa%25ss@proxy:8080</string>" in launchd
    assert "<key>HTTPS_PROXY</key>\n    <string>https://proxy:8443</string>" in launchd
    assert (
        "<key>NO_PROXY</key>\n    <string>localhost,.internal &amp; "
        "&lt;local&gt; &apos;quoted&apos;</string>"
    ) in launchd
    assert (
        'Environment="HTTP_PROXY=http://user:pa%%25ss@proxy:8080"' in systemd
    ), systemd
    assert 'Environment="HTTPS_PROXY=https://proxy:8443"' in systemd
    assert (
        'Environment="NO_PROXY=localhost,.internal &amp; '
    ) not in systemd
    assert 'Environment="NO_PROXY=localhost,.internal & <local> \'quoted\'"' in systemd


def test_systemd_proxy_values_double_percent_and_escape_quotes(
    tmp_path: Path,
) -> None:
    config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={"NO_PROXY": "a%b\\c\"d"},
    )

    rendered = ServiceTemplates().render_systemd(config)

    assert 'Environment="NO_PROXY=a%%b\\\\c\\\"d"' in rendered


def test_absent_and_empty_proxy_values_render_deterministically(
    tmp_path: Path,
) -> None:
    """Absent keys render nothing (byte-identical to a no-proxy render);
    a present-but-empty value renders as an empty value, not a missing key."""

    base_config = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={},
    )
    templates = ServiceTemplates()
    baseline_launchd = templates.render_launchd(base_config)
    baseline_systemd = templates.render_systemd(base_config)

    absent = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={"HTTPS_PROXY": "https://proxy:8443"},
    )
    absent_launchd = templates.render_launchd(absent)
    absent_systemd = templates.render_systemd(absent)
    assert "HTTP_PROXY" not in absent_launchd
    assert "NO_PROXY" not in absent_launchd
    assert "HTTPS_PROXY" in absent_launchd
    assert "HTTP_PROXY" not in absent_systemd
    assert "NO_PROXY" not in absent_systemd

    empty = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={"NO_PROXY": ""},
    )
    assert "<key>NO_PROXY</key>\n    <string></string>" in templates.render_launchd(empty)
    assert 'Environment="NO_PROXY="' in templates.render_systemd(empty)

    ordered = ServiceConfig(
        executable=Path("/opt/hyprial/bin/hyprial"),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={"NO_PROXY": "localhost", "HTTP_PROXY": "http://p:1"},
    )
    ordered_launchd = templates.render_launchd(ordered)
    assert ordered_launchd.index("HTTP_PROXY") < ordered_launchd.index("NO_PROXY")
    assert baseline_launchd.index("HYPRIAL_SERVICE_UNIT") < len(baseline_launchd)
    assert "PROXY" not in baseline_launchd and "PROXY" not in baseline_systemd


def test_module_form_renders_the_python_m_invocation(tmp_path: Path) -> None:
    config = ServiceConfig(
        executable=Path(sys.executable),
        home=tmp_path / "home",
        hyprial_home=tmp_path / "home" / ".hyprial",
        state_dir=tmp_path / "state",
        path_env=_DEFAULT_PATH_ENV,
        proxy_env={},
        daemon_args=("-m", "hyprial.cli", "daemon", "run"),
    )
    templates = ServiceTemplates()

    resolved_interpreter = os.path.abspath(sys.executable)
    launchd = templates.render_launchd(config)
    systemd = templates.render_systemd(config)

    assert (
        f"    <string>{resolved_interpreter}</string>\n    <string>-m</string>\n"
        "    <string>hyprial.cli</string>\n    <string>daemon</string>\n"
        "    <string>run</string>"
    ) in launchd
    assert f'ExecStart="{resolved_interpreter}" -m hyprial.cli daemon run' in systemd


def test_default_and_derived_homes_own_distinct_units_and_uninstall_removes_only_its_own(
    tmp_path: Path,
) -> None:
    default_config = _identity_config(tmp_path)
    derived_config = _identity_config(
        tmp_path, hyprial_home=tmp_path / "home" / ".hyprial-acceptance"
    )
    default = ServiceManager(
        default_config,
        platform="systemd",
        service_home=default_config.home,
        runner=_not_loaded_runner,
        username="tester",
        sleep=lambda _seconds: None,
    )
    derived = ServiceManager(
        derived_config,
        platform="systemd",
        service_home=derived_config.home,
        runner=_not_loaded_runner,
        username="tester",
        sleep=lambda _seconds: None,
    )

    assert default.unit_path != derived.unit_path
    default.unit_path.parent.mkdir(parents=True, exist_ok=True)
    for manager in (default, derived):
        manager.unit_path.parent.mkdir(parents=True, exist_ok=True)
        manager.unit_path.write_text(
            ServiceTemplates().render_systemd(manager.config), encoding="utf-8"
        )

    removed = derived.uninstall()

    assert removed.installed is False
    assert derived.unit_path.exists() is False
    assert default.unit_path.is_file()


def _not_loaded_runner(command, *, timeout=None):
    del timeout
    command = tuple(command)
    if command[:3] == ("systemctl", "--user", "show"):
        return subprocess.CompletedProcess(command, 4, "", "nicht gefunden")
    if command[:2] == ("loginctl", "show-user"):
        return subprocess.CompletedProcess(command, 0, "no\n", "")
    return subprocess.CompletedProcess(command, 0, "", "")


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
