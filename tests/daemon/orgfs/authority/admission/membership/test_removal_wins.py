"""Removal-wins acceptance (Allen 2026-10-09 ruling, 0.5.5 blocker).

Store-level R2: an envelope authored by X is admitted only if X is a member
at the envelope's stamped metaFrontier AND this replica holds no removal of X
that the envelope is concurrent with or later than.  Whoever relays the
envelope is irrelevant at this layer; the mesh-level supplier gate (R1) is
covered by the mesh tests.  Re-adding X opens a new epoch: only frontiers
that cover the re-add are inside it.

The frozen acceptance set (owner-side observation only):
  1. Act 2 repro — a removed member's post-removal writes import nothing.
  2. Relay — a third member's supplier identity cannot smuggle them in.
  3. Forgery — (a) a pre-removal frontier stamp is refused; (b) an active
     member's future frontier keeps the pending-meta semantics.
  4. Content grid — modify-other / blind-overwrite / modify-own-old / delete
     / rename-move / new-doc control all refused for the removed author with
     zero owner-side content change; the stale tree-doc cell stays
     ``unknown-doc``.
  5. Re-add epoch — envelopes carrying the re-add frontier import again.
  6. update-existing-doc — the same refusal on an existing document.
"""

from __future__ import annotations

import base64
import json

from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _decode_state_vector,
    _encode_state_vector,
    _state_covers,
)

from ..support import (
    TREE_DOC_ID,
    _close_stores as _close_stores,
    _envelopes,
    _store,
)

DOC_BOB = "doc-00000000-0000-0000-0000-0000000b0b01"
DOC_BOB_ALT = "doc-00000000-0000-0000-0000-0000000b0b02"
DOC_CAROL = "doc-00000000-0000-0000-0000-000000ca4001"
OWNER, BOB, CAROL = "user:owner", "user:bob", "user:carol"


def _genesis(store, *, bob_mode="rw"):
    return store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", OWNER),
            doc["members"].__setitem__(OWNER, Map({"mode": "rw"})),
            doc["members"].__setitem__(BOB, Map({"mode": bob_mode})),
            doc["members"].__setitem__(CAROL, Map({"mode": "rw"})),
        ),
        author=OWNER,
        actor=None,
    )


def _remove(store, user, *, stamp="2026-10-09T00:00:00Z"):
    return store.commit(
        "meta",
        lambda doc: doc["members"][user].__setitem__("removedAt", stamp),
        author=OWNER,
        actor=None,
    )


def _readd(store, user):
    return store.commit(
        "meta",
        lambda doc: doc["members"].__setitem__(
            user, Map({"mode": "rw", "addedBy": OWNER, "removedAt": None})
        ),
        author=OWNER,
        actor=None,
    )


def _reseat(envelope: bytes, meta_frontier_b64: str) -> bytes:
    """Test-side envelope constructor: re-stamp origin.metaFrontier only."""

    value = json.loads(envelope)
    value["origin"]["metaFrontier"] = meta_frontier_b64
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _frontier_of(record) -> str:
    return json.loads(record.envelope_bytes)["origin"]["metaFrontier"]


def _sync(source, target, *, skip=()):
    for envelope in _envelopes(source):
        if envelope not in skip:
            target.import_envelope(envelope, supplier="node-owner")


def _content(store, doc_id: str, key: str):
    if doc_id not in store._docs:
        return None
    try:
        return store._docs[doc_id].get(key, type=Text).to_py()
    except Exception:  # noqa: BLE001 - assertion-side read only
        return "<unreadable>"


def _commit_count(store, doc_id: str) -> int:
    return int(
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]
    )


def _pending_count(store) -> int:
    return int(store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])


# ---------------------------------------------------------------------------
# 1 + 2 + 3a: the defect's own shape, its relay variant, and the forged stamp.


def test_removed_member_post_removal_write_is_refused_whoever_supplies(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    _genesis(owner)
    _sync(owner, bob)

    _remove(owner, BOB)
    marker = "bob-write-after-removal"
    bob_write = bob.commit(
        DOC_BOB,
        lambda doc: doc.get("bobWrite", type=Text).__iadd__(marker),
        author=BOB,
        actor="agent:bob",
    )
    # Bob's node never saw the removal, so the natural stamp already carries
    # the pre-removal frontier: this is the Act 2 defect shape as observed.
    natural_frontier = _frontier_of(bob_write)
    assert natural_frontier

    for supplier in ("node-bob", "node-carol"):
        result = owner.import_envelope(
            bob_write.envelope_bytes, supplier=supplier
        )
        assert (result.status, result.code) == ("rejected", "removed-author"), (
            supplier,
            result,
        )
    assert owner.admission(bob_write.envelope_bytes) == "removed-author"
    assert _commit_count(owner, DOC_BOB) == 0
    assert _pending_count(owner) == 0
    assert _content(owner, DOC_BOB, "bobWrite") is None


def test_forged_pre_removal_frontier_is_refused_future_frontier_stays_pending(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    carol = _store(tmp_path, "carol", "node-carol")
    _genesis(owner)
    _sync(owner, bob)
    _sync(owner, carol)

    pre_removal = owner._meta_frontier_for_commit()
    _remove(owner, BOB)
    _sync(owner, carol)

    # (a) bob's node never saw the removal, so its natural stamp already is
    # the pre-removal frontier (e2e r3-act2-forge: testStampEqualsNatural).
    # Re-stamping that same frontier is exactly the forged shape: it must be
    # refused even though it is a genuine "before the removal" state.
    bob_write = bob.commit(
        DOC_BOB,
        lambda doc: doc.get("bobWrite", type=Text).__iadd__("forged-stale"),
        author=BOB,
        actor="agent:bob",
    )
    pre_removal_stamp = base64.b64encode(pre_removal).decode()
    forged = _reseat(bob_write.envelope_bytes, pre_removal_stamp)
    refused = owner.import_envelope(forged, supplier="node-carol")
    assert (refused.status, refused.code) == ("rejected", "removed-author")
    assert _commit_count(owner, DOC_BOB) == 0
    assert _pending_count(owner) == 0

    # (b) an active member stamping a future frontier keeps pending-meta:
    # parked, never early-admitted, exactly as before the fix.
    carol_write = carol.commit(
        DOC_CAROL,
        lambda doc: doc.get("carolWrite", type=Text).__iadd__("future"),
        author=CAROL,
        actor="agent:carol",
    )
    current = base64.b64decode(_frontier_of(carol_write))
    future = _encode_state_vector(
        {client: clock + 7 for client, clock in _decode_state_vector(current).items()}
    )
    future_write = _reseat(
        carol_write.envelope_bytes, base64.b64encode(future).decode()
    )
    result = owner.import_envelope(future_write, supplier="node-carol")
    assert (result.status, result.code) == ("pending", "pending-meta")
    assert owner.admission(future_write) == "pending-meta"
    assert _commit_count(owner, DOC_CAROL) == 0
    assert _pending_count(owner) == 1


# ---------------------------------------------------------------------------
# 4 + 6: the content grid and the update-existing-doc path.


def test_content_grid_for_removed_author_is_refused_with_zero_owner_change(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    _genesis(owner)

    keep = owner.commit(
        DOC_CAROL,
        lambda doc: doc.get("keep", type=Text).__iadd__("owner-kept"),
        author=OWNER,
        actor=None,
    )
    bob_old = owner.commit(
        DOC_BOB,
        lambda doc: doc.get("bobOld", type=Text).__iadd__("bob-pre"),
        author=BOB,
        actor="agent:bob",
    )
    _sync(owner, bob)
    _remove(owner, BOB)

    # Bob's stale node keeps writing through the facade-shaped paths: each
    # cell is one envelope relayed to the owner by a still-current member.
    def bob_writes(doc_id, mutate):
        return bob.commit(doc_id, mutate, author=BOB, actor="agent:bob")

    modify_other = bob_writes(
        DOC_CAROL, lambda doc: doc.get("keep", type=Text).__iadd__("bob-tamper")
    )
    blind = bob_writes(
        DOC_BOB_ALT,
        lambda doc: doc.get("blind", type=Text).__iadd__("bob-blind"),
    )
    own_old = bob_writes(
        DOC_BOB, lambda doc: doc.get("bobOld", type=Text).__iadd__("bob-more")
    )
    delete_cell = bob_writes(
        DOC_BOB,
        lambda doc: doc.get("bobOld", type=Text).clear(),
    )
    stale_tree = bob_writes(
        "tree-00000000-0000-0000-0000-00000000dead",
        lambda doc: doc.get("x", type=Text).__iadd__("stale-tree"),
    )
    control = owner.commit(
        "doc-00000000-0000-0000-0000-000000ca4002",
        lambda doc: doc.get("control", type=Text).__iadd__("carol-new"),
        author=CAROL,
        actor="agent:carol",
    )
    # Rename/move is a tree-doc mutation on the stale node's known tree.
    rename_move = bob_writes(
        TREE_DOC_ID,
        lambda doc: doc.get("nodes", type=Map).__setitem__(
            "bob-forged-child",
            json.dumps(
                {
                    "blob_hash": None,
                    "deleted": False,
                    "doc_id": "doc-00000000-0000-0000-0000-0000000b0b03",
                    "kind": "doc",
                    "name": "bob-forged-child.md",
                    "node_id": "bob-forged-child",
                    "parent": "root",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )

    for envelope in (
        modify_other.envelope_bytes,
        blind.envelope_bytes,
        own_old.envelope_bytes,
        delete_cell.envelope_bytes,
        rename_move.envelope_bytes,
    ):
        result = owner.import_envelope(envelope, supplier="node-carol")
        assert (result.status, result.code) == ("rejected", "removed-author"), (
            result,
            envelope[:60],
        )

    # The stale tree cell keeps its pre-existing unknown-doc verdict, which
    # stays ahead of every author judgment.
    stale_result = owner.import_envelope(stale_tree.envelope_bytes, supplier="node-carol")
    assert (stale_result.status, stale_result.code) == ("rejected", "unknown-doc")

    # Owner-side content is untouched, nothing parked, nothing journaled.
    assert _content(owner, DOC_CAROL, "keep") == "owner-kept"
    assert _content(owner, DOC_BOB, "bobOld") == "bob-pre"
    assert _commit_count(owner, DOC_BOB) == 1
    assert _pending_count(owner) == 0

    # Control: a current member's new document still imports.
    applied = owner.import_envelope(control.envelope_bytes, supplier="node-carol")
    assert applied.status == "applied"
    assert keep.envelope_bytes in _envelopes(owner)
    assert bob_old.envelope_bytes in _envelopes(owner)


def test_update_existing_doc_path_refused_for_removed_author(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    _genesis(owner)
    owner.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("v1"),
        author=BOB,
        actor="agent:bob",
    )
    _sync(owner, bob)
    _remove(owner, BOB)

    update = bob.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("v2"),
        author=BOB,
        actor="agent:bob",
    )
    result = owner.import_envelope(update.envelope_bytes, supplier="node-carol")
    assert (result.status, result.code) == ("rejected", "removed-author")
    assert _content(owner, DOC_BOB, "v") == "v1"
    assert _commit_count(owner, DOC_BOB) == 1


# ---------------------------------------------------------------------------
# 5: the re-add epoch.


def test_readd_epoch_restores_only_frontiers_covering_the_readd(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    _genesis(owner)
    # A document the owner already holds gives bob's post-re-add write a
    # predecessor the owner can integrate, so the author judgment alone
    # decides its fate.
    owner.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("v1"),
        author=OWNER,
        actor=None,
    )
    _sync(owner, bob)
    _remove(owner, BOB)

    # The stale-epoch write lives in its own document so it cannot leave a
    # structural gap behind the fresh write the owner must integrate.
    stale_write = bob.commit(
        DOC_BOB_ALT,
        lambda doc: doc.get("v", type=Text).__iadd__("stale-epoch"),
        author=BOB,
        actor="agent:bob",
    )

    _readd(owner, BOB)
    _sync(owner, bob)

    # The stale-frontier envelope stays outside the new epoch even though
    # bob is a member again at the owner.
    refused = owner.import_envelope(stale_write.envelope_bytes, supplier="node-carol")
    assert (refused.status, refused.code) == ("rejected", "removed-author")
    assert _commit_count(owner, DOC_BOB_ALT) == 0

    # A write stamped after the re-add (bob's node has synced it) is inside.
    fresh = bob.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("new-epoch"),
        author=BOB,
        actor="agent:bob",
    )
    fresh_token = base64.b64decode(_frontier_of(fresh))
    readd_floor = owner.member_latest_add_frontier(BOB)
    assert readd_floor is not None and _state_covers(fresh_token, readd_floor)
    applied = owner.import_envelope(fresh.envelope_bytes, supplier="node-carol")
    assert applied.status == "applied"
    assert _content(owner, DOC_BOB, "v") == "v1new-epoch"


def test_merged_write_survives_later_removal_as_duplicate(tmp_path):
    """Already-merged writes are never re-judged (ruling: merged writes stay)."""

    owner = _store(tmp_path, "owner", "node-owner")
    _genesis(owner)
    merged = owner.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("merged"),
        author=BOB,
        actor="agent:bob",
    )
    _remove(owner, BOB)
    replay = owner.import_envelope(merged.envelope_bytes, supplier="node-carol")
    assert (replay.status, replay.code) == ("applied", "duplicate")
    assert _content(owner, DOC_BOB, "v") == "merged"
    assert _commit_count(owner, DOC_BOB) == 1


def test_relay_restamped_with_owner_post_removal_frontier_is_refused(tmp_path):
    """A relay cannot smuggle a removed author in by re-stamping a frontier
    the owner can already resolve (the removal's own frontier).  The author
    judgment, not the stamp's resolvability, decides; nothing is parked."""

    owner = _store(tmp_path, "owner", "node-owner")
    bob = _store(tmp_path, "bob", "node-bob")
    _genesis(owner)
    _sync(owner, bob)
    _remove(owner, BOB)

    post_removal = base64.b64encode(owner._meta_frontier_for_commit()).decode()
    bob_write = bob.commit(
        DOC_BOB,
        lambda doc: doc.get("v", type=Text).__iadd__("relayed"),
        author=BOB,
        actor="agent:bob",
    )
    relayed = _reseat(bob_write.envelope_bytes, post_removal)
    result = owner.import_envelope(relayed, supplier="node-carol")
    assert (result.status, result.code) == ("rejected", "removed-author")
    assert owner.admission(relayed) == "removed-author"
    assert _pending_count(owner) == 0
    assert _commit_count(owner, DOC_BOB) == 0

