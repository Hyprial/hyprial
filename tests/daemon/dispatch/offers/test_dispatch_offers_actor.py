from __future__ import annotations

import threading

import pytest

from hyprial.kernel import ActorRuntime, AdmissionResult
from hyprial.kernel import EffectCompleted
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.dispatch.runtime.offers import (
    DispatchAgentTimeout,
    DispatchOfferCoordinator,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage


def _offer(worker: str) -> tuple[HarnessDelivery, InboxMessage]:
    delivery = HarnessDelivery(
        f"delivery-{worker}", f"conversation-{worker}",
        "agent:owner:node:sender", worker, "work",
    )
    message = InboxMessage(
        message_id=delivery.delivery_id,
        conversation_id=delivery.conversation_id,
        sender=delivery.sender,
        recipient=worker,
        payload=b'{"message":"work"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    return delivery, message


def test_one_worker_block_does_not_hold_another_and_close_is_fenced() -> None:
    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()

    def dispatch(worker: str, _delivery: HarnessDelivery) -> bool:
        if worker == "worker-a":
            entered.set()
            assert release.wait(2)
        else:
            second.set()
        return True

    owner = DispatchOfferCoordinator(dispatch, workers=2)
    a, a_message = _offer("worker-a")
    b, b_message = _offer("worker-b")
    assert owner.offer("worker-a", a, original=a_message, retry=False, notice=False)
    assert entered.wait(1)
    assert not owner.offer("worker-a", a, original=a_message, retry=False, notice=False)
    assert owner.offer("worker-b", b, original=b_message, retry=False, notice=False)
    assert second.wait(1)
    assert not owner.close(0.02)
    release.set()
    assert owner.drain(2)
    outcomes = owner.completed()
    assert {item.delivery_id for item in outcomes} == {
        "delivery-worker-a", "delivery-worker-b"
    }
    assert owner.close(2)


def test_lost_accepted_completion_replays_without_repeating_offer() -> None:
    class LostOnce(ActorRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.lost = False

        def tell(self, handle, command):  # type: ignore[no-untyped-def]
            if isinstance(command, EffectCompleted) and not self.lost:
                self.lost = True
                return AdmissionResult.ACCEPTED
            return super().tell(handle, command)

    runtime = LostOnce()
    calls: list[str] = []
    owner = DispatchOfferCoordinator(
        lambda worker, _delivery: calls.append(worker) or True,
        runtime=runtime,
    )
    delivery, message = _offer("worker-a")
    assert owner.offer("worker-a", delivery, original=message, retry=False, notice=False)
    assert owner.drain(2)
    outcomes = owner.completed()
    assert len(outcomes) == 1 and outcomes[0].accepted
    assert calls == ["worker-a"]
    assert runtime.lost
    assert owner.close(2)


def test_four_hung_dispatches_do_not_starve_a_healthy_worker() -> None:
    release = threading.Event()
    healthy = threading.Event()

    def dispatch(worker: str, _delivery: HarnessDelivery) -> bool:
        if worker.startswith("hung"):
            release.wait(2)
        else:
            healthy.set()
        return True

    owner = DispatchOfferCoordinator(dispatch)
    try:
        for index in range(4):
            delivery, message = _offer(f"hung{index}")
            with pytest.raises(DispatchAgentTimeout):
                owner.offer_sync(
                    f"hung{index}",
                    delivery,
                    original=message,
                    retry=False,
                    notice=False,
                    timeout_ms=20,
                )
        delivery, message = _offer("healthy")
        assert owner.offer_sync(
            "healthy",
            delivery,
            original=message,
            retry=False,
            notice=False,
            timeout_ms=20,
        )
        assert healthy.wait(0.5)
    finally:
        release.set()
        assert owner.close(2)



def test_base_exception_completion_releases_the_active_slot() -> None:
    def dispatch(_worker: str, _delivery: HarnessDelivery) -> bool:
        raise SystemExit("adapter exit")

    owner = DispatchOfferCoordinator(dispatch)
    delivery, message = _offer("one")
    try:
        assert owner.offer_sync(
            "one",
            delivery,
            original=message,
            retry=False,
            notice=False,
            timeout_ms=30,
        ) is False
        assert owner._effects.snapshot().outstanding == 0
        assert owner.active("one") is None
    finally:
        owner._active.clear()
        owner._active_since.clear()
        assert owner.close(2)


def test_idle_worker_lanes_are_reclaimed() -> None:
    owner = DispatchOfferCoordinator(lambda _worker, _delivery: True)
    try:
        for index in range(20):
            worker = f"worker-{index}"
            delivery, message = _offer(worker)
            assert owner.offer_sync(
                worker,
                delivery,
                original=message,
                retry=False,
                notice=False,
                timeout_ms=1_000,
            )
        assert owner._effects._lanes == {}
    finally:
        assert owner.close(2)
