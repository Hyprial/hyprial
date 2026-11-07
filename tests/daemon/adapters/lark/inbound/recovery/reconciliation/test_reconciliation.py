from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkApiError, LarkHistoryBatch, QuotedMessage
from hyprial.daemon.impl.adapters.lark.runtime.health import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS, DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS, DEFAULT_REST_PROBE_TIMEOUT_SECONDS, DEFAULT_STALE_AFTER_SECONDS, required_reconcile_lookback
from hyprial.daemon.impl.adapters.lark.worker.sweep import _reconcile_health_result

from ..support import (
    LOOKBACK_SECONDS,
    NOW_MS,
    WINDOW_START_MS,
    actor as actor,
    inbound_message,
    make_adapter,
    sdk_event,
)


def test_reconcile_replays_only_missed_messages_in_window(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    # Live delivery before the "outage": creates the correlation that puts
    # oc_dm into the reconciliation scan set.
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_before")
    assert len(harness.requests) == 1

    lark.history["oc_dm"] = (
        inbound_message("om_before", create_time="1720000001000"),  # seen live
        inbound_message(
            "om_missed", text="sent during the outage", create_time="1720000002000"
        ),
        inbound_message(
            "om_ancient", create_time=str(WINDOW_START_MS - 60_000)  # outside window
        ),
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.chats_scanned == 1
    assert report.messages_scanned == 2  # om_ancient filtered by the window
    assert report.forwarded == 1
    assert report.duplicates == 1
    assert report.retryable_errors == ()
    assert len(harness.requests) == 2
    assert harness.requests[1].message_id == "om_missed"
    assert harness.requests[1].text == "sent during the outage"
    # The history pull used a seconds-level start_time inside the window.
    assert lark.list_calls == [
        ("oc_dm", str(WINDOW_START_MS // 1000), "p2p")
    ]

    # A second sweep (next reconnect) is a no-op: same native ids never
    # re-deliver.
    second = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert second.forwarded == 0
    assert second.duplicates == 2
    assert len(harness.requests) == 2


def test_default_reconcile_covers_oldest_message_before_stale_rebuild(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_before")
    oldest_age = required_reconcile_lookback(
        stale_after=DEFAULT_STALE_AFTER_SECONDS,
        health_interval=DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        probe_timeout=DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
        reconcile_timeout=DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    ) - 1
    lark.history["oc_dm"] = (
        inbound_message(
            "om_boundary",
            text="arrived as the event path became silent",
            create_time=str(NOW_MS - oldest_age * 1000),
        ),
    )

    report = adapter.reconcile_recent(now_ms=NOW_MS)

    assert report.forwarded == 1
    assert len(harness.requests) == 2
    assert harness.requests[-1].message_id == "om_boundary"
    second = adapter.reconcile_recent(now_ms=NOW_MS)
    assert second.forwarded == 0
    assert second.duplicates == 1
    assert len(harness.requests) == 2


def test_reconcile_isolates_per_chat_errors(tmp_path: Path, actor: ActorTarget) -> None:
    adapter, lark, harness, routes = make_adapter(tmp_path, actor)
    routes.pins["lark:oc_two:oc_two"] = actor
    # Two chats with prior activity: one per live forward.
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_two",
        chat_id="oc_two",
    )
    # A real LarkApiError, not a RuntimeError whose *string* mentions a code.
    # The old fixture flattened the platform's structured rejection into text
    # -- which is precisely the defect this change removes -- so it could not
    # tell a permission failure from any other, and neither could the test.
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages",
        99991672,
        "no permission",
        missing_scopes=("im:message.group_msg",),
    )
    lark.history["oc_two"] = (
        inbound_message(
            "om_missed_two", chat_id="oc_two", create_time="1720000002000"
        ),
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.chats_scanned == 2
    assert report.forwarded == 1
    # A permission rejection can never clear by retrying, so it is reported as
    # a blocked chat rather than a retryable error -- the caller fails closed
    # on the latter only.
    assert report.retryable_errors == ()
    assert report.blocked_chats == ("oc_dm: missing-scope im:message.group_msg",)
    assert harness.requests[-1].message_id == "om_missed_two"


def test_reconcile_keeps_an_unclassifiable_failure_retryable(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """Only a *named* permanent cause may leave the fail-closed column.

    Anything else -- a timeout, a rate limit, an SDK shape we have never seen
    -- might succeed on the next sweep, so it must keep failing the probe.
    Widening this to "any error is survivable" would be the same bug pointed
    the other way.
    """

    adapter, lark, harness, routes = make_adapter(tmp_path, actor)
    routes.pins["lark:oc_two:oc_two"] = actor
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_two",
        chat_id="oc_two",
    )
    lark.history_errors["oc_dm"] = TimeoutError("read timed out")
    lark.history["oc_two"] = (
        inbound_message(
            "om_missed_two", chat_id="oc_two", create_time="1720000002000"
        ),
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    assert report.blocked_chats == ()
    # Isolation still holds: the other chat was swept.
    assert harness.requests[-1].message_id == "om_missed_two"


@pytest.mark.parametrize(
    "code",
    [230001, 99999999, None],
    ids=["invalid-parameter", "unknown-code", "no-code"],
)
def test_reconcile_unrecognized_codes_stay_fail_closed(
    tmp_path: Path, actor: ActorTarget, code: int | None
) -> None:
    """Only the documented permanent codes retire a chat; anything else --
    including a missing code -- keeps the sweep failing closed."""

    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", code, "transient looking refusal"
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    assert report.retired_chats == ()
    with pytest.raises(RuntimeError):
        _reconcile_health_result(report)

    # Not retired: the next sweep still probes the chat.
    adapter.reconcile_recent(lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS)
    assert [call[0] for call in lark.list_calls].count("oc_dm") == 2


def test_reconcile_marks_missing_history_scope_as_optional(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A mention-only app must keep its live event stream when history is forbidden."""

    from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_seed")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages",
        230027,
        "permission denied",
    )

    try:
        report = adapter.reconcile_recent(
            lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
        )

        assert report.retryable_errors == ("oc_dm: history-permission-unavailable",)
    finally:
        adapter.close()


def test_capped_history_processes_newest_messages_but_reports_incomplete(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_seed")
    lark.history["oc_dm"] = LarkHistoryBatch(
        (
            inbound_message(
                "om_newest", create_time="1720000002000"
            ),
        ),
        complete=False,
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.forwarded == 0
    assert report.retryable_errors == ("oc_dm: history-incomplete",)
    assert all(request.message_id != "om_newest" for request in harness.requests)


def test_reconcile_then_late_live_event_is_a_duplicate(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A message the platform *also* delivers late must not forward twice."""

    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_seed")
    lark.history["oc_dm"] = (
        inbound_message("om_late", text="borderline", create_time="1720000002000"),
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert report.forwarded == 1

    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(capture, event_id="evt_late", message_id="om_late")

    assert outcome is not None and outcome.status == "duplicate"
    assert len(harness.requests) == 2  # seed + one delivery of om_late


def test_reconcile_forwards_nontext_then_deduplicates_late_live_copy(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_seed", message_id="om_seed")
    post = inbound_message(
        "om_post",
        text="Release note\nready",
        message_type="post",
        create_time=str(NOW_MS - 1000),
    )
    lark.history["oc_dm"] = (post,)

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS,
        now_ms=NOW_MS,
    )
    duplicate = adapter.handle_inbound(
        replace(post, event_id="evt_live_after_reconcile")
    )

    assert report.forwarded == 1
    assert report.retryable_errors == ()
    assert harness.requests[-1].text == "Release note\nready"
    assert harness.requests[-1].provider_metadata["messageType"] == "post"
    assert duplicate.status == "duplicate"
    assert len(harness.requests) == 2  # seed + one delivery of om_post


def test_reconcile_expands_a_missed_merge_forward_and_never_refetches_seen(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """``im.v1.message.list`` returns only a forward's shell, so a sweep that
    recovers one must expand it through ``get_message`` -- and must not spend
    a platform fetch per already-seen forward on every later sweep."""

    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_seed", message_id="om_seed")
    lark.history["oc_dm"] = (
        inbound_message(
            "om_missed_forward",
            text="[Lark merged-forward message]",
            message_type="merge_forward",
            create_time=str(NOW_MS - 1000),
        ),
    )
    quote_fetches: list[str] = []

    def get_message(message_id: str) -> QuotedMessage:
        quote_fetches.append(message_id)
        return QuotedMessage(
            message_id=message_id,
            sender_id="ou_forwarder",
            sender_type="user",
            text=(
                "[Lark merged-forward message: 1 messages]\n"
                "1. sent inside the outage forward"
            ),
            message_type="merge_forward",
        )

    lark.get_message = get_message  # type: ignore[method-assign]

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.forwarded == 1
    assert quote_fetches == ["om_missed_forward"]
    assert "sent inside the outage forward" in harness.requests[-1].text
    assert harness.requests[-1].provider_metadata["messageType"] == "merge_forward"

    second = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert second.duplicates == 1
    # The seen pre-check kept the repeat sweep free of platform fetches.
    assert quote_fetches == ["om_missed_forward"]
