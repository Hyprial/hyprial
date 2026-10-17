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
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hyprial.daemon.impl.transfer.archive.signature import ensure_private_directory
from hyprial.kernel import atomic_json_write

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "ENVELOPE_ALGORITHM",
    "PROTOTYPE_PROTOCOL",
    "EnvelopeError",
    "EnvelopeFormatError",
    "EnvelopeOpenError",
    "EnvelopeStrategyError",
    "CredentialInstall",
    "CredentialRecord",
    "CredentialPolicy",
    "EnvCredential",
    "env_credential_name",
    "envelope_header_digest",
    "generate_keypair",
    "seal_envelope",
    "open_envelope",
    "load_envelope",
    "write_envelope",
    "resolve_policy",
    "RECIPIENT_KEY_ALGORITHM",
    "RECIPIENT_KEY_RELPATH",
    "RECIPIENT_KEY_SCHEMA_VERSION",
    "RecipientKeyError",
    "ensure_recipient_key",
    "generate_recipient_key",
    "read_recipient_key",
    "recipient_key_path",
    "recipient_key_record",
    "recipient_public_key_b64",
    "write_recipient_key",
    "ENVELOPE_RELPATH",
    "KNOWN_NATIVE_CREDENTIAL_PATHS",
    "CredentialSealPlan",
    "envelope_json_bytes",
    "plan_credential_seal",
    "seal_credential_envelope",
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


#: Mirrors ``runtime.secrets``' safe entry segment: the name becomes a file
#: name under the landed home's ``secrets/`` directory, so it must not be able
#: to name a parent directory or an absolute path.
_ENTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HARNESS_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class CredentialInstall:
    """Where a restored credential is installed on the target.

    Phase 2 (owner ruling 2026-10-14) installs the sealed plaintext into the
    landed agent home.  Two kinds are defined; a record without one stays
    ``reauth-required``, because an uninstallable credential must not be
    reported as restored:

    * ``native-file`` -- written under ``<home>/secrets/native/<harness>/<path>``
      (0600), the projection the harness's own credential file reads at launch;
    * ``env`` -- written to ``<home>/secrets/<name>`` (0600) and bound to the
      named environment variables through the target's secret registry.
    """

    kind: str
    harness: str = ""
    path: str = ""
    environment: tuple[str, ...] = ()
    field: str | None = None

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"kind": self.kind}
        if self.kind == "native-file":
            body["harness"] = self.harness
            body["path"] = self.path
        else:
            body["environment"] = list(self.environment)
            if self.field is not None:
                body["field"] = self.field
        return body

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CredentialInstall":
        kind = raw.get("kind")
        if kind == "native-file":
            harness = raw.get("harness")
            if not isinstance(harness, str) or not _HARNESS_PATTERN.fullmatch(harness):
                raise EnvelopeFormatError(
                    "credential install harness must be a lowercase name"
                )
            path = raw.get("path")
            if not isinstance(path, str) or not path:
                raise EnvelopeFormatError("credential install path must be a string")
            parts = PurePosixPath(path).parts
            if (
                path.startswith("/")
                or "\\" in path
                or not parts
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise EnvelopeFormatError(
                    "credential install path must be home-relative with no "
                    "parent or absolute segments"
                )
            return cls(kind="native-file", harness=harness, path="/".join(parts))
        if kind == "env":
            names = raw.get("environment")
            if not isinstance(names, (list, tuple)) or not names:
                raise EnvelopeFormatError(
                    "credential install environment must be a non-empty list"
                )
            cleaned: list[str] = []
            for name in names:
                if not isinstance(name, str) or not _ENVIRONMENT_PATTERN.fullmatch(
                    name
                ):
                    raise EnvelopeFormatError(
                        "credential install environment names must be upper-case "
                        "environment names"
                    )
                cleaned.append(name)
            field = raw.get("field")
            if field is not None and (
                not isinstance(field, str) or not _ENTRY_PATTERN.fullmatch(field)
            ):
                raise EnvelopeFormatError(
                    "credential install field must be a safe entry name"
                )
            return cls(kind="env", environment=tuple(cleaned), field=field)
        raise EnvelopeFormatError(
            f"unknown credential install kind {kind!r}; expected 'native-file' or 'env'"
        )


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
    install: CredentialInstall | None = None

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "name": self.name,
            "machine_bound": self.machine_bound,
            "requires_reauth": self.requires_reauth,
            "refreshable": self.refreshable,
            "notes": self.notes,
        }
        if self.install is not None:
            body["install"] = self.install.as_dict()
        return body

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CredentialRecord":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise EnvelopeFormatError("credential record needs a non-empty name")
        install_raw = raw.get("install")
        install = None
        if install_raw is not None:
            if not isinstance(install_raw, Mapping):
                raise EnvelopeFormatError("credential install must be an object")
            install = CredentialInstall.from_dict(install_raw)
            if install.kind == "env" and not _ENTRY_PATTERN.fullmatch(name):
                raise EnvelopeFormatError(
                    "an env install names the secret entry, so the credential "
                    "name must be a safe entry segment"
                )
        return cls(
            name=name,
            machine_bound=bool(raw.get("machine_bound", True)),
            requires_reauth=bool(raw.get("requires_reauth", False)),
            refreshable=bool(raw.get("refreshable", False)),
            notes=str(raw.get("notes", "")),
            install=install,
        )


@dataclass(frozen=True)
class EnvCredential:
    """One environment credential the *daemon* resolved for the export.

    The export side has no registry of its own.  An env-key agent's credential
    is an explicitly authorized secret (``secrets.py``'s approved vocabulary),
    and the only correct way to read it is the daemon's own resolver -- the
    same grant + incarnation gate the launch uses.  The quiesce answer therefore
    publishes these records; the CLI carries them into ``plan_credential_seal``
    and never touches the value's file itself.

    ``value`` is plaintext by design (it is the envelope's input), kept out of
    ``repr`` and never written to a receipt or log.  ``requires_reauth`` is the
    degraded record: the grant existed but the gate could not produce a value,
    so the target is told to re-authenticate instead of silently losing it.
    ``exclude`` is the home-relative plaintext path the export must keep out of
    the payload (agent-private sources only; a user-provider entry lives
    outside the agent home and is not exported at all).
    """

    harness: str
    entry_id: str
    environment: tuple[str, ...]
    field_name: str | None = None
    value: str | None = field(default=None, repr=False, compare=False)
    requires_reauth: bool = False
    notes: str = ""
    exclude: str | None = None

    @property
    def name(self) -> str:
        return env_credential_name(self.harness, self.entry_id)

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "name": self.name,
            "harness": self.harness,
            "entryId": self.entry_id,
            "environment": list(self.environment),
            "field": self.field_name,
            "requiresReauth": self.requires_reauth,
            "notes": self.notes,
        }
        if self.value is not None:
            body["value"] = self.value
        if self.exclude is not None:
            body["exclude"] = self.exclude
        return body

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "EnvCredential":
        harness = raw.get("harness")
        if not isinstance(harness, str) or not _HARNESS_PATTERN.fullmatch(harness):
            raise EnvelopeFormatError(
                "env credential harness must be a lowercase name"
            )
        entry_id = raw.get("entryId")
        if not isinstance(entry_id, str) or not _ENTRY_PATTERN.fullmatch(entry_id):
            raise EnvelopeFormatError("env credential entryId must be a safe segment")
        names = raw.get("environment")
        if not isinstance(names, (list, tuple)) or not names:
            raise EnvelopeFormatError(
                "env credential environment must be a non-empty list"
            )
        cleaned: list[str] = []
        for name in names:
            if not isinstance(name, str) or not _ENVIRONMENT_PATTERN.fullmatch(name):
                raise EnvelopeFormatError(
                    "env credential environment names must be upper-case names"
                )
            cleaned.append(name)
        field_name = raw.get("field")
        if field_name is not None and (
            not isinstance(field_name, str) or not _ENTRY_PATTERN.fullmatch(field_name)
        ):
            raise EnvelopeFormatError(
                "env credential field must be a safe entry name"
            )
        value = raw.get("value")
        if value is not None and (not isinstance(value, str) or not value):
            raise EnvelopeFormatError(
                "env credential value must be a non-empty string or absent"
            )
        exclude = raw.get("exclude")
        if exclude is not None and (not isinstance(exclude, str) or not exclude):
            raise EnvelopeFormatError(
                "env credential exclude must be a non-empty home-relative path"
            )
        return cls(
            harness=harness,
            entry_id=entry_id,
            environment=tuple(cleaned),
            field_name=field_name,
            value=value,
            requires_reauth=bool(raw.get("requiresReauth", False)),
            notes=str(raw.get("notes", "")),
            exclude=exclude,
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


def envelope_header_digest(envelope: Mapping[str, object]) -> str:
    """``sha256:<hex>`` over the header metadata the AEAD authenticates.

    The bundle signature binds this digest so a verifier records *which*
    envelope header it accepted, not just which file bytes arrived.  The
    associated data's caller-supplied ``extra`` is not stored in the envelope,
    so it cannot be part of a reproducible digest; the authenticated header
    fields can be, and those are the ones that carry the policy and identity.
    """

    missing = [name for name in _AAD_FIELDS if name not in envelope]
    if missing:
        raise EnvelopeFormatError(
            f"envelope header is missing {missing}; it has no reproducible digest"
        )
    return "sha256:" + hashlib.sha256(_aad(envelope, b"")).hexdigest()


def _normalise_credentials(
    credentials: Iterable[CredentialRecord | Mapping[str, object]],
) -> list[CredentialRecord]:
    records: list[CredentialRecord] = []
    for item in credentials:
        records.append(
            item
            if isinstance(item, CredentialRecord)
            else CredentialRecord.from_dict(item)
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
    recipient_public = x25519.X25519PublicKey.from_public_bytes(recipient_public_key)
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

    sender_public = _unb64(
        envelope.get("sender_public_key"), "sender_public_key", length=32
    )
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


def write_envelope(
    path: os.PathLike[str] | str, envelope: Mapping[str, object]
) -> Path:
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


# ---------------------------------------------------------------------------
# AT07 phase 2: the target machine's credential-envelope recipient key.
#
# The sealed envelope carries the source owner's credentials as ciphertext for
# one recipient: whoever holds the X25519 private key behind the
# ``--recipient-public-key`` the source sealed to.  Phase 2 (owner ruling
# 2026-10-14) puts that private key on the **target machine**: it lives beside
# the transfer signing key under the same 0600/0700 discipline, so another user
# on the target cannot read it -- a copied bundle stays worthless to them.
#
# Custody rules this section carries:
#
# * the target operator mints and publishes only the public half before a
#   transfer (that is the key the source seals to); the private half never
#   leaves the machine and is never written into a bundle, a receipt or a log;
# * a record whose public half does not match its private half is refused by
#   name -- opening with the wrong key would otherwise look like tampering;
# * minting is explicit and idempotent (``ensure_recipient_key``); nothing here
#   rotates a key, and nothing here mints a *new* key at landing time, because
#   a key minted after the source sealed cannot open the envelope.
# ---------------------------------------------------------------------------

#: Same home-relative root as the signing key: the machine's own transfer
#: secrets, never the agent home and never a bundle payload.
RECIPIENT_KEY_RELPATH = "secrets/transfer/recipient-key.json"
RECIPIENT_KEY_ALGORITHM = "x25519"
RECIPIENT_KEY_SCHEMA_VERSION = 1


class RecipientKeyError(EnvelopeError):
    """The recipient key record is missing or disagrees with itself."""

    code = "envelope_recipient_key_invalid"


def _recipient_unb64(
    value: object, label: str, *, length: int | None = None
) -> bytes:
    if not isinstance(value, str):
        raise RecipientKeyError(f"{label} must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise RecipientKeyError(f"{label} is not valid base64") from error
    if length is not None and len(raw) != length:
        raise RecipientKeyError(f"{label} must be {length} bytes")
    return raw


def generate_recipient_key() -> tuple[bytes, bytes]:
    """Return ``(private_bytes, public_bytes)`` for a fresh X25519 keypair."""

    return generate_keypair()


def recipient_key_path(home: os.PathLike[str] | str) -> Path:
    """Where this machine's credential-envelope recipient key lives (0600)."""

    return Path(home) / RECIPIENT_KEY_RELPATH


def recipient_key_record(
    private_bytes: bytes, public_bytes: bytes
) -> dict[str, object]:
    """The on-disk record: both halves, so opening never needs a second read."""

    return {
        "schemaVersion": RECIPIENT_KEY_SCHEMA_VERSION,
        "algorithm": RECIPIENT_KEY_ALGORITHM,
        "publicKey": _b64(public_bytes),
        "privateKey": _b64(private_bytes),
    }


def write_recipient_key(
    path: os.PathLike[str] | str, private_bytes: bytes, public_bytes: bytes
) -> None:
    """Persist a recipient key at mode 0600 under 0700 secret directories."""

    target = Path(path)
    ensure_private_directory(target.parent)
    atomic_json_write(target, recipient_key_record(private_bytes, public_bytes))


def read_recipient_key(path: os.PathLike[str] | str) -> tuple[bytes, bytes]:
    """Load ``(private_bytes, public_bytes)``, refusing an inconsistent file."""

    target = Path(path)
    try:
        raw = json.loads(target.read_text("utf-8"))
    except FileNotFoundError:
        raise RecipientKeyError(f"no recipient key at {target}") from None
    except ValueError as error:
        raise RecipientKeyError(
            f"recipient key at {target} is not valid JSON"
        ) from error
    if not isinstance(raw, Mapping):
        raise RecipientKeyError("recipient key record must be an object")
    if raw.get("schemaVersion") != RECIPIENT_KEY_SCHEMA_VERSION:
        raise RecipientKeyError("recipient key record has an unknown schemaVersion")
    if raw.get("algorithm") != RECIPIENT_KEY_ALGORITHM:
        raise RecipientKeyError(
            f"recipient key algorithm must be {RECIPIENT_KEY_ALGORITHM!r}"
        )
    private_bytes = _recipient_unb64(raw.get("privateKey"), "privateKey", length=32)
    public_bytes = _recipient_unb64(raw.get("publicKey"), "publicKey", length=32)
    derived = (
        x25519.X25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes_raw()
    )
    if derived != public_bytes:
        raise RecipientKeyError(
            "recipient key record disagrees with itself: the public half does "
            "not match the private half"
        )
    return (private_bytes, public_bytes)


def ensure_recipient_key(path: os.PathLike[str] | str) -> tuple[bytes, bytes]:
    """Load the key at ``path``, minting one on first use."""

    target = Path(path)
    if target.exists():
        return read_recipient_key(target)
    private_bytes, public_bytes = generate_recipient_key()
    write_recipient_key(target, private_bytes, public_bytes)
    return (private_bytes, public_bytes)


def recipient_public_key_b64(public_bytes: bytes) -> str:
    """The published half, spelled exactly as ``--recipient-public-key`` reads it."""

    if len(public_bytes) != 32:
        raise RecipientKeyError("an x25519 public key is 32 bytes")
    return _b64(public_bytes)


# ---------------------------------------------------------------------------
# AT06 credential carry: seal an agent home's credentials for one target.
#
# The export-side counterpart of ``landing/credentials.py``.  The owner ruling
# of 2026-10-14 fixed the rule this section implements: an agent's own
# credentials travel with its bundle, sealed to the target machine's X25519
# key, and the *plaintext never rides the transport*.  Landing installs what
# it can open with the key it holds; anything unreadable degrades there to
# ``reauth-required``.
#
# Collection is deliberately narrow.  Only the files the agent-home layout
# names as credential-bearing (``secrets/native/<harness>/<file>``) are
# sealed -- and, at the same time, kept out of the plaintext payload through
# ``export_bundle(exclude_paths=...)``.  Everything else in the home travels
# as before: a directory named ``secrets/`` is not a credential by itself,
# and excluding one wholesale would drop the agent's own state with it.
#
# The envelope is *declared* by the manifest (``credential_envelope``) and
# rides the payload as a registered entry under ``secrets/transfer/`` -- an
# existing top-level tree of a registry home, so the landing's own topology
# gate accepts it without widening what a home may hold.
# ---------------------------------------------------------------------------

#: Where the sealed envelope travels inside the agent home the bundle carries.
#: Bundle-relative; ``validate_bundle`` resolves a declaration exactly here.
ENVELOPE_RELPATH = "secrets/transfer/envelope.json"

#: Credential-bearing native files per harness, relative to the harness's
#: native root (``<home>/secrets/native/<harness>/``).  Mirrors
#: ``hyprial.identity.impl.agents.home.config._KNOWN_NATIVE_CREDENTIAL_PATHS``:
#: the home projection refuses to freeze these paths into agent config, so the
#: envelope is the only way they can travel.  A test keeps the two tables
#: equal so they cannot drift apart.
KNOWN_NATIVE_CREDENTIAL_PATHS: dict[str, frozenset[str]] = {
    "claude": frozenset({".credentials.json"}),
    "codex": frozenset({"auth.json"}),
    "pi": frozenset({"auth.json", "models.json", "models-store.json"}),
}


@dataclass(frozen=True)
class CredentialSealPlan:
    """What the export found to seal, and what must not ride in the clear.

    ``secrets`` is plaintext by design -- it is the envelope's input, kept out
    of ``repr`` and never serialised anywhere else.  ``excluded`` names the
    home-relative paths the export must skip: exactly the files whose values
    went into the envelope, so no sealed value stays behind in the payload.
    """

    records: tuple[CredentialRecord, ...]
    secrets: Mapping[str, str] = field(repr=False, compare=False)
    excluded: tuple[str, ...] = ()


def _record_name(harness: str, filename: str) -> str:
    """A receipt-readable, collision-free name for one native credential."""

    return f"native-{harness}-{filename.lstrip('.')}"


def env_credential_name(harness: str, entry_id: str) -> str:
    """A readable, collision-free record name for one env credential.

    The name becomes the secret entry under the landed home's ``secrets/`` and
    the grant's entry id, so it must be a safe entry segment (both halves are
    already constrained) and stable across the two machines.
    """

    name = f"env-{harness}-{entry_id}"
    if not _ENTRY_PATTERN.fullmatch(name):
        raise EnvelopeFormatError(
            f"env credential name {name!r} is not a safe entry segment"
        )
    return name


def plan_credential_seal(
    home: str | Path,
    *,
    env_credentials: Iterable[EnvCredential] = (),
) -> CredentialSealPlan:
    """Find this home's credential files and plan their sealed carry.

    Only regular files are sealed.  A credential that cannot be read as UTF-8
    text is still *excluded from the payload* -- shipping it in the clear
    would defeat the envelope -- and its record says the target must
    re-authenticate, so a receipt never claims a restore that did not happen.

    ``env_credentials`` are the environment credentials the source daemon
    resolved through its own grant + incarnation gate; each one becomes an
    ``env`` install record.  A value the gate could not resolve becomes a
    ``reauth-required`` record rather than being dropped, and its agent-private
    plaintext (when there is one) is still excluded from the payload.  A
    harness whose own credential file was sealed here wins: Claude refuses a
    native credential and an explicit auth env in one launch, so carrying both
    would land a pair the target cannot start.
    """

    root = Path(home)
    records: list[CredentialRecord] = []
    secrets: dict[str, str] = {}
    excluded: list[str] = []
    for harness in sorted(KNOWN_NATIVE_CREDENTIAL_PATHS):
        for filename in sorted(KNOWN_NATIVE_CREDENTIAL_PATHS[harness]):
            path = root / "secrets" / "native" / harness / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            excluded.append(relative)
            name = _record_name(harness, filename)
            try:
                value = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                records.append(
                    CredentialRecord(
                        name=name,
                        machine_bound=False,
                        requires_reauth=True,
                        notes=(
                            f"could not be sealed as text ({type(error).__name__}); "
                            "kept out of the bundle, the target must "
                            "re-authenticate"
                        ),
                    )
                )
                continue
            records.append(
                CredentialRecord(
                    name=name,
                    machine_bound=False,
                    requires_reauth=False,
                    notes=f"sealed from {relative}",
                    install=CredentialInstall(
                        kind="native-file", harness=harness, path=filename
                    ),
                )
            )
            secrets[name] = value
    sealed_harnesses = {
        record.install.harness
        for record in records
        if record.install is not None and record.install.kind == "native-file"
    }
    for credential in env_credentials:
        if credential.exclude is not None:
            excluded.append(credential.exclude)
        if credential.harness in sealed_harnesses:
            continue
        name = credential.name
        if name in secrets or any(record.name == name for record in records):
            continue
        if credential.requires_reauth or not credential.value:
            records.append(
                CredentialRecord(
                    name=name,
                    machine_bound=False,
                    requires_reauth=True,
                    notes=credential.notes
                    or "the daemon gate resolved no value; the target must "
                    "re-authenticate",
                )
            )
            continue
        records.append(
            CredentialRecord(
                name=name,
                machine_bound=False,
                requires_reauth=False,
                notes=credential.notes,
                install=CredentialInstall(
                    kind="env",
                    environment=credential.environment,
                    field=credential.field_name,
                ),
            )
        )
        secrets[name] = credential.value
    return CredentialSealPlan(
        records=tuple(records), secrets=secrets, excluded=tuple(excluded)
    )


def seal_credential_envelope(
    plan: CredentialSealPlan,
    *,
    actor: str,
    owner: str,
    created_at: str,
    recipient_public_key: bytes,
    policy: CredentialPolicy | None = None,
) -> dict[str, object]:
    """Seal the plan's values so only the named target can open them."""

    return seal_envelope(
        dict(plan.secrets),
        recipient_public_key=recipient_public_key,
        actor=actor,
        owner=owner,
        created_at=created_at,
        records=plan.records,
        policy=policy if policy is not None else resolve_policy(),
    )


def envelope_json_bytes(envelope: Mapping[str, object]) -> bytes:
    """The bytes ``write_envelope`` would put on disk, as a payload entry."""

    return (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode("utf-8")
