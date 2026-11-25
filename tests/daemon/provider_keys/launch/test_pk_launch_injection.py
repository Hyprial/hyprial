"""G2a: owner-key secrets reach the managed worker environment (contract §7.3, §8.2, §8.7).

Every launch here is composed, never spawned. The gate is pinned per test
except the default-gate case, which uses the real reader on an empty home.
"""
from __future__ import annotations

import logging

import pytest

from hyprial.daemon.impl.application import compose_daemon_worker_launch
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.kernel import HarnessLaunchSpec

LEGACY_VALUE = "LEGACY-SENTINEL-0123456789"
OWNER_VALUE = "OWNER-SENTINEL-9876543210"
PRIVATE_VALUE = "PRIVATE-SENTINEL-5555"


def _compose(harness, *, app=None):
    channel = build_worker_channel(
        actor="agent:owner-a:pk-node:worker",
        session_ref="sess-1",
        hyprial_home=harness.home,
        state_dir=harness.state,
        node_id="pk-node",
        owner="owner-a",
    )
    spec = HarnessLaunchSpec(harness="pi", name="worker", headless=True)
    kwargs = {} if app is None else {"app": app}
    return compose_daemon_worker_launch(
        registry=harness.registry, hyprial_home=harness.home, spec=spec,
        channel=channel, environ={"PATH": "/usr/bin:/bin"}, **kwargs,
    )


def test_default_gate_is_legacy_and_reads_nothing(harness):
    harness.legacy_private_grant("deepseek", "DEEPSEEK_API_KEY")
    harness.service.answer = RuntimeError("must not be called")
    before = _compose(harness)
    after = _compose(harness, app=harness.app)
    assert after.environment.for_exec() == before.environment.for_exec()
    assert after.grants == before.grants == (("deepseek", 1),)
    assert harness.service.calls == []
    assert harness.resolved == []


def test_pinned_legacy_gate_is_byte_identical_and_reads_nothing(harness, gate):
    gate("legacy")
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY")
    harness.service.answer = RuntimeError("must not be called")
    before = _compose(harness)
    after = _compose(harness, app=harness.app)
    assert after.environment.for_exec() == before.environment.for_exec()
    assert after.environment.for_exec()["OPENAI_API_KEY"] == LEGACY_VALUE
    assert harness.service.calls == [] and harness.resolved == []


def test_owner_keys_inject_the_unsealed_secret(harness, gate):
    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY", seq=4),))
    launch = _compose(harness, app=harness.app)
    environment = launch.environment.for_exec()
    assert environment["OPENAI_API_KEY"] == OWNER_VALUE
    assert launch.grants == (("openai", 4),)
    [(name, agent, now_ms)] = harness.service.calls
    assert name == "launch_secrets"
    assert agent.actor == "worker" and agent.entity_token == harness.agent.entity_token
    assert isinstance(now_ms, int) and now_ms > 0
    assert harness.resolved == [("worker", None)]


def test_a_held_slot_shadows_the_legacy_grant_of_the_same_id(harness, gate, caplog):
    gate("owner-keys")
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY")
    harness.legacy_private_grant("deepseek", "DEEPSEEK_API_KEY")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY"),))
    with caplog.at_level(logging.WARNING):
        launch = _compose(harness, app=harness.app)
    environment = launch.environment.for_exec()
    assert environment["OPENAI_API_KEY"] == OWNER_VALUE
    assert environment["DEEPSEEK_API_KEY"] == PRIVATE_VALUE
    assert launch.grants == (("deepseek", 1), ("openai", 4))
    shadow = [record for record in caplog.records if "openai" in record.getMessage()]
    assert shadow, "a shadowed legacy grant must be logged"
    assert all(LEGACY_VALUE not in record.getMessage() for record in caplog.records)
    assert all(OWNER_VALUE not in record.getMessage() for record in caplog.records)


def test_a_shadowed_legacy_grant_is_never_resolved(harness, gate):
    gate("owner-keys")
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY", write=False)
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY"),))
    launch = _compose(harness, app=harness.app)
    assert launch.environment.for_exec()["OPENAI_API_KEY"] == OWNER_VALUE


def test_an_empty_held_slot_still_shadows_the_legacy_grant(harness, gate):
    gate("owner-keys")
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY")
    harness.service.answer = (frozenset({"openai"}), ())
    launch = _compose(harness, app=harness.app)
    assert "OPENAI_API_KEY" not in launch.environment.for_exec()
    assert launch.grants == ()


def test_legacy_grants_outside_the_held_slots_resolve_as_today(harness, gate):
    gate("owner-keys")
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY")
    harness.service.answer = (frozenset(), ())
    launch = _compose(harness, app=harness.app)
    assert launch.environment.for_exec()["OPENAI_API_KEY"] == LEGACY_VALUE
    assert launch.grants == (("openai", 1),)


def test_every_launch_asks_the_service_again(harness, gate):
    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY"),))
    _compose(harness, app=harness.app)
    _compose(harness, app=harness.app)
    assert [call[0] for call in harness.service.calls] == ["launch_secrets", "launch_secrets"]


@pytest.mark.parametrize("name", ["PATH", "HOME", "HYPRIAL_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "lower"])
def test_an_unsafe_owner_environment_name_fails_the_launch_closed(harness, gate, name):
    from hyprial.identity import SecretResolutionError

    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", name),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness, app=harness.app)
    assert OWNER_VALUE not in str(caught.value)


def test_an_environment_collision_with_a_legacy_grant_fails_closed(harness, gate):
    from hyprial.identity import SecretResolutionError

    gate("owner-keys")
    harness.legacy_private_grant("deepseek", "SHARED_API_KEY")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "SHARED_API_KEY"),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness, app=harness.app)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")
    assert OWNER_VALUE not in str(caught.value) and PRIVATE_VALUE not in str(caught.value)


def test_a_collision_is_refused_before_any_legacy_value_is_read(harness, gate):
    from hyprial.identity import SecretResolutionError

    gate("owner-keys")
    harness.legacy_user_grant("anthropic", "anthropic", "SHARED_API_KEY", write=False)
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "SHARED_API_KEY"),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness, app=harness.app)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")


def test_two_held_secrets_on_one_environment_name_fail_closed(harness, gate):
    from hyprial.identity import SecretResolutionError

    gate("owner-keys")
    harness.service.answer = (frozenset({"openai", "azure"}), (
        harness.held("openai", "OPENAI_API_KEY"), harness.held("azure", "OPENAI_API_KEY"),
    ))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness, app=harness.app)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "TERM"])
def test_a_child_vocabulary_name_is_refused_whatever_the_daemon_environ(harness, gate, name):
    from hyprial.identity import SecretResolutionError

    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", name),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness, app=harness.app)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")


def test_the_receipt_writing_call_runs_once_per_launch_and_never_for_foreign_hosted_agents(harness, gate):
    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY"),))
    _compose(harness, app=harness.app)
    assert [call[0] for call in harness.service.calls] == ["launch_secrets"]
    guest = harness.foreign_hosted()
    from hyprial.daemon.impl.provider_keys.launch import launch_resolver

    assert launch_resolver(harness.app, registry=harness.registry)(guest.actor) is None
    assert [call[0] for call in harness.service.calls] == ["launch_secrets"]
    assert harness.resolved == [("worker", None)]
