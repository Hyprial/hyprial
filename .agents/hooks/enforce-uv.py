#!/usr/bin/env python3
"""PreToolUse hook: 在本仓内,把裸 python/python3 调用改写为 `uv run python`。

同时服务 Claude Code 与 codex 两种 harness —— 两者的 PreToolUse hook 契约同构:
- 从 stdin 读一个 JSON;Bash/shell 工具的命令在 tool_input.command。
- 要改写命令时,输出 {"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow", "updatedInput": {...}}}(Claude 语义);
  codex 侧读同一结构的 updatedInput 生效。
- 不改写时静默 exit 0,原样放行。

只改写"直接以 python/python3 开头"的简单调用,避免误伤管道/重定向/已在 uv 下的命令。
设计目标是 fail-open:任何解析异常都放行原命令,绝不因为 hook 挡住正常工作。
"""
import json
import re
import sys


def rewrite(cmd: str) -> str | None:
    """把裸 python/python3 起头的命令改成 uv run。返回 None 表示不改写。"""
    stripped = cmd.lstrip()
    # 已经在 uv 下 / 不是 python 起头 —— 不动
    if stripped.startswith("uv "):
        return None
    m = re.match(r"^(python3?)(\s|$)", stripped)
    if not m:
        return None
    # 只处理"命令以 python 开头"的直接调用;含 shell 复合结构时保守放行,
    # 因为 `uv run` 前缀套在 `a && python b` 这类上语义会错。
    if any(tok in cmd for tok in ("&&", "||", ";", "|", "\n")):
        return None
    lead_ws = cmd[: len(cmd) - len(stripped)]
    return f"{lead_ws}uv run {stripped}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # 读不到/坏 JSON —— 放行
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0
    new_cmd = rewrite(cmd)
    if new_cmd is None or new_cmd == cmd:
        return 0
    updated = dict(tool_input)
    updated["command"] = new_cmd
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
