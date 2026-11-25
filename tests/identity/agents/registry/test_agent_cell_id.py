"""An agent is a cell: ``Agent.cell_id`` is derived from its ``a.`` id."""

from __future__ import annotations

import uuid

from hyprial.identity.impl.agents import Agent
from hyprial.kernel import is_cell_id_segment, subject_cell_id


def _agent(token: str) -> Agent:
    return Agent(
        uri="agent:alice:mac:worker",
        actor="worker",
        owner="alice",
        machine="mac",
        entity_token=token,
    )


def test_agent_cell_id_is_the_agent_subject_cell_of_its_agent_id() -> None:
    agent = _agent(uuid.uuid4().hex)
    assert is_cell_id_segment(agent.cell_id)
    assert agent.cell_id == subject_cell_id("agent", agent.agent_id)


def test_agent_cell_id_is_stable_per_incarnation_and_changes_with_it() -> None:
    token = uuid.uuid4().hex
    assert _agent(token).cell_id == _agent(token).cell_id
    assert _agent(uuid.uuid4().hex).cell_id != _agent(token).cell_id


def test_agent_cell_id_stays_out_of_the_published_json_shape() -> None:
    agent = _agent(uuid.uuid4().hex)
    assert "cellId" not in agent.to_json()
    assert agent.cell_id not in agent.to_json().values()
