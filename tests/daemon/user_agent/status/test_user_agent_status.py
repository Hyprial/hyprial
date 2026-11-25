"""The user agent's state where operators look: ``ps`` (daemon status) and doctor.

A machine that cannot get its user agent (name held by an ordinary agent,
every create attempt failed) reports it through the ``ps``
``userAgent`` block, and ``hyprial doctor`` turns that block into one row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.user_agent import (
    USER_AGENT_NAME,
    run_user_agent_ensure,
    user_agent_status,
)
from hyprial.daemon.impl.user_agent import runtime
from hyprial.identity.impl.agents import AgentRegistry
from hyprial.shell.impl.cli.commands.doctor.checks.user_agent import user_agent_doctor_check
from tests.identity.agents.registry.entity.support import (
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)


def _run_loop(app: Any) -> None:
    runtime.schedule_user_agent_ensure(app, "test")
    thread = app._user_agent_ensure_state["thread"]
    if thread is not None:
        thread.join(30.0)


def test_before_any_attempt_the_state_is_pending(tmp_path: Path) -> None:
    app = application(tmp_path)

    assert user_agent_status(app) == {"state": "pending", "address": None, "attempts": 0}


def test_ps_reports_the_present_user_agent(tmp_path: Path) -> None:
    app = application(tmp_path)
    _run_loop(app)

    agent = app.agents.get(USER_AGENT_NAME)
    block = app.handle("ps", {})["userAgent"]
    assert block == {"state": "present", "address": agent.uri, "attempts": 1}


def test_a_name_held_by_an_ordinary_agent_is_reported(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.agents.create(USER_AGENT_NAME)
    _run_loop(app)

    block = user_agent_status(app)
    assert block["state"] == "name-taken"
    assert block["address"] is None
    assert USER_AGENT_NAME in block["reason"]


def test_no_name_frozen_state_is_left(tmp_path: Path) -> None:
    from hyprial.daemon.impl import user_agent

    assert not hasattr(user_agent, "NAME_FROZEN")
    assert "name-frozen" not in runtime._REASONS


def test_exhausted_attempts_are_reported_until_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    monkeypatch.setattr(runtime, "RETRY_BASE_S", 0.0)

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("actor unavailable")

    monkeypatch.setattr(app.agents, "create", fail)
    _run_loop(app)

    block = user_agent_status(app)
    assert block["state"] == "unavailable"
    assert block["attempts"] == runtime.MAX_ATTEMPTS
    assert block["address"] is None


@pytest.mark.parametrize("token", [None, "former-derived"])
def test_a_marked_record_is_present_whatever_its_id(tmp_path: Path, token: str | None) -> None:
    """A registry-minted id and the id an older build derived from the owner alike."""

    import hashlib

    from hyprial.identity.impl.agents import Agent

    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine="node-1")
    if token is None:
        registry.create(USER_AGENT_NAME, capabilities={"template": "user"})
    else:
        derived = hashlib.sha256(b"hyprial-user-agent-token-v1\0" + OWNER.encode()).digest()
        registry._create_record(Agent.minted(
            actor=USER_AGENT_NAME, owner=OWNER, machine="node-1",
            entity_token=derived[:16].hex(), capabilities={"template": "user"},
        ))

    class App:
        agents = registry
        _user_agent_ensure_state = {"thread": None, "again": False, "outcome": "present",
                                    "attempts": 1}

    block = user_agent_status(App())
    assert block == {"state": "present", "address": registry.get(USER_AGENT_NAME).uri,
                     "attempts": 1}
    registry.close()


def test_run_once_still_answers_the_outcome(tmp_path: Path) -> None:
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == "created"



def _ps(block: object) -> dict[str, object]:
    return {"daemon": {"running": True}, "userAgent": block}


def test_doctor_has_no_row_against_an_older_daemon() -> None:
    assert user_agent_doctor_check({"daemon": {"running": True}}) is None


@pytest.mark.parametrize("state", ["present", "pending", "no-owner"])
def test_doctor_is_quiet_while_the_user_agent_is_fine_or_on_its_way(state: str) -> None:
    check = user_agent_doctor_check(_ps({"state": state, "address": None, "attempts": 0}))
    assert check is not None and check["status"] == "ok"


@pytest.mark.parametrize(
    "state", ["name-taken", "unavailable"]
)
def test_doctor_warns_when_the_machine_has_no_usable_user_agent(state: str) -> None:
    check = user_agent_doctor_check(
        _ps({"state": state, "address": None, "attempts": 6, "reason": "why"})
    )
    assert check is not None
    assert check["name"] == "user-agent"
    assert check["status"] == "warn"
    assert "why" in check["detail"]
    assert check["action"]["command"]
