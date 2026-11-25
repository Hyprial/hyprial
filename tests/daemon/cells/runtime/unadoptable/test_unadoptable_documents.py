"""Unadoptable cell documents are read-only (contract OC0 D1, §1.10.3: T-D1-1 … T-D1-10).

Nothing in the cell runtime removes, rewrites or overwrites a document it
will not adopt: the bytes, the OrgFS version and the revision stay, the
local cell stays indexed and dirty, the (space, cell) copy is blocked and
logged once per version, and the next write after an agent moves the
document away (``hyprial fs rm``) clears the block.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellScope
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.cells.runtime import CellCache
from hyprial.identity import org_space_name

from tests.daemon.cells.runtime.cell_runtime_support import (
    ALICE,
    ORG,
    authority_for,
    org_fs,
    runtime,
)

AGENT_ID = "a." + "d" * 26
BOB = authority_for("user:bob")
CELLS_SRC = Path(__file__).resolve().parents[5] / "src" / "hyprial" / "daemon" / "impl" / "cells"


class SpyFs:
    """The OrgFS facade, recording every ``remove`` the runtime asks for."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.removed: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def remove(self, *args: Any, **kwargs: Any) -> Any:
        self.removed.append(args)
        return self.inner.remove(*args, **kwargs)


def _spec() -> CellSpec:
    return CellSpec(kind="agent", binding=CellBinding.subject(AGENT_ID), name="a")


def _agent_cell(authority: str = ALICE, *, name: str = "planted") -> Cell:
    return Cell(cell_id=_spec().binding.derive("agent", authority) or "", kind="agent",
                authority=authority, scope=CellScope.of_org(),
                binding=CellBinding.subject(AGENT_ID), name=name)


def _plant_text(fs: Any, space: str, path: str, text: str, author: str) -> None:
    for directory in ("cells", path.rsplit("/", 1)[0]):
        try:
            fs.mkdir(space, directory)
        except Exception:  # noqa: BLE001 - already there
            pass
    fs.author, previous = author, fs.author
    try:
        fs.write_text(space, path, text)
    finally:
        fs.author = previous


def _old_spelling(cell: Cell) -> str:
    text = encode(cell)
    old = text.replace(cell.authority, "user:alice")
    assert old != text
    return old


def _state(fs: Any, space: str, path: str) -> tuple[str, str, tuple[str, ...]]:
    snapshot = fs.read_text_snapshot(space, path)
    return snapshot.content, str(snapshot.version), tuple(fs.document_authors(space, path))


def _count(log: Any, event: str) -> int:
    return log.names().count(event)


def _blocked_setup(tmp_path: Path) -> tuple[Any, SpyFs, str, str, Any, Any, Cell]:
    inner, space = org_fs()
    fs = SpyFs(inner)
    path = f"cells/agent/{_agent_cell().cell_id}.json"
    _plant_text(inner, space, path, _old_spelling(_agent_cell()), "user:alice")
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    return inner, fs, space, path, cells, log, cell


def test_t_d1_1_an_old_spelling_document_is_never_touched(tmp_path: Path) -> None:
    inner, fs, space, path, cells, log, cell = _blocked_setup(tmp_path)
    before = _state(inner, space, path)
    for _ in range(3):
        cells.run_once()
    assert _state(inner, space, path) == before
    assert fs.removed == []
    assert cells.lookup(cell.cell_id) == cell
    assert cells.dirty() == frozenset({cell.cell_id})
    assert _count(log, "cell.document_blocked") == 1
    cells.close()


def test_t_d1_2_a_tampered_document_is_never_touched(tmp_path: Path) -> None:
    inner, space = org_fs()
    inner.invite(space, "user:mallory")
    fs = SpyFs(inner)
    path = f"cells/agent/{_agent_cell().cell_id}.json"
    _plant_text(inner, space, path, encode(_agent_cell()), "user:mallory")
    before = _state(inner, space, path)
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    for _ in range(3):
        cells.run_once()
    assert _state(inner, space, path) == before
    assert fs.removed == []
    assert cells.lookup(cell.cell_id) == cell and cell.cell_id in cells.dirty()
    blocked = [f for _, e, f in log.events if e == "cell.document_blocked"]
    assert len(blocked) == 1 and blocked[0]["reason"] == "author-mismatch"
    cells.close()


def test_t_d1_3_a_bound_foreign_authority_copy_at_the_local_path_blocks(
    tmp_path: Path,
) -> None:
    inner, space = org_fs()
    inner.invite(space, "user:bob")
    fs = SpyFs(inner)
    foreign = _agent_cell(BOB, name="bob's")
    path = f"cells/agent/{foreign.cell_id}.json"
    _plant_text(inner, space, path, encode(foreign), "user:bob")
    before = _state(inner, space, path)
    cells, log = runtime(tmp_path, fs, writer_bindings=lambda: {BOB: "user:bob"})
    cell = cells.ensure(_spec())
    for _ in range(3):
        cells.run_once()
    assert _state(inner, space, path) == before
    assert fs.removed == []
    assert cells.lookup(cell.cell_id) == cell and cell.cell_id in cells.dirty()
    blocked = [f for _, e, f in log.events if e == "cell.document_blocked"]
    assert len(blocked) == 1 and blocked[0]["reason"] == "foreign-authority"
    cells.close()


def test_t_d1_4_moving_the_document_away_unblocks_the_next_write(tmp_path: Path) -> None:
    inner, fs, space, path, cells, log, cell = _blocked_setup(tmp_path)
    cells.run_once()
    inner.remove(space, path)
    cells.run_once()
    assert cells.dirty() == frozenset()
    content, _version, authors = _state(inner, space, path)
    assert '"revision":%d' % cell.revision in content and authors == ("user:alice",)
    assert cells._blocked == {}
    unblocked = [f for _, e, f in log.events if e == "cell.document_unblocked"]
    assert len(unblocked) == 1 and unblocked[0]["blocked"] == 0
    assert fs.removed == []
    cells.close()


def _clean_then_tampered(tmp_path: Path) -> tuple[Any, SpyFs, str, str, Any, Any, Cell]:
    inner, space = org_fs()
    inner.invite(space, "user:mallory")
    fs = SpyFs(inner)
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    cells.run_once()
    assert cells.dirty() == frozenset()
    path = f"cells/agent/{cell.cell_id}.json"
    text, version = inner.read_text(space, path)
    inner.author = "user:mallory"
    inner.write_text(space, path, text.replace('"name":"a"', '"name":"taken"'),
                     expect_version=version)
    inner.author = "user:alice"
    return inner, fs, space, path, cells, log, cell


def test_t_d1_5_a_scan_marks_the_clean_cell_dirty_and_blocks(tmp_path: Path) -> None:
    inner, fs, space, path, cells, log, cell = _clean_then_tampered(tmp_path)
    before = _state(inner, space, path)
    cells.run_once()
    cells.run_once()
    assert cell.cell_id in cells.dirty()
    assert _count(log, "cell.document_blocked") == 1
    assert _state(inner, space, path) == before
    assert fs.removed == []
    cells.close()


def test_t_d1_5b_the_local_dirty_mark_writes_back_after_the_move(tmp_path: Path) -> None:
    inner, fs, space, path, cells, log, cell = _clean_then_tampered(tmp_path)
    cells.run_once()
    inner.remove(space, path)
    cells.run_once()
    assert cells.dirty() == frozenset()
    assert cells._blocked == {}
    content, _version, authors = _state(inner, space, path)
    assert '"name":"a"' in content and authors == ("user:alice",)
    assert _count(log, "cell.document_unblocked") == 1
    assert fs.removed == []
    cells.close()


def test_t_d1_6_a_restart_logs_the_block_once_more_and_touches_nothing(
    tmp_path: Path,
) -> None:
    inner, fs, space, path, cells, log, _cell = _blocked_setup(tmp_path)
    before = _state(inner, space, path)
    cells.run_once()
    cells.close()
    again, again_log = runtime(tmp_path, fs)
    again.run_once()
    again.run_once()
    assert _count(log, "cell.document_blocked") == 1
    assert _count(again_log, "cell.document_blocked") == 1
    assert _state(inner, space, path) == before
    assert fs.removed == []
    again.close()


def test_t_d1_7_no_revision_moves_on_either_side(tmp_path: Path) -> None:
    inner, _fs, space, path, cells, _log, cell = _blocked_setup(tmp_path)
    document_before = inner.read_text(space, path)[0]
    for _ in range(4):
        cells.run_once()
    assert cells.lookup(cell.cell_id).revision == cell.revision == 1
    assert inner.read_text(space, path)[0] == document_before
    assert '"revision":1' in document_before
    cells.close()


def test_t_d1_8_an_undecodable_cache_row_is_skipped_counted_and_kept(
    tmp_path: Path,
) -> None:
    fs, _space = org_fs()
    old = _agent_cell(name="cached")
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.close()
    with sqlite3.connect(tmp_path / "cells.sqlite3") as db:
        db.execute(
            "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
            " VALUES (?, 'agent', 'user:alice', 1, 1, ?)",
            (old.cell_id, _old_spelling(old)),
        )
    cells, log = runtime(tmp_path, fs)
    assert cells.lookup(old.cell_id) is None
    skipped = [f for _, e, f in log.events if e == "cell.cache_rows_skipped"]
    assert skipped == [{"count": 1}]
    for _ in range(3):
        cells.run_once()
    cells.close()
    with sqlite3.connect(tmp_path / "cells.sqlite3") as db:
        (document,) = db.execute(
            "SELECT document FROM cells WHERE cell_id = ?", (old.cell_id,)
        ).fetchone()
    assert document == _old_spelling(old)


def test_t_d1_10_one_blocked_space_never_stops_the_other(tmp_path: Path) -> None:
    from hyprial.daemon.impl.composition.cells.spaces import CellSpaces

    inner, first_space = org_fs()
    inner.create_space(org_space_name("group-beta"))
    fs = SpyFs(inner)
    order = [ref.space_id for ref in CellSpaces(inner).write_spaces(CellScope.of_org(), "user:alice")]
    assert len(order) == 2 and first_space in order
    blocked_space, free_space = order
    path = f"cells/agent/{_agent_cell().cell_id}.json"
    _plant_text(inner, blocked_space, path, _old_spelling(_agent_cell()), "user:alice")
    before = _state(inner, blocked_space, path)
    cells, log = runtime(tmp_path, fs)
    cell = cells.ensure(_spec())
    cells.run_once()
    cells.run_once()
    assert _state(inner, blocked_space, path) == before
    content, _version, authors = _state(inner, free_space, path)
    assert ALICE in content and authors == ("user:alice",)
    copies = cells._cache.copies(cell.cell_id)
    assert free_space in copies and blocked_space not in copies
    assert cells.dirty() == frozenset({cell.cell_id})
    blocked = [f for _, e, f in log.events if e == "cell.document_blocked"]
    assert [f["space"] for f in blocked] == [blocked_space]
    assert fs.removed == []
    cells.close()


def test_t_d1_9_the_cell_runtime_has_no_repair_or_remove_path() -> None:
    everywhere = re.compile(
        r"(?<![A-Za-z0-9])_repair\b|document_repaired|_fs\.remove\(|\bfs\.remove\("
    )
    hits = [
        f"{path.relative_to(CELLS_SRC)}:{number}"
        for path in sorted(CELLS_SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if everywhere.search(line)
    ]
    assert hits == []
    mirror = (CELLS_SRC / "runtime" / "mirror.py").read_text(encoding="utf-8")
    assert "def remove" not in mirror
    assert ORG
