"""Agent restore-policy command handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.identity import RestorePolicyError
from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.ipc.params import JsonObject, _required_string


def handle_restore_policy(
    method: str,
    params: JsonObject,
    *,
    authority: Any,
    normalize_actor: Callable[[str], str],
    submit_wake: Callable[..., None],
) -> dict[str, Any]:
    """Read or mutate the single restore-policy authority."""

    if method == "agent.restore-threshold":
        threshold_ms = params.get("thresholdMs")
        if (
            not isinstance(threshold_ms, int)
            or isinstance(threshold_ms, bool)
            or threshold_ms < 0
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "thresholdMs must be a non-negative integer",
            )
        policy = authority.set_threshold(threshold_ms)
        return {"ok": True, "restoreThresholdMs": policy.threshold_ms}

    if params.get("list") is True:
        projection = authority.projection()
        return {
            "ok": True,
            "restoreThresholdMs": projection.policy.threshold_ms,
            "agents": dict(projection.policy.agents),
            **(
                {"degradedReason": projection.degraded}
                if projection.degraded is not None
                else {}
            ),
        }

    name = normalize_actor(_required_string(params.get("name"), "name"))
    policy_name = _required_string(params.get("policy"), "policy")
    try:
        policy = authority.set_agent(name, policy_name)
    except RestorePolicyError as error:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    if policy_name == "always":
        submit_wake(name, reason="per-agent-always")
    return {
        "ok": True,
        "actor": name,
        "policy": policy.policy_for(name),
        "restoreThresholdMs": policy.threshold_ms,
    }
