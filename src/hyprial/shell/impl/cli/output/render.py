"""The generic human view of a result that has no renderer of its own.

Rules, in order (design §4.6):
1. a top-level ``ok: true`` is hidden -- it only matters to machines;
2. scalar fields become aligned ``key: value`` lines, keys verbatim;
3. a list of objects becomes a table (columns in first-seen order) when it
   fits in 120 columns, otherwise one ``key: value`` block per item;
   an empty list reads ``(none)``;
4. a nested object becomes an indented section headed by its key;
5. any other list puts one item per line;
6. values are never cut: an identifier, path or URL is printed whole.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.output.tables import MAX_WIDTH, render_table

_INDENT = "  "


def scalar_text(value: Any) -> str:
    """One value as display text: JSON spelling for null/bools, compact JSON for containers."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (Mapping, list, tuple))


def _is_table(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
    )


def _table(rows: list[Mapping[str, Any]], depth: int) -> list[str] | None:
    """The rows as a table, or None when it would not fit in MAX_WIDTH."""
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(str(key))
    cells = [[scalar_text(row.get(header)) for header in headers] for row in rows]
    prefix = _INDENT * depth
    lines = [prefix + line for line in render_table(None, headers, cells).splitlines()]
    if max(len(line) for line in lines) > MAX_WIDTH:
        return None
    return lines


def _records(rows: list[Mapping[str, Any]], depth: int) -> list[str]:
    """One ``key: value`` block per row, blocks separated by a blank line."""
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index:
            lines.append("")
        lines.extend(_lines(row, depth))
    return lines


def _lines(data: Mapping[str, Any], depth: int) -> list[str]:
    prefix = _INDENT * depth
    scalars = [(str(key), value) for key, value in data.items() if _is_scalar(value)]
    width = max((len(key) for key, _ in scalars), default=0)
    lines = [f"{prefix}{key + ':':<{width + 1}} {scalar_text(value)}" for key, value in scalars]
    for key, value in data.items():
        if _is_scalar(value):
            continue
        if isinstance(value, (list, tuple)) and not value:
            lines.append(f"{prefix}{key}: (none)")
        elif isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.extend(_lines(value, depth + 1) if value else [f"{prefix}{_INDENT}(none)"])
        elif _is_table(value):
            lines.append(f"{prefix}{key}:")
            rows = list(value)
            lines.extend(_table(rows, depth + 1) or _records(rows, depth + 1))
        else:
            lines.append(f"{prefix}{key}:")
            lines.extend(f"{prefix}{_INDENT}- {scalar_text(item)}" for item in value)
    return lines


def render_generic(data: Mapping[str, Any]) -> str:
    """The human view of ``data`` under the rules above."""
    visible = {key: value for key, value in data.items() if not (key == "ok" and value is True)}
    if not visible:
        return "ok"
    return "\n".join(_lines(visible, 0))
