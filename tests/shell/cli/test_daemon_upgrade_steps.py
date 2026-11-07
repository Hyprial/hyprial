"""The daemon upgrade group absorbs the old top-level upgrade surface."""

from __future__ import annotations

from typer.main import get_command
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_old_top_level_upgrade_autoupdate_and_mcp_are_gone() -> None:
    root = get_command(_cli_root_app)
    assert "upgrade" not in root.commands
    assert "autoupdate" not in root.commands
    assert "mcp" not in root.commands
    daemon = root.commands["daemon"]
    assert hasattr(daemon, "commands")
    assert "upgrade" in daemon.commands
    upgrade = daemon.commands["upgrade"]
    assert hasattr(upgrade, "commands")
    assert {"status", "apply", "run"} <= set(upgrade.commands)
    assert upgrade.commands["run"].hidden is True


def test_doctor_exposes_fix_and_daemon_upgrade_is_the_only_executor() -> None:
    result = CliRunner().invoke(_cli_root_app, ["doctor", "--help"])
    assert result.exit_code == 0, result.output
    assert "--fix" in result.output
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--help"])
    assert result.exit_code == 0, result.output
    assert "status" in result.output
    assert "apply" in result.output


def test_autoupdate_scheduler_calls_daemon_upgrade_run(monkeypatch, tmp_path) -> None:
    from hyprial.daemon.impl.autoupdate import InProcessAutoUpdateScheduler

    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(list(command))
        class Result:
            returncode = 0
        return Result()

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path,
        runner=runner,
        hyprial_home=tmp_path,
    )
    scheduler._execute("test")  # noqa: SLF001 - pins the child argv
    assert commands[0][2:5] == ["hyprial.cli", "daemon", "upgrade"]
    assert commands[0][5] == "run"


def test_doctor_reports_pending_steps_and_fix_runs_them(tmp_path, monkeypatch) -> None:
    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as doctor_upgrade_steps

    class Services:
        def _state_dir(self):
            return tmp_path / "state"

        def _hyprial_home(self):
            return tmp_path / "home"

    (tmp_path / "home").mkdir()
    monkeypatch.setattr(doctor_upgrade_steps, "get_services", lambda: Services())
    monkeypatch.setattr("hyprial.daemon.impl.upgrade.steps.mcp_entry.legacy_processes", lambda **_: [])

    pending = doctor_upgrade_steps._upgrade_steps_doctor_check(fix=False)
    assert pending["name"] == "upgrade-steps"
    assert pending["status"] == "warn"
    assert "mcp-entry-v1" in pending["detail"]

    fixed = doctor_upgrade_steps._upgrade_steps_doctor_check(fix=True)
    assert fixed["status"] == "ok"
    assert all(step["state"] == "done" for step in fixed["steps"])


def test_doctor_warns_and_marks_relaunch_for_live_legacy_process(tmp_path, monkeypatch) -> None:
    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as doctor_upgrade_steps

    class Services:
        def _state_dir(self):
            return tmp_path / "state"

        def _hyprial_home(self):
            return tmp_path / "home"

    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(doctor_upgrade_steps, "get_services", lambda: Services())
    monkeypatch.setattr(
        "hyprial.daemon.legacy_processes",
        lambda **_: [{"pid": 123, "entry": "agent-channel", "actor": "a"}],
    )

    check = doctor_upgrade_steps._upgrade_steps_doctor_check(fix=False)

    assert check["status"] == "warn"
    assert check["relaunchRequired"] is True
    assert check["legacyArgvProcesses"][0]["pid"] == 123


def test_doctor_detects_late_legacy_file_without_writing(tmp_path, monkeypatch) -> None:
    import json

    from hyprial.shell.impl.cli.commands.doctor import upgrade_steps as doctor_upgrade_steps

    class Services:
        def _state_dir(self):
            return tmp_path / "state"

        def _hyprial_home(self):
            return tmp_path / "home"

    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    late = tmp_path / "state" / "claude-channel-late-1-aaaa.json"
    late.write_text(
        json.dumps({"mcpServers": {"x": {"args": ["-m", "hyprial.cli", "mcp", "agent-channel"]}}}),
        encoding="utf-8",
    )
    before = late.read_bytes()
    monkeypatch.setattr(doctor_upgrade_steps, "get_services", lambda: Services())
    monkeypatch.setattr("hyprial.daemon.impl.upgrade.steps.mcp_entry.legacy_processes", lambda **_: [])

    check = doctor_upgrade_steps._upgrade_steps_doctor_check(fix=False)

    assert check["status"] == "warn"
    assert check["legacyArgvFiles"] == [str(late)]
    assert late.read_bytes() == before
