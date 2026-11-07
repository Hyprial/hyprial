"""ROUND 9 stateless member rows, root squats, and corrupt-row tolerance."""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest
from pycrdt import Map

from hyprial.daemon.impl.org.network import directory as directory_module
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.store import admission as admission_module
from hyprial.identity import (
    identity_slug,
    protected_directory_doc_id,
    protected_directory_node_id,
)

from hyprial.identity.impl.device import DeviceRecord, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar, member

from .test_round4 import (
    ALICE,
    BOB,
    OWNER,
    _add_tree_doc,
    _commits_since,
    _deliver_all,
    _device,
    _position,
    _receive,
    replicas,
)
from .test_round7 import _add_dir, _move, _rewrite, _tree_envelope

_IMPORTED_REPLICAS_FIXTURE = replicas


@pytest.fixture
def round9_replicas(request):
    assert _IMPORTED_REPLICAS_FIXTURE is not None
    return request.getfixturevalue("replicas")


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
    round9_replicas, order: tuple[str, str], receiver_name: str
) -> None:
    """The update's own deterministic id decides, even when its CRDT op loses."""

    _tmp_path, space_id, owner, alice, _bob, carol = round9_replicas
    path = f"directory/people/{ALICE}"
    node_id = protected_directory_node_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert node_id is not None
    parent = owner.facade.stat(space_id, "directory/people")
    envelopes = {
        actor: _tree_envelope(
            runtime,
            space_id,
            author=actor_name,
            mutate=lambda doc: _add_dir(
                doc,
                node_id=node_id,
                parent=parent.node_id,
                name=ALICE,
            ),
        )
        for actor, actor_name, runtime in (
            ("owner", OWNER, owner),
            ("alice", ALICE, alice),
        )
    }
    receiver = owner if receiver_name == "owner" else carol
    suppliers = {"owner": owner.node_id, "alice": alice.node_id}
    verdicts = {}

    for actor in order:
        result = _receive(receiver, space_id, envelopes[actor], suppliers[actor])
        verdicts[actor] = (result.status, result.code)

    assert verdicts == {
        "owner": ("rejected", "not-a-member"),
        "alice": ("applied", None),
    }
    assert receiver.facade.stat(space_id, path).node_id == node_id


def test_r2_other_member_cannot_link_people_binding_row_on_any_replica(
    round9_replicas,
) -> None:
    _tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    user_key = identity_slug(ALICE)
    alice.facade.mkdir(space_id, f"directory/people/{user_key}")
    _deliver_all(alice, (owner, bob, carol), space_id, before)
    parent = owner.facade.stat(space_id, f"directory/people/{user_key}")
    path = f"directory/people/{user_key}/binding.json"
    node_id = protected_directory_node_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    doc_id = protected_directory_doc_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert node_id is not None and doc_id is not None
    envelope = _tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda doc: _add_tree_doc(
            doc.get("nodes", type=Map),
            node_id=node_id,
            parent=parent.node_id,
            name="binding.json",
            doc_id=doc_id,
        ),
    )

    for replica in (owner, alice, carol):
        result = _receive(replica, space_id, envelope, bob.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        with pytest.raises(OrgFsError) as missing:
            replica.facade.stat(space_id, path)
        assert missing.value.code == "unknown-doc"


def test_r2_binding_row_publishes_with_exact_shape_to_every_replica(
    round9_replicas,
) -> None:
    _tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    row = {
        "user": ALICE,
        "larkUnionId": "on_alice",
        "proof": _unverified_proof(expires_at=1),
        "publishedAt": "2026-10-07T12:00:00+00:00",
    }
    OrgFsDirectoryStore(alice.facade).put_binding("group-acme", row)
    _deliver_all(alice, (owner, bob, carol), space_id, before)

    for replica in (owner, alice, bob, carol):
        assert OrgFsDirectoryStore(replica.facade).list_binding_rows(
            "group-acme"
        ) == [(identity_slug(ALICE), row)]
        text, _version = replica.facade.read_text(
            space_id, f"directory/people/{identity_slug(ALICE)}/binding.json"
        )
        assert set(json.loads(text)) == {
            "user",
            "larkUnionId",
            "proof",
            "publishedAt",
        }


def test_r2f_future_binding_proof_is_accepted_on_peer_import(
    round9_replicas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receiver clock behind the publisher must not reject replicated facts."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    expires_at = int(time.time()) + 3_600
    row = {
        "user": ALICE,
        "larkUnionId": "on_alice",
        "proof": _unverified_proof(expires_at=expires_at),
        "publishedAt": "2026-10-07T12:00:00+00:00",
    }

    with monkeypatch.context() as source_clock:
        advanced_time = SimpleNamespace(time=lambda: expires_at + 120)
        source_clock.setattr(admission_module, "time", advanced_time)
        source_clock.setattr(directory_module, "time", advanced_time)
        OrgFsDirectoryStore(alice.facade).put_binding("group-acme", row)

    assert expires_at > time.time()
    for envelope in _commits_since(alice, space_id, before):
        result = _receive(owner, space_id, envelope, alice.node_id)
        assert (result.status, result.code) == ("applied", None)
    assert OrgFsDirectoryStore(owner.facade).list_binding_rows("group-acme") == [
        (identity_slug(ALICE), row)
    ]


def test_r9_move_cannot_squat_the_shared_directory_root(round9_replicas) -> None:
    """A move result cannot give a random node a protected ancestor name."""

    _tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    squatter = alice.facade.mkdir(space_id, "S")
    alice.facade.mkdir(space_id, "S/devices")
    _deliver_all(alice, (owner, bob, carol), space_id, before)
    envelope = _tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda doc: _move(
            doc,
            node_id=squatter.node_id,
            old_parent="root",
            new_parent="root",
            name="directory",
            peer=alice.node_id,
        ),
    )

    for receiver in (owner, bob, carol):
        result = _receive(receiver, space_id, envelope, alice.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert OrgFsDirectoryStore(receiver.facade).list_devices("group-acme") == []
        assert receiver.facade.stat(space_id, "S/devices").kind == "dir"


def test_r9_owner_cannot_delete_a_member_encoded_row(round9_replicas) -> None:
    """Member identity, not org ownership, authorizes a protected deletion."""

    _tmp_path, space_id, owner, alice, bob, _carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    row = _device(ALICE, "kept", address="tcp://10.0.0.7:7447#kept")
    OrgFsDirectoryStore(alice.facade).put_device("group-acme", row)
    _deliver_all(alice, (owner, bob), space_id, before)
    node = owner.facade.stat(
        space_id, f"directory/devices/{ALICE}/kept.json"
    )
    envelope = _tree_envelope(
        owner,
        space_id,
        author=OWNER,
        mutate=lambda doc: _rewrite(doc, node.node_id, deleted=True),
    )

    for receiver in (owner, bob):
        result = _receive(receiver, space_id, envelope, owner.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert OrgFsDirectoryStore(receiver.facade).list_devices("group-acme") == [row]


def test_r9_removed_member_rows_are_inert_for_devices_people_and_peers(
    round9_replicas,
) -> None:
    """Active meta membership filters both protected read trees on each replica."""

    tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    row = _device(
        ALICE, "alice-device", address="tcp://10.0.0.7:7447#alice-secret"
    )
    OrgFsDirectoryStore(alice.facade).put_device("group-acme", row)
    alice.facade.mkdir(space_id, f"directory/people/{ALICE}")
    profile = alice.facade.write_text(
        space_id, f"directory/people/{ALICE}/profile.json", "{}"
    )
    _deliver_all(alice, (owner, bob, carol), space_id, before)
    device = alice.facade.stat(
        space_id, f"directory/devices/{ALICE}/alice-device.json"
    )
    for replica in (owner, alice, bob, carol):
        assert OrgFsDirectoryStore(replica.facade).list_devices("group-acme") == [row]
        assert [
            item.name for item in replica.facade.listdir(space_id, "directory/people")
        ] == [ALICE]

    owner._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(owner, space_id)
    owner.facade.remove_member(space_id, f"user:{ALICE}")
    _deliver_all(owner, (alice, bob, carol), space_id, before)

    for replica in (owner, alice, bob, carol):
        assert OrgFsDirectoryStore(replica.facade).list_devices("group-acme") == []
        assert replica.facade.listdir(space_id, "directory/people") == ()
        home = tmp_path / f"home-{replica.node_id}"
        home.mkdir()
        # A direct OrgFS removal is not a tailnet revoke (§6).
        local = member(OWNER, "observer")
        local["masterPublic"] = "nodekey:" + "f" * 64  # This fixture initiates the dial.
        peer = member(ALICE, "alice-device")
        sidecar = FakeMembershipSidecar(local=local)
        sidecar.seed(space_id, members=[local, peer])
        write_device_record(home, DeviceRecord(
            device_id="observer", owner=OWNER,
            server_public=local["serverPublic"], client_public=local["masterPublic"],
        ))
        service = OrgNetworkService(
            fs=replica.facade,
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
        physical = replica.facade._space(space_id).nodes
        assert not physical[device.node_id].deleted
        assert not physical[profile.node_id].deleted


def test_r9_non_json_tree_row_does_not_block_later_updates(
    round9_replicas, caplog
) -> None:
    """A legacy corrupt row is warning-only during admission of healthy work."""

    _tmp_path, space_id, owner, alice, bob, _carol = round9_replicas
    tree_doc_id = owner.stores[space_id].active_tree_doc_id()
    assert tree_doc_id is not None
    for receiver in (owner, alice):
        receiver.stores[space_id]._docs[tree_doc_id].get("nodes", type=Map)[
            "legacy-invalid-json"
        ] = "not-json"
    healthy = _tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda doc: _add_dir(
            doc,
            node_id="round9-healthy-node",
            parent="root",
            name="round9-healthy",
        ),
    )
    caplog.set_level("WARNING")

    for receiver in (owner, alice):
        result = _receive(receiver, space_id, healthy, bob.node_id)
        assert (result.status, result.code) == ("applied", None)
        assert receiver.facade.stat(space_id, "round9-healthy").kind == "dir"

    assert "orgfs skipped invalid tree row" in caplog.text


def test_r11_row_rewrite_cannot_rename_an_ordinary_node_to_directory(
    round9_replicas,
) -> None:
    """A direct row rewrite obeys the same protected-result rule as a move."""

    _tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    alice._meshes[space_id].broadcast_pending = lambda _space_id=None: 0
    before = _position(alice, space_id)
    squatter = alice.facade.mkdir(space_id, "S")
    alice.facade.mkdir(space_id, "S/devices")
    _deliver_all(alice, (owner, bob, carol), space_id, before)
    envelope = _tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda doc: _rewrite(doc, squatter.node_id, name="directory"),
    )

    for receiver in (owner, bob, carol):
        result = _receive(receiver, space_id, envelope, alice.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")
        assert OrgFsDirectoryStore(receiver.facade).list_devices("group-acme") == []
        assert receiver.facade.stat(space_id, "S/devices").kind == "dir"


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
    round9_replicas, order: tuple[str, str], receiver_name: str
) -> None:
    """An incoming non-object node value cannot win or erase a protected row."""

    _tmp_path, space_id, owner, alice, bob, carol = round9_replicas
    path = f"directory/devices/{ALICE}"
    node_id = protected_directory_node_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert node_id is not None
    parent = owner.facade.stat(space_id, "directory/devices")
    envelopes = {
        "alice": _tree_envelope(
            alice,
            space_id,
            author=ALICE,
            mutate=lambda doc: _add_dir(
                doc,
                node_id=node_id,
                parent=parent.node_id,
                name=ALICE,
            ),
        ),
        "bob": _tree_envelope(
            bob,
            space_id,
            author=BOB,
            mutate=lambda doc: doc.get("nodes", type=Map).__setitem__(node_id, 7),
        ),
    }
    receiver = owner if receiver_name == "owner" else carol
    suppliers = {"alice": alice.node_id, "bob": bob.node_id}
    verdicts = {}

    for actor in order:
        result = _receive(receiver, space_id, envelopes[actor], suppliers[actor])
        verdicts[actor] = (result.status, result.code)

    assert verdicts == {
        "alice": ("applied", None),
        "bob": ("rejected", "not-a-member"),
    }
    assert receiver.facade.stat(space_id, path).node_id == node_id
