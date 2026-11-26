"""Opening a service grant with this device's local key, without an owner key."""
import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

import hyprial.identity as identity
from hyprial.kernel import DaemonRequestError, ipc_errors

SECRET = b"SYNTHETIC-SERVICE-GRANT-KEY-NEVER-VALID"
GRANT_ID = "sg." + "abcdefghijklmnopqrstuvwxyz"


def store(tmp_path, device="device-b", kind="user"):
    return identity.EncryptionKey.Store(
        state_dir=tmp_path / "state", kind=kind, casdoor_sub="bob" if kind == "user" else None,
        device_uid=device, fingerprint="machine-" + device)


def grant(sealed, public):
    header = sealed.service_grant_header(
        org="org-a", grant_id=GRANT_ID, grantor="alice", service_device="device-a", port=8447,
        recipient_owner="bob", recipient_device="device-b", encryption_key_id=public["keyId"])
    envelope = sealed.seal_service_grant(SECRET, header=header,
                                         recipient_public_key=base64.b64decode(public["publicKey"]))
    return header, envelope


def code_of(call, *args, **kwargs):
    with pytest.raises(DaemonRequestError) as caught:
        call(*args, **kwargs)
    assert SECRET.decode() not in str(caught.value)
    return caught.value.code


def test_the_right_key_opens(tmp_path, sealed):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    assert keys.open_service_grant(envelope, header=header) == SECRET


def test_another_device_key_fails(tmp_path, sealed):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    other = store(tmp_path, device="device-c")
    other.ensure()
    assert code_of(other.open_service_grant, envelope, header=header) == ipc_errors.PAYLOAD_INVALID
    alice = store(tmp_path / "alice", device="device-b")
    alice.ensure()
    assert code_of(alice.open_service_grant, envelope, header=header) == ipc_errors.PAYLOAD_INVALID


def test_absent_revoked_and_host_keys_refuse(tmp_path, sealed):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    fresh = store(tmp_path / "fresh")
    assert code_of(fresh.open_service_grant, envelope, header=header) == ipc_errors.USER_KEY_ABSENT
    host = store(tmp_path / "host", kind="host")
    host.ensure()
    assert code_of(host.open_service_grant, envelope, header=header) == ipc_errors.USER_KEY_UNBOUND
    keys.revoke()
    assert code_of(keys.open_service_grant, envelope, header=header) == ipc_errors.USER_KEY_UNBOUND


def test_open_sealed_still_requires_an_owner_key(tmp_path, sealed):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    with pytest.raises(TypeError):
        keys.open_sealed(envelope)
    assert code_of(keys.open_sealed, envelope, owner_key=None) == ipc_errors.USER_KEY_UNBOUND
    other_owner = identity.ProviderKeys.OwnerKey("alice", "ed25519:" + "0" * 32, bytes(32))
    assert code_of(keys.open_sealed, envelope, owner_key=other_owner) == ipc_errors.USER_KEY_UNBOUND


def test_open_sealed_refuses_a_service_grant_and_vice_versa(tmp_path, sealed, vector):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    private = base64.b64decode(vector["ownerPrivate"])
    signer = identity.ProviderKeys.SigningKey("bob", private)
    public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    owner_key = identity.ProviderKeys.OwnerKey("bob", vector["ownerKeyId"], public)
    assert code_of(keys.open_sealed, envelope, owner_key=owner_key) in {
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID, ipc_errors.PROVIDER_KEY_UNSEAL_FAILED}
    provider_header = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM",
                       "provider": "synthetic", "keyId": "pk_test", "owner": "bob",
                       "dekVersion": 1, "createdAt": 1000}
    provider_envelope = identity.ProviderKeys.seal(SECRET, header=provider_header,
                                                   recipients=[keys.recipient()], signer=signer)
    assert keys.open_sealed(provider_envelope, owner_key=owner_key) == SECRET
    assert code_of(keys.open_service_grant, provider_envelope, header=header) == ipc_errors.PAYLOAD_INVALID


def test_key_material_never_leaves_through_the_entry(tmp_path, sealed):
    keys = store(tmp_path)
    header, envelope = grant(sealed, keys.ensure())
    private = json.loads((keys.root / "key.json").read_text())["privateKey"]
    tampered = json.loads(json.dumps(envelope))
    tampered["payload"]["ciphertext"] = base64.b64encode(b"\0" * 64).decode()
    with pytest.raises(DaemonRequestError) as caught:
        keys.open_service_grant(tampered, header=header)
    assert private not in str(caught.value) and private not in repr(caught.value)
