"""H-01: real isolated daemon + real HTTP process, no fixtures as authority."""

from __future__ import annotations

import json
import time

OWNER = "h01-owner"
ACTOR = "agent:h01-owner:h01-node:operator"


def test_health_ready_catalog_and_two_link_reads(http_server) -> None:
    status, headers, body = http_server.request("GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}

    status, headers, ready = http_server.request("GET", "/readyz")
    assert status == 200, ready
    assert ready["ready"] is True and ready["notReady"] == []
    assert ready["backend"]["owner"] == OWNER
    assert {item["name"] for item in ready["methods"]} == {
        "link.list", "link.stats", "agent.list", "agent.secret.list", "agent.provider.default",
    }
    catalog_digest = headers["X-Hyprial-Catalog-Digest"]
    assert ready["catalog"]["catalogDigest"] == catalog_digest

    status, headers, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    assert catalog["catalogDigest"] == headers["X-Hyprial-Catalog-Digest"]
    assert catalog["generatedFrom"] == "daemon.catalog"
    assert catalog["principal"] == {
        "kind": "local-operator",
        "owner": OWNER,
        "actor": ACTOR,
    }
    methods = {item["name"]: item for item in catalog["methods"]}
    assert set(methods) == {
        "link.list", "link.stats", "agent.list", "agent.secret.list", "agent.provider.default",
    }
    assert methods["agent.provider.default"]["mutates"] is False
    assert methods["agent.provider.default"]["paramsSchema"]["additionalProperties"] is False
    assert methods["link.list"]["httpBinding"]["identity"] == {
        "mode": "local-operator-selector",
        "param": "from",
    }
    assert methods["link.list"]["state"] == "available"
    assert "link.add" not in methods and "daemon.catalog" not in methods

    for name in ("link.list", "link.stats"):
        status, headers, payload = http_server.request("POST", f"/daemon/request/{name}", body={})
        assert status == 200, payload
        assert payload["dispatch"] == "dispatched"
        assert payload["method"] == name
        assert payload["result"]["actor"] == ACTOR
        if name == "link.list":
            assert payload["result"]["orgAvailable"] is False
        assert payload["result"]["links"] == []
        assert headers["X-Hyprial-Catalog-Digest"] == catalog_digest
        assert headers["X-Hyprial-Request-Id"].startswith("http-")


def test_rejection_matrix_distinguishes_local_and_daemon_sources(http_server) -> None:
    status, _, body = http_server.request("POST", "/daemon/request/link.list", body={}, token="wrong")
    assert (status, body["code"], body["dispatch"]) == (401, "HTTP_UNAUTHORIZED", "not-dispatched")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/link.list",
        body={"from": "agent:h01-owner:h01-node:someone-else"},
    )
    assert (status, body["code"], body["dispatch"]) == (403, "PRINCIPAL_MISMATCH", "not-dispatched")
    assert body["data"] == {"field": "from"}
    assert "someone-else" not in json.dumps(body)

    status, _, body = http_server.request("POST", "/daemon/request/link.list", body={"from": 7})
    assert (status, body["code"], body["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/link.list",
        body={"from": "agent:h01-owner:h01-node:someone-else", "junk": 1},
    )
    assert (status, body["code"], body["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")

    status, _, body = http_server.request("POST", "/daemon/request/unknown.method", body={})
    assert (status, body["code"]) == (404, "METHOD_NOT_FOUND")

    status, _, body = http_server.request("POST", "/daemon/request/link.add", body={"from": ACTOR, "peer": "x"})
    assert (status, body["code"]) == (404, "METHOD_NOT_FOUND")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/link.list",
        body={},
        headers={"Content-Type": "text/plain"},
    )
    assert (status, body["code"]) == (415, "HTTP_UNSUPPORTED_MEDIA_TYPE")

    status, _, body = http_server.request(
        "POST", "/daemon/request/link.list", body={}, headers={"Idempotency-Key": "k"}
    )
    assert (status, body["code"]) == (400, "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/link.list",
        body={},
        headers={"X-Hyprial-Catalog-Digest": "sha256:" + "0" * 64},
    )
    assert (status, body["code"]) == (409, "CATALOG_MISMATCH")

    status, _, body = http_server.request("POST", "/daemon/request/link.list", body={"junk": 1})
    assert (status, body["code"]) == (400, "INVALID_ARGUMENT")


def test_raw_framing_and_size_refusals(http_server) -> None:
    token = http_server.token
    auth = f"Authorization: Bearer {token}\r\n"
    status, _ = http_server.raw(
        (
            "POST /daemon/request/link.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + "Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n0\r\n\r\n"
        ).encode()
    )
    assert status == 400

    status, _ = http_server.raw(
        (
            "POST /daemon/request/link.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + "Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
        ).encode()
    )
    assert status == 400

    status, _ = http_server.raw(
        ("GET /daemon/request?x=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n" + auth + "\r\n").encode()
    )
    assert status == 400

    status, _ = http_server.raw(
        ("POST /daemon/request/link.list HTTP/1.1\r\nHost: 127.0.0.1\r\n" + auth + "\r\n").encode()
    )
    assert status == 411

    long_path = "/daemon/request/" + "a" * 600
    status, _ = http_server.raw(
        ("GET " + long_path + " HTTP/1.1\r\nHost: 127.0.0.1\r\n" + auth + "\r\n").encode()
    )
    assert status == 414

    status, _, body = http_server.request(
        "POST", "/daemon/request/link.list", body={"from": "x" * (262144 + 10)}
    )
    assert (status, body["code"]) == (413, "HTTP_BODY_TOO_LARGE")


def test_huge_declared_body_is_refused_without_waiting_for_content(http_server) -> None:
    status, raw = http_server.raw(
        (
            "POST /daemon/request/link.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {http_server.token}\r\n"
            "Content-Type: application/json\r\nContent-Length: 999999999\r\n\r\n"
        ).encode(),
        timeout=1,
    )
    body = json.loads(raw)
    assert (status, body["code"], body["dispatch"]) == (
        413, "HTTP_BODY_TOO_LARGE", "not-dispatched",
    )


def test_cors_preflight_and_error_origin(http_server) -> None:
    origin = "http://127.0.0.1:4173"
    status, headers, _ = http_server.request(
        "OPTIONS", "/daemon/request/link.list", headers={"Origin": origin}
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == origin
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "X-Hyprial-Catalog-Digest" in headers["Access-Control-Expose-Headers"]
    assert "Access-Control-Allow-Credentials" not in headers

    status, headers, _ = http_server.request(
        "OPTIONS", "/readyz", headers={"Origin": origin}
    )
    assert status == 204

    status, headers, body = http_server.request(
        "POST",
        "/daemon/request/link.list",
        body={"from": "agent:h01-owner:h01-node:other"},
        headers={"Origin": origin},
    )
    assert status == 403
    assert headers["Access-Control-Allow-Origin"] == origin
    assert headers["Vary"] == "Origin"

    status, headers, body = http_server.request(
        "GET", "/daemon/request", headers={"Origin": "http://evil.example"}
    )
    assert (status, body["code"]) == (403, "HTTP_ORIGIN_DENIED")
    assert "Access-Control-Allow-Origin" not in headers


def test_epoch_change_without_a_connection_error_is_rehandshaken(http_server, daemon) -> None:
    status, _, before = http_server.request("GET", "/daemon/request")
    assert status == 200
    old_epoch = before["backend"]["epoch"]

    new_epoch = daemon.restart()
    assert new_epoch != old_epoch

    status, headers, after = http_server.request("GET", "/daemon/request")
    assert status == 200, after
    assert after["backend"]["epoch"] == new_epoch
    assert after["catalogDigest"] == headers["X-Hyprial-Catalog-Digest"]
    assert http_server.server_json["backend"]["epoch"] != new_epoch  # snapshot at startup
    document = json.loads((http_server.isolation.runtime / "server.json").read_text(encoding="utf-8"))
    assert document["backend"]["epoch"] == new_epoch

    status, _, payload = http_server.request("POST", "/daemon/request/link.list", body={})
    assert status == 200 and payload["dispatch"] == "dispatched"


def test_daemon_down_is_not_dispatched(http_server, daemon) -> None:
    daemon.stop()
    status, _, ready = http_server.request("GET", "/readyz")
    assert status == 503
    assert ready["ready"] is False
    assert ready["code"] == "DAEMON_UNAVAILABLE"

    status, _, body = http_server.request("POST", "/daemon/request/link.list", body={})
    assert status == 503
    assert (body["code"], body["dispatch"]) == ("DAEMON_UNAVAILABLE", "not-dispatched")
    time.sleep(0.1)


def test_openapi_is_protected_and_reflects_the_live_catalog(http_server) -> None:
    status, _, body = http_server.request("GET", "/openapi.json", token="wrong")
    assert (status, body["code"]) == (401, "HTTP_UNAUTHORIZED")

    status, _, document = http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    assert "/daemon/request/link.list" in paths
    assert "/daemon/request/link.stats" in paths
    assert "/daemon/request/<method>" not in paths
    assert "/docs" not in paths
    assert "422" not in paths["/daemon/request/link.list"]["post"]["responses"]

    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    body_schema = paths["/daemon/request/link.list"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema == methods["link.list"]["paramsSchema"]
    result_schema = paths["/daemon/request/link.list"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["result"]
    assert result_schema == methods["link.list"]["resultSchema"]

    status, _, body = http_server.request("GET", "/healthz/")
    assert (status, body["code"]) == (404, "METHOD_NOT_FOUND")


def test_double_slash_target_is_rejected_by_the_live_listener(http_server) -> None:
    status, raw = http_server.raw(
        b"GET //healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert status == 400
    assert json.loads(raw)["code"] == "INVALID_REQUEST"

    status, raw = http_server.raw(
        b"GET /healthz?x=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert status == 400
    assert json.loads(raw)["code"] == "INVALID_REQUEST"

    status, raw = http_server.raw(
        b"GET http://127.0.0.1/healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert status == 400
    assert json.loads(raw)["code"] == "INVALID_REQUEST"
