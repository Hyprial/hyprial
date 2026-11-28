"""One member's malformed grant document never hides anyone else's grants.

Decision (service grant documents fix, 2026-11-26): every parse or validation
failure of a single document, including ``RecursionError``, skips only that
document. Bodies over 64 KiB are refused by byte count before parsing, and
``credential`` must have the fixed key set and scalar types of the sealed
envelope. Bob writes in his own path, so the OrgFS author rule admits every one
of these documents.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.service_grants import ServiceGrantService, load_org_service_grants

from tests.daemon.service_grants.test_service_grants_core import (
    ORG,
    _as,
    _device,
    harness,
)

_FIXTURES = (harness,)
BETA = "group-beta"
BOB_GOOD = "sg.bbbbbbbbbbbbbbbbbbbbbbbbbb"
BOB_BAD = "sg.eeeeeeeeeeeeeeeeeeeeeeeeee"


def _record(grant_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "grantId": grant_id,
        "service": {"deviceId": "device-b", "port": 8447, "name": "svc", "protocol": "tcp", "note": None},
        "recipient": {"owner": "alice", "deviceId": "device-a"},
        "grantedAtMs": 1,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }
    base.update(over)
    return base


def envelope_shape(grant_id: str = BOB_GOOD) -> dict[str, Any]:
    """A credential with the sealed envelope's exact key set and scalar types."""

    return {
        "header": {
            "v": 1,
            "alg": "A256GCM",
            "wrap": "X25519-HKDF-SHA256-A256GCM",
            "org": ORG,
            "grantId": grant_id,
            "service": {"owner": "bob", "deviceId": "device-b", "port": 8447},
            "recipient": {"owner": "alice", "deviceId": "device-a"},
            "encryptionKeyId": "x25519:" + "0" * 32,
        },
        "recipients": [
            {"recipientKeyId": "x25519:" + "0" * 32, "epk": "AA==", "nonce": "AA==", "wrappedDek": "AA=="}
        ],
        "payload": {"nonce": "AA==", "ciphertext": "AA=="},
    }


def _bob_writes_text(harness: dict[str, Any], grant_id: str, text: str) -> None:
    store: OrgFsDirectoryStore = harness["store"]
    runtime = harness["runtime"]
    space_id = store._space_id(ORG)
    with _as(runtime, "bob"):
        store._mkdir_if_absent(space_id, "directory/service-grants/bob")
        runtime.facade.write_text(space_id, f"directory/service-grants/bob/{grant_id}.json", text)


def _bob_writes(harness: dict[str, Any], grant_id: str, record: dict[str, Any]) -> None:
    _bob_writes_text(harness, grant_id, json.dumps(record))


def _seed_good_grants(harness: dict[str, Any]) -> str:
    service: ServiceGrantService = harness["service"]
    own = service.grant(org=ORG, port=8447, device="@members")["grant"]["grantId"]
    _bob_writes(harness, BOB_GOOD, _record(BOB_GOOD))
    return own


def _listed(service: ServiceGrantService, **kwargs: Any) -> tuple[list[str], list[str]]:
    view = service.grants(**kwargs)
    return (
        [item["grantId"] for item in view["granted"]],
        [item["grantId"] for item in view["received"]],
    )


def _deep_dict(depth: int) -> object:
    nested: object = {"x": 1}
    for _ in range(depth):
        nested = {"n": nested}
    return nested


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("[" * 100000 + "]" * 100000, id="deep-array-200k"),
        pytest.param("[" * 30000 + "]" * 30000, id="deep-array-under-64k"),
        pytest.param(json.dumps(_record(BOB_BAD, credential=_deep_dict(700))), id="nested-credential-700"),
    ],
)
def test_malformed_member_document_does_not_hide_valid_grants(
    harness: dict[str, Any], body: str
) -> None:
    """Each malformed body lists the other valid grants instead of raising."""

    service: ServiceGrantService = harness["service"]
    own = _seed_good_grants(harness)
    _bob_writes_text(harness, BOB_BAD, body)

    assert _listed(service, org=ORG) == ([own], [BOB_GOOD])
    assert _listed(service) == ([own], [BOB_GOOD])
    loaded = load_org_service_grants(harness["store"], ORG)
    assert sorted(item.grant.grant_id for item in loaded) == sorted([own, BOB_GOOD])
    assert service.revoke(org=ORG, grant_id="sg.zzzzzzzzzzzzzzzzzzzzzzzzzz") == {
        "grantId": "sg.zzzzzzzzzzzzzzzzzzzzzzzzzz",
        "revoked": False,
    }


def test_malformed_document_in_one_org_leaves_other_orgs_listed(harness: dict[str, Any]) -> None:
    """Listing every org keeps an unaffected org's grants when another org holds the bad document."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    runtime = harness["runtime"]
    space = runtime.facade.create_space(BETA)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    store.ensure_org_directory_roots(BETA)
    store.put_device(BETA, _device("device-a", "alice"))
    beta = service.grant(org=BETA, port=8447, device="@members")["grant"]["grantId"]
    _bob_writes(harness, BOB_BAD, _record(BOB_BAD, credential=_deep_dict(700)))

    assert _listed(service) == ([beta], [])


def test_body_over_64_kib_is_skipped_by_byte_count(harness: dict[str, Any]) -> None:
    """A schema-valid document whose UTF-8 body exceeds 64 KiB is not read."""

    service: ServiceGrantService = harness["service"]
    text = json.dumps(_record(BOB_BAD))
    padded = text[:-1] + " " * (64 * 1024 - len(text) + 1) + "}"
    assert len(padded.encode("utf-8")) > 64 * 1024
    _bob_writes_text(harness, BOB_BAD, padded)
    _bob_writes(harness, BOB_GOOD, _record(BOB_GOOD))

    assert _listed(service, org=ORG) == ([], [BOB_GOOD])


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param({"anything": True}, id="arbitrary-object"),
        pytest.param({**envelope_shape(), "sealedBy": "x"}, id="extra-top-key"),
        pytest.param({**envelope_shape(), "recipients": []}, id="no-recipient-row"),
        pytest.param({**envelope_shape(), "payload": {"nonce": 1, "ciphertext": "AA=="}}, id="non-string-nonce"),
        pytest.param(
            {**envelope_shape(), "header": {**envelope_shape()["header"], "v": True}},
            id="boolean-version",
        ),
        pytest.param(
            {**envelope_shape(), "header": {**envelope_shape()["header"], "service": {"owner": "bob"}}},
            id="short-header-service",
        ),
    ],
)
def test_credential_must_match_the_sealed_envelope_shape(
    harness: dict[str, Any], credential: dict[str, Any]
) -> None:
    """A credential that is not a sealed envelope invalidates its document."""

    service: ServiceGrantService = harness["service"]
    _bob_writes(harness, BOB_BAD, _record(BOB_BAD, credential=credential))
    _bob_writes(harness, BOB_GOOD, _record(BOB_GOOD, credential=envelope_shape()))

    view = service.grants(org=ORG)
    assert [item["grantId"] for item in view["received"]] == [BOB_GOOD]
    assert view["received"][0]["hasCredential"] is True


def test_reader_skips_mismatched_id_extra_key_and_members_with_credential(
    harness: dict[str, Any],
) -> None:
    """Each invalid document is skipped and the valid one stays."""

    service: ServiceGrantService = harness["service"]
    _bob_writes(harness, "sg.cccccccccccccccccccccccccc", _record("sg.dddddddddddddddddddddddddd"))
    _bob_writes(harness, "sg.ffffffffffffffffffffffffff", {**_record("sg.ffffffffffffffffffffffffff"), "owner": "alice"})
    _bob_writes(
        harness,
        "sg.gggggggggggggggggggggggggg",
        _record("sg.gggggggggggggggggggggggggg", recipient="@members", credential=envelope_shape()),
    )
    _bob_writes(harness, BOB_GOOD, _record(BOB_GOOD))

    assert _listed(service, org=ORG, include_revoked=True) == ([], [BOB_GOOD])


@pytest.mark.parametrize(
    "service_record",
    [
        pytest.param({"deviceId": "device-b", "port": 8447, "name": "../x", "protocol": "tcp", "note": None}, id="name-path"),
        pytest.param({"deviceId": "device-b", "port": 8447, "name": "svc", "protocol": "tcp", "note": "n" * 513}, id="note-513"),
        pytest.param({"deviceId": "device-b", "port": 8447, "name": "svc", "protocol": "tcp", "note": "a\nb"}, id="note-control"),
        pytest.param({"deviceId": "@device-b", "port": 8447, "name": "svc", "protocol": "tcp", "note": None}, id="device-selector"),
        pytest.param({"deviceId": "d" * 257, "port": 8447, "name": "svc", "protocol": "tcp", "note": None}, id="device-257"),
    ],
)
def test_reader_applies_the_write_rules_to_names_notes_and_identifiers(
    harness: dict[str, Any], service_record: dict[str, Any]
) -> None:
    """On the read side: a document the writer would refuse is skipped."""

    service: ServiceGrantService = harness["service"]
    _bob_writes(harness, BOB_BAD, _record(BOB_BAD, service=service_record))
    _bob_writes(harness, BOB_GOOD, _record(BOB_GOOD))

    assert _listed(service, org=ORG) == ([], [BOB_GOOD])
