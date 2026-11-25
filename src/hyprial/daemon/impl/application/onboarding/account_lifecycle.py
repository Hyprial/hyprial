"""Serving-generation account lifecycle: the serving request port, startup
classification, the durable account projection and same-owner reauthentication.

Decision: identity-http-a3-serving-account-20261106.
Decision: identity-http-a5-logout-custody-20261106 (superseded in this file by
the daemon-owned logout below; the supervisor-side custody seam
``operations.custody`` and its shell coordinator were deleted in Z-7 A2).
Decision: daemon-owned logout, 2026-11-09 (intg/gui-setting-fix-109): after the
serving journal moved to the daemon process there is no supervisor registration
point and no supervisor-owned handle on the journal writer, so A5 custody can
neither be registered nor honoured.  The daemon is the single onboarding
authority: an accepted logout is completed by this port — identity artifacts
removed through the identity-owned primitive, the receipt carried to a truthful
terminal state before the daemon's own stop is requested — and the declared
optional ``expectedEpoch`` fence is honoured before any identity work.

Every answer here derives from durable public state, never from the transport or
a request body.  A committed local identity means normal startup, so a crash
between the identity commit and initialize never re-enters bootstrap and the
projection reports the missing step instead of claiming ready.  The local
identity snapshot, the public device record and the durable initialize receipt
stay three separate facts, so an invalid online credential is never reported as
a missing local identity.  Reauthentication is one narrow non-bootstrap permit
bound to the observed snapshot and run under the home identity transaction lock;
a different owner, mode or issuer is refused, and a failed attempt leaves the
local identity in place.  Nothing here invents an owner.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from hyprial.identity import (
    AuthProfile,
    HttpTransport,
    IdentityCredentialPort,
    IdentityError,
    IdentityRecordError,
    IdentitySnapshot,
    LogoutArtifacts,
    LogoutError,
    LogoutPartialRemoval,
    OnboardingSubject,
    read_device_record,
    read_identity_snapshot,
    read_onboarding_identity,
    read_onboarding_subject,
    read_settings_username,
)
from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.onboarding.first_run import first_run_status
from hyprial.daemon.impl.onboarding.lifecycle.identity import (
    project_home_account_conflict,
)
from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.onboarding.operations.journal import OperationReceiptError
from hyprial.daemon.impl.onboarding.operations.service import (
    OperationService,
    RecoveryAuthority,
)
from hyprial.daemon.impl.request_catalog.onboarding.first_run import (
    FirstRunSnapshot,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    REGISTRATION_UNAVAILABLE,
)
from hyprial.daemon.impl.user_key.login_key import ensure_login_user_key
from hyprial.daemon.impl.request_catalog.onboarding.state import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    LOGOUT_KIND,
    AccountStatus,
    CallContext,
    OnboardingStateError,
    OperationError,
    OperationRecord,
    status_snapshot,
)

from .errors import OnboardingGatewayError
from .lifecycle import (
    SERVING_PERMIT_SECONDS,
    LogoutArtifactPort,
    ServingRecovery,
    ServingRelogin,
    _IdentityLogoutArtifacts,
    _auth_operation,
    _begin_record_result,
    _begin_result,
    _cancel_result,
    _logout_operation,
    _logout_result,
)
from .lifecycle.relogin import (
    _DEFAULT_INTERVAL_SECONDS as _DEFAULT_INTERVAL_SECONDS,
    _verification as _verification,
)
from .relogin import ServingReloginDriver, replay_begin_result, wait_relogin_operation

_AUTH_KINDS = frozenset({"login", "register"})

StartupMode = Literal["bootstrap", "serving"]


@dataclass(frozen=True, slots=True)
class StartupDecision:
    """The startup path one home requires; ``owner`` is never caller-supplied."""

    mode: StartupMode
    owner: str | None

    @property
    def bootstrap_required(self) -> bool:
        return self.mode == "bootstrap"


def classify_startup(identity: IdentitySnapshot) -> StartupDecision:
    """Decide the startup path from the public identity snapshot alone."""

    if not isinstance(identity, IdentitySnapshot):
        raise OnboardingGatewayError(
            ipc_errors.INVALID_ARGUMENT,
            "a public identity snapshot is required to classify startup",
        )
    if identity.owner is not None:
        return StartupDecision("serving", identity.owner)
    return StartupDecision("bootstrap", None)


def startup_decision(
    *,
    home: Path,
    identity_reader: Callable[[Path], IdentitySnapshot] = read_identity_snapshot,
) -> StartupDecision:
    """Classify one explicit home without admitting, locking or writing."""

    try:
        identity = identity_reader(Path(home))
    except Exception as error:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR,
            "the public identity snapshot is unavailable",
        ) from error
    return classify_startup(identity)


def observe_account(
    *,
    home: Path,
    operations_root: Path,
    identity_reader: Callable[[Path], IdentitySnapshot] = read_identity_snapshot,
    device_reader: Callable[[Path], Any] = read_device_record,
    journal_reader: Callable[[Path], Any] = journal.read_journal,
    username_reader: Callable[[Path], str | None] = read_settings_username,
    credential: str = "unverified",
) -> AccountStatus:
    """Project the durable account state of one serving home.

    The initialize receipt is read across transport generations on purpose: a
    new generation must not report a finished account as uninitialized.  The
    wire ``operations`` list stays generation bounded; only this projection
    looks past generations.
    """

    home = Path(home)
    try:
        identity = identity_reader(home)
    except Exception as error:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR,
            "the public identity snapshot is unavailable",
        ) from error
    if not isinstance(identity, IdentitySnapshot):
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR,
            "the public identity snapshot has an unexpected shape",
        )
    try:
        device = device_reader(home)
    except Exception as error:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR,
            "the public device record is unreadable",
        ) from error
    try:
        state = journal_reader(Path(operations_root))
    except OperationReceiptError as error:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR,
            "the onboarding operation journal is unreadable",
        ) from error
    completed = False
    if state is not None:
        completed = any(
            record.kind == "initialize"
            and record.state == "completed"
            and (identity.owner is None or record.account_owner == identity.owner)
            for record in state.snapshot.records
        )
    device_ready = (
        device is not None
        and identity.owner is not None
        and getattr(device, "owner", None) == identity.owner
    )
    username: str | None = None
    if identity.owner is not None:
        raw_username = username_reader(home)
        if isinstance(raw_username, str) and raw_username.strip():
            username = raw_username.strip()
    try:
        return AccountStatus(
            owner=identity.owner,
            mode=identity.mode,
            issuer=identity.issuer,
            credential=credential,
            device_ready=device_ready,
            device_id=device.device_id if device_ready else None,
            initialize_completed=completed,
            username=username,
        )
    except OnboardingStateError as error:
        raise OnboardingGatewayError(error.code, error.message) from error


@contextmanager
def _translated(*, unavailable_code: str) -> Iterator[None]:
    try:
        yield
    except OnboardingGatewayError:
        raise
    except OnboardingStateError as error:
        raise OnboardingGatewayError(error.code, error.message) from error
    except OperationReceiptError as error:
        raise OnboardingGatewayError(ipc_errors.DAEMON_ERROR, str(error)) from error
    except IdentityError as error:
        raise OnboardingGatewayError(unavailable_code, str(error)) from error


def _close_quietly(identity: IdentityCredentialPort | None) -> None:
    if identity is None:
        return
    try:
        identity.close()
    except Exception:
        return


#: Serving account methods answer the admitted GUI view and the daemon's own
#: local operator (the CLI forwarding over the request socket).
_SERVING_CALLERS = frozenset({"local-gui", "local-operator"})


def _require_context(
    context: CallContext, phase: str, caller: str | frozenset[str]
) -> CallContext:
    if not isinstance(context, CallContext):
        raise OnboardingGatewayError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "an admitted onboarding context is required",
        )
    allowed = caller if isinstance(caller, frozenset) else frozenset({caller})
    if context.phase != phase or context.caller not in allowed:
        raise OnboardingGatewayError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "the admitted control session is not allowed for this method",
        )
    return context


@dataclass(frozen=True, slots=True)
class _TrustedIdentity:
    snapshot: IdentitySnapshot
    device: Any
    subject: OnboardingSubject | None


class ServingOnboardingGateway:
    """Serving phase port over the one shared ``OperationService``.

    Serves two paths: a promoted bootstrap, which passes the claim its
    initialize receipt created, and a normal startup of an already-authenticated
    home, which passes no claim.  Without a claim there is nothing to migrate and
    nothing to complete, so no finalizer exists and a normal startup cannot
    fabricate a promotion receipt.  Reopening the journal here would create a
    second writer and is forbidden.

    Logout (daemon-owned) uses the same journal: acceptance is durable *before*
    any removal, the identity-owned artifacts are removed through
    ``logout_artifacts``, the receipt is carried to a truthful terminal state
    and only then is the daemon's own stop requested through ``request_stop``.
    An empty home completes without stopping or removing anything, and a failed
    removal reports the labels that really went.
    """

    def __init__(
        self,
        *,
        operations: OperationService,
        home: Path,
        complete_initialize: Callable[[], None] | None = None,
        promotion_operation_id: str | None = None,
        identity_reader: Callable[[Path], Any] = read_identity_snapshot,
        device_reader: Callable[[Path], Any] = read_device_record,
        journal_reader: Callable[[Path], Any] = journal.read_journal,
        profile_resolver: Callable[[Path], AuthProfile] | None = None,
        identity_factory: Callable[[], IdentityCredentialPort] | None = None,
        transaction_factory: Callable[[Path], Any] | None = None,
        transport: HttpTransport | None = None,
        permit_seconds: int = SERVING_PERMIT_SECONDS,
        clock: Callable[[], float] = time.time,
        logout_artifacts: LogoutArtifactPort | None = None,
        request_stop: Callable[[], None] | None = None,
        provisioner: Any | None = None,
        user_key: Callable[[Path, Path, str, Any], Any] = ensure_login_user_key,
    ) -> None:
        if promotion_operation_id is not None and (
            not isinstance(promotion_operation_id, str) or not promotion_operation_id
        ):
            raise ValueError("a promotion operation id must be non-empty")
        if (promotion_operation_id is None) != (complete_initialize is None):
            raise ValueError(
                "a promotion claim and its completion callback are given "
                "together; a normal startup gives neither"
            )
        self._operations = operations
        self._home = Path(home)
        self._promotion_operation_id = promotion_operation_id
        self._identity_reader = identity_reader
        self._device_reader = device_reader
        self._journal_reader = journal_reader
        self._transport = transport
        self._credential = "unverified"
        self._logout_artifacts: LogoutArtifactPort = (
            logout_artifacts
            if logout_artifacts is not None
            else _IdentityLogoutArtifacts()
        )
        self._request_stop = request_stop
        self._clock = clock
        self._verification_by_id: dict[str, dict[str, Any]] = {}
        self._relogin = ServingRelogin(
            home=self._home,
            profile_resolver=profile_resolver,
            identity_factory=identity_factory,
            identity_reader=identity_reader,
            lock_factory=transaction_factory,
            permit_seconds=permit_seconds,
            clock=clock,
        )
        self._recovery = ServingRecovery(
            operations=operations,
            home=self._home,
            identity_reader=identity_reader,
            device_reader=device_reader,
            journal_reader=journal_reader,
            provisioner=provisioner,
            user_key=user_key,
        )
        self._relogin_lock = threading.RLock()
        self._relogin_driver = ServingReloginDriver(
            lock=self._relogin_lock,
            operations=operations,
            relogin=self._relogin,
            recovery=self._recovery,
            identity_reader=self._identity,
            set_credential=self._set_credential,
            mark_unknown=self._mark_relogin_unknown,
            auth_operation_fn=_auth_operation,
        )
        self._finalizer = None
        if promotion_operation_id is not None and complete_initialize is not None:
            from .service import PromotionFinalizer

            self._finalizer = PromotionFinalizer(complete_initialize)

    # -- shared ------------------------------------------------------------

    def _set_credential(self, value: str) -> None:
        self._credential = value

    def _context(self, context: CallContext, *, owner: str | None) -> CallContext:
        return CallContext(
            surface=context.surface,
            phase="serving",
            # Never rewrite the caller: the operator path would otherwise be
            # relabelled as a GUI principal (a synthesized principal).
            caller=context.caller,
            owner=owner,
        )

    def _identity(self) -> Any:
        try:
            return self._identity_reader(self._home)
        except Exception as error:
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_ERROR,
                "the public identity snapshot is unavailable",
            ) from error

    def _owner(self) -> str | None:
        return self._identity().owner

    def _verification_for(self, record: OperationRecord) -> dict[str, Any] | None:
        if record.state != "pending":
            return None
        verification = self._verification_by_id.get(record.operation_id)
        return None if verification is None else dict(verification)

    # -- account projection ------------------------------------------------

    def account_projection(self, context: CallContext) -> AccountStatus:
        """Return the projection source for ``status``; not a wire method."""

        _require_context(context, "serving", "local-gui")
        return self._account()

    def _account(self) -> AccountStatus:
        return self._account_from(self._trusted_identity())

    def _account_from(self, trusted: _TrustedIdentity) -> AccountStatus:
        return observe_account(
            home=self._home,
            operations_root=self._operations.root,
            identity_reader=lambda _home: trusted.snapshot,
            device_reader=lambda _home: trusted.device,
            journal_reader=self._journal_reader,
            credential=self._credential,
        )

    def _trusted_identity(self) -> _TrustedIdentity:
        if (
            self._identity_reader is read_identity_snapshot
            and self._device_reader is read_device_record
        ):
            try:
                snapshot, device, subject = read_onboarding_identity(self._home)
            except (IdentityRecordError, OSError, ValueError) as error:
                raise OnboardingGatewayError(
                    ipc_errors.DAEMON_ERROR,
                    "the public identity or device record is unreadable",
                ) from error
            return _TrustedIdentity(snapshot, device, subject)
        identity = self._identity()
        try:
            device = self._device_reader(self._home)
        except Exception as error:
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_ERROR,
                "the public device record is unreadable",
            ) from error
        subject = None
        if identity.owner is not None:
            try:
                subject = read_onboarding_subject(self._home)
            except (IdentityRecordError, OSError, ValueError):
                subject = None
        return _TrustedIdentity(identity, device, subject)

    def _first_run_for(self, trusted: _TrustedIdentity) -> FirstRunSnapshot:
        try:
            current = self._trusted_identity()
        except (IdentityRecordError, OSError, ValueError, OnboardingGatewayError):
            return FirstRunSnapshot(state="unavailable")
        if current != trusted:
            return FirstRunSnapshot(state="unavailable")
        if (
            trusted.snapshot.owner is not None
            and getattr(trusted.device, "owner", None) != trusted.snapshot.owner
        ):
            return FirstRunSnapshot(state="unavailable")
        return first_run_status(
            self._home,
            self._operations.root,
            authenticated=trusted.snapshot.owner is not None,
            subject=trusted.subject,
        )

    def first_run_trusted(self) -> tuple[str, OnboardingSubject | None]:
        """The verified owner and stable subject for the P3 attempt port."""
        trusted = self._trusted_identity()
        owner, device_owner = trusted.snapshot.owner, getattr(trusted.device, "owner", None)
        if owner is None or device_owner != owner:
            raise OnboardingGatewayError(ipc_errors.IDENTITY_UNBOUND, "no account owner")
        return owner, trusted.subject

    # -- handler operations ------------------------------------------------

    def status(self, context: CallContext) -> dict[str, Any]:
        checked = _require_context(context, "serving", _SERVING_CALLERS)
        trusted = self._trusted_identity()
        account = self._account_from(trusted)
        effective = self._context(checked, owner=account.owner)
        with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
            receipts = self._operations.status(effective)
        projected = status_snapshot(
            effective,
            (),
            account=account,
            first_run=self._first_run_for(trusted),
        )
        projected["operations"] = receipts["operations"]
        if (attempt := getattr(self, "first_run_attempt", None)) is not None:
            attempt.attach_status(projected, trusted.subject)
        return projected

    def begin_auth(
        self, context: CallContext, *, intent: str, operation_key: str, until: str | None = None
    ) -> dict[str, Any]:
        checked = _require_context(context, "serving", _SERVING_CALLERS)
        if intent == "register":
            raise OnboardingGatewayError(REGISTRATION_UNAVAILABLE, "local account registration is not available")
        if intent not in _AUTH_KINDS:
            raise OnboardingGatewayError(ipc_errors.INVALID_ARGUMENT, "intent must be login or register")
        return self._begin_relogin(checked, operation_key=operation_key, until=until)

    def relogin(
        self, context: CallContext, *, operation_key: str, until: str | None = None
    ) -> dict[str, Any]:
        """Start a same-owner reauthentication with a fresh operation key."""

        checked = _require_context(context, "serving", _SERVING_CALLERS)
        if until is not None and until not in {"authenticated", "serving"}:
            raise OnboardingGatewayError(ipc_errors.INVALID_ARGUMENT, "until must be authenticated or serving")
        return self._begin_relogin(checked, operation_key=operation_key, until=until)

    # -- logout ------------------------------------------------------------

    def logout(
        self,
        context: CallContext,
        *,
        operation_key: str,
        confirm: bool,
        expected_epoch: str | None = None,
    ) -> dict[str, Any]:
        """Complete one logout for this serving home, then request self-stop.

        The daemon is the single onboarding authority and the one journal
        writer, so the transaction is daemon-owned end to end: the declared
        optional ``expectedEpoch`` fence fires before any identity work, the
        acceptance is durable *before* the identity-owned artifacts are removed,
        the receipt reaches a truthful terminal state with the labels the
        removal primitive really reported, and the daemon's own stop is
        requested through the assembly-provided hook after the receipt is
        durable (the reply is written before the drain begins, exactly like the
        internal ``shutdown`` path).  A removal failure is a ``failed`` receipt
        with the labels that really went, never an invented success.
        """

        checked = _require_context(context, "serving", _SERVING_CALLERS)
        if confirm is not True:
            raise OnboardingGatewayError(
                ipc_errors.INVALID_ARGUMENT,
                "logout requires the explicit confirm:true literal",
            )
        if not isinstance(operation_key, str) or not operation_key:
            raise OnboardingGatewayError(
                ipc_errors.INVALID_ARGUMENT, "an operation key is required"
            )
        if expected_epoch is not None:
            if not isinstance(expected_epoch, str) or not expected_epoch:
                raise OnboardingGatewayError(
                    ipc_errors.INVALID_ARGUMENT,
                    "an expected epoch must be a non-empty string",
                )
            if expected_epoch != checked.surface.transport_generation:
                raise OnboardingGatewayError(
                    ipc_errors.STALE_DAEMON_GENERATION,
                    "the transport generation is stale for this logout",
                )
        effective = self._context(checked, owner=self._owner())
        existing = self._visible_logout(effective)
        if existing is not None:
            # One logout per view: a replay (or a second key) observes the
            # receipt instead of accepting a second side effect.
            return _logout_result(existing)
        preview = self._logout_preview()
        operation_id = uuid4().hex
        with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
            accepted = self._operations.begin_logout(
                effective, operation_key, operation_id, uuid4().hex
            )
        if not preview.present:
            # Nothing login-owned is left: complete without stopping or removing.
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                completed = self._operations.mark_logout(
                    effective, accepted.operation_id, "completed", ()
                )
            return _logout_result(completed)
        try:
            removed = tuple(self._logout_artifacts.remove(self._home))
        except LogoutPartialRemoval as error:
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                failed = self._operations.mark_logout(
                    effective, accepted.operation_id, "failed", tuple(error.removed)
                )
            self._request_daemon_stop()
            return _logout_result(failed)
        except LogoutError:
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                failed = self._operations.mark_logout(
                    effective, accepted.operation_id, "failed", ()
                )
            self._request_daemon_stop()
            return _logout_result(failed)
        with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
            completed = self._operations.mark_logout(
                effective, accepted.operation_id, "completed", removed
            )
        self._request_daemon_stop()
        return _logout_result(completed)

    def _visible_logout(self, effective: CallContext) -> OperationRecord | None:
        """The one logout receipt this view and generation may replay.

        A completed logout emptied this home, so the durable owner a receipt
        recorded no longer matches the identity the next replay reads.  The
        owner-scoped service view can therefore miss the receipt exactly once
        the logout it names succeeded; the fallback reads the journal directly,
        scoped to this view and generation, the way the cross-generation
        account projection already does.
        """

        with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
            visible = self._operations.visible(effective)
        existing = next((item for item in visible if item.kind == LOGOUT_KIND), None)
        if existing is not None:
            return existing
        surface = effective.surface
        try:
            state = self._journal_reader(Path(self._operations.root))
        except OperationReceiptError as error:
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_ERROR,
                "the onboarding operation journal is unreadable",
            ) from error
        if state is None:
            return None
        return next(
            (
                record
                for record in state.snapshot.records
                if record.kind == LOGOUT_KIND
                and record.surface.home_binding == surface.home_binding
                and record.surface.host_control_session_id
                == surface.host_control_session_id
                and record.surface.view_session_id == surface.view_session_id
                and record.surface.transport_generation == surface.transport_generation
            ),
            None,
        )

    def _request_daemon_stop(self) -> None:
        """Ask the assembly-provided hook to stop this daemon generation.

        The hook is the application's own stop request, the same fire-and-forget
        entry the internal ``shutdown`` path uses: the serve loop writes the
        logout reply first and starts the graceful drain afterwards.
        """

        if self._request_stop is None:
            return
        self._request_stop()

    def _logout_preview(self) -> LogoutArtifacts:
        """Inspect login-owned artifacts without writing or exposing labels."""

        try:
            return self._logout_artifacts.inspect(self._home)
        except LogoutError as error:
            # A preflight refusal happens before any acceptance write, so the
            # home is untouched and the caller can inspect and retry.
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, str(error)
            ) from error

    def _begin_relogin(
        self, checked: CallContext, *, operation_key: str, until: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(operation_key, str) or not operation_key:
            raise OnboardingGatewayError(ipc_errors.INVALID_ARGUMENT, "an operation key is required")
        with self._relogin_lock:
            identity = self._identity()
            if identity.owner is None:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                    "an ownerless serving home has no credential worker; bootstrap owns the first login",
                )
            effective = self._context(checked, owner=identity.owner)
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                visible = self._operations.visible(effective)
            existing = next((i for i in visible if i.kind in _AUTH_KINDS and i.operation_key == operation_key), None)
            if existing is not None:
                if existing.kind != "login":
                    raise OnboardingGatewayError(AUTH_OPERATION_CONFLICT, "the operation key already belongs to another intent")
                run = self._relogin_driver.run_for(existing.operation_id)
                if run is not None:
                    return replay_begin_result(run)
                return _begin_record_result(existing, "login", self._verification_for(existing))
            logout = next((i for i in visible if i.kind == LOGOUT_KIND and i.state != "completed"), None)
            if logout is not None:
                raise OnboardingGatewayError(AUTH_OPERATION_CONFLICT, "a logout is accepted for this home; finish or inspect it first")
            pending = next((i for i in visible if i.kind in _AUTH_KINDS and i.state == "pending"), None)
            if pending is not None:
                raise OnboardingGatewayError(ipc_errors.OPERATION_IN_PROGRESS, "another reauthentication operation is pending")
            if self._relogin.live:
                raise OnboardingGatewayError(AUTH_OPERATION_CONFLICT, "another reauthentication is already in flight")
            self._relogin.precheck(identity)
            operation_id = uuid4().hex
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                self._operations.begin_auth(effective, "login", operation_key, operation_id)
            try:
                begin = self._relogin.begin(operation_id=operation_id, expected=identity, transport=self._transport)
            except BaseException:
                self._credential = "failed"
                self._mark_relogin_unknown(effective, operation_id)
                self._relogin.close()
                raise
            self._credential = "pending"
            result = _begin_result(begin, "login")
            verification = result.get("verification")
            if isinstance(verification, dict):
                self._verification_by_id[begin.operation_id] = dict(verification)
            if until == "serving":
                run = self._relogin_driver.start(
                    effective=effective,
                    begin=begin,
                    begin_result=result,
                    expected_identity=identity,
                )
                result["completion"] = run.completion()
            return result

    def get(
        self, context: CallContext, *, operation_id: str, wait_seconds: float | None = None
    ) -> dict[str, Any]:
        checked = _require_context(context, "serving", _SERVING_CALLERS)
        if wait_seconds is not None and (
            isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or wait_seconds < 0 or wait_seconds > 900
        ):
            raise OnboardingGatewayError(ipc_errors.INVALID_ARGUMENT, "waitSeconds must be between 0 and 900")
        with self._relogin_lock:
            effective = self._context(checked, owner=self._owner())
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                record = self._operations.get(effective, operation_id)
            if record.kind == LOGOUT_KIND:
                return {"ok": True, "operation": _logout_operation(record)}
            if record.kind not in _AUTH_KINDS:
                raise OnboardingGatewayError(AUTH_OPERATION_NOT_FOUND, "auth.operation.get is limited to login, registration and logout")
            run = self._relogin_driver.run_for(operation_id)
            if run is None:
                if record.state == "pending" and self._relogin.live_operation_id == operation_id:
                    self._reconcile_relogin(effective)
                    with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                        record = self._operations.get(effective, operation_id)
                return {"ok": True, "operation": _auth_operation(record, self._verification_for(record))}
        if wait_seconds is not None and wait_seconds > 0:
            return wait_relogin_operation(run, float(wait_seconds), stopped=self._relogin_driver.stopped_event)
        return run.answer()

    def cancel(self, context: CallContext, *, operation_id: str) -> dict[str, Any]:
        checked = _require_context(context, "serving", _SERVING_CALLERS)
        with self._relogin_lock:
            identity = self._identity()
            effective = self._context(checked, owner=identity.owner)
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                record = self._operations.get(effective, operation_id)
            if record.kind in _AUTH_KINDS and record.state == "pending":
                if self._relogin.live_operation_id == operation_id:
                    self._cancel_live_relogin(effective, identity, operation_id)
                else:
                    self._credential = "failed"
                    self._mark_relogin_unknown(effective, operation_id)
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                outcome = self._operations.cancel(effective, operation_id)
        self._relogin_driver.poke(operation_id)
        return _cancel_result(outcome)

    def ensure_device(self, context: CallContext, *, operation_key: str) -> dict[str, Any]:
        _require_context(context, "serving", "local-gui")
        raise OnboardingGatewayError(ipc_errors.CALLER_NOT_AUTHORIZED, "onboarding.device.ensure is bootstrap-only")

    def initialize(self, context: CallContext, *, operation_key: str) -> dict[str, Any]:
        _require_context(context, "serving", "local-gui")
        raise OnboardingGatewayError(ipc_errors.CALLER_NOT_AUTHORIZED, "onboarding.initialize is bootstrap-only")

    # -- relogin reconciliation --------------------------------------------

    def _cancel_live_relogin(
        self, effective: CallContext, identity: Any, operation_id: str
    ) -> None:
        try:
            state = self._relogin.cancel(operation_id)
        except OnboardingGatewayError:
            self._credential = "failed"
            self._mark_relogin_unknown(effective, operation_id)
            self._relogin.close()
            return
        if state.state == "committed":
            self._confirm_relogin(effective, identity, state)
            return
        if state.state == "cancelled":
            self._credential = "unverified"
        else:
            self._credential = "failed"
            self._mark_relogin_unknown(effective, operation_id)
        self._relogin.close()

    def _reconcile_relogin(self, effective: CallContext) -> None:
        operation_id = self._relogin.live_operation_id
        if operation_id is None:
            return
        try:
            state = self._relogin.poll(operation_id)
        except OnboardingGatewayError:
            self._credential = "failed"
            self._mark_relogin_unknown(effective, operation_id)
            self._relogin.close()
            raise
        if state.state == "pending":
            return
        try:
            if state.state == "committed":
                self._confirm_relogin(effective, self._identity(), state)
                return
            if state.state == "failed" and state.error_code == "HOME_ACCOUNT_CONFLICT":
                self._credential = "failed"
                self._mark_relogin_failed(
                    effective, operation_id, project_home_account_conflict(state)
                )
                return
            self._credential = "unverified" if state.state == "cancelled" else "failed"
            self._mark_relogin_unknown(effective, operation_id)
        finally:
            self._relogin.close()

    def _confirm_relogin(
        self, effective: CallContext, identity: Any, state: Any
    ) -> None:
        try:
            try:
                self._relogin.verify_commit(state, expected=identity)
            except OnboardingGatewayError:
                self._credential = "failed"
                self._mark_relogin_unknown(effective, state.operation_id)
                raise
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                self._operations.mark_authenticated(
                    effective, state.operation_id, owner=state.owner
                )
            self._credential = "committed"
        finally:
            self._relogin.close()

    def _mark_relogin_unknown(self, effective: CallContext, operation_id: str) -> None:
        try:
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                self._operations.mark_auth_unknown(effective, operation_id)
        except OnboardingGatewayError:
            pass

    def _mark_relogin_failed(
        self, effective: CallContext, operation_id: str, error: OperationError
    ) -> None:
        try:
            with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
                self._operations.mark_auth_ended(
                    effective, operation_id, "failed", error=error
                )
        except OnboardingGatewayError:
            pass

    # -- control-layer hooks ----------------------------------------------

    def migrate_generation(
        self,
        context: CallContext,
        *,
        authority: RecoveryAuthority,
    ) -> OperationRecord | None:
        """Adopt the claim's transport generation; a normal startup has none.

        Without a claim this returns ``None`` without touching the journal, so
        no initialize receipt is created or migrated.
        """

        checked = _require_context(context, "serving", "local-gui")
        claim = self._promotion_operation_id
        if claim is None:
            return None
        owner = self._identity().owner
        if owner is None:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_UNBOUND,
                "generation migration requires a verified owner",
            )
        effective = self._context(checked, owner=owner)
        with _translated(unavailable_code=ipc_errors.DAEMON_ERROR):
            record = self._operations.migrate_generation(effective, claim, authority)
        finalizer = self._finalizer
        if finalizer is not None:
            finalizer.mark_generation_migrated()
        return record

    def take_post_reply(self) -> Callable[[], None] | None:
        """Serving has no bootstrap quiesce action."""

        return None

    def on_daemon_ready(self) -> None:
        finalizer = self._finalizer
        if finalizer is not None:
            finalizer.mark_daemon_ready()
        self._recovery.start()

    def close(self) -> None:
        """Release a live reauthentication worker and any recovery attempt."""

        self._relogin_driver.shutdown()
        self._recovery.close()
        self._relogin.close()


__all__ = [
    "SERVING_PERMIT_SECONDS",
    "AccountStatus",
    "LogoutArtifactPort",
    "ServingOnboardingGateway",
    "ServingRelogin",
    "StartupDecision",
    "classify_startup",
    "observe_account",
    "startup_decision",
]
