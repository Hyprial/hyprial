# Decisions (phase F1, item 5, D2 report §8, 2026-11-25):
# - ``await_content``, ``fetch_blob``, ``refresh_space`` and ``known_holders``
#   of the state runtime get their own tests.  A node that knows a file but
#   not its content is made by handing it only the holder's tree update
#   through its ``SyncHost`` (what a tree-first pull round leaves behind),
#   with the holder partitioned when the test needs it unreachable.
# - T2 Q-1 ruling: ``stat`` of a remote birth whose content has not arrived
#   reports ``pending`` before any read, from what the host already knows in
#   memory; after a restart the document is unverified and reports
#   ``arrived`` (design §9.1) until verification classifies it.  These tests
#   do not read before ``stat``, since a read itself marks the document.
# - Deadlines are asserted as upper bounds with generous slack; outcomes, not
#   timings, are the contract.
"""Content provisioning of ``StateOrgFsRuntime``: await, fetch, refresh, holders."""

from __future__ import annotations

import hashlib
import time

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.kernel import ORGFS_CONTENT_PENDING

from ..support import Peer, Peers, eventually

OWNER = "user:owner@example"
MEMBER = "user:member@example"
BLOB = b"\x00\x01" + b"blob-bytes" * 4096


def _pair(peers: Peers) -> tuple[Peer, Peer, str]:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    member.fs.join(space_id)
    return owner, member, space_id


def _tree_only(source: Peer, target: Peer, space_id: str) -> None:
    since = target.fs.sync_host(space_id).read_vectors(("tree",))["tree"]
    found = source.fs.sync_host(space_id).read_update("tree", since)
    assert found is not None
    update, vector = found
    target.fs.sync_host(space_id).apply_remote("tree", update, vector, source.node_id)


def _write_unseen(
    peers: Peers, owner: Peer, member: Peer, space_id: str, *, blob: bool, check: bool = True
) -> str:
    peers.disconnect(member)
    if blob:
        node_id = owner.fs.write_bytes(space_id, "data.bin", BLOB).node_id
    else:
        node_id = owner.fs.write_text(space_id, "note.md", "content from the owner").node_id
    _tree_only(owner, member, space_id)
    if not check:
        return node_id
    if blob:
        assert member.fs.stat(space_id, f"id:{node_id}").content_state != "arrived"
    else:
        with pytest.raises(OrgFsError) as pending:
            member.fs.read_text(space_id, f"id:{node_id}")
        assert pending.value.code == ORGFS_CONTENT_PENDING
    return node_id


@pytest.mark.parametrize("blob", [False, True], ids=["text", "blob"])
def test_await_content_provisions_from_an_online_holder(peers: Peers, blob: bool) -> None:
    owner, member, space_id = _pair(peers)
    node_id = _write_unseen(peers, owner, member, space_id, blob=blob)
    peers.reconnect(member)
    assert member.runtime is not None

    assert member.runtime.await_content(space_id, node_id, deadline_monotonic=time.monotonic() + 10.0)
    if blob:
        assert member.runtime.blobs.contains(hashlib.sha256(BLOB).hexdigest())
        assert member.fs.read_bytes(space_id, f"id:{node_id}") == BLOB
    else:
        assert member.fs.read_text(space_id, f"id:{node_id}")[0] == "content from the owner"


@pytest.mark.parametrize("blob", [False, True], ids=["text", "blob"])
def test_await_content_gives_up_at_the_deadline_without_a_holder(peers: Peers, blob: bool) -> None:
    owner, member, space_id = _pair(peers)
    node_id = _write_unseen(peers, owner, member, space_id, blob=blob)
    assert member.runtime is not None

    started = time.monotonic()
    assert not member.runtime.await_content(space_id, node_id, deadline_monotonic=started + 0.5)
    assert time.monotonic() - started < 5.0
    assert not member.runtime.await_content(space_id, node_id, deadline_monotonic=time.monotonic() - 1)


def test_await_content_on_arrived_content_and_on_a_directory_returns_at_once(peers: Peers) -> None:
    owner, _member, space_id = _pair(peers)
    assert owner.runtime is not None
    text = owner.fs.write_text(space_id, "here.md", "here").node_id
    folder = owner.fs.mkdir(space_id, "folder").node_id
    blob = owner.fs.write_bytes(space_id, "here.bin", BLOB).node_id
    deadline = time.monotonic() + 5.0
    for node_id in (text, folder, blob):
        assert owner.runtime.await_content(space_id, node_id, deadline_monotonic=deadline)


def test_fetch_blob_returns_and_stores_the_bytes_of_an_online_holder(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    _write_unseen(peers, owner, member, space_id, blob=True)
    peers.reconnect(member)
    assert member.runtime is not None
    digest = hashlib.sha256(BLOB).hexdigest()

    assert member.runtime.fetch_blob(space_id, digest) == BLOB
    assert member.runtime.blobs.contains(digest)


def test_fetch_blob_without_an_online_holder_or_of_an_unknown_digest_is_blob_unavailable(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    _write_unseen(peers, owner, member, space_id, blob=True)
    assert member.runtime is not None
    with pytest.raises(OrgFsError) as offline:
        member.runtime.fetch_blob(space_id, hashlib.sha256(BLOB).hexdigest())
    assert offline.value.code == "blob-unavailable"
    assert "lastKnownHolders" in offline.value.details
    assert "orgfs.content.blob-fetch-failed" in member.events.names()

    peers.reconnect(member)
    with pytest.raises(OrgFsError) as unknown:
        member.runtime.fetch_blob(space_id, hashlib.sha256(b"never written").hexdigest())
    assert unknown.value.code == "blob-unavailable"


def test_refresh_space_pulls_from_online_holders_within_its_bound(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    _write_unseen(peers, owner, member, space_id, blob=False)
    peers.reconnect(member)
    assert member.runtime is not None
    eventually(lambda: _assert(owner.node_id in member.runtime.known_holders(space_id)))

    assert member.runtime.refresh_space(space_id, timeout=10.0) is True
    assert member.fs.read_text(space_id, "note.md")[0] == "content from the owner"
    assert member.runtime.refresh_space(space_id, timeout=0.0) is False


def test_refresh_space_with_no_online_holder_is_true_and_unbound_is_false(peers: Peers, tmp_path) -> None:
    owner, member, space_id = _pair(peers)
    peers.disconnect(member)
    assert member.runtime is not None
    assert member.runtime.refresh_space(space_id, timeout=1.0) is True

    from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime

    unbound = StateOrgFsRuntime(tmp_path / "unbound", node_id="node-unbound", author=OWNER)
    try:
        assert unbound.refresh_space(space_id, timeout=1.0) is False
    finally:
        unbound.close()


def test_known_holders_lists_online_holders_first_and_never_this_node(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    third = peers.add("node-third", "user:third@example")
    owner.fs.invite(space_id, third.author, "rw")
    third.fs.join(space_id)
    assert member.runtime is not None
    eventually(lambda: _assert(
        set(member.runtime.known_holders(space_id)) == {owner.node_id, third.node_id}
    ))

    peers.disconnect(owner)
    eventually(lambda: _assert(member.runtime.known_holders(space_id)[0] == third.node_id))
    assert member.node_id not in member.runtime.known_holders(space_id)


def _assert(condition: bool) -> None:
    assert condition


def test_stat_of_a_remote_birth_without_content_is_pending_until_the_content_arrives(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    node_id = _write_unseen(peers, owner, member, space_id, blob=False, check=False)

    assert member.fs.stat(space_id, f"id:{node_id}").content_state == "pending"
    assert member.fs.status(space_id).content_pending >= 1
    peers.reconnect(member)
    assert member.runtime is not None
    assert member.runtime.await_content(space_id, node_id, deadline_monotonic=time.monotonic() + 10.0)
    assert member.fs.stat(space_id, f"id:{node_id}").content_state == "arrived"


def test_after_a_restart_an_unverified_remote_birth_reports_arrived_until_verified(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    node_id = _write_unseen(peers, owner, member, space_id, blob=False, check=False)
    assert member.fs.stat(space_id, f"id:{node_id}").content_state == "pending"

    member.reopen()
    assert member.fs.stat(space_id, f"id:{node_id}").content_state == "arrived"
    member.fs.sync_host(space_id).pending_content(256)
    assert member.fs.stat(space_id, f"id:{node_id}").content_state == "pending"
