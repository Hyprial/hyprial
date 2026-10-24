"""Local user-login session inspection and removal.

A home is logged in only when its validated ``secrets/login.json`` credential,
its persisted Casdoor identity, and the selected profile all name the same
issuer.  This is deliberately local and offline: the refresh token is opaque,
so its expiry is unknown until an issuer is contacted.

Logout owns only the files and settings fields written by the user login.  It
never traverses agent homes and never removes Tailcat device material.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write
from hyprial.shell.impl.login.flow import credential_path, read_login_credential
from hyprial.shell.impl.login.oidc import LoginError

_LOGIN_SETTINGS_FIELDS = (
    "identityBinding",
    "identityClaims",
    "identityIssuer",
    "identityMode",
    "owner",
    "userId",
    "username",
)


@dataclass(frozen=True, slots=True)
class LoginArtifacts:
    """User-login artifacts currently present in one home."""

    credential_present: bool
    casdoor_identity_present: bool

    @property
    def present(self) -> bool:
        return self.credential_present or self.casdoor_identity_present


def inspect_login_session(home: Path, *, expected_issuer: str) -> dict[str, Any] | None:
    """Return a coherent current-profile login, or ``None`` without writes."""

    from hyprial.daemon import read_settings_identity

    try:
        credential = read_login_credential(hyprial_home=home)
    except LoginError:
        return None
    identity = read_settings_identity(hyprial_home=home)
    if identity is None:
        return None
    owner, mode, issuer = identity
    if (
        mode != "casdoor"
        or issuer != credential.issuer
        or credential.issuer != expected_issuer
    ):
        return None
    return {
        "status": "already-logged-in",
        "owner": owner,
        "issuer": issuer,
        "home": str(home),
        "obtainedAt": credential.obtained_at,
        # The credential schema records no expiry. Do not invent one from
        # obtainedAt or attempt an online refresh for a read-only status.
        "expiresAt": None,
    }


def login_device_stage_complete(
    home: Path, *, owner: str, device_id: str
) -> bool:
    """Return whether login's device-key stage is complete for this identity."""

    from hyprial.identity import device_key_path, read_device_record

    if not device_key_path(home).is_file():
        return False
    try:
        record = read_device_record(home)
    except ValueError:
        return False
    return (
        record is not None
        and record.owner == owner
        and record.device_id == device_id
    )


def inspect_login_artifacts(home: Path) -> LoginArtifacts:
    """Report whether logout has any user-login-owned state to remove."""

    path = credential_path(hyprial_home=home)
    credential_present = path.exists() or path.is_symlink()
    settings = _read_settings(home / "settings.json", missing_ok=True)
    return LoginArtifacts(
        credential_present=credential_present,
        casdoor_identity_present=(
            settings is not None and settings.get("identityMode") == "casdoor"
        ),
    )


def remove_login_artifacts(home: Path) -> list[str]:
    """Remove only this home's user-login credential and Casdoor fields."""

    settings_path = home / "settings.json"
    settings = _read_settings(settings_path, missing_ok=True)
    removed: list[str] = []
    if settings is not None and settings.get("identityMode") == "casdoor":
        for field in _LOGIN_SETTINGS_FIELDS:
            if field in settings:
                settings.pop(field)
                removed.append(f"settings.{field}")
        atomic_json_write(settings_path, settings)

    login_path = credential_path(hyprial_home=home)
    if login_path.exists() or login_path.is_symlink():
        if login_path.is_dir() and not login_path.is_symlink():
            raise LoginError(
                "CREDENTIAL_UNREADABLE",
                f"login credential path is a directory: {login_path}",
            )
        login_path.unlink()
        removed.insert(0, "secrets/login.json")
    return removed


def _read_settings(path: Path, *, missing_ok: bool) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LoginError(
            "SETTINGS_UNREADABLE", f"cannot read login settings at {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise LoginError(
            "SETTINGS_UNREADABLE", f"{path}: top level is not an object"
        )
    return value
