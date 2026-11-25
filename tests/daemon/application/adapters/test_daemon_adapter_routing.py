"""Adapter inbound routing resolves from agent cell documents, not a pin store.

Since I6-4 the only adapter-binding authority is each agent's cell document:
an adapter member names the local adapter configuration, and the daemon's
``adapter.list`` projection resolves the binding through the cell
adapter-routing index.  A cross-cell conflict fails closed (``agent: null``);
an unavailable cell runtime is refused with ``DAEMON_NOT_READY`` so it never reads
as "unbound"; nothing falls back to the retired pins table.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.ports import LarkPortClient
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.daemon.impl.application import DaemonApplication
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from tests.support.identity.cell_runtime import bound_cell, cell_runtime_stub

OWNER = "alice"
NODE = "node-1"


@pytest.fixture(autouse=True)
def _own_applications(own_daemon_application):  # type: ignore[no-untyped-def]
    token = _APP_OWNER.set(own_daemon_application)
    try:
        yield
    finally:
        _APP_OWNER.reset(token)

_APP_OWNER: ContextVar[Any | None] = ContextVar("adapter_routing_app_owner", default=None)


def _supervisor(names: tuple[str, ...]) -> AdapterRuntime:
    launcher = SimpleNamespace(startup_timeout=20.0)
    channels = SimpleNamespace(gateways=[SimpleNamespace(name=name) for name in names])
    return AdapterRuntime(launcher, channels)  # type: ignore[arg-type]


class _EmptyInbox:
    def pending_messages(self, actor: str) -> tuple[Any, ...]:
        return ()


def _application(tmp_path: Path, adapters: tuple[str, ...]) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_app_injections(),
    )
    app._inbox = _EmptyInbox()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    runtime = _supervisor(adapters)
    app._adapters = runtime
    app._lark_client = LarkPortClient(runtime, CorrelatedDomainEvents())
    owner = _APP_OWNER.get()
    if owner is None:
        raise RuntimeError("adapter-routing application owner fixture is missing")
    return owner(app)


def _bind(app: DaemonApplication, agents: tuple[str, ...], adapter: str) -> None:
    cells = tuple(
        bound_cell(app.agents.require(name), (adapter,)) for name in agents
    )
    app._cell_runtime = cell_runtime_stub(cells)


def test_adapter_list_resolves_the_binding_from_the_cell_document(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1", "lark-2"))
    app.handle("agent.create", {"name": "worker"})
    _bind(app, ("worker",), "lark-1")

    payload = app.handle("adapter.list", {})

    assert payload["adapters"] == [
        {**row, "agent": app.agents.require("worker").uri if row["name"] == "lark-1" else None}
        for row in payload["adapters"]
    ]
    assert {
        row["name"]: row["agent"] for row in payload["adapters"]
    } == {"lark-1": app.agents.require("worker").uri, "lark-2": None}


def test_one_agent_may_bind_several_adapters(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1", "lark-2"))
    app.handle("agent.create", {"name": "worker"})
    agent = app.agents.require("worker")
    cell = bound_cell(agent, ("lark-1", "lark-2"))
    app._cell_runtime = cell_runtime_stub((cell,))

    agents = {row["name"]: row["agent"] for row in app.handle("adapter.list", {})["adapters"]}

    assert agents == {"lark-1": agent.uri, "lark-2": agent.uri}


def test_a_cross_cell_conflict_fails_closed(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "one"})
    app.handle("agent.create", {"name": "two"})
    _bind(app, ("one", "two"), "lark-1")

    agents = {row["name"]: row["agent"] for row in app.handle("adapter.list", {})["adapters"]}

    assert agents == {"lark-1": None}


def test_an_unavailable_cell_runtime_is_refused_not_reported_unbound(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    app._cell_runtime = None

    with pytest.raises(Exception) as failure:
        app.handle("adapter.list", {})

    assert getattr(failure.value, "code", None) == "DAEMON_NOT_READY"
    assert "cell runtime is unavailable" in str(failure.value)


def test_routing_updates_when_the_cell_document_loses_the_member(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))
    app.handle("agent.create", {"name": "worker"})
    _bind(app, ("worker",), "lark-1")
    before = {row["name"]: row["agent"] for row in app.handle("adapter.list", {})["adapters"]}
    app._cell_runtime = cell_runtime_stub(())
    after = {row["name"]: row["agent"] for row in app.handle("adapter.list", {})["adapters"]}

    assert before == {"lark-1": app.agents.require("worker").uri}
    assert after == {"lark-1": None}


def test_cli_adapter_list_calls_adapter_list(monkeypatch: Any) -> None:
    calls: list[tuple[str, object]] = []

    def fake(method: str, params: object = None, **_kwargs: object) -> object:
        calls.append((method, params))
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)

    result = CliRunner().invoke(app, ["adapter", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [("adapter.list", {})]


def test_retired_pin_requests_are_not_served(tmp_path: Path) -> None:
    app = _application(tmp_path, ("lark-1",))

    for method in ("adapter.pin", "adapter.pins", "adapter.unpin"):
        with pytest.raises(Exception) as failure:
            app.handle(method, {"name": "lark-1"})
        assert getattr(failure.value, "code", None) == "METHOD_NOT_FOUND"
