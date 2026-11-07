from __future__ import annotations

import json
from pathlib import Path
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkApiError

from ..support import (
    LOOKBACK_SECONDS,
    NOW_MS,
    actor as actor,
    inbound_message,
    make_adapter,
    sdk_event,
)


def test_live_post_forwards_visible_text_without_opaque_fields(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes = make_adapter(tmp_path, actor)
    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(
        capture,
        event_id="evt_post",
        message_id="om_post",
        message_type="post",
        content=json.dumps(
            {
                "title": "Incident",
                "content": [[
                    {"tag": "text", "text": "please investigate "},
                    {
                        "tag": "a",
                        "text": "dashboard",
                        "href": "https://secret.invalid/?token=must-not-leak",
                    },
                ]],
            }
        ),
    )

    assert outcome is not None and outcome.status == "forwarded"
    assert harness.requests[0].text == "Incident\nplease investigate dashboard"
    assert harness.requests[0].provider_metadata["messageType"] == "post"
    assert "must-not-leak" not in repr(harness.requests)


def test_live_card_forwards_visible_text_but_never_opaque_fields(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_card_guard",
        message_id="om_card_guard",
        message_type="interactive",
        content=json.dumps(
            {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "token=user-visible-live",
                        "href": "https://opaque.invalid/hidden-href",
                        "value": {"target": "hidden-value"},
                    }
                ],
                "raw": {"content": "hidden-raw"},
            }
        ),
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.text.startswith("[Lark card]")
    assert "token=user-visible-live" in request.text
    assert "hidden-href" not in repr(request)
    assert "hidden-value" not in repr(request)
    assert "hidden-raw" not in repr(request)


def test_recover_message_fetches_from_server_and_forwards_once(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    # Establish the chat type from live traffic, as the REST models lack it.
    sdk_event(adapter.handle_sdk_event, event_id="evt_0", message_id="om_live")
    assert len(harness.requests) == 1

    lark.inbound["om_lost"] = inbound_message("om_lost", text="lost in the outage")

    first = adapter.recover_message("om_lost")
    assert first.status == "forwarded"
    assert len(harness.requests) == 2
    assert harness.requests[1].message_id == "om_lost"
    assert harness.requests[1].text == "lost in the outage"
    assert harness.requests[1].provider_metadata["messageId"] == "om_lost"

    second = adapter.recover_message("om_lost")
    assert second.status == "duplicate"
    assert len(harness.requests) == 2
    # Dedup short-circuits before another server fetch.
    assert lark.get_calls == ["om_lost"]


def test_recover_without_known_chat_type_does_not_pin_route(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """Unknown chat_type must not fall into the p2p pin path (group safety)."""

    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    lark.inbound["om_lost"] = inbound_message("om_lost")

    outcome = adapter.recover_message("om_lost")

    assert outcome.status == "no-target"
    assert harness.requests == []
    assert lark.replies == []  # recovery never spams guidance
    assert [letter.reason for letter in adapter.dead_letters()] == ["no-target"]

    # Once live traffic reveals the chat type, recovery succeeds.
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_live")
    assert adapter.recover_message("om_lost").status == "forwarded"
    assert adapter.dead_letters() == ()  # resolved by the successful forward


def test_recover_not_found(tmp_path: Path, actor: ActorTarget) -> None:
    adapter, _lark, _harness, _routes = make_adapter(tmp_path, actor)

    assert adapter.recover_message("om_gone").status == "not-found"


def test_live_event_after_recovery_is_a_duplicate(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    lark.inbound["om_msg"] = inbound_message("om_msg", chat_type="p2p")

    assert adapter.recover_message("om_msg").status == "forwarded"
    assert len(harness.requests) == 1

    # The platform later delivers the same message as a fresh live event.
    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(capture, event_id="evt_new", message_id="om_msg")

    assert outcome is not None and outcome.status == "duplicate"
    assert len(harness.requests) == 1


def test_recovery_after_live_forward_is_a_duplicate(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_msg")
    assert len(harness.requests) == 1
    lark.inbound["om_msg"] = inbound_message("om_msg")

    assert adapter.recover_message("om_msg").status == "duplicate"
    assert len(harness.requests) == 1
    # Already delivered: no server fetch, and no dead letter can linger.
    assert lark.get_calls == []
    assert adapter.dead_letters() == ()


def test_a_lark_api_error_without_scopes_stays_retryable(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The discriminator is ``missing_scopes``, not the exception type.

    A rate limit arrives as the same class as a permission rejection; only the
    scope list says "no retry will ever fix this".
    """

    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", 99991400, "too many requests"
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    assert report.blocked_chats == ()


def test_live_event_unretires_a_chat_the_bot_rejoined(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """230002 is permanent for the *membership*, not the chat: if the bot is
    re-added to the same group, the next live event is direct evidence of
    membership and must lift the retirement -- otherwise reconcile would
    stay blind to that chat through the next websocket outage."""

    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", 230002, "The bot can not be outside the group"
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert report.retired_chats == ("oc_dm",)
    assert adapter._state.retired_chat_code("oc_dm") == 230002

    # The bot is re-added: the next live event lifts the retirement.
    sdk_event(adapter.handle_sdk_event, event_id="evt_2", message_id="om_back")
    assert adapter._state.retired_chat_code("oc_dm") is None

    # And the chat is swept again, so the outage window is recoverable.
    lark.history_errors.clear()
    lark.history["oc_dm"] = (
        inbound_message("om_missed", create_time="1720000002000"),
    )
    second = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert second.retryable_errors == ()
    assert second.forwarded == 1
    assert harness.requests[-1].message_id == "om_missed"


def test_recover_message_unretires_only_when_the_platform_serves(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The third upstream of ``_handle_inbound``: a ``recover_message``
    re-drive counts as membership evidence only when the platform actually
    serves the body; its ``not-found`` early return must leave the
    retirement untouched."""

    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", 230002, "The bot can not be outside the group"
    )
    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert report.retired_chats == ("oc_dm",)

    # Platform refuses to serve the body: early return, retirement stands.
    outcome = adapter.recover_message("om_unserved")
    assert outcome.status == "not-found"
    assert adapter._state.retired_chat_code("oc_dm") == 230002

    # Platform serves the body: that is membership evidence, lift it.
    lark.inbound["om_served"] = inbound_message("om_served")
    served = adapter.recover_message("om_served")
    assert served.status == "forwarded"
    assert adapter._state.retired_chat_code("oc_dm") is None
