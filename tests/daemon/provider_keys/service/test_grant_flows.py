"""Grant slots: allow, status, revoke and launch on a real single-replica OrgFS space [security]."""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import DEVICE, NOW, OTHER_SECRET, SECRET, SUB


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.data)
    return caught.value.code


def _receipts(world) -> list[dict]:
    path = world.state / "provider-keys" / "receipts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_allow_then_status_ok_and_launch_injects(world):
    key = world.login(environment_names=["ANTHROPIC_API_KEY"])
    ref = world.ref()
    result = world.service().allow(agent=ref, provider="anthropic")
    assert result["changed"] is True
    grant = result["grant"]
    assert grant | {"issuedAt": None, "expiresAt": None, "recipientKeyId": None} == {
        "agent": ref.agent, "agentId": ref.agent_id, "actor": ref.actor, "grantId": "anthropic",
        "provider": "anthropic", "owner": SUB, "keyId": key["keyId"], "state": "active", "seq": 1,
        "dekVersion": 1, "issuedAt": None, "expiresAt": None, "environmentNames": ["ANTHROPIC_API_KEY"],
        "recipientKeyId": None, "legacyRef": None,
    }
    assert grant["issuedAt"] == NOW and grant["expiresAt"] == NOW + GRANT_TTL_MS
    assert grant["recipientKeyId"] == key["recipients"][0]["recipientKeyId"]
    status = world.service().status(agent=ref)
    assert status == {"agent": ref.agent, "agentId": ref.agent_id, "actor": ref.actor, "grants": [{
        "grantId": "anthropic", "provider": "anthropic", "state": "ok", "code": None, "owner": SUB,
        "keyId": key["keyId"], "seq": 1, "dekVersion": 1, "expiresAt": NOW + GRANT_TTL_MS,
        "unsealable": True}]}
    slots, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert slots == frozenset({"anthropic"})
    assert [(s.grant_id, s.provider, s.seq, s.environment_names, s.value) for s in secrets] == [
        ("anthropic", "anthropic", 1, ("ANTHROPIC_API_KEY",), SECRET)]
    assert SECRET not in repr(secrets)
    receipt = _receipts(world)[-1]
    assert receipt["outcome"] == "injected" and receipt["keyId"] == key["keyId"]
    assert receipt["agent"] == ref.agent and receipt["agentId"] == ref.agent_id
    assert receipt["deviceUid"] == DEVICE and receipt["owner"] == SUB
    assert receipt["grantDigest"] == ProviderKeys.record_digest(world.service().grants_for(ref)[0]["grant"])
    listed = world.service().list_keys()["keys"][0]
    assert listed["lastUsedAt"] == NOW + 1
    assert listed["grants"] == [{"agent": ref.agent, "agentId": ref.agent_id, "grantId": "anthropic",
                                 "provider": "anthropic", "state": "active"}]


def test_allow_resolves_the_single_active_key(world):
    ref = world.ref()
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic")) == \
        ipc_errors.PROVIDER_KEY_NOT_FOUND
    world.login()
    world.login()
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic")) == ipc_errors.INVALID_ARGUMENT
    other = world.login(provider="openai")
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic", key_id=other["keyId"])) == \
        ipc_errors.PROVIDER_KEY_NOT_FOUND


def test_allow_is_idempotent_for_equivalent_content(world):
    world.login()
    ref = world.ref()
    first = world.service().allow(agent=ref, provider="anthropic")
    again = world.service().allow(agent=ref, provider="anthropic")
    assert again == {"grant": first["grant"], "changed": False}
    changed = world.service().allow(agent=ref, provider="anthropic", environment_names=["OTHER_NAME"])
    assert changed["changed"] is True and changed["grant"]["seq"] == 2
    assert changed["grant"]["environmentNames"] == ["OTHER_NAME"]


def test_idempotent_allow_needs_no_signer(world):
    world.login()
    ref = world.ref()
    first = world.service().allow(agent=ref, provider="anthropic")
    world.app._user_key_signer = type(world.signer)(world.signer.store)
    assert world.service().allow(agent=ref, provider="anthropic") == {"grant": first["grant"], "changed": False}
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic", environment_names=["X_KEY"])) == \
        ipc_errors.USER_KEY_UNBOUND


def test_another_key_in_the_slot_requires_replace(world):
    first = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic", key_id=first["keyId"])
    second = world.login(secret=OTHER_SECRET)
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic", key_id=second["keyId"])) == \
        ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    replaced = world.service().allow(agent=ref, provider="anthropic", key_id=second["keyId"], replace=True)
    assert replaced["grant"]["keyId"] == second["keyId"] and replaced["grant"]["seq"] == 2
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [OTHER_SECRET]


def test_allow_refuses_when_this_device_is_not_a_recipient(world):
    world.login()
    world.service()._ctx.sealed.revoke()
    assert _code(lambda: world.service().allow(agent=world.ref(), provider="anthropic")) == \
        ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING


def test_allow_refuses_a_rotated_incarnation_before_writing(world):
    world.login()
    ref = world.ref()
    world.registry.rotate_secret_authority("worker")
    assert _code(lambda: world.service().allow(agent=ref, provider="anthropic")) == \
        ipc_errors.AGENT_INCARNATION_MISMATCH
    assert world.store.listdir(storage.GRANTS_ROOT) == ()


def test_revoke_is_idempotent_and_refuses_the_whole_launch(world):
    key = world.login()
    ref = world.ref()
    assert _code(lambda: world.service().revoke(agent=ref, grant_id="anthropic")) == \
        ipc_errors.PROVIDER_GRANT_MISSING
    world.service().allow(agent=ref, provider="anthropic")
    revoked = world.service().revoke(agent=ref, grant_id="anthropic")
    assert revoked["changed"] is True and revoked["restart"] == {"state": "not-supported"}
    assert (revoked["grant"]["state"], revoked["grant"]["seq"], revoked["grant"]["keyId"]) == \
        ("revoked", 2, key["keyId"])
    assert revoked["grant"]["dekVersion"] is None and revoked["grant"]["issuedAt"] is None
    world.app._user_key_signer = type(world.signer)(world.signer.store)
    again = world.service().revoke(agent=ref, grant_id="anthropic")
    assert again["changed"] is False and again["grant"]["seq"] == 2
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_GRANT_REVOKED
    assert _receipts(world)[-1]["outcome"] == "refused:PROVIDER_GRANT_REVOKED"
    status = world.service().status(agent=ref)["grants"][0]
    assert (status["state"], status["code"], status["unsealable"]) == \
        ("revoked", ipc_errors.PROVIDER_GRANT_REVOKED, False)


def test_revoke_reports_not_running_when_liveness_says_so(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")

    class Liveness:
        def verdict(self, uri):
            return False

        def binding(self, uri):
            return None

    world.app._agent_liveness = Liveness()
    assert world.service().revoke(agent=ref, grant_id="anthropic")["restart"] == {"state": "not-running"}


def test_reallow_after_revoke_raises_seq(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().revoke(agent=ref, grant_id="anthropic")
    back = world.service().allow(agent=ref, provider="anthropic")
    assert (back["changed"], back["grant"]["state"], back["grant"]["seq"]) == (True, "active", 3)


def test_status_reports_missing_slot_and_filters_by_provider(world):
    world.login()
    world.login(provider="openai")
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().allow(agent=ref, provider="openai")
    assert [row["grantId"] for row in world.service().status(agent=ref)["grants"]] == ["anthropic", "openai"]
    assert [row["grantId"] for row in world.service().status(agent=ref, provider="openai")["grants"]] == ["openai"]
    missing = world.service().status(agent=ref, provider="mistral")["grants"]
    assert missing == [{"grantId": "mistral", "provider": "mistral", "state": "missing",
                        "code": ipc_errors.PROVIDER_GRANT_MISSING, "owner": None, "keyId": None,
                        "seq": None, "dekVersion": None, "expiresAt": None, "unsealable": False}]


def test_status_reports_deleted_key_and_awaiting_slots(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().put_awaiting(agent=ref, grant_id="legacy-slot", provider="deepseek",
                                 environment_names=["DEEPSEEK_API_KEY"],
                                 legacy_ref={"entryId": "deepseek", "fieldName": "key", "revision": 1})
    world.store.remove(storage.key_envelope_path(key["keyId"], 1))
    rows = {row["grantId"]: row for row in world.service().status(agent=ref)["grants"]}
    assert (rows["anthropic"]["state"], rows["anthropic"]["code"]) == \
        ("signature-invalid", ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID)
    assert (rows["legacy-slot"]["state"], rows["legacy-slot"]["code"]) == \
        ("awaiting-key", ipc_errors.PROVIDER_KEY_NOT_FOUND)


def test_launch_fails_closed_on_any_unverifiable_slot(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    junk = storage.grant_path(ref.agent, "junk")
    world.store.write(junk, '{"payload": {}, "sealedBy": {}}', expect_version=None, create_only=True)
    service = world.service()
    assert [grant["grantId"] for grant in service.grants_for(ref)] == ["anthropic"]
    assert service.slot_names(ref) == frozenset({"anthropic", "junk"})
    assert _code(lambda: service.launch_secrets(ref, now_ms=NOW + 1)) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_launch_with_no_records_holds_no_slot(world):
    assert world.service().launch_secrets(world.ref(), now_ms=NOW) == (frozenset(), ())


def test_launch_refuses_a_deleted_key(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().put_awaiting(agent=world.ref(), grant_id="other", provider="other",
                                 environment_names=["OTHER_KEY"],
                                 legacy_ref={"entryId": "other", "fieldName": "key", "revision": 1})
    world.service().revoke(agent=ref, grant_id="other")
    meta = world.read_json(storage.key_meta_path(key["keyId"]))["payload"]
    deleted = {**meta, "seq": meta["seq"] + 1, "state": "deleted", "dekVersion": None,
               "envelopeDigest": None, "recipients": []}
    signed = ProviderKeys.sign_record(deleted, signer=world.signer.signing_key("provider-key/test"))
    world.overwrite(storage.key_meta_path(key["keyId"]), signed)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == ipc_errors.PROVIDER_KEY_NOT_FOUND
    assert _receipts(world)[-1]["outcome"] == "refused:PROVIDER_KEY_NOT_FOUND"


def test_receipt_failure_never_changes_the_launch(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    (world.state / "provider-keys" / "receipts.jsonl").mkdir()
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_refused_launch_leaves_no_injected_receipt(world):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().put_awaiting(agent=ref, grant_id="gui-first-run", provider="deepseek",
                                 environment_names=["DEEPSEEK_API_KEY"],
                                 legacy_ref={"entryId": "deepseek", "fieldName": "key", "revision": 1})
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_NOT_FOUND
    assert [row["outcome"] for row in _receipts(world)] == ["refused:PROVIDER_KEY_NOT_FOUND"]
    assert world.service().list_keys()["keys"][0]["lastUsedAt"] is None


def test_undecodable_plaintext_refusal_carries_no_secret_context(world, monkeypatch):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    sealed_type = type(world.service()._ctx.sealed)
    monkeypatch.setattr(sealed_type, "open_sealed", lambda self, envelope, owner_key: b"\xff" + SECRET.encode())
    with pytest.raises(DaemonRequestError) as caught:
        world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert caught.value.code == ipc_errors.PROVIDER_KEY_UNSEAL_FAILED
    assert caught.value.__context__ is None and caught.value.__cause__ is None
