# Decision record (I6-3, plan §H.4):
# - Computes an immutable ``adapter -> agent cell`` reverse index purely from
#   an immutable ``CellIndex`` without touching SQLite, OrgFS, pins, or runtime
#   inbound dispatch.
# - Cross-cell adapter collisions fail closed: ``lookup(adapter)`` returns
#   ``None`` and ``resolve(adapter)`` reports ``status="conflict"`` with
#   deterministically sorted candidate targets.
# Decision record (I7a, contract i7-0 v2 section 2.4, owner item A-8):
# - The index answers the inbound question "which agent receives what arrives
#   on this adapter".  The user agent cell (param ``agent.template`` is
#   ``user``, the registrar seeds it from the template) is outbound-only and
#   is skipped before targets and conflicts are computed, so one adapter may
#   serve the user agent cell (outbound) and one ordinary agent (inbound and
#   outbound) at once.  ``agent_adapter_bindings`` stays per document and
#   still reports the user agent cell's adapters.
"""Pure read-only ``adapter -> agent cell`` reverse index over ``CellIndex``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.ports import CellIndex
from hyprial.daemon.impl.cells.structure import ADAPTER_ROLE, parse_structure
from hyprial.daemon.impl.user_agent.naming import USER_AGENT_TEMPLATE

ResolutionStatus = Literal["matched", "unmatched", "conflict"]


@dataclass(frozen=True, slots=True, order=True)
class AdapterRoutingTarget:
    """One adapter member declaration inside an active agent cell."""

    adapter: str
    cell_id: str
    agent_id: str
    member_name: str
    cell_name: str = ""
    routes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterRoutingConflict:
    """Multiple distinct active agent cells claiming the same adapter."""

    adapter: str
    targets: tuple[AdapterRoutingTarget, ...]


@dataclass(frozen=True, slots=True)
class AdapterRoutingResolution:
    """Deterministic resolution result for one adapter name."""

    adapter: str
    status: ResolutionStatus
    target: AdapterRoutingTarget | None = None
    candidates: tuple[AdapterRoutingTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterRoutingIndex:
    """Immutable read-only reverse index from adapter name to agent cells."""

    by_adapter: Mapping[str, tuple[AdapterRoutingTarget, ...]]
    conflicts: tuple[AdapterRoutingConflict, ...] = ()

    @property
    def adapters(self) -> tuple[str, ...]:
        """All indexed adapter names in sorted order."""

        return tuple(sorted(self.by_adapter))

    def targets_for(self, adapter: str) -> tuple[AdapterRoutingTarget, ...]:
        """All active agent-cell targets claiming *adapter*, deterministically ordered."""

        return self.by_adapter.get(adapter, ())

    def is_conflicted(self, adapter: str) -> bool:
        """True when more than one distinct active agent cell claims *adapter*."""

        return len({target.cell_id for target in self.targets_for(adapter)}) > 1

    def lookup(self, adapter: str) -> AdapterRoutingTarget | None:
        """Return the unambiguous target for *adapter*, or ``None`` if absent or conflicted."""

        resolution = self.resolve(adapter)
        return resolution.target if resolution.status == "matched" else None

    def resolve(self, adapter: str) -> AdapterRoutingResolution:
        """Resolve *adapter* into ``matched``, ``unmatched``, or ``conflict``."""

        targets = self.targets_for(adapter)
        if not targets:
            return AdapterRoutingResolution(adapter=adapter, status="unmatched")
        distinct_cells = {target.cell_id for target in targets}
        if len(distinct_cells) > 1:
            return AdapterRoutingResolution(
                adapter=adapter,
                status="conflict",
                target=None,
                candidates=targets,
            )
        if len(targets) == 1:
            return AdapterRoutingResolution(
                adapter=adapter,
                status="matched",
                target=targets[0],
                candidates=targets,
            )
        merged_routes = tuple(sorted({route for target in targets for route in target.routes}))
        primary = AdapterRoutingTarget(
            adapter=adapter,
            cell_id=targets[0].cell_id,
            agent_id=targets[0].agent_id,
            member_name=targets[0].member_name,
            cell_name=targets[0].cell_name,
            routes=merged_routes,
        )
        return AdapterRoutingResolution(
            adapter=adapter,
            status="matched",
            target=primary,
            candidates=targets,
        )


def agent_adapter_bindings(cell: Cell | None) -> tuple[str, ...]:
    """Adapter names one agent cell declares through its adapter members.

    Cell documents are the only adapter-binding authority (I6-4); a missing,
    retired or non-agent cell reports no binding instead of consulting any
    retired pin store.
    """

    if cell is None or cell.kind != "agent" or cell.retired:
        return ()
    structure = parse_structure(cell)
    return tuple(
        sorted(
            {member.adapter for member in structure.adapter_members if member.adapter}
        )
    )


def build_adapter_routing_index(cell_index: CellIndex) -> AdapterRoutingIndex:
    """Build a pure read-only ``AdapterRoutingIndex`` from an immutable ``CellIndex``."""

    buckets: dict[str, list[AdapterRoutingTarget]] = {}
    for cell in cell_index.of_kind("agent", include_retired=False):
        if cell.param("agent.template") == USER_AGENT_TEMPLATE:
            continue
        structure = parse_structure(cell)
        agent_id = cell.binding.value if cell.binding.type == "subject" else ""
        routes_by_member: dict[str, set[str]] = {}
        for connection in structure.connections:
            if connection.route is not None:
                routes_by_member.setdefault(connection.to, set()).add(connection.route)
        for member in structure.members:
            if member.role != ADAPTER_ROLE or not member.adapter:
                continue
            target = AdapterRoutingTarget(
                adapter=member.adapter,
                cell_id=cell.cell_id,
                agent_id=agent_id,
                member_name=member.name,
                cell_name=cell.name,
                routes=tuple(sorted(routes_by_member.get(member.name, ()))),
            )
            buckets.setdefault(member.adapter, []).append(target)

    frozen_map = MappingProxyType(
        {
            adapter: tuple(sorted(targets, key=lambda item: (item.cell_id, item.member_name)))
            for adapter, targets in sorted(buckets.items())
        }
    )
    conflicts = tuple(
        AdapterRoutingConflict(adapter=adapter, targets=targets)
        for adapter, targets in frozen_map.items()
        if len({target.cell_id for target in targets}) > 1
    )
    return AdapterRoutingIndex(by_adapter=frozen_map, conflicts=conflicts)


__all__ = [
    "AdapterRoutingConflict",
    "agent_adapter_bindings",
    "AdapterRoutingIndex",
    "AdapterRoutingResolution",
    "AdapterRoutingTarget",
    "build_adapter_routing_index",
]
