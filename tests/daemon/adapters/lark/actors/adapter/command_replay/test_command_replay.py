from __future__ import annotations

from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkAdapter, LarkInboundMessage, LarkStateStore
from hyprial.daemon.impl.adapters.lark.contracts.messages import MAX_LARK_TEXT_CONTENT_BYTES
from hyprial.daemon.impl.adapters.lark.contracts.wire import encode_lark_text_content
from hyprial.daemon.impl.adapters.lark.state.records import MAX_PENDING_COMMAND_RESPONSE_BYTES
from hyprial.daemon.impl.adapters.lark.inbound import commands as lark_adapter_commands
from hyprial.daemon.impl.adapters.lark.state import records as lark_state_records

from ..support import (
    actor as actor,
    make_adapter,
    runtime_target,
    sdk_event,
)


def test_local_command_reply_replay_uses_persisted_immutable_response(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (runtime_target("agent:node-a"),)
    state = adapter._state
    original = state.complete_command_response

    def crash_after_reply(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic crash after provider accepted reply")

    monkeypatch.setattr(state, "complete_command_response", crash_after_reply)
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_replayed_command",
        message_id="om_replayed_command",
        text="/hyprial agents",
    )
    first_pending = LarkStateStore(
        tmp_path / "lark-state.sqlite3"
    ).pending_command_response("om_replayed_command")
    assert first_pending is not None
    assert first_pending.message_id == "om_replayed_command"
    assert first_pending.event_id == "evt_replayed_command"
    assert first_pending.kind == "agents"
    assert first_pending.idempotency_key == (
        "om_replayed_command:command:agents"
    )
    assert first_pending.text == lark.reply_bodies[0]

    # Anything used to compute the response may change after provider
    # acceptance.  A restart/replay must resend the immutable persisted body,
    # not recalculate against today's target directory.
    routes.runtime_targets = (runtime_target("agent:node-b"),)
    monkeypatch.setattr(
        routes,
        "list_runtime_targets",
        lambda: (_ for _ in ()).throw(AssertionError("response was recalculated")),
    )
    restarted = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
    )
    monkeypatch.setattr(restarted._state, "complete_command_response", original)
    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt_replayed_command_new_delivery",
        message_id="om_replayed_command",
        text="/hyprial agents",
    )

    assert harness.requests == []
    assert len(lark.reply_attempts) == 2
    assert lark.reply_attempts[0][1] == lark.reply_attempts[1][1]
    assert lark.reply_attempts[0][1] == "om_replayed_command:command:agents"
    assert lark.reply_bodies[0] == lark.reply_bodies[1]
    assert "agent:node-a" in lark.reply_bodies[1]
    assert "agent:node-b" not in lark.reply_bodies[1]
    assert len(lark.visible_replies) == 1
    restored = LarkStateStore(tmp_path / "lark-state.sqlite3")
    assert restored.pending_command_response("om_replayed_command") is None
    assert restored.seen("evt_replayed_command")
    assert restored.seen("evt_replayed_command_new_delivery")
    assert restored.seen_message("om_replayed_command")


def test_local_command_pending_response_freezes_clock_and_timezone(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    state = adapter._state
    original = state.complete_command_response
    monkeypatch.setattr(lark_adapter_commands, "_time_in", lambda _zone: "FIRST CLOCK")
    monkeypatch.setattr(
        state,
        "complete_command_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_timezone_first",
        message_id="om_timezone_pending",
        text="/hyprial set timezone=Asia/Taipei",
    )
    state.set_conversation_timezone("lark:oc_dm:oc_dm", "UTC")
    monkeypatch.setattr(
        lark_adapter_commands,
        "_time_in",
        lambda _zone: (_ for _ in ()).throw(AssertionError("clock was recalculated")),
    )
    restarted = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
    )
    monkeypatch.setattr(restarted._state, "complete_command_response", original)

    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt_timezone_retry",
        message_id="om_timezone_pending",
        text="/hyprial set timezone=Asia/Taipei",
    )

    assert lark.reply_bodies == [lark.reply_bodies[0], lark.reply_bodies[0]]
    assert "FIRST CLOCK" in lark.reply_bodies[1]
    assert "Asia/Taipei" in lark.reply_bodies[1]
    assert LarkStateStore(tmp_path / "lark-state.sqlite3").conversation_timezone(
        "lark:oc_dm:oc_dm"
    ) == "UTC"


def test_local_command_send_failure_keeps_pending_response_for_retry(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (runtime_target("agent:before-failure"),)
    lark.fail_reply = True

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_send_fails",
        message_id="om_send_fails",
        text="/hyprial agents",
    )

    pending = LarkStateStore(tmp_path / "lark-state.sqlite3").pending_command_response(
        "om_send_fails"
    )
    assert pending is not None
    assert "agent:before-failure" in pending.text
    routes.runtime_targets = (runtime_target("agent:after-failure"),)
    lark.fail_reply = False
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_send_retry",
        message_id="om_send_fails",
        text="/hyprial agents",
    )

    assert lark.reply_bodies == [pending.text]
    assert "agent:after-failure" not in lark.reply_bodies[0]
    assert harness.requests == []


def test_local_command_never_sends_when_pending_state_cannot_be_written(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)
    monkeypatch.setattr(
        adapter._state,
        "record_pending_command_response",
        lambda _response: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_state_write_fails",
        message_id="om_state_write_fails",
        text="/hyprial",
    )

    assert not any(item.startswith("lark.reply:") for item in events)
    assert lark.reply_attempts == []
    assert harness.requests == []


def test_local_command_never_sends_response_at_lark_wire_limit(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)
    overhead = len(encode_lark_text_content("").encode("utf-8"))
    reply = "x" * (MAX_LARK_TEXT_CONTENT_BYTES - overhead)
    assert (
        len(encode_lark_text_content(reply).encode("utf-8"))
        == MAX_LARK_TEXT_CONTENT_BYTES
    )
    monkeypatch.setattr(lark_adapter_commands, "_command_help", lambda: reply)

    outcome = adapter.handle_inbound(
        LarkInboundMessage(
            event_id="evt-wire-limit",
            message_id="om-wire-limit",
            chat_id="oc_dm",
            chat_type="p2p",
            text="/hyprial",
            sender_id="ou_sender",
            sender_type="user",
        )
    )

    assert outcome.status == "error"
    assert outcome.ack_error == "ValueError"
    assert not any(item.startswith("lark.reply:") for item in events)
    assert lark.reply_attempts == []
    assert harness.requests == []
    assert adapter._state.pending_command_response("om-wire-limit") is None


def test_saturated_pending_commands_fail_closed_without_provider_send(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lark_state_records, "MAX_PENDING_COMMAND_RESPONSES", 1)
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (runtime_target("agent:old"),)
    lark.fail_reply = True
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt-old",
        message_id="om-old",
        text="/hyprial agents",
    )
    old = adapter._state.pending_command_response("om-old")
    assert old is not None

    lark.fail_reply = False
    before_attempts = tuple(lark.reply_attempts)
    outcome = adapter.handle_inbound(
        LarkInboundMessage(
            event_id="evt-new",
            message_id="om-new",
            chat_id="oc_dm",
            chat_type="p2p",
            text="/hyprial",
            sender_id="ou_sender",
            sender_type="user",
        )
    )

    assert outcome.status == "command-capacity-exhausted"
    assert outcome.pending_capacity is not None
    assert outcome.pending_capacity.as_dict() == {
        "status": "full",
        "count": 1,
        "bytes": adapter._state.pending_command_capacity().bytes,
        "maxCount": 1,
        "maxBytes": MAX_PENDING_COMMAND_RESPONSE_BYTES,
    }
    assert tuple(lark.reply_attempts) == before_attempts
    assert harness.requests == []
    assert adapter._state.pending_command_response("om-old") == old
    assert adapter._state.pending_command_response("om-new") is None


def test_saturated_restart_replays_original_body_with_same_provider_uuid_once(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lark_state_records, "MAX_PENDING_COMMAND_RESPONSES", 1)
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (runtime_target("agent:before-restart"),)
    lark.fail_reply = True
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt-original",
        message_id="om-restart",
        text="/hyprial agents",
    )
    original = adapter._state.pending_command_response("om-restart")
    assert original is not None

    routes.runtime_targets = (runtime_target("agent:after-restart"),)
    restarted = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
    )
    lark.fail_reply = False
    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt-redelivery",
        message_id="om-restart",
        text="/hyprial agents",
    )
    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt-redelivery-again",
        message_id="om-restart",
        text="/hyprial agents",
    )

    assert lark.reply_attempts == [("om-restart", original.idempotency_key)]
    assert lark.reply_bodies == [original.text]
    assert lark.reply_bodies[0].encode("utf-8") == original.text.encode("utf-8")
    assert lark.visible_replies == {original.idempotency_key: original.text}
    assert "agent:before-restart" in original.text
    assert "agent:after-restart" not in original.text
    assert restarted._state.pending_command_capacity().count == 0
    assert harness.requests == []


def test_exact_aggregate_byte_limit_replays_existing_body_immutably(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (runtime_target("agent:before-byte-limit"),)
    lark.fail_reply = True
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt-byte-original",
        message_id="om-byte-limit",
        text="/hyprial agents",
    )
    original = adapter._state.pending_command_response("om-byte-limit")
    assert original is not None
    exact_bytes = adapter._state.pending_command_capacity().bytes
    monkeypatch.setattr(
        lark_state_records, "MAX_PENDING_COMMAND_RESPONSE_BYTES", exact_bytes
    )

    routes.runtime_targets = (runtime_target("agent:after-byte-limit"),)
    restarted = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
    )
    assert restarted._state.pending_command_capacity().status == "full"
    lark.fail_reply = False
    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt-byte-redelivery",
        message_id="om-byte-limit",
        text="/hyprial agents",
    )

    assert lark.reply_attempts == [
        ("om-byte-limit", original.idempotency_key)
    ]
    assert lark.reply_bodies == [original.text]
    assert lark.reply_bodies[0].encode("utf-8") == original.text.encode("utf-8")
    assert "agent:before-byte-limit" in original.text
    assert "agent:after-byte-limit" not in original.text
    assert restarted._state.pending_command_capacity().count == 0
    assert harness.requests == []


def test_consumed_command_deduplicates_by_native_message_id(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, _harness, _routes, events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_help_live",
        message_id="om_help_same",
        text="/hyprial",
    )
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_help_replay",
        message_id="om_help_same",
        text="/hyprial",
    )

    assert [event for event in events if event == "lark.reply:om_help_same"] == [
        "lark.reply:om_help_same"
    ]
    assert lark.last_reply_text is not None
