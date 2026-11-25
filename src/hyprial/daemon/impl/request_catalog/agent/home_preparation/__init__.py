"""Agent lookup, readiness diagnostics and local home preparation over IPC."""

from collections.abc import Mapping
from typing import Any

from hyprial.identity import AgentError
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

from ..entity import agent_status_row, call_with_legacy_agent_errors
from .schemas import (
    CONTEXT_PARAMS,
    CONTEXT_RESULT,
    ENSURE_RESULT,
    NAME_PARAMS,
    READINESS_PARAMS,
    READINESS_RESULT,
    CREDENTIAL_ERROR_DATA,
    record,
)


def _binding(spec: MethodSpec):
    def handle(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
        return call_with_legacy_agent_errors(
            lambda: app._handle_agent(spec.name, dict(params))
        )

    return spec, handle


_LOOKUP_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.AGENT_NOT_FOUND),
    MethodError(ipc_errors.INVALID_AGENT_NAME),
    MethodError(AgentError.code),
)
_WRITE_ERRORS = (*_LOOKUP_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE))

SPECS = (
    MethodSpec(
        name="agent.get",
        owner="daemon.agent",
        params_schema=NAME_PARAMS,
        result_schema=record(
            {"ok": {"const": True}, "agent": agent_status_row(with_activity=False)}
        ),
        errors=_LOOKUP_ERRORS,
        ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="safe", scope="agent-lookup"),
    ),
    MethodSpec(
        name="agent.readiness",
        owner="daemon.agent",
        params_schema=READINESS_PARAMS,
        result_schema=READINESS_RESULT,
        errors=_LOOKUP_ERRORS,
        ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="safe", scope="agent-readiness"),
    ),
    MethodSpec(
        name="agent.ensure-home",
        owner="daemon.agent",
        params_schema=NAME_PARAMS,
        result_schema=ENSURE_RESULT,
        errors=_WRITE_ERRORS,
        mutates=True,
        retry="never",
        ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="never", scope="agent-home"),
    ),
    MethodSpec(
        name="agent.runtime-context",
        owner="daemon.agent",
        params_schema=CONTEXT_PARAMS,
        result_schema=CONTEXT_RESULT,
        errors=(
            *_WRITE_ERRORS,
            MethodError(ipc_errors.AGENT_HOME_ACCESS_FAILED),
            MethodError(ipc_errors.AGENT_RUNTIME_CONTEXT_STALE),
            MethodError(ipc_errors.AGENT_MIGRATION_BUSY),
            MethodError(ipc_errors.AGENT_LAUNCH_IN_PROGRESS),
            MethodError(ipc_errors.AGENT_VERSION_CONFLICT),
            MethodError("GIT_AUTHOR_UNRESOLVED"),
            MethodError(ipc_errors.CODEX_CREDENTIAL_MISSING, CREDENTIAL_ERROR_DATA),
        ),
        mutates=True,
        retry="never",
        ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="never", scope="agent-launch-preparation"),
    ),
)

BINDINGS = tuple(_binding(spec) for spec in SPECS)
