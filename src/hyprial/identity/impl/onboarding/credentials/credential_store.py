# Decision record (W1E-2, 2026-11-21):
# - Rollback in write_login_credential restores prior bytes atomically via
#   tempfile.mkstemp + os.replace inside HYPRIAL_HOME/secrets instead of
#   in-place write_bytes + os.chmod.
"""The single credential-record store used by the isolated identity worker.

Decision: the credential file format, path rule, read validation, and
restore-on-failure write moved here from the shell login flow.  The shell keeps
a thin conversion wrapper for the original CLI; the worker uses this module
directly.  No daemon module imports it, and it never stores an access token.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write
from hyprial.kernel import configured_hyprial_home

from ..ports import (
    LOGIN_FILENAME,
    SECRETS_DIRNAME,
    IdentityError,
    login_credential_path,
)

CREDENTIAL_VERSION = 1
#: Monotonic identity-commit counter carried in the credential's own atomic
#: write (0.5.5.3.4 ``shell/impl/login/generation.py``); merged into the
#: identity store when release/v0.5.5.3.4 joined release/v0.5.5.4 (per Allen
#: 2026-11-07) so both login paths read, keep and advance the same field.
IDENTITY_GENERATION_KEY = "identityGeneration"
#: Non-secret high-water mark, ``HYPRIAL_HOME/<this>``; survives logout.
HIGH_WATER_FILENAME = ".identity-generation"

_CREDENTIAL_KEYS = frozenset(
    {"version", "issuer", "refreshToken", "obtainedAt", IDENTITY_GENERATION_KEY}
)


@dataclass(frozen=True, slots=True)
class IdentityCredential:
    """The whole ``secrets/login.json`` record: refresh token only."""

    version: int
    issuer: str
    refresh_token: str
    obtained_at: str
    identity_generation: int | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "version": self.version,
            "issuer": self.issuer,
            "refreshToken": self.refresh_token,
            "obtainedAt": self.obtained_at,
        }
        if self.identity_generation is not None:
            record[IDENTITY_GENERATION_KEY] = self.identity_generation
        return record


def credential_path(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> Path:
    """Resolve the credential under the same home rule as settings."""

    env = os.environ if environ is None else environ
    if hyprial_home is not None:
        home = Path(hyprial_home)
    else:
        home = configured_hyprial_home(env)[0]
    return login_credential_path(home)


def read_login_credential(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> IdentityCredential:
    """Load and strictly validate the credential; every defect is loud."""

    path = credential_path(environ, hyprial_home=hyprial_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise IdentityError(
            "NOT_LOGGED_IN",
            f"no login credential at {path}: run hyprial login first",
        ) from error
    except OSError as error:
        raise IdentityError(
            "CREDENTIAL_UNREADABLE", f"cannot read {path}: {error}"
        ) from error
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IdentityError(
            "CREDENTIAL_UNPARSEABLE", f"cannot parse {path}: {error}"
        ) from error
    if not isinstance(record, dict):
        raise IdentityError(
            "CREDENTIAL_UNPARSEABLE", f"{path}: top level is not an object"
        )
    unknown = sorted(set(record) - _CREDENTIAL_KEYS)
    if unknown:
        raise IdentityError(
            "CREDENTIAL_UNPARSEABLE",
            f"{path}: unknown key(s) {unknown}; the credential is a small "
            "closed record",
        )
    if record.get("version") != CREDENTIAL_VERSION:
        raise IdentityError(
            "CREDENTIAL_UNPARSEABLE",
            f"{path}: version must be {CREDENTIAL_VERSION}; "
            f"got {record.get('version')!r}",
        )
    issuer = record.get("issuer")
    refresh = record.get("refreshToken")
    obtained = record.get("obtainedAt")
    for name, value in (
        ("issuer", issuer),
        ("refreshToken", refresh),
        ("obtainedAt", obtained),
    ):
        if not isinstance(value, str) or not value:
            raise IdentityError(
                "CREDENTIAL_UNPARSEABLE",
                f"{path}: {name} must be a non-empty string; got {value!r}",
            )
    generation = record.get(IDENTITY_GENERATION_KEY)
    if generation is not None and (type(generation) is not int or generation <= 0):
        raise IdentityError(
            "CREDENTIAL_UNPARSEABLE",
            f"{path}: identityGeneration must be a positive integer; "
            f"got {generation!r}",
        )
    return IdentityCredential(
        version=CREDENTIAL_VERSION,
        issuer=issuer,
        refresh_token=refresh,
        obtained_at=obtained,
        identity_generation=generation,
    )


def _positive_generation(path: Path) -> int:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return 0
    value = record.get(IDENTITY_GENERATION_KEY) if isinstance(record, dict) else None
    return value if type(value) is int and value > 0 else 0


def read_login_generation(home: Path) -> int | None:
    """The committed non-secret generation in ``secrets/login.json``, or None.

    Only the integer is returned; the refresh token is never exposed.  A
    missing, corrupt or pre-generation record answers None so a caller refuses
    instead of inventing an epoch.  Logout removes the file, so a logged-out
    home has no current epoch even though the high-water mark survives.
    """

    return _positive_generation(credential_path(hyprial_home=Path(home))) or None


def reserve_identity_generation(home: Path) -> int:
    """Reserve the next generation (call under the identity lock, before commit).

    ``max(login.json, high-water) + 1``; the high-water mark is advanced first,
    so a commit that then fails only leaves a gap, never a reused number.
    """

    home = Path(home)
    value = max(
        _positive_generation(credential_path(hyprial_home=home)),
        _positive_generation(home / HIGH_WATER_FILENAME),
    ) + 1
    atomic_json_write(home / HIGH_WATER_FILENAME, {IDENTITY_GENERATION_KEY: value})
    return value


def write_login_credential(credential: IdentityCredential, path: Path) -> None:
    """Validate, write, verify, and restore prior bytes on any failure."""

    payload = credential_record_bytes(credential.as_record())
    try:
        reparsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise IdentityError(
            "CREDENTIAL_INVALID", f"credential record failed round-trip: {error}"
        ) from error
    if reparsed != credential.as_record():
        raise IdentityError("CREDENTIAL_INVALID", "credential round-trip mismatch")
    backup = path.read_bytes() if path.exists() else None
    try:
        atomic_json_write(path, reparsed)
        if path.read_bytes() != payload:
            raise IdentityError(
                "CREDENTIAL_WRITE_CONFLICT",
                f"credential changed during write: {path.name}",
            )
    except BaseException:
        current = path.read_bytes() if path.exists() else None
        if current == payload:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(backup)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        raise


def credential_record_bytes(record: Mapping[str, Any]) -> bytes:
    """Serialize exactly as ``atomic_json_write`` does for verification."""

    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


__all__ = [
    "CREDENTIAL_VERSION",
    "HIGH_WATER_FILENAME",
    "IDENTITY_GENERATION_KEY",
    "LOGIN_FILENAME",
    "SECRETS_DIRNAME",
    "IdentityCredential",
    "credential_path",
    "credential_record_bytes",
    "read_login_credential",
    "reserve_identity_generation",
    "write_login_credential",
]
