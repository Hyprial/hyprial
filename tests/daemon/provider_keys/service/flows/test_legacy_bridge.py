"""The C2 legacy agent.secret.* adapter on the real owner service (contract §8.1, §8.4)."""
from __future__ import annotations

from hyprial.daemon.impl.provider_keys import legacy, storage

from ..conftest import NOW, OTHER_SECRET, SECRET

GRANT = {"actor": "worker", "grantId": "gui-first-run", "source": "user-provider", "entryId": "deepseek",
         "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1}


def _write(world, value: str) -> dict:
    return legacy.dispatch(world.app, "agent.secret.provider-write",
                           {"entryId": "deepseek", "fieldName": "key", "value": value})


def test_grant_before_write_waits_then_write_completes_and_launch_injects(world):
    row = legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    assert row == {"ok": True, "grant": {
        "actor": world.agent.actor, "grantId": "gui-first-run", "source": "user-provider",
        "entryId": "deepseek", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
        "ownerActor": None, "ownerEntityToken": None}}
    assert _write(world, SECRET) == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    ref = world.ref()
    slots, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert slots == frozenset({"gui-first-run"})
    assert [(s.value, s.environment_names) for s in secrets] == [(SECRET, ("DEEPSEEK_API_KEY",))]
    assert legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT)) == row
    assert world.service().grants_for(ref)[0]["seq"] == 2


def test_second_write_rotates_the_same_key(world):
    _write(world, SECRET)
    legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    _write(world, OTHER_SECRET)
    keys = world.service().list_keys()["keys"]
    assert len(keys) == 1 and keys[0]["dekVersion"] == 2 and keys[0]["label"] == "legacy:deepseek/key"
    _, secrets = world.service().launch_secrets(world.ref(), now_ms=NOW + 1)
    assert [s.value for s in secrets] == [OTHER_SECRET]
    assert world.sentinel_hits(SECRET, OTHER_SECRET) == []


def test_revoke_of_a_held_slot_goes_to_the_owner_authority(world):
    _write(world, SECRET)
    legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    result = legacy.dispatch(world.app, "agent.secret.revoke", {"actor": "worker", "grantId": "gui-first-run"})
    assert result == {"ok": True, "revoked": True}
    record = world.read_json(storage.grant_path(world.ref().agent, "gui-first-run"))["payload"]
    assert record["state"] == "revoked"
    assert legacy.dispatch(world.app, "agent.secret.revoke",
                           {"actor": "worker", "grantId": "never-held"}) is legacy.NOT_HANDLED
