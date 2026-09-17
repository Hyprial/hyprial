"""Authoring-time owner resolution (design-pac-owner-full-uri §3).

One port for CLI / template renderers / routine graph builders: turn an
authoring input into a storable principal URI, or refuse with the candidate
list.  Nothing here writes a graph, and a resolution report is not an
authorization credential.

Rules (§3.1):

1. A full principal URI input is validated and pinned verbatim -- never
   stripped to its owner segment, never filtered by local knowledge.
2. A bare short name collects candidates by EXACT name match from every
   configured source; person and agent candidates are listed side by side.
3. Zero candidates = unresolved; more than one = ambiguous; any source
   failure or unknown coverage = unavailable.  None of these may be folded
   into "unique".
4. Auto-completion (``selected_uri``) requires a scope the port can declare
   COMPLETE.  Today the only declarable-complete scope is the graph being
   edited: its node set is fully known to the writer.  The local registry,
   local squire profiles, and live ``targets`` are all partial views --
   "only one candidate visible" does not prove an offline or foreign
   same-named principal does not exist (P1 evidence: the targets port has
   no pagination/coverage contract and no global catalog exists), so a lone
   candidate from those sources is reported, never silently selected.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import time_ns
from typing import Any

from hyprial.uri import canonical_user_uri

from .errors import (
    PAC_OWNER_AMBIGUOUS,
    PAC_OWNER_UNRESOLVED,
    PAC_PRINCIPAL_SHAPE_INVALID,
    PAC_RESOLUTION_UNAVAILABLE,
    PacError,
)
from .principal import parse_principal
from .store import PacGraphStore

SCOPE_GRAPH = "graph"
SCOPE_LOCAL = "local"

SOURCE_GRAPH = "graph"
SOURCE_REGISTRY = "agents-registry"
SOURCE_USER_PROFILES = "user-profiles"


@dataclass(frozen=True, slots=True)
class Candidate:
    uri: str
    kind: str
    source: str

    def to_json(self) -> dict[str, str]:
        return {"uri": self.uri, "kind": self.kind, "source": self.source}


@dataclass(slots=True)
class ResolveReport:
    """The resolution product (§3.1): evidence, never an authorization."""

    input: str
    kind_hint: str | None
    candidates: list[Candidate] = field(default_factory=list)
    selected_uri: str | None = None
    source: str | None = None
    observed_at: int = 0
    scope: str | None = None
    completeness: str = "partial"
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "kindHint": self.kind_hint,
            "candidates": [item.to_json() for item in self.candidates],
            "selectedUri": self.selected_uri,
            "source": self.source,
            "observedAt": self.observed_at,
            "scope": self.scope,
            "completeness": self.completeness,
            "reason": self.reason,
        }


def _registry_candidates(state_dir: Path, short_name: str) -> list[Candidate]:
    """Local agent registry rows whose actor segment matches exactly.

    Read-only against the REAL registry schema (``agents(actor, owner,
    machine, uri, …)``, taking the stored canonical ``uri`` column verbatim --
    never a hand-built ``agents(actor)`` table and never a re-minted URI
    (design §7.2: a simplified table would silently disagree with the real
    serialization; a re-mint could drift from what the registry stored).
    Constructing ``AgentRegistry`` here is deliberately avoided: its
    constructor performs the legacy-JSON import, a WRITE side effect a
    read-only resolution must not have.
    """

    database = Path(state_dir) / "agents.sqlite3"
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT actor, uri FROM agents WHERE actor = ?", (short_name,)
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise PacError(
            PAC_RESOLUTION_UNAVAILABLE,
            f"cannot read the agents registry at {database}: {error}",
        ) from error
    return [
        Candidate(uri=str(row[1]), kind="agent", source=SOURCE_REGISTRY)
        for row in rows
    ]


def _user_profile_candidates(state_dir: Path, short_name: str) -> list[Candidate]:
    """Local squire profiles whose owner matches exactly (people candidates).

    Reads the real ``users.json`` shape (``{"version": 1, "users": [...]}``,
    squire.profile.UserProfileStore); a malformed file is
    resolution-unavailable, never an empty candidate set.
    """

    store_path = Path(state_dir) / "users.json"
    if not store_path.is_file():
        return []
    try:
        document = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PacError(
            PAC_RESOLUTION_UNAVAILABLE,
            f"cannot read the local user profiles at {store_path}: {error}",
        ) from error
    users = document.get("users") if isinstance(document, dict) else None
    if not isinstance(users, list):
        raise PacError(
            PAC_RESOLUTION_UNAVAILABLE,
            f"user profiles at {store_path} have an unsupported shape; "
            "refusing to guess candidates",
        )
    return [
        Candidate(
            uri=canonical_user_uri(str(profile["owner"])),
            kind="user",
            source=SOURCE_USER_PROFILES,
        )
        for profile in users
        if isinstance(profile, dict) and profile.get("owner") == short_name
    ]


def _graph_candidates(
    store: PacGraphStore, graph_id: str, short_name: str
) -> list[Candidate]:
    """Owners already on THIS graph whose short name matches exactly."""

    candidates: list[Candidate] = []
    for node in store.nodes(graph_id):
        try:
            principal = parse_principal(node.owner)
        except PacError:
            continue  # legacy short-name row: not a URI candidate
        if principal.short_name == short_name:
            candidates.append(
                Candidate(uri=principal.uri, kind=principal.kind, source=SOURCE_GRAPH)
            )
    return candidates


def resolve_owner(
    value: str,
    *,
    state_dir: Path,
    store: PacGraphStore | None = None,
    graph_id: str | None = None,
    kind_hint: str | None = None,
) -> ResolveReport:
    """Resolve one authoring input to a principal URI, or raise with candidates.

    Returns a report with ``selected_uri`` set only when the input was already
    a full URI or exactly one candidate exists inside a declarable-complete
    scope.  Raises :class:`PacError` (ambiguous / unresolved / unavailable /
    shape-invalid) otherwise; the error's ``data`` carries the same
    candidates the report would have.
    """

    observed_at = time_ns() // 1_000_000
    report = ResolveReport(input=value, kind_hint=kind_hint, observed_at=observed_at)

    # 1. Full URI: validate, pin verbatim.
    if ":" in value:
        principal = parse_principal(value)  # raises PAC_PRINCIPAL_SHAPE_INVALID
        report.selected_uri = principal.uri
        report.source = "input"
        report.scope = "input"
        report.completeness = "exact"
        report.reason = "full principal URI pinned verbatim"
        return report

    short_name = value
    if kind_hint not in (None, "human", "agent"):
        raise PacError(
            PAC_PRINCIPAL_SHAPE_INVALID,
            f"kind hint must be 'human' or 'agent', not {kind_hint!r}",
        )

    # 2. Collect candidates from every configured source; a source failure is
    #    resolution-unavailable, never an empty set.
    graph_candidates: list[Candidate] = []
    if store is not None and graph_id is not None:
        graph_candidates = _graph_candidates(store, graph_id, short_name)
    local_candidates = [
        *_registry_candidates(state_dir, short_name),
        *_user_profile_candidates(state_dir, short_name),
    ]
    if kind_hint == "human":
        graph_candidates = [c for c in graph_candidates if c.kind == "user"]
        local_candidates = [c for c in local_candidates if c.kind == "user"]
    elif kind_hint == "agent":
        graph_candidates = [c for c in graph_candidates if c.kind == "agent"]
        local_candidates = [c for c in local_candidates if c.kind == "agent"]

    # Deduplicate by URI, keeping every source that vouched for it.
    by_uri: dict[str, Candidate] = {}
    for candidate in [*graph_candidates, *local_candidates]:
        by_uri.setdefault(candidate.uri, candidate)
    report.candidates = list(by_uri.values())

    # 3. Graph scope is the only declarable-complete scope: its node set is
    #    fully known to this writer.
    graph_uris = {candidate.uri for candidate in graph_candidates}
    if len(graph_uris) == 1:
        selected = next(iter(graph_uris))
        report.selected_uri = selected
        report.source = SOURCE_GRAPH
        report.scope = SCOPE_GRAPH
        report.completeness = "graph"
        report.reason = "unique owner already on this graph"
        return report
    if len(graph_uris) > 1:
        report.scope = SCOPE_GRAPH
        report.reason = "short name matches several owners already on this graph"
        raise PacError(
            PAC_OWNER_AMBIGUOUS,
            f"owner {short_name!r} matches {len(graph_uris)} principals on this "
            "graph; pass the full URI",
            {"candidates": [item.to_json() for item in report.candidates]},
        )

    # 4. Partial views: report, never silently select (rule 4 in the module
    #    docstring -- no global catalog exists to declare completeness).
    if not report.candidates:
        report.reason = "no candidate in any configured source"
        raise PacError(
            PAC_OWNER_UNRESOLVED,
            f"owner {short_name!r} matches no known principal; pass a full "
            "user:<owner> or agent:<owner>:<machine>:<actor> URI",
            {"candidates": []},
        )
    report.reason = (
        "candidate sources are partial views (no complete directory exists); "
        "confirm by passing the full URI"
    )
    raise PacError(
        PAC_OWNER_AMBIGUOUS,
        f"owner {short_name!r} cannot be auto-resolved: {len(report.candidates)} "
        "candidate(s) found but no complete scope proves uniqueness; pass the "
        "full URI",
        {"candidates": [item.to_json() for item in report.candidates]},
    )
