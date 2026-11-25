"""The summary seams: frame carriage and the byte budget.

Decision: T0 zero-behaviour move 2026-11-19 (send-link/thread removal plan).
``test_frame_carries_summary_additively`` and
``test_summary_budget_is_bytes_not_characters`` (with the ``_message`` helper)
moved verbatim from ``tests/daemon/links/test_wp0_seams.py``; only the
``SUMMARY_MAX_BYTES`` import changed source, to the summary contract's own
module (``inbox/contracts/summary.py``).
"""

from __future__ import annotations

import json

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.contracts.summary import SUMMARY_MAX_BYTES
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)


def _message(**overrides: object) -> InboxMessage:
    base = dict(
        message_id="m1",
        conversation_id="c1",
        sender="agent:o:m:a",
        recipient="agent:o:m:b",
        payload=b"body",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    base.update(overrides)
    return InboxMessage(**base)  # type: ignore[arg-type]


def test_frame_carries_summary_additively() -> None:
    plain = _message()
    assert plain.summary is None
    assert "summary" not in json.loads(encode_delivery_frame(plain))
    assert decode_delivery_frame(encode_delivery_frame(plain)).summary is None

    stamped = _message(summary="please review the WP0 seams")
    frame = json.loads(encode_delivery_frame(stamped))
    assert frame["summary"] == "please review the WP0 seams"
    decoded = decode_delivery_frame(encode_delivery_frame(stamped))
    assert decoded.summary == stamped.summary
    assert decoded.payload == b"body"


def test_summary_budget_is_bytes_not_characters() -> None:
    assert SUMMARY_MAX_BYTES == 512
    # 171 CJK characters are 513 UTF-8 bytes: the budget must be measured
    # in bytes, so this string is over it although it is short in characters.
    assert len(("摘" * 171).encode("utf-8")) > SUMMARY_MAX_BYTES
