"""Failures and Ctrl-C of a daemon-run first login.

Decision: daemon-first-z5-cli-failures-20261119.  Every terminal state of the
daemon's operation maps to one published error; a refusal of ``auth.login.begin``
is surfaced as the daemon wrote it; Ctrl-C sends exactly one
``auth.operation.cancel`` and, when the daemon answers that the identity is already
committed, ends with exit 130 and a notice instead of waiting on.
"""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import RequestPortError, ipc_errors

from .support import OPERATION_ID, answer


def _failure(world, result):
    payload = world.payload(result)
    assert result.exit_code == 1, (result.stdout, result.stderr)
    assert payload["ok"] is False
    return payload


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("denied", "ACCESS_DENIED"),
        ("expired", "DEVICE_CODE_EXPIRED"),
        ("cancelled", "CANCELLED"),
        ("unknown", "SUBMIT_OUTCOME_UNKNOWN"),
    ],
)
def test_a_terminal_state_maps_to_one_published_error(make_world, state, code):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [answer(state=state, stage="stopped", device=None, owner=None)]

    payload = _failure(world, world.login("--no-open"))

    assert payload["code"] == code
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.get"]
    assert world.launches == []


def test_a_stopped_daemon_after_the_commit_names_the_start_that_completes_it(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    failure = {"code": "DAEMON_ERROR", "message": "the first login stopped before the daemon was serving"}
    world.answers["auth.operation.get"] = [answer(stage="stopped", failure=failure)]

    payload = _failure(world, world.login("--no-open"))

    assert payload["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert payload["data"]["identityCommitted"] is True
    assert payload["data"]["failure"] == failure
    assert payload["data"]["nextSteps"] == ["hyprial daemon start"]


def test_a_wait_that_ends_without_a_terminal_stage_is_unknown_and_is_not_asked_again(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [answer(state="authenticated", stage="starting", device=None)]

    payload = _failure(world, world.login("--no-open"))

    assert payload["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert payload["data"]["stage"] == "starting"
    assert payload["data"]["nextSteps"] == ["hyprial daemon status"]
    assert world.methods.count("auth.operation.get") == 1


@pytest.mark.parametrize("code", [ipc_errors.IDENTITY_CONFLICT, ipc_errors.OPERATION_IN_PROGRESS])
def test_a_refused_begin_is_the_daemons_answer_and_nothing_else_is_asked(make_world, code):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.login.begin"] = [RequestPortError(code, "daemon text", {"reason": "x"})]

    payload = _failure(world, world.login("--no-open"))

    assert (payload["code"], payload["error"], payload["data"]) == (code, "daemon text", {"reason": "x"})
    assert world.methods == ["ping", "auth.login.begin"]


def test_an_interrupt_sends_exactly_one_cancel_and_ends_as_interrupted(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [KeyboardInterrupt()]

    result = world.login("--no-open")

    payload = world.payload(result)
    assert result.exit_code == 1 and payload["code"] == "INTERRUPTED"
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.get", "auth.operation.cancel"]
    assert world.calls[-1].params == {"operationId": OPERATION_ID}
    assert "cancelled" in result.stderr


@pytest.mark.parametrize("json_output", [True, False])
def test_an_interrupt_after_the_commit_exits_130_with_a_notice_and_does_not_wait(make_world, json_output):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [KeyboardInterrupt()]
    world.answers["auth.operation.cancel"] = [
        {"ok": True, "operationId": OPERATION_ID, "state": "authenticated", "cancelled": False,
         "reason": "identity-committed"}
    ]

    result = world.login("--no-open", json_output=json_output)

    assert result.exit_code == 130, (result.stdout, result.stderr)
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.get", "auth.operation.cancel"]
    assert "the daemon is finishing" in result.stderr
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False and payload["code"] == "INTERRUPTED"
        assert payload["data"]["identityCommitted"] is True
    else:
        assert result.stdout == ""


def test_a_cancel_that_cannot_be_sent_still_ends_the_command_once(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [KeyboardInterrupt()]
    world.answers["auth.operation.cancel"] = [ipc_errors.DaemonUnavailableError("gone")]

    result = world.login("--no-open")

    assert result.exit_code == 1 and world.payload(result)["code"] == "INTERRUPTED"
    assert world.methods.count("auth.operation.cancel") == 1


def _one_cancel(world, result, *, json_output=True):
    assert result.exit_code == 1, (result.stdout, result.stderr)
    if json_output:
        assert world.payload(result)["code"] == "INTERRUPTED"
    else:
        assert "login cancelled" in result.stderr
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.cancel"]
    assert world.calls[-1].params == {"operationId": OPERATION_ID}


def test_an_interrupt_while_opening_the_browser_sends_exactly_one_cancel(make_world, monkeypatch):
    import hyprial.shell.impl.login.flow as login_flow

    world = make_world(cell="ownerless", phase="bootstrap")

    def interrupted(url):
        raise KeyboardInterrupt

    monkeypatch.setattr(login_flow, "open_verification_uri", interrupted, raising=False)

    _one_cancel(world, world.login())


def test_an_interrupt_while_showing_the_code_sends_exactly_one_cancel(make_world, monkeypatch):
    from hyprial.shell.impl.cli.commands.common.services import get_services

    world = make_world(cell="ownerless", phase="bootstrap")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(get_services(), "resolve_profile", interrupted)

    _one_cancel(world, world.login("--no-open", json_output=False), json_output=False)


@pytest.mark.parametrize(
    "failure",
    [ipc_errors.IpcTimeoutError("no answer"), ipc_errors.DaemonDisconnectedError("peer closed")],
    ids=["timeout", "disconnected"],
)
def test_a_wait_whose_transport_fails_is_an_unknown_outcome_and_is_not_retried(make_world, failure):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [failure]

    payload = _failure(world, world.login("--no-open"))

    assert payload["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert payload["data"]["nextSteps"] == ["hyprial daemon status", "hyprial login"]
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.get"]
