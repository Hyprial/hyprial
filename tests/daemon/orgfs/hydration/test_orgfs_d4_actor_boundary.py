"""D4 preserves per-space writer and bounded read-fetch custody."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh


def test_hydration_and_text_write_enter_space_owner(monkeypatch):
    facade = LocalOrgFs()
    try:
        space = facade.create_space("owned content")
        node = facade.write_text(space.space_id, "note.md", "base")
        observed = []
        original = facade._execute_space_operation

        def capture(operation):
            observed.append((type(operation).__name__, threading.get_ident()))
            return original(operation)

        monkeypatch.setattr(facade, "_execute_space_operation", capture)
        caller = threading.get_ident()
        snapshot = facade.export_content_update(space.space_id, f"id:{node.node_id}")
        facade.hydrate_content_snapshot(
            space.space_id, f"id:{node.node_id}", snapshot,
            expected_doc_id=node.doc_id,
        )
        facade.write_text(space.space_id, f"id:{node.node_id}", "owned")
        assert {name for name, _thread in observed} == {
            "_HydrateContentSnapshot",
            "_WriteText",
        }
        assert all(owner != caller for _name, owner in observed)
        assert facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "owned"
    finally:
        assert facade.close_effects()


def test_read_fetch_coalesces_bounds_and_retains_close_custody(monkeypatch):
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.network.recovery.ORGFS_BLOB_FETCH_QUEUE_LIMIT', 2)

    def registration(*_args, **_kwargs):
        return SimpleNamespace(close=lambda: None)

    transport = SimpleNamespace(
        subscribe=registration,
        declare_query_handler=registration,
    )
    mesh = OrgFsMesh(
        transport,
        SimpleNamespace(space_id="space"),
        "reader",
        author="user:r",
        blob_store=SimpleNamespace(contains=lambda _digest: False),
        recovery_candidates=lambda: ("supplier",),
    )
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(mesh, "_supplier_allowed", lambda _supplier: True)

    def fetch(_supplier, _digest):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(mesh, "_fetch_blob_into_store", fetch)
    try:
        first = mesh.request_blob("a" * 64)
        assert entered.wait(1)
        assert all(mesh.request_blob("a" * 64) is first for _ in range(50))
        second = mesh.request_blob("b" * 64)
        refused = mesh.request_blob("c" * 64)
        assert refused.is_set()
        assert len(mesh._blob_fetch_pending) == 2
        assert len(mesh._blob_fetch_waiters) == 2
        assert all(len(waiters) == 1 for waiters in mesh._blob_fetch_waiters.values())
        assert len(mesh._worker_futures) == 2
        with pytest.raises(TimeoutError):
            mesh.close(timeout=0.01)
        assert not first.is_set()
        release.set()
        assert first.wait(2)
        assert second.wait(2)
    finally:
        release.set()
        mesh.close()


def test_hydration_rejects_a_node_rebound_to_another_document(tmp_path):
    from dataclasses import replace
    from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _ContentDocument
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(tmp_path, node_id="local", author="user:local")
    facade = runtime.facade
    try:
        space = facade.create_space("hydration incarnation")
        node = facade.write_text(space.space_id, "note.md", "retired payload")
        old_snapshot = facade.export_content_update(space.space_id, f"id:{node.node_id}")
        successor = _ContentDocument("successor")
        tree = TreeDocument(update=facade.export_tree_update(space.space_id))
        original = facade._spaces[space.space_id].nodes[node.node_id]
        replacement_id = "doc-successor"
        tree.record(replace(
            original, doc_id=replacement_id,
            required_content_frontier=successor.doc.get_state(),
        ))
        facade.apply_tree_update(space.space_id, tree.get_update())
        facade.install_replacement_snapshot(
            space.space_id, node.doc_id, replacement_id, successor.export()
        )
        with pytest.raises(OrgFsError) as caught:
            facade.hydrate_content_snapshot(
                space.space_id, f"id:{node.node_id}", old_snapshot,
                expected_doc_id=node.doc_id,
            )
        assert caught.value.code == "snapshot-barrier"
        assert caught.value.details["replacementDocId"] == replacement_id
        assert facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "successor"
    finally:
        runtime.close()
        assert facade.close_effects()
