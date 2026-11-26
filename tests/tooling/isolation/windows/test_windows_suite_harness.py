"""Self-test of the Windows-suite harness in ``tests/support/windows_suite``.

Decision (2026-11-26): the watchdog behaviour
is exercised in real pytest subprocesses whose probe directory sits under
``tests/`` and runs with ``cwd`` at the repository root, the same way as
``tests/tooling/isolation/test_fd_hygiene_collection.py``.  The probe therefore
loads the real root ``conftest.py`` and its ``pytest_configure`` forwarding, so
the wiring is tested end to end rather than through a stand-in conftest.  The
harness variables of an outer run are removed from the probe environment.

Decision (same date): the fallback exit (faulthandler ``exit=True``, exit
code 1) is exercised with a test that holds the GIL inside one long C call
(``sum`` over a huge ``range``), so the Python watchdog thread cannot run and
only faulthandler's C thread can end the worker.  The worker exit codes are
read by a probe-local ``conftest.py`` from xdist's worker ``Popen`` objects
after the controller has torn its workers down.

Decision (same date): the ``os.kill`` interceptor is installed only on
Windows, so on other platforms its behaviour is tested by replacing the
module's ``is_windows`` check and checking it against a fake original
``os.kill``.  The ACL probe's audit hook cannot be removed, so it is installed
in a throwaway interpreter, never in the test process, with its ``is_windows``
replaced the same way.

Decision (same date, self-test rework): ``os.name`` is never changed.  With
``os.name == "nt"`` on Linux, ``pathlib.Path`` picks ``WindowsPath`` and raises
``NotImplementedError`` inside the install functions, and any other thread
that builds a path in that window would fail the same way.

Decision (2026-11-26): every probe directory written under ``tests/`` appends a
``pytest_ignore_collect`` guard to its ``conftest.py`` that ignores the
directory unless ``HYPRIAL_WINDOWS_SUITE_PROBE == "1"``, which only ``_probe``
sets.  A probe directory left behind by a killed pytest process is therefore
not collected by an ordinary ``pytest tests/`` run (including broken top-level
imports and GIL-holding tests).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

import pytest

from tests.support.windows_suite import host, watchdog

ROOT = Path(__file__).resolve().parents[4]
PROBE_TIMEOUT_S = 120
PROBE_ENV = "HYPRIAL_WINDOWS_SUITE_PROBE"
HARNESS_ENV = (
    watchdog.LEDGER_ENV,
    watchdog.WATCHDOG_ENV,
    watchdog.LEAK_ENV,
    watchdog.STACK_ENV,
    watchdog.GRACE_ENV,
    watchdog.ACL_PROBE_ENV,
    "HYPRIAL_CI_PROFILE",
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)
WORKER_EXIT_CONFTEST = """
import json
import os

_workers = {}


def pytest_testnodedown(node, error):
    _workers[node.gateway.id] = node.gateway._io.popen


def pytest_unconfigure(config):
    if _workers:
        codes = {name: popen.wait(timeout=60) for name, popen in _workers.items()}
        with open(os.environ["PROBE_WORKER_EXIT_FILE"], "w", encoding="utf-8") as stream:
            json.dump(codes, stream)
"""
PROBE_IGNORE_CONFTEST = """
import os as _hyprial_probe_os


def pytest_ignore_collect(collection_path, config):
    if _hyprial_probe_os.environ.get("HYPRIAL_WINDOWS_SUITE_PROBE") != "1":
        return True
    return None
"""


def _stop_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGKILL)
    else:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)


def _write_probe_dir(files: dict[str, str]) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="windows-suite-probe-", dir=ROOT / "tests"))
    for name, source in files.items():
        (directory / name).write_text(textwrap.dedent(source), encoding="utf-8")
    conftest = directory / "conftest.py"
    existing = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    guard = textwrap.dedent(PROBE_IGNORE_CONFTEST).lstrip("\n")
    content = f"{existing.rstrip()}\n\n\n{guard}" if existing.strip() else guard
    conftest.write_text(content, encoding="utf-8")
    return directory


def _probe(
    tmp_path: Path, files: dict[str, str], *, workers: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    directory = _write_probe_dir(files)
    environment = {key: value for key, value in os.environ.items() if key not in HARNESS_ENV}
    environment["PROBE_WORKER_EXIT_FILE"] = str(tmp_path / "worker-exit.json")
    environment.update(env or {})
    environment[PROBE_ENV] = "1"
    args = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-o", "faulthandler_timeout=0", "-n", str(workers), "--max-worker-restart=0",
        "--continue-on-collection-errors", f"--basetemp={tmp_path / 'basetemp'}",
        f"--junitxml={tmp_path / 'junit.xml'}", str(directory),
    ]
    group = (
        {"start_new_session": True}
        if os.name == "posix"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    )
    try:
        process = subprocess.Popen(
            args, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, **group,
        )
        try:
            stdout, stderr = process.communicate(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _stop_tree(process)
            stdout, stderr = process.communicate()
            pytest.fail(f"probe exceeded {PROBE_TIMEOUT_S}s\nstdout:\n{stdout}\nstderr:\n{stderr}")
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        shutil.rmtree(directory)


def _events(ledger: Path, worker: str) -> list[dict[str, Any]]:
    lines = (ledger / f"{worker}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _of(events: list[dict[str, Any]], kind: str, test: str) -> list[dict[str, Any]]:
    return [
        event for event in events
        if event["event"] == kind and str(event.get("nodeid", "")).endswith(f"::{test}")
    ]


def _junit_names(tmp_path: Path) -> set[str]:
    root = ElementTree.parse(tmp_path / "junit.xml").getroot()
    return {case.get("name", "") for case in root.iter("testcase")}


def _worker_exits(tmp_path: Path) -> dict[str, int]:
    return json.loads((tmp_path / "worker-exit.json").read_text(encoding="utf-8"))


def _ledger_env(ledger: Path, **extra: str) -> dict[str, str]:
    return {watchdog.LEDGER_ENV: str(ledger), **extra}


def test_without_a_ledger_dir_nothing_is_registered_created_or_armed(tmp_path: Path) -> None:
    result = _probe(
        tmp_path,
        {
            "test_probe.py": """
            import os
            import time

            from tests.support.windows_suite.host import KillInterceptor
            from tests.support.windows_suite.watchdog import (
                RECORDER_PLUGIN, STACKS_PLUGIN, current_nodeid,
            )


            def test_nothing_is_registered(request):
                manager = request.config.pluginmanager
                assert manager.get_plugin(RECORDER_PLUGIN) is None
                assert manager.get_plugin(STACKS_PLUGIN) is None
                assert not isinstance(os.kill, KillInterceptor)
                assert current_nodeid() is None


            def test_no_test_timeout_is_armed():
                time.sleep(2)
            """,
        },
        workers=1,
        env={watchdog.WATCHDOG_ENV: "1", watchdog.ACL_PROBE_ENV: "1"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert not list(tmp_path.rglob("*.jsonl"))
    assert not list(tmp_path.rglob("hang-*.txt"))
    assert not list(tmp_path.rglob("stacks-*.txt"))


def test_a_serial_run_records_each_test_and_never_applies_the_test_timeout(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    result = _probe(
        tmp_path,
        {
            "test_probe.py": """
            import time

            from tests.support.windows_suite.watchdog import current_nodeid


            def test_knows_its_nodeid(request):
                assert current_nodeid() == request.node.nodeid


            def test_outlives_the_test_timeout():
                time.sleep(3)
            """,
        },
        workers=0,
        env=_ledger_env(ledger, **{watchdog.WATCHDOG_ENV: "1"}),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    events = _events(ledger, "controller")
    for test in ("test_knows_its_nodeid", "test_outlives_the_test_timeout"):
        (start,) = _of(events, "start", test)
        assert start["phase"] == "setup"
        assert {"nodeid", "phase", "wall", "mono", "worker", "pid", "thread"} <= start.keys()
        assert start["worker"] == "controller"
        assert [event["phase"] for event in _of(events, "phase", test)] == ["call", "teardown"]
        (end,) = _of(events, "end", test)
        assert (end["outcome"], end["phase"], end["excinfo"]) == ("passed", "call", None)
        assert end["mono"] > start["mono"]
    (slow,) = _of(events, "end", "test_outlives_the_test_timeout")
    assert slow["duration"] >= 3
    assert not [event for event in events if event["event"] == "crash"]
    assert not list(ledger.glob("hang-*.txt"))
    assert not (ledger / "stacks-controller.txt").exists()


def test_a_worker_records_outcomes_failures_and_collection_errors(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    result = _probe(
        tmp_path,
        {
            "test_probe.py": """
            import pytest


            @pytest.fixture
            def broken():
                raise RuntimeError("setup broke")


            def test_passes():
                pass


            def test_fails():
                raise ValueError("x" * 600)


            def test_skips():
                pytest.skip("not here")


            @pytest.mark.xfail(reason="known", strict=True)
            def test_xfails():
                assert False


            @pytest.mark.xfail(reason="known")
            def test_xpasses():
                pass


            def test_errors_in_setup(broken):
                pass
            """,
            "test_broken.py": "import module_that_does_not_exist_for_the_probe\n",
        },
        workers=1,
        env=_ledger_env(ledger, **{watchdog.WATCHDOG_ENV: "120"}),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    events = _events(ledger, "gw0")
    outcomes = {
        test: (end["outcome"], end["phase"])
        for test in (
            "test_passes", "test_fails", "test_skips",
            "test_xfails", "test_xpasses", "test_errors_in_setup",
        )
        for end in _of(events, "end", test)
    }
    assert outcomes == {
        "test_passes": ("passed", "call"),
        "test_fails": ("failed", "call"),
        "test_skips": ("skipped", "call"),
        "test_xfails": ("xfailed", "call"),
        "test_xpasses": ("xpassed", "call"),
        "test_errors_in_setup": ("error", "setup"),
    }
    (failed,) = _of(events, "end", "test_fails")
    assert failed["excinfo"]["type"] == "builtins.ValueError"
    assert len(failed["excinfo"]["message"]) == watchdog.MESSAGE_LIMIT
    (errored,) = _of(events, "end", "test_errors_in_setup")
    assert errored["excinfo"] == {"type": "builtins.RuntimeError", "message": "setup broke"}
    (collected,) = [event for event in events if event["event"] == "collect"]
    assert collected["collected"] == 6
    assert [error["nodeid"].rsplit("/", 1)[-1] for error in collected["errors"]] == ["test_broken.py"]
    assert not [event for event in events if event["event"] == "crash"]
    assert not (ledger / "controller.jsonl").exists()
    assert not list(ledger.glob("hang-*.txt"))
    assert not list(ledger.glob("stacks-*.txt"))


def test_l1_dumps_records_and_ends_a_hung_worker_with_96(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    result = _probe(
        tmp_path,
        {
            "conftest.py": WORKER_EXIT_CONFTEST,
            "test_probe.py": """
            import time


            def test_hangs():
                time.sleep(30)
            """,
        },
        workers=1,
        env=_ledger_env(ledger, **{watchdog.WATCHDOG_ENV: "1"}),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    hang = (ledger / "hang-gw0.txt").read_text(encoding="utf-8", errors="replace")
    assert "hyprial watchdog: test-timeout" in hang
    assert "Thread" in hang
    (crash,) = [event for event in _events(ledger, "gw0") if event["event"] == "crash"]
    assert crash["reason"] == "test-timeout"
    assert crash["exit_code"] == watchdog.TEST_TIMEOUT_EXIT
    assert crash["nodeid"].endswith("::test_hangs")
    assert "test_hangs" in _junit_names(tmp_path)
    assert _worker_exits(tmp_path) == {"gw0": watchdog.TEST_TIMEOUT_EXIT}


def test_l1_fallback_ends_a_worker_that_holds_the_gil_with_1(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    result = _probe(
        tmp_path,
        {
            "conftest.py": WORKER_EXIT_CONFTEST,
            "test_probe.py": """
            def test_holds_the_gil():
                sum(range(10**12))
            """,
        },
        workers=1,
        env=_ledger_env(ledger, **{watchdog.WATCHDOG_ENV: "1", watchdog.GRACE_ENV: "1"}),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    hang = (ledger / "hang-gw0.txt").read_text(encoding="utf-8", errors="replace")
    assert "Timeout (" in hang
    assert "Thread" in hang
    assert "hyprial watchdog" not in hang
    assert not [event for event in _events(ledger, "gw0") if event["event"] == "crash"]
    assert "test_holds_the_gil" in _junit_names(tmp_path)
    assert _worker_exits(tmp_path) == {"gw0": watchdog.FALLBACK_EXIT}


def test_l2_records_the_leak_and_ends_a_worker_that_cannot_exit_with_97(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger"
    result = _probe(
        tmp_path,
        {
            "conftest.py": WORKER_EXIT_CONFTEST,
            "test_probe.py": """
            import threading

            _never = threading.Event()


            def test_leaves_a_thread():
                threading.Thread(target=_never.wait, name="probe-leaked-thread").start()
            """,
        },
        workers=1,
        env=_ledger_env(ledger, **{watchdog.LEAK_ENV: "1"}),
    )

    assert "1 passed" in result.stdout, result.stdout + result.stderr
    events = _events(ledger, "gw0")
    (leak,) = [event for event in events if event["event"] == "leak"]
    assert "probe-leaked-thread" in leak["threads"]
    (crash,) = [event for event in events if event["event"] == "crash"]
    assert crash["reason"] == "leak-timeout"
    assert crash["exit_code"] == watchdog.LEAK_TIMEOUT_EXIT
    assert "probe-leaked-thread" in crash["threads"]
    assert "hyprial watchdog: leak-timeout" in (ledger / "hang-gw0.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    assert _worker_exits(tmp_path) == {"gw0": watchdog.LEAK_TIMEOUT_EXIT}


def test_ledger_events_are_flushed_and_fsynced_one_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    ledger = watchdog.Ledger(tmp_path / "gw9.jsonl", "gw9")
    assert not ledger.path.exists()

    ledger.write("start", nodeid="probe.py::test", phase="setup", thread=1)
    assert len(synced) == 1
    ledger.write("end", nodeid="probe.py::test", phase="call", outcome="passed")
    assert len(synced) == 2

    first, second = (json.loads(line) for line in ledger.path.read_text().splitlines())
    assert (first["event"], first["worker"], first["pid"]) == ("start", "gw9", os.getpid())
    assert {"wall", "mono"} <= first.keys()
    assert second["outcome"] == "passed"


def test_the_current_nodeid_exists_only_while_a_test_runs(tmp_path: Path) -> None:
    recorder = watchdog.RunRecorder(watchdog.Ledger(tmp_path / "x.jsonl", "x"))
    assert recorder.current_nodeid is None

    recorder.pytest_runtest_logstart(nodeid="probe.py::test")
    assert recorder.current_nodeid == "probe.py::test"

    recorder.pytest_runtest_logfinish(nodeid="probe.py::test")
    assert recorder.current_nodeid is None


def test_the_kill_interceptor_refuses_probes_and_self_signals_and_forwards_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host.is_windows() is (os.name == "nt")
    if os.name != "nt":
        assert host.install_kill_interceptor(tmp_path, lambda: None) is None
    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: forwarded.append((pid, sig)))
    monkeypatch.setattr(host, "is_windows", lambda: True)
    interceptor = host.install_kill_interceptor(tmp_path, lambda: "probe.py::test")
    again = host.install_kill_interceptor(tmp_path, lambda: "other")

    assert isinstance(os.kill, host.KillInterceptor)
    assert again is interceptor is os.kill
    with pytest.raises(AssertionError, match=r"os\.kill\(pid=\d+, sig=0\) is not allowed"):
        os.kill(os.getpid(), 0)
    with pytest.raises(AssertionError):
        os.kill(4242, 1)
    with pytest.raises(AssertionError):
        os.kill(os.getpid(), signal.SIGTERM)
    os.kill(4242, int(signal.SIGTERM))

    assert forwarded == [(4242, int(signal.SIGTERM))]
    hits = [
        json.loads(line)
        for line in (tmp_path / host.HITS_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert [(hit["nodeid"], hit["pid"], hit["sig"]) for hit in hits] == [
        ("probe.py::test", os.getpid(), 0),
        ("probe.py::test", 4242, 1),
        ("probe.py::test", os.getpid(), int(signal.SIGTERM)),
    ]


def test_pid_alive_sees_this_process_and_not_a_reaped_child() -> None:
    assert host.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert host.pid_alive(child.pid) is False


ACL_PROBE_SCRIPT = """
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from tests.support.windows_suite import acl_probe

ledger = Path(sys.argv[1])
acl_probe.is_windows = lambda: True
assert acl_probe.install_acl_probe(ledger, lambda: "probe.py::test")
os.mkdir(ledger / "made-by-mkdir", 0o700)
tempfile.mkdtemp(prefix="made-by-mkdtemp-", dir=ledger)
"""


def test_the_acl_probe_records_directory_creation_points(tmp_path: Path) -> None:
    if os.name != "nt":
        from tests.support.windows_suite import acl_probe

        assert acl_probe.is_windows() is False
        assert acl_probe.install_acl_probe(tmp_path, lambda: None) is False
    ledger = tmp_path / "ledger"
    ledger.mkdir()

    subprocess.run(
        [sys.executable, "-I", "-c", ACL_PROBE_SCRIPT, str(ledger), str(ROOT)],
        check=True, timeout=60, capture_output=True,
    )

    records = [
        json.loads(line)
        for line in (ledger / "acl-probe.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    made = {
        (record["event"], Path(record["path"]).name.split("-", 3)[2])
        for record in records
        if Path(record["path"]).name.startswith("made-by-")
    }
    assert made == {("os.mkdir", "mkdir"), ("os.mkdir", "mkdtemp"), ("tempfile.mkdtemp", "mkdtemp")}
    (mkdir,) = [
        record for record in records
        if record["event"] == "os.mkdir" and record["path"].endswith("made-by-mkdir")
    ]
    assert mkdir["mode"] == 0o700
    assert all(record["nodeid"] == "probe.py::test" for record in records)
    assert all({"mono", "pid"} <= record.keys() for record in records)


def test_a_leftover_probe_directory_is_ignored_by_an_ordinary_collection(tmp_path: Path) -> None:
    cases = (
        {
            "test_broken.py": "import module_that_does_not_exist_for_the_probe\n",
            "test_gil.py": """
            def test_holds_gil():
                sum(range(10**12))
            """,
        },
        {
            "conftest.py": WORKER_EXIT_CONFTEST,
            "test_broken.py": "import module_that_does_not_exist_for_the_probe\n",
            "test_gil.py": """
            def test_holds_gil():
                sum(range(10**12))
            """,
        },
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in HARNESS_ENV and key != PROBE_ENV
    }
    for index, files in enumerate(cases):
        directory = _write_probe_dir(files)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-p",
                    "no:cacheprovider",
                    f"--basetemp={tmp_path / f'basetemp-{index}'}",
                    str(directory),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
            )
            assert result.returncode == 5, result.stdout + result.stderr
            assert "collected 0 items" in result.stdout, result.stdout + result.stderr
        finally:
            shutil.rmtree(directory, ignore_errors=True)
