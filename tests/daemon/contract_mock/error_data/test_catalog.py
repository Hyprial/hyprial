"""Registered structural negatives reproduce request error data."""

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.workflows import _WorkflowSurfaceMixin
from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import _AgentRegistryOpsMixin
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, daemon_openapi
from hyprial.daemon.impl.service_connect import ServiceManager
from hyprial.kernel import DaemonRequestError


CASES = [
    (entry["name"], case)
    for entry in daemon_openapi()["x-hyprial-methods"]
    for case in entry.get("x-hyprial-examples", [])
    if case["kind"] == "invalid-params"
]


class _ServiceApp:
    def __init__(self):
        self.manager = ServiceManager.__new__(ServiceManager)
        self.manager._admission_closed = threading.Event()
        self.manager._closed = False
        self.manager._lock = threading.RLock()

    def _handle_service_connect(self, method, params):
        return self.manager.handle(method, params)


class _RoutineStatus:
    def status(self, *, name):
        pytest.fail(f"invalid name reached routine status: {name!r}")


class _RoutineApp(_WorkflowSurfaceMixin):
    owner = "owner"
    _routine_service = _RoutineStatus()

    def _pac_bound_caller(self, params):
        pytest.fail(f"unexpected routine caller: {params!r}")


class _IdentityApp(_IpcDispatchMixin):
    _ipc_peer = SimpleNamespace(pid=None)


class _DestroyApp(_IdentityApp):
    owner = "owner"
    epoch = "test-epoch"

    def _destroy_agent(self, *args, **kwargs):
        pytest.fail("invalid parameters reached Agent deletion")


class _AgentApp(_AgentRegistryOpsMixin):
    def _resolve_agent_alias(self, name):
        pytest.fail(f"invalid name reached agent lookup: {name!r}")


def _handler_app(method):
    if method == "agent.resolve":
        return _AgentApp()
    if method in {
        "service.connect",
        "service.disconnect",
        "service.grant",
        "service.guide",
        "service.list",
    }:
        return _ServiceApp()
    if method in {"routine.pause", "routine.resume"}:
        return _RoutineApp()
    pytest.fail(f"handler-validated example needs a real admission context: {method}")


@pytest.mark.parametrize("method,case", CASES, ids=[f"{m}:{c['name']}" for m, c in CASES])
def test_invalid_examples_preserve_error_and_data_omission(method, case):
    binding = METHOD_REGISTRY.get(method)
    handler_validation = binding.spec.ipc_param_validation == "handler"
    needs_handler = handler_validation and isinstance(case["params"], dict)
    app = _handler_app(method) if needs_handler else object()
    if method in {"agent.destroy", "agent.destroy.preview"}:
        app = _DestroyApp()
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, method, case["params"], trusted_message_origin="ipc")
    assert raised.value.code == case["error"]["code"]
    assert raised.value.data is None
    assert "data" not in case["error"]
    if needs_handler:
        assert str(raised.value) == case["error"]["message"]
