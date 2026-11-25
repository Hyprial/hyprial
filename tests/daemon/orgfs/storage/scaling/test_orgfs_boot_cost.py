"""Reads of a space's meta must not pay the whole meta on every call.

2026-11-24: an owner-home space with 24.7k documents took 592 s to become
healthy.  ``_load_documents`` asked ``retired()`` for every document file and
each answer flattened every meta root, including a ``writers`` row whose
``peerIds`` list had grown to 24,758 entries, so the load was quadratic in the
document count.  A clean reopen then rewrote and fsynced every document file
although nothing had been replayed.

Live-meta reads now go through ``read_model.MetaReadModel``.  The counters
here are deterministic: root flattens, values flattened, and document files
rewritten.  Wall time is not asserted.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, vocabulary
from hyprial.daemon.impl.orgfs.storage.store.read_model import (
    MetaDoc,
    MetaReadModel,
    SpaceReader,
)
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _doc_root, _doc_roots
from tests.support.scaling import at_scales


_AUTHOR = "user:boot"


def _doc_id(index: int) -> str:
    return f"doc-{uuid.UUID(int=index + 16)}"


def _open(path: Path) -> LocalSpaceStore:
    return LocalSpaceStore(path, "space-boot", node_id="node-boot")


def _foreign_update(body: str) -> bytes:
    source = Doc()
    source["text"] = Text(body)
    return source.get_update()


def _build(path: Path, documents: int) -> LocalSpaceStore:
    store = _open(path)
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", _AUTHOR),
            doc["space"].__setitem__("spaceId", "space-boot"),
            doc["space"].__setitem__("name", "boot"),
            doc["members"].__setitem__(_AUTHOR, Map({"mode": "rw", "removedAt": None})),
        ),
        author=_AUTHOR,
        actor=None,
    )
    for index in range(documents):
        update = _foreign_update(f"body {index}")
        store.commit(
            _doc_id(index),
            lambda doc, update=update: doc.apply_update(update),
            author=_AUTHOR,
            actor="agent:0",
        )
    return store


def _count_plain(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = vocabulary._plain

    def counting(value):
        calls[0] += 1
        return original(value)

    monkeypatch.setattr(vocabulary, "_plain", counting)
    return calls


def _count_document_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    written: list[str] = []
    original = LocalSpaceStore._persist_documents

    def recording(self, doc_ids):
        ids = tuple(doc_ids)
        written.extend(ids)
        return original(self, ids)

    monkeypatch.setattr(LocalSpaceStore, "_persist_documents", recording)
    return written


def test_single_root_read_matches_the_full_flatten(tmp_path: Path) -> None:
    store = _build(tmp_path, 3)
    meta = store._docs["meta"]
    roots = _doc_roots(meta)
    keys_before = sorted(str(key) for key in meta.keys())

    assert roots
    for name in roots:
        assert _doc_root(meta, name) == roots[name]
    assert _doc_root(meta, "absent-root") is None
    assert sorted(str(key) for key in meta.keys()) == keys_before
    store.close()


def test_the_fixture_grows_the_writer_row_with_the_document_count(
    tmp_path: Path,
) -> None:
    store = _build(tmp_path, 8)
    writers = _doc_root(store._docs["meta"], "writers")
    longest = max(len(row["peerIds"]) for row in writers.values())
    store.close()

    assert longest >= 8


def test_retired_lookup_does_not_flatten_the_writer_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def measure(documents: int) -> int:
        store = _build(tmp_path / f"retired-{documents}", documents)
        calls = _count_plain(monkeypatch)
        assert store.retired(_doc_id(0)) is None
        monkeypatch.undo()
        store.close()
        return calls[0]

    at_n, at_4n = at_scales(measure, 6)

    assert at_n == at_4n, (at_n, at_4n)


def test_reopen_meta_work_grows_linearly_with_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def measure(documents: int) -> int:
        path = tmp_path / f"reopen-{documents}"
        _build(path, documents).close()
        calls = _count_plain(monkeypatch)
        store = _open(path)
        monkeypatch.undo()
        assert len(store._docs) == documents + 1
        store.close()
        return calls[0]

    at_n, at_4n = at_scales(measure, 24)

    assert at_4n <= at_n * 5, (at_n, at_4n)


def test_clean_reopen_rewrites_no_document_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(tmp_path, 6).close()
    written = _count_document_writes(monkeypatch)

    with _open(tmp_path) as store:
        monkeypatch.undo()
        loaded = len(store._docs)

    assert written == []
    assert loaded == 7


def test_reopen_after_an_unclean_stop_rewrites_only_replayed_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _build(tmp_path, 6)
    store.checkpoint()
    update = _foreign_update("second body")
    store.commit(
        _doc_id(2),
        lambda doc: doc.apply_update(update),
        author=_AUTHOR,
        actor="agent:0",
    )
    expected = store._docs[_doc_id(2)].get_update()
    store._db.close()

    written = _count_document_writes(monkeypatch)
    with _open(tmp_path) as reopened:
        monkeypatch.undo()
        recovered = reopened._docs[_doc_id(2)].get_update()

    assert sorted(written) == sorted(["meta", _doc_id(2)])
    assert recovered == expected

    written_again = _count_document_writes(monkeypatch)
    with _open(tmp_path):
        monkeypatch.undo()

    assert written_again == []


@pytest.mark.parametrize("reverse", [False, True])
def test_a_reordered_checkpoint_state_vector_is_still_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    store = _build(tmp_path, 3)
    update = _foreign_update("second body")
    store.commit(
        _doc_id(1),
        lambda doc: doc.apply_update(update),
        author=_AUTHOR,
        actor="agent:0",
    )
    clocks = vocabulary._decode_state_vector(store._docs[_doc_id(1)].get_state())
    store.close()
    assert len(clocks) >= 2
    items = sorted(clocks.items(), reverse=reverse)
    encoded = vocabulary._write_var_uint(len(items)) + b"".join(
        vocabulary._write_var_uint(client) + vocabulary._write_var_uint(clock)
        for client, clock in items
    )
    journal = sqlite3.connect(tmp_path / "orgfs/spaces/space-boot/journal.sqlite3")
    journal.execute(
        "UPDATE doc_checkpoints SET version = ? WHERE doc_id = ?",
        (encoded, _doc_id(1)),
    )
    journal.commit()
    journal.close()
    written = _count_document_writes(monkeypatch)

    with _open(tmp_path):
        monkeypatch.undo()

    assert written == []


def test_reopen_flattens_each_meta_root_a_bounded_number_of_times(
    tmp_path: Path,
) -> None:
    def measure(documents: int) -> int:
        path = tmp_path / f"flattens-{documents}"
        _build(path, documents).close()
        with _open(path) as store:
            return store._meta_view.flattens

    at_n, at_4n = at_scales(measure, 12)

    assert at_n == at_4n, (at_n, at_4n)


def test_commits_that_leave_the_meta_unchanged_do_not_flatten_it(
    tmp_path: Path,
) -> None:
    with _build(tmp_path, 4) as store:

        def append(doc) -> None:
            doc.get("text", type=Text).__iadd__("!")

        store.commit(_doc_id(0), append, author=_AUTHOR, actor="agent:0")
        before = store._meta_view.flattens
        for _ in range(5):
            store.commit(_doc_id(0), append, author=_AUTHOR, actor="agent:0")

        assert store._meta_view.flattens == before


def test_read_model_tracks_inserts_overwrites_and_deletions(tmp_path: Path) -> None:
    with _build(tmp_path, 2) as store:

        def check() -> None:
            live = _doc_roots(store._docs["meta"])
            for name in (*live, "absent-root"):
                assert store._meta_root(name) == live.get(name), name

        check()
        store.commit(
            "meta",
            lambda doc: doc["purgeList"].__setitem__(
                "a" * 64, Map({"planId": "plan-1", "unbannedAt": None})
            ),
            author=_AUTHOR,
            actor=None,
        )
        check()
        assert store.purge_listed("a" * 64)
        store.commit(
            "meta",
            lambda doc: doc["purgeList"]["a" * 64].__setitem__("unbannedAt", "later"),
            author=_AUTHOR,
            actor=None,
        )
        check()
        assert not store.purge_listed("a" * 64)
        store.commit(
            "meta",
            lambda doc: doc["purgeList"].__delitem__("a" * 64),
            author=_AUTHOR,
            actor=None,
        )
        check()
        assert store.purge_list_entries() == {}
        assert store._meta_root("purgeList") == {}


def test_a_deletion_that_keeps_the_state_vector_still_invalidates() -> None:
    live = MetaDoc(client_id=1)
    writer = Doc(client_id=2)
    writer["purgeList"] = Map({"a": 1, "b": 2})
    live.apply_update(writer.get_update())
    view = MetaReadModel()
    assert view.root(live, "purgeList") == {"a": 1, "b": 2}

    state = live.get_state()
    del writer["purgeList"]["a"]
    live.apply_update(writer.get_update(state))

    assert live.get_state() == state
    assert view.root(live, "purgeList") == {"b": 2}


def test_a_root_named_without_a_transaction_is_seen() -> None:
    live = MetaDoc(client_id=1)
    view = MetaReadModel()
    assert view.root(live, "late") is None

    live.get("late", type=Map)

    assert view.root(live, "late") == _doc_roots(live).get("late")


def test_the_store_is_a_space_reader(tmp_path: Path) -> None:
    with _build(tmp_path, 1) as store:
        assert isinstance(store, SpaceReader)


def test_no_store_module_flattens_the_live_meta_outside_the_read_model() -> None:
    root = Path(vocabulary.__file__).parent
    direct = re.compile(r"_doc_roots?\(\s*(self|store)\._docs")
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted(root.rglob("*.py"))
        if "read_model" not in path.parts
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if direct.search(line)
    ]

    assert offenders == []


def test_an_overwrite_made_without_apply_update_is_seen() -> None:
    live = MetaDoc(client_id=1)
    live["members"] = Map({"a": 1})
    view = MetaReadModel()
    assert view.root(live, "members") == {"a": 1}

    live["members"]["a"] = 2

    assert view.root(live, "members") == {"a": 2}


def test_a_plain_document_is_never_cached() -> None:
    plain = Doc(client_id=1)
    plain["members"] = Map({"a": 1})
    view = MetaReadModel()

    assert view.root(plain, "members") == {"a": 1}
    assert view.root(plain, "members") == {"a": 1}
    assert view.flattens == 2


def test_a_store_read_from_another_thread_survives_close(tmp_path: Path) -> None:
    import threading

    store = _build(tmp_path, 2)
    seen: list[object] = []
    reader = threading.Thread(target=lambda: seen.append(store.retired(_doc_id(0))))
    reader.start()
    reader.join()
    closer = threading.Thread(target=store.close)
    closer.start()
    closer.join()

    assert seen == [None]
