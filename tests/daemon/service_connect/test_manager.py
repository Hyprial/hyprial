from __future__ import annotations

import threading
import time
import logging
from dataclasses import replace

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.service_connect import ServiceConnectError, ServiceManager
from hyprial.daemon.impl.service_connect.models import CatalogEntry as _CatalogEntryModel

from hyprial.daemon.impl.service_connect.registry import CatalogRegistry
from hyprial.daemon.impl.service_connect import manager as manager_module

from .support import (
    FakeController,
    FakeStatePort,
    ManualScheduler,
    catalog_document,
    write_access,
)


CATALOG_ENTRY_PAYLOAD = _CatalogEntryModel.from_json(
    {
        "name": "subapi",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "protocol": "http",
        "localPort": 18080,
        "usage": "claude-code-baseurl",
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:{port}"},
        "auth": "sub2api API key; agents request grant sub2api-key through hq",
        "guide": "Export the printed environment manually. Existing streams break on restart.\n",
    }
)


def _manager(tmp_path, *, controller=None, state=None):
    access = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(access)
    controller = controller or FakeController()
    state = state or FakeStatePort()
    manager = ServiceManager(
        orgfs=None,
        state=state,
        persistence_late_result=None,
        controller_factory=lambda: controller,
        access_path=access,
        scheduler=ManualScheduler(),
        service_discovery=lambda: (CATALOG_ENTRY_PAYLOAD,),
    )
    return manager, controller, state


def test_connect_persists_intent_maps_then_persists_actual_port(tmp_path) -> None:
    manager, controller, state = _manager(tmp_path)
    try:
        result = manager.handle("service.connect", {"name": "subapi"})
        assert [call[0] for call in state.calls] == ["set", "set"]
        assert state.calls[0] == ("set", ("subapi", 18080))
        assert state.calls[1] == ("set", ("subapi", 18080))
        assert controller.calls[0][0] == "map"
        assert result["service"]["state"] == "listening"
        assert result["service"]["localPort"] == 18080
        assert result["env"] == {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:18080"
        }
        assert "address" not in repr(result)
        assert "serverPublic" not in repr(result)
    finally:
        manager.close(1.0)


def test_unknown_unsupported_and_denied_connect_have_zero_mutations_or_maps(tmp_path) -> None:
    manager, controller, state = _manager(tmp_path)
    try:
        with pytest.raises(ServiceConnectError) as unknown:
            manager.handle("service.connect", {"name": "missing"})
        assert unknown.value.code == "SERVICE_NOT_REGISTERED"
    finally:
        manager.close(1.0)
    assert state.calls == []
    assert controller.calls == []

    unsupported = FakeController(supported=False)
    manager, controller, state = _manager(tmp_path, controller=unsupported)
    try:
        with pytest.raises(ServiceConnectError) as raised:
            manager.handle("service.connect", {"name": "subapi"})
        assert raised.value.code == "SERVICE_SIDECAR_UNSUPPORTED"
    finally:
        manager.close(1.0)
    assert state.calls == []
    assert controller.calls == []


def test_failed_bind_remains_visible_and_disconnect_removes_before_unmap(tmp_path) -> None:
    controller = FakeController()
    controller.fail_map = RuntimeError("raw private failure tcDoNotLeak")
    manager, controller, state = _manager(tmp_path, controller=controller)
    try:
        with pytest.raises(ServiceConnectError) as raised:
            manager.handle("service.connect", {"name": "subapi"})
        assert raised.value.code == "SERVICE_UNAVAILABLE"
        assert "tcDoNotLeak" not in str(raised.value)
        listed = manager.handle("service.list", {})
        assert listed["services"][0]["state"] == "failed"
        assert listed["services"][0]["localPort"] == 18080

        controller.fail_map = None
        controller.services["subapi"] = {}
        disconnected = manager.handle("service.disconnect", {"name": "subapi"})
        assert disconnected == {"name": "subapi", "removed": True}
        assert state.calls[-1] == ("remove", "subapi")
        assert controller.calls[-1] == ("unmap", "subapi")
    finally:
        manager.close(1.0)


def test_actual_port_persistence_failure_compensates_mapping(tmp_path) -> None:
    state = FakeStatePort()
    state.fail_actual_port = True
    manager, controller, _state = _manager(tmp_path, state=state)
    try:
        with pytest.raises(ServiceConnectError) as raised:
            manager.handle("service.connect", {"name": "subapi"})
        assert raised.value.code == "SERVICE_UNAVAILABLE"
        assert [call[0] for call in controller.calls] == ["map", "unmap"]
        assert "subapi" not in controller.services
    finally:
        manager.close(1.0)


def test_tcp_dial_observation_is_not_mistaken_for_private_tailcat_material(
    tmp_path,
) -> None:
    controller = FakeController()
    original = controller.map_service

    def map_with_tcp_diagnostic(*args, **kwargs):
        value = original(*args, **kwargs)
        value["lastDialState"] = "error"
        value["lastError"] = "dial tcp 127.0.0.1:8080: connection refused"
        return value

    controller.map_service = map_with_tcp_diagnostic  # type: ignore[method-assign]
    manager, _controller, _state = _manager(tmp_path, controller=controller)
    try:
        connected = manager.handle("service.connect", {"name": "subapi"})
        assert connected["service"]["lastError"] == (
            "dial tcp 127.0.0.1:8080: connection refused"
        )
    finally:
        manager.close(1.0)


def test_guide_and_list_work_without_data_plane_and_reject_unknown_fields(tmp_path) -> None:
    manager, controller, state = _manager(
        tmp_path, controller=FakeController(supported=False)
    )
    try:
        guide = manager.handle("service.guide", {"name": "subapi"})
        assert guide["source"] == "discovery"
        assert guide["entry"]["deviceId"] == "sub2api"
        assert manager.handle("service.list", {})["services"] == []
        with pytest.raises(ServiceConnectError) as raised:
            manager.handle("service.connect", {"name": "subapi", "target": "bad"})
        assert raised.value.code == "INVALID_ARGUMENT"
    finally:
        manager.close(1.0)
    assert state.calls == []


def test_identical_orgfs_catalog_does_not_persist_on_reconcile_ticks(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="node-a", author="user:owner")
    manager = None
    try:
        space = runtime.facade.create_space("services")
        runtime.facade.write_text(space.space_id, "api.md", catalog_document())
        cached = CatalogRegistry(
            runtime.facade,
            space_id=space.space_id,
            owner="user:owner",
            now_ms=lambda: 1,
        ).load().cache
        assert cached is not None
        state = FakeStatePort()
        state.snapshot = replace(state.snapshot, service_registry=cached)
        access = tmp_path / "state" / "tailcat" / "service-access.json"
        write_access(access, space_id=space.space_id)
        manager = ServiceManager(
            orgfs=runtime.facade,
            state=state,
            persistence_late_result=None,
            controller_factory=lambda: FakeController(),
            access_path=access,
            scheduler=ManualScheduler(),
        )

        for _ in range(4):
            manager.reconcile()

        assert [call for call in state.calls if call[0] == "registry"] == []
    finally:
        if manager is not None:
            manager.close(1.0)
        runtime.close()


def test_late_persistence_completion_reloads_desired_on_next_reconcile(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingState(FakeStatePort):
        def set_service_connection(self, name: str, local_port: int):
            entered.set()
            assert release.wait(1.0)
            return super().set_service_connection(name, local_port)

    monkeypatch.setattr(manager_module, "SERVICE_CONTROL_TIMEOUT_SECONDS", 0.01)
    state = BlockingState()
    manager, controller, _state = _manager(tmp_path, state=state)
    try:
        with pytest.raises(ServiceConnectError, match="remains unsettled"):
            manager.handle("service.connect", {"name": "subapi"})
        assert entered.is_set()
        release.set()
        deadline = time.monotonic() + 1.0
        while not state.snapshot.service_connections and time.monotonic() < deadline:
            time.sleep(0.005)
        assert state.snapshot.service_connections

        manager.reconcile()

        assert [call[0] for call in controller.calls] == ["map"]
        assert manager.handle("service.list", {})["services"][0]["name"] == "subapi"
    finally:
        release.set()
        manager.close(1.0)


def test_missing_access_diagnostics_emit_once_per_transition(tmp_path, caplog) -> None:
    access = tmp_path / "state" / "tailcat" / "service-access.json"
    manager = ServiceManager(
        orgfs=None,
        state=FakeStatePort(),
        persistence_late_result=None,
        controller_factory=lambda: FakeController(),
        access_path=access,
        scheduler=ManualScheduler(),
    )
    try:
        with caplog.at_level(logging.DEBUG):
            for _ in range(4):
                manager.reconcile()
        events = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("service.connect.")
        ]
        assert events.count("service.connect.access.invalid") == 1
        assert events.count("service.connect.reconcile.failed") == 0
        assert events.count("service.connect.reconcile.completed") == 1
        access_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "service.connect.access.invalid"
        )
        assert access_record.levelno <= logging.INFO
        reconcile_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "service.connect.reconcile.completed"
        )
        assert reconcile_record.levelno <= logging.INFO
    finally:
        manager.close(1.0)
