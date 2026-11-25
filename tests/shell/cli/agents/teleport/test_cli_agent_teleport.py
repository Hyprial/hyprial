"""``hyprial agent teleport AGENT --to HOST [--dry-run | --yes] [--json]``.

The command only wraps the ``teleport.run`` daemon request: one request with
``{"agent", "to", "dryRun"}`` and an IPC timeout covering the §2 budgets
(at least 335 s).  Without ``--yes`` (and not a dry run) the run needs an
interactive confirmation on stderr; non-interactive or ``--json`` callers get
``CONFIRMATION_REQUIRED`` and nothing is sent; declining is ``CANCELLED``.
``--dry-run`` together with ``--yes`` is ``INVALID_ARGUMENT``.  Decisions are
recorded in ``tests/daemon/teleport/source/fakes.py``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as root_app
from hyprial.shell.impl.cli.commands.common.services import get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

MIN_TIMEOUT = 335.0

RESULT = {
    "ok": True,
    "operationId": "op-1",
    "dryRun": False,
    "agent": "w1",
    "from": {"node": "node-a", "uri": "agent://owner-1/node-a/w1"},
    "to": {"node": "node-b", "uri": "agent://owner-1/node-b/w1"},
    "bytes": 4096,
    "routines": [],
    "commit": {"ok": True, "pausedRoutines": []},
}
DRY = {
    "ok": True,
    "operationId": "op-2",
    "dryRun": True,
    "agent": "w1",
    "from": {"node": "node-a", "uri": "agent://owner-1/node-a/w1"},
    "to": {"node": "node-b", "uri": None},
    "bytes": 0,
    "routines": [],
}


class _Daemon:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []
        self.timeouts: list[Any] = []

    def __call__(self, method: str, params: Any = None, **kwargs: Any) -> Any:
        self.calls.append((method, params))
        self.timeouts.append(kwargs.get("timeout"))
        if isinstance(self.answer, Exception):
            raise self.answer
        return deepcopy(self.answer)


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch):
    def set_tty(value: bool) -> None:
        monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: value)

    set_tty(False)
    return set_tty


def _install(monkeypatch: pytest.MonkeyPatch, answer: Any) -> _Daemon:
    daemon = _Daemon(answer)
    monkeypatch.setattr(cli_support, "daemon_request", daemon)
    return daemon


def _invoke(*arguments: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(
        root_app, ["agent", "teleport", *arguments], input=input, color=False
    )


def _one_json(stdout: str) -> dict:
    payload = json.loads(stdout)
    assert isinstance(payload, dict) and "ok" in payload
    return payload


def test_yes_sends_exactly_one_request(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    result = _invoke("w1", "--to", "node-b", "--yes", "--json")
    assert result.exit_code == 0, result.output
    assert daemon.calls == [
        ("teleport.run", {"agent": "w1", "to": "node-b", "dryRun": False})
    ]
    assert _one_json(result.stdout) == RESULT


def test_request_timeout_covers_the_protocol_budgets(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    assert _invoke("w1", "--to", "node-b", "--yes").exit_code == 0
    assert daemon.timeouts[0] is not None and daemon.timeouts[0] >= MIN_TIMEOUT


def test_dry_run_needs_no_confirmation(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, DRY)
    result = _invoke("w1", "--to", "node-b", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    assert daemon.calls == [
        ("teleport.run", {"agent": "w1", "to": "node-b", "dryRun": True})
    ]
    assert _one_json(result.stdout) == DRY


def test_dry_run_text_says_nothing_moved(monkeypatch, interactive) -> None:
    _install(monkeypatch, DRY)
    result = _invoke("w1", "--to", "node-b", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run" in result.stdout.lower()
    assert "node-b" in result.stdout


def test_success_text_names_the_target_agent(monkeypatch, interactive) -> None:
    _install(monkeypatch, RESULT)
    result = _invoke("w1", "--to", "node-b", "--yes")
    assert result.exit_code == 0, result.output
    assert "agent://owner-1/node-b/w1" in result.stdout


def test_dry_run_and_yes_are_mutually_exclusive(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    result = _invoke("w1", "--to", "node-b", "--dry-run", "--yes", "--json")
    assert result.exit_code == 1
    payload = _one_json(result.stdout)
    assert (payload["ok"], payload["code"]) == (False, "INVALID_ARGUMENT")
    assert daemon.calls == []


@pytest.mark.parametrize("json_flag", [True, False])
def test_non_interactive_run_without_yes_is_refused(
    monkeypatch, interactive, json_flag: bool
) -> None:
    daemon = _install(monkeypatch, RESULT)
    interactive(False)
    arguments = ["w1", "--to", "node-b"] + (["--json"] if json_flag else [])
    result = _invoke(*arguments)
    assert result.exit_code == 1
    assert daemon.calls == []
    if json_flag:
        payload = _one_json(result.stdout)
        assert payload["code"] == "CONFIRMATION_REQUIRED"
    else:
        assert "--yes" in result.stderr


def test_json_without_yes_is_refused_even_on_a_terminal(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    interactive(True)
    result = _invoke("w1", "--to", "node-b", "--json")
    assert result.exit_code == 1
    assert _one_json(result.stdout)["code"] == "CONFIRMATION_REQUIRED"
    assert daemon.calls == []


def test_interactive_confirmation_is_asked_on_stderr(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    interactive(True)
    result = _invoke("w1", "--to", "node-b", input="y\n")
    assert result.exit_code == 0, result.output
    assert "w1" in result.stderr and "node-b" in result.stderr
    assert daemon.calls == [
        ("teleport.run", {"agent": "w1", "to": "node-b", "dryRun": False})
    ]


def test_declined_confirmation_sends_nothing(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    interactive(True)
    result = _invoke("w1", "--to", "node-b", input="n\n")
    assert result.exit_code == 1
    assert daemon.calls == []
    assert "cancel" in result.stderr.lower()


@pytest.mark.parametrize(
    ("code", "data"),
    [
        ("TELEPORT_AGENT_EXISTS", None),
        ("AGENT_NOT_FOUND", None),
        (
            "TELEPORT_UNRESOLVED",
            {
                "operationId": "op-1",
                "targetNode": "node-b",
                "agent": "w1",
                "sourceAgentUri": "agent://owner-1/node-a/w1",
                "bytesSent": 4096,
                "sourceState": "stopped",
                "pausedRoutines": [],
                "manualCheck": "look for w1 on node-b",
            },
        ),
    ],
)
def test_daemon_refusals_exit_one_with_their_code(
    monkeypatch, interactive, code: str, data
) -> None:
    daemon = _install(monkeypatch, RequestPortError(code, f"refused {code}", data))
    result = _invoke("w1", "--to", "node-b", "--yes", "--json")
    assert result.exit_code == 1
    payload = _one_json(result.stdout)
    assert (payload["ok"], payload["code"]) == (False, code)
    if data is not None:
        assert payload["data"] == data
    assert len(daemon.calls) == 1


def test_text_refusal_exits_one_on_stderr(monkeypatch, interactive) -> None:
    _install(monkeypatch, RequestPortError("TELEPORT_WRONG_TARGET", "node-z did not answer"))
    result = _invoke("w1", "--to", "node-z", "--yes")
    assert result.exit_code == 1
    assert "node-z did not answer" in result.stderr
    assert result.stdout == ""


def test_to_is_required(monkeypatch, interactive) -> None:
    daemon = _install(monkeypatch, RESULT)
    result = _invoke("w1", "--yes")
    assert result.exit_code != 0
    assert daemon.calls == []


def test_help_lists_the_options_and_agent_help_lists_the_command() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0, result.output
    for option in ("--to", "--dry-run", "--yes", "--json"):
        assert option in result.stdout
    agent_help = CliRunner().invoke(root_app, ["agent", "--help"], color=False)
    assert "teleport" in agent_help.stdout
