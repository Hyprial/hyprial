"""Fake daemon, GUI hosts and loopback calls for the phase-change tests.

Decision: daemon-first-z6c-phase-test-support-20261117.  The daemon is one
callable on ``gui_host.daemon_request_once`` that answers like the real one in
each phase: the bootstrap wire dispatcher's own ping and catalog, or a serving
ping and a serving catalog built from the declared method set.  The hosts and
the transport are the real ``GuiRuntimeHost`` and ``RequestHttpServer``.  No
bearer is ever printed; tests compare tokens.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import urllib.error
import urllib.request

from hyprial import __version__
from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.application.onboarding import gui_host
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    WIRE_POLICY,
    BootstrapDispatcher,
)
from hyprial.daemon.impl.application.onboarding.gui_host import GuiRuntimeHost
from hyprial.kernel import canonical_user_uri, ipc_errors
from hyprial.shell.impl.request_http.config import (
    DEFAULT_BUDGETS,
    RequestHttpConfig,
    _budgets,
)

OWNER = "rebind-owner@example.test"
OTHER = "rebind-other@example.test"


def write_home(tmp_path: Path, *, owner: str | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    settings: dict[str, Any] = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _dispatcher(epoch: str) -> BootstrapDispatcher:
    runtime = SimpleNamespace(
        socket_path=Path("daemon.sock"),
        view=SimpleNamespace(record=SimpleNamespace(epoch=epoch)),
    )
    dispatcher = BootstrapDispatcher(app=None, runtime=runtime, context=None, policy=WIRE_POLICY)
    dispatcher._promotion_enabled = True
    return dispatcher


class PhaseDaemon:
    """One daemon socket whose phase, owner and epoch a test changes."""

    def __init__(self, socket_path: Path, *, phase: str = "bootstrap", owner: str | None = None, epoch: str = "daemon-epoch-1") -> None:
        self.socket_path = socket_path
        self.phase = phase
        self.owner = owner
        self.epoch = epoch
        self.up = True
        self.pid = os.getppid()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.overrides: dict[str, dict[str, Any]] = {}
        self.refusals: dict[str, Exception] = {}
        self.handlers: dict[str, Any] = {}
        self.before: dict[str, Any] = {}

    def become_serving(self, owner: str = OWNER, epoch: str = "daemon-epoch-2") -> None:
        self.phase, self.owner, self.epoch = "serving", owner, epoch

    def become_bootstrap(self, epoch: str = "daemon-epoch-3") -> None:
        self.phase, self.owner, self.epoch = "bootstrap", None, epoch

    def __call__(self, method, params=None, *, socket_path, timeout=None):
        self.calls.append((method, dict(params or {})))
        if not self.up or Path(socket_path) != self.socket_path:
            raise ipc_errors.DaemonUnavailableError("no daemon on this socket")
        if method in self.before:
            self.before[method]()
        if method == "ping":
            return self._ping()
        if method == "daemon.catalog":
            return self._catalog()
        if method in self.refusals:
            raise self.refusals[method]
        if method in self.handlers:
            return self.handlers[method](dict(params or {}))
        return {"forwarded": method}

    def dispatched(self, method: str) -> bool:
        return any(name == method for name, _ in self.calls)

    def _ping(self) -> dict[str, Any]:
        if self.phase == "bootstrap":
            ping = _dispatcher(self.epoch).ping()
        else:
            ping = {"version": __version__, "nodeId": "daemon-node", "epoch": self.epoch}
        ping.update({"socket": str(self.socket_path), "pid": self.pid, "phase": self.phase, "owner": self.owner})
        return ping

    def _catalog(self) -> dict[str, Any]:
        if self.phase == "bootstrap":
            return _dispatcher(self.epoch).catalog()
        methods = []
        for entry in daemon_catalog()["methods"]:
            state = "available" if entry["httpExposed"] else "disabled"
            item = {**entry, "declared": True, "state": state, "callable": state == "available", "reason": None}
            if entry["name"] in self.overrides:
                item.update(self.overrides[entry["name"]])
            methods.append(item)
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "packageVersion": __version__,
            "epoch": self.epoch,
            "methods": methods,
        }


class Gate:
    """A host handle over one daemon that records its calls and can be closed."""

    def __init__(self, daemon: PhaseDaemon, phase: str) -> None:
        self.daemon = daemon
        self.phase = phase
        self.forwarding = phase == "bootstrap"
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method, params=None, *, socket_path, timeout=None):
        self.calls.append((method, dict(params or {})))
        if self.closed:
            raise ipc_errors.DaemonUnavailableError("the GUI host is unavailable")
        return self.daemon(method, params, socket_path=socket_path, timeout=timeout)

    def close(self) -> None:
        self.closed = True

    def dispatched(self, method: str) -> bool:
        return any(name == method for name, _ in self.calls)


def list_result(params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "participant": params.get("from"), "conversations": [], "nextCursor": None}


def gui_config(tmp_path: Path, *, owner: str | None = None) -> RequestHttpConfig:
    home = tmp_path / "home"
    state = home / "state"
    return RequestHttpConfig(
        socket_path=state / "daemon.sock",
        owner=owner,
        actor=canonical_user_uri(owner) if owner else None,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost"),
        budgets=_budgets(DEFAULT_BUDGETS),
        runtime_dir=tmp_path / "http",
        source=tmp_path,
        principal_kind="local-gui",
        home=home,
        state_dir=state,
    )


def with_owner(config: RequestHttpConfig, *, owner: str | None) -> RequestHttpConfig:
    return replace(config, owner=owner, actor=canonical_user_uri(owner) if owner else None)


def install_daemon(monkeypatch, tmp_path: Path, **kwargs: Any) -> PhaseDaemon:
    daemon = PhaseDaemon(tmp_path / "home" / "state" / "daemon.sock", **kwargs)
    monkeypatch.setattr(gui_host, "daemon_request_once", daemon)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    return daemon


def new_host(tmp_path: Path) -> GuiRuntimeHost:
    return GuiRuntimeHost(tmp_path / "home")


def call(endpoint: str, token: str | None, method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        endpoint + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.load(error)


def get(server, path: str, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    return call(server.endpoint, server.token if token is None else token, "GET", path)


def post(server, method: str, body: dict | None = None, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    return call(
        server.endpoint,
        server.token if token is None else token,
        "POST",
        "/daemon/request/" + method,
        {} if body is None else body,
    )
