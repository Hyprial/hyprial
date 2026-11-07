# 组织与会话模型：presence/link → cell → agent → thread → conversation → department（设计稿 r1）

- 日期：2026-11-03
- 状态：WIP，待评审；概念与分层（§0–§8）+ 目标模块结构（§9）+ 重构计划（§10）
- 作者：emergent-org-structure-refactor（R&D lead）
- 集成分支：`release/v0.5.5.4`
- 依据：Allen 2026-11-02～11-03 的六条模型与三次裁定（原话见 §1）；身份与连通分层设计
  （`notes/identity-connectivity-layering-design-2026-11-03.md`，HQ 仓）；cell 设计 v3
  （`docs/design/design-cell-2026-10-15.md`，intg/cell）；send-links 设计
  （`docs/design/design-send-links-2026-10-14.md`）
- 术语以 [GLOSSARY.md](../../GLOSSARY.md) 为准；本稿新增的词条已同步写入术语表

## 0. 结论

1. **层次**：网络层只有 presence 和 link（节点之间的连接）；OrgFS 层承载 cell（固定的连接结构）；在 cell 上构造 agent、thread、conversation、department。
2. **收拢**：今天散在五处的“谁可以和谁说话”，都收成 **thread** 一个概念，即 agent 加一个单一对象（另一个 agent 或 adapter）。这五处是 send-link、`route:` 地址、owner link、squire 投递、GUI 聊天会话。
3. **机制在代码，配置在数据**：代码只提供通用 cell 运行时与执行闸门。department 的定义、成员，以及 thread 的流控参数（k、W、cwnd）都是 OrgFS cell 文档里的数据，修改不需要发版。
4. **收件箱拆两层**：
   - **投递队列**留在 daemon 本地，负责送达、确认、重试、TTL、离线暂存，不依赖 OrgFS，不保留正文；
   - **交互记录**写入所属 conversation 的 cell（OrgFS），是共享、可检索的历史；
   - **系统运行信息**只走本地队列，不进 OrgFS。

## 1. 裁定原话（Allen）

- 2026-11-03：「1. 在 tailnet 网络层存在的是 presence 和 link，表示的是节点的链接 2. 在 orgfs 层存在的是 cell，表达的是一个固定的连接结构（routine path/graph）3. 基于 cell 机制，首先形成的是 agent，表达为是一系列 harness 组成的结构 4. agent 和一个单一对象（其他 agent，adapter）形成的是 thread 5. 一系列 thread 组合成的是 conversation，表达的是 threads 写入的一个空间（cell）6. agent 和 adapter 可以进一步组合为 department（也是一个 cell）」
- 2026-11-03：「department、流量控制等内容，看起来像是业务层数据层面的配置，未必要放入代码」；「daemon 收件箱本身是否考虑也应该建立在 OrgFS 上，或者，应该拆分收件箱，区分系统运行的信息和人类/agent 交互的信息」
- 2026-11-03：「确定，本地队列不需要保留消息正文。」
- 2026-11-02：「我不建议把 cell/link 和对话内容关联起来，组织结构理论上和对话内容无关」（因此 conversation 是一种 cell 用法，组织结构不决定对话归属）

## 2. 概念定义

| 概念 | 定义 | 所在层 | 权威数据 |
|---|---|---|---|
| presence | actor/节点的在线事实（zenoh liveliness） | 网络层 | 运行时，不持久 |
| link | 两个节点之间可用的连接（成员名册 + tailcat 通道） | 网络层 | sidecar 成员名册 |
| cell | 一个固定的连接结构：成员、权限面、路径/图；可嵌套 | OrgFS 层 | OrgFS cell 文档 |
| agent | 由一个或多个 harness 组成的 cell，有持久身份（`a.` id） | cell 实例 | agent registry + cell 文档 |
| thread | agent 与一个单一对象（agent、adapter 端点、owner）之间的通道；带流控参数 | cell 之上 | thread 记录（OrgFS） |
| conversation | 一组 thread 写入的空间；本身是一个 cell | cell 实例 | conversation cell（OrgFS） |
| department | agent 与 adapter 组成的 cell | cell 实例 | cell 文档（数据，不进代码） |

关系：presence 和 link 只回答“节点能不能连上”；thread 回答“这个 agent 能不能和那个对象说话、按什么流控”；conversation 回答“这些 thread 写到哪里”。三者互不替代。

## 3. 现状对照（origin/dev，路径省略 `src/hyprial/`）

| 新概念 | 已有实现 | 状态 |
|---|---|---|
| presence | `daemon/impl/transport/zenoh/presence.py`、`daemon/impl/transport/presence_actor.py` | 已有 |
| link（节点连接） | daemon 侧 `daemon/impl/forwarding/membership/`，sidecar 侧 `sidecar/hyprial-tailcat/internal/membership`；mailbox 离线兜底同层 | 已有 |
| cell | `daemon/impl/cells/observability`（只有挂载与观测）；cell 设计 v3 在 intg/cell，未合入 | 雏形 |
| cell 实例（路径/图） | PAC 工作流图（`daemon/impl/pac/graphs`、`pac/workflows`）、routines（`daemon/impl/application/actors/routines.py`）、org 本身 | 可转成实例 |
| agent | 单个 harness 实例：`daemon/impl/harnesses/*` + `daemon/impl/session_actor`，身份 `identity/impl/agents` | 已有（单成员）；多 harness 组成未有 |
| thread | 散在五处：<br>① send-link（`daemon/impl/links`，agent↔agent 授权与预算）<br>② `route:<adapter>:<name>`（`daemon/impl/network/route_delivery.py`）<br>③ owner link 特例<br>④ squire 投递（`daemon/impl/squire`）<br>⑤ GUI 聊天会话（`shell/impl/gui/adapter/chat.py`，由发送方与目标推导）<br>消息上的 `conversationId` 今天实际上是 thread id | 可收拢为一个概念 |
| conversation | 无；最接近的是飞书群（一个 route、多人）和 PAC 图会话（`pac-wf-…`） | 不存在 |
| department | 无（只在 cell 设计 v3） | 不存在 |

## 4. 不再需要的

- `route` 作为独立的寻址与配置概念：成为 thread 的 adapter 端，adapter 绑定（adapter、chat id）只是传输细节。
- `daemon/impl/links` 作为独立原语存进 OrgFS：授权与预算成为 thread 的属性。
- owner link 特例分支：owner 是一条普通 thread。
- GUI 的 `sessions.*` 推导层：直接读 thread 与 conversation。
- `trustMembersForUpperLayers` 开关（分层设计 r1 已定删除）。
- `route_registration` 这个名字：它是交互会话的投递路由，与 adapter route 撞名，需改名。

## 5. 机制与配置的边界

代码提供：
- 通用 cell 运行时：创建、成员、权限面、持久化到 OrgFS；
- thread 的建立与撤销；
- 流控闸门：超出预算即拒绝或排队；
- 投递队列。

数据提供（OrgFS cell 文档字段，改了不用发版）：
- department 的定义与成员；
- 每条 thread 的 k、W、cwnd，或按类型的默认值；
- routines 的编排；
- adapter 端点绑定。

边界规则：**闸门必须由 daemon 代码执行**。数据只描述策略，不能靠配置自觉遵守，否则一条写错的配置就能绕过限流。

## 6. 收件箱拆分

| 层 | 内容 | 位置 | 权威 | 保留 |
|---|---|---|---|---|
| 投递队列 | 所有消息的送达状态：ack、重试、TTL、离线暂存、custody | daemon 本地 | 送达状态 | 到 ack 加保留期；**不保留正文**，之后只留指向记录的引用 |
| 交互记录 | 人与 agent 的 message/request/reply 正文 | conversation cell（OrgFS） | 内容与历史 | 按 conversation 策略 |
| 系统运行信息 | workflow 节点通知、看门狗、TTL 到期、actor_lost、PAC 内部派单 | daemon 本地 | 本地 | 短留存，不进 OrgFS，不唤醒人 |

流程：
1. 发送：先进投递队列，按 thread 流控闸门放行，经网络层送达。全程不依赖 OrgFS。
2. 送达后：收发双方把正文追加到 conversation cell。写 OrgFS 失败只让历史延后出现，不影响送达；队列里只留引用。
3. GUI 的会话列表、历史与搜索读 conversation cell。

与分层原则一致：网络与投递不依赖 OrgFS；OrgFS 只承载组织结构与记录。

## 7. 待定与风险

- conversation 的保留与可见性策略：谁能读哪个 conversation、默认保留多久，由 cell 权限面决定，需要单独定义。
- 迁移：
  - 旧 `route:` 地址与旧 send-link 表保留一个大版本的别名；
  - 旧 conversationId 映射为 thread id；
  - 与稳定 id 工作（0.5.5.4 发 id、0.5.6 切路由键）同节奏。
- 交互记录写入 OrgFS 后，正文进入多机复制。凭据与敏感内容的脱敏规则需要在 conversation cell 写入前执行。
- 实现切片与版本分配，在本稿评审通过后另出一份计划，不在本稿范围内。

## 8. 验收（设计层面）

- 每个现有功能在 §3 / §4 中都有明确归属：已有、转成实例、收拢、或删除。
- GLOSSARY.md 与本稿词条一致，锚点路径真实存在。

## 9. 目标模块结构

仍然遵守五个深模块与分层（kernel → identity → daemon → biz → shell），只在域内重组。下表路径省略 `src/hyprial/`，“现状”列为 origin/dev 上的位置。

### 9.1 kernel
| 目标 | 内容 | 现状 | 动作 |
|---|---|---|---|
| `kernel/impl/primitives/uri.py` | 稳定 id 语法 `u.` `d.` `a.`，新增 thread `t.` 与 cell `c.`；名字只作别名 | 已有 `u.` `d.` `a.` | 扩展 |
| wire 词汇 | envelope 增加 `threadId`、`conversationId`（cell id）、`class=interaction\|system` | `conversationId` 字段语义模糊 | 增加字段；旧字段作别名保留一个大版本 |

### 9.2 identity
| 目标 | 内容 | 现状 | 动作 |
|---|---|---|---|
| `identity/impl/users`、`devices` | 用户与设备身份（网络层身份） | `users`、`device` | 不变 |
| `identity/impl/agents` | agent 身份（agent cell 的身份部分） | 已有 | 不变；registry 增加 cell id |
| `identity/impl/org_directory` | 只读镜像，供 GUI 和人查看 | 已有 | 降级为镜像（身份与连通分层 r1） |

### 9.3 daemon（平台层）
| 目标模块 | 职责 | 由谁而来 | 动作 |
|---|---|---|---|
| `daemon/impl/network/` | 网络层：presence、link（成员名册 + tailcat 通道）、mailbox、transport | `transport/zenoh/presence*`、`transport/presence_actor.py`、`forwarding/membership`、`network/tailcat`、`transport/` | 合并为一个域内子包，公共面只暴露“节点是否可达、在线事实” |
| `daemon/impl/delivery/` | 投递队列：inbox/outbox、custody、重试、TTL、离线暂存；按 `class` 区分交互与系统消息；ack 后加保留期即丢弃正文，只留引用 | `inbox/*`（含 `inbox/links/` 投递传输：local、zenoh、pull、wake；不含 `inbox/service/link_ledger.py`）、`application/messaging/delivery` | 改名并收窄职责，不再持有正文 |
| `daemon/impl/cells/` | 通用 cell 运行时：创建、成员、权限面、持久化到 orgfs；`observability` 保留为子模块 | `cells/observability` | 扩展 |
| `daemon/impl/threads/` | thread 注册与查询、**流控闸门**（k/W/cwnd 执行）、端点绑定（agent、adapter、owner） | `links/`、`inbox/service/link_ledger.py`（send-link 授权与预算账本）、`network/route_delivery.py`、owner link 分支、`squire` 的投递目标解析 | 新建；旧模块迁入后退役 |
| `daemon/impl/conversations/` | 交互记录写入 conversation cell、历史与检索读取 | 无 | 新建 |
| `daemon/impl/harnesses/`、`session_actor/` | agent 的运行时成员（harness 进程与运行会话） | 已有 | 不变；agent 由 cell 组织 |
| `daemon/impl/adapters/` | 外部平台；只负责“把 thread 的 adapter 端送达到平台” | `adapters/lark`；route 配置在本机 settings | 配置迁到 thread 端点数据 |
| ~~`daemon/impl/squire/`~~ | ——（Allen 2026-11-03 裁定：squire 是普通 agent） | 已有 | 阶段 B 退役：squire 由 agent 模板经通用 agent create 创建（角色在 cell 数据里）；与主人的飞书私聊是一条普通 adapter thread；`squire` 地址改为解析到该 thread 的别名；专用错误码并入通用码；profile 的 `squire_channel/squire_adapter` 改为 thread 绑定；删除包与 shutdown/PAC/dispatch 中的特判 |
| `daemon/impl/pac/` | 声明式派单；图定义存为 cell 文档 | 已有 | 图存储迁到 cell |
| `daemon/impl/orgfs/` | 文档同步存储 | 已有 | 不变；只被 cells/conversations 使用，network 和 delivery 不得依赖它 |

### 9.4 biz
| 目标 | 内容 | 现状 | 动作 |
|---|---|---|---|
| `biz/impl/routine` | routine 编排读 cell 数据执行 | `biz/impl/routine` + `daemon/impl/application/actors/routines.py`（默认编排写在代码里） | 默认编排移出代码，成为 cell 数据 |
| department 策略 | 不新建代码模块；department 是 cell 数据 | 无 | 只有数据 |

### 9.5 shell
| 目标 | 内容 | 现状 | 动作 |
|---|---|---|---|
| `shell/impl/gui/adapter/chat.py` | GUI 读 thread 与 conversation | 由发送方和目标推导 `sessions.*` | 删掉推导层，`sessions.*` 改名为 `threads.*` / `conversations.*` |
| CLI | `hyprial message` 增加 `--thread`/`--conversation`；`route:` 地址保留为别名 | 已有 | 兼容扩展 |

### 9.6 退役与改名
- `daemon/impl/links/`、`daemon/impl/inbox/service/link_ledger.py` → 迁入 `threads/` 后删除。`daemon/impl/inbox/links/` 是投递传输层（不是 send-link），随 `delivery/` 改名迁移，不并入 `threads/`。（2026-11-03 云电脑代码梳理更正）
- `daemon/impl/network/route_delivery.py` → 迁入 `threads/`（端点绑定）和 `adapters/`（送达）后删除。
- `daemon/impl/route_registration/` → 改名为 `session_delivery/`，避免与 route 撞名。
- `forwarding/membership` 中从 OrgFS 目录播种名册的路径 → 删除（身份与连通分层 r1 已定）。
- `trustMembersForUpperLayers` → 删除。

模块边界由 `scripts/ci/check_module_boundaries.py` 强制：network 与 delivery 不得导入 orgfs、cells、conversations；threads 可以读 cells（流控参数是数据），不得读 conversations。

## 10. 重构计划（Allen 2026-11-03：「#2313 的设计实现我希望在 0.5.5.4 就完成」；「先做通用 cell 运行时，再做 attach」）

全部实现进 0.5.5.4。纪律不变：
- 每一片可以单独合入，也可以单独回退；
- 行为变化要有先红后绿的测试；
- 对外字段只增不改；
- 旧接口（`route:` 地址、send-link、`sessions.*`、旧 conversationId）在 0.5.5.4 内以别名保留，打弃用日志，删除放到之后的版本。

### 10.0 两阶段（Allen 2026-11-03：「先拆为不影响 GUI（即 daemon_request 内部）的工作，在 daemon_request 中添加保留的转译层，全部交给云电脑一次性完成，进入 0.5.5.4，然后再做 daemon_request 以外的工作」）

**阶段 A（0.5.5.4，云电脑一次完成）：daemon_request 以内**
- 范围：S2（内部 id 与 envelope 字段）、S3（通用 cell 运行时）、S4（threads 与发送闸门）、S5（投递队列拆分）、S6 的写入侧（交互记录写入 conversation cell）、S7（department、routine、PAC 数据化）。
- **转译层**：现有全部方法——`daemon/impl/request_catalog/` 的 12 个 MethodSpec 方法，以及仍在 `daemon/impl/ipc/router.py` 旧分支里的其余方法，转译层两边都要覆盖（2026-11-03 云电脑代码梳理更正）——（message.*、sessions.*、link.*、agent.*、org.* 等）的名字、参数、返回结构、错误码一律不变，由转译层映射到新的内部模块。比如 link.add 映射为建立一条 thread，sessions.history 映射为读 conversation。`contract/daemon-request/openapi.json` 与 GUI 生成的 client 必须逐字节不变（`--check` PASS），GUI 零改动。
- 不在阶段 A：S1（网络层）已由 HQ 的 PAC 在做，不重复；CLI 新命令（含 attach）、GUI 改读 threads 和 conversations、对外改名、契约字段新增，都放到阶段 B。

**阶段 B（阶段 A 合入后）：daemon_request 以外**
- S6 的读取侧对外暴露（新增 threads.* / conversations.* 方法，旧方法保留为别名）、GUI 改读、CLI 新参数。
- S8 attach 命令。
- S9 收尾：改名、删除过期别名的计划、门禁。

阶段 A 的验收：
- 契约与生成物 `--check` 全部 PASS，GUI 测试不改一行也全绿；
- 现有 E2E 全部不回归；
- 发送闸门只走 threads（旧 link 数据迁移后逐条一致）；
- 投递队列在 ack 加保留期之后不留正文，历史能从 conversation cell 读回；
- 断开 OrgFS 时消息照常收发；
- 改 department 或 thread 流控参数只改数据。

### 10.1 切片与依赖

| 片 | 内容 | 依赖 | 建议执行方 |
|---|---|---|---|
| S1 网络层解环 | 迁移不覆盖 cPBS；删除目录播种和 trust 开关；名册周期重播；sidecar 走 HTTPS proxy；兑现失败时降级启动；OrgFS 副本 attach 窗口修复 | — | HQ PAC（后三项已在进行） |
| S2 稳定 id 与 envelope 字段 | `u.` `d.` `a.` `t.` `c.` 语法与别名解析；envelope 增加 `threadId`、`conversationId`（cell id）、`class` | — | HQ PAC |
| S3 通用 cell 运行时 | `daemon/impl/cells/`：创建、成员、权限面、持久化到 orgfs；observability 并入为子模块；agent 作为 cell（单 harness 成员）登记 | S2 | 云电脑（cloud-claude），HQ 评审 |
| S4 threads | `daemon/impl/threads/`：注册与查询、流控闸门（参数从 cell 数据读）、端点绑定（agent、adapter、owner）；发送路径改读 threads；links 和 route 变成别名 | S2、S3 | HQ PAC |
| S5 投递队列拆分 | `daemon/impl/delivery/`：按 `class` 分流；系统信息不唤醒人；ack 后过保留期丢弃正文，只留引用 | S2 | HQ PAC |
| S6 conversations | `daemon/impl/conversations/`：交互记录写入 conversation cell，提供历史与检索读取；GUI 和 CLI 改读这里（`sessions.*` 改为 `threads.*` / `conversations.*`，旧名保留别名） | S3、S5 | HQ PAC + GUI 线 |
| S7 数据化 | department、routine 编排、PAC 图定义改为 cell 数据 | S3 | HQ PAC |
| S8 attach | `hyprial agent attach/detach`：创建 agent cell，成员是一个 `custody=external` 的 harness | S3（受限网络场景还依赖 S1 的 proxy 修复） | 云电脑 |
| S10 squire 普通化（阶段 B） | Allen 2026-11-03 裁定 squire 是普通 agent：模板化创建（通用 agent create，角色在 cell 数据）；与主人的私聊为普通 adapter thread；`squire` 地址→thread 别名；`TARGET_SQUIRE_*` 并入通用错误码；profile `squire_channel/squire_adapter`→thread 绑定；退役 `daemon/impl/squire/` 及 shutdown/PAC/dispatch/onboarding 特判；provider key 授权对 squire 无特例 | S3、S4、S6 | 阶段 B（daemon_request 外部，含 GUI 引导步骤） |
| S9 收尾 | GLOSSARY 锚点 CI 门；`route_registration` 改名；module boundaries 门禁规则（network、delivery 不得导入 orgfs、cells、conversations） | 随各片 | HQ |

关键路径：S2 → S3 → S4/S6 → S8。S1、S5 与之并行。

### 10.2 验收（0.5.5.4 整体）
- 断开 OrgFS 时消息照常收发，历史稍后补齐；删除所有 OrgFS 目录设备行之后，连通、在线、收发全部正常。
- 发送闸门只读 threads；旧 `route:` 地址经别名仍可用并打弃用日志。
- 投递队列在 ack 加保留期之后不保留正文；GUI 和 CLI 的历史来自 conversation cell。
- 新增 department 或调整一条 thread 的流控参数，只改数据、不发版。
- 云电脑在只允许 proxy 出站的环境下，用 `hyprial agent attach` 接入，并完成 message batch/reply 闭环（Claude Code cloud session 实测）。
- `check_module_boundaries`、结构门、E2E 全绿；不回归现有 E2E 场景。

### 10.3 风险
- 体量大：九片并行，合入时冲突集中在 kernel wire、inbox 和 GUI 生成物。生成物按规矩不做文本合并，每次合入后重新生成并 `--check`。
- 双写期（S5 和 S6 之间）要监控交互记录与队列的一致性；不一致时以队列为准，并告警。
- 正文进入 OrgFS 后会被多机复制：写入前要脱敏，可见性由 cell 权限面决定（§7）。
- 时间：关键路径上 S3 最重。S3 合入之前，S4、S6、S8 只能先做到可测的接口层。
