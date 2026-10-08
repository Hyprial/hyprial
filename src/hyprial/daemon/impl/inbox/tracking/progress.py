"""Progress-event contract (route C): the ``hyprial-progress-event/v1`` shape.

A progress event is a side-channel observation of one in-flight delivery
("running bash", "third tool finished", "compacting").  It is carried in an
``InboxMessage.payload`` with ``intent="progress"``, stored in its own table
on its own zenoh keyspace, and consumed through its own IPC method -- so the
notice dispatch path (which turns a stored notice into a PROMPT for the
local harness) can never read it.  See
``hq/notes/design-progress-events-route-c.md``.

Consumer rules pinned by this contract (golden-tested):

1. ``seq`` gaps are normal, never an error: the publish hop is
   receipt-less and presence-gated, and the sender coalesces.  A consumer
   seeing ``seq`` jump 3 -> 9 must keep working.
2. ``seq`` is never a reassembly cursor: every ``summary`` is
   self-contained.  This is the wall between route C and chunked streaming.
3. The only legal uses of ``seq`` are ordering and "how much did I miss"
   (with ``droppedSinceSeq``).
4. A progress event is never authoritative output: the terminal reply via
   ``_reply_and_ack`` is the only authority.  Losing every event must lose
   nothing.
5. Progress events are not replyable, not ackable, and never enter the
   inbox FIFO (offered-once semantics, like notices).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Self

#: Fixed schema token; a decoder rejects any payload that does not carry it.
PROGRESS_SCHEMA = "hyprial-progress-event/v1"

#: The InboxMessage.intent for progress events.  Deliberately absent from
#: the proto VALID_INTENTS list: the frame encoder passes intent through
#: unchecked, and receive_progress_event guards on this exact value.
PROGRESS_INTENT = "progress"

#: Self-contained human summary bound (characters).
SUMMARY_LIMIT = 200

#: Whole-payload bound (bytes); overflow truncates ``detail``.
PAYLOAD_LIMIT = 2048

# Phase values.  Consumers MUST ignore unknown phases (forward compat), so
# decode keeps the raw string instead of validating against this set.
PHASE_TURN_START = "turn-start"
PHASE_THINKING = "thinking"
PHASE_MESSAGE_SEGMENT = "message-segment"
PHASE_TOOL_CALL = "tool-call"
PHASE_TOOL_RESULT = "tool-result"
PHASE_COMPACTION = "compaction"
PHASE_RETRY = "retry"
PHASE_TURN_END = "turn-end"

KNOWN_PHASES = frozenset(
    {
        PHASE_TURN_START,
        PHASE_THINKING,
        PHASE_MESSAGE_SEGMENT,
        PHASE_TOOL_CALL,
        PHASE_TOOL_RESULT,
        PHASE_COMPACTION,
        PHASE_RETRY,
        PHASE_TURN_END,
    }
)

#: Phases whose latest event survives coalescing individually (the daemon
#: emits at most one latest tool-call, one latest tool-result, and one
#: latest anything-else per delivery per tick).
COALESCE_KEPT_PHASES = frozenset({PHASE_TOOL_CALL, PHASE_TOOL_RESULT})


class ProgressEventError(ValueError):
    """A locally-constructed progress event violates the contract."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One self-contained progress observation of one delivery."""

    delivery_id: str
    conversation_id: str
    actor: str
    harness: str
    phase: str
    seq: int
    emitted_at_ms: int
    summary: str
    dropped_since_seq: int = 0
    tool_call_id: str | None = None
    tool_name: str | None = None
    detail: dict[str, Any] | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("delivery_id", self.delivery_id),
            ("conversation_id", self.conversation_id),
            ("actor", self.actor),
            ("harness", self.harness),
            ("phase", self.phase),
            ("summary", self.summary),
        ):
            if not isinstance(value, str) or not value:
                raise ProgressEventError(f"progress event {label} must be non-empty")
        if len(self.summary) > SUMMARY_LIMIT:
            raise ProgressEventError(
                f"progress event summary exceeds {SUMMARY_LIMIT} characters"
            )
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ProgressEventError("progress event seq must be a non-negative int")
        if (
            isinstance(self.emitted_at_ms, bool)
            or not isinstance(self.emitted_at_ms, int)
            or self.emitted_at_ms <= 0
        ):
            raise ProgressEventError(
                "progress event emitted_at_ms must be a positive int"
            )
        if (
            isinstance(self.dropped_since_seq, bool)
            or not isinstance(self.dropped_since_seq, int)
            or self.dropped_since_seq < 0
        ):
            raise ProgressEventError(
                "progress event dropped_since_seq must be a non-negative int"
            )
        if self.detail is not None and not isinstance(self.detail, dict):
            raise ProgressEventError("progress event detail must be an object")
        for label, value in (
            ("tool_call_id", self.tool_call_id),
            ("tool_name", self.tool_name),
        ):
            if value is not None and not isinstance(value, str):
                raise ProgressEventError(
                    f"progress event {label} must be a string when present"
                )

    def to_payload_dict(self) -> dict[str, Any]:
        """The on-wire object.

        ``message`` duplicates ``summary`` on purpose: generic payload
        readers (``runtime._message_text``, ``message.pending.list``) only
        understand ``body["message"]``, and without it they degrade to raw
        base64.  ``progressEvent`` is self-description for cross-layer
        readers; the structural isolation (own table, own keyspace) is the
        real defense, not this flag.
        """

        record: dict[str, Any] = {
            "schema": PROGRESS_SCHEMA,
            "progressEvent": True,
            "message": self.summary,
            "summary": self.summary,
            "deliveryId": self.delivery_id,
            "conversationId": self.conversation_id,
            "actor": self.actor,
            "harness": self.harness,
            "phase": self.phase,
            "seq": self.seq,
            "emittedAtMs": self.emitted_at_ms,
        }
        if self.dropped_since_seq:
            record["droppedSinceSeq"] = self.dropped_since_seq
        if self.tool_call_id is not None:
            record["toolCallId"] = self.tool_call_id
        if self.tool_name is not None:
            record["toolName"] = self.tool_name
        if self.detail is not None:
            record["detail"] = self.detail
        if self.terminal:
            record["terminal"] = True
        return record

    @classmethod
    def from_payload_dict(cls, record: object) -> Self | None:
        """Parse a decoded payload object; None on any contract violation.

        Unknown ``phase`` values are preserved, not rejected (forward
        compat).  ``schema``/``progressEvent`` mismatches and missing or
        mistyped required fields are rejections.
        """

        if not isinstance(record, dict):
            return None
        if record.get("schema") != PROGRESS_SCHEMA:
            return None
        if record.get("progressEvent") is not True:
            return None
        summary = record.get("summary")
        if record.get("message") != summary:
            # The compat field and the canonical field must not drift apart;
            # a payload where they disagree is not one of ours.
            return None
        detail = record.get("detail")
        if detail is not None and not isinstance(detail, dict):
            return None
        try:
            return cls(
                delivery_id=record.get("deliveryId"),  # type: ignore[arg-type]
                conversation_id=record.get("conversationId"),  # type: ignore[arg-type]
                actor=record.get("actor"),  # type: ignore[arg-type]
                harness=record.get("harness"),  # type: ignore[arg-type]
                phase=record.get("phase"),  # type: ignore[arg-type]
                seq=record.get("seq"),  # type: ignore[arg-type]
                emitted_at_ms=record.get("emittedAtMs"),  # type: ignore[arg-type]
                summary=summary,  # type: ignore[arg-type]
                dropped_since_seq=record.get("droppedSinceSeq", 0),  # type: ignore[arg-type]
                tool_call_id=record.get("toolCallId"),  # type: ignore[arg-type]
                tool_name=record.get("toolName"),  # type: ignore[arg-type]
                detail=detail,
                terminal=record.get("terminal") is True,
            )
        except ProgressEventError:
            return None


def encode_progress_event(event: ProgressEvent) -> bytes:
    """Encode one event as an InboxMessage payload, within PAYLOAD_LIMIT.

    Overflow truncates ``detail`` (replaced by a truncation marker) exactly
    once; an event that still exceeds the bound afterwards is a contract
    violation at the producer, not something to ship.
    """

    payload = _dumps(event.to_payload_dict())
    if len(payload) <= PAYLOAD_LIMIT:
        return payload
    if event.detail:
        truncated = ProgressEvent(
            delivery_id=event.delivery_id,
            conversation_id=event.conversation_id,
            actor=event.actor,
            harness=event.harness,
            phase=event.phase,
            seq=event.seq,
            emitted_at_ms=event.emitted_at_ms,
            summary=event.summary,
            dropped_since_seq=event.dropped_since_seq,
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            detail={"truncated": True},
            terminal=event.terminal,
        )
        payload = _dumps(truncated.to_payload_dict())
        if len(payload) <= PAYLOAD_LIMIT:
            return payload
    raise ProgressEventError(
        f"progress event payload exceeds {PAYLOAD_LIMIT} bytes"
    )


def decode_progress_event(payload: bytes) -> ProgressEvent | None:
    """Decode one payload; None on malformed JSON or contract violation."""

    try:
        record = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return ProgressEvent.from_payload_dict(record)


def _dumps(record: dict[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
