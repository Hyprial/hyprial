"""The stall dump fires on a hung shutdown and stays silent on a healthy one.

⚠️ Both directions are the point. A diagnostic that always prints is noise
nobody reads by the second week, and one that never prints is indistinguishable
from a shutdown that never stalled — which is the state we were in on
2026-08-30, when the daemon hung for nine hours and left nothing saying which
thread held it.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from hyprial.daemon.impl.processes.shutdown_stall import (
    arm_shutdown_stall_dump,
    cancel_shutdown_stall_dump,
    live_thread_summary,
)


def test_a_stalled_shutdown_dumps_every_thread(tmp_path: Path) -> None:
    """The armed watchdog fires when exit does not arrive in time.

    ⚠️ Writes to a real file: ``faulthandler`` dumps through a file descriptor,
    so an in-memory buffer makes arming fail rather than making the dump
    invisible — a fixture that cannot receive the output would have looked like
    a code defect.
    """

    target = tmp_path / "stall.txt"
    with target.open("w", encoding="utf-8") as handle:
        assert arm_shutdown_stall_dump(seconds=0.15, stream=handle)
        try:
            time.sleep(0.45)
        finally:
            cancel_shutdown_stall_dump()
    written = target.read_text(encoding="utf-8", errors="replace")
    assert "Thread" in written or "File " in written, (
        "the watchdog armed but produced no traceback; a dump that does not "
        "fire leaves exactly the evidence gap it was added to close"
    )


def test_a_healthy_shutdown_prints_nothing(tmp_path: Path) -> None:
    """Control group: disarming before the deadline must leave no output.

    Without this, a watchdog that fired unconditionally would still pass the
    test above while making every clean shutdown print a wall of stacks — and
    the first person to see that noise switches it off.
    """

    target = tmp_path / "healthy.txt"
    with target.open("w", encoding="utf-8") as handle:
        assert arm_shutdown_stall_dump(seconds=5.0, stream=handle)
        cancel_shutdown_stall_dump()
        time.sleep(0.2)
    assert target.read_text(encoding="utf-8") == "", (
        "a shutdown that finished in time still printed a traceback"
    )


def test_arming_never_raises_even_with_a_hostile_stream() -> None:
    """Failing to arm a diagnostic must not become failing to shut down.

    This runs on the shutdown path, which is where the outage it documents
    lived. Returning False is the whole contract; propagating would let a
    broken diagnostic take the daemon with it.
    """

    class _Hostile:
        def fileno(self) -> int:
            raise OSError("no descriptor here")

    assert arm_shutdown_stall_dump(seconds=1.0, stream=_Hostile()) is False
    cancel_shutdown_stall_dump()


def test_the_summary_marks_non_daemon_threads_instead_of_filtering_them() -> None:
    """⚠️ Marking, not filtering — the reason the whole module exists.

    The strongest candidate for the 2026-08-30 hang is anyio's socket selector,
    which joins without a timeout from a ``threading._register_atexit`` hook.
    That runs *before* ``threading._shutdown``, at a moment when every thread
    this codebase starts is still a daemon thread. A summary that filtered to
    non-daemon threads would print an empty list during precisely the stall it
    was added to explain.
    """

    started = threading.Event()
    release = threading.Event()

    def wait() -> None:
        started.set()
        release.wait(timeout=5.0)

    worker = threading.Thread(target=wait, name="stall-probe-daemon", daemon=True)
    worker.start()
    assert started.wait(timeout=2.0)
    try:
        rows = live_thread_summary()
        probe = [row for row in rows if "stall-probe-daemon" in row]
        assert probe, "a live daemon thread was missing from the summary"
        assert "daemon" in probe[0] and "NON-DAEMON" not in probe[0]
        assert any("NON-DAEMON" in row for row in rows), (
            "the main thread is non-daemon, so at least one row must be marked; "
            "if none is, the marking is not working and the dump reads as though "
            "nothing could ever block exit"
        )
    finally:
        release.set()
        worker.join(timeout=2.0)


def test_a_signal_makes_the_process_print_its_own_stacks(tmp_path: Path) -> None:
    """The half that does not need a stall, a person, or root.

    The timer answers "it hung for thirty seconds". This answers "tell me now",
    which is what a health check or CI can actually use. ⚠️ The alternative --
    attaching `py-spy` -- needs `task_for_pid` on macOS, an OS privilege gate
    that is identical however py-spy was installed. Asking the process to print
    its own stacks is what removes both the privilege and the human.
    """

    dump = tmp_path / "stall.log"
    program = textwrap.dedent(
        f"""
        import os, signal, threading, time
        from hyprial.daemon.impl.processes.shutdown_stall import register_stall_signal

        def _parked():
            threading.Event().wait(30)

        threading.Thread(target=_parked, name="askable", daemon=True).start()
        handle = register_stall_signal(path={str(dump)!r})
        assert handle is not None

        # ⚠️ There is deliberately no `del handle; gc.collect()` here, though
        # an earlier revision had one and it read like a strong control. It was
        # not: `faulthandler.register` keeps its own reference to the file
        # object, verified with a `weakref` that survives an explicit
        # collection, so that step could never have failed. A step that cannot
        # fail is not a control -- it is a claim wearing a control's clothes,
        # and this file spends its whole length arguing against those.
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.5)
        """
    ).strip()
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr[-800:]
    text = dump.read_text(encoding="utf-8")
    assert "source=signal" in text and "durable=no" in text, (
        "the block is unlabelled; a reader mixing signal-written and "
        f"backstop-written blocks cannot tell which one may be truncated: {text[:300]!r}"
    )
    assert "in _parked" in text, (
        "the signal was delivered but produced no stacks, so nothing is "
        f"registered on it and asking for a dump silently does nothing: {text[:600]!r}"
    )


def test_the_signal_is_one_nothing_else_in_hyprial_claims() -> None:
    """Choosing a taken signal would replace behaviour rather than add it.

    `SIGINT`, `SIGTERM` and `SIGHUP` are all used, and the first three are
    forwarded to harness children by `_FORWARDED_SIGNALS`, so a dump request
    would have reached every child as well.
    """

    from hyprial.daemon.impl.processes.shutdown_stall import STALL_DUMP_SIGNAL
    from hyprial.daemon.impl.harnesses.python_worker._exec_env_socket import _FORWARDED_SIGNALS

    assert STALL_DUMP_SIGNAL == signal.SIGUSR1
    assert STALL_DUMP_SIGNAL not in _FORWARDED_SIGNALS, (
        "the dump signal is forwarded to harness children; asking the daemon "
        "for a stack would also signal every process it started"
    )


def test_the_signal_also_writes_every_named_thread_past_faulthandlers_cap(
    tmp_path: Path,
) -> None:
    """faulthandler stops at 100 threads; the chained full dump does not.

    Production 2026-10-06: the daemon ran ~490 threads, so three SIGUSR1
    dumps never reached the PAC threads being investigated.
    """

    dump = tmp_path / "stall.log"
    program = textwrap.dedent(
        f"""
        import os, signal, threading, time
        from hyprial.daemon.impl.processes.shutdown_stall import register_stall_signal

        gate = threading.Event()

        def _parked():
            gate.wait(30)

        for index in range(130):
            threading.Thread(target=_parked, name=f"probe-{{index}}", daemon=True).start()
        assert register_stall_signal(path={str(dump)!r}) is not None
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.5)
        gate.set()
        """
    ).strip()
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr[-800:]
    text = dump.read_text(encoding="utf-8")
    assert "source=signal-full" in text, text[-600:]
    full = text.split("source=signal-full", 1)[1]
    named = {line.split("[", 1)[1].split("]", 1)[0] for line in full.splitlines() if line.startswith("Thread 0x")}
    assert {f"probe-{index}" for index in range(130)} <= named, sorted(named)[:5]
