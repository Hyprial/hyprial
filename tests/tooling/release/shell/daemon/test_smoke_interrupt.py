"""An interrupted ``smoke_bundle.py`` stops everything it started, even when a second signal arrives during the cleanup.

The smoke runs as a real process (``main``) against the fake app whose daemon
ignores SIGTERM, so the cleanup needs its 10 s SIGTERM window and a SIGKILL:
the second signal is sent once the app is gone (the cleanup terminated it
first), which is inside that window. SIGINT, SIGTERM and SIGHUP all end with
runner BLOCKED "interrupted by <signal>" and status 128 + signal. Every
process is recorded in the birth-checked ledger and the test ends only when
none is left; whatever is left on failure is killed by birth.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.process_table import Ledger, alive, wait_until
from tests.tooling.release.shell.daemon import fakes
from tests.tooling.release.shell.support import load

WAIT = 60.0


def _daemon_pid(root: Path) -> int | None:
    try:
        return int(json.loads((root / "s" / "daemon.json").read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError):
        return None


def _roots(parent: Path) -> list[Path]:
    return sorted(parent.glob("sm-*"))


def _up(parent: Path) -> bool:
    roots = _roots(parent)
    if len(roots) != 1:
        return False
    root = roots[0]
    pid = _daemon_pid(root)
    return pid is not None and alive(pid) and (root / "u" / ".fake-instance").exists() and not (root / "s" / "gui" / "daemon" / "launch.url").exists()


@pytest.mark.parametrize(("sent", "name", "status"), [(signal.SIGINT, "SIGINT", 130), (signal.SIGTERM, "SIGTERM", 143), (signal.SIGHUP, "SIGHUP", 129)])
def test_an_interrupted_smoke_stops_the_app_and_the_daemon_despite_a_second_signal(tmp_path: Path, parent: Path, sent: int, name: str, status: int) -> None:
    app = fakes.app(tmp_path, "stubborn")
    evidence_file = tmp_path / "evidence.json"
    command = [sys.executable, str(Path(load("smoke_bundle").__file__)), "--app", str(app), "--root-parent", str(parent), "--stop", "sigterm", "--hold", "300", "--evidence", str(evidence_file)]
    ledger = Ledger()
    smoke = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert ledger.record(smoke.pid)
        assert wait_until(lambda: _up(parent), WAIT), "the fake app and its daemon did not come up"
        root = _roots(parent)[0]
        daemon = _daemon_pid(root)
        app_pid = int((root / "u" / ".fake-instance").read_text(encoding="utf-8"))
        assert daemon is not None and ledger.record(daemon) and ledger.record(app_pid)
        ledger.adopt_descendants(smoke.pid)
        os.kill(smoke.pid, sent)
        assert wait_until(lambda: not alive(app_pid), WAIT), "the cleanup did not terminate the app"
        assert smoke.poll() is None and alive(daemon), "the stubborn daemon should still be inside the cleanup's SIGTERM window"
        os.kill(smoke.pid, sent)
        out, err = smoke.communicate(timeout=WAIT)
        assert smoke.returncode == status, (smoke.returncode, out, err)
        assert out.strip() == f"BLOCKED runner interrupted by {name}", (out, err)
        assert ledger.live() == [], ledger.describe(ledger.live())
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
        assert evidence["interrupted"] == name and evidence["leftAtExit"] == [] and daemon in evidence["fallbackKilled"]
        assert [item["name"] for item in evidence["steps"]][-1] == "runner"
        assert _roots(parent) == []
    finally:
        survivors = ledger.stop(kill=10)
        smoke.wait(timeout=10)
        for pipe in (smoke.stdout, smoke.stderr):
            pipe.close()
        assert survivors == []


@pytest.mark.parametrize("ignored", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_signal_the_caller_ignores_stays_ignored_and_the_run_completes(tmp_path: Path, parent: Path, ignored: int) -> None:
    app = fakes.app(tmp_path, "daemon")
    evidence_file = tmp_path / "evidence.json"
    command = [sys.executable, str(Path(load("smoke_bundle").__file__)), "--app", str(app), "--root-parent", str(parent), "--stop", "sigterm", "--hold", "3", "--evidence", str(evidence_file)]
    ledger = Ledger()
    smoke = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=lambda: signal.signal(ignored, signal.SIG_IGN))
    try:
        assert ledger.record(smoke.pid)
        assert wait_until(lambda: _up(parent), WAIT), "the fake app and its daemon did not come up"
        ledger.adopt_descendants(smoke.pid)
        os.kill(smoke.pid, ignored)
        out, err = smoke.communicate(timeout=WAIT)
        assert (smoke.returncode, out.strip()) == (0, "PASS"), (smoke.returncode, out, err)
        assert "interrupted" not in json.loads(evidence_file.read_text(encoding="utf-8"))
    finally:
        survivors = ledger.stop(kill=10)
        smoke.wait(timeout=10)
        for pipe in (smoke.stdout, smoke.stderr):
            pipe.close()
        assert survivors == []
