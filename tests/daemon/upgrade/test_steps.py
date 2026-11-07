"""Versioned daemon upgrade steps: mcp argv, live processes, timer fence.

The registry is data, not a second daemon: every step is idempotent, writes an
atomic marker, and never blocks startup.  The timer step's whitelist is exact;
the look-alike launchd fixtures must remain byte-identical.

Decision (0.5.5.3, CI run 42756 job 104891): the double-slash home test
allocates its base from the default temp root, which exists on macOS and
Linux alike; the hard-coded macOS-only ``/private/tmp`` broke collection on
Linux runners while the tested property (a ``//``-prefixed absolute home is
legal and removed) never depended on that root.
"""

from __future__ import annotations

import json
import subprocess
import pytest
import shutil
import tempfile
from pathlib import Path

from hyprial.daemon.impl.upgrade import run_upgrade_steps, upgrade_step_status
from hyprial.daemon.impl.upgrade.steps.mcp_entry import (
    legacy_processes,
    migrate_persisted_argv,
)
from hyprial.daemon.impl.upgrade.steps.autoupdate_timer import (
    AUTOUPDATE_LABEL,
    SYSTEMD_SERVICE_UNIT,
    SYSTEMD_TIMER_UNIT,
)


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(tuple(command), returncode, stdout, stderr)


def test_mcp_step_rewrites_exact_legacy_argv_and_is_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = state / "claude-channel-session-1-abcd1234.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness-bridge": {
                        "args": [
                            "-m", "hyprial.cli", "mcp", "agent-channel",
                            "--actor", "agent:op:node:worker",
                        ]
                    }
                },
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "args": [
                                        "-m", "hyprial.cli", "mcp",
                                        "claude-channel-recover",
                                        "--signal-path", "/tmp/recover",
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    before = config.read_bytes()

    result = migrate_persisted_argv(state)

    rewritten = json.loads(config.read_text())
    assert rewritten["mcpServers"]["harness-bridge"]["args"][:3] == [
        "-m", "hyprial.daemon.impl.mcp.entry", "agent-channel",
    ]
    assert rewritten["hooks"]["SessionStart"][0]["hooks"][0]["args"][:3] == [
        "-m", "hyprial.daemon.impl.mcp.entry", "claude-channel-recover",
    ]
    assert result["rewritten"] == [str(config)]
    assert config.read_bytes() != before

    second = migrate_persisted_argv(state)
    assert second["rewritten"] == []
    assert second["failed"] == []


def test_mcp_step_skips_symlink_and_invalid_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = state / "claude-channel-real-1-aaaa.json"
    target.write_text('{"mcpServers": {}}', encoding="utf-8")
    link = state / "claude-channel-link-1-bbbb.json"
    link.symlink_to(target)
    bad = state / "claude-channel-bad-1-cccc.json"
    bad.write_text("{not json", encoding="utf-8")

    result = migrate_persisted_argv(state)

    assert result["rewritten"] == []
    assert {row["path"] for row in result["failed"]} == {str(link), str(bad)}


def test_live_process_scan_marks_relaunch_without_signalling() -> None:
    commands = []

    def runner(command, **kwargs):
        commands.append(tuple(command))
        return _completed(
            command,
            stdout=(
                "  101 python -m hyprial.cli mcp agent-channel --actor agent:op:n:a --session-ref s1\n"
                "  202 python -m hyprial.daemon.impl.mcp.entry agent-channel --actor b\n"
                "  303 python -m hyprial.cli daemon status\n"
            ),
        )

    rows = legacy_processes(process_runner=runner)

    assert rows == [
        {
            "pid": 101,
            "entry": "agent-channel",
            "actor": "agent:op:n:a",
            "sessionRef": "s1",
        }
    ]
    assert commands == [("ps", "-axo", "pid=,command=")]


def test_runner_records_done_and_retries_failed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    calls: list[str] = []

    def flaky(context):
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": True}

    # A test-only registry entry is injected through the public runner's
    # explicit steps parameter; production cannot add one this way.
    from hyprial.daemon.impl.upgrade import UpgradeStep

    step = UpgradeStep("flaky-v1", flaky)
    first = run_upgrade_steps(state, steps=(step,))
    assert first["steps"][0]["state"] == "failed"
    assert first["steps"][0]["detail"].startswith("first attempt fails")

    second = run_upgrade_steps(state, steps=(step,))
    assert second["steps"][0]["state"] == "done"
    assert second["steps"][0]["attempts"] == 2
    status = upgrade_step_status(state, steps=(step,))
    assert status["steps"][0]["state"] == "done"
    assert status["pending"] == []


def test_timer_step_fires_only_for_the_exact_autoupdate_units(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    timer = launch / f"{AUTOUPDATE_LABEL}.plist"
    timer.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.hyprial.hyprial.autoupdate</string><key>ProgramArguments</key><array><string>hyprial</string><string>daemon</string><string>upgrade</string><string>run</string></array></dict></plist>
""", encoding="utf-8")
    lookalikes = {
        "com.hyprial.daemon.plist": b"production daemon\n",
        "com.hyprial.hyprial.daemon.plist": b"tree daemon\n",
        "com.hyprial.daemon-watchdog.plist": b"watchdog\n",
        "com.hyprial.infra-ops-daemon-upgrade.plist": b"infra upgrade\n",
        "com.hyprial.infra-ops-daemon-supervise.plist": b"supervise\n",
        "com.hyprial.infra-ops-daemon-reinstall.plist": b"reinstall\n",
        "com.example.other.plist": b"third party\n",
    }
    for name, content in lookalikes.items():
        (launch / name).write_bytes(content)
    commands: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        command = tuple(command)
        commands.append(command)
        if command[:2] == ("launchctl", "print"):
            return _completed(command, 113, stderr="Could not find service")
        return _completed(command)

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=runner,
        uid=501,
    )

    assert not timer.exists()
    for name, content in lookalikes.items():
        assert (launch / name).read_bytes() == content
    assert commands == [("launchctl", "print", f"gui/501/{AUTOUPDATE_LABEL}")]
    assert result["steps"][0]["state"] == "done"


def test_timer_step_systemd_fire_and_ambiguous_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service = unit_dir / SYSTEMD_SERVICE_UNIT
    timer = unit_dir / SYSTEMD_TIMER_UNIT
    service.write_text("""[Unit]
Description=hyprial autoupdate
[Service]
Type=oneshot
ExecStart=hyprial daemon upgrade run
""", encoding="utf-8")
    timer.write_text("""[Unit]
Description=hyprial autoupdate timer
[Timer]
OnCalendar=*-*-* 03:17:00
Unit=hyprial-autoupdate.service
[Install]
WantedBy=timers.target
""", encoding="utf-8")
    daemon = unit_dir / "com.hyprial.hyprial.daemon.plist"
    daemon.write_bytes(b"daemon\n")
    commands: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        command = tuple(command)
        commands.append(command)
        if command[:3] == ("systemctl", "--user", "show"):
            return _completed(command, stdout=f"LoadState=loaded\nFragmentPath={unit_dir / command[3]}\n")
        return _completed(command)

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="systemd",
        command_runner=runner,
    )

    assert not service.exists() and not timer.exists()
    assert daemon.read_bytes() == b"daemon\n"
    assert commands == [
        ("systemctl", "--user", "show", SYSTEMD_SERVICE_UNIT,
         "--property=LoadState", "--property=FragmentPath"),
        ("systemctl", "--user", "show", SYSTEMD_TIMER_UNIT,
         "--property=LoadState", "--property=FragmentPath"),
        ("systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_UNIT),
        ("systemctl", "--user", "daemon-reload"),
    ]
    assert result["steps"][0]["state"] == "done"


def test_marker_done_still_rescans_for_new_legacy_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    run_upgrade_steps(state, only=("mcp-entry-v1",))
    late = state / "claude-channel-late-1-dddd.json"
    late.write_text(
        json.dumps({"mcpServers": {"x": {"args": ["-m", "hyprial.cli", "mcp", "agent-channel"]}}}),
        encoding="utf-8",
    )

    run_upgrade_steps(state, only=("mcp-entry-v1",))

    assert json.loads(late.read_text())["mcpServers"]["x"]["args"][:3] == [
        "-m", "hyprial.daemon.impl.mcp.entry", "agent-channel",
    ]


def test_mcp_step_leaves_non_target_arrays_alone(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = state / "claude-channel-other-1-eeee.json"
    config.write_text(
        json.dumps(
            {
                "notes": ["-m", "hyprial.cli", "mcp", "agent-channel"],
                "mcpServers": {"x": {"args": ["normal", "args"]}},
            }
        ),
        encoding="utf-8",
    )
    before = config.read_bytes()

    result = migrate_persisted_argv(state)

    assert config.read_bytes() == before
    assert result["rewritten"] == []


def test_malformed_exact_timer_is_preserved_and_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    timer = launch / f"{AUTOUPDATE_LABEL}.plist"
    timer.write_text("not a plist", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        commands.append(tuple(command))
        return _completed(command)

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=runner,
        uid=501,
    )

    assert timer.read_text() == "not a plist"
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "malformed" in result["steps"][0]["detail"]


def test_status_is_read_only_and_does_not_create_step_dir(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()

    status = upgrade_step_status(state)

    assert status["pending"]
    assert not (state / "upgrade-steps").exists()


def test_mcp_rewrite_enforces_owner_only_mode(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = state / "claude-channel-mode-1-ffff.json"
    config.write_text(
        json.dumps({"mcpServers": {"x": {"args": ["-m", "hyprial.cli", "mcp", "agent-channel"]}}}),
        encoding="utf-8",
    )
    config.chmod(0o644)

    migrate_persisted_argv(state)

    assert config.stat().st_mode & 0o777 == 0o600


_REAL_LAUNCHD = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.hyprial.hyprial.autoupdate</string>
<key>ProgramArguments</key><array><string>/usr/bin/hyprial</string><string>autoupdate</string><string>run</string><string>--json</string></array>
</dict></plist>
'''
_REAL_SERVICE = '''[Unit]
Description=Harness Bridge latest tag pull
[Service]
Type=oneshot
ExecStart=/usr/bin/hyprial autoupdate run --json
'''
_REAL_TIMER = '''[Unit]
Description=Twice-daily Harness Bridge latest tag pull
[Timer]
OnCalendar=*-*-* 03:17:00
Unit=hyprial-autoupdate.service
[Install]
WantedBy=timers.target
'''


def test_real_retired_templates_are_accepted_and_removed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    plist = launch / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        commands.append(tuple(command))
        return _completed(command, 113, stderr="Could not find service")

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=runner,
        uid=501,
    )
    assert not plist.exists()
    assert result["steps"][0]["state"] == "done"

    unit = home / ".config" / "systemd" / "user"
    unit.mkdir(parents=True)
    service = unit / SYSTEMD_SERVICE_UNIT
    timer = unit / SYSTEMD_TIMER_UNIT
    service.write_text(_REAL_SERVICE, encoding="utf-8")
    timer.write_text(_REAL_TIMER, encoding="utf-8")
    commands.clear()

    def systemd_runner(command, **kwargs):
        commands.append(tuple(command))
        if command[:3] == ("systemctl", "--user", "show"):
            return _completed(command, stdout=f"LoadState=loaded\nFragmentPath={unit / command[3]}\n")
        return _completed(command)

    result = run_upgrade_steps(
        tmp_path / "state2",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="systemd",
        command_runner=systemd_runner,
    )
    assert not service.exists() and not timer.exists()
    assert result["steps"][0]["state"] == "done"


def test_mcp_rewrite_calls_fsync_for_file_and_directory(tmp_path: Path, monkeypatch) -> None:
    from hyprial.daemon.impl.upgrade.steps import mcp_entry

    state = tmp_path / "state"
    state.mkdir()
    config = state / "claude-channel-fsync-1-aaaa.json"
    config.write_text(
        json.dumps({"mcpServers": {"x": {"args": ["-m", "hyprial.cli", "mcp", "agent-channel"]}}}),
        encoding="utf-8",
    )
    calls: list[int] = []
    real = mcp_entry.os.fsync
    monkeypatch.setattr(mcp_entry.os, "fsync", lambda fd: calls.append(fd) or real(fd))

    migrate_persisted_argv(state)

    assert len(calls) >= 2


def test_marker_write_calls_fsync_and_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    from hyprial.daemon.impl.upgrade import runner

    calls: list[int] = []
    real = runner.os.fsync
    monkeypatch.setattr(runner.os, "fsync", lambda fd: calls.append(fd) or real(fd))

    run_upgrade_steps(tmp_path / "state", only=("autoupdate-timer-v1",), home=tmp_path, platform="systemd")
    marker = tmp_path / "state" / "upgrade-steps" / "autoupdate-timer-v1.json"
    assert len(calls) >= 2
    assert marker.stat().st_mode & 0o777 == 0o600


def _foreign_launchd(argv: list[str]) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>{AUTOUPDATE_LABEL}</string>
<key>ProgramArguments</key><array>{''.join(f'<string>{item}</string>' for item in argv)}</array></dict></plist>
'''


def test_foreign_launchd_argv_at_exact_label_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    plist = launch / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(
        _foreign_launchd(["/bin/echo", "foreign", "autoupdate", "run"]),
        encoding="utf-8",
    )
    before = plist.read_bytes()
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert plist.read_bytes() == before
    assert commands == []
    assert result["steps"][0]["state"] == "failed"


def test_systemd_comment_cannot_make_a_foreign_execstart_legacy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unit = home / ".config" / "systemd" / "user"
    unit.mkdir(parents=True)
    service = unit / SYSTEMD_SERVICE_UNIT
    timer = unit / SYSTEMD_TIMER_UNIT
    service.write_text(
        "[Unit]\nDescription=foreign\n[Service]\nExecStart=/bin/echo foreign\n"
        "# mentions autoupdate run but does not execute it\n",
        encoding="utf-8",
    )
    timer.write_text(_REAL_TIMER, encoding="utf-8")
    before = service.read_bytes()
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="systemd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
    )

    assert service.read_bytes() == before
    assert timer.exists()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"


def _run_launchd_step(home: Path, state: Path, argv: list[str]):
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True, exist_ok=True)
    plist = launch / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_foreign_launchd(argv), encoding="utf-8")
    commands: list[tuple[str, ...]] = []
    result = run_upgrade_steps(
        state,
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )
    return plist, commands, result


def test_foreign_executable_and_reordered_or_extra_args_are_preserved(tmp_path: Path) -> None:
    for argv in (
        ["/bin/echo", "autoupdate", "run"],
        ["hyprial", "run", "autoupdate"],
        ["hyprial", "autoupdate", "run", "--json", "--force"],
    ):
        home = tmp_path / "home"
        state = tmp_path / "state"
        plist, commands, result = _run_launchd_step(home, state, argv)
        assert plist.exists(), argv
        assert commands == [], argv
        assert result["steps"][0]["state"] == "failed", argv


def test_systemd_multiple_execstart_lines_are_foreign(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unit = home / ".config" / "systemd" / "user"
    unit.mkdir(parents=True)
    service = unit / SYSTEMD_SERVICE_UNIT
    timer = unit / SYSTEMD_TIMER_UNIT
    service.write_text(
        "[Unit]\n[Service]\nType=oneshot\n"
        "ExecStart=/usr/bin/hyprial autoupdate run --json\n"
        "ExecStart=/bin/echo foreign\n",
        encoding="utf-8",
    )
    timer.write_text(_REAL_TIMER, encoding="utf-8")
    before = service.read_bytes()
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="systemd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
    )

    assert service.read_bytes() == before
    assert commands == []
    assert result["steps"][0]["state"] == "failed"


def test_symlink_at_the_exact_timer_path_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    target = tmp_path / "real-timer.plist"
    target.write_text(_REAL_LAUNCHD, encoding="utf-8")
    link = launch / f"{AUTOUPDATE_LABEL}.plist"
    link.symlink_to(target)
    before = target.read_bytes()
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert link.is_symlink()
    assert target.read_bytes() == before
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "symlink" in result["steps"][0]["detail"]


def test_symlinked_launchagents_parent_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = tmp_path / "real-launchagents"
    real.mkdir()
    (home / "Library").mkdir(parents=True)
    (home / "Library" / "LaunchAgents").symlink_to(real)
    plist = real / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert plist.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "ancestor" in result["steps"][0]["detail"]


def test_hardlinked_exact_timer_path_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch = home / "Library" / "LaunchAgents"
    launch.mkdir(parents=True)
    source = tmp_path / "source.plist"
    source.write_text(_REAL_LAUNCHD, encoding="utf-8")
    plist = launch / f"{AUTOUPDATE_LABEL}.plist"
    plist.hardlink_to(source)
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert plist.is_file() and source.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "link" in result["steps"][0]["detail"].lower()


def test_symlinked_systemd_user_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = tmp_path / "real-systemd-user"
    real.mkdir()
    (home / ".config" / "systemd").mkdir(parents=True)
    (home / ".config" / "systemd" / "user").symlink_to(real)
    service = real / SYSTEMD_SERVICE_UNIT
    timer = real / SYSTEMD_TIMER_UNIT
    service.write_text(_REAL_SERVICE, encoding="utf-8")
    timer.write_text(_REAL_TIMER, encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="systemd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
    )

    assert service.is_file() and timer.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "ancestor" in result["steps"][0]["detail"]


def test_symlinked_parent_above_home_is_refused(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_home = real_parent / "home"
    (real_home / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = real_home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent)
    home = alias_parent / "home"
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert plist.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "alias" in result["steps"][0]["detail"].lower() or "symlink" in result["steps"][0]["detail"].lower()


def test_symlink_dotdot_home_alias_is_refused(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    home = actual / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
    nested = actual / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(nested)
    home_alias = alias / ".." / "home"
    commands: list[tuple[str, ...]] = []

    result = run_upgrade_steps(
        tmp_path / "state",
        only=("autoupdate-timer-v1",),
        home=home_alias,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )

    assert plist.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"
    assert "alias" in result["steps"][0]["detail"].lower() or "symlink" in result["steps"][0]["detail"].lower()


def _run_home_alias_case(home: Path):
    commands: list[tuple[str, ...]] = []
    result = run_upgrade_steps(
        home.parent / "state",
        only=("autoupdate-timer-v1",),
        home=home,
        platform="launchd",
        command_runner=lambda command, **_: commands.append(tuple(command)),
        uid=501,
    )
    return commands, result


def test_tmp_user_symlink_parent_is_not_the_os_alias(tmp_path: Path) -> None:
    if not Path("/tmp").is_symlink() and Path("/tmp").resolve() == Path("/tmp"):
        return
    root = Path(tempfile.mkdtemp(prefix="hf-home-alias-", dir="/tmp"))
    try:
        real = root / "real"
        home = real / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        plist = home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
        plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
        alias = root / "alias"
        alias.symlink_to(real)
        commands, result = _run_home_alias_case(alias / "home")
        assert plist.is_file()
        assert commands == []
        assert result["steps"][0]["state"] == "failed"
    finally:
        shutil.rmtree(root)


def test_tmp_home_symlink_is_not_the_os_alias(tmp_path: Path) -> None:
    if not Path("/tmp").is_symlink() and Path("/tmp").resolve() == Path("/tmp"):
        return
    root = Path(tempfile.mkdtemp(prefix="hf-home-alias-", dir="/tmp"))
    try:
        real = root / "real"
        home = real / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        plist = home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
        plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
        link = root / "home-link"
        link.symlink_to(home)
        commands, result = _run_home_alias_case(link)
        assert plist.is_file()
        assert commands == []
        assert result["steps"][0]["state"] == "failed"
    finally:
        shutil.rmtree(root)


def test_relative_home_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    home = Path("relative-home")
    (tmp_path / "relative-home" / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = tmp_path / "relative-home" / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
    plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
    commands, result = _run_home_alias_case(home)
    assert plist.is_file()
    assert commands == []
    assert result["steps"][0]["state"] == "failed"


@pytest.mark.parametrize("root", ["/tmp", "/var/tmp"])
def test_tmp_or_var_symlink_dotdot_cancellation_is_refused(root: str, tmp_path: Path) -> None:
    if not Path(root).is_dir():
        return
    base = Path(tempfile.mkdtemp(prefix="hf-home-cancel-", dir=root))
    try:
        child = base / "child"
        (child / "Library" / "LaunchAgents").mkdir(parents=True)
        plist = child / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
        plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
        alias = base / "alias-to-child"
        alias.symlink_to(child)
        home = alias / ".."
        commands: list[tuple[str, ...]] = []
        result = run_upgrade_steps(
            tmp_path / "state",
            only=("autoupdate-timer-v1",),
            home=home,
            platform="launchd",
            command_runner=lambda command, **_: commands.append(tuple(command)),
            uid=501,
        )
        assert plist.is_file()
        assert commands == []
        assert result["steps"][0]["state"] == "failed"
    finally:
        shutil.rmtree(base)


def test_double_slash_private_temp_home_is_legal_and_removed(tmp_path: Path) -> None:
    base = Path(tempfile.mkdtemp(prefix="hf-home-double-"))
    try:
        home = base / "home"
        launch = home / "Library" / "LaunchAgents"
        launch.mkdir(parents=True)
        plist = launch / f"{AUTOUPDATE_LABEL}.plist"
        plist.write_text(_REAL_LAUNCHD, encoding="utf-8")
        plist.chmod(0o644)
        doubled = Path("//" + str(home).lstrip("/"))
        commands: list[tuple[str, ...]] = []

        result = run_upgrade_steps(
            tmp_path / "state",
            only=("autoupdate-timer-v1",),
            home=doubled,
            platform="launchd",
            command_runner=lambda command, **_: commands.append(tuple(command))
            or _completed(command, 113, stderr="Could not find service"),
            uid=501,
        )

        assert not plist.exists()
        assert commands == [("launchctl", "print", "gui/501/com.hyprial.hyprial.autoupdate")]
        assert result["steps"][0]["state"] == "done"
    finally:
        shutil.rmtree(base)
