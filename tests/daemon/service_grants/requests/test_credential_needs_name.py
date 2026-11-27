"""A credential travels only with a service name the recipient can use.

Decision (service grant integration fix, 2026-11-27): the recipient derives a
service entry only from a grant that names the service, so a Key sent
without a name would arrive with nothing to use it from. ``service.grant``
refuses a credential without ``name`` with INVALID_ARGUMENT and says why. A
grant with neither credential nor name still admits the device. The CLI's
human view says when the recipient will get no service entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.cli.commands.service import commands
from hyprial.shell.impl.cli.commands.service import grants as cli_grants
from tests.daemon.service_grants.renewal._world import BOB_NODE, MEMBERS, ORG, PORT, World
from tests.daemon.service_grants.test_ipc_and_cli import _FakeCliServices

NO_ENTRY = "will not get a service entry"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    made = World(tmp_path)
    for member in MEMBERS:
        made.publish(member)
    try:
        yield made
    finally:
        made.close()


def test_a_credential_without_a_name_is_refused_with_the_reason(world: World) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        world.grants().grant(
            org=ORG, port=PORT, device=BOB_NODE.slug,
            credential={"entryId": "gateway", "fieldName": "API_KEY"},
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "name" in str(refused.value) and "recipient" in str(refused.value)
    assert world.grant_docs() == []


def test_a_grant_with_neither_credential_nor_name_still_admits(world: World) -> None:
    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug)

    assert made["grant"]["service"]["name"] is None
    assert made["grant"]["hasCredential"] is False


def _grant_view(name: str | None) -> dict:
    return {
        "org": ORG, "grantor": "alice", "schemaVersion": 1, "grantId": "sg." + "a" * 26,
        "service": {"deviceId": "device-a", "port": PORT, "name": name, "protocol": "tcp", "note": None},
        "recipient": {"owner": "bob", "deviceId": "device-b"},
        "grantedAtMs": 1, "expiresAtMs": None, "revokedAtMs": None, "updatedAtMs": None,
        "hasCredential": False, "effective": True,
    }


def _human(monkeypatch: pytest.MonkeyPatch, name: str | None) -> str:
    services = _FakeCliServices({"grant": _grant_view(name), "updated": False})
    monkeypatch.setattr(commands, "get_services", lambda: services)
    monkeypatch.setattr(cli_grants, "get_services", lambda: services)
    args = ["grant", str(PORT), "--org", ORG, "--device", "bob/device-b"]
    if name is not None:
        args += ["--name", name]
    result = CliRunner().invoke(commands.service_app, args)
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_the_cli_says_when_the_recipient_gets_no_service_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    assert NO_ENTRY in _human(monkeypatch, None)
    assert NO_ENTRY not in _human(monkeypatch, "magpie")
