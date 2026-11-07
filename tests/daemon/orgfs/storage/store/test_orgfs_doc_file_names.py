"""Document files are named by a hash of the id, and old id-named files migrate.

Protected directory ids grow with real names: an invite in ``group-internal``
owned by ``user:bob.smith@example.com`` has a 253-character id, past what a
``docs/<id>.loro`` file name can hold.  The store names each file
``h-<hash>.loro`` and keeps the id in the file's header.
"""

from __future__ import annotations

import threading
import time

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore, LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import document_file_stem
from hyprial.identity import protected_directory_doc_id

SPACE_ID = "a2b5fb64-de25-4897-aaf1-0e25e2d83e93"
OWNER = "user:bob.smith@example.com"
TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"
INVITE_PATH = "directory/invites/" + "0" * 64 + ".json"
SHORT_DOC_ID = "doc-00000000-0000-0000-0000-0000000000aa"

_OPEN: list[LocalSpaceStore] = []


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _store(root, **kwargs) -> LocalSpaceStore:
    store = FileSpaceStore(root, SPACE_ID, node_id="node-hq-01.example-tailnet.ts.net", **kwargs)
    _OPEN.append(store)
    return store


def _genesis(store: LocalSpaceStore) -> None:
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", OWNER),
            doc["space"].__setitem__("spaceId", SPACE_ID),
            doc["members"].__setitem__(OWNER, Map({"mode": "rw", "removedAt": None})),
            doc["docs"].__setitem__(
                "tree",
                Map({"active": TREE_DOC_ID, "activatedAt": "genesis", "planId": None}),
            ),
        ),
        author=OWNER,
        actor=None,
    )


def _write(store: LocalSpaceStore, doc_id: str, text: str) -> None:
    store.commit(
        doc_id,
        lambda doc: doc.__setitem__("text", Text(text)),
        author=OWNER,
        actor=None,
    )


def _text(store: LocalSpaceStore, doc_id: str) -> str:
    return store._docs[doc_id].get("text", type=Text).to_py()


def _invite_doc_id() -> str:
    doc_id = protected_directory_doc_id(SPACE_ID, INVITE_PATH, space_owner=OWNER)
    assert doc_id is not None
    return doc_id


def _legacy_layout(store: LocalSpaceStore, doc_ids: list[str]) -> dict[str, bytes]:
    """Rewrite each document file as 0.5.2 did: ``docs/<doc id>.loro``, raw update."""

    raw: dict[str, bytes] = {}
    for doc_id in doc_ids:
        path = store._doc_path(doc_id)
        read_id, update = store._read_document_file(path)
        assert read_id == doc_id
        (path.parent / f"{doc_id}.loro").write_bytes(update)
        path.unlink()
        raw[doc_id] = update
    return raw


def test_the_production_invite_id_is_stored_read_and_reloaded(tmp_path) -> None:
    doc_id = _invite_doc_id()
    # The hq reading on 2026-10-08: 253 characters, refused by the 240 cap.
    assert len(doc_id) == 253

    store = _store(tmp_path / "state")
    _genesis(store)
    _write(store, doc_id, "pending invite")
    store.checkpoint()

    path = store._doc_path(doc_id)
    assert path.name == f"{document_file_stem(doc_id)}.loro"
    assert len(path.name) <= 255 and path.is_file()

    store.close()
    reopened = _store(tmp_path / "state")
    assert _text(reopened, doc_id) == "pending invite"


def test_id_named_files_migrate_once_and_keep_their_content(tmp_path) -> None:
    root = tmp_path / "state"
    store = _store(root)
    _genesis(store)
    short_protected = protected_directory_doc_id(
        SPACE_ID, "directory/devices/owner/short.json", space_owner=OWNER
    )
    assert short_protected is not None and len(short_protected) < 240
    _write(store, SHORT_DOC_ID, "ordinary")
    _write(store, short_protected, "protected")
    store.checkpoint()
    _legacy_layout(store, [SHORT_DOC_ID, short_protected])
    store.close()

    reopened = _store(root)

    docs = root / "orgfs" / "spaces" / SPACE_ID / "docs"
    assert sorted(path.name for path in docs.glob("*.loro")) == sorted(
        f"{document_file_stem(doc_id)}.loro" for doc_id in (SHORT_DOC_ID, short_protected)
    )
    assert _text(reopened, SHORT_DOC_ID) == "ordinary"
    assert _text(reopened, short_protected) == "protected"


def test_a_crash_between_writing_and_removing_resumes_on_the_next_open(tmp_path) -> None:
    root = tmp_path / "state"
    store = _store(root)
    _genesis(store)
    _write(store, SHORT_DOC_ID, "survives")
    store.checkpoint()
    _legacy_layout(store, [SHORT_DOC_ID])
    store.close()

    def crash(point: str) -> None:
        if point == "docs-migration:written":
            raise RuntimeError("crash after the hashed file is durable")

    with pytest.raises(RuntimeError, match="crash after"):
        FileSpaceStore(root, SPACE_ID, node_id="node-crash", fault_hook=crash)
    docs = root / "orgfs" / "spaces" / SPACE_ID / "docs"
    # Both names exist now; the hashed one is complete.
    assert (docs / f"{SHORT_DOC_ID}.loro").is_file()
    assert (docs / f"{document_file_stem(SHORT_DOC_ID)}.loro").is_file()

    reopened = _store(root)

    assert not (docs / f"{SHORT_DOC_ID}.loro").exists()
    assert _text(reopened, SHORT_DOC_ID) == "survives"


def test_with_both_names_present_the_hashed_file_wins(tmp_path) -> None:
    root = tmp_path / "state"
    store = _store(root)
    _genesis(store)
    _write(store, SHORT_DOC_ID, "old")
    store.checkpoint()
    stale = _legacy_layout(store, [SHORT_DOC_ID])[SHORT_DOC_ID]
    store.close()
    store = _store(root)
    _write(store, SHORT_DOC_ID, "new")
    store.checkpoint()
    store.close()
    _OPEN.remove(store)
    docs = root / "orgfs" / "spaces" / SPACE_ID / "docs"
    hashed = docs / f"{document_file_stem(SHORT_DOC_ID)}.loro"
    current = hashed.read_bytes()
    (docs / f"{SHORT_DOC_ID}.loro").write_bytes(stale)

    # Observe the migration step itself: a completed open replays the journal
    # and rewrites the cache, which would hide a stale overwrite.  After a
    # retirement drops journal rows the file is the only copy, so it matters.
    def stop(point: str) -> None:
        if point == "docs-migration:written":
            raise RuntimeError("stop after the migration step")

    with pytest.raises(RuntimeError, match="stop after"):
        FileSpaceStore(root, SPACE_ID, node_id="node-stop", fault_hook=stop)
    assert hashed.read_bytes() == current

    _store(root)
    assert not (docs / f"{SHORT_DOC_ID}.loro").exists()


def test_two_concurrent_opens_do_not_interleave_the_migration(tmp_path) -> None:
    root = tmp_path / "state"
    store = _store(root)
    _genesis(store)
    doc_ids = [f"doc-00000000-0000-0000-0000-0000000000{index:02x}" for index in range(8)]
    for doc_id in doc_ids:
        _write(store, doc_id, doc_id)
    store.checkpoint()
    _legacy_layout(store, doc_ids)
    store.close()

    first_in_migration = threading.Event()

    def slow(point: str) -> None:
        if point == "docs-migration:written":
            first_in_migration.set()
            time.sleep(0.05)

    errors: list[BaseException] = []
    opened: list[LocalSpaceStore] = []

    def open_store(**kwargs) -> None:
        try:
            opened.append(FileSpaceStore(root, SPACE_ID, node_id="node-x", **kwargs))
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    first = threading.Thread(target=open_store, kwargs={"fault_hook": slow})
    first.start()
    assert first_in_migration.wait(5)
    second = threading.Thread(target=open_store)
    second.start()
    first.join(10)
    second.join(10)
    _OPEN.extend(opened)

    assert errors == []
    assert len(opened) == 2
    for reopened in opened:
        assert all(_text(reopened, doc_id) == doc_id for doc_id in doc_ids)
    docs = root / "orgfs" / "spaces" / SPACE_ID / "docs"
    assert not any(not path.name.startswith("h-") for path in docs.glob("*.loro"))
