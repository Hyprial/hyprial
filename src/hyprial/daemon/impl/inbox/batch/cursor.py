"""The opaque inbox-batch cursor: canonical JSON, base64url, domain-separated MAC.

The cursor is the only snapshot identity a connector hands back.  It is
self-contained (the frozen member vector travels inside it) and stateless on
the daemon side: no batch ledger, no cleanup, no second authority.  The MAC
key is derived from this installation's inbox identity (node id plus the
resolved inbox database path), so it survives a daemon restart and a wiped
process memory; it is not shared with, or reused by, any other protocol.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from pathlib import Path

from hyprial.daemon.impl.inbox.batch.contract import (
    SCHEMA_VERSION,
    SOURCE_INBOX,
    SOURCE_NOTICE,
    BatchBounds,
    BatchCursor,
    BatchMember,
)

__all__ = [
    "MAX_CURSOR_CHARS",
    "CursorError",
    "actor_digest",
    "cursor_digest",
    "cursor_key",
    "decode_cursor",
    "encode_cursor",
]

#: Bounded like any other client-supplied field: a cursor larger than this is
#: malformed, not a legitimate snapshot of an unbounded inbox.
MAX_CURSOR_CHARS = 1024 * 1024

_DOMAIN = "hyprial/inbox-batch/v1"
_MAC_BYTES = 16


class CursorError(ValueError):
    """A malformed, oversized, unsupported or unauthenticated cursor."""

    def __init__(self, message: str, *, foreign_actor: bool = False) -> None:
        super().__init__(message)
        #: Authenticated by this installation but minted for another actor:
        #: the caller is not authorized, which is not a malformed request.
        self.foreign_actor = foreign_actor


def cursor_key(database: str | Path, node_id: str) -> bytes:
    """This installation's persistent, domain-separated cursor-MAC key."""

    identity = f"{_DOMAIN}\n{node_id}\n{Path(database).resolve()}"
    return hashlib.sha256(identity.encode("utf-8")).digest()


def actor_digest(actor: str) -> str:
    return hashlib.sha256(f"actor\n{actor}".encode("utf-8")).hexdigest()


def cursor_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as error:
        raise CursorError("cursor is not valid base64url") from error


def _payload(cursor: BatchCursor) -> dict[str, object]:
    return {
        "v": SCHEMA_VERSION,
        "a": actor_digest(cursor.actor),
        "ih": cursor.bounds.inbox_high,
        "nh": cursor.bounds.notice_high,
        "co": cursor.bounds.capture_ordinal,
        "m": [
            [member.source, member.message_id, member.ordinal]
            for member in cursor.members
        ],
        "d": [
            [member.source, member.message_id, member.ordinal]
            for member in cursor.deferred
        ],
        "p": cursor.predecessor_digest,
    }


def _mac(key: bytes, raw: bytes) -> bytes:
    return hmac.new(key, _DOMAIN.encode("ascii") + b"\0" + raw, hashlib.sha256).digest()[
        :_MAC_BYTES
    ]


def encode_cursor(cursor: BatchCursor, key: bytes) -> str:
    raw = json.dumps(
        _payload(cursor), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return f"{_b64(raw)}.{_b64(_mac(key, raw))}"


def _members(value: object, field: str) -> tuple[BatchMember, ...]:
    if not isinstance(value, list):
        raise CursorError(f"cursor {field} must be a list")
    members: list[BatchMember] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 3:
            raise CursorError(f"cursor {field} entries must be [source, id, ordinal]")
        source, message_id, ordinal = entry
        if source not in (SOURCE_INBOX, SOURCE_NOTICE):
            raise CursorError("cursor member source is not supported")
        if not isinstance(message_id, str) or not message_id:
            raise CursorError("cursor member id must be a non-empty string")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise CursorError("cursor member ordinal must be a non-negative integer")
        members.append(BatchMember(source, message_id, ordinal))
    return tuple(members)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CursorError("cursor bounds must be non-negative integers")
    return value


def _actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor:
        raise CursorError("cursor actor must be a non-empty identity")
    return actor


def decode_cursor(token: object, key: bytes, *, actor: str) -> BatchCursor:
    """Verify one cursor token and bind it to ``actor`` (the authenticated caller)."""

    if not isinstance(token, str) or not token:
        raise CursorError("cursor must be a non-empty string")
    if len(token) > MAX_CURSOR_CHARS:
        raise CursorError("cursor is oversized")
    body, separator, mac_text = token.partition(".")
    if not separator or not body or not mac_text:
        raise CursorError("cursor is not in the expected two-part form")
    raw = _unb64(body)
    provided = _unb64(mac_text)
    expected = _mac(key, raw)
    if len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
        raise CursorError("cursor authentication failed")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise CursorError("cursor payload is not valid JSON") from error
    if not isinstance(payload, dict):
        raise CursorError("cursor payload must be an object")
    if payload.get("v") != SCHEMA_VERSION:
        raise CursorError("cursor schema version is not supported")
    digest = payload.get("a")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, actor_digest(_actor(actor))
    ):
        # Another actor's snapshot is not discoverable through its cursor.
        raise CursorError("cursor belongs to another actor", foreign_actor=True)
    bounds = BatchBounds(
        inbox_high=_integer(payload.get("ih")),
        notice_high=_integer(payload.get("nh")),
        capture_ordinal=_integer(payload.get("co")),
    )
    predecessor = payload.get("p")
    if predecessor is not None and not isinstance(predecessor, str):
        raise CursorError("cursor predecessor digest must be a string or null")
    return BatchCursor(
        actor=actor,
        bounds=bounds,
        members=_members(payload.get("m"), "members"),
        deferred=_members(payload.get("d"), "deferred"),
        predecessor_digest=predecessor,
    )
