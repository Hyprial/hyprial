"""Golden tests for the hyprial-progress-event/v1 contract (route C, T1).

These pin the five consumer rules from the design doc as executable
behavior: seq gaps are normal, seq is never a reassembly cursor, progress
is never authoritative, and the payload shape is exactly what every layer
(zenoh frame, inbox table, IPC consumer) will see.
"""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.inbox.tracking.progress import (
    KNOWN_PHASES,
    PAYLOAD_LIMIT,
    PROGRESS_INTENT,
    SUMMARY_LIMIT,
    ProgressEvent,
    ProgressEventError,
    decode_progress_event,
    encode_progress_event,
)


def event(**overrides: object) -> ProgressEvent:
    fields: dict[str, object] = {
        "delivery_id": "delivery-1",
        "conversation_id": "conversation-1",
        "actor": "agent:op:node-1:worker",
        "harness": "pi",
        "phase": "tool-call",
        "seq": 0,
        "emitted_at_ms": 1_700_000_000_000,
        "summary": "running read on src/main.py",
    }
    fields.update(overrides)
    return ProgressEvent(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Shape: exactly the fields the design doc fixes, nothing more
# --------------------------------------------------------------------------- #


def test_payload_shape_is_the_contract_shape() -> None:
    record = json.loads(encode_progress_event(event()))

    assert record == {
        "schema": "hyprial-progress-event/v1",
        "progressEvent": True,
        # The compat field mirrors summary verbatim: generic readers only
        # understand body["message"].
        "message": "running read on src/main.py",
        "summary": "running read on src/main.py",
        "deliveryId": "delivery-1",
        "conversationId": "conversation-1",
        "actor": "agent:op:node-1:worker",
        "harness": "pi",
        "phase": "tool-call",
        "seq": 0,
        "emittedAtMs": 1_700_000_000_000,
    }
    # Optional fields are omitted, not null -- and the intent constant is
    # the value hop 5 must stamp on the InboxMessage.
    assert PROGRESS_INTENT == "progress"


def test_optional_fields_round_trip() -> None:
    original = event(
        phase="tool-result",
        seq=7,
        dropped_since_seq=3,
        tool_call_id="call-9",
        tool_name="bash",
        detail={"exitCode": 0},
        terminal=True,
    )

    decoded = decode_progress_event(encode_progress_event(original))

    assert decoded == original
    record = json.loads(encode_progress_event(original))
    assert record["droppedSinceSeq"] == 3
    assert record["terminal"] is True
    assert record["toolCallId"] == "call-9"
    assert record["toolName"] == "bash"
    assert record["detail"] == {"exitCode": 0}


def test_every_known_phase_round_trips() -> None:
    for phase in sorted(KNOWN_PHASES):
        original = event(phase=phase)
        assert decode_progress_event(encode_progress_event(original)) == original


# --------------------------------------------------------------------------- #
# Rule 1+2: seq gaps are normal; no reassembly API exists
# --------------------------------------------------------------------------- #


def test_seq_gaps_decode_normally() -> None:
    """A consumer seeing seq 3 -> 9 must keep working: decode never
    validates continuity, and the contract offers no reorder buffer."""

    gapped = [event(seq=seq, summary=f"step {seq}") for seq in (0, 1, 3, 9)]
    decoded = [decode_progress_event(encode_progress_event(item)) for item in gapped]

    assert decoded == gapped
    assert [item.seq for item in decoded if item is not None] == [0, 1, 3, 9]


def test_every_summary_is_self_contained() -> None:
    """Rule 2's teeth: a summary must be non-empty and human-sized; an
    empty fragment that only makes sense when concatenated is rejected at
    construction."""

    with pytest.raises(ProgressEventError, match="summary"):
        event(summary="")
    with pytest.raises(ProgressEventError, match="summary"):
        event(summary="x" * (SUMMARY_LIMIT + 1))


def test_unknown_phase_is_preserved_not_rejected() -> None:
    """Forward compat: a newer producer's phase must not break an older
    consumer -- decode keeps the raw string."""

    original = event(phase="future-phase")

    decoded = decode_progress_event(encode_progress_event(original))

    assert decoded is not None
    assert decoded.phase == "future-phase"


# --------------------------------------------------------------------------- #
# Rule: schema mismatch is a rejection, never a guess
# --------------------------------------------------------------------------- #


def test_decode_rejects_foreign_payloads() -> None:
    assert decode_progress_event(b"not json") is None
    assert decode_progress_event(json.dumps(["a", "list"]).encode()) is None
    assert decode_progress_event(
        json.dumps({"schema": "other/v1", "progressEvent": True}).encode()
    ) is None
    # A notice-shaped payload (the prompt-injection hazard) is not a
    # progress event even though it carries "message".
    notice = {"message": "alarm", "systemNotice": True}
    assert decode_progress_event(json.dumps(notice).encode()) is None


@pytest.mark.parametrize(
    "missing",
    [
        "schema",
        "progressEvent",
        "message",
        "summary",
        "deliveryId",
        "conversationId",
        "actor",
        "harness",
        "phase",
        "seq",
        "emittedAtMs",
    ],
)
def test_decode_rejects_missing_required_fields(missing: str) -> None:
    record = json.loads(encode_progress_event(event()))
    record.pop(missing)

    assert decode_progress_event(json.dumps(record).encode()) is None


def test_decode_rejects_message_summary_drift() -> None:
    record = json.loads(encode_progress_event(event()))
    record["message"] = "something else"

    assert decode_progress_event(json.dumps(record).encode()) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("seq", -1),
        ("seq", "0"),
        ("seq", True),
        ("emittedAtMs", 0),
        ("droppedSinceSeq", -1),
        ("detail", ["not", "an", "object"]),
        ("toolCallId", 42),
    ],
)
def test_decode_rejects_mistyped_fields(field: str, value: object) -> None:
    record = json.loads(encode_progress_event(event()))
    record[field] = value

    assert decode_progress_event(json.dumps(record).encode()) is None


# --------------------------------------------------------------------------- #
# Size discipline: payload bound with detail truncation
# --------------------------------------------------------------------------- #


def test_oversized_detail_is_truncated_with_marker() -> None:
    original = event(detail={"blob": "x" * (PAYLOAD_LIMIT * 2)})

    payload = encode_progress_event(original)

    assert len(payload) <= PAYLOAD_LIMIT
    decoded = decode_progress_event(payload)
    assert decoded is not None
    assert decoded.detail == {"truncated": True}


def test_summary_bound_makes_payload_overflow_impossible_without_detail() -> None:
    """With summary capped at 200 chars and no detail, the payload always
    fits: the encoder never silently truncates the summary itself."""

    payload = encode_progress_event(event(summary="s" * SUMMARY_LIMIT))

    assert len(payload) <= PAYLOAD_LIMIT
    assert decode_progress_event(payload) is not None
