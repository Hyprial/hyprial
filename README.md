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
某个人记得上下文。设计目标与长期可执行约束见 [GOALS.md](GOALS.md)，协议和
架构入口见[设计文档](docs/design.md)。

## Quickstart

前置条件：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。分发走公网发布仓
[`Hyprial/hyprial`](https://github.com/Hyprial/hyprial)，安装不需要内网访问权限。

```sh
uv tool install git+https://github.com/Hyprial/hyprial.git@<version-tag>
hyprial version --json
hyprial init
hyprial doctor
```

`hyprial version --json` 在尚未创建 home 时也可用。首次 `hyprial init` 若未找到
登录身份，会在同一进程中自动进入 login，成功后继续启动 daemon；已经 init 的
机器仍可单独运行 `hyprial login` 重新登录。也可以从 login 开始：

```sh
uv tool install git+https://github.com/Hyprial/hyprial.git@<version-tag>
hyprial login
hyprial doctor
```

这条路径会在 home 不存在时自动执行 init 的 home 初始化部分，并在身份建立后
启动 daemon，所以最终状态与 `install → init → doctor` 相同。

加入现有网络前先完成[节点入网清单](docs/onboarding-checklist.md)。首次启动、
显式 Zenoh 端点、macOS 防火墙和诊断细节见[运维指南](docs/operator-guide.md)。

## 安装

正式安装必须钉[公网发布仓](https://github.com/Hyprial/hyprial/releases)上的版本 tag；
不要把 `dev` 当作版本来源。可执行文件 `hyprial` 默认安装到 `~/.local/bin`，
请确保该目录在 `PATH` 中。

当前没有需要迁移的旧 TypeScript 主机，也不再提供旧状态迁移。若意外发现仍装有
旧版的机器，不要覆盖安装：先停止并删除旧版、归档其状态目录，再按全新主机安装。
可复制的安全步骤见[退役 TS 安装处理](docs/cutover-runbook.md)。

从已消失的旧 `harness-bridge-py` 来源安装过 v0.4.0 时，先用同版本刷新来源，
不会切换到 `dev`：

```sh
uv tool install --force git+https://github.com/Hyprial/hyprial.git@v0.4.0
```

### 取源慢／认证失败的诊断与应急 ssh 改写（内网开发者适用）

> 本节只适用于 catalog 仍指向内部 Forgejo 的场景，也就是已加入开发内网的贡献者。
> 从公网发布仓安装 hyprial 本身不经过下面这些地址。

`hyprial install <app>` 从 catalog 钉住的 git 源取应用，走 `code.hyprial.com` 的 https。
两类失败会在原始报错之后追加提示：

- **认证失败**：forge 已关闭匿名读（`info/refs` 匿名 401），`code.hyprial.com`
  需要登录才能读取。请用**你自己的 Forgejo token** 为本机配置 git 凭据
  （credential helper 或系统钥匙串）。安装器不打印、也不保存 token。
- **超时**：该地址经公网 Cloudflare 隧道，慢是常态。

**应急**（仅 tailnet 内）：把它改走 ssh 内网，绕开隧道与匿名限制。这是**机器全局的
git 改写**（`url.<base>.insteadOf`），会影响本机所有 git；安装回执里记录的仍是 catalog
地址。两种写法：

**永久**（写进 `~/.gitconfig`）：

```sh
git config --global url."ssh://git@git.internal.hyprial.com/".insteadOf https://code.hyprial.com/
```

**单次**（不改任何配置文件）：

```sh
env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.ssh://git@git.internal.hyprial.com/.insteadOf GIT_CONFIG_VALUE_0=https://code.hyprial.com/ hyprial install <app> --yes
```

本机设了 `HTTP(S)_PROXY` 时，再确认 `NO_PROXY` 是否包含 `code.hyprial.com`。
提示只在超时与认证失败时出现；其它非零退出的 git 失败报错逐字不变。
同一段提示也收录在 `hyprial-ops` skill 里。

恢复前提、核验步骤、旧 TypeScript 安装删除与失败边界都保留在
[运维指南的安装章节](docs/operator-guide.md#安装)，不要只凭上面一条命令跳过核验。

## 核心概念

| 概念 | 作用 |
| --- | --- |
| daemon | 持有传输会话、durable inbox、路由和受管运行时状态，是消息与恢复的权威边界。 |
| connector | 把一个具体 harness 会话接到 daemon，承接该 agent 的输入、输出与生命周期。 |
| adapter | 连接 Lark 等外部平台，把平台身份和消息映射到 Hyprial 的统一寻址与投递模型。 |
| PAC | 声明式派单和跟踪层；描述任务图、条件与等待规则，不替 worker 执行任务。 |
| sidecar | 由 Hyprial 校验并控制的辅助进程，用于 tsnet 入网、跨 tailnet 转发等独立能力。 |

进一步阅读：

- [daemon 生命周期](docs/p2-daemon.md)
- [Harness provider 模型](docs/harness-model-providers.md)
- [PAC workflow](docs/design-pac-workflow.md) 与 [PAC v2 图/flag 反应器](docs/design-pac-graph-flag-reactor.md)
- [Lark App onboarding](docs/lark-app-onboarding.md)
- [tsnet sidecar 与登录设计](docs/hyprial-login-design.md)

## 运维指南

原 README 的运维内容没有删除，已整体迁入[运维指南](docs/operator-guide.md)：

- 安装诊断与 v0.4.0 历史来源恢复
- macOS 防火墙与旧 TypeScript 安装删除
- tag-only 升级、三轨选择和自动更新 loop
- 网络 profile、多 home、`hyprial login` U1/U2/U3b 与 sidecar 入网

升级轨道的治理和发布细节另见[升级轨道](docs/upgrade-tracks.md)，双机与显式端点
部署见 [Zenoh 双机指南](docs/zenoh-two-machine.md)。

## Development

> **开发仍在内部网络进行。** 下面的 clone 地址是内网 Forgejo，只有已加入开发内网的
> 机器能够访问；公网发布仓只承载发行版本，不接收补丁。
> **希望贡献代码，请先联系 maintainer 申请加入开发内网**，拿到内网访问权限后再执行下面的步骤。
> 在此之前可以通过公网发布仓的 issue 反馈问题。

```sh
git clone ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git
cd harness-bridge
uv sync --extra test
uv run pytest
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

GUI 源码现在位于 [`src/gui`](src/gui)，保持独立 Node 工程。开发时在该目录执行
`npm ci --ignore-scripts`、`npm run build:static` 和 `npm test`。
GUI 的 CI 位于 `.forgejo/workflows/gui.yml`；源码发布包构建及隔离安装演练见
[GUI 合仓实施记录](docs/gui-migration/implementation.md)。源码合仓尚不代表公开
catalog 或现有用户安装已切换，发行切换条件也记录在该文档中。

欢迎提交问题和补丁。**补丁需要开发内网访问权限** —— 请先联系 maintainer 申请加入，
流程见上面的 [Development](#development)；在此之前可以在
[公网发布仓](https://github.com/Hyprial/hyprial/issues)提 issue。
开发环境、测试要求、提交范围以及仓库现有 agent/harness
约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

本项目按 [Apache License 2.0](LICENSE) 授权。
