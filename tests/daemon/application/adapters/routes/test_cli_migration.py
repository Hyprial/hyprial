"""The CLI routes configured adapter work through ``adapter.routes.*`` only.

Decision F1a (2026-11-23): the IPC-only ``adapter.route-config.*`` twin is
retired; its names are unknown to the registry and the CLI no longer calls them.
"""

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.application.adapters.authorization.test_status import app as app_fixture
from tests.shell.cli.adapters.test_cli_adapter_route import _channels, _payload, _run, _seed

app = app_fixture

RETIRED = (
    ("adapter.route-config.list", {}),
    ("adapter.route-config.candidates", {"name": "demo"}),
    ("adapter.route-config.add", {"name": "demo", "route": "r", "chatId": "c", "makeDefault": False, "force": False}),
    ("adapter.route-config.remove", {"name": "demo", "route": "r", "force": False}),
)


@pytest.mark.parametrize("method,params", RETIRED)
def test_retired_route_config_twin_is_an_unknown_method(app, method, params):
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
    from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS, ROUTER_ONLY_METHODS

    assert METHOD_REGISTRY.get(method) is None
    assert method not in MIGRATED_METHODS
    assert method not in ROUTER_ONLY_METHODS
    with pytest.raises(DaemonRequestError) as error:
        app.handle(method, params)
    assert error.value.code == ipc_errors.METHOD_NOT_FOUND


def test_offline_cli_refuses_without_changing_configuration(tmp_path):
    home = tmp_path / "home"
    _seed(home, "first=oc_aaa")
    before = _channels(home)
    result = _payload(_run(home, "route", "add", "demo", "second=oc_bbb", "--json"))
    assert result["ok"] is False
    assert result["code"] in {ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_UNAVAILABLE}
    assert _channels(home) == before


def test_interactive_peer_refused_before_writer(app, monkeypatch):
    from hyprial.daemon.impl.desired_state import InteractiveSession
    from hyprial.daemon.impl.mcp.channel import ownership

    app.desired_state.register_interactive(InteractiveSession(
        actor="agent:owner:node:interactive", cwd=str(app.hyprial_home), command=("codex",),
        source="codex-tui", session_ref="session-interactive", process_pid=4242,
        process_identity="birth:interactive"))
    app._ipc_peer.pid = 4242
    monkeypatch.setattr(ownership, "_read_process_identity", lambda pid: "birth:interactive" if pid == 4242 else None)
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.routes.list", {"name": "demo"})
    assert error.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_actual_cli_routes_over_isolated_daemon():
    from tests.shell.cli.adapters.test_cli_adapter_online import _cli, _online_daemon

    from hyprial.kernel import RequestPortError, daemon_request_once

    with _online_daemon(seed_adapter=True) as (home, environment):
        old = (home / "secrets" / "lark-feishu.json").read_bytes()
        listed = _cli(environment, "adapter", "route", "list", "feishu")
        with pytest.raises(RequestPortError) as retired:
            daemon_request_once("adapter.route-config.list", {}, socket_path=home / "daemon.sock", timeout=5)
        assert retired.value.code == ipc_errors.METHOD_NOT_FOUND
        assert [adapter["name"] for adapter in listed["adapters"]] == ["feishu"]
        assert "credentialRef" not in listed["adapters"][0]
        saved = _cli(environment, "adapter", "route", "add", "feishu", "ops=oc_changed")
        assert [route["nativeId"] for route in saved["adapter"]["routes"]][-1] == "oc_changed"
        assert saved["configured"] is True
        assert (home / "secrets" / "lark-feishu.json").read_bytes() == old
