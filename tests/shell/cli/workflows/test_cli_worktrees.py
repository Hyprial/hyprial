"""Production-CLI coverage for the read-only worktree advice in ``workflow gc``."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
from typing import NamedTuple
from unittest import mock

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.commands.workflow.internal.worktrees as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


_COMMIT_S = 1577934245  # 2020-01-02T03:04:05Z
_DIRTY_MTIME_S = 1893456000  # 2030-01-01T00:00:00Z

_COMMIT_ENV = {
    "GIT_AUTHOR_DATE": "2020-01-02T03:04:05+0000",
    "GIT_COMMITTER_DATE": "2020-01-02T03:04:05+0000",
}


class WorktreeFixture(NamedTuple):
    repo: Path
    paths: dict[str, Path]
    env: dict[str, str]


def _git(
    cwd: Path, *arguments: str, env: dict[str, str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(cwd), *arguments],
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _commit(cwd: Path, message: str, *, env: dict[str, str]) -> None:
    _git(cwd, "add", "-A", env=env)
    _git(cwd, "commit", "-m", message, env={**env, **_COMMIT_ENV})


@pytest.fixture(scope="module")
def worktree_repo(tmp_path_factory: pytest.TempPathFactory) -> WorktreeFixture:
    root = tmp_path_factory.mktemp("cli-worktrees")
    empty_config = root / "empty-git-config"
    empty_config.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.fspath(empty_config),
        "GIT_CONFIG_SYSTEM": os.fspath(empty_config),
    }

    remote = root / "remote.git"
    source = root / "source"
    repo = root / "clone"
    subprocess.run(
        ["git", "init", "--bare", os.fspath(remote)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", os.fspath(source)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    _git(source, "config", "user.name", "Worktree Test", env=env)
    _git(source, "config", "user.email", "worktrees@example.test", env=env)
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit(source, "base", env=env)
    _git(source, "remote", "add", "origin", os.fspath(remote), env=env)
    _git(source, "push", "-u", "origin", "main", env=env)
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main", env=env)
    subprocess.run(
        ["git", "clone", os.fspath(remote), os.fspath(repo)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    _git(repo, "config", "user.name", "Worktree Test", env=env)
    _git(repo, "config", "user.email", "worktrees@example.test", env=env)

    paths: dict[str, Path] = {}

    merged = root / "merged"
    _git(repo, "worktree", "add", "-b", "merged-clean", os.fspath(merged), env=env)
    (merged / "merged.txt").write_text("merged\n", encoding="utf-8")
    _commit(merged, "merged branch", env=env)
    _git(
        repo,
        "merge",
        "--no-ff",
        "merged-clean",
        "-m",
        "merge clean",
        env={**env, **_COMMIT_ENV},
    )
    _git(repo, "push", "origin", "main", env=env)
    paths["merged"] = merged

    pushed = root / "pushed"
    _git(repo, "worktree", "add", "-b", "pushed-feature", os.fspath(pushed), env=env)
    (pushed / "pushed.txt").write_text("published\n", encoding="utf-8")
    _commit(pushed, "published branch", env=env)
    _git(pushed, "push", "-u", "origin", "pushed-feature", env=env)
    paths["pushed"] = pushed

    dirty = root / "dirty"
    _git(repo, "worktree", "add", "-b", "dirty-branch", os.fspath(dirty), env=env)
    (dirty / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    paths["dirty"] = dirty

    no_upstream = root / "no-upstream"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "no-upstream",
        os.fspath(no_upstream),
        env=env,
    )
    (no_upstream / "local.txt").write_text("local\n", encoding="utf-8")
    _commit(no_upstream, "local only", env=env)
    paths["no_upstream"] = no_upstream

    ahead = root / "ahead"
    _git(repo, "worktree", "add", "-b", "ahead-branch", os.fspath(ahead), env=env)
    (ahead / "ahead.txt").write_text("published\n", encoding="utf-8")
    _commit(ahead, "ahead published", env=env)
    _git(ahead, "push", "-u", "origin", "ahead-branch", env=env)
    (ahead / "ahead.txt").write_text("published\nlocal\n", encoding="utf-8")
    _commit(ahead, "ahead local", env=env)
    paths["ahead"] = ahead

    missing = root / "missing"
    _git(repo, "worktree", "add", "-b", "missing-branch", os.fspath(missing), env=env)
    shutil.rmtree(missing)
    paths["missing"] = missing

    detached = root / "detached"
    _git(repo, "worktree", "add", "--detach", os.fspath(detached), "main", env=env)
    paths["detached"] = detached

    bracket = root / "bracket[one]"
    _git(repo, "worktree", "add", "-b", "bracket-one", os.fspath(bracket), env=env)
    (bracket / "tracked.txt").write_text("bracket dirty\n", encoding="utf-8")
    paths["bracket"] = bracket

    long_path = root / ("long-path-" + "x" * 135)
    _git(repo, "worktree", "add", "-b", "long-dirty", os.fspath(long_path), env=env)
    (long_path / "tracked.txt").write_text("long dirty\n", encoding="utf-8")
    paths["long"] = long_path

    # Non-ASCII names come back C-quoted as octal UTF-8 bytes without -z.
    unicode_tree = root / "unicode"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "unicode-branch",
        os.fspath(unicode_tree),
        env=env,
    )
    (unicode_tree / "é.txt").write_text("accent\n", encoding="utf-8")
    os.utime(unicode_tree / "é.txt", (_DIRTY_MTIME_S, _DIRTY_MTIME_S))
    _git(unicode_tree, "mv", "tracked.txt", "renamed.txt", env=env)
    os.utime(unicode_tree / "renamed.txt", (_COMMIT_S, _COMMIT_S))
    paths["unicode"] = unicode_tree

    # The directory survives but its .git link file is gone, so git reports
    # "prunable gitdir file points to non-existent location".
    orphaned = root / "orphaned"
    _git(repo, "worktree", "add", "-b", "orphaned-branch", os.fspath(orphaned), env=env)
    (orphaned / ".git").unlink()
    paths["orphaned"] = orphaned

    # A branch whose remote was deleted after it was pushed.
    gone = root / "gone"
    _git(repo, "worktree", "add", "-b", "gone-branch", os.fspath(gone), env=env)
    (gone / "gone.txt").write_text("gone\n", encoding="utf-8")
    _commit(gone, "gone upstream", env=env)
    _git(gone, "push", "-u", "origin", "gone-branch", env=env)
    _git(repo, "push", "origin", "--delete", "gone-branch", env=env)
    _git(repo, "fetch", "--prune", "origin", env=env)
    paths["gone"] = gone

    # Merged into origin/dev but not into origin/HEAD (= main).
    dev_merged = root / "dev-merged"
    _git(repo, "worktree", "add", "-b", "dev-feature", os.fspath(dev_merged), env=env)
    (dev_merged / "dev.txt").write_text("dev\n", encoding="utf-8")
    _commit(dev_merged, "dev feature", env=env)
    _git(repo, "push", "origin", "dev-feature:dev", env=env)
    _git(repo, "push", "origin", "--delete", "dev-feature", env=env, check=False)
    _git(repo, "fetch", "--prune", "origin", env=env)
    paths["dev_merged"] = dev_merged

    # Pin every worktree's own index/HEAD mtime to the commit time so the
    # "touched recently" signal is deterministic; a test that wants a fresh
    # worktree touches it itself.
    for tree in (repo, *paths.values()):
        _pin_git_touch(tree, _COMMIT_S)

    return WorktreeFixture(repo=repo, paths=paths, env=env)


def _worktree_git_dir(tree: Path) -> Path | None:
    dot_git = tree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    return Path(dot_git.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip())


def _pin_git_touch(tree: Path, seconds: int) -> None:
    git_dir = _worktree_git_dir(tree)
    if git_dir is None:
        return
    for name in ("index", "HEAD"):
        if (git_dir / name).exists():
            os.utime(git_dir / name, (seconds, seconds))


@contextmanager
def _cli_environment():
    """A daemon preview and a ready home: the scan itself must not need either."""
    with mock.patch.object(_cli_get_services(), "require_initialized_hyprial_home", lambda: None), mock.patch.object(
        _cli_support,
        "_daemon_request",
        lambda *args, **kwargs: {
            "ok": True,
            "dryRun": True,
            "selected": [],
            "selectedCount": 0,
        },
    ):
        yield


def _run(*arguments: str, env: dict[str, str] | None = None):
    with _cli_environment():
        return CliRunner().invoke(
            _cli_root_app, ["workflow", "gc", "--dry-run", *arguments], env=env or {}
        )


def _invoke(fixture: WorktreeFixture, *arguments: str):
    return _run(os.fspath(fixture.repo), *arguments, env=fixture.env)


def _json_rows(
    fixture: WorktreeFixture,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    result = _invoke(fixture, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    repo = payload["worktrees"][0]
    rows = {row["path"]: row for row in repo["worktrees"]}
    return repo, rows


def test_json_reports_every_state_and_shape(worktree_repo: WorktreeFixture) -> None:
    repo, rows = _json_rows(worktree_repo)

    assert repo["repo"] == os.fspath(worktree_repo.repo)
    assert repo["base"] == "refs/remotes/origin/HEAD"
    assert repo["summary"]["total"] == len(rows)
    assert repo["bases"] == ["refs/remotes/origin/HEAD", "origin/dev", "origin/main"]
    assert repo["summary"]["leftover"] == 9
    assert repo["summary"]["error"] == 0
    assert rows[os.fspath(worktree_repo.paths["dirty"])]["state"] == "dirty"
    assert rows[os.fspath(worktree_repo.paths["no_upstream"])]["state"] == "unpushed"
    assert rows[os.fspath(worktree_repo.paths["ahead"])]["state"] == "unpushed"
    assert rows[os.fspath(worktree_repo.paths["merged"])]["state"] == "merged"
    assert rows[os.fspath(worktree_repo.paths["pushed"])]["state"] == "pushed"
    assert rows[os.fspath(worktree_repo.paths["missing"])]["state"] == "missing"
    assert rows[os.fspath(worktree_repo.paths["detached"])]["branch"] is None
    assert rows[os.fspath(worktree_repo.paths["bracket"])]["branch"] == "bracket-one"
    assert rows[os.fspath(worktree_repo.paths["ahead"])]["aheadOfUpstream"] == 1


def test_unpushed_without_upstream_is_leftover(worktree_repo: WorktreeFixture) -> None:
    _, rows = _json_rows(worktree_repo)
    row = rows[os.fspath(worktree_repo.paths["no_upstream"])]

    assert row["upstream"] is None
    assert row["aheadOfUpstream"] is None
    assert row["unpushedCommits"] == 1
    assert row["leftover"] is True


def test_human_default_all_hints_and_width(worktree_repo: WorktreeFixture) -> None:
    default = _invoke(worktree_repo)
    assert default.exit_code == 0, default.output
    table = default.stdout.split("Summary:", 1)[0]
    assert "dirty-branch" in table
    assert "no-upstream" in table
    assert os.fspath(worktree_repo.paths["pushed"]) not in table
    assert "git worktree prune" in default.stdout
    collapsed = default.stdout.replace("\\\n", "")
    assert f"git worktree remove {worktree_repo.paths['merged']}" in collapsed
    assert f"git worktree remove {worktree_repo.paths['pushed']}" not in default.stdout
    assert "commit changes before removal" in default.stdout
    assert "push commits before removal" in default.stdout
    overlong = [line for line in default.stdout.splitlines() if len(line) > 120]
    assert not overlong

    show_all = _invoke(worktree_repo, "--all")
    assert show_all.exit_code == 0, show_all.output
    assert "pushed-feature" in show_all.stdout


def test_plain_renderer_preserves_brackets_in_branch_name() -> None:
    rendered = _cli_owner_1._render_worktree_repository(
        {
            "repo": "/tmp/repo",
            "base": "main",
            "worktrees": [
                {
                    "path": "/tmp/repo/topic",
                    "branch": "topic[one]",
                    "dirtyFiles": 1,
                    "unpushedCommits": 0,
                    "lastActivityAtMs": 1,
                    "mergedIntoBase": False,
                    "state": "dirty",
                    "leftover": True,
                }
            ],
            "summary": {
                "missing": 0,
                "dirty": 1,
                "unpushed": 0,
                "merged": 0,
                "pushed": 0,
                "error": 0,
                "leftover": 1,
                "total": 1,
            },
        },
        show_all=False,
        now_ms=1,
    )

    assert "topic[one]" in rendered


def test_path_inside_linked_worktree_resolves_common_repo(
    worktree_repo: WorktreeFixture,
) -> None:
    nested = worktree_repo.paths["dirty"] / "nested"
    nested.mkdir()

    result = _run(os.fspath(nested), "--json", env=worktree_repo.env)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["worktrees"][0]["repo"] == os.fspath(
        worktree_repo.repo
    )


def test_repo_defaults_to_repository_containing_cwd(
    worktree_repo: WorktreeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(worktree_repo.repo)

    result = _run("--json", env=worktree_repo.env)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["worktrees"][0]["repo"] == os.fspath(
        worktree_repo.repo
    )


def test_non_repo_path_has_clear_error(
    worktree_repo: WorktreeFixture, tmp_path: Path
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run(os.fspath(outside), env=worktree_repo.env)

    assert result.exit_code == 1
    assert os.fspath(outside) in result.stderr.replace("\n", "")
    assert "pass a repository path" in result.stderr


def test_default_cwd_outside_a_repo_keeps_the_gc_preview(
    worktree_repo: WorktreeFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "no-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = _run("--json", env=worktree_repo.env)

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dryRun"] is True
    assert body["worktrees"] == []


def test_one_worktree_git_error_does_not_abort_scan(
    worktree_repo: WorktreeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _cli_get_services()._run_worktree_git
    failed_path = worktree_repo.paths["dirty"]

    def injected(
        arguments: list[str] | tuple[str, ...], *, cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        if cwd == failed_path and arguments[:1] == ["status"]:
            return subprocess.CompletedProcess(
                ["git", *arguments], 17, stdout="", stderr="injected worktree error"
            )
        return original(arguments, cwd=cwd)

    monkeypatch.setattr(_cli_get_services(), "_run_worktree_git", injected)
    repo, rows = _json_rows(worktree_repo)

    failed = rows[os.fspath(failed_path)]
    assert failed["state"] == "error"
    assert "injected worktree error" in str(failed["error"])
    assert repo["summary"]["total"] == len(rows)
    assert rows[os.fspath(worktree_repo.paths["ahead"])]["state"] == "unpushed"


def test_non_ascii_dirty_path_counts_its_own_mtime(
    worktree_repo: WorktreeFixture,
) -> None:
    _, rows = _json_rows(worktree_repo)
    row = rows[os.fspath(worktree_repo.paths["unicode"])]

    # é.txt plus one rename entry; the rename's original path is not a file.
    assert row["dirtyFiles"] == 2
    assert row["lastCommitAtMs"] == _COMMIT_S * 1000
    assert row["lastActivityAtMs"] == _DIRTY_MTIME_S * 1000


def test_prunable_worktree_with_surviving_directory_is_missing(
    worktree_repo: WorktreeFixture,
) -> None:
    _, rows = _json_rows(worktree_repo)
    row = rows[os.fspath(worktree_repo.paths["orphaned"])]

    assert row["state"] == "missing"
    assert row["missing"] is True
    assert row["leftover"] is True
    assert "error" not in row


def test_deleted_upstream_is_not_an_error(worktree_repo: WorktreeFixture) -> None:
    _, rows = _json_rows(worktree_repo)
    row = rows[os.fspath(worktree_repo.paths["gone"])]

    assert row["state"] == "unpushed"
    assert row["upstreamGone"] is True
    assert row["aheadOfUpstream"] is None
    assert row["unpushedCommits"] == 1


def test_merged_into_dev_counts_although_origin_head_is_main(
    worktree_repo: WorktreeFixture,
) -> None:
    _, rows = _json_rows(worktree_repo)
    row = rows[os.fspath(worktree_repo.paths["dev_merged"])]

    assert row["mergedIntoBase"] is True
    assert row["state"] == "merged"
    only_main = _invoke(worktree_repo, "--json", "--base", "origin/main")
    assert only_main.exit_code == 0, only_main.output
    main_rows = {
        item["path"]: item
        for item in json.loads(only_main.stdout)["worktrees"][0]["worktrees"]
    }
    assert (
        main_rows[os.fspath(worktree_repo.paths["dev_merged"])]["mergedIntoBase"]
        is False
    )


def test_recently_touched_merged_worktree_is_not_offered_for_removal(
    worktree_repo: WorktreeFixture,
) -> None:
    """hq review of #962: a worker's fresh worktree from origin/dev is merged,
    clean and has nothing unpushed -- exactly like a finished one."""
    merged = worktree_repo.paths["merged"]
    now_s = int(time.time())
    _pin_git_touch(merged, now_s)
    try:
        _, rows = _json_rows(worktree_repo)
        assert rows[os.fspath(merged)]["lastActivityAtMs"] >= now_s * 1000
        result = _invoke(worktree_repo)
        assert result.exit_code == 0, result.output
        collapsed = result.stdout.replace("\\\n", "")
        assert f"git worktree remove {merged}" not in collapsed
        assert "Confirm nobody is using a worktree before removing it" in result.stdout
        assert "active in the last 24h are left out" in result.stdout
    finally:
        _pin_git_touch(merged, _COMMIT_S)


def test_removal_suggestions_start_with_the_in_use_caution(
    worktree_repo: WorktreeFixture,
) -> None:
    result = _invoke(worktree_repo)
    steps = result.stdout.split("Next steps:", 1)[1].splitlines()
    caution = next(i for i, line in enumerate(steps) if "Confirm nobody" in line)
    remove = next(i for i, line in enumerate(steps) if "git worktree remove" in line)
    assert caution < remove


def test_control_characters_in_branch_and_path_are_escaped() -> None:
    rendered = _cli_owner_1._render_worktree_repository(
        {
            "repo": "/tmp/repo",
            "base": "main",
            "worktrees": [
                {
                    "path": "/tmp/repo/a\x1b[2Jb",
                    "branch": "topic\rspoof",
                    "dirtyFiles": 1,
                    "unpushedCommits": 0,
                    "lastActivityAtMs": 1,
                    "mergedIntoBase": False,
                    "state": "dirty",
                    "leftover": True,
                }
            ],
            "summary": {
                "missing": 0,
                "dirty": 1,
                "unpushed": 0,
                "merged": 0,
                "pushed": 0,
                "error": 0,
                "leftover": 1,
                "total": 1,
            },
        },
        show_all=False,
        now_ms=1,
    )

    assert "\x1b" not in rendered and "\r" not in rendered
    assert "a\\x1b[2Jb" in rendered
    assert "topic\\x0dspoof" in rendered
