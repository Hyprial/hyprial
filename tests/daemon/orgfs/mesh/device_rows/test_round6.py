# Decisions:
# - Phase D T1: the alias, receive-validation and pending tests run on the
#   state host (``device_rows/support``) through ``SyncHost.apply_remote``.
#   The state host keeps no pending queue (design §3.6): an update whose
#   predecessor is missing is ``Pending`` and nothing is stored, so the
#   supplier-cap and drop-warning tests now pin "waiting stores, logs and
#   counts nothing, and converges once the predecessor is pulled", plus the
#   §3.7 "a repeated refusal is logged and counted once".
# - ``test_r6_nonmember_missing_parent_update_is_rejected_not_parked`` was
#   deleted: receivers deliberately do not check a row author's current
#   membership (§3.6 "不做的校验"; the node gate does, covered by
#   ``runtime_state/test_node_gate.py``) and there is no queue to park in.
# - A row that links a protected document id outside its deterministic path
#   was refused by the old receivers; the state host accepts it, so the two
#   alias tests fail on purpose (T1 report, defect repros).
"""ROUND 6 alias, receive-validation, and pending-queue acceptance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import Accepted, Rejected
from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    ReplicaStore,
)
from hyprial.daemon.impl.orgfs.storage.store import ExportPage
from hyprial.identity import directory_device_path, protected_directory_doc_id
from hyprial.identity.impl.org_directory.models import MAX_PROTECTED_DOC_ID_LENGTH

from tests.daemon.orgfs.coordinator.support import EMPTY

from .support import ORG, Frame, Org, new_row
from .test_round4 import (
    ALICE,
    BOB,
    OWNER,
    _device,
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


@pytest.fixture
def state_org():
    value = Org()
    try:
        yield value
    finally:
        value.close()


def _state_protected_device(org: Org, device_id: str) -> tuple[str, str]:
    path = directory_device_path(OWNER, device_id)
    org.directory(OWNER).put_device(ORG, _device(OWNER, device_id, address="tcp://10.0.0.7:7447#alias"))
    org.deliver()
    doc_id = protected_directory_doc_id(org.space_id, path, space_owner=f"user:{OWNER}")
    assert doc_id is not None
    return path, doc_id


def test_r6_protected_doc_alias_link_is_rejected_on_owner_and_receivers(state_org: Org) -> None:
    """A protected physical id cannot be linked outside its deterministic path."""

    org = state_org
    _path, protected_id = _state_protected_device(org, "alias-link")
    forged = org.forge_rows(
        OWNER,
        [new_row("protected-alias", parent="root", name="public-alias.json", doc_id=protected_id)],
        author=OWNER,
    )

    verdicts = {receiver: org.apply(receiver, forged, peer="node-owner") for receiver in (OWNER, ALICE, BOB)}

    assert {receiver: isinstance(outcome, Rejected) for receiver, outcome in verdicts.items()} == {
        OWNER: True, ALICE: True, BOB: True
    }
    for receiver in (OWNER, ALICE, BOB):
        assert org.missing(receiver, "public-alias.json")


@pytest.mark.parametrize(
    "bad_doc_id",
    [
        "doc-orgdir-v3-" + "a" * MAX_PROTECTED_DOC_ID_LENGTH,
        "doc-orgdir-v3-not-canonical-0",
    ],
)
def test_r6_receiver_rejects_invalid_protected_ids_without_persisting(
    state_org: Org, bad_doc_id: str
) -> None:
    """Malformed protected content is a normal refusal, never stored."""

    org = state_org
    content = org.forge_content(None, bad_doc_id, "{}", author=OWNER)
    link = org.forge_rows(
        OWNER,
        [new_row("receive-guard", parent="root", name="receive-guard.json", doc_id=bad_doc_id)],
        author=OWNER,
    )
    host = org.fs(ALICE).sync_host(org.space_id)

    assert not isinstance(org.apply(ALICE, content, peer="node-owner"), Accepted)
    assert isinstance(org.apply(ALICE, link, peer="node-owner"), Rejected)
    assert not isinstance(org.apply(ALICE, content, peer="node-owner"), Accepted)
    assert host.read_update(bad_doc_id, EMPTY) is None
    assert org.missing(ALICE, "receive-guard.json")


def _state_missing_parent_rows(org: Org, author: str, count: int, prefix: str) -> list[Frame]:
    """Rows under a directory the receivers have not seen, one forger each."""

    parent = org.fs(OWNER).mkdir(org.space_id, f"{prefix}-parent")
    org.cluster.take()
    return [
        org.forge_rows(
            OWNER,
            [new_row(f"{prefix}-{index}", parent=parent.node_id, name=f"{prefix}-{index}.json",
                     doc_id=f"doc-00000000-0000-4000-8000-0000000001{index:02d}")],
            author=author,
        )
        for index in range(count)
    ]


def test_r6_pending_supplier_cap_preserves_budget_for_another_supplier(state_org: Org) -> None:
    """Updates waiting on a predecessor hold nothing, so no supplier crowds out another."""

    org = state_org
    bob_updates = _state_missing_parent_rows(org, BOB, 2, "bob-pending")
    alice_updates = _state_missing_parent_rows(org, ALICE, 1, "alice-pending")
    before = org.listing(ALICE)

    first = org.apply(ALICE, bob_updates[0], peer="node-bob")
    second = org.apply(ALICE, bob_updates[1], peer="node-bob")
    other = org.apply(ALICE, alice_updates[0], peer="node-alice")

    assert [outcome.__class__.__name__ for outcome in (first, second, other)] == ["Pending"] * 3
    assert org.listing(ALICE) == before
    assert org.rejected(ALICE) == 0
    assert org.cluster.pull(org[ALICE], org[OWNER]) >= 1
    for frame, peer in ((bob_updates[0], "node-bob"), (bob_updates[1], "node-bob"),
                        (alice_updates[0], "node-alice")):
        assert org.apply(ALICE, frame, peer=peer) == Accepted()
    assert {node[1] for node in org.listing(ALICE)} >= {
        "bob-pending-parent/bob-pending-0.json",
        "bob-pending-parent/bob-pending-1.json",
        "alice-pending-parent/alice-pending-0.json",
    }


def test_r6_pending_drop_warnings_are_rate_limited_with_a_count(state_org: Org) -> None:
    """A burst of waiting updates logs nothing; a repeated refusal logs and counts once."""

    org = state_org
    events = org.events[ALICE]
    for frame in _state_missing_parent_rows(org, BOB, 3, "drop-child"):
        assert not isinstance(org.apply(ALICE, frame, peer="node-owner"), (Accepted, Rejected))
    assert [event for event, _fields in events if event == "orgfs.update.rejected"] == []

    forged = org.forge_rows(
        BOB,
        [new_row("drop-squatter", parent=org.node_id(BOB, "directory"), name="squatter", kind="dir")],
        author=BOB,
    )
    for _attempt in range(3):
        assert isinstance(org.apply(ALICE, forged, peer="node-bob"), Rejected)
    assert [event for event, _fields in events if event == "orgfs.update.rejected"] == [
        "orgfs.update.rejected"
    ]
    assert org.rejected(ALICE) == 1


def test_pending_forged_alias_is_rejected_when_invite_meta_converges(state_org: Org) -> None:
    org = state_org
    _path, protected_id = _state_protected_device(org, "alias-link-pending")
    pending_author = "pending@example"
    invite = org.capture(
        OWNER, lambda: org.fs(OWNER).invite(org.space_id, f"user:{pending_author}", "rw")
    )
    assert [frame.doc_id for frame in invite] == ["meta"]
    forged = org.forge_rows(
        OWNER,
        [new_row("protected-alias-pending", parent="root", name="public-alias-pending.json",
                 doc_id=protected_id)],
        author=pending_author,
    )

    assert not isinstance(org.apply(ALICE, forged, peer="node-owner"), Accepted)
    assert org.apply(ALICE, invite[0], peer="node-owner") == Accepted()
    assert isinstance(org.apply(ALICE, forged, peer="node-owner"), Rejected)
    for public_lookup in (
        lambda: org.fs(ALICE).stat(org.space_id, "public-alias-pending.json"),
        lambda: org.fs(ALICE).read_text(org.space_id, "public-alias-pending.json"),
    ):
        with pytest.raises(OrgFsError) as missing:
            public_lookup()
        assert missing.value.code == "unknown-doc"
