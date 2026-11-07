"""Selector binding, injection and the 400-before-403 order."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.catalog import Route, build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from pathlib import Path

OWNER = "h01-owner"
ACTOR = "agent:h01-owner:h01-node:operator"
PING = {
    "version": __version__,
    "nodeId": "h01-node",
    "owner": OWNER,
    "epoch": "epoch-1",
    "phase": "serving",
}


def _entries() -> list[dict]:
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = "available" if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None
        entries.append(item)
    return entries


def _server(calls: list[dict[str, Any]], *, route_override: Route | None = None) -> RequestHttpServer:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner=OWNER,
        actor=ACTOR,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"),
        source=Path("/tmp/config.json"),
    )

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return {"ok": True, "actor": params["from"], "links": []}

    server = RequestHttpServer(config, request_once=request_once)
    view = build_view(
        PING,
        {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": __version__,
            "methods": _entries(),
        },
        socket_path="/tmp/daemon.sock",
        local_entries=local_catalog()["methods"],
    )
    routes = dict(view.routes)
    if route_override is not None:
        routes[route_override.name] = route_override
    server._view = replace(view, routes=routes)
    server._startup_declaration = server._view.declaration_digest
    server._startup_http = server._view.http_contract_digest
    server._snapshot_at = time.monotonic()
    return server


def test_missing_selector_is_injected_from_the_configured_actor() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke("link.list", "http-1", time.monotonic(), {}, None)
    assert envelope.status == 200
    assert envelope.payload["dispatch"] == "dispatched"
    assert calls[0]["params"] == {"from": ACTOR}


def test_a_foreign_canonical_selector_is_403_without_calling_the_daemon() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke(
        "link.list",
        "http-1",
        time.monotonic(),
        {"from": "agent:h01-owner:h01-node:someone-else"},
        None,
    )
    assert envelope.status == 403
    assert envelope.payload["code"] == "PRINCIPAL_MISMATCH"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert envelope.payload["data"] == {"field": "from"}
    assert calls == []


def test_non_string_selector_is_400_before_the_mismatch_check() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke("link.list", "http-1", time.monotonic(), {"from": 7}, None)
    assert envelope.status == 400
    assert envelope.payload["code"] == "INVALID_ARGUMENT"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def test_extra_identity_and_session_fields_are_rejected() -> None:
    for field in ("onBehalfOf", "sessionRef"):
        calls: list[dict[str, Any]] = []
        server = _server(calls)
        envelope = server._invoke(
            "link.list",
            "http-1",
            time.monotonic(),
            {"from": ACTOR, field: "x"},
            None,
        )
        assert envelope.status == 400
        assert calls == []


def test_identity_mode_none_injects_nothing() -> None:
    calls: list[dict[str, Any]] = []
    route = Route(
        name="probe.read",
        path="/daemon/request/probe.read",
        state="available",
        callable=True,
        reason=None,
        http_binding={"identity": {"mode": "none", "param": None}, "readiness": None},
        mode="unary",
        mutates=False,
        retry="safe",
        confirmation="none",
        audience=("local-operator",),
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_schema={
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        errors=(),
    )
    server = _server(calls, route_override=route)

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return {"ok": True}

    server.request_once = request_once
    envelope = server._invoke("probe.read", "http-1", time.monotonic(), {}, None)
    assert envelope.status == 200
    assert calls[0]["params"] == {}


def test_schema_error_precedes_the_selector_mismatch() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke(
        "link.list",
        "http-1",
        time.monotonic(),
        {"from": "agent:h01-owner:h01-node:someone-else", "junk": 1},
        None,
    )
    assert envelope.status == 400
    assert envelope.payload["code"] == "INVALID_ARGUMENT"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def test_expired_request_budget_never_dispatches() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    server.config = replace(
        server.config,
        budgets=replace(server.config.budgets, request_timeout=1.0),
    )
    envelope = server._invoke(
        "link.list",
        "http-expired",
        time.monotonic() - 2.0,
        {},
        None,
    )
    assert envelope.status == 408
    assert envelope.payload["code"] == "HTTP_TIMEOUT"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []
