"""ROUND 6 alias, receive-validation, and pending-queue acceptance."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    ReplicaStore,
)
from hyprial.daemon.impl.orgfs.storage.store import ExportPage
from hyprial.identity import directory_device_path, protected_directory_doc_id
from hyprial.identity.impl.org_directory.models import MAX_PROTECTED_DOC_ID_LENGTH

from .test_round4 import (
    ALICE,
    BOB,
    OWNER,
    _commits_since,
    _device,
    _eventually,
    _position,
    _receive,
    replicas,
)

_IMPORTED_REPLICAS_FIXTURE = replicas


@pytest.fixture
def round6_replicas(request):
    assert _IMPORTED_REPLICAS_FIXTURE is not None
    return request.getfixturevalue("replicas")


def _tree_envelope(
    runtime,
    space_id: str,
    *,
    author: str,
    actor: str,
    node_id: str,
    parent: str,
    name: str,
    kind: str = "doc",
    doc_id: str | None = None,
) -> bytes:
    store = runtime.stores[space_id]
    tree_doc_id = store.active_tree_doc_id()
    assert tree_doc_id is not None
    writer = store._writer_for(f"user:{author}", actor)
    doc = Doc(client_id=store._client_id(writer))
    doc.apply_update(store.snapshot(tree_doc_id, shallow_since=None).snapshot_bytes)
    before = doc.get_state()
    doc.get("nodes", type=Map)[node_id] = json.dumps(
        {
            "blob_hash": None,
            "deleted": False,
            "doc_id": doc_id,
            "kind": kind,
            "name": name,
            "node_id": node_id,
            "parent": parent,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    seq = int(
        store._db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
            "WHERE writer = ? AND doc_id = ?",
            (writer, tree_doc_id),
        ).fetchone()[0]
    )
    store._advance_commit_clock(doc, writer=writer, seq=seq)
    return store._make_envelope(
        tree_doc_id,
        writer,
        seq,
        doc.get_update(before),
        author=f"user:{author}",
        actor=actor,
        meta_frontier=store._meta_frontier_for_commit(),
    )


def _pending_count(runtime, space_id: str) -> int:
    return int(
        runtime.stores[space_id]
        ._db.execute("SELECT COUNT(*) FROM pending")
        .fetchone()[0]
    )


def _meta_commits(runtime, space_id: str) -> tuple[tuple[str, int, bytes], ...]:
    return tuple(
        (str(row[0]), int(row[1]), bytes(row[2]))
        for row in runtime.stores[space_id]._db.execute(
            "SELECT writer, seq, envelope_bytes FROM commits "
            "WHERE doc_id = 'meta' ORDER BY writer, seq"
        )
    )


def test_r6_protected_doc_alias_link_is_rejected_on_owner_and_receivers(
    round6_replicas,
) -> None:
    """A protected physical id cannot be linked outside its deterministic path."""

    _tmp_path, space_id, owner, alice, bob, _carol = round6_replicas
    row = _device(OWNER, "alias-link", address="tcp://10.0.0.7:7447#alias")
    OrgFsDirectoryStore(owner.facade).put_device("group-acme", row)

    def device_row_converged() -> None:
        assert all(
            OrgFsDirectoryStore(runtime.facade).list_devices("group-acme") == [row]
            for runtime in (owner, alice, bob)
        )

    _eventually(device_row_converged)
    expected_members = {f"user:{name}" for name in (OWNER, ALICE, BOB)}

    def membership_converged() -> None:
        assert all(
            expected_members
            <= {item.user for item in runtime.facade.members(space_id)}
            for runtime in (owner, alice, bob)
        )

    _eventually(membership_converged)
    protected_id = protected_directory_doc_id(
        space_id,
        directory_device_path(OWNER, row.device_id),
        space_owner=f"user:{OWNER}",
    )
    assert protected_id is not None
    forged = _tree_envelope(
        owner,
        space_id,
        author=OWNER,
        actor="alias-link",
        node_id="protected-alias",
        parent="root",
        name="public-alias.json",
        doc_id=protected_id,
    )

    meta_token = base64.b64decode(json.loads(forged)["origin"]["metaFrontier"])

    def receiver_meta_converged() -> None:
        assert all(
            _meta_commits(runtime, space_id) == _meta_commits(owner, space_id)
            and runtime.stores[space_id]._meta_frontier_for_commit() == meta_token
            and runtime.stores[space_id]._membership_at(meta_token) is not None
            for runtime in (alice, bob)
        )

    _eventually(receiver_meta_converged)
    for receiver in (owner, alice, bob):
        result = _receive(receiver, space_id, forged, owner.node_id)
        assert (result.status, result.code) == (
            "rejected",
            "not-a-member",
        ), receiver.node_id
        with pytest.raises(OrgFsError) as missing:
            receiver.facade.stat(space_id, "public-alias.json")
        assert missing.value.code == "unknown-doc"


def test_r6_purge_refuses_a_protected_doc_reached_through_an_alias(
    round6_replicas,
) -> None:
    """A pre-fix alias cannot bypass the protected-document purge fence."""

    _tmp_path, space_id, owner, _alice, _bob, _carol = round6_replicas
    row = _device(OWNER, "alias-purge", address="tcp://10.0.0.7:7447#secret")
    OrgFsDirectoryStore(owner.facade).put_device("group-acme", row)
    original = owner.facade.stat(space_id, directory_device_path(OWNER, row.device_id))
    space = owner.facade._space(space_id)
    source = space.nodes[original.node_id]
    alias = replace(
        source,
        node_id="pre-fix-protected-alias",
        parent="root",
        name="public-alias.json",
    )
    space.nodes[alias.node_id] = alias
    try:
        with pytest.raises(OrgFsError) as refused:
            owner.facade.purge_plan(
                space_id,
                [
                    {
                        "kind": "doc-range",
                        "node": f"id:{alias.node_id}",
                        "from": alias.version,
                        "to": alias.version,
                    }
                ],
            )
        assert refused.value.code == "protected-path"
        assert refused.value.details == {"path": "public-alias.json"}
        assert row.address not in str(refused.value)
    finally:
        space.nodes.pop(alias.node_id, None)


@pytest.mark.parametrize(
    "bad_doc_id",
    [
        "doc-orgdir-v3-" + "a" * MAX_PROTECTED_DOC_ID_LENGTH,
        "doc-orgdir-v3-not-canonical-0",
    ],
)
def test_r6_receiver_rejects_invalid_protected_ids_without_persisting(
    round6_replicas, bad_doc_id: str
) -> None:
    """Malformed protected content is a normal rejection, never replica residue."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round6_replicas
    alice.serve(space_id, backend="memory")
    before = _position(owner, space_id)
    OrgFsDirectoryStore(owner.facade).put_device(
        "group-acme", _device(OWNER, "receive-guard", address="tcp://10.0.0.7:7447#guard")
    )
    content = next(
        envelope
        for envelope in _commits_since(owner, space_id, before)
        if json.loads(envelope)["docId"].startswith("doc-orgdir-")
    )
    value = json.loads(content)
    value["docId"] = bad_doc_id
    crafted = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    result = _receive(alice, space_id, crafted, owner.node_id)

    assert (result.status, result.code) == ("rejected", "invalid-argument")
    assert alice.stores[space_id]._db.execute(
        "SELECT 1 FROM commits WHERE doc_id = ?", (bad_doc_id,)
    ).fetchone() is None
    assert not any(
        f"/{bad_doc_id}/" in key
        for key in alice._replicas[space_id].backend_keys("log/")
    )


def test_r6_replica_prime_skips_one_bad_document_and_keeps_the_rest(caplog) -> None:
    """One invalid journal entry cannot abort resident-replica priming."""

    bad_doc_id = "doc-orgdir-v3-" + "a" * MAX_PROTECTED_DOC_ID_LENGTH
    snapshot = b"good snapshot"
    snapshot_id = hashlib.sha256(snapshot).hexdigest()
    envelope = json.dumps(
        {
            "docId": "doc-good",
            "origin": {"writer": "writer-good"},
            "seq": 0,
        },
        sort_keys=True,
    ).encode()

    class _PrimeSource:
        def document_ids(self):
            return (bad_doc_id, "doc-good")

        def export_since(self, doc_id, _version, *, cursor, max_bytes):
            del cursor, max_bytes
            if doc_id == bad_doc_id:
                raise ValueError("invalid protected id")
            return ExportPage((envelope,), None)

        def snapshot(self, doc_id, *, shallow_since):
            del doc_id, shallow_since
            return SimpleNamespace(
                snapshot_id=snapshot_id,
                snapshot_bytes=snapshot,
            )

    retirement = SimpleNamespace(
        retired=lambda _doc_id: None,
        purge_listed=lambda _digest: False,
        snapshot_point=lambda _doc_id: None,
    )
    backend = MemoryReplicaBackend("space")
    replica = ReplicaStore("space", backend, retirement)
    caplog.set_level("WARNING")

    replica.prime_from(_PrimeSource())

    assert backend.get("log/space/doc-good/writer-good/0") == envelope
    assert backend.get(f"snapshot/space/doc-good/{snapshot_id}") == snapshot
    assert "orgfs replica prime skipped invalid document" in caplog.text
    assert bad_doc_id not in " ".join(backend.keys("log/"))


def test_r6_nonmember_missing_parent_update_is_rejected_not_parked(
    round6_replicas,
) -> None:
    """Membership is checked before a missing tree dependency can consume budget."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round6_replicas
    forged = _tree_envelope(
        owner,
        space_id,
        author="mallory@example",
        actor="nonmember",
        node_id="nonmember-child",
        parent="missing-parent",
        name="child.json",
        doc_id="doc-00000000-0000-4000-8000-000000000121",
    )

    result = _receive(alice, space_id, forged, owner.node_id)

    assert (result.status, result.code) == ("rejected", "not-a-member")
    assert _pending_count(alice, space_id) == 0


def test_r6_pending_supplier_cap_preserves_budget_for_another_supplier(
    round6_replicas,
) -> None:
    """Distinct writer lanes do not let one supplier consume the whole space cap."""

    _tmp_path, space_id, owner, alice, bob, _carol = round6_replicas
    store = alice.stores[space_id]
    store._pending_import_limit = 8
    store._pending_import_supplier_limit = 1
    bob_updates = tuple(
        _tree_envelope(
            owner,
            space_id,
            author=BOB,
            actor=f"bob-lane-{index}",
            node_id=f"bob-pending-{index}",
            parent="missing-parent",
            name=f"bob-{index}.json",
            doc_id=f"doc-00000000-0000-4000-8000-00000000013{index}",
        )
        for index in range(2)
    )
    alice_update = _tree_envelope(
        owner,
        space_id,
        author=ALICE,
        actor="alice-lane",
        node_id="alice-pending",
        parent="missing-parent",
        name="alice.json",
        doc_id="doc-00000000-0000-4000-8000-000000000140",
    )

    first = _receive(alice, space_id, bob_updates[0], bob.node_id)
    dropped = _receive(alice, space_id, bob_updates[1], bob.node_id)
    other = _receive(alice, space_id, alice_update, alice.node_id)

    assert (first.status, first.code, first.details) == (
        "pending",
        "pending-dependency",
        None,
    )
    assert dropped.details == {"dropped": True, "reason": "capacity"}
    assert (other.status, other.code, other.details) == (
        "pending",
        "pending-dependency",
        None,
    )
    assert _pending_count(alice, space_id) == 2


class _PagingConnection:
    def __init__(self, connection, page_size: int) -> None:
        self._connection = connection
        self.page_size = page_size
        self.pending_selects = 0

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT rowid AS pending_rowid, * FROM pending"):
            assert "LIMIT ?" in normalized
            assert parameters[-1] == self.page_size
            self.pending_selects += 1
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)


def test_r6_pending_drain_pages_more_rows_than_one_page(round6_replicas) -> None:
    """The pending drain never materializes its full bounded queue at once."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round6_replicas
    children = tuple(
        _tree_envelope(
            owner,
            space_id,
            author=OWNER,
            actor=f"child-lane-{index}",
            node_id=f"late-child-{index}",
            parent="late-parent",
            name=f"child-{index}.json",
            doc_id=f"doc-00000000-0000-4000-8000-00000000015{index}",
        )
        for index in range(3)
    )
    parent = _tree_envelope(
        owner,
        space_id,
        author=OWNER,
        actor="parent-lane",
        node_id="late-parent",
        parent="root",
        name="late-parent",
        kind="dir",
    )
    for envelope in children:
        assert _receive(alice, space_id, envelope, owner.node_id).status == "pending"
    assert _pending_count(alice, space_id) == 3

    store = alice.stores[space_id]
    store._pending_drain_page_size = 1
    proxy = _PagingConnection(store._db, page_size=1)
    store._db = proxy
    result = _receive(alice, space_id, parent, owner.node_id)

    assert result.status == "applied"
    assert len((result.details or {}).get("drained", ())) == 3
    assert _pending_count(alice, space_id) == 0
    assert proxy.pending_selects >= 3


def test_r6_pending_drop_warnings_are_rate_limited_with_a_count(
    round6_replicas, caplog
) -> None:
    """A burst of capacity drops emits one per-space warning with a count."""

    _tmp_path, space_id, owner, alice, _bob, _carol = round6_replicas
    store = alice.stores[space_id]
    store._pending_import_supplier_limit = 0
    caplog.set_level("WARNING")
    for index in range(3):
        envelope = _tree_envelope(
            owner,
            space_id,
            author=BOB,
            actor=f"drop-lane-{index}",
            node_id=f"drop-child-{index}",
            parent="missing-parent",
            name=f"drop-{index}.json",
            doc_id=f"doc-00000000-0000-4000-8000-00000000016{index}",
        )
        result = _receive(alice, space_id, envelope, owner.node_id)
        assert result.details == {"dropped": True, "reason": "capacity"}

    records = [
        record
        for record in caplog.records
        if "orgfs dropped pending dependency" in record.getMessage()
    ]
    assert len(records) == 1
    assert "droppedCount=" in records[0].getMessage()
    assert store._pending_drop_count == 3


def test_pending_forged_alias_is_rejected_when_invite_meta_converges(
    round6_replicas, monkeypatch
) -> None:
    _tmp_path, space_id, owner, alice, bob, _carol = round6_replicas
    row = _device(OWNER, "alias-link-pending", address="tcp://10.0.0.7:7447#alias")
    OrgFsDirectoryStore(owner.facade).put_device("group-acme", row)

    def device_row_converged() -> None:
        assert OrgFsDirectoryStore(alice.facade).list_devices("group-acme") == [row]

    _eventually(device_row_converged)
    baseline_meta = owner.stores[space_id]._meta_frontier_for_commit()

    def baseline_meta_converged() -> None:
        assert all(
            _meta_commits(runtime, space_id) == _meta_commits(owner, space_id)
            and runtime.stores[space_id]._meta_frontier_for_commit() == baseline_meta
            and runtime.stores[space_id]._membership_at(baseline_meta) is not None
            for runtime in (alice, bob)
        )

    _eventually(baseline_meta_converged)

    def owner_outbox_drained() -> None:
        assert not tuple(owner.stores[space_id].unbroadcast())

    _eventually(owner_outbox_drained)
    owner_mesh = owner._meshes[space_id]
    original_broadcast = owner_mesh.broadcast_pending
    monkeypatch.setattr(owner_mesh, "broadcast_pending", lambda _space_id=None: 0)
    before = _position(owner, space_id)
    pending_author = "pending@example"
    owner.facade.invite(space_id, f"user:{pending_author}", "rw")
    invite_meta = tuple(
        envelope
        for envelope in _commits_since(owner, space_id, before)
        if json.loads(envelope)["docId"] == "meta"
    )
    assert len(invite_meta) == 1

    protected_id = protected_directory_doc_id(
        space_id,
        directory_device_path(OWNER, row.device_id),
        space_owner=f"user:{OWNER}",
    )
    assert protected_id is not None
    forged = _tree_envelope(
        owner,
        space_id,
        author=pending_author,
        actor="alias-link-pending",
        node_id="protected-alias-pending",
        parent="root",
        name="public-alias-pending.json",
        doc_id=protected_id,
    )
    forged_value = json.loads(forged)
    receiver_store = alice.stores[space_id]
    decisions: list[str] = []
    original_admission = receiver_store.admission

    def observed_admission(envelope: bytes):
        decision = original_admission(envelope)
        if envelope == forged:
            decisions.append(decision)
        return decision

    monkeypatch.setattr(receiver_store, "admission", observed_admission)
    pending = _receive(alice, space_id, forged, owner.node_id)
    assert (pending.status, pending.code) == ("pending", "pending-meta")
    assert _pending_count(alice, space_id) == 1

    released = _receive(alice, space_id, invite_meta[0], owner.node_id)
    assert released.status == "applied"
    assert decisions == ["pending-meta", "not-a-member"]
    assert _pending_count(alice, space_id) == 0
    assert receiver_store._db.execute(
        "SELECT 1 FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
        (
            forged_value["origin"]["writer"],
            forged_value["seq"],
            forged_value["docId"],
        ),
    ).fetchone() is None
    for public_lookup in (
        lambda: alice.facade.stat(space_id, "public-alias-pending.json"),
        lambda: alice.facade.read_text(space_id, "public-alias-pending.json"),
    ):
        with pytest.raises(OrgFsError) as missing:
            public_lookup()
        assert missing.value.code == "unknown-doc"

    monkeypatch.setattr(owner_mesh, "broadcast_pending", original_broadcast)
    owner.broadcast_pending(space_id)
