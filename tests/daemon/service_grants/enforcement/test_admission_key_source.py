"""Where the admission key of a granted device comes from (docs/design/service-grant-2026-11-27.md, Exposing side).

Decision (service grant exposing side, 2026-11-26): an early design assumed
that the peer public key the sidecar sees equals the recipient's directory row
``clientPublicByServer[local serverPublic]`` and named that row as the key
source. These tests were made the proof, and the run settled it: the directory
row carries no such key, the sidecar's roster does. These cases keep that
settled, with product code on both sides. The recipient's row is written by the
real publish path over a real OrgFS runtime, the grant by the real grant
service and read back through the real reader, and the sidecar admits the way
the Go engine does.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.service_grants import load_org_service_grants
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    MEMBERS,
    ORG,
    PORT,
    DirectSidecar,
    Harness,
    peer_key,
)


def _publish_everyone(harness: Harness) -> None:
    for member in MEMBERS:
        harness.publish(member)


def _directory_admission_key(harness: Harness, recipient, server_public: str) -> str | None:
    """The withdrawn assumption: the key read from the recipient's directory device row.

    The row's ``clientPublicByServer`` entry under this device's ``serverPublic``.
    """

    return harness.device_row(recipient).get("clientPublicByServer", {}).get(server_public)


def test_the_directory_row_of_a_published_device_carries_no_per_server_key(tmp_path: Path) -> None:
    """What the real publish path writes: an empty map, by decision."""

    harness = Harness(tmp_path)
    try:
        rows = {member.device_id: harness.publish(member) for member in MEMBERS}
    finally:
        harness.close()

    assert {device_id: row["clientPublicByServer"] for device_id, row in rows.items()} == {
        ALICE_NODE.device_id: {},
        BOB_NODE.device_id: {},
        CAROL_NODE.device_id: {},
    }
    assert all(row["serverPublic"] and row["clientPublic"] for row in rows.values())


def test_the_directory_row_key_never_admits_a_granted_device(tmp_path: Path) -> None:
    """[A] refuted: the withdrawn source yields no key, so the grant admits nobody."""

    harness = Harness(tmp_path)
    sidecar = DirectSidecar(tmp_path)
    try:
        _publish_everyone(harness)
        harness.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug)
        assert load_org_service_grants(harness.store, ORG, owner=ALICE_NODE.owner)

        key = _directory_admission_key(harness, BOB_NODE, ALICE_NODE.server_public)
        sidecar.expose("keys", [key] if key else [])

        answer = sidecar.connect(BOB_NODE)
    finally:
        sidecar.close()
        harness.close()

    assert key is None
    assert answer["admitted"] is False


def test_the_roster_key_of_the_granted_device_admits_it_and_only_it(tmp_path: Path) -> None:
    """The control: the same flow with the roster key passes, and others stay out."""

    harness = Harness(tmp_path)
    sidecar = DirectSidecar(tmp_path)
    try:
        _publish_everyone(harness)
        harness.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug)

        sidecar.expose("keys", [peer_key(BOB_NODE, ALICE_NODE)])
        granted = sidecar.connect(BOB_NODE)
        member = sidecar.connect(CAROL_NODE)
        local = sidecar.connect(ALICE_NODE)
    finally:
        sidecar.close()
        harness.close()

    assert granted["admitted"] is True
    assert (member["admitted"], member["reason"]) == (False, "denied-by-gate")
    assert (local["admitted"], local["reason"]) == (False, "denied-by-gate")
