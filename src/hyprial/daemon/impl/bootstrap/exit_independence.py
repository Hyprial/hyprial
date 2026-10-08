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
