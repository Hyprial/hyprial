"""IPC wiring: translated errors, the local device id, and the published device row.

Decision (service grant documents fix, 2026-11-26): every failure leaves the
handler as a ``DaemonRequestError`` without a local path; an unreadable device
record is refused instead of falling back to ``node_id``; a grant is refused
while this device has no row in that org's directory. The ``updated`` flag of
an in-place rewrite travels through the real router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.daemon.service_grants.test_ipc_and_cli import _RouterApp
from tests.daemon.service_grants.test_service_grants_core import (
    ORG,
    SYNTHETIC_SECRET,
    _as,
    _device,
    _make_key_store,
    _write_user_provider,
    harness,
)

_FIXTURES = (harness,)


@pytest.fixture
def routed(tmp_path: Path) -> Iterator[dict[str, Any]]:
    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="device-a", author="user:alice")
    store = OrgFsDirectoryStore(runtime.facade)
    space = runtime.facade.create_space(ORG)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    store.ensure_org_directory_roots(ORG)
    bob_state = tmp_path / "bob-state"
    bob_state.mkdir(mode=0o700)
    bob_pub = _make_key_store(bob_state, sub="bob", device_uid="device-b").ensure()
    with _as(runtime, "bob"):
        store.put_device(
            ORG,
            _device("device-b", "bob", encryption_key_id=bob_pub["keyId"], encryption_public_key=bob_pub["publicKey"]),
        )
    exposures_path = tmp_path / "exposures.json"
    exposures = ExposureStore(exposures_path)
    exposures.set({"port": 8447, "target": "tcp:127.0.0.1:8447", "proxyProtocol": "none"})
    app = _RouterApp(tmp_path, runtime, exposures)
    try:
        yield {"app": app, "store": store, "runtime": runtime, "exposures_path": exposures_path, "tmp": tmp_path}
    finally:
        runtime.close()


def _publish_local_device(routed: dict[str, Any]) -> None:
    routed["store"].put_device(ORG, _device("device-a", "alice"))


def _routed_error(app: _RouterApp, params: dict[str, Any]) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("service.grant", params)
    return caught.value


def test_corrupt_exposure_state_is_a_request_error_without_a_path(routed: dict[str, Any]) -> None:
    """No ``ForwardingSidecarError`` and no absolute path reach the caller."""

    _publish_local_device(routed)
    routed["exposures_path"].write_text("broken", encoding="utf-8")
    error = _routed_error(routed["app"], {"org": ORG, "port": 8447, "device": "@members"})
    assert type(error) is DaemonRequestError
    assert error.code == ipc_errors.SERVICE_UNAVAILABLE
    assert str(routed["tmp"]) not in str(error)
    assert routed["store"].list_service_grants(ORG) == []


def test_unreadable_device_record_is_refused_without_node_id_fallback(routed: dict[str, Any]) -> None:
    """``service.deviceId`` is never taken from ``node_id``."""

    _publish_local_device(routed)
    app: _RouterApp = routed["app"]
    for candidate in app.hyprial_home.rglob("*"):
        if candidate.is_file() and "device" in candidate.name:
            candidate.write_text("{broken", encoding="utf-8")
    error = _routed_error(app, {"org": ORG, "port": 8447, "device": "@members"})
    assert error.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE
    assert str(routed["tmp"]) not in str(error)
    assert routed["store"].list_service_grants(ORG) == []


def test_grant_is_refused_while_this_device_is_not_in_the_org_directory(routed: dict[str, Any]) -> None:
    """Through the router: no row under the caller for this device, no grant."""

    error = _routed_error(routed["app"], {"org": ORG, "port": 8447, "device": "@members"})
    assert error.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE
    assert routed["store"].list_service_grants(ORG) == []


def test_grant_is_refused_when_the_row_belongs_to_another_owner(harness: dict[str, Any]) -> None:
    """At the service: a ``device-c`` row under carol is not this device under alice."""

    service = ServiceGrantService(
        store=harness["store"],
        home=harness["home"],
        owner="alice",
        local_device_id="device-c",
        exposed_ports=lambda: frozenset({8447}),
        now_ms=harness["clock"],
    )
    with pytest.raises(DaemonRequestError) as caught:
        service.grant(org=ORG, port=8447, device="@members")
    assert caught.value.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE
    assert harness["store"].list_service_grants(ORG) == []


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param({"entryId": "absent", "fieldName": "API_KEY"}, id="missing-entry"),
        pytest.param({"entryId": "gateway", "fieldName": "NOPE"}, id="missing-field"),
    ],
)
def test_secret_errors_through_the_router_carry_no_entry_content(
    routed: dict[str, Any], credential: dict[str, str]
) -> None:
    """The routed error names the entry, never a value or a path."""

    _publish_local_device(routed)
    app: _RouterApp = routed["app"]
    _write_user_provider(app.hyprial_home, "gateway", {"API_KEY": SYNTHETIC_SECRET, "OTHER": "SYNTHETIC-OTHER"})
    error = _routed_error(app, {"org": ORG, "port": 8447, "device": "bob/device-b", "name": "svc", "credential": credential})
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    for forbidden in (SYNTHETIC_SECRET, "SYNTHETIC-OTHER", str(routed["tmp"])):
        assert forbidden not in str(error)
    assert all(SYNTHETIC_SECRET not in repr(event) for event in app.events)


def test_the_updated_flag_travels_through_the_router(routed: dict[str, Any]) -> None:
    _publish_local_device(routed)
    app: _RouterApp = routed["app"]
    first = app.handle("service.grant", {"org": ORG, "port": 8447, "device": "bob/device-b"})
    second = app.handle("service.grant", {"org": ORG, "port": 8447, "device": "bob/device-b"})
    assert first["updated"] is False
    assert second["updated"] is True
    assert second["grant"]["grantId"] == first["grant"]["grantId"]
    listed = app.handle("service.grants", {"org": ORG})
    assert [item["grantId"] for item in listed["granted"]] == [second["grant"]["grantId"]]
