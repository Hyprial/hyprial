"""Owner context: gate, store resolver, owner identity, owner anchor and agent resolution [security]."""
from __future__ import annotations

import base64
import os

import pytest

from hyprial.daemon.impl.provider_keys import context, storage
from hyprial.daemon.impl.provider_keys.service import OwnerKeyService
from hyprial.daemon.impl.user_key.verifier import Lookup
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import OWNER, SUB


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    return caught.value.code


def test_injected_service_wins_without_reading_the_gate(world):
    sentinel = object()
    world.app._provider_key_service = sentinel
    world.settings("{damaged")
    assert context.service_for(world.app) is sentinel


def test_open_gate_builds_the_owner_service(world):
    service = context.service_for(world.app)
    assert isinstance(service, OwnerKeyService)


@pytest.mark.parametrize("settings", [
    {"owner": OWNER, "userId": SUB},
    {"owner": OWNER, "userId": SUB, "providerKeys": {"authority": "legacy"}},
    {"owner": OWNER, "userId": SUB, "providerKeys": {"authority": "bogus"}},
    "{not json",
])
def test_closed_or_damaged_gate_refuses_unavailable(world, settings):
    if isinstance(settings, str):
        (world.home / "settings.json").write_text(settings)
    else:
        world.settings(settings)
    assert _code(lambda: context.service_for(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_absent_home_settings_is_legacy_and_refuses(world):
    (world.home / "settings.json").unlink()
    assert _code(lambda: context.service_for(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_production_resolver_default_uses_local_state_and_absent_home_is_unavailable(world):
    del world.app._provider_key_space_resolver
    assert isinstance(context.service_for(world.app), OwnerKeyService)
    world.app.hyprial_home = None
    assert _code(lambda: context.service_for(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    missing = world.tmp / "nonexistent-home"
    world.app.hyprial_home = missing
    assert _code(lambda: context.owner_context(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert not missing.exists()
    loose = world.tmp / "loose-home"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    world.app.hyprial_home = loose
    assert _code(lambda: context.owner_context(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert not (loose / "state").exists()


def test_missing_owner_identity_is_unavailable(world):
    world.signer.store.casdoor_sub = ""
    assert _code(lambda: context.service_for(world.app)) == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def _anchor(world, port):
    return context.OwnerAnchor(port, OWNER)


def test_anchor_adapts_verified_u2_lookup_to_owner_key(world):
    anchor = _anchor(world, world.anchor)
    result = anchor.lookup(SUB, world.anchor.key_id)
    assert result.state == "verified"
    assert result.key == ProviderKeys.OwnerKey(SUB, world.anchor.key_id,
                                               base64.b64decode(world.anchor.public_key))
    assert world.anchor.calls[-1] == (OWNER, world.anchor.key_id)


def test_anchor_keeps_absent_and_unavailable_distinct(world):
    anchor = _anchor(world, world.anchor)
    assert anchor.lookup(SUB, "ed25519:" + "0" * 32).state == "unavailable"
    world.anchor.revoked.add("ed25519:" + "0" * 32)
    assert anchor.lookup(SUB, "ed25519:" + "0" * 32).state == "absent"
    world.anchor.state = "unavailable"
    assert anchor.lookup(SUB, world.anchor.key_id).state == "unavailable"
    assert _anchor(world, None).lookup(SUB, world.anchor.key_id).state == "unavailable"


def test_anchor_verified_for_another_subject_is_absent(world):
    assert _anchor(world, world.anchor).lookup("another-sub", world.anchor.key_id).state == "absent"


def test_anchor_source_failure_is_unavailable(world):
    class Broken:
        def lookup(self, owner, key_id):
            raise RuntimeError("boom")

    class Garbage:
        def lookup(self, owner, key_id):
            return Lookup("verified", "!!not-base64!!", {}, 0, "d", SUB)

    assert _anchor(world, Broken()).lookup(SUB, world.anchor.key_id).state == "unavailable"
    assert _anchor(world, Garbage()).lookup(SUB, world.anchor.key_id).state == "unavailable"


def test_resolve_agent_by_name_id_and_uri(world):
    agent = world.agent
    by_name = context.resolve_agent(world.app, "worker", None)
    assert by_name.actor == agent.actor and by_name.uri == agent.uri
    assert by_name.entity_token == agent.entity_token
    assert by_name.agent == ProviderKeys.agent_uuid(agent.entity_token)
    assert by_name.agent_id == agent.agent_id
    assert by_name.created_at_ms == agent.created_at_ms
    assert context.resolve_agent(world.app, agent.agent_id, None) == by_name
    assert context.resolve_agent(world.app, agent.uri, None) == by_name


def test_resolve_agent_checks_expected_incarnation(world):
    agent = world.agent
    good = {"uri": agent.uri, "createdAtMs": agent.created_at_ms}
    assert context.resolve_agent(world.app, "worker", good).entity_token == agent.entity_token
    for bad in ({**good, "uri": agent.uri + "x"}, {**good, "createdAtMs": agent.created_at_ms + 1}):
        assert _code(lambda: context.resolve_agent(world.app, "worker", bad)) == \
            ipc_errors.AGENT_INCARNATION_MISMATCH
    assert _code(lambda: context.resolve_agent(world.app, "ghost", good)) == \
        ipc_errors.AGENT_INCARNATION_MISMATCH


def test_resolve_agent_missing_and_guest_are_not_found(world):
    assert _code(lambda: context.resolve_agent(world.app, "ghost", None)) == ipc_errors.AGENT_NOT_FOUND
    assert _code(lambda: context.resolve_agent(world.app, "a." + "b" * 26, None)) == ipc_errors.AGENT_NOT_FOUND
    guest = world.registry.create_transfer_hosted("guest", pinned_owner="bob@test.invalid")
    assert _code(lambda: context.resolve_agent(world.app, guest.actor, None)) == ipc_errors.AGENT_NOT_FOUND
    assert _code(lambda: context.resolve_agent(world.app, guest.agent_id, None)) == ipc_errors.AGENT_NOT_FOUND


def test_injected_agent_resolver_is_honored(world):
    calls = []
    world.app._provider_key_agent_resolver = lambda ref, expected: calls.append((ref, expected)) or "ref"
    assert context.resolve_agent(world.app, "x", {"uri": "u", "createdAtMs": 1}) == "ref"
    assert calls == [("x", {"uri": "u", "createdAtMs": 1})]


def test_store_is_resolved_for_the_casdoor_subject(world):
    seen = []

    class Recording:
        def store_for(self, owner):
            seen.append(owner)
            return world.store

    world.app._provider_key_space_resolver = Recording()
    context.service_for(world.app)
    assert seen == [SUB]
    assert isinstance(world.store, storage.OwnerStore)
