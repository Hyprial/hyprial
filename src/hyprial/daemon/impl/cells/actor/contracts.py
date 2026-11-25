"""Stable contracts of the cell actor: states, wakeups, stamps and envelopes.

Decision notes (cell model slice I5-1, plan §2, §A, §D, §E, §G):

- The cell actor is a document interpreter, not a policy engine: member
  names, labels and routes stay opaque tokens carried from the document, and
  no class here names a routing, judging or buffering strategy.  The states
  and phases are the runtime projection vocabulary of plan §A.1 and §G.1;
  ``retiring`` is defined by the contract but cannot be produced without
  admitted work.
- The engine command for a cell actor is a content-free wakeup.  The handler
  reads the current document and health through the runtime's narrow in-memory
  port, so the engine never becomes a second input authority.
- ``CellCommandExtension`` is the actor-side mount: a command no core rule
  claims is offered to the installed extensions together with a read-only
  ``CellActorAccess`` view, so a later slice adds behaviour without editing
  the actor core files.

Decision notes (cell model I5-2 parts B/C, the M′ shape):

- The I5-1 ``CellWorkLease`` reservation is retired without ever being
  wired: M′ admits headless work by reading the published snapshot
  (``headless_admission``), so no ownership credential exists to mint,
  hold, expire or revoke.  The cell actor keeps no in-flight work state and
  is not a party to offer, steer or settlement.
- ``CellDispatchStamp`` is the non-owning diagnostic credential of one
  admitted dispatch: it records which cell, which entry member the snapshot
  published, the epoch/revision of that read and the exact origin message
  ids of the captured input.  It cannot authorise anything by itself, it
  never expires and it is never revoked; the execution-side origin
  reservation (``HarnessRuntimeActor._delivery_reservations``) is the
  work-in-flight authority.
- ``CellDispatchEnvelope`` carries the stamp plus the already captured
  immutable input (a ``HarnessDelivery``), a ``hop_index`` that stays 0
  until I5-4 routes next hops, the dispatch kind and an optional execution
  budget hint.  Its origin ids must equal the input's batch member ids or
  the single scalar delivery id, so a stamp can never describe a different
  work item than the input it travels with.  The identity properties mirror
  the input's delivery fields so dispatch-side consumers can read one shape.

Decision notes (I5-2 second review round, 2026-11-20):

- ``dispatch_kind`` is ``steer`` if and only if the envelope carries no
  input and the stamp names exactly one delivery id; a steer with input and
  a turn without input are both rejected at construction.
- I5-2 stamps are headless-only: the stamp constructor rejects any other
  source value, and the execution side refuses a non-headless stamp again.
  The interactive source remains vocabulary for I5-3's own custody seam and
  cannot travel through this envelope.
- Production envelopes are constructed only by the composition builder in
  ``composition/cell_delivery`` (see ``tests/tooling/architecture/delivery/
  test_cell_entry_gate.py``); the steer kind reuses the original turn stamp
  so a steer never re-reads the cell snapshot to pick a member.
- An extension that needs to widen the access view widens this contract
  file, not the interpreter; raising from ``handle`` is a supervised actor
  failure like any handler fault (plan §F).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot
    from hyprial.daemon.impl.cells.actor.actor import CellActorProjection
    from hyprial.daemon.impl.cells.model import Cell
    from hyprial.daemon.impl.cells.structure import CellStructure

EPOCH_PREFIX = "ce-"

WORK_SOURCE_HEADLESS = "headless-claim"
WORK_SOURCE_INTERACTIVE = "interactive-session"
WORK_SOURCES = (WORK_SOURCE_HEADLESS, WORK_SOURCE_INTERACTIVE)

DISPATCH_TURN = "turn"
DISPATCH_STEER = "steer"
DISPATCH_KINDS = (DISPATCH_TURN, DISPATCH_STEER)

REASON_REVISION_CONFLICT = "revision-conflict"
REASON_NO_ENTRY = "no-entry"


class CellActorState(StrEnum):
    """The runtime projection of one cell (plan §A.1)."""

    EMPTY = "empty"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RETIRING = "retiring"
    RETIRED = "retired"


class CellMemberPhase(StrEnum):
    """One member's process-side phase; never mixed into the cell state."""

    MISSING = "missing"
    STARTING = "starting"
    READY = "ready"
    IN_TURN = "in-turn"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CellActorWakeup:
    """A level-triggered prompt to read the registry's current input."""


@dataclass(frozen=True, slots=True)
class CellActorInput:
    """The immutable input level read by one actor wakeup."""

    document: Cell | None
    document_version: int
    projection: CellActorProjection | None
    health: tuple[bool, str | None] | None
    health_version: int


class CellActorInputPort(Protocol):
    """The actor's narrow, read-only in-memory registry input port."""

    def read(self) -> CellActorInput: ...


@dataclass(frozen=True, slots=True)
class CellDispatchStamp:
    """The non-owning credential of one admitted headless dispatch (M′ M3).

    It names the cell whose snapshot admitted the work, the entry member that
    snapshot published, the epoch/revision observed at that read and the
    exact origin message ids of the captured input.  It authorises nothing
    on its own: the execution-side origin reservation is the work-in-flight
    authority, and epoch/revision are diagnostics that never invalidate work
    already handed to a harness.
    """

    cell_id: str
    member: str
    epoch: str
    revision: int
    origin_message_ids: tuple[str, ...] = ()
    source: str = WORK_SOURCE_HEADLESS

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin_message_ids", tuple(self.origin_message_ids))
        for label, value in (("cell_id", self.cell_id), ("member", self.member),
                             ("epoch", self.epoch)):
            if not isinstance(value, str) or not value:
                raise TypeError(f"{label} must be a non-empty string")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise TypeError("revision must not be negative")
        if self.source != WORK_SOURCE_HEADLESS:
            raise TypeError(f"unknown headless work source {self.source!r}")
        if not self.origin_message_ids:
            raise TypeError("origin message ids must not be empty")
        for value in self.origin_message_ids:
            if not isinstance(value, str) or not value:
                raise TypeError("origin message ids must be non-empty strings")


@dataclass(frozen=True, slots=True)
class CellDispatchEnvelope:
    """One admitted dispatch of a member's work (M′ M3).

    ``stamp`` records the snapshot decision; ``input`` is the already
    captured immutable delivery.  A turn envelope's stamp origin ids must
    equal the input's batch member ids, or the single scalar delivery id; a
    steer envelope carries no input and exactly the one steered delivery id.
    """

    stamp: CellDispatchStamp
    input: object = None
    hop_index: int = 0
    dispatch_kind: str = DISPATCH_TURN
    effect_deadline_ms: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.hop_index, bool) or not isinstance(self.hop_index, int):
            raise TypeError("hop_index must be an integer")
        if self.hop_index < 0:
            raise TypeError("hop_index must not be negative")
        if self.dispatch_kind not in DISPATCH_KINDS:
            raise TypeError(f"unknown dispatch kind {self.dispatch_kind!r}")
        if not isinstance(self.stamp, CellDispatchStamp):
            raise TypeError("stamp must be a CellDispatchStamp")
        origin = tuple(self.stamp.origin_message_ids)
        if self.input is None:
            if self.dispatch_kind != DISPATCH_STEER:
                raise TypeError("an envelope without input must be a steer")
            if len(origin) != 1:
                raise TypeError("a steer stamps exactly one delivery id")
            return
        if self.dispatch_kind == DISPATCH_STEER:
            raise TypeError("a steer envelope carries no input")
        batch = getattr(self.input, "message_ids", None)
        members = tuple(batch) if batch else ()
        expected = members if members else (getattr(self.input, "delivery_id", None),)
        if not expected or expected[0] is None or origin != tuple(expected):
            raise TypeError("stamp origin ids must match the captured input")

    @property
    def delivery_id(self) -> str:
        """The delivery this envelope names; the steered id for a steer."""

        if self.input is None:
            origin = self.stamp.origin_message_ids
            return origin[0] if origin else ""
        return str(getattr(self.input, "delivery_id", ""))

    @property
    def recipient(self) -> str:
        """The input's recipient, kept for identity-reading consumers."""

        return str(getattr(self.input, "recipient", ""))

    @property
    def message_ids(self) -> tuple[str, ...]:
        """The input's ordered settlement ids, empty for a steer envelope."""

        return tuple(getattr(self.input, "message_ids", ()) or ())


class CellActorAccess(Protocol):
    """The read-only view an installed extension receives."""

    @property
    def epoch(self) -> str: ...

    @property
    def observed_revision(self) -> int: ...

    @property
    def effective_revision(self) -> int: ...

    @property
    def state(self) -> CellActorState: ...

    @property
    def source_stale(self) -> bool: ...

    def snapshot(self) -> CellActorSnapshot: ...

    def structure(self) -> CellStructure | None: ...


class CellCommandExtension(Protocol):
    """One later-installed actor-side behaviour (the ``后装`` seam)."""

    def handles(self, command: object) -> bool: ...

    def handle(self, access: CellActorAccess, command: object) -> None: ...


__all__ = [
    "DISPATCH_KINDS",
    "DISPATCH_STEER",
    "DISPATCH_TURN",
    "EPOCH_PREFIX",
    "REASON_NO_ENTRY",
    "REASON_REVISION_CONFLICT",
    "WORK_SOURCES",
    "WORK_SOURCE_HEADLESS",
    "WORK_SOURCE_INTERACTIVE",
    "CellActorAccess",
    "CellActorState",
    "CellCommandExtension",
    "CellDispatchEnvelope",
    "CellDispatchStamp",
    "CellMemberPhase",
]
