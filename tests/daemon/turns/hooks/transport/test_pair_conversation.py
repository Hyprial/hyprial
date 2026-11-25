"""Slice H1: hook exchanges share the pair's default conversation.

Every exchange between ``_turn-hooks`` and one handler uses the default
conversation of that pair, and a reply is matched to its request by the
``replyTo`` its body carries, not by a per-exchange conversation id.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from hyprial.daemon.impl.conversations.ids import default_conversation_id, endpoint_key
from hyprial.daemon.impl.correlation.alarm import (
    failure_dedup_scope,
    is_default_conversation_id,
)
from hyprial.daemon.impl.harnesses.turn_delivery import InboxTurnHookInvoker
from hyprial.daemon.impl.inbox.contracts.api import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)

from ..support import HANDLER

CALLBACK = "agent:owner:node:_turn-hooks"
OTHER_HANDLER = "agent:owner:node:other-handler"


class _PairInbox:
    """Requests are recorded; replies are appended by the test."""

    def __init__(self, on_submit=None) -> None:  # type: ignore[no-untyped-def]
        self.requests: list[InboxMessage] = []
        self.pending: list[InboxMessage] = []
        self.acked: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.on_submit = on_submit

    def submit(self, message: InboxMessage, *, now_ms=None) -> SubmissionResult:  # type: ignore[no-untyped-def]
        with self.lock:
            self.requests.append(message)
        if self.on_submit is not None:
            self.on_submit(self, message)
        return SubmissionResult(message.message_id, accepted=True)

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        with self.lock:
            return tuple(self.pending) if actor == CALLBACK else ()

    def ack(self, recipient: str, message_id: str) -> AckResult:
        with self.lock:
            self.acked.append((recipient, message_id))
            if recipient == CALLBACK:
                self.pending = [
                    item for item in self.pending if item.message_id != message_id
                ]
        return AckResult(message_id, acknowledged=True)

    def answer(
        self,
        message_id: str,
        text: str,
        *,
        reply_to: str | None,
        sender: str = HANDLER,
        conversation_id: str | None = None,
    ) -> None:
        body: dict[str, object] = {"message": text}
        if reply_to is not None:
            body["replyTo"] = reply_to
        reply = InboxMessage(
            message_id=message_id,
            conversation_id=conversation_id
            or default_conversation_id(endpoint_key(CALLBACK), endpoint_key(sender)),
            sender=sender,
            recipient=CALLBACK,
            payload=json.dumps(body).encode(),
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=2,
        )
        with self.lock:
            self.pending.append(reply)


def _event(index: int) -> dict[str, object]:
    return {"event": "delivery.prepared", "n": index}


def test_every_exchange_with_one_handler_uses_the_pair_default_conversation() -> None:
    inbox = _PairInbox()
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]

    for index in range(10):
        invoker.notify(HANDLER, _event(index), timeout_seconds=1.0)
    invoker.notify(OTHER_HANDLER, _event(10), timeout_seconds=1.0)

    pair = default_conversation_id(endpoint_key(CALLBACK), endpoint_key(HANDLER))
    other = default_conversation_id(endpoint_key(CALLBACK), endpoint_key(OTHER_HANDLER))
    assert [item.conversation_id for item in inbox.requests] == [pair] * 10 + [other]
    assert pair != other
    assert is_default_conversation_id(pair)
    assert len({item.message_id for item in inbox.requests}) == 11
    assert all(item.message_id.startswith("turn-hook-") for item in inbox.requests)
    assert {item.lifecycle for item in inbox.requests} == {DeliveryLifecycle.ONLINE_ONLY}


def test_a_failing_exchange_still_reports_once_per_request() -> None:
    inbox = _PairInbox()
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]

    invoker.notify(HANDLER, _event(1), timeout_seconds=1.0)
    invoker.notify(HANDLER, _event(2), timeout_seconds=1.0)

    first, second = inbox.requests
    scopes = {
        failure_dedup_scope(item.conversation_id, item.message_id)
        for item in (first, second)
    }
    assert scopes == {f"request:{first.message_id}", f"request:{second.message_id}"}


def test_concurrent_exchanges_each_get_their_own_reply() -> None:
    inbox = _PairInbox()
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]
    results: dict[int, object] = {}

    def run(index: int) -> None:
        try:
            results[index] = invoker.invoke(HANDLER, _event(index), timeout_seconds=5.0)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion
            results[index] = error

    workers = [threading.Thread(target=run, args=(index,)) for index in range(4)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 5
    while len(inbox.requests) < 4:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    for request in reversed(list(inbox.requests)):
        number = json.loads(json.loads(request.payload)["message"])["n"]
        inbox.answer(
            f"reply-{number}", f"answer-{number}", reply_to=request.message_id
        )
    for worker in workers:
        worker.join(5)

    assert results == {index: f"answer-{index}" for index in range(4)}
    assert inbox.pending == []


def test_reply_naming_another_request_or_sender_is_not_taken() -> None:
    """Rows of the pair conversation that answer nothing in flight are retired."""

    def answer_wrongly(inbox: _PairInbox, request: InboxMessage) -> None:
        inbox.answer("foreign-hook", "wrong", reply_to="turn-hook-not-in-flight")
        inbox.answer("unrelated", "wrong", reply_to="some-other-message")
        inbox.answer("no-reply-to", "wrong", reply_to=None)
        inbox.answer(
            "impostor", "wrong", reply_to=request.message_id, sender=OTHER_HANDLER
        )
        inbox.answer("elsewhere", "kept", reply_to=None, sender=OTHER_HANDLER)

    inbox = _PairInbox(on_submit=answer_wrongly)
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="hook handler timed out"):
        invoker.invoke(HANDLER, _event(1), timeout_seconds=0.05)

    acked = {message_id for actor, message_id in inbox.acked if actor == CALLBACK}
    assert acked == {"foreign-hook", "unrelated", "no-reply-to", "impostor"}
    assert [item.message_id for item in inbox.pending] == ["elsewhere"]


def test_late_reply_after_timeout_is_swept_and_not_given_to_the_next_exchange() -> None:
    inbox = _PairInbox()
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        invoker.invoke(HANDLER, _event(1), timeout_seconds=0.02)
    [expired] = inbox.requests
    assert (HANDLER, expired.message_id) in inbox.acked

    def late_only(inbox: _PairInbox, request: InboxMessage) -> None:
        inbox.answer("late", "late answer", reply_to=expired.message_id)

    inbox.on_submit = late_only
    with pytest.raises(TimeoutError):
        invoker.invoke(HANDLER, _event(2), timeout_seconds=0.05)
    assert (CALLBACK, "late") in inbox.acked

    def fresh(inbox: _PairInbox, request: InboxMessage) -> None:
        inbox.answer("late-again", "late answer", reply_to=expired.message_id)
        inbox.answer("fresh", "fresh answer", reply_to=request.message_id)

    inbox.on_submit = fresh
    assert invoker.invoke(HANDLER, _event(3), timeout_seconds=1.0) == "fresh answer"
    assert inbox.pending == []


def test_notify_reply_is_swept_by_a_later_exchange() -> None:
    inbox = _PairInbox()
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]
    invoker.notify(HANDLER, _event(1), timeout_seconds=1.0)
    [notified] = inbox.requests
    inbox.answer("notify-reply", "ignored", reply_to=notified.message_id)

    inbox.on_submit = lambda inbox, request: inbox.answer(
        "own", "own answer", reply_to=request.message_id
    )
    assert invoker.invoke(HANDLER, _event(2), timeout_seconds=1.0) == "own answer"
    assert (CALLBACK, "notify-reply") in inbox.acked
    assert inbox.pending == []


def test_rows_in_a_legacy_exchange_conversation_are_retired() -> None:
    inbox = _PairInbox()
    inbox.answer(
        "legacy-with-reply-to",
        "old",
        reply_to="turn-hook-0123",
        conversation_id="turn-hook:0123",
    )
    inbox.answer(
        "legacy-plain", "old", reply_to=None, conversation_id="turn-hook:4567"
    )
    inbox.on_submit = lambda inbox, request: inbox.answer(
        "own", "own answer", reply_to=request.message_id
    )
    invoker = InboxTurnHookInvoker(inbox, callback_actor=CALLBACK)  # type: ignore[arg-type]

    assert invoker.invoke(HANDLER, _event(1), timeout_seconds=1.0) == "own answer"
    assert inbox.pending == []
    assert {message_id for _actor, message_id in inbox.acked} >= {
        "legacy-with-reply-to",
        "legacy-plain",
    }
