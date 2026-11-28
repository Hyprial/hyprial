"""The ``agent.start`` declaration: params, result and the enumerated error set.

Decision record (implB3, 2026-11-02; revB2 REJECT @ dd52727e49)
- The start declaration moved out of ``lifecycle.py`` because the forward
  launch enumeration (revB2 B2-1) pushed the shared module over the 1000-line
  structural gate; the create/stop declarations stay where they were.  The
  module imports the shared error tuples from ``lifecycle.py`` and is itself
  imported at the bottom of that module, so ``AGENT_LIFECYCLE_BINDINGS``
  keeps its registry-facing shape and no second binding table appears.
- B2-1: the start error set no longer inherits only the stop lifecycle set.
  ``agent.start`` runs ``LifecycleOperation.create`` whose forward plan
  reaches ``harness.ensure``, so every producer on the forward launch call
  graph can put its own code into the terminal lifecycle result
  (``_finish_start_attempt`` preserves a non-empty ``failure.code`` from
  ``HarnessProcessStarted.error``).  The declared launch family covers
  ``HARNESS_CWD_MISSING`` (launch-cwd validation in the harness IO port),
  ``CLAUDE_CREDENTIAL_MISSING``/``CODEX_CREDENTIAL_MISSING`` (credential
  preflights), ``PI_P2_LAUNCH_UNTRUSTED`` (pi P2 project trust),
  ``AGENT_SECRET_RESOLUTION_FAILED`` (launch-time grant resolution, both the
  child-environment resolver and the custody materializer),
  ``AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED`` (runtime-launch custody lease),
  ``SHARED_CREDENTIAL_CONFLICT`` (launch shared-credential environment
  validation), ``PROVIDER_UNSAFE_PATH`` and ``UNSUPPORTED`` (legacy Claude
  native-root preparation) and the per-launch ``WORKER_PROXY_SETTINGS_INVALID``
  /``WORKER_PROXY_URL_INVALID``/``WORKER_PROXY_VENDORS_INVALID`` read.  Each
  carries only the additive optional ``data.lastError``: the underlying
  domain rejection loses everything but its code on this boundary, and
  ``_start_failure_with_harness_error`` may attach the harness's published
  refusal.  ``AGENT_RUNTIME_CONTEXT_STALE`` (custody seam) stays declared via
  the existing domain tail.  The enumeration module
  (tests/daemon/ipc/contracts/agent/inventory/) walks the launch producer
  modules; a new code literal on the forward launch walk fails the guard
  until declared or excluded with a recorded reason.
- B2-3: the ``agent.start`` binding applies the single legacy ``agent.*``
  translation (``call_with_legacy_agent_errors``) around ``_ipc_agent_start``
  so a direct agent-domain call (``native_actor``, ``agents.get/require``,
  workspace resolution) surfaces as ``DaemonRequestError`` with the domain
  code instead of leaking a raw ``InvalidAgentNameError`` — the same boundary
  fix revBc round 2 (Bc-2) applied to ``agent.create``.  Codes outside the
  declared set still surface as the registry's ``DAEMON_ERROR`` replacement.
- B2-2 belongs to ``DaemonApplication._ipc_agent_start`` (see the decision
  record there): every start — fresh and existing — routes through the
  registered ``agent.create`` authority before the lifecycle attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import AgentAlreadyRunning, AgentError
from hyprial.kernel import (
    HttpBinding,
    IdempotencySpec,
    IdentityBinding,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from .lifecycle import (
    AGENT_START,
    _CREATE_ERRORS,
    _STOP_ERRORS,
    _STRING,
    _WRITE_ERRORS,
    _name_param_error,
    _path,
)


_START_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            **_STRING,
            "description": "The registered agent to start in the background.",
        },
        "harness": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "Harness to start on; omitted uses the registered preference.",
        },
        "provider": {
            "type": ["string", "null"],
            "description": "Model vendor preference for this start.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Model id preference for this start.",
        },
        "cwd": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": (
                "Existing absolute working directory for this start. Omitted/null "
                "preserves the prior launch directory or registered workspace. "
                "A valid selection is saved before launch; a later launch failure "
                "does not roll back that selection. The Agent home is not moved."
            ),
        },
        "ignoreStoredHarnessArgs": {
            "type": "boolean",
            "description": (
                "Ignore stored harnessArgs for this start only without "
                "mutating the Agent record."
            ),
        },
        "approve": {
            "type": "boolean",
            "description": (
                "Pi only: true strictly resolves the explicit existing absolute cwd "
                "and approves that canonical directory path for "
                "this launch and automatic restoration of its saved launch intent. "
                "It is not a one-shot permission. The Pi project guard still applies. "
                "False refuses before mutation even with stored --approve; omitted "
                "preserves existing stored-argument behavior. False does not revoke "
                "prior approval; stop removes the running desired intent. Does not edit harnessArgs."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_START_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "accepted": {
            "type": "boolean",
            "description": "The daemon admitted the background start.",
        },
        "running": {
            "type": "boolean",
            "description": (
                "True only with positive runtime evidence; a start that was "
                "merely accepted never claims it."
            ),
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Non-fatal warnings published by the launch funnel; present "
                "only when the harness reported some."
            ),
        },
    },
    "required": ["ok", "accepted", "running"],
    "additionalProperties": False,
}

_START_ERROR_DATA = {
    "type": "object",
    "properties": {"lastError": {"type": "string", "minLength": 1}},
    "required": [],
    "additionalProperties": False,
}
_HARNESS_ARGS_ERROR_DATA = {
    "type": "object",
    "properties": {
        "harness": _STRING,
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": _STRING,
                    "message": _STRING,
                    "token": {"type": ["string", "null"]},
                },
                "required": ["code", "message", "token"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["harness", "issues"],
    "additionalProperties": False,
}

_START_INHERITED_FROM_CREATE = frozenset(
    {
        ipc_errors.AGENT_EXISTS,
        AgentAlreadyRunning.code,
        # Create-on-start (nested agent.create) and _create_record_before_start.
        ipc_errors.AGENT_DEFAULT_ROUTINE_PENDING,
        ipc_errors.AGENT_DEFAULT_ROUTINE_FAILED,
    }
)

_START_EXCLUDED_FROM_STOP_INHERITANCE = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.TARGET_NOT_AUTHORIZED,
        ipc_errors.AGENT_NOT_FOUND,
        AgentError.code,
        ipc_errors.DAEMON_ERROR,
        ipc_errors.AGENT_EXISTS,
        AgentAlreadyRunning.code,
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    }
)


def _start_launch_data_schema(*, operation_id: bool = False) -> dict:
    properties: dict = {"lastError": {"type": "string", "minLength": 1}}
    required: list[str] = []
    if operation_id:
        properties = {
            "operationId": {"type": "string", "minLength": 1},
            "lastError": {"type": "string", "minLength": 1},
        }
        required = ["operationId"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _start_inherited_create_errors() -> tuple[MethodError, ...]:
    return tuple(
        error for error in _CREATE_ERRORS if error.code in _START_INHERITED_FROM_CREATE
    )


def _start_lifecycle_errors() -> tuple[MethodError, ...]:
    inherited = [
        error
        for error in _STOP_ERRORS
        if error.code not in _START_EXCLUDED_FROM_STOP_INHERITANCE
    ]
    entries = []
    for error in inherited:
        if error.description == "No data: this error carries no data payload.":
            description = (
                f"Lifecycle failure {error.code}: the start did not take "
                "effect; data may name the harness's published refusal."
            )
        elif "the stop" in (error.description or ""):
            description = error.description.replace("the stop", "the start")
        else:
            description = error.description
        entries.append(
            MethodError(
                error.code,
                data_schema=_START_ERROR_DATA,
                description=description,
            )
        )
    entries.append(
        MethodError(
            ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
            data_schema=_start_launch_data_schema(operation_id=True),
            description=(
                "The lifecycle reservation outcome is unknown: the start may "
                "or may not have taken effect and is never retried; data names "
                "the operation id to reconcile and may carry the harness's "
                "published refusal."
            ),
        )
    )
    return tuple(entries)


#: Codes only the forward launch of ``LifecycleOperation.create`` can put
#: into the terminal result.  Each loses everything but its code on this
#: boundary; ``_start_failure_with_harness_error`` may add ``lastError``.
_START_LAUNCH_CODES = (
    "HARNESS_CWD_MISSING",
    "CLAUDE_CREDENTIAL_MISSING",
    ipc_errors.CODEX_CREDENTIAL_MISSING,
    "PI_P2_LAUNCH_UNTRUSTED",
    ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
    "AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED",
    "SHARED_CREDENTIAL_CONFLICT",
    ipc_errors.PROVIDER_UNSAFE_PATH,
    ipc_errors.UNSUPPORTED,
    "WORKER_PROXY_SETTINGS_INVALID",
    "WORKER_PROXY_URL_INVALID",
    "WORKER_PROXY_VENDORS_INVALID",
)

_START_LAUNCH_PRODUCERS = {
    "HARNESS_CWD_MISSING": "the harness IO port's launch-cwd validation",
    "CLAUDE_CREDENTIAL_MISSING": "the Claude credential preflight",
    ipc_errors.CODEX_CREDENTIAL_MISSING: "the Codex credential preflight",
    "PI_P2_LAUNCH_UNTRUSTED": "the pi P2 project-trust resolution",
    ipc_errors.AGENT_SECRET_RESOLUTION_FAILED: "launch-time secret-grant resolution",
    "AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED": "the runtime-launch custody lease",
    "SHARED_CREDENTIAL_CONFLICT": "the launch shared-credential check",
    ipc_errors.PROVIDER_UNSAFE_PATH: "the legacy Claude native-root preparation",
    ipc_errors.UNSUPPORTED: "native credential preparation on this platform",
    "WORKER_PROXY_SETTINGS_INVALID": "the per-launch worker-proxy read",
    "WORKER_PROXY_URL_INVALID": "the per-launch worker-proxy read",
    "WORKER_PROXY_VENDORS_INVALID": "the per-launch worker-proxy read",
}


def _start_launch_errors() -> tuple[MethodError, ...]:
    return tuple(
        MethodError(
            code,
            data_schema=_START_ERROR_DATA,
            description=(
                f"{_START_LAUNCH_PRODUCERS[code].capitalize()} refused the "
                "forward launch; the start did not take effect and is never "
                "retried; data may name the harness's published refusal."
            ),
        )
        for code in _START_LAUNCH_CODES
    )


def _model_launch_errors() -> tuple[MethodError, ...]:
    from hyprial.daemon.impl.harnesses.pi.bindings.errors import MODEL_START_FAILURES

    return tuple(MethodError(code, data_schema={
        "type": "object", "additionalProperties": False,
        "properties": {"lastError": {"type": "string"}, "phase": {"const": phase},
                       "reason": {"type": "string", "enum": list(reasons)}},
        "dependentRequired": {"phase": ["reason"], "reason": ["phase"]},
    }, description="The Pi connection/session precondition refused this start without credential fallback.")
        for code, (phase, reasons) in MODEL_START_FAILURES.items())


_START_ERRORS = _WRITE_ERRORS + (
    MethodError(
        ipc_errors.HARNESS_BINARY_MISSING,
        description=(
            "The selected Claude, Codex or Pi executable was not found on the "
            "daemon's PATH. Start was refused before Agent record or launch "
            "intent mutation; no error data or private paths are exposed. "
            "Install the executable or fix the daemon launch environment."
        ),
    ),
    MethodError(
        ipc_errors.HARNESS_ARGS_INVALID,
        data_schema=_HARNESS_ARGS_ERROR_DATA,
        description="Effective stored and explicit harness arguments are invalid.",
    ),
    MethodError(
        ipc_errors.USE_ADAPTER_COMMAND,
        description=(
            "The requested harness is an external-platform adapter; use the "
            "adapter start command instead."
        ),
    ),
    MethodError(
        ipc_errors.TRANSFER_STARTS_HELD,
        description=(
            "Source custody holds the agent during a transfer; an explicit "
            "start needs an authenticated override."
        ),
    ),
    *_start_inherited_create_errors(),
    *_start_lifecycle_errors(),
    *_start_launch_errors(),
    *_model_launch_errors(),
)

AGENT_START_SPEC = MethodSpec(
    name=AGENT_START,
    owner="daemon.agent.lifecycle",
    params_schema=_START_PARAMS,
    result_schema=_START_RESULT,
    errors=(*_START_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-background-start"),
)


def agent_start(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    from .entity import call_with_legacy_agent_errors

    return call_with_legacy_agent_errors(lambda: app._ipc_agent_start(dict(params)))


def _start_param_error(error: Any, params: Mapping[str, Any]) -> str:
    field_message = _name_param_error(error)
    if field_message is not None:
        return field_message
    path_text = "/".join(_path(error)) or "<root>"
    return f"{AGENT_START} params do not match the declared schema at {path_text}"


__all__ = [
    "AGENT_START",
    "AGENT_START_SPEC",
    "agent_start",
    "_start_param_error",
]
