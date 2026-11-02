"""Read acceptance evidence without submitting, fetching or acknowledging mail."""

from __future__ import annotations

import json
import sqlite3


def lookup_send(
    connection: sqlite3.Connection,
    *,
    sender: str,
    conversation_id: str,
    target: str,
    text: str,
    idempotency_key: str,
) -> dict[str, str]:
    """Resolve a single-agent message.send from its retained sender copy.

    The stored key belongs to target index zero in the existing send contract.
    No message id is guessed. A copy commits atomically with its outbox entry,
    before delivery I/O, and survives deletion of that outbox entry. Absence
    never proves refusal: the original request may still be in flight.
    """
    rows = connection.execute(
        """SELECT message_id, payload FROM sender_copies
             WHERE sender = ? AND conversation_id = ? AND recipient = ?
               AND idempotency_key = ? AND intent = 'request'
               AND lifecycle = 'durable_service'
             LIMIT 2""",
        (sender, conversation_id, target, f"{idempotency_key}:{target}:0"),
    ).fetchall()
    if not rows:
        return {"status": "unknown", "reason": "not-found"}
    if len(rows) != 1:
        return {"status": "unknown", "reason": "invalid-record"}
    message_id, payload = rows[0]
    try:
        body = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {"status": "unknown", "reason": "invalid-record"}
    if not isinstance(message_id, str) or not message_id or not isinstance(body, dict):
        return {"status": "unknown", "reason": "invalid-record"}
    if not isinstance(body.get("message"), str):
        return {"status": "unknown", "reason": "invalid-record"}
    if body["message"] != text:
        return {"status": "unknown", "reason": "request-mismatch"}
    return {
        "status": "accepted", "commandId": idempotency_key,
        "conversationId": conversation_id, "messageId": message_id,
    }
