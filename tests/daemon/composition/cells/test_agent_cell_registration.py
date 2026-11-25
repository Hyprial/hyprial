"""Every local agent gets its ``agent`` cell once the cell runtime composes (S3).

The S3 ``register_agents`` had no production caller, so no agent cell
existed unless an owner hand-wrote its document.  These tests pin the
production wiring: composing ``cell_runtime(app)`` ensures the agent cell of
every agent the runtime's author owns, an agent created afterwards is
registered through the agent directory's ``on_mutation`` port, and none of
it can fail composition, agent creation or messaging, also with OrgFS down.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name
from hyprial.identity.impl.agents import Agent
from tests.daemon.acceptance.acceptance_support import (
    Outage,
    boot_like_services,
    send,
)
from tests.support.golden.app import (
    ALPHA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)
from tests.daemon.cells.runtime.cell_runtime_support import uid_for


def _agent(actor: str, owner: str = "alice") -> Agent:
    return Agent(
        uri=f"agent:{owner}:mac:{actor}",
        actor=actor,
        owner=owner,
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )


class _Agents:
    """The agent directory port the wiring reads: ``list`` and ``on_mutation``."""

    def __init__(self, *agents: Agent) -> None:
        self.items = list(agents)
        self.listeners: list[Any] = []
        self.failing = False
        self.lock = threading.Lock()

    def list(self) -> tuple[Agent, ...]:
        if self.failing:
            raise RuntimeError("agent projection unavailable")
        return tuple(self.items)

    def on_mutation(self, listener: Any) -> Any:
        with self.lock:
            self.listeners.append(listener)

        def unregister() -> None:
            with self.lock:
                if listener in self.listeners:
                    self.listeners.remove(listener)

        return unregister

    def create(self, agent: Agent) -> None:
        self.items.append(agent)
        for listener in tuple(self.listeners):
            listener()


def _app(tmp_path: Path, agents: _Agents) -> Any:
    fs = LocalOrgFs(author="user:alice")
    fs.create_space(org_space_name("group-acme"))
    logged: list[tuple[str, dict[str, Any]]] = []
    runtime = SimpleNamespace(facade=fs)
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="alice",
        user_id=uid_for("user:alice"),
        user_issuer="",
        agents=agents,
        _require_orgfs_runtime=lambda: runtime,
        _log=lambda _level, _component, event, **fields: logged.append((event, fields)),
        logged=logged,
    )


def _events(app: Any, name: str) -> list[dict[str, Any]]:
    return [fields for event, fields in app.logged if event == name]


def test_composing_the_runtime_registers_every_owned_agent_cell(tmp_path: Path) -> None:
    mine, other, hosted = _agent("worker"), _agent("reviewer"), _agent("guest", "bob")
    app = _app(tmp_path, _Agents(mine, other, hosted))
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        for agent in (mine, other):
            cell = runtime.lookup(agent.cell_id)
            assert cell is not None and cell.kind == "agent"
            assert [member.ref for member in cell.members] == [agent.uri]
        assert runtime.lookup(hosted.cell_id) is None
        assert [fields["agent"] for fields in _events(app, "cell.agent_skipped")] == [
            hosted.uri
        ]
    finally:
        close_cell_runtime(app)


def test_an_agent_created_after_composition_is_registered(tmp_path: Path) -> None:
    agents = _Agents(_agent("worker"))
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        later = _agent("later")
        agents.create(later)
        assert runtime.lookup(later.cell_id) is not None
        agents.create(_agent("guest", "bob"))
        agents.create(_agent("another"))
        assert len(_events(app, "cell.agent_skipped")) == 1, "skips are logged once"
    finally:
        close_cell_runtime(app)
    assert agents.listeners == [], "close detaches the agent listener"


def test_a_failing_agent_directory_never_fails_composition(tmp_path: Path) -> None:
    agents = _Agents(_agent("worker"))
    agents.failing = True
    app = _app(tmp_path, agents)
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        assert _events(app, "cell.agent_registration_failed")
        agents.failing = False
        agents.create(_agent("later"))
        assert all(runtime.lookup(agent.cell_id) is not None for agent in agents.items)
    finally:
        close_cell_runtime(app)


def test_the_daemon_registers_its_agents_and_new_ones_with_orgfs_down(
    golden_daemon, monkeypatch
) -> None:
    """The owner's user agent is on this node, so a send to ``user:op`` is queued."""

    daemon, recorder = boot_like_services(
        golden_daemon(bypass=False, links=False, user_agent=True), monkeypatch
    )
    app = daemon.app
    send(app, OWNER_USER, ALPHA, "compose")
    assert recorder.run_once().failed is False
    runtime = app._cell_runtime
    assert runtime is not None, "the recorder did not compose the cell runtime"
    for agent in app.agents.list():
        cell = runtime.lookup(agent.cell_id)
        assert cell is not None and cell.kind == "agent", agent.actor
    runtime.run_once()
    assert runtime.dirty() == frozenset()

    outage = Outage(daemon, app._require_orgfs_runtime().facade, "fs-raises")
    outage.down()
    created = app.handle("agent.create", {"name": "delta"})
    assert created["agent"]["actor"] == "delta"
    delta = app.agents.get("delta")
    assert delta is not None and runtime.lookup(delta.cell_id) is not None
    assert send(app, ALPHA, OWNER_USER, "while-down")["deliveries"][0]["accepted"]
    outage.up()
