"""Zenoh delivery link for the transport-neutral durable inbox state machine."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace

from hyprial.transport import KeySpace, PresenceView, Registration, TransportSession

from .api import DeliveryLifecycle, InboxMessage
from .pull import TerminalState, merge_delivery_status, query_delivery_status
from .service import InboxService


def publish_fetch_receipt(
    session: TransportSession,
    message: InboxMessage,
    *,
    keys: KeySpace | None = None,
) -> None:
    """Push one idempotent fetch receipt to the sender's daemon."""

    space = keys or KeySpace()
    session.put(space.fetch_receipt(message.sender, message.message_id), b"ack")


def encode_delivery_frame(message: InboxMessage) -> bytes:
    return json.dumps(
        {
            "schema": "hyprial-delivery-frame/v1",
            "message_id": message.message_id,
            "conversation_id": message.conversation_id,
            "sender": message.sender,
            "recipient": message.recipient,
            "intent": message.intent,
            "lifecycle": message.lifecycle.value,
            "idempotency_key": message.idempotency_key,
            "created_at_ms": message.created_at_ms,
            "payload": base64.b64encode(message.payload).decode("ascii"),
            **(
                {"expires_at_ms": message.expires_at_ms}
                if message.expires_at_ms is not None
                else {}
            ),
            # Optional and additive: pre-stamp decoders ignore the key, and
            # pre-stamp frames simply decode to origin_node=None.
            **(
                {"origin_node": message.origin_node}
                if message.origin_node is not None
                else {}
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_delivery_frame(frame: bytes) -> InboxMessage:
    value = json.loads(frame)
    if value.get("schema") != "hyprial-delivery-frame/v1":
        raise ValueError("unsupported delivery frame schema")
    return InboxMessage(
        message_id=str(value["message_id"]),
        conversation_id=str(value["conversation_id"]),
        sender=str(value["sender"]),
        recipient=str(value["recipient"]),
        payload=base64.b64decode(value["payload"], validate=True),
        intent=str(value["intent"]),
        lifecycle=DeliveryLifecycle(str(value["lifecycle"])),
        idempotency_key=value.get("idempotency_key"),
        created_at_ms=int(value["created_at_ms"]),
        expires_at_ms=(
            int(value["expires_at_ms"])
            if value.get("expires_at_ms") is not None
            else None
        ),
        origin_node=(
            str(value["origin_node"]) if value.get("origin_node") is not None else None
        ),
    )


class ZenohDeliveryTransport:
    def __init__(
        self,
        session: TransportSession,
        presence: PresenceView,
        *,
        keys: KeySpace | None = None,
        receipt_timeout: float = 1.0,
        origin_node: str | None = None,
    ) -> None:
        self._session = session
        self._presence = presence
        self._keys = keys or KeySpace()
        self._receipt_timeout = receipt_timeout
        # Stamped onto every frame this node puts on the wire (when the
        # author did not set one): the physical provenance the inbox
        # persists as origin_node.
        self._origin_node = origin_node

    def _stamp_origin(self, message: InboxMessage) -> InboxMessage:
        if self._origin_node is None or message.origin_node is not None:
            return message
        return replace(message, origin_node=self._origin_node)

    def is_online(self, recipient: str) -> bool:
        return self._presence.actor_online(recipient)

    def online_mailboxes(self) -> tuple[str, ...]:
        return self._presence.online_mailboxes()

    def deliver(self, message: InboxMessage) -> bool:
        message = self._stamp_origin(message)
        self._session.put(
            self._keys.inbox(message.recipient, message.message_id),
            encode_delivery_frame(message),
        )
        return self._await_receipt(
            self._keys.receipt(message.sender, message.message_id)
        )

    def confirm_fetch(self, message: InboxMessage) -> bool:
        """Query durable commit evidence even when the actor route is gone.

        A custody or direct publication can reach the recipient while its
        one-shot receipt is lost.  The recipient records that commit in the
        daemon-wide delivery-status ledger, whose lifetime is independent of
        the actor-scoped fetch-receipt queryable.  Consult that authoritative
        ledger before falling back to the legacy fetch marker.
        """

        report = query_delivery_status(
            self._session,
            message.sender,
            message_id=message.message_id,
            keys=self._keys,
            timeout=self._receipt_timeout,
        )
        if any(
            record.message_id == message.message_id
            and record.sender == message.sender
            and record.recipient == message.recipient
            and record.state is TerminalState.FETCHED
            for record in merge_delivery_status(report.records)
        ):
            return True

        return self._await_receipt(
            self._keys.fetch_receipt(message.sender, message.message_id)
        )

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        message = self._stamp_origin(message)
        self._session.put(
            self._keys.custody(mailbox, message.message_id),
            encode_delivery_frame(message),
        )
        return self._await_receipt(
            self._keys.custody_receipt(message.sender, message.message_id)
        )

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        """Publish once to the sender daemon, deliberately without a receipt."""

        if not self._presence.actor_online(node):
            return False
        message = self._stamp_origin(message)
        self._session.put(
            self._keys.system_notice(node, message.message_id),
            encode_delivery_frame(message),
        )
        return True

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        """Publish one progress event, deliberately without a receipt.

        Same fire-and-forget contract as ``deliver_notice``: presence-gated,
        offered once, and a silent miss is normal loss the consumer must
        tolerate (the publish hop is one of the two sanctioned sources of
        ``seq`` gaps).
        """

        if not self._presence.actor_online(node):
            return False
        message = self._stamp_origin(message)
        self._session.put(
            self._keys.progress(node, message.message_id),
            encode_delivery_frame(message),
        )
        return True

    def _await_receipt(self, key: str) -> bool:
        deadline = time.monotonic() + self._receipt_timeout
        while time.monotonic() < deadline:
            timeout = min(0.2, max(0.01, deadline - time.monotonic()))
            if any(
                sample.payload == b"ack"
                for sample in self._session.get(key, timeout=timeout)
            ):
                return True
        return False


class ZenohInboxEndpoint:
    """Recipient-owned subscriber that signs receipts only after SQLite commit."""

    def __init__(
        self,
        session: TransportSession,
        service: InboxService,
        actor: str,
        *,
        mailbox_node: str | None = None,
        notice_node: str | None = None,
        progress_node: str | None = None,
        declare_receipts: bool = True,
        consume_fetch_receipts: bool = False,
        keys: KeySpace | None = None,
    ) -> None:
        self._session = session
        self._service = service
        self._actor = actor
        self._mailbox_node = mailbox_node
        self._keys = keys or KeySpace()
        self._registrations: list[Registration] = [
            session.subscribe(self._keys.inbox_all(actor), self._on_inbox)
        ]
        if declare_receipts:
            self._registrations.extend(
                (
                    session.declare_queryable(self._keys.receipt_any(), self._receipt),
                    session.declare_queryable(
                        self._keys.fetch_receipt_any(), self._fetch_receipt
                    ),
                )
            )
        if consume_fetch_receipts:
            self._registrations.append(
                session.subscribe(
                    self._keys.fetch_receipt_any(), self._on_fetch_receipt
                )
            )
        if mailbox_node is not None:
            self._registrations.extend(
                [
                    session.subscribe(
                        self._keys.custody_all(mailbox_node), self._on_custody
                    ),
                    session.declare_queryable(
                        self._keys.custody_receipt_any(), self._custody_receipt
                    ),
                ]
            )
        if notice_node is not None:
            self._registrations.append(
                session.subscribe(
                    self._keys.system_notice_all(notice_node),
                    self._on_system_notice,
                )
            )
        if progress_node is not None:
            self._registrations.append(
                session.subscribe(
                    self._keys.progress_all(progress_node),
                    self._on_progress_event,
                )
            )

    def _on_inbox(self, sample: object) -> None:
        payload = sample.payload
        message = decode_delivery_frame(payload)
        if message.recipient != self._actor:
            return
        self._service.receive(message)

    def _on_custody(self, sample: object) -> None:
        assert self._mailbox_node is not None
        payload = sample.payload
        message = decode_delivery_frame(payload)
        self._service.accept_custody(message, mailbox_node=self._mailbox_node)

    def _on_system_notice(self, sample: object) -> None:
        self._service.receive_system_notice(decode_delivery_frame(sample.payload))

    def _on_progress_event(self, sample: object) -> None:
        self._service.receive_progress_event(decode_delivery_frame(sample.payload))

    def _on_fetch_receipt(self, sample: object) -> None:
        if sample.payload != b"ack":
            return
        key = str(sample.key).split("?", 1)[0]
        prefix = f"{self._keys.prefix}/fetch-receipt/"
        if not key.startswith(prefix):
            return
        segments = key[len(prefix) :].split("/")
        if len(segments) != 2 or not all(segments):
            return
        sender, message_id = (
            self._keys.decode_identity(segment) for segment in segments
        )
        self._service.retire_outbox_receipt(sender, message_id)

    def _receipt(self, selector: str) -> bytes | None:
        message_id = self._message_id(selector)
        return b"ack" if self._service.has_received(message_id) else None

    def _custody_receipt(self, selector: str) -> bytes | None:
        message_id = self._message_id(selector)
        return b"ack" if self._service.has_custody(message_id) else None

    def _fetch_receipt(self, selector: str) -> bytes | None:
        message_id = self._message_id(selector)
        return b"ack" if self._service.has_fetched(message_id) else None

    def _message_id(self, selector: str) -> str:
        key = selector.split("?", 1)[0]
        return self._keys.decode_identity(key.rsplit("/", 1)[-1])

    def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()
