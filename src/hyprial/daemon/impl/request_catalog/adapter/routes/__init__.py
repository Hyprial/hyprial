"""Configured outbound routes and observed candidates, with optional write expectations.

``expectedRevision`` on add/remove is optional (F1a, 2026-11-24): when sent, a
stale revision is refused with ``ADAPTER_CONFIG_CONFLICT``; when omitted, the
write still refuses a taken, unbound or default route under the daemon's write
lease, so a one-request CLI write cannot silently retarget a route.

``adapter.routes.send`` (I4b, owner item Q0-d, 2026-11-24) is the local
operator's post to a route named by the adapter's own route name; it replaced
the retired ``message.send --to route:<adapter>:<route>`` for the Lark reauth
notice.  It is not HTTP-exposed and reuses existing codes only.  ``sender``
only names who the post is relayed for (``转述自 <sender>``); it is resolved
like ``message.send``'s ``onBehalfOf`` and must then be an agent registered on
this node, so an unregistered bare name or full ``agent:`` URI is refused with
``SENDER_UNRESOLVED`` before anything is posted.  Each post and each refusal of
the platform is logged as ``adapter.routes.send`` with the adapter, route,
sender and state (and ``larkCode`` on a platform refusal).
"""

from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, ipc_errors

from hyprial.daemon.impl.request_catalog.address_errors import AGENT_ADDRESS_RETIRED_ERROR

_STRING = {"type": "string", "minLength": 1}
_REVISION = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NAMES_STATUS = {"enum": ["complete", "partial", "unavailable"]}
_ROUTE = {"type": "object", "properties": {
    "name": _STRING, "type": {"enum": ["direct", "fanout"]}, "nativeId": _STRING,
    "members": {"type": "array", "items": _STRING}, "nickname": _STRING,
    "chatName": {"type": ["string", "null"]},
}, "required": ["name", "type"], "additionalProperties": False}
_ADAPTER = {"type": "object", "properties": {
    "name": _STRING, "provider": {"const": "lark"}, "appId": _STRING,
    "routes": {"type": "array", "items": _ROUTE}, "defaultRoute": {"type": ["string", "null"]}, "revision": _REVISION,
    "namesStatus": _NAMES_STATUS,
}, "required": ["name", "provider", "appId", "routes", "defaultRoute", "revision"], "additionalProperties": False}
_RESULT = {"type": "object", "properties": {"ok": {"const": True}, "adapter": _ADAPTER},
           "required": ["ok", "adapter"], "additionalProperties": False}
_WRITE_RESULT = {**_RESULT, "properties": {**_RESULT["properties"], "configured": {"const": True},
                 "reloaded": {"type": "boolean"}, "nextAction": {"enum": ["none", "reload"]}},
                 "required": ["ok", "adapter", "configured", "reloaded", "nextAction"]}
_CANDIDATES = {"type": "object", "properties": {"ok": {"const": True}, "adapter": _STRING,
    "namesStatus": _NAMES_STATUS,
    "candidates": {"type": "array", "items": {"type": "object", "properties": {
        "chatId": _STRING, "chatType": {"type": ["string", "null"]}, "chatName": {"type": ["string", "null"]}}, "required": ["chatId", "chatType"], "additionalProperties": False}}},
    "required": ["ok", "adapter", "candidates"], "additionalProperties": False}


def _params(extra=None, optional=()):
    properties = {"name": _STRING, **(extra or {})}
    required = [key for key in properties if key not in optional]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _spec(action, params, result, *, write=False):
    return MethodSpec(
        name="adapter.routes." + action, owner="daemon.adapters", params_schema=params, result_schema=result,
        errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.DAEMON_NOT_READY, ipc_errors.ADAPTER_NOT_FOUND,
            ipc_errors.DAEMON_ERROR, "ADAPTER_CONFIG_CONFLICT", "ROUTE_EXISTS", "ROUTE_NOT_FOUND", "ROUTE_IN_USE", ipc_errors.SUBMIT_OUTCOME_UNKNOWN)),
        mode="unary", mutates=write, retry="never" if write else "safe", audience=("local-operator",),
        http_exposed=True, http_binding=HttpBinding(IdentityBinding("none"), readiness="lark-service"), confirmation="none",
    )


LIST = _spec("list", _params(), _RESULT)
CANDIDATES = _spec("candidates", _params(), _CANDIDATES)
ADD = _spec("add", _params({"route": _STRING, "chatId": _STRING, "makeDefault": {"type": "boolean"}, "expectedRevision": _REVISION}, ("expectedRevision",)), _WRITE_RESULT, write=True)
REMOVE = _spec("remove", _params({"route": _STRING, "clearDefault": {"type": "boolean"}, "expectedRevision": _REVISION}, ("expectedRevision",)), _WRITE_RESULT, write=True)


_POSTS = {"type": "array", "items": {"type": "object", "properties": {
    "chatId": _STRING, "nativeMessageId": _STRING}, "required": ["chatId", "nativeMessageId"], "additionalProperties": False}}
SEND = MethodSpec(
    name="adapter.routes.send", owner="daemon.adapters",
    params_schema={"type": "object", "properties": {"route": _STRING, "message": _STRING, "sender": _STRING},
                   "required": ["route", "message", "sender"], "additionalProperties": False},
    result_schema={"type": "object", "properties": {"ok": {"const": True}, "adapter": _STRING, "route": _STRING, "posts": _POSTS},
                   "required": ["ok", "adapter", "route", "posts"], "additionalProperties": False},
    errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, "ROUTE_NOT_FOUND",
        ipc_errors.USER_DELIVERY_UNAVAILABLE, ipc_errors.USER_DELIVERY_FAILED, ipc_errors.SUBMIT_OUTCOME_UNKNOWN)) + (
        MethodError(ipc_errors.SENDER_UNRESOLVED, {"type": "object", "properties": {"sender": _STRING},
                                                   "additionalProperties": False}),
        AGENT_ADDRESS_RETIRED_ERROR,
    ),
    mode="unary", mutates=True, retry="never", audience=("local-operator",), confirmation="none",
)


def route_list(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_routes(dict(params), action="list", trusted_message_origin=trusted_message_origin)


def candidates(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_routes(dict(params), action="candidates", trusted_message_origin=trusted_message_origin)


def add(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_routes(dict(params), action="add", trusted_message_origin=trusted_message_origin)


def remove(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_routes(dict(params), action="remove", trusted_message_origin=trusted_message_origin)


def send(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_routes_send(dict(params), trusted_message_origin=trusted_message_origin)


BINDINGS = ((LIST, route_list), (CANDIDATES, candidates), (ADD, add), (REMOVE, remove), (SEND, send))
