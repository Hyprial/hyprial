"""Confirmed deletion reuses Agent reservations and the daemon preview registry.

The socket's OS access control remains the administrative trust boundary;
an agent shell with socket access cannot be distinguished from a human CLI.
Known worker or body identity is refused, without changing the relay allowlist.
The admitted GUI frame, when present, only narrows the confirmation binding.
The owner's user agent is refused at preview (UA1b): it is the owner's
identity on this machine, and confirmation needs a preview.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.kernel import DaemonRequestError, canonical_agent_uri, ipc_errors

from ..entity import translated_agent_errors
from .specs import ERROR_CODES, EXECUTE, PREVIEW
from ...preview import confirmation_caller as _caller


def admit(app, params, *, trusted_message_origin=None):
    _caller(app, params, trusted_message_origin)


def _snapshot(app, agent):
    from hyprial.daemon.impl.composition.cells import agent_adapter_bindings

    receipt = app._agent_registry.home_receipt(agent.actor, require_ready=False, validate_mirror=False)
    routines = app._routines_bound_to(agent.actor)
    return {
        "name": agent.actor,
        "homeToken": receipt.resource_token if receipt is not None else None,
        "adapterBindings": sorted(agent_adapter_bindings(app, agent)),
        "routines": sorted((str(row.get("name", "")), str(row.get("registrationId", ""))) for row in routines),
    }


def _workspace(app, actor):
    workspace = app._read_workspace_summary_for_destroy(actor)
    if workspace is not None:
        return workspace.to_json()
    return {"path": str(Path(app.hyprial_home) / "agents" / actor), "exists": False,
            "files": 0, "bytes": 0, "unreadableEntries": 0}


def _failure(error):
    code = getattr(error, "code", ipc_errors.DAEMON_ERROR)
    if code not in ERROR_CODES:
        code = ipc_errors.DAEMON_ERROR
    return DaemonRequestError(code, "Agent deletion did not complete; inspect the Agent before a new preview")


def preview(app, params, *, trusted_message_origin=None):
    from hyprial.daemon.impl.user_agent.naming import refuse_user_agent_target

    caller, session = _caller(app, params, trusted_message_origin)
    refuse_user_agent_target(app.agents, params["name"], "destroy")
    try:
        with translated_agent_errors():
            agent = app.agents.require(params["name"])
            snapshot = _snapshot(app, agent)
            workspace = _workspace(app, agent.actor)
            spellings = (
                agent.uri,
                canonical_agent_uri(agent.owner, agent.machine, agent.actor),
                agent.actor,
            )
            pending = sum(len(app._inbox.pending_messages(key)) for key in dict.fromkeys(spellings))
            summary = "Stop this Agent, remove its home, bindings and routines, and discard its pending messages. This cannot be undone."
            record = app._agent_destroy_previews.issue(
                PREVIEW, snapshot, caller=caller, session=session, summary=summary,
                resource_version=agent.entity_token,
            )
            return {"ok": True, "actor": agent.uri, "agent": agent.actor,
                    "operationId": record.operation_id, "expiresAt": record.expires_at_ms,
                    "summary": summary, "workspace": workspace,
                    "adapterBindings": snapshot["adapterBindings"],
                    "routines": [row[0] for row in snapshot["routines"]],
                    "pendingMessages": pending, "irreversible": True}
    except Exception as error:
        raise _failure(error) from error


def _replay(record):
    result = record.result
    if result is None:
        raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "deletion is in progress")
    if result.get("ok") is False:
        raise DaemonRequestError(result["code"], result["message"])
    return dict(result)


class _Completed(Exception):
    def __init__(self, record):
        self.record = record


def execute(app, params, *, trusted_message_origin=None):
    caller, session = _caller(app, params, trusted_message_origin)
    registry = app._agent_destroy_previews
    record = registry.lookup(EXECUTE, params, caller=caller, session=session)
    if record.claimed:
        return _replay(record)
    claimed = False

    def confirm(agent):
        nonlocal claimed
        snapshot = _snapshot(app, agent)
        if json.dumps(snapshot, sort_keys=True) != json.dumps(record.params, sort_keys=True):
            raise DaemonRequestError(ipc_errors.PREVIEW_STALE, "Agent bindings changed; preview again")
        receipt = registry.claim(EXECUTE, params, caller=caller, session=session,
                                 resource_version=agent.entity_token, expected_digest=record.params_digest)
        if receipt.completed:
            raise _Completed(receipt)
        claimed = True

    try:
        with translated_agent_errors():
            current = app.agents.current(record.params["name"])
            if current is None or current.entity_token != record.resource_version:
                raise DaemonRequestError(ipc_errors.PREVIEW_STALE, "Agent incarnation changed; preview again")
            result = app._destroy_agent(record.params["name"], expected_entity_token=record.resource_version,
                                        before_effect=confirm)
        if not claimed or result.get("destroyed") is not True:
            raise DaemonRequestError(ipc_errors.PREVIEW_STALE, "Agent incarnation changed; preview again")
        registry.complete(record.operation_id, result)
        return result
    except _Completed as completed:
        return _replay(completed.record)
    except BaseException as error:
        failure = _failure(error)
        if claimed:
            registry.complete(record.operation_id, {"ok": False, "code": failure.code, "message": str(failure)})
        if not isinstance(error, Exception):
            raise
        raise failure from error


BINDINGS = ((PREVIEW, preview), (EXECUTE, execute))
