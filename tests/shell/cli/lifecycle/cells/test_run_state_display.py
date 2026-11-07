"""Slice D: one shared diagnostic view, no new run-state authority."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.runtime import entry as cli

CORPUS = json.loads((Path(__file__).parents[5] / "contract/daemon-lifecycle/run-state-display-vectors.json").read_text())


def _row(case):
    row = {"name": case["name"], "actor": f"agent:owner:node:{case['name']}",
           "uri": f"agent:owner:node:{case['name']}", "runtime": "pi",
           "status": "online", "state": "ok", "processState": "running",
           "workerState": "blocked", "restoreStatus": "restored", "pid": None,
           "pendingCount": 3, "error": "legacy text does not establish run state"}
    if case["runState"] is not None:
        row["runState"] = {**deepcopy(CORPUS["defaults"]), **deepcopy(case["runState"])}
    return row


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["name"])
@pytest.mark.parametrize("command", ["list", "top"])
def test_human_commands_share_golden_diagnostics(monkeypatch, case, command):
    row = _row(case)
    payload = {"ok": True, "agents" if command == "list" else "actors": [row]}
    before = deepcopy(payload)
    calls = []

    def request(method, *args, **kwargs):
        calls.append(method)
        return payload

    monkeypatch.setattr(cli_support, "_daemon_request", request)
    monkeypatch.setattr(get_services(), "_sample_process_table", lambda: {})
    result = CliRunner().invoke(cli.app, ["agent", command], color=False)
    assert result.exit_code == 0, result.output
    assert case["display"] in result.output
    assert calls == ["agent.list" if command == "list" else "top.snapshot"]
    assert payload == before  # renderers must not edit the response/legacy fields
    if command == "top":
        assert "RUN STATE" in result.output
        assert "LEGACY" in result.output
        assert "not run state" in result.output


@pytest.mark.parametrize("command", ["list", "top"])
@pytest.mark.parametrize("old_daemon", [False, True])
def test_json_is_one_unchanged_object_without_sampling(monkeypatch, command, old_daemon):
    cases = CORPUS["cases"][-1:] if old_daemon else CORPUS["cases"]
    payload = {"ok": True, "daemon": {"pid": 12},
               "agents" if command == "list" else "actors": [_row(c) for c in cases],
               "priorField": {"retain": [1, 2, 3]}}

    def forbidden():
        pytest.fail("JSON diagnostics must not sample host processes")

    monkeypatch.setattr(cli_support, "_daemon_request", lambda *a, **k: payload)
    monkeypatch.setattr(get_services(), "_sample_process_table", forbidden)
    result = CliRunner().invoke(cli.app, ["agent", command, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert result.stderr == ""


@pytest.mark.parametrize("command", ["list", "top"])
@pytest.mark.parametrize("json_output", [False, True])
def test_unavailable_is_error_not_empty_fleet(monkeypatch, command, json_output):
    def unavailable(*a, **k):
        raise ipc_errors.DaemonUnavailableError("isolated test socket unavailable")

    monkeypatch.setattr(cli_support, "_daemon_request", unavailable)
    result = CliRunner().invoke(cli.app, ["agent", command, *(["--json"] if json_output else [])])
    assert result.exit_code == 1, result.output
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["code"] == "DAEMON_UNAVAILABLE"
        assert "agents" not in payload and "actors" not in payload
    else:
        assert result.stdout == ""
        assert "isolated test socket unavailable" in result.stderr


@pytest.mark.parametrize("value", [True, -1, "2000", None])
def test_unknown_age_is_not_inferred_from_timestamps(value):
    from hyprial.shell.impl.cli.output.status import format_run_state

    run = {**CORPUS["defaults"], "state": "in-turn", "reason": "turn-open",
           "turnAgeMs": value, "sinceMs": 1, "observedAtMs": 1000000}
    assert "turn-age=-" in format_run_state(run)


def test_bounded_labels_do_not_render_raw_provider_or_permission_details():
    from hyprial.shell.impl.cli.output.status import format_run_state

    run = {**CORPUS["defaults"], "state": "blocked", "reason": "private prompt",
           "suspicions": ["private tool arguments"], "permissionDetails": "private answer"}
    text = format_run_state(run)
    assert "private" not in text
    assert "unknown" in text


@pytest.mark.parametrize("payload", [None, {}, {"agents": None}, {"agents": "idle"}])
def test_invalid_list_response_is_not_an_empty_fleet(monkeypatch, payload):
    monkeypatch.setattr(cli_support, "_daemon_request", lambda *a, **k: payload)
    result = CliRunner().invoke(cli.app, ["agent", "list", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_RESPONSE"


def test_agent_list_filters_and_other_fields_are_preserved(monkeypatch):
    seen = []

    def request(method, params):
        seen.append((method, params))
        return {"ok": True, "agents": [], "agentsWithoutRoutine": ["agent:o:n:a"]}

    monkeypatch.setattr(cli_support, "_daemon_request", request)
    result = CliRunner().invoke(cli.app, ["agent", "list", "--inactive-since", "2h", "--exclude-wf"])
    assert result.exit_code == 0, result.output
    assert seen == [("agent.list", {"inactiveSince": "2h", "excludeWf": True})]
    assert "agent:o:n:a" in result.stdout
