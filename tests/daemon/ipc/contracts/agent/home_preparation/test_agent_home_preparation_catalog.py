"""Local Agent queries and launch preparation stay on their native IPC surface."""

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.migration import ROUTER_ONLY_METHODS


@pytest.mark.parametrize(
    ("method", "writes"),
    [
        ("agent.get", False),
        ("agent.readiness", False),
        ("agent.runtime-context", True),
        ("agent.ensure-home", True),
    ],
)
def test_ipc_registration_preserves_effects(method, writes):
    binding = METHOD_REGISTRY.get(method)
    assert binding is not None
    assert method not in ROUTER_ONLY_METHODS
    assert binding.spec.mutates is writes
    assert not binding.spec.http_exposed
    assert binding.spec.retry == ("never" if writes else "safe")
