"""provider_availability_contract: in-channel notices for human conversations.

Allen, 2026-10-13 (work item A, combined steer + in-DM alarm ruling): an
undelivered/stalled delivery notice for a human-originated conversation must
be REPLIED INTO THE ORIGINAL DM -- the chat where the requester sent their
message -- instead of being diverted to the node owner.  The daemon keeps the
timeout/custody decisions and hands the notice to an adapter-side in-channel
deliverer (the adapter owns the chat handle); owner diversion is retired for
human-originated conversations (observed 2026-10-12: 34 diverted notices in
24h reached the owner; none reached the waiting person).  Rate limit per
(conversation x notice kind): the first occurrence and the terminal outcome
reach the chat; later intermediates are suppressed, never re-sent per tick --
the 2026-09-26 allen-proxy 252-notice self-feeding chain must not regress.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox.contracts.api import (
    DeliveryLifecycle,
    InboxMessage,
)

from .support import (
    HUMAN_FACING,
    PROXY,
    WORKER,
    FakeHarnessFarm,
    LoudInbox,
    _fail_once,
    _human_request,
    _in_channel_runtime,
    _proxy_in_channel_runtime,
    _proxy_request,
    _wait_for,
)


def _recording_deliverer(sink: list[tuple[str, str | None, str]]):
    """Record (recipient, deliveryId, text) for every in-channel notice."""

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    return deliverer


def test_a_failure_notice_is_replied_into_the_original_chat(tmp_path: Path) -> None:
    """The main case: the person's own chat hears the failure, the owner does not."""

    sink: list[tuple[str, str | None, str]] = []
    owner_calls: list[str] = []
    sender = HUMAN_FACING[0]
    inbox = LoudInbox()
    inbox.add(_human_request("req-c1", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now, _recording_deliverer(sink),
        notifier=lambda text, *, idempotency_key: owner_calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-c1")
        runtime.on_timer()  # a retry tick must not reply twice
    finally:
        runtime.stop()

    assert [(s[0], s[1]) for s in sink] == [(sender, "req-c1")]
    assert "PROVIDER_USAGE_LIMIT" in sink[0][2]  # what happened
    assert "- worker:" not in sink[0][2]  # person-facing text only
    # Never a machine leg into the inbox addressed to the person's chat.
    assert [m for m in inbox.sent if m.recipient == sender] == []
    # Owner diversion is retired for human-originated conversations.
    assert owner_calls == []
    assert not [f for e, f in events if e == "availability_loud.diverted"]
    sent = [f for e, f in events if e == "availability_loud.in_channel_sent"]
    assert len(sent) == 1 and sent[0]["recipient"] == sender


def test_no_progress_reaches_the_chat_once_per_conversation(tmp_path: Path) -> None:
    """First occurrence only: a second stalled request in the same chat is
    suppressed, and the log says so instead of the chat."""

    sink: list[tuple[str, str | None, str]] = []
    sender = HUMAN_FACING[0]
    conversation = "lark:oc_customer-stalled"
    inbox = LoudInbox()
    inbox.add(_human_request("req-c2a", sender))
    inbox.add(
        InboxMessage(
            message_id="req-c2b",
            conversation_id=conversation,
            sender=sender,
            recipient=WORKER,
            payload=b'{"message":"another question in the same chat"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1_000,
        )
    )
    # _human_request derives the conversation from the id; align the first row.
    inbox.rows[WORKER][0] = InboxMessage(
        message_id="req-c2a",
        conversation_id=conversation,
        sender=sender,
        recipient=WORKER,
        payload=b'{"message":"customer question"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now, _recording_deliverer(sink)
    )
    try:
        runtime.on_timer()  # dispatch both
        now["v"] += 500_000  # both past pi's 480 s budget, no progress
        runtime.on_timer()
        runtime.on_timer()  # a later tick must not re-send either
    finally:
        runtime.stop()

    assert len(sink) == 1
    assert sink[0][1] in {"req-c2a", "req-c2b"}  # the first one reported
    assert "没有可核进展" in sink[0][2]
    suppressed = [
        f for e, f in events if e == "availability_loud.in_channel_suppressed"
    ]
    assert len(suppressed) == 1
    assert suppressed[0]["conversationId"] == conversation


def test_the_terminal_outcome_always_reaches_the_chat(tmp_path: Path) -> None:
    """First occurrence, suppressed intermediates, and the terminal outcome.

    PROVIDER_CONNECTION is retryable (non-terminal notices); an
    authentication-class failure is permanent, and the terminal verdict must
    reach the chat even after the conversation already used its first
    occurrence.
    """

    sink: list[tuple[str, str | None, str]] = []
    sender = HUMAN_FACING[0]
    inbox = LoudInbox()
    inbox.add(_human_request("req-c3", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}

    def fail(code: str) -> None:
        farm.results.append(
            HarnessResult(
                delivery_id="req-c3",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error=code,
                failure_code=code,
            )
        )
        farm.enqueued.discard("req-c3")
        runtime.on_timer()

    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now, _recording_deliverer(sink)
    )
    try:
        runtime.on_timer()  # dispatch
        fail("PROVIDER_CONNECTION")  # attempt 1: first occurrence -> the chat
        fail("PROVIDER_CONNECTION")  # attempt 2: intermediate -> suppressed
        fail("PROVIDER_AUTHENTICATION_FAILED")  # terminal -> the chat again
    finally:
        runtime.stop()

    assert len(sink) == 2
    assert "将重试" in sink[0][2]  # attempt 1 was non-terminal
    assert "已终止" in sink[1][2]  # the terminal outcome
    suppressed = [
        f for e, f in events if e == "availability_loud.in_channel_suppressed"
    ]
    assert len(suppressed) == 1


def test_an_undeliverable_in_channel_notice_is_held_not_diverted(
    tmp_path: Path,
) -> None:
    """The adapter refusing is a retry, never a return to owner diversion."""

    sink: list[tuple[str, str | None, str]] = []
    owner_calls: list[str] = []
    attempts: list[bool] = [False, True]
    sender = HUMAN_FACING[0]

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        ok = attempts.pop(0) if attempts else True
        if ok:
            sink.append((message.recipient, body.get("deliveryId"), text))
        return ok

    inbox = LoudInbox()
    inbox.add(_human_request("req-c4", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now, deliverer,
        notifier=lambda text, *, idempotency_key: owner_calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-c4")
        runtime.on_timer()  # retry tick: the held notice goes out in channel
        assert _wait_for(lambda: len(sink) >= 1)
    finally:
        runtime.stop()

    assert [(s[0], s[1]) for s in sink] == [(sender, "req-c4")]
    assert owner_calls == []
    held = [f for e, f in events if e == "availability_loud.delivery_failed"]
    assert len(held) == 1 and held[0]["recipient"] == sender


def test_a_user_proxy_requester_hears_it_in_the_relay_once(tmp_path: Path) -> None:
    """The proxy's relay into the person's DM is the original channel, and the
    (conversation x kind) rate limit bounds it -- the 252-notice chain shape."""

    owner_calls: list[str] = []
    inbox = LoudInbox()
    inbox.add(_proxy_request("req-c5"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _proxy_in_channel_runtime(
        tmp_path, inbox, farm, now,
        lambda text, *, idempotency_key: owner_calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-c5")
        runtime.on_timer()  # a retry tick must not submit twice
    finally:
        runtime.stop()

    notices = [
        json.loads(m.payload)
        for m in inbox.sent
        if m.recipient == PROXY and "notification" in json.loads(m.payload)
    ]
    assert len(notices) == 1  # the relay leg, exactly once
    assert notices[0]["deliveryId"] == "req-c5"
    assert owner_calls == []  # diversion retired for the person's requests
    assert not [f for e, f in events if e == "availability_loud.diverted"]
