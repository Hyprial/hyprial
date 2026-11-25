"""Ownerless B0-B6 cold-start runtime and same-process daemon promotion.

Decision: gui-m2-coldstart-mini-r2-20261105.  The runtime consumes the public
identity credential proxy, durable onboarding receipts and strict lifecycle
admission.  It creates no authenticated owner of its own: B1 serves onboarding
only, and the trusted owner appears only after an isolated worker commit is
reconciled with public identity.  B5 constructs the full daemon application
and transfers the same continuous OS locks into its existing ``run`` path, in
this process and on this thread.  Business methods are never mounted here.

This module is lifecycle assembly only.  A wire host remains responsible for
creating its private control session, verifying the OS peer and calling these
methods; no parameter dict is accepted as a transport or caller identity.

Decision: L1 (owner 2026-11-21).  ``logout`` on a home with no login artifact
completes with ``removed: []`` before any phase check (the CLI no longer removes
anything itself); ``expectedEpoch`` is compared with the epoch ``ping`` publishes.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.identity import (
    AuthProfile,
    CommitPermit,
    HttpTransport,
    IdentityBegin,
    IdentityCredentialPort,
    IdentityCredentialService,
    IdentityPublicState,
    IdentityRecordError,
    LogoutError,
    LogoutPartialRemoval,
    inspect_logout_artifacts,
    read_identity_snapshot,
    remove_logout_artifacts,
)
from hyprial.kernel import ipc_errors

from ..request_catalog.onboarding import (
    CallContext,
    CancelOutcome,
    OnboardingStateError,
    OperationRecord,
    Surface,
)
from .lifecycle.admission import BootstrapAdmission
from .lifecycle.identity import (
    VerifiedIdentity,
    build_bootstrap_status,
    classify_auth_failure,
    prepare_permit_binding,
    verify_promotion_identity,
    verify_worker_commit,
)
from .operations import OperationService
from .records import BootstrapRecord

BOOTSTRAP_PHASE_INVALID = "BOOTSTRAP_PHASE_INVALID"
BOOTSTRAP_SCOPE_MISMATCH = "BOOTSTRAP_SCOPE_MISMATCH"
BOOTSTRAP_IDENTITY_UNAVAILABLE = "BOOTSTRAP_IDENTITY_UNAVAILABLE"
BOOTSTRAP_OPERATION_CONFLICT = "BOOTSTRAP_OPERATION_CONFLICT"
BOOTSTRAP_PROMOTION_CONFLICT = "BOOTSTRAP_PROMOTION_CONFLICT"

_IDENTITY_PERMIT_SECONDS = 600


class BootstrapError(RuntimeError):
    """A refused bootstrap transition with a stable public code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BootstrapView:
    """The immutable public lifecycle view a control channel may expose."""

    record: BootstrapRecord
    claimed_operation_id: str | None = None


class BootstrapRuntime:
    """Own B0-B6 until an admitted bootstrap becomes the serving daemon."""

    def __init__(
        self,
        admission: BootstrapAdmission,
        record: BootstrapRecord,
        operations: OperationService,
        *,
        identity_factory: Callable[[], IdentityCredentialPort] | None = None,
    ) -> None:
        self._admission = admission
        self._record = record
        self._operations = operations
        self._identity_factory = identity_factory or IdentityCredentialService
        self._identity: IdentityCredentialPort | None = None
        self._surface: Surface | None = None
        self._profile: AuthProfile | None = None
        self._operation_id: str | None = None
        self._operation_key: str | None = None
        self._operation_intent: str | None = None
        self._last_begin: IdentityBegin | None = None
        self._last_state: IdentityPublicState | None = None
        self._verified: VerifiedIdentity | None = None
        self._claimed_operation_id: str | None = None
        self._quiesce: list[Callable[[], None]] = []
        self._quiesced = False
        self._post_reply: Callable[[], None] | None = None
        self._promoted = False
        self._promotion_failed = False
        self._logout_record: OperationRecord | None = None

    @classmethod
    def admit(
        cls,
        *,
        home: Path,
        state_dir: Path,
        socket_path: Path,
        identity_factory: Callable[[], IdentityCredentialPort] | None = None,
    ) -> BootstrapRuntime:
        """B0/B1: admit one ownerless home and publish its private record."""
        admission = BootstrapAdmission.acquire(
            Path(home), Path(state_dir), Path(socket_path)
        )
        record = BootstrapRecord.current(str(Path(socket_path)))
        try:
            record.publish(Path(state_dir))
        except BaseException:
            admission.close()
            raise
        try:
            operations = OperationService.open(
                Path(state_dir) / "onboarding"
            )
            return cls(
                admission,
                record,
                operations,
                identity_factory=identity_factory,
            )
        except BaseException:
            record.remove_if_owned(Path(state_dir))
            admission.close()
            raise

    @property
    def home(self) -> Path:
        return self._admission.home

    @property
    def state_dir(self) -> Path:
        return self._admission.state_dir

    @property
    def socket_path(self) -> Path:
        return self._admission.socket_path

    @property
    def phase(self) -> str:
        return self._record.phase

    @property
    def view(self) -> BootstrapView:
        return BootstrapView(self._record, self._claimed_operation_id)

    def local_operator_surface(self) -> Surface:
        """Return a selector for the explicit local bootstrap entry.

        This is not a control-channel registration or an authorization proof.
        The daemon's own bootstrap dispatcher is admitted only by the
        continuous OS locks, the same OS user, and an unoverridden
        environment; every transition still checks that this runtime holds
        those locks and owns the operation. A GUI host must supply a surface
        derived from its registered host/view control session instead.
        """
        self._admission.locks.require_active()
        if self._surface is not None:
            return self._surface
        if self._record.phase != "awaiting-auth":
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                "a local operator surface is only available before authentication",
            )
        return Surface(
            home_binding=f"local-home-{self._record.epoch}",
            host_control_session_id=(
                f"local-operator-{os.getpid()}-{secrets.token_hex(16)}"
            ),
            view_session_id="local-operator",
            transport_generation=f"bootstrap-{self._record.epoch}",
        )

    def interrupt(self) -> None:
        """Mark nonterminal bootstrap work unknown and release unpromoted locks.

        A promoted runtime owns nothing here: the full daemon owns both lock
        streams and the initialize receipt by then. This never replays an
        authentication or initialize operation.
        """
        if self._promoted:
            self._close_identity()
            return
        try:
            if (
                self._record.phase in {"awaiting-auth", "authenticating"}
                and self._surface is not None
                and self._operation_id is not None
            ):
                self._close_identity()
                self._mark_auth_unknown(
                    self._context(self._surface), self._operation_id
                )
            elif self._record.phase == "promoting" and not self._promotion_failed:
                self._mark_initialize("unknown")
        finally:
            self.close()

    def register_quiesce(self, callback: Callable[[], None]) -> None:
        """Register control/listener shutdown to run after durable acceptance."""
        if not callable(callback):
            raise ValueError("bootstrap quiesce callback must be callable")
        if self._record.phase == "promoting":
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                "bootstrap control is already quiescing",
            )
        self._quiesce.append(callback)

    def quiesce_promotion(self) -> None:
        """Stop bootstrap listeners after the initialize response was flushed.

        The caller must have written and flushed the successful initialize
        response before calling this or ``run_daemon``.  This method never
        releases the continuous OS locks and never replays the operation.  A
        duplicate call (including a callback-driven retry) is a no-op.
        """
        if self._record.phase != "promoting":
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                "bootstrap is not promoting",
            )
        if self._quiesced:
            return
        self._quiesced = True
        for callback in tuple(self._quiesce):
            callback()

    def take_post_reply(self) -> Callable[[], None] | None:
        """Return the one-shot post-reply action, or None if none is armed.

        The public IPC calls the returned action only after a complete
        successful frame write.  A partial write or disconnect must not call
        it; the initialize claim then remains ``promoting`` and recovery owns
        the truth.
        """
        callback = self._post_reply
        self._post_reply = None
        return callback

    def begin_authentication(
        self,
        *,
        surface: Surface,
        intent: str,
        operation_key: str,
        operation_id: str,
        profile: AuthProfile,
        transport: HttpTransport | None = None,
        permit_seconds: int = _IDENTITY_PERMIT_SECONDS,
    ) -> IdentityBegin:
        """B2: start the isolated identity worker under the continuous locks."""
        if self._record.phase == "authenticating":
            self._admission.locks.require_active()
            self._require_surface(surface)
            if (
                self._operation_key == operation_key
                and self._operation_intent == intent
                and self._last_begin is not None
                and self._last_begin.operation_id == self._operation_id
            ):
                # A lost response may be retried with the same operationKey;
                # the durable pending receipt and the in-flight worker are the
                # same operation, so return the cached begin without
                # dispatching identity again.
                return self._last_begin
            raise BootstrapError(
                BOOTSTRAP_OPERATION_CONFLICT,
                "another authentication operation is in flight",
            )
        self._require_phase("awaiting-auth")
        if self._identity is not None:
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                "an identity operation is already in flight",
            )
        if type(permit_seconds) is not int or permit_seconds <= 0:
            raise ValueError("bootstrap permit lifetime must be a positive integer")
        self._require_surface(surface)
        self._surface = surface
        context = self._context(surface)
        try:
            home_binding, legacy_business_data = prepare_permit_binding(self.home)
        except IdentityRecordError as error:
            raise BootstrapError(BOOTSTRAP_IDENTITY_UNAVAILABLE, str(error)) from error
        begin_record = self._operations.begin_auth(
            context, intent, operation_key, operation_id
        )
        if (
            begin_record.state != "pending"
            or begin_record.operation_id != operation_id
            or begin_record.kind != intent
        ):
            raise BootstrapError(
                BOOTSTRAP_OPERATION_CONFLICT,
                "an existing auth operation cannot be resumed safely",
            )
        # Remember the durable operation before dispatch so an exit after an
        # unconfirmed worker failure can still mark it unknown exactly once.
        self._operation_id = operation_id
        self._require_identity_snapshot_unchanged()
        permit = CommitPermit(
            operation_id=operation_id,
            nonce=secrets.token_hex(32),
            home=self.home,
            issuer=profile.issuer,
            client_id=profile.client_id,
            expected_identity=self._admission.identity.snapshot,
            expires_at=int(time.time()) + permit_seconds,
            bootstrap=True,
            home_binding=home_binding,
            legacy_business_data=legacy_business_data,
        )
        self._admission.locks.require_active()
        try:
            identity = self._identity_factory()
            self._identity = identity
            begin = identity.begin(
                profile=profile,
                home=self.home,
                permit=permit,
                lock_fd=None,
                transport=transport,
            )
            self._profile = profile
            self._operation_id = begin.operation_id
            self._set_phase("authenticating")
            self._operation_key = operation_key
            self._operation_intent = intent
            self._last_begin = begin
        except BaseException as error:
            if _begin_outcome_unknown(error):
                self._mark_auth_unknown(context, operation_id)
            self._close_identity()
            raise
        return begin

    def poll_authentication(self, *, surface: Surface) -> IdentityPublicState:
        """B3: reconcile a committed worker with public identity only."""
        self._require_surface(surface)
        if self._record.phase == "authenticated" and self._last_state is not None:
            return self._last_state
        self._require_phase("authenticating")
        if self._identity is None or self._profile is None or self._operation_id is None:
            raise BootstrapError(
                BOOTSTRAP_IDENTITY_UNAVAILABLE,
                "no identity operation is in flight for this bootstrap",
            )
        context = self._context(surface)
        state = self._identity.poll_public(self._operation_id)
        if state.state == "committed":
            verified = verify_worker_commit(
                self._admission,
                state,
                operation_id=self._operation_id,
                issuer=self._profile.issuer,
            )
            self._operations.mark_authenticated(
                context, self._operation_id, owner=state.owner
            )
            self._verified = verified
            self._last_state = state
            self._set_phase("authenticated")
            self._close_identity()
            return state
        if state.state in {"failed", "cancelled"}:
            outcome, op_error = classify_auth_failure(state)
            if outcome != "unknown":
                try:
                    self._require_identity_snapshot_unchanged()
                except BootstrapError:
                    outcome = "unknown"
                    op_error = None
                if state.owner is not None or any(
                    state.receipt.get(key)
                    for key in ("credentialCommitted", "settingsCommitted")
                ):
                    outcome = "unknown"
                    op_error = None
            self._operations.mark_auth_ended(
                context, self._operation_id, outcome, error=op_error
            )
            self._last_state = state
            self._close_identity()
            self._operation_key = None
            self._operation_intent = None
            self._last_begin = None
            self._set_phase("awaiting-auth")
            return state
        self._last_state = state
        return state

    def accept_initialize(
        self,
        *,
        surface: Surface,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
        defer_quiesce: bool = False,
    ) -> OperationRecord:
        """B4: durably accept initialize, then quiesce without releasing locks.

        ``defer_quiesce=True`` is the HTTP request path: the listener callbacks
        are armed behind ``take_post_reply()`` so the public transport can flush
        the successful initialize response first.  The default keeps the
        existing local path synchronous; both paths commit the same durable
        accepted/promoting receipt and never release the continuous locks here.
        """
        if self._record.phase == "promoting":
            self._admission.locks.require_active()
            self._require_surface(surface)
            verified = self._verified
            if verified is None or self._claimed_operation_id is None:
                raise BootstrapError(
                    BOOTSTRAP_PROMOTION_CONFLICT,
                    "bootstrap has no durable promotion claim",
                )
            record = self._operations.get(
                self._context(surface, owner=verified.snapshot.owner),
                self._claimed_operation_id,
            )
            if (
                record.kind != "initialize"
                or record.operation_key != operation_key
            ):
                raise BootstrapError(
                    BOOTSTRAP_OPERATION_CONFLICT,
                    "this promotion is bound to another initialize operation key",
                )
            return record
        self._require_phase("authenticated")
        self._require_surface(surface)
        verified = self._verified
        if verified is None or verified.snapshot.owner is None:
            raise BootstrapError(
                BOOTSTRAP_IDENTITY_UNAVAILABLE,
                "bootstrap has no verified owner to initialize",
            )
        context = self._context(surface, owner=verified.snapshot.owner)

        def promotion(record: OperationRecord) -> None:
            verify_promotion_identity(self._admission, verified)
            self._set_phase("promoting")
            self._claimed_operation_id = record.operation_id
            if defer_quiesce:
                # The request path arms the callbacks; the public transport
                # runs take_post_reply() only after the success frame is
                # completely written and flushed.
                self._post_reply = self.quiesce_promotion
            else:
                self.quiesce_promotion()

        accepted = self._operations.accept_initialize(
            context,
            operation_key,
            operation_id,
            receipt_id,
            promotion=promotion,
        )
        if (
            self._record.phase != "promoting"
            or self._claimed_operation_id != accepted.operation_id
        ):
            raise BootstrapError(
                BOOTSTRAP_PROMOTION_CONFLICT,
                "initialize receipt did not start this bootstrap promotion",
            )
        return accepted

    def begin_device(
        self,
        *,
        surface: Surface,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        """B3: record a device-preparation receipt for the verified owner."""
        self._require_phase("authenticated")
        self._require_surface(surface)
        verified = self._verified
        if verified is None or verified.snapshot.owner is None:
            raise BootstrapError(
                BOOTSTRAP_IDENTITY_UNAVAILABLE,
                "bootstrap has no verified owner for device preparation",
            )
        return self._operations.begin_device(
            self._context(surface, owner=verified.snapshot.owner),
            operation_key,
            operation_id,
            receipt_id,
        )

    def mark_device(
        self,
        *,
        surface: Surface,
        operation_id: str,
        state: str,
    ) -> OperationRecord:
        """B3: finish a device-preparation receipt for the verified owner."""
        self._require_phase("authenticated")
        self._require_surface(surface)
        verified = self._verified
        if verified is None or verified.snapshot.owner is None:
            raise BootstrapError(
                BOOTSTRAP_IDENTITY_UNAVAILABLE,
                "bootstrap has no verified owner for device preparation",
            )
        return self._operations.mark_device(
            self._context(surface, owner=verified.snapshot.owner),
            operation_id,
            state,
        )

    def cancel_authentication(
        self, context: CallContext, operation_id: str
    ) -> CancelOutcome:
        """Cancel an in-flight auth worker and reconcile the durable receipt.

        The credential worker is asked first.  The durable state is reconciled
        from the real result before the business result is returned:
        committed -> authenticated/identity-committed; cancelled -> cancelled;
        failed/pending/unavailable -> unknown/outcome-unknown.  A pending
        receipt is never blindly rewritten as cancelled.
        """
        self._require_surface(context.surface)
        if context.phase != "bootstrap" or context.caller != "local-bootstrap":
            raise BootstrapError(
                BOOTSTRAP_SCOPE_MISMATCH,
                "cancel is only available from the bootstrap control session",
            )
        effective = self._context(context.surface, owner=self.verified_owner())
        record = self._operations.get(effective, operation_id)
        if record.kind not in {"login", "register"}:
            return self._operations.cancel(effective, operation_id)
        if record.state != "pending":
            return self._operations.cancel(effective, operation_id)
        identity = self._identity
        if identity is None or self._operation_id != operation_id:
            # No live worker in this process: the outcome cannot be confirmed,
            # so it is unknown, not cancelled.
            self._mark_auth_unknown(effective, operation_id)
            return self._operations.cancel(effective, operation_id)
        try:
            state = identity.cancel(operation_id)
        except Exception:
            self._mark_auth_unknown(effective, operation_id)
            self._close_identity()
            self._operation_id = None
            self._operation_key = None
            self._operation_intent = None
            self._last_begin = None
            if self._record.phase == "authenticating":
                self._set_phase("awaiting-auth")
            return self._operations.cancel(effective, operation_id)
        if (
            state.state == "committed"
            and state.owner is not None
            and self._profile is not None
        ):
            try:
                verified = verify_worker_commit(
                    self._admission,
                    state,
                    operation_id=operation_id,
                    issuer=self._profile.issuer,
                )
            except BaseException:
                self._mark_auth_unknown(effective, operation_id)
            else:
                self._operations.mark_authenticated(
                    effective, operation_id, owner=state.owner
                )
                self._verified = verified
                self._last_state = state
                self._set_phase("authenticated")
        elif state.state == "cancelled":
            self._last_state = state
            if self._record.phase == "authenticating":
                self._set_phase("awaiting-auth")
        else:
            # failed, still pending, or committed without verifiable public
            # identity: never report a clean cancellation.
            self._mark_auth_unknown(effective, operation_id)
            self._last_state = state
            if self._record.phase == "authenticating":
                self._set_phase("awaiting-auth")
        self._close_identity()
        self._operation_id = None
        self._operation_key = None
        self._operation_intent = None
        self._last_begin = None
        return self._operations.cancel(
            self._context(context.surface, owner=self.verified_owner()),
            operation_id,
        )

    def run_daemon(
        self,
        application_factory: Callable[[], Any],
        *,
        owns_process_exit: bool = False,
        exit_unless_logout: bool = False,
    ) -> str:
        """B5/B6: construct the full daemon and transfer the held locks into run.

        Returns the application's explicit ``stop_reason`` (``logout`` or
        ``shutdown``); the caller, not this method, decides what follows.
        """
        if self._promoted or self._promotion_failed:
            raise BootstrapError(
                BOOTSTRAP_PROMOTION_CONFLICT,
                "bootstrap promotion already transferred or failed",
            )
        self._require_phase("promoting")
        verified = self._verified
        if verified is None or self._claimed_operation_id is None:
            raise BootstrapError(
                BOOTSTRAP_PROMOTION_CONFLICT,
                "bootstrap has no durable promotion claim",
            )
        verify_promotion_identity(self._admission, verified)
        # The local path already quiesced synchronously at accept_initialize;
        # this is the safety net for a deferred request path whose caller never
        # ran take_post_reply() (partial write/disconnect).  Idempotent.
        self.quiesce_promotion()
        try:
            application = application_factory()
        except BaseException:
            self._promotion_failed = True
            self._mark_initialize("failed")
            raise
        verify_promotion_identity(self._admission, verified)
        ownership_stream, identity_stream = self._admission.locks.detach()
        self._promoted = True
        self._record.remove_if_owned(self.state_dir)
        if owns_process_exit:
            application.owns_process_exit()
        elif exit_unless_logout:
            application.exit_unless_logout()
        try:
            application.run(
                ownership_stream=ownership_stream,
                identity_transaction_stream=identity_stream,
            )
        except BaseException:
            self._mark_initialize("unknown")
            raise
        # Returning from application.run() is not serving-ready.  The durable
        # claim stays promoting until the serving finalizer observes daemon.ready
        # and the transport-generation migration and calls complete_initialize().
        # Failure remains failed/unknown with no replay.
        return application.stop_reason

    def complete_initialize(self, *, surface: Surface | None = None) -> None:
        """Mark initialize completed at the serving daemon.ready boundary.

        The application mixin's promotion finalizer calls this after daemon.json
        is written and only after the control layer migrated the claim to the
        serving transport generation.  The call is idempotent.
        """
        if (
            not self._promoted
            or self._verified is None
            or self._surface is None
            or self._claimed_operation_id is None
        ):
            raise BootstrapError(
                BOOTSTRAP_PROMOTION_CONFLICT,
                "bootstrap has no completed promotion claim",
            )
        if surface is not None:
            self.adopt_promoted_surface(surface)
        self._mark_initialize("completed")

    def adopt_promoted_surface(self, surface: Surface) -> None:
        """Follow a verified journal migration before any serving setup can fail."""
        previous = self._surface
        if (
            previous is None
            or self._verified is None
            or self._claimed_operation_id is None
        ):
            raise BootstrapError(
                BOOTSTRAP_PROMOTION_CONFLICT,
                "bootstrap has no promotion claim",
            )
        if (
            surface.home_binding,
            surface.host_control_session_id,
            surface.view_session_id,
        ) != (
            previous.home_binding,
            previous.host_control_session_id,
            previous.view_session_id,
        ):
            raise BootstrapError(
                BOOTSTRAP_SCOPE_MISMATCH, "promotion changed its control scope"
            )
        self._operations.get(
            self._context(surface, owner=self._verified.snapshot.owner),
            self._claimed_operation_id,
        )
        self._surface = surface

    @property
    def operation_service(self) -> OperationService:
        """The one journal object the serving port must share (single writer)."""
        return self._operations

    @property
    def claimed_operation_id(self) -> str | None:
        return self._claimed_operation_id

    @property
    def operation_key(self) -> str | None:
        return self._operation_key

    @property
    def operation_intent(self) -> str | None:
        return self._operation_intent

    @property
    def last_begin(self) -> IdentityBegin | None:
        return self._last_begin

    def verified_owner(self) -> str | None:
        verified = self._verified
        return None if verified is None else verified.snapshot.owner

    def call_context(
        self, *, surface: Surface, owner: str | None = None
    ) -> CallContext:
        self._require_surface(surface)
        return self._context(surface, owner=owner)

    def get_operation(
        self, context: CallContext, operation_id: str
    ) -> OperationRecord:
        self._require_surface(context.surface)
        if (
            self._logout_record is not None
            and operation_id == self._logout_record.operation_id
        ):
            context = self._context(
                context.surface, owner=self._logout_record.account_owner
            )
        return self._operations.get(context, operation_id)

    @property
    def reopen_required(self) -> bool:
        return self._logout_record is not None and self._logout_record.state == "completed"

    def logout(
        self,
        context: CallContext,
        *,
        operation_key: str,
        confirm: bool,
        expected_epoch: str | None = None,
    ) -> OperationRecord:
        """Remove only login-owned identity under the continuous bootstrap locks.

        The journal accepts before removal.  Replays read that receipt until the
        host drains and rotates the transport; they never repeat removal.
        """
        self._admission.locks.require_active()
        self._require_surface(context.surface)
        if (
            confirm is not True
            or not isinstance(operation_key, str)
            or not operation_key
        ):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "confirmed logout and an operation key are required",
            )
        if expected_epoch is not None and expected_epoch != self._record.epoch:
            raise OnboardingStateError(
                ipc_errors.STALE_DAEMON_GENERATION,
                "the logout transport generation is stale",
            )
        if self._logout_record is not None:
            return self.get_operation(context, self._logout_record.operation_id)
        verified = None
        if inspect_logout_artifacts(self.home).present:
            self._require_phase("authenticated")
            verified = self._verified
            if verified is None:
                raise BootstrapError(
                    BOOTSTRAP_IDENTITY_UNAVAILABLE,
                    "no verified login is available",
                )
            try:
                verify_promotion_identity(self._admission, verified)
            except RuntimeError as error:
                raise BootstrapError(
                    BOOTSTRAP_IDENTITY_UNAVAILABLE, str(error)
                ) from error
        owner = None if verified is None else verified.snapshot.owner
        effective = self._context(context.surface, owner=owner)
        accepted = self._operations.begin_logout(
            effective,
            operation_key,
            secrets.token_hex(16),
            secrets.token_hex(16),
        )
        self._logout_record = accepted
        removed: tuple[str, ...] = ()
        state = "completed"
        try:
            if verified is not None:
                removed = remove_logout_artifacts(self.home)
        except LogoutPartialRemoval as error:
            removed, state = tuple(error.removed), "failed"
        except LogoutError:
            state = "failed"
        finally:
            if verified is None or read_identity_snapshot(self.home) != verified.snapshot:
                self._verified = None
        record = self._operations.mark_logout(
            effective, accepted.operation_id, state, removed
        )
        self._logout_record = record
        if state == "completed":
            self._verified = None
        return record

    def status(self, context: CallContext) -> dict:
        """Return the durable onboarding status for an already-verified caller."""
        return build_bootstrap_status(
            home=self.home,
            operations=self._operations,
            context=context,
            verified=self._verified,
        )

    def visible(self, context: CallContext) -> tuple[OperationRecord, ...]:
        """Return only the operations visible to this trusted control scope."""
        return self._operations.visible(context)

    def close(self) -> None:
        """Release an unpromoted bootstrap; a promoted runtime owns nothing."""
        if self._promoted:
            self._close_identity()
            return
        try:
            self._close_identity()
        finally:
            try:
                self._record.remove_if_owned(self.state_dir)
            finally:
                self._admission.close()

    def __enter__(self) -> BootstrapRuntime:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _context(self, surface: Surface, *, owner: str | None = None) -> CallContext:
        return CallContext(
            surface=surface,
            phase="bootstrap",
            caller="local-bootstrap",
            owner=owner,
        )

    def _require_surface(self, surface: Surface) -> None:
        if not isinstance(surface, Surface):
            raise BootstrapError(
                BOOTSTRAP_SCOPE_MISMATCH, "bootstrap caller surface is required"
            )
        if self._surface is not None and surface != self._surface:
            raise BootstrapError(
                BOOTSTRAP_SCOPE_MISMATCH,
                "bootstrap transition belongs to another control surface",
            )

    def _require_phase(self, expected: str) -> None:
        self._admission.locks.require_active()
        if self._logout_record is not None:
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                "the logout receipt must be reconciled before another transition",
            )
        if self._record.phase != expected:
            raise BootstrapError(
                BOOTSTRAP_PHASE_INVALID,
                f"bootstrap is in {self._record.phase}, expected {expected}",
            )

    def _require_identity_snapshot_unchanged(self) -> None:
        observed = read_identity_snapshot(self.home)
        if observed != self._admission.identity.snapshot:
            raise BootstrapError(
                BOOTSTRAP_IDENTITY_UNAVAILABLE,
                "public identity changed before the credential permit",
            )

    def _set_phase(self, phase: str) -> None:
        self._admission.locks.require_active()
        updated = BootstrapRecord(
            pid=self._record.pid,
            birth_identity=self._record.birth_identity,
            epoch=self._record.epoch,
            socket=self._record.socket,
            phase=phase,
        )
        updated.publish(self.state_dir)
        self._record = updated

    def _mark_auth_unknown(self, context: CallContext, operation_id: str) -> None:
        try:
            self._operations.mark_auth_unknown(context, operation_id)
        except BaseException:
            pass

    def _mark_initialize(self, state: str) -> None:
        verified = self._verified
        surface = self._surface
        operation_id = self._claimed_operation_id
        if verified is None or surface is None or operation_id is None:
            return
        try:
            self._operations.mark_initialize(
                self._context(surface, owner=verified.snapshot.owner),
                operation_id,
                state,
            )
        except BaseException:
            if state == "completed":
                raise

    def _close_identity(self) -> None:
        identity = self._identity
        self._identity = None
        if identity is not None:
            identity.close()


def _begin_outcome_unknown(error: BaseException) -> bool:
    """Never replay a worker start whose dispatch may have reached the child."""
    if getattr(error, "dispatched", False) is True:
        return True
    data = getattr(error, "data", None)
    return isinstance(data, dict) and data.get("outcome") == "unknown"
