"""How big a document's state file gets as history accumulates.

2026-11-25 (C6): the plan's "Yjs GC keeps state bounded" holds only for some
write shapes (measured with pycrdt 0.14.9, see the C6 report):

* overwriting a text wholesale, or one Map key consecutively, is bounded;
* overwriting a text *and* bumping ``__orgfs__.c`` in the same transactions
  (what the §3.3 commit helper does on every local write) leaves one
  deleted-item tombstone per commit that cannot merge with its neighbours:
  ~13 B per commit, unbounded, independent of body size;
* overwriting Map rows in a tree, and deleting rows, leave 5-15 B per
  overwrite / per deleted key.

The tests pin each measured coefficient with a ceiling so a regression to
"old bodies are kept" (hundreds of bytes per commit) fails, and record the
curves.  They do not claim a bound that Yjs does not give.
"""

from __future__ import annotations

import json
from pathlib import Path
import random

from pycrdt import Doc, Map, Text

from ..conftest import space_path, StoreFactory, build_space
from ..reference_store import Batch, document_stem
from ..synthetic import doc_id
from .test_compaction_bounds import _overwrite

STATE_MAGIC = 8


def _state_bytes(space_dir: Path, document: str) -> int:
    return (space_dir / "docs" / f"{document_stem(document)}.state").stat().st_size


def _curve(
    factory: StoreFactory,
    space_dir: Path,
    document: str,
    writes: int,
    *,
    body: str,
    clock: bool,
    new_client_every: int = 0,
    every: int = 1000,
) -> list[tuple[int, int]]:
    store = factory(space_dir)
    doc = Doc(client_id=6001)
    curve = []
    for step in range(1, writes + 1):
        if new_client_every and step % new_client_every == 0:
            fresh = Doc(client_id=random.getrandbits(53))
            fresh.apply_update(doc.get_update())
            doc = fresh
        store.commit(Batch(appends=((document, _overwrite(doc, body, clock=clock)),)))
        if store.needs_compaction(document) or step % every == 0:
            store.compact(document, doc.get_update())
        if step % every == 0:
            curve.append((step, _state_bytes(space_dir, document)))
    return curve


def test_text_overwrites_keep_the_state_flat(state_store_factory, tmp_path, record_property) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 2)
    curve = _curve(state_store_factory, space_dir, doc_id(900), 5_000, body="z" * 400, clock=False)
    record_property("text_overwrite_state_curve", curve)
    print("text overwrite state bytes (writes, bytes):", curve)
    sizes = [size for _, size in curve]
    assert max(sizes) - min(sizes) <= 16, curve
    assert max(sizes) <= 400 + 128, curve


def test_the_commit_clock_costs_a_small_constant_per_commit_not_the_old_bodies(
    state_store_factory, tmp_path, record_property
) -> None:
    slopes = {}
    for label, body in (("small", "s" * 100), ("large", "L" * 4_000)):
        space_dir = space_path(tmp_path, label)
        build_space(state_store_factory, space_dir, 2)
        curve = _curve(state_store_factory, space_dir, doc_id(901), 4_000, body=body, clock=True)
        record_property(f"clock_state_curve_{label}", curve)
        print(f"clock-bearing overwrite, {label} body, state bytes (writes, bytes):", curve)
        (k1, s1), (k2, s2) = curve[0], curve[-1]
        slopes[label] = (s2 - s1) / (k2 - k1)
        assert s2 - len(body) < (k2 * 24), (label, curve)
    assert 0 < slopes["small"] <= 24 and 0 < slopes["large"] <= 24, slopes
    assert abs(slopes["small"] - slopes["large"]) <= 6, slopes


def test_a_new_client_id_per_process_start_costs_per_client_not_per_write(
    state_store_factory, tmp_path, record_property
) -> None:
    finals = {}
    for writes in (2_000, 8_000):
        space_dir = space_path(tmp_path, f"w{writes}")
        build_space(state_store_factory, space_dir, 2)
        curve = _curve(
            state_store_factory,
            space_dir,
            doc_id(902),
            writes,
            body="r" * 200,
            clock=False,
            new_client_every=writes // 20,
            every=writes // 4,
        )
        finals[writes] = curve[-1][1]
        record_property(f"restart_state_curve_{writes}", curve)
        print(f"20 restarts over {writes} writes, state bytes:", curve)
    assert finals[8_000] <= finals[2_000] * 1.25 + 64, finals
    assert finals[8_000] <= 20 * 64 + 400 + 128, finals


def test_state_file_equals_the_live_documents_export_not_the_sum_of_deltas(
    state_store_factory, tmp_path
) -> None:
    space_dir = space_path(tmp_path, "space")
    build_space(state_store_factory, space_dir, 2)
    store = state_store_factory(space_dir)
    document = doc_id(903)
    doc = Doc(client_id=6002)
    for index in range(300):
        store.commit(Batch(appends=((document, _overwrite(doc, f"{index:04d}" * 50)),)))
    store.compact(document, doc.get_update())

    assert _state_bytes(space_dir, document) == STATE_MAGIC + len(doc.get_update())
    assert _state_bytes(space_dir, document) < 400


def test_tree_row_overwrites_and_deletes_leave_only_small_tombstones(record_property) -> None:
    rows = 1_000
    doc = Doc(client_id=6003)
    nodes = doc.get("nodes", type=Map)

    def row(index: int, revision: int) -> str:
        return json.dumps({"i": index, "name": "n" * 100, "r": revision}, sort_keys=True)

    with doc.transaction():
        for index in range(rows):
            nodes[f"n{index}"] = row(index, 0)
    base = len(doc.get_update())
    curve = [(0, base)]
    for revision in range(1, 21):
        with doc.transaction():
            for index in range(rows):
                nodes[f"n{index}"] = row(index, revision)
        if revision in (1, 5, 10, 20):
            curve.append((revision, len(doc.get_update())))
    record_property("tree_overwrite_curve_rounds_bytes", curve)
    print("tree rows overwritten in rounds (round, bytes):", curve)
    per_overwrite = (curve[-1][1] - base) / (20 * rows)
    assert per_overwrite <= 12, (per_overwrite, curve)

    full = len(doc.get_update())
    with doc.transaction():
        for index in range(rows):
            del nodes[f"n{index}"]
    emptied = len(doc.get_update())
    record_property("tree_delete_after_overwrites_bytes_before_after", (full, emptied))
    print("rows deleted after 20 overwrites each: bytes before/after:", full, emptied)
    assert emptied <= full * 0.6, (full, emptied)

    fresh = Doc(client_id=6004)
    keys = fresh.get("nodes", type=Map)
    with fresh.transaction():
        for index in range(3 * rows):
            keys[f"n{index}"] = row(index, 0)
    before = len(fresh.get_update())
    with fresh.transaction():
        for index in range(3 * rows):
            del keys[f"n{index}"]
    after = len(fresh.get_update())
    record_property("tree_delete_without_history_bytes_before_after", (before, after))
    print("rows deleted without overwrite history: bytes before/after:", before, after)
    assert after <= before * 0.2 and after / (3 * rows) <= 20, (before, after)


def _helper_commit(doc: Doc, mutate) -> bytes:
    """The §3.3 commit helper: bump ``__orgfs__.c`` only when the vector did not advance."""

    from ..synthetic import decode_state_vector

    before_raw = doc.get_state()
    before = decode_state_vector(before_raw)
    with doc.transaction():
        mutate(doc)
    if decode_state_vector(doc.get_state()) == before:
        stamp = doc.get("__orgfs__", type=Map)
        with doc.transaction():
            stamp["c"] = int(stamp["c"] if "c" in stamp else 0) + 1
    return doc.get_update(before_raw)


def _overwrite_body(doc: Doc) -> None:
    text = doc.get("text", type=Text)
    if len(text):
        del text[0 : len(text)]
    text += "o" * 300


def _shorten_body(doc: Doc) -> None:
    text = doc.get("text", type=Text)
    if len(text):
        del text[len(text) - 1 : len(text)]


def _helper_curve(factory, space_dir: Path, document: str, commits: int, pure_delete_every: int):
    store = factory(space_dir)
    doc = Doc(client_id=6100)
    curve = []
    pure_deletes = 0
    for step in range(1, commits + 1):
        if pure_delete_every and step % pure_delete_every == 0:
            mutate = _shorten_body
            pure_deletes += 1
        else:
            mutate = _overwrite_body
        store.commit(Batch(appends=((document, _helper_commit(doc, mutate)),)))
        if store.needs_compaction(document) or step % 1000 == 0:
            store.compact(document, doc.get_update())
        if step % 1000 == 0:
            curve.append((step, _state_bytes(space_dir, document), pure_deletes))
    return curve


def test_with_the_clock_only_on_pure_deletes_repeated_overwrites_have_a_bounded_state(
    state_store_factory, tmp_path, record_property
) -> None:
    space_dir = space_path(tmp_path, "overwrites")
    build_space(state_store_factory, space_dir, 2)
    curve = _helper_curve(state_store_factory, space_dir, doc_id(910), 5_000, 0)
    record_property("helper_overwrite_state_curve", curve)
    print("clock only on pure deletes, overwrites only, (commits, bytes, pure deletes):", curve)
    sizes = [size for _, size, _ in curve]
    assert max(sizes) - min(sizes) <= 16, curve
    assert max(sizes) <= 300 + 160, curve


def test_with_the_clock_only_on_pure_deletes_growth_follows_the_pure_deletes_only(
    state_store_factory, tmp_path, record_property
) -> None:
    space_dir = space_path(tmp_path, "mixed")
    build_space(state_store_factory, space_dir, 2)
    curve = _helper_curve(state_store_factory, space_dir, doc_id(911), 4_000, 4)
    record_property("helper_mixed_state_curve", curve)
    print("clock only on pure deletes, every 4th commit a pure delete, (commits, bytes, pure deletes):", curve)
    (_, first, first_deletes), (_, last, last_deletes) = curve[0], curve[-1]
    per_pure_delete = (last - first) / (last_deletes - first_deletes)
    assert 0 <= per_pure_delete <= 24, (per_pure_delete, curve)
