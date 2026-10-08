"""Crash-safe persistent Lark message correlation state, backed by SQLite.

One shared database (``~/.hyprial/state/adapters.sqlite3``) serves every adapter
process on the host; rows are namespaced by an ``adapter`` column.  The
storage conventions follow ``hyprial.inbox.service``: WAL journal, full
synchronous, and a busy timeout so concurrent adapter processes queue on the
write lock instead of failing.  Mutations run under ``BEGIN IMMEDIATE`` so a
read-modify-write (capacity checks, upsert-detection) is atomic across
processes, not just across threads.

There is no migration from the JSON era (product decision: the old runtime
state is void after the cutover).  A pre-SQLite state file found on disk is
renamed with a ``.retired`` suffix -- kept for audit, never read -- and the
store starts empty.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from hyprial.daemon.impl.adapters.lark.contracts.messages import MAX_LARK_TEXT_CONTENT_BYTES
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    PendingCommandCapacityStatus,
    encode_lark_text_content,
)
#: Version 2 is the SQLite schema.  Version 1 was the retired JSON layout;
#: a version mismatch fails closed instead of guessing at the contents.
STATE_SCHEMA_VERSION = 2
#: The ``identities`` table is versioned under its own meta key
#: (``identitiesSchemaVersion``) instead of bumping ``schemaVersion``.
#: Deliberate: deployed v2 builds exact-match the global stamp and fail
#: closed on anything else, while the table itself is purely additive and
#: invisible to them -- so an existing database gains the table on first
#: open by this build *without* bricking a v2 worker that restarts before
#: the rollout completes.  Once the fleet is fully on identities-aware
#: builds, a later migration may collapse this key into ``schemaVersion``.
IDENTITIES_SCHEMA_VERSION = 1
MAX_CORRELATIONS = 10_000
#: Pending replies are custody records, not a cache: unresolved records may
#: never be evicted.  New custody fails closed at either independent limit.
MAX_PENDING_COMMAND_RESPONSES = MAX_CORRELATIONS
MAX_PENDING_COMMAND_RESPONSE_BYTES = 16 * 1024 * 1024
#: Dead letters are an operator-facing audit trail, not a queue: bounded hard
#: so a broken deployment cannot grow the database without limit.
MAX_DEAD_LETTERS = 500
#: Inbound text is preserved for traceability, but truncated so one giant
#: message cannot bloat the audit trail.
DEAD_LETTER_TEXT_LIMIT = 2_000
DEAD_LETTER_DETAIL_LIMIT = 500
# Full, immutable outbound requests are custody, not the truncated audit trail.
MAX_PENDING_INBOUND_SUBMISSIONS = 1024
MAX_PENDING_INBOUND_SUBMISSION_BYTES = 16 * 1024 * 1024

#: Namespace used when a caller does not name its adapter (tests, ad hoc
#: tooling).  Production workers always pass their gateway name.
DEFAULT_ADAPTER = "default"

#: Closed identity taxonomies.  ``kind`` says what the platform id denotes:
#: a human (``user``, open_id), an app's bot presence (``bot``, open_id) or
#: the app itself (``app``, app_id).  ``standing`` says how much the mapping
#: may be trusted: ``observed`` rows were collected mechanically and may be
#: refreshed by any later observation; ``verified`` rows were confirmed by a
#: human and are never downgraded or overwritten by observations.
IDENTITY_KINDS = ("user", "bot", "app")
IDENTITY_STANDINGS = ("observed", "verified")

_SQLITE_MAGIC = b"SQLite format 3\x00"
_BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True, slots=True)
class RequestCorrelation:
    harness_message_id: str
    message_id: str
    chat_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ReplyRoute:
    message_id: str
    actor_id: str
    actor_key: str
    harness_message_id: str
    conversation_id: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class PendingCommandResponse:
    """Immutable Lark response persisted before its first send attempt."""

    message_id: str
    event_id: str
    kind: str
    text: str
    idempotency_key: str


class PendingCommandCapacityError(RuntimeError):
    """A new response cannot be durably accepted without losing custody."""

    def __init__(self, capacity: PendingCommandCapacityStatus) -> None:
        super().__init__("pending Lark command response capacity exhausted")
        self.capacity = capacity


class PendingInboundSubmissionCapacityError(RuntimeError):
    """A new request cannot be durably retained before daemon submission."""


def pending_command_response_encoded_bytes(response: PendingCommandResponse) -> int:
    """Return one response's exact charged UTF-8 JSON bytes."""

    return _pending_command_responses_encoded_bytes(
        {response.message_id: response}
    )


def _pending_command_responses_encoded_bytes(
    responses: dict[str, PendingCommandResponse],
) -> int:
    """Charge the larger exact durable-state or aggregate SDK representation.

    The durable budget is measured against compact, sorted,
    ``ensure_ascii=False`` JSON -- the JSON-era charging rule, kept verbatim
    so the byte limit means the same thing across the storage cutover.
    Lark's generated SDK receives the JSON string produced by
    :func:`encode_lark_text_content`.  The budget charges the larger exact
    representation, so neither persisted escaping nor SDK-wire escaping can
    bypass the byte limit. An empty/released budget reports zero bytes.
    """

    if not responses:
        return 0
    durable_map = {
        message_id: asdict(response)
        for message_id, response in responses.items()
    }
    durable_bytes = len(
        json.dumps(
            durable_map,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    sdk_bytes = sum(
        len(encode_lark_text_content(response.text).encode("utf-8"))
        for response in responses.values()
    )
    return max(durable_bytes, sdk_bytes)


def _pending_responses(
    values: dict[str, Any],
) -> dict[str, PendingCommandResponse]:
    parsed: dict[str, PendingCommandResponse] = {}
    for message_id, raw in values.items():
        try:
            if not isinstance(message_id, str) or not message_id:
                raise ValueError
            if not isinstance(raw, dict):
                raise TypeError
            response = PendingCommandResponse(**raw)
            if response.message_id != message_id or not all(
                isinstance(value, str) and value
                for value in (
                    response.message_id,
                    response.event_id,
                    response.kind,
                    response.text,
                    response.idempotency_key,
                )
            ):
                raise ValueError
            # A pending response must remain sendable after restart.  Apply
            # the platform's strict per-item wire limit at the shared parser
            # used by both load and record, so a stored response can never
            # enter durable custody when Lark would reject its body.
            if (
                len(encode_lark_text_content(response.text).encode("utf-8"))
                >= MAX_LARK_TEXT_CONTENT_BYTES
            ):
                raise ValueError
            # Validate the aggregate encodings eagerly too. Lone surrogates
            # and other corrupt persisted material must fail closed at load.
            pending_command_response_encoded_bytes(response)
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("invalid pending Lark command response") from error
        parsed[message_id] = response
    return parsed


def _pending_capacity(
    values: dict[str, Any], *, force_full: bool = False
) -> PendingCommandCapacityStatus:
    responses = _pending_responses(values)
    encoded_bytes = _pending_command_responses_encoded_bytes(responses)
    full = force_full or len(responses) >= MAX_PENDING_COMMAND_RESPONSES or (
        encoded_bytes >= MAX_PENDING_COMMAND_RESPONSE_BYTES
    )
    return PendingCommandCapacityStatus(
        status="full" if full else "available",
        count=len(responses),
        bytes=encoded_bytes,
        max_count=MAX_PENDING_COMMAND_RESPONSES,
        max_bytes=MAX_PENDING_COMMAND_RESPONSE_BYTES,
    )


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """An inbound user message whose body would otherwise be unrecoverable.

    Keyed by the native Lark message id so an operator can later replay it
    with ``get_inbound_message``/``recover_message``.  Contains only message
    content and routing metadata — never credentials.
    """

    message_id: str
    event_id: str
    chat_id: str
    chat_type: str
    conversation_id: str
    sender_id: str
    sender_type: str
    text: str
    reason: str
    created_at: str
    detail: str | None = None
    create_time: str | None = None
    reply_to: str | None = None
    message_type: str | None = None


@dataclass(frozen=True, slots=True)
class Identity:
    """One platform identity ↔ display name ↔ hyprial ownership mapping.

    Identity lookups must run on recorded data, never on live-chat deduction:
    a member view truncated mid-page once "proved" a wrong who-is-who by
    elimination.  Rows carry their provenance (``source``) and confidence
    (``standing``) so a consumer can always tell a mechanical observation
    from a human-confirmed mapping.
    """

    kind: str
    platform_id: str
    display_name: str | None = None
    #: Feishu's cross-App stable user id.  ``platform_id`` (an open_id) is
    #: namespaced *per App*: the same human has a different open_id under
    #: every App, so rows from different adapters can only be joined through
    #: this column (or a human).  Stored when a source carries it (live
    #: message events do); never required.
    union_id: str | None = None
    hyprial_owner: str | None = None
    standing: str = "observed"
    source: str = ""
    first_seen_ms: int | None = None
    last_seen_ms: int | None = None


#: Identity fields in declaration order, so a row selected with these columns
#: constructs the dataclass directly.
_IDENTITY_COLUMNS = (
    "kind, platform_id, display_name, union_id, hyprial_owner, standing, source,"
    " first_seen_ms, last_seen_ms"
)


def _checked_identity(identity: Identity) -> Identity:
    if identity.kind not in IDENTITY_KINDS:
        raise ValueError(
            f"identity kind must be one of {', '.join(IDENTITY_KINDS)}"
        )
    if identity.standing not in IDENTITY_STANDINGS:
        raise ValueError(
            f"identity standing must be one of {', '.join(IDENTITY_STANDINGS)}"
        )
    if not identity.platform_id:
        raise ValueError("identity platform_id must be non-empty")
    if not identity.source:
        raise ValueError("identity source must be non-empty")
    return identity


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


#: DeadLetter fields in declaration order, so a row selected with these
#: columns constructs the dataclass directly.
_DEAD_LETTER_COLUMNS = (
    "message_id, event_id, chat_id, chat_type, conversation_id, sender_id,"
    " sender_type, text, reason, created_at, detail, create_time, reply_to,"
    " message_type"
)

_PENDING_COLUMNS = "message_id, event_id, kind, text, idempotency_key"
