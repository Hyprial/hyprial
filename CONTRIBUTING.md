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

旧独立 GUI（`src/gui`）与旧 Tauri 外壳（`app`）已退场，仓库里不再有；共享网页 UI
以根 `gui/` 为唯一主线。CLI 只公开裸 `hyprial gui`
（先 `npm run build`，再由 Python 会话 host 提供 `gui/dist`）和 `hyprial gui dev`（Vite 热更新，
Python 会话 host 反向代理 Vite dev server），在包含
`gui/package.json` 的源码 checkout 内运行；`hyprial gui dev` 可用 `--gui-home` 或
`HYPRIAL_DEV_GUI_HOME` 指向另一个工作树的 `gui/`。`dev` 等价于 `--with-dev-tools --live-source`
两个独立开关（开发工具层、源码热更新），详见 `docs/guides/gui-dev-mode.md`。它经 admission 接入真实 daemon：会话 host 在自己的进程内提供 `request_http`
（安装态与源码态同由 Python 会话 host 承载，已无 Node host，`hyprial gui` 只起一个 Python 子进程），
不恢复旧 Python Web/backend。daemon 是唯一常驻后端：`hyprial gui` 在 home 的每种状态下都先确保
daemon（前提：home 目录已存在且为私有，否则先报 `GUI_HOME_NOT_INITIALIZED` / `GUI_HOME_UNSAFE`），关闭 GUI 不停 daemon，登录与登出在原 GUI 进程内换绑 principal。最小原生壳在 `clients/native/`，
打包在 `packaging/clients/`。操作契约见
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

人和 agent 共用的机器可读规定位于 [`.github/git-flow.json`](.github/git-flow.json)，
CI planner 实际读取它；流程说明见 [dev-first PR 规则](docs/guides/dev-source-branch-rule.md)。

| 分支 | 用途 | 接受的 PR 来源 |
| --- | --- | --- |
| `dev` | 唯一集成主线；从已验证的固定 SHA 选择发布候选 | 完成的功能、修复、文档、CI 与集成 PR |
| `intg/<主题>` | 可选的临时组件集成线；完成后进入 dev | 该主题的功能分支 |
| `release/v<版本>` | 历史发布线保留祖先；新建时仅承载发布准备 WIP，回流 dev | 不继续接收功能 PR |
| `main` | 保留历史发布祖先；push 不再触发正式发布 | 按发布维护需要处理 |

功能分支命名为 `<type>/<slug>`（如 `feat/…`、`fix/…`、`docs/…`），完成后以 dev 为
目标；WIP retarget 后仍保持 Draft。发布候选必须更新对应版本的 CHANGELOG，并使用
不可变 `v<VERSION>` tag；stable 由正式发布链推进。0.5.5.4 收敛期间 internal 与正式
发布写入保持冻结，详见 [发布规则](docs/guides/release-rules.md) 和
[dev 来源分支规则](docs/guides/dev-source-branch-rule.md)。

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
