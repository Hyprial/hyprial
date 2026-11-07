from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkStateStore, QuotedMessage, ReplyRoute
from hyprial.kernel import Logger

from ..support import (
    BOT_OPEN_ID,
    MERGE_FORWARD_EVENT_CONTENT,
    _pin_group,
    actor as actor,
    forwarded_quote,
    make_adapter,
    sdk_event,
    sdk_event_with_content,
)


def test_adapter_inbound_emits_correlated_trajectory_event_with_required_fields(
    tmp_path: Path, actor: ActorTarget
) -> None:
    logger = Logger.adapter(tmp_path, name="test")
    adapter, _lark, harness, _routes, _events = make_adapter(
        tmp_path, actor, logger=logger
    )
    try:
        sdk_event(
            adapter.handle_sdk_event,
            event_id="evt-trajectory",
            message_id="om-trajectory",
            text="trace this",
        )

        assert len(harness.requests) == 1
        records = [json.loads(line) for line in logger.path.read_text().splitlines()]
        inbound = next(item for item in records if item["event"] == "adapter.inbound")
        assert {
            "messageId": inbound["messageId"],
            "correlationId": inbound["correlationId"],
            "node": inbound["node"],
            "component": inbound["component"],
            "conversationId": inbound["conversationId"],
        } == {
            "messageId": "msg_harness_1",
            "correlationId": "msg_harness_1",
            "node": "adapter-inbound",
            "component": "adapter",
            "conversationId": "lark:oc_dm:oc_dm",
        }
        assert inbound["nativeMessageId"] == "om-trajectory"
        assert inbound["nativeEventId"] == "evt-trajectory"
        assert inbound["actorId"] == "channel:lark:tenant:bot"
        assert inbound["sender"] == "channel:lark:tenant:bot"
        assert inbound["target"] == actor.actor_id
        assert inbound["platform"] == "lark"
        assert isinstance(inbound["ts"], str)
    finally:
        adapter._state.close()


def test_lark_help_is_consumed_locally_and_never_enters_agent_prompt(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_help",
        message_id="om_help",
        text="/hyprial",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert "/hyprial agents" in lark.last_reply_text
    assert "/hyprial ask <runtime-target> <message>" in lark.last_reply_text
    assert "/hyprial org" in lark.last_reply_text
    assert "route" not in lark.last_reply_text.casefold()
    assert "pin" not in lark.last_reply_text.casefold()


def test_unsupported_inbound_type_replies_with_a_native_failure_alarm(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """Phase-0 red proof: an unsupported human message must not stay silent."""

    adapter, lark, harness, _routes, _events = make_adapter(
        tmp_path, actor, logger=Logger.adapter(tmp_path, name="test")
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt-unsupported-alarm",
        message_id="om-unsupported-alarm",
        message_type="quantum_teleport",
        content=json.dumps({"payload": "opaque"}),
    )

    assert harness.requests == []
    assert lark.reply_attempts == [
        (
            "om-unsupported-alarm",
            "alarm:om-unsupported-alarm:unsupported-message-type",
        )
    ]
    assert lark.reply_bodies == [
        "消息投递失败：unsupported-message-type（消息 om-unsupported-alarm）。"
    ]
    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "lark-gateway.jsonl").read_text().splitlines()
    ]
    alarm_records = [item for item in records if str(item["event"]).startswith("alarm.")]
    assert [item["event"] for item in alarm_records] == [
        "alarm.raised",
        "alarm.delivered",
    ]
    terminal = [item for item in records if item["event"] == "terminal.failure"]
    assert len(terminal) == 1
    assert {item["correlationId"] for item in [*terminal, *alarm_records]} == {
        "om-unsupported-alarm"
    }


def test_no_actor_human_failure_is_native_and_throttled(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(
        tmp_path, actor, logger=Logger.adapter(tmp_path, name="test")
    )
    routes.pins.clear()

    for suffix in ("one", "two"):
        sdk_event(
            adapter.handle_sdk_event,
            event_id=f"evt-no-actor-{suffix}",
            message_id=f"om-no-actor-{suffix}",
            text="please run",
        )

    assert harness.requests == []
    assert lark.reply_attempts == [
        ("om-no-actor-one", "alarm:om-no-actor-one:no-target")
    ]


def test_alarm_send_failure_does_not_bounce_again(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, _harness, routes, _events = make_adapter(
        tmp_path, actor, logger=Logger.adapter(tmp_path, name="test")
    )
    routes.pins.clear()
    lark.fail_reply = True

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt-alarm-fails",
        message_id="om-alarm-fails",
        text="please run",
    )

    assert lark.events.count("lark.reply:om-alarm-fails") == 1
    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "lark-gateway.jsonl").read_text().splitlines()
    ]
    assert [item["event"] for item in records].count("alarm.failed") == 1


def test_official_sdk_event_converts_then_adds_onit_ack(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_1",
        message_id="om_inbound",
        text="@_user_1 please investigate",
        mention=actor.display_name,
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.to == actor
    assert request.text == "please investigate"
    assert request.conversation_id == "lark:oc_dm:oc_dm"
    assert request.provider_metadata == {
        "provider": "lark",
        "eventId": "evt_1",
        "messageId": "om_inbound",
        "chatId": "oc_dm",
        "chatType": "p2p",
        "rootId": "om_root",
        "threadId": None,
        "replyTo": None,
        "senderId": "ou_sender",
        "senderType": "user",
        # Nothing on record names this sender, so the block says so
        # explicitly instead of being absent or guessing a name.
        "sender": {
            "kind": "user",
            "platformId": "ou_sender",
            "unionId": "on_sender",
            "displayName": None,
            "owner": None,
            "standing": "unresolved",
            "source": None,
        },
        "createTime": "1720000000000",
        "messageType": "text",
    }
    assert events == [
        "harness.send:om_inbound",
        "lark.react:om_inbound:OnIt",
    ]
    assert lark.reactions == {"om_inbound"}
    assert LarkStateStore(tmp_path / "lark-state.sqlite3").request(
        "msg_harness_1"
    ).message_id == ("om_inbound")


def test_post_rich_text_message_is_parsed_and_forwarded_not_dead_lettered(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """P0 regression: rich-text (post) messages used to hit ``_required``'s
    ``content.get("text")`` lookup (posts have no top-level ``text`` field),
    raise, and get silently dead-lettered instead of reaching the agent."""

    adapter, _lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event_with_content(
        adapter.handle_sdk_event,
        event_id="evt_post",
        message_id="om_post",
        message_type="post",
        content={
            "zh_cn": {
                "title": "Deploy request",
                "content": [
                    [
                        {"tag": "text", "text": "please rotate the "},
                        {"tag": "a", "text": "Cloudflare token", "href": "https://example.com/token"},
                    ],
                    [
                        {"tag": "text", "text": "cc "},
                        {"tag": "at", "user_id": "ou_x", "user_name": "tom"},
                    ],
                ],
            }
        },
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.provider_metadata["messageType"] == "post"
    assert "Deploy request" in request.text
    assert "please rotate the Cloudflare token" in request.text
    assert "https://example.com/token" not in request.text
    assert "@tom" in request.text
    assert request.text.strip() != ""
    assert adapter.dead_letters() == ()


def test_direct_media_requests_name_the_exact_fetch_command(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    for suffix, message_type, content, ref in (
        (
            "image",
            "image",
            {"image_key": "img_v2_direct"},
            "ref:om_direct_image/img_v2_direct",
        ),
        (
            "file",
            "file",
            {"file_key": "file_v3_direct", "file_name": "runbook.md"},
            "ref:om_direct_file/file_v3_direct",
        ),
    ):
        sdk_event_with_content(
            adapter.handle_sdk_event,
            event_id=f"evt_direct_{suffix}",
            message_id=f"om_direct_{suffix}",
            message_type=message_type,
            content=content,
        )
        request = harness.requests[-1]
        assert ref in request.text
        assert "hyprial adapter media get bot <ref>" in request.text
        assert request.text.count("hyprial adapter media get bot <ref>") == 1


def test_merge_forward_event_is_expanded_into_its_children(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A forwarded conversation must reach the agent as its contents.

    The live event body is only the platform placeholder, so the adapter
    fetches the shell once and forwards what the children actually said.
    """

    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)
    lark.quotes["om_forward"] = forwarded_quote("om_forward")

    sdk_event_with_content(
        adapter.handle_sdk_event,
        event_id="evt_forward",
        message_id="om_forward",
        message_type="merge_forward",
        content=MERGE_FORWARD_EVENT_CONTENT,
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.provider_metadata["messageType"] == "merge_forward"
    # The words inside the forward, not a stand-in for them.
    assert "Incident 42" in request.text
    assert "gateway timed out" in request.text
    assert "and the Cloudflare token: abc123" in request.text
    # A forwarded resource ref retains the child id but downloads through the
    # received shell id. The request also names the exact directed-pull command.
    assert (
        "[Lark image ref:om_forward/om_child_image/img_v2_fwd]"
        in request.text
    )
    assert (
        "hyprial adapter media get bot <ref>" in request.text
    )
    assert request.text.count("hyprial adapter media get bot <ref>") == 1
    assert request.text != "[Lark merged-forward message]"
    # Exactly one bounded platform fetch, and it happens before the inbound
    # lock rather than inside it.
    assert events.count("lark.get:om_forward") == 1
    assert adapter.dead_letters() == ()


def test_redelivered_merge_forward_event_is_not_refetched(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The duplicate pre-check keeps platform redelivery from costing one
    ``get_message`` per repeat of an already-forwarded event."""

    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)
    lark.quotes["om_forward"] = forwarded_quote("om_forward")

    for _ in range(2):
        sdk_event_with_content(
            adapter.handle_sdk_event,
            event_id="evt_forward",
            message_id="om_forward",
            message_type="merge_forward",
            content=MERGE_FORWARD_EVENT_CONTENT,
        )

    assert len(harness.requests) == 1
    assert events.count("lark.get:om_forward") == 1


def test_merge_forward_expansion_failure_degrades_loudly_not_silently(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """When the children cannot be fetched the forward still arrives, and it
    says so instead of pretending the placeholder was the message."""

    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    def failing_get(message_id: str) -> QuotedMessage | None:
        raise RuntimeError("platform unavailable")

    lark.get_message = failing_get  # type: ignore[method-assign]

    sdk_event_with_content(
        adapter.handle_sdk_event,
        event_id="evt_forward_opaque",
        message_id="om_forward_opaque",
        message_type="merge_forward",
        content=MERGE_FORWARD_EVENT_CONTENT,
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.text == "[Lark merged-forward message (contents unavailable)]"
    assert request.provider_metadata["messageType"] == "merge_forward"
    # A fetch failure is a degraded body, never a dropped message.
    assert adapter.dead_letters() == ()


def test_merge_forward_without_children_keeps_the_plain_label(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)
    lark.quotes["om_forward_empty"] = QuotedMessage(
        message_id="om_forward_empty",
        sender_id="ou_forwarder",
        sender_type="user",
        text="[Lark merged-forward message]",
        message_type="merge_forward",
    )

    sdk_event_with_content(
        adapter.handle_sdk_event,
        event_id="evt_forward_empty",
        message_id="om_forward_empty",
        message_type="merge_forward",
        content=MERGE_FORWARD_EVENT_CONTENT,
    )

    assert len(harness.requests) == 1
    assert harness.requests[0].text == "[Lark merged-forward message]"
    assert adapter.dead_letters() == ()


def test_quoting_a_merge_forward_yields_readable_context_not_unavailable(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The reported defect: replying to a forwarded conversation gave the
    agent only ``[Quoted message om_... (unavailable)]``."""

    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)
    lark.quotes["om_forwarded_context"] = forwarded_quote("om_forwarded_context")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_quote_forward",
        message_id="om_reply_to_forward",
        reply_to="om_forwarded_context",
        text="please summarize this",
    )

    request = harness.requests[-1]
    assert "Quoted context could not be resolved." not in request.text
    assert "(unavailable)" not in request.text
    assert (
        "[Quoted message om_forwarded_context "
        "(from ou_forwarder, merge_forward)]" in request.text
    )
    assert "Incident 42" in request.text
    assert "and the Cloudflare token: abc123" in request.text


def test_duplicate_event_is_not_forwarded_twice(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(adapter.handle_sdk_event, event_id="evt_same", message_id="om_same")
    sdk_event(adapter.handle_sdk_event, event_id="evt_same", message_id="om_same")

    assert len(harness.requests) == 1


def test_group_mention_of_this_bot_routes_to_the_pinned_agent(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """@ the adapter's bot in a group -> the adapter's pinned agent.

    Identity-keyed: the mention's open_id equals the app's own bot open_id.
    The mentioned display name is deliberately NOT the pinned agent's name --
    name matching is gone, so only the identity comparison can explain the
    delivery.
    """

    adapter, _lark, harness, routes, _events = make_adapter(tmp_path, actor)
    _pin_group(routes, actor, "oc_group")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group_at",
        message_id="om_group_at",
        chat_id="oc_group",
        chat_type="group",
        text="@_user_1 please summarize",
        mention="HyprialOS HQ Adjutant",
        mention_open_id=BOT_OPEN_ID,
    )

    assert len(harness.requests) == 1
    assert harness.requests[0].to == actor
    assert harness.requests[0].text == "please summarize"


def test_group_mention_of_somebody_else_stays_ignored(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    _pin_group(routes, actor, "oc_group")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group_other",
        message_id="om_group_other",
        chat_id="oc_group",
        chat_type="group",
        mention="Some Human",
        mention_open_id="ou_other_user",
    )

    assert harness.requests == []
    # Ignored means silent: no guidance reply in a group we were not asked in.
    assert lark.last_reply_text is None


def test_group_mention_of_this_bot_without_a_pin_is_a_loud_no_target(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.pins.clear()

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group_unpinned",
        message_id="om_group_unpinned",
        chat_id="oc_group",
        chat_type="group",
        mention="HyprialOS HQ Adjutant",
        mention_open_id=BOT_OPEN_ID,
    )

    assert harness.requests == []
    # The user addressed us; the guidance names the exact operator command.
    assert lark.last_reply_text is not None
    assert "hyprial adapter pin bot <agent>" in lark.last_reply_text
    # And the body is preserved as a dead letter, not silently gone.
    assert any(
        letter.message_id == "om_group_unpinned"
        for letter in adapter.dead_letters()
    )


def test_unanswerable_bot_identity_keeps_group_traffic_ignored(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """No identity, no guessing: mention routing declines, group stays silent."""

    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    _pin_group(routes, actor, "oc_group")
    lark.bot_open_id_value = None

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group_noid",
        message_id="om_group_noid",
        chat_id="oc_group",
        chat_type="group",
        mention="HyprialOS HQ Adjutant",
        mention_open_id=BOT_OPEN_ID,
    )

    assert harness.requests == []
    assert lark.last_reply_text is None


def test_rich_text_post_mentioning_this_bot_routes_with_readable_content(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The production shape that used to vanish: a group post (image + text)
    that @s the bot must reach the pinned agent with readable text."""

    adapter, _lark, harness, routes, _events = make_adapter(tmp_path, actor)
    _pin_group(routes, actor, "oc_group")

    post = {
        "title": "Deploy plan",
        "content": [
            [
                {"tag": "at", "user_id": "cli_bot", "user_name": "HQ Adjutant"},
                {"tag": "text", "text": " please review the rollout"},
            ],
            [{"tag": "img", "image_key": "img_v2_key"}],
        ],
    }
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group_post",
        message_id="om_group_post",
        chat_id="oc_group",
        chat_type="group",
        message_type="post",
        content=json.dumps(post),
        mention="HQ Adjutant",
        mention_open_id=BOT_OPEN_ID,
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.to == actor
    assert "Deploy plan" in request.text
    assert "please review the rollout" in request.text
    # The embedded image stand-in carries the retrieval ref of the post
    # message that carries it (2026-08-14 ruling).
    assert "[image ref:om_group_post/img_v2_key]" in request.text


def test_dm_mention_of_this_bot_agrees_with_the_pin(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """In a DM the pin is already unique; @ing the bot lands on the same
    agent, so mention and pin cannot disagree."""

    adapter, _lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_dm_at",
        message_id="om_dm_at",
        text="@_user_1 status?",
        mention="HQ Adjutant",
        mention_open_id=BOT_OPEN_ID,
    )

    assert len(harness.requests) == 1
    assert harness.requests[0].to == actor


def test_state_store_merges_concurrent_writers_without_temp_file_races(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "lark-state.sqlite3"
    stores = [LarkStateStore(state_path), LarkStateStore(state_path)]

    def write(index: int) -> None:
        store = stores[index % len(stores)]
        if index % 2 == 0:
            store.record_seen(f"event_{index}")
            return
        store.record_reply(
            ReplyRoute(
                message_id=f"om_{index}",
                actor_id="agent:alice:codex:main",
                actor_key="alice/codex/main",
                harness_message_id=f"msg_{index}",
                conversation_id="lark:oc_dm:oc_dm",
                chat_id="oc_dm",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(200)))

    restored = LarkStateStore(state_path)
    for index in range(200):
        if index % 2 == 0:
            assert restored.seen(f"event_{index}")
        else:
            assert restored.reply(f"om_{index}") is not None
