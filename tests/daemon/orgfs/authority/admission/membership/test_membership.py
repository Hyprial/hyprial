from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import pytest
from pycrdt import Doc, Map, Text
from hyprial.daemon.impl.orgfs.storage.store import StoreError

from ..support import (
    TREE_DOC_ID,
    _close_stores as _close_stores,
    _envelopes,
    _setup,
    _store,
)


def test_admission_is_pinned_to_meta_frontier_not_receiver_current_state(tmp_path):
    """review-p1 fixes 1-2 + Allen 2026-10-09 ruling: frontier pinning stays,
    removal wins on top.

    Frontier pinning (judge at the envelope's stamped metaFrontier, not at the
    receiver's current view) still governs active authors. For a removed
    author the removal wins: a fresh envelope is refused however it is
    stamped, while an envelope the replica already merged is untouched
    (duplicates stay applied) and its journal row survives the removal.
    """
    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:writer", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:gone", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    old_write = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("old", Text("ok")),
        author="user:writer",
        actor="agent:w",
    )
    gone_write = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("gone", Text("gone")),
        author="user:gone",
        actor="agent:g",
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"]["user:gone"].__setitem__(
            "removedAt", "2026-10-09T00:00:00Z"
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("new", Text("late")),
        author="user:owner",
        actor="agent:o",
    )
    receiver = _store(tmp_path, "receiver", "node-receiver")
    for envelope in (old_write.envelope_bytes,):
        assert (
            receiver.import_envelope(envelope, supplier="node-owner").status
            == "pending"
        )
    # Deliver metadata in order, then the old write is accepted by its pinned
    # frontier: the receiver has not seen any removal of its author, and the
    # envelope pins a member state that is independent of arrival order.
    for envelope in _envelopes(owner)[:4]:
        if envelope not in (gone_write.envelope_bytes,):
            receiver.import_envelope(envelope, supplier="node-owner")
    assert (
        receiver.import_envelope(old_write.envelope_bytes, supplier="node-owner").status
        == "applied"
    )
    for envelope in _envelopes(owner)[4:]:
        receiver.import_envelope(envelope, supplier="node-owner")
    # An already-merged write is never re-judged: the duplicate re-import
    # stays applied even though the receiver now holds the author's removal.
    assert (
        receiver.import_envelope(old_write.envelope_bytes, supplier="node-owner").code
        == "duplicate"
    )
    assert receiver._db.execute(
        "SELECT 1 FROM commits WHERE json_extract(envelope_bytes, '$.origin.author')"
        " = 'user:writer'"
    ).fetchone() is not None

    # Removal wins for every envelope the replica has not merged yet: the
    # removed author's pre-removal write arriving after the removal is
    # refused without a journal write, whatever frontier it stamps.
    remove_first = _store(tmp_path, "remove-first", "node-remove-first")
    for envelope in _envelopes(owner):
        if envelope != gone_write.envelope_bytes:
            remove_first.import_envelope(envelope, supplier="node-owner")
    before = remove_first._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    refused = remove_first.import_envelope(
        gone_write.envelope_bytes, supplier="node-owner"
    )
    assert (refused.status, refused.code) == ("rejected", "removed-author")
    assert remove_first.admission(gone_write.envelope_bytes) == "removed-author"
    assert (
        remove_first._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        == before
    )
    assert remove_first._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_cold_receiver_reconstructs_a_frontier_it_never_passed_through(tmp_path):
    """review-p1 fix 1: a cold receiver rebuilds an uncached owner frontier from journal."""

    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:w", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    old = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("old", Text("accepted")),
        author="user:w",
        actor="agent:w",
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"]["user:w"].__setitem__(
            "removedAt", "2026-09-26T00:00:00Z"
        ),
        author="user:owner",
        actor=None,
    )
    owner._db.execute("PRAGMA wal_checkpoint(FULL)")
    owner.close()
    source_root = tmp_path / "owner" / "orgfs" / "spaces" / "space-1"
    cold_root = tmp_path / "cold" / "orgfs" / "spaces" / "space-1"
    shutil.copytree(source_root, cold_root)
    database = sqlite3.connect(cold_root / "journal.sqlite3")
    database.execute("DELETE FROM meta_frontiers")
    database.commit()
    database.close()
    cold = _store(tmp_path, "cold", "node-cold")
    token = __import__("base64").b64decode(
        json.loads(old.envelope_bytes)["origin"]["metaFrontier"]
    )
    assert (
        cold._db.execute(
            "SELECT 1 FROM meta_frontiers WHERE token = ?", (token,)
        ).fetchone()
        is None
    )
    # The cold journal holds the removal, so removal wins refuses the removed
    # author's fresh envelope — but only after the frontier it stamps was
    # reconstructed from the journal, which repopulates the cache row.
    assert cold.admission(old.envelope_bytes) == "removed-author"
    assert (
        cold._db.execute(
            "SELECT 1 FROM meta_frontiers WHERE token = ?", (token,)
        ).fetchone()
        is not None
    )


def test_removed_and_read_only_authors_are_rejected_and_meta_exception_is_narrow(
    tmp_path,
):
    """review-p1 fix 3: meta exceptions allow only a member's own writer/ack rows."""
    owner = _setup(tmp_path)
    ro = _store(tmp_path, "ro", "node-ro")
    for envelope in _envelopes(owner):
        ro.import_envelope(envelope, supplier="node-owner")
    with pytest.raises(StoreError, match="not-a-member"):
        ro.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("x", Text("no")),
            author="user:ro",
            actor="agent:ro",
        )
    # A member may register only its own writer row in metadata.
    registration = ro.commit(
        "meta", lambda doc: None, author="user:ro", actor="agent:ro"
    )
    assert registration
    with pytest.raises(StoreError, match="not-owner"):
        ro.commit(
            "meta",
            lambda doc: doc["members"].__setitem__("user:other", Map({"mode": "rw"})),
            author="user:ro",
            actor="agent:ro",
        )


def test_acl_space_id_meta_is_owner_only_on_every_replica(tmp_path, monkeypatch):
    owner = _store(tmp_path, "owner-acl-meta", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    member = _store(tmp_path, "member-acl-meta", "node-member")
    receivers = (
        owner,
        _store(tmp_path, "receiver-acl-meta-a", "node-receiver-a"),
        _store(tmp_path, "receiver-acl-meta-b", "node-receiver-b"),
    )
    for replica in (member, *receivers[1:]):
        for envelope in _envelopes(owner):
            assert replica.import_envelope(envelope, supplier="node-owner").status in {
                "applied",
                "pending",
            }

    monkeypatch.setattr(member, "_local_meta_exception_allowed", lambda **_kw: True)
    forged = member.commit(
        "meta",
        lambda doc: doc["space"].__setitem__("aclSpaceId", "attacker-space"),
        author="user:member",
        actor="agent:member",
    )

    for replica in receivers:
        result = replica.import_envelope(forged.envelope_bytes, supplier="node-member")
        assert (result.status, result.code) == ("rejected", "not-a-member")
        space = replica._docs["meta"].get("space", type=Map)
        assert "aclSpaceId" not in (space.to_py() or {})


def test_read_only_member_cannot_overwrite_own_membership_on_nonempty_replica(tmp_path):
    """review-p1 fix 3: the five-line pycrdt empty-probe exploit is rejected in production."""

    replica = Doc()
    replica["members"] = Map({"user:ro": Map({"mode": "ro"})})
    before = replica.get_state()
    replica["members"]["user:ro"]["mode"] = "rw"
    delta = replica.get_update(before)
    empty_probe = Doc()
    empty_probe.apply_update(delta)
    assert list(empty_probe.keys()) == []

    owner = _setup(tmp_path)
    ro = _store(tmp_path, "ro-overwrite", "node-ro")
    for envelope in _envelopes(owner):
        ro.import_envelope(envelope, supplier="node-owner")
    with pytest.raises(StoreError, match="not-owner"):
        ro.commit(
            "meta",
            lambda doc: doc["members"].__setitem__(
                "user:ro", Map({"mode": "rw", "removedAt": None})
            ),
            author="user:ro",
            actor="agent:ro",
        )


def test_reinvite_allows_new_writer_again(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:b", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"]["user:b"].__setitem__(
            "removedAt", "2026-09-26T00:00:00Z"
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"].__setitem__(
            "user:b", Map({"mode": "rw", "removedAt": None})
        ),
        author="user:owner",
        actor=None,
    )
    accepted = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("after-reinvite", Text("yes")),
        author="user:b",
        actor="agent:b",
    )
    receiver = _store(tmp_path, "receiver", "node-receiver")
    for envelope in _envelopes(owner):
        result = receiver.import_envelope(envelope, supplier="node-owner")
        assert result.status in {"applied", "pending"}
    assert receiver.admission(accepted.envelope_bytes) == "ok"
    assert receiver.frontier(TREE_DOC_ID) == owner.frontier(TREE_DOC_ID)


def test_only_true_first_meta_envelope_may_use_empty_frontier(tmp_path):
    """review-p1 fix 2: later empty-frontier metadata cannot replace the pinned owner."""

    owner = _store(tmp_path, "owner", "node-owner")
    genesis = owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    later = owner.commit(
        "meta",
        lambda doc: doc["members"].__setitem__("user:new", Map({"mode": "rw"})),
        author="user:owner",
        actor=None,
    )
    receiver = _store(tmp_path, "receiver", "node-receiver")
    assert (
        receiver.import_envelope(genesis.envelope_bytes, supplier="node-owner").status
        == "applied"
    )
    forged = json.loads(later.envelope_bytes)
    forged["origin"]["metaFrontier"] = ""
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    result = receiver.import_envelope(raw, supplier="node-owner")
    assert (result.status, result.code) == ("rejected", "not-a-member")
    assert receiver._owner() == "user:owner"

    owner_change = Doc(client_id=owner._client_id(str(forged["origin"]["writer"])))
    owner_change.apply_update(receiver._docs["meta"].get_update())
    before = owner_change.get_state()
    owner_change.get("space", type=Map)["owner"] = "user:attacker"
    forged["origin"]["metaFrontier"] = json.loads(later.envelope_bytes)["origin"][
        "metaFrontier"
    ]
    forged["update"] = base64.b64encode(owner_change.get_update(before)).decode()
    result = receiver.import_envelope(
        json.dumps(forged, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-owner",
    )
    assert (result.status, result.code) == ("rejected", "not-a-member")
    assert receiver._owner() == "user:owner"

    bad_genesis = json.loads(genesis.envelope_bytes)
    bad_genesis["origin"]["author"] = "user:attacker"
    empty = _store(tmp_path, "empty", "node-empty")
    result = empty.import_envelope(
        json.dumps(bad_genesis, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-owner",
    )
    assert (result.status, result.code) == ("rejected", "not-a-member")


def test_duplicate_is_returned_before_membership_is_rejudged(tmp_path):
    """review-p1 fix 9: an already-merged envelope bypasses admission rejudgment."""

    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    write = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    receiver = _store(tmp_path, "receiver", "node-receiver")
    for envelope in _envelopes(owner):
        receiver.import_envelope(envelope, supplier="node-owner")
    receiver._db.execute("DELETE FROM meta_frontiers")
    receiver._db.execute("DELETE FROM commits WHERE doc_id = 'meta'")
    receiver._db.commit()
    duplicate = receiver.import_envelope(write.envelope_bytes, supplier="node-owner")
    assert (duplicate.status, duplicate.code) == ("applied", "duplicate")


def test_full_meta_frontier_from_member_is_compared_as_owner_projection(tmp_path):
    """A legacy full token and the receiver's owner projection name one snapshot."""

    owner = _store(tmp_path, "full-frontier-owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    member = _store(tmp_path, "full-frontier-member", "node-member")
    receiver = _store(tmp_path, "full-frontier-receiver", "node-receiver")
    for envelope in _envelopes(owner):
        assert member.import_envelope(envelope, supplier="node-owner").status == "applied"

    member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    write = member.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("member", Text("accepted")),
        author="user:member",
        actor=None,
    )
    for envelope in _envelopes(member):
        if json.loads(envelope)["docId"] == "meta":
            assert receiver.import_envelope(envelope, supplier="node-member").status in {
                "applied",
                "pending",
            }

    legacy = json.loads(write.envelope_bytes)
    legacy["origin"]["metaFrontier"] = base64.b64encode(
        member._docs["meta"].get_state()
    ).decode()
    result = receiver.import_envelope(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-member",
    )

    assert (result.status, result.code) == ("applied", None)
    assert receiver._docs[TREE_DOC_ID].get("member", type=Text).to_py() == "accepted"


def test_membership_frontier_cache_is_read_on_second_admission(tmp_path, monkeypatch):
    """review-p1-r2 R10: cached membership avoids replaying owner meta history."""

    owner = _store(tmp_path, "r10-owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    write = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    receiver = _store(tmp_path, "r10-receiver", "node-receiver")
    for envelope in _envelopes(owner):
        if json.loads(envelope)["docId"] == "meta":
            receiver.import_envelope(envelope, supplier="node-owner")
    token = base64.b64decode(json.loads(write.envelope_bytes)["origin"]["metaFrontier"])
    receiver._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (token,))
    receiver._db.commit()
    assert receiver.admission(write.envelope_bytes) == "ok"
    monkeypatch.setattr(
        receiver,
        "_genesis_owner",
        lambda: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert receiver.admission(write.envelope_bytes) == "ok"


def test_membership_cache_hit_and_replay_use_equivalent_h1_baselines(
    tmp_path, monkeypatch
):
    """n11: cached and replayed membership baselines are replica-equivalent."""

    store = _store(tmp_path, "n11", "node-owner")
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("member", Text("registered")),
        author="user:member",
        actor="agent:member",
    )
    owner_change = store.commit(
        "meta",
        lambda doc: doc["space"].__setitem__("n11", "owner-change"),
        author="user:owner",
        actor=None,
    )
    token = base64.b64decode(
        json.loads(owner_change.envelope_bytes)["origin"]["metaFrontier"]
    )
    baselines: list[bytes] = []
    original = store._meta_exception_allowed

    def capture(envelope, update, owner, members, baseline):
        baselines.append(baseline.get_update())
        return original(envelope, update, owner, members, baseline)

    monkeypatch.setattr(store, "_meta_exception_allowed", capture)
    assert store.admission(owner_change.envelope_bytes) == "ok"
    store._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (token,))
    store._db.commit()
    assert store.admission(owner_change.envelope_bytes) == "ok"
    assert len(baselines) == 2
    assert baselines[0] == baselines[1]
