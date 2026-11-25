"""One cache, one identity: no rehome, no aliases, no reclaim (contract OC0 §1.8.2, §1.9)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellError, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime import CellCache, CellRuntime, create_cell_cache
from hyprial.kernel import subject_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import (
    ALICE,
    MALLORY,
    identity_for,
    org_fs,
    runtime,
)

AGENT_ID = "a." + "c" * 26
SRC = Path(__file__).resolve().parents[4] / "src" / "hyprial"


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_new_cache_records_its_identity_and_has_no_alias_table(tmp_path: Path) -> None:
    path = tmp_path / "cells.sqlite3"
    create_cell_cache(path, authority=ALICE, issuer="https://id.example", writer="user:alice")
    CellCache(path).close()
    assert "cell_aliases" not in _tables(path)
    with sqlite3.connect(path) as db:
        meta = dict(db.execute("SELECT key, value FROM cache_meta").fetchall())
    assert meta == {"authority": ALICE, "issuer": "https://id.example", "writer": "user:alice"}


def test_the_runtime_exposes_its_identity_and_no_author(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    assert cells.identity == identity_for("user:alice")
    assert not hasattr(cells, "author")
    assert not hasattr(CellRuntime, "author")
    cells.close()


def test_opening_a_cache_never_writes_an_author_key(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    cells.run_once()
    cells.close()
    with sqlite3.connect(tmp_path / "cells.sqlite3") as db:
        keys = {row[0] for row in db.execute("SELECT key FROM cache_meta")}
    assert keys == set()


def test_a_cell_held_under_another_authority_is_never_reclaimed(tmp_path: Path) -> None:
    fs, _ = org_fs()
    foreign = Cell(cell_id=subject_cell_id("agent", AGENT_ID), kind="agent",
                   authority=MALLORY, scope=CellScope.of_org(),
                   binding=CellBinding.subject(AGENT_ID), name="held")
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.put(foreign, dirty=True)
    cache.close()
    cells, log = runtime(tmp_path, fs)
    with pytest.raises(CellError) as raised:
        cells.ensure(CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID)))
    assert raised.value.code == "not-authority"
    assert cells.lookup(foreign.cell_id) == foreign
    assert "cell.subject_reclaimed" not in log.names()
    cells.run_once()
    assert fs.spaces() and not any(
        name.endswith(".json")
        for space in fs.spaces()
        for name in _names(fs, space.space_id, "cells/agent")
    )
    cells.close()


def _names(fs: object, space: str, path: str) -> list[str]:
    try:
        return [entry.name for entry in fs.listdir(space, path)]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - absent directory
        return []


def _hits(root: Path, pattern: re.Pattern[str]) -> list[str]:
    return [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]


def test_the_deletion_closure_leaves_no_rehome_or_alias_code() -> None:
    pattern = re.compile(
        r"_rehome|cell_aliases|\.rehome\(|cell\.rehomed|subject_reclaimed|set_author\("
        r"|(?<![A-Za-z0-9])_repair\b|document_repaired"
    )
    assert _hits(SRC, pattern) == []
