# Decisions:
# - Design §2.5 / §3.3 / §9 R1, R2, R14: the local write path of the state
#   host.  Store failures are injected with ``MemoryStateStore.fail_next``
#   (C1): ``commit`` fails before anything is applied, ``commit.after`` after
#   the batch became durable (the store is then broken until reopened).
# - "Only through the commit helper" is observed two ways: every write entry
#   calls ``membership.local_commit`` once per document it appends, and each
#   entry makes exactly one ``StateStore.commit``.
"""Local commits of StateOrgFs: helper, failures, ordering, versions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.document.coordinator import commit as commit_module
from hyprial.daemon.impl.orgfs.document.coordinator.registry import BaseTexts
from hyprial.daemon.impl.orgfs.membership import Accepted, decode_vector, vector_advanced
from hyprial.daemon.impl.orgfs.state import COMPACTION_RECORDS

from .support import EMPTY, OWNER, Node, RecordingSync, clone_node, eventually

ENTRIES = {
    "write-existing": (lambda fs, sid: fs.write_text(sid, "folder/a.md", "alpha beta gamma"), ("a", "tree")),
    "write-new": (lambda fs, sid: fs.write_text(sid, "new.md", "new"), ("tree", "+content")),
    "clear-text": (lambda fs, sid: fs.write_text(sid, "folder/a.md", ""), ("a", "tree")),
    "write-bytes": (lambda fs, sid: fs.write_bytes(sid, "folder/c.bin", b"after"), ("tree",)),
    "mkdir": (lambda fs, sid: fs.mkdir(sid, "new-dir"), ("tree",)),
    "move": (lambda fs, sid: fs.move(sid, "folder/a.md", "renamed.md"), ("tree",)),
    "remove": (lambda fs, sid: fs.remove(sid, "folder/a.md"), ("tree",)),
    "remove-permanent": (lambda fs, sid: fs.remove(sid, "folder/c.bin", permanent=True), ("tree",)),
    "restore": (lambda fs, sid: fs.restore(sid, "trashed.md"), ("tree",)),
    "update-meta": (lambda fs, sid: fs.update_space_meta(sid, {"aclSpaceId": "acl-1"}), ("meta",)),
    "invite": (lambda fs, sid: fs.invite(sid, "user:new", "ro"), ("meta",)),
    "remove-member": (lambda fs, sid: fs.remove_member(sid, "user:guest"), ("meta",)),
}


def _seed(node: Node) -> str:
    fs = node.fs
    space_id = fs.create_space("commits").space_id
    fs.mkdir(space_id, "folder")
    fs.write_text(space_id, "folder/a.md", "alpha beta")
    fs.write_bytes(space_id, "folder/c.bin", b"before")
    trashed = fs.write_text(space_id, "trashed.md", "in trash")
    fs.remove(space_id, f"id:{trashed.node_id}")
    fs.invite(space_id, "user:guest", "rw")
    return space_id


def _docs(node: Node, space_id: str, names: tuple[str, ...]) -> tuple[str, ...]:
    a = node.fs.stat(space_id, "folder/a.md").doc_id
    return tuple(a if name == "a" else name for name in names if not name.startswith("+"))


def _kinds(node: Node, space_id: str, names: tuple[str, ...]) -> list[str]:
    return sorted(
        name.removeprefix("+") if name.startswith("+") else ("content" if name == "a" else name)
        for name in names
    )


def _observable(node: Node, space_id: str) -> tuple[object, ...]:
    fs = node.fs
    nodes = fs.listdir(space_id, "", recursive=True)
    texts = tuple(
        (item.node_id, fs.read_text(space_id, f"id:{item.node_id}")[0]) for item in nodes if item.kind == "doc"
    )
    vectors = fs.sync_host(space_id).read_vectors(("meta", "tree"))
    return (
        nodes,
        texts,
        fs.members(space_id),
        fs.trash(space_id),
        tuple(sorted(fs.space_meta(space_id).items())),
        dict(vectors),
        dict(fs._registry(space_id).batches),
    )


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_every_write_entry_commits_once_through_the_commit_helper(monkeypatch, entry: str) -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _seed(node)
        write, touched = ENTRIES[entry]
        doc_ids = _docs(node, space_id, touched)
        host = node.fs.sync_host(space_id)
        before = host.read_vectors(doc_ids)
        stamped: list[str] = []
        original = commit_module.local_commit

        def counting(doc, kind, author, **kwargs):
            stamped.append(kind)
            return original(doc, kind, author, **kwargs)

        monkeypatch.setattr(commit_module, "local_commit", counting)
        commits = node.backing(space_id).commits

        write(node.fs, space_id)

        assert node.backing(space_id).commits == commits + 1
        assert sorted(stamped) == _kinds(node, space_id, touched)
        after = host.read_vectors(doc_ids)
        for doc_id in doc_ids:
            assert vector_advanced(before[doc_id], after[doc_id]), doc_id
    finally:
        node.close()


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_a_failed_commit_leaves_memory_equal_to_before(entry: str) -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _seed(node)
        seen = []
        node.fs.watch(space_id, "**", seen.append)
        sync = RecordingSync()
        node.fs.attach_sync(space_id, sync)
        before = _observable(node, space_id)
        write, _touched = ENTRIES[entry]
        node.fs._registry(space_id).store.fail_next("commit")

        with pytest.raises(OrgFsError):
            write(node.fs, space_id)

        assert _observable(node, space_id) == before
        assert node.fs.drain_broadcasts((space_id,))
        assert sync.published == [] and seen == []
        write(node.fs, space_id)
        assert _observable(node, space_id) != before
    finally:
        node.close()


def test_a_broken_store_is_reopened_and_the_space_rebuilt_from_disk() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _seed(node)
        registry = node.fs._registry(space_id)
        old_store = registry.store
        registry.store.fail_next("commit.after")

        with pytest.raises(OrgFsError):
            node.fs.write_text(space_id, "folder/a.md", "applied before the failure")

        assert registry.store is not old_store
        assert registry.store.recovered_batch is True
        assert registry.broken is False
        assert node.fs.read_text(space_id, "folder/a.md")[0] == "applied before the failure"
        node.fs.write_text(space_id, "after.md", "still writable")
        assert node.fs.read_text(space_id, "after.md")[0] == "still writable"
        assert node.fs.status(space_id).content_pending == 1
        assert node.fs.sync_host(space_id).pending_content(128) == ()
        assert node.fs.status(space_id).content_pending == 0
    finally:
        node.close()


def test_a_store_that_cannot_be_reopened_makes_the_space_unavailable(monkeypatch) -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = _seed(node)
        node.fs._registry(space_id).store.fail_next("commit.after")

        def refuse(space_id: str, create: bool):
            raise OSError("disk gone")

        monkeypatch.setattr(node.fs, "_store_factory", refuse)
        with pytest.raises(OrgFsError):
            node.fs.mkdir(space_id, "x")
        with pytest.raises(OrgFsError) as error:
            node.fs.mkdir(space_id, "y")
        assert error.value.code == "unavailable"
    finally:
        node.close()


def test_new_methods_run_serially_on_the_space_command_loop(monkeypatch) -> None:
    owner = Node(OWNER, "node-a")
    peer = clone_node(owner, _seed(owner), "user:guest", "node-b")
    space_id = next(iter(owner.stores.backings))
    try:
        peer.fs.mkdir(space_id, "from-peer")
        update, vector = peer.fs.sync_host(space_id).read_update("tree", EMPTY)
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []
        threads: list[str] = []
        accept = owner.fs._accept_remote

        def slow_accept(*args, **kwargs):
            threads.append(threading.current_thread().name)
            order.append("apply-start")
            entered.set()
            assert release.wait(5)
            order.append("apply-end")
            return accept(*args, **kwargs)

        monkeypatch.setattr(owner.fs, "_accept_remote", slow_accept)
        remove = owner.fs._remove_permanently

        def recorded_remove(*args, **kwargs):
            order.append("remove")
            threads.append(threading.current_thread().name)
            return remove(*args, **kwargs)

        monkeypatch.setattr(owner.fs, "_remove_permanently", recorded_remove)
        with ThreadPoolExecutor(max_workers=2) as pool:
            applied = pool.submit(owner.fs.sync_host(space_id).apply_remote, "tree", update, vector, "node-b")
            assert entered.wait(5)
            removed = pool.submit(owner.fs.remove, space_id, "trashed.md", permanent=True)
            assert not removed.done()
            release.set()
            assert applied.result(5) == Accepted()
            removed.result(5)

        assert order == ["apply-start", "apply-end", "remove"]
        assert threading.current_thread().name not in threads
        assert len(set(threads)) == 1
    finally:
        owner.close()
        peer.close()


def test_publications_follow_durability_tree_before_content() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("team").space_id
        sync = RecordingSync()
        node.fs.attach_sync(space_id, sync)
        generation = node.fs.sync_host(space_id).local_generation()

        created = node.fs.write_text(space_id, "new.md", "hello")
        node.fs.update_space_meta(space_id, {"aclSpaceId": "acl-1"})

        assert node.fs.drain_broadcasts((space_id,))
        assert [doc_id for doc_id, _update, _vector in sync.published] == ["tree", created.doc_id, "meta"]
        vectors = node.fs.sync_host(space_id).read_vectors(("tree", created.doc_id, "meta"))
        assert {doc_id: vector for doc_id, _update, vector in sync.published} == dict(vectors)
        assert node.fs.sync_host(space_id).local_generation() == generation + 2
    finally:
        node.close()


def test_compaction_runs_after_enough_commits_and_swaps_the_instance() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("team").space_id
        info = node.fs.write_text(space_id, "busy.md", "0")
        registry = node.fs._registry(space_id)
        tree_doc = registry.tree.doc
        for index in range(COMPACTION_RECORDS):
            node.fs.write_text(space_id, "busy.md", str(index + 1))
        backing = node.backing(space_id)

        assert backing.compactions >= 2
        assert registry.tree.doc is not tree_doc
        assert len(backing.documents[info.doc_id].delta) < COMPACTION_RECORDS
        assert node.fs.read_text(space_id, "busy.md")[0] == str(COMPACTION_RECORDS)
        reopened = Node(OWNER, "node-b")
        reopened.stores.backings[space_id] = backing
        reopened.fs.load_space(space_id)
        try:
            assert reopened.fs.read_text(space_id, "busy.md")[0] == str(COMPACTION_RECORDS)
        finally:
            reopened.close()
    finally:
        node.close()


def test_base_text_cache_miss_is_stale_write_unless_it_is_the_current_version() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("team").space_id
        first = node.fs.write_text(space_id, "merge.md", "alpha\nbeta\n")
        node.fs._registry(space_id).base_texts = BaseTexts(entries=1)
        second = node.fs.write_text(space_id, "merge.md", "alpha\nbeta\ngamma\n")
        node.fs.write_text(space_id, "other.md", "evicts the cache")

        with pytest.raises(OrgFsError) as error:
            node.fs.write_text(space_id, "merge.md", "ALPHA\nbeta\n", base_version=first.version)
        assert error.value.code == "stale-write"
        assert error.value.details["baseVersion"] == first.version

        merged = node.fs.write_text(space_id, "merge.md", "replaced\n", base_version=second.version)
        assert node.fs.read_text(space_id, "merge.md") == ("replaced\n", merged.version)
    finally:
        node.close()


def test_versions_are_stable_across_reopen_eviction_and_nodes() -> None:
    node = Node(OWNER, "node-a", content_capacity=1)
    try:
        space_id = node.fs.create_space("team").space_id
        first = node.fs.write_text(space_id, "a.md", "a")
        node.fs.write_text(space_id, "b.md", "b")
        assert node.fs._registry(space_id).loaded(first.doc_id) is None
        assert node.fs.stat(space_id, "a.md").version == first.version
        clone = clone_node(node, space_id, OWNER, "node-b")
        try:
            assert clone.fs.stat(space_id, "a.md").version == first.version
            assert clone.fs.read_text(space_id, "a.md") == ("a", first.version)
            clone.fs.write_text(space_id, "a.md", "a2", expect_version=first.version)
        finally:
            clone.close()
    finally:
        node.close()


def test_changes_since_accepts_a_cursor_or_a_version() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("team").space_id
        first = node.fs.write_text(space_id, "a.md", "one")
        cursor = node.fs.change_cursor(space_id)
        node.fs.write_text(space_id, "b.md", "two")

        assert [event.node.path for event in node.fs.changes_since(space_id, "*", cursor)] == ["b.md"]
        assert [event.node.path for event in node.fs.changes_since(space_id, "*", first.version)] == ["b.md"]
        assert len(node.fs.changes_since(space_id, "*", "evt:other-process:1")) == 2
        assert len(node.fs.changes_since(space_id, "*", "unknown")) == 2
    finally:
        node.close()


def test_the_registry_evicts_least_recent_unpinned_documents_and_reloads_them() -> None:
    node = Node(OWNER, "node-a", content_capacity=2)
    try:
        space_id = node.fs.create_space("team").space_id
        infos = [node.fs.write_text(space_id, f"{index}.md", str(index)) for index in range(4)]
        registry = node.fs._registry(space_id)

        assert list(registry.contents) == [infos[2].doc_id, infos[3].doc_id]
        loads = node.backing(space_id).loads
        assert node.fs.read_text(space_id, "0.md")[0] == "0"
        assert node.backing(space_id).loads == loads + 1
        eventually(lambda: _assert_size(registry, 2))
        assert [item.size for item in node.fs.listdir(space_id, "")] == [1, 1, 1, 1]
        assert node.backing(space_id).loads == loads + 1
    finally:
        node.close()


def _assert_size(registry, size: int) -> None:
    assert len(registry.contents) <= size


def test_a_command_pins_every_document_it_touches() -> None:
    node = Node(OWNER, "node-a", content_capacity=1)
    try:
        space_id = node.fs.create_space("team").space_id
        infos = [node.fs.write_text(space_id, f"{index}.md", str(index)) for index in range(3)]
        registry = node.fs._registry(space_id)
        registry.begin_command()
        documents = [registry.content(info.doc_id) for info in infos]
        assert [registry.loaded(info.doc_id) for info in infos] == documents
        registry.end_command()
        assert list(registry.contents) == [infos[2].doc_id]
    finally:
        node.close()


def test_every_space_has_its_own_random_client_id_for_all_documents() -> None:
    node = Node(OWNER, "node-a")
    try:
        left = node.fs.create_space("left").space_id
        right = node.fs.create_space("right").space_id
        info = node.fs.write_text(left, "a.md", "a")
        ids = {
            space_id: node.fs._registry(space_id).client_id for space_id in (left, right)
        }
        assert ids[left] != ids[right]
        assert 0 < ids[left] < 2**53
        for doc_id in ("meta", "tree", info.doc_id):
            vector = node.fs.sync_host(left).read_vectors((doc_id,))[doc_id]
            assert set(decode_vector(vector)) == {ids[left]}
        reopened = clone_node(node, left, OWNER, "node-b")
        try:
            assert reopened.fs._registry(left).client_id != ids[left]
        finally:
            reopened.close()
    finally:
        node.close()
