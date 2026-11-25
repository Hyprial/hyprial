"""Structure queries: the next hop of a labelled frame, and user push targets.

Decision notes (cell model slice I1, ``cell-model-rulings-and-plan-2026-11-19``
design §5.1, §5.3, §6.2; push default ruled by the owner, review 2026-11-19):

- ``next_hop`` is the whole routing rule of the future cell interpreter: a
  member's terminal frame carries one label; the connection whose ``from``
  is the member and whose ``on`` carries that label names the next member
  (a self-loop names the member itself).  Duplicates were already excluded
  at parse time, so the first match is the only match.  A member without a
  matching connection goes to the cell's fail-open member; without one,
  the work ends.  A *from_member* the cell does not have simply never
  matches, like any other non-matching scan.  The label must be a
  non-empty string; anything else is refused with ``TypeError``.
- ``user_push_targets`` reads one member's outbound binding under the
  ruled default: the member's own connections on the reserved label
  ``user`` towards adapter members are its targets, in document order.  A
  member without such a connection gets every adapter member with no
  route when no connection anywhere in the cell carries the label
  ``user`` (the cell is silent on pushes, so the default applies); when
  some connection in the cell carries the label, the data has already
  expressed who pushes and the member gets nothing.  A connection on
  ``user`` towards a non-adapter member is a normal hop, not a push
  target, and it still counts as the cell expressing itself on ``user``.
- ``user_push_targets`` is the whole push surface of a cell that has an
  emitting member: there is no separate default-targets query, because the
  default is part of the ruled answer rather than a caller's decision.

Decision notes (I7a, contract i7-0 v2 section 2.2, owner items A-1 and A-8):

- ``cell_push_targets`` answers the same question for a cell with no
  emitting member, the user agent cell: every connection on ``user``
  towards an adapter member, in document order; when no connection in the
  cell carries ``user``, every adapter member with no route.  A cell with
  a harness member is refused with ``TypeError``: it has an emitter and
  uses ``user_push_targets``, so this lookup never becomes a generic
  fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from hyprial.daemon.impl.cells.structure.parse import (
    ADAPTER_ROLE,
    USER_LABEL,
    CellStructure,
)


def _token(value: object) -> bool:
    return isinstance(value, str) and bool(value)


@dataclass(frozen=True, slots=True)
class HopToMember:
    """The work goes to this member."""

    member: str

    def __post_init__(self) -> None:
        if not _token(self.member):
            raise TypeError("hop member must be a non-empty string")


@dataclass(frozen=True, slots=True)
class HopToFailOpen:
    """No connection matched; the work goes to the fail-open member."""

    member: str

    def __post_init__(self) -> None:
        if not _token(self.member):
            raise TypeError("hop member must be a non-empty string")


@dataclass(frozen=True, slots=True)
class HopEnd:
    """No connection matched and the cell has no fail-open member."""


@dataclass(frozen=True, slots=True)
class PushTarget:
    """One adapter member a user-bound message goes to, with its route."""

    member: str
    route: str | None = None

    def __post_init__(self) -> None:
        if not _token(self.member):
            raise TypeError("push target member must be a non-empty string")
        if self.route is not None and not _token(self.route):
            raise TypeError("push target route must be None or a non-empty string")


Hop = Union[HopToMember, HopToFailOpen, HopEnd]


def next_hop(structure: CellStructure, from_member: str, label: str) -> Hop:
    """Where the work goes when *from_member* emits *label*."""

    if not isinstance(label, str) or not label:
        raise TypeError(f"label must be a non-empty string, got {label!r}")
    for connection in structure.connections:
        if connection.from_member == from_member and label in connection.on:
            return HopToMember(connection.to)
    if structure.fail_open is not None:
        return HopToFailOpen(structure.fail_open)
    return HopEnd()


def user_push_targets(
    structure: CellStructure, member: str
) -> tuple[PushTarget, ...]:
    """The adapter targets for *member*'s messages bound for the user."""

    own = tuple(
        connection
        for connection in structure.connections
        if connection.from_member == member and USER_LABEL in connection.on
    )
    if own:
        return tuple(
            PushTarget(connection.to, connection.route)
            for connection in own
            if _is_adapter(structure, connection.to)
        )
    if any(USER_LABEL in connection.on for connection in structure.connections):
        return ()
    return tuple(
        PushTarget(member.name)
        for member in structure.members
        if member.role == ADAPTER_ROLE
    )


def cell_push_targets(structure: CellStructure) -> tuple[PushTarget, ...]:
    """The adapter targets of a cell that has no emitting member."""

    if any(member.role != ADAPTER_ROLE for member in structure.members):
        raise TypeError("a cell with an emitting member uses user_push_targets")
    rows = tuple(
        connection
        for connection in structure.connections
        if USER_LABEL in connection.on
    )
    if rows:
        return tuple(
            PushTarget(connection.to, connection.route)
            for connection in rows
            if _is_adapter(structure, connection.to)
        )
    return tuple(PushTarget(member.name) for member in structure.members)


def _is_adapter(structure: CellStructure, name: str) -> bool:
    member = structure.member(name)
    return member is not None and member.role == ADAPTER_ROLE


__all__ = [
    "Hop",
    "HopEnd",
    "HopToFailOpen",
    "HopToMember",
    "PushTarget",
    "cell_push_targets",
    "next_hop",
    "user_push_targets",
]
