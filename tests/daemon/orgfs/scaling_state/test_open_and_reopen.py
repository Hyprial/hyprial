"""Opening a space costs the same however many content documents it has.

2026-11-25 (C6): the old open replayed a journal and re-checkpointed every
document (155-592 s for a 25,000 document space, #3142).  The new open reads
``meta`` and ``tree`` only.  These tests count files and bytes at the file
system seam for N = 100 / 1000 / 10000 content documents and require the
counts to be equal, not merely small.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import space_path, StoreFactory, assert_same_cost, build_space, write_pending_batch
from .fsprobe import FsProbe
from .reference_store import Batch
from .synthetic import content_update

SPACE_JSON_SLACK = 1024

_ALLOWED_ON_OPEN = {
    "space.json",
    "batch.pending",
    "meta.state",
    "meta.delta",
    "tree.state",
    "tree.delta",
}


def _open_space(factory: StoreFactory, space_dir: Path) -> dict[str, object]:
    probe = FsProbe(space_dir)
    with probe.active():
        store = factory(space_dir)
        assert store.load("meta") is not None
        assert store.load("tree") is not None
        store.close()
    return {"summary": probe.summary(), "probe": probe}


def _meta_tree_bytes(space_dir: Path) -> int:
    return sum(
        (space_dir / f"{name}.{kind}").stat().st_size
        for name in ("meta", "tree")
        for kind in ("state", "delta")
        if (space_dir / f"{name}.{kind}").exists()
    )


def test_open_touches_only_meta_tree_and_pending_whatever_the_document_count(
    state_store_factory, sizes, tmp_path
) -> None:
    results = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        build_space(state_store_factory, space_dir, count)
        results.append((count, _open_space(state_store_factory, space_dir)))

    for count, result in results:
        probe: FsProbe = result["probe"]
        assert probe.files_opened <= _ALLOWED_ON_OPEN, (count, probe.files_opened)
        assert not any(path.startswith("docs") for path in probe.listings), (count, probe.listings)
        assert probe.bytes_read <= _meta_tree_bytes(space_path(tmp_path, f"n{count}")) + SPACE_JSON_SLACK, count
    summaries = [result["summary"] for _, result in results]
    assert_same_cost(summaries)


def test_open_reads_grow_with_the_tree_not_with_the_content_documents(
    state_store_factory, tmp_path
) -> None:
    small = space_path(tmp_path, "small-tree")
    large = space_path(tmp_path, "large-tree")
    build_space(state_store_factory, small, 200, tree_rows=40)
    build_space(state_store_factory, large, 200, tree_rows=4000)

    small_read = _open_space(state_store_factory, small)["probe"].bytes_read
    large_read = _open_space(state_store_factory, large)["probe"].bytes_read

    assert large_read > small_read * 20
    assert large_read <= _meta_tree_bytes(large) + SPACE_JSON_SLACK


def test_clean_reopen_writes_nothing(state_store_factory, sizes, tmp_path) -> None:
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        build_space(state_store_factory, space_dir, count)
        result = _open_space(state_store_factory, space_dir)
        probe: FsProbe = result["probe"]
        assert probe.bytes_written == 0, (count, probe.write_bytes)
        assert probe.fsyncs == 0, (count, probe.file_fsyncs, probe.dir_fsyncs)
        assert probe.renames == [] and probe.unlinks == [], (count, probe.renames, probe.unlinks)
        assert not probe.truncates and not probe.opens_for_write, (count, probe.opens_for_write)


def test_reopen_after_commit_and_compaction_still_writes_nothing(
    state_store_factory, tmp_path
) -> None:
    space_dir = space_path(tmp_path, "space")
    ids = build_space(state_store_factory, space_dir, 50)
    store = state_store_factory(space_dir)
    store.commit(Batch(appends=((ids[0], content_update(0, "again")),)))
    store.compact(ids[0])
    store.close()

    probe: FsProbe = _open_space(state_store_factory, space_dir)["probe"]

    assert probe.bytes_written == 0 and probe.fsyncs == 0
    assert probe.files_opened <= _ALLOWED_ON_OPEN


def test_recovering_a_pending_batch_costs_the_batch_not_the_space(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        appends = [(ids[0], content_update(0, "recovered")), (ids[1], content_update(1, "recovered"))]
        write_pending_batch(space_dir, appends)
        probe = FsProbe(space_dir)
        with probe.active():
            store = state_store_factory(space_dir)
        assert store.recovered_batch is True
        assert not (space_dir / "batch.pending").exists()
        assert not any(
            path.startswith("docs") and path not in {_delta(ids[0]), _delta(ids[1])}
            for path in probe.files_opened
        ), (count, probe.files_opened)
        costs.append(probe.summary())
    assert_same_cost(costs)


def test_a_torn_pending_batch_is_discarded_without_touching_documents(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        write_pending_batch(space_dir, [(ids[0], content_update(0, "never"))], torn=True)
        probe = FsProbe(space_dir)
        with probe.active():
            store = state_store_factory(space_dir)
        assert store.recovered_batch is False
        assert not (space_dir / "batch.pending").exists()
        assert not any(path.startswith("docs") for path in probe.files_opened)
        costs.append(probe.summary())
    assert_same_cost(costs)


def _delta(document: str) -> str:
    from .reference_store import document_stem

    return f"docs/{document_stem(document)}.delta"
