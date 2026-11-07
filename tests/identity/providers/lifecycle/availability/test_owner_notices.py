"""provider_availability_contract: retired owner-diversion scenarios.

The 2026-09-26 divert-to-owner rule was retired for human-originated
conversations by Allen's 2026-10-13 in-DM alarm ruling: the notice is replied
into the chat where the requester asked (see ``test_in_channel_notices.py``
for the new contract).  This file keeps the boundaries that ruling did NOT
change: the agent-requester path, the notice-of-notice chain fence, and the
person-facing text shape.  The owner channel is no longer a fallback for any
human-originated conversation, with or without a reachable in-channel
deliverer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon import AttemptIdentity, unavailable_notice
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox.contracts.api import (
    DeliveryLifecycle,
    InboxMessage,
)

from .support import (
    HUMAN_FACING,
    PROXY,
    REQUESTER,
    WORKER,
    FakeHarnessFarm,
    LoudInbox,
    _fail_once,
    _human_request,
    _in_channel_runtime,
    _notices_to,
    _proxy_in_channel_runtime,
    _proxy_request,
    _proxy_runtime,
    _request,
    _wait_for,
)


@pytest.mark.parametrize("sender", HUMAN_FACING)
def test_failure_notice_never_becomes_an_inbox_leg_to_the_requester(
    tmp_path: Path, sender: str
) -> None:
    """The in-channel reply is the only leg: nothing is submitted to the
    inbox addressed to the person's chat, on any human-facing shape."""

    sink: list[tuple[str, str | None, str]] = []

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    inbox = LoudInbox()
    inbox.add(_human_request("req-h1", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(tmp_path, inbox, farm, now, deliverer)
    try:
        runtime.on_timer()
        now["v"] += 1_500
        farm.results.append(
            HarnessResult(
                delivery_id="req-h1",
                recipient=WORKER,
                status=HarnessResultStatus.FAILED,
                error="quota exhausted",
                failure_code="PROVIDER_USAGE_LIMIT",
            )
        )
        runtime.on_timer()
        runtime.on_timer()  # a retry tick must not resend it either
    finally:
        runtime.stop()

    assert [m for m in inbox.sent if m.recipient == sender] == []
    assert [(s[0], s[1]) for s in sink] == [(sender, "req-h1")]
    assert not [f for e, f in events if e == "availability_loud.diverted"]
    assert not [f for e, f in events if e == "availability_loud.delivery_failed"]


def test_no_progress_notice_goes_in_channel_without_touching_the_turn(
    tmp_path: Path,
) -> None:
    sender = HUMAN_FACING[0]
    sink: list[tuple[str, str | None, str]] = []

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    inbox = LoudInbox()
    inbox.add(_human_request("req-h2", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(tmp_path, inbox, farm, now, deliverer)
    try:
        runtime.on_timer()
        now["v"] += 500_000  # past pi's 480 s budget, no progress
        runtime.on_timer()
        runtime.on_timer()
    finally:
        runtime.stop()

    assert len(sink) == 1 and sink[0][0] == sender
    assert "没有可核进展" in sink[0][2]
    # The turn is still not interrupted: reporting is not a new action on it.
    assert farm.interrupts == []
    assert not [f for e, f in events if e == "availability_loud.diverted"]


def test_owner_diversion_stays_retired_without_a_deliverer(tmp_path: Path) -> None:
    """No in-channel deliverer wired (an old composition, a user: shape with
    no chat handle): the notice is held loudly for retry -- it must NOT fall
    back to the owner channel (2026-10-13 retirement is unconditional for
    human-originated conversations)."""

    calls: list[str] = []
    inbox = LoudInbox()
    inbox.add(_human_request("req-o1", HUMAN_FACING[0]))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now,
        deliverer=None,  # type: ignore[arg-type]
        notifier=lambda text, *, idempotency_key: calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-o1")
        runtime.on_timer()
    finally:
        runtime.stop()

    assert calls == []
    assert not [f for e, f in events if e == "availability_loud.diverted"]
    held = [f for e, f in events if e == "availability_loud.delivery_failed"]
    assert len(held) == 1 and held[0]["code"] == "IN_CHANNEL_DELIVERY_UNWIRED"


def test_an_agent_requester_never_reaches_the_owner_channel(tmp_path: Path) -> None:
    calls: list[str] = []
    inbox = LoudInbox()
    inbox.add(_request("req-o2"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path, inbox, farm, now,
        deliverer=None,  # type: ignore[arg-type]
        notifier=lambda text, *, idempotency_key: calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-o2")
    finally:
        runtime.stop()
    assert len(_notices_to(inbox, REQUESTER)) == 1  # unchanged path
    assert calls == []


def test_a_failing_deliverer_never_breaks_the_tick(tmp_path: Path) -> None:
    def boom(message: InboxMessage, text: str) -> bool:
        raise RuntimeError("lark down")

    inbox = LoudInbox()
    inbox.add(_human_request("req-o3", HUMAN_FACING[0]))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(tmp_path, inbox, farm, now, boom)
    try:
        _fail_once(runtime, farm, now, "req-o3")
        runtime.on_timer()
        assert _wait_for(
            lambda: any(e == "availability_loud.delivery_failed" for e, _ in events)
        )
    finally:
        runtime.stop()
    held = [f for e, f in events if e == "availability_loud.delivery_failed"]
    assert held[0]["code"] == "RuntimeError"


def test_the_owners_own_request_hears_it_in_their_own_chat(
    tmp_path: Path,
) -> None:
    """The 2026-09-26 requester-is-owner exemption retired with diversion:
    the owner waiting in a chat gets the notice in that chat -- the same
    person, zero extra wrappers (and the owner DM channel stays untouched)."""

    import time

    sender = HUMAN_FACING[0]  # the owner's own adapter, as it arrives in a notice
    calls: list[str] = []
    sink: list[tuple[str, str | None, str]] = []

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    inbox = LoudInbox()
    inbox.add(_human_request("req-o4", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _in_channel_runtime(
        tmp_path,
        inbox,
        farm,
        now,
        deliverer,
        notifier=lambda text, *, idempotency_key: calls.append(text),
        owner_requester_addresses=(sender,),
    )
    try:
        _fail_once(runtime, farm, now, "req-o4")
        runtime.on_timer()  # a retry tick must not re-hand it either
        time.sleep(0.2)  # any legacy notify ran on its own thread; give it its chance
    finally:
        runtime.stop()

    assert calls == []
    assert [(s[0], s[1]) for s in sink] == [(sender, "req-o4")]
    assert not [f for e, f in events if e == "availability_loud.diverted"]


def test_in_channel_text_drops_the_worker_uri_and_attempt_id(
    tmp_path: Path,
) -> None:
    sender = HUMAN_FACING[0]
    sink: list[tuple[str, str | None, str]] = []

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    inbox = LoudInbox()
    inbox.add(_human_request("req-o6", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, _events = _in_channel_runtime(tmp_path, inbox, farm, now, deliverer)
    try:
        _fail_once(runtime, farm, now, "req-o6")
    finally:
        runtime.stop()

    assert len(sink) == 1
    text = sink[0][2]
    assert "PROVIDER_USAGE_LIMIT" in text  # what happened
    assert "- worker:" not in text
    assert "- attempt:" not in text


def test_in_channel_no_progress_text_drops_the_silence_budget(
    tmp_path: Path,
) -> None:
    sender = HUMAN_FACING[0]
    sink: list[tuple[str, str | None, str]] = []

    def deliverer(message: InboxMessage, text: str) -> bool:
        body = json.loads(message.payload)
        sink.append((message.recipient, body.get("deliveryId"), text))
        return True

    inbox = LoudInbox()
    inbox.add(_human_request("req-o7", sender))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, _events = _in_channel_runtime(tmp_path, inbox, farm, now, deliverer)
    try:
        runtime.on_timer()
        now["v"] += 500_000  # past pi's 480 s budget, no progress
        runtime.on_timer()
    finally:
        runtime.stop()

    assert len(sink) == 1
    text = sink[0][2]
    assert "静默预算" not in text
    assert "- worker:" not in text
    assert "- attempt:" not in text


def test_a_failed_notice_never_produces_a_notice_about_itself(tmp_path: Path) -> None:
    """Negative for the chain: whoever the requester is, a delivery that IS a
    fail-loud notice gets no notice of its own when it fails."""

    identity = AttemptIdentity(
        delivery_id="orig-1", conversation_id="conv-orig-1", sender=REQUESTER,
        worker=WORKER, harness="pi", generation=1, observed_at_ms=900,
    )
    notice = unavailable_notice(
        identity, failure_code="ROUTE_SEND_FAILED", terminal=True,
        attempts=1, max_attempts=3, detail=None,
    )
    inbox = LoudInbox()
    inbox.add(
        InboxMessage(
            message_id="notice-as-request",
            conversation_id="conv-n1",
            sender=REQUESTER,
            recipient=WORKER,
            payload=notice.payload,
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1_000,
        )
    )
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _proxy_runtime(tmp_path, inbox, farm, now)
    try:
        _fail_once(runtime, farm, now, "notice-as-request")
        runtime.on_timer()
        now["v"] += 900_000  # far past any no-progress budget
        runtime.on_timer()
    finally:
        runtime.stop()

    assert _notices_to(inbox, REQUESTER) == []
    assert [f for e, f in events if e == "availability_loud.notice_of_notice_suppressed"]


def test_an_ordinary_request_from_an_agent_still_gets_its_notice(tmp_path: Path) -> None:
    """Negative control: the new guards leave the agent-requester path alone."""

    inbox = LoudInbox()
    inbox.add(_request("req-p3"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, _events = _proxy_runtime(tmp_path, inbox, farm, now)
    try:
        _fail_once(runtime, farm, now, "req-p3")
    finally:
        runtime.stop()
    assert len(_notices_to(inbox, REQUESTER)) == 1


def test_a_notice_for_a_user_proxy_relay_is_not_rehanded_to_the_owner(
    tmp_path: Path,
) -> None:
    """The proxy leg reaches the person's DM on its own; the owner channel is
    not a second copy (the in-DM ruling supersedes the 2026-09-26 divert)."""

    calls: list[str] = []
    inbox = LoudInbox()
    inbox.add(_proxy_request("req-p1"))
    farm = FakeHarnessFarm()
    now = {"v": 1_000}
    runtime, events = _proxy_in_channel_runtime(
        tmp_path, inbox, farm, now,
        lambda text, *, idempotency_key: calls.append(text),
    )
    try:
        _fail_once(runtime, farm, now, "req-p1")
        runtime.on_timer()
    finally:
        runtime.stop()

    notices = _notices_to(inbox, PROXY)
    assert len(notices) == 1  # the relay leg, exactly once
    assert calls == []
    assert not [f for e, f in events if e == "availability_loud.diverted"]
