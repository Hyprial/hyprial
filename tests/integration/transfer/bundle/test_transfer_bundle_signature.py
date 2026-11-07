"""AT06: keyed bundle signature -- fail-loud mutants (card 399).

The producer/verifier contract under test:

* a bundle digest signed with the machine's own Ed25519 key verifies;
* every way the digest, the signature, or the key can disagree is refused by a
  *named* code -- never a skip and never a warning;
* the signed message is domain-separated, so a signature minted for another
  protocol cannot be replayed here.

``signature._verify_raw`` is the single place a signature is cryptographically
checked; the negative control short-circuits it and the tamper arms below must
turn red.
"""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from hyprial.daemon.impl.transfer.archive.signature import (
    KEY_ID_PREFIX,
    SIGNATURE_ALGORITHM,
    SIGNATURE_DOMAIN,
    SIGNATURE_SCHEMA_VERSION,
    SIGNING_KEY_RELPATH,
    SignatureBlock,
    SignatureFormatError,
    SignatureInvalidError,
    SignatureKeyError,
    SignatureMissingError,
    ensure_signing_key,
    generate_signing_key,
    public_key_id,
    read_signing_key,
    require_bundle_signature,
    sign_bundle,
    signed_message,
    signing_key_path,
    signing_key_record,
    verify_bundle_signature,
    write_signing_key,
)

DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "ef" * 32
HEADER_DIGEST = "sha256:" + "cd" * 32


def _resolver(public_bytes: bytes):
    """Resolve exactly one key id, the way a landing resolves the source's key."""

    wanted = public_key_id(public_bytes)

    def resolve(key_id: str) -> bytes | None:
        return public_bytes if key_id == wanted else None

    return resolve


def _signed(digest: str = DIGEST, header_digest: str | None = None):
    private_bytes, public_bytes = generate_signing_key()
    block = sign_bundle(
        private_bytes=private_bytes, digest=digest, header_digest=header_digest
    )
    return private_bytes, public_bytes, block


# -- the positive arm --------------------------------------------------------


def test_a_signature_over_the_digest_verifies() -> None:
    _, public_bytes, block = _signed()

    assert block.algorithm == SIGNATURE_ALGORITHM
    assert block.domain == SIGNATURE_DOMAIN
    assert block.key_id.startswith(KEY_ID_PREFIX)
    assert (
        verify_bundle_signature(
            block, digest=DIGEST, public_key_resolver=_resolver(public_bytes)
        )
        is None
    )


def test_a_signature_binds_the_header_digest_too() -> None:
    _, public_bytes, block = _signed(header_digest=HEADER_DIGEST)

    assert block.header_digest == HEADER_DIGEST
    verify_bundle_signature(
        block,
        digest=DIGEST,
        header_digest=HEADER_DIGEST,
        public_key_resolver=_resolver(public_bytes),
    )

    with pytest.raises(SignatureInvalidError):
        verify_bundle_signature(
            block,
            digest=DIGEST,
            header_digest=None,
            public_key_resolver=_resolver(public_bytes),
        )


def test_a_block_roundtrips_through_its_frozen_wire_shape() -> None:
    _, public_bytes, block = _signed(header_digest=HEADER_DIGEST)
    raw = block.as_dict()

    assert set(raw) == {
        "schemaVersion",
        "algorithm",
        "domain",
        "keyId",
        "digest",
        "headerDigest",
        "signature",
    }
    assert SignatureBlock.from_dict(raw) == block
    verify_bundle_signature(
        SignatureBlock.from_dict(raw),
        digest=DIGEST,
        header_digest=HEADER_DIGEST,
        public_key_resolver=_resolver(public_bytes),
    )


# -- tamper arms: every disagreement is a named refusal ----------------------


def test_a_block_that_names_another_digest_is_refused() -> None:
    _, public_bytes, block = _signed()

    with pytest.raises(SignatureInvalidError) as caught:
        verify_bundle_signature(
            block, digest=OTHER_DIGEST, public_key_resolver=_resolver(public_bytes)
        )
    assert caught.value.code == "bundle_signature_invalid"


def test_a_signature_minted_over_other_bytes_is_refused() -> None:
    """The declared digest is honest but the signature covers something else."""

    private_bytes, public_bytes = generate_signing_key()
    elsewhere = sign_bundle(private_bytes=private_bytes, digest=OTHER_DIGEST)
    forged = SignatureBlock(
        key_id=public_key_id(public_bytes),
        digest=DIGEST,
        signature=elsewhere.signature,
    )

    with pytest.raises(SignatureInvalidError) as caught:
        verify_bundle_signature(
            forged, digest=DIGEST, public_key_resolver=_resolver(public_bytes)
        )
    assert caught.value.code == "bundle_signature_invalid"


def test_a_flipped_signature_byte_is_refused() -> None:
    _, public_bytes, block = _signed()
    flipped = bytearray(block.signature)
    flipped[0] ^= 0x01
    tampered = SignatureBlock(
        key_id=block.key_id,
        digest=block.digest,
        signature=bytes(flipped),
        header_digest=block.header_digest,
    )

    with pytest.raises(SignatureInvalidError):
        verify_bundle_signature(
            tampered, digest=DIGEST, public_key_resolver=_resolver(public_bytes)
        )


def test_a_signature_from_another_domain_does_not_verify() -> None:
    """Domain separation: a signature minted elsewhere must not replay here."""

    private_bytes, public_bytes = generate_signing_key()
    foreign_message = b"\x00".join(
        (b"some-other-protocol-v1", DIGEST.encode("ascii"), b"")
    )
    foreign_signature = ed25519.Ed25519PrivateKey.from_private_bytes(
        private_bytes
    ).sign(foreign_message)
    replay = SignatureBlock(
        key_id=public_key_id(public_bytes),
        digest=DIGEST,
        signature=foreign_signature,
    )

    with pytest.raises(SignatureInvalidError):
        verify_bundle_signature(
            replay, digest=DIGEST, public_key_resolver=_resolver(public_bytes)
        )


def test_a_hand_built_block_cannot_pick_its_own_domain() -> None:
    """The verifier pins the domain itself; ``from_dict`` is not the only entry."""

    private_bytes, public_bytes = generate_signing_key()
    honest = sign_bundle(private_bytes=private_bytes, digest=DIGEST)
    forged = SignatureBlock(
        key_id=honest.key_id,
        digest=DIGEST,
        signature=honest.signature,
        domain="some-other-protocol-v1",
    )

    with pytest.raises(SignatureFormatError):
        verify_bundle_signature(
            forged, digest=DIGEST, public_key_resolver=_resolver(public_bytes)
        )


def test_an_unknown_key_is_refused_never_skipped() -> None:
    _, _, block = _signed()
    _, other_public = generate_signing_key()

    with pytest.raises(SignatureKeyError) as caught:
        verify_bundle_signature(
            block, digest=DIGEST, public_key_resolver=_resolver(other_public)
        )
    assert caught.value.code == "bundle_signature_unknown_key"

    with pytest.raises(SignatureKeyError):
        verify_bundle_signature(
            block, digest=DIGEST, public_key_resolver=lambda _: None
        )


def test_a_public_key_that_does_not_hash_to_the_key_id_is_refused() -> None:
    _, _, block = _signed()
    _, unrelated_public = generate_signing_key()

    with pytest.raises(SignatureFormatError):
        verify_bundle_signature(
            block,
            digest=DIGEST,
            public_key_resolver=lambda _: unrelated_public,
        )


def test_a_missing_signature_block_is_refused() -> None:
    with pytest.raises(SignatureMissingError) as caught:
        require_bundle_signature(None)
    assert caught.value.code == "bundle_signature_missing"


def test_require_bundle_signature_accepts_only_the_frozen_shape() -> None:
    _, _, block = _signed()

    assert require_bundle_signature(block) is block
    assert require_bundle_signature(block.as_dict()) == block

    with pytest.raises(SignatureFormatError):
        require_bundle_signature("ed25519:abcd")


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"schemaVersion": 99}, "unsupported signature schemaVersion"),
        ({"algorithm": "rsa"}, "algorithm"),
        ({"domain": "some-other-protocol-v1"}, "domain"),
        ({"keyId": "rsa:abcd"}, "keyId"),
        ({"digest": "md5:00"}, "digest"),
        ({"signature": "not base64!!"}, "base64"),
    ],
)
def test_a_malformed_block_is_refused(override: dict, expected: str) -> None:
    _, _, block = _signed()
    raw = dict(block.as_dict(), **override)

    with pytest.raises(SignatureFormatError) as caught:
        SignatureBlock.from_dict(raw)
    assert expected.split()[0] in str(caught.value) or expected in str(caught.value)


def test_a_block_with_extra_or_missing_fields_is_refused() -> None:
    _, _, block = _signed()
    raw = block.as_dict()

    with pytest.raises(SignatureFormatError):
        SignatureBlock.from_dict(dict(raw, extra="field"))
    with pytest.raises(SignatureFormatError):
        SignatureBlock.from_dict({k: v for k, v in raw.items() if k != "signature"})


def test_a_short_signature_is_refused_before_any_crypto() -> None:
    _, _, block = _signed()
    raw = dict(block.as_dict(), signature="AAAA")

    with pytest.raises(SignatureFormatError):
        SignatureBlock.from_dict(raw)


# -- the module refuses bad digests before signing anything ------------------


@pytest.mark.parametrize(
    "bad", ["", "sha256:", "sha256:zz", "ab" * 32, "md5:" + "ab" * 32]
)
def test_signed_message_refuses_a_malformed_digest(bad: str) -> None:
    with pytest.raises(SignatureFormatError):
        signed_message(bad)


def test_public_key_id_refuses_a_wrong_length_key() -> None:
    with pytest.raises(SignatureFormatError):
        public_key_id(b"too short")


# -- key custody -------------------------------------------------------------


def test_the_signing_key_lives_under_the_home_at_0600(tmp_path: Path) -> None:
    path = signing_key_path(tmp_path)

    assert path == tmp_path / SIGNING_KEY_RELPATH
    private_bytes, public_bytes = ensure_signing_key(path)

    assert read_signing_key(path) == (private_bytes, public_bytes)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


def test_ensure_signing_key_is_stable_across_calls(tmp_path: Path) -> None:
    path = signing_key_path(tmp_path)
    first = ensure_signing_key(path)

    assert ensure_signing_key(path) == first


def test_a_key_record_that_disagrees_with_itself_is_refused(tmp_path: Path) -> None:
    _, good_public = generate_signing_key()
    _, other_public = generate_signing_key()
    private_bytes, public_bytes = generate_signing_key()
    record = signing_key_record(private_bytes, public_bytes)

    mismatched = dict(record, publicKey=base64.b64encode(other_public).decode())
    path = tmp_path / "signing-key.json"
    path.write_text(json.dumps(mismatched))
    with pytest.raises(SignatureFormatError) as caught:
        read_signing_key(path)
    assert "does not match the private half" in str(caught.value)

    wrong_id = dict(record, keyId=public_key_id(good_public))
    path.write_text(json.dumps(wrong_id))
    with pytest.raises(SignatureFormatError):
        read_signing_key(path)


def test_a_missing_or_unreadable_key_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(SignatureKeyError):
        read_signing_key(tmp_path / "absent.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(SignatureFormatError):
        read_signing_key(broken)

    wrong_alg = tmp_path / "wrong-alg.json"
    private_bytes, public_bytes = generate_signing_key()
    wrong_alg.write_text(
        json.dumps(
            dict(signing_key_record(private_bytes, public_bytes), algorithm="rsa")
        )
    )
    with pytest.raises(SignatureFormatError):
        read_signing_key(wrong_alg)


def test_write_signing_key_is_readable_by_an_independent_verifier(
    tmp_path: Path,
) -> None:
    path = signing_key_path(tmp_path)
    private_bytes, public_bytes = generate_signing_key()
    write_signing_key(path, private_bytes, public_bytes)

    reloaded_private, reloaded_public = read_signing_key(path)
    assert reloaded_public == public_bytes
    block = sign_bundle(private_bytes=reloaded_private, digest=DIGEST)
    verify_bundle_signature(
        block, digest=DIGEST, public_key_resolver=_resolver(reloaded_public)
    )


def test_the_key_id_is_a_hash_of_the_public_key_not_a_name() -> None:
    _, first_public = generate_signing_key()
    _, second_public = generate_signing_key()

    assert public_key_id(first_public) == public_key_id(first_public)
    assert public_key_id(first_public) != public_key_id(second_public)
    assert len(public_key_id(first_public)) == len(KEY_ID_PREFIX) + 32


def test_the_prototype_version_and_domain_are_pinned() -> None:
    assert SIGNATURE_SCHEMA_VERSION == 1
    assert SIGNATURE_ALGORITHM == "ed25519"
    assert SIGNATURE_DOMAIN == "hyprial-transfer-bundle-v1"
