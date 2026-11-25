"""Which account invites this device still owes an answer (§3 O2c).

The account says *what* was accepted on the landing page.  Whether this
device acts on it stays a device fact, and the device facts are the same
two the pickup has always used (§5.3, 2026-10-04 ruling):

* the spaces this node already holds — an invite for one of them is
  settled: joining twice is not a second membership;
* the orgs this node deliberately left — an old accepted invite never
  brings a left org back; only an explicit ``hyprial org join`` does.

Everything here is a pure function over views and sets: no daemon call,
no clock, no I/O, so both the shell adapter and the daemon answer can use
it and be tested without either.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import UNKNOWN, InviteView

__all__ = ["InviteSelection", "select_invites"]


@dataclass(frozen=True, slots=True)
class InviteSelection:
    """The three disjoint halves of one account snapshot."""

    candidates: tuple[InviteView, ...]
    """Invites that still wait for this device's answer, one per space."""

    settled: tuple[InviteView, ...]
    """Already held here, or deliberately left: never auto-joined again."""

    duplicates: tuple[InviteView, ...]
    """A second row for a space already in ``candidates`` (§5.3, one join)."""

    def pending_rows(self) -> list[dict[str, object]]:
        return [view.row() for view in self.candidates]


def select_invites(
    views: Iterable[InviteView],
    *,
    held_spaces: Iterable[str],
    left_orgs: Iterable[str],
) -> InviteSelection:
    """Split account views into candidates, settled rows and duplicates.

    An ``"unknown"`` field never settles and never dedupes by that field:
    the row is kept so its own join can report the decode failure, and two
    different undecodable payloads keep two different identifiers.
    """

    held = {space for space in held_spaces if isinstance(space, str) and space}
    left = {org for org in left_orgs if isinstance(org, str) and org}
    candidates: list[InviteView] = []
    settled: list[InviteView] = []
    duplicates: list[InviteView] = []
    seen_ids: set[str] = set()
    seen_spaces: set[str] = set()
    for view in views:
        if view.space_id != UNKNOWN and view.space_id in held:
            settled.append(view)
            continue
        if view.org != UNKNOWN and view.org in left:
            settled.append(view)
            continue
        if view.invite_id in seen_ids or (
            view.space_id != UNKNOWN and view.space_id in seen_spaces
        ):
            duplicates.append(view)
            continue
        seen_ids.add(view.invite_id)
        if view.space_id != UNKNOWN:
            seen_spaces.add(view.space_id)
        candidates.append(view)
    return InviteSelection(
        candidates=tuple(candidates),
        settled=tuple(settled),
        duplicates=tuple(duplicates),
    )
