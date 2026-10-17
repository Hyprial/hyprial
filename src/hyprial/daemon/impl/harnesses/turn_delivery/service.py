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
)


class InboxTurnHookInvoker:
    """Exchange hook events through the ordinary actor inbox."""

    def __init__(
        self,
        inbox: InboxPort,
        *,
        callback_actor: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if parse_agent_uri(callback_actor) is None:
            raise ValueError("hook callback actor must be a canonical agent URI")
        # Production passes the actor-owned DeliveryCustodyFacade.  Keep the
        # name aligned with that composed port rather than implying direct
        # InboxService ownership at this call site.
        self._port = inbox
        self._callback_actor = callback_actor
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._active_conversations: set[str] = set()
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
        exchange_id = uuid4().hex
        message = self._request(handler, event, exchange_id=exchange_id)
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
        exchange_id = uuid4().hex
        conversation_id = f"{_HOOK_CONVERSATION_PREFIX}{exchange_id}"
        message = self._request(handler, event, exchange_id=exchange_id)
        with self._active_lock:
            self._active_conversations.add(conversation_id)
        try:
            submitted = self._port.submit(message)
        except Exception:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)
            raise
        if not submitted.accepted:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)
            raise RuntimeError(submitted.code or "hook request was rejected")
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                reply = self._reply_and_sweep_stale(conversation_id, handler)
                if reply is not None:
                    text = _message_text(reply)
                    self._port.ack(self._callback_actor, reply.message_id)
                    return text
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        finally:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)

        # Retire a local request row that missed its deadline.  ONLINE_ONLY
        # prevents offline retry accumulation; this ack bounds the same-daemon
        # handler case, and the final sweep catches a reply that raced timeout.
        try:
            self._port.ack(handler, message.message_id)
        except Exception:
            # Cleanup is advisory after the deadline; the timeout remains the
            # invocation's authoritative outcome and stale replies still get
            # their own best-effort sweep.
            pass
        try:
            self._reply_and_sweep_stale(conversation_id, handler)
        except Exception:
            # A cleanup failure must not replace the bounded timeout result.
            pass
        raise TimeoutError("hook handler timed out")

    def _request(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        exchange_id: str,
    ) -> InboxMessage:
        return InboxMessage(
            message_id=f"turn-hook-{exchange_id}",
            conversation_id=f"{_HOOK_CONVERSATION_PREFIX}{exchange_id}",
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
            # A bounded hook exchange is meaningful only while both actors are
            # online.  Do not create a durable retry stream for a handler that
            # cannot answer inside this invocation's deadline.
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=self._clock_ms(),
            idempotency_key=f"turn-hook:{exchange_id}",
        )

    def _reply_and_sweep_stale(
        self, conversation_id: str, handler: str
    ) -> InboxMessage | None:
        messages = self._port.pending_messages(self._callback_actor)
        with self._active_lock:
            active = frozenset(self._active_conversations)
        reply: InboxMessage | None = None
        for item in messages:
            if not item.conversation_id.startswith(_HOOK_CONVERSATION_PREFIX):
                continue
            if (
                item.conversation_id == conversation_id
                and item.sender == handler
                and item.conversation_id in active
            ):
                reply = item
                continue
            if item.conversation_id not in active:
                self._port.ack(self._callback_actor, item.message_id)
        return reply


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
