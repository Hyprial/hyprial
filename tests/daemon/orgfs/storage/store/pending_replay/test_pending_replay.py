"""Pending-replay livelock regression tests (M-emergent-org-structure-refactor-0062).

Covers four behaviors demanded by the work item:

1. An unresolvable membership token is replayed at most once per meta-history
   head (negative memo); unchanged meta makes the next tick replay nothing.
2. A new meta commit invalidates the memo so each row is re-evaluated once.
3. The drain honors an injectable time budget and resumes on the next tick.
4. The linearized replay resolves exactly like the original quadratic
   algorithm (differential equivalence over generated histories).
"""

from __future__ import annotations

import json
import time

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.storage.store import (
    FileSpaceStore,
    LocalSpaceStore,
    StoreError,
)
from hyprial.daemon.impl.orgfs.storage.store import membership as membership_module
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _decode_state_vector,
    _doc_roots,
    _encode_state_vector,
    _unb64,
)

TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"

_OPEN: list[LocalSpaceStore] = []


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    yield
    while _OPEN:
        _OPEN.pop().close()


def _store(tmp_path, name, node):
    store = FileSpaceStore(tmp_path / name, "space-1", node_id=node)
    _OPEN.append(store)
    return store


def _genesis(store):
    return store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["space"].__setitem__("spaceId", "space-1"),
            doc["members"].__setitem__(
                "user:owner", Map({"mode": "rw", "removedAt": None})
            ),
            doc["members"].__setitem__(
                "user:member", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree",
                Map({"active": TREE_DOC_ID, "activatedAt": "genesis", "planId": None}),
            ),
        ),
        author="user:owner",
        actor=None,
    )


def _writer_touch(store, writer_key, author):
    return lambda doc: doc["writers"].__setitem__(
        writer_key,
        Map(
            {
                "author": author,
                "node": store.node_id,
                "peerId": store._client_id(writer_key),
            }
        ),
    )


def _count_membership_applies(monkeypatch):
    counts = {"applies": 0}
    real_doc = membership_module.Doc

    class _CountingDoc(real_doc):
        def apply_update(self, update):
            counts["applies"] += 1
            return super().apply_update(update)

    monkeypatch.setattr(membership_module, "Doc", _CountingDoc)
    return counts


def _pending_row(store, token, *, writer="writer-x", seq=0, doc_id="doc-x"):
    store._db.execute(
        "INSERT INTO pending(writer, seq, doc_id, author, meta_frontier, "
        "envelope_bytes, supplier, held_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            writer,
            seq,
            doc_id,
            "user:member",
            token,
            b"\x00",
            "node-member",
            "2100-01-01T00:00:00+00:00",
        ),
    )
    store._db.commit()


def _unresolvable_token(store):
    owner = store._owner()
    client_id = next(iter(store._owner_client_ids(owner)))
    return _encode_state_vector({client_id: 10_000})


def _mixed_history(tmp_path, *, member_commits, owner_commits):
    owner = _store(tmp_path, "history-owner", "node-owner")
    genesis = _genesis(owner)
    member = _store(tmp_path, "history-member", "node-member")
    imported = member.import_envelope(genesis.envelope_bytes, supplier="node-owner")
    assert imported.status == "applied", imported.details
    member_key = member._writer_for("user:member", None)
    member_records = []
    for index in range(member_commits):
        record = member.commit(
            "meta",
            _writer_touch(member, member_key, "user:member"),
            author="user:member",
            actor=None,
        )
        member_records.append(record)
    for record in member_records:
        result = owner.import_envelope(record.envelope_bytes, supplier="node-member")
        assert result.status == "applied", result.details
    owner_key = owner._writer_for("user:owner", None)
    owner_records = []
    for index in range(owner_commits):
        record = owner.commit(
            "meta",
            _writer_touch(owner, owner_key, "user:owner"),
            author="user:owner",
            actor=None,
        )
        owner_records.append(record)
    return owner, member, genesis, member_records, owner_records


def test_unresolvable_pending_second_tick_replays_nothing(tmp_path, monkeypatch):
    owner, _member, _genesis, member_records, owner_records = _mixed_history(
        tmp_path, member_commits=550, owner_commits=50
    )
    token = _unresolvable_token(owner)
    for index in range(50):
        _pending_row(owner, token, writer=f"writer-{index}", seq=index)
    pending_before = owner._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    assert pending_before == 50

    counts = _count_membership_applies(monkeypatch)
    first = owner.expire_pending()
    first_tick_applies = counts["applies"]

    counts["applies"] = 0
    second = owner.expire_pending()
    second_tick_applies = counts["applies"]

    assert first == 0
    assert second == 0
    assert owner._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 50
    history = 1 + len(member_records) + len(owner_records)
    assert first_tick_applies > 0
    assert first_tick_applies <= 50 * history * 3, (
        f"first tick must be linear: {first_tick_applies} applies "
        f"for {history} commits x 50 rows"
    )
    assert second_tick_applies == 0, (
        f"unchanged meta must not replay: {second_tick_applies} applies"
    )


def test_new_meta_commit_invalidates_negative_memo(tmp_path, monkeypatch):
    owner, member, _genesis, member_records, _owner_records = _mixed_history(
        tmp_path, member_commits=30, owner_commits=10
    )
    token = _unresolvable_token(owner)
    for index in range(5):
        _pending_row(owner, token, writer=f"writer-{index}", seq=index)

    counts = _count_membership_applies(monkeypatch)
    owner.expire_pending()
    assert counts["applies"] > 0
    counts["applies"] = 0
    owner.expire_pending()
    assert counts["applies"] == 0

    member_key = member._writer_for("user:member", None)
    extra = member.commit(
        "meta",
        _writer_touch(member, member_key, "user:member"),
        author="user:member",
        actor=None,
    )
    assert owner.import_envelope(extra.envelope_bytes, supplier="node-member").status
    owner.expire_pending()
    reevaluated = counts["applies"]
    counts["applies"] = 0
    owner.expire_pending()
    assert reevaluated > 0
    assert counts["applies"] == 0


def _stepping_clock(step):
    state = {"now": 0.0}

    def clock():
        state["now"] += step
        return state["now"]

    return clock


def test_drain_budget_stops_tick_and_resumes(tmp_path, monkeypatch):
    owner, _member, _genesis, _member_records, _owner_records = _mixed_history(
        tmp_path, member_commits=8, owner_commits=4
    )
    token = _unresolvable_token(owner)
    rows = 6
    for index in range(rows):
        _pending_row(owner, token, writer=f"writer-{index}", seq=index)

    calls = {"count": 0}
    real_membership = owner._membership_at

    def counting(token_bytes):
        calls["count"] += 1
        return real_membership(token_bytes)

    monkeypatch.setattr(owner, "_membership_at", counting)
    monkeypatch.setattr(owner, "_monotonic", _stepping_clock(0.45))
    monkeypatch.setattr(owner, "_pending_drain_budget_seconds", 1.0)
    applies = _count_membership_applies(monkeypatch)

    per_tick = []
    for _tick in range(rows - 1):
        calls["count"] = 0
        owner.expire_pending()
        per_tick.append(calls["count"])

    assert owner._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == rows
    assert all(0 < count < rows for count in per_tick), per_tick
    assert sum(per_tick) < rows

    monkeypatch.setattr(owner, "_monotonic", time.monotonic)
    calls["count"] = 0
    applies["applies"] = 0
    owner.expire_pending()
    assert calls["count"] == rows, calls
    assert applies["applies"] > 0, applies
    calls["count"] = 0
    applies["applies"] = 0
    owner.expire_pending()
    assert calls["count"] == rows, calls
    assert applies["applies"] == 0, applies


def _membership_at_reference(store, token):
    if not token:
        return None
    owner = store._genesis_owner()
    if owner is None:
        return None
    ignored_ids = (
        store._known_non_owner_client_ids(owner) - store._owner_client_ids(owner)
    )
    target = {
        client: clock
        for client, clock in _decode_state_vector(token).items()
        if client not in ignored_ids
    }
    selected: list[bytes] = []
    remaining: list[tuple[dict[int, int], bytes]] = []
    for commit in store._db.execute(
        "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
    ):
        try:
            envelope = store._decode_envelope(bytes(commit["envelope_bytes"]))
            update = store._update_bytes(envelope)
            if str(envelope["origin"].get("author")) == owner:
                remaining.append(
                    (
                        {
                            client: clock
                            for client, clock in _decode_state_vector(
                                _unb64(str(envelope["origin"]["metaFrontier"]))
                            ).items()
                            if client not in ignored_ids
                        },
                        update,
                    )
                )
            else:
                selected.append(update)
        except (KeyError, StoreError, TypeError, ValueError):
            return None
    probe = Doc()
    for update in selected:
        probe.apply_update(update)
    while remaining:
        progressed = False
        for before, update in tuple(remaining):
            if not store._state_map_covers(target, before):
                remaining.remove((before, update))
                continue
            trial = Doc()
            try:
                for applied in (*selected, update):
                    trial.apply_update(applied)
            except (TypeError, ValueError):
                return None
            trial_frontier = {
                client: clock
                for client, clock in _decode_state_vector(trial.get_state()).items()
                if client not in ignored_ids
            }
            remaining.remove((before, update))
            if store._state_map_covers(target, trial_frontier):
                probe = trial
                selected.append(update)
                progressed = True
                break
        if not progressed:
            break
    projected = {
        client: clock
        for client, clock in _decode_state_vector(probe.get_state()).items()
        if client not in ignored_ids
    }
    if projected != target:
        return None
    roots = _doc_roots(probe)
    members = roots.get("members")
    return owner, (members if isinstance(members, dict) else {})


def test_membership_at_equivalence_with_original_algorithm(tmp_path):
    owner, _member, genesis, member_records, owner_records = _mixed_history(
        tmp_path, member_commits=12, owner_commits=8
    )
    tokens: list[bytes] = []
    for record in (genesis, *member_records, *owner_records):
        value = json.loads(record.envelope_bytes)
        tokens.append(_unb64(str(value["origin"]["metaFrontier"])))
    tokens.append(owner._docs["meta"].get_state())
    tokens.append(b"")
    tokens.append(_unresolvable_token(owner))
    for record in member_records[:3]:
        tokens.append(_unb64(str(json.loads(record.envelope_bytes)["origin"]["metaFrontier"])))

    for token in tokens:
        expected = _membership_at_reference(owner, token)
        actual = owner._membership_at(token)
        if expected is None:
            assert actual is None, f"token {token!r}: expected None, got {actual!r}"
            continue
        assert actual is not None, f"token {token!r}: expected {expected!r}, got None"
        assert actual[0] == expected[0], f"owner mismatch for {token!r}"
        assert actual[1] == expected[1], f"membership mismatch for {token!r}"
