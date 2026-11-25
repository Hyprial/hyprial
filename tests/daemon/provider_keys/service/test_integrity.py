"""Record integrity: CAS retries, rollback, tampering and anchor states fail closed [security]."""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import CAS_ATTEMPTS
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import NOW, SECRET


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


class Flaky:
    """OwnerStore wrapper that loses the next `stale` conditional writes."""

    def __init__(self, inner, stale: int) -> None:
        self.inner, self.stale, self.writes = inner, stale, 0

    def read(self, path):
        return self.inner.read(path)

    def listdir(self, path):
        return self.inner.listdir(path)

    def remove(self, path):
        return self.inner.remove(path)

    def write(self, path, text, *, expect_version, create_only):
        self.writes += 1
        if self.stale > 0:
            self.stale -= 1
            raise storage.StaleWrite()
        return self.inner.write(path, text, expect_version=expect_version, create_only=create_only)


class Resolver:
    def __init__(self, store) -> None:
        self.store = store

    def store_for(self, owner):
        return self.store


def _flaky(world, stale: int) -> Flaky:
    flaky = Flaky(world.store, stale)
    world.app._provider_key_space_resolver = Resolver(flaky)
    return flaky


def test_lost_compare_and_set_is_retried(world):
    world.login()
    ref = world.ref()
    flaky = _flaky(world, CAS_ATTEMPTS - 1)
    assert world.service().allow(agent=ref, provider="anthropic")["grant"]["seq"] == 1
    assert flaky.writes == CAS_ATTEMPTS


def test_compare_and_set_conflict_after_the_retry_budget(world):
    world.login()
    ref = world.ref()
    flaky = _flaky(world, CAS_ATTEMPTS)
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic")) == \
        ipc_errors.PROVIDER_STORE_CONFLICT
    assert flaky.writes == CAS_ATTEMPTS
    assert world.store.listdir(storage.GRANTS_ROOT) == ()


def _foreign_active_grant(world, ref, key) -> str:
    """A grant-state for `key` signed by the owner, as another owner device would write it."""
    signing = world.signer.signing_key("provider-key/test")
    recipient = key["recipients"][0]["recipientKeyId"]
    grant = ProviderKeys.sign_grant({
        "v": 1, "provider": key["provider"], "keyId": key["keyId"], "owner": key["owner"], "agent": ref.agent,
        "dekVersion": key["dekVersion"], "recipientKeyId": recipient, "issuedAt": NOW, "expiresAt": NOW + 1000,
    }, signer=signing)
    state = {"type": "provider-grant-state", "v": 1, "owner": key["owner"], "agent": ref.agent,
             "grantId": "anthropic", "provider": key["provider"], "state": "active", "seq": 1,
             "keyId": key["keyId"], "grant": grant, "environmentNames": ["ANTHROPIC_API_KEY"],
             "legacyRef": None, "updatedAt": NOW}
    return json.dumps(ProviderKeys.sign_record(state, signer=signing))


def test_conflict_retry_revalidates_preconditions(world):
    first = world.login()
    second = world.login(secret="sk-other-synthetic")
    ref = world.ref()
    path = storage.grant_path(ref.agent, "anthropic")
    foreign = _foreign_active_grant(world, ref, second)

    class Racing(Flaky):
        def write(self, path_, text, *, expect_version, create_only):
            if self.stale:
                self.stale = 0
                self.inner.write(path, foreign, expect_version=None, create_only=True)
                raise storage.StaleWrite()
            return self.inner.write(path_, text, expect_version=expect_version, create_only=create_only)

    world.app._provider_key_space_resolver = Resolver(Racing(world.store, 1))
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic", key_id=first["keyId"])) == \
        ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    assert world.service().grants_for(ref)[0]["keyId"] == second["keyId"]


def test_replayed_older_grant_is_a_rollback_and_never_injects(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    active_text = world.store.read(path)[0]
    world.service().launch_secrets(ref, now_ms=NOW + 1)
    world.service().revoke(agent=ref, grant_id="anthropic")
    _, version = world.store.read(path)
    world.store.write(path, active_text, expect_version=version, create_only=False)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 2)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert world.service().grants_for(ref) == []


def test_replayed_older_meta_is_a_rollback(world):
    key = world.login()
    path = storage.key_meta_path(key["keyId"])
    old_text = world.store.read(path)[0]
    world.service().list_keys()
    world.service().rotate(key_id=key["keyId"])
    _, version = world.store.read(path)
    world.store.write(path, old_text, expect_version=version, create_only=False)
    listed = world.service().list_keys()
    assert listed["keys"] == []
    assert listed["invalid"] == [{"keyId": key["keyId"], "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID}]


def test_tampered_meta_is_listed_invalid_and_refused(world):
    key = world.login()
    path = storage.key_meta_path(key["keyId"])
    record = world.read_json(path)
    record["payload"]["label"] = "changed"
    world.overwrite(path, record)
    assert world.service().list_keys()["invalid"] == [
        {"keyId": key["keyId"], "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID}]
    assert _code(lambda: world.service().allow(agent=world.ref(), provider="anthropic", key_id=key["keyId"])) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert _code(lambda: world.service().rotate(key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


@pytest.mark.parametrize("target", ["envelope", "grant", "unparseable"])
def test_tampered_envelope_or_grant_refuses_launch(world, target):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    if target == "envelope":
        path = storage.key_envelope_path(key["keyId"], 1)
        envelope = world.read_json(path)
        envelope["header"]["createdAt"] += 1
        world.overwrite(path, envelope)
    elif target == "grant":
        path = storage.grant_path(ref.agent, "anthropic")
        record = world.read_json(path)
        record["payload"]["environmentNames"] = ["STOLEN_NAME"]
        world.overwrite(path, record)
    else:
        path = storage.grant_path(ref.agent, "anthropic")
        _, version = world.store.read(path)
        world.store.write(path, "{not json", expect_version=version, create_only=False)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_record_copied_to_another_agent_path_is_refused(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    helper = world.registry.create("helper")
    other = world.ref("helper")
    text = world.store.read(storage.grant_path(ref.agent, "anthropic"))[0]
    world.store.write(storage.grant_path(other.agent, "anthropic"), text, expect_version=None, create_only=True)
    assert helper.agent_id == other.agent_id
    assert _code(lambda: world.service().launch_secrets(other, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_envelope_at_the_meta_path_is_refused(world):
    key = world.login()
    envelope = world.store.read(storage.key_envelope_path(key["keyId"], 1))[0]
    world.overwrite(storage.key_meta_path(key["keyId"]), json.loads(envelope))
    assert world.service().list_keys()["invalid"][0]["code"] == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_record_signed_by_an_unanchored_key_is_invalid(world):
    key = world.login()
    world.anchor.revoked.add(world.anchor.key_id)
    assert world.service().list_keys()["invalid"] == [
        {"keyId": key["keyId"], "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID}]


def test_unavailable_anchor_is_never_absent(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.anchor.state = "unavailable"
    assert _code(lambda: world.service().list_keys()) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_STORE_UNAVAILABLE
    status = world.service().status(agent=ref)["grants"][0]
    assert (status["state"], status["code"]) == ("unavailable", ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def test_deleted_meta_cannot_be_reactivated(world):
    key = world.login()
    world.service().logout(key_id=key["keyId"])
    meta = world.read_json(storage.key_meta_path(key["keyId"]))["payload"]
    revived = {**meta, "seq": meta["seq"] + 1, "state": "active", "dekVersion": 1, "envelopeDigest": "0" * 64,
               "recipients": [{"recipientKeyId": "x25519:" + "0" * 32, "role": "owner-device", "deviceUid": "d"}]}
    with pytest.raises(ProviderKeys.Error):
        ProviderKeys.check_transition(meta, revived)
    assert _code(lambda: world.service().rotate(key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_NOT_FOUND
