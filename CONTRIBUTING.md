# Contributing to Harness Bridge

感谢你改进 Harness Bridge。提交补丁前，请先说明问题、保持改动聚焦，并为行为
变化补上能够先失败再通过的测试。不要把凭据、生产状态或本机私有配置写入提交。

## 开发环境

项目要求 Python 3.12+，依赖和命令由 uv 管理：

```sh
uv sync --extra test
uv run --with libcst==1.9.0 pytest tests/
uv run ruff check .
```

旧独立 GUI（`src/gui`）与 Tauri 外壳（`app`）在退场批次中移出运行路径；共享网页 UI
以根 `gui/` 为唯一主线，由 UI 任务独立 PR 提供。CLI 只公开裸 `hyprial gui`
（`npm start`）和 `hyprial gui dev`（Vite 热更新），需要 `--gui-home` 或
`HYPRIAL_GUI_HOME` 指向包含 `gui/package.json` 的 Git 工作树。它启动独立 Node
占位服务，业务明确返回 `not implemented: hyprial backend not wired`，不接真实
daemon，也不恢复旧 Python Web/backend。操作契约见
[docs/guides/gui-dev-mode.md](docs/guides/gui-dev-mode.md)，退场与恢复范围见
[docs/design/gui-retirement-restore-ledger-2026-10-27.md](docs/design/gui-retirement-restore-ledger-2026-10-27.md)。

仓库提供的 [uv 命令 hook](.agents/hooks/README.md) 会避免 agent harness 误用
系统 Python。Claude Code 的项目级配置已经在 [.claude/settings.json](.claude/settings.json)
注册；Codex 的用户级配置片段和适用边界也写在同一份 hook 文档中。请复用这些
入口，不要在贡献中再维护一套等价脚本。

## 测试

- 先运行直接覆盖改动的焦点测试，再运行 `uv run --with libcst==1.9.0 pytest tests/` 全量套件。
  全套包含一次性迁移工具测试；LibCST 的 pin 来自 `scripts/refactor/pyproject.toml`，
  `--with` 为临时工具叠加，不进入产品依赖或根锁文件。
- 交付前运行全仓 `uv run ruff check .`。
- 涉及跨实现协议时，运行 `./contract/run-all.sh --isolated-only`；需要真实环境的
  场景必须明确报告 PASS、FAIL 或 BLOCKED，不能用 skip 代替结论。
- daemon、provider、Lark 和分发链路的选择与证据要求见
  [.agents/skills/hyprial-e2e/SKILL.md](.agents/skills/hyprial-e2e/SKILL.md)。

测试产生的 home、socket、端口和缓存必须与日常环境隔离。不要读取、改写或清理
生产 daemon、Lark App、凭据与用户级配置。

## 分支模型

| 分支 | 用途 | 接受的 PR 来源 |
| --- | --- | --- |
| `main` | 正式发布；合入后打 `v<VERSION>` tag 并推进 `stable` 轨道 | `dev` |
| `dev` | 集成主线；每次通过 `unit` 门后推进 `internal` 轨道 | 仅 `release/v*` 与 `hotfix/*` |
| `release/v<版本>` | 当前版本的集成分支（release train） | 功能分支与 `intg/*` |
| `intg/<主题>` | 多个 PR 组成的组件集成线 | 该主题的功能分支 |
| `hotfix/<slug>` | 直接修复 `dev` 的小改动 | — |

功能分支命名为 `<type>/<slug>`（如 `feat/…`、`fix/…`、`docs/…`）。目标为 `release/**`
的 PR 必须改动 `CHANGELOG.md` 中对应版本的小节。规则来源见
[发布规则](docs/guides/release-rules.md) 与 [dev 来源分支规则](docs/guides/dev-source-branch-rule.md)。

公网 GitHub 仓库 `Hyprial/hyprial` 是只读发布镜像，不接受 PR；补丁请在内部 Forgejo 提交。

## 提交与 Pull Request

1. 从目标分支的新 head 建立短分支，保持每个提交可读、可测试。
2. 在 PR 正文说明动机、改动、验证命令和实测结果；未测项显式写 UNKNOWN 或 BLOCKED。
3. 若改变可分发的 agent 操作方式，说明 Skill-Impact，并同步相关 skill。
4. 若改动改变已发布字段（JSON 键、诊断字段、事件名、错误码）的语义，同步
   contract/ 下引用它的文件；确不同步时在 PR 正文写一行
   `Contract-Impact: <为什么这次放行是安全的>`。CI 的 contract-impact 门
   按 PR 自身增量逐字判定（scripts/verify/check_contract_impact.py）。
5. 不自行绕过分支保护，不把 PR 绿等同于合并后目标分支也绿。

Forgejo 的认证、SSH transport、PR 和 CI 操作遵循
[.agents/skills/forgejo-ops/SKILL.md](.agents/skills/forgejo-ops/SKILL.md)；PAC 派单工作流
遵循 [.agents/skills/hyprial-workflow/SKILL.md](.agents/skills/hyprial-workflow/SKILL.md)。

## 报告安全问题

安全问题不要附带真实凭据或生产数据。请通过维护者指定的私有渠道报告，并只提供
复现所需的最小、已脱敏证据。
