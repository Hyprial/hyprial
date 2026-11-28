"""Service grant device encryption key: ``org.network`` accepts a directory row carrying the key pair.

The result of ``org.network`` is validated against a closed schema at request
time.  A directory row that publishes ``encryptionKeyId`` /
``encryptionPublicKey`` must stay a valid row, or the first upgraded device in
an org would turn every ``org network`` answer into ``DAEMON_ERROR``.

The schema takes each key on its own.  That they come as a pair is the
directory model's rule and has one enforcement point there; what the schema
adds is that a key is never ``null`` or empty, and that the key set of a row
stays closed.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.org.network.status.view import device_view
from hyprial.daemon.impl.request_catalog.org.network_specs import _NETWORK_RESULT
from hyprial.identity import DirectoryDevice

PUBLIC_KEY = base64.b64encode(bytes(range(1, 33))).decode("ascii")
KEY_ID = "x25519:" + hashlib.sha256(bytes(range(1, 33))).hexdigest()[:32]

_ROW = {
    "deviceId": "device-a",
    "owner": "alice",
    "serverPublic": "nodekey:" + "1" * 64,
    "clientPublic": "nodekey:" + "2" * 64,
    "clientPublicByServer": {},
    "addressPresent": True,
    "keyGeneration": 1,
    "updatedAt": "2026-11-25T00:00:00+00:00",
    "presence": "unknown",
}
_AVAILABLE = {"available": True}


def _result(row: dict[str, object]) -> dict[str, object]:
    return {
        "orgs": ["group-acme"],
        "unavailableOrgs": [],
        "dataProblems": [],
        "directory": {"group-acme": [row]},
        "directoryPending": {"group-acme": {"contentPending": []}},
        "self": {
            "deviceId": "device-a",
            "owner": "alice",
            "deviceUid": None,
            "serverPublic": "nodekey:" + "1" * 64,
            "addressPresent": True,
        },
        "mappedPeers": [],
        "sidecar": None,
        "paneStatus": {
            "directory": _AVAILABLE,
            "mappedPeers": _AVAILABLE,
            "sidecar": _AVAILABLE,
        },
        "sources": {
            "directory": "orgfs",
            "mappedPeers": "forwarding-sidecar",
            "sidecar": "forwarding-sidecar",
        },
    }


def _plain(value: object) -> object:
    """Thaw a frozen example into the plain JSON the schema validators expect."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _first_error(value: object) -> str | None:
    errors = sorted(
        Draft202012Validator(_NETWORK_RESULT).iter_errors(_plain(value)), key=str
    )
    return None if not errors else str(errors[0].message)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="row-without-the-pair"),
        pytest.param(
            {"encryptionKeyId": KEY_ID, "encryptionPublicKey": PUBLIC_KEY},
            id="row-with-the-pair",
        ),
    ],
)
def test_a_directory_row_with_or_without_the_pair_is_a_valid_network_result(
    extra: dict[str, object]
) -> None:
    assert _first_error(_result({**_ROW, **extra})) is None


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            {"encryptionKeyId": None, "encryptionPublicKey": PUBLIC_KEY}, id="id-null"
        ),
        pytest.param(
            {"encryptionKeyId": KEY_ID, "encryptionPublicKey": None}, id="public-key-null"
        ),
        pytest.param(
            {"encryptionKeyId": "", "encryptionPublicKey": PUBLIC_KEY}, id="id-empty"
        ),
        pytest.param(
            {"encryptionKeyId": KEY_ID, "encryptionPublicKey": ""}, id="public-key-empty"
        ),
    ],
)
def test_the_pair_is_never_null_or_empty(extra: dict[str, object]) -> None:
    """The daemon answers both keys or neither; a null or empty one is a broken row."""

    assert _first_error(_result({**_ROW, **extra})) is not None


def test_the_row_still_refuses_a_key_it_does_not_know() -> None:
    """The pair does not open the closed key set of a directory row."""

    extra = {"encryptionKeyId": KEY_ID, "encryptionPublicKey": PUBLIC_KEY}
    error = _first_error(_result({**_ROW, **extra, "encryptionKeyPrivate": "x"}))

    assert error is not None and "encryptionKeyPrivate" in error


def test_what_the_daemon_renders_from_a_real_row_is_a_valid_result() -> None:
    """The projection of a stored row, with and without the pair, passes the schema.

    This is the cross-layer guard: the directory model decides what a row can
    hold and the result schema decides what an answer may hold, and a row that
    is one but not the other would turn every ``org network`` answer of an org
    into ``DAEMON_ERROR``.
    """

    for fields in ({}, {"encryption_key_id": KEY_ID, "encryption_public_key": PUBLIC_KEY}):
        device = DirectoryDevice(
            device_id="device-a",
            owner="alice",
            server_public="nodekey:" + "1" * 64,
            client_public="nodekey:" + "2" * 64,
            address="tcp://10.0.0.1:7447#psk",
            key_generation=1,
            updated_at="2026-11-25T00:00:00+00:00",
            device_uid=None,
            **fields,
        )

        assert _first_error(_result(device_view(device))) is None


def test_the_published_example_stays_valid() -> None:
    """The contract example must not drift from what the daemon really answers."""

    from hyprial.daemon.impl.request_catalog.org.management_examples import (
        ORG_MANAGEMENT_EXAMPLES,
    )

    [network] = [item for item in ORG_MANAGEMENT_EXAMPLES if item.method == "org.network"]
    for case in network.cases:
        if case.kind == "success":
            assert _first_error(case.result) is None
