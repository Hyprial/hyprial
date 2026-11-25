"""The GUI check's child process: argv, environment, record, stop and handoff.

Plan section 4.4 and PR-3 (owner answers Q1 (a), Q2, Q9, 2026-11-20; decision
of 2026-11-21 point 3).  ``hyprial gui`` runs as a child of the script through
``Runner.spawn`` with ``--stdin-stop --no-open --json``; no ``systemd-run`` and no
transient unit.  Everything runs through the fake runner, the fake child and
the fake clock of ``gui_support``; the real ``ChildProcess`` handle is tested
over an ``os.pipe`` with a stub process object, never a real subprocess.

Decisions pinned here:
- the child environment is the live environment without any ``HYPRIAL_GUI_*``
  variable, ``HYPRIAL_BACKEND_ENDPOINT`` or ``HYPRIAL_DEV_GUI_HOME``; the script
  owns ``HYPRIAL_GUI_LAUNCH_TTL_MS``: absent without a hold (product default),
  ``min(hold, 10 min)`` but at least 60 s with one; ``--gui-ttl-minutes`` is gone;
- a clean stop is "stdin closed, exit 0"; a child that needs SIGTERM or SIGKILL
  fails ``gui.stop`` (the upgrade is rolled back), a child already gone is never
  signalled;
- a ``GUI_RUNTIME_BUSY`` record fails ``gui.start`` with the home and the runtime
  named, and the upgrade is rolled back (the fallback of the 2026-11-21
  decision; no preflight probe exists, see the report of this PR);
- "exit 0 with no line" is a stop before the record, a failure here;
- the handoff file is only ``lstat``-ed: a regular file, owned by the caller,
  mode 0600; it is never opened.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import signal
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ..gui_support import GUI_PID, Listener, assert_final, build, check, smoke, summary_of

BUSY_ERROR = "another foreground GUI already owns this runtime"


def rolled_back(instance: Any, runner: Any, rc: int) -> None:
    assert_final(
        instance, runner, rc, expected_rc=smoke.EXIT_FAILED,
        result="ROLLED_BACK", active=True, version="0.1.0",
    )
    assert runner.installs == ["0.2.0", "0.1.0"]


def test_the_child_runs_the_live_cli_with_stdin_stop_and_no_service_manager(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])

    assert instance.run_apply() == smoke.EXIT_OK

    [command] = runner.gui_calls()
    assert command[:2] == [str(instance.live_cli), "gui"]
    for flag in ("--stdin-stop", "--no-open", "--json"):
        assert command.count(flag) == 1
    assert command[command.index("--port") + 1] == str(listener.port)
    assert runner.manager_calls() == []
    assert instance.gui["command"] == command


def test_the_child_environment_drops_inherited_gui_variables(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = {
        "HYPRIAL_GUI_UPSTREAM_TOKEN": "bearer",
        "HYPRIAL_GUI_HOME": "/elsewhere/gui",
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HYPRIAL_GUI_LAUNCH_TTL_MS": "86400000",
        "HYPRIAL_GUI_PRINCIPAL": "user:other",
        "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:1",
        "HYPRIAL_DEV_GUI_HOME": "/elsewhere/dev",
        "HYPRIAL_OWNER": "other",
        "HARNESS_SOCKET_PATH": "/elsewhere/daemon.sock",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    instance, runner = build(tmp_path, listener.port, ["--gui"])

    assert instance.run_apply() == smoke.EXIT_OK

    env = runner.gui_spawns()[0]["env"]
    assert sorted(set(inherited) & set(env)) == []
    assert env["PATH"].split(os.pathsep)[0] == str(instance.bin_dir)
    assert env["HYPRIAL_HOME"] == str(instance.home)


@pytest.mark.parametrize(
    ("hold", "expected_ms"),
    [("0.5", 60_000), ("1", 60_000), ("5", 300_000), ("10", 600_000), ("60", 600_000)],
)
def test_with_a_hold_the_grant_lasts_the_hold_capped_at_ten_minutes(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch,
    hold: str, expected_ms: int,
) -> None:
    monkeypatch.setenv("HYPRIAL_GUI_LAUNCH_TTL_MS", "86400000")
    instance, runner = build(tmp_path, listener.port, ["--gui", "--gui-hold-minutes", hold])

    assert instance.run_apply() == smoke.EXIT_OK

    assert runner.gui_spawns()[0]["env"]["HYPRIAL_GUI_LAUNCH_TTL_MS"] == str(expected_ms)
    assert instance.gui["launchTtlMs"] == expected_ms


def test_without_a_hold_the_product_default_grant_is_used(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])

    assert instance.run_apply() == smoke.EXIT_OK

    assert "HYPRIAL_GUI_LAUNCH_TTL_MS" not in runner.gui_spawns()[0]["env"]
    assert instance.gui["launchTtlMs"] is None


def test_the_grant_ttl_flag_no_longer_exists() -> None:
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(
            ["apply", "--new-wheel", "n", "--wheelhouse", "w", "--gui-ttl-minutes", "5"]
        )


def test_exit_zero_without_a_record_is_a_failed_gui_start(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.write_record = False
    runner.exit_after_record = 0.0
    runner.exit_after_record_code = 0

    rc = instance.run_apply()

    assert check(instance, "gui.start").status == smoke.FAIL
    assert "before" in check(instance, "gui.start").detail
    assert "exit 0" in check(instance, "gui.start").detail
    assert runner.gui_children()[0].signals == []
    rolled_back(instance, runner, rc)


def test_a_child_that_fails_without_a_record_names_its_exit_code_and_log(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.write_record = False
    runner.exit_after_record = 0.0
    runner.exit_after_record_code = 2

    rc = instance.run_apply()

    detail = check(instance, "gui.start").detail
    assert check(instance, "gui.start").status == smoke.FAIL
    assert "rc=2" in detail
    assert str(runner.gui_spawns()[0]["stderr"]) in detail
    rolled_back(instance, runner, rc)


def test_a_first_line_that_is_not_json_fails_the_gui_start(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.raw_record = "Traceback (most recent call last):"

    rc = instance.run_apply()

    assert check(instance, "gui.start").status == smoke.FAIL
    assert "not a JSON object" in check(instance, "gui.start").detail
    rolled_back(instance, runner, rc)


def test_a_gui_already_holding_the_runtime_fails_the_check_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runtime = str(instance.state_dir / "gui" / "daemon")
    runner.raw_record = json.dumps(
        {"ok": False, "code": "GUI_RUNTIME_BUSY", "error": BUSY_ERROR,
         "data": {"runtime": runtime}}
    )
    runner.exit_after_record = 0.1
    runner.exit_after_record_code = 1

    rc = instance.finish(instance.run_apply())

    start = check(instance, "gui.start")
    assert start.status == smoke.FAIL
    assert "GUI_RUNTIME_BUSY" in start.detail
    assert runtime in start.detail
    assert str(instance.home) in start.detail
    assert "--gui" in start.detail
    assert check(instance, "gui.stop").status == smoke.PASS
    assert runner.gui_children()[0].signals == []
    rolled_back(instance, runner, rc)
    summary = summary_of(instance)
    assert summary["result"] == "ROLLED_BACK"
    assert "gui.start" in summary["rollback"]["reason"]
    assert summary["gui"]["exitCode"] == 1


def test_a_child_that_ignores_stdin_is_terminated_and_fails_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.stdin_stop_seconds = None

    rc = instance.run_apply()

    assert runner.gui_children()[0].signals == [signal.SIGTERM]
    assert check(instance, "gui.stop").status == smoke.FAIL
    assert "SIGTERM" in check(instance, "gui.stop").detail
    assert all(item.name != "gui.daemon-survives-close" for item in instance.checks)
    rolled_back(instance, runner, rc)


def test_a_child_that_ignores_sigterm_is_killed_and_fails_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.stdin_stop_seconds = None
    runner.exits_on_sigterm = False

    rc = instance.run_apply()

    assert runner.gui_children()[0].signals == [signal.SIGTERM, signal.SIGKILL]
    assert check(instance, "gui.stop").status == smoke.FAIL
    assert "SIGKILL" in check(instance, "gui.stop").detail
    rolled_back(instance, runner, rc)


def test_a_non_zero_exit_after_stdin_closes_fails_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.stdin_stop_code = 143

    rc = instance.run_apply()

    assert runner.gui_children()[0].signals == []
    assert check(instance, "gui.stop").status == smoke.FAIL
    assert "rc=143" in check(instance, "gui.stop").detail
    rolled_back(instance, runner, rc)


def test_a_gui_that_exits_during_the_hold_ends_it_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui", "--gui-hold-minutes", "5"])
    runner.exit_after_record = 30.0
    runner.exit_after_record_code = 1

    rc = instance.run_apply()

    assert instance.clock.slept < 300
    assert runner.gui_children()[0].signals == []
    assert check(instance, "gui.hold").status == smoke.FAIL
    assert "by itself" in check(instance, "gui.hold").detail
    assert check(instance, "gui.stop").status == smoke.PASS
    rolled_back(instance, runner, rc)


def test_ctrl_c_while_waiting_for_the_record_still_closes_the_childs_stdin(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_after = 60.0
    fired = []

    def interrupt(seconds: float) -> None:
        if runner.gui_children() and not fired:
            fired.append(seconds)
            raise KeyboardInterrupt

    instance.clock.hooks.append(interrupt)

    with pytest.raises(KeyboardInterrupt):
        instance.run_apply()

    assert runner.gui_children()[0].stdin_closed_at is not None
    assert runner.gui_active is False


def test_the_handoff_file_is_never_opened(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    handoff = str(runner.handoff)
    opened: list[str] = []

    def guard(original: Any) -> Any:
        def wrapper(target: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(target, (str, os.PathLike)) and os.fspath(target) == handoff:
                opened.append(handoff)
                raise AssertionError("the handoff file was opened")
            return original(target, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(os, "open", guard(os.open))
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))

    rc = instance.run_apply()

    assert opened == []
    assert rc == smoke.EXIT_OK
    assert check(instance, "gui.handoff-mode").status == smoke.PASS
    assert check(instance, "gui.grant").status == smoke.PASS


def test_a_handoff_file_readable_by_others_fails_the_check(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.handoff.chmod(0o644)

    rc = instance.run_apply()

    assert check(instance, "gui.handoff-mode").status == smoke.FAIL
    rolled_back(instance, runner, rc)


def test_a_handoff_symlink_fails_the_check(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    target = tmp_path / "real-handoff"
    runner.handoff.rename(target)
    runner.handoff.symlink_to(target)

    rc = instance.run_apply()

    assert check(instance, "gui.handoff-mode").status == smoke.FAIL
    assert "regular" in check(instance, "gui.handoff-mode").detail
    rolled_back(instance, runner, rc)


def test_a_handoff_file_owned_by_another_user_fails_the_check(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)

    rc = instance.run_apply()

    assert check(instance, "gui.handoff-mode").status == smoke.FAIL
    assert "owner" in check(instance, "gui.handoff-mode").detail
    rolled_back(instance, runner, rc)


class StubProcess:
    def __init__(self, read_fd: int) -> None:
        self.pid = 31337
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = io.BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_the_real_child_handle_reads_whole_lines_without_blocking() -> None:
    read_fd, write_fd = os.pipe()
    process = StubProcess(read_fd)
    child = smoke.ChildProcess(process)
    try:
        assert child.read_line() is None
        os.write(write_fd, b'{"ok":')
        assert child.read_line() is None
        os.write(write_fd, b' true}\n{"ok": false')
        assert child.read_line() == '{"ok": true}\n'
        assert child.read_line() is None
        os.close(write_fd)
        assert child.read_line() == '{"ok": false'
        assert child.read_line() is None
    finally:
        child.close()


def test_the_real_child_handle_never_signals_an_exited_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    process = StubProcess(read_fd)
    child = smoke.ChildProcess(process)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, number: sent.append((pgid, number)))
    try:
        assert child.signal_group(signal.SIGTERM) is True
        process.returncode = 0
        assert child.signal_group(signal.SIGKILL) is False
        child.close_stdin()
        child.close_stdin()
        assert process.stdin.closed
    finally:
        child.close()

    assert sent == [(31337, signal.SIGTERM)]


def test_runner_spawn_starts_its_own_session_with_pipes_and_a_stderr_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    def recording_popen(argv: Any, **kwargs: Any) -> StubProcess:
        calls.append({"argv": argv, **kwargs})
        return StubProcess(read_fd)

    monkeypatch.setattr(smoke.common.subprocess, "Popen", recording_popen)
    stderr_log = tmp_path / "gui.err.log"

    child = smoke.Runner().spawn(
        ["/bin/hyprial", "gui", "--stdin-stop"], env={"A": "1"}, stderr_path=stderr_log
    )
    try:
        [call] = calls
        assert call["argv"] == ["/bin/hyprial", "gui", "--stdin-stop"]
        assert call["stdin"] == subprocess.PIPE
        assert call["stdout"] == subprocess.PIPE
        assert call["stderr"].name == str(stderr_log)
        assert call["start_new_session"] is True
        assert call["env"] == {"A": "1"}
        assert stderr_log.exists()
        assert isinstance(child, smoke.ChildProcess)
        assert child.pid == 31337
    finally:
        child.close()


def test_a_relative_handoff_path_fails_the_record_check_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.record_overrides = {"handoff_path": "relative/handoff"}

    rc = instance.run_apply()

    assert check(instance, "gui.record").status == smoke.FAIL
    assert "absolute" in check(instance, "gui.record").detail
    rolled_back(instance, runner, rc)


@pytest.mark.parametrize("mode_bits", [0o1600, 0o4600])
def test_a_handoff_file_with_special_mode_bits_fails_the_check(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch, mode_bits: int
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    handoff_str = str(runner.handoff)
    real_lstat = Path.lstat
    uid = os.getuid()

    def fake_lstat(path_obj: Path) -> os.stat_result:
        if str(path_obj) == handoff_str:
            return os.stat_result((stat.S_IFREG | mode_bits, 1, 1, 1, uid, 0, 7, 0, 0, 0))
        return real_lstat(path_obj)

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    rc = instance.run_apply()

    assert check(instance, "gui.handoff-mode").status == smoke.FAIL
    assert oct(mode_bits) in check(instance, "gui.handoff-mode").detail
    rolled_back(instance, runner, rc)


def test_group_alive_and_kill_residual_group_check_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    process = StubProcess(read_fd)
    child = smoke.ChildProcess(process)
    behavior: dict[int, Any] = {0: None, signal.SIGTERM: None}

    def fake_killpg(pgid: int, sig: int) -> None:
        outcome = behavior[sig]
        if isinstance(outcome, BaseException):
            raise outcome

    monkeypatch.setattr(os, "killpg", fake_killpg)
    try:
        assert child.group_alive() is True
        behavior[0] = PermissionError("denied")
        assert child.group_alive() is True
        behavior[0] = ProcessLookupError("gone")
        assert child.group_alive() is False

        assert child.kill_residual_group(signal.SIGTERM) is True
        behavior[signal.SIGTERM] = ProcessLookupError("gone")
        assert child.kill_residual_group(signal.SIGTERM) is False
        behavior[signal.SIGTERM] = PermissionError("denied")
        assert child.kill_residual_group(signal.SIGTERM) is False
    finally:
        child.close()


def test_residual_process_group_killed_by_sigterm_fails_gui_stop_and_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.group_survives_root = True
    runner.group_exits_on_sigterm = True

    rc = instance.run_apply()

    assert check(instance, "gui.stop").status == smoke.FAIL
    assert "SIGTERM (residual group)" in check(instance, "gui.stop").detail
    assert runner.gui_children()[0].signals == [signal.SIGTERM]
    rolled_back(instance, runner, rc)
    assert runner.gui_active is False


def test_residual_process_group_surviving_sigkill_ends_as_gui_left(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.group_survives_root = True
    runner.group_exits_on_sigterm = False
    runner.group_exits_on_sigkill = False

    rc = instance.finish(instance.run_apply())

    assert_final(
        instance,
        runner,
        rc,
        expected_rc=smoke.EXIT_ROLLBACK_FAILED,
        result="ROLLED_BACK_GUI_LEFT",
        active=True,
        version="0.1.0",
        gui_alive=True,
    )
    assert runner.gui_children()[0].signals == [signal.SIGTERM, signal.SIGKILL]
    assert check(instance, "gui.stop").status == smoke.FAIL
    assert f"kill -KILL -- -{GUI_PID}" in check(instance, "gui.stop").detail
