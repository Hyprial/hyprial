"""Read acceptance evidence without submitting, fetching or acknowledging mail."""

from __future__ import annotations

import sqlite3

from hyprial.daemon.impl.delivery import INVALID_TEXT_DIGEST, digest_text, text_sha256
from hyprial.kernel import parse_agent_uri, parse_user_uri

_EMPTY_TEXT_DIGEST = digest_text("")


def _stored_text_digest(payload: object, dropped_at_ms: object, text_digest: object) -> str | None:
    """The text digest of one sender copy, whether or not its body was released.

    A retained body is classified now through ``classify_send_body``; a released
    body answers from the ``body_text_sha256`` written at release time.  ``None``
    marks an invalid record.
    """

    if dropped_at_ms is None:
        text_digest = text_sha256(payload)
    if not isinstance(text_digest, str) or text_digest == INVALID_TEXT_DIGEST:
        return None
    return text_digest


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
    The text is compared by digest through the one ``classify_send_body``,
    so a sender copy whose body was released answers exactly as before.
    """
    rows = connection.execute(
        """SELECT message_id, payload, body_dropped_at_ms, body_text_sha256
             FROM sender_copies
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
    message_id, payload, dropped_at_ms, stored_digest = rows[0]
    if not isinstance(message_id, str) or not message_id:
        return {"status": "unknown", "reason": "invalid-record"}
    text_digest = _stored_text_digest(payload, dropped_at_ms, stored_digest)
    if text_digest is None:
        return {"status": "unknown", "reason": "invalid-record"}
    if text_digest != digest_text(text):
        return {"status": "unknown", "reason": "request-mismatch"}
    return {
        "status": "accepted", "commandId": idempotency_key,
        "conversationId": conversation_id, "messageId": message_id,
    }


def key_lookup_send(
    connection: sqlite3.Connection, *, sender: str, operation_key: str,
) -> dict[str, str]:
    """Resolve a single-agent send from its operation key alone.

    Like ``lookup_send``, the body check goes through the text digest so a
    sender copy whose body was released (cell-model body retention) still
    answers ``accepted``; an empty message text stays an invalid record.
    The target is an agent URI or, since a message to the owner is queued
    like any other (UA2), a ``user:<owner>`` URI.
    """
    rows = connection.execute(
        """SELECT message_id, conversation_id, recipient, idempotency_key,
                  payload, intent, lifecycle, body_dropped_at_ms, body_text_sha256
             FROM sender_copies
            WHERE sender = ?
              AND (idempotency_key = ?
                   OR substr(idempotency_key, 1, length(?) + 1) = ? || ':')
            LIMIT 2""",
        (sender, operation_key, operation_key, operation_key),
    ).fetchall()
    if not rows:
        return {"status": "unknown", "reason": "not-found"}
    if len(rows) != 1:
        return {"status": "unknown", "reason": "invalid-record"}
    (message_id, conversation_id, recipient, stored_key, payload, intent,
     lifecycle, dropped_at_ms, stored_digest) = rows[0]
    if intent != "request" or lifecycle != "durable_service":
        return {"status": "unknown", "reason": "invalid-record"}
    if not all(isinstance(value, str) and value for value in (
        message_id, conversation_id, recipient, stored_key,
    )):
        return {"status": "unknown", "reason": "invalid-record"}
    prefix = f"{operation_key}:"
    if not stored_key.startswith(prefix):
        return {"status": "unknown", "reason": "invalid-record"}
    target, separator, index = stored_key[len(prefix):].rpartition(":")
    addressable = parse_agent_uri(target) is not None or parse_user_uri(target) is not None
    if not separator or index != "0" or not addressable or recipient != target:
        return {"status": "unknown", "reason": "invalid-record"}
    text_digest = _stored_text_digest(payload, dropped_at_ms, stored_digest)
    if text_digest is None or text_digest == _EMPTY_TEXT_DIGEST:
        return {"status": "unknown", "reason": "invalid-record"}
    return {
        "status": "accepted", "commandId": operation_key,
        "conversationId": conversation_id, "messageId": message_id,
    }
