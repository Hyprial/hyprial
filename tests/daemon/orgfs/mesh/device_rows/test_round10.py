"""ROUND 10 forged suppliers, empty frontiers, and replica priming."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
from types import SimpleNamespace

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    ReplicaStore,
)
from hyprial.daemon.impl.orgfs.storage.store import ExportPage
from hyprial.daemon.impl.transport import TransportSample

from ...authority.admission.support import (
    TREE_DOC_ID,
    _close_stores as _close_stores,
    _store,
)
from .test_round4 import BOB, OWNER, _commits_since, _position, replicas
from .test_round8 import _pending_meta_envelope
from ..test_orgfs_mesh import _Bus

_IMPORTED_REPLICAS_FIXTURE = replicas


@pytest.fixture
def round10_replicas(request):
    assert _IMPORTED_REPLICAS_FIXTURE is not None
    return request.getfixturevalue("replicas")


def _ingress(runtime, space_id: str, envelope: bytes) -> None:
    runtime._meshes[space_id]._process_log_sample(TransportSample("", envelope))


@pytest.mark.xfail(
    strict=True,
    reason="supplier is self-declared until signed updates (card 21aafdb0 G4)",
)
def test_r10_forged_origin_node_cannot_fill_honest_supplier_bucket(
    round10_replicas,
) -> None:
    """G4 trigger: signed supplier identity will make this strict xfail red."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round10_replicas
    store = alice.stores[space_id]
    store._pending_import_limit = 8
    store._pending_import_supplier_limit = 1
    forged = _pending_meta_envelope(
        owner,
        space_id,
        author=BOB,
        supplier=owner.node_id,
        index=1001,
    )
    honest = _pending_meta_envelope(
        owner,
        space_id,
        author=OWNER,
        supplier=owner.node_id,
        index=1002,
    )

    _ingress(alice, space_id, forged)
    _ingress(alice, space_id, honest)

    assert store._db.execute(
        "SELECT 1 FROM pending WHERE supplier = ? AND author = ?",
        (owner.node_id, f"user:{OWNER}"),
    ).fetchone() is not None


def test_r10_forged_fill_only_delays_update_until_anti_entropy(
    round10_replicas, monkeypatch
) -> None:
    """A capacity drop is non-durable; ordered anti-entropy still converges."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round10_replicas
    store = alice.stores[space_id]
    store._pending_import_limit = 8
    store._pending_import_supplier_limit = 1
    forged = _pending_meta_envelope(
        owner,
        space_id,
        author=BOB,
        supplier=owner.node_id,
        index=1010,
    )
    _ingress(alice, space_id, forged)

    monkeypatch.setattr(
        owner._meshes[space_id], "broadcast_pending", lambda _space_id=None: 0
    )
    before = _position(owner, space_id)
    owner.facade.mkdir(space_id, "round10-parent")
    owner.facade.mkdir(space_id, "round10-parent/child")
    parent, child = tuple(
        envelope
        for envelope in _commits_since(owner, space_id, before)
        if str(json.loads(envelope)["docId"]).startswith("tree-")
    )

    _ingress(alice, space_id, child)
    child_value = json.loads(child)
    assert store._db.execute(
        "SELECT 1 FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
        (
            child_value["origin"]["writer"],
            child_value["seq"],
            child_value["docId"],
        ),
    ).fetchone() is None
    assert store._pending_drop_count == 1

    assert alice._meshes[space_id].sync_from(owner.node_id) >= 2
    assert alice.facade.stat(space_id, "round10-parent/child").kind == "dir"
    assert parent != child


def test_r10_non_meta_empty_frontier_is_rejected_not_parked(tmp_path) -> None:
    """An empty causal token is valid only for the unique meta genesis."""

    source = _store(tmp_path, "r10-empty-source", "node-source")
    genesis = source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    change = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("empty-frontier", Text("must reject")),
        author="user:owner",
        actor="agent:owner:source",
    )
    raw = json.loads(change.envelope_bytes)
    raw["origin"]["metaFrontier"] = ""
    empty_frontier = json.dumps(
        raw, sort_keys=True, separators=(",", ":")
    ).encode()
    target = _store(tmp_path, "r10-empty-target", "node-target")
    assert target.import_envelope(
        genesis.envelope_bytes, supplier="node-source"
    ).status == "applied"

    result = target.import_envelope(empty_frontier, supplier="node-source")

    assert (result.status, result.code) == ("rejected", "not-a-member")
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_r12_malformed_varint_frontier_is_rejected_by_mesh(tmp_path) -> None:
    """Malformed causal tokens stay typed rejections at the mesh boundary."""

    source = _store(tmp_path, "r12-varint-source", "node-source")
    genesis = source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    record = source.commit(
        "meta",
        lambda doc: doc["members"].__setitem__("user:alice", Map({"mode": "rw"})),
        author="user:owner",
        actor=None,
    )
    malformed = json.loads(record.envelope_bytes)
    malformed["origin"]["metaFrontier"] = base64.b64encode(b"\x80" * 10).decode()
    envelope = json.dumps(malformed, sort_keys=True, separators=(",", ":")).encode()
    target = _store(tmp_path, "r12-varint-target", "node-target")
    assert target.import_envelope(
        genesis.envelope_bytes, supplier="node-source"
    ).status == "applied"
    committed_before = target._db.execute(
        "SELECT COUNT(*) FROM commits"
    ).fetchone()[0]
    mesh = OrgFsMesh(
        _Bus().session(),
        target,
        "node-target",
        author="user:owner",
        supplier_owner={"node-source": "user:owner"}.get,
        supplier_online=lambda _node: True,
    )
    try:
        result = mesh.receive(envelope, supplier="node-source")

        assert (result.status, result.code) == ("rejected", "invalid-argument")
        assert (
            target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
            == committed_before
        )
    finally:
        mesh.close()


def test_r10_replica_prime_propagates_enospc() -> None:
    """A broken replica must not attach as if a durable write had succeeded."""

    snapshot = b"round10 snapshot"
    snapshot_id = hashlib.sha256(snapshot).hexdigest()
    envelope = json.dumps(
        {"docId": "doc-space", "origin": {"writer": "writer-space"}, "seq": 0},
        sort_keys=True,
    ).encode()

    class _PrimeSource:
        def document_ids(self):
            return ("doc-space",)

        def export_since(self, doc_id, _version, *, cursor, max_bytes):
            del doc_id, cursor, max_bytes
            return ExportPage((envelope,), None)

        def snapshot(self, doc_id, *, shallow_since):
            del doc_id, shallow_since
            return SimpleNamespace(
                snapshot_id=snapshot_id,
                snapshot_bytes=snapshot,
            )

    class _FullBackend(MemoryReplicaBackend):
        def put(self, key, data):
            del key, data
            raise OSError(errno.ENOSPC, "simulated ENOSPC")

    retirement = SimpleNamespace(
        retired=lambda _doc_id: None,
        purge_listed=lambda _digest: False,
        snapshot_point=lambda _doc_id: None,
    )
    replica = ReplicaStore("space", _FullBackend("space"), retirement)

    with pytest.raises(OSError) as raised:
        replica.prime_from(_PrimeSource())

    assert raised.value.errno == errno.ENOSPC


def test_r12_attach_replica_is_atomic_when_prime_hits_enospc(tmp_path) -> None:
    """A failed initial prime must not expose a half-attached replica."""

    source = _store(tmp_path, "r12-attach-source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )

    class _FullBackend(MemoryReplicaBackend):
        def put(self, key, data):
            del key, data
            raise OSError(errno.ENOSPC, "simulated ENOSPC")

    retirement = SimpleNamespace(
        retired=lambda _doc_id: None,
        purge_listed=lambda _digest: False,
        snapshot_point=lambda _doc_id: None,
    )
    replica = ReplicaStore("space-1", _FullBackend("space-1"), retirement)
    mesh = OrgFsMesh(
        _Bus().session(),
        source,
        "node-source",
        author="user:owner",
        durable=False,
    )
    try:
        with pytest.raises(OSError) as raised:
            mesh.attach_replica(replica)

        assert raised.value.errno == errno.ENOSPC
        assert mesh.replica_store is None
        assert mesh.durable is False
    finally:
        mesh.close()
