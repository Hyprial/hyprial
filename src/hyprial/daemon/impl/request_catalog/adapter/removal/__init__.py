"""Bind platform removal to the existing operator confirmation protocol."""

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterNotFoundError, AdapterConfigConflictError
from hyprial.daemon.impl.operations.adapter_removal.service import AdapterRemovalService
from ...preview import confirmation_caller
from .specs import PREVIEW, EXECUTE, ERROR_CODES


def _failure(error):
    if isinstance(error, AdapterNotFoundError):
        code = ipc_errors.ADAPTER_NOT_FOUND
    elif isinstance(error, AdapterConfigConflictError):
        code = ipc_errors.PREVIEW_STALE
    else:
        code = getattr(error, "code", ipc_errors.DAEMON_ERROR)
    if code not in ERROR_CODES:
        code = ipc_errors.DAEMON_ERROR
    return DaemonRequestError(code, "Platform deletion is not confirmed; inspect before a new preview")


def preview(app, params, *, trusted_message_origin=None):
    caller, session = confirmation_caller(app, params, trusted_message_origin)
    try:
        snapshot = AdapterRemovalService(app).observe(params["name"])
        summary = "Remove this local platform configuration. The remote app, the Agent and its cell binding remain."
        record = app._adapter_remove_previews.issue(PREVIEW, {"name": snapshot.name},
            caller=caller, session=session, summary=summary, resource_version=snapshot.digest)
        return {"ok": True, "name": snapshot.name, "provider": "lark",
                "operationId": record.operation_id, "expiresAt": record.expires_at_ms,
                "summary": summary, "boundAgent": snapshot.bound_agent,
                "processRunning": snapshot.running, "routes": list(snapshot.routes),
                "credentialRemoved": snapshot.remove_credential and snapshot.credential_digest is not None}
    except Exception as error:
        raise _failure(error) from error


def execute(app, params, *, trusted_message_origin=None):
    caller, session = confirmation_caller(app, params, trusted_message_origin)
    registry = app._adapter_remove_previews
    record = registry.lookup(EXECUTE, params, caller=caller, session=session)
    if record.claimed:
        if record.result is None:
            raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "deletion is not settled")
        if record.result.get("ok") is False:
            raise DaemonRequestError(record.result["code"], record.result["message"])
        return dict(record.result)
    claimed = False

    def confirm(digest):
        nonlocal claimed
        receipt = registry.claim(EXECUTE, params, caller=caller, session=session,
                                 resource_version=digest, expected_digest=record.params_digest)
        if receipt.completed:
            raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "deletion was already submitted")
        claimed = True

    try:
        result = AdapterRemovalService(app).execute(record.params["name"], record.resource_version,
                                                    record.operation_id, caller, confirm)
        registry.complete(record.operation_id, result)
        return result
    except BaseException as error:
        failure = _failure(error)
        if claimed:
            registry.complete(record.operation_id, {"ok": False, "code": failure.code, "message": str(failure)})
        if not isinstance(error, Exception):
            raise
        raise failure from error


BINDINGS = ((PREVIEW, preview), (EXECUTE, execute))
