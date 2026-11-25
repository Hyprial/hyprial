"""Route behaviour: status, readyz, catalog, method relay and host faults."""

from __future__ import annotations

import json

import httpx
import pytest

from hyprial.shell.impl.gui.host.envelope import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
from tests.shell.gui.host.support import (
    FakeClock,
    DIGEST,
    UPSTREAM_TOKEN,
    FakeUpstream,
    catalog_payload,
    host_config,
    json_response,
    method_success,
    run_gateway,
)

DAEMON_METHODS = [
    {"name": "agent.list", "path": "/daemon/request/agent.list", "state": "available", "callable": True, "mutates": False},
    {"name": "agent.create", "path": "/daemon/request/agent.create", "state": "available", "callable": True, "mutates": True},
    {"name": "hosts", "path": "/daemon/request/hosts", "state": "blocked", "callable": False, "mutates": False},
]


def test_the_session_response_carries_the_published_shape_and_the_pinned_catalog() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        body = opened.session
        assert set(body) >= {
            "schemaVersion", "ok", "requestId", "driver", "phase", "view", "generation", "csrf",
            "expiresAt", "principal", "catalog",
        }
        assert body["requestId"].startswith("gui-")
        assert opened.reply.headers["x-hyprial-request-id"] == body["requestId"]
        assert body["principal"] == {
            "kind": "serving", "owner": "owner", "node": "node", "actor": "user:owner",
        }
        catalog = body["catalog"]
        assert catalog["digest"] == DIGEST and catalog["methodCount"] == len(catalog["methods"]) == 3
        assert catalog["methods"][0] == {
            "name": "agent.list", "callable": True, "state": "available", "mutates": False,
        }
        assert set(catalog) == {
            "digest", "declarationDigest", "backendEpoch", "httpContractVersion", "catalogVersion",
            "methodCount", "methods",
        }
        assert isinstance(body["expiresAt"], int)
        cookies = opened.reply.headers.get_list("set-cookie")
        assert len(cookies) == 2
        for line in cookies:
            assert "; Path=/daemon; Max-Age=60; HttpOnly; SameSite=Strict" in line
            assert "Secure" not in line

    run_gateway(scenario, clock=FakeClock())


def test_an_ownerless_daemon_opens_an_explicitly_anonymous_bootstrap_view() -> None:
    upstream = FakeUpstream(
        ready={
            "ok": True, "ready": True, "transport": "ready", "notReady": [],
            "backend": {"available": True, "owner": None, "nodeId": "node", "epoch": "e"},
            "catalog": {"fresh": True, "catalogDigest": DIGEST, "declarationDigest": "d"},
        },
        catalog=catalog_payload(
            declarationDigest="d",
            backend={"kind": "gui", "owner": None, "nodeId": "node", "epoch": "e"},
            principal={"kind": "local-gui", "owner": None, "actor": None},
        ),
    )

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.session["phase"] == "bootstrap"
        assert opened.session["principal"] == {
            "kind": "anonymous-bootstrap", "owner": None, "node": None, "actor": None,
        }

    run_gateway(scenario, upstream=upstream, config=host_config(principal=None))


def test_session_status_reports_whether_this_cookie_pair_is_the_live_one() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        headers = opened.headers()
        current = await gateway.call("/daemon/session/status", method="POST", body={}, headers=headers)
        assert current.status == 200 and current.json["current"] is True
        stale = await gateway.call(
            "/daemon/session/status", method="POST", body={}, headers={**headers, "x-hyprial-generation": "old"}
        )
        assert stale.json["current"] is False
        spoof = await gateway.call(
            "/daemon/session/status", method="POST", body={}, headers={**headers, "x-hyprial-view": "wrong"}
        )
        assert (spoof.status, spoof.json["code"]) == (401, "SESSION_REQUIRED")
        filled = await gateway.call("/daemon/session/status", method="POST", body={"x": 1}, headers=headers)
        assert (filled.status, filled.json["code"]) == (400, "INVALID_REQUEST")
        wrong_verb = await gateway.call("/daemon/session/status", headers=headers)
        assert (wrong_verb.status, wrong_verb.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")

    run_gateway(scenario)


def test_daemon_readyz_and_catalog_are_session_gated_relays_that_keep_the_query() -> None:
    def upstream_for_reads() -> FakeUpstream:
        return FakeUpstream()

    upstream = upstream_for_reads()

    async def scenario(gateway) -> None:
        anonymous = await gateway.call("/daemon/readyz")
        assert (anonymous.status, anonymous.json["code"]) == (401, "SESSION_REQUIRED")
        opened = await gateway.open()
        ready = await gateway.call("/daemon/readyz", headers=opened.headers())
        assert ready.status == 200 and ready.json["ready"] is True
        assert ready.headers["x-hyprial-catalog-digest"] == DIGEST
        catalog = await gateway.call("/daemon/request?view=inbox", headers=opened.headers())
        assert catalog.status == 200 and catalog.json["generatedFrom"] == "daemon.catalog"
        post = await gateway.call("/daemon/readyz", method="POST", body={}, headers=opened.headers())
        assert (post.status, post.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")
        post = await gateway.call("/daemon/request", method="POST", body={}, headers=opened.headers())
        assert (post.status, post.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")

    run_gateway(scenario, upstream=upstream)
    paths = [r.url.raw_path.decode() for r in upstream.captured]
    assert paths[-2:] == ["/readyz", "/daemon/request?view=inbox"] or "/daemon/request?view=inbox" in paths
    assert "/daemon/request?view=inbox" in paths


def test_the_contract_mock_catalog_is_synthesised_from_the_session_pin() -> None:
    methods: list[str] = []

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
                    "methods": {
                        "agent.list": {"scenarios": ["success"], "mutates": False},
                        "agent.create": {"scenarios": ["success"], "mutates": True},
                    },
                },
            )
        name = request.url.path.rsplit("/", 1)[-1]
        methods.append(name)
        return json_response(200, method_success(name))

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.session["driver"] == "contract-mock"
        assert opened.session["catalog"]["digest"] == "sha256:mock-contract"
        assert opened.session["catalog"]["catalogVersion"] == "2"

        catalog = await gateway.call("/daemon/request", headers=opened.headers())
        assert catalog.status == 200
        assert catalog.json["catalogVersion"] == "2" and catalog.json["generatedFrom"] == "contract-mock"
        assert catalog.json["backend"] == {"kind": "contract-mock", "epoch": "contract-mock:sha256:mock-contract"}
        by_name = {m["name"]: m for m in catalog.json["methods"]}
        assert by_name["agent.list"]["mutates"] is False and by_name["agent.create"]["mutates"] is True
        assert by_name["agent.list"]["path"] == "/daemon/request/agent.list"
        assert catalog.headers["x-hyprial-catalog-digest"] == "sha256:mock-contract"

        ready = await gateway.call("/daemon/readyz", headers=opened.headers())
        assert ready.json["driver"] == "contract-mock"
        assert ready.json["backend"] == {"available": True, "epoch": "contract-mock:sha256:mock-contract"}
        assert ready.json["catalog"]["fresh"] is True

        headers = opened.headers()
        del headers["x-hyprial-csrf"]
        read = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=headers)
        assert read.status == 200 and "agent.list" in methods
        write = await gateway.call("/daemon/request/agent.create", method="POST", body={"name": "x"}, headers=headers)
        assert (write.status, write.json["code"]) == (403, "CSRF_REJECTED")
        assert "agent.create" not in methods

    run_gateway(scenario, upstream=httpx.MockTransport(handle), config=host_config(driver="contract-mock"))


def test_only_callable_catalog_methods_with_the_pinned_digest_are_admitted() -> None:
    observed: list[httpx.Request] = []

    def method(request: httpx.Request, body: bytes):
        observed.append(request)
        return json_response(200, method_success("agent.list"))

    upstream = FakeUpstream(catalog=catalog_payload(methods=DAEMON_METHODS), method=method)

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        get_write = await gateway.call("/daemon/request/agent.create", headers=opened.headers())
        assert (get_write.status, get_write.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")
        assert get_write.json["method"] == "agent.create"

        unknown = await gateway.call("/daemon/request/not.registered", method="POST", body={}, headers=opened.headers())
        assert (unknown.status, unknown.json["code"], unknown.json["method"]) == (404, "METHOD_NOT_FOUND", "not.registered")

        blocked = await gateway.call("/daemon/request/hosts", method="POST", body={}, headers=opened.headers())
        assert (blocked.status, blocked.json["code"], blocked.json["dispatch"]) == (409, "METHOD_NOT_CALLABLE", "not-dispatched")

        drift = await gateway.call(
            "/daemon/request/agent.list", method="POST", body={}, headers=opened.headers(digest="sha256:other")
        )
        assert (drift.status, drift.json["code"], drift.json["method"]) == (409, "CATALOG_MISMATCH", "agent.list")
        assert observed == []

        pinned = opened.headers()
        del pinned["x-hyprial-catalog-digest"]
        ok = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=pinned)
        assert ok.status == 200
        assert observed[0].headers["x-hyprial-catalog-digest"] == DIGEST

    run_gateway(scenario, upstream=upstream)


@pytest.mark.parametrize("name", ["Agent", "agent..x", "a/b", "agent.", "1agent", "agent list"])
def test_a_malformed_method_name_is_not_found(name: str) -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(f"/daemon/request/{name}", method="POST", body={}, headers=opened.headers())
        assert (reply.status, reply.json["code"]) == (404, "METHOD_NOT_FOUND")

    run_gateway(scenario)


def test_the_relay_preserves_query_request_id_pin_bearer_custody_and_upstream_receipts() -> None:
    observed: list[tuple[httpx.Request, bytes]] = []

    def method(request: httpx.Request, body: bytes):
        observed.append((request, body))
        return json_response(
            202,
            method_success("agent.list", "upstream-request-42"),
            {"x-hyprial-request-id": "header-request", "x-hyprial-catalog-digest": DIGEST, "x-hyprial-dispatch": "dispatched"},
        )

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        headers = opened.headers()
        headers["x-hyprial-request-id"] = "browser-request-id"
        headers["cookie"] = f"{opened.cookies}; hostile=value"
        reply = await gateway.call(
            "/daemon/request/agent.list?view=inbox&size=2", method="POST", body={"query": "ignored"}, headers=headers
        )
        assert reply.status == 202
        assert reply.headers["x-hyprial-request-id"] == "upstream-request-42"
        assert reply.headers["x-hyprial-catalog-digest"] == DIGEST
        assert reply.headers["x-hyprial-dispatch"] == "dispatched"

    run_gateway(scenario, upstream=FakeUpstream(method=method))
    request, body = observed[0]
    assert request.url.raw_path == b"/daemon/request/agent.list?view=inbox&size=2"
    assert json.loads(body) == {"query": "ignored"}
    assert request.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
    assert request.headers["x-hyprial-request-id"] == "browser-request-id"
    assert "cookie" not in request.headers
    assert "hyprial_gui" not in str(dict(request.headers))


def test_malformed_method_bodies_are_refused_before_dispatch_and_a_host_id_is_forwarded() -> None:
    seen: list[httpx.Request] = []

    def method(request: httpx.Request, body: bytes):
        seen.append(request)
        return json_response(200, method_success("agent.list"))

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        headers = opened.headers()
        array = await gateway.call("/daemon/request/agent.list", method="POST", body=[], headers=headers)
        assert (array.status, array.json["code"], array.json["dispatch"]) == (400, "INVALID_REQUEST", "not-dispatched")
        scalar = await gateway.call("/daemon/request/agent.list", method="POST", body="text", headers=headers)
        assert (scalar.status, scalar.json["code"]) == (400, "INVALID_REQUEST")
        media = await gateway.call(
            "/daemon/request/agent.list", method="POST", body={}, headers={**headers, "content-type": "text/plain"}
        )
        assert (media.status, media.json["code"]) == (415, "HTTP_UNSUPPORTED_MEDIA_TYPE")
        oversized = await gateway.call(
            "/daemon/request/agent.list", method="POST", body="x" * MAX_REQUEST_BYTES, headers=headers
        )
        assert (oversized.status, oversized.json["code"]) == (413, "HTTP_REQUEST_TOO_LARGE")
        assert seen == []
        accepted = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=headers)
        assert accepted.status == 200
        assert accepted.headers["x-hyprial-request-id"] == "upstream-request"

    run_gateway(scenario, upstream=FakeUpstream(method=method))
    assert seen[0].headers["x-hyprial-request-id"].startswith("gui-")


def test_a_session_body_over_its_limit_is_a_413() -> None:
    async def scenario(gateway) -> None:
        reply = await gateway.call("/daemon/session", method="POST", raw_body=b'{"grant":"' + b"x" * 9_000 + b'"}')
        assert (reply.status, reply.json["code"]) == (413, "HTTP_REQUEST_TOO_LARGE")

    run_gateway(scenario)


def test_a_missing_contract_mock_scenario_is_relayed_as_a_truthful_409_receipt() -> None:
    missing = {
        "schemaVersion": 1, "ok": False, "method": "agent.list", "requestId": "mock-request",
        "code": "MOCK_SCENARIO_MISSING", "message": "agent.list has no empty example", "dispatch": "not-dispatched",
    }

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        assert reply.status == 409 and reply.json == missing
        assert reply.headers["x-hyprial-request-id"] == "mock-request"

    run_gateway(scenario, upstream=FakeUpstream(method=lambda request, body: json_response(409, missing)))


def test_invalid_or_oversized_upstream_method_envelopes_are_host_refusals() -> None:
    async def invalid(gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(
            "/daemon/request/agent.create", method="POST", body={"name": "x"}, headers=opened.headers()
        )
        assert reply.status == 502
        assert (reply.json["code"], reply.json["dispatch"]) == ("UPSTREAM_INVALID_RESPONSE", "unknown")

    run_gateway(
        invalid,
        upstream=FakeUpstream(method=lambda r, b: httpx.Response(200, content=b'{"schemaVersion":1,"ok":true}')),
    )

    async def oversized(gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        assert reply.status == 502
        assert (reply.json["code"], reply.json["dispatch"]) == ("UPSTREAM_RESPONSE_TOO_LARGE", "not-dispatched")

    run_gateway(
        oversized,
        upstream=FakeUpstream(method=lambda r, b: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))),
    )


def test_transport_loss_is_classified_without_retrying_and_keeps_write_uncertainty() -> None:
    calls: list[str] = []

    def lost(request: httpx.Request, body: bytes):
        calls.append(request.url.path)
        raise httpx.ReadError("socket closed after dispatch", request=request)

    upstream = FakeUpstream(method=lost)

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        write = await gateway.call(
            "/daemon/request/agent.create", method="POST", body={"name": "x"}, headers=opened.headers()
        )
        assert write.status == 502
        assert (write.json["ok"], write.json["code"], write.json["dispatch"]) == (False, "UPSTREAM_UNAVAILABLE", "unknown")
        read = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        assert (read.status, read.json["code"], read.json["dispatch"]) == (502, "UPSTREAM_UNAVAILABLE", "not-dispatched")

    run_gateway(scenario, upstream=upstream)
    assert calls == ["/daemon/request/agent.create", "/daemon/request/agent.list"]
    assert len(upstream.captured) == 4


def test_other_daemon_paths_are_not_found_and_dot_segments_leaving_daemon_are_static() -> None:
    async def scenario(gateway) -> None:
        for path in ("/daemon/", "/daemon/unknown", "/daemon/request/", "/daemon/session/other", "/daemon/readyz/x",
                     "/daemon/session%00"):
            reply = await gateway.call(path)
            assert (reply.status, reply.json["code"]) == (404, "METHOD_NOT_FOUND"), path
        for raw in (b"/daemon/../x", b"/daemon/..", b"/daemon/a/../../x", b"/daemon/%2e%2e/x"):
            status, _, body = await gateway.raw(raw)
            assert (status, json.loads(body)["code"]) == (404, "NOT_FOUND"), raw
        status, _, body = await gateway.raw(b"/daemon/a/../session")
        assert (status, json.loads(body)["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")
        status, _, body = await gateway.raw(b"/daemon/a b")
        assert (status, json.loads(body)["code"]) == (404, "METHOD_NOT_FOUND")

    run_gateway(scenario)


def test_an_unexpected_fault_is_a_500_host_envelope_without_dispatch() -> None:
    async def scenario(gateway) -> None:
        async def boom(config):
            raise RuntimeError("synthetic")

        gateway.app.upstream.describe = boom
        opened = await gateway.open()
        assert (opened.reply.status, opened.reply.json["code"]) == (500, "HOST_INTERNAL_ERROR")
        assert opened.reply.json["dispatch"] == "not-dispatched"
        assert "synthetic" not in opened.reply.raw
        method = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers={"x-hyprial-view": "v"})
        assert method.status == 401

    run_gateway(scenario)


def test_the_session_response_declares_the_contract_version_it_serves() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open(headers={"x-hyprial-session-contract": "1"})
        assert opened.session["sessionContract"] == {"version": 1, "min": 1, "max": 1}
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        refreshed = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": control, "x-hyprial-view": str(opened.session["view"])},
        )
        assert refreshed.json["sessionContract"] == {"version": 1, "min": 1, "max": 1}

    run_gateway(scenario)


@pytest.mark.parametrize("value", ["2", "0", "01", "+1", "1.0", "-1", "abc", "1.5", "99999999999999999999"])
def test_an_unsupported_session_contract_is_refused_before_the_grant_is_spent(value: str) -> None:
    async def scenario(gateway) -> None:
        refused = await gateway.open(headers={"x-hyprial-session-contract": value})
        assert refused.reply.status == 409
        assert refused.reply.json["code"] == "SESSION_CONTRACT_UNSUPPORTED"
        assert refused.reply.json["dispatch"] == "not-dispatched"
        assert refused.reply.json["data"] == {"min": 1, "max": 1}
        assert refused.cookies == ""
        accepted = await gateway.open()
        assert accepted.reply.status == 200

    run_gateway(scenario)


def test_an_absent_session_contract_header_means_version_one() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.reply.status == 200

    run_gateway(scenario)
