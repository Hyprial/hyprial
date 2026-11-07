from dataclasses import replace
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from hyprial.daemon.impl.adapters.lark import LarkHistoryBatch

from hyprial.daemon.impl.adapters.lark import LarkAdapter, LarkStateStore
from hyprial.kernel import Logger

from ..support import NOW_MS, actor as actor, inbound_message, make_adapter


def restart(adapter, lark, harness, routes, now_ms, logger=None):
    path = adapter._state.path
    adapter.close()
    return LarkAdapter(
        state=LarkStateStore(path), lark=lark, harness=harness, routes=routes,
        channel_actor_id=adapter._channel_actor_id, logger=logger,
        utcnow=lambda: datetime.fromtimestamp(now_ms / 1000, UTC),
        org_context_path=path.parent / "org-context.md",
    )


def prepare(tmp_path, actor, *, gap=59 * 60_000, logger=None):
    before = NOW_MS - gap
    adapter, lark, harness, routes = make_adapter(
        tmp_path, actor, utcnow=lambda: datetime.fromtimestamp(before / 1000, UTC)
    )
    assert adapter.handle_inbound(inbound_message(
        "before", chat_type="p2p", create_time=str(before)
    )).status == "forwarded"
    lark.history["oc_dm"] = tuple(
        inbound_message(name, create_time=str(before + offset))
        for name, offset in (("third", 180_000), ("first", 60_000), ("second", 120_000))
    )
    adapter = restart(adapter, lark, harness, routes, NOW_MS, logger)
    return adapter, lark, harness


def test_cold_start_backfills_entire_59_minute_gap_in_create_time_order(tmp_path, actor):
    adapter, lark, harness = prepare(tmp_path, actor)
    try:
        report = adapter.reconcile_recent(now_ms=NOW_MS)
        assert report.forwarded == 3
        assert [r.message_id for r in harness.requests] == ["before", "first", "second", "third"]
        assert int(lark.list_calls[0][1]) <= (NOW_MS - 59 * 60_000) // 1000
        assert adapter.reconcile_recent(now_ms=NOW_MS).forwarded == 0
    finally:
        adapter.close()


def test_live_event_cannot_overtake_cold_start_backfill(tmp_path, actor):
    adapter, _lark, harness = prepare(tmp_path, actor)
    try:
        assert adapter.handle_inbound(inbound_message(
            "live", chat_type="p2p", create_time=str(NOW_MS)
        )).status == "forwarded"
        assert [r.message_id for r in harness.requests] == [
            "before", "first", "second", "third", "live"
        ]
    finally:
        adapter.close()


def test_reconnect_fences_live_before_sweep_is_scheduled(tmp_path, actor):
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    adapter._utcnow = lambda: datetime.fromtimestamp((NOW_MS - 59 * 60_000) / 1000, UTC)
    adapter.handle_inbound(inbound_message("before", chat_type="p2p", create_time=str(NOW_MS - 59 * 60_000)))
    adapter._utcnow = lambda: datetime.fromtimestamp(NOW_MS / 1000, UTC)
    lark.history["oc_dm"] = (inbound_message("outage", create_time=str(NOW_MS - 58 * 60_000)),)
    adapter.begin_backfill()
    try:
        adapter.handle_inbound(inbound_message("live", chat_type="p2p", create_time=str(NOW_MS)))
        assert [r.message_id for r in harness.requests] == ["before", "outage", "live"]
    finally:
        adapter.close()


def test_live_waits_for_concurrent_history_sweep(tmp_path, actor):
    adapter, lark, harness = prepare(tmp_path, actor)
    entered, release = threading.Event(), threading.Event()
    original = lark.list_chat_messages

    def history(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    lark.list_chat_messages = history
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sweep = pool.submit(adapter.reconcile_recent, now_ms=NOW_MS)
            assert entered.wait(3)
            live = pool.submit(adapter.handle_inbound, inbound_message("live", chat_type="p2p", create_time=str(NOW_MS)))
            assert [r.message_id for r in harness.requests] == ["before"]
            release.set()
            assert sweep.result(timeout=5).forwarded == 3
            assert live.result(timeout=5).status == "forwarded"
        assert [r.message_id for r in harness.requests] == ["before", "first", "second", "third", "live"]
    finally:
        release.set()
        adapter.close()


def test_incomplete_history_holds_live_and_does_not_advance_checkpoint(tmp_path, actor):
    adapter, lark, harness = prepare(tmp_path, actor)
    lark.history["oc_dm"] = LarkHistoryBatch(lark.history["oc_dm"], complete=False)
    try:
        before = adapter._state.chat_observed_ms("oc_dm")
        outcome = adapter.handle_inbound(inbound_message("live", chat_type="p2p", create_time=str(NOW_MS)))
        assert outcome.ack_error == "backfill-unavailable"
        assert [r.message_id for r in harness.requests] == ["before"]
        assert adapter._state.chat_observed_ms("oc_dm") == before
        assert adapter.dead_letters()[0].message_id == "live"
    finally:
        adapter.close()


def test_equal_timestamp_reply_follows_parent(tmp_path, actor):
    adapter, lark, harness = prepare(tmp_path, actor)
    parent = inbound_message("z_parent", create_time=str(NOW_MS - 1000))
    child = replace(parent, message_id="a_child", event_id="child", reply_to="z_parent")
    lark.history["oc_dm"] = (child, parent)
    try:
        assert adapter.reconcile_recent(now_ms=NOW_MS).forwarded == 2
        assert [r.message_id for r in harness.requests] == ["before", "z_parent", "a_child"]
    finally:
        adapter.close()


def test_frozen_submission_joins_history_order_after_restart(tmp_path, actor):
    before = NOW_MS - 59 * 60_000
    adapter, lark, harness, routes = make_adapter(
        tmp_path, actor, utcnow=lambda: datetime.fromtimestamp(before / 1000, UTC)
    )
    adapter.handle_inbound(inbound_message("before", chat_type="p2p", create_time=str(before)))
    original = harness.send_request

    def crash(_request, **_kwargs):
        raise KeyboardInterrupt("isolated freeze-before-IPC crash")

    harness.send_request = crash
    third = inbound_message("third", chat_type="p2p", create_time=str(before + 180_000))
    assert adapter.handle_inbound(third).status == "error"
    assert adapter._state.pending_inbound_submission("third") is not None
    harness.send_request = original
    lark.history["oc_dm"] = (
        third,
        inbound_message("second", create_time=str(before + 120_000)),
        inbound_message("first", create_time=str(before + 60_000)),
    )
    adapter = restart(adapter, lark, harness, routes, NOW_MS)
    try:
        adapter.handle_inbound(inbound_message("live", chat_type="p2p", create_time=str(NOW_MS)))
        assert [r.message_id for r in harness.requests] == ["before", "first", "second", "third", "live"]
        assert adapter._state.pending_inbound_submission("third") is None
    finally:
        adapter.close()


def test_max_lookback_emits_truncation_and_sweep_counts(tmp_path, actor):
    import json

    logger = Logger(tmp_path / "logs", component="test", name="backfill")
    adapter, lark, harness = prepare(tmp_path, actor, gap=25 * 3600_000, logger=logger)
    lark.history["oc_dm"] += (inbound_message("recent", create_time=str(NOW_MS - 1000)),)
    try:
        report = adapter.reconcile_recent(now_ms=NOW_MS)
        assert report.forwarded == 1
        assert int(lark.list_calls[0][1]) == (NOW_MS - 24 * 3600_000) // 1000
        events = [json.loads(line) for line in adapter._logger.path.read_text().splitlines()]
        truncation = next(row for row in events if row.get("event") == "lark.backfill.truncated")
        assert truncation["observedMs"] == NOW_MS - 25 * 3600_000
        assert truncation["windowStartMs"] == NOW_MS - 24 * 3600_000
        assert truncation["maxLookbackSeconds"] == 86400
        sweep = next(row for row in events if row.get("event") == "lark.backfill.sweep")
        assert {key: sweep[key] for key in ("chatsScanned", "forwarded", "duplicates", "truncated")} == {
            "chatsScanned": 1, "forwarded": 1, "duplicates": 0, "truncated": 1,
        }
        assert sweep["windowEndMs"] == NOW_MS
        assert "oc_dm" not in json.dumps([truncation, sweep])
        assert "recovered body" not in json.dumps([truncation, sweep])
        assert adapter.reconcile_recent(now_ms=NOW_MS).duplicates == 1
        assert adapter._state.chat_observed_ms("oc_dm") == NOW_MS
        assert [r.message_id for r in harness.requests] == ["before", "recent"]
    finally:
        adapter.close()


def test_hung_history_holds_only_that_chat_and_other_chats_keep_flowing(
    tmp_path, actor,
):
    adapter, lark, harness, routes = make_adapter(
        tmp_path, actor, inline_backfill_budget=1.0,
    )
    routes.pins["lark:oc_chat2:oc_chat2"] = actor
    assert adapter.handle_inbound(inbound_message(
        "before", chat_type="p2p", create_time=str(NOW_MS - 60_000)
    )).status == "forwarded"
    adapter.begin_backfill()
    entered = threading.Event()
    original = lark.list_chat_messages

    def hanging_history(chat_id, *args, **kwargs):  # type: ignore[no-untyped-def]
        if chat_id == "oc_dm":
            entered.set()
            threading.Event().wait()
        return original(chat_id, *args, **kwargs)

    lark.list_chat_messages = hanging_history
    observed_before = adapter._state.chat_observed_ms("oc_dm")
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            dm = pool.submit(adapter.handle_inbound, inbound_message(
                "dm_live", chat_type="p2p", create_time=str(NOW_MS)))
            assert entered.wait(5)
            other = pool.submit(adapter.handle_inbound, inbound_message(
                "c2_live", chat_id="oc_chat2", chat_type="p2p", create_time=str(NOW_MS)))
            again = pool.submit(adapter.handle_inbound, inbound_message(
                "dm_live2", chat_type="p2p", create_time=str(NOW_MS)))
            assert dm.result(timeout=20).ack_error == "backfill-unavailable"
            assert other.result(timeout=20).status == "forwarded"
            assert again.result(timeout=20).ack_error == "backfill-unavailable"
        assert [r.message_id for r in harness.requests] == ["before", "c2_live"]
        held = {letter.message_id for letter in adapter.dead_letters()}
        assert {"dm_live", "dm_live2"} <= held
        assert adapter._state.chat_observed_ms("oc_dm") == observed_before
        assert adapter._backfilled_chats.get("oc_dm") != adapter._backfill_epoch
    finally:
        adapter.close()


def test_paused_chat_resumes_in_order_after_backfill_completes(
    tmp_path, actor,
):
    adapter, lark, harness, _routes = make_adapter(
        tmp_path, actor, inline_backfill_budget=1.0,
    )
    adapter._utcnow = lambda: datetime.fromtimestamp(NOW_MS / 1000, UTC)
    release = threading.Event()
    original = lark.list_chat_messages

    def slow_history(chat_id, *args, **kwargs):  # type: ignore[no-untyped-def]
        release.wait(30)
        return original(chat_id, *args, **kwargs)

    lark.list_chat_messages = slow_history
    lark.history["oc_dm"] = (
        inbound_message("first", chat_type="p2p", create_time=str(NOW_MS - 180_000)),
        inbound_message("second", chat_type="p2p", create_time=str(NOW_MS - 120_000)),
    )
    try:
        assert adapter.handle_inbound(inbound_message(
            "before", chat_type="p2p", create_time=str(NOW_MS - 400_000)
        )).status == "forwarded"
        adapter.begin_backfill()
        outcome = adapter.handle_inbound(inbound_message(
            "dm_live", chat_type="p2p", create_time=str(NOW_MS)
        ))
        assert outcome.ack_error == "backfill-unavailable"
        assert [r.message_id for r in harness.requests] == ["before"]
        assert adapter.dead_letters()[0].message_id == "dm_live"
        release.set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if [r.message_id for r in harness.requests] == [
                "before", "first", "second", "dm_live",
            ]:
                break
            time.sleep(0.02)
        assert [r.message_id for r in harness.requests] == [
            "before", "first", "second", "dm_live",
        ]
        assert adapter._backfilled_chats.get("oc_dm") == adapter._backfill_epoch
        assert adapter.dead_letters() == ()
        assert adapter._state.chat_observed_ms("oc_dm") == NOW_MS
        redelivered = adapter.handle_inbound(inbound_message(
            "dm_live", chat_type="p2p", create_time=str(NOW_MS)
        ))
        assert redelivered.status == "duplicate"
        assert [r.message_id for r in harness.requests] == [
            "before", "first", "second", "dm_live",
        ]
    finally:
        release.set()
        adapter.close()


def test_sweep_window_anchors_to_last_reconciled_point_not_live_watermark(tmp_path, actor):
    adapter, lark, harness, _routes = make_adapter(tmp_path, actor)
    t = NOW_MS

    def at(ms: int) -> None:
        adapter._utcnow = lambda: datetime.fromtimestamp(ms / 1000, UTC)

    at(t - 70 * 60_000)
    assert adapter.handle_inbound(inbound_message(
        "before", chat_type="p2p", create_time=str(t - 70 * 60_000)
    )).status == "forwarded"
    adapter.begin_backfill()
    at(t - 65 * 60_000)
    assert adapter.reconcile_recent(now_ms=t - 65 * 60_000).forwarded == 0
    lark.history["oc_dm"] = (
        inbound_message("gap", chat_type="p2p", create_time=str(t - 40 * 60_000)),
    )
    at(t - 35 * 60_000)
    adapter.handle_inbound(inbound_message(
        "watermark", chat_type="p2p", create_time=str(t - 35 * 60_000)))
    assert [r.message_id for r in harness.requests] == ["before", "watermark"]
    try:
        at(t)
        report = adapter.reconcile_recent(now_ms=t)
        assert report.forwarded == 1
        assert [r.message_id for r in harness.requests] == [
            "before", "watermark", "gap",
        ]
    finally:
        adapter.close()
