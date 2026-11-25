"""The generic cell: the OrgFS-layer fixed connection structure (design §2, §5).

Decision notes (phase A, S3):

- A cell is data: kind, members, a permission surface and a first-class
  ``params`` map.  Code supplies the runtime and the gates; org rosters
  and flow parameters are cell data (design §5, contract OC0 §2).
- Kinds are a closed set: ``agent`` and ``org`` (contract OC0 §2.5).
  ``department``, ``workflow-definition``, ``graph``, ``routine`` and
  ``conversation`` do not exist as kinds: ``department`` was superseded by
  sub-orgs (owner ruling J24); graphs and routines belong to
  PAC, which keeps them in its own local databases, and the OrgFS side
  keeps no copy; a conversation is an interaction record, not an
  organization (owner rulings 2026-11-16 and 2026-11-17).  ``conversation``
  survives only as a storage category: the recorder keeps its segments
  under ``cells/conversation/<c.id>/`` in the owner-home space, where the
  ``c.`` id is still derived with ``keyed_cell_id('conversation', ...)``
  even though no cell of that kind exists.  A ``department``, ``graph``,
  ``routine`` or ``conversation`` document left by an earlier build no
  longer decodes; it is skipped in the cache and never read from OrgFS,
  and nothing deletes it.
- Kinds are indexed by scanning org directory spaces.
- The authority is ``user:u.<sub>``: ``user:`` plus an exact user-id segment
  (the Casdoor ``sub``; contract OC0 §1.3, owner ruling 2026-11-21).  The
  earlier ``user:<username>`` spelling no longer decodes, and nothing
  rewrites a document that still carries it.  The OrgFS principal that
  writes a cell (its writer, ``user:<username>``) is not part of the cell.
- The scope ``owner`` maps to the writer's owner-home OrgFS space (plan Q3);
  ``org`` maps to one named org directory space or, without a name, to every
  live org directory space.
- The id is bound to the content: a keyed or subject binding must re-derive
  the id, so a document cannot claim another binding's id.  ``minted`` ids are
  random and carry no derivation.
- ``revision`` grows by one per authored change; ``retire`` is a tombstone
  revision, never a delete, so peers converge on the retirement.
- ``params`` keys are free-form dotted names; values are JSON.  Readers and
  combine rules belong to the S7 resolver, not to this model.
- ``params`` is a read-only mapping, so the dataclass hash is replaced by one
  over (id, authority, revision, retired): equal cells always share those.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

from hyprial.kernel import (
    is_cell_id_segment,
    is_user_id_segment,
    keyed_cell_id,
    subject_cell_id,
)

CellKind = Literal["agent", "org"]
ScopeKind = Literal["org", "owner"]
BindingType = Literal["keyed", "subject", "minted"]

CELL_KINDS: frozenset[str] = frozenset({"agent", "org"})
EAGER_KINDS: frozenset[str] = CELL_KINDS
PERMISSION_ACTIONS: tuple[str, ...] = ("read", "write", "admin")
MEMBERS_TOKEN = "@members"
ANYONE_TOKEN = "*"

_ROLE = re.compile(r"[a-z][a-z0-9-]{0,63}")


def is_authority(value: object) -> bool:
    """Whether *value* is a cell authority ``user:u.<sub>``."""

    return (
        isinstance(value, str)
        and value.startswith("user:")
        and is_user_id_segment(value[len("user:"):])
    )


class CellError(Exception):
    """A refused cell operation with a stable ``code``."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


def _invalid(message: str) -> CellError:
    return CellError("invalid-cell", message)


@dataclass(frozen=True, slots=True)
class CellScope:
    """Where a cell's document lives: an org directory space or the owner home."""

    kind: ScopeKind
    org: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("org", "owner"):
            raise _invalid(f"unknown cell scope {self.kind!r}")
        if self.kind == "owner" and self.org is not None:
            raise _invalid("owner scope takes no org")
        if self.org is not None and (not isinstance(self.org, str) or not self.org):
            raise _invalid("org scope names a non-empty org")

    @classmethod
    def of_org(cls, org: str | None = None) -> CellScope:
        return cls("org", org)

    @classmethod
    def owner(cls) -> CellScope:
        return cls("owner")

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "org": self.org}

    @classmethod
    def from_json(cls, value: object) -> CellScope:
        if not isinstance(value, Mapping):
            raise _invalid("scope must be an object")
        return cls(value.get("kind"), value.get("org"))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CellBinding:
    """How a cell id was obtained, so readers can re-derive and check it."""

    type: BindingType
    value: str = ""

    def __post_init__(self) -> None:
        if self.type not in ("keyed", "subject", "minted"):
            raise _invalid(f"unknown binding type {self.type!r}")
        if not isinstance(self.value, str):
            raise _invalid("binding value must be a string")
        if self.type == "subject" and not self.value:
            raise _invalid("subject binding needs a subject id")
        if self.type == "minted" and self.value:
            raise _invalid("minted binding carries no value")

    @classmethod
    def keyed(cls, key: str) -> CellBinding:
        return cls("keyed", key)

    @classmethod
    def subject(cls, subject: str) -> CellBinding:
        return cls("subject", subject)

    @classmethod
    def minted(cls) -> CellBinding:
        return cls("minted")

    def derive(self, kind: str, authority: str) -> str | None:
        """The id this binding fixes for (kind, authority); None when minted."""

        if self.type == "keyed":
            return keyed_cell_id(kind, authority, self.value)
        if self.type == "subject":
            return subject_cell_id(kind, self.value)
        return None

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "value": self.value}

    @classmethod
    def from_json(cls, value: object) -> CellBinding:
        if not isinstance(value, Mapping):
            raise _invalid("binding must be an object")
        return cls(value.get("type"), value.get("value", ""))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, order=True)
class CellMember:
    """One member reference (agent URI, ``a.``/``c.`` id, adapter or user) and role."""

    ref: str
    role: str = "member"

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or not self.ref or self.ref.strip() != self.ref:
            raise _invalid("member ref must be a non-empty trimmed string")
        if not isinstance(self.role, str) or _ROLE.fullmatch(self.role) is None:
            raise _invalid(f"member role {self.role!r} is not a lowercase token")

    def to_json(self) -> dict[str, Any]:
        return {"ref": self.ref, "role": self.role}

    @classmethod
    def from_json(cls, value: object) -> CellMember:
        if not isinstance(value, Mapping):
            raise _invalid("member must be an object")
        return cls(value.get("ref"), value.get("role", "member"))  # type: ignore[arg-type]


def _principal(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _invalid("permission principals are non-empty trimmed strings")
    return value


@dataclass(frozen=True, slots=True)
class CellPermissions:
    """The permission surface: action -> principals (``*`` anyone, ``@members``)."""

    grants: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.grants, Mapping):
            raise _invalid("permissions must be a mapping")
        normalised: dict[str, frozenset[str]] = {}
        for action, principals in self.grants.items():
            if action not in PERMISSION_ACTIONS:
                raise _invalid(f"unknown permission action {action!r}")
            if isinstance(principals, str):
                raise _invalid("permission principals must be a collection")
            chosen = frozenset(_principal(item) for item in principals)
            if chosen:
                normalised[action] = chosen
        object.__setattr__(
            self, "grants", MappingProxyType(dict(sorted(normalised.items())))
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CellPermissions) and dict(self.grants) == dict(
            other.grants
        )

    def __hash__(self) -> int:
        return hash(tuple(self.grants.items()))

    @classmethod
    def of(cls, **grants: Iterable[str]) -> CellPermissions:
        return cls({action: frozenset(items) for action, items in grants.items()})

    def granted(self, action: str) -> frozenset[str]:
        return self.grants.get(action, frozenset())

    def to_json(self) -> dict[str, list[str]]:
        return {action: sorted(items) for action, items in self.grants.items()}

    @classmethod
    def from_json(cls, value: object) -> CellPermissions:
        if not isinstance(value, Mapping):
            raise _invalid("permissions must be an object")
        grants: dict[str, frozenset[str]] = {}
        for action, items in value.items():
            if not isinstance(items, list):
                raise _invalid("permission grants must be lists")
            grants[action] = frozenset(items)
        return cls(grants)


def _freeze_json(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _invalid(f"param keys must be non-empty strings at {path}")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path) for item in value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _invalid(f"param {path} is not finite JSON")
        return value
    raise _invalid(f"param {path} is not JSON")


def thaw_json(value: Any) -> Any:
    """A plain JSON-compatible copy of a frozen params value."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell document as the runtime holds it (immutable)."""

    cell_id: str
    kind: str
    authority: str
    scope: CellScope
    binding: CellBinding
    name: str = ""
    members: tuple[CellMember, ...] = ()
    permissions: CellPermissions = field(default_factory=CellPermissions)
    params: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1
    retired: bool = False
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if self.kind not in CELL_KINDS:
            raise _invalid(f"unknown cell kind {self.kind!r}")
        if not is_authority(self.authority):
            raise _invalid("authority must be a user:u.<sub> principal")
        if not isinstance(self.cell_id, str) or not is_cell_id_segment(self.cell_id):
            raise _invalid(f"{self.cell_id!r} is not a c. id")
        if not isinstance(self.scope, CellScope) or not isinstance(self.binding, CellBinding):
            raise _invalid("scope and binding must be typed values")
        derived = self.binding.derive(self.kind, self.authority)
        if derived is not None and derived != self.cell_id:
            raise CellError("id-mismatch", "cell id does not match its binding")
        if not isinstance(self.name, str):
            raise _invalid("name must be a string")
        if not isinstance(self.permissions, CellPermissions):
            raise _invalid("permissions must be CellPermissions")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise _invalid("revision must be a positive integer")
        if not isinstance(self.retired, bool):
            raise _invalid("retired must be a boolean")
        if isinstance(self.updated_at_ms, bool) or not isinstance(self.updated_at_ms, int):
            raise _invalid("updated_at_ms must be an integer")
        members: dict[str, CellMember] = {}
        for member in self.members:
            if not isinstance(member, CellMember):
                raise _invalid("members must be CellMember values")
            members.setdefault(member.ref, member)
        object.__setattr__(self, "members", tuple(sorted(members.values())))
        if not isinstance(self.params, Mapping):
            raise _invalid("params must be a mapping")
        object.__setattr__(self, "params", _freeze_json(self.params, "params"))
        json.dumps(thaw_json(self.params), allow_nan=False)

    def __hash__(self) -> int:
        return hash((self.cell_id, self.authority, self.revision, self.retired))

    def has_member(self, ref: str) -> bool:
        return any(member.ref == ref for member in self.members)

    def param(self, key: str, default: Any = None) -> Any:
        """One top-level param as plain JSON, or *default* when absent."""

        if key not in self.params:
            return default
        return thaw_json(self.params[key])

    def revised(self, *, now_ms: int, **changes: Any) -> Cell:
        """The next revision with *changes* applied (identity fields stay)."""

        for fixed in ("cell_id", "kind", "authority", "scope", "binding", "revision"):
            if fixed in changes:
                raise _invalid(f"{fixed} cannot change on a revision")
        return replace(
            self, revision=self.revision + 1, updated_at_ms=int(now_ms), **changes
        )


def default_scope(kind: str) -> CellScope:
    """Where a *kind* cell lives when its spec names no scope."""

    if kind not in CELL_KINDS:
        raise _invalid(f"unknown cell kind {kind!r}")
    if kind == "org":
        raise _invalid("org cells require an explicit org scope")
    return CellScope.of_org()


__all__ = [
    "ANYONE_TOKEN",
    "CELL_KINDS",
    "Cell",
    "CellBinding",
    "CellError",
    "CellKind",
    "CellMember",
    "CellPermissions",
    "CellScope",
    "EAGER_KINDS",
    "MEMBERS_TOKEN",
    "PERMISSION_ACTIONS",
    "default_scope",
    "is_authority",
    "thaw_json",
]
