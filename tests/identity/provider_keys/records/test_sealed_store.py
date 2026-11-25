"""X1 store custody: open/probe/reseal with this device's key, never exporting it."""
import copy
import json

import pytest

import hyprial.identity as identity
from hyprial.identity.impl.provider_keys import models
from hyprial.kernel import ipc_errors
from record_vectors import KEY_ID, OWNER

SECRET = b"SYNTHETIC-SEALED-SECRET-NEVER-VALID"


def store(tmp_path, device="device-a"):
    return identity.EncryptionKey.Store(state_dir=tmp_path / "state", kind="user", casdoor_sub=OWNER,
                                        device_uid=device, fingerprint="machine-" + device)


def header(**changes):
    value = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "provider": "synthetic",
             "keyId": KEY_ID, "owner": OWNER, "dekVersion": 1, "createdAt": 1000}
    value.update(changes)
    return value


def private_text(keys):
    return json.loads((keys.root / "key.json").read_text())["privateKey"]


def resigned(envelope, signer):
    unsigned = {name: envelope[name] for name in ("header", "recipients", "payload")}
    return {**unsigned, "sealedBy": models.sign(unsigned, signer, models.ENVELOPE_PURPOSE, owner=OWNER)}


def flip(value):
    raw = bytearray(models.unb64(value))
    raw[0] ^= 1
    return models.b64(bytes(raw))


def test_recipient_requires_an_active_local_key(tmp_path, core):
    keys = store(tmp_path)
    with pytest.raises(identity.UserKey.Error) as raised:
        keys.recipient()
    assert raised.value.code == ipc_errors.USER_KEY_ABSENT
    public = keys.ensure()
    recipient = keys.recipient()
    assert isinstance(recipient, core.Recipient)
    assert recipient.key_id == public["keyId"]
    assert models.b64(recipient.public_key) == public["publicKey"]
    keys.revoke()
    with pytest.raises(identity.UserKey.Error) as raised:
        keys.recipient()
    assert raised.value.code == ipc_errors.USER_KEY_UNBOUND


def test_open_probe_reseal_round_trip(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    assert keys.probe_sealed(copy.deepcopy(envelope), owner_key=owner_key) is True
    assert keys.open_sealed(envelope, owner_key=owner_key) == SECRET
    other = store(tmp_path, "device-b")
    other.ensure()
    resealed = keys.reseal_sealed(envelope, owner_key=owner_key, signer=signer,
                                  recipients=[keys.recipient(), other.recipient()], created_at=2000)
    assert resealed["header"]["dekVersion"] == 2 and resealed["header"]["createdAt"] == 2000
    assert {row["recipientKeyId"] for row in resealed["recipients"]} == {
        keys.recipient().key_id, other.recipient().key_id}
    for holder in (keys, other):
        assert holder.probe_sealed(resealed, owner_key=owner_key) is True
        assert holder.open_sealed(resealed, owner_key=owner_key) == SECRET
    with pytest.raises(core.Error):
        keys.reseal_sealed(envelope, owner_key=owner_key, signer=signer,
                           recipients=[keys.recipient()], created_at=999)


def test_wrong_device_is_recipient_pending(tmp_path, core, signer, owner_key):
    keys, other = store(tmp_path), store(tmp_path, "device-b")
    keys.ensure()
    other.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    for call in (other.open_sealed, other.probe_sealed):
        with pytest.raises(core.Error) as raised:
            call(envelope, owner_key=owner_key)
        assert raised.value.code == ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING


def test_tampered_wrap_is_unseal_failed_for_open_and_probe(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    for name in ("wrappedDek", "epk", "nonce"):
        tampered = copy.deepcopy(envelope)
        tampered["recipients"][0][name] = flip(tampered["recipients"][0][name])
        tampered = resigned(tampered, signer)
        for call in (keys.open_sealed, keys.probe_sealed):
            with pytest.raises(core.Error) as raised:
                call(tampered, owner_key=owner_key)
            assert raised.value.code == ipc_errors.PROVIDER_KEY_UNSEAL_FAILED, name


def test_unsigned_tamper_raises_and_never_returns_false(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    for path in (("payload", "ciphertext"), ("header", "provider")):
        tampered = copy.deepcopy(envelope)
        tampered[path[0]][path[1]] = flip(envelope["payload"]["ciphertext"]) if path[0] == "payload" else "x"
        for call in (keys.open_sealed, keys.probe_sealed):
            with pytest.raises(core.Error) as raised:
                call(tampered, owner_key=owner_key)
            assert raised.value.code == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_probe_unwraps_without_decrypting_payload(tmp_path, core, signer, owner_key, monkeypatch):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    corrupted = copy.deepcopy(envelope)
    corrupted["payload"]["ciphertext"] = flip(envelope["payload"]["ciphertext"])
    corrupted = resigned(corrupted, signer)
    with pytest.raises(core.Error) as raised:
        keys.open_sealed(corrupted, owner_key=owner_key)
    assert raised.value.code == ipc_errors.PROVIDER_KEY_UNSEAL_FAILED
    decrypted = []
    from cryptography.hazmat.primitives.ciphers import aead
    original = aead.AESGCM.decrypt

    def spy(self, nonce, data, associated):
        decrypted.append(len(data))
        return original(self, nonce, data, associated)
    monkeypatch.setattr(aead.AESGCM, "decrypt", spy)
    assert keys.probe_sealed(corrupted, owner_key=owner_key) is True
    assert decrypted == [48]


def test_revoked_or_missing_key_refuses_with_store_error(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    fresh = store(tmp_path, "device-c")
    with pytest.raises(identity.UserKey.Error) as raised:
        fresh.open_sealed(envelope, owner_key=owner_key)
    assert raised.value.code == ipc_errors.USER_KEY_ABSENT
    keys.revoke()
    for call in (keys.open_sealed, keys.probe_sealed):
        with pytest.raises(identity.UserKey.Error) as raised:
            call(envelope, owner_key=owner_key)
        assert raised.value.code == ipc_errors.USER_KEY_UNBOUND


def test_private_key_never_in_repr_or_errors(tmp_path, core, signer, owner_key, caplog):
    keys = store(tmp_path)
    keys.ensure()
    private = private_text(keys)
    raw = models.unb64(private, 32)
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    texts = [repr(keys), repr(keys.recipient()), str(vars(keys))]
    bad = copy.deepcopy(envelope)
    bad["recipients"][0]["wrappedDek"] = flip(bad["recipients"][0]["wrappedDek"])
    bad = resigned(bad, signer)
    for call, value in ((keys.open_sealed, bad), (keys.probe_sealed, bad),
                        (keys.open_sealed, {"header": private}), (keys.probe_sealed, None)):
        with pytest.raises(core.Error) as raised:
            call(value, owner_key=owner_key)
        texts += [str(raised.value), repr(raised.value), repr(raised.value.__cause__),
                  repr(raised.value.__context__)]
    resealed = keys.reseal_sealed(envelope, owner_key=owner_key, signer=signer,
                                  recipients=[keys.recipient()], created_at=1000)
    texts += [json.dumps(resealed), caplog.text]
    blob = "\n".join(texts)
    assert private not in blob
    assert raw.hex() not in blob
    assert models.b64(raw) not in blob
    assert SECRET.decode() not in json.dumps(resealed)


def test_host_store_is_never_used_for_sealed_custody(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    host = identity.EncryptionKey.Store(state_dir=tmp_path / "state", kind="host", casdoor_sub=None,
                                        device_uid="device-a", fingerprint="machine-device-a")
    host.ensure()
    calls = (host.recipient, lambda: host.open_sealed(envelope, owner_key=owner_key),
             lambda: host.probe_sealed(envelope, owner_key=owner_key),
             lambda: host.reseal_sealed(envelope, owner_key=owner_key, signer=signer,
                                        recipients=[keys.recipient()], created_at=2000))
    for call in calls:
        with pytest.raises(identity.UserKey.Error) as raised:
            call()
        assert raised.value.code == ipc_errors.USER_KEY_UNBOUND


def test_owner_key_for_another_sub_is_refused(tmp_path, core, signer, owner_key):
    keys = store(tmp_path)
    keys.ensure()
    envelope = core.seal(SECRET, header=header(), recipients=[keys.recipient()], signer=signer)
    foreign = identity.EncryptionKey.Store(state_dir=tmp_path / "state", kind="user",
                                           casdoor_sub="other-sub", device_uid="device-a",
                                           fingerprint="machine-device-a")
    foreign.ensure()
    shared = keys.recipient()
    foreign.root.joinpath("key.json").write_text(keys.root.joinpath("key.json").read_text().replace(
        f'"casdoorSub": "{OWNER}"', '"casdoorSub": "other-sub"').replace(
        f'"casdoorSub":"{OWNER}"', '"casdoorSub":"other-sub"'))
    assert foreign.recipient() == shared
    calls = (lambda: foreign.open_sealed(envelope, owner_key=owner_key),
             lambda: foreign.probe_sealed(envelope, owner_key=owner_key),
             lambda: foreign.reseal_sealed(envelope, owner_key=owner_key, signer=signer,
                                           recipients=[shared], created_at=2000))
    for call in calls:
        with pytest.raises(identity.UserKey.Error) as raised:
            call()
        assert raised.value.code == ipc_errors.USER_KEY_UNBOUND
    for call in (keys.open_sealed, keys.probe_sealed):
        for anchor in (None, core.OwnerKey("other-sub", owner_key.key_id, owner_key.public_key)):
            with pytest.raises(identity.UserKey.Error) as raised:
                call(envelope, owner_key=anchor)
            assert raised.value.code == ipc_errors.USER_KEY_UNBOUND
