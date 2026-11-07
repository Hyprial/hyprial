from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.identity.impl.users.profile import UserProfileStore
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _prepare_home(tmp_path: Path) -> tuple[Path, UserProfileStore]:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    store = UserProfileStore(state / "users.json")
    store.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    squire = "channel:bob:receiver-mac:bob-squire"
    store.set_squire_channel("owner-bob", squire)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": name,
                        "appId": f"cli_{name}",
                        "credentialRef": f"lark-{name}",
                        "routes": [
                            {
                                "name": "owner",
                                "type": "direct",
                                "nativeId": f"oc_{name}",
                            }
                        ],
                    }
                    for name in ("bob-squire", "bob-channel")
                ],
            }
        ),
        encoding="utf-8",
    )
    return home, store


def _invoke(home: Path, *arguments: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(
        _cli_root_app,
        ["user", "proxy", "setup", *arguments, "--json"],
        env={
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(home / "state"),
            "HYPRIAL_OWNER": "bob",
        },
    )


def test_setup_refuses_the_profiles_squire_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _store = _prepare_home(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, _params, **_kwargs: calls.append(method) or {},
    )

    result = _invoke(home, "--adapter", "bob-squire", "--route", "owner")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "dedicated Lark adapter" in payload["error"]
    assert calls == []


def test_setup_refuses_adapter_pinned_to_a_different_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _store = _prepare_home(tmp_path)
    calls: list[str] = []

    def request(method: str, _params: dict[str, object], **_kwargs: object):
        calls.append(method)
        if method == "ps":
            return {
                "daemon": {"nodeId": "receiver-mac"},
                "agents": [],
                "connectors": [],
            }
        if method == "adapter.pins":
            return {"pins": {"bob-channel": "agent:bob:receiver-mac:somebody-else"}}
        raise AssertionError(f"unexpected mutation: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = _invoke(home, "--adapter", "bob-channel", "--route", "owner")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "already pinned to a different agent" in payload["error"]
    assert calls == ["ps", "adapter.pins"]


def test_setup_starts_pins_and_records_delivery_agent_then_reruns_as_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, store = _prepare_home(tmp_path)
    actor = "agent:bob:receiver-mac:owner-bob-proxy"
    state: dict[str, Any] = {
        "pins": {},
        "agents": [],
        "connectors": [],
    }
    mutations: list[str] = []

    def request(
        method: str, params: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        if method == "ps":
            return {
                "daemon": {"nodeId": "receiver-mac"},
                "agents": list(state["agents"]),
                "connectors": list(state["connectors"]),
            }
        if method == "adapter.pins":
            return {"pins": dict(state["pins"])}
        mutations.append(method)
        if method == "agent.create":
            state["agents"] = [
                {"actor": "owner-bob-proxy", "uri": actor, "status": "offline"}
            ]
            return {"created": True, "agent": state["agents"][0]}
        if method == "adapter.pin":
            state["pins"]["bob-channel"] = actor
            return {"changed": True, "actor": actor}
        if method == "lifecycle.start":
            state["connectors"] = [
                {
                    "runtime": "user-proxy",
                    "name": "owner-bob-proxy",
                    "running": True,
                }
            ]
            state["agents"][0]["status"] = "online"
            return {"ok": True, "changed": True}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(
        _cli_get_services(),
        "_stored_user_proxy_route",
        lambda _name: "route:bob-channel:owner",
    )

    first = _invoke(home, "--adapter", "bob-channel", "--route", "owner")

    assert first.exit_code == 0, first.output
    payload = json.loads(first.stdout)
    assert payload["changed"] == [
        "agent",
        "adapter.pin",
        "worker",
        "users.deliveryAgent",
    ]
    assert payload["deliveryAgent"] == "owner-bob-proxy"
    assert payload["deliveryAgentLive"] is True
    profile = store.get("owner-bob")
    assert profile is not None
    assert profile.delivery_agent == "owner-bob-proxy"
    assert mutations == ["agent.create", "adapter.pin", "lifecycle.start"]

    mutations.clear()
    second = _invoke(home, "--adapter", "bob-channel", "--route", "owner")

    assert second.exit_code == 0, second.output
    repeated = json.loads(second.stdout)
    assert repeated["changed"] == []
    assert repeated["worker"]["started"] is False
    assert repeated["worker"]["live"] is True
    assert mutations == []


def _status(home: Path):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(
        _cli_root_app,
        ["user", "proxy", "status", "--json"],
        env={
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(home / "state"),
            "HYPRIAL_OWNER": "bob",
        },
    )


def test_status_unconfigured_makes_no_daemon_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _store = _prepare_home(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, _params=None, **_kwargs: calls.append(method) or {},
    )

    result = _status(home)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "configured": False,
        "deliveryAgent": None,
        "live": False,
    }
    assert calls == []


@pytest.mark.parametrize("running", [True, False])
def test_status_configured_reads_liveness_from_one_ps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    home, store = _prepare_home(tmp_path)
    store.set_delivery_agent("owner-bob", "bob-proxy")
    calls: list[str] = []

    def fake_request(method: str, _params: Any = None, **_kwargs: Any) -> Any:
        calls.append(method)
        return {
            "connectors": [
                {"runtime": "user-proxy", "name": "bob-proxy", "running": running}
            ]
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", fake_request)

    result = _status(home)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "configured": True,
        "deliveryAgent": "bob-proxy",
        "live": running,
    }
    assert calls == ["ps"]
