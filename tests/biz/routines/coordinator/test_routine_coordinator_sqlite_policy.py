"""Each routine coordinator connection applies WAL's NORMAL durability rule."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hyprial.biz.impl.routine.execution.coordinator import _Generation


def test_generation_connection_uses_synchronous_normal(tmp_path) -> None:
    database = tmp_path / "coordinator.sqlite3"
    with sqlite3.connect(database) as bootstrap:
        bootstrap.execute("PRAGMA journal_mode=WAL")
    generation = object.__new__(_Generation)
    generation.owner = SimpleNamespace(database=database)
    connection = generation._connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        connection.close()
