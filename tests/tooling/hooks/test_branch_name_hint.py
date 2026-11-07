"""`.agents/hooks/branch-name-hint.py` 的行为测试。

行为契约（README 与脚本 docstring 都写着）：建分支时名字不带本仓的类型前缀就
提醒一次；合规、列分支、切已有分支、坏 JSON 一律静默放行 —— 这条 hook 永不放行
之外的东西，也永不阻断命令。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / ".agents" / "hooks" / "branch-name-hint.py"


def _run(command: str) -> tuple[int, str, str]:
    payload = json.dumps({"tool_input": {"command": command}})
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return done.returncode, done.stdout, done.stderr


def test_hook_exists_and_is_executable() -> None:
    assert HOOK.is_file()


def test_non_prefixed_branch_name_is_reminded() -> None:
    rc, out, err = _run("git checkout -b alarm-inchannel-dm")
    assert rc == 0
    assert out == ""
    assert "alarm-inchannel-dm" in err
    assert "<type>/<slug>" in err


def test_compliant_branch_names_are_silent() -> None:
    for command in (
        "git checkout -b feat/thing",
        "git switch -c fix/other",
        "git worktree add -b docs/note /tmp/w",
        "git branch test/unit",
    ):
        rc, out, err = _run(command)
        assert (rc, out, err) == (0, "", ""), command


def test_ambiguous_or_listing_commands_are_silent() -> None:
    for command in (
        "git branch",
        "git status",
        "git checkout main",
        "git log --oneline",
        "uv run pytest tests/",
    ):
        rc, out, err = _run(command)
        assert (rc, out, err) == (0, "", ""), command


def test_hook_never_blocks_on_bad_input() -> None:
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0
    assert done.stdout == ""
