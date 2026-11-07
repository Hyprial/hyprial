"""Deterministic counters shared by scale-cost contract tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar


T = TypeVar("T")


def at_scales(measure: Callable[[int], T], size: int) -> tuple[T, T]:
    """Measure at N and 4N, where only the fixture's unrelated data grows."""

    return measure(size), measure(size * 4)


def assert_flat(at_n: T, at_4n: T) -> None:
    """Require identical deterministic work at N and 4N."""

    assert at_n == at_4n, (at_n, at_4n)


class CountingCursor:
    """Count SQLite result rows actually fetched by production code."""

    def __init__(self, cursor: Any, count: Callable[[int], None]) -> None:
        self._cursor = cursor
        self._count = count

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        row = next(self._cursor)
        self._count(1)
        return row

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        if row is not None:
            self._count(1)
        return row

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall()
        self._count(len(rows))
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class CountingConnection:
    """Transparent connection proxy with a fetched-row counter."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self.rows_fetched = 0

    def _add(self, amount: int) -> None:
        self.rows_fetched += amount

    def execute(self, *args: Any, **kwargs: Any) -> CountingCursor:
        return CountingCursor(
            self._connection.execute(*args, **kwargs), self._add
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)
