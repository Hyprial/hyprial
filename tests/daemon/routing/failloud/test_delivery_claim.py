"""UUID addressing §2.3: an unclaimed self-node recipient fails terminally.

The 2026-10-23 incident shape: a self-node ``agent:`` recipient nothing on
this node claims was committed with ``ACK_RECEIVED`` and parked in the inbox
forever.  With the claim predicate injected through wiring, every path into
the local receive -- direct local delivery, the custody/mailbox pump and
network ingress -- ends such a message in a terminal failure instead.  With
no predicate (the switch off) the inbox behaves exactly as before.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    HoldReason,
    InboxMessage,
    InboxService,
    LocalFirstDeliveryTransport,
    MemoryDeliveryTransport,
    TerminalState,
)
from tests.daemon.routing.failloud.conftest import NODE, OWNER, name_uri
from tests.support.actor_hosts import new_actor_host

SENDER = name_uri("a.bbbbbbbbbbbbbbbbbbbbbbbbbb")
GHOST = name_uri("ghost")
CLAIMED = name_uri("a.cccccccccccccccccccccccccc")


def _claimed(recipient: str) -> bool:
    return recipient != GHOST


def _message(recipient: str, message_id: str, sender: str = SENDER) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-claim",
        sender=sender,
        recipient=recipient,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _wired(tmp_path: Path, *, claim=_claimed):
    network = MemoryDeliveryTransport()
    delivery = LocalFirstDeliveryTransport(
        network,
        origin_node=NODE,
        local_node_id=NODE,
        local_owner=OWNER,
        recipient_claimed=claim,
    )
    inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        delivery,
        node_id=NODE,
        recipient_claimed=claim,
    )
    delivery.bind_receiver(inbox.receive)
    return inbox, delivery, network


def _record(inbox, message_id: str, sender: str = SENDER):
    (record,) = inbox.delivery_status_records(sender, message_id=message_id)
    return record


def test_local_direct_delivery_to_an_unclaimed_recipient_is_terminal(
    tmp_path: Path,
) -> None:
    inbox, _delivery, network = _wired(tmp_path)
    try:
        result = inbox.submit(_message(GHOST, "m-direct"), now_ms=1)

        assert (result.accepted, result.queued, result.code) == (
            False,
            False,
            "TARGET_UNRESOLVED",
        )
        assert inbox.pending_messages(GHOST) == ()
        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 1
        record = _record(inbox, "m-direct")
        assert record.state is TerminalState.EXPIRED
        assert record.reason == "TARGET_UNRESOLVED"
        assert network.delivered == []
    finally:
        inbox.close()


def test_custody_pump_to_an_unclaimed_recipient_is_terminal(tmp_path: Path) -> None:
    inbox, _delivery, _network = _wired(tmp_path)
    try:
        assert inbox.accept_custody(
            _message(GHOST, "m-custody"), mailbox_node=NODE, now_ms=1
        ).acknowledged
        (outcome,) = inbox.retry_custody_due(now_ms=2)

        assert (outcome.accepted, outcome.queued, outcome.code) == (
            False,
            False,
            "TARGET_UNRESOLVED",
        )
        assert inbox.custody_count() == 0
        assert inbox.pending_messages(GHOST) == ()
        assert inbox.dlq_count() == 1
        record = _record(inbox, "m-custody")
        assert record.state is TerminalState.EXPIRED
        assert record.reason == "TARGET_UNRESOLVED"
    finally:
        inbox.close()


def test_outbox_pump_to_an_unclaimed_recipient_reports_the_terminal_code(
    tmp_path: Path,
) -> None:
    inbox, _delivery, _network = _wired(tmp_path)
    try:
        queued = inbox.submit(_message(GHOST, "m-pump"), now_ms=1, defer_direct=True)
        assert queued.queued is True

        (outcome,) = inbox.retry_due(now_ms=2)

        assert (outcome.accepted, outcome.queued, outcome.code) == (
            False,
            False,
            "TARGET_UNRESOLVED",
        )
        assert inbox.outbox_count() == 0
        assert inbox.dlq_count() == 1
    finally:
        inbox.close()


def test_network_ingress_to_an_unclaimed_recipient_is_refused(tmp_path: Path) -> None:
    remote_sender = "agent:op:far-node:a.dddddddddddddddddddddddddd"
    inbox, _delivery, _network = _wired(tmp_path)
    try:
        result = inbox.receive(_message(GHOST, "m-remote", sender=remote_sender))

        assert result.accepted is False
        assert result.acknowledged is False
        assert result.code == "TARGET_UNRESOLVED"
        assert inbox.pending_messages(GHOST) == ()
        record = _record(inbox, "m-remote", sender=remote_sender)
        assert record.state is TerminalState.REFUSED
        assert record.reason == "TARGET_UNRESOLVED"
        (notice,) = inbox.take_pending_link_notices()
        assert notice.target_node == "far-node"
        assert notice.notice.recipient == remote_sender
    finally:
        inbox.close()


def test_a_redelivered_accepted_message_still_acks_as_a_duplicate(
    tmp_path: Path,
) -> None:
    claims = {"ghost": True}
    inbox, _delivery, _network = _wired(
        tmp_path, claim=lambda recipient: recipient != GHOST or claims["ghost"]
    )
    try:
        assert inbox.receive(_message(GHOST, "m-dup")).accepted
        claims["ghost"] = False

        again = inbox.receive(_message(GHOST, "m-dup"))

        assert again.duplicate is True
        assert again.acknowledged is True
    finally:
        inbox.close()


def test_the_claim_is_asked_outside_the_receive_write_transaction(
    tmp_path: Path,
) -> None:
    """The predicate reads the agent registry, desired state and presence;
    it must not run while receive holds the inbox SQLite write lock."""

    held: list[bool] = []
    services: list[InboxService] = []

    def claim(recipient: str) -> bool:
        held.append(services[0]._db.in_transaction)
        return recipient != GHOST

    inbox, _delivery, _network = _wired(tmp_path, claim=claim)
    services.append(inbox)
    remote_sender = "agent:op:far-node:a.dddddddddddddddddddddddddd"
    try:
        assert inbox.receive(_message(CLAIMED, "m-in", sender=remote_sender)).accepted
        refused = inbox.receive(_message(GHOST, "m-out", sender=remote_sender))
        again = inbox.receive(_message(CLAIMED, "m-in", sender=remote_sender))

        assert refused.code == "TARGET_UNRESOLVED"
        assert (again.duplicate, again.acknowledged) == (True, True)
        assert held and not any(held)
    finally:
        inbox.close()


def test_claimed_recipient_and_no_predicate_keep_ack_received(tmp_path: Path) -> None:
    inbox, _delivery, _network = _wired(tmp_path)
    plain, _plain_delivery, _ = _wired(tmp_path / "plain", claim=None)
    try:
        inbox.submit(_message(CLAIMED, "m-claimed"), now_ms=1)
        plain.submit(_message(GHOST, "m-legacy"), now_ms=1)

        for service, recipient, message_id in (
            (inbox, CLAIMED, "m-claimed"),
            (plain, GHOST, "m-legacy"),
        ):
            assert [m.message_id for m in service.pending_messages(recipient)] == [
                message_id
            ]
            record = _record(service, message_id)
            assert record.state is TerminalState.FETCHED
            assert record.reason == HoldReason.ACK_RECEIVED.value
    finally:
        inbox.close()
        plain.close()


def test_unclaimed_recipient_is_never_handed_to_a_custody_mailbox(
    tmp_path: Path,
) -> None:
    network = MemoryDeliveryTransport()
    network.online_mailboxes = lambda: ("mailbox-node",)  # type: ignore[method-assign]
    delivery = LocalFirstDeliveryTransport(
        network,
        local_node_id=NODE,
        local_owner=OWNER,
        recipient_claimed=_claimed,
    )

    assert delivery.transfer_custody("mailbox-node", _message(GHOST, "m-x")) is False
    assert delivery.transfer_custody("mailbox-node", _message(CLAIMED, "m-y")) is (
        network.transfer_custody("mailbox-node", _message(CLAIMED, "m-y"))
    )


def test_actor_authority_submit_to_an_unclaimed_recipient_is_terminal(
    tmp_path: Path,
) -> None:
    network = MemoryDeliveryTransport()
    delivery = LocalFirstDeliveryTransport(
        network,
        origin_node=NODE,
        local_node_id=NODE,
        local_owner=OWNER,
        recipient_claimed=_claimed,
    )
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        delivery,
        SimpleNamespace(publish=lambda _event: None),
        node_id=NODE,
        service_options={"recipient_claimed": _claimed}, actor_host=new_actor_host(),
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    delivery.bind_receiver(facade.receive)
    try:
        facade.submit(_message(GHOST, "m-actor"))
        deadline = time.monotonic() + 10
        while facade.dlq_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)

        assert facade.dlq_count() == 1
        assert facade.outbox_count() == 0
        assert facade.pending_messages(GHOST) == ()
        record = _record(facade, "m-actor")
        assert record.state is TerminalState.EXPIRED
        assert record.reason == "TARGET_UNRESOLVED"
    finally:
        facade.close()


def test_daemon_claim_drives_the_actor_authority_end_to_end(
    flipped, tmp_path: Path
) -> None:
    """The real ``_local_recipient_claimed``, wired as production wires it
    and called from the inbox actor thread: an unclaimed name is terminal
    on local delivery and refused on network ingress, a live id is kept."""

    worker = flipped.agents.require("worker")
    ghost = name_uri("ghost")
    claim = flipped._local_recipient_claimed
    delivery = LocalFirstDeliveryTransport(
        MemoryDeliveryTransport(),
        origin_node=NODE,
        local_node_id=NODE,
        local_owner=OWNER,
        recipient_claimed=claim,
    )
    database = tmp_path / "authority" / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        delivery,
        SimpleNamespace(publish=lambda _event: None),
        node_id=NODE,
        service_options={"recipient_claimed": claim}, actor_host=new_actor_host(),
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    delivery.bind_receiver(facade.receive)
    try:
        facade.submit(_message(ghost, "m-ghost"))
        facade.submit(_message(worker.uri, "m-worker"))
        deadline = time.monotonic() + 10
        while (
            facade.dlq_count() == 0 or not facade.pending_messages(worker.uri)
        ) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert facade.dlq_count() == 1
        assert facade.pending_messages(ghost) == ()
        assert [m.message_id for m in facade.pending_messages(worker.uri)] == [
            "m-worker"
        ]
        assert _record(facade, "m-ghost").reason == "TARGET_UNRESOLVED"
        assert _record(facade, "m-worker").reason == HoldReason.ACK_RECEIVED.value

        remote = "agent:op:far-node:a.dddddddddddddddddddddddddd"
        refused = facade.receive(_message(ghost, "m-remote", sender=remote))
        assert (refused.accepted, refused.code) == (False, "TARGET_UNRESOLVED")
    finally:
        facade.close()
