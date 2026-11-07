"""``message.history`` v2: tail/older/after over the same durable rows.

The v1 read (no ``mode``) stays byte-compatible: ``{ok, conversationId,
entries, nextCursor}`` with a version-1 cursor.  ``mode=tail|older|after``
returns a daemon-issued version-2 cursor bound to the conversation and the
authenticated participant; the adapter never constructs or repairs one.

These tests import the real daemon/inbox harness from the v1 file so both
surfaces are proven against the same store shape.
"""

from __future__ import annotations

import base64
import json

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from tests.daemon.delivery.message.test_message_history import (  # noqa: F401 - fixtures/helpers
    ACTOR,
    CONVERSATION,
    OTHER,
    OTHER_CONVERSATION,
    OWNER,
    READER,
    SESSION,
    _HistoryHarness,
    _deliver,
    _history,
    _ids,
    harness as harness,
)


def _decode(cursor: str) -> dict:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _forge_v2(**overrides: object) -> str:
    payload: dict[str, object] = {
        "v": 2,
        "conversationId": CONVERSATION,
        "participant": ACTOR,
        "createdAtMs": 2000,
        "messageId": "m-2",
        "kind": "before",
    }
    payload.update(overrides)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _deliver_series(harness: _HistoryHarness, count: int) -> None:
    for index in range(1, count + 1):
        _deliver(harness, f"m-{index}", created_at_ms=index * 1000)


def test_tail_returns_the_newest_page_oldest_first_then_pages_older(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 5)

    tail = _history(harness, mode="tail", limit=2)
    assert tail["mode"] == "tail"
    assert set(tail) >= {
        "ok",
        "conversationId",
        "mode",
        "entries",
        "headCursor",
        "nextOlderCursor",
    }
    assert _ids(tail) == ["m-4", "m-5"]
    assert tail["nextCursor"] is None
    head = _decode(tail["headCursor"])
    assert (head["messageId"], head["kind"], head["v"]) == ("m-5", "after", 2)
    older_cursor = tail["nextOlderCursor"]
    assert (_decode(older_cursor)["messageId"], _decode(older_cursor)["kind"]) == (
        "m-4",
        "before",
    )

    older = _history(harness, mode="older", cursor=older_cursor, limit=2)
    assert older["mode"] == "older"
    assert _ids(older) == ["m-2", "m-3"]
    assert _decode(older["headCursor"])["messageId"] == "m-5"
    assert _decode(older["nextOlderCursor"])["messageId"] == "m-2"

    oldest = _history(
        harness, mode="older", cursor=older["nextOlderCursor"], limit=2
    )
    assert _ids(oldest) == ["m-1"]
    assert oldest["nextOlderCursor"] is None
    assert _decode(oldest["headCursor"])["messageId"] == "m-5"


def test_tail_empty_page_and_limit_boundaries(
    harness: _HistoryHarness,
) -> None:
    empty = _history(harness, mode="tail")
    assert empty["mode"] == "tail"
    assert empty["entries"] == []
    assert empty["headCursor"] is None
    assert empty["nextCursor"] is None
    assert empty["nextOlderCursor"] is None

    for bad_limit in (0, 201, True, "5", 1.5):
        with pytest.raises(DaemonRequestError) as refused:
            _history(harness, mode="tail", limit=bad_limit)
        assert refused.value.code == "INVALID_ARGUMENT"

    for index in range(51):
        _deliver(harness, f"m-{index:02d}", created_at_ms=1000 + index)

    default_page = _history(harness, mode="tail")
    assert len(default_page["entries"]) == 50
    assert _ids(default_page) == [f"m-{index:02d}" for index in range(1, 51)]
    assert default_page["nextOlderCursor"] is not None

    full = _history(harness, mode="tail", limit=200)
    assert len(full["entries"]) == 51
    assert full["nextOlderCursor"] is None


@pytest.mark.parametrize("bad_mode", ["newest", "TAIL", "", 1, True, ["tail"]])
def test_unknown_mode_is_refused(
    harness: _HistoryHarness, bad_mode: object
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _history(harness, mode=bad_mode)
    assert refused.value.code == "INVALID_ARGUMENT"


def test_tail_refuses_a_cursor_and_older_after_require_one(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 2)
    head = _history(harness, mode="tail")["headCursor"]

    with pytest.raises(DaemonRequestError) as tail_cursor:
        _history(harness, mode="tail", cursor=head)
    assert tail_cursor.value.code == "INVALID_ARGUMENT"

    for mode in ("older", "after"):
        with pytest.raises(DaemonRequestError) as missing:
            _history(harness, mode=mode)
        assert missing.value.code == "INVALID_ARGUMENT"

    with pytest.raises(DaemonRequestError) as malformed:
        _history(harness, mode="older", cursor="not-a-cursor")
    assert malformed.value.code == "INVALID_ARGUMENT"


def test_v2_cursor_is_bound_to_conversation_and_participant(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 2)
    cursor = _history(harness, mode="tail", limit=1)["nextOlderCursor"]
    assert cursor is not None

    with pytest.raises(DaemonRequestError) as wrong_conversation:
        _history(
            harness,
            mode="older",
            cursor=cursor,
            conversation=OTHER_CONVERSATION,
        )
    assert wrong_conversation.value.code == "INVALID_ARGUMENT"

    with pytest.raises(DaemonRequestError) as wrong_participant:
        harness.app.handle(
            "message.history",
            {
                "from": f"user:{OWNER}",
                "onBehalfOf": READER,
                "conversationId": CONVERSATION,
                "mode": "older",
                "cursor": cursor,
            },
            _trusted_message_origin="ipc",
        )
    assert wrong_participant.value.code == "INVALID_ARGUMENT"

    for forged in (
        _forge_v2(kind="after"),
        _forge_v2(kind="sideways"),
        _forge_v2(v=3),
        _forge_v2(conversationId=OTHER_CONVERSATION),
        _forge_v2(participant=READER),
        _forge_v2(createdAtMs=0),
        _forge_v2(messageId=""),
        _forge_v2(extra="field"),
    ):
        with pytest.raises(DaemonRequestError) as refused:
            _history(harness, mode="older", cursor=forged)
        assert refused.value.code == "INVALID_ARGUMENT"


def test_after_reads_only_newer_rows_and_pages_forward(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 5)

    # A version-1 forward cursor remains accepted for after (transitional).
    v1_page = _history(harness, limit=1)
    assert _ids(v1_page) == ["m-1"]
    after = _history(harness, mode="after", cursor=v1_page["nextCursor"], limit=2)
    assert after["mode"] == "after"
    assert _ids(after) == ["m-2", "m-3"]
    assert after["nextOlderCursor"] is None
    assert _decode(after["headCursor"])["messageId"] == "m-5"
    assert _decode(after["headCursor"])["kind"] == "after"
    next_cursor = after["nextCursor"]
    assert (_decode(next_cursor)["messageId"], _decode(next_cursor)["kind"]) == (
        "m-3",
        "after",
    )

    rest = _history(harness, mode="after", cursor=next_cursor, limit=2)
    assert _ids(rest) == ["m-4", "m-5"]
    assert rest["nextCursor"] is None
    assert _decode(rest["headCursor"])["messageId"] == "m-5"

    at_head = _history(
        harness, mode="after", cursor=rest["headCursor"], limit=2
    )
    assert at_head["entries"] == []
    assert at_head["nextCursor"] is None
    assert at_head["nextOlderCursor"] is None
    assert _decode(at_head["headCursor"])["messageId"] == "m-5"

    before_cursor = _history(harness, mode="tail", limit=2)["nextOlderCursor"]
    with pytest.raises(DaemonRequestError) as wrong_kind:
        _history(harness, mode="after", cursor=before_cursor)
    assert wrong_kind.value.code == "INVALID_ARGUMENT"

    with pytest.raises(DaemonRequestError) as v1_backwards:
        _history(harness, mode="older", cursor=v1_page["nextCursor"])
    assert v1_backwards.value.code == "INVALID_ARGUMENT"


def test_default_read_keeps_the_v1_shape_and_cursor(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 3)

    page = _history(harness, limit=2)
    assert set(page) == {"ok", "conversationId", "entries", "nextCursor"}
    assert _ids(page) == ["m-1", "m-2"]
    decoded = _decode(page["nextCursor"])
    assert decoded["v"] == 1
    assert set(decoded) == {
        "v",
        "conversationId",
        "participant",
        "createdAtMs",
        "messageId",
    }
    assert decoded["participant"] == ACTOR

    rest = _history(harness, cursor=page["nextCursor"], limit=2)
    assert _ids(rest) == ["m-3"]
    assert rest["nextCursor"] is None


def test_all_modes_are_scoped_to_the_authenticated_participant(
    harness: _HistoryHarness,
) -> None:
    _deliver_series(harness, 3)  # READER -> ACTOR
    _deliver(
        harness,
        "m-unrelated",
        sender=OTHER,
        recipient=ACTOR,
        created_at_ms=4000,
    )

    actor_page = _history(harness, mode="tail", limit=10)
    assert "m-unrelated" in _ids(actor_page)

    reader_tail = _history(
        harness, actor=READER, session="session-reader", mode="tail", limit=2
    )
    assert _ids(reader_tail) == ["m-2", "m-3"]
    reader_older = _history(
        harness,
        actor=READER,
        session="session-reader",
        mode="older",
        cursor=reader_tail["nextOlderCursor"],
        limit=200,
    )
    assert _ids(reader_older) == ["m-1"]

    reader_v1 = _history(
        harness, actor=READER, session="session-reader", limit=1
    )
    reader_after = _history(
        harness,
        actor=READER,
        session="session-reader",
        mode="after",
        cursor=reader_v1["nextCursor"],
        limit=200,
    )
    # The reader's cursors never widen the read to another participant's rows.
    assert _ids(reader_after) == ["m-2", "m-3"]
    for page in (reader_tail, reader_older, reader_after):
        assert "m-unrelated" not in _ids(page)
