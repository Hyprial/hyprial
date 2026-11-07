"""The offline compaction tool analyzes, applies with a backup, and restores on failure.

Determinism fix (2026-11-03, workflow wf-ff332cef #implDF): _make_space used
``with sqlite3.connect(...)`` for the broadcast_at backfill and relied on
refcount collection to close the connection. The Connection survives the
function (it stays GC-tracked) and its finalization checkpoints the WAL into
journal.sqlite3 at an arbitrary later moment -- locally demonstrated by a
gc.collect() between the two digest reads flipping the digest, and observed
in CI (run 45309) when analyze's allocation churn triggered that collection
between the before/after snapshots, failing assert before == after on a
dry-run tool that never writes. The fixture now closes the connection
explicitly, so the checkpoint lands inside _make_space, before the test
reads the journal; analyze keeps its mode=ro connection and still cannot
mutate anything.

File-level digests (2026-11-03, workflow wf-2b3f130c #implDF, work item 0089):
the fixture fix removed one checkpoint trigger, but the three remaining
raw-byte/whole-file-sha256 assertions on journal.sqlite3 stayed fragile: a
WAL-mode SQLite file has no stable byte identity, because the last-connection
checkpoint merges -wal frames into the main file whenever the finalizing
close runs, and _restore copies back exactly the bytes the backup captured at
its own moment. Equal logical content can therefore live behind different
bytes -- observed three ways in CI (dry-run digest flip on #2319 run 45309,
again on #2327 run 45572, and bytes-differ-at-index-4102 on #2325 run 46043
in test_apply_self_check_failure_restores_the_backup). Every raw-byte and
whole-file-hash comparison is replaced by a logical snapshot: PRAGMA
integrity_check plus the schema and all rows of every table ordered by rowid.
The properties are unchanged: dry-run does not mutate the journal; a failed
apply restores the pre-apply logical state.

Schema-capture fix (2026-11-03, workflow wf-ea63661e r3 review): the first
snapshot version only read table *names* from sqlite_master, so a schema-only
mutation -- reviewer probe: DROP INDEX commits_doc between the two snapshots --
left before == after and the dry-run property proved less than before this
file's refactor. The snapshot now also captures the full ordered
sqlite_master(type, name, tbl_name, sql) rows, so a dropped or added index,
trigger, or view changes the snapshot even though no table row moves. A
negative control injects the reviewer's DROP INDEX and requires the snapshot
to detect it. Raw bytes and whole-file hashes stay retired.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "orgfs_compact_journal.py"
SPEC = importlib.util.spec_from_file_location("orgfs_compact_journal_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SPACE = "11111111-1111-4111-8111-111111111111"
TREE = "tree-00000000-0000-0000-0000-000000000002"


def _make_space(tmp_path: Path) -> Path:
    store = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
    try:
        store.commit(
            "meta",
            lambda doc: (
                doc["space"].update(
                    {"owner": "user:owner", "spaceId": SPACE, "name": "team"}
                ),
                doc["members"].__setitem__(
                    "user:owner", Map({"mode": "rw", "removedAt": None})
                ),
                doc["docs"].__setitem__("tree", Map({"active": TREE, "planId": None})),
            ),
            author="user:owner",
            actor=None,
        )
        for index in range(3):
            store.commit(
                TREE,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
    finally:
        store.close()
    space = tmp_path / "orgfs" / "spaces" / SPACE
    db = sqlite3.connect(space / "journal.sqlite3")
    try:
        with db:
            db.execute("UPDATE commits SET broadcast_at = '2026-10-29T00:00:00Z'")
    finally:
        db.close()
    return space


def _journal_snapshot(
    journal: Path,
) -> tuple[
    str,
    tuple[tuple[str, str, str, str | None], ...],
    tuple[tuple[str, tuple], ...],
]:
    """Capture integrity, full schema rows, and every table's rows by rowid.

    Schema rows are sqlite_master(type, name, tbl_name, sql) ordered by
    (type, name), so a schema-only change -- a dropped or added index,
    trigger, or view, including sqlite_autoindex entries -- changes the
    snapshot even though no table row moves.
    """
    integrity = ""
    schema: tuple[tuple[str, str, str, str | None], ...] = ()
    db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        schema = tuple(
            (str(row[0]), str(row[1]), str(row[2]), row[3])
            for row in db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
                " ORDER BY type, name"
            )
        )
        tables = [
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = tuple(
            (
                table,
                tuple(
                    map(tuple, db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
                ),
            )
            for table in tables
        )
    finally:
        db.close()
    return integrity, schema, contents


def test_snapshot_detects_a_schema_only_change(tmp_path: Path) -> None:
    space = _make_space(tmp_path)
    journal = space / "journal.sqlite3"
    before = _journal_snapshot(journal)
    db = sqlite3.connect(journal)
    try:
        with db:
            db.execute("DROP INDEX commits_doc")
    finally:
        db.close()
    after = _journal_snapshot(journal)

    assert after != before


def test_dry_run_reports_reclaimable_rows_and_does_not_touch_files(tmp_path: Path) -> None:
    space = _make_space(tmp_path)
    journal = space / "journal.sqlite3"
    before = _journal_snapshot(journal)
    report = MODULE.analyze(space)
    after = _journal_snapshot(journal)

    assert report["dryRun"] is True
    assert report["reclaimRows"] == 5
    assert report["reclaimBytes"] > 0
    assert report["protocol"]["baselineSnapshotInstallable"] is False
    assert before == after
    tree = next(item for item in report["documents"] if item["docId"] == TREE)
    assert tree["missingMaterialized"] is False
    assert tree["reclaimRows"] == 3
    assert tree["reclaimBytes"] > 0


def test_apply_backs_up_deletes_and_preserves_materialized_state(tmp_path: Path) -> None:
    space = _make_space(tmp_path)
    store = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
    try:
        before_state = store._doc(TREE).get_state()
    finally:
        store.close()

    report = MODULE.apply_compaction(space, accept_lagging_peers=True)

    assert report["ok"] is True
    assert report["dryRun"] is False
    assert 0 < report["deletedRows"] < 5
    assert Path(report["backup"]).is_dir()
    assert all(item["beforeHash"] == item["afterHash"] for item in report["documents"])

    reopened = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
    try:
        assert reopened._doc(TREE).get_state() == before_state
        assert reopened._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] < 5
    finally:
        reopened.close()


def test_apply_restores_the_backup_when_persistence_fails(
    tmp_path: Path, monkeypatch
) -> None:
    space = _make_space(tmp_path)
    journal = space / "journal.sqlite3"
    before = _journal_snapshot(journal)
    real = MODULE.LocalSpaceStore

    class FailingStore(real):
        def _persist_all_documents(self) -> None:
            raise RuntimeError("forced persistence failure")

    monkeypatch.setattr(MODULE, "LocalSpaceStore", FailingStore)
    try:
        MODULE.apply_compaction(space, accept_lagging_peers=True)
    except RuntimeError as error:
        assert "forced persistence failure" in str(error)
    else:
        raise AssertionError("compaction should have failed")
    assert _journal_snapshot(journal) == before


def test_apply_refuses_while_a_daemon_socket_exists(tmp_path: Path) -> None:
    space = _make_space(tmp_path)
    socket = tmp_path / "daemon.sock"
    socket.write_text("", encoding="utf-8")
    try:
        MODULE.apply_compaction(space, accept_lagging_peers=True)
    except RuntimeError as error:
        assert "stop the daemon" in str(error)
    else:
        raise AssertionError("apply must refuse while the daemon socket exists")


def test_apply_requires_explicit_lagging_peer_acceptance(tmp_path: Path) -> None:
    space = _make_space(tmp_path)
    try:
        MODULE.apply_compaction(space)
    except RuntimeError as error:
        assert "--accept-lagging-peers" in str(error)
    else:
        raise AssertionError("apply must require explicit acceptance")


def test_apply_leaves_backups_outside_spaces_and_daemon_start_succeeds(
    tmp_path: Path,
) -> None:
    space = _make_space(tmp_path)
    report = MODULE.apply_compaction(space, accept_lagging_peers=True)
    backup = Path(report["backup"])
    assert backup.is_dir()
    assert not backup.is_relative_to(tmp_path / "orgfs" / "spaces")
    assert backup.parent == tmp_path / "orgfs" / "compaction-backups"

    runtime = OrgFsRuntime(tmp_path, node_id="node-a", author="user:owner")
    try:
        assert space.name in runtime.stores
    finally:
        runtime.close()


def test_startup_rejects_a_non_uuid_directory_under_spaces(tmp_path: Path) -> None:
    stray = tmp_path / "orgfs" / "spaces" / "not-a-uuid"
    stray.mkdir(parents=True)
    with pytest.raises(StoreError) as raised:
        OrgFsRuntime(tmp_path, node_id="node-a", author="user:owner")
    assert raised.value.code == "ORGFS_INVALID_SPACE_DIRECTORY"
    assert "not-a-uuid" in str(raised.value)


def test_apply_self_check_failure_restores_the_backup(
    tmp_path: Path, monkeypatch
) -> None:
    space = _make_space(tmp_path)
    journal = space / "journal.sqlite3"
    before = _journal_snapshot(journal)
    real = MODULE.LocalSpaceStore
    calls = 0

    class FailsOnSelfCheck(real):
        def __init__(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced self-check open failure")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(MODULE, "LocalSpaceStore", FailsOnSelfCheck)
    with pytest.raises(RuntimeError, match="self-check"):
        MODULE.apply_compaction(space, accept_lagging_peers=True)
    assert _journal_snapshot(journal) == before
