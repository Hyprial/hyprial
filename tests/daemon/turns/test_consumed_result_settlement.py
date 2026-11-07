from __future__ import annotations

import queue
import sqlite3
from pathlib import Path

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.pac.contracts.delivery import ALREADY_CONSUMED, EXPIRED
from hyprial.daemon.impl.runtime.results import _BridgeResultsMixin


class _SettlementProbe(_BridgeResultsMixin):
    def __init__(
        self, state_dir: Path, inbox: InboxService, workflow_outcome=None
    ) -> None:
        self.state_dir = state_dir
        self.inbox = inbox
        self._queued_delivery_holds: dict[str, object] = {}
        self._pending_workflow_attempts: dict[str, object] = {}
        self._pending_workflow_results: dict[str, object] = {}
        self._pending_settle_results: dict[str, object] = {}
        self._workflow_outcome = workflow_outcome

    @staticmethod
    def _finish_attempt(_delivery_id: str) -> None:
        return None

    @staticmethod
    def _clock_ms() -> int:
        return 2

    @staticmethod
    def _forget_turn_hook_delivery(_delivery_id: str) -> None:
        return None


def _row(database: Path, message_id: str) -> tuple[int, int | None, int]:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT consumed, acknowledged_at_ms FROM inbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    finally:
        connection.close()
    assert row is not None
    return int(row[0]), row[1], int(count)


def test_already_consumed_interrupted_result_is_a_settled_noop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    recipient = "agent:alice:mac:worker"
    message = InboxMessage(
        message_id="consumed-result",
        conversation_id="conversation",
        sender="agent:alice:mac:requester",
        recipient=recipient,
        payload=b"opaque",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = InboxService(database, MemoryDeliveryTransport())
    assert inbox.receive(message, now_ms=1).acknowledged
    assert inbox.ack(recipient, message.message_id).acknowledged
    before = _row(database, message.message_id)
    try:
        settled = _SettlementProbe(tmp_path, inbox)._settle_claimed_result_business(
            HarnessResult(
                message.message_id,
                recipient,
                HarnessResultStatus.INTERRUPTED,
                failure_code=ALREADY_CONSUMED,
            )
        )

        assert settled is True
        assert _row(database, message.message_id) == before
        assert inbox.pending_all() == ()
        assert inbox.dispatchable_messages(recipient, now_ms=2) == ()
        assert inbox.harness_failure_settlement(message.message_id) is None
        assert inbox.harness_failure_attempts(message.message_id) == ()
        assert inbox.outbox_items() == ()
    finally:
        inbox.close()


def test_workflow_skip_results_do_not_enter_the_pac_outcome_hook(
    tmp_path: Path,
) -> None:
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    outcomes: list[HarnessResult] = []
    probe = _SettlementProbe(tmp_path, inbox, outcomes.append)
    try:
        for failure_code in (ALREADY_CONSUMED, EXPIRED):
            result = HarnessResult(
                f"workflow-{failure_code}",
                "agent:alice:mac:worker",
                HarnessResultStatus.INTERRUPTED,
                failure_code=failure_code,
            )
            assert probe._settle_claimed_result_business(result) is True
        assert outcomes == []
    finally:
        inbox.close()


def test_batched_workflow_skip_does_not_enter_the_pac_outcome_hook(
    tmp_path: Path,
) -> None:
    result = HarnessResult(
        "workflow-consumed-batch",
        "agent:alice:mac:worker",
        HarnessResultStatus.INTERRUPTED,
        failure_code=ALREADY_CONSUMED,
    )

    class Harnesses:
        def drain_results(self) -> tuple[HarnessResult, ...]:
            return (result,)

    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    outcomes: list[HarnessResult] = []
    probe = _SettlementProbe(tmp_path, inbox, outcomes.append)
    probe.harnesses = Harnesses()
    probe._forward_outcomes = queue.Queue()
    probe._logger = None
    try:
        assert probe._complete_harness_results() == 0
        assert outcomes == []
    finally:
        inbox.close()
