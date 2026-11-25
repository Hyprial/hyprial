from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from tests.integration.service_connect.test_service_connect_integration import (
    _IdleRuntime, _application, _ipc,
)


@pytest.mark.parametrize("method,params", [
    ("service.list", {"name": "extra"}),
    ("service.connect", {}),
    ("service.connect", {"name": 4}),
    ("service.disconnect", {"name": "x", "extra": 1}),
    ("service.guide", {}),
])
def test_handler_retains_legacy_parameter_error(tmp_path, method, params):
    app, _ = _application(tmp_path)
    app._start_service_connect()
    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, params)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert str(caught.value) == "service request is invalid"
    assert caught.value.data is None


def test_empty_name_and_closed_manager_keep_order(tmp_path):
    app, _ = _application(tmp_path)
    app._start_service_connect()
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("service.guide", {"name": ""})
    assert caught.value.code == "SERVICE_NOT_REGISTERED"
    assert str(caught.value) == "service is not registered"
    app._service_manager.close(1.0)
    with pytest.raises(RuntimeError, match="service manager is closed"):
        app.handle("service.guide", {})


def test_service_specs_are_local_mutating_and_handler_validated():
    for suffix in ("connect", "disconnect", "guide", "list"):
        spec = METHOD_REGISTRY.get(f"service.{suffix}").spec
        assert (spec.mutates, spec.retry, spec.http_exposed, spec.ipc_param_validation) == (
            True, "never", False, "handler"
        )


def test_all_four_operations_over_isolated_socket(tmp_path):
    app, _ = _application(tmp_path)
    app.socket_path = Path("/tmp") / f"sc-{uuid4().hex}.sock"
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_service_connect()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        assert _ipc(app.socket_path, "service.list", {})["result"]["services"] == []
        assert _ipc(app.socket_path, "service.guide", {"name": "subapi"})["result"]["entry"]["name"] == "subapi"
        assert _ipc(app.socket_path, "service.connect", {"name": "subapi"})["result"]["service"]["state"] == "listening"
        assert _ipc(app.socket_path, "service.disconnect", {"name": "subapi"})["result"]["removed"] is True
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app.socket_path.unlink(missing_ok=True)
    assert not dispatcher.is_alive()


def test_restore_gate_precedes_registry_dispatch(tmp_path):
    app, _ = _application(tmp_path)
    app._restore_done.clear()
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("service.list", {})
    assert caught.value.code == "DAEMON_RESTORING"


def test_list_fallback_keeps_error_as_success_payload(tmp_path, monkeypatch):
    from hyprial.daemon.impl.service_connect import ServiceConnectError
    from hyprial.kernel import ipc_errors

    app, _ = _application(tmp_path)
    app._start_service_connect()

    def unavailable():
        raise ServiceConnectError(ipc_errors.SERVICE_UNAVAILABLE, "catalog unavailable")

    monkeypatch.setattr(app._service_manager, "_read_access", unavailable)
    result = app.handle("service.list", {})
    assert result["services"] == []
    assert result["registry"]["error"] == "catalog unavailable"


@pytest.mark.parametrize("code", [
    "SERVICE_INVALID_TARGET", "SERVICE_PORT_IN_USE", "SERVICE_NAME_CONFLICT",
    "SERVICE_UNAVAILABLE", "SERVICE_NOT_REGISTERED", "SERVICE_SIDECAR_UNSUPPORTED",
    "SERVICE_REGISTRY_INVALID", "SERVICE_DEVICE_UNAVAILABLE", "SERVICE_NOT_AUTHORIZED",
])
def test_domain_error_code_message_and_absent_data_are_preserved(code):
    from hyprial.daemon.impl.service_connect import ServiceConnectError

    class App:
        def _handle_service_connect(self, method, params):
            raise ServiceConnectError(code, "domain failure")

    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(App(), "service.connect", {"name": "test"})
    assert (caught.value.code, str(caught.value), caught.value.data) == (code, "domain failure", None)


@pytest.mark.parametrize("method,params", [
    ("service.list", {}), ("service.guide", {"name": "api"}),
])
def test_read_methods_persist_new_catalog_cache(tmp_path, actor_host, method, params):
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
    from hyprial.daemon.impl.service_connect import ServiceManager
    from tests.daemon.service_connect.support import (
        FakeController, FakeStatePort, ManualScheduler, catalog_document, write_access,
    )

    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="node-a", author="user:owner")
    manager = None
    try:
        space = runtime.facade.create_space("services")
        runtime.facade.write_text(space.space_id, "api.md", catalog_document())
        access = tmp_path / "state" / "tailcat" / "service-access.json"
        write_access(access, space_id=space.space_id)
        state = FakeStatePort()
        manager = ServiceManager(
            orgfs=runtime.facade, state=state, persistence_late_result=None,
            controller_factory=FakeController, access_path=access,
            scheduler=ManualScheduler(), actor_host=actor_host,
        )

        class App:
            def _handle_service_connect(self, selected, values):
                return manager.handle(selected, values)

        result = METHOD_REGISTRY.invoke(App(), method, params)
        assert result
        assert [name for name, _ in state.calls] == ["registry"]
        assert state.snapshot.service_registry is not None
    finally:
        if manager is not None:
            manager.close(1.0)
        runtime.close()
