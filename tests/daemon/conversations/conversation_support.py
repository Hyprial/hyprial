"""Shared doubles for the conversation recorder tests (isolated LocalOrgFs)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.runtime import CellCache, CellRuntime
from tests.daemon.cells.runtime.cell_runtime_support import identity_for
from hyprial.daemon.impl.composition.cells.spaces import CellSpaces
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.api import OrgFsError

OWNER_PRINCIPAL = "user:op"
_GUARDED = frozenset(
    {
        "create_space",
        "mkdir",
        "read_text",
        "read_text_snapshot",
        "spaces",
        "space_meta",
        "stat",
        "update_space_meta",
        "write_text",
    }
)


class FlakyOrgFs:
    """A LocalOrgFs proxy that answers ``unavailable`` while ``down``."""

    def __init__(self, fs: LocalOrgFs) -> None:
        self.inner = fs
        self.down = False
        self.refused: list[str] = []

    @property
    def author(self) -> str:
        return self.inner.author

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.inner, name)
        if name not in _GUARDED:
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.down:
                self.refused.append(name)
                raise OrgFsError("unavailable", {"message": "orgfs is down"})
            return attribute(*args, **kwargs)

        return guarded


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, component, event, fields))

    def names(self) -> list[str]:
        return [event for _, _, event, _ in self.events]


def cell_runtime_for(tmp_path: Path, fs: Any) -> CellRuntime:
    """A cell runtime over *fs* authored as the local owner (thread not started)."""

    return CellRuntime(
        fs=fs,
        spaces=CellSpaces(fs),
        cache=CellCache(tmp_path / "cells.sqlite3"),
        identity=identity_for(fs.author),
    )


def inbox_bodies(database: Path, table: str = "inbox") -> dict[str, dict[str, Any]]:
    """Every row of *table* by message id: payload and body columns."""

    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT message_id, payload, body_dropped_at_ms, body_payload_sha256 FROM {table}"
        ).fetchall()
    return {
        str(row["message_id"]): {
            "payload": bytes(row["payload"]),
            "dropped": row["body_dropped_at_ms"] is not None,
            "granted": row["body_payload_sha256"] is not None,
        }
        for row in rows
    }


def record_lines(fs: Any, space_id: str, cell_id: str) -> list[dict[str, Any]]:
    """Every record line written for *cell_id* in *space_id*, in file order."""

    base = f"cells/conversation/{cell_id}"
    found: list[dict[str, Any]] = []
    for writer in sorted(fs.listdir(space_id, base), key=lambda node: node.name):
        if writer.kind != "dir":
            continue
        for segment in sorted(
            fs.listdir(space_id, f"{base}/{writer.name}"), key=lambda node: node.name
        ):
            content, _ = fs.read_text(space_id, f"{base}/{writer.name}/{segment.name}")
            found.extend(json.loads(line) for line in content.splitlines() if line)
    return found


def home_space(fs: Any) -> str | None:
    """The owner-home space id of the local author, if one exists."""

    homes = CellSpaces(fs).home_spaces(fs.author)
    return homes[0].space_id if homes else None


__all__ = [
    "OWNER_PRINCIPAL",
    "FlakyOrgFs",
    "Log",
    "cell_runtime_for",
    "home_space",
    "inbox_bodies",
    "record_lines",
]
