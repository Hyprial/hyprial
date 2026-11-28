"""Behaviour every ``StateStore`` shares (design 2.3, 2.4, 2.6), file and memory."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.state import (
    COMPACTION_BYTES,
    COMPACTION_RECORDS,
    Batch,
    StateStore,
)

from .conftest import (
    SPACE_ID,
    StoreFactory,
    Writer,
    content_doc_id,
    invite_doc_id,
    materialize,
    vector_of,
)


def test_a_new_space_is_empty_and_satisfies_the_protocol(factory: StoreFactory) -> None:
    store = factory.open()
    assert isinstance(store, StateStore)
    assert store.space_id == SPACE_ID
    assert store.recovered_batch is False
    assert store.load("meta") is None
    assert store.load("tree") is None


def test_a_committed_append_survives_reopen(factory: StoreFactory) -> None:
    store = factory.open()
    writer = Writer(1)
    store.commit(Batch(appends=(("meta", writer.put("owner", "alice")),)))
    store.commit(Batch(appends=(("meta", writer.put("acl", "x")),)))
    reopened = factory.reopen(store)
    assert materialize(reopened.load("meta")) == {"m": {"owner": "alice", "acl": "x"}, "t": ""}
    assert vector_of(reopened.load("meta")) == vector_of(writer.full())


@pytest.mark.parametrize("doc_id", ["meta", "tree", "content", "invite"])
def test_directory_and_content_documents_share_one_interface(
    factory: StoreFactory, doc_id: str
) -> None:
    resolved = {"content": content_doc_id(), "invite": invite_doc_id()}.get(doc_id, doc_id)
    store = factory.open()
    writer = Writer(2)
    store.commit(Batch(appends=((resolved, writer.replace_text("pending invite")),)))
    reopened = factory.reopen(store)
    assert materialize(reopened.load(resolved)) == {"m": {}, "t": "pending invite"}


def test_a_batch_applies_appends_in_order_then_deletes(factory: StoreFactory) -> None:
    kept, gone = content_doc_id(), content_doc_id()
    tree, content, victim = Writer(3), Writer(4), Writer(5)
    store = factory.open()
    store.commit(Batch(appends=((gone, victim.put("x", 1)), ("tree", tree.put(gone, "row")))))
    store.commit(
        Batch(
            appends=(
                (kept, content.replace_text("hello")),
                ("tree", tree.edit(lambda w: (w.map.__delitem__(gone), w.map.__setitem__(kept, "row")))),
            ),
            deletes=(gone,),
        )
    )
    reopened = factory.reopen(store)
    assert reopened.load(gone) is None
    assert materialize(reopened.load(kept)) == {"m": {}, "t": "hello"}
    assert materialize(reopened.load("tree")) == {"m": {kept: "row"}, "t": ""}


def test_a_pure_deletion_is_persisted_without_moving_the_vector(factory: StoreFactory) -> None:
    writer = Writer(6)
    store = factory.open()
    store.commit(Batch(appends=(("tree", writer.put("a", 1)),)))
    before = vector_of(store.load("tree"))
    deletion = writer.drop("a")
    store.commit(Batch(appends=(("tree", deletion),)))
    reopened = factory.reopen(store)
    assert materialize(reopened.load("tree")) == {"m": {}, "t": ""}
    assert vector_of(reopened.load("tree")) == before


def test_repeated_appends_are_harmless(factory: StoreFactory) -> None:
    writer = Writer(7)
    update = writer.put("k", "v")
    store = factory.open()
    store.commit(Batch(appends=(("meta", update), ("meta", update))))
    store.commit(Batch(appends=(("meta", update),)))
    assert materialize(factory.reopen(store).load("meta")) == {"m": {"k": "v"}, "t": ""}


def test_an_empty_batch_is_a_no_op(factory: StoreFactory) -> None:
    store = factory.open()
    store.commit(Batch())
    assert store.load("meta") is None


@pytest.mark.parametrize("doc_id", ["meta", "tree", ""])
def test_directory_documents_cannot_be_deleted(factory: StoreFactory, doc_id: str) -> None:
    kept = content_doc_id()
    store = factory.open()
    with pytest.raises(ValueError):
        store.commit(Batch(appends=((kept, Writer(1).put("a", 1)),), deletes=(doc_id,)))
    assert store.load(kept) is None


def test_invalid_append_arguments_are_rejected_before_anything_is_written(
    factory: StoreFactory,
) -> None:
    store = factory.open()
    with pytest.raises(ValueError):
        store.commit(Batch(appends=(("meta", Writer(1).put("a", 1)), ("", b"\x00\x00"))))
    with pytest.raises(ValueError):
        store.commit(Batch(appends=(("meta", b""),)))
    assert store.load("meta") is None


def test_deleting_an_unknown_document_is_harmless(factory: StoreFactory) -> None:
    doc_id = content_doc_id()
    store = factory.open()
    store.commit(Batch(deletes=(doc_id,)))
    assert store.load(doc_id) is None


def test_needs_compaction_after_the_record_threshold(factory: StoreFactory) -> None:
    writer = Writer(8)
    store = factory.open()
    for index in range(COMPACTION_RECORDS - 1):
        store.commit(Batch(appends=(("tree", writer.put("k", index)),)))
    assert store.needs_compaction("tree") is False
    store.commit(Batch(appends=(("tree", writer.put("k", "last")),)))
    assert store.needs_compaction("tree") is True
    reopened = factory.reopen(store)
    assert reopened.needs_compaction("tree") is True
    reopened.compact("tree")
    assert reopened.needs_compaction("tree") is False
    assert materialize(reopened.load("tree")) == {"m": {"k": "last"}, "t": ""}


def test_needs_compaction_after_the_byte_threshold(factory: StoreFactory) -> None:
    doc_id = content_doc_id()
    writer = Writer(9)
    store = factory.open()
    store.commit(Batch(appends=((doc_id, writer.put("blob", "x" * (COMPACTION_BYTES // 2))),)))
    assert store.needs_compaction(doc_id) is False
    store.commit(Batch(appends=((doc_id, writer.put("blob2", "y" * (COMPACTION_BYTES // 2))),)))
    assert store.needs_compaction(doc_id) is True
    store.compact(doc_id)
    assert store.needs_compaction(doc_id) is False


def test_needs_compaction_is_false_for_an_absent_document(factory: StoreFactory) -> None:
    assert factory.open().needs_compaction(content_doc_id()) is False


@pytest.mark.parametrize("given", [False, True])
def test_compaction_preserves_the_loaded_state(factory: StoreFactory, given: bool) -> None:
    writer = Writer(10)
    store = factory.open()
    for index in range(20):
        store.commit(Batch(appends=(("tree", writer.put(f"k{index % 5}", index)),)))
    store.commit(Batch(appends=(("tree", writer.drop("k0")),)))
    store.commit(Batch(appends=(("tree", writer.replace_text("final")),)))
    before = store.load("tree")
    store.compact("tree", writer.full() if given else None)
    after = factory.reopen(store).load("tree")
    assert materialize(after) == materialize(before)
    assert vector_of(after) == vector_of(before)


def test_appends_after_compaction_are_kept(factory: StoreFactory) -> None:
    writer = Writer(11)
    store = factory.open()
    store.commit(Batch(appends=(("meta", writer.put("a", 1)),)))
    store.compact("meta")
    store.commit(Batch(appends=(("meta", writer.put("b", 2)),)))
    assert materialize(factory.reopen(store).load("meta")) == {"m": {"a": 1, "b": 2}, "t": ""}


def test_compacting_an_absent_document_is_a_no_op(factory: StoreFactory) -> None:
    store = factory.open()
    doc_id = content_doc_id()
    store.compact(doc_id)
    store.compact(doc_id, Writer(1).put("a", 1))
    assert store.load(doc_id) is None


def test_a_closed_store_refuses_every_operation(factory: StoreFactory) -> None:
    store = factory.open()
    store.close()
    store.close()
    for call in (
        lambda: store.load("meta"),
        lambda: store.commit(Batch(appends=(("meta", Writer(1).put("a", 1)),))),
        lambda: store.needs_compaction("meta"),
        lambda: store.compact("meta"),
    ):
        with pytest.raises(RuntimeError):
            call()
