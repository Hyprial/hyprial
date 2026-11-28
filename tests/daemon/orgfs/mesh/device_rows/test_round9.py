# Decisions:
# - Phase D T1: rewritten onto the state host (``device_rows/support``).
#   Honest writes are local facade calls delivered by the in-process cluster;
#   forged rows, move records and non-object values are C2 forgeries through
#   ``SyncHost.apply_remote``.  Receivers say ``protected-path`` for an author
#   the path does not allow and ``invalid-argument`` for a malformed row
#   (design §3.6); both were ``not-a-member`` in the journal.
# - Design §3.6 judges the result state.  In the member-encoded create race,
#   the owner's concurrent write of the same row loses the CRDT merge when it
#   arrives after Alice's; it then changes nothing and the state host accepts
#   it.  The test therefore asserts the verdict where the owner's write would
#   change the row (it arrives first) and the converged row in both orders.
# - A corrupt row can no longer be planted in a store; the old "legacy
#   corrupt row" test became "a corrupt row from a peer is rejected and does
#   not block that peer's later healthy rows".
# - Removal-filter checks on the physical node objects were dropped: the
#   readers (directory store, ``listdir``, ``directory_peers``) are what users
#   see.
"""ROUND 9 stateless member rows, root squats, and corrupt-row tolerance."""

from __future__ import annotations

import base64
import json
import time

import pytest

from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.membership import Accepted, Rejected
from hyprial.identity import (
    identity_slug,
    protected_directory_doc_id,
    protected_directory_node_id,
)
from hyprial.identity.impl.device import DeviceRecord, write_device_record

from tests.support.tailnet_membership import FakeMembershipSidecar, member

from .support import ALICE, BOB, CAROL, ORG, OWNER, Org, device, new_row


@pytest.fixture
def org():
    value = Org()
    try:
        yield value
    finally:
        value.close()


def _code(outcome: object) -> str | None:
    return outcome.code if isinstance(outcome, Rejected) else None


def _unverified_proof(*, expires_at: int) -> str:
    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(value).encode()
    ).rstrip(b"=").decode()
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': expires_at})}.signature"


@pytest.mark.parametrize(
    ("order", "receiver_name"),
    [
        (("owner", "alice"), "owner"),
        (("alice", "owner"), "owner"),
        (("owner", "alice"), "carol"),
        (("alice", "owner"), "carol"),
    ],
)
def test_r9_member_encoded_create_is_member_only_in_every_order(
    org: Org, order: tuple[str, str], receiver_name: str
) -> None:
    """The member's row wins in every order; the owner's write never lands."""

    space_id = org.space_id
    path = f"directory/people/{ALICE}"
    node_id = protected_directory_node_id(space_id, path, space_owner=f"user:{OWNER}")
    assert node_id is not None
    parent = org.node_id(OWNER, "directory/people")
    frames = {
        "owner": org.forge_rows(
            OWNER, [new_row(node_id, parent=parent, name=ALICE, kind="dir")], author=OWNER
        ),
        "alice": org.forge_rows(
            ALICE, [new_row(node_id, parent=parent, name=ALICE, kind="dir")], author=ALICE
        ),
    }
    receiver = OWNER if receiver_name == "owner" else CAROL

    verdicts = {actor: org.apply(receiver, frames[actor], peer=f"node-{actor}") for actor in order}

    assert verdicts["alice"] == Accepted()
    if order[0] == "owner":
        assert _code(verdicts["owner"]) == "protected-path"
    assert org.fs(receiver).stat(space_id, path).node_id == node_id
    assert org.rows(receiver)[node_id]["author"] == f"user:{ALICE}"


def test_r2_other_member_cannot_link_people_binding_row_on_any_replica(org: Org) -> None:
    space_id = org.space_id
    user_key = identity_slug(ALICE)
    org.fs(ALICE).mkdir(space_id, f"directory/people/{user_key}")
    org.deliver()
    parent = org.node_id(OWNER, f"directory/people/{user_key}")
    path = f"directory/people/{user_key}/agents-node-1.json"
    node_id = protected_directory_node_id(space_id, path, space_owner=f"user:{OWNER}")
    doc_id = protected_directory_doc_id(space_id, path, space_owner=f"user:{OWNER}")
    assert node_id is not None and doc_id is not None
    forged = org.forge_rows(
        BOB,
        [new_row(node_id, parent=parent, name="agents-node-1.json", doc_id=doc_id)],
        author=BOB,
    )

    for receiver in (OWNER, ALICE, CAROL):
        assert _code(org.apply(receiver, forged, peer="node-bob")) == "protected-path"
        assert org.missing(receiver, path)


def test_a_people_row_written_by_its_member_replicates_to_every_replica(org: Org) -> None:
    """R3: no publication-time gate; a member's own ``people`` row replicates
    whatever its content, including an unexpired proof-shaped value."""

    space_id = org.space_id
    row = {
        "user": ALICE,
        "larkUnionId": "on_alice",
        "proof": _unverified_proof(expires_at=int(time.time()) + 3_600),
        "publishedAt": "2026-10-07T12:00:00+00:00",
    }
    path = f"directory/people/{identity_slug(ALICE)}/agents-node-1.json"
    org.fs(ALICE).mkdir(space_id, path.rsplit("/", 1)[0])
    org.fs(ALICE).write_text(space_id, path, json.dumps(row))
    org.deliver()

    for user in (OWNER, ALICE, BOB, CAROL):
        assert json.loads(org.text(user, path)) == row


def test_r9_move_cannot_squat_the_shared_directory_root(org: Org) -> None:
    """A move result cannot give a random node a protected ancestor name."""

    space_id = org.space_id
    squatter = org.fs(ALICE).mkdir(space_id, "S")
    org.fs(ALICE).mkdir(space_id, "S/devices")
    org.deliver()
    forged = org.forge_move(
        ALICE, squatter.node_id, old_parent="root", new_parent="root", name="directory"
    )

    for receiver in (OWNER, BOB, CAROL):
        assert _code(org.apply(receiver, forged, peer="node-alice")) == "protected-path"
        assert org.directory(receiver).list_devices(ORG) == []
        assert org.fs(receiver).stat(space_id, "S/devices").kind == "dir"


def test_r9_owner_cannot_delete_a_member_encoded_row(org: Org) -> None:
    """Member identity, not org ownership, authorizes a protected deletion."""

    row = device(ALICE, "kept", address="tcp://10.0.0.7:7447#kept")
    org.directory(ALICE).put_device(ORG, row)
    org.deliver()
    node_id = org.node_id(OWNER, f"directory/devices/{ALICE}/kept.json")
    forged = org.forge_rewrite(OWNER, node_id, author=OWNER, deleted=True)

    for receiver in (OWNER, BOB):
        assert _code(org.apply(receiver, forged, peer="node-owner")) == "protected-path"
        assert org.directory(receiver).list_devices(ORG) == [row]


def test_r9_removed_member_rows_are_inert_for_devices_people_and_peers(
    org: Org, tmp_path
) -> None:
    """Active meta membership filters both protected read trees on each replica."""

    space_id = org.space_id
    row = device(ALICE, "alice-device", address="tcp://10.0.0.7:7447#alice-secret")
    org.directory(ALICE).put_device(ORG, row)
    org.fs(ALICE).mkdir(space_id, f"directory/people/{ALICE}")
    org.fs(ALICE).write_text(space_id, f"directory/people/{ALICE}/profile.json", "{}")
    org.deliver()
    for user in (OWNER, ALICE, BOB, CAROL):
        assert org.directory(user).list_devices(ORG) == [row]
        assert [item.name for item in org.fs(user).listdir(space_id, "directory/people")] == [ALICE]

    org.fs(OWNER).remove_member(space_id, f"user:{ALICE}")
    org.deliver()

    for user in (OWNER, ALICE, BOB, CAROL):
        assert org.directory(user).list_devices(ORG) == []
        assert org.fs(user).listdir(space_id, "directory/people") == ()
        home = tmp_path / f"home-{org[user].node_id}"
        home.mkdir()
        local = member(OWNER, "observer")
        local["masterPublic"] = "nodekey:" + "f" * 64
        peer = member(ALICE, "alice-device")
        sidecar = FakeMembershipSidecar(local=local)
        sidecar.seed(space_id, members=[local, peer])
        write_device_record(home, DeviceRecord(
            device_id="observer", owner=OWNER,
            server_public=local["serverPublic"], client_public=local["masterPublic"],
        ))
        service = OrgNetworkService(
            fs=org.fs(user),
            home=home,
            node_id="observer",
            owner=OWNER,
            invalid_devices=InvalidDeviceQuarantine(),
            membership_request=sidecar.membership_request,
        )
        assert service.directory_peers() == {"alice-device": peer["address"]}
        sidecar.membership_request("member-revoke", {
            "operationId": "revoke-alice", "tailnetId": space_id,
            "deviceIds": ["alice-device"], "expectedRevision": 1,
            "confirmed": True, "reason": "fixture-explicit-removal",
        })
        assert service.directory_peers() == {}


def test_r9_non_json_tree_row_does_not_block_later_updates(org: Org) -> None:
    """A corrupt row from a peer is refused without blocking its healthy rows."""

    space_id = org.space_id
    corrupt = org.forge_tree(
        BOB, lambda nodes: nodes.__setitem__("legacy-invalid-json", "not-json"), author=BOB
    )
    healthy = org.forge_rows(
        BOB,
        [new_row("round9-healthy-node", parent="root", name="round9-healthy", kind="dir")],
        author=BOB,
    )

    for receiver in (OWNER, ALICE):
        assert _code(org.apply(receiver, corrupt, peer="node-bob")) == "invalid-argument"
        assert org.apply(receiver, healthy, peer="node-bob") == Accepted()
        assert org.fs(receiver).stat(space_id, "round9-healthy").kind == "dir"
        assert "legacy-invalid-json" not in org.rows(receiver)


def test_r11_row_rewrite_cannot_rename_an_ordinary_node_to_directory(org: Org) -> None:
    """A direct row rewrite obeys the same protected-result rule as a move."""

    space_id = org.space_id
    squatter = org.fs(ALICE).mkdir(space_id, "S")
    org.fs(ALICE).mkdir(space_id, "S/devices")
    org.deliver()
    forged = org.forge_rewrite(ALICE, squatter.node_id, author=ALICE, name="directory")

    for receiver in (OWNER, BOB, CAROL):
        assert _code(org.apply(receiver, forged, peer="node-alice")) == "protected-path"
        assert org.directory(receiver).list_devices(ORG) == []
        assert org.fs(receiver).stat(space_id, "S/devices").kind == "dir"


@pytest.mark.parametrize(
    ("order", "receiver_name"),
    [
        (("bob", "alice"), "owner"),
        (("alice", "bob"), "owner"),
        (("bob", "alice"), "carol"),
        (("alice", "bob"), "carol"),
    ],
)
def test_r11_non_object_protected_node_write_is_rejected_in_every_order(
    org: Org, order: tuple[str, str], receiver_name: str
) -> None:
    """An incoming non-object node value cannot win or erase a protected row."""

    space_id = org.space_id
    path = f"directory/devices/{ALICE}"
    node_id = protected_directory_node_id(space_id, path, space_owner=f"user:{OWNER}")
    assert node_id is not None
    parent = org.node_id(OWNER, "directory/devices")
    frames = {
        "alice": org.forge_rows(
            ALICE, [new_row(node_id, parent=parent, name=ALICE, kind="dir")], author=ALICE
        ),
        "bob": org.forge_tree(BOB, lambda nodes: nodes.__setitem__(node_id, 7), author=BOB),
    }
    receiver = OWNER if receiver_name == "owner" else CAROL

    verdicts = {actor: org.apply(receiver, frames[actor], peer=f"node-{actor}") for actor in order}

    assert verdicts["alice"] == Accepted()
    assert _code(verdicts["bob"]) == "invalid-argument"
    assert org.fs(receiver).stat(space_id, path).node_id == node_id
