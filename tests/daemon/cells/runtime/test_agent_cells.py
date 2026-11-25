"""Agents are registered as cells with one harness member."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.model import CellBinding, CellError, CellMember, CellScope
from hyprial.daemon.impl.cells.runtime.agents import agent_cell_spec, register_agents
from hyprial.identity.impl.agents import Agent

from tests.daemon.cells.runtime.cell_runtime_support import org_fs, runtime


def _agent(owner: str = "alice", actor: str = "worker") -> Agent:
    return Agent(
        uri=f"agent:{owner}:mac:{actor}",
        actor=actor,
        owner=owner,
        machine="mac",
        entity_token=uuid.uuid4().hex,
    )


def test_the_agent_cell_spec_binds_the_agent_id_and_one_harness_member() -> None:
    agent = _agent()
    spec = agent_cell_spec(agent)
    assert spec.kind == "agent"
    assert spec.binding == CellBinding.subject(agent.agent_id)
    assert spec.scope == CellScope.of_org()
    assert tuple(spec.members) == (CellMember(agent.uri, "harness"),)
    assert spec.name == "worker"


def test_register_agents_ensures_cells_whose_id_is_agent_cell_id(tmp_path: Path) -> None:
    fs, space = org_fs()
    cells, _ = runtime(tmp_path, fs)
    mine, theirs = _agent(), _agent(owner="bob")
    registered = register_agents(cells, (mine, theirs))
    assert [cell.cell_id for cell in registered] == [mine.cell_id]
    assert cells.lookup(mine.cell_id).has_member(mine.uri)
    assert cells.lookup(theirs.cell_id) is None
    assert register_agents(cells, (mine,)) == registered
    cells.run_once()
    content, _ = fs.read_text(space, f"cells/agent/{mine.cell_id}.json")
    assert mine.agent_id in content


def test_a_derivation_drift_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    agent = _agent()
    monkeypatch.setattr(Agent, "cell_id", property(lambda _self: "c." + "a" * 26))
    with pytest.raises(CellError) as raised:
        register_agents(cells, (agent,))
    assert raised.value.code == "id-mismatch"


def test_skipped_agents_are_logged(tmp_path: Path) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    events: list[tuple[str, dict[str, object]]] = []
    register_agents(cells, (_agent(owner="bob"),),
                    logger=lambda _level, _domain, event, **fields: events.append((event, fields)))
    assert events == [("cell.agent_skipped", {"agent": "agent:bob:mac:worker",
                                               "reason": "owner-mismatch"})]


def test_register_agents_preserves_document_edited_members_for_daemon_agent(
    tmp_path: Path,
) -> None:
    fs, _ = org_fs()
    cells, _ = runtime(tmp_path, fs)
    agent = _agent()
    [initial] = register_agents(cells, (agent,))
    edited = cells.update(
        initial.cell_id,
        members=(CellMember("w1", "harness"), CellMember("w2", "harness")),
        expect_revision=initial.revision,
    )
    [after] = register_agents(cells, (agent,))
    assert after.revision == edited.revision
    assert [m.ref for m in after.members] == ["w1", "w2"]
