"""``agent.attach`` / ``agent.detach``: declarations and real handlers (S8).

Decision record (phase B, S8, 2026-11-03):
- Declared by exact name like ``agent.list``: the ``agent`` family stays out
  of ``MIGRATED_FAMILIES`` and every other ``agent.*`` name keeps its legacy
  branch.  The handlers call ``composition.cells.attach`` only, which uses
  the agent directory's public ports and the composed cell runtime.
- Audience is ``local-operator`` only: attaching or detaching an identity is
  an operator decision on this machine, not something a bound agent does for
  itself or for a peer.  The local socket is that operator boundary, as for
  ``agent.create``; no caller parameter is accepted.
- Both mutate and behave idempotently by agent name: repeating a call with
  the same name converges on the same agent incarnation and attachment
  (``changed: false``), never a second identity.  The declared retry is
  still ``never`` (scope ``agent-attach-by-name``/``agent-detach-by-name``),
  the conservative choice ``agent.create`` makes: the generated GUI client and
  the shared request client admit only ``never``/``safe``, and a client must
  not blindly replay a write whose outcome is unknown; it re-reads instead.
- Catalogued but not HTTP exposed.  The original reason (no mutating method
  on HTTP until write admission H-03 lands) is obsolete since the merge onto
  the release-synced cell-model line: release v0.5.5.4 served ``agent.create``
  and ``agent.stop`` over HTTP as never-retry writes with
  ``IdentityBinding("none")``.  The flag still stays off in that merge,
  because a sync merge does not widen a published surface, the audience is
  ``local-operator`` only (an operator decision on this machine), and GUI
  reading of cells is out of phase B scope.  The phase B brief asks new
  daemon methods to be served through ``POST /daemon/request/<method>``;
  turning ``http_exposed`` on (with ``HttpBinding(IdentityBinding("none"))``
  like ``agent.create``) is a pending HQ decision (待 HQ 追认), not a merge
  fix.
- New methods, so params are closed (``additionalProperties: false``); there
  is no legacy value domain to preserve.  Results are closed too.
- Error codes are existing registry codes only; ``AGENT_EXISTS`` carries the
  same ``{actor, agent}`` data as ``agent.create``'s refusal.
- A package of its own (merge onto the release-synced cell-model line): the
  release's ``lifecycle``/``start``/``relations`` modules filled
  ``request_catalog/agent`` to the seven-direct-file limit.
- ``cell.state`` names all four registrar outcomes (S8 review): ``deferred``
  alone was misleading for an owner-mismatch skip or a retired cell, which
  are never reconciled automatically.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

AGENT_ATTACH = "agent.attach"
AGENT_DETACH = "agent.detach"
_AGENT_ERROR = "AGENT_ERROR"

_STRING = {"type": "string", "minLength": 1}

_ATTACH_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_STRING,
            "description": "Bare local agent name; the canonical agent URI is derived from it.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_DETACH_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_STRING,
            "description": "Local agent name or its canonical agent URI.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_CELL = {
    "type": "object",
    "properties": {
        "state": {
            "enum": ["recorded", "deferred", "skipped", "refused"],
            "description": (
                "recorded: the agent cell matches the agent record in the local cell "
                "cache and is written to OrgFS by the cell runtime; deferred: no cell "
                "runtime is available (OrgFS down) or the write failed transiently, and "
                "the cell is reconciled from the agent record when the runtime returns "
                "or on the registrar's retry; skipped: the agent's owner is not the "
                "OrgFS author, so no agent cell is kept for it; refused: the cell "
                "runtime refused the agent cell (for example it is retired), not "
                "retried until the agent record changes."
            ),
        },
        "revision": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["state", "revision"],
    "additionalProperties": False,
}

_COMMON_RESULT = {
    "ok": {"const": True},
    "actor": {**_STRING, "description": "Canonical agent URI."},
    "name": _STRING,
    "agentId": {**_STRING, "description": "The agent's a. id."},
    "cellId": {**_STRING, "description": "The agent cell's c. id."},
    "custody": {"const": "external"},
    "changed": {
        "type": "boolean",
        "description": "False when the call found the requested state already in place.",
    },
    "cell": _CELL,
}

_COMMON_REQUIRED = [
    "ok", "actor", "name", "agentId", "cellId", "custody", "attached", "changed", "cell",
]

_ATTACH_RESULT = {
    "type": "object",
    "properties": {
        **_COMMON_RESULT,
        "attached": {"const": True},
        "created": {
            "type": "boolean",
            "description": "True when this call registered a new agent identity.",
        },
    },
    "required": [*_COMMON_REQUIRED, "created"],
    "additionalProperties": False,
}

_DETACH_RESULT = {
    "type": "object",
    "properties": {**_COMMON_RESULT, "attached": {"const": False}},
    "required": _COMMON_REQUIRED,
    "additionalProperties": False,
}


def _error(code: str, description: str, data: Mapping[str, Any] | None = None) -> MethodError:
    return MethodError(code, data_schema=data, description=description)


_EXISTS_DATA = {
    "type": "object",
    "properties": {"actor": _STRING, "agent": _STRING},
    "required": ["actor", "agent"],
    "additionalProperties": False,
}

_CONFLICT = _error(
    ipc_errors.AGENT_VERSION_CONFLICT,
    "The agent record changed concurrently; nothing was overwritten. No data payload.",
)
_REGISTRY = _error(
    _AGENT_ERROR,
    "The local agent registry refused or failed the change. No data payload.",
)

ATTACH_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "name is not a valid bare agent name, is held by an adapter, or an unknown "
        "parameter was given. No data payload.",
    ),
    _error(
        ipc_errors.AGENT_EXISTS,
        "The name is held by a daemon-managed agent; attach never converts it.",
        _EXISTS_DATA,
    ),
    _CONFLICT,
    _REGISTRY,
)

DETACH_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        "The agent is daemon-managed (not custody=external), or an unknown parameter "
        "was given. No data payload.",
    ),
    _error(
        ipc_errors.AGENT_NOT_FOUND,
        "No agent with that name is registered on this machine. No data payload.",
    ),
    _CONFLICT,
    _REGISTRY,
)

AGENT_ATTACH_SPEC = MethodSpec(
    name=AGENT_ATTACH,
    owner="daemon.agents",
    params_schema=_ATTACH_PARAMS,
    result_schema=_ATTACH_RESULT,
    errors=(*ATTACH_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-attach-by-name"),
)

AGENT_DETACH_SPEC = MethodSpec(
    name=AGENT_DETACH,
    owner="daemon.agents",
    params_schema=_DETACH_PARAMS,
    result_schema=_DETACH_RESULT,
    errors=(*DETACH_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-detach-by-name"),
)


def agent_attach(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    from hyprial.daemon.impl.composition.cells.attach import attach_agent

    return attach_agent(app, str(params["name"]))


def agent_detach(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    from hyprial.daemon.impl.composition.cells.attach import detach_agent

    return detach_agent(app, str(params["name"]))


def _param_error(method: str, error: Any, params: Mapping[str, Any]) -> str:
    validator = getattr(error, "validator", None)
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    if validator == "additionalProperties":
        extra = sorted(set(params) - {"name"})
        return f"{method} does not accept {', '.join(extra)}"
    if (path[:1] == ["name"] or validator == "required") and validator in {
        "required", "type", "minLength",
    }:
        return "name must be a non-empty string"
    where = "/".join(path) or "<root>"
    return f"{method} params do not match the declared schema at {where}"


AGENT_ATTACH_BINDINGS = (
    (AGENT_ATTACH_SPEC, agent_attach, partial(_param_error, AGENT_ATTACH)),
    (AGENT_DETACH_SPEC, agent_detach, partial(_param_error, AGENT_DETACH)),
)

__all__ = [
    "AGENT_ATTACH",
    "AGENT_ATTACH_BINDINGS",
    "AGENT_ATTACH_SPEC",
    "AGENT_DETACH",
    "AGENT_DETACH_SPEC",
    "agent_attach",
    "agent_detach",
]
