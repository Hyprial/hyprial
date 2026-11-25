"""Minimal cell-runtime stubs for tests that need adapter bindings."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.ports import CellIndex
from hyprial.kernel import subject_cell_id


def owner_authority(owner: str) -> str:
    """The cell authority ``user:u.<sub>`` a test pairs with *owner*."""

    digest = hashlib.sha256(f"user:{owner}".encode("utf-8")).hexdigest()[:32]
    return f"user:u.ou_{digest}"


def bound_cell(agent: Any, adapters: tuple[str, ...]) -> Cell:
    from hyprial.daemon.impl.user_agent import is_user_agent

    if is_user_agent(agent) and not adapters:
        return Cell(
            cell_id=subject_cell_id("agent", agent.agent_id),
            kind="agent",
            authority=owner_authority(agent.owner),
            scope=CellScope.of_org(),
            binding=CellBinding.subject(agent.agent_id),
            name=agent.actor,
            members=(),
            params={"agent.role": "user", "agent.template": "user"},
            revision=1,
            updated_at_ms=1,
        )
    members = [CellMember(agent.uri, "harness")]
    data: dict[str, Any] = {agent.uri: {}}
    for adapter in adapters:
        members.append(CellMember(adapter, "adapter"))
        data[adapter] = {"adapter": adapter}
    return Cell(
        cell_id=subject_cell_id("agent", agent.agent_id),
        kind="agent",
        authority=owner_authority(agent.owner),
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent.agent_id),
        name=agent.actor,
        members=tuple(members),
        params={"cell.entry": agent.uri, "cell.members": data},
        revision=1,
        updated_at_ms=1,
    )


def cell_runtime_stub(cells: tuple[Cell, ...] = ()) -> Any:
    by_id = {cell.cell_id: cell for cell in cells}
    return SimpleNamespace(lookup=by_id.get, index=lambda: CellIndex(1, by_id))


class RegistryCellRuntime:
    """A stub runtime answering an unbound agent cell for every registered agent.

    Guards treat a missing runtime or cell as "bindings unknown", so tests that
    expect an agent to be reclaimable give their app this runtime; adapters are
    bound per agent with :func:`bind_adapters`.
    """

    def __init__(self, agents: Any) -> None:
        self._agents = agents
        self.bound: dict[str, tuple[str, ...]] = {}

    def _cells(self) -> dict[str, Cell]:
        cells = (
            bound_cell(agent, self.bound.get(agent.cell_id, ()))
            for agent in self._agents.list()
        )
        return {cell.cell_id: cell for cell in cells}

    def lookup(self, cell_id: str) -> Cell | None:
        return self._cells().get(cell_id)

    def index(self) -> CellIndex:
        return CellIndex(1, self._cells())


def registry_cell_runtime(app: Any) -> RegistryCellRuntime:
    """Install and return a :class:`RegistryCellRuntime` over ``app.agents``."""

    runtime = RegistryCellRuntime(app.agents)
    app._cell_runtime = runtime
    return runtime


def bind_adapters(app: Any, agent: Any, adapters: tuple[str, ...]) -> None:
    """Make *app*'s cell for *agent* bind *adapters*.

    A :class:`RegistryCellRuntime` keeps answering for every other agent;
    otherwise *app* gets a stub runtime holding only this agent's cell.
    """

    runtime = getattr(app, "_cell_runtime", None)
    if isinstance(runtime, RegistryCellRuntime):
        runtime.bound[agent.cell_id] = adapters
        return
    app._cell_runtime = cell_runtime_stub((bound_cell(agent, adapters),))

