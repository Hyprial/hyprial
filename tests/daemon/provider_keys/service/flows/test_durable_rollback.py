"""Rollback marks survive a daemon restart: a replayed older record never renews or injects [security].

A restart is simulated by dropping the in-process mark cache on the app, so the next
service is built from the durable marks under the local state directory only.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import HOUR, NOW, SECRET, SUB

LATER = NOW + GRANT_TTL_MS + HOUR


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


def _restart(world) -> None:
    if hasattr(world.app, "_provider_key_high_water"):
        del world.app._provider_key_high_water


def _put(world, path: str, text: str) -> None:
    found = world.store.read(path)
    if found is None:
        world.store.write(path, text, expect_version=None, create_only=True)
    else:
        world.store.write(path, text, expect_version=found[1], create_only=False)


def _marks(world) -> list:
    return sorted((world.state / "provider-keys").glob("high-water-*.json"))


def test_replayed_active_grant_after_revoke_and_restart_is_refused(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    active = world.store.read(path)[0]
    world.service().revoke(agent=ref, grant_id="anthropic")
    _put(world, path, active)
    _restart(world)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=LATER)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert world.store.read(path)[0] == active


def test_replayed_key_and_grant_after_logout_and_restart_are_refused(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    saved = {path: world.store.read(path)[0] for path in (
        storage.key_meta_path(key["keyId"]), storage.key_envelope_path(key["keyId"], 1),
        storage.grant_path(ref.agent, "anthropic"))}
    world.service().logout(key_id=key["keyId"])
    for path, text in saved.items():
        _put(world, path, text)
    _restart(world)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=LATER)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    _restart(world)
    assert world.service().list_keys()["invalid"] == [
        {"keyId": key["keyId"], "code": ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID}]


def test_replayed_meta_after_rotate_and_restart_is_refused(world):
    key = world.login()
    path = storage.key_meta_path(key["keyId"])
    old = world.store.read(path)[0]
    world.service().rotate(key_id=key["keyId"])
    _put(world, path, old)
    _restart(world)
    assert world.service().list_keys()["keys"] == []
    assert _code(lambda: world.service().rotate(key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_marks_are_owner_local_private_and_value_free(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    files = _marks(world)
    assert len(files) == 1
    info = files[0].stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert stat.S_IMODE((world.state / "provider-keys").stat().st_mode) == 0o700
    data = json.loads(files[0].read_text())
    assert data["owner"] == SUB
    assert storage.grant_path(ref.agent, "anthropic") in data["marks"]
    assert world.sentinel_hits(SECRET) == []


def test_marks_from_another_instance_are_merged_not_lowered(world):
    world.login()
    ref = world.ref()
    stale = world.service()
    _restart(world)
    world.service().allow(agent=ref, provider="anthropic")
    world.service().revoke(agent=ref, grant_id="anthropic")
    other = storage.grant_path("00000000-0000-4000-8000-000000000000", "other")
    stale._ctx.high_water.observe(other, {"v": 1, "seq": 1})
    data = json.loads(_marks(world)[0].read_text())
    assert data["marks"][storage.grant_path(ref.agent, "anthropic")][0] == 2
    assert data["marks"][other][0] == 1


@pytest.mark.parametrize("damage", ["garbage", "symlink", "wrong-owner-field"])
def test_damaged_marks_fail_closed(world, damage):
    world.login()
    target = _marks(world)[0]
    if damage == "garbage":
        target.write_text("{not json")
    elif damage == "symlink":
        decoy = world.tmp / "decoy.json"
        decoy.write_text(target.read_text())
        target.unlink()
        os.symlink(decoy, target)
    else:
        data = json.loads(target.read_text())
        data["owner"] = "someone-else"
        target.write_text(json.dumps(data))
    _restart(world)
    assert _code(world.service) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
