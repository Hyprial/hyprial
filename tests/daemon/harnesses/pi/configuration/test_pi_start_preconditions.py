"""Pi P2 launch preconditions are actionable and remain configuration errors."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import (
    PiStartPreconditionError,
    resolve_approved_pi_project,
)


_COMMAND = {
    "command_name": "worker one",
    "command_headless": True,
    "command_model_provider": "deepseek",
    "command_model": "deepseek-chat",
}


def test_missing_approve_keeps_code_and_gives_a_full_command(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)

    with pytest.raises(PiStartPreconditionError) as caught:
        resolve_approved_pi_project(
            cwd=str(project), runtime_args=(), **_COMMAND
        )

    assert caught.value.code == "PI_P2_LAUNCH_UNTRUSTED"
    message = str(caught.value)
    assert "Pi P2 headless launch requires explicit --approve trust" in message
    assert "--headless and --tmux" in message
    assert (
        "hyprial agent start pi --name 'worker one' --headless --cwd " in message
    )
    assert "--provider deepseek --model deepseek-chat -- --approve" in message


def test_non_git_cwd_keeps_code_and_names_qualification_and_command(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "not-git"
    cwd.mkdir()

    with pytest.raises(PiStartPreconditionError) as caught:
        resolve_approved_pi_project(
            cwd=str(cwd),
            runtime_args=("--approve",),
            **{
                **_COMMAND,
                "command_headless": False,
                "command_tmux": True,
            },
        )

    assert caught.value.code == "PI_P2_LAUNCH_UNTRUSTED"
    message = str(caught.value)
    assert "Pi P2 cwd is not inside an approved Git repository" in message
    assert "directory inside a Git working tree" in message
    assert "hyprial agent start pi --name 'worker one' --tmux" in message
    assert "--cwd /absolute/path/to/git-working-tree" in message
    assert message.endswith("-- --approve`.")
    assert not (cwd / ".git").exists()


def test_git_work_tree_does_not_move_the_approved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))

    receipt = resolve_approved_pi_project(
        cwd=str(project), runtime_args=("--approve",), **_COMMAND
    )

    assert receipt.cwd == project.resolve()
    assert receipt.repository_root == project.resolve()


def test_git_dir_cannot_approve_a_non_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    subprocess.run(["git", "init", "--quiet", str(approved)], check=True)
    cwd = tmp_path / "not-git"
    cwd.mkdir()
    monkeypatch.setenv("GIT_DIR", str(approved / ".git"))

    with pytest.raises(PiStartPreconditionError, match="cwd is not inside"):
        resolve_approved_pi_project(
            cwd=str(cwd), runtime_args=("--approve",), **_COMMAND
        )


def test_git_ceiling_does_not_hide_the_real_approved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project))

    receipt = resolve_approved_pi_project(
        cwd=str(cwd), runtime_args=("--approve",), **_COMMAND
    )

    assert receipt.cwd == cwd.resolve()
    assert receipt.repository_root == project.resolve()


def test_recovery_command_puts_approve_immediately_after_delimiter(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)

    with pytest.raises(PiStartPreconditionError) as caught:
        resolve_approved_pi_project(
            cwd=str(project), runtime_args=("--offline",), **_COMMAND
        )

    assert " -- --approve --offline`" in str(caught.value)


def test_misplaced_hyprial_option_explains_the_delimiter_and_corrects_command(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)

    with pytest.raises(PiStartPreconditionError) as caught:
        resolve_approved_pi_project(
            cwd=str(project),
            runtime_args=("--approve", "--json"),
            **_COMMAND,
        )

    assert caught.value.code == "HARNESS_START_FAILED"
    message = str(caught.value)
    assert "does not support argument '--json'" in message
    assert "Arguments after '--' go to Pi" in message
    assert "hyprial options such as --json belong before '--'" in message
    command = message.split("Run: `", 1)[1].removesuffix("`.")
    assert " --json -- --approve" in command
    assert not command.endswith("--approve --json")
