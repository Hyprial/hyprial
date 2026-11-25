"""Cell documents, gateway failures and channel settings for the I3 push tests."""

from __future__ import annotations

import json
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.kernel import subject_cell_id
from tests.support.identity.cell_runtime import RegistryCellRuntime, owner_authority


def push_cell(
    agent: Any,
    members: dict[str, tuple[str, str | None]],
    *,
    connections: tuple[dict[str, Any], ...] = (),
    entry: str | None = None,
) -> Cell:
    """An agent cell whose members are ``name -> (role, adapter datum)``."""

    data: dict[str, Any] = {}
    for name, (_role, adapter) in members.items():
        data[name] = {} if adapter is None else {"adapter": adapter}
    params: dict[str, Any] = {"cell.members": data}
    if entry is not None:
        params["cell.entry"] = entry
    if connections:
        params["cell.connections"] = [dict(row) for row in connections]
    return Cell(
        cell_id=subject_cell_id("agent", agent.agent_id),
        kind="agent",
        authority=owner_authority(agent.owner),
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent.agent_id),
        name=agent.actor,
        members=tuple(CellMember(name, role) for name, (role, _a) in members.items()),
        params=params,
        revision=1,
        updated_at_ms=1,
    )


def user_agent_cell(
    app: Any,
    members: dict[str, str | None],
    *,
    connections: tuple[dict[str, Any], ...] = (),
) -> Cell:
    """The user agent cell of *app* with adapter members ``ref -> adapter datum``."""

    from hyprial.daemon.impl.user_agent import USER_AGENT_NAME

    agent = app.agents.get(USER_AGENT_NAME)
    assert agent is not None
    params: dict[str, Any] = {"agent.role": "user", "agent.template": "user"}
    if members:
        params["cell.members"] = {
            ref: {} if adapter is None else {"adapter": adapter}
            for ref, adapter in members.items()
        }
    if connections:
        params["cell.connections"] = [dict(row) for row in connections]
    return Cell(
        cell_id=subject_cell_id("agent", agent.agent_id),
        kind="agent",
        authority=owner_authority(agent.owner),
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent.agent_id),
        name=agent.actor,
        members=tuple(CellMember(ref, "adapter") for ref in members),
        params=params,
        revision=1,
        updated_at_ms=1,
    )


def bind_user_agent(app: Any, adapter: str, *, default_route: str | None = "dm") -> Cell:
    """Bind *adapter* into *app*'s user agent cell, with the gateway's default route."""

    if default_route is not None:
        edit_gateway(app, defaultRoute=default_route)
    document = user_agent_cell(app, {"im": adapter})
    install_cell(app, document)
    return document


class DocumentCellRuntime(RegistryCellRuntime):
    """The registry runtime with whole documents installed for some agents."""

    def __init__(self, agents: Any) -> None:
        super().__init__(agents)
        self.documents: dict[str, Cell] = {}

    def _cells(self) -> dict[str, Cell]:
        cells = super()._cells()
        cells.update(self.documents)
        return cells


def install_cell(app: Any, cell: Cell) -> DocumentCellRuntime:
    """Serve *cell* from *app*'s cell runtime; every other agent stays unbound."""

    runtime = getattr(app, "_cell_runtime", None)
    if not isinstance(runtime, DocumentCellRuntime):
        runtime = DocumentCellRuntime(app.agents)
        app._cell_runtime = runtime
    runtime.documents[cell.cell_id] = cell
    return runtime


def edit_gateway(app: Any, **fields: Any) -> None:
    """Change the golden gateway's entry in ``channels.json`` (``None`` removes a key)."""

    path = app.hyprial_home / "channels.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    [gateway] = config["gateways"]
    for key, value in fields.items():
        if value is None:
            gateway.pop(key, None)
        else:
            gateway[key] = value
    path.write_text(json.dumps(config), encoding="utf-8")


def fail_chats(gateway: Any, monkeypatch: Any, failures: dict[str, BaseException]) -> None:
    """Make the golden chat gateway raise for the chats in *failures*."""

    send_chat = gateway.send_chat

    def scripted(chat_id: str, text: str, *, idempotency_key: str) -> str:
        failure = failures.get(chat_id)
        if failure is not None:
            gateway.calls.append((chat_id, text, idempotency_key))
            raise failure
        return send_chat(chat_id, text, idempotency_key=idempotency_key)

    monkeypatch.setattr(gateway, "send_chat", scripted)
