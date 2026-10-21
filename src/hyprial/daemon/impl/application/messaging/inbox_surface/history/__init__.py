"""Durable conversation history: an identity-scoped, read-only IPC read.

The method serves a conversation an authenticated caller takes part in.  It
does not accept a participant selector: the participant is the authenticated
subject resolved by the existing message-caller chain, exactly as a send or an
ack resolves it.  A browser can neither choose nor widen that identity.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from hyprial.daemon.impl.application.messaging.delivery.status_queries import (
    _query_entry,
)
from hyprial.daemon.impl.inbox.history import (
    HISTORY_PAGE_MAX,
    HISTORY_READ_LIMIT_MAX,
)
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.kernel import DaemonRequestError, ipc_errors

#: Default page size; the adapter's historical default is preserved.
HISTORY_PAGE_DEFAULT = 50
_HISTORY_CURSOR_VERSION = 1
_HISTORY_CURSOR_VERSION_V2 = 2
#: A cursor is a small JSON token; anything larger is not one this method
#: issued and is refused before any decode work.
_HISTORY_CURSOR_MAX_CHARS = 4096
_HISTORY_REQUEST_KEYS = frozenset(
    {
        "from",
        "actor",
        "sessionRef",
        "onBehalfOf",
        "conversationId",
        "cursor",
        "limit",
        "mode",
    }
)
_HISTORY_CURSOR_KEYS = frozenset(
    {"v", "conversationId", "participant", "createdAtMs", "messageId"}
)
_HISTORY_CURSOR_KEYS_V2 = frozenset(
    {
        "v",
        "conversationId",
        "participant",
        "createdAtMs",
        "messageId",
        "kind",
    }
)
_HISTORY_MODES = frozenset({"tail", "older", "after"})
_HISTORY_KIND_AFTER = "after"
_HISTORY_KIND_BEFORE = "before"


class _MessageHistoryMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_message_history(
        self,
        method: str,
        params: JsonObject,
        _trusted_message_origin: str | None = None,
    ) -> JsonObject:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        if _trusted_message_origin == "ipc" and caller.via == "daemon":
            # Global sender-auth rollout defaults to observe, but a NEW read
            # surface must not inherit that compatibility window: an IPC
            # request that presented no session, operator or adapter identity
            # cannot read anyone's conversation.
            raise self._sender_not_authenticated(caller.subject)

        unexpected = set(params) - _HISTORY_REQUEST_KEYS
        if unexpected:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "message.history accepts from/actor, sessionRef, onBehalfOf, "
                "conversationId, cursor, limit and mode only",
                {"unexpectedFields": sorted(unexpected)},
            )

        conversation_id = _required_string(
            params.get("conversationId"), "conversationId"
        )
        limit = _history_limit(params.get("limit"))
        mode = _history_mode(params.get("mode"))
        if mode is None:
            # Version 1: byte-compatible {entries, nextCursor} page,
            # ascending, with the existing forward cursor.
            after = _history_after(
                params.get("cursor"),
                conversation_id=conversation_id,
                participant=caller.subject,
            )
            rows = self._inbox.conversation_messages(
                caller.subject,
                conversation_id,
                after=after,
                limit=min(limit + 1, HISTORY_READ_LIMIT_MAX),
            )
            page = rows[:limit]
            has_more = len(rows) > limit
            next_cursor = (
                _encode_history_cursor(
                    page[-1],
                    conversation_id=conversation_id,
                    participant=caller.subject,
                )
                if has_more
                else None
            )
            return {
                "ok": True,
                "conversationId": conversation_id,
                "entries": [
                    _query_entry(message, kind="message") for message in page
                ],
                "nextCursor": next_cursor,
            }

        if mode == "tail":
            if params.get("cursor") is not None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "mode=tail does not accept a cursor",
                )
            return _tail_page(
                self._inbox,
                participant=caller.subject,
                conversation_id=conversation_id,
                limit=limit,
            )
        if mode == "older":
            position = _history_cursor_position(
                params.get("cursor"),
                conversation_id=conversation_id,
                participant=caller.subject,
                allowed_kinds=frozenset({_HISTORY_KIND_BEFORE}),
                allow_v1=False,
            )
            return _older_page(
                self._inbox,
                participant=caller.subject,
                conversation_id=conversation_id,
                position=position,
                limit=limit,
            )
        position = _history_cursor_position(
            params.get("cursor"),
            conversation_id=conversation_id,
            participant=caller.subject,
            allowed_kinds=frozenset({_HISTORY_KIND_AFTER}),
            allow_v1=True,
        )
        return _after_page(
            self._inbox,
            participant=caller.subject,
            conversation_id=conversation_id,
            position=position,
            limit=limit,
        )


def _history_limit(value: object) -> int:
    if value is None:
        return HISTORY_PAGE_DEFAULT
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > HISTORY_PAGE_MAX
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"limit must be an integer between 1 and {HISTORY_PAGE_MAX}",
        )
    return value


def _history_after(
    value: object,
    *,
    conversation_id: str,
    participant: str,
) -> tuple[int, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid_history_cursor()
    if len(value) > _HISTORY_CURSOR_MAX_CHARS:
        raise _invalid_history_cursor()
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise _invalid_history_cursor() from None
    if not isinstance(payload, dict) or set(payload) != _HISTORY_CURSOR_KEYS:
        raise _invalid_history_cursor()
    version = payload.get("v")
    created_at_ms = payload.get("createdAtMs")
    message_id = payload.get("messageId")
    if (
        isinstance(version, bool)
        or version != _HISTORY_CURSOR_VERSION
        or payload.get("conversationId") != conversation_id
        or payload.get("participant") != participant
        or isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or created_at_ms < 1
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise _invalid_history_cursor()
    return created_at_ms, message_id


def _encode_history_cursor(
    message: Any,
    *,
    conversation_id: str,
    participant: str,
) -> str:
    payload = {
        "v": _HISTORY_CURSOR_VERSION,
        "conversationId": conversation_id,
        "participant": participant,
        "createdAtMs": message.created_at_ms,
        "messageId": message.message_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _history_mode(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _HISTORY_MODES:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "mode must be tail, older or after",
        )
    return value


def _decode_history_cursor(value: str) -> dict:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise _invalid_history_cursor() from None
    if not isinstance(payload, dict):
        raise _invalid_history_cursor()
    return payload


def _history_cursor_position(
    value: object,
    *,
    conversation_id: str,
    participant: str,
    allowed_kinds: frozenset[str],
    allow_v1: bool,
) -> tuple[int, str]:
    """Decode the daemon-issued position for an explicit mode.

    A version-1 cursor is accepted only where a forward walk is meaningful
    (``after``); ``older`` needs the version-2 direction kind.  The token is
    bound to the conversation and the authenticated participant, so a
    cursor cannot be replayed across either.
    """

    if not isinstance(value, str) or not value:
        raise _invalid_history_cursor()
    if len(value) > _HISTORY_CURSOR_MAX_CHARS:
        raise _invalid_history_cursor()
    payload = _decode_history_cursor(value)
    version = payload.get("v")
    if isinstance(version, bool):
        raise _invalid_history_cursor()
    if version == _HISTORY_CURSOR_VERSION:
        if not allow_v1 or set(payload) != _HISTORY_CURSOR_KEYS:
            raise _invalid_history_cursor()
        return _cursor_bound_position(
            payload,
            conversation_id=conversation_id,
            participant=participant,
        )
    if (
        version != _HISTORY_CURSOR_VERSION_V2
        or set(payload) != _HISTORY_CURSOR_KEYS_V2
    ):
        raise _invalid_history_cursor()
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in allowed_kinds:
        raise _invalid_history_cursor()
    return _cursor_bound_position(
        payload,
        conversation_id=conversation_id,
        participant=participant,
    )


def _cursor_bound_position(
    payload: dict,
    *,
    conversation_id: str,
    participant: str,
) -> tuple[int, str]:
    created_at_ms = payload.get("createdAtMs")
    message_id = payload.get("messageId")
    if (
        payload.get("conversationId") != conversation_id
        or payload.get("participant") != participant
        or isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or created_at_ms < 1
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise _invalid_history_cursor()
    return created_at_ms, message_id


def _tail_page(
    inbox: Any,
    *,
    participant: str,
    conversation_id: str,
    limit: int,
) -> JsonObject:
    rows = inbox.conversation_messages(
        participant,
        conversation_id,
        limit=min(limit + 1, HISTORY_READ_LIMIT_MAX),
        newest_first=True,
    )
    has_older = len(rows) > limit
    newest = rows[:limit]
    return _v2_page(
        mode="tail",
        conversation_id=conversation_id,
        entries=tuple(reversed(newest)),
        head_cursor=(
            _encode_history_cursor_v2(
                newest[0],
                conversation_id=conversation_id,
                participant=participant,
                kind=_HISTORY_KIND_AFTER,
            )
            if newest
            else None
        ),
        next_cursor=None,
        next_older=(
            _encode_history_cursor_v2(
                newest[-1],
                conversation_id=conversation_id,
                participant=participant,
                kind=_HISTORY_KIND_BEFORE,
            )
            if has_older
            else None
        ),
    )


def _older_page(
    inbox: Any,
    *,
    participant: str,
    conversation_id: str,
    position: tuple[int, str],
    limit: int,
) -> JsonObject:
    rows = inbox.conversation_messages(
        participant,
        conversation_id,
        before=position,
        limit=min(limit + 1, HISTORY_READ_LIMIT_MAX),
        newest_first=True,
    )
    has_older = len(rows) > limit
    newest = rows[:limit]
    return _v2_page(
        mode="older",
        conversation_id=conversation_id,
        entries=tuple(reversed(newest)),
        head_cursor=_head_cursor(
            inbox,
            participant=participant,
            conversation_id=conversation_id,
        ),
        next_cursor=None,
        next_older=(
            _encode_history_cursor_v2(
                newest[-1],
                conversation_id=conversation_id,
                participant=participant,
                kind=_HISTORY_KIND_BEFORE,
            )
            if has_older
            else None
        ),
    )


def _after_page(
    inbox: Any,
    *,
    participant: str,
    conversation_id: str,
    position: tuple[int, str],
    limit: int,
) -> JsonObject:
    rows = inbox.conversation_messages(
        participant,
        conversation_id,
        after=position,
        limit=min(limit + 1, HISTORY_READ_LIMIT_MAX),
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return _v2_page(
        mode="after",
        conversation_id=conversation_id,
        entries=page,
        head_cursor=_head_cursor(
            inbox,
            participant=participant,
            conversation_id=conversation_id,
        ),
        next_cursor=(
            _encode_history_cursor_v2(
                page[-1],
                conversation_id=conversation_id,
                participant=participant,
                kind=_HISTORY_KIND_AFTER,
            )
            if has_more
            else None
        ),
        next_older=None,
    )


def _head_cursor(
    inbox: Any,
    *,
    participant: str,
    conversation_id: str,
) -> str | None:
    rows = inbox.conversation_messages(
        participant,
        conversation_id,
        limit=1,
        newest_first=True,
    )
    if not rows:
        return None
    return _encode_history_cursor_v2(
        rows[0],
        conversation_id=conversation_id,
        participant=participant,
        kind=_HISTORY_KIND_AFTER,
    )


def _v2_page(
    *,
    mode: str,
    conversation_id: str,
    entries: tuple[Any, ...],
    head_cursor: str | None,
    next_cursor: str | None,
    next_older: str | None,
) -> JsonObject:
    return {
        "ok": True,
        "conversationId": conversation_id,
        "mode": mode,
        "entries": [
            _query_entry(message, kind="message") for message in entries
        ],
        "headCursor": head_cursor,
        "nextCursor": next_cursor,
        "nextOlderCursor": next_older,
    }


def _encode_history_cursor_v2(
    message: Any,
    *,
    conversation_id: str,
    participant: str,
    kind: str,
) -> str:
    payload = {
        "v": _HISTORY_CURSOR_VERSION_V2,
        "conversationId": conversation_id,
        "participant": participant,
        "createdAtMs": message.created_at_ms,
        "messageId": message.message_id,
        "kind": kind,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _invalid_history_cursor() -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.INVALID_ARGUMENT,
        "cursor is not a history page cursor issued for this conversation "
        "and caller",
    )
