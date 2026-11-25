"""Tests for ``hyprial adapter route`` — binding routes on an existing adapter.

Runs the CLI as a subprocess so the real argv/stdout surface is exercised, with
an isolated ``HYPRIAL_HOME``/``HARNESS_STATE_DIR`` under ``tmp_path`` and
``HYPRIAL_ZENOH_LISTEN`` stripped, so nothing can touch a real ``~/.hyprial``.

The property under test is narrow and load-bearing: a route change must edit
``channels.json`` and *nothing else*. The pre-existing way to add a route was
``adapter add --force``, which replaces the whole gateway entry and rewrites the
credential file; several tests here assert the credential's bytes are untouched,
because that is the difference that motivated the command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import signal
import time

import pytest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]

SECRET = "app-secret-must-never-be-printed-4a1d"
_DAEMONS = None


@pytest.fixture(autouse=True)
def _route_daemons():
    global _DAEMONS
    _DAEMONS = {}
    yield
    for daemon in _DAEMONS.values():
        if daemon.poll() is None:
            daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=20)
    _DAEMONS = None


def _start_route_daemon(home: Path, environment: dict[str, str]) -> None:
    if _DAEMONS is None or home in _DAEMONS:
        return
    daemon = subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
        cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _DAEMONS[home] = daemon
    deadline = time.monotonic() + 30
    while not (home / "daemon.sock").exists():
        assert daemon.poll() is None, "isolated daemon exited during startup"
        assert time.monotonic() < deadline, "isolated daemon did not start"
        time.sleep(0.05)



def _env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
        "HYPRIAL_OWNER": "route-test",
        "HYPRIAL_NODE_ID": "route-test-node",
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_LARK_BASE_URL": "http://127.0.0.1:9",
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_CONNECT": "",
    }
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    return environment


def _run(
    home: Path, *arguments: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = _env(home)
    if arguments and arguments[0] == "route":
        _start_route_daemon(home, environment)
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", *arguments],
        cwd=ROOT,
        env=environment,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _seed(home: Path, *routes: str) -> dict[str, Any]:
    arguments = ["add", "demo", "--app-id", "cli_demo"]
    for route in routes:
        arguments += ["--route", route]
    result = _run(home, *arguments, "--json", stdin=SECRET + "\n")
    payload = _payload(result)
    assert payload["ok"] is True, payload
    return payload


def _route_names(payload: dict[str, Any]) -> list[str]:
    return [route["name"] for route in payload["adapter"]["routes"]]


def _channels(home: Path) -> dict[str, Any]:
    return json.loads((home / "channels.json").read_text(encoding="utf-8"))


def _credential_bytes(home: Path) -> bytes:
    return (home / "secrets" / "lark-demo.json").read_bytes()


def test_add_binds_a_second_route_without_touching_the_credential(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")
    before = _credential_bytes(home)

    payload = _payload(_run(home, "route", "add", "demo", "team=oc_bbb", "--json"))

    assert payload["ok"] is True
    assert _route_names(payload) == ["allen-dm", "team"]
    # The first route stays the default: binding a route is not a re-routing.
    assert payload["adapter"]["defaultRoute"] == "allen-dm"
    assert _credential_bytes(home) == before
    stored = _channels(home)["gateways"][0]
    assert [route["name"] for route in stored["routes"]] == ["allen-dm", "team"]
    assert stored["appId"] == "cli_demo"


def test_add_refuses_an_existing_name_and_points_at_remove(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    refused = _payload(
        _run(home, "route", "add", "demo", "allen-dm=oc_zzz", "--json")
    )
    assert refused["ok"] is False
    assert refused["code"] == "ROUTE_EXISTS"
    assert "hyprial adapter route remove demo allen-dm" in refused["error"]
    assert _channels(home)["gateways"][0]["routes"][0]["nativeId"] == "oc_aaa"

    retired = _run(home, "route", "add", "demo", "allen-dm=oc_zzz", "--force", "--json")
    assert retired.returncode != 0

    assert _payload(_run(home, "route", "remove", "demo", "allen-dm", "--force", "--json"))["ok"] is True
    rebound = _payload(
        _run(home, "route", "add", "demo", "allen-dm=oc_zzz", "--json")
    )
    assert rebound["ok"] is True
    assert rebound["adapter"]["routes"][0]["nativeId"] == "oc_zzz"
    assert _route_names(rebound) == ["allen-dm"]


def test_add_with_default_moves_the_gateway_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    payload = _payload(
        _run(home, "route", "add", "demo", "ops=oc_ccc", "--default", "--json")
    )

    assert payload["adapter"]["defaultRoute"] == "ops"
    assert _route_names(payload) == ["allen-dm", "ops"]


def test_remove_unbinds_one_route(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa", "team=oc_bbb")
    before = _credential_bytes(home)

    payload = _payload(_run(home, "route", "remove", "demo", "team", "--json"))

    assert payload["ok"] is True
    assert _route_names(payload) == ["allen-dm"]
    assert _credential_bytes(home) == before


def test_remove_refuses_the_default_route_until_forced(tmp_path: Path) -> None:
    home = tmp_path / "home"
    seeded = _seed(home, "allen-dm=oc_aaa", "team=oc_bbb")
    assert seeded["adapter"].get("defaultRoute") is None
    assert _payload(_run(home, "route", "remove", "demo", "allen-dm", "--json"))["ok"] is True
    assert _payload(_run(home, "route", "add", "demo", "allen-dm=oc_aaa", "--default", "--json"))["ok"] is True

    refused = _payload(
        _run(home, "route", "remove", "demo", "allen-dm", "--json")
    )
    assert refused["ok"] is False
    assert refused["code"] == "ROUTE_IN_USE"
    assert _channels(home)["gateways"][0]["defaultRoute"] == "allen-dm"

    forced = _payload(
        _run(home, "route", "remove", "demo", "allen-dm", "--force", "--json")
    )
    assert forced["ok"] is True
    assert _route_names(forced) == ["team"]
    assert forced["adapter"]["defaultRoute"] is None


def test_remove_reports_an_unbound_route_by_name(tmp_path: Path) -> None:
    """The message names the read that shows what is bound, without a second request."""
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    payload = _payload(_run(home, "route", "remove", "demo", "nope", "--json"))

    assert payload["ok"] is False
    assert payload["code"] == "ROUTE_NOT_FOUND"
    assert "hyprial adapter route list demo" in payload["error"]


def test_unknown_adapter_reports_adapter_not_found(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    for arguments in (
        ("route", "add", "missing", "x=oc_x", "--json"),
        ("route", "remove", "missing", "x", "--json"),
        ("route", "list", "missing", "--json"),
    ):
        payload = _payload(_run(home, *arguments))
        assert payload["ok"] is False, arguments
        assert payload["code"] == "ADAPTER_NOT_FOUND", arguments


def test_malformed_route_argument_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")
    before = (home / "channels.json").read_bytes()

    payload = _payload(_run(home, "route", "add", "demo", "no-equals-sign", "--json"))

    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"
    assert (home / "channels.json").read_bytes() == before


def test_list_reports_the_named_adapter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    payload = _payload(_run(home, "route", "list", "demo", "--json"))

    assert payload["ok"] is True
    names = [adapter["name"] for adapter in payload["adapters"]]
    assert names == ["demo"]
    assert payload["adapters"][0]["routes"][0]["nativeId"] == "oc_aaa"


def test_candidates_report_the_chats_inbound_traffic_came_from(tmp_path: Path) -> None:
    """The first route's target is read from the adapter's own observations.

    ``route candidates`` is how the machine answers "which chat should the first
    route point at" without a person copying an id: the user messages the bot
    once and the id is already recorded.  Read-only, and it must not fabricate a
    database for an adapter that never received anything.
    """

    from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
    from hyprial.daemon.impl.adapters.lark.state.records import RequestCorrelation

    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    empty = _payload(_run(home, "route", "candidates", "demo", "--json"))
    assert empty["ok"] is True
    assert empty["adapter"] == "demo"
    assert empty["candidates"] == []
    assert not (home / "adapters.sqlite3").exists()

    store = LarkStateStore(home / "adapters.sqlite3", adapter="lark:demo")
    store.record_inbound(
        RequestCorrelation(
            harness_message_id="hm-1",
            message_id="om-1",
            chat_id="oc_dm",
            conversation_id="lark:oc_dm:oc_dm",
        ),
        event_id="evt-1",
    )
    store.record_chat_type("oc_dm", "p2p")
    store.close()

    payload = _payload(_run(home, "route", "candidates", "demo", "--json"))

    assert payload["ok"] is True
    assert payload["candidates"] == [{"chatId": "oc_dm", "chatType": "p2p", "chatName": None}]
    other = _payload(_run(home, "route", "candidates", "other", "--json"))
    assert other["ok"] is False
    assert other["code"] == "ADAPTER_NOT_FOUND"


def test_route_output_never_contains_the_app_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    for arguments in (
        ("route", "add", "demo", "team=oc_bbb", "--json"),
        ("route", "list", "demo", "--json"),
        ("route", "remove", "demo", "team", "--json"),
    ):
        result = _run(home, *arguments)
        assert SECRET not in result.stdout, arguments
        assert SECRET not in result.stderr, arguments


def test_list_requires_an_adapter_name(tmp_path: Path) -> None:
    """No daemon request returns every adapter's routes, so the CLI does not loop."""

    home = tmp_path / "home"

    result = _run(home, "route", "list", "--json")

    assert result.returncode != 0
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "name" in payload["error"]


@pytest.mark.parametrize("arguments,method,params", [
    (("add", "demo", "ops=oc_ccc", "--default"), "adapter.routes.add",
     {"name": "demo", "route": "ops", "chatId": "oc_ccc", "makeDefault": True}),
    (("remove", "demo", "ops", "--force"), "adapter.routes.remove",
     {"name": "demo", "route": "ops", "clearDefault": True}),
    (("list", "demo"), "adapter.routes.list", {"name": "demo"}),
    (("candidates", "demo"), "adapter.routes.candidates", {"name": "demo"}),
])
def test_each_route_command_sends_exactly_one_request(monkeypatch, arguments, method, params) -> None:
    """F1a review: no revision pre-read, no per-adapter loop; writes omit ``expectedRevision``."""

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.commands.common import support as cli_support
    from hyprial.shell.impl.cli.commands.common.root import app as root_app
    from hyprial.shell.impl.cli.commands.common.services import get_services
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

    calls: list[tuple[str, dict[str, Any]]] = []
    adapter = {"name": "demo", "provider": "lark", "appId": "cli_demo", "routes": [],
               "defaultRoute": None, "revision": "0" * 64}

    def daemon(name: str, request: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        calls.append((name, dict(request or {})))
        if name == "adapter.routes.candidates":
            return {"ok": True, "adapter": "demo", "candidates": []}
        return {"ok": True, "adapter": adapter}

    monkeypatch.setattr(cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(get_services(), "require_initialized_hyprial_home", lambda: None)
    result = CliRunner().invoke(root_app, ["adapter", "route", *arguments, "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [(method, params)]
