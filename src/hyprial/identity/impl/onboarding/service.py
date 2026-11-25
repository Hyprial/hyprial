# Decision record (W2A-LOGIN-INPROC, 2026-11-21):
# - Retired the isolated onboarding worker subprocess, socketpair frame protocol,
#   and pass_fds descriptor inheritance (which blocked Windows onboarding).
# - IdentityCredentialPort and its public types/error codes remain unchanged;
#   IdentityCredentialService now drives OIDC device authorization and commits
#   credentials in-process while verifying IdentityTransactionLock.is_held_in_process(home).
# - Credential and OIDC modules are lazy-imported inside methods to preserve the
#   T11 import boundary (importing hyprial.identity or daemon onboarding never
#   loads hyprial.identity.impl.onboarding.credentials).
"""In-process identity-credential service behind the public onboarding port."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Any, Literal, Mapping

from .ports import (
    AuthProfile,
    CommitPermit,
    HttpTransport,
    IdentityBegin,
    IdentityCredentialPort,
    IdentityError,
    IdentityPublicState,
    IdentityWorkerError,
)

_OperationState = Literal["pending", "committed", "cancelled", "failed"]


@dataclass
class _Operation:
    operation_id: str
    profile: AuthProfile
    home: Path
    permit: CommitPermit
    client: Any
    endpoints: Any
    authorization: Any | None
    interval: float
    deadline: float
    state: _OperationState = "pending"
    owner: str | None = None
    issuer: str | None = None
    user_id: str | None = None
    receipt: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None

    def clear_secrets(self) -> None:
        self.authorization = None


class IdentityCredentialService(IdentityCredentialPort):
    """Serves one identity-credential operation in-process under the home lock."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._op: _Operation | None = None
        self._begin_used = False
        self._closed = False
        self._state_lock = threading.RLock()
        self._cancellation_epoch = 0

    def begin(
        self,
        *,
        profile: AuthProfile,
        home: Path,
        permit: CommitPermit | None,
        lock_fd: int | None = None,
        transport: HttpTransport | None = None,
    ) -> IdentityBegin:
        del lock_fd
        with self._state_lock:
            if self._closed:
                raise IdentityWorkerError("IDENTITY_WORKER_CLOSED", "service is closed")
            if self._begin_used or self._op is not None:
                raise IdentityWorkerError(
                    "IDENTITY_WORKER_BUSY", "one worker serves one identity operation"
                )
            self._begin_used = True

        try:
            if permit is None:
                raise IdentityWorkerError(
                    "PERMIT_REQUIRED",
                    "identity commit requires a narrow commit permit; nothing was written",
                )
            target_home = Path(home)
            if target_home.resolve() != Path(permit.home).resolve():
                raise IdentityWorkerError(
                    "PERMIT_HOME_MISMATCH",
                    "commit permit belongs to another home; nothing was written",
                )
            if not target_home.is_absolute() or not target_home.is_dir():
                raise IdentityWorkerError(
                    "HOME_UNAVAILABLE", "permit home is not an existing absolute directory"
                )
            if not isinstance(permit.expires_at, int) or isinstance(
                permit.expires_at, bool
            ):
                raise IdentityWorkerError(
                    "PERMIT_INVALID", "permit expiresAt must be an integer"
                )
            if not isinstance(permit.bootstrap, bool):
                raise IdentityWorkerError(
                    "PERMIT_INVALID", "permit bootstrap must be boolean"
                )
            from hyprial.identity.impl.identity_transaction import IdentityTransactionLock

            if not IdentityTransactionLock.is_held_in_process(target_home):
                raise IdentityWorkerError(
                    "IDENTITY_LOCK_REQUIRED",
                    "identity transaction lock is not held in-process for home",
                )
            from .credentials.flow import validate_begin_permit
            from .credentials.oidc import OidcClient, initial_interval

            validate_begin_permit(
                permit=permit,
                profile=profile,
                home=target_home,
                lock_home=target_home,
                now_epoch=int(time.time()),
            )
            client = OidcClient(
                transport if transport is not None else HttpTransport()
            )
            endpoints = client.fetch_endpoints(profile)
            authorization = client.request_device_authorization(endpoints, profile)
        except IdentityError as error:
            self.close()
            if isinstance(error, IdentityWorkerError):
                raise
            raise IdentityWorkerError(error.code, str(error), error.data) from error
        except BaseException:
            self.close()
            raise

        with self._state_lock:
            if self._closed:
                authorization = None
                raise IdentityWorkerError("IDENTITY_WORKER_CLOSED", "service is closed")
            self._op = _Operation(
                operation_id=permit.operation_id,
                profile=profile,
                home=target_home,
                permit=permit,
                client=client,
                endpoints=endpoints,
                authorization=authorization,
                interval=initial_interval(authorization),
                deadline=time.monotonic() + authorization.expires_in,
            )
        return IdentityBegin(
            operation_id=permit.operation_id,
            verification_uri=(
                authorization.verification_uri_complete
                or authorization.verification_uri
            ),
            user_code=authorization.user_code,
            expires_in=authorization.expires_in,
            interval=authorization.interval,
        )

    def poll_public(self, operation_id: str) -> IdentityPublicState:
        operation = self._require_operation(operation_id)
        with self._state_lock:
            if operation.state != "pending":
                return self._public_state(operation)
            epoch = self._cancellation_epoch
        from hyprial.identity.impl.identity_transaction import IdentityTransactionLock

        if not IdentityTransactionLock.is_held_in_process(operation.home):
            raise IdentityWorkerError(
                "IDENTITY_LOCK_REQUIRED",
                "identity transaction lock is not held in-process for home",
            )
        if time.monotonic() >= operation.deadline:
            return self._fail(
                operation,
                "DEVICE_CODE_EXPIRED",
                "the device code expired before authorization completed; "
                "re-run the identity stage",
                epoch=epoch,
            )
        with self._state_lock:
            if not self._can_progress(operation, epoch):
                return self._public_state(operation)
            authorization = operation.authorization
        if authorization is None:
            return self._fail(
                operation,
                "IDENTITY_WORKER_FAILURE",
                "identity authorization state is no longer available",
                epoch=epoch,
            )
        try:
            result = operation.client.poll_device_token_once(
                operation.endpoints, operation.profile, authorization
            )
        except IdentityError as error:
            if error.code == "ISSUER_UNREACHABLE":
                with self._state_lock:
                    if not self._can_progress(operation, epoch):
                        return self._public_state(operation)
                if time.monotonic() >= operation.deadline:
                    return self._fail(
                        operation,
                        "DEVICE_CODE_EXPIRED",
                        f"the device code expired while the issuer was unreachable: {error}",
                        epoch=epoch,
                    )
                return self._public_state(operation)
            with self._state_lock:
                if not self._can_progress(operation, epoch):
                    return self._public_state(operation)
                return self._fail(operation, error.code, str(error), error.data, epoch=epoch)
        if result.outcome == "pending":
            with self._state_lock:
                if not self._can_progress(operation, epoch):
                    return self._public_state(operation)
                return self._public_state(operation)
        if result.outcome == "slow_down":
            from .credentials.oidc import interval_after_slow_down

            with self._state_lock:
                if not self._can_progress(operation, epoch):
                    return self._public_state(operation)
                operation.interval = interval_after_slow_down(operation.interval)
                return self._public_state(operation)
        return self._commit_tokens(operation, result, epoch)

    def cancel(self, operation_id: str) -> IdentityPublicState:
        with self._state_lock:
            operation = self._require_operation(operation_id)
            if operation.state == "pending":
                self._cancellation_epoch += 1
                operation.state = "cancelled"
                operation.receipt = {
                    "credentialCommitted": False,
                    "settingsCommitted": False,
                }
                operation.clear_secrets()
            return self._public_state(operation)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._cancellation_epoch += 1
            if self._op is not None:
                self._op.clear_secrets()

    def stderr_text(self) -> str:
        """Retained for diagnostic call-site compatibility; in-process runs emit none."""

        return ""

    def _commit_tokens(
        self, operation: _Operation, result: Any, epoch: int
    ) -> IdentityPublicState:
        access_token = result.access_token
        refresh_token = result.refresh_token
        if access_token is None or refresh_token is None:
            return self._fail(
                operation,
                "TOKEN_RESPONSE_INVALID",
                "token response did not carry both access and refresh tokens",
                epoch=epoch,
            )
        with self._state_lock:
            if not self._can_progress(operation, epoch):
                return self._public_state(operation)
        try:
            owner, subject = operation.client.fetch_userinfo(
                operation.endpoints, access_token
            )
        except IdentityError as error:
            with self._state_lock:
                if not self._can_progress(operation, epoch):
                    return self._public_state(operation)
                return self._fail(operation, error.code, str(error), error.data, epoch=epoch)
        self._before_commit_identity(operation, epoch)
        with self._state_lock:
            if not self._can_progress(operation, epoch):
                operation.clear_secrets()
                return self._public_state(operation)
            from hyprial.identity.impl.identity_transaction import IdentityTransactionLock

            if not IdentityTransactionLock.is_held_in_process(operation.home):
                operation.clear_secrets()
                raise IdentityWorkerError(
                    "IDENTITY_LOCK_REQUIRED",
                    "identity transaction lock is not held in-process for home",
                )
            from .credentials.flow import commit_identity
            from .identity_record import read_identity_snapshot

            try:
                if not self._can_progress(operation, epoch):
                    operation.clear_secrets()
                    return self._public_state(operation)
                receipt = commit_identity(
                    permit=operation.permit,
                    profile=operation.profile,
                    home=operation.home,
                    owner=owner,
                    subject=subject,
                    refresh_token=refresh_token,
                    environ=os.environ,
                    now_epoch=int(time.time()),
                )
            except IdentityError as error:
                return self._fail(operation, error.code, str(error), error.data, epoch=epoch)
            committed_owner = read_identity_snapshot(operation.home).owner or owner
            operation.state = "committed"
            operation.owner = committed_owner
            operation.issuer = operation.profile.issuer
            operation.user_id = subject
            operation.receipt = dict(receipt)
            operation.clear_secrets()
            return self._public_state(operation)

    def _require_operation(self, operation_id: str) -> _Operation:
        with self._state_lock:
            if self._closed:
                raise IdentityWorkerError("IDENTITY_WORKER_CLOSED", "service is closed")
            if self._op is None:
                raise IdentityWorkerError(
                    "NO_OPERATION", "identity service has no started operation"
                )
            if operation_id != self._op.operation_id:
                raise IdentityWorkerError(
                    "OPERATION_MISMATCH", "operation id does not match this service"
                )
            return self._op

    def _can_progress(self, operation: _Operation, epoch: int) -> bool:
        return (
            not self._closed
            and self._op is operation
            and operation.state == "pending"
            and self._cancellation_epoch == epoch
        )

    def _before_commit_identity(self, operation: _Operation, epoch: int) -> None:
        del operation, epoch

    def _fail(
        self,
        operation: _Operation,
        code: str,
        message: str,
        data: Mapping[str, Any] | None = None,
        *,
        epoch: int | None = None,
    ) -> IdentityPublicState:
        with self._state_lock:
            if epoch is not None and not self._can_progress(operation, epoch):
                return self._public_state(operation)
            operation.state = "failed"
            operation.error_code = code
            operation.error_message = message
            operation.receipt = dict(data or {})
            operation.clear_secrets()
            return self._public_state(operation)

    @staticmethod
    def _public_state(operation: _Operation) -> IdentityPublicState:
        return IdentityPublicState(
            operation_id=operation.operation_id,
            state=operation.state,
            owner=operation.owner,
            issuer=operation.issuer,
            user_id=operation.user_id,
            receipt=dict(operation.receipt or {}),
            error_code=operation.error_code,
            error_message=operation.error_message,
            retry_after_seconds=(
                float(operation.interval) if operation.state == "pending" else None
            ),
        )


__all__ = ["IdentityCredentialService"]
