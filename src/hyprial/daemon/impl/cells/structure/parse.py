"""One cell's structure data: members, entry, connections and fail-open.

Decision notes (cell model slice I1, ``cell-model-rulings-and-plan-2026-11-19``
design §1 ruling 1, §5.1, §5.2):

- Cell internals are OrgFS document data, not code: this module is the pure
  reader that turns one ``Cell`` into an immutable ``CellStructure``.  It
  knows no specific configuration vocabulary: member names, roles, labels
  and routes are opaque tokens carried from the document.
- The four ``cell.*`` keys live in ``params`` and only mean something for
  the cell that carries them, so this module reads ``cell.params`` directly
  and never through the multi-source resolver.  The keys are declared in
  the process registry to claim their names and shapes; ``unique`` is the
  registry's resting combine mode, not a merge this module performs.  A
  shape that the closed type set cannot name (``cell.connections`` is a
  list) is declared ``any``.
- Compatibility with today's documents (design §5.2, no migration): without
  ``cell.entry`` the only harness member is the entry; a cell without
  harness members has no entry and is legal; a harness member without a
  ``cell.members`` entry keeps ``kind=None``, meaning the local record
  decides; without ``cell.connections`` there are no hops; a member ref in
  agent-address form stays an opaque member name.
- Bad data never raises and never drops the whole structure.  Problems
  carry stable codes (``STRUCTURE_PROBLEM_CODES``).  A connection with any
  problem is excluded while the others stay in document order; a bad
  member-data field reads as absent; an invalid ``cell.entry`` or
  ``cell.failOpen`` value reads as absent, after which the entry
  compatibility rule applies.
- Per-member launch policy (I6-5a, ruling §13.1.5): ``cell.members.<m>``
  may carry ``allow`` and ``deny``, each a list of the harness's own rule
  strings kept verbatim.  A missing key means nothing is declared; there is
  no in-code default.  A wrong type, a duplicate or an overlong rule reads
  the field as absent with ``members-policy-type``; a rule in both lists is
  ambiguous under deny-before-allow, so both read as absent with
  ``members-policy-overlap``.  Any problem rejects the revision, so the last
  effective policy stays in force.
- One connection covers every label it lists, so a repeated ``(from,
  label)`` pair would be ambiguous: the first connection wins and the later
  one is excluded as ``connection-duplicate``.  The reserved label ``user``
  is the one deliberate exception, required by design §5.3: several
  ``user``-labelled connections from one member are how a member binds
  several chats, so they are all kept unless a later one repeats an earlier
  push's member and route exactly (a literal re-row, which would push one
  chat twice).
- A cell with several harness members and no valid entry resolves to no
  entry and no problem code: that is a caller-facing question, not a
  data-shape question.
- The public records are safe to build directly: every constructor
  normalises the collections it takes (``on`` to a frozenset copy,
  sequences to tuples) and refuses wrong field types with ``TypeError``,
  so a caller cannot smuggle mutable state into an immutable structure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.params import ParamSpec, declare_param

HARNESS_ROLE = "harness"
ADAPTER_ROLE = "adapter"
USER_LABEL = "user"

CELL_MEMBERS_PARAM = declare_param(ParamSpec("cell.members", "object"))
CELL_ENTRY_PARAM = declare_param(ParamSpec("cell.entry", "string"))
CELL_CONNECTIONS_PARAM = declare_param(ParamSpec("cell.connections", "any"))
CELL_FAILOPEN_PARAM = declare_param(ParamSpec("cell.failOpen", "string"))

MEMBERS_SHAPE = "members-shape"
MEMBERS_ITEM_SHAPE = "members-item-shape"
MEMBERS_FIELD_TYPE = "members-field-type"
MEMBERS_UNKNOWN_MEMBER = "members-unknown-member"
ENTRY_TYPE = "entry-type"
ENTRY_UNKNOWN_MEMBER = "entry-unknown-member"
ENTRY_NOT_HARNESS = "entry-not-harness"
CONNECTIONS_SHAPE = "connections-shape"
CONNECTION_SHAPE = "connection-shape"
CONNECTION_LABEL_TYPE = "connection-label-type"
CONNECTION_UNKNOWN_MEMBER = "connection-unknown-member"
CONNECTION_ROUTE_TYPE = "connection-route-type"
CONNECTION_ROUTE_MISPLACED = "connection-route-misplaced"
CONNECTION_DUPLICATE = "connection-duplicate"
FAIL_OPEN_TYPE = "fail-open-type"
FAIL_OPEN_UNKNOWN_MEMBER = "fail-open-unknown-member"
MEMBERS_POLICY_TYPE = "members-policy-type"
MEMBERS_POLICY_OVERLAP = "members-policy-overlap"

STRUCTURE_PROBLEM_CODES: tuple[str, ...] = (
    MEMBERS_SHAPE,
    MEMBERS_ITEM_SHAPE,
    MEMBERS_FIELD_TYPE,
    MEMBERS_UNKNOWN_MEMBER,
    ENTRY_TYPE,
    ENTRY_UNKNOWN_MEMBER,
    ENTRY_NOT_HARNESS,
    CONNECTIONS_SHAPE,
    CONNECTION_SHAPE,
    CONNECTION_LABEL_TYPE,
    CONNECTION_UNKNOWN_MEMBER,
    CONNECTION_ROUTE_TYPE,
    CONNECTION_ROUTE_MISPLACED,
    CONNECTION_DUPLICATE,
    FAIL_OPEN_TYPE,
    FAIL_OPEN_UNKNOWN_MEMBER,
    MEMBERS_POLICY_TYPE,
    MEMBERS_POLICY_OVERLAP,
)

_MEMBER_FIELDS = ("kind", "model", "adapter")
_POLICY_FIELDS = ("allow", "deny")
MAX_POLICY_RULES = 256
MAX_POLICY_RULE_LENGTH = 200


def _token(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _optional_token(value: object) -> bool:
    return value is None or _token(value)


def _rule_list(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) <= MAX_POLICY_RULES
        and len(set(value)) == len(value)
        and all(_rule(item) for item in value)
    )


def _rule(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_POLICY_RULE_LENGTH
        and value == value.strip()
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    )


@dataclass(frozen=True, slots=True)
class StructureMember:
    """One member of the cell: its opaque name, role and declared data.

    ``kind``/``model`` come from ``cell.members.<name>`` for harness members
    (``kind=None``: the local record decides), ``adapter`` names the local
    adapter configuration for adapter members.  ``allow``/``deny`` are the
    member's declared launch policy rules (``None``: not declared).
    """

    name: str
    role: str
    kind: str | None = None
    model: str | None = None
    adapter: str | None = None
    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not _token(self.name):
            raise TypeError("member name must be a non-empty string")
        if not _token(self.role):
            raise TypeError("member role must be a non-empty string")
        if not (
            _optional_token(self.kind)
            and _optional_token(self.model)
            and _optional_token(self.adapter)
        ):
            raise TypeError("kind, model and adapter must be None or non-empty strings")
        for field_name in _POLICY_FIELDS:
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, str) or not isinstance(value, Iterable):
                raise TypeError(f"{field_name} must be None or a sequence of rules")
            rules = tuple(value)
            if not _rule_list(rules):
                raise TypeError(f"{field_name} must hold unique bounded rule strings")
            object.__setattr__(self, field_name, rules)


@dataclass(frozen=True, slots=True)
class StructureConnection:
    """One hop: when *from_member* emits a label in *on*, work goes to *to*.

    *route* is the route on the adapter member *to*; it is None for hops
    without one.  Construction copies *on* into a frozenset, so a mutable
    collection passed in cannot change the hop afterwards.
    """

    from_member: str
    on: frozenset[str]
    to: str
    route: str | None = None

    def __post_init__(self) -> None:
        if not _token(self.from_member) or not _token(self.to):
            raise TypeError("connection members must be non-empty strings")
        if not _optional_token(self.route):
            raise TypeError("route must be None or a non-empty string")
        if isinstance(self.on, str) or not isinstance(self.on, Iterable):
            raise TypeError("on must be a collection of labels, not one label")
        labels = frozenset(self.on)
        if not labels or not all(_token(label) for label in labels):
            raise TypeError("on must carry non-empty string labels")
        object.__setattr__(self, "on", labels)


@dataclass(frozen=True, slots=True)
class StructureProblem:
    """One piece of bad structure data with a stable *code* and a detail."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not _token(self.code):
            raise TypeError("problem code must be a non-empty string")
        if not isinstance(self.detail, str):
            raise TypeError("problem detail must be a string")


@dataclass(frozen=True, slots=True)
class CellStructure:
    """One cell's members, entry, connections and fail-open, with problems.

    Construction copies the sequences it takes into tuples, so mutable
    lists passed in cannot change the structure afterwards.
    """

    cell_id: str
    members: tuple[StructureMember, ...]
    entry: str | None
    connections: tuple[StructureConnection, ...]
    fail_open: str | None
    problems: tuple[StructureProblem, ...] = ()

    def __post_init__(self) -> None:
        if not _token(self.cell_id):
            raise TypeError("cell_id must be a non-empty string")
        if not _optional_token(self.entry):
            raise TypeError("entry must be None or a non-empty string")
        if not _optional_token(self.fail_open):
            raise TypeError("fail_open must be None or a non-empty string")
        members = tuple(self.members)
        connections = tuple(self.connections)
        problems = tuple(self.problems)
        if not all(isinstance(member, StructureMember) for member in members):
            raise TypeError("members must be StructureMember values")
        if not all(isinstance(item, StructureConnection) for item in connections):
            raise TypeError("connections must be StructureConnection values")
        if not all(isinstance(item, StructureProblem) for item in problems):
            raise TypeError("problems must be StructureProblem values")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "connections", connections)
        object.__setattr__(self, "problems", problems)

    def member(self, name: str) -> StructureMember | None:
        """The member called *name*, or None when the cell has none such."""

        for member in self.members:
            if member.name == name:
                return member
        return None

    @property
    def harness_members(self) -> tuple[StructureMember, ...]:
        """The members whose role the interpreter drives as processes."""

        return tuple(m for m in self.members if m.role == HARNESS_ROLE)

    @property
    def adapter_members(self) -> tuple[StructureMember, ...]:
        """The members whose role the interpreter drives as endpoints."""

        return tuple(m for m in self.members if m.role == ADAPTER_ROLE)


def parse_structure(cell: Cell) -> CellStructure:
    """The immutable structure of *cell* plus every problem found in it."""

    problems: list[StructureProblem] = []
    data = _member_data(cell, problems)
    members = tuple(
        StructureMember(
            name=member.ref,
            role=member.role,
            kind=data.get(member.ref, {}).get("kind"),
            model=data.get(member.ref, {}).get("model"),
            adapter=data.get(member.ref, {}).get("adapter"),
            allow=data.get(member.ref, {}).get("allow"),
            deny=data.get(member.ref, {}).get("deny"),
        )
        for member in cell.members
    )
    entry = _entry_of(cell, members, problems)
    connections = _connections_of(cell, members, problems)
    fail_open = _fail_open_of(cell, members, problems)
    return CellStructure(
        cell_id=cell.cell_id,
        members=members,
        entry=entry,
        connections=connections,
        fail_open=fail_open,
        problems=tuple(problems),
    )


def _describe(value: Any) -> str:
    return repr(value)


def _member_data(
    cell: Cell, problems: list[StructureProblem]
) -> dict[str, dict[str, Any]]:
    if "cell.members" not in cell.params:
        return {}
    raw = cell.param("cell.members")
    if not isinstance(raw, Mapping):
        problems.append(StructureProblem(MEMBERS_SHAPE, _describe(raw)))
        return {}
    known = {member.ref for member in cell.members}
    table: dict[str, dict[str, Any]] = {}
    for name, item in raw.items():
        if name not in known:
            problems.append(StructureProblem(MEMBERS_UNKNOWN_MEMBER, name))
            continue
        if not isinstance(item, Mapping):
            problems.append(StructureProblem(MEMBERS_ITEM_SHAPE, f"{name}: {item!r}"))
            continue
        entry: dict[str, Any] = {}
        for field in _MEMBER_FIELDS:
            value = item.get(field)
            if value is None or isinstance(value, str):
                entry[field] = value
            else:
                problems.append(
                    StructureProblem(MEMBERS_FIELD_TYPE, f"{name}.{field}: {value!r}")
                )
                entry[field] = None
        entry.update(_member_policy(name, item, problems))
        table[name] = entry
    return table


def _member_policy(
    name: str, item: Mapping[str, Any], problems: list[StructureProblem]
) -> dict[str, tuple[str, ...] | None]:
    policy: dict[str, tuple[str, ...] | None] = {}
    for field in _POLICY_FIELDS:
        value = item.get(field)
        if value is None:
            policy[field] = None
            continue
        rules = tuple(value) if isinstance(value, (list, tuple)) else None
        if rules is None or not _rule_list(rules):
            problems.append(
                StructureProblem(MEMBERS_POLICY_TYPE, f"{name}.{field}: {value!r}")
            )
            policy[field] = None
            continue
        policy[field] = rules
    allow, deny = policy["allow"], policy["deny"]
    if allow is not None and deny is not None and set(allow) & set(deny):
        overlap = ",".join(sorted(set(allow) & set(deny)))
        problems.append(StructureProblem(MEMBERS_POLICY_OVERLAP, f"{name}: {overlap}"))
        policy = {"allow": None, "deny": None}
    return policy


def _entry_of(
    cell: Cell, members: tuple[StructureMember, ...], problems: list[StructureProblem]
) -> str | None:
    if "cell.entry" in cell.params:
        raw = cell.param("cell.entry")
        if not isinstance(raw, str):
            problems.append(StructureProblem(ENTRY_TYPE, _describe(raw)))
        else:
            member = next((m for m in members if m.name == raw), None)
            if member is None:
                problems.append(StructureProblem(ENTRY_UNKNOWN_MEMBER, raw))
            elif member.role != HARNESS_ROLE:
                problems.append(StructureProblem(ENTRY_NOT_HARNESS, raw))
            else:
                return raw
    harness = [m for m in members if m.role == HARNESS_ROLE]
    if len(harness) == 1:
        return harness[0].name
    return None


def _connections_of(
    cell: Cell, members: tuple[StructureMember, ...], problems: list[StructureProblem]
) -> tuple[StructureConnection, ...]:
    if "cell.connections" not in cell.params:
        return ()
    raw = cell.param("cell.connections")
    if not isinstance(raw, (list, tuple)):
        problems.append(StructureProblem(CONNECTIONS_SHAPE, _describe(raw)))
        return ()
    known = {member.name: member for member in members}
    claimed: set[tuple[str, str]] = set()
    pushes: set[tuple[str, str, str | None]] = set()
    kept: list[StructureConnection] = []
    for index, item in enumerate(raw):
        connection = _one_connection(index, item, known, claimed, pushes, problems)
        if connection is not None:
            kept.append(connection)
    return tuple(kept)


def _one_connection(
    index: int,
    item: Any,
    known: dict[str, StructureMember],
    claimed: set[tuple[str, str]],
    pushes: set[tuple[str, str, str | None]],
    problems: list[StructureProblem],
) -> StructureConnection | None:
    where = f"#{index}"
    if not isinstance(item, Mapping):
        problems.append(StructureProblem(CONNECTION_SHAPE, f"{where}: {item!r}"))
        return None
    source = item.get("from")
    target = item.get("to")
    if not isinstance(source, str) or not isinstance(target, str):
        problems.append(StructureProblem(CONNECTION_SHAPE, f"{where}: {item!r}"))
        return None
    labels = _labels_of(item.get("on"))
    if labels is None:
        problems.append(StructureProblem(CONNECTION_LABEL_TYPE, f"{where}: {item.get('on')!r}"))
        return None
    missing = sorted({name for name in (source, target) if name not in known})
    if missing:
        problems.append(
            StructureProblem(CONNECTION_UNKNOWN_MEMBER, f"{where}: {','.join(missing)}")
        )
        return None
    route = item.get("route")
    if route is not None and (not isinstance(route, str) or not route):
        problems.append(StructureProblem(CONNECTION_ROUTE_TYPE, f"{where}: {route!r}"))
        return None
    if route is not None and known[target].role != ADAPTER_ROLE:
        problems.append(StructureProblem(CONNECTION_ROUTE_MISPLACED, where))
        return None
    routing = [label for label in labels if label != USER_LABEL]
    overlap = sorted(label for label in routing if (source, label) in claimed)
    if overlap:
        problems.append(
            StructureProblem(CONNECTION_DUPLICATE, f"{where}: {','.join(overlap)}")
        )
        return None
    if USER_LABEL in labels and (source, target, route) in pushes:
        problems.append(StructureProblem(CONNECTION_DUPLICATE, f"{where}: {USER_LABEL}"))
        return None
    claimed.update((source, label) for label in routing)
    if USER_LABEL in labels:
        pushes.add((source, target, route))
    return StructureConnection(
        from_member=source, on=frozenset(labels), to=target, route=route
    )


def _labels_of(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,) if value else None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    labels: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            return None
        labels.append(item)
    return tuple(labels)


def _fail_open_of(
    cell: Cell, members: tuple[StructureMember, ...], problems: list[StructureProblem]
) -> str | None:
    if "cell.failOpen" not in cell.params:
        return None
    raw = cell.param("cell.failOpen")
    if not isinstance(raw, str) or not raw:
        problems.append(StructureProblem(FAIL_OPEN_TYPE, _describe(raw)))
        return None
    if not any(member.name == raw for member in members):
        problems.append(StructureProblem(FAIL_OPEN_UNKNOWN_MEMBER, raw))
        return None
    return raw


__all__ = [
    "ADAPTER_ROLE",
    "CELL_CONNECTIONS_PARAM",
    "CELL_ENTRY_PARAM",
    "CELL_FAILOPEN_PARAM",
    "CELL_MEMBERS_PARAM",
    "CellStructure",
    "CONNECTION_DUPLICATE",
    "CONNECTION_LABEL_TYPE",
    "CONNECTION_ROUTE_MISPLACED",
    "CONNECTION_ROUTE_TYPE",
    "CONNECTION_SHAPE",
    "CONNECTION_UNKNOWN_MEMBER",
    "CONNECTIONS_SHAPE",
    "ENTRY_NOT_HARNESS",
    "ENTRY_TYPE",
    "ENTRY_UNKNOWN_MEMBER",
    "FAIL_OPEN_TYPE",
    "FAIL_OPEN_UNKNOWN_MEMBER",
    "HARNESS_ROLE",
    "MEMBERS_FIELD_TYPE",
    "MEMBERS_ITEM_SHAPE",
    "MEMBERS_SHAPE",
    "MEMBERS_POLICY_OVERLAP",
    "MEMBERS_POLICY_TYPE",
    "MEMBERS_UNKNOWN_MEMBER",
    "STRUCTURE_PROBLEM_CODES",
    "StructureConnection",
    "StructureMember",
    "StructureProblem",
    "USER_LABEL",
    "parse_structure",
]
