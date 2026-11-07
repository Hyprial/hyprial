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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]

SECRET = "app-secret-must-never-be-printed-4a1d"


def _env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
    }
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    return environment


def _run(
    home: Path, *arguments: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", *arguments],
        cwd=ROOT,
        env=_env(home),
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


def test_add_refuses_an_existing_name_until_forced(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    refused = _payload(
        _run(home, "route", "add", "demo", "allen-dm=oc_zzz", "--json")
    )
    assert refused["ok"] is False
    assert refused["code"] == "ROUTE_EXISTS"
    # Refusal is a no-op, not a partial write.
    assert _channels(home)["gateways"][0]["routes"][0]["nativeId"] == "oc_aaa"

    forced = _payload(
        _run(home, "route", "add", "demo", "allen-dm=oc_zzz", "--force", "--json")
    )
    assert forced["ok"] is True
    assert forced["adapter"]["routes"][0]["nativeId"] == "oc_zzz"
    assert _route_names(forced) == ["allen-dm"]


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
    _run(home, "route", "add", "demo", "allen-dm=oc_aaa", "--force", "--default")

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
    # Clearing the default is stated in the output, never implied.
    assert "defaultRoute" not in forced["adapter"]


def test_remove_reports_an_unbound_route_by_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    payload = _payload(_run(home, "route", "remove", "demo", "nope", "--json"))

    assert payload["ok"] is False
    assert payload["code"] == "ROUTE_NOT_FOUND"
    # The message names what IS bound, so the caller can fix it in one step.
    assert "allen-dm" in payload["error"]


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


def test_list_reports_every_adapter_when_no_name_is_given(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed(home, "allen-dm=oc_aaa")

    payload = _payload(_run(home, "route", "list", "--json"))

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
    assert payload["candidates"] == [{"chatId": "oc_dm", "chatType": "p2p"}]
    # Another adapter's namespace is not this adapter's evidence.
    other = _payload(_run(home, "route", "candidates", "other", "--json"))
    assert other["adapter"] == "other"
    assert other["candidates"] == []


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
