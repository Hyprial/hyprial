# Decision notes (conversation plan slice H1, 2026-11-18):
# - Every exchange between the reserved _turn-hooks actor and one handler
#   shares one conversation: the default conversation of that pair
#   (conversations.ids), derived from the exact sender and recipient strings
#   the request carries.  Before, each exchange minted turn-hook:<uuid>, so
#   the recorder opened one conversation per hook invocation.
# - A reply is matched to its request by the request's message id, read from
#   the reply body's replyTo (the daemon's mechanism answer and message.reply
#   both write it), plus the handler as sender.  The conversation id no longer
#   identifies an exchange.  A reply without replyTo is not a reply to any
#   exchange: guessing by "the only exchange in flight" would hand a late
#   reply to a later exchange.
# - The default id is computed through a lazy import, as the send path does:
#   harnesses may load conversations, but a module-level import would make
#   every importer of this module load the conversations package.  Production
#   wiring uses that default; conversation_id_for exists for tests.
# - Request ids, the idempotency key, ONLINE_ONLY delivery and the 10 ms poll
#   are unchanged.
# - Sweep: a pending row of the callback actor that answers no request in
#   flight in this invoker is acked when it names a hook request in replyTo
#   (starts with turn-hook-), sits in a pair conversation this invoker has
#   issued requests in, or sits in a legacy turn-hook:<uuid> conversation.
#   This keeps the old rule that every row of a hook conversation which is not
#   an in-flight reply is retired: replies without replyTo, replies naming an
#   unknown id and fail-loud notices addressed to the callback actor.  The
#   legacy branch retires rows written before this slice and replies to
#   requests sent by an older build.  Rows of other conversations stay.
# - Retiring a request that missed its deadline stays advisory: the timeout
#   is the authoritative outcome, so a failed ack or a failed final sweep
#   never replaces it.  ONLINE_ONLY prevents offline retry accumulation; the
#   ack bounds the same-daemon handler case and the final sweep catches a
#   reply that raced the timeout.
"""Hook exchange transport: one bounded request/reply through the actor inbox."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage, InboxPort
from hyprial.kernel import parse_agent_uri

from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    HOOK_REQUEST_MARKER,
    _HOOK_CONVERSATION_PREFIX,
    _HOOK_REQUEST_ID_PREFIX,
)


def _pair_default_conversation(sender: str, recipient: str) -> str:
    """The default conversation of the two ends of a hook exchange."""

    from hyprial.daemon.impl.conversations.ids import (
        default_conversation_id,
        endpoint_key,
    )

    return default_conversation_id(endpoint_key(sender), endpoint_key(recipient))


class InboxTurnHookInvoker:
    """Exchange hook events through the ordinary actor inbox."""

    def __init__(
        self,
        inbox: InboxPort,
        *,
        callback_actor: str,
        clock_ms: Callable[[], int] | None = None,
        conversation_id_for: Callable[[str, str], str] | None = None,
    ) -> None:
        if parse_agent_uri(callback_actor) is None:
            raise ValueError("hook callback actor must be a canonical agent URI")
        self._port = inbox
        self._callback_actor = callback_actor
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._conversation_id_for = conversation_id_for or _pair_default_conversation
        self._active_requests: set[str] = set()
        self._issued_conversations: set[str] = set()
        self._active_lock = threading.Lock()

    def notify(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> None:
        """Submit one handler request without waiting for or reading its reply."""

        del timeout_seconds
        message = self._request(handler, event, exchange_id=uuid4().hex)
        submitted = self._port.submit(message)
        if not submitted.accepted:
            raise RuntimeError(submitted.code or "hook request was rejected")

    def invoke(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> str | None:
        message = self._request(handler, event, exchange_id=uuid4().hex)
        request_id = message.message_id
        with self._active_lock:
            self._active_requests.add(request_id)
        try:
            submitted = self._port.submit(message)
        except Exception:
            with self._active_lock:
                self._active_requests.discard(request_id)
            raise
        if not submitted.accepted:
            with self._active_lock:
                self._active_requests.discard(request_id)
            raise RuntimeError(submitted.code or "hook request was rejected")
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                reply = self._reply_and_sweep_stale(request_id, handler)
                if reply is not None:
                    text = _message_text(reply)
                    self._port.ack(self._callback_actor, reply.message_id)
                    return text
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        finally:
            with self._active_lock:
                self._active_requests.discard(request_id)

        try:
            self._port.ack(handler, request_id)
        except Exception:
            pass
        try:
            self._reply_and_sweep_stale(request_id, handler)
        except Exception:
            pass
        raise TimeoutError("hook handler timed out")

    def _request(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        exchange_id: str,
    ) -> InboxMessage:
        conversation_id = self._conversation_id_for(self._callback_actor, handler)
        with self._active_lock:
            self._issued_conversations.add(conversation_id)
        return InboxMessage(
            message_id=f"{_HOOK_REQUEST_ID_PREFIX}{exchange_id}",
            conversation_id=conversation_id,
            sender=self._callback_actor,
            recipient=handler,
            payload=json.dumps(
                {
                    "message": json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    HOOK_REQUEST_MARKER: True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=self._clock_ms(),
            idempotency_key=f"turn-hook:{exchange_id}",
        )

    def _reply_and_sweep_stale(
        self, request_id: str, handler: str
    ) -> InboxMessage | None:
        messages = self._port.pending_messages(self._callback_actor)
        with self._active_lock:
            active = frozenset(self._active_requests)
            issued = frozenset(self._issued_conversations)
        reply: InboxMessage | None = None
        for item in messages:
            answered = _replied_request_id(item)
            if answered is not None and answered in active:
                if reply is None and answered == request_id and item.sender == handler:
                    reply = item
                continue
            if (
                (answered is not None and answered.startswith(_HOOK_REQUEST_ID_PREFIX))
                or item.conversation_id.startswith(_HOOK_CONVERSATION_PREFIX)
                or item.conversation_id in issued
            ):
                self._port.ack(self._callback_actor, item.message_id)
        return reply


def _replied_request_id(message: InboxMessage) -> str | None:
    """The request id a reply body names in ``replyTo``, or None."""

    if b'"replyTo"' not in message.payload:
        return None
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    value = body.get("replyTo") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


def _message_text(message: InboxMessage) -> str:
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return message.payload.decode("utf-8", errors="replace")
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return message.payload.decode("utf-8", errors="replace")


__all__ = [
    "InboxTurnHookInvoker",
]
