"""Z-6c part 2: the listener keeps its endpoint when the principal changes.

Real ``RequestHttpServer`` (uvicorn on port 0) over fake daemon handles.  What is
pinned: the endpoint, the uvicorn object and the pid are kept; the bearer is
re-issued in the same step that installs the principal (the old bearer is 401
on every authenticated route, the new one is accepted only where the new
principal is installed); a request is wholly of the principal it was admitted
with; a refusal before the install leaves the previous binding in place.  No
bearer value is printed or asserted by value.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from hyprial.kernel import canonical_user_uri
from hyprial.shell.impl.request_http.config import server_json_path
from hyprial.shell.impl.request_http.server import RequestHttpServer, StartupRefusal

from .support import (
    OTHER,
    OWNER,
    Gate,
    PhaseDaemon,
    get,
    gui_config,
    install_daemon,
    list_result,
    new_host,
    post,
    with_owner,
    write_home,
)

AUTHENTICATED_GET = ("/readyz", "/daemon/request", "/openapi.json")


def _document(server):
    return json.loads(server_json_path(server.config.runtime_dir).read_text(encoding="utf-8"))


def _bootstrap_server(tmp_path, monkeypatch):
    write_home(tmp_path)
    daemon = install_daemon(monkeypatch, tmp_path)
    host = new_host(tmp_path)
    server = RequestHttpServer(gui_config(tmp_path), request_once=host)
    server.start()
    return daemon, host, server


def _to_serving(tmp_path, daemon, host, server, owner=OWNER):
    write_home(tmp_path, owner=owner)
    daemon.become_serving(owner)
    host.close()
    fresh = new_host(tmp_path)
    server.rebind(with_owner(server.config, owner=owner), request_once=fresh)
    return fresh


def _to_bootstrap(tmp_path, daemon, host, server):
    write_home(tmp_path)
    daemon.become_bootstrap()
    host.close()
    fresh = new_host(tmp_path)
    server.rebind(with_owner(server.config, owner=None), request_once=fresh)
    return fresh


def _refused_everywhere(server, old_token):
    for path in AUTHENTICATED_GET:
        status, body = get(server, path, token=old_token)
        assert status == 401, (path, body)
    status, body = post(server, "onboarding.status", token=old_token)
    assert status == 401, body
    status, _ = get(server, "/healthz", token=old_token)
    assert status == 200


def test_initialize_and_logout_keep_the_listener_and_reissue_the_bearer(tmp_path, monkeypatch):
    daemon, host, server = _bootstrap_server(tmp_path, monkeypatch)
    try:
        endpoint, uvicorn_server, bearer0 = server.endpoint, server._uvicorn, server.token
        first = _document(server)
        assert first["principal"]["owner"] is None
        digest = get(server, "/daemon/request")[1]["catalogDigest"]

        host = _to_serving(tmp_path, daemon, host, server)
        bearer1 = server.token
        second = _document(server)
        assert server.endpoint == endpoint and server._uvicorn is uvicorn_server
        assert second["endpoint"] == endpoint and second["pid"] == first["pid"] == os.getpid()
        assert bearer1 != bearer0 and second["token"] == bearer1
        assert second["principal"]["owner"] == OWNER
        assert second["principal"]["actor"] == canonical_user_uri(OWNER)
        assert "transport" not in first and "transport" not in second
        assert stat.S_IMODE(server_json_path(server.config.runtime_dir).stat().st_mode) == 0o600
        _refused_everywhere(server, bearer0)
        status, catalog = get(server, "/daemon/request")
        assert status == 200 and catalog["catalogDigest"] == digest
        assert catalog["principal"]["owner"] == OWNER
        assert get(server, "/readyz")[0] == 200
        status, body = post(server, "onboarding.initialize", {"operationKey": "rebind-key-0000001"})
        assert (status, body["data"]) == (503, {"reason": "BOOTSTRAP_ONLY"})

        host = _to_bootstrap(tmp_path, daemon, host, server)
        bearer2 = server.token
        third = _document(server)
        assert server.endpoint == endpoint and server._uvicorn is uvicorn_server
        assert third["pid"] == first["pid"] and third["endpoint"] == endpoint
        assert bearer2 not in {bearer0, bearer1} and third["token"] == bearer2
        assert third["principal"]["owner"] is None and third["principal"]["actor"] is None
        assert "transport" not in third
        _refused_everywhere(server, bearer1)
        _refused_everywhere(server, bearer0)
        status, catalog = get(server, "/daemon/request")
        assert status == 200 and catalog["catalogDigest"] == digest
        assert catalog["principal"]["owner"] is None
        status, body = post(server, "agent.list")
        assert (status, body["data"]) == (503, {"reason": "ACCOUNT_REQUIRED"})
    finally:
        server.shutdown()
        host.close()
    assert not server_json_path(server.config.runtime_dir).exists()


def _serving_server(tmp_path, owner):
    daemon = PhaseDaemon(tmp_path / "home" / "state" / "daemon.sock", phase="serving", owner=owner)
    gate = Gate(daemon, "serving")
    write_home(tmp_path, owner=owner)
    server = RequestHttpServer(gui_config(tmp_path, owner=owner), request_once=gate)
    server.start()
    return daemon, gate, server


def test_the_new_bearer_exists_only_where_the_new_principal_is_installed(tmp_path):
    old_daemon, old_gate, server = _serving_server(tmp_path, OWNER)
    new_daemon = PhaseDaemon(old_daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    new_gate = Gate(new_daemon, "serving")
    bearer0 = server.token
    seen: dict = {}
    original = server._write_capability_file

    def at_install():
        seen["owner"] = server.config.owner
        seen["handle"] = server.request_once
        seen["changed"] = server.token != bearer0
        seen["old"] = get(server, "/readyz", token=bearer0)[0]
        waiting = threading.Thread(target=lambda: seen.__setitem__("new", get(server, "/readyz")))
        waiting.start()
        seen["waiting"] = waiting
        original()

    server._write_capability_file = at_install
    try:
        old_gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        assert seen["owner"] == OTHER and seen["handle"] is new_gate and seen["changed"] is True
        assert seen["old"] == 401
        seen["waiting"].join(30)
        status, body = seen["new"]
        assert status == 200 and body["ready"] is True, (status, body.get("code"))
        assert get(server, "/readyz")[0] == 200
        assert get(server, "/daemon/request")[1]["principal"]["owner"] == OTHER
    finally:
        server.shutdown()


def test_a_request_admitted_before_the_change_is_never_dispatched_under_the_new_principal(tmp_path):
    old_daemon, old_gate, server = _serving_server(tmp_path, OWNER)
    new_daemon = PhaseDaemon(old_daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    new_daemon.handlers["conversations.list"] = list_result
    new_gate = Gate(new_daemon, "serving")
    try:
        admitted = time.monotonic()
        old_gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        envelope = server._invoke("conversations.list", "http-early", admitted, {}, None)
        assert envelope.status == 503 and envelope.code == "DAEMON_UNAVAILABLE"
        assert envelope.dispatch == "not-dispatched"
        assert not new_gate.dispatched("conversations.list")
        assert not old_gate.dispatched("conversations.list")
        late = server._invoke("conversations.list", "http-late", time.monotonic(), {}, None)
        assert late.status == 200, late.payload
        assert new_daemon.calls[-1] == ("conversations.list", {"from": canonical_user_uri(OTHER)})
    finally:
        server.shutdown()


def test_a_request_in_flight_completes_under_the_principal_it_was_admitted_with(tmp_path):
    old_daemon, old_gate, server = _serving_server(tmp_path, OWNER)
    new_daemon = PhaseDaemon(old_daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    new_daemon.handlers["conversations.list"] = list_result
    new_gate = Gate(new_daemon, "serving")
    entered, release = threading.Event(), threading.Event()
    answers: list = []

    def hold(params):
        entered.set()
        assert release.wait(30)
        return list_result(params)

    old_daemon.handlers["conversations.list"] = hold
    caller = threading.Thread(target=lambda: answers.append(post(server, "conversations.list")))
    try:
        caller.start()
        assert entered.wait(30)
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        release.set()
        caller.join(30)
        assert not caller.is_alive()
        status, body = answers[0]
        assert status == 200 and body["result"]["participant"] == canonical_user_uri(OWNER), body
        assert not new_gate.dispatched("conversations.list")
        assert [params for name, params in old_daemon.calls if name == "conversations.list"] == [
            {"from": canonical_user_uri(OWNER)}
        ]
    finally:
        release.set()
        caller.join(30)
        server.shutdown()


def _snapshot(server):
    path = server_json_path(server.config.runtime_dir)
    return server.token, path.read_bytes(), server._binding


@pytest.mark.parametrize(
    "mutation",
    ["runtime_dir", "socket_path", "principal_kind", "serving-without-owner", "contract-changed"],
)
def test_a_refused_rebind_leaves_the_previous_binding_installed(tmp_path, mutation):
    from dataclasses import replace

    old_daemon, old_gate, server = _serving_server(tmp_path, OWNER)
    new_daemon = PhaseDaemon(old_daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    new_gate = Gate(new_daemon, "serving")
    config = with_owner(server.config, owner=OTHER)
    code = "PRINCIPAL_CONFIG_MISMATCH"
    if mutation == "runtime_dir":
        config = replace(config, runtime_dir=tmp_path / "elsewhere")
    elif mutation == "socket_path":
        config = replace(config, socket_path=tmp_path / "other.sock")
    elif mutation == "principal_kind":
        config = replace(config, principal_kind="local-operator")
    elif mutation == "serving-without-owner":
        config = with_owner(server.config, owner=None)
    else:
        new_daemon.overrides["conversations.list"] = {"state": "disabled", "callable": False}
        code = "CATALOG_MISMATCH"
    before = _snapshot(server)
    try:
        with pytest.raises(StartupRefusal) as refusal:
            server.rebind(config, request_once=new_gate)
        assert refusal.value.code == code
        if mutation == "contract-changed":
            assert refusal.value.reason == "CONTRACT_CHANGED"
        assert _snapshot(server) == before
        assert get(server, "/healthz")[0] == 200
        assert get(server, "/daemon/request")[1]["principal"]["owner"] == OWNER
    finally:
        server.shutdown()


def test_a_bootstrap_listener_refuses_a_rebind_to_another_principal_kind(tmp_path, monkeypatch):
    daemon, host, server = _bootstrap_server(tmp_path, monkeypatch)
    try:
        from dataclasses import replace

        with pytest.raises(StartupRefusal) as refusal:
            server.rebind(replace(server.config, principal_kind="local-bootstrap"), request_once=host)
        assert refusal.value.code == "PRINCIPAL_CONFIG_MISMATCH"
    finally:
        server.shutdown()
        host.close()


def test_a_contract_drift_is_stale_with_text_that_does_not_ask_for_a_restart(tmp_path):
    from hyprial.shell.impl.request_http.errors import HttpFailure

    daemon, gate, server = _serving_server(tmp_path, OWNER)
    try:
        daemon.overrides["conversations.list"] = {"state": "disabled", "callable": False}
        with pytest.raises(HttpFailure) as stale:
            server.refresh(force=True)
        assert stale.value.code == "CATALOG_STALE" and stale.value.dispatch == "not-dispatched"
        assert "restart the request-http process" not in stale.value.message
        status, body = get(server, "/readyz")
        assert status == 503 and body["code"] == "CATALOG_STALE"
        daemon.overrides.clear()
        assert get(server, "/readyz")[0] == 200
    finally:
        server.shutdown()
