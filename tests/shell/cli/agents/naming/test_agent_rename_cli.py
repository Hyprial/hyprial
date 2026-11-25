"""UUID addressing lane C: ``hyprial agent rename`` and the ``display(a.id)`` view.

Rename sends the target and the new display name to ``agent.rename`` and shows
the renamed row; registry refusals keep their own codes.  The human views of
``agent list`` and ``agent rename`` name each agent as its display name
followed by its ``a.`` id; ``--json`` stays the daemon's answer, unchanged.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

AGENT_ID = "a.jbnq2egbjqrt5pqaxjr2rzcapq"


def _row(actor: str, *, display: str | None, agent_id: str | None = AGENT_ID) -> dict[str, Any]:
    return {
        "uri": f"agent:op:node-1:{actor}",
        "actor": actor,
        "owner": "op",
        "machine": "node-1",
        "status": "offline",
        "agentId": agent_id,
        "displayName": display,
    }


class _Daemon:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, method: str, params: Any = None, **_kwargs: Any) -> Any:
        self.calls.append((method, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return deepcopy(self.answer)


def _install(monkeypatch: pytest.MonkeyPatch, answer: Any) -> _Daemon:
    daemon = _Daemon(answer)
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)
    return daemon


def _invoke(*arguments: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(root_app, ["agent", *arguments], color=False)


def test_rename_sends_the_target_and_the_new_display_name(monkeypatch) -> None:
    daemon = _install(
        monkeypatch, {"ok": True, "agent": _row("worker", display="reviewer")}
    )
    result = _invoke("rename", AGENT_ID, "reviewer")
    assert result.exit_code == 0, result.output
    assert daemon.calls == [
        ("agent.rename", {"name": AGENT_ID, "displayName": "reviewer"})
    ]
    assert f"reviewer({AGENT_ID})" in result.output


def test_rename_json_is_the_daemon_answer(monkeypatch) -> None:
    answer = {"ok": True, "agent": _row("worker", display="reviewer")}
    _install(monkeypatch, answer)
    result = _invoke("rename", "worker", "reviewer", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer


@pytest.mark.parametrize(
    "code",
    [
        ipc_errors.AGENT_EXISTS,
        "INVALID_AGENT_NAME",
        ipc_errors.AGENT_NOT_FOUND,
    ],
)
def test_rename_refusals_keep_their_code(monkeypatch, code) -> None:
    def refuse(method: str, params: Any = None, **_kwargs: Any) -> Any:
        assert method == "agent.rename"
        raise RequestPortError(code, f"refused with {code}")

    monkeypatch.setattr(cli_support, "daemon_request", refuse)
    result = _invoke("rename", "worker", "taken", "--json")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == code


def test_rename_is_listed_after_unblock_in_the_agent_help() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0, result.output
    assert "rename" in result.output
    assert result.output.index("unblock") < result.output.index("rename")


def test_agent_list_shows_display_followed_by_the_id(monkeypatch) -> None:
    answer = {
        "ok": True,
        "agents": [
            _row("worker", display="reviewer"),
            _row("plain", display=None, agent_id="a.aaaaaaaaaaaaaaaaaaaaaaaaaa"),
            _row("legacy", display=None, agent_id=None),
        ],
        "agentsWithoutRoutine": [],
    }
    _install(monkeypatch, answer)
    result = _invoke("list")
    assert result.exit_code == 0, result.output
    assert f"reviewer({AGENT_ID})" in result.output
    assert "plain(a.aaaaaaaaaaaaaaaaaaaaaaaaaa)" in result.output
    assert "legacy(" not in result.output


def test_agent_list_json_carries_the_fields_unchanged(monkeypatch) -> None:
    answer = {
        "ok": True,
        "agents": [_row("worker", display="reviewer")],
        "agentsWithoutRoutine": [],
    }
    _install(monkeypatch, answer)
    result = _invoke("list", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer
