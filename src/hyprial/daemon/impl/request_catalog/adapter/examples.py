"""Synthetic adapter cases exported with the declared request methods."""

from __future__ import annotations

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_ADAPTER = {
    "id": "adapter-example-1",
    "provider": "lark",
    "name": "lark-example",
    "status": "running",
    "online": True,
    "configured": True,
    "desired": True,
    "processRunning": True,
    "agent": None,
}

ADAPTER_EXAMPLES = (
    MethodExampleSet(
        method="adapter.list",
        cases=(
            MethodExample(
                name="one-online-adapter", kind="success",
                description="one configured adapter is online",
                params={}, result={"ok": True, "adapters": [_ADAPTER]},
            ),
            MethodExample(
                name="no-adapters", kind="empty",
                description="no adapter is configured",
                params={}, result={"ok": True, "adapters": []},
                reason="an empty local adapter registry is valid",
            ),
            MethodExample(
                name="unexpected-actor", kind="invalid-params",
                description="browser input cannot supply a caller identity",
                params={"actor": "forged"},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "adapter.list takes no parameters"),
            ),
            MethodExample(
                name="adapter-service-not-ready", kind="domain-error",
                description="the adapter service has not started",
                params={}, error=MethodExampleError(ipc_errors.DAEMON_NOT_READY, "adapter service is not ready"),
            ),
        ),
    ),
    MethodExampleSet(
        method="adapter.status",
        cases=(
            MethodExample(
                name="online-adapter", kind="success",
                description="a configured adapter is online",
                params={"name": "lark-example"}, result={"ok": True, "adapter": _ADAPTER},
            ),
            MethodExample(
                name="missing-name", kind="invalid-params",
                description="a status query needs an adapter name",
                params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required"),
            ),
            MethodExample(
                name="adapter-not-found", kind="domain-error",
                description="the named adapter is not configured",
                params={"name": "missing"},
                error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter is not configured: missing"),
            ),
        ),
        empty_not_applicable="a missing name is an ADAPTER_NOT_FOUND error, not an empty adapter projection",
    ),
    MethodExampleSet(
        method="adapter.start",
        cases=(MethodExample(
            name="started", kind="success", description="one configured adapter was started",
            params={"name": "lark-example"}, result={"ok": True, "changed": True, "adapter": _ADAPTER},
        ), MethodExample(
            name="missing-name", kind="invalid-params", description="an adapter name is required",
            params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required"),
        ), MethodExample(
            name="not-configured", kind="domain-error", description="the adapter is not configured",
            params={"name": "missing"}, error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter is not configured"),
        )),
        empty_not_applicable="starting a missing adapter is an ADAPTER_NOT_FOUND error",
    ),
    MethodExampleSet(
        method="adapter.stop",
        cases=(MethodExample(
            name="stopped", kind="success", description="one configured adapter was stopped",
            params={"name": "lark-example"},
            result={"ok": True, "changed": True, "adapter": {**_ADAPTER,
                "status": "stopped", "online": False, "desired": False, "processRunning": False}},
        ), MethodExample(
            name="missing-name", kind="invalid-params", description="an adapter name is required",
            params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required"),
        ), MethodExample(
            name="not-configured", kind="domain-error", description="the adapter is not configured",
            params={"name": "missing"}, error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter is not configured"),
        )),
        empty_not_applicable="stopping a missing adapter is an ADAPTER_NOT_FOUND error",
    ),
    MethodExampleSet(
        method="adapter.reload",
        cases=(MethodExample(
            name="reloaded", kind="success", description="adapter configuration was reloaded",
            params={}, result={"ok": True, "added": ["lark-example"],
                               "updated": [], "removed": [], "removedRunning": []},
        ), MethodExample(
            name="unexpected-actor", kind="invalid-params", description="reload accepts no selectors",
            params={"actor": "forged"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "unexpected actor"),
        ), MethodExample(
            name="service-not-ready", kind="domain-error", description="adapter service is unavailable",
            params={}, error=MethodExampleError(ipc_errors.DAEMON_NOT_READY, "adapter service is not ready"),
        )),
        empty_not_applicable="an empty adapter configuration still returns the four empty reload lists",
    ),
)
