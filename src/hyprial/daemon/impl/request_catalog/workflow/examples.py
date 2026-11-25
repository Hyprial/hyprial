"""Wire examples for the PAC workflow.node.context activation projection."""

from hyprial.identity import PAC_EVENTS_RESYNC, PAC_GRAPH_NOT_FOUND, PAC_NODE_NOT_FOUND
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_BASE = {"ok": True, "schemaVersion": 1, "type": "context", "graphId": "g-11111111", "nodeId": "task",
    "version": 2, "journalId": "j-1", "cursor": 7, "owner": "user:alice", "kind": "task", "briefRef": "brief:task",
    "requires": None, "blockedOn": "human", "flag": False, "setBy": None, "setAt": None, "reasonRef": None,
    "completedCount": 0, "currentActivation": None, "predecessors": [], "active": {"at": 100, "by": "user:alice"},
    "closed": None}
_REQUEST = {"eventId": "e-1", "edge": "a->task", "kind": "turn", "recipient": "user:alice", "node": "task",
    "round": 1, "text": "work", "sender": "user:bob", "at": 100, "messageId": None, "deliveredAt": None, "plan": None}
_ACTIVATION = {"activationId": "a-1", "nodeId": "task", "owner": "user:alice", "round": 1,
    "requests": [_REQUEST], "blockedOn": "human"}
_PARAMS = {"runId": "g-11111111", "target": "task"}
EXAMPLES = (MethodExampleSet(method="workflow.node.context", cases=(
    MethodExample(name="requested", kind="success", description="A node has a recorded current activation.",
        params=_PARAMS, result={**_BASE, "currentActivation": _ACTIVATION}),
    MethodExample(name="no-activation", kind="success", description="A node has no current activation.",
        params=_PARAMS, result=_BASE),
    MethodExample(name="missing-target", kind="invalid-params", description="The target is required.", params={"runId": "g-11111111"},
        error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "workflow.node.context params do not match the declared schema at <root>")),
    MethodExample(name="missing-store", kind="domain-error", description="The PAC database is absent.", params=_PARAMS,
        error=MethodExampleError(PAC_GRAPH_NOT_FOUND, "PAC database does not exist")),
    MethodExample(name="missing-node", kind="domain-error", description="The graph has no such node.", params=_PARAMS,
        error=MethodExampleError(PAC_NODE_NOT_FOUND, "node 'task' not found on 'g-11111111'")),
    MethodExample(name="resync", kind="domain-error", description="The stored schema requires a subscription resync.", params=_PARAMS,
        error=MethodExampleError(PAC_EVENTS_RESYNC, "upgrade the PAC store before subscribing", {"resync": True})),
), empty_not_applicable="A successful context always describes a specific node."),)
