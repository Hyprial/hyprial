"""Compaction returns a document's delta to its magic and bounds disk use.

2026-11-25 (C6): the writes below do not bump the commit clock, because
together with the clock a document's state grows ~13 B per commit even after
compaction (test_history_growth, report "设计问题" 1); this file pins the
store's own bounds.  Design §2.3 compacts a document when its delta reaches 500
records or 1 MiB.  The coordinator (here: the test) hands the authoritative
doc's ``get_update()`` to ``compact``.  Writing K >> 500 times with
auto-compaction must leave a disk footprint that is bounded and does not grow
with K when the content does not grow.
"""

from __future__ import annotations

from pathlib import Path

from pycrdt import Doc, Map, Text

from ..conftest import space_path, StoreFactory, assert_same_cost, build_space
from ..fsprobe import FsProbe
from ..reference_store import Batch, document_stem
from ..synthetic import doc_id

DELTA_MAGIC_BYTES = 8


def _paths(space_dir: Path, document: str) -> tuple[Path, Path]:
    stem = document_stem(document)
    return space_dir / "docs" / f"{stem}.state", space_dir / "docs" / f"{stem}.delta"


def _footprint(space_dir: Path, document: str) -> int:
    return sum(path.stat().st_size for path in _paths(space_dir, document) if path.exists())


def _overwrite(doc: Doc, body: str, *, clock: bool = False) -> bytes:
    """Replace the text; ``clock`` also bumps ``__orgfs__.c`` (§3.3), see test_history_growth."""

    before = doc.get_state()
    text = doc.get("text", type=Text)
    stamp = doc.get("__orgfs__", type=Map)
    with doc.transaction():
        if len(text):
            del text[0 : len(text)]
        text += body
        if clock:
            stamp["c"] = int(stamp["c"] if "c" in stamp else 0) + 1
    return doc.get_update(before)


def _drive(
    factory: StoreFactory,
    space_dir: Path,
    document: str,
    writes: int,
    *,
    body: str,
    probe_every: int = 0,
) -> tuple[int, int, int, list[tuple[int, int]]]:
    store = factory(space_dir)
    doc = Doc(client_id=4242)
    peak = compactions = 0
    curve: list[tuple[int, int]] = []
    for step in range(1, writes + 1):
        store.commit(Batch(appends=((document, _overwrite(doc, body)),)))
        if store.needs_compaction(document):
            store.compact(document, doc.get_update())
            compactions += 1
        peak = max(peak, _footprint(space_dir, document))
        if probe_every and step % probe_every == 0:
            curve.append((step, _footprint(space_dir, document)))
    return peak, _footprint(space_dir, document), compactions, curve


def test_compact_returns_the_delta_to_its_magic_and_keeps_the_content(
    state_store_factory, tmp_path
) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 5)
    store = state_store_factory(space_dir)
    document = doc_id(800)
    doc = Doc(client_id=77)
    for index in range(30):
        store.commit(Batch(appends=((document, _overwrite(doc, f"version {index}")),)))
    expected = str(doc.get("text", type=Text))

    store.compact(document, doc.get_update())

    state_path, delta_path = _paths(space_dir, document)
    assert delta_path.stat().st_size == DELTA_MAGIC_BYTES
    assert state_path.read_bytes()[:8] == b"ORGFSST3"
    reloaded = Doc()
    reloaded.apply_update(state_factory_load(state_store_factory, space_dir, document))
    assert str(reloaded.get("text", type=Text)) == expected
    assert store.needs_compaction(document) is False


def state_factory_load(factory: StoreFactory, space_dir: Path, document: str) -> bytes:
    update = factory(space_dir).load(document)
    assert update is not None
    return update


def test_compacting_an_unloaded_document_matches_compacting_with_its_state(
    state_store_factory, tmp_path
) -> None:
    results = []
    for mode in ("handed-state", "store-computed"):
        space_dir = space_path(tmp_path, mode)
        build_space(state_store_factory, space_dir, 5)
        store = state_store_factory(space_dir)
        document = doc_id(801)
        doc = Doc(client_id=78)
        for index in range(25):
            store.commit(Batch(appends=((document, _overwrite(doc, f"revision {index}")),)))
        store.compact(document, doc.get_update() if mode == "handed-state" else None)
        merged = Doc()
        merged.apply_update(state_factory_load(state_store_factory, space_dir, document))
        results.append(str(merged.get("text", type=Text)))
        assert _paths(space_dir, document)[1].stat().st_size == DELTA_MAGIC_BYTES
    assert results[0] == results[1] == "revision 24"


def test_disk_footprint_is_bounded_when_writing_far_more_than_500_times(
    state_store_factory, tmp_path
) -> None:
    body = "the same sized body " * 20
    outcomes = {}
    for writes in (1_200, 6_000):
        space_dir = space_path(tmp_path, f"k{writes}")
        build_space(state_store_factory, space_dir, 3)
        outcomes[writes] = _drive(state_store_factory, space_dir, doc_id(802), writes, body=body)
    peak_small, final_small, compactions_small, _ = outcomes[1_200]
    peak_large, final_large, compactions_large, _ = outcomes[6_000]

    assert compactions_small == 1_200 // 500 and compactions_large == 6_000 // 500
    assert peak_large <= peak_small * 1.05 + 64, outcomes
    assert final_large <= final_small * 1.05 + 64, outcomes
    one_record = len(body) + 120
    assert peak_large <= 500 * one_record + 4096, outcomes


def test_a_delta_of_large_updates_compacts_at_one_mebibyte_before_500_records(
    state_store_factory, tmp_path
) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 3)
    store = state_store_factory(space_dir)
    document = doc_id(803)
    doc = Doc(client_id=79)
    records = 0
    while not store.needs_compaction(document):
        store.commit(Batch(appends=((document, _overwrite(doc, "b" * 8192 + str(records))),)))
        records += 1
        assert records < 500, "size trigger never fired before the record trigger"
    delta_size = _paths(space_dir, document)[1].stat().st_size
    assert delta_size >= 1 << 20
    assert records < 500


def test_compaction_cost_follows_the_document_not_the_space(
    state_store_factory, sizes, tmp_path
) -> None:
    costs = []
    for count in sizes:
        space_dir = space_path(tmp_path, f"n{count}")
        ids = build_space(state_store_factory, space_dir, count)
        store = state_store_factory(space_dir)
        target = ids[count // 3]
        doc = Doc(client_id=4300)
        text = doc.get("text", type=Text)
        with doc.transaction():
            text += "compacted body"
        store.commit(Batch(appends=((target, doc.get_update()),)))
        probe = FsProbe(space_dir)
        with probe.active():
            store.compact(target, doc.get_update())
        stem = document_stem(target)
        assert probe.files_opened <= {
            f"docs/{stem}.state",
            f"docs/{stem}.state.tmp",
            f"docs/{stem}.delta",
            "docs",
            "docs/.",
        }, (count, probe.files_opened)
        assert probe.listings == []
        costs.append(probe.summary())
    assert_same_cost(costs)


def test_the_footprint_curve_is_flat_after_the_first_compaction(
    state_store_factory, tmp_path, record_property
) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 3)
    _, _, _, curve = _drive(
        state_store_factory, space_dir, doc_id(804), 4_000, body="flat " * 40, probe_every=500
    )
    record_property("footprint_curve_bytes", curve)
    print("footprint curve (writes, bytes):", curve)
    sizes_after = [size for step, size in curve]
    assert max(sizes_after) - min(sizes_after) <= 64, curve
