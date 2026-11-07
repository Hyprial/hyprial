from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.actor import InboxAuthorityUnavailable
from hyprial.daemon.impl.inbox.actor.internal import _ActorOwnedInboxService
from hyprial.daemon.impl.inbox.contracts.ports import ReceiveMessageCommand
from hyprial.daemon.impl.inbox.links.zenoh_link import ZenohInboxEndpoint, encode_delivery_frame


class _Session:
    def subscribe(self, *args):
        return SimpleNamespace(close=lambda: None)

    declare_queryable = subscribe


def test_rx_admission_does_not_wait_for_commit_and_never_forges_receipt(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = _ActorOwnedInboxService.receive

    def slow_receive(self, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_ActorOwnedInboxService, "receive", slow_receive)
    db = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        db,
        MemoryDeliveryTransport(),
        SimpleNamespace(publish=lambda event: None),
        mailbox_capacity=1,
    )
    facade = DeliveryCustodyFacade(coordinator, db)
    endpoint = ZenohInboxEndpoint(_Session(), facade, "recipient")
    message = InboxMessage(
        message_id="ingress-message",
        conversation_id="conversation",
        sender="agent:owner:node:sender",
        recipient="recipient",
        payload=b"test",
        intent="message",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    sample = SimpleNamespace(payload=encode_delivery_frame(message))
    try:
        endpoint._on_inbox(sample)
        assert entered.wait(1)
        assert endpoint._receipt("receipt/" + message.message_id) is None
        endpoint._on_inbox(sample)
        with pytest.raises(InboxAuthorityUnavailable, match="overloaded"):
            endpoint._on_inbox(sample)
    finally:
        release.set()
        # This waits at an explicit protocol edge, not in the RX callback.
        coordinator.drain(timeout=3)
        endpoint.close()
    from hyprial.daemon.impl.inbox.authority import InboxReadProjection

    assert InboxReadProjection(db).has_received(message.message_id)


def test_ingress_admission_returns_closing_without_synchronous_call():
    facade = object.__new__(DeliveryCustodyFacade)
    commands = []

    def submit(command):
        commands.append(command)
        return PortAdmission.CLOSING

    facade._coordinator = SimpleNamespace(submit=submit)
    message = InboxMessage(
        message_id="ingress-message",
        conversation_id="conversation",
        sender="agent:owner:node:sender",
        recipient="recipient",
        payload=b"test",
        intent="message",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    command = ReceiveMessageCommand("ingress-1", message)
    assert facade.admit_ingress(command) is PortAdmission.CLOSING
    assert commands == [command]
    with pytest.raises(TypeError):
        facade.admit_ingress(object())
