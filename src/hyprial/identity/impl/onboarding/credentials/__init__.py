"""Credential-worker internals; loaded only in the isolated child process."""

from __future__ import annotations

from .credential_store import (
    CREDENTIAL_VERSION,
    LOGIN_FILENAME,
    SECRETS_DIRNAME,
    IdentityCredential,
    credential_path,
    credential_record_bytes,
    read_login_credential,
    write_login_credential,
)
from .flow import commit_identity, validate_begin_permit
from .oidc import (
    LOGIN_SCOPE,
    USER_AGENT,
    DeviceAuthorization,
    DevicePollResult,
    OidcClient,
    OidcEndpoints,
    initial_interval,
    interval_after_slow_down,
)

__all__ = [
    "CREDENTIAL_VERSION",
    "LOGIN_FILENAME",
    "LOGIN_SCOPE",
    "SECRETS_DIRNAME",
    "USER_AGENT",
    "DeviceAuthorization",
    "DevicePollResult",
    "IdentityCredential",
    "OidcClient",
    "OidcEndpoints",
    "commit_identity",
    "credential_path",
    "credential_record_bytes",
    "initial_interval",
    "interval_after_slow_down",
    "read_login_credential",
    "validate_begin_permit",
    "write_login_credential",
]
