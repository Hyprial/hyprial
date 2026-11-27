"""Red tests for when the recipient side derives: restart, periodic maintenance, cost.

Decision (service grant recipient side, 2026-11-26): derivation is re-run from the
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
from hyprial.daemon.impl.service_grants.consumer.startup import STARTUP_BUDGET_SECONDS
from tests.daemon.service_connect.support import FakeController, FakeStatePort
from tests.daemon.service_grants.consumer.support import (
    ORG,
    OTHER_ORG,
    OTHER_PORT,
    PORT,
    Clock,
    Recorder,
    as_author,
    build_directory,
    device_row,
    grantor,
    hand_placed,
    leave_org,
    service_manager,
)


@pytest.fixture
def world(tmp_path: Path, actor_host) -> dict[str, Any]:
    directory = build_directory(tmp_path)
    world = {
        **directory,
        "tmp": tmp_path,
        "clock": Clock(),
        "events": Recorder(),
        "state": FakeStatePort(),
        "controller": FakeController(),
        "access": tmp_path / "recipient-state" / "tailcat" / "service-access.json",
        "actor_host": actor_host,
        "managers": [],
    }
    yield world
    for manager in world["managers"]:
        manager.close(2.0)
    directory["runtime"].close()


def alice(world: dict[str, Any], *ports: int) -> ServiceGrantService:
    """Alice's grantor side, exposing ``ports`` besides the usual one.

    One grant supersedes her earlier grant of the same port to the same
    recipient, so grants that must coexist use distinct ports.
    """

    return grantor(world, world["clock"], ports=(PORT, OTHER_PORT, *ports))


def consumer(world: dict[str, Any], **overrides: Any) -> GrantConsumer:
    arguments: dict[str, Any] = {
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


def start(world: dict[str, Any]) -> tuple[GrantConsumer, ServiceManager]:
    """One daemon start: a fresh consumer and a fresh manager over the same durable state."""

    source = consumer(world)
    service = service_manager(world, source)
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
    alice(world, OTHER_PORT).grant(org=ORG, port=OTHER_PORT, device="@members", name="relay")
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
    sets = [call for call in world["state"].calls if call[0] == "set"]
    assert sets == [("set", ("magpie", 0)), ("set", ("magpie", 18080))]
    assert [call for call in world["state"].calls if call[0] == "remove"] == []


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

    space = hand_placed(world)
    source, service = start(world)
    tick(source, service)
    assert service.handle("service.connect", {"name": "api"})["service"]["state"] == "listening"
    world["runtime"].facade.remove(space.space_id, "api.md")
    tick(source, service)

    rows = service.handle("service.list", {})["services"]
    assert [(row["name"], row["state"], row["errorCode"]) for row in rows] == [
        ("api", "failed", "SERVICE_NOT_REGISTERED")
    ]
    assert [call for call in world["state"].calls if call[0] == "remove"] == []


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
    """The tick that notices a change reloads the whole org: every grant document and device row, once."""

    ports = list(range(8500, 8513))
    for port in ports[:12]:
        alice(world, *ports).grant(org=ORG, port=port, device="@members", name=f"svc-{port}")
    source = consumer(world)
    source.refresh()
    alice(world, *ports).grant(org=ORG, port=ports[12], device="@members", name="svc-new")
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
        log.clear()
        source.refresh()
    finally:
        object.__setattr__(facade, "listdir", original_listdir)
        object.__setattr__(facade, "read_text", original_read)

    assert sorted(item.name for item in source.entries())[-1] == "svc-new"
    assert len(source.entries()) == 13
    device_rows = len(world["store"].list_devices(ORG))
    assert sum(line.startswith("read id:") for line in log) == 13 + device_rows
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
    service = service_manager(world, source)
    world["managers"].append(service)

    tick(source, service)

    assert sorted(item.name for item in source.entries()) == ["magpie", "relay"]
    assert [device.device_id for device in source.devices()] == ["device-a"]

    leave_org(world["bob_home"], ORG)
    tick(source, service)

    assert sorted(item.name for item in source.entries()) == ["relay"]
    assert [device.device_id for device in source.devices()] == ["device-a"]


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


def test_the_startup_check_gives_up_once_its_budget_is_spent(world: dict[str, Any]) -> None:
    """A slow directory cannot hold daemon startup: past the budget the answer is "no".

    The question reads its clock once for the deadline, then before each org,
    the listing and every document read; after the told times the clock keeps
    telling the last one, so the second time stands for the whole question.
    """

    alice(world, OTHER_PORT).grant(org=ORG, port=OTHER_PORT, device="@members", name="relay")
    reads: list[str] = []
    facade = world["runtime"].facade
    original = facade.read_text

    def counting(space_id: str, ref: str) -> tuple[str, str]:
        reads.append(ref)
        return original(space_id, ref)

    object.__setattr__(facade, "read_text", counting)
    try:
        assert ask(world, [500.0, 500.1]) is True
        assert len(reads) == 1
        assert ask(world, [500.0, 500.0 + STARTUP_BUDGET_SECONDS + 1.0]) is False
    finally:
        object.__setattr__(facade, "read_text", original)

    assert len(reads) == 1


def ask(world: dict[str, Any], ticks: list[float]) -> bool:
    """Ask the startup question with a clock that tells the given times, then stays at the last."""

    moments = iter(ticks)
    last = ticks[-1]
    return has_effective_grant(
        orgfs=world["runtime"].facade, home=world["bob_home"], owner="bob", device_id="device-b",
        now_ms=world["clock"], monotonic=lambda: next(moments, last),
    )


def test_the_owner_may_come_as_the_principal_the_daemon_carries(world: dict[str, Any]) -> None:
    """The daemon's owner is ``user:<name>`` or bare; both mean the same member."""

    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    alice(world).grant(org=ORG, port=OTHER_PORT, device="bob/device-b", name="relay")

    for owner in ("bob", "user:bob"):
        source = consumer(world, owner=owner)
        source.refresh()
        assert sorted(item.name for item in source.entries()) == ["magpie", "relay"], owner
        assert has_effective_grant(
            orgfs=world["runtime"].facade, home=world["bob_home"], owner=owner,
            device_id="device-b", now_ms=world["clock"],
        ) is True, owner
    invalid = consumer(world, owner="not a principal")
    invalid.refresh()
    assert invalid.entries() == ()


def test_the_fingerprint_watches_the_tree_the_store_writes_grants_into(world: dict[str, Any]) -> None:
    """The consumer spells the grants path itself; it must be the store's own.

    A document the store writes there must change the fingerprint, so the next
    tick re-reads the org instead of keeping its answer.
    """

    import hyprial.daemon.impl.org.network.grant_docs as grant_docs_module
    import hyprial.daemon.impl.service_grants.consumer.maintenance as consumer_module

    assert consumer_module.GRANTS_DIR == grant_docs_module.SERVICE_GRANTS_DIR
    alice(world).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source = consumer(world)
    source.refresh()
    facade = world["runtime"].facade
    reads: list[str] = []
    original = facade.read_text

    def counting(space_id: str, ref: str) -> tuple[str, str]:
        reads.append(ref)
        return original(space_id, ref)

    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(
            ORG, "alice", "sg.aaaaaaaaaaaaaaaaaaaaaaaaaa", {"schemaVersion": 1}
        )
    object.__setattr__(facade, "read_text", counting)
    try:
        source.refresh()
    finally:
        object.__setattr__(facade, "read_text", original)

    assert reads
    assert [item.name for item in source.entries()] == ["magpie"]


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
    assert set(world["events"].names()) <= {
        "service.grant.received",
        "service.grant.rejected",
        "service.grant.withdrawn",
    }
    assert [row["reason"] for row in world["events"].of("service.grant.withdrawn")] == ["revoked"]
