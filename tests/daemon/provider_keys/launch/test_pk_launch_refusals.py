"""G2a refusals: every owner-key failure reaches the legacy start surface as
AGENT_SECRET_RESOLUTION_FAILED, keeps its code only as the category, and
carries no value, message or cause (contract §7.3, §8.5).
"""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.application import compose_daemon_worker_launch
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.identity import ProviderKeys, SecretResolutionError
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors

OWNER_VALUE = "OWNER-SENTINEL-9876543210"

SIX = (
    ipc_errors.PROVIDER_GRANT_MISSING,
    ipc_errors.PROVIDER_GRANT_REVOKED,
    ipc_errors.PROVIDER_KEY_NOT_FOUND,
    ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING,
    ipc_errors.PROVIDER_KEY_UNSEAL_FAILED,
    ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID,
    ipc_errors.PROVIDER_STORE_UNAVAILABLE,
)


def _compose(harness):
    channel = build_worker_channel(
        actor="agent:owner-a:pk-node:worker",
        session_ref="sess-1",
        hyprial_home=harness.home,
        state_dir=harness.state,
        node_id="pk-node",
        owner="owner-a",
    )
    return compose_daemon_worker_launch(
        registry=harness.registry, hyprial_home=harness.home,
        spec=HarnessLaunchSpec(harness="pi", name="worker", headless=True),
        channel=channel, environ={"PATH": "/usr/bin:/bin"}, app=harness.app,
    )


def _assert_sanitized(error: SecretResolutionError, category: str) -> None:
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert error.category == category
    assert error.__cause__ is None and error.__suppress_context__
    assert error.__context__ is None
    assert OWNER_VALUE not in str(error) and OWNER_VALUE not in repr(error)


def test_an_export_query_refusal_carries_no_context(harness, gate):
    from hyprial.daemon.impl.provider_keys.launch import held_slots

    gate("owner-keys")
    harness.service.answer = RuntimeError(f"boom {OWNER_VALUE}")
    with pytest.raises(SecretResolutionError) as caught:
        held_slots(harness.app, "worker", registry=harness.registry)
    _assert_sanitized(caught.value, ipc_errors.DAEMON_ERROR)


def test_a_held_secret_outside_the_reported_slots_is_refused(harness, gate):
    gate("owner-keys")
    harness.legacy_user_grant("openai", "openai", "LEGACY_OPENAI_KEY")
    harness.service.answer = (frozenset(), (harness.held("openai", "OPENAI_API_KEY"),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")
    assert OWNER_VALUE not in str(caught.value)


def test_a_duplicated_held_grant_id_is_refused(harness, gate):
    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (
        harness.held("openai", "OPENAI_API_KEY"), harness.held("openai", "OPENAI_KEY"),
    ))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    assert (caught.value.category, caught.value.phase) == ("invalid", "owner-key")


@pytest.mark.parametrize("code", SIX)
def test_a_service_refusal_surfaces_as_the_declared_launch_code(harness, gate, code):
    gate("owner-keys")
    harness.service.answer = ProviderKeys.Error(code)
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    _assert_sanitized(caught.value, code)


def test_an_incarnation_refusal_is_wrapped(harness, gate):
    gate("owner-keys")

    def mismatch(ref, expected):
        raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "agent incarnation changed")

    harness.app._provider_key_agent_resolver = mismatch
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    _assert_sanitized(caught.value, ipc_errors.AGENT_INCARNATION_MISMATCH)


def test_an_unexpected_failure_never_leaks_its_message(harness, gate):
    gate("owner-keys")
    harness.service.answer = RuntimeError(f"boom {OWNER_VALUE}")
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    _assert_sanitized(caught.value, ipc_errors.DAEMON_ERROR)


def test_a_damaged_gate_fails_the_launch_closed(harness, gate):
    gate(ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    _assert_sanitized(caught.value, ipc_errors.PROVIDER_STORE_UNAVAILABLE)
    assert harness.service.calls == []


def test_an_unavailable_service_fails_the_launch_closed(harness, gate):
    gate("owner-keys")
    harness.app._provider_key_service = None
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    _assert_sanitized(caught.value, ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def test_a_secret_unsealed_for_another_incarnation_is_refused(harness, gate):
    from hyprial.daemon.impl.provider_keys.api import AgentRef

    gate("owner-keys")
    real = harness.app._provider_key_agent_resolver

    def stale(ref, expected):
        agent = real(ref, expected)
        return AgentRef(actor=agent.actor, uri=agent.uri, created_at_ms=0,
                        entity_token="f" * 32, agent=agent.agent, agent_id=agent.agent_id)

    harness.app._provider_key_agent_resolver = stale
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY", value=OWNER_VALUE),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    assert caught.value.category == "grant-stale"
    assert OWNER_VALUE not in str(caught.value)


def test_an_empty_value_is_refused(harness, gate):
    gate("owner-keys")
    harness.service.answer = (frozenset({"openai"}), (harness.held("openai", "OPENAI_API_KEY", value=""),))
    with pytest.raises(SecretResolutionError) as caught:
        _compose(harness)
    assert caught.value.category == "invalid"
