"""Leaf: the orgs this node has left (``state/tailcat/left-orgs.json``).

Imported by both ``org.network.state`` (which re-exports it) and the
membership import's skip rules. It depends on nothing above the kernel, so
neither side can form an import cycle through it (round-2 review, 2026-10-28:
offline -> spaces -> org.network -> tailnet -> migration -> spaces).
"""
from __future__ import annotations

import json
from pathlib import Path

#: JSON array of org names, relative to the daemon's home.
LEFT_ORGS_RELPATH = "state/tailcat/left-orgs.json"


def left_orgs_path(home: Path) -> Path:
    return Path(home) / LEFT_ORGS_RELPATH


def read_left_orgs(home: Path) -> list[str]:
    """The orgs this node has left; an absent or corrupt file means none.

    # LAX(tailnet-cutover): a corrupt file is treated as empty rather than
    # failing loudly; the proper shape is a typed state record with a
    # schema version, like the other files under ``state/tailcat/``.
    """

    try:
        raw = left_orgs_path(home).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return sorted({org for org in value if isinstance(org, str)})
