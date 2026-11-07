# Hyprial 运维指南

本文保留原 README 中已经投入使用的安装、恢复、网络、升级和登录细节。
项目概览与最短上手路径见[仓库 README](../../README.md)。

harness-bridge 的 Python 全栈重写:zenoh 传输层 + IRC 语法层 + 去中心 durable inbox。

- 设计稿:`design.md`(全部裁决已闭合,2026-08-07)
- 契约测试:`contract/`(三件套:wire golden / CLI 黑盒 / E2E 场景;TS 版为 reference implementation)
- daemon 生命周期边界与恢复契约:`p2-daemon.md`
- MCP 无状态代理与 wake 契约:`p3-mcp.md`
- 统一结构化日志 API、路由、脱敏与历史文件归档:`logging.md`
- Squire v3 迁移与配置向导:`p4-squire.md`
- `user:<owner>` 寻址经接收方自己的 Squire adapter 投递；接收 daemon 独占其
  app-scoped open_id，未配置侍从时明确提示改由协调者转达
- 双机(真机 / 本地模拟)Zenoh 显式端点部署:`zenoh-two-machine.md`
- 开发方式:TDD,契约测试红绿驱动;一次性完整开发,P0-P4 为内部里程碑
- 分发:`uv tool install git+ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git@<version-tag>`
- 仓库切换已完成:Python 实现使用 harness-bridge,原 TS 仓(封版于
  71ea88c,46 PR)已改名 harness-bridge-ts 归档

## 安装

```sh
uv tool install git+ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git@<version-tag>
```

可执行文件 `hyprial` 落在 `~/.local/bin`,确保它在 PATH 上。daemon 启动必须
显式配置 Zenoh 端点(否则起来的是一个孤立节点;
listen/connect 全空时 `hyprial init` 会告警、`hyprial doctor` 会报 fail,但端点配了
却不可达仍然全绿),见 `zenoh-two-machine.md`。

有受管 DSH connector 时，`hyprial doctor --json` 还会主动调用其有效 endpoint 的
`host.describe`（不创建会话、不发送 prompt、不调用模型）。同一 endpoint 去重，
单端探测预算最多 2 秒、整组网络探测预算 4 秒，随后关闭传输和未完成的解析；
超出预算的 endpoint 标记 `unprobed` 并报 warn，不以 connector 线程存活判健康。
接口不可达或缺少 provider/model 配置报 fail；成功仅证明接口与配置可见，
不证明模型凭据、额度或推理成功。旧 daemon 未提供 endpoint 时会报 warn，
需检查 daemon 版本及 DSH endpoint 配置。doctor 输出不包含 URL 凭据、查询参数、
路径或 HTTP 错误正文；daemon connector 状态保留实际路由路径，配置中不要将
凭据放入 URL 路径。该状态由 lifecycle actor 的只读投影提供，不新增状态存储。

### Codex 与 Squire 首次启动

当前验证版本为 Codex CLI **0.164.0**（2026-10-26）。已验证技能加载和离线线程创建；模型调用仍需有效凭据。

- `hyprial init` 询问是否安装用户服务。仅试用时可选“否”；需要登录后自动启动 daemon 时选“是”。
- Codex 安装器询问是否卸载已有 npm 版时，只有决定切换安装方式才选“是”。询问是否立即启动时，可选“否”，再继续登录步骤。
- `hyprial agent provider login codex --actor squire` 只保存凭据。首次启动 Squire 时才验证凭据；保存成功不等于凭据有效。
- 启动命令使用 `hyprial agent start`，不再使用 `hyprial start`。例如：`hyprial agent start codex --name squire --headless`。

技能加载失败不会单独阻止 agent 启动。Codex 会跳过有错误的技能。
启动结果的 `warnings` 和 daemon 日志会保留脱敏后的错误原因，最多 4096 字节。
非 JSON 启动命令也会向 stderr 显示提醒。

已有 Squire 可运行 `hyprial squire setup --step create-squire` 补齐技能的 YAML frontmatter。
只有与旧模板渲染结果逐字节相同的文件会自动更新。旧目录迁移也遵循此规则。
如果文件已有自定义内容，setup 会保留内容并提示手工修复。
可在文件开头添加 `docs/domains/squire/SKILL-template.md` 中的 frontmatter，
或先把文件移到备份路径，再重跑 setup。

### 首次启动:放行 macOS 防火墙

新机器上首次启动 daemon 时,macOS 会弹出"允许传入网络连接"的防火墙提示——
**必须点「允许」**。因为 `hyprial` 是 `uv tool install` 的入口点,提示和防火墙允许
列表里显示的名字多半是它的 Python 解释器,而不是 `hyprial`,按这个名字找。

误点「拒绝」是一种静默故障:mesh 无法建立,但 `hyprial doctor` 仍全绿——doctor
只校验配置,不校验可达性(与上面"配了端点却不可达"同一类失败)。补救:在
系统设置 → 网络 → 防火墙 → 选项 中删除该条目以重置授权,再重启 daemon,这次
点「允许」。

### 意外发现旧 TS 安装时：先删除旧版，再安装

Allen 2026-09-11 确认旧 TS 机器已经没有了，因此仓库不再维护状态转换或组合
清理脚本。若盘点之外仍发现旧安装，不迁移旧状态，也不要把 Python 版覆盖到旧
home 上；按[退役 TS 安装处理](cutover-runbook.md)先停掉旧 daemon、归档
`~/.hyprial`，再卸载全局 npm 包：

```sh
npm uninstall -g @hyprial/harness-bridge
uv tool install --force git+ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git@<version-tag>
```

上面两条命令之间必须先完成 runbook 的 PID 与 state 归档检查。新的 Python home
从空目录初始化；此路径**不迁移旧状态**，也不承诺保留旧 channels、secrets、
desired state、users 或消息历史。

### tag-only 升级与排序规则

`hyprial daemon upgrade` 只读取 Forgejo 的全部 tags，绝不跟踪 `dev`、`nightly` 或任何
其他分支。**发什么版 = 在 Forgejo 打什么 tag**；分支与 tag 的绑定、移动和
发布节奏都在 Forgejo 侧管理。每次成功解析(包括 no-op)都会输出
`resolvedTag` 和 `resolvedCommit`；没有可解析 tag 时 fail-loud，不回落分支。

默认候选必须以恰好一个小写 `v` 开头、无 epoch、具有恰好三段 release 号，
并能按 PEP 440 解析。排序使用 `packaging.version.Version` 的完整 PEP 440
次序，默认包含预发布：同一 release 内
`dev < alpha < beta < rc < final < local < post`，例如
`v1.2.3.dev1 < v1.2.3rc1 < v1.2.3 < v1.2.3.post1`；下一 release 的
预发布仍更高，如 `v1.2.4.dev1 > v1.2.3.post1`。PEP 440 等价的不同拼写
以原始 tag 字典序作稳定 tie-break。annotated tag 使用 peeled commit。
团队用 dev tag 发布预发布，因此不另设“只看稳定版”的客户端过滤器。

需要回退、复现或安装非版本 tag 时，使用 `hyprial daemon upgrade --tag <t>` 精确覆盖
（显式 `--tag` 永远优先于任何 track 配置）。

升级轨道（2026-08-20 Allen 定案，task 25）：`settings.json` 的
`updateTrack` 是三轨选择器，可设 `dev` / `nightly` / `stable`，各解析同名
可移动 tag（`dev` = 单测绿后的 dev 分支头，`nightly` = 双闸门全绿的每夜
晋升点，`stable` = 手动发版点）；track 安装钉解析到的确切 commit
（可移动 tag 在解析与 uv 抓取之间可能移动），track tag 缺失 = 报错，
绝不回落最高版本 tag。**未设 `updateTrack` 的机器保持旧"最高版本 tag"
行为逐字节不变**（过渡态；三 tag 初始化完成后缺省才翻为 `stable`）。
`settings.json` 里蛇形 `update_track` 拼写是退役残留，仅触发兼容提示，
不参与解析，也不会被命令写回；`hyprial set update_track` 已移除（track 由运维
直接编辑 settings.json）。**`config.json` 是 TS 版遗留物，Python 版完全不读它** ——
它里面的任何 track 键既不参与解析，也不再触发任何提示。

### 转发(sidecar 联网)开关

Tailcat sidecar 是当前 daemon 转发实现。`hyprial login` 可为当前 home 配置
身份并尝试确保设备 key；它不安装 sidecar，也不执行控制面 join。daemon 的
forwarding `auto` 模式只在 sidecar、设备 key/记录及转发配置可用时启用；它不会
自动下载 sidecar 或替操作员登录。隔离 daemon(`HYPRIAL_NETWORK_ISOLATED=1`)
永不转发。Tailcat 后端可用性、生产网络连通性和组织授权仍须按部署单独验收，
不能从本地配置或 `login` 成功推断。

开关是 `settings.json` 的 `forwarding.mode`,缺省 `auto`;下次 daemon 启动生效:

```sh
hyprial config set forwarding.mode off    # 关闭(连显式的转发变量也一并压住,可作回滚)
hyprial config set forwarding.mode auto   # 缺省:已入网就开,未入网就关并在 ps 里写明原因
hyprial config set forwarding.mode on     # 必须开:不满足条件时 daemon 拒绝启动
hyprial daemon status --json                         # .forwarding.policy 显示生效的模式与来源
```

daemon 环境里的 `HYPRIAL_FORWARDING=off|auto|on` 优先于 settings.json。
`auto` 下未启用时，可检查 `hyprial daemon status --json` 的 forwarding 诊断以及
`hyprial doctor`。若 sidecar 缺失，使用包含 Tailcat 的 Hyprial wheel，或由部署
流程将匹配版本的 `hyprial-tailcat` 安装到 `$HYPRIAL_HOME/bin/`；也可显式设置
`HYPRIAL_TAILCAT_BINARY`。然后运行 `hyprial login` 重试设备 key 配置，并按配置
要求重启 daemon。不要用旧 tsnet 下载参数或登录 flags 修复。具体部署获取方式
若未由组织文档规定则为 UNKNOWN。

### 安全重启 daemon

使用 `daemon restart` 重启，不需要手工拼接 `daemon stop` 与 `init`：

```sh
hyprial daemon restart             # 无进行中 turn 时直接重启；有 turn 时交互确认
hyprial daemon restart --yes       # 明确允许中断列出的进行中 turn
hyprial daemon restart --json      # 非交互；有进行中 turn 时以 RESTART_WOULD_INTERRUPT 拒绝
```

命令在停止前读取 `ps` 的 `connectors[].inFlight`，只列出会被中断的 actor、
harness 与数量，不输出消息内容。成功结果包含重启前后 PID、daemon epoch、版本、
中断清单和耗时；daemon 原本未运行时只启动。若自动更新已有待确认重启，成功的
`hyprial daemon restart` 同时完成确认，清除 pending marker，并把新 PID/epoch 与确认命令
写入 autoupdate last-run；之后 `hyprial daemon upgrade status` 不再显示旧的
“restart waits for confirmation”。

### 自动更新 loop(每天两次，按轨道或最新 tag)

⭐ **默认关(2026-09-15 Allen 定案，spec autoupdate-isolated-home r1)**:
自动升级开关是 `settings.json` 的 `autoUpgrade`（布尔），缺省/未设 = **关**。
未开启时，daemon 内调度器到点只记一条 `autoupdate.run.skipped`
（`reason=disabled`）事件、不起子进程。隐藏的 `upgrade run` 只供 daemon
调度器的子进程使用；daemon 不在线时直接跳过，不会退回本地升级（遗留
launchd/systemd timer 直跑的入口同样被拦，last-run 记录 `skipped/disabled`）。
开启/关闭:

```sh
hyprial config set autoUpgrade true    # 开启自动升级（或 false 关闭）
hyprial daemon upgrade status --json       # autoUpgradeEnabled 字段读出当前开/关
```

手动 `hyprial daemon upgrade` 是操作员显式动作，**不受此开关约束**。开启后仍有
两道安全防线：本进程安装源是 `file://`/本地路径（工作树安装）时拒绝升级
（`UPGRADE_LOCAL_SOURCE`）；执行 `uv tool install` 前校验本进程确实位于 uv
将写入的 tool 目录内，否则拒绝（`UPGRADE_TOOL_DIR_MISMATCH`，fail-closed）。
**迁移影响**：已开 autoupdate 的旧安装升到含此开关的版本后默认变关，需要
自动升级的机器由运维显式 `config set autoUpgrade true`。

每台机器每天 03:17 / 15:17(本地时间)按上述规则检查升级目标（设了
`updateTrack` 的机器解析其轨道 tag，未设的保持最高版本 tag）。调度器由
daemon 自己持有、在独立线程触发升级子进程；升级和新 daemon 因而继承
daemon 的完整环境，且不会占用 IPC accept/reconcile 执行流。机器睡眠错过
时间点会在唤醒后补跑:

```sh
hyprial daemon upgrade status --json  # 进程内调度状态 + 上次运行结果
```

`autoupdate install/uninstall` 已删除。旧版留下的 launchd/systemd 自动更新
单元由 `hyprial daemon upgrade`（或 `hyprial doctor --fix`）的第二个升级步骤
按精确白名单移除；daemon 启动只读检查并报告 pending，不执行迁移。卸载失败会
写入 doctor 并保留单元文件，供再次运行或人工诊断。部署顺序：切换代码后先运行
`hyprial daemon upgrade`（或 `doctor --fix`），随后立即 relaunch 全部 agent
会话，再用 `doctor` 确认 legacy argv/进程计数为 0。旧单元若仍调用已删除的
`hyprial upgrade run` 会 exit 2，因此该步骤必须在删除顶层命令的同一版本执行。

- 执行体是 `hyprial daemon upgrade`:最高 tag 的 commit 未动时是秒级 no-op(一次
  `git ls-remote --tags`);新 tag 安装成功后会平滑重启正在运行的 daemon,
  并报告重启前后的 PID 与版本。无 daemon 时明确跳过;需只换盘时使用
  `--no-restart`。紧急回退使用显式 `--tag`。
- 运行日志:`$HYPRIAL_HOME/state/autoupdate.log`;上次运行结果:
  `$HYPRIAL_HOME/state/autoupdate-last.json`。失败会落盘并在 `status` 里可见,
  不静默。
- 新版本安装后，daemon 会先经 user/Squire 总线发出旧版本→新版本、预计
  中断时长和失败恢复办法；只有收到接收端送达回执才会停止旧 daemon。
  通知失败时升级已落盘，但重启会 fail-closed，具体结果写入 last-run。
- 更新子进程以 daemon 环境运行 `hyprial daemon upgrade` 与 `uv`;git SSH 必须无交互
  可用(`~/.ssh/config` 配 IdentityFile 或常驻 agent)。

## 网络 profile 与多 home

一个 network profile 对应一个 `HYPRIAL_HOME`，`profile.json` 是非机密的
schema v2 记录：`version`、`org`、`issuer`、公开的 OIDC `clientId`，以及可选
`inviteBaseUrl`。旧 v1 的 `controlPlane` 和 `join` 字段已退役，不再配置 Tailscale
或 Headscale；reachability 由 Tailcat sidecar 处理。没有 profile 文件时使用内建
默认 profile；不要把生产 issuer、client id、邀请地址或 key 从文档复制到新部署。

| 东西 | 落点 | 说明 |
| --- | --- | --- |
| profile 记录 | `$HYPRIAL_HOME/profile.json` | 非机密 v2 网络/身份 issuer 坐标；不包含控制面、join 模式或密钥 |
| 身份(owner) | `$HYPRIAL_HOME/settings.json` | `hyprial login` 写入；也受 `HYPRIAL_OWNER` 约束 |
| 登录 refresh token | `$HYPRIAL_HOME/secrets/login.json`（0600） | 由登录流程写入；不应复制或手工编辑 |
| Tailcat device key | `$HYPRIAL_HOME/secrets/tailcat/device-key.json` | 私钥材料；由 sidecar 管理，不得复制或公开 |
| Tailcat device record | `$HYPRIAL_HOME/state/tailcat/device.json` | 公开设备记录；不包含私钥或 PSK |
| Tailcat machine binding | `$HYPRIAL_HOME/state/tailcat/machine-binding.json` | 机器标识的 SHA-256 指纹与轮换元数据；不包含 key 材料 |
| 默认 home | `~/.hyprial` | 无 profile 文件时解析为内建默认 profile |
| 其它 profile homes | `~/.hyprial/profiles/<name>/` | 每个目录是独立 home；resident daemon 的 home 仍由其服务配置决定 |

查看、创建 profile 和选择 home 的命令：

```sh
hyprial profile list [--json]
hyprial profile show [--json]
hyprial profile create <name> --issuer <url> --client-id <public-id> [--invite-base-url <https-url>]
eval "$(hyprial profile use <name>)"  # 只为当前 shell 输出/设置 HYPRIAL_HOME
```

`profile create` 对相同记录幂等，拒绝覆盖不同记录；它不启动 daemon，也不写
settings。`profile use` 不持久化机器级选择指针。issuer/client id 等部署值由组织
管理员提供；这里不记录生产值，未提供的配置为 UNKNOWN。network profile 与
Squire 用户档案 `state/users.json` 是不同概念。

## 用户登录（身份与设备 key）

### 宿主改名与托管访客冲突

旧 owner=X 改为 Y 时，若任一托管 agent 的 owner 是 X 或 Y，迁移以
`OWNER_MIGRATION_HOSTED_CONFLICT` 拒绝；不会自动改写访客或合并账号。
正常 `hyprial login --switch-account`（含 `--no-daemon`）在配置 owner 和凭据
提交前做相同检查，保留原配置；预览也会报告同一错误码。

- 访客 owner=X：先移走或迁出该托管 agent，再改宿主名。
- 访客 owner=Y：选择不与访客碰撞的新 owner。
- 若手工改配置绕过登录检查，daemon 会拒启。撤销那次手工配置改动，恢复
  迁移前的登录配置（owner=X），再执行 `hyprial daemon run`，可按原身份启动。
  不要直接改访客行、删数据库或把访客归到宿主名下。

X→X 不触发此检查；非碰撞改名保留原行为。此拒绝在迁移写入之前发生，
不引入跨库回滚或账号合并。它也不代替既有 secret custody 的独立检查。

### 登录与设备准备

`hyprial login` 对当前 profile 的 issuer 做身份授权；成功后写入 owner 与
refresh token，并调用 `hyprial-tailcat` 确保设备 key/公开设备记录存在。命令选项
以当前 CLI 为准：

```sh
hyprial login [--force] [--no-open] [--json] [--switch-account] [--dry-run] [--no-daemon]
hyprial logout [--yes] [--json]
```

“已登录”是本地、离线判定：`secrets/login.json` 必须是有效凭据，
`settings.json` 必须是 Casdoor 身份，凭据、持久化身份和当前 profile 的 issuer
相同，并且当前 owner 与设备 ID 的 Tailcat device key/公开记录完整。全部满足时，
普通 `login` 返回 `already-logged-in`，报告 owner、issuer、home、凭据取得时间，
并且不写文件、不启动认证；存储格式没有 token 过期字段，因此 `expiresAt` 为 null。
若凭据和身份完整但设备阶段未完成，普通 `login` 只重试设备阶段，不重新认证。
需要重新认证时显式使用 `--force`。

`logout` 只清除当前 `HYPRIAL_HOME` 的本地用户会话状态，包括登录凭据和登录写入的
Casdoor 身份字段。它不会删除 agent 凭据、Tailcat device key 或无关设置。
它不会撤销 issuer 已签发的 access token；这些 token 在过期前仍可能有效。存在登录状态时，命令在
变更前要求交互确认；自动化和 `--json` 模式必须使用 `--yes`。重复执行返回
`not-logged-in` 并成功。若 Casdoor 身份仍在使用，命令先停止 daemon 并证明旧进程
退出，再清理身份；不要用手工删文件替代该流程。

首次 home 初始化与已存在 home 的重登路径不同。更换现有账号需显式
`--switch-account`，并遵循命令对 daemon 状态的检查；不要手工改 owner 或搬运
secrets。daemon 会把 Tailcat identity 绑定到当前机器：Linux 使用 machine-id，
macOS 使用 IOPlatformUUID，Windows 使用 MachineGuid；取不到平台标识时，使用
hostname、硬件 node id 与 OS 名的组合。只持久化带来源标签的 SHA-256 指纹，不保存
原始机器标识或 key 材料。旧 identity 首次启动只补记指纹；复制到不同机器的 home
会自动换发 identity。sidecar 缺失/不可运行时，身份结果不会回滚，设备阶段报告未就绪；先从
批准的构建/分发流程取得匹配 sidecar（wheel 内也可带 sidecar），放入
`$HYPRIAL_HOME/bin/hyprial-tailcat` 或设置 `HYPRIAL_TAILCAT_BINARY`，再运行
`hyprial login` 重试设备阶段。`hyprial doctor` 可诊断 sidecar 与 device-key。

`--json` 的 stdout 只输出一个带 `ok` 的结果对象；设备码、验证链接和过程提示走
stderr，两种模式相同。`ok:false` 的退出码为 1，参数错误为 2。请不要从 stdout
截取登录链接，也不要使用已退役的 join/sidecar-install 参数。

Tailcat 协议 v3 与 profile v2 的范围、命令和当前实现边界见
[Tailcat cutover 架构与边车协议 v3](../design/tailnet-cutover-architecture-2026-10-03.md)。
本指南不提供生产登录、真实 key/tag 或组织邀请操作步骤；生产身份/后端部署参数及
端到端网络验收要求由部署方另行提供，当前 UNKNOWN。`login` 成功、sidecar 版本
通过或本机诊断通过，都不等于生产网络或组织功能已验收。

当前 `service` CLI 提供 daemon 管理的 Tailcat 入站 exposure：
`hyprial org service expose <port> --target unix:/绝对路径` 创建默认 PROXY v2 入口。
unix target 的父目录须属当前用户且权限为 0700；身份来自组织目录映射的 nodekey，
不是调用者自报。tcp 回环 target 必须显式指定 `--proxy-protocol none`，不携带身份。
`hyprial org service unexpose <port>` 删除，`hyprial org service exposures [--json]` 查看期望状态；
`hyprial org service whois <IP:PORT>` 查询已观察地址对应的 peer key（原 `host peer-key`）。
一级 `hyprial ps` 已退役：daemon 状态看 `hyprial daemon status`，
agent/connector 看 `hyprial agent list`。这里只确认
CLI 能力，不代表生产 peer 可达或目标服务已安全配置；实际部署规则由组织运维确定。

## worker 的 provider OAuth 失效(provider_auth)

pi worker 的 provider 凭据(kimi-coding / openai-codex 的 OAuth refresh
grant)失效时,daemon 自动处理,无需值守:

- **A 类(OAuth 失效,可重登录)**:daemon 发起一次 pi 设备码登录(pi 自己的
  `ModelRuntime.login`,凭据由 pi 写回 `~/.pi/agent/auth.json`),并把
  **登录链接 + user code + 过期时刻 + 受影响 worker 短名** DM 给 owner
  (复用 autoupdate 的 owner 通道)。同一 provider 同时只发一条、只跑一个流程;
  每集(episode)最多 3 轮新码,用完发一条收尾通知后停手。owner 点开链接输入
  code 即恢复:daemon 发「已恢复」并解除不可派。
- **C 类(网络抖动、abort、usage-limit 等)**:不发起登录、不写 A/B 标记;但**不再静默**——
  该次请求的**原发送者**会收到一条带当次 attempt identity 的失败通知(见下「fail-loud」)。
- **B 类(账号/权限,重登录解决不了)**:如 `The '<model>' model is not
  supported when using Codex with a ChatGPT account` ⇒ 立即告警(附错误原文摘要),
  ⛔ 不发起登录;只标记出事的那个组合(标记仅用于诊断)。
- **不可派标记**:写在 users.json 的 runtime capability(`reason:
  provider-auth-invalid` / `provider-auth-account`),**仅用于诊断**:PAC 派发 /
  `requires.tier` **不再据它跳过**该 provider,标记也不参与排序或晋级;恢复后仍会解除。
- **fail-loud(2026-09-21 裁定)**:harness/model 的可用性由**用户自己保证**,所以派发
  **假设永远可用**:不探活、不因不可用换 provider/model。代价是不可用必须**报出声**:
  额度耗尽 · api 网络断开 · reconnecting · 弹权限提示卡住 —— 四类都要在界内把一条
  **带本次 delivery/attempt identity + nonce** 的通知发给**等待该次回执的原发送者**
  (不是发回给卡住的 worker 让它代转,也不假定 owner == sender)。「卡住」没有失败事件,
  按各 harness 的静默预算(pi 480s · claude 1620s · codex 2520s;无样本兜底 2520s)
  由时钟触发,正文只声明「无进展、原因待确认」,**不杀 turn、不自动放宽审批**。
  既有 owner 通道(quota watchdog 的额度汇总、provider_auth 告警)保留,但**不替代**
  sender 那一份:2026-09-21 事故里 owner 1 秒内被告知,受损的发送者什么都没有收到。
  契约测试:`tests/test_provider_availability_contract.py` 的三条正控
  (`test_pc1_selection_reads_no_runtime_fact` /
  `test_pc2_failed_attempt_reaches_the_sender_with_this_attempts_evidence` /
  `test_pc3_marked_head_is_still_selected_and_mark_survives`)。
- **daemon 重启**:restore 时发现仍标 broken 且无在飞流程 ⇒ 重新发起一轮
  (新 episode);若 owner 已在停机期间手动登录,则只发恢复通知。

人工兜底:

- 手动重登录:本机运行 `pi`,执行 `/login <provider>`(daemon 会在下一次
  失败/重启时对账到 auth.json 里的新凭据,自动解除标记并发恢复通知)。
- B 类人工处理账号问题后:`hyprial agent tiers probe`(可选 `--combo
  harness:provider:model`)重新探测,成功即写回 available。
- ⛔ 告警正文永远不会包含 access/refresh token 或轮询用的 device_code;
  日志只记 provider、状态迁移与时间(事件名 `provider.auth.*`)。

## Users(daemon identity resolver: `hyprial user`)

`hyprial user` commands read the daemon's merged identity view. Binding rows
include their row-level `source` (`casdoor-login`, `org-directory`,
`local-override`, `legacy-user-bind`, or `legacy-identities`) and `confirmedBy`;
the CLI does not read a local user database directly. `user list --json` keeps
the CLI envelope `{"rows": [...]}` rather than exposing the daemon's
`{"bindings": [...]}` envelope.

```sh
hyprial user add --kind member --owner bob.smith@example.com --real-name 林懿伦 --confirmed-by allen
hyprial user add --kind guest --display-name 王蒴 --confirmed-by hq-adjutant   # 用户 key 由 daemon 生成（成员 = slug(owner)，访客随机），输出里会显示
hyprial user bind <user_key> --union-id on_xxx --confirmed-by allen
hyprial user bind <user_key> --adapter cli-developer --open-id ou_xxx --confirmed-by allen
hyprial user unbind --union-id on_xxx
hyprial user unbind --adapter cli-developer --open-id ou_xxx
hyprial user whois cli-developer ou_xxx
hyprial user whois --union-id on_xxx
hyprial user bindings audit
hyprial user list|show <user_key> [--json]
```

- 成员的 key 是 owner 的 slug;访客的 key 是 `guest-<8 位 hex>`,不从名字派生。
  访客**没有 owner**,消息永远不是任何人的授权。
- `user bind` 只能选择一个目标：`--union-id`，或同时提供 `--adapter` 与
  `--open-id`；`user unbind` 使用相同的两种目标形式且不接受用户或确认人参数。
- `user whois` 查询“这是谁”；`user bindings audit` 只读检查旧的
  `legacy-user-bind` 与 `legacy-identities` 两类行与合并视图的对应关系。
- 一个 `(adapter, open_id)` 已绑给别人时 `bind` 拒绝(`IDENTITY_CONFLICT`),
  ⛔ 不会悄悄改绑;先在原用户上 `unbind`。同一人重复 bind 不产生变化。
- 授权白名单的键必须是经绑定核验的平台身份,绝不能是群或通道；访客没有 owner,
  永远不是任何人的授权。

## 契约测试

全套验证(默认解析本仓 `.venv` 里的 Python 版 `hyprial`;封存的 TS 基线需显式
`--hyprial` 指向归档仓构建产物):

```sh
./contract/run-all.sh
```

纯隔离 CI 验证:

```sh
./contract/run-all.sh --isolated-only
```

双实现选择、生产安全边界及各套件说明见
[`contract/README.md`](../../contract/README.md)。
