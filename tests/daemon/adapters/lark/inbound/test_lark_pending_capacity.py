from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import (
    LarkStateStore,
    PendingCommandCapacityError,
    PendingCommandResponse,
)
from hyprial.daemon.impl.adapters.lark.state import records as lark_state
from hyprial.daemon.impl.adapters.lark.contracts.messages import MAX_LARK_TEXT_CONTENT_BYTES
from hyprial.daemon.impl.adapters.lark.contracts.wire import encode_lark_text_content
def pending(message_id: str, *, text: str = "reply") -> PendingCommandResponse:
    return PendingCommandResponse(
        message_id=message_id,
        event_id=f"evt-{message_id}",
        kind="help",
        text=text,
        idempotency_key=f"{message_id}:command:help",
    )


def text_with_encoded_size(size: int) -> str:
    overhead = len(encode_lark_text_content("").encode("utf-8"))
    text = "x" * (size - overhead)
    assert len(encode_lark_text_content(text).encode("utf-8")) == size
    return text


def test_single_response_wire_limit_is_strict_before_write(
    tmp_path: Path,
) -> None:
    accepted_path = tmp_path / "accepted.sqlite3"
    accepted_store = LarkStateStore(accepted_path)
    accepted = pending(
        "om-limit-minus-one",
        text=text_with_encoded_size(MAX_LARK_TEXT_CONTENT_BYTES - 1),
    )
    assert accepted_store.record_pending_command_response(accepted) == accepted

    rejected_path = tmp_path / "rejected.sqlite3"
    rejected_store = LarkStateStore(rejected_path)
    rejected = pending(
        "om-at-limit",
        text=text_with_encoded_size(MAX_LARK_TEXT_CONTENT_BYTES),
    )
    with pytest.raises(ValueError, match="invalid pending Lark command response"):
        rejected_store.record_pending_command_response(rejected)
    assert rejected_store.pending_command_response(rejected.message_id) is None
    # Refusal happened before the write: a fresh view of the database holds
    # no pending record at all.
    assert LarkStateStore(rejected_path).pending_command_capacity().count == 0


def test_persisted_response_at_wire_limit_fails_closed_at_open(
    tmp_path: Path,
) -> None:
    """A stored body Lark would reject must fail the store at open.

    The row is injected with raw SQL — the write API refuses it — modelling
    corrupt or hand-edited durable state, the SQLite twin of the JSON-era
    legacy-file check.
    """

    state_path = tmp_path / "lark-state.sqlite3"
    store = LarkStateStore(state_path)
    response = pending(
        "om-oversize",
        text=text_with_encoded_size(MAX_LARK_TEXT_CONTENT_BYTES),
    )
    with sqlite3.connect(state_path) as raw_db:
        raw_db.execute(
            "INSERT INTO pending_command_responses(adapter, message_id,"
            " event_id, kind, text, idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (
                store.adapter,
                response.message_id,
                response.event_id,
                response.kind,
                response.text,
                response.idempotency_key,
            ),
        )

    with pytest.raises(ValueError, match="invalid pending Lark command response"):
        LarkStateStore(state_path)


def test_full_store_preserves_every_unresolved_record_and_returns_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lark_state, "MAX_PENDING_COMMAND_RESPONSES", 2)
    store = LarkStateStore(tmp_path / "lark-state.sqlite3")
    first = store.record_pending_command_response(pending("om-first", text="FIRST"))
    second = store.record_pending_command_response(pending("om-second", text="SECOND"))

    # An already-owned native message is immutable and retryable even while
    # the store is saturated.  A newly computed body must never replace it.
    assert store.record_pending_command_response(
        pending("om-first", text="RECALCULATED")
    ) == first

    before = store.pending_command_capacity().as_dict()
    with pytest.raises(PendingCommandCapacityError) as captured:
        store.record_pending_command_response(pending("om-third", text="THIRD"))
    # The refused write rolled back: a fresh view reports identical custody.
    assert (
        LarkStateStore(tmp_path / "lark-state.sqlite3")
        .pending_command_capacity()
        .as_dict()
        == before
    )
    assert store.pending_command_response("om-first") == first
    assert store.pending_command_response("om-second") == second
    assert store.pending_command_response("om-third") is None
    assert captured.value.capacity.as_dict() == {
        "status": "full",
        "count": 2,
        "bytes": store.pending_command_capacity().bytes,
        "maxCount": 2,
        "maxBytes": lark_state.MAX_PENDING_COMMAND_RESPONSE_BYTES,
    }


def test_aggregate_byte_charge_uses_larger_durable_map_or_sdk_sum(
    tmp_path: Path,
) -> None:
    responses = (
        pending("om-ascii", text="quoted: \\\""),
        pending("om-unicode", text="边界"),
    )
    store = LarkStateStore(tmp_path / "lark-state.sqlite3")
    for response in responses:
        store.record_pending_command_response(response)

    durable_map = {
        response.message_id: asdict(response) for response in responses
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
        for response in responses
    )
    assert store.pending_command_capacity().bytes == max(
        durable_bytes, sdk_bytes
    )


def test_existing_response_replays_immutably_at_exact_aggregate_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "lark-state.sqlite3"
    store = LarkStateStore(state_path)
    original = store.record_pending_command_response(
        pending("om-owned", text="ORIGINAL")
    )
    exact_aggregate_bytes = store.pending_command_capacity().bytes
    monkeypatch.setattr(
        lark_state,
        "MAX_PENDING_COMMAND_RESPONSE_BYTES",
        exact_aggregate_bytes,
    )

    # Exact aggregate capacity remains a valid stored map, but it is full:
    # an existing owner wins before admission checks and a new owner fails.
    assert LarkStateStore(state_path).pending_command_capacity().status == "full"
    before = store.pending_command_capacity().as_dict()
    assert store.record_pending_command_response(
        pending("om-owned", text="RECALCULATED")
    ) == original
    assert LarkStateStore(state_path).pending_command_capacity().as_dict() == before
    with pytest.raises(PendingCommandCapacityError):
        store.record_pending_command_response(pending("om-new"))
    assert LarkStateStore(state_path).pending_command_capacity().as_dict() == before
    assert store.pending_command_response("om-owned") == original
    assert store.pending_command_response("om-new") is None


def test_encoded_utf8_byte_budget_rejects_single_record_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = pending("om-multibyte", text='界\\"' * 20)
    exact_bytes = lark_state.pending_command_response_encoded_bytes(response)
    assert exact_bytes > len(response.text)
    monkeypatch.setattr(
        lark_state, "MAX_PENDING_COMMAND_RESPONSE_BYTES", exact_bytes - 1
    )
    state_path = tmp_path / "lark-state.sqlite3"
    store = LarkStateStore(state_path)

    with pytest.raises(PendingCommandCapacityError) as captured:
        store.record_pending_command_response(response)

    assert captured.value.capacity.count == 0
    assert captured.value.capacity.bytes == 0
    assert store.pending_command_response(response.message_id) is None
    # Refusal happens before the write: the database gained no record.
    assert LarkStateStore(state_path).pending_command_capacity().count == 0


def test_completion_releases_count_and_bytes_for_next_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lark_state, "MAX_PENDING_COMMAND_RESPONSES", 1)
    store = LarkStateStore(tmp_path / "lark-state.sqlite3")
    first = store.record_pending_command_response(pending("om-first", text="FIRST"))
    occupied = store.pending_command_capacity()
    assert occupied.count == 1
    assert occupied.bytes > 0

    with pytest.raises(PendingCommandCapacityError):
        store.record_pending_command_response(pending("om-blocked"))
    store.complete_command_response(first, delivered_event_id="evt-redelivery")
    assert store.pending_command_capacity().as_dict()["count"] == 0
    assert store.pending_command_capacity().as_dict()["bytes"] == 0
    assert store.record_pending_command_response(pending("om-next")).message_id == (
        "om-next"
    )


def test_capacity_prewrite_is_atomic_across_store_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lark_state, "MAX_PENDING_COMMAND_RESPONSES", 1)
    state_path = tmp_path / "lark-state.sqlite3"
    stores = (LarkStateStore(state_path), LarkStateStore(state_path))

    def attempt(index: int) -> str:
        try:
            return stores[index].record_pending_command_response(
                pending(f"om-{index}")
            ).message_id
        except PendingCommandCapacityError:
            return "full"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(attempt, range(2)))

    assert results.count("full") == 1
    restored = LarkStateStore(state_path)
    status = restored.pending_command_capacity()
    assert status.count == 1
    surviving = [
        message_id
        for message_id in ("om-0", "om-1")
        if restored.pending_command_response(message_id) is not None
    ]
    assert results.count(surviving[0]) == 1


def test_stored_pending_is_charged_but_corruption_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "lark-state.sqlite3"
    assert LarkStateStore(state_path).pending_command_capacity().as_dict()[
        "count"
    ] == 0

    store = LarkStateStore(state_path)
    response = store.record_pending_command_response(pending("om-stored", text="旧"))
    expected = lark_state.pending_command_response_encoded_bytes(response)
    assert LarkStateStore(state_path).pending_command_capacity().bytes == expected

    # Corrupt durable material (an empty required field, injected with raw
    # SQL because the write API refuses it) must fail the store at open.
    with sqlite3.connect(state_path) as raw_db:
        raw_db.execute(
            "INSERT INTO pending_command_responses(adapter, message_id,"
            " event_id, kind, text, idempotency_key)"
            " VALUES (?, 'om-corrupt', 'evt', 'help', '', 'key')",
            (store.adapter,),
        )
    with pytest.raises(ValueError, match="invalid pending Lark command response"):
        LarkStateStore(state_path)

    with sqlite3.connect(state_path) as raw_db:
        raw_db.execute(
            "DELETE FROM pending_command_responses WHERE message_id = 'om-corrupt'"
        )
    monkeypatch.setattr(lark_state, "MAX_PENDING_COMMAND_RESPONSES", 0)
    with pytest.raises(PendingCommandCapacityError):
        LarkStateStore(state_path)
