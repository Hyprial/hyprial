# hyprial

面向 AI agent 与其运行 harness 的去中心 durable 通信层。

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB)](pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-D22128)](LICENSE)

## 简介

hyprial 让分布在不同 harness、进程和机器上的 AI agent 通过统一的消息协议协作。daemon 持有去中心化的 durable inbox 和
路由状态；connector 把 Claude Code、Codex、Pi 等 harness 接入同一网络；
adapter 再把 Lark 等外部平台接到相同的消息面。

它解决的是长任务中的通信连续性问题：协调者、worker 或即时通道暂时不可用时，
任务目标与消息仍应当可发现、可恢复、可核验，而不是依赖某个进程一直在线或
某个人记得上下文。设计目标与长期可执行约束见 [ROADMAP.md](ROADMAP.md)，协议和
架构入口见[设计文档](docs/design/design.md)。

## Quickstart

前置条件：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。分发走公网发布仓
[`Hyprial/hyprial`](https://github.com/Hyprial/hyprial)，安装不需要内网访问权限。

```sh
uv tool install git+https://github.com/Hyprial/hyprial.git@internal
hyprial daemon upgrade --tag internal
hyprial version --json
hyprial init --service
hyprial doctor
```

`hyprial version --json` 在尚未创建 home 时也可用。首次 `hyprial init` 若未找到
登录身份，会在同一进程中自动进入 login，成功后继续启动 daemon。`--service` 会把
daemon 注册为当前用户的 launchd（macOS）或 systemd user（Linux）服务；交互式 init
未给 `--service/--no-service` 时会询问，非交互式默认不改服务。已经 init 的机器仍可
单独运行 `hyprial login` 重新登录。也可以从 login 开始：

```sh
uv tool install git+https://github.com/Hyprial/hyprial.git@internal
hyprial daemon upgrade --tag internal
hyprial login
hyprial daemon service install --yes
hyprial doctor
```

这条路径会在 home 不存在时自动执行 init 的 home 初始化部分，并在身份建立后
启动 daemon，所以最终状态与 `uv tool install → init → doctor` 相同。

加入现有网络前先完成[节点入网清单](docs/guides/onboarding-checklist.md)。首次启动、
显式 Zenoh 端点、macOS 防火墙和诊断细节见[运维指南](docs/guides/operator-guide.md)。

## 安装

正式安装钉[公网发布仓](https://github.com/Hyprial/hyprial/releases)上的 `internal` ——
它是一个**会随发布移动**的轨道 tag，所以这条命令不会过期；⛔ 不要把 `dev` 当作来源
（`dev` 在本仓同时是分支和标签，裸名解析会命中标签）。可执行文件 `hyprial` 默认安装到 `~/.local/bin`，
请确保该目录在 `PATH` 中。

首条 `uv tool install` 只负责引导 CLI；紧接着运行 `hyprial daemon upgrade --tag internal`
会从同一公网 ref 取得 `uv.lock`、导出约束并重装，避免依赖版本随当天的 PyPI 状态漂移。
之后普通 `hyprial daemon upgrade` 和自动更新始终回到公网发布仓。开发者如需一次性验证
Forgejo ref，可用 `hyprial daemon upgrade --source forgejo --ref <完整提交或标签>`；该选择不持久化。

wheel 与 `uv tool install` 不能执行可靠的 post-install hook，因此服务注册必须由
`hyprial init --service` 或 `hyprial daemon service install --yes` 显式完成。服务状态用
`hyprial daemon service status --json` 查看；升级和重启会在服务已接管时通过 supervisor
重启，不会另起一个游离 daemon。

当前没有需要迁移的旧 TypeScript 主机，也不再提供旧状态迁移。若意外发现仍装有
旧版的机器，不要覆盖安装：先停止并删除旧版、归档其状态目录，再按全新主机安装。
可复制的安全步骤见[退役 TS 安装处理](docs/guides/cutover-runbook.md)。

从已消失的旧 `harness-bridge-py` 来源安装过 v0.4.0 时，先用同版本刷新来源，
不会切换到 `dev`。⚠️ **这一条只对内网机器适用**：`v0.4.0` 是历史版本，
**只存在于内部 Forgejo，公网发布仓上没有这个 tag**，所以这里仍用内网地址：

```sh
uv tool install --force git+ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git@v0.4.0
```

### Socialware 安装占位

应用 catalog、制品下载与命令挂载已经退役。`hyprial socialware install <app>`
目前接受兼容参数但恒返回 `COMING_SOON`；它不会访问 catalog、安装应用或改写应用目录。

恢复前提、核验步骤、旧 TypeScript 安装删除与失败边界都保留在
[运维指南的安装章节](docs/guides/operator-guide.md#安装)，不要只凭上面一条命令跳过核验。

## 核心概念

| 概念 | 作用 |
| --- | --- |
| daemon | 持有传输会话、durable inbox、路由和受管运行时状态，是消息与恢复的权威边界。 |
| connector | 把一个具体 harness 会话接到 daemon，承接该 agent 的输入、输出与生命周期。 |
| adapter | 连接 Lark 等外部平台，把平台身份和消息映射到 Hyprial 的统一寻址与投递模型。 |
| PAC | 声明式派单和跟踪层；描述任务图、条件与等待规则，不替 worker 执行任务。 |
| sidecar | 由 Hyprial 校验并控制的辅助进程（Tailcat，随 wheel 分发），用于设备密钥与跨网络转发等独立能力。 |

进一步阅读：

- [daemon 生命周期](docs/reference/p2-daemon.md)
- [Harness provider 模型](docs/reference/harness-model-providers.md)
- [PAC workflow](docs/design/design-pac-workflow.md) 与 [PAC v2 图/flag 反应器](docs/design/design-pac-graph-flag-reactor.md)
- [Lark App onboarding](docs/guides/lark-app-onboarding.md)
- [Tailcat cutover 架构与边车协议 v3](docs/design/tailnet-cutover-architecture-2026-10-03.md)

## 运维指南

原 README 的运维内容没有删除，已整体迁入[运维指南](docs/guides/operator-guide.md)：

- 安装诊断与 v0.4.0 历史来源恢复
- macOS 防火墙与旧 TypeScript 安装删除
- tag-only 升级、三轨选择和自动更新 loop
- profile v2、多 home、`hyprial login` 身份与设备 key，以及 Tailcat 转发边界

升级轨道的治理和发布细节另见[升级轨道](docs/guides/upgrade-tracks.md)，双机与显式端点
部署见 [Zenoh 双机指南](docs/guides/zenoh-two-machine.md)。

## Development

> **开发仍在内部网络进行。** 下面的 clone 地址是内网 Forgejo，只有已加入开发内网的
> 机器能够访问；公网发布仓只承载发行版本，不接收补丁。
> **希望贡献代码，请先联系 maintainer 申请加入开发内网**，拿到内网访问权限后再执行下面的步骤。
> 在此之前可以通过公网发布仓的 issue 反馈问题。

```sh
git clone ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git
cd harness-bridge
uv sync --extra test
npm --prefix src/gui ci --ignore-scripts # Node 24; Python tests include native GUI fixtures.
uv run --with libcst==1.9.0 pytest tests/
uv run ruff check .
```

跨实现契约测试：

```sh
./contract/run-all.sh --isolated-only
```

完整合同、解释器/CLI 选择顺序以及真实环境边界见
[contract/README.md](contract/README.md)。涉及 daemon、provider、Lark 或分发链路的
变更，还应按 [.agents/skills/hyprial-e2e/SKILL.md](.agents/skills/hyprial-e2e/SKILL.md)
选择对应的 E2E 场景。

## Contributing

[`src/gui`](src/gui) 是独立的 `@hyprial/gui` Node 工程；
[`app`](app) 是 Tauri v2 外壳：默认 Core 加载静态页面，不启动本机 runtime；
`--features power-pack` 保留包装 GUI product server 的 legacy 路径。使用 Node 24，
在仓库根执行 `npm --prefix src/gui ci --ignore-scripts`、`npm --prefix src/gui run check`、
`npm --prefix src/gui test` 和 `npm --prefix src/gui run build`；app 单独执行
`npm --prefix app ci --ignore-scripts`、`npm --prefix app test`。
先运行 `npm --prefix app run build:core` 生成静态资产；`npm --prefix app run dev:core` 启动 Core。
Rust 外壳执行 `cargo fmt --manifest-path app/src-tauri/Cargo.toml -- --check`、
`cargo clippy --locked --manifest-path app/src-tauri/Cargo.toml --all-targets -- -D warnings`、
`cargo test --locked --manifest-path app/src-tauri/Cargo.toml`（GUI 依赖已安装，PATH 有 Node ≥24）。
另须对 clippy/test 添加 `--features power-pack` 验证 legacy 层，不能因默认 feature 为空而省略旧 runtime 回归。
启动 GUI 必须显式配置 transport driver 与可信 principal，缺失时 fail closed，不默认接入
mock 或生产 daemon。Python 使用 uv；先运行结构和公共边界门，再跑焦点测试。
具体命令、模块职责与隔离要求见 [GUI workflow](.agents/skills/gui-workflow/SKILL.md)。
GUI 与 app 分别由 `.forgejo/workflows/gui.yml` 和 `desktop-check.yml` 检查；
unit/build 通过不代表浏览器、真实 backend、完整功能等价或已发布安装切换完成，这些仍须独立证据。

欢迎提交问题和补丁。**补丁需要开发内网访问权限** —— 请先联系 maintainer 申请加入，
流程见上面的 [Development](#development)；在此之前可以在
[公网发布仓](https://github.com/Hyprial/hyprial/issues)提 issue。
开发环境、测试要求、提交范围以及仓库现有 agent/harness
约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

本项目按 [Apache License 2.0](LICENSE) 授权。
