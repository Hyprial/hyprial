"""Closing the effect store under a live actor segfaults the worker.

``ReactionEffectsRuntime.close`` asked the actor to stop and then closed the
sqlite connection whether or not it had.  The connection is opened with
``check_same_thread=False``, so nothing above the C layer objects when the
actor thread is still inside ``pending()``'s ``execute()`` -- the process just
dies.  Not the adapter: the whole worker, with SIGSEGV.

That is not hypothetical and not test-only.  ``stop`` is handed whatever is
left of the close budget, and ``wait_idle`` runs first and can spend all of
it -- which is what happens whenever a Lark reaction call has not come back
yet, the ordinary state of an adapter being shut down.  ``worker.py`` closes
with the 5.0s default and ``adapter.py`` passes its own shutdown timeout, so
both production paths reach ``stop`` with ~0 seconds exactly as this test does
with 0.0.  CI task 2713 died this way; the interleaved measurement was 17
crashes in 100 runs before the fix and 0 in 100 after.

The tests below assert the property rather than the crash.  A test that had to
segfault to fail could not report its own failure, and would take the rest of
the suite with it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ReactionEffectAdmission,
    ReactionEffectsRuntime,
)
def _noop(_native_message_id: str) -> None:
    return None


def test_close_leaves_the_store_open_when_the_actor_would_not_stop(
    tmp_path: Path,
) -> None:
    """The store must survive a close that could not stop the actor.

    Reaching into ``_store.connection`` is deliberate: the defect is about
    that exact object's lifetime, and every public accessor opens a
    *different* connection (``snapshot`` does), so nothing public can tell a
    live connection from a closed one. Asserting the thing that actually
    crashed beats asserting a proxy for it.
    """

    reports: list[dict[str, object]] = []
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_effect(_native_message_id: str) -> None:
        entered.set()
        release.wait(10.0)
        finished.set()

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=blocking_effect,
        reply_effect=_noop,
        overflow_report=reports.append,
    )
    try:
        assert runtime.ack("om-live-actor") is ReactionEffectAdmission.ACCEPTED
        assert entered.wait(5.0), (
            "the effect never started, so the actor was never busy and this "
            "test would pass without exercising anything"
        )

        # Budget 0 is what production arrives at once wait_idle has spent it.
        runtime.close(0.0)

        # The connection carries a Row factory, so compare the column rather
        # than the row object. What is being asserted is that the query runs
        # at all: a closed handle raises instead.
        assert runtime._store.connection.execute("SELECT 1").fetchone()[0] == 1, (
            "close() closed the effect store while the actor was still "
            "running; the actor thread can be inside pending()'s execute(), "
            "and with check_same_thread=False that is a use-after-free that "
            "takes the whole worker process down with SIGSEGV"
        )
        assert [
            report
            for report in reports
            if report.get("event") == "lark.reaction-effect.close-abandoned"
        ], (
            "the store was left open but nothing said so. An actor that "
            "refused to stop is the half worth knowing about, and before this "
            "fix the segfault was the only way anyone found out: silence here "
            "trades a loud wrong behaviour for a quiet one"
        )
    finally:
        release.set()
        # The abandoned close leaks the connection on purpose -- that is the
        # whole point -- and the suite's fd-hygiene fixture is right to notice
        # three sqlite descriptors going missing. Production leaks them for
        # the milliseconds it has left to live; a test lives inside a long
        # run, so it cleans up after the actor is genuinely done.
        finished.wait(10.0)
        runtime._store.close()


def test_a_close_that_did_stop_the_actor_still_closes_the_store(
    tmp_path: Path,
) -> None:
    """The control, without which the fix could be "never close anything".

    A close() that simply stopped closing the store would pass the test above
    and leak a connection on every clean shutdown. This pins the other
    direction: when the actor really did stop, the store really does close.
    """

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=_noop,
        reply_effect=_noop,
    )
    assert runtime.ack("om-quiet") is ReactionEffectAdmission.ACCEPTED
    assert runtime.wait_idle(5.0), "the lane never drained; the control is void"

    runtime.close(5.0)

    try:
        runtime._store.connection.execute("SELECT 1")
    except Exception as error:  # sqlite3.ProgrammingError on a closed handle
        assert "closed" in str(error).lower(), error
    else:
        raise AssertionError(
            "close() left the effect store open after a clean stop; the fix "
            "for the live-actor case must not become 'never close the store'"
        )
