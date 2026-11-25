"""IPC examples for identity introspection."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors


_WHOAMI = {"ok": True, "actor": "agent:owner:node:worker", "sessionRef": "session-1",
           "nodeId": "node", "sessionRegistered": True, "daemonEpoch": "epoch"}
EXAMPLES = (
    MethodExampleSet(method="identity.whoami", empty_not_applicable="The method reports session status rather than a collection.", cases=(
        MethodExample(name="registered", kind="success", description="Current session identity.",
                      params={"actor": "agent:owner:node:worker", "sessionRef": "session-1"}, result=_WHOAMI),
        MethodExample(name="stale", kind="success", description="A stale child still receives its identity and registration status.",
                      params={"actor": "agent:owner:node:worker", "sessionRef": "old"},
                      result={**_WHOAMI, "sessionRef": "old", "sessionRegistered": False}),
        MethodExample(name="non-object", kind="invalid-params", description="The registry rejects non-object parameters.",
                      params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "identity.whoami params must be an object")),
        MethodExample(name="missing-actor", kind="semantic-param-error", description="An actor is required by the legacy handler.",
                      params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string")),
    )),
)
