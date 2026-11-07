"""Disposable policy, graph, and git fixtures for PAC expansion I5."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from hyprial.daemon.impl.pac.contracts.expansion.policy import load_expansion_policy
from hyprial.daemon.impl.pac.contracts.expansion.validation import validate_parent
from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.graphs import (
    compile_workflow,
    input_token,
)


USER = "user:test"
PLANNER = "agent:test:machine:planner"


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "pac-expansion@example.invalid")
    _git(repo, "config", "user.name", "PAC Expansion Test")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def policy_home(tmp_path: Path, repo: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    work_root = tmp_path / "managed"
    work_root.mkdir()
    (home / "pac-expansion.json").write_text(
        json.dumps(
            {
                "version": 1,
                "workRoot": str(work_root),
                "limits": {
                    "maxNodes": 4,
                    "tiers": ["fast", "strong", "super"],
                    "owners": [PLANNER],
                    "cwdUnder": [str(work_root)],
                },
            }
        ),
        encoding="utf-8",
    )
    return home, work_root


def parent_graph(
    tmp_path: Path,
    *,
    child_nodes: int = 2,
) -> tuple[PacGraphStore, str, str, Path, Path, str]:
    repo, base_oid = repository(tmp_path)
    home, work_root = policy_home(tmp_path, repo)
    parent = load_workflow_text(
        f"""
version: 2
name: expandable
nodes:
  - id: planner
    owner: {PLANNER}
    role: plan
    expands: slot
    task: plan the child
  - id: slot
    kind: expansion
    after: [planner]
    timeout: 10m
    limits:
      maxNodes: {child_nodes}
      worktree:
        repo: {repo}
        base: HEAD
"""
    )
    parent = validate_parent(parent, policy=load_expansion_policy(home))
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(state / "pac-graph.sqlite3")
    graph_id = compile_workflow(
        store,
        parent,
        sender=USER,
        machine="machine",
        local_owner="test",
        operation_key="parent",
        at=100,
    )
    token = input_token(store, graph_id, "planner")
    assert token is not None
    with store.write() as db:
        db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id='request-1',"
            "input_token=?,generation=1,deadline_ms=1000000000000000 "
            "WHERE graph_id=? AND node_id='planner'",
            (token, graph_id),
        )
    return store, graph_id, token, home, work_root, base_oid


def child_document(count: int = 2) -> str:
    nodes = [
        {
            "id": f"work{index}",
            "task": f"do work {index}",
            "role": "execute",
            "launch": {"tier": "fast"},
            "timeout": "2m",
        }
        for index in range(1, count + 1)
    ]
    return json.dumps({"version": 2, "nodes": nodes})


def close_store(store: PacGraphStore) -> None:
    store.close()
