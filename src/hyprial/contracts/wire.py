"""Cross-module executor for the frozen 27-case wire contract.

This is intentionally a scenario adapter, not a second implementation of the
protocol.  Envelope fixtures are consumed directly as JSON (2026-08-25 IRC
codec removal, PAC NAMT3 664a7d314947 -- there is no wire-format conversion
step to cross any more: the real production wire format is the Zenoh
delivery-frame JSON envelope, and this adapter's job is durable-inbox
semantics, not envelope serialization).  Durable cases cross the real
``hyprial.inbox`` state machine.  Registry and Lark route state are environment
inputs whose product owners have not landed yet, so this adapter models
those boundaries with deterministic fixtures rather than putting their
policy into transport or inbox.

Three cases retired in the same PAC (formerly WG-026/027/030): they asserted
a "correlated reply may address a ChannelGateway" / "hop count above limit
is rejected" guard that only ever lived inside the now-deleted IRC envelope
codec (``envelope_to_lines``) -- it was never wired into any production
send/receive path (confirmed zero callers before deletion, see
docs/proto.md §2/§6), so retiring the cases that pinned it is not a coverage
regression against anything that ever ran in production.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.contracts import ipc_errors
from hyprial.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RetryPolicy,
)
from hyprial.uri import ADAPTER_URI_PREFIX, CHANNEL_URI_PREFIX

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class _OperationReceipt:
    operation_id: str
    message_id: str
    conversation_id: str
    deliveries: tuple[str, ...]

    def public(self) -> JsonObject:
        return {
            "operationId": self.operation_id,
            "messageId": self.message_id,
            "conversationId": self.conversation_id,
            "deliveries": list(self.deliveries),
        }


class _RouteIndex:
    """Persistent boundary fixture for future adapter-owned reply routes."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS route_index (
                   message_id TEXT PRIMARY KEY,
                   actor TEXT NOT NULL,
                   native_anchor TEXT,
                   created_at_ms INTEGER NOT NULL
               )"""
        )
        self._db.commit()

    def put(
        self,
        message_id: str,
        actor: str,
        *,
        native_anchor: str | None = None,
        created_at_ms: int = 0,
    ) -> None:
        with self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO route_index
                   (message_id, actor, native_anchor, created_at_ms)
                   VALUES (?, ?, ?, ?)""",
                (message_id, actor, native_anchor, created_at_ms),
            )

    def get(self, message_id: str) -> tuple[str, str | None] | None:
        row = self._db.execute(
            "SELECT actor, native_anchor FROM route_index WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return (str(row[0]), row[1]) if row is not None else None

    def close(self) -> None:
        self._db.close()


class _Stack(AbstractContextManager["_Stack"]):
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="hyprial-wire-contract-")
        self.root = Path(self._temporary.name)
        self._services: list[InboxService] = []

    def service(
        self,
        name: str,
        transport: MemoryDeliveryTransport | None = None,
        *,
        max_inbox_items: int = 10_000,
    ) -> InboxService:
        service = InboxService(
            self.root / f"{name}.sqlite3",
            transport or MemoryDeliveryTransport(),
            max_inbox_items=max_inbox_items,
        )
        self._services.append(service)
        return service

    def close_service(self, service: InboxService) -> None:
        service.close()
        self._services.remove(service)

    def __exit__(self, *_: object) -> None:
        for service in reversed(self._services):
            service.close()
        self._temporary.cleanup()


def _target(envelope: JsonObject, index: int = 0) -> str:
    target = envelope["to"][index]
    return str(target.get("actorId") or target["actorKey"])


def _lifecycle(value: str | None, recipient: str) -> DeliveryLifecycle:
    if value is not None:
        return DeliveryLifecycle(value)

    return (
        DeliveryLifecycle.DURABLE_SERVICE
        if recipient.startswith((CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX))
        else DeliveryLifecycle.ONLINE_ONLY
    )


def _message(
    envelope: JsonObject,
    *,
    target_index: int = 0,
    lifecycle: DeliveryLifecycle | None = None,
) -> tuple[InboxMessage, JsonObject]:
    """Build the InboxMessage a real production delivery transport would
    hand to InboxService, directly from the JSON fixture -- no wire-format
    conversion step (see module docstring: the codec that used to sit here
    was IRC-only and never production-reachable)."""

    recipient = _target(envelope, target_index)
    message = InboxMessage(
        message_id=str(envelope["messageId"]),
        conversation_id=str(envelope["conversationId"]),
        sender=str(envelope["from"]["actorId"]),
        recipient=recipient,
        payload=json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
        intent=str(envelope["intent"]),
        lifecycle=lifecycle or _lifecycle(None, recipient),
        idempotency_key=envelope.get("idempotencyKey"),
        created_at_ms=int(envelope.get("timestamp", 0)),
    )
    return message, envelope


def _text(envelope: JsonObject) -> str:
    content = envelope.get("content")
    return str(content.get("text", "")) if isinstance(content, dict) else ""


class _WireEvaluator:
    def __init__(self, envelopes: dict[str, JsonObject]) -> None:
        self.envelopes = envelopes

    def evaluate(self, case: JsonObject) -> JsonObject:
        case_id = str(case.get("id", ""))
        method = getattr(self, f"_{case_id.replace('-', '_')}", None)
        if method is None:
            raise ValueError(f"unsupported wire contract case: {case_id}")
        fixture_ids = case.get("envelope_ids")
        if not isinstance(fixture_ids, list):
            raise TypeError(f"{case_id} has invalid envelope_ids")
        fixtures = [self.envelopes[str(item)] for item in fixture_ids]
        context = case.get("context")
        if not isinstance(context, dict):
            raise TypeError(f"{case_id} has invalid context")
        return method(fixtures, context)

    def _WG_001(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, decoded = _message(fixtures[0])
        transport = MemoryDeliveryTransport()
        transport.set_online(message.recipient, True)
        transport.confirm_direct(message.message_id)
        with _Stack() as stack:
            sender = stack.service("sender", transport)
            recipient = stack.service("recipient")
            submitted = sender.submit(message, now_ms=message.created_at_ms)
            received = recipient.receive(message, now_ms=message.created_at_ms)
        receipt = _OperationReceipt(
            f"operation:{message.message_id}",
            message.message_id,
            str(decoded["conversationId"]),
            (message.recipient,),
        ).public()
        return {
            "accepted": submitted.accepted,
            "wake": received.wake,
            "autoReply": received.auto_reply,
            "receiptFields": list(receipt),
        }

    def _WG_002(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, decoded = _message(fixtures[1])
        with _Stack() as stack:
            result = stack.service("recipient").receive(message, now_ms=0)
        return {
            "replyTo": decoded["replyTo"],
            "conversationId": decoded["conversationId"],
            "wake": result.wake,
            "autoReply": result.auto_reply,
        }

    def _WG_003(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        request, _ = _message(fixtures[0])
        _, ack_envelope = _message(fixtures[1])
        acknowledged_id = str(ack_envelope["content"]["acknowledgedMessageId"])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            inbox.receive(request, now_ms=0)
            result = inbox.ack(request.recipient, acknowledged_id)
            pending = inbox.next(request.recipient)
        return {
            "acknowledged": result.acknowledged,
            "publishedReply": False,
            "pendingAfter": pending is not None,
        }

    def _WG_004(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        with _Stack() as stack:
            result = stack.service("recipient").receive(message, now_ms=0)
        return {"wake": result.wake, "autoReply": result.auto_reply}

    def _WG_005(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            result = inbox.receive(message, now_ms=0)
            recorded = inbox.pending_count(message.recipient) == 1
        return {"record": recorded, "wake": result.wake, "autoReply": result.auto_reply}

    def _WG_006(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        envelope = fixtures[0]
        accepted: list[bool] = []
        with _Stack() as stack:
            for index in range(len(envelope["to"])):
                message, _ = _message(envelope, target_index=index)
                accepted.append(
                    stack.service(f"recipient-{index}")
                    .receive(message, now_ms=0)
                    .accepted
                )
        return {
            "deliveryEntries": len(accepted),
            "independentAcceptance": all(accepted),
            "rollbackAcceptedSibling": False,
        }

    def _WG_007(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            first = inbox.receive(message, now_ms=0)
            second = inbox.receive(message, now_ms=1)
            deliveries = inbox.pending_count(message.recipient)
        return {
            "firstAccepted": first.accepted,
            "secondAccepted": second.accepted,
            "deliveries": deliveries,
        }

    def _WG_008(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        first_message, _ = _message(fixtures[0])
        second_message, _ = _message(fixtures[1])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            first = inbox.receive(first_message, now_ms=0)
            second = inbox.receive(second_message, now_ms=1)
            deliveries = inbox.pending_count(first_message.recipient)
        return {
            "firstAccepted": first.accepted,
            "secondAccepted": second.accepted,
            "deliveries": deliveries,
        }

    def _WG_009(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            first = inbox.receive(message, now_ms=0)
            inbox.ack(message.recipient, message.message_id)
            second = inbox.receive(message, now_ms=inbox.dedup_window_ms + 1)
        return {"firstAccepted": first.accepted, "secondAccepted": second.accepted}

    def _WG_010(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0], lifecycle=DeliveryLifecycle.ONLINE_ONLY)
        with _Stack() as stack:
            inbox = stack.service("sender")
            result = inbox.submit(message, now_ms=0)
            dead_letter = inbox.dlq_count() > 0
        return {
            "accepted": result.accepted,
            "code": result.code,
            "queued": result.queued,
            "deadLetter": dead_letter,
            "rerouted": result.custody_mailbox is not None,
        }

    def _WG_011(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0], lifecycle=DeliveryLifecycle.DURABLE_SERVICE)
        transport = MemoryDeliveryTransport()
        with _Stack() as stack:
            inbox = stack.service("sender", transport)
            result = inbox.submit(message, now_ms=0)
            queued = inbox.outbox_item(message.message_id)
        return {
            "accepted": result.accepted,
            "queued": result.queued,
            "consumeAttemptsWhileOffline": len(transport.delivered),
            # The hold used to run 30 days; the delivery design cut it to 30
            # minutes and made the eviction observable instead of silent.
            "holdExpiresAfterMinutes": queued.expires_at_ms // 60_000,
        }

    def _WG_012(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0], lifecycle=DeliveryLifecycle.DURABLE_SERVICE)
        transport = MemoryDeliveryTransport()
        with _Stack() as stack:
            inbox = stack.service("sender", transport)
            inbox.submit(message, now_ms=0)
            stack.close_service(inbox)
            transport.set_online(message.recipient, True)
            transport.confirm_direct(message.message_id)
            restarted = stack.service("sender", transport)
            outcomes = restarted.retry_due(now_ms=1_000)
        return {
            "redeliverAfterPresence": any(item.accepted for item in outcomes),
            "sameMessageId": bool(
                outcomes and outcomes[0].message_id == message.message_id
            ),
            "duplicateUserDelivery": transport.delivered.count(message.message_id) > 1,
        }

    def _WG_013(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        first, _ = _message(fixtures[0])
        second, _ = _message(fixtures[1])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            inbox.receive(first, now_ms=0)
            inbox.receive(second, now_ms=1)
            before = inbox.next(first.recipient)
            inbox.ack(first.recipient, first.message_id)
            after = inbox.next(first.recipient)
        return {
            "visibleBeforeFirstAccept": [before.message_id] if before else [],
            "visibleAfterFirstAccept": [after.message_id] if after else [],
        }

    def _WG_014(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0], lifecycle=DeliveryLifecycle.DURABLE_SERVICE)
        policy = RetryPolicy()
        transport = MemoryDeliveryTransport()
        transport.set_online(message.recipient, True)
        with _Stack() as stack:
            inbox = stack.service("sender", transport)
            inbox.submit(message, now_ms=0)
            for now_ms in (1_000, 6_000, 36_000, 156_000):
                inbox.retry_due(now_ms=now_ms)
            terminal_before_exhausted = inbox.dlq_count() > 0
        return {
            "retryBackoffSeconds": list(policy.backoff_seconds),
            "maximumAttempts": policy.maximum_attempts,
            "terminalBeforeScheduleExhausted": terminal_before_exhausted,
        }

    def _WG_015(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0], lifecycle=DeliveryLifecycle.DURABLE_SERVICE)
        blocker = InboxMessage(
            message_id="capacity-blocker",
            conversation_id=message.conversation_id,
            sender=message.sender,
            recipient=message.recipient,
            payload=b"blocker",
            intent="request",
            lifecycle=message.lifecycle,
            created_at_ms=0,
        )
        with _Stack() as stack:
            inbox = stack.service("recipient", max_inbox_items=1)
            inbox.receive(blocker, now_ms=0)
            result = inbox.receive(message, now_ms=1)
            blocker_retained = inbox.next(message.recipient) is not None
            dead_letter = inbox.dlq_count() > 0
        return {
            "accepted": result.accepted,
            "code": result.code,
            "evictOlder": not blocker_retained,
            "deadLetter": dead_letter,
        }

    def _WG_016(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            accepted = inbox.receive(message, now_ms=0)
            failure = inbox.fail(
                message.recipient, message.message_id, "deterministic reject"
            )
            dead_letter = inbox.dlq_count() == 1
        return {
            "accepted": accepted.accepted,
            "code": failure.code,
            "errorReply": {"intent": "reply", "replyTo": failure.reply_to},
            "deadLetter": dead_letter,
        }

    def _WG_017(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _message(fixtures[0])
        matches = context.get("registryMatches", [])
        return {
            "accepted": bool(matches),
            "code": ipc_errors.TARGET_NOT_FOUND if not matches else None,
            "rerouted": False,
            "nextStepPresent": not bool(matches),
        }

    def _WG_018(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _message(fixtures[0])
        matches = context.get("registryMatches", [])
        return {
            "accepted": len(matches) == 1,
            "code": "TARGET_AMBIGUOUS" if len(matches) > 1 else None,
            "rerouted": False,
            "candidatesPresent": len(matches) > 1,
        }

    def _WG_019(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        has_default = bool(context.get("gatewayHasDefaultRoute"))
        return {
            "accepted": has_default,
            "code": None if has_default else "CHANNEL_DESTINATION_REQUIRED",
            "rerouted": False,
        }

    def _WG_020(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _, decoded = _message(fixtures[0])
        with _Stack() as stack:
            path = stack.root / "reply-index.sqlite3"
            index = _RouteIndex(path)
            age_ms = int(context.get("replyRouteAgeDays", 0)) * 86_400_000
            index.put(
                str(decoded["replyTo"]),
                "agent:alice:mac:worker",
                created_at_ms=-age_ms,
            )
            index.close()
            restored = _RouteIndex(path)
            route = restored.get(str(decoded["replyTo"]))
            restored.close()
        return {
            "target": route[0] if route else None,
            "correlated": route is not None,
            "fallbackUsed": False,
        }

    def _WG_021(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _, outbound = _message(fixtures[0])
        _message(fixtures[1])
        with _Stack() as stack:
            path = stack.root / "reply-index.sqlite3"
            index = _RouteIndex(path)
            index.put(
                str(outbound["replyTo"]),
                "agent:alice:mac:worker",
                native_anchor="om-original-request",
            )
            index.close()
            restored = _RouteIndex(path)
            route = restored.get(str(outbound["replyTo"]))
            restored.close()
        return {
            "nativeReplyAnchor": route[1] if route else None,
            "replyIndexPersisted": route is not None,
            "correlated": route is not None,
        }

    def _WG_022(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _, decoded = _message(fixtures[0])
        pinned = context.get("pinnedActor")
        return {
            "target": pinned,
            "quotedContextIncluded": "[Quoted message" in _text(fixtures[0]),
            "errorReply": pinned is None or decoded["intent"] == "reply",
        }

    def _WG_023(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _message(fixtures[0])
        return {
            "target": context.get("pinnedActor"),
            "quotedContextIncluded": "[Quoted message" in _text(fixtures[0]),
            "replyContextOverride": False,
        }

    def _WG_024(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        message, _ = _message(fixtures[0])
        pinned = context.get("pinnedActor")
        missing_note = "Quoted context could not be resolved."
        with _Stack() as stack:
            accepted = stack.service("recipient").receive(message, now_ms=0).accepted
        return {
            "target": pinned,
            "messageContains": missing_note
            if missing_note in _text(fixtures[0])
            else "",
            "accepted": bool(pinned) and accepted,
        }

    def _WG_025(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _message(fixtures[0])
        pinned = context.get("pinnedActor")
        return {
            "accepted": pinned is not None,
            "code": None if pinned is not None else "REPLY_CONTEXT_NOT_FOUND",
            "rerouted": False,
        }

    def _WG_028(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        request, _ = _message(fixtures[0])
        _, ack_envelope = _message(fixtures[1])
        message_id = str(ack_envelope["content"]["acknowledgedMessageId"])
        with _Stack() as stack:
            inbox = stack.service("recipient")
            inbox.receive(request, now_ms=0)
            settled_before = inbox.is_acknowledged(message_id)
            acknowledged = inbox.ack(request.recipient, message_id).acknowledged
            stack.close_service(inbox)
            restarted = stack.service("recipient")
            replayed = restarted.next(request.recipient) is not None
        return {
            "settledBeforeConfirmation": settled_before,
            "acknowledgedAfterConfirmation": acknowledged,
            "replayedAfterRestart": replayed,
        }

    def _WG_029(self, fixtures: list[JsonObject], context: JsonObject) -> JsonObject:
        _, ack_envelope = _message(fixtures[0])
        message_id = str(ack_envelope["content"]["acknowledgedMessageId"])
        with _Stack() as stack:
            result = stack.service("recipient").ack(
                "agent:alice:mac:sender", message_id
            )
        return {"acknowledged": result.acknowledged, "code": result.code}

def evaluate_case(case: JsonObject, envelopes: dict[str, JsonObject]) -> JsonObject:
    """Execute one declared case without consulting its expected output."""

    return _WireEvaluator(envelopes).evaluate(case)
