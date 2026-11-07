"""ROUND 7 deterministic protected-tree identities and move admission."""

from __future__ import annotations

import base64
import json

import pytest
from pycrdt import Array, Doc, Map

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import (
    parse_protected_directory_node_id,
    protected_directory_node_id,
)
from tests.daemon.orgfs.mesh.device_rows import test_round4 as round4
from tests.daemon.orgfs.mesh.device_rows.test_round4 import (
    ALICE,
    BOB,
    OWNER,
    _deliver_all,
    _position,
    _receive,
)


@pytest.fixture
def replicas(tmp_path):
    yield from round4.replicas.__wrapped__(tmp_path)


def _expected_node_id(space_id: str, path: str, principal: str) -> str:
    values = (space_id, principal, path)
    payload = "".join(f"{len(value)}:{value}" for value in values).encode()
    token = base64.b32encode(payload).decode("ascii").rstrip("=").lower()
    return f"node-orgdir-v1-{token}"


def _tree_envelope(
    runtime: OrgFsRuntime,
    space_id: str,
    *,
    author: str,
    mutate,
) -> bytes:
    store = runtime.stores[space_id]
    doc_id = store.active_tree_doc_id()
    assert doc_id is not None
    writer = store._writer_for(f"user:{author}", None)
    doc = Doc(client_id=store._client_id(writer))
    doc.apply_update(store.snapshot(doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    mutate(doc)
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = ?",
            (writer, doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        doc_id,
        writer,
        seq,
        doc.get_update(before),
        author=f"user:{author}",
        actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _add_dir(doc: Doc, *, node_id: str, parent: str, name: str) -> None:
    doc.get("nodes", type=Map)[node_id] = json.dumps(
        {
            "blob_hash": None,
            "deleted": False,
            "doc_id": None,
            "kind": "dir",
            "name": name,
            "node_id": node_id,
            "parent": parent,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _move(
    doc: Doc,
    *,
    node_id: str,
    old_parent: str,
    new_parent: str,
    name: str,
    peer: str,
) -> None:
    doc.get("moves", type=Array).append(
        json.dumps(
            {
                "name": name,
                "newParent": new_parent,
                "node": node_id,
                "oldParent": old_parent,
                "peer": peer,
                "ts": 959,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _rewrite(doc: Doc, node_id: str, **changes: object) -> None:
    nodes = doc.get("nodes", type=Map)
    value = json.loads(str(nodes[node_id]))
    value.update(changes)
    nodes[node_id] = json.dumps(value, sort_keys=True, separators=(",", ":"))


def _prepare_repro(replicas):
    _tmp_path, space_id, owner, alice, bob, carol = replicas
    before = _position(bob, space_id)
    bob.facade.mkdir(space_id, f"directory/devices/{BOB}")
    _deliver_all(bob, (owner, alice, carol), space_id, before)

    before = _position(alice, space_id)
    folder = alice.facade.mkdir(space_id, "F")
    child = alice.facade.write_text(space_id, "x.txt", "x")
    _deliver_all(alice, (owner, bob, carol), space_id, before)
    bob_parent = bob.facade.stat(space_id, f"directory/devices/{BOB}")

    alice_move = _tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda doc: _move(
            doc,
            node_id=child.node_id,
            old_parent="root",
            new_parent=folder.node_id,
            name="x.txt",
            peer=alice.node_id,
        ),
    )
    bob_move = _tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda doc: _move(
            doc,
            node_id=folder.node_id,
            old_parent="root",
            new_parent=bob_parent.node_id,
            name="F",
            peer=bob.node_id,
        ),
    )
    return space_id, owner, alice, bob, carol, alice_move, bob_move


def test_r7_every_protected_tree_node_uses_its_deterministic_id(replicas) -> None:
    _tmp_path, space_id, owner, alice, bob, _carol = replicas
    paths = {
        "directory": f"user:{OWNER}",
        "directory/devices": f"user:{OWNER}",
        "directory/people": f"user:{OWNER}",
        "directory/leaves": f"user:{OWNER}",
    }
    for path, principal in paths.items():
        expected = _expected_node_id(space_id, path, principal)
        assert protected_directory_node_id(
            space_id, path, space_owner=f"user:{OWNER}"
        ) == expected
        assert parse_protected_directory_node_id(expected) == (
            space_id,
            principal,
            path,
        )
        assert owner.facade.stat(space_id, path).node_id == expected

    before = _position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}")
    alice.facade.write_text(
        space_id, f"directory/devices/{ALICE}/node-a.json", "{}"
    )
    _deliver_all(alice, (owner, bob), space_id, before)
    for replica in (owner, alice, bob):
        for path in (
            f"directory/devices/{ALICE}",
            f"directory/devices/{ALICE}/node-a.json",
        ):
            assert replica.facade.stat(space_id, path).node_id == _expected_node_id(
                space_id, path, f"user:{ALICE}"
            )


def test_r7_create_under_protected_parent_requires_deterministic_child_id_and_author(
    replicas,
) -> None:
    _tmp_path, space_id, owner, alice, bob, _carol = replicas
    before = _position(alice, space_id)
    alice.facade.mkdir(space_id, f"directory/devices/{ALICE}")
    _deliver_all(alice, (owner, bob), space_id, before)
    parent = owner.facade.stat(space_id, f"directory/devices/{ALICE}")
    expected = _expected_node_id(
        space_id, f"directory/devices/{ALICE}/child", f"user:{ALICE}"
    )

    wrong_id = _tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda doc: _add_dir(
            doc, node_id="random-child", parent=parent.node_id, name="child"
        ),
    )
    wrong_author = _tree_envelope(
        bob,
        space_id,
        author=BOB,
        mutate=lambda doc: _add_dir(
            doc, node_id=expected, parent=parent.node_id, name="child"
        ),
    )
    for envelope, supplier in ((wrong_id, alice.node_id), (wrong_author, bob.node_id)):
        for receiver in (owner, bob):
            result = _receive(receiver, space_id, envelope, supplier)
            assert (result.status, result.code) == ("rejected", "not-a-member")


def test_r7_moves_are_refused_when_any_operation_id_is_protected(replicas) -> None:
    _tmp_path, space_id, owner, alice, bob, _carol = replicas
    before = _position(bob, space_id)
    bob.facade.mkdir(space_id, f"directory/devices/{BOB}")
    folder = bob.facade.mkdir(space_id, "ordinary")
    bob.facade.write_text(space_id, "ordinary.txt", "ordinary")
    _deliver_all(bob, (owner, alice), space_id, before)

    with pytest.raises(OrgFsError) as into_protected:
        bob.facade.move(
            space_id, f"id:{folder.node_id}", f"directory/devices/{BOB}/ordinary"
        )
    assert into_protected.value.code == "not-a-member"

    moved = bob.facade.move(space_id, "ordinary.txt", "ordinary/ordinary.txt")
    assert moved.path == "ordinary/ordinary.txt"
    assert bob.stores[space_id]._db.execute(
        "SELECT COUNT(*) FROM pending"
    ).fetchone()[0] == 0


def test_r7_delete_of_protected_id_is_owner_or_encoded_user_only(replicas) -> None:
    _tmp_path, space_id, owner, alice, bob, _carol = replicas
    before = _position(bob, space_id)
    bob.facade.mkdir(space_id, f"directory/devices/{BOB}")
    row = bob.facade.write_text(
        space_id, f"directory/devices/{BOB}/row.json", "{}"
    )
    _deliver_all(bob, (owner, alice), space_id, before)

    forged = _tree_envelope(
        alice,
        space_id,
        author=ALICE,
        mutate=lambda doc: _rewrite(doc, row.node_id, deleted=True),
    )
    for receiver in (owner, bob):
        result = _receive(receiver, space_id, forged, alice.node_id)
        assert (result.status, result.code) == ("rejected", "not-a-member")

    bob.facade.remove(space_id, f"id:{row.node_id}")


@pytest.mark.parametrize(
    ("order", "receiver_name"),
    [
        (("alice", "bob"), "owner"),
        (("bob", "alice"), "owner"),
        (("alice", "bob"), "carol"),
        (("bob", "alice"), "carol"),
    ],
)
def test_r7_959_repro_converges_in_both_orders_on_two_replicas(
    replicas, order: tuple[str, str], receiver_name: str
) -> None:
    space_id, owner, alice, bob, carol, alice_move, bob_move = _prepare_repro(replicas)
    receiver = owner if receiver_name == "owner" else carol
    envelopes = {"alice": alice_move, "bob": bob_move}
    suppliers = {"alice": alice.node_id, "bob": bob.node_id}
    verdicts = {}
    for actor in order:
        result = _receive(receiver, space_id, envelopes[actor], suppliers[actor])
        verdicts[actor] = (result.status, result.code)

    assert verdicts == {
        "alice": ("applied", None),
        "bob": ("rejected", "not-a-member"),
    }
    assert receiver.facade.read_text(space_id, "F/x.txt")[0] == "x"
    with pytest.raises(OrgFsError) as missing:
        receiver.facade.stat(space_id, f"directory/devices/{BOB}/F")
    assert missing.value.code == "unknown-doc"
    assert receiver.stores[space_id]._db.execute(
        "SELECT COUNT(*) FROM pending"
    ).fetchone()[0] == 0
    bob_value = json.loads(bob_move)
    assert receiver.stores[space_id]._db.execute(
        "SELECT 1 FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
        (
            bob_value["origin"]["writer"],
            bob_value["seq"],
            bob_value["docId"],
        ),
    ).fetchone() is None
