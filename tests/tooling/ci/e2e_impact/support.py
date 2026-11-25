"""Disposable git snapshots for implementation-to-E2E impact checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
CHECKER = ROOT / "scripts/ci/check_e2e_impact.py"
REGISTRY = "contract/e2e/scenarios.json"


def git(repo: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        ["git", "-c", "user.name=e2e-impact-test", "-c", "user.email=e2e@example.invalid",
         "-c", "commit.gpgsign=false", *args], cwd=repo, env=environment,
        text=True, capture_output=True, timeout=10, check=True,
    )
    return result.stdout.strip()


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "snapshot")
    return git(repo, "rev-parse", "HEAD")


def registry(repo: Path) -> dict:
    return json.loads((repo / REGISTRY).read_text())


def save_registry(repo: Path, value: dict) -> None:
    write(repo, REGISTRY, json.dumps(value))


def seed(repo: Path) -> tuple[Path, str]:
    git(repo, "init", "-q")
    value = {"schema": "hyprial-e2e-scenarios/v1", "coverage_schema": "hyprial-e2e-coverage/v1", "scenarios": []}
    for number, name in ((1, "adapter"), (2, "delivery")):
        value["scenarios"].append({
            "id": f"E2E-{number:03d}", "runner": f"run-{name}.py", "document": f"{name}.md",
            "covers": [f"src/hyprial/daemon/impl/{name}/**", "contract/cli/shared.md"],
        })
        write(repo, f"src/hyprial/daemon/impl/{name}/interface.py", "def invoke(): return 1\n")
        write(repo, f"contract/e2e/run-{name}.py", "from _shared import invoke\ninvoke()\n")
        write(repo, f"contract/e2e/{name}.md", f"{name} scenario\n")
    write(repo, "contract/e2e/_shared.py", "def invoke(): return 1\n")
    write(repo, "contract/cli/shared.md", "published CLI semantics\n")
    save_registry(repo, value)
    return repo, commit(repo)


def check(repo: Path, base: str, body: str = "", *, plan: bool = False) -> tuple[subprocess.CompletedProcess, dict]:
    body_file = repo.parent / f"{repo.name}-body.txt"
    body_file.write_text(body)
    args = [sys.executable, str(CHECKER), "--repo", str(repo), "--base", base,
            "--head", "HEAD", "--pr-body-file", str(body_file)]
    if plan:
        args.append("--plan-only")
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"stdout": result.stdout, "stderr": result.stderr}
    return result, report
