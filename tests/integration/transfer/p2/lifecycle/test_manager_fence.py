"""Both lifecycle engines carry the incarnation through every deactivate step."""

from dataclasses import replace

import pytest

from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation, LifecyclePorts, LifecycleProcessManager, LifecycleState,
)
from tests.daemon.lifecycle.process.manager.support import _CompletionPorts, _spec


@pytest.mark.parametrize('engine', [LifecycleProcessManager, LifecycleCoordinator])
def test_deactivate_carries_fence_to_all_owners(tmp_path, engine):
    router = CorrelationEventRouter()
    port = _CompletionPorts(router)
    manager = engine(tmp_path/'lifecycle.sqlite3', LifecyclePorts(port, port, port, port), router)
    try:
        spec = replace(_spec(), expected_entity_token='current')
        operation = LifecycleOperation.deactivate('transfer-quiesce:op:1', spec)
        assert manager.submit(operation) is PortAdmission.ACCEPTED
        result = manager.wait(operation.operation_id, 10)
        assert result.state is LifecycleState.COMPLETED
        assert len(port.requests) == 4
        assert {type(r.payload).__name__ for r in port.requests} == {
            'DropRouteCommand', 'UnregisterSessionCommand',
            'RemoveHarnessCommand', 'ReleaseAgentCommand',
        }
        assert all(r.expected_entity_token == 'current' for r in port.requests)
        assert all(r.expected_resource_token is None for r in port.requests)
        calls = len(port.requests)
        assert manager.submit(operation) is PortAdmission.ACCEPTED
        assert manager.wait(operation.operation_id, 10).state is LifecycleState.COMPLETED
        assert len(port.requests) == calls
    finally:
        port.release.set()
        assert manager.drain(5)
