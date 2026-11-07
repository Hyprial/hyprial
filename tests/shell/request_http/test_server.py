"""Startup handshake, epoch retry, stale contracts and ready/not-ready states."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.parse

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.errors import HttpFailure
from hyprial.shell.impl.request_http.server import RequestHttpServer, StartupRefusal

OWNER = "h01-owner"
ACTOR = f"agent:{OWNER}:h01-node:operator"


def _config(tmp_path: Path) -> RequestHttpConfig:
    return RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner=OWNER,
        actor=ACTOR,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=tmp_path / "runtime",
        source=tmp_path / "config.json",
    )


def _entries(state: str = "available") -> list[dict]:
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = state if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None if item["state"] == "available" else "SERVICE_NOT_READY"
        entries.append(item)
    return entries


def _fake(entries: list[dict], *, ping_epoch: str = "epoch-1", catalog_epoch: str = "epoch-1", phase: str = "serving", version: str = __version__):
    calls = {"count": 0}

    def request_once(method: str, params: dict, **_kwargs):
        calls["count"] += 1
        if method == "ping":
            return {
                "version": version,
                "nodeId": "h01-node",
                "owner": OWNER,
                "epoch": ping_epoch,
                "phase": phase,
                "restorePending": phase == "restoring",
            }
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": catalog_epoch,
            "packageVersion": __version__,
            "methods": entries,
        }

    return request_once, calls


def test_epoch_inconsistency_is_retried_then_refused(tmp_path: Path) -> None:
    entries = _entries()
    request_once, calls = _fake(entries, ping_epoch="a", catalog_epoch="b")
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    with pytest.raises(StartupRefusal, match="do not share one epoch") as failure:
        server.start()
    assert failure.value.code == "CATALOG_MISMATCH"
    assert failure.value.reason == "EPOCH_INCONSISTENT"
    assert calls["count"] == 6
    assert not (tmp_path / "runtime" / "server.json").exists()


def test_version_mismatch_refuses_at_startup(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries(), version="9.9.9")
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    with pytest.raises(StartupRefusal) as failure:
        server.start()
    assert failure.value.code == "VERSION_MISMATCH"


def test_contract_change_after_start_is_stale(tmp_path: Path) -> None:
    entries = _entries()
    request_once, _ = _fake(entries)
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        for entry in entries:
            if entry["name"] == "link.list":
                entry["owner"] = "daemon.changed"
        with pytest.raises(HttpFailure) as failure:
            server.refresh(force=True)
        assert failure.value.code == "CATALOG_STALE"
        assert failure.value.dispatch == "not-dispatched"
    finally:
        server.shutdown()
    assert not (tmp_path / "runtime" / "server.json").exists()


def test_not_ready_route_is_refused_before_the_daemon_call(tmp_path: Path) -> None:
    entries = _entries()
    request_once, calls = _fake(entries)
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        for entry in entries:
            if entry["name"] == "link.list":
                entry["state"] = "not-ready"
                entry["callable"] = False
                entry["reason"] = "SERVICE_NOT_READY"
        server.refresh(force=True)
        before = calls["count"]
        envelope = server._invoke("link.list", "http-1", time.monotonic(), {}, None)
        assert envelope.status == 503
        assert envelope.payload["code"] == "HTTP_NOT_READY"
        assert envelope.payload["dispatch"] == "not-dispatched"
        assert calls["count"] == before
    finally:
        server.shutdown()


def test_restoring_phase_is_not_ready(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries(), phase="restoring")
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        envelope = server._ready("http-1", time.monotonic())
        assert envelope.status == 503
        assert envelope.payload["ready"] is False
        assert envelope.payload["code"] == "DAEMON_RESTORING"
    finally:
        server.shutdown()


def test_server_json_is_written_with_the_startup_epoch(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        document = json.loads((tmp_path / "runtime" / "server.json").read_text(encoding="utf-8"))
        assert document["backend"]["epoch"] == "epoch-1"
        assert document["endpoint"].startswith("http://127.0.0.1:")
    finally:
        server.shutdown()


def _http(server: RequestHttpServer, method: str, path: str, *, body: object = None, token: str | None = None) -> tuple[int, dict[str, str], bytes]:
    endpoint = urllib.parse.urlsplit(server.endpoint)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {server.token}"} if token is not None else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result = (response.status, dict(response.getheaders()), raw)
    connection.close()
    return result


def _raw(server: RequestHttpServer, request: bytes) -> tuple[int, bytes]:
    endpoint = urllib.parse.urlsplit(server.endpoint)
    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=5) as client:
        client.sendall(request)
        data = bytearray()
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    return int(head.split(b"\r\n", 1)[0].split()[1]), body


def test_production_listener_is_uvicorn_with_a_fastapi_app(tmp_path: Path) -> None:
    import fastapi
    import uvicorn

    request_once, _ = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        assert isinstance(server._uvicorn, uvicorn.Server)
        assert isinstance(server._uvicorn.config.app, fastapi.FastAPI)
        assert server._uvicorn.config.timeout_keep_alive == 30
        assert server._thread is not None and server._thread.is_alive()
        status, _, raw = _http(server, "GET", "/healthz")
        assert status == 200 and json.loads(raw) == {"ok": True}
    finally:
        server.shutdown()


def test_http_framework_is_not_imported_by_the_module_entry() -> None:
    code = (
        "import sys, hyprial.shell.impl.request_http; "
        "print(int('fastapi' in sys.modules), int('uvicorn' in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "0 0"


def test_response_budget_refuses_an_oversized_catalog(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    base = _config(tmp_path)
    config = replace(base, budgets=replace(base.budgets, max_response_bytes=1024))
    server = RequestHttpServer(config, request_once=request_once)
    server.start()
    try:
        status, _, raw = _http(server, "GET", "/daemon/request", token=server.token)
        payload = json.loads(raw)
        assert status == 502
        assert payload["code"] == "INVALID_RESPONSE"
        assert payload["dispatch"] == "unknown"
        assert len(raw) <= 1024
    finally:
        server.shutdown()


def test_original_request_target_is_validated_before_routing(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        for target in ("//healthz", "/healthz?x=1", "http://127.0.0.1/healthz"):
            status, raw = _raw(
                server,
                f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode(),
            )
            assert status == 400, target
            assert json.loads(raw)["code"] == "INVALID_REQUEST"
        status, raw = _raw(
            server, b"GET /healthz/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        )
        assert status == 404
        assert json.loads(raw)["code"] == "METHOD_NOT_FOUND"
    finally:
        server.shutdown()


def test_openapi_matches_routes_and_catalog(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        status, _, raw = _http(server, "GET", "/openapi.json", token=server.token)
        assert status == 200
        document = json.loads(raw)
        paths = document["paths"]
        for path in ("/healthz", "/readyz", "/daemon/request", "/daemon/request/link.list", "/daemon/request/link.stats"):
            assert path in paths
        route = server.current_view().routes["link.list"]
        post = paths[route.path]["post"]
        body_schema = post["requestBody"]["content"]["application/json"]["schema"]
        assert body_schema == route.params_schema
        result_schema = post["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["result"]
        assert result_schema == dict(route.result_schema)
        assert "422" not in post["responses"]
        assert "/docs" not in paths

        status, _, raw = _http(server, "GET", "/healthz/")
        assert status == 404
        assert json.loads(raw)["code"] == "METHOD_NOT_FOUND"
        status, _, raw = _http(server, "GET", "/openapi.json")
        assert status == 401
        assert json.loads(raw)["code"] == "HTTP_UNAUTHORIZED"
    finally:
        server.shutdown()


def _actual_routes(server: RequestHttpServer) -> dict[str, object]:
    from fastapi.routing import APIRoute

    app = server._uvicorn.config.app
    return {
        route.path: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    }


def _served_document(server: RequestHttpServer) -> tuple[dict, dict, dict[str, object]]:
    status, _, raw = _http(server, "GET", "/openapi.json", token=server.token)
    assert status == 200
    document = json.loads(raw)
    status, _, raw = _http(server, "GET", "/daemon/request", token=server.token)
    assert status == 200
    return document, json.loads(raw), _actual_routes(server)


def _drift_issues(
    document: dict, catalog: dict, actual: dict[str, object]
) -> list[str]:
    """Cross-check catalog, OpenAPI document and the real FastAPI route table."""

    fixed = {"/healthz", "/readyz", "/daemon/request", "/openapi.json"}
    methods = {item["name"]: item for item in catalog["methods"]}
    expected = {item["path"]: item for item in methods.values()}
    issues: list[str] = []
    if set(document.get("paths", {})) != fixed | set(expected):
        issues.append("OpenAPI path inventory differs from the catalog")
    if set(actual) != fixed | set(expected):
        issues.append("actual FastAPI route inventory differs from the catalog")
    for path, method in expected.items():
        operation = document.get("paths", {}).get(path, {}).get("post")
        if operation is None:
            issues.append(f"{path}: missing POST operation")
            continue
        if operation.get("x-hyprial-method") != method["name"]:
            issues.append(f"{path}: x-hyprial-method drift")
        if operation.get("x-hyprial-state") != method["state"]:
            issues.append(f"{path}: x-hyprial-state drift")
        if operation.get("x-hyprial-http-binding") != method["httpBinding"]:
            issues.append(f"{path}: binding drift")
        if operation.get("x-hyprial-identity") != method["httpBinding"]["identity"]:
            issues.append(f"{path}: identity drift")
        body = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if body != method["paramsSchema"]:
            issues.append(f"{path}: params schema drift")
        result = (
            operation.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("properties", {})
            .get("result")
        )
        if result != method["resultSchema"]:
            issues.append(f"{path}: result schema drift")
        route = actual.get(path)
        if route is None:
            issues.append(f"{path}: missing actual route")
        else:
            if "POST" not in route.methods:
                issues.append(f"{path}: actual route is not POST")
            extra = route.openapi_extra or {}
            if extra.get("x-hyprial-method") != method["name"]:
                issues.append(f"{path}: actual route method metadata drift")
    return issues


def test_openapi_tracks_catalog_state_and_rejects_route_drift(tmp_path: Path) -> None:
    entries = _entries()
    request_once, _ = _fake(entries)
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        document, catalog, actual = _served_document(server)
        assert _drift_issues(document, catalog, actual) == []

        for entry in entries:
            if entry["name"] == "link.list":
                entry["state"] = "not-ready"
                entry["callable"] = False
                entry["reason"] = "SERVICE_NOT_READY"
        server.refresh(force=True)
        document, catalog, actual = _served_document(server)
        assert _drift_issues(document, catalog, actual) == []
        assert (
            document["paths"]["/daemon/request/link.list"]["post"][
                "x-hyprial-state"
            ]
            == "not-ready"
        )

        path = "/daemon/request/link.list"

        def _controls():
            yield "missing OpenAPI path", lambda d, c, a: d["paths"].pop(path)
            yield "extra OpenAPI path", lambda d, c, a: d["paths"].update(
                {"/daemon/request/ghost": {}}
            )
            yield "params schema", lambda d, c, a: d["paths"][path]["post"][
                "requestBody"
            ]["content"]["application/json"].update({"schema": {"type": "object"}})
            yield "result schema", lambda d, c, a: d["paths"][path]["post"][
                "responses"
            ]["200"]["content"]["application/json"]["schema"]["properties"].update(
                {"result": {"type": "string"}}
            )
            yield "catalog state", lambda d, c, a: next(entry for entry in c["methods"] if entry["name"] == "link.list").update(
                {"state": "available"}
            )
            yield "missing actual route", lambda d, c, a: a.pop(path)
            yield "extra actual route", lambda d, c, a: a.update(
                {"/daemon/request/ghost": SimpleNamespace(methods={"POST"})}
            )
            yield "actual route metadata", lambda d, c, a: a.update(
                {
                    path: SimpleNamespace(
                        methods={"POST"},
                        openapi_extra={"x-hyprial-method": "link.stats"},
                    )
                }
            )

        for name, mutate in _controls():
            document_copy = deepcopy(document)
            catalog_copy = deepcopy(catalog)
            actual_copy = dict(actual)
            mutate(document_copy, catalog_copy, actual_copy)
            assert _drift_issues(document_copy, catalog_copy, actual_copy), name
    finally:
        server.shutdown()


def test_partial_headers_expire_before_late_completion(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    base = _config(tmp_path)
    config = replace(base, budgets=replace(base.budgets, header_timeout=1.0))
    server = RequestHttpServer(config, request_once=request_once)
    server.start()
    try:
        endpoint = urllib.parse.urlsplit(server.endpoint)
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=3) as client:
            client.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            time.sleep(1.5)
            client.settimeout(0.5)
            try:
                first = client.recv(4096)
                state = "closed" if first == b"" else "response"
            except (socket.timeout, ConnectionResetError):
                state = "still-open"
            assert state == "closed", "unfinished headers outlived headerTimeoutSeconds"
    finally:
        server.shutdown()


def test_openapi_declares_bearer_for_each_protected_operation(tmp_path: Path) -> None:
    request_once, _ = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    try:
        status, _, raw = _http(server, "GET", "/openapi.json", token=server.token)
        assert status == 200
        document = json.loads(raw)
        schemes = document.get("components", {}).get("securitySchemes", {})
        bearer = {
            name
            for name, scheme in schemes.items()
            if scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
        }
        assert bearer
        for path in (
            "/readyz",
            "/daemon/request",
            "/openapi.json",
            "/daemon/request/link.list",
            "/daemon/request/link.stats",
        ):
            operation = next(iter(document["paths"][path].values()))
            requirement = operation.get("security", document.get("security", []))
            assert any(bearer.intersection(entry) for entry in requirement), path
        assert "security" not in document["paths"]["/healthz"]["get"]
        assert not document.get("security")
    finally:
        server.shutdown()
