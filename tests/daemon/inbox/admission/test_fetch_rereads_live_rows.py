"""The actor fetch re-reads live rows after the candidate query (review B3).

Moved from ``tests/daemon/links/admission/test_pending_list_link_filter.py``
in T1b (CM-PLAN §5.1): the race it pins is the inbox actor's own candidate
query versus a consuming ack between the two, not anything the link gate
did.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from tests.support.actor_hosts import new_actor_host

READER = "agent:op:node-1:reader"
SENDER = "agent:op:node-1:driver"


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(message_id: str, *, recipient: str = READER) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message": "hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_fetch_rereads_live_rows_after_candidate_query(tmp_path: Path) -> None:
    facade = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            tmp_path / "race.sqlite3",
            MemoryDeliveryTransport(),
            _Events(),
            actor_host=new_actor_host(),
        ),
        tmp_path / "race.sqlite3",
    )
    try:
        assert facade.receive(_message("m-gone")).accepted
        candidates = [m.message_id for m in facade.pending_messages(READER)]
        assert candidates == ["m-gone"]
        facade.ack(READER, "m-gone")
        assert facade.fetch_pending(READER, only=frozenset(candidates)) == ()
    finally:
        facade.close()
