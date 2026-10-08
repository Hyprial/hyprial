"""Plain-text tables and relative times shared by the human renderers."""

from __future__ import annotations

MAX_WIDTH = 120
"""Rendered lines stay within this many columns."""


def shorten(value: str, width: int) -> str:
    """``value`` cut to ``width`` columns, ending in ``…`` when cut."""
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "…"


def render_table(
    title: str | None,
    headers: list[str],
    cells: list[list[str]],
    *,
    truncate_column: int | None = None,
) -> str:
    """Left-aligned columns separated by two spaces, under an optional title.

    With ``truncate_column`` the table is narrowed to ``MAX_WIDTH`` by
    shortening that column first, then the widest others.
    """
    widths = [
        max([len(header), *(len(row[index]) for row in cells)])
        for index, header in enumerate(headers)
    ]
    if truncate_column is not None:
        available = MAX_WIDTH - 2 * (len(headers) - 1)
        excess = max(0, sum(widths) - available)
        candidates = [truncate_column] + sorted(
            (index for index in range(len(headers)) if index != truncate_column),
            key=lambda index: widths[index] - len(headers[index]),
            reverse=True,
        )
        for index in candidates:
            reducible = widths[index] - len(headers[index])
            reduction = min(excess, reducible)
            widths[index] -= reduction
            excess -= reduction
            if excess == 0:
                break

    lines = [] if title is None else [title[:MAX_WIDTH]]
    lines.append(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()
    )
    for row in cells:
        lines.append(
            "  ".join(
                shorten(cell, widths[index]).ljust(widths[index])
                for index, cell in enumerate(row)
            ).rstrip()
        )
    return "\n".join(lines)


def duration(milliseconds: int) -> str:
    """A coarse duration: ``42s``, ``5m``, ``3h``, ``2d``."""
    seconds = max(0, milliseconds // 1000)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h"
    return f"{seconds // (24 * 60 * 60)}d"


def age(milliseconds: object, *, now_ms: int) -> str:
    """How long ago an epoch-millisecond timestamp was, or ``-``."""
    if type(milliseconds) is not int:
        return "-"
    return duration(now_ms - milliseconds)


def relative(milliseconds: object, *, now_ms: int) -> str:
    """``in 5m`` for a future timestamp, ``5m ago`` for a past one, or ``-``."""
    if type(milliseconds) is not int:
        return "-"
    if milliseconds >= now_ms:
        return f"in {duration(milliseconds - now_ms)}"
    return f"{duration(now_ms - milliseconds)} ago"
