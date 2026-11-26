"""Service grant S1: ``org.network`` accepts a directory row carrying the key pair.

The result of ``org.network`` is validated against a closed schema at request
time.  A directory row that publishes ``encryptionKeyId`` /
``encryptionPublicKey`` must stay a valid row, or the first upgraded device in
an org would turn every ``org network`` answer into ``DAEMON_ERROR``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org.network_specs import _NETWORK_RESULT

KEY_ID = "x25519:" + "a" * 32
PUBLIC_KEY = base64.b64encode(bytes(range(1, 33))).decode("ascii")

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
        "directoryPending": {
            "group-acme": {"contentPending": [], "parkedCommits": None}
        },
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
        pytest.param({"encryptionKeyId": KEY_ID}, id="key-id-only"),
        pytest.param({"encryptionPublicKey": PUBLIC_KEY}, id="public-key-only"),
    ],
)
def test_half_of_the_pair_is_still_not_a_valid_result(extra: dict[str, object]) -> None:
    """The pair rule is one rule: the request schema must not accept half a row."""

    assert _first_error(_result({**_ROW, **extra})) is not None


def test_the_published_example_stays_valid() -> None:
    """The contract example must not drift from what the daemon really answers."""

    from hyprial.daemon.impl.request_catalog.org.management_examples import (
        ORG_MANAGEMENT_EXAMPLES,
    )

    [network] = [item for item in ORG_MANAGEMENT_EXAMPLES if item.method == "org.network"]
    for case in network.cases:
        if case.kind == "success":
            assert _first_error(case.result) is None
