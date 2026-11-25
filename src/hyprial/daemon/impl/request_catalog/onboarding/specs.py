"""MethodSpec declarations for M2 onboarding and local auth.

Decision: gui-m2-onboarding-contracts-20261105.  These are declarations only.
Importing this module binds no handler, opens no home and starts no daemon.
The common line registers them after the structural packages merge and after
the daemon catalog result projection accepts the phaseCaller metadata.
"""

from __future__ import annotations

from hyprial.error.codes import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    DEVICE_ENSURE_FAILED,
    REGISTRATION_UNAVAILABLE,
)
from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    PhaseCallerSpec,
    ipc_errors,
)

from .schemas import (
    AUTH_GET_PARAMS,
    AUTH_GET_RESULT,
    CANCEL_RESULT,
    DEVICE_ERROR_DATA,
    DEVICE_RESULT,
    FIRST_RUN_ATTEMPT_RESULT,
    FIRST_RUN_BEGIN_PARAMS,
    FIRST_RUN_BIND_PARAMS,
    FIRST_RUN_CANCEL_PARAMS,
    FIRST_RUN_CONFIRM_PARAMS,
    FIRST_RUN_CONFIRM_RESULT,
    FIRST_RUN_SKIP_PARAMS,
    FIRST_RUN_SKIP_RESULT,
    INITIALIZE_RESULT,
    LOGIN_BEGIN_PARAMS,
    LOGIN_BEGIN_RESULT,
    LOGOUT_ERROR_DATA,
    LOGOUT_PARAMS,
    LOGOUT_RESULT,
    OPERATION_KEY_PARAMS,
    OPERATION_PARAMS,
    RELOGIN_PARAMS,
    RELOGIN_RESULT,
    STATUS_PARAMS,
    STATUS_RESULT,
)


BOOTSTRAP_AND_SERVING = PhaseCallerSpec(
    pairs=(
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-gui"),
    ),
)
BOOTSTRAP_ONLY = PhaseCallerSpec(
    pairs=(("bootstrap", "local-bootstrap"),),
)
#: The P3 attempt exists only after login committed and the daemon is serving;
#: a bootstrap caller has no committed epoch, no local Agent and no send.
SERVING_OPERATOR_AND_GUI = PhaseCallerSpec(
    pairs=(
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
)
#: Account-face methods: the child's BootstrapRuntime calls them in bootstrap, and
#: in serving the daemon's own local operator (the CLI forwarded over the daemon
#: request socket) joins the admitted GUI view.
BOOTSTRAP_SERVING_OPERATOR_AND_GUI = PhaseCallerSpec(
    pairs=(
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
)

_READ_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_RESTORING),
    MethodError(ipc_errors.DAEMON_ERROR),
)

_OPERATION_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(AUTH_OPERATION_NOT_FOUND),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_ERROR),
)

_BEGIN_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.IDENTITY_CONFLICT),
    MethodError(ipc_errors.IDENTITY_SOURCE_UNAVAILABLE),
    MethodError(REGISTRATION_UNAVAILABLE),
    MethodError(AUTH_OPERATION_CONFLICT),
    MethodError(ipc_errors.OPERATION_IN_PROGRESS),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_ERROR),
)

# Same-owner reauthentication cannot be refused for registration: there is no
# registration intent on this method at all.
_RELOGIN_ERRORS = tuple(
    error for error in _BEGIN_ERRORS if error.code != REGISTRATION_UNAVAILABLE
)

_DEVICE_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.IDENTITY_UNBOUND),
    MethodError(ipc_errors.IDENTITY_CONFLICT),
    MethodError(DEVICE_ENSURE_FAILED, DEVICE_ERROR_DATA),
    MethodError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, DEVICE_ERROR_DATA),
    MethodError(ipc_errors.OPERATION_IN_PROGRESS),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_ERROR),
    MethodError(AUTH_OPERATION_CONFLICT),
)

_INITIALIZE_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.IDENTITY_UNBOUND),
    MethodError(ipc_errors.IDENTITY_CONFLICT),
    MethodError(ipc_errors.OPERATION_IN_PROGRESS),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_ERROR),
    MethodError(AUTH_OPERATION_CONFLICT),
    MethodError(DEVICE_ENSURE_FAILED, DEVICE_ERROR_DATA),
    MethodError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, DEVICE_ERROR_DATA),
)

_LOGOUT_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(AUTH_OPERATION_CONFLICT),
    MethodError(ipc_errors.OPERATION_IN_PROGRESS),
    MethodError(ipc_errors.IDENTITY_CONFLICT),
    MethodError(ipc_errors.IDENTITY_SOURCE_UNAVAILABLE),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, LOGOUT_ERROR_DATA),
    MethodError(ipc_errors.DAEMON_ERROR),
)


def _read_spec(
    name: str,
    params_schema,
    result_schema,
    errors,
    phase_caller: PhaseCallerSpec,
) -> MethodSpec:
    return MethodSpec(
        name=name,
        owner="daemon.onboarding",
        params_schema=params_schema,
        result_schema=result_schema,
        errors=errors,
        mode="unary",
        mutates=False,
        retry="safe",
        audience=("local-operator",),
        http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")),
        confirmation="none",
        phase_caller=phase_caller,
    )


def _write_spec(
    name: str,
    params_schema,
    result_schema,
    errors,
    phase_caller: PhaseCallerSpec,
    scope: str,
) -> MethodSpec:
    return MethodSpec(
        name=name,
        owner="daemon.onboarding",
        params_schema=params_schema,
        result_schema=result_schema,
        errors=errors,
        mode="unary",
        mutates=True,
        retry="never",
        audience=("local-operator",),
        http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")),
        confirmation="none",
        idempotency=IdempotencySpec(mode="never", scope=scope),
        phase_caller=phase_caller,
    )


STATUS_SPEC = _read_spec(
    "onboarding.status",
    STATUS_PARAMS,
    STATUS_RESULT,
    _READ_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
)

LOGIN_BEGIN_SPEC = _write_spec(
    "auth.login.begin",
    LOGIN_BEGIN_PARAMS,
    LOGIN_BEGIN_RESULT,
    _BEGIN_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
    "onboarding-auth-operation",
)

AUTH_OPERATION_GET_SPEC = _read_spec(
    "auth.operation.get",
    AUTH_GET_PARAMS,
    AUTH_GET_RESULT,
    _OPERATION_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
)

AUTH_OPERATION_CANCEL_SPEC = _write_spec(
    "auth.operation.cancel",
    OPERATION_PARAMS,
    CANCEL_RESULT,
    _OPERATION_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
    "onboarding-auth-cancel",
)

DEVICE_ENSURE_SPEC = _write_spec(
    "onboarding.device.ensure",
    OPERATION_KEY_PARAMS,
    DEVICE_RESULT,
    _DEVICE_ERRORS,
    BOOTSTRAP_ONLY,
    "onboarding-device-ensure",
)

INITIALIZE_SPEC = _write_spec(
    "onboarding.initialize",
    OPERATION_KEY_PARAMS,
    INITIALIZE_RESULT,
    _INITIALIZE_ERRORS,
    BOOTSTRAP_ONLY,
    "onboarding-initialize",
)

AUTH_LOGOUT_SPEC = _write_spec(
    "auth.logout",
    LOGOUT_PARAMS,
    LOGOUT_RESULT,
    _LOGOUT_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
    "onboarding-auth-logout",
)

# A1 declares same-owner reauthentication as a login alias with a fresh key.
# Bootstrap delegates to ordinary begin-login; serving requires the same owner
# (A3).  The handler refuses with DAEMON_NOT_READY until the port exposes
# ``relogin``.
AUTH_RELOGIN_SPEC = _write_spec(
    "auth.relogin",
    RELOGIN_PARAMS,
    RELOGIN_RESULT,
    _RELOGIN_ERRORS,
    BOOTSTRAP_SERVING_OPERATOR_AND_GUI,
    "onboarding-auth-operation",
)

FIRST_RUN_REPLY_UNVERIFIED = "FIRST_RUN_REPLY_UNVERIFIED"
FIRST_RUN_CONFIGURATION_CHANGED = "FIRST_RUN_CONFIGURATION_CHANGED"
FIRST_RUN_ATTEMPT_CONFLICT = "FIRST_RUN_ATTEMPT_CONFLICT"
FIRST_RUN_ATTEMPT_STALE = "FIRST_RUN_ATTEMPT_STALE"
FIRST_RUN_ATTEMPT_NOT_FOUND = "FIRST_RUN_ATTEMPT_NOT_FOUND"
FIRST_RUN_ATTEMPT_NOT_BOUND = "FIRST_RUN_ATTEMPT_NOT_BOUND"
FIRST_RUN_SUBJECT_REQUIRED = "FIRST_RUN_SUBJECT_REQUIRED"
FIRST_RUN_REVISION_CONFLICT = "FIRST_RUN_REVISION_CONFLICT"
FIRST_RUN_OPERATION_KEY_CONFLICT = "FIRST_RUN_OPERATION_KEY_CONFLICT"
FIRST_RUN_STORE_UNAVAILABLE = "FIRST_RUN_STORE_UNAVAILABLE"
FIRST_RUN_STORE_CORRUPT = "FIRST_RUN_STORE_CORRUPT"
FIRST_RUN_STORE_WRITE_FAILED = "FIRST_RUN_STORE_WRITE_FAILED"
FIRST_RUN_STORE_COMMIT_UNCONFIRMED = "FIRST_RUN_STORE_COMMIT_UNCONFIRMED"

_FIRST_RUN_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.IDENTITY_UNBOUND),
    MethodError(FIRST_RUN_SUBJECT_REQUIRED),
    MethodError(FIRST_RUN_CONFIGURATION_CHANGED),
    MethodError(FIRST_RUN_REPLY_UNVERIFIED),
    MethodError(FIRST_RUN_ATTEMPT_CONFLICT),
    MethodError(FIRST_RUN_ATTEMPT_STALE),
    MethodError(FIRST_RUN_ATTEMPT_NOT_FOUND),
    MethodError(FIRST_RUN_ATTEMPT_NOT_BOUND),
    MethodError(FIRST_RUN_REVISION_CONFLICT),
    MethodError(FIRST_RUN_OPERATION_KEY_CONFLICT),
    MethodError(FIRST_RUN_STORE_UNAVAILABLE),
    MethodError(FIRST_RUN_STORE_CORRUPT),
    MethodError(FIRST_RUN_STORE_WRITE_FAILED),
    MethodError(FIRST_RUN_STORE_COMMIT_UNCONFIRMED),
    MethodError(ipc_errors.STALE_DAEMON_GENERATION),
    MethodError(ipc_errors.DAEMON_NOT_READY),
    MethodError(ipc_errors.DAEMON_ERROR),
)

FIRST_RUN_BEGIN_SPEC = _write_spec(
    "onboarding.firstRun.begin",
    FIRST_RUN_BEGIN_PARAMS,
    FIRST_RUN_ATTEMPT_RESULT,
    _FIRST_RUN_ERRORS,
    SERVING_OPERATOR_AND_GUI,
    "onboarding-first-run-attempt",
)

FIRST_RUN_BIND_SPEC = _write_spec(
    "onboarding.firstRun.bind",
    FIRST_RUN_BIND_PARAMS,
    FIRST_RUN_ATTEMPT_RESULT,
    _FIRST_RUN_ERRORS,
    SERVING_OPERATOR_AND_GUI,
    "onboarding-first-run-attempt",
)

FIRST_RUN_CONFIRM_SPEC = _write_spec(
    "onboarding.firstRun.confirm",
    FIRST_RUN_CONFIRM_PARAMS,
    FIRST_RUN_CONFIRM_RESULT,
    _FIRST_RUN_ERRORS,
    SERVING_OPERATOR_AND_GUI,
    "onboarding-first-run-attempt",
)

FIRST_RUN_CANCEL_SPEC = _write_spec(
    "onboarding.firstRun.cancel",
    FIRST_RUN_CANCEL_PARAMS,
    FIRST_RUN_ATTEMPT_RESULT,
    _FIRST_RUN_ERRORS,
    SERVING_OPERATOR_AND_GUI,
    "onboarding-first-run-attempt",
)

FIRST_RUN_SKIP_SPEC = _write_spec(
    "onboarding.firstRun.skip",
    FIRST_RUN_SKIP_PARAMS,
    FIRST_RUN_SKIP_RESULT,
    _FIRST_RUN_ERRORS,
    SERVING_OPERATOR_AND_GUI,
    "onboarding-first-run-receipt",
)

ONBOARDING_SPECS = (
    STATUS_SPEC,
    LOGIN_BEGIN_SPEC,
    AUTH_OPERATION_GET_SPEC,
    AUTH_OPERATION_CANCEL_SPEC,
    DEVICE_ENSURE_SPEC,
    INITIALIZE_SPEC,
    AUTH_LOGOUT_SPEC,
    AUTH_RELOGIN_SPEC,
    FIRST_RUN_BEGIN_SPEC,
    FIRST_RUN_BIND_SPEC,
    FIRST_RUN_CONFIRM_SPEC,
    FIRST_RUN_CANCEL_SPEC,
    FIRST_RUN_SKIP_SPEC,
)
