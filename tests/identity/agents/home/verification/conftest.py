"""Isolated identity owners for home-operation contract tests."""

from pathlib import Path

import pytest

from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.registry import AgentRegistry


@pytest.fixture
def home_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "process-state"))
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3", owner="test-owner",
        machine="test-node", hyprial_home=tmp_path / "agent-home",
    )
    actors = []

    def create(*, configured: bool, preparer=None, event_sink=None):
        source = tmp_path / "personality"
        source.mkdir(exist_ok=True)
        config = {
            "sources": [{"path": str(source), "required": True}],
            "discovery": "explicit-only",
        } if configured else None
        agent = registry.create("worker", cwd=str(tmp_path / "work"), config=config)
        actor = AgentActor(
            registry, event_sink=event_sink or (lambda _event: None),
            home_runtime_preparers={"pi": preparer} if preparer else None,
        )
        actors.append(actor)
        return registry, actor, agent

    yield create
    try:
        for actor in actors:
            assert actor.drain(3.0).complete
    finally:
        registry.close()
