"""What a sealed-envelope profile decides; the primitive itself lives in core.

Decision (service grant S2, 2026-11-26): a profile fixes the wrap domain fed to
HKDF, the exact header field set and its value checks, whether an owner
signature is required (signature is None means unsigned), and which wire codes
each failure class raises. Callers own the error vocabulary so existing
provider-key refusals keep their codes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Recipient:
    """One recipient device's X25519 public key and its x25519:<hex> key id."""
    key_id: str
    public_key: bytes


@dataclass(frozen=True)
class Signature:
    """Owner-signature hooks for a signed profile; the primitive never chooses a key."""
    check_signer: Callable[[object, dict], None]
    sign: Callable[[dict, object, dict], dict]
    verify: Callable[[dict, object, object, dict], None]


@dataclass(frozen=True)
class Profile:
    """Parameters of one envelope family.

    invalid: structure or signature refusals. unseal: key exchange or AEAD
    failure. missing: no row for the requested recipient. argument: unusable
    seal input (secret, recipient list or recipient key).
    """
    name: str
    wrap_domain: bytes
    header_fields: frozenset[str]
    check_header: Callable[[dict], None] = field(repr=False)
    signature: Signature | None = field(repr=False)
    error: Callable[[str], Exception] = field(repr=False)
    invalid: str
    unseal: str
    missing: str
    argument: str
    recipient_type: type = Recipient
    max_secret_bytes: int = 16384
    max_recipients: int = 32
    reject_weak_recipients: bool = False
