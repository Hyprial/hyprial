from __future__ import annotations

import json
import os

import pytest

from hyprial.daemon.impl.service_connect import ProtectedResolver, ServiceConnectError

from .support import ADDRESS, SERVER_PUBLIC, write_access


def test_protected_record_resolves_exact_device_port_without_repr_leak(tmp_path) -> None:
    path = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(path, space_id="space-services")
    resolver = ProtectedResolver(path)

    snapshot = resolver.load()
    record = snapshot.resolve_device("sub2api", 8080)
    assert record.device_id == "sub2api"
    assert record.server_public == SERVER_PUBLIC
    assert record.address == ADDRESS
    assert record.generation == 7
    assert ADDRESS not in repr(record)
    assert SERVER_PUBLIC not in repr(record)
    assert snapshot.catalog_trust is not None
    assert snapshot.catalog_trust.space_id == "space-services"


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda value: value["devices"][0].update(enabled=False), "SERVICE_DEVICE_UNAVAILABLE"),
        (lambda value: value["devices"][0].update(ports=[9000]), "SERVICE_NOT_AUTHORIZED"),
        (lambda value: value["devices"].append(dict(value["devices"][0])), "SERVICE_REGISTRY_INVALID"),
    ],
)
def test_disabled_denied_and_duplicate_records_fail_closed(tmp_path, mutate, code) -> None:
    path = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    resolver = ProtectedResolver(path)

    with pytest.raises(ServiceConnectError) as raised:
        resolver.load().resolve_device("sub2api", 8080)
    assert raised.value.code == code
    assert ADDRESS not in str(raised.value)


def test_permissions_owner_symlink_and_duplicate_json_keys_are_rejected(tmp_path) -> None:
    path = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(path)
    path.chmod(0o644)
    with pytest.raises(ServiceConnectError, match="protected service access unavailable"):
        ProtectedResolver(path).load()

    path.chmod(0o600)
    target = path.with_name("real.json")
    path.replace(target)
    path.symlink_to(target.name)
    with pytest.raises(ServiceConnectError, match="protected service access unavailable"):
        ProtectedResolver(path).load()

    path.unlink()
    target.replace(path)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ServiceConnectError) as raised:
        ProtectedResolver(path).load()
    assert raised.value.code == "SERVICE_REGISTRY_INVALID"

    assert os.getuid() == path.stat().st_uid


def test_same_generation_changed_material_is_rejected_but_new_generation_is_allowed(tmp_path) -> None:
    path = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(path)
    resolver = ProtectedResolver(path)
    resolver.load()
    value = json.loads(path.read_text(encoding="utf-8"))
    value["devices"][0]["address"] = "tcChangedPrivateAddressForTestsOnly"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ServiceConnectError, match="revision is inconsistent"):
        resolver.load()

    value["devices"][0]["generation"] = 8
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    assert resolver.load().resolve_device("sub2api", 8080).generation == 8
