"""App-facing chat adapter v0: the node side of the browser Core chat port.

The shared browser Core projects sessions, replies and terminal state from
ONE injected connection port. The old ``src/gui`` tree was retired on
2026-10-27; the replacement UI will live under ``gui/``.  This module is the
node-side
facade for that port on this machine: it binds ``sessions.send`` onto the
daemon's existing ``message.send`` surface (``conversationId`` +
``idempotencyKey`` pass through unchanged), ``sessions.events`` onto the
daemon's ``progress.list`` progress stream, and the read-only directory plus
session creation onto the daemon's authoritative ``hosts`` / ``targets``
methods.  It holds no business authority of its own beyond the deterministic
session binding it derives: every directory answer is a projection of what
the daemon returned, and success is only claimed where the daemon claimed it.

v0 boundaries (deliberate, fail-closed):

- ``targets.list`` is one bounded, read-only directory read: the daemon's
  ``hosts`` result becomes ``nodes`` and its ``targets`` result filtered to
  ``kind=agent`` becomes ``targets``.  Rows are shape-checked and capped;
  nothing is synthesized, and no online/role claim is made beyond the
  daemon's own ``status`` string.
- ``sessions.create`` takes only a ``targetUri``.  It first checks the target
  against the same live daemon directory, derives a stable session id and
  conversation id from the fixed sender + target, and returns the existing
  binding when one is already configured or ensured.  The renderer cannot
  name a sender, targets list, conversation or session id.
- ``sessions.list`` is the composition root's configured bindings plus the
  deterministic bindings ensured through ``sessions.create``: no daemon call,
  no invented session.  It exists so Core's connect() can complete against
  the sessions this node really has.
- ``sessions.history`` projects one page of the session's durable daemon
  conversation read (``message.history``): retained inbox rows -- consumed
  and pending, real replies included -- plus still-undelivered outbox rows
  for the fixed sender, filtered to the session's
  ``binding.conversation_id``.  The daemon owns identity scoping, ordering,
  deduplication and the opaque page cursor; the adapter checks the answer's
  shape and fails closed on anything it cannot prove.  It is a projection,
  never a local transcript: rows the daemon does not return are reported by
  absence, never invented, and there is no fallback to a partial
  ``message.query`` read.
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

from hyprial.daemon import SUMMARY_MAX_BYTES

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.gui.adapter.errors import (
    DAEMON_REJECTED,
    IDENTITY_REJECTED,
    INVALID_ARGUMENT,
    SEND_REJECTED,
    UNKNOWN_SESSION,
    UNKNOWN_TARGET,
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
    "sessions.create": {"supported": True},
    "targets.list": {"supported": True},
}

#: History page bounds.  Core clamps its own page size to 1..200 and sends it
#: explicitly; the adapter accepts the same window so a direct bridge call
#: cannot ask for an unbounded page.
HISTORY_PAGE_DEFAULT = 50
HISTORY_PAGE_MAX = 200

#: Directory bounds.  One request can only return so many rows, and one row
#: only so many characters; the daemon remains the authority for the full set,
#: and the bounded projection says so via ``truncated``/``counts``.
DIRECTORY_ROW_LIMIT = 200
DIRECTORY_URI_MAX = 512
DIRECTORY_ID_MAX = 256
DIRECTORY_STATUS_MAX = 64

#: The only row kind ``message.history`` returns.  A notice or any other kind
#: is not a conversation message and makes the whole answer unknown.
HISTORY_ROW_KIND = "message"


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


def _directory_string(value: object, field: str, limit: int) -> str:
    """One bounded, non-empty directory field; anything else is unknown."""

    if not isinstance(value, str) or not value or len(value) > limit:
        raise AppAdapterUnavailable(
            "invalid-response",
            {"detail": f"directory {field} is not a bounded non-empty string"},
        )
    return value


def _directory_nodes(result: object) -> list[dict[str, Any]]:
    """Validate the daemon's ``hosts`` answer into bounded node rows."""

    if not isinstance(result, Mapping) or not isinstance(result.get("hosts"), list):
        raise AppAdapterUnavailable(
            "invalid-response", {"detail": "hosts answer has no hosts list"}
        )
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in result["hosts"]:
        if not isinstance(entry, Mapping):
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "hosts row is not an object"}
            )
        node_id = _directory_string(
            entry.get("nodeId"), "nodeId", DIRECTORY_ID_MAX
        )
        status = _directory_string(
            entry.get("status"), "node status", DIRECTORY_STATUS_MAX
        )
        if node_id in seen:
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "hosts answer repeats a node id"}
            )
        seen.add(node_id)
        nodes.append({"nodeId": node_id, "status": status})
    return nodes


def _directory_agents(result: object) -> list[dict[str, Any]]:
    """Validate the daemon's ``targets`` answer into bounded agent rows.

    Only rows the daemon itself marks ``targetKind=agent`` and deliverable are
    accepted; a row of another kind in a ``kind=agent`` answer, or an agent
    row the daemon says is not deliverable, makes the whole answer unknown.
    """

    if not isinstance(result, Mapping) or not isinstance(result.get("targets"), list):
        raise AppAdapterUnavailable(
            "invalid-response", {"detail": "targets answer has no targets list"}
        )
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in result["targets"]:
        if not isinstance(entry, Mapping):
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "targets row is not an object"}
            )
        if entry.get("targetKind") != "agent":
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "targets answer contains a non-agent row"},
            )
        uri = _directory_string(
            entry.get("targetUri"), "targetUri", DIRECTORY_URI_MAX
        )
        if entry.get("deliverable") is not True:
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "agent target is not marked deliverable"},
            )
        status = _directory_string(
            entry.get("status"), "target status", DIRECTORY_STATUS_MAX
        )
        if uri in seen:
            raise AppAdapterUnavailable(
                "invalid-response", {"detail": "targets answer repeats a target"}
            )
        row: dict[str, Any] = {
            "targetUri": uri,
            "targetKind": "agent",
            "status": status,
        }
        if "hosted" in entry:
            if not isinstance(entry["hosted"], bool):
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "agent target hosted is not a boolean"},
                )
            row["hosted"] = entry["hosted"]
        if "hostedBy" in entry:
            hosted_by = entry["hostedBy"]
            if hosted_by is not None and (
                not isinstance(hosted_by, str)
                or not hosted_by
                or len(hosted_by) > DIRECTORY_URI_MAX
            ):
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "agent target hostedBy is not a bounded string"},
                )
            row["hostedBy"] = hosted_by
        seen.add(uri)
        agents.append(row)
    return agents


def _target_uri(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > DIRECTORY_URI_MAX
    ):
        raise AppAdapterError(
            INVALID_ARGUMENT,
            f"targetUri must be a non-empty string of at most {DIRECTORY_URI_MAX} characters",
        )
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


def _history_cursor(cursor: object) -> str | None:
    """The daemon's opaque cursor passes through untouched.

    The adapter neither parses nor re-mints it: only the daemon that issued a
    page key knows what it binds to, and a cursor it rejects is a definitive
    refusal, not a reason to restart or guess.
    """

    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise AppAdapterError(
            INVALID_ARGUMENT, "cursor must be a non-empty string when provided"
        )
    return cursor


def _history_at(created_at_ms: int) -> str:
    """RFC 3339 UTC timestamp, the shape Core's demo fixtures use."""

    return (
        datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _history_row(entry: object) -> dict[str, Any]:
    """One ``message.history`` entry, checked and normalized to the row shape.

    A row that is not a conversation message, or that does not carry the
    fields a conversation row must carry, is an unknown answer -- not a row
    to skip or repair.
    """

    if not isinstance(entry, Mapping) or entry.get("kind") != HISTORY_ROW_KIND:
        raise AppAdapterUnavailable(
            "invalid-response", {"detail": "message.history row is not a message"}
        )
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
        or intent not in {"request", "reply"}
        or not isinstance(message, str)
        or not isinstance(sender, str)
        or not sender
        or not isinstance(recipient, str)
        or not recipient
    ):
        raise AppAdapterUnavailable(
            "invalid-response", {"detail": "message.history row is malformed"}
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
        # ``sessions.create`` runs on a request thread: a second device or a
        # retry can ensure the same target concurrently, so the binding table
        # is read and mutated under one lock.
        self._sessions_lock = threading.Lock()
        self._ensured: dict[str, str] = {}

    # -- port operations ---------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """``capabilities.get`` -- the supported/unsupported v0 operation map."""
        return {"operations": {name: dict(flags) for name, flags in V0_OPERATIONS.items()}}

    def directory_operation(
        self, operation: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Dispatch the hosted directory operations with their own argument
        rules, so the host's generic dispatch stays one narrow branch."""

        if operation == "targets.list":
            return self.targets_list(params)
        return self.create_session_from(params)

    def targets_list(
        self, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """``targets.list`` -- the daemon's bounded node + agent directory.

        ``hosts`` answers which nodes the daemon can see; ``targets`` answers
        what it can deliver to.  This adapter asks the daemon for agents only
        and projects both answers verbatim apart from shape checks, stable
        row selection and the size bound.  A row the daemon did not return is
        absent -- never synthesized, deduplicated by invention or repaired.
        The frame carries no selectors.
        """

        if params:
            raise AppAdapterError(
                INVALID_ARGUMENT, "targets.list takes no parameters"
            )
        nodes = _directory_nodes(self._call("hosts", {}))
        agents = _directory_agents(self._call("targets", {"kind": "agent"}))
        return {
            "nodes": nodes[:DIRECTORY_ROW_LIMIT],
            "targets": agents[:DIRECTORY_ROW_LIMIT],
            "counts": {"nodes": len(nodes), "targets": len(agents)},
            "truncated": {
                "nodes": len(nodes) > DIRECTORY_ROW_LIMIT,
                "targets": len(agents) > DIRECTORY_ROW_LIMIT,
            },
        }

    def create_session(self, target_uri: object) -> dict[str, Any]:
        """``sessions.create`` -- create or reuse one stable agent binding.

        The caller names only a target URI.  The sender is the composition
        root's fixed sender, and the target must be a live agent row in the
        same daemon ``targets`` directory the UI lists.  The binding is
        deterministic in ``(sender, target)``: a restart re-derives the same
        session id and daemon conversation id, so history is restored without
        storing a second authority.  An existing configured or ensured
        single-target binding is reused rather than duplicated.
        """

        target = _target_uri(target_uri)
        agents = _directory_agents(self._call("targets", {"kind": "agent"}))
        match = next(
            (row for row in agents if row["targetUri"] == target),
            None,
        )
        if match is None:
            raise AppAdapterError(
                UNKNOWN_TARGET,
                "the daemon's current agent directory does not contain this target",
                {"targetUri": target},
            )
        with self._sessions_lock:
            for session_id, binding in self._sessions.items():
                if binding.targets == (target,):
                    return {
                        "sessionId": session_id,
                        "targetUri": target,
                        "status": match["status"],
                        "created": False,
                    }
            session_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial:gui:session:{self._sender}:{target}",
                )
            )
            conversation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial:gui:conversation:{self._sender}:{target}",
                )
            )
            self._sessions[session_id] = SessionBinding(
                targets=(target,), conversation_id=conversation_id
            )
            self._ensured[session_id] = target
        return {
            "sessionId": session_id,
            "targetUri": target,
            "status": match["status"],
            "created": True,
        }

    def create_session_from(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """``sessions.create`` frame rules: one target, never caller identity."""

        unexpected = set(params) - {"targetUri"}
        identity = unexpected & IDENTITY_FIELDS
        if identity:
            raise AppAdapterError(
                IDENTITY_REJECTED,
                "sessions.create may not carry identity fields",
                {"identityFields": sorted(identity)},
            )
        if unexpected:
            raise AppAdapterError(
                INVALID_ARGUMENT,
                "unexpected sessions.create fields",
                {"unexpectedFields": sorted(unexpected)},
            )
        return self.create_session(params.get("targetUri"))

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
            "summary": body["text"].encode("utf-8")[:SUMMARY_MAX_BYTES].decode("utf-8", errors="ignore"),
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
        """``sessions.list`` -- configured and ensured bindings, in order.

        The browser cannot invent a session identity.  Configured bindings
        keep their frozen ``{"sessionId"}`` shape; bindings ensured through
        ``sessions.create`` also carry the target they were created for so
        the directory UI can label them.  No daemon call is made.
        """
        with self._sessions_lock:
            rows = [{"sessionId": session_id} for session_id in self._sessions]
            for row in rows:
                target = self._ensured.get(row["sessionId"])
                if target is not None:
                    row["title"] = target
                    row["targetUri"] = target
            return rows

    def session_binding(self, session_id: str) -> SessionBinding | None:
        """The configured or ensured binding for ``session_id``, or ``None``.

        The authorization matrix reads this through the host; the adapter
        never invents a session beyond the deterministic binding it just
        ensured for a live daemon target.
        """

        with self._sessions_lock:
            return self._sessions.get(session_id)

    def history(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``sessions.history`` -- one page of the durable conversation.

        Rows come from the daemon's ``message.history`` read for this
        adapter's fixed sender and the session's
        ``binding.conversation_id``.  Unlike the old pending-only projection,
        consumed and acknowledged rows stay visible, so a request that was
        answered and a real reply that was consumed survive a refresh or a
        reconnect.  Ordering, participant scoping, deduplication and the
        opaque ``nextCursor`` belong to the daemon; the adapter only checks
        that the answer is internally consistent.

        Direction is read off the row, not guessed: a row whose intent is
        ``reply``, or whose sender is not this adapter's fixed sender, is the
        conversation's assistant side (Core's ``assistant`` kind); every
        other row is the user side.  A malformed or non-success answer is an
        unknown outcome, never a partial history.
        """
        binding = self._binding(session_id)
        page_size = _history_limit(limit)
        page_cursor = _history_cursor(cursor)
        params: dict[str, Any] = {
            "from": self._sender,
            "conversationId": binding.conversation_id,
            "limit": page_size,
        }
        if self._session_ref is not None:
            params["sessionRef"] = self._session_ref
        if page_cursor is not None:
            params["cursor"] = page_cursor
        return self._project_history(
            self._call("message.history", params),
            binding=binding,
        )

    # -- internals ---------------------------------------------------------

    def _binding(self, session_id: object) -> SessionBinding:
        if not isinstance(session_id, str):
            raise AppAdapterError(INVALID_ARGUMENT, "session id must be a string")
        with self._sessions_lock:
            binding = self._sessions.get(session_id)
        if binding is None:
            raise AppAdapterError(
                UNKNOWN_SESSION,
                f"unknown session {session_id!r}",
                {"sessionId": session_id},
            )
        return binding

    def _project_history(
        self,
        result: Any,
        *,
        binding: SessionBinding,
    ) -> dict[str, Any]:
        """Validate one ``message.history`` answer and project it to Core rows.

        The daemon promises conversation scoping, ascending
        ``(createdAtMs, messageId)`` order and unique message ids.  An answer
        that breaks any of those promises is not repaired or partially
        returned: it is an unknown outcome for the whole page.
        """

        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "message.history answer is not a success object"},
            )
        if result.get("conversationId") != binding.conversation_id:
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "message.history answer is for another conversation"},
            )
        entries = result.get("entries")
        if not isinstance(entries, list):
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "message.history answer has no entries list"},
            )
        if "nextCursor" not in result:
            raise AppAdapterUnavailable(
                "invalid-response",
                {"detail": "message.history answer has no nextCursor"},
            )
        rows = [_history_row(entry) for entry in entries]
        seen: set[str] = set()
        previous: tuple[int, str] | None = None
        for row in rows:
            if row["conversationId"] != binding.conversation_id:
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "message.history row is from another conversation"},
                )
            if row["messageId"] in seen:
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "message.history answer repeats a message id"},
                )
            key = (row["createdAtMs"], row["messageId"])
            if previous is not None and key <= previous:
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "message.history answer is not strictly ordered"},
                )
            seen.add(row["messageId"])
            previous = key
        next_cursor = result.get("nextCursor")
        if next_cursor is not None:
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "message.history nextCursor is not a cursor"},
                )
            if not rows:
                # A cursor with no page cannot advance: accepting it would
                # either loop forever or silently truncate the conversation.
                raise AppAdapterUnavailable(
                    "invalid-response",
                    {"detail": "message.history cursor accompanies an empty page"},
                )
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
            for row in rows
        ]
        return {"events": events, "nextCursor": next_cursor}

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
