"""Exercise the real autouse FD guard in isolated pytest subprocesses.

Each probe runs in its own session so a timeout kills the probe's xdist
workers with it; a loaded CI runner can take well past 40s to start them,
hence the generous PROBE_TIMEOUT_S."""

from __future__ import annotations

import shutil
import os
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROBE_TIMEOUT_S = 120


@pytest.mark.parametrize("returncode", [-15, 137])
def test_worker_failure_reports_exit_status_without_changing_it(
    capsys, monkeypatch, tmp_path, returncode
) -> None:
    from tests.conftest import _CiTestProfile

    (tmp_path / "memory.events").write_text("oom_kill 5\n")
    monkeypatch.setenv("CI_CGROUP_ROOT", str(tmp_path))

    class Process:
        pid = 1234

        def wait(self, timeout):
            assert timeout == 1.0
            return returncode

    node = SimpleNamespace(
        gateway=SimpleNamespace(id="gw0", _io=SimpleNamespace(popen=Process())),
        workeroutput={},
    )
    profile = _CiTestProfile(SimpleNamespace(getoption=lambda *a, **k: 2))
    profile.oom_kill_start = 3
    profile.pytest_testnodedown(node, object())
    line = capsys.readouterr().out
    data = json.loads(line.removeprefix("CI_WORKER_EXIT "))
    assert data == {
        "worker": "gw0",
        "pid": 1234,
        "returncode": returncode,
        "signal": 15 if os.name == "posix" and returncode == -15 else None,
        "oom_kill": 5,
        "oom_kill_since_start": 2,
    }


def test_worker_exit_missing_process_or_timeout_is_unknown(capsys, monkeypatch, tmp_path) -> None:
    from tests.conftest import _CiTestProfile

    monkeypatch.setenv("CI_CGROUP_ROOT", str(tmp_path / "absent"))

    class Process:
        pid = 1234

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("worker", timeout)

    profile = _CiTestProfile(SimpleNamespace(getoption=lambda *a, **k: 2))
    for io, expected_pid in ((object(), None), (SimpleNamespace(popen=Process()), 1234)):
        node = SimpleNamespace(gateway=SimpleNamespace(id="gw0", _io=io), workeroutput={})
        profile.pytest_testnodedown(node, object())
        data = json.loads(capsys.readouterr().out.removeprefix("CI_WORKER_EXIT "))
        assert data == {
            "worker": "gw0",
            "pid": expected_pid,
            "returncode": None,
            "signal": None,
            "oom_kill": None,
            "oom_kill_since_start": None,
        }
        profile.pytest_testnodedown(node, None)
        assert capsys.readouterr().out == ""


def _probe(
    source: str, *, other_source: str | None = None, workers: int = 0
) -> subprocess.CompletedProcess[str]:
    directory = Path(tempfile.mkdtemp(prefix="fd-guard-probe-", dir=ROOT / "tests"))
    path = directory / "test_probe.py"
    path.write_text(source, encoding="utf-8")
    if other_source is not None:
        (directory / "test_other.py").write_text(other_source, encoding="utf-8")
    parallel_args = (
        ["-n", str(workers), "--dist=loadfile", "--max-worker-restart=0"]
        if workers else []
    )
    args = [sys.executable, "-m", "pytest", "-q", *parallel_args, str(directory)]
    try:
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env={**os.environ, "HYPRIAL_CI_PROFILE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            pytest.fail(
                f"fd-guard probe exceeded {PROBE_TIMEOUT_S}s\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize("promote", [False, True])
def test_guard_collects_file_cycles_even_in_old_generations(promote: bool) -> None:
    result = _probe(
        f"""import gc
import weakref

state = {{}}
class Cycle:
    pass

def test_create_cycle(tmp_path):
    item = Cycle()
    item.file = open(tmp_path / 'cycle', 'w')
    item.self = item
    state['ref'] = weakref.ref(item)
    state['file'] = weakref.ref(item.file)
    if {promote!r}:
        gc.collect()  # Promote while a strong reference still exists.

def test_cycle_was_released():
    assert state['ref']() is None
    assert state['file']() is None
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


@pytest.mark.posix_only("/proc")
def test_parallel_workers_preserve_leak_failure_and_report_all_tests() -> None:
    result = _probe(
        """pinned = []
def test_leak(tmp_path):
    pinned.append(open(tmp_path / 'intentional-leak', 'w'))
""",
        other_source="""def test_first(tmp_path):
    (tmp_path / 'one').write_text('one')
def test_second(tmp_path):
    (tmp_path / 'two').write_text('two')
""",
        workers=2,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test leaked 1 file descriptor" in result.stdout
    profile = next(
        line.removeprefix("CI_TEST_PROFILE ")
        for line in result.stdout.splitlines()
        if line.startswith("CI_TEST_PROFILE ")
    )
    data = json.loads(profile)
    assert data["exit_status"] == 1
    assert data["parallel_controller"] is True
    assert data["collected"] == data["completed"] == 3
    assert len(data["workers"]) == 2
    assert sum(p["completed"] for p in data["workers"].values()) == 3
    assert {p["collection_digest"] for p in data["workers"].values()} == {
        data["collection_digest"]
    }
    assert any(p["gc_collections"]["2"] > 0 for p in data["workers"].values())

    crashed = _probe(
        "import os\ndef test_crash():\n    os._exit(7)\n",
        other_source="def test_survivor():\n    pass\n",
        workers=2,
    )
    assert crashed.returncode == 1, crashed.stdout + crashed.stderr
    assert "INTERNALERROR" not in crashed.stdout + crashed.stderr
    assert "test_crash" in crashed.stdout
    crash_exit = next(line for line in crashed.stdout.splitlines() if line.startswith("CI_WORKER_EXIT "))
    assert json.loads(crash_exit.removeprefix("CI_WORKER_EXIT "))["returncode"] == 7

    if os.name == "posix":
        signaled = _probe(
            "import os, signal\ndef test_crash():\n    os.kill(os.getpid(), signal.SIGTERM)\n",
            other_source="def test_survivor():\n    pass\n",
            workers=2,
        )
        assert signaled.returncode == 1, signaled.stdout + signaled.stderr
        assert "INTERNALERROR" not in signaled.stdout + signaled.stderr
        assert "test_crash" in signaled.stdout
        crash_exit = next(line for line in signaled.stdout.splitlines() if line.startswith("CI_WORKER_EXIT "))
        data = json.loads(crash_exit.removeprefix("CI_WORKER_EXIT "))
        assert data["returncode"] == -signal.SIGTERM
        assert data["signal"] == signal.SIGTERM


@pytest.mark.posix_only("/proc")
def test_guard_still_rejects_a_reachable_file_leak() -> None:
    result = _probe(
        """pinned = []

def test_leak(tmp_path):
    pinned.append(open(tmp_path / 'intentional-leak', 'w'))
"""
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test leaked 1 file descriptor" in result.stdout
    assert "ERROR at teardown" in result.stdout
    profile = next(
        line
        for line in result.stdout.splitlines()
        if line.startswith("CI_TEST_PROFILE ")
    )
    data = json.loads(profile.removeprefix("CI_TEST_PROFILE "))
    assert data["exit_status"] == 1
    assert data["completed"] == 1
    assert data["phases_seconds"]["teardown"] > 0
    assert data["gc_collections"]["2"] > 0


def test_clean_test_avoids_full_heap_collection() -> None:
    result = _probe(
        """import gc

generations = []
def observe(phase, info):
    if phase == 'start':
        generations.append(info['generation'])

def test_clean():
    gc.disable()  # Keep this controlled subprocess free of automatic GC.
    gc.callbacks.append(observe)

def test_only_young_generation_was_collected():
    gc.callbacks.remove(observe)
    gc.enable()
    assert 0 in generations
    assert 2 not in generations
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
