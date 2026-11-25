"""Every error code a group method declares reaches the ``hyprial message group`` user.

The codes are read from the method declarations, so a code added to a
method is covered here without editing this file.  A refusal keeps its code
and data in ``--json`` (``ok: false``) and its message in the human output,
with exit status 1, through the CLI's one error boundary.  A transient code
(``DAEMON_UNAVAILABLE``) arrives as its registered transient class, the way
the request port raises it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.support import CliError
from tests.shell.cli.groups.support import GROUP_ID, declared_codes, example, invoke, record

COMMANDS = {
    "conversations.create": (("create", "--title", "release", "--member", "worker"), {}),
    "conversations.get": (("show", GROUP_ID), {}),
    "conversations.list": (("list",), {}),
    "conversations.members.add": (("members", "add", GROUP_ID, "reviewer"), {}),
    "conversations.members.remove": (
        ("members", "remove", GROUP_ID, "reviewer"),
        {"conversations.get": example("conversations.get", "release-group")},
    ),
}
CASES = [
    pytest.param(method, code, id=f"{method}-{code}")
    for method in COMMANDS
    for code in declared_codes(method)
]
DATA = {"conversationId": GROUP_ID}


def _refusal(code: str) -> Exception:
    message = f"refused with {code}"
    transient = ipc_errors.transient_error_from_code(code, message, DATA)
    return transient if transient is not None else CliError(code, message, DATA)


def _refuse(code: str) -> Any:
    def answer(_params: dict) -> Any:
        raise _refusal(code)

    return answer


def test_the_transient_case_is_exercised() -> None:
    assert any(
        ipc_errors.transient_error_from_code(code, "x") is not None
        for method in COMMANDS
        for code in declared_codes(method)
    )


@pytest.mark.parametrize(("method", "code"), CASES)
def test_json_keeps_the_code_and_data(
    monkeypatch: pytest.MonkeyPatch, method: str, code: str
) -> None:
    args, before = COMMANDS[method]
    record(monkeypatch, {**before, method: _refuse(code)})

    result = invoke(*args, "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == code
    assert payload["data"] == DATA


@pytest.mark.parametrize(("method", "code"), CASES)
def test_human_output_names_the_refusal(
    monkeypatch: pytest.MonkeyPatch, method: str, code: str
) -> None:
    args, before = COMMANDS[method]
    record(monkeypatch, {**before, method: _refuse(code)})

    result = invoke(*args)

    assert result.exit_code == 1, result.output
    assert f"refused with {code}" in result.output
