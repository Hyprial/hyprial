"""Origin provenance must be identical in batch JSON and model-facing text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox.batch.contract import (
    BatchBounds,
    BatchItem,
    BatchSnapshot,
)
from hyprial.daemon.impl.inbox.batch.service import _render_response


def _snapshot(*origins: dict[str, object]) -> BatchSnapshot:
    items = tuple(
        BatchItem(
            message_id=f"message-{index}",
            delivery_id=f"message-{index}",
            conversation_id=f"conversation-{index}",
            sender="adapter:lark:test",
            recipient="agent:owner:machine:reader",
            intent="request",
            kind="message",
            created_at_ms=index,
            summary=f"summary-{index}",
            body=("body-" + str(index)) * 300,
            body_bytes=len(("body-" + str(index)) * 300),
            origin=origin,
            cls="human",
            expected_action="reply",
            payload=json.dumps({"origin": origin}).encode(),
        )
        for index, origin in enumerate(origins, start=1)
    )
    return BatchSnapshot(
        actor="agent:owner:machine:reader",
        cursor="cursor-with-origin",
        items=items,
        bounds=BatchBounds(inbox_high=len(items), notice_high=0, capture_ordinal=len(items)),
        total_body_bytes=sum(item.body_bytes for item in items),
    )


_CASES = (
    (
        {"standing": "verified", "owner": "user:owner"},
        "origin: owner=user:owner, standing=verified",
    ),
    (
        {"standing": "observed"},
        "origin: standing=observed (NOT verified)",
    ),
    (
        {
            "sender": {
                "displayName": "Recorded Owner",
                "standing": "verified",
                "owner": "user:owner",
            }
        },
        "origin: sender=Recorded Owner (owner user:owner, verified)",
    ),
    (
        {"sender": {"displayName": "Claimed Owner", "standing": "observed"}},
        "origin: sender=Claimed Owner (NOT verified: display name only, no owner)",
    ),
    (
        {"sender": {"platformId": "ou_sender", "standing": "unresolved", "lookupFailed": True}},
        "origin: sender=ou_sender (NOT verified: identity lookup failed)",
    ),
    (
        {"sender": {"platformId": "ou_sender", "standing": "unresolved"}},
        "origin: sender=ou_sender (NOT verified: sender unresolved)",
    ),
    (
        {"sender": {"platformId": "ou_sender", "standing": "unresolved", "lookupFailed": "true"}},
        "origin: sender=ou_sender (NOT verified: sender unresolved)",
    ),
    ({"via": "adapter"}, "origin: via=adapter (NOT verified)"),
    ({}, "origin: (unrecorded; NOT verified)"),
)


@pytest.mark.parametrize(("origin", "expected_line"), _CASES)
def test_inline_text_uses_the_same_enriched_origin_as_wire_items(
    origin: dict[str, object], expected_line: str
) -> None:
    response = _render_response(
        _snapshot(origin),
        limit_bytes=1_000_000,
        actor_home=None,
        epoch="epoch-1",
    )

    assert response["delivery"]["mode"] == "inline"
    assert response["items"][0]["origin"] == origin
    assert expected_line in response["delivery"]["text"]


@pytest.mark.parametrize(("origin", "expected_line"), _CASES)
def test_spill_file_uses_the_same_enriched_origin_as_wire_items(
    tmp_path: Path, origin: dict[str, object], expected_line: str
) -> None:
    actor_home = tmp_path / "actor-home"
    actor_home.mkdir(mode=0o700)

    response = _render_response(
        _snapshot(origin),
        limit_bytes=1,
        actor_home=actor_home,
        epoch="epoch-1",
    )

    assert response["delivery"]["mode"] == "spill"
    assert response["items"][0]["origin"] == origin
    spill_text = Path(response["delivery"]["spillPath"]).read_text()
    assert expected_line in spill_text
    assert expected_line in response["delivery"]["text"]


def test_each_text_block_and_wire_item_are_derived_from_the_same_origin() -> None:
    origins = tuple(case[0] for case in _CASES)
    response = _render_response(
        _snapshot(*origins),
        limit_bytes=1_000_000,
        actor_home=None,
        epoch="epoch-1",
    )
    text = response["delivery"]["text"]

    for index, (origin, expected_line) in enumerate(_CASES, start=1):
        start = text.index(f"[{index}/{len(_CASES)} ")
        end = text.index("--- END UNTRUSTED MESSAGE DATA ---", start)
        block = text[start:end]
        assert response["items"][index - 1]["origin"] == origin
        assert expected_line in block
