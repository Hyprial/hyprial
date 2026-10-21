"""Read-only agent readiness RPC, split from the large registry mixin."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_string_param,
    _required_string,
)
from hyprial.identity import (
    AgentHomeError,
    ReadinessError,
    agent_home_mode,
    assess_agent_readiness,
    parse_tool_declaration,
)
from hyprial.kernel import DaemonRequestError, ipc_errors


def _optional_string_list_param(params: JsonObject, key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"{key} must be a list of non-empty strings",
        )
    return value


class _AgentReadinessMixin:
    """Registry-facing readiness handler; reads state, never materializes it."""

    def _agent_readiness(self, params: JsonObject) -> dict[str, Any]:
        name = self.agents.normalize_actor(
            _required_string(params.get("name"), "name")
        )
        agent = self.agents.require(name)
        role = _required_string(params.get("role"), "role")
        harness = (
            _optional_string_param(params.get("harness"), "harness")
            or agent.preferred_harness
            or agent.last_harness
        )
        if not harness:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "harness is required when the agent has no preferred/last harness",
            )
        read_tools = _optional_string_list_param(params, "tools")
        work_paths = _optional_string_list_param(params, "workPaths")
        forgejo_identity = params.get("forgejoIdentity", False)
        if not isinstance(forgejo_identity, bool):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "forgejoIdentity must be a boolean",
            )
        timeout_seconds = params.get("timeoutSeconds", 8.0)
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 60
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "timeoutSeconds must be a number within (0, 60]",
            )
        try:
            declared_tools = tuple(
                parse_tool_declaration(item) for item in read_tools
            )
        except ReadinessError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        receipt = None
        try:
            receipt = self._agent_registry.home_receipt(
                agent.actor, require_ready=False
            )
        except AgentHomeError as error:
            if error.category != "not-provisioned":
                raise
        mode, legacy_reason = agent_home_mode(agent, harness)
        running = (
            self._agent_liveness.binding(agent.uri) is not None
            or self._agent_liveness.binding(agent.actor) is not None
        )
        try:
            report = assess_agent_readiness(
                actor=agent.actor,
                harness=harness,
                role=role,
                mode=mode,
                cwd=(
                    _optional_string_param(params.get("cwd"), "cwd")
                    or agent.cwd
                ),
                config=agent.config,
                agent_home=(None if receipt is None else Path(receipt.path)),
                entity_token=agent.entity_token,
                running=running,
                environ=os.environ,
                declared_tools=declared_tools,
                work_paths=tuple(work_paths),
                forgejo_identity=forgejo_identity,
                expect_login=_optional_string_param(
                    params.get("expectLogin"), "expectLogin"
                ),
                forgejo_host=_optional_string_param(
                    params.get("forgejoHost"), "forgejoHost"
                ),
                timeout_seconds=float(timeout_seconds),
            )
        except ReadinessError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        payload = report.to_json()
        payload["modeReason"] = legacy_reason
        return payload
