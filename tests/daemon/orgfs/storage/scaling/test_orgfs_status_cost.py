"""orgfs.status must count unconfirmed commits without reading the journal.

Decision record (2026-11-23): status used to decode every committed envelope
in the space while holding the per-space write lock.  One 322k-commit space
took 13-16.5 s, above the CLI's 15 s limit, with no corruption involved.  The
count is now materialized: each journal row records its envelope's origin node
at insert time, commit_stats moves in the same transaction as every insert and
retired-document delete, and covered rows are tracked in memory.

Acceptance (Allen 2026-11-23 09:44): judge algorithmic complexity, not a row
count or a wall-clock threshold.  These tests measure deterministic work
(fetched rows, envelope decodes, SQL statements) at three journal sizes and
require constant growth; they also pin equivalence with the old journal
oracle, the one-time classification of rows written before origin_node
existed, and retirement accounting.  The same oracle pins an empty journal, a
holder re-announcement after restart, a two-way imported fork merge that
converges both stores to one frontier, a corrupt legacy row, and migration
idempotence across reopen, including recompute from a persisted uninitialized
marker.  Deterministic work for a holder change is bounded by the changed
document, and the residual per-document scan is pinned at three sizes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, StoreError
from tests.support.scaling import CountingConnection, assert_work_growth


_DOC = "doc-00000000-0000-0000-0000-000000000031"
_SECOND_DOC = "doc-00000000-0000-0000-0000-000000000032"
_TREE = "tree-00000000-0000-0000-0000-000000000031"
_SPACE = "space-status"
_HEX = "0123456789abcdef0123456789abcdef"


def _replace(value: str):
    def mutate(doc: Doc) -> None:
        text = doc.get("text", type=Text)
        del text[:]
        text += value

    return mutate


def _new_store(path: Path, commits: int = 0) -> LocalSpaceStore:
    store = LocalSpaceStore(path, _SPACE, node_id="node-status")
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:status"),
            doc["space"].__setitem__("spaceId", _SPACE),
            doc["space"].__setitem__("name", "status"),
            doc["members"].__setitem__(
                "user:status", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree",
                Map(
                    {
                        "active": _TREE,
                        "activatedAt": "genesis",
                        "planId": None,
                    }
                ),
            ),
        ),
        author="user:status",
        actor=None,
    )
    store.commit(
        _DOC,
        lambda doc: doc.__setitem__("text", Text("0")),
        author="user:status",
        actor="agent:status",
    )
    for index in range(commits):
        store.commit(
            _DOC,
            _replace(str(index + 1)),
            author="user:status",
            actor="agent:status",
        )
    return store


def _add_second_document(store: LocalSpaceStore) -> None:
    store.commit(
        _SECOND_DOC,
        lambda doc: doc.__setitem__("text", Text("0")),
        author="user:status",
        actor="agent:status",
    )


def _oracle(store: LocalSpaceStore) -> int:
    """The pre-2026-11-23 algorithm: decode every row, then apply holders."""

    with store._lock:  # noqa: SLF001
        holders = [
            frontiers
            for durable, frontiers in store._holders.values()  # noqa: SLF001
            if durable
        ]
        local_rows = []
        for row in store._db.execute("SELECT * FROM commits ORDER BY rowid"):  # noqa: SLF001
            try:
                origin = store._decode_envelope(bytes(row["envelope_bytes"]))[  # noqa: SLF001
                    "origin"
                ]
            except StoreError:
                continue
            if str(origin.get("node")) == store.node_id:
                local_rows.append(row)
        if not holders:
            return len(local_rows)
        count = 0
        for row in local_rows:
            doc_id = str(row["doc_id"])
            if not any(
                store._covered(  # noqa: SLF001
                    bytes(row["version"]), frontiers.get(doc_id, b"")
                )
                for frontiers in holders
            ):
                count += 1
        return count


def _all(store: LocalSpaceStore, doc_id: str) -> list[bytes]:
    """Every journaled envelope for one document, in rowid order."""

    return [
        bytes(row[0])
        for row in store._db.execute(  # noqa: SLF001
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
            (doc_id,),
        )
    ]


def test_materialized_count_matches_the_journal_oracle(tmp_path: Path) -> None:
    store = _new_store(tmp_path / "oracle", commits=6)
    try:
        assert store.unconfirmed_commits() == _oracle(store)
        assert store.unconfirmed_commits() >= 8

        early = store.frontier(_DOC)
        store.record_holder_frontier("node-peer", True, {_DOC: early})
        assert store.unconfirmed_commits() == _oracle(store)

        store.commit(
            _DOC,
            _replace("after-early-frontier"),
            author="user:status",
            actor="agent:status",
        )
        assert store.unconfirmed_commits() == _oracle(store)

        current = store.frontier(_DOC)
        store.record_holder_frontier("node-peer", True, {_DOC: current})
        assert store.unconfirmed_commits() == _oracle(store)

        store.record_holder_frontier("node-peer", False, {_DOC: current})
        assert store.unconfirmed_commits() == _oracle(store)

        store.record_holder_frontier("node-peer", True, {_DOC: early})
        assert store.unconfirmed_commits() == _oracle(store)

        store.record_holder_frontier("node-peer", True, {})
        assert store.unconfirmed_commits() == _oracle(store)

        store.record_holder_frontier("node-other", True, {_DOC: current})
        assert store.unconfirmed_commits() == _oracle(store)

        _add_second_document(store)
        assert store.unconfirmed_commits() == _oracle(store)

        store.commit_many(
            [
                (_DOC, _replace("many-first")),
                (_SECOND_DOC, _replace("many-second")),
            ],
            author="user:status",
            actor="agent:status",
        )
        assert store.unconfirmed_commits() == _oracle(store)
    finally:
        store.close()


def _status_work(path: Path, commits: int):
    """Run one facade status and report its count plus deterministic work."""

    store = _new_store(path, commits=commits)
    facade = LocalOrgFs(stores=store, author="user:status")
    try:
        space_id = facade.create_space("scale").space_id
        facade.write_text(space_id, "probe.md", "seed")
        expected = store.unconfirmed_commits()
        raw = store._db  # noqa: SLF001
        counted = CountingConnection(raw)
        store._db = counted  # noqa: SLF001
        decodes = 0
        real_decode = LocalSpaceStore._decode_envelope

        def counting_decode(self, payload: bytes):
            nonlocal decodes
            decodes += 1
            return real_decode(self, payload)

        LocalSpaceStore._decode_envelope = counting_decode
        try:
            status = facade.status(space_id)
        finally:
            LocalSpaceStore._decode_envelope = real_decode
            store._db = raw  # noqa: SLF001
        return (
            status.unconfirmed_commits,
            expected,
            counted.rows_fetched,
            decodes,
            counted.statements,
        )
    finally:
        assert facade.close_effects()
        store.close()


def test_status_work_is_constant_across_journal_sizes(tmp_path: Path) -> None:
    sizes = (40, 160, 640)
    work = {size: _status_work(tmp_path / f"scale-{size}", size) for size in sizes}

    for size, (count, expected, *_rest) in work.items():
        assert count == expected, size
    counts = [work[size][0] for size in sizes]
    assert counts[0] < counts[-1]
    rows = assert_work_growth(
        lambda size: work[size][2], sizes, label="rows fetched by status"
    )
    decodes = assert_work_growth(
        lambda size: work[size][3], sizes, label="envelopes decoded by status"
    )
    statements = assert_work_growth(
        lambda size: work[size][4], sizes, label="SQL statements issued by status"
    )
    assert rows == (1, 1, 1)
    assert decodes == (0, 0, 0)
    assert set(statements) == {2}


def test_legacy_rows_are_classified_once_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy"
    store = _new_store(path, commits=5)
    try:
        expected = _oracle(store)
        with store._lock, store._db:  # noqa: SLF001
            store._db.execute("UPDATE commits SET origin_node = NULL")  # noqa: SLF001
            store._db.execute(  # noqa: SLF001
                "UPDATE commit_stats SET local_total = 0, initialized = 0"
            )

        calls = 0
        real_decode = LocalSpaceStore._decode_envelope

        def counting_decode(self, raw: bytes):
            nonlocal calls
            calls += 1
            return real_decode(self, raw)

        monkeypatch.setattr(LocalSpaceStore, "_decode_envelope", counting_decode)
        store._init_commit_stats()  # noqa: SLF001
        monkeypatch.undo()

        assert calls == 0
        assert store.unconfirmed_commits() == expected
        initialized = store._db.execute(  # noqa: SLF001
            "SELECT initialized FROM commit_stats"
        ).fetchone()[0]
        assert initialized == 1
    finally:
        store.close()

    reopened = LocalSpaceStore(path, _SPACE, node_id="node-status")
    try:
        assert reopened.unconfirmed_commits() == expected
        nulls = reopened._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM commits WHERE origin_node IS NULL"
        ).fetchone()[0]
        assert nulls == expected
    finally:
        reopened.close()


def test_writer_classification_splits_node_from_lane() -> None:
    from hyprial.daemon.impl.orgfs.storage.store.retirement import (
        _writer_belongs_to_node,
    )

    lane = f"node.status.{_HEX}.lane-7"
    assert _writer_belongs_to_node(lane, "node.status")
    assert not _writer_belongs_to_node(lane, "node")
    assert not _writer_belongs_to_node(lane, "node.status.")
    assert not _writer_belongs_to_node(f"node.status2.{_HEX}.lane-7", "node.status")
    assert not _writer_belongs_to_node(f"node.status.{_HEX[:-1]}.lane-7", "node.status")
    assert not _writer_belongs_to_node(f"node.status.{_HEX.upper()}.lane-7", "node.status")
    assert not _writer_belongs_to_node(f"node.status.{_HEX}.lane-x", "node.status")
    assert not _writer_belongs_to_node(f"node.status.{_HEX}", "node.status")
    assert not _writer_belongs_to_node(f"node.status.{_HEX}.other-7", "node.status")


def test_retiring_a_document_moves_the_materialized_count(tmp_path: Path) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "retire", node_id="node-status", author="user:status"
    )
    try:
        space_id = runtime.facade.create_space("retire").space_id
        node = runtime.facade.write_text(space_id, "note.md", "P2R-STATUS-SECRET")
        runtime.facade.write_text(space_id, f"id:{node.node_id}", "safe")
        store = runtime.stores.get(space_id)
        store.snapshot(node.doc_id, shallow_since=None)
        before = store.unconfirmed_commits()
        assert before == _oracle(store)

        plan = runtime.facade.purge_plan(
            space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": node.version,
                    "to": node.version,
                }
            ],
        )
        runtime.facade.purge(space_id, plan.plan_id)
        assert store.retired(node.doc_id) is not None
        assert store.unconfirmed_commits() == _oracle(store)

        store.delete_retired_objects(node.doc_id)
        after = store.unconfirmed_commits()
        assert after == _oracle(store)
        assert store.retired_residue(node.doc_id) == ()

        store.delete_retired_objects(node.doc_id)
        assert store.unconfirmed_commits() == after == _oracle(store)
    finally:
        runtime.close()


def test_empty_space_counts_zero_without_reading_commits(tmp_path: Path) -> None:
    store = LocalSpaceStore(tmp_path / "empty", _SPACE, node_id="node-status")
    try:
        assert store.unconfirmed_commits() == 0 == _oracle(store)
        raw = store._db  # noqa: SLF001
        counted = CountingConnection(raw)
        decodes = 0
        real_decode = LocalSpaceStore._decode_envelope

        def counting_decode(self, payload: bytes):
            nonlocal decodes
            decodes += 1
            return real_decode(self, payload)

        store._db = counted  # noqa: SLF001
        LocalSpaceStore._decode_envelope = counting_decode
        try:
            assert store.unconfirmed_commits() == 0
        finally:
            LocalSpaceStore._decode_envelope = real_decode
            store._db = raw  # noqa: SLF001
        assert counted.rows_fetched == 1
        assert counted.statements == 1
        assert decodes == 0
    finally:
        store.close()


def test_holder_reannouncement_after_reopen_matches_the_oracle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart"
    store = _new_store(path, commits=3)
    try:
        early = store.frontier(_DOC)
        store.commit(
            _DOC,
            _replace("late"),
            author="user:status",
            actor="agent:status",
        )
        store.record_holder_frontier("node-peer", True, {_DOC: early})
        assert store.unconfirmed_commits() == _oracle(store)
        assert store.unconfirmed_commits() > 0
    finally:
        store.close()

    reopened = LocalSpaceStore(path, _SPACE, node_id="node-status")
    try:
        without_holders = reopened.unconfirmed_commits()
        assert without_holders == _oracle(reopened)
        assert reopened.holders_seen() == ()

        current = reopened.frontier(_DOC)
        reopened.record_holder_frontier("node-peer", True, {_DOC: current})
        assert reopened.unconfirmed_commits() == _oracle(reopened)
        assert reopened.unconfirmed_commits() < without_holders

        reopened.record_holder_frontier("node-peer", True, {_DOC: early})
        assert reopened.unconfirmed_commits() == _oracle(reopened)
        assert reopened.unconfirmed_commits() > 0
    finally:
        reopened.close()


def test_legacy_corrupt_row_is_counted_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_store(tmp_path / "corrupt", commits=3)
    try:
        expected = _oracle(store)
        with store._lock, store._db:  # noqa: SLF001
            store._db.execute("UPDATE commits SET origin_node = NULL")  # noqa: SLF001
            store._db.execute(  # noqa: SLF001
                "UPDATE commit_stats SET local_total = 0, initialized = 0"
            )
            store._db.execute(  # noqa: SLF001
                "INSERT INTO commits"
                "(writer, seq, doc_id, envelope_bytes, version, origin_node, committed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    f"node-status.{_HEX}.lane-9",
                    0,
                    _DOC,
                    b"{not-json",
                    b"\x00",
                    "2026-11-23T00:00:00Z",
                ),
            )

        calls = 0
        real_decode = LocalSpaceStore._decode_envelope

        def counting_decode(self, raw: bytes):
            nonlocal calls
            calls += 1
            return real_decode(self, raw)

        monkeypatch.setattr(LocalSpaceStore, "_decode_envelope", counting_decode)
        store._init_commit_stats()  # noqa: SLF001
        monkeypatch.undo()

        assert calls == 0
        assert store.unconfirmed_commits() == expected + 1
        assert _oracle(store) == expected
    finally:
        store.close()


def test_migration_is_idempotent_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "idempotent"
    store = _new_store(path, commits=4)
    try:
        total = store._db.execute(  # noqa: SLF001
            "SELECT local_total FROM commit_stats"
        ).fetchone()[0]
        assert store.unconfirmed_commits() == total == _oracle(store)

        with store._lock, store._db:  # noqa: SLF001
            store._db.execute(  # noqa: SLF001
                "UPDATE commit_stats SET local_total = 0, initialized = 0"
            )
        store._init_commit_stats()  # noqa: SLF001
        assert store.unconfirmed_commits() == total

        raw = store._db  # noqa: SLF001
        counted = CountingConnection(raw)
        store._db = counted  # noqa: SLF001
        try:
            store._init_commit_stats()  # noqa: SLF001
        finally:
            store._db = raw  # noqa: SLF001
        assert counted.statements == 1
        assert counted.rows_fetched == 1
        assert store.unconfirmed_commits() == total
    finally:
        store.close()

    reopened = LocalSpaceStore(path, _SPACE, node_id="node-status")
    try:
        assert reopened.unconfirmed_commits() == total == _oracle(reopened)
        with reopened._lock, reopened._db:  # noqa: SLF001
            reopened._db.execute(  # noqa: SLF001
                "UPDATE commit_stats SET local_total = 0, initialized = 0"
            )
    finally:
        reopened.close()

    resumed = LocalSpaceStore(path, _SPACE, node_id="node-status")
    try:
        assert resumed.unconfirmed_commits() == total == _oracle(resumed)
    finally:
        resumed.close()


def test_imported_fork_merges_without_moving_the_local_count(
    tmp_path: Path,
) -> None:
    local = _new_store(tmp_path / "local", commits=3)
    peer = LocalSpaceStore(tmp_path / "peer", _SPACE, node_id="node-peer")
    try:
        for envelope in _all(local, "meta"):
            result = peer.import_envelope(envelope, supplier="local")
            assert result.applied, result

        peer.commit(
            _DOC,
            lambda doc: doc.__setitem__("peer", Text("p")),
            author="user:status",
            actor="agent:status",
        )
        local.commit(
            _DOC,
            lambda doc: doc.__setitem__("local", Text("l")),
            author="user:status",
            actor="agent:status",
        )
        before = local.unconfirmed_commits()
        assert before == _oracle(local)

        local_meta_rows = len(_all(local, "meta"))
        for envelope in _all(peer, "meta")[local_meta_rows:]:
            result = local.import_envelope(envelope, supplier="peer")
            assert result.applied, result
        forked = _all(peer, _DOC)
        assert len(forked) == 1
        result = local.import_envelope(forked[0], supplier="peer")
        assert result.applied, result

        rows = local._db.execute(  # noqa: SLF001
            "SELECT writer, origin_node FROM commits "
            "WHERE writer LIKE 'node-peer.%' AND doc_id = ?",
            (_DOC,),
        ).fetchall()
        assert len(rows) == 1
        assert str(rows[0][1]) == "node-peer"
        assert local.unconfirmed_commits() == before == _oracle(local)

        page = local.export_since(
            _DOC, peer.frontier(_DOC), cursor=None, max_bytes=1 << 20
        )
        for envelope in page.envelopes:
            result = peer.import_envelope(envelope, supplier="local")
            assert result.applied, result
        assert peer.frontier(_DOC) == local.frontier(_DOC)
        assert peer.unconfirmed_commits() == _oracle(peer)

        local.commit(
            _DOC,
            _replace("after-merge"),
            author="user:status",
            actor="agent:status",
        )
        assert local.unconfirmed_commits() == before + 1 == _oracle(local)
    finally:
        peer.close()
        local.close()


def _holder_scan_rows(path: Path, commits: int, target: str) -> int:
    store = _new_store(path, commits=commits)
    try:
        _add_second_document(store)
        store.commit(
            _SECOND_DOC,
            _replace("second-move"),
            author="user:status",
            actor="agent:status",
        )
        frontier = store.frontier(target)
        raw = store._db  # noqa: SLF001
        counted = CountingConnection(raw)
        store._db = counted  # noqa: SLF001
        try:
            store.record_holder_frontier("node-peer", True, {target: frontier})
        finally:
            store._db = raw  # noqa: SLF001
        assert store.unconfirmed_commits() == _oracle(store)
        return counted.rows_fetched
    finally:
        store.close()


def test_holder_change_scans_only_the_changed_document(tmp_path: Path) -> None:
    sizes = (40, 160, 640)
    untouched = assert_work_growth(
        lambda size: _holder_scan_rows(
            tmp_path / f"bystander-{size}", size, _SECOND_DOC
        ),
        sizes,
        label="rows scanned for an unchanged large document",
    )
    assert untouched == (2, 2, 2)

    changed = tuple(
        _holder_scan_rows(tmp_path / f"changed-{size}", size, _DOC) for size in sizes
    )
    assert changed == tuple(size + 1 for size in sizes)
