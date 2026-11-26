"""Red tests for when the recipient side derives: restart, periodic maintenance, cost.

Decision (service grant S5, 2026-11-26): derivation is re-run from the
documents on every maintenance tick of service-connect and again after a
daemon restart. A tick compares a cheap directory fingerprint first; when
nothing changed it reads no document, writes nothing and opens no credential.
A connection that this slice derived for a grant is withdrawn together with
the grant, also when the withdrawal is noticed only after a restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.service_connect import ServiceManager
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer, has_effective_grant
from tests.daemon.service_connect.support import FakeController, FakeStatePort, ManualScheduler
from tests.daemon.service_grants.consumer.support import (
    ORG,
    OTHER_ORG,
    PORT,
    Clock,
    Recorder,
    build_directory,
    device_row,
    grantor,
    leave_org,
)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    directory = build_directory(tmp_path)
    world = {
        **directory,
        "tmp": tmp_path,
        "clock": Clock(),
        "events": Recorder(),
        "state": FakeStatePort(),
        "controller": FakeController(),
        "access": tmp_path / "recipient-state" / "tailcat" / "service-access.json",
        "managers": [],
    }
    yield world
    for manager in world["managers"]:
        manager.close(2.0)
    directory["runtime"].close()


def alice(world: dict[str, Any]) -> ServiceGrantService:
    return grantor(world, world["clock"])


def consumer(world: dict[str, Any]) -> GrantConsumer:
    return GrantConsumer(
        orgfs=world["runtime"].facade,
        home=world["bob_home"],
        state_dir=world["bob_state"],
        owner="bob",
        device_id="device-b",
        now_ms=world["clock"],
        logger=world["events"],
    )


def start(world: dict[str, Any]) -> tuple[GrantConsumer, ServiceManager]:
    """One daemon start: a fresh consumer and a fresh manager over the same durable state."""

    source = consumer(world)
    service = ServiceManager(
        orgfs=world["runtime"].facade,
        state=world["state"],
        persistence_late_result=None,
        controller_factory=lambda: world["controller"],
        access_path=world["access"],
        scheduler=ManualScheduler(),
        derived_source=source,
    )
    world["managers"].append(service)
    world["controller"].calls.clear()
    return source, service


def tick(source: GrantConsumer, service: ServiceManager) -> None:
    source.refresh()
    service.reconcile()


def names(service: ServiceManager) -> list[str]:
    return [row["name"] for row in service.handle("service.list", {})["services"]]


def test_new_grants_are_followed_by_periodic_maintenance_without_restart(world: dict[str, Any]) -> None:
    source, service = start(world)
    tick(source, service)
    assert names(service) == []

    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    tick(source, service)

    service.handle("service.connect", {"name": "magpie"})
    assert names(service) == ["magpie"]
    alice(world).grant(org=ORG, port=PORT, device="@members", name="relay")
    tick(source, service)
    service.handle("service.connect", {"name": "relay"})
    assert names(service) == ["magpie", "relay"]


def test_restart_derives_again_and_restores_the_grant_connection(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world)
    tick(source, service)
    service.handle("service.connect", {"name": "magpie"})
    assert names(service) == ["magpie"]
    assert service.close(2.0) is True
    world["managers"].remove(service)
    world["controller"].services.clear()

    restarted_source, restarted = start(world)

    assert restarted.handle("service.guide", {"name": "magpie"})["entry"]["deviceId"] == "device-a"
    tick(restarted_source, restarted)
    assert [row["state"] for row in restarted.handle("service.list", {})["services"]] == ["listening"]
    assert world["controller"].services.keys() == {"magpie"}
    assert [call for call in world["state"].calls if call[0] == "set" and call[1][0] == "magpie"] == [
        ("set", ("magpie", 18080))
    ]


def test_restart_after_withdrawal_removes_the_connection_instead_of_failing_it(world: dict[str, Any]) -> None:
    made = alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world)
    tick(source, service)
    service.handle("service.connect", {"name": "magpie"})
    assert names(service) == ["magpie"]
    service.close(2.0)
    world["managers"].remove(service)
    alice(world).revoke(org=ORG, grant_id=made["grant"]["grantId"])

    restarted_source, restarted = start(world)
    tick(restarted_source, restarted)

    assert names(restarted) == []
    assert [call for call in world["state"].calls if call[0] == "remove"] == [("remove", "magpie")]
    assert made["grant"]["grantId"]


def test_connection_of_a_hand_placed_entry_whose_document_vanishes_keeps_its_failed_row(
    world: dict[str, Any],
) -> None:
    """Only connections this slice derived are withdrawn silently; the existing rule is unchanged."""

    from tests.daemon.service_connect.support import catalog_document, write_access

    runtime = world["runtime"]
    space = next(item for item in runtime.facade.spaces() if item.name == ORG)
    runtime.facade.write_text(space.space_id, "api.md", catalog_document())
    write_access(world["access"], space_id=space.space_id, owner="user:alice")
    source, service = start(world)
    tick(source, service)
    service.handle("service.connect", {"name": "api"})
    runtime.facade.remove(space.space_id, "api.md")
    tick(source, service)

    rows = service.handle("service.list", {})["services"]
    assert [(row["name"], row["state"], row["errorCode"]) for row in rows] == [
        ("api", "failed", "SERVICE_NOT_REGISTERED")
    ]


def test_unchanged_ticks_read_no_document(world: dict[str, Any]) -> None:
    alice(world).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    source, service = start(world)
    source.refresh()
    facade = world["runtime"].facade
    reads: list[str] = []
    original = facade.read_text

    def counting(space_id: str, ref: str) -> tuple[str, str]:
        reads.append(ref)
        return original(space_id, ref)

    object.__setattr__(facade, "read_text", counting)
    try:
        for _ in range(4):
            tick(source, service)
    finally:
        object.__setattr__(facade, "read_text", original)

    service.handle("service.connect", {"name": "magpie"})
    assert names(service) == ["magpie"]
    assert reads == []


def test_a_changed_directory_reloads_and_reads_only_what_exists(world: dict[str, Any]) -> None:
    for index in range(12):
        alice(world).grant(org=ORG, port=PORT, device="@members", name=f"svc-{index:02d}")
    source = consumer(world)
    source.refresh()
    facade = world["runtime"].facade
    log: list[str] = []
    original_listdir = facade.listdir
    original_read = facade.read_text

    def counting_listdir(space_id: str, ref: str, **_: object) -> tuple[object, ...]:
        log.append(f"listdir {ref}")
        return original_listdir(space_id, ref, **_)

    def counting_read(space_id: str, ref: str) -> tuple[str, str]:
        log.append(f"read {ref}")
        return original_read(space_id, ref)

    object.__setattr__(facade, "listdir", counting_listdir)
    object.__setattr__(facade, "read_text", counting_read)
    try:
        alice(world).grant(org=ORG, port=PORT, device="@members", name="svc-99")
        source.refresh()
    finally:
        object.__setattr__(facade, "listdir", original_listdir)
        object.__setattr__(facade, "read_text", original_read)

    assert sorted(source.entries())[-1] == "svc-99"
    assert sum(line.startswith("read ") and "service-grants" in line for line in log) == 13
    assert sum(line.startswith("listdir ") for line in log) <= 16


def test_an_unreadable_directory_keeps_the_last_snapshot_and_writes_nothing(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="bob/device-b", name="magpie")
    source, service = start(world)
    tick(source, service)
    service.handle("service.connect", {"name": "magpie"})
    facade = world["runtime"].facade
    original = facade.listdir

    def failing(*_: object, **__: object) -> tuple[object, ...]:
        raise OrgFsError("io", {"reason": "synthetic"})

    object.__setattr__(facade, "listdir", failing)
    try:
        tick(source, service)
    finally:
        object.__setattr__(facade, "listdir", original)

    assert names(service) == ["magpie"]
    assert service.handle("service.list", {})["services"][0]["state"] == "listening"
    assert world["events"].names().count("service.grant.rejected") == 0


def test_grants_are_collected_from_every_live_org_of_this_device(world: dict[str, Any]) -> None:
    runtime = world["runtime"]
    space = runtime.facade.create_space(OTHER_ORG)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    world["store"].ensure_org_directory_roots(OTHER_ORG)
    world["store"].put_device(OTHER_ORG, device_row("device-a", "alice"))
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    alice(world).grant(org=OTHER_ORG, port=PORT, device="@members", name="relay")
    source = consumer(world)
    service = ServiceManager(
        orgfs=runtime.facade,
        state=world["state"],
        persistence_late_result=None,
        controller_factory=lambda: world["controller"],
        access_path=world["access"],
        scheduler=ManualScheduler(),
        derived_source=source,
    )
    world["managers"].append(service)

    tick(source, service)

    assert sorted(source.entries()) == ["magpie", "relay"]
    assert [device.device_id for device in source.devices()] == ["device-a", "device-a"]

    leave_org(world["bob_home"], ORG)
    tick(source, service)

    assert sorted(source.entries()) == ["relay"]


def test_has_effective_grant_is_a_cheap_answer_and_treats_failure_as_no(world: dict[str, Any]) -> None:
    assert has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner="bob", device_id="device-b",
        now_ms=world["clock"],
    ) is False
    alice(world).grant(org=ORG, port=PORT, device="carol/device-c", name="magpie")
    cheap = has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner="bob", device_id="device-b",
        now_ms=world["clock"],
    )
    assert cheap is False

    made = alice(world).grant(org=ORG, port=PORT, device="@members", name="relay")
    assert has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner="bob", device_id="device-b",
        now_ms=world["clock"],
    ) is True
    alice(world).revoke(org=ORG, grant_id=made["grant"]["grantId"])
    assert has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner="bob", device_id="device-b",
        now_ms=world["clock"],
    ) is False
    assert has_effective_grant(
        orgfs=None, home=world["bob_home"], owner="bob", device_id="device-b", now_ms=world["clock"]
    ) is False
    assert has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner=None, device_id="device-b",
        now_ms=world["clock"],
    ) is False


def test_consumer_without_an_owner_never_touches_the_directory(world: dict[str, Any]) -> None:
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    counted: list[object] = []
    facade = world["runtime"].facade

    class Counting:
        def spaces(self) -> object:
            counted.append("spaces")
            return facade.spaces()

    GrantConsumer(
        orgfs=Counting(), home=world["bob_home"], state_dir=world["bob_state"], owner=None,
        device_id=None, now_ms=world["clock"], logger=world["events"],
    ).refresh()

    assert counted == []


def test_withdrawn_events_name_the_grant_without_private_material(world: dict[str, Any]) -> None:
    made = alice(world).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    source, service = start(world)
    tick(source, service)
    alice(world).revoke(org=ORG, grant_id=made["grant"]["grantId"])
    tick(source, service)

    text = world["events"].text()
    assert made["grant"]["grantId"] in text
    assert "nodekey:" not in text and "tcdevice" not in text
    assert set(world["events"].names()) <= {"service.grant.received", "service.grant.rejected"}
