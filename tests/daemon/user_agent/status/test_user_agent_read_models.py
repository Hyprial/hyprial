"""Read models that list or count agents for a person: the user agent is hidden by default.

Agent listings and counts (``agent.list``, ``ps.agents``, the whole-node
``agent.home-census``, ``top.snapshot``, ``targets``) leave it out, so a user
who created no agents sees exactly what they saw before the user agent
existed.  It stays visible through the ``userAgent`` state of ``ps``, through
``agent.list`` with ``includeUserAgent: true`` (marked), and through explicit
lookups by name (``agent.get``, a census ``actor`` selector).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, run_user_agent_ensure
from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    application,
)


def _ensured(tmp_path: Path, *, plain: bool = True):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == "created"
    if plain:
        app.handle("agent.create", {"name": "plain"})
    return app, app.agents.get(USER_AGENT_NAME)


def test_the_default_list_of_a_user_with_no_agents_is_empty(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path, plain=False)

    assert app.handle("agent.list", {}) == {
        "ok": True, "agents": [], "agentsWithoutRoutine": [],
    }
    assert app.handle("agent.list", {"includeUserAgent": "yes"})["agents"] == []


def test_the_opt_in_list_shows_the_marked_row_and_never_offers_a_routine(
    tmp_path: Path,
) -> None:
    app, agent = _ensured(tmp_path)

    listed = app.handle("agent.list", {"includeUserAgent": True})
    rows = {row["actor"]: row for row in listed["agents"]}
    assert set(rows) == {USER_AGENT_NAME, "plain"}
    assert rows[USER_AGENT_NAME]["userAgent"] is True
    assert rows[USER_AGENT_NAME]["userActor"] == "user:op"
    assert agent.uri not in listed["agentsWithoutRoutine"]
    assert {row["actor"] for row in app.handle("agent.list", {})["agents"]} == {"plain"}


def test_the_inactive_listing_leaves_the_user_agent_out_even_on_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _agent = _ensured(tmp_path)
    later = app._agent_registry.now_ms() + 2 * 3_600_000
    monkeypatch.setattr(app._agent_registry, "now_ms", lambda: later)

    for params in ({"inactiveSince": "1h"}, {"inactiveSince": "1h", "includeUserAgent": True}):
        actors = {row["actor"] for row in app.handle("agent.list", params)["agents"]}
        assert actors == {"plain"}


def test_an_explicit_lookup_by_name_answers_marked(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    for name in (USER_AGENT_NAME, agent.uri):
        row = app.handle("agent.get", {"name": name})["agent"]
        assert row["uri"] == agent.uri and row["userAgent"] is True


def test_ps_lists_no_user_agent_row_but_reports_its_state(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    status = app.handle("ps", {})
    assert {row["uri"] for row in status["agents"]} == {app.agents.get("plain").uri}
    assert status["userAgent"]["state"] == "present"
    assert status["userAgent"]["address"] == agent.uri


def test_the_home_census_counts_no_user_agent_unless_selected(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    census = app.handle("agent.home-census", {})
    assert {row["actor"] for row in census["agents"]} == {"plain"}
    assert census["totals"]["agents"] == 1
    selected = app.handle("agent.home-census", {"actor": USER_AGENT_NAME})
    assert [row["uri"] for row in selected["agents"]] == [agent.uri]
    assert selected["totals"]["agents"] == 1


def test_the_activity_view_leaves_the_user_agent_out(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    snapshot = app.handle("top.snapshot", {})
    rows = snapshot["actors"]
    assert agent.uri not in {row.get("actor") for row in rows}
    assert any(row.get("actor") == app.agents.get("plain").uri for row in rows)
