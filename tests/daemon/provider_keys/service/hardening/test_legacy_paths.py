"""Legacy agent.secret.* on the real owner service: fail closed, recover, never strand (contract §8).

- A slot whose signed record fails verification hides its legacy row, routes revoke to
  the authority (which refuses) and is recovered by a re-grant (§8.2, §8.4).
- An awaiting-key slot whose completion failed is completed by replaying the grant.
- A guest or foreign-owner actor keeps its legacy rows in an actor-scoped list (§8.1).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.provider_keys import legacy, storage
from hyprial.identity import ProviderKeys, SecretSource
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import NOW, SECRET

GRANT = {"actor": "worker", "grantId": "gui-first-run", "source": "user-provider", "entryId": "deepseek",
         "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1}


@pytest.fixture
def bridged(world):
    world.app._agent_session_domains = SimpleNamespace(
        agent=SimpleNamespace(read_secret_inventory=world.registry.secret_inventory))
    return world


def _write(world, value: str = SECRET) -> dict:
    return legacy.dispatch(world.app, "agent.secret.provider-write",
                           {"entryId": "deepseek", "fieldName": "key", "value": value})


def _rows(world, **params) -> list[tuple[str, str, str]]:
    answer = legacy.dispatch(world.app, "agent.secret.list", params)
    return [(row["actor"], row["grantId"], row["entryId"]) for row in answer["grants"]]


def _tamper(world, grant_id: str) -> None:
    path = storage.grant_path(world.ref().agent, grant_id)
    record = json.loads(world.store.read(path)[0])
    record["payload"]["environmentNames"] = ["TAMPERED_KEY"]
    world.overwrite(path, record)


def test_an_unverifiable_slot_never_falls_back_to_the_legacy_row_and_a_re_grant_recovers(bridged):
    world = bridged
    world.registry.grant_secret("worker", grant_id="gui-first-run", source=SecretSource.USER_PROVIDER,
                                entry_id="old-entry", field_name="key", environment_names=("OLD_KEY",),
                                revision=7)
    _write(world)
    legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    assert _rows(world, actor="worker") == [("worker", "gui-first-run", "deepseek")]
    _tamper(world, "gui-first-run")
    assert _rows(world, actor="worker") == []
    assert _rows(world) == []
    with pytest.raises(DaemonRequestError) as caught:
        legacy.dispatch(world.app, "agent.secret.revoke", {"actor": "worker", "grantId": "gui-first-run"})
    assert caught.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    regrant = legacy.dispatch(world.app, "agent.secret.grant", {**GRANT, "revision": 2})
    assert regrant["grant"]["revision"] == 2
    _, secrets = world.service().launch_secrets(world.ref(), now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]
    assert _rows(world, actor="worker") == [("worker", "gui-first-run", "deepseek")]


class FailFirstGrantWrite:
    """OwnerStore wrapper whose first grant-state write fails like a transient store error."""

    def __init__(self, inner) -> None:
        self.inner, self.armed = inner, True

    def read(self, path):
        return self.inner.read(path)

    def write(self, path, text, *, expect_version, create_only):
        if self.armed and path.startswith(storage.GRANTS_ROOT + "/"):
            self.armed = False
            raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
        return self.inner.write(path, text, expect_version=expect_version, create_only=create_only)

    def listdir(self, path):
        return self.inner.listdir(path)

    def remove(self, path):
        return self.inner.remove(path)


class Resolver:
    def __init__(self, store) -> None:
        self.store = store

    def store_for(self, owner):
        return self.store


def test_replaying_an_awaiting_grant_completes_it_once_the_key_exists(bridged):
    world = bridged
    legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    world.app._provider_key_space_resolver = Resolver(FailFirstGrantWrite(world.store))
    assert _write(world) == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    world.app._provider_key_space_resolver = Resolver(world.store)
    ref = world.ref()
    assert world.service().grants_for(ref)[0]["state"] == "awaiting-key"
    replay = legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    assert replay["grant"]["revision"] == 1
    assert world.service().grants_for(ref)[0]["state"] == "active"
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_replaying_an_awaiting_grant_without_a_key_stays_a_no_op(bridged):
    world = bridged
    first = legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT))
    assert legacy.dispatch(world.app, "agent.secret.grant", dict(GRANT)) == first
    states = world.service().grants_for(world.ref())
    assert [(state["state"], state["seq"]) for state in states] == [("awaiting-key", 1)]


@pytest.mark.parametrize("create", ["transfer_hosted"])
def test_an_actor_scoped_list_of_a_non_local_agent_keeps_its_legacy_rows(bridged, create):
    world = bridged
    factory = getattr(world.registry, "create_" + create)
    guest = factory("guest", pinned_owner="carol@test.invalid")
    world.registry.grant_secret("guest", grant_id="routes", source=SecretSource.AGENT_PRIVATE,
                                entry_id="routes.json", field_name="key", environment_names=("ROUTES_KEY",),
                                revision=1)
    assert _rows(world, actor="guest") == [("guest", "routes", "routes.json")]
    expected = {"uri": guest.uri, "createdAtMs": guest.created_at_ms}
    assert _rows(world, actor="guest", expectedAgent=expected) == [("guest", "routes", "routes.json")]
    with pytest.raises(DaemonRequestError) as caught:
        _rows(world, actor="guest", expectedAgent={**expected, "createdAtMs": guest.created_at_ms + 1})
    assert caught.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    with pytest.raises(DaemonRequestError) as missing:
        _rows(world, actor="ghost")
    assert missing.value.code == ipc_errors.AGENT_NOT_FOUND
