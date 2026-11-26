"""Red tests for the recipient side of service grants: discovery, connect, withdrawal.

Decision (service grant S5, 2026-11-26): an effective grant whose recipient
is this device or ``@members`` becomes a second source for service-connect:
name, port, protocol and note come from the grant document, the server public
key and address from the grantor's device row. The result equals a hand-placed
catalog entry plus a protected record. Nothing derived is persisted by this
slice; only a connect intent is, by service-connect itself. A grant whose
``service.deviceId`` has no row under the grantor is discarded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_connect import ServiceConnectError, ServiceManager
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from hyprial.kernel import ipc_errors
from tests.daemon.service_connect.support import FakeController, FakeStatePort, ManualScheduler
from tests.daemon.service_grants.consumer.support import (
    ORG,
    PORT,
    Clock,
    Recorder,
    as_author,
    build_directory,
    device_row,
    grantor,
    leave_org,
)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    directory = build_directory(tmp_path)
    clock = Clock()
    events = Recorder()
    state = FakeStatePort()
    controller = FakeController()
    access = tmp_path / "recipient-home" / "state" / "tailcat" / "service-access.json"
    world = {
        **directory,
        "tmp": tmp_path,
        "clock": clock,
        "events": events,
        "state": state,
        "controller": controller,
        "access": access,
        "managers": [],
    }
    yield world
    for manager in world["managers"]:
        manager.close(2.0)
    directory["runtime"].close()


def alice(world: dict[str, Any]) -> ServiceGrantService:
    return grantor(world, world["clock"])


def consumer(world: dict[str, Any], **overrides: Any) -> GrantConsumer:
    arguments = {
        "orgfs": world["runtime"].facade,
        "home": world["bob_home"],
        "state_dir": world["bob_state"],
        "owner": "bob",
        "device_id": "device-b",
        "now_ms": world["clock"],
        "logger": world["events"],
    }
    arguments.update(overrides)
    return GrantConsumer(**arguments)


def manager(world: dict[str, Any], source: Any) -> ServiceManager:
    created = ServiceManager(
        orgfs=world["runtime"].facade,
        state=world["state"],
        persistence_late_result=None,
        controller_factory=lambda: world["controller"],
        access_path=world["access"],
        scheduler=ManualScheduler(),
        derived_source=source,
    )
    world["managers"].append(created)
    return created


def tick(world: dict[str, Any], source: GrantConsumer, service: ServiceManager) -> None:
    source.refresh()
    service.reconcile()


def test_effective_grant_is_listed_and_connects_like_a_hand_placed_entry(world: dict[str, Any]) -> None:
    made = alice(world).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie", protocol="http", note="Internal gateway"
    )
    source = consumer(world)
    source.refresh()
    service = manager(world, source)

    guide = service.handle("service.guide", {"name": "magpie"})["entry"]

    assert {key: guide[key] for key in ("name", "deviceId", "remotePort", "protocol", "localPort", "usage", "env")} == {
        "name": "magpie",
        "deviceId": "device-a",
        "remotePort": PORT,
        "protocol": "http",
        "localPort": None,
        "usage": "Internal gateway",
        "env": {},
    }
    assert "alice" in guide["guide"] and made["grant"]["grantId"] in guide["guide"]
    assert guide["auth"] and "nodekey" not in guide["auth"] + guide["guide"]
    assert service.handle("service.list", {})["services"] == []

    connected = service.handle("service.connect", {"name": "magpie"})

    assert connected["service"]["state"] == "listening"
    local_port = connected["service"]["localPort"]
    [mapped] = world["controller"].calls
    assert mapped == (
        "map",
        ("magpie", "device-a", "tcdeviceaPrivateAddressForTests", "nodekey:" + "61" * 32, PORT, 1, 0),
    )
    listed = service.handle("service.list", {})["services"]
    assert [(row["name"], row["state"], row["localPort"]) for row in listed] == [
        ("magpie", "listening", local_port)
    ]


def test_derived_entry_follows_the_grantor_device_row(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source = consumer(world)
    source.refresh()
    service = manager(world, source)
    service.handle("service.connect", {"name": "magpie"})
    world["controller"].calls.clear()
    store = world["store"]
    [row] = [item for item in store.list_devices(ORG) if item.device_id == "device-a"]
    with as_author(world["runtime"], "alice"):
        store.put_device(
            ORG,
            device_row("device-a", "alice", keys={"keyId": "x25519:unused", "publicKey": "AA"}),
        )

    tick(world, source, service)

    assert service.handle("service.list", {})["services"][0]["state"] == "listening"
    assert [call[0] for call in world["controller"].calls] == ["unmap", "map"]


def test_revoked_or_expired_grant_removes_the_entry_and_tears_the_mapping_down(world: dict[str, Any]) -> None:
    service_grant = alice(world)
    made = service_grant.grant(org=ORG, port=PORT, device="bob/device-b", name="magpie")
    grant_id = made["grant"]["grantId"]
    source = consumer(world)
    service = manager(world, source)
    tick(world, source, service)
    service.handle("service.connect", {"name": "magpie"})
    assert service.handle("service.guide", {"name": "magpie"})["entry"]["name"] == "magpie"

    service_grant.revoke(org=ORG, grant_id=grant_id)
    tick(world, source, service)

    with pytest.raises(ServiceConnectError) as unknown:
        service.handle("service.guide", {"name": "magpie"})
    assert unknown.value.code == ipc_errors.SERVICE_NOT_REGISTERED
    assert service.handle("service.list", {})["services"] == []
    assert world["controller"].services == {}
    assert [call for call in world["state"].calls if call[0] == "remove"] == [("remove", "magpie")]

    expiring = alice(world).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        expires_at_ms=world["clock"].value + 60_000,
    )
    tick(world, source, service)
    service.handle("service.connect", {"name": "magpie"})
    world["clock"].value += 61_000
    tick(world, source, service)

    assert service.handle("service.list", {})["services"] == []
    assert world["controller"].services == {}
    assert expiring["grant"]["grantId"] != grant_id


def test_removed_grantor_device_row_withdraws_the_derived_entry(world: dict[str, Any]) -> None:
    alice(world).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    source = consumer(world)
    service = manager(world, source)
    tick(world, source, service)
    service.handle("service.connect", {"name": "magpie"})
    assert source.entries()

    world["store"].remove_device(ORG, "device-a", owner="alice")
    tick(world, source, service)

    assert source.entries() == {}
    assert service.handle("service.list", {})["services"] == []
    assert world["controller"].services == {}


def test_grant_for_a_device_absent_from_the_grantor_is_discarded(world: dict[str, Any]) -> None:
    grant_id = alice(world).grant(org=ORG, port=PORT, device="bob/device-b", name="magpie")["grant"]["grantId"]
    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None
    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(
            ORG, "alice", grant_id, {**raw, "service": {**raw["service"], "deviceId": "device-ghost"}}
        )
    source = consumer(world)

    source.refresh()

    assert source.entries() == {}
    assert source.devices() == ()
    assert world["events"].of("service.grant.rejected") == [
        {"org": ORG, "grantId": grant_id, "grantor": "alice", "reason": "service-device-unknown"}
    ]


def test_members_grant_connects_without_a_key(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source = consumer(world)
    service = manager(world, source)
    tick(world, source, service)

    service.handle("service.connect", {"name": "magpie"})

    assert service.handle("service.list", {})["services"][0]["state"] == "listening"
    assert list((world["bob_home"] / "secrets" / "providers").glob("*.json")) == []


def test_members_grant_carrying_a_credential_is_never_opened(world: dict[str, Any]) -> None:
    made = alice(world).grant(org=ORG, port=PORT, device="bob/device-b", name="magpie", credential={"entryId": "gateway"})
    raw = world["store"].read_service_grant(ORG, "alice", made["grant"]["grantId"])
    assert raw is not None
    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(ORG, "alice", made["grant"]["grantId"], {**raw, "recipient": "@members"})
    source = consumer(world)
    service = manager(world, source)

    tick(world, source, service)

    assert source.entries()
    assert list((world["bob_home"] / "secrets" / "providers").glob("*.json")) == []


def test_hand_placed_sources_win_and_a_colliding_derived_name_is_renamed(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    hand_placed = {
        "name": "magpie",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "protocol": "http",
        "localPort": 18080,
        "usage": "claude-code-baseurl",
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:{port}"},
        "auth": "request a key separately",
        "guide": "Use the printed port.",
    }
    runtime = world["runtime"]
    space = next(item for item in runtime.facade.spaces() if item.name == ORG)
    from tests.daemon.service_connect.support import catalog_document, write_access

    runtime.facade.write_text(space.space_id, "magpie.md", catalog_document())
    write_access(world["access"], space_id=space.space_id, owner="user:alice")
    source = consumer(world)
    service = manager(world, source)

    tick(world, source, service)

    guide = service.handle("service.guide", {"name": "magpie-alice"})["entry"]
    assert (guide["deviceId"], guide["remotePort"], guide["protocol"]) == ("device-a", PORT, "tcp")
    assert service.handle("service.guide", {"name": "magpie"})["entry"]["deviceId"] == "sub2api"
    assert hand_placed["name"] == "magpie"
    service.handle("service.connect", {"name": "magpie-alice"})
    service.handle("service.connect", {"name": "magpie"})
    assert sorted(row["name"] for row in service.handle("service.list", {})["services"]) == ["magpie", "magpie-alice"]


def test_hand_placed_record_and_catalog_are_unchanged_by_an_empty_grant_set(world: dict[str, Any]) -> None:
    from tests.daemon.service_connect.support import catalog_document, write_access

    runtime = world["runtime"]
    space = next(item for item in runtime.facade.spaces() if item.name == ORG)
    runtime.facade.write_text(space.space_id, "api.md", catalog_document())
    write_access(world["access"], space_id=space.space_id, owner="user:alice")
    before = world["access"].read_bytes()
    source = consumer(world)
    service = manager(world, source)

    tick(world, source, service)
    result = service.handle("service.connect", {"name": "api"})

    assert result["service"]["state"] == "listening"
    assert world["access"].read_bytes() == before
    assert [row["name"] for row in service.handle("service.list", {})["services"]] == ["api"]


def test_grant_of_another_recipient_is_not_derived(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="carol/device-c", name="magpie")
    source = consumer(world)
    source.refresh()

    assert source.entries() == {}


def test_leaving_the_org_withdraws_the_entry(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source = consumer(world)
    service = manager(world, source)
    tick(world, source, service)
    service.handle("service.connect", {"name": "magpie"})

    leave_org(world["bob_home"])
    tick(world, source, service)

    assert source.entries() == {}
    assert service.handle("service.list", {})["services"] == []


def test_unnamed_grant_has_no_connectable_entry(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members")
    source = consumer(world)

    source.refresh()

    assert source.entries() == {}
    assert world["events"].names().count("service.grant.rejected") == 1
