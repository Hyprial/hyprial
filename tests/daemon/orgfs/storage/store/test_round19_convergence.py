from __future__ import annotations

import os
import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, StoreError
from hyprial.daemon.impl.orgfs.storage.store import _decode_state_vector

from .test_member_add_index import _genesis, _set_member, _store


MEMBERS = tuple(f"user:r19-member-{index}" for index in range(4))


@pytest.fixture
def stores() -> Iterator[list[LocalSpaceStore]]:
    opened: list[LocalSpaceStore] = []
    yield opened
    for store in reversed(opened):
        store.close()


def _meta_envelopes(store: LocalSpaceStore) -> list[bytes]:
    return [
        bytes(row[0])
        for row in store._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        )
    ]


def _meta_keys(store: LocalSpaceStore) -> set[tuple[str, int]]:
    return {
        (str(row[0]), int(row[1]))
        for row in store._db.execute(
            "SELECT writer, seq FROM commits WHERE doc_id = 'meta'"
        )
    }


def _offer(
    store: LocalSpaceStore,
    envelopes: list[bytes],
    *,
    supplier: str,
) -> list[tuple[str, str, str, str, int]]:
    results = []
    for envelope in envelopes:
        result = store.import_envelope(envelope, supplier=supplier)
        assert not (result.details or {}).get("dropped", False)
        value = store._decode_envelope(envelope)
        origin = value["origin"]
        results.append(
            (
                result.status,
                result.code,
                str(origin["author"]),
                str(origin["writer"]),
                int(value["seq"]),
            )
        )
    return results


def test_local_meta_admission_uses_the_envelope_frontier(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R19-A: local and remote admission judge the identical snapshot."""

    store = _store(tmp_path, "r19-local-frontier", stores)
    _genesis(store)
    token = store._meta_frontier_for_commit()
    owner_only = store._membership_at(token)
    assert owner_only is not None
    _set_member(store, MEMBERS[0], active=True, serial=1)
    stamped = store._meta_frontier_for_commit()
    seen: list[bytes] = []

    def snapshot(frontier: bytes):
        seen.append(frontier)
        return owner_only

    monkeypatch.setattr(store, "_membership_at", snapshot)
    with pytest.raises(StoreError, match="not-owner"):
        store.commit("meta", lambda _doc: None, author=MEMBERS[0], actor=None)

    assert seen == [stamped]


def test_rebuild_does_not_trust_a_pending_lane_clock() -> None:
    """R19-B: only integrated structs may advance a rebuild lane."""

    source = Doc(client_id=19)
    before = source.get_state()
    source["members"] = Map({MEMBERS[0]: Map({"mode": "rw"})})
    predecessor = source.get_update(before)
    before = source.get_state()
    source["members"][MEMBERS[0]]["removedAt"] = None
    dependent = source.get_update(before)
    expected_clock = _decode_state_vector(source.get_state())[19]

    probe = Doc()
    before = probe.get_state()
    probe.apply_update(dependent)
    assert not LocalSpaceStore._rebuild_update_integrated(
        probe, before, 19, expected_clock
    )
    probe.apply_update(predecessor)
    before = probe.get_state()
    probe.apply_update(dependent)
    assert LocalSpaceStore._rebuild_update_integrated(
        probe, before, 19, expected_clock
    )


def test_direct_import_parks_an_out_of_order_member_meta_prefix(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
) -> None:
    """Review 1016 M2: import's keep-pending branch fires with no patching.

    A member's second writer registration arrives before its first: the
    target answers not-a-member, the causal-prefix rule parks it, and the
    missing first commit drains it.
    """

    source = _store(tmp_path, "r20-prefix-source", stores)
    _genesis(source)
    _set_member(source, MEMBERS[0], active=True, serial=1)
    base = len(_meta_envelopes(source))
    first = source.commit(
        "meta", lambda _doc: None, author=MEMBERS[0], actor="agent:r20"
    ).envelope_bytes
    second = source.commit(
        "meta", lambda _doc: None, author=MEMBERS[0], actor="agent:r20"
    ).envelope_bytes
    target = _store(tmp_path, "r20-prefix-target", stores)
    for envelope in _meta_envelopes(source)[:base]:
        assert target.import_envelope(envelope, supplier=source.node_id).status == "applied"
    assert target.admission(second) == "not-a-member"

    parked = target.import_envelope(second, supplier=source.node_id)
    assert (parked.status, parked.code) == ("pending", "pending-dependency")

    applied = target.import_envelope(first, supplier=source.node_id)
    assert applied.status == "applied"
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert _meta_keys(target) == _meta_keys(source)


def test_forged_member_meta_is_refused_at_import_but_kept_by_the_drain(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1016 M2: import and drain differ on purpose; pin both sides.

    Only the SOURCE is bent (a peer whose local check lets a member edit the
    member table); the target runs unpatched.
    """

    source = _store(tmp_path, "r20-forged-source", stores)
    _genesis(source)
    _set_member(source, MEMBERS[0], active=True, serial=1)
    monkeypatch.setattr(source, "_local_meta_exception_allowed", lambda **_: True)
    forged = source.commit(
        "meta",
        lambda doc: doc["members"].__setitem__(
            MEMBERS[1],
            Map({"mode": "rw", "addedBy": MEMBERS[0], "addedAt": "forged", "removedAt": None}),
        ),
        author=MEMBERS[0],
        actor="agent:r20",
    ).envelope_bytes
    target = _store(tmp_path, "r20-forged-target", stores)
    for envelope in _meta_envelopes(source)[:-1]:
        assert target.import_envelope(envelope, supplier=source.node_id).status == "applied"

    refused = target.import_envelope(forged, supplier=source.node_id)
    assert (refused.status, refused.code) == ("rejected", "not-a-member")
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0

    # Parked for another reason, the same bytes are kept by the drain's rule.
    assert target._pending(forged, source.node_id)
    target._drain_pending()
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    assert MEMBERS[1] not in target._membership()


def test_owner_commit_on_an_unmaterialized_frontier_is_not_a_permission_refusal(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 1016 m3: the owner is never told its own write is owner-only."""

    store = _store(tmp_path, "r19-owner-unavailable", stores)
    _genesis(store)
    monkeypatch.setattr(store, "_membership_at", lambda _token: None)

    with pytest.raises(StoreError) as failure:
        _set_member(store, MEMBERS[0], active=True, serial=1)

    assert failure.value.code == "unavailable"


def test_rebuild_warns_when_it_skips_an_unreachable_entry(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review 1016 m4: a skipped rebuild entry is never silent."""

    store = _store(tmp_path, "r19-rebuild-warn", stores)
    _genesis(store)
    _set_member(store, MEMBERS[0], active=True, serial=1)
    _set_member(store, MEMBERS[1], active=True, serial=2)
    # Drop the middle owner commit: the last entry's frontier now names a
    # lane clock no journal row can reach.
    rowid = store._db.execute(
        "SELECT rowid FROM commits WHERE doc_id = 'meta' ORDER BY rowid LIMIT 1 OFFSET 1"
    ).fetchone()[0]
    store._db.execute("DELETE FROM commits WHERE rowid = ?", (rowid,))
    store._db.execute("DELETE FROM member_add_frontiers")
    store._db.commit()

    with caplog.at_level("WARNING"):
        store._rebuild_member_add_frontiers()

    assert any(
        "rebuild skipped an entry whose frontier is unreachable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("seed", range(int(os.environ.get("HYPRIAL_R19_SEEDS", "12"))))
def test_seeded_partial_exchange_converges(seed: int, tmp_path: Path) -> None:
    """R19-B: locally admitted metadata converges after a complete re-offer."""

    rng = random.Random(19000 + seed)
    opened: list[LocalSpaceStore] = []
    try:
        owner_count = 2 + seed % 2
        owners = [
            _store(tmp_path, f"r19-{seed}-owner-{i}", opened)
            for i in range(owner_count)
        ]
        members = [_store(tmp_path, f"r19-{seed}-member-{i}", opened) for i in range(4)]
        actors = [*owners, *members]
        for index, store in enumerate(actors):
            store._incarnation = f"r19-{seed}-incarnation-{index}"
            store._lanes.clear()

        history = [_genesis(owners[0])]
        for store in actors[1:]:
            _offer(store, history, supplier=owners[0].node_id)

        serial = 0
        for cycle in range(16):
            owner = rng.choice(owners)
            partial = [item for item in history if rng.random() < 0.58]
            rng.shuffle(partial)
            _offer(owner, partial, supplier="r19-partial-owner")
            _offer(owner, history, supplier="r19-owner-causal-past")
            user = MEMBERS[cycle % len(MEMBERS)]
            current = owner._membership().get(user)
            active = isinstance(current, dict) and not current.get("removedAt")
            serial += 1
            history.append(_set_member(owner, user, active=not active, serial=serial))

            member = members[cycle % len(members)]
            partial = [item for item in history if rng.random() < 0.52]
            rng.shuffle(partial)
            _offer(member, partial, supplier="r19-partial-member")
            _offer(member, history, supplier="r19-member-causal-past")
            visible = member._membership().get(user)
            if isinstance(visible, dict) and not visible.get("removedAt"):
                member._incarnation = f"r19-{seed}-member-{cycle}-restart"
                member._lanes.clear()
                try:
                    record = member.commit(
                        "meta",
                        lambda _doc: None,
                        author=user,
                        actor=f"agent:r19-{cycle}",
                    )
                except StoreError as exc:
                    assert exc.code == "not-owner"
                else:
                    history.append(record.envelope_bytes)

        observers = [
            _store(tmp_path, f"r19-{seed}-observer-{index}", opened)
            for index in range(2)
        ]
        not_members: list[tuple[str, str, str, str, int]] = []
        for index, observer in enumerate(observers):
            shuffled = list(history)
            random.Random(19100 + seed * 7 + index).shuffle(shuffled)
            not_members.extend(
                result
                for result in _offer(
                    observer, shuffled, supplier=f"r19-observer-{index}"
                )
                if result[1] == "not-a-member"
            )

        replicas = [*actors, *observers]
        for _repeat in range(3):
            for replica in replicas:
                not_members.extend(
                    result
                    for result in _offer(replica, history, supplier="r19-full-reoffer")
                    if result[1] == "not-a-member"
                )

        accepted = [_meta_keys(replica) for replica in replicas]
        assert not not_members, (seed, not_members)
        assert all(keys == accepted[0] for keys in accepted[1:]), seed
        for replica in replicas:
            assert (
                replica._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
            ), (seed, replica.node_id)
            live = {user: replica.member_latest_add_frontier(user) for user in MEMBERS}
            oracle = {
                user: replica._member_latest_add_frontier_scan(user) for user in MEMBERS
            }
            assert live == oracle, (seed, replica.node_id, "live")
            replica._rebuild_member_add_frontiers()
            rebuilt = {
                user: replica.member_latest_add_frontier(user) for user in MEMBERS
            }
            assert rebuilt == oracle, (seed, replica.node_id, "rebuild")
    finally:
        for store in reversed(opened):
            store.close()


@pytest.mark.parametrize("author_at_frontier", [True, False])
def test_local_meta_admission_judges_the_author_at_the_stamped_frontier(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
    author_at_frontier: bool,
) -> None:
    """Owner review of R19-A: the author's membership must come from the
    stamped frontier, not from live state.  Same snapshot except for the
    author's membership row: present => the member's own writer registration
    commits; absent (although live membership has it) => refused."""

    store = _store(tmp_path, f"r19-author-{author_at_frontier}", stores)
    _genesis(store)
    _set_member(store, MEMBERS[0], active=True, serial=1)
    stamped = store._meta_frontier_for_commit()
    real = store._membership_at(stamped)
    assert real is not None
    owner, members, frontier_doc = real
    assert MEMBERS[0] in members
    if not author_at_frontier:
        members = {key: value for key, value in members.items() if key != MEMBERS[0]}
    monkeypatch.setattr(store, "_membership_at", lambda _token: (owner, members, frontier_doc))

    if author_at_frontier:
        store.commit("meta", lambda _doc: None, author=MEMBERS[0], actor=None)
    else:
        with pytest.raises(StoreError, match="not-owner"):
            store.commit("meta", lambda _doc: None, author=MEMBERS[0], actor=None)
