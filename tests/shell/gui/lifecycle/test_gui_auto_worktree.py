"""Integration coverage for the automatic sparse GUI development worktree."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from hyprial.shell.impl.gui import worktree as auto_worktree
from hyprial.shell.impl.gui.errors import GuiError

AUTO_PATH = auto_worktree.AUTO_WORKTREE_RELATIVE
AUTO_BRANCH = auto_worktree.AUTO_BRANCH
POSIX = pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink semantics"
)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "R5 Auto Worktree Tests",
            "GIT_AUTHOR_EMAIL": "r5-auto@example.invalid",
            "GIT_COMMITTER_NAME": "R5 Auto Worktree Tests",
            "GIT_COMMITTER_EMAIL": "r5-auto@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env or _git_env(),
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr}"
        )
    return completed.stdout.strip()


def _write_project(repo: Path) -> None:
    gui = repo / "gui"
    (gui / "src").mkdir(parents=True)
    (gui / "scripts").mkdir()
    (gui / "package.json").write_text(
        json.dumps(
            {
                "name": "@hyprial/gui-app",
                "version": "0.0.0",
                "scripts": {
                    "dev": "node scripts/serve.mjs",
                    "start": "node scripts/serve.mjs",
                    "stub": "node scripts/stub-backend.mjs",
                },
                "engines": {"node": ">=24"},
            }
        ),
        encoding="utf-8",
    )
    (gui / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "@hyprial/gui-app",
                "version": "0.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {"name": "@hyprial/gui-app", "version": "0.0.0"}
                },
            }
        ),
        encoding="utf-8",
    )
    (gui / "src" / "app.js").write_text("export const app = true;\n", encoding="utf-8")
    (gui / "scripts" / "serve.mjs").write_text("// serve\n", encoding="utf-8")
    (gui / "scripts" / "stub-backend.mjs").write_text("// stub\n", encoding="utf-8")
    (repo / "src" / "hyprial").mkdir(parents=True)
    (repo / "src" / "hyprial" / "not_gui.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_not_gui.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "not-materialized.md").write_text("docs\n", encoding="utf-8")
    (repo / "README.md").write_text("source checkout\n", encoding="utf-8")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    env = _git_env()
    _git(repo, "init", "-q", "-b", "main", env=env)
    _git(repo, "config", "user.name", "R5 Auto Worktree Tests", env=env)
    _git(repo, "config", "user.email", "r5-auto@example.invalid", env=env)
    _git(repo, "config", "commit.gpgsign", "false", env=env)
    _write_project(repo)
    _git(repo, "add", "--", ".", env=env)
    _git(repo, "commit", "-q", "-m", "base", env=env)
    return repo


class FakeNpmRunner:
    """Run real Git commands and record the one supported npm install command."""

    def __init__(self, *, install_returncode: int = 0) -> None:
        git = shutil.which("git")
        assert git, "tests require git"
        self.git = git
        self.install_returncode = install_returncode
        self.install_calls: list[tuple[tuple[str, ...], dict]] = []
        self.git_calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **kwargs):
        if argv[0] == "/fake/npm":
            self.install_calls.append((tuple(argv), dict(kwargs)))
            cwd = Path(kwargs["cwd"])
            if self.install_returncode == 0:
                (cwd / "node_modules").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(argv, 0, "added 1 package\n", "")
            return subprocess.CompletedProcess(
                argv, self.install_returncode, "", "npm ERR! synthetic failure\n"
            )
        assert argv[0] == self.git, argv
        self.git_calls.append(tuple(argv))
        return subprocess.run(argv, **kwargs)

    def which(self, name: str) -> str | None:
        if name == "git":
            return self.git
        if name == "npm":
            return "/fake/npm"
        return None


def _ensure(repo: Path, runner: FakeNpmRunner, env: dict[str, str] | None = None):
    return auto_worktree.ensure_dev_worktree(
        cwd=repo,
        environ=env or _git_env(),
        which=runner.which,
        run=runner,
    )


def _install_arguments(runner: FakeNpmRunner) -> list[tuple[tuple[str, ...], dict]]:
    return runner.install_calls


def test_first_create_materializes_only_gui_and_installs_once(
    source_repo: Path,
) -> None:
    env = _git_env()
    source_head = _git(source_repo, "rev-parse", "HEAD", env=env)
    source_branch = _git(source_repo, "branch", "--show-current", env=env)
    dirty = source_repo / "README.md"
    dirty.write_text("source checkout\nlocal dirty change\n", encoding="utf-8")
    untracked = source_repo / "local-only.txt"
    untracked.write_text("local only\n", encoding="utf-8")
    status_before = set(
        _git(source_repo, "status", "--porcelain", env=env).splitlines()
    )
    lock_before = (source_repo / "gui" / "package-lock.json").read_bytes()

    runner = FakeNpmRunner()
    result = _ensure(source_repo, runner, env)

    expected_path = (source_repo / AUTO_PATH).resolve()
    assert result.created is True
    assert result.source == source_repo.resolve()
    assert result.path == expected_path
    assert result.branch == AUTO_BRANCH
    assert result.base == source_head
    assert result.home == expected_path / "gui"
    assert result.install.ran is True
    assert result.install.returncode == 0
    assert result.install.cwd == expected_path / "gui"
    assert result.install.command == ("/fake/npm", "ci", "--ignore-scripts")

    installs = _install_arguments(runner)
    assert len(installs) == 1
    argv, kwargs = installs[0]
    assert argv == ("/fake/npm", "ci", "--ignore-scripts")
    assert kwargs["cwd"] == str(expected_path / "gui")
    assert "checkout" not in " ".join(argv)

    commands = [call[3:] for call in runner.git_calls]
    expected_creation = (
        (
            "worktree",
            "add",
            "-b",
            AUTO_BRANCH,
            "--no-checkout",
            str(expected_path),
            source_head,
        ),
        ("sparse-checkout", "set", "--cone", "gui"),
        ("checkout",),
    )
    positions = [commands.index(command) for command in expected_creation]
    assert positions == sorted(positions)

    assert _git(expected_path, "rev-parse", "HEAD", env=env) == source_head
    assert _git(expected_path, "branch", "--show-current", env=env) == AUTO_BRANCH
    assert _git(expected_path, "sparse-checkout", "list", env=env).splitlines() == [
        "gui"
    ]
    assert (expected_path / "gui" / "package.json").is_file()
    assert (expected_path / "gui" / "src" / "app.js").is_file()
    assert not (expected_path / "src").exists()
    assert not (expected_path / "tests").exists()
    assert not (expected_path / "docs").exists()

    assert _git(source_repo, "rev-parse", "HEAD", env=env) == source_head
    assert _git(source_repo, "branch", "--show-current", env=env) == source_branch
    assert dirty.read_text(encoding="utf-8").endswith("local dirty change\n")
    assert untracked.read_text(encoding="utf-8") == "local only\n"
    status_after = set(
        _git(source_repo, "status", "--porcelain", env=env).splitlines()
    )
    assert status_before <= status_after
    assert status_after - status_before == {"?? .worktrees/"}
    assert (source_repo / "gui" / "package-lock.json").read_bytes() == lock_before


def test_restore_preserves_dirty_and_untracked_and_does_not_reinstall(
    source_repo: Path,
) -> None:
    env = _git_env()
    first_runner = FakeNpmRunner()
    first = _ensure(source_repo, first_runner, env)
    first_head = _git(first.path, "rev-parse", "HEAD", env=env)

    dirty = first.home / "src" / "app.js"
    dirty.write_text("export const app = 'local';\n", encoding="utf-8")
    untracked = first.home / "local-note.txt"
    untracked.write_text("keep me\n", encoding="utf-8")
    lock_before = (first.home / "package-lock.json").read_bytes()

    second_runner = FakeNpmRunner()
    second = _ensure(source_repo, second_runner, env)

    assert second.created is False
    assert second.path == first.path
    assert second.install.ran is False
    assert second.install.reason == "node_modules already exists"
    assert _install_arguments(second_runner) == []
    assert dirty.read_text(encoding="utf-8") == "export const app = 'local';\n"
    assert untracked.read_text(encoding="utf-8") == "keep me\n"
    assert _git(first.path, "rev-parse", "HEAD", env=env) == first_head
    assert (first.home / "package-lock.json").read_bytes() == lock_before

    restore_commands = [call[3:] for call in second_runner.git_calls]
    forbidden = {
        "reset",
        "clean",
        "stash",
        "rebase",
        "fetch",
        "merge",
        "checkout",
    }
    assert all(command[0] not in forbidden for command in restore_commands)
    assert ("worktree", "add") not in [command[:2] for command in restore_commands]

    shutil.rmtree(first.home / "node_modules")
    third_runner = FakeNpmRunner()
    third = _ensure(source_repo, third_runner, env)
    assert third.created is False
    assert third.install.ran is True
    assert len(_install_arguments(third_runner)) == 1
    assert (first.home / "node_modules").is_dir()


def test_invoking_inside_the_auto_worktree_reuses_it_without_nesting(
    source_repo: Path,
) -> None:
    env = _git_env()
    first = _ensure(source_repo, FakeNpmRunner(), env)

    runner = FakeNpmRunner()
    second = _ensure(first.path, runner, env)

    assert second.created is False
    assert second.path == first.path
    assert second.home == first.home
    assert second.install.ran is False
    assert not (first.path / AUTO_PATH).exists()
    assert _install_arguments(runner) == []


def test_existing_non_worktree_path_fails_without_damage(
    source_repo: Path,
) -> None:
    path = source_repo / AUTO_PATH
    path.mkdir(parents=True)
    marker = path / "keep.txt"
    marker.write_text("not a worktree\n", encoding="utf-8")
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, runner)

    assert caught.value.code == "GUI_AUTO_WORKTREE_CONFLICT"
    assert "not a Git worktree" in str(caught.value)
    assert marker.read_text(encoding="utf-8") == "not a worktree\n"
    assert _install_arguments(runner) == []
    assert _git(source_repo, "branch", "--list", AUTO_BRANCH).strip() == ""


@POSIX
def test_symlinked_worktree_parent_is_rejected_without_damage(
    source_repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("outside\n", encoding="utf-8")
    (source_repo / ".worktrees").symlink_to(outside, target_is_directory=True)
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, runner)

    assert caught.value.code == "GUI_AUTO_WORKTREE_CONFLICT"
    assert "symlink" in str(caught.value)
    assert marker.read_text(encoding="utf-8") == "outside\n"
    assert (source_repo / ".worktrees").is_symlink()
    assert not (outside / "gui-dev-auto").exists()
    assert _install_arguments(runner) == []
    assert _git(source_repo, "branch", "--list", AUTO_BRANCH).strip() == ""


@POSIX
def test_symlinked_gui_directory_is_rejected_without_damage(
    source_repo: Path, tmp_path: Path
) -> None:
    env = _git_env()
    first = _ensure(source_repo, FakeNpmRunner(), env)
    shutil.rmtree(first.home)
    outside = tmp_path / "outside-gui"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("outside\n", encoding="utf-8")
    first.home.symlink_to(outside, target_is_directory=True)
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, runner, env)

    assert caught.value.code == "GUI_AUTO_WORKTREE_CONFLICT"
    assert marker.read_text(encoding="utf-8") == "outside\n"
    assert first.home.is_symlink()
    assert _install_arguments(runner) == []
    assert not (outside / "node_modules").exists()


def test_branch_checked_out_elsewhere_fails_without_touching_it(
    source_repo: Path, tmp_path: Path
) -> None:
    env = _git_env()
    other = tmp_path / "other-branch-worktree"
    _git(source_repo, "worktree", "add", "-q", "-b", AUTO_BRANCH, str(other), "HEAD", env=env)
    marker = other / "README.md"
    before = marker.read_text(encoding="utf-8")
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, runner)

    assert caught.value.code == "GUI_AUTO_WORKTREE_CONFLICT"
    assert "already exists" in str(caught.value)
    assert marker.read_text(encoding="utf-8") == before
    assert not (source_repo / AUTO_PATH).exists()
    assert _install_arguments(runner) == []


def test_worktree_from_another_repository_is_not_reused(
    source_repo: Path, tmp_path: Path
) -> None:
    env = _git_env()
    other_repo = tmp_path / "other-source"
    other_repo.mkdir()
    _git(other_repo, "init", "-q", "-b", "main", env=env)
    _git(other_repo, "config", "user.name", "Other", env=env)
    _git(other_repo, "config", "user.email", "other@example.invalid", env=env)
    (other_repo / "README.md").write_text("other repository\n", encoding="utf-8")
    _git(other_repo, "add", "--", ".", env=env)
    _git(other_repo, "commit", "-q", "-m", "other", env=env)

    path = source_repo / AUTO_PATH
    path.parent.mkdir(parents=True)
    _git(other_repo, "worktree", "add", "-q", str(path), "HEAD", env=env)
    marker = path / "README.md"
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, runner)

    assert caught.value.code == "GUI_AUTO_WORKTREE_CONFLICT"
    assert marker.read_text(encoding="utf-8") == "other repository\n"
    assert _install_arguments(runner) == []


def test_install_failure_preserves_worktree_and_can_be_retried(
    source_repo: Path,
) -> None:
    env = _git_env()
    failing = FakeNpmRunner(install_returncode=17)

    with pytest.raises(GuiError) as caught:
        _ensure(source_repo, failing, env)

    assert caught.value.code == "GUI_AUTO_WORKTREE_INSTALL_FAILED"
    assert caught.value.data["returncode"] == 17
    assert caught.value.data["gui_home"] == str((source_repo / AUTO_PATH) / "gui")
    path = source_repo / AUTO_PATH
    assert (path / "gui" / "package.json").is_file()
    assert _git(path, "branch", "--show-current", env=env) == AUTO_BRANCH
    assert _git(path, "sparse-checkout", "list", env=env).splitlines() == ["gui"]

    recovery = FakeNpmRunner()
    result = _ensure(source_repo, recovery, env)
    assert result.created is False
    assert result.install.ran is True
    assert len(_install_arguments(recovery)) == 1


def test_no_source_repository_is_a_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    fake_module = empty / "module.py"
    fake_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(auto_worktree, "__file__", str(fake_module))
    runner = FakeNpmRunner()

    with pytest.raises(GuiError) as caught:
        auto_worktree.ensure_dev_worktree(
            cwd=empty,
            environ=_git_env(),
            which=runner.which,
            run=runner,
        )

    assert caught.value.code == "GUI_AUTO_WORKTREE_SOURCE"
    assert "source Git repository" in str(caught.value)
    assert _install_arguments(runner) == []
