"""Deterministic examples owned by the M2 onboarding MethodSpecs.

Decision: gui-m2-onboarding-contracts-20261105.  Every example is synthetic and
contains no credential, device code, token or live owner.  The same cases feed
the catalog, OpenAPI and contract-mock projections.
"""

from __future__ import annotations
from hyprial.kernel import ipc_errors

from hyprial.identity import CREDENTIAL_ARTIFACT_LABEL
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet

from .specs import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    DEVICE_ENSURE_FAILED,
    REGISTRATION_UNAVAILABLE,
)

KEY = "opkey-0123456789abcdef"
KEY_2 = "opkey-fedcba9876543210"
OP = "op-0123456789abcdef"
RCPT = "rcpt-0123456789abcdef"
DEVICE = "device-0123456789abcdef"
OWNER = "user:alice@example.test"

VERIFICATION = {
    "verificationUri": "https://auth.example.test/device",
    "userCode": "ABCD-EFGH",
    "expiresAt": "2026-11-05T06:00:00Z",
    "intervalSeconds": 5,
}

IDENTITY = {
    "mode": "casdoor",
    "issuer": "https://auth.example.test",
    "source": "built-in default",
}
DEVICE_READY = {
    "ready": True,
    "deviceId": DEVICE,
    "keyGeneration": 1,
    "userKey": {"state": "unbound", "code": ipc_errors.USER_KEY_UNBOUND},
    "error": None,
    "retry": None,
}
DEVICE_NOT_READY = {
    "ready": False,
    "deviceId": None,
    "keyGeneration": None,
    "userKey": {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT},
    "error": {
        "code": DEVICE_ENSURE_FAILED,
        "message": "the device could not be registered during this login",
    },
    "retry": "serving-start",
}


def _completion(stage: str, *, identity=None, device=None, failure=None) -> dict:
    return {
        "stage": stage,
        "identity": identity,
        "device": device,
        "failure": failure,
    }


INVALID_OPERATION_KEY = MethodExampleError(
    ipc_errors.INVALID_ARGUMENT, "operationKey must be at least 16 characters"
)
LOGOUT_CONFLICT = MethodExampleError(
    AUTH_OPERATION_CONFLICT, "another logout already owns this home view"
)


def _case(
    name: str,
    kind: str,
    params: dict,
    *,
    result: dict | None = None,
    error: MethodExampleError | None = None,
    reason: str = "",
) -> MethodExample:
    return MethodExample(
        name=name,
        kind=kind,
        description=name.replace("-", " "),
        params=params,
        result=result,
        error=error,
        reason=reason,
    )


STATUS_EXAMPLES = MethodExampleSet(
    method="onboarding.status",
    cases=(
        _case(
            "fresh-bootstrap",
            "empty",
            {},
            result={
                "ok": True,
                "phase": "bootstrap",
                "authenticated": False,
                "owner": None,
                "username": None,
                "deviceReady": False,
                "initialized": False,
                "operations": [],
                "nextActions": ["login", "register"],
                "firstRun": {
                    "state": "none",
                    "revision": 0,
                    "confirmed": [],
                    "updatedAt": None,
                    "skipped": False,
                },
            },
            reason="a fresh bootstrap has no operations yet",
        ),
        _case(
            "authenticated-device-pending",
            "success",
            {},
            result={
                "ok": True,
                "phase": "bootstrap",
                "authenticated": True,
                "owner": OWNER,
                "username": OWNER,
                "deviceReady": False,
                "initialized": False,
                "operations": [
                    {
                        "operationId": OP,
                        "kind": "login",
                        "state": "authenticated",
                        "receiptId": None,
                    }
                ],
                "nextActions": ["device.ensure", "initialize"],
            },
        ),
        _case(
            "initialize-unknown",
            "success",
            {},
            result={
                "ok": True,
                "phase": "bootstrap",
                "authenticated": True,
                "owner": OWNER,
                "username": OWNER,
                "deviceReady": True,
                "initialized": False,
                "operations": [
                    {
                        "operationId": OP,
                        "kind": "initialize",
                        "state": "unknown",
                        "receiptId": RCPT,
                    }
                ],
                "nextActions": ["initialize"],
            },
        ),
        _case(
            "first-run-recorded",
            "success",
            {},
            result={
                "ok": True,
                "phase": "serving",
                "authenticated": True,
                "owner": OWNER,
                "username": OWNER,
                "deviceReady": True,
                "initialized": True,
                "operations": [],
                "nextActions": [],
                "firstRun": {
                    "state": "recorded",
                    "revision": 1,
                    "confirmed": ["configuration"],
                    "updatedAt": "2026-11-18T00:00:00Z",
                    "skipped": False,
                },
            },
        ),
        _case(
            "unexpected-phase-param",
            "invalid-params",
            {"phase": "bootstrap"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "phase comes from the daemon control context"
            ),
        ),
        _case(
            "stale-generation",
            "domain-error",
            {},
            error=MethodExampleError(
                ipc_errors.STALE_DAEMON_GENERATION, "the transport generation is stale"
            ),
        ),
    ),
)

LOGIN_BEGIN_EXAMPLES = MethodExampleSet(
    method="auth.login.begin",
    cases=(
        _case(
            "begin-login",
            "success",
            {"intent": "login", "operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "pending",
                "verification": VERIFICATION,
                "owner": None,
            },
        ),
        _case(
            "idempotent-authenticated-replay",
            "success",
            {"intent": "login", "operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "authenticated",
                "verification": None,
                "owner": OWNER,
            },
        ),
        _case(
            "begin-login-until-serving",
            "success",
            {"intent": "login", "operationKey": KEY, "until": "serving"},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "pending",
                "verification": VERIFICATION,
                "owner": None,
                "completion": _completion("authorizing"),
            },
        ),
        _case(
            "short-operation-key",
            "invalid-params",
            {"intent": "login", "operationKey": "short"},
            error=INVALID_OPERATION_KEY,
        ),
        _case(
            "registration-unavailable",
            "domain-error",
            {"intent": "register", "operationKey": KEY_2},
            error=MethodExampleError(
                REGISTRATION_UNAVAILABLE,
                "this identity source has no verified registration path",
            ),
        ),
    ),
    empty_not_applicable="begin always returns an operation or a domain error",
)

AUTH_GET_EXAMPLES = MethodExampleSet(
    method="auth.operation.get",
    cases=(
        _case(
            "pending-login",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operation": {
                    "operationId": OP,
                    "kind": "login",
                    "state": "pending",
                    "owner": None,
                    "receiptId": None,
                    "verification": VERIFICATION,
                },
            },
        ),
        _case(
            "unknown-operation-state",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operation": {
                    "operationId": OP,
                    "kind": "login",
                    "state": "unknown",
                    "owner": None,
                    "receiptId": None,
                    "verification": None,
                },
            },
        ),
        _case(
            "failed-home-account-conflict",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operation": {
                    "operationId": OP,
                    "kind": "login",
                    "state": "failed",
                    "owner": None,
                    "receiptId": None,
                    "verification": None,
                    "error": {
                        "code": ipc_errors.IDENTITY_CONFLICT,
                        "message": "此 home 目录已绑定另一个账号并留有其数据，本次登录未写入任何内容。请换一个新的 home 目录，或让 agent 按 docs/guides/migration-account-binding.md 处理后重试。",
                        "data": {
                            "reason": "home-bound-to-another-account",
                            "guide": "docs/guides/migration-account-binding.md",
                            "loginUserId": "user-other-001",
                            "issuer": "https://id.example.invalid",
                        },
                    },
                },
            },
        ),
        _case(
            "serving-after-wait",
            "success",
            {"operationId": OP, "waitSeconds": 30},
            result={
                "ok": True,
                "operation": {
                    "operationId": OP,
                    "kind": "login",
                    "state": "authenticated",
                    "owner": OWNER,
                    "receiptId": RCPT,
                    "verification": None,
                    "completion": _completion(
                        "serving", identity=IDENTITY, device=DEVICE_READY
                    ),
                },
            },
        ),
        _case(
            "serving-with-device-not-ready",
            "success",
            {"operationId": OP, "waitSeconds": 30},
            result={
                "ok": True,
                "operation": {
                    "operationId": OP,
                    "kind": "login",
                    "state": "authenticated",
                    "owner": OWNER,
                    "receiptId": RCPT,
                    "verification": None,
                    "completion": _completion(
                        "serving", identity=IDENTITY, device=DEVICE_NOT_READY
                    ),
                },
            },
        ),
        _case(
            "missing-operation-id",
            "invalid-params",
            {},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "operationId is required"),
        ),
        _case(
            "other-owner-operation",
            "domain-error",
            {"operationId": "op-ffffffffffffffff"},
            error=MethodExampleError(
                AUTH_OPERATION_NOT_FOUND,
                "the operation is not visible in this control context",
            ),
        ),
    ),
    empty_not_applicable="an operation result is never an empty collection",
)

AUTH_CANCEL_EXAMPLES = MethodExampleSet(
    method="auth.operation.cancel",
    cases=(
        _case(
            "cancel-pending",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "cancelled",
                "cancelled": True,
                "reason": "cancelled",
            },
        ),
        _case(
            "identity-already-committed",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "authenticated",
                "cancelled": False,
                "reason": "identity-committed",
            },
        ),
        _case(
            "cancel-promoting",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "promoting",
                "cancelled": False,
                "reason": "not-cancellable",
            },
        ),
        _case(
            "cancel-completed",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "completed",
                "cancelled": False,
                "reason": "identity-committed",
            },
        ),
        _case(
            "cancel-failed",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "failed",
                "cancelled": False,
                "reason": "not-cancellable",
            },
        ),
        _case(
            "cancel-unknown",
            "success",
            {"operationId": OP},
            result={
                "ok": True,
                "operationId": OP,
                "state": "unknown",
                "cancelled": False,
                "reason": "outcome-unknown",
            },
        ),
        _case(
            "missing-operation-id",
            "invalid-params",
            {},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "operationId is required"),
        ),
        _case(
            "other-owner-operation",
            "domain-error",
            {"operationId": "op-ffffffffffffffff"},
            error=MethodExampleError(
                AUTH_OPERATION_NOT_FOUND,
                "the operation is not visible in this control context",
            ),
        ),
    ),
    empty_not_applicable="cancel always returns an operation state",
)

DEVICE_EXAMPLES = MethodExampleSet(
    method="onboarding.device.ensure",
    cases=(
        _case(
            "device-ready",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "identityCommitted": True,
                "deviceReady": True,
                "deviceId": DEVICE,
                "retryable": False,
            },
        ),
        _case(
            "short-operation-key",
            "invalid-params",
            {"operationKey": "short"},
            error=INVALID_OPERATION_KEY,
        ),
        _case(
            "device-unavailable",
            "domain-error",
            {"operationKey": KEY_2},
            error=MethodExampleError(
                DEVICE_ENSURE_FAILED,
                "identity is committed but the device stage failed",
                {
                    "operationId": OP,
                    "receiptId": RCPT,
                    "identityCommitted": True,
                    "deviceReady": False,
                    "retryable": True,
                    "reason": "device-unavailable",
                },
            ),
        ),
        _case(
            "device-outcome-unknown",
            "domain-error",
            {"operationKey": KEY_2},
            error=MethodExampleError(
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                "the device stage outcome is unknown; query the receipt",
                {
                    "operationId": OP,
                    "receiptId": None,
                    "identityCommitted": True,
                    "deviceReady": False,
                    "retryable": False,
                    "reason": "outcome-unknown",
                },
            ),
        ),
    ),
    empty_not_applicable="device.ensure always records a receipt or a partial-success error",
)

LOGOUT_EXAMPLES = MethodExampleSet(
    method="auth.logout",
    cases=(
        _case(
            "logout-accepted",
            "success",
            {"operationKey": KEY, "confirm": True},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "accepted",
                "loggedOut": False,
                "removed": [],
                "issuerRevocation": "not-attempted",
                "nextAction": "poll",
            },
        ),
        _case(
            "logout-completed",
            "success",
            {"operationKey": KEY, "confirm": True},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "completed",
                "loggedOut": True,
                "removed": [
                    CREDENTIAL_ARTIFACT_LABEL,
                    "settings.identityMode",
                    "settings.owner",
                ],
                "issuerRevocation": "not-attempted",
                "nextAction": "login",
            },
        ),
        _case(
            "logout-already-logged-out",
            "empty",
            {"operationKey": KEY_2, "confirm": True},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "completed",
                "loggedOut": True,
                "removed": [],
                "issuerRevocation": "not-attempted",
                "nextAction": "login",
            },
            reason="an already logged out home completes with nothing to remove",
        ),
        _case(
            "logout-failed-partial-removal",
            "success",
            {"operationKey": KEY, "confirm": True},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "failed",
                "loggedOut": False,
                "removed": [CREDENTIAL_ARTIFACT_LABEL],
                "issuerRevocation": "not-attempted",
                "nextAction": "inspect",
            },
        ),
        _case(
            "logout-unknown",
            "success",
            {"operationKey": KEY_2, "confirm": True},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "unknown",
                "loggedOut": False,
                "removed": [],
                "issuerRevocation": "not-attempted",
                "nextAction": "inspect",
            },
        ),
        _case(
            "logout-without-confirmation",
            "invalid-params",
            {"operationKey": KEY},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "confirm must be the literal true"
            ),
        ),
        _case(
            "logout-operation-conflict",
            "domain-error",
            {"operationKey": KEY, "confirm": True},
            error=LOGOUT_CONFLICT,
        ),
    ),
)

RELOGIN_EXAMPLES = MethodExampleSet(
    method="auth.relogin",
    cases=(
        _case(
            "relogin-pending",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "pending",
                "verification": VERIFICATION,
                "owner": None,
            },
        ),
        _case(
            "relogin-authenticated",
            "success",
            {"operationKey": KEY_2},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "authenticated",
                "verification": None,
                "owner": OWNER,
            },
        ),
        _case(
            "relogin-until-serving",
            "success",
            {"operationKey": KEY, "until": "serving"},
            result={
                "ok": True,
                "operationId": OP,
                "intent": "login",
                "state": "pending",
                "verification": VERIFICATION,
                "owner": None,
                "completion": _completion("authorizing"),
            },
        ),
        _case(
            "relogin-missing-operation-key",
            "invalid-params",
            {},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "operationKey is required"
            ),
        ),
        _case(
            "relogin-different-owner",
            "domain-error",
            {"operationKey": KEY},
            error=MethodExampleError(
                ipc_errors.IDENTITY_CONFLICT,
                "the candidate account differs from the local owner",
            ),
        ),
    ),
    empty_not_applicable="relogin always returns a login-begun result",
)

INITIALIZE_EXAMPLES = MethodExampleSet(
    method="onboarding.initialize",
    cases=(
        _case(
            "initialize-accepted",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "accepted",
                "initialized": False,
                "cancelable": True,
            },
        ),
        _case(
            "initialize-promoting",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "promoting",
                "initialized": False,
                "cancelable": False,
            },
        ),
        _case(
            "initialize-cancelled",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "cancelled",
                "initialized": False,
                "cancelable": False,
            },
        ),
        _case(
            "initialize-failed",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "failed",
                "initialized": False,
                "cancelable": False,
            },
        ),
        _case(
            "initialize-completed",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "completed",
                "initialized": True,
                "cancelable": False,
            },
        ),
        _case(
            "initialize-unknown",
            "success",
            {"operationKey": KEY},
            result={
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": "unknown",
                "initialized": False,
                "cancelable": False,
            },
        ),
        _case(
            "missing-operation-key",
            "invalid-params",
            {},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "operationKey is required"),
        ),
        _case(
            "identity-not-bound",
            "domain-error",
            {"operationKey": KEY_2},
            error=MethodExampleError(
                ipc_errors.IDENTITY_UNBOUND,
                "initialize requires a completed identity and trusted owner",
            ),
        ),
    ),
    empty_not_applicable="initialize always returns a durable receipt reference",
)
