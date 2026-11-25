"""Execute the PR-close workflow against an isolated HTTP Actions authority."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/ops-cancel-on-pr-close.yml"
REPO = "HyprialOS/harness-bridge"
BRANCH = "hotfix/example"
TOKEN = "isolated-actions-token"


def run_record(run_id, status="running", event="pull_request", branch=BRANCH, **extra):
    return {
        "id": run_id,
        "event": event,
        "status": status,
        "workflow_id": "gate-python.yml",
        "prettyref": "#123",
        "event_payload": json.dumps({
            "number": 123,
            "pull_request": {
                "number": 123,
                "head": {"ref": branch, "repo": {"full_name": REPO}},
            },
        }),
        **extra,
    }


@pytest.fixture
def authority():
    state = {"pages": {}, "runs": {}, "posts": [], "gets": [], "failures": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code, body):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            assert self.headers.get("Authorization") == f"token {TOKEN}"
            parsed = urlsplit(self.path)
            state["gets"].append(self.path)
            if parsed.path.endswith("/runs"):
                if "list" in state["failures"]:
                    return self.respond(*state["failures"]["list"])
                page = int(parse_qs(parsed.query)["page"][0])
                records = state["pages"].get(page, [])
                return self.respond(200, {
                    "workflow_runs": records,
                    "total_count": sum(len(p) for p in state["pages"].values()),
                })
            run_id = int(parsed.path.rsplit("/", 1)[1])
            if ("get", run_id) in state["failures"]:
                return self.respond(*state["failures"][("get", run_id)])
            return self.respond(200, state["runs"][run_id])

        def do_POST(self):
            assert self.headers.get("Authorization") == f"token {TOKEN}"
            assert self.path.endswith("/cancel")
            run_id = int(self.path.split("/")[-2])
            state["posts"].append(run_id)
            if run_id in state["failures"]:
                return self.respond(*state["failures"][run_id])
            state["runs"][run_id]["status"] = state.get("post_status", {}).get(run_id, "cancelled")
            return self.respond(204, "")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["url"] = f"http://127.0.0.1:{server.server_port}/api/v1"
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)


def seed(state, *records):
    state["pages"] = {1: list(records)}
    state["runs"] = {r["id"]: copy.deepcopy(r) for r in records}


def execute(state, tmp_path, **overrides):
    """An absent close handler has no cancellation effects on the baseline."""
    script = ""
    if WORKFLOW.exists():
        workflow = yaml.safe_load(WORKFLOW.read_text())
        script = workflow["jobs"]["cancel"]["steps"][0]["run"]
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_API_URL": state["url"],
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_RUN_ID": "999",
            "GITHUB_STEP_SUMMARY": str(summary),
            "PR_HEAD_REF": BRANCH,
            "PR_HEAD_REPO": REPO,
            "PR_NUMBER": "123",
            **overrides,
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    text = summary.read_text() if summary.exists() else ""
    assert TOKEN not in result.stdout + result.stderr + text
    assert all(TOKEN not in p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    return result, text


def test_workflow_trigger_serialization_and_token_boundary():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow.get("on", workflow.get(True)) == {"pull_request": {"types": ["closed"]}}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "pull_request.number" in workflow["concurrency"]["group"]
    step = workflow["jobs"]["cancel"]["steps"][0]
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert step["env"]["PR_HEAD_REF"] == "${{ github.event.pull_request.head.ref }}"
    assert len(workflow["jobs"]["cancel"]["steps"]) == 1
    assert "|| true" not in step["run"]
    assert "permissions" not in workflow


def test_cancels_only_unfinished_pr_head_runs_and_replay_is_noop(authority, tmp_path):
    seed(authority,
         run_record(1), run_record(2, "waiting"), run_record(3, "success"),
         run_record(4, event="push", branch="dev"), run_record(5, event="push"),
         run_record(6, branch="other"), run_record(7, workflow_id="ops-cancel-on-pr-close.yml"),
         run_record(8, "cancelled"), run_record(9, event="pull_request_review"))
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == [1, 2, 9]
    assert "canceled=[1,2,9]" in summary
    assert "skipped=" in summary and "not-pull-request" in summary
    authority["pages"][1] = list(authority["runs"].values())
    _, summary = execute(authority, tmp_path)
    assert authority["posts"] == [1, 2, 9]
    assert "canceled=[]" in summary


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "running"])
def test_accepts_active_statuses(authority, tmp_path, status):
    seed(authority, run_record(1, status))
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == [1]
    assert "run_id=1 HTTP=204" in summary


def test_snapshots_all_pages_before_cancelling_and_deduplicates(authority, tmp_path):
    records = [run_record(i) for i in range(1, 52)]
    authority["pages"] = {1: records[:50], 2: [records[49], records[50]]}
    authority["runs"] = {r["id"]: r for r in records}
    result, _ = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == list(range(1, 52))
    assert any("page=2" in path for path in authority["gets"])
    assert all("status=" not in path for path in authority["gets"])


def test_rechecks_finished_and_changed_records_before_post(authority, tmp_path):
    seed(authority, run_record(1), run_record(2))
    authority["runs"][1]["status"] = "success"
    authority["runs"][2]["event"] = "push"
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == []
    assert "canceled=[]" in summary


@pytest.mark.parametrize("code", [403, 404, 500])
def test_cancel_failure_records_raw_body_and_fails_after_remaining_runs(authority, tmp_path, code):
    seed(authority, run_record(1), run_record(2))
    raw = '{"message":"cancel denied <raw>"}\n'
    authority["failures"][1] = (code, raw)
    result, summary = execute(authority, tmp_path)
    assert result.returncode != 0
    assert authority["posts"] == [1, 2]
    assert f"run_id=1 HTTP={code}" in summary
    assert raw in summary
    assert "canceled=[2]" in summary


def test_list_failure_is_reported_and_cancels_nothing(authority, tmp_path):
    seed(authority, run_record(1))
    authority["failures"]["list"] = (500, "raw listing failure")
    result, summary = execute(authority, tmp_path)
    assert result.returncode != 0
    assert authority["posts"] == []
    assert "raw listing failure" in summary and "HTTP=500" in summary


def test_forks_and_ambiguous_identity_are_skipped(authority, tmp_path):
    seed(authority, run_record(1, is_fork_pull_request=True),
         run_record(2, event_payload="not json"),
         run_record(3, event_payload=json.dumps({"number": 456})))
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == []
    assert "skipped=" in summary
    result, summary = execute(authority, tmp_path, PR_HEAD_REPO="outsider/fork")
    assert result.returncode == 0, result.stderr
    assert "fork-pr" in summary


def test_native_head_branch_and_trigger_event_fallback(authority, tmp_path):
    seed(authority, run_record(1, event="", trigger_event="pull_request", head_branch=BRANCH))
    result, _ = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == [1]


def test_completion_racing_cancel_is_not_counted_as_canceled(authority, tmp_path):
    seed(authority, run_record(1))
    authority["post_status"] = {1: "success"}
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == [1]
    assert "canceled=[]" in summary
    assert "completed-during-cancel:success" in summary


def test_unconfirmed_cancellation_fails_without_retrying_post(authority, tmp_path):
    seed(authority, run_record(1))
    authority["post_status"] = {1: "running"}
    result, summary = execute(authority, tmp_path)
    assert result.returncode != 0
    assert authority["posts"] == [1]
    assert "canceled=[]" in summary and "cancellation not confirmed" in summary


@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped", "blocked", "unknown", "completed"])
def test_finished_or_unapproved_status_is_never_cancelled(authority, tmp_path, status):
    seed(authority, run_record(1, status))
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == []
    assert "canceled=[]" in summary


def test_recheck_failure_and_credential_echo_are_reported_safely(authority, tmp_path):
    seed(authority, run_record(1), run_record(2))
    authority["failures"][("get", 1)] = (403, "raw detail denied")
    authority["failures"][2] = (500, f"denied {TOKEN}")
    result, summary = execute(authority, tmp_path)
    assert result.returncode != 0
    assert authority["posts"] == [2]
    assert "raw detail denied" in summary and "denied [REDACTED]" in summary


def test_same_head_branch_on_another_pr_or_fork_is_not_cancelled(authority, tmp_path):
    other_pr = run_record(1)
    payload = json.loads(other_pr["event_payload"])
    payload["pull_request"]["number"] = 456
    other_pr["event_payload"] = json.dumps(payload)
    fork = run_record(2)
    payload = json.loads(fork["event_payload"])
    payload["pull_request"]["head"]["repo"]["full_name"] = "outsider/fork"
    fork["event_payload"] = json.dumps(payload)
    seed(authority, other_pr, fork)
    result, summary = execute(authority, tmp_path)
    assert result.returncode == 0, result.stderr
    assert authority["posts"] == []
    assert "different-pr" in summary and "head-repository-mismatch" in summary
