from __future__ import annotations

import threading
import time
from pathlib import Path

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.correlation.bounded_cadence import CadenceCompleted
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.runtime.ports import ClaimedHarnessResult, HarnessResultsClaimed
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.processes.result_settlement import ResultSettlementCoordinator
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage


def _claim(delivery_id: str) -> ClaimedHarnessResult:
    return ClaimedHarnessResult(
        f"token-{delivery_id}", "pi:worker-a", 1,
        HarnessResult(delivery_id, "worker-a", HarnessResultStatus.COMPLETED, output="done"),
    )


def test_blocked_result_does_not_hold_unrelated_settlement() -> None:
    blocked = threading.Event()
    release = threading.Event()
    second = threading.Event()

    def settle(claim: ClaimedHarnessResult) -> bool:
        if claim.result.delivery_id == "first":
            blocked.set()
            assert release.wait(2)
        else:
            second.set()
        return True

    owner = ResultSettlementCoordinator(settle, capacity=2, workers=2)
    try:
        assert owner.submit(_claim("first")) is AdmissionResult.ACCEPTED
        assert blocked.wait(1)
        assert owner.submit(_claim("first")) is AdmissionResult.ACCEPTED
        assert owner.submit(_claim("second")) is AdmissionResult.ACCEPTED
        assert second.wait(1)
        deadline = time.monotonic() + 1
        while owner.projection().pending != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert owner.projection().pending == 1
    finally:
        release.set()
        assert owner.close(2)


def test_settlement_timeout_keeps_accepted_effect_for_retryable_close() -> None:
    entered = threading.Event()
    release = threading.Event()

    def settle(_claim: ClaimedHarnessResult) -> bool:
        entered.set()
        assert release.wait(2)
        return True

    owner = ResultSettlementCoordinator(settle, capacity=1, workers=1)
    try:
        assert owner.submit(_claim("held")) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert owner.submit(_claim("second")) is AdmissionResult.OVERLOADED
        assert not owner.close(0.01)
        assert owner.projection().pending == 1
        assert owner.projection().overloaded == 1
    finally:
        release.set()
        assert owner.close(2)


def test_blocked_workflow_result_does_not_hold_new_dispatch(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    dispatched = threading.Event()
    original = InboxMessage(
        message_id="new-delivery", conversation_id="conversation-new",
        sender="sender", recipient="worker-b", payload=b'{"message":"work"}',
        intent="request", lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )

    class Harnesses:
        def streaming_actors(self) -> tuple[str, ...]:
            return ("worker-b",)

        def dispatch(self, _actor: str, _delivery: object) -> bool:
            dispatched.set()
            return True

        def claim_result_batch(self) -> HarnessResultsClaimed:
            return HarnessResultsClaimed("claim", 1, 1, ())

        def settle_result(self, _token: str, _delivery_id: str) -> bool:
            return True

        def pending_result_delivery_ids(self) -> frozenset[str]:
            return frozenset({"old-delivery"})

    class Inbox:
        def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
            return (original,) if recipient == "worker-b" else ()

        def refresh_hold(self, _message_id: str) -> bool:
            return True

    def workflow_outcome(result: HarnessResult) -> bool:
        if result.delivery_id == "old-delivery":
            entered.set()
            assert release.wait(2)
        return True

    bridge = DaemonEventBridge(
        state_dir=tmp_path / "state", node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        transport=object(),  # type: ignore[arg-type]
        inbox=Inbox(),  # type: ignore[arg-type]
        harnesses=Harnesses(),  # type: ignore[arg-type]
        workflow_outcome=workflow_outcome, actor_mode=True,
    )
    bridge.start()
    try:
        batch = HarnessResultsClaimed("claim", 1, 1, (
            ClaimedHarnessResult(
                "old-token", "pi:worker-a", 1,
                HarnessResult(
                    "old-delivery", "worker-a", HarnessResultStatus.COMPLETED,
                    output="done",
                ),
            ),
        ))
        bridge._claimed_results_available(CadenceCompleted(
            1, 1, time.monotonic(), 0, batch, None,
        ))
        assert entered.wait(1)
        started = time.monotonic()
        bridge._dispatch_harness_deliveries()
        assert time.monotonic() - started < 0.3
        assert dispatched.wait(1)
    finally:
        release.set()
        bridge.stop()
