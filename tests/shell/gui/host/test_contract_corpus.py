# Decision record (GUI session host, PR 2b):
# - The corpus contract/gui-session/gui-session.v1.json is the single source
#   the Node host, the client and this host are compared with. These tests read
#   it and compare the host's constants, and live responses of the ASGI app, to
#   it; none of its values is copied here.
# - Error codes are checked from source (ast) because the table is closed: every
#   literal (status, code) pair the host raises must be a pair the corpus lists,
#   and every corpus code must be reachable from the host source. The two codes
#   the static file layer answers outside /daemon/* are the only exemption.
# - Schema validation uses jsonschema (already a product dependency), Draft 2020-12.
"""The Python host equals the GUI session contract corpus."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui.host import app, config, envelope, session, upstream
from hyprial.shell.impl.gui.host.web import devtools
from tests.shell.gui.host.support import GRANT, FakeUpstream, catalog_payload, host_config, ready_payload, run_gateway

ROOT = Path(__file__).resolve().parents[4]
CORPUS: dict[str, Any] = json.loads((ROOT / "contract/gui-session/gui-session.v1.json").read_text())
HOST_SOURCE = ROOT / "src/hyprial/shell/impl/gui/host"
STATIC_ONLY = {(404, "NOT_FOUND")}


def _failure_pairs() -> tuple[set[tuple[int, str]], set[str]]:
    pairs: set[tuple[int, str]] = set()
    strings: set[str] = set()
    for path in HOST_SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", node.value):
                strings.add(node.value)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ipc_errors":
                strings.add(getattr(ipc_errors, node.attr))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HostFailure" and len(node.args) >= 2:
                status, code = node.args[0], node.args[1]
                if not (isinstance(status, ast.Constant) and isinstance(status.value, int)):
                    continue
                if isinstance(code, ast.Constant) and isinstance(code.value, str):
                    pairs.add((status.value, code.value))
                elif isinstance(code, ast.Attribute) and getattr(code.value, "id", None) == "ipc_errors":
                    pairs.add((status.value, getattr(ipc_errors, code.attr)))
    return pairs, strings


def _corpus_pairs() -> set[tuple[int, str]]:
    return {(status, row["code"]) for row in CORPUS["errors"] for status in row["status"]}


def test_the_cookie_names_attributes_and_path_equal_the_corpus() -> None:
    cookies = CORPUS["cookies"]
    assert session.CONTROL_COOKIE == cookies["control"]["name"]
    assert session.SESSION_COOKIE == cookies["session"]["name"]
    assert f"Path={session.COOKIE_PATH}" in cookies["attributes"]

    async def scenario(gateway: Any) -> list[str]:
        return (await gateway.open()).reply.headers.get_list("set-cookie")

    for header in run_gateway(scenario):
        attributes = {part.strip().split("=", 1)[0].lower() for part in header.split(";")[1:]}
        for required in cookies["attributes"]:
            assert required.split("=", 1)[0].lower() in attributes
        for forbidden in cookies["forbiddenAttributes"]:
            assert forbidden.lower() not in attributes
        assert "Path=/daemon" in header and "SameSite=Strict" in header
        max_age = int(re.search(r"Max-Age=(\d+)", header).group(1))
        assert max_age >= cookies["maxAgeMinimumSeconds"]


def test_the_limits_and_header_set_equal_the_corpus() -> None:
    limits = CORPUS["limits"]
    assert envelope.MAX_SESSION_BYTES == limits["sessionBodyBytes"]
    assert envelope.MAX_REQUEST_BYTES == limits["methodBodyBytes"]
    assert envelope.MAX_RESPONSE_BYTES == limits["upstreamResponseBytes"]
    assert config.UPSTREAM_REQUEST_SECONDS * 1000 == limits["upstreamTimeoutMilliseconds"]
    assert app.METHOD_NAME.pattern == limits["methodNamePattern"]
    assert dict(envelope.BASE_HEADERS) == CORPUS["headers"]["hostResponseFixed"]
    assert app.SESSION_CONTRACT_HEADER == CORPUS["versioning"]["requestHeader"]
    assert app.HEALTH_PATH == CORPUS["static"]["healthz"]["path"]


def test_the_defaults_and_ranges_equal_the_corpus() -> None:
    defaults = CORPUS["hostDefaults"]
    assert config.DEFAULT_LAUNCH_TTL_MS == defaults["launchGrantTtlMilliseconds"]
    assert config.DEFAULT_SESSION_TTL_MS == defaults["sessionTtlMilliseconds"]
    assert [config.MIN_DURATION_MS, config.MAX_DURATION_MS] == defaults["ttlMillisecondsRange"]
    assert config.DEFAULT_MAX_SESSIONS == defaults["maxSessions"]
    assert [1, 1_024] == defaults["maxSessionsRange"]
    assert set(config.LOOPBACK_NAMES) == set(CORPUS["origin"]["loopbackHostnames"])
    assert upstream.DISPATCH_VALUES == tuple(CORPUS["envelope"]["dispatchValues"])
    assert set(config.DRIVERS) == {"daemon", "contract-mock"}


def test_the_route_paths_and_methods_equal_the_corpus() -> None:
    by_id = {route["id"]: route for route in CORPUS["routes"]}
    assert by_id["session.open"]["path"] == app.SESSION_PATH
    assert by_id["session.status"]["path"] == f"{app.SESSION_PATH}/status"
    assert by_id["session.logout"]["path"] == f"{app.SESSION_PATH}/logout"
    assert by_id["daemon.catalog"]["path"] == app.REQUEST_PREFIX
    assert by_id["daemon.method"]["path"] == f"{app.REQUEST_PREFIX}/{{method}}"
    assert by_id["daemon.readyz"]["path"] == "/daemon/readyz"
    assert by_id["session.attach"]["path"] == devtools.ATTACH_PATH
    assert by_id["daemon.openapi"]["path"] == devtools.SCHEMA_PATH
    flagged = {route_id for route_id, route in by_id.items() if route.get("flag") == "devTools"}
    assert flagged == {"session.attach", "daemon.openapi"}
    assert set(by_id) - flagged == {
        "session.open", "session.status", "session.logout", "daemon.readyz", "daemon.catalog", "daemon.method",
        "daemon.other",
    }

    async def scenario(gateway: Any) -> dict[str, tuple[int, str]]:
        seen: dict[str, tuple[int, str]] = {}
        for route_id, route in by_id.items():
            if route_id == "daemon.other":
                continue
            wrong = "GET" if "GET" not in route["methods"] else "DELETE"
            path = route["path"].replace("{method}", "agent.list")
            reply = await gateway.call(path, method=wrong, headers={"origin": gateway_origin})
            seen[route_id] = (reply.status, reply.json.get("code", ""))
        return seen

    gateway_origin = "http://127.0.0.1:4173"
    for route_id, (status, code) in run_gateway(scenario).items():
        expected = (404, "METHOD_NOT_FOUND") if route_id in flagged else (405, "HTTP_METHOD_NOT_ALLOWED")
        assert (status, code) == expected, route_id
    for status, code in run_gateway(scenario, dev_tools=devtools.DevTools()).values():
        assert (status, code) == (405, "HTTP_METHOD_NOT_ALLOWED")


def test_the_fetch_site_set_equals_the_corpus() -> None:
    accepted = set(CORPUS["origin"]["fetchSiteAccepted"])

    async def scenario(gateway: Any) -> dict[str, int]:
        results: dict[str, int] = {}
        for value in ("same-origin", "none", "same-site", "cross-site", "", "Same-Origin", "None"):
            reply = await gateway.call("/daemon/readyz", headers={"sec-fetch-site": value})
            results[value] = reply.status
        return results

    results = run_gateway(scenario)
    for value, status in results.items():
        refused = status == 403
        assert refused is (value not in accepted), value


def test_the_version_range_and_the_response_value_equal_the_corpus() -> None:
    value = CORPUS["versioning"]["responseValue"]
    assert app.SESSION_CONTRACT == value
    assert CORPUS["sessionContractVersion"] == value["max"]
    assert CORPUS["versioning"]["headerValue"]["wellFormed"] == "^[1-9][0-9]*$"
    assert CORPUS["versioning"]["refusal"]["status"] == 409


def test_every_literal_failure_the_host_raises_is_a_corpus_pair() -> None:
    pairs, _ = _failure_pairs()
    unknown = pairs - _corpus_pairs() - STATIC_ONLY
    assert unknown == set()


def test_every_corpus_code_is_reachable_from_the_host_source() -> None:
    _, strings = _failure_pairs()
    missing = {row["code"] for row in CORPUS["errors"]} - strings
    assert missing == set()


def test_every_corpus_error_row_has_a_known_dispatch_value() -> None:
    allowed = set(CORPUS["envelope"]["dispatchValues"])
    for row in CORPUS["errors"]:
        assert set(row["dispatch"]) <= allowed
        assert row["status"] and all(isinstance(status, int) for status in row["status"])


def _validator(name: str) -> jsonschema.Draft202012Validator:
    schema = CORPUS["schemas"][name]
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize(
    ("schema", "example"),
    [
        ("sessionResponse", "sessionResponseReady"),
        ("sessionResponse", "sessionResponseBootstrap"),
        ("sessionStatusResponse", "sessionStatusResponse"),
        ("logoutResponse", "logoutResponse"),
        ("attachResponse", "attachResponse"),
        ("hostError", "hostError"),
    ],
)
def test_the_corpus_examples_validate_against_its_own_schemas(schema: str, example: str) -> None:
    assert list(_validator(schema).iter_errors(CORPUS["examples"][example])) == []


def test_live_session_status_logout_and_failure_responses_validate_against_the_corpus() -> None:
    async def scenario(gateway: Any) -> dict[str, Any]:
        opened = await gateway.open(headers={"x-hyprial-session-contract": "1"})
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        status = await gateway.call(
            "/daemon/session/status",
            method="POST",
            body={},
            headers={"cookie": control, "x-hyprial-view": str(opened.session["view"])},
        )
        denied = await gateway.call("/daemon/readyz", headers={"origin": "http://evil.example"})
        method_denied = await gateway.call(
            "/daemon/request/agent.list", method="POST", body={}, headers={"cookie": "", "x-hyprial-view": "v"}
        )
        logout = await gateway.call(
            "/daemon/session/logout", method="POST", body={}, headers=opened.headers()
        )
        return {
            "session": opened.session,
            "status": status.json,
            "denied": denied.json,
            "method": method_denied.json,
            "logout": logout.json,
        }

    live = run_gateway(scenario)
    assert list(_validator("sessionResponse").iter_errors(live["session"])) == []
    assert live["session"]["sessionContract"] == {"version": 1, "min": 1, "max": 1}
    assert list(_validator("sessionStatusResponse").iter_errors(live["status"])) == []
    assert list(_validator("logoutResponse").iter_errors(live["logout"])) == []
    for name in ("denied", "method"):
        assert list(_validator("hostError").iter_errors(live[name])) == []
    assert live["method"]["method"] == "agent.list"


def test_a_bootstrap_session_response_validates_and_carries_the_contract() -> None:
    ownerless = FakeUpstream(
        ready=ready_payload(backend={"available": True, "owner": None, "nodeId": "node", "epoch": "epoch"}),
        catalog=catalog_payload(
            backend={"kind": "gui", "nodeId": "node", "owner": None, "epoch": "epoch"},
            principal={"kind": "local-gui", "owner": None, "actor": None},
        ),
    )

    async def scenario(gateway: Any) -> dict[str, Any]:
        return (await gateway.open()).session

    session_body = run_gateway(scenario, config=host_config(principal=None), upstream=ownerless)
    assert list(_validator("sessionResponse").iter_errors(session_body)) == []
    assert session_body["phase"] == "bootstrap"
    assert GRANT not in json.dumps(session_body)


def test_the_developer_tool_paths_and_the_session_field_equal_the_corpus() -> None:
    section = CORPUS["static"]["developerTools"]
    assert section["flag"] == "devTools" and section["healthzField"] == "devTools"
    assert section["gatedPrefix"].startswith(f"{devtools.TOOL_PREFIX}/*")
    assert devtools.DOCS_PATH in section["withFlag"] and devtools.DOCS_PATH in section["withoutFlag"]
    assert f"{devtools.DOCS_ASSETS_PREFIX}*" in section["withFlag"]
    for name in (devtools.SCRIPT_NAME, *devtools.DOCS_FILES):
        assert name in section["withFlag"][f"{devtools.DOCS_ASSETS_PREFIX}*"]
    assert devtools.VIEW_STORAGE_KEY in section["docsDocumentInput"]
    assert section["sessionField"]["name"] == "devTools"
    assert set(devtools.DevTools().capability("daemon")) == set(section["sessionField"]["value"])


def test_live_developer_responses_validate_against_the_corpus() -> None:
    docs = {name: b"x" for name in devtools.DOCS_FILES}

    async def scenario(gateway: Any) -> dict[str, Any]:
        opened = await gateway.open()
        attached = await gateway.call(
            devtools.ATTACH_PATH,
            method="POST",
            body={},
            headers={"cookie": opened.cookies, "x-hyprial-view": str(opened.session["view"])},
        )
        refused = await gateway.call(devtools.ATTACH_PATH, method="POST", body={})
        health = await gateway.call(app.HEALTH_PATH)
        return {"session": opened.session, "attach": attached.json, "refused": refused.json, "health": health.json}

    live = run_gateway(scenario, dev_tools=devtools.DevTools(docs=docs))
    assert list(_validator("sessionResponse").iter_errors(live["session"])) == []
    assert live["session"]["devTools"] == {"apiDocs": True}
    assert list(_validator("attachResponse").iter_errors(live["attach"])) == []
    assert list(_validator("hostError").iter_errors(live["refused"])) == []
    assert set(CORPUS["static"]["healthz"]["requiredFields"]) <= set(live["health"])
    assert live["health"]["devTools"] is True
    broken = {**live["session"], "devTools": {"apiDocs": "yes"}}
    assert list(_validator("sessionResponse").iter_errors(broken)) != []
