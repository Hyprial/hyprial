"""AT07 item 2: what a landing does with the bundle's credentials.

Card 356 item 2 asks for four things at once -- the original owner's credentials
usable on the target, no silent fallback to the host's own key, a credential
that needs refreshing *not* revived behind the operator's back, and an explicit
refusal or re-authentication prompt when the target cannot honour the
credential.

Two phases live here, and which one runs is a fact the plan records:

* **No key (first phase).**  The target reads the envelope *header* only
  (policy plus the per-credential records the exporter wrote), refuses a
  ``host`` policy that carries no declared grant, and reports every credential
  as ``reauth-required`` -- a receipt must never claim a credential was
  restored when it merely travelled.
* **With the target's recipient key (second phase; owner ruling 2026-10-14).**
  The envelope is opened with the target machine's private half, the header is
  AEAD-verified (the policy stops being a declaration the moment the open
  succeeds), and every record that declares an install target, is not marked
  machine-bound or reauth-required, and carries a sealed string value becomes
  ``restorable``.  ``apply_credential_landing`` then writes those into the
  landed home; a receipt says ``restored`` only for what was actually written.

Host-key fallback, unchanged and deliberate: the host's own key is never used
on the strength of the envelope's own declaration.  A ``host`` policy without
a declared grant is refused; with one it is recorded as declared, and no
credential is installed from the bundle either way.  Any *use* of the host key
is a separate act that must take an explicit host-owner approval at the target
(owner ruling 2026-10-14); nothing in this module performs it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from hyprial.identity import secret_environment_name_is_allowed

from hyprial.daemon.impl.transfer.archive.bundle import (
    PAYLOAD_DIRNAME,
    BundleManifest,
    _safe_relative,
)
from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_SCHEMA_VERSION,
    CredentialInstall,
    CredentialPolicy,
    CredentialRecord,
    EnvelopeError,
    EnvelopeFormatError,
    EnvelopeStrategyError,
    load_envelope,
    open_envelope,
    resolve_policy,
)
from hyprial.daemon.impl.transfer.archive.signature import ensure_private_directory

__all__ = [
    "CREDENTIAL_REAUTH_REQUIRED",
    "CREDENTIAL_RESTORABLE",
    "CREDENTIAL_RESTORED",
    "CredentialApplyResult",
    "CredentialBinding",
    "CredentialEnvelopeInvalid",
    "CredentialEnvelopeMissing",
    "CredentialHostKeyRefused",
    "CredentialInstallError",
    "CredentialLanding",
    "CredentialLandingError",
    "CredentialLandingPlan",
    "apply_credential_landing",
    "plan_credential_landing",
]

#: The credential is carried (or was refused installation), so the target has
#: to authenticate again before use.
CREDENTIAL_REAUTH_REQUIRED = "reauth-required"
#: Planned only: the sealed value is present and has an install target, so a
#: real landing will write it.  A dry-run receipt reports this, never
#: ``restored``, because nothing was written.
CREDENTIAL_RESTORABLE = "restorable"
#: Written into the landed home (or bound through the target's secret
#: registry) by ``apply_credential_landing``.
CREDENTIAL_RESTORED = "restored"


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


class CredentialInstallError(CredentialLandingError):
    """A credential the plan marked restorable could not be written."""

    code = "credential_install_error"


@dataclass(frozen=True)
class CredentialLanding:
    """One credential as the landing receipt reports it.

    ``install`` is the envelope header's declared destination: non-secret,
    already public in the sealed header, and carried so a receipt can say
    *where* a credential would land.  The credential's value travels only in
    the plan, never in this record.
    """

    name: str
    disposition: str
    machine_bound: bool
    requires_reauth: bool
    refreshable: bool
    notes: str = ""
    install: CredentialInstall | None = None

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "name": self.name,
            "disposition": self.disposition,
            "machineBound": self.machine_bound,
            "requiresReauth": self.requires_reauth,
            "refreshable": self.refreshable,
            "notes": self.notes,
        }
        if self.install is not None:
            body["install"] = self.install.as_dict()
        return body


@dataclass(frozen=True)
class CredentialLandingPlan:
    """What a landing will do about credentials, decided before it writes.

    ``payload`` is the opened, decrypted credential text -- populated only
    when the envelope opened with this machine's recipient key.  It is kept
    out of ``repr`` and is never serialised: receipts report dispositions and
    install targets, never a value.
    """

    policy: CredentialPolicy
    credentials: tuple[CredentialLanding, ...]
    payload: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    @property
    def needs_reauth(self) -> tuple[str, ...]:
        """The credentials the target must re-authenticate before use."""

        return tuple(
            item.name
            for item in self.credentials
            if item.disposition == CREDENTIAL_REAUTH_REQUIRED
        )


def plan_credential_landing(
    manifest: BundleManifest,
    bundle_dir: os.PathLike[str] | str,
    *,
    recipient_key: bytes | None = None,
) -> CredentialLandingPlan:
    """Decide what this landing can do with the bundle's credentials.

    Without ``recipient_key`` (first phase) only the public header is read and
    every credential is reported ``reauth-required``.  With the target's
    X25519 private key the envelope is opened and AEAD-verified, and each
    record that declares an install target, is not machine-bound or bound to a
    fresh sign-in, and carries a sealed string value becomes ``restorable``.
    A key that cannot open the envelope (sealed for another target, tampered
    ciphertext, changed header) degrades every record to ``reauth-required``
    with the reason recorded: an unreadable envelope must not fail the landing
    or be reported as installed.
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
    for name in ("owner", "actor"):
        if envelope.get(name) != getattr(manifest, name):
            raise CredentialEnvelopeInvalid(
                f"credential envelope {name} contradicts the bundle manifest"
            )
    policy = _declared_policy(envelope)
    records = _landing_records(envelope)
    if recipient_key is None or policy.strategy != "original-user":
        # A host-key policy, granted or refused, never installs the bundle's
        # own credentials: it only records that the declared fallback stood.
        return CredentialLandingPlan(policy=policy, credentials=records)
    sealed = _open_with_recipient_key(envelope, recipient_key)
    if sealed is None:
        return CredentialLandingPlan(
            policy=policy,
            credentials=tuple(
                replace(
                    item,
                    notes=_join_notes(
                        item.notes,
                        "the sealed envelope could not be opened with this "
                        "machine's recipient key",
                    ),
                )
                for item in records
            ),
        )
    decided, payload = _phase_two_records(records, sealed)
    return CredentialLandingPlan(policy=policy, credentials=decided, payload=payload)


def _envelope_path(bundle_dir: os.PathLike[str] | str, declared: str) -> Path:
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
    seen: set[str] = set()
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise CredentialEnvelopeInvalid("envelope credential must be an object")
        try:
            record = CredentialRecord.from_dict(raw)
        except EnvelopeFormatError as error:
            raise CredentialEnvelopeInvalid(str(error)) from error
        if record.name in seen:
            raise CredentialEnvelopeInvalid(
                f"envelope declares credential {record.name!r} twice"
            )
        seen.add(record.name)
        records.append(
            CredentialLanding(
                name=record.name,
                disposition=CREDENTIAL_REAUTH_REQUIRED,
                machine_bound=record.machine_bound,
                requires_reauth=record.requires_reauth,
                refreshable=record.refreshable,
                notes=record.notes,
                install=record.install,
            )
        )
    return tuple(records)


def _open_with_recipient_key(
    envelope: Mapping[str, object], recipient_key: bytes
) -> dict[str, object] | None:
    """Open the envelope, or return ``None`` for every way a key cannot."""

    try:
        return open_envelope(envelope, recipient_private_key=recipient_key)
    except (EnvelopeError, ValueError):
        return None


def _phase_two_records(
    records: tuple[CredentialLanding, ...],
    sealed: Mapping[str, object],
) -> tuple[tuple[CredentialLanding, ...], dict[str, str]]:
    """Split records into install-now and re-authenticate-later decisions."""

    decided: list[CredentialLanding] = []
    payload: dict[str, str] = {}
    for item in records:
        blockers: list[str] = []
        if item.install is None:
            blockers.append("the envelope names no install target")
        if item.install is not None and item.install.kind == "env":
            rejected = [
                name
                for name in item.install.environment
                if not secret_environment_name_is_allowed(name)
            ]
            if rejected:
                blockers.append(
                    "the environment name(s) "
                    + ", ".join(rejected)
                    + " are not ones this target can bind"
                )
        if item.machine_bound:
            blockers.append("it is marked machine-bound")
        if item.requires_reauth:
            blockers.append("it is marked as needing a fresh sign-in")
        value = sealed.get(item.name)
        if not isinstance(value, str) or not value:
            blockers.append("the sealed envelope carries no value for it")
        if blockers:
            decided.append(
                replace(
                    item,
                    notes=_join_notes(
                        item.notes, "not restorable: " + "; ".join(blockers)
                    ),
                )
            )
            continue
        decided.append(replace(item, disposition=CREDENTIAL_RESTORABLE))
        payload[item.name] = value
    return tuple(decided), payload


def _join_notes(existing: str, addition: str) -> str:
    return "; ".join(part for part in (existing, addition) if part)


@dataclass(frozen=True)
class CredentialBinding:
    """An env credential that was written and needs a grant on the target."""

    entry_id: str
    environment: tuple[str, ...]
    field: str | None = None


@dataclass(frozen=True)
class CredentialApplyResult:
    """What ``apply_credential_landing`` wrote.

    ``written`` names the files so a caller that has to undo the landing can
    take them back too; it is not part of any receipt.
    """

    plan: CredentialLandingPlan
    bindings: tuple[CredentialBinding, ...] = ()
    written: tuple[Path, ...] = ()


def apply_credential_landing(
    home: os.PathLike[str] | str,
    plan: CredentialLandingPlan,
) -> CredentialApplyResult:
    """Write every ``restorable`` credential into the landed home.

    A first-phase plan carries no payload, so nothing is written and every
    disposition stays ``reauth-required``.  A restorable record whose sealed
    value is missing falls back to ``reauth-required`` rather than claiming a
    restore that did not happen.
    """

    home_path = Path(home)
    decided: list[CredentialLanding] = []
    bindings: list[CredentialBinding] = []
    written: list[Path] = []
    for item in plan.credentials:
        if item.disposition != CREDENTIAL_RESTORABLE:
            decided.append(item)
            continue
        install = item.install
        value = plan.payload.get(item.name)
        if install is None or not isinstance(value, str) or not value:
            decided.append(
                replace(
                    item,
                    disposition=CREDENTIAL_REAUTH_REQUIRED,
                    notes=_join_notes(item.notes, "nothing installable was sealed"),
                )
            )
            continue
        target = _install_path(home_path, item.name, install)
        try:
            _write_secret_text(target, value, field_name=install.field)
        except OSError as error:
            raise CredentialInstallError(
                f"could not install credential {item.name!r}: {error}"
            ) from error
        written.append(target)
        if install.kind == "env":
            bindings.append(
                CredentialBinding(
                    entry_id=item.name,
                    environment=install.environment,
                    field=install.field,
                )
            )
        decided.append(replace(item, disposition=CREDENTIAL_RESTORED))
    return CredentialApplyResult(
        plan=replace(plan, credentials=tuple(decided)),
        bindings=tuple(bindings),
        written=tuple(written),
    )


def _install_path(home: Path, name: str, install: CredentialInstall) -> Path:
    if install.kind == "native-file":
        return (
            home
            / "secrets"
            / "native"
            / install.harness
            / Path(*PurePosixPath(install.path).parts)
        )
    # ``CredentialRecord.from_dict`` refuses an env install whose record name
    # is not a safe entry segment, so the name can be a file under secrets/.
    return home / "secrets" / name


def _write_secret_text(path: Path, value: str, *, field_name: str | None) -> None:
    """Write one secret at 0600: raw text, or one named JSON field."""

    text = (
        value
        if field_name is None
        else json.dumps({field_name: value}, sort_keys=True) + "\n"
    )
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
