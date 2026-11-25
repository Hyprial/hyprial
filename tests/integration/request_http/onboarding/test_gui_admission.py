"""A4 GUI admission: account-only serving behind the GUI host.

The host admits its phase from durable state and the daemon's ping.  These
tests prove, over the real loopback request entry:

* a serving home projects the owner read from its own settings, and losing that
  account refuses serving methods (account-only callability);
* untrusted ``owner``/``phase``/``home``/``view`` body fields cannot construct a
  context or change the projection;
* the legacy four-method freeze and the B/A5 disabled methods stay intact.

The hosted-bootstrap cases (an ownerless home served by the child itself) were
deleted with that path in Z-6b; the daemon's bootstrap wire owns them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

import pytest

from hyprial import __version__
from hyprial.daemon.impl.application.onboarding import gui_host
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    BOOTSTRAP_CALLABLE_METHODS,
)
from hyprial.daemon.impl.application.onboarding.gui_host import (
    DISABLED_METHODS,
    GuiRuntimeHost,
    METHODS,
    SERVING_ONLY_METHODS,
)
from hyprial.kernel import (
    DaemonRequestError,
    canonical_user_uri,
    ipc_errors,
)
from hyprial.shell.impl.request_http.config import (
    DEFAULT_BUDGETS,
    RequestHttpConfig,
    _budgets,
)
from hyprial.shell.impl.request_http.server import RequestHttpServer


def _home(tmp_path: Path, *, owner: str | None) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings: dict = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _config(
    home: Path,
    host: GuiRuntimeHost,
    *,
    owner: str | None = None,
    actor: str | None = None,
) -> RequestHttpConfig:
    return RequestHttpConfig(
        socket_path=host.socket_path,
        owner=owner,
        actor=actor,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost"),
        budgets=_budgets(DEFAULT_BUDGETS),
        runtime_dir=home / "state" / "gui-http",
        source=home,
        principal_kind="local-gui",
    )


def _post(server: RequestHttpServer, method: str, body: dict, *, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    headers["Authorization"] = "Bearer " + (server.token if token is None else token)
    return urllib.request.urlopen(
        urllib.request.Request(
            server.endpoint + "/daemon/request/" + method,
            data=json.dumps(body).encode(),
            headers=headers,
        ),
        timeout=10,
    )


def _readyz(server: RequestHttpServer) -> dict:
    request = urllib.request.Request(
        server.endpoint + "/readyz",
        headers={"Authorization": "Bearer " + server.token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _catalog(server: RequestHttpServer) -> dict:
    request = urllib.request.Request(
        server.endpoint + "/daemon/request",
        headers={"Authorization": "Bearer " + server.token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def test_serving_projects_the_account_surface_and_forwards_all_of_it(
    tmp_path, monkeypatch
):
    """F2-v4: no local onboarding port; the account surface is the daemon's."""

    home = _home(tmp_path, owner="serving-owner")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    recorder = _RecordingDaemon(ping=_ping_answer(), catalog=_account_catalog())
    monkeypatch.setattr(gui_host, "daemon_request_once", recorder)
    host = GuiRuntimeHost(home, state_dir=state)
    try:
        assert host.phase == "serving"
        assert host.owner == "serving-owner"

        catalog = host("daemon.catalog", {}, socket_path=host.socket_path)
        states = {entry["name"]: entry for entry in catalog["methods"]}
        for name in METHODS | SERVING_ONLY_METHODS:
            assert states[name]["state"] == "available", name
        for name in DISABLED_METHODS:
            assert states[name]["callable"] is False
            assert states[name]["state"] == "not-ready"
            assert states[name]["reason"] == "BOOTSTRAP_ONLY"

        for name in sorted(METHODS | SERVING_ONLY_METHODS):
            recorder.calls.clear()
            host(name, {"probe": name}, socket_path=host.socket_path)
            assert recorder.calls == [
                (name, {"probe": name}, host.socket_path, None)
            ], name

        recorder.calls.clear()
        host(
            "onboarding.status", {"owner": "forged"}, socket_path=host.socket_path
        )
        assert recorder.calls == [
            ("onboarding.status", {"owner": "forged"}, host.socket_path, None)
        ]

        (home / "settings.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        catalog = host("daemon.catalog", {}, socket_path=host.socket_path)
        states = {entry["name"]: entry for entry in catalog["methods"]}
        assert states["onboarding.status"]["state"] == "not-ready"
        assert states["onboarding.status"]["reason"] == "ACCOUNT_REQUIRED"
    finally:
        host.close()


def test_host_refuses_wrong_socket_and_a_symlinked_home(tmp_path, monkeypatch):
    home = _home(tmp_path, owner="serving-owner")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setattr(gui_host, "daemon_request_once", _serving_daemon("serving-owner"))
    host = GuiRuntimeHost(home)
    try:
        with pytest.raises(DaemonRequestError) as refused:
            host("onboarding.status", {}, socket_path=home / "elsewhere.sock")
        assert refused.value.code == ipc_errors.DAEMON_UNAVAILABLE
    finally:
        host.close()

    # The GUI layer no longer repeats the OS home-custody gate (Allen ruling c),
    # but a symlinked home is still refused before it is resolved.
    real = tmp_path / "real-home"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked-home"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(Exception) as refused:
        GuiRuntimeHost(link)
    assert "not a real directory" in str(refused.value)


def test_legacy_four_method_freeze_and_disabled_methods(tmp_path, monkeypatch):
    home = _home(tmp_path, owner="serving-owner")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    assert len(METHODS) == 4
    assert DISABLED_METHODS == {
        "onboarding.device.ensure",
        "onboarding.initialize",
    }
    assert "auth.logout" in BOOTSTRAP_CALLABLE_METHODS
    monkeypatch.setattr(gui_host, "daemon_request_once", _serving_daemon("serving-owner"))
    host = GuiRuntimeHost(home)
    try:
        for name in sorted(DISABLED_METHODS):
            monkeypatch.setattr(
                gui_host,
                "daemon_request_once",
                _RecordingDaemon(
                    error=DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "bootstrap only")
                ),
            )
            with pytest.raises(DaemonRequestError) as refused:
                host(name, {}, socket_path=host.socket_path)
            assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
            monkeypatch.setattr(
                gui_host,
                "daemon_request_once",
                _RecordingDaemon(error=ipc_errors.DaemonUnavailableError("no daemon")),
            )
            with pytest.raises(DaemonRequestError) as gone:
                host(name, {}, socket_path=host.socket_path)
            assert gone.value.code == ipc_errors.DAEMON_UNAVAILABLE
    finally:
        host.close()


def test_loopback_http_requires_the_gui_host(tmp_path):
    """A bare callable is not an admitted GUI runtime host."""

    home = _home(tmp_path, owner="serving-owner")
    config = RequestHttpConfig(
        socket_path=home / "state" / "daemon.sock",
        owner=None,
        actor=None,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost"),
        budgets=_budgets(DEFAULT_BUDGETS),
        runtime_dir=home / "state" / "gui-http",
        source=home,
        principal_kind="local-gui",
    )
    from hyprial.shell.impl.request_http.server import StartupRefusal

    with pytest.raises(StartupRefusal) as refused:
        RequestHttpServer(config, request_once=lambda *args, **kwargs: {})
    assert refused.value.code == "GUI_HOST_REQUIRED"


def test_serving_child_publishes_the_canonical_actor_the_session_host_compares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the published principal equals the trusted principal the host compares.

    The session host's probe (``host/upstream.py``; the deleted Node host's
    ``probeDaemon`` before it) answers 502 when the
    daemon catalog ``principal.owner``/``actor`` differ from the trusted
    ``(owner, canonical actor)`` pair.  The child used to publish a null actor,
    so every serving session failed there.  This case walks the exact payload
    fields that probe reads, over the real loopback entry.
    """

    owner = "serving-owner"
    home = _home(tmp_path, owner=owner)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    actor = canonical_user_uri(owner)
    monkeypatch.setattr(gui_host, "daemon_request_once", _serving_daemon(owner))
    host = GuiRuntimeHost(home, state_dir=state)
    try:
        assert host.phase == "serving"
        server = RequestHttpServer(
            _config(home, host, owner=owner, actor=actor), request_once=host
        )
        try:
            server.start()
            ready = _readyz(server)
            catalog = _catalog(server)
        finally:
            server.shutdown()
    finally:
        host.close()

    backend = ready["backend"]
    ready_catalog = ready["catalog"]
    assert ready["ok"] is True
    assert ready["ready"] is True
    assert ready["transport"] == "ready"
    assert backend["available"] is True
    assert backend["owner"] == owner
    assert ready_catalog["fresh"] is True

    assert catalog["principal"] == {
        "kind": "local-gui",
        "owner": owner,
        "actor": actor,
    }
    assert catalog["backend"]["owner"] == backend["owner"]
    assert catalog["backend"]["nodeId"] == backend["nodeId"]
    assert catalog["backend"]["epoch"] == backend["epoch"]
    assert catalog["catalogDigest"] == ready_catalog["catalogDigest"]
    assert catalog["declarationDigest"] == ready_catalog["declarationDigest"]


def test_serving_child_refuses_a_missing_or_foreign_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null actor can never match the trusted principal; a foreign one lies."""

    owner = "serving-owner"
    home = _home(tmp_path, owner=owner)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(gui_host, "daemon_request_once", _serving_daemon(owner))
    host = GuiRuntimeHost(home, state_dir=state)
    try:
        with pytest.raises(Exception) as missing:
            RequestHttpServer(
                _config(home, host, owner=owner, actor=None), request_once=host
            )
        assert getattr(missing.value, "code", None) == "PRINCIPAL_CONFIG_MISMATCH"

        foreign = canonical_user_uri("someone-else")
        foreign_server = RequestHttpServer(
            _config(home, host, owner=owner, actor=foreign), request_once=host
        )
        try:
            with pytest.raises(Exception) as refused:
                foreign_server.start()
            assert getattr(refused.value, "code", None) == "PRINCIPAL_CONFIG_MISMATCH"
        finally:
            foreign_server.shutdown()
    finally:
        host.close()


class _RecordingDaemon:
    """A daemon client stand-in that records every forwarded call."""

    def __init__(self, *, ping=None, catalog=None, error=None):
        self.calls: list[tuple[str, dict, Path, float | None]] = []
        self._ping = ping
        self._catalog = catalog
        self._error = error

    def __call__(self, method, params=None, *, socket_path, timeout=None):
        self.calls.append((method, dict(params or {}), Path(socket_path), timeout))
        if self._error is not None:
            raise self._error
        if method == "ping":
            return dict(self._ping or {})
        if method == "daemon.catalog":
            return dict(self._catalog or {})
        return {"ok": True, "method": method, "params": dict(params or {})}


def _account_catalog() -> dict:
    """A daemon catalog carrying every declared onboarding name."""

    names = sorted(METHODS | SERVING_ONLY_METHODS | DISABLED_METHODS)
    return {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "packageVersion": __version__,
        "epoch": "daemon-epoch",
        "backend": {
            "owner": "serving-owner",
            "nodeId": "daemon-node",
            "epoch": "daemon-epoch",
        },
        "methods": [
            {
                "name": name,
                "state": "disabled",
                "callable": False,
                "reason": None,
                "mutates": True,
            }
            for name in names
        ]
        + [
            {
                "name": "agent.list",
                "state": "available",
                "callable": True,
                "reason": None,
                "mutates": False,
            }
        ],
    }


def _ping_answer(owner: str = "serving-owner") -> dict:
    return {
        "version": __version__,
        "nodeId": "daemon-node",
        "owner": owner,
        "epoch": "daemon-epoch",
        "phase": "serving",
    }


def _serving_daemon(owner: str = "serving-owner") -> _RecordingDaemon:
    """A serving daemon that answers its ping and its whole declared catalog."""

    from hyprial.daemon import daemon_catalog

    catalog = {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "packageVersion": __version__,
        "epoch": "daemon-epoch",
        "methods": [
            {**entry, "declared": True, "state": "available", "callable": True, "reason": None}
            for entry in daemon_catalog()["methods"]
        ],
    }
    return _RecordingDaemon(ping=_ping_answer(owner), catalog=catalog)


def _serving_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str = "serving-owner"):
    home = _home(tmp_path, owner=owner)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    actor = canonical_user_uri(owner)
    monkeypatch.setattr(gui_host, "daemon_request_once", _serving_daemon(owner))
    host = GuiRuntimeHost(home, state_dir=state, actor=actor)
    assert host.phase == "serving"
    return home, state, actor, host


def test_serving_forwards_every_non_account_method_to_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: a business method is the daemon's, called exactly like the CLI."""

    home, state, actor, host = _serving_host(tmp_path, monkeypatch)
    recorder = _RecordingDaemon()
    monkeypatch.setattr(gui_host, "daemon_request_once", recorder)
    try:
        result = host(
            "agent.list", {"limit": 3}, socket_path=host.socket_path, timeout=7
        )
        assert result == {"ok": True, "method": "agent.list", "params": {"limit": 3}}
        assert recorder.calls == [("agent.list", {"limit": 3}, host.socket_path, 7)]

        for name in sorted(METHODS | SERVING_ONLY_METHODS):
            recorder.calls.clear()
            forwarded = host(name, {"probe": name}, socket_path=host.socket_path)
            assert forwarded["method"] == name
            assert recorder.calls == [
                (name, {"probe": name}, host.socket_path, None)
            ], name

        recorder.calls.clear()
        RequestHttpServer(
            _config(home, host, owner="serving-owner", actor=actor), request_once=host
        )
        assert recorder.calls == []

    finally:
        host.close()


def test_serving_ping_normalizes_a_reconciled_daemon_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon may answer ``reconciled``; the GUI entry only accepts serving."""

    _home_dir, _state, _actor, host = _serving_host(tmp_path, monkeypatch)
    recorder = _RecordingDaemon(
        ping={
            "running": True,
            "version": "daemon-version",
            "nodeId": "daemon-node",
            "owner": "serving-owner",
            "epoch": "daemon-epoch",
            "phase": "reconciled",
        }
    )
    monkeypatch.setattr(gui_host, "daemon_request_once", recorder)
    try:
        ping = host("ping", {}, socket_path=host.socket_path)
        assert ping["phase"] == "serving"
        assert ping["version"] == "daemon-version"
        assert ping["nodeId"] == "daemon-node"
        assert ping["owner"] == "serving-owner"
        assert ping["epoch"] == "daemon-epoch"
        assert recorder.calls == [("ping", {}, host.socket_path, 10)]
    finally:
        host.close()


def test_serving_catalog_overlays_the_daemon_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon stays the method authority; the account surface is overlaid."""

    _home_dir, _state, actor, host = _serving_host(tmp_path, monkeypatch)
    catalog = {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "packageVersion": __version__,
        "epoch": "daemon-epoch",
        "backend": {"owner": "serving-owner", "nodeId": "daemon-node", "epoch": "daemon-epoch"},
        "methods": [
            {
                "name": "agent.list",
                "state": "available",
                "callable": True,
                "reason": None,
                "mutates": False,
            },
            {
                "name": "onboarding.status",
                "state": "disabled",
                "callable": False,
                "reason": None,
                "mutates": False,
            },
        ],
    }
    monkeypatch.setattr(
        gui_host, "daemon_request_once", _RecordingDaemon(catalog=catalog)
    )
    try:
        projection = host("daemon.catalog", {}, socket_path=host.socket_path)
        by_name = {entry["name"]: entry for entry in projection["methods"]}
        assert by_name["agent.list"]["state"] == "available"
        assert by_name["agent.list"]["callable"] is True
        assert by_name["onboarding.status"]["state"] == "available"
        assert by_name["onboarding.status"]["callable"] is True
        assert projection["epoch"] == "daemon-epoch"
        assert projection["backend"] == catalog["backend"]
        assert projection["principal"] == {
            "kind": "local-gui",
            "owner": "serving-owner",
            "actor": actor,
        }
    finally:
        host.close()


def test_serving_propagates_a_daemon_error_code_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: a daemon code reaches the caller; nothing is re-mapped or swallowed."""

    _home_dir, _state, _actor, host = _serving_host(tmp_path, monkeypatch)
    monkeypatch.setattr(
        gui_host,
        "daemon_request_once",
        _RecordingDaemon(
            error=DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, "no such agent")
        ),
    )
    try:
        with pytest.raises(DaemonRequestError) as refused:
            host("agent.get", {"name": "missing"}, socket_path=host.socket_path)
        assert refused.value.code == ipc_errors.AGENT_NOT_FOUND
    finally:
        host.close()


def test_serving_host_never_opens_the_onboarding_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serving child must not become a second journal writer.

    ``OperationService.open`` is replaced with a tripwire before the host is
    built, so both construction and every call would fail loudly if any path
    still opened the onboarding store locally.
    """

    from hyprial.daemon.impl.onboarding.operations.service import OperationService

    def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "a serving child must never open the onboarding journal"
        )

    monkeypatch.setattr(OperationService, "open", forbidden)

    home = _home(tmp_path, owner="serving-owner")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    recorder = _RecordingDaemon(ping=_ping_answer(), catalog=_account_catalog())
    monkeypatch.setattr(gui_host, "daemon_request_once", recorder)
    host = GuiRuntimeHost(home, state_dir=state)
    try:
        assert host.phase == "serving"
        host("ping", {}, socket_path=host.socket_path)
        host("daemon.catalog", {}, socket_path=host.socket_path)
        for name in sorted(METHODS | SERVING_ONLY_METHODS):
            recorder.calls.clear()
            host(name, {}, socket_path=host.socket_path)
            assert recorder.calls == [(name, {}, host.socket_path, None)], name
    finally:
        host.close()


