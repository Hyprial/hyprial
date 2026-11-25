"""Method contracts owned by the corresponding registered runtime handlers."""
from hyprial.kernel import ipc_errors

from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec

from ..agent import _SECRET_GRANT_ROW

SECRET_PROVIDER_WRITE = "agent.secret.provider-write"

SECRET_GRANT = "agent.secret.grant"

SECRET_REVOKE = "agent.secret.revoke"
SECRET_PROVIDER_LIST = "agent.secret.provider-list"

_STRING = {"type": "string", "minLength": 1}

MODEL_ROUTE = {"type": "object", "properties": {
    "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
    "protocol": {"const": "openai-completions"}, "vendorId": {"const": "deepseek"}},
    "required": ["endpoint", "protocol", "vendorId"], "additionalProperties": False}
MODEL_CONNECTION_INPUT = {"type": "object", "properties": {
    "version": {"const": 1, "type": "integer"},
    "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
    "protocol": {"const": "openai-completions"},
    "profile": {"const": "openai-chat-completions/v1"},
    "preset": {"type": "object", "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"},
        "version": {"type": "integer", "minimum": 1}},
        "required": ["id", "version"], "additionalProperties": False}},
    "required": ["version", "endpoint", "protocol", "profile"], "additionalProperties": False}
MODEL_CONNECTION = {"type": "object", "properties": {
    **MODEL_CONNECTION_INPUT["properties"],
    "connectionRef": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    "configRevision": {"type": "integer", "minimum": 1}},
    "required": [*MODEL_CONNECTION_INPUT["required"], "preset", "connectionRef", "configRevision"],
    "additionalProperties": False}
MODEL_ROUTES = {"type": "object", "propertyNames": {
    "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}, "additionalProperties": MODEL_ROUTE}
MODEL_CONNECTIONS = {**MODEL_ROUTES, "additionalProperties": MODEL_CONNECTION}

_ERRORS = tuple(MethodError(code, description="Existing daemon rejection; no data payload.") for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING, ipc_errors.CALLER_NOT_AUTHORIZED,
    "AGENT_ERROR", ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, ipc_errors.AGENT_NOT_FOUND, ipc_errors.INVALID_AGENT_NAME, ipc_errors.AGENT_HOME_ACCESS_FAILED,
    ipc_errors.AGENT_INCARNATION_MISMATCH, "STALE_AGENT_SECRET_GRANT", ipc_errors.PORT_OVERLOADED, ipc_errors.PORT_CLOSING, ipc_errors.DOMAIN_COMMAND_TIMEOUT,
)) + (MethodError("HTTP_NOT_READY", {
    "type": "object", "properties": {"reason": {"type": "string"}},
    "required": ["reason"], "additionalProperties": False,
}, description="The HTTP readiness gate refused dispatch."),)

_AGENT_WRITE_ERRORS = _ERRORS + (MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload."),)

def _spec(name: str, properties: dict, required: list[str], result: dict,
          errors: tuple[MethodError, ...] = _ERRORS) -> MethodSpec:
    return MethodSpec(
        name=name, owner="daemon.agent.secret",
        params_schema={"type": "object", "properties": properties,
                       "required": required, "additionalProperties": {
                           "type": ["string", "number", "boolean", "null", "array", "object"],
                           "description": "Ignored legacy extension values; not caller identity.",
                       }},
        result_schema=result, errors=errors, mode="unary", mutates=True, retry="never",
        audience=("local-operator",), http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
        ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="never", scope="agent-secret-write"),
    )

SECRET_PROVIDER_WRITE_SPEC = _spec(
    SECRET_PROVIDER_WRITE,
    {"entryId": _STRING, "fieldName": _STRING,
     "value": {"type": "string", "minLength": 1, "not": {"type": "string", "pattern": "[\\r\\n]"}, "writeOnly": True}},
    ["entryId", "fieldName", "value"],
    {"type": "object", "properties": {"ok": {"const": True}, "entryId": _STRING,
     "fieldNames": {"type": "array", "items": _STRING, "minItems": 1}},
     "required": ["ok", "entryId", "fieldNames"], "additionalProperties": False},
)

SECRET_GRANT_SPEC = _spec(
    SECRET_GRANT,
    {"actor": _STRING, "grantId": _STRING,
     "source": {"type": "string", "enum": ["user-provider", "agent-private", "agent-shared"]},
     "entryId": _STRING, "fieldName": _STRING,
     "environmentNames": {"type": "array", "items": _STRING, "minItems": 1},
     "revision": {"type": "integer", "minimum": 1}, "ownerActor": {"type": ["string", "null"]},
     # Legacy IPC ignores this extension. HTTP must not promise an unsupported fence.
     "expectedAgent": {
         "type": ["string", "number", "boolean", "null", "array", "object"],
         "x-hyprial-http-forbidden": True,
         "description": "Unsupported over HTTP; no expected-incarnation fence is implemented by this handler.",
     }},
    ["actor", "grantId", "source", "entryId", "fieldName", "environmentNames", "revision"],
    {"type": "object", "properties": {"ok": {"const": True}, "grant": _SECRET_GRANT_ROW},
     "required": ["ok", "grant"], "additionalProperties": False},
    _AGENT_WRITE_ERRORS,
)

SECRET_REVOKE_SPEC = _spec(
    SECRET_REVOKE,
    {"actor": _STRING, "grantId": _STRING,
     "expectedAgent": SECRET_GRANT_SPEC.params_schema["properties"]["expectedAgent"]},
    ["actor", "grantId"],
    {"type": "object", "properties": {"ok": {"const": True}, "revoked": {"type": "boolean"}},
     "required": ["ok", "revoked"], "additionalProperties": False},
)

SECRET_PROVIDER_LIST_SPEC = MethodSpec(
    name=SECRET_PROVIDER_LIST, owner="daemon.agent.secret",
    params_schema={"type": "object", "properties": {"connectionVersion": {"type": "integer", "const": 1}}, "additionalProperties": False},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "entries": {"type": "array", "maxItems": 1000, "items": {
            "type": "object", "properties": {"entryId": _STRING,
                "fieldNames": {"type": "array", "items": _STRING}, "routes": MODEL_ROUTES, "connections": MODEL_CONNECTIONS},
            "required": ["entryId", "fieldNames"], "additionalProperties": False}}},
        "required": ["ok", "entries"], "additionalProperties": False},
    errors=_ERRORS, mode="unary", mutates=False, retry="safe",
    audience=("local-operator",), http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="agent-secret-catalog"),
)

_EXAMPLE_GRANT = {
    "actor": "squire", "grantId": "example-model", "source": "user-provider",
    "entryId": "example-model", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"],
    "revision": 1, "ownerActor": None, "ownerEntityToken": None,
}

SECRET_WRITE_EXAMPLES = (
    MethodExampleSet(
        method=SECRET_PROVIDER_WRITE,
        empty_not_applicable="A write always acknowledges its non-secret entry and field names.",
        cases=(
            MethodExample(name="saved", kind="success", description="Synthetic input, not a real credential.",
                          params={"entryId": "example-model", "fieldName": "key", "value": "example-not-a-credential"},
                          result={"ok": True, "entryId": "example-model", "fieldNames": ["key"]}),
            MethodExample(name="non-object-params", kind="invalid-params",
                          description="The registry rejects non-object params before legacy admission.",
                          params=[],
                          error=MethodExampleError(
                              ipc_errors.INVALID_ARGUMENT,
                              f"{SECRET_PROVIDER_WRITE} params must be an object",
                          )),
            MethodExample(name="unsafe-entry", kind="domain-error", description="Path traversal is refused by the secret authority.",
                          params={"entryId": "../invalid", "fieldName": "key", "value": "example-not-a-credential"},
                          error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Invalid secret entry.")),
        ),
    ),
    MethodExampleSet(
        method=SECRET_PROVIDER_LIST,
        cases=(
            MethodExample(name="saved", kind="success", description="Saved entries without secret values or grants.",
                          params={}, result={"ok": True, "entries": [{"entryId": "example-model", "fieldNames": ["key"]}]}),
            MethodExample(name="empty", kind="empty", description="No saved entries; no files are created.",
                          params={}, result={"ok": True, "entries": []}, reason="The local catalog has no saved entries."),
            MethodExample(name="unknown-param", kind="invalid-params", description="The catalog accepts no selectors or paths.",
                          params={"path": "outside"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Invalid catalog params.")),
            MethodExample(name="unavailable", kind="domain-error", description="Unsafe storage or a different authority refuses observation.",
                          params={}, error=MethodExampleError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, "Saved secret catalog is unavailable.")),
            MethodExample(name="versioned-connection", kind="success", description="Explicit metadata version; no credential value or model catalog.",
                          params={"connectionVersion": 1}, result={"ok": True, "entries": [{"entryId": "example-custom", "fieldNames": ["key"],
                          "connections": {"key": {"version": 1, "endpoint": "https://model.example/v1", "protocol": "openai-completions",
                          "profile": "openai-chat-completions/v1", "preset": {"id": "custom", "version": 1},
                          "connectionRef": "0123456789abcdef0123456789abcdef", "configRevision": 1}}}]}),
        ),
    ),
    MethodExampleSet(
        method=SECRET_REVOKE,
        empty_not_applicable="Revocation always acknowledges whether an existing grant changed.",
        cases=(
            MethodExample(name="revoked", kind="success", description="Revocation leaves the saved entry intact.",
                          params={"actor": "squire", "grantId": "example-model"}, result={"ok": True, "revoked": True}),
            MethodExample(name="non-object-params", kind="invalid-params", description="Non-object params are refused.",
                          params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Invalid revoke params.")),
            MethodExample(name="authority-unavailable", kind="domain-error", description="The selected authority refuses the operation.",
                          params={"actor": "squire", "grantId": "example-model"},
                          error=MethodExampleError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, "Secret authority is unavailable.")),
        ),
    ),
    MethodExampleSet(
        method=SECRET_GRANT,
        empty_not_applicable="A grant write returns its committed grant receipt.",
        cases=(
            MethodExample(name="granted", kind="success", description="Synthetic grant receipt without a key.",
                          params={key: value for key, value in _EXAMPLE_GRANT.items() if key != "ownerEntityToken"},
                          result={"ok": True, "grant": _EXAMPLE_GRANT}),
            MethodExample(name="non-object-params", kind="invalid-params",
                          description="The registry rejects non-object params before legacy admission.",
                          params=[],
                          error=MethodExampleError(
                              ipc_errors.INVALID_ARGUMENT,
                              f"{SECRET_GRANT} params must be an object",
                          )),
            MethodExample(name="missing-agent", kind="domain-error", description="No authority exists for the selected agent.",
                          params={key: value for key, value in _EXAMPLE_GRANT.items() if key != "ownerEntityToken"},
                          error=MethodExampleError(ipc_errors.AGENT_NOT_FOUND, "Agent not found.")),
        ),
    ),
)
