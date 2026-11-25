"""Thin bindings for the eight M2 onboarding MethodSpecs.

Each handler is only a projection:

    handler(app, params, *, trusted_message_origin=None)

``params`` were already validated against the MethodSpec schema by the request
path.  A handler never builds a home, surface, phase, owner, generation or
control session from ``params`` or ``trusted_message_origin``.  The admitted
``CallContext`` comes from ``app.onboarding_context`` and the single request
port from ``app.onboarding``.  If no admitted control frame exists the context
acquisition refuses with CALLER_NOT_AUTHORIZED; a plain IPC frame or an HTTP
bearer/body cannot construct one.

The binding tuple is imported and registered by the public catalog line; this
module does not edit the registry, MethodSpecs, schemas, examples, OpenAPI or
the generated client.  ``take_post_reply`` is a port hook the public IPC calls
only after a complete successful frame write; no handler quiesces directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors, redact

from hyprial.daemon.impl.request_catalog.onboarding.state import CallContext

from .specs import (
    AUTH_LOGOUT_SPEC,
    AUTH_OPERATION_CANCEL_SPEC,
    AUTH_OPERATION_GET_SPEC,
    AUTH_RELOGIN_SPEC,
    DEVICE_ENSURE_SPEC,
    FIRST_RUN_BEGIN_SPEC,
    FIRST_RUN_BIND_SPEC,
    FIRST_RUN_CANCEL_SPEC,
    FIRST_RUN_CONFIRM_SPEC,
    FIRST_RUN_SKIP_SPEC,
    INITIALIZE_SPEC,
    LOGIN_BEGIN_SPEC,
    STATUS_SPEC,
)

PortCall = Callable[[Any, CallContext], dict[str, Any]]


def _port(app: Any) -> Any:
    try:
        port = app.onboarding
    except AttributeError as error:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY,
            "onboarding services are not assembled",
        ) from error
    if port is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY,
            "onboarding services are not assembled",
        )
    return port


def _context(app: Any, trusted_message_origin: str | None) -> CallContext:
    getter = getattr(app, "onboarding_context", None)
    if not callable(getter):
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "no admitted control frame is attached to this request",
        )
    context = getter(trusted_message_origin=trusted_message_origin)
    if not isinstance(context, CallContext):
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "no admitted control context is available for this request",
        )
    return context


def _declared(spec: Any, code: str) -> bool:
    return any(error.code == code for error in spec.errors)


_MAX_FAILURE_CHAIN = 8
_MAX_FAILURE_FRAMES = 32
_MAX_DIAGNOSTIC_TEXT = 256


def _safe_text(value: object, limit: int = _MAX_DIAGNOSTIC_TEXT) -> str:
    text = redact(value)
    if not isinstance(text, str):
        text = str(text)
    return text[:limit]


def _failure_site(error: BaseException) -> str | None:
    frame = error.__traceback__
    site: str | None = None
    for _ in range(_MAX_FAILURE_FRAMES):
        if frame is None:
            break
        code = frame.tb_frame.f_code
        site = _safe_text(
            f"{Path(code.co_filename).name}:{frame.tb_lineno}:{code.co_name}"
        )
        frame = frame.tb_next
    return site


def _failure_chain(error: BaseException) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(_MAX_FAILURE_CHAIN):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        entry = {
            "errorType": _safe_text(type(current).__name__, 128),
        }
        site = _failure_site(current)
        if site is not None:
            entry["failureSite"] = site
        code = getattr(current, "code", None)
        if isinstance(code, str) and code:
            entry["code"] = _safe_text(code, 128)
        chain.append(entry)
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return chain


def _log_failure(
    app: Any,
    spec: Any,
    error: BaseException,
    *,
    code: str | None = None,
) -> None:
    from hyprial.daemon.impl.application.onboarding.errors import OnboardingGatewayError

    logger = getattr(app, "_log", None)
    if logger is None:
        return
    chain = _failure_chain(error)
    fields: dict[str, Any] = {
        "method": _safe_text(spec.name),
        "errorType": _safe_text(type(error).__name__, 128),
        "detail": "onboarding handler failed",
        "errorChain": chain,
    }
    if chain and "failureSite" in chain[0]:
        fields["failureSite"] = chain[0]["failureSite"]
    if isinstance(error, OnboardingGatewayError) and code is None:
        code = error.code
    if isinstance(code, str) and code:
        fields["code"] = _safe_text(code, 128)
    try:
        logger("error", "daemon", "onboarding.handler_failed", **fields)
    except Exception:
        return


def _invoke(
    app: Any,
    spec: Any,
    action: PortCall,
    trusted_message_origin: str | None,
    *,
    port_method: str | None = None,
    preflight: Callable[[], None] | None = None,
) -> dict[str, Any]:
    # Defer the application import until dispatch: the registry itself is
    # constructed before DaemonApplication imports the IPC router.
    from hyprial.daemon.impl.application.onboarding.errors import OnboardingGatewayError

    try:
        if preflight is not None:
            preflight()
        port = _port(app)
        if port_method is not None and not callable(getattr(port, port_method, None)):
            # Declared but not yet assembled (A5 owns the logout lifecycle,
            # A3 the serving relogin path).  Refuse with a declared code; never
            # answer ok for work nobody performed.
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY,
                f"{spec.name} is not assembled in this daemon",
            )
        context = _context(app, trusted_message_origin)
        return action(port, context)
    except DaemonRequestError:
        raise
    except OnboardingGatewayError as error:
        if _declared(spec, error.code):
            if error.code == ipc_errors.DAEMON_ERROR:
                _log_failure(app, spec, error, code=error.code)
            raise DaemonRequestError(error.code, str(error), error.data) from error
        # Every conflict code a handler may legitimately see is now declared on
        # every spec that can raise it (AUTH_OPERATION_CONFLICT is on device and
        # initialize too).  Residual internal receipt/recovery/store codes are
        # never passed through, so the wire contract stays closed.
        _log_failure(app, spec, error, code=error.code)
        raise DaemonRequestError(
            ipc_errors.DAEMON_ERROR,
            f"{spec.name} failed",
        ) from error
    except Exception as error:
        _log_failure(app, spec, error)
        raise DaemonRequestError(
            ipc_errors.DAEMON_ERROR, f"{spec.name} failed"
        ) from error


def onboarding_status(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    del params
    return _invoke(
        app,
        STATUS_SPEC,
        lambda port, context: port.status(context),
        trusted_message_origin,
    )


def _require_bootstrap(app: Any, trusted_message_origin: str | None) -> None:
    """Refuse a first login anywhere but on an ownerless (bootstrap) daemon.

    Checked before the port is acquired, so a daemon with no onboarding port at
    all (an environment-owned one) answers exactly like a serving daemon.
    """

    try:
        context = _context(app, trusted_message_origin)
    except DaemonRequestError:
        context = None
    if context is None or context.phase != "bootstrap":
        raise DaemonRequestError(
            ipc_errors.IDENTITY_CONFLICT,
            "this daemon already has an account; a first login runs only on an "
            "ownerless daemon",
        )


def auth_login_begin(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    if params.get("until") != "serving":
        return _invoke(
            app,
            LOGIN_BEGIN_SPEC,
            lambda port, context: port.begin_auth(
                context,
                intent=params["intent"],
                operation_key=params["operationKey"],
            ),
            trusted_message_origin,
        )
    return _invoke(
        app,
        LOGIN_BEGIN_SPEC,
        lambda port, context: port.begin_auth(
            context,
            intent=params["intent"],
            operation_key=params["operationKey"],
            until="serving",
        ),
        trusted_message_origin,
        preflight=lambda: _require_bootstrap(app, trusted_message_origin),
    )


def auth_operation_get(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    wait_seconds = params.get("waitSeconds")
    if wait_seconds is None:
        return _invoke(
            app,
            AUTH_OPERATION_GET_SPEC,
            lambda port, context: port.get(
                context, operation_id=params["operationId"]
            ),
            trusted_message_origin,
        )
    return _invoke(
        app,
        AUTH_OPERATION_GET_SPEC,
        lambda port, context: port.get(
            context,
            operation_id=params["operationId"],
            wait_seconds=float(wait_seconds),
        ),
        trusted_message_origin,
    )


def auth_operation_cancel(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        AUTH_OPERATION_CANCEL_SPEC,
        lambda port, context: port.cancel(
            context, operation_id=params["operationId"]
        ),
        trusted_message_origin,
    )


def onboarding_device_ensure(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        DEVICE_ENSURE_SPEC,
        lambda port, context: port.ensure_device(
            context, operation_key=params["operationKey"]
        ),
        trusted_message_origin,
    )


def onboarding_initialize(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        INITIALIZE_SPEC,
        lambda port, context: port.initialize(
            context, operation_key=params["operationKey"]
        ),
        trusted_message_origin,
    )


def auth_logout(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        AUTH_LOGOUT_SPEC,
        lambda port, context: port.logout(
            context,
            operation_key=params["operationKey"],
            confirm=params["confirm"],
            expected_epoch=params.get("expectedEpoch"),
        ),
        trusted_message_origin,
        port_method="logout",
    )


def auth_relogin(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    until = params.get("until")
    if until is None:
        return _invoke(
            app,
            AUTH_RELOGIN_SPEC,
            lambda port, context: port.relogin(
                context, operation_key=params["operationKey"]
            ),
            trusted_message_origin,
            port_method="relogin",
        )
    return _invoke(
        app,
        AUTH_RELOGIN_SPEC,
        lambda port, context: port.relogin(
            context, operation_key=params["operationKey"], until=until
        ),
        trusted_message_origin,
        port_method="relogin",
    )


def _first_run(port: Any) -> Any:
    attempt = getattr(port, "first_run_attempt", None)
    if attempt is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY,
            "the first-run attempt port is not assembled",
        )
    return attempt


def onboarding_first_run_begin(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        FIRST_RUN_BEGIN_SPEC,
        lambda port, context: _first_run(port).begin(
            context,
            operation_key=params["operationKey"],
            agent_uri=params["agentUri"],
        ),
        trusted_message_origin,
    )


def onboarding_first_run_bind(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        FIRST_RUN_BIND_SPEC,
        lambda port, context: _first_run(port).bind(
            context,
            attempt_id=params["attemptId"],
            expected_revision=params["expectedRevision"],
            operation_key=params["operationKey"],
            conversation_id=params["conversationId"],
            request_message_id=params["requestMessageId"],
        ),
        trusted_message_origin,
    )


def onboarding_first_run_confirm(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        FIRST_RUN_CONFIRM_SPEC,
        lambda port, context: _first_run(port).confirm(
            context,
            attempt_id=params["attemptId"],
            expected_attempt_revision=params["expectedAttemptRevision"],
            expected_receipt_revision=params["expectedReceiptRevision"],
            operation_key=params["operationKey"],
        ),
        trusted_message_origin,
    )


def onboarding_first_run_cancel(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        FIRST_RUN_CANCEL_SPEC,
        lambda port, context: _first_run(port).cancel(
            context,
            attempt_id=params["attemptId"],
            expected_revision=params["expectedRevision"],
            operation_key=params["operationKey"],
        ),
        trusted_message_origin,
    )


def onboarding_first_run_skip(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    return _invoke(
        app,
        FIRST_RUN_SKIP_SPEC,
        lambda port, context: _first_run(port).skip(
            context,
            expected_revision=params["expectedRevision"],
            operation_key=params["operationKey"],
        ),
        trusted_message_origin,
    )


ONBOARDING_BINDINGS = (
    (STATUS_SPEC, onboarding_status),
    (LOGIN_BEGIN_SPEC, auth_login_begin),
    (AUTH_OPERATION_GET_SPEC, auth_operation_get),
    (AUTH_OPERATION_CANCEL_SPEC, auth_operation_cancel),
    (DEVICE_ENSURE_SPEC, onboarding_device_ensure),
    (INITIALIZE_SPEC, onboarding_initialize),
    (AUTH_LOGOUT_SPEC, auth_logout),
    (AUTH_RELOGIN_SPEC, auth_relogin),
    (FIRST_RUN_BEGIN_SPEC, onboarding_first_run_begin),
    (FIRST_RUN_BIND_SPEC, onboarding_first_run_bind),
    (FIRST_RUN_CONFIRM_SPEC, onboarding_first_run_confirm),
    (FIRST_RUN_CANCEL_SPEC, onboarding_first_run_cancel),
    (FIRST_RUN_SKIP_SPEC, onboarding_first_run_skip),
)

__all__ = [
    "ONBOARDING_BINDINGS",
    "auth_login_begin",
    "auth_logout",
    "auth_operation_cancel",
    "auth_operation_get",
    "auth_relogin",
    "onboarding_device_ensure",
    "onboarding_first_run_begin",
    "onboarding_first_run_bind",
    "onboarding_first_run_cancel",
    "onboarding_first_run_confirm",
    "onboarding_first_run_skip",
    "onboarding_initialize",
    "onboarding_status",
]
