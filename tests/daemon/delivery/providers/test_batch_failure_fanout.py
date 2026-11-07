"""Managed batch failures retain per-request provider settlement."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from hyprial.daemon.impl import DaemonEventBridge
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.kernel import HarnessLaunchSpec, canonical_agent_uri

from tests.identity.providers.lifecycle.availability.support import (
    FakeHarnessFarm,
    WORKER,
    _NullTransport,
    _request,
)
from tests.support.links.gate import permissive_admission_slot

class _BatchHarnesses(FakeHarnessFarm):
    def __init__(self) -> None:
        super().__init__()
        self.batch_deliveries: list[object] = []

    def dispatch(self, name: str, delivery: object) -> bool:
        accepted = super().dispatch(name, delivery)
        if accepted:
            self.batch_deliveries.append(delivery)
        return accepted

    def drain_results(self):
        for result in self.results:
            self.enqueued.discard(result.delivery_id)
        return super().drain_results()


def _arrange(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    inbox = InboxService(
        state_dir / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-reg",
        link_admission=permissive_admission_slot(),
    )
    desired = DesiredStateStore(state_dir / "desired-state.json")
    desired.upsert_harness(
        HarnessLaunchSpec(harness="pi", name="squire", headless=True)
    )
    harnesses = _BatchHarnesses()
    clock = {"now": 1_000}
    runtime = DaemonEventBridge(
        state_dir=state_dir,
        node_id="node-reg",
        desired_state=desired,
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=harnesses,
        harness_actor_uri=lambda _name: WORKER,
        clock_ms=lambda: clock["now"],
    )
    return inbox, harnesses, clock, runtime


def _fail_batch(harnesses: _BatchHarnesses, code: str) -> None:
    [delivery] = harnesses.batch_deliveries[-1:]
    harnesses.results.append(
        HarnessResult(
            delivery.delivery_id,
            WORKER,
            HarnessResultStatus.FAILED,
            error="provider failed the managed batch",
            failure_code=code,
            message_ids=delivery.message_ids,
        )
    )


def _notices(inbox: InboxService) -> list[dict[str, object]]:
    return [
        json.loads(item.message.payload)
        for item in inbox.outbox_items()
        if b'"notification"' in item.message.payload
    ]


def test_batch_failure_notifies_each_original_requester(tmp_path: Path) -> None:
    inbox, harnesses, _clock, runtime = _arrange(tmp_path)
    first = replace(
        _request("batch-two-requesters-1"),
        sender=canonical_agent_uri("op", "first-node", "requester"),
    )
    second = replace(
        _request("batch-two-requesters-2"),
        sender=canonical_agent_uri("op", "second-node", "requester"),
    )
    assert inbox.receive(first).accepted
    assert inbox.receive(second).accepted
    runtime.start()
    try:
        assert runtime.on_timer().harness_deliveries == 1
        delivery = harnesses.batch_deliveries[0]
        assert delivery.message_ids == (first.message_id, second.message_id)
        # The batch has no identity of its own: it is addressed by its first
        # member, and nothing batch-shaped is minted.
        assert delivery.delivery_id == first.message_id
        assert "batch-turn" not in delivery.delivery_id

        _fail_batch(harnesses, "PROVIDER_USAGE_LIMIT")
        runtime.on_timer()

        notices = _notices(inbox)
        assert {notice["deliveryId"] for notice in notices} == {
            first.message_id,
            second.message_id,
        }
        assert {item.message.recipient for item in inbox.outbox_items()} == {
            first.sender,
            second.sender,
        }
        assert all(notice["attemptId"].endswith("#1") for notice in notices)
        # Attribution is per member: the first member's id (which also
        # addresses the batch) must never stand in for the second member.
        sender_of = {first.message_id: first.sender, second.message_id: second.sender}
        for item in inbox.outbox_items():
            body = repr(item.message)
            owned = [mid for mid in sender_of if mid in body]
            assert len(owned) == 1, owned
            assert item.message.recipient == sender_of[owned[0]]
        assert "batch-turn" not in json.dumps(notices)
    finally:
        runtime.stop()
        inbox.close()


def test_claimed_batch_failure_classifies_each_member_before_retiring(
    tmp_path: Path,
) -> None:
    inbox, harnesses, _clock, runtime = _arrange(tmp_path)
    members = (_request("batch-claimed-1"), _request("batch-claimed-2"))
    for message in members:
        assert inbox.receive(message).accepted
    runtime.start()
    try:
        runtime.on_timer()
        [delivery] = harnesses.batch_deliveries
        result = HarnessResult(
            delivery.delivery_id,
            WORKER,
            HarnessResultStatus.FAILED,
            error="provider quota exhausted",
            failure_code="PROVIDER_USAGE_LIMIT",
            message_ids=delivery.message_ids,
        )

        assert runtime._settle_claimed_result_business(result)  # noqa: SLF001
        assert {notice["deliveryId"] for notice in _notices(inbox)} == {
            message.message_id for message in members
        }
        assert runtime._batch_claims == {}  # noqa: SLF001
    finally:
        runtime.stop()
        inbox.close()


def test_batch_failure_uses_tombstone_route_for_an_acked_member(
    tmp_path: Path,
) -> None:
    inbox, harnesses, clock, runtime = _arrange(tmp_path)
    pending = replace(
        _request("batch-mixed-pending"),
        sender=canonical_agent_uri("op", "pending-node", "requester"),
    )
    consumed = replace(
        _request("batch-mixed-acked"),
        sender=canonical_agent_uri("op", "acked-node", "requester"),
    )
    assert inbox.receive(pending).accepted
    assert inbox.receive(consumed).accepted
    runtime.start()
    try:
        runtime.on_timer()
        assert inbox.ack(WORKER, consumed.message_id).acknowledged

        _fail_batch(harnesses, "PROVIDER_TRANSIENT_FAILURE")
        runtime.on_timer()

        notices = _notices(inbox)
        assert {notice["deliveryId"] for notice in notices} == {
            pending.message_id,
            consumed.message_id,
        }
        assert inbox.harness_failure_original(consumed.message_id) == consumed
        assert inbox.harness_failure_settlement(pending.message_id) is not None
        assert inbox.harness_failure_settlement(consumed.message_id) is None

        clock["now"] = 2_000
        assert runtime.on_timer().harness_deliveries == 1
        assert harnesses.batch_deliveries[-1].message_ids == (pending.message_id,)
    finally:
        runtime.stop()
        inbox.close()


def test_transient_batch_retry_advances_each_members_attempt(
    tmp_path: Path,
) -> None:
    inbox, harnesses, clock, runtime = _arrange(tmp_path)
    members = (_request("batch-retry-1"), _request("batch-retry-2"))
    for message in members:
        assert inbox.receive(message).accepted
    runtime.start()
    try:
        runtime.on_timer()
        _fail_batch(harnesses, "PROVIDER_TRANSIENT_FAILURE")
        runtime.on_timer()
        assert {
            message.message_id: inbox.harness_failure_settlement(
                message.message_id
            ).attempts
            for message in members
        } == {"batch-retry-1": 1, "batch-retry-2": 1}

        clock["now"] = 2_000
        assert runtime.on_timer().harness_deliveries == 1
        assert harnesses.batch_deliveries[-1].message_ids == tuple(
            message.message_id for message in members
        )
        _fail_batch(harnesses, "PROVIDER_TRANSIENT_FAILURE")
        runtime.on_timer()

        assert {
            message.message_id: inbox.harness_failure_settlement(
                message.message_id
            ).attempts
            for message in members
        } == {"batch-retry-1": 2, "batch-retry-2": 2}
        assert {notice["attemptId"] for notice in _notices(inbox)} == {
            "batch-retry-1#1",
            "batch-retry-1#2",
            "batch-retry-2#1",
            "batch-retry-2#2",
        }
    finally:
        runtime.stop()
        inbox.close()
