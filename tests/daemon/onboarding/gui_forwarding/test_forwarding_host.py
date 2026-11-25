"""Z-6a: the GUI host forwards to a daemon that answers in bootstrap.

The daemon socket is replaced by a fake that answers exactly like the wire
bootstrap host (same dispatcher, wire policy).  The host must then take no lock,
write no ``bootstrap.json`` and open no journal: it is a pure client (KP-083).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon import DaemonOwnershipBusy, DaemonStateOwnershipFence
from hyprial.daemon.impl.application.onboarding import gui_host
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    WIRE_POLICY,
    BootstrapDispatcher,
)
from hyprial.daemon.impl.application.onboarding.gui_host import GuiRuntimeHost
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.request_http.catalog import build_view

OWNER = "forwarding-owner@example.test"
CALLABLE_BOOTSTRAP = (
    "auth.login.begin",
    "auth.logout",
    "auth.operation.cancel",
    "auth.operation.get",
    "auth.relogin",
    "onboarding.device.ensure",
    "onboarding.initialize",
    "onboarding.status",
)


def _home(tmp_path: Path, *, owner: str | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings: dict = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _dispatcher(policy, *, epoch: str = "daemon-epoch-1") -> BootstrapDispatcher:
    runtime = SimpleNamespace(
        socket_path=Path("daemon.sock"),
        view=SimpleNamespace(record=SimpleNamespace(epoch=epoch)),
    )
    dispatcher = BootstrapDispatcher(app=None, runtime=runtime, context=None, policy=policy)
    dispatcher._promotion_enabled = True
    return dispatcher


class FakeDaemon:
    """A bootstrap daemon on the socket: the wire dispatcher's own answers."""

    def __init__(self, socket_path: Path, *, phase: str = "bootstrap", epoch: str = "daemon-epoch-1") -> None:
        self.socket_path = socket_path
        self.phase = phase
        self.epoch = epoch
        self.calls: list[str] = []
        self.up = True
        self.owner: str | None = None
        self.pid = os.getppid()
        self.timeouts = 0

    def __call__(self, method, params=None, *, socket_path, timeout=None):
        self.calls.append(method)
        if not self.up or Path(socket_path) != self.socket_path:
            raise ipc_errors.DaemonUnavailableError("no daemon on this socket")
        if self.timeouts > 0:
            self.timeouts -= 1
            raise TimeoutError("ping timed out")
        dispatcher = _dispatcher(WIRE_POLICY, epoch=self.epoch)
        if method == "ping":
            ping = dispatcher.ping()
            ping["socket"] = str(self.socket_path)
            ping["pid"] = self.pid
            ping["phase"] = self.phase
            ping["owner"] = self.owner
            return ping
        if method == "daemon.catalog":
            return dispatcher.catalog()
        return {"forwarded": method, "params": params}


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    fake = FakeDaemon(tmp_path / "home" / "state" / "daemon.sock")
    monkeypatch.setattr(gui_host, "daemon_request_once", fake)
    return fake


def test_an_ownerless_home_with_a_bootstrap_daemon_forwards_and_takes_no_lock(tmp_path, daemon):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    try:
        assert host.phase == "bootstrap"
        assert host.forwarding is True
        assert host.owner is None
        assert not (home / "state" / "bootstrap.json").exists()
        fence = DaemonStateOwnershipFence.acquire(home / "state", timeout=0)
        fence.close()
        assert host("onboarding.status", {}, socket_path=host.socket_path) == {
            "forwarded": "onboarding.status",
            "params": {},
        }
        assert host("ping", {}, socket_path=host.socket_path)["phase"] == "bootstrap"
    finally:
        host.close()


def test_the_admitted_state_dir_names_the_socket_and_home_state_stays_untouched(tmp_path, monkeypatch):
    home = _home(tmp_path)
    state = tmp_path / "elsewhere"
    state.mkdir(mode=0o700)
    fake = FakeDaemon(state / "daemon.sock")
    monkeypatch.setattr(gui_host, "daemon_request_once", fake)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    host = GuiRuntimeHost(home, state_dir=state)
    try:
        assert host.phase == "bootstrap" and host.forwarding is True
        assert host.state_dir == state
        assert host.socket_path == state / "daemon.sock"
        assert host("onboarding.status", {}, socket_path=host.socket_path)["forwarded"]
        assert not (home / "state").exists()
        assert not any(state.iterdir())
    finally:
        host.close()


def test_the_forwarded_catalog_marks_phase_unavailable_methods_not_ready(tmp_path, daemon):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    try:
        from hyprial.daemon import daemon_catalog

        forwarded = host("daemon.catalog", {}, socket_path=host.socket_path)

        def expected_state(entry):
            if entry["name"] in CALLABLE_BOOTSTRAP:
                return "available", True, None
            if entry["httpExposed"]:
                return "not-ready", False, "ACCOUNT_REQUIRED"
            return "disabled", False, None

        expected = [
            (entry["name"], *expected_state(entry)) for entry in daemon_catalog()["methods"]
        ]
        assert [
            (item["name"], item["state"], item["callable"], item["reason"])
            for item in forwarded["methods"]
        ] == expected
        by_name = {item["name"]: item for item in forwarded["methods"]}
        assert by_name["onboarding.status"]["state"] == "available"
        assert by_name["onboarding.initialize"]["state"] == "available"
        assert by_name["agent.list"]["state"] == "not-ready"
        assert by_name["agent.list"]["callable"] is False
        assert by_name["agent.list"]["reason"] == "ACCOUNT_REQUIRED"
        assert any(item["state"] == "disabled" for item in forwarded["methods"])
        assert "principal" not in forwarded
        assert forwarded["epoch"] == "daemon-epoch-1"
        ping = host("ping", {}, socket_path=host.socket_path)
        view = build_view(
            ping, forwarded, socket_path=str(host.socket_path), local_entries=daemon_catalog()["methods"]
        )
        exposed = sorted(entry["name"] for entry in daemon_catalog()["methods"] if entry["httpExposed"])
        assert sorted(view.routes) == exposed
        assert {name for name, route in view.routes.items() if route.state == "available"} == set(CALLABLE_BOOTSTRAP)
    finally:
        host.close()


def test_a_forwarded_catalog_failure_is_unavailable_not_a_local_declaration(tmp_path, daemon):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    try:
        daemon.up = False
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("daemon.catalog", {}, socket_path=host.socket_path)
    finally:
        host.close()


def test_the_daemon_phase_wins_over_an_owner_in_the_snapshot(tmp_path, daemon):
    home = _home(tmp_path, owner=OWNER)
    host = GuiRuntimeHost(home)
    try:
        assert host.phase == "bootstrap"
        assert host.forwarding is True
        assert host.owner is None
        assert not (home / "state" / "bootstrap.json").exists()
    finally:
        host.close()


def test_attach_only_never_admits_when_no_daemon_answers(tmp_path, daemon):
    home = _home(tmp_path)
    daemon.up = False
    with pytest.raises(DaemonOwnershipBusy):
        GuiRuntimeHost(home, attach_only=True)
    assert not (home / "state" / "bootstrap.json").exists()
    fence = DaemonStateOwnershipFence.acquire(home / "state", timeout=0)
    fence.close()


def test_no_daemon_on_an_ownerless_home_is_unavailable_and_creates_nothing(tmp_path, daemon):
    home = _home(tmp_path)
    daemon.up = False
    with pytest.raises(ipc_errors.DaemonUnavailableError):
        GuiRuntimeHost(home)
    state = home / "state"
    assert not state.exists() or not any(state.iterdir())
    DaemonStateOwnershipFence.acquire(state, timeout=0).close()


def test_daemon_changed_sees_a_new_phase_or_epoch_and_never_a_silence(tmp_path, daemon, monkeypatch):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    monkeypatch.setattr(gui_host, "DAEMON_WATCH_SECONDS", 0.0)
    try:
        assert host.daemon_changed() is False
        daemon.up = False
        assert host.daemon_changed() is False
        daemon.up = True
        daemon.epoch = "daemon-epoch-2"
        assert host.daemon_changed() is True
        daemon.epoch = "daemon-epoch-1"
        daemon.phase = "serving"
        assert host.daemon_changed() is True
    finally:
        host.close()


def test_an_owned_home_with_no_daemon_is_unavailable_and_creates_nothing(tmp_path, daemon):
    home = _home(tmp_path, owner=OWNER)
    daemon.up = False
    with pytest.raises(ipc_errors.DaemonUnavailableError):
        GuiRuntimeHost(home)
    state = home / "state"
    assert not state.exists() or not any(state.iterdir())
    DaemonStateOwnershipFence.acquire(state, timeout=0).close()


def test_a_constructor_ping_that_times_out_is_unavailable_not_a_local_host(tmp_path, daemon):
    home = _home(tmp_path, owner=OWNER)
    daemon.phase = "serving"
    daemon.owner = OWNER
    daemon.timeouts = 1
    with pytest.raises(ipc_errors.DaemonUnavailableError):
        GuiRuntimeHost(home)
    host = GuiRuntimeHost(home)
    try:
        assert host.phase == "serving"
    finally:
        host.close()


def test_a_serving_host_sees_a_phase_or_owner_change_and_not_an_epoch_change(tmp_path, daemon, monkeypatch):
    home = _home(tmp_path, owner=OWNER)
    daemon.phase = "serving"
    daemon.owner = OWNER
    monkeypatch.setattr(gui_host, "DAEMON_WATCH_SECONDS", 0.0)
    host = GuiRuntimeHost(home)
    try:
        assert host.daemon_changed() is False
        daemon.epoch = "daemon-epoch-2"
        assert host.daemon_changed() is False
        daemon.owner = "other-owner@example.test"
        assert host.daemon_changed() is True
        daemon.owner = OWNER
        assert host.daemon_changed() is False
        daemon.phase = "bootstrap"
        daemon.owner = None
        assert host.daemon_changed() is True
    finally:
        host.close()


@pytest.mark.parametrize("after", ["bootstrap", "other-owner"])
def test_a_serving_host_that_sees_another_phase_or_owner_is_stale_for_good(tmp_path, daemon, after):
    home = _home(tmp_path, owner=OWNER)
    daemon.phase = "serving"
    daemon.owner = OWNER
    host = GuiRuntimeHost(home)
    try:
        assert host("ping", {}, socket_path=host.socket_path)["owner"] == OWNER
        if after == "bootstrap":
            daemon.phase, daemon.owner = "bootstrap", None
        else:
            daemon.owner = "other-owner@example.test"
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("ping", {}, socket_path=host.socket_path)
        assert host.daemon_changed() is True
        daemon.phase, daemon.owner = "serving", OWNER
        daemon.calls.clear()
        for method in ("ping", "daemon.catalog", "agent.list"):
            with pytest.raises(ipc_errors.DaemonUnavailableError):
                host(method, {}, socket_path=host.socket_path)
        assert daemon.calls == []
        assert host.daemon_changed() is True
    finally:
        host.close()


def test_a_serving_host_takes_its_epoch_from_the_daemon(tmp_path, daemon):
    home = _home(tmp_path, owner=OWNER)
    daemon.phase = "serving"
    daemon.owner = OWNER
    daemon.epoch = "daemon-epoch-7"
    host = GuiRuntimeHost(home)
    try:
        assert host.phase == "serving" and host.forwarding is False
        assert host.epoch == "daemon-epoch-7"
        assert host("ping", {}, socket_path=host.socket_path)["epoch"] == "daemon-epoch-7"
    finally:
        host.close()


def test_a_serving_host_whose_daemon_stops_answering_is_unavailable_and_not_changed(
    tmp_path, daemon, monkeypatch
):
    home = _home(tmp_path, owner=OWNER)
    daemon.phase = "serving"
    daemon.owner = OWNER
    monkeypatch.setattr(gui_host, "DAEMON_WATCH_SECONDS", 0.0)
    host = GuiRuntimeHost(home)
    try:
        daemon.up = False
        for method in ("ping", "daemon.catalog", "agent.list"):
            with pytest.raises(ipc_errors.DaemonUnavailableError):
                host(method, {}, socket_path=host.socket_path)
        assert host.daemon_changed() is False
        daemon.up = True
        ping = host("ping", {}, socket_path=host.socket_path)
        assert ping["phase"] == "serving" and ping["owner"] == OWNER
        catalog = host("daemon.catalog", {}, socket_path=host.socket_path)
        assert catalog["methods"] and catalog["principal"]["owner"] == OWNER
        assert host("agent.list", {}, socket_path=host.socket_path)["forwarded"] == "agent.list"
    finally:
        host.close()


def test_a_daemon_error_reaches_the_caller_unchanged(tmp_path, daemon, monkeypatch):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)

    def refuse(method, params=None, *, socket_path, timeout=None):
        raise DaemonRequestError("ACCOUNT_REQUIRED", "sign in first", {"method": method})

    try:
        monkeypatch.setattr(gui_host, "daemon_request_once", refuse)
        with pytest.raises(DaemonRequestError) as raised:
            host("agent.list", {}, socket_path=host.socket_path)
        assert raised.value.code == "ACCOUNT_REQUIRED"
    finally:
        host.close()


def test_a_forward_after_the_daemon_left_bootstrap_is_refused_before_it_is_sent(tmp_path, daemon):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    try:
        assert host("onboarding.status", {}, socket_path=host.socket_path)["forwarded"]
        daemon.phase = "serving"
        daemon.owner = OWNER
        daemon.calls.clear()
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("agent.list", {}, socket_path=host.socket_path)
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("agent.list", {}, socket_path=host.socket_path)
        assert "agent.list" not in daemon.calls
        daemon.phase = "bootstrap"
        daemon.owner = None
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("onboarding.status", {}, socket_path=host.socket_path)
        assert host("ping", {}, socket_path=host.socket_path)["phase"] == "bootstrap"
    finally:
        host.close()


def test_a_forward_after_an_epoch_change_is_refused(tmp_path, daemon):
    home = _home(tmp_path)
    host = GuiRuntimeHost(home)
    try:
        daemon.epoch = "daemon-epoch-2"
        with pytest.raises(ipc_errors.DaemonUnavailableError):
            host("onboarding.status", {}, socket_path=host.socket_path)
    finally:
        host.close()
