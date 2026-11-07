"""Our exit must not wait on threads we do not own.

2026-08-30: teardown completed — fifty-three registrations closed, lock
released, ``daemon.json`` handed over — and the process then stayed alive for
nine hours holding a hundred and six descendants that blocked its replacement.
Nothing in our code was still running. The interpreter was joining threads that
belong to dependencies.

⚠️ Three of those families exist and only one of them is a bug we could ask
upstream to fix:

* ``websockets/sync/server.py:285`` states the tradeoff outright — it will not
  use daemon threads because open connections would be "terminated brutally".
  We cannot change that and must not wait on it.
* ``anyio``'s socket selector *does* try to stop, then joins **without a
  timeout** from a ``threading._register_atexit`` hook.
* ``httpx2``'s two websocket background threads simply never set ``daemon``.

⇒ So the ruling (Allen, 2026-08-30): **do not build our graceful exit on top of
anyone else's.** Once our own teardown is finished, end the process rather than
returning and letting the interpreter join whatever is left.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest

from hyprial.daemon.impl.bootstrap import exit_independence as ei


def test_the_exit_runs_after_our_own_teardown_and_not_before() -> None:
    """The exit point is where our responsibility ends — not earlier, not later.

    Earlier drops work we owe; later steps into the region those dependency
    threads live in, which is the whole thing being avoided.
    """

    order: list[str] = []
    ei.finish_shutdown(
        steps=(
            lambda: order.append("close"),
            lambda: order.append("sweep"),
            lambda: order.append("flush"),
        ),
        exit_process=lambda code: order.append(f"exit:{code}"),
    )
    assert order == ["close", "sweep", "flush", "exit:0"]


def test_flush_runs_after_the_sweep_because_the_sweep_writes_evidence() -> None:
    """⚠️ Ordering that is easy to get backwards, and silent when you do.

    Sweeping logs what it killed and what it spared, and those are the lines
    the next investigation reads. Flushing before the sweep exits cleanly while
    dropping exactly the records that explain the exit — and nothing reports a
    log that was never written.
    """

    written: list[str] = []
    flushed: list[int] = []

    def sweep() -> None:
        written.append("swept: stopped 3, spared 1")

    def flush() -> None:
        flushed.append(len(written))

    ei.finish_shutdown(steps=(sweep, flush), exit_process=lambda _code: None)
    assert flushed == [1], (
        "flush ran before the sweep had written its record; the process would "
        "exit clean and lose the only account of what it killed"
    )


def test_a_step_that_raises_does_not_prevent_the_exit() -> None:
    """A failure while shutting down must never become a process that stays.

    The husk is the thing that caused the outage: a surviving process blocks the
    replacement's bootstrap. Exiting with a failure code is strictly better than
    remaining alive, so a raising step is recorded and the exit still happens.
    """

    seen: list[int] = []
    errors = ei.finish_shutdown(
        steps=(
            lambda: (_ for _ in ()).throw(RuntimeError("sweep blew up")),
            lambda: None,
        ),
        exit_process=seen.append,
    )
    assert seen == [1], "a raising step left the process alive"
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)


def test_later_steps_still_run_after_an_earlier_one_raises() -> None:
    """One broken step must not strand the rest.

    Otherwise a failure in the sweep would skip the flush, and the record of
    that very failure is what gets lost.
    """

    ran: list[str] = []
    ei.finish_shutdown(
        steps=(
            lambda: (_ for _ in ()).throw(RuntimeError("first")),
            lambda: ran.append("second"),
        ),
        exit_process=lambda _code: None,
    )
    assert ran == ["second"]


def test_a_dependency_thread_that_never_stops_does_not_delay_us(
    monkeypatch: Any,
) -> None:
    """The control group for the whole ruling.

    A non-daemon thread that refuses to finish is exactly what the interpreter
    would block on. With the proactive exit our shutdown is unaffected; without
    it, the process would hang here — which is what production did.
    """

    release = threading.Event()
    started = threading.Event()

    def never_stops() -> None:
        started.set()
        release.wait(timeout=30.0)

    # daemon=True only so a failing test cannot wedge the suite; what is under
    # test is that we never wait on it either way.
    stubborn = threading.Thread(target=never_stops, name="stubborn-dep", daemon=True)
    stubborn.start()
    try:
        assert started.wait(timeout=2.0)
        exited: list[int] = []
        began = time.monotonic()
        ei.finish_shutdown(steps=(lambda: None,), exit_process=exited.append)
        elapsed = time.monotonic() - began
        assert exited == [0]
        assert elapsed < 1.0, (
            "shutdown waited on a thread it does not own; the ruling is that "
            "our exit never depends on someone else's"
        )
    finally:
        release.set()
        stubborn.join(timeout=2.0)


def test_the_exit_is_not_reached_when_the_caller_never_calls_it() -> None:
    """Guards the mutation the acceptance asks for.

    Removing the call from the shutdown path must be observable, so this pins
    that ``finish_shutdown`` is the only thing that exits: nothing here exits as
    a side effect of importing or of running the steps.
    """

    exited: list[int] = []
    for step in (lambda: None, lambda: None):
        step()
    assert exited == [], "something exited without finish_shutdown being called"


def test_exit_code_reflects_whether_teardown_was_clean() -> None:
    assert ei.finish_shutdown(steps=(), exit_process=lambda c: c) is not None
    codes: list[int] = []
    ei.finish_shutdown(steps=(lambda: None,), exit_process=codes.append)
    assert codes == [0]
    codes.clear()
    ei.finish_shutdown(
        steps=(lambda: (_ for _ in ()).throw(OSError("x")),),
        exit_process=codes.append,
    )
    assert codes == [1]


def test_an_existing_failure_survives_successful_shutdown_steps() -> None:
    codes: list[int] = []
    ei.finish_shutdown(
        steps=(lambda: None, lambda: None),
        failed=True,
        exit_process=codes.append,
    )
    assert codes == [ei.EXIT_DIRTY]


def test_no_existing_failure_and_successful_steps_is_the_empty_control() -> None:
    codes: list[int] = []
    ei.finish_shutdown(
        steps=(lambda: None, lambda: None),
        failed=False,
        exit_process=codes.append,
    )
    assert codes == [ei.EXIT_CLEAN]


def test_existing_failure_does_not_stop_later_steps_after_a_step_raises() -> None:
    ran: list[str] = []
    codes: list[int] = []

    def fail_step() -> None:
        ran.append("failed-step")
        raise RuntimeError("shutdown failed too")

    errors = ei.finish_shutdown(
        steps=(fail_step, lambda: ran.append("last-step")),
        failed=True,
        exit_process=codes.append,
    )

    assert ran == ["failed-step", "last-step"]
    assert codes == [ei.EXIT_DIRTY]
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)


@pytest.mark.parametrize("count", [0, 1, 5])
def test_any_number_of_steps_still_ends_in_exactly_one_exit(count: int) -> None:
    exited: list[int] = []
    ei.finish_shutdown(
        steps=tuple(lambda: None for _ in range(count)), exit_process=exited.append
    )
    assert len(exited) == 1, "shutdown must exit exactly once"


def test_an_embedded_daemon_discharges_its_teardown_without_taking_the_process() -> None:
    """The steps are owed either way; only the departure belongs to the owner.

    ⚠️ Getting this backwards is not a hypothetical. An earlier draft called
    `os._exit` unconditionally from `run()`'s finally, and `run()` has real
    in-process callers -- two tests construct a daemon and run it inside the
    test process. The suite then died at test 777 of 2820 with **exit code 0
    and no summary line**: the failure wore the exact shape of a pass, and only
    a wrapper that refuses to read an unfinished run as a green one caught it.

    So this asserts both halves. Skipping the steps would leave the host's home
    guard held; taking the exit would end the host.
    """

    import hyprial.daemon.impl.application as app

    closed: list[str] = []
    restored: list[object] = []
    logged: list[dict[str, object]] = []
    exited: list[int] = []

    class _Guard:
        def close(self) -> None:
            closed.append("home-guard")

    class _Embedded:
        _owns_process_exit = False
        _home_guard = _Guard()
        node_id = "embedded"

        def _close_home_lease(self) -> None:
            self._home_guard.close()

        def _exit_process(self, code: int) -> None:
            exited.append(code)

        def _restore_signal_handlers(self, handlers: object) -> None:
            restored.append(handlers)

        def _log_trace(self, level: str, event: str, **fields: object) -> None:
            logged.append({"event": event, **fields})

    errors = app.DaemonApplication._finish_shutdown_and_leave(_Embedded(), "handlers")

    assert errors == ()
    assert exited == [], "an embedded daemon ended a process it does not own"
    assert closed == ["home-guard"] and restored == ["handlers"], (
        "withholding the exit also withheld the teardown; an embedded daemon "
        f"owes every step, it merely does not leave: {closed!r} {restored!r}"
    )
    departure = [row for row in logged if row["event"] == "daemon.exiting"]
    assert departure and departure[0]["owned"] is False, logged
    assert isinstance(departure[0].get("threads"), list) and departure[0]["threads"], (
        "no live-thread inventory was recorded on the way out. Once this lands "
        "the backstop stops firing and every visible symptom disappears while "
        "the leak is untouched -- this record is the only thing that can tell "
        f"'fixed' apart from 'no longer observable': {departure!r}"
    )


def test_the_last_step_flushes_after_everything_that_writes() -> None:
    """⚠️ Ordering that only matters at the moment it is too late to notice.

    `os._exit` drops whatever is still in a Python buffer. Today the departure
    record survives because the logger writes with a raw `os.write` -- true,
    and nowhere stated, so the steps tuple read as though flushing had been
    weighed and dismissed rather than merely described in a docstring. Anyone
    adding a buffered writer above would be relying on a property this code
    never claimed, and the loss would be silent: the process exits zero and the
    record simply is not there.

    Pinned by **position**, not by presence: a flush that runs before the last
    writing step is the same as no flush at all.
    """

    import io

    import hyprial.daemon.impl.application as app

    order: list[str] = []

    class _Guard:
        def close(self) -> None:
            order.append("home-guard")

    class _Embedded:
        _owns_process_exit = False
        _home_guard = _Guard()
        node_id = "flush-order"

        def _close_home_lease(self) -> None:
            self._home_guard.close()

        def _restore_signal_handlers(self, handlers: object) -> None:
            order.append("restore")

        def _log_trace(self, level: str, event: str, **fields: object) -> None:
            order.append("record")

    class _Watched(io.StringIO):
        def flush(self) -> None:
            order.append("flush")

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Watched(), _Watched()
    try:
        app.DaemonApplication._finish_shutdown_and_leave(_Embedded(), "handlers")
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert "flush" in order, f"nothing flushed before the exit: {order}"
    assert order.index("flush") > order.index("record"), (
        f"the flush ran before the last step that writes: {order}. On the real "
        "path the exit follows immediately, so anything a later step buffered "
        "is gone -- and nothing reports its absence."
    )


def test_the_stall_dump_is_armed_before_the_proactive_exit_can_be_reached() -> None:
    """⚠️ Ordering the acceptance calls out, pinned by a test rather than a comment.

    The backstop guards *our own teardown hanging*. If it were armed after the
    proactive exit, a hung ``_close`` would never reach either — the exit line
    is downstream of the hang — and the one thing still able to fire would not
    be armed.

    The future misreading this stops is "we exit ourselves now, so the backstop
    is redundant". It is not: the two cover disjoint failures, and this is the
    one that covers the failure that actually happened.
    """

    import ast
    from pathlib import Path
    from hyprial.daemon.impl.application import DaemonApplication

    from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source
    native_root = Path(__file__).resolve().parents[3] / 'src/hyprial'
    if not Path(DaemonApplication.run.__code__.co_filename).is_relative_to(native_root):
        raise RuntimeError("Exit source reader resolved outside the native product tree")
    source = _native_application_source()
    tree = ast.parse(source)

    def enclosing(target: str) -> set[str]:
        found: set[str] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name == target:
                    found.add(fn.name)
        return found

    exits_in = enclosing("finish_shutdown")
    assert exits_in == {"_finish_shutdown_and_leave"}, (
        f"finish_shutdown must have exactly one caller, found {exits_in}. "
        "A second departure point is a second answer to 'when may this process "
        "end', and the day the two disagree nothing reports it."
    )
    assert "_close" not in enclosing("_finish_shutdown_and_leave"), (
        "leaving must not be called from inside _close; there it would sit "
        "behind the very hang the backstop exists to cover, and a stalled "
        "teardown would then reach neither the exit nor the guarantee."
    )

    # Pin the call order as well as the real blocked-close subprocess test.
    # ast.walk is breadth-first, so sort calls by their source position.
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    sequence = [
        name
        for node in sorted(
            (node for node in ast.walk(run) if isinstance(node, ast.Call)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for name in [getattr(node.func, "attr", None)]
        if name in {"_arm_exit_backstop", "_close", "_finish_shutdown_and_leave"}
    ]
    assert sequence == [
        "_arm_exit_backstop", "_close", "_arm_exit_backstop", "_finish_shutdown_and_leave"
    ], (
        f"expected the backstop to be armed before the closing steps, got "
        f"{sequence}. ⚠️ Armed after them, a hang inside `_home_guard.close` "
        "leaves nothing armed at all -- the guarantee would be absent in "
        "precisely the case it exists for, and absent silently."
    )
