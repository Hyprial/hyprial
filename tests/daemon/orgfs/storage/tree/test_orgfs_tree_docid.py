from __future__ import annotations

import base64
import json
import uuid

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import StoreError


@pytest.fixture
def runtime(tmp_path, request):
    value = OrgFsRuntime(tmp_path / "state", node_id="node-owner", author="user:owner")
    request.addfinalizer(value.close)
    return value


def _space(runtime: OrgFsRuntime):
    info = runtime.facade.create_space("team")
    return info, runtime.stores.get(info.space_id)


def test_genesis_writes_physical_tree_docid_mapping(runtime):
    info, store = _space(runtime)
    runtime.facade.mkdir(info.space_id, "docs")
    active = store.active_tree_doc_id()
    assert active is not None and active.startswith("tree-")
    docs = store._docs["meta"].get("docs", type=Map).to_py()
    assert docs["tree"]["active"] == active
    envelopes = [
        json.loads(row[0])
        for row in store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id <> 'meta'"
        )
    ]
    assert {item["docId"] for item in envelopes} == {active}


def test_tree_rebuild_rejects_pre_purge_update_with_snapshot_barrier(runtime):
    info, store = _space(runtime)
    sensitive = runtime.facade.mkdir(info.space_id, "sensitive-codename")
    old_tree = store.active_tree_doc_id()
    old_envelope = bytes(
        store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid LIMIT 1",
            (old_tree,),
        ).fetchone()[0]
    )
    runtime.facade.move(info.space_id, f"id:{sensitive.node_id}", "sanitized")
    current = runtime.facade.stat(info.space_id, f"id:{sensitive.node_id}").version
    plan = runtime.facade.purge_plan(
        info.space_id,
        [{"kind": "tree-range", "from": current, "to": current}],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    new_tree = store.active_tree_doc_id()
    assert new_tree != old_tree
    before = store.snapshot(new_tree, shallow_since=None).snapshot_bytes
    result = store.import_envelope(old_envelope, supplier="node-late")
    assert result.code == "snapshot-barrier"
    assert result.details == {
        "retiredDocId": old_tree,
        "replacementDocId": new_tree,
        "planId": plan.plan_id,
    }
    assert store.snapshot(new_tree, shallow_since=None).snapshot_bytes == before
    assert b"sensitive-codename" not in before


def test_retired_docid_rejected_on_import_export_snapshot_paths(runtime):
    """Review item 21: admission returns the frozen snapshot-barrier verdict."""

    info, store = _space(runtime)
    runtime.facade.mkdir(info.space_id, "old")
    old_tree = store.active_tree_doc_id()
    envelope = bytes(
        store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? LIMIT 1",
            (old_tree,),
        ).fetchone()[0]
    )
    version = runtime.facade.stat(info.space_id, "old").version
    plan = runtime.facade.purge_plan(
        info.space_id,
        [{"kind": "tree-range", "from": version, "to": version}],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    assert store.import_envelope(envelope, supplier="late").code == "snapshot-barrier"
    for action in (
        lambda: store.export_since(old_tree, b"", cursor=None, max_bytes=10_000),
        lambda: store.snapshot(old_tree, shallow_since=None),
    ):
        with pytest.raises(StoreError) as caught:
            action()
        assert caught.value.code == "snapshot-barrier"
    assert store.admission(envelope) == "snapshot-barrier"


def test_retirement_record_immutable(runtime):
    info, store = _space(runtime)
    runtime.facade.mkdir(info.space_id, "old")
    old_tree = store.active_tree_doc_id()
    version = runtime.facade.stat(info.space_id, "old").version
    plan = runtime.facade.purge_plan(
        info.space_id,
        [{"kind": "tree-range", "from": version, "to": version}],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)

    def rewrite(doc):
        retirements = doc.get("retirements", type=Map)
        retirements[old_tree] = Map(
            {
                "replacementDocId": "tree-forged",
                "planId": plan.plan_id,
                "snapshotId": "0" * 64,
                "retiredAt": "forged",
            }
        )

    with pytest.raises(StoreError) as caught:
        store.commit("meta", rewrite, author="user:owner", actor=None)
    assert caught.value.code == "not-owner"
    assert store.retired(old_tree).replacement_doc_id != "tree-forged"


def test_literal_tree_wire_id_is_rejected_without_fallback(runtime):
    """Review item 7: production import accepts only physical tree UUID ids."""

    info, store = _space(runtime)
    runtime.facade.mkdir(info.space_id, "docs")
    active = store.active_tree_doc_id()
    raw = bytes(
        store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? LIMIT 1", (active,)
        ).fetchone()[0]
    )
    envelope = json.loads(raw)
    envelope["docId"] = "tree"
    result = store.import_envelope(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-live",
    )
    assert (result.status, result.code) == ("rejected", "invalid-argument")
    assert "tree" not in store.document_ids()


def test_r1_rw_member_shadow_tree_envelope_cannot_touch_active_tree_or_reopen(
    tmp_path,
):
    """R1: a valid fresh physical-tree id cannot alias the active tree file."""

    state_dir = tmp_path / "r1-state"
    runtime = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        info = runtime.facade.create_space("team")
        runtime.facade.invite(info.space_id, "user:rw")
        runtime.facade.mkdir(info.space_id, "kept")
        store = runtime.stores.get(info.space_id)
        active = store.active_tree_doc_id()
        assert active is not None
        active_before = store.snapshot(active, shallow_since=None).snapshot_bytes
        tree_file_before = (store.root / "tree.loro").read_bytes()

        shadow_id = f"tree-{uuid.uuid4()}"
        writer = "node-rw.incarnation.lane"
        shadow = Doc(client_id=store._client_id(writer))
        shadow.get("shadow", type=Map)["payload"] = "must-not-persist"
        store._advance_commit_clock(shadow, writer=writer, seq=0)
        envelope = store._make_envelope(
            shadow_id,
            writer,
            0,
            shadow.get_update(),
            author="user:rw",
            actor="agent:rw:test:orgfs",
            meta_frontier=store._meta_frontier_for_commit(),
        )

        result = store.import_envelope(envelope, supplier="node-rw")
        assert (result.status, result.code) == ("rejected", "unknown-doc")
        assert shadow_id not in store.document_ids()
        assert (
            store.snapshot(active, shallow_since=None).snapshot_bytes == active_before
        )
        assert (store.root / "tree.loro").read_bytes() == tree_file_before
    finally:
        runtime.close()

    reopened = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        reopened_store = reopened.stores.get(info.space_id)
        assert reopened_store.active_tree_doc_id() == active
        assert shadow_id not in reopened_store.document_ids()
        assert (
            reopened_store.snapshot(active, shallow_since=None).snapshot_bytes
            == active_before
        )
        assert reopened.facade.stat(info.space_id, "kept").path == "kept"
    finally:
        reopened.close()


def test_r5_pending_drain_keeps_snapshot_barrier_after_retirement(runtime, caplog):
    """R5: a parked old-tree envelope drains with snapshot-barrier semantics."""

    info, store = _space(runtime)
    runtime.facade.mkdir(info.space_id, "kept")
    old_tree = store.active_tree_doc_id()
    assert old_tree is not None
    replacement = f"tree-{uuid.uuid4()}"
    plan_id = "r5-pending-drain-plan"
    writer = store._writer_for("user:owner", None)
    meta_seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = 'meta'",
            (writer,),
        ).fetchone()[0]
    )

    def retire(doc):
        doc.get("retirements", type=Map)[old_tree] = Map(
            {
                "replacementDocId": replacement,
                "planId": plan_id,
                "snapshotId": "0" * 64,
                "retiredAt": "2026-09-27T00:00:00Z",
            }
        )
        doc.get("docs", type=Map)["tree"] = Map(
            {
                "active": replacement,
                "activatedAt": "2026-09-27T00:00:00Z",
                "planId": plan_id,
            }
        )

    future = Doc(client_id=store._client_id(writer))
    future.apply_update(store._docs["meta"].get_update())
    retire(future)
    store._advance_commit_clock(future, writer=writer, seq=meta_seq)
    future_token = store._project_frontier(future, "user:owner")

    late_writer = "node-late.incarnation.lane"
    late = Doc(client_id=store._client_id(late_writer))
    late.apply_update(store._docs[old_tree].get_update())
    before = late.get_state()
    late.get("late", type=Map)["payload"] = "must-not-apply"
    store._advance_commit_clock(late, writer=late_writer, seq=0)
    envelope = store._make_envelope(
        old_tree,
        late_writer,
        0,
        late.get_update(before),
        author="user:owner",
        actor=None,
        meta_frontier=future_token,
    )
    raw = json.loads(envelope)
    assert base64.b64decode(raw["origin"]["metaFrontier"]) == future_token
    parked = store.import_envelope(envelope, supplier="node-late")
    assert (parked.status, parked.code) == ("pending", "pending-meta")

    with caplog.at_level("WARNING", logger='hyprial.daemon.impl.orgfs.storage.store'):
        store.commit("meta", retire, author="user:owner", actor=None)

    assert "code=snapshot-barrier" in caplog.text
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = ? AND writer = ?",
            (old_tree, late_writer),
        ).fetchone()[0]
        == 0
    )
