"""Synthetic examples for service-connect request declarations."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors


ENTRY = {"name": "subapi", "deviceId": "host", "remotePort": 8080, "protocol": "http",
         "localPort": 18080, "usage": None, "env": {}, "auth": "API key", "guide": "Use the local port."}
ROW = {"name": "subapi", "deviceId": "host", "remotePort": 8080,
       "recordGeneration": 1, "localPort": 18080, "activeConnections": 0,
       "path": "unknown", "pathDetail": None, "observedAtMs": None,
       "lastDialState": "unknown", "lastDialMs": None, "lastError": None,
       "lastErrorAtMs": None, "state": "listening", "errorCode": None}
REGISTRY = {"source": "discovery", "spaceId": None, "owner": None, "error": None}


def case(name, kind, params, result=None, error=None, reason=""):
    return MethodExample(name=name, kind=kind, description=name.replace("-", " "),
                         params=params, result=result, error=error, reason=reason)


def examples(method, result, *, empty=None, domain=None):
    named = method != "service.list"
    params = {"name": "subapi"} if named else {}
    invalid = {} if named else {"name": "subapi"}
    cases = [case("success", "success", params, result=result),
             case("invalid-params", "invalid-params", invalid,
                  error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "service request is invalid"))]
    if empty is not None:
        cases.append(case("empty", "empty", params, result=empty,
                          reason="No matching mapping exists."))
    if domain is not None:
        name, code, message = domain
        cases.append(case("domain-error", "domain-error", {"name": name},
                          error=MethodExampleError(code, message)))
    return MethodExampleSet(method=method, cases=tuple(cases),
                            empty_not_applicable="A service is required for this result." if empty is None else "",
                            domain_errors_not_applicable="Catalog failures are represented in registry.error." if domain is None else "")


SERVICE_EXAMPLES = (
    examples("service.connect", {"service": ROW, "env": {}, "auth": "API key", "guide": "Use the local port."},
             domain=("missing", ipc_errors.SERVICE_NOT_REGISTERED, "service is not registered")),
    examples("service.disconnect", {"name": "subapi", "removed": True},
             empty={"name": "subapi", "removed": False},
             domain=("subapi", ipc_errors.SERVICE_UNAVAILABLE, "service teardown failed")),
    examples("service.guide", {"entry": ENTRY, "source": "discovery"},
             domain=("missing", ipc_errors.SERVICE_NOT_REGISTERED, "service is not registered")),
    examples("service.list", {"services": [ROW], "registry": REGISTRY},
             empty={"services": [], "registry": REGISTRY}),
)
