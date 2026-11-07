"""Nail: the ack read-face — InboxService.consumption_state (PAC M2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import ConsumptionState, InboxService

SENDER = "agent:bobadmin:node-a:runner"
TARGET = "agent:bobadmin:node-a:worker"


def _message(message_id: str = "m1") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="wf-run-x-w1",
        sender=SENDER,
        recipient=TARGET,
        payload=b"task",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


class ConsumptionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = InboxService(
            Path(self._tmp.name) / "inbox.sqlite3", MemoryDeliveryTransport()
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def _land(self, message_id: str = "m1") -> None:
        message = _message(message_id)
        self.service.submit(message, now_ms=0)
        accepted = self.service.receive(message, now_ms=0)
        assert accepted.accepted

    def test_pending_before_consumption(self) -> None:
        self._land()
        assert self.service.consumption_state("m1") is ConsumptionState.PENDING

    def test_consumed_after_ack(self) -> None:
        self._land()
        result = self.service.ack(TARGET, "m1")
        assert result.acknowledged
        assert self.service.consumption_state("m1") is ConsumptionState.CONSUMED

    def test_unknown_message_is_unknown_not_guessed(self) -> None:
        assert self.service.consumption_state("never-seen") is ConsumptionState.UNKNOWN

    def test_consumption_survives_inbox_prune_via_fetched_record(self) -> None:
        self._land()
        self.service.ack(TARGET, "m1")
        # Prune the consumed row: the FETCHED terminal record must keep the
        # verdict CONSUMED rather than decaying to UNKNOWN.
        with self.service._db:  # test-only: emulate the retention prune
            self.service._db.execute("DELETE FROM inbox WHERE message_id = ?", ("m1",))
        assert self.service.consumption_state("m1") is ConsumptionState.CONSUMED

    def test_defer_direct_enqueues_without_touching_the_transport(self) -> None:
        """#99 guardrail: a deferred submit must make ZERO transport calls —
        no direct delivery attempt, no custody attempt — so the workflow
        tick can never block the IPC request lock on network I/O."""
        transport = MemoryDeliveryTransport()
        transport.set_online(TARGET, True)
        transport.advertise_mailbox("node-mailbox")
        service = InboxService(
            Path(self._tmp.name) / "deferred.sqlite3", transport
        )
        result = service.submit(_message("m-defer"), now_ms=0, defer_direct=True)
        assert result.accepted and result.queued
        assert transport.delivered == []  # no direct attempt
        assert transport.custody_transfers == []  # no custody attempt
        # ...and the existing pump delivers it on a later pass.
        transport.confirm_direct("m-defer")
        service.retry_due(now_ms=1_000)
        assert transport.delivered == ["m-defer"]
        service.close()


if __name__ == "__main__":
    unittest.main()
