"""Z-6c part 1: the HTTP catalog is the same in every phase; a phase refusal is not-ready.

Real ``GuiRuntimeHost`` and ``RequestHttpServer`` over a fake daemon that answers
the bootstrap wire catalog or a serving catalog.  Rule 1: the route set and both
digests do not depend on the phase; a method the phase cannot call is
``not-ready`` with ``ACCOUNT_REQUIRED`` (no account) or ``BOOTSTRAP_ONLY``
(serving) and answers 503 ``HTTP_NOT_READY`` before any dispatch.
"""

from __future__ import annotations

from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.application.onboarding import gui_host
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import ACCOUNT_REQUIRED
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.request_http import catalog as request_catalog
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.server import RequestHttpServer

from .support import OWNER, gui_config, get, install_daemon, new_host, post, write_home

BOOTSTRAP_CALLABLE = {
    "auth.login.begin",
    "auth.logout",
    "auth.operation.cancel",
    "auth.operation.get",
    "auth.relogin",
    "onboarding.device.ensure",
    "onboarding.initialize",
    "onboarding.status",
}


def _view(tmp_path, monkeypatch, *, serving: bool):
    write_home(tmp_path, owner=OWNER if serving else None)
    daemon = install_daemon(monkeypatch, tmp_path)
    if serving:
        daemon.become_serving()
    host = new_host(tmp_path)
    try:
        ping = host("ping", {}, socket_path=host.socket_path)
        catalog = host("daemon.catalog", {}, socket_path=host.socket_path)
        return build_view(
            ping, catalog, socket_path=str(host.socket_path), local_entries=daemon_catalog()["methods"]
        )
    finally:
        host.close()


def test_the_route_set_and_both_digests_do_not_depend_on_the_phase(tmp_path, monkeypatch):
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "serving").mkdir()
    bootstrap = _view(tmp_path / "bootstrap", monkeypatch, serving=False)
    serving = _view(tmp_path / "serving", monkeypatch, serving=True)
    assert sorted(bootstrap.routes) == sorted(serving.routes)
    assert len(bootstrap.routes) > len(BOOTSTRAP_CALLABLE)
    assert bootstrap.http_contract_digest == serving.http_contract_digest
    assert bootstrap.declaration_digest == serving.declaration_digest

    for name, route in bootstrap.routes.items():
        if name in BOOTSTRAP_CALLABLE:
            assert (route.state, route.callable, route.reason) == ("available", True, None), name
        else:
            assert (route.state, route.callable, route.reason) == ("not-ready", False, "ACCOUNT_REQUIRED"), name
    for name in ("onboarding.device.ensure", "onboarding.initialize"):
        route = serving.routes[name]
        assert (route.state, route.callable, route.reason) == ("not-ready", False, "BOOTSTRAP_ONLY"), name
    assert gui_host.DISABLED_METHODS == {"onboarding.device.ensure", "onboarding.initialize"}


def test_the_phase_reasons_are_the_two_the_daemon_side_emits(tmp_path, monkeypatch):
    assert ACCOUNT_REQUIRED == "ACCOUNT_REQUIRED"
    assert request_catalog.PHASE_REASONS == frozenset({ACCOUNT_REQUIRED, "BOOTSTRAP_ONLY"})
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "serving").mkdir()
    seen = set()
    for label, serving in (("bootstrap", False), ("serving", True)):
        view = _view(tmp_path / label, monkeypatch, serving=serving)
        seen |= {route.reason for route in view.routes.values() if route.state != "available"}
    assert seen == request_catalog.PHASE_REASONS


def _started(tmp_path, monkeypatch, *, serving: bool, overrides=None):
    owner = OWNER if serving else None
    write_home(tmp_path, owner=owner)
    daemon = install_daemon(monkeypatch, tmp_path)
    if serving:
        daemon.become_serving()
    daemon.overrides.update(overrides or {})
    host = new_host(tmp_path)
    server = RequestHttpServer(gui_config(tmp_path, owner=owner), request_once=host)
    server.start()
    return daemon, host, server


def test_readyz_ignores_phase_gated_routes_but_not_a_real_not_ready(tmp_path, monkeypatch):
    (tmp_path / "boot").mkdir()
    (tmp_path / "real").mkdir()
    _daemon, host, server = _started(tmp_path / "boot", monkeypatch, serving=False)
    try:
        status, body = get(server, "/readyz")
        assert status == 200 and body["ready"] is True and body["notReady"] == [], body
        states = {item["name"]: item["state"] for item in body["methods"]}
        assert states["agent.list"] == "not-ready"
    finally:
        server.shutdown()
        host.close()

    forced = {"conversations.list": {"state": "not-ready", "callable": False, "reason": "SERVICE_NOT_READY"}}
    _daemon, host, server = _started(tmp_path / "real", monkeypatch, serving=True, overrides=forced)
    try:
        status, body = get(server, "/readyz")
        assert status == 503 and body["ready"] is False, body
        assert body["notReady"] == ["conversations.list"], body
    finally:
        server.shutdown()
        host.close()


def test_a_business_method_without_an_account_is_not_ready_and_never_dispatched(tmp_path, monkeypatch):
    daemon, host, server = _started(tmp_path, monkeypatch, serving=False)
    try:
        status, body = post(server, "agent.list")
        assert status == 503, body
        assert body["code"] == "HTTP_NOT_READY" and body["dispatch"] == "not-dispatched"
        assert body["data"] == {"reason": "ACCOUNT_REQUIRED"}
        assert "agent.list" not in {name for name, _ in daemon.calls}
        status, catalog = get(server, "/daemon/request")
        assert status == 200
        by_name = {item["name"]: item for item in catalog["methods"]}
        assert by_name["agent.list"]["state"] == "not-ready"
        assert by_name["agent.list"]["callable"] is False
        assert by_name["agent.list"]["reason"] == "ACCOUNT_REQUIRED"
        status, unknown = post(server, "no.such.method")
        assert status == 404 and unknown["code"] == "METHOD_NOT_FOUND"
    finally:
        server.shutdown()
        host.close()


def test_a_bootstrap_only_method_in_serving_is_not_ready(tmp_path, monkeypatch):
    daemon, host, server = _started(tmp_path, monkeypatch, serving=True)
    try:
        status, body = post(server, "onboarding.initialize", {"operationKey": "phase-key-0000001"})
        assert status == 503, body
        assert body["code"] == "HTTP_NOT_READY" and body["dispatch"] == "not-dispatched"
        assert body["data"] == {"reason": "BOOTSTRAP_ONLY"}
        assert not daemon.dispatched("onboarding.initialize")
    finally:
        server.shutdown()
        host.close()


def test_a_daemon_that_answers_account_required_is_not_a_possibly_executed_write(tmp_path, monkeypatch):
    daemon, host, server = _started(tmp_path, monkeypatch, serving=True)
    try:
        daemon.refusals["agent.list"] = DaemonRequestError("ACCOUNT_REQUIRED", "sign in first", {"method": "agent.list"})
        status, body = post(server, "agent.list")
        assert status == 503, body
        assert body["code"] == "HTTP_NOT_READY" and body["dispatch"] == "not-dispatched", body
        assert body["data"] == {"reason": "ACCOUNT_REQUIRED"}
    finally:
        server.shutdown()
        host.close()
