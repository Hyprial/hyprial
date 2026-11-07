"""A compensation that never settles must end, not hold its actor forever.

Production 0.5.0, 2026-10-05: kanban's start timed out and went to
compensation; its harness removal stayed ``dispatched`` with no time limit,
the operation kept its resources, and every later ``down``/``start`` for kanban
queued behind it.  Compensation now has the same deadline as the forward run:
a harness step is offered to the Harness owner's fenced settlement, and
whatever is still unconfirmed ends FAILED and releases the actor.
"""

from __future__ import annotations

from pathlib import Path


from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleState,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission

from ..support import TERMINAL_WAIT_SECONDS, _CompletionPorts, _spec

SESSION_REGISTER = 4  # the failing forward step; compensation starts at the harness
DEADLINE = 0.5


def _coordinator(tmp_path: Path, ports: _CompletionPorts, router, events):  # type: ignore[no-untyped-def]
    return LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
        operation_deadline=DEADLINE,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )


def test_an_unsettled_harness_removal_ends_failed_and_frees_the_actor(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router,
        fail_at=SESSION_REGISTER,
        block_effect_token="first:compensation:3",
        harness_failure_settlement="timeout",
    )
    events: list[tuple[str, dict[str, object]]] = []
    coordinator = _coordinator(tmp_path, ports, router, events)
    try:
        assert coordinator.submit(LifecycleOperation.create("first", _spec())) is PortAdmission.ACCEPTED
        first = coordinator.wait("first", TERMINAL_WAIT_SECONDS)

        assert first.state is LifecycleState.FAILED, first
        assert first.error_code == "LIFECYCLE_COMPENSATION_TIMEOUT"
        assert [name for name, _ in events] == ["compensation_deadline_failed"]
        assert events[0][1]["step"] == "harness.ensure"

        # The actor's resources were released: the next operation runs.
        assert coordinator.submit(LifecycleOperation.create("second", _spec())) is PortAdmission.ACCEPTED
        assert coordinator.wait("second", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_a_removal_the_harness_owner_settles_finishes_compensating(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router,
        fail_at=SESSION_REGISTER,
        block_effect_token="first:compensation:3",
        harness_failure_settlement="completed",
    )
    events: list[tuple[str, dict[str, object]]] = []
    coordinator = _coordinator(tmp_path, ports, router, events)
    try:
        coordinator.submit(LifecycleOperation.create("first", _spec()))
        first = coordinator.wait("first", TERMINAL_WAIT_SECONDS)

        assert first.state is LifecycleState.COMPENSATED, first
        assert events == []
        # The blocked removal is re-submitted on each re-drive until the
        # deadline; collapse those repeats before checking the order.
        inverses = [type(call).__name__ for call in ports.calls[SESSION_REGISTER + 1:]]
        collapsed = [name for i, name in enumerate(inverses) if i == 0 or inverses[i - 1] != name]
        assert collapsed == [
            "RemoveHarnessCommand",
            "ReleaseAgentCommand",
            "DropRouteCommand",
            "DestroyAgentCommand",
        ]
    finally:
        ports.release.set()
        coordinator.drain(3.0)


class _UnretirablePorts(_CompletionPorts):
    """The harness removal completes, but its receipt is never retireable."""

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        if attempt_token == "first:compensation:3":
            return False
        return super().retire_receipt(attempt_token, resource_token)


def test_a_completed_step_whose_receipt_stays_held_still_ends_failed(tmp_path: Path) -> None:
    # Independent review on #1135: here the deadline path used to ask the
    # owner again, retry the same retirement, raise from inside its own
    # except branch and re-drive the operation forever.
    router = CorrelationEventRouter()
    ports = _UnretirablePorts(
        router, fail_at=SESSION_REGISTER, harness_failure_settlement="completed"
    )
    events: list[tuple[str, dict[str, object]]] = []
    coordinator = _coordinator(tmp_path, ports, router, events)
    try:
        coordinator.submit(LifecycleOperation.create("first", _spec()))
        first = coordinator.wait("first", TERMINAL_WAIT_SECONDS)

        assert first.state is LifecycleState.FAILED, first
        assert first.error_code == "LIFECYCLE_COMPENSATION_TIMEOUT"
        assert [name for name, _ in events] == ["compensation_deadline_failed"]
        assert ports.failure_controls == []  # the owner was not asked again
    finally:
        ports.release.set()
        coordinator.drain(3.0)
