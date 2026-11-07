import json
import threading
import time

from hyprial.daemon.impl.inbox.tracking.watchdog import UNFETCHED_ALERT_MS
from hyprial.daemon.impl.operations.watchdog_actor import AsyncInboxWatchdog, AsyncQuotaWatchdog, AlertDeliveryOutcome
from hyprial.biz.impl.usage import SourceSnapshot, QuotaWindow
from hyprial.daemon import QuotaWatchdogDeps
from hyprial.shell.impl.daemon_wiring.quota import quota_evaluator_factory


def _wait(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_slow_alert_is_off_tick_and_state_commits_after_delivery(tmp_path):
    entered, release = threading.Event(), threading.Event()
    delivered = []

    def deliver(key, text):
        entered.set()
        assert release.wait(3)
        delivered.append(key)
        return True

    actor = AsyncInboxWatchdog(
        state_dir=tmp_path, deliver=deliver, clock_ms=lambda: UNFETCHED_ALERT_MS + 1
    )
    try:
        assert (
            actor.observe_unfetched((("actor", 1, 0),), is_running=lambda _: True) == []
        )
        assert entered.wait(1)
        assert (tmp_path / "inbox-watchdog.outbox.json").exists()
        scope = json.loads(actor._outbox.read_text())["key_scope"]
        assert not (tmp_path / "inbox-watchdog.json").exists()
        assert not actor.close(0.01)
    finally:
        release.set()
        assert actor.close(2)
    assert delivered == [f"unfetched:actor:batch:{scope}"]
    assert (
        json.loads((tmp_path / "inbox-watchdog.json").read_text())["reported"][
            "unfetched:actor"
        ]
        == UNFETCHED_ALERT_MS + 1
    )


def test_failed_delivery_retains_durable_batch_and_deferred_loss_event(tmp_path):
    allow = threading.Event()
    calls = []

    def deliver(key, text):
        calls.append(key)
        return allow.is_set()

    actor = AsyncInboxWatchdog(
        state_dir=tmp_path, deliver=deliver, clock_ms=lambda: UNFETCHED_ALERT_MS + 1
    )
    try:
        actor.observe_pruned((("actor", "message1"),), is_running=lambda _: True)
        _wait(lambda: actor.status().failed == 1)
        assert not (tmp_path / "inbox-watchdog.json").exists()
        actor.observe_pruned((("actor", "message2"),), is_running=lambda _: True)
        _wait(lambda: actor.status().failed == 2)
        allow.set()
    finally:
        allow.set()
        assert actor.close(2)
    assert not (tmp_path / "inbox-watchdog.outbox.json").exists()
    assert len(calls) == 4  # two refusals, then both admitted loss observations


def test_restart_replays_durable_batch_before_new_observation(tmp_path):
    actor = AsyncInboxWatchdog(
        state_dir=tmp_path,
        deliver=lambda key, text: AlertDeliveryOutcome(False, definitely_not_sent=True),
        clock_ms=lambda: UNFETCHED_ALERT_MS + 1,
    )
    actor.observe_pruned((("actor", "message"),), is_running=lambda _: True)
    _wait(lambda: actor.status().failed == 1)
    scope = json.loads(actor._outbox.read_text())["key_scope"]
    # Simulate process loss after persisted refusal, before another cadence.
    assert actor._effects.close(1)
    assert actor._runtime.stop(actor._handle, 1)
    delivered = []
    recovered = AsyncInboxWatchdog(
        state_dir=tmp_path,
        deliver=lambda key, text: delivered.append(key) or True,
        clock_ms=lambda: UNFETCHED_ALERT_MS + 1,
    )
    recovered.observe_unfetched((), is_running=lambda _: False)
    assert recovered.close(2)
    assert delivered == [f"pruned:actor:batch:{scope}:actor-retry-1"]


def test_quota_transition_commits_once_after_delivery(tmp_path):
    sent = []
    snapshot = SourceSnapshot(
        "claude", True, 1, windows=(QuotaWindow("window", "hour", 95, 100, 10000),)
    )
    def deliver(key, text):
        sent.append(key)
        return True

    def readings():
        return (snapshot,)

    def clock_ms():
        return 1

    evaluator = quota_evaluator_factory(QuotaWatchdogDeps(
        state_dir=tmp_path, deliver=deliver, readings=readings,
        clock_ms=clock_ms, on_alert=lambda _alert: None,
    ))
    actor = AsyncQuotaWatchdog(
        state_dir=tmp_path,
        deliver=deliver,
        readings=readings,
        clock_ms=clock_ms,
        evaluator=evaluator,
    )
    try:
        actor.observe_readings()
        actor.observe_readings()
        actor.flush_failures()
    finally:
        assert actor.close(2)
    assert len(sent) == 1
    state = json.loads((tmp_path / "quota-watchdog.json").read_text())
    assert state["windows"]["claude/window"]["sent"]


def test_uncertain_receipt_retry_preserves_native_identity(tmp_path):
    delivered = set()
    calls = []

    def deliver(key, text):
        calls.append(key)
        if len(calls) == 1:
            # Native delivery succeeded; its receipt missed the caller deadline.
            delivered.add(key)
            return AlertDeliveryOutcome(False)
        assert key in delivered
        return AlertDeliveryOutcome(True)

    actor = AsyncInboxWatchdog(
        state_dir=tmp_path, deliver=deliver, clock_ms=lambda: UNFETCHED_ALERT_MS + 1
    )
    try:
        actor.observe_pruned((("actor", "message"),), is_running=lambda _: True)
        _wait(lambda: actor.status().failed == 1)
        raw = json.loads(actor._outbox.read_text())
        assert raw["attempt"] == 0
        scope = raw["key_scope"]
    finally:
        assert actor.close(2)
    assert calls == [f"pruned:actor:batch:{scope}"] * 2
    assert len(delivered) == 1


def test_old_failed_completion_cannot_acknowledge_new_alert_attempt(tmp_path):
    from hyprial.kernel.impl.actor_runtime import AdmissionResult
    from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
    from hyprial.daemon.impl.operations.watchdog_actor import RetryAlerts

    entered, release = threading.Event(), threading.Event()
    calls = []

    def deliver(key, text):
        calls.append(key)
        if len(calls) == 1:
            return AlertDeliveryOutcome(False)
        entered.set()
        assert release.wait(3)
        return AlertDeliveryOutcome(True)

    actor = AsyncInboxWatchdog(
        state_dir=tmp_path, deliver=deliver, clock_ms=lambda: UNFETCHED_ALERT_MS + 1
    )
    try:
        actor.observe_pruned((("actor", "message"),), is_running=lambda _: True)
        _wait(lambda: actor.status().failed == 1)
        scope = json.loads(actor._outbox.read_text())["key_scope"]
        previous = EffectCompleted(actor._pending.operation_id, actor._generation, False)
        assert actor._runtime.tell(actor._handle, RetryAlerts()) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert actor._runtime.tell(actor._handle, previous) is AdmissionResult.ACCEPTED
        _wait(lambda: actor._runtime.snapshot(actor._handle).queued == 0
              and actor._runtime.snapshot(actor._handle).in_flight == 0)
        assert actor._effects.snapshot().outstanding == 1
        assert actor._active
        assert actor.status().failed == 1
        assert calls == [f"pruned:actor:batch:{scope}"] * 2
    finally:
        release.set()
        assert actor.close(2)


def test_new_alert_after_cooldown_has_a_distinct_durable_identity(tmp_path):
    from hyprial.daemon.impl.inbox.tracking.watchdog import REPEAT_AFTER_MS

    now = [UNFETCHED_ALERT_MS + 1]
    deliveries = []
    actor = AsyncInboxWatchdog(
        state_dir=tmp_path,
        deliver=lambda key, text: deliveries.append(key) or True,
        clock_ms=lambda: now[0],
    )
    try:
        actor.observe_unfetched((("actor", 1, 0),), is_running=lambda _: True)
        _wait(lambda: len(deliveries) == 1 and not actor.status().pending)
        now[0] += REPEAT_AFTER_MS + 1
        actor.observe_unfetched((("actor", 1, 0),), is_running=lambda _: True)
        _wait(lambda: len(deliveries) == 2 and not actor.status().pending)
        assert deliveries[0] != deliveries[1]
    finally:
        assert actor.close(2)


def test_legacy_outbox_replay_preserves_pre_upgrade_identity(tmp_path):
    path = tmp_path / "inbox-watchdog.outbox.json"
    path.write_text(json.dumps({
        "operation_id": "old-batch", "after_state": "{}",
        "deliveries": [["legacy-key", "text"]], "completed": 0, "attempt": 0,
    }))
    sent = []
    actor = AsyncInboxWatchdog(
        state_dir=tmp_path, deliver=lambda key, text: sent.append(key) or True,
        clock_ms=lambda: 1,
    )
    assert actor.close(2)
    assert sent == ["legacy-key"]
