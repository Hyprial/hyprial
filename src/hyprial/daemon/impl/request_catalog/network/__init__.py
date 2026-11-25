"""IPC network methods bound to the existing forwarding operations."""

from hyprial.kernel import MethodError, MethodSpec, ipc_errors

_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}
_EXPOSURE = {
    "type": "object",
    "properties": {
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "target": {"type": "string"},
        "proxyProtocol": {"enum": ["v2", "none"]},
    },
    "required": ["port", "target", "proxyProtocol"],
    "additionalProperties": False,
}
_EXPOSURES = {
    "type": "object",
    "properties": {
        "exposures": {"type": "array", "items": _EXPOSURE},
        "rejected": {"type": "array", "items": {
            "type": "object", "properties": {"exposure": _JSON, "reason": {"type": "string"}},
            "required": ["exposure", "reason"], "additionalProperties": False,
        }},
    },
    "required": ["exposures", "rejected"], "additionalProperties": False,
}
_EXPOSE_RESULT = {"type": "object", "properties": {"exposure": _EXPOSURE},
                  "required": ["exposure"], "additionalProperties": False}
_UNEXPOSE_RESULT = {"type": "object", "properties": {
    "port": {"type": "integer"}, "removed": {"const": True}},
    "required": ["port", "removed"], "additionalProperties": False}
_PEER_RESULT = {"type": "object", "properties": {
    "addr": {"type": "string"}, "found": {"type": "boolean"}, "key": _JSON},
    "required": ["addr", "found"], "additionalProperties": False}


def _spec(name, result, properties, *, mutates):
    return MethodSpec(
        name=name, owner="daemon.forwarding", params_schema={
            "type": "object", "properties": properties, "additionalProperties": _JSON,
        }, result_schema=result,
        errors=tuple(MethodError(code) for code in (
            ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_ERROR,
            *(() if name == "network.expose" else ("FORWARD_PROTOCOL",)),
            *(("FORWARDING_UNAVAILABLE", "SIDECAR_EXPOSE_UNSUPPORTED")
              if name != "network.exposures" else ()),
        )), mode="unary", mutates=mutates, retry="never" if mutates else "safe",
        audience=("local-operator",), http_exposed=False, confirmation="none",
        ipc_param_validation="handler",
    )


EXPOSE = _spec("network.expose", _EXPOSE_RESULT, {
    "port": {"type": "integer"}, "target": {"type": "string"},
    "proxyProtocol": {"type": "string"},
}, mutates=True)
UNEXPOSE = _spec("network.unexpose", _UNEXPOSE_RESULT, {"port": {"type": "integer"}}, mutates=True)
EXPOSURES = _spec("network.exposures", _EXPOSURES, {}, mutates=False)
PEER_KEY = _spec("network.peerKey", _PEER_RESULT, {"addr": {"type": "string"}}, mutates=True)


def expose(app, params, *, trusted_message_origin=None):
    return app._ipc_network_expose(params)


def unexpose(app, params, *, trusted_message_origin=None):
    return app._ipc_network_unexpose(params)


def exposures(app, params, *, trusted_message_origin=None):
    return app._ipc_network_exposures()


def peer_key(app, params, *, trusted_message_origin=None):
    return app._ipc_network_peer_key(params)


NETWORK_BINDINGS = ((EXPOSE, expose), (UNEXPOSE, unexpose), (EXPOSURES, exposures), (PEER_KEY, peer_key))
