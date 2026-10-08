"""AT07 item 2, first phase: what a landing does with the bundle's credentials.

Card 356 item 2 asks for four things at once -- the original owner's credentials
usable on the target, no silent fallback to the host's own key, a credential
that needs refreshing *not* revived behind the operator's back, and an explicit
refusal or re-authentication prompt when the target cannot honour the
credential.

This is the no-decrypt half that cc-sw-review approved: the target never opens
the sealed envelope and nothing here invents a key-custody scheme.  Landing
reads the envelope **header** only (policy plus the per-credential record the
exporter wrote), refuses the landing outright when the declared policy would
fall back to the host key without the host owner's explicit grant, and reports
every credential as ``reauth-required`` -- with ``machine_bound`` /
``requires_reauth`` / ``refreshable`` carried through unchanged, so a receipt
cannot claim a credential was restored when it merely travelled.

Decrypting the envelope on the target is the second phase and stays open until
the owner rules on that scope.

Known limit, stated rather than discovered later: because the envelope is not
opened, its header AEAD is not verified either.  ``resolve_policy`` accepts a
``host`` strategy as soon as ``host_owner_grant`` is a non-empty string, so a
bundle can *declare* an authorisation this target cannot check.  In this phase
that has no effect -- every credential is reported as ``reauth-required`` and
the host key is never used -- but the recorded policy is a declaration, not a
verified grant.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from hyprial.daemon.impl.transfer.archive.bundle import PAYLOAD_DIRNAME, BundleManifest, _safe_relative
from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_SCHEMA_VERSION,
    CredentialPolicy,
    CredentialRecord,
    EnvelopeError,
    EnvelopeFormatError,
    EnvelopeStrategyError,
    load_envelope,
    resolve_policy)

__all__ = [
    "CREDENTIAL_REAUTH_REQUIRED",
    "CredentialEnvelopeInvalid",
    "CredentialEnvelopeMissing",
    "CredentialHostKeyRefused",
    "CredentialLanding",
    "CredentialLandingError",
    "CredentialLandingPlan",
    "plan_credential_landing",
]

#: The only disposition this phase can honestly report: the credential is
#: carried, not restored, so the target has to authenticate again.
CREDENTIAL_REAUTH_REQUIRED = "reauth-required"


class CredentialLandingError(Exception):
    """Base class for credential failures during a landing."""

    code = "credential_landing_error"


class CredentialEnvelopeMissing(CredentialLandingError):
    """The manifest declares a credential envelope the bundle does not carry."""

    code = "credential_envelope_missing"


class CredentialEnvelopeInvalid(CredentialLandingError):
    """The declared envelope is not a readable sealed envelope."""

    code = "credential_envelope_invalid"


class CredentialHostKeyRefused(CredentialLandingError):
    """The declared policy would fall back to the host key without a grant."""

    code = "credential_host_key_refused"


@dataclass(frozen=True)
class CredentialLanding:
    """One credential as the landing receipt reports it."""

    name: str
    disposition: str
    machine_bound: bool
    requires_reauth: bool
    refreshable: bool
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "disposition": self.disposition,
            "machineBound": self.machine_bound,
            "requiresReauth": self.requires_reauth,
            "refreshable": self.refreshable,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CredentialLandingPlan:
    """What a landing will do about credentials, decided before it writes."""

    policy: CredentialPolicy
    credentials: tuple[CredentialLanding, ...]

    @property
    def needs_reauth(self) -> tuple[str, ...]:
        """The credentials the target must re-authenticate before use."""

        return tuple(item.name for item in self.credentials)


def plan_credential_landing(
    manifest: BundleManifest,
    bundle_dir: os.PathLike[str] | str,
) -> CredentialLandingPlan:
    """Read the declared envelope header and say what the target must redo.

    Nothing is decrypted and nothing is written: the point of this phase is to
    refuse a policy the target cannot honour *before* a home or a row exists,
    and to hand the receipt an honest list of credentials that need attention.
    """

    declared = manifest.credential_envelope
    if not declared:
        return CredentialLandingPlan(policy=resolve_policy(), credentials=())
    path = _envelope_path(bundle_dir, declared)
    if not path.is_file():
        raise CredentialEnvelopeMissing(
            f"manifest declares credential envelope {declared!r} but the bundle "
            "does not carry it"
        )
    try:
        envelope = load_envelope(path)
    except (EnvelopeError, OSError, ValueError) as error:
        raise CredentialEnvelopeInvalid(
            f"credential envelope {declared!r} is not a readable sealed "
            f"envelope: {error}"
        ) from error

    # Reading public header declarations still requires a supported format
    # and agreement with the already-validated bundle identity. This does not
    # authenticate the header or turn a declared grant into authorization.
    if envelope.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        raise CredentialEnvelopeInvalid("unsupported credential envelope schema")
    if envelope.get("algorithm") != ENVELOPE_ALGORITHM:
        raise CredentialEnvelopeInvalid("unsupported credential envelope algorithm")
    for field in ("owner", "actor"):
        if envelope.get(field) != getattr(manifest, field):
            raise CredentialEnvelopeInvalid(
                f"credential envelope {field} contradicts the bundle manifest"
            )
    policy = _declared_policy(envelope)
    return CredentialLandingPlan(
        policy=policy,
        credentials=_landing_records(envelope),
    )


def _envelope_path(
    bundle_dir: os.PathLike[str] | str, declared: str
) -> Path:
    """Resolve a declared envelope path the same way ``validate_bundle`` does."""

    try:
        safe = _safe_relative(declared)
    except Exception as error:  # noqa: BLE001 - one message for every refusal
        raise CredentialEnvelopeInvalid(
            f"credential envelope path {declared!r} is not bundle-relative: {error}"
        ) from error
    parts = PurePosixPath(safe).parts
    if parts and parts[0] == PAYLOAD_DIRNAME:
        parts = parts[1:]
    if not parts:
        raise CredentialEnvelopeInvalid(
            f"credential envelope path {declared!r} names no file"
        )
    return Path(bundle_dir) / PAYLOAD_DIRNAME / Path(*parts)


def _declared_policy(envelope: Mapping[str, object]) -> CredentialPolicy:
    """Vet the envelope's own policy: the host key is never an implicit default."""

    raw = envelope.get("policy")
    if not isinstance(raw, Mapping):
        raise CredentialEnvelopeInvalid("envelope header carries no policy")
    strategy = raw.get("strategy")
    if not isinstance(strategy, str) or not strategy:
        raise CredentialEnvelopeInvalid("envelope policy carries no strategy")
    grant = raw.get("host_owner_grant")
    if grant is not None and not isinstance(grant, str):
        raise CredentialEnvelopeInvalid("host_owner_grant must be a string or absent")
    try:
        return resolve_policy(requested=strategy, host_owner_grant=grant)
    except EnvelopeStrategyError as error:
        raise CredentialHostKeyRefused(str(error)) from error


def _landing_records(
    envelope: Mapping[str, object],
) -> tuple[CredentialLanding, ...]:
    raw_records = envelope.get("credentials") or []
    if not isinstance(raw_records, list):
        raise CredentialEnvelopeInvalid("envelope credentials must be a list")
    records: list[CredentialLanding] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise CredentialEnvelopeInvalid("envelope credential must be an object")
        try:
            record = CredentialRecord.from_dict(raw)
        except EnvelopeFormatError as error:
            raise CredentialEnvelopeInvalid(str(error)) from error
        records.append(
            CredentialLanding(
                name=record.name,
                disposition=CREDENTIAL_REAUTH_REQUIRED,
                machine_bound=record.machine_bound,
                requires_reauth=record.requires_reauth,
                refreshable=record.refreshable,
                notes=record.notes,
            )
        )
    return tuple(records)
