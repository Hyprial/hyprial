"""Public identity-onboarding port for the isolated credential worker.

Decision: the daemon and shell never import credential code or hold a refresh
token.  They pass a narrow commit permit, an already-held transaction lock
descriptor, and public profile data.  The worker alone talks to the issuer and
lands the credential.  Only the four commands in ``WORKER_COMMANDS`` cross the
subprocess boundary, and only the fields in ``PUBLIC_STATE_FIELDS`` are
returned.  The same public face exports the strict, secret-free settings
admission used before a bootstrap credential operation; that reader never
opens ``secrets/login.json``.

Decision (2026-11-14): the credential *location* rule (directory, filename,
``login_credential_path``) is owned here rather than in the worker's
``credentials`` package.  Logout only needs to know where the artifact is in
order to unlink it; importing the location from ``credentials`` pulled the
whole worker package (OIDC client, credential reader, commit flow) into the
daemon bootstrap process through ``identity.remove_logout_artifacts``.  The
worker's store imports the rule from here, so there is still one definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

SECRETS_DIRNAME = "secrets"
LOGIN_FILENAME = "login.json"

#: Public label of the login credential artifact logout may remove.  It lives
#: in this credential-free port module so declaration-only consumers (the
#: daemon MethodSpec schemas) can mirror the receipt label without importing
#: the worker's credential code.
CREDENTIAL_ARTIFACT_LABEL = f"{SECRETS_DIRNAME}/{LOGIN_FILENAME}"


def login_credential_path(home: Path) -> Path:
    """Locate the login credential under ``home`` without touching it."""

    return Path(home) / SECRETS_DIRNAME / LOGIN_FILENAME

WORKER_COMMANDS = frozenset({"begin", "poll-public", "cancel", "close"})

PUBLIC_STATE_FIELDS = frozenset(
    {
        "operationId",
        "state",
        "owner",
        "issuer",
        "userId",
        "receipt",
        "errorCode",
        "errorMessage",
        "retryAfterSeconds",
    }
)


class IdentityError(Exception):
    """A structured identity failure with a stable code and public data."""

    def __init__(
        self, code: str, message: str, data: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})


class IdentityWorkerError(IdentityError):
    """A worker-protocol failure surfaced by the public proxy."""


class IdentityWorkerUnavailable(IdentityWorkerError):
    """The worker could not be reached or stopped answering.

    ``dispatched=False`` means the request was never written, so the operation
    definitely did not execute.  ``dispatched=True`` means a frame may have
    reached the worker before the transport failed: the outcome is unknown,
    credentials or settings may already be committed, and callers must
    reconcile public state instead of retrying blindly.
    """

    def __init__(
        self,
        code: str,
        message: str,
        data: Mapping[str, Any] | None = None,
        *,
        dispatched: bool = False,
    ) -> None:
        state = "unknown" if dispatched else "not_executed"
        merged = dict(data or {})
        merged["dispatched"] = dispatched
        merged["outcome"] = state
        super().__init__(code, message, merged)
        self.dispatched = dispatched
        self.outcome = state


class IdentityWorkerProtocolError(IdentityWorkerError):
    """The worker returned a frame outside the public allowlist."""


class IdentityRecordError(IdentityError):
    """Strict settings-record admission failed with a stable code."""


@dataclass(frozen=True, slots=True)
class AuthProfile:
    """The read-only issuer/client data trusted assembly passes to identity."""

    issuer: str
    client_id: str


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    """The strict settings identity observed immediately before the permit."""

    owner: str | None = None
    mode: str | None = None
    issuer: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class HomeBinding:
    """The account binding pinned in ``settings.homeAccount`` for one home."""

    issuer: str
    user_id: str
    owner: str

    def as_dict(self) -> dict[str, str]:
        """Return the persisted ``settings.homeAccount`` mapping."""

        return {
            "issuer": self.issuer,
            "userId": self.user_id,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class OnboardingSubject:
    """The narrow, server-side partition key for first-run progress.

    This is not a GUI identity projection: it is only read by the onboarding
    service to select one local receipt partition.  Missing any component means
    the caller has no stable subject and must not fall back to owner, username,
    session or an empty string.
    """

    issuer: str
    user_id: str
    device_uid: str

    def __post_init__(self) -> None:
        for name in ("issuer", "user_id", "device_uid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if len(value) > 2048:
                raise ValueError(f"{name} exceeds the local receipt bound")
            if any(ord(character) < 0x20 for character in value):
                raise ValueError(f"{name} must not contain control characters")
        if len(self.device_uid) != 32 or any(
            character not in "0123456789abcdef" for character in self.device_uid
        ):
            raise ValueError("deviceUid must be 32 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class IdentityAdmission:
    """A strict, secret-free bootstrap-admission view of one home."""

    present: bool
    snapshot: IdentitySnapshot


@dataclass(frozen=True, slots=True)
class CommitPermit:
    """The caller's one-use authorization to commit this identity.

    ``expires_at`` is epoch seconds.  A permit is bound to one operation, one
    home, one profile, the exact settings snapshot, and any home account
    binding or legacy-data observation captured before launch.
    The worker refuses a stale, expired, mismatched, or replayed permit.
    """

    operation_id: str
    nonce: str
    home: Path
    issuer: str
    client_id: str
    expected_identity: IdentitySnapshot
    expires_at: int
    bootstrap: bool = True
    home_binding: HomeBinding | None = None
    legacy_business_data: bool = False


@dataclass(frozen=True, slots=True)
class HttpTransport:
    """Trusted proxy/CA data; the worker builds its own stdlib opener."""

    proxy: str | None = None
    ca_bundle: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityBegin:
    """The public device-authorization start; no private code is included."""

    operation_id: str
    verification_uri: str
    user_code: str
    expires_in: int
    interval: int | None


@dataclass(frozen=True, slots=True)
class IdentityPublicState:
    """The complete public view of one worker operation."""

    operation_id: str
    state: Literal["pending", "committed", "cancelled", "failed"]
    owner: str | None = None
    issuer: str | None = None
    receipt: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retry_after_seconds: float | None = None
    user_id: str | None = None


class IdentityCredentialPort(Protocol):
    """The identity-side credential-commit port.

    The daemon may consume this secret-free proxy through the identity public
    face to start the isolated worker; it must never import the worker or
    credential modules, and it never reads credential files itself.  The
    strict settings snapshot/admission is a separate secret-free public
    reader, not part of this credential protocol.
    """

    def begin(
        self,
        *,
        profile: AuthProfile,
        home: Path,
        permit: CommitPermit | None,
        lock_fd: int | None,
        transport: HttpTransport | None = None,
    ) -> IdentityBegin: ...

    def poll_public(self, operation_id: str) -> IdentityPublicState: ...

    def cancel(self, operation_id: str) -> IdentityPublicState: ...

    def close(self) -> None: ...


__all__ = [
    "AuthProfile",
    "CommitPermit",
    "HomeBinding",
    "HttpTransport",
    "IdentityAdmission",
    "IdentityBegin",
    "IdentityCredentialPort",
    "IdentityError",
    "IdentityPublicState",
    "IdentityRecordError",
    "IdentitySnapshot",
    "IdentityWorkerError",
    "IdentityWorkerProtocolError",
    "IdentityWorkerUnavailable",
    "OnboardingSubject",
    "PUBLIC_STATE_FIELDS",
    "WORKER_COMMANDS",
]
