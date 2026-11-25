"""Protocol 3 frame codecs: strict validation, whole-frame rejection."""

from __future__ import annotations

import base64
import json

from pycrdt import Doc, Text
import pytest

from hyprial.daemon.impl.orgfs.sync import (
    FrameError,
    ReplyEntry,
    SyncFailure,
    SyncReply,
    SyncRequest,
    UpdateFrame,
    covers,
    decode_reply,
    decode_request,
    decode_update,
    decode_vector,
    encode_error,
    encode_reply,
    encode_request,
    encode_update,
    encode_vector,
)
from hyprial.daemon.impl.orgfs.sync.interface import (
    ORGFS_PAGE_BYTES,
    SYNC_ENVELOPE_BYTES,
)


def _update(text: str = "hello", client: int = 7) -> tuple[bytes, bytes]:
    doc = Doc(client_id=client)
    body = doc.get("body", type=Text)
    body += text
    return doc.get_update(), doc.get_state()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _request(**overrides) -> SyncRequest:
    values = dict(
        space_id="space-1",
        round="round-1",
        requester_node="node-b",
        requester_author="user:b",
        vectors=(("meta", b"\x00"), ("tree", b"\x00"), ("doc-1", b"\x00")),
        max_bytes=ORGFS_PAGE_BYTES,
    )
    values.update(overrides)
    return SyncRequest(**values)


def test_state_vectors_compare_by_decoded_clock_not_bytes() -> None:
    forward = encode_vector({1: 5, 9: 2})
    reordered = b"\x02\x09\x02\x01\x05"

    assert decode_vector(reordered) == {1: 5, 9: 2}
    assert reordered != forward
    assert covers(reordered, forward) and covers(forward, reordered)
    assert covers(encode_vector({1: 6, 9: 2}), forward)
    assert not covers(encode_vector({1: 5}), forward)
    assert covers(b"\x00", b"\x00")
    with pytest.raises(FrameError):
        decode_vector(b"\x02\x01")


def test_state_vector_codec_matches_pycrdt() -> None:
    doc = Doc(client_id=3)
    body = doc.get("body", type=Text)
    body += "x" * 300
    assert decode_vector(doc.get_state()) == {3: 300}
    assert encode_vector({3: 300}) == doc.get_state()


def test_sync_request_round_trips() -> None:
    request = _request()

    assert decode_request(encode_request(request), space_id="space-1") == request


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(schemaVersion=1),
        lambda v: v.update(type="update"),
        lambda v: v.update(spaceId="space-2"),
        lambda v: v.update(round=""),
        lambda v: v.update(requester={"node": "node-b"}),
        lambda v: v.update(requester={"node": 3, "author": "user:b"}),
        lambda v: v["vectors"].pop("tree"),
        lambda v: v["vectors"].update({"doc-2": "%%%"}),
        lambda v: v["vectors"].update({"doc-2": _b64(b"\x02\x01")}),
        lambda v: v["vectors"].update({f"doc-{i}": _b64(b"\x00") for i in range(129)}),
        lambda v: v.update(maxBytes="big"),
        lambda v: v.update(maxBytes=True),
        lambda v: v.update(extra=1),
    ],
)
def test_sync_request_rejects_any_invalid_field(mutate) -> None:
    value = json.loads(encode_request(_request()))
    mutate(value)

    with pytest.raises(FrameError):
        decode_request(json.dumps(value).encode(), space_id="space-1")


def test_sync_request_rejects_non_json_and_oversized_payloads() -> None:
    with pytest.raises(FrameError):
        decode_request(b"\xff", space_id="space-1")
    with pytest.raises(FrameError):
        decode_request(None, space_id="space-1")
    with pytest.raises(FrameError):
        decode_request(b" " * (ORGFS_PAGE_BYTES + 1), space_id="space-1")


def _reply(**overrides) -> SyncReply:
    update, vector = _update()
    values = dict(
        space_id="space-1",
        round="round-1",
        supplier="node-a",
        entries=(
            ReplyEntry("meta", vector, update=update),
            ReplyEntry("doc-1", vector, blob="a" * 64, size=12),
        ),
        meta_vector=vector,
        tree_vector=b"\x00",
        omitted=("tree",),
    )
    values.update(overrides)
    return SyncReply(**values)


def _decode(payload: bytes):
    return decode_reply(
        payload,
        space_id="space-1",
        round="round-1",
        supplier="node-a",
        requested=frozenset({"meta", "tree", "doc-1"}),
    )


def test_sync_reply_round_trips() -> None:
    reply = _reply()

    assert _decode(encode_reply(reply)) == reply


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(round="round-2"),
        lambda v: v.update(supplier="node-z"),
        lambda v: v.update(spaceId="space-2"),
        lambda v: v["updates"].append(dict(v["updates"][0])),
        lambda v: v["updates"][0].update(docId="doc-unrequested"),
        lambda v: v["updates"][0].update(update=_b64(b"\xff\x01garbage")),
        lambda v: v["updates"][0].update(update=""),
        lambda v: v["updates"][0].update(vector="!!"),
        lambda v: v["updates"][1].update(blob="not-a-digest"),
        lambda v: v["updates"][1].update(size=-1),
        lambda v: v["updates"][1].update(update=v["updates"][0]["update"]),
        lambda v: v["updates"][1].pop("blob"),
        lambda v: v["vectors"].pop("meta"),
        lambda v: v["vectors"].update({"doc-1": v["vectors"]["tree"]}),
        lambda v: v.update(omitted=["meta"]),
        lambda v: v.update(omitted=["doc-unrequested"]),
        lambda v: v.update(omitted="tree"),
    ],
)
def test_sync_reply_with_one_bad_part_is_rejected_whole(mutate) -> None:
    value = json.loads(encode_reply(_reply()))
    mutate(value)

    with pytest.raises(FrameError):
        _decode(json.dumps(value).encode())


def test_sync_error_reply_is_a_typed_failure() -> None:
    payload = encode_error(
        space_id="space-1", round="round-1", supplier="node-a",
        code="not-a-member", message="requester is not a member",
    )

    assert _decode(payload) == SyncFailure("not-a-member", "requester is not a member")


def test_update_frame_round_trips_and_drops_oversized_bodies() -> None:
    update, vector = _update()
    frame = UpdateFrame("space-1", "node-a", "doc-1", vector, update)

    assert decode_update(encode_update(frame), space_id="space-1") == frame
    big = UpdateFrame("space-1", "node-a", "doc-1", vector, b"\x00" * SYNC_ENVELOPE_BYTES)
    encoded = encode_update(big)
    assert len(encoded) < 1024
    assert decode_update(encoded, space_id="space-1") == UpdateFrame(
        "space-1", "node-a", "doc-1", vector, None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(schemaVersion=1),
        lambda v: v.update(type="sync"),
        lambda v: v.update(spaceId="space-2"),
        lambda v: v.update(node=""),
        lambda v: v.update(docId=""),
        lambda v: v.update(docId="doc-orgdir-v9-junk"),
        lambda v: v.update(update=_b64(b"\xff\x01garbage")),
        lambda v: v.update(vector=5),
        lambda v: v.pop("vector"),
    ],
)
def test_update_frame_rejects_invalid_fields(mutate) -> None:
    update, vector = _update()
    value = json.loads(encode_update(UpdateFrame("space-1", "node-a", "doc-1", vector, update)))
    mutate(value)

    with pytest.raises(FrameError):
        decode_update(json.dumps(value).encode(), space_id="space-1")


def test_update_frame_rejects_payload_over_the_envelope_bound() -> None:
    with pytest.raises(FrameError):
        decode_update(b" " * (SYNC_ENVELOPE_BYTES + 1), space_id="space-1")


def test_outcome_kind_recognises_the_admission_types_only() -> None:
    from types import SimpleNamespace

    from hyprial.daemon.impl.orgfs.membership import Accepted, Pending, Rejected
    from hyprial.daemon.impl.orgfs.sync.interface import outcome_kind

    assert outcome_kind(Accepted()) == "accepted"
    assert outcome_kind(Pending("dependency")) == "pending"
    assert outcome_kind(Pending("unknown-doc")) == "pending"
    assert outcome_kind(Rejected("not-owner", {})) == "rejected"
    for stray in (None, SimpleNamespace(code="x"), SimpleNamespace(reason="dependency"), ValueError("x")):
        with pytest.raises(TypeError):
            outcome_kind(stray)
