"""合入流程步骤 pr_merge_guard：判据与三条负控。

背景（2026-10-30 现场）
    #2158 的 merge commit parents = [dev, 51e7de589]，而最后一个提交 f1e995e98
    在合并之后 1 分 51 秒才产生 ⇒ PR 显示 merged=true / head=f1e995e98，但该 sha 不在 dev。
    平台字段回答"PR 关了"，不回答"我要的提交在主干里"。

本文件的用例都以**注入的 callable** 驱动，不联网、不依赖任何 ops-only 路径或凭据。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts/ci"))

import pr_merge_guard as guard  # noqa: E402


def _run(*, pr_head: str, branch_head: str, mirror: str = "", merged: bool = False,
         base_has: set[str] | None = None, expect: str | None = None):
    return guard.evaluate(
        pr=2160,
        expect_in_base=expect,
        fetch_base=lambda: "b" * 40,
        read_pr=lambda: {"head": {"sha": pr_head, "ref": "feat/x"}, "merged": merged},
        read_ref=lambda ref: mirror if ref.startswith("refs/pull/") else branch_head,
        is_ancestor=lambda commit, tip: commit in (base_has or set()),
    )


def test_pre_merge_requires_pr_head_equals_branch_head() -> None:
    rows = _run(pr_head="a" * 40, branch_head="a" * 40)
    assert all(row.ok for row in rows)
    assert any(row.name == "pr_head==branch_head" and row.ok for row in rows)


def test_pre_merge_detects_a_stale_read() -> None:
    """平台读到的 head 与分支真实头不一致（镜像/旧页面）⇒ 必须红。"""
    rows = _run(pr_head="a" * 40, branch_head="c" * 40)
    failed = [row for row in rows if not row.ok]
    assert [row.name for row in failed] == ["pr_head==branch_head"]


def test_post_merge_detects_the_2158_race() -> None:
    """复现 #2158 竞态：合入的提交在主干，**后来那个提交不在** ⇒ 必须红。

    这正是"生产代码进了、判据没进"的机器版判词。
    """
    rows = _run(pr_head="f" * 40, branch_head="", merged=True,
                base_has={"5" * 40}, expect="f" * 40)
    failed = [row for row in rows if not row.ok]
    assert [row.name for row in failed] == [f"in_base({'f' * 9})"], rows
    assert "NOT an ancestor" in failed[0].detail


def test_post_merge_success_when_the_commit_is_in_base() -> None:
    rows = _run(pr_head="f" * 40, branch_head="", merged=True,
                base_has={"f" * 40}, expect="f" * 40)
    assert all(row.ok for row in rows), rows


def test_deleted_source_branch_is_not_a_failure() -> None:
    """合入时勾了删分支 ⇒ refs/heads/<ref> 不存在，这不是失败。"""
    rows = _run(pr_head="f" * 40, branch_head="", merged=True, base_has={"f" * 40})
    assert all(row.ok for row in rows), rows
    assert any(row.name == "pr_merged" and "deleted" in row.detail for row in rows)


def test_mirror_ref_is_recorded_but_never_judged() -> None:
    """refs/pull/N/head 冻结在合并那一刻：只记录，不参与判定。"""
    rows = _run(pr_head="f" * 40, branch_head="", mirror="5" * 40, merged=True)
    mirror = next(row for row in rows if row.name == "mirror_ref_is_historical")
    assert mirror.ok and "5" * 9 in mirror.detail


# --- 无法判定必须是 3，不是 0 也不是 1 --------------------------------------
def test_unreadable_pr_is_undecidable(capsys) -> None:
    rows = guard.evaluate(
        pr=1, expect_in_base=None,
        fetch_base=lambda: "b" * 40,
        read_pr=lambda: None,
        read_ref=lambda ref: "",
        is_ancestor=lambda commit, tip: False,
    )
    assert rows[0].name == "pr_readable" and not rows[0].ok


def test_missing_token_or_api_is_undecidable(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert guard.main(["--pr", "2160", "--repo", str(ROOT)]) == guard.RC_UNDECIDABLE


def test_missing_pr_is_undecidable() -> None:
    assert guard.main([]) == guard.RC_UNDECIDABLE


# --- 真 git 的祖先判据（能变红）--------------------------------------------
def test_ancestor_check_can_turn_red(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "dev", str(tmp_path)], check=True)
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "f").write_text("1")
    git("add", ".")
    git("commit", "-qm", "a")
    base = git("rev-parse", "HEAD")
    (tmp_path / "f").write_text("2")
    git("commit", "-qam", "b")
    side = git("rev-parse", "HEAD")
    git("checkout", "-q", base)
    git("branch", "-f", "dev", "HEAD")

    def is_ancestor(commit: str, tip: str) -> bool:
        return subprocess.run(["git", "-C", str(tmp_path), "merge-base", "--is-ancestor",
                               commit, tip], capture_output=True).returncode == 0

    tip = git("rev-parse", "dev")
    assert is_ancestor(base, tip) is True
    assert is_ancestor(side, tip) is False


# --- --remote: 必填或自动探测，歧义即 rc=3（e2e 2026-10-30）------------------
def _repo_with_remotes(tmp_path: Path, *names: str) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in names:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", name,
                        f"https://example.invalid/{name}.git"], check=True)
    return tmp_path


@pytest.mark.parametrize("remotes,expect_rc", [
    (("origin",), "adopted"),          # 唯一远端 ⇒ 自动采用（不应报歧义）
    ((), "ambiguous"),                 # 没有远端 ⇒ 也不许偷偷用 origin
    (("origin", "internal"), "ambiguous"),
])
def test_remote_is_detected_or_the_run_is_undecidable(tmp_path, monkeypatch, capsys,
                                                      remotes, expect_rc) -> None:
    repo = _repo_with_remotes(tmp_path / "wt", *remotes)
    monkeypatch.setenv("GITHUB_API_URL", "https://example.invalid/api")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    rc = guard.main(["--pr", "1", "--repo", str(repo), "--base", "dev"])
    assert rc == guard.RC_UNDECIDABLE, rc
    captured = capsys.readouterr()
    if expect_rc == "ambiguous":
        assert "ambiguous_remote" in captured.err, captured.err
    else:
        # 唯一远端被采用 ⇒ 失败发生在更后面的步骤（fetch），而不是 remote 选择
        assert "ambiguous_remote" not in captured.err, captured.err
