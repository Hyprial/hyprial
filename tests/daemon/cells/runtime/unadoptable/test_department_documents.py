"""Retired ``department`` documents: never scanned, read or deleted (contract OC0 §1.10.2, §2.5, §8.6)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.cells.codec import decode
from hyprial.daemon.impl.cells.model import CellBinding, CellError, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime import CellCache
from hyprial.kernel import keyed_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, org_fs, runtime

DEPARTMENT_ID = keyed_cell_id("department", ALICE, "eng")
DEPARTMENT_DOCUMENT = json.dumps(
    {
        "schema": "hyprial-cell/v1",
        "cellId": DEPARTMENT_ID,
        "kind": "department",
        "authority": ALICE,
        "scope": {"kind": "org", "org": "group-acme"},
        "binding": {"type": "keyed", "value": "eng"},
        "name": "eng",
        "members": [{"ref": "agent:alice:mac:a", "role": "member"}],
        "permissions": {},
        "params": {"delivery.bodyRetentionMs": 3_600_000},
        "revision": 1,
        "retired": False,
        "updatedAtMs": 1_000,
    },
    sort_keys=True,
    separators=(",", ":"),
)


class _AccessSpyFs:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.list_calls: list[tuple[str, str]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.write_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def listdir(self, space: str, path: str) -> Any:
        self.list_calls.append((space, path))
        return self.inner.listdir(space, path)

    def read_text_snapshot(self, space: str, path: str) -> Any:
        self.read_calls.append((space, path))
        return self.inner.read_text_snapshot(space, path)

    def write_text(self, space: str, path: str, content: str, **options: Any) -> Any:
        self.write_calls.append((space, path))
        return self.inner.write_text(space, path, content, **options)

    def remove(self, *args: Any, **kwargs: Any) -> Any:
        self.remove_calls.append(args)
        return self.inner.remove(*args, **kwargs)


def test_department_decode_and_cell_spec_are_refused() -> None:
    with pytest.raises(CellError) as dec_raised:
        decode(DEPARTMENT_DOCUMENT)
    assert dec_raised.value.code == "invalid-cell"
    with pytest.raises(CellError) as spec_raised:
        CellSpec(kind="department", binding=CellBinding.keyed("eng"), scope=CellScope.of_org("group-acme"))
    assert spec_raised.value.code == "invalid-cell"


def test_department_directory_is_never_listed_read_written_or_removed(tmp_path: Path) -> None:
    inner, space = org_fs()
    path = f"cells/department/{DEPARTMENT_ID}.json"
    inner.mkdir(space, "cells")
    inner.mkdir(space, "cells/department")
    inner.write_text(space, path, DEPARTMENT_DOCUMENT)
    before = inner.read_text_snapshot(space, path)
    spy = _AccessSpyFs(inner)
    cells, log = runtime(tmp_path, spy)
    try:
        for _ in range(3):
            cells.run_once()
        assert [p for _s, p in spy.list_calls if "department" in p] == []
        assert [p for _s, p in spy.read_calls if "department" in p] == []
        assert spy.write_calls == []
        assert spy.remove_calls == []
        after = inner.read_text_snapshot(space, path)
        assert after.content == before.content and after.version == before.version
        assert cells.lookup(DEPARTMENT_ID) is None
        assert "cell.document_rejected" not in log.names()
        assert "cell.document_blocked" not in log.names()
    finally:
        cells.close()


def test_department_document_under_agent_path_is_rejected_read_only(tmp_path: Path) -> None:
    inner, space = org_fs()
    path = f"cells/agent/{DEPARTMENT_ID}.json"
    inner.mkdir(space, "cells")
    inner.mkdir(space, "cells/agent")
    inner.write_text(space, path, DEPARTMENT_DOCUMENT)
    before = inner.read_text_snapshot(space, path)
    spy = _AccessSpyFs(inner)
    cells, log = runtime(tmp_path, spy)
    try:
        for _ in range(3):
            cells.run_once()
        after = inner.read_text_snapshot(space, path)
        assert after.content == before.content and after.version == before.version
        assert spy.remove_calls == []
        assert spy.write_calls == []
        assert cells.lookup(DEPARTMENT_ID) is None
        rejected = [f for _, e, f in log.events if e == "cell.document_rejected"]
        assert len(rejected) == 1
        assert "cell.document_blocked" not in log.names()
    finally:
        cells.close()


def test_cached_department_row_is_skipped_counted_and_kept_byte_identical(
    tmp_path: Path,
) -> None:
    fs, _space = org_fs()
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.close()
    with sqlite3.connect(tmp_path / "cells.sqlite3") as db:
        db.execute(
            "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
            " VALUES (?, 'department', ?, 1, 1, ?)",
            (DEPARTMENT_ID, ALICE, DEPARTMENT_DOCUMENT),
        )
    cells, log = runtime(tmp_path, fs)
    try:
        assert cells.lookup(DEPARTMENT_ID) is None
        assert DEPARTMENT_ID not in cells.index().cells
        assert cells.dirty() == frozenset()
        skipped = [f for _, e, f in log.events if e == "cell.cache_rows_skipped"]
        assert skipped == [{"count": 1}]
        for _ in range(3):
            cells.run_once()
    finally:
        cells.close()
    with sqlite3.connect(tmp_path / "cells.sqlite3") as db:
        (stored,) = db.execute(
            "SELECT document FROM cells WHERE cell_id = ?", (DEPARTMENT_ID,)
        ).fetchone()
    assert stored == DEPARTMENT_DOCUMENT
