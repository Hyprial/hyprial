"""A recipient that relies only on service grants still gets service-connect.

Decision (service grant S5, 2026-11-26, coordinator ruling a): the daemon
starts its service manager when the home has a protected access record, has
persisted connections, or this device holds an effective grant in any of its
orgs. The third check runs at startup, so it is cheap, answers "no" on any
failure, and never delays or blocks daemon startup.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.netendpoints import services as services_module
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _daemon_dependencies
from tests.daemon.service_connect.support import FakeController
from tests.daemon.service_grants.consumer.support import PORT, as_author, device_row
from tests.support.runtime._owned_applications import owned_applications as owned_applications

ORG = "group-acme"
GRANTOR = "alice"


def _grant_record(grant_id: str, name: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "grantId": grant_id,
        "service": {"deviceId": "device-a", "port": PORT, "name": name, "protocol": "tcp", "note": None},
        "recipient": "@members",
        "grantedAtMs": 1,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }


def _grant_id(index: int) -> str:
    return "sg." + base64.b32encode(index.to_bytes(17, "big")).decode("ascii").lower()[:26]


def _application(tmp_path: Path) -> tuple[DaemonApplication, FakeController]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = home / "state"
    state.mkdir()
    write_device_record(
        home,
        DeviceRecord(
            device_id="device-b",
            owner="bob",
            server_public="nodekey:" + "a" * 64,
            client_public="nodekey:" + "b" * 64,
            key_generation=1,
        ),
    )
    controller = _ClosableController()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="grant-only-startup",
        hyprial_home=home,
        owner="bob",
        forwarding_discovery=ForwardingEndpoints(controller, slots=lambda: ()),
        **_daemon_dependencies(),
    )
    return app, controller


class _ClosableController(FakeController):
    def close(self) -> None:
        self.closed = True


def _grant(app: DaemonApplication, name: str, index: int = 0) -> str:
    """Another member grants ``@members`` access to her device; this device receives."""

    runtime = app._require_orgfs_runtime()
    facade = runtime.facade
    space = next((item for item in facade.spaces() if item.name == ORG), None)
    if space is None:
        space = facade.create_space(ORG)
        facade.invite(space.space_id, f"user:{GRANTOR}", "rw")
    store = OrgFsDirectoryStore(facade)
    store.ensure_org_directory_roots(ORG)
    with as_author(runtime, GRANTOR):
        store.put_device(ORG, device_row("device-a", GRANTOR))
        grant_id = _grant_id(index)
        store.put_service_grant(ORG, GRANTOR, grant_id, _grant_record(grant_id, name))
    return grant_id


def _scheduler_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "service-connect-scheduler"]


def _access_record(app: DaemonApplication) -> Path:
    return app.hyprial_home / "state" / "tailcat" / "service-access.json"


def test_a_grant_only_recipient_starts_service_connect_at_startup(tmp_path: Path) -> None:
    app, controller = _application(tmp_path)
    _grant(app, "magpie")
    assert not _access_record(app).exists()
    before = {thread.ident for thread in threading.enumerate()}
    app._start_service_connect()
    try:
        assert app._service_manager is not None
        assert len(_scheduler_threads()) == 1
        added = [thread for thread in threading.enumerate() if thread.ident not in before]
        assert not any(isinstance(thread, threading.Timer) for thread in added)
        app._service_manager.reconcile()
        connected = app._handle_service_connect("service.connect", {"name": "magpie"})
        assert connected["service"]["state"] == "listening"
        assert [call[0] for call in controller.calls] == ["map"]
    finally:
        assert app._close_service_connect(2.0) is True
    assert _scheduler_threads() == []


def test_startup_re_derives_and_withdraws_after_a_restart(tmp_path: Path) -> None:
    app, controller = _application(tmp_path)
    _grant(app, "magpie")
    app._start_service_connect()
    app._service_manager.reconcile()
    app._handle_service_connect("service.connect", {"name": "magpie"})
    assert app._close_service_connect(2.0) is True
    controller.services.clear()

    app._start_service_connect()
    try:
        assert app._service_manager is not None
        app._service_manager.reconcile()
        assert app._handle_service_connect("service.list", {})["services"][0]["state"] == "listening"
    finally:
        assert app._close_service_connect(2.0) is True

    with as_author(app._require_orgfs_runtime(), GRANTOR):
        OrgFsDirectoryStore(app._require_orgfs_runtime().facade).remove_device(
            ORG, "device-a", owner=GRANTOR
        )
    app._start_service_connect()
    try:
        assert app._service_manager is not None
        app._service_manager.reconcile()
        assert app._handle_service_connect("service.list", {})["services"] == []
    finally:
        assert app._close_service_connect(2.0) is True


def test_no_effective_grant_keeps_the_unconfigured_home_inactive(tmp_path: Path, caplog) -> None:
    app, _controller = _application(tmp_path)
    before = {thread.ident for thread in threading.enumerate()}
    with caplog.at_level(logging.DEBUG):
        app._start_service_connect()

    assert app._service_manager is None
    assert {thread.ident for thread in threading.enumerate()} == before
    assert [record for record in caplog.records if record.getMessage().startswith("service.connect.")] == []
    app._close_service_connect(0.0)


def test_a_grant_for_someone_else_does_not_start_service_connect(tmp_path: Path) -> None:
    app, _controller = _application(tmp_path)
    _grant(app, "magpie")
    app.owner = "carol"

    app._start_service_connect()

    assert app._service_manager is None
    app._close_service_connect(0.0)


def test_a_failing_grant_check_is_answered_as_no_and_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path)
    _grant(app, "magpie")

    def failing(*args: object, **kwargs: object) -> object:
        raise OrgFsError("io", {"reason": "synthetic"})

    monkeypatch.setattr(services_module, "has_effective_grant", failing)
    started = time.monotonic()
    app._start_service_connect()
    app._close_service_connect(0.0)

    assert app._service_manager is None
    assert time.monotonic() - started < 1.0


def test_the_startup_grant_check_stays_cheap_as_grants_accumulate(tmp_path: Path) -> None:
    app, _controller = _application(tmp_path)
    for index in range(60):
        _grant(app, "magpie", index)

    samples = []
    for _ in range(10):
        started = time.monotonic()
        answer = services_module.has_effective_grant(
            orgfs=app._require_orgfs_runtime().facade,
            home=app.hyprial_home,
            owner="bob",
            device_id="device-b",
        )
        samples.append(time.monotonic() - started)
        assert answer is True

    assert sum(samples) / len(samples) < 0.1
