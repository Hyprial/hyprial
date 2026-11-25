"""S8: an attached agent's cell has one ``custody=external`` harness member.

The custody and the attachment state are facts on the local agent record
(``capabilities``), so the cell can always be re-derived from the record:
``register_agents`` reconciles an external agent's cell to the record
(member present while attached, absent once detached, ``agent.custody``
param kept) without touching params an owner wrote, and a daemon-managed
agent keeps the phase A ensure-only path.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.model import CellError, CellMember
from hyprial.daemon.impl.cells.runtime.agents import (
    CUSTODY_PARAM,
    agent_cell_spec,
    agent_custody,
    is_attached,
    registration_key,
    register_agents,
)
from hyprial.identity.impl.agents import Agent
from tests.daemon.cells.runtime.cell_runtime_support import org_fs, runtime


def _agent(*, attached: bool | None = True, owner: str = "alice") -> Agent:
    capabilities = (
        {} if attached is None else {"custody": "external", "attached": attached}
    )
    return Agent(
        uri=f"agent:{owner}:mac:ext",
        actor="ext",
        owner=owner,
        machine="mac",
        entity_token=uuid.uuid4().hex,
        capabilities=capabilities,
    )


def test_custody_and_attachment_are_read_from_the_record() -> None:
    managed, attached = _agent(attached=None), _agent()
    assert agent_custody(managed) == "daemon" and agent_custody(attached) == "external"
    assert is_attached(attached) is True
    assert is_attached(_agent(attached=False)) is False
    assert is_attached(managed) is False
    assert registration_key(managed) == managed.cell_id
    detached = _with(attached, attached=False)
    assert detached.cell_id == attached.cell_id
    assert registration_key(attached) != registration_key(detached)


def test_the_external_spec_has_one_external_harness_member() -> None:
    agent = _agent()
    spec = agent_cell_spec(agent)
    assert tuple(spec.members) == (CellMember(agent.uri, "harness"),)
    assert dict(spec.params) == {CUSTODY_PARAM: "external"}
    detached = agent_cell_spec(_agent(attached=False))
    assert tuple(detached.members) == ()
    assert dict(detached.params) == {CUSTODY_PARAM: "external"}


def test_a_managed_agent_spec_is_unchanged() -> None:
    agent = _agent(attached=None)
    spec = agent_cell_spec(agent)
    assert tuple(spec.members) == (CellMember(agent.uri, "harness"),)
    assert dict(spec.params) == {}


def test_register_reconciles_membership_and_keeps_owner_params(tmp_path: Path) -> None:
    fs, space = org_fs()
    cells, _ = runtime(tmp_path, fs)
    agent = _agent()
    [cell] = register_agents(cells, (agent,))
    assert cell.cell_id == agent.cell_id and cell.has_member(agent.uri)
    assert cell.param(CUSTODY_PARAM) == "external"
    owned = cells.update(cell.cell_id, params={**cell.params, "threads.policy": {"k": 1}})

    detached = _with(agent, attached=False)
    [after] = register_agents(cells, (detached,))
    assert after.members == () and after.revision == owned.revision + 1
    assert after.param("threads.policy") == {"k": 1}
    assert after.param(CUSTODY_PARAM) == "external"
    assert register_agents(cells, (detached,)) == (after,)

    [again] = register_agents(cells, (agent,))
    assert again.has_member(agent.uri) and again.revision == after.revision + 1
    cells.run_once()
    content, _ = fs.read_text(space, f"cells/agent/{agent.cell_id}.json")
    assert '"agent.custody"' in content and agent.uri in content


def test_a_retired_external_cell_is_refused_not_revived(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    agent = _agent()
    [cell] = register_agents(cells, (agent,))
    cells.retire(cell.cell_id)
    with pytest.raises(CellError) as raised:
        register_agents(cells, (agent,))
    assert raised.value.code == "retired"


def test_an_external_agent_of_another_owner_is_skipped(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    assert register_agents(cells, (_agent(owner="bob"),)) == ()


def _with(agent: Agent, *, attached: bool) -> Agent:
    return replace(agent, capabilities={"custody": "external", "attached": attached})
