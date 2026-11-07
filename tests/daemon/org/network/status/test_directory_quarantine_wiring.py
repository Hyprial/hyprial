"""Regression coverage for the daemon-lifetime invalid-device quarantine."""

from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import traceback

import pytest

from hyprial.daemon.impl.application.messaging.identity_binding import (
    _IdentityBindingMixin,
)
from hyprial.daemon.impl.composition import links as links_composition
from hyprial.daemon.impl.composition.links import OrgLinkSpaces, SPACE_REFRESH_MS
from hyprial.daemon.impl.org.network import directory as directory_module
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
    network_directory_projection,
)
from hyprial.daemon.impl.org.network.ipc import _build_service, directory_peers_for_app
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import DirectoryDevice
from hyprial.kernel import ORGFS_CONTENT_PENDING
from tests.support.links.org import FakeClock, LogRecorder


@pytest.fixture
def runtime(tmp_path: Path) -> OrgFsRuntime:
    value = OrgFsRuntime(
        tmp_path / "state", node_id="node-a", author="user:owner@example"
    )
    yield value
    value.close()


def _invalid_device(runtime: OrgFsRuntime) -> None:
    space = runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    store.put_device(
        "group-acme",
        DirectoryDevice(
            device_id="broken",
            owner="owner@example",
            server_public="nodekey:server-broken",
            client_public="nodekey:client-broken",
            address="tcp://127.0.0.1:7447#broken-psk",
            key_generation=1,
            updated_at="2026-10-20T00:00:00+00:00",
        ),
    )
    runtime.facade.write_text(
        space.space_id,
        "directory/devices/owner@example/broken.json",
        "not json",
    )


def test_org_link_spaces_threads_one_quarantine_across_resolutions(
    runtime: OrgFsRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _invalid_device(runtime)
    quarantine = InvalidDeviceQuarantine()
    logs = LogRecorder()
    real_store = OrgFsDirectoryStore

    class ScanningStore:
        def __init__(self, fs, **kwargs):  # type: ignore[no-untyped-def]
            self._store = real_store(fs, **kwargs)

        def orgs(self) -> list[str]:
            self._store.list_devices("group-acme")
            return self._store.orgs()

        def org_state(self, org: str) -> str:
            return self._store.org_state(org)

    monkeypatch.setattr(links_composition, "OrgFsDirectoryStore", ScanningStore)
    clock = FakeClock()
    spaces = OrgLinkSpaces(
        runtime.facade,
        home=tmp_path,
        clock_ms=clock,
        logger=logs,
        invalid_devices=quarantine,
    )

    spaces()
    clock.advance(SPACE_REFRESH_MS)
    spaces()

    assert logs.count("directory.device_invalid") == 1


def test_identity_binding_directory_reuses_application_quarantine(
    runtime: OrgFsRuntime,
) -> None:
    _invalid_device(runtime)
    logs = LogRecorder()

    class App(_IdentityBindingMixin):
        pass

    app = App()
    app._org_directory_quarantine = InvalidDeviceQuarantine()
    app._require_orgfs_runtime = lambda: SimpleNamespace(facade=runtime.facade)
    app._log = logs

    app._org_binding_directory().list_devices("group-acme")
    app._org_binding_directory().list_devices("group-acme")

    assert logs.count("directory.device_invalid") == 1


def test_network_service_default_store_reuses_injected_quarantine(
    runtime: OrgFsRuntime,
    tmp_path: Path,
) -> None:
    _invalid_device(runtime)
    quarantine = InvalidDeviceQuarantine()
    logs = LogRecorder()

    for _ in range(2):
        service = OrgNetworkService(
            fs=runtime.facade,
            home=tmp_path,
            node_id="other-node",
            owner="owner@example",
            invalid_devices=quarantine,
            logger=logs,
        )
        assert service._store.list_devices("group-acme") == []

    assert logs.count("directory.device_invalid") == 1


def test_versionless_invalid_row_is_quarantined_before_second_parse(
    runtime: OrgFsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    space = runtime.facade.create_space("group-acme")
    setup = OrgFsDirectoryStore(runtime.facade)
    setup.ensure_org_directory_roots("group-acme")
    setup.put_device(
        "group-acme",
        DirectoryDevice(
            device_id="broken-time",
            owner="owner@example",
            server_public="nodekey:server-broken-time",
            client_public="nodekey:client-broken-time",
            address="tcp://127.0.0.1:7447#broken-time-psk",
            key_generation=1,
            updated_at="2026-10-20T00:00:00+00:00",
        ),
    )
    path = "directory/devices/owner@example/broken-time.json"
    payload = json.dumps(
        {
            **DirectoryDevice(
                device_id="broken-time",
                owner="owner@example",
                server_public="nodekey:server-broken-time",
                client_public="nodekey:client-broken-time",
                address="tcp://127.0.0.1:7447#broken-time-psk",
                key_generation=1,
                updated_at="2026-10-20T00:00:00+00:00",
            ).as_record(),
            "updatedAt": "not-an-iso8601-time",
        }
    )
    runtime.facade.write_text(space.space_id, path, payload)
    original_listdir = runtime.facade.listdir
    original_read = runtime.facade.read_text

    def versionless_listdir(space_id: str, node: str):
        return tuple(
            replace(entry, version="") if entry.name == "broken-time.json" else entry
            for entry in original_listdir(space_id, node)
        )

    def versionless_read(space_id: str, node: str):
        text, version = original_read(space_id, node)
        return (text, "") if text == payload else (text, version)

    parse_count = 0
    original_loads = directory_module.json.loads

    def counting_loads(value, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal parse_count
        if value == payload:
            parse_count += 1
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(runtime.facade, "listdir", versionless_listdir)
    monkeypatch.setattr(runtime.facade, "read_text", versionless_read)
    monkeypatch.setattr(directory_module.json, "loads", counting_loads)
    quarantine = InvalidDeviceQuarantine()
    logs = LogRecorder()

    for _ in range(2):
        assert OrgFsDirectoryStore(
            runtime.facade,
            invalid_devices=quarantine,
            logger=logs,
        ).list_devices("group-acme") == []

    assert parse_count == 1
    assert logs.count("directory.device_invalid") == 1


def test_versionless_content_pending_row_skips_second_quarantine_note(
    runtime: OrgFsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _invalid_device(runtime)
    info = runtime.facade.stat(
        runtime.facade.spaces()[0].space_id,
        "directory/devices/owner@example/broken.json",
    )
    original_listdir = runtime.facade.listdir
    original_read = runtime.facade.read_text

    def versionless_listdir(space_id: str, node: str):
        return tuple(
            replace(entry, version="") if entry.node_id == info.node_id else entry
            for entry in original_listdir(space_id, node)
        )

    def pending_read(space_id: str, node: str):
        if node.removeprefix("id:") == info.node_id:
            raise OrgFsError(ORGFS_CONTENT_PENDING, {"node": node})
        return original_read(space_id, node)

    notes = 0
    original_note = OrgFsDirectoryStore._note_invalid_device

    def counting_note(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal notes
        notes += 1
        return original_note(self, *args, **kwargs)

    monkeypatch.setattr(runtime.facade, "listdir", versionless_listdir)
    monkeypatch.setattr(runtime.facade, "read_text", pending_read)
    monkeypatch.setattr(OrgFsDirectoryStore, "_note_invalid_device", counting_note)
    quarantine = InvalidDeviceQuarantine()
    logs = LogRecorder()

    for _ in range(2):
        assert OrgFsDirectoryStore(
            runtime.facade,
            invalid_devices=quarantine,
            logger=logs,
        ).list_devices("group-acme") == []

    assert notes == 1
    assert logs.count("directory.device_invalid") == 1


def test_real_composition_quarantines_three_versionless_production_shapes(
    runtime: OrgFsRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    space = runtime.facade.create_space("group-acme")
    setup = OrgFsDirectoryStore(runtime.facade)
    setup.ensure_org_directory_roots("group-acme")
    device_ids = ("autodl-container", "MacBookPro0.local", "Dev000000000000000002.local")
    for device_id in (*device_ids, "healthy-peer"):
        setup.put_device(
            "group-acme",
            DirectoryDevice(
                device_id=device_id,
                owner="owner@example",
                server_public=f"nodekey:server-{device_id}",
                client_public=f"nodekey:client-{device_id}",
                address=f"tcp://127.0.0.1:7447#{device_id}-psk",
                key_generation=1,
                updated_at="2026-10-20T00:00:00+00:00",
            ),
        )
    invalid_time_path = "directory/devices/owner@example/autodl-container.json"
    invalid_time = json.dumps(
        {
            **DirectoryDevice(
                device_id="autodl-container",
                owner="owner@example",
                server_public="nodekey:server-autodl-container",
                client_public="nodekey:client-autodl-container",
                address="tcp://127.0.0.1:7447#autodl-container-psk",
                key_generation=1,
                updated_at="2026-10-20T00:00:00+00:00",
            ).as_record(),
            "updatedAt": "2026-10-197T005:52:31.7934582.438541+00:00",
        }
    )
    runtime.facade.write_text(space.space_id, invalid_time_path, invalid_time)
    bad_nodes = {
        runtime.facade.stat(
            space.space_id,
            f"directory/devices/owner@example/{device_id}.json",
        ).node_id: device_id
        for device_id in device_ids
    }
    original_listdir = runtime.facade.listdir
    original_read = runtime.facade.read_text

    def versionless_listdir(space_id: str, node: str):
        return tuple(
            replace(entry, version="") if entry.node_id in bad_nodes else entry
            for entry in original_listdir(space_id, node)
        )

    def production_read(space_id: str, node: str):
        device_id = bad_nodes.get(node.removeprefix("id:"))
        if device_id in {"MacBookPro0.local", "Dev000000000000000002.local"}:
            raise OrgFsError(ORGFS_CONTENT_PENDING, {"node": node})
        text, version = original_read(space_id, node)
        return (text, "") if device_id == "autodl-container" else (text, version)

    parses = 0
    original_loads = directory_module.json.loads

    def counting_loads(value, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal parses
        if value == invalid_time:
            parses += 1
        return original_loads(value, *args, **kwargs)

    observations: list[dict[str, object]] = []
    original_note = OrgFsDirectoryStore._note_invalid_device

    def observed_note(self, space_id, path, text, error, **kwargs):  # type: ignore[no-untyped-def]
        fingerprint = self._device_fingerprint(
            version=kwargs.get("version", ""), text=text, error=error
        )
        observations.append(
            {
                "key": (space_id, path),
                "fingerprint": fingerprint,
                "previousIsNone": (space_id, path) not in self._invalid_device_quiet,
                "quarantineId": id(self._invalid_device_quiet),
                "storeId": id(self),
                "callers": tuple(frame.name for frame in traceback.extract_stack(limit=8)),
            }
        )
        return original_note(self, space_id, path, text, error, **kwargs)

    monkeypatch.setattr(runtime.facade, "listdir", versionless_listdir)
    monkeypatch.setattr(runtime.facade, "read_text", production_read)
    monkeypatch.setattr(directory_module.json, "loads", counting_loads)
    monkeypatch.setattr(OrgFsDirectoryStore, "_note_invalid_device", observed_note)
    logs = LogRecorder()
    home = tmp_path / "home"
    app = SimpleNamespace(
        hyprial_home=home,
        node_id="local-node",
        owner="owner@example",
        _org_directory_quarantine=InvalidDeviceQuarantine(),
        _orgfs_runtime=runtime,
        _org_dial_state=None,
        _log=logs,
        _require_orgfs_runtime=lambda: runtime,
        _start_forwarding_supervisor=lambda: None,
        _reconcile_forwarding_endpoints=lambda: None,
        _request_forwarding_reconcile=lambda reason, **fields: True,
        _await_forwarding_reconcile=lambda reason, **fields: None,
        _forwarding_status_json=lambda: {},
    )

    store_ids: set[int] = set()
    quarantine_ids: set[int] = set()
    for _ in range(3):
        directory_peers_for_app(app)
        service = _build_service(app)
        store_ids.add(id(service._store))
        quarantine_ids.add(id(service._store._invalid_device_quiet))
        network_directory_projection(
            service._store,
            ["group-acme"],
            set(),
            device_view=lambda device: device.as_record(),
            logger=logs,
        )

    assert parses == 1
    assert logs.count("directory.device_invalid") == 3
    assert len(store_ids) == 3
    assert quarantine_ids == {id(app._org_directory_quarantine.entries)}
    assert len(observations) == 3
    assert all(item["previousIsNone"] is True for item in observations)
    assert all("_scan" in item["callers"] for item in observations)
    assert {
        fields["path"].rsplit("/", 1)[-1]
        for fields in logs.fields_for("directory.device_invalid")
    } == {f"{device_id}.json" for device_id in device_ids}


def test_every_product_directory_store_construction_injects_quarantine() -> None:
    root = Path(__file__).parents[5] / "src"
    omissions: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name != "OrgFsDirectoryStore":
                continue
            if not any(keyword.arg == "invalid_devices" for keyword in node.keywords):
                omissions.append(f"{path.relative_to(root)}:{node.lineno}")

    assert omissions == []
