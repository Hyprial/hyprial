# Decisions:
# - Design §1.1 / §4.6 / §9 R4: ``status`` and ``pending_content`` read the
#   coordinator's verification state; observations come from an attached fake
#   sync (``RecordingSync``).
# - F4 ruling (design question 2, option 1): ``synced`` is ``False`` while any
#   online observation is ahead of the local meta or tree vector, even when
#   another observation covers the local vectors.
# - Design §3.2 / §3.3 / §9 R7 / §9 R8 / §6: member rows, ``space.modifiedBy``,
#   the root row, removal batches and permanent removal rights, all through
#   the public facade.
"""Status, pending content, members, root row and trash batches."""

from __future__ import annotations

import threading

import pytest

from hyprial.kernel import ORGFS_CONTENT_PENDING
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import read_member_rows, read_space
from hyprial.daemon.impl.orgfs.state import MemoryBacking
from hyprial.daemon.impl.orgfs.sync import PeerObservation

from .support import OWNER, Cluster, Node, RecordingSync, clone_node, copy_backing


def _space(node: Node) -> str:
    return node.fs.create_space("team").space_id


def test_synced_needs_an_online_holder_covering_local_vectors_and_no_pending() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _space(node)
        sync = RecordingSync()
        node.fs.attach_sync(space_id, sync)
        assert node.fs.status(space_id).synced is None

        vectors = node.fs.sync_host(space_id).read_vectors(("meta", "tree"))
        sync.observed = (PeerObservation("node-b", vectors["meta"], vectors["tree"], 0),)
        status = node.fs.status(space_id)
        assert (status.synced, status.holders_online, status.content_pending) == (True, ("node-b",), 0)

        node.fs.mkdir(space_id, "later")
        assert node.fs.status(space_id).synced is False
        vectors = node.fs.sync_host(space_id).read_vectors(("meta", "tree"))
        sync.observed = (PeerObservation("node-b", vectors["meta"], vectors["tree"], 1),)
        assert node.fs.status(space_id).synced is True
    finally:
        node.close()


def test_synced_is_false_while_any_online_observation_is_ahead_of_the_local_catalog() -> None:
    origin = Node(OWNER, "node-a")
    try:
        space_id = _space(origin)
        node = clone_node(origin, space_id, OWNER, "node-b")
        try:
            sync = RecordingSync()
            node.fs.attach_sync(space_id, sync)
            local = node.fs.sync_host(space_id).read_vectors(("meta", "tree"))
            origin.fs.mkdir(space_id, "only-on-the-origin")
            ahead = origin.fs.sync_host(space_id).read_vectors(("meta", "tree"))

            sync.observed = (PeerObservation("node-a", local["meta"], ahead["tree"], 0),)
            assert node.fs.status(space_id).synced is False
            sync.observed = (
                PeerObservation("node-a", local["meta"], ahead["tree"], 0),
                PeerObservation("node-c", local["meta"], local["tree"], 0),
            )
            assert node.fs.status(space_id).synced is False
            sync.observed = (PeerObservation("node-c", local["meta"], local["tree"], 0),)
            assert node.fs.status(space_id).synced is True
        finally:
            node.close()
    finally:
        origin.close()


def test_a_loaded_space_counts_unverified_documents_until_pending_content_checks_them() -> None:
    origin = Node(OWNER, "node-a")
    try:
        space_id = _space(origin)
        infos = [origin.fs.write_text(space_id, f"{index}.md", str(index)) for index in range(3)]
        backing = copy_backing(origin.backing(space_id))
        del backing.documents[infos[1].doc_id]
        node = Node(OWNER, "node-b")
        node.stores.backings[space_id] = backing
        node.fs.load_space(space_id)
        try:
            assert backing.loads == 2
            assert node.fs.status(space_id).content_pending == 3
            assert [item.content_state for item in node.fs.listdir(space_id, "")] == ["arrived"] * 3
            host = node.fs.sync_host(space_id)

            host.pending_content(1)
            assert len(node.fs._registry(space_id).unverified) == 2
            assert host.pending_content(128) == (infos[1].doc_id,)
            assert node.fs.status(space_id).content_pending == 1
            assert node.fs.stat(space_id, "1.md").content_state == "pending"
            with pytest.raises(OrgFsError) as error:
                node.fs.read_text(space_id, "1.md")
            assert error.value.code == ORGFS_CONTENT_PENDING
        finally:
            node.close()
    finally:
        origin.close()


def test_read_vectors_does_not_wait_for_the_space_lock() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _space(node)
        host = node.fs.sync_host(space_id)
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with node.fs._space_lock(space_id):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        try:
            assert held.wait(5)
            done: list[object] = []
            reader = threading.Thread(target=lambda: done.append(host.read_vectors(("meta", "tree"))))
            reader.start()
            reader.join(2)
            assert done and set(done[0]) == {"meta", "tree"}
        finally:
            release.set()
            worker.join(5)
    finally:
        node.close()


def test_members_are_sorted_and_removal_keeps_the_row_with_removed_at() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _space(node)
        node.fs.invite(space_id, "user:zed", "rw")
        first = node.fs.invite(space_id, "user:amy", "ro")
        assert [member.user for member in node.fs.members(space_id)] == ["user:amy", "user:owner", "user:zed"]

        node.fs.remove_member(space_id, "user:amy")
        space = node.fs._space(space_id)
        rows = read_member_rows(space.meta)
        assert rows["user:amy"].removed_at is not None
        assert "user:amy" not in {member.user for member in node.fs.members(space_id)}

        back = node.fs.invite(space_id, "user:amy", "rw")
        assert back.added_at > first.added_at
        with pytest.raises(OrgFsError) as error:
            node.fs.remove_member(space_id, OWNER)
        assert error.value.code == "invalid-argument"
        node.fs.update_space_meta(space_id, {"aclSpaceId": "acl-1"})
        assert read_space(space.meta)["modifiedBy"] == OWNER
    finally:
        node.close()


def test_member_commits_do_not_rewrite_the_space_root() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _space(node)
        before = dict(node.fs.space_meta(space_id))
        node.fs.invite(space_id, "user:zed", "rw")
        assert dict(node.fs.space_meta(space_id)) == before
    finally:
        node.close()


def test_recursive_restore_follows_the_removal_batch_only() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _space(node)
        folder = node.fs.mkdir(space_id, "folder")
        early = node.fs.write_text(space_id, "folder/early.md", "removed first")
        late = node.fs.write_text(space_id, "folder/late.md", "removed with the folder")
        node.fs.remove(space_id, f"id:{early.node_id}")
        node.fs.remove(space_id, f"id:{folder.node_id}")

        node.fs.restore(space_id, f"id:{folder.node_id}", recursive=True)

        assert {item.node_id for item in node.fs.listdir(space_id, "folder")} == {late.node_id}
        assert [entry.node_id for entry in node.fs.trash(space_id)] == [early.node_id]
        node.fs.restore(space_id, f"id:{early.node_id}", "ignored-legacy-version")
        assert node.fs.trash(space_id) == ()
    finally:
        node.close()


def test_permanent_removal_needs_the_owner_or_the_row_author() -> None:
    owner = Node(OWNER, "node-owner")
    space_id = _space(owner)
    owner.fs.invite(space_id, "user:alice", "rw")
    owner.fs.invite(space_id, "user:bob", "rw")
    owner_file = owner.fs.write_text(space_id, "owner.md", "owner")
    alice = clone_node(owner, space_id, "user:alice", "node-alice")
    bob = clone_node(owner, space_id, "user:bob", "node-bob")
    cluster = Cluster(space_id, [owner, alice, bob])
    try:
        mine = alice.fs.write_text(space_id, "alice.md", "alice")
        cluster.deliver()

        with pytest.raises(OrgFsError) as error:
            bob.fs.remove(space_id, f"id:{mine.node_id}", permanent=True)
        assert error.value.code == "not-owner"
        with pytest.raises(OrgFsError) as error:
            alice.fs.remove(space_id, f"id:{owner_file.node_id}", permanent=True)
        assert error.value.code == "not-owner"

        alice.fs.remove(space_id, f"id:{mine.node_id}", permanent=True)
        owner.fs.remove(space_id, f"id:{owner_file.node_id}", permanent=True)
        cluster.deliver()
        for node in (owner, alice, bob):
            assert node.fs.listdir(space_id, "") == ()
            assert mine.doc_id not in node.backing(space_id).documents
    finally:
        for node in (owner, alice, bob):
            node.close()


def test_a_tree_without_a_root_row_refuses_local_writes() -> None:
    origin = Node(OWNER, "node-a")
    try:
        space_id = _space(origin)
        backing = MemoryBacking(space_id)
        backing.documents["meta"] = copy_backing(origin.backing(space_id)).documents["meta"]
        node = Node(OWNER, "node-b")
        node.stores.backings[space_id] = backing
        node.fs.load_space(space_id)
        try:
            assert node.fs.stat(space_id, "").kind == "dir"
            with pytest.raises(OrgFsError) as error:
                node.fs.mkdir(space_id, "early")
            assert error.value.code == ORGFS_CONTENT_PENDING
            assert node.fs.sync_host(space_id).read_vectors(("tree",))["tree"] == b"\x00"
        finally:
            node.close()
    finally:
        origin.close()


def test_read_only_and_removed_members_are_refused_locally() -> None:
    owner = Node(OWNER, "node-owner")
    space_id = _space(owner)
    owner.fs.invite(space_id, "user:reader", "ro")
    reader = clone_node(owner, space_id, "user:reader", "node-reader")
    try:
        with pytest.raises(OrgFsError) as error:
            reader.fs.mkdir(space_id, "nope")
        assert error.value.code == "not-a-member"
        with pytest.raises(OrgFsError) as error:
            reader.fs.invite(space_id, "user:x")
        assert error.value.code == "not-owner"
    finally:
        owner.close()
        reader.close()
