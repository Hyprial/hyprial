"""``CellParamSnapshot`` and the view that follows the cell index (plan §1.1, S7a).

Decision notes (phase A, S7a):

- A snapshot is immutable and built from one ``CellIndex`` generation: the
  trusted sources, the rejected cells, the agent alias groups and the refs
  that more than one agent cell claims (``ambiguous``).  Readers
  hold it for one decision; a newer generation is a new snapshot.
- ``CellParamView`` is the refresher.  It registers an ``on_index`` hook on
  the S3 runtime, so rebuilds run on the ``cell-runtime`` thread after each
  pass that changed the index; there is no refresher thread and no shutdown
  step of its own (plan §2.0).  ``snapshot()`` also compares the index
  generation, an in-memory read, so a local ``ensure``/``update`` is seen
  before the next runtime pass.  ``current()`` never touches the runtime.
- A rebuild that fails keeps the last-known-good snapshot and logs
  ``cell.params_refresh_failed`` (plan Q5 option B).  The runtime index is
  itself last-known-good while OrgFS is unreachable, so params never drop
  to defaults on a disconnect once a snapshot was built.
- ``cell.params_untrusted`` is logged at info when the set of rejected cells
  changes, not on every generation: peers' cells from other authorities
  are routine in a multi-org node, so a refusal is not a fault.
- Rebuilds are serialised by a lock; publishing the snapshot is a single
  attribute store, so readers never take the lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from hyprial.daemon.impl.cells.params.source import (
    ParamSource,
    RejectedSource,
    select_sources,
)
from hyprial.daemon.impl.cells.ports import CellIndex, IndexHook

LOG_LIMIT = 16


@dataclass(frozen=True, slots=True)
class CellParamSnapshot:
    """The trusted param sources of one cell index generation."""

    generation: int = 0
    owner: str | None = None
    sources: tuple[ParamSource, ...] = ()
    rejected: tuple[RejectedSource, ...] = ()
    aliases: Mapping[str, frozenset[str]] = field(default_factory=dict)
    ambiguous: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(self, "ambiguous", frozenset(self.ambiguous))

    def __hash__(self) -> int:
        return hash((self.generation, self.owner, self.sources))

    @property
    def available(self) -> bool:
        """False for the empty snapshot used when no runtime is composed."""

        return self.owner is not None

    @classmethod
    def from_index(cls, index: CellIndex, owner: str) -> CellParamSnapshot:
        selected = select_sources(index.cells.values(), owner)
        return cls(
            index.generation,
            owner,
            selected.sources,
            selected.rejected,
            selected.aliases,
            selected.ambiguous,
        )

    def subject_refs(self, subject: str | None) -> frozenset[str] | None:
        """Every name of *subject* (None: no subject, every source applies).

        An agent subject expands to its identity group; any other subject,
        including an ambiguous ref, is matched literally.
        """

        if subject is None:
            return None
        return self.aliases.get(subject, frozenset({subject}))

    def sources_for(self, subject: str | None) -> tuple[ParamSource, ...]:
        refs = self.subject_refs(subject)
        return tuple(source for source in self.sources if source.applies_to(refs))


EMPTY_SNAPSHOT = CellParamSnapshot()


class IndexSource(Protocol):
    def index(self) -> CellIndex: ...

    def on_index(self, hook: IndexHook) -> Callable[[], None]: ...


class CellParamView:
    """The current ``CellParamSnapshot`` of one cell runtime."""

    def __init__(
        self,
        runtime: IndexSource,
        *,
        owner: str,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.owner = owner
        self._logger = logger
        self._lock = threading.Lock()
        self._unregister: Callable[[], None] | None = None
        self._snapshot = EMPTY_SNAPSHOT
        self._rejected: frozenset[str] = frozenset()
        self._rebuild()

    def bind(self) -> CellParamView:
        """Register the index hook; idempotent."""

        with self._lock:
            if self._unregister is None:
                self._unregister = self.runtime.on_index(self.refresh)
        return self

    def close(self) -> None:
        with self._lock:
            unregister, self._unregister = self._unregister, None
        if unregister is not None:
            unregister()

    def current(self) -> CellParamSnapshot:
        """The last built snapshot, without reading the runtime."""

        return self._snapshot

    def snapshot(self) -> CellParamSnapshot:
        """The snapshot of the runtime's current index generation."""

        return self._rebuild()

    def refresh(self, _generation: int | None = None) -> None:
        """The ``on_index`` hook: rebuild when the generation moved."""

        self._rebuild()

    def _rebuild(self) -> CellParamSnapshot:
        with self._lock:
            held = self._snapshot
            try:
                index = self.runtime.index()
                if held.available and index.generation == held.generation:
                    return held
                fresh = CellParamSnapshot.from_index(index, self.owner)
            except Exception as error:  # noqa: BLE001 - keep the last-known-good snapshot
                self._log(
                    "warn",
                    "cell.params_refresh_failed",
                    generation=held.generation,
                    errorType=type(error).__name__,
                    detail=str(error)[:300],
                )
                return held
            self._snapshot = fresh
            rejected = frozenset(item.cell_id for item in fresh.rejected)
            changed, self._rejected = rejected != self._rejected, rejected
        if changed and rejected:
            self._log(
                "info",
                "cell.params_untrusted",
                generation=fresh.generation,
                count=len(rejected),
                cells=sorted(rejected)[:LOG_LIMIT],
            )
        return fresh

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, "cells", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks a refresh
            pass


__all__ = [
    "CellParamSnapshot",
    "CellParamView",
    "EMPTY_SNAPSHOT",
    "IndexSource",
    "LOG_LIMIT",
]
