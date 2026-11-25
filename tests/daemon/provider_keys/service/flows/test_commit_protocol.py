"""Contract §4.3 commit-point protocol and §8.3/§5.2 preconditions, pinned against regressions [security]."""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import SECRET


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


class Recording:
    """OwnerStore wrapper that records writes and removes, and can tamper envelope reads after a meta write."""

    def __init__(self, inner, *, tamper: bool = False) -> None:
        self.inner, self.tamper, self.ops, self.meta_written = inner, tamper, [], False

    def read(self, path):
        found = self.inner.read(path)
        if found is not None and self.tamper and self.meta_written and "/envelope-" in path:
            envelope = json.loads(found[0])
            envelope["header"]["createdAt"] += 1
            return json.dumps(envelope), found[1]
        return found

    def listdir(self, path):
        return self.inner.listdir(path)

    def remove(self, path):
        self.ops.append(("remove", path))
        return self.inner.remove(path)

    def write(self, path, text, *, expect_version, create_only):
        self.ops.append(("write", path))
        result = self.inner.write(path, text, expect_version=expect_version, create_only=create_only)
        self.meta_written = self.meta_written or path.endswith("/meta.json")
        return result


class Resolver:
    def __init__(self, store) -> None:
        self.store = store

    def store_for(self, owner):
        return self.store


def _record(world, **kwargs) -> Recording:
    recording = Recording(world.store, **kwargs)
    world.app._provider_key_space_resolver = Resolver(recording)
    return recording


def test_login_readback_failure_is_reported(world):
    _record(world, tamper=True)
    assert _code(world.login) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_login_local_probe_failure_is_reported(world, monkeypatch):
    sealed_type = type(world.service()._ctx.sealed)

    def probe(self, envelope, *, owner_key):
        raise ProviderKeys.Error(ipc_errors.PROVIDER_KEY_UNSEAL_FAILED)

    monkeypatch.setattr(sealed_type, "probe_sealed", probe)
    assert _code(world.login) == ipc_errors.PROVIDER_KEY_UNSEAL_FAILED


def test_rotate_readback_failure_is_reported_before_any_reissue(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    recording = _record(world, tamper=True)
    assert _code(lambda: world.service().rotate(key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert [op for op in recording.ops if "provider-grants" in op[1]] == []


def test_rotate_order_is_create_next_then_meta_then_reissue_then_remove(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    recording = _record(world)
    world.service().rotate(key_id=key["keyId"])
    assert recording.ops == [
        ("write", storage.key_envelope_path(key["keyId"], 2)),
        ("write", storage.key_meta_path(key["keyId"])),
        ("write", storage.grant_path(ref.agent, "anthropic")),
        ("remove", storage.key_envelope_path(key["keyId"], 1)),
    ]


def test_logout_order_is_revoke_then_tombstone_then_remove(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    recording = _record(world)
    world.service().logout(key_id=key["keyId"])
    assert recording.ops == [
        ("write", storage.grant_path(ref.agent, "anthropic")),
        ("write", storage.key_meta_path(key["keyId"])),
        ("remove", storage.key_envelope_path(key["keyId"], 1)),
    ]


@pytest.mark.parametrize("damage", ["removed", "replaced"])
def test_active_meta_whose_envelope_does_not_bind_is_listed_invalid(world, damage):
    key = world.login()
    path = storage.key_envelope_path(key["keyId"], 1)
    if damage == "removed":
        world.store.remove(path)
    else:
        other = world.login(secret="sk-other-synthetic")
        world.overwrite(path, json.loads(world.store.read(storage.key_envelope_path(other["keyId"], 1))[0]))
    listed = world.service().list_keys()
    assert key["keyId"] not in [row["keyId"] for row in listed["keys"]]
    assert {"keyId": key["keyId"], "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID} in listed["invalid"]


def test_allow_after_reprovisioning_this_device_is_recipient_pending(world):
    world.login()
    sealed = world.service()._ctx.sealed
    before = sealed.recipient().key_id
    (sealed.root / "key.json").unlink()
    sealed.ensure()
    assert sealed.recipient().key_id != before
    assert _code(lambda: world.service().allow(agent=world.ref(), provider="anthropic")) == \
        ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING
    assert world.store.listdir(storage.GRANTS_ROOT) == ()


def test_put_awaiting_refuses_a_rotated_incarnation_before_writing(world):
    ref = world.ref()
    world.registry.rotate_secret_authority("worker")
    assert _code(lambda: world.service().put_awaiting(
        agent=ref, grant_id="gui-first-run", provider="deepseek", environment_names=["DEEPSEEK_API_KEY"],
        legacy_ref={"entryId": "deepseek", "fieldName": "key", "revision": 1})) == \
        ipc_errors.AGENT_INCARNATION_MISMATCH
    assert world.store.listdir(storage.GRANTS_ROOT) == ()
