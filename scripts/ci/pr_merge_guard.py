#!/usr/bin/env python3
"""合入流程步骤：确认「要合的提交」真的就是「分支头」，且合入后真的进了主干。

为什么需要（2026-10-30 现场）
    #2158 的 merge commit `5b8ba962b` 的 parents 是 [dev, **51e7de589**]，
    而最后一个提交 `f1e995e98` 在合并**之后**才产生（17:18:13 vs 17:16:22）。
    结果：PR 页显示 merged=true、`head.sha=f1e995e98`，但该 sha 不在 dev ——
    生产代码进了主干、判据没进。平台字段 `merged` 回答的是"这个 PR 关了"，
    不回答"我关心的提交在主干里"；只有 `merge-base --is-ancestor` 能回答后者。

三个时钟
    ① PR 的 `head.sha`          —— 平台记录的"这个 PR 现在指哪"
    ② `refs/heads/<head.ref>`   —— 分支**现在**的真实头
    ③ `refs/pull/<n>/head`      —— PR 镜像；PR 关闭后**冻结在合并那一刻**，不是"现在的头"
    ① == ② 才算"我要的提交就是分支头"；③ 只说明"合并时看到的是哪个头"。

用法
    # 合前（在本机/任一 checkout 上）
    pr_merge_guard.py --pr 2160 --repo . --remote internal --base dev
    # 合后
    pr_merge_guard.py --pr 2160 --repo . --remote internal --base dev \\
        --expect-in-base a6b510d2c

退出码
    0  所有断言成立
    1  有断言不成立（合前 head 不一致 / 合后不在主干）
    3  **无法判定**：读不到 PR 或 fetch 失败 —— 既不是通过，也不是失败

定位
    这是**合入流程的一步**（人执行），不是必需的 CI 检查：
    它需要仓库读权限与一次 fetch，且只在"合并前后"有意义。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RC_OK = 0
RC_FAIL = 1
RC_UNDECIDABLE = 3


@dataclass(frozen=True)
class Reading:
    """一条断言的结果。name 就是判据名，detail 只放可核对的数字/sha。"""

    name: str
    ok: bool
    detail: str


def _default_pr_reader(*, number: int, api: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/repos/{os.environ.get('GITHUB_REPOSITORY', '')}/pulls/{number}",
        headers={
            "Authorization": f"token {token}",
            # 前置反代会按 UA 拦默认的 `Python-urllib/x.y`（实测 403），必须有常规 UA。
            "User-Agent": "pr-merge-guard/1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def evaluate(
    *,
    pr: int,
    expect_in_base: str | None,
    fetch_base: Callable[[], str | None],
    read_pr: Callable[[], dict | None],
    read_ref: Callable[[str], str],
    is_ancestor: Callable[[str, str], bool],
) -> list[Reading]:
    """纯逻辑：所有外部依赖都以可替换的 callable 传入，便于行为测试。"""

    base_sha = fetch_base()
    if not base_sha:
        return [Reading("fetch_base", False, "git fetch of the base branch failed")]

    data = read_pr()
    if not data:
        return [Reading("pr_readable", False, "cannot read the pull request")]

    head = data.get("head") or {}
    pr_head, head_ref = head.get("sha", ""), head.get("ref", "")
    if not pr_head or not head_ref:
        return [Reading("pr_fields", False, "head.sha / head.ref missing")]

    rows: list[Reading] = []
    branch_head = read_ref(f"refs/heads/{head_ref}")
    if data.get("merged"):
        # 合入时若勾了删除分支，`refs/heads/<head.ref>` 就不存在了 —— 不是失败，
        # 但必须写明：此时唯一有意义的判据是 expect_in_base。
        rows.append(Reading("pr_merged", True,
                            f"merged; source branch {'deleted' if not branch_head else branch_head[:9]}"))
    else:
        rows.append(Reading("pr_head==branch_head", pr_head == branch_head,
                            f"pr={pr_head[:9]} branch={branch_head[:9] or '<missing>'}"))

    rows.append(Reading("mirror_ref_is_historical", True,
                        f"refs/pull/{pr}/head={read_ref(f'refs/pull/{pr}/head')[:9] or '<missing>'} "
                        "(记录合并时看到的头，不参与判定)"))

    if expect_in_base:
        rows.append(Reading(f"in_base({expect_in_base[:9]})", is_ancestor(expect_in_base, base_sha),
                            "already in base" if is_ancestor(expect_in_base, base_sha)
                            else "NOT an ancestor of the base tip"))

    rows.append(Reading("base_tip", True, base_sha[:9]))
    return rows


def _real_evaluate(repo: Path, remote: str, base: str, pr: int, token: str, api: str,
                   expect_in_base: str | None) -> list[Reading]:
    def fetch_base() -> str | None:
        code, _ = _git(repo, "fetch", "-q", remote, base)
        if code != 0:
            return None
        code, sha = _git(repo, "rev-parse", "FETCH_HEAD")
        return sha if code == 0 else None

    base_sha = {"value": ""}

    def read_pr() -> dict | None:
        try:
            return _default_pr_reader(number=pr, api=api, token=token)
        except Exception:  # noqa: BLE001 - 读不到就是"无法判定"
            return None

    def read_ref(ref: str) -> str:
        code, out = _git(repo, "ls-remote", remote, ref)
        if code != 0 or not out:
            return ""
        return out.split()[0]

    def is_ancestor(commit: str, tip: str) -> bool:
        return _git(repo, "merge-base", "--is-ancestor", commit, tip)[0] == 0

    def fetch_base_wrapper() -> str | None:
        sha = fetch_base()
        base_sha["value"] = sha or ""
        return sha

    return evaluate(pr=pr, expect_in_base=expect_in_base, fetch_base=fetch_base_wrapper,
                    read_pr=read_pr, read_ref=read_ref, is_ancestor=is_ancestor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, required=False)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default=None,
                        help="git remote 名；省略时自动探测（恰好一个才采用，否则 rc=3）")
    parser.add_argument("--base", default="dev")
    parser.add_argument("--expect-in-base", default=None,
                        help="merge 之后：这个 sha 必须已是 <base> 的祖先")
    parser.add_argument("--api", default=os.environ.get("GITHUB_API_URL", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    args = parser.parse_args(argv)

    if not args.pr:
        print("INPUT_DEFECT reason=missing_pr（无法判定，不是通过）", file=sys.stderr)
        return RC_UNDECIDABLE
    remote = args.remote
    if remote is None:
        remotes = _git(args.repo, "remote")[1].split()
        if len(remotes) != 1:
            print(f"INPUT_DEFECT reason=ambiguous_remote（候选 {remotes or 'none'}；"
                  "用 --remote 明确指定）", file=sys.stderr)
            return RC_UNDECIDABLE
        remote = remotes[0]
    if not args.token or not args.api:
        print("INPUT_DEFECT reason=missing_api_or_token（需要 GITHUB_API_URL + GITHUB_TOKEN）",
              file=sys.stderr)
        return RC_UNDECIDABLE

    rows = _real_evaluate(args.repo, remote, args.base, args.pr, args.token, args.api,
                          args.expect_in_base)
    failed = 0
    for row in rows:
        print(f"{'OK  ' if row.ok else 'FAIL'} {row.name}: {row.detail}")
        failed += 0 if row.ok else 1
    if rows and not rows[0].ok and rows[0].name in {"fetch_base", "pr_readable", "pr_fields"}:
        print(f"INPUT_DEFECT reason=undecidable ({rows[0].name})", file=sys.stderr)
        return RC_UNDECIDABLE
    print(f"SUMMARY pr={args.pr} checks={len(rows)} failed={failed}")
    return RC_OK if failed == 0 else RC_FAIL


if __name__ == "__main__":
    sys.exit(main())
