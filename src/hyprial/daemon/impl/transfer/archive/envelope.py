"""AT06: sealed credential envelope (prototype, not a frozen protocol).

Allen's ruling (2026-09-26, quoted in the transfer handoff): a bundle carries
**the original owner's credentials** by default, and the target may only fall
back to the host's own key when the host owner explicitly authorises it.  This
module carries that envelope:

    envelope.json   # recipient-sealed credentials + explicit risk metadata

It is a *replaceable prototype*.  Card 355 item 1 allows a prototype before the
#820 §2 / Q8 ruling, but forbids freezing the suggested protocol as the product
default -- hence ``PROTOTYPE_PROTOCOL`` and the ``--prototype`` gate on the CLI
entry points.  Nothing here flips a production default.

Crypto: X25519 key agreement, HKDF-SHA256, AES-256-GCM.  The bundle transport
never sees plaintext credentials, and the envelope authenticates the header so
a re-sealed envelope with different metadata fails to open.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "ENVELOPE_ALGORITHM",
    "PROTOTYPE_PROTOCOL",
    "EnvelopeError",
    "EnvelopeFormatError",
    "EnvelopeOpenError",
    "EnvelopeStrategyError",
    "CredentialRecord",
    "CredentialPolicy",
    "generate_keypair",
    "seal_envelope",
    "open_envelope",
    "load_envelope",
    "write_envelope",
    "resolve_policy",
]

ENVELOPE_SCHEMA_VERSION = 1
ENVELOPE_ALGORITHM = "x25519-hkdf-sha256-aes256gcm"
PROTOTYPE_PROTOCOL = True

_AAD_FIELDS = (
    "schema_version",
    "algorithm",
    "actor",
    "owner",
    "created_at",
    "prototype",
    "policy",
    "credentials",
)


class EnvelopeError(Exception):
    """Base class for envelope failures."""

    code = "envelope_error"


class EnvelopeFormatError(EnvelopeError):
    """The envelope is not a well-formed sealed credential envelope."""

    code = "envelope_invalid"


class EnvelopeOpenError(EnvelopeError):
    """The envelope could not be opened with the supplied key."""

    code = "envelope_unreadable"


class EnvelopeStrategyError(EnvelopeError):
    """The requested credential strategy is not allowed."""

    code = "envelope_strategy_refused"


@dataclass(frozen=True)
class CredentialRecord:
    """One credential carried (or referenced) by the envelope.

    ``machine_bound`` and ``requires_reauth`` are the exporter's assertions,
    not guesses: card 355 item 4 requires machine-bound or re-login credentials
    to be *reported*, so the field is mandatory and defaults to True -- an
    unknown credential is reported as needing attention rather than silently
    treated as portable.
    """

    name: str
    machine_bound: bool = True
    requires_reauth: bool = False
    refreshable: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "machine_bound": self.machine_bound,
            "requires_reauth": self.requires_reauth,
            "refreshable": self.refreshable,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CredentialRecord":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise EnvelopeFormatError("credential record needs a non-empty name")
        return cls(
            name=name,
            machine_bound=bool(raw.get("machine_bound", True)),
            requires_reauth=bool(raw.get("requires_reauth", False)),
            refreshable=bool(raw.get("refreshable", False)),
            notes=str(raw.get("notes", "")),
        )


@dataclass(frozen=True)
class CredentialPolicy:
    """Which credential set a landing may use."""

    strategy: str
    host_owner_grant: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "host_owner_grant": self.host_owner_grant,
            "reason": self.reason,
        }


def resolve_policy(
    *,
    requested: str = "original-user",
    host_owner_grant: str | None = None,
) -> CredentialPolicy:
    """Decide whether a landing may use the original user's key or the host's.

    ``requested`` is ``original-user`` (the default) or ``host``.  Choosing
    ``host`` without an explicit host-owner grant is refused -- the host key
    never becomes an implicit fallback.
    """

    if requested == "original-user":
        return CredentialPolicy(
            strategy="original-user",
            host_owner_grant=host_owner_grant,
            reason="default: the bundle carries the original owner's credentials",
        )
    if requested != "host":
        raise EnvelopeStrategyError(
            f"unknown credential strategy {requested!r}; "
            "expected 'original-user' or 'host'"
        )
    if not host_owner_grant:
        raise EnvelopeStrategyError(
            "host credentials require an explicit host-owner grant; "
            "the original user's key is the default"
        )
    return CredentialPolicy(
        strategy="host",
        host_owner_grant=host_owner_grant,
        reason=f"host owner granted {host_owner_grant!r}",
    )


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_bytes, public_bytes)`` for a fresh X25519 keypair."""

    private = x25519.X25519PrivateKey.generate()
    return (
        private.private_bytes_raw(),
        private.public_key().public_bytes_raw(),
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, label: str, *, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise EnvelopeFormatError(f"{label} must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise EnvelopeFormatError(f"{label} is not valid base64") from error
    if length is not None and len(raw) != length:
        raise EnvelopeFormatError(f"{label} must be {length} bytes")
    return raw


def _derive_key(shared: bytes, salt: bytes, info: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info)
    return hkdf.derive(shared)


def _aad(header: Mapping[str, object], extra: bytes) -> bytes:
    canonical = json.dumps(
        {name: header[name] for name in _AAD_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return canonical + b"|" + extra


def _normalise_credentials(
    credentials: Iterable[CredentialRecord | Mapping[str, object]],
) -> list[CredentialRecord]:
    records: list[CredentialRecord] = []
    for item in credentials:
        records.append(
            item if isinstance(item, CredentialRecord) else CredentialRecord.from_dict(item)
        )
    return records


def seal_envelope(
    credentials: Mapping[str, object],
    *,
    recipient_public_key: bytes,
    actor: str,
    owner: str,
    created_at: str,
    sender_private_key: bytes | None = None,
    records: Iterable[CredentialRecord | Mapping[str, object]] = (),
    policy: CredentialPolicy | None = None,
    aad: bytes = b"",
) -> dict[str, object]:
    """Seal ``credentials`` so only the recipient's private key can open them."""

    if len(recipient_public_key) != 32:
        raise EnvelopeStrategyError("recipient public key must be 32 raw X25519 bytes")

    private_bytes = sender_private_key or os.urandom(32)
    sender_private = x25519.X25519PrivateKey.from_private_bytes(private_bytes)
    sender_public = sender_private.public_key().public_bytes_raw()
    recipient_public = x25519.X25519PublicKey.from_public_bytes(
        recipient_public_key
    )
    shared = sender_private.exchange(recipient_public)

    header: dict[str, object] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "algorithm": ENVELOPE_ALGORITHM,
        "actor": actor,
        "owner": owner,
        "created_at": created_at,
        "prototype": PROTOTYPE_PROTOCOL,
        "sender_public_key": _b64(sender_public),
        "policy": (policy or resolve_policy()).as_dict(),
        "credentials": [record.as_dict() for record in _normalise_credentials(records)],
    }

    nonce = os.urandom(12)
    salt = os.urandom(32)
    key = _derive_key(shared, salt, _aad(header, aad))
    plaintext = json.dumps(
        dict(credentials), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(header, aad))

    envelope = dict(header)
    envelope["salt"] = _b64(salt)
    envelope["nonce"] = _b64(nonce)
    envelope["ciphertext"] = _b64(ciphertext)
    return envelope


def open_envelope(
    envelope: Mapping[str, object],
    *,
    recipient_private_key: bytes,
    aad: bytes = b"",
) -> dict[str, object]:
    """Open a sealed envelope with the recipient's private key."""

    if envelope.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        raise EnvelopeFormatError(
            f"unknown envelope schema {envelope.get('schema_version')!r}"
        )
    if envelope.get("algorithm") != ENVELOPE_ALGORITHM:
        raise EnvelopeFormatError(
            f"unsupported envelope algorithm {envelope.get('algorithm')!r}"
        )
    missing = [name for name in _AAD_FIELDS if name not in envelope]
    if missing:
        raise EnvelopeFormatError(f"envelope is missing {', '.join(missing)}")

    sender_public = _unb64(envelope.get("sender_public_key"), "sender_public_key", length=32)
    salt = _unb64(envelope.get("salt"), "salt")
    nonce = _unb64(envelope.get("nonce"), "nonce", length=12)
    ciphertext = _unb64(envelope.get("ciphertext"), "ciphertext")

    private = x25519.X25519PrivateKey.from_private_bytes(recipient_private_key)
    shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(sender_public))
    key = _derive_key(shared, salt, _aad(envelope, aad))
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(envelope, aad))
    except Exception as error:  # noqa: BLE001 - one message for every failure mode
        raise EnvelopeOpenError(
            "envelope could not be opened: wrong key, tampered ciphertext or "
            "changed header metadata"
        ) from error
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnvelopeOpenError("envelope plaintext is not JSON") from error
    if not isinstance(payload, dict):
        raise EnvelopeOpenError("envelope plaintext must be a JSON object")
    return payload


def write_envelope(path: os.PathLike[str] | str, envelope: Mapping[str, object]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def load_envelope(path: os.PathLike[str] | str) -> dict[str, object]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EnvelopeFormatError(f"envelope is not JSON: {error}") from error
    if not isinstance(envelope, dict):
        raise EnvelopeFormatError("envelope must be a JSON object")
    return envelope
