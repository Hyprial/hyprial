# Decision record (UUID addressing lane C, 2026-11-03):
# - Ruling U2: anyone may rename for now; the method adds no permission check
#   beyond the operator/bound-agent IPC trust every agent method already has.
#   Cell-based tightening is a later ruling, not this declaration.
# - Rename changes only the display name (implementation spec §2.2): actor,
#   URI, home, grants and pins keep their keys, so the answer is the same
#   agent row ``agent.get`` serves, read after the change: the list row
#   schema without the list-only ``activityHints``/``lastActiveAtMs`` keys.
# - New method, so it is declared in the catalog instead of growing the
#   legacy ``agent.*`` branch.  It calls the directory facade directly; the
#   typed-error translation is the same one the migrated agent reads use.
# - The params object is closed: there is no legacy caller to stay lenient for.
# - A refusal whose code the spec does not declare (the DOMAIN_* settlement
#   codes, an AgentError subclass with its own code) folds into AGENT_ERROR
#   with ``<code>: <message>``, so the cause survives instead of becoming the
#   catalog's bare undeclared-code DAEMON_ERROR.  DOMAIN_COMMAND_TIMEOUT means
#   the outcome is unknown; the caller re-reads with ``agent.get``.
# - HTTP exposure (owner decision, 2026-11-06): agents may rename over HTTP
#   too.  The binding is the agent.create/stop write profile (identity
#   ``none``, mutates, retry never, idempotency never), the same profile the
#   request_http entry admits for every other agent write.
# - UA1b (2026-11-18): the owner's user agent is never renamed
#   (USER_AGENT_IMMUTABLE) and no agent is renamed to its reserved name
#   (INVALID_ARGUMENT); both are declared and raised before the registry is
#   touched.
"""``agent.rename``: give a local agent a new display name."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import AgentError
from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdempotencySpec,
    IdentityBinding,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from . import _AGENT_ROW, translated_agent_errors

AGENT_RENAME = "agent.rename"

_NAME = {"type": "string", "minLength": 1}

_AGENT_RENAME_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_NAME,
            "description": "The agent to rename: actor, display name, a.<id> or agent URI.",
        },
        "displayName": {
            **_NAME,
            "description": "The new display name; the actor name clears it.",
        },
    },
    "required": ["name", "displayName"],
    "additionalProperties": False,
}

_RENAMED_AGENT_ROW = {
    **_AGENT_ROW,
    "required": [
        key for key in _AGENT_ROW["required"]
        if key not in {"activityHints", "lastActiveAtMs"}
    ],
}

_AGENT_RENAME_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "agent": _RENAMED_AGENT_ROW,
    },
    "required": ["ok", "agent"],
    "additionalProperties": False,
}

AGENT_RENAME_SPEC = MethodSpec(
    name=AGENT_RENAME,
    owner="daemon.agent",
    params_schema=_AGENT_RENAME_PARAMS,
    result_schema=_AGENT_RENAME_RESULT,
    errors=(
        MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload."),
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="name or displayName is not a non-empty string; no data payload.",
        ),
        MethodError(
            ipc_errors.INVALID_AGENT_NAME,
            description="The display name is not a bare agent name; no data payload.",
        ),
        MethodError(
            ipc_errors.AGENT_NOT_FOUND,
            description="No local agent answers to that name, id or URI; no data payload.",
        ),
        MethodError(
            ipc_errors.AGENT_EXISTS,
            description="Another live agent already uses the name; no data payload.",
        ),
        MethodError(
            AgentError.code,
            description=(
                "The local agent registry could not complete the rename, or the "
                "command did not settle (outcome unknown: re-read with agent.get); "
                "the message starts with the underlying code; no data payload."
            ),
        ),
    ),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-display-name-write"),
)


def agent_rename(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    from hyprial.daemon.impl.user_agent.naming import (
        refuse_user_agent_name,
        refuse_user_agent_target,
    )

    refuse_user_agent_name(params["displayName"])
    refuse_user_agent_target(app.agents, params["name"], "rename")
    try:
        with translated_agent_errors():
            agent = app.agents.rename(params["name"], params["displayName"])
            with app._worker_status_snapshot():
                return {"ok": True, "agent": app._agent_status_json(agent)}
    except DaemonRequestError as error:
        if error.code in _DECLARED_CODES:
            raise
        raise DaemonRequestError(AgentError.code, f"{error.code}: {error}") from error


_DECLARED_CODES = frozenset(error.code for error in AGENT_RENAME_SPEC.errors)


def _param_error(error: Any, params: Mapping[str, Any]) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    field = path[0] if path else ""
    if getattr(error, "validator", None) == "required":
        missing = [
            name for name in getattr(error, "validator_value", ()) if name not in params
        ]
        field = missing[0] if missing else field
    if field in {"name", "displayName"}:
        return f"{field} must be a non-empty string"
    location = "/".join(path) or "<root>"
    return f"{AGENT_RENAME} params do not match the declared schema at {location}"


AGENT_RENAME_BINDINGS = ((AGENT_RENAME_SPEC, agent_rename, _param_error),)

__all__ = [
    "AGENT_RENAME",
    "AGENT_RENAME_BINDINGS",
    "AGENT_RENAME_SPEC",
    "agent_rename",
]
