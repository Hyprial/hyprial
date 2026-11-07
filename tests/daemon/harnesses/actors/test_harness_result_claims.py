from __future__ import annotations

import threading
import time

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import ClaimHarnessResultsCommand


class _Process:
    pid = None

    def __init__(self) -> None:
        self.running = True
        self.deliveries: list[str] = []
        self.results: list[HarnessResult] = []
        self.drain_entered = threading.Event()
        self.drain_gate: threading.Event | None = None
        self.drain_calls = 0
        self.steers: list[str] = []
        self.steer_answer = True

    def enqueue(self, delivery) -> bool:  # type: ignore[no-untyped-def]
        self.deliveries.append(delivery.delivery_id)
        self.results.append(HarnessResult(
            delivery.delivery_id, delivery.recipient, HarnessResultStatus.COMPLETED,
            output="done", message_ids=delivery.message_ids,
        ))
        return True

    def drain_results(self, limit: int | None = None) -> tuple[HarnessResult, ...]:
        self.drain_calls += 1
        self.drain_entered.set()
        if self.drain_gate is not None:
            assert self.drain_gate.wait(2)
        taken = self.results[:] if limit is None else self.results[:limit]
        del self.results[:len(taken)]
        return tuple(taken)

    def interrupt(self, _delivery_id: str, *, timeout: float = 1.0) -> bool:
        return True

    def steer(self, delivery_id: str, *, timeout: float = 5.0) -> bool:
        del timeout
        self.steers.append(delivery_id)
        return self.steer_answer

    def stop(self) -> None:
        self.running = False


class _Launcher:
    def __init__(self) -> None:
        self.process = _Process()

    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        return self.process


def test_steer_routes_to_the_live_process_and_reports_acceptance() -> None:
    """The actor seam: facade.steer reaches the live process's own steer."""
    from hyprial.daemon.impl.api import HarnessDelivery
    from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    facade = HarnessRuntimeFacade(actor, reply_timeout=5.0)
    try:
        assert facade.start(HarnessLaunchSpec("pi", "worker", True))
        delivery = HarnessDelivery(
            "delivery-1", "conversation-1", "sender", "worker", "work"
        )
        assert facade.dispatch("worker", delivery)
        assert facade.steer("worker", "delivery-1") is True
        assert launcher.process.steers == ["delivery-1"]
        launcher.process.steer_answer = False
        assert facade.steer("worker", "delivery-1") is False
        # An unknown name has no live process: explicit not-accepted.
        assert facade.steer("nobody", "delivery-1") is False
    finally:
        facade.stop(2.0)


def test_steer_port_client_reaches_the_live_actor_process() -> None:
    """Regression (live composition port): the bridge's harnesses expose steer.

    ``DaemonEventBridge`` is wired with ``graph.harnesses``, which
    ``application/wiring/transport.py`` builds as this ``HarnessPortClient``,
    and the T1 timeout path reads ``getattr(self.harnesses, "steer", None)``.
    When ``steer`` existed only on ``ManagedHarnessRuntime`` /
    ``HarnessRuntimeFacade`` that getattr returned ``None`` and every T1 hit
    logged ``availability_loud.steer_failed`` ("harness runtime exposes no
    steer port"): no queued delivery was ever folded into a running turn.
    This pins the port object the live graph actually passes, over the real
    actor, through to the live process's native steer.
    """
    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "delivery-steer-port", "conversation-1", "sender", "worker", "work"
    )
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        # The bridge reads this exact attribute off this exact object.
        steer = getattr(client, "steer", None)
        assert callable(steer), "the live harnesses port exposes no steer"
        assert steer("worker", delivery.delivery_id) is True
        assert launcher.process.steers == [delivery.delivery_id]
        launcher.process.steer_answer = False
        assert steer("worker", delivery.delivery_id) is False
        # No live process under that name: explicit not-accepted, not silence.
        assert steer("nobody", delivery.delivery_id) is False
    finally:
        client.stop(2.0)


def test_claim_retains_delivery_fence_after_native_drain() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "delivery-1", "conversation-1", "sender", "worker", "work"
    )
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        assert not client.dispatch("worker", delivery)
        claims = client.claim_results()
        assert len(claims) == 1
        first = claims[0]
        assert first.result.delivery_id == delivery.delivery_id
        assert launcher.process.results == []
        assert client.pending_result_delivery_ids() == frozenset({"delivery-1"})
        assert not client.dispatch("worker", delivery)
        assert client.claim_results()[0].claim_token == first.claim_token
        assert client.settle_result(first.claim_token, "delivery-1")
        assert client.pending_result_delivery_ids() == frozenset()
        assert client.dispatch("worker", delivery)
        second = client.claim_results()[0]
        assert second.claim_token != first.claim_token
        assert not client.settle_result(first.claim_token, "delivery-1")
        assert client.pending_result_delivery_ids() == frozenset({"delivery-1"})
        assert client.settle_result(second.claim_token, "delivery-1")
    finally:
        client.stop(2.0)


def test_late_claim_completion_replays_without_releasing_offer_fence() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "late-delivery", "late-conversation", "sender", "worker", "work"
    )
    gate = threading.Event()
    launcher.process.drain_gate = gate
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        assert actor.submit(ClaimHarnessResultsCommand("claim-late", 1)) is PortAdmission.ACCEPTED
        assert launcher.process.drain_entered.wait(1)
        assert client.pending_result_delivery_ids() == frozenset({"late-delivery"})
        assert not client.dispatch("worker", delivery)
        gate.set()
        deadline = time.monotonic() + 1
        while not actor._claimed_results and time.monotonic() < deadline:
            time.sleep(0.005)
        claims = client.claim_results(limit=1)
        assert len(claims) == 1
        token = claims[0].claim_token
        assert client.claim_results(limit=1)[0].claim_token == token
        assert client.settle_result(token, delivery.delivery_id)
    finally:
        gate.set()
        client.stop(2.0)


def test_bounded_claim_drains_only_admitted_results() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        for index in range(3):
            assert client.dispatch("worker", HarnessDelivery(
                f"bounded-{index}", f"conversation-{index}",
                "sender", "worker", "work",
            ))
        first = client.claim_results(limit=1)
        assert len(first) == 1
        assert len(launcher.process.results) == 2
        assert client.pending_result_delivery_ids() == frozenset(
            {"bounded-0", "bounded-1", "bounded-2"}
        )
        assert client.settle_result(first[0].claim_token, first[0].result.delivery_id)
        second = client.claim_results(limit=1)
        assert len(second) == 1
        assert second[0].result.delivery_id == "bounded-1"
        assert len(launcher.process.results) == 1
        assert client.settle_result(second[0].claim_token, second[0].result.delivery_id)
        third = client.claim_results(limit=1)
        assert third[0].result.delivery_id == "bounded-2"
        assert client.settle_result(third[0].claim_token, third[0].result.delivery_id)
    finally:
        client.stop(2.0)


def test_old_claim_settlement_does_not_clear_replacement_reservation() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery
    from hyprial.daemon.impl.harnesses.actor import _DeliveryReservation

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "replacement", "conversation", "sender", "worker", "work"
    )
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        claim = client.claim_results()[0]
        original = actor._delivery_reservations[delivery.delivery_id]
        # Simulates a confirmed stop and new generation offer after the old
        # result was drained.  The old settlement must not clear that offer.
        replacement = _DeliveryReservation(original.harness_id, original.process_generation + 1)
        actor._delivery_reservations[delivery.delivery_id] = replacement
        actor._result_reservation_snapshot = frozenset(actor._delivery_reservations)
        assert client.settle_result(claim.claim_token, delivery.delivery_id)
        assert actor._delivery_reservations[delivery.delivery_id] == replacement
        assert client.pending_result_delivery_ids() == frozenset({"replacement"})
    finally:
        client.stop(2.0)


def test_confirmed_stop_waits_for_inflight_claim_before_reopening_offer() -> None:
    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "stop-during-scan", "conversation", "sender", "worker", "work"
    )
    gate = threading.Event()
    launcher.process.drain_gate = gate
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        assert actor.submit(ClaimHarnessResultsCommand("claim-before-stop", 1)) is PortAdmission.ACCEPTED
        assert launcher.process.drain_entered.wait(1)
        reservation = actor._delivery_reservations[delivery.delivery_id]
        # Inject the confirmed-stop transition while native drain is held.
        # The actor's stop completion calls this same transition.
        actor._release_unclaimed_for(reservation.harness_id)
        assert client.pending_result_delivery_ids() == frozenset({delivery.delivery_id})
        gate.set()
        deadline = time.monotonic() + 1
        while not actor._claimed_results and time.monotonic() < deadline:
            time.sleep(0.005)
        claim = client.claim_results()[0]
        assert claim.result.delivery_id == delivery.delivery_id
        assert client.settle_result(claim.claim_token, delivery.delivery_id)
        assert client.pending_result_delivery_ids() == frozenset()
    finally:
        gate.set()
        client.stop(2.0)


def test_batch_dispatch_reserves_every_member() -> None:
    """A batch reservation is keyed by every member, not only its delivery id."""

    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "r1",
        "conversation-1",
        "sender",
        "worker",
        "batch",
        message_ids=("r1", "r2"),
    )
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        assert launcher.process.deliveries == ["r1"]
        assert client.pending_result_delivery_ids() == frozenset({"r1", "r2"})
        assert not client.dispatch("worker", delivery)
        claim = client.claim_results()[0]
        assert client.settle_result(claim.claim_token, "r1")
        assert client.pending_result_delivery_ids() == frozenset()
    finally:
        client.stop(2.0)


def test_claimed_batch_survives_worker_removal_and_restart() -> None:
    """A claimed batch stays fenced even when its unclaimed siblings are released."""

    from hyprial.daemon.impl.api import HarnessDelivery

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events.publish)
    client = HarnessPortClient(actor, events)
    delivery = HarnessDelivery(
        "r1",
        "conversation-1",
        "sender",
        "worker",
        "batch",
        message_ids=("r1", "r2"),
    )
    try:
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert client.dispatch("worker", delivery)
        claim = client.claim_results()[0]
        assert client.pending_result_delivery_ids() == frozenset({"r1", "r2"})
        assert client.remove("pi", "worker")
        assert client.start(HarnessLaunchSpec("pi", "worker", True))
        assert not client.dispatch("worker", delivery)
        assert client.pending_result_delivery_ids() == frozenset({"r1", "r2"})
        assert client.settle_result(claim.claim_token, "r1")
        assert client.pending_result_delivery_ids() == frozenset()
    finally:
        client.stop(2.0)
