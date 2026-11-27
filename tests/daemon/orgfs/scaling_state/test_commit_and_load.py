"""A commit or a load costs what its own documents cost, never what the space costs.

2026-11-25 (C6): design §2.4 makes a single-document commit one delta append
plus one fsync, and a two-document batch one ``batch.pending`` round plus the
two appends.  ``load(doc)`` reads that document's ``state`` and ``delta`` only.
Each test measures N = 100 / 1000 / 10000 spaces at the file system seam and
requires equal counts, and (for appends) equality across how much history the
document already has.
"""

from __future__ import annotations


from pathlib import Path

from pycrdt import Doc, Text

from .conftest import space_path, assert_same_cost, build_space
from .fsprobe import FsProbe
from .reference_store import Batch, document_stem
from .synthetic import content_update, doc_id, synthetic_tree_doc, edit_tree_row


def _files(document: str) -> set[str]:
    stem = document_stem(document)
    return {f"docs/{stem}.state", f"docs/{stem}.delta"}


def _on_disk(space_dir: Path, documents: list[str]) -> int:
    total = 0
    for document in documents:
        paths = (
            [space_dir / f"{document}.state", space_dir / f"{document}.delta"]
            if document in ("meta", "tree")
            else [space_dir / path for path in _files(document)]
        )
        total += sum(path.stat().st_size for path in paths if path.exists())
    return total


def _doc_with_history(records: int) -> tuple[Doc, list[bytes]]:
    doc = Doc(client_id=99)
    text = doc.get("text", type=Text)
    updates: list[bytes] = []
    for index in range(records):
        before = doc.get_state()
        with doc.transaction():
            text += f"line {index:06d}\n"
        updates.append(doc.get_update(before))
    return doc, updates


def _next_update(doc: Doc) -> bytes:
    before = doc.get_state()
    with doc.transaction():
        doc.get("text", type=Text).insert(0, "N")
    return doc.get_update(before)


def test_single_document_commit_is_one_append_and_one_fsync_at_every_size(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        store = state_store_factory(space_dir)
        update = content_update(0, "edited")
        probe = FsProbe(space_dir)
        with probe.active():
            store.commit(Batch(appends=((ids[0], update),)))
        assert probe.fsyncs == 1, (count, probe.file_fsyncs, probe.dir_fsyncs)
        assert probe.bytes_written == len(update) + 8, (count, probe.write_bytes)
        assert probe.files_opened <= _files(ids[0]), (count, probe.files_opened)
        assert probe.renames == [] and probe.unlinks == []
        costs.append(probe.summary())
    assert_same_cost(costs)


def test_single_document_commit_does_not_depend_on_the_documents_history(
    state_store_factory, tmp_path
) -> None:
    costs = {}
    for records in (1, 60, 400):
        space_dir = space_path(tmp_path, f"h{records}")
        build_space(state_store_factory, space_dir, 20)
        store = state_store_factory(space_dir)
        doc, updates = _doc_with_history(records)
        document = doc_id(900)
        for update in updates:
            store.commit(Batch(appends=((document, update),)))
        update = _next_update(doc)
        probe = FsProbe(space_dir)
        with probe.active():
            store.commit(Batch(appends=((document, update),)))
        assert probe.bytes_read == 0, (records, probe.read_bytes)
        costs[records] = (probe.summary(), len(update))
    assert_same_cost([cost[0] for cost in costs.values()])
    for summary, update_len in costs.values():
        assert summary["bytes_written"] == update_len + 8, costs


def test_two_document_batch_writes_the_same_at_every_size(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        tree = synthetic_tree_doc(40)
        store = state_store_factory(space_dir)
        tree_update = edit_tree_row(tree, "node-00000001", "renamed.md")
        content = content_update(1, "second")
        batch = Batch(appends=((ids[1], content), ("tree", tree_update)))
        probe = FsProbe(space_dir)
        with probe.active():
            store.commit(batch)
        allowed = _files(ids[1]) | {
            "tree.delta",
            "batch.pending",
            "batch.pending.tmp",
            ".",
        }
        assert probe.files_opened <= allowed, (count, probe.files_opened)
        assert probe.listings == [], probe.listings
        assert not (space_dir / "batch.pending").exists()
        assert probe.bytes_read <= _on_disk(space_dir, [ids[1], "tree"]), probe.read_bytes
        costs.append(probe.summary())
    assert_same_cost(costs)
    payload = len(content) + len(tree_update)
    assert costs[0]["bytes_written"] <= 3 * payload + 1024, (costs[0], payload)


def test_permanent_delete_batch_unlinks_only_the_deleted_documents_files(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        store = state_store_factory(space_dir)
        tree_update = edit_tree_row(synthetic_tree_doc(40), "node-00000002", "gone.md")
        probe = FsProbe(space_dir)
        with probe.active():
            store.commit(Batch(appends=(("tree", tree_update),), deletes=(ids[2],)))
        assert set(probe.unlinks) - {"batch.pending"} <= _files(ids[2]), (count, probe.unlinks)
        assert probe.listings == [], probe.listings
        for path in _files(ids[2]):
            assert not (space_dir / path).exists()
        assert store.load(ids[3]) is not None
        costs.append(probe.summary())
    assert_same_cost(costs)


def test_load_reads_only_that_documents_files_at_every_size(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        store = state_store_factory(space_dir)
        target = ids[count // 2]
        on_disk = sum((space_dir / path).stat().st_size for path in _files(target) if (space_dir / path).exists())
        probe = FsProbe(space_dir)
        with probe.active():
            update = store.load(target)
        assert update is not None
        assert probe.files_opened <= _files(target), (count, probe.files_opened)
        assert probe.bytes_read <= on_disk, (count, probe.read_bytes, on_disk)
        assert probe.bytes_written == 0 and probe.fsyncs == 0
        assert probe.listings == []
        costs.append(probe.summary())
    assert_same_cost(costs)


def test_load_reads_in_proportion_to_the_documents_own_size(
    state_store_factory, tmp_path
) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 300)
    store = state_store_factory(space_dir)
    reads = {}
    for kib in (1, 16, 256):
        document = doc_id(10_000 + kib)
        doc = Doc(client_id=500 + kib)
        with doc.transaction():
            doc.get("text", type=Text).insert(0, "y" * (kib * 1024))
        store.commit(Batch(appends=((document, doc.get_update()),)))
        store.close()
        store = state_store_factory(space_dir)
        probe = FsProbe(space_dir)
        with probe.active():
            store.load(document)
        reads[kib] = probe.bytes_read
    assert reads[16] > reads[1] * 8 and reads[256] > reads[16] * 8, reads
    assert reads[256] <= 256 * 1024 * 1.1 + 64, reads


def test_load_of_a_missing_document_touches_no_other_document(
    state_store_factory, sizes, tmp_path
) -> None:
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        build_space(state_store_factory, space_dir, count)
        store = state_store_factory(space_dir)
        probe = FsProbe(space_dir)
        with probe.active():
            assert store.load(doc_id(count + 5)) is None
        assert probe.bytes_read == 0 and probe.listings == [], (count, probe.summary())
        assert probe.files_opened <= _files(doc_id(count + 5)), (count, probe.files_opened)


def test_a_torn_delta_tail_is_cut_for_the_cost_of_one_document(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        target = ids[0]
        delta = space_dir / next(p for p in _files(target) if p.endswith(".delta"))
        good = delta.stat().st_size
        with delta.open("ab") as handle:
            handle.write(b"\x00\x00\x00\x40torn-record")
        store = state_store_factory(space_dir)
        probe = FsProbe(space_dir)
        with probe.active():
            assert store.load(target) is not None
        assert delta.stat().st_size == good
        assert probe.files_opened <= _files(target), (count, probe.files_opened)
        costs.append(probe.summary())
    assert_same_cost(costs)
