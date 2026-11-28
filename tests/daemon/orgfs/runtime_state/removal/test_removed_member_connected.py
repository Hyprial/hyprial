# Decisions (package F6, real-node finding R3-1, 2026-11-25):
# - Three nodes stay connected while the owner removes one member, the shape
#   of the real three-node run: the removed member learns its removal from the
#   meta broadcast and then must hold meta only.  Broadcast tree and content
#   updates of the other two members are dropped on arrival, its own blob and
#   content requests are not made, and a supplier answers a blob request of a
#   removed (or never-invited) requester with no chunk.  Later member changes
#   (meta) still arrive.  Re-adding the member restores sync without manual
#   pulls.
# - Device owners come from a fixed ``node -> author`` map with trust off, as
#   in ``test_removed_member.py``, so the node gate is the production gate.
# - The owner's status lists only current members' nodes in
#   ``holders_online`` and counts updates its receive gate refused in
#   ``rejected_updates`` (R3-2, R3-3).
# - The removed member's due maintenance tick schedules no pull for the space
#   once a pull observed the holder's meta (R3-6).
"""A connected member removed by the owner keeps meta only, until re-added."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.blobs import encode_request
from hyprial.daemon.impl.transport import KeySpace

from ..support import Peers, eventually, holds, text

OWNERS = {
    "node-alice": "user:alice@example",
    "node-bob": "user:bob@example",
    "node-carol": "user:carol@example",
    "node-stranger": "user:stranger@example",
}
DAVE = "user:dave@example"
BINARY = bytes(range(256)) * 16


class _FixedOwnerRuntime(OrgFsRuntime):
    def _device_owner_resolver(self, space_id: str) -> Any:
        return OWNERS.get


def _add(peers: Peers, name: str):
    return peers.add(name, OWNERS[name], trust=False, runtime_class=_FixedOwnerRuntime)


def _names(peer, space_id: str) -> set[str]:
    return {node.path for node in peer.fs.listdir(space_id, "", recursive=True)}


def _members(peer, space_id: str) -> set[str]:
    return {row.user for row in peer.fs.members(space_id)}


def _team(peers: Peers):
    alice, bob, carol = (_add(peers, name) for name in ("node-alice", "node-bob", "node-carol"))
    space_id = alice.fs.create_space("team").space_id
    alice.fs.invite(space_id, bob.author, "rw")
    alice.fs.invite(space_id, carol.author, "rw")
    bob.fs.join(space_id)
    carol.fs.join(space_id)
    alice.fs.write_text(space_id, "a0.md", "before")
    for peer in (bob, carol):
        eventually(lambda peer=peer: _assert(text(peer.fs, space_id, "a0.md") == "before"))
    return alice, bob, carol, space_id


def _blob_answer(peer, space_id: str, digest: str, requester: str) -> dict:
    replies = peer.sync(space_id).session.query(
        KeySpace().orgfs_blob(digest),
        encode_request(space_id, digest, requester_node=peer.node_id, requester_author=requester),
        timeout=2.0,
        all_replies=True,
    )
    return {"types": sorted({json.loads(reply.payload)["type"] for reply in replies}),
            "codes": sorted({str(json.loads(reply.payload).get("code")) for reply in replies})}


def test_a_connected_removed_member_gets_meta_only_until_re_added(peers: Peers) -> None:
    alice, bob, carol, space_id = _team(peers)
    tree_before = _names(bob, space_id)

    alice.fs.remove_member(space_id, bob.author)
    for peer in (bob, carol):
        eventually(lambda peer=peer: _assert(bob.author not in _members(peer, space_id)))

    alice.fs.write_text(space_id, "a1.md", "alice after removal")
    alice.fs.write_text(space_id, "a0.md", "rewritten after removal")
    alice.fs.move(space_id, "a0.md", "a0-moved.md")
    carol.fs.write_text(space_id, "c1.md", "carol after removal")
    digest = carol.fs.write_bytes(space_id, "c1.bin", BINARY).blob_hash
    assert digest == hashlib.sha256(BINARY).hexdigest()
    alice.fs.invite(space_id, DAVE, "ro")
    for peer in (alice, carol):
        eventually(lambda peer=peer: _assert({"a1.md", "c1.md", "c1.bin", "a0-moved.md"} <= _names(peer, space_id)))

    eventually(lambda: _assert(_members(bob, space_id) == {alice.author, carol.author, DAVE}))
    bob.sync(space_id).request_blob(digest).wait(5.0)

    def meta_only() -> None:
        assert _names(bob, space_id) == tree_before
        assert text(bob.fs, space_id, "a0.md") == "before"
        assert not bob.runtime.blobs.contains(digest)

    holds(meta_only, duration=1.0)
    assert _blob_answer(bob, space_id, digest, bob.author)["types"] == ["orgfs-error"]
    assert "not-a-member" in _blob_answer(bob, space_id, digest, bob.author)["codes"]
    assert "orgfs-blob-chunk" in _blob_answer(alice, space_id, digest, alice.author)["types"]

    status = alice.runtime.status(space_id)
    assert bob.node_id not in status.holders_online
    assert carol.node_id in status.holders_online

    alice.fs.invite(space_id, bob.author, "rw")
    eventually(lambda: _assert(text(bob.fs, space_id, "a1.md") == "alice after removal"))
    eventually(lambda: _assert(text(bob.fs, space_id, "c1.md") == "carol after removal"))
    eventually(lambda: _assert(text(bob.fs, space_id, "a0-moved.md") == "rewritten after removal"))
    assert bob.runtime.await_content(
        space_id, bob.fs.stat(space_id, "c1.bin").node_id, deadline_monotonic=time.monotonic() + 10.0
    )
    assert bob.fs.read_bytes(space_id, "c1.bin") == BINARY


def test_a_blob_supplier_refuses_a_requester_that_never_was_a_member(peers: Peers) -> None:
    alice = _add(peers, "node-alice")
    space_id = alice.fs.create_space("team").space_id
    digest = alice.fs.write_bytes(space_id, "x.bin", BINARY).blob_hash
    stranger = OWNERS["node-stranger"]

    answer = _blob_answer(alice, space_id, digest, stranger)

    assert answer == {"types": ["orgfs-error"], "codes": ["not-a-member"]}


def test_a_removed_member_pushes_count_as_rejected_updates_at_the_owner(peers: Peers) -> None:
    alice, bob, _carol, space_id = _team(peers)
    peers.disconnect(bob)
    alice.fs.remove_member(space_id, bob.author)
    update = bob.fs.sync_host(space_id).read_update("tree", b"\x00")
    assert update is not None
    before = alice.runtime.status(space_id).rejected_updates
    peers.reconnect(bob)

    bob.sync(space_id).publish("tree", update[0], update[1])

    eventually(lambda: _assert(alice.runtime.status(space_id).rejected_updates == before + 1))
    assert alice.fs.status(space_id).rejected_updates == before + 1


def test_a_removed_member_maintenance_tick_does_not_pull_a_space_it_observed(peers: Peers) -> None:
    alice, bob, carol, space_id = _team(peers)
    alice.fs.remove_member(space_id, bob.author)
    eventually(lambda: _assert(bob.author not in _members(bob, space_id)))
    peers.disconnect(bob)
    alice.fs.write_text(space_id, "missed.md", "while bob was away")
    peers.reconnect(bob)
    peers.pull_all(space_id)
    eventually(lambda: _assert(bob.sync(space_id).observations()))
    scheduled: list[str] = []
    sync = bob.sync(space_id)
    sync.schedule_sync_from = lambda peer: scheduled.append(peer) or True

    bob.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - the due tick hook

    assert scheduled == []
    assert "missed.md" not in _names(bob, space_id)
    assert {item.node for item in sync.observations()} <= {alice.node_id, carol.node_id}


def _assert(condition: bool) -> None:
    assert condition
