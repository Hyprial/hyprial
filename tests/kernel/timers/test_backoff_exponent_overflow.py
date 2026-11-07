"""Backoff arithmetic must stay finite for arbitrarily large retry counters.

Each retry loop below persists or accumulates its own counter with no upper
bound (or an operator-configurable one), while the outer ``min(cap, ...)``
discards the product.  Evaluating ``base * 2 ** (n - 1)`` directly raised
``OverflowError`` before the cap could be applied; these tests pin the value at
small ``n`` (unchanged), just past the cap, and at 10_000 (must be the cap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hyprial.biz.impl.usage as usage
from hyprial.kernel.impl.actor_runtime import AdmissionResult, SupervisionPolicy
from hyprial.kernel import capped_exponential
from hyprial.kernel.impl.contracts.ports import PortCommandRejected
from hyprial.daemon.impl.harnesses.actor.behaviors import desired as harness_actor
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.daemon.impl.desired_state_io import DesiredStateIoCompleted
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.contracts import _Record
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection
from hyprial.kernel import (
    MutationProvenance,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.harnesses.codex import settlement as codex_settlement
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import CarrierDeliverySnapshot
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess

BIG = 10_000


def _delivery(delivery_id: str = "delivery-1") -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:pi-worker",
        message="hello",
    )


def test_capped_exponential_is_finite_for_every_exponent() -> None:
    """Small exponents are unchanged; huge ones are the cap, not an error."""

    assert capped_exponential(0.5, 5.0, 0) == 0.5
    assert capped_exponential(0.5, 5.0, 1) == 1.0
    assert capped_exponential(0.5, 5.0, 3) == 4.0
    assert capped_exponential(0.5, 5.0, 4) == 5.0
    assert capped_exponential(0.5, 5.0, BIG) == 5.0
    # Negative exponents stay the plain product.
    assert capped_exponential(0.5, 5.0, -1) == 0.25
    # Degenerate bases/caps do not raise.
    assert capped_exponential(0.0, 5.0, BIG) == 0.0
    assert capped_exponential(0.5, 0.0, BIG) == 0.0
    # A subnormal base against a large cap still reaches the cap.
    assert capped_exponential(5e-324, 1e308, BIG) == 1e308


def test_supervision_policy_delay_clamps_exponent() -> None:
    """``policies.py``: attempt is bounded by max_restarts, but not by a constant."""

    policy = SupervisionPolicy(
        max_restarts=BIG,
        restart_window=60.0,
        base_backoff=0.1,
        max_backoff=2.0,
        jitter_ratio=0.0,
    )
    assert policy.delay(1, 0.5) == 0.1
    assert policy.delay(2, 0.5) == 0.2
    assert policy.delay(6, 0.5) == 2.0
    assert policy.delay(BIG, 0.5) == 2.0


def test_codex_settlement_backoff_clamps_exponent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``codex.py``: persisted settlement_attempts has no upper bound."""

    carrier = object.__new__(codex.CodexInteractiveCarrier)
    carrier.settlement_retry_seconds = 0.25
    facts: list[object] = []
    carrier._submit_carrier_fact = lambda command: facts.append(command) or True  # type: ignore[method-assign]
    carrier._log = lambda *args, **kwargs: None  # type: ignore[method-assign]
    monkeypatch.setattr(codex_settlement.time, "monotonic", lambda: 1000.0)

    def delay_for(attempts: int) -> float:
        state = CarrierDeliverySnapshot(
            generation=1,
            version=1,
            delivery=_delivery(),
            intent="reply",
            stage="final_observed",
            settlement_attempts=attempts - 1,
        )
        carrier._defer_settlement(state, method="message.settle_hint", error="boom")
        fact = facts[-1]
        return float(fact.next_settlement_at) - 1000.0  # type: ignore[attr-defined]

    assert delay_for(1) == 0.25
    assert delay_for(2) == 0.5
    assert delay_for(6) == 5.0
    assert delay_for(BIG) == 5.0


def test_streaming_reconnect_backoff_clamps_exponent() -> None:
    """``streaming.py``: repeated-failure and connect counters both grow unbounded."""

    process = object.__new__(StreamingTurnProcess)
    process._reconnect_delay_seconds = 0.25
    process._reconnect_delay_max_seconds = 30.0

    assert process._backoff_delay(1) == 0.25
    assert process._backoff_delay(2) == 0.5
    assert process._backoff_delay(8) == 30.0
    assert process._backoff_delay(BIG) == 30.0


def test_usage_refresh_backoff_clamps_exponent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``usage.py``: consecutive network failures increment without a bound."""

    for source in usage.SOURCES:
        monkeypatch.setattr(
            usage,
            f"collect_{source}",
            lambda *args, _source=source, **kwargs: usage.SourceSnapshot(
                _source, False, 0, reason=usage.REASON_NETWORK
            ),
        )
    cache = usage.UsageCache(
        home=tmp_path,
        fetcher=lambda *args, **kwargs: None,
        refresh_interval_seconds=120.0,
        clock=lambda: 1000.0,
    )
    now_ms = 1_000_000

    def delay_for(failures: int) -> float:
        cache._snapshots = {}
        cache._retry_after_ms = {}
        cache._failures = {"codex": failures - 1}
        cache.refresh_once()
        return (cache._retry_after_ms["codex"] - now_ms) / 1000.0

    assert delay_for(1) == 120.0
    assert delay_for(2) == 240.0
    assert delay_for(5) == 1800.0
    assert delay_for(BIG) == 1800.0


def test_harness_actor_restart_backoff_clamps_exponent() -> None:
    """``harness_actor.py``: budget 0 disables termination, so failures grow forever."""

    actor = object.__new__(HarnessRuntimeActor)
    actor._failure_budget = 0
    actor._restart_backoff_seconds = 1.0
    actor._start_backoff_max_seconds = 60.0
    actor._clock = lambda: 1000.0  # type: ignore[method-assign]
    actor._failed_events = []
    actor._desired_state = None

    def delay_for(failures: int) -> float:
        record = object.__new__(_Record)
        record.failures = failures - 1
        record.last_error = None
        record.restore_failed_terminal = False
        record.restart_after = None
        actor._record_failure("harness:probe", record, "boom")
        assert record.restart_after is not None
        return record.restart_after - 1000.0

    assert delay_for(1) == 1.0
    assert delay_for(2) == 2.0
    assert delay_for(7) == 60.0
    assert delay_for(BIG) == 60.0


def test_harness_actor_lifecycle_retry_backoff_clamps_exponent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``harness_actor.py``: the retry budget is operator-configurable, not fixed."""

    actor = object.__new__(HarnessRuntimeActor)
    actor._failure_budget = 100_000
    actor._closing = False
    actor._lifecycle_retry_base_seconds = 0.05
    actor._lifecycle_retry_timers = {}
    actor._lifecycle_pending = {}
    actor._lifecycle_native_admitted = {}
    actor._handler_generation = 1
    actor._version = 1
    attempts = {"value": 1}

    class _Persistence:
        request = None

        def submit(self, request):  # type: ignore[no-untyped-def]
            self.request = request
            return AdmissionResult.ACCEPTED

    persistence = _Persistence()
    actor._persistence = persistence

    class _Timer:
        def __init__(self, interval: float, function: object) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False

        def cancel(self) -> None:
            pass

        def start(self) -> None:
            pass

    timers: list[_Timer] = []

    def _timer(interval: float, function: object) -> _Timer:
        timers.append(_Timer(interval, function))
        return timers[-1]

    monkeypatch.setattr(harness_actor.threading, "Timer", _timer)

    def delay_for(count: int) -> float:
        correlation_id = f"corr-{count}"
        request = LifecycleMutationRequest(
            correlation_id,
            "attempt-token",
            "operation-id",
            None,
            EnsureHarnessCommand(
                correlation_id + ":ensure",
                HarnessLaunchProjection("codex", "probe", True),
            ),
        )
        provenance = MutationProvenance(True, True, "resource-token")
        actor._lifecycle_pending[correlation_id] = (request, provenance)
        attempts["value"] = count
        actor._emit_event(
            PortCommandRejected(correlation_id, "harness", 1, 1, "CODE", "detail")
        )
        assert persistence.request is not None
        actor._on_desired_completed(
            DesiredStateIoCompleted(
                persistence.request, attempts["value"], DesiredState()
            )
        )
        return timers[-1].interval

    assert delay_for(1) == 0.05
    assert delay_for(2) == 0.1
    assert delay_for(6) == 1.0
    assert delay_for(BIG) == 1.0
