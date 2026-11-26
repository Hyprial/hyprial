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


def public(private):
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()


def kid(value):
    return "x25519:" + hashlib.sha256(value).hexdigest()[:32]


def fields(**changes):
    value = dict(org="org-a", grant_id=GRANT_ID, grantor="alice", service_device="device-a",
                 port=8447, recipient_owner="bob", recipient_device="device-b",
                 encryption_key_id=kid(public(BOB_PRIVATE)))
    value.update(changes)
    return value


@pytest.fixture
def header(sealed):
    return sealed.service_grant_header(**fields())


@pytest.fixture
def envelope(sealed, header):
    return sealed.seal_service_grant(SECRET, header=header, recipient_public_key=public(BOB_PRIVATE))


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
    assert sealed.service_grant_header(**fields()) == header


def test_round_trip(sealed, header, envelope):
    assert set(envelope) == {"header", "recipients", "payload"}
    assert "sealedBy" not in envelope
    assert envelope["header"] == header
    assert SECRET.decode() not in str(envelope)
    assert sealed.open_service_grant(envelope, header=header, private_key=BOB_PRIVATE) == SECRET


def test_seal_matches_independent_fixed_vector(sealed, header, entropy):
    dek, nonce, ephemeral, wrap_nonce = bytes(range(32)), bytes(range(12)), bytes(range(96, 128)), bytes(range(12, 24))
    entropy([dek, nonce, ephemeral, wrap_nonce])
    result = sealed.seal_service_grant(SECRET, header=header, recipient_public_key=public(BOB_PRIVATE))
    aad = UserKey.canonical_bytes(header)
    recipient = public(BOB_PRIVATE)
    key_id = kid(recipient)
    epk = public(ephemeral)
    shared = X25519PrivateKey.from_private_bytes(ephemeral).exchange(
        X25519PrivateKey.from_private_bytes(BOB_PRIVATE).public_key())
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient,
               info=b"hyprial/service-grant/wrap/v1\0" + aad + b"\0" + key_id.encode()).derive(shared)
    b64 = lambda value: base64.b64encode(value).decode()
    assert result == {
        "header": header,
        "recipients": [{"recipientKeyId": key_id, "epk": b64(epk), "nonce": b64(wrap_nonce),
                        "wrappedDek": b64(AESGCM(kek).encrypt(wrap_nonce, dek, aad + b"\0" + key_id.encode()))}],
        "payload": {"nonce": b64(nonce), "ciphertext": b64(AESGCM(dek).encrypt(nonce, SECRET, aad))},
    }


def test_profile_is_unsigned_with_its_own_wrap_domain(sealed):
    profile = sealed.SERVICE_GRANT
    assert profile.signature is None
    assert profile.wrap_domain == b"hyprial/service-grant/wrap/v1"
    assert profile.header_fields == frozenset(
        {"v", "alg", "wrap", "org", "grantId", "service", "recipient", "encryptionKeyId"})


@pytest.mark.parametrize("name,value", [
    ("org", "org-b"), ("grant_id", "sg." + "b" * 26), ("grantor", "mallory"),
    ("service_device", "device-c"), ("port", 8448), ("recipient_owner", "carol"),
    ("recipient_device", "device-c"),
])
def test_another_grant_document_cannot_open_the_ciphertext(sealed, envelope, name, value):
    other = sealed.service_grant_header(**fields(**{name: value}))
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, header=other,
            private_key=BOB_PRIVATE)
    moved = copy.deepcopy(envelope)
    moved["header"] = other
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, moved, header=other,
            private_key=BOB_PRIVATE)


@pytest.mark.parametrize("path", [
    ("header", "org"), ("header", "grantId"), ("header", "service", "owner"),
    ("header", "service", "deviceId"), ("header", "service", "port"), ("header", "recipient", "owner"),
    ("header", "recipient", "deviceId"), ("header", "encryptionKeyId"), ("payload", "nonce"),
    ("payload", "ciphertext"), ("recipients", 0, "epk"), ("recipients", 0, "nonce"),
    ("recipients", 0, "wrappedDek"), ("recipients", 0, "recipientKeyId"),
])
def test_tampered_envelope_is_refused(sealed, header, envelope, path):
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
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, header=header,
            private_key=BOB_PRIVATE)


def test_another_recipient_key_cannot_open(sealed, header, envelope):
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, header=header,
            private_key=EVE_PRIVATE)
    eve_header = sealed.service_grant_header(**fields(encryption_key_id=kid(public(EVE_PRIVATE))))
    relabelled = copy.deepcopy(envelope)
    relabelled["header"] = eve_header
    relabelled["recipients"][0]["recipientKeyId"] = eve_header["encryptionKeyId"]
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, relabelled, header=eve_header,
            private_key=EVE_PRIVATE)


def test_seal_refuses_a_header_that_names_another_key(sealed):
    eve_header = sealed.service_grant_header(**fields(encryption_key_id=kid(public(EVE_PRIVATE))))
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, header=eve_header,
            recipient_public_key=public(BOB_PRIVATE))


def test_service_grant_envelope_does_not_open_as_provider_key(sealed, provider, envelope, materials):
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
    target = sealed.service_grant_header(**fields(encryption_key_id=row["keyId"]))
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, vector["envelope"], header=target,
            private_key=private)
    stripped = {key: vector["envelope"][key] for key in ("header", "recipients", "payload")}
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, stripped, header=target,
            private_key=private)


def test_wrap_domain_separates_profiles(sealed, header, envelope):
    from hyprial.identity.impl.provider_keys import envelope as provider_envelope

    assert sealed.SERVICE_GRANT.wrap_domain != provider_envelope.PROFILE.wrap_domain
    swapped = dataclasses.replace(sealed.SERVICE_GRANT, wrap_domain=provider_envelope.PROFILE.wrap_domain)
    with pytest.raises(sealed.ServiceGrantError) as caught:
        sealed.open(swapped, envelope, recipient_key_id=header["encryptionKeyId"], private_key=BOB_PRIVATE)
    assert caught.value.code == "PAYLOAD_INVALID"


@pytest.mark.parametrize("value", LOW_ORDER, ids=lambda value: value.hex()[:8] + value.hex()[-2:])
def test_low_order_recipient_keys_are_refused_at_seal(sealed, value):
    header = sealed.service_grant_header(**fields(encryption_key_id=kid(value)))
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, header=header,
            recipient_public_key=value)


@pytest.mark.parametrize("value", [b"", bytes(31), public(BOB_PRIVATE)[:31], public(BOB_PRIVATE) + b"\0",
                                   public(BOB_PRIVATE).hex(), None, bytearray(public(BOB_PRIVATE))])
def test_malformed_recipient_keys_are_refused_at_seal(sealed, header, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, header=header,
            recipient_public_key=value)


@pytest.mark.parametrize("secret", [b"", b"x" * 16385, "text", None], ids=["empty", "too-long", "str", "none"])
def test_secret_bounds_are_parameter_errors(sealed, header, secret):
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, secret, header=header,
            recipient_public_key=public(BOB_PRIVATE))


@pytest.mark.parametrize("name,value", [
    ("org", ""), ("org", 7), ("org", "a\0b"), ("org", "a\nb"), ("grant_id", "sg.short"),
    ("grant_id", "sg." + "A" * 26), ("grant_id", "xx." + "a" * 26), ("grantor", "al/ice"),
    ("service_device", ""), ("port", 0), ("port", 65536), ("port", True), ("port", "8447"),
    ("recipient_owner", "@members"), ("recipient_device", None),
    ("encryption_key_id", "x25519:" + "0" * 31), ("encryption_key_id", "ed25519:" + "0" * 32),
    ("encryption_key_id", "x25519:" + "A" * 32),
])
def test_header_fields_are_validated(sealed, name, value):
    refused(sealed, "INVALID_ARGUMENT", sealed.service_grant_header, **fields(**{name: value}))


@pytest.mark.parametrize("change", [
    lambda h: h.update(extra=1), lambda h: h.pop("org"), lambda h: h.update(v=2),
    lambda h: h["service"].update(name="magpie"), lambda h: h.update(recipient="@members"),
])
def test_hand_built_headers_are_checked_on_seal_and_open(sealed, header, envelope, change):
    changed = copy.deepcopy(header)
    change(changed)
    refused(sealed, "INVALID_ARGUMENT", sealed.seal_service_grant, SECRET, header=changed,
            recipient_public_key=public(BOB_PRIVATE))
    refused(sealed, "INVALID_ARGUMENT", sealed.open_service_grant, envelope, header=changed,
            private_key=BOB_PRIVATE)


@pytest.mark.parametrize("location", [(), ("header",), ("payload",), ("recipients", 0)])
def test_unknown_envelope_fields_are_refused(sealed, header, envelope, location):
    value = copy.deepcopy(envelope)
    node = value
    for part in location:
        node = node[part]
    node["extra"] = "SYNTHETIC-SENTINEL"
    error = refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, header=header,
                    private_key=BOB_PRIVATE)
    assert "SYNTHETIC-SENTINEL" not in str(error)


def test_extra_recipient_rows_are_refused(sealed, header, envelope):
    value = copy.deepcopy(envelope)
    value["recipients"].append({**value["recipients"][0], "recipientKeyId": kid(public(EVE_PRIVATE))})
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, value, header=header,
            private_key=BOB_PRIVATE)


@pytest.mark.parametrize("private", [b"", bytes(31), "text", None], ids=["empty", "short", "str", "none"])
def test_malformed_private_key_is_refused(sealed, header, envelope, private):
    refused(sealed, "PAYLOAD_INVALID", sealed.open_service_grant, envelope, header=header,
            private_key=private)


def test_fresh_randomness_per_seal(sealed, header):
    first = sealed.seal_service_grant(SECRET, header=header, recipient_public_key=public(BOB_PRIVATE))
    second = sealed.seal_service_grant(SECRET, header=header, recipient_public_key=public(BOB_PRIVATE))
    assert first["payload"] != second["payload"]
    assert first["recipients"][0]["epk"] != second["recipients"][0]["epk"]
