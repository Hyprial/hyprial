"""Service-grant profile: unsigned, bound to one grant, one recipient device key.

Decision (service grant S2, 2026-11-26): the fixed vector below is built from
raw cryptography calls, not product code, so it pins the wrap domain, the HKDF
inputs and the header encoding independently of the implementation.
"""
import base64
import copy
import dataclasses
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import pytest

from hyprial.identity import UserKey

SECRET = b"SYNTHETIC-SERVICE-GRANT-KEY-NEVER-VALID"
GRANT_ID = "sg." + "abcdefghijklmnopqrstuvwxyz"
BOB_PRIVATE = bytes(range(64, 96))
EVE_PRIVATE = bytes(range(160, 192))
LOW_ORDER = [bytes.fromhex(value) for value in (
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b880",
)]


def b64(value):
    return base64.b64encode(value).decode()


def public(private):
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()


def kid(value):
    return "x25519:" + hashlib.sha256(value).hexdigest()[:32]


def fields(**changes):
    value = dict(org="org-a", grant_id=GRANT_ID, grantor="alice", service_device="device-a",
                 port=8447, recipient_owner="bob", recipient_device="device-b")
    value.update(changes)
    return value


def header_fields(**changes):
    value = dict(fields(), encryption_key_id=kid(public(BOB_PRIVATE)))
    value.update(changes)
    return value


@pytest.fixture
def header(sealed):
    return sealed.service_grant_header(**header_fields())


@pytest.fixture
def envelope(sealed):
    return sealed.seal_service_grant(SECRET, **fields(), recipient_public_key=public(BOB_PRIVATE))


def refused(sealed, code, call, *args, **kwargs):
    with pytest.raises(sealed.ServiceGrantError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code
    assert SECRET.decode() not in str(caught.value)
    return caught.value


def test_header_has_one_canonical_form(sealed, header):
    assert header == {
        "v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "org": "org-a",
        "grantId": GRANT_ID, "service": {"owner": "alice", "deviceId": "device-a", "port": 8447},
        "recipient": {"owner": "bob", "deviceId": "device-b"},
        "encryptionKeyId": kid(public(BOB_PRIVATE)),
    }
    assert UserKey.canonical_bytes(header) == (
        b'{"alg":"A256GCM","encryptionKeyId":"' + kid(public(BOB_PRIVATE)).encode()
        + b'","grantId":"' + GRANT_ID.encode() + b'","org":"org-a",'
        b'"recipient":{"deviceId":"device-b","owner":"bob"},'
        b'"service":{"deviceId":"device-a","owner":"alice","port":8447},'
        b'"v":1,"wrap":"X25519-HKDF-SHA256-A256GCM"}')
    assert sealed.service_grant_header(**header_fields()) == header


def test_round_trip(sealed, header, envelope):
    assert set(envelope) == {"header", "recipients", "payload"}
    assert "sealedBy" not in envelope
    assert envelope["header"] == header
    assert SECRET.decode() not in str(envelope)
    assert sealed.open_service_grant(envelope, **header_fields(), private_key=BOB_PRIVATE) == SECRET


def test_seal_matches_independent_fixed_vector(sealed, header, entropy):
    dek, nonce, ephemeral, wrap_nonce = bytes(range(32)), bytes(range(12)), bytes(range(96, 128)), bytes(range(12, 24))
    entropy([dek, nonce, ephemeral, wrap_nonce])
    result = sealed.seal_service_grant(SECRET, **fields(), recipient_public_key=public(BOB_PRIVATE))
    aad = UserKey.canonical_bytes(header)
    recipient = public(BOB_PRIVATE)
    key_id = kid(recipient)
    epk = public(ephemeral)
    shared = X25519PrivateKey.from_private_bytes(ephemeral).exchange(
        X25519PrivateKey.from_private_bytes(BOB_PRIVATE).public_key())
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient,
               info=b"hyprial/service-grant/wrap/v1\0" + aad + b"\0" + key_id.encode()).derive(shared)
    assert result == {
        "header": header,
        "recipients": [{"recipientKeyId": key_id, "epk": b64(epk), "nonce": b64(wrap_nonce),
                        "wrappedDek": b64(AESGCM(kek).encrypt(wrap_nonce, dek, aad + b"\0" + key_id.encode()))}],
        "payload": {"nonce": b64(nonce), "ciphertext": b64(AESGCM(dek).encrypt(nonce, SECRET, aad))},
    }


def test_profile_is_unsigned_with_its_own_wrap_domain(service_grant_mod):
    profile = service_grant_mod.SERVICE_GRANT
    assert profile.signature is None
    assert profile.wrap_domain == b"hyprial/service-grant/wrap/v1"
    assert profile.reject_weak_recipients is True
    assert profile.header_fields == frozenset(
        {"v", "alg", "wrap", "org", "grantId", "service", "recipient", "encryptionKeyId"})


def test_package_entry_hides_generic_bypass_primitives(sealed):
    for name in ("open", "seal", "verify", "unwrap_check", "unwrap_row", "SERVICE_GRANT"):
        assert not hasattr(sealed, name)
    for name in ("SERVICE_GRANT", "open_service_grant", "service_grant_header", "Profile", "Recipient", "Signature"):
        assert not hasattr(sealed.SealedEnvelope, name)
    assert hasattr(sealed.SealedEnvelope, "seal_service_grant")
    assert hasattr(sealed.SealedEnvelope, "ServiceGrantError")


def test_seal_and_open_refuse_raw_header_dict(sealed, header, envelope):
    with pytest.raises(TypeError):
        sealed.seal_service_grant(SECRET, header=header, recipient_public_key=public(BOB_PRIVATE))
    with pytest.raises(TypeError):
        sealed.open_service_grant(envelope, header=envelope["header"], private_key=BOB_PRIVATE)


def test_seal_derives_encryption_key_id_from_recipient_public_key(sealed, envelope):
    assert envelope["header"]["encryptionKeyId"] == kid(public(BOB_PRIVATE))
    with pytest.raises(TypeError):
        sealed.seal_service_grant(SECRET, **fields(), encryption_key_id=kid(public(EVE_PRIVATE)),
                                  recipient_public_key=public(BOB_PRIVATE))


def test_envelope_header_fields_cannot_open_ciphertext_moved_to_another_grantor(sealed, envelope):
    moved = copy.deepcopy(envelope)
    moved["header"]["service"]["owner"] = "mallory"
    claimed = moved["header"]
    refused(
        sealed, "PAYLOAD_INVALID", sealed.open_service_grant, moved,
        org=claimed["org"], grant_id=claimed["grantId"], grantor=claimed["service"]["owner"],
        service_device=claimed["service"]["deviceId"], port=claimed["service"]["port"],
        recipient_owner=claimed["recipient"]["owner"], recipient_device=claimed["recipient"]["deviceId"],
        encryption_key_id=claimed["encryptionKeyId"], private_key=BOB_PRIVATE,
    )


@pytest.mark.parametrize("name,value", [
    ("org", "org-b"), ("grant_id", "sg." + "b" * 26), ("grantor", "mallory"),
    ("service_device", "device-c"), ("port", 8448), ("recipient_owner", "carol"),
    ("recipient_device", "device-c"),
])
def test_another_grant_document_cannot_open_the_ciphertext(sealed, envelope, name, value):
    other_fields = header_fields(**{name: value})
    other = sealed.service_grant_header(**other_fields)
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, **other_fields,
            private_key=BOB_PRIVATE)
    moved = copy.deepcopy(envelope)
    moved["header"] = other
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, moved, **other_fields,
            private_key=BOB_PRIVATE)


@pytest.mark.parametrize("path", [
    ("header", "org"), ("header", "grantId"), ("header", "service", "owner"),
    ("header", "service", "deviceId"), ("header", "service", "port"), ("header", "recipient", "owner"),
    ("header", "recipient", "deviceId"), ("header", "encryptionKeyId"), ("payload", "nonce"),
    ("payload", "ciphertext"), ("recipients", 0, "epk"), ("recipients", 0, "nonce"),
    ("recipients", 0, "wrappedDek"), ("recipients", 0, "recipientKeyId"),
])
def test_tampered_envelope_is_refused(sealed, envelope, path):
    value = copy.deepcopy(envelope)
    node = value
    for part in path[:-1]:
        node = node[part]
    old = node[path[-1]]
    if type(old) is int:
        node[path[-1]] = old + 1
    elif path[-1] == "recipientKeyId":
        node[path[-1]] = kid(public(EVE_PRIVATE))
    elif path[0] == "header":
        node[path[-1]] = old + "x"
    else:
        data = bytearray(base64.b64decode(old) or b"\0")
        data[0] ^= 1
        node[path[-1]] = base64.b64encode(bytes(data)).decode()
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, **header_fields(),
            private_key=BOB_PRIVATE)


def test_another_recipient_key_cannot_open(sealed, envelope):
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, **header_fields(),
            private_key=EVE_PRIVATE)
    eve_fields = header_fields(encryption_key_id=kid(public(EVE_PRIVATE)))
    eve_header = sealed.service_grant_header(**eve_fields)
    relabelled = copy.deepcopy(envelope)
    relabelled["header"] = eve_header
    relabelled["recipients"][0]["recipientKeyId"] = eve_header["encryptionKeyId"]
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, relabelled, **eve_fields,
            private_key=EVE_PRIVATE)


def test_service_grant_envelope_does_not_open_as_provider_key(provider, envelope, materials):
    _, owner, _ = materials
    for call in (provider.verify_envelope, provider.open_envelope):
        kwargs = {"owner_key": owner}
        if call is provider.open_envelope:
            kwargs.update(recipient_key_id=kid(public(BOB_PRIVATE)), private_key=BOB_PRIVATE)
        with pytest.raises(provider.Error):
            call(envelope, **kwargs)
    signed = {**envelope, "sealedBy": {"keyId": "ed25519:" + "0" * 32, "sig": "AA=="}}
    with pytest.raises(provider.Error):
        provider.open_envelope(signed, owner_key=owner, recipient_key_id=kid(public(BOB_PRIVATE)),
                               private_key=BOB_PRIVATE)


def test_provider_key_envelope_does_not_open_as_service_grant(sealed, vector):
    row = vector["recipients"][0]
    private = base64.b64decode(row["private"])
    target = header_fields(encryption_key_id=row["keyId"])
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, vector["envelope"], **target,
            private_key=private)
    stripped = {key: vector["envelope"][key] for key in ("header", "recipients", "payload")}
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, stripped, **target,
            private_key=private)


def test_transplanted_provider_key_row_and_payload_fails_crypto_check(
    sealed, sealed_core, service_grant_mod, vector,
):
    from hyprial.identity.impl.provider_keys import envelope as provider_envelope

    row = vector["recipients"][0]
    private = base64.b64decode(row["private"])
    target = header_fields(encryption_key_id=row["keyId"])
    transplanted = {
        "header": sealed.service_grant_header(**target),
        "recipients": [vector["envelope"]["recipients"][0]],
        "payload": vector["envelope"]["payload"],
    }
    assert sealed_core.verify(service_grant_mod.SERVICE_GRANT, transplanted) == transplanted
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, transplanted, **target, private_key=private)
    same_domain = dataclasses.replace(service_grant_mod.SERVICE_GRANT,
                                      wrap_domain=provider_envelope.PROFILE.wrap_domain)
    refused(sealed, "PAYLOAD_INVALID", sealed_core.open, same_domain, transplanted,
            recipient_key_id=row["keyId"], private_key=private)


def test_wrap_domain_separates_profiles(sealed, sealed_core, service_grant_mod, header, envelope):
    from hyprial.identity.impl.provider_keys import envelope as provider_envelope

    assert service_grant_mod.SERVICE_GRANT.wrap_domain != provider_envelope.PROFILE.wrap_domain
    swapped = dataclasses.replace(service_grant_mod.SERVICE_GRANT,
                                  wrap_domain=provider_envelope.PROFILE.wrap_domain)
    with pytest.raises(sealed.ServiceGrantError) as caught:
        sealed_core.open(swapped, envelope, recipient_key_id=header["encryptionKeyId"], private_key=BOB_PRIVATE)
    assert caught.value.code == "PAYLOAD_INVALID"


@pytest.mark.parametrize("value", LOW_ORDER, ids=lambda value: value.hex()[:8] + value.hex()[-2:])
def test_low_order_recipient_keys_are_refused_at_seal(sealed, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, **fields(),
            recipient_public_key=value)


@pytest.mark.parametrize("value", [b"", bytes(31), public(BOB_PRIVATE)[:31], public(BOB_PRIVATE) + b"\0",
                                   public(BOB_PRIVATE).hex(), None, bytearray(public(BOB_PRIVATE))])
def test_malformed_recipient_keys_are_refused_at_seal(sealed, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, **fields(),
            recipient_public_key=value)


@pytest.mark.parametrize("secret", [b"", b"x" * 16385, "text", None], ids=["empty", "too-long", "str", "none"])
def test_secret_bounds_are_parameter_errors(sealed, secret):
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, secret, **fields(),
            recipient_public_key=public(BOB_PRIVATE))


@pytest.mark.parametrize("name,value", [
    ("org", ""), ("org", 7), ("org", "a\0b"), ("org", "a\nb"), ("org", "."), ("org", ".."),
    ("org", "a\\b"), ("org", "a/b"), ("org", "@org"), ("org", "a\x7fb"), ("org", "x" * 257),
    ("grant_id", "sg.short"), ("grant_id", "sg." + "A" * 26), ("grant_id", "xx." + "a" * 26),
    ("grantor", "al/ice"), ("grantor", "al\\ice"), ("grantor", "."), ("grantor", ".."),
    ("grantor", "user:alice"), ("grantor", "alice:1"),
    ("service_device", ""), ("service_device", "."), ("service_device", ".."),
    ("service_device", "dev\\a"), ("service_device", "dev/a"),
    ("port", 0), ("port", 65536), ("port", True), ("port", "8447"),
    ("recipient_owner", "@members"), ("recipient_owner", "user:bob"), ("recipient_owner", "bob:extra"),
    ("recipient_owner", "."), ("recipient_owner", ".."), ("recipient_owner", "b\\ob"),
    ("recipient_device", None), ("recipient_device", "."), ("recipient_device", ".."),
    ("recipient_device", "dev\\b"), ("recipient_device", "dev/b"),
    ("encryption_key_id", "x25519:" + "0" * 31), ("encryption_key_id", "ed25519:" + "0" * 32),
    ("encryption_key_id", "x25519:" + "A" * 32),
])
def test_header_fields_are_validated(sealed, name, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.service_grant_header, **header_fields(**{name: value}))
    if name != "encryption_key_id":
        refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET,
                **fields(**{name: value}), recipient_public_key=public(BOB_PRIVATE))


@pytest.mark.parametrize("name,value", [
    ("org", ".."), ("grantor", "user:alice"), ("service_device", "dev\\a"),
    ("recipient_owner", "user:bob"), ("recipient_device", "."), ("encryption_key_id", "bad"),
])
def test_open_validates_structured_header_fields(sealed, envelope, name, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.open_service_grant, envelope,
            **header_fields(**{name: value}), private_key=BOB_PRIVATE)


@pytest.mark.parametrize("path,bad", [
    (("service", "owner"), "user:alice"),
    (("recipient", "owner"), "user:bob"),
    (("service", "deviceId"), ".."),
    (("recipient", "deviceId"), "dev\\b"),
    (("org",), "."),
])
def test_envelope_with_invalid_owner_or_path_component_header_is_refused(
    sealed, sealed_core, service_grant_mod, envelope, path, bad,
):
    value = copy.deepcopy(envelope)
    node = value["header"]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = bad
    refused(sealed, "PAYLOAD_INVALID", sealed_core.verify, service_grant_mod.SERVICE_GRANT, value)


def test_open_path_argument_error_does_not_say_sealing(sealed, envelope):
    error = refused(sealed, "INVALID_ARGUMENT", sealed.open_service_grant, envelope,
                    **header_fields(grantor="user:alice"), private_key=BOB_PRIVATE)
    assert "sealing" not in str(error).lower()
    assert "user:alice" not in str(error)


@pytest.mark.parametrize("location", [(), ("header",), ("payload",), ("recipients", 0)])
def test_unknown_envelope_fields_are_refused(sealed, envelope, location):
    value = copy.deepcopy(envelope)
    node = value
    for part in location:
        node = node[part]
    node["extra"] = "SYNTHETIC-SENTINEL"
    error = refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, **header_fields(),
                    private_key=BOB_PRIVATE)
    assert "SYNTHETIC-SENTINEL" not in str(error)


def test_extra_recipient_rows_are_refused(sealed, envelope):
    value = copy.deepcopy(envelope)
    value["recipients"].append({**value["recipients"][0], "recipientKeyId": kid(public(EVE_PRIVATE))})
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, **header_fields(),
            private_key=BOB_PRIVATE)


@pytest.mark.parametrize("private", [b"", bytes(31), "text", None], ids=["empty", "short", "str", "none"])
def test_malformed_private_key_is_refused(sealed, envelope, private):
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, **header_fields(),
            private_key=private)


def test_fresh_randomness_per_seal(sealed):
    first = sealed.seal_service_grant(SECRET, **fields(), recipient_public_key=public(BOB_PRIVATE))
    second = sealed.seal_service_grant(SECRET, **fields(), recipient_public_key=public(BOB_PRIVATE))
    assert first["payload"] != second["payload"]
    assert first["recipients"][0]["epk"] != second["recipients"][0]["epk"]
