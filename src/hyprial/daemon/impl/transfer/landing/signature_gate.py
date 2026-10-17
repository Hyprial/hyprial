"""AT06 slice 2b: the landing-side attribution gate.

``signature.py`` answers *"does this digest verify under key X"* and
``trust.py`` says *"which keys does this target accept"*, but neither is on a
landing path.  This module is the seam the three landing entries call; the
matching export-side stamp lives in ``archive/attribution.py``, which shares
``envelope_header_digest_for_bundle`` so both ends bind the same bytes.  It reads
``<bundle>/signature.json``, refuses a bundle this target cannot attribute under
a named code, and reports the attestation a landing receipt records.

A key alone is not an attribution.  An enrollment names the identity its key
speaks for (``(actor, sourceMachine)``, the trust store's own record identity),
so the gate binds *both* halves: the signature has to verify under a trusted
key **and** the identity that key is enrolled for has to be the one the bundle's
sealed manifest claims.  A key enrolled for ``alice`` may not sign a bundle that
lands as ``bob`` -- otherwise the receipt would attribute the transfer to the
actor the store names while the landing adopts the actor the manifest names.

Order is the point.  The gate runs *after* ``validate_bundle`` (integrity: the
manifest seal and every payload digest) and *before* anything is written, so a
bundle that fails attribution leaves the target byte-identical -- the same
"unresolved means hold, never compensate" rule A2 and A6 follow elsewhere.

The digest it binds is the manifest's own canonical ``sha256:`` seal, which
transitively covers every payload file, so the signature adds attribution and
nothing else.  The digest and the identity both come from *one* reading of
``manifest.json`` (``SealedManifest``), taken by the landing entry and threaded
through validation, this gate and the copy loop -- a writer that rewrites the
file between two gates cannot make the validated, the signed and the landed
manifest three different documents.  ``headerDigest`` binds the credential
envelope's authenticated header when the bundle carries one, and is ``null``
otherwise.

Nothing here decides *policy from the wire*: the target's acceptance set is the
operator's own offline file, and a missing file means "trust nobody".
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.attribution import (
    SIGNATURE_FILENAME,
    envelope_header_digest_for_bundle,
)
from hyprial.daemon.impl.transfer.archive.signature import (
    SignatureBlock,
    SignatureError,
    SignatureFormatError,
    SignatureMissingError,
    require_bundle_signature,
    verify_bundle_signature,
)
from hyprial.daemon.impl.transfer.archive.trust import (
    RETIREMENT_REFUSE,
    TrustStore,
    load_trust_store,
    trust_store_path,
)
from hyprial.daemon.impl.transfer.manifest_reading import (
    SealedManifest,
    load_sealed_manifest,
)

__all__ = [
    "SIGNATURE_NAME",
    "SignatureAttestation",
    "SignatureIdentityMismatch",
    "SignaturePolicy",
    "SignatureRetiredError",
    "require_trusted_signature",
    "trust_policy_for",
]

#: Where a bundle carries its detached signature, beside ``manifest.json``.
SIGNATURE_NAME = SIGNATURE_FILENAME


class SignatureRetiredError(SignatureError):
    """The signing key is past its ``retiredAt`` and this store refuses it."""

    code = "bundle_signature_retired"


class SignatureIdentityMismatch(SignatureError):
    """The key verifies, but not for the identity the manifest claims.

    The trust store enrolls a key against one ``(actor, sourceMachine)``
    identity; the bundle's sealed manifest names the identity it lands as.  A
    signature that verifies under the key yet claims a different identity is
    exactly the "a machine's key vouches for any actor name" hole this binds
    shut.  The refusal names only the manifest's claim and the key -- never the
    identity the store enrolled, which is not the sender's to learn.
    """

    code = "bundle_signature_identity_mismatch"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SignaturePolicy:
    """How one landing decides whether it will accept a bundle's sender.

    ``trust_store`` is this target's own offline anchor; ``trust_home`` names the
    home whose store to load, so a daemon hands over a path and the file is read
    once, at the gate, where its failure is attributable.  ``allow_unsigned`` is
    for test and rehearsal fixtures that exercise landing mechanics on bundles no
    machine ever signed -- a production call site builds its policy with
    :func:`trust_policy_for`, which cannot produce one.  Under it the gate
    skips attribution wholesale, so a fixture that does sign cannot be judged
    against a store the test never meant to consult.
    """

    trust_store: TrustStore | None = None
    trust_home: Path | None = None
    allow_unsigned: bool = False
    now: Callable[[], datetime] = _utc_now

    def store(self) -> TrustStore:
        """The acceptance set this policy verifies against, loaded once per call."""

        if self.trust_store is not None:
            return self.trust_store
        if self.trust_home is not None:
            return load_trust_store(trust_store_path(Path(self.trust_home)))
        # No anchor is not an anchor: an empty store refuses every key, which is
        # exactly what "nobody told this target about any source" has to mean.
        return TrustStore()


@dataclass(frozen=True)
class SignatureAttestation:
    """What a landing recorded about the bundle's sender.

    ``actor`` and ``source_machine`` are the sealed manifest's own claim -- the
    identity the bundle says it lands as -- which the gate has already proven
    equal to the enrolled identity of the verifying key.  Recording the claim
    rather than the store's copy is the point of F2: a receipt and the agent a
    landing adopts must name the same actor.
    """

    key_id: str
    actor: str
    source_machine: str
    digest: str
    header_digest: str | None = None
    retirement_notice: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "keyId": self.key_id,
            "actor": self.actor,
            "sourceMachine": self.source_machine,
            "digest": self.digest,
            "headerDigest": self.header_digest,
            "retirementNotice": self.retirement_notice,
        }


def trust_policy_for(home: os.PathLike[str] | str) -> SignaturePolicy:
    """The production policy: require a signature, accept this home's own keys."""

    return SignaturePolicy(trust_home=Path(home))


def require_trusted_signature(
    bundle_dir: os.PathLike[str] | str,
    *,
    policy: SignaturePolicy,
    credential_envelope: str | None = None,
    sealed: SealedManifest | None = None,
) -> SignatureAttestation | None:
    """Refuse a bundle this target cannot attribute, under a named code.

    Attribution is a double bind: the signature has to verify under a key this
    target trusts, and the sealed manifest's ``(actor, sourceMachine)`` has to
    be the identity that key is enrolled for.  Only then does the returned
    attestation describe the sender.

    ``sealed`` is the one reading a landing already took
    (``load_sealed_manifest``); the signature is weighed against *that*
    reading's digest and the identity check uses *that* reading's manifest, so
    a file swapped between validation and the gate cannot rebind either.  When
    omitted the gate takes its own single reading.

    Returns the attestation a receipt should record, or ``None`` for the
    rehearsal policy on an unsigned bundle.  Raises the ``SignatureError``
    family (and, for a broken trust store, the ``TrustStoreError`` family).
    """

    root = Path(bundle_dir)
    if policy.allow_unsigned:
        # Rehearsal: the caller has stated it is testing landing mechanics, not
        # attribution, so neither a missing nor a stray present signature
        # decides anything here -- a half-attributed test bundle would otherwise
        # be judged against an empty store and fail for the wrong reason.
        # Production cannot build this policy (the tripwire pins that).
        return None
    block = _read_signature_block(root)
    if block is None:
        raise SignatureMissingError(
            f"bundle {root} carries no {SIGNATURE_NAME}: this target lands only "
            "bundles it can attribute to a source key"
        )

    # One reading: the digest the signature is weighed against and the
    # manifest whose identity the receipt names come from the same bytes.
    reading = sealed if sealed is not None else load_sealed_manifest(root)
    header_digest = envelope_header_digest_for_bundle(root, credential_envelope)
    store = policy.store()
    verify_bundle_signature(
        block,
        digest=reading.digest,
        header_digest=header_digest,
        public_key_resolver=store.resolver(),
    )
    record = store.key(block.key_id)
    if record is None:  # pragma: no cover - verification resolved it above
        raise SignatureFormatError(
            f"signature names {block.key_id!r}, which the trust store no longer holds"
        )
    # The key is trusted; now bind *who* it is trusted for.  The manifest is
    # the signed claim (the reading's seal was verified by ``from_dict`` when
    # it was taken), and the store's record identity is what the operator
    # enrolled, so a mismatch is a refusal rather than a re-labelled receipt.
    manifest = reading.manifest
    if (manifest.actor, manifest.source_machine) != record.identity():
        raise SignatureIdentityMismatch(
            f"bundle's sealed manifest names actor {manifest.actor!r} on "
            f"{manifest.source_machine!r}, but trusted key {block.key_id} is not "
            "enrolled for that identity; a bundle lands only under the actor "
            "and source machine its key was enrolled for"
        )
    notice = store.retirement_notice(block.key_id, now=policy.now())
    if notice is not None and store.retirement_policy == RETIREMENT_REFUSE:
        raise SignatureRetiredError(notice)
    return SignatureAttestation(
        key_id=block.key_id,
        # From the manifest, not the store: the two are equal by the check
        # above, and a receipt must describe the bundle that landed.
        actor=manifest.actor,
        source_machine=manifest.source_machine,
        digest=reading.digest,
        header_digest=header_digest,
        retirement_notice=notice,
    )


def _read_signature_block(root: Path) -> SignatureBlock | None:
    """The bundle's signature block, or ``None`` when it carries none."""

    path = root / SIGNATURE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise SignatureFormatError(
            f"{SIGNATURE_NAME} is not readable JSON: {error}"
        ) from error
    if raw is None:
        raise SignatureFormatError(f"{SIGNATURE_NAME} is empty")
    return require_bundle_signature(raw)
