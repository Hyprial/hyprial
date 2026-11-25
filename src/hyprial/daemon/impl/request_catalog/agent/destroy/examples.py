"""Synthetic deletion examples; the workspace path is a display placeholder."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_WORKSPACE = {"path": "<agent-home>/workspace", "exists": True,
              "files": 2, "bytes": 128, "unreadableEntries": 0}
_PREVIEW = {"ok": True, "actor": "agent:example:host:worker", "agent": "worker",
            "operationId": "example-destroy-operation", "expiresAt": 4102444800000,
            "summary": "Remove this Agent and its home. This cannot be undone.",
            "workspace": _WORKSPACE, "adapterBindings": ["lark-main"],
            "routines": ["worker-duty"], "pendingMessages": 1, "irreversible": True}
_EXECUTE = {"ok": True, "destroyed": True, "actor": _PREVIEW["actor"], "agent": "worker",
            "stopped": [], "destroyedMessages": 1, "unboundAdapters": ["lark-main"],
            "removedRoutines": ["worker-duty"], "irreversible": True,
            "workspace": {**_WORKSPACE, "deleted": True}}
EXAMPLES = (
    MethodExampleSet(method="agent.destroy.preview", empty_not_applicable="A target is required; a missing Agent is refused.", cases=(
        MethodExample(name="ready", kind="success", description="Review the impact before confirming.", params={"name": "worker"}, result=_PREVIEW),
        MethodExample(name="missing-name", kind="invalid-params", description="A target is required.", params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Target required.")),
        MethodExample(name="missing-agent", kind="domain-error", description="The target no longer exists.", params={"name": "missing"}, error=MethodExampleError(ipc_errors.AGENT_NOT_FOUND, "Agent missing.")),
    )),
    MethodExampleSet(method="agent.destroy", empty_not_applicable="Confirmation must identify an existing preview operation.", cases=(
        MethodExample(name="deleted", kind="success", description="Confirmed deletion completed.", params={"operationId": _PREVIEW["operationId"]}, result=_EXECUTE),
        MethodExample(name="name-refused", kind="invalid-params", description="Direct name execution is refused.", params={"name": "worker"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Preview required.")),
        MethodExample(name="expired", kind="domain-error", description="An expired preview requires fresh confirmation.", params={"operationId": "expired"}, error=MethodExampleError(ipc_errors.PREVIEW_EXPIRED, "Preview expired.")),
    )),
)
