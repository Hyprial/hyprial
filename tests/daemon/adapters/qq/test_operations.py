"""Durable operation store: msg_seq determinism, source-A limit, provenance codec."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AudienceSnapshot,
    ChatRef,
    HydratedEvent,
    PrincipalRef,
)
from hyprial.daemon.impl.adapters.qq.components import QQOperationStore
from hyprial.daemon.impl.adapters.qq.errors import QQMsgSeqLimit
from hyprial.daemon.impl.adapters.qq.codec import QQEventCodec

from .support import APP_ID, BOT_ID, qq_instance

T0 = 1_000_000


def _store(path: Path) -> QQOperationStore:
    return QQOperationStore(path)


def test_deterministic_msg_seq_is_stable_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite"
    store = _store(path)
    store.record_inbound(instance_id="qq-1", generation=2, msg_id="m-1", received_at_ms=T0)
    first = store.claim_msg_seq(
        instance_id="qq-1", generation=2, msg_id="m-1", operation_id="op-1", now_ms=T0
    )
    second = store.claim_msg_seq(
        instance_id="qq-1", generation=2, msg_id="m-1", operation_id="op-1", now_ms=T0 + 5
    )
    assert first == second
    other = store.claim_msg_seq(
        instance_id="qq-1", generation=2, msg_id="m-1", operation_id="op-2", now_ms=T0 + 6
    )
    store.close()
    fresh = _store(path)
    assert (
        fresh.claim_msg_seq(
            instance_id="qq-1", generation=2, msg_id="m-1", operation_id="op-1", now_ms=T0 + 7
        )
        == first
    )
    assert 0 <= first <= 65535
    assert other != first, "probe guarantees unique seq per operation within a msg_id"
    rows = fresh.attempts(instance_id="qq-1", generation=2, operation_id="op-1")
    assert rows == ()
    fresh.close()


def test_msg_seq_source_a_limit_is_four_distinct_replies_per_hour(tmp_path: Path) -> None:
    store = _store(tmp_path / "ops.sqlite")
    store.record_inbound(instance_id="qq-1", generation=2, msg_id="m-1", received_at_ms=T0)
    for index in range(4):
        seq = store.claim_msg_seq(
            instance_id="qq-1",
            generation=2,
            msg_id="m-1",
            operation_id=f"op-{index}",
            now_ms=T0 + index,
        )
        assert 0 <= seq <= 65535
    with pytest.raises(QQMsgSeqLimit):
        store.claim_msg_seq(
            instance_id="qq-1", generation=2, msg_id="m-1", operation_id="op-4", now_ms=T0 + 10
        )
    store.claim_msg_seq(
        instance_id="qq-1",
        generation=2,
        msg_id="m-1",
        operation_id="op-1",
        now_ms=T0 + 11,
    )  # reusing an already-claimed operation stays within the limit
    fresh = store.claim_msg_seq(
        instance_id="qq-1",
        generation=2,
        msg_id="m-1",
        operation_id="op-4",
        now_ms=T0 + 3_600_100,
    )
    assert 0 <= fresh <= 65535, "window slides: an hour later the limit resets"
    store.claim_msg_seq(
        instance_id="qq-1", generation=2, msg_id="m-2", operation_id="op-4", now_ms=T0 + 12
    )  # limit is per msg_id
    store.close()


def test_concurrent_claims_stay_transactional_and_unique(tmp_path: Path) -> None:
    store = _store(tmp_path / "ops.sqlite")
    try:
        store.record_inbound(instance_id="qq-1", generation=2, msg_id="m-1", received_at_ms=T0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            same = list(
                pool.map(
                    lambda _: store.claim_msg_seq(
                        instance_id="qq-1",
                        generation=2,
                        msg_id="m-1",
                        operation_id="op-same",
                        now_ms=T0,
                    ),
                    range(8),
                )
            )
        assert len(set(same)) == 1
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(
                    store.claim_msg_seq,
                    instance_id="qq-1",
                    generation=2,
                    msg_id="m-2",
                    operation_id=f"op-{index}",
                    now_ms=T0,
                )
                for index in range(5)
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except QQMsgSeqLimit:
                    outcomes.append("limited")
        assert outcomes.count("limited") == 1, "exactly the fifth distinct operation is barred"
        assert len(set(outcomes)) - (1 if "limited" in outcomes else 0) == 4
    finally:
        store.close()


def test_inbound_status_tracks_known_unknown_stale(tmp_path: Path) -> None:
    store = _store(tmp_path / "ops.sqlite")
    assert (
        store.inbound_status(
            instance_id="qq-1", generation=2, msg_id="m-1", now_ms=T0
        )
        == "unknown"
    )
    store.record_inbound(instance_id="qq-1", generation=2, msg_id="m-1", received_at_ms=T0)
    assert (
        store.inbound_status(
            instance_id="qq-1", generation=2, msg_id="m-1", now_ms=T0 + 1000
        )
        == "known"
    )
    assert (
        store.inbound_status(
            instance_id="qq-1", generation=2, msg_id="m-1", now_ms=T0 + 3_600_001
        )
        == "stale"
    )
    assert (
        store.inbound_status(
            instance_id="qq-1", generation=3, msg_id="m-1", now_ms=T0 + 1000
        )
        == "unknown"
    )
    store.close()


def test_attempt_records_persist_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite"
    store = _store(path)
    store.record_attempt(
        instance_id="qq-1",
        generation=2,
        operation_id="op-1",
        msg_id="m-1",
        msg_seq=7,
        attempted_at_ms=T0,
        result="unknown",
    )
    store.record_attempt(
        instance_id="qq-1",
        generation=2,
        operation_id="op-1",
        msg_id="m-1",
        msg_seq=7,
        attempted_at_ms=T0 + 9,
        result="accepted",
    )
    store.close()
    fresh = _store(path)
    rows = fresh.attempts(instance_id="qq-1", generation=2, operation_id="op-1")
    assert [row["result"] for row in rows] == ["unknown", "accepted"]
    assert rows[0]["msg_seq"] == 7
    fresh.close()


def test_media_state_round_trips_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite"
    store = _store(path)
    assert (
        store.load_media_state(instance_id="qq-1", generation=2, operation_id="op-m")
        is None
    )
    store.save_media_state(
        instance_id="qq-1",
        generation=2,
        operation_id="op-m",
        state={"upload_id": "u-1", "block_size": 8, "done_parts": [1], "file_info": None},
    )
    store.save_media_state(
        instance_id="qq-1",
        generation=2,
        operation_id="op-m",
        state={"upload_id": "u-1", "block_size": 8, "done_parts": [1, 2], "file_info": None},
    )
    store.close()
    fresh = _store(path)
    state = fresh.load_media_state(instance_id="qq-1", generation=2, operation_id="op-m")
    assert state is not None
    assert state["done_parts"] == [1, 2]
    fresh.close()


def _hydrated(frame: dict) -> HydratedEvent:
    from hyprial.daemon.impl.adapters.contracts import (
        IngressObservation,
        NativeAlias,
        PlatformEventEnvelope,
        frozen_json_digest_v1,
        freeze_json_v1,
    )

    frozen = freeze_json_v1(frame)
    alias = NativeAlias("qq-1", 2, APP_ID, "qq-event", "create", "event", "event-1", "qq-official-v1")
    observation = IngressObservation(
        f"observation:qq:qq-1:{frame.get('s')}",
        (alias,),
        "live",
        "raw-source-ref:qq:qq-1:event-1",
        frozen_json_digest_v1(frozen),
        0,
    )
    return HydratedEvent(PlatformEventEnvelope(observation, frozen), frozen)


def test_codec_decodes_group_provenance_with_msg_seq_and_media() -> None:
    codec = QQEventCodec(qq_instance(), APP_ID, BOT_ID)
    event = _hydrated(
        {
            "op": 0,
            "s": 2,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "id": "event-1",
            "d": {
                "id": "message-1",
                "event_id": "event-1",
                "group_openid": "group-1",
                "author": {"id": "user-1"},
                "content": " <@!bot-1> hello ",
                "msg_seq": 7,
                "message_reference": {"message_id": "message-0"},
                "attachments": [
                    {
                        "id": "media-1",
                        "filename": "a.png",
                        "content_type": "image/png",
                        "size": 5,
                    }
                ],
            },
        }
    )
    normalized, platform = codec.decode(event)
    assert platform.schema == "hyprial.chat.platform.qq/v1"
    assert platform.event_id == "event-1"
    assert platform.message_id == "message-1"
    assert platform.reply_id == "message-0"
    assert platform.msg_seq == 7
    assert platform.media_refs == ("media-1",)
    assert platform.scene == "group"
    assert platform.group_open_id == "group-1"
    assert normalized.attachments[0].handle_id == "qq:qq-1:media-1"
    assert normalized.reply_to is not None
    assert normalized.content[0].text == "hello"
    assert codec.normalize(event).provenance.native_sender_id == "user-1"


def test_codec_decodes_c2c_events() -> None:
    codec = QQEventCodec(qq_instance(), APP_ID, BOT_ID)
    event = _hydrated(
        {
            "op": 0,
            "s": 3,
            "t": "C2C_MESSAGE_CREATE",
            "id": "event-2",
            "d": {
                "id": "message-2",
                "event_id": "event-2",
                "author": {"user_openid": "user-9"},
                "content": "hi",
            },
        }
    )
    normalized, platform = codec.decode(event)
    assert platform.scene == "c2c"
    assert platform.group_open_id is None
    assert platform.author_id == "user-9"
    assert normalized.context.source_chat.kind == "dm"
    assert normalized.context.channel_id.startswith("adapter:qq:")


def _reply_target(parent_id: str | None):
    instance = qq_instance()
    chat = ChatRef(
        "chat-ref:v1:qq:qq-1:group:group-1",
        instance,
        APP_ID,
        "group-1",
        "group",
        None,
        None,
    )
    sender = PrincipalRef(
        "user-1", None, None, None, None, "unresolved", None, None, None, "qq-official-gateway"
    )
    audience = AudienceSnapshot("group", (sender,), "partial", None, 1)

    class _Message:
        content = ()
        attachments = ()

    from hyprial.daemon.impl.adapters.contracts import AuthorizedChatTarget

    return AuthorizedChatTarget("t-1", chat, audience, "reply", parent_id), _Message()


def test_codec_render_is_passive_only_and_carries_msg_id() -> None:
    codec = QQEventCodec(qq_instance(), APP_ID, BOT_ID)
    target, message = _reply_target("message-9")
    draft = codec.render(_resolved(target), message)
    node = draft.nodes[-1]
    template = dict(node.template)
    assert template["msg_id"] == "message-9"
    assert template["action"] == "send_message"
    assert template["scene"] == "group"
    assert template["target_id"] == "group-1"
    bare, _ = _reply_target(None)
    with pytest.raises(ValueError, match="msg_id"):
        codec.render(_resolved(bare), message)


def _resolved(target):
    from hyprial.daemon.impl.adapters.contracts import NativeTargetResolution

    return NativeTargetResolution(target, "resolved", "group-1", None, None)
