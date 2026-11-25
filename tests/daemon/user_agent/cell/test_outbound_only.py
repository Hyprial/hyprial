"""I7a, owner item A-8: the user agent cell is outbound-only.

Decision notes (contract i7-0 v2 section 2.4):

- The adapter routing index answers "which agent receives what arrives on
  this adapter".  The user agent receives nothing, so its cell is skipped
  before targets and conflicts are computed: an adapter named only by the
  user agent cell has no receiving agent (``adapter.list`` answers
  ``agent: null``), and an adapter named by the user agent cell and one
  ordinary agent's cell is that agent's, with no conflict.  Two ordinary
  agents on one adapter stay a conflict.
- Adapter removal has never been refused for a bound adapter (the preview
  only names it, ``boundAgent``, and the binding stays in the cell).  The
  preview reads that name from the cell documents themselves, user agent
  included: the ordinary agent when one names the adapter, else the user
  agent, so the owner is still told before the user agent cell loses its
  chat.  The removal itself goes through, and a user agent cell that cannot
  be read names nobody rather than failing the preview.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.cells.adapter_routing import build_adapter_routing_index
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, ensure_user_agent
from tests.daemon.management.removal.test_protocol import (
    _isolated_environment as _isolated_environment,
    app as app,
    execute,
    preview,
)
from tests.support.identity.cell_push import install_cell, push_cell, user_agent_cell

ADAPTER = "app"


def _adapter_agent(app: Any) -> Any:
    [row] = [row for row in app.handle("adapter.list", {})["adapters"] if row["name"] == ADAPTER]
    return row["agent"]


def _bind_agent(app: Any, name: str) -> Any:
    agent = app.agents.get(name)
    if agent is None:
        app.handle("agent.create", {"name": name})
        agent = app.agents.get(name)
    install_cell(app, push_cell(agent, {agent.uri: ("harness", None), "im": ("adapter", ADAPTER)}))
    return agent


def _user_agent_binds(app: Any) -> Any:
    ensure_user_agent(agents=app.agents)
    return install_cell(app, user_agent_cell(app, {"im": ADAPTER}))


def _resolution(runtime: Any) -> Any:
    return build_adapter_routing_index(runtime.index()).resolve(ADAPTER)


def test_an_adapter_named_only_by_the_user_agent_cell_has_no_receiving_agent(app) -> None:
    runtime = _user_agent_binds(app)

    assert _resolution(runtime).status == "unmatched"
    assert _adapter_agent(app) is None


def test_the_user_agent_cell_and_one_agent_share_an_adapter_without_conflict(app) -> None:
    runtime = _user_agent_binds(app)
    worker = _bind_agent(app, "worker")

    resolution = _resolution(runtime)

    assert resolution.status == "matched"
    assert resolution.target is not None and resolution.target.cell_id == worker.cell_id
    assert _adapter_agent(app) == worker.uri


def test_two_ordinary_agents_on_one_adapter_are_still_a_conflict(app) -> None:
    _user_agent_binds(app)
    _bind_agent(app, "worker")
    runtime = install_cell(app, push_cell(*_second(app)))

    assert _resolution(runtime).status == "conflict"
    assert _adapter_agent(app) is None


def _second(app: Any) -> tuple[Any, dict[str, tuple[str, str | None]]]:
    app.handle("agent.create", {"name": "reviewer"})
    agent = app.agents.get("reviewer")
    return agent, {agent.uri: ("harness", None), "im": ("adapter", ADAPTER)}


def test_the_removal_preview_names_the_user_agent_when_only_its_cell_binds_the_adapter(
    app,
) -> None:
    _user_agent_binds(app)

    assert preview(app)["boundAgent"] == app.agents.get(USER_AGENT_NAME).uri


def test_the_removal_preview_names_the_ordinary_agent_before_the_user_agent(app) -> None:
    _user_agent_binds(app)
    worker = _bind_agent(app, "worker")

    assert preview(app)["boundAgent"] == worker.uri


def test_an_adapter_named_only_by_the_user_agent_cell_is_removed(app) -> None:
    _user_agent_binds(app)

    result = execute(app, preview(app))

    assert result["removed"] is True
    assert app._lark_client.read_adapter(ADAPTER) is None
    assert app.agents.get(USER_AGENT_NAME) is not None


def test_an_unreadable_user_agent_cell_names_nobody_in_the_preview(app, monkeypatch) -> None:
    runtime = _user_agent_binds(app)

    def unreadable(cell_id: str) -> Any:
        raise RuntimeError(cell_id)

    monkeypatch.setattr(runtime, "lookup", unreadable)

    assert preview(app)["boundAgent"] is None
