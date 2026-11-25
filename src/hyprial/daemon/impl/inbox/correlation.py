"""Read exact send/reply associations from existing durable message authority.

Decision (first-run P3, 2026-11-18): operation-key lookup uses the existing
sender-copy admission contract. A reply must either have reached the recipient's
inbox or be the selected local Agent's accepted durable sender copy, and must
explicitly name the request. User destinations can route to an adapter without
leaving a local user inbox; the sender copy proves the Agent's actual reply,
not adapter delivery or vendor authentication. An outbox row, the original
send's acceptance and conversation proximity are not proof. Consumed rows remain readable.
Missing, ambiguous, released or malformed evidence is unverified. These reads
never fetch, ACK, hydrate, send or create another message ledger, and expose
only correlation metadata rather than message bodies.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from hyprial.kernel import reply_message_id

from .send_lookup import key_lookup_send


@dataclass(frozen=True, slots=True)
class CorrelatedRequest:
    message_id: str
    sender: str
    recipient: str
    conversation_id: str
    operation_key: str


@dataclass(frozen=True, slots=True)
class CorrelatedReply:
    message_id: str
    request_message_id: str
    sender: str
    recipient: str
    conversation_id: str
    received_at_ms: int


def _require_selectors(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("correlation selectors must be non-empty strings")


def correlated_request(
    connection: sqlite3.Connection, *, sender: str, recipient: str, operation_key: str,
) -> CorrelatedRequest | None:
    """The unique accepted single-target request carrying this send key."""

    _require_selectors(sender, recipient, operation_key)
    accepted = key_lookup_send(connection, sender=sender, operation_key=operation_key)
    if accepted.get("status") != "accepted":
        return None
    row = connection.execute(
        "SELECT recipient FROM sender_copies WHERE message_id=? AND sender=?",
        (accepted["messageId"], sender),
    ).fetchone()
    if row is None or row[0] != recipient:
        return None
    return CorrelatedRequest(
        message_id=accepted["messageId"], sender=sender, recipient=recipient,
        conversation_id=accepted["conversationId"], operation_key=operation_key,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate message field")
        result[key] = value
    return result


def correlated_reply(
    connection: sqlite3.Connection, *, sender: str, recipient: str,
    conversation_id: str, request_message_id: str,
) -> CorrelatedReply | None:
    """One exact reply retained by delivery or by the trusted local sender."""

    _require_selectors(sender, recipient, conversation_id, request_message_id)
    expected_id = reply_message_id(request_message_id)
    rows = connection.execute(
        """WITH replies AS (
           SELECT message_id, payload, received_at_ms, 0 AS source,
                  sender, recipient, conversation_id, intent, lifecycle,
                  body_dropped_at_ms, idempotency_key
             FROM inbox
           UNION ALL
           SELECT message_id, payload, created_at_ms, 1 AS source,
                  sender, recipient, conversation_id, intent, lifecycle,
                  body_dropped_at_ms, idempotency_key
             FROM sender_copies
        ) SELECT * FROM replies
            WHERE message_id=? OR (
              sender=? AND recipient=? AND conversation_id=?
              AND CASE WHEN json_valid(CAST(payload AS TEXT))
                       THEN json_extract(CAST(payload AS TEXT), '$.replyTo') END = ?)
            LIMIT 3""",
        (expected_id, sender, recipient, conversation_id, request_message_id),
    ).fetchall()
    if not rows or len(rows) > 2:
        return None
    expected = (sender, recipient, conversation_id, "reply", "durable_service",
                None, f"reply:{request_message_id}")
    previous_body = None
    for row in rows:
        message_id, payload, timestamp, _, *metadata = row
        if (message_id != expected_id or tuple(metadata) != expected
                or type(timestamp) is not int or timestamp < 0):
            return None
        try:
            body = json.loads(payload, object_pairs_hook=_unique_object)
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        if (not isinstance(body, dict) or body.get("replyTo") != request_message_id
                or (previous_body is not None and body != previous_body)):
            return None
        previous_body = body
    received_at_ms = min(rows, key=lambda row: row[3])[2]
    return CorrelatedReply(
        message_id=expected_id, request_message_id=request_message_id,
        sender=sender, recipient=recipient, conversation_id=conversation_id,
        received_at_ms=received_at_ms,
    )
