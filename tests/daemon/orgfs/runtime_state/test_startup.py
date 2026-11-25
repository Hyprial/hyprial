# Decisions:
# - Design §2.3 / §2.7 / §9 R6, C1 report §12: the startup scan opens v3
#   spaces, refuses an old (``journal.sqlite3``) or unknown/corrupt layout for
#   that space only with ``ORGFS_LAYOUT_UNSUPPORTED`` and its ``reason``,
#   never writes a refused directory, and deletes unfinished creations (no
#   ``space.json``, or v3 with no meta ``space`` root).
# - Crash recovery is exercised through the runtime: a commit that dies right
#   after ``batch.pending`` became durable is redone at the next start
#   (``orgfs.state.batch-recovered``); a torn ``batch.pending`` is discarded
#   (``orgfs.state.batch-discarded``); the space stays usable either way.
"""Startup scan and crash recovery of ``StateOrgFsRuntime``."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import struct
import uuid

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime
from hyprial.daemon.impl.orgfs.state import FileStateStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError
from hyprial.kernel import ipc_errors

from .support import Events, snapshot, text

UNSUPPORTED = ipc_errors.ORGFS_LAYOUT_UNSUPPORTED


def _runtime(state_dir: Path, events: Events | None = None) -> StateOrgFsRuntime:
    return StateOrgFsRuntime(state_dir, node_id="node-a", author="user:owner", logger=events)


def _space_dir(state_dir: Path, space_id: str) -> Path:
    return state_dir / "orgfs" / "spaces" / space_id


def _seed(state_dir: Path) -> str:
    runtime = _runtime(state_dir)
    try:
        space = runtime.facade.create_space("kept")
        runtime.facade.write_text(space.space_id, "a.md", "alpha")
        return space.space_id
    finally:
        runtime.close()


def _new_dir(root: Path) -> Path:
    path = root / str(uuid.uuid4())
    path.mkdir(parents=True)
    return path


def test_startup_opens_v3_refuses_other_layouts_per_space_and_discards_unfinished(tmp_path) -> None:
    state_dir = tmp_path / "node"
    kept = _seed(state_dir)
    root = state_dir / "orgfs" / "spaces"
    legacy = _new_dir(root)
    (legacy / "journal.sqlite3").write_bytes(b"SQLite format 3\x00" + bytes(range(64)))
    (legacy / "blobs").mkdir()
    (legacy / "blobs" / "kept.bin").write_bytes(b"legacy blob")
    unknown = _new_dir(root)
    (unknown / "space.json").write_text('{"layoutVersion": 2}')
    mismatch = _new_dir(root)
    (mismatch / "space.json").write_text(
        json.dumps({"layoutVersion": 3, "spaceId": str(uuid.uuid4()), "createdAt": "x"})
    )
    unfinished = _new_dir(root)
    (unfinished / "docs").mkdir()
    rootless = root / str(uuid.uuid4())
    FileStateStore.open(rootless, create=True).close()
    refused = {legacy: "legacy-journal", unknown: "unknown-space-json", mismatch: "space-id-mismatch"}
    before = {path: snapshot(path) for path in refused}
    events = Events()

    runtime = _runtime(state_dir, events)
    try:
        assert [info.space_id for info in runtime.facade.spaces()] == [kept]
        assert text(runtime.facade, kept, "a.md") == "alpha"
        runtime.facade.write_text(kept, "b.md", "beta")
        for path, reason in refused.items():
            with pytest.raises(OrgFsError) as error:
                runtime.facade.stat(path.name, "id:root")
            assert error.value.code == UNSUPPORTED
            assert error.value.details["reason"] == reason
            with pytest.raises((OrgFsError, StoreError)) as again:
                runtime.facade.create_space("taken", space_id=path.name)
            assert again.value.code == UNSUPPORTED
        assert not unfinished.exists()
        assert not rootless.exists()
        assert events.names().count("orgfs.space.layout-unsupported") == 3
        assert events.names().count("orgfs.space.unfinished-discarded") == 2
    finally:
        runtime.close()
    for path in refused:
        assert snapshot(path) == before[path]


def test_a_directory_not_named_by_a_uuid_refuses_startup(tmp_path) -> None:
    state_dir = tmp_path / "node"
    (state_dir / "orgfs" / "spaces" / "not-a-space").mkdir(parents=True)

    with pytest.raises(StoreError) as error:
        _runtime(state_dir)

    assert error.value.code == ipc_errors.ORGFS_INVALID_SPACE_DIRECTORY


def test_a_commit_that_died_after_its_batch_became_durable_is_redone_at_start(tmp_path) -> None:
    state_dir = tmp_path / "node"
    runtime = _runtime(state_dir)
    space_id = runtime.facade.create_space("kept").space_id
    runtime.facade.write_text(space_id, "a.md", "alpha")
    store = runtime.stores.get(space_id)
    assert store is not None

    def crash(batch: object) -> None:
        store._write_pending(batch)  # noqa: SLF001 - the crash point under test
        raise OSError("simulated crash after batch.pending became durable")

    store.commit = crash
    with pytest.raises(OrgFsError):
        runtime.facade.write_text(space_id, "b.md", "beta")
    with pytest.raises(OrgFsError):
        runtime.facade.stat(space_id, "b.md")
    assert (_space_dir(state_dir, space_id) / "batch.pending").exists()
    runtime.close()

    events = Events()
    runtime = _runtime(state_dir, events)
    try:
        assert "orgfs.state.batch-recovered" in events.names()
        assert not (_space_dir(state_dir, space_id) / "batch.pending").exists()
        assert text(runtime.facade, space_id, "a.md") == "alpha"
        assert text(runtime.facade, space_id, "b.md") == "beta"
        runtime.facade.write_text(space_id, "c.md", "gamma")
        assert text(runtime.facade, space_id, "c.md") == "gamma"
    finally:
        runtime.close()


def test_a_torn_batch_is_discarded_at_start_and_the_space_stays_usable(tmp_path) -> None:
    state_dir = tmp_path / "node"
    space_id = _seed(state_dir)
    body = json.dumps(
        {"appends": [["tree", base64.b64encode(b"\x00\x00").decode()]], "deletes": []}
    ).encode()
    crc = (binascii.crc32(body) & 0xFFFFFFFF) ^ 1
    pending = _space_dir(state_dir, space_id) / "batch.pending"
    pending.write_bytes(b"ORGFSBT3" + struct.pack(">II", len(body), crc) + body)
    events = Events()

    runtime = _runtime(state_dir, events)
    try:
        assert "orgfs.state.batch-discarded" in events.names()
        assert not pending.exists()
        assert text(runtime.facade, space_id, "a.md") == "alpha"
        runtime.facade.write_text(space_id, "a.md", "alpha 2")
        assert text(runtime.facade, space_id, "a.md") == "alpha 2"
    finally:
        runtime.close()
