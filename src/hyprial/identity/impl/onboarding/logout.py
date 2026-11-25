"""Identity-owned authority for the login artifacts that logout removes.

Decision: the inspection/removal semantics that used to live in
``hyprial.shell.impl.login.session`` moved here (A2).  The shell keeps a thin
compatibility wrapper for the unchanged CLI, and the eventual logout lifecycle
coordinator gets a secret-free removal port from the identity domain instead of
importing CLI private code.

This module owns exactly two artifacts and nothing else:

* the local OIDC credential file ``secrets/login.json`` (removed as a file; its
  bytes are never read here, so a refresh token cannot leak through this path),
* the five login-written identity fields in ``settings.json``.

It never traverses agent homes, never removes Tailcat device material, agent
secrets, grants, or unrelated settings.  Removal is idempotent: a home that is
already logged out reports ``removed=()`` and writes nothing.

Failure behavior is explicit rather than silent:

* a missing ``settings.json`` is absence, not an error;
* malformed settings abort **before** any removal, so nothing is half-removed;
* a directory at the credential path is refused (never recursed into);
* a symlink at the credential path is unlinked as a link; its target is never
  followed or modified;
* a permission failure while removing the credential after settings were
  already rewritten raises :class:`LogoutPartialRemoval` carrying the labels
  that were actually removed, so a caller can report partial removal truthfully;
* a permission failure with nothing removed raises
  :class:`LogoutPermissionDenied`.

Removed labels are bounded and allowlisted (:data:`LOGIN_ARTIFACT_LABELS`);
no path outside the home, no token value, and no issuer response body can enter
the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write

from .ports import CREDENTIAL_ARTIFACT_LABEL, IdentityError, login_credential_path

SETTINGS_FILENAME = "settings.json"
CASDOOR_IDENTITY_MODE = "casdoor"

LOGIN_SETTINGS_FIELDS = (
    "identityIssuer",
    "identityMode",
    "owner",
    "userId",
    "username",
)
"""The account-scoped settings fields logout removes.

Login writes all of them.  ``owner`` is login-owned even though it is not part of the identity *residue* set that
admission rejects.
"""

#: Bounded, allowlisted labels for ``LOGOUT_RESULT.removed`` elements.
LOGIN_ARTIFACT_LABELS = (
    CREDENTIAL_ARTIFACT_LABEL,
    *(f"settings.{field}" for field in LOGIN_SETTINGS_FIELDS),
)

_CREDENTIAL_UNREADABLE = "CREDENTIAL_UNREADABLE"
_SETTINGS_UNREADABLE = "SETTINGS_UNREADABLE"
_PERMISSION_DENIED = "PERMISSION_DENIED"
_PARTIAL_REMOVAL = "PARTIAL_REMOVAL"


class LogoutError(IdentityError):
    """A structured logout artifact failure with a stable ``code``."""


class LogoutCredentialUnreadable(LogoutError):
    """The credential path is a directory, or otherwise not a removable file."""


class LogoutSettingsUnreadable(LogoutError):
    """``settings.json`` is not a readable JSON object."""


class LogoutPermissionDenied(LogoutError):
    """Nothing was removed because the filesystem refused the operation."""


class LogoutPartialRemoval(LogoutError):
    """Some login artifacts were removed before a later removal failed.

    ``removed`` lists the labels that *were* removed.  A caller must report
    partial removal truthfully and must not imply a rollback.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        removed: tuple[str, ...],
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(data or {})
        payload["removed"] = list(removed)
        super().__init__(code, message, payload)
        self.removed = removed


@dataclass(frozen=True, slots=True)
class LogoutArtifacts:
    """User-login artifacts currently present in one home.

    ``settings_fields`` lists the login-owned settings fields that actually
    exist, in canonical order, and only when this home carries a Casdoor login
    identity.  It is empty for a non-Casdoor home even if those keys remain:
    only login wrote them, and only a Casdoor identity makes them login-owned.
    """

    credential_present: bool
    casdoor_identity_present: bool
    settings_fields: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return self.credential_present or self.casdoor_identity_present

    @property
    def labels(self) -> tuple[str, ...]:
        """The labels removal would report, without writing anything.

        Derived from the fields that actually exist, so the preview equals what
        :func:`remove_logout_artifacts` returns for the same home.
        """

        labels: list[str] = []
        if self.credential_present:
            labels.append(CREDENTIAL_ARTIFACT_LABEL)
        if self.casdoor_identity_present:
            labels.extend(f"settings.{field}" for field in self.settings_fields)
        return tuple(labels)


def inspect_logout_artifacts(
    home: Path, *, settings: dict[str, Any] | None = None
) -> LogoutArtifacts:
    """Report whether logout has any user-login-owned state to remove.

    ``settings`` lets a caller that already read and validated the file avoid a
    second read; it must be the parsed object.  Nothing is written.
    """

    path = login_credential_path(home)
    credential_present = path.exists() or path.is_symlink()
    record = (
        settings if settings is not None else _read_settings(home / SETTINGS_FILENAME)
    )
    casdoor_identity_present = (
        record is not None and record.get("identityMode") == CASDOOR_IDENTITY_MODE
    )
    return LogoutArtifacts(
        credential_present=credential_present,
        casdoor_identity_present=casdoor_identity_present,
        settings_fields=(
            tuple(field for field in LOGIN_SETTINGS_FIELDS if field in record)
            if casdoor_identity_present and record is not None
            else ()
        ),
    )


def remove_logout_artifacts(home: Path) -> tuple[str, ...]:
    """Remove only this home's user-login credential and Casdoor fields.

    Returns the removed labels in canonical order (credential first).  Idempotent:
    a second call on the same home returns ``()``.
    """

    settings_path = home / SETTINGS_FILENAME
    settings = _read_settings(settings_path)
    removed: list[str] = []
    if settings is not None and settings.get("identityMode") == CASDOOR_IDENTITY_MODE:
        identity_fields = [
            field for field in LOGIN_SETTINGS_FIELDS if field in settings
        ]
        if identity_fields:
            for field in identity_fields:
                settings.pop(field)
            try:
                atomic_json_write(settings_path, settings)
            except OSError as error:
                raise LogoutPermissionDenied(
                    _PERMISSION_DENIED,
                    f"cannot rewrite login settings at {settings_path}: {error}",
                ) from error
            removed.extend(f"settings.{field}" for field in identity_fields)

    login_path = login_credential_path(home)
    if not (login_path.is_symlink() or login_path.exists()):
        return tuple(_canonical_labels(removed))
    if login_path.is_dir() and not login_path.is_symlink():
        _raise_after_partial(
            removed,
            LogoutCredentialUnreadable(
                _CREDENTIAL_UNREADABLE,
                f"login credential path is a directory: {login_path}",
            ),
        )
    try:
        # ``unlink`` never follows a symlink: only the link itself is removed.
        login_path.unlink()
    except OSError as error:
        _raise_after_partial(
            removed,
            LogoutPermissionDenied(
                _PERMISSION_DENIED,
                f"cannot remove login credential at {login_path}: {error}",
            ),
        )
    return tuple(_canonical_labels([CREDENTIAL_ARTIFACT_LABEL, *removed]))


def _canonical_labels(removed: list[str]) -> list[str]:
    """Return labels in the canonical credential-then-settings-field order."""

    present = set(removed)
    return [label for label in LOGIN_ARTIFACT_LABELS if label in present]


def _raise_after_partial(removed: list[str], error: LogoutError) -> None:
    """Raise ``error``, or a truthful partial-removal error if some labels went."""

    if not removed:
        raise error
    raise LogoutPartialRemoval(
        _PARTIAL_REMOVAL,
        str(error),
        removed=tuple(_canonical_labels(removed)),
        data={"cause": error.code},
    ) from error


def _read_settings(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LogoutSettingsUnreadable(
            _SETTINGS_UNREADABLE, f"cannot read login settings at {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise LogoutSettingsUnreadable(
            _SETTINGS_UNREADABLE, f"{path}: top level is not an object"
        )
    return value


__all__ = [
    "CASDOOR_IDENTITY_MODE",
    "CREDENTIAL_ARTIFACT_LABEL",
    "LOGIN_ARTIFACT_LABELS",
    "LOGIN_SETTINGS_FIELDS",
    "LogoutArtifacts",
    "LogoutCredentialUnreadable",
    "LogoutError",
    "LogoutPartialRemoval",
    "LogoutPermissionDenied",
    "LogoutSettingsUnreadable",
    "SETTINGS_FILENAME",
    "inspect_logout_artifacts",
    "remove_logout_artifacts",
]
