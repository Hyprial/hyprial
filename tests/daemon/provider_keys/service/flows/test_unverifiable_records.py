"""Unverifiable neighbour records and post-commit failures never block the owner [security].

The anchor in these tests behaves like the real U2 port: a record sealed by a key id
with no binding document looks up as unavailable, not absent.
"""
from __future__ import annotations

import json
import uuid

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import NOW, SECRET


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


def _plant(world, *, agent: str | None = None, grant_id: str = "planted") -> str:
    """A grant-state file sealed by a stranger key id that the anchor reports unavailable."""
    agent = agent or str(uuid.uuid4())
    record = {"payload": {"type": "provider-grant-state", "state": "revoked", "seq": 1},
              "sealedBy": {"keyId": "ed25519:" + "ab" * 16}, "sig": "AAAA"}
    path = storage.grant_path(agent, grant_id)
    world.store.write(path, json.dumps(record), expect_version=None, create_only=True)
    return path


class Scans:
    """OwnerStore wrapper that fails grant-root scans after the first `allowed` ones."""

    def __init__(self, inner, allowed: int) -> None:
        self.inner, self.allowed = inner, allowed

    def read(self, path):
        return self.inner.read(path)

    def remove(self, path):
        return self.inner.remove(path)

    def write(self, path, text, *, expect_version, create_only):
        return self.inner.write(path, text, expect_version=expect_version, create_only=create_only)

    def listdir(self, path):
        if path == storage.GRANTS_ROOT:
            if self.allowed <= 0:
                raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
            self.allowed -= 1
        return self.inner.listdir(path)


class Resolver:
    def __init__(self, store) -> None:
        self.store = store

    def store_for(self, owner):
        return self.store


def test_planted_unavailable_grant_record_does_not_block_logout(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    _plant(world)
    result = world.service().logout(key_id=key["keyId"])
    assert result["alreadyDeleted"] is False
    assert [row["grantId"] for row in result["revokedGrants"]] == ["anthropic"]
    meta = world.read_json(storage.key_meta_path(key["keyId"]))["payload"]
    assert meta["state"] == "deleted"
    assert world.store.listdir(storage.key_dir(key["keyId"])) == ("meta.json",)
    assert world.service().grants_for(ref)[0]["state"] == "revoked"


def test_planted_record_in_the_same_agent_does_not_block_logout(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    _plant(world, agent=ref.agent)
    world.service().logout(key_id=key["keyId"])
    assert world.read_json(storage.key_meta_path(key["keyId"]))["payload"]["state"] == "deleted"


def test_planted_record_does_not_block_list_rotate_or_legacy_lookup(world):
    key = world.login(legacy_ref={"entryId": "anthropic", "fieldName": "key"})
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    _plant(world)
    listed = world.service().list_keys()
    assert [row["keyId"] for row in listed["keys"]] == [key["keyId"]]
    assert [row["grantId"] for row in listed["keys"][0]["grants"]] == ["anthropic"]
    rotated = world.service().rotate(key_id=key["keyId"])
    assert rotated["reissuedGrants"] == 1 and rotated["key"]["dekVersion"] == 2
    found = world.service().find_key_by_legacy_ref(entry_id="anthropic", field_name="key")
    assert found is not None and found["keyId"] == key["keyId"]
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_planted_record_does_not_fail_a_committed_login(world):
    _plant(world)
    key = world.login()
    assert key["state"] == "active" and key["grants"] == []
    assert world.store.listdir(storage.KEYS_ROOT) == (key["keyId"],)


def test_unreadable_grant_scan_after_the_login_commit_still_reports_the_key(world):
    world.app._provider_key_space_resolver = Resolver(Scans(world.store, 0))
    key = world.login()
    assert key["state"] == "active" and key["grants"] == []
    assert world.store.listdir(storage.KEYS_ROOT) == (key["keyId"],)


def test_unreadable_grant_scan_after_the_rotate_commit_still_reports_the_rotation(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.app._provider_key_space_resolver = Resolver(Scans(world.store, 1))
    rotated = world.service().rotate(key_id=key["keyId"])
    assert rotated["reissuedGrants"] == 1 and rotated["key"]["dekVersion"] == 2
    assert rotated["key"]["grants"] == []
    world.app._provider_key_space_resolver = Resolver(world.store)
    assert world.store.listdir(storage.key_dir(key["keyId"])) == ("envelope-2.json", "meta.json")


def test_unavailable_key_meta_still_aborts_logout(world):
    key = world.login()
    world.anchor.state = "unavailable"
    assert _code(lambda: world.service().logout(key_id=key["keyId"])) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    world.anchor.state = "verified"
    assert world.read_json(storage.key_meta_path(key["keyId"]))["payload"]["state"] == "active"
