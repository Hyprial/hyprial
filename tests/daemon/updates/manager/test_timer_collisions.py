"""Legacy cleanup must not disable another home's autoupdate job."""

from subprocess import CompletedProcess

import pytest

from hyprial.daemon.impl.autoupdate import (
    AUTOUPDATE_LABEL,
    SYSTEMD_SERVICE_UNIT,
    SYSTEMD_TIMER_UNIT,
    remove_legacy_timer_units,
)


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
@pytest.mark.parametrize("foreign_unit", ["service", "timer"])
@pytest.mark.parametrize("local", [False, True])
def test_legacy_cleanup_refuses_foreign_loaded_path(
    tmp_path, platform, foreign_unit, local,
):
    home = tmp_path.resolve()
    if platform == "launchd":
        paths = [home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"]
    else:
        unit_dir = home / ".config" / "systemd" / "user"
        paths = [unit_dir / SYSTEMD_SERVICE_UNIT, unit_dir / SYSTEMD_TIMER_UNIT]
    if local:
        paths[0].parent.mkdir(parents=True)
        for path in paths:
            path.write_text("legacy unit\n")
    commands = []

    def runner(command):
        command = tuple(command)
        commands.append(command)
        if command[:2] == ("launchctl", "print"):
            stdout = "path = /foreign/autoupdate.plist\n"
        elif command[:3] == ("systemctl", "--user", "show"):
            unit = command[3]
            path = home / ".config/systemd/user" / unit
            if unit.endswith(foreign_unit):
                path = home / "foreign" / unit
            stdout = f"LoadState=loaded\nFragmentPath={path}\n"
        else:
            stdout = ""
        return CompletedProcess(command, 0, stdout, "")

    try:
        if local:
            with pytest.raises(RuntimeError, match="foreign"):
                remove_legacy_timer_units(home=home, platform=platform, runner=runner)
        else:
            assert remove_legacy_timer_units(home=home, platform=platform, runner=runner) == ()
            assert commands == []
    finally:
        assert all(path.read_text() == "legacy unit\n" for path in paths if local)
        assert all(
            c[:2] == ("launchctl", "print") or c[:3] == ("systemctl", "--user", "show")
            for c in commands
        )


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
@pytest.mark.parametrize("state", [
    "owned", "absent", "unknown", "blank-path", "probe-failed", "ambiguous-absence",
])
def test_legacy_cleanup_requires_owned_or_definitively_absent_job(tmp_path, platform, state):
    home = tmp_path.resolve()
    if platform == "launchd":
        paths = [home / "Library/LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"]
    else:
        unit_dir = home / ".config/systemd/user"
        paths = [unit_dir / SYSTEMD_SERVICE_UNIT, unit_dir / SYSTEMD_TIMER_UNIT]
    paths[0].parent.mkdir(parents=True)
    for path in paths:
        path.write_text("legacy\n")
    commands = []

    def runner(command):
        command = tuple(command)
        commands.append(command)
        probe = command[:2] == ("launchctl", "print") or "show" in command
        if not probe:
            return CompletedProcess(command, 0, "", "")
        if state in ("probe-failed", "ambiguous-absence"):
            error = "I/O error" if state == "probe-failed" else "Could not find service"
            return CompletedProcess(command, 5, "", error)
        if state == "absent":
            if platform == "launchd":
                return CompletedProcess(command, 113, "", "Could not find service")
            return CompletedProcess(command, 4, "", f"Unit {command[3]} could not be found.")
        path = paths[0] if platform == "launchd" else paths[0].parent / command[3]
        stdout = f"path = {path}\n" if platform == "launchd" else f"LoadState=loaded\nFragmentPath={path}\n"
        if state in ("unknown", "blank-path"):
            stdout = "state = running\n" if platform == "launchd" else "LoadState=loaded\n"
            if state == "blank-path":
                stdout += "path =   \n" if platform == "launchd" else "FragmentPath=   \n"
        return CompletedProcess(command, 0, stdout, "")

    if state in ("unknown", "blank-path", "probe-failed", "ambiguous-absence"):
        with pytest.raises(RuntimeError):
            remove_legacy_timer_units(home=home, platform=platform, runner=runner)
        assert all(p.read_text() == "legacy\n" for p in paths)
        assert all("print" in c or "show" in c for c in commands)
    else:
        assert remove_legacy_timer_units(home=home, platform=platform, runner=runner) == tuple(paths)
        assert all(not p.exists() for p in paths)
        if platform == "launchd":
            assert any("bootout" in c for c in commands) == (state == "owned")
        else:
            assert ("systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_UNIT) in commands
