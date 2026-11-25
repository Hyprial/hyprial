"""Selector binding, injection and the 400-before-403 order."""

from __future__ import annotations

from dataclasses import replace
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.catalog import Route, build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from pathlib import Path

OWNER = "h01-owner"
OWNER_USER = "user:h01-owner"
ACTOR = OWNER_USER
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
        if method == "progress.list":
            return {"ok": True, "events": [], "daemonEpoch": "e-1"}
        if method == "conversations.list":
            return {
                "ok": True,
                "participant": params.get("from", ACTOR),
                "conversations": [],
                "nextCursor": None,
                "recordAvailable": True,
            }
        if method == "conversations.history":
            return {
                "ok": True,
                "conversationId": params["conversationId"],
                "mode": "tail",
                "entries": [],
                "headCursor": None,
                "nextCursor": None,
                "nextOlderCursor": None,
                "legacyConversationId": None,
                "recordAvailable": True,
            }
        return {"ok": True}

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


def test_progress_read_injects_owner_and_preserves_the_selected_agent() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    selected = "agent:h01-owner:h01-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"
    envelope = server._invoke(
        "progress.list", "http-1", time.monotonic(), {"from": selected}, None
    )
    assert envelope.status == 200
    assert envelope.payload["dispatch"] == "dispatched"
    assert calls[0]["params"] == {"from": selected, "actor": OWNER_USER}


def test_orgfs_export_injects_owner_via_local_owner_selector() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)

    def export_call(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return {
            "space_id": "space-1",
            "node_id": "node-1",
            "kind": "doc",
            "name": "out",
            "path": "/out",
            "size": 0,
            "blob_hash": None,
            "doc_id": None,
            "version": "1",
            "modified_by": OWNER_USER,
            "modified_via": None,
            "name_conflict": False,
            "deleted": False,
            "contentState": "arrived",
            "uri": "orgfs://space-1/out",
        }

    server._binding = replace(server._binding, request_once=export_call)
    envelope = server._invoke(
        "orgfs.export",
        "http-write-selected",
        time.monotonic(),
        {"spaceId": "space-1", "destination": "/tmp/out"},
        None,
    )
    assert envelope.status == 200
    assert calls[0]["params"] == {
        "from": OWNER_USER,
        "spaceId": "space-1",
        "destination": "/tmp/out",
    }


def test_a_browser_supplied_owner_is_rejected_without_calling_the_daemon() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke(
        "progress.list",
        "http-1",
        time.monotonic(),
        {"actor": "user:someone-else", "from": "agent:h01-owner:h01-node:someone-else"},
        None,
    )
    assert envelope.status == 403
    assert envelope.payload["code"] == "PRINCIPAL_MISMATCH"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def test_non_string_selector_is_400_before_the_mismatch_check() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke("progress.list", "http-1", time.monotonic(), {"from": 7}, None)
    assert envelope.status == 400
    assert envelope.payload["code"] == "INVALID_ARGUMENT"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def test_null_business_target_is_accepted_when_schema_nullable() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke("progress.list", "http-null", time.monotonic(), {"from": None}, None)
    assert envelope.status == 200
    assert calls[0]["params"] == {"from": None, "actor": OWNER_USER}


def test_lookup_uses_bound_actor_when_from_is_forged() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)

    def lookup(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return {"status": "unknown", "reason": "not-found"}

    server._binding = replace(server._binding, request_once=lookup)
    params = {"from": "agent:h01-owner:h01-node:forged", "target": "agent:h01-owner:h01-node:worker",
              "text": "hello", "conversationId": "conversation-1", "idempotencyKey": "key-1"}
    envelope = server._invoke("message.send.lookup", "http-lookup", time.monotonic(), params, None)
    assert envelope.status == 200
    assert calls == [{"method": "message.send.lookup", "params": {**params, "actor": OWNER_USER}}]

    calls.clear()
    forged_actor = server._invoke("message.send.lookup", "http-forged", time.monotonic(),
                                  {**params, "actor": "agent:h01-owner:h01-node:forged"}, None)
    assert forged_actor.status == 403
    assert forged_actor.payload["code"] == "PRINCIPAL_MISMATCH"
    assert calls == []


def test_owner_user_selector_is_injected_and_rejects_browser_claims() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    route = replace(server._view.routes["progress.list"], http_binding={
        "identity": {"mode": "local-owner-user-selector", "param": "actor"}, "readiness": None,
    }, params_schema={
        "type": "object", "properties": {"actor": {"type": ["string", "null"]}, "from": {"type": "string"}},
        "additionalProperties": False,
    }, http_forbidden=frozenset())
    server._view = replace(server._view, routes={**server._view.routes, "progress.list": route})
    params = {"from": "agent:h01-owner:h01-node:forged"}
    injected = server._invoke("progress.list", "http-user", time.monotonic(), params, None)
    assert injected.status == 200
    assert calls[0]["params"] == {**params, "actor": OWNER_USER}
    for actor in ("agent:h01-owner:h01-node:forged", None):
        calls.clear()
        refused = server._invoke("progress.list", "http-user-forged", time.monotonic(), {**params, "actor": actor}, None)
        assert refused.status == 403
        assert refused.payload["dispatch"] == "not-dispatched"
        assert calls == []


def test_extra_identity_and_session_fields_are_rejected() -> None:
    for field in ("onBehalfOf", "sessionRef"):
        calls: list[dict[str, Any]] = []
        server = _server(calls)
        envelope = server._invoke(
            "progress.list",
            "http-1",
            time.monotonic(),
            {"from": ACTOR, field: "x"},
            None,
        )
        assert envelope.status == 400
        assert calls == []


@pytest.mark.parametrize("name, params", (
    ("conversations.list", {}),
    ("conversations.history", {"conversationId": "c.abcdefghijklmnopqrstuvwxyz"}),
))
@pytest.mark.parametrize("field", ("onBehalfOf", "sessionRef"))
def test_conversation_reads_refuse_browser_session_identity(name, params, field) -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    route = server._view.routes[name]
    assert "from" not in route.params_schema.get("properties", {})
    assert "from" not in route.params_schema.get("required", [])
    assert {"onBehalfOf", "sessionRef"} <= set(route.http_forbidden)
    assert field not in route.params_schema.get("properties", {})
    refused = server._invoke(name, "http-conversation", time.monotonic(), {**params, field: "x"}, None)
    assert refused.status == 400
    assert "daemon session identity is not accepted over HTTP" in refused.payload["message"]
    assert calls == []
    calls.clear()
    server._invoke(name, "http-conversation-ok", time.monotonic(), dict(params), None)
    assert calls == [{"method": name, "params": {**params, "from": ACTOR}}]


@pytest.mark.parametrize("name, params", (
    ("conversations.list", {}),
    ("conversations.history", {"conversationId": "c.abcdefghijklmnopqrstuvwxyz"}),
))
@pytest.mark.parametrize("selector", (OWNER_USER, "user:other", "agent:h01-owner:h01-node:operator"))
def test_conversation_http_caller_cannot_be_supplied_by_the_browser(name, params, selector) -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    refused = server._invoke(
        name, "http-conversation-caller", time.monotonic(), {**params, "from": selector}, None,
    )
    assert refused.status == 400
    assert refused.payload["code"] == "INVALID_ARGUMENT"
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
        if method == "progress.list":
            return {"ok": True, "events": [], "daemonEpoch": "e-1"}
        if method == "conversations.list":
            return {
                "ok": True,
                "participant": params.get("from", ACTOR),
                "conversations": [],
                "nextCursor": None,
                "recordAvailable": True,
            }
        if method == "conversations.history":
            return {
                "ok": True,
                "conversationId": params["conversationId"],
                "mode": "tail",
                "entries": [],
                "headCursor": None,
                "nextCursor": None,
                "nextOlderCursor": None,
                "legacyConversationId": None,
                "recordAvailable": True,
            }
        return {"ok": True}

    server._binding = replace(server._binding, request_once=request_once)
    envelope = server._invoke("probe.read", "http-1", time.monotonic(), {}, None)
    assert envelope.status == 200
    assert calls[0]["params"] == {}


def test_schema_error_precedes_the_selector_mismatch() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke(
        "progress.list",
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
    server._binding = replace(
        server._binding,
        config=replace(server.config, budgets=replace(server.config.budgets, request_timeout=1.0)),
    )
    envelope = server._invoke(
        "progress.list",
        "http-expired",
        time.monotonic() - 2.0,
        {"from": "agent:h01-owner:h01-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"},
        None,
    )
    assert envelope.status == 408
    assert envelope.payload["code"] == "HTTP_TIMEOUT"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def _write_route() -> Route:
    return Route(
        name="synthetic.write",
        path="/daemon/request/synthetic.write",
        state="available",
        callable=True,
        reason=None,
        http_binding={
            "identity": {"mode": "local-owner-user-selector", "param": "from"},
            "readiness": None,
        },
        mode="unary",
        mutates=True,
        retry="never",
        confirmation="none",
        audience=("local-operator",),
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "from": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["from", "target"],
        },
        result_schema={"type": "object"},
        errors=(
            {"code": "INVALID_ARGUMENT", "description": "", "dataSchema": None},
        ),
    )


def test_write_injects_the_caller_and_preserves_the_business_target() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls, route_override=_write_route())
    envelope = server._invoke(
        "synthetic.write",
        "http-write",
        time.monotonic(),
        {"target": "agent:h01-owner:h01-node:business"},
        None,
    )
    assert envelope.status == 200
    assert envelope.payload["dispatch"] == "dispatched"
    assert calls == [
        {
            "method": "synthetic.write",
            "params": {"from": ACTOR, "target": "agent:h01-owner:h01-node:business"},
        }
    ]


def test_write_foreign_selector_is_refused_before_dispatch() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls, route_override=_write_route())
    envelope = server._invoke(
        "synthetic.write",
        "http-write",
        time.monotonic(),
        {"from": "agent:h01-owner:h01-node:someone-else", "target": "agent:x:y:z"},
        None,
    )
    assert envelope.status == 403
    assert envelope.payload["code"] == "PRINCIPAL_MISMATCH"
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


def test_write_timeout_is_dispatched_unknown_and_never_retried() -> None:
    from hyprial.kernel import ipc_errors

    calls: list[dict[str, Any]] = []
    server = _server(calls, route_override=_write_route())

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        raise ipc_errors.IpcTimeoutError("no answer")

    server._binding = replace(server._binding, request_once=request_once)
    envelope = server._invoke(
        "synthetic.write",
        "http-write",
        time.monotonic(),
        {"target": "agent:h01-owner:h01-node:business"},
        None,
    )
    assert envelope.status == 504
    assert envelope.payload["dispatch"] == "unknown"
    assert len(calls) == 1


def test_write_expired_budget_never_dispatches() -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls, route_override=_write_route())
    server._binding = replace(
        server._binding,
        config=replace(server.config, budgets=replace(server.config.budgets, request_timeout=1.0)),
    )
    envelope = server._invoke(
        "synthetic.write",
        "http-expired",
        time.monotonic() - 2.0,
        {"target": "agent:h01-owner:h01-node:business"},
        None,
    )
    assert envelope.status == 408
    assert envelope.payload["dispatch"] == "not-dispatched"
    assert calls == []


@pytest.mark.parametrize(
    "name,params",
    [
        ("identity.users.list", {"owner": "forged"}),
        ("identity.users.list", {"kind": 3}),
    ],
)
def test_identity_reads_are_strictly_admitted_before_dispatch(name, params) -> None:
    calls: list[dict[str, Any]] = []
    server = _server(calls)
    envelope = server._invoke(name, "http-identity", time.monotonic(), params, None)
    assert (envelope.status, envelope.payload["code"], envelope.payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )
    assert calls == []


def test_identity_reads_dispatch_through_production_registry(tmp_path) -> None:
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    state = tmp_path / "state"
    state.mkdir()
    app = SimpleNamespace(state_dir=state, hyprial_home=tmp_path)
    calls: list[dict[str, Any]] = []
    server = _server(calls)

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append({"method": method, "params": dict(params)})
        return METHOD_REGISTRY.invoke(app, method, params)

    server._binding = replace(server._binding, request_once=request_once)
    envelope = server._invoke(
        "identity.users.list", "http-users", time.monotonic(), {}, None
    )
    assert (envelope.status, envelope.payload["result"]) == (200, {"users": []})
    assert calls == [{"method": "identity.users.list", "params": {}}]
