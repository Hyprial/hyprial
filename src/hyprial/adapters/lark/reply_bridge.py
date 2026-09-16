"""Durable outbox transport for replies addressed to a Lark channel actor.

The channel actor is an inbound identity, not a general-purpose outbound
address.  The one valid outbound operation is a correlated Harness reply.  It
stays in the daemon's ordinary outbox until the named adapter worker confirms
that :class:`~hyprial.adapters.lark.adapter.LarkAdapter` sent the native reply and
persisted its reply route.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from hyprial.inbox.api import DeliveryTransport, InboxMessage

from .api import ActorTarget, HarnessDelivery

#: Legacy spelling -- historical data only after the 2026-08-24 naming
#: migration (PAC b8d2b52b6358 / NAMB efeeaf0a01ba), never a new write.
_LARK_CHANNEL_PREFIX = "channel:lark:"
#: Current spelling -- every new writer mints this one.
_LARK_ADAPTER_PREFIX = "adapter:lark:"
_LARK_REPLY_BRIDGE_PREFIXES = (_LARK_CHANNEL_PREFIX, _LARK_ADAPTER_PREFIX)
_REPLY_KEY_PREFIX = "reply:"


@runtime_checkable
class AdapterReplyClient(Protocol):
    """Daemon-side view of the supervised adapter worker fleet."""

    def reply_online(self, adapter: str) -> bool: ...

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool: ...


def lark_reply_adapter(recipient: str) -> str | None:
    """Return the adapter name for the exact reply-bridge URI shape.

    Dual-read: accepts both the legacy ``channel:lark:`` spelling
    (historical inbound identities, never rewritten) and the current
    ``adapter:lark:`` spelling (every new mint) -- both name the same
    reply-bridge identity.
    """

    for prefix in _LARK_REPLY_BRIDGE_PREFIXES:
        if recipient.startswith(prefix):
            adapter = recipient.removeprefix(prefix)
            if not adapter or ":" in adapter:
                return None
            return adapter
    return None


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

    def deliver(self, message: InboxMessage) -> bool:
        adapter = lark_reply_adapter(message.recipient)
        if adapter is None:
            return self._inner.deliver(message)
        delivery = _delivery(message)
        if delivery is None:
            return False
        return self._adapter_client.deliver_reply(adapter, delivery)

    def confirm_fetch(self, message: InboxMessage) -> bool:
        if lark_reply_adapter(message.recipient) is not None:
            return False
        confirm = getattr(self._inner, "confirm_fetch", None)
        return bool(confirm(message)) if callable(confirm) else False

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
