# Decision record (W2A-LOGIN-INPROC, 2026-11-21):
# - ServingRelogin retains the acquired identity transaction lock across the
#   in-process device-authorization operation from begin() through a committed
#   poll(), verify_commit(), and the caller's durable receipt update until
#   close(), passing lock_fd=None instead of a transferable POSIX fileno.
# - Non-commit terminal polls (cancelled/failed), cancel(), close(), and error
#   paths in begin()/poll()/verify_commit() release the lock immediately.
"""Same-owner reauthentication under the home identity transaction lock.

Decision: identity-http-a3-serving-account-20261106 (split out for the 1000-line
module budget; behaviour is unchanged).

The permit is narrow (``bootstrap=False``) and bound to the exact snapshot the
caller observed.  The identity service refuses a different owner, mode or issuer
before writing; this module refuses the mode/issuer mismatch before a journal
receipt exists, so a conflict never mutates durable state.  Nothing here reads a
credential file or invents an owner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import time
from typing import Any

from hyprial.daemon.impl.request_catalog.onboarding.state import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    CancelOutcome,
    OperationRecord,
)
from hyprial.identity import (
    AuthProfile,
    CommitPermit,
    DIFFERENT_ACCOUNT_REASON,
    HttpTransport,
    IdentityBegin,
    IdentityCredentialPort,
    IdentityCredentialService,
    IdentityError,
    IdentityPublicState,
    IdentityRecordError,
    IdentitySnapshot,
    IdentityTransactionBusy,
    IdentityTransactionLock,
    home_account_conflict_message,
    legacy_business_data_present,
    read_home_binding,
    read_identity_snapshot,
)
from hyprial.kernel import ipc_errors

from ..errors import OnboardingGatewayError

SERVING_PERMIT_SECONDS = 600
_DEFAULT_INTERVAL_SECONDS = 5
_MAX_INTERVAL_SECONDS = 3600
_MAX_VERIFICATION_LIFETIME_SECONDS = 24 * 3600


def _verification(begin: IdentityBegin) -> dict[str, Any]:
    interval = begin.interval
    if type(interval) is not int or interval <= 0:
        interval = _DEFAULT_INTERVAL_SECONDS
    interval = min(interval, _MAX_INTERVAL_SECONDS)
    expires_in = begin.expires_in
    if type(expires_in) is not int or expires_in <= 0:
        expires_in = 60
    expires_in = min(expires_in, _MAX_VERIFICATION_LIFETIME_SECONDS)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return {
        "verificationUri": begin.verification_uri,
        "userCode": begin.user_code,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "intervalSeconds": interval,
    }


def _begin_result(begin: IdentityBegin, intent: str) -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": begin.operation_id,
        "intent": intent,
        "state": "pending",
        "verification": _verification(begin),
        "owner": None,
    }


def _begin_record_result(
    record: OperationRecord,
    intent: str,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": record.operation_id,
        "intent": intent,
        "state": record.state,
        "verification": verification,
        "owner": record.account_owner,
    }


def _auth_operation(
    record: OperationRecord, verification: dict[str, Any] | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operationId": record.operation_id,
        "kind": record.kind,
        "state": record.state,
        "owner": record.account_owner,
        "receiptId": record.receipt_id,
        "verification": verification,
    }
    if record.error is not None:
        payload["error"] = record.error.as_dict()
    return payload


def _cancel_result(outcome: CancelOutcome) -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": outcome.operation_id,
        "state": outcome.state,
        "cancelled": outcome.cancelled,
        "reason": outcome.reason,
    }

_ACCOUNT_IDENTITY_MODES = frozenset({"casdoor"})
_CONFLICT_IDENTITY_CODES = frozenset(
    {
        "OWNER_MISMATCH",
        "IDENTITY_MODE_CONFLICT",
        "IDENTITY_ISSUER_CONFLICT",
        "PERMIT_PROFILE_MISMATCH",
        "HOME_ACCOUNT_CONFLICT",
    }
)


def _translated_identity_error(error: IdentityError) -> OnboardingGatewayError:
    if error.code == "HOME_ACCOUNT_CONFLICT":
        reason = error.data.get("reason")
        message = home_account_conflict_message(
            reason if isinstance(reason, str) else DIFFERENT_ACCOUNT_REASON
        )
        return OnboardingGatewayError(
            ipc_errors.IDENTITY_CONFLICT,
            message,
            dict(error.data) if error.data else None,
        )
    if error.code in _CONFLICT_IDENTITY_CODES:
        return OnboardingGatewayError(
            ipc_errors.IDENTITY_CONFLICT,
            str(error),
            dict(error.data) if error.data else None,
        )
    return OnboardingGatewayError(ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, str(error))


def _translated_worker_error(error: BaseException) -> OnboardingGatewayError:
    if isinstance(error, IdentityError):
        return _translated_identity_error(error)
    return OnboardingGatewayError(
        ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
        "the reauthentication worker is unavailable",
    )


def _close_quietly(resource: Any | None) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        pass


def _require_same_identity(expected: IdentitySnapshot, profile: AuthProfile) -> None:
    if expected.mode is not None and expected.mode not in _ACCOUNT_IDENTITY_MODES:
        raise OnboardingGatewayError(
            ipc_errors.IDENTITY_CONFLICT,
            f"the local identity mode is {expected.mode!r}; reauthentication "
            "cannot replace it",
        )
    if expected.issuer is not None and expected.issuer != profile.issuer:
        raise OnboardingGatewayError(
            ipc_errors.IDENTITY_CONFLICT,
            f"the local identity issuer is {expected.issuer!r}, not {profile.issuer!r}",
        )


class ServingRelogin:
    """One live same-owner reauthentication inside the serving generation.

    The permit is narrow (``bootstrap=False``) and bound to the exact snapshot
    the caller observed.  The service refuses a different owner, mode or issuer
    before writing; this class refuses the mode/issuer mismatch before a journal
    receipt exists, so a conflict never mutates durable state.
    """

    def __init__(
        self,
        *,
        home: Path,
        profile_resolver: Callable[[Path], AuthProfile] | None = None,
        identity_factory: Callable[[], IdentityCredentialPort] | None = None,
        identity_reader: Callable[[Path], IdentitySnapshot] = read_identity_snapshot,
        lock_factory: Callable[[Path], Any] | None = None,
        permit_seconds: int = SERVING_PERMIT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(permit_seconds) is not int or permit_seconds <= 0:
            raise ValueError("the serving permit lifetime must be a positive integer")
        self._home = Path(home)
        self._profile_resolver = profile_resolver
        self._identity_factory = identity_factory
        self._identity_reader = identity_reader
        self._lock_factory = lock_factory or IdentityTransactionLock.acquire
        self._permit_seconds = permit_seconds
        self._clock = clock
        self._identity: IdentityCredentialPort | None = None
        self._lock: Any | None = None
        self._operation_id: str | None = None
        self._profile: AuthProfile | None = None

    @property
    def live_operation_id(self) -> str | None:
        return self._operation_id

    @property
    def live(self) -> bool:
        return self._identity is not None

    def _release_lock(self) -> None:
        lock = self._lock
        self._lock = None
        _close_quietly(lock)

    def _resolve_profile(self) -> AuthProfile:
        resolver = self._profile_resolver
        if resolver is None:
            from ..service import default_profile_resolver

            resolver = default_profile_resolver
        try:
            profile = resolver(self._home)
        except OnboardingGatewayError:
            raise
        except Exception as error:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "the official auth profile is unavailable",
            ) from error
        if not isinstance(profile, AuthProfile):
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "the official auth profile has an unexpected shape",
            )
        return profile

    def precheck(self, expected: IdentitySnapshot) -> AuthProfile:
        """Refuse a reauthentication that must not reach a durable write."""

        if not isinstance(expected, IdentitySnapshot):
            raise OnboardingGatewayError(
                ipc_errors.INVALID_ARGUMENT,
                "a public identity snapshot is required for reauthentication",
            )
        if expected.owner is None:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_UNBOUND,
                "same-owner reauthentication requires a committed local identity",
            )
        profile = self._resolve_profile()
        _require_same_identity(expected, profile)
        try:
            observed = self._identity_reader(self._home)
        except Exception as error:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "the local identity snapshot is unreadable",
            ) from error
        if observed != expected:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_CONFLICT,
                "the local identity changed before the reauthentication permit",
            )
        return profile

    def begin(
        self,
        *,
        operation_id: str,
        expected: IdentitySnapshot,
        transport: HttpTransport | None = None,
    ) -> IdentityBegin:
        if self._identity is not None:
            raise OnboardingGatewayError(
                AUTH_OPERATION_CONFLICT,
                "another reauthentication is already in flight",
            )
        if not isinstance(operation_id, str) or not operation_id:
            raise OnboardingGatewayError(
                ipc_errors.INVALID_ARGUMENT,
                "a reauthentication operation id is required",
            )
        profile = self.precheck(expected)
        observed = self._identity_reader(self._home)
        try:
            home_binding = read_home_binding(self._home)
        except IdentityRecordError as error:
            raise _translated_identity_error(error) from error
        legacy_business_data = legacy_business_data_present(self._home)
        permit = CommitPermit(
            operation_id=operation_id,
            nonce=secrets.token_hex(32),
            home=self._home,
            issuer=profile.issuer,
            client_id=profile.client_id,
            expected_identity=observed,
            expires_at=int(self._clock()) + self._permit_seconds,
            bootstrap=False,
            home_binding=home_binding,
            legacy_business_data=legacy_business_data,
        )
        try:
            lock = self._lock_factory(self._home)
        except IdentityTransactionBusy as error:
            raise OnboardingGatewayError(
                AUTH_OPERATION_CONFLICT,
                "another identity transaction owns this home",
            ) from error
        self._lock = lock
        identity: IdentityCredentialPort | None = None
        try:
            factory = self._identity_factory or IdentityCredentialService
            identity = factory()
            begin = identity.begin(
                profile=profile,
                home=self._home,
                permit=permit,
                lock_fd=None,
                transport=transport,
            )
        except IdentityError as error:
            _close_quietly(identity)
            self._release_lock()
            raise _translated_identity_error(error) from error
        except BaseException:
            _close_quietly(identity)
            self._release_lock()
            raise
        self._identity = identity
        self._operation_id = begin.operation_id
        self._profile = profile
        return begin

    def _require_live(self, operation_id: str) -> IdentityCredentialPort:
        identity = self._identity
        if identity is None or self._operation_id != operation_id:
            raise OnboardingGatewayError(
                AUTH_OPERATION_NOT_FOUND,
                "no reauthentication worker is live for this operation",
            )
        return identity

    def poll(self, operation_id: str) -> IdentityPublicState:
        identity = self._require_live(operation_id)
        try:
            state = identity.poll_public(operation_id)
        except Exception as error:
            self._release_lock()
            raise _translated_worker_error(error) from error
        except BaseException:
            self._release_lock()
            raise
        if state.state not in {"pending", "committed"}:
            self._release_lock()
        return state

    def cancel(self, operation_id: str) -> IdentityPublicState:
        identity = self._require_live(operation_id)
        try:
            return identity.cancel(operation_id)
        except Exception as error:
            raise _translated_worker_error(error) from error
        finally:
            self._release_lock()

    def verify_commit(
        self,
        state: IdentityPublicState,
        *,
        expected: IdentitySnapshot,
    ) -> None:
        """Accept only the observed owner and the trusted issuer.

        This coordinator never wrote the identity files; the credential service
        did.  A commit for another owner is a conflict and the receipt stays
        unconfirmed.
        """

        try:
            if state.state != "committed" or state.owner is None:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "the reauthentication did not commit an owner",
                )
            if expected.owner is None or state.owner != expected.owner:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "the reauthentication committed another owner",
                    {"committedOwner": state.owner, "expectedOwner": expected.owner},
                )
            if expected.user_id is not None and state.user_id != expected.user_id:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "the reauthentication committed another user",
                    {
                        "committedUserId": state.user_id,
                        "expectedUserId": expected.user_id,
                    },
                )
            profile = self._profile
            if (
                state.issuer is not None
                and profile is not None
                and state.issuer != profile.issuer
            ):
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "the reauthentication committed another issuer",
                    {
                        "committedIssuer": state.issuer,
                        "expectedIssuer": profile.issuer,
                    },
                )
        except BaseException:
            self._release_lock()
            raise

    def close(self) -> None:
        identity = self._identity
        self._identity = None
        self._operation_id = None
        self._profile = None
        try:
            _close_quietly(identity)
        finally:
            self._release_lock()

    def close_if_current(self, operation_id: str) -> bool:
        if self._operation_id != operation_id:
            return False
        self.close()
        return True


__all__ = [
    "SERVING_PERMIT_SECONDS",
    "ServingRelogin",
]
