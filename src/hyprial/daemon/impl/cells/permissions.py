"""The cell permission surface and the phase A authoring rule (design §5, §7).

Decision notes (phase A, S3):

- Actions are ordered ``read`` < ``write`` < ``admin``; a grant of a higher
  action implies the lower ones.  ``*`` grants anyone, ``@members`` grants the
  cell's member refs.
- The cell's authority holds every action.  Who may *author* the document is a
  separate, stricter rule: OrgFS readers accept a cell only when every author
  of its document is the authority (``codec.verify``), so the runtime refuses
  any local write whose author is not the authority (plan Q10: local owner
  only in phase A).  Admin grants do not delegate authoring yet.
- ``permits`` is a pure check for consumers (conversations, threads); the
  runtime itself does not interpret read or write grants.
"""

from __future__ import annotations

from hyprial.daemon.impl.cells.model import (
    ANYONE_TOKEN,
    MEMBERS_TOKEN,
    PERMISSION_ACTIONS,
    Cell,
    CellError,
    CellPermissions,
)


def permits(cell: Cell, principal: str, action: str) -> bool:
    """Whether *principal* may perform *action* on *cell*."""

    if action not in PERMISSION_ACTIONS:
        raise CellError("invalid-cell", f"unknown permission action {action!r}")
    if principal == cell.authority:
        return True
    floor = PERMISSION_ACTIONS.index(action)
    for granted in PERMISSION_ACTIONS[floor:]:
        principals = cell.permissions.granted(granted)
        if ANYONE_TOKEN in principals or principal in principals:
            return True
        if MEMBERS_TOKEN in principals and cell.has_member(principal):
            return True
    return False


def require_authority(cell: Cell, author: str) -> None:
    """Refuse authoring *cell* as anyone but its authority."""

    if author != cell.authority:
        raise CellError(
            "not-authority", f"{author} cannot author a cell of {cell.authority}"
        )


__all__ = ["PERMISSION_ACTIONS", "CellPermissions", "permits", "require_authority"]
