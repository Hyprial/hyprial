"""ROUND 8 writer, pending-supplier, and drain-efficiency acceptance."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    ReplicaStore,
    _segment,
)
from hyprial.daemon.impl.orgfs.storage.store import (
    ExportPage,
    _decode_state_vector,
    _encode_state_vector,
)

from .test_round4 import ALICE, OWNER, _receive, replicas
from .test_round6 import _pending_count, _tree_envelope

_IMPORTED_REPLICAS_FIXTURE = replicas


@pytest.fixture
def round8_replicas(request):
    assert _IMPORTED_REPLICAS_FIXTURE is not None
    return request.getfixturevalue("replicas")


def _future_meta_token(store) -> str:
    frontier = _decode_state_vector(store._meta_frontier_for_commit())
    client = next(iter(frontier))
    frontier[client] += 1
    return base64.b64encode(_encode_state_vector(frontier)).decode()


def _pending_meta_envelope(
    runtime,
    space_id: str,
    *,
    author: str,
    supplier: str,
    index: int,
) -> bytes:
    raw = _tree_envelope(
        runtime,
        space_id,
        author=author,
        actor=f"round8-{index}",
        node_id=f"round8-pending-{index}",
        parent="round8-missing-parent",
        name=f"pending-{index}.json",
        doc_id=f"doc-00000000-0000-4000-8000-{index:012d}",
    )
    value = json.loads(raw)
    value["origin"]["node"] = supplier
    value["origin"]["metaFrontier"] = _future_meta_token(
        runtime.stores[space_id]
    )
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_r8_overlong_writer_is_rejected_without_journal_or_replica_residue(
    round8_replicas,
) -> None:
    """A writer that cannot be a filesystem component never crosses ingress."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round8_replicas
    alice.serve(space_id, backend="memory")
    envelope = _tree_envelope(
        owner,
        space_id,
        author=OWNER,
        actor="overlong-writer",
        node_id="overlong-writer-child",
        parent="missing-parent",
        name="child.json",
        doc_id="doc-00000000-0000-4000-8000-000000000801",
    )
    value = json.loads(envelope)
    value["origin"]["writer"] = "w" * 300
    crafted = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    result = _receive(alice, space_id, crafted, owner.node_id)

    assert (result.status, result.code) == ("rejected", "invalid-argument")
    assert alice.stores[space_id]._db.execute(
        "SELECT 1 FROM commits WHERE writer = ?", ("w" * 300,)
    ).fetchone() is None
    assert alice.stores[space_id]._db.execute(
        "SELECT 1 FROM pending WHERE writer = ?", ("w" * 300,)
    ).fetchone() is None
    assert not any("w" * 300 in key for key in alice._replicas[space_id].backend_keys("log/"))


def test_r8_replica_segments_reserve_suffix_margin() -> None:
    """Replica components reserve 15 bytes below the 255-byte filesystem cap."""

    assert _segment("w" * 240, name="writer") == "w" * 240
    with pytest.raises(ValueError, match="safe key segment"):
        _segment("w" * 241, name="writer")


def test_r8_replica_prime_skips_enametoolong_and_continues(caplog) -> None:
    """Unstorable keys are skipped without aborting the remaining prime set."""

    snapshot = b"round8 snapshot"
    snapshot_id = hashlib.sha256(snapshot).hexdigest()

    def envelope(doc_id: str, writer: str) -> bytes:
        return json.dumps(
            {"docId": doc_id, "origin": {"writer": writer}, "seq": 0},
            sort_keys=True,
        ).encode()

    class _PrimeSource:
        def document_ids(self):
            return ("doc-mixed",)

        def export_since(self, doc_id, _version, *, cursor, max_bytes):
            del cursor, max_bytes
            return ExportPage(
                (
                    envelope(doc_id, "writer-bad-a"),
                    envelope(doc_id, "writer-bad-b"),
                    envelope(doc_id, "writer-good"),
                ),
                None,
            )

        def snapshot(self, doc_id, *, shallow_since):
            del doc_id, shallow_since
            return SimpleNamespace(snapshot_id=snapshot_id, snapshot_bytes=snapshot)

    class _OSErrorBackend(MemoryReplicaBackend):
        def put(self, key, data):
            if "writer-bad" in key:
                raise OSError(errno.ENAMETOOLONG, "simulated ENAMETOOLONG")
            return super().put(key, data)

    retirement = SimpleNamespace(
        retired=lambda _doc_id: None,
        purge_listed=lambda _digest: False,
        snapshot_point=lambda _doc_id: None,
    )
    backend = _OSErrorBackend("space")
    replica = ReplicaStore("space", backend, retirement)
    caplog.set_level("WARNING")

    replica.prime_from(_PrimeSource())

    assert backend.get("log/space/doc-mixed/writer-good/0") == envelope(
        "doc-mixed", "writer-good"
    )
    assert backend.get(f"snapshot/space/doc-mixed/{snapshot_id}") == snapshot
    assert "orgfs replica prime skipped invalid document" in caplog.text
    assert caplog.text.count("orgfs replica prime skipped invalid document") == 1


def test_r8_pending_meta_cap_is_per_supplier_not_mintable_author(
    round8_replicas,
) -> None:
    """Minted authors from one host cannot consume another supplier's budget."""

    _tmp_path, space_id, owner, alice, bob, _carol = round8_replicas
    store = alice.stores[space_id]
    store._pending_import_limit = 2
    store._pending_import_supplier_limit = 1
    forged = tuple(
        _pending_meta_envelope(
            owner,
            space_id,
            author=f"mallory{index}@example",
            supplier=bob.node_id,
            index=810 + index,
        )
        for index in range(3)
    )
    honest = _pending_meta_envelope(
        owner,
        space_id,
        author=ALICE,
        supplier=alice.node_id,
        index=820,
    )

    first = _receive(alice, space_id, forged[0], bob.node_id)
    dropped = tuple(_receive(alice, space_id, item, bob.node_id) for item in forged[1:])
    accepted = _receive(alice, space_id, honest, alice.node_id)

    assert (first.status, first.code, first.details) == ("pending", "pending-meta", None)
    assert all(item.details == {"dropped": True, "reason": "capacity"} for item in dropped)
    assert (accepted.status, accepted.code, accepted.details) == (
        "pending",
        "pending-meta",
        None,
    )
    assert _pending_count(alice, space_id) == 2
    assert store._db.execute(
        "SELECT 1 FROM pending WHERE supplier = ? AND author = ?",
        (alice.node_id, f"user:{ALICE}"),
    ).fetchone() is not None


def test_r8_uncovered_pending_meta_rows_are_not_redecoded(
    round8_replicas, monkeypatch
) -> None:
    """Drain checks the stored wait token before touching large envelope bytes."""

    _tmp_path, space_id, owner, alice, bob, _carol = round8_replicas
    store = alice.stores[space_id]
    store._pending_import_limit = 8
    store._pending_import_supplier_limit = 8
    for index in range(3):
        result = _receive(
            alice,
            space_id,
            _pending_meta_envelope(
                owner,
                space_id,
                author=f"mallory{index}@example",
                supplier=bob.node_id,
                index=830 + index,
            ),
            bob.node_id,
        )
        assert (result.status, result.code) == ("pending", "pending-meta")
    assert _pending_count(alice, space_id) == 3
    assert store._db.execute(
        "SELECT COUNT(*) FROM pending WHERE meta_frontier IS NOT NULL"
    ).fetchone()[0] == 3

    decodes = 0
    original_decode = store._decode_pending_envelope

    def counted_decode(raw):
        nonlocal decodes
        decodes += 1
        return original_decode(raw)

    monkeypatch.setattr(store, "_decode_pending_envelope", counted_decode)

    assert store._drain_pending() == ()
    assert decodes == 0
    assert _pending_count(alice, space_id) == 3
