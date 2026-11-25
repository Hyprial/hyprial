"""The payload inner envelope: the sender's stamps inside the opaque body.

Decisions (cell model phase B, wire option B, Allen 2026-11-03; proto.md
§2.4, §5.1 and §5.2):

* ``hyprial-delivery-frame/v1`` is unchanged byte for byte.  The sender's
  ``threadId``, ``conversationCellId`` and ``class`` travel inside the frame's
  opaque ``payload`` as one versioned object under the top-level key
  ``envelope``: ``{"class":"interaction","threadId":"t.…","v":1}``.  The key
  names the inner message envelope of ``inbox.envelope``, never frame ①.
* Only a payload in the message inner schema is stamped: a JSON object whose
  ``message`` is a string, with no ``envelope`` key, whose bytes start with
  ``{`` and end with ``}``, and whose intent is ``request``, ``reply`` or
  ``system``.  Every other payload (progress events, notices with their own
  schema, opaque bytes) leaves untouched.  A 0.5.5.3.3 receiver reads named
  keys (``message``, ``origin``, ``replyTo``) and ignores the unknown one.
* The object is canonical JSON (§2.3) appended as the last key, so the
  receiver restores the sender's exact payload bytes by removing that suffix.
  An ``envelope`` key in any other form (not last, not an object, not
  canonical, duplicated) is body content and stays where it is.
* The sender writes ``class`` always (its explicit stamp, else its derived
  class), ``threadId`` and ``conversationCellId`` only when its message
  carries them.  Absent keys are not written as null.
* The receiver strips the recognised object before anything is stored and
  adopts each value on its own: a well-formed ``t.``/``c.`` id and a known
  class.  A malformed value, or a version other than the integer 1, is
  dropped and that field falls back to local inference.  A delivery never
  fails because of the envelope.  An object with any other version is still
  stripped (it is the sender form) and logged once per message at INFO with
  the message id and the version, so a newer sender's loss is visible.
* A class equal to what the receiver infers locally is not stored as a stamp,
  so the column stays NULL ("derive") exactly as for an unstamped message and
  the class a reply inherits is unchanged; ``message_class_of`` answers the
  sender's class either way.
* An adopted ``threadId`` is a stored stamp: the conversation recorder files
  by it and ``inherit_envelope`` hands it to the receiver's reply, which
  carries it back on the wire.  No check against the (sender, recipient)
  pair is made here (inbox may not import threads); thread ids are
  deterministic per pair, so an honest sender's value equals the local
  derivation.  No pair check is added, by decision (per Allen 2026-11-07):
  this is recorded as a known issue, and the shipped reach is pinned by
  ``test_payload_trust``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.envelope.rules import (
    derive_message_class,
    message_class_of,
)
from hyprial.kernel import MessageClass, is_cell_id_segment, is_thread_id_segment

_LOG = logging.getLogger(__name__)
PAYLOAD_ENVELOPE_KEY = "envelope"
PAYLOAD_ENVELOPE_VERSION = 1
STAMPED_INTENTS = frozenset({"request", "reply", "system"})
_SUFFIX_HEAD = b',"' + PAYLOAD_ENVELOPE_KEY.encode() + b'":'


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _object(payload: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def payload_envelope(message: InboxMessage) -> dict[str, object]:
    """The inner envelope object the sender writes for *message*."""

    envelope: dict[str, object] = {
        "v": PAYLOAD_ENVELOPE_VERSION,
        "class": message_class_of(message).value,
    }
    if message.thread_id is not None:
        envelope["threadId"] = message.thread_id
    if message.conversation_cell_id is not None:
        envelope["conversationCellId"] = message.conversation_cell_id
    return envelope


def stamp_payload(message: InboxMessage) -> InboxMessage:
    """*message* with its inner envelope in the payload, or unchanged."""

    payload = bytes(message.payload)
    if message.intent not in STAMPED_INTENTS:
        return message
    if not (payload.startswith(b"{") and payload.endswith(b"}")):
        return message
    body = _object(payload)
    if body is None or PAYLOAD_ENVELOPE_KEY in body:
        return message
    if not isinstance(body.get("message"), str):
        return message
    return message.with_payload(
        payload[:-1] + _SUFFIX_HEAD + _canonical(payload_envelope(message)) + b"}"
    )


def split_payload(payload: bytes) -> tuple[bytes, Mapping[str, Any] | None]:
    """The sender's original payload and its inner envelope, if it has one."""

    payload = bytes(payload)
    if _SUFFIX_HEAD not in payload:
        return payload, None
    body = _object(payload)
    value = None if body is None else body.get(PAYLOAD_ENVELOPE_KEY)
    if body is None or not isinstance(value, dict):
        return payload, None
    suffix = _SUFFIX_HEAD + _canonical(value) + b"}"
    if not payload.endswith(suffix):
        return payload, None
    original = payload[: -len(suffix)] + b"}"
    expected = {key: item for key, item in body.items() if key != PAYLOAD_ENVELOPE_KEY}
    if _object(original) != expected:
        return payload, None
    return original, value


def _thread(value: object) -> str | None:
    return value if isinstance(value, str) and is_thread_id_segment(value) else None


def _cell(value: object) -> str | None:
    return value if isinstance(value, str) and is_cell_id_segment(value) else None


def _message_class(value: object) -> MessageClass | None:
    if not isinstance(value, str):
        return None
    try:
        return MessageClass(value)
    except ValueError:
        return None


def adopt_payload_envelope(message: InboxMessage) -> InboxMessage:
    """*message* as received: the sender's payload and its adopted envelope."""

    payload, envelope = split_payload(message.payload)
    if envelope is None:
        return message
    received = message.with_payload(payload)
    version = envelope.get("v")
    if type(version) is not int or version != PAYLOAD_ENVELOPE_VERSION:
        _LOG.info(
            "payload envelope ignored: message %s carries version %r, this node reads %d",
            message.message_id,
            version,
            PAYLOAD_ENVELOPE_VERSION,
        )
        return received
    message_class = _message_class(envelope.get("class"))
    if message_class is derive_message_class(received):
        message_class = None
    return received.with_envelope(
        thread_id=_thread(envelope.get("threadId")),
        conversation_cell_id=_cell(envelope.get("conversationCellId")),
        message_class=message_class,
    )


__all__ = [
    "PAYLOAD_ENVELOPE_KEY",
    "PAYLOAD_ENVELOPE_VERSION",
    "STAMPED_INTENTS",
    "adopt_payload_envelope",
    "payload_envelope",
    "split_payload",
    "stamp_payload",
]
