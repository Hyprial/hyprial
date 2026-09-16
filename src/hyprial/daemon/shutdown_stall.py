"""Make a hung shutdown leave its own evidence behind.

On 2026-08-30 the daemon completed its teardown — fifty-three registrations
closed, lock released, ``daemon.json`` handed to the replacement — and then did
not exit.  It stayed alive nine hours holding a hundred and six descendants that
blocked the replacement's bootstrap.  Nobody knows which thread was blocking,
because by the time anyone looked the only way to find out was to catch the next
occurrence with a debugger attached, and ``autoupdate`` runs twice a day.

⚠️ This module fixes nothing.  It only removes the requirement that a person be
present and prepared at the moment it happens.

**Why every thread, not only the non-daemon ones.**  The obvious version prints
non-daemon threads, since those are what ``threading._shutdown`` joins.  It
would have missed the strongest candidate found so far: ``anyio``'s socket
selector registers ``_stop`` through ``threading._register_atexit`` and then
joins **without a timeout** — that runs *before* ``_shutdown``, at a point where
every thread this codebase starts is still a daemon thread.  So a non-daemon
filter would print an empty-looking list during exactly the stall it was added
to explain.

⇒ Dump everything and let the reader classify. A dump that can only confirm one
of the two mechanisms cannot distinguish them, and telling them apart is the
whole reason for dumping.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading

#: How long teardown may take before the stall is assumed real.  Deliberately
#: well past a healthy close (which is sub-second) and past the CLI's own ten
#: second teardown receipt, so a slow-but-finishing shutdown never dumps.
STALL_SECONDS = 30.0

_ENV_DISABLE = "HYPRIAL_NO_SHUTDOWN_STALL_DUMP"


def arm_shutdown_stall_dump(
    *, seconds: float = STALL_SECONDS, stream: object | None = None
) -> bool:
    """Arm a one-shot traceback dump that fires only if exit stalls.

    Returns whether it was armed.  ``faulthandler.dump_traceback_later`` runs a
    watchdog that prints **every** thread's stack and then lets the process
    continue; if the interpreter exits first the timer simply never fires, so a
    healthy shutdown produces nothing at all.

    ⚠️ Deliberately never raises. This is diagnostic scaffolding on the shutdown
    path, and the shutdown path is where this month's outage lived — a failure
    to arm a diagnostic must never become a failure to shut down.
    """

    if os.environ.get(_ENV_DISABLE):
        return False
    try:
        target = stream if stream is not None else sys.stderr
        faulthandler.dump_traceback_later(
            seconds,
            repeat=False,
            file=target,  # type: ignore[arg-type]
            exit=False,
        )
    except BaseException:
        return False
    return True


#: Where a forced exit leaves its stacks.  A fixed name under the state dir,
#: deliberately **not** stderr: a production daemon's stderr is a per-launch
#: capture file, and the next launch replaces it -- see ``dump_live_threads``.
STALL_DUMP_FILENAME = "shutdown-stall.log"


def stall_dump_path(state_dir: object) -> object:
    """The stall dump's location, so callers and tests agree on one name."""

    from pathlib import Path

    return Path(str(state_dir)) / STALL_DUMP_FILENAME


def dump_live_threads(
    *, stream: object | None = None, path: object | None = None
) -> bool:
    """Print every thread's stack **now**, rather than on a timer.

    The armed watchdog above covers ``_close()`` itself hanging.  This covers
    the other case, and it is the one production actually shows: ``_close()``
    returns, the interpreter then parks on a join, and the exit backstop forces
    the process out fifteen seconds later.  A timer set for thirty never
    reaches that -- the process is already gone -- so the two need two separate
    triggers, and neither is a substitute for the other.

    ⚠️ Taking the evidence at the moment the backstop fires is what removes the
    timing assumption entirely.  That moment is, by definition, "teardown
    finished and the process is still here", which is precisely the state worth
    photographing.  Racing a second timer against the backstop's fifteen
    seconds would put two unrelated constants in two files in a relationship
    nothing asserts -- and when they drift, the tool goes quiet rather than
    wrong, which is the failure nobody notices.

    ⚠️ ``path`` exists because stderr is the wrong place, and the way it is
    wrong is worth stating.  A production daemon's stderr points at a
    **per-launch** capture file: ``cli._daemon_launch_capture`` creates a new
    inode for every start and moves the stable name onto it.  The stall this
    dumps for happens *during a restart* -- so the very next launch, seconds
    later, replaces the file the stacks were just written into.  On 2026-08-30
    a real forced exit wrote one hundred and ninety-one thread stacks and by
    the time anyone looked the file held fifty-three bytes of the next launch's
    marker.  The diagnostic reached the scene, recorded everything, and the
    record was erased by the event that made it necessary.

    So the stacks go to a fixed name of our own, flushed and fsynced because
    ``os._exit`` follows within milliseconds and would drop a buffer.

    Never raises, for the same reason as ``arm_shutdown_stall_dump``.
    """

    try:
        if path is not None:
            with open(str(path), "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n===== source=backstop durable=yes pid={os.getpid()} "
                    f"threads={threading.active_count()} =====\n"
                )
                handle.flush()
                faulthandler.dump_traceback(file=handle, all_threads=True)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        target = stream if stream is not None else sys.stderr
        faulthandler.dump_traceback(file=target, all_threads=True)  # type: ignore[arg-type]
    except BaseException:
        return False
    return True


#: Asking for a dump from outside, at any moment.  `SIGUSR1` because nothing
#: in this codebase uses it (`SIGINT`, `SIGTERM`, `SIGHUP` and `SIGKILL` are
#: taken, and `_FORWARDED_SIGNALS` passes the first three down to harness
#: children -- `SIGUSR1` reaches only the daemon).  `SIGUSR2` is left free for
#: the reload-style convention.
STALL_DUMP_SIGNAL = getattr(signal, "SIGUSR1", None)

_signal_dump_handle: object | None = None


def register_stall_signal(
    *, path: object, signum: int | None = None
) -> object | None:
    """Let anything ask this process for its stacks, with no privileges.

    The timer covers "it hung for thirty seconds"; this covers "tell me right
    now" -- a health check, CI, or a person, at any moment, without waiting for
    a stall and without `sudo`.  That last part is the point: reading another
    process's memory with `py-spy` needs `task_for_pid`, which is an OS
    privilege gate and is unaffected by how py-spy was installed.  Having the
    process print its own stacks is what removes the human from the loop.

    A module-level reference to the handle is kept, but **not** for the reason
    it looks like.  The obvious worry is that dropping the last reference would
    close the file and silently break the registration -- and that "never
    registered" and "registered, nobody signalled" are indistinguishable.  That
    worry was checked rather than assumed: `faulthandler.register` holds its
    own reference to the file object (a `weakref` to it survives an explicit
    `gc.collect()`, and the fd stays valid), so the hazard does not exist on
    this interpreter.  The reference below is kept only so the guarantee is
    ours rather than an undocumented detail of CPython's -- it is belt and
    braces, and no test can prove it is doing anything.

    ⚠️ What this deliberately does **not** promise is durability.  It runs in a
    signal handler, so it must stay async-signal-safe: `faulthandler` writes
    with raw `write()` and there is no `fsync` here, unlike the backstop path,
    which is a normal call site moments before `os._exit`.  Both write to the
    same file; only one of them can promise the bytes survived a crash.  The
    header line records which wrote each block, because a reader who cannot
    tell them apart will treat the weaker one as if it were the stronger.

    Never raises: it is armed on the startup path, and a diagnostic that fails
    to arm must not become a daemon that fails to start.
    """

    global _signal_dump_handle

    target = STALL_DUMP_SIGNAL if signum is None else signum
    if target is None:  # platform without SIGUSR1
        return None
    try:
        handle = open(str(path), "a", encoding="utf-8", buffering=1)
        handle.write(
            f"\n===== source=signal durable=no pid={os.getpid()} "
            f"armed on signal {target} =====\n"
        )
        handle.flush()
        faulthandler.register(target, file=handle, all_threads=True, chain=False)
    except BaseException:
        return None
    _signal_dump_handle = handle
    return handle


def cancel_shutdown_stall_dump() -> None:
    """Disarm the watchdog; safe to call when it was never armed."""

    try:
        faulthandler.cancel_dump_traceback_later()
    except BaseException:
        pass


def live_thread_summary() -> tuple[str, ...]:
    """One line per living thread, non-daemon ones marked.

    The dump above is the primary evidence; this is the readable index beside
    it, because a traceback of two hundred threads is not something an operator
    reads at three in the morning. ⚠️ It marks rather than filters, for the same
    reason the dump is unfiltered.
    """

    rows: list[str] = []
    for thread in threading.enumerate():
        kind = "daemon" if thread.daemon else "NON-DAEMON"
        rows.append(f"{kind:>10}  {thread.name}  alive={thread.is_alive()}")
    return tuple(sorted(rows))
