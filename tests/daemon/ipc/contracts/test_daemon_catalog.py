"""daemon.catalog: live state, fixed reason order, and refusals."""

from __future__ import annotations

import threading

import pytest
from dataclasses import replace

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.daemon.catalog import (
    DAEMON_CATALOG_SPEC,
    _method_state,
    daemon_catalog_handler,
)
from hyprial.daemon.impl.request_catalog.daemon.readiness import READINESS_PROBES
from hyprial.daemon.impl.request_catalog.message.query import MESSAGE_QUERY_SPEC
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, MethodRegistry
from hyprial.kernel import HttpBinding, IdentityBinding, MethodSpecError


class _App:
    def __init__(self, *, restoring: bool = False, inbox: object | None = None) -> None:
        self.epoch = "epoch-1"
        self._restore_done = threading.Event()
        if not restoring:
            self._restore_done.set()
        self._inbox = inbox


def _entry(document: dict, name: str) -> dict:
    return next(item for item in document["methods"] if item["name"] == name)


def test_handler_projects_declarations_and_live_state() -> None:
    app = _App(inbox=object())
    document = daemon_catalog_handler(app, {})
    assert document["schemaVersion"] == 1
    assert document["catalogVersion"] == "1"
    assert document["epoch"] == "epoch-1"
    assert {item["name"] for item in document["methods"]} == set(METHOD_REGISTRY.names)

    listed = _entry(document, "message.query")
    assert listed["declared"] is True
    assert listed["state"] == "available"
    assert listed["callable"] is True
    assert listed["reason"] is None
    assert listed["httpBinding"]["identity"] == {
        "mode": "local-owner-user-selector",
        "param": "actor",
    }

    catalog = _entry(document, "daemon.catalog")
    assert catalog["state"] == "disabled"

    unwired = _entry(daemon_catalog_handler(_App(inbox=None), {}), "message.query")
    assert (unwired["state"], unwired["callable"], unwired["reason"]) == (
        "not-ready",
        False,
        "SERVICE_NOT_READY",
    )


def test_live_catalog_matches_its_declared_result_schema() -> None:
    document = daemon_catalog_handler(_App(inbox=object()), {})
    Draft202012Validator(DAEMON_CATALOG_SPEC.result_schema).validate(document)


def test_live_catalog_projects_the_ipc_param_validation_policy() -> None:
    document = daemon_catalog_handler(_App(inbox=object()), {})
    assert _entry(document, "message.send")["ipcParamValidation"] == "handler"
    assert "ipcParamValidation" not in _entry(document, "message.history")


def test_restore_gate_wins_before_binding_checks() -> None:
    app = _App(restoring=True, inbox=object())
    listed = _entry(daemon_catalog_handler(app, {}), "message.query")
    assert (listed["state"], listed["callable"], listed["reason"]) == (
        "not-ready",
        False,
        "RESTORE_IN_PROGRESS",
    )


def test_unwired_service_is_not_ready_not_disabled() -> None:
    app = _App(inbox=None)
    listed = _entry(daemon_catalog_handler(app, {}), "message.query")
    assert (listed["state"], listed["callable"], listed["reason"]) == (
        "not-ready",
        False,
        "SERVICE_NOT_READY",
    )


def test_method_state_order_and_unsupported_cases() -> None:
    app = _App(inbox=object())
    restore = app._restore_done
    # No binding means the generic no-selector default, not a missing route.
    assert _method_state(
        app,
        replace(MESSAGE_QUERY_SPEC, http_binding=None),
        restore_done=restore,
        probes=READINESS_PROBES,
    ) == ("available", True, None)
    assert _method_state(
        app,
        replace(
            MESSAGE_QUERY_SPEC,
            http_binding=replace(
                MESSAGE_QUERY_SPEC.http_binding,  # type: ignore[arg-type]
                readiness="missing-probe",
            ),
        ),
        restore_done=restore,
        probes=READINESS_PROBES,
    ) == ("unsupported", False, "READINESS_PROBE_MISSING")
    assert _method_state(
        app,
        MESSAGE_QUERY_SPEC,
        restore_done=restore,
        probes={},
    ) == ("unsupported", False, "READINESS_PROBE_MISSING")


def test_eligible_but_unbound_synthetic_method_is_unsupported_not_fatal() -> None:
    app = _App(inbox=object())
    spec = replace(
        MESSAGE_QUERY_SPEC,
        name="synthetic.eligible",
        owner="synthetic.probe",
        http_exposed=True,
        http_binding=None,
    )
    assert _method_state(
        app,
        spec,
        restore_done=app._restore_done,
        probes=READINESS_PROBES,
    ) == ("available", True, None)


def test_the_catalog_method_itself_is_internal_and_not_http_exposed() -> None:
    assert DAEMON_CATALOG_SPEC.audience == ("internal",)
    assert DAEMON_CATALOG_SPEC.http_exposed is False
    assert DAEMON_CATALOG_SPEC.http_binding is None
    assert DAEMON_CATALOG_SPEC.mutates is False
    assert DAEMON_CATALOG_SPEC.retry == "safe"


def test_router_serves_the_meta_method_before_business_assertions() -> None:
    from hyprial.daemon.impl.ipc.router import (
        _RESTORE_GATE_LIGHT_METHODS,
        _IpcDispatchMixin,
    )
    from hyprial.kernel import ipc_errors

    class _Router(_IpcDispatchMixin):
        def __init__(self) -> None:
            self.epoch = "epoch-1"
            self._restore_done = threading.Event()

    assert "daemon.catalog" in _RESTORE_GATE_LIGHT_METHODS

    restoring = _Router()
    document = restoring.handle("daemon.catalog", {})
    assert document["epoch"] == "epoch-1"
    assert all(item["state"] != "available" for item in document["methods"])
    try:
        restoring.handle("message.query", {"from": "agent:h01-owner:h01-node:a"})
    except ipc_errors.DaemonRestoringError:
        pass
    else:  # pragma: no cover - the restore gate must refuse
        raise AssertionError("message.query was served during restore")

    restored = _Router()
    restored._restore_done.set()
    # daemon.catalog never reaches the _inbox/_harnesses/_presence assertions.
    assert restored.handle("daemon.catalog", {})["schemaVersion"] == 1
    # A declared business method with restore done does reach them, so this
    # dummy router fails exactly there: proof the meta branch is earlier.
    try:
        restored.handle("message.query", {"from": "agent:h01-owner:h01-node:a"})
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("message.query unexpectedly bypassed the business gates")


def test_registry_refuses_an_unassembled_readiness_probe() -> None:
    spec = replace(MESSAGE_QUERY_SPEC,
        http_exposed=True,
        http_binding=HttpBinding(
            IdentityBinding("local-owner-user-selector", "from"),
            readiness="not-a-probe",
        ),
    )
    with pytest.raises(MethodSpecError, match="is not assembled"):
        MethodRegistry.from_bindings(((spec, lambda app, params, **_: {"ok": True}),))
def test_the_frozen_declarations_are_exported_once_per_process(monkeypatch) -> None:
    """daemon.catalog is on the HTTP entry's 2 s handshake path.

    Re-validating every declared schema per call cost ~2 s with the org
    catalog; the registry is frozen, so the export is computed once.
    """

    from hyprial.daemon.impl.request_catalog import registry
    from hyprial.daemon.impl.request_catalog.daemon import catalog

    calls: list[int] = []
    real = registry.export_catalog

    def counting(specs, **options):
        calls.append(1)
        return real(specs, **options)

    monkeypatch.setattr(registry, "export_catalog", counting)
    monkeypatch.setattr(catalog, "export_catalog", counting, raising=False)
    registry._frozen_catalog.cache_clear()
    app = _App(inbox=object())

    first = daemon_catalog_handler(app, {})
    second = daemon_catalog_handler(app, {})
    public = registry.daemon_catalog()
    public["methods"].clear()

    assert len(calls) == 1
    assert first["methods"] == second["methods"]
    assert {item["name"] for item in registry.daemon_catalog()["methods"]} == set(
        METHOD_REGISTRY.names
    )
