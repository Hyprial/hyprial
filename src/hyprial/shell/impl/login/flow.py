"""Shared OIDC credential paths, refresh, and verification browser helpers.

T11 anchor: the daemon package must not import this module and must not read
``secrets/login.json`` directly; offline identity resolution reads ``settings.owner``
and ``HYPRIAL_OWNER`` only.  The identity login/relogin workflow itself is owned
by the daemon onboarding runtime.
"""

from __future__ import annotations

import os
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from hyprial.identity import (
    CREDENTIAL_VERSION,
    LOGIN_FILENAME,
    IdentityCredential,
    IdentityError,
    identity_credential_path,
    read_identity_credential,
    write_identity_credential,
)
from hyprial.daemon import NetworkProfile
from hyprial.shell.impl.login.oidc import (
    LOGIN_SCOPE,
    REFRESH_GRANT_TYPE,
    USER_AGENT,
    DeviceAuthorization,
    LoginError,
    OidcEndpoints,
    _fetch_endpoints,
    _request_json,
    _token_error,
)

__all__ = [
    "CREDENTIAL_VERSION",
    "LOGIN_FILENAME",
    "LOGIN_SCOPE",
    "DeviceAuthorization",
    "LoginCredential",
    "LoginError",
    "OidcEndpoints",
    "USER_AGENT",
    "credential_path",
    "read_login_credential",
    "refresh_access_token",
]


# -- records -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginCredential:
    """The whole content of ``secrets/login.json`` — refresh token only.

    ⚠️ The access token never reaches this record (T10: writing it is a
    mutation-red).  ``issuer`` scopes the credential to one profile so a
    later profile switch cannot silently reuse another issuer's token.
    """

    version: int
    issuer: str
    refresh_token: str
    obtained_at: str
    #: Monotonic identity-commit counter (``generation.py``): rides the
    #: credential's atomic write so a daemon can detect a newer login.
    identity_generation: int | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "version": self.version,
            "issuer": self.issuer,
            "refreshToken": self.refresh_token,
            "obtainedAt": self.obtained_at,
        }
        if self.identity_generation is not None:
            record["identityGeneration"] = self.identity_generation
        return record


# -- paths and credential I/O -------------------------------------------------


def credential_path(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> Path:
    """The shared ``$HYPRIAL_HOME/secrets/login.json`` path rule."""

    return identity_credential_path(environ, hyprial_home=hyprial_home)


def _login_error(error: IdentityError) -> LoginError:
    return LoginError(error.code, str(error), data=dict(error.data))


def read_login_credential(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> LoginCredential:
    """Load the shared credential record and preserve CLI error semantics."""

    try:
        credential = read_identity_credential(
            environ, hyprial_home=hyprial_home
        )
    except IdentityError as error:
        raise _login_error(error) from error
    return LoginCredential(
        version=credential.version,
        issuer=credential.issuer,
        refresh_token=credential.refresh_token,
        obtained_at=credential.obtained_at,
        identity_generation=credential.identity_generation,
    )


def _write_credential(credential: LoginCredential, path: Path) -> None:
    """Delegate the validate-verify-restore credential write to identity."""

    try:
        write_identity_credential(
            IdentityCredential(
                version=credential.version,
                issuer=credential.issuer,
                refresh_token=credential.refresh_token,
                obtained_at=credential.obtained_at,
                identity_generation=credential.identity_generation,
            ),
            path,
        )
    except IdentityError as error:
        raise _login_error(error) from error


def open_verification_uri(url: str) -> bool:
    """Open ``url`` in the user's browser; any failure degrades to False (the URI is printed anyway)."""

    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - any webbrowser failure degrades to print
        return False


# -- refresh (U3's enrollment interface) ---------------------------------------------


def refresh_access_token(
    profile: NetworkProfile,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    timeout: float | None = None,
) -> str:
    """Exchange the stored refresh token for a fresh access token.

    U3's enrollment interface (spec §1.3): read ``secrets/login.json`` →
    token endpoint with ``grant_type=refresh_token`` → if the server rotated
    the refresh token, atomically replace the credential file.  Any failure
    raises with the remedy "re-run hyprial login" — ⛔ no fallback identity
    guessing, and the file is never touched on failure (T12).
    """

    env = os.environ if environ is None else environ
    now = now or (lambda: datetime.now(UTC))
    credential = read_login_credential(env, hyprial_home=hyprial_home)
    if credential.issuer != profile.issuer:
        raise LoginError(
            "CREDENTIAL_ISSUER_MISMATCH",
            f"the stored credential belongs to issuer {credential.issuer!r} "
            f"but this profile's issuer is {profile.issuer!r}; re-run "
            "hyprial login for this profile",
            data={"credentialIssuer": credential.issuer, "issuer": profile.issuer},
        )
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    endpoints = _fetch_endpoints(profile, timeout=remaining())
    status, payload = _request_json(
        endpoints.token_endpoint,
        data={
            "grant_type": REFRESH_GRANT_TYPE,
            "client_id": profile.client_id,
            "refresh_token": credential.refresh_token,
        },
        timeout=remaining(),
    )
    error = _token_error(status, payload, where="refresh token")
    if error is not None:
        raise LoginError(
            "REFRESH_FAILED",
            f"refreshing the access token failed ({error}); re-run hyprial login",
            data=error.data,
        )
    if not isinstance(payload, dict):
        raise LoginError("REFRESH_FAILED", "refresh response invalid")
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise LoginError(
            "REFRESH_FAILED",
            "refresh response has no usable access_token; re-run hyprial login",
        )
    rotated = payload.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != credential.refresh_token:
        _write_rotated_credential(
            credential,
            LoginCredential(
                version=CREDENTIAL_VERSION,
                issuer=profile.issuer,
                refresh_token=rotated,
                obtained_at=now().isoformat(),
                identity_generation=credential.identity_generation,
            ),
            env,
            hyprial_home,
        )
    return access


def _write_rotated_credential(
    read: LoginCredential,
    rotated: LoginCredential,
    env: Mapping[str, str],
    hyprial_home: Path | None,
) -> None:
    """Compare-and-swap the rotated refresh token under the identity lock.

    The read happened before a network round trip; a login may have
    committed a new identity (and generation) meanwhile.  Writing blindly
    would restore the old account's refresh token *and* roll the generation
    back -- making the newer login's daemon look superseded and letting the
    next login reuse a generation number.  So the write lands only when the
    file still holds exactly what was read; otherwise the rotation is
    discarded and reported.
    """

    from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock

    path = credential_path(env, hyprial_home=hyprial_home)
    try:
        transaction = IdentityTransactionLock.acquire(path.parent.parent)
    except IdentityTransactionBusy as error:
        raise LoginError(
            "CREDENTIAL_WRITE_CONFLICT",
            "an identity transaction is in progress; the rotated refresh token "
            "was not saved -- retry, or re-run hyprial login",
        ) from error
    with transaction:
        try:
            current = read_login_credential(env, hyprial_home=hyprial_home)
        except LoginError:
            current = None
        if (
            current is None
            or current.refresh_token != read.refresh_token
            or current.identity_generation != read.identity_generation
        ):
            raise LoginError(
                "CREDENTIAL_WRITE_CONFLICT",
                "secrets/login.json changed while the access token was being "
                "refreshed (a login or logout committed); the rotated refresh "
                "token was discarded -- retry against the current identity",
                data={
                    "identityGeneration": read.identity_generation,
                    "currentIdentityGeneration": (
                        None if current is None else current.identity_generation
                    ),
                },
            )
        _write_credential(rotated, path)
