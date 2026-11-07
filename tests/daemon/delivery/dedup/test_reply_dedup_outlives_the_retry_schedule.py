"""Exactly-one reply per delivery rests on a *time margin*, not on absolutes.

A mechanism answer (``_answer_hook_request`` / ``_answer_mechanism_result``;
the old automatic ``_reply_and_ack`` is gone) becomes at most one reply because
it submits under a deterministic idempotency key (``reply:<message_id>``) and the
recipient's ``InboxService.receive`` refuses a delivery carrying a key it has
already seen.  (The check is on the receiving side, not on ``submit`` -- worth
saying because the sending call is the one that looks like it would do it.)  That refusal
is not permanent: dedup rows expire after ``dedup_window_ms``.  So the
guarantee is "at most one, provided every retry that could resubmit happens
inside the window" -- and today it holds only because the failure-retry
schedule spans seconds while the window spans a day.

Nothing enforces that relationship.  Someone tuning the retry schedule --
a plausible change, made in ``daemon/runtime.py``, for reasons that have
nothing to do with deduplication -- can widen it past the window, and the
guarantee then fails silently: no error, no log, just a second reply where
the design promised one.  A comment does not reach that person; they are not
reading ``inbox/service.py``.  These tests are here to be the thing that
reaches them.

⚠️ The arithmetic assertion alone would be a tautology about two constants.
It is paired with the behaviour it stands for -- a resubmission inside the
window is refused, one outside it is not -- so the number has a demonstrated
meaning rather than an assumed one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.runtime import HARNESS_FAILURE_BACKOFF_MS, HARNESS_FAILURE_MAX_ATTEMPTS
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService

RECIPIENT = "agent:owner:node:worker"
SENDER = "agent:owner:node:sender"

#: How much slack the relationship must keep.  Not a tuning knob -- it is the
#: statement "these live on different orders of magnitude".  A change that
#: brings them within 100x of each other is one worth stopping to look at,
#: whichever direction it came from.
REQUIRED_MARGIN = 100


@pytest.fixture
def inbox(tmp_path: Path) -> Iterator[InboxService]:
    """A real inbox that closes its sqlite handles.

    Leaving them open costs three descriptors per test (db, -wal, -shm), which
    the suite's hygiene guard reports as a leak -- correctly: at this scale
    "a few per test" is what walks a long run into macOS's descriptor ceiling.
    """

    # node_id must match the recipient URI, or the submission is a remote
    # delivery routed to the outbox and never lands in this local inbox.
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node"
    )
    try:
        yield service
    finally:
        service.close()


def _reply(message_id: str, *, idempotency_key: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation",
        sender=SENDER,
        recipient=RECIPIENT,
        payload=b'{"message":"the reply"}',
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=idempotency_key,
        created_at_ms=0,
    )


def test_the_retry_schedule_stays_far_inside_the_dedup_window() -> None:
    """Pin the relationship the exactly-once guarantee actually rests on.

    The failure message is the point of this test.  Whoever trips it is
    changing a retry schedule, not thinking about deduplication, so the red
    has to explain the coupling rather than just report two numbers.
    """

    worst_case_retry_span_ms = HARNESS_FAILURE_MAX_ATTEMPTS * max(
        HARNESS_FAILURE_BACKOFF_MS
    )
    window_ms = InboxService.dedup_window_ms

    assert worst_case_retry_span_ms * REQUIRED_MARGIN <= window_ms, (
        "the harness failure retry schedule has grown close to the inbox dedup "
        f"window ({worst_case_retry_span_ms}ms of retries against a "
        f"{window_ms}ms window). Those two are coupled even though they live in "
        "different modules: a settlement retry resubmits the reply under the "
        "same idempotency key, and the inbox only refuses it while its dedup "
        "row is still there. Let the retries outlast the window and one "
        "delivery produces two replies -- with nothing raised and nothing "
        "logged. Either keep the schedule well inside the window, or make the "
        "reply path idempotent by something that does not expire."
    )


def test_a_resubmission_inside_the_window_is_refused(inbox: InboxService) -> None:
    """The behaviour the arithmetic stands for, on the real inbox."""

    first = inbox.receive(_reply("reply-a", idempotency_key="reply:d1"), now_ms=0)
    second = inbox.receive(
        _reply("reply-b", idempotency_key="reply:d1"),
        now_ms=InboxService.dedup_window_ms // 2,
    )

    assert first.accepted
    assert not second.accepted and second.duplicate, (
        "the retry was taken as a new delivery rather than recognised as the "
        f"same one: {second}"
    )
    # Still acknowledged: a deduplicated retry did reach its recipient, and a
    # sender that is told otherwise retries forever.
    assert second.acknowledged
    held = [
        message.message_id
        for message in inbox.pending_all()
        if message.recipient == RECIPIENT
    ]
    assert held == ["reply-a"], (
        "the second delivery under the same idempotency key created a second "
        f"durable row: {held}"
    )


def test_a_resubmission_after_the_window_is_not_refused(inbox: InboxService) -> None:
    """The control -- and the reason the margin above is load-bearing.

    Without this, the first test would be an assertion about two constants
    with no demonstrated consequence, and the margin could be argued down by
    anyone who read it as mere caution.
    """

    inbox.receive(_reply("reply-a", idempotency_key="reply:d1"), now_ms=0)
    second = inbox.receive(
        _reply("reply-b", idempotency_key="reply:d1"),
        now_ms=InboxService.dedup_window_ms + 1,
    )

    assert second.accepted and not second.duplicate, (
        "the dedup row was expected to have expired, which is what makes the "
        f"window a real constraint rather than a formality: {second}"
    )
    held = [
        message.message_id
        for message in inbox.pending_all()
        if message.recipient == RECIPIENT
    ]
    assert held == ["reply-a", "reply-b"], (
        f"two durable rows were expected once the window lapsed: {held}"
    )
