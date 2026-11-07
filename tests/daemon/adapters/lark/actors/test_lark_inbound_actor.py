from __future__ import annotations

import threading
import time

from hyprial.kernel import ActorState
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    InboundOutcome,
    LarkInboundMessage,
)
from hyprial.daemon.impl.adapters.lark.inbound.inbound_runtime import LarkInboundRuntime


def _message(index: int, *, message_id: str | None = None) -> LarkInboundMessage:
    return LarkInboundMessage(
        event_id=f"event-{index}",
        message_id=message_id or f"message-{index}",
        chat_id="chat",
        chat_type="p2p",
        text=f"body-{index}",
        sender_id="sender",
        sender_type="user",
    )


def test_concurrent_duplicate_is_rejected_while_first_has_custody() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def process(message: LarkInboundMessage, suppress: bool) -> InboundOutcome:
        del suppress
        calls.append(message.message_id)
        entered.set()
        assert release.wait(3.0)
        return InboundOutcome(status="forwarded")

    runtime = LarkInboundRuntime("test", processor=process)
    first: list[InboundOutcome] = []
    thread = threading.Thread(
        target=lambda: first.append(runtime.process(_message(1))),
    )
    thread.start()
    assert entered.wait(1.0)
    duplicate = runtime.process(_message(2, message_id="message-1"))
    release.set()
    thread.join(3.0)
    try:
        assert duplicate.status == "duplicate"
        assert [item.status for item in first] == ["forwarded"]
        assert calls == ["message-1"]
    finally:
        runtime.close()


def test_distinct_callbacks_preserve_actor_admission_order() -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    order: list[str] = []

    def process(message: LarkInboundMessage, suppress: bool) -> InboundOutcome:
        del suppress
        order.append(f"start:{message.message_id}")
        if message.message_id == "message-1":
            first_entered.set()
            assert release_first.wait(3.0)
        order.append(f"finish:{message.message_id}")
        return InboundOutcome(status="forwarded")

    runtime = LarkInboundRuntime("test", processor=process)
    results: list[InboundOutcome] = []
    first = threading.Thread(target=lambda: results.append(runtime.process(_message(1))))
    second = threading.Thread(target=lambda: results.append(runtime.process(_message(2))))
    first.start()
    assert first_entered.wait(1.0)
    second.start()
    time.sleep(0.05)
    assert order == ["start:message-1"]
    release_first.set()
    first.join(3.0)
    second.join(3.0)
    try:
        assert order == [
            "start:message-1",
            "finish:message-1",
            "start:message-2",
            "finish:message-2",
        ]
        assert len(results) == 2
    finally:
        runtime.close()


def test_inflight_custody_survives_actor_restart_until_old_effect_completes() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def process(message: LarkInboundMessage, suppress: bool) -> InboundOutcome:
        nonlocal calls
        del message, suppress
        calls += 1
        entered.set()
        assert release.wait(3.0)
        return InboundOutcome(status="forwarded")

    runtime = LarkInboundRuntime("test", processor=process)
    first: list[InboundOutcome] = []
    thread = threading.Thread(target=lambda: first.append(runtime.process(_message(1))))
    thread.start()
    assert entered.wait(1.0)
    handle = runtime._handle
    assert handle is not None
    runtime._runtime.tell(handle, object())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        snapshot = runtime._runtime.snapshot(handle)
        if snapshot.generation > 1 and snapshot.state is ActorState.RUNNING:
            break
        time.sleep(0.01)
    duplicate = runtime.process(_message(1))
    release.set()
    thread.join(3.0)
    try:
        assert duplicate.status == "duplicate"
        assert calls == 1
        assert [item.status for item in first] == ["forwarded"]
    finally:
        runtime.close()


def test_timeout_fails_waiter_but_keeps_dedup_custody_until_effect_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def process(message: LarkInboundMessage, suppress: bool) -> InboundOutcome:
        nonlocal calls
        del message, suppress
        calls += 1
        entered.set()
        assert release.wait(3.0)
        return InboundOutcome(status="forwarded")

    runtime = LarkInboundRuntime("test", processor=process)
    timed_out = runtime.process(_message(1), timeout=0.05)
    assert entered.is_set()
    duplicate = runtime.process(_message(1))
    release.set()
    try:
        assert timed_out.ack_error == "inbound-timeout"
        assert duplicate.status == "duplicate"
        assert calls == 1
    finally:
        runtime.close()
