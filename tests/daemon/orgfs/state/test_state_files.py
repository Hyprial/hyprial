"""File layout of ``FileStateStore``: formats, torn tails, space.json, old layouts."""

from __future__ import annotations

import base64
import json
import struct
import uuid
import zlib
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.state import (
    LAYOUT_VERSION,
    ORGFS_LAYOUT_UNSUPPORTED,
    Batch,
    FileStateStore,
    is_space_directory_name,
    space_directory_kind,
)
from hyprial.daemon.impl.orgfs.storage.vocabulary import StoreError, document_file_stem

from .conftest import SPACE_ID, StoreFactory, Writer, content_doc_id, invite_doc_id, materialize


def _record(update: bytes) -> bytes:
    return struct.pack(">II", len(update), zlib.crc32(update)) + update


def _pending(appends: list[tuple[str, bytes]], deletes: list[str], *, crc: int | None = None) -> bytes:
    body = json.dumps(
        {
            "appends": [[doc_id, base64.b64encode(update).decode("ascii")] for doc_id, update in appends],
            "deletes": deletes,
        }
    ).encode("utf-8")
    return b"ORGFSBT3" + struct.pack(">II", len(body), zlib.crc32(body) if crc is None else crc) + body


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


class _Events:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level: str, event: str, **fields: object) -> None:
        self.seen.append((level, event, fields))

    def names(self) -> list[str]:
        return [event for _level, event, _fields in self.seen]


def test_space_json_is_created_with_layout_three(file_factory: StoreFactory) -> None:
    file_factory.open()
    value = json.loads((file_factory.space_dir / "space.json").read_text("utf-8"))
    assert value["layoutVersion"] == LAYOUT_VERSION == 3
    assert value["spaceId"] == SPACE_ID
    assert isinstance(value["createdAt"], str) and value["createdAt"]
    assert (file_factory.space_dir / "docs").is_dir()
    assert not (file_factory.space_dir / "journal.sqlite3").exists()


def test_reopening_keeps_the_original_space_json(file_factory: StoreFactory) -> None:
    file_factory.open().close()
    first = (file_factory.space_dir / "space.json").read_bytes()
    file_factory.open().close()
    assert (file_factory.space_dir / "space.json").read_bytes() == first


def test_delta_records_have_the_documented_format(file_factory: StoreFactory) -> None:
    store = file_factory.open()
    first, second = Writer(1).put("a", 1), Writer(2).put("b", 2)
    store.commit(Batch(appends=(("meta", first),)))
    store.commit(Batch(appends=(("meta", second),)))
    assert (file_factory.space_dir / "meta.delta").read_bytes() == b"ORGFSDL3" + _record(first) + _record(second)
    assert not (file_factory.space_dir / "meta.state").exists()


def test_compaction_writes_a_state_file_and_empties_the_delta(file_factory: StoreFactory) -> None:
    store = file_factory.open()
    writer = Writer(1)
    store.commit(Batch(appends=(("tree", writer.put("a", 1)),)))
    store.compact("tree", writer.full())
    assert (file_factory.space_dir / "tree.state").read_bytes() == b"ORGFSST3" + writer.full()
    assert (file_factory.space_dir / "tree.delta").read_bytes() == b"ORGFSDL3"
    assert not list(file_factory.space_dir.glob("*.tmp"))


def test_content_files_are_named_by_document_file_stem(file_factory: StoreFactory) -> None:
    doc_id = invite_doc_id()
    store = file_factory.open()
    store.commit(Batch(appends=((doc_id, Writer(1).replace_text("pending invite")),)))
    store.compact(doc_id)
    stem = document_file_stem(doc_id)
    for suffix in (".state", ".delta"):
        path = file_factory.space_dir / "docs" / f"{stem}{suffix}"
        assert path.is_file() and len(path.name) <= 255
    assert materialize(file_factory.reopen(store).load(doc_id)) == {"m": {}, "t": "pending invite"}


def test_deleting_a_document_removes_both_files(file_factory: StoreFactory) -> None:
    doc_id = content_doc_id()
    store = file_factory.open()
    store.commit(Batch(appends=((doc_id, Writer(1).put("a", 1)),)))
    store.compact(doc_id)
    store.commit(Batch(appends=((doc_id, Writer(2).put("b", 1)),)))
    store.commit(Batch(appends=(("tree", Writer(3).put("x", 1)),), deletes=(doc_id,)))
    assert list((file_factory.space_dir / "docs").iterdir()) == []
    assert not (file_factory.space_dir / "batch.pending").exists()


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(lambda good: _record(good)[: len(_record(good)) // 2], id="half-record"),
        pytest.param(lambda good: _record(good)[:5], id="half-header"),
        pytest.param(lambda good: struct.pack(">II", len(good), zlib.crc32(good) ^ 1) + good, id="bad-crc"),
        pytest.param(lambda good: struct.pack(">II", 1 << 30, 0) + good, id="length-out-of-range"),
    ],
)
def test_a_torn_delta_tail_is_truncated_on_load(file_factory: StoreFactory, tail) -> None:
    events = _Events()
    writer = Writer(1)
    store = file_factory.open()
    kept = [writer.put("a", 1), writer.put("b", 2)]
    for update in kept:
        store.commit(Batch(appends=(("tree", update),)))
    store.close()
    delta = file_factory.space_dir / "tree.delta"
    complete = delta.read_bytes()
    with delta.open("ab") as stream:
        stream.write(tail(writer.put("c", 3)))
    reopened = file_factory.open(logger=events)
    assert materialize(reopened.load("tree")) == {"m": {"a": 1, "b": 2}, "t": ""}
    assert delta.read_bytes() == complete
    assert "orgfs.state.delta-truncated" in events.names()


def test_a_torn_tail_is_repaired_before_the_next_append(file_factory: StoreFactory) -> None:
    writer = Writer(1)
    store = file_factory.open()
    store.commit(Batch(appends=(("tree", writer.put("a", 1)),)))
    store.close()
    delta = file_factory.space_dir / "tree.delta"
    with delta.open("ab") as stream:
        stream.write(_record(writer.put("lost", 0))[:7])
    complete = delta.read_bytes()[:-7]
    later = Writer(2).put("b", 2)
    reopened = file_factory.open()
    reopened.commit(Batch(appends=(("tree", later),)))
    assert delta.read_bytes() == complete + _record(later)
    assert materialize(file_factory.reopen(reopened).load("tree")) == {"m": {"a": 1, "b": 2}, "t": ""}


@pytest.mark.parametrize("partial", [b"", b"ORG", b"ORGFSDL"])
def test_a_torn_new_delta_header_reads_as_empty(file_factory: StoreFactory, partial: bytes) -> None:
    store = file_factory.open()
    store.close()
    (file_factory.space_dir / "meta.delta").write_bytes(partial)
    reopened = file_factory.open()
    assert reopened.load("meta") is None
    reopened.commit(Batch(appends=(("meta", Writer(1).put("a", 1)),)))
    assert materialize(file_factory.reopen(reopened).load("meta")) == {"m": {"a": 1}, "t": ""}


@pytest.mark.parametrize("name", ["meta.delta", "meta.state"])
def test_a_foreign_magic_is_refused_not_overwritten(file_factory: StoreFactory, name: str) -> None:
    file_factory.open().close()
    path = file_factory.space_dir / name
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)
    store = file_factory.open()
    with pytest.raises(StoreError) as caught:
        store.load("meta")
    assert caught.value.details["reason"] == "corrupt-file"
    with pytest.raises(StoreError):
        store.compact("meta")
    if name == "meta.delta":
        with pytest.raises(StoreError):
            store.commit(Batch(appends=(("meta", Writer(1).put("a", 1)),)))
    assert path.read_bytes().startswith(b"SQLite format 3")


def test_a_valid_pending_batch_is_redone_on_open(file_factory: StoreFactory) -> None:
    kept, gone = content_doc_id(), content_doc_id()
    store = file_factory.open()
    store.commit(Batch(appends=((gone, Writer(1).put("x", 1)),)))
    store.close()
    tree = Writer(2).put(kept, "row")
    content = Writer(3).replace_text("hello")
    (file_factory.space_dir / "batch.pending").write_bytes(_pending([(kept, content), ("tree", tree)], [gone]))
    reopened = file_factory.open()
    assert reopened.recovered_batch is True
    assert not (file_factory.space_dir / "batch.pending").exists()
    assert reopened.load(gone) is None
    assert materialize(reopened.load(kept)) == {"m": {}, "t": "hello"}
    assert materialize(reopened.load("tree")) == {"m": {kept: "row"}, "t": ""}
    assert file_factory.reopen(reopened).recovered_batch is False


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda raw: raw[:-3], id="truncated"),
        pytest.param(lambda raw: raw[:8] + raw[8:12] + struct.pack(">I", 0) + raw[16:], id="bad-crc"),
        pytest.param(lambda raw: b"ORGFSXX3" + raw[8:], id="bad-magic"),
        pytest.param(lambda raw: raw[:4], id="torn-magic"),
    ],
)
def test_a_damaged_pending_batch_is_discarded(file_factory: StoreFactory, damage) -> None:
    events = _Events()
    kept = content_doc_id()
    file_factory.open().close()
    raw = _pending([(kept, Writer(1).put("a", 1)), ("tree", Writer(2).put(kept, "row"))], [])
    (file_factory.space_dir / "batch.pending").write_bytes(damage(raw))
    reopened = file_factory.open(logger=events)
    assert reopened.recovered_batch is False
    assert not (file_factory.space_dir / "batch.pending").exists()
    assert reopened.load(kept) is None and reopened.load("tree") is None
    assert "orgfs.state.batch-discarded" in events.names()


def test_a_pending_batch_with_a_correct_crc_but_bad_shape_is_refused(file_factory: StoreFactory) -> None:
    file_factory.open().close()
    body = json.dumps({"appends": [["meta", "not base64!"]], "deletes": []}).encode()
    (file_factory.space_dir / "batch.pending").write_bytes(
        b"ORGFSBT3" + struct.pack(">II", len(body), zlib.crc32(body)) + body
    )
    with pytest.raises(StoreError):
        file_factory.open()
    assert (file_factory.space_dir / "batch.pending").exists()


def test_a_leftover_pending_temp_file_is_removed_on_open(file_factory: StoreFactory) -> None:
    file_factory.open().close()
    temp = file_factory.space_dir / "batch.pending.tmp"
    temp.write_bytes(_pending([("meta", Writer(1).put("a", 1))], []))
    store = file_factory.open()
    assert not temp.exists()
    assert store.recovered_batch is False and store.load("meta") is None


def test_the_layout_code_is_the_registered_wire_code() -> None:
    from hyprial.kernel import ipc_errors

    assert ORGFS_LAYOUT_UNSUPPORTED == "ORGFS_LAYOUT_UNSUPPORTED"
    assert ORGFS_LAYOUT_UNSUPPORTED in ipc_errors.ALL_CODES
    assert ipc_errors.ORGFS_LAYOUT_UNSUPPORTED is ORGFS_LAYOUT_UNSUPPORTED


def test_an_old_layout_space_is_refused_and_left_untouched(tmp_path: Path) -> None:
    space_dir = tmp_path / "spaces" / SPACE_ID
    (space_dir / "docs").mkdir(parents=True)
    (space_dir / "journal.sqlite3").write_bytes(b"SQLite format 3\x00")
    (space_dir / "docs" / "h-aaaa.loro").write_bytes(b"old")
    before = _snapshot(space_dir)
    for create in (False, True):
        with pytest.raises(StoreError) as caught:
            FileStateStore.open(space_dir, create=create)
        assert caught.value.code == ORGFS_LAYOUT_UNSUPPORTED
        assert caught.value.details["guide"] == "docs/guides/migration-orgfs-minimal.md"
        assert caught.value.details["spaceId"] == SPACE_ID
    assert _snapshot(space_dir) == before
    assert space_directory_kind(space_dir) == "legacy"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param({"layoutVersion": 2, "spaceId": SPACE_ID, "createdAt": "x"}, id="version-2"),
        pytest.param({"layoutVersion": True, "spaceId": SPACE_ID, "createdAt": "x"}, id="version-bool"),
        pytest.param({"layoutVersion": 3, "spaceId": str(uuid.uuid4()), "createdAt": "x"}, id="other-space"),
        pytest.param([], id="not-an-object"),
        pytest.param(None, id="not-json"),
    ],
)
def test_an_unusable_space_json_is_refused(tmp_path: Path, content) -> None:
    space_dir = tmp_path / "spaces" / SPACE_ID
    space_dir.mkdir(parents=True)
    raw = b"{not json" if content is None else json.dumps(content).encode()
    (space_dir / "space.json").write_bytes(raw)
    (space_dir / "journal.sqlite3").write_bytes(b"x")
    with pytest.raises(StoreError) as caught:
        FileStateStore.open(space_dir, create=True)
    assert caught.value.code == ORGFS_LAYOUT_UNSUPPORTED
    assert (space_dir / "space.json").read_bytes() == raw


def test_a_missing_space_is_not_created_without_create(tmp_path: Path) -> None:
    space_dir = tmp_path / "spaces" / SPACE_ID
    with pytest.raises(FileNotFoundError):
        FileStateStore.open(space_dir)
    assert not space_dir.exists()
    space_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        FileStateStore.open(space_dir)
    assert space_directory_kind(space_dir) == "uninitialized"


def test_a_space_directory_must_be_named_by_a_uuid(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileStateStore.open(tmp_path / "spaces" / "not-a-uuid", create=True)
    assert not (tmp_path / "spaces" / "not-a-uuid").exists()


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        (SPACE_ID, True),
        (SPACE_ID.upper(), True),
        (SPACE_ID.replace("-", ""), True),
        ("{" + SPACE_ID + "}", True),
        ("urn:uuid:" + SPACE_ID, True),
        ("not-a-uuid", False),
        ("", False),
        (SPACE_ID + "x", False),
    ],
)
def test_space_directory_names_follow_the_existing_uuid_rule(name: str, accepted: bool) -> None:
    try:
        uuid.UUID(name)
        existing = True
    except ValueError:
        existing = False
    assert existing is accepted
    assert is_space_directory_name(name) is accepted


def test_space_directory_kind_reports_v3(file_factory: StoreFactory) -> None:
    file_factory.open().close()
    assert space_directory_kind(file_factory.space_dir) == "v3"


def _scan_cache(store: FileStateStore) -> tuple[int, int]:
    scans = store._scans  # noqa: SLF001 - the bound under test is internal state
    return len(scans), sum(scan.payload_bytes for scan in scans.values())


def _text_of(store: FileStateStore, doc_id: str) -> str:
    value = materialize(store.load(doc_id))
    assert value is not None
    return str(value["t"])


def test_delta_scan_cache_is_bounded_by_entries_and_bytes(file_factory: StoreFactory) -> None:
    from hyprial.daemon.impl.orgfs.state.files import SCAN_CACHE_BYTES, SCAN_CACHE_ENTRIES

    store = file_factory.open()
    doc_ids = [f"doc-{index:04d}" for index in range(SCAN_CACHE_ENTRIES + 40)]
    for index, doc_id in enumerate(doc_ids):
        store.commit(Batch(appends=((doc_id, Writer(index + 1).replace_text(doc_id)),)))
    entries, size = _scan_cache(store)
    assert entries <= SCAN_CACHE_ENTRIES
    assert size <= SCAN_CACHE_BYTES
    assert _text_of(store, doc_ids[0]) == doc_ids[0]
    body = "x" * (SCAN_CACHE_BYTES // 4)
    for index in range(6):
        update = Writer(10_000 + index).replace_text(body + str(index))
        store.commit(Batch(appends=((f"big-{index}", update),)))
        entries, size = _scan_cache(store)
        assert size <= SCAN_CACHE_BYTES
    reopened = file_factory.reopen(store)
    for index in range(6):
        assert _text_of(reopened, f"big-{index}") == body + str(index)
    assert _text_of(reopened, doc_ids[-1]) == doc_ids[-1]
