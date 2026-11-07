"""Actor restart preserves online ACK-or-TTL retry state."""

import time

from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from tests.daemon.outbox.lifecycle.test_outbox_reconcile_submit_timeout import RECIPIENT, _Events, _HalfOpenCommittedTransport, _message


def test_actor_restart_preserves_online_retry_without_confirmation_state(tmp_path):
    transport = _HalfOpenCommittedTransport()
    transport.set_online(RECIPIENT, True)
    database = tmp_path / "actor-ack-or-ttl.sqlite3"
    owner = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(owner, database)
    try:
        start = time.time_ns() // 1_000_000
        facade.submit(
            _message("actor-ack-or-ttl"), now_ms=start, defer_direct=True
        )
        [pending] = facade.retry_due(now_ms=start)
        assert pending.accepted and pending.queued
        item = facade.outbox_item("actor-ack-or-ttl")
        assert item.confirmation_started_at_ms is None
        assert item.confirmation_attempts == 0
        assert transport.notices == []

        facade.close()
        owner = DeliveryCustodyCoordinator(database, transport, _Events())
        facade = DeliveryCustodyFacade(owner, database)
        restored = facade.outbox_item("actor-ack-or-ttl")
        [result] = facade.retry_due(now_ms=restored.next_attempt_ms)
        assert result.accepted and result.queued
        assert facade.outbox_item("actor-ack-or-ttl").attempts == 2
        assert facade.dlq_count() == 0
        assert transport.notices == []
    finally:
        facade.close()
