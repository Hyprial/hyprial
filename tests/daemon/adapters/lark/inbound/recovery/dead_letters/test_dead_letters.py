from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from lark_oapi import EventDispatcherHandler
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkStateStore
from hyprial.daemon.impl.adapters.lark.runtime.health import LarkStreamHealthMonitor
from hyprial.daemon.impl.adapters.lark.state.records import DEAD_LETTER_TEXT_LIMIT, MAX_DEAD_LETTERS, DeadLetter
from hyprial.daemon.impl.adapters.lark.worker.sweep import _reconcile_health_result
from hyprial.kernel import Logger

from ..support import (
    LOOKBACK_SECONDS,
    NOW_MS,
    actor as actor,
    inbound_message,
    make_adapter,
    sdk_event,
)


def test_forward_failure_preserves_body_in_dead_letters(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes = make_adapter(tmp_path, actor)
    harness.fail = True

    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(capture, event_id="evt_1", message_id="om_lost", text="deploy is red")

    assert outcome is not None and outcome.status == "error"
    assert outcome.ack_error == "SubmitOutcomeUnknownError"
    # Persisted, not just in memory: a fresh store over the same file sees it.
    letters = LarkStateStore(tmp_path / "lark-state.sqlite3").dead_letters()
    assert len(letters) == 1
    letter = letters[0]
    assert letter.message_id == "om_lost"
    assert letter.event_id == "evt_1"
    assert letter.text == "deploy is red"
    assert letter.reason == "forward-outcome-unknown"
    assert letter.detail == "SUBMIT_OUTCOME_UNKNOWN"
    assert letter.chat_id == "oc_dm"
    assert letter.sender_id == "ou_sender"
    assert letter.create_time == "1720000001000"
    # The message was never marked delivered: it stays recoverable.
    assert not LarkStateStore(tmp_path / "lark-state.sqlite3").seen_message("om_lost")


def test_no_target_dm_dead_letters_body_and_still_guides(tmp_path: Path) -> None:
    adapter, lark, _harness, _routes = make_adapter(tmp_path)

    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(capture, event_id="evt_1", message_id="om_orphan", text="anyone there?")

    assert outcome is not None and outcome.status == "no-target"
    assert [reply_to for reply_to, _text in lark.replies] == ["om_orphan"]
    letters = adapter.dead_letters()
    assert len(letters) == 1
    assert letters[0].reason == "no-target"
    assert letters[0].text == "anyone there?"


def test_dead_letter_log_contains_references_but_never_original_content(
    tmp_path: Path,
) -> None:
    logger = Logger.adapter(tmp_path, name="test")
    adapter, _lark, _harness, _routes = make_adapter(tmp_path, logger=logger)
    message = inbound_message(
        "om_visible",
        chat_id="oc_visible",
        chat_type="p2p",
        text="RAW_BODY_MUST_NOT_APPEAR",
    )

    assert adapter._record_dead_letter(
        message, reason="forward-error", detail="RAW_DETAIL_MUST_NOT_APPEAR"
    )
    assert not adapter._record_dead_letter(
        message, reason="forward-error", detail="RAW_DETAIL_MUST_NOT_APPEAR"
    )

    raw_log = logger.path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw_log.splitlines()]
    assert len(records) == 4
    assert records[0]["component"] == "adapter"
    assert records[0]["name"] == "test"
    assert records[0]["event"] == "dead-letter"
    assert records[0]["reason"] == "forward-error"
    assert records[0]["message_id"] == "om_visible"
    assert records[0]["chat_id"] == "oc_visible"
    assert records[0]["chat_type"] == "p2p"
    assert records[0]["conversation_id"] == "lark:oc_visible:oc_visible"
    assert records[0]["level"] == "warn"
    assert [record["event"] for record in records[1:]] == [
        "terminal.failure",
        "alarm.raised",
        "alarm.delivered",
    ]
    assert all(record["correlationId"] == "om_visible" for record in records[1:])
    assert "RAW_BODY_MUST_NOT_APPEAR" not in raw_log
    assert "RAW_DETAIL_MUST_NOT_APPEAR" not in raw_log
    assert "ou_sender" not in raw_log


def test_dead_letter_backlog_notifies_once_per_window_across_restart(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 8, 16, 9, 5, tzinfo=UTC)]
    notifications: list[tuple[str, str]] = []

    def notify(text: str, idempotency_key: str) -> bool:
        notifications.append((text, idempotency_key))
        return True

    def clock() -> datetime:
        return current[0]

    adapter, _lark, _harness, _routes = make_adapter(
        tmp_path,
        notify_operator=notify,
        dead_letter_alert_threshold=3,
        utcnow=clock,
    )
    assert adapter._record_dead_letter(
        inbound_message("om_1", text="first body"), reason="forward-error"
    )
    assert adapter._record_dead_letter(
        inbound_message("om_2", text="second body"), reason="no-target"
    )
    assert adapter._record_dead_letter(
        inbound_message("om_3", text="third body"), reason="forward-error"
    )
    assert notifications == [
        (
            "Dead-letter backlog: count=3; reasons=forward-error:2,no-target:1",
            "dead-letter-backlog:test:1786870800",
        )
    ]
    assert all(
        token not in notifications[0][0] for token in ("om_", "oc_", "body")
    )

    restarted, _lark, _harness, _routes = make_adapter(
        tmp_path,
        notify_operator=notify,
        dead_letter_alert_threshold=3,
        utcnow=clock,
    )
    assert restarted._record_dead_letter(
        inbound_message("om_4"), reason="no-target"
    )
    assert len(notifications) == 1

    current[0] = datetime(2026, 8, 16, 10, 5, tzinfo=UTC)
    for index, reason in enumerate(
        ("normalize-error", "normalize-error", "route-unavailable"), start=5
    ):
        assert restarted._record_dead_letter(
            inbound_message(f"om_{index}"), reason=reason
        )
    assert notifications[-1][0] == (
        "Dead-letter backlog: count=3; "
        "reasons=normalize-error:2,route-unavailable:1"
    )
    assert len(notifications) == 2


def test_dead_letter_backlog_retries_after_notification_failure(
    tmp_path: Path,
) -> None:
    attempts: list[tuple[str, str]] = []

    def notify(text: str, idempotency_key: str) -> bool:
        attempts.append((text, idempotency_key))
        return len(attempts) > 1

    adapter, _lark, _harness, _routes = make_adapter(
        tmp_path,
        notify_operator=notify,
        dead_letter_alert_threshold=1,
        utcnow=lambda: datetime(2026, 8, 16, 9, 5, tzinfo=UTC),
    )

    assert adapter._record_dead_letter(
        inbound_message("om_retry_1", text="first secret body"),
        reason="forward-error",
    )
    assert adapter._record_dead_letter(
        inbound_message("om_retry_2", text="second secret body"),
        reason="no-target",
    )
    assert adapter._record_dead_letter(
        inbound_message("om_after_success"), reason="no-target"
    )

    assert attempts == [
        (
            "Dead-letter backlog: count=1; reasons=forward-error:1",
            "dead-letter-backlog:test:1786870800",
        ),
        (
            "Dead-letter backlog: count=2; reasons=forward-error:1,no-target:1",
            "dead-letter-backlog:test:1786870800",
        ),
    ]
    assert all("secret body" not in text for text, _key in attempts)


def test_dead_letter_threshold_reuses_alarm_emitter_vocabulary(
    tmp_path: Path,
) -> None:
    notifications: list[tuple[str, str]] = []
    logger = Logger.adapter(tmp_path, name="test")
    adapter, _lark, _harness, _routes = make_adapter(
        tmp_path,
        logger=logger,
        notify_operator=lambda text, key: not notifications.append((text, key)),
        dead_letter_alert_threshold=1,
        utcnow=lambda: datetime(2026, 8, 16, 9, 5, tzinfo=UTC),
    )

    assert adapter._record_dead_letter(
        inbound_message("om_threshold"), reason="forward-error"
    )

    assert notifications == [
        (
            "Dead-letter backlog: count=1; reasons=forward-error:1",
            "dead-letter-backlog:test:1786870800",
        )
    ]
    records = [
        json.loads(line) for line in logger.path.read_text().splitlines()
    ]
    threshold = [
        item
        for item in records
        if item.get("correlationId")
        == "dead-letter-backlog:test:1786870800"
    ]
    assert [item["event"] for item in threshold] == [
        "alarm.raised",
        "alarm.delivered",
    ]
    assert all(item["reason"] == "dead-letter-backlog" for item in threshold)


def test_ignored_group_message_is_not_dead_lettered(tmp_path: Path) -> None:
    adapter, _lark, _harness, _routes = make_adapter(tmp_path)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_1",
        message_id="om_group",
        chat_id="oc_group",
        chat_type="group",
    )

    assert adapter.dead_letters() == ()


def test_unparseable_event_dead_letters_best_effort(tmp_path: Path) -> None:
    adapter, _lark, _harness, _routes = make_adapter(tmp_path)

    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    sdk_event(
        capture,
        event_id="evt_img",
        message_id="om_image",
        content=json.dumps({"image_key": "img_v2_xyz"}),
    )

    assert outcome is not None and outcome.status == "invalid-message-content"
    letters = adapter.dead_letters()
    assert len(letters) == 1
    letter = letters[0]
    assert letter.message_id == "om_image"
    assert letter.reason == "invalid-message-content"
    assert letter.text == "[Unreadable Lark text message]"
    assert "img_v2_xyz" not in letter.text


def test_unknown_live_message_is_dead_lettered_without_raw_content(
    tmp_path: Path,
) -> None:
    adapter, _lark, _harness, _routes = make_adapter(tmp_path)
    outcome = None

    def capture(event):  # type: ignore[no-untyped-def]
        nonlocal outcome
        outcome = adapter.handle_sdk_event(event)

    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_future", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_sender"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_future",
                "create_time": "1720000001000",
                "chat_id": "oc_dm",
                "chat_type": "p2p",
                "message_type": "future_secret_type",
                "content": json.dumps({"access_token": "must-not-leak"}),
                "mentions": [],
            },
        },
    }
    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(capture)
        .build()
    )
    dispatcher._do_without_validation(json.dumps(payload).encode())

    assert outcome is not None
    assert outcome.status == "unsupported-message-type"
    letter = adapter.dead_letters()[0]
    assert letter.reason == "unsupported-message-type"
    assert letter.message_type == "unknown"
    assert "must-not-leak" not in repr(letter)


def test_untrusted_type_is_canonicalized_before_metadata_and_dead_letter(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, _routes = make_adapter(tmp_path, actor)
    raw_type = "bad\nAuthorization: Bearer must-not-persist"
    message = inbound_message(
        "om_bad_type",
        text="raw body must-not-forward",
        message_type=raw_type,
    )

    outcome = adapter.handle_inbound(message)

    assert outcome.status == "unsupported-message-type"
    assert harness.requests == []
    letter = adapter.dead_letters()[0]
    assert letter.message_type == "unknown"
    assert letter.text == "[Unsupported Lark message type]"
    assert "must-not-persist" not in repr(letter)
    assert "raw body" not in repr(letter)


def test_dead_letters_are_truncated_bounded_and_credential_free(
    tmp_path: Path,
) -> None:
    store = LarkStateStore(tmp_path / "lark-state.sqlite3")

    def letter(index: int, text: str = "body") -> DeadLetter:
        return DeadLetter(
            message_id=f"om_{index}",
            event_id=f"evt_{index}",
            chat_id="oc_dm",
            chat_type="p2p",
            conversation_id="lark:oc_dm:oc_dm",
            sender_id="ou_sender",
            sender_type="user",
            text=text,
            reason="forward-error",
            created_at="2024-01-01T00:00:00+00:00",
        )

    store.record_dead_letter(letter(0, text="x" * (DEAD_LETTER_TEXT_LIMIT * 3)))
    assert len(store.dead_letters()[0].text) == DEAD_LETTER_TEXT_LIMIT

    for index in range(1, MAX_DEAD_LETTERS + 50):
        store.record_dead_letter(letter(index))
    letters = store.dead_letters()
    assert len(letters) == MAX_DEAD_LETTERS
    # Oldest evicted; the newest records survive.
    assert {item.message_id for item in letters} >= {
        f"om_{MAX_DEAD_LETTERS + 49}",
        f"om_{MAX_DEAD_LETTERS + 48}",
    }
    assert "om_1" not in {item.message_id for item in letters}

    # The trail holds message content only — never app credentials.  SQLite
    # stores text uncompressed, so scanning the database (and its WAL, where
    # recent commits may still live) remains a meaningful leak check.
    state_path = tmp_path / "lark-state.sqlite3"
    raw = b"".join(
        candidate.read_bytes()
        for candidate in (
            state_path,
            state_path.with_name(state_path.name + "-wal"),
        )
        if candidate.exists()
    )
    assert b"secret" not in raw.lower()
    assert b"appSecret" not in raw


def test_repeated_failure_upserts_one_dead_letter(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The same lost message reprocessed must not grow the trail."""

    adapter, _lark, harness, _routes = make_adapter(tmp_path, actor)
    harness.fail = True

    message = inbound_message("om_lost", chat_type="p2p", text="deploy is red")
    first = adapter.handle_inbound(message)
    replay = adapter.handle_inbound(replace(message, event_id="recover:om_lost"))

    assert first.new_dead_letter is True
    assert replay.new_dead_letter is False
    assert len(adapter.dead_letters()) == 1


def test_failed_forward_then_recover_delivers_and_clears_dead_letter(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """End-to-end of the incident: delivery failed at event time; the body was
    preserved; an operator replays by native id once the daemon is back."""

    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    harness.fail = True
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_lost")
    assert [letter.message_id for letter in adapter.dead_letters()] == ["om_lost"]

    harness.fail = False
    lark.inbound["om_lost"] = inbound_message("om_lost", text="deploy is red")

    outcome = adapter.recover_message("om_lost")

    assert outcome.status == "forwarded"
    assert len(harness.requests) == 1
    # A reply-less send may already have committed: replay the frozen body,
    # even if a later platform read returns different text for the same id.
    assert harness.requests[0].text == "please investigate"
    assert adapter.dead_letters() == ()


def test_reconcile_suppresses_guidance_but_dead_letters(tmp_path: Path) -> None:
    adapter, lark, harness, _routes = make_adapter(tmp_path)
    # Prior failed delivery puts oc_dm in the scan set without a pin.
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_orphan")
    assert [letter.message_id for letter in adapter.dead_letters()] == ["om_orphan"]

    lark.history["oc_dm"] = (
        inbound_message("om_orphan", text="anyone there?", create_time="1720000002000"),
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.forwarded == 0
    assert report.dead_lettered == 0
    assert harness.requests == []
    # Only the original live-event guidance was sent; the sweep added none.
    assert len(lark.replies) == 1
    # Still one dead letter, refreshed in place rather than duplicated.
    assert [letter.message_id for letter in adapter.dead_letters()] == ["om_orphan"]


def test_existing_dead_letter_history_probe_stays_healthy_without_rebuild(
    tmp_path: Path,
) -> None:
    """Real adapter history must not rediscover one audit record forever."""

    adapter, lark, harness, _routes = make_adapter(tmp_path)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_orphan")
    lark.history["oc_dm"] = (
        inbound_message("om_orphan", create_time="1720000002000"),
    )
    now = [0.0]
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = LarkStreamHealthMonitor(
        name="cross-layer",
        stale_after=1.0,
        rest_probe=lambda: None,
        reconcile_idle=lambda: _reconcile_health_result(
            adapter.reconcile_recent(
                lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
            )
        ),
        rebuild=rebuilds.append,
        report=reports.append,
        monotonic=lambda: now[0],
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    health.connected()
    now[0] = 1.5
    health.transport_activity()
    now[0] = 2.0

    try:
        health.check_once()

        assert health.drain_telemetry()
        assert harness.requests == []
        assert rebuilds == []
        assert reports[-1]["streamHealth"] == "healthy"
        assert [item.message_id for item in adapter.dead_letters()] == ["om_orphan"]
    finally:
        # The monitor's dispatcher thread pins whatever its lambdas closed
        # over -- here the adapter and its sqlite fds (card #77).
        health.close()


def test_reconcile_dead_letters_unsupported_message_once(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_seed", message_id="om_seed")
    unsupported = inbound_message(
        "om_future",
        text="[Unsupported Lark message type]",
        message_type="unknown",
        content_status="unsupported",
        create_time=str(NOW_MS - 1000),
    )
    lark.history["oc_dm"] = (unsupported,)

    first = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS,
        now_ms=NOW_MS,
    )
    second = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS,
        now_ms=NOW_MS,
    )

    assert first.dead_lettered == 1
    assert first.retryable_errors == ()
    assert second.dead_lettered == 0
    assert second.retryable_errors == ()
    assert [item.reason for item in adapter.dead_letters()] == [
        "unsupported-message-type"
    ]
