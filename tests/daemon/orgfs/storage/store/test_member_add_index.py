from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.storage.store import membership as membership_module
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore


OWNER = "user:owner"
USERS = tuple(f"user:member-{index}" for index in range(6))
TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000013"


@pytest.fixture
def stores() -> Iterator[list[LocalSpaceStore]]:
    opened: list[LocalSpaceStore] = []
    yield opened
    for store in reversed(opened):
        store.close()


def _store(
    root: Path, name: str, opened: list[LocalSpaceStore]
) -> LocalSpaceStore:
    store = LocalSpaceStore(root / name, "space-r13", node_id=name)
    opened.append(store)
    return store


def _genesis(store: LocalSpaceStore) -> bytes:
    record = store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", OWNER),
            doc["space"].__setitem__("spaceId", "space-r13"),
            doc["members"].__setitem__(
                OWNER,
                Map(
                    {
                        "mode": "rw",
                        "addedBy": OWNER,
                        "addedAt": "genesis",
                        "removedAt": None,
                    }
                ),
            ),
            doc["docs"].__setitem__(
                "tree",
                Map(
                    {
                        "active": TREE_DOC_ID,
                        "activatedAt": "genesis",
                        "planId": None,
                    }
                ),
            ),
        ),
        author=OWNER,
        actor=None,
    )
    return record.envelope_bytes


def _set_member(
    store: LocalSpaceStore, user: str, *, active: bool, serial: int
) -> bytes:
    previous = store._membership().get(user)
    added_at = (
        f"add-{serial:03d}"
        if active
        else str(previous["addedAt"] if previous is not None else f"add-{serial:03d}")
    )
    record = store.commit(
        "meta",
        lambda doc: doc["members"].__setitem__(
            user,
            Map(
                {
                    "mode": "rw",
                    "addedBy": OWNER,
                    "addedAt": added_at,
                    "removedAt": None if active else f"remove-{serial:03d}",
                }
            ),
        ),
        author=OWNER,
        actor=None,
    )
    return record.envelope_bytes


def _register_member_writer(
    store: LocalSpaceStore, user: str, *, actor: str | None = None
) -> bytes:
    record = store.commit(
        "meta",
        lambda _doc: None,
        author=user,
        actor=actor,
    )
    return record.envelope_bytes


def _ack_member_purge(
    store: LocalSpaceStore, user: str, *, serial: int
) -> bytes:
    record = store.commit(
        "meta",
        lambda doc: doc.get("purgeAcks", type=Map).__setitem__(
            store.node_id,
            Map(
                {
                    "plan-r17": Map(
                        {"ackedAt": f"2026-10-07T00:00:{serial:02d}Z"}
                    )
                }
            ),
        ),
        author=user,
        actor=f"agent:mixed-{serial:02d}",
    )
    return record.envelope_bytes


def _import_all(store: LocalSpaceStore, envelopes: list[bytes]) -> None:
    for envelope in envelopes:
        result = store.import_envelope(envelope, supplier="node-source")
        assert result.status in {"applied", "pending"}, (
            result.code,
            store._decode_envelope(envelope)["origin"],
        )
        assert not (result.details or {}).get("dropped", False)
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def _import_meta_unchecked(store: LocalSpaceStore, envelope_bytes: bytes) -> None:
    """Install fixture history without exercising the out-of-scope live path."""

    envelope = store._decode_envelope(envelope_bytes)
    update = store._update_bytes(envelope)
    store._docs["meta"].apply_update(update)
    store._db.execute(
        "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, "
        "committed_at) VALUES (?, ?, 'meta', ?, ?, 'fixture')",
        (
            str(envelope["origin"]["writer"]),
            int(envelope["seq"]),
            envelope_bytes,
            store._docs["meta"].get_state(),
        ),
    )
    store._db.commit()


def _laggy_two_device_history(
    root: Path, opened: list[LocalSpaceStore], commit_count: int
) -> LocalSpaceStore:
    assert commit_count > 1 and commit_count % 2 == 1
    device_a = _store(root, f"laggy-a-{commit_count}", opened)
    device_b = _store(root, f"laggy-b-{commit_count}", opened)
    observer = _store(root, f"laggy-observer-{commit_count}", opened)
    genesis = _genesis(device_a)
    _import_meta_unchecked(device_b, genesis)
    device_a._record_meta_frontier = lambda _token: None
    device_b._record_meta_frontier = lambda _token: None
    a_commits: list[bytes] = []
    b_commits: list[bytes] = []
    for cycle in range((commit_count - 1) // 2):
        a_commits.append(
            _set_member(
                device_a,
                f"user:laggy-a-{cycle:04d}",
                active=True,
                serial=cycle * 2 + 1,
            )
        )
        if cycle >= 2:
            _import_meta_unchecked(device_b, a_commits[cycle - 2])
        b_commits.append(
            _set_member(
                device_b,
                f"user:laggy-b-{cycle:04d}",
                active=True,
                serial=cycle * 2 + 2,
            )
        )
    for envelope in (genesis, *a_commits, *b_commits):
        _import_meta_unchecked(observer, envelope)
    return observer


def test_member_add_index_matches_scan_for_randomized_arrival_orders(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """The derived lookup is exactly the scan oracle after either arrival order."""

    source = _store(tmp_path, "source", stores)
    envelopes = [_genesis(source)]
    rng = random.Random(13)
    active: set[str] = set()
    for serial in range(1, 25):
        user = rng.choice(USERS)
        should_remove = user in active and rng.random() < 0.45
        envelopes.append(
            _set_member(source, user, active=not should_remove, serial=serial)
        )
        if should_remove:
            active.remove(user)
        else:
            active.add(user)

    forward = _store(tmp_path, "forward", stores)
    reverse = _store(tmp_path, "reverse", stores)
    _import_all(forward, envelopes)
    _import_all(reverse, list(reversed(envelopes)))

    for user in USERS:
        expected = source._member_latest_add_frontier_scan(user)
        assert source.member_latest_add_frontier(user) == expected
        assert forward.member_latest_add_frontier(user) == expected
        assert reverse.member_latest_add_frontier(user) == expected
        assert forward._member_latest_add_frontier_scan(user) == expected
        assert reverse._member_latest_add_frontier_scan(user) == expected


def test_member_add_lookup_cost_is_constant_across_meta_journal_sizes(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 13: lookup never reconstructs a membership snapshot per commit."""

    store = _store(tmp_path, "cost", stores)
    _genesis(store)
    counts: list[int] = []
    original = store._membership_at
    calls = 0

    def counted(token: bytes):
        nonlocal calls
        calls += 1
        return original(token)

    for size in (10, 300):
        while (
            store._db.execute(
                "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
            ).fetchone()[0]
            < size
        ):
            serial = int(
                store._db.execute(
                    "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
                ).fetchone()[0]
            )
            _set_member(
                store,
                f"user:cost-{serial:03d}",
                active=True,
                serial=serial,
            )
        monkeypatch.setattr(store, "_membership_at", counted)
        membership = store._membership
        monkeypatch.setattr(
            store,
            "_membership",
            lambda: pytest.fail("indexed lookup materialized every member"),
        )
        calls = 0
        assert store.member_latest_add_frontier("user:cost-001") is not None
        counts.append(calls)
        monkeypatch.setattr(store, "_membership_at", original)
        monkeypatch.setattr(store, "_membership", membership)

    assert counts == [0, 0]


def test_missing_member_add_index_rebuilds_from_journal(
    tmp_path: Path, stores: list[LocalSpaceStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "rebuild", stores)
    _genesis(store)
    _set_member(store, USERS[0], active=True, serial=1)
    _set_member(store, USERS[0], active=False, serial=2)
    _set_member(store, USERS[0], active=True, serial=3)
    expected = store._member_latest_add_frontier_scan(USERS[0])
    journal = store._journal_path
    store.close()
    stores.remove(store)

    connection = sqlite3.connect(journal)
    connection.execute("DROP TABLE member_add_frontiers")
    connection.commit()
    connection.close()

    reopened = _store(tmp_path, "rebuild", stores)
    monkeypatch.setattr(
        reopened,
        "_membership_at",
        lambda _token: pytest.fail("indexed lookup rescanned membership"),
    )
    assert reopened.member_latest_add_frontier(USERS[0]) == expected


def test_missing_member_add_frontier_fails_closed(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    store = _store(tmp_path, "compacted", stores)
    _genesis(store)
    _set_member(store, USERS[0], active=True, serial=1)
    store._db.execute(
        "DELETE FROM member_add_frontiers WHERE user = ?", (USERS[0],)
    )
    store._db.commit()

    assert store._membership()[USERS[0]]["removedAt"] is None
    assert store.member_latest_add_frontier(USERS[0]) is None


def test_member_add_index_update_is_atomic_with_meta_journal(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    store = _store(tmp_path, "atomic", stores)
    _genesis(store)
    before = store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    store._db.execute(
        "CREATE TRIGGER reject_member_add_index BEFORE INSERT "
        "ON member_add_frontiers BEGIN SELECT RAISE(ABORT, 'index-failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="index-failure"):
        _set_member(store, USERS[0], active=True, serial=1)

    assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before
    assert USERS[0] not in store._membership()


def test_two_owner_device_index_rebuild_matches_scan_oracle(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 14: a merged owner frontier remains replayable on upgrade."""

    device_a = _store(tmp_path, "owner-a", stores)
    device_b = _store(tmp_path, "owner-b", stores)
    genesis = _genesis(device_a)
    _import_all(device_b, [genesis])

    concurrent_a = _set_member(device_a, USERS[0], active=True, serial=1)
    concurrent_b = _set_member(device_b, USERS[1], active=True, serial=2)
    _import_all(device_a, [concurrent_b])
    _import_all(device_b, [concurrent_a])

    for serial, (writer, peer, user) in enumerate(
        (
            (device_a, device_b, USERS[2]),
            (device_b, device_a, USERS[3]),
            (device_a, device_b, USERS[4]),
        ),
        start=3,
    ):
        envelope = _set_member(writer, user, active=True, serial=serial)
        _import_all(peer, [envelope])

    expected = {
        user: device_a._member_latest_add_frontier_scan(user)
        for user in USERS[:5]
    }
    assert all(frontier is not None for frontier in expected.values())
    assert {
        user: device_a.member_latest_add_frontier(user) for user in USERS[:5]
    } == expected

    device_a._rebuild_member_add_frontiers()

    assert {
        user: device_a.member_latest_add_frontier(user) for user in USERS[:5]
    } == expected


def test_rebuild_merge_frontier_not_on_any_replay_path(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 15: a baseline miss still derives the envelope's add row."""

    device_a = _store(tmp_path, "owner-a-miss", stores)
    device_b = _store(tmp_path, "owner-b-miss", stores)
    observer = _store(tmp_path, "owner-observer-miss", stores)
    genesis = _genesis(device_a)
    _import_all(device_b, [genesis])
    _import_all(observer, [genesis])

    a1 = _set_member(device_a, USERS[0], active=True, serial=1)
    b1 = _set_member(device_b, USERS[1], active=True, serial=2)
    _import_all(device_b, [a1])
    b2 = _set_member(device_b, USERS[2], active=True, serial=3)
    a2 = _set_member(device_a, USERS[3], active=True, serial=4)
    _import_all(observer, [a1, a2, b1, b2])

    expected = {
        user: observer._member_latest_add_frontier_scan(user)
        for user in USERS[:4]
    }
    assert all(frontier is not None for frontier in expected.values())
    assert {
        user: observer.member_latest_add_frontier(user) for user in USERS[:4]
    } == expected

    observer._rebuild_member_add_frontiers()

    assert {
        user: observer.member_latest_add_frontier(user) for user in USERS[:4]
    } == expected


def test_randomized_multi_device_rebuild_matches_scan_oracle(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 15: rebuild is invariant across seeded concurrent arrivals."""

    rng = random.Random(15)
    devices = [
        _store(tmp_path, f"random-owner-{index}", stores)
        for index in range(3)
    ]
    genesis = _genesis(devices[0])
    for device in devices[1:]:
        _import_all(device, [genesis])

    envelopes = [genesis]
    known = [{0} for _device in devices]
    members: list[str] = []
    for serial in range(1, 19):
        writer_index = rng.randrange(len(devices))
        if rng.random() < 0.55:
            missing = [
                envelope
                for index, envelope in enumerate(envelopes)
                if index not in known[writer_index]
            ]
            _import_all(devices[writer_index], missing)
            known[writer_index].update(range(len(envelopes)))
        user = f"user:random-{serial:03d}"
        members.append(user)
        envelopes.append(
            _set_member(
                devices[writer_index], user, active=True, serial=serial
            )
        )
        known[writer_index].add(len(envelopes) - 1)

    arrival_orders = [
        list(envelopes),
        list(reversed(envelopes)),
        [envelopes[0], *rng.sample(envelopes[1:], len(envelopes) - 1)],
        [envelopes[0], *rng.sample(envelopes[1:], len(envelopes) - 1)],
    ]
    for order_index, arrival_order in enumerate(arrival_orders):
        observer = _store(tmp_path, f"random-observer-{order_index}", stores)
        _import_all(observer, arrival_order)
        expected = {
            user: observer._member_latest_add_frontier_scan(user)
            for user in members
        }
        assert all(frontier is not None for frontier in expected.values())
        assert {
            user: observer.member_latest_add_frontier(user) for user in members
        } == expected

        observer._rebuild_member_add_frontiers()

        assert {
            user: observer.member_latest_add_frontier(user) for user in members
        } == expected


def test_member_add_rebuild_decodes_journal_only_linearly(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 14: rebuild must not rescan the journal for every row."""

    store = _store(tmp_path, "linear-rebuild", stores)
    _genesis(store)
    for serial in range(1, 40):
        _set_member(
            store,
            f"user:linear-{serial:03d}",
            active=True,
            serial=serial,
        )
    commit_count = int(
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
        ).fetchone()[0]
    )
    original = store._decode_envelope
    decode_calls = 0

    def counted(raw: bytes):
        nonlocal decode_calls
        decode_calls += 1
        return original(raw)

    monkeypatch.setattr(store, "_decode_envelope", counted)
    store._rebuild_member_add_frontiers()

    assert decode_calls == commit_count


def test_laggy_two_device_rebuild_materializes_each_commit_once(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 16: one miss per B commit never triggers journal rescans."""

    store = _laggy_two_device_history(tmp_path, stores, 121)
    original_decode = store._decode_envelope
    original_membership_at = store._membership_at
    decode_calls = 0
    membership_at_calls = 0

    def counted_decode(raw: bytes):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(raw)

    def counted_membership_at(token: bytes):
        nonlocal membership_at_calls
        membership_at_calls += 1
        return original_membership_at(token)

    monkeypatch.setattr(store, "_decode_envelope", counted_decode)
    monkeypatch.setattr(store, "_membership_at", counted_membership_at)
    store._rebuild_member_add_frontiers()

    assert membership_at_calls == 0
    assert decode_calls == 121


def test_rebuild_includes_earlier_member_meta_dependencies(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 17: member metadata can be a structural dependency of owner adds."""

    store = _store(tmp_path, "member-meta-dependency", stores)
    _genesis(store)
    _set_member(store, USERS[0], active=True, serial=1)
    _register_member_writer(store, USERS[0])
    store.commit(
        "meta",
        lambda doc: doc.get("purgePlans", type=Map).__setitem__(
            "plan-r17", Map({})
        ),
        author=OWNER,
        actor=None,
    )
    store.commit(
        "meta",
        lambda doc: doc.get("purgeAcks", type=Map).__setitem__(
            store.node_id,
            Map(
                {
                    "plan-r17": Map(
                        {"ackedAt": "2026-10-07T00:00:00Z"}
                    )
                }
            ),
        ),
        author=OWNER,
        actor=None,
    )
    member = _store(tmp_path, "member-meta-writer", stores)
    _import_all(
        member,
        [
            bytes(row[0])
            for row in store._db.execute(
                "SELECT envelope_bytes FROM commits "
                "WHERE doc_id = 'meta' ORDER BY rowid"
            )
        ],
    )
    acknowledgement = member.commit(
        "meta",
        lambda doc: doc.get("purgeAcks", type=Map).__setitem__(
            member.node_id,
            Map(
                {
                    "plan-r17": Map(
                        {"ackedAt": "2026-10-07T00:00:00Z"}
                    )
                }
            ),
        ),
        author=USERS[0],
        actor=None,
    )
    result = store.import_envelope(
        acknowledgement.envelope_bytes, supplier=member.node_id
    )
    assert result.status == "applied", (result.code, result.details)
    _set_member(store, USERS[1], active=True, serial=2)
    _set_member(store, USERS[2], active=True, serial=3)

    expected = {
        user: store._member_latest_add_frontier_scan(user) for user in USERS[:3]
    }
    assert all(frontier is not None for frontier in expected.values())
    store._rebuild_member_add_frontiers()

    assert {
        user: store.member_latest_add_frontier(user) for user in USERS[:3]
    } == expected


def test_randomized_mixed_owner_member_rebuild_matches_scan_oracle(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 17: owner membership and member metadata survive arrival order."""

    source = _store(tmp_path, "mixed-source", stores)
    envelopes = [_genesis(source)]
    envelopes.append(
        source.commit(
            "meta",
            lambda doc: doc.get("purgePlans", type=Map).__setitem__(
                "plan-r17", Map({})
            ),
            author=OWNER,
            actor=None,
        ).envelope_bytes
    )
    envelopes.append(
        source.commit(
            "meta",
            lambda doc: doc.get("purgeAcks", type=Map).__setitem__(
                source.node_id,
                Map(
                    {
                        "plan-r17": Map(
                            {"ackedAt": "2026-10-07T00:00:00Z"}
                        )
                    }
                ),
            ),
            author=OWNER,
            actor=None,
        ).envelope_bytes
    )
    members = [f"user:mixed-{index:02d}" for index in range(5)]
    active: set[str] = set()
    rng = random.Random(17)
    for serial in range(1, 18):
        user = members[serial % len(members)]
        if user not in active:
            envelopes.append(
                _set_member(source, user, active=True, serial=serial)
            )
            active.add(user)
            envelopes.append(
                _register_member_writer(
                    source, user, actor=f"agent:mixed-{serial:02d}"
                )
            )
        elif serial % 5 == 0:
            envelopes.append(
                _set_member(source, user, active=False, serial=serial)
            )
            active.remove(user)
        else:
            envelopes.append(_ack_member_purge(source, user, serial=serial))

    arrival_orders = [
        list(envelopes),
        list(reversed(envelopes)),
        [envelopes[0], *rng.sample(envelopes[1:], len(envelopes) - 1)],
        [envelopes[0], *rng.sample(envelopes[1:], len(envelopes) - 1)],
    ]
    for order_index, arrival_order in enumerate(arrival_orders):
        observer = _store(tmp_path, f"mixed-observer-{order_index}", stores)
        _import_all(observer, arrival_order)
        expected = {
            user: observer._member_latest_add_frontier_scan(user)
            for user in members
        }
        observer._rebuild_member_add_frontiers()
        assert {
            user: observer.member_latest_add_frontier(user) for user in members
        } == expected


@pytest.mark.parametrize("incarnation_seed", (0, 1, 2))
def test_live_mixed_history_materializes_member_dependencies(
    incarnation_seed: int, tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 18: shuffled live imports settle without false rejection."""

    devices = [
        _store(tmp_path, f"live-owner-{incarnation_seed}-{index}", stores)
        for index in range(3)
    ]
    for index, device in enumerate(devices):
        device._incarnation = f"live-incarnation-{incarnation_seed}-{index}"
        device._lanes.clear()
    envelopes = [_genesis(devices[0])]
    envelopes.append(
        devices[0].commit(
            "meta",
            lambda doc: doc.get("purgePlans", type=Map).__setitem__(
                "plan-r17", Map({})
            ),
            author=OWNER,
            actor=None,
        ).envelope_bytes
    )
    envelopes.append(
        devices[0].commit(
            "meta",
            lambda doc: doc.get("purgeAcks", type=Map).__setitem__(
                devices[0].node_id,
                Map(
                    {
                        "plan-r17": Map(
                            {"ackedAt": "2026-10-07T00:00:00Z"}
                        )
                    }
                ),
            ),
            author=OWNER,
            actor=None,
        ).envelope_bytes
    )
    for device in devices[1:]:
        _import_all(device, envelopes)
    known = [set(range(len(envelopes))) for _device in devices]
    members: list[str] = []
    history_rng = random.Random(1800)
    for serial in range(1, 9):
        writer_index = history_rng.randrange(len(devices))
        device = devices[writer_index]
        if history_rng.random() < 0.65:
            missing = [
                envelope
                for index, envelope in enumerate(envelopes)
                if index not in known[writer_index]
            ]
            _import_all(device, missing)
            known[writer_index].update(range(len(envelopes)))
        user = f"user:live-mixed-{serial:03d}"
        members.append(user)
        envelopes.append(_set_member(device, user, active=True, serial=serial))
        known[writer_index].add(len(envelopes) - 1)
        envelopes.append(_ack_member_purge(device, user, serial=serial))
        known[writer_index].add(len(envelopes) - 1)

    source = _store(tmp_path, f"live-mixed-source-{incarnation_seed}", stores)
    _import_all(source, envelopes)

    expected_membership = source._membership()
    expected_frontiers = {
        user: source.member_latest_add_frontier(user) for user in members
    }
    arrival_orders: list[tuple[str, list[bytes]]] = []
    for seed in (18, 181, 1801, 18001, 180001, 1800001):
        shuffled = list(envelopes[1:])
        random.Random(seed).shuffle(shuffled)
        arrival_orders.append((str(seed), shuffled))
    for order_name, arrival_order in arrival_orders:
        observer = _store(
            tmp_path,
            f"live-mixed-observer-{incarnation_seed}-{order_name}",
            stores,
        )
        for envelope in (envelopes[0], *arrival_order):
            value = observer._decode_envelope(envelope)
            result = observer.import_envelope(envelope, supplier=source.node_id)
            assert result.code != "not-a-member"
            if result.status == "rejected":
                applied = observer._db.execute(
                    "SELECT 1 FROM commits WHERE writer = ? AND seq = ? "
                    "AND doc_id = ?",
                    (
                        str(value["origin"]["writer"]),
                        int(value["seq"]),
                        str(value["docId"]),
                    ),
                ).fetchone()
                assert applied is None, (order_name, result.code, value["origin"])
            assert result.status in {"applied", "pending"}, (
                order_name,
                result.code,
                value["origin"],
            )

        assert observer._db.execute(
            "SELECT COUNT(*) FROM pending"
        ).fetchone()[0] == 0, (
            incarnation_seed,
            order_name,
        )
        assert observer._membership() == expected_membership
        assert {
            user: observer._member_latest_add_frontier_scan(user)
            for user in members
        } == expected_frontiers
        assert {
            user: observer.member_latest_add_frontier(user) for user in members
        } == expected_frontiers


def test_rebuild_work_is_linear_across_owner_restart_writers(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 17: a fresh owner writer per commit does not multiply replay."""

    store = _store(tmp_path, "restart-writers", stores)
    _genesis(store)
    for serial in range(1, 60):
        store._incarnation = f"restart-{serial:03d}"
        store._lanes.clear()
        _set_member(
            store,
            f"user:restart-{serial:03d}",
            active=True,
            serial=serial,
        )
    commit_count = int(
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
        ).fetchone()[0]
    )
    original = store._apply_member_rebuild_update
    apply_calls = 0

    def counted(doc, membership, update):
        nonlocal apply_calls
        apply_calls += 1
        return original(doc, membership, update)

    monkeypatch.setattr(store, "_apply_member_rebuild_update", counted)
    store._rebuild_member_add_frontiers()

    assert commit_count == 60
    assert apply_calls == commit_count


def test_rebuild_frontier_codec_calls_are_linear_across_writers(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 18: W≈N frontiers are decoded once and encoded only for output."""

    store = _store(tmp_path, "restart-codecs", stores)
    _genesis(store)
    for serial in range(1, 60):
        store._incarnation = f"codec-restart-{serial:03d}"
        store._lanes.clear()
        _set_member(
            store,
            f"user:codec-restart-{serial:03d}",
            active=True,
            serial=serial,
        )
    commit_count = int(store._db.execute(
        "SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'"
    ).fetchone()[0])
    original_decode = membership_module._decode_state_vector
    original_encode = membership_module._encode_state_vector
    codec_calls = {"decode": 0, "encode": 0}

    def counted_decode(value):
        codec_calls["decode"] += 1
        return original_decode(value)

    def counted_encode(value):
        codec_calls["encode"] += 1
        return original_encode(value)

    monkeypatch.setattr(membership_module, "_decode_state_vector", counted_decode)
    monkeypatch.setattr(membership_module, "_encode_state_vector", counted_encode)
    store._rebuild_member_add_frontiers()

    assert codec_calls["decode"] <= 2 * commit_count
    assert codec_calls["encode"] <= commit_count


@pytest.mark.parametrize("marker_kind", ("missing-column", "older"))
def test_old_member_add_index_version_forces_rebuild(
    marker_kind: str,
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """ROUND 14: a pre-version or older marker is never accepted as current."""

    store = _store(tmp_path, "old-version", stores)
    _genesis(store)
    _set_member(store, USERS[0], active=True, serial=1)
    expected = store._member_latest_add_frontier_scan(USERS[0])
    journal = store._journal_path
    store.close()
    stores.remove(store)

    with sqlite3.connect(journal) as connection:
        connection.execute("DELETE FROM member_add_frontiers")
        if marker_kind == "missing-column":
            connection.execute(
                "ALTER TABLE member_add_frontier_state RENAME TO old_state"
            )
            connection.execute(
                "CREATE TABLE member_add_frontier_state ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1))"
            )
            connection.execute(
                "INSERT INTO member_add_frontier_state(singleton) "
                "SELECT singleton FROM old_state"
            )
            connection.execute("DROP TABLE old_state")
        else:
            connection.execute(
                "UPDATE member_add_frontier_state SET schema_version = 0 "
                "WHERE singleton = 1"
            )

    reopened = _store(tmp_path, "old-version", stores)
    assert reopened.member_latest_add_frontier(USERS[0]) == expected
    assert reopened._db.execute(
        "SELECT schema_version FROM member_add_frontier_state WHERE singleton = 1"
    ).fetchone()[0] == 1


def test_current_member_add_index_version_skips_rebuild(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND 14: a complete current-version index opens without replay."""

    store = _store(tmp_path, "current-version", stores)
    _genesis(store)
    journal = store._journal_path
    store.close()
    stores.remove(store)
    with sqlite3.connect(journal) as connection:
        assert connection.execute(
            "SELECT schema_version FROM member_add_frontier_state "
            "WHERE singleton = 1"
        ).fetchone()[0] == 1

    monkeypatch.setattr(
        LocalSpaceStore,
        "_rebuild_member_add_frontiers",
        lambda _self: pytest.fail("current member-add index was rebuilt"),
    )
    _store(tmp_path, "current-version", stores)


def test_rebuild_fails_closed_when_a_covered_owner_commit_is_missing(
    tmp_path: Path, stores: list[LocalSpaceStore]
) -> None:
    """A missing covered owner commit (compacted lane) must leave the add
    unindexed (None, fail-closed), never index it against a wrong, earlier
    baseline that could let a stale leave through."""

    store = _store(tmp_path, "compacted-lane", stores)
    _genesis(store)
    covered = _set_member(store, USERS[0], active=True, serial=1)
    _set_member(store, USERS[1], active=True, serial=2)
    assert store.member_latest_add_frontier(USERS[1]) is not None

    covered_seq = store._decode_envelope(covered)["seq"]
    with store._db:
        store._db.execute(
            "DELETE FROM commits WHERE doc_id = 'meta' AND seq = ?",
            (covered_seq,),
        )
    store._rebuild_member_add_frontiers()

    assert store.member_latest_add_frontier(USERS[1]) is None

