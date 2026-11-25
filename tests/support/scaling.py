"""Deterministic counters shared by scale-cost contract tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar


T = TypeVar("T")


def at_scales(measure: Callable[[int], T], size: int) -> tuple[T, T]:
    """Measure at N and 4N, where only the fixture's unrelated data grows."""

    return measure(size), measure(size * 4)


def assert_flat(at_n: T, at_4n: T) -> None:
    """Require identical deterministic work at N and 4N."""

    assert at_n == at_4n, (at_n, at_4n)


def classify_growth(sizes: Sequence[int], values: Sequence[int]) -> str:
    """Name deterministic work growth over at least three fixture sizes.

    The classification exists to reject work proportional to the fixture.
    Tests assert the expected class instead of comparing a fixed number.
    """

    if len(sizes) != len(values) or len(sizes) < 3:
        raise ValueError("growth needs at least three paired sizes and values")
    if any(size <= 0 for size in sizes):
        raise ValueError("sizes must be positive")
    if all(value == values[0] for value in values):
        return "constant"
    if any(later < earlier for earlier, later in zip(values, values[1:])):
        return "other"
    increments = [later - earlier for earlier, later in zip(values, values[1:])]
    if all(later <= earlier for earlier, later in zip(increments, increments[1:])):
        return "logarithmic"
    ratios = [value / size for value, size in zip(values, sizes)]
    spread = max(ratios) - min(ratios)
    reference = max(abs(max(ratios)), abs(min(ratios)), 1e-12)
    if spread <= reference * 0.25:
        return "linear"
    return "other"


def assert_work_growth(
    measure: Callable[[int], int],
    sizes: Sequence[int] = (40, 160, 640),
    *,
    expected: str = "constant",
    label: str = "work",
) -> tuple[int, ...]:
    """Measure deterministic work at each size and assert its growth class."""

    values = tuple(int(measure(size)) for size in sizes)
    observed = classify_growth(sizes, values)
    assert observed == expected, (label, tuple(sizes), values, observed)
    return values


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
        self.statements = 0

    def _add(self, amount: int) -> None:
        self.rows_fetched += amount

    def execute(self, *args: Any, **kwargs: Any) -> CountingCursor:
        self.statements += 1
        return CountingCursor(
            self._connection.execute(*args, **kwargs), self._add
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)
