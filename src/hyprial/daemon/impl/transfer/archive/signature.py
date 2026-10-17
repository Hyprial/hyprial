"""AT06: bundle provenance signature (prototype, not a frozen protocol).

``envelope.py`` hides *what* travels and ``bundle.py`` proves the bytes arrived
unaltered -- neither answers *who sent it*.  Anyone holding a copied bundle can
re-serve one whose manifest matches byte for byte.  This module adds the
missing half: a detached Ed25519 signature over a domain-separated bundle
digest, so a landing can refuse a bundle it cannot attribute.

The shape follows card 399 and the design draft
``.artifacts/at06/keyed-signature-design-001.md``:

* one independent Ed25519 signing key per machine -- never the tailcat
  Noise/WireGuard key, because cross-protocol key reuse is a defect and not a
  convenience;
* the signed message is ``domain || 0x00 || digest || 0x00 || header_digest``,
  where ``digest`` is the existing canonical manifest digest;
* verification happens before landing and fails closed under a named code.

First slice is deliberately library-only: nothing on the production path calls
it yet, the same shape the C1 protocol slice landed with.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from hyprial.kernel import atomic_json_write

__all__ = [
    "KEY_ID_PREFIX",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_DOMAIN",
    "SIGNATURE_SCHEMA_VERSION",
    "SIGNING_KEY_RELPATH",
    "SIGNATURE_FILENAME",
    "SignatureBlock",
    "SignatureError",
    "SignatureFormatError",
    "SignatureInvalidError",
    "SignatureKeyError",
    "SignatureMissingError",
    "ensure_private_directory",
    "ensure_signing_key",
    "generate_signing_key",
    "public_key_id",
    "read_signing_key",
    "require_bundle_signature",
    "sign_bundle",
    "signing_key_path",
    "signing_key_record",
    "verify_bundle_signature",
    "write_signing_key",
]

SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "ed25519"
#: Domain separator; a signature minted here is not valid in any other protocol.
SIGNATURE_DOMAIN = "hyprial-transfer-bundle-v1"
SIGNING_KEY_RELPATH = "secrets/transfer/signing-key.json"
#: The detached signature travels beside the manifest, under the bundle root.
SIGNATURE_FILENAME = "signature.json"
KEY_ID_PREFIX = "ed25519:"

_DIGEST_PREFIX = "sha256:"
_BLOCK_KEYS = frozenset(
    {
        "schemaVersion",
        "algorithm",
        "domain",
        "keyId",
        "digest",
        "headerDigest",
        "signature",
    }
)


class SignatureError(Exception):
    """Base class for bundle-signature failures."""

    code = "bundle_signature_error"


class SignatureFormatError(SignatureError):
    """The signature block is present but not well formed."""

    code = "bundle_signature_malformed"


class SignatureMissingError(SignatureError):
    """No signature block accompanies the bundle."""

    code = "bundle_signature_missing"


class SignatureKeyError(SignatureError):
    """The signing key named by the block cannot be resolved."""

    code = "bundle_signature_unknown_key"


class SignatureInvalidError(SignatureError):
    """The signature does not verify over the digest it claims."""

    code = "bundle_signature_invalid"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, label: str, *, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise SignatureFormatError(f"{label} must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise SignatureFormatError(f"{label} is not valid base64") from error
    if length is not None and len(raw) != length:
        raise SignatureFormatError(f"{label} must be {length} bytes")
    return raw


def _check_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise SignatureFormatError(f"{label} must be a sha256:<hex> digest")
    hex_part = value[len(_DIGEST_PREFIX) :]
    if len(hex_part) != 64 or any(
        character not in "0123456789abcdef" for character in hex_part
    ):
        raise SignatureFormatError(f"{label} is not a lowercase sha256 hex digest")
    return value


def generate_signing_key() -> tuple[bytes, bytes]:
    """Return ``(private_bytes, public_bytes)`` for a fresh Ed25519 keypair."""

    private = ed25519.Ed25519PrivateKey.generate()
    return (private.private_bytes_raw(), private.public_key().public_bytes_raw())


def public_key_id(public_bytes: bytes) -> str:
    """The stable id a verifier resolves a public key by."""

    if len(public_bytes) != 32:
        raise SignatureFormatError("an ed25519 public key is 32 bytes")
    return KEY_ID_PREFIX + sha256(public_bytes).hexdigest()[:32]


def signing_key_path(home: Path) -> Path:
    """Where this machine's transfer signing key lives (0600)."""

    return Path(home) / SIGNING_KEY_RELPATH


def signing_key_record(private_bytes: bytes, public_bytes: bytes) -> dict[str, object]:
    """The on-disk record: both halves plus the id they hash to."""

    return {
        "schemaVersion": SIGNATURE_SCHEMA_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "keyId": public_key_id(public_bytes),
        "publicKey": _b64(public_bytes),
        "privateKey": _b64(private_bytes),
    }


def ensure_private_directory(directory: Path) -> None:
    """Create ``directory`` and any missing parents at mode 0700.

    ``atomic_json_write`` pins the key file at 0600 and its immediate parent at
    0700, but ``mkdir(parents=True)`` leaves *intermediate* directories at the
    umask default (often 0755).  A key file whose containing directories are
    world-readable is still unreadable, but it leaks the layout -- and the
    secrets root is exactly where a restrictive default belongs.  Only
    directories this call creates are touched; an existing one is left alone.
    """

    missing: list[Path] = []
    current = Path(directory)
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        os.chmod(item, 0o700)


def write_signing_key(path: Path, private_bytes: bytes, public_bytes: bytes) -> None:
    """Persist a signing key at mode 0600 under 0700 secret directories."""

    path = Path(path)
    ensure_private_directory(path.parent)
    atomic_json_write(path, signing_key_record(private_bytes, public_bytes))


def read_signing_key(path: Path) -> tuple[bytes, bytes]:
    """Load ``(private_bytes, public_bytes)``, refusing an inconsistent file.

    A record whose public half does not match the private half is an operator
    situation worth naming -- publishing the wrong public key would make every
    signature look invalid on the target.
    """

    path = Path(path)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise SignatureKeyError(f"no signing key at {path}") from None
    except ValueError as error:
        raise SignatureFormatError(
            f"signing key at {path} is not valid JSON"
        ) from error
    if not isinstance(raw, Mapping):
        raise SignatureFormatError("signing key record must be an object")
    if raw.get("algorithm") != SIGNATURE_ALGORITHM:
        raise SignatureFormatError(
            f"signing key algorithm must be {SIGNATURE_ALGORITHM!r}"
        )
    private_bytes = _unb64(raw.get("privateKey"), "privateKey", length=32)
    public_bytes = _unb64(raw.get("publicKey"), "publicKey", length=32)
    derived = (
        ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes_raw()
    )
    if derived != public_bytes:
        raise SignatureFormatError(
            "signing key record disagrees with itself: the public half does not "
            "match the private half"
        )
    if raw.get("keyId") != public_key_id(public_bytes):
        raise SignatureFormatError("signing key record carries the wrong keyId")
    return (private_bytes, public_bytes)


def ensure_signing_key(path: Path) -> tuple[bytes, bytes]:
    """Load the key at ``path``, minting one on first use."""

    path = Path(path)
    if path.exists():
        return read_signing_key(path)
    private_bytes, public_bytes = generate_signing_key()
    write_signing_key(path, private_bytes, public_bytes)
    return (private_bytes, public_bytes)


def signed_message(
    digest: str,
    header_digest: str | None = None,
    *,
    domain: str = SIGNATURE_DOMAIN,
) -> bytes:
    """The exact bytes a bundle signature covers."""

    _check_digest(digest, "digest")
    if header_digest is not None:
        _check_digest(header_digest, "headerDigest")
    return b"\x00".join(
        (
            domain.encode("utf-8"),
            digest.encode("ascii"),
            (header_digest or "").encode("ascii"),
        )
    )


@dataclass(frozen=True)
class SignatureBlock:
    """A detached signature over one bundle digest."""

    key_id: str
    digest: str
    signature: bytes
    header_digest: str | None = None
    algorithm: str = SIGNATURE_ALGORITHM
    domain: str = SIGNATURE_DOMAIN
    schema_version: int = SIGNATURE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "algorithm": self.algorithm,
            "domain": self.domain,
            "keyId": self.key_id,
            "digest": self.digest,
            "headerDigest": self.header_digest,
            "signature": _b64(self.signature),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SignatureBlock":
        if not isinstance(raw, Mapping):
            raise SignatureFormatError("signature block must be an object")
        if set(raw) != _BLOCK_KEYS:
            raise SignatureFormatError(
                "signature block does not have exactly the frozen fields"
            )
        if raw.get("schemaVersion") != SIGNATURE_SCHEMA_VERSION:
            raise SignatureFormatError("unsupported signature schemaVersion")
        if raw.get("algorithm") != SIGNATURE_ALGORITHM:
            raise SignatureFormatError(
                f"unsupported signature algorithm {raw.get('algorithm')!r}"
            )
        if raw.get("domain") != SIGNATURE_DOMAIN:
            raise SignatureFormatError("signature domain separator does not match")
        key_id = raw.get("keyId")
        if not isinstance(key_id, str) or not key_id.startswith(KEY_ID_PREFIX):
            raise SignatureFormatError("keyId must be an ed25519: key id")
        header_digest = raw.get("headerDigest")
        if header_digest is not None:
            header_digest = _check_digest(header_digest, "headerDigest")
        return cls(
            key_id=key_id,
            digest=_check_digest(raw.get("digest"), "digest"),
            signature=_unb64(raw.get("signature"), "signature", length=64),
            header_digest=header_digest,
        )


def require_bundle_signature(raw: object | None) -> SignatureBlock:
    """The fail-closed entry point for a landing that demands attribution."""

    if raw is None:
        raise SignatureMissingError("the bundle carries no signature block")
    if isinstance(raw, SignatureBlock):
        return raw
    return SignatureBlock.from_dict(raw)


def sign_bundle(
    *,
    private_bytes: bytes,
    digest: str,
    header_digest: str | None = None,
) -> SignatureBlock:
    """Sign one bundle digest with the machine's own Ed25519 key."""

    message = signed_message(digest, header_digest)
    private = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
    signature = private.sign(message)
    return SignatureBlock(
        key_id=public_key_id(private.public_key().public_bytes_raw()),
        digest=digest,
        signature=signature,
        header_digest=header_digest,
    )


def _verify_raw(public_bytes: bytes, message: bytes, signature: bytes) -> None:
    """The one place a signature is actually checked (negative controls patch here)."""

    ed25519.Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature, message)


def verify_bundle_signature(
    block: SignatureBlock,
    *,
    digest: str,
    header_digest: str | None = None,
    public_key_resolver: Callable[[str], bytes | None],
) -> None:
    """Verify ``block`` over ``digest``; raise a named code when it does not hold.

    ``public_key_resolver`` maps the block's ``key_id`` to raw public bytes and
    returns ``None`` when the key is unknown -- an unknown key is a refusal,
    never a skip.
    """

    if block.domain != SIGNATURE_DOMAIN:
        # ``from_dict`` pins this, but a caller can hand-build a block; the
        # verifier must not let a foreign domain pick its own separator.
        raise SignatureFormatError("signature domain separator does not match")
    if block.digest != _check_digest(digest, "digest"):
        raise SignatureInvalidError("the signature covers a different digest")
    expected_header = (
        None if header_digest is None else _check_digest(header_digest, "headerDigest")
    )
    if block.header_digest != expected_header:
        raise SignatureInvalidError("the signature covers a different header digest")
    public_bytes = public_key_resolver(block.key_id)
    if public_bytes is None:
        raise SignatureKeyError(f"no public key published for {block.key_id!r}")
    expected_key_id = public_key_id(public_bytes)
    if expected_key_id != block.key_id:
        raise SignatureFormatError(
            "the resolved public key does not hash to the key id in the signature"
        )
    message = signed_message(block.digest, block.header_digest, domain=block.domain)
    try:
        _verify_raw(public_bytes, message, block.signature)
    except InvalidSignature as error:
        raise SignatureInvalidError("the bundle signature does not verify") from error
