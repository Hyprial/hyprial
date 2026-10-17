"""App-facing chat adapter v0: the node side of the browser Core chat port.

The browser Core (``src/gui/core``) projects sessions, replies and terminal
state from ONE injected connection port with five named operations.  This
module is the node-side facade for that port on this machine: it binds
``sessions.send`` onto the daemon's existing ``message.send`` surface
(``conversationId`` + ``idempotencyKey`` pass through unchanged) and
``sessions.events`` onto the daemon's ``progress.list`` progress stream
(per-conversation, numeric ``seq`` cursor, the daemon's own terminal
marker projected as Core's terminal object).  It holds no
business authority of its own and stores nothing: every answer is a
projection of what the daemon returned, and success is only claimed where
the daemon claimed it.

v0 boundaries (deliberate, fail-closed):

- ``sessions.list`` is exactly the composition root's configured bindings:
  no daemon call, no discovered sessions, nothing invented.  It exists so
  Core's connect() can complete against the sessions this node really has.
- ``sessions.history`` projects the conversation's messages that the
  session's fixed sender can still read on the daemon's person-read surface:
  the sender's pending inbox rows (messages received, real replies included)
  plus the sender's undelivered outbox rows, filtered to the session's
  ``binding.conversation_id``, oldest first, deduplicated by message id.
  It is a projection, never a transcript: the daemon takes a received row
  out of the pending set when the harness answers it (``message.reply``
  acks the original) or acknowledges it, and consumed rows are not readable
  through any daemon IPC method at this head.  A durable conversation read
  that keeps consumed rows is a daemon-authority slice and is deliberately
  not synthesized here; missing rows are reported by absence, never
  invented.  (The store already retains consumed rows -- see
  ``hyprial.daemon.impl.inbox.authority.read.InboxReadProjection.workflow_replies``
  -- but no IPC surface exposes that shape yet.)
- The sender identity is fixed at construction by the trusted composition
  root; request fields that try to carry identity (``from``, ``sender``,
  ``actor``, ``onBehalfOf``, ``sessionRef``, ...) are rejected with
  :data:`~...errors.IDENTITY_REJECTED`.  The daemon authenticates the
  caller; this adapter never accepts one from the caller's payload.
- An unknown ``sessionId`` is rejected before any daemon call.  Session
  bindings (target(s) + daemon conversation id) are configuration of the
  composition root, not of the request.
- A daemon outcome that is not provably an acceptance (unknown submit
  outcome, transport failure, malformed answer) raises
  :class:`AppAdapterUnavailable` -- the caller must treat it as uncertain,
  keeping its original ``idempotencyKey`` for retry or lookup.
- ``acceptedAt`` has no source on the daemon's ``message.send`` answer and is
  deliberately absent (Core falls back to its own clock); the daemon's
  per-delivery ``duplicate`` marker passes through as ``deduplicated`` when
  every delivery carries it.  A mixed accept/reject send is reported as
  ``send-rejected`` with the accepted targets named in the detail -- the
  adapter never claims whole-send success for a partial outcome.

The adapter talks to the daemon through an injected ``request(method,
params)`` callable (the same shape the CLI's daemon client exposes).  It
does not open transports, read environments or discover credentials; the
transport kind (``local-ipc`` / ``tailnet``) is the bridge's business.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.gui.adapter.errors import (
    DAEMON_REJECTED,
    IDENTITY_REJECTED,
    INVALID_ARGUMENT,
    SEND_REJECTED,
    UNKNOWN_SESSION,
    AppAdapterError,
    AppAdapterUnavailable,
)

#: Request keys the browser Core is allowed to send on ``sessions.send``.
SEND_REQUEST_KEYS = frozenset({"text", "idempotencyKey"})

#: Request keys that would smuggle caller-supplied identity/authority.
IDENTITY_FIELDS = frozenset(
    {
        "from",
        "sender",
        "actor",
        "principal",
        "onBehalfOf",
        "sessionRef",
        "token",
        "credential",
    }
)

#: The v0 support matrix, mirrored in ``capabilities()``.
V0_OPERATIONS = {
    "capabilities.get": {"supported": True},
    "sessions.send": {"supported": True},
    "sessions.events": {"supported": True},
    "sessions.list": {"supported": True},
    "sessions.history": {"supported": True},
}

#: History page bounds.  Core clamps its own page size to 1..200 and sends it
#: explicitly; the adapter accepts the same window so a direct bridge call
#: cannot ask for an unbounded page.
HISTORY_PAGE_DEFAULT = 50
HISTORY_PAGE_MAX = 200

#: ``message.query`` inbox rows this projection consumes as conversation
#: messages.  The inbox view interleaves system notices (``kind: "notice"``);
#: a notice is not a message of the conversation and is never projected.
HISTORY_INBOX_KIND = "message"
#: ``message.query`` outbox rows (undelivered sends of the fixed sender).
HISTORY_OUTBOX_KIND = "outbox"


@dataclass(frozen=True, slots=True)
class SessionBinding:
    """One known session: daemon target(s) plus the daemon conversation id."""

    targets: tuple[str, ...]
    conversation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.targets, tuple) or not self.targets:
            raise ValueError("session binding needs at least one target")
        for target in self.targets:
            if not isinstance(target, str) or not target.strip():
                raise ValueError("session binding targets must be non-empty strings")
        if not isinstance(self.conversation_id, str) or not self.conversation_id.strip():
            raise ValueError("session binding conversation_id must be a non-empty string")


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppAdapterError(INVALID_ARGUMENT, f"{field} must be a non-empty string")
    return value


def _history_limit(limit: object) -> int:
    value = HISTORY_PAGE_DEFAULT if limit is None else limit
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= HISTORY_PAGE_MAX
    ):
        raise AppAdapterError(
            INVALID_ARGUMENT,
            f"limit must be an integer between 1 and {HISTORY_PAGE_MAX}",
        )
    return value


def _encode_history_cursor(row: Mapping[str, Any]) -> str:
    """The opaque page key: the row's ordering stamp plus its message id.

    The separator is deliberately not a colon: this key is a page cursor the
    adapter itself mints and consumes, never an identity/address grammar --
    colon decomposition of identity strings belongs to ``hyprial.uri`` alone
    (see the addressing guard in tests/kernel/addressing).
    """

    return f"{row['createdAtMs']}/{row['messageId']}"


def _decode_history_cursor(cursor: object) -> tuple[int, str]:
    """Parse a page key this adapter issued; anything else is refused."""

    if isinstance(cursor, str) and "/" in cursor:
        stamp, _, message_id = cursor.partition("/")
        if stamp.isdigit() and message_id:
            return (int(stamp), message_id)
    raise AppAdapterError(
        INVALID_ARGUMENT, "cursor must be a page key this adapter issued"
    )


def _history_at(created_at_ms: int) -> str:
    """RFC 3339 UTC timestamp, the shape Core's demo fixtures use."""

    return (
        datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _history_row(entry: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """One ``message.query`` entry, checked and normalized to the row shape.

    Both views of that method name the two ends with ``from``/``to``
    (``_query_entry``); the projection speaks one shape.  A row that does
    not carry the fields a conversation row must carry is an unknown
    answer, not a row to repair.
    """

    message_id = entry.get("messageId")
    conversation_id = entry.get("conversationId")
    created_at_ms = entry.get("createdAtMs")
    intent = entry.get("intent")
    message = entry.get("message")
    sender = entry.get("from")
    recipient = entry.get("to")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(conversation_id, str)
        or not conversation_id
        or isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or created_at_ms <= 0
        or not isinstance(intent, str)
        or not intent
        or not isinstance(message, str)
        or not isinstance(sender, str)
        or not sender
        or not isinstance(recipient, str)
        or not recipient
    ):
        raise AppAdapterUnavailable(
            "invalid-response", {"detail": f"message.query {kind} row is malformed"}
        )
    return {
        "messageId": message_id,
        "conversationId": conversation_id,
        "createdAtMs": created_at_ms,
        "intent": intent,
        "message": message,
        "from": sender,
        "to": recipient,
    }


class AppChatAdapter:
    """Node-side facade for the Core chat port (v0).

    ``request(method, params)`` performs one authenticated daemon call;
    ``sender`` is the fixed local sending actor; ``sessions`` maps the app's
    session ids to :class:`SessionBinding` configurations; ``session_ref``
    is the optional managed-worker fence attached to the sender claim.
    """

    def __init__(
        self,
        *,
        request: Callable[[str, dict], Any],
        sender: str,
        sessions: Mapping[str, SessionBinding],
        session_ref: str | None = None,
    ) -> None:
        if not callable(request):
            raise TypeError("request must be a callable performing one daemon call")
        if not isinstance(sender, str) or not sender.strip():
            raise ValueError("sender must be a non-empty actor string")
        if session_ref is not None and (
            not isinstance(session_ref, str) or not session_ref.strip()
        ):
            raise ValueError("session_ref must be a non-empty string when provided")
        if not isinstance(sessions, Mapping):
            raise ValueError("sessions must be a mapping of session id to binding")
        bindings: dict[str, SessionBinding] = {}
        for session_id, binding in sessions.items():
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("session ids must be non-empty strings")
            if not isinstance(binding, SessionBinding):
                raise ValueError(f"session {session_id!r} binding must be a SessionBinding")
            bindings[session_id] = binding
        self._request = request
        self._sender = sender
        self._session_ref = session_ref
        self._sessions = bindings

    # -- port operations ---------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """``capabilities.get`` -- the supported/unsupported v0 operation map."""
        return {"operations": {name: dict(flags) for name, flags in V0_OPERATIONS.items()}}

    def send(self, session_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """``sessions.send`` -- pass the text through the daemon's send surface.

        Returns ``{"status": "accepted", "commandId", "conversationId"}``
        only when the daemon proved acceptance; a definitive refusal raises
        :class:`AppAdapterError`, an unknown outcome raises
        :class:`AppAdapterUnavailable`.
        """
        binding = self._binding(session_id)
        body = self._validate_send_request(request)
        params: dict[str, Any] = {
            "from": self._sender,
            "to": list(binding.targets),
            "message": body["text"],
            "conversationId": binding.conversation_id,
            "idempotencyKey": body["idempotencyKey"],
        }
        if self._session_ref is not None:
            params["sessionRef"] = self._session_ref
        return self._project_send_ack(self._call("message.send", params))

    def events(self, session_id: str, since: int | None = None) -> dict[str, Any]:
        """``sessions.events`` -- project this session's delivery progress.

        v0 semantics: entries are the daemon's ``progress.list`` progress
        events for this adapter's fixed sender, filtered to the session's
        conversation (``binding.conversation_id``), in the order the daemon
        returned them.  ``since`` is the numeric cursor Core uses: ``None``
        or ``0`` means "from the beginning" (the daemon's first event of a
        stream carries ``seq`` 0, so zero must not filter it away), a
        positive integer returns the events whose daemon-assigned ``seq`` is
        strictly greater -- the daemon applies that filter itself.  ``seq``
        is the daemon's counter within one delivery stream, so a cursor
        names a position in the stream it came from; a cursor past the
        newest event simply returns no entries.

        ``terminal`` is ``null`` until the daemon reports a terminal
        progress event for this session, then the object Core renders: the
        daemon's terminal phase label as the state, the event id and its
        timestamp.  It is the node's own progress verdict -- reported, never
        synthesized.
        """
        binding = self._binding(session_id)
        if since is not None and (
            isinstance(since, bool) or not isinstance(since, int) or since < 0
        ):
            raise AppAdapterError(
                INVALID_ARGUMENT, "since must be a non-negative integer cursor"
            )
        params: dict[str, Any] = {"from": self._sender}
        if since:
            params["sinceSeq"] = since
        result = self._call("progress.list", params)
        events = self._progress_events(result)
        entries: list[dict[str, Any]] = []
        terminal: dict[str, Any] | None = None
        for event in events:
            if event["conversationId"] != binding.conversation_id:
                continue
            entries.append(
                {
                    "id": event["messageId"],
                    "seq": event["seq"],
                    "state": event["phase"],
                    "at": event["emittedAtMs"],
                    "summary": event["summary"],
                }
            )
            if event.get("terminal") is True:
                terminal = {
                    "terminal": event["phase"],
                    "eventId": event["messageId"],
                    "at": event["emittedAtMs"],
                }
        return {"entries": entries, "terminal": terminal}

    def sessions_list(self) -> list[dict[str, Any]]:
        """``sessions.list`` -- the sessions this composition root configured.

        v0 semantics: the browser cannot invent a session identity and the
        adapter holds no session authority of its own, so the list is exactly
        the configured bindings, in configuration order.  No daemon call is
        made; richer discovery belongs to the daemon-authority slice.
        """
        return [{"sessionId": session_id} for session_id in self._sessions]

    def session_binding(self, session_id: str) -> SessionBinding | None:
        """The configured binding for ``session_id``, or ``None``.

        The authorization matrix reads this through the host; the mapping
        itself stays exactly what the composition root configured -- the
        adapter still invents no session and widens no binding.
        """

        return self._sessions.get(session_id)

    def history(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``sessions.history`` -- project this conversation's visible messages.

        Rows come from the daemon's person-read surface (``message.query``)
        for this adapter's fixed sender: its pending inbox -- messages this
        session received, a real reply included while the daemon still holds
        it -- and its undelivered outbox, both filtered to the session's
        ``binding.conversation_id``.  Events are ordered oldest first; the
        ordering key plus the message id form the opaque ``nextCursor``.

        Direction is read off the row, not guessed: a row whose intent is
        ``reply``, or whose sender is not this adapter's fixed sender, is the
        conversation's assistant side (Core's ``assistant`` kind); every
        other row is the user side.  Messages the daemon has already consumed
        (a replied-to request, an acknowledged row) are outside every IPC
        read at this head and are reported by absence -- never invented; a
        malformed daemon answer is an unknown outcome, not a partial history.
        """
        binding = self._binding(session_id)
        page_size = _history_limit(limit)
        after = _decode_history_cursor(cursor) if cursor is not None else None
        rows = self._history_rows(binding)
        if after is not None:
            rows = [
                row
                for row in rows
                if (row["createdAtMs"], row["messageId"]) > after
            ]
        page = rows[:page_size]
        next_cursor = _encode_history_cursor(page[-1]) if len(rows) > len(page) else None
        events = [
            {
                "eventId": row["messageId"],
                "kind": (
                    "assistant"
                    if row["intent"] == "reply" or row["from"] != self._sender
                    else "user"
                ),
                "text": row["message"],
                "at": _history_at(row["createdAtMs"]),
            }
            for row in page
        ]
        return {"events": events, "nextCursor": next_cursor}

    # -- internals ---------------------------------------------------------

    def _binding(self, session_id: object) -> SessionBinding:
        if not isinstance(session_id, str):
            raise AppAdapterError(INVALID_ARGUMENT, "session id must be a string")
        binding = self._sessions.get(session_id)
        if binding is None:
            raise AppAdapterError(
                UNKNOWN_SESSION,
                f"unknown session {session_id!r}",
                {"sessionId": session_id},
            )
        return binding

    def _history_query(self, *, view: str, actor: str) -> list[Mapping[str, Any]]:
        """One ``message.query`` view for the fixed sender, shape-checked."""

        result = self._call("message.query", {"view": view, "actor": actor})
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": f"message.query {view} answer is not a success object"},
            )
        entries = result.get("entries")
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) for entry in entries
        ):
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": f"message.query {view} answer has no entries list"},
            )
        return entries

    def _history_rows(self, binding: SessionBinding) -> list[dict[str, Any]]:
        """This session's conversation rows, oldest first, deduplicated.

        The inbox view interleaves system notices; unknown kinds are not
        conversation messages and are skipped, while a row of a known kind
        that is missing its fields is malformed and fails the whole read.
        A message the daemon holds on both surfaces (outbox awaiting
        delivery, inbox as the recipient's copy) is projected once -- the
        inbox copy, which names both ends.
        """

        merged: dict[str, dict[str, Any]] = {}
        for entry in self._history_query(view="inbox", actor=self._sender):
            if entry.get("kind") != HISTORY_INBOX_KIND:
                continue
            row = _history_row(entry, kind=HISTORY_INBOX_KIND)
            if row["conversationId"] != binding.conversation_id:
                continue
            merged.setdefault(row["messageId"], row)
        for entry in self._history_query(view="outbox", actor=self._sender):
            if entry.get("kind") != HISTORY_OUTBOX_KIND:
                continue
            row = _history_row(entry, kind=HISTORY_OUTBOX_KIND)
            if row["conversationId"] != binding.conversation_id:
                continue
            merged.setdefault(row["messageId"], row)
        return sorted(
            merged.values(), key=lambda row: (row["createdAtMs"], row["messageId"])
        )

    @staticmethod
    def _validate_send_request(request: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(request, Mapping):
            raise AppAdapterError(INVALID_ARGUMENT, "send request must be a mapping")
        unexpected = set(request) - SEND_REQUEST_KEYS
        identity = unexpected & IDENTITY_FIELDS
        if identity:
            raise AppAdapterError(
                IDENTITY_REJECTED,
                "the adapter's sender is fixed by the composition root; "
                "identity fields are not accepted from the request",
                {"identityFields": sorted(identity)},
            )
        if unexpected:
            raise AppAdapterError(
                INVALID_ARGUMENT,
                "unexpected send request fields",
                {"unexpectedFields": sorted(unexpected)},
            )
        text = _require_text(request.get("text"), "text")
        idempotency_key = _require_text(request.get("idempotencyKey"), "idempotencyKey")
        return {"text": text, "idempotencyKey": idempotency_key}

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self._request(method, params)
        except ipc_errors.TransientDaemonError as error:
            raise AppAdapterUnavailable(
                "daemon-unavailable", {"detail": str(error)}
            ) from error
        except DaemonRequestError as error:
            raise AppAdapterError(
                DAEMON_REJECTED, str(error), {"code": error.code, "data": error.data}
            ) from error
        except (TimeoutError, OSError) as error:
            raise AppAdapterUnavailable(
                "daemon-unavailable", {"detail": str(error)}
            ) from error

    @staticmethod
    def _project_send_ack(result: Any) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise AppAdapterUnavailable("invalid-response", {"detail": "send answer is not an object"})
        ok = result.get("ok")
        operation_id = result.get("operationId")
        conversation_id = result.get("conversationId")
        deliveries = result.get("deliveries")
        if (
            not isinstance(ok, bool)
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(deliveries, list)
            or not deliveries
        ):
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "send answer is missing required fields"}
            )
        rejections: list[dict[str, Any]] = []
        unknowns: list[str] = []
        for delivery in deliveries:
            if not isinstance(delivery, Mapping) or not isinstance(delivery.get("accepted"), bool):
                raise AppAdapterUnavailable(
                    "invalid-response", {"detail": "delivery record is malformed"}
                )
            if delivery["accepted"]:
                continue
            target = str(delivery.get("target", ""))
            code = delivery.get("code")
            if delivery.get("outcomeKnown") is False or code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN:
                unknowns.append(target)
            else:
                rejections.append({"target": target, "code": code})
        if unknowns or not all(delivery["accepted"] for delivery in deliveries) and not rejections:
            raise AppAdapterUnavailable("outcome-unknown", {"targets": unknowns})
        if rejections:
            accepted = [
                str(delivery.get("target", ""))
                for delivery in deliveries
                if delivery["accepted"]
            ]
            detail: dict[str, Any] = {"rejections": rejections}
            if accepted:
                # Partial acceptance, stated as such: the send as a whole is
                # not claimed accepted, and the caller can still see which
                # targets did take the message.
                detail["acceptedTargets"] = accepted
            raise AppAdapterError(
                SEND_REJECTED,
                "the daemon did not accept the message",
                detail,
            )
        ack: dict[str, Any] = {
            "status": "accepted",
            "commandId": operation_id,
            "conversationId": conversation_id,
        }
        message_id = result.get("messageId")
        if isinstance(message_id, str) and message_id:
            ack["messageId"] = message_id
        # The daemon marks a replayed idempotent delivery per target; the
        # node's own dedup verdict passes through only when every delivery
        # carries the marker.  ``acceptedAt`` stays absent: the daemon
        # answer has no such field and inventing one would be a claim.
        if all(delivery.get("duplicate") is True for delivery in deliveries):
            ack["deduplicated"] = True
        return ack

    @staticmethod
    def _progress_events(result: Any) -> list[Mapping[str, Any]]:
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "progress answer is not a success object"}
            )
        events = result.get("events")
        if not isinstance(events, list):
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "progress answer has no events list"}
            )
        for event in events:
            if (
                not isinstance(event, Mapping)
                or not isinstance(event.get("messageId"), str)
                or not event["messageId"]
                or not isinstance(event.get("conversationId"), str)
                or not event["conversationId"]
                or not isinstance(event.get("phase"), str)
                or not event["phase"]
                or isinstance(event.get("seq"), bool)
                or not isinstance(event.get("seq"), int)
                or event["seq"] < 0
                or isinstance(event.get("emittedAtMs"), bool)
                or not isinstance(event.get("emittedAtMs"), int)
                or event["emittedAtMs"] <= 0
                or not isinstance(event.get("summary"), str)
            ):
                raise AppAdapterUnavailable(
                    "invalid-response", {"detail": "progress event is malformed"}
                )
        return events
