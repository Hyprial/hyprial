# harness-bridge Python 全栈重写设计稿 v1

- 日期:2026-08-07
- 作者:hq-adjutant(经 Allen 四问 grill 裁决后成稿)
- 状态:待 Allen 终审
- 决策基线(均已裁决):Python 全栈 / zenoh 通信层 / **IRC 语法不带 server** / 尽可能去中心化 / 契约测试先行 / uv+git 分发 / 开发期 TS 版持续运行,完整收尾后一日切换
- 参考:research-zenoh.md(一轮)、research-zenoh-topology.md(二轮)、spec-squire.md v3(需求基准)、PR #41 审计矩阵(输入材料)

> **2026-10-27 临时退场说明**：旧 `src/gui/` 与 Tauri/Electron `app/` 及其 Python
> bundle/product/unpack 消费者在退场批次中移出运行路径；Tailcat 载荷与共享
> adapter/授权/会话代码保留。本文第 1 节中 `src/gui/`、`desktop/src/` 的目录与构建
> 描述是退场前的历史设计，只用于后续恢复对照。当前 CLI 入口为裸 `hyprial gui` 与
> `hyprial gui dev`，操作契约见 [GUI source mode](../guides/gui-dev-mode.md)；当前活动
> 入口另见 [README](../../README.md)、[AGENTS](../../AGENTS.md) 与
> [GUI 退场恢复台账](gui-retirement-restore-ledger-2026-10-27.md)。新共享 UI 按
> `gui/`、`clients/native/`、`packaging/clients/` 独立 PR 迁入，不恢复旧 `src/gui`。

---

## 0. 三层架构总览

```text
┌─────────────────────────────────────────────────────────┐
│ 上层:adapter + connector                                 │
│   provider connectors(claude/pi/codex 进程管理+注入)      │
│   飞书 adapter(oapi-sdk-python 长连接)                    │
│   MCP server(官方 python-sdk,固定身份 stdio)              │
│   CLI(hyprial 命令,uv tool 分发)                             │
├─────────────────────────────────────────────────────────┤
│ 中层:harness network = IRC 语法层(sans-io)               │
│   irctokens 行协议编解码(RFC1459 + IRCv3 tags)            │
│   频道/昵称/成员语义,msgid 回执关联,CAP 能力协商            │
│   durable inbox 协议(outbox→收件方 storage ack)           │
├─────────────────────────────────────────────────────────┤
│ 底层:纯通信 = zenoh(zenoh-python 官方绑定)                │
│   tailnet 内 peer mesh(gossip 发现,router 可选)           │
│   storage+filesystem 后端(收件人本机持久化)               │
│   liveliness(presence)/queryable(registry/discovery)     │
└─────────────────────────────────────────────────────────┘
```

信任模型(已裁决,承 decouple-identity):网络隔离与身份认证**全部下沉 tailnet**;无 login/enroll/凭据续期;envelope 级本地 Ed25519 签名(公钥挂 user 档案)标识发送方,内网不做防冒充(未来如需走完整 ACL)。

## 1. 模块切分(退场前历史布局;当前状态见顶部 2026-10-27 说明)

本节保留退场前的深层模块与旧 GUI 落点，供后续恢复对照；当前活动仓库布局以
[AGENTS](../../AGENTS.md) 和 [README](../../README.md) 为准。后续协议、信任模型与
历史裁决保持其原有讨论范围。
每个 Python 域只有一套公共契约面：`interface.py` 声明，由包入口导出；实现位于
`impl/`，跨域消费者只能依赖公共面。依赖分层为 kernel → identity → daemon → biz → shell
（上层消费下层），薄 composition root 负责注入依赖，不建立第二套状态权威。

```text
src/hyprial/
├── kernel/{interface.py,impl/}    # 协议/值类型、actor 基础设施、平台与进程原语
├── identity/{interface.py,impl/}  # user/agent 身份、注册与 home 边界
├── daemon/{interface.py,impl/}    # durable inbox/路由、connector/adapter 与受管运行时
├── biz/{interface.py,impl/}       # PAC/routine 与业务编排，消费 daemon 的消息面
├── shell/{interface.py,impl/}     # CLI、登录/诊断等应用组合；impl/gui 现为 GUI_UNAVAILABLE 兼容面
├── cli.py                       # CLI 薄入口
└── gui_build_hook.py             # 仅 Tailcat 载荷构建薄入口
# 以下为 2026-10-27 前的历史布局，已临时退场（见顶部说明）：
src/gui/                         # 旧独立 Node GUI；不是 agent runtime
├── {client,session,studio,host,presentation}/index.mjs
└── {transport,product}/index.mjs # backend 注入与 product/server.mjs 组合根
desktop/src/                     # 旧 Electron 外壳，启动并包装同一 GUI server
contract/                        # 仓库级契约 corpus 与 runner，不是 Python 产品子包
```

以下旧 GUI 约束在临时退场期间不适用，仅供后续恢复对照：

Python 依赖以 `pyproject.toml`/`uv.lock` 为准，命令使用 uv；旧 GUI 以自身
`package.json`/`package-lock.json` 为准，使用 Node 24。GUI session/transport 是 daemon
业务状态的投影和适配面，不另建 durable inbox、业务 session 或 turn 调度权威。
host 接收可信身份解析器与端口注入；Cordis 插件是受信本地代码，Studio 只接受声明式 schema，
不是任意 eval 或插件沙箱。GUI product/desktop 缺少显式 backend 或可信 principal 时 fail closed；
mock 必须明确选择，仅用于隔离开发/测试，不证明真实 backend 的连通与授权。

`scripts/ci/check_structural_limits.py` 对三个源码根执行每个含代码目录最多 7 个直接文件、
每个代码文件最多 1000 行的硬限制；`scripts/ci/check_module_boundaries.py` 检查五域公共面，
GUI 的 `npm run check` 检查其 `index.mjs` 边界。结构门是静态证据，不替代运行时契约和 E2E；
独立 GUI 的真实 backend、浏览器交互和完整功能等价仍需独立验收。

## 2. 协议映射:现 envelope → IRC 语法

| 现概念 | IRC 映射 | 备注 |
|---|---|---|
| actor(agent:owner:machine:name) | 昵称 `name` + tag `+hyprial/actor=<full-id>` | 昵称短名即现 nickname 系统 |
| 会话/conversation | 频道 `#<conv>` 或私聊目标 | 群组=频道,DM=nick 目标 |
| harness_send 文本 | `PRIVMSG <target> :<text>` | 长文本用 IRCv3 `BATCH`(multiline) |
| messageId/回执关联 | IRCv3 `msgid` tag + `+draft/reply` tag | 标准化 #21 的轮子 |
| intent(request/reply) | tag `+hyprial/intent=` | reply 同时带 reply tag |
| harness_ack(出队不回复) | `TAGMSG` + `+hyprial/ack` | 纯 tag 消息,无正文 |
| 资源/多媒体 | 正文只放 manifest(哈希+尺寸+取用地址),sidecar 处理 | 已裁决:多媒体走 sidecar 二次处理 |
| presence/上下线 | `JOIN`/`PART`/`QUIT` + zenoh liveliness | 双源合一:liveliness 是真值,IRC 事件是播报 |
| registry 查询 | zenoh queryable(不走 IRC) | 服务发现属传输层 |
| 能力协商 | IRCv3 `CAP LS/REQ` | 版本演进的正门 |
| kanban/协议指令 | 正文约定(现状照搬) | 对 watcher 无感 |

**命名词表 v2(2026-08-07 12:09 Allen 方向 + 协调者定稿,待最终确认)**:`channel` = **IRC 频道**(群组会话空间,回归 IRC 原语);`adapter` = **外部平台接入**(飞书 app 等;connector 已被 provider 占用故不选,proxy 语义偏同协议转发故不选);`connector` = provider 进程(claude/pi/codex,现状不变);`link` = zenoh 链路(仅实现文档)。room/gateway 两词退役。CLI 形态:`hyprial adapter start lark-main`、`hyprial channel join #hyprial-dev`。

## 3. 去中心 durable inbox 协议(核心自研件)

- **收件箱在收件人机器**:zenoh storage(filesystem 后端)挂 `hyprial/v1/inbox/<actor>/**`,只有本机写入生效(他人经 put 投递,由本机 inbox 服务落库)。
- **投递流程**:发送方写本机 outbox(先落盘)→ 向收件人 inbox key put(携带 msgid)→ 收件方 inbox 服务收到即落库并回 ack(queryable 应答或 receipt key)→ 发送方收 ack 删 outbox 项;未 ack 按退避重试(收件人离线即自然排队在发送方 outbox)。
- **去重**:收件方按 msgid 幂等(24h 窗口,SQLite 或 storage 内记录);**排序**:每发送方序号 + 收件方按到达序投递(跨发送方不保序,与现状一致)。
- **双离线兜底:mailbox 角色(已裁进首版,2026-08-07 12:19)**:任意 daemon 以 `--as-mailbox`(配置项,入 desired-state)志愿承担,**角色而非特殊节点**。协议:①多 mailbox 并存无需选主——收件方 msgid 幂等使冗余投递无害,越多越可用,零协调;②**托管移交(custody transfer)**:发件方直投失败 → 交任一在线 mailbox → mailbox 先落盘再签收(custody-ack)→ 发件方清 outbox 可下线 → mailbox 接管重投义务至收件方真实 ack;发件方回线重投由收件方幂等兜底;③礼貌缓存边界:TTL(7 天)+容量上限(1GiB),发现走 liveliness 令牌 `mailbox/<node>`;内网明文(未来 E2E 加密留 ACL 阶段钩子)。
- **DLQ**:重试超限移入本机 `dlq/<actor>/**`,doctor 可查。
- **消费位点**:agent 会话经 daemon 读 inbox(FIFO),ack 后标记消费;崩溃后按未消费位点续投——语义等价现 JetStream WorkQueue,实现载体换为本机 storage,**位点漂移类故障(今晨流配置漂移)结构性消失**(单机自治,无远端 provision)。

## 4. 契约测试库(Phase 0,冻结例外已批)

三件套,语言无关,TS 版作 reference implementation:
1. **wire golden**:envelope JSON 样本 × 预期行为(路由/回执/去重/错误语义),含全部已修 bug 的回归样本(#21 历史引用、#32 回退、#38 ack、离线拒绝→排队 R10 等);
2. **CLI 黑盒**:`hyprial <cmd> --json` 输入输出断言套件,shell 可执行,双实现同跑;
3. **E2E 场景脚本**:双机收发/重启存活/pin 持久/飞书往返(源自 verifier protocol 的证据矩阵)。
验收原则:Python 版每模块以对应契约段红绿驱动;全绿 = 行为等价证明。

## 5. 分发与升级(uv,已查证)

- 安装:`uv tool install git+ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git@<version-tag>`——无 registry，发布权在 Forgejo tag；
- tag-only(Allen 2026-08-15 裁定):`hyprial daemon upgrade` 用一次 `git ls-remote --tags` 读取全部 tag，只接受恰好一个小写 `v` 前缀、无 epoch、恰好三段 release 号且 PEP 440 可解析的默认候选；以 `packaging.version.Version` 排序，包含 dev/a/b/rc 等预发布以及 local/post 版本，同一 release 为 `dev < a < b < rc < final < local < post`，下一 release 的 dev 高于上一 release 的 post；PEP 440 等价拼写用原始 tag 字典序稳定决胜，annotated tag 取 peeled commit；无候选 fail-loud，永不回落分支；`--tag <t>` 精确覆盖；每个成功/no-op 结果都报告 `resolvedTag` + `resolvedCommit`；**发什么版 = 在 Forgejo 打什么 tag**；
- 三轨升级轨道(Allen 2026-08-20 定案，task 25):`settings.json` `updateTrack` 复活为 opt-in 轨道选择器 —— `dev`/`nightly`/`stable` 各解析同名可移动 tag 并把安装钉在解析到的 commit 上（防 movable tag 竞态 + 防 requested_revision 劣化导致的每 tick 重启）；track tag 缺失 fail-loud，绝不回落最高版本 tag；缺省（未设 track）保持旧最高版本 tag 行为逐字节等价，三 tag 初始化完成后缺省才翻 `stable`；蛇形 `update_track` 与 `config.json` track 键仍为退役残留仅警告；
  - ⚠️ **2026-09-02 Allen 定(PR #331):Python 版不再读 `config.json`,连警告也一并移除。** 上面那句「`config.json` track 键仍为退役残留仅警告」是 2026-08-20 当时的裁定原文,**刻意保留不改** —— 改它等于改「当时说了什么」。今天的现状是:`config.json` 是 TS 版遗留物,Python 代码一处都不读它;`settings.json` 的蛇形 `update_track` 仍然只警告不遵从,那一半未变。
- 自动更新(2026-08-24 裁决):daemon 内独立调度线程每日两次(03:17/15:17 本地时间)按轨道或最高版本 tag 跑现有 `hyprial daemon upgrade` 路径，升级子进程和重启出的 daemon 继承原 daemon 环境，绝不占用 accept/reconcile 执行流；安装新版本后先经 user/Squire 总线取得通知送达回执，再优雅重启，未确认则升级落盘但禁止切断总线；`hyprial daemon upgrade status` 同时展示进程内调度与遗留单元，`install|uninstall` 仅供分阶段移除旧 launchd/systemd 单元；显式 `--tag` 仍是回退逃生门；
- 单文件配置迁移脚本:TS 版 user 档案/desired-state JSON → Python 版格式。

## 6. 迁移分期与切换日

| 阶段 | 内容 | 出口判据 |
|---|---|---|
> **节奏裁决(2026-08-07 12:09 Allen)**:开发一次性完整完成,P0-P4 仅为内部里程碑(进度可见性),不设逐段审批门;worker 编成照批(codex 流水线 + verifier 闸门 + 契约测试红绿)。

| P0 | 契约测试库固化(对 TS 零改动) | TS 版全绿跑通三件套 |
| P1 | 骨架:proto+transport+inbox,双机 zenoh 收发 | wire golden 全绿 |
| P2 | daemon+providers+CLI | CLI 黑盒全绿;pi/codex worker 可派单 |
| P3 | 飞书 adapter+MCP server | E2E 场景全绿(含飞书往返/重启存活) |
| P4 | 侍从批次需求(spec-squire v3:C/B2/D/E 载体迁移) | squire 验收标准(v3 §11) |
| **切换日** | 全员同日:装 Python 版→迁配置→起 daemon→烟测→观察 24h | 回滚路径:TS 版保留安装,重启即回退 |

开发期 TS 版持续运行(冻结态最稳),唯一运维:每日凭据续期(协调者值守)。

## 7. 风险清单

1. **zenoh-python 能力边界**:实时传输使用官方 Python 绑定；持久消息状态由本地 SQLite 保存，不再维护 `zenohd` storage-manager 的第二条生命周期轴。
2. **inbox 协议是最大自研件**(等价一轮估算 25-40 人日的队列层):P1 必须故障注入测试(双端崩溃/重复投递/时钟漂移);
3. irctokens 单维护者风险:sans-io 纯解析器,必要时可 vendor(千行级);
4. 飞书 SDK Python 版长连接的行为差异(与 Node 版):P3 首项验证;
5. 重写期士气/进度风险:契约测试让进度可量化(绿灯百分比),周报给 Allen;
6. 过渡期 TS 版事故:仅止血,修复不回传(封存原则),必要时提前切换。

## 8. 终审记录与待裁项

**已裁(2026-08-07 11:56,Allen)**:
- ④ 仓库方案(已完成):Python 开发仓曾用 **harness-bridge-py**,E2E 全绿后已改名 **harness-bridge**;原仓已改名 **harness-bridge-ts** 封存归档;hq submodule 已切换到新仓名。
- room/channel 关系澄清(已向 Allen 说明):room=harness 内部群组会话空间(IRC 原语的 channel,改名避撞);channel=飞书 app 接入桥(用法不变);room 消息可经 channel 镜像到飞书;DM 非 room。

**待裁**:
1. 命名词表(channel/room/link,含上述澄清)确认;
2. P1-P4 worker 编成(建议沿用 codex 流水线 + verifier 闸门,契约测试作红绿);
3. mailbox 兜底节点是否进 P1(建议进,mac-studio 常驻)。
