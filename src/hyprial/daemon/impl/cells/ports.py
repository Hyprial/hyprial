"""Ports of the generic cell runtime (plan §1.1: owned by S3, consumed by S4/S6/S7).

Decision notes (phase A, S3):

- ``CellRuntimePort`` is the only way other daemon modules read or author
  cells: ``index``/``lookup`` read an immutable in-memory snapshot and never do
  OrgFS I/O, so they keep answering while OrgFS is unreachable; ``ensure``,
  ``update`` and ``retire`` commit to the local cache first and queue the
  OrgFS write; ``kick`` wakes the runtime thread.
- ``ensure`` is idempotent for keyed and subject bindings: the derived id
  returns the existing cell unchanged.  A ``minted`` spec always creates.
  A cell id held under another authority refuses with ``not-authority``
  (contract OC0 §1.8.2: the subject reclaim went away with the cache
  identity check, which keeps another identity's cells out of the runtime).
- ``lookup`` reads the held cell only; there are no aliases (owner ruling
  2026-11-21: the owner-change re-mint and its alias table are deleted).
- Two principals (contract OC0 §1.4): ``CellIdentity.authority`` is
  ``user:u.<sub>``, the identity written into documents and keyed ids;
  ``CellIdentity.writer`` is the local OrgFS principal ``user:<username>``,
  used only against OrgFS authors and space owners.  ``issuer`` is the
  OIDC issuer the uid belongs to (empty when unknown or overridden).
- ``WriterBindings`` returns the non-local authority -> writer bindings.
  Callers must supply a callable that reads memory only and does not raise;
  the runtime still defends: a raising call keeps the previous snapshot and
  logs ``cell.writer_bindings_failed`` (an empty snapshot when there is no
  previous one).  The local binding is always added and never overridden.
- ``CellSpacesPort.write_spaces`` takes the writer (the local OrgFS
  principal), never a cell authority: owner-home spaces are owned by OrgFS
  principals.
- ``on_index`` hooks run on the runtime thread after each new index
  generation; a raising hook is logged and never stops the runtime or the
  other hooks.
- I5-1 adds the in-memory health snapshot: ``health``/``on_health`` publish
  ``sourceStale`` when a refresh or space pass fails and when it recovers,
  with no new revision involved; the value is memory-only, never an OrgFS
  probe, and health hooks run fenced like index hooks.
- ``CellSpacesPort`` maps a scope to OrgFS spaces; it is the only part that
  knows about org directory spaces and owner-home spaces, and it is
  implemented in ``composition.cells.spaces``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from hyprial.daemon.impl.cells.model import (
    CELL_KINDS,
    Cell,
    CellBinding,
    CellError,
    CellMember,
    CellPermissions,
    CellScope,
    default_scope,
)

SpaceRole = Literal["org", "owner-home"]


@dataclass(frozen=True, slots=True)
class CellSpec:
    """What a caller asks the runtime to ensure."""

    kind: str
    binding: CellBinding
    scope: CellScope | None = None
    name: str = ""
    members: Sequence[CellMember] = ()
    permissions: CellPermissions = field(default_factory=CellPermissions)
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in CELL_KINDS:
            raise CellError("invalid-cell", f"unknown cell kind {self.kind!r}")
        if self.scope is None:
            object.__setattr__(self, "scope", default_scope(self.kind))


@dataclass(frozen=True, slots=True)
class CellIndex:
    """One immutable generation of every cell the runtime knows."""

    generation: int = 0
    cells: Mapping[str, Cell] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", MappingProxyType(dict(self.cells)))

    def get(self, cell_id: str) -> Cell | None:
        return self.cells.get(cell_id)

    def of_kind(self, kind: str, *, include_retired: bool = False) -> tuple[Cell, ...]:
        """Cells of *kind* ordered by id; retired cells only on request."""

        return tuple(
            cell
            for _, cell in sorted(self.cells.items())
            if cell.kind == kind and (include_retired or not cell.retired)
        )


IndexHook = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class CellRuntimeHealth:
    """The runtime's in-memory source health (I5-1).

    ``source_stale`` is True while the last refresh or space pass failed and
    the cache is served without OrgFS; it is a memory value, not a probe.
    """

    source_stale: bool = False
    detail: str | None = None
    updated_at_ms: int = 0


HealthHook = Callable[[CellRuntimeHealth], None]


@dataclass(frozen=True, slots=True)
class CellIdentity:
    """Who the local cell runtime is: the authority, its issuer, its writer."""

    authority: str
    issuer: str
    writer: str


WriterBindings = Callable[[], Mapping[str, str]]


class CellRuntimePort(Protocol):
    @property
    def identity(self) -> CellIdentity: ...

    def index(self) -> CellIndex: ...

    def lookup(self, cell_id: str) -> Cell | None: ...

    def ensure(self, spec: CellSpec) -> Cell: ...

    def update(
        self,
        cell_id: str,
        *,
        name: str | None = None,
        members: Sequence[CellMember] | None = None,
        permissions: CellPermissions | None = None,
        params: Mapping[str, Any] | None = None,
        expect_revision: int | None = None,
    ) -> Cell: ...

    def retire(self, cell_id: str, *, expect_revision: int | None = None) -> Cell: ...

    def kick(self) -> None: ...

    def request_index_retry(self) -> None: ...

    def health(self) -> CellRuntimeHealth: ...

    def on_index(self, hook: IndexHook) -> Callable[[], None]: ...

    def on_health(self, hook: HealthHook) -> Callable[[], None]: ...


@dataclass(frozen=True, slots=True)
class SpaceRef:
    """One OrgFS space the runtime reads or writes cells in."""

    space_id: str
    owner: str
    role: SpaceRole
    org: str | None = None


class CellSpacesPort(Protocol):
    def read_spaces(self) -> tuple[SpaceRef, ...]: ...

    def write_spaces(self, scope: CellScope, writer: str) -> tuple[SpaceRef, ...]: ...


__all__ = [
    "CellIdentity",
    "CellIndex",
    "CellRuntimeHealth",
    "CellRuntimePort",
    "CellSpacesPort",
    "CellSpec",
    "HealthHook",
    "IndexHook",
    "SpaceRef",
    "SpaceRole",
    "WriterBindings",
]
