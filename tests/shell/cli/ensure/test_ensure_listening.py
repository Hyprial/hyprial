"""Nothing is retried: a failed connect starts the daemon, then one request (Z-4).

Decision: daemon-first-z4-ensure-listening-20261117.  A daemon that accepts the
connection and answers ``DAEMON_UNAVAILABLE`` is never restarted; the opt-out,
``ensure_daemon=False`` and agent sessions change neither stdout nor stderr.
"""

from __future__ import annotations

import json

import pytest
import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.output import json_failure

from .conftest import unavailable


@pytest.fixture(autouse=True)
def _autostart_allowed(monkeypatch):
    monkeypatch.delenv("HYPRIAL_DAEMON_AUTOSTART", raising=False)


def _run(env, method="agent.list", json_output=True, **kwargs):
    outcome = {}

    def operation():
        outcome["value"] = env.services.runtime.request(method)
        return outcome["value"]

    support._execute(operation, json_output=json_output, **kwargs)
    return outcome


def _fails(env, capsys, **kwargs):
    with pytest.raises(typer.Exit):
        _run(env, **kwargs)
    return capsys.readouterr()


def test_no_socket_starts_the_daemon_once_then_sends_one_request(ensure_env, capsys):
    ensure_env.script = [{"agents": []}]

    outcome = _run(ensure_env)

    assert outcome["value"] == {"agents": []}
    assert len(ensure_env.launches) == 1
    launch = ensure_env.launches[0]
    assert launch["implicit"] is True and launch["autoinstall"] is False
    assert launch["ready_timeout"] == support.DAEMON_START_IDLE_BUDGET_SECONDS
    assert ensure_env.requests == ["agent.list"]
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"event": "daemon-ensure"}
    assert json.loads(captured.out)["agents"] == []


def test_a_stale_socket_file_starts_the_daemon(ensure_env):
    ensure_env.listen(stale=True)
    ensure_env.script = [{"agents": []}]

    _run(ensure_env)

    assert len(ensure_env.launches) == 1 and ensure_env.requests == ["agent.list"]


def test_the_human_notice_goes_to_stderr(ensure_env, capsys):
    _run(ensure_env, json_output=False)

    captured = capsys.readouterr()
    assert "Starting the daemon" in captured.err and "Starting the daemon" not in captured.out


def test_a_second_failure_after_the_start_is_reported_without_a_retry(ensure_env, capsys):
    ensure_env.script = [unavailable()]

    captured = _fails(ensure_env, capsys)

    assert json.loads(captured.out)["code"] == ipc_errors.DAEMON_UNAVAILABLE
    assert len(ensure_env.launches) == 1 and ensure_env.requests == ["agent.list"]


def test_a_live_daemon_answering_unavailable_is_never_restarted(ensure_env, capsys):
    ensure_env.listen()
    ensure_env.script = [unavailable()]

    captured = _fails(ensure_env, capsys)

    assert json.loads(captured.out)["code"] == ipc_errors.DAEMON_UNAVAILABLE
    assert captured.err == ""
    assert ensure_env.launches == [] and ensure_env.requests == ["agent.list"]


def test_a_failed_launch_surfaces_its_own_error_and_sends_nothing(ensure_env, capsys):
    ensure_env.launch_error = ensure_env.services.CliError("DAEMON_START_FAILED", "daemon failed to start")

    captured = _fails(ensure_env, capsys)

    assert json.loads(captured.out)["code"] == "DAEMON_START_FAILED"
    assert ensure_env.requests == []


def test_outside_a_scope_nothing_starts(ensure_env):
    ensure_env.script = [unavailable()]

    with pytest.raises(ipc_errors.DaemonUnavailableError):
        support._daemon_request("agent.list")

    assert ensure_env.launches == []


@pytest.mark.parametrize("method", ["ping", "shutdown"])
def test_the_readiness_probe_and_shutdown_never_start_a_daemon(ensure_env, capsys, method):
    ensure_env.script = [unavailable()]

    captured = _fails(ensure_env, capsys, method=method)

    assert json.loads(captured.out)["code"] == ipc_errors.DAEMON_UNAVAILABLE
    assert ensure_env.launches == []


def test_the_launch_does_not_recurse(ensure_env):
    def launching(**kwargs):
        ensure_env.launches.append(kwargs)
        try:
            support._daemon_request("agent.list")
        except ipc_errors.DaemonUnavailableError:
            pass

    ensure_env.script = [unavailable(), {"ok": True}]
    ensure_env.services._launch_daemon_process = launching

    _run(ensure_env)

    assert len(ensure_env.launches) == 1
    assert ensure_env.requests == ["agent.list", "agent.list"]


EXPECTED_ABSENT = json_failure(unavailable())


def _absent_output(env, capsys, **kwargs):
    env.script = [unavailable()]
    captured = _fails(env, capsys, **kwargs)
    assert env.launches == [] and env.requests == ["agent.list"]
    return captured


def test_ensure_daemon_false_output_is_exactly_the_unchanged_failure(ensure_env, capsys):
    captured = _absent_output(ensure_env, capsys, ensure_daemon=False)

    assert captured.out == json.dumps(EXPECTED_ABSENT, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert captured.err == ""


def test_the_opt_out_output_is_exactly_the_unchanged_failure(ensure_env, capsys, monkeypatch):
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")

    captured = _absent_output(ensure_env, capsys)

    assert captured.out == json.dumps(EXPECTED_ABSENT, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert captured.err == ""


def test_the_opt_out_human_output_is_unchanged(ensure_env, capsys, monkeypatch):
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")
    ensure_env.script = [unavailable()]

    captured = _fails(ensure_env, capsys, json_output=False)

    assert captured.out == "" and "Starting the daemon" not in captured.err
    assert "daemon socket is not reachable" in captured.err


@pytest.mark.parametrize(
    "environment",
    [
        {"HYPRIAL_WORKER_ACTOR": "agent://example/worker", "HYPRIAL_WORKER_SESSION_REF": "session-1"},
        {"HYPRIAL_WORKER_ACTOR": "agent://example/worker"},
        {"HYPRIAL_MANAGED_WORKER": "1"},
    ],
    ids=["bound", "partial-actor", "partial-marker"],
)
def test_an_agent_session_output_is_exactly_the_unchanged_failure(ensure_env, capsys, monkeypatch, environment):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    captured = _absent_output(ensure_env, capsys)

    assert captured.out == json.dumps(EXPECTED_ABSENT, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert captured.err == ""
