"""Build attribute-able bundles for suites that drive a production entry.

Slice 2b puts the attribution gate on all three landing entries, and slice 2c
makes the export side stamp a bundle with the exporting machine's own key.  A
suite that drives a *production* entry -- the CLI or ``DaemonApplication.handle
("transfer.land", ...)`` -- therefore has to do what a real pair of machines
does: sign the bundle with the source's key and enroll that key's public half in
the target's offline trust store.

The home these helpers default to is the fixture home (``HYPRIAL_HOME``, set by
``tests/conftest.py``), because that is the home the CLI and a test-built
``DaemonApplication`` both resolve.  Production enrolls keys by hand through the
``transfer trust-source`` command; nothing here is on a product path.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.kernel import configured_hyprial_home

from hyprial.daemon.impl.transfer.archive.attribution import sign_exported_bundle
from hyprial.daemon.impl.transfer.archive.signature import (
    ensure_signing_key,
    public_key_id,
    read_signing_key,
    signing_key_path,
)
from hyprial.daemon.impl.transfer.archive.trust import (
    add_trusted_source_key,
    trust_store_path,
)

__all__ = [
    "ENROLLED_AT",
    "configured_test_home",
    "enroll_machine_key",
    "sign_bundle_for_target",
]

#: A fixed enrollment timestamp: the store's schema wants an ISO instant with an
#: offset and nothing in these suites depends on the value.
ENROLLED_AT = "2026-01-01T00:00:00+00:00"


def configured_test_home() -> Path:
    """The home ``HYPRIAL_HOME`` names for this test."""

    return Path(configured_hyprial_home()[0])


def enroll_machine_key(
    *,
    actor: str,
    source_machine: str,
    source_home: Path | None = None,
    target_home: Path | None = None,
    added_at: str = ENROLLED_AT,
) -> str:
    """Enroll ``source_home``'s public key in ``target_home``'s trust store.

    Mints the source key on first use (the same call the export makes), so the
    enrollment matches whatever a later export signs with.  Returns the key id.
    """

    source = Path(source_home) if source_home is not None else configured_test_home()
    target = Path(target_home) if target_home is not None else configured_test_home()
    _private_bytes, public_bytes = ensure_signing_key(signing_key_path(source))
    add_trusted_source_key(
        trust_store_path(target),
        actor=actor,
        source_machine=source_machine,
        public_key=public_bytes,
        added_at=added_at,
    )
    return public_key_id(public_bytes)


def sign_bundle_for_target(
    bundle_dir: Path,
    *,
    actor: str,
    source_machine: str,
    source_home: Path | None = None,
    target_home: Path | None = None,
    credential_envelope: str | None = None,
    added_at: str = ENROLLED_AT,
) -> str:
    """Sign an exported bundle and enroll its key: the two halves of a transfer."""

    source = Path(source_home) if source_home is not None else configured_test_home()
    target = Path(target_home) if target_home is not None else configured_test_home()
    block = sign_exported_bundle(
        bundle_dir, home=source, credential_envelope=credential_envelope
    )
    _private_bytes, public_bytes = read_signing_key(signing_key_path(source))
    add_trusted_source_key(
        trust_store_path(target),
        actor=actor,
        source_machine=source_machine,
        public_key=public_bytes,
        added_at=added_at,
    )
    return block.key_id
