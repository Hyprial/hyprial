"""Real HTTP and IPC for synthetic preview handlers using the real registry.

The synthetic handler uses a fixed test-only caller, has no resource to delete,
and counts executions. This exercises transport admission and registry replay,
not domain authorization, resource-version locking or product deletion.
"""

from pathlib import Path

from tests.integration.request_http.conftest import DaemonProcess, running_http
from tests.integration.request_http.test_synthetic_http_entry import _SITECUSTOMIZE


PROBE = '''
from dataclasses import replace
from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, PreviewSpec, ipc_errors
from hyprial.daemon.impl.request_catalog.preview import PreviewRegistry

ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.PREVIEW_UNKNOWN,
    ipc_errors.PREVIEW_STALE, ipc_errors.PREVIEW_EXPIRED, ipc_errors.OPERATION_IN_PROGRESS,
))
PREVIEW = MethodSpec(
    name="synthetic.remove.preview", owner="synthetic.probe",
    params_schema={"type":"object","properties":{"target":{"type":"string"}},
                   "required":["target"],"additionalProperties":False},
    result_schema={"type":"object","properties":{
        "operationId":{"type":"string"},"expiresAt":{"type":"integer"},
        "summary":{"type":"string"}},
        "required":["operationId","expiresAt","summary"],"additionalProperties":False},
    errors=ERRORS, confirmation="preview", preview=PreviewSpec(),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")),
)
EXECUTE = MethodSpec(
    name="synthetic.remove", owner="synthetic.probe",
    params_schema={"type":"object","properties":{"operationId":{"type":"string"}},
                   "required":["operationId"],"additionalProperties":False},
    result_schema={"type":"object","properties":{"ok":{"const":True},"count":{"type":"integer"}},
                   "required":["ok","count"],"additionalProperties":False},
    errors=ERRORS, confirmation="execute", preview_of=PREVIEW.name,
    mutates=True, retry="never", http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
)
registry = PreviewRegistry(epoch="synthetic-process")
count = 0

def preview(app, params, **kwargs):
    record = registry.issue(PREVIEW, params, caller="test-operator", summary="test preview")
    return {"operationId": record.operation_id, "expiresAt": record.expires_at_ms, "summary": record.summary}

def execute(app, params, **kwargs):
    global count
    record = registry.claim(EXECUTE, params, caller="test-operator")
    if record.completed:
        return record.result
    count += 1
    result = {"ok":True,"count":count}
    registry.complete(record.operation_id, result)
    return result

SYNTHETIC_BINDINGS = ((PREVIEW, preview), (EXECUTE, execute))
'''


def test_preview_execute_reaches_daemon_once_and_rejects_replacement_fields(isolation, tmp_path: Path):
    probe = tmp_path / "registration"
    probe.mkdir()
    (probe / "synthetic_probe.py").write_text(PROBE)
    (probe / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    daemon = DaemonProcess(isolation, pythonpath=probe)
    daemon.start()
    try:
        with running_http(isolation, pythonpath=probe) as http:
            status, _, catalog = http.request("GET", "/daemon/request")
            assert status == 200, catalog
            entries = {entry["name"]: entry for entry in catalog["methods"]}
            assert entries["synthetic.remove"]["previewOf"] == "synthetic.remove.preview"
            assert entries["synthetic.remove.preview"]["preview"]["ttlSeconds"] == 300
            status, _, schema = http.request("GET", "/openapi.json")
            assert status == 200, schema
            assert schema["paths"]["/daemon/request/synthetic.remove"]["post"]["x-hyprial-confirmation"] == "execute"
            status, _, payload = http.request(
                "POST", "/daemon/request/synthetic.remove.preview", body={"target": "one"},
            )
            assert status == 200, payload
            operation_id = payload["result"]["operationId"]
            body = {"operationId": operation_id}
            for field in ("target", "actor", "sessionRef"):
                status, _, payload = http.request(
                    "POST", "/daemon/request/synthetic.remove", body={**body, field: "foreign"},
                )
                assert (status, payload["dispatch"]) == (400, "not-dispatched"), payload
            for _ in range(2):
                status, _, payload = http.request("POST", "/daemon/request/synthetic.remove", body=body)
                assert (status, payload["dispatch"]) == (200, "dispatched"), payload
                assert payload["result"] == {"ok": True, "count": 1}
            status, _, payload = http.request(
                "POST", "/daemon/request/synthetic.remove", body={"operationId": "unknown"},
            )
            assert payload["code"] == "PREVIEW_UNKNOWN", (status, payload)
    finally:
        daemon.stop()
