"""Compaction drops collected content from the state file (design 2.3, section 6 GC)."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.orgfs.state import Batch, FileStateStore

from .conftest import SPACE_ID, StoreFactory, Writer, materialize, vector_of

VALUE = "v" * 4096


def _sizes(space_dir: Path) -> tuple[int, int]:
    return (space_dir / "tree.state").stat().st_size, (space_dir / "tree.delta").stat().st_size


def test_deleted_map_keys_do_not_occupy_the_state_file(file_factory: StoreFactory, record_property) -> None:
    writer = Writer(1)
    store = file_factory.open()
    for index in range(100):
        store.commit(Batch(appends=(("tree", writer.put(f"row-{index}", VALUE)),)))
    store.compact("tree")
    full_state, _ = _sizes(file_factory.space_dir)
    for index in range(100):
        store.commit(Batch(appends=(("tree", writer.drop(f"row-{index}")),)))
    store.commit(Batch(appends=(("tree", writer.put("kept", 1)),)))
    delta_before = (file_factory.space_dir / "tree.delta").stat().st_size
    store.compact("tree")
    state, delta = _sizes(file_factory.space_dir)
    record_property("state_with_rows", full_state)
    record_property("delta_after_deletes", delta_before)
    record_property("state_after_compaction", state)
    assert full_state > 100 * len(VALUE)
    assert state < 2048
    assert delta == 8
    assert materialize(file_factory.reopen(store).load("tree")) == {"m": {"kept": 1}, "t": ""}


def test_overwritten_text_does_not_occupy_the_state_file(file_factory: StoreFactory, record_property) -> None:
    writer = Writer(2)
    store = file_factory.open()
    for index in range(50):
        store.commit(Batch(appends=(("tree", writer.replace_text(f"{index}:{VALUE}")),)))
    delta_before = (file_factory.space_dir / "tree.delta").stat().st_size
    store.compact("tree")
    state, _ = _sizes(file_factory.space_dir)
    record_property("delta_before", delta_before)
    record_property("state_after_compaction", state)
    assert delta_before > 50 * len(VALUE)
    assert state < len(VALUE) + 2048
    assert materialize(store.load("tree")) == {"m": {}, "t": f"49:{VALUE}"}


def test_compaction_by_the_store_matches_the_writer_state(file_factory: StoreFactory) -> None:
    writer = Writer(3)
    other = Writer(4)
    store = file_factory.open()
    for index in range(40):
        store.commit(Batch(appends=(("meta", writer.put(f"k{index % 7}", index)),)))
        store.commit(Batch(appends=(("meta", other.put(f"o{index % 3}", index)),)))
    store.commit(Batch(appends=(("meta", writer.drop("k1")),)))
    store.compact("meta")
    loaded = FileStateStore.open(file_factory.space_dir).load("meta")
    reference = Writer(5)
    reference.doc.apply_update(writer.full())
    reference.doc.apply_update(other.full())
    assert materialize(loaded) == materialize(reference.full())
    assert vector_of(loaded) == vector_of(reference.full())


def test_load_without_a_state_file_merges_every_delta_record(tmp_path: Path) -> None:
    space_dir = tmp_path / SPACE_ID
    store = FileStateStore.open(space_dir, create=True)
    writer = Writer(6)
    updates = [writer.put("a", 1), writer.put("b", 2), writer.replace_text("t")]
    for update in updates:
        store.commit(Batch(appends=(("tree", update),)))
    loaded = store.load("tree")
    assert loaded is not None
    assert materialize(loaded) == {"m": {"a": 1, "b": 2}, "t": "t"}
    assert loaded == writer.full()
