from __future__ import annotations

from dataclasses import dataclass

import pytest

from hyprial.kernel.impl.contracts.ports import PortCommandRejected
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, DomainCommandError


@dataclass(frozen=True, slots=True)
class _Completed:
    correlation_id: str


def test_correlated_events_return_only_the_expected_completion() -> None:
    events = CorrelatedDomainEvents(capacity=2)
    events.publish(_Completed("one"))
    assert events.wait("one", _Completed, timeout=0.01) == _Completed("one")


def test_correlated_rejection_is_not_misreported_as_success() -> None:
    events = CorrelatedDomainEvents()
    events.publish(
        PortCommandRejected(
            correlation_id="one",
            domain="agent",
            generation=1,
            version=2,
            code="NOPE",
            detail="rejected",
        )
    )
    with pytest.raises(DomainCommandError, match="rejected") as caught:
        events.wait("one", _Completed, timeout=0.01)
    assert caught.value.code == "NOPE"


def test_correlated_events_are_bounded_by_correlation() -> None:
    events = CorrelatedDomainEvents(capacity=2)
    events.publish(_Completed("one"))
    events.publish(_Completed("two"))
    events.publish(_Completed("three"))
    with pytest.raises(DomainCommandError) as caught:
        events.wait("one", _Completed, timeout=0)
    assert caught.value.code == "DOMAIN_COMMAND_TIMEOUT"
