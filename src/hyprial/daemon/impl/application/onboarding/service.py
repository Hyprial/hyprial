"""Bootstrap/serving onboarding request port over the frozen handler seam.

This module is the bridge between the six thin request handlers and the
existing ``BootstrapRuntime``/``OperationService`` assembly.  It owns no
contract declaration, no transport, no credential file and no second journal.

Frozen rules implemented here:

- every handler passes an already-admitted ``CallContext``; the request body and
  ``trusted_message_origin`` never contribute to it;
- bootstrap callers are ``local-bootstrap``, serving callers are ``local-gui``;
- the owner is read from the verified runtime identity, never from the context
  or params that the transport supplied;
- bootstrap and serving use the same ``OperationService`` object (one writer);
- a same-key/same-intent begin returns the existing operation without
  re-dispatching identity; a different key is refused truthfully;
- cancel asks ``IdentityCredentialPort.cancel`` first and reconciles the
  durable receipt before returning a business result;
- initialize returns the durable ``promoting`` claim; quiesce and completion are
  deferred to ``take_post_reply()``/the runtime boundary;
- initialize prepares a missing device through the typed Tailcat port; a stable
  derived key and serialized writes never replay pending/unknown device work.

Decision: daemon-first-z5-first-login-20261118.  ``begin_auth`` takes an optional
``until``.  Absent (or ``authenticated``) it is byte-for-byte what it was: the
client's own ``get`` polls are the clock of the flow.  ``until == "serving"`` asks
the daemon to drive the login to serving (``first_login`` package); the gateway
only decides which login that call means and hands the begun login to the
controller:

* ``awaiting-auth``: begin, then start the driver.
* ``authenticating`` and a driver owns it: return THAT operation (same
  verification, whatever the key), so a second client attaches.
* ``authenticating`` and the client's polls drive it: ``OPERATION_IN_PROGRESS``,
  as for any other key today.
* ``authenticated``/``promoting`` and a driver owns it: the authenticated record
  plus the run's projection; with no driver there is nothing to attach to and the
  answer is ``IDENTITY_CONFLICT`` (the client-clocked flow finishes the login).

``get`` never polls the issuer for a login a driver owns (the driver is the only
poller) and answers a finished run from its frozen record, never from the
journal, which the promotion migrates on another thread.  A login nobody drives
is answered exactly as before and carries no ``completion``.  ``cancel`` wakes
the driver so it observes the new phase at once.
"""

from __future__ import annotations

import threading
from hashlib import sha256
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from hyprial.identity import (
    AuthProfile,
    HttpTransport,
    IdentityBegin,
    IdentityError,
    read_device_record,
)
from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.onboarding.operations.journal import OperationReceiptError
from hyprial.daemon.impl.onboarding.runtime import (
    BOOTSTRAP_IDENTITY_UNAVAILABLE,
    BOOTSTRAP_OPERATION_CONFLICT,
    BOOTSTRAP_PHASE_INVALID,
    BOOTSTRAP_PROMOTION_CONFLICT,
    BOOTSTRAP_SCOPE_MISMATCH,
    BootstrapError,
    BootstrapRuntime,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    CallContext,
    OnboardingStateError,
    OperationRecord,
    Surface,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    DEVICE_ENSURE_FAILED,
    REGISTRATION_UNAVAILABLE,
)

from .account_lifecycle import (
    ServingOnboardingGateway,
    _AUTH_KINDS,
    _auth_operation,
    _begin_record_result,
    _begin_result,
    _cancel_result,
    _verification,
)
from hyprial.daemon.impl.user_key.login_key import ensure_login_user_key

from .errors import OnboardingGatewayError
from .first_login import FirstLogin
from .lifecycle.logout import _logout_operation, _logout_result
from .provisioning import DeviceProvisioner, DeviceProvisioningError

TrustedPhase = Literal["bootstrap", "serving"]
# The Literal is the type surface only: runtime checks stay exact, so
# adding a caller here never widens what a TrustedScope may claim.
TrustedCaller = Literal["local-bootstrap", "local-gui", "local-operator"]

_EXACT_PAIRS = frozenset(
    {("bootstrap", "local-bootstrap"), ("serving", "local-gui")}
)


class OnboardingRequestPort(Protocol):
    """The six handler-facing operations plus the post-reply ready hooks.

    Every request method takes the admitted ``CallContext`` first.  The port
    never accepts a home, surface, phase, caller, owner or generation as a
    request parameter.
    """

    def status(self, context: CallContext) -> dict[str, Any]: ...

    def begin_auth(
        self, context: CallContext, *, intent: str, operation_key: str
    ) -> dict[str, Any]: ...

    def get(
        self,
        context: CallContext,
        *,
        operation_id: str,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def logout(
        self,
        context: CallContext,
        *,
        operation_key: str,
        confirm: bool,
        expected_epoch: str | None = None,
    ) -> dict[str, Any]: ...

    def cancel(
        self, context: CallContext, *, operation_id: str
    ) -> dict[str, Any]: ...

    def ensure_device(
        self, context: CallContext, *, operation_key: str
    ) -> dict[str, Any]: ...

    def initialize(
        self, context: CallContext, *, operation_key: str
    ) -> dict[str, Any]: ...

    def relogin(
        self,
        context: CallContext,
        *,
        operation_key: str,
        until: str | None = None,
    ) -> dict[str, Any]: ...

    def take_post_reply(self) -> Callable[[], None] | None:
        """Return the one-shot action to run after a successful frame write."""

    def on_daemon_ready(self) -> None:
        """Run at the existing daemon.ready boundary (pid file written)."""


@dataclass(frozen=True, slots=True)
class TrustedScope:
    """One verified control-session scope for the current request.

    Only the control/transport layer constructs this value from its verified
    binding.  Request bodies and ``trusted_message_origin`` never contribute.
    Retained as an explicit value object so the public transport can build the
    ``CallContext`` without importing runtime internals.
    """

    surface: Surface
    phase: TrustedPhase
    caller: TrustedCaller

    def __post_init__(self) -> None:
        if not isinstance(self.surface, Surface):
            raise ValueError("trusted scope requires a verified Surface")
        if (self.phase, self.caller) not in _EXACT_PAIRS:
            raise ValueError(
                "trusted scope must be bootstrap/local-bootstrap or "
                "serving/local-gui"
            )


def _require_context(
    context: CallContext, phase: TrustedPhase, caller: TrustedCaller
) -> CallContext:
    if not isinstance(context, CallContext):
        raise OnboardingGatewayError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "an admitted onboarding context is required",
        )
    if (context.phase, context.caller) != (phase, caller):
        raise OnboardingGatewayError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "the admitted control session is not allowed for this method",
        )
    return context


def _profile_with_source(home: Path) -> tuple[AuthProfile, str]:
    from hyprial.daemon import resolve_profile

    network, source = resolve_profile(hyprial_home=Path(home))
    return AuthProfile(issuer=network.issuer, client_id=network.client_id), source


def default_profile_resolver(home: Path) -> AuthProfile:
    """Resolve the official issuer/client for this home through the public face."""

    return _profile_with_source(home)[0]


class PromotionFinalizer:
    """Complete initialize only after daemon.ready and generation migration.

    Both signals are needed: the durable claim is already committed by the
    runtime, but marking ``completed`` before the serving transport generation
    has adopted the record would hide it from the new generation.  The latch is
    idempotent and only runs the completion callback once.
    """

    def __init__(self, complete: Callable[[], None]) -> None:
        if not callable(complete):
            raise ValueError("initialize completion callback must be callable")
        self._complete = complete
        self._lock = threading.RLock()
        self._ready = False
        self._migrated = False
        self._done = False

    def mark_daemon_ready(self) -> None:
        self._signal(ready=True)

    def mark_generation_migrated(self) -> None:
        self._signal(migrated=True)

    def _signal(
        self, *, ready: bool = False, migrated: bool = False
    ) -> None:
        with self._lock:
            self._ready = self._ready or ready
            self._migrated = self._migrated or migrated
            if self._done or not (self._ready and self._migrated):
                return
            self._complete()
            self._done = True


@contextmanager
def _translated(*, unavailable_code: str) -> Iterator[None]:
    """Translate implementation failures into declared wire-level failures."""

    try:
        yield
    except OnboardingGatewayError:
        raise
    except BootstrapError as error:
        raise _bootstrap_error(error, unavailable_code=unavailable_code) from error
    except OnboardingStateError as error:
        raise OnboardingGatewayError(error.code, error.message) from error
    except OperationReceiptError as error:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR, str(error)
        ) from error
    except IdentityError as error:
        raise OnboardingGatewayError(
            ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, str(error)
        ) from error


def _bootstrap_error(
    error: BootstrapError, *, unavailable_code: str
) -> OnboardingGatewayError:
    code = {
        BOOTSTRAP_PHASE_INVALID: ipc_errors.DAEMON_NOT_READY,
        BOOTSTRAP_SCOPE_MISMATCH: AUTH_OPERATION_NOT_FOUND,
        BOOTSTRAP_IDENTITY_UNAVAILABLE: unavailable_code,
        BOOTSTRAP_OPERATION_CONFLICT: AUTH_OPERATION_CONFLICT,
        BOOTSTRAP_PROMOTION_CONFLICT: AUTH_OPERATION_CONFLICT,
    }.get(error.code, ipc_errors.DAEMON_ERROR)
    return OnboardingGatewayError(code, str(error))


def _initialize_result(record: OperationRecord) -> dict[str, Any]:
    state = record.state
    return {
        "ok": True,
        "operationId": record.operation_id,
        "receiptId": record.receipt_id,
        "state": state,
        "initialized": state == "completed",
        "cancelable": state == "accepted",
    }


def _device_error_data(
    record: OperationRecord,
    *,
    device_ready: bool,
    retryable: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "operationId": record.operation_id,
        "receiptId": record.receipt_id,
        "identityCommitted": True,
        "deviceReady": device_ready,
        "retryable": retryable,
        "reason": reason,
    }


class BootstrapOnboardingGateway:
    """Bootstrap phase port: no owner until the worker commit is verified."""

    def __init__(
        self,
        *,
        runtime: BootstrapRuntime,
        profile_resolver: Callable[[Path], AuthProfile] | None = None,
        provisioner: DeviceProvisioner | None = None,
        transport: HttpTransport | None = None,
        user_key: Callable[[Path, Path, str, Any], dict[str, Any]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._device_lock = threading.RLock()
        self._profile_resolver = profile_resolver or default_profile_resolver
        self._provisioner = provisioner
        self._transport = transport
        self._user_key = user_key or ensure_login_user_key
        self._first_login: FirstLogin | None = None
        self._begin_by_key: dict[str, tuple[str, IdentityBegin]] = {}
        self._begin_by_id: dict[str, IdentityBegin] = {}
        self._verification_by_id: dict[str, dict[str, Any]] = {}

    # -- context/projections ----------------------------------------------

    def _context(
        self, context: CallContext, *, owner: str | None
    ) -> CallContext:
        return CallContext(
            surface=context.surface,
            phase="bootstrap",
            caller="local-bootstrap",
            owner=owner,
        )

    def _owner(self) -> str | None:
        return self._runtime.verified_owner()

    def _verification_for(
        self, operation_id: str
    ) -> dict[str, Any] | None:
        verification = self._verification_by_id.get(operation_id)
        return None if verification is None else dict(verification)

    def _remember(
        self, operation_key: str, intent: str, begin: IdentityBegin
    ) -> None:
        self._begin_by_key[operation_key] = (intent, begin)
        self._begin_by_id[begin.operation_id] = begin
        self._verification_by_id[begin.operation_id] = _verification(begin)

    def _forget(self, operation_id: str) -> None:
        self._begin_by_id.pop(operation_id, None)
        self._verification_by_id.pop(operation_id, None)
        for key, (_intent, begin) in tuple(self._begin_by_key.items()):
            if begin.operation_id == operation_id:
                self._begin_by_key.pop(key, None)

    # -- request operations -----------------------------------------------

    def status(self, context: CallContext) -> dict[str, Any]:
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        with _translated(
            unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
        ):
            return self._runtime.status(self._context(checked, owner=self._owner()))

    def attach_first_login(self, controller: FirstLogin) -> None:
        """Give this gateway the controller of daemon-driven logins (once)."""

        if self._first_login is not None:
            raise RuntimeError("the first-login controller is already attached")
        self._first_login = controller

    def ensure_login_key(self, owner: str, device: Any) -> dict[str, Any]:
        """The login user-key step: a secret-free ``{state, code}`` answer."""

        return self._user_key(
            self._runtime.home, self._runtime.state_dir, owner, device
        )

    def begin_auth(
        self,
        context: CallContext,
        *,
        intent: str,
        operation_key: str,
        until: str | None = None,
    ) -> dict[str, Any]:
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        if intent == "register":
            # The register intent is retained in the declared contract, but
            # this bootstrap has no local registration method.  Never fake it.
            raise OnboardingGatewayError(
                REGISTRATION_UNAVAILABLE,
                "local account registration is not available",
            )
        if intent not in _AUTH_KINDS:
            raise OnboardingGatewayError(
                ipc_errors.INVALID_ARGUMENT,
                "intent must be login or register",
            )
        if until == "serving":
            return self._begin_until_serving(checked, intent, operation_key)
        owner = self._owner()
        phase = self._runtime.phase
        if phase == "authenticating":
            cached = self._begin_by_key.get(operation_key)
            if cached is not None:
                cached_intent, cached_begin = cached
                if cached_intent != intent:
                    raise OnboardingGatewayError(
                        AUTH_OPERATION_CONFLICT,
                        "the operation key already belongs to another intent",
                    )
                result = _begin_result(cached_begin, cached_intent)
                result["verification"] = self._verification_for(cached_begin.operation_id)
                return result
            if self._runtime.operation_key != operation_key:
                raise OnboardingGatewayError(
                    ipc_errors.OPERATION_IN_PROGRESS,
                    "another login or registration operation is in flight",
                )
            if self._runtime.operation_intent != intent:
                raise OnboardingGatewayError(
                    AUTH_OPERATION_CONFLICT,
                    "the operation key already belongs to another intent",
                )
            begin = self._runtime.last_begin
            if begin is None:
                raise OnboardingGatewayError(
                    AUTH_OPERATION_CONFLICT,
                    "the in-flight operation cannot be resumed safely",
                )
            self._remember(operation_key, intent, begin)
            result = _begin_result(begin, intent)
            result["verification"] = self._verification_for(begin.operation_id)
            return result

        if phase == "authenticated":
            visible = self._runtime.visible(
                self._context(checked, owner=owner)
            )
            record = next(
                (
                    item
                    for item in visible
                    if item.kind in _AUTH_KINDS
                    and item.operation_key == operation_key
                ),
                None,
            )
            if record is None:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "this bootstrap is already authenticated; a new "
                    "operation key is refused",
                )
            if record.kind != intent:
                raise OnboardingGatewayError(
                    AUTH_OPERATION_CONFLICT,
                    "the operation key already belongs to another intent",
                )
            verification = (
                self._verification_for(record.operation_id)
                if record.state == "pending"
                else None
            )
            return _begin_record_result(record, intent, verification)

        begin, _source = self._begin_fresh(checked, intent, operation_key)
        result = _begin_result(begin, intent)
        result["verification"] = self._verification_for(begin.operation_id)
        return result

    def _begin_fresh(
        self, checked: CallContext, intent: str, operation_key: str
    ) -> tuple[IdentityBegin, str | None]:
        source: str | None = None
        try:
            if self._profile_resolver is default_profile_resolver:
                profile, source = _profile_with_source(self._runtime.home)
            else:
                profile = self._profile_resolver(self._runtime.home)
        except Exception as error:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "the official auth profile is unavailable",
            ) from error
        with _translated(
            unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
        ):
            begin = self._runtime.begin_authentication(
                surface=checked.surface,
                intent=intent,
                operation_key=operation_key,
                operation_id=uuid4().hex,
                profile=profile,
                transport=self._transport,
            )
        self._remember(operation_key, intent, begin)
        return begin, source

    def _begin_until_serving(
        self, checked: CallContext, intent: str, operation_key: str
    ) -> dict[str, Any]:
        controller = self._first_login
        if controller is None:
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_NOT_READY,
                "this bootstrap host cannot drive a login to serving",
            )
        phase = self._runtime.phase
        last = self._runtime.last_begin
        run = None if last is None else controller.run_for(last.operation_id)
        if phase == "authenticating":
            if run is None or run.final or last is None:
                raise OnboardingGatewayError(
                    ipc_errors.OPERATION_IN_PROGRESS,
                    "another login or registration operation is in flight",
                )
            result = _begin_result(last, intent)
            result["verification"] = self._verification_for(last.operation_id)
            result["completion"] = run.completion()
            return result
        if phase in {"authenticated", "promoting"} and run is not None:
            operation = run.operation()
            return {
                "ok": True,
                "operationId": operation["operationId"],
                "intent": intent,
                "state": operation["state"],
                "verification": None,
                "owner": operation["owner"],
                "completion": run.completion(),
            }
        if phase != "awaiting-auth":
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_CONFLICT,
                "this bootstrap is already authenticated and no daemon-driven "
                "login owns it",
            )
        begin, source = self._begin_fresh(checked, intent, operation_key)
        verification = self._verification_for(begin.operation_id) or {}
        try:
            run = controller.start(begin, verification=verification, source=source)
        except BaseException as error:
            with _translated(
                unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
            ):
                self._runtime.cancel_authentication(
                    self._context(checked, owner=None), begin.operation_id
                )
            self._forget(begin.operation_id)
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_ERROR, "the first-login driver did not start"
            ) from error
        result = _begin_result(begin, intent)
        result["verification"] = verification
        result["completion"] = run.completion()
        return result

    def get(
        self,
        context: CallContext,
        *,
        operation_id: str,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        del wait_seconds
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        effective = self._context(checked, owner=self._owner())
        controller = self._first_login
        run = None if controller is None else controller.run_for(operation_id)
        if run is not None and run.frozen:
            return run.answer()
        with _translated(
            unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
        ):
            record = self._runtime.get_operation(effective, operation_id)
            if (
                run is None
                and record.kind in _AUTH_KINDS
                and record.state == "pending"
                and self._runtime.phase == "authenticating"
                and self._runtime.last_begin is not None
                and self._runtime.last_begin.operation_id == record.operation_id
            ):
                # There is no seventh poll method.  A get of the in-flight
                # login/register performs exactly one reconciliation against
                # the credential worker and re-reads the same durable journal;
                # it never re-begins the operation or touches device/initialize.
                self._runtime.poll_authentication(surface=checked.surface)
                effective = self._context(checked, owner=self._owner())
                record = self._runtime.get_operation(effective, operation_id)
        if record.kind == "logout":
            return {"ok": True, "operation": _logout_operation(record)}
        if record.kind not in _AUTH_KINDS:
            raise OnboardingGatewayError(
                AUTH_OPERATION_NOT_FOUND,
                "auth.operation.get is limited to account operations",
            )
        verification = (
            self._verification_for(record.operation_id)
            if record.state == "pending"
            else None
        )
        operation = _auth_operation(record, verification)
        if run is not None:
            operation["completion"] = run.completion()
        return {"ok": True, "operation": operation}

    def logout(
        self,
        context: CallContext,
        *,
        operation_key: str,
        confirm: bool,
        expected_epoch: str | None = None,
    ) -> dict[str, Any]:
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        with _translated(unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE):
            record = self._runtime.logout(
                checked,
                operation_key=operation_key,
                confirm=confirm,
                expected_epoch=expected_epoch,
            )
        return _logout_result(record)

    def cancel(
        self, context: CallContext, *, operation_id: str
    ) -> dict[str, Any]:
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        with _translated(
            unavailable_code=ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
        ):
            outcome = self._runtime.cancel_authentication(
                self._context(checked, owner=self._owner()), operation_id
            )
        self._forget(operation_id)
        if self._first_login is not None:
            self._first_login.poke(operation_id)
        return _cancel_result(outcome)

    def ensure_device(
        self, context: CallContext, *, operation_key: str
    ) -> dict[str, Any]:
        with self._device_lock:
            checked = _require_context(context, "bootstrap", "local-bootstrap")
            owner = self._owner()
            if owner is None:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_UNBOUND,
                    "device.ensure requires a verified owner",
                )
            self._require_resolved_device(checked)
            with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                record = self._runtime.begin_device(
                    surface=checked.surface,
                    operation_key=operation_key,
                    operation_id=uuid4().hex,
                    receipt_id=uuid4().hex,
                )
            if record.state == "ready":
                existing = read_device_record(self._runtime.home)
                if existing is not None:
                    return self._device_success(record, existing.device_id)
            if record.state in {"failed", "unknown"}:
                raise OnboardingGatewayError(
                    DEVICE_ENSURE_FAILED,
                    "the device receipt is already terminal",
                    _device_error_data(
                        record,
                        device_ready=record.state == "ready",
                        retryable=False,
                        reason="device-unavailable",
                    ),
                )
            provisioner = self._provisioner or _default_provisioner()
            try:
                device = provisioner.ensure(home=self._runtime.home, owner=owner)
            except DeviceProvisioningError as error:
                terminal = "unknown" if error.outcome_unknown else "failed"
                self._mark_device_quietly(
                    surface=checked.surface,
                    operation_id=record.operation_id,
                    state=terminal,
                )
                if error.outcome_unknown:
                    raise OnboardingGatewayError(
                        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                        str(error),
                        _device_error_data(
                            record,
                            device_ready=False,
                            retryable=False,
                            reason="outcome-unknown",
                        ),
                    ) from error
                raise OnboardingGatewayError(
                    DEVICE_ENSURE_FAILED,
                    str(error),
                    _device_error_data(
                        record,
                        device_ready=False,
                        retryable=True,
                        reason="device-unavailable",
                    ),
                ) from error
            try:
                with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                    updated = self._runtime.mark_device(
                        surface=checked.surface,
                        operation_id=record.operation_id,
                        state="ready",
                    )
            except OnboardingGatewayError as error:
                raise OnboardingGatewayError(
                    ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                    "device preparation succeeded but the receipt is unconfirmed",
                    _device_error_data(
                        record,
                        device_ready=True,
                        retryable=False,
                        reason="outcome-unknown",
                    ),
                ) from error
            if updated.state != "ready":
                raise OnboardingGatewayError(
                    ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                    "device receipt did not reach ready",
                    _device_error_data(
                        updated,
                        device_ready=True,
                        retryable=False,
                        reason="outcome-unknown",
                    ),
                )
            return self._device_success(updated, device.device_id)

    def _require_resolved_device(self, context: CallContext) -> None:
        status = self.status(context)
        for operation in status["operations"]:
            if operation["kind"] != "device" or operation["state"] not in {"pending", "unknown"}:
                continue
            with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                record = self._runtime.get_operation(
                    self._context(context, owner=self._owner()), operation["operationId"]
                )
            raise OnboardingGatewayError(
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                "device preparation must be reconciled before initialize",
                _device_error_data(record, device_ready=False, retryable=False, reason="outcome-unknown"),
            )

    def initialize(
        self, context: CallContext, *, operation_key: str
    ) -> dict[str, Any]:
        checked = _require_context(context, "bootstrap", "local-bootstrap")
        with self._device_lock:
            owner = self._owner()
            if owner is None:
                raise OnboardingGatewayError(
                    ipc_errors.IDENTITY_UNBOUND,
                    "initialize requires a verified owner",
                )
            status = self.status(checked)
            existing = next((item for item in status["operations"] if item["kind"] == "initialize"), None)
            if existing is not None and existing["state"] in {"completed", "failed", "cancelled", "unknown"}:
                with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                    record = self._runtime.get_operation(
                        self._context(checked, owner=owner), existing["operationId"]
                    )
                return _initialize_result(record)
            if existing is None and not status["deviceReady"]:
                device_key = "initialize-device-" + sha256(operation_key.encode()).hexdigest()
                self.ensure_device(checked, operation_key=device_key)
            with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                record = self._runtime.accept_initialize(
                    surface=checked.surface,
                    operation_key=operation_key,
                    operation_id=uuid4().hex,
                    receipt_id=uuid4().hex,
                    defer_quiesce=True,
                )
            return _initialize_result(record)

    def relogin(
        self,
        context: CallContext,
        *,
        operation_key: str,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Bootstrap reauthentication is an ordinary login begin with a fresh key."""

        if until is None:
            return self.begin_auth(
                context, intent="login", operation_key=operation_key
            )
        return self.begin_auth(
            context, intent="login", operation_key=operation_key, until=until
        )

    # -- post-reply / ready hooks ------------------------------------------

    def take_post_reply(self) -> Callable[[], None] | None:
        """Return the one-shot quiesce action for a successfully written reply."""

        return self._runtime.take_post_reply()

    def on_daemon_ready(self) -> None:
        """The bootstrap port has no daemon.ready completion latch."""

    def _device_success(
        self, record: OperationRecord, device_id: str
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "operationId": record.operation_id,
            "receiptId": record.receipt_id,
            "identityCommitted": True,
            "deviceReady": True,
            "deviceId": device_id,
            "retryable": False,
        }

    def _mark_device_quietly(
        self, *, surface: Surface, operation_id: str, state: str
    ) -> None:
        try:
            with _translated(unavailable_code=ipc_errors.IDENTITY_UNBOUND):
                self._runtime.mark_device(
                    surface=surface, operation_id=operation_id, state=state
                )
        except OnboardingGatewayError:
            # The provisioning failure remains the reported business result;
            # an unconfirmed receipt is never replayed here.
            pass


def _default_provisioner() -> DeviceProvisioner:
    # Imported lazily so unit tests can inject a fake without spawning the
    # sidecar path or importing its binary locator.
    from .provisioning import TailcatDeviceProvisioner

    return TailcatDeviceProvisioner()


__all__ = [
    "BootstrapOnboardingGateway",
    "OnboardingRequestPort",
    "PromotionFinalizer",
    "ServingOnboardingGateway",
    "TrustedScope",
    "default_profile_resolver",
]
