"""``hyprial agent start user-proxy --name <n> -- --route route:<adapter>:<route>``.

The route is the person's DM: where everyone else's messages are relayed.  It
is the only runtime argument, it is validated before anything is created, and
it is baked into the packaged worker's command (the daemon never guesses it).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ROUTE = "route:allen-feishu:allen-dm"


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, dict[str, object]]]:
    seen: list[tuple[str, dict[str, object]]] = []

    def daemon(method: str, params: dict[str, object] | None = None, **_: object) -> dict[str, object]:
        seen.append((method, dict(params or {})))
        if method == "agent.create":
            return {"ok": True, "created": True, "agent": {"name": (params or {})["name"]}}
        return {"ok": True, "id": "user-proxy:allen-proxy"}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    return seen


def _start(*args: str):
    return CliRunner().invoke(
        _cli_root_app, ["agent", "start", "user-proxy", "--name", "allen-proxy", "--json", *args]
    )


def test_start_launches_the_packaged_relay_with_the_route(calls) -> None:
    result = _start("--", "--route", ROUTE)
    assert result.exit_code == 0, result.output

    methods = [method for method, _ in calls]
    assert methods == ["agent.create", "lifecycle.start"]
    create = calls[0][1]
    assert create.get("provider") is None and create.get("model") is None
    start = calls[1][1]
    assert start["provider"] == "user-proxy"
    assert start["headless"] is True
    assert start["args"] == []
    command = start["command"]
    assert command[1:] == [
        "-m",
        "hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker",
        "--kind",
        "user-proxy",
        "--route",
        ROUTE,
    ]
    assert "model" not in start and "modelProvider" not in start


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--", "--route"),
        ("--", ROUTE),
        ("--", "--route", "allen-dm"),
        ("--", "--route", "route:only-two"),
        ("--", "--route", ROUTE, "extra"),
        ("--model", "x", "--", "--route", ROUTE),
    ],
)
def test_a_bad_start_creates_nothing(calls, args: tuple[str, ...]) -> None:
    result = _start(*args)
    assert result.exit_code != 0
    assert calls == []
