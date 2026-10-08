"""What a command hands to the output layer instead of printing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

Render = Callable[[Mapping[str, Any]], Any]
"""Human rendering of a result's data: returns a ``str`` or a Rich renderable."""


@dataclass(frozen=True)
class CliResult:
    """One command result.

    ``data`` is the JSON document, emitted as-is under ``--json``.  ``render``
    turns the same data into the human view; without it the generic renderer
    is used.  ``json_indent`` keeps the few commands that pretty-print JSON.
    """

    data: Mapping[str, Any]
    render: Render | None = None
    json_indent: int | None = None


@dataclass(frozen=True)
class CliStream:
    """A declared NDJSON stream: one JSON object per line under ``--json``."""

    items: Iterable[Mapping[str, Any]]
    render_item: Render | None = None
