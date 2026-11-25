# Decision record (file header; no inline comments by repository rule):
# - Contract: RoutineFacade.wait_in_flight_drained(*, names: Sequence[str],
#   timeout_seconds: float) -> list[str]. It blocks until every named
#   routine's in_flight rows are gone or the timeout elapses, and returns the
#   names still in flight, in the order given (empty list = drained).
# - It only observes: it never pauses, resumes, dispatches or settles. The
#   rows drain through the registry's existing timer poll of paused routines
#   (pac.status), which the daemon timer drives in production and the tests
#   drive with submit_timer.
# - Bounded: timeout_seconds must be finite and >= 0, else ValueError; 0 is a
#   single check. Not a busy wait: a blocked wait burns little CPU, checked
#   with process CPU time over a short real wait.
# - Every name must exist; an unknown name raises
#   RoutineServiceError("ROUTINE_NOT_FOUND") before any waiting.
"""Agent teleport slice 1: wait for paused routines' in-flight work to drain."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import pytest

from hyprial.biz.impl.routine.execution.service import RoutineServiceError

from .support import (
    ALPHA,
    OWNER,
    Clock,
    FakePac,
    dispatch_once,
    make_service,
    scheduled_yaml,
)


def _dispatched_and_paused(tmp_path: Path, name: str = "busy"):
    clock = Clock()
    pac = FakePac()
    service = make_service(tmp_path, pac=pac, clock=clock)
    service.add(yaml_text=scheduled_yaml(name, actor=ALPHA), owner=OWNER)
    dispatch_once(service, clock, name)
    service.pause(name=name)
    assert service.status(name=name)["inFlight"] != []
    return service, pac


def test_already_drained_returns_empty_immediately(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("idle", actor=ALPHA), owner=OWNER)
        service.pause(name="idle")
        started = time.monotonic()
        assert service.wait_in_flight_drained(names=["idle"], timeout_seconds=5.0) == []
        assert time.monotonic() - started < 1.0
    finally:
        service.close()


def test_empty_name_list_is_trivially_drained(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        assert service.wait_in_flight_drained(names=[], timeout_seconds=5.0) == []
    finally:
        service.close()


def test_drains_within_timeout(tmp_path: Path) -> None:
    service, pac = _dispatched_and_paused(tmp_path)

    def settle_later() -> None:
        pac.settle("busy")
        service.submit_timer()

    trigger = threading.Timer(0.2, settle_later)
    try:
        started = time.monotonic()
        trigger.start()
        remaining = service.wait_in_flight_drained(names=["busy"], timeout_seconds=5.0)
        elapsed = time.monotonic() - started
        assert remaining == []
        assert 0.2 <= elapsed < 5.0
        status = service.status(name="busy")
        assert status["inFlight"] == []
        assert status["enabled"] is False
    finally:
        trigger.cancel()
        service.close()


def test_timeout_returns_names_still_in_flight(tmp_path: Path) -> None:
    service, pac = _dispatched_and_paused(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("idle", actor=ALPHA), owner=OWNER)
        service.pause(name="idle")
        started = time.monotonic()
        remaining = service.wait_in_flight_drained(
            names=["idle", "busy"], timeout_seconds=0.3
        )
        elapsed = time.monotonic() - started
        assert remaining == ["busy"]
        assert 0.3 <= elapsed < 2.0
        assert service.status(name="busy")["inFlight"] != []
        assert service.status(name="busy")["enabled"] is False
        assert pac.rearm_calls == []
    finally:
        service.close()


def test_result_preserves_the_given_order(tmp_path: Path) -> None:
    clock = Clock()
    service = make_service(tmp_path, clock=clock)
    try:
        for name in ("first", "second"):
            service.add(yaml_text=scheduled_yaml(name, actor=ALPHA), owner=OWNER)
        dispatch_once(service, clock, "first")
        for name in ("first", "second"):
            if service.status(name=name)["inFlight"] == []:
                dispatch_once(service, clock, name)
            service.pause(name=name)
        remaining = service.wait_in_flight_drained(
            names=["second", "first"], timeout_seconds=0.0
        )
        assert remaining == ["second", "first"]
    finally:
        service.close()


def test_zero_timeout_is_a_single_check(tmp_path: Path) -> None:
    service, _pac = _dispatched_and_paused(tmp_path)
    try:
        started = time.monotonic()
        assert service.wait_in_flight_drained(names=["busy"], timeout_seconds=0.0) == [
            "busy"
        ]
        assert time.monotonic() - started < 1.0
    finally:
        service.close()


@pytest.mark.parametrize("timeout", [-1.0, math.inf, math.nan])
def test_unbounded_or_negative_timeout_is_rejected(tmp_path: Path, timeout: float) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("idle", actor=ALPHA), owner=OWNER)
        with pytest.raises(ValueError):
            service.wait_in_flight_drained(names=["idle"], timeout_seconds=timeout)
    finally:
        service.close()


def test_unknown_routine_is_explicit_not_found(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        started = time.monotonic()
        with pytest.raises(RoutineServiceError) as failure:
            service.wait_in_flight_drained(names=["absent"], timeout_seconds=5.0)
        assert failure.value.code == "ROUTINE_NOT_FOUND"
        assert time.monotonic() - started < 1.0
    finally:
        service.close()


def test_blocked_wait_is_not_a_busy_loop(tmp_path: Path) -> None:
    service, _pac = _dispatched_and_paused(tmp_path)
    try:
        cpu_started = time.process_time()
        remaining = service.wait_in_flight_drained(names=["busy"], timeout_seconds=0.6)
        cpu_used = time.process_time() - cpu_started
        assert remaining == ["busy"]
        assert cpu_used < 0.3
    finally:
        service.close()
