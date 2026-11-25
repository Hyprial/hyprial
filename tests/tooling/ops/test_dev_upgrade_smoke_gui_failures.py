"""Per-failure-point behaviour of the dev-upgrade smoke GUI check.

Each test drives ``run_apply`` through the fake runner and asserts the FINAL
state (service active, installed wheel, GUI child alive), the result name and
the exit code, not the argv text.

Decisions pinned here:
- a GUI child still alive after SIGKILL is manual work: the upgrade is rolled
  back first, then the run ends with ``ROLLED_BACK_GUI_LEFT`` / exit 3 naming
  the process group and the exact kill command;
- ``gui.tunnel`` is NOT_RUN (informational) unless ``--gui-hold-minutes`` keeps
  the GUI open, in which case it is PASS;
- the systemd ``failed`` / ``MainPID=0`` stop semantics belong to the legacy
  systemctl control (a baseline CLI without ``supervision`` in its status).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .gui_support import (
    GUI_PID,
    WALL,
    Listener,
    assert_final,
    build,
    check,
    smoke,
    summary_of,
)

STDERR_LOG = f"gui-{WALL}.err.log"
RECORD_WAIT = 120


def rolled_back(instance, runner, rc) -> None:
    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_FAILED,
        result="ROLLED_BACK", active=True, version="0.1.0",
    )
    assert runner.installs == ["0.2.0", "0.1.0"]


def test_target_daemon_not_established_before_the_gui_rolls_back_without_a_child(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.status_fails_after_ready = True

    rc = instance.run_apply()

    assert check(instance, "gui.daemon-identity").status == smoke.FAIL
    assert runner.gui_spawns() == []
    rolled_back(instance, runner, rc)


def test_one_failed_daemon_status_read_before_the_gui_does_not_fail_the_check(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.status_fails_after_ready = True
    runner.status_fail_budget = 2

    rc = instance.run_apply()

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_OK,
        result="PASS", active=True, version="0.2.0",
    )
    assert runner.installs == ["0.2.0"]
    assert len(runner.gui_spawns()) == 1


def test_no_startup_record_within_the_wait_stops_the_child_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.write_record = False

    rc = instance.run_apply()

    assert check(instance, "gui.start").status == smoke.FAIL
    assert f"{RECORD_WAIT}s" in check(instance, "gui.start").detail
    assert instance.clock.slept >= RECORD_WAIT
    rolled_back(instance, runner, rc)
    assert runner.gui_children()[0].stdin_closed_at is not None


def test_a_child_that_cannot_be_started_fails_the_check_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.spawn_error = FileNotFoundError(2, "No such file or directory")

    rc = instance.run_apply()

    assert check(instance, "gui.start").status == smoke.FAIL
    assert "No such file" in check(instance, "gui.start").detail
    assert runner.gui_children() == []
    rolled_back(instance, runner, rc)


def test_a_gui_that_survives_sigkill_is_manual_work_after_the_rollback(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.stdin_stop_seconds = None
    runner.exits_on_sigterm = False
    runner.exits_on_sigkill = False

    rc = instance.finish(instance.run_apply())

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_ROLLBACK_FAILED,
        result="ROLLED_BACK_GUI_LEFT", active=True, version="0.1.0",
        gui_alive=True,
    )
    assert runner.installs == ["0.2.0", "0.1.0"]
    summary = summary_of(instance)
    assert summary["rollback"]["ok"] is True
    assert summary["rollback"]["guiPid"] == GUI_PID
    command = f"kill -KILL -- -{GUI_PID}"
    assert summary["rollback"]["guiStopCommand"] == command
    assert "guiUnit" not in summary["rollback"]
    assert command in check(instance, "gui.stop").detail
    assert check(instance, "rollback.manual").status == smoke.FAIL


def test_a_failed_rollback_after_a_gui_failure_exits_three(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_overrides = {"backend_owned": True}
    runner.fail_install_version = "0.1.0"

    rc = instance.finish(instance.run_apply())

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_ROLLBACK_FAILED,
        result="ROLLBACK_FAILED", active=False, version="0.2.0",
    )
    assert summary_of(instance)["rollback"]["ok"] is False


def test_stop_timeout_restore_that_starts_but_is_not_ready_exits_three(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port)
    runner.hang_stop_seconds = 45.0
    runner.fail_status_when_restored = True

    rc = instance.finish(instance.run_apply())

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_ROLLBACK_FAILED,
        result="FAILED_STOP_TIMEOUT_RESTORE_FAILED", active=True, version="0.1.0",
    )
    assert runner.installs == []
    assert check(instance, "restore.start").status == smoke.PASS
    assert check(instance, "daemon.ready").status == smoke.FAIL


def test_a_stale_gui_log_is_refused_before_the_child_starts(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    stale = instance.artifacts_dir / STDERR_LOG
    stale.write_text("earlier run\n")

    rc = instance.run_apply()

    assert runner.gui_spawns() == []
    assert check(instance, "gui.start").status == smoke.FAIL
    assert "already exists" in check(instance, "gui.start").detail
    assert stale.read_text() == "earlier run\n"
    rolled_back(instance, runner, rc)


def test_a_reused_artifacts_dir_gets_a_fresh_gui_log(
    tmp_path: Path, listener: Listener
) -> None:
    first, first_runner = build(tmp_path, listener.port, ["--gui", "--gui-port", "0"])
    first_runner.record_overrides = {"port": 1, "url": "http://127.0.0.1:1/"}
    assert first.run_apply() == smoke.EXIT_FAILED

    second_root = tmp_path / "again"
    second_root.mkdir()
    second, runner = build(second_root, listener.port, ["--gui"])
    second.artifacts_dir = first.artifacts_dir
    second.recorder = first.recorder
    second.clock.wall_now = WALL + 1
    rc = second.run_apply()

    assert check(second, "gui.listener").status == smoke.PASS
    assert runner.gui_spawns()[0]["stderr"] != first_runner.gui_spawns()[0]["stderr"]
    assert_final(
        second, runner, rc, expected_rc=smoke.EXIT_OK,
        result="PASS", active=True, version="0.2.0",
    )


def test_hold_keeps_the_gui_open_then_stops_it_and_keeps_the_upgrade(
    tmp_path: Path, listener: Listener, capsys: pytest.CaptureFixture[str]
) -> None:
    instance, runner = build(
        tmp_path, listener.port, ["--gui", "--ssh-host", "box", "--gui-hold-minutes", "5"]
    )
    runner.record_after = 0.0
    runner.stdin_stop_seconds = 0.0
    runner.probe_overrides = {"record_after": 0.0, "stdin_stop_seconds": 0.0}
    held: list[float] = []
    instance.clock.hooks.append(lambda s: held.append(s) if runner.gui_active else None)

    rc = instance.run_apply()

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_OK,
        result="PASS", active=True, version="0.2.0",
    )
    assert sum(held) == pytest.approx(300)
    assert len(runner.service_stops()) == 1
    assert check(instance, "gui.hold").status == smoke.PASS
    assert check(instance, "gui.tunnel").status == smoke.PASS
    assert check(instance, "gui.daemon-survives-close").status == smoke.PASS
    err = capsys.readouterr().err
    assert instance.gui["tunnelCommand"] in err
    assert "closes" in err


def test_without_a_hold_the_tunnel_is_informational_and_nothing_is_held(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui", "--ssh-host", "box"])
    runner.record_after = 0.0
    runner.stdin_stop_seconds = 0.0
    runner.probe_overrides = {"record_after": 0.0, "stdin_stop_seconds": 0.0}
    held: list[float] = []
    instance.clock.hooks.append(lambda s: held.append(s) if runner.gui_active else None)

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert held == []
    assert check(instance, "gui.tunnel").status == smoke.NOT_RUN
    assert "--gui-hold-minutes" in check(instance, "gui.tunnel").detail
    assert "tunnelCommand" not in instance.gui
    assert all(item.name != "gui.hold" for item in instance.checks)


def test_ctrl_c_during_the_hold_stops_the_gui_and_keeps_the_upgrade(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui", "--gui-hold-minutes", "30"])
    runner.stdin_stop_seconds = 0.0
    held = [0.0]

    def interrupt(seconds: float) -> None:
        if runner.gui_active:
            held[0] += seconds
            if held[0] > 60:
                raise KeyboardInterrupt

    instance.clock.hooks.append(interrupt)

    rc = instance.run_apply()

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_OK,
        result="PASS", active=True, version="0.2.0",
    )
    assert check(instance, "gui.hold").status == smoke.PASS
    assert "interrupted" in check(instance, "gui.hold").detail
    assert check(instance, "gui.daemon-survives-close").status == smoke.PASS
    assert len(runner.service_stops()) == 1


@pytest.mark.parametrize("value", ["-1", "60.5", "nan"])
def test_hold_minutes_are_bounded(value: str) -> None:
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(
            ["apply", "--new-wheel", "n", "--wheelhouse", "w", "--gui-hold-minutes", value]
        )


def test_hold_minutes_default_to_zero_and_accept_the_upper_bound() -> None:
    base = ["apply", "--new-wheel", "n", "--wheelhouse", "w"]
    assert smoke.build_parser().parse_args(base).gui_hold_minutes == 0.0
    assert smoke.build_parser().parse_args(
        [*base, "--gui-hold-minutes", "60"]
    ).gui_hold_minutes == 60.0


def test_a_hold_without_gui_is_a_usage_error_before_any_command(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui-hold-minutes", "5"])

    rc = instance.run()

    assert (rc, instance.result) == (smoke.EXIT_USAGE, "ABORTED")
    assert runner.calls == []


def test_the_gui_failure_reason_names_the_first_failing_gui_check(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_overrides = {"backend_owned": True}

    instance.run_apply()

    assert "gui.record" in instance.rollback["reason"]


def test_a_relative_home_and_state_dir_are_made_absolute_once(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    monkeypatch.chdir(tmp_path)
    args = smoke.build_parser().parse_args(
        [
            "apply", "--new-wheel", "n", "--wheelhouse", "w",
            "--home", "rel-home", "--state-dir", "rel-home/state",
            "--artifacts-dir", str(tmp_path / "other"),
        ]
    )

    again = smoke.Smoke(args, runner=runner)

    assert again.options.home == Path(os.path.abspath("rel-home"))
    assert again.options.state_dir == Path(os.path.abspath("rel-home/state"))
    again.home = again._resolve_home({})
    again.state_dir = again._resolve_state_dir({})
    assert again.live_home_variables() == {
        "HYPRIAL_HOME": str(tmp_path / "rel-home"),
        "HARNESS_STATE_DIR": str(tmp_path / "rel-home" / "state"),
    }


@pytest.mark.parametrize("stopped_state", ["inactive", "failed"])
def test_a_unit_left_failed_with_no_main_process_counts_as_stopped(
    tmp_path: Path, listener: Listener, stopped_state: str
) -> None:
    instance, runner = build(tmp_path, listener.port)
    runner.legacy = True
    runner.stopped_state = stopped_state

    rc = instance.run_apply()

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_OK,
        result="PASS", active=True, version="0.2.0",
    )
    assert runner.installs == ["0.2.0"]
    assert check(instance, "service.stop").status == smoke.PASS


def test_a_failed_unit_with_a_live_main_process_is_not_stopped(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port)
    runner.legacy = True
    runner.stopped_state = "failed-live"

    rc = instance.finish(instance.run_apply())

    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_FAILED,
        result="FAILED_STOP_TIMEOUT_RESTORED", active=True, version="0.1.0",
    )
    assert runner.installs == []
    assert check(instance, "service.stop").status == smoke.FAIL
