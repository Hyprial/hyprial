from __future__ import annotations

from dataclasses import dataclass

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.correlation.correlation import (
    CompletionReceipt,
    CorrelationEventRouter,
    CorrelationRouterOverloaded,
)


@dataclass(frozen=True)
class _Event:
    correlation_id: str
    generation: int
    version: int
    value: str = "ok"


def test_exact_generation_and_version_fence_stale_completion() -> None:
    router = CorrelationEventRouter(capacity=2)
    waiter = router.register(
        "op", attempt_token="attempt-4", generation=4, versions=frozenset({9})
    )

    assert (
        router.publish(_Event("op", 3, 9), attempt_token="attempt-4")
        is CompletionReceipt.STALE
    )
    assert (
        router.publish(_Event("op", 4, 8), attempt_token="attempt-4")
        is CompletionReceipt.STALE
    )
    event = _Event("op", 4, 9)
    assert (
        router.publish(event, attempt_token="attempt-4") is CompletionReceipt.COMMITTED
    )
    assert waiter.wait(0.1) is event


def test_timeout_retires_waiter_and_late_event_cannot_wake_reuse() -> None:
    router = CorrelationEventRouter()
    old = router.register(
        "same", attempt_token="old", generation=1, versions=frozenset({1})
    )
    with pytest.raises(TimeoutError):
        old.wait(0.001)
    assert (
        router.publish(_Event("same", 1, 1), attempt_token="old")
        is CompletionReceipt.STALE
    )

    current = router.register(
        "same", attempt_token="new", generation=2, versions=frozenset({1})
    )
    assert (
        router.publish(_Event("same", 2, 1), attempt_token="old")
        is CompletionReceipt.STALE
    )
    assert (
        router.publish(_Event("same", 1, 1), attempt_token="new")
        is CompletionReceipt.STALE
    )
    assert (
        router.publish(_Event("same", 2, 1), attempt_token="new")
        is CompletionReceipt.COMMITTED
    )
    assert current.wait(0.1).generation == 2


def test_waiter_registry_is_bounded() -> None:
    router = CorrelationEventRouter(capacity=1)
    first = router.register(
        "first", attempt_token="first", generation=1, versions=frozenset({1})
    )
    with pytest.raises(CorrelationRouterOverloaded):
        router.register(
            "second", attempt_token="second", generation=1, versions=frozenset({1})
        )
    first.cancel()
    assert router.register(
        "second", attempt_token="second", generation=1, versions=frozenset({1})
    )


def test_mailbox_admission_is_not_a_completion_event() -> None:
    router = CorrelationEventRouter()
    router.register(
        "op", attempt_token="attempt", generation=1, versions=frozenset({1})
    )
    with pytest.raises(TypeError):
        router.publish(PortAdmission.ACCEPTED)  # type: ignore[arg-type]
    assert router.pending() == 1

    rejected = PortCommandRejected(
        "op",
        "agent",
        1,
        1,
        "PORT_OVERLOADED",
        "mailbox full",
        admission=PortAdmission.OVERLOADED,
    )
    assert router.publish(rejected, attempt_token="attempt") is CompletionReceipt.STALE
    assert router.pending() == 1
