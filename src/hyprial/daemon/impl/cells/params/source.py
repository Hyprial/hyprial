"""The trusted param source set (plan §2.0, Q9, Q10; S7a).

Decision notes (phase A, S7a; updated OC1b):

- A param source is a live ``agent`` cell whose authority is the local
  owner (the runtime's authority).  ``org`` cells are never param sources
  (they hold org rosters only; cross-owner delegation is deferred).
  ``department`` was retired and is superseded by sub-orgs (J24).
  Conversations are not cells and never feed params (Q9: conversation
  records are not trusted param sources).
- Writer trust needs no second rule: S3's ``codec.verify`` already admits a
  document only when its every OrgFS author is the binding writer, so the
  authority is verified.  This module narrows param sources to the local
  authority (Q10); org-admin delegation is deferred.
- Cells that carry params but fail the rule are reported as rejected
  (``kind`` or ``authority``), so the refusal is observable.  Retired cells
  and cells with no params are skipped silently.
- An agent cell applies only when the subject is that agent.  An agent's
  identity is its cell id, its binding ``a.`` id and its ``harness``-role
  member URIs; other member refs (adapters, users) are not names of the
  agent.  Every trusted agent cell, with or without params, contributes its
  identity as an alias group; groups are never unioned across cells: a ref
  claimed by two agent cells is ambiguous, is dropped from both groups and
  names no agent.
- Source order is deterministic by cell id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, NamedTuple

from hyprial.daemon.impl.cells.model import Cell

PARAM_SOURCE_KINDS: frozenset[str] = frozenset({"agent"})
HARNESS_ROLE = "harness"
RejectReason = Literal["kind", "authority"]


@dataclass(frozen=True, slots=True)
class ParamSource:
    """One trusted cell's params and the refs it applies to."""

    cell_id: str
    kind: str
    revision: int
    refs: frozenset[str]
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def __hash__(self) -> int:
        return hash((self.cell_id, self.revision))

    def applies_to(self, subject_refs: frozenset[str] | None) -> bool:
        """Whether this source covers a subject (None: every subject)."""

        if subject_refs is None:
            return True
        return self.cell_id in subject_refs


@dataclass(frozen=True, slots=True)
class RejectedSource:
    """A params-bearing cell outside the trusted set."""

    cell_id: str
    kind: str
    authority: str
    reason: RejectReason


class SourceSelection(NamedTuple):
    """The outcome of ``select_sources`` over one index."""

    sources: tuple[ParamSource, ...]
    rejected: tuple[RejectedSource, ...]
    aliases: Mapping[str, frozenset[str]]
    ambiguous: frozenset[str]


def agent_refs(cell: Cell) -> frozenset[str]:
    """The identity of an agent cell: cell id, ``a.`` id, harness URIs."""

    refs = {cell.cell_id, *(m.ref for m in cell.members if m.role == HARNESS_ROLE)}
    if cell.binding.type == "subject":
        refs.add(cell.binding.value)
    return frozenset(refs)


def refusal(cell: Cell, owner: str) -> RejectReason | None:
    """Why *cell* is not a param source for *owner*, or None when trusted."""

    if cell.kind not in PARAM_SOURCE_KINDS:
        return "kind"
    if cell.authority != owner:
        return "authority"
    return None


def _source(cell: Cell) -> ParamSource:
    return ParamSource(
        cell.cell_id, cell.kind, cell.revision, agent_refs(cell), cell.params
    )


def _alias_groups(
    groups: Sequence[frozenset[str]],
) -> tuple[Mapping[str, frozenset[str]], frozenset[str]]:
    claims: dict[str, int] = {}
    for group in groups:
        for ref in group:
            claims[ref] = claims.get(ref, 0) + 1
    ambiguous = frozenset(ref for ref, count in claims.items() if count > 1)
    aliases: dict[str, frozenset[str]] = {}
    for group in groups:
        names = group - ambiguous
        for ref in names:
            aliases[ref] = names
    return MappingProxyType(aliases), ambiguous


def select_sources(cells: Iterable[Cell], owner: str) -> SourceSelection:
    """Trusted sources, rejected cells and agent alias groups of *cells*."""

    sources: list[ParamSource] = []
    rejected: list[RejectedSource] = []
    identities: list[frozenset[str]] = []
    for cell in cells:
        if cell.retired:
            continue
        reason = refusal(cell, owner)
        if reason is None and cell.kind == "agent":
            identities.append(agent_refs(cell))
        if not cell.params:
            continue
        if reason is None:
            sources.append(_source(cell))
        else:
            rejected.append(
                RejectedSource(cell.cell_id, cell.kind, cell.authority, reason)
            )
    sources.sort(key=lambda source: source.cell_id)
    rejected.sort(key=lambda item: item.cell_id)
    aliases, ambiguous = _alias_groups(identities)
    return SourceSelection(tuple(sources), tuple(rejected), aliases, ambiguous)


__all__ = [
    "HARNESS_ROLE",
    "PARAM_SOURCE_KINDS",
    "ParamSource",
    "RejectReason",
    "RejectedSource",
    "SourceSelection",
    "agent_refs",
    "refusal",
    "select_sources",
]
