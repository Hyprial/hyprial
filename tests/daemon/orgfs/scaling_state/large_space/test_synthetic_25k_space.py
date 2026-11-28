"""A 25,000 document space: counts are asserted, wall time and memory are only recorded.

2026-11-25 (C6): production's slow case was a 25,000 document space whose
first start took 155-592 s (#3142).  The space built here has a 50,000 row
tree (half directories, half document rows, rows shaped like
``TreeDocument._wire_node`` output) and 25,000 content documents of one short
text.  Open, one commit, a two-document batch and a tree compaction are timed
and their peak RSS recorded; none of those numbers is asserted.  What is
asserted is the count at the file system seam: the same files and fsyncs as a
100-document space, and reads/writes proportional to the tree file for the
operations that legitimately touch it.

Set ``ORGFS_SCALING_REPORT_DIR`` to also write the record as JSON; set
``ORGFS_SCALING_LARGE_DOCS`` (default 25000) to shrink the space on a slow host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from pycrdt import Doc, Map

from ..conftest import space_path, StoreFactory, assert_same_cost, build_space
from ..fsprobe import FsProbe, fsync_disabled
from ..reference_store import Batch, document_stem
from ..synthetic import content_update, content_updates, edit_tree_row, synthetic_meta_doc, synthetic_tree_doc

MIB = 1 << 20


def _reset_peak() -> None:
    try:
        Path("/proc/self/clear_refs").write_text("5")
    except OSError:
        pass


def _rss_mib() -> tuple[float | None, float | None]:
    try:
        fields = dict(
            line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line
        )
        current = int(fields["VmRSS"].split()[0]) / 1024
        peak = int(fields["VmHWM"].split()[0]) / 1024
        return round(current, 1), round(peak, 1)
    except (OSError, KeyError, ValueError):
        return None, None


class _Timer:
    def __init__(self, record: dict[str, object], name: str) -> None:
        self.record, self.name = record, name

    def __enter__(self) -> "_Timer":
        _reset_peak()
        self.start_rss = _rss_mib()[0]
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        elapsed = time.perf_counter() - self.start
        current, peak = _rss_mib()
        self.record[self.name] = {
            "seconds": round(elapsed, 4),
            "rss_before_mib": self.start_rss,
            "rss_after_mib": current,
            "peak_rss_mib": peak,
        }


def _build_large(factory: StoreFactory, space_dir: Path, documents: int, record: dict[str, object]) -> Doc:
    started = time.perf_counter()
    tree = synthetic_tree_doc(2 * documents)
    tree_update = tree.get_update()
    with fsync_disabled():
        store = factory(space_dir)
        meta_update = synthetic_meta_doc().get_update()
        store.commit(Batch(appends=(("meta", meta_update),)))
        store.compact("meta", meta_update)
        store.commit(Batch(appends=(("tree", tree_update),)))
        store.compact("tree", tree_update)
        for doc_id, update in content_updates(documents):
            store.commit(Batch(appends=((doc_id, update),)))
        store.close()
    record["build_seconds"] = round(time.perf_counter() - started, 2)
    record["tree_rows"] = 2 * documents
    record["tree_state_bytes"] = (space_dir / "tree.state").stat().st_size
    record["content_documents"] = documents
    return tree


def test_open_commit_batch_and_compaction_at_25k_documents(
    state_store_factory, tmp_path, record_property
) -> None:
    documents = int(os.environ.get("ORGFS_SCALING_LARGE_DOCS", "25000"))
    record: dict[str, object] = {}
    space_dir = space_path(tmp_path, "large")
    _build_large(state_store_factory, space_dir, documents, record)
    small_dir = space_path(tmp_path, "small")
    small_ids = build_space(state_store_factory, small_dir, 100, tree_rows=2 * 100)

    probe = FsProbe(space_dir)
    with probe.active():
        with _Timer(record, "open_store_and_load_meta_tree"):
            store = state_store_factory(space_dir)
            assert store.load("meta") is not None
            tree_update = store.load("tree")
        assert tree_update is not None
    open_cost = probe.summary()
    assert probe.bytes_read <= record["tree_state_bytes"] + (space_dir / "meta.state").stat().st_size + 64 * 1024
    assert not any(path.startswith("docs") for path in probe.files_opened)
    assert probe.bytes_written == 0 and probe.fsyncs == 0
    record["open_cost_counts"] = open_cost

    with _Timer(record, "apply_tree_update_to_a_document"):
        live = Doc(client_id=777)
        live.apply_update(tree_update)
        rows = len(live.get("nodes", type=Map))
    record["tree_rows_loaded"] = rows
    assert rows == 2 * documents

    target = f"doc-{documents // 2:08d}-0000-4000-8000-{documents // 2:012d}"
    update = content_update(documents // 2, "committed")
    probe = FsProbe(space_dir)
    with probe.active(), _Timer(record, "commit_one_document"):
        store.commit(Batch(appends=((target, update),)))
    assert probe.fsyncs == 1 and probe.bytes_written == len(update) + 8
    commit_cost = probe.summary()
    record["commit_one_document_counts"] = commit_cost

    small_store = state_store_factory(small_dir)
    small_probe = FsProbe(small_dir)
    with small_probe.active():
        small_store.commit(Batch(appends=((small_ids[50], content_update(50, "committed")),)))
    assert_same_cost([small_probe.summary(), commit_cost])

    tree_delta = edit_tree_row(live, "node-00000010", "renamed-in-batch.md")
    probe = FsProbe(space_dir)
    with probe.active(), _Timer(record, "commit_two_document_batch"):
        store.commit(Batch(appends=((target, content_update(documents // 2, "again")), ("tree", tree_delta))))
    batch_cost = probe.summary()
    record["commit_two_document_batch_counts"] = batch_cost
    assert probe.bytes_read == 0
    assert probe.bytes_written <= 3 * (len(tree_delta) + 400) + 4096, probe.write_bytes

    probe = FsProbe(space_dir)
    with probe.active(), _Timer(record, "compact_tree_with_handed_state"):
        store.compact("tree", live.get_update())
    handed = probe.summary()
    record["compact_tree_handed_counts"] = handed
    assert probe.files_opened <= {"tree.state", "tree.state.tmp", "tree.delta", "."}, probe.files_opened
    state_after = (space_dir / "tree.state").stat().st_size
    assert probe.bytes_written <= state_after + 64
    assert (space_dir / "tree.delta").stat().st_size == 8

    probe = FsProbe(space_dir)
    with probe.active(), _Timer(record, "compact_tree_store_computed_state"):
        store.compact("tree")
    assert probe.files_opened <= {"tree.state", "tree.state.tmp", "tree.delta", "."}, probe.files_opened
    assert probe.bytes_read <= 2 * state_after + 64

    with _Timer(record, "throwaway_doc_load_and_export_of_tree"):
        scratch = Doc()
        scratch.apply_update(store.load("tree") or b"")
        exported = scratch.get_update()
    record["throwaway_export_bytes"] = len(exported)

    print("C6 25k record:", json.dumps(record, indent=1, sort_keys=True))
    record_property("c6_25k_record", record)
    report_dir = os.environ.get("ORGFS_SCALING_REPORT_DIR")
    if report_dir:
        Path(report_dir, "c6-25k-record.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    assert record["tree_state_bytes"] > 0 and document_stem(target)
