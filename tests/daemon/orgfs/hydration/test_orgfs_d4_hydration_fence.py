"""D4 hydration applies a fetched snapshot only to the document it was captured from."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _ContentDocument
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime


def test_hydration_rejects_a_node_rebound_to_another_document(tmp_path, request):
    # Replacement snapshots require a real durable store, while the original
    # incarnation/barrier assertions below remain the subject of this test.
    runtime = OrgFsRuntime(tmp_path, node_id="local", author="user:local")
    request.addfinalizer(runtime.close)
    facade = runtime.facade
    space = facade.create_space("hydration incarnation")
    node = facade.write_text(space.space_id, "note.md", "retired payload")
    old_snapshot = facade.export_content_update(space.space_id, f"id:{node.node_id}")
    successor = _ContentDocument("successor")
    tree = TreeDocument(update=facade.export_tree_update(space.space_id))
    original = facade._spaces[space.space_id].nodes[node.node_id]
    replacement_id = "doc-successor"
    tree.record(
        replace(
            original,
            doc_id=replacement_id,
            required_content_frontier=successor.doc.get_state(),
        )
    )
    facade.apply_tree_update(space.space_id, tree.get_update())
    facade.install_replacement_snapshot(
        space.space_id, node.doc_id, replacement_id, successor.export()
    )
    with pytest.raises(OrgFsError) as caught:
        facade.hydrate_content_snapshot(
            space.space_id,
            f"id:{node.node_id}",
            old_snapshot,
            expected_doc_id=node.doc_id,
        )
    assert caught.value.code == "snapshot-barrier"
    assert caught.value.details["replacementDocId"] == replacement_id
    assert facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "successor"


@pytest.fixture
def memory_facade():
    facade = LocalOrgFs()
    try:
        yield facade
    finally:
        assert facade.close_effects()


def test_hydration_into_the_captured_document_still_merges(memory_facade):
    facade = memory_facade
    space = facade.create_space("hydration same incarnation")
    node = facade.write_text(space.space_id, "note.md", "payload")
    snapshot = facade.export_content_update(space.space_id, f"id:{node.node_id}")
    facade.hydrate_content_snapshot(
        space.space_id, f"id:{node.node_id}", snapshot, expected_doc_id=node.doc_id
    )
    assert facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "payload"
