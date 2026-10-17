"""One reading of a bundle's ``manifest.json``: parse, seal and digest together.

``bundle.py`` owns the manifest schema, but a landing that re-opens
``manifest.json`` between its gates lets a local writer rebind what
"validated", "signed" and "landed" describe.  ``load_sealed_manifest`` reads
the file once and returns ``SealedManifest``: the parsed manifest (whose
``from_dict`` re-derives and checks the seal) plus that same reading's digest.

The module sits beside ``archive/`` -- that directory is at its seven-file
budget and ``bundle.py`` at its line budget -- and reads the schema from
``archive.bundle`` in one direction only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import (
    MANIFEST_DIGEST_FIELD,
    MANIFEST_NAME,
    BundleManifest,
    BundleManifestError,
    BundleTamperError,
    _manifest_digest,
)

__all__ = ["SealedManifest", "load_sealed_manifest", "manifest_digest"]


@dataclass(frozen=True)
class SealedManifest:
    """One reading of ``manifest.json``: the parsed manifest and its seal.

    ``digest`` is the manifest's own ``sha256:``-prefixed seal over exactly the
    bytes this object was parsed from.  Landing entries hand this object to
    every gate (validation, attribution, the copy loop), so the manifest that
    was validated, the digest that was signed and the entries that land are one
    reading -- not three files a writer can swap between gates.
    """

    manifest: BundleManifest
    digest: str


def load_sealed_manifest(bundle_dir: os.PathLike[str] | str) -> SealedManifest:
    """Read ``manifest.json`` once and bind its parse, seal and digest.

    The file is opened a single time: the structural parse (which re-derives
    the seal) and the digest both come from that one reading, so a local writer
    that rewrites the file between two gates can no longer make "validated",
    "signed" and "landed" describe three different manifests.
    """

    path = Path(bundle_dir) / MANIFEST_NAME
    if not path.is_file():
        raise BundleManifestError(f"manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleManifestError(f"manifest is not readable JSON: {error}") from error
    manifest = BundleManifest.from_dict(raw)
    # ``from_dict`` proved the seal is a non-empty string equal to the body's
    # own digest, so both fields below come from this same reading.
    sealed = raw[MANIFEST_DIGEST_FIELD]
    return SealedManifest(manifest=manifest, digest=f"sha256:{sealed}")


def manifest_digest(bundle_dir: os.PathLike[str] | str) -> str:
    """The bundle's sealed digest, re-derived from ``manifest.json``.

    Never taken from a caller: a signer and a verifier bind the same bytes, and
    a manifest that fails its own seal raises here, never a signature.
    """

    path = Path(bundle_dir) / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BundleManifestError(f"manifest not found: {path}") from None
    except (OSError, ValueError) as error:
        raise BundleManifestError(f"manifest is not readable JSON: {error}") from error
    if not isinstance(raw, Mapping):
        raise BundleManifestError("manifest must be an object")
    sealed = raw.get(MANIFEST_DIGEST_FIELD)
    if not isinstance(sealed, str) or not sealed:
        raise BundleManifestError("manifest is not sealed")
    if sealed != _manifest_digest(raw):
        raise BundleTamperError("manifest seal does not match its contents")
    return f"sha256:{sealed}"
