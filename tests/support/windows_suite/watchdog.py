"""Per-process JSONL ledger and the L1-L3 watchdogs for long Windows pytest runs.

Everything here is enabled by :func:`configure`, which the root
``tests/conftest.py`` calls from ``pytest_configure``.  Without
``HYPRIAL_TEST_LEDGER_DIR`` it registers nothing, opens nothing and arms
nothing.  No ``-p`` plugin is used: ``-p`` imports too early and would also load
for runs without xdist.

Ledger contract (read by ``scripts/ci/windows/run_waves.py``)
-------------------------------------------------------------
One file per process in the ledger directory: ``gwN.jsonl`` for an xdist
worker, ``controller.jsonl`` for the controlling process (with ``-n 0`` that
process also runs the tests).  A file is created by its first event, so an xdist
controller, which runs no tests, normally writes none.  Every event is one JSON
line written with ``flush`` and ``fsync`` and carries ``event``, ``worker``,
``pid``, ``wall`` (``time.time()``) and ``mono`` (``time.monotonic()``):

- ``start`` (setup begins) and ``phase`` (call or teardown begins): ``nodeid``,
  ``phase``, ``thread``.
- ``end`` (after the teardown report): ``nodeid``, ``phase`` (the phase that
  decided the outcome), ``outcome`` (``passed``, ``failed``, ``error``,
  ``skipped``, ``xfailed``, ``xpassed``), ``duration`` (seconds, all phases),
  ``excinfo`` (``None`` or ``{"type": "module.QualName", "message": first 500
  characters}`` of the first failing phase).
- ``collect`` (written by ``collection.py``): ``collected`` and ``errors``.
- ``leak`` (worker, after ``pytest_sessionfinish``): ``threads``, the names of
  live non-daemon threads.
- ``crash`` (a watchdog fired): ``reason`` (``test-timeout`` or
  ``leak-timeout``), ``exit_code`` and, for a test timeout, ``nodeid``,
  ``phase``, ``seconds``.

Exits
-----
- L1, test timeout (xdist worker only, ``HYPRIAL_TEST_WATCHDOG_SECONDS``):
  exit code 96 on the primary path, 1 on the fallback path.
- L2, worker does not exit after the session (``HYPRIAL_TEST_LEAK_SECONDS``,
  default 120): exit code 97 on the primary path, 1 on the fallback path.
- L3, controller (``HYPRIAL_TEST_STACK_SECONDS``, default 60): never exits.

Decision (2026-11-26): primary path plus faulthandler fallback.
``faulthandler.dump_traceback_later(T, exit=True)`` cannot be combined with a
``crash`` event followed by ``os._exit(96)``: faulthandler's ``exit=True``
calls C ``_exit(1)`` from its own C thread and runs no Python, so it can
neither write the event nor choose the exit code.  The primary path is
therefore a Python daemon thread: at ``T`` it dumps every thread with
``faulthandler.dump_traceback`` into ``hang-gwN.txt``, writes the ``crash``
event (flushed and fsynced) and only then calls ``os._exit``.  A thread that
holds the GIL in native code keeps that Python thread from running, so each
arming also arms ``faulthandler.dump_traceback_later(T + grace, exit=True)``
on the same file (grace 30 seconds, ``HYPRIAL_TEST_WATCHDOG_GRACE_SECONDS``
for the self-test).  That fallback writes faulthandler's ``Timeout (...)!``
dump and exits with 1 and no ``crash`` event; ``run_waves.py`` treats
``hang-gwN.txt`` plus exit code 1 as ``test-watchdog-timeout`` as well.

Decision (2026-11-26): the ``leak`` event is the primary L2 signal
and exit code 97 is only the fallback.  xdist tears its workers down with
``execnet`` ``Group.terminate(10)``, which kills a worker still alive after ten
seconds, so the 120-second L2 timer normally loses that race.  It fires only
when the controller cannot terminate the worker.

Decision (2026-11-26): only one faulthandler timer exists per
process.  L1 and L2 use it in workers, L3 in the controller, never both in one
process.  pytest's faulthandler plugin cancels that timer in
``pytest_exception_interact`` (every failing test or collection error), so L3
re-arms itself from the same hook.  pytest's own ``faulthandler_timeout`` must
stay 0 (``run_waves.py`` passes ``-o faulthandler_timeout=0``), otherwise its
per-test cancel would also stop L3 in a ``-n 0`` run.

Decision (2026-11-26): ``hang-gwN.txt`` and
``stacks-controller.txt`` must exist while a timer is armed, because the
faulthandler C thread writes to their descriptors.  An ``atexit`` hook cancels
the timer and removes them again when they are still empty, so after a clean
exit the presence of a hang file means a watchdog fired.  ``atexit`` runs after
``threading._shutdown``, so a worker blocked on a leaked thread keeps them.
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import os
import threading
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any

import pytest

LEDGER_ENV = "HYPRIAL_TEST_LEDGER_DIR"
WATCHDOG_ENV = "HYPRIAL_TEST_WATCHDOG_SECONDS"
LEAK_ENV = "HYPRIAL_TEST_LEAK_SECONDS"
STACK_ENV = "HYPRIAL_TEST_STACK_SECONDS"
GRACE_ENV = "HYPRIAL_TEST_WATCHDOG_GRACE_SECONDS"
ACL_PROBE_ENV = "HYPRIAL_TEST_ACL_PROBE"
TEST_TIMEOUT_EXIT = 96
LEAK_TIMEOUT_EXIT = 97
FALLBACK_EXIT = 1
DEFAULT_LEAK_SECONDS = 120
DEFAULT_STACK_SECONDS = 60
DEFAULT_GRACE_SECONDS = 30
RECORDER_PLUGIN = "hyprial-windows-ledger"
STACKS_PLUGIN = "hyprial-windows-stacks"
COLLECTION_PLUGIN = "hyprial-windows-collection"
MESSAGE_LIMIT = 500

_ACTIVE: RunRecorder | None = None
_CONFIGURED = False


def current_nodeid() -> str | None:
    """The node id of the test this process is running now, if any."""

    return None if _ACTIVE is None else _ACTIVE.current_nodeid


class Ledger:
    """Append-only JSONL event file for one process, durable per event."""

    def __init__(self, path: Path, worker: str) -> None:
        self.path = path
        self.worker = worker
        self._lock = threading.Lock()
        self._stream: Any = None

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "event": event,
            "worker": self.worker,
            "pid": os.getpid(),
            "wall": time.time(),
            "mono": time.monotonic(),
            **fields,
        }
        line = (json.dumps(record, default=str) + "\n").encode("utf-8")
        with self._lock:
            if self._stream is None:
                self._stream = open(self.path, "ab")
            self._stream.write(line)
            self._stream.flush()
            os.fsync(self._stream.fileno())


class DumpFile:
    """A stack-dump target that faulthandler can write to from its C thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = open(path, "ab", buffering=0)

    def header(self, text: str) -> None:
        self.stream.write((text + "\n").encode("utf-8"))

    def discard_if_empty(self) -> None:
        self.stream.close()
        try:
            if self.path.stat().st_size == 0:
                self.path.unlink()
        except FileNotFoundError:
            pass


class Alarm:
    """One re-armable deadline served by a daemon thread."""

    def __init__(self, name: str) -> None:
        self._condition = threading.Condition()
        self._deadline: float | None = None
        self._action: Callable[[], None] | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def arm(self, seconds: float, action: Callable[[], None]) -> None:
        with self._condition:
            self._deadline = time.monotonic() + seconds
            self._action = action
            self._condition.notify()

    def cancel(self) -> None:
        with self._condition:
            self._deadline = None
            self._action = None
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._deadline is None:
                        self._condition.wait()
                        continue
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                action = self._action
                self._deadline = None
                self._action = None
            if action is not None:
                action()


def non_daemon_threads() -> list[str]:
    """Names of live threads that would block interpreter shutdown."""

    main = threading.main_thread()
    return [
        thread.name
        for thread in threading.enumerate()
        if thread is not main and not thread.daemon and thread.is_alive()
    ]


def describe_excinfo(excinfo: pytest.ExceptionInfo[BaseException]) -> dict[str, str]:
    kind = excinfo.type
    return {
        "type": f"{kind.__module__}.{kind.__qualname__}",
        "message": str(excinfo.value)[:MESSAGE_LIMIT],
    }


def outcome_of(reports: Sequence[pytest.TestReport]) -> tuple[str, str]:
    """``(outcome, phase)`` of one test from its setup/call/teardown reports."""

    for report in reports:
        if report.failed:
            return ("failed" if report.when == "call" else "error"), report.when
        if report.skipped:
            return ("xfailed" if hasattr(report, "wasxfail") else "skipped"), report.when
    for report in reports:
        if report.when == "call" and hasattr(report, "wasxfail"):
            return "xpassed", "call"
    return "passed", "call"


class RunRecorder:
    """Ledger events for every test this process runs, plus L1 and L2 in workers."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        hang: DumpFile | None = None,
        test_seconds: int | None = None,
        leak_seconds: int = DEFAULT_LEAK_SECONDS,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.ledger = ledger
        self.current_nodeid: str | None = None
        self._hang = hang
        self._test_seconds = test_seconds if hang is not None else None
        self._leak_seconds = leak_seconds
        self._grace_seconds = grace_seconds
        self._alarm = Alarm("hyprial-test-watchdog") if hang is not None else None
        self._reports: dict[str, list[pytest.TestReport]] = {}
        self._excinfo: dict[str, dict[str, str]] = {}

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.current_nodeid = nodeid

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.current_nodeid = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None, object, object]:
        self._enter(item, "setup")
        try:
            return (yield)
        finally:
            self._leave()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        self._enter(item, "call")
        try:
            return (yield)
        finally:
            self._leave()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Generator[None, object, object]:
        self._enter(item, "teardown")
        try:
            return (yield)
        finally:
            self._leave()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        if report.failed and call.excinfo is not None:
            self._excinfo.setdefault(item.nodeid, describe_excinfo(call.excinfo))
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        reports = self._reports.setdefault(report.nodeid, [])
        reports.append(report)
        if report.when != "teardown":
            return
        del self._reports[report.nodeid]
        outcome, phase = outcome_of(reports)
        self.ledger.write(
            "end",
            nodeid=report.nodeid,
            phase=phase,
            outcome=outcome,
            duration=sum(entry.duration for entry in reports),
            excinfo=self._excinfo.pop(report.nodeid, None),
        )

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None, object, object]:
        try:
            return (yield)
        finally:
            if self._alarm is not None:
                self._watch_shutdown()

    def _enter(self, item: pytest.Item, phase: str) -> None:
        if self._test_seconds is not None:
            seconds = self._test_seconds
            fields = {"nodeid": item.nodeid, "phase": phase, "seconds": seconds}
            self._arm(seconds, "test-timeout", TEST_TIMEOUT_EXIT, fields)
        self.ledger.write(
            "start" if phase == "setup" else "phase",
            nodeid=item.nodeid,
            phase=phase,
            thread=threading.get_ident(),
        )

    def _leave(self) -> None:
        if self._test_seconds is None or self._alarm is None:
            return
        self._alarm.cancel()
        faulthandler.cancel_dump_traceback_later()

    def _watch_shutdown(self) -> None:
        threads = non_daemon_threads()
        if threads:
            self.ledger.write("leak", threads=threads)
        self._arm(self._leak_seconds, "leak-timeout", LEAK_TIMEOUT_EXIT, {})

    def _arm(self, seconds: int, reason: str, exit_code: int, fields: dict[str, Any]) -> None:
        assert self._alarm is not None and self._hang is not None
        self._alarm.arm(seconds, lambda: self._fire(reason, exit_code, fields))
        faulthandler.dump_traceback_later(
            seconds + self._grace_seconds, exit=True, file=self._hang.stream
        )

    def _fire(self, reason: str, exit_code: int, fields: dict[str, Any]) -> None:
        assert self._hang is not None
        try:
            self._hang.header(
                f"hyprial watchdog: {reason} worker={self.ledger.worker} "
                f"exit_code={exit_code} {json.dumps(fields, default=str)}"
            )
            faulthandler.dump_traceback(file=self._hang.stream, all_threads=True)
            if reason == "leak-timeout":
                fields = {**fields, "threads": non_daemon_threads()}
            self.ledger.write("crash", reason=reason, exit_code=exit_code, **fields)
        finally:
            os._exit(exit_code)


class ControllerStacks:
    """L3: periodic stack dumps of the controlling process; never exits it."""

    def __init__(self, dump: DumpFile, seconds: int) -> None:
        self._dump = dump
        self._seconds = seconds
        self._active = True
        self._arm()

    def _arm(self) -> None:
        faulthandler.dump_traceback_later(
            self._seconds, repeat=True, exit=False, file=self._dump.stream
        )

    @pytest.hookimpl(trylast=True)
    def pytest_exception_interact(self) -> None:
        if self._active:
            self._arm()

    def pytest_unconfigure(self) -> None:
        self._active = False
        faulthandler.cancel_dump_traceback_later()


def _env_seconds(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise pytest.UsageError(f"{name} must be a positive integer number of seconds, got {raw!r}")
    return value


def _release(dumps: list[DumpFile]) -> None:
    faulthandler.cancel_dump_traceback_later()
    for dump in dumps:
        dump.discard_if_empty()


def configure(config: pytest.Config) -> None:
    """Register the Windows-suite harness when ``HYPRIAL_TEST_LEDGER_DIR`` is set."""

    global _ACTIVE, _CONFIGURED
    raw = os.environ.get(LEDGER_ENV, "").strip()
    if not raw or _CONFIGURED:
        return
    _CONFIGURED = True
    from tests.support.windows_suite import acl_probe, collection, host

    ledger_dir = Path(raw)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    workerinput = getattr(config, "workerinput", None)
    worker = "controller" if workerinput is None else str(workerinput["workerid"])
    xdist_controller = workerinput is None and config.getoption("dist", default="no") != "no"
    ledger = Ledger(ledger_dir / f"{worker}.jsonl", worker)
    dumps: list[DumpFile] = []
    recorder: RunRecorder | None = None
    if workerinput is not None:
        hang = DumpFile(ledger_dir / f"hang-{worker}.txt")
        dumps.append(hang)
        recorder = RunRecorder(
            ledger,
            hang=hang,
            test_seconds=_env_seconds(WATCHDOG_ENV),
            leak_seconds=_env_seconds(LEAK_ENV) or DEFAULT_LEAK_SECONDS,
            grace_seconds=_env_seconds(GRACE_ENV) or DEFAULT_GRACE_SECONDS,
        )
    else:
        stacks = DumpFile(ledger_dir / f"stacks-{worker}.txt")
        dumps.append(stacks)
        seconds = _env_seconds(STACK_ENV) or DEFAULT_STACK_SECONDS
        config.pluginmanager.register(ControllerStacks(stacks, seconds), STACKS_PLUGIN)
        if not xdist_controller:
            recorder = RunRecorder(ledger)
    atexit.register(_release, dumps)
    if recorder is not None:
        config.pluginmanager.register(recorder, RECORDER_PLUGIN)
        config.pluginmanager.register(collection.CollectionRecorder(ledger.write), COLLECTION_PLUGIN)
        _ACTIVE = recorder
    host.install_kill_interceptor(ledger_dir, current_nodeid)
    if os.environ.get(ACL_PROBE_ENV) == "1":
        acl_probe.install_acl_probe(ledger_dir, current_nodeid)
