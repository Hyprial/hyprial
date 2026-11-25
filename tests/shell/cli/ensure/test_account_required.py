"""``ACCOUNT_REQUIRED`` is the daemon's answer; the CLI adds the next step only (Z-4).

Decision: daemon-first-z4-account-required-20261117.  Nothing is translated and
no ping is sent: a ``METHOD_NOT_FOUND`` stays ``METHOD_NOT_FOUND``.
"""

from __future__ import annotations

import json

import pytest
import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support

from .conftest import refusal

DAEMON_TEXT = "this daemon has no account yet; sign in first"
WIRE_DATA = {"reason": "ACCOUNT_REQUIRED", "phase": "bootstrap", "method": "agent.list"}


def _failure(env, capsys, method="agent.list", **kwargs):
    with pytest.raises(typer.Exit):
        support._execute(lambda: env.services.runtime.request(method), json_output=True, **kwargs)
    return json.loads(capsys.readouterr().out)


def test_the_daemons_answer_keeps_its_text_and_gains_the_next_step(ensure_env, capsys):
    ensure_env.script = [refusal("ACCOUNT_REQUIRED", DAEMON_TEXT, dict(WIRE_DATA))]

    payload = _failure(ensure_env, capsys)

    assert payload["code"] == "ACCOUNT_REQUIRED" and payload["error"] == DAEMON_TEXT
    assert payload["data"] == {**WIRE_DATA, "nextSteps": ["hyprial login"]}
    assert ensure_env.requests == ["agent.list"]


@pytest.mark.parametrize("code", [ipc_errors.METHOD_NOT_FOUND, ipc_errors.CALLER_NOT_AUTHORIZED])
def test_other_refusals_are_untouched_and_cost_no_ping(ensure_env, capsys, code):
    ensure_env.script = [refusal(code, "daemon text")]

    payload = _failure(ensure_env, capsys)

    assert payload["code"] == code and payload["error"] == "daemon text" and "data" not in payload
    assert ensure_env.requests == ["agent.list"]


def test_ensure_daemon_false_sees_the_original_answer(ensure_env, capsys):
    ensure_env.script = [refusal("ACCOUNT_REQUIRED", DAEMON_TEXT, dict(WIRE_DATA))]

    payload = _failure(ensure_env, capsys, ensure_daemon=False)

    assert payload["data"] == WIRE_DATA


def test_the_shell_spelling_is_the_daemons_constant():
    from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import ACCOUNT_REQUIRED
    from hyprial.shell.impl.cli.commands.common.ensure.scope import ACCOUNT_REQUIRED as SHELL_SPELLING

    assert SHELL_SPELLING == ACCOUNT_REQUIRED
