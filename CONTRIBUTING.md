# Contributing to Harness Bridge

感谢你改进 Harness Bridge。提交补丁前，请先说明问题、保持改动聚焦，并为行为
变化补上能够先失败再通过的测试。不要把凭据、生产状态或本机私有配置写入提交。

## 开发环境

项目要求 Python 3.12+，依赖和命令由 uv 管理：

```sh
uv sync --extra test
uv run pytest
uv run ruff check .
```

仓库提供的 [uv 命令 hook](.agents/hooks/README.md) 会避免 agent harness 误用
系统 Python。Claude Code 的项目级配置已经在 [.claude/settings.json](.claude/settings.json)
注册；Codex 的用户级配置片段和适用边界也写在同一份 hook 文档中。请复用这些
入口，不要在贡献中再维护一套等价脚本。

## 测试

- 先运行直接覆盖改动的焦点测试，再运行 `uv run pytest` 全量套件。
- 交付前运行全仓 `uv run ruff check .`。
- 涉及跨实现协议时，运行 `./contract/run-all.sh --isolated-only`；需要真实环境的
  场景必须明确报告 PASS、FAIL 或 BLOCKED，不能用 skip 代替结论。
- daemon、provider、Lark 和分发链路的选择与证据要求见
  [.agents/skills/hyprial-e2e/SKILL.md](.agents/skills/hyprial-e2e/SKILL.md)。

测试产生的 home、socket、端口和缓存必须与日常环境隔离。不要读取、改写或清理
生产 daemon、Lark App、凭据与用户级配置。

## 提交与 Pull Request

1. 从目标分支的新 head 建立短分支，保持每个提交可读、可测试。
2. 在 PR 正文说明动机、改动、验证命令和实测结果；未测项显式写 UNKNOWN 或 BLOCKED。
3. 若改变可分发的 agent 操作方式，说明 Skill-Impact，并同步相关 skill。
4. 若改动改变已发布字段（JSON 键、诊断字段、事件名、错误码）的语义，同步
   contract/ 下引用它的文件；确不同步时在 PR 正文写一行
   `Contract-Impact: <为什么这次放行是安全的>`。CI 的 contract-impact 门
   按 PR 自身增量逐字判定（scripts/check_contract_impact.py）。
5. 不自行绕过分支保护，不把 PR 绿等同于合并后目标分支也绿。

Forgejo 的认证、SSH transport、PR 和 CI 操作遵循
[.agents/skills/forgejo-ops/SKILL.md](.agents/skills/forgejo-ops/SKILL.md)；PAC 派单工作流
遵循 [.agents/skills/hyprial-workflow/SKILL.md](.agents/skills/hyprial-workflow/SKILL.md)。

## 报告安全问题

安全问题不要附带真实凭据或生产数据。请通过维护者指定的私有渠道报告，并只提供
复现所需的最小、已脱敏证据。
