#!/usr/bin/env python3
"""PreToolUse hook: 建分支时提醒本仓的分支命名约定。

本仓约定 ``<type>/<slug>``（conventional-commit 的类型当前缀）。实测在用前缀见
:data:`ALLOWED_PREFIXES`。这个 hook 只在**创建/切换分支**的命令上检查名字，名字
不带已知类型前缀时往 stderr 打一条提醒，然后**原样放行**。

它与 ``enforce-uv.py`` 同构：从 stdin 读 PreToolUse 的 JSON，命令在
``tool_input.command``；同时服务 Claude Code（项目 ``.claude/settings.json``）
与 codex（``~/.codex/config.toml`` 的 ``[hooks.PreToolUse]`` 片段，见
``codex-hook.toml``）。

设计目标是**只提醒、不拦**：任何解析异常、任何拿不准的形状都静默退出 0，绝不
因为命名不符合约定而挡住一条本来应当执行的命令。
"""

from __future__ import annotations

import json
import re
import shlex
import sys

#: 本仓实测在用的分支类型前缀（2026-10-13 按 origin 远端 ref 统计）。
ALLOWED_PREFIXES: tuple[str, ...] = (
    "fix",
    "feat",
    "work",
    "intg",
    "docs",
    "bb",
    "ci",
    "test",
    "refactor",
    "design",
    "wk",
    "scratch",
    "spike",
    "plan",
    "audit",
    "perf",
    "instrument",
    "promote",
    "legs",
    "hotfix",
    "release",
    "intg-check",
    "review",
)

#: ``git checkout``/``git switch``/``git worktree add`` 里"新分支名紧随其后"的旗标。
_CREATE_FLAGS = {
    "-b",
    "-B",
    "--branch",
    "-c",
    "-C",
    "--create",
    "--force-create",
}

_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _new_branch_name(command: str) -> str | None:
    """返回命令要创建/切换到的分支名；拿不准时返回 ``None``（放行）。"""

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or argv[0] != "git":
        return None
    if len(argv) < 3:
        return None

    sub = argv[1]
    rest = argv[2:]

    if sub == "worktree":
        # git worktree add [-b NAME] <path> ...
        if not rest or rest[0] != "add":
            return None
        return _flag_value(rest[1:])
    if sub in {"checkout", "switch"}:
        return _flag_value(rest)
    if sub == "branch":
        # git branch NAME（创建）；列分支/带旗标的一律放行。
        if not rest or rest[0].startswith("-"):
            return None
        return rest[0] if _BRANCH_NAME.match(rest[0]) else None
    return None


def _flag_value(args: list[str]) -> str | None:
    for index, token in enumerate(args):
        if token in _CREATE_FLAGS:
            if index + 1 < len(args):
                candidate = args[index + 1]
                return candidate if _BRANCH_NAME.match(candidate) else None
            return None
        # --branch=NAME 形式
        for flag in _CREATE_FLAGS:
            prefix = f"{flag}="
            if token.startswith(prefix):
                candidate = token[len(prefix) :]
                return candidate if _BRANCH_NAME.match(candidate) else None
    return None


def reminder(branch: str) -> str | None:
    """名字合规时返回 ``None``，否则返回提醒文本。"""

    prefix, _, slug = branch.partition("/")
    if not slug or prefix not in ALLOWED_PREFIXES:
        allowed = "、".join(ALLOWED_PREFIXES[:8])
        return (
            f"⚠️ 分支名 {branch!r} 未带本仓的类型前缀。约定 `<type>/<slug>`"
            f"（如 {allowed} …），现有前缀全表见 .agents/hooks/branch-name-hint.py。"
            "这条只是提醒，命令已照常执行。"
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # 读不到/坏 JSON —— 放行
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    try:
        branch = _new_branch_name(command)
        message = None if branch is None else reminder(branch)
    except Exception:
        return 0  # 解析异常一律放行
    if message is not None:
        print(message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
