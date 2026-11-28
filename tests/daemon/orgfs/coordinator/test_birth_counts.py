# Decisions:
# - Design §9.1 (D1 report §6-4): accepting a remote tree update whose new
#   document rows arrive before their content must not load any content
#   document; the count is taken at the store seam (``StateStore.load`` doc
#   ids), C6 style, so it holds for any number of rows.
# - The birth stays one ``created`` event that already carries the content:
#   no event while only the tree has arrived, one per document after its
#   content is applied.
"""Remote births never load content documents to decide that they wait."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.orgfs.membership import Accepted

from .support import OWNER, Cluster, Node, clone_node, eventually

ROWS = 40


def _record_loads(node: Node, space_id: str) -> list[str]:
    registry = node.fs._registry(space_id)  # noqa: SLF001 - the store seam under count
    store = registry.store
    original = store.load
    loaded: list[str] = []

    def load(doc_id: str) -> Any:
        loaded.append(doc_id)
        return original(doc_id)

    store.load = load
    return loaded


def _assert_created(events: list[Any], *, kind: str, count: int) -> None:
    created = [event for event in events if event.kind == "created" and event.node.kind == kind]
    assert len(created) == count


def test_a_tree_update_with_new_document_rows_loads_no_content_document() -> None:
    owner = Node(OWNER, "node-a")
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, "user:b", "rw")
    peer = clone_node(owner, space_id, "user:b", "node-b")
    cluster = Cluster(space_id, [owner, peer])
    try:
        owner.fs.mkdir(space_id, "notes")
        for index in range(ROWS):
            owner.fs.write_text(space_id, f"notes/n{index}.md", f"body {index}")
        frames = cluster.take()
        tree = [frame for frame in frames if frame[1] == "tree"]
        content = [frame for frame in frames if frame[1] not in ("meta", "tree")]
        assert len(content) == ROWS
        events: list[Any] = []
        peer.fs.watch(space_id, "**", events.append)
        loaded = _record_loads(peer, space_id)
        host = peer.fs.sync_host(space_id)

        for _sender, doc_id, update, vector in tree:
            assert isinstance(host.apply_remote(doc_id, update, vector, "node-a"), Accepted)

        assert [doc_id for doc_id in loaded if doc_id not in ("meta", "tree")] == []
        listed = peer.fs.listdir(space_id, "notes")
        assert len(listed) == ROWS
        assert [doc_id for doc_id in loaded if doc_id not in ("meta", "tree")] == []
        eventually(lambda: _assert_created(events, kind="dir", count=1))
        _assert_created(events, kind="doc", count=0)

        for _sender, doc_id, update, vector in content:
            assert isinstance(host.apply_remote(doc_id, update, vector, "node-a"), Accepted)

        created = {doc_id for _sender, doc_id, _update, _vector in content}
        assert set(loaded) - {"meta", "tree"} <= created
        assert peer.fs.read_text(space_id, "notes/n7.md")[0] == "body 7"
        eventually(lambda: _assert_created(events, kind="doc", count=ROWS))
        files = [event.node.path for event in events if event.node.kind == "doc"]
        assert sorted(files) == sorted(f"notes/n{index}.md" for index in range(ROWS))
    finally:
        owner.close()
        peer.close()
