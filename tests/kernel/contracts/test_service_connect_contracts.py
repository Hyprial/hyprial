"""Frozen baseline-C service-connect contracts shared across implementation slices."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import re

from hyprial import kernel
from hyprial.kernel import ipc_errors


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = ROOT / "contract" / "service-connect" / "fixtures.json"
README_PATH = FIXTURE_PATH.with_name("README.md")
GO_MESSAGE_PATH = ROOT / "sidecar" / "hyprial-tailcat" / "internal" / "proto" / "message.go"

SERVICE_CODES = {
    "SERVICE_INVALID_TARGET",
    "SERVICE_PORT_IN_USE",
    "SERVICE_NAME_CONFLICT",
    "SERVICE_UNAVAILABLE",
    "SERVICE_NOT_REGISTERED",
    "SERVICE_SIDECAR_UNSUPPORTED",
    "SERVICE_REGISTRY_INVALID",
    "SERVICE_DEVICE_UNAVAILABLE",
    "SERVICE_NOT_AUTHORIZED",
}
GO_SERVICE_FAILED_CODES = {
    "BAD_REQUEST",
    "NOT_UP",
    "ALREADY_UP",
    "KEY_FILE_INVALID",
    "START_FAILED",
    "PEER_UNKNOWN",
    "DIAL_FAILED",
    "PORT_IN_USE",
    "INTERNAL",
}
DAEMON_LOG_EVENTS = {
    "service.connect.access.invalid",
    "service.connect.mapping.closed",
    "service.connect.mapping.failed",
    "service.connect.reconcile.completed",
    "service.connect.reconcile.failed",
    "service.connect.registry.fallback",
    "service.connect.registry.updated",
    "service.connect.restore.failed",
}


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_service_error_registry_is_frozen_and_nontransient() -> None:
    assert SERVICE_CODES <= ipc_errors.ALL_CODES
    assert "SERVICE_TAILNET_MISMATCH" not in ipc_errors.ALL_CODES
    assert SERVICE_CODES.isdisjoint(ipc_errors.TRANSIENT_CODE_CLASSES)
    for code in SERVICE_CODES:
        assert getattr(ipc_errors, code) == code


def test_service_policy_budgets_are_exported_through_kernel() -> None:
    expected = {
        "SERVICE_CONTROL_TIMEOUT_SECONDS": 3.0,
        "SERVICE_REGISTRY_READ_TIMEOUT_SECONDS": 1.0,
        "SERVICE_RECONCILE_INTERVAL_SECONDS": 5.0,
    }
    for name, value in expected.items():
        assert getattr(kernel, name) == value
        assert name in kernel.__all__


def test_service_daemon_modules_are_import_safe() -> None:
    # P0 shipped these as empty stubs; the slices fill them. Importing each
    # must stay side-effect free and never fail.
    modules = {
        "hyprial.daemon.impl.application.netendpoints.services",
        "hyprial.daemon.impl.desired_state.services",
        "hyprial.daemon.impl.forwarding.services",
        "hyprial.daemon.impl.service_connect",
        "hyprial.daemon.impl.service_connect.io",
        "hyprial.daemon.impl.service_connect.manager",
        "hyprial.daemon.impl.service_connect.models",
        "hyprial.daemon.impl.service_connect.registry",
        "hyprial.daemon.impl.service_connect.resolver",
        "hyprial.daemon.impl.state_persistence.services",
    }
    for name in modules:
        module = importlib.import_module(name)
        assert Path(module.__file__).is_file()


def test_service_contract_fixture_freezes_wire_and_durable_schema() -> None:
    fixture = _fixture()
    assert fixture["schemaVersion"] == 1
    assert fixture["catalogEntryFields"] == [
        "name", "deviceId", "remotePort", "protocol", "localPort",
        "usage", "env", "auth", "guide",
    ]
    assert fixture["protectedRecordFields"] == [
        "deviceId", "serverPublic", "address", "generation", "ports", "enabled",
    ]
    assert fixture["protocol"] == {
        "version": 3,
        "capability": "service-connect-v1",
        "requests": {
            "map-service": [
                "v", "op", "requestId", "name", "deviceId", "address",
                "serverPublic", "remotePort", "recordGeneration", "localPort",
            ],
            "unmap-service": ["v", "op", "requestId", "name"],
            "service-status": ["v", "op", "requestId"],
        },
        "responses": {
            "service-mapped": ["v", "event", "requestId", "service"],
            "service-unmapped": ["v", "event", "requestId", "name", "removed"],
            "service-status": ["v", "event", "requestId", "services"],
            "service-failed": [
                "v", "event", "requestId", "op", "name", "code", "message",
            ],
        },
    }
    assert fixture["mappingFields"] == [
        "name", "deviceId", "remotePort", "recordGeneration", "localPort",
        "activeConnections", "path", "pathDetail", "observedAtMs",
        "lastDialState", "lastDialMs", "lastError", "lastErrorAtMs",
    ]
    assert fixture["resultFields"] == ["state", "errorCode"]
    assert fixture["durableFields"] == ["serviceConnections", "serviceRegistry"]


def test_service_contract_fixture_freezes_go_errors_budgets_and_events() -> None:
    fixture = _fixture()
    errors = fixture["errors"]
    assert set(errors["serviceCodes"]) == SERVICE_CODES
    assert set(errors["goServiceFailedCodes"]) == GO_SERVICE_FAILED_CODES
    assert set(errors["goToService"]) == GO_SERVICE_FAILED_CODES
    assert set(errors["goToService"].values()) <= SERVICE_CODES
    assert errors["invalidParameterCode"] == "INVALID_ARGUMENT"
    assert errors["transientServiceCodes"] == []

    go_codes = set(re.findall(r'Code\w+\s*=\s*"([A-Z_]+)"', GO_MESSAGE_PATH.read_text()))
    assert GO_SERVICE_FAILED_CODES <= go_codes
    assert fixture["budgets"] == {
        "pythonControlSeconds": 3.0,
        "registryReadSeconds": 1.0,
        "reconcileIntervalSeconds": 5.0,
        "goServiceDialSeconds": 5.0,
        "streamLifetimeSeconds": None,
        "hostShutdownBounded": True,
    }
    assert set(fixture["daemonLogEvents"]) == DAEMON_LOG_EVENTS

    readme = README_PATH.read_text(encoding="utf-8")
    for phrase in (
        "unbounded stream lifetime",
        "bounded host shutdown",
        "No SERVICE_* code is transient",
        "no generic DaemonRequestError",
    ):
        assert phrase in readme


def test_tailcat_design_contains_host_command_and_service_connect_flow() -> None:
    design = (
        ROOT / "docs/design/tailnet-cutover-architecture-2026-10-03.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "`host --config <absolute config path>`",
        "`map-service`",
        "`service-mapped`",
        "service-connect-v1",
    ):
        assert phrase in design
