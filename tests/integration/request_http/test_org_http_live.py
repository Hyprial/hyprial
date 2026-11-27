"""Real isolated org.* over the authenticated HTTP entry.

Decision record (org-members batch 1 fix 2, http node, 2026-11-02)
- The org data is created through the real daemon AF_UNIX boundary with
  ``orgfs.create``; no test fixture or in-process facade is the authority.
- The ambiguous-space case is raised by the real org service when two OrgFS
  spaces carry the same directory name, so the assertion covers the complete
  AF_UNIX -> MethodRegistry -> HTTP envelope path, including declared data.
- The post-dispatch timeout case injects a lost reply only after the real
  daemon call completed.  It proves the HTTP write path keeps ``dispatch``
  unknown and does not retry; a real network partition remains UNKNOWN.

Decision record (org lifecycle batch 2, 2026-11-02)
- ``org.create``/``org.invite``/``org.join`` run against a real daemon process
  over AF_UNIX with real OrgFS state.  Creating an org needs a device record
  and a Tailnet address, so the daemon is launched through the documented
  ``tests.support.tailnet_daemon`` membership fixture (an explicit local
  authority double).  That proves the request path, not a real tailnet mesh;
  the two-daemon E2E chain stays UNKNOWN in this batch.
- ``org.join`` success needs a redeemable invite and an online holder; neither
  exists inside one isolated daemon, so the live cases pin parameter rejection
  and the real pre-redemption domain errors instead of faking a join.

Decision record (OrgFS package D4b, 2026-11-25)
- ``orgfs.restore`` lost its ``version`` (design §1.1), so the domain error
  that keeps its data through the registry and HTTP is now the restore of a
  node removed with ``permanent: true``: ``unknown-doc`` naming the node.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import RequestPortError, daemon_request_once, ipc_errors
from hyprial.shell.impl.request_http.config import load_config
from hyprial.shell.impl.request_http.server import RequestHttpServer

from tests.integration.request_http.conftest import (
    ACTOR,
    DaemonProcess,
    HttpProcess,
    Isolation,
    running_http,
    write_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _create_space(isolation: Isolation, name: str) -> str:
    created = daemon_request_once(
        "orgfs.create",
        {"name": name},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert isinstance(created, dict), created
    space_id = created.get("space_id")
    assert isinstance(space_id, str) and space_id
    return space_id


def _write_text(
    isolation: Isolation, space_id: str, path: str, text: str
) -> dict[str, Any]:
    written = daemon_request_once(
        "orgfs.write",
        {"spaceId": space_id, "path": path, "text": text},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert isinstance(written, dict), written
    return written


def _move_node(
    isolation: Isolation, space_id: str, node_id: str, destination: str
) -> None:
    moved = daemon_request_once(
        "orgfs.move",
        {
            "spaceId": space_id,
            "sourcePath": f"id:{node_id}",
            "destinationPath": destination,
        },
        socket_path=isolation.socket_path,
        timeout=15,
    )
    assert isinstance(moved, dict), moved


def _set_org_fetch_source(isolation: Isolation, source: str) -> None:
    path = isolation.home / "settings.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["org"] = {"fetchSource": source}
    path.write_text(json.dumps(document), encoding="utf-8")


def _in_process_http(server: RequestHttpServer, isolation: Isolation) -> HttpProcess:
    return HttpProcess(
        isolation,
        None,  # type: ignore[arg-type]
        server.endpoint,
        server.token,
        {},
        isolation.root / "in-process-http.log",
    )


@pytest.fixture
def org_daemon(isolation: Isolation):
    """A real daemon whose only double is the documented membership port."""

    process = DaemonProcess(
        isolation,
        pythonpath=REPO_ROOT,
        module="tests.support.tailnet_daemon",
    )
    process.start()
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
def org_http_server(isolation: Isolation, org_daemon: DaemonProcess):
    with running_http(isolation) as http:
        yield http


def _invite_marker(value: str) -> str:
    """A non-reversible marker: invite links are bearer credentials."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _create_org(http: HttpProcess, org: str) -> dict[str, Any]:
    status, _, payload = http.request(
        "POST", "/daemon/request/org.create", body={"org": org}
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    result = payload["result"]
    assert isinstance(result, dict), payload
    return result


def test_org_list_success_is_authenticated_and_reads_isolated_orgfs(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "group-acme")

    status, _, body = http_server.request(
        "POST", "/daemon/request/org.list", body={}, token="wrong"
    )
    assert (status, body["code"], body["dispatch"]) == (
        401,
        "HTTP_UNAUTHORIZED",
        "not-dispatched",
    )

    status, headers, payload = http_server.request(
        "POST", "/daemon/request/org.list", body={}
    )
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["method"] == "org.list"
    assert payload["dispatch"] == "dispatched"
    assert payload["result"]["leftOrgs"] == []
    rows = payload["result"]["orgs"]
    assert len(rows) == 1
    assert rows[0] == {
        "org": "group-acme",
        "spaceId": space_id,
        "role": "owner",
        "devices": 0,
        "state": "partial",
        "authorityDevice": None,
        "kind": "work",
        "current": False,
    }
    assert headers["X-Hyprial-Request-Id"].startswith("http-")


def test_org_list_ambiguous_space_keeps_code_and_data_through_ipc_and_http(
    http_server, isolation: Isolation
) -> None:
    _create_space(isolation, "group-dup")
    _create_space(isolation, "group-dup")

    status, _, payload = http_server.request(
        "POST", "/daemon/request/org.list", body={}
    )

    assert status == 500, payload
    assert payload["ok"] is False
    assert payload["method"] == "org.list"
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "ambiguous-space"
    assert payload["data"] == {"space": "group-dup", "count": 2}
    assert "group-dup" not in json.dumps(payload["message"])


def test_org_list_rejects_non_object_bodies_and_identity_shaped_extras(
    http_server,
) -> None:
    for body in ([], "list", 0):
        status, _, payload = http_server.request(
            "POST", "/daemon/request/org.list", body=body
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        )
    for field in ("actor", "from", "caller"):
        status, _, payload = http_server.request(
            "POST", "/daemon/request/org.list", body={field: ACTOR}
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        )




def test_org_fetch_rejects_non_object_bodies_before_dispatch(http_server) -> None:
    for body in ([], "fetch", 0):
        status, _, payload = http_server.request(
            "POST", "/daemon/request/org.fetch", body=body
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        )


@pytest.mark.parametrize("timeout", [0, -1, 31, True, "2"])
def test_org_fetch_rejects_invalid_timeout_values_before_dispatch(
    http_server, timeout: Any
) -> None:
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/org.fetch",
        body={"timeoutSeconds": timeout},
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )


def test_org_fetch_rejects_identity_shaped_extras_before_dispatch(http_server) -> None:
    for field in ("actor", "caller"):
        status, _, payload = http_server.request(
            "POST",
            "/daemon/request/org.fetch",
            body={field: ACTOR},
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        )

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/org.fetch",
        body={"from": ACTOR, "actor": ACTOR},
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )


def test_org_fetch_orgfs_success_uses_the_declared_write_route(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "context")
    _set_org_fetch_source(isolation, "orgfs")

    status, _, payload = http_server.request(
        "POST", "/daemon/request/org.fetch", body={"timeoutSeconds": 2}
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    result = payload["result"]
    assert result["requestedSources"] == []
    assert result["responseCount"] == 0
    assert result["receivedCount"] == 0
    assert result["rejectedCount"] == 0
    assert result["candidates"] == []
    assert result["source"] == "orgfs"
    assert result["spaceId"] == space_id

    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    assert methods["org.fetch"]["mutates"] is True
    assert methods["org.fetch"]["retry"] == "never"


def test_org_fetch_candidate_enumeration_surfaces_invalid_argument(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "context")
    _set_org_fetch_source(isolation, "orgfs")
    _write_text(isolation, space_id, "candidates", "# not a candidates directory\n")

    with pytest.raises(RequestPortError) as direct:
        daemon_request_once(
            "org.fetch",
            {"timeoutSeconds": 2},
            socket_path=isolation.socket_path,
            timeout=15,
        )
    assert direct.value.code == "ORG_PATH_SHAPE_INVALID"
    assert direct.value.data is None

    status, _, payload = http_server.request(
        "POST", "/daemon/request/org.fetch", body={"timeoutSeconds": 2}
    )
    assert status == 500, payload
    assert payload["ok"] is False
    assert payload["method"] == "org.fetch"
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "ORG_PATH_SHAPE_INVALID"
    assert "data" not in payload


def test_orgfs_domain_error_keeps_data_through_registry_and_http(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "error-data-team")
    written = _write_text(isolation, space_id, "note.md", "body\n")
    daemon_request_once(
        "orgfs.remove",
        {"spaceId": space_id, "path": "note.md", "permanent": True},
        socket_path=isolation.socket_path,
        timeout=15,
    )
    params = {"spaceId": space_id, "node": f"id:{written['node_id']}"}

    with pytest.raises(RequestPortError) as direct:
        daemon_request_once(
            "orgfs.restore",
            params,
            socket_path=isolation.socket_path,
            timeout=15,
        )
    assert direct.value.code == "unknown-doc"
    assert direct.value.data == {"node": f"id:{written['node_id']}"}

    status, _, payload = http_server.request(
        "POST", "/daemon/request/orgfs.restore", body=params
    )
    assert status >= 400, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == direct.value.code
    assert payload["data"] == direct.value.data


def test_org_fetch_ambiguous_candidate_path_keeps_the_domain_code(
    http_server, isolation: Isolation
) -> None:
    space_id = _create_space(isolation, "context")
    _set_org_fetch_source(isolation, "orgfs")
    first = _write_text(isolation, space_id, "candidates", "# first\n")
    second = _write_text(isolation, space_id, "other", "# second\n")
    _move_node(isolation, space_id, str(second["node_id"]), "candidates")

    with pytest.raises(RequestPortError) as direct:
        daemon_request_once(
            "org.fetch",
            {"timeoutSeconds": 2},
            socket_path=isolation.socket_path,
            timeout=15,
        )
    assert direct.value.code == "ambiguous-path"
    assert direct.value.data is None

    status, _, payload = http_server.request(
        "POST", "/daemon/request/org.fetch", body={"timeoutSeconds": 2}
    )
    assert status == 500, payload
    assert payload["method"] == "org.fetch"
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "ambiguous-path"
    assert "data" not in payload
    assert str(first["node_id"]) not in json.dumps(payload)


def test_org_fetch_reports_the_reachable_not_joined_state(
    http_server, isolation: Isolation
) -> None:
    _set_org_fetch_source(isolation, "orgfs")

    status, _, payload = http_server.request(
        "POST", "/daemon/request/org.fetch", body={"timeoutSeconds": 2}
    )

    assert status == 500, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == ipc_errors.ORG_SPACE_NOT_JOINED
    assert payload["data"] == {"hint": "hyprial org fs space join <spaceId>"}


def test_org_fetch_post_dispatch_timeout_is_unknown_and_never_retried(
    isolation: Isolation, daemon
) -> None:
    calls: list[dict[str, Any]] = []
    real_request = daemon_request_once

    def request_once(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        if method != "org.fetch":
            return real_request(method, params, **kwargs)
        calls.append(dict(params))
        real_request(method, params, **kwargs)
        raise ipc_errors.IpcTimeoutError("injected lost reply after dispatch")

    server = RequestHttpServer(
        load_config(write_config(isolation)),
        request_once=request_once,
    )
    server.start()
    try:
        http = _in_process_http(server, isolation)
        status, _, payload = http.request(
            "POST",
            "/daemon/request/org.fetch",
            body={"timeoutSeconds": 2},
            timeout=20,
        )
        assert status == 504, payload
        assert payload["code"] == ipc_errors.IPC_TIMEOUT
        assert payload["dispatch"] == "unknown"
        assert len(calls) == 1
    finally:
        server.shutdown()


def test_openapi_exposes_both_org_operations_and_declared_errors(http_server) -> None:
    status, _, document = http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    assert "/daemon/request/org.list" in paths
    assert "/daemon/request/org.fetch" in paths
    read = paths["/daemon/request/org.list"]["post"]
    write = paths["/daemon/request/org.fetch"]["post"]
    assert read["x-hyprial-call-profile"] == "read"
    assert write["x-hyprial-call-profile"] == "write"
    assert write["x-hyprial-mutates"] is True
    assert write["x-hyprial-retry"] == "never"
    for operation in (read, write):
        codes = {
            variant["properties"]["code"]["const"]
            for variant in operation["responses"]["default"]["content"]["application/json"][
                "schema"
            ]["oneOf"]
        }
        assert {"ambiguous-space", "unknown-space", "unavailable"} <= codes
    write_variants = {
        variant["properties"]["code"]["const"]: variant
        for variant in write["responses"]["default"]["content"]["application/json"][
            "schema"
        ]["oneOf"]
    }
    for code in ("ORG_PATH_SHAPE_INVALID", "ambiguous-path"):
        assert write_variants[code]["properties"]["data"] == {}
    assert "invalid-argument" not in write_variants


def test_org_create_success_and_resume_keep_the_real_publish_flags(
    org_http_server: HttpProcess,
) -> None:
    status, headers, payload = org_http_server.request(
        "POST", "/daemon/request/org.create", body={"org": "group-http-org"}
    )
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["method"] == "org.create"
    assert payload["dispatch"] == "dispatched"
    assert headers["X-Hyprial-Request-Id"].startswith("http-")

    result = payload["result"]
    assert result["org"] == "group-http-org"
    assert result["existed"] is False
    assert result["aclExisted"] is False
    assert result["casdoorGroup"] == "not-configured"
    assert (result["published"], result["devicePublished"]) == (True, True)
    assert result["space"] == {
        "spaceId": result["spaceId"],
        "name": "group-http-org",
        "owner": "user:h01-owner",
        "createdAt": result["space"]["createdAt"],
    }
    assert result["aclSpace"] == {
        "spaceId": result["aclSpaceId"],
        "name": "group-http-org-acl",
        "owner": "user:h01-owner",
        "createdAt": result["aclSpace"]["createdAt"],
    }
    assert result["spaceId"] != result["aclSpaceId"]

    status, _, again = org_http_server.request(
        "POST", "/daemon/request/org.create", body={"org": "group-http-org"}
    )
    assert status == 200, again
    resumed = again["result"]
    assert (resumed["existed"], resumed["aclExisted"]) == (True, True)
    assert (resumed["published"], resumed["devicePublished"]) == (True, True)
    assert resumed["spaceId"] == result["spaceId"]

    status, _, listed = org_http_server.request(
        "POST", "/daemon/request/org.list", body={}
    )
    assert status == 200, listed
    assert [row["org"] for row in listed["result"]["orgs"]] == ["group-http-org"]


def test_orgfs_ls_recursive_returns_nested_paths_over_authenticated_http(
    org_http_server: HttpProcess,
) -> None:
    created = _create_org(org_http_server, "group-http-nested-files")
    space_id = created["spaceId"]

    for path in ("nested-test", "nested-test/child"):
        status, _, payload = org_http_server.request(
            "POST",
            "/daemon/request/orgfs.mkdir",
            body={"spaceId": space_id, "path": path},
        )
        assert status == 200, payload
    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/orgfs.write",
        body={
            "spaceId": space_id,
            "path": "nested-test/child/evidence.md",
            "text": "nested",
        },
    )
    assert status == 200, payload

    status, _, direct = org_http_server.request(
        "POST",
        "/daemon/request/orgfs.ls",
        body={"spaceId": space_id, "path": "nested-test"},
    )
    assert status == 200, direct
    assert [node["path"] for node in direct["result"]["nodes"]] == [
        "nested-test/child"
    ]

    status, _, recursive = org_http_server.request(
        "POST",
        "/daemon/request/orgfs.ls",
        body={"spaceId": space_id, "path": "nested-test", "recursive": True},
    )
    assert status == 200, recursive
    assert [(node["path"], node["kind"]) for node in recursive["result"]["nodes"]] == [
        ("nested-test/child", "dir"),
        ("nested-test/child/evidence.md", "doc"),
    ]


def test_org_create_requires_the_bearer_token_and_rejects_before_dispatch(
    org_http_server: HttpProcess,
) -> None:
    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.create",
        body={"org": "group-http-org"},
        token="wrong",
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        401,
        "HTTP_UNAUTHORIZED",
        "not-dispatched",
    )
    for body in ({}, [], {"org": ""}, {"org": "group-x", "extra": True}):
        status, _, payload = org_http_server.request(
            "POST", "/daemon/request/org.create", body=body
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        ), body
    for field in ("actor", "from", "caller"):
        status, _, payload = org_http_server.request(
            "POST",
            "/daemon/request/org.create",
            body={"org": "group-http-org", field: ACTOR},
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        )


def test_org_create_domain_error_keeps_code_and_data_through_http(
    org_http_server: HttpProcess, isolation: Isolation
) -> None:
    _create_space(isolation, "group-dup")
    _create_space(isolation, "group-dup")

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.create", body={"org": "group-dup"}
    )

    assert status == 500, payload
    assert payload["ok"] is False
    assert payload["method"] == "org.create"
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "ambiguous-space"
    assert payload["data"] == {"space": "group-dup", "count": 2}


def test_org_invite_success_returns_the_three_real_link_forms(
    org_http_server: HttpProcess,
) -> None:
    created = _create_org(org_http_server, "group-http-invite")

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.invite",
        body={"org": "group-http-invite", "user": "user:bob@example"},
    )

    assert status == 200, {
        "code": payload.get("code"),
        "dispatch": payload.get("dispatch"),
    }
    assert payload["dispatch"] == "dispatched"
    result = payload["result"]
    assert result["org"] == "group-http-invite"
    assert result["spaceId"] == created["spaceId"]
    assert result["aclSpaceId"] == created["aclSpaceId"]
    assert result["user"] == "user:bob@example"

    link = result["link"]
    deep_link = result["deepLink"]
    command = result["command"]
    token = result["invite"]
    link_shaped = link.startswith("https://") and "/join#v2." in link
    assert link_shaped, {
        "invite": "link-shape-mismatch",
        "sha256": _invite_marker(link),
    }
    deep_link_shaped = deep_link.startswith("hyprial://join#v2.")
    assert deep_link_shaped, {
        "invite": "deep-link-shape-mismatch",
        "sha256": _invite_marker(deep_link),
    }
    command_matches = command == f"hyprial org join '{link}'"
    assert command_matches, {
        "invite": "command-shape-mismatch",
        "sha256": _invite_marker(command),
    }
    token_shaped = token.startswith("hyprial-invite:v2:")
    assert token_shaped, {
        "invite": "token-shape-mismatch",
        "sha256": _invite_marker(token),
    }
    assert isinstance(result["expiresAt"], str) and result["expiresAt"]
    assert result["member"] == {
        "spaceId": created["spaceId"],
        "user": "user:bob@example",
        "mode": "rw",
    }
    assert result["aclMember"] == {
        "spaceId": created["aclSpaceId"],
        "user": "user:bob@example",
        "mode": "ro",
    }


def test_org_invite_parameter_rejection_and_fail_closed_policy(
    org_http_server: HttpProcess,
) -> None:
    _create_org(org_http_server, "group-http-invite")

    for body in ({}, {"org": "group-http-invite"}, {"org": "group-http-invite", "user": ""}):
        status, _, payload = org_http_server.request(
            "POST", "/daemon/request/org.invite", body=body
        )
        assert (status, payload["code"], payload["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        ), body

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.invite",
        body={"org": "group-missing", "user": "user:bob@example"},
    )
    assert status == 500, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "ORG_ACTION_NOT_AUTHORIZED"
    assert "data" not in payload


def test_org_join_parameter_rejection_and_real_decode_failures(
    org_http_server: HttpProcess,
) -> None:
    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.join", body={}
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.join",
        body={"link": "hyprial-invite:v1:not-base64!!!"},
    )
    assert status == 500, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "INVITE_VERSION_UNSUPPORTED"
    assert "data" not in payload

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.join",
        body={"link": "https://invite.example.org/wrong#v2.abc"},
    )
    assert status == 500, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["code"] == "INVITE_MALFORMED"
    assert "data" not in payload
    assert "invite.example.org" not in json.dumps(payload["message"])


def test_org_join_errors_do_not_echo_the_invite_payload(
    org_http_server: HttpProcess,
) -> None:
    payload = "v2.synthetic-not-a-real-token"
    for link in (
        f"https://invite.example.org/wrong#{payload}",
        f"not-a-link {payload}",
    ):
        status, _, body = org_http_server.request(
            "POST", "/daemon/request/org.join", body={"link": link}
        )
        assert status == 500
        assert body["dispatch"] == "dispatched"
        assert body["code"] == "INVITE_MALFORMED"
        echoed = payload in json.dumps(body)
        assert echoed is False, {
            "invite": "payload-echo",
            "sha256": _invite_marker(payload),
        }


def test_org_invite_post_dispatch_timeout_is_unknown_and_never_retried(
    isolation: Isolation, org_daemon: DaemonProcess
) -> None:
    created = daemon_request_once(
        "org.create",
        {"org": "group-http-timeout"},
        socket_path=isolation.socket_path,
        timeout=30,
    )
    assert created["org"] == "group-http-timeout"

    calls: list[dict[str, Any]] = []
    real_request = daemon_request_once

    def request_once(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        if method != "org.invite":
            return real_request(method, params, **kwargs)
        calls.append(dict(params))
        real_request(method, params, **kwargs)
        raise ipc_errors.IpcTimeoutError("injected lost reply after dispatch")

    server = RequestHttpServer(
        load_config(write_config(isolation)),
        request_once=request_once,
    )
    server.start()
    try:
        http = _in_process_http(server, isolation)
        status, _, payload = http.request(
            "POST",
            "/daemon/request/org.invite",
            body={"org": "group-http-timeout", "user": "user:bob@example"},
            timeout=20,
        )
        assert status == 504, payload
        assert payload["code"] == ipc_errors.IPC_TIMEOUT
        assert payload["dispatch"] == "unknown"
        assert len(calls) == 1
    finally:
        server.shutdown()


def test_openapi_exposes_the_lifecycle_operations_as_writes(http_server) -> None:
    status, _, document = http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    for name, code in (
        ("org.create", "ambiguous-space"),
        ("org.invite", "ORG_ACTION_NOT_AUTHORIZED"),
        ("org.join", "INVITE_MALFORMED"),
    ):
        operation = paths[f"/daemon/request/{name}"]["post"]
        assert operation["x-hyprial-call-profile"] == "write"
        assert operation["x-hyprial-mutates"] is True
        assert operation["x-hyprial-retry"] == "never"
        codes = {
            variant["properties"]["code"]["const"]
            for variant in operation["responses"]["default"]["content"][
                "application/json"
            ]["schema"]["oneOf"]
        }
        assert code in codes


def test_batch_one_member_lifecycle_methods_are_http_routed(
    org_http_server: HttpProcess,
) -> None:
    """execute/leave/remove/delete have real HTTP routes and real outcomes."""

    org = "group-removal-http"
    created = _create_org(org_http_server, org)

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.remove",
        body={"org": org, "user": "user:bob"},
    )
    assert status == 200, payload
    removal = payload["result"]
    assert removal["org"] == org
    assert removal["user"] == "user:bob"
    assert removal["phase"] == "pending-confirmation"
    assert removal["spaceId"] == created["spaceId"]

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.leave", body={"org": org}
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        500,
        "LAST_AUTHORITY_REFUSED",
        "dispatched",
    )

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.delete", body={"org": org}
    )
    assert status == 200, payload
    deletion = payload["result"]
    assert deletion["org"] == org
    assert deletion["spaceId"] == created["spaceId"]
    assert deletion["phase"] == "pending-confirmation"
    assert deletion["confirmed"] is False
    assert deletion["irreversible"] is True
    assert deletion["orgfsMarked"] is False

    status, _, payload = org_http_server.request(
        "POST",
        "/daemon/request/org.delete",
        body={"org": org, "confirmed": True, "operationId": deletion["operationId"]},
    )
    assert status == 200, payload
    assert payload["result"]["phase"] == "complete"
    assert payload["result"]["operationId"] == deletion["operationId"]
    status, _, payload = org_http_server.request("POST", "/daemon/request/org.list", body={})
    assert status == 200, payload
    assert org not in {entry["org"] for entry in payload["result"]["orgs"]}

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.execute", body={"link": "not-an-invite"}
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        500,
        "INVITE_MALFORMED",
        "dispatched",
    )


def test_batch_one_openapi_profiles(org_http_server: HttpProcess) -> None:
    status, _, document = org_http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    for name in ("org.delete", "org.execute", "org.leave", "org.remove"):
        path = f"/daemon/request/{name}"
        assert path in paths, name
        operation = paths[path]["post"]
        assert operation["x-hyprial-call-profile"] == "write", name
        codes = {
            variant["properties"]["code"]["const"]
            for variant in operation["responses"]["default"]["content"][
                "application/json"
            ]["schema"]["oneOf"]
        }
        assert "INVALID_ARGUMENT" in codes, name


def test_batch_two_network_repair_and_publish_are_http_routed(
    org_http_server: HttpProcess,
) -> None:
    """network/repair/publish answer over the real isolated HTTP path."""

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.network", body={}
    )
    assert status == 200, payload
    network = payload["result"]
    assert network["orgs"] == []
    assert network["unavailableOrgs"] == []
    assert set(network["paneStatus"]) == {"directory", "mappedPeers", "sidecar"}
    assert network["self"]["deviceId"]

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.repair", body={}
    )
    assert status == 200, payload
    repair = payload["result"]
    assert repair["apply"] is False
    assert repair["candidates"] == []
    assert repair["repaired"] == []

    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.publish", body={}
    )
    assert status == 200, payload
    publish = payload["result"]
    assert isinstance(publish["published"], bool)
    assert isinstance(publish["orgfsPublished"], bool)

    org = "group-network-http"
    _create_org(org_http_server, org)
    status, _, payload = org_http_server.request(
        "POST", "/daemon/request/org.network", body={"org": org}
    )
    assert status == 200, payload
    assert org in payload["result"]["orgs"]
    assert org in payload["result"]["directory"]


def test_batch_two_openapi_profiles(org_http_server: HttpProcess) -> None:
    status, _, document = org_http_server.request("GET", "/openapi.json")
    assert status == 200
    paths = document["paths"]
    network = paths["/daemon/request/org.network"]["post"]
    assert network["x-hyprial-call-profile"] == "read"
    assert network["x-hyprial-mutates"] is False
    assert network["x-hyprial-retry"] == "safe"
    for name, code in (
        ("org.repair", "CASDOOR_GROUP_UNAVAILABLE"),
        ("org.publish", ipc_errors.DAEMON_NOT_READY),
    ):
        operation = paths[f"/daemon/request/{name}"]["post"]
        assert operation["x-hyprial-call-profile"] == "write", name
        assert operation["x-hyprial-mutates"] is True, name
        assert operation["x-hyprial-retry"] == "never", name
        codes = {
            variant["properties"]["code"]["const"]
            for variant in operation["responses"]["default"]["content"][
                "application/json"
            ]["schema"]["oneOf"]
        }
        assert code in codes, name
