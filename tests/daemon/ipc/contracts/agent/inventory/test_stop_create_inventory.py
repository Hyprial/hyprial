"""agent.stop / agent.create: declaration exactness, survival and probes.

``USER_AGENT_IMMUTABLE`` is declared on agent.stop but outside the
enumeration: the catalog dispatcher raises it before the stop walk (UA1b).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

from .enumeration import (
    _CREATE_METHOD,
    _EXCLUSIONS,
    _INVENTORY,
    _STOP_METHOD,
    _START_ONLY_CODES,
    _STOP_ONLY_CODES,
    _TerminalManager,
    _application,
    _declared_codes,
    _desired_harness,
    _inventory_reachable,
    _wire_af_unix,
    scan_candidates,
)


def test_inventory_covers_the_stop_declaration_exactly() -> None:
    declared = _declared_codes(_STOP_METHOD)
    reachable = _inventory_reachable() - _START_ONLY_CODES
    missing = reachable - declared
    assert not missing, f"reachable codes not declared on {_STOP_METHOD}: {sorted(missing)}"
    undeclared_only = declared - reachable - {
        "CALLER_NOT_AUTHORIZED",
        "TARGET_NOT_AUTHORIZED",
        "DAEMON_ERROR",
        "USER_AGENT_IMMUTABLE",
    }
    assert not undeclared_only, (
        f"{_STOP_METHOD} declares codes outside the enumeration: "
        f"{sorted(undeclared_only)}"
    )


def test_stop_carries_only_the_reservation_unknown_data() -> None:
    spec = METHOD_REGISTRY.get(_STOP_METHOD).spec
    declared = {error.code: error for error in spec.errors}
    for fact in _INVENTORY:
        if (
            fact.code not in declared
            or fact.code in _EXCLUSIONS
            or fact.code in _START_ONLY_CODES
        ):
            continue
        error = declared[fact.code]
        if fact.data == "none":
            assert error.data_schema is None, fact.code
        else:
            assert error.data_schema is not None, fact.code
            assert set(error.data_schema["required"]) == {"operationId"}, fact.code


def test_create_declares_the_domain_tail() -> None:
    declared = _declared_codes(_CREATE_METHOD)
    reachable = _inventory_reachable() - _STOP_ONLY_CODES - _START_ONLY_CODES
    missing = reachable - declared
    assert not missing, f"create is missing its domain tail: {sorted(missing)}"


def test_guard_every_producer_literal_is_inventory_or_excluded() -> None:
    candidates = scan_candidates()
    inventoried = {fact.code for fact in _INVENTORY} | set(_EXCLUSIONS)
    unknown = {
        code: sorted(modules)
        for code, modules in candidates.items()
        if code not in inventoried
    }
    assert not unknown, (
        "New producer code literal(s) are neither declared nor excluded; "
        f"extend _INVENTORY or _EXCLUSIONS with a recorded decision: {unknown}"
    )


def _stop_params() -> dict:
    return {"name": "alpha"}


def _assert_stop_survives(app, error_code: str) -> None:
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as legacy:
        app._ipc_down(
            {"target": "alpha", "all": False, "agentsOnly": True}
        )
    assert getattr(legacy.value, "code", None) == error_code, (
        f"legacy down erased {error_code}: {legacy.value}"
    )

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as routed:
        METHOD_REGISTRY.invoke(app, "agent.stop", _stop_params())
    assert routed.value.code == error_code, (
        f"registry erased {error_code}: {routed.value}"
    )
    assert routed.value.data is None, error_code

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as ipc:
        app.handle("agent.stop", _stop_params())
    assert ipc.value.code == routed.value.code, error_code
    assert ipc.value.data == routed.value.data, error_code


@pytest.mark.parametrize(
    "error_code",
    sorted(_inventory_reachable() - _START_ONLY_CODES - {"SUBMIT_OUTCOME_UNKNOWN"}),
)
def test_every_reachable_stop_code_survives_legacy_registry_and_ipc(
    app, error_code: str
) -> None:
    _desired_harness(app)
    _assert_stop_survives(app, error_code)


def test_reservation_outcome_unknown_survives_with_its_operation_id(app) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import (
        LifecycleReservationOutcomeUnknown,
    )
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    class _UnknownManager:
        crashed = False

        def submit(self, operation):
            raise LifecycleReservationOutcomeUnknown(operation.operation_id)

        def wait(self, _operation_id: str, _timeout: float):
            raise AssertionError("never waited; the reservation never admitted")

        def drain(self, _timeout: float) -> bool:
            return True

    _desired_harness(app)
    app._lifecycle_manager = _UnknownManager()
    with pytest.raises(Exception) as legacy:
        app._ipc_down({"target": "alpha", "all": False, "agentsOnly": True})
    assert legacy.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert set(legacy.value.data) == {"operationId"}

    app._lifecycle_manager = _UnknownManager()
    with pytest.raises(Exception) as ipc:
        app.handle("agent.stop", _stop_params())
    assert ipc.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert set(ipc.value.data) == {"operationId"}
    assert ipc.value.data["operationId"].startswith("lifecycle-down:")
    assert METHOD_REGISTRY.get("agent.stop") is not None


def test_legacy_down_preserves_what_the_enumeration_declares(app) -> None:
    """The legacy ``down`` IPC method shares the handler and the error set."""

    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    _desired_harness(app)
    sampled = ("LIFECYCLE_COMPENSATION_TIMEOUT", "DOMAIN_COMMAND_TIMEOUT")
    for error_code in sampled:
        assert error_code in _declared_codes(_STOP_METHOD)
        app._lifecycle_manager = _TerminalManager(error_code)
        with pytest.raises(Exception) as legacy:
            app.handle(
                "down", {"target": "alpha", "all": False, "agentsOnly": True}
            )
        assert legacy.value.code == error_code
        app._lifecycle_manager = _TerminalManager(error_code)
        with pytest.raises(Exception) as routed:
            METHOD_REGISTRY.invoke(app, "agent.stop", _stop_params())
        assert routed.value.code == legacy.value.code


# --- create: the domain tail survives the legacy translation -----------------


@pytest.mark.parametrize(
    "error_code",
    sorted(_inventory_reachable() - _STOP_ONLY_CODES - _START_ONLY_CODES),
)
def test_every_reachable_create_tail_code_survives_registry_and_ipc(
    app, error_code: str
) -> None:
    from hyprial.daemon.impl.composition import DomainCommandError
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    def refusing(_params):
        raise DomainCommandError(error_code, "domain owner refused the command")

    app._handle_agent_create_with_default = refusing  # type: ignore[method-assign]
    with pytest.raises(Exception) as routed:
        METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    assert routed.value.code == error_code, (
        f"registry erased {error_code} on agent.create"
    )
    assert routed.value.data is None
    with pytest.raises(Exception) as ipc:
        app.handle("agent.create", {"name": "alpha"})
    assert ipc.value.code == error_code
    assert ipc.value.data is None


# --- codex-daemon-request REWORK of PR #2270 ---------------------------------


def _probe_app(root: Path):
    return _application(root)


def test_reviewer_probe_create_success_preserves_durable_identity(app) -> None:
    response = _wire_af_unix(app, "agent.create", {"name": "created"})
    assert response["result"]["created"] is True
    assert app.agents.get("created") is not None


def test_reviewer_probe_create_closing_preserves_port_error(app) -> None:
    actor = app._agent_session_domains.agent
    saved = actor.submit
    actor.submit = lambda command: __import__(
        "hyprial.kernel", fromlist=["PortAdmission"]
    ).PortAdmission.CLOSING
    try:
        response = _wire_af_unix(app, "agent.create", {"name": "closed"})
    finally:
        actor.submit = saved
    assert response["error"]["code"] == "PORT_CLOSING"
    assert app.agents.get("closed") is None


def test_reviewer_probe_stop_closing_preserves_admission_error(app) -> None:
    from hyprial.daemon.impl.lifecycle_manager.process import LifecycleProcessManager

    _desired_harness(app)
    manager = LifecycleProcessManager(Path(app.state_dir) / "manager.db", object(), object())
    assert manager.drain(2)
    app._lifecycle_manager = manager
    try:
        response = _wire_af_unix(app, "agent.stop", {"name": "alpha"})
    finally:
        app._lifecycle_manager = None
    assert response["error"]["code"] == "DAEMON_START_FAILED"


def test_reviewer_probe_stop_unknown_preserves_operation_id(app) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import (
        LifecycleReservationOutcomeUnknown,
    )

    class _AdmissionUnknown:
        crashed = False

        def submit(self, operation):
            raise LifecycleReservationOutcomeUnknown(operation.operation_id)

        def drain(self, timeout):
            return True

    _desired_harness(app)
    app._lifecycle_manager = _AdmissionUnknown()
    response = _wire_af_unix(app, "agent.stop", {"name": "alpha"})
    assert response["error"]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert response["error"]["data"]["operationId"].startswith("lifecycle-down:")


def test_reviewer_probe_stop_empty_desired_state_control(app) -> None:
    app.agents.create("alpha")
    response = _wire_af_unix(app, "agent.stop", {"name": "alpha"})
    assert response["result"] == {"ok": True, "removed": []}
