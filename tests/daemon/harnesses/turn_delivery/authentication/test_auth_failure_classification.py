"""A credential the model vendor rejects must stop redelivery, in both layers.

Observed 2026-09-17 on a claude worker: "Failed to authenticate. API Error:
403 Request not allowed" was retried as transient, and every retry re-ran a
turn that had already sent its side-effect message -- 56 verbatim duplicates.

Two retry layers decide independently: the turn runtime (attempt budget,
``provider_failure_is_terminal``) and the daemon inbox (``max_attempts``,
``classify_harness_failure``).  Stopping only one of them still multiplies
the side effects, so each layer is pinned here, and so is their agreement.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from hyprial.daemon.impl.api import classify_harness_failure, harness_failure_is_permanent
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import EnqueueTurnCommand, TurnDeliveryProjection
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import (
    _TERMINAL_PROVIDER_ERROR_MARKERS,
    TurnRuntime,
    provider_failure_is_terminal,
)

OBSERVED = "Failed to authenticate. API Error: 403 Request not allowed"

#: The verbatim shape the controlled loader hands the observer when the fake
#: provider answers 429: pi reports ``<status>: <body>``.
OBSERVED_RATE_LIMIT = '429: {"type":"rate_limit_error","message":"slow down"}'


def test_observed_text_is_a_permanent_authentication_failure() -> None:
    code = classify_harness_failure(OBSERVED)
    assert code == "PROVIDER_AUTHENTICATION_FAILED"
    assert harness_failure_is_permanent(code)


def test_observed_rate_limit_is_terminal_for_the_turn_runtime() -> None:
    """A 429 cannot heal inside one turn; retrying only multiplies calls."""

    assert provider_failure_is_terminal(OBSERVED_RATE_LIMIT) is True


def test_observed_rate_limit_is_a_permanent_non_blocking_code_for_the_daemon() -> None:
    """Both retry layers must agree, or the daemon redelivers what pi stopped.

    The code is its own class, not the quota class: the window resets, so a
    429 must stop this delivery without blocking the agent.
    """

    code = classify_harness_failure(OBSERVED_RATE_LIMIT)
    assert code == "PROVIDER_RATE_LIMITED", code
    assert harness_failure_is_permanent(code)


def test_observed_text_is_terminal_for_the_turn_runtime() -> None:
    assert provider_failure_is_terminal(OBSERVED) is True


@pytest.mark.parametrize("marker", sorted(_TERMINAL_PROVIDER_ERROR_MARKERS))
def test_every_turn_terminal_marker_is_permanent_for_the_daemon(marker: str) -> None:
    """The turn runtime stopping while the daemon redelivers still repeats
    the turn -- the two vocabularies must not drift apart in that direction."""

    code = classify_harness_failure(f"model vendor said: {marker}")
    assert harness_failure_is_permanent(code), (marker, code)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


def _run_until_abandoned(error: str, max_attempts: int) -> tuple[list, object]:
    started: list[tuple[int, int]] = []
    decisions: list[tuple[bool, int]] = []
    runtime = TurnRuntime(
        name="auth-abandonment",
        max_delivery_attempts=max_attempts,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version)
        ),
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=(
            lambda generation, version, delivery, retry, attempt, err: decisions.append(
                (retry, attempt)
            )
        ),
    )
    runtime.submit(
        EnqueueTurnCommand(
            correlation_id="correlation-one",
            delivery=TurnDeliveryProjection(
                delivery_id="one",
                conversation_id="conversation",
                sender="sender",
                recipient="recipient",
                message="prompt-one",
            ),
        )
    )
    _wait_until(lambda: bool(started))
    generation, version = started[0]
    for _ in range(max_attempts):
        before = len(decisions)
        runtime.fail_io(
            generation=generation, version=version, delivery_id="one", error=error
        )
        _wait_until(lambda: len(decisions) > before)
        if not decisions[-1][0]:
            break
    [result] = runtime.read_results()
    runtime.drain_results()
    runtime.drain(1.0)
    return decisions, result


@pytest.mark.parametrize(
    "error",
    [
        OBSERVED,
        # Already terminal before this fix: isolates the hand-off itself,
        # independent of the new marker.
        "authentication failed",
    ],
)
def test_abandoned_terminal_turn_carries_a_permanent_failure_code(error: str) -> None:
    """The turn runtime already stops on the first attempt here; the result it
    hands the daemon must say so, or the daemon redelivers it anyway."""

    decisions, result = _run_until_abandoned(error, max_attempts=5)
    assert decisions == [(False, 1)]
    assert result.status == "failed"
    assert harness_failure_is_permanent(result.failure_code), result.failure_code


def test_abandoned_rate_limited_turn_stops_on_the_first_attempt() -> None:
    """§8.2: one 429 settles the delivery instead of re-running the turn."""

    decisions, result = _run_until_abandoned(OBSERVED_RATE_LIMIT, max_attempts=5)
    assert decisions == [(False, 1)]
    assert result.status == "failed"
    assert result.failure_code == "PROVIDER_RATE_LIMITED", result.failure_code
    assert harness_failure_is_permanent(result.failure_code)


def test_abandoned_retryable_turn_keeps_its_transient_code() -> None:
    """Budget exhaustion on a non-credential failure is unchanged."""

    decisions, result = _run_until_abandoned("wire disconnected", max_attempts=2)
    assert decisions == [(True, 1), (False, 2)]
    assert result.failure_code == "HARNESS_TRANSIENT_FAILURE"
