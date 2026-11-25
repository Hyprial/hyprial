"""Progress query scenarios validated against its declared MethodSpec."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

PROGRESS_LIST_EXAMPLES = MethodExampleSet(
    method="progress.list",
    cases=(
        MethodExample(
            name="one-progress-event", kind="success",
            description="one self-contained delivery progress observation",
            params={"deliveryId": "delivery-1"},
            result={"ok": True, "daemonEpoch": "epoch-1", "events": [{
                "messageId": "progress:delivery-1:1", "from": "agent:alice:node:worker",
                "recipient": "user:alice", "intent": "progress", "createdAtMs": 1000,
                "schema": "hyprial-progress-event/v1", "progressEvent": True,
                "message": "working", "summary": "working", "deliveryId": "delivery-1",
                "conversationId": "conversation-1", "actor": "agent:alice:node:worker",
                "harness": "codex", "phase": "thinking", "seq": 1, "emittedAtMs": 1000,
            }]},
        ),
        MethodExample(
            name="no-progress", kind="empty", description="no progress rows are visible",
            params={}, result={"ok": True, "events": [], "daemonEpoch": "epoch-1"},
            reason="progress is optional and non-authoritative",
        ),
        MethodExample(
            name="non-object-params", kind="invalid-params",
            description="the registry rejects non-object params before legacy admission",
            params=[],
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "progress.list params must be an object",
            ),
        ),
        MethodExample(
            name="stale-session", kind="domain-error",
            description="a superseded consumer session cannot read progress",
            params={"actor": "agent:alice:node:worker", "sessionRef": "old-session"},
            error=MethodExampleError(ipc_errors.SESSION_SUPERSEDED, "session was superseded"),
        ),
    ),
)
