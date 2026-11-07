"""A TTL-pruned inbox row must not wedge the worker's turn FIFO.

``InboxService.prune_inbox`` DELETEs an unconsumed row whose hold deadline
passed -- which happens whenever a worker stays silent longer than the hold
while its turn is still running.  The row is gone and was never acked, so
afterwards the daemon can neither find the original message nor observe it as
acknowledged.

The question this pins is what happens to the *next* turn.  Releasing the FIFO
head only on an explicit durable settlement makes that unreachable for a
pruned row: the head is retained forever and every later turn for that worker
queues behind it.  A worker that goes quiet once then stops answering for good
is what that looks like from outside, and no assertion in this suite noticed
it -- the review that found it recorded 2695 passing while the wedge was live.

⚠️ Written from a review probe rather than invented here, and it is only worth
its keep because it can go red: copied into a worktree at af6bc60 (the
settlement-fence rework) it fails, because turn two never starts.  A test for
a defect that no longer exists anywhere is a test nobody can check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    EnqueueTurnCommand,
    TurnDeliveryProjection,
    TurnResultProjection,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime
from hyprial.daemon.impl.inbox import AckResult, DeliveryLifecycle, InboxMessage, SubmissionResult

WORKER = "agent:owner:node:worker"
SENDER = "agent:owner:node:sender"


@dataclass
class _Registration:
    def close(self) -> None:
        return None


class _Transport:
    def declare_liveliness(self, _key: str) -> _Registration:
        return _Registration()


class _Inbox:
    """Just enough durable inbox to delete one row the way a TTL sweep does."""

    def __init__(self, messages: tuple[InboxMessage, ...]) -> None:
        self._messages = {message.message_id: message for message in messages}
        self._acknowledged: set[str] = set()
        self.ack_calls: list[str] = []

    def prune(self, message_id: str) -> None:
        # prune_inbox DELETEs the row; it does not mark it consumed.
        self._messages.pop(message_id, None)

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return tuple(
            message
            for message in self._messages.values()
            if message.recipient == recipient
            and message.message_id not in self._acknowledged
        )

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, True)

    def ack(self, _recipient: str, message_id: str) -> AckResult:
        self.ack_calls.append(message_id)
        if message_id not in self._messages or message_id in self._acknowledged:
            return AckResult(message_id, False, "MESSAGE_ACK_UNAVAILABLE")
        self._acknowledged.add(message_id)
        return AckResult(message_id, True)

    def is_acknowledged(self, message_id: str) -> bool:
        # Mirrors the real service: "row is not None and bool(row['consumed'])",
        # so a deleted row answers False rather than True.
        return message_id in self._messages and message_id in self._acknowledged

    def harness_failure_settlement(self, _message_id: str) -> None:
        return None

    def fetch_pending(
        self,
        _recipient: str,
        *,
        only: frozenset[str] | None = None,
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        # WP5-A settle path: the durable service stamps/clears in one
        # transaction; this double only answers the read.
        del only, completed_turn
        return ()

    def retry_due(self) -> list[SubmissionResult]:
        return []

    def prune_inbox(self) -> tuple[()]:
        return ()

    def refresh_hold(self, _message_id: str) -> bool:
        return True


class _Harnesses:
    """A worker whose turns complete immediately, over the real TurnRuntime."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.turn = TurnRuntime(
            name="prune-fifo",
            capacity=2,
            io_submit=self._execute,
            io_interrupt=lambda _g, _v, _c, _d: None,
            io_close=lambda _g, _v, _c: None,
            io_abort=lambda _g, ready: ready(),
            io_failure_decision=lambda _g, _v, _d, _r, _a, _e: None,
        )

    def _execute(self, generation: int, version: int, delivery) -> None:
        self.starts.append(delivery.delivery_id)
        accepted = self.turn.complete_io(
            generation=generation,
            version=version,
            result=TurnResultProjection(
                delivery.delivery_id,
                delivery.recipient,
                "completed",
                f"result-{delivery.delivery_id}",
                None,
                None,
            ),
        )
        assert accepted is PortAdmission.ACCEPTED

    def reconcile(self) -> int:
        return 0

    def session_refs(self) -> dict[str, str]:
        return {}

    def reconcile_session_refs(self) -> dict[str, str]:
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return ("worker",)

    def dispatch(self, _name: str, delivery: HarnessDelivery) -> bool:
        return self.turn.submit(
            EnqueueTurnCommand(
                delivery.delivery_id,
                TurnDeliveryProjection(
                    delivery.delivery_id,
                    delivery.conversation_id,
                    delivery.sender,
                    delivery.recipient,
                    delivery.message,
                ),
            )
        ) is PortAdmission.ACCEPTED

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return tuple(
            HarnessResult(
                item.delivery_id,
                item.recipient,
                HarnessResultStatus(item.status),
                output=item.output,
                error=item.error,
                failure_code=item.failure_code,
            )
            for item in self.turn.drain_results()
        )

    def drain_progress(self) -> tuple[()]:
        return ()

    def drain_failed_events(self) -> tuple[()]:
        return ()


def _message(identifier: str) -> InboxMessage:
    return InboxMessage(
        message_id=identifier,
        conversation_id="conversation",
        sender=SENDER,
        recipient=WORKER,
        payload=f'{{"message":"prompt-{identifier}"}}'.encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_a_pruned_head_row_does_not_wedge_the_next_turn(tmp_path: Path) -> None:
    """Two queued turns; the first one's row is deleted mid-flight.

    The assertion is on the *second* turn.  Asserting only that the first one
    completed would pass in both worlds -- it completes either way; what a
    retained head costs is everything queued behind it.
    """

    inbox = _Inbox((_message("one"), _message("two")))
    harnesses = _Harnesses()
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda _name: WORKER,
    )
    runtime.start()
    try:
        runtime.on_timer()
        assert _wait(lambda: bool(harnesses.turn.read_results())), (
            "the first turn never produced a result, so this test never "
            "reached the situation it is about"
        )
        assert harnesses.starts[:1] == ["one"]

        # The TTL sweep deletes the unconsumed row while its turn is in flight.
        inbox.prune("one")

        # Several ticks: the head is released on one of them, and the next
        # dispatch happens on a later one.  Bounded by the predicate rather
        # than by sleeping a fixed time, so a fast machine does not wait.
        started_two = _wait(
            lambda: (runtime.on_timer(), harnesses.starts[:2] == ["one", "two"])[1],
            timeout=3.0,
        )

        assert started_two, (
            "turn 'two' never started: the pruned row's turn is still holding "
            f"the FIFO head. starts={harnesses.starts} "
            f"retained={[r.delivery_id for r in harnesses.turn.read_results()]} "
            f"in_flight={harnesses.turn.read_in_flight()}"
        )
    finally:
        runtime.stop()
        assert harnesses.turn.drain(2.0)
