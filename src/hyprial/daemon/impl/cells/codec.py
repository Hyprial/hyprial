"""Cell document codec (``hyprial-cell/v1``) and the reader verification rule.

Decision notes (phase A, S3):

- One canonical JSON spelling: sorted keys, compact separators, no NaN.  The
  same cell always encodes to the same bytes, so an unchanged revision never
  produces an OrgFS write.
- ``decode`` is strict: an unknown schema, a missing field or an id that does
  not re-derive from its binding refuses the whole document.  Unknown extra
  top-level keys are ignored so a later minor revision can add fields.
- ``verify`` is the trust rule (plan §2.0, S7 writer trust; contract OC0
  §1.4): the authority ``user:u.<sub>`` is not an OrgFS principal, so the
  reader passes ``writer_of``, which maps an authority to the OrgFS writer
  ``user:<username>`` bound to it from local facts only (never from the
  document).  An authority with no binding is ``authority-unbound``, decided
  before the author check; then every OrgFS author must equal the writer,
  and an ``owner`` scope cell must live in a space owned by the writer.
  Readers never adopt a document that fails it.
- ``verify`` also binds the scope to the space the document was read from,
  when the reader names the space role: ``org`` scope only in org directory
  spaces (of the named org, when it names one), ``owner`` scope only in an
  owner-home space.  A document planted in the wrong kind of space is
  refused instead of widening where a cell can come from.
- Documents are capped at 256 KiB; larger params belong in records, not in
  the cell.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from hyprial.daemon.impl.cells.model import (
    Cell,
    CellBinding,
    CellError,
    CellMember,
    CellPermissions,
    CellScope,
    thaw_json,
)
from hyprial.daemon.impl.cells.org import validate_org_cell

CELL_SCHEMA = "hyprial-cell/v1"
MAX_DOCUMENT_BYTES = 256 * 1024


def _invalid(message: str) -> CellError:
    return CellError("invalid-cell", message)


def _validate_kind_rules(cell: Cell) -> None:
    if cell.kind == "org":
        validate_org_cell(cell)
        return
    for key in cell.params:
        if key.startswith("org."):
            raise _invalid(f"agent cell cannot carry {key!r}")


def to_document(cell: Cell) -> dict[str, Any]:
    """The JSON object form of *cell*."""

    _validate_kind_rules(cell)
    return {
        "schema": CELL_SCHEMA,
        "cellId": cell.cell_id,
        "kind": cell.kind,
        "authority": cell.authority,
        "scope": cell.scope.to_json(),
        "binding": cell.binding.to_json(),
        "name": cell.name,
        "members": [member.to_json() for member in cell.members],
        "permissions": cell.permissions.to_json(),
        "params": thaw_json(cell.params),
        "revision": cell.revision,
        "retired": cell.retired,
        "updatedAtMs": cell.updated_at_ms,
    }


def encode(cell: Cell) -> str:
    """Canonical document text for *cell*; refuses oversized documents."""

    text = json.dumps(
        to_document(cell), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise CellError("too-large", f"cell document exceeds {MAX_DOCUMENT_BYTES} bytes")
    return text


def _field(document: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in document:
        raise _invalid(f"missing {key}")
    value = document[key]
    if isinstance(value, bool) and kind is int:
        raise _invalid(f"{key} has the wrong type")
    if not isinstance(value, kind):
        raise _invalid(f"{key} has the wrong type")
    return value


def from_document(document: object) -> Cell:
    """Build a :class:`Cell` from its JSON object form (strict)."""

    if not isinstance(document, dict):
        raise _invalid("cell document must be a JSON object")
    if document.get("schema") != CELL_SCHEMA:
        raise _invalid(f"unsupported cell schema {document.get('schema')!r}")
    members = _field(document, "members", list)
    kind = _field(document, "kind", str)
    try:
        parsed_members = tuple(CellMember.from_json(item) for item in members)
        if kind == "org":
            seen_refs: set[str] = set()
            for member in parsed_members:
                if member.ref in seen_refs:
                    raise _invalid(f"duplicate org member ref {member.ref!r}")
                seen_refs.add(member.ref)
        cell = Cell(
            cell_id=_field(document, "cellId", str),
            kind=kind,
            authority=_field(document, "authority", str),
            scope=CellScope.from_json(_field(document, "scope", dict)),
            binding=CellBinding.from_json(_field(document, "binding", dict)),
            name=_field(document, "name", str),
            members=parsed_members,
            permissions=CellPermissions.from_json(_field(document, "permissions", dict)),
            params=_field(document, "params", dict),
            revision=_field(document, "revision", int),
            retired=_field(document, "retired", bool),
            updated_at_ms=_field(document, "updatedAtMs", int),
        )
    except (TypeError, ValueError) as error:
        raise _invalid(str(error)) from error
    _validate_kind_rules(cell)
    return cell


def decode(text: str) -> Cell:
    """Parse and validate one cell document text."""

    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise CellError("too-large", "cell document is too large")
    try:
        document = json.loads(text)
    except ValueError as error:
        raise _invalid("cell document is not JSON") from error
    return from_document(document)


def verify(
    cell: Cell,
    *,
    authors: Sequence[str],
    space_owner: str,
    writer_of: Callable[[str], str | None],
    kind: str | None = None,
    cell_id: str | None = None,
    space_role: str | None = None,
    space_org: str | None = None,
) -> str | None:
    """None when a reader may trust *cell* as read from OrgFS, else the reason."""

    if kind is not None and kind != cell.kind:
        return "path-mismatch"
    if cell_id is not None and cell_id != cell.cell_id:
        return "path-mismatch"
    writer = writer_of(cell.authority)
    if writer is None:
        return "authority-unbound"
    if not authors or any(author != writer for author in authors):
        return "author-mismatch"
    if cell.scope.kind == "owner" and space_owner != writer:
        return "space-owner-mismatch"
    if space_role is not None:
        expected = "org" if cell.scope.kind == "org" else "owner-home"
        if space_role != expected:
            return "space-role-mismatch"
        if cell.scope.org is not None and space_org != cell.scope.org:
            return "space-org-mismatch"
    return None


__all__ = [
    "CELL_SCHEMA",
    "MAX_DOCUMENT_BYTES",
    "decode",
    "encode",
    "from_document",
    "to_document",
    "verify",
]
