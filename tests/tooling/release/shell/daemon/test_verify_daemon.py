"""``verify_runtime.py`` treats the daemon of its own isolated home as expected, and nothing else.

Daemon-first (KP-083): ``hyprial gui`` leaves the home's daemon running. The
verifier identifies it by the pid in the home's own record together with that
pid holding the isolated root, stops it with ``daemon stop`` for that home
through the bundled runtime, confirms it exited, and keeps the leftover check
as strict as before for every other process. The fakes are in ``fakes.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.tooling.release.shell.daemon import fakes
from tests.tooling.release.shell.support import load


@pytest.fixture()
def parent(tmp_path: Path) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="vrd-", dir="/tmp")).resolve()
    try:
        yield directory
        fakes.reap(directory, tmp_path)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _verify(tmp_path: Path, parent: Path, mode: str, **options: float) -> tuple[int, str, dict]:
    verify = load("verify_runtime")
    return verify.run(fakes.runtime(tmp_path, mode), root_parent=parent, startup_timeout=30.0, stop_timeout=20.0, keep=False, daemon_stop_timeout=options.get("daemon_stop", 10.0))


def test_the_daemon_the_gui_left_is_recognised_stopped_and_confirmed_gone(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "daemon")
    assert (code, line) == (0, "PASS"), evidence
    names = [step["name"] for step in evidence["steps"]]
    assert names == ["manifest", "isolation", "version", "gui-record", "handoff", "stdin-stop", "daemon", "children", "manifest-after"]
    step = next(item for item in evidence["steps"] if item["name"] == "daemon")
    stop = evidence["daemonStop"]
    assert step["status"] == "PASS" and f"expected daemon {stop['pid']}" in step["detail"] and "record daemon.json" in step["detail"]
    assert stop["graceful"] is True and stop["exit"] == 0 and stop["fallbackKilled"] == []
    assert stop["stdout"].count('"stateDir": "' + str(parent)) == 1 and '/s"' in stop["stdout"]
    assert evidence["daemons"]["expected"] == [stop["pid"]] and evidence["daemons"]["extra"] == [] and evidence["daemons"]["foreign"] == []
    assert not fakes.alive(stop["pid"])
    assert evidence["fallbackKilled"] == [] and evidence["leftAtExit"] == []
    assert list(parent.iterdir()) == []


def test_a_home_without_a_daemon_still_passes_and_says_so(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "none")
    assert (code, line) == (0, "PASS"), evidence
    assert next(item for item in evidence["steps"] if item["name"] == "daemon")["detail"] == "no daemon of the home remained"
    assert "daemonStop" not in evidence


def test_a_stale_record_of_a_dead_pid_is_not_a_daemon(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "stale")
    assert (code, line) == (0, "PASS"), evidence
    assert evidence["daemons"]["expected"] == [] and evidence["daemons"]["foreign"] == []


def test_a_record_that_names_a_live_process_outside_the_home_fails_and_that_process_is_not_touched(tmp_path: Path, parent: Path) -> None:
    with fakes.foreign_guard(tmp_path):
        code, line, evidence = _verify(tmp_path, parent, "foreign")
        foreign = evidence.get("daemons", {}).get("foreign", [])
        assert code == 1 and line.startswith("FAIL daemon") and "do not hold the isolated home" in line, line
        assert len(foreign) == 1 and fakes.alive(foreign[0])
        assert "daemonStop" not in evidence


def test_the_foreign_process_is_stopped_even_when_the_run_raises_before_its_pid_is_known(tmp_path: Path, parent: Path) -> None:
    fake = fakes.runtime(tmp_path, "foreign")
    environment = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    command = [str(fake / "bin" / "python3.12"), "-I", "-m", "hyprial.cli", "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"]
    gui = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(tmp_path))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="raised before the evidence"):
            with fakes.foreign_guard(tmp_path):
                assert gui.stdout is not None and gui.stdout.readline()
                pids = fakes.foreign_pids(tmp_path)
                assert len(pids) == 1 and fakes.alive(pids[0])
                raise RuntimeError("raised before the evidence")
        assert not fakes.alive(pids[0])
    finally:
        assert gui.stdin is not None
        gui.stdin.close()
        gui.wait(timeout=30)
        gui.stdout.close()
        for pid in pids:
            fakes.stop_foreign(pid)


def test_a_daemon_on_the_home_that_no_record_names_fails(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "unrecorded")
    assert code == 1 and line.startswith("FAIL daemon") and "are not the one its record names" in line, line
    assert len(evidence["daemons"]["extra"]) == 1 and evidence["daemons"]["expected"] == []
    assert "daemonStop" not in evidence
    assert evidence["leftAtExit"] == [] and evidence["fallbackKilled"] == evidence["daemons"]["extra"]


def test_a_second_daemon_for_the_same_home_fails_even_beside_the_recorded_one(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "second")
    assert code == 1 and line.startswith("FAIL daemon"), line
    assert len(evidence["daemons"]["expected"]) == 1 and len(evidence["daemons"]["extra"]) == 1
    assert set(evidence["fallbackKilled"]) >= {evidence["daemons"]["extra"][0], evidence["daemons"]["expected"][0]}
    assert evidence["leftAtExit"] == []


def test_a_graceful_stop_that_does_not_end_the_daemon_in_time_fails_and_names_the_fallback(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "stubborn", daemon_stop=1.0)
    stop = evidence["daemonStop"]
    assert code == 1 and line.startswith("FAIL daemon"), line
    assert "graceful stop failed" in line and "the fallback kill_carriers had to stop" in line and str(stop["pid"]) in line
    assert stop["graceful"] is False and stop["exit"] == 0 and stop["fallbackKilled"] == [stop["pid"]]
    assert not fakes.alive(stop["pid"]) and evidence["leftAtExit"] == []


def test_any_other_leftover_still_fails_after_the_daemon_was_stopped(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _verify(tmp_path, parent, "stray")
    assert code == 1 and line.startswith("FAIL children") and "left running" in line, line
    assert next(item for item in evidence["steps"] if item["name"] == "daemon")["status"] == "PASS"
    assert evidence["leftAtExit"] == [] and len(evidence["fallbackKilled"]) == 1


def test_the_daemon_helpers_classify_by_pid_and_record_together(tmp_path: Path) -> None:
    verify = load("verify_runtime")
    state = tmp_path / "state"
    state.mkdir()
    (state / "bootstrap.json").write_text('{"pid": 4242, "birthIdentity": "x"}\n', encoding="utf-8")
    (state / "daemon.json").write_text('{"pid": true}\n', encoding="utf-8")
    assert verify.record_pids(state) == {4242: "bootstrap.json"}
    (state / "daemon.json").write_text("not json", encoding="utf-8")
    assert verify.record_pids(state) == {4242: "bootstrap.json"}
    clean = verify.HomeDaemons(expected=[1], foreign=[], extra=[], records={1: "daemon.json"})
    assert clean.problem() is None
    assert "more than one" in verify.HomeDaemons(expected=[1, 2], foreign=[], extra=[], records={}).problem()
    assert "do not hold" in verify.HomeDaemons(expected=[], foreign=[3], extra=[], records={}).problem()
    assert "not the one its record names" in verify.HomeDaemons(expected=[1], foreign=[], extra=[9], records={}).problem()


def test_carriers_on_macos_runs_lsof_by_its_fixed_system_path_whatever_the_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    verify = load("verify_runtime")
    calls: list[list[str]] = []

    def run(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(verify.sys, "platform", "darwin")
    monkeypatch.setattr(verify.subprocess, "run", run)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert verify.carriers(tmp_path) == []
    assert [argv[0] for argv in calls] == ["/usr/sbin/lsof"], calls


def _hanging(tmp_path: Path, name: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\nexec /bin/sleep 600\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_a_ps_that_hangs_times_out_instead_of_blocking_forever(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    verify = load("verify_runtime")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _hanging(stubs, "ps")
    monkeypatch.setattr(verify, "PS_TIMEOUT", 0.3)
    monkeypatch.setenv("PATH", f"{stubs}:/usr/bin:/bin")
    for call in (verify._commands, lambda: verify._alive(os.getpid()), lambda: verify.carriers(tmp_path, command="x")):
        with pytest.raises(subprocess.TimeoutExpired):
            call()


def test_an_lsof_that_hangs_times_out_instead_of_blocking_forever(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    verify = load("verify_runtime")
    monkeypatch.setattr(verify, "LSOF", str(_hanging(tmp_path, "lsof")))
    monkeypatch.setattr(verify, "LSOF_TIMEOUT", 0.3)
    monkeypatch.setattr(verify.sys, "platform", "darwin")
    with pytest.raises(subprocess.TimeoutExpired):
        verify.carriers(tmp_path)
