"""HTTP framing for the Contract Mock: loopback, auth, CORS and envelopes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from contract_mock import Envelope, MockService, create_server, load_contract

TOKEN = "mock-test-token"
ORIGIN = "http://localhost:5173"


@pytest.fixture()
def live_server(contract_file: Path):
    contract = load_contract(contract_file)
    service = MockService(
        contract,
        Envelope(contract),
        scenario="success",
        token=TOKEN,
    )
    server = create_server(
        service, port=0, allowed_origins=(ORIGIN,)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(
    url: str,
    *,
    method: str = "POST",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
):
    data = raw
    if data is None and payload is not None:
        data = json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return (
                response.status,
                dict(response.headers),
                json.loads(body) if body else None,
            )
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, dict(error.headers), json.loads(body) if body else None


def test_success_response_uses_the_pinned_envelope_and_headers(
    live_server,
) -> None:
    base, _service = live_server
    status, headers, body = _call(
        f"{base}/daemon/request/demo.list",
        payload={"filter": None},
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["dispatch"] == "dispatched"
    assert body["result"]["items"] == ["alpha"]
    assert headers["X-Hyprial-Request-Id"] == body["requestId"]
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert headers["Cache-Control"] == "no-store"


def test_healthz_is_the_only_unauthenticated_get(live_server) -> None:
    base, _service = live_server
    status, _headers, body = _call(f"{base}/healthz", method="GET", headers={})
    assert status == 200
    assert body == {"ok": True}
    status, _headers, body = _call(f"{base}/mock/contract", method="GET", headers={})
    assert status == 401
    assert body["code"] == "HTTP_UNAUTHORIZED"


def test_mock_contract_metadata_requires_the_bearer(live_server) -> None:
    base, service = live_server
    status, _headers, body = _call(
        f"{base}/mock/contract",
        method="GET",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert body["mock"] is True
    assert body["contractDigest"] == service.contract.digest


def test_disallowed_origin_is_refused_without_echoing_it(live_server) -> None:
    base, _service = live_server
    status, headers, body = _call(
        f"{base}/daemon/request/demo.list",
        payload={"filter": None},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "http://evil.example",
        },
    )
    assert status == 403
    assert body["code"] == "HTTP_ORIGIN_DENIED"
    assert "Access-Control-Allow-Origin" not in headers


def test_preflight_only_allows_configured_origins(live_server) -> None:
    base, _service = live_server
    status, headers, body = _call(
        f"{base}/daemon/request/demo.list",
        method="OPTIONS",
        headers={"Origin": ORIGIN},
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert body is None
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list",
        method="OPTIONS",
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert body["code"] == "HTTP_ORIGIN_DENIED"


def test_invalid_json_and_non_object_params_are_refused(live_server) -> None:
    base, _service = live_server
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list",
        raw=b"{not json",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert body["code"] == "INVALID_REQUEST"
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list",
        payload=[1, 2],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert body["code"] == "INVALID_ARGUMENT"


def test_wrong_media_type_and_unknown_path_are_refused(live_server) -> None:
    base, _service = live_server
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list",
        raw=b"{}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "text/plain",
        },
    )
    assert status == 415
    assert body["code"] == "HTTP_UNSUPPORTED_MEDIA_TYPE"
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.missing",
        payload={},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 404
    assert body["code"] == "METHOD_NOT_FOUND"


def test_wrong_http_method_and_query_are_refused(live_server) -> None:
    base, _service = live_server
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list",
        method="GET",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 405
    assert body["code"] == "HTTP_METHOD_NOT_ALLOWED"
    status, _headers, body = _call(
        f"{base}/daemon/request/demo.list?x=1",
        payload={"filter": None},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert body["code"] == "INVALID_REQUEST"
