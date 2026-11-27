"""Two cross-source rules at the seam between hand-placed records and derived grants.

Decision (service grant S5 fix, 2026-11-27, critic 2): a hand-placed protected
record and a grantor's device row may share a ``deviceId``. When they agree
about server public key, address and generation they are one device and are
merged as before. When they differ, neither may silently win: every grant that
depends on that device is rejected with ``device-conflict``, nothing of the
derived device is used, and the hand-placed record keeps working alone.

Decision (service grant S5 fix, 2026-11-27, critic 4): the alternative name of
a derived entry must differ from its own name and be a valid service name,
whatever the service name and the grantor are: at the 63-character limit the
name is shortened to make room, and a grantor with no slug character (the
models allow any non-control Unicode owner) gets a stable digest instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_connect import ServiceConnectError
from hyprial.daemon.impl.service_connect.derived import alternative_name
from hyprial.daemon.impl.service_connect.models import CatalogEntry
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from hyprial.kernel import ipc_errors
from tests.daemon.service_connect.support import write_access
from tests.daemon.service_grants.consumer.support import (
    ORG,
    PORT,
    as_author,
    device_row,
    grantor,
    hand_placed,
)
from tests.daemon.service_grants.consumer_fixes.support import (
    close,
    consumer_arguments,
    manager_for,
    names,
    reasons,
    world_at,
)

GRANTOR_ROW = device_row("device-a", "alice")


def _device(**changes: Any) -> dict[str, object]:
    return {
        "deviceId": "device-a",
        "serverPublic": GRANTOR_ROW.server_public,
        "address": GRANTOR_ROW.address,
        "generation": GRANTOR_ROW.key_generation,
        "ports": [PORT],
        "enabled": True,
        **changes,
    }


@pytest.fixture
def world(tmp_path: Path, actor_host) -> dict[str, Any]:
    made = world_at(tmp_path, actor_host)
    yield made
    close(made)


def start(world: dict[str, Any], access_devices: list[dict[str, object]] | None) -> tuple[GrantConsumer, Any]:
    if access_devices is not None:
        write_access(world["access"], devices=access_devices)
    source = GrantConsumer(**consumer_arguments(world))
    service = manager_for(world, source)
    return source, service


def test_an_agreeing_hand_placed_record_and_device_row_are_one_device(world: dict[str, Any]) -> None:
    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, [_device()])

    source.refresh()
    service.reconcile()
    connected = service.handle("service.connect", {"name": "magpie"})

    assert connected["service"]["state"] == "listening"
    assert world["events"].of("service.grant.rejected") == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"serverPublic": "nodekey:" + "9" * 64}, id="server-public"),
        pytest.param({"address": "tcHandPlacedAddressForTests"}, id="address"),
        pytest.param({"generation": 5}, id="generation"),
    ],
)
def test_a_disagreeing_hand_placed_record_rejects_the_grant_instead_of_winning(
    world: dict[str, Any], change: dict[str, object]
) -> None:
    """Before the fix the derived entry stayed and was mapped to the hand-placed device."""

    made = grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, [_device(**change)])

    source.refresh()
    service.reconcile()

    assert source.entries() == ()
    assert reasons(world) == [(made["grant"]["grantId"], "device-conflict")]
    with pytest.raises(ServiceConnectError) as unknown:
        service.handle("service.connect", {"name": "magpie"})
    assert unknown.value.code == ipc_errors.SERVICE_NOT_REGISTERED
    assert world["controller"].calls == []


def test_only_the_grants_of_the_contested_device_are_rejected(world: dict[str, Any]) -> None:
    """A conflict on one device id leaves the other grantor's device alone."""

    with as_author(world["runtime"], "carol"):
        world["store"].put_device(ORG, device_row("device-c", "carol"))
    contested = grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    with as_author(world["runtime"], "carol"):
        world["store"].put_service_grant(
            ORG, "carol", "sg.cccccccccccccccccccccccccc",
            {
                "schemaVersion": 1,
                "grantId": "sg.cccccccccccccccccccccccccc",
                "service": {"deviceId": "device-c", "port": PORT, "name": "relay", "protocol": "tcp", "note": None},
                "recipient": "@members",
                "grantedAtMs": 1,
                "expiresAtMs": None,
                "revokedAtMs": None,
                "credential": None,
            },
        )
    source, service = start(world, [_device(serverPublic="nodekey:" + "9" * 64)])

    source.refresh()
    service.reconcile()

    assert [item.name for item in source.entries()] == ["relay"]
    assert reasons(world) == [(contested["grant"]["grantId"], "device-conflict")]
    assert service.handle("service.connect", {"name": "relay"})["service"]["state"] == "listening"
    with pytest.raises(ServiceConnectError):
        service.handle("service.connect", {"name": "magpie"})


def test_the_hand_placed_record_keeps_working_through_the_conflict(world: dict[str, Any]) -> None:
    """A hand-placed entry on the contested device connects to the record's own device, as before."""

    from tests.daemon.service_connect.support import catalog_document

    space = hand_placed(world, name="api")
    world["runtime"].facade.write_text(
        space.space_id, "edge.md", catalog_document(name="edge", device_id="device-a", remote_port=8080)
    )
    write_access(
        world["access"],
        devices=[{**_device(serverPublic="nodekey:" + "9" * 64), "ports": [8080]}],
        space_id=space.space_id,
        owner=space.owner,
    )
    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, None)

    source.refresh()
    service.reconcile()
    connected = service.handle("service.connect", {"name": "edge"})

    assert connected["service"]["state"] == "listening"
    [mapped] = world["controller"].calls
    assert mapped[1][2:4] == (GRANTOR_ROW.address, "nodekey:" + "9" * 64)
    assert len(reasons(world)) == 1 and reasons(world)[0][1] == "device-conflict"


def test_a_conflict_that_appears_later_disconnects_the_derived_mapping(world: dict[str, Any]) -> None:
    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, None)
    source.refresh()
    service.reconcile()
    service.handle("service.connect", {"name": "magpie"})
    assert names(service) == ["magpie"]

    write_access(world["access"], devices=[_device(address="tcHandPlacedAddressForTests")])
    source.refresh()
    service.reconcile()

    assert names(service) == []
    assert world["controller"].services == {}
    assert [call for call in world["state"].calls if call[0] == "remove"] == [("remove", "magpie")]
    assert [row["reason"] for row in world["events"].of("service.grant.rejected")] == ["device-conflict"]


def test_a_conflict_that_is_resolved_lets_the_grant_back_in(world: dict[str, Any]) -> None:
    """The operator removes the disagreeing record; the grant is back on the next read.

    Rewriting the record at the same generation is not a way out: service-connect
    already refuses a record whose device changes without a new generation.
    """

    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, [_device(address="tcHandPlacedAddressForTests")])
    source.refresh()
    service.reconcile()
    assert source.entries() == ()

    world["access"].unlink()
    source.refresh()
    service.reconcile()

    assert [item.name for item in source.entries()] == ["magpie"]
    assert service.handle("service.connect", {"name": "magpie"})["service"]["state"] == "listening"


def test_a_derived_connection_of_the_contested_device_is_taken_down(world: dict[str, Any]) -> None:
    """The record arriving later must not leave a live mapping to the grantor's device either."""

    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="magpie")
    source, service = start(world, None)
    source.refresh()
    service.reconcile()
    service.handle("service.connect", {"name": "magpie"})

    write_access(world["access"], devices=[_device(serverPublic="nodekey:" + "9" * 64)])
    source.refresh()
    service.reconcile()

    assert world["controller"].services == {}
    assert [call[0] for call in world["controller"].calls] == ["map", "unmap"]


class _Source:
    """The two calls the device seam makes, for a source of any kind."""

    def __init__(self, devices: tuple[Any, ...]) -> None:
        self._devices = devices
        self.told: list[tuple[str, ...]] = []

    def devices(self) -> tuple[Any, ...]:
        return self._devices

    def contested(self, device_ids: tuple[str, ...]) -> None:
        self.told.append(device_ids)


def test_the_seam_keeps_the_record_drops_the_disagreeing_derived_device_and_tells_the_source() -> None:
    """The rule lives in service-connect's seam, so a source of any kind meets it."""

    from hyprial.daemon.impl.service_connect.derived import with_derived_devices
    from hyprial.daemon.impl.service_connect.models import AccessSnapshot, ProtectedDevice

    record = ProtectedDevice.from_json({**_device(), "ports": [8080]})
    disagreeing = ProtectedDevice.from_json(_device(address="tcSomewhereElseForTests"))
    other = ProtectedDevice.from_json({**_device(), "deviceId": "device-c"})
    source = _Source((disagreeing, other))

    merged = with_derived_devices(AccessSnapshot(None, (record,)), source)

    assert {device.device_id: device for device in merged.devices} == {"device-a": record, "device-c": other}
    assert source.told == [("device-a",)]


def test_the_seam_merges_an_agreeing_device_and_tells_the_source_nothing_is_contested() -> None:
    from hyprial.daemon.impl.service_connect.derived import derived_access, with_derived_devices
    from hyprial.daemon.impl.service_connect.models import AccessSnapshot, ProtectedDevice

    record = ProtectedDevice.from_json({**_device(), "ports": [8080]})
    agreeing = ProtectedDevice.from_json(_device())
    source = _Source((agreeing,))

    merged = with_derived_devices(AccessSnapshot(None, (record,)), source)
    absent = derived_access(source, Path("/nonexistent-for-tests/service-access.json"))

    assert merged.devices == (record,)
    assert absent is not None and absent.devices == (agreeing,)
    assert source.told == [(), ()]


def test_the_alternative_name_always_differs_and_stays_a_valid_service_name() -> None:
    import re

    rule = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")

    cases = [
        ("magpie", "alice"),
        ("a" * 63, "alice"),
        ("a" * 62, "alice"),
        ("a" * 63, "bo"),
        ("svc", "中文"),
        ("svc", "---"),
        ("svc", "é"),
        ("svc", ""),
        ("a" * 63, "中文"),
        ("a" * 63, ""),
        ("a" * 63, "@admin"),
        ("a", "a" * 200),
    ]
    for name, grantor_name in cases:
        alternative = alternative_name(name, grantor_name)

        assert alternative != name, (name, grantor_name, alternative)
        assert rule.fullmatch(alternative) is not None, (name, grantor_name, alternative)
        assert len(alternative) <= 63


def test_the_alternative_name_keeps_the_grantor_readable_and_is_stable() -> None:
    assert alternative_name("magpie", "alice") == "magpie-alice"
    assert alternative_name("magpie", "Alice") == "magpie-alice"
    assert alternative_name("svc", "中文") == alternative_name("svc", "中文")
    assert alternative_name("svc", "中文") != alternative_name("svc", "日本語")
    assert alternative_name("svc", "---") != alternative_name("svc", "中文")
    assert alternative_name("a" * 63, "alice").startswith("a" * 56)
    assert alternative_name("a" * 63, "alice").endswith("-alice")
    assert alternative_name("a" * 63, "b") != alternative_name("a" * 63, "c")


def test_the_alternative_name_differs_even_when_the_name_already_ends_with_the_grantor() -> None:
    """Truncating ``x...x-alice`` to make room for ``-alice`` would give the name back."""

    name = "x" * 57 + "-alice"

    assert len(name) == 63
    assert alternative_name(name, "alice") != name
    assert len(alternative_name(name, "alice")) <= 63


def test_a_long_service_name_colliding_with_a_hand_placed_one_still_connects_under_its_alternative(
    world: dict[str, Any],
) -> None:
    long_name = "a" * 63
    hand_placed(world, name=long_name)
    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name=long_name)
    source, service = start(world, None)

    source.refresh()
    service.reconcile()

    listed = sorted(names_after_connect(service, source))
    assert len(listed) == 2
    derived = [item for item in source.entries() if item.name == long_name][0]
    assert derived.alternative != long_name
    assert service.handle("service.connect", {"name": derived.alternative})["service"]["state"] == "listening"


def names_after_connect(service: Any, source: GrantConsumer) -> list[str]:
    [item] = source.entries()
    service.handle("service.connect", {"name": item.name})
    service.handle("service.connect", {"name": item.alternative})
    return names(service)


def test_the_alternative_name_is_valid_for_the_catalog_it_is_merged_into() -> None:
    """The name is final once merged; it must satisfy the catalog's own identifier rule."""

    for length in (1, 30, 55, 56, 57, 58, 62, 63):
        for grantor_name in ("alice", "bo", "中文", "", "---"):
            alternative = alternative_name("a" * length, grantor_name)
            assert alternative != "a" * length
            CatalogEntry.from_json(
                {
                    "name": alternative,
                    "deviceId": "device-a",
                    "remotePort": PORT,
                    "protocol": "tcp",
                    "localPort": None,
                    "usage": None,
                    "env": {},
                    "auth": "none",
                    "guide": "none",
                }
            )
