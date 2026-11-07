from __future__ import annotations

import json

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.blobs import BlobPurged
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore


@pytest.fixture
def runtime(tmp_path, request):
    value = OrgFsRuntime(tmp_path / "state", node_id="node-owner", author="user:owner")
    request.addfinalizer(value.close)
    return value


def _space(runtime: OrgFsRuntime, name: str = "team"):
    info = runtime.facade.create_space(name)
    return info, runtime.stores.get(info.space_id)


def _text_plan(runtime: OrgFsRuntime, *, content: str = "secret"):
    info, store = _space(runtime)
    node = runtime.facade.write_text(info.space_id, "note.md", content)
    store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    return info, store, node, plan


def _meta_envelope(
    store, mutate, *, origin_node: str, clock: object | None = None
) -> bytes:
    writer = store._writer_for("user:owner", None)
    probe = Doc(client_id=store._client_id(writer))
    probe.apply_update(store._docs["meta"].get_update())
    before = probe.get_state()
    mutate(probe)
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = 'meta'",
            (writer,),
        ).fetchone()[0]
    )
    store._advance_commit_clock(probe, writer=writer, seq=seq)
    if clock is not None:
        probe.get("__orgfs__", type=Map)["c"] = clock
    raw = store._make_envelope(
        "meta",
        writer,
        seq,
        probe.get_update(before),
        author="user:owner",
        actor=None,
        meta_frontier=store._meta_frontier_for_commit(),
    )
    value = json.loads(raw)
    value["origin"]["node"] = origin_node
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_purge_plan_enumerates_logs_updateblobs_snapshots_and_authors_before(runtime):
    """Review item 29: internal log keys do not extend the frozen plan wire shape."""

    info, store = _space(runtime)
    node = runtime.facade.write_text(info.space_id, "large.md", "x" * 600_000)
    manifest = store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    assert plan.docs[0].log_keys
    assert plan.docs[0].update_blobs
    assert {item.snapshot_id for item in plan.snapshots} == {manifest.snapshot_id}
    assert plan.authors_before == {"user:owner": 1}
    assert "logKeys" not in plan.to_dict()["docs"][0]


def test_purge_plan_recompute_mismatch_stale_plan_never_expands(runtime):
    info, _store, node, plan = _text_plan(runtime)
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "later")
    with pytest.raises(OrgFsError) as caught:
        runtime.facade.purge(info.space_id, plan.plan_id)
    assert caught.value.code == "stale-plan"


def test_purge_plan_expires(runtime, monkeypatch):
    import hyprial.daemon.impl.orgfs.document.operations.purge as docs_module

    info, _store, _node, plan = _text_plan(runtime)
    monkeypatch.setattr(
        docs_module,
        "utc_now",
        lambda: docs_module.parse_iso(plan.expires_at),
    )
    with pytest.raises(OrgFsError) as caught:
        runtime.facade.purge(info.space_id, plan.plan_id)
    assert caught.value.code == "stale-plan"


def test_purge_rebuild_local_byte_scan_no_residue(runtime):
    """Review items 4/6/14a: real byte scan is clean before the purge ack."""

    info, store = _space(runtime)
    marker = "P2R-LARGE-SECRET-7d1a"
    node = runtime.facade.write_text(info.space_id, "note.md", marker + ("x" * 520_000))
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "safe")
    deleted = runtime.facade.mkdir(info.space_id, "P2R-DELETED-SECRET-4ab2")
    runtime.facade.remove(info.space_id, f"id:{deleted.node_id}")
    store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            },
            {
                "kind": "tree-range",
                "from": deleted.version,
                "to": deleted.version,
            },
        ],
    )
    old_doc_id = node.doc_id
    old_path = store.root / "docs" / f"{old_doc_id}.loro"
    before = store.scan_forbidden_bytes([marker.encode(), b"P2R-DELETED-SECRET-4ab2"])
    assert before["journal"]
    assert before["documents"]
    assert before["blobs"]

    observed_before_ack: list[dict[str, tuple[str, ...]]] = []

    def fault(point: str) -> None:
        if point == "before_purge_ack":
            observed_before_ack.append(
                store.scan_forbidden_bytes(
                    [marker.encode(), b"P2R-DELETED-SECRET-4ab2"]
                )
            )
            assert not old_path.exists()

    store.fault_hook = fault
    runtime.facade.purge(info.space_id, plan.plan_id)
    assert observed_before_ack == [{"journal": (), "documents": (), "blobs": ()}]
    assert store.retired_residue(old_doc_id) == ()
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE doc_id = ?", (old_doc_id,)
        ).fetchone()[0]
        == 0
    )
    assert runtime.facade.read_text(info.space_id, f"id:{node.node_id}")[0] == "safe"
    assert all(
        item.node_id != deleted.node_id for item in runtime.facade.trash(info.space_id)
    )
    state_dir = runtime.state_dir
    runtime.close()
    reopened = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        reopened_store = reopened.stores.get(info.space_id)
        assert old_doc_id not in reopened_store.document_ids()
        assert (
            reopened.facade.read_text(info.space_id, f"id:{node.node_id}")[0] == "safe"
        )
        assert all(
            item.node_id != deleted.node_id
            for item in reopened.facade.trash(info.space_id)
        )
    finally:
        reopened.close()


def test_retirement_committed_before_replacement_advertised(runtime):
    info, store, _node, plan = _text_plan(runtime)
    events: list[str] = []
    store.fault_hook = events.append
    runtime.facade.purge(info.space_id, plan.plan_id)
    assert events.index("retirement_committed") < events.index("replacement_advertised")


def test_purge_blob_space_scoped_cross_space(runtime):
    first, _ = _space(runtime, "first")
    second, _ = _space(runtime, "second")
    data = b"same bytes"
    first_node = runtime.facade.write_bytes(first.space_id, "a.bin", data)
    runtime.facade.write_bytes(second.space_id, "b.bin", data)
    sha = first_node.blob_hash
    first_plan = runtime.facade.purge_plan(
        first.space_id, [{"kind": "blob", "sha": sha}]
    )
    runtime.facade.purge(first.space_id, first_plan.plan_id)
    with pytest.raises(BlobPurged):
        runtime.blobs.get(first.space_id, sha)
    assert runtime.blobs.get(second.space_id, sha) == data
    assert runtime.blobs.has(sha)
    second_plan = runtime.facade.purge_plan(
        second.space_id, [{"kind": "blob", "sha": sha}]
    )
    runtime.facade.purge(second.space_id, second_plan.plan_id)
    assert not runtime.blobs.has(sha)


def test_purge_same_content_rewrite_rejected_then_unban_allows(runtime):
    """Review item 19: the facade exposes the frozen purged error type/code."""

    info, _store = _space(runtime)
    data = b"classified"
    node = runtime.facade.write_bytes(info.space_id, "a.bin", data)
    plan = runtime.facade.purge_plan(
        info.space_id, [{"kind": "blob", "sha": node.blob_hash}]
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    with pytest.raises(OrgFsError) as caught:
        runtime.facade.write_bytes(info.space_id, "again.bin", data)
    assert caught.value.code == "purged"
    runtime.facade.unban(info.space_id, node.blob_hash)
    assert (
        runtime.facade.write_bytes(info.space_id, "again.bin", data).blob_hash
        == node.blob_hash
    )


def test_purge_ack_key_must_equal_origin_node(runtime):
    _info, store = _space(runtime)
    store.commit(
        "meta",
        lambda doc: doc.get("purgePlans", type=Map).__setitem__(
            "p", Map({"at": "now"})
        ),
        author="user:owner",
        actor=None,
    )

    def forge(doc):
        doc.get("purgeAcks", type=Map)["other"] = Map({"p": Map({"ackedAt": "now"})})

    envelope = _meta_envelope(store, forge, origin_node=store.node_id)
    assert store.admission(envelope) == "not-a-member"


def test_purge_ack_unknown_plan_rejected(runtime):
    _info, store = _space(runtime)

    def forge(doc):
        doc.get("purgeAcks", type=Map)["origin"] = Map(
            {"missing": Map({"ackedAt": "now"})}
        )

    envelope = _meta_envelope(store, forge, origin_node="origin")
    assert store.admission(envelope) == "not-a-member"


def test_purge_result_pending_lists_offline_member(runtime):
    info, store, _node, plan = _text_plan(runtime)
    runtime.facade.invite(info.space_id, "user:offline")

    offline_writer = "node-offline.incarnation.lane"
    offline_peer_id = store._client_id(offline_writer)

    def register_offline_node() -> None:
        writers = runtime.facade._space(info.space_id).meta.get("writers", type=Map)
        writers[offline_writer] = Map(
            {
                "node": "node-offline",
                "peerId": offline_peer_id,
                "peerIds": [offline_peer_id],
                "author": "user:offline",
                "actor": None,
                "startedAt": "2026-09-27T00:00:00Z",
            }
        )

    runtime.facade._commit(
        runtime.facade._space(info.space_id), "meta", register_offline_node
    )
    # Membership changed after planning, so bind a fresh exact plan.
    node = runtime.facade.stat(info.space_id, "note.md")
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    result = runtime.facade.purge(info.space_id, plan.plan_id)
    assert "node-offline" in result.pending
    assert "user:offline" not in result.pending
    assert (
        runtime.facade.purge_status(info.space_id, plan.plan_id).pending
        == result.pending
    )


def test_r7_purge_status_accounts_for_writerless_rw_and_ro_then_ack_binds_node(
    tmp_path,
):
    """R7: every member is pending; a first ack replaces its user placeholder."""

    runtime = OrgFsRuntime(
        tmp_path / "r7-owner", node_id="node-owner", author="user:owner"
    )
    member = None
    try:
        info = runtime.facade.create_space("team")
        runtime.facade.invite(info.space_id, "user:rw", "rw")
        runtime.facade.invite(info.space_id, "user:ro", "ro")
        node = runtime.facade.write_text(info.space_id, "note.md", "secret")
        plan = runtime.facade.purge_plan(
            info.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": node.version,
                    "to": node.version,
                }
            ],
        )
        result = runtime.facade.purge(info.space_id, plan.plan_id)
        assert {"user:rw", "user:ro"} <= set(result.pending)

        owner_store = runtime.stores.get(info.space_id)
        member = FileSpaceStore(
            tmp_path / "r7-member",
            info.space_id,
            node_id="node-ro",
            blob_store=runtime.blobs,
        )
        for (envelope,) in owner_store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            admitted = member.import_envelope(bytes(envelope), supplier="node-owner")
            assert admitted.status == "applied", (admitted.code, admitted.details)

        def acknowledge(doc):
            doc.get("purgeAcks", type=Map)["node-ro"] = Map(
                {plan.plan_id: Map({"ackedAt": "2026-09-27T00:00:00Z"})}
            )

        record = member.commit("meta", acknowledge, author="user:ro", actor=None)
        admitted = owner_store.import_envelope(
            record.envelope_bytes, supplier="node-ro"
        )
        assert admitted.status == "applied", (admitted.code, admitted.details)
        runtime.facade.apply_envelope(info.space_id, record.envelope_bytes)
        status = runtime.facade.purge_status(info.space_id, plan.plan_id)
        assert "user:ro" not in status.pending
        assert "node-ro" in status.acknowledged
        assert "user:rw" in status.pending
    finally:
        if member is not None:
            member.close()
        runtime.close()


def test_purge_audit_record_has_no_names_or_paths(runtime):
    info, _store, _node, plan = _text_plan(runtime, content="content-secret")
    runtime.facade.purge(info.space_id, plan.plan_id)
    audit = (
        runtime.facade._space(info.space_id)
        .meta.get("purgeAudit", type=Map)
        .to_py()[plan.plan_id]
    )
    encoded = json.dumps(audit, sort_keys=True)
    assert "note.md" not in encoded
    assert "content-secret" not in encoded
    assert set(audit) == {"at", "nodeId", "docIds", "blobHashes"}


def test_behind_writer_unsynced_work_exports_as_new_file(runtime):
    info, _store = _space(runtime)
    node = runtime.facade.write_text(info.space_id, "behind.md", "unpublished")
    exported = runtime.facade._export_behind_writer_work(
        runtime.facade._space(info.space_id), node.doc_id, "a" * 64
    )
    assert exported is not None
    assert exported.doc_id != node.doc_id
    assert exported.path == "purge-conflict-aaaaaaaaaaaa.md"
    assert (
        runtime.facade.read_text(info.space_id, f"id:{exported.node_id}")[0]
        == "unpublished"
    )


def test_meta_clock_marker_value_shape_enforced(runtime):
    """Review item 14b: n12 is enforced through gate 3/import_envelope."""

    _info, store = _space(runtime)
    for invalid in ("bad", True, 3.5):
        envelope = _meta_envelope(
            store,
            lambda doc: doc.get("purgePlans", type=Map).__setitem__(
                f"plan-{invalid}", Map({"at": "now"})
            ),
            origin_node=store.node_id,
            clock=invalid,
        )
        result = store.import_envelope(envelope, supplier="node-live")
        assert (result.status, result.code) == ("rejected", "not-a-member")


def test_review_dispute_integral_float_clock_is_same_pycrdt_bytes():
    """REVIEW-DISPUTE item 18: pycrdt erases int-vs-integral-float shape."""

    integer = Doc(client_id=73)
    integer.get("__orgfs__", type=Map)["c"] = 3
    integral_float = Doc(client_id=73)
    integral_float.get("__orgfs__", type=Map)["c"] = 3.0

    assert integer.get_update() == integral_float.get_update()
    assert integer.get("__orgfs__", type=Map).to_py()["c"] == 3.0
    assert isinstance(integer.get("__orgfs__", type=Map).to_py()["c"], float)


def test_m2_stale_token_membership_cache_equals_full_rebuild(runtime, monkeypatch):
    """M2: a member suffix refreshes baselines pinned to an older owner token."""

    info, store = _space(runtime)
    runtime.facade.invite(info.space_id, "user:a")
    old_token = store._meta_frontier_for_commit()
    assert store._membership_at(old_token) is not None
    store.commit(
        "meta",
        lambda doc: doc.get("purgePlans", type=Map).__setitem__(
            "known", Map({"at": "now"})
        ),
        author="user:owner",
        actor=None,
    )
    token = store._meta_frontier_for_commit()
    assert token != old_token
    # Model the divergent replica that materialized this older token after a
    # later owner commit but before the member-authored suffix arrived.
    assert store._membership_at(old_token) is not None
    cached = store._db.execute(
        "SELECT owner, membership_json, meta_update FROM meta_frontiers WHERE token = ?",
        (old_token,),
    ).fetchone()
    assert cached is not None
    rebuild_calls = 0
    original_membership_at = store._membership_at
    old_baseline = original_membership_at(old_token)
    assert old_baseline is not None

    def observe_rebuild(value):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return original_membership_at(value)

    monkeypatch.setattr(store, "_membership_at", observe_rebuild)

    writer = "node-a.incarnation.lane"
    probe = Doc(client_id=store._client_id(writer))
    probe.apply_update(old_baseline[2].get_update())
    before = probe.get_state()
    store._register_writer(
        probe,
        writer=writer,
        author="user:a",
        actor=None,
        peer_ids={store._client_id(writer)},
    )
    registered = probe.get("writers", type=Map).to_py()[writer]
    registered["node"] = "node-a"
    probe.get("writers", type=Map)[writer] = Map(registered)
    store._advance_commit_clock(probe, writer=writer, seq=0)
    suffix = store._make_envelope(
        "meta",
        writer,
        0,
        probe.get_update(before),
        author="user:a",
        actor=None,
        meta_frontier=old_token,
    )
    suffix_value = json.loads(suffix)
    suffix_value["origin"]["node"] = "node-a"
    result = store.import_envelope(
        json.dumps(suffix_value, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-a",
    )
    assert result.status == "applied", (result.code, result.details)
    assert rebuild_calls > 0
    after_suffix = store._db.execute(
        "SELECT owner, membership_json, meta_update FROM meta_frontiers WHERE token = ?",
        (old_token,),
    ).fetchone()
    if after_suffix is None:
        assert store._membership_at(old_token) is not None
        after_suffix = store._db.execute(
            "SELECT owner, membership_json, meta_update FROM meta_frontiers WHERE token = ?",
            (old_token,),
        ).fetchone()
    assert writer in store._membership_at(old_token)[2].get("writers", type=Map)
    incremental = (
        after_suffix["owner"],
        after_suffix["membership_json"],
        bytes(after_suffix["meta_update"]),
    )

    with store._db:
        store._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (old_token,))
    rebuilt = store._membership_at(old_token)
    assert rebuilt is not None
    full = store._db.execute(
        "SELECT owner, membership_json, meta_update FROM meta_frontiers WHERE token = ?",
        (old_token,),
    ).fetchone()
    assert (
        full["owner"],
        full["membership_json"],
        bytes(full["meta_update"]),
    ) == incremental


def test_combined_tree_doc_purge_commits_replacement_pointer(runtime):
    """Review item 5: combined tree+doc rebuild persists the new content pointer."""

    info, store = _space(runtime)
    node = runtime.facade.write_text(info.space_id, "note.md", "safe")
    old_doc_id = node.doc_id
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            },
            {"kind": "tree-range", "from": node.version, "to": node.version},
        ],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    replacement = store.retired(old_doc_id).replacement_doc_id
    assert (
        runtime.facade.stat(info.space_id, f"id:{node.node_id}").doc_id == replacement
    )
    materialized = runtime.facade._space(info.space_id).tree.materialize()
    assert materialized[node.node_id]["doc_id"] == replacement


def test_purge_plan_survives_facade_restart(runtime):
    """Review item 20: an unexpired owner-local purge plan is durable."""

    info, _store, node, plan = _text_plan(runtime)
    state_dir = runtime.state_dir
    runtime.close()
    reopened = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        result = reopened.facade.purge(info.space_id, plan.plan_id)
        assert result.executed_locally
        assert (
            reopened.facade.stat(info.space_id, f"id:{node.node_id}").doc_id
            != node.doc_id
        )
    finally:
        reopened.close()


def test_m3_purge_status_loads_saved_unexecuted_plan_after_restart(runtime):
    """M3: the public status entry reads an owner-local durable purge plan."""

    info, _store, _node, plan = _text_plan(runtime)
    state_dir = runtime.state_dir
    runtime.close()
    reopened = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        status = reopened.facade.purge_status(info.space_id, plan.plan_id)
        assert status.plan_id == plan.plan_id
        assert status.acknowledged == ()
        assert status.pending == ("node-owner",)
    finally:
        reopened.close()


def test_purge_leaves_nothing_a_later_state_checkpoint_can_rewrite(runtime):
    # State files are refreshed in batches.  A member replica deletes a
    # retired document's objects while it may still hold the live document
    # with an unrefreshed change; no later checkpoint or close() may rewrite
    # that document's state file from memory.
    info, store = _space(runtime)
    marker = "P2R-DIRTY-SECRET-91c3"
    node = runtime.facade.write_text(info.space_id, "note.md", marker)
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "safe")
    store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    assert store.retired(node.doc_id) is not None

    # The member-replica state: a live copy holding the purged text, marked
    # changed since the last checkpoint, when its retired objects are deleted.
    live = Doc()
    live.get("text", type=Text).insert(0, marker)
    store._docs[node.doc_id] = live
    store._dirty_docs.add(node.doc_id)
    store.delete_retired_objects(node.doc_id)
    # Guard 1: deleting the retired objects also drops the live copy.
    assert node.doc_id not in store._docs
    assert node.doc_id not in store._dirty_docs
    store.checkpoint()

    assert not (store.root / "docs" / f"{node.doc_id}.loro").exists()
    assert store.scan_forbidden_bytes([marker.encode()])["documents"] == ()


def test_a_checkpoint_never_writes_a_retired_document(runtime):
    # Guard 2: between the retirement record and the deletion of its objects,
    # a checkpoint must still refuse to write the retired document.
    info, store = _space(runtime)
    marker = "P2R-CHECKPOINT-SECRET-2b44"
    node = runtime.facade.write_text(info.space_id, "note.md", marker)
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "safe")
    store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    live = Doc()
    live.get("text", type=Text).insert(0, marker)
    store._docs[node.doc_id] = live
    store._dirty_docs.add(node.doc_id)

    store.checkpoint()

    assert not (store.root / "docs" / f"{node.doc_id}.loro").exists()


def test_purged_text_is_not_readable_from_any_in_memory_version(runtime):
    # Versions share snapshot entries in memory; after a purge no earlier
    # version that saw the purged text may stay reachable through read_at,
    # history or restore (customer-data deletion right).
    info, store = _space(runtime)
    marker = "P2R-HISTORY-SECRET-5e07"
    node = runtime.facade.write_text(info.space_id, "note.md", marker)
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "safe")
    store.snapshot(node.doc_id, shallow_since=None)
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)

    with pytest.raises(OrgFsError):
        runtime.facade.read_at(info.space_id, f"id:{node.node_id}", node.version)
    with pytest.raises(OrgFsError):
        runtime.facade.restore(info.space_id, f"id:{node.node_id}", node.version)
    space = runtime.facade._spaces[info.space_id]
    for version in space.snapshots.values():
        for entry in version.values():
            assert marker.encode() not in entry.content
    for entries in space.history.values():
        for entry in entries:
            assert marker.encode() not in entry.node.content
    assert runtime.facade.read_text(info.space_id, f"id:{node.node_id}")[0] == "safe"


def test_retirement_vacuum_resets_the_meta_journal_cursor(runtime):
    # The owner-client-id cache scans meta journal rows past a rowid cursor.
    # VACUUM (run when retired objects are deleted) may renumber rowids of the
    # commits table, so the cursor must be dropped and the next read rescans.
    info, store = _space(runtime)
    node = runtime.facade.write_text(info.space_id, "note.md", "x")
    runtime.facade.write_text(info.space_id, f"id:{node.node_id}", "y")
    store.snapshot(node.doc_id, shallow_since=None)
    store._journal_client_ids("user:owner")
    assert store._meta_journal_scan is not None
    plan = runtime.facade.purge_plan(
        info.space_id,
        [
            {
                "kind": "doc-range",
                "node": f"id:{node.node_id}",
                "from": node.version,
                "to": node.version,
            }
        ],
    )
    runtime.facade.purge(info.space_id, plan.plan_id)
    # A cursor past every row stands in for one that VACUUM made stale.
    store._meta_journal_scan = {"rowid": 10**9, "by_author": {}}
    store.delete_retired_objects(node.doc_id)  # runs VACUUM again

    assert store._meta_journal_scan is None
    assert store._journal_client_ids("user:owner")
