"""Public workflow CLI: graph plans, bound identity and explicit outcomes."""

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

DOC = "version: 2\nname: cli-work\nnodes: [{id: work, task: Implement the task.}]\n"


@pytest.fixture
def workflow_file(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text(DOC)
    return path


def test_plan_expands_effective_policy_and_ownership(workflow_file):
    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "plan", str(workflow_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)["plan"]
    assert plan["onFailure"] == "terminate"
    assert plan["nodes"][0]["ownership"] == "workflow"
    assert "await" not in plan


def test_run_posts_verified_identity_and_operation_key(monkeypatch, workflow_file):
    calls = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **kw: (
            calls.append((method, params))
            or {
                "graphId": "wf-test",
                "runId": "wf-test",
                "actingPrincipal": "user:hyprial-unit-owner",
            }
        ),
    )
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "workflow",
            "run",
            str(workflow_file),
            "--operation-key",
            "business-task",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    method, params = calls[0]
    assert method == "workflow.start"
    assert params == {
        "yaml": DOC,
        "actor": "user:hyprial-unit-owner",
        "operationKey": "business-task",
    }
    assert json.loads(result.stdout)["actingPrincipal"] == "user:hyprial-unit-owner"


def test_run_rejects_impersonation_before_ipc(monkeypatch, workflow_file):
    calls = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: calls.append(a))
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "workflow",
            "run",
            str(workflow_file),
            "--from",
            "agent:other:node:actor",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "PAC_PRINCIPAL_UNVERIFIED"
    assert calls == []


def test_managed_worker_carries_its_binding(monkeypatch, workflow_file):
    calls = []
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:test:node:actor")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-test")
    monkeypatch.setenv("HYPRIAL_MANAGED_WORKER", "1")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **kw: (
            calls.append((method, params))
            or {
                "graphId": "wf-test",
                "runId": "wf-test",
                "actingPrincipal": "agent:test:node:actor",
            }
        ),
    )
    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1]["actor"] == "agent:test:node:actor"
    assert calls[0][1]["sessionRef"] == "session-test"
    assert json.loads(result.stdout)["actingPrincipal"] == "agent:test:node:actor"


def test_interactive_carrier_file_carries_verified_identity(
    monkeypatch, workflow_file, tmp_path
):
    calls = []
    binding_file = tmp_path / "cli-principal.json"
    binding_file.write_text("session-interactive", encoding="utf-8")
    binding_file.chmod(0o600)
    monkeypatch.setenv("HYPRIAL_AGENT_SESSION", "1")
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:test:node:interactive")
    monkeypatch.delenv("HYPRIAL_WORKER_SESSION_REF", raising=False)
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF_FILE", str(binding_file))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **kw: (
            calls.append((method, params))
            or {
                "graphId": "wf-test",
                "runId": "wf-test",
                "actingPrincipal": "agent:migrated-owner:node:interactive",
            }
        ),
    )

    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["actor"] == "agent:test:node:interactive"
    assert calls[0][1]["sessionRef"] == "session-interactive"
    assert json.loads(result.stdout)["actingPrincipal"] == (
        "agent:migrated-owner:node:interactive"
    )


def test_unsafe_interactive_binding_file_fails_closed(
    monkeypatch, workflow_file, tmp_path
):
    calls = []
    binding_file = tmp_path / "world-readable.ref"
    binding_file.write_text("session-interactive", encoding="utf-8")
    binding_file.chmod(0o644)
    monkeypatch.setenv("HYPRIAL_AGENT_SESSION", "1")
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:test:node:interactive")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF_FILE", str(binding_file))
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *args, **kwargs: calls.append(args))

    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "PAC_PRINCIPAL_UNVERIFIED"
    assert calls == []


def test_legacy_yaml_cannot_dispatch(monkeypatch, tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text("version: 1\nname: old\ntask: old\ntargets: [actor]\n")
    calls = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: calls.append(a))
    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(path), "--yes", "--json"]
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "WORKFLOW_SCHEMA_ERROR"
    assert calls == []


def test_status_history_cancel_and_outcomes_use_graph_methods(monkeypatch):
    calls = []

    def request(method, params, **kw):
        calls.append((method, params))
        return {"runs": []} if method == "workflow.list" else {"state": "completed"}

    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        request,
    )
    runner = CliRunner()
    for command in (
        ["inspect", "wf-test"],
        ["list"],
        ["cancel", "wf-test"],
        ["history", "status", "legacy-test"],
        ["history", "list"],
        [
            "complete",
            "wf-test",
            "work",
            "--request-id",
            "request",
            "--reason-ref",
            "result:test",
        ],
        [
            "fail",
            "wf-test",
            "work",
            "--request-id",
            "request",
            "--reason-ref",
            "failure:test",
            "--output-text",
            "inline failure",
        ],
    ):
        result = runner.invoke(_cli_root_app, ["workflow", *command, "--json"])
        assert result.exit_code == 0, result.output
    assert [m for m, p in calls] == [
        "workflow.status",
        "workflow.list",
        "workflow.cancel",
        "workflow.history.status",
        "workflow.history.list",
        "workflow.complete",
        "workflow.fail",
    ]
    assert all(p["actor"] == "user:hyprial-unit-owner" for m, p in calls)
    assert calls[-1][1]["requestId"] == "request"
    assert calls[-1][1]["outputText"] == "inline failure"
    assert "outputText" not in calls[-2][1]


def test_inspect_context_reads_the_local_snapshot_without_ipc(monkeypatch, tmp_path):
    import hyprial.daemon as daemon

    seen = {}

    def fake_node_context(path, graph_id, node_id):
        seen.update(path=path, graph_id=graph_id, node_id=node_id)
        return {
            "version": 3,
            "cursor": 7,
            "briefRef": "brief:x",
            "completedCount": 2,
            "currentActivation": {"round": 1},
        }

    monkeypatch.setattr(daemon, "node_context", fake_node_context)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: pytest.fail("context used IPC"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(
        _cli_root_app,
        ["workflow", "inspect", "g-1", "--node", "n-1", "--context", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["completedCount"] == 2
    assert seen["graph_id"] == "g-1" and seen["node_id"] == "n-1"
    assert seen["path"] == tmp_path / "pac-graph.sqlite3"


def test_inspect_context_requires_node_without_ipc(monkeypatch):
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: pytest.fail("context used IPC"))

    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "inspect", "g-1", "--context", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("action", ["complete", "fail"])
def test_workflow_outcome_reports_agent_acting_principal(monkeypatch, action):
    calls = []
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:test:node:actor")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-test")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **kw: calls.append((method, params))
        or {
            "ok": True,
            "actingPrincipal": "agent:migrated-owner:node:actor",
        },
    )
    command = [
        "workflow",
        action,
        "wf-test",
        "work",
        "--request-id",
        "request",
        "--reason-ref",
        "result:test",
        "--json",
    ]

    result = CliRunner().invoke(_cli_root_app, command)

    assert result.exit_code == 0, result.output
    assert calls[0][1]["actor"] == "agent:test:node:actor"
    assert calls[0][1]["sessionRef"] == "session-test"
    assert json.loads(result.stdout)["actingPrincipal"] == (
        "agent:migrated-owner:node:actor"
    )


def test_list_human_table_is_bounded_plain_text_and_all_reaches_daemon(monkeypatch):
    calls = []
    now_ms = 10_000_000
    monkeypatch.setattr(_cli_get_services().time, "time_ns", lambda: now_ms * 1_000_000)

    def request(method, params, **_kw):
        calls.append((method, params))
        assert method == "workflow.list"
        return {
            "runs": [
                {
                    "graphId": "wf-1234567890abcdef",
                    "name": "[red] " + "very-long-name-" * 10,
                    "state": "running",
                    "sender": "agent:test:node:builder",
                    "createdAtMs": now_ms - 3 * 60 * 60 * 1000,
                    "lastProgressAtMs": now_ms - 17 * 60 * 1000,
                    "currentNode": {"nodeId": "review", "state": "requested"},
                }
            ]
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(_cli_root_app, ["workflow", "list", "--all"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "hyprial workflow list   1 runs (all callers)"
    assert "GRAPH" in result.stdout
    assert "PROGRESS" in result.stdout
    assert "wf-123456789" in result.stdout
    assert "[red]" in result.stdout
    assert "3h" in result.stdout
    assert "17m" in result.stdout
    assert "review:requested" in result.stdout
    assert all(len(line) <= 120 for line in result.stdout.splitlines())
    assert calls[0][1]["all"] is True
