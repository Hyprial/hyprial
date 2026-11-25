"""What crosses to and from the upstream: exact request headers, no cookie memory, dispatch truth."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.shell.gui.host.support import (
    FakeUpstream,
    json_response,
    method_success,
    run_gateway,
)

PROBE_HEADERS = {"accept", "accept-encoding", "authorization", "connection", "host", "user-agent"}
WRITE_HEADERS = PROBE_HEADERS | {
    "content-length",
    "content-type",
    "x-hyprial-catalog-digest",
    "x-hyprial-request-id",
}
DEEP = ('{"a":' * 100_000) + "1" + ("}" * 100_000)


def cookie_setting(upstream: FakeUpstream) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        response = upstream.handle(request)
        response.headers["set-cookie"] = "evil=1; Path=/"
        return response

    return httpx.MockTransport(handle)


def test_upstream_set_cookie_is_never_replayed_and_the_header_set_is_exact() -> None:
    upstream = FakeUpstream()
    transport = cookie_setting(upstream)

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        for _ in range(2):
            reply = await gateway.call(
                "/daemon/request/agent.create", method="POST", body={}, headers=opened.headers()
            )
            assert reply.status == 200 and "set-cookie" not in reply.headers
        await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        refreshed = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": opened.cookies, "x-hyprial-view": str(opened.session["view"])},
        )
        assert refreshed.status == 200

    run_gateway(scenario, upstream=transport)
    assert len(upstream.captured) >= 7
    for request in upstream.captured:
        assert "cookie" not in request.headers
        names = set(request.headers.keys())
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path.startswith("/daemon/request/"):
            assert names == WRITE_HEADERS
        else:
            assert names == PROBE_HEADERS


def test_the_upstream_sees_no_browser_identity_even_when_the_browser_sends_it() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        headers = {
            **opened.headers(),
            "x-hyprial-principal": "user:evil",
            "authorization": "Bearer browser",
            "x-forwarded-for": "10.0.0.9",
            "x-hyprial-request-id": "browser-id-1",
        }
        reply = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=headers)
        assert reply.status == 200

    run_gateway(scenario, upstream=upstream)
    relayed = [r for r in upstream.captured if r.url.path == "/daemon/request/agent.list"][0]
    assert set(relayed.headers.keys()) == WRITE_HEADERS
    assert relayed.headers["x-hyprial-request-id"] == "browser-id-1"
    assert relayed.headers["authorization"].startswith("Bearer upstream-secret")


@pytest.mark.parametrize("name,mutates", [("agent.create", True), ("agent.list", False)])
def test_a_deeply_nested_upstream_envelope_is_an_invalid_response_with_honest_dispatch(
    name: str, mutates: bool
) -> None:
    def method(request: httpx.Request, content: bytes) -> httpx.Response:
        return httpx.Response(200, content=DEEP.encode(), headers={"content-type": "application/json"})

    upstream = FakeUpstream(method=method)

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")
    assert reply.json["dispatch"] == ("unknown" if mutates else "not-dispatched")
    assert reply.json["method"] == name


@pytest.mark.parametrize("bad_id", ["snow☃man", "line\r\nbreak", "café", "tab\there"])
@pytest.mark.parametrize("name,mutates", [("agent.create", True), ("agent.list", False)])
def test_an_unsendable_upstream_request_id_is_an_invalid_response_not_a_broken_reply(
    name: str, mutates: bool, bad_id: str
) -> None:
    def method(request: httpx.Request, content: bytes) -> httpx.Response:
        return json_response(200, method_success(name, bad_id))

    upstream = FakeUpstream(method=method)

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")
    assert reply.json["dispatch"] == ("unknown" if mutates else "not-dispatched")
    assert "x-hyprial-dispatch" not in reply.headers


def test_an_unsendable_catalog_digest_in_an_upstream_body_is_an_invalid_response() -> None:
    def method(request: httpx.Request, content: bytes) -> httpx.Response:
        return json_response(200, {**method_success("agent.list"), "catalogDigest": "sha☃"})

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(scenario, upstream=FakeUpstream(method=method))
    assert (reply.status, reply.json["code"], reply.json["dispatch"]) == (
        502,
        "UPSTREAM_INVALID_RESPONSE",
        "not-dispatched",
    )


@pytest.mark.parametrize("name,mutates", [("agent.create", True), ("agent.list", False)])
def test_a_redirect_on_a_relay_is_a_transport_failure_and_its_body_is_not_passed_on(
    name: str, mutates: bool
) -> None:
    def method(request: httpx.Request, content: bytes) -> httpx.Response:
        return httpx.Response(
            302,
            content=json.dumps(method_success(name)).encode(),
            headers={"location": "http://127.0.0.1:1/elsewhere", "content-type": "application/json"},
        )

    upstream = FakeUpstream(method=method)

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"]) == (502, "UPSTREAM_UNAVAILABLE")
    assert reply.json["dispatch"] == ("unknown" if mutates else "not-dispatched")
    assert "location" not in reply.headers and "x-hyprial-dispatch" not in reply.headers


@pytest.mark.parametrize("name,mutates", [("agent.create", True), ("agent.list", False)])
def test_a_browser_request_id_that_cannot_be_sent_upstream_is_refused_before_dispatch(
    name: str, mutates: bool
) -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        headers = {**opened.headers(), "x-hyprial-request-id": "caf\u00e9".encode("latin-1")}
        return await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=headers)

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"], reply.json["dispatch"]) == (400, "INVALID_REQUEST", "not-dispatched")
    assert reply.json["method"] == name
    assert upstream.posts() == []


def test_a_write_whose_host_side_processing_fails_after_the_send_is_unknown(monkeypatch) -> None:
    from hyprial.shell.impl.gui.host import app as app_module

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        monkeypatch.setattr(app_module, "relay_reply", explode)
        write = await gateway.call("/daemon/request/agent.create", method="POST", body={}, headers=opened.headers())
        read = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        return write, read

    write, read = run_gateway(scenario)
    assert (write.status, write.json["code"], write.json["dispatch"]) == (500, "HOST_INTERNAL_ERROR", "unknown")
    assert (read.status, read.json["dispatch"]) == (500, "not-dispatched")


@pytest.mark.parametrize("status", [600, 999, 99])
@pytest.mark.parametrize("name,mutates", [("agent.create", True), ("agent.list", False)])
def test_an_upstream_status_the_http_stack_cannot_write_is_an_invalid_response_with_honest_dispatch(
    name: str, mutates: bool, status: int
) -> None:
    def method(request: httpx.Request, content: bytes) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(method_success(name)).encode(), headers={"content-type": "application/json"}
        )

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(scenario, upstream=FakeUpstream(method=method))
    assert (reply.status, reply.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")
    assert reply.json["dispatch"] == ("unknown" if mutates else "not-dispatched")
    assert reply.json["method"] == name
