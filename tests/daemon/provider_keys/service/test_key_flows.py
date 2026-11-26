"""Key lifecycle on a real single-replica OrgFS space: login, list, rotate, logout [security]."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hyprial.daemon.impl.provider_keys import storage
from hyprial.identity import ProviderKeys
from hyprial.identity.impl.provider_keys.envelope import PROFILE as PROVIDER_KEY_PROFILE
from hyprial.identity.impl.provider_keys.models import encode
from hyprial.identity.impl.sealed_envelope.core import _unwrap
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import DEVICE, NOW, OTHER_SECRET, SECRET, SUB, bound_signer


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


def _local_recipient(world):
    return world.service()._ctx.sealed.recipient().key_id


def _x1_private(world) -> bytes:
    sealed = world.service()._ctx.sealed
    record = json.loads((sealed.root / "key.json").read_text())
    return base64.b64decode(record["privateKey"])


def _open(world, envelope) -> bytes:
    key = ProviderKeys.OwnerKey(SUB, world.anchor.key_id, base64.b64decode(world.anchor.public_key))
    return world.service()._ctx.sealed.open_sealed(envelope, owner_key=key)


def test_login_commits_signed_meta_and_envelope_readable_only_here(world):
    key = world.login(label="work", environment_names=["ANTHROPIC_API_KEY", "CLAUDE_KEY"])
    assert key["keyId"].startswith("pk.") and len(key["keyId"]) == 29
    assert key | {"keyId": None, "createdAt": None, "updatedAt": None} == {
        "keyId": None, "provider": "anthropic", "kind": "api-key", "label": "work", "owner": SUB,
        "state": "active", "createdAt": None, "updatedAt": None, "dekVersion": 1,
        "environmentNames": ["ANTHROPIC_API_KEY", "CLAUDE_KEY"],
        "recipients": [{"recipientKeyId": _local_recipient(world), "role": "owner-device",
                        "deviceUid": DEVICE}],
        "lastUsedAt": None, "grants": [],
    }
    assert key["createdAt"] == key["updatedAt"] == NOW
    meta = world.read_json(storage.key_meta_path(key["keyId"]))
    assert set(meta) == {"payload", "sealedBy"} and meta["payload"]["seq"] == 1
    envelope = world.read_json(storage.key_envelope_path(key["keyId"], 1))
    ProviderKeys.check_envelope_matches(meta["payload"], envelope)
    assert _open(world, envelope) == SECRET.encode()
    assert world.store.listdir(storage.key_dir(key["keyId"])) == ("envelope-1.json", "meta.json")


def test_login_defaults_environment_names_and_label(world):
    key = world.login(provider="open.ai-x")
    assert key["environmentNames"] == ["OPEN_AI_X_API_KEY"]
    assert key["label"] == ""
    assert world.login(provider="9lives")["environmentNames"] == ["API_9LIVES_API_KEY"]


def test_every_login_is_a_new_key(world):
    first, second = world.login(), world.login()
    assert first["keyId"] != second["keyId"]
    listed = world.service().list_keys()
    assert sorted(row["keyId"] for row in listed["keys"]) == sorted([first["keyId"], second["keyId"]])
    assert listed["invalid"] == []


def test_login_with_disabled_signer_refuses_before_any_write(world):
    world.app._user_key_signer = type(world.signer)(world.signer.store)
    assert _code(world.login) == ipc_errors.USER_KEY_UNBOUND
    assert world.store.listdir(storage.KEYS_ROOT) == ()


def test_login_refuses_unknown_kind(world):
    assert _code(lambda: world.service().login(provider="anthropic", kind="oauth", secret=SECRET)) == \
        ipc_errors.INVALID_ARGUMENT


def test_list_filters_and_reports_last_use(world):
    one = world.login(provider="anthropic")
    world.login(provider="openai")
    service = world.service()
    assert [row["keyId"] for row in service.list_keys(provider="anthropic")["keys"]] == [one["keyId"]]
    storage.append_receipt(world.state, {
        "at": NOW + 5, "owner": SUB, "keyId": one["keyId"], "provider": "anthropic",
        "agent": world.ref().agent, "agentId": world.ref().agent_id, "deviceUid": DEVICE,
        "dekVersion": 1, "grantDigest": "0" * 64, "outcome": "injected",
    })
    rows = {row["keyId"]: row for row in service.list_keys()["keys"]}
    assert rows[one["keyId"]]["lastUsedAt"] == NOW + 5


def test_unreadable_receipts_list_null_last_use(world):
    key = world.login()
    (world.state / "provider-keys" / "receipts.jsonl").mkdir()
    assert world.service().list_keys()["keys"][0]["lastUsedAt"] is None
    assert world.service().list_keys()["keys"][0]["keyId"] == key["keyId"]


def test_orphan_envelope_without_meta_is_invisible(world):
    key = world.login()
    orphan = "pk." + "q" * 26
    world.store.write(storage.key_envelope_path(orphan, 1), "{}", expect_version=None, create_only=True)
    assert [row["keyId"] for row in world.service().list_keys()["keys"]] == [key["keyId"]]


def test_rotate_reseals_with_a_new_dek_and_removes_the_old_envelope(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    old = world.read_json(storage.key_envelope_path(key["keyId"], 1))
    world.clock[0] = NOW + 10
    result = world.service().rotate(key_id=key["keyId"])
    assert result["reissuedGrants"] == 1
    assert result["key"]["dekVersion"] == 2 and result["key"]["updatedAt"] == NOW + 10
    assert world.store.listdir(storage.key_dir(key["keyId"])) == ("envelope-2.json", "meta.json")
    new = world.read_json(storage.key_envelope_path(key["keyId"], 2))
    assert _open(world, new) == SECRET.encode()
    private = _x1_private(world)
    row = old["recipients"][0]
    old_dek = _unwrap(PROVIDER_KEY_PROFILE, row, private, encode(old["header"]))
    with pytest.raises(InvalidTag):
        AESGCM(old_dek).decrypt(base64.b64decode(new["payload"]["nonce"]),
                                base64.b64decode(new["payload"]["ciphertext"]), encode(new["header"]))
    grant = world.service().grants_for(ref)[0]
    assert grant["seq"] == 2 and grant["grant"]["payload"]["dekVersion"] == 2
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 20)
    assert [secret.value for secret in secrets] == [SECRET]


def test_rotate_with_replacement_changes_the_plaintext(world):
    key = world.login()
    result = world.service().rotate(key_id=key["keyId"], replace_secret=OTHER_SECRET)
    assert result == {"key": result["key"], "reissuedGrants": 0}
    envelope = world.read_json(storage.key_envelope_path(key["keyId"], 2))
    assert _open(world, envelope) == OTHER_SECRET.encode()


def test_rotate_and_logout_unknown_key_are_not_found(world):
    missing = "pk." + "z" * 26
    assert _code(lambda: world.service().rotate(key_id=missing)) == ipc_errors.PROVIDER_KEY_NOT_FOUND
    assert _code(lambda: world.service().logout(key_id=missing)) == ipc_errors.PROVIDER_KEY_NOT_FOUND


def test_logout_cascades_revoke_tombstone_and_envelope_removal(world):
    key = world.login()
    other = world.registry.create("helper")
    service = world.service()
    service.allow(agent=world.ref(), provider="anthropic")
    service.allow(agent=world.ref("helper"), provider="anthropic")
    result = world.service().logout(key_id=key["keyId"])
    assert result["keyId"] == key["keyId"] and result["state"] == "deleted"
    assert result["alreadyDeleted"] is False
    assert sorted(row["agentId"] for row in result["revokedGrants"]) == sorted(
        [world.agent.agent_id, other.agent_id])
    assert {row["grantId"] for row in result["revokedGrants"]} == {"anthropic"}
    meta = world.read_json(storage.key_meta_path(key["keyId"]))["payload"]
    assert meta["state"] == "deleted" and meta["dekVersion"] is None and meta["recipients"] == []
    assert world.store.listdir(storage.key_dir(key["keyId"])) == ("meta.json",)
    assert {grant["state"] for grant in world.service().grants_for(world.ref())} == {"revoked"}
    again = world.service().logout(key_id=key["keyId"])
    assert again == {"keyId": key["keyId"], "state": "deleted", "revokedGrants": [], "alreadyDeleted": True}
    assert world.service().list_keys()["keys"] == []
    deleted = world.service().list_keys(include_deleted=True)["keys"]
    assert [(row["keyId"], row["state"], row["dekVersion"]) for row in deleted] == [(key["keyId"], "deleted", None)]
    assert _code(lambda: world.service().allow(agent=world.ref(), provider="anthropic",
                                               key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_NOT_FOUND
    assert _code(lambda: world.service().rotate(key_id=key["keyId"])) == ipc_errors.PROVIDER_KEY_NOT_FOUND


def test_legacy_ref_lookup_finds_only_the_matching_active_key(world):
    key = world.login(provider="deepseek", legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    service = world.service()
    found = service.find_key_by_legacy_ref(entry_id="deepseek", field_name="key")
    assert found is not None and found["keyId"] == key["keyId"]
    assert service.find_key_by_legacy_ref(entry_id="deepseek", field_name="other") is None
    service.logout(key_id=key["keyId"])
    assert world.service().find_key_by_legacy_ref(entry_id="deepseek", field_name="key") is None


def test_awaiting_key_grant_completes_after_matching_login(world):
    ref = world.ref()
    legacy = {"entryId": "deepseek", "fieldName": "key", "revision": 3}
    awaiting = world.service().put_awaiting(agent=ref, grant_id="gui-first-run", provider="deepseek",
                                            environment_names=["DEEPSEEK_API_KEY"], legacy_ref=legacy)
    assert awaiting["changed"] is True
    assert awaiting["grant"]["state"] == "awaiting-key" and awaiting["grant"]["keyId"] is None
    replay = world.service().put_awaiting(agent=ref, grant_id="gui-first-run", provider="deepseek",
                                          environment_names=["DEEPSEEK_API_KEY"], legacy_ref=legacy)
    assert replay["changed"] is False and replay["grant"]["seq"] == 1
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=NOW)) == ipc_errors.PROVIDER_KEY_NOT_FOUND
    world.login(provider="deepseek", secret=SECRET, legacy_ref={"entryId": "deepseek", "fieldName": "other"})
    assert world.service().grants_for(ref)[0]["state"] == "awaiting-key"
    key = world.login(provider="deepseek", secret=OTHER_SECRET,
                      legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    grant = world.service().grants_for(ref)[0]
    assert (grant["state"], grant["seq"], grant["keyId"], grant["legacyRef"]) == ("active", 2, key["keyId"], legacy)
    assert grant["environmentNames"] == ["DEEPSEEK_API_KEY"]
    slots, secrets = world.service().launch_secrets(ref, now_ms=NOW)
    assert slots == frozenset({"gui-first-run"})
    assert [(s.grant_id, s.value, s.environment_names) for s in secrets] == [
        ("gui-first-run", OTHER_SECRET, ("DEEPSEEK_API_KEY",))]


def test_no_plaintext_reaches_any_file_after_the_full_lifecycle(world):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.service().launch_secrets(ref, now_ms=NOW + 1)
    world.service().rotate(key_id=key["keyId"], replace_secret=OTHER_SECRET)
    world.service().launch_secrets(ref, now_ms=NOW + 2)
    world.service().status(agent=ref)
    world.service().list_keys()
    world.service().logout(key_id=key["keyId"])
    world.runtime.close()
    assert world.sentinel_hits(SECRET, OTHER_SECRET) == []


def test_signing_key_without_a_verified_anchor_never_writes(world, tmp_path):
    stranger = bound_signer(tmp_path / "stranger")
    world.app._user_key_signer = stranger
    assert _code(world.login) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    world.anchor.revoked.add(stranger.store.status()["keyId"])
    assert _code(world.login) == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    world.anchor.state = "unavailable"
    assert _code(world.login) == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert world.store.listdir(storage.KEYS_ROOT) == ()


def test_invalid_utf8_secret_refusal_carries_no_secret_context(world):
    bad = SECRET + "\ud800"
    with pytest.raises(DaemonRequestError) as caught:
        world.login(secret=bad)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    key = world.login()
    with pytest.raises(DaemonRequestError) as caught:
        world.service().rotate(key_id=key["keyId"], replace_secret=bad)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert caught.value.__context__ is None
