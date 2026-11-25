"""Upstream probe and relay (ports of describeUpstream and relayUpstream cases)."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from hyprial.shell.impl.gui.host.envelope import MAX_RESPONSE_BYTES, HostFailure
from hyprial.shell.impl.gui.host.upstream import UpstreamClient
from tests.shell.gui.host.support import (
    DECLARATION,
    DIGEST,
    EPOCH,
    PRINCIPAL,
    UPSTREAM_TOKEN,
    FakeUpstream,
    catalog_payload,
    host_config,
    json_response,
    method_success,
    ready_payload,
)

BOOTSTRAP_BACKEND = {"available": True, "owner": None, "nodeId": "node", "epoch": EPOCH}


def describe(upstream: FakeUpstream, **config: Any):
    client = UpstreamClient(transport=upstream.transport())
    return asyncio.run(client.describe(host_config(**config)))


def refused(upstream: FakeUpstream, **config: Any) -> HostFailure:
    with pytest.raises(HostFailure) as caught:
        describe(upstream, **config)
    return caught.value


def ownerless() -> FakeUpstream:
    return FakeUpstream(
        ready=ready_payload(backend=BOOTSTRAP_BACKEND),
        catalog=catalog_payload(
            backend={"kind": "gui", "nodeId": "node", "owner": None, "epoch": EPOCH},
            principal={"kind": "local-gui", "owner": None, "actor": None},
        ),
    )


def test_a_serving_daemon_yields_a_ready_authority_with_its_pinned_catalog() -> None:
    authority = describe(FakeUpstream())
    assert authority.driver == "daemon" and authority.phase == "ready"
    assert authority.catalog_digest == DIGEST and authority.declaration_digest == DECLARATION
    assert authority.backend_epoch == EPOCH
    assert [(m.name, m.mutates, m.callable, m.state) for m in authority.methods] == [
        ("agent.list", False, True, "available"),
        ("agent.create", True, True, "available"),
        ("message.query", False, True, "available"),
    ]


def test_an_ownerless_daemon_projects_a_bootstrap_authority() -> None:
    authority = describe(ownerless(), principal=None)
    assert authority.phase == "bootstrap"
    assert authority.backend_epoch == EPOCH and authority.catalog_digest == DIGEST


def test_an_ownerless_daemon_is_refused_when_a_serving_principal_is_configured() -> None:
    assert refused(ownerless()).code == "PRINCIPAL_MISMATCH"


def test_a_daemon_that_claims_an_owner_is_refused_for_the_anonymous_principal() -> None:
    claiming = FakeUpstream(ready=ready_payload(backend={**BOOTSTRAP_BACKEND, "owner": "someone"}))
    failure = refused(claiming, principal=None)
    assert (failure.status, failure.code) == (502, "PRINCIPAL_MISMATCH")


def test_an_ownerless_catalog_that_claims_an_identity_is_refused() -> None:
    claiming = ownerless()
    claiming.catalog = catalog_payload(
        backend={"kind": "gui", "nodeId": "node", "owner": None, "epoch": EPOCH},
        principal={"kind": "local-gui", "owner": "someone", "actor": None},
    )
    assert refused(claiming, principal=None).code == "PRINCIPAL_MISMATCH"


def test_an_absent_readiness_owner_key_is_a_contract_mismatch_not_an_ownerless_home() -> None:
    backend = {"available": True, "nodeId": "node", "epoch": EPOCH}
    failure = refused(FakeUpstream(ready=ready_payload(backend=backend)), principal=None)
    assert (failure.status, failure.code) == (502, "UPSTREAM_CONTRACT_MISMATCH")


def test_an_absent_catalog_principal_actor_key_is_a_contract_mismatch() -> None:
    upstream = ownerless()
    upstream.catalog = catalog_payload(
        backend={"kind": "gui", "nodeId": "node", "owner": None, "epoch": EPOCH},
        principal={"kind": "local-gui", "owner": None},
    )
    assert refused(upstream, principal=None).code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"ready_status": 503, "ready": {"ok": False, "code": "DAEMON_RESTORING"}}, 503, "HTTP_NOT_READY"),
        ({"ready_status": 401, "ready": {"ok": False}}, 502, "UPSTREAM_AUTH_REJECTED"),
        ({"ready_status": 403, "ready": {"ok": False}}, 502, "UPSTREAM_AUTH_REJECTED"),
        (
            {"ready": ready_payload(backend={"available": True, "owner": "other", "nodeId": PRINCIPAL.node, "epoch": EPOCH})},
            502,
            "PRINCIPAL_MISMATCH",
        ),
        (
            {"ready": ready_payload(backend={"available": True, "owner": PRINCIPAL.owner, "nodeId": "other", "epoch": EPOCH})},
            502,
            "PRINCIPAL_MISMATCH",
        ),
        (
            {"catalog": catalog_payload(principal={"owner": PRINCIPAL.owner, "actor": "agent:owner:node:other"})},
            502,
            "PRINCIPAL_MISMATCH",
        ),
        (
            {"catalog": catalog_payload(backend={"kind": "daemon", "owner": PRINCIPAL.owner, "nodeId": PRINCIPAL.node, "epoch": "other-epoch"})},
            502,
            "UPSTREAM_CONTRACT_MISMATCH",
        ),
        ({"catalog": catalog_payload(catalogDigest="sha256:other")}, 502, "CATALOG_MISMATCH"),
        ({"catalog": catalog_payload(declarationDigest="sha256:other")}, 502, "CATALOG_MISMATCH"),
        ({"catalog": catalog_payload(generatedFrom="other")}, 502, "UPSTREAM_CONTRACT_MISMATCH"),
        ({"catalog": catalog_payload(ok=False)}, 502, "UPSTREAM_CONTRACT_MISMATCH"),
        ({"catalog": catalog_payload(methods=[{"name": "x"}])}, 502, "UPSTREAM_CONTRACT_MISMATCH"),
        ({"catalog": catalog_payload(methods="nope")}, 502, "UPSTREAM_CONTRACT_MISMATCH"),
        ({"ready": ready_payload(ready=False)}, 503, "HTTP_NOT_READY"),
        ({"ready": ready_payload(notReady=["agent.list"])}, 503, "HTTP_NOT_READY"),
        ({"ready": ready_payload(backend={"available": False, "owner": "owner", "nodeId": "node", "epoch": EPOCH})}, 503, "HTTP_NOT_READY"),
        ({"ready": ready_payload(backend=None)}, 502, "UPSTREAM_CONTRACT_MISMATCH"),
        ({"catalog_status": 500, "catalog": {"code": "BOOM", "message": "broken"}}, 502, "BOOM"),
    ],
)
def test_the_probe_refuses_before_minting(overrides: dict[str, Any], status: int, code: str) -> None:
    failure = refused(FakeUpstream(**overrides))
    assert (failure.status, failure.code, failure.dispatch) == (status, code, "not-dispatched")


@pytest.mark.parametrize(
    "catalog",
    [
        {"fresh": False, "catalogDigest": DIGEST, "declarationDigest": DECLARATION},
        {"catalogDigest": DIGEST, "declarationDigest": DECLARATION},
    ],
)
def test_a_ready_projection_whose_catalog_is_not_explicitly_fresh_is_not_ready(catalog: dict[str, Any]) -> None:
    failure = refused(FakeUpstream(ready=ready_payload(catalog=catalog)))
    assert (failure.status, failure.code) == (503, "HTTP_NOT_READY")


def test_the_authoritative_restoring_refusal_is_relayed_with_its_upstream_code() -> None:
    failure = refused(
        FakeUpstream(
            ready_status=503,
            ready={"schemaVersion": 1, "ok": False, "ready": False, "code": "DAEMON_RESTORING", "message": "restoring"},
        )
    )
    assert (failure.status, failure.code, failure.message) == (503, "HTTP_NOT_READY", "restoring")
    assert failure.data == {"upstreamCode": "DAEMON_RESTORING"}


def test_a_catalog_digest_header_that_differs_from_its_body_is_a_mismatch() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return json_response(200, ready_payload())
        return json_response(200, catalog_payload(), {"x-hyprial-catalog-digest": "sha256:header"})

    client = UpstreamClient(transport=httpx.MockTransport(handle))
    with pytest.raises(HostFailure) as caught:
        asyncio.run(client.describe(host_config()))
    assert caught.value.code == "CATALOG_MISMATCH"


def test_probe_calls_carry_the_bearer_and_nothing_from_the_browser() -> None:
    upstream = FakeUpstream()
    describe(upstream)
    assert [r.url.raw_path.decode() for r in upstream.captured] == ["/readyz", "/daemon/request"]
    for request in upstream.captured:
        assert request.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
        assert request.headers["accept"] == "application/json"
        assert "cookie" not in request.headers


def test_an_unreachable_or_non_json_upstream_is_unavailable_or_invalid() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(HostFailure) as down:
        asyncio.run(UpstreamClient(transport=httpx.MockTransport(unreachable)).describe(host_config()))
    assert (down.value.status, down.value.code) == (503, "UPSTREAM_UNAVAILABLE")

    garbage = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(HostFailure) as invalid:
        asyncio.run(UpstreamClient(transport=garbage).describe(host_config()))
    assert (invalid.value.status, invalid.value.code) == (502, "UPSTREAM_INVALID_RESPONSE")

    scalar = httpx.MockTransport(lambda request: httpx.Response(200, content=b"[]"))
    with pytest.raises(HostFailure) as listed:
        asyncio.run(UpstreamClient(transport=scalar).describe(host_config()))
    assert listed.value.code == "UPSTREAM_INVALID_RESPONSE"


def test_a_redirect_is_never_followed() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:1/elsewhere"})

    with pytest.raises(HostFailure) as caught:
        asyncio.run(UpstreamClient(transport=httpx.MockTransport(handle)).describe(host_config()))
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert seen == ["/readyz"]


def test_an_oversized_probe_response_is_refused() -> None:
    big = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(HostFailure) as caught:
        asyncio.run(UpstreamClient(transport=big).describe(host_config()))
    assert caught.value.code == "UPSTREAM_RESPONSE_TOO_LARGE"


def mock_upstream(methods: dict[str, Any], **extra: Any) -> httpx.MockTransport:
    contract = {"mock": True, "contractVersion": "2", "contractDigest": "sha256:mock-contract", "methods": methods, **extra}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return json_response(200, {"ok": True})
        if request.url.path == "/mock/contract":
            return json_response(200, contract)
        return json_response(404, {"ok": False})

    return httpx.MockTransport(handle)


def test_the_contract_mock_probe_reads_the_real_metadata_shape() -> None:
    client = UpstreamClient(transport=mock_upstream({"agent.list": {"scenarios": ["success"]}}))
    authority = asyncio.run(client.describe(host_config(driver="contract-mock")))
    assert authority.driver == "contract-mock" and authority.phase == "ready"
    assert authority.catalog_digest == authority.declaration_digest == "sha256:mock-contract"
    assert authority.backend_epoch == "contract-mock:sha256:mock-contract"
    assert authority.catalog_version == "2" and authority.http_contract_version == 1
    assert [(m.name, m.callable, m.state) for m in authority.methods] == [("agent.list", True, "available")]


def test_a_mock_method_mutates_is_the_wire_boolean_otherwise_true() -> None:
    methods = {
        "wire.read": {"scenarios": [], "mutates": False},
        "wire.write": {"scenarios": [], "mutates": True},
        "agent.list": {"scenarios": []},
        "string.false": {"scenarios": [], "mutates": "false"},
        "zero": {"scenarios": [], "mutates": 0},
        "null": {"scenarios": [], "mutates": None},
    }
    client = UpstreamClient(transport=mock_upstream(methods))
    authority = asyncio.run(client.describe(host_config(driver="contract-mock")))
    assert {m.name: m.mutates for m in authority.methods} == {
        "wire.read": False,
        "wire.write": True,
        "agent.list": True,
        "string.false": True,
        "zero": True,
        "null": True,
    }


def test_the_host_has_no_local_method_declaration_to_consult() -> None:
    import hyprial.shell.impl.gui.host.upstream as upstream

    assert not hasattr(upstream, "declared_mutates")
    assert "declared_mutates" not in UpstreamClient.__init__.__code__.co_varnames


@pytest.mark.parametrize(
    "contract",
    [
        {"mock": False, "contractDigest": "d", "methods": {}},
        {"mock": True, "methods": {}},
        {"mock": True, "contractDigest": "d"},
        {"mock": True, "contractDigest": "d", "methods": {"a": {"scenarios": "x"}}},
    ],
)
def test_an_incomplete_contract_mock_is_a_contract_mismatch(contract: dict[str, Any]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"ok": True} if request.url.path == "/healthz" else contract)

    client = UpstreamClient(transport=httpx.MockTransport(handle))
    with pytest.raises(HostFailure) as caught:
        asyncio.run(client.describe(host_config(driver="contract-mock")))
    assert (caught.value.status, caught.value.code) == (502, "UPSTREAM_CONTRACT_MISMATCH")


def test_an_unhealthy_contract_mock_is_not_ready() -> None:
    client = UpstreamClient(transport=httpx.MockTransport(lambda request: json_response(200, {"ok": False})))
    with pytest.raises(HostFailure) as caught:
        asyncio.run(client.describe(host_config(driver="contract-mock")))
    assert (caught.value.status, caught.value.code) == (503, "HTTP_NOT_READY")


def relay(upstream: httpx.MockTransport, *, write: bool = False, body: bytes | None = None,
          expected: str | None = "agent.list", incoming_id: str | None = None, path: str = "/daemon/request/agent.list"):
    client = UpstreamClient(transport=upstream)
    return asyncio.run(
        client.relay(
            host_config(),
            path=path,
            method="POST" if body is not None else "GET",
            incoming_request_id=incoming_id,
            body=body,
            write=write,
            pinned_digest=DIGEST,
            expected_method=expected,
            fallback_request_id="gui-fallback",
        )
    )


def test_relay_sends_only_the_host_header_set_and_keeps_the_path_and_query() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(202, method_success("agent.list", "upstream-request-42"), {
            "x-hyprial-request-id": "header-request",
            "x-hyprial-catalog-digest": DIGEST,
            "x-hyprial-dispatch": "dispatched",
        })

    result = relay(
        httpx.MockTransport(handle), body=b'{"query":"ignored"}', incoming_id="browser-request-id",
        path="/daemon/request/agent.list?view=inbox&size=2",
    )
    request = seen[0]
    assert request.url.raw_path == b"/daemon/request/agent.list?view=inbox&size=2"
    assert request.content == b'{"query":"ignored"}'
    assert request.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"
    assert request.headers["x-hyprial-request-id"] == "browser-request-id"
    assert request.headers["x-hyprial-catalog-digest"] == DIGEST
    assert request.headers["content-type"] == "application/json"
    assert not {"cookie", "origin", "x-hyprial-view", "x-hyprial-csrf", "x-hyprial-generation"} & set(request.headers)
    assert result.status == 202
    assert result.headers == {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "x-hyprial-request-id": "upstream-request-42",
        "x-hyprial-catalog-digest": DIGEST,
        "x-hyprial-dispatch": "dispatched",
    }
    assert json.loads(result.body)["requestId"] == "upstream-request-42"


def test_relay_forwards_the_host_request_id_when_the_browser_sent_none() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, method_success("agent.list"))

    relay(httpx.MockTransport(handle), body=b"{}")
    assert seen[0].headers["x-hyprial-request-id"] == "gui-fallback"


def test_a_get_relay_has_no_content_type_and_uses_the_pin_for_the_digest_header() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "content-type" not in request.headers
        return json_response(200, {"schemaVersion": 1, "ok": True, "requestId": "r"})

    result = relay(httpx.MockTransport(handle), expected=None, path="/readyz")
    assert result.headers["x-hyprial-catalog-digest"] == DIGEST
    assert result.headers["x-hyprial-request-id"] == "r"
    assert "x-hyprial-dispatch" not in result.headers


def test_a_relayed_error_envelope_passes_through_byte_for_byte() -> None:
    missing = {
        "schemaVersion": 1, "ok": False, "method": "agent.list", "requestId": "mock-request",
        "code": "MOCK_SCENARIO_MISSING", "message": "no empty example", "dispatch": "not-dispatched",
    }
    raw = json.dumps(missing, indent=1).encode()
    result = relay(httpx.MockTransport(lambda request: httpx.Response(409, content=raw)), body=b"{}")
    assert result.status == 409 and result.body == raw
    assert result.headers["x-hyprial-request-id"] == "mock-request"
    assert result.headers["x-hyprial-dispatch"] == "not-dispatched"


@pytest.mark.parametrize(
    "payload",
    [
        {"schemaVersion": 1, "ok": True},
        {"schemaVersion": 2, "ok": True, "requestId": "r", "dispatch": "dispatched", "method": "agent.list"},
        {"schemaVersion": 1, "ok": "yes", "requestId": "r", "dispatch": "dispatched", "method": "agent.list"},
        {"schemaVersion": 1, "ok": True, "requestId": "r", "dispatch": "maybe", "method": "agent.list"},
        {"schemaVersion": 1, "ok": True, "requestId": "r", "dispatch": "dispatched", "method": "other"},
    ],
)
@pytest.mark.parametrize(("write", "dispatch"), [(True, "unknown"), (False, "not-dispatched")])
def test_an_invalid_method_envelope_is_a_502_with_write_uncertainty(payload, write, dispatch) -> None:
    transport = httpx.MockTransport(lambda request: json_response(200, payload))
    with pytest.raises(HostFailure) as caught:
        relay(transport, write=write, body=b"{}")
    assert (caught.value.status, caught.value.code, caught.value.dispatch, caught.value.method) == (
        502, "UPSTREAM_INVALID_RESPONSE", dispatch, "agent.list",
    )


def test_a_non_json_method_response_cannot_be_validated() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(HostFailure) as caught:
        relay(transport, write=True, body=b"{}")
    assert (caught.value.code, caught.value.dispatch) == ("UPSTREAM_INVALID_RESPONSE", "unknown")


@pytest.mark.parametrize(("write", "dispatch"), [(True, "unknown"), (False, "not-dispatched")])
def test_an_oversized_method_response_keeps_the_write_dispatch_truth(write: bool, dispatch: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(HostFailure) as caught:
        relay(transport, write=write, body=b"{}")
    assert (caught.value.status, caught.value.code, caught.value.dispatch) == (
        502, "UPSTREAM_RESPONSE_TOO_LARGE", dispatch,
    )


@pytest.mark.parametrize(("write", "dispatch"), [(True, "unknown"), (False, "not-dispatched")])
def test_transport_loss_is_classified_without_retry(write: bool, dispatch: str) -> None:
    calls: list[int] = []

    def lost(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadError("socket closed after dispatch", request=request)

    with pytest.raises(HostFailure) as caught:
        relay(httpx.MockTransport(lost), write=write, body=b"{}")
    assert (caught.value.status, caught.value.code, caught.value.dispatch) == (502, "UPSTREAM_UNAVAILABLE", dispatch)
    assert calls == [1]


@pytest.mark.parametrize(("write", "dispatch"), [(True, "unknown"), (False, "not-dispatched")])
def test_a_response_that_dies_mid_body_is_unavailable_not_invalid(write: bool, dispatch: str) -> None:
    class Dying(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"schemaVersion":'
            raise httpx.ReadError("reset")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Dying()))
    with pytest.raises(HostFailure) as caught:
        relay(transport, write=write, body=b"{}")
    assert (caught.value.code, caught.value.dispatch) == ("UPSTREAM_UNAVAILABLE", dispatch)


class _Recording(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:
        body = json.dumps(ready_payload()).encode()
        type(self).seen.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


def test_proxy_environment_variables_never_change_the_upstream_target(monkeypatch) -> None:
    _Recording.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        client = UpstreamClient()
        config = host_config(endpoint=f"http://127.0.0.1:{server.server_address[1]}", principal=PRINCIPAL)

        async def probe() -> None:
            status, _, body = await client.get_json(config, "/readyz", authenticated=True)
            assert status == 200 and body["ok"] is True
            await client.aclose()

        asyncio.run(probe())
    finally:
        server.shutdown()
        server.server_close()
    assert _Recording.seen == ["/readyz"]

