"""Quota watchdog: one alert per transition per account window, durably."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import threading
import time
from pathlib import Path

import pytest

from hyprial.biz.impl.hooks.quota.policy import (
    FAILURE_HOLD_MS,
    FAILURE_REPORTED_TTL_MS,
    STATE_FILE_NAME,
    QuotaWatchdog,
)
from hyprial.biz.impl.usage import QuotaWindow, SourceSnapshot, UsageCache

NOW_MS = 1_787_043_000_000
RESET_A = NOW_MS + 3 * 3_600_000
RESET_B = RESET_A + 5 * 3_600_000


class Harness:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.now = NOW_MS
        self.readings: tuple[SourceSnapshot, ...] = ()
        self.sent: list[tuple[str, str]] = []
        self.accept = True

    def deliver(self, key: str, text: str) -> bool:
        self.sent.append((key, text))
        return self.accept

    def watchdog(self) -> QuotaWatchdog:
        return QuotaWatchdog(
            state_dir=self.state_dir,
            deliver=self.deliver,
            readings=lambda: self.readings,
            clock_ms=lambda: self.now,
        )

    def claude(self, used: float, resets_at_ms: int | None = RESET_A) -> None:
        self.readings = (
            _snapshot("claude", used, resets_at_ms),
            _snapshot("codex", 20.0, RESET_B),
        )


def _snapshot(source: str, used: float, resets_at_ms: int | None) -> SourceSnapshot:
    return SourceSnapshot(
        source,
        True,
        NOW_MS,
        windows=(
            QuotaWindow(
                id="five_hour",
                label="5h",
                used=used,
                limit=100.0,
                resets_at_ms=resets_at_ms,
            ),
        ),
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _kinds(harness: Harness) -> list[str]:
    return [key.rsplit(":", 1)[-1] for key, _ in harness.sent]


def test_warning_exhausted_and_recovered_are_each_said_once(harness: Harness) -> None:
    watchdog = harness.watchdog()
    for used in (50.0, 89.9):
        harness.claude(used)
        assert watchdog.observe_readings() == []
    harness.claude(90.0)
    watchdog.observe_readings()
    harness.claude(95.0)
    watchdog.observe_readings()
    harness.claude(100.0)
    watchdog.observe_readings()
    harness.claude(100.0)
    watchdog.observe_readings()
    harness.claude(10.0)
    watchdog.observe_readings()
    harness.claude(5.0)
    watchdog.observe_readings()

    assert _kinds(harness) == ["warning", "exhausted", "recovered"]
    assert [key for key, _ in harness.sent] == [
        f"quota-watchdog:claude/five_hour:{RESET_A}:warning",
        f"quota-watchdog:claude/five_hour:{RESET_A}:exhausted",
        f"quota-watchdog:claude/five_hour:{RESET_A}:recovered",
    ]


def test_flapping_around_the_line_inside_one_window_is_not_repeated(
    harness: Harness,
) -> None:
    """Each kind once per window: hovering at 90% must not page every refresh."""

    watchdog = harness.watchdog()
    for used in (91.0, 85.0, 92.0, 86.0, 93.0):
        harness.claude(used)
        watchdog.observe_readings()

    assert [key for key, _ in harness.sent] == [
        f"quota-watchdog:claude/five_hour:{RESET_A}:warning",
        f"quota-watchdog:claude/five_hour:{RESET_A}:recovered",
    ]


def test_straight_to_exhausted_names_the_reset_and_the_accounts_with_headroom(
    harness: Harness,
) -> None:
    harness.claude(100.0)
    [alert] = harness.watchdog().observe_readings()

    assert alert.kind == "exhausted"
    assert "claude 账号 5h 窗口已用 100%" in alert.text
    assert "重置" in alert.text and "重置时间未知" not in alert.text
    assert "还有余量的账号:codex" in alert.text


def test_no_headroom_is_said_plainly(harness: Harness) -> None:
    harness.readings = (
        _snapshot("claude", 100.0, RESET_A),
        _snapshot("codex", 95.0, RESET_B),
        SourceSnapshot("kimi", False, NOW_MS, reason="credentials missing"),
    )
    alerts = harness.watchdog().observe_readings()

    exhausted = next(alert for alert in alerts if alert.kind == "exhausted")
    assert "其他账号目前没有确认有余量的" in exhausted.text


def test_a_new_window_is_announced_again_even_without_a_low_reading(
    harness: Harness,
) -> None:
    watchdog = harness.watchdog()
    harness.claude(100.0, RESET_A)
    watchdog.observe_readings()
    # No refresh caught the reset's low point: the next reading is already
    # high, but it belongs to a different window.
    harness.claude(92.0, RESET_B)
    watchdog.observe_readings()

    assert [key for key, _ in harness.sent] == [
        f"quota-watchdog:claude/five_hour:{RESET_A}:exhausted",
        f"quota-watchdog:claude/five_hour:{RESET_B}:warning",
    ]


def test_a_reset_after_exhaustion_is_a_recovery(harness: Harness) -> None:
    watchdog = harness.watchdog()
    harness.claude(100.0, RESET_A)
    watchdog.observe_readings()
    harness.claude(3.0, RESET_B)
    watchdog.observe_readings()

    assert [key for key, _ in harness.sent] == [
        f"quota-watchdog:claude/five_hour:{RESET_A}:exhausted",
        f"quota-watchdog:claude/five_hour:{RESET_B}:recovered",
    ]


def test_a_daemon_restart_neither_repeats_nor_forgets(harness: Harness) -> None:
    harness.claude(100.0)
    harness.watchdog().observe_readings()
    assert _kinds(harness) == ["exhausted"]

    restarted = harness.watchdog()
    restarted.observe_readings()
    assert _kinds(harness) == ["exhausted"]

    harness.claude(1.0)
    restarted.observe_readings()
    assert _kinds(harness) == ["exhausted", "recovered"]
    state = json.loads((harness.state_dir / STATE_FILE_NAME).read_text())
    assert state["windows"]["claude/five_hour"]["level"] == "normal"


def test_an_undelivered_alert_is_retried_under_a_fresh_key(harness: Harness) -> None:
    """The Squire ledger records failures too: the same key would replay the
    failure, so a retry must not reuse it."""

    watchdog = harness.watchdog()
    harness.claude(100.0)
    harness.accept = False
    assert watchdog.observe_readings() == []
    harness.accept = True
    [alert] = watchdog.observe_readings()
    watchdog.observe_readings()

    base = f"quota-watchdog:claude/five_hour:{RESET_A}:exhausted"
    assert [key for key, _ in harness.sent] == [base, f"{base}:retry-1"]
    assert alert.kind == "exhausted"


def test_a_delivery_exception_is_contained_and_retried(harness: Harness) -> None:
    calls = {"n": 0}

    def flaky(key: str, text: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("squire down")
        return harness.deliver(key, text)

    watchdog = QuotaWatchdog(
        state_dir=harness.state_dir,
        deliver=flaky,
        readings=lambda: harness.readings,
        clock_ms=lambda: harness.now,
    )
    harness.claude(100.0)
    assert watchdog.observe_readings() == []
    assert len(watchdog.observe_readings()) == 1


def test_unreadable_readings_are_not_levels(harness: Harness) -> None:
    harness.readings = (SourceSnapshot("claude", False, NOW_MS, reason="network error"),)
    assert harness.watchdog().observe_readings() == []
    assert harness.sent == []


def test_first_usage_limit_turn_alerts_at_once_with_the_readings(harness: Harness) -> None:
    harness.claude(97.0)
    watchdog = harness.watchdog()
    alert = watchdog.observe_usage_limit_failure("agent:op:node:worker-a")

    assert alert is not None
    assert "agent:op:node:worker-a" in alert.text
    assert "PROVIDER_USAGE_LIMIT" in alert.text
    assert "- claude: 5h 97%" in alert.text
    assert "- codex: 5h 20%" in alert.text


def test_usage_limit_turns_during_a_hold_are_batched_and_not_repeated(
    harness: Harness,
) -> None:
    watchdog = harness.watchdog()
    assert watchdog.observe_usage_limit_failure("agent:op:node:a") is not None
    harness.now += 60_000
    assert watchdog.observe_usage_limit_failure("agent:op:node:b") is None
    assert watchdog.observe_usage_limit_failure("agent:op:node:a") is None
    assert watchdog.observe_usage_limit_failure("agent:op:node:c") is None
    assert watchdog.flush_failures() is None

    harness.now = NOW_MS + FAILURE_HOLD_MS
    held = watchdog.flush_failures()
    assert held is not None
    assert "agent:op:node:b" in held.text and "agent:op:node:c" in held.text
    assert "agent:op:node:a" not in held.text
    assert len(harness.sent) == 2

    # Still failing an hour later: already named, not named again...
    harness.now = NOW_MS + 60 * 60_000
    assert watchdog.observe_usage_limit_failure("agent:op:node:a") is None
    harness.now = NOW_MS + 90 * 60_000
    assert watchdog.flush_failures() is None
    # ...until the reported TTL has passed.
    harness.now = NOW_MS + FAILURE_REPORTED_TTL_MS
    again = watchdog.observe_usage_limit_failure("agent:op:node:a")
    assert again is not None and "agent:op:node:a" in again.text


def test_held_failures_survive_a_restart(harness: Harness) -> None:
    watchdog = harness.watchdog()
    watchdog.observe_usage_limit_failure("agent:op:node:a")
    harness.now += 60_000
    watchdog.observe_usage_limit_failure("agent:op:node:b")

    restarted = harness.watchdog()
    harness.now = NOW_MS + FAILURE_HOLD_MS
    held = restarted.flush_failures()
    assert held is not None and "agent:op:node:b" in held.text


def test_an_unreadable_state_file_refuses_to_start(harness: Harness) -> None:
    (harness.state_dir / STATE_FILE_NAME).write_text('{"schemaVersion": 99}', encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable quota watchdog state"):
        harness.watchdog()


def test_usage_cache_runs_the_observer_after_each_refresh_and_survives_its_failure(
    tmp_path: Path,
) -> None:
    refreshed = threading.Event()
    calls = {"n": 0}

    def observer() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            refreshed.set()
        raise RuntimeError("observer bug")

    cache = UsageCache(
        home=tmp_path,
        refresh_interval_seconds=0.01,
        fetcher=lambda *_args: {},
    )
    cache.set_refresh_observer(observer)
    cache.start()
    try:
        # A second call proves the first exception did not end the loop.
        assert refreshed.wait(5.0)
    finally:
        cache.stop()
    assert [snapshot.source for snapshot in cache.snapshots()] == ["claude", "codex", "kimi"]


def test_the_daemon_tells_its_owner_through_squire(tmp_path: Path) -> None:
    """Wiring: a daemon built with a usage cache owns a watchdog whose alerts
    are Squire user deliveries to the daemon owner.  Unified hooks H4: the
    watchdog is driven by bus events (usage.refreshed + usage-limit
    turn.failed), not by the two deleted callbacks."""

    from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.hooks import DaemonHookService
    from hyprial.daemon.impl.squire.addressing import UserDeliveryRequest, UserDeliveryResult
    from hyprial.kernel import HookBus
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    state_dir = tmp_path / "state"
    (state_dir / "logs" / "workers").mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    cache = UsageCache(home=tmp_path / "home", fetcher=lambda *_args: {})
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
        usage_cache=cache,
        zenoh_listen=("tcp/127.0.0.1:0",), zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    requests: list[UserDeliveryRequest] = []

    class _Squire:
        def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
            requests.append(request)
            return UserDeliveryResult(
                message_id=request.message_id,
                accepted=True,
                code=None,
                message=None,
            )

    def wait_for(predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert predicate()

    # The startup composition builds the bus before any refresh or turn can
    # publish; this test stands in for that seam with the same helper the
    # composition calls (pinned by test_the_composition_hands_both_signals_
    # to_the_bus below).
    from hyprial.daemon.impl.application.wiring.quota_hooks import wire_quota_hooks

    bus = HookBus()
    daemon_hooks = DaemonHookService(
        bus=bus,
        config_path_for_agent=lambda _actor: tmp_path / "absent" / "hooks.json",
        consumers={},
        handler_notifier=lambda *_args, **_kwargs: None,
    )
    app._hook_bus = bus
    app._daemon_hooks = daemon_hooks
    wire_quota_hooks(
        usage_cache=cache,
        watchdog=app._quota_watchdog,
        consumer_factory=app._quota_hook_consumer_factory,
        daemon_hooks=daemon_hooks,
    )

    try:
        # Wait for the asynchronous refusal before installing the Squire path.
        cache._snapshots = {"claude": _snapshot("claude", 100.0, RESET_A)}
        daemon_hooks.observe_usage_refreshed()
        wait_for(lambda: app._quota_watchdog.status().failed >= 1
                 and not app._quota_watchdog._active
                 and app._quota_watchdog._runtime.snapshot(app._quota_watchdog._handle).queued == 0
                 and app._quota_watchdog._runtime.snapshot(app._quota_watchdog._handle).in_flight == 0)
        assert requests == []
        outbox = json.loads(app._quota_watchdog._outbox.read_text())
        scope, attempt = outbox["key_scope"], outbox["attempt"]
        assert attempt >= 1

        app._user_delivery = _Squire()
        daemon_hooks.observe_usage_refreshed()
        agent = "agent:op:node-1:worker"
        daemon_hooks.observe_turn(
            HarnessDelivery("delivery-1", "conversation-1", "agent:op:node-1:sender", agent, "work"),
            HarnessResult(
                "delivery-1",
                agent,
                HarnessResultStatus.FAILED,
                error="provider said no",
                failure_code="PROVIDER_USAGE_LIMIT",
            ),
            started_at_ms=1,
            ended_at_ms=2,
        )
        wait_for(lambda: len(requests) == 2 and not app._quota_watchdog.status().pending)

        assert [(request.owner, request.sender) for request in requests] == [
            ("op", "quota-watchdog"), ("op", "quota-watchdog"),
        ]
        assert requests[0].idempotency_key == (
            f"quota-watchdog:claude/five_hour:{RESET_A}:exhausted:batch:{scope}:actor-retry-{attempt}"
        )
        assert agent in requests[1].message
        assert (state_dir / STATE_FILE_NAME).exists()
    finally:
        app._close()
        assert bus.close(5.0)


def test_the_composition_hands_both_signals_to_the_bus() -> None:
    """The one line that connects the quota signals is inside daemon startup
    composition, which no unit test builds.  Deleting it would make the
    watchdog silently deaf -- no error, every other test green -- so the
    source itself is pinned: startup calls wire_quota_hooks with the real
    daemon hook service and the app's watchdog pieces, that helper registers
    the quota consumer lane for the two design-§3 events, points the cache
    refresh observer at the bus publisher, and the event bridge is built
    without the old callback."""

    import ast

    application = (
        Path(__file__).resolve().parents[3] / 'src/hyprial/daemon/impl/application'
    )
    transport = ast.parse(
        (application / "wiring" / "transport.py").read_text(encoding="utf-8")
    )

    wire_calls = [
        node
        for node in ast.walk(transport)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "wire_quota_hooks"
    ]
    [wire] = wire_calls
    assert {
        keyword.arg: ast.unparse(keyword.value) for keyword in wire.keywords
    } == {
        "usage_cache": "self._usage_cache",
        "watchdog": "self._quota_watchdog",
        "consumer_factory": "self._quota_hook_consumer_factory",
        "daemon_hooks": "daemon_hooks",
    }

    quota_wiring = ast.parse(
        (application / "wiring" / "quota_hooks.py").read_text(encoding="utf-8")
    )
    register_calls = [
        node
        for node in ast.walk(quota_wiring)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_consumer"
    ]
    [register] = register_calls
    assert [ast.unparse(argument) for argument in register.args] == [
        "'quota'",
        "consumer_factory(watchdog)",
        "('turn.failed', 'usage.refreshed')",
    ]

    refresh_hooks = [
        ast.unparse(call.args[0])
        for tree in (
            ast.parse((application / "messaging" / "maintenance.py").read_text(encoding="utf-8")),
            quota_wiring,
        )
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "set_refresh_observer"
        and call.args
    ]
    assert refresh_hooks == ["daemon_hooks.observe_usage_refreshed"]

    bridge_calls = [
        node
        for node in ast.walk(transport)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DaemonEventBridge"
    ]
    [bridge] = bridge_calls
    assert "usage_limit_observer" not in [
        keyword.arg for keyword in bridge.keywords
    ], "DaemonEventBridge still receives the deleted usage_limit_observer callback"
