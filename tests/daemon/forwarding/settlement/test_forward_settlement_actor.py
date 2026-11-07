from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.runtime import (
    DaemonEventBridge,
    ForwardOutcome,
    ForwardSettlementCoordinator,
)
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.runtime.ports import ClaimedHarnessResult
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage


def _message(delivery_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=delivery_id,
        conversation_id=f"conversation-{delivery_id}",
        sender="agent:owner:node:sender",
        recipient="agent:owner:node:proxy",
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _result(delivery_id: str) -> HarnessResult:
    return HarnessResult(
        delivery_id,
        "agent:owner:node:proxy",
        HarnessResultStatus.COMPLETED,
        output="forwarded",
        forward_to="agent:owner:node:target",
    )


def test_blocked_forward_keeps_custody_and_deduplicates_replayed_claim() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def forward(original: InboxMessage, _to: str, _text: str) -> ForwardOutcome:
        calls.append(original.message_id)
        entered.set()
        assert release.wait(2)
        return ForwardOutcome(True)

    owner = ForwardSettlementCoordinator(forward, capacity=2)
    original = _message("delivery-1")
    result = _result("delivery-1")
    assert owner.submit(original, result, None) is AdmissionResult.ACCEPTED
    assert entered.wait(1)
    assert owner.submit(original, result, None) is AdmissionResult.ACCEPTED
    assert not owner.close(0.02)
    release.set()
    deadline = time.monotonic() + 1
    while owner.completed(result.delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    completed = owner.completed(result.delivery_id)
    assert completed is not None
    assert completed.outcome.accepted
    assert calls == [result.delivery_id]
    assert owner.projection().decisions == 1
    assert owner.projection().completed == 1
    assert owner.retire(result.delivery_id, completed.decision_token)
    assert owner.close(2)


def test_forward_exception_becomes_a_typed_retryable_outcome() -> None:
    def forward(
        _original: InboxMessage, _to: str, _text: str
    ) -> ForwardOutcome:
        raise RuntimeError("injected")

    owner = ForwardSettlementCoordinator(forward)
    result = _result("delivery-2")
    assert owner.submit(_message("delivery-2"), result, None) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 1
    while owner.completed(result.delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    completed = owner.completed(result.delivery_id)
    assert completed is not None
    assert completed.outcome == ForwardOutcome(
        False, "HARNESS_TRANSIENT_FAILURE", "forward raised RuntimeError"
    )
    assert owner.retire(result.delivery_id, completed.decision_token)
    assert owner.close(2)


def test_delayed_old_completion_cannot_overwrite_readmitted_delivery() -> None:
    second_entered = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []

    def forward(original: InboxMessage, _to: str, _text: str) -> ForwardOutcome:
        calls.append(original.message_id)
        if len(calls) == 2:
            second_entered.set()
            assert release_second.wait(2)
        return ForwardOutcome(True)

    owner = ForwardSettlementCoordinator(forward)
    delivery_id = "delivery-reused"
    assert owner.submit(
        _message(delivery_id), _result(delivery_id), None
    ) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 1
    while owner.completed(delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    first = owner.completed(delivery_id)
    assert first is not None
    assert owner.retire(delivery_id, first.decision_token)

    assert owner.submit(
        _message(delivery_id), _result(delivery_id), None
    ) is AdmissionResult.ACCEPTED
    assert second_entered.wait(1)
    assert owner._runtime.tell(  # noqa: SLF001 - injected stale completion
        owner._handle,  # noqa: SLF001
        EffectCompleted(first.operation_id, first.generation, first),
    ) is AdmissionResult.ACCEPTED
    # Typed retirement is also an actor-ordering barrier behind the injected
    # stale completion. It must not retire the new in-flight incarnation.
    assert not owner.retire(delivery_id, first.decision_token)
    assert owner.completed(delivery_id) is None

    release_second.set()
    deadline = time.monotonic() + 1
    while owner.completed(delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    second = owner.completed(delivery_id)
    assert second is not None
    assert second.operation_id != first.operation_id
    assert second.decision_token != first.decision_token
    assert calls == [delivery_id, delivery_id]
    assert owner.retire(delivery_id, second.decision_token)
    assert owner.close(2)


def test_error_completion_settles_matching_request_without_repeating_effect() -> None:
    calls = 0

    def forward(
        _original: InboxMessage, _to: str, _text: str
    ) -> ForwardOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemExit("synthetic native forward exit")
        return ForwardOutcome(True)

    owner = ForwardSettlementCoordinator(forward)
    delivery_id = "delivery-effect-error"
    assert owner.submit(
        _message(delivery_id), _result(delivery_id), None
    ) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 1
    while owner.completed(delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    completed = owner.completed(delivery_id)
    assert completed is not None
    assert completed.outcome == ForwardOutcome(
        False,
        "HARNESS_TRANSIENT_FAILURE",
        "forward effect failed: SystemExit",
    )
    assert owner.retire(delivery_id, completed.decision_token)

    healthy_id = "delivery-after-effect-error"
    assert owner.submit(
        _message(healthy_id), _result(healthy_id), None
    ) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 1
    while owner.completed(healthy_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    healthy = owner.completed(healthy_id)
    assert healthy is not None
    assert healthy.outcome == ForwardOutcome(True)
    assert owner.retire(healthy_id, healthy.decision_token)
    assert owner.close(2)
    assert calls == 2


def test_claim_settlement_defers_native_forward_and_reuses_completed_decision(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    original = _message("delivery-3")
    result = _result("delivery-3")

    def forward(message: InboxMessage, _to: str, _text: str) -> ForwardOutcome:
        calls.append(message.message_id)
        entered.set()
        assert release.wait(2)
        return ForwardOutcome(True)

    class Inbox:
        ack_calls = 0

        def pending_messages(self, _recipient):  # type: ignore[no-untyped-def]
            return (original,)

        def ack(self, recipient, message_id):  # type: ignore[no-untyped-def]
            self.ack_calls += 1
            assert (recipient, message_id) == (
                original.recipient,
                original.message_id,
            )
            return SimpleNamespace(acknowledged=True)

    class Harnesses:
        def __init__(self) -> None:
            self.settlements = [False, True]

        def settle_result(self, claim_token, delivery_id):  # type: ignore[no-untyped-def]
            assert (claim_token, delivery_id) == ("claim-3", result.delivery_id)
            return self.settlements.pop(0)

    inbox = Inbox()
    harnesses = Harnesses()
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        forwarder=forward,
    )
    owner = ForwardSettlementCoordinator(forward)
    bridge._forward_settlement = owner
    claim = ClaimedHarnessResult("claim-3", "proxy", 1, result)
    started = time.monotonic()
    assert not bridge._settle_claimed_result(claim)
    assert time.monotonic() - started < 0.3
    assert entered.wait(1)
    release.set()
    deadline = time.monotonic() + 1
    while owner.completed(result.delivery_id) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not bridge._settle_claimed_result(claim)
    assert owner.completed(result.delivery_id) is not None
    assert bridge._pending_forward_outcomes[result.delivery_id].accepted
    assert bridge._settle_claimed_result(claim)
    assert calls == [result.delivery_id]
    assert inbox.ack_calls == 1
    assert owner.completed(result.delivery_id) is None
    assert owner.close(2)
