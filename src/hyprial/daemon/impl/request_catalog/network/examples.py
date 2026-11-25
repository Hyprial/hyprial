"""Examples describe existing IPC behavior, not external network availability."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_EXPOSURE = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}


def _examples(method, params, result, *, empty=None, semantic=None, domain_code="FORWARDING_UNAVAILABLE"):
    cases = [
        MethodExample(name="result", kind="success", description="Existing forwarding operation result.",
                      params=params, result=result),
        MethodExample(name="non-object", kind="invalid-params", description="IPC params must be an object.",
                      params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
        MethodExample(name="unavailable", kind="domain-error", description="Forwarding state is unavailable.",
                      params=params, error=MethodExampleError(domain_code,
                          "desired network exposures have an unsupported format"
                          if method == "network.exposures" else "forwarding sidecar is not configured")),
    ]
    if empty is not None:
        cases.append(MethodExample(name="empty", kind="empty", description="No desired or rejected exposures.",
                                   params={}, result=empty, reason="The exposure store can be empty."))
    if semantic is not None:
        cases.append(MethodExample(name="missing-field", kind="semantic-param-error", description="Handler validates required fields.",
                                   params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, semantic)))
    return MethodExampleSet(method=method, cases=tuple(cases),
        empty_not_applicable="This operation returns a receipt or lookup result." if empty is None else "")


NETWORK_EXAMPLES = (
    _examples("network.expose", _EXPOSURE, {"exposure": _EXPOSURE}, semantic="port must be between 1 and 65535"),
    _examples("network.unexpose", {"port": 8443}, {"port": 8443, "removed": True}, semantic="port must be between 1 and 65535"),
    _examples("network.exposures", {}, {"exposures": [_EXPOSURE], "rejected": []},
              empty={"exposures": [], "rejected": []}, domain_code="FORWARD_PROTOCOL"),
    _examples("network.peerKey", {"addr": "192.0.2.8:54321"}, {"addr": "192.0.2.8:54321", "found": False},
              semantic="addr must be an IP:port string"),
)
