"""Durable outbox transport for replies addressed to a Lark channel actor.

The channel actor is an inbound identity, not a general-purpose outbound
address.  The one valid outbound operation is a correlated Harness reply.  It
stays in the daemon's ordinary outbox until the named adapter worker confirms
that :class:`~hyprial.daemon.impl.adapters.lark.inbound.adapter.LarkAdapter` sent the native reply and
persisted its reply route.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from hyprial.daemon.impl.inbox.contracts.api import DeliveryTransport, InboxMessage

from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ActorTarget,
    HarnessDelivery,
)

_LARK_ADAPTER_PREFIX = "adapter:lark:"
_REPLY_KEY_PREFIX = "reply:"


@runtime_checkable
class AdapterReplyClient(Protocol):
    """Daemon-side view of the supervised adapter worker fleet."""

    def reply_online(self, adapter: str) -> bool: ...

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool: ...


def lark_reply_adapter(recipient: str) -> str | None:
    """Return the adapter name for the exact reply-bridge URI shape.

    Only ``adapter:lark:<name>``; the retired ``channel:lark:`` spelling is
    not a reply bridge.
    """

    if not recipient.startswith(_LARK_ADAPTER_PREFIX):
        return None
    adapter = recipient.removeprefix(_LARK_ADAPTER_PREFIX)
    if not adapter or ":" in adapter:
        return None
    return adapter


def lark_alarm_destination(recipient: str) -> str | None:
    """The adapter whose chat a human-facing notice must be replied into.

    Allen 2026-10-13 (in-DM alarm ruling): a fail-loud notice for a
    human-originated conversation is replied into the chat where the request
    came from, and the adapter owns that chat handle.  Only the exact
    reply-bridge shape (:func:`lark_reply_adapter`) names one; a retired
    ``route:<adapter>:<route>`` address (I4a, 2026-11-24) never requests
    anything.  Anything else -- a ``user:`` address, a bare actor name -- has
    no Lark chat handle here; the caller holds the notice for retry rather
    than guessing a chat.
    """

    return lark_reply_adapter(recipient)


def _reply_to(message: InboxMessage, body: object) -> str | None:
    if isinstance(body, dict):
        value = body.get("replyTo")
        if isinstance(value, str) and value:
            return value
    key = message.idempotency_key
    if isinstance(key, str) and key.startswith(_REPLY_KEY_PREFIX):
        value = key.removeprefix(_REPLY_KEY_PREFIX)
        if value:
            return value
    return None


def _delivery(message: InboxMessage) -> HarnessDelivery | None:
    if message.intent != "reply":
        return None
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    text = body.get("message")
    reply_to = _reply_to(message, body)
    if not isinstance(text, str) or not text or reply_to is None:
        return None
    actor = ActorTarget(
        actor_id=message.sender,
        actor_key=message.sender,
        display_name=message.sender,
    )
    return HarnessDelivery(
        delivery_id=message.message_id,
        message_id=message.message_id,
        reply_to=reply_to,
        from_actor=actor,
        text=text,
    )


class LarkReplyBridgeTransport:
    """Intercept correlated Lark replies; delegate every other address."""

    def __init__(
        self, inner: DeliveryTransport, adapter_client: AdapterReplyClient
    ) -> None:
        self._inner = inner
        self._adapter_client = adapter_client

    def is_online(self, recipient: str) -> bool:
        adapter = lark_reply_adapter(recipient)
        if adapter is None:
            return self._inner.is_online(recipient)
        return self._adapter_client.reply_online(adapter)

    def ack_received(self, message: InboxMessage) -> bool:
        confirm = getattr(self._inner, "ack_received", None)
        return bool(confirm(message)) if callable(confirm) else False

    def deliver(self, message: InboxMessage) -> bool:
        adapter = lark_reply_adapter(message.recipient)
        if adapter is None:
            return self._inner.deliver(message)
        delivery = _delivery(message)
        if delivery is None:
            return False
        return self._adapter_client.deliver_reply(adapter, delivery)

    def online_mailboxes(self) -> tuple[str, ...]:
        return self._inner.online_mailboxes()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        # A channel actor belongs to this daemon's supervised worker.  Moving
        # that private control operation to another node cannot make it
        # deliverable, so the ordinary InboxService only calls this for
        # non-bridge traffic.
        if lark_reply_adapter(message.recipient) is not None:
            return False
        return self._inner.transfer_custody(mailbox, message)

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        """System notices are not correlated Lark replies; keep them receipt-free."""

        return self._inner.deliver_notice(node, message)

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        """Progress events are not Lark replies either; delegate untouched."""

        return self._inner.deliver_progress(node, message)


__all__ = [
    "AdapterReplyClient",
    "LarkReplyBridgeTransport",
    "lark_reply_adapter",
]
