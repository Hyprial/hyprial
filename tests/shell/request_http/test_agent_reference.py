"""Write-profile admission for the reference-slice agent provider methods.

Round 2 (H-03 integration): all three ``agent.provider.*`` methods are
admitted through H's write admission — real ``mutates=True``/``retry=never``
routes carrying the daemon-declared params schema.  The round-1 read-only
projection is retired; reads over IPC are unchanged.

The transport half below drives the real Uvicorn entry with a fake daemon:
every refusal happens before dispatch; an admitted write reaches the
existing daemon handler exactly once per request, and credential payloads
never enter transport logs or error envelopes.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import urllib.parse

import pytest
from jsonschema import Draft202012Validator

from hyprial import __version__
from hyprial.daemon import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.server import RequestHttpServer

OWNER = "h01-owner"
ACTOR = f"user:{OWNER}"
TARGET = f"agent:{OWNER}:h01-node:worker"
SENTINEL = "example-not-a-credential"

_PROVIDER_METHODS = (
    "agent.provider.default",
    "agent.provider.login",
    "agent.provider.logout",
)


def _view():
    entries = [dict(row, declared=True, state="available", callable=True)
               for row in daemon_catalog()["methods"]]
    return build_view(
        {"version": "test", "nodeId": "node", "owner": "owner", "epoch": "test", "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "test",
         "packageVersion": "test", "methods": entries},
        socket_path="/tmp/reference-test.sock", local_entries=entries,
    )


def _catalog_entries() -> dict[str, dict]:
    return {row["name"]: row for row in daemon_catalog()["methods"]}


def test_agent_provider_methods_admitted_under_the_write_profile():
    view = _view()
    entries = _catalog_entries()
    assert {"agent.list", "agent.secret.list", *_PROVIDER_METHODS} <= set(view.routes)
    paths = daemon_openapi()["paths"]
    for name in _PROVIDER_METHODS:
        route = view.routes[name]
        assert route.mutates is True
        assert route.retry == "never"
        assert route.confirmation == "none"
        assert route.http_binding["identity"] == {"mode": "none", "param": None}
        spec = METHOD_REGISTRY.get(name).spec
        assert spec.http_exposed and spec.http_binding is not None
        assert route.params_schema == entries[name]["paramsSchema"]
        assert f"/daemon/request/{name}" in paths


def test_provider_default_http_body_is_the_full_legacy_domain():
    route = _view().routes["agent.provider.default"]
    validator = Draft202012Validator(route.params_schema)
    for params in ({"provider": "claude"}, {"provider": None}, {"clear": True},
                   {"clear": "yes"}, {"clear": False}, {"clear": []}):
        assert validator.is_valid(params), params


def test_login_body_admits_only_the_declared_legacy_domain():
    route = _view().routes["agent.provider.login"]
    validator = Draft202012Validator(route.params_schema)
    assert validator.is_valid({"actor": "worker", "provider": "claude"})
    assert validator.is_valid({"actor": "worker", "setupToken": "example-not-a-credential"})
    assert not validator.is_valid([])
    assert not validator.is_valid("login")


@pytest.mark.parametrize("method", ["agent.list", "agent.secret.list", *_PROVIDER_METHODS])
def test_agent_error_contract_explicitly_declares_absent_data(method):
    for error in METHOD_REGISTRY.get(method).spec.errors:
        assert error.data_schema is not None or "no data" in error.description.lower()


def _config(tmp_path: Path) -> RequestHttpConfig:
    return RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner=OWNER,
        actor=ACTOR,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=tmp_path / "runtime",
        source=tmp_path / "config.json",
    )


def _entries() -> list[dict]:
    entries = []
    for entry in daemon_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = "available" if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None if item["state"] == "available" else "SERVICE_NOT_READY"
        entries.append(item)
    return entries


_LOGIN_RESULT = {
    "ok": True,
    "actor": TARGET,
    "provider": "claude",
    "files": [
        {
            "path": "/tmp/hyprial-example/.credentials.json",
            "bytes": 25,
            "replaced": False,
            "unchanged": False,
        }
    ],
}
_LOGOUT_RESULT = {
    "ok": True,
    "actor": TARGET,
    "provider": "claude",
    "removed": [],
    "absent": ["/tmp/hyprial-example/.credentials.json"],
}


def _fake_request_once(dispatched: list[tuple[str, dict]]):
    def request_once(method: str, params: dict, **_kwargs):
        if method == "ping":
            return {
                "version": __version__,
                "nodeId": "h01-node",
                "owner": OWNER,
                "epoch": "epoch-1",
                "phase": "serving",
                "restorePending": False,
            }
        if method == "daemon.catalog":
            return {
                "schemaVersion": 1,
                "catalogVersion": "1",
                "epoch": "epoch-1",
                "packageVersion": __version__,
                "methods": _entries(),
            }
        dispatched.append((method, dict(params)))
        if method == "agent.provider.login":
            if params.get("provider") == "refused-provider":
                from hyprial.kernel import DaemonRequestError

                raise DaemonRequestError(
                    "TARGET_NOT_AUTHORIZED", "the target agent is not authorized"
                )
            return dict(_LOGIN_RESULT)
        if method == "agent.provider.logout":
            return dict(_LOGOUT_RESULT)
        if method == "agent.provider.default":
            return {"ok": True, "provider": None, "previous": None}
        raise AssertionError(f"unexpected dispatch {method}")

    return request_once


def _running_server(tmp_path: Path, dispatched: list[tuple[str, dict]]):
    server = RequestHttpServer(
        _config(tmp_path), request_once=_fake_request_once(dispatched)
    )
    server.start()
    return server


def _post(
    server: RequestHttpServer,
    method: str,
    params: dict,
    *,
    token: str | None = "sent",
    origin: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict]:
    endpoint = urllib.parse.urlsplit(server.endpoint)

    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {server.token}"
    if origin is not None:
        headers["Origin"] = origin
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    connection.request(
        "POST",
        f"/daemon/request/{method}",
        body=json.dumps(params).encode("utf-8"),
        headers=headers,
    )
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw)


def _login_params() -> dict:
    return {
        "actor": TARGET,
        "provider": "claude",
        "files": [
            {
                "filename": ".credentials.json",
                "contentBase64": "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs",
            }
        ],
    }


def test_unauthenticated_login_write_is_refused_before_dispatch(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        status, envelope = _post(
            server, "agent.provider.login", _login_params(), token=None
        )
        assert status == 401
        assert envelope["code"] == "HTTP_UNAUTHORIZED"
        assert envelope["dispatch"] == "not-dispatched"
        assert dispatched == []
    finally:
        server.shutdown()


def test_foreign_origin_login_write_is_refused_before_dispatch(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        status, envelope = _post(
            server,
            "agent.provider.login",
            _login_params(),
            origin="http://evil.example",
        )
        assert status == 403
        assert envelope["code"] == "HTTP_ORIGIN_DENIED"
        assert envelope["dispatch"] == "not-dispatched"
        assert dispatched == []
    finally:
        server.shutdown()


def test_replayed_login_write_with_idempotency_key_is_refused(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        status, envelope = _post(
            server,
            "agent.provider.login",
            _login_params(),
            idempotency_key="replay-1",
        )
        assert status == 400
        assert envelope["code"] == "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED"
        assert envelope["dispatch"] == "not-dispatched"
        assert dispatched == []
    finally:
        server.shutdown()


def test_admitted_login_write_reaches_the_handler_exactly_once(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        status, envelope = _post(server, "agent.provider.login", _login_params())
        assert status == 200
        assert envelope["ok"] is True
        assert envelope["dispatch"] == "dispatched"
        assert envelope["method"] == "agent.provider.login"
        assert envelope["result"] == _LOGIN_RESULT
        assert len(dispatched) == 1
        assert dispatched[0][0] == "agent.provider.login"
        assert dispatched[0][1] == _login_params()
    finally:
        server.shutdown()


def test_admitted_logout_write_reaches_the_handler_exactly_once(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        params = {"actor": TARGET, "provider": "claude"}
        status, envelope = _post(server, "agent.provider.logout", params)
        assert status == 200
        assert envelope["dispatch"] == "dispatched"
        assert envelope["result"] == _LOGOUT_RESULT
        assert dispatched == [("agent.provider.logout", params)]
    finally:
        server.shutdown()


def test_domain_error_login_keeps_credentials_out_of_envelope_and_logs(
    tmp_path: Path, capfd
) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        status, envelope = _post(
            server, "agent.provider.login", dict(_login_params(), provider="refused-provider")
        )
        assert status == 403
        assert envelope["ok"] is False
        assert envelope["dispatch"] == "dispatched"
        body = json.dumps(envelope)
        assert "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs" not in body
        assert "contentBase64" not in body
        err = capfd.readouterr().err
        assert "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs" not in err
        assert "contentBase64" not in err
        assert len(dispatched) == 1
    finally:
        server.shutdown()


def test_write_routes_carry_the_never_retry_write_profile(tmp_path: Path) -> None:
    dispatched: list[tuple[str, dict]] = []
    server = _running_server(tmp_path, dispatched)
    try:
        endpoint = urllib.parse.urlsplit(server.endpoint)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
        connection.request(
            "GET",
            "/daemon/request",
            headers={"Authorization": f"Bearer {server.token}"},
        )
        response = connection.getresponse()
        catalog = json.loads(response.read())
        connection.close()
        methods = {item["name"]: item for item in catalog["methods"]}
        for name in ("agent.provider.login", "agent.provider.logout", "agent.provider.default"):
            method = methods[name]
            assert method["state"] == "available"
            assert method["mutates"] is True
            assert method["retry"] == "never"
            assert method["httpBinding"]["identity"] == {"mode": "none", "param": None}
    finally:
        server.shutdown()
