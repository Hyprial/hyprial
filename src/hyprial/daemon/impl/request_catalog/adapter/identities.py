"""Platform identity requests use the existing adapter store and collector.

Listing is local-only; syncing observes platform facts without issuing owner
bindings. Collection stops scheduling pages after six seconds, with a
two-second SDK request timeout. Partial results remain partial. A transport
timeout is not permission to replay the write.
"""

from typing import Any
from collections.abc import Mapping

from hyprial.kernel import (
    HttpBinding, IdentityBinding, MethodError, MethodExample, MethodExampleError,
    MethodExampleSet, MethodSpec, ipc_errors,
)


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


_STRING = {"type": "string"}
_NAME = {"type": "string", "minLength": 1}
_NULLABLE_STRING = {"type": ["string", "null"]}
_COUNT = {"type": "integer", "minimum": 0}
_NULLABLE_COUNT = {"type": ["integer", "null"], "minimum": 0}
_KIND = {"enum": ["user", "bot", "app"]}
_ROW = _object({
    "kind": _KIND, "platformId": _NAME, "displayName": _NULLABLE_STRING,
    "unionId": _NULLABLE_STRING,
    "standing": {"enum": ["observed", "verified"]}, "source": _STRING,
    "firstSeenMs": _NULLABLE_COUNT, "lastSeenMs": _NULLABLE_COUNT,
})
_LIST = _object({
    "ok": {"const": True}, "adapter": _NAME, "count": _COUNT,
    "identities": {"type": "array", "items": _ROW},
})
_CHAT = _object({
    "chatId": _NAME, "name": _NULLABLE_STRING, "memberTotal": _NULLABLE_COUNT,
    "membersRecorded": _COUNT, "complete": {"type": "boolean"},
})
_SYNC = _object({
    "ok": {"type": "boolean"}, "adapter": _NAME, "appId": _NAME,
    "bot": _object({"openId": _NAME, "appName": _NULLABLE_STRING}),
    "chats": {"type": "array", "items": _CHAT},
    "chatsComplete": {"type": "boolean"}, "identitiesRecorded": _COUNT,
    "errors": {"type": "array", "items": _STRING},
})
_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED,
    ipc_errors.ADAPTER_NOT_FOUND, ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR,
))
_HTTP = HttpBinding(IdentityBinding("none"), readiness="lark-service")

ADAPTER_IDENTITIES_LIST = MethodSpec(
    name="adapter.identities.list", owner="daemon.adapters",
    params_schema=_object({"name": _NAME, "kind": _KIND}, ["name"]),
    result_schema=_LIST, errors=_ERRORS, mode="unary", mutates=False,
    retry="safe", audience=("local-operator",), http_exposed=True,
    http_binding=_HTTP, confirmation="none",
)
ADAPTER_IDENTITIES_SYNC = MethodSpec(
    name="adapter.identities.sync", owner="daemon.adapters",
    params_schema=_object({"name": _NAME}), result_schema=_SYNC,
    errors=_ERRORS, mode="unary", mutates=True, retry="never",
    audience=("local-operator",), http_exposed=True, http_binding=_HTTP,
    confirmation="none",
)


def adapter_identities_list(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
    return app._ipc_adapter_identities_list(dict(params), trusted_message_origin=trusted_message_origin)


def adapter_identities_sync(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
    return app._ipc_adapter_identities_sync(dict(params), trusted_message_origin=trusted_message_origin)


ADAPTER_IDENTITY_BINDINGS = (
    (ADAPTER_IDENTITIES_LIST, adapter_identities_list),
    (ADAPTER_IDENTITIES_SYNC, adapter_identities_sync),
)

_ROW_EXAMPLE = {
    "kind": "user", "platformId": "ou_demo", "displayName": "Example User",
    "unionId": None, "standing": "observed",
    "source": "chat-members:oc_demo", "firstSeenMs": 100, "lastSeenMs": 100,
}
_REPORT = {
    "ok": True, "adapter": "lark-main", "appId": "cli_demo",
    "bot": {"openId": "ou_bot", "appName": "Example App"},
    "chats": [], "chatsComplete": True, "identitiesRecorded": 2, "errors": [],
}
_INVALID = MethodExample(
    name="missing-name", kind="invalid-params", description="the adapter name is required",
    params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required"),
)
_MISSING = MethodExample(
    name="missing-adapter", kind="domain-error", description="no configured adapter has this name",
    params={"name": "missing"}, error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter not configured"),
)
ADAPTER_IDENTITY_EXAMPLES = (
    MethodExampleSet(method="adapter.identities.list", cases=(
        MethodExample(name="observed-person", kind="success", description="observations do not imply a Hyprial account",
                      params={"name": "lark-main"}, result={"ok": True, "adapter": "lark-main", "count": 1,
                                                          "identities": [_ROW_EXAMPLE]}),
        MethodExample(name="no-records", kind="empty", description="nothing has been observed yet",
                      params={"name": "lark-main"}, result={"ok": True, "adapter": "lark-main", "count": 0,
                                                          "identities": []}, reason="a configured adapter can have no observations"),
        _INVALID, _MISSING,
    )),
    MethodExampleSet(method="adapter.identities.sync", cases=(
        MethodExample(name="bot-only", kind="success", description="the bot belongs to no groups",
                      params={"name": "lark-main"}, result=_REPORT),
        MethodExample(name="unnamed-platform-records", kind="success", description="platform names can be absent",
                      params={"name": "lark-main"}, result={**_REPORT,
                          "bot": {"openId": "ou_bot", "appName": None},
                          "chats": [{"chatId": "oc_demo", "name": None, "memberTotal": 0,
                                     "membersRecorded": 0, "complete": True}]}),
        MethodExample(name="partial-budget", kind="success", description="budget exhaustion is an incomplete report",
                      params={"name": "lark-main"}, result={**_REPORT, "ok": False, "chatsComplete": False,
                          "errors": ["chats: enumeration-incomplete (budget-exhausted)"]}),
        _INVALID, _MISSING,
    ), empty_not_applicable="successful collection always records the app and bot"),
)
