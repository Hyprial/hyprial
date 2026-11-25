"""F-5: grant renewal on the owner device before launch, and its refusal (contract §9.5) [security]."""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.daemon.impl.provider_keys.launch import launch_resolver
from hyprial.identity import SecretResolutionError
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import NOW, SECRET, HOUR


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


def _granted(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    return ref


def _grant(world, ref) -> dict:
    return world.service().grants_for(ref)[0]


def _disable_signing(world) -> None:
    world.app._user_key_signer = type(world.signer)(world.signer.store)


def _last_receipt(world) -> dict:
    lines = (world.state / "provider-keys" / "receipts.jsonl").read_text().splitlines()
    return json.loads(lines[-1])


def test_expired_grant_is_renewed_then_launch_succeeds(world):
    ref = _granted(world)
    later = NOW + GRANT_TTL_MS + 5
    slots, secrets = world.service().launch_secrets(ref, now_ms=later)
    assert slots == frozenset({"anthropic"}) and [s.value for s in secrets] == [SECRET]
    state = _grant(world, ref)
    payload = state["grant"]["payload"]
    assert state["seq"] == 2 and secrets[0].seq == 2
    assert (payload["issuedAt"], payload["expiresAt"]) == (later, later + GRANT_TTL_MS)
    assert payload["dekVersion"] == 1
    assert _last_receipt(world)["outcome"] == "injected"


def test_grant_past_half_life_is_renewed_before_use(world):
    ref = _granted(world)
    world.service().launch_secrets(ref, now_ms=NOW + HOUR)
    assert _grant(world, ref)["seq"] == 1
    half = NOW + GRANT_TTL_MS // 2
    world.service().launch_secrets(ref, now_ms=half)
    state = _grant(world, ref)
    assert state["seq"] == 2 and state["grant"]["payload"]["issuedAt"] == half


def test_expired_grant_without_signing_refuses_recipient_pending(world):
    ref = _granted(world)
    _disable_signing(world)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS)) == \
        ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING
    assert _grant(world, ref)["seq"] == 1
    assert _last_receipt(world)["outcome"] == "refused:PROVIDER_KEY_RECIPIENT_PENDING"


def test_unexpired_grant_past_half_life_is_used_when_renewal_is_impossible(world):
    ref = _granted(world)
    _disable_signing(world)
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS - 1)
    assert [s.value for s in secrets] == [SECRET] and _grant(world, ref)["seq"] == 1


def test_unanchored_signer_cannot_renew_an_expired_grant(world, tmp_path):
    from ..conftest import bound_signer

    ref = _granted(world)
    stranger = bound_signer(tmp_path / "stranger")
    world.anchor.revoked.add(stranger.store.status()["keyId"])
    world.app._user_key_signer = stranger
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS)) == \
        ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING


def test_launch_resolver_renews_then_injects_through_the_legacy_surface(world):
    ref = _granted(world)
    resolve = launch_resolver(world.app, registry=world.registry, clock=lambda: NOW + GRANT_TTL_MS + 1)
    slots, held = resolve("worker")
    assert slots == frozenset({"anthropic"})
    assert [(h.grant_id, h.revision, h.environment_names, h.value) for h in held] == [
        ("anthropic", 2, ("ANTHROPIC_API_KEY",), SECRET)]
    assert held[0].entity_token == ref.entity_token


def test_launch_resolver_maps_impossible_renewal_to_secret_resolution_failure(world):
    _granted(world)
    _disable_signing(world)
    resolve = launch_resolver(world.app, registry=world.registry, clock=lambda: NOW + GRANT_TTL_MS + 1)
    with pytest.raises(SecretResolutionError) as caught:
        resolve("worker")
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert caught.value.category == ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING
    assert SECRET not in str(caught.value)


def test_revoked_grant_is_never_renewed(world):
    ref = _granted(world)
    world.service().revoke(agent=ref, grant_id="anthropic")
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS + 1)) == \
        ipc_errors.PROVIDER_GRANT_REVOKED
    record = world.read_json(storage.grant_path(ref.agent, "anthropic"))["payload"]
    assert (record["state"], record["seq"]) == ("revoked", 2)


def test_anchor_unavailable_for_the_signer_is_store_unavailable_not_pending(world, tmp_path):
    from ..conftest import bound_signer

    ref = _granted(world)
    world.app._user_key_signer = bound_signer(tmp_path / "stranger")
    for now in (NOW + GRANT_TTL_MS, NOW + GRANT_TTL_MS - 1):
        assert _code(lambda: world.service().launch_secrets(ref, now_ms=now)) == \
            ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert _grant(world, ref)["seq"] == 1


class _LosesEveryWrite:
    def __init__(self, inner) -> None:
        self.inner, self.writes = inner, 0

    def read(self, path):
        return self.inner.read(path)

    def listdir(self, path):
        return self.inner.listdir(path)

    def remove(self, path):
        return self.inner.remove(path)

    def write(self, path, text, *, expect_version, create_only):
        self.writes += 1
        raise storage.StaleWrite()


class _Resolver:
    def __init__(self, store) -> None:
        self.store = store

    def store_for(self, owner):
        return self.store


def test_half_life_renewal_that_loses_every_cas_still_injects(world):
    ref = _granted(world)
    lossy = _LosesEveryWrite(world.store)
    world.app._provider_key_space_resolver = _Resolver(lossy)
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS - 1)
    assert [s.value for s in secrets] == [SECRET] and secrets[0].seq == 1
    assert lossy.writes > 0 and _grant(world, ref)["seq"] == 1
    assert _last_receipt(world)["outcome"] == "injected"


def test_expired_renewal_that_loses_every_cas_waits_for_the_owner(world):
    ref = _granted(world)
    world.app._provider_key_space_resolver = _Resolver(_LosesEveryWrite(world.store))
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + GRANT_TTL_MS)) == \
        ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING


def test_revocation_refuses_the_legacy_launch_resolver_without_returning_secrets(world):
    ref = _granted(world)
    world.service().revoke(agent=ref, grant_id="anthropic")
    resolve = launch_resolver(world.app, registry=world.registry, clock=lambda: NOW + 1)
    with pytest.raises(SecretResolutionError) as caught:
        resolve("worker")
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert caught.value.category == ipc_errors.PROVIDER_GRANT_REVOKED
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert SECRET not in str(caught.value)
    assert _grant(world, ref)["state"] == "revoked"
    assert _last_receipt(world)["outcome"] == "refused:PROVIDER_GRANT_REVOKED"
