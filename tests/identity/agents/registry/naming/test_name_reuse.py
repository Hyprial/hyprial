"""A destroyed agent's name is free again (owner decision 2026-11-19).

A new agent with a previously used name is a new agent with a new id; the
registry keeps no tombstone of used names, and no create path, home claim or
rename refuses one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentExistsError, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import (
    CreateAgentCommand,
    CreateTransferHostedAgentCommand,
    DestroyAgentCommand,
)
from hyprial.kernel import LifecycleMutationRequest

from .conftest import NODE, OWNER


def _lifecycle(direction: str, ordinal: int, payload: object, token: str | None = None):
    attempt = f"op:{direction}:{ordinal}"
    return LifecycleMutationRequest(f"lifecycle:{attempt}", attempt, "op", token, payload)


@pytest.fixture
def homed(tmp_path: Path):
    instance = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3", owner=OWNER, machine=NODE,
        hyprial_home=tmp_path / "hyprial",
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.usefixtures("id_form")
def test_every_create_path_reuses_a_destroyed_name_as_a_new_agent(
    registry: AgentRegistry,
) -> None:
    seen = {registry.create("foo").agent_id}
    registry.destroy("foo")
    for create in (
        lambda: registry.create("foo"),
        lambda: registry.create_transfer_hosted("foo", pinned_owner="alice"),
        lambda: registry.apply_lifecycle(
            _lifecycle("forward", len(seen), CreateAgentCommand("c", "foo"))
        ),
    ):
        create()
        agent = registry.require("foo")
        assert agent.agent_id not in seen
        assert agent.uri.endswith(agent.agent_id)
        seen.add(agent.agent_id)
        registry.destroy("foo")
    assert registry.get("foo") is None


@pytest.mark.usefixtures("id_form")
def test_the_home_create_claim_reuses_a_destroyed_name(homed: AgentRegistry) -> None:
    first = homed.create("foo")
    homed.destroy("foo")
    for command in (
        CreateAgentCommand("c1", "foo"),
        CreateTransferHostedAgentCommand("c2", "foo", "alice"),
    ):
        homed.prepare_create_command(command)
    homed.apply_lifecycle(_lifecycle("forward", 0, CreateAgentCommand("c4", "foo")))
    assert homed.require("foo").agent_id != first.agent_id


@pytest.mark.usefixtures("id_form")
def test_destroy_paths_leave_the_name_free(homed: AgentRegistry) -> None:
    homed.apply_lifecycle(_lifecycle("forward", 1, CreateAgentCommand("c", "gone")))
    gone = homed.require("gone")
    homed.apply_lifecycle(_lifecycle("forward", 2, DestroyAgentCommand("d", "gone")))
    forward = homed.create("forward")
    homed.prepare_destroy_record("forward")
    for name, old in (("gone", gone), ("forward", forward)):
        assert homed.create(name).agent_id != old.agent_id


@pytest.mark.usefixtures("id_form")
def test_rename_onto_a_previously_used_name_succeeds(registry: AgentRegistry) -> None:
    old = registry.create("old")
    registry.rename("old", "shared")
    registry.destroy("old")
    other = registry.create("other")
    registry.rename("other", "temp")
    registry.rename("other", "other-display")
    renamed = registry.rename("other", "shared")
    assert renamed.display == "shared"
    assert renamed.agent_id == other.agent_id != old.agent_id
    assert registry.rename("other", "old").display == "old"
    assert registry.rename("other", "temp").display == "temp"


@pytest.mark.usefixtures("id_form")
def test_the_registry_keeps_no_name_ledger(registry: AgentRegistry) -> None:
    registry.create("foo")
    registry.rename("foo", "bar")
    registry.destroy("foo")
    for name in ("historical_agent_id", "historical_agent_ids", "has_frozen_names_for"):
        assert not hasattr(registry, name)


@pytest.mark.parametrize("minting", [False, True], ids=["switch-off", "switch-on"])
def test_a_taken_name_names_destroy_as_the_way_to_reuse_it(
    registry: AgentRegistry, monkeypatch: pytest.MonkeyPatch, minting: bool
) -> None:
    from hyprial.identity.impl.agents import addressing

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", minting)
    holder = registry.create("foo")
    with pytest.raises(AgentExistsError) as failure:
        registry.create("foo")
    message = str(failure.value)
    assert holder.uri in message
    assert "To reuse the name, destroy that agent first ('hyprial agent destroy foo'" in message
    assert "freezes" not in message


@pytest.mark.usefixtures("id_form")
def test_a_reused_name_gets_a_fresh_home_and_no_grants(homed: AgentRegistry) -> None:
    old = homed.create("foo")
    first = homed.ensure_home("foo")
    homed.grant_capability(
        "foo", grant_id="tool-1", capability="tool-surface",
        scope='["test-tool"]', granted_by="user:op", revision=1,
    )
    homed.destroy("foo")

    new = homed.create("foo")
    again = homed.ensure_home("foo")
    homed.create("bar")
    fresh = homed.ensure_home("bar")

    def layout(path: str) -> list[str]:
        return sorted(str(item.relative_to(path)) for item in Path(path).rglob("*"))

    assert new.agent_id != old.agent_id
    assert again.entity_token == new.entity_token != first.entity_token
    assert again.resource_token != first.resource_token
    assert layout(again.path) == layout(fresh.path)
    assert all(grant.grant_id != "tool-1" for grant in homed.capability_grants("foo"))
