"""The identity owner binds config without replacing a caller's stale record."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
import threading

import pytest

from hyprial.identity import (
    AgentActor,
    AgentConfig,
    AgentRegistry,
    BindAgentConfigCommand,
    agent_config_digest,
)
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.registry import config_binding
from hyprial.daemon.impl.composition.domains import AgentSessionDomains
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents, DomainCommandError
from hyprial.daemon.impl.composition.views import AgentDirectoryFacade


@pytest.fixture
def registry(tmp_path):
    value = AgentRegistry(tmp_path / "agents.sqlite3", owner="test-owner", machine="test-node")
    value.create("visitor")
    try:
        yield value
    finally:
        value.close()


def bind(registry, agent, config, expected=None):
    return registry.bind_agent_config(
        agent.actor, expected_entity_token=agent.entity_token, config=config,
        expected_current_config_digest=agent_config_digest(expected),
    )


def test_config_bind_preserves_admin_fields_and_survives_reload(registry, tmp_path):
    prior = registry.require("visitor")
    prior = registry.save(replace(
        prior, cwd=str(tmp_path / "workspace"), model="admin-choice",
        capabilities={"test": True}, last_harness="codex", last_session_id="admin-session",
    ))
    config = AgentConfig(str(tmp_path / "config"))
    result = bind(registry, prior, config)
    assert result == replace(prior, config=config)
    other = AgentRegistry(registry.database, owner="test-owner", machine="test-node")
    try:
        assert other.require("visitor") == result
    finally:
        other.close()


def test_config_change_by_admin_is_visible_conflict_not_overwrite(registry, tmp_path):
    before = registry.require("visitor")
    admin_config = AgentConfig(str(tmp_path / "admin"))
    current = registry.save(replace(before, config=admin_config, model="admin-model"))
    with pytest.raises(AgentError) as error:
        bind(registry, before, AgentConfig(str(tmp_path / "migration")))
    assert error.value.code == "AGENT_CONFIG_CAS_MISMATCH"
    assert registry.require("visitor") == current


def test_old_incarnation_cannot_bind_or_destroy_replacement(registry, tmp_path):
    old = registry.require("visitor")
    registry.destroy("visitor")
    replacement = registry.create("visitor")
    with pytest.raises(AgentError) as error:
        bind(registry, old, AgentConfig(str(tmp_path / "migration")))
    assert error.value.code == "AGENT_INCARNATION_MISMATCH"
    assert registry.require("visitor") == replacement


def test_two_connections_cannot_both_bind_the_same_expected_config(registry, tmp_path):
    other = AgentRegistry(registry.database, owner="test-owner", machine="test-node")
    original = registry.require("visitor")
    barrier = threading.Barrier(2)

    def attempt(store, suffix):
        barrier.wait(3)
        try:
            return bind(store, original, AgentConfig(str(tmp_path / suffix)))
        except AgentError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(attempt, registry, "a")
            b = pool.submit(attempt, other, "b")
            results = [a.result(5), b.result(5)]
        assert results.count("AGENT_CONFIG_CAS_MISMATCH") == 1
        winner = next(value for value in results if not isinstance(value, str))
        assert registry.require("visitor") == winner
    finally:
        other.close()


def test_admin_writer_cannot_enter_between_cas_read_and_write(registry, tmp_path, monkeypatch):
    other = AgentRegistry(registry.database, owner="test-owner", machine="test-node")
    prior = registry.require("visitor")
    migration = AgentConfig(str(tmp_path / "migration"))
    admin = AgentConfig(str(tmp_path / "admin"))
    expected = agent_config_digest(None)
    read, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original_digest = config_binding.agent_config_digest

    def held_digest(config):
        read.set()
        assert release.wait(3)
        return original_digest(config)

    def admin_write():
        attempted.set()
        other.save(replace(prior, config=admin, model="admin-after"))

    monkeypatch.setattr(config_binding, "agent_config_digest", held_digest)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(registry.bind_agent_config, "visitor",
                                expected_entity_token=prior.entity_token, config=migration,
                                expected_current_config_digest=expected)
            try:
                assert read.wait(3)
                second = pool.submit(admin_write)
                assert attempted.wait(3)
                with pytest.raises(TimeoutError):
                    second.result(0.1)
            finally:
                release.set()
            assert first.result(3).config == migration
            second.result(3)
        assert registry.require("visitor").config == admin
        assert registry.require("visitor").model == "admin-after"
    finally:
        release.set()
        other.close()


def test_digest_uses_normalized_config_and_distinguishes_absence(tmp_path):
    config = AgentConfig(str(tmp_path / "config"))
    assert agent_config_digest(config) == agent_config_digest(config.to_json())
    assert agent_config_digest(None) != agent_config_digest(config)
    assert len(agent_config_digest(None)) == 64


def test_command_detaches_mutable_input(tmp_path):
    payload = AgentConfig(str(tmp_path / "config")).to_json()
    command = BindAgentConfigCommand("request", "visitor", "token", payload, agent_config_digest(None))
    payload["sources"][0]["path"] = str(tmp_path / "changed")
    assert command.config_payload()["sources"][0]["path"] == str(tmp_path / "config")


@pytest.fixture
def facade(registry):
    events = CorrelatedDomainEvents()
    actor = AgentActor(registry, event_sink=events.publish)
    domains = object.__new__(AgentSessionDomains)
    domains.agent, domains._registry, domains._agent_events = actor, registry, events
    try:
        yield AgentDirectoryFacade(domains), actor
    finally:
        assert actor.drain(3)
        events.close()


def test_facade_uses_identity_owner_and_publishes_updated_projection(facade, registry, tmp_path):
    port, actor = facade
    prior = registry.require("visitor")
    config = AgentConfig(str(tmp_path / "config"))
    result = bind(port, prior, config)
    assert result.config == config
    assert actor.read_agent("visitor").config_payload() == config.to_json()
    with pytest.raises(DomainCommandError) as error:
        bind(port, prior, AgentConfig(str(tmp_path / "other")))
    assert error.value.code == "AGENT_CONFIG_CAS_MISMATCH"
    assert registry.require("visitor") == result


def test_replay_requires_current_config_digest_even_for_identical_new_value(registry, tmp_path):
    prior = registry.require("visitor")
    config = AgentConfig(str(tmp_path / "config"))
    bound = bind(registry, prior, config)
    with pytest.raises(AgentError, match="config"):
        bind(registry, prior, config)
    assert bind(registry, bound, config, expected=config) == bound


@pytest.mark.parametrize("field,value", [
    ("expected_entity_token", ""),
    ("expected_entity_token", None),
    ("expected_current_config_digest", ""),
    ("expected_current_config_digest", None),
])
def test_empty_fences_are_never_wildcards(registry, tmp_path, field, value):
    prior = registry.require("visitor")
    args = dict(expected_entity_token=prior.entity_token,
                expected_current_config_digest=agent_config_digest(None),
                config=AgentConfig(str(tmp_path / "config")))
    args[field] = value
    with pytest.raises(ValueError):
        registry.bind_agent_config("visitor", **args)
    assert registry.require("visitor") == prior


def test_facade_propagates_incarnation_conflict_without_destroy(facade, registry, tmp_path):
    port, _actor = facade
    prior = registry.require("visitor")
    registry.destroy("visitor")
    current = registry.create("visitor")
    with pytest.raises(DomainCommandError) as error:
        bind(port, prior, AgentConfig(str(tmp_path / "config")))
    assert error.value.code == "AGENT_INCARNATION_MISMATCH"
    assert registry.require("visitor") == current
