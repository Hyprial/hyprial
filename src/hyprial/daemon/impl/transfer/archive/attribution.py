"""AT06 slice 2c: mint the detached bundle signature a landing verifies.

``signature.py`` is the primitive (*does this digest verify under key X*) and
``landing/signature_gate.py`` is the policy (*which keys does this target
accept*).  This module is the other end of the wire: the export side that
stamps a finished bundle with the exporting machine's own key, plus the one
shared way both ends compute the declared credential envelope's header digest.

Why the signature is minted here and not inside ``export_bundle``: the key
belongs to the *machine*, not to the payload.  A bundle carries the source
agent home, ``secrets/`` included, so a key stored inside that home would travel
to the target with the bytes it is meant to vouch for.  ``signing_key_path``
therefore resolves under the exporting machine's own home (the daemon's home on
the chain path, the CLI's configured home for a standalone export), and the
transport unit stays pure data.

Order matters on this side too: the digest is re-derived from the sealed
manifest by ``manifest_digest`` (never taken from a caller), and a manifest that
no longer matches its own seal raises rather than being signed, so a tampered
tree cannot pick up an attribution stamp on the way out.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from hyprial.kernel import atomic_json_write

from hyprial.daemon.impl.transfer.archive.bundle import (
    PAYLOAD_DIRNAME,
    _safe_relative,
)
from hyprial.daemon.impl.transfer.archive.envelope import (
    EnvelopeFormatError,
    envelope_header_digest,
)
from hyprial.daemon.impl.transfer.archive.signature import (
    SIGNATURE_FILENAME,
    SignatureBlock,
    SignatureFormatError,
    ensure_signing_key,
    sign_bundle,
    signing_key_path,
)
from hyprial.daemon.impl.transfer.manifest_reading import manifest_digest

__all__ = [
    "SIGNATURE_FILENAME",
    "envelope_header_digest_for_bundle",
    "sign_exported_bundle",
    "write_bundle_signature",
]


def envelope_header_digest_for_bundle(
    bundle_dir: os.PathLike[str] | str, credential_envelope: str | None
) -> str | None:
    """The declared credential envelope's header digest, or ``None``.

    The declaration is resolved the way ``validate_bundle`` resolves it: the
    friendly name is payload-relative, and a ``payload/``-prefixed name means
    the same file (``bundle._resolve_declared_path``).  A declared envelope the
    bundle does not carry is a malformed bundle, not an unsigned one.
    """

    if not credential_envelope:
        return None
    root = Path(bundle_dir)
    parts = PurePosixPath(_safe_relative(credential_envelope)).parts
    if parts and parts[0] == PAYLOAD_DIRNAME:
        parts = parts[1:]
    path = root / PAYLOAD_DIRNAME / Path(*parts)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SignatureFormatError(
            f"the manifest declares credential envelope {credential_envelope!r} "
            "but the bundle carries no such file"
        ) from None
    except (OSError, ValueError) as error:
        raise SignatureFormatError(
            f"{credential_envelope} is not readable JSON: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise SignatureFormatError(f"{credential_envelope} must be an object")
    try:
        return envelope_header_digest(raw)
    except EnvelopeFormatError as error:
        # Name the declaration, not just the header: an operator reading the
        # refusal has to know which declared file could not be bound.
        raise SignatureFormatError(
            f"declared credential envelope {credential_envelope!r} is not a "
            f"sealed envelope: {error}"
        ) from error


def write_bundle_signature(
    bundle_dir: os.PathLike[str] | str, block: SignatureBlock
) -> Path:
    """Write the signature block beside the manifest, atomically at 0600."""

    path = Path(bundle_dir) / SIGNATURE_FILENAME
    atomic_json_write(path, block.as_dict())
    return path


def sign_exported_bundle(
    bundle_dir: os.PathLike[str] | str,
    *,
    home: os.PathLike[str] | str,
    credential_envelope: str | None = None,
) -> SignatureBlock:
    """Stamp an exported bundle with ``home``'s own Ed25519 key.

    ``home`` is the exporting **machine's** home -- the daemon's own home on the
    chain path, the CLI's configured home for a standalone export.  The key is
    minted on first use under that home and never leaves it, and the digest is
    re-derived here so the signature always covers the bundle's current bytes.
    """

    private_bytes, _public_bytes = ensure_signing_key(signing_key_path(Path(home)))
    digest = manifest_digest(bundle_dir)
    header_digest = envelope_header_digest_for_bundle(bundle_dir, credential_envelope)
    block = sign_bundle(
        private_bytes=private_bytes, digest=digest, header_digest=header_digest
    )
    write_bundle_signature(bundle_dir, block)
    return block
