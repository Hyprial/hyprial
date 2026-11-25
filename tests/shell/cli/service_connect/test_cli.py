from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError
from hyprial.shell.impl.cli.commands.service import commands


class FakeServices:
    CliError = RequestPortError

    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.runtime = SimpleNamespace(
            request=self._daemon_request,
            execute=self._execute,
        )

    def _daemon_request(self, method: str, params: dict[str, object], **_: object) -> Any:
        self.requests.append((method, params))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def _execute(self, operation, *, json_output: bool, **_: object) -> None:
        from hyprial.shell.impl.cli.output import run_command

        run_command(operation, json_output=json_output, require_home=None)


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    args: list[str],
    *,
    input: str | None = None,
):
    services = FakeServices(response)
    monkeypatch.setattr(commands, "get_services", lambda: services)
    result = CliRunner().invoke(commands.service_app, args, input=input)
    return result, services


def _json(result) -> dict[str, Any]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _mapping(*, state: str = "listening", path: str = "direct") -> dict[str, object]:
    return {
        "name": "api",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "recordGeneration": 7,
        "localPort": 18080,
        "activeConnections": 0,
        "path": path,
        "pathDetail": None,
        "observedAtMs": 1791292000000,
        "lastDialState": "ok",
        "lastDialMs": 12,
        "lastError": None,
        "lastErrorAtMs": None,
        "state": state,
        "errorCode": None if state == "listening" else "SERVICE_UNAVAILABLE",
    }


def test_connect_uses_exact_rpc_and_emits_one_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "service": _mapping(),
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18080"},
        "auth": "create an application key separately",
        "guide": "Use the loopback endpoint.",
    }
    result, services = _invoke(monkeypatch, response, ["connect", "api", "--json"])

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, **response}
    assert services.requests == [("service.connect", {"name": "api"})]


@pytest.mark.parametrize(
    ("args", "method", "params", "response"),
    [
        (["disconnect", "api", "--json"], "service.disconnect", {"name": "api"}, {"name": "api", "removed": True}),
        (["list", "--json"], "service.list", {}, {"services": [], "registry": {"source": "builtin", "error": None}}),
        (["guide", "api", "--json"], "service.guide", {"name": "api"}, {"entry": {"name": "api"}, "source": "builtin"}),
    ],
)
def test_each_subcommand_has_its_frozen_rpc_shape(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    method: str,
    params: dict[str, object],
    response: dict[str, object],
) -> None:
    result, services = _invoke(monkeypatch, response, args)

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, **response}
    assert services.requests == [(method, params)]


def test_bare_service_is_help_and_never_probes_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    result, services = _invoke(monkeypatch, {}, [])

    assert result.exit_code == 2, result.stderr
    assert "connect" in result.output
    assert "disconnect" in result.output
    assert "list" in result.output
    assert "guide" in result.output
    assert "status" not in result.output
    assert services.requests == []


@pytest.mark.parametrize("option", ["--target", "--tailnet", "--address", "--bind", "--inject-env"])
def test_connect_has_no_legacy_or_arbitrary_target_options(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    result, services = _invoke(monkeypatch, {}, ["connect", "api", option, "value"])

    assert result.exit_code == 2
    assert services.requests == []


def test_daemon_service_error_is_json_error_with_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RequestPortError("SERVICE_NOT_REGISTERED", "service is not registered")
    result, services = _invoke(monkeypatch, error, ["connect", "missing", "--json"])

    assert result.exit_code == 1
    assert _json(result) == {
        "ok": False,
        "code": "SERVICE_NOT_REGISTERED",
        "error": "service is not registered",
    }
    assert services.requests == [("service.connect", {"name": "missing"})]


_GRANT = {
    "grantId": "sg." + "a" * 26,
    "service": "api",
    "deviceId": "sub2api",
    "recipientKeyId": "x25519:" + "b" * 32,
    "createdAt": 1795443172228,
}


def test_grant_reads_the_secret_only_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    recipient = "x25519:" + "a" * 64
    result, services = _invoke(
        monkeypatch,
        {"grant": _GRANT},
        ["grant", "api", "--recipient-key", recipient, "--json"],
        input="s3cret-value\n",
    )

    assert result.exit_code == 0, result.stderr
    assert _json(result) == {"ok": True, "grant": _GRANT}
    assert services.requests == [
        (
            "service.grant",
            {
                "name": "api",
                "recipientKey": recipient,
                "secretB64": base64.b64encode(b"s3cret-value").decode("ascii"),
            },
        )
    ]
    assert "s3cret-value" not in result.output


def test_grant_never_accepts_the_secret_as_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipient = "x25519:" + "a" * 64
    result, services = _invoke(
        monkeypatch,
        {"grant": _GRANT},
        ["grant", "api", "s3cret-value", "--recipient-key", recipient],
    )

    assert result.exit_code == 2
    assert services.requests == []


def test_grant_refuses_an_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    recipient = "x25519:" + "a" * 64
    result, services = _invoke(
        monkeypatch,
        {"grant": _GRANT},
        ["grant", "api", "--recipient-key", recipient, "--json"],
        input="\n",
    )

    assert result.exit_code == 2
    assert services.requests == []

