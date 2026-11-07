"""Roster ownership wins over colliding or spoofed directory device rows.

V6 membership keys are unique deviceIds, not owner/device directory tuples.
Keep review 937's anti-hijack intent: a directory row cannot replace either a
roster candidate or its owner, even when it claims the same deviceId.
"""
from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.forwarding.membership.projection import owner_for_key
from hyprial.identity.impl.org_directory import DIRECTORY_DIR
from tests.daemon.org.network import test_org_network_service as _svc
from tests.daemon.org.network.test_org_network_service import (
    INVITEE_OWNER, INVITER_ADDRESS, NODE_ID, OWN_ADDRESS, OWNER,
    FakeFs, Recorder, _create_org_spaces, _invite_link, _service,
)
from tests.support.tailnet_membership import key, member

home = _svc.home
_invite_clock = _svc._invite_clock
SPACE = "space-group-acme"
LOCAL_SERVER = key("server-node-a")


def _directory_row(fs, owner, device_id, address):
    fs.invite(SPACE, f"user:{owner}")
    fs.dirs.update({(SPACE, DIRECTORY_DIR), (SPACE, f"{DIRECTORY_DIR}/{owner}")})
    fs.docs[(SPACE, f"{DIRECTORY_DIR}/{owner}/{device_id}.json")] = json.dumps({
        "deviceId": device_id, "owner": owner,
        "serverPublic": key("directory-server-" + owner),
        "clientPublic": key("directory-client-" + owner),
        "clientPublicByServer": {LOCAL_SERVER: key("directory-derived-" + owner)},
        "address": address, "keyGeneration": 1,
        "updatedAt": "2026-10-03T11:00:00+00:00",
    })


def _peer(owner, device_id, order):
    row = member(owner, device_id)
    # Explicit canonical keys below the local key select the local dialer.
    row["masterPublic"] = "nodekey:" + f"{order:064x}"
    row["clientPublicByServer"] = {LOCAL_SERVER: key("derived-" + device_id)}
    return row


def _assert_owner(fake, row):
    status = fake.membership_request("membership-status", {})
    assert owner_for_key(status, row["clientPublicByServer"][LOCAL_SERVER], LOCAL_SERVER) == row["owner"]
    assert owner_for_key(status, key("directory-derived-" + row["owner"]), LOCAL_SERVER) is None


def test_roster_peers_keep_owner_identity_despite_colliding_directory_ids(home: Path) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    alice = _peer("alice@example", "node-alice", 1)
    bob = _peer(INVITEE_OWNER, "node-bob", 2)
    fake = fs.membership_sidecar
    fake.seed(SPACE, members=[fake.local, alice, bob])
    for row in (alice, bob):
        _directory_row(fs, row["owner"], "shared-device", "tcp://10.0.0.8:7447#stale")

    assert _service(fs, home).directory_peers() == {
        alice["deviceId"]: alice["address"], bob["deviceId"]: bob["address"],
    }
    _assert_owner(fake, alice)
    _assert_owner(fake, bob)


def test_roster_excludes_self_despite_another_owners_colliding_directory_id(home: Path) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    bob = _peer(INVITEE_OWNER, "node-bob", 1)
    fake = fs.membership_sidecar
    fake.seed(SPACE, members=[fake.local, bob])
    _directory_row(fs, OWNER, NODE_ID, OWN_ADDRESS)
    _directory_row(fs, INVITEE_OWNER, NODE_ID, "tcp://10.0.0.8:7447#spoofed-self")

    assert _service(fs, home).directory_peers() == {bob["deviceId"]: bob["address"]}
    _assert_owner(fake, bob)


def test_redeemed_inviter_is_not_shadowed_by_another_owners_directory_row(home: Path) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    _directory_row(fs, INVITEE_OWNER, "node-b", "tcp://10.0.0.8:7447#attacker")
    fake = fs.membership_sidecar
    inviter = fake.tailnets[SPACE]["members"]["node-b"]
    inviter["clientPublicByServer"] = {LOCAL_SERVER: key("derived-inviter")}
    service = _service(fs, home, recorder=Recorder())

    service.execute(_invite_link())

    assert service.directory_peers() == {"node-b": INVITER_ADDRESS}
    _assert_owner(fake, inviter)
    status = fake.membership_request("membership-status", {})
    assert owner_for_key(status, key("directory-derived-" + INVITEE_OWNER), LOCAL_SERVER) is None


def test_connect_members_maps_distinct_roster_devices_not_colliding_directory_rows(home: Path) -> None:
    fs = FakeFs()
    _create_org_spaces(fs)
    alice = _peer("alice@example", "node-alice", 1)
    bob = _peer(INVITEE_OWNER, "node-bob", 2)
    fake = fs.membership_sidecar
    # Explicit authority roster before redemption, independent of directory.
    for row in (alice, bob):
        fake.tailnets[SPACE]["members"][row["deviceId"]] = row
        _directory_row(fs, row["owner"], "shared-device", "tcp://10.0.0.8:7447#stale")
    # A stronger collision: the other owner claims Alice's actual roster id.
    _directory_row(fs, INVITEE_OWNER, alice["deviceId"], "tcp://10.0.0.9:7447#hijack")
    recorder = Recorder()
    service = _service(fs, home, recorder=recorder)

    service.execute(_invite_link())

    member_maps = [
        (peer, address)
        for kind, *rest in recorder.calls if kind == "map-peer"
        for peer, address in [rest] if address != INVITER_ADDRESS
    ]
    # Redemption already exposes the full roster at connect-inviter; the
    # later connect-members phase maps it again. Neither phase reads peers
    # from the directory, and both must preserve the exact identities.
    assert member_maps == [
        (alice["deviceId"], alice["address"]), (bob["deviceId"], bob["address"]),
    ] * 2
    _assert_owner(fake, alice)
    _assert_owner(fake, bob)
