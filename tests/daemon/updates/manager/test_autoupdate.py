"""Daemon autoupdate persistence and one-way legacy timer migration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import hyprial.daemon.impl.autoupdate as autoupdate
from hyprial.daemon.impl.autoupdate import (
    AUTOUPDATE_LABEL,
    LAST_RUN_FILENAME,
    SYSTEMD_SERVICE_UNIT,
    SYSTEMD_TIMER_UNIT,
    read_last_run,
    write_last_run,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


def completed(
    command: tuple[str, ...], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_last_run_roundtrip_and_visible_damage(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    assert read_last_run(state_dir) is None

    record = {"ok": True, "at": "2026-08-13T00:00:00+00:00", "upgraded": False}
    written = write_last_run(state_dir, record)
    assert written.name == LAST_RUN_FILENAME
    assert read_last_run(state_dir) == record

    written.write_text("{corrupt\n", encoding="utf-8")
    damaged = read_last_run(state_dir)
    assert isinstance(damaged, dict) and damaged["ok"] is False
    assert "not valid JSON" in damaged["error"]

    written.write_text('["not", "an", "object"]', encoding="utf-8")
    damaged = read_last_run(state_dir)
    assert isinstance(damaged, dict) and damaged["ok"] is False
    assert "not an object" in damaged["error"]


def test_startup_migration_removes_legacy_systemd_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service = unit_dir / SYSTEMD_SERVICE_UNIT
    timer = unit_dir / SYSTEMD_TIMER_UNIT
    service.write_text("legacy service\n", encoding="utf-8")
    timer.write_text("legacy timer\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        commands.append(command)
        if command[:3] == ("systemctl", "--user", "show"):
            return completed(command, stdout=f"LoadState=loaded\nFragmentPath={unit_dir / command[3]}\n")
        return completed(command)

    removed = autoupdate.remove_legacy_timer_units(
        home=tmp_path,
        platform="systemd",
        runner=runner,
    )

    assert removed == (service, timer)
    assert not service.exists() and not timer.exists()
    assert commands == [
        ("systemctl", "--user", "show", SYSTEMD_SERVICE_UNIT,
         "--property=LoadState", "--property=FragmentPath"),
        ("systemctl", "--user", "show", SYSTEMD_TIMER_UNIT,
         "--property=LoadState", "--property=FragmentPath"),
        ("systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_UNIT),
        ("systemctl", "--user", "daemon-reload"),
    ]


def test_startup_migration_removes_unloaded_launchd_unit(tmp_path: Path) -> None:
    plist = tmp_path / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("legacy plist\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        commands.append(command)
        return completed(command, 113, stderr="Could not find service")

    removed = autoupdate.remove_legacy_timer_units(
        home=tmp_path,
        platform="launchd",
        runner=runner,
        uid=501,
    )

    assert removed == (plist,)
    assert not plist.exists()
    assert commands == [
        ("launchctl", "print", f"gui/501/{AUTOUPDATE_LABEL}"),
    ]


def test_startup_migration_failure_is_visible_and_preserves_units(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service = unit_dir / SYSTEMD_SERVICE_UNIT
    timer = unit_dir / SYSTEMD_TIMER_UNIT
    service.write_text("legacy service\n", encoding="utf-8")
    timer.write_text("legacy timer\n", encoding="utf-8")

    def runner(command):  # type: ignore[no-untyped-def]
        command = tuple(command)
        return completed(command, 1, stderr="user bus unavailable")

    with pytest.raises(RuntimeError, match="user bus unavailable"):
        autoupdate.remove_legacy_timer_units(
            home=tmp_path,
            platform="systemd",
            runner=runner,
        )

    assert service.is_file() and timer.is_file()


def test_daemon_startup_reports_pending_steps_without_running_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    own_daemon_application,
) -> None:
    service_home = tmp_path / "os-home"
    plist = (
        service_home
        / "Library"
        / "LaunchAgents"
        / f"{AUTOUPDATE_LABEL}.plist"
    )
    plist.parent.mkdir(parents=True)
    plist.write_text("legacy plist\n", encoding="utf-8")

    def runner(command):  # type: ignore[no-untyped-def]
        raise AssertionError("daemon startup must not run migration commands")

    monkeypatch.setattr(autoupdate, "_run_legacy_timer_command", runner)
    monkeypatch.setattr(autoupdate.sys, "platform", "darwin")
    hyprial_home = tmp_path / "hyprial-home"
    hyprial_home.mkdir()
    state_dir = tmp_path / "state"

    app = own_daemon_application(
        DaemonApplication(
            state_dir=state_dir,
            socket_path=state_dir / "daemon.sock",
            node_id="legacy-timer-pending-test",
            hyprial_home=hyprial_home,
            owner="tester",
            legacy_timer_home=service_home,
            **daemon_dependencies(),
        )
    )

    assert plist.is_file()
    app._migrate_logs_at_startup()
    status = app._logger.writer_status()
    assert status is not None
    assert app._logger.wait_for_writes(
        target=status.accepted,
        rejected_at_capture=status.rejected,
    )
    records = [
        json.loads(line)
        for line in app._logger.path.read_text(encoding="utf-8").splitlines()
    ]
    pending = [row for row in records if row["event"] == "upgrade.steps.pending"]
    assert len(pending) == 1
    assert "mcp-entry-v1" in pending[0]["steps"]
    assert "hyprial daemon upgrade" in pending[0]["detail"]
