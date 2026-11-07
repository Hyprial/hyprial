"""Focused CLI tests for the provider-side ``service`` leaves (R7 §2.13)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError
from hyprial.shell.impl.cli.commands.service import commands, network


class FakeServices:
    CliError = RequestPortError

    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.runtime = SimpleNamespace(
            request=self._daemon_request,
            execute=self._execute,
        )

    def _daemon_request(self, method: str, params: dict[str, object] | None = None, **_: object) -> Any:
        self.requests.append((method, params or {}))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def _execute(self, operation, *, json_output: bool, **_: object) -> None:
        from hyprial.shell.impl.cli.output import run_command

        run_command(operation, json_output=json_output, require_home=None)


def _invoke(monkeypatch: pytest.MonkeyPatch, response: Any, args: list[str]):
    services = FakeServices(response)
    monkeypatch.setattr(commands, "get_services", lambda: services)
    monkeypatch.setattr(network, "get_services", lambda: services)
    result = CliRunner().invoke(commands.service_app, args)
    return result, services


def _json(result) -> dict[str, Any]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def test_expose_uses_exact_rpc_and_returns_the_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = {
        "port": 8443,
        "target": "tcp:127.0.0.1:9443",
        "proxyProtocol": "none",
    }
    result, services = _invoke(
        monkeypatch,
        {"exposure": exposure},
        ["expose", "8443", "--target", "tcp:127.0.0.1:9443", "--proxy-protocol", "none", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, "exposure": exposure}
    assert services.requests == [
        ("network.expose", {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"})
    ]


def test_unexpose_calls_the_network_unexpose_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    result, services = _invoke(monkeypatch, {"port": 8443, "removed": True}, ["unexpose", "8443", "--json"])

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, "port": 8443, "removed": True}
    assert services.requests == [("network.unexpose", {"port": 8443})]


def test_exposures_json_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    result, services = _invoke(monkeypatch, {"exposures": [], "rejected": []}, ["exposures", "--json"])

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, "exposures": [], "rejected": []}
    assert services.requests == [("network.exposures", {})]


def test_whois_keeps_the_peer_key_rpc_and_renames_the_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, services = _invoke(
        monkeypatch,
        {"found": True, "key": "nodekey:abc", "addr": "100.64.20.8:54321"},
        ["whois", "100.64.20.8:54321", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {
        "ok": True,
        "found": True,
        "key": "nodekey:abc",
        "addr": "100.64.20.8:54321",
    }
    assert services.requests == [("network.peerKey", {"addr": "100.64.20.8:54321"})]


@pytest.mark.parametrize(
    ("args", "response"),
    [
        (["expose", "8443", "--target", "tcp:127.0.0.1:9443", "--json"], {}),
        (["unexpose", "8443", "--json"], {"port": 99}),
        (["exposures", "--json"], {"exposures": "nope"}),
        (["whois", "100.64.20.8:54321", "--json"], {"found": "yes"}),
    ],
)
def test_invalid_daemon_payloads_fail_loudly(
    monkeypatch: pytest.MonkeyPatch, args: list[str], response: dict[str, object]
) -> None:
    result, _ = _invoke(monkeypatch, response, args)

    assert result.exit_code == 1
    assert _json(result)["code"] == "INVALID_RESPONSE"


def test_retired_host_spelling_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    result, services = _invoke(monkeypatch, {}, ["host", "exposures"])

    assert result.exit_code != 0
    assert services.requests == []
