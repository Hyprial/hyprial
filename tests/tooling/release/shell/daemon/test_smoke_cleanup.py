"""``smoke_bundle.py`` cleanup when ``carriers`` fails: what the run can name is still stopped and the failure is reported.

``carriers`` (``lsof`` on macOS) raising during the cleanup used to skip
``kill_carriers`` and let the exception escape, leaving the isolated home's
daemon running. The processes started here are tracked with the birth-checked
ledger, never by pid alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.process_table import Ledger
from tests.tooling.release.shell.daemon import fakes
from tests.tooling.release.shell.support import load

SLEEPER = "import time\nwhile True:\n    time.sleep(1)\n"
IGNORING = "import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\nwhile True:\n    time.sleep(1)\n"
HANGING_PS = '#!/bin/sh\nif [ -e "{marker}" ]; then exec /bin/sleep 600; fi\nexec "{real}" "$@"\n'


def _run(tmp_path: Path, parent: Path, mode: str) -> tuple[int, str, dict]:
    smoke = load("smoke_bundle")
    return smoke.run(fakes.app(tmp_path, mode), root_parent=parent, stop="sigterm", app_home=False, second_start=False, startup_timeout=30.0, keep=False, daemon_stop_timeout=10.0)


def test_a_failing_carriers_does_not_skip_the_stop_of_what_the_run_started(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    real = load("verify_runtime").carriers
    ledger = Ledger()

    def broken(root: Path, command: str | None = None) -> Any:
        for pid in real(root, command=command):
            ledger.record(pid)
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    monkeypatch.setattr(smoke.verify, "carriers", broken)
    try:
        code, line, evidence = _run(tmp_path, parent, "stubborn")
        assert len(ledger.births) >= 2, "the fake app and its daemon were not seen"
        assert ledger.live() == [], ledger.describe(ledger.live())
    finally:
        assert ledger.stop(kill=10) == []
    assert code == 2 and line.startswith("BLOCKED runner"), (code, line)
    names = [item["name"] for item in evidence["steps"]]
    assert names == ["preflight", "launch", "runner", "cleanup"], names
    cleanup = next(item for item in evidence["steps"] if item["name"] == "cleanup")
    assert cleanup["status"] == "BLOCKED" and "carriers failed" in cleanup["detail"]
    assert evidence["leftAtExit"] == [] and evidence["fallbackKilled"], evidence
    assert list(parent.iterdir()) == []


def test_a_carriers_that_fails_only_in_the_cleanup_makes_a_passing_run_blocked(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    real_carriers, real_manifest = smoke.verify.carriers, smoke.verify.check_manifest
    state = {"broken": False}

    def carriers(root: Path, command: str | None = None) -> Any:
        if state["broken"]:
            raise FileNotFoundError(2, "No such file or directory", "lsof")
        return real_carriers(root, command=command)

    def manifest(path: Path) -> Any:
        state["broken"] = True
        return real_manifest(path)

    monkeypatch.setattr(smoke.verify, "carriers", carriers)
    monkeypatch.setattr(smoke.verify, "check_manifest", manifest)
    code, line, evidence = _run(tmp_path, parent, "daemon")
    statuses = {item["name"]: item["status"] for item in evidence["steps"]}
    assert statuses.pop("cleanup") == "BLOCKED" and set(statuses.values()) == {"PASS"}, statuses
    assert code == 2 and line.startswith("BLOCKED cleanup carriers failed"), (code, line)
    assert evidence["leftAtExit"] == [] and list(parent.iterdir()) == []


def _hanging_ps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A ``ps`` first on PATH that hangs while the returned marker file exists and is the real one otherwise."""

    real = shutil.which("ps")
    assert real is not None
    marker = tmp_path / "ps-hangs"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "ps"
    stub.write_text(HANGING_PS.format(marker=marker, real=real), encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    return marker


def _decoy(ledger: Ledger, *args: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen([sys.executable, "-c", SLEEPER, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    assert ledger.record(process.pid)
    return process


def _reap(ledger: Ledger, *processes: subprocess.Popen[bytes]) -> None:
    survivors = ledger.stop(kill=10)
    for process in processes:
        process.wait(timeout=10)
    assert survivors == []


@pytest.mark.parametrize("kind", ["stale-record", "other-home", "other-bundle"])
def test_the_fallback_never_signals_a_daemon_run_process_that_is_not_this_runs(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    smoke = load("smoke_bundle")
    real_carriers, real_kill = smoke.verify.carriers, os.kill
    other_interpreter = tmp_path / "other" / "HyprialOS.app" / "Contents" / "Resources" / "runtime" / "bin" / "python3.12"
    arguments = {
        "stale-record": ["daemon", "run"],
        "other-home": ["--home", str(tmp_path / "other-home"), "daemon", "run"],
        "other-bundle": [str(other_interpreter), "-I", "-m", "hyprial.cli", "daemon", "run"],
    }[kind]
    ledger = Ledger()
    decoy = _decoy(ledger, *arguments)
    kills: list[tuple[int, int]] = []

    def spy(pid: int, sig: int) -> None:
        if sig != 0:
            kills.append((pid, sig))
        real_kill(pid, sig)

    def broken(root: Path, command: str | None = None) -> Any:
        for pid in real_carriers(root, command=command):
            ledger.record(pid)
        (root / "s" / "bootstrap.json").write_text(json.dumps({"pid": decoy.pid}), encoding="utf-8")
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(smoke.verify, "carriers", broken)
            patch.setattr(os, "kill", spy)
            _, _, evidence = _run(tmp_path, parent, "stubborn")
        ours = [pid for pid in ledger.births if pid != decoy.pid]
        assert len(ours) >= 2, "the fake app and its daemon were not seen"
        assert [pid for pid in ours if ledger.same(pid)] == [], ledger.describe(ours)
        assert decoy.pid not in [pid for pid, _ in kills], kills
        assert ledger.same(decoy.pid), "the decoy was killed"
        assert decoy.pid not in evidence["fallbackKilled"] and evidence["fallbackKilled"], evidence
        assert evidence["leftAtExit"] == []
    finally:
        _reap(ledger, decoy)


def test_a_pid_that_cannot_be_signalled_is_reported_left_and_the_others_are_still_stopped(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    real_carriers, real_kill = smoke.verify.carriers, os.kill
    ledger = Ledger()
    chosen: list[int] = []

    def broken(root: Path, command: str | None = None) -> Any:
        for pid in real_carriers(root, command=command):
            ledger.record(pid)
        chosen[:] = [int(json.loads((root / "s" / "daemon.json").read_text(encoding="utf-8"))["pid"])]
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    def deny(pid: int, sig: int) -> None:
        if sig != 0 and chosen and pid == chosen[0]:
            raise PermissionError(1, "Operation not permitted")
        real_kill(pid, sig)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(smoke.verify, "carriers", broken)
            patch.setattr(os, "kill", deny)
            _, line, evidence = _run(tmp_path, parent, "second")
        others = [pid for pid in ledger.births if pid != chosen[0]]
        assert len(others) >= 2, "the fake app and the second daemon were not seen"
        assert [pid for pid in others if ledger.same(pid)] == [], ledger.describe(others)
        assert ledger.same(chosen[0])
        assert evidence["leftAtExit"] == [chosen[0]], evidence
        assert chosen[0] not in evidence["fallbackKilled"] and evidence["fallbackKilled"], evidence
        cleanup = next(item for item in evidence["steps"] if item["name"] == "cleanup")
        assert cleanup["status"] == "BLOCKED" and f"could not be signalled: [{chosen[0]}]" in cleanup["detail"], cleanup
        assert "naming" not in cleanup["detail"], cleanup
    finally:
        _reap(ledger)


def test_the_held_processes_are_stopped_by_handle_even_when_ps_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    child = subprocess.Popen([sys.executable, "-c", IGNORING], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"

        def hang(*_args: object, **_options: object) -> Any:
            raise subprocess.TimeoutExpired(["ps"], 0.1)

        monkeypatch.setattr(smoke.verify, "_commands", hang)
        held = smoke._Held(root=tmp_path, interpreter=str(tmp_path / "bin" / "python3.12"), shells=[child])
        outcome = smoke._stop_identified(held, wait=0.5)
        assert child.poll() is not None, "the held process was left running"
        assert child.pid in outcome.stopped and child.pid not in outcome.left
        assert outcome.error and "ps" in outcome.error
    finally:
        child.kill()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()


def test_a_ps_that_hangs_during_the_fallback_leaves_the_leftovers_unknown_not_none(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    marker = _hanging_ps(monkeypatch, tmp_path)
    monkeypatch.setattr(smoke.verify, "PS_TIMEOUT", 2.0)
    real_carriers = smoke.verify.carriers
    ledger = Ledger()

    def broken(root: Path, command: str | None = None) -> Any:
        for pid in real_carriers(root, command=command):
            ledger.record(pid)
        marker.write_text("", encoding="utf-8")
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    monkeypatch.setattr(smoke.verify, "carriers", broken)
    try:
        code, line, evidence = _run(tmp_path, parent, "stubborn")
        assert code == 2 and line.startswith("BLOCKED runner"), (code, line)
        cleanup = next(item for item in evidence["steps"] if item["name"] == "cleanup")
        assert cleanup["status"] == "BLOCKED" and "naming the remaining processes failed" in cleanup["detail"], cleanup
        assert evidence["leftAtExit"] is None, evidence
    finally:
        marker.unlink(missing_ok=True)
        _reap(ledger)


def test_a_ps_that_hangs_on_the_first_call_is_a_blocked_runner_not_a_hang_or_traceback(tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    marker = _hanging_ps(monkeypatch, tmp_path)
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(smoke.verify, "PS_TIMEOUT", 0.3)
    try:
        code, line, evidence = _run(tmp_path, parent, "daemon")
    finally:
        marker.unlink(missing_ok=True)
    assert code == 2 and line.startswith("BLOCKED runner"), (code, line)
    assert [item["name"] for item in evidence["steps"]] == ["runner"], evidence["steps"]
    assert list(parent.iterdir()) == []


def test_pids_one_this_process_and_its_parent_are_never_identified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load("smoke_bundle")
    interpreter = str(tmp_path / "bin" / "python3.12")
    far = 4_000_000
    table = {1: f"{interpreter} -I -m hyprial.cli daemon run", os.getpid(): interpreter, os.getppid(): interpreter, far: f"{interpreter} -I -m hyprial.cli daemon run", far + 1: "/bin/sleep 5"}
    monkeypatch.setattr(smoke.verify, "_commands", lambda: table)
    monkeypatch.setattr(smoke.verify, "_alive", lambda pid: True)
    held = smoke._Held(root=tmp_path, interpreter=interpreter, shells=[])
    assert smoke._identified(held) == [far]
