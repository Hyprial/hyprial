from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
import pytest
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import LIFECYCLE_WAIT_MARGIN_SECONDS
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import MutationProvenance
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState
from hyprial.daemon.impl.network.route_ports import DropRouteCommand

from ..support import (
    TERMINAL_WAIT_SECONDS,
    _CompletionPorts,
    _FailIndicesPorts,
    _ReplaceBeforeFailurePorts,
    _close_test_managers as _close_test_managers,
    _manager,
    _spec,
)


@pytest.mark.parametrize("failure_index", range(6))
def test_failure_at_each_create_step_compensates_completed_effects_in_reverse(
    tmp_path: Path, failure_index: int
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, fail_at=failure_index)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    assert (
        manager.submit(LifecycleOperation.create("create-1", _spec()))
        is PortAdmission.ACCEPTED
    )

    # This checks rollback order, not a two-second throughput contract. Six
    # forward admissions plus their durable inverse writes can exceed that
    # budget on the shared CI disk. wait returns as soon as the saga settles.
    result = manager.wait("create-1", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert len(result.completed_effects) == failure_index
    assert len(result.compensated_effects) == failure_index
    # The failed forward call is followed by one inverse for every committed
    # effect; this assertion is killed by deleting or forward-ordering rollback.
    assert len(ports.calls) == failure_index + 1 + failure_index
    inverse_types = [
        "DestroyAgentCommand",
        "DropRouteCommand",
        "ReleaseAgentCommand",
        "RemoveHarnessCommand",
        "UnregisterSessionCommand",
        "DropRouteCommand",
    ]
    assert [type(item).__name__ for item in ports.calls[failure_index + 1 :]] == list(
        reversed(inverse_types[:failure_index])
    )
    assert manager.drain(1)


def test_deactivate_preserves_agent_identity_and_persona_route(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    operation = LifecycleOperation.deactivate("deactivate", _spec())
    assert manager.submit(operation) is PortAdmission.ACCEPTED
    result = manager.wait("deactivate", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPLETED
    assert result.completed_effects == (
        "managed.ensure.drop",
        "register.unregister",
        "ensure.remove",
        "bind.release",
    )
    assert [type(item).__name__ for item in ports.calls] == [
        "DropRouteCommand",
        "UnregisterSessionCommand",
        "RemoveHarnessCommand",
        "ReleaseAgentCommand",
    ]
    route_drop = ports.calls[0]
    assert isinstance(route_drop, DropRouteCommand)
    assert route_drop.owner_lease == f"managed:{_spec().actor}"
    assert manager.drain(1)


def test_closed_domain_port_drives_the_same_reverse_compensation(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, close_at=2)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("closed", _spec()))
    result = manager.wait("closed", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.compensated_effects == (
        "agent.create",
        "route.persona.ensure",
    )
    assert [type(item).__name__ for item in ports.calls[-2:]] == [
        "DropRouteCommand",
        "DestroyAgentCommand",
    ]
    assert manager.drain(1)


def test_external_incumbent_created_at_mutation_boundary_is_never_compensated(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    # This receipt represents the exact TOCTOU interleaving: an external
    # transaction creates the incumbent immediately before the domain's create
    # transaction.  The atomic completion says it was not created by this op.
    receipts = {
        "existing:forward:0": MutationProvenance(False, False, "external-owner-token")
    }
    ports = _CompletionPorts(
        router,
        no_change_at=0,
        fail_at=1,
        receipts=receipts,
    )
    path = tmp_path / "saga.sqlite3"
    manager = _manager(path, ports, router)
    manager.submit(LifecycleOperation.create("existing", _spec()))
    result = manager.wait("existing", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.completed_effects == ("agent.create",)
    assert result.compensated_effects == ()
    assert [type(item).__name__ for item in ports.calls] == [
        "CreateAgentCommand",
        "EnsureRouteCommand",
    ]
    with sqlite3.connect(path) as db:
        receipt = db.execute(
            "SELECT created_by_operation, resource_token FROM lifecycle_effects "
            "WHERE operation_id = 'existing' AND effect_name = 'agent.create' "
            "AND direction = 'forward'"
        ).fetchone()
    assert receipt == (0, "external-owner-token")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert manager.drain(1)


@pytest.mark.parametrize(
    ("created_index", "failure_index", "resource_key", "inverse_type"),
        (
            (0, 1, "agent:worker", "DestroyAgentCommand"),
            (2, 3, "binding:agent:owner:node:worker", "ReleaseAgentCommand"),
            (3, 4, "harness:codex:worker", "RemoveHarnessCommand"),
            (4, 5, "session:agent:owner:node:worker", "UnregisterSessionCommand"),
        ),
)
def test_compensation_compare_and_delete_preserves_external_replacement(
    tmp_path: Path,
    created_index: int,
    failure_index: int,
    resource_key: str,
    inverse_type: str,
) -> None:
    router = CorrelationEventRouter()
    ports = _ReplaceBeforeFailurePorts(
        router, fail_at=failure_index, resource_key=resource_key
    )
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("replacement", _spec()))
    # This test checks compensation/provenance, not a two-second latency SLA.
    # Wait for the manager's terminal outcome, including durable inverse writes,
    # using the same deadline + caller margin as the application composition.
    result = manager.wait(
        "replacement", manager._operation_deadline + LIFECYCLE_WAIT_MARGIN_SECONDS
    )

    assert result.state is LifecycleState.COMPENSATED
    assert ports.current_tokens[resource_key] == "external-replacement-token"
    inverse_request = next(
        request
        for request in ports.requests
        if type(request.payload).__name__ == inverse_type
    )
    assert inverse_request.expected_resource_token == (
        f"resource:replacement:forward:{created_index}"
    )
    assert manager.drain(1)


def test_transfer_uses_one_saga_and_compensates_source_and_target(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    # Six source-remove steps, then fail on target create's second step.
    ports = _CompletionPorts(router, fail_at=7)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    operation = LifecycleOperation.transfer(
        "transfer-1", source=_spec("source"), target=_spec("target")
    )
    manager.submit(operation)
    result = manager.wait("transfer-1", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert any(name.startswith("source.") for name in result.compensated_effects)
    assert any(name.startswith("target.") for name in result.compensated_effects)
    assert manager.drain(1)


def test_original_error_code_wins_when_compensation_also_fails(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _FailIndicesPorts(
        router,
        {2, 3},
        failure_code="CALLER_NOT_AUTHORIZED",
    )
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("double-failure", _spec()))
    result = manager.wait("double-failure", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.FAILED
    assert result.error_code == "CALLER_NOT_AUTHORIZED"
    assert "compensation failed" in (result.error or "")
    assert manager.drain(1)
