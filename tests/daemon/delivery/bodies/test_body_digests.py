"""Pure delivery faces: grants, digests, residue, retention clamp, wake gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

import pytest

from hyprial.daemon.impl.delivery import (
    BODY_RETENTION_PARAM,
    DEFAULT_BODY_RETENTION_MS,
    INVALID_TEXT_DIGEST,
    MAX_BODY_RETENTION_MS,
    MIN_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
    BodyRef,
    ObserveWakeGate,
    classify_send_body,
    clamp_body_retention_ms,
    payload_sha256,
    residue_payload,
    text_sha256,
)
from hyprial.kernel import MessageClass

SHA = "a" * 64


@dataclass(frozen=True)
class _Envelope:
    message_id: str = "m-1"
    conversation_id: str = "c-1"
    sender: str = "agent:op:n:a"
    recipient: str = "user:op"
    payload: bytes = b"{}"
    intent: str = "request"
    idempotency_key: str | None = None
    created_at_ms: int = 1
    thread_id: str | None = None
    conversation_cell_id: str | None = None
    message_class: MessageClass | None = None

    def with_payload(self, payload: bytes) -> _Envelope:
        return replace(self, payload=payload)


def test_default_retention_is_seven_days_and_the_param_key_is_single() -> None:
    assert DEFAULT_BODY_RETENTION_MS == 604_800_000
    assert BODY_RETENTION_PARAM == "delivery.bodyRetentionMs"
    assert MIN_BODY_RETENTION_MS == 3_600_000
    assert MAX_BODY_RETENTION_MS == 365 * 86_400_000


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_BODY_RETENTION_MS),
        (True, DEFAULT_BODY_RETENTION_MS),
        ("86400000", DEFAULT_BODY_RETENTION_MS),
        (1.5, DEFAULT_BODY_RETENTION_MS),
        (1, MIN_BODY_RETENTION_MS),
        (MIN_BODY_RETENTION_MS + 1, MIN_BODY_RETENTION_MS + 1),
        (10**15, MAX_BODY_RETENTION_MS),
    ],
)
def test_retention_is_clamped_to_one_hour_through_one_year(value, expected) -> None:
    assert clamp_body_retention_ms(value) == expected


def test_a_grant_validates_its_key() -> None:
    grant = BodyGrant(SOURCE_INBOX, 3, SHA, "rec-1")
    assert grant.retention_ms is None
    assert BodyGrant(SOURCE_SENDER_COPY, 1, SHA, "r", retention_ms=5).retention_ms == 5
    for bad in (
        {"source": "dlq"},
        {"position": 0},
        {"position": True},
        {"payload_sha256": "A" * 64},
        {"payload_sha256": "a" * 63},
        {"ref": ""},
        {"ref": "x" * 1025},
        {"retention_ms": "1"},
    ):
        fields = {
            "source": SOURCE_INBOX, "position": 1, "payload_sha256": SHA,
            "ref": "r", **bad,
        }
        with pytest.raises(ValueError):
            BodyGrant(**fields)


def test_a_body_ref_names_the_row_and_the_record() -> None:
    ref = BodyRef(SOURCE_INBOX, 7, "m-7", SHA, "rec-7")
    assert (ref.source, ref.position, ref.message_id, ref.ref) == (
        SOURCE_INBOX, 7, "m-7", "rec-7",
    )


def test_payload_digest_is_the_sha256_of_the_whole_payload() -> None:
    payload = b'{"message":"hi"}'
    assert payload_sha256(payload) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        (b'{"message":"hello"}', "hello"),
        (b'{"message":""}', ""),
        (b'{"message":1}', None),
        (b"[]", None),
        (b"not json", None),
        (b"\xff\xfe", None),
    ],
)
def test_classify_send_body_matches_the_lookup_rules(payload, text) -> None:
    assert classify_send_body(payload) == text


def test_text_digest_marks_invalid_records_and_survives_surrogates() -> None:
    assert text_sha256(b'{"message":"hi"}') == hashlib.sha256(b"hi").hexdigest()
    assert text_sha256(b'{"other":1}') == INVALID_TEXT_DIGEST
    lone = text_sha256(b'{"message":"\\ud800"}')
    assert lone != INVALID_TEXT_DIGEST and len(lone) == 64


def test_residue_keeps_only_origin_and_reply_to() -> None:
    payload = json.dumps({
        "message": "secret", "origin": {"kind": "agent"}, "replyTo": "m-0",
        "attachments": ["x"],
    }).encode()
    assert json.loads(residue_payload(payload)) == {
        "origin": {"kind": "agent"}, "replyTo": "m-0",
    }
    assert json.loads(residue_payload(b'{"message":"only"}')) == {}
    assert json.loads(residue_payload(b"garbage")) == {}


def test_observe_gate_logs_system_wakes_and_suppresses_nothing() -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    classes = {"m-sys": MessageClass.SYSTEM, "m-int": MessageClass.INTERACTION}
    gate = ObserveWakeGate(
        lambda message: classes[message.message_id],
        log=lambda level, event, **fields: events.append((level, event, fields)),
    )

    assert gate.mode == "observe"
    assert gate.observe(_Envelope(message_id="m-int"), path="inbox.receive") is True
    assert events == []
    assert gate.observe(_Envelope(message_id="m-sys"), path="user.workflow") is True
    assert events == [(
        "info",
        "delivery.wake.would_suppress",
        {
            "messageId": "m-sys", "recipient": "user:op", "path": "user.workflow",
            "messageClass": "system", "mode": "observe",
        },
    )]


def test_observe_gate_never_fails_the_wake_path() -> None:
    def broken_log(*_args, **_fields):
        raise RuntimeError("log down")

    def broken_class(_message):
        raise RuntimeError("class down")

    gate = ObserveWakeGate(lambda _m: MessageClass.SYSTEM, log=broken_log)
    assert gate.observe(_Envelope(), path="p") is True
    assert ObserveWakeGate(broken_class).observe(_Envelope(), path="p") is True
