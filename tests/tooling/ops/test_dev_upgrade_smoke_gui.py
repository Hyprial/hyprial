"""GUI check, rollback semantics and stop-timeout behaviour of the dev-upgrade smoke.

Everything runs through the fake runner, fake GUI child and fake clock of
``gui_support``: no systemctl, systemd-run, hyprial or daemon is ever executed
and nothing sleeps.  The daemon service is driven through the hyprial seam
(``hyprial daemon stop|start``); the GUI is a child of the script started
through ``Runner.spawn``.  The listener checks use a real loopback socket owned
by the test.

Decisions pinned here:
- the GUI child gets the same home and state dir as every other hyprial command;
- a GUI check failure rolls the upgrade back (owner decision 2026-11-20);
- the GUI child is stopped on success and on failure, the daemon service never;
- a stop that times out leaves the old wheel installed and the daemon started
  again once the stop has ended (the fake ends a hung stop after 45 s, as
  systemd does after its stop timeout).
Per-failure-point final-state tests live in ``test_dev_upgrade_smoke_gui_failures``;
the child's own protocol (argv, environment, stop) in ``gui/test_gui_child``.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from .gui_support import (
    Listener,
    build,
    check,
    smoke,
    summary_of,
)


def test_healthy_gui_passes_and_the_child_is_stopped_through_stdin(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert runner.installs == ["0.2.0"]
    assert check(instance, "gui.record").status == smoke.PASS
    assert check(instance, "gui.daemon-identity").status == smoke.PASS
    assert check(instance, "gui.stop").status == smoke.PASS
    assert check(instance, "gui.daemon-survives-close").status == smoke.PASS
    [child] = runner.gui_children()
    assert child.stdin_closed_at is not None
    assert child.signals == []
    assert child.returncode == 0
    assert runner.manager_calls() == []
    assert runner.gui_active is False


def test_the_gui_child_carries_the_target_home_and_state_dir(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])

    instance.run_apply()

    [spawn] = runner.gui_spawns()
    assert spawn["env"]["HYPRIAL_HOME"] == str(instance.home)
    assert spawn["env"]["HARNESS_STATE_DIR"] == str(instance.state_dir)


def test_live_environment_and_gui_child_share_one_home_source(
    tmp_path: Path, listener: Listener
) -> None:
    instance, _ = build(tmp_path, listener.port, ["--gui"])

    env = instance.live_environment()

    assert env["HYPRIAL_HOME"] == str(instance.home)
    assert env["HARNESS_STATE_DIR"] == str(instance.state_dir)
    assert instance.live_home_variables() == {
        "HYPRIAL_HOME": str(instance.home),
        "HARNESS_STATE_DIR": str(instance.state_dir),
    }


def test_tunnel_and_listener_use_the_record_port_when_gui_port_is_zero(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(
        tmp_path, listener.port, ["--gui", "--gui-port", "0", "--ssh-host", "box", "--gui-hold-minutes", "1"]
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    [command] = runner.gui_calls()
    assert command[command.index("--port") + 1] == "0"
    tunnel = instance.gui["tunnelCommand"]
    assert f"-L {listener.port}:127.0.0.1:{listener.port} " in tunnel
    assert ":0:" not in tunnel and " 0:" not in tunnel
    assert f"127.0.0.1:{listener.port}" in check(instance, "gui.listener").detail
    assert check(instance, "gui.listener").status == smoke.PASS


INVALID_RECORDS = {
    "record-version-2": {"record_version": 2},
    "record-version-missing": {"record_version": None},
    "ok-false": {"ok": False},
    "driver-mock": {"backend_driver": "contract-mock"},
    "backend-pid-set": {"backend_pid": 123},
    "backend-owned": {"backend_owned": True},
    "port-string": {"port": "4173"},
    "port-bool": {"port": True},
    "port-zero": {"port": 0},
    "port-too-large": {"port": 70000},
    "handoff-empty": {"handoff_path": ""},
    "handoff-null": {"handoff_path": None},
    "url-other-port": {"url": "http://127.0.0.1:1/"},
    "url-other-host": {"url": "http://[::1]:{port}/"},
    "host-not-loopback": {"host": "10.0.0.5"},
}


@pytest.mark.parametrize("name", sorted(INVALID_RECORDS))
def test_each_invalid_startup_record_fails_the_gui_check_and_rolls_back(
    tmp_path: Path, listener: Listener, name: str
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_overrides = {
        key: value.format(port=listener.port) if isinstance(value, str) else value
        for key, value in INVALID_RECORDS[name].items()
    }

    rc = instance.run_apply()

    assert check(instance, "gui.record").status == smoke.FAIL
    assert len(check(instance, "gui.record").detail) > 10
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert runner.installs == ["0.2.0", "0.1.0"]
    assert runner.gui_children()[0].stdin_closed_at is not None
    assert runner.gui_active is False


@pytest.mark.parametrize(
    "key", ["handoff_path", "mode", "frontend_pid", "assets_pid", "backend_pid", "url"]
)
def test_a_record_missing_a_required_key_fails_the_gui_check(
    tmp_path: Path, listener: Listener, key: str
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.drop_keys = [key]

    rc = instance.run_apply()

    assert check(instance, "gui.record").status == smoke.FAIL
    assert key in check(instance, "gui.record").detail
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"


def test_a_record_port_other_than_a_fixed_gui_port_fails_the_gui_check(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(
        tmp_path, listener.port, ["--gui", "--gui-port", str(listener.port + 1)]
    )

    rc = instance.run_apply()

    assert check(instance, "gui.record").status == smoke.FAIL
    assert "--gui-port" in check(instance, "gui.record").detail
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"


def test_a_failed_listener_rolls_back_to_the_old_wheel(
    tmp_path: Path, listener: Listener
) -> None:
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    dead_port = unused.getsockname()[1]
    unused.close()
    instance, runner = build(tmp_path, listener.port, ["--gui", "--gui-port", "0"])
    runner.record_overrides = {
        "port": dead_port,
        "url": f"http://127.0.0.1:{dead_port}/",
    }

    rc = instance.run_apply()

    assert check(instance, "gui.listener").status == smoke.FAIL
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert runner.installs == ["0.2.0", "0.1.0"]
    assert instance.rollback["ok"] is True
    assert "GUI" in instance.rollback["reason"]


def test_a_gui_failure_is_reported_as_rolled_back_in_the_summary(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_overrides = {"backend_owned": True}

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    summary = summary_of(instance)
    assert summary["result"] == "ROLLED_BACK"
    assert summary["rollback"]["ok"] is True
    assert "GUI" in summary["rollback"]["reason"]
    assert summary["gui"]["pid"] == runner.gui_children()[0].pid
    assert summary["gui"]["exitCode"] == 0
    assert "unit" not in summary["gui"]
    assert runner.active is True
    assert runner.installed_version == "0.1.0"


def test_gui_start_that_changes_the_target_daemon_fails_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.move_on_gui = True

    rc = instance.run_apply()

    assert check(instance, "gui.daemon-identity").status == smoke.FAIL
    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert runner.gui_children()[0].stdin_closed_at is not None


def test_closing_the_gui_never_stops_the_daemon_service(
    tmp_path: Path, listener: Listener
) -> None:
    ok_instance, ok_runner = build(tmp_path, listener.port, ["--gui"])
    ok_instance.run_apply()
    assert len(ok_runner.service_stops()) == 1
    assert ok_runner.active is True

    sub = tmp_path / "second"
    sub.mkdir()
    bad_instance, bad_runner = build(sub, listener.port, ["--gui"])
    bad_runner.record_overrides = {"backend_pid": 7}
    bad_instance.run_apply()
    assert len(bad_runner.service_stops()) == 2
    assert bad_runner.active is True
    closed_at = bad_runner.gui_children()[0].stdin_closed_after_calls
    stops_after_gui = [
        i
        for i, c in enumerate(bad_runner.calls)
        if i >= closed_at and c[1:4] == ["daemon", "stop", "--json"]
    ]
    assert len(stops_after_gui) == 1


def test_stop_timeout_defaults_to_180_seconds() -> None:
    args = smoke.build_parser().parse_args(
        ["apply", "--new-wheel", "n.whl", "--wheelhouse", "w"]
    )
    assert args.stop_timeout == 180.0


def test_stop_timeout_restarts_the_service_on_the_old_wheel(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port)
    runner.hang_stop_seconds = 45.0

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_FAILED
    assert runner.installs == []
    assert runner.installed_version == "0.1.0"
    assert runner.active is True
    assert len(runner.service_starts()) == 1
    assert check(instance, "service.stop").status == smoke.FAIL
    assert check(instance, "restore.start").status == smoke.PASS
    summary = summary_of(instance)
    assert summary["result"] == "FAILED_STOP_TIMEOUT_RESTORED"
    assert summary["rollback"]["ok"] is True
    assert summary["rollback"]["mutated"] is False


def test_stop_timeout_with_a_failed_restart_is_reported_as_manual_work(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port)
    runner.hang_stop_seconds = 45.0
    runner.start_fails = True

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_ROLLBACK_FAILED
    assert runner.installs == []
    summary = summary_of(instance)
    assert summary["result"] == "FAILED_STOP_TIMEOUT_RESTORE_FAILED"
    assert summary["rollback"]["ok"] is False
