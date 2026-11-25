# Decisions (phase D, package F3, 2026-11-25):
# - The registry's ``NodeIndex`` is kept incrementally on local writes and
#   dropped on every other change.  The invariant checked after every step is
#   that whatever the index answers equals a fresh build over the same
#   ``space.nodes`` and equals the shared full scans (``FacadeProjection``'s
#   ``_resolve_ids`` and ``_name_conflict``) for every live path and node.
# - The index is forced to exist before each step, so a step that keeps it
#   (local writes) is checked on the incremental index, not on a rebuild.
# - Paths covered as the integrator asked: remote tree updates, a failed
#   commit (reload from the store) and a broken store (reopen), export-and-
#   swap (tree compaction), the temporary space of a join, and close/reopen.
"""The incremental node index always equals a rebuild of it."""

from __future__ import annotations

import random

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.document.coordinator.registry import NodeIndex
from hyprial.daemon.impl.orgfs.document.projection import FacadeProjection
from hyprial.daemon.impl.orgfs.state import COMPACTION_RECORDS

from ..support import OWNER, Cluster, Node, clone_node


def _force(node: Node, space_id: str) -> None:
    fs = node.fs
    with fs._space_lock(space_id):
        fs._doc_node(fs._spaces[space_id], "doc-force-index")


def index_of(fs, space):
    return fs._bound_index(space, fs._registry(space.info.space_id))


def assert_consistent(node: Node, space_id: str) -> None:
    fs = node.fs
    with fs._space_lock(space_id):
        space = fs._spaces[space_id]
        registry = fs._registry(space_id)
        index = registry.doc_index
        if index is not None and index.nodes is space.nodes:
            assert index.view() == NodeIndex(space.nodes).view()
        for item in list(space.nodes.values()):
            assert fs._name_conflict(space, item) == FacadeProjection._name_conflict(fs, space, item)
            if item.doc_id:
                expected = NodeIndex(space.nodes).doc_node(item.doc_id)
                assert fs._doc_node(space, item.doc_id) is expected
                assert sorted(other.node_id for other in index_of(fs, space).doc_nodes(item.doc_id)) == sorted(
                    other.node_id for other in space.nodes.values() if other.doc_id == item.doc_id
                )
            if item.node_id == "root" or item.deleted:
                continue
            path = fs._path(space, item.node_id)
            try:
                legacy = FacadeProjection._resolve_ids(fs, space, path)
            except OrgFsError as exc:
                with pytest.raises(OrgFsError) as raised:
                    fs._resolve_ids(space, path)
                assert raised.value.code == exc.code
            else:
                assert fs._resolve_ids(space, path) == legacy
                assert fs._resolve_ids(space, path, include_deleted=True) == (
                    FacadeProjection._resolve_ids(fs, space, path, include_deleted=True)
                )


def _live(node: Node, space_id: str, kind: str | None = None) -> list[str]:
    space = node.fs._spaces[space_id]
    return sorted(
        item.node_id
        for item in space.nodes.values()
        if item.node_id != "root" and not item.deleted and (kind is None or item.kind == kind)
    )


def _step(rng: random.Random, node: Node, space_id: str, counter: list[int]) -> None:
    fs = node.fs
    counter[0] += 1
    serial = counter[0]
    dirs = [""] + [f"id:{node_id}/" for node_id in _live(node, space_id, "dir")]
    docs = _live(node, space_id, "doc")
    choice = rng.choice(
        ["create", "create", "overwrite", "mkdir", "move", "rename-same", "remove", "restore", "purge", "fail", "break"]
    )
    try:
        if choice == "create":
            name = rng.choice(["a.md", "b.md", f"f{serial}.md"])
            fs.write_text(space_id, f"{rng.choice(dirs)}{name}", f"text {serial}")
        elif choice == "overwrite" and docs:
            fs.write_text(space_id, f"id:{rng.choice(docs)}", f"over {serial}")
        elif choice == "mkdir":
            fs.mkdir(space_id, f"{rng.choice(dirs)}dir{serial}")
        elif choice == "move" and docs:
            fs.move(space_id, f"id:{rng.choice(docs)}", f"{rng.choice(dirs)}m{serial}.md")
        elif choice == "rename-same" and docs:
            fs.move(space_id, f"id:{rng.choice(docs)}", f"{rng.choice(dirs)}a.md")
        elif choice == "remove" and docs:
            fs.remove(space_id, f"id:{rng.choice(docs)}")
        elif choice == "restore":
            space = fs._spaces[space_id]
            gone = sorted(item.node_id for item in space.nodes.values() if item.deleted)
            if gone:
                fs.restore(space_id, f"id:{rng.choice(gone)}")
        elif choice == "purge" and docs:
            fs.remove(space_id, f"id:{rng.choice(docs)}", permanent=True)
        elif choice == "fail":
            fs._registry(space_id).store.fail_next("commit")
            fs.write_text(space_id, f"{rng.choice(dirs)}failed{serial}.md", "never")
        elif choice == "break":
            fs._registry(space_id).store.fail_next("commit.after")
            fs.write_text(space_id, f"{rng.choice(dirs)}broken{serial}.md", "recovered")
    except OrgFsError:
        pass


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_local_writes_failures_and_reopen_keep_the_index_equal_to_a_rebuild(seed: int) -> None:
    rng = random.Random(seed)
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("fuzz").space_id
        counter = [0]
        for _ in range(150):
            _force(node, space_id)
            _step(rng, node, space_id, counter)
            assert_consistent(node, space_id)
    finally:
        node.close()


def test_remote_tree_updates_and_concurrent_names_keep_the_index_equal_to_a_rebuild() -> None:
    rng = random.Random(7)
    origin = Node(OWNER, "node-a")
    space_id = origin.fs.create_space("team").space_id
    origin.fs.invite(space_id, "user:b", "rw")
    origin.fs.write_text(space_id, "a.md", "seed")
    other = clone_node(origin, space_id, "user:b", "node-b")
    cluster = Cluster(space_id, [origin, other])
    try:
        counters = {"node-a": [0], "node-b": [1000]}
        for round_index in range(40):
            for node in (origin, other):
                _force(node, space_id)
                _step(rng, node, space_id, counters[node.node_id])
                assert_consistent(node, space_id)
            for node in (origin, other):
                _force(node, space_id)
            if round_index % 3 == 0:
                cluster.settle()
            else:
                cluster.deliver()
            for node in (origin, other):
                assert_consistent(node, space_id)
    finally:
        origin.close()
        other.close()


def test_compaction_swapping_the_tree_keeps_the_index_equal_to_a_rebuild() -> None:
    node = Node(OWNER, "node-a")
    try:
        space_id = node.fs.create_space("busy").space_id
        registry = node.fs._registry(space_id)
        tree_doc = registry.tree.doc
        for index in range(COMPACTION_RECORDS + 20):
            _force(node, space_id)
            node.fs.write_text(space_id, f"d{index % 7}.md" if index % 2 else f"n{index}.md", str(index))
            if index % 50 == 0:
                assert_consistent(node, space_id)
        assert registry.tree.doc is not tree_doc
        assert node.backing(space_id).compactions >= 1
        assert_consistent(node, space_id)
    finally:
        node.close()


def test_close_and_reopen_rebuild_the_index_for_the_new_space() -> None:
    node = Node(OWNER, "node-a")
    reopened = Node(OWNER, "node-b")
    try:
        space_id = node.fs.create_space("team").space_id
        node.fs.mkdir(space_id, "folder")
        node.fs.write_text(space_id, "folder/a.md", "one")
        _force(node, space_id)
        assert node.fs.detach_space(space_id)
        assert space_id not in node.fs._registries
        reopened.stores.backings[space_id] = node.backing(space_id)
        reopened.fs.load_space(space_id)
        _force(reopened, space_id)
        assert_consistent(reopened, space_id)
        assert reopened.fs.resolve(space_id, "folder/a.md")[0].name == "a.md"
        reopened.fs.write_text(space_id, "folder/b.md", "two")
        reopened.fs.move(space_id, "folder/a.md", "folder/b.md")
        assert_consistent(reopened, space_id)
    finally:
        node.close()
        reopened.close()


def test_the_temporary_space_of_a_join_keeps_the_index_equal_to_a_rebuild() -> None:
    origin = Node(OWNER, "node-a")
    space_id = origin.fs.create_space("team").space_id
    origin.fs.invite(space_id, "user:b", "rw")
    origin.fs.mkdir(space_id, "folder")
    for index in range(20):
        origin.fs.write_text(space_id, f"folder/f{index}.md", str(index))
    joiner = Node("user:b", "node-b")
    try:
        joiner.fs.begin_join(space_id)
        _force(joiner, space_id)
        assert_consistent(joiner, space_id)
        cluster = Cluster(space_id, [origin, joiner])
        cluster.settle()
        assert_consistent(joiner, space_id)
        joiner.fs.complete_join(space_id)
        _force(joiner, space_id)
        joiner.fs.write_text(space_id, "folder/joined.md", "hello")
        assert_consistent(joiner, space_id)
        assert [info.name for info in joiner.fs.resolve(space_id, "folder/f3.md")] == ["f3.md"]
    finally:
        origin.close()
        joiner.close()
