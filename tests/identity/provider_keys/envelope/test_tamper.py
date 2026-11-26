"""Signed header, ciphertext, recipient and purpose separation negatives."""
import base64
from copy import deepcopy
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from hyprial.identity import UserKey


PATHS = [("header", field) for field in (
    "v", "alg", "wrap", "provider", "keyId", "owner", "dekVersion", "createdAt",
)] + [("payload", field) for field in ("nonce", "ciphertext")] + [
    ("recipients", index, field) for index in (0, 1)
    for field in ("recipientKeyId", "epk", "nonce", "wrappedDek")
] + [("sealedBy", field) for field in ("keyId", "sig")]


def resign(value, vector, purpose=b"hyprial/provider-key/envelope/v1"):
    unsigned = {key: value[key] for key in ("header", "recipients", "payload")}
    raw = purpose + b"\0" + hashlib.sha256(UserKey.canonical_bytes(unsigned)).digest()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(vector["ownerPrivate"]))
    value["sealedBy"]["sig"] = base64.b64encode(key.sign(raw)).decode()
    return value


@pytest.mark.parametrize("path", PATHS)
def test_every_field_tamper_is_refused(core, vector, materials, path):
    value = deepcopy(vector["envelope"])
    node = value
    for part in path[:-1]:
        node = node[part]
    old = node[path[-1]]
    node[path[-1]] = old + 1 if type(old) is int else "tampered-" + old
    with pytest.raises(core.Error) as caught:
        core.verify_envelope(value, owner_key=materials[1])
    assert caught.value.code in {"PROVIDER_KEY_SIGNATURE_INVALID", "PROVIDER_KEY_UNSEAL_FAILED"}


@pytest.mark.parametrize("location", [(), ("header",), ("payload",), ("sealedBy",), ("recipients", 0)])
def test_unknown_fields_are_refused(core, vector, materials, location):
    value = deepcopy(vector["envelope"])
    node = value
    for part in location:
        node = node[part]
    node["secret"] = "SYNTHETIC-SENTINEL"
    with pytest.raises(core.Error) as caught:
        core.verify_envelope(value, owner_key=materials[1])
    assert "SYNTHETIC-SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("field,value", [("v", 2), ("v", True), ("alg", "none"), ("wrap", "none"),
                                          ("dekVersion", 0), ("createdAt", -1), ("createdAt", 1.5)])
def test_even_validly_signed_unsupported_header_is_refused(core, vector, materials, field, value):
    changed = deepcopy(vector["envelope"])
    changed["header"][field] = value
    if type(value) is not float:
        resign(changed, vector)
    with pytest.raises(core.Error):
        core.verify_envelope(changed, owner_key=materials[1])


def test_purpose_swapping_is_refused(core, vector, materials):
    swapped = resign(deepcopy(vector["envelope"]), vector, b"hyprial/provider-key/grant/v1")
    with pytest.raises(core.Error) as caught:
        core.verify_envelope(swapped, owner_key=materials[1])
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_recipient_wrap_cannot_move_to_another_name_even_after_resigning(core, vector, materials):
    changed = deepcopy(vector["envelope"])
    a, b = changed["recipients"]
    for field in ("epk", "nonce", "wrappedDek"):
        b[field] = a[field]
    resign(changed, vector)
    recipient = vector["recipients"][1]
    with pytest.raises(core.Error) as caught:
        core.open_envelope(changed, owner_key=materials[1], recipient_key_id=recipient["keyId"],
                           private_key=base64.b64decode(recipient["private"]))
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"


def test_owner_key_kind_and_owner_are_external_trust_not_self_asserted(core, vector, materials):
    for owner in (core.OwnerKey("other-owner", vector["ownerKeyId"], base64.b64decode(vector["ownerPublic"])),
                  core.OwnerKey("test-sub", vector["ownerKeyId"], base64.b64decode(vector["ownerPublic"]), key_kind="device")):
        with pytest.raises(core.Error) as caught:
            core.verify_envelope(vector["envelope"], owner_key=owner)
        assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_signature_is_checked_before_unwrap(core, vector, materials, monkeypatch):
    changed = deepcopy(vector["envelope"])
    changed["sealedBy"]["sig"] = base64.b64encode(bytes(64)).decode()
    def no_decrypt(*args, **kwargs):
        pytest.fail("must verify before any decrypt")
    monkeypatch.setattr("hyprial.identity.impl.sealed_envelope.core._unwrap", no_decrypt)
    row = vector["recipients"][0]
    with pytest.raises(core.Error) as caught:
        core.open_envelope(changed, owner_key=materials[1], recipient_key_id=row["keyId"],
                           private_key=base64.b64decode(row["private"]))
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_low_order_recipient_is_refused_and_errors_are_secret_free(core, vector, materials):
    low_key = "x25519:" + hashlib.sha256(bytes(32)).hexdigest()[:32]
    with pytest.raises(core.Error) as caught:
        core.seal(b"SYNTHETIC-SENTINEL", header=vector["envelope"]["header"], signer=materials[0],
                  recipients=[core.Recipient(low_key, bytes(32))])
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"
    assert "SYNTHETIC-SENTINEL" not in str(caught.value)
    assert vector["ownerPrivate"] not in str(caught.value)


@pytest.mark.parametrize("path", PATHS)
def test_missing_fields_are_refused(core, vector, materials, path):
    value = deepcopy(vector["envelope"])
    node = value
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    with pytest.raises(core.Error):
        core.verify_envelope(value, owner_key=materials[1])


@pytest.mark.parametrize("field", ["provider", "keyId", "owner", "dekVersion", "createdAt"])
def test_resigned_header_still_binds_both_aead_layers(core, vector, materials, field):
    value = deepcopy(vector["envelope"])
    old = value["header"][field]
    value["header"][field] = old + 1 if type(old) is int else old + "-changed"
    resign(value, vector)
    owner_key = materials[1]
    if field == "owner":
        owner_key = core.OwnerKey(value["header"]["owner"], owner_key.key_id, owner_key.public_key)
    row = vector["recipients"][0]
    with pytest.raises(core.Error) as caught:
        core.open_envelope(value, owner_key=owner_key, recipient_key_id=row["keyId"],
                           private_key=base64.b64decode(row["private"]))
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"


@pytest.mark.parametrize("part", ["epk", "nonce", "wrappedDek", "payload"])
def test_valid_signature_cannot_make_corrupt_ciphertext_decrypt(core, vector, materials, part):
    value = deepcopy(vector["envelope"])
    if part == "payload":
        raw = bytearray(base64.b64decode(value["payload"]["ciphertext"]))
        raw[0] ^= 1
        value["payload"]["ciphertext"] = base64.b64encode(raw).decode()
    else:
        size = {"epk": 32, "nonce": 12, "wrappedDek": 48}[part]
        value["recipients"][0][part] = base64.b64encode(bytes(size)).decode()
    resign(value, vector)
    row = vector["recipients"][0]
    with pytest.raises(core.Error) as caught:
        core.open_envelope(value, owner_key=materials[1], recipient_key_id=row["keyId"],
                           private_key=base64.b64decode(row["private"]))
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"


def test_duplicate_recipients_cannot_ambiguously_select_wraps(core, vector, materials):
    value = deepcopy(vector["envelope"])
    value["recipients"].append(deepcopy(value["recipients"][0]))
    resign(value, vector)
    with pytest.raises(core.Error):
        core.verify_envelope(value, owner_key=materials[1])
    with pytest.raises(core.Error):
        core.seal(b"synthetic", header=value["header"], signer=materials[0],
                  recipients=[materials[2][0], materials[2][0]])


@pytest.mark.parametrize("bad", [None, [], "not-an-object", {"extra": "SENTINEL" * 10000}])
def test_invalid_or_oversized_input_is_sanitized(core, materials, bad):
    with pytest.raises(core.Error) as caught:
        core.verify_envelope(bad, owner_key=materials[1])
    assert "SENTINEL" not in str(caught.value)
