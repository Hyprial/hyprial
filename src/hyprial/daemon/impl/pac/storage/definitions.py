"""A read-only feed of PAC graph definitions for the default-off cell mirror.

Decision notes (phase A, S7c, plan §2.7 and Q14):

- ``pac-graph.sqlite3`` stays the local, authoritative home of run state.
  This module never writes it: the feed opens its own ``mode=ro``
  connection, so a mirror cannot take a PAC writer lock.
- ``digests_after`` is the cheap half of the feed: (position, digest) pairs
  from ``workflow_graphs`` alone, no join, no row objects, so a mirror can
  pass over a long history of definitions it already holds and read full
  rows (``definitions_after``) only for digests it has not settled.
- A definition is one ``workflow_graphs`` row: the graph's name, the sha256
  digest of the canonical specification and the immutable specification
  artifact it was compiled from.  Rows are listed by rowid after a caller's
  cursor, bounded by ``limit``, so a mirror can resume where it stopped.
- The specification text is read back from its artifact and verified against
  the row's digest; a missing, symlinked or changed artifact reads as None
  and is never mirrored.  ``definition_size`` reads only the artifact's
  metadata, so a caller can refuse an oversized artifact without reading it.
- Notification is a per-database sink registry.  ``compile_workflow`` calls
  ``notify_definition_published`` after its commit; with no sink registered
  (the default-off state) the call is a dictionary miss.  A sink is fenced:
  whatever it raises is swallowed, so a mirror can never fail a PAC
  operation.  PAC imports nothing from cells or composition for this.
"""

from __future__ import annotations

import sqlite3
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from hyprial.daemon.impl.pac.storage.store import connect

DefinitionSink = Callable[[Path], None]

_SINKS: dict[Path, DefinitionSink] = {}
_SINKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    """One compiled workflow definition as the PAC store recorded it."""

    position: int
    graph_id: str
    name: str
    digest: str
    specification_ref: str
    on_failure: str


def _key(path: Path) -> Path:
    return Path(path).resolve()


def digests_after(path: Path, cursor: int, *, limit: int) -> tuple[tuple[int, str], ...]:
    """(position, digest) pairs above *cursor*, oldest first, at most *limit*."""

    if limit <= 0 or not Path(path).is_file():
        return ()
    try:
        db = connect(Path(path), read_only=True)
    except sqlite3.Error:
        return ()
    try:
        db.row_factory = None
        rows = db.execute(
            "SELECT rowid, specification_digest FROM workflow_graphs "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (int(cursor), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        db.close()
    return tuple((int(position), str(digest)) for position, digest in rows)


def definitions_after(path: Path, cursor: int, *, limit: int) -> tuple[GraphDefinition, ...]:
    """Definitions whose position is above *cursor*, oldest first, at most *limit*."""

    if limit <= 0 or not Path(path).is_file():
        return ()
    try:
        db = connect(Path(path), read_only=True)
    except sqlite3.Error:
        return ()
    try:
        rows = db.execute(
            "SELECT w.rowid AS position, w.graph_id, g.name, w.specification_digest, "
            "w.specification_ref, w.on_failure "
            "FROM workflow_graphs w JOIN graphs g USING(graph_id) "
            "WHERE w.rowid > ? ORDER BY w.rowid LIMIT ?",
            (int(cursor), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        db.close()
    return tuple(
        GraphDefinition(
            position=int(row["position"]),
            graph_id=str(row["graph_id"]),
            name=str(row["name"]),
            digest=str(row["specification_digest"]),
            specification_ref=str(row["specification_ref"]),
            on_failure=str(row["on_failure"]),
        )
        for row in rows
    )


def definition_size(definition: GraphDefinition) -> int | None:
    """The artifact's size in bytes from its metadata, or None when unavailable."""

    try:
        status = Path(definition.specification_ref).lstat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return int(status.st_size)


def read_definition_text(definition: GraphDefinition) -> str | None:
    """The verified canonical specification text, or None when unavailable."""

    artifact = Path(definition.specification_ref)
    try:
        if artifact.is_symlink():
            return None
        payload = artifact.read_bytes()
    except OSError:
        return None
    if sha256(payload).hexdigest() != definition.digest:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def register_definition_sink(path: Path, sink: DefinitionSink) -> Callable[[], None]:
    """Register *sink* for the database at *path*; returns its unregister."""

    key = _key(path)
    with _SINKS_GUARD:
        _SINKS[key] = sink

    def unregister() -> None:
        with _SINKS_GUARD:
            if _SINKS.get(key) is sink:
                del _SINKS[key]

    return unregister


def notify_definition_published(path: Path) -> None:
    """Tell the sink of *path*, if any, that a definition was committed; never raises."""

    try:
        key = _key(path)
        with _SINKS_GUARD:
            sink = _SINKS.get(key)
        if sink is not None:
            sink(key)
    except Exception:  # noqa: BLE001 - a mirror never fails a PAC operation
        pass


__all__ = [
    "DefinitionSink",
    "GraphDefinition",
    "definition_size",
    "definitions_after",
    "digests_after",
    "notify_definition_published",
    "read_definition_text",
    "register_definition_sink",
]
