"""The daemon driver served in the host's own process (2822, B3).

Decision: host-serves-daemon-in-process-20261119.  ``LocalDaemon`` is the host
side of the fold: the daemon-side ``GuiRuntimeHost``, a listener-free
``RequestHttpServer``, an ``httpx.ASGITransport`` over its one application, an
in-memory authority and the principal watcher thread.  The daemon is the rebind
suite's scripted ``PhaseDaemon`` installed under the real ``GuiRuntimeHost``; the
session layer is the real ``HostApp`` driven in process.  Nothing here binds a
socket or writes a file: the old child's ``server.json`` and the launcher's
``frontend-authority.json`` have no counterpart.  Waits are on conditions
(a condition variable of the authority, an event the daemon hook sets); the one
number is the failure bound of such a wait.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from hyprial.daemon import DaemonOwnershipBusy
from hyprial.daemon.impl.application.onboarding import gui_host
from hyprial.kernel import canonical_user_uri
from hyprial.shell.impl.gui.host.config import AuthorityUnavailable, Principal, read_local_config
from hyprial.shell.impl.gui.host.local import LocalDaemon
from hyprial.shell.impl.request_http import StartupRefusal
from hyprial.shell.impl.request_http.bootstrap import gui as bootstrap_gui
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.gui.host.local.support import (
    ENV,
    FAILURE_BOUND_SECONDS,
    NODE,
    bearer_status,
    drive,
    open_local,
    world,
)
from tests.shell.gui.host.support import GRANT, Opened, run_gateway
from tests.shell.request_http.launch.support import tree
from tests.shell.request_http.rebind.support import OWNER, write_home

def test_a_serving_session_is_relayed_in_process(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="serving")
    local = open_local(tmp_path)
    try:
        assert local.config.principal == Principal(actor=canonical_user_uri(OWNER), owner=OWNER, node=NODE)

        async def scenario(gateway: Any) -> None:
            opened = await gateway.open()
            assert opened.reply.status == 200, opened.reply.raw
            assert opened.session["principal"]["owner"] == OWNER
            ready = await gateway.call("/daemon/readyz", headers=opened.headers())
            assert ready.status == 200 and ready.json["ready"] is True
            listed = await gateway.call(
                "/daemon/request/conversations.list", method="POST", body={}, headers=opened.headers()
            )
            assert listed.status == 200 and listed.json["ok"] is True, listed.raw

        drive(local, scenario)
        assert daemon.calls[-1] == ("conversations.list", {"from": canonical_user_uri(OWNER)})
    finally:
        local.close()


class HoldsTheAnswer(httpx.AsyncBaseTransport):
    """Delivers the in-process answer only after the test released it."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path.endswith("/onboarding.status") and self.release is not None:
            assert self.entered is not None
            self.entered.set()
            await self.release.wait()
        return response


def test_a_phase_change_is_session_changed_and_a_refresh_follows_on_the_same_origin(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    local = open_local(tmp_path)
    first = local.config
    held = HoldsTheAnswer(local.transport)
    try:
        assert first.principal is None

        async def scenario(gateway: Any) -> None:
            held.entered, held.release = asyncio.Event(), asyncio.Event()
            opened = await gateway.open()
            assert opened.session["principal"]["kind"] == "anonymous-bootstrap"
            headers = opened.headers()
            pending = asyncio.create_task(
                gateway.call("/daemon/request/onboarding.status", method="POST", body={}, headers=headers)
            )
            await asyncio.wait_for(held.entered.wait(), FAILURE_BOUND_SECONDS)
            write_home(tmp_path, owner=OWNER)
            daemon.become_serving()
            assert await asyncio.to_thread(local.wait_for_change, first, FAILURE_BOUND_SECONDS)
            held.release.set()
            changed = await pending
            assert (changed.status, changed.json["code"]) == (409, "GUI_SESSION_CHANGED")
            assert changed.json["dispatch"] == "not-dispatched"
            stale = await gateway.call("/daemon/request/conversations.list", method="POST", body={}, headers=headers)
            assert (stale.status, stale.json["dispatch"]) == (401, "not-dispatched")
            refreshed = await gateway.call("/daemon/session", method="POST", body={}, headers=headers)
            assert refreshed.status == 200, refreshed.raw
            assert refreshed.json["view"] == opened.session["view"]
            assert refreshed.json["generation"] != opened.session["generation"]
            assert refreshed.json["principal"] == {
                "kind": "serving", "owner": OWNER, "node": NODE, "actor": canonical_user_uri(OWNER)
            }
            fresh = Opened(refreshed)
            listed = await gateway.call(
                "/daemon/request/conversations.list",
                method="POST",
                body={},
                headers=fresh.headers(cookies=fresh.cookies or opened.cookies),
            )
            assert listed.status == 200, listed.raw

        run_gateway(scenario, upstream=held, config=first, authority=local.authority)
        assert local.failure is None
    finally:
        local.close()


def test_the_old_bearer_is_never_accepted_for_the_new_principal(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    local = open_local(tmp_path)
    first = local.config
    old = first.upstream_token
    try:
        assert asyncio.run(bearer_status(local, old)) == 200
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()
        assert local.wait_for_change(first, FAILURE_BOUND_SECONDS)
        new = local.authority()
        assert new.upstream_token not in {old, ""}
        assert new.principal is not None and new.principal.owner == OWNER
        assert asyncio.run(bearer_status(local, old)) == 401
        assert asyncio.run(bearer_status(local, old, "/daemon/request")) == 401
        assert asyncio.run(bearer_status(local, new.upstream_token)) == 200
        assert asyncio.run(bearer_status(local, new.upstream_token, "/daemon/request")) == 200
    finally:
        local.close()


def test_a_watcher_that_dies_is_surfaced_and_the_host_stops_serving(tmp_path, monkeypatch, capfd) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    stopped = threading.Event()

    attach = bootstrap_gui.new_host

    def broken(config: Any, **extra: Any) -> Any:
        if extra.get("attach_only"):
            raise RuntimeError("reattach blew up with token-should-not-leak")
        return attach(config, **extra)

    monkeypatch.setattr(bootstrap_gui, "new_host", broken)
    local = open_local(tmp_path)
    local.stop_on_failure(stopped.set)
    try:
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()
        assert stopped.wait(FAILURE_BOUND_SECONDS)
        assert isinstance(local.failure, RuntimeError)
        assert local.exit_code == 3
        with pytest.raises(AuthorityUnavailable):
            local.authority()
        line = json.loads(capfd.readouterr().err.strip().splitlines()[-1])
        assert line["event"] == "watcher_failed" and line["error"] == "RuntimeError"
        assert "token-should-not-leak" not in json.dumps(line)

        async def scenario(gateway: Any) -> None:
            reply = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
            assert (reply.status, reply.json["code"]) == (503, "GUI_AUTHORITY_UNAVAILABLE")

        drive(local, scenario)
    finally:
        local.close()


def test_a_failure_before_stop_on_failure_is_reported_when_it_is_registered(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    local = open_local(tmp_path)
    monkeypatch.setattr(bootstrap_gui, "new_host", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("early")))
    try:
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()
        assert local.wait_for_failure(FAILURE_BOUND_SECONDS)
        called = threading.Event()
        local.stop_on_failure(called.set)
        assert called.is_set()
    finally:
        local.close()


@pytest.mark.parametrize("rebinds", [False, True])
def test_shutdown_stops_the_watcher_before_the_server_and_closes_the_live_host_once(
    tmp_path, monkeypatch, rebinds
) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap" if rebinds else "serving")
    events: list[tuple[str, Any]] = []
    original_shutdown = RequestHttpServer.shutdown
    original_close = gui_host.GuiRuntimeHost.close
    holder: dict[str, LocalDaemon] = {}

    def shutdown(self: RequestHttpServer, **kwargs: Any) -> None:
        events.append(("shutdown", holder["local"].watcher_alive()))
        original_shutdown(self, **kwargs)

    def close(self: gui_host.GuiRuntimeHost) -> None:
        events.append(("close", id(self)))
        original_close(self)

    monkeypatch.setattr(RequestHttpServer, "shutdown", shutdown)
    monkeypatch.setattr(gui_host.GuiRuntimeHost, "close", close)
    local = open_local(tmp_path)
    holder["local"] = local
    first = local.config
    if rebinds:
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()
        assert local.wait_for_change(first, FAILURE_BOUND_SECONDS)
    live = local.live_host()
    local.close()
    local.close()
    names = [name for name, _ in events]
    assert names.count("shutdown") == 1 and ("shutdown", False) in events
    assert [value for name, value in events if name == "close"].count(id(live)) == 1
    assert events.index(("shutdown", False)) < events.index(("close", id(live)))
    assert events[-1] == ("close", id(live))
    assert not local.watcher_alive()


def test_a_closed_host_routes_nothing_and_never_asks_the_embedded_healthz(tmp_path, monkeypatch) -> None:
    world(tmp_path, monkeypatch, phase="serving")
    local = open_local(tmp_path)
    local.close()

    async def scenario(gateway: Any) -> None:
        reply = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        assert (reply.status, reply.json["code"]) == (503, "GUI_AUTHORITY_UNAVAILABLE")
        assert (await gateway.call("/__gui/healthz")).status == 200

    drive(local, scenario)
    assert asyncio.run(bearer_status(local, local.config.upstream_token, "/healthz")) == 200


def test_the_driver_binds_no_socket_and_writes_no_file(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the in-process driver must not bind a socket")

    before = tree(tmp_path)
    monkeypatch.setattr(socket.socket, "bind", refuse)
    local = open_local(tmp_path)
    first = local.config
    write_home(tmp_path, owner=OWNER)
    daemon.become_serving()
    assert local.wait_for_change(first, FAILURE_BOUND_SECONDS)
    local.close()
    after = tree(tmp_path)
    assert after == before
    names = {Path(item).name for item in after}
    assert not names & {"server.json", "server.lock", "frontend-authority.json", "request-http.json"}


def test_no_daemon_is_backend_unavailable_with_exit_three(tmp_path, monkeypatch) -> None:
    daemon = world(tmp_path, monkeypatch, phase="serving")
    daemon.up = False
    with pytest.raises(StartupRefusal) as refusal:
        open_local(tmp_path)
    assert (refusal.value.code, refusal.value.exit_code) == ("BACKEND_UNAVAILABLE", 3)


def test_a_daemon_that_is_still_stopping_is_ownership_busy_with_exit_three(tmp_path, monkeypatch) -> None:
    world(tmp_path, monkeypatch, phase="serving")

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise DaemonOwnershipBusy("the daemon is still stopping")

    monkeypatch.setattr(bootstrap_gui, "GuiRuntimeHost", busy)
    with pytest.raises(StartupRefusal) as refusal:
        open_local(tmp_path)
    assert (refusal.value.code, refusal.value.exit_code) == ("DAEMON_OWNERSHIP_BUSY", 3)


class CountingHost:
    def __init__(self, on_close: Any = None) -> None:
        self.closed = 0
        self.on_close = on_close

    def close(self) -> None:
        self.closed += 1
        if self.on_close is not None:
            self.on_close()


def test_a_host_the_watcher_installed_late_is_closed_once_after_the_server_shut_down() -> None:
    late = CountingHost()
    first = CountingHost(on_close=lambda: setattr(live, "host", late))
    live = bootstrap_gui.LiveHost(first)

    class Server:
        def shutdown(self) -> None:
            return None

    local = LocalDaemon(read_local_config(ENV), NODE)
    local._server = Server()
    local._live = live
    local.close()
    local.close()

    assert (first.closed, late.closed) == (1, 1)
