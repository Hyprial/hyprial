"""The mail-collection watchdog: speak before the deadline, and at it.

2026-09-18 is the case these tests encode: an actor stopped fetching at
03:00, kept merging pull requests for an hour, and eleven messages addressed
to it expired unread with nothing reported.  Both halves of that silence are
covered -- "nobody has taken this yet" (still recoverable) and "the sweep
threw it away" (not).

The sharpest tests here are the two that must stay QUIET, because an actor
can hold its mail in two different ways and each one looks like silence from
outside: a pull-path actor stamps ``fetched_at_ms``, while a streaming worker
leaves only the ``refresh_hold`` fingerprint on ``expires_at_ms``.  Alarming
on either would report a healthy busy worker -- and the streaming half was
found by reading a production timeline, not by a failing test.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService
from hyprial.daemon.impl.inbox import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.tracking.watchdog import (
    REPEAT_AFTER_MS,
    UNFETCHED_ALERT_MS,
    InboxWatchdog,
)

RUNNING = "agent:o:n:busy-worker"
STOPPED = "agent:o:n:retired-worker"
NOW = 10_000_000


def _native_watchdog_source() -> str:
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.messaging.maintenance import _MaintenanceMixin

    assert DaemonApplication._watch_inbox_collection is _MaintenanceMixin._watch_inbox_collection
    assert DaemonApplication._on_dispatch_tick_completed is _MaintenanceMixin._on_dispatch_tick_completed
    return inspect.getsource(_MaintenanceMixin)


class Sink:
    def __init__(self, *, accept: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.accept = accept

    def __call__(self, key: str, text: str) -> bool:
        self.sent.append((key, text))
        return self.accept


def make(tmp_path: Path, *, accept: bool = True, now: int = NOW):
    clock = {"ms": now}
    sink = Sink(accept=accept)
    watchdog = InboxWatchdog(
        state_dir=tmp_path, deliver=sink, clock_ms=lambda: clock["ms"]
    )
    return watchdog, sink, clock


def running_only(recipient: str) -> bool:
    return recipient == RUNNING


# ── before the deadline ────────────────────────────────────────────────────


def test_a_running_actor_that_has_not_collected_its_mail_is_reported(
    tmp_path: Path,
) -> None:
    watchdog, sink, _ = make(tmp_path)
    stats = [(RUNNING, 5, NOW - UNFETCHED_ALERT_MS - 1)]

    [alert] = watchdog.observe_unfetched(stats, is_running=running_only)

    assert alert.kind == "unfetched"
    assert RUNNING in alert.text
    assert "5" in alert.text
    # It must say the mail is still readable: that is the whole reason this
    # fires at half the TTL instead of at the sweep.
    assert "还读得到" in alert.text
    assert len(sink.sent) == 1


def test_mail_younger_than_the_threshold_is_not_reported(tmp_path: Path) -> None:
    """Every actor has unfetched mail for a moment; that is not a fault."""

    watchdog, sink, _ = make(tmp_path)
    stats = [(RUNNING, 1, NOW - UNFETCHED_ALERT_MS + 1_000)]

    assert watchdog.observe_unfetched(stats, is_running=running_only) == []
    assert sink.sent == []


def test_an_actor_that_is_not_running_is_not_reported(tmp_path: Path) -> None:
    """An offline actor's mailbox filling up is what a mailbox is FOR.

    Reporting it would bury the one case that matters -- a reader that is
    alive and not listening -- under every stopped worker on the node.
    """

    watchdog, sink, _ = make(tmp_path)
    stats = [(STOPPED, 9, NOW - UNFETCHED_ALERT_MS * 4)]

    assert watchdog.observe_unfetched(stats, is_running=running_only) == []
    assert sink.sent == []


def test_the_same_stuck_actor_is_not_reported_on_every_tick(tmp_path: Path) -> None:
    watchdog, sink, clock = make(tmp_path)
    stats = [(RUNNING, 5, NOW - UNFETCHED_ALERT_MS - 1)]
    assert len(watchdog.observe_unfetched(stats, is_running=running_only)) == 1

    clock["ms"] += REPEAT_AFTER_MS - 1
    assert watchdog.observe_unfetched(stats, is_running=running_only) == []

    clock["ms"] += 2
    assert len(watchdog.observe_unfetched(stats, is_running=running_only)) == 1
    assert len(sink.sent) == 2


def test_a_refused_alert_is_not_recorded_as_sent(tmp_path: Path) -> None:
    """Squire being down must not consume the one notification."""

    watchdog, sink, _ = make(tmp_path, accept=False)
    stats = [(RUNNING, 5, NOW - UNFETCHED_ALERT_MS - 1)]

    assert watchdog.observe_unfetched(stats, is_running=running_only) == []
    assert len(sink.sent) == 1  # attempted

    watchdog._deliver = Sink()  # type: ignore[assignment]
    assert len(watchdog.observe_unfetched(stats, is_running=running_only)) == 1


def test_the_de_duplication_survives_a_restart(tmp_path: Path) -> None:
    watchdog, _, _ = make(tmp_path)
    stats = [(RUNNING, 5, NOW - UNFETCHED_ALERT_MS - 1)]
    assert len(watchdog.observe_unfetched(stats, is_running=running_only)) == 1

    revived, sink, _ = make(tmp_path)
    assert revived.observe_unfetched(stats, is_running=running_only) == []
    assert sink.sent == []


# ── at the deadline ────────────────────────────────────────────────────────


def test_mail_thrown_away_under_a_running_reader_is_reported(tmp_path: Path) -> None:
    watchdog, sink, _ = make(tmp_path)

    [alert] = watchdog.observe_pruned(
        [(RUNNING, "m-1"), (RUNNING, "m-2"), (STOPPED, "m-3")],
        is_running=running_only,
    )

    assert alert.kind == "pruned"
    assert "2" in alert.text and "m-1" in alert.text
    # The sender's side still reads "delivered"; say so, because that is what
    # makes the loss invisible from both ends.
    assert "已投递" in alert.text
    assert len(sink.sent) == 1


def test_mail_thrown_away_for_a_stopped_actor_is_silent(tmp_path: Path) -> None:
    """That population is exactly what the sweep exists to reap."""

    watchdog, sink, _ = make(tmp_path)
    assert watchdog.observe_pruned([(STOPPED, "m-1")], is_running=running_only) == []
    assert sink.sent == []


# ── the signal itself: fetched-but-unconsumed is NOT uncollected ───────────


def _message(message_id: str, recipient: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c",
        sender="agent:o:n:sender",
        recipient=recipient,
        payload=b"body",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_a_fetched_message_leaves_the_uncollected_reading(tmp_path: Path) -> None:
    """The discriminator between "deaf" and "busy", at the query.

    An actor in a long turn has FETCHED its mail and not consumed it.  It
    stays in ``pending_recipient_stats`` (which counts ``consumed = 0``) --
    which is why that reading, the one ``hyprial agent top`` shows, would report a
    healthy busy worker as stuck.  Fetching must empty the uncollected
    reading, and only consuming empties the pending one.
    """

    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        service.receive(_message("m-1", RUNNING), now_ms=1)
        assert [row[0] for row in service.unfetched_recipient_stats()] == [RUNNING]

        service.fetch_pending(RUNNING, now_ms=2)

        assert service.unfetched_recipient_stats() == ()
        assert [row[0] for row in service.pending_recipient_stats()] == [RUNNING]
    finally:
        service.close()


def test_a_delivery_queued_to_a_streaming_worker_is_not_uncollected(
    tmp_path: Path,
) -> None:
    """The second way to take responsibility -- and the one that bites.

    A streaming worker never touches ``fetched_at_ms``: the daemon calls
    ``refresh_hold`` when the worker ACCEPTS the delivery into its queue
    (#276), which is the only trace that path leaves.  Keying solely on
    ``fetched_at_ms IS NULL`` would therefore report every healthy streaming
    worker holding a queued message as "not collecting its mail" -- the
    alarm would fire loudest on the normal case.

    Found by reading the 02:05 -> 02:35 timeline of message b13f3ca7, not by
    a failing test: both spellings of "taken" look identical from outside.
    """

    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        service.receive(_message("m-queued", RUNNING), now_ms=1)
        assert [row[0] for row in service.unfetched_recipient_stats()] == [RUNNING]

        # what the daemon does the moment a streaming worker accepts it
        assert service.refresh_hold("m-queued", now_ms=1_000)

        assert service.unfetched_recipient_stats() == ()
        # It is still pending -- the worker has it, nobody has consumed it.
        assert [row[0] for row in service.pending_recipient_stats()] == [RUNNING]
    finally:
        service.close()


# ── composition ────────────────────────────────────────────────────────────


def test_the_daemon_feeds_both_readings_to_the_watchdog() -> None:
    """Pin the wiring: the hook has no other observer, so a refactor that
    dropped it would leave every test above green while the daemon went
    silent again -- the exact failure this whole file is about."""

    # This actual mixin owns both the dispatch-completion callsite and the hook.
    source = _native_watchdog_source()
    tree = ast.parse(source.lstrip())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_watch_inbox_collection" in called

    [hook] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_watch_inbox_collection"
    ]
    inner = {
        node.func.attr
        for node in ast.walk(hook)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"observe_pruned", "observe_unfetched"} <= inner
