"""Wire option B end to end: two actor-owned inboxes over a real Zenoh link.

The sender composes its network transport as the daemon does
(``stamped_network_transport``); the recipient receives through
``ZenohInboxEndpoint`` -> ``DeliveryCustodyFacade`` -> inbox actor ->
``receive`` / ``accept_custody``.  The frame on the wire carries the payload
envelope; what either inbox stores never does.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
    RetryPolicy,
    ZenohInboxEndpoint,
    stamped_network_transport,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryTransport
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.kernel import mint_thread_id
from tests.support.actor_hosts import new_actor_host

THREAD = mint_thread_id(b"payload-zenoh-thread")
SENDER = "agent:op:mac:driver"
RECIPIENT = "agent:op:pc:reader"
PAYLOAD = json.dumps(
    {"message": "over zenoh", "origin": {"via": "session"}}, separators=(",", ":")
).encode()


class _Presence:
    def __init__(self, actor: str | None = None, mailbox: str | None = None) -> None:
        self._actor = actor
        self._mailbox = mailbox

    def actor_online(self, actor: str) -> bool:
        return actor == self._actor

    def online_mailboxes(self) -> tuple[str, ...]:
        return (self._mailbox,) if self._mailbox else ()


class _DiscardEvents:
    def publish(self, _event: object) -> None:
        pass


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _facade(database: Path, transport: DeliveryTransport, node: str) -> DeliveryCustodyFacade:
    coordinator = DeliveryCustodyCoordinator(
        database, transport, _DiscardEvents(), retry_policy=RetryPolicy(), node_id=node, actor_host=new_actor_host()
    )
    return DeliveryCustodyFacade(coordinator, database)


def _message() -> InboxMessage:
    return InboxMessage(
        message_id="m-zenoh-1",
        conversation_id="c-zenoh",
        sender=SENDER,
        recipient=RECIPIENT,
        payload=PAYLOAD,
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=time.time_ns() // 1_000_000,
    ).with_envelope(thread_id=THREAD)


@contextmanager
def _link() -> Iterator[tuple[ZenohTransport, ZenohTransport]]:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    receiver = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    sender = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    try:
        yield receiver, sender
    finally:
        sender.close()
        receiver.close()


def _wait(predicate: object, what: str) -> None:
    deadline = time.monotonic() + 5.0
    while not predicate():  # type: ignore[operator]
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _rows(database: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def test_a_direct_delivery_adopts_the_envelope_through_the_composed_path(
    tmp_path: Path,
) -> None:
    with _link() as (receiver_session, sender_session):
        receiver = _facade(tmp_path / "receiver.sqlite3", MemoryDeliveryTransport(), "pc")
        endpoint = ZenohInboxEndpoint(receiver_session, receiver, RECIPIENT)
        sender = _facade(
            tmp_path / "sender.sqlite3",
            stamped_network_transport(
                sender_session, _Presence(RECIPIENT), receipt_timeout=2.0, origin_node="mac"
            ),
            "mac",
        )
        try:
            time.sleep(0.5)
            message = _message()
            submitted = sender.submit(message, now_ms=message.created_at_ms)
            assert submitted.accepted
            _wait(lambda: receiver.pending_messages(RECIPIENT), "the delivery")
            (stored,) = receiver.pending_messages(RECIPIENT)
            assert stored.payload == PAYLOAD
            assert stored.thread_id == THREAD
            assert _rows(tmp_path / "sender.sqlite3", "SELECT payload FROM sender_copies") == [
                (PAYLOAD,)
            ]
        finally:
            sender.close()
            endpoint.close()
            receiver.close()


def test_a_custody_transfer_adopts_the_envelope_through_the_composed_path(
    tmp_path: Path,
) -> None:
    with _link() as (holder_session, sender_session):
        holder = _facade(tmp_path / "holder.sqlite3", MemoryDeliveryTransport(), "pc")
        endpoint = ZenohInboxEndpoint(holder_session, holder, "pc", mailbox_node="pc")
        sender = _facade(
            tmp_path / "sender.sqlite3",
            stamped_network_transport(
                sender_session, _Presence(mailbox="pc"), receipt_timeout=2.0, origin_node="mac"
            ),
            "mac",
        )
        try:
            time.sleep(0.5)
            message = _message()
            assert sender.submit(message, now_ms=message.created_at_ms).accepted
            query = "SELECT payload, thread_id FROM custody"
            _wait(lambda: _rows(tmp_path / "holder.sqlite3", query), "the custody row")
            assert _rows(tmp_path / "holder.sqlite3", query) == [(PAYLOAD, THREAD)]
        finally:
            sender.close()
            endpoint.close()
            holder.close()
