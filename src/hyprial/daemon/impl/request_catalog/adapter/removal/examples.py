"""Synthetic confirmation examples do not contain platform credentials."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

PREVIEW = {"ok": True, "name": "lark-main", "provider": "lark",
           "operationId": "example-platform-removal", "expiresAt": 4102444800000,
           "summary": "Remove this local platform integration; keep the remote app and Agent.",
           "boundAgent": None, "processRunning": False, "routes": [], "credentialRemoved": True}
RESULT = {"ok": True, "name": "lark-main", "provider": "lark", "removed": True,
          "unboundAgent": None, "credentialRemoved": True}
EXAMPLES = (
    MethodExampleSet(method="management.adapter.remove.preview", empty_not_applicable="An existing target is required.", cases=(
        MethodExample(name="ready", kind="success", description="Review the local effects.", params={"name": "lark-main"}, result=PREVIEW),
        MethodExample(name="missing-name", kind="invalid-params", description="A name is required.", params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Target required.")),
        MethodExample(name="missing", kind="domain-error", description="The target is absent.", params={"name": "missing"}, error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "Adapter missing.")),
    )),
    MethodExampleSet(method="management.adapter.remove", empty_not_applicable="An existing confirmation is required.", cases=(
        MethodExample(name="deleted", kind="success", description="The local configuration and binding were removed.", params={"operationId": PREVIEW["operationId"]}, result=RESULT),
        MethodExample(name="name-refused", kind="invalid-params", description="A target name is not confirmation.", params={"name": "lark-main"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Preview required.")),
        MethodExample(name="changed", kind="domain-error", description="Preview again after a configuration change.", params={"operationId": "changed"}, error=MethodExampleError(ipc_errors.PREVIEW_STALE, "Preview changed.")),
    )),
)
