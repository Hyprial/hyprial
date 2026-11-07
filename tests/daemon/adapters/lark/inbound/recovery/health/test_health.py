from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkAdapter, LarkApiError, LarkStateStore, ReconcileReport
from hyprial.daemon.impl.adapters.lark.runtime.health import LarkStreamHealthMonitor
from hyprial.daemon.impl.adapters.lark.worker.sweep import _reconcile_health_result
from hyprial.kernel import Logger

from ..support import (
    LOOKBACK_SECONDS,
    NOW_MS,
    RecordingLogger,
    _exploding_create_time,
    _exploding_recent_chats,
    _sweep_started_events,
    _sweep_with_history_error,
    actor as actor,
    inbound_message,
    make_adapter,
    sdk_event,
)


def test_reconcile_retires_permanently_gone_chat_and_stays_healthy(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A disbanded group (bot no longer a member) must not quarantine the
    whole adapter: the chat is retired out of the scan set, the sweep stays
    healthy, and the retirement survives a restart -- while the chat's audit
    rows are kept."""

    logger = Logger.adapter(tmp_path, name="test")
    adapter, lark, harness, routes = make_adapter(tmp_path, actor, logger=logger)
    routes.pins["lark:oc_two:oc_two"] = actor
    # Two chats with prior activity; oc_dm is the disbanded one.
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_two",
        chat_id="oc_two",
    )
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages",
        230002,
        "The bot can not be outside the group; "
        "tenant_access_token=SECRET-MUST-NOT-APPEAR",
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
    assert report.retryable_errors == ()
    assert report.retired_chats == ("oc_dm",)
    assert report.forwarded == 1
    assert harness.requests[-1].message_id == "om_missed_two"

    # The retired chat is out of the scan set: later sweeps never call it.
    second = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )
    assert second.retryable_errors == ()
    assert second.retired_chats == ()
    assert [call[0] for call in lark.list_calls].count("oc_dm") == 1

    # Retirement is durable state, not a process-local cache.
    reopened = LarkStateStore(tmp_path / "lark-state.sqlite3")
    assert reopened.retired_chat_code("oc_dm") == 230002
    assert reopened.recent_chats() == ("oc_two",)
    # Audit rows for the retired chat are deliberately kept.
    assert reopened.request("msg_harness_1") is not None

    # One visible event: adapter, chat, code -- and never the SDK's text.
    records = [
        json.loads(line)
        for line in logger.path.read_text(encoding="utf-8").splitlines()
    ]
    retired_events = [
        record
        for record in records
        if record["event"] == "lark.reconcile.chat-retired"
    ]
    assert len(retired_events) == 1
    assert retired_events[0]["level"] == "warn"
    assert retired_events[0]["chat_id"] == "oc_dm"
    assert retired_events[0]["code"] == 230002
    assert "adapter" in retired_events[0]
    assert "SECRET-MUST-NOT-APPEAR" not in json.dumps(records)


def test_permanently_gone_chat_keeps_cross_layer_probe_healthy(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """End to end through the health monitor: one disbanded chat plus one
    live chat must report healthy, with no rebuild."""

    adapter, lark, harness, routes = make_adapter(tmp_path, actor)
    routes.pins["lark:oc_two:oc_two"] = actor
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_2",
        message_id="om_two",
        chat_id="oc_two",
    )
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", 230002, "The bot can not be outside the group"
    )
    # The live chat has nothing new: a forwarded miss would legitimately
    # request an event-path rebuild, which is not what is under test here.
    lark.history["oc_two"] = ()
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
        assert rebuilds == []
        assert reports[-1]["streamHealth"] == "healthy"
        assert adapter._state.retired_chat_code("oc_dm") == 230002
    finally:
        health.close()


def test_all_chats_permanently_gone_stays_stale_until_restart(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """Retirement must not bless an adapter that reached nothing: a sweep
    whose every chat retired is still a total probe failure.  The restart it
    triggers then converges, because the scan set no longer names the chat."""

    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages", 230002, "The bot can not be outside the group"
    )

    report = adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )

    assert report.retired_chats == ("oc_dm",)
    with pytest.raises(RuntimeError):
        _reconcile_health_result(report)

    # After the supervised restart the retired chat is no longer scanned,
    # so an empty scan set is honestly healthy.
    reopened = LarkStateStore(tmp_path / "lark-state.sqlite3")
    assert reopened.recent_chats() == ()
    assert _reconcile_health_result(ReconcileReport(0, 0, 0, 0, 0)) == 0


@pytest.mark.parametrize("partial", [False, True], ids=["all-failed", "partial-failed"])
def test_reconcile_errors_make_history_probe_fail_closed_and_sanitized(
    tmp_path: Path, actor: ActorTarget, partial: bool
) -> None:
    adapter, lark, harness, routes = make_adapter(tmp_path, actor)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = RuntimeError(
        "https://tenant.example.invalid/history?tenant_access_token=secret-token"
    )
    if partial:
        routes.pins["lark:oc_two:oc_two"] = actor
        sdk_event(
            adapter.handle_sdk_event,
            event_id="evt_2",
            message_id="om_two",
            chat_id="oc_two",
        )
        lark.history["oc_two"] = (
            inbound_message(
                "om_partial_missed",
                chat_id="oc_two",
                create_time="1720000002000",
            ),
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
        assert rebuilds == ["history-probe-failed"]
        assert reports[-1]["streamHealth"] == "stale"
        assert "secret-token" not in str(reports[-1])
    finally:
        health.close()
    assert "tenant.example.invalid" not in str(reports[-1])
    if partial:
        assert harness.requests[-1].message_id == "om_partial_missed"


def test_a_blocked_chat_does_not_rebuild_the_stream(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The seam: per-chat isolation must survive the aggregate.

    ``reconcile_recent`` has always isolated a per-chat failure -- and the
    layer above turned any error into a rebuild, which for a missing scope
    could never succeed: rebuild, same rejection, rebuild, quarantine.  The
    adapter died of a condition no restart could fix, and every healthy chat
    died with it.

    The test above (its sibling) covers the other half and must keep passing:
    an *unclassified* failure still rebuilds.  Note its ``partial-failed``
    parameter covers "some chats succeeded", not "some failures are
    permanent" -- which is why the gap read as covered.
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
    lark.history_errors["oc_dm"] = LarkApiError(
        "list chat messages",
        99991672,
        "no permission",
        missing_scopes=("im:message.group_msg",),
    )
    # Deliberately no missed history anywhere: this test asks whether a
    # blocked chat *by itself* rebuilds the stream.  The first version of it
    # also planted a genuinely missed message in oc_two, which made
    # ``missed_messages > 0`` and rebuilt the stream for a correct reason
    # (health.py: reconciliation redrove a message, so the websocket path is
    # suspect).  The assertion below was therefore false no matter how the
    # adapter behaved -- it tested the fixture, not the change.  The sibling
    # test after this one keeps that legitimate rebuild covered.

    now = [0.0]
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = LarkStreamHealthMonitor(
        name="blocked-chat",
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
        assert rebuilds == []
    finally:
        health.close()


def test_a_missed_message_still_rebuilds_even_with_a_blocked_chat(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """The other half: a blocked chat must not *suppress* a real rebuild.

    ``missed_messages > 0`` means reconciliation redrove something the
    websocket should have delivered, so the subscription is rebuilt.  That is
    unrelated to permissions and must keep working -- otherwise a later change
    could make the sibling test above pass by disabling this path, which would
    look like finishing this PR and would actually lose the recovery.
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

    now = [0.0]
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = LarkStreamHealthMonitor(
        name="blocked-chat-with-missed-message",
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
        assert rebuilds == ["event-path-stale"]
        assert harness.requests[-1].message_id == "om_missed_two"
    finally:
        health.close()


@pytest.mark.parametrize(
    ("code", "why"),
    [
        (232011, "the bot was removed from the chat"),
        (232009, "the chat was dissolved"),
        (232006, "the chat_id is no longer valid"),
    ],
)
def test_a_chat_beyond_retry_does_not_quarantine_the_adapter(
    tmp_path: Path, actor: ActorTarget, code: int, why: str
) -> None:
    """⭐ The defect: every sweep fails identically, so the adapter dies.

    None of these clear by retrying, so failing the probe on them produces
    the restart loop a954c161 was written to remove -- and it takes every
    other chat down with it.
    """

    report = _sweep_with_history_error(
        tmp_path, actor, LarkApiError("list chat messages", code, "nope")
    )

    assert report.blocked_chats == (f"oc_dm: unreadable {code}",), why
    assert report.retryable_errors == ()
    # The claim that matters: the probe survives, so no rebuild is requested.
    assert _reconcile_health_result(report) == 0


def test_a_rate_limit_still_quarantines_rather_than_passing_silently(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """⛔ The negative control this change must not weaken.

    A rate limit is the same exception class with a different code, and it
    WILL clear.  Treating it as permanent would let a sweep that genuinely
    missed messages report healthy -- the failure fail-closed exists to
    prevent, and a strictly worse outcome than a restart.
    """

    report = _sweep_with_history_error(
        tmp_path, actor, LarkApiError("list chat messages", 99991400, "slow down")
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    assert report.blocked_chats == ()
    with pytest.raises(RuntimeError):
        _reconcile_health_result(report)


def test_an_unrecognised_code_fails_closed_but_says_which_code(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """⭐ Why the fix is not just a longer list of codes.

    The sweep calls the message-list endpoint, whose error table is not in
    the docs the constant was sourced from, so the list is certainly
    incomplete.  An unrecognised code therefore still fails closed -- but it
    now names itself, because otherwise the only way to discover that a code
    belongs in the permanent set is to watch an adapter quarantine itself
    with nothing recording the cause.
    """

    logger = RecordingLogger()
    report = _sweep_with_history_error(
        tmp_path,
        actor,
        LarkApiError("list chat messages", 7654321, "unheard of"),
        logger=logger,
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    with pytest.raises(RuntimeError):
        _reconcile_health_result(report)

    named = [
        fields
        for event, fields in logger.events
        if event == "adapter.reconcile.chat_unreadable"
    ]
    assert named, f"the cause must be recorded; saw {logger.events}"
    assert named[0]["platformCode"] == 7654321
    assert named[0]["chatId"] == "oc_dm"


def test_a_non_platform_failure_is_still_retryable_and_still_names_the_chat(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """A timeout is not a LarkApiError at all and must keep failing closed.

    It must still name the chat, and this is the harder half: there is no
    platform code on this path, so without the id the failure reaches the
    probe carrying nothing at all. The exception's own text stays out -- that
    is the thing the surrounding rule forbids -- so the id is all there is,
    which is exactly why it has to be emitted.
    """

    logger = RecordingLogger()
    report = _sweep_with_history_error(
        tmp_path, actor, TimeoutError("read timed out"), logger=logger
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    assert report.blocked_chats == ()

    named = [
        fields
        for event, fields in logger.events
        if event == "adapter.reconcile.chat_unreadable"
    ]
    assert named, f"the chat must be named even with no code; saw {logger.events}"
    assert named[0]["chatId"] == "oc_dm"
    assert named[0]["platformCode"] is None
    # The SDK text must not have travelled with it.
    assert not any("timed out" in str(v) for v in named[0].values())


def test_the_sweep_started_marker_reports_the_scan_set(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """§3.2: the marker carries the phase, and ``chatCount`` the scan set.

    ``chatCount`` also settles the open question of §7: how many chats this
    adapter's ``recent_chats()`` actually returns.
    """

    logger = Logger.adapter(tmp_path / "marker-state", name="lark-test")
    report = _sweep_with_history_error(
        tmp_path,
        actor,
        LarkApiError("list chat messages", 7654321, "unheard of"),
        logger=logger,
    )

    assert report.chats_scanned == 1
    started = _sweep_started_events(logger)
    assert [(item["chatCount"], item["node"]) for item in started] == [
        (1, "adapter-reconcile")
    ]


def test_the_sweep_started_count_separates_pre_loop_from_in_loop_failure(
    tmp_path: Path, actor: ActorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.2, both cells on one logger, read as a difference.

    Cell (ii) raises inside the loop but outside the per-chat ``try`` (the
    message-timestamp decode): the marker appears.  Cell (i) makes the scan
    set itself raise: the marker must not.  Sharing one logger keeps the
    instrument demonstrably live -- the second reading is a difference from a
    non-empty first reading, not an absent file that could mean anything.
    """

    logger = Logger.adapter(tmp_path / "phase-state", name="lark-test")

    # The monkeypatch below pins these stores (and their sqlite fds) until
    # after the fd-hygiene check, so release them on every path (card #77).
    in_loop, in_loop_lark, _harness_a, _routes_a = make_adapter(
        tmp_path, actor, logger=logger
    )
    pre_loop: LarkAdapter | None = None
    sdk_event(in_loop.handle_sdk_event, event_id="evt_1", message_id="om_one")
    in_loop_lark.history["oc_dm"] = (inbound_message("om_missed"),)
    monkeypatch.setattr(
        "hyprial.daemon.impl.adapters.lark.inbound.reconcile._create_time_ms", _exploding_create_time
    )
    try:
        with pytest.raises(TypeError):
            in_loop.reconcile_recent(
                lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
            )

        after_in_loop = _sweep_started_events(logger)
        assert [item["chatCount"] for item in after_in_loop] == [1], (
            "the loop was entered, so the marker must be there"
        )

        pre_loop, _pre_loop_lark, _harness_b, _routes_b = make_adapter(
            tmp_path, actor, logger=logger
        )
        monkeypatch.setattr(pre_loop._state, "recent_chats", _exploding_recent_chats)
        with pytest.raises(sqlite3.OperationalError):
            pre_loop.reconcile_recent(
                lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
            )

        assert _sweep_started_events(logger) == after_in_loop, (
            "recent_chats() raised before the loop, so the marker must not appear"
        )
    finally:
        in_loop._state.close()
        if pre_loop is not None:
            pre_loop._state.close()


def test_the_unreadable_chat_emit_reaches_the_real_logger(
    tmp_path: Path, actor: ActorTarget
) -> None:
    """⛔ The stub in this file defines ``warning``; the real Logger does not.

    ``RecordingLogger`` grew a ``warning`` method plus a catch-all
    ``__getattr__``, so every assertion on it passed while the production
    emit raised ``AttributeError`` inside ``_log_unreadable_chat``'s own
    ``except Exception: pass`` -- the chat id and the platform code never
    reached any log at all.  This test binds the emit to the real Logger, so
    a method name the real object does not have is red instead of silent.
    """

    logger = Logger.adapter(tmp_path / "state", name="lark-test")
    report = _sweep_with_history_error(
        tmp_path,
        actor,
        LarkApiError("list chat messages", 7654321, "unheard of"),
        logger=logger,
    )

    assert report.retryable_errors == ("oc_dm: history-unavailable",)
    records = [json.loads(line) for line in logger.path.read_text().splitlines()]
    named = [
        item
        for item in records
        if item.get("event") == "adapter.reconcile.chat_unreadable"
    ]
    assert named, (
        "the cause must reach the real logger; saw "
        f"{[item.get('event') for item in records]}"
    )
    assert named[0]["platformCode"] == 7654321
    assert named[0]["chatId"] == "oc_dm"
