# hooks —— 在本仓内强制 python 走 uv

## 为什么
本仓用 uv 管理 Python 环境。裸跑 `python`/`python3` 可能命中系统解释器、丢掉
uv 锁定的依赖。这个 hook 在**命令执行前**把裸调用改写成 `uv run python`。

## 一个脚本,两个 harness
`enforce-uv.py` 同时服务 **Claude Code** 与 **codex** —— 两者的 `PreToolUse`
hook 契约同构(stdin 读 JSON、`tool_input.command` 是命令、输出 `updatedInput`
改写)。所以**逻辑只有一份**,只是注册位置不同:

| harness | 注册处 | 本仓提供 |
|---|---|---|
| Claude Code | 项目 `.claude/settings.json` 的 `PreToolUse`(matcher `Bash`) | ✅ 已就地生效 |
| codex | `~/.codex/config.toml` 的 `[hooks.PreToolUse]`(matcher `shell`) | 片段见 `codex-hook.toml`,需把绝对路径填对后并入用户全局 config |

> codex 的 hook 是**用户级全局配置**(`~/.codex/config.toml`),不像 Claude 有
> 项目级 settings.json 就地生效 —— 这是 codex 侧的机制限制,不是本仓能改的。
> 所以 codex 侧需要**一次性**把 `codex-hook.toml` 的片段并进全局 config(把
> `/ABSOLUTE/PATH/TO/...` 换成本仓的真实绝对路径)。并入后对所有 codex 会话生效。

## 改写规则(保守,fail-open)
- 只改写**直接以 `python`/`python3` 开头**的简单命令 → 加 `uv run` 前缀。
- **不动**:已在 `uv ` 下的、含 `&&`/`||`/`;`/`|`/换行 的复合命令(前缀语义会错)。
- 任何解析异常一律**放行原命令** —— hook 绝不因自身故障挡住正常工作。

## 自测
```
echo '{"tool_input":{"command":"python x.py"}}' | .agents/hooks/enforce-uv.py
# → 输出 updatedInput.command = "uv run python x.py"
echo '{"tool_input":{"command":"uv run pytest"}}' | .agents/hooks/enforce-uv.py
# → 无输出(不改写)
```

## 分支命名提醒

本仓分支名用 `<type>/<slug>`（type 与提交类型同一套）。`branch-name-hint.py` 在
**创建/切换分支**的命令上检查名字，不合规就往 stderr 打一条提醒，然后原样放行。

- 覆盖的形状：`git checkout -b/-B/--branch NAME`、`git switch -c/-C/--create/--force-create NAME`、
  `git worktree add -b/-B NAME`、`git branch NAME`。
- **只提醒、不阻断**：解析异常、命令形状拿不准、`git branch` 列分支、`git checkout` 切已有分支，一律静默放行。
- 前缀全表在脚本的 `ALLOWED_PREFIXES`（2026-10-13 按 origin 远端 ref 实测统计）。

注册方式与 `enforce-uv.py` 相同：Claude Code 见 `.claude/settings.json`（项目级，
已就地生效）；codex 见 `codex-hook.toml` 的两段片段（需把绝对路径填对后并入
`~/.codex/config.toml`）。

自测：

```
echo '{"tool_input":{"command":"git checkout -b alarm-inchannel-dm"}}' | .agents/hooks/branch-name-hint.py
# → stderr 有一条提醒

echo '{"tool_input":{"command":"git switch -c fix/thing"}}' | .agents/hooks/branch-name-hint.py
# → 无输出（合规）
```
