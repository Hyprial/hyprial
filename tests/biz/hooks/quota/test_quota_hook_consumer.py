"""biz/hooks/quota: the two bus events drive the watchdog exactly as the
deleted callbacks did (design §3, H4)."""

from __future__ import annotations

from pathlib import Path

from hyprial.biz.impl.hooks.quota import QUOTA_HOOK_EVENTS, QuotaHookConsumer
from hyprial.biz.impl.hooks.quota.policy import QuotaWatchdog
from hyprial.biz.impl.usage import QuotaWindow, SourceSnapshot
from hyprial.kernel import HookEvent, usage_refreshed_payload

NOW_MS = 1_787_043_000_000
RESET_A = NOW_MS + 3 * 3_600_000


def _readings(used: float) -> tuple[SourceSnapshot, ...]:
    return (
        SourceSnapshot(
            "claude",
            True,
            NOW_MS,
            windows=(
                QuotaWindow(
                    id="five_hour",
                    label="5h",
                    used=used,
                    limit=100.0,
                    resets_at_ms=RESET_A,
                ),
            ),
        ),
    )


class _RecordingWatchdog(QuotaWatchdog):
    """Same policy, same state file; records the observation order."""

    def __init__(self, state_dir: Path, used: float) -> None:
        self.calls: list[str] = []
        self.sent: list[str] = []
        self._used = used
        super().__init__(
            state_dir=state_dir,
            deliver=lambda key, text: self.sent.append(text) or True,
            readings=lambda: _readings(self._used),
            clock_ms=lambda: NOW_MS,
        )

    def observe_readings(self):
        self.calls.append("readings")
        return super().observe_readings()

    def observe_usage_limit_failure(self, actor):
        self.calls.append(f"failure:{actor}")
        return super().observe_usage_limit_failure(actor)

    def flush_failures(self):
        self.calls.append("flush")
        return super().flush_failures()


def _turn_failed(actor: str, failure_code: str | None) -> HookEvent:
    payload: dict[str, object] = {"failureCode": failure_code}
    return HookEvent(
        event="turn.failed",
        kind="observe",
        emitted_at_ms=NOW_MS,
        seq=1,
        source="daemon.turns",
        actor=actor,
        payload=payload,
    )


def _usage_refreshed() -> HookEvent:
    return HookEvent(
        event="usage.refreshed",
        kind="observe",
        emitted_at_ms=NOW_MS,
        seq=2,
        source="daemon.turns",
        actor=None,
        payload=usage_refreshed_payload(),
    )


def test_the_lane_subscribes_to_exactly_the_two_design_events() -> None:
    assert QUOTA_HOOK_EVENTS == ("turn.failed", "usage.refreshed")


def test_usage_refreshed_runs_readings_then_flush_in_the_callback_order(
    tmp_path: Path,
) -> None:
    watchdog = _RecordingWatchdog(tmp_path, 97.0)
    QuotaHookConsumer(watchdog).observe(_usage_refreshed())

    assert watchdog.calls == ["readings", "flush"]
    # The readings evaluation itself alerted (97% of the window).
    assert watchdog._state["windows"]["claude/five_hour"]["sent"] == ["warning"]


def test_only_usage_limit_turn_failures_reach_the_watchdog(tmp_path: Path) -> None:
    watchdog = _RecordingWatchdog(tmp_path, 10.0)
    consumer = QuotaHookConsumer(watchdog)
    agent = "agent:op:node:worker"

    consumer.observe(_turn_failed(agent, "PROVIDER_TRANSIENT_FAILURE"))
    consumer.observe(_turn_failed(agent, None))
    consumer.observe(_turn_failed(agent, "PROVIDER_USAGE_LIMIT"))
    consumer.observe(_turn_failed(agent, "PROVIDER_AUTHENTICATION_FAILED"))

    assert watchdog.calls == [f"failure:{agent}"]


def test_the_alert_names_the_failing_agent_with_the_current_readings(
    tmp_path: Path,
) -> None:
    """Same alert content as the deleted settlement callback produced."""

    watchdog = _RecordingWatchdog(tmp_path, 97.0)
    agent = "agent:op:node:worker-a"
    QuotaHookConsumer(watchdog).observe(_turn_failed(agent, "PROVIDER_USAGE_LIMIT"))

    [text] = watchdog.sent
    assert agent in text
    assert "PROVIDER_USAGE_LIMIT" in text
    assert "- claude: 5h 97%" in text


def test_turns_without_an_actor_are_ignored(tmp_path: Path) -> None:
    watchdog = _RecordingWatchdog(tmp_path, 10.0)
    QuotaHookConsumer(watchdog).observe(_turn_failed("", "PROVIDER_USAGE_LIMIT"))
    QuotaHookConsumer(watchdog).observe(
        HookEvent(
            event="turn.failed",
            kind="observe",
            emitted_at_ms=NOW_MS,
            seq=3,
            source="daemon.turns",
            actor=None,
            payload={"failureCode": "PROVIDER_USAGE_LIMIT"},
        )
    )
    assert watchdog.calls == []
