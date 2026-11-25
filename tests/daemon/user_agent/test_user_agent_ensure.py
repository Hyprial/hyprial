"""The user agent ensure: one zero-harness agent per machine, each an independent agent.

Two "machines" of one owner are two registries with different machine ids.
Nothing replicated is read or written, so no shared fake is needed.  Each
machine's registry mints its user agent's id like any other agent's.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hyprial.daemon.impl.user_agent import (
    CREATED,
    DEFERRED,
    NAME_TAKEN,
    NO_OWNER,
    PRESENT,
    USER_AGENT_NAME,
    ensure_user_agent,
    is_user_agent,
    is_user_agent_address,
    local_user_agent,
    local_user_agent_address,
    user_agent_capabilities,
)
from hyprial.identity import agent_id_for_entity_token
from hyprial.identity.impl.agents import Agent, AgentRegistry
from hyprial.kernel import is_agent_id_segment

OWNER = "owner@example"


def _former_derived_token(owner: str) -> str:
    """The token an older build derived from the owner for every machine's user agent."""

    return hashlib.sha256(b"hyprial-user-agent-token-v1\0" + owner.encode()).digest()[:16].hex()


def _registry(tmp_path: Path, machine: str, owner: str = OWNER) -> AgentRegistry:
    return AgentRegistry(tmp_path / machine / "agents.sqlite3", owner=owner, machine=machine)


def test_the_ensure_creates_a_zero_harness_agent_with_a_registry_minted_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    routes: list[str] = []

    assert ensure_user_agent(agents=registry, declare_route=routes.append) == CREATED

    agent = local_user_agent(registry.list(), OWNER)
    assert agent is not None and agent.actor == USER_AGENT_NAME
    assert is_user_agent(agent)
    assert dict(agent.capabilities) == {"template": "user"}
    assert (agent.preferred_harness, agent.last_harness, agent.provider, agent.model, agent.cwd) == (
        None, None, None, None, None,
    )
    assert dict(agent.harness_args) == {}
    assert is_agent_id_segment(agent.agent_id)
    assert agent.agent_id != agent_id_for_entity_token(_former_derived_token(OWNER))
    assert agent.uri == f"agent:{OWNER}:node-a:{agent.agent_id}"
    assert routes == [agent.uri]
    assert not is_user_agent_address(agent.uri, OWNER)


def test_the_ensure_is_idempotent(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    assert ensure_user_agent(agents=registry) == CREATED
    first = registry.get(USER_AGENT_NAME)

    assert ensure_user_agent(agents=registry) == PRESENT
    assert ensure_user_agent(agents=registry) == PRESENT

    assert [a.actor for a in registry.list()] == [USER_AGENT_NAME]
    assert registry.get(USER_AGENT_NAME) == first


def test_two_homes_of_one_owner_get_different_user_agent_ids(tmp_path: Path) -> None:
    node_a, node_b = _registry(tmp_path, "node-a"), _registry(tmp_path, "node-b")

    assert ensure_user_agent(agents=node_a) == CREATED
    assert ensure_user_agent(agents=node_b) == CREATED

    a, b = node_a.get(USER_AGENT_NAME), node_b.get(USER_AGENT_NAME)
    assert a is not None and b is not None
    assert (a.actor, dict(a.capabilities)) == (b.actor, dict(b.capabilities))
    assert is_user_agent(a) and is_user_agent(b)
    assert a.agent_id != b.agent_id
    assert a.entity_token != b.entity_token
    assert (a.machine, b.machine) == ("node-a", "node-b")
    assert a.uri.replace(":node-a:", ":") != b.uri.replace(":node-b:", ":")
    assert local_user_agent_address(node_a) == a.uri
    assert local_user_agent_address(node_b) == b.uri


def test_different_owners_get_different_ids(tmp_path: Path) -> None:
    mine, theirs = _registry(tmp_path, "node-a"), _registry(tmp_path, "node-x", owner="other")
    assert ensure_user_agent(agents=mine) == CREATED
    assert ensure_user_agent(agents=theirs) == CREATED

    assert mine.get(USER_AGENT_NAME).agent_id != theirs.get(USER_AGENT_NAME).agent_id


class _NoOwner:
    owner = ""

    def list(self) -> tuple[()]:
        raise AssertionError("nothing is read without an owner")


def test_without_an_owner_nothing_is_created() -> None:
    assert ensure_user_agent(agents=_NoOwner()) == NO_OWNER
    assert local_user_agent_address(_NoOwner()) is None


def test_the_lookup_is_none_before_the_ensure(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    registry.create("plain")

    assert local_user_agent_address(registry) is None


def test_an_unmarked_agent_holding_the_name_is_never_adopted(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    registry.create(USER_AGENT_NAME)

    assert ensure_user_agent(agents=registry) == NAME_TAKEN
    assert local_user_agent(registry.list(), OWNER) is None
    assert local_user_agent_address(registry) is None


class _FailingCreate:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry
        self.owner = registry.owner

    def list(self):  # type: ignore[no-untyped-def]
        return self._registry.list()

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return self._registry.get(name)

    def create(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("actor unavailable")


def test_a_failed_create_is_deferred_and_the_next_run_creates(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    events: list[str] = []

    assert ensure_user_agent(
        agents=_FailingCreate(registry), log=lambda level, event, **_: events.append(event)
    ) == DEFERRED
    assert events == ["user_agent.create_failed"]
    assert registry.list() == ()

    assert ensure_user_agent(agents=registry) == CREATED


class _Refusing(_FailingCreate):
    def __init__(self, registry: AgentRegistry, code: str) -> None:
        super().__init__(registry)
        self.code = code

    def create(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        error = RuntimeError("refused")
        error.code = self.code  # type: ignore[attr-defined]
        raise error


def test_a_display_name_refusal_is_terminal(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    assert ensure_user_agent(agents=_Refusing(registry, "AGENT_EXISTS")) == NAME_TAKEN


def test_a_real_display_name_collision_is_terminal(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    registry.create("plain")
    registry.rename("plain", USER_AGENT_NAME)

    assert ensure_user_agent(agents=registry) == NAME_TAKEN
    assert local_user_agent(registry.list(), OWNER) is None


def test_a_marked_record_with_any_id_is_present_without_a_warning(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "node-a")
    registry.create(USER_AGENT_NAME, capabilities={"template": "user"})
    events: list[str] = []

    assert ensure_user_agent(
        agents=registry, log=lambda level, event, **_: events.append(event)
    ) == PRESENT
    assert events == []
    assert len(registry.list()) == 1


def test_an_existing_derived_id_row_is_present_and_left_as_it_is(tmp_path: Path) -> None:
    """A dev install created before this change keeps its row: no rewrite, no second record."""

    registry = _registry(tmp_path, "node-a")
    registry._create_record(Agent.minted(
        actor=USER_AGENT_NAME, owner=OWNER, machine="node-a",
        entity_token=_former_derived_token(OWNER), capabilities=user_agent_capabilities(),
    ))
    before = registry.get(USER_AGENT_NAME)
    events: list[str] = []

    assert ensure_user_agent(
        agents=registry, log=lambda level, event, **_: events.append(event)
    ) == PRESENT

    assert events == []
    assert registry.list() == (before,)
    assert before.agent_id == agent_id_for_entity_token(_former_derived_token(OWNER))
    assert local_user_agent_address(registry) == before.uri


def test_a_row_lost_with_its_registry_is_ensured_again(tmp_path: Path) -> None:
    """A row lost together with its registry database is minted again."""

    registry = _registry(tmp_path, "node-a")
    assert ensure_user_agent(agents=registry) == CREATED
    first = registry.get(USER_AGENT_NAME).agent_id
    registry.close()
    for path in (tmp_path / "node-a").glob("agents.sqlite3*"):
        path.unlink()

    again = _registry(tmp_path, "node-a")
    try:
        assert ensure_user_agent(agents=again) == CREATED
        assert again.get(USER_AGENT_NAME).agent_id != first
    finally:
        again.close()


def test_a_forward_destroyed_user_agent_is_simply_ensured_again(
    tmp_path: Path,
) -> None:
    """No name tombstone (2026-11-19): the next ensure mints a new incarnation.

    Every public destroy refuses the user agent, so only an internal registry
    destroy reaches this.
    """

    registry = _registry(tmp_path, "node-a")
    try:
        assert ensure_user_agent(agents=registry) == CREATED
        first = registry.get(USER_AGENT_NAME).agent_id
        assert registry.destroy(USER_AGENT_NAME)

        assert ensure_user_agent(agents=registry) == CREATED
        again = local_user_agent(registry.list(), OWNER)
        assert again is not None and again.agent_id != first
    finally:
        registry.close()
