"""Revocation that does not depend on parsing, port-wide revocation, and supersession.

Decision (service grant S3 fix, 2026-11-26, critic S1, S2, S3 and S7 items 6,
7 and 8): a document under the caller's own path is revoked even when it no
longer parses, by writing a minimal revoked document that the reader lists as
revoked. ``revoke_all_for_port`` revokes this device's effective grants for one
port in every org. A grant for a ``(device, port, recipient)`` that already
has an effective grant rewrites that grant in place (integration fix,
2026-11-27): the same grantId, ``updated: true``, and still one effective
grant per target.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.daemon.service_grants.fixes.test_reader_hardening import _bob_writes, _record
from tests.daemon.service_grants.test_service_grants_core import (
    ORG,
    SYNTHETIC_SECRET,
    _device,
    _write_user_provider,
    harness,
)

_FIXTURES = (harness,)
BETA = "group-beta"


def _grant_with_credential(harness: dict[str, Any]) -> str:
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})
    service: ServiceGrantService = harness["service"]
    return service.grant(
        org=ORG,
        port=8447,
        device="bob/device-b",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )["grant"]["grantId"]


def _overwrite_own(harness: dict[str, Any], grant_id: str, text: str) -> None:
    store: OrgFsDirectoryStore = harness["store"]
    space_id = store._space_id(ORG)
    harness["runtime"].facade.write_text(
        space_id, f"directory/service-grants/alice/{grant_id}.json", text
    )


def _granted_view(service: ServiceGrantService, grant_id: str) -> dict[str, Any]:
    view = service.grants(org=ORG, include_revoked=True)
    [item] = [item for item in view["granted"] if item["grantId"] == grant_id]
    return item


def test_revoke_own_document_with_broken_json_writes_a_listed_tombstone(
    harness: dict[str, Any],
) -> None:
    """S1 case 1: an unparseable own document is revoked, not reported as absent."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    now = harness["clock"].value
    grant_id = _grant_with_credential(harness)
    _overwrite_own(harness, grant_id, "{broken json")

    assert service.revoke(org=ORG, grant_id=grant_id) == {"grantId": grant_id, "revoked": True}

    stored = store.read_service_grant(ORG, "alice", grant_id)
    assert stored is not None
    assert stored["credential"] is None
    assert stored["revokedAtMs"] == now
    view = _granted_view(service, grant_id)
    assert view["revokedAtMs"] == now
    assert view["effective"] is False
    assert view["hasCredential"] is False
    assert service.revoke(org=ORG, grant_id=grant_id) == {"grantId": grant_id, "revoked": False}


def test_revoke_own_document_with_extra_key_clears_credential_and_keeps_known_fields(
    harness: dict[str, Any],
) -> None:
    """S1 case 2: a schema-invalid own document loses its ciphertext and keeps what still validates."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    grant_id = _grant_with_credential(harness)
    raw = store.read_service_grant(ORG, "alice", grant_id)
    assert raw is not None and raw["credential"] is not None
    _overwrite_own(harness, grant_id, json.dumps({**raw, "extra": 1}))

    assert service.revoke(org=ORG, grant_id=grant_id) == {"grantId": grant_id, "revoked": True}

    stored = store.read_service_grant(ORG, "alice", grant_id)
    assert stored is not None
    assert "extra" not in stored
    assert stored["credential"] is None
    view = _granted_view(service, grant_id)
    assert view["recipient"] == {"owner": "bob", "deviceId": "device-b"}
    assert view["service"]["port"] == 8447
    assert view["effective"] is False


def test_revoke_absent_document_is_not_revoked(harness: dict[str, Any]) -> None:
    service: ServiceGrantService = harness["service"]
    gid = "sg.zzzzzzzzzzzzzzzzzzzzzzzzzz"
    assert service.revoke(org=ORG, grant_id=gid) == {"grantId": gid, "revoked": False}


def test_revoke_unreadable_and_unwritable_document_is_an_explicit_error(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: when the tombstone cannot be written the caller gets an error, not ``revoked: false``."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    grant_id = _grant_with_credential(harness)
    _overwrite_own(harness, grant_id, "{broken json")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OrgFsError("io", {"reason": "synthetic"})

    monkeypatch.setattr(store, "put_service_grant", refuse)
    with pytest.raises(DaemonRequestError) as caught:
        service.revoke(org=ORG, grant_id=grant_id)
    assert caught.value.code == ipc_errors.SERVICE_UNAVAILABLE


def test_revoke_all_for_port_revokes_this_devices_effective_grants_in_every_org(
    harness: dict[str, Any],
) -> None:
    """S2: only this device's grants for the port, across orgs, are revoked."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    runtime = harness["runtime"]
    harness["exposures"].set({"port": 9000, "target": "tcp:127.0.0.1:9000", "proxyProtocol": "none"})
    to_bob = service.grant(org=ORG, port=8447, device="bob/device-b")["grant"]["grantId"]
    to_all = service.grant(org=ORG, port=8447, device="@members")["grant"]["grantId"]
    other_port = service.grant(org=ORG, port=9000, device="@members")["grant"]["grantId"]
    space = runtime.facade.create_space(BETA)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    store.ensure_org_directory_roots(BETA)
    store.put_device(BETA, _device("device-a", "alice"))
    in_beta = service.grant(org=BETA, port=8447, device="@members")["grant"]["grantId"]
    other_device = "sg.oooooooooooooooooooooooooo"
    store.put_service_grant(
        ORG,
        "alice",
        other_device,
        _record(
            other_device,
            service={"deviceId": "device-a2", "port": 8447, "name": None, "protocol": "tcp", "note": None},
            recipient="@members",
        ),
    )
    _bob_writes(harness, "sg.bbbbbbbbbbbbbbbbbbbbbbbbbb", _record("sg.bbbbbbbbbbbbbbbbbbbbbbbbbb"))

    result = service.revoke_all_for_port(8447)

    assert sorted((item["org"], item["grantId"]) for item in result["revoked"]) == sorted(
        [(ORG, to_bob), (ORG, to_all), (BETA, in_beta)]
    )
    assert result["failed"] == []
    effective = {item["grantId"] for item in service.grants()["granted"]}
    assert effective == {other_port, other_device}
    assert [item["grantId"] for item in service.grants(org=ORG)["received"]] == [
        "sg.bbbbbbbbbbbbbbbbbbbbbbbbbb"
    ]
    assert service.revoke_all_for_port(8447)["revoked"] == []


def test_new_grant_supersedes_effective_grants_for_the_same_recipient(
    harness: dict[str, Any],
) -> None:
    """S3 and S7 item 7: at most one effective grant per (device, port, recipient)."""

    service: ServiceGrantService = harness["service"]
    first = service.grant(org=ORG, port=8447, device="bob/device-b")
    assert first["updated"] is False
    members = service.grant(org=ORG, port=8447, device="@members")
    assert members["updated"] is False

    second = service.grant(org=ORG, port=8447, device="user:bob/device-b")

    assert second["updated"] is True
    assert second["grant"]["grantId"] == first["grant"]["grantId"]
    effective = sorted(item["grantId"] for item in service.grants(org=ORG)["granted"])
    assert effective == sorted([second["grant"]["grantId"], members["grant"]["grantId"]])
    assert service.revoke(org=ORG, grant_id=second["grant"]["grantId"])["revoked"] is True
    assert [item["grantId"] for item in service.grants(org=ORG)["granted"]] == [
        members["grant"]["grantId"]
    ]


def test_expiry_boundary_and_expired_grants_are_not_superseded(harness: dict[str, Any]) -> None:
    """S7 item 6: ``expiresAtMs == now`` is refused; a grant expires once the clock reaches it."""

    service: ServiceGrantService = harness["service"]
    clock = harness["clock"]
    with pytest.raises(DaemonRequestError) as caught:
        service.grant(org=ORG, port=8447, device="bob/device-b", expires_at_ms=clock.value)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT

    short = service.grant(org=ORG, port=8447, device="bob/device-b", expires_at_ms=clock.value + 1)
    assert short["grant"]["effective"] is True
    clock.value += 1
    assert service.grants(org=ORG)["granted"] == []
    assert _granted_view(service, short["grant"]["grantId"])["effective"] is False

    renewed = service.grant(org=ORG, port=8447, device="bob/device-b")
    assert renewed["updated"] is False
    assert renewed["grant"]["grantId"] != short["grant"]["grantId"]
