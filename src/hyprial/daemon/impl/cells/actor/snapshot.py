"""The published read model of one cell actor (plan §G.1).

Decision notes (cell model slice I5-1):

- ``CellActorSnapshot`` is the actor's published value, not an OrgFS query
  and not the URI-keyed ``CellSnapshot`` of ``cells.observability``; the
  near-name is deliberate distance, keeping the two read models separate.
- Reads never ask an actor: the store holds the last value each incarnation
  published, behind a lock held only for dict access, so one slow cell can
  never delay another cell's snapshot read (plan §F).
- ``effective_revision`` is 0 while no executable structure exists; a
  document with problems advances ``revision`` (observed) but not
  ``effective_revision`` (plan §A.3).  ``active_work`` and the work counters
  exist in the shape the later slices fill; I5-1 admits no work, so they
  rest at their empty values.
- ``observed_document_digest`` is paired with ``revision`` and
  ``effective_document_digest`` is paired with ``effective_revision``.  A
  compatibility property retains the original effective digest lookup.
- ``entry_member`` (cell model I5-2) is the pure entry
  query of the last effective structure — ``parse_structure(effective)``
  already answers it: the only harness member when the document carries no
  ``cell.entry``, the valid entry member when it does, and None for zero
  members, several harness members without an entry, or an entry no member
  matches.  It is published in the same reduction as
  state/revision/effective_revision, follows the previous effective
  structure under problems or a conflict like every other effective field,
  and is not work in flight, not a credential and not a member process
  fact.  Consumers start headless work only through ``headless_admission``:
  one judgment of this read model, taken at the moment the
  snapshot is returned — an active state with an entry admits that member;
  a missing snapshot, any other state, or a missing entry defers.
- Member phases come from member facts the later slices feed in; I5-1 has
  none, so a member of a live cell reads ``missing`` and a member of a
  retired cell reads ``stopped``.  The cell state and the member phase are
  separate vocabularies and are never mixed (plan §G.1).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from hyprial.daemon.impl.cells.actor.contracts import CellActorState, CellMemberPhase
from hyprial.daemon.impl.cells.structure import StructureConnection

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.actor import CellActorProjection


def _token(value: object) -> bool:
    return isinstance(value, str) and bool(value)


@dataclass(frozen=True, slots=True)
class CellActorMemberSnapshot:
    """One member as the actor projects it."""

    name: str
    role: str
    kind: str | None = None
    phase: CellMemberPhase = CellMemberPhase.MISSING
    running: bool = False
    work_id: str | None = None
    last_error: str | None = None
    policy_revision: int | None = None
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        if not _token(self.name):
            raise TypeError("member name must be a non-empty string")
        if not _token(self.role):
            raise TypeError("member role must be a non-empty string")
        if self.kind is not None and not _token(self.kind):
            raise TypeError("kind must be None or a non-empty string")


@dataclass(frozen=True, slots=True)
class CellActiveWork:
    """The one work chain currently inside the cell."""

    work_id: str
    member: str
    hop_index: int = 0
    started_at_ms: int = 0

    def __post_init__(self) -> None:
        if not _token(self.work_id) or not _token(self.member):
            raise TypeError("work_id and member must be non-empty strings")
        if isinstance(self.hop_index, bool) or not isinstance(self.hop_index, int):
            raise TypeError("hop_index must be an integer")
        if self.hop_index < 0:
            raise TypeError("hop_index must not be negative")


@dataclass(frozen=True, slots=True)
class CellActorSnapshot:
    """The immutable published snapshot of one cell actor (plan §G.1)."""

    cell_id: str
    epoch: str
    sequence: int = 0
    revision: int = 0
    effective_revision: int = 0
    observed_document_digest: str = ""
    effective_document_digest: str = ""
    entry_member: str | None = None
    state: CellActorState = CellActorState.EMPTY
    reason: str | None = None
    source_stale: bool = False
    updated_at_ms: int = 0
    members: tuple[CellActorMemberSnapshot, ...] = ()
    active_work: CellActiveWork | None = None
    queued_work: int = 0
    completed_work: int = 0
    last_terminal: str | None = None
    connections: tuple[StructureConnection, ...] = ()
    fail_open: str | None = None

    def __post_init__(self) -> None:
        if not _token(self.cell_id) or not _token(self.epoch):
            raise TypeError("cell_id and epoch must be non-empty strings")
        for label, value in (("sequence", self.sequence),
                             ("revision", self.revision),
                             ("effective_revision", self.effective_revision)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{label} must be a non-negative integer")
        if not isinstance(self.source_stale, bool):
            raise TypeError("source_stale must be a boolean")
        if self.entry_member is not None and not _token(self.entry_member):
            raise TypeError("entry_member must be None or a non-empty string")
        members = tuple(self.members)
        if not all(isinstance(member, CellActorMemberSnapshot) for member in members):
            raise TypeError("members must be CellActorMemberSnapshot values")
        object.__setattr__(self, "members", members)
        connections = tuple(self.connections)
        if not all(isinstance(conn, StructureConnection) for conn in connections):
            raise TypeError("connections must be StructureConnection values")
        object.__setattr__(self, "connections", connections)
        if self.fail_open is not None and not _token(self.fail_open):
            raise TypeError("fail_open must be None or a non-empty string")

    @property
    def document_digest(self) -> str:
        """The effective digest kept for callers of the original field."""

        return self.effective_document_digest or self.observed_document_digest


@dataclass(frozen=True, slots=True)
class HeadlessAdmission:
    """Whether one snapshot read admits headless work, and its entry member.

    Cell model I5-2: the value a bridge slice consumes.
    A deferring decision carries no member; an admitted one always names the
    entry member its snapshot published.
    """

    member: str | None = None
    admitted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise TypeError("admitted must be a boolean")
        if self.member is not None and not _token(self.member):
            raise TypeError("member must be None or a non-empty string")
        if self.admitted and self.member is None:
            raise TypeError("an admitted decision names its entry member")


def headless_admission(snapshot: CellActorSnapshot | None) -> HeadlessAdmission:
    """The admission judgment of one published snapshot read.

    Only an ``active`` snapshot with a published ``entry_member`` admits
    headless work; a missing snapshot, every other state, or a missing
    entry defers.  The snapshot read that produced this decision is the
    linearization point of the work decision: later document or health
    changes belong to the next read, never to this one.
    """

    if snapshot is None:
        return HeadlessAdmission()
    if snapshot.state is not CellActorState.ACTIVE:
        return HeadlessAdmission()
    if snapshot.entry_member is None:
        return HeadlessAdmission()
    return HeadlessAdmission(snapshot.entry_member, True)


class CellActorHistoryStore:
    """The last committed reduction for each cell."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, CellActorProjection] = {}
        self._committed: dict[str, tuple[str, int, int]] = {}
        self._next: dict[str, int] = {}
        self._ranks: dict[str, dict[str, int]] = {}

    def read(self, cell_id: str) -> CellActorProjection | None:
        with self._lock:
            return self._values.get(cell_id)

    def next_epoch(self, cell_id: str) -> str:
        with self._lock:
            number = self._next.get(cell_id, 0) + 1
            self._next[cell_id] = number
            epoch = f"ce-{number:020d}-{uuid.uuid4().hex}"
            self._ranks.setdefault(cell_id, {})[epoch] = number
            return epoch

    def commit(
        self,
        cell_id: str,
        epoch: str,
        sequence: int,
        projection: CellActorProjection,
    ) -> bool:
        with self._lock:
            rank = self._rank_locked(cell_id, epoch)
            current = self._committed.get(cell_id)
            if current is not None and (rank, sequence) <= (current[2], current[1]):
                return False
            self._committed[cell_id] = (epoch, sequence, rank)
            self._values[cell_id] = projection
            return True

    def _rank_locked(self, cell_id: str, epoch: str) -> int:
        ranks = self._ranks.setdefault(cell_id, {})
        rank = ranks.get(epoch)
        if rank is not None:
            return rank
        try:
            rank = int(epoch.split("-", 2)[1])
        except (IndexError, ValueError):
            rank = self._next.get(cell_id, 0) + 1
        self._next[cell_id] = max(self._next.get(cell_id, 0), rank)
        ranks[epoch] = rank
        return rank


class CellActorSnapshotStore:
    """The latest published snapshot for each cell."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, CellActorSnapshot] = {}
        self._ranks: dict[str, dict[str, int]] = {}

    def publish(self, cell_id: str, snapshot: CellActorSnapshot) -> bool:
        with self._lock:
            rank = self._rank_locked(cell_id, snapshot.epoch)
            current = self._values.get(cell_id)
            if current is not None:
                current_rank = self._rank_locked(cell_id, current.epoch)
                if (rank, snapshot.sequence) <= (current_rank, current.sequence):
                    return False
            self._values[cell_id] = snapshot
            return True

    def _rank_locked(self, cell_id: str, epoch: str) -> int:
        ranks = self._ranks.setdefault(cell_id, {})
        rank = ranks.get(epoch)
        if rank is not None:
            return rank
        try:
            rank = int(epoch.split("-", 2)[1])
        except (IndexError, ValueError):
            rank = max(ranks.values(), default=0) + 1
        ranks[epoch] = rank
        return rank

    def get(self, cell_id: str) -> CellActorSnapshot | None:
        with self._lock:
            return self._values.get(cell_id)

    def snapshots(self) -> Mapping[str, CellActorSnapshot]:
        with self._lock:
            return dict(self._values)

    def cell_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._values))


__all__ = [
    "CellActiveWork",
    "CellActorMemberSnapshot",
    "CellActorSnapshot",
    "CellActorHistoryStore",
    "CellActorSnapshotStore",
    "HeadlessAdmission",
    "headless_admission",
]
