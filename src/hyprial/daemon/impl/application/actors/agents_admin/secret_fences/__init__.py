"""Expected-instance guard; trusted internal conditions precede public list conditions."""

from hyprial.kernel import DaemonRequestError, ipc_errors


def expected_secret_target(app, method, params, expected):
    if expected is None and method == "agent.secret.list":
        expected = params.get("expectedAgent")
    if expected is None:
        return None
    if (method not in {"agent.secret.grant", "agent.secret.list"}
            or not isinstance(expected, dict) or set(expected) != {"uri", "createdAtMs"}
            or not isinstance(expected["uri"], str) or not expected["uri"]
            or type(expected["createdAtMs"]) is not int or expected["createdAtMs"] < 0
            or not isinstance(params.get("actor"), str) or not params["actor"]):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "invalid internal expected secret instance")
    target = app.agents.get(params["actor"])
    if (target is None or str(target.uri) != expected["uri"]
            or target.created_at_ms != expected["createdAtMs"]):
        raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target agent incarnation changed")
    return target
