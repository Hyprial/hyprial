"""Disposable subprocess homes and a read-only real-user service tripwire.

HOME and XDG paths isolate files, not launchd's UID domain. Product service
management must reject disposable homes; integration tests use --no-service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def isolated_environment(root: Path) -> dict[str, str]:
    """Keep subprocess state and user configuration inside one disposable root."""

    root = root.resolve()
    home = root / "operating-home"
    paths = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_DATA_HOME": home / ".local/share",
        "XDG_STATE_HOME": home / ".local/state",
        "XDG_RUNTIME_DIR": root / "runtime",
    }
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(("HYPRIAL_", "HARNESS_", "DBUS_")):
            env.pop(key)
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    env.update({
        "HYPRIAL_HOME": str(root / "home"),
        "HARNESS_STATE_DIR": str(root / "state"),
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_OWNER": "hyprial-unit-owner",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    })
    return env


def assert_root_local_environment(root: Path, env: dict[str, str]) -> None:
    """Check final subprocess paths after scenario-specific overrides."""

    for key in (
        "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "HYPRIAL_HOME", "HARNESS_STATE_DIR",
    ):
        assert Path(env[key]).resolve().is_relative_to(root.resolve()), (key, env[key])


def remove_isolated_service_files(root: Path, env: dict[str, str]) -> None:
    """Remove only exact test-local unit files; never call a user service manager."""

    root = root.resolve()
    home = Path(env["HOME"]).resolve()
    assert home.is_relative_to(root) and home != root
    for suffix in (
        "Library/LaunchAgents/com.hyprial.hyprial.daemon.plist",
        ".config/systemd/user/hyprial.service",
    ):
        path = home / suffix
        assert path.resolve().is_relative_to(home)
        path.unlink(missing_ok=True)


def service_unit_snapshot(home: Path) -> dict[str, int]:
    """List only names and lstat mtimes; never open a unit's contents."""

    result = {}
    for suffix in ("Library/LaunchAgents", ".config/systemd/user"):
        directory = home / suffix
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if "hyprial" in entry.name:
                        result[f"{suffix}/{entry.name}"] = entry.stat(follow_symlinks=False).st_mtime_ns
        except FileNotFoundError:
            pass
    return result


class ServiceUnitTripwire:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.before: dict[str, int] = {}
        self.changed: list[str] = []

    def pytest_sessionstart(self, session) -> None:
        self.before = service_unit_snapshot(self.home)

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        after = service_unit_snapshot(self.home)
        self.changed = sorted(
            name for name in self.before.keys() | after.keys()
            if self.before.get(name) != after.get(name)
        )
        if self.changed:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.changed:
            terminalreporter.write_sep("!", "REAL USER SERVICE UNIT CHANGED")
            for name in self.changed:
                terminalreporter.write_line(name)


def real_account_home() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)
