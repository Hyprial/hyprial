"""The summary contract: the only sender-written text a carrier may inject.

Design: docs/design/design-send-links-2026-10-14.md §7.  A message is split
into a required ``summary`` (at most ``SUMMARY_MAX_BYTES`` UTF-8 bytes) and a
body (the payload, read on demand by message id).  This module holds the pure
rules: validation at the send boundary, the deterministic derivation used by
components that relay a person's words, and the one body-size reading shared
by ``message.pending.list`` and ``message.read_body``.
"""

from __future__ import annotations

import json
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.links.ports import SUMMARY_MAX_BYTES

__all__ = ["body_bytes", "body_text", "check_summary", "summary_from_text"]

_MESSAGE = (
    "summary is the only part of a message that enters the recipient's "
    f"context and must be at most {SUMMARY_MAX_BYTES} bytes of UTF-8"
)
_MARKER = "…[全文 {size} 字节，用 read_body 读取]"


def _refuse(reason: str, **extra: Any) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.SUMMARY_REQUIRED,
        _MESSAGE,
        {"field": "summary", "reason": reason, "maxBytes": SUMMARY_MAX_BYTES, **extra},
    )


def check_summary(value: object, *, required: bool) -> str | None:
    """Validate a caller-supplied summary, returning it unchanged.

    ``required`` is False during WP3-A (the hard cut to required=True is
    WP3-B).  Over budget is reported with exactly ``summaryBytes`` and
    ``maxBytes``; the other refusals keep the diagnostic ``field``/``reason``.
    """

    if value is None:
        if not required:
            return None
        raise _refuse("missing")
    if not isinstance(value, str):
        raise _refuse("not_string")
    if value.strip() == "":
        raise _refuse("empty")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _refuse("invalid_utf8") from error
    if len(encoded) > SUMMARY_MAX_BYTES:
        raise DaemonRequestError(
            ipc_errors.SUMMARY_REQUIRED,
            _MESSAGE,
            {"summaryBytes": len(encoded), "maxBytes": SUMMARY_MAX_BYTES},
        )
    return value


def summary_from_text(text: str) -> str:
    """Derive a summary from text a person wrote.

    Short text is its own summary.  Longer text keeps the longest prefix that
    fits on a UTF-8 character boundary beside an explicit marker naming the
    full byte count, so the reader knows the body was not truncated.  The
    result is deterministic: a retried Lark inbound must reproduce the same
    receipt digest.
    """

    size = len(text.encode("utf-8"))
    if size <= SUMMARY_MAX_BYTES:
        return text
    marker = _MARKER.format(size=size)
    budget = SUMMARY_MAX_BYTES - len(marker.encode("utf-8"))
    used = 0
    end = 0
    for index, character in enumerate(text):
        width = len(character.encode("utf-8"))
        if used + width > budget:
            break
        used += width
        end = index + 1
    return text[:end] + marker


def body_text(message: InboxMessage) -> str:
    """The message body: the payload's ``message`` string, else decoded bytes."""

    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        text = body.get("message")
        if isinstance(text, str):
            return text
    return message.payload.decode("utf-8", errors="replace")


def body_bytes(message: InboxMessage) -> int:
    """UTF-8 byte length of :func:`body_text`; the one size all readers share."""

    return len(body_text(message).encode("utf-8"))
