# Decision record (Z-5 PR 2, owner decision 2026-11-18: first login is one daemon operation):
# - Principle.  The CLI renders and calls daemon methods; the daemon owns the
#   login.  A first login here is exactly three requests, each in its own small
#   function: ``auth.login.begin`` (``until: serving``), ``auth.operation.get``
#   (one parked wait) and, only on Ctrl-C, ``auth.operation.cancel``.  Nothing in
#   this file registers a device, initializes, waits for readiness, touches the
#   user key or the identity lock: all of that is the daemon's and is reported
#   back in ``completion``.
# - One wait, no loop.  The daemon ends the authorisation of a driven login
#   itself after the device code's lifetime, capped at 600 s (the begin result
#   publishes the expiry), and the steps after it are bounded daemon work.  A
#   single ``waitSeconds`` of 900 therefore covers the whole login.  A wait that
#   still ends without a terminal stage is reported as an unknown outcome with
#   the daemon's state named, never re-issued: re-asking would be a poll loop in
#   a client, which is exactly what this change removes.  A wait whose transport
#   fails or times out (the daemon died) is the same unknown outcome, with the
#   next steps ``hyprial daemon status`` then ``hyprial login``; no retry.
# - Ctrl-C.  Exactly one ``auth.operation.cancel``, for a Ctrl-C anywhere after
#   ``auth.login.begin`` has answered with an operation (showing the code,
#   resolving the profile, opening the browser, waiting).  A Ctrl-C inside
#   ``_begin``, before any reply, cannot name an operation, so nothing is
#   cancelled and the interrupt propagates as it is.  If the daemon answers that
#   the identity is already committed the login cannot be undone and the daemon
#   finishes it: the command says so, exits 130 and does not wait.  Otherwise the
#   interrupt propagates as before (INTERRUPTED, exit 1).
# - Device not ready.  The login succeeded (exit 0); the result says the device
#   is not ready and states the daemon's own retry rule (one attempt at each
#   daemon serving start, this login's start included).  The CLI never restarts
#   the daemon for it.
# - Codes.  This module imports nothing from ``hyprial.kernel``, ``identity`` or
#   ``daemon``.  The wire error code it raises for an unknown outcome is the
#   ``hyprial.error.codes`` constant (owner decision 2026-11-19: the error
#   vocabulary is outside the backend domains); the other error codes, login
#   states and the cancel reason it compares with are string literals pinned to
#   the daemon's own constants by
#   ``tests/shell/login/daemon_first/test_contract.py`` (precedent:
#   ``common/ensure/scope.py``).
# - Browser.  The one existing opener of the login engine, under its public name.
# - Login code never names an adapter.
"""First login from the CLI: one daemon operation, rendered."""

from __future__ import annotations

from typing import Any

import typer

from hyprial.error.codes import SUBMIT_OUTCOME_UNKNOWN
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject, device_sign_in_hint
from hyprial.shell.impl.cli.output import notice, progress, report_error, warn

WAIT_SECONDS = 900
REQUEST_SECONDS = 915.0
CANCEL_SECONDS = 15.0
PROBE_SECONDS = 5.0
INTERRUPTED_EXIT_CODE = 130

_FAILED_STATES = {
    "denied": ("ACCESS_DENIED", "the sign-in was denied at the identity service"),
    "expired": (
        "DEVICE_CODE_EXPIRED",
        "the device code expired before the sign-in was authorised",
    ),
    "cancelled": ("CANCELLED", "the login was cancelled"),
}
_TERMINAL_STAGES = ("serving", "stopped")
_DEVICE_NEXT_STEP = (
    "the identity stays committed and the daemon serves; it tries to register the "
    "device once at each daemon start (the start of this login included), so to "
    "try again run `hyprial daemon stop` then `hyprial daemon start`"
)


def run_first_login(*, no_open: bool, json_output: bool, started: bool) -> JsonObject:
    """Begin, wait once and map the daemon's terminal state to the login result."""

    services = get_services()
    begun = _begin(services)
    operation_id = begun.get("operationId")
    if not isinstance(operation_id, str):
        raise services.CliError("INVALID_RESPONSE", "the daemon began a login without an operation id")
    verification = begun.get("verification")
    try:
        if isinstance(verification, dict):
            _show_device(services, verification, json_output=json_output)
            if not no_open:
                _open(str(verification.get("verificationUri")))
        answer = _wait(services, operation_id)
    except KeyboardInterrupt:
        _interrupted(services, operation_id, json_output=json_output)
        raise
    return _result(
        services,
        answer,
        verification if isinstance(verification, dict) else {},
        started=started,
        json_output=json_output,
    )


def _begin(services: Any) -> dict[str, Any]:
    result = services.runtime.request(
        "auth.login.begin",
        {
            "intent": "login",
            "operationKey": f"cli-login-{services.uuid4().hex}",
            "until": "serving",
        },
    )
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", "auth.login.begin result must be an object")
    return result


def _wait(services: Any, operation_id: str) -> Any:
    try:
        return services.runtime.request(
            "auth.operation.get",
            {"operationId": operation_id, "waitSeconds": WAIT_SECONDS},
            timeout=REQUEST_SECONDS,
        )
    except (RuntimeError, OSError) as error:
        raise services.CliError(
            SUBMIT_OUTCOME_UNKNOWN,
            "the daemon stopped answering while this login was running; its "
            f"outcome is not known to this command ({error})",
            {
                "operationId": operation_id,
                "transport": getattr(error, "code", type(error).__name__),
                "nextSteps": ["hyprial daemon status", "hyprial login"],
            },
        ) from error


def _cancel(services: Any, operation_id: str) -> Any:
    return services.runtime.request(
        "auth.operation.cancel",
        {"operationId": operation_id},
        timeout=CANCEL_SECONDS,
        restore_wait=0.0,
    )


def _open(uri: str) -> None:
    from hyprial.shell.impl.login import flow as login_flow

    login_flow.open_verification_uri(uri)


def _show_device(services: Any, verification: dict[str, Any], *, json_output: bool) -> None:
    uri = verification.get("verificationUri")
    code = verification.get("userCode")
    interval = verification.get("intervalSeconds")
    expires_at = verification.get("expiresAt")
    if json_output:
        progress(
            "device",
            "",
            json_output=True,
            verificationUri=uri,
            userCode=code,
            interval=interval,
            expiresAt=expires_at,
        )
        return
    profile, _source = services.resolve_profile()
    notice(
        "device authorization required — open this URL and enter the code:\n"
        f"  {uri}\n"
        f"  code: {code}\n"
        f"  (waiting for authorization; the code expires at {expires_at})\n"
        f"  {device_sign_in_hint(profile.issuer)}"
    )


def _interrupted(services: Any, operation_id: str, *, json_output: bool) -> None:
    try:
        cancelled = _cancel(services, operation_id)
    except Exception as error:
        notice(f"login interrupted; the daemon could not be asked to cancel it ({error})")
        return
    reason = cancelled.get("reason") if isinstance(cancelled, dict) else None
    if reason != "identity-committed":
        notice("login cancelled; the daemon is ready for a new `hyprial login`")
        return
    message = (
        "login interrupted after the account was committed; the daemon is finishing "
        "the login on its own, so nothing is undone. Run `hyprial daemon status` to see "
        "when it serves"
    )
    notice(message)
    if json_output:
        report_error(
            services.CliError(
                "INTERRUPTED",
                message,
                {"identityCommitted": True, "nextSteps": ["hyprial daemon status"]},
            ),
            json_output=True,
        )
    raise typer.Exit(code=INTERRUPTED_EXIT_CODE)


def _result(
    services: Any,
    answer: Any,
    verification: dict[str, Any],
    *,
    started: bool,
    json_output: bool,
) -> JsonObject:
    operation = answer.get("operation") if isinstance(answer, dict) else None
    if not isinstance(operation, dict):
        raise services.CliError("INVALID_RESPONSE", "auth.operation.get result has no operation")
    completion = operation.get("completion")
    completion = completion if isinstance(completion, dict) else {}
    state = operation.get("state")
    if completion.get("stage") == "serving" and state == "authenticated":
        return _success(
            services, operation, completion, verification, started=started, json_output=json_output
        )
    raise _failure(services, state, completion, operation.get("error"))


def _failure(
    services: Any,
    state: object,
    completion: dict[str, Any],
    op_error: object = None,
) -> Exception:
    stage = completion.get("stage")
    data: dict[str, Any] = {"state": state, "stage": stage}
    failure = completion.get("failure")
    if isinstance(failure, dict):
        data["failure"] = {"code": failure.get("code"), "message": failure.get("message")}
    if state == "failed" and isinstance(op_error, dict):
        code = op_error.get("code")
        message = op_error.get("message")
        if (
            isinstance(code, str)
            and code
            and isinstance(message, str)
            and message
        ):
            extra = op_error.get("data")
            if isinstance(extra, dict):
                data["errorData"] = dict(extra)
                for key in ("reason", "guide", "loginUserId", "issuer"):
                    value = extra.get(key)
                    if isinstance(value, str) and value:
                        data[key] = value
            return services.CliError(code, message, data)
    refused = _FAILED_STATES.get(state) if isinstance(state, str) else None
    if refused is not None:
        data["nextSteps"] = ["hyprial login"]
        return services.CliError(refused[0], refused[1], data)
    committed = state == "authenticated"
    data["identityCommitted"] = committed
    if stage not in _TERMINAL_STAGES:
        data["nextSteps"] = ["hyprial daemon status"]
        message = (
            f"the daemon is still running this login (stage {stage}) after "
            f"{WAIT_SECONDS} s; its outcome is not known to this command"
        )
    else:
        data["nextSteps"] = ["hyprial daemon start"] if committed else ["hyprial login"]
        message = "the daemon stopped before this login was serving"
    return services.CliError(SUBMIT_OUTCOME_UNKNOWN, message, data)


def _success(
    services: Any,
    operation: dict[str, Any],
    completion: dict[str, Any],
    verification: dict[str, Any],
    *,
    started: bool,
    json_output: bool,
) -> JsonObject:
    owner = operation.get("owner")
    identity = completion.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    return {
        "ok": True,
        "identity": {
            "status": "authenticated",
            "owner": owner,
            "mode": identity.get("mode"),
            "issuer": identity.get("issuer"),
            "source": identity.get("source"),
            "committed": True,
        },
        "device": _device(completion.get("device"), owner, json_output=json_output),
        "daemon": _daemon(services, started=started),
        "migration": {"status": "not-required"},
        "pending": {"status": "not-attempted"},
        "verificationUri": verification.get("verificationUri"),
        "userCode": verification.get("userCode"),
    }


def _device(value: object, owner: object, *, json_output: bool) -> JsonObject:
    device = value if isinstance(value, dict) else {}
    user_key = device.get("userKey")
    user_key = dict(user_key) if isinstance(user_key, dict) else {"state": "unavailable", "code": None}
    if device.get("ready") is True:
        warn(f"  device key: ready (device {device.get('deviceId')})", json_output=json_output)
        return {
            "ready": True,
            "deviceId": device.get("deviceId"),
            "owner": owner,
            "keyGeneration": device.get("keyGeneration"),
            "userKey": user_key,
        }
    error = device.get("error")
    error = error if isinstance(error, dict) else {}
    warn(
        "  device key: NOT provisioned "
        f"({error.get('code', 'DEVICE_KEY_FAILED')}: {error.get('message', '')})\n"
        f"  next step: {_DEVICE_NEXT_STEP}",
        json_output=json_output,
    )
    return {
        "ready": False,
        "userKey": user_key,
        "error": error or None,
        "retry": device.get("retry"),
        "nextStep": _DEVICE_NEXT_STEP,
    }


def _daemon(services: Any, *, started: bool) -> JsonObject:
    try:
        probe = services._daemon_probe(timeout=PROBE_SECONDS)
    except Exception as error:
        return {"state": "unknown", "code": getattr(error, "code", type(error).__name__)}
    if not isinstance(probe, dict):
        return {"state": "unknown", "code": "INVALID_RESPONSE"}
    return {"ok": True, "started": started, "alreadyRunning": not started, **probe}
