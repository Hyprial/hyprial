from __future__ import annotations

import json

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore
from hyprial.daemon.impl.orgfs.storage.store import membership as membership_module
from hyprial.daemon.impl.orgfs.storage.store import snapshots as snapshots_module
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _encode_state_vector
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _MembershipBudgetExceeded
from tests.daemon.orgfs.storage.store.pending_replay.test_pending_replay import (
    _membership_at_reference,
)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    opened = []

    def create(name):
        store = FileSpaceStore(tmp_path / name, "space-1", node_id=name)
        opened.append(store)
        return store

    yield create
    for store in reversed(opened):
        store.close()


def genesis(store):
    return store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )


def owner_change(store, value):
    return store.commit(
        "meta",
        lambda doc: doc["members"].__setitem__(
            "user:changing", Map({"mode": "rw", "value": str(value)})
        ),
        author="user:owner",
        actor=None,
    )


def cached(store, token):
    return store._db.execute(
        "SELECT membership_json, meta_update FROM meta_frontiers WHERE token = ?",
        (token,),
    ).fetchone()


def test_owner_append_preserves_uncovered_cached_tokens(stores, monkeypatch):
    store = stores("owner")
    genesis(store)
    tokens = []
    for value in range(20):
        tokens.append(store._meta_frontier_for_commit())
        owner_change(store, value)
    for token in tokens:
        assert store._membership_at(token) is not None
    before = {token: tuple(cached(store, token)) for token in tokens}

    owner_change(store, "first")
    owner_change(store, "second")

    assert all(cached(store, token) is not None for token in tokens)
    monkeypatch.setattr(
        store, "_genesis_owner", lambda: pytest.fail("uncovered cache was replayed")
    )
    for token in tokens:
        assert store._membership_at(token) is not None
        assert tuple(cached(store, token)) == before[token]


def test_member_suffix_invalidates_old_and_current_owner_tokens(stores):
    owner = stores("owner")
    birth = genesis(owner)
    old_token = owner._meta_frontier_for_commit()
    member = stores("member")
    assert member.import_envelope(birth.envelope_bytes, supplier="owner").status == "applied"
    owner_change(owner, "later")
    current_token = owner._meta_frontier_for_commit()
    assert current_token != old_token
    assert owner._membership_at(old_token) is not None
    old_update = bytes(cached(owner, old_token)["meta_update"])

    member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    records = member._db.execute(
        "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
    ).fetchall()
    for row in records[1:]:
        result = owner.import_envelope(bytes(row[0]), supplier="member")
        assert result.status == "applied", (result.code, result.details)

    assert owner._meta_frontier_for_commit() == current_token
    assert cached(owner, old_token) is None
    assert owner._membership_at(old_token) is not None
    assert bytes(cached(owner, old_token)["meta_update"]) != old_update
    before = tuple(cached(owner, old_token))
    with owner._db:
        owner._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (old_token,))
    assert owner._membership_at(old_token) is not None
    assert tuple(cached(owner, old_token)) == before


@pytest.mark.parametrize("aged", [False, True])
def test_one_pending_replay_yields_inside_history_and_resumes(stores, monkeypatch, aged):
    store = stores("owner")
    genesis(store)
    for value in range(35):
        owner_change(store, value)
    client = next(iter(store._owner_client_ids("user:owner")))
    token = _encode_state_vector({client: 100_000})
    with store._db:
        store._db.execute(
            "INSERT INTO pending(writer, seq, doc_id, author, meta_frontier, "
            "envelope_bytes, supplier, held_at) VALUES (?, 0, 'doc-x', ?, ?, ?, ?, ?)",
            ("missing", "user:member", token, b"invalid", "member", "2000-01-01" if aged else "2100-01-01"),
        )
    clock = {"now": 0.0, "applies": 0}
    real_doc = membership_module.Doc

    class TimedDoc(real_doc):
        def apply_update(self, update):
            clock["applies"] += 1
            clock["now"] += 0.1
            return super().apply_update(update)

    monkeypatch.setattr(membership_module, "Doc", TimedDoc)
    monkeypatch.setattr(store, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(store, "_pending_drain_budget_seconds", 0.5)
    assert store.expire_pending() == 0
    assert 0 < clock["applies"] <= 6
    assert token not in store._membership_memo
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    with store._db:
        store._db.execute("UPDATE pending SET held_at = '2100-01-01'")
    first = clock["applies"]
    for _ in range(100):
        assert store.expire_pending() == 0
        if token in store._membership_memo:
            break
    assert token in store._membership_memo
    assert clock["applies"] > first
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    previous = clock["applies"]
    assert store.expire_pending() == 0
    assert clock["applies"] == previous


def test_every_token_cache_equals_journal_replay_after_suffixes(stores):
    store = stores("owner")
    genesis(store)
    tokens = []
    for value in range(12):
        tokens.append(store._meta_frontier_for_commit())
        owner_change(store, value)
        for token in tokens:
            actual = store._membership_at(token)
            assert actual is not None
            before = tuple(cached(store, token))
            with store._db:
                store._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (token,))
            expected = store._membership_at(token)
            assert expected is not None
            assert json.dumps(actual[1], sort_keys=True) == json.dumps(expected[1], sort_keys=True)
            assert tuple(cached(store, token)) == before


def test_member_suffix_can_affect_token_older_than_its_stamped_frontier(stores):
    owner = stores("owner")
    birth = genesis(owner)
    old_token = owner._meta_frontier_for_commit()
    later = owner_change(owner, "later")
    member = stores("member")
    for record in (birth, later):
        assert member.import_envelope(record.envelope_bytes, supplier="owner").status == "applied"
    assert owner._membership_at(old_token) is not None
    prior = bytes(cached(owner, old_token)["meta_update"])
    suffix = member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    assert json.loads(suffix.envelope_bytes)["origin"]["metaFrontier"] != ""
    for row in member._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid"):
        assert owner.import_envelope(bytes(row[0]), supplier="member").status == "applied"
    assert cached(owner, old_token) is None
    snapshot = owner._membership_at(old_token)
    assert snapshot is not None
    assert snapshot[2].get_update() != prior


def test_cached_snapshot_copy_and_mutation_stay_private(stores):
    store = stores("owner")
    genesis(store)
    token = store._meta_frontier_for_commit()
    first = store._membership_at(token)
    second = store._membership_at(token)
    update = bytes(cached(store, token)["meta_update"])
    assert first[2].get_update() == second[2].get_update() == update
    first[2].get("members", type=Map)["user:owner"] = Map({"mode": "none"})
    assert second[2].get("members", type=Map).to_py()["user:owner"]["mode"] == "rw"
    assert second[2].get_update() == update
    assert store._membership_at(token)[1]["user:owner"]["mode"] == "rw"


def test_budgeted_invalidation_never_returns_stale_cache_or_loses_drain(stores, monkeypatch):
    owner = stores("owner")
    birth = genesis(owner)
    member = stores("member")
    assert member.import_envelope(birth.envelope_bytes, supplier="owner").status == "applied"
    old_token = owner._meta_frontier_for_commit()
    for value in range(12):
        owner_change(owner, value)
    assert owner._membership_at(old_token) is not None
    suffix = member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    assert owner._pending(suffix.envelope_bytes, "member")
    clock = {"now": 0.0}
    real_doc = snapshots_module.Doc

    class TimedDoc(real_doc):
        def apply_update(self, update):
            clock["now"] += 0.1
            return super().apply_update(update)

    monkeypatch.setattr(snapshots_module, "Doc", TimedDoc)
    monkeypatch.setattr(owner, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(owner, "_pending_drain_budget_seconds", 0.5)
    assert owner.expire_pending() == 1
    assert owner.take_drained() == (suffix.envelope_bytes,)
    assert owner._meta_frontier_dirty
    assert clock["now"] <= 0.6
    owner._membership_deadline = clock["now"]
    with pytest.raises(_MembershipBudgetExceeded):
        owner._membership_at(old_token)
    owner._membership_deadline = None
    assert owner._membership_at(old_token) is not None
    assert not owner._meta_frontier_dirty
    snapshot = tuple(cached(owner, old_token))
    with owner._db:
        owner._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (old_token,))
    assert owner._membership_at(old_token) is not None
    assert tuple(cached(owner, old_token)) == snapshot


def test_mixed_suffix_cache_matches_original_replay_for_every_token(stores):
    owner = stores("owner")
    birth = genesis(owner)
    member = stores("member")
    assert member.import_envelope(birth.envelope_bytes, supplier="owner").status == "applied"
    tokens = {owner._meta_frontier_for_commit()}
    comparisons = 0
    for value in range(10):
        owner.commit(
            "meta", lambda doc, value=value: doc["members"].__setitem__(
                "user:changing", Map({"mode": "rw", "value": str(value)})),
            author="user:owner", actor=f"agent:{value % 2}",
        )
        tokens.add(owner._meta_frontier_for_commit())
        if value % 2 == 0:
            writer = member._writer_for("user:member", None)
            member.commit(
                "meta", lambda doc: doc["writers"].__setitem__(writer, Map({
                    "author": "user:member", "node": "member", "peerId": member._client_id(writer),
                })), author="user:member", actor=None,
            )
            for row in member._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid"):
                assert owner.import_envelope(bytes(row[0]), supplier="member").status == "applied"
        tokens.add(owner._docs["meta"].get_state())
        for token in tokens:
            expected = _membership_at_reference(owner, token)
            actual = owner._membership_at(token)
            assert expected is not None
            assert actual is not None
            assert actual[:2] == expected
            comparisons += 1
    assert comparisons >= 100


def test_restart_reconciles_suffix_committed_before_cache_invalidation(stores, monkeypatch):
    owner = stores("owner")
    birth = genesis(owner)
    token = owner._meta_frontier_for_commit()
    prior = bytes(cached(owner, token)["meta_update"])
    member = stores("member")
    assert member.import_envelope(birth.envelope_bytes, supplier="owner").status == "applied"
    suffix = member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    watermark = owner._db.execute("SELECT journal_rowid FROM meta_frontier_history").fetchone()[0]
    monkeypatch.setattr(owner, "_record_meta_frontier", lambda _token: None)
    assert owner.import_envelope(suffix.envelope_bytes, supplier="member").status == "applied"
    assert owner._meta_history_head()[1] > watermark
    owner.close()

    restarted = stores("owner")
    snapshot = restarted._membership_at(token)
    assert snapshot is not None
    assert snapshot[2].get_update() != prior
    assert snapshot[:2] == _membership_at_reference(restarted, token)
    assert restarted._db.execute("SELECT journal_rowid FROM meta_frontier_history").fetchone()[0] == restarted._meta_history_head()[1]


def test_current_token_uses_complete_journal_baseline_after_two_commits(stores, monkeypatch):
    store = stores("owner")
    genesis(store)
    for value in range(40):
        owner_change(store, value)
    monkeypatch.setattr(
        store, "_replay_membership", lambda _token: pytest.fail("current token replayed history")
    )
    owner_change(store, "first")
    owner_change(store, "second")
    token = store._meta_frontier_for_commit()
    assert store._membership_at(token)[1]["user:changing"]["value"] == "second"


def test_unaccounted_prefix_is_replayed_before_using_incremental_baseline(stores, monkeypatch):
    store = stores("owner")
    genesis(store)
    replay = store._replay_membership
    calls = []

    def counted(token):
        calls.append(token)
        return replay(token)

    monkeypatch.setattr(store, "_replay_membership", counted)
    with monkeypatch.context() as gap:
        gap.setattr(store, "_advance_meta_baseline", lambda _update: None)
        owner_change(store, "after-gap")
    token = store._meta_frontier_for_commit()
    assert len(calls) == 1
    assert store._membership_at(token)[:2] == _membership_at_reference(store, token)
    owner_change(store, "incremental-again")
    assert len(calls) == 1


def test_replay_result_cannot_mutate_private_incremental_baseline(stores):
    store = stores("owner")
    genesis(store)
    token = store._meta_frontier_for_commit()
    with store._db:
        store._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (token,))
    store._meta_baseline = None
    result = store._membership_at(token)
    result[2].get("members", type=Map)["user:fake"] = Map({"mode": "rw"})
    owner_change(store, "later")
    current = store._membership_at(store._meta_frontier_for_commit())
    assert "user:fake" not in current[1]
    assert current[:2] == _membership_at_reference(store, store._meta_frontier_for_commit())
