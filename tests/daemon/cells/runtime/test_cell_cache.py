"""The local cell cache (``cells.sqlite3``) survives restarts and OrgFS loss."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellScope
from hyprial.daemon.impl.cells.runtime.cache import CellCache
from hyprial.kernel import keyed_cell_id

from tests.daemon.cells.runtime.cell_runtime_support import (
    RETIRED_GRAPH_DOCUMENT,
    RETIRED_GRAPH_ID,
    RETIRED_ROUTINE_DOCUMENT,
    RETIRED_ROUTINE_ID,
)
from tests.daemon.cells.runtime.cell_runtime_support import ALICE


def _cell(revision: int = 1) -> Cell:
    return Cell(
        cell_id=keyed_cell_id("agent", ALICE, "eng"),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org("acme"),
        binding=CellBinding.keyed("eng"),
        params={"delivery.bodyRetentionMs": 7},
        revision=revision,
    )


def test_cells_and_dirty_flags_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "cells.sqlite3"
    cache = CellCache(path)
    cache.put(_cell(), dirty=True)
    cache.close()
    reopened = CellCache(path)
    rows = reopened.load()
    assert rows == {_cell().cell_id: (_cell(), True)}
    reopened.close()


def test_mark_clean_only_clears_the_revision_that_was_written(tmp_path: Path) -> None:
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.put(_cell(1), dirty=True)
    cache.put(_cell(2), dirty=True)
    assert cache.mark_clean(_cell().cell_id, 1) is False
    assert cache.load()[_cell().cell_id] == (_cell(2), True)
    assert cache.mark_clean(_cell().cell_id, 2) is True
    assert cache.load()[_cell().cell_id] == (_cell(2), False)


def test_copies_track_one_version_per_space(tmp_path: Path) -> None:
    cache = CellCache(tmp_path / "cells.sqlite3")
    cell_id = _cell().cell_id
    cache.set_copy(cell_id, "s1", "n1", "v1")
    cache.set_copy(cell_id, "s1", "n1", "v2")
    cache.set_copy(cell_id, "s2", "n9", "v1")
    assert cache.copies(cell_id) == {"s1": ("n1", "v2"), "s2": ("n9", "v1")}
    cache.drop_copy(cell_id, "s2")
    assert cache.copies(cell_id) == {"s1": ("n1", "v2")}
    assert cache.copies("c." + "a" * 26) == {}


def test_an_unreadable_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.put(_cell(), dirty=False)
    cache._connection.execute("UPDATE cells SET document = '{broken'")
    assert cache.load() == {}
    assert cache.skipped_rows == 1


def test_a_row_of_the_removed_graph_kind_is_skipped_and_kept(tmp_path: Path) -> None:
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.put(_cell(), dirty=False)
    cache._connection.execute(
        "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
        " VALUES (?, 'graph', 'user:alice', 1, 1, ?)",
        (RETIRED_GRAPH_ID, RETIRED_GRAPH_DOCUMENT),
    )
    assert cache.load() == {_cell().cell_id: (_cell(), False)}
    assert cache.skipped_rows == 1
    assert cache._connection.execute(
        "SELECT document FROM cells WHERE cell_id = ?", (RETIRED_GRAPH_ID,)
    ).fetchone() == (RETIRED_GRAPH_DOCUMENT,)
    cache.close()


def test_a_row_of_the_removed_routine_kind_is_skipped_and_kept(tmp_path: Path) -> None:
    cache = CellCache(tmp_path / "cells.sqlite3")
    cache.put(_cell(), dirty=False)
    cache._connection.execute(
        "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
        " VALUES (?, 'routine', 'user:alice', 2, 0, ?)",
        (RETIRED_ROUTINE_ID, RETIRED_ROUTINE_DOCUMENT),
    )
    assert cache.load() == {_cell().cell_id: (_cell(), False)}
    assert cache.skipped_rows == 1
    assert cache._connection.execute(
        "SELECT document FROM cells WHERE cell_id = ?", (RETIRED_ROUTINE_ID,)
    ).fetchone() == (RETIRED_ROUTINE_DOCUMENT,)
    cache.close()
