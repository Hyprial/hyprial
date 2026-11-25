"""E1 positive vectors, custody and fresh-DEK re-sealing."""
import base64
import json

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from hyprial.identity import UserKey
from hyprial.kernel import ipc_errors

from vectors import envelope_vector


def test_reference_vector_is_reproducible(vector):
    assert envelope_vector() == vector


def test_six_failure_codes_are_registered():
    expected = {"PROVIDER_GRANT_MISSING", "PROVIDER_GRANT_REVOKED", "PROVIDER_KEY_NOT_FOUND",
                "PROVIDER_KEY_RECIPIENT_PENDING", "PROVIDER_KEY_UNSEAL_FAILED", "PROVIDER_KEY_SIGNATURE_INVALID"}
    assert expected <= ipc_errors.ALL_CODES


def test_seal_matches_independent_fixed_vector(core, vector, materials, entropy):
    signer, owner, recipients = materials
    sealed = core.seal(base64.b64decode(vector["secret"]), header=vector["envelope"]["header"],
                       recipients=recipients, signer=signer)
    assert sealed == vector["envelope"]
    assert core.verify_envelope(sealed, owner_key=owner) == sealed
    assert base64.b64decode(vector["secret"]).decode() not in json.dumps(sealed)
    assert vector["ownerPrivate"] not in repr(signer)
    assert bytes(range(32)).hex() not in repr(signer)


@pytest.mark.parametrize("index", [0, 1])
def test_each_named_device_can_open(core, vector, materials, index):
    row = vector["recipients"][index]
    assert core.open_envelope(vector["envelope"], owner_key=materials[1],
                              recipient_key_id=row["keyId"], private_key=base64.b64decode(row["private"])) == base64.b64decode(vector["secret"])


def test_unnamed_device_never_gets_plaintext(core, vector, materials):
    with pytest.raises(core.Error) as caught:
        core.open_envelope(vector["envelope"], owner_key=materials[1],
                           recipient_key_id="x25519:" + "0" * 32, private_key=b"x" * 32)
    assert caught.value.code == "PROVIDER_KEY_RECIPIENT_PENDING"


def test_reseal_increments_version_and_old_dek_cannot_decrypt(core, vector, materials):
    row = vector["recipients"][0]
    sealed = core.reseal(vector["envelope"], owner_key=materials[1],
                         recipient_key_id=row["keyId"], private_key=base64.b64decode(row["private"]),
                         signer=materials[0], recipients=materials[2], created_at=2000)
    assert sealed["header"]["dekVersion"] == 2
    assert sealed["payload"]["nonce"] != vector["envelope"]["payload"]["nonce"]
    assert sealed["payload"]["ciphertext"] != vector["envelope"]["payload"]["ciphertext"]
    with pytest.raises(InvalidTag):
        AESGCM(base64.b64decode(vector["randomValues"][0])).decrypt(
            base64.b64decode(sealed["payload"]["nonce"]), base64.b64decode(sealed["payload"]["ciphertext"]),
            UserKey.canonical_bytes(sealed["header"]))
    for recipient in vector["recipients"]:
        assert core.open_envelope(sealed, owner_key=materials[1], recipient_key_id=recipient["keyId"],
                                  private_key=base64.b64decode(recipient["private"])) == base64.b64decode(vector["secret"])


def test_no_production_signer_activation(core):
    assert "hyprial/provider-key/envelope/v1" not in UserKey.PURPOSES
    assert "hyprial/provider-key/grant/v1" not in UserKey.PURPOSES
    for name in ("StoragePort", "OwnerKeyPort", "RecipientPort"):
        assert getattr(core, name)


def test_randomness_is_fresh_and_no_known_vector_secret_is_reused(core, vector, materials):
    first = core.seal(b"synthetic-one", header=vector["envelope"]["header"],
                      recipients=materials[2], signer=materials[0])
    second = core.seal(b"synthetic-two", header={**vector["envelope"]["header"], "dekVersion": 2},
                       recipients=materials[2], signer=materials[0])
    assert first["payload"]["nonce"] != second["payload"]["nonce"]
    assert first["recipients"][0]["epk"] != first["recipients"][1]["epk"]
    assert first["recipients"][0]["epk"] != second["recipients"][0]["epk"]
    with pytest.raises(InvalidTag):
        AESGCM(base64.b64decode(vector["randomValues"][0])).decrypt(
            base64.b64decode(first["payload"]["nonce"]), base64.b64decode(first["payload"]["ciphertext"]),
            UserKey.canonical_bytes(first["header"]))


def test_wrong_recipient_private_key_never_falls_back(core, vector, materials):
    with pytest.raises(core.Error) as caught:
        core.open_envelope(vector["envelope"], owner_key=materials[1],
                           recipient_key_id=vector["recipients"][0]["keyId"],
                           private_key=base64.b64decode(vector["recipients"][1]["private"]))
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"
