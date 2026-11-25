"""End the process ourselves once our own teardown is done.

Allen's ruling, 2026-08-30: **do not build hyprial's graceful exit on top of anyone
else's.**

The occasion was a nine-hour outage. Teardown completed — fifty-three
registrations closed, the lock released, ``daemon.json`` handed to the
replacement — and the process then did not exit, holding a hundred and six
descendants that blocked the replacement's bootstrap. Nothing of ours was still
running; the interpreter was joining threads belonging to dependencies.

⚠️ Those threads are not all the same kind of problem, and the difference is why
"wait more politely" is not a fix:

* ``websockets/sync/server.py:285`` says it outright — daemon threads are
  refused so that open connections are not "terminated brutally". Its priority
  is the connection; ours is that the process leaves. When the peer stops
  responding those goals are simply opposed, and it chose. **We cannot fix this
  upstream and must not wait on it.**
* ``anyio``'s socket selector *does* signal its thread and wake it, then joins
  **without a timeout**, from a ``threading._register_atexit`` hook.
* The HTTP client ``mcp`` pulls in transitively starts two websocket
  background threads that never set ``daemon`` at all.  It is named indirectly
  because it is retired from this codebase's own surface and the retirement
  gate scans product source for its name as a plain substring; it still
  reaches this process through ``mcp``, and
  ``tests/test_exit_independence.py`` names it.

⇒ Only the first is a deliberate tradeoff, but the remedy is the same for all
three, because it does not depend on which: **our exit stops being their
responsibility.**

🔑 Where the exit belongs is the whole design: precisely at the moment our own
obligations end. Earlier drops work we owe. Later is the region those threads
live in — and that region is not ours to wait in.

⚠️ What this costs, stated because it is the kind of loss that is silent:
ending the process ourselves skips ``atexit``, buffered writes and ``finally``.
So anything we need on disk must be written **before** the exit, and — see
``finish_shutdown`` — flushed after the last step that produces evidence, not
merely "somewhere before exiting".

📌 This does **not** replace the force-exit backstop. That guard covers our own
teardown hanging, which this cannot: if the steps below never return, the exit
below is never reached, and the backstop is the only thing left that fires. The
two cover disjoint failures and neither is redundant.

Decision, 2026-11-16 (ownerless lifecycle loop): the same-PID loop must be able
to return after a ``logout`` stop, so the CLI cannot claim unconditional
ownership of the exit.  A serving daemon that stopped for any other reason is
the process's last act, so ownership is conditional on the *stop reason*:

* ``owns_process_exit``: always ours (embedded callers and the bootstrap
  launcher; ``daemon run`` uses ``exit_unless_logout`` on every home).
* ``exit_unless_logout``: ours unless the only stop requested was ``logout``.
  The logout reason is sticky-overridden by every other request, so this errs
  towards leaving.  Close-hang budget, stall watchdog and the SIGUSR1 dump are
  available in both modes; only the final ``os._exit`` and the lingering-thread
  grace are withheld for a pure logout, and the guards are released once the
  steps have returned so the next bootstrap phase is not killed by them.
* A logout whose teardown hangs is covered by the close-hang budget and ends
  through the same ``os._exit`` a final stop uses; the next start finds a dead
  process and a stale record and comes up in bootstrap.

Decision, 2026-11-16 (final stop from bootstrap): ``exit_unless_logout`` only
covers the serving application's ``run()``.  A final stop reached from the
bootstrap phase (never logged in, or login, logout, bootstrap) returns the loop
to the CLI with no serving application left to exit, so a non-daemon thread
carried over from the serving phase would keep the finished interpreter alive.
``leave_interpreter_after_loop`` is the CLI's last act once the loop has
returned *and* the result envelope has been emitted: flush stdout and stderr,
then leave through ``finish_shutdown`` (``EXIT_CLEAN`` for a clean loop,
``EXIT_DIRTY`` for a failed one), never a bare ``sys.exit``.  It is called only
by the real console entry (``main()`` marks the interpreter as owned); the same
command invoked in-process returns normally.  There is no timer: the exit comes
right after the flush, so the envelope can never be cut off.

Decision, 2026-11-16 (release race): a backstop whose budget expired just before
a clean logout released the guards would still pass the "still the active
timer" check and exit 75 after the logout had completed.  The release now sets
the cancel event under the backstop lock and the expiry commit re-checks the
event under the same lock, so exactly one of them wins.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

#: Exit status when every shutdown step completed.
EXIT_CLEAN = 0
#: Exit status when at least one step raised.  We still exit: a half-closed
#: daemon that stays alive is the failure this module exists to prevent, and a
#: surviving process blocks its own replacement.
EXIT_DIRTY = 1


def do_not_exit(code: int) -> None:
    """What a daemon that does not own its interpreter does instead of leaving.

    Named rather than written inline as ``lambda _: None`` so the embedded case
    is a thing the reader can see and a test can name.  The steps still run --
    an embedded daemon owes the same teardown -- and only the departure is
    withheld, because the process belongs to somebody else.
    """

    return None


def finish_shutdown(
    *,
    steps: Sequence[Callable[[], object]],
    failed: bool = False,
    exit_process: Callable[[int], object] = os._exit,
) -> tuple[BaseException, ...]:
    """Run the final shutdown steps in order, then end the process.

    ``steps`` is the ordered tail of shutdown — typically close, sweep, flush.
    Order is the caller's to decide and matters: **flush must come after the
    last step that writes anything**, because the sweep records what it stopped
    and what it spared, and those are the lines the next investigation reads.
    Flushing first exits clean while dropping exactly the account of the exit.

    Every step runs even if an earlier one raises: a failure in the sweep must
    not skip the flush, or the record of that failure is what gets lost. The
    exceptions are returned for the caller to log, and the process still exits —
    with ``EXIT_DIRTY`` — because staying alive is worse than any of them.

    ``failed`` carries a failure that happened before this shutdown tail. Such
    a failure must survive successful cleanup: cleanup completing does not turn
    the operation that required cleanup into a success.

    ``exit_process`` exists so this is testable without ending the test runner.
    Production passes ``os._exit``: it is deliberate that no ``atexit`` handler
    runs, since those handlers are where the joins we are escaping live.
    """

    errors: list[BaseException] = []
    for step in steps:
        try:
            step()
        except BaseException as error:  # noqa: BLE001 - one step must not strand the rest
            errors.append(error)
    exit_process(EXIT_DIRTY if failed or errors else EXIT_CLEAN)
    return tuple(errors)


LOGOUT_STOP_REASON = "logout"


def process_exit_owned(application: object) -> bool:
    """Whether this stop ends the process: unconditional ownership, or any non-logout stop."""

    if getattr(application, "_owns_process_exit", False):
        return True
    if not getattr(application, "_exit_unless_logout", False):
        return False
    return getattr(application, "stop_reason", None) != LOGOUT_STOP_REASON


def exit_guards_wanted(application: object) -> bool:
    """Whether the process-level guards (backstop, stall dump, SIGUSR1) apply at all."""

    return bool(
        getattr(application, "_owns_process_exit", False)
        or getattr(application, "_exit_unless_logout", False)
    )


def release_exit_guards_for_logout(application: object) -> None:
    """After a pure logout returned control to the loop, stop the timers that would hit the next phase."""

    if process_exit_owned(application) or not exit_guards_wanted(application):
        return
    cancel = getattr(application, "_exit_backstop_cancel", None)
    if cancel is not None:
        lock = getattr(application, "_exit_backstop_lock", None)
        if lock is None:
            cancel.set()
        else:
            with lock:
                cancel.set()
    from hyprial.daemon.impl.processes.shutdown_stall import cancel_shutdown_stall_dump

    cancel_shutdown_stall_dump()


def leave_interpreter_after_loop(
    *,
    failed: bool = False,
    exit_process: Callable[[int], object] = os._exit,
) -> tuple[BaseException, ...]:
    """Flush the streams the result went to, then leave like a final stop does."""

    import sys

    return finish_shutdown(
        steps=(lambda: sys.stdout.flush(), lambda: sys.stderr.flush()),
        failed=failed,
        exit_process=exit_process,
    )
