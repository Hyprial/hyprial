"""A crowd of granted devices, for the sidecar's key-set limit.

Decision (service grant S4-py, 2026-11-26): the limit is a property of the
derived set's size, not of how grants or roster rows are stored, so these are
in-memory inputs handed to the enforcer as its readers. Keys are canonical
nodekeys so the wire validation the controller performs is the real one.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.service_grants import (
    GrantRecipient,
    ServiceEndpoint,
    ServiceGrant,
    StoredServiceGrant,
)
from tests.daemon.service_grants.enforcement._support import ALICE_NODE, ORG
from tests.support.tailnet_membership import key

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def _body(index: int) -> str:
    """A 26 character base32 lower body, unique for every index below 32**26."""

    digits = []
    for _ in range(26):
        index, digit = divmod(index, 32)
        digits.append(ALPHABET[digit])
    return "".join(digits)


def crowd_grants(port: int, count: int) -> dict[str, tuple[StoredServiceGrant, ...]]:
    return {
        ORG: tuple(
            StoredServiceGrant(
                org=ORG,
                grantor=ALICE_NODE.owner,
                grant=ServiceGrant(
                    schema_version=1,
                    grant_id=f"sg.{_body(index)}",
                    service=ServiceEndpoint(
                        device_id=ALICE_NODE.device_id,
                        port=port,
                        name=None,
                        protocol="tcp",
                        note=None,
                    ),
                    recipient=GrantRecipient(owner="crowd", device_id=f"crowd-{index}"),
                    granted_at_ms=0,
                ),
            )
            for index in range(count)
        )
    }


def crowd_roster(count: int, tailnet_id: str = "") -> dict[str, Any]:
    """A membership observation whose rows each carry a key for this server."""

    members = [
        {
            "owner": "crowd",
            "deviceId": f"crowd-{index}",
            "masterPublic": key(f"master-crowd-{index}"),
            "serverPublic": key(f"server-crowd-{index}"),
            "clientPublicByServer": {ALICE_NODE.server_public: key(f"peer-crowd-{index}")},
            "address": "",
            "state": "active",
            "revision": 1,
        }
        for index in range(count)
    ]
    return {
        "readiness": {"sidecar": True, "control": True, "member": True},
        "tailnets": [
            {
                "tailnetId": tailnet_id,
                "role": "authority",
                "revision": 1,
                "authorityDeviceId": ALICE_NODE.device_id,
                "memberReady": True,
                "leaseRemainingSeconds": None,
                "activeCount": count,
                "tombstoneCount": 0,
                "code": None,
                "members": members,
            }
        ],
    }
