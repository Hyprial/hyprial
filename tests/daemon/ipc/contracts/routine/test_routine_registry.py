from __future__ import annotations

import pytest

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app


METHODS = ("routine.audit", "routine.pause", "routine.resume")


@pytest.mark.parametrize("method", METHODS)
def test_routine_declared_and_preserves_caller_admission(method):
    class App:
        def _workflow_caller(self, params):
            raise DaemonRequestError("PAC_PRINCIPAL_UNVERIFIED", "caller is not bound", {"cause": "CALLER_NOT_AUTHORIZED"})

    assert method in METHOD_REGISTRY
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(App(), method, {"actor": "invalid"})
    assert caught.value.code == "PAC_PRINCIPAL_UNVERIFIED"
    assert caught.value.data == {"cause": "CALLER_NOT_AUTHORIZED"}


@pytest.mark.parametrize("method", ["routine.pause", "routine.resume"])
def test_routine_owner_gate_precedes_business_action(method):
    class Service:
        def status(self, *, name):
            assert name == "r"
            return {"owner": "user:other", "actor": None}

    class App:
        owner = "owner"
        _routine_service = Service()

        def _workflow_caller(self, params):
            return "user:other-caller"

        def _routine_caller_is(self, caller, owner, actor):
            return False

    assert method in METHOD_REGISTRY
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(App(), method, {"name": "r"})
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert str(caught.value) == "caller does not own this routine"


def test_audit_includes_unenriched_quarantine_and_migrations(app):
    from hyprial.biz.impl.routine.contracts.ports import RoutineProjection

    row = RoutineProjection(
        version=1, name="quarantined", owner="user:spec-owner", enabled=False,
        next_due_ms=0, source_error_streak=0, outcomes=(), in_flight=(),
        quarantine_reason="unresolved actor", registration_id="registration",
    ).to_payload()
    migration = {"routine": "quarantined", "field": "actor", "before": "old",
                 "after": "new", "migratedAtMs": 42}

    class Service:
        def list(self):
            return {"routines": [row]}

        def address_migrations(self):
            return [migration]

    original = app._routine_service
    app._routine_service = Service()
    try:
        assert app.handle("routine.audit", {"actor": "user:spec-owner"}) == {
            "quarantined": [row], "addressMigrations": [migration],
        }
    finally:
        app._routine_service = original


def test_other_routine_methods_remain_legacy():
    from hyprial.daemon.impl.request_catalog.migration import MIGRATED_FAMILIES

    assert "routine" not in MIGRATED_FAMILIES
    for method in ("routine.add", "routine.remove", "routine.set"):
        assert method not in METHOD_REGISTRY


@pytest.mark.parametrize("method", ["routine.pause", "routine.resume"])
def test_owner_lookup_keeps_original_port_error(method):
    from hyprial.daemon.impl.application.ports import RoutinePortError

    class Service:
        def status(self, *, name):
            raise RoutinePortError("ROUTINE_NOT_FOUND", "missing", {"source": "status"})

    class App:
        _routine_service = Service()

        def _workflow_caller(self, params):
            return "user:owner"

    with pytest.raises(RoutinePortError) as caught:
        METHOD_REGISTRY.invoke(App(), method, {"name": "missing"})
    assert (caught.value.code, str(caught.value), caught.value.data) == (
        "ROUTINE_NOT_FOUND", "missing", {"source": "status"}
    )


def test_three_methods_over_isolated_socket(app):
    import threading
    from pathlib import Path
    from uuid import uuid4

    from tests.daemon.ipc.contracts.agent.test_relations_specs import AGENT_ROUTINE_YAML, USER
    from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime, _ipc

    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app.socket_path = Path("/tmp") / f"routine-{uuid4().hex}.sock"
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        assert _ipc(app.socket_path, "routine.audit", {"actor": USER})["result"] == {
            "quarantined": [], "addressMigrations": [],
        }
        params = {"actor": USER, "name": "delegated"}
        assert _ipc(app.socket_path, "routine.pause", params)["result"] == {
            "name": "delegated", "enabled": False,
        }
        assert _ipc(app.socket_path, "routine.resume", params)["result"] == {
            "name": "delegated", "enabled": True,
        }
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app.socket_path.unlink(missing_ok=True)
    assert not dispatcher.is_alive()


@pytest.mark.parametrize("method,code", [
    ("routine.pause", "ROUTINE_NOT_FOUND"),
    ("routine.pause", "ROUTINE_OVERLOADED"),
    ("routine.resume", "ROUTINE_QUARANTINED"),
    ("routine.resume", "ROUTINE_COMMAND_TIMEOUT"),
])
def test_mutation_errors_keep_code_and_message(method, code):
    class Service:
        def status(self, *, name):
            return {"owner": "user:owner", "actor": None}

    class App:
        owner = "owner"
        _routine_service = Service()

        def _workflow_caller(self, params):
            return "user:owner"

        def _ipc_routine_pause(self, params):
            raise DaemonRequestError(code, "routine refused")

        _ipc_routine_resume = _ipc_routine_pause

    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(App(), method, {"name": "daily"})
    assert (caught.value.code, str(caught.value), caught.value.data) == (code, "routine refused", None)
