"""Body digests and the residue a dropped body leaves behind (plan §2.0).

Decision notes (phase A, S5):

- Two digests are kept per released row: ``body_payload_sha256`` over the
  whole stored payload (the hydration key a resolver must match) and
  ``body_text_sha256`` over the message text, or ``!invalid`` when the
  payload is not a valid send body.  ``classify_send_body`` is the one
  definition of "valid send body"; ``message.send.lookup`` uses it, so the
  lookup answers the same before and after a drop.
- The residue keeps only ``origin`` and ``replyTo``: history derives the
  visible origin from the body and read admission derives the reply
  association from it, so both stay identical on a residue row.
- Text is encoded with ``surrogatepass``: JSON can carry a lone surrogate
  that strict UTF-8 refuses, and a digest must never raise.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hyprial.daemon.impl.delivery.ports import INVALID_TEXT_DIGEST

RESIDUE_KEYS = ("origin", "replyTo")


def payload_sha256(payload: bytes) -> str:
    """The SHA-256 hex digest of the whole stored payload."""

    return hashlib.sha256(bytes(payload)).hexdigest()


def _json_object(payload: object) -> dict[str, Any] | None:
    try:
        body = json.loads(payload)  # type: ignore[arg-type]
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def classify_send_body(payload: object) -> str | None:
    """The message text of a valid send body, or None for an invalid record."""

    body = _json_object(payload)
    if body is None:
        return None
    text = body.get("message")
    return text if isinstance(text, str) else None


def digest_text(text: str) -> str:
    """The SHA-256 hex digest of one message text."""

    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def text_sha256(payload: object) -> str:
    """The text digest of *payload*, or ``INVALID_TEXT_DIGEST``."""

    text = classify_send_body(payload)
    return INVALID_TEXT_DIGEST if text is None else digest_text(text)


def residue_payload(payload: object) -> bytes:
    """The payload kept after a drop: only ``origin`` and ``replyTo``."""

    body = _json_object(payload) or {}
    kept = {key: body[key] for key in RESIDUE_KEYS if key in body}
    return json.dumps(kept, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", "surrogatepass"
    )


__all__ = [
    "RESIDUE_KEYS",
    "classify_send_body",
    "digest_text",
    "payload_sha256",
    "residue_payload",
    "text_sha256",
]
