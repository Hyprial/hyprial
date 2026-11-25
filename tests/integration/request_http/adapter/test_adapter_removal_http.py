"""Confirmation crosses the real isolated HTTP and daemon boundary."""

import json

from tests.integration.request_http.adapter.test_adapter_http_live import (
    configured_adapter_http as configured_adapter_http,
)


def test_preview_confirm_remove_and_reread_preserves_other_platforms(configured_adapter_http, isolation):
    http, marker = configured_adapter_http
    status, _, envelope = http.request("POST", "/daemon/request/management.adapter.remove.preview", body={"name": "ready"})
    assert status == 200, envelope
    preview = envelope["result"]
    assert preview["processRunning"] is False
    assert preview["credentialRemoved"] is True
    assert "isolated-secret" not in json.dumps(envelope)
    assert (isolation.home / "secrets/lark-ready.json").exists()
    status, _, invalid = http.request("POST", "/daemon/request/management.adapter.remove", body={"name": "ready"})
    assert status == 400 and invalid["code"] == "INVALID_ARGUMENT"
    status, _, result = http.request("POST", "/daemon/request/management.adapter.remove", body={"operationId": preview["operationId"]})
    assert status == 200, result
    assert result["result"]["removed"] is True
    status, _, rows = http.request("POST", "/daemon/request/adapter.list", body={})
    assert status == 200
    assert {row["name"] for row in rows["result"]["adapters"]} == {"fail", "slow"}
    assert not (isolation.home / "secrets/lark-ready.json").exists()
    assert (isolation.home / "secrets/lark-fail.json").exists()
    assert not marker.exists()
    status, _, replay = http.request("POST", "/daemon/request/management.adapter.remove", body={"operationId": preview["operationId"]})
    assert status == 200 and replay["result"] == result["result"]


def test_http_running_preview_requires_explicit_stop(configured_adapter_http, isolation):
    http, marker = configured_adapter_http
    status, _, started = http.request("POST", "/daemon/request/adapter.start", body={"name": "ready"})
    assert status == 200, started
    status, _, envelope = http.request("POST", "/daemon/request/management.adapter.remove.preview", body={"name": "ready"})
    assert status == 200, envelope
    assert envelope["result"]["processRunning"] is True
    status, _, refused = http.request("POST", "/daemon/request/management.adapter.remove", body={"operationId": envelope["result"]["operationId"]})
    assert refused["code"] in {"OPERATION_IN_PROGRESS", "PREVIEW_STALE"}
    assert (isolation.home / "secrets/lark-ready.json").exists()
    http.request("POST", "/daemon/request/adapter.stop", body={"name": "ready"})


def test_http_different_preview_version_cannot_delete_new_config(configured_adapter_http, isolation):
    http, _ = configured_adapter_http
    status, _, envelope = http.request("POST", "/daemon/request/management.adapter.remove.preview", body={"name": "ready"})
    assert status == 200, envelope
    channels = isolation.home / "channels.json"
    changed = channels.read_text() + "\n"
    channels.write_text(changed)
    status, _, refused = http.request("POST", "/daemon/request/management.adapter.remove", body={"operationId": envelope["result"]["operationId"]})
    assert refused["code"] == "PREVIEW_STALE"
    assert channels.read_text() == changed
    assert (isolation.home / "secrets/lark-ready.json").exists()
