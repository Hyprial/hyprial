"""Pi P2 cwd trust: an agent's own 0700 workspace is a trust root.

2026-10-16 production: resident pi agents default their cwd to their own
agent-home workspace (``~/.hyprial/.../agents/<name>/workspace``, 0700, not a
Git repository).  ``resolve_approved_pi_project`` only accepted a Git
worktree, so ``hyprial start`` refused every such agent with "Pi P2 cwd is not
inside an approved Git repository", and the operator's workaround was to pass
``--cwd`` pointing at an unrelated Git checkout.

These tests pin the bounded rule: the trust root may be the agent's OWN
workspace (private to its owner, not a Git worktree), and nothing else.  An
arbitrary directory, another agent's workspace, or a group/other-accessible
workspace are all refused.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import (
    PiProjectTrustError,
    PiProjectTrustReceipt,
    resolve_approved_pi_project,
)


def _workspace(root: Path, name: str) -> Path:
    workspace = root / "agents" / name / "workspace"
    workspace.mkdir(parents=True)
    os.chmod(workspace, 0o700)
    return workspace


def test_own_workspace_is_signed_as_a_workspace_trust_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")

    receipt = resolve_approved_pi_project(
        cwd=str(workspace), runtime_args=("--approve",), own_workspace=workspace
    )

    assert receipt.trusted is True
    assert receipt.scope == "workspace"
    assert receipt.repository_root == workspace.resolve()
    assert receipt.cwd == workspace.resolve()


def test_a_git_worktree_still_wins_over_the_workspace_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    nested = repository / "src"
    nested.mkdir()

    receipt = resolve_approved_pi_project(
        cwd=str(nested), runtime_args=("--approve",), own_workspace=tmp_path / "workspace"
    )

    assert receipt.scope == "repository"
    assert receipt.repository_root == repository.resolve()


def test_an_unrelated_non_git_directory_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()

    with pytest.raises(PiProjectTrustError, match="approved Git repository"):
        resolve_approved_pi_project(
            cwd=str(unrelated), runtime_args=("--approve",), own_workspace=workspace
        )


def test_another_agents_workspace_is_refused(tmp_path: Path) -> None:
    own = _workspace(tmp_path, "be-dev-1")
    intruder = _workspace(tmp_path, "be-dev-2")

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(intruder), runtime_args=("--approve",), own_workspace=own
        )


def test_a_group_readable_workspace_is_not_a_trust_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    os.chmod(workspace, 0o755)

    with pytest.raises(PiProjectTrustError, match="private to its owner"):
        resolve_approved_pi_project(
            cwd=str(workspace), runtime_args=("--approve",), own_workspace=workspace
        )


def test_workspace_trust_still_requires_explicit_approval(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")

    with pytest.raises(PiProjectTrustError, match="requires explicit --approve"):
        resolve_approved_pi_project(
            cwd=str(workspace), runtime_args=(), own_workspace=workspace
        )


def test_receipt_rejects_a_workspace_root_that_is_not_the_cwd(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    nested = workspace / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="must equal cwd"):
        PiProjectTrustReceipt(nested, workspace, True, "workspace")


def test_a_workspace_symlink_to_another_workspace_is_refused(tmp_path: Path) -> None:
    """A link from this agent's workspace to another agent's must not trust it."""

    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    intruder = _workspace(tmp_path, "be-dev-2")
    own.symlink_to(intruder, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(intruder), runtime_args=("--approve",), own_workspace=own
        )


def test_a_workspace_symlink_to_an_arbitrary_directory_is_refused(
    tmp_path: Path,
) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    arbitrary = tmp_path / "arbitrary"
    arbitrary.mkdir()
    os.chmod(arbitrary, 0o700)
    own.symlink_to(arbitrary, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(arbitrary), runtime_args=("--approve",), own_workspace=own
        )


def test_a_multi_level_workspace_symlink_is_refused(tmp_path: Path) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    target = _workspace(tmp_path, "be-dev-2")
    hop = tmp_path / "hop"
    hop.symlink_to(target, target_is_directory=True)
    own.symlink_to(hop, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(target), runtime_args=("--approve",), own_workspace=own
        )


def test_a_workspace_symlink_is_refused_even_when_cwd_is_the_link(
    tmp_path: Path,
) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    target = _workspace(tmp_path, "be-dev-2")
    own.symlink_to(target, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(own), runtime_args=("--approve",), own_workspace=own
        )
