"""Read-only recipient status on ``service.grants`` and its human listing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from hyprial.daemon.impl.service_grants.consumer.keys import entry_path
from hyprial.daemon.impl.service_grants.ipc import build_service
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.shell.impl.cli.commands.service import commands, grants as cli_grants
from tests.daemon.service_connect.support import FakeController, FakeStatePort
from tests.daemon.service_grants.consumer.support import (
    ORG,
    PORT,
    Clock,
    build_directory,
    grantor,
    hand_placed,
    service_manager,
)
from tests.daemon.service_grants.test_ipc_and_cli import _FakeCliServices


@pytest.fixture
def world(tmp_path: Path, actor_host) -> Iterator[dict[str, Any]]:
    directory = build_directory(tmp_path)
    clock = Clock()
    made = grantor(directory, clock).grant(
        org=ORG,
        port=PORT,
        device="bob/device-b",
        name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    recipient = ServiceGrantService(
        store=directory["store"],
        home=directory["bob_home"],
        owner="bob",
        local_device_id="device-b",
        exposed_ports=lambda: frozenset(),
        now_ms=clock,
    )
    managers = []
    try:
        yield {
            **directory,
            "clock": clock,
            "grant": made["grant"],
            "recipient": recipient,
            "actor_host": actor_host,
            "access": tmp_path / "bob-home" / "state" / "tailcat" / "service-access.json",
            "managers": managers,
        }
    finally:
        for manager in managers:
            manager.close(2.0)
        directory["runtime"].close()


def _consumer(world: dict[str, Any]) -> GrantConsumer:
    return GrantConsumer(
        orgfs=world["runtime"].facade,
        home=world["bob_home"],
        state_dir=world["bob_state"],
        owner="bob",
        device_id="device-b",
        now_ms=world["clock"],
    )


def _manager(world: dict[str, Any], source: GrantConsumer) -> Any:
    return service_manager(
        {**world, "state": FakeStatePort(), "controller": FakeController()}, source
    )


def _received(world: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return world["recipient"].grants(**kwargs)["received"][0]


def test_received_status_reads_stored_key_without_starting_a_manager(world: dict[str, Any]) -> None:
    row = _received(world)

    assert row["keyStored"] is False
    assert row["entryId"] is None
    assert row["connected"] is False
    assert row["serviceName"] is None
    assert not world["access"].exists()

    source = _consumer(world)
    source.refresh()

    stored = _received(world)
    assert stored["keyStored"] is True
    assert stored["entryId"] == "grant." + world["grant"]["grantId"].removeprefix("sg.")
    assert stored["connected"] is False
    assert stored["serviceName"] is None


def test_received_status_stops_claiming_a_missing_key_file(world: dict[str, Any]) -> None:
    source = _consumer(world)
    source.refresh()
    entry_path(world["bob_home"], "grant." + world["grant"]["grantId"].removeprefix("sg.")).unlink()

    row = _received(world)

    assert row["keyStored"] is False
    assert row["entryId"] is None


def test_received_status_uses_the_running_managers_derived_snapshot(world: dict[str, Any]) -> None:
    source = _consumer(world)
    service = _manager(world, source)
    world["managers"].append(service)
    world["recipient"]._status = service.grant_status
    service.handle("service.guide", {"name": "magpie"})
    service.handle("service.connect", {"name": "magpie"})

    row = _received(world)

    assert row["keyStored"] is True
    assert row["connected"] is True
    assert row["serviceName"] == "magpie"


def test_received_status_reports_the_renamed_service(world: dict[str, Any]) -> None:
    hand_placed(world, name="magpie")
    source = _consumer(world)
    service = _manager(world, source)
    world["managers"].append(service)
    world["recipient"]._status = service.grant_status
    service.handle("service.guide", {"name": "magpie-alice"})

    row = _received(world)

    assert row["serviceName"] == "magpie-alice"
    assert row["connected"] is False


def test_received_status_does_not_derive_for_an_inactive_grant(world: dict[str, Any]) -> None:
    grantor(world, world["clock"]).revoke(org=ORG, grant_id=world["grant"]["grantId"])

    row = _received(world, include_revoked=True)

    assert row["keyStored"] is False
    assert row["entryId"] is None
    assert row["connected"] is False
    assert row["serviceName"] is None


def test_ipc_status_reads_only_an_existing_manager(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    write_device_record(
        home,
        DeviceRecord(
            device_id="device-b",
            owner="bob",
            server_public="nodekey:server-device-b",
            client_public="nodekey:client-device-b",
            key_generation=1,
        ),
    )
    app = SimpleNamespace(
        hyprial_home=home,
        owner="bob",
        _service_manager=None,
        _orgfs_runtime=SimpleNamespace(facade=object()),
    )
    app._require_orgfs_runtime = lambda: app._orgfs_runtime

    service = build_service(app)

    assert service._status is None
    assert app._service_manager is None
    app._service_manager = SimpleNamespace(
        grant_status=lambda: {("group-acme", "sg." + "a" * 26): {"serviceName": "magpie", "connected": True}}
    )
    with_manager = build_service(app)
    assert with_manager._status is app._service_manager.grant_status


def test_cli_received_row_shows_the_read_only_status(monkeypatch: pytest.MonkeyPatch) -> None:
    services = _FakeCliServices(
        {
            "granted": [],
            "received": [
                {
                    "grantId": "sg.abcdefghijklmnopqrstuvwxyz",
                    "org": ORG,
                    "grantor": "alice",
                    "service": {"deviceId": "device-a", "port": PORT, "name": "magpie"},
                    "recipient": {"owner": "bob", "deviceId": "device-b"},
                    "hasCredential": True,
                    "effective": True,
                    "keyStored": True,
                    "entryId": "grant.abcdefghijklmnopqrstuvwxyz",
                    "connected": True,
                    "serviceName": "magpie-alice",
                }
            ],
        }
    )
    monkeypatch.setattr(commands, "get_services", lambda: services)
    monkeypatch.setattr(cli_grants, "get_services", lambda: services)

    result = CliRunner().invoke(commands.service_app, ["grants"])

    assert result.exit_code == 0, result.stderr
    assert "keyStored=yes" in result.stdout
    assert "entry=grant.abcdefghijklmnopqrstuvwxyz" in result.stdout
    assert "connected=yes" in result.stdout
    assert "service=magpie-alice" in result.stdout
