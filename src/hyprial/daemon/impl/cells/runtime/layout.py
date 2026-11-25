"""OrgFS placement of cell documents: ``cells/<kind>/<c.id>.json`` (plan §3).

Decision notes (phase A, S3):

- One document per cell, named by its id under a per-kind directory, in
  every space its scope maps to.  The conversation record segments S6 keeps
  under ``cells/conversation/<c.id>/`` live in the same owner-home space;
  ``conversation`` is a storage category there, not a cell kind.
- Kinds are discovered by listing their directory.
"""

from __future__ import annotations

from hyprial.kernel import is_cell_id_segment

CELLS_ROOT = "cells"
_SUFFIX = ".json"


def kind_directory(kind: str) -> str:
    return f"{CELLS_ROOT}/{kind}"


def cell_filename(cell_id: str) -> str:
    return f"{cell_id}{_SUFFIX}"


def cell_path(kind: str, cell_id: str) -> str:
    return f"{kind_directory(kind)}/{cell_filename(cell_id)}"


def cell_id_from_filename(name: str) -> str | None:
    """The cell id a directory entry names, or None for any other file."""

    if not name.endswith(_SUFFIX):
        return None
    candidate = name[: -len(_SUFFIX)]
    return candidate if is_cell_id_segment(candidate) else None


__all__ = [
    "CELLS_ROOT",
    "cell_filename",
    "cell_id_from_filename",
    "cell_path",
    "kind_directory",
]
