"""Private local request for one workflow node's existing snapshot context."""

from hyprial.identity import PAC_EVENTS_RESYNC, PAC_GRAPH_NOT_FOUND, PAC_NODE_NOT_FOUND, PacError
from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, ipc_errors

_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NULL_STRING = {"type": ["string", "null"]}
_NULL_INTEGER = {"type": ["integer", "null"]}
_BLOCKED = {"enum": ["human", "agent", "none"]}


def _object(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_NOTIFICATION = _object({
    "eventId": _STRING, "edge": _STRING, "kind": _STRING, "recipient": _STRING,
    "node": _STRING, "round": _NULL_INTEGER, "text": _STRING, "sender": _STRING,
    "at": _INTEGER, "messageId": _NULL_STRING, "deliveredAt": _NULL_INTEGER,
    "plan": {"type": ["object", "null"]},
}, ["eventId", "edge", "kind", "recipient", "node", "text", "sender", "at", "messageId", "deliveredAt", "plan"])
_ASSIGNMENT = _object({"activationId": _STRING, "nodeId": _STRING, "owner": _STRING,
    "round": _INTEGER, "requests": {"type": "array", "items": _NOTIFICATION}, "blockedOn": _BLOCKED},
    ["activationId", "nodeId", "owner", "round", "requests", "blockedOn"])
_PREDECESSOR = _object({"nodeId": _STRING, "owner": _STRING, "briefRef": _STRING, "flag": {"type": "boolean"},
    "setBy": _NULL_STRING, "setAt": _NULL_INTEGER, "reasonRef": _NULL_STRING},
    ["nodeId", "owner", "briefRef", "flag", "setBy", "setAt", "reasonRef"])
_ATTRIBUTION = {"oneOf": [{"type": "null"}, _object({"at": _INTEGER, "by": _STRING}, ["at", "by"])]}
_ACTIVATION = _object({"incarnation": _INTEGER, "desired": _STRING, "op": _STRING,
    "effectId": _STRING, "operationId": _STRING, "identityMarker": _NULL_STRING, "daemonEpoch": _NULL_STRING},
    ["incarnation", "desired", "op", "effectId", "operationId", "identityMarker", "daemonEpoch"])
_ACTOR = _object({"nodeId": _STRING, "actorName": _STRING, "launchRef": _STRING,
    "activation": {"oneOf": [{"type": "null"}, _ACTIVATION]},
    "assignments": {"type": "array", "items": _ASSIGNMENT}},
    ["nodeId", "actorName", "launchRef", "activation", "assignments"])
_RESULT = _object({
    "ok": {"const": True}, "schemaVersion": {"const": 1}, "type": {"const": "context"},
    "graphId": _STRING, "nodeId": _STRING, "version": _INTEGER, "journalId": _STRING, "cursor": _INTEGER,
    "owner": _STRING, "kind": {"enum": ["task", "clock", "actor", "end"]}, "briefRef": _STRING,
    "flag": {"type": "boolean"}, "setBy": _NULL_STRING, "setAt": _NULL_INTEGER,
    "reasonRef": _NULL_STRING, "completedCount": _INTEGER,
    "currentActivation": {"oneOf": [{"type": "null"}, _ASSIGNMENT]},
    "predecessors": {"type": "array", "items": _PREDECESSOR}, "active": _ATTRIBUTION, "closed": _ATTRIBUTION,
    "requires": {"type": ["object", "null"]}, "blockedOn": _BLOCKED,
    "deadlineMs": _INTEGER, "guardedByNodeId": _STRING, "actor": _ACTOR,
}, ["ok", "schemaVersion", "type", "graphId", "nodeId", "version", "journalId", "cursor", "owner", "kind",
    "briefRef", "flag", "setBy", "setAt", "reasonRef", "completedCount", "currentActivation", "predecessors",
    "active", "closed", "requires", "blockedOn"])
_RESYNC = _object({"resync": {"const": True}}, ["resync"])
SPEC = MethodSpec(
    name="workflow.node.context", owner="daemon.workflow", params_schema=_object({"runId": _STRING, "target": _STRING}, ["runId", "target"]),
    result_schema=_RESULT, errors=(MethodError(ipc_errors.INVALID_ARGUMENT), MethodError(PAC_GRAPH_NOT_FOUND),
        MethodError(PAC_NODE_NOT_FOUND), MethodError(PAC_EVENTS_RESYNC, _RESYNC),
        MethodError(ipc_errors.DAEMON_RESTORING), MethodError(ipc_errors.DAEMON_ERROR)),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",), http_exposed=False,
    confirmation="none", idempotency=IdempotencySpec(mode="safe", scope="workflow-node-context"),
)


def context(app, params, *, trusted_message_origin=None):
    from hyprial.daemon.impl.pac.views.context import node_context

    try:
        return node_context(app.state_dir / "pac-graph.sqlite3", params["runId"], params["target"],
                            local_identity=(app.owner, app.node_id))
    except PacError as error:
        raise DaemonRequestError(error.code, str(error), error.data) from error


BINDINGS = ((SPEC, context),)
