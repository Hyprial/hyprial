"""The launch path reads ``providerKeys.authority`` from the home's settings on
every call (contract §8.7): absent means legacy, only "owner-keys" opens the
new authority, anything unreadable fails closed. No environment override.
"""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys.launch import held_slots, launch_resolver
from hyprial.identity import SecretResolutionError
from hyprial.kernel import ipc_errors


def _write(harness, text: str) -> None:
    (harness.home / "settings.json").write_text(text, encoding="utf-8")


def _launch(harness, name: str = "worker"):
    return launch_resolver(harness.app, registry=harness.registry)(name)


def _held(harness, name: str = "worker"):
    return held_slots(harness.app, name, registry=harness.registry)


def test_a_home_less_application_is_legacy(harness):
    harness.app.hyprial_home = None
    harness.service.answer = RuntimeError("must not be called")
    assert _launch(harness) is None and _held(harness) is None
    assert harness.service.calls == []


def test_a_foreign_hosted_agent_never_reaches_the_owner_authority(harness):
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    guest = harness.foreign_hosted()
    harness.service.answer = RuntimeError("must not be called")
    assert _launch(harness, guest.actor) is None
    assert _held(harness, guest.actor) is None
    assert harness.service.calls == [] and harness.resolved == []


def test_a_local_agent_still_refuses_when_the_owner_authority_refuses(harness):
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    harness.service.answer = RuntimeError("store down")
    with pytest.raises(SecretResolutionError):
        _launch(harness)
    with pytest.raises(SecretResolutionError):
        _held(harness)


def test_held_slots_reports_each_state_payload(harness):
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    harness.service.grant_states = [harness.grant_state("openai", "OPENAI_API_KEY", state="revoked")]
    [slot] = _held(harness)
    assert (slot.grant_id, slot.entry_id, slot.state, slot.environment_names) == (
        "openai", "openai", "revoked", ("OPENAI_API_KEY",))


@pytest.mark.parametrize("payload", [
    {"grantId": "openai"},
    {"grantId": 7, "provider": "openai", "state": "active", "environmentNames": ["A_KEY"]},
    {"grantId": "openai", "provider": "openai", "state": "active", "environmentNames": "A_KEY"},
])
def test_a_malformed_state_payload_refuses(harness, payload):
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    harness.service.grant_states = [payload]
    with pytest.raises(SecretResolutionError) as caught:
        _held(harness)
    assert caught.value.category == ipc_errors.DAEMON_ERROR


@pytest.mark.parametrize("text", [
    None,
    json.dumps({}),
    json.dumps({"owner": "owner-a"}),
    json.dumps({"providerKeys": {}}),
    json.dumps({"providerKeys": {"authority": "legacy"}}),
])
def test_legacy_settings_read_nothing(harness, text):
    if text is not None:
        _write(harness, text)
    harness.service.answer = RuntimeError("must not be called")
    assert _launch(harness) is None
    assert _held(harness) is None
    assert harness.service.calls == [] and harness.resolved == []


def test_owner_keys_opens_the_new_authority(harness):
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    assert _launch(harness) == (frozenset(), ())
    assert _held(harness) == ()


@pytest.mark.parametrize("text", [
    "{damaged",
    json.dumps([]),
    json.dumps({"providerKeys": "owner-keys"}),
    json.dumps({"providerKeys": {"authority": "OWNER-KEYS"}}),
    json.dumps({"providerKeys": {"authority": None}}),
    json.dumps({"providerKeys": {"authority": True}}),
    json.dumps({"provider_keys": {"authority": "owner-keys"}}),
    json.dumps({"provider_keys": {"authority": "legacy"}}),
])
def test_unreadable_settings_fail_closed(harness, text):
    _write(harness, text)
    for call in (lambda: _launch(harness), lambda: _held(harness)):
        with pytest.raises(SecretResolutionError) as caught:
            call()
        assert caught.value.category == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert harness.service.calls == []


def test_the_environment_cannot_open_the_gate(harness, monkeypatch):
    monkeypatch.setenv("HYPRIAL_PROVIDER_KEYS_AUTHORITY", "owner-keys")
    monkeypatch.setenv("PROVIDER_KEYS_AUTHORITY", "owner-keys")
    harness.service.answer = RuntimeError("must not be called")
    assert _launch(harness) is None


def test_launch_uses_the_single_storage_gate_reader(harness, monkeypatch):
    from hyprial.daemon.impl.provider_keys import launch
    from hyprial.daemon.impl.provider_keys.storage import gate

    homes = []

    def spy(home):
        homes.append(home)
        return "legacy"

    monkeypatch.setattr(gate, "authority", spy)
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    assert _launch(harness) is None and _held(harness) is None
    assert homes == [harness.app.hyprial_home, harness.app.hyprial_home]
    assert not {"_authority", "LEGACY", "OWNER_KEYS"} & set(vars(launch))


def test_the_gate_is_read_on_every_launch(harness):
    resolve = launch_resolver(harness.app, registry=harness.registry)
    assert resolve("worker") is None
    _write(harness, json.dumps({"providerKeys": {"authority": "owner-keys"}}))
    assert resolve("worker") == (frozenset(), ())
