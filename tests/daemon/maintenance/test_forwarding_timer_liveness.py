"""Instrumentation: the forwarding timer's low-rate liveness beat.

`forwarding.timer.live` proves the forwarding timer is alive even when nothing
changes, and the same beat re-emits the last `org.dial_direction.resolved`
answers so a quiet but healthy pair is visible in the round products.  This
unit test pins the low rate and the payload; it exercises no dialing.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.application.messaging.instrumentation import (
    orgfs_announce as instrumentation_module,
)
from hyprial.daemon.impl.application.messaging.maintenance import _MaintenanceMixin


class _DialState:
    def __init__(self) -> None:
        self.answers: dict[str, tuple[str, str]] = {
            "bob/host": ("accept", "member"),
            "ann/host": ("dial", "ordered-before"),
        }


class _Ticker(_MaintenanceMixin):
    def __init__(self, *, enabled: bool = False) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        self._org_dial_state = _DialState()
        self._trace_presence_enabled = enabled

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, component, event, fields))


def test_forwarding_timer_liveness_is_low_rate_and_repeats_dial_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(instrumentation_module.time, "monotonic", lambda: clock[0])
    ticker = _Ticker(enabled=True)

    for _ in range(10):
        ticker._note_forwarding_timer_tick()

    beats = [entry for entry in ticker.events if entry[2] == "forwarding.timer.live"]
    assert len(beats) == 1  # ten ticks inside one interval -> exactly one beat
    level, component, _event, fields = beats[0]
    assert (level, component) == ("info", "zenoh")
    assert fields["ticks"] == 1  # the beat fires on the first tick of the interval

    steady = [
        entry
        for entry in ticker.events
        if entry[2] == "org.dial_direction.resolved"
    ]
    assert sorted((entry[3]["peer"], entry[3]["direction"]) for entry in steady) == [
        ("ann/host", "dial"),
        ("bob/host", "accept"),
    ]
    assert all(entry[3]["steadyState"] is True for entry in steady)
    assert all(entry[1] == "org" for entry in steady)

    clock[0] += 61.0
    ticker._note_forwarding_timer_tick()
    beats = [entry for entry in ticker.events if entry[2] == "forwarding.timer.live"]
    assert len(beats) == 2
    assert beats[1][3]["ticks"] == 11
    assert beats[1][3]["intervalMs"] == 61000


def test_forwarding_timer_beat_needs_no_dial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The beat is liveness for the timer, so it must survive a missing state."""

    clock = [5.0]
    monkeypatch.setattr(instrumentation_module.time, "monotonic", lambda: clock[0])
    ticker = _Ticker(enabled=True)
    del ticker._org_dial_state

    ticker._note_forwarding_timer_tick()

    assert [entry[2] for entry in ticker.events] == ["forwarding.timer.live"]


def test_forwarding_timer_instrumentation_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instrumentation_module.time, "monotonic", lambda: 5.0)
    ticker = _Ticker()

    ticker._note_forwarding_timer_tick()

    assert ticker.events == []


def test_forwarding_timer_instrumentation_cannot_break_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instrumentation_module.time, "monotonic", lambda: 5.0)
    ticker = _Ticker(enabled=True)
    before = dict(ticker._org_dial_state.answers)

    def broken_log(*_args: Any, **_fields: Any) -> None:
        raise RuntimeError("diagnostic sink failed")

    monkeypatch.setattr(ticker, "_log", broken_log)
    ticker._note_forwarding_timer_tick()

    assert ticker._org_dial_state.answers == before
