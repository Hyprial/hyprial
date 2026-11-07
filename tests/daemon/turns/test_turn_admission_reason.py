"""A refused delivery must say which rule refused it.

Two unrelated conditions both produce OVERLOADED, and they call for opposite
responses.  An id already reserved is waiting on the daemon to drain its
result -- it will become admissible on its own.  A full gate is capacity
pressure and will not.  Collapsed into one value, the holder of a refused
delivery cannot tell those apart, nor either of them from an unhealthy
process, because enqueue() only answers False.

This is the same defect as a turn failure logged as "failed": the reason was
already in hand at the point of refusal and simply never spoken.
"""

from __future__ import annotations

import logging

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import _AdmissionGate


def test_a_repeat_delivery_is_refused_as_already_reserved() -> None:
    """The recoverable case must be nameable.

    To make this red, return a bare PortAdmission again: both refusals become
    the same value and this distinction disappears.
    """

    gate = _AdmissionGate(capacity=8)
    first, first_reason = gate.reserve("delivery-1")
    assert first is PortAdmission.ACCEPTED
    assert first_reason == "accepted"

    second, second_reason = gate.reserve("delivery-1")
    assert second is PortAdmission.OVERLOADED
    assert second_reason == "already-reserved"


def test_a_full_gate_is_refused_as_capacity() -> None:
    """The unrecoverable case must be distinguishable from the recoverable one.

    Both are OVERLOADED, so a test asserting only the admission value would
    pass while the two remained indistinguishable -- which was the defect.
    """

    gate = _AdmissionGate(capacity=2)
    assert gate.reserve("a")[0] is PortAdmission.ACCEPTED
    assert gate.reserve("b")[0] is PortAdmission.ACCEPTED

    admission, reason = gate.reserve("c")
    assert admission is PortAdmission.OVERLOADED
    assert reason == "capacity"


def test_the_two_refusals_do_not_share_a_reason() -> None:
    """Pinned as a relation, not two constants.

    The two constant assertions above go red if either reason changes -- but
    they are also the two lines someone will edit to match a change.  This one
    survives that: whatever the two reasons end up called, collapsing them into
    a single value fails here.  The defect being guarded is not a wrong string,
    it is the two conditions becoming indistinguishable again.
    """

    full = _AdmissionGate(capacity=1)
    full.reserve("taken")
    _, capacity_reason = full.reserve("other")

    repeat = _AdmissionGate(capacity=8)
    repeat.reserve("same")
    _, repeat_reason = repeat.reserve("same")

    assert capacity_reason != repeat_reason


def test_releasing_makes_a_repeat_admissible_again() -> None:
    """Names why "already-reserved" is the recoverable one.

    The id is released when the daemon drains the result, so this refusal
    resolves itself once the daemon does its half -- which is exactly what a
    holder needs to know and could not previously learn.
    """

    gate = _AdmissionGate(capacity=4)
    gate.reserve("delivery-2")
    assert gate.reserve("delivery-2")[1] == "already-reserved"

    gate.release("delivery-2")
    admission, reason = gate.reserve("delivery-2")
    assert admission is PortAdmission.ACCEPTED
    assert reason == "accepted"


def test_refusal_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """A reason that never leaves the function is not observable.

    The caller sees a bool; without this record the distinction exists in the
    code and nowhere an operator can read it.
    """

    from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime

    runtime = TurnRuntime(
        name="admission-log-probe",
        io_submit=lambda *_a, **_k: None,
        io_interrupt=lambda *_a, **_k: None,
        io_close=lambda *_a, **_k: None,
        io_abort=lambda *_a, **_k: None,
        io_failure_decision=lambda *_a, **_k: None,
    )
    try:
        from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
            EnqueueTurnCommand,
            TurnDeliveryProjection,
        )

        delivery = TurnDeliveryProjection(
            delivery_id="delivery-logged",
            conversation_id="c",
            sender="s",
            recipient="r",
            message="m",
        )
        command = EnqueueTurnCommand(
            correlation_id="delivery-logged", delivery=delivery
        )
        runtime.submit(command)
        with caplog.at_level(logging.INFO, logger="hyprial.turn_runtime"):
            runtime.submit(command)
    finally:
        runtime.drain(1.0)

    logged = [
        record
        for record in caplog.records
        if "admission refused" in record.getMessage()
    ]
    assert logged, "a refused admission left no record"
    detail = getattr(logged[0], "turn_admission", {})
    assert detail.get("reason") == "already-reserved"
    assert detail.get("deliveryId") == "delivery-logged"
