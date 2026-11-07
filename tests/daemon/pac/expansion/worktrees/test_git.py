"""Behavioural tests for bounded, non-destructive PAC git effects."""

from __future__ import annotations

import inspect
import math
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.actors.worktrees import git as worktree_git
from hyprial.identity.impl.pac_errors import PacError


def _git(repo: Path, *args: str, timeout: float = 5.0) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "PAC test")
    _git(root, "config", "user.email", "pac-test@example.invalid")
    (root / "tracked.txt").write_text("base\n")
    _git(root, "add", "--", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "base")
    return root


def _plan(repo: Path, tmp_path: Path, *, node: str = "build") -> dict[str, str]:
    base = _git(repo, "rev-parse", "HEAD")
    path = tmp_path / "worktrees" / "wf-child" / node
    return {
        "graphId": "wf-child",
        "nodeId": node,
        "actorNode": "actor-build",
        "repo": str(repo),
        "baseRef": "HEAD",
        "baseOid": base,
        "branch": f"pac/wf-child/{node}",
        "path": str(path),
        "effectiveCwd": str(path),
        "operationId": f"pac-worktree:wf-child:{node}",
    }


def test_effects_require_a_positive_finite_aggregate_timeout() -> None:
    for value in (0, -1, math.inf, math.nan):
        with pytest.raises(PacError):
            worktree_git.resolve_base(Path("."), "HEAD", timeout_s=value)

    assert inspect.signature(worktree_git.resolve_base).parameters["timeout_s"].default is inspect.Parameter.empty
    assert inspect.signature(worktree_git.prepare_worktree).parameters["timeout_s"].default is inspect.Parameter.empty
    assert inspect.signature(worktree_git.cleanup_worktree).parameters["timeout_s"].default is inspect.Parameter.empty


def test_resolve_base_pins_a_local_commit(repo: Path) -> None:
    expected = _git(repo, "rev-parse", "HEAD")

    assert worktree_git.resolve_base(repo, "HEAD", timeout_s=5.0) == expected


def test_prepare_uses_pinned_oid_when_base_ref_moves(repo: Path, tmp_path: Path) -> None:
    pinned = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "base-ref")
    (repo / "tracked.txt").write_text("moved\n")
    _git(repo, "add", "--", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "move base ref")
    _git(repo, "branch", "-f", "base-ref", "HEAD")
    plan = dict(_plan(repo, tmp_path), baseRef="base-ref", baseOid=pinned)

    worktree_git.prepare_worktree(plan, timeout_s=5.0)

    assert _git(Path(plan["path"]), "rev-parse", "HEAD") == pinned


def test_prepare_registers_exact_branch_and_oid(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)

    result = worktree_git.prepare_worktree(plan, timeout_s=5.0)

    assert result == {
        "state": "prepared",
        "path": plan["path"],
        "branch": plan["branch"],
        "baseOid": plan["baseOid"],
        "reason": None,
    }
    assert _git(Path(plan["path"]), "rev-parse", "--show-toplevel") == str(
        Path(plan["path"]).resolve()
    )
    assert _git(Path(plan["path"]), "symbolic-ref", "--short", "HEAD") == plan["branch"]
    assert _git(repo, "remote") == ""


def test_prepare_retry_allows_worker_commit_after_success_before_result(
    repo: Path, tmp_path: Path
) -> None:
    plan = _plan(repo, tmp_path)
    worktree_git.prepare_worktree(plan, timeout_s=5.0)
    path = Path(plan["path"])
    (path / "worker.txt").write_text("worker\n")
    _git(path, "add", "--", "worker.txt")
    _git(path, "commit", "--quiet", "-m", "worker")

    retry = worktree_git.prepare_worktree(plan, timeout_s=5.0)

    assert retry["state"] == "prepared"
    assert _git(path, "rev-parse", "HEAD") != plan["baseOid"]


def test_prepare_never_adopts_preexisting_or_replaced_paths(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)
    path = Path(plan["path"])
    path.mkdir(parents=True)
    with pytest.raises(PacError) as existing:
        worktree_git.prepare_worktree(plan, timeout_s=5.0)
    assert existing.value.code == "PAC_WORKTREE_IDENTITY_MISMATCH"
    assert path.is_dir()

    path.rmdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    path.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(PacError) as symlink:
        worktree_git.prepare_worktree(plan, timeout_s=5.0)
    assert symlink.value.code == "PAC_WORKTREE_IDENTITY_MISMATCH"


def test_prepare_rejects_wrong_repo_branch_or_base(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)
    wrong = dict(plan, baseOid="0" * 40)
    with pytest.raises(PacError) as base_error:
        worktree_git.prepare_worktree(wrong, timeout_s=5.0)
    assert base_error.value.code == "PAC_WORKTREE_PREPARE_FAILED"

    wrong_branch = dict(plan, branch="-unsafe")
    with pytest.raises(PacError) as branch_error:
        worktree_git.prepare_worktree(wrong_branch, timeout_s=5.0)
    assert branch_error.value.code == "PAC_WORKTREE_IDENTITY_MISMATCH"


def test_cleanup_removes_only_clean_owned_tree_and_keeps_branch_and_commits(
    repo: Path, tmp_path: Path
) -> None:
    plan = _plan(repo, tmp_path)
    worktree_git.prepare_worktree(plan, timeout_s=5.0)
    path = Path(plan["path"])
    (path / "worker.txt").write_text("worker\n")
    _git(path, "add", "--", "worker.txt")
    _git(path, "commit", "--quiet", "-m", "worker")
    worker_oid = _git(path, "rev-parse", "HEAD")

    result = worktree_git.cleanup_worktree(plan, timeout_s=5.0)

    assert result["state"] == "removed"
    assert not path.exists()
    assert _git(repo, "show", "--no-patch", "--format=%H", plan["branch"]) == worker_oid
    assert worktree_git.cleanup_worktree(plan, timeout_s=5.0)["state"] == "removed"


def test_cleanup_retains_dirty_untracked_and_locked_trees(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)
    worktree_git.prepare_worktree(plan, timeout_s=5.0)
    path = Path(plan["path"])
    (path / "untracked.txt").write_text("keep\n")

    dirty = worktree_git.cleanup_worktree(plan, timeout_s=5.0)
    assert dirty["state"] == "retained"
    assert path.exists()
    assert (path / "untracked.txt").exists()

    (path / "untracked.txt").unlink()
    _git(repo, "worktree", "lock", str(path), "--reason", "operator hold")
    locked = worktree_git.cleanup_worktree(plan, timeout_s=5.0)
    assert locked["state"] == "retained"
    assert path.exists()
    _git(repo, "worktree", "unlock", str(path))


def test_cleanup_retains_gitignored_files(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)
    worktree_git.prepare_worktree(plan, timeout_s=5.0)
    path = Path(plan["path"])
    (path / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(path, "add", "--", ".gitignore")
    _git(path, "commit", "--quiet", "-m", "ignore generated output")
    ignored = path / "ignored.log"
    ignored.write_text("must survive cleanup\n", encoding="utf-8")

    result = worktree_git.cleanup_worktree(plan, timeout_s=5.0)

    assert result["state"] == "retained"
    assert result["reason"] == "dirty-or-untracked"
    assert ignored.read_text(encoding="utf-8") == "must survive cleanup\n"


def test_cleanup_does_not_remove_differing_effective_cwd(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo, tmp_path)
    worktree_git.prepare_worktree(plan, timeout_s=5.0)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    altered = dict(plan, effectiveCwd=str(explicit))

    result = worktree_git.cleanup_worktree(altered, timeout_s=5.0)

    assert result["state"] == "retained"
    assert Path(plan["path"]).exists()
    assert explicit.exists()


def test_timeout_is_aggregate_and_git_is_invoked_without_a_shell(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(worktree_git.subprocess, "run", fake_run)
    with pytest.raises(PacError) as error:
        worktree_git.resolve_base(repo, "HEAD", timeout_s=0.1)

    assert error.value.code == "PAC_WORKTREE_PREPARE_FAILED"
    assert calls
    assert calls[0][0][0] == "git"
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] <= 0.1
