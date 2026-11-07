"""Template registration through the real routine facade/registry/store.

Only the CLI IPC transport is replaced. Responses come from the production
facade, not invented response dictionaries; no source or PAC graphs here.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from typer.testing import CliRunner

from hyprial.biz.impl.routine.contracts.schema import RoutineSchemaError, load_routine_text
from hyprial.biz.impl.routine.execution.service import RoutineService, RoutineServiceError
from hyprial.biz.impl.routine.storage.store import RoutineStore
from hyprial.biz.impl.routine.templates import render_template
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "agent:test:isolated:coordinator"


@pytest.fixture
def registration(monkeypatch, tmp_path):
    # No cadence is submitted. Any accidental PAC/alarm IO fails loudly.
    service = RoutineService(pac=object(), alarm=object(), state_dir=tmp_path)
    calls = []

    def request(method, params, **_kwargs):
        calls.append((method, params))
        assert method == "routine.add"
        try:
            return service.add(yaml_text=params["yaml"], owner=params["actor"])
        except RoutineServiceError as error:
            raise _cli_get_services().CliError(error.code, str(error)) from error

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    try:
        yield service, calls
    finally:
        service.close()


def add_args(*extra):
    return ["routine", "add", "--template", "selfdrive", "--for", OWNER,
            "--escalate-to", "user:test", "--json", *extra]


@pytest.mark.parametrize("source", ["pac-journal", "taskwarrior"])
def test_template_add_matches_handwritten_yaml_plan(registration, tmp_path, source):
    service, calls = registration
    extra = ["--name", "watch", "--interval", "2m", "--source", source]
    source_yaml = "{kind: pac-journal}"
    if source == "taskwarrior":
        extra += ["--filter", "+selfdrive project:local -blocked"]
        source_yaml = '{kind: taskwarrior, filter: "+selfdrive project:local -blocked"}'
    result = CliRunner().invoke(_cli_root_app, add_args(*extra))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["enabled"] is True
    assert service.status(name="watch")["owner"] == "user:hyprial-unit-owner"
    text = calls[0][1]["yaml"]
    expected = '''version: 2
mode: source
name: watch
schedule: {interval: 2m}
source: SOURCE
policy:
  routes:
    - {tag: "route:worker", target_from: tagarg}
    - {tag: "route:self", target: self}
    - {tag: "route:human", escalate_to: "user:test"}
  default: escalate
  task_template: |
    你被 routine 唤醒:{{reason}}
    【自驱任务】{{task.description}}
    (task {{task.uuid}},标签:{{task.tags}})
    完成以下方给出的 `hyprial workflow complete` 为准;⛔ 回复文字不算完成。
    做不了使用下方的 `hyprial workflow fail` 明确报告失败;回复文字不改变任务状态。
limits:
  max_in_flight: 3
  circuit_breaker: {window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}
on_task_timeout: {action: escalate, escalate_to: "user:test"}
produces: agent:test:isolated:coordinator
'''.replace("SOURCE", source_yaml)
    assert load_routine_text(text) == load_routine_text(expected)
    plans = []
    for index, content in enumerate([text, expected]):
        path = tmp_path / f"plan-{index}.yaml"
        path.write_text(content)
        result = CliRunner().invoke(_cli_root_app, ["routine", "plan", str(path), "--json"])
        assert result.exit_code == 0, result.output
        plans.append(json.loads(result.stdout))
    assert plans[0] == plans[1]


def test_template_default_and_taskwarrior_default_filter(registration):
    service, calls = registration
    result = CliRunner().invoke(_cli_root_app, add_args())
    assert result.exit_code == 0, result.output
    spec = load_routine_text(calls[0][1]["yaml"])
    assert (spec.name, spec.source_kind, spec.interval_seconds) == ("selfdrive", "pac-journal", 900)
    assert spec.source_idle_threshold_seconds == 1800
    assert service.list()["routines"][0]["enabled"] is True
    result = CliRunner().invoke(_cli_root_app, add_args("--name", "board", "--source", "taskwarrior"))
    assert result.exit_code == 0, result.output
    assert load_routine_text(calls[1][1]["yaml"]).source_filter == "+selfdrive -blocked"


def test_same_name_refused_without_overwriting_paused_state(registration):
    service, calls = registration
    runner = CliRunner()
    assert runner.invoke(_cli_root_app, add_args()).exit_code == 0
    service.pause(name="selfdrive")
    before = service.status(name="selfdrive")
    result = runner.invoke(_cli_root_app, add_args("--interval", "1m"))
    assert result.exit_code == 1, result.output
    assert "ROUTINE_EXISTS" in result.stdout
    assert service.status(name="selfdrive") == before


def test_concurrent_same_name_admission_has_one_winner(registration):
    service, _ = registration
    text = render_template("selfdrive", owner=OWNER, escalate_to="user:test")

    def register(_):
        try:
            service.add(yaml_text=text, owner=OWNER)
            return "added"
        except RoutineServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(register, range(4)))
    assert sorted(results) == ["ROUTINE_EXISTS"] * 3 + ["added"]


def test_same_name_refused_after_registry_restart(tmp_path):
    text = render_template("selfdrive", owner=OWNER, escalate_to="user:test")
    first = RoutineService(pac=object(), alarm=object(), state_dir=tmp_path)
    try:
        first.add(yaml_text=text, owner=OWNER)
        first.pause(name="selfdrive")
        before = first.status(name="selfdrive")
    finally:
        first.close()
    second = RoutineService(pac=object(), alarm=object(), state_dir=tmp_path)
    try:
        with pytest.raises(RoutineServiceError) as raised:
            second.add(yaml_text=text, owner="agent:test:isolated:other")
        assert raised.value.code == "ROUTINE_EXISTS"
        assert second.status(name="selfdrive") == before
    finally:
        second.close()


@pytest.mark.parametrize("args", [
    ["routine", "add", "--template", "selfdrive", "--json"],
    add_args("--from", "other"),
    add_args("--source", "unknown"),
    add_args("--filter", "+selfdrive"),
    add_args("--interval", "1s"),
    add_args("--name", ""),
    ["routine", "add", "--template", "selfdrive", "--for", "user:test", "--escalate-to", "user:test", "--json"],
    ["routine", "add", "--template", "selfdrive", "--for", OWNER, "--escalate-to", OWNER, "--json"],
    ["routine", "add", "--template", "unknown", "--for", OWNER, "--escalate-to", "user:test", "--json"],
    ["routine", "add", "missing.yaml", "--template", "selfdrive", "--json"],
    ["routine", "add", "missing.yaml", "--from", OWNER, "--source", "taskwarrior", "--json"],
])
def test_invalid_template_options_never_call_daemon(registration, args):
    _, calls = registration
    result = CliRunner().invoke(_cli_root_app, args)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["ok"] is False
    assert calls == []


def test_template_declares_and_projects_coordinator_ownership(registration, tmp_path):
    service, _ = registration
    text = render_template("selfdrive", owner=OWNER, escalate_to="user:test")
    spec = load_routine_text(text)
    assert spec.produces == OWNER
    service.add(yaml_text=text, owner=OWNER)
    assert service.status(name="selfdrive")["owner"] == OWNER
    assert service.status(name="selfdrive")["produces"] == OWNER
    store = RoutineStore(tmp_path / "routines.sqlite3")
    try:
        assert yaml.safe_load(store.snapshot("selfdrive").routine.yaml_text)["produces"] == OWNER
    finally:
        store.close()


@pytest.mark.parametrize("value", ["user:test", "short-name", "null", "[]"])
def test_produces_requires_actor_uri(value):
    text = render_template("selfdrive", owner=OWNER, escalate_to="user:test")
    with pytest.raises(RoutineSchemaError, match="produces"):
        load_routine_text(text + f"produces: {value}\n")
