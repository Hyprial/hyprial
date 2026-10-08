"""Narrow mutable-state tolerance for Codex's projected config."""

from __future__ import annotations

import tomllib


# Allen's 2026-10-08 ruling permits only Codex's exact top-level UI-state table.
CODEX_MUTABLE_CONFIG_TABLES = frozenset({"tui"})


def _comparable_config(body: bytes) -> dict[str, object] | None:
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    for table in CODEX_MUTABLE_CONFIG_TABLES:
        parsed.pop(table, None)
    return parsed


def codex_projection_item_matches(
    native_path: str, projected: bytes, current: bytes
) -> bool:
    """Compare one projected item, tolerating only exact ``config.toml.tui``."""

    if native_path != "config.toml":
        return current == projected
    projected_config = _comparable_config(projected)
    current_config = _comparable_config(current)
    return (
        projected_config is not None
        and current_config is not None
        and current_config == projected_config
    )
