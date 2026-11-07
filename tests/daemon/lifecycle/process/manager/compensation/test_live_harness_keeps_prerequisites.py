"""A harness removal that removed nothing must not strip the live harness's Agent.

Production 0.5.0, 2026-10-05: four ``lifecycle-start`` operations settled late
and were compensated.  Each ``harness.ensure`` compensation completed with
``changed = 0`` -- the harness was still running -- yet ``agent.bind`` was
compensated with ``changed = 1``.  The result was a running harness whose
Agent binding had been released (e2e-verifier, squire, kanban,
hyprial-introducer).  Everything ordered before ``harness.ensure`` is a
prerequisite of that harness, so it stays.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleState,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission

from ..support import (
    TERMINAL_WAIT_SECONDS,
    _CompletionPorts,
    _ReplaceBeforeFailurePorts,
    _close_test_managers as _close_test_managers,
    _spec,
)

# Forward call order for _spec(): agent.create, route.persona.ensure,
# agent.bind, harness.ensure, session.register.  Failing the session step
# compensates in reverse, starting with the harness removal (call 5).
SESSION_REGISTER = 4
HARNESS_REMOVAL = 5
# Removal is compare-and-delete on the token this operation created.  The
# harness is taken over (a newer token) just before the failing step, so the
# removal changes nothing -- the production shape.
HARNESS_KEY = "harness:codex:worker"


def _coordinator(tmp_path: Path, ports: _CompletionPorts, router):  # type: ignore[no-untyped-def]
    # The fake domain's current tokens play the desired state's active
    # lifecycle_resources, which the daemon wires in production.
    return LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
        live_resource=lambda key, token: ports.current_tokens.get(key) == token,
    )


@pytest.mark.parametrize("build", [_coordinator], ids=["coordinator"])
def test_a_harness_that_was_not_removed_keeps_its_agent_and_route(tmp_path: Path, build) -> None:  # type: ignore[no-untyped-def]
    router = CorrelationEventRouter()
    ports = _ReplaceBeforeFailurePorts(
        router, fail_at=SESSION_REGISTER, resource_key=HARNESS_KEY
    )
    manager = build(tmp_path, ports, router)
    try:
        assert (
            manager.submit(LifecycleOperation.create("late", _spec()))
            is PortAdmission.ACCEPTED
        )
        result = manager.wait("late", TERMINAL_WAIT_SECONDS)

        assert result.state is LifecycleState.COMPENSATED
        inverses = [type(call).__name__ for call in ports.calls[HARNESS_REMOVAL:]]
        assert inverses == ["RemoveHarnessCommand"], inverses
        assert ports.current_tokens[HARNESS_KEY] == "external-replacement-token"
        # The Agent record and binding the live harness runs under survive.
        assert "agent:worker" in ports.current_tokens
        assert "binding:agent:owner:node:worker" in ports.current_tokens
    finally:
        manager.drain(3.0)


@pytest.mark.parametrize("build", [_coordinator], ids=["coordinator"])
def test_a_removed_harness_still_rolls_back_everything(tmp_path: Path, build) -> None:  # type: ignore[no-untyped-def]
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, fail_at=SESSION_REGISTER)
    manager = build(tmp_path, ports, router)
    try:
        manager.submit(LifecycleOperation.create("early", _spec()))
        result = manager.wait("early", TERMINAL_WAIT_SECONDS)

        assert result.state is LifecycleState.COMPENSATED
        inverses = [type(call).__name__ for call in ports.calls[HARNESS_REMOVAL:]]
        assert inverses == [
            "RemoveHarnessCommand",
            "ReleaseAgentCommand",
            "DropRouteCommand",
            "DestroyAgentCommand",
        ], inverses
    finally:
        manager.drain(3.0)
