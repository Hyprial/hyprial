"""daemon.catalog: live state, fixed reason order, and refusals."""

from __future__ import annotations

import threading
from dataclasses import replace

from hyprial.daemon.impl.request_catalog.daemon.catalog import (
    DAEMON_CATALOG_SPEC,
    _method_state,
    daemon_catalog_handler,
)
from hyprial.daemon.impl.request_catalog.link import LINK_LIST_SPEC
from hyprial.daemon.impl.request_catalog.daemon.readiness import READINESS_PROBES
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY


class _App:
    def __init__(self, *, restoring: bool = False, link_service: object | None = None) -> None:
        self.epoch = "epoch-1"
        self._restore_done = threading.Event()
        if not restoring:
            self._restore_done.set()
        self._link_service = link_service


def _entry(document: dict, name: str) -> dict:
    return next(item for item in document["methods"] if item["name"] == name)


def test_handler_projects_declarations_and_live_state() -> None:
    app = _App(link_service=object())
    document = daemon_catalog_handler(app, {})
    assert document["schemaVersion"] == 1
    assert document["catalogVersion"] == "1"
    assert document["epoch"] == "epoch-1"
    assert {item["name"] for item in document["methods"]} == set(METHOD_REGISTRY.names)

    listed = _entry(document, "link.list")
    assert listed["declared"] is True
    assert listed["state"] == "available"
    assert listed["callable"] is True
    assert listed["reason"] is None
    assert listed["httpBinding"]["identity"] == {
        "mode": "local-operator-selector",
        "param": "from",
    }

    admin = _entry(document, "link.add")
    assert admin["state"] == "disabled"
    assert admin["callable"] is False
    catalog = _entry(document, "daemon.catalog")
    assert catalog["state"] == "disabled"


def test_restore_gate_wins_before_binding_checks() -> None:
    app = _App(restoring=True, link_service=object())
    listed = _entry(daemon_catalog_handler(app, {}), "link.list")
    assert (listed["state"], listed["callable"], listed["reason"]) == (
        "not-ready",
        False,
        "RESTORE_IN_PROGRESS",
    )


def test_unwired_service_is_not_ready_not_disabled() -> None:
    app = _App(link_service=None)
    listed = _entry(daemon_catalog_handler(app, {}), "link.list")
    assert (listed["state"], listed["callable"], listed["reason"]) == (
        "not-ready",
        False,
        "SERVICE_NOT_READY",
    )


def test_method_state_order_and_unsupported_cases() -> None:
    app = _App(link_service=object())
    restore = app._restore_done
    # No binding means the generic no-selector default, not a missing route.
    assert _method_state(
        app,
        replace(LINK_LIST_SPEC, http_binding=None),
        restore_done=restore,
        probes=READINESS_PROBES,
    ) == ("available", True, None)
    assert _method_state(
        app,
        replace(
            LINK_LIST_SPEC,
            http_binding=replace(
                LINK_LIST_SPEC.http_binding,  # type: ignore[arg-type]
                readiness="missing-probe",
            ),
        ),
        restore_done=restore,
        probes=READINESS_PROBES,
    ) == ("unsupported", False, "READINESS_PROBE_MISSING")
    assert _method_state(
        app,
        LINK_LIST_SPEC,
        restore_done=restore,
        probes={},
    ) == ("unsupported", False, "READINESS_PROBE_MISSING")


def test_eligible_but_unbound_synthetic_method_is_unsupported_not_fatal() -> None:
    app = _App(link_service=object())
    spec = replace(
        LINK_LIST_SPEC,
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
        restoring.handle("link.list", {"from": "agent:h01-owner:h01-node:a"})
    except ipc_errors.DaemonRestoringError:
        pass
    else:  # pragma: no cover - the restore gate must refuse
        raise AssertionError("link.list was served during restore")

    restored = _Router()
    restored._restore_done.set()
    # daemon.catalog never reaches the _inbox/_harnesses/_presence assertions.
    assert restored.handle("daemon.catalog", {})["schemaVersion"] == 1
    # A declared business method with restore done does reach them, so this
    # dummy router fails exactly there: proof the meta branch is earlier.
    try:
        restored.handle("link.list", {"from": "agent:h01-owner:h01-node:a"})
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("link.list unexpectedly bypassed the business gates")
