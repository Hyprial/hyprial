from __future__ import annotations

import json
from types import SimpleNamespace
import logging
import sqlite3

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.desired_state import DesiredStateStore, ServiceRegistry
from hyprial.daemon.impl.service_connect import ServiceConnectError, ServiceManager
from hyprial.daemon.impl.service_connect.models import RegistrySnapshot
from hyprial.daemon.impl.service_connect.models import CatalogEntry
from hyprial.kernel import RequestPortError


from hyprial.shell.impl.cli.commands.service import commands

from .support import (

    ADDRESS,
    SERVER_PUBLIC,
    FakeController,
    FakeStatePort,
    ManualScheduler,
    write_access,
)


CATALOG_ENTRY_PAYLOAD = CatalogEntry.from_json(
    {
        "name": "api",
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


DECLARED_EVENTS = {
    "service.connect.registry.updated",
    "service.connect.registry.fallback",
    "service.connect.access.invalid",
    "service.connect.mapping.failed",
    "service.connect.mapping.closed",
    "service.connect.reconcile.completed",
    "service.connect.reconcile.failed",
    "service.connect.restore.failed",
}


def _manager(tmp_path, *, controller=None, state=None) -> ServiceManager:
    access = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(access)
    return ServiceManager(
        orgfs=None,
        state=state or FakeStatePort(),
        persistence_late_result=None,
        controller_factory=lambda: controller or FakeController(),
        access_path=access,
        scheduler=ManualScheduler(),
        service_discovery=lambda: (CATALOG_ENTRY_PAYLOAD,),
    )


def test_every_declared_event_is_emitted_at_its_operational_site(
    tmp_path, caplog, monkeypatch
) -> None:
    caplog.set_level(
        logging.INFO, logger="hyprial.daemon.impl.service_connect.manager"
    )

    controller = FakeController()
    manager = _manager(tmp_path / "active", controller=controller)
    try:
        manager.reconcile()

        cache = ServiceRegistry.from_json(
            {
                "schemaVersion": 1,
                "spaceId": "space-1",
                "owner": "user:owner",
                "cachedAtMs": 1,
                "entries": [],
            }
        )
        snapshots = iter(
            (
                RegistrySnapshot(
                    (CATALOG_ENTRY_PAYLOAD,), "orgfs", "space-1", "user:owner", None, cache
                ),
                RegistrySnapshot(
                    (CATALOG_ENTRY_PAYLOAD,),
                    "cache",
                    "space-1",
                    "user:owner",
                    "service catalog is unavailable",
                    cache,
                ),
            )
        )
        original_read_catalog = manager._io.read_catalog  # noqa: SLF001
        monkeypatch.setattr(
            manager._io,  # noqa: SLF001 - event-site contract
            "read_catalog",
            lambda _registry, _timeout: next(snapshots),
        )
        manager.handle("service.guide", {"name": "api"})
        manager.handle("service.guide", {"name": "api"})
        monkeypatch.setattr(manager._io, "read_catalog", original_read_catalog)  # noqa: SLF001

        manager.handle("service.connect", {"name": "api"})
        manager.access_path.unlink()
        manager.reconcile()
    finally:
        manager.close(1.0)

    state = FakeStatePort()
    state.set_service_connection("api", 18080)
    controller = FakeController()
    controller.fail_map = RuntimeError("private controller diagnostic")
    restoring = _manager(tmp_path / "restore", controller=controller, state=state)
    try:
        restoring.reconcile()
    finally:
        restoring.close(1.0)

    events = {
        record.event
        for record in caplog.records
        if isinstance(getattr(record, "event", None), str)
        and record.event.startswith("service.connect.")
    }
    assert events == DECLARED_EVENTS
    serialized = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert ADDRESS not in serialized
    assert SERVER_PUBLIC not in serialized


class _CliServices:
    CliError = RequestPortError

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.runtime = SimpleNamespace(
            request=self._daemon_request,
            execute=self._execute,
        )

    def _daemon_request(self, method: str, _params: dict[str, object], **_kwargs):
        value = self.responses[method]
        if isinstance(value, BaseException):
            raise value
        return value

    def _execute(self, operation, *, json_output: bool, **_kwargs) -> None:
        from hyprial.shell.impl.cli.output import run_command

        run_command(operation, json_output=json_output, require_home=None)


@pytest.fixture
def leak_surfaces(tmp_path, caplog, monkeypatch) -> dict[str, str]:
    caplog.set_level(
        logging.INFO, logger="hyprial.daemon.impl.service_connect.manager"
    )
    controller = FakeController()
    controller.argv = ["hyprial-tailcat", "forward"]
    controller.environ = {
        "HYPRIAL_FORWARDING_COMMAND": '["hyprial-tailcat","forward"]',
        "HYPRIAL_FORWARDING_UP": json.dumps(
            {
                "v": 6,
                "op": "up",
                "keyFile": "/private/key.json",
                "addressFile": "/private/address",
            }
        ),
    }
    manager = _manager(tmp_path / "manager", controller=controller)
    ipc_values: list[object] = []
    try:
        connected = manager.handle("service.connect", {"name": "api"})
        ipc_values.extend(
            (
                connected,
                manager.handle("service.list", {}),
                manager.handle("service.guide", {"name": "api"}),
                manager.handle("service.disconnect", {"name": "api"}),
            )
        )
        manager.reconcile()
        for method, params in (
            ("service.connect", {"name": "missing"}),
            ("service.disconnect", {"name": "api", "address": ADDRESS}),
            ("service.guide", {"name": "missing"}),
            ("service.list", {"address": ADDRESS}),
        ):
            try:
                manager.handle(method, params)
            except ServiceConnectError as error:
                ipc_values.append({"method": method, "code": error.code, "error": str(error)})
    finally:
        manager.close(1.0)

    responses = {
        "service.connect": connected,
        "service.disconnect": {"name": "api", "removed": True},
        "service.guide": ipc_values[2],
        "service.list": ipc_values[1],
    }
    monkeypatch.setattr(commands, "get_services", lambda: _CliServices(responses))
    cli_output: list[str] = []
    for args in (
        ["connect", "api"],
        ["connect", "api", "--json"],
        ["disconnect", "api"],
        ["disconnect", "api", "--json"],
        ["guide", "api"],
        ["guide", "api", "--json"],
        ["list"],
        ["list", "--json"],
    ):
        result = CliRunner().invoke(commands.service_app, args)
        assert result.exit_code == 0, result.stderr
        cli_output.extend((result.stdout, result.stderr))
    monkeypatch.setattr(
        commands,
        "get_services",
        lambda: _CliServices(
            {
                key: RequestPortError("SERVICE_UNAVAILABLE", "service unavailable")
                for key in responses
            }
        ),
    )
    for args in (["connect", "api", "--json"], ["list"]):
        result = CliRunner().invoke(commands.service_app, args)
        assert result.exit_code == 1
        cli_output.extend((result.stdout, result.stderr))

    store_root = tmp_path / "store"
    store = DesiredStateStore(store_root / "desired.json")
    store.set_service_connection("api", 18080)
    store.set_service_registry(
        {
            "schemaVersion": 1,
            "spaceId": "space-1",
            "owner": "user:owner",
            "cachedAtMs": 1,
            "entries": [CATALOG_ENTRY_PAYLOAD.to_json()],
        }
    )
    with sqlite3.connect(store_root / "lifecycle-operations.sqlite3") as db:
        sqlite_dump = "\n".join(db.iterdump())

    return {
        "ipc-success-and-failure": json.dumps(ipc_values, sort_keys=True),
        "cli-human-json-stderr": "\n".join(cli_output),
        "caplog": caplog.text
        + "\n"
        + "\n".join(str(record.__dict__) for record in caplog.records),
        "sqlite-service-store": sqlite_dump,
        "sidecar-argv-env": json.dumps(
            {"argv": controller.argv, "env": controller.environ}, sort_keys=True
        ),
    }


@pytest.mark.parametrize(
    "surface",
    (
        "ipc-success-and-failure",
        "cli-human-json-stderr",
        "caplog",
        "sqlite-service-store",
        "sidecar-argv-env",
    ),
)
@pytest.mark.parametrize("canary", (ADDRESS, SERVER_PUBLIC))
def test_private_service_bytes_never_reach_public_surfaces(
    leak_surfaces: dict[str, str], surface: str, canary: str
) -> None:
    assert canary.encode() not in leak_surfaces[surface].encode()
