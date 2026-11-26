"""Native Auth inventory: one owner-scoped read, separate from Key grants.

Imports and removal retain the existing native Auth wire methods. Import records
are checked against bounded native-file digests; no timestamp or launch success
is inferred from filesystem metadata or Agent online status.
"""

from hyprial.identity import AgentError
from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec, MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_STRING = {"type": "string", "minLength": 1}
_HARNESS = {"enum": ["claude", "codex", "pi", "agy"]}
_FILES = {"type": "array", "items": _STRING}
_ROW = {
    "type": "object",
    "properties": {
        "actor": _STRING, "displayName": _STRING, "harness": _HARNESS,
        "state": {"enum": ["stored", "partial", "unavailable"]},
        "files": _FILES, "missingFiles": _FILES,
        "targets": {"type": "array", "items": {"type": "object", "properties": {
            "nativeAuthRef": _STRING, "target": _STRING, "kind": {"enum": ["oauth", "api-key"]}},
            "required": ["nativeAuthRef", "target", "kind"], "additionalProperties": False}},
        "errorCode": {"type": ["string", "null"]},
        "importedAt": {"type": ["integer", "null"], "minimum": 1,
                       "description": "Registry import time in Unix milliseconds, only when the current Agent incarnation, home resource and bounded native-file digest match. Missing, changed or incomplete files have no proven import time."},
        "startupConfirmedAt": {"type": ["integer", "null"], "minimum": 1,
                               "description": "Latest successful carrier response in Unix milliseconds, bound to this import generation, Agent incarnation, home resource and unchanged native-file set. Requires actual native auth selection; a Key-based launch, process readiness or unrelated reply cannot confirm it. Null means no matching evidence, not authentication failure."},
    },
    "required": ["actor", "displayName", "harness", "state", "files", "missingFiles", "errorCode", "importedAt", "startupConfirmedAt"],
    "additionalProperties": False,
}
SPEC = MethodSpec(
    name="agent.provider.auth.list", owner="daemon.agents",
    params_schema={"type": "object", "properties": {"actor": _STRING, "harness": _HARNESS, "targetsVersion": {"const": 1}}, "additionalProperties": False},
    result_schema={"type": "object", "properties": {"ok": {"const": True}, "records": {"type": "array", "items": _ROW}},
                   "required": ["ok", "records"], "additionalProperties": False},
    errors=tuple(MethodError(code, description="Native Auth inventory refusal.") for code in (
        ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.TARGET_NOT_AUTHORIZED, ipc_errors.AGENT_NOT_FOUND, AgentError.code, ipc_errors.DAEMON_ERROR,
    )),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none", idempotency=IdempotencySpec(mode="safe", scope="native-auth-inventory"),
)


def auth_list(app, params, *, trusted_message_origin=None):
    return app._handle_native_auth_list(dict(params), trusted_message_origin=trusted_message_origin)


BINDINGS = ((SPEC, auth_list),)

EXAMPLES = MethodExampleSet(
    method="agent.provider.auth.list",
    cases=(
        MethodExample(name="empty", kind="empty", description="No native files for owned agents.", params={}, result={"ok": True, "records": []}, reason="No imported files is a legal successful inventory."),
        MethodExample(name="stored", kind="success", description="Files exist; import and startup provenance remain unknown.", params={"harness": "codex"}, result={"ok": True, "records": [{
            "actor": "agent:example:node:worker", "displayName": "worker", "harness": "codex",
            "state": "stored", "files": ["auth.json"], "missingFiles": [], "errorCode": None,
            "importedAt": None, "startupConfirmedAt": None,
        }]}),
        MethodExample(name="unsupported-harness", kind="invalid-params", description="Unknown harness is refused before reading files.", params={"harness": "other"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Unsupported harness.")),
        MethodExample(name="worker-refused", kind="domain-error", description="Trusted worker cannot enumerate operator inventory.", params={}, error=MethodExampleError(ipc_errors.CALLER_NOT_AUTHORIZED, "Local operator required.")),
    ),
)
