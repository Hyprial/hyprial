"""Record reads: a stale read is re-read, a deleted record is refused, a re-grant recovers [security].

A store wrapper runs another commit right after one read of a path returns, which is
the interleaving two daemon request threads can produce.
"""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import legacy, storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import NOW, SECRET

HALF = NOW + GRANT_TTL_MS // 2 + 1


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


class Interleave:
    """OwnerStore wrapper: right after an armed read of `path` returns, run `hook` once."""

    def __init__(self, inner, path: str) -> None:
        self.inner, self.path, self.hook, self.armed = inner, path, None, False
        self.skip_reads = 0

    def read(self, path):
        found = self.inner.read(path)
        if self.armed and path == self.path:
            if self.skip_reads:
                self.skip_reads -= 1
            else:
                self.armed = False
                self.hook()
        return found

    def write(self, path, text, *, expect_version, create_only):
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


def _racing(world, path: str, hook) -> Interleave:
    """Route the app through an Interleave whose hook runs `hook` against the plain store."""
    plain = world.store
    wrapper = Interleave(plain, path)

    def run():
        world.app._provider_key_space_resolver = Resolver(plain)
        try:
            hook()
        finally:
            world.app._provider_key_space_resolver = Resolver(wrapper)

    wrapper.hook = run
    world.app._provider_key_space_resolver = Resolver(wrapper)
    return wrapper


def _renew(world, ref, at: int = HALF):
    """Another thread's launch: it renews past half-life; its own outcome is not under test."""

    def launch():
        try:
            world.service().launch_secrets(ref, now_ms=at)
        except DaemonRequestError:
            pass

    return launch


def test_a_renewal_racing_the_rotate_scan_still_reissues_the_grant(world, monkeypatch):
    from hyprial.daemon.impl.provider_keys.ops import grants

    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    wrapper = _racing(world, storage.grant_path(ref.agent, "anthropic"), _renew(world, ref))
    scan = grants.grant_states

    def armed(ctx):
        wrapper.armed = True
        return scan(ctx)

    monkeypatch.setattr(grants, "grant_states", armed)
    rotated = world.service().rotate(key_id=key["keyId"])
    monkeypatch.setattr(grants, "grant_states", scan)
    world.app._provider_key_space_resolver = Resolver(world.store)
    assert rotated["reissuedGrants"] == 1
    assert world.service().grants_for(ref)[0]["grant"]["payload"]["dekVersion"] == 2
    _, secrets = world.service().launch_secrets(ref, now_ms=HALF + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_a_commit_racing_a_listing_keeps_the_slot_listed(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    wrapper = _racing(world, storage.grant_path(ref.agent, "anthropic"), _renew(world, ref))
    wrapper.armed = True
    listed = world.service().grants_for(ref)
    assert [state["state"] for state in listed] == ["active"]
    assert listed[0]["seq"] == 2


def test_a_legacy_revoke_racing_a_renewal_is_not_lost(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic", grant_id="slot")
    wrapper = _racing(world, storage.grant_path(ref.agent, "slot"), _renew(world, ref))
    wrapper.armed = True
    answer = legacy.dispatch(world.app, "agent.secret.revoke", {"actor": "worker", "grantId": "slot"})
    world.app._provider_key_space_resolver = Resolver(world.store)
    assert answer == {"ok": True, "revoked": True}
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=HALF + 2)) == \
        ipc_errors.PROVIDER_GRANT_REVOKED


def test_a_real_rollback_is_still_refused_after_the_re_read(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    active = world.store.read(path)[0]
    world.service().revoke(agent=ref, grant_id="anthropic")
    found = world.store.read(path)
    world.store.write(path, active, expect_version=found[1], create_only=False)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_a_deleted_revoked_record_still_holds_its_slot_and_refuses_launch(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().revoke(agent=ref, grant_id="anthropic")
    world.store.remove(storage.grant_path(ref.agent, "anthropic"))
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert world.service().slot_names(ref) == frozenset({"anthropic"})
    rows = world.service().status(agent=ref)["grants"]
    assert [(row["grantId"], row["state"]) for row in rows] == [("anthropic", "signature-invalid")]


def test_a_re_grant_over_a_deleted_record_writes_above_the_mark(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().revoke(agent=ref, grant_id="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    world.store.remove(path)
    result = world.service().allow(agent=ref, provider="anthropic")
    assert result["changed"] is True and result["grant"]["seq"] == 3
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_a_re_grant_over_a_tampered_record_recovers_the_slot(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    record = json.loads(world.store.read(path)[0])
    record["payload"]["environmentNames"] = ["TAMPERED_KEY"]
    world.overwrite(path, record)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert _code(lambda: world.service().revoke(agent=ref, grant_id="anthropic")) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    result = world.service().allow(agent=ref, provider="anthropic")
    assert result["changed"] is True and result["grant"]["seq"] == 2
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


@pytest.mark.parametrize("skip_reads", [0, 1], ids=["before-recovery-read", "after-recovery-read"])
@pytest.mark.parametrize("unavailable,replace", [(False, False), (False, True), (True, False)],
                         ids=["active-requires-replace", "active-replace-allowed", "anchor-unavailable"])
def test_re_grant_revalidates_the_exact_snapshot_used_for_cas(world, skip_reads, unavailable, replace):
    first = world.login()
    second = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic", key_id=first["keyId"])
    world.service().allow(agent=ref, provider="anthropic", key_id=second["keyId"], replace=True)
    path = storage.grant_path(ref.agent, "anthropic")
    recovered = world.read_json(path)
    corrupted = json.loads(json.dumps(recovered))
    corrupted["payload"]["environmentNames"] = ["TAMPERED_KEY"]
    world.overwrite(path, corrupted)
    if unavailable:
        recovered["sealedBy"]["keyId"] = "ed25519:" + "ab" * 16
    wrapper = _racing(world, path, lambda: world.overwrite(path, recovered))
    wrapper.skip_reads = skip_reads
    wrapper.armed = True

    def regrant():
        return world.service().allow(agent=ref, provider="anthropic", key_id=first["keyId"],
                                     replace=replace)

    if replace:
        result = regrant()
        assert result["changed"] is True and result["grant"]["seq"] == 3
        assert world.read_json(path)["payload"]["keyId"] == first["keyId"]
    else:
        expected = ipc_errors.PROVIDER_STORE_UNAVAILABLE if unavailable else ipc_errors.PROVIDER_CREDENTIAL_EXISTS
        assert _code(regrant) == expected
        assert world.read_json(path) == recovered
