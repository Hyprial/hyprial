"""The cell cache identity record and its read-only check (contract OC0 §1.9: T-ID-1 … T-ID-5)."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.model import CellBinding
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime import (
    CellCache,
    CellCacheConflict,
    CellCacheIdentity,
    cell_cache_identity_conflict,
    create_cell_cache,
    read_cell_cache_identity,
)

from tests.daemon.cells.runtime.cell_runtime_support import ALICE, MALLORY, org_fs, runtime

ISSUER = "https://id.example"
SRC = Path(__file__).resolve().parents[5] / "src" / "hyprial"


def _files(state: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(state.iterdir()) if path.is_file()}


def _create(state: Path, *, writer: str = "user:alice", issuer: str = ISSUER) -> Path:
    path = state / "cells.sqlite3"
    create_cell_cache(path, authority=ALICE, issuer=issuer, writer=writer)
    return path


def test_t_id_1_a_new_cache_holds_the_three_keys(tmp_path: Path) -> None:
    path = _create(tmp_path)
    assert not tmp_path.joinpath("cells.sqlite3.new").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cells.sqlite3"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        meta = dict(db.execute("SELECT key, value FROM cache_meta").fetchall())
    assert meta == {"authority": ALICE, "issuer": ISSUER, "writer": "user:alice"}
    assert read_cell_cache_identity(tmp_path) == CellCacheIdentity(ALICE, ISSUER, "user:alice")


def test_t_id_1b_a_leftover_staging_file_fails_closed_and_is_kept(tmp_path: Path) -> None:
    staging = tmp_path / "cells.sqlite3.new"
    staging.write_bytes(b"left by an interrupted create")
    with pytest.raises(FileExistsError):
        _create(tmp_path)
    assert staging.read_bytes() == b"left by an interrupted create"
    assert not tmp_path.joinpath("cells.sqlite3").exists()
    conflict = cell_cache_identity_conflict(
        tmp_path, authority=ALICE, issuer=ISSUER, writer="user:alice"
    )
    assert conflict == CellCacheConflict("cache-unreadable")


def test_t_id_1c_main_cache_with_leftover_staging_fails_closed_and_is_kept(
    tmp_path: Path,
) -> None:
    _create(tmp_path)
    staging = tmp_path / "cells.sqlite3.new"
    staging.write_bytes(b"left by an interrupted replacement")

    conflict = cell_cache_identity_conflict(
        tmp_path, authority=ALICE, issuer=ISSUER, writer="user:alice"
    )

    assert conflict == CellCacheConflict("cache-unreadable")
    assert staging.read_bytes() == b"left by an interrupted replacement"


def test_create_cell_cache_cleans_only_its_own_staging_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "cells.sqlite3").write_bytes(b"real-cache")
    with pytest.raises(FileExistsError):
        _create(existing)
    assert (existing / "cells.sqlite3").read_bytes() == b"real-cache"
    assert not (existing / "cells.sqlite3.new").exists()

    failed = tmp_path / "failed"
    failed.mkdir()

    def failing_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk full"):
        _create(failed)
    assert not (failed / "cells.sqlite3").exists()
    assert not (failed / "cells.sqlite3.new").exists()


def test_t_id_2_every_other_identity_or_state_is_refused_with_its_reason(
    tmp_path: Path,
) -> None:
    def conflict(state: Path, **overrides: str) -> CellCacheConflict | None:
        fields = {"authority": ALICE, "issuer": ISSUER, "writer": "user:alice", **overrides}
        return cell_cache_identity_conflict(state, **fields)

    old = tmp_path / "old"
    old.mkdir()
    with sqlite3.connect(old / "cells.sqlite3") as db:
        db.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO cache_meta VALUES ('author', 'user:alice')")
    old_conflict = conflict(old)
    assert old_conflict == CellCacheConflict(
        "cache-identity-mismatch", ("authority", "issuer", "writer")
    )
    assert old_conflict is not None and old_conflict.keys_csv == "authority,issuer,writer"
    empty = tmp_path / "empty"
    empty.mkdir()
    sqlite3.connect(empty / "cells.sqlite3").close()
    assert conflict(empty) == CellCacheConflict(
        "cache-identity-mismatch", ("authority", "issuer", "writer")
    )
    current = tmp_path / "current"
    current.mkdir()
    _create(current)
    assert conflict(current, authority=MALLORY) == CellCacheConflict(
        "cache-identity-mismatch", ("authority",)
    )
    assert conflict(current, issuer="https://other") == CellCacheConflict(
        "cache-identity-mismatch", ("issuer",)
    )
    assert conflict(current, writer="user:alice2") == CellCacheConflict(
        "cache-identity-mismatch", ("writer",)
    )
    wal_only = tmp_path / "wal-only"
    wal_only.mkdir()
    (wal_only / "cells.sqlite3-wal").write_bytes(b"x")
    assert conflict(wal_only) == CellCacheConflict("cache-unreadable")
    garbage = tmp_path / "garbage"
    garbage.mkdir()
    (garbage / "cells.sqlite3").write_bytes(b"not a database at all" * 64)
    assert conflict(garbage) == CellCacheConflict("cache-unreadable")
    assert read_cell_cache_identity(garbage) is None
    assert conflict(tmp_path / "absent") is None
    assert read_cell_cache_identity(tmp_path / "absent") == "absent"


def test_t_id_3_a_matching_cache_is_kept_with_its_cells(tmp_path: Path) -> None:
    _create(tmp_path, issuer="")
    fs, _space = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cell = cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject("a." + "e" * 26),
                                 params={"delivery.bodyRetentionMs": 3_600_000}))
    cells.run_once()
    cells.close()
    assert cell_cache_identity_conflict(tmp_path, authority=ALICE, issuer="",
                                        writer="user:alice") is None
    again, _ = runtime(tmp_path, fs)
    assert again.lookup(cell.cell_id).param("delivery.bodyRetentionMs") == 3_600_000
    again.close()


def test_t_id_4_only_the_create_flow_writes_cache_meta() -> None:
    writes = re.compile(r"(INSERT|UPDATE|REPLACE|DELETE)[^\n]*cache_meta", re.IGNORECASE)
    hits = sorted(
        f"{path.relative_to(SRC)}"
        for path in SRC.rglob("*.py")
        if writes.search(path.read_text(encoding="utf-8"))
    )
    assert hits == ["daemon/impl/cells/runtime/cache.py"]
    cache_text = (SRC / "daemon/impl/cells/runtime/cache.py").read_text(encoding="utf-8")
    create = cache_text[cache_text.index("def create_cell_cache"):]
    before_create = cache_text[: cache_text.index("def create_cell_cache")]
    assert not writes.search(before_create)
    assert writes.search(create)


def test_t_id_5_the_check_reads_a_live_cache_without_changing_or_adding_files(
    tmp_path: Path,
) -> None:
    path = _create(tmp_path)
    cache = CellCache(path)
    try:
        fs, _space = org_fs()
        from tests.daemon.cells.runtime.cell_runtime_support import Clock, Log
        from hyprial.daemon.impl.cells.runtime import CellRuntime
        from hyprial.daemon.impl.composition.cells.spaces import CellSpaces
        from tests.daemon.cells.runtime.cell_runtime_support import identity_for

        cells = CellRuntime(fs=fs, spaces=CellSpaces(fs), cache=cache,
                            identity=identity_for("user:alice", ISSUER),
                            clock_ms=Clock(), logger=Log())
        for index in range(5):
            cells.ensure(CellSpec(kind="agent",
                                  binding=CellBinding.subject("a." + "fghij"[index] * 26)))
        before = _files(tmp_path)
        assert cell_cache_identity_conflict(tmp_path, authority=ALICE, issuer=ISSUER,
                               writer="user:alice") is None
        assert read_cell_cache_identity(tmp_path) == CellCacheIdentity(
            ALICE, ISSUER, "user:alice")
        assert _files(tmp_path) == before
    finally:
        cache.close()


def test_the_daemon_face_exports_cell_cache_identity_conflict() -> None:
    import hyprial.daemon as daemon

    assert daemon.cell_cache_identity_conflict is cell_cache_identity_conflict
    assert not hasattr(daemon, "cell_authority_report")
