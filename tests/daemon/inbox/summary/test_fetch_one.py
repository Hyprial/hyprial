"""T14: one ``message.read_body`` fetch reads and stamps exactly one row.

The single-row fetch is the same inbox change operation as the batch fetch;
only the predicate narrows.  Retiring the sender's outbox receipt for one row
must not touch its neighbour.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from tests.support.links.gate import PermissiveLinkGate

READER = "agent:op:node-1:reader"


class _Events:
    def publish(self, _event: object) -> None:
        pass


def _message(message_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender="agent:op:node-1:driver",
        recipient=READER,
        payload=json.dumps({"message": message_id}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _seed(service: InboxService, message_id: str) -> None:
    assert service.submit(_message(message_id)).accepted
    assert service.receive(_message(message_id)).accepted


def test_fetch_pending_by_message_id_stamps_one_row(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        _seed(service, "x")
        _seed(service, "y")
        assert [item.message.message_id for item in service.outbox_items()] == ["x", "y"]

        fetched = service.fetch_pending(READER, only=frozenset({"x"}))

        assert [message.message_id for message in fetched] == ["x"]
        assert service.has_fetched("x")
        assert not service.has_fetched("y")
        assert [item.message.message_id for item in service.outbox_items()] == ["y"]
    finally:
        service.close()


def test_facade_fetch_pending_carries_the_message_id(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    slot = LinkAdmission()
    slot.bind(PermissiveLinkGate())
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        node_id="node-1",
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        for message_id in ("x", "y"):
            assert facade.submit(_message(message_id)).accepted
            assert facade.receive(_message(message_id)).accepted

        # WP4 semantics: the single-row pull is the same actor command with a
        # narrowed candidate set (``FetchPendingCommand.only``); the row is
        # re-read and admitted inside the actor before it is stamped.
        fetched = facade.fetch_pending(READER, only=frozenset({"y"}))

        assert [message.message_id for message in fetched] == ["y"]
        assert facade.has_fetched("y")
        assert not facade.has_fetched("x")
        assert [item.message.message_id for item in facade.outbox_items()] == ["x"]
    finally:
        facade.close()
