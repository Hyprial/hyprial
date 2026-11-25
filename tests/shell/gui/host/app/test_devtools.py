# Decision record (developer tools on the session host, PR 4b-H, 2026-11-16):
# - Every case has its "off" twin: a host built without the DevTools record
#   answers 404 for the developer routes and says nothing about them in the
#   session body, whatever the request carries.
# - The attach cases compare with the live session a refresh would mint and
#   prove that attach rotates nothing: no Set-Cookie, the same values on a
#   second call, and the opener's own next call still validates.
# - The schema cases use a fake upstream that records what the host sent, so
#   the bearer is checked on the upstream side and its absence on the browser
#   side, against the token of the run.
"""Developer routes: the session capability, session attach and the schema relay."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hyprial.shell.impl.gui.host.web.devtools import ATTACH_PATH, SCHEMA_PATH, DevTools
from tests.shell.gui.host.support import (
    ENDPOINT,
    UPSTREAM_TOKEN,
    FakeUpstream,
    Gateway,
    Opened,
    host_config,
    json_response,
    run_gateway,
)

DOCS = {"swagger-ui-bundle.js": b"window.SwaggerUIBundle=function(){};", "swagger-ui.css": b".swagger-ui{}"}
WITH_DOCS = DevTools(docs=DOCS)
WITHOUT_DOCS = DevTools(docs=None)
SCHEMA = {
    "openapi": "3.1.0",
    "info": {"title": "Hyprial daemon request", "version": "1"},
    "servers": [{"url": "http://127.0.0.1:8791"}],
    "security": [{"HyprialBearer": []}],
    "paths": {
        "/daemon/request": {"get": {"operationId": "catalog", "security": [{"HyprialBearer": []}]}},
        "/daemon/request/agent.list": {"post": {"operationId": "agent.list", "security": [{"HyprialBearer": []}]}},
        "/readyz": {"get": {"operationId": "ready"}},
        "/openapi.json": {"get": {"operationId": "schema"}},
        "/daemon/requests": {"get": {"operationId": "lookalike"}},
    },
    "components": {
        "schemas": {"Agent": {"type": "object"}},
        "securitySchemes": {"HyprialBearer": {"type": "http", "scheme": "bearer"}},
    },
}


def mock_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return json_response(200, {"ok": True})
        if request.url.path == "/mock/contract":
            return json_response(
                200,
                {
                    "mock": True,
                    "contractVersion": "2",
                    "contractDigest": "sha256:mock-contract",
                    "methods": {"agent.list": {"scenarios": ["success"], "mutates": False}},
                },
            )
        return json_response(200, SCHEMA)

    return httpx.MockTransport(handle)


def control_cookie(opened: Opened) -> str:
    return next(part for part in opened.cookies.split("; ") if part.startswith("hyprial_gui_control="))


def session_cookie(opened: Opened) -> str:
    return next(part for part in opened.cookies.split("; ") if part.startswith("hyprial_gui_session="))


def proof(opened: Opened, **overrides: str) -> dict[str, str]:
    headers = {"cookie": opened.cookies, "x-hyprial-view": str(opened.session["view"])}
    headers.update(overrides)
    return {name: value for name, value in headers.items() if value != ""}


async def attach(gateway: Gateway, opened: Opened, **options: Any):
    options.setdefault("headers", proof(opened))
    options.setdefault("body", {})
    return await gateway.call(ATTACH_PATH, method="POST", **options)


def test_a_host_without_developer_tools_says_nothing_about_them() -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        assert "devTools" not in opened.session
        health = await gateway.call("/__gui/healthz")
        assert health.json["devTools"] is False

    run_gateway(scenario)


@pytest.mark.parametrize(
    ("dev_tools", "driver", "expected"),
    [
        (WITH_DOCS, "daemon", {"apiDocs": True}),
        (WITHOUT_DOCS, "daemon", {"apiDocs": False}),
        (WITH_DOCS, "contract-mock", {"apiDocs": False}),
    ],
)
def test_the_session_reports_the_capability_only_with_developer_tools(
    dev_tools: DevTools, driver: str, expected: dict[str, bool]
) -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        assert opened.session["devTools"] == expected
        refreshed = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": control_cookie(opened), "x-hyprial-view": str(opened.session["view"])},
        )
        assert refreshed.status == 200 and refreshed.json["devTools"] == expected
        health = await gateway.call("/__gui/healthz")
        assert health.json["devTools"] is True

    upstream = mock_transport() if driver == "contract-mock" else None
    run_gateway(scenario, config=host_config(driver=driver), upstream=upstream, dev_tools=dev_tools)


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_attach_and_the_schema_are_unknown_paths_without_developer_tools(method: str) -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        for path in (ATTACH_PATH, SCHEMA_PATH):
            body = {} if method == "POST" else None
            reply = await gateway.call(path, method=method, body=body, headers=opened.headers())
            assert (reply.status, reply.json["code"]) == (404, "METHOD_NOT_FOUND"), path
            assert reply.json["dispatch"] == "not-dispatched"

    run_gateway(scenario)


def test_attach_returns_the_live_session_without_rotating_anything() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        probes = len(upstream.captured)
        first = await attach(gateway, opened)
        assert first.status == 200
        assert set(first.json) == {"schemaVersion", "ok", "requestId", "generation", "csrf", "expiresAt"}
        for key in ("generation", "csrf", "expiresAt"):
            assert first.json[key] == opened.session[key], key
        assert first.headers.get_list("set-cookie") == []
        assert first.headers["cache-control"] == "no-store"
        second = await attach(gateway, opened)
        assert {key: second.json[key] for key in ("generation", "csrf", "expiresAt")} == {
            key: first.json[key] for key in ("generation", "csrf", "expiresAt")
        }
        assert len(upstream.captured) == probes
        ready = await gateway.call("/daemon/readyz", headers=opened.headers())
        assert ready.status == 200
        write = await gateway.call(
            "/daemon/request/agent.create", method="POST", body={}, headers=opened.headers()
        )
        assert write.status == 200

    run_gateway(scenario, upstream=upstream, dev_tools=WITH_DOCS)


def test_attach_follows_a_refresh_of_the_opener() -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        refreshed = Opened(
            await gateway.call(
                "/daemon/session",
                method="POST",
                body={},
                headers={"cookie": control_cookie(opened), "x-hyprial-view": str(opened.session["view"])},
            )
        )
        assert refreshed.session["generation"] != opened.session["generation"]
        live = await attach(gateway, refreshed)
        assert live.status == 200
        assert live.json["generation"] == refreshed.session["generation"]
        assert live.json["csrf"] == refreshed.session["csrf"]
        retired = await attach(gateway, opened)
        assert (retired.status, retired.json["code"]) == (401, "SESSION_REQUIRED")

    run_gateway(scenario, dev_tools=WITH_DOCS)


def test_attach_refuses_every_incomplete_or_wrong_proof() -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        wrong_method = await gateway.call(ATTACH_PATH, headers=proof(opened))
        assert (wrong_method.status, wrong_method.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")
        no_origin = await attach(gateway, opened, omit_origin=True)
        assert (no_origin.status, no_origin.json["code"]) == (403, "ORIGIN_REQUIRED")
        foreign = await attach(gateway, opened, origin="http://127.0.0.1:9999")
        assert (foreign.status, foreign.json["code"]) == (403, "ORIGIN_REJECTED")
        with_keys = await attach(gateway, opened, body={"x": 1})
        assert (with_keys.status, with_keys.json["code"]) == (400, "INVALID_REQUEST")
        refusals = {
            "no control cookie": proof(opened, cookie=session_cookie(opened)),
            "no session cookie": proof(opened, cookie=control_cookie(opened)),
            "no cookie": proof(opened, cookie=""),
            "no view": {"cookie": opened.cookies},
            "wrong view": proof(opened, **{"x-hyprial-view": "another-view"}),
            "unknown session": proof(opened, cookie=f"{control_cookie(opened)}; hyprial_gui_session=unknown"),
            "unknown control": proof(opened, cookie=f"hyprial_gui_control=unknown; {session_cookie(opened)}"),
        }
        for name, headers in refusals.items():
            reply = await attach(gateway, opened, headers=headers)
            assert (reply.status, reply.json["code"]) == (401, "SESSION_REQUIRED"), name
            assert "generation" not in reply.json and "csrf" not in reply.json, name
        still = await attach(gateway, opened)
        assert still.status == 200 and still.json["generation"] == opened.session["generation"]

    run_gateway(scenario, dev_tools=WITH_DOCS)


def test_attach_is_refused_after_logout() -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        logout = await gateway.call("/daemon/session/logout", method="POST", body={}, headers=opened.headers())
        assert logout.status == 200
        reply = await attach(gateway, opened)
        assert (reply.status, reply.json["code"]) == (401, "SESSION_REQUIRED")

    run_gateway(scenario, dev_tools=WITH_DOCS)


def test_the_schema_relay_keeps_the_request_paths_and_no_credential_scheme() -> None:
    upstream = FakeUpstream(openapi=SCHEMA)

    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(SCHEMA_PATH, headers=opened.headers(), omit_origin=True)
        assert reply.status == 200
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["content-type"] == "application/json; charset=utf-8"
        document = reply.json
        assert sorted(document["paths"]) == ["/daemon/request", "/daemon/request/agent.list"]
        assert "security" not in document
        assert "servers" in SCHEMA and "servers" not in document
        assert ENDPOINT not in reply.raw
        assert "securitySchemes" not in document["components"]
        assert document["components"]["schemas"] == SCHEMA["components"]["schemas"]
        assert document["openapi"] == "3.1.0" and document["info"] == SCHEMA["info"]
        for item in document["paths"].values():
            for operation in item.values():
                assert "security" not in operation
        assert "HyprialBearer" not in reply.raw and "bearer" not in reply.raw.lower()
        assert UPSTREAM_TOKEN not in reply.raw
        assert UPSTREAM_TOKEN not in json.dumps(dict(reply.headers))
        sent = [request for request in upstream.captured if request.url.path == "/openapi.json"]
        assert len(sent) == 1 and sent[0].method == "GET"
        assert sent[0].headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
        assert "cookie" not in sent[0].headers and "x-hyprial-view" not in sent[0].headers

    run_gateway(scenario, upstream=upstream, dev_tools=WITH_DOCS)


def test_the_schema_relay_needs_a_live_session_and_get() -> None:
    upstream = FakeUpstream(openapi=SCHEMA)

    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        anonymous = await gateway.call(SCHEMA_PATH)
        assert (anonymous.status, anonymous.json["code"]) == (401, "SESSION_REQUIRED")
        stale = await gateway.call(SCHEMA_PATH, headers={**opened.headers(), "x-hyprial-generation": "retired"})
        assert (stale.status, stale.json["code"]) == (403, "GENERATION_REJECTED")
        posted = await gateway.call(SCHEMA_PATH, method="POST", body={}, headers=opened.headers())
        assert (posted.status, posted.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")
        assert [request for request in upstream.captured if request.url.path == "/openapi.json"] == []

    run_gateway(scenario, upstream=upstream, dev_tools=WITH_DOCS)


def test_the_schema_relay_does_not_exist_over_the_contract_mock() -> None:
    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(SCHEMA_PATH, headers=opened.headers())
        assert (reply.status, reply.json["code"]) == (404, "METHOD_NOT_FOUND")

    run_gateway(
        scenario, config=host_config(driver="contract-mock"), upstream=mock_transport(), dev_tools=WITH_DOCS
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, {"code": "DAEMON_RESTORING", "message": "restoring"}, (503, "HTTP_NOT_READY")),
        (401, {"code": "UNAUTHORIZED"}, (502, "UPSTREAM_AUTH_REJECTED")),
        (200, {"openapi": "3.1.0", "paths": []}, (502, "UPSTREAM_INVALID_RESPONSE")),
        (200, {"schemaVersion": 1, "ok": True, "result": {"paths": {}}}, (502, "UPSTREAM_INVALID_RESPONSE")),
    ],
)
def test_the_schema_relay_reports_an_unusable_upstream_answer_as_a_host_failure(
    status: int, body: dict[str, Any], expected: tuple[int, str]
) -> None:
    upstream = FakeUpstream(openapi=body, openapi_status=status)

    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(SCHEMA_PATH, headers=opened.headers())
        assert (reply.status, reply.json["code"]) == expected
        assert reply.json["ok"] is False and UPSTREAM_TOKEN not in reply.raw

    run_gateway(scenario, upstream=upstream, dev_tools=WITH_DOCS)
