from __future__ import annotations

import hashlib
import json

from pycrdt import Map, Text
import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.replica import MemoryReplicaBackend, ReplicaStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import (
    ORGFS_INLINE_UPDATE_BYTES,
    LocalSpaceStore,
    assert_client_ids_registered)


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _doc_target(node_id: str) -> list[dict[str, str]]:
    return [
        {
            "kind": "doc-range",
            "node": f"id:{node_id}",
            "from": "all",
            "to": "all",
        }
    ]


def _latest(store: LocalSpaceStore, doc_id: str) -> bytes:
    return bytes(
        store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid DESC LIMIT 1",
            (doc_id,),
        ).fetchone()[0]
    )


def _scan_replica_backend(replica: ReplicaStore, *needles: bytes) -> None:
    for prefix in ("log/", "snapshot/", "blob/"):
        for key in replica.backend.keys(prefix):
            value = replica.backend.get(key)
            assert value is not None
            assert all(needle not in value for needle in needles), key


def test_purge_full_scan_no_residue_across_member_and_replica_backends(
    tmp_path,
) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        marker = b"P2-FULL-SCAN-SECRET-3e91"
        payload = (marker + b"x" * (ORGFS_INLINE_UPDATE_BYTES + 64_000)).decode()
        node = runtime.facade.write_text(space.space_id, "secret-name.md", payload)
        store = runtime.stores[space.space_id]
        rows = tuple(
            json.loads(bytes(row[0]))
            for row in store._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                (node.doc_id,),
            )
        )
        update_blob = next(row["updateBlob"] for row in rows if "updateBlob" in row)
        runtime.facade.write_text(
            space.space_id, f"id:{node.node_id}", "sanitized and non-empty"
        )
        store.snapshot(node.doc_id, shallow_since=None)
        runtime.serve(space.space_id, "memory")
        replica = runtime._replicas[space.space_id]
        assert replica.backend.get(f"blob/{update_blob}") is not None
        before = store.scan_forbidden_bytes((marker,))
        assert before["journal"]
        assert before["blobs"]
        old_doc_id = node.doc_id
        plan = runtime.purge_plan(space.space_id, _doc_target(node.node_id))
        runtime.purge(space.space_id, plan.plan_id)
        assert store.retired_residue(old_doc_id) == ()
        assert store.scan_forbidden_bytes((marker,)) == {
            "journal": (),
            "documents": (),
            "blobs": (),
        }
        assert not tuple(replica.backend.keys(f"log/{space.space_id}/{old_doc_id}/"))
        assert not tuple(
            replica.backend.keys(f"snapshot/{space.space_id}/{old_doc_id}/")
        )
        assert replica.backend.get(f"blob/{update_blob}") is None
        _scan_replica_backend(replica, marker)
        audit = store._docs["meta"].get("purgeAudit", type=Map).to_py()
        encoded = json.dumps(audit, sort_keys=True)
        assert "secret-name.md" not in encoded
        assert marker.decode() not in encoded
        assert_client_ids_registered(store)
    finally:
        runtime.close()


def test_offline_replica_returns_meta_first_deletes_no_resurrection(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "secret.md", "old")
        old_envelope = _latest(runtime.stores[space.space_id], node.doc_id)
        plan = runtime.purge_plan(space.space_id, _doc_target(node.node_id))
        runtime.purge(space.space_id, plan.plan_id)
        result = runtime.stores[space.space_id].import_envelope(
            old_envelope, supplier="offline-replica"
        )
        assert result.status == "rejected"
        assert result.code == "snapshot-barrier"
        # Simulate an offline backend that still has the old immutable bytes:
        # every mesh response path must consult replicated retirement metadata
        # before those bytes can be served.
        stale_backend = MemoryReplicaBackend()
        value = json.loads(old_envelope)
        old_key = (
            f"log/{space.space_id}/{value['docId']}/"
            f"{value['origin']['writer']}/{value['seq']}"
        )
        stale_backend.put(old_key, old_envelope)
        stale_replica = ReplicaStore(
            space.space_id, stale_backend, runtime.stores[space.space_id]
        )
        with pytest.raises(OrgFsError) as fenced:
            stale_replica.serve_sync_page([node.doc_id])
        assert fenced.value.code == "snapshot-barrier"
    finally:
        runtime.close()


def test_behind_node_pre_snapshot_update_rejected_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "secret.md", "old")
        store = runtime.stores[space.space_id]
        doc_envelope = _latest(store, node.doc_id)
        tree_envelope = _latest(
            store, runtime.facade._spaces[space.space_id].tree_doc_id
        )
        plan = runtime.purge_plan(
            space.space_id,
            [
                *_doc_target(node.node_id),
                {"kind": "tree-range", "from": "all", "to": "all"},
            ],
        )
        runtime.purge(space.space_id, plan.plan_id)
        for envelope in (doc_envelope, tree_envelope):
            result = store.import_envelope(envelope, supplier="behind-node")
            assert (result.status, result.code) == ("rejected", "snapshot-barrier")
    finally:
        runtime.close()


def test_plan_binding_stale_plan_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "secret.md", "one")
        plan = runtime.purge_plan(space.space_id, _doc_target(node.node_id))
        runtime.facade.write_text(space.space_id, f"id:{node.node_id}", "two")
        with pytest.raises(OrgFsError) as stale:
            runtime.purge(space.space_id, plan.plan_id)
        assert stale.value.code == "stale-plan"
    finally:
        runtime.close()


def test_cross_space_blob_purge_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        first = runtime.facade.create_space("first")
        second = runtime.facade.create_space("second")
        payload = b"same bytes"
        first_node = runtime.facade.write_bytes(first.space_id, "asset.bin", payload)
        second_node = runtime.facade.write_bytes(second.space_id, "asset.bin", payload)
        digest = hashlib.sha256(payload).hexdigest()
        plan = runtime.purge_plan(first.space_id, [{"kind": "blob", "sha": digest}])
        runtime.purge(first.space_id, plan.plan_id)
        with pytest.raises(Exception) as purged:
            runtime.facade.read_bytes(first.space_id, f"id:{first_node.node_id}")
        assert getattr(purged.value, "code", None) == "purged"
        assert (
            runtime.facade.read_bytes(second.space_id, f"id:{second_node.node_id}")
            == payload
        )
        assert runtime.blobs.has(digest)
        second_plan = runtime.purge_plan(
            second.space_id, [{"kind": "blob", "sha": digest}]
        )
        runtime.purge(second.space_id, second_plan.plan_id)
        assert not runtime.blobs.has(digest)
    finally:
        runtime.close()


def test_same_content_retransmit_purged_then_unban_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        payload = b"redacted"
        runtime.facade.write_bytes(space.space_id, "asset.bin", payload)
        digest = hashlib.sha256(payload).hexdigest()
        plan = runtime.purge_plan(space.space_id, [{"kind": "blob", "sha": digest}])
        runtime.purge(space.space_id, plan.plan_id)
        with pytest.raises(Exception) as purged:
            runtime.facade.write_bytes(space.space_id, "again.bin", payload)
        assert getattr(purged.value, "code", None) == "purged"
        runtime.unban(space.space_id, digest)
        assert runtime.facade.write_bytes(space.space_id, "again.bin", payload)
    finally:
        runtime.close()


def test_offline_member_purge_pending_then_ack_clears_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner"
    )
    offline: LocalSpaceStore | None = None
    try:
        space = runtime.facade.create_space("team")
        runtime.facade.invite(space.space_id, "user:offline", "rw")
        node = runtime.facade.write_text(space.space_id, "secret.md", "secret")
        owner_store = runtime.stores[space.space_id]
        offline = LocalSpaceStore(
            tmp_path / "offline", space.space_id, node_id="node-offline"
        )
        for (envelope,) in owner_store._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        ):
            offline.import_envelope(bytes(envelope), supplier="node-owner")

        def offline_touch(doc) -> None:
            text = doc.get("text", type=Text)
            text.insert(len(text), " from offline node")

        offline.commit(node.doc_id, offline_touch, author="user:offline", actor=None)
        offline_envelopes = [
            record.envelope_bytes
            for record in offline.unbroadcast()
            if json.loads(record.envelope_bytes)["origin"]["node"] == "node-offline"
        ]
        for envelope in offline_envelopes:
            admitted = owner_store.import_envelope(envelope, supplier="node-offline")
            assert admitted.status == "applied"
            runtime.facade.apply_envelope(space.space_id, envelope)
        plan = runtime.purge_plan(space.space_id, _doc_target(node.node_id))
        result = runtime.purge(space.space_id, plan.plan_id)
        assert "node-offline" in result.pending
        assert "user:offline" not in result.pending
        for (envelope,) in owner_store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            offline.import_envelope(bytes(envelope), supplier="node-owner")

        def acknowledge(doc) -> None:
            acks = doc.get("purgeAcks", type=Map)
            acks["node-offline"] = Map(
                {plan.plan_id: Map({"ackedAt": "2026-09-27T00:00:00Z"})}
            )

        record = offline.commit("meta", acknowledge, author="user:offline", actor=None)
        admitted = owner_store.import_envelope(
            record.envelope_bytes, supplier="node-offline"
        )
        assert admitted.status == "applied"
        runtime.facade.apply_envelope(space.space_id, record.envelope_bytes)
        assert runtime.purge_status(space.space_id, plan.plan_id).pending == ()
    finally:
        if offline is not None:
            offline.close()
        runtime.close()


def test_purge_plan_reports_authors_before_e2e(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        node = runtime.facade.write_text(space.space_id, "secret.md", "one")
        runtime.facade.write_text(space.space_id, f"id:{node.node_id}", "two")
        plan = runtime.purge_plan(space.space_id, _doc_target(node.node_id))
        assert plan.authors_before["user:owner"] >= 2
        assert plan.writers_affected
    finally:
        runtime.close()
