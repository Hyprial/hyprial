"""H-01: real isolated daemon + real HTTP process, no fixtures as authority."""

from __future__ import annotations

import base64
import json
import time


from hyprial.kernel import daemon_request_once

from tests.integration.request_http.conftest import Isolation
from tests.integration.request_http.test_org_http_live import (
    _create_space,
    _write_text,
)

OWNER = "h01-owner"
ACTOR = "user:h01-owner"
TARGET = "agent:h01-owner:h01-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_agent_provider_logout_write_reaches_the_real_handler(http_server, isolation) -> None:
    """H-03 round-2 write path over a real daemon: admitted write, refused repeat."""

    created = daemon_request_once(
        "agent.create", {"name": "worker"},
        socket_path=isolation.socket_path, timeout=10,
    )
    agent = created.get("agent") if isinstance(created, dict) else None
    actor = agent["uri"] if isinstance(agent, dict) else created.get("uri")
    params = {"actor": actor, "provider": "claude"}
    status, _, payload = http_server.request(
        "POST", "/daemon/request/agent.provider.logout", body=params,
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["ok"] is True
    assert payload["result"]["actor"].endswith("worker")
    assert payload["result"]["provider"] == "claude"
    assert payload["result"]["removed"] == []
    assert isinstance(payload["result"]["absent"], list)

    login_params = {
        "actor": actor,
        "provider": "claude",
        "files": [
            {
                "filename": ".credentials.json",
                "contentBase64": "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs",
            }
        ],
    }
    status, _, payload = http_server.request(
        "POST", "/daemon/request/agent.provider.login", body=login_params,
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["ok"] is True

    replaced = dict(login_params, files=[
        dict(login_params["files"][0], contentBase64="ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFsLTI=")
    ])
    status, _, payload = http_server.request(
        "POST", "/daemon/request/agent.provider.login", body=replaced,
    )
    assert status == 500, payload
    assert payload["ok"] is False
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "PROVIDER_CREDENTIAL_EXISTS"
    assert "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs" not in json.dumps(payload)

    status, _, payload = http_server.request(
        "POST", "/daemon/request/agent.provider.logout", body=params,
    )
    assert status == 200, payload
    assert payload["result"]["removed"] and payload["result"]["absent"] == []


def test_health_ready_catalog_and_two_link_reads(http_server) -> None:
    status, headers, body = http_server.request("GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}

    status, headers, ready = http_server.request("GET", "/readyz")
    assert status == 200, ready
    assert ready["ready"] is True and ready["notReady"] == []
    assert ready["backend"]["owner"] == OWNER
    assert {
        "adapter.list",
        "adapter.status",
        "adapter.start",
        "adapter.stop",
        "adapter.reload",
        "message.send.lookup",
        "message.history",
        "message.query",
        "progress.list",
        "agent.list",
        "agent.secret.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.create",
        "agent.stop",
        "agent.start",
        "routine.list",
        "routine.status",
        "workflow.list",
        "agent.home-census",
        "org.list",
        "org.invites.list",
        "org.fetch",
        "orgfs.spaces",
        "orgfs.write",
        "orgfs.export",
        "orgfs.import",
        "conversations.list",
        "conversations.history",
    } <= {item["name"] for item in ready["methods"]}
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
    assert {
        "adapter.list",
        "adapter.status",
        "adapter.start",
        "adapter.stop",
        "adapter.reload",
        "message.send.lookup",
        "message.history",
        "message.query",
        "progress.list",
        "agent.list",
        "agent.secret.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.create",
        "agent.stop",
        "agent.start",
        "routine.list",
        "routine.status",
        "workflow.list",
        "agent.home-census",
        "org.list",
        "org.invites.list",
        "org.fetch",
        "orgfs.spaces",
        "orgfs.write",
        "orgfs.export",
        "orgfs.import",
        "conversations.list",
        "conversations.history",
    } <= set(methods)
    for name in (
        "adapter.start", "adapter.stop", "adapter.reload",
        "agent.provider.default", "agent.provider.login", "agent.provider.logout",
        "agent.create", "agent.stop", "agent.start",
    ):
        assert methods[name]["mutates"] is True, name
        assert methods[name]["retry"] == "never", name
        assert methods[name]["httpBinding"]["identity"] == {"mode": "none", "param": None}, name
        assert methods[name]["state"] == "available", name
        assert methods[name]["callable"] is True, name
    assert methods["agent.provider.default"]["paramsSchema"]["additionalProperties"]["type"] == ["string", "number", "boolean", "null", "array", "object"]
    assert methods["progress.list"]["httpBinding"]["identity"] == {
        "mode": "local-owner-user-selector",
        "param": "actor",
    }
    assert methods["progress.list"]["state"] == "available"
    assert methods["adapter.list"]["httpBinding"]["identity"] == {"mode": "none", "param": None}
    assert methods["message.send.lookup"]["httpBinding"]["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert methods["message.history"]["httpBinding"]["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert methods["message.query"]["httpBinding"]["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert "orgfs.export" in methods and "daemon.catalog" not in methods
    assert methods["orgfs.export"]["httpBinding"]["identity"] == {
        "mode": "local-owner-selector",
        "param": "from",
    }
    assert methods["message.send"]["httpBinding"]["paramsConstraints"]["required"] == [
        "to",
        "message",
        "summary",
        "idempotencyKey",
    ]

    status, headers, adapters = http_server.request("POST", "/daemon/request/adapter.list", body={})
    if methods["adapter.list"]["state"] == "available":
        assert status == 200, adapters
        assert adapters["dispatch"] == "dispatched"
        assert adapters["result"] == {"ok": True, "adapters": []}
    else:
        assert methods["adapter.list"]["state"] == "not-ready"
        assert status == 503, adapters

    status, headers, payload = http_server.request(
        "POST", "/daemon/request/message.query", body={"view": "inbox", "from": TARGET}
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["method"] == "message.query"
    assert payload["result"]["actor"] == ACTOR
    assert payload["result"]["entries"] == []
    assert headers["X-Hyprial-Catalog-Digest"] == catalog_digest
    assert headers["X-Hyprial-Request-Id"].startswith("http-")

    status, headers, payload = http_server.request(
        "POST", "/daemon/request/conversations.list", body={}
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["participant"] == ACTOR
    assert payload["result"]["conversations"] == []


def test_retired_link_and_thread_methods_are_not_exposed(http_server) -> None:
    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    for name in ("link.list", "link.stats", "link.add", "link.drop", "threads.list", "threads.get"):
        assert name not in methods
        status, _, body = http_server.request("POST", f"/daemon/request/{name}", body={})
        assert (status, body["code"]) == (404, "METHOD_NOT_FOUND"), name


def test_rejection_matrix_distinguishes_local_and_daemon_sources(http_server) -> None:
    status, _, body = http_server.request("POST", "/daemon/request/conversations.list", body={}, token="wrong")
    assert (status, body["code"], body["dispatch"]) == (401, "HTTP_UNAUTHORIZED", "not-dispatched")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/progress.list",
        body={"actor": "user:someone-else", "from": TARGET},
    )
    assert (status, body["code"], body["dispatch"]) == (403, "PRINCIPAL_MISMATCH", "not-dispatched")
    assert "someone-else" not in json.dumps(body)

    status, _, body = http_server.request("POST", "/daemon/request/progress.list", body={"from": 7})
    assert (status, body["code"], body["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/progress.list",
        body={"from": "agent:h01-owner:h01-node:someone-else", "junk": 1},
    )
    assert (status, body["code"], body["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")

    status, _, body = http_server.request("POST", "/daemon/request/unknown.method", body={})
    assert (status, body["code"]) == (404, "METHOD_NOT_FOUND")

    status, _, body = http_server.request(
        "POST", "/daemon/request/daemon.catalog", body={}
    )
    assert (status, body["code"]) == (404, "METHOD_NOT_FOUND")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/conversations.list",
        body={},
        headers={"Content-Type": "text/plain"},
    )
    assert (status, body["code"]) == (415, "HTTP_UNSUPPORTED_MEDIA_TYPE")

    status, _, body = http_server.request(
        "POST", "/daemon/request/conversations.list", body={}, headers={"Idempotency-Key": "k"}
    )
    assert (status, body["code"]) == (400, "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED")

    status, _, body = http_server.request(
        "POST",
        "/daemon/request/conversations.list",
        body={},
        headers={"X-Hyprial-Catalog-Digest": "sha256:" + "0" * 64},
    )
    assert (status, body["code"]) == (409, "CATALOG_MISMATCH")

    status, _, body = http_server.request("POST", "/daemon/request/conversations.list", body={"junk": 1})
    assert (status, body["code"]) == (400, "INVALID_ARGUMENT")


def test_http_openapi_omits_host_only_identity_fields(http_server) -> None:
    status, _, document = http_server.request("GET", "/openapi.json")
    assert status == 200, document
    cases = {
        "orgfs.export": {"from", "actor", "sessionRef"},
        "orgfs.import": {"from", "actor", "sessionRef"},
        "conversations.list": {"from", "actor", "sessionRef", "onBehalfOf"},
        "conversations.history": {"from", "actor", "sessionRef", "onBehalfOf"},
    }
    for name, forbidden in cases.items():
        schema = document["paths"][f"/daemon/request/{name}"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        assert forbidden.isdisjoint(schema.get("properties", {})), name
        assert forbidden.isdisjoint(schema.get("required", [])), name


def test_raw_framing_and_size_refusals(http_server) -> None:
    token = http_server.token
    auth = f"Authorization: Bearer {token}\r\n"
    status, _ = http_server.raw(
        (
            "POST /daemon/request/conversations.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + "Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n0\r\n\r\n"
        ).encode()
    )
    assert status == 400

    status, _ = http_server.raw(
        (
            "POST /daemon/request/conversations.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
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
        ("POST /daemon/request/conversations.list HTTP/1.1\r\nHost: 127.0.0.1\r\n" + auth + "\r\n").encode()
    )
    assert status == 411

    long_path = "/daemon/request/" + "a" * 600
    status, _ = http_server.raw(
        ("GET " + long_path + " HTTP/1.1\r\nHost: 127.0.0.1\r\n" + auth + "\r\n").encode()
    )
    assert status == 414

    status, _, body = http_server.request(
        "POST", "/daemon/request/progress.list", body={"from": "x" * (262144 + 10)}
    )
    assert (status, body["code"]) == (413, "HTTP_BODY_TOO_LARGE")


def test_huge_declared_body_is_refused_without_waiting_for_content(http_server) -> None:
    status, raw = http_server.raw(
        (
            "POST /daemon/request/conversations.list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
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
        "OPTIONS", "/daemon/request/conversations.list", headers={"Origin": origin}
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
        "/daemon/request/progress.list",
        body={"actor": "agent:h01-owner:other-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"},
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

    status, _, payload = http_server.request("POST", "/daemon/request/conversations.list", body={})
    assert status == 200 and payload["dispatch"] == "dispatched"


def test_daemon_down_is_not_dispatched(http_server, daemon) -> None:
    daemon.stop()
    status, _, ready = http_server.request("GET", "/readyz")
    assert status == 503
    assert ready["ready"] is False
    assert ready["code"] == "DAEMON_UNAVAILABLE"

    status, _, body = http_server.request("POST", "/daemon/request/conversations.list", body={})
    assert status == 503
    assert (body["code"], body["dispatch"]) == ("DAEMON_UNAVAILABLE", "not-dispatched")
    time.sleep(0.1)


def test_openapi_is_protected_and_reflects_the_live_catalog(http_server) -> None:
    status, _, body = http_server.request("GET", "/openapi.json", token="wrong")
    assert (status, body["code"]) == (401, "HTTP_UNAUTHORIZED")

    status, _, document = http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    assert "/daemon/request/conversations.list" in paths
    assert "/daemon/request/conversations.history" in paths
    assert "/daemon/request/<method>" not in paths
    assert "/docs" not in paths
    assert "422" not in paths["/daemon/request/conversations.list"]["post"]["responses"]

    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    body_schema = paths["/daemon/request/conversations.list"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema == {"$ref": "#/components/schemas/params.conversations.list"}
    components = document["components"]["schemas"]
    assert components["params.conversations.list"] == methods["conversations.list"]["paramsSchema"]
    success_schema = paths["/daemon/request/conversations.list"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert success_schema == {"$ref": "#/components/schemas/success.conversations.list"}
    assert components["success.conversations.list"]["properties"]["result"] == {
        "$ref": "#/components/schemas/result.conversations.list"
    }
    assert components["result.conversations.list"] == methods["conversations.list"]["resultSchema"]

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


def test_orgfs_spaces_rejects_mcp_session_identity_over_http(http_server) -> None:
    for field in ("actor", "sessionRef"):
        status, _, payload = http_server.request(
            "POST",
            "/daemon/request/orgfs.spaces",
            body={field: ACTOR},
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        ), field

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.spaces",
        body={"actor": ACTOR, "sessionRef": "mcp-session"},
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )


def test_orgfs_write_rejects_mcp_session_identity_over_http(http_server) -> None:
    for field in ("actor", "sessionRef"):
        status, _, payload = http_server.request(
            "POST",
            "/daemon/request/orgfs.write",
            body={
                field: ACTOR,
                "spaceId": "space-x",
                "path": "docs/a.md",
                "text": "hi",
            },
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        ), field


def test_orgfs_write_create_only_never_clears_existing_text(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "create-only-team")
    _write_text(isolation, space_id, "keep.md", "keep")

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.write",
        body={
            "spaceId": space_id,
            "path": "keep.md",
            "text": "replace",
            "createOnly": True,
        },
    )
    assert status in (409, 500), payload
    assert payload["code"] == "stale-write"
    assert payload["dispatch"] == "dispatched"

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.read",
        body={"spaceId": space_id, "path": "keep.md"},
    )
    assert status == 200, payload
    assert payload["result"]["text"] == "keep"

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.write",
        body={
            "spaceId": space_id,
            "path": "new.md",
            "text": "fresh",
            "createOnly": True,
        },
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["path"] == "new.md"


def test_orgfs_write_create_only_is_rejected_for_binary_writes(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "create-only-binary-team")
    original = base64.b64encode(b"keep bytes").decode("ascii")
    daemon_request_once(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": "keep.bin",
            "contentB64": original,
        },
        socket_path=isolation.socket_path,
        timeout=15,
    )

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.write",
        body={
            "spaceId": space_id,
            "path": "keep.bin",
            "contentB64": base64.b64encode(b"replaced").decode("ascii"),
            "createOnly": True,
        },
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.read",
        body={"spaceId": space_id, "path": "keep.bin"},
    )
    assert status == 200, payload
    assert payload["result"]["contentB64"] == original


def test_orgfs_write_rejects_text_and_binary_payloads_together(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "dual-payload-team")
    original = base64.b64encode(b"keep bytes").decode("ascii")
    daemon_request_once(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": "keep.bin",
            "contentB64": original,
        },
        socket_path=isolation.socket_path,
        timeout=15,
    )

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.write",
        body={
            "spaceId": space_id,
            "path": "keep.bin",
            "text": "replace",
            "contentB64": base64.b64encode(b"replaced").decode("ascii"),
            "createOnly": True,
        },
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.read",
        body={"spaceId": space_id, "path": "keep.bin"},
    )
    assert status == 200, payload
    assert payload["result"]["contentB64"] == original


def test_orgfs_restore_uses_the_trash_node_version(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "restore-team")
    _write_text(isolation, space_id, "trash.md", "body")
    daemon_request_once(
        "orgfs.remove",
        {"spaceId": space_id, "path": "trash.md"},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    trashed = daemon_request_once(
        "orgfs.trash",
        {"spaceId": space_id},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    item = next(node for node in trashed["nodes"] if node["path"] == "trash.md")
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.restore",
        body={
            "spaceId": space_id,
            "node": f"id:{item['node_id']}",
            "version": item["version"],
            "recursive": item["kind"] == "dir",
        },
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["deleted"] is False
    assert payload["result"]["path"] == "trash.md"


def test_orgfs_purge_plan_accepts_a_deleted_doc_target(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "purge-team")
    _write_text(isolation, space_id, "purge.md", "body")
    daemon_request_once(
        "orgfs.remove",
        {"spaceId": space_id, "path": "purge.md"},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    trashed = daemon_request_once(
        "orgfs.trash",
        {"spaceId": space_id},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    item = next(node for node in trashed["nodes"] if node["path"] == "purge.md")
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.purge_plan",
        body={
            "spaceId": space_id,
            "targets": [
                {
                    "kind": "doc-range",
                    "node": f"id:{item['node_id']}",
                    "from": item["version"],
                    "to": item["version"],
                }
            ],
        },
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["plan_id"]
    assert payload["result"]["docs"] or payload["result"]["blobs"]


def test_orgfs_export_and_import_use_the_trusted_local_owner(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "local-file-team")
    written = _write_text(isolation, space_id, "note.md", "exported")

    destination = isolation.root / "exported.md"
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.export",
        body={
            "spaceId": space_id,
            "node": f"id:{written['node_id']}",
            "destination": str(destination),
            "overwrite": True,
        },
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert destination.read_text(encoding="utf-8") == "exported"

    source = isolation.root / "imported.md"
    source.write_text("imported", encoding="utf-8")
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.import",
        body={
            "spaceId": space_id,
            "path": "imported.md",
            "source": str(source),
        },
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["path"] == "imported.md"

    # A browser may not select the owner identity itself; the host injects it.
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.export",
        body={
            "spaceId": space_id,
            "node": f"id:{written['node_id']}",
            "destination": str(destination),
            "from": "user:h01-owner",
        },
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )


def test_orgfs_spaces_accepts_the_mcp_session_envelope_over_real_ipc(
    daemon, isolation: Isolation
) -> None:
    # ``daemon`` owns the isolated AF_UNIX socket; the direct call is the same
    # AF_UNIX -> MethodRegistry -> _handle_orgfs path MCP's proxy uses.
    result = daemon_request_once(
        "orgfs.spaces",
        {
            "actor": "agent:h01-owner:h01-node:mcp-worker",
            "sessionRef": "mcp-session",
        },
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert result == {"spaces": []}


def test_orgfs_purge_status_accepts_a_real_plan_over_ipc_and_http(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "purge-team")
    written = _write_text(isolation, space_id, "note.md", "secret")
    plan = daemon_request_once(
        "orgfs.purge_plan",
        {
            "spaceId": space_id,
            "targets": [
                {
                    "kind": "doc-range",
                    "node": f"id:{written['node_id']}",
                    "from": "all",
                    "to": "all",
                }
            ],
        },
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert isinstance(plan, dict), plan
    # ``_orgfs_json`` uses dataclass ``asdict``; PurgePlan keeps plan_id.
    plan_id = plan["plan_id"]
    assert isinstance(plan_id, str) and plan_id

    direct = daemon_request_once(
        "orgfs.purge_status",
        {"spaceId": space_id, "planId": plan_id},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert direct["plan_id"] == plan_id
    assert direct["acknowledged"] == []
    assert isinstance(direct["pending"], list)

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/orgfs.purge_status",
        body={"spaceId": space_id, "planId": plan_id},
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["result"] == direct


def test_message_http_rejects_session_impersonation_before_dispatch(http_server) -> None:
    for method, required in (
        ("message.history", {"conversationId": "private"}),
        ("message.query", {"view": "inbox"}),
        ("message.send.lookup", {"target": "agent:h01-owner:h01-node:worker", "text": "x",
                                 "conversationId": "private", "idempotencyKey": "lookup"}),
    ):
        for field, value in (("sessionRef", "forged"), ("onBehalfOf", "agent:h01-owner:h01-node:worker")):
            if method == "message.send.lookup" and field == "onBehalfOf":
                continue
            status, _, body = http_server.request(
                "POST", f"/daemon/request/{method}", body={**required, field: value},
            )
            assert (status, body["code"], body["dispatch"]) == (
                400, "INVALID_ARGUMENT", "not-dispatched",
            ), (method, field, body)


def test_http_owner_cannot_read_nonempty_private_agent_conversation(http_server, isolation) -> None:
    created = daemon_request_once(
        "agent.create", {"name": "private-reader"}, socket_path=isolation.socket_path, timeout=10,
    )
    agent = created.get("agent") if isinstance(created, dict) else None
    actor = agent["uri"] if isinstance(agent, dict) else created["uri"]
    conversation_id = "private-channel-agent-conversation"
    daemon_request_once(
        "message.send",
        {"from": "channel:lark:main", "to": [actor], "message": "private marker", "summary": "private marker",
         "conversationId": conversation_id},
        socket_path=isolation.socket_path, timeout=10,
    )
    private = daemon_request_once(
        "message.history", {"actor": f"user:{OWNER}", "onBehalfOf": actor,
                            "conversationId": conversation_id},
        socket_path=isolation.socket_path, timeout=10,
    )
    assert any(row["message"] == "private marker" for row in private["entries"])
    for method, params in (
        ("message.history", {"conversationId": conversation_id}),
        ("message.query", {"view": "inbox"}),
    ):
        status, _, body = http_server.request("POST", f"/daemon/request/{method}", body=params)
        assert status == 200, (method, body)
        assert "private marker" not in json.dumps(body)
        status, _, body = http_server.request(
            "POST", f"/daemon/request/{method}", body={**params, "onBehalfOf": actor},
        )
        assert (status, body["code"], body["dispatch"]) == (
            400, "INVALID_ARGUMENT", "not-dispatched",
        )
