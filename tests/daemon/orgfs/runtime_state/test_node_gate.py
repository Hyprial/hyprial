# Decisions:
# - Design §3.5 / §0.2: the node gate admits a peer only when its device row
#   in the org directory space belongs to a current member.  This test builds
#   a real org directory (``group-acme`` with the owner-created roots and one
#   device row per node, written through ``OrgFsDirectoryStore`` as the org
#   layer does) instead of trusting the tailnet.
# - Bootstrap copies the legacy device-row tests (mesh/device_rows,
#   mesh/test_orgfs_device_row_owner.py): the directory replicates while the
#   tailnet is trusted, because a member's first device row can only reach
#   the owner from that member's own, not yet known, device.  Trust is then
#   switched off; the runtime reads the flag at every gate call.
# - A member removed while partitioned keeps writing; after healing, the owner
#   and the remaining member refuse its node and never show its writes.
"""Node gate of ``StateOrgFsRuntime`` over a real org directory space."""

from __future__ import annotations

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.identity import DIRECTORY_DIR, DirectoryDevice

from .support import Peer, Peers, eventually, holds, text

ORG = "group-acme"


def _device(owner: str, node_id: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=node_id,
        owner=owner,
        server_public=f"nodekey:server-{node_id}",
        client_public=f"nodekey:client-{node_id}",
        address=f"tcp://127.0.0.1:7447#{node_id}",
        key_generation=1,
        updated_at="2026-11-25T00:00:00+00:00",
    )


def _devices(peer: Peer, space_id: str) -> set[str]:
    return {
        node.name
        for node in peer.fs.listdir(space_id, DIRECTORY_DIR, recursive=True)
        if node.kind == "doc"
    }


def _gate(peer: Peer, space_id: str, node: str) -> bool:
    return bool(peer.sync(space_id).host.supplier_allowed(node))


def test_a_removed_members_node_fails_the_node_gate_and_its_writes_never_land(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner@example")
    alice = peers.add("node-alice", "user:alice@example")
    bob = peers.add("node-bob", "user:bob@example")
    space_id = owner.fs.create_space(ORG).space_id
    OrgFsDirectoryStore(owner.fs).ensure_org_directory_roots(ORG)
    for member in (alice, bob):
        owner.fs.invite(space_id, member.author, "rw")
    for member in (alice, bob):
        assert member.fs.join(space_id).name == ORG
    for peer, bare in ((owner, "owner@example"), (alice, "alice@example"), (bob, "bob@example")):
        OrgFsDirectoryStore(peer.fs).put_device(ORG, _device(bare, peer.node_id))
    expected = {"node-owner.json", "node-alice.json", "node-bob.json"}
    for peer in (owner, alice, bob):
        eventually(lambda peer=peer: _assert_equal(_devices(peer, space_id), expected))

    for peer in (owner, alice, bob):
        assert peer.runtime is not None
        peer.runtime._trust_tailnet_members = False  # noqa: SLF001 - end of the trusted bootstrap
    assert _gate(owner, space_id, "node-alice") and _gate(owner, space_id, "node-bob")
    assert not _gate(owner, space_id, "node-stranger")
    alice.fs.write_text(space_id, "alice.md", "from alice")
    eventually(lambda: _assert_equal(text(owner.fs, space_id, "alice.md"), "from alice"))

    peers.disconnect(bob)
    owner.fs.remove_member(space_id, bob.author)
    eventually(lambda: _assert_equal({row.user for row in alice.fs.members(space_id)}, {owner.author, alice.author}))
    bob.fs.write_text(space_id, "bob.md", "written after removal")
    bob.fs.write_text(space_id, "alice.md", "overwritten after removal")
    peers.reconnect(bob)
    peers.pull_all(space_id)

    assert not _gate(owner, space_id, "node-bob")
    assert not _gate(alice, space_id, "node-bob")
    assert _gate(owner, space_id, "node-alice")

    def unchanged() -> None:
        for peer in (owner, alice):
            assert "bob.md" not in {node.name for node in peer.fs.listdir(space_id, "")}
            assert text(peer.fs, space_id, "alice.md") == "from alice"

    holds(unchanged, duration=1.0)


def _assert_equal(actual: object, expected: object) -> None:
    assert actual == expected
