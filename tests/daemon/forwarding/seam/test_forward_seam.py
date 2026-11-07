"""A harness result that names ``forward_to`` is SENT as the worker, not replied.

user-proxy (docs/design/design-user-proxy-harness.md §3, §5) relays: its turn's output
goes to a third party, never back to whoever sent the turn.  The worker only
names the recipient; the daemon owns the send and settles the original inbox
row by what the send did:

* accepted          -> the row is acked, and nobody gets a reply;
* address unusable  -> ``FORWARD_TARGET_UNKNOWN``, permanent, sender told once;
* send fault        -> an ordinary transient failure, redelivered.

The send runs off the tick (a route post has no bound of its own); outcomes are
settled on the tick.  These tests drive ``_complete_harness_results`` directly.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import (
    FORWARD_UNAVAILABLE,
    DaemonEventBridge,
    ForwardOutcome,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)

NODE = "node-fwd"
PROXY = f"agent:op:{NODE}:allen-proxy"
ASKER = f"agent:op:{NODE}:asker"
DM = "route:allen-feishu:allen-dm"


class _Transport:
    def close(self) -> None:
        return None


class _ScriptedHarnesses:
    """Only the one seam settlement reads: results handed out once."""

    def __init__(self) -> None:
        self.results: list[HarnessResult] = []

    def drain_results(self) -> tuple[HarnessResult, ...]:
        drained, self.results = tuple(self.results), []
        return drained

    def streaming_actors(self) -> tuple[str, ...]:
        return ("allen-proxy",)


class _Forwarder:
    def __init__(self, outcomes: list[ForwardOutcome | Exception] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._outcomes = list(outcomes or [])
        self.lock = threading.Lock()

    def __call__(self, original: InboxMessage, to: str, text: str) -> ForwardOutcome:
        with self.lock:
            self.calls.append((original.message_id, to, text))
            outcome = self._outcomes.pop(0) if self._outcomes else ForwardOutcome(True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def inbox(tmp_path: Path) -> Iterator[InboxService]:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id=NODE
    )
    try:
        yield service
    finally:
        service.close()


def _bridge(
    tmp_path: Path, inbox: InboxService, forwarder: _Forwarder | None
) -> tuple[DaemonEventBridge, _ScriptedHarnesses]:
    harnesses = _ScriptedHarnesses()
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,
        harnesses=harnesses,  # type: ignore[arg-type]
        forwarder=forwarder,
    )
    return bridge, harnesses


def _incoming(message_id: str, *, text: str, intent: str = "request") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=ASKER,
        recipient=PROXY,
        payload=json.dumps({"message": text}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _forward(message_id: str, to: str, text: str) -> HarnessResult:
    return HarnessResult(
        message_id, PROXY, HarnessResultStatus.COMPLETED, output=text, forward_to=to
    )


def _settle(bridge: DaemonEventBridge, *, expected: int) -> int:
    """Hand results to the pump, wait for its outcomes, settle them on 'the tick'."""

    settled = bridge._complete_harness_results()
    deadline = time.monotonic() + 5.0
    while bridge._forward_outcomes.qsize() < expected:
        assert time.monotonic() < deadline, "forward pump produced no outcome"
        time.sleep(0.01)
    return settled + bridge._complete_harness_results()


def _pending_ids(inbox: InboxService, actor: str) -> set[str]:
    return {message.message_id for message in inbox.pending_messages(actor)}


def _sent_to(inbox: InboxService, actor: str) -> list[InboxMessage]:
    """What the daemon submitted to ``actor`` (offline here, so it sits queued)."""

    return [item.message for item in inbox.outbox_items() if item.message.recipient == actor]


def test_an_accepted_forward_acks_the_row_and_replies_to_nobody(
    tmp_path: Path, inbox: InboxService
) -> None:
    forwarder = _Forwarder()
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    assert inbox.receive(_incoming("m-1", text="hi allen")).accepted
    harnesses.results.append(_forward("m-1", DM, "hi allen"))

    try:
        assert _settle(bridge, expected=1) == 1
    finally:
        bridge._halt_forward_thread()

    assert forwarder.calls == [("m-1", DM, "hi allen")]
    assert _pending_ids(inbox, PROXY) == set()
    # The asker is not sent the relayed text back as a "reply".
    assert _sent_to(inbox, ASKER) == []


def test_a_reply_intent_row_is_still_forwarded(
    tmp_path: Path, inbox: InboxService
) -> None:
    """A person answering in-thread arrives as intent=reply; an ack-only
    short-circuit would drop the relay without a trace."""

    forwarder = _Forwarder()
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    assert inbox.receive(_incoming("m-2", text="noon", intent="reply")).accepted
    harnesses.results.append(_forward("m-2", ASKER, "noon"))

    try:
        _settle(bridge, expected=1)
    finally:
        bridge._halt_forward_thread()

    assert forwarder.calls == [("m-2", ASKER, "noon")]
    assert _pending_ids(inbox, PROXY) == set()


def test_an_unknown_recipient_settles_permanently_and_tells_the_sender(
    tmp_path: Path, inbox: InboxService
) -> None:
    forwarder = _Forwarder(
        [ForwardOutcome(False, "FORWARD_TARGET_UNKNOWN", "TARGET_IS_NODE: nobody")]
    )
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    assert inbox.receive(_incoming("m-3", text="@nobody hi")).accepted
    harnesses.results.append(_forward("m-3", "nobody", "hi"))

    try:
        _settle(bridge, expected=1)
    finally:
        bridge._halt_forward_thread()

    # Permanent: not left pending for another attempt.
    assert _pending_ids(inbox, PROXY) == set()
    notices = _sent_to(inbox, ASKER)
    assert len(notices) == 1, "the sender must hear that the forward failed"
    assert "FORWARD_TARGET_UNKNOWN" in notices[0].payload.decode()


def test_a_send_fault_is_retried_not_dropped(
    tmp_path: Path, inbox: InboxService
) -> None:
    forwarder = _Forwarder([ForwardOutcome(False, "ROUTE_SEND_FAILED", "lark 500")])
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    assert inbox.receive(_incoming("m-4", text="later")).accepted
    harnesses.results.append(_forward("m-4", DM, "later"))

    try:
        _settle(bridge, expected=1)
    finally:
        bridge._halt_forward_thread()

    # Still owned by the inbox: scheduled for another attempt, not acked.
    assert "m-4" in _pending_ids(inbox, PROXY)


def test_a_forwarder_that_raises_fails_the_turn_and_keeps_the_pump(
    tmp_path: Path, inbox: InboxService
) -> None:
    forwarder = _Forwarder([RuntimeError("boom"), ForwardOutcome(True)])
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    assert inbox.receive(_incoming("m-5", text="one")).accepted
    assert inbox.receive(_incoming("m-6", text="two")).accepted
    harnesses.results.extend([_forward("m-5", DM, "one"), _forward("m-6", DM, "two")])

    try:
        _settle(bridge, expected=2)
    finally:
        bridge._halt_forward_thread()

    assert [call[0] for call in forwarder.calls] == ["m-5", "m-6"]
    assert "m-5" in _pending_ids(inbox, PROXY)
    assert "m-6" not in _pending_ids(inbox, PROXY)


def test_forwards_are_sent_in_the_order_the_worker_finished_them(
    tmp_path: Path, inbox: InboxService
) -> None:
    forwarder = _Forwarder()
    bridge, harnesses = _bridge(tmp_path, inbox, forwarder)
    ids = [f"o-{index}" for index in range(5)]
    for message_id in ids:
        assert inbox.receive(_incoming(message_id, text=message_id)).accepted
        harnesses.results.append(_forward(message_id, DM, message_id))

    try:
        _settle(bridge, expected=len(ids))
    finally:
        bridge._halt_forward_thread()

    assert [call[0] for call in forwarder.calls] == ids


def test_without_a_forwarder_the_turn_fails_loudly_instead_of_acking(
    tmp_path: Path, inbox: InboxService
) -> None:
    bridge, harnesses = _bridge(tmp_path, inbox, None)
    assert inbox.receive(_incoming("m-7", text="hello")).accepted
    harnesses.results.append(_forward("m-7", DM, "hello"))

    bridge._complete_harness_results()

    assert "m-7" in _pending_ids(inbox, PROXY)
    notices = _sent_to(inbox, ASKER)
    assert len(notices) == 1
    assert FORWARD_UNAVAILABLE in notices[0].payload.decode()


def test_an_ordinary_result_settles_per_hint_and_replies_to_no_one(
    tmp_path: Path, inbox: InboxService
) -> None:
    """Control: the forward branch must not swallow the ordinary settle path."""

    bridge, harnesses = _bridge(tmp_path, inbox, _Forwarder())
    assert inbox.receive(_incoming("m-8", text="q")).accepted
    harnesses.results.append(
        HarnessResult("m-8", PROXY, HarnessResultStatus.COMPLETED, output="a")
    )

    bridge._complete_harness_results()

    # No gate is bound, so the hint is keep (WP5-A §3.2): the row stays
    # pending, the final text never becomes a reply, the forwarder is not
    # called, and the row is not re-dispatched.
    assert _pending_ids(inbox, PROXY) == {"m-8"}
    assert _sent_to(inbox, ASKER) == []
    bridge._dispatch_harness_deliveries()
    assert _pending_ids(inbox, PROXY) == {"m-8"}
