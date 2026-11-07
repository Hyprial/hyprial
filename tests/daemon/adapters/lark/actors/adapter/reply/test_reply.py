from __future__ import annotations

import threading
from pathlib import Path
from hyprial.daemon.impl.adapters.lark import ActorTarget, HarnessDelivery, InboundOutcome, LarkAdapter, LarkInboundMessage, LarkStateStore, QuotedMessage

from ..support import (
    FakeHarness,
    FakeLark,
    FakeRoutes,
    actor as actor,
    make_adapter,
    sdk_event,
)


def test_reply_accepts_before_best_effort_ack_clear_and_route_survives_restart(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")

    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        )
    )

    assert outcome.status == "accepted"
    assert events[-3:] == [
        "lark.reply:om_inbound",
        "harness.accept:delivery_1:om_outbound",
        "lark.clear:om_inbound:OnIt",
    ]
    assert harness.rejected == []
    assert lark.last_reply_text is not None
    assert "Agent: alice/codex/main" in lark.last_reply_text
    assert "Reply-To: om_inbound" in lark.last_reply_text

    restarted = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
    )
    routes.pins["lark:oc_dm:oc_dm"] = ActorTarget(
        actor_id="agent:bob:pi:main",
        actor_key="bob/pi/main",
        display_name="bob/pi/main",
    )
    sdk_event(
        restarted.handle_sdk_event,
        event_id="evt_2",
        message_id="om_followup",
        reply_to="om_outbound",
        text="one more thing",
    )
    assert harness.requests[-1].to == actor


def test_reply_bridge_defers_reaction_cleanup_until_after_control_receipt(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes, events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")

    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        ),
        settle=False,
    )

    assert outcome.status == "accepted"
    assert outcome.native_message_id == "om_outbound"
    assert outcome.cleanup_error is None
    assert harness.accepted == []
    assert events[-1] == "lark.reply:om_inbound"
    assert adapter.clear_delivery_reaction("msg_harness_1") is None
    assert events[-1] == "lark.clear:om_inbound:OnIt"


def test_slow_ack_add_does_not_block_reply_and_clear_wins_race(
    tmp_path: Path, actor: ActorTarget
) -> None:
    events: list[str] = []
    reply_lark = FakeLark(events)

    class BlockingReactionLark(FakeLark):
        def __init__(self) -> None:
            super().__init__(events)
            self.add_entered = threading.Event()
            self.add_release = threading.Event()

        def add_reaction(self, message_id: str, emoji_type: str) -> None:
            self.add_entered.set()
            assert self.add_release.wait(2.0)
            super().add_reaction(message_id, emoji_type)

    reactions = BlockingReactionLark()
    harness = FakeHarness(events)
    routes = FakeRoutes()
    routes.actors[actor.actor_id] = actor
    routes.mentions[actor.display_name] = actor
    routes.pins["lark:oc_dm:oc_dm"] = actor
    adapter = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=reply_lark,
        reaction_lark=reactions,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
        org_context_path=tmp_path / "org-context.md",
    )
    inbound: list[InboundOutcome] = []

    def receive() -> None:
        inbound.append(
            adapter.handle_inbound(
                LarkInboundMessage(
                    event_id="evt_1",
                    message_id="om_inbound",
                    chat_id="oc_dm",
                    chat_type="p2p",
                    text="please investigate",
                    sender_id="ou_sender",
                    sender_type="user",
                )
            )
        )

    receiver = threading.Thread(target=receive)
    receiver.start()
    assert reactions.add_entered.wait(1.0)
    assert harness.requests[-1].message_id == "om_inbound"

    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        ),
        settle=False,
    )
    assert outcome.status == "accepted"
    assert reply_lark.reply_attempts != []

    cleanup_done = threading.Event()

    def cleanup() -> None:
        assert adapter.clear_delivery_reaction("msg_harness_1") is None
        cleanup_done.set()

    cleaner = threading.Thread(target=cleanup)
    cleaner.start()
    assert not cleanup_done.wait(0.05)
    reactions.add_release.set()
    receiver.join(2.0)
    cleaner.join(2.0)
    assert not receiver.is_alive() and not cleaner.is_alive()
    assert inbound[0].status == "forwarded"
    assert cleanup_done.is_set()
    assert reactions.reactions == set()
    adapter.close()


def test_failed_reply_rejects_delivery_and_keeps_onit(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")
    lark.fail_reply = True

    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        )
    )

    assert outcome.status == "rejected"
    assert harness.accepted == []
    assert harness.rejected == [("delivery_1", "send failed", False)]
    assert "om_inbound" in lark.reactions
    assert not any(event.startswith("lark.clear") for event in events)


def test_ack_cleanup_failure_does_not_reject_or_duplicate_delivery(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")
    lark.fail_clear = True

    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        )
    )

    assert outcome.status == "accepted"
    assert outcome.cleanup_error == "reaction cleanup failed"
    assert harness.accepted == [("delivery_1", "om_outbound")]
    assert harness.rejected == []


def test_missing_native_route_falls_back_to_whole_dm_pin_with_quote_context(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)
    lark.quotes["om_external"] = QuotedMessage(
        message_id="om_external",
        sender_id="ou_other",
        sender_type="user",
        text="the original question",
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_1",
        message_id="om_fallback",
        reply_to="om_external",
        text="please continue",
    )

    assert harness.requests[-1].to == actor
    assert harness.requests[-1].conversation_id == "lark:oc_dm:oc_dm"
    assert harness.requests[-1].text == (
        "please continue\n\n"
        "[Quoted message om_external (from ou_other, text)]\n"
        "the original question"
    )


def test_known_native_route_to_unavailable_actor_fails_closed(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")
    adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="done",
        )
    )
    routes.actors.clear()

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_followup",
        reply_to="om_outbound",
    )

    assert [request.message_id for request in harness.requests] == ["om_inbound"]
    assert lark.last_reply_text is not None
    assert "not rerouted" in lark.last_reply_text


def test_unresolved_reply_without_pin_returns_explicit_guidance(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.pins.clear()

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_missing",
        message_id="om_missing",
        reply_to="om_unknown",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert "does not resolve" in lark.last_reply_text


def test_group_chatter_without_mention_or_native_route_is_silent(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_group",
        message_id="om_group",
        chat_id="oc_group",
        chat_type="group",
    )

    assert harness.requests == []


def test_a_native_reply_carries_the_quoted_agent_message(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A quote of the agent's own message must reach the agent (2026-10-07).

    Allen quoted one of an agent's messages ("同意，请开始设计方案"); the
    reply routed natively to that agent but arrived without the quoted
    block, so the agent could not tell which of its messages was meant.
    Only replies that fell back to the DM pin carried the quote.
    """

    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_inbound")
    adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_1",
            message_id="msg_reply_1",
            reply_to="msg_harness_1",
            from_actor=actor,
            text="Proposal: start the design?",
        )
    )
    lark.quotes["om_outbound"] = QuotedMessage(
        message_id="om_outbound",
        sender_id="ou_bot",
        sender_type="app",
        text="Proposal: start the design?",
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_quoting",
        reply_to="om_outbound",
        text="同意，请开始设计方案",
    )

    assert harness.requests[-1].to == actor
    assert harness.requests[-1].text == (
        "同意，请开始设计方案\n\n"
        "[Quoted message om_outbound (from ou_bot, text)]\n"
        "Proposal: start the design?"
    )
