"""Contract examples do not assert live platform or message delivery."""

from hashlib import sha256
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors
from . import ADD, CANDIDATES, LIST, REMOVE, SEND

_DIGEST = sha256(b"example-not-a-credential").hexdigest()
_ADAPTER = {"name": "lark-demo", "provider": "lark", "appId": "cli_demo", "routes": [], "defaultRoute": None, "revision": _DIGEST}
_CASES = (
    (LIST, {"name": "lark-demo"}, {"ok": True, "adapter": _ADAPTER}),
    (CANDIDATES, {"name": "lark-demo"}, {"ok": True, "adapter": "lark-demo", "candidates": []}),
    (ADD, {"name": "lark-demo", "route": "main", "chatId": "oc_demo", "makeDefault": False, "expectedRevision": _DIGEST},
        {"ok": True, "adapter": {**_ADAPTER, "routes": [{"name": "main", "type": "direct", "nativeId": "oc_demo"}]},
         "configured": True, "reloaded": True, "nextAction": "none"}),
    (REMOVE, {"name": "lark-demo", "route": "main", "clearDefault": False, "expectedRevision": _DIGEST},
        {"ok": True, "adapter": _ADAPTER, "configured": True, "reloaded": False, "nextAction": "reload"}),
)
EXAMPLES = tuple(MethodExampleSet(method=spec.name, cases=(
    MethodExample(name="result", kind="success", description="configured state, not delivery evidence", params=params, result=result),
    MethodExample(name="empty-input", kind="invalid-params", description="name is required", params={},
                  error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required")),
    MethodExample(name="missing-adapter", kind="domain-error", description="configured adapter required", params=params,
                  error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter is not configured")),
), empty_not_applicable="empty route/candidate lists are valid successful reads; writes have explicit receipts") for spec, params, result in _CASES) + (
    MethodExampleSet(method=SEND.name, cases=(
        MethodExample(name="posted", kind="success", description="the adapter's answer, not proof a person read it",
                      params={"route": "main", "message": "hello", "sender": "agent:demo:node:worker"},
                      result={"ok": True, "adapter": "lark-demo", "route": "main",
                              "posts": [{"chatId": "oc_demo", "nativeMessageId": "om_demo"}]}),
        MethodExample(name="empty-input", kind="invalid-params", description="route, message and sender are required",
                      params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "route is required")),
        MethodExample(name="unknown-route", kind="domain-error", description="no configured adapter has the route",
                      params={"route": "missing", "message": "hello", "sender": "agent:demo:node:worker"},
                      error=MethodExampleError("ROUTE_NOT_FOUND", "no configured adapter has this route")),
    ), empty_not_applicable="a post either reaches a chat or is refused; there is no empty success"),
)
