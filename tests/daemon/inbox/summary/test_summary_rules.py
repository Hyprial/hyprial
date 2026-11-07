"""The summary contract: check_summary, summary_from_text, body size rules.

Design: docs/design/design-send-links-2026-10-14.md §7; WP3 spec §2.1-§2.3,
with review blocking R1 (the over-budget ``data`` shape is exactly
``summaryBytes`` + ``maxBytes``).
"""

from __future__ import annotations

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox.contracts.summary import (
    body_bytes,
    body_text,
    check_summary,
    summary_from_text,
)
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.links.ports import SUMMARY_MAX_BYTES


def _message(payload: bytes) -> InboxMessage:
    return InboxMessage(
        message_id="m-1",
        conversation_id="c-1",
        sender="agent:op:node-1:reader",
        recipient="agent:op:node-1:driver",
        payload=payload,
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_the_budget_is_the_seam_constant() -> None:
    assert SUMMARY_MAX_BYTES == 512


def test_missing_summary_is_refused_only_when_required() -> None:
    assert check_summary(None, required=False) is None
    with pytest.raises(DaemonRequestError) as error:
        check_summary(None, required=True)
    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data == {
        "field": "summary",
        "reason": "missing",
        "maxBytes": 512,
    }


def test_non_string_summary_is_refused() -> None:
    with pytest.raises(DaemonRequestError) as error:
        check_summary(7, required=False)
    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data["reason"] == "not_string"
    assert error.value.data["field"] == "summary"
    assert error.value.data["maxBytes"] == 512


def test_blank_summary_is_refused() -> None:
    with pytest.raises(DaemonRequestError) as error:
        check_summary("   \n\t ", required=False)
    assert error.value.data["reason"] == "empty"


def test_lone_surrogate_summary_is_refused_as_invalid_utf8() -> None:
    with pytest.raises(DaemonRequestError) as error:
        check_summary("\ud800", required=False)
    assert error.value.data["reason"] == "invalid_utf8"


def test_exactly_512_bytes_passes_and_is_returned_verbatim() -> None:
    value = "a" * 512
    assert check_summary(value, required=False) == value


def test_over_budget_data_is_exactly_summary_bytes_and_max_bytes() -> None:
    value = "a" * 513
    with pytest.raises(DaemonRequestError) as error:
        check_summary(value, required=False)
    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data == {"summaryBytes": 513, "maxBytes": 512}


def test_the_budget_counts_bytes_not_characters() -> None:
    # "摘" is 3 UTF-8 bytes: 171 of them are 513 bytes and must be refused,
    # 170 are 510 and must pass.
    with pytest.raises(DaemonRequestError) as error:
        check_summary("摘" * 171, required=False)
    assert error.value.data == {"summaryBytes": 513, "maxBytes": 512}
    assert check_summary("摘" * 170, required=False) == "摘" * 170


def test_summary_from_text_keeps_short_text_verbatim() -> None:
    text = "一行人写的原文"
    assert summary_from_text(text) == text


def test_summary_from_text_marks_a_deterministic_prefix() -> None:
    text = "摘" * 400  # 1200 bytes
    marked = summary_from_text(text)
    assert marked.encode("utf-8").__len__() <= 512
    assert marked.endswith("字节，用 read_body 读取]")
    assert "[全文 1200 字节" in marked
    assert marked == summary_from_text(text)
    # The prefix never splits a multi-byte character.
    marked.encode("utf-8").decode("utf-8")


def test_body_text_json_object_then_raw_bytes() -> None:
    assert body_text(_message(b'{"message":"hi\\nthere"}')) == "hi\nthere"
    assert body_text(_message(b'["not", "an", "object"]')) == '["not", "an", "object"]'
    assert body_text(_message(b'{"message": 7}')) == '{"message": 7}'
    assert body_text(_message("多字节换行\n".encode())) == "多字节换行\n"
    assert body_bytes(_message("多字节换行\n".encode())) == len(
        "多字节换行\n".encode()
    )
