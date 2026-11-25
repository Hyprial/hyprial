"""One cell actor incarnation: a serial, short, I/O-free document interpreter.

Decision notes (cell model slice I5-1, plan §A, §E, §F, §G):

- ``CellActorCore`` is the whole interpreter of one incarnation.  It holds
  only memory values, does a bounded parse (``cells.structure``) per
  document level and never touches a process, OrgFS, SQLite, HTTP or inbox
  surface.  It is also the ``CellActorAccess`` view extensions receive.
- Revision fencing follows plan §A.2/A.3: a level below the observed
  revision and a byte-identical duplicate are no-ops; a same-revision
  different-digest level is a conflict (degraded,
  ``revision-conflict``), and only a higher clean revision clears it.  A
  document with structure problems advances the observed revision and never
  the effective one; the last effective structure is kept for the snapshot.
  A retired tombstone projects ``retired`` and is never revived by a later
  revision.  ``retiring`` cannot be produced in I5-1: no work is admitted
  and no member is stopped, so a tombstone has nothing to drain.
- State judgment (plan §G.2): zero harness members is ``empty``; a clean
  structure with an entry is ``active``; problems, a revision conflict, or
  harness members the document gives no entry are ``degraded`` (reason
  ``no-entry``: the plan's "entry unavailable with no fail-open" clause in
  its strongest form).  Which choice covers a hand-written multi-harness
  document without ``cell.entry`` is flagged for the planner in the slice
  report; parse itself keeps emitting no problem code for it (slice I1).
- The epoch is obtained only when an incarnation handles its first wakeup.  A
  factory may therefore run and be discarded without changing either store.
- The document digest is SHA-256 over the canonical codec text, so it
  changes with any content change, including ``updatedAtMs``; the snapshot
  reports separate observed and effective document digests.
- I5-1 treats a tombstone with no admitted work or member execution fact as
  completed retirement.  The ``retiring`` phase remains for the member
  reconcile slice to own.
- A health level only flips ``source_stale``: an OrgFS outage is not a
  structural failure and never moves the state away from ``active``
  (plan §E); the actor keeps serving the last effective revision.
- ``entry_member`` (cell model I5-2) is the entry query
  of the last effective structure, published with
  state/revision/effective_revision in the same reduction.  A revision
  with problems or a conflict leaves it on the previous effective value,
  and a clean tombstone keeps the tombstone structure's own query; the
  field is a pure query, never work in flight, a credential or a member
  process fact.
- ``CellActorHandler`` reads both input levels on every wakeup, commits the
  resulting projection, then publishes its snapshot.  Commands other than a
  wakeup are offered to installed extensions, in install order.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Iterable

from hyprial.daemon.impl.cells.actor.contracts import (
    CellActorInputPort,
    EPOCH_PREFIX,
    REASON_NO_ENTRY,
    REASON_REVISION_CONFLICT,
    CellActorState,
    CellActorWakeup,
    CellCommandExtension,
    CellMemberPhase,
)
from hyprial.daemon.impl.cells.actor.snapshot import CellActorMemberSnapshot, CellActorSnapshot
from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.structure import CellStructure, parse_structure

__all__ = [
    "CellActorCore",
    "CellActorHandler",
    "CellActorProjection",
    "document_digest",
    "mint_epoch",
]


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def mint_epoch() -> str:
    """A fresh opaque incarnation token (not a revision, never ordered)."""

    return f"{EPOCH_PREFIX}{uuid.uuid4().hex}"


def document_digest(cell: Cell) -> str:
    """SHA-256 over the cell's canonical document text."""

    return hashlib.sha256(encode(cell).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CellActorProjection:
    """The in-memory document projection needed to rebuild an incarnation."""

    observed: Cell | None
    observed_digest: str
    effective: Cell | None
    effective_digest: str
    conflict: bool
    retired_seen: bool
    source_stale: bool
    source_detail: str | None
    updated_at: int


class CellActorCore:
    """One incarnation's interpreter state; pure and I/O-free."""

    __slots__ = (
        "_cell_id",
        "_clock",
        "_conflict",
        "_effective",
        "_effective_digest",
        "_effective_structure",
        "_epoch",
        "_observed",
        "_observed_digest",
        "_observed_structure",
        "_reason",
        "_retired_seen",
        "_source_detail",
        "_source_stale",
        "_state",
        "_updated_at",
        "_sequence",
    )

    def __init__(
        self, *, cell_id: str, epoch: str, clock_ms: Callable[[], int] = _wall_ms
    ) -> None:
        self._cell_id = cell_id
        self._epoch = epoch
        self._clock = clock_ms
        self._observed: Cell | None = None
        self._observed_digest = ""
        self._observed_structure: CellStructure | None = None
        self._effective: Cell | None = None
        self._effective_digest = ""
        self._effective_structure: CellStructure | None = None
        self._conflict = False
        self._retired_seen = False
        self._source_stale = False
        self._source_detail: str | None = None
        self._updated_at = 0
        self._state = CellActorState.EMPTY
        self._reason: str | None = None
        self._sequence = 0

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def observed_revision(self) -> int:
        return self._observed.revision if self._observed is not None else 0

    @property
    def effective_revision(self) -> int:
        return self._effective.revision if self._effective is not None else 0

    @property
    def source_stale(self) -> bool:
        return self._source_stale

    @property
    def state(self) -> CellActorState:
        return self._state

    @classmethod
    def from_projection(
        cls,
        *,
        cell_id: str,
        epoch: str,
        clock_ms: Callable[[], int],
        projection: CellActorProjection,
    ) -> "CellActorCore":
        core = cls(cell_id=cell_id, epoch=epoch, clock_ms=clock_ms)
        core._observed = projection.observed
        core._observed_digest = projection.observed_digest
        core._observed_structure = (
            parse_structure(projection.observed) if projection.observed is not None else None
        )
        core._effective = projection.effective
        core._effective_digest = projection.effective_digest
        core._effective_structure = (
            parse_structure(projection.effective) if projection.effective is not None else None
        )
        core._conflict = projection.conflict
        core._retired_seen = projection.retired_seen
        core._source_stale = projection.source_stale
        core._source_detail = projection.source_detail
        core._updated_at = projection.updated_at
        core._state, core._reason = core._judgement()
        core._sequence = 0
        return core

    def projection(self) -> CellActorProjection:
        return CellActorProjection(
            observed=self._observed,
            observed_digest=self._observed_digest,
            effective=self._effective,
            effective_digest=self._effective_digest,
            conflict=self._conflict,
            retired_seen=self._retired_seen,
            source_stale=self._source_stale,
            source_detail=self._source_detail,
            updated_at=self._updated_at,
        )

    def structure(self) -> CellStructure | None:
        """The last effective structure, or None while there is none."""

        return self._effective_structure

    def apply_document(self, cell: Cell) -> bool:
        """Adopt one document value; False for a stale or duplicate level."""

        digest = document_digest(cell)
        observed = self._observed
        if observed is not None:
            if cell.revision < observed.revision:
                return False
            if cell.revision == observed.revision and digest == self._observed_digest:
                return False
        conflict = (
            observed is not None
            and cell.revision == observed.revision
            and digest != self._observed_digest
        )
        self._observed = cell
        self._observed_digest = digest
        self._observed_structure = parse_structure(cell)
        if cell.retired:
            self._retired_seen = True
        if conflict:
            self._conflict = True
        else:
            self._conflict = False
            if not self._observed_structure.problems:
                self._effective = cell
                self._effective_digest = digest
                self._effective_structure = self._observed_structure
        self._updated_at = self._clock()
        self._state, self._reason = self._judgement()
        return True

    def apply_health(self, source_stale: bool, detail: str | None = None) -> bool:
        """Record the runtime's source health; False when nothing changed."""

        if source_stale == self._source_stale and detail == self._source_detail:
            return False
        self._source_stale = source_stale
        self._source_detail = detail
        self._updated_at = self._clock()
        return True

    def advance_sequence(self) -> None:
        self._sequence += 1

    def snapshot(self) -> CellActorSnapshot:
        """The published read model of this incarnation (plan §G.1)."""

        return CellActorSnapshot(
            cell_id=self._cell_id,
            epoch=self._epoch,
            sequence=self._sequence,
            revision=self.observed_revision,
            effective_revision=self.effective_revision,
            observed_document_digest=self._observed_digest,
            effective_document_digest=self._effective_digest,
            entry_member=self._effective_structure.entry
            if self._effective_structure is not None
            else None,
            state=self._state,
            reason=self._reason,
            source_stale=self._source_stale,
            updated_at_ms=self._updated_at,
            members=self._members(self._state),
            connections=(
                self._effective_structure.connections
                if self._effective_structure is not None
                else ()
            ),
            fail_open=(
                self._effective_structure.fail_open
                if self._effective_structure is not None
                else None
            ),
        )

    def _judgement(self) -> tuple[CellActorState, str | None]:
        if self._observed is None:
            return CellActorState.EMPTY, None
        if self._conflict:
            return (
                CellActorState.RETIRED if self._retired_seen else CellActorState.DEGRADED,
                REASON_REVISION_CONFLICT,
            )
        assert self._observed_structure is not None
        if self._observed_structure.problems:
            return (
                CellActorState.RETIRED if self._retired_seen else CellActorState.DEGRADED,
                self._observed_structure.problems[0].code,
            )
        if self._retired_seen:
            return CellActorState.RETIRED, None
        assert self._effective_structure is not None
        if self._observed.kind == "org":
            if not self._observed.members:
                return CellActorState.EMPTY, None
            return CellActorState.ACTIVE, None
        if not self._effective_structure.harness_members:
            return CellActorState.EMPTY, None
        if self._effective_structure.entry is None:
            return CellActorState.DEGRADED, REASON_NO_ENTRY
        return CellActorState.ACTIVE, None

    def _members(self, state: CellActorState) -> tuple[CellActorMemberSnapshot, ...]:
        structure = self._effective_structure
        if structure is None:
            return ()
        phase = CellMemberPhase.STOPPED if state is CellActorState.RETIRED else CellMemberPhase.MISSING
        return tuple(
            CellActorMemberSnapshot(
                name=member.name,
                role=member.role,
                kind=member.kind,
                phase=phase,
                running=False,
            )
            for member in structure.members
        )


class CellActorHandler:
    """The engine command handler of one cell actor incarnation."""

    __slots__ = (
        "_cell_id",
        "_core",
        "_projection",
        "_clock",
        "_next_epoch",
        "_commit",
        "_extensions",
        "_input_port",
        "_publish",
    )

    def __init__(
        self,
        *,
        cell_id: str,
        core: CellActorCore | None = None,
        projection: CellActorProjection | None = None,
        clock_ms: Callable[[], int] = _wall_ms,
        input_port: CellActorInputPort | None = None,
        next_epoch: Callable[[str], str] | None = None,
        commit: Callable[[str, str, int, CellActorProjection], bool] | None = None,
        publish: Callable[[CellActorSnapshot], bool] | None = None,
        extensions: Iterable[CellCommandExtension] = (),
    ) -> None:
        self._cell_id = cell_id
        self._core = core
        self._projection = projection
        self._clock = clock_ms
        self._next_epoch = next_epoch
        self._commit = commit
        self._input_port = input_port
        self._publish = publish
        self._extensions = tuple(extensions)

    @property
    def core(self) -> CellActorCore:
        """The incarnation state; for tests and diagnostics only."""

        return self._core

    def __call__(self, command: object) -> object:
        if isinstance(command, CellActorWakeup):
            return self._wakeup()
        self._offer_to_extensions(command)
        return None

    def _wakeup(self) -> None:
        if self._input_port is None:
            return None
        if self._core is None:
            if self._next_epoch is None:
                return None
            epoch = self._next_epoch(self._cell_id)
            if self._projection is None:
                self._core = CellActorCore(
                    cell_id=self._cell_id, epoch=epoch, clock_ms=self._clock
                )
            else:
                self._core = CellActorCore.from_projection(
                    cell_id=self._cell_id,
                    epoch=epoch,
                    clock_ms=self._clock,
                    projection=self._projection,
                )
        current = self._input_port.read()
        assert self._core is not None
        if current.document is not None:
            self._core.apply_document(current.document)
        if current.health is not None:
            self._core.apply_health(*current.health)
        self._core.advance_sequence()
        snapshot = self._core.snapshot()
        committed = True
        if self._commit is not None:
            committed = self._commit(
                self._cell_id,
                self._core.epoch,
                snapshot.sequence,
                self._core.projection(),
            )
        if committed and self._publish is not None:
            self._publish(snapshot)
        return None

    def _offer_to_extensions(self, command: object) -> bool:
        for extension in self._extensions:
            if extension.handles(command):
                extension.handle(self._core, command)
                return True
        return False
