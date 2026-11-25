"""Durable conversation history: an identity-scoped, read-only IPC read.

The method serves a conversation an authenticated caller takes part in.  It
does not accept a participant selector: the participant is the authenticated
subject resolved by the existing message-caller chain, exactly as a send or an
ack resolves it.  A browser can neither choose nor widen that identity.

Decision notes (cell model phase B, S6-read):

- ``history_answer`` is the one paging engine (modes, cursors, limits and the
  entry projection).  ``message.history`` (the daemon read behind the GUI's
  ``sessions.history``) feeds it the delivery facade's hydrating read of one
  legacy conversation; ``conversations.history`` feeds it the same read
  restricted to one conversation cell's thread.  Cursors are bound to the id
  the caller named, so a legacy cursor never pages a cell or the reverse.
- ``conversations.list`` and ``conversations.history`` resolve their caller
  with ``_history_caller``, the rule ``message.history`` uses.  They never
  compose the cell runtime; since plan slice RM1 they read the conversation
  record (``records``) and merge it with the local queue.
- ``_queue_threads`` is the caller's thread set as the queue records it
  (stored ``thread_id`` or the inferred one, the recorder's rule).
  ``threads.*`` merge it into the authority's projection only for a reader
  ``_history_caller`` admits; without a group read it is empty and
  ``threads.*`` answer from the projection alone.
- The conversations and threads packages are reached through their public
  faces, imported where they are used, so composing the application imports
  no conversations module.

Decision notes (plan slice RM1, R13, R15):

- ``conversations.history`` answers the record's messages of the cell plus
  the queue rows of the cell the record lacks (``merged_read``); the cell's
  thread key and legacy id come from the queue head, else from the record.
  ``conversations.list`` folds record heads into the queue heads
  (``merge_head_sets``).  Both publish ``recordAvailable``: false when the
  record could not be read and the answer is the queue alone.
- ``conversations.history`` entries carry ``messageClass`` in every mode,
  the no-mode page included (``classed``).  ``message.history``'s no-mode
  page keeps its pinned shape (golden snapshots) and gains nothing.
- (plan slice S2) A record placeholder whose body the queue no longer
  holds carries ``bodyOmitted`` with the reason and an empty ``message``.
  Only ``conversations.history`` reads record messages, so a
  ``message.history`` entry never carries it.

Integrator decision D9 (release/v0.5.5.4): a registered agent's history is
read under every consumer read key (``_consumer_read_keys``), like fetch, pull
and wake.  Since the name-form read alias was removed (owner decision
2026-11-19) that is the agent's own address alone.  The pages merge by the
keyset order ``(created_at_ms, message_id)`` and cursors stay bound to the
authenticated subject.  ``message.history`` and
``conversations.history`` both read their rows through ``_aliased_read``,
and the heads behind ``conversations.list``, ``conversations.history`` and
``threads.*`` (``_conversation_heads``, ``_queue_threads``) are built from
the same keys, one head set per key with that key as its participant.  A
row keeps the thread its own ends infer (rows are not rewritten), so it is
listed under its own thread and cell.

Decision notes (slice GC2): ``conversations.list`` reads ``kinds``.  The
default (absent, or direct alone) runs exactly the read above and binds no
kinds into its cursor.  ``group`` adds the caller's group heads from
``inbox_surface.groups`` (the queue is not read for them), merged into the
same newest-first order; ``group`` alone skips the direct read, and
``recordAvailable`` is false when any read it made fell back.

Decision notes (slice GC3): ``conversations.history`` with a ``g.`` id is
the group document's history (``inbox_surface.groups.history``) on this
same page engine; ``history_answer`` takes the group entry projection as
``project``.  A ``c.`` id reads exactly as before.

Decision notes (slice GC4, draft §4.6): a delivered group row carries the
group id as its conversation id, and the group document is its copy.  The
queue heads and queue threads (``_direct_groups``) leave such rows out, so
they never surface as a direct conversation or a thread, and
``message.history`` refuses a group id (``INVALID_ARGUMENT``) and points to
``conversations.history``.  The pending reads still serve the rows: that
is how a member agent sees them.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from hyprial.daemon.impl.application.messaging.delivery.status_queries import (
    _query_entry,
)
from hyprial.daemon.impl.inbox.envelope.rules import message_class_of
from hyprial.daemon.impl.inbox.history import (
    HISTORY_PAGE_MAX,
    HISTORY_READ_LIMIT_MAX,
)
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.kernel import (
    DaemonRequestError,
    canonical_user_uri,
    ipc_errors,
    is_group_id_segment,
)

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
        caller = self._history_caller(method, params, _trusted_message_origin)
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
        if is_group_id_segment(conversation_id):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "message.history reads direct conversations; read a group "
                "with conversations.history",
            )
        return history_answer(
            params,
            read=self._aliased_read(caller.subject, conversation_id),
            participant=caller.subject,
            conversation_id=conversation_id,
        )

    def _aliased_read(self, participant: str, conversation_id: str) -> HistoryRead:
        """One conversation's rows under every consumer read key (D9)."""

        reader = _AliasedConversationReader(
            self._inbox, self._consumer_read_keys(participant)
        )
        return partial(reader.conversation_messages, participant, conversation_id)

    def _history_caller(
        self, method: str, params: JsonObject, trusted_origin: str | None
    ) -> Any:
        """The authenticated reader of a conversation read.

        The global sender-auth rollout defaults to observe, but a read
        surface does not inherit that compatibility window: an IPC request
        that presented no session, operator or adapter identity cannot read
        anyone's conversation.
        """

        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=trusted_origin,
        )
        if trusted_origin == "ipc" and caller.via == "daemon":
            raise self._sender_not_authenticated(caller.subject)
        return caller

    def _ipc_conversations_list(
        self,
        method: str,
        params: JsonObject,
        _trusted_message_origin: str | None = None,
    ) -> JsonObject:
        """``conversations.list``: the caller's conversations, newest first."""

        from hyprial.daemon.impl.conversations import (
            decode_list_cursor,
            encode_list_cursor,
            page_heads,
        )
        from hyprial.daemon.impl.conversations.read import head_order

        caller = self._history_caller(method, params, _trusted_message_origin)
        participant = caller.subject
        limit = _history_limit(params.get("limit"))
        kinds = _list_kinds(params.get("kinds"))
        if kinds is None or "direct" in kinds:
            heads, available = self._listed_heads(participant)
        else:
            heads, available = (), True
        if kinds is not None and "group" in kinds:
            from ..groups import group_heads

            grouped, grouped_available = group_heads(self, participant)
            heads = tuple(sorted((*heads, *grouped), key=head_order))
            available = available and grouped_available
        after = None
        if params.get("cursor") is not None:
            try:
                after = decode_list_cursor(
                    params["cursor"], participant=participant,
                    kinds=kinds,
                )
            except ValueError:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "cursor is not a conversation list cursor issued for this "
                    "caller and filter",
                ) from None
        page, more = page_heads(heads, after=after, limit=limit)
        return {
            "ok": True,
            "participant": participant,
            "conversations": [head.to_json() for head in page],
            "nextCursor": (
                encode_list_cursor(
                    page[-1], participant=participant, kinds=kinds
                )
                if more
                else None
            ),
            "recordAvailable": available,
        }

    def _ipc_conversations_history(
        self,
        method: str,
        params: JsonObject,
        _trusted_message_origin: str | None = None,
    ) -> JsonObject:
        """``conversations.history``: one page of one conversation cell.

        The page engine is ``message.history``'s; the rows are the cell's
        record messages plus the legacy conversation's queue rows restricted
        to the cell's thread that the record lacks, the queue rows hydrated
        by the delivery facade exactly as ``message.history`` hydrates them.
        A conversation the caller has no message of answers an empty page.
        """

        from hyprial.daemon.impl.conversations import merged_read, thread_filtered

        from .records import record_messages_for

        if is_group_id_segment(str(params.get("conversationId") or "")):
            from ..groups.history import group_history

            return group_history(self, method, params, _trusted_message_origin)
        caller = self._history_caller(method, params, _trusted_message_origin)
        participant = caller.subject
        cell_id = _required_string(params.get("conversationId"), "conversationId")
        head = next(
            (
                head
                for head in self._conversation_heads(participant)
                if head.conversation_id == cell_id
            ),
            None,
        )
        recorded, available = record_messages_for(
            self,
            cell_id,
            keys=self._consumer_read_keys(participant),
            authority=_conversation_authority(self),
        )
        if head is not None:
            thread_id, legacy = head.thread_id, head.legacy_conversation_id
        elif recorded:
            thread_id, legacy = recorded[-1].thread_id, recorded[-1].conversation_id
        else:
            thread_id = legacy = None
        if thread_id is None or legacy is None:
            read: HistoryRead = _no_rows
        else:
            read = thread_filtered(
                self._aliased_read(participant, legacy),
                thread_id=thread_id,
                thread_key=_thread_key(self),
                batch=HISTORY_READ_LIMIT_MAX,
            )
            if recorded:
                read = merged_read(recorded, read, batch=HISTORY_READ_LIMIT_MAX)
        page = history_answer(
            params,
            read=read,
            participant=participant,
            conversation_id=cell_id,
            classed=True,
        )
        return {
            **page,
            "legacyConversationId": legacy,
            "recordAvailable": available,
        }

    def _listed_heads(self, participant: str) -> tuple[tuple[Any, ...], bool]:
        """The caller's queue heads merged with its record heads (RM1)."""

        from hyprial.daemon.impl.conversations import merge_head_sets

        from .records import record_heads_for

        queued = self._conversation_heads(participant)
        recorded, available = record_heads_for(
            self,
            keys=self._consumer_read_keys(participant),
            thread_key=_thread_key(self),
            authority=_conversation_authority(self),
        )
        return merge_head_sets(queued, recorded), available

    def _conversation_heads(self, participant: str) -> tuple[Any, ...]:
        """The caller's conversations under every consumer read key (D9)."""

        from hyprial.daemon.impl.conversations import conversation_heads, merge_heads

        inbox = self._inbox
        groups = getattr(inbox, "conversation_groups", None)
        if not callable(groups) or not callable(
            getattr(inbox, "conversation_messages", None)
        ):
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY, "conversation reads are not wired"
            )
        authority = _conversation_authority(self)
        thread_key = _thread_key(self)
        return merge_heads(
            conversation_heads(
                _direct_groups(groups(key)),
                participant=key,
                authority=authority,
                thread_key=thread_key,
            )
            for key in self._consumer_read_keys(participant)
        )


HistoryRead = Callable[..., Sequence[Any]]


def _list_kinds(value: object) -> tuple[str, ...] | None:
    """The listed kinds, sorted; None for the default (direct conversations alone)."""

    if value is None:
        return None
    kinds = tuple(sorted({str(kind) for kind in value}))  # type: ignore[union-attr]
    return None if kinds == ("direct",) else kinds


def _direct_groups(groups: Sequence[Any]) -> tuple[Any, ...]:
    """The queue's conversation groups without the delivered group rows (GC4)."""

    return tuple(group for group in groups if not is_group_id_segment(group.conversation_id))


def _no_rows(**_bounds: Any) -> tuple[Any, ...]:
    return ()


def _thread_key(app: Any) -> Callable[[str, str], Any]:
    from hyprial.daemon.impl.conversations import direct_key

    del app
    return direct_key


def _conversation_authority(app: Any) -> str:
    """The owner the local conversation cells are keyed under.

    The recorder keys a cell under the local OrgFS principal: the composed
    cell runtime's writer (``identity.writer``, never the uid authority;
    contract OC0 §1.6), else ``user:<owner>``.  The read side never composes
    the runtime (it must answer without OrgFS); it takes the composed
    runtime's writer when there is one.
    """

    identity = getattr(getattr(app, "_cell_runtime", None), "identity", None)
    writer = getattr(identity, "writer", None)
    if isinstance(writer, str) and writer.startswith("user:"):
        return writer
    owner = str(app.owner)
    return owner if owner.startswith("user:") else canonical_user_uri(owner)


def history_answer(
    params: JsonObject,
    *,
    read: HistoryRead,
    participant: str,
    conversation_id: str,
    classed: bool = False,
    project: Callable[[Any], JsonObject] | None = None,
) -> JsonObject:
    """One history page over *read*, bound to the caller and conversation.

    ``message.history`` and ``conversations.history`` share this engine:
    *read* takes ``after``, ``before``, ``limit`` and ``newest_first`` and
    returns the conversation's rows in that direction; ``conversation_id``
    is the id the cursors are bound to.  *classed* adds ``messageClass`` to
    the entries of the page returned without a mode (the modes always
    carry it).  *project* replaces the classed entry projection
    (group history, slice GC3).
    """

    entry = project or _classed_entry
    limit = _history_limit(params.get("limit"))
    mode = _history_mode(params.get("mode"))
    if mode is None:
        after = _history_after(
            params.get("cursor"),
            conversation_id=conversation_id,
            participant=participant,
        )
        rows = read(after=after, limit=min(limit + 1, HISTORY_READ_LIMIT_MAX))
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = (
            _encode_history_cursor(
                page[-1],
                conversation_id=conversation_id,
                participant=participant,
            )
            if has_more
            else None
        )
        return {
            "ok": True,
            "conversationId": conversation_id,
            "entries": [
                entry(message) if classed else _query_entry(message, kind="message")
                for message in page
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
            read,
            participant=participant,
            conversation_id=conversation_id,
            limit=limit,
            entry=entry,
        )
    if mode == "older":
        position = _history_cursor_position(
            params.get("cursor"),
            conversation_id=conversation_id,
            participant=participant,
            allowed_kinds=frozenset({_HISTORY_KIND_BEFORE}),
            allow_v1=False,
        )
        return _older_page(
            read,
            participant=participant,
            conversation_id=conversation_id,
            position=position,
            limit=limit,
            entry=entry,
        )
    position = _history_cursor_position(
        params.get("cursor"),
        conversation_id=conversation_id,
        participant=participant,
        allowed_kinds=frozenset({_HISTORY_KIND_AFTER}),
        allow_v1=True,
    )
    return _after_page(
        read,
        participant=participant,
        conversation_id=conversation_id,
        position=position,
        limit=limit,
        entry=entry,
    )


class _AliasedConversationReader:
    """One participant's conversation read under all its consumer read keys."""

    def __init__(self, inbox: Any, keys: tuple[str, ...]) -> None:
        self._inbox = inbox
        self._keys = keys

    def conversation_messages(
        self,
        participant: str,
        conversation_id: str,
        *,
        after: tuple[int, str] | None = None,
        before: tuple[int, str] | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[Any, ...]:
        """The first ``limit`` rows over every key, in the walk's keyset order.

        Each key's own page is already the first ``limit`` rows of that key
        past the bound, so their merge holds the first ``limit`` rows of
        the union; a message both keys see is kept once.
        """

        keys = self._keys if self._keys[:1] == (participant,) else (participant,)
        merged: dict[str, Any] = {}
        for key in keys:
            for message in self._inbox.conversation_messages(
                key, conversation_id, after=after, before=before,
                limit=limit, newest_first=newest_first,
            ):
                merged.setdefault(message.message_id, message)
        ordered = sorted(
            merged.values(),
            key=lambda message: (message.created_at_ms, message.message_id),
            reverse=newest_first,
        )
        return tuple(ordered[:limit])


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
        or created_at_ms > 2**63 - 1
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
        or created_at_ms > 2**63 - 1
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise _invalid_history_cursor()
    return created_at_ms, message_id


def _tail_page(
    read: HistoryRead,
    *,
    participant: str,
    conversation_id: str,
    limit: int,
    entry: Callable[[Any], JsonObject],
) -> JsonObject:
    rows = read(limit=min(limit + 1, HISTORY_READ_LIMIT_MAX), newest_first=True)
    has_older = len(rows) > limit
    newest = rows[:limit]
    return _v2_page(
        mode="tail",
        entry=entry,
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
    read: HistoryRead,
    *,
    participant: str,
    conversation_id: str,
    position: tuple[int, str],
    limit: int,
    entry: Callable[[Any], JsonObject],
) -> JsonObject:
    rows = read(
        before=position,
        limit=min(limit + 1, HISTORY_READ_LIMIT_MAX),
        newest_first=True,
    )
    has_older = len(rows) > limit
    newest = rows[:limit]
    return _v2_page(
        mode="older",
        entry=entry,
        conversation_id=conversation_id,
        entries=tuple(reversed(newest)),
        head_cursor=_head_cursor(
            read,
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
    read: HistoryRead,
    *,
    participant: str,
    conversation_id: str,
    position: tuple[int, str],
    limit: int,
    entry: Callable[[Any], JsonObject],
) -> JsonObject:
    rows = read(after=position, limit=min(limit + 1, HISTORY_READ_LIMIT_MAX))
    has_more = len(rows) > limit
    page = rows[:limit]
    return _v2_page(
        mode="after",
        entry=entry,
        conversation_id=conversation_id,
        entries=page,
        head_cursor=_head_cursor(
            read,
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
    read: HistoryRead,
    *,
    participant: str,
    conversation_id: str,
) -> str | None:
    rows = read(limit=1, newest_first=True)
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
    entry: Callable[[Any], JsonObject],
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
        "entries": [entry(message) for message in entries],
        "headCursor": head_cursor,
        "nextCursor": next_cursor,
        "nextOlderCursor": next_older,
    }


def _classed_entry(message: Any) -> JsonObject:
    entry = {
        **_query_entry(message, kind="message"),
        "messageClass": message_class_of(message).value,
    }
    omitted = getattr(message, "body_omitted", None)
    if isinstance(omitted, str) and omitted:
        entry["bodyOmitted"] = omitted
    return entry


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
