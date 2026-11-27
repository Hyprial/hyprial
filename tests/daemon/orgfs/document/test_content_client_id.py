"""Creating ordinary documents must not grow the writer row or the meta journal.

2026-11-24: an owner-home space held one writer row whose ``peerIds`` list had
25,456 entries for 25,453 documents.  Every new content document was authored
under a client id derived from its node id, so every creation registered one
more id, and each registration rewrote the whole list into a meta commit: meta
commits were 90.2% of that journal's envelope bytes (3.63 GiB).

Ordinary documents are now authored under one client id per store
incarnation.  Protected directory documents keep the node-derived id.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _decode_state_vector,
    _doc_roots,
)
from tests.support.scaling import assert_flat, at_scales


_AUTHOR = "user:plain"


def _node_client_id(node_id: str) -> int:
    return int.from_bytes(hashlib.sha256(node_id.encode()).digest()[:8], "big") & (
        (1 << 53) - 1
    )


def _write(path: Path, name: str, documents: int) -> tuple[LocalSpaceStore, LocalOrgFs, str]:
    space_id = str(uuid.uuid4())
    store = LocalSpaceStore(path, space_id, node_id="node-plain")
    facade = LocalOrgFs(stores={space_id: store}, author=_AUTHOR)
    facade.create_space(name, space_id=space_id)
    for index in range(documents):
        facade.write_text(space_id, f"file-{index}.md", f"body {index}")
    return store, facade, space_id


def _meta_commits(store: LocalSpaceStore) -> int:
    return int(
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
        ).fetchone()[0]
    )


def _longest_peer_list(store: LocalSpaceStore) -> int:
    writers = _doc_roots(store._docs["meta"]).get("writers") or {}
    return max(len(row.get("peerIds") or []) for row in writers.values())


def test_creating_documents_adds_no_meta_commit(tmp_path: Path) -> None:
    def measure(documents: int) -> tuple[int, int]:
        store, facade, _ = _write(tmp_path / str(documents), "plain-home", documents)
        try:
            return _meta_commits(store), _longest_peer_list(store)
        finally:
            assert facade.close_effects()
            store.close()

    assert_flat(*at_scales(measure, 6))


def test_new_documents_share_the_facade_client_id(tmp_path: Path) -> None:
    store, facade, space_id = _write(tmp_path, "plain-home", 3)
    try:
        shared = store.facade_client_id()
        writer = store.writer_client_id(_AUTHOR, None)
        assert shared != writer
        for index in range(3):
            node = facade.stat(space_id, f"file-{index}.md")
            clients = set(_decode_state_vector(store.frontier(node.doc_id)))
            assert shared in clients
            assert _node_client_id(node.node_id) not in clients
            assert facade.read_text(space_id, f"file-{index}.md")[0] == f"body {index}"
    finally:
        assert facade.close_effects()
        store.close()


def test_documents_stay_readable_and_editable_after_reopen(tmp_path: Path) -> None:
    store, facade, space_id = _write(tmp_path, "plain-home", 2)
    assert facade.close_effects()
    store.close()

    reopened = LocalSpaceStore(tmp_path, space_id, node_id="node-plain")
    again = LocalOrgFs(stores={space_id: reopened}, author=_AUTHOR)
    try:
        again.load_space(space_id)
        assert again.read_text(space_id, "file-0.md")[0] == "body 0"
        again.write_text(space_id, "file-0.md", "edited")
        again.write_text(space_id, "file-9.md", "new after reopen")
        assert again.read_text(space_id, "file-0.md")[0] == "edited"
        assert again.read_text(space_id, "file-9.md")[0] == "new after reopen"
    finally:
        assert again.close_effects()
        reopened.close()


def test_a_protected_node_keeps_its_node_derived_client_id(tmp_path: Path) -> None:
    store, facade, space_id = _write(tmp_path, "plain-home", 0)
    try:
        space = facade._space(space_id)
        node_id = "protected-node"
        assert facade._content_client_id(
            space, node_id, protected=True
        ) == _node_client_id(node_id)
        assert (
            facade._content_client_id(space, node_id, protected=False)
            == store.facade_client_id()
        )
    finally:
        assert facade.close_effects()
        store.close()


def test_a_store_without_a_facade_id_keeps_the_node_derived_id() -> None:
    facade = LocalOrgFs(author=_AUTHOR)
    try:
        space_id = facade.create_space("no-store").space_id
        node = facade.write_text(space_id, "file.md", "body")
        space = facade._space(space_id)
        assert space.contents[node.doc_id].doc.client_id == _node_client_id(
            node.node_id
        )
    finally:
        assert facade.close_effects()
