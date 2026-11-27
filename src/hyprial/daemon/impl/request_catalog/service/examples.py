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


GRANT_ID = "sg.aaaaaaaaaaaaaaaaaaaaaaaaaa"
GRANT_VIEW = {
    "org": "group-acme",
    "grantor": "alice",
    "schemaVersion": 1,
    "grantId": GRANT_ID,
    "service": {
        "deviceId": "device-a",
        "port": 8447,
        "name": "magpie",
        "protocol": "http",
        "note": None,
    },
    "recipient": {"owner": "bob", "deviceId": "device-b"},
    "grantedAtMs": 1700000000000,
    "expiresAtMs": None,
    "revokedAtMs": None,
    "updatedAtMs": None,
    "hasCredential": True,
    "effective": True,
}


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
    MethodExampleSet(
        method="service.grant",
        cases=(
            case(
                "success",
                "success",
                {
                    "org": "group-acme",
                    "port": 8447,
                    "device": "bob/device-b",
                    "name": "magpie",
                    "protocol": "http",
                    "credential": {"entryId": "magpie", "fieldName": "API_KEY"},
                },
                result={"grant": GRANT_VIEW, "updated": False},
            ),
            case(
                "invalid-params",
                "invalid-params",
                {"org": "group-acme", "port": 8447, "device": "@members", "extra": True},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT,
                    "service.grant params do not match the declared schema at <root>",
                ),
            ),
            case(
                "domain-error",
                "domain-error",
                {"org": "group-acme", "port": 9999, "device": "bob/device-b"},
                error=MethodExampleError(
                    ipc_errors.SERVICE_NOT_REGISTERED,
                    "port 9999 is not currently exposed on this device",
                ),
            ),
        ),
        empty_not_applicable="A service grant always returns the created or updated grant view.",
    ),
    MethodExampleSet(
        method="service.revoke",
        cases=(
            case(
                "success",
                "success",
                {"org": "group-acme", "grantId": GRANT_ID},
                result={"grantId": GRANT_ID, "revoked": True},
            ),
            case(
                "empty",
                "empty",
                {"org": "group-acme", "grantId": GRANT_ID},
                result={"grantId": GRANT_ID, "revoked": False},
                reason="Grant was already revoked or absent under caller.",
            ),
            case(
                "invalid-params",
                "invalid-params",
                {"org": "group-acme"},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT,
                    "service.revoke params do not match the declared schema at <root>",
                ),
            ),
            case(
                "domain-error",
                "domain-error",
                {"org": "group-acme", "grantId": GRANT_ID},
                error=MethodExampleError(
                    ipc_errors.SERVICE_NOT_AUTHORIZED,
                    "cannot revoke a service grant owned by another member",
                ),
            ),
        ),
    ),
    MethodExampleSet(
        method="service.grants",
        cases=(
            case(
                "success",
                "success",
                {"org": "group-acme", "port": 8447},
                result={
                    "granted": [GRANT_VIEW],
                    "received": [],
                    "documentCount": {"group-acme": 1},
                    "documentLimit": 256,
                    "limitedGrantors": [],
                    "unreadableOrgs": [],
                },
            ),
            case(
                "empty",
                "empty",
                {"org": "group-acme"},
                result={
                    "granted": [],
                    "received": [],
                    "documentCount": {"group-acme": 0},
                    "documentLimit": 256,
                    "limitedGrantors": [],
                    "unreadableOrgs": [],
                },
                reason="No effective service grants exist for caller.",
            ),
            case(
                "invalid-params",
                "invalid-params",
                {"org": "group-acme", "unknown": True},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT,
                    "service.grants params do not match the declared schema at <root>",
                ),
            ),
            case(
                "domain-error",
                "domain-error",
                {"org": "group-missing"},
                error=MethodExampleError(
                    ipc_errors.SERVICE_NOT_REGISTERED,
                    "org directory space is not available",
                ),
            ),
        ),
    ),
)
