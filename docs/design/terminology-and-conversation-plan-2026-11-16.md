# 名词整理与对话模型收口：规划（2026-11-16）

- 日期：2026-11-16
- 状态：**历史讨论记录（已按本轮裁定实施收口，脱敏归档）**。合并后的裁定与实施计划见 [conversation-implementation-plan-2026-11-17.md](conversation-implementation-plan-2026-11-17.md)，冲突时以后者为准。owner 2026-11-16 的裁定汇总在 §12，与前文冲突处以 §12 为准。绕过 conversation 的路径清单在 §13。§1 是 owner 原话；其余标【已核】的是代码读数，标【提议】的为当时待裁项。本稿按脱敏标准仅保留统计数字与分析逻辑，不涉内部人员、机器或会话细节
- 记录：设计讨论记录（非注册 agent，已脱敏）
- 读数基线：`origin/dev` @ `0849e7932`（§13 为 `1dddd5325`）。全部来自读代码与检索，**没有在真实 daemon 上运行验证**
- 相关：PR #2882（GUI 聊天载体讨论）；
  `docs/design/cell-thread-conversation-model-2026-11-03.md`（现行模型）；
  分支 `docs/cell-mode-post-actor-rustify-2026-11-12` 上的 cell-mode handoff（未合入）
- 术语以 [GLOSSARY.md](../../GLOSSARY.md) 为准；本稿提议的改动在裁定前**不**写入术语表

## 0. 结论

1. **对话的载体已经存在**，是 conversation 记录（`cells/conversation/`）。PR #2882 提议新增可写 chat 文档，会造出第二份记录，不应按原方案推进。
2. **agent 无法主动发消息进 GUI 的原因是投递分支，不是缺地址**：`message.send` 对 `user:` 目标直接交给外部 adapter，本地收件箱不落行（§3）。
3. **cell、PAC、conversation 是三个互不相关的概念**（已裁，§12）：cell 是实体如何组织，PAC 是任务如何运行，conversation 是主体之间如何交互。现在三者都被做成 cell 的 kind，需要拆开（§5）。
4. **OrgFS 侧的 `graph` cell 是 PAC 图定义的第二份副本**，默认关闭、无读取方，建议删除（§6 步骤 B1）。
5. **thread 与 conversation 只保留一层**（已裁方向，§12；本稿提议保留 conversation 这个名字）。群聊按 conversation 的成员表组织（§8）。
6. **所有通信都走 conversation，不允许绕过**（已裁，§12）。现有的绕过路径共三类，清单在 §13；其中哪些算「通信」还有四项待裁（Q11–Q14）。

## 1. owner 原话（2026-11-16）

- 「当前，agent无法在GUI中主动向用户发送消息，因为GUI的对话没有身份，没有地址可以发送进去。但仔细思考，似乎更合理的方式是，为每个对话维护一个chat的文档，agent和人的消息都写入这个文档中，GUI只是将这个文档呈现出来。在此基础上，GUI似乎也没有必要再绑定user-proxy，可以直接以user的身份写入chat文档。」
- 「在我本来的设计中，cell是一种组织机制，例如，把多个harness和timer等机制组合为一个cell，这个cell对外显示为一个agent。但按照这个逻辑，agent和department称为cell是合理的，但为什么routine、conversation和graph也能称为cell？」
- 「我建议改回routine和workflow，kind应该是pac，conversation是和pac、cell同一个层积的概念」
- 「graph 在PAC的实现中有吗？如果没有的话，我认为不应该在orgfs侧新增一套」
- 「thread id和conversation id的必要性我们完全理解，另外，conversation id 是不是可以支持群聊模式？」
- 「在我本来的计划中，所有通信都应该走 conversation 机制，不应该出现绕过的情况」

与此一致的更早表述（2026-11-12，转引自 cell-mode handoff §1，未一手复核）：「agent本身是一个cell进程，这个进程内部没有harness也可以称为一个agent，cell内的harness可以自动增加或者删减」；「每 cell 一个 keyed actor 就是我的预期」。

## 2. 名词现状【已核】

### 2.1 执行组：全部属于 PAC，权威在本地库

| 名词 | 含义 | 权威数据 | 对外入口 |
|---|---|---|---|
| PAC | 声明式派单与跟踪层，不替 worker 执行 | `pac-graph.sqlite3` | 无独立命令 |
| workflow | 人写的任务图声明（YAML，format 2） | 编译后进 `workflow_graphs` 表 | `hyprial workflow`、`workflow.*` |
| graph | workflow 编译后的运行对象：节点、边、标志事件 | `graphs`/`nodes`/`edges`/`flag_events` 表；代码在 `daemon/impl/pac/graphs/` | 无，内部名词 |
| routine | 周期触发器，到点产生一次 PAC run，本身不含任务图 | `routines`、`routine_cycles` 等表（`biz/impl/routine/`） | `hyprial routine`、`routine.*` |
| worker | 被 PAC 派出执行任务的 agent 实例 | `actor_assign` 等 | 无 |

workflow 与 graph 是同一件东西的编写形态和运行形态。routine 不是 workflow：workflow 描述做什么，routine 描述什么时候做。

### 2.2 组织组：权威在 OrgFS

| 名词 | 含义 | 出处 |
|---|---|---|
| cell（代码定义） | 一份数据文档：kind、成员、权限面、参数表 | `daemon/impl/cells/model.py` |
| cell kind | `agent`、`conversation`、`department`、`routine`、`graph` | `model.py:44` |
| 存放 | `cells/<kind>/<c.id>.json`；`agent`/`department`/`routine` 在组织目录 space 并全量索引，`conversation`/`graph` 在 owner-home space 并按需读取 | `cells/runtime/layout.py`、`model.py:51-52,344` |

### 2.3 交互组

| 名词 | 含义 | 权威数据 |
|---|---|---|
| thread | agent 与一个单一对象（agent、user、adapter 端点）之间的通道；`t.` id 由两端地址派生，不存储 | 由 send-link 文档与发送记录投影（`daemon/impl/threads/ids.py`） |
| conversation | 记录空间；键为（owner，thread，旧 conversationId） | owner-home 的 `cells/conversation/<c.id>.json` |
| 交互记录 | 消息正文，送达后追加 | `cells/conversation/<c.id>/rec-<节点>/<序号>.jsonl` |
| 投递队列 | 送达、确认、重试；不留正文 | 本地收件箱库 |
| `conversationId` | `message.send` 上的自由字符串；不传时为 `uuid4`；GUI 用 `uuid5(sender, target)` 推导 | `inbox_surface/send.py:186`、`shell/impl/gui/adapter/chat.py:525` |

### 2.4 重叠的名词

| 名词 | 并存的含义 |
|---|---|
| cell | ① owner 原本的组合机制（harness 等部件组成一个对外单元）；② `cells/model.py` 的通用数据文档；③ `cells/observability/` 的逻辑挂载；④ Rust actor 引擎里的 `Cell` 类型（据 cell-mode handoff §3.3，未一手复核） |
| graph | ① PAC 的运行对象；② OrgFS 的 `graph` cell 镜像 |
| routine | ① `biz/impl/routine/` 里真正的 routine；② `routine` cell，只承载 `agent-default` 一份配置（是否启用、间隔），代码里只见读取 |
| conversation | ① 消息上的 `conversationId`（11-03 设计稿称其"实际上是 thread id"）；② conversation 记录；③ 飞书 adapter 里的一个群；④ hook 里带前缀的一次交换 |
| link | ① 术语表：网络层节点连接；② `daemon/impl/links/` 与 `hyprial link`：agent 之间的发送授权（send-link） |
| session | ① 术语表：agent 进程的运行会话；② GUI 的 `sessions.*`：聊天会话 |
| channel | ① 术语表：群组会话空间；② `channel:` 是 `adapter:` 地址的旧写法 |

## 3. agent 主动消息为何进不了 GUI【已核】

| | agent 回复 GUI 用户 | agent 主动发给 `user:<owner>` |
|---|---|---|
| 路径 | `message.reply`：构造 `InboxMessage` 并提交收件箱 | `message.send` 的 `is_user_target` 分支：交给 agent-bot 或 `_user_delivery.deliver(...)` 后 `continue`（`inbox_surface/send.py:249-386`） |
| 落点 | 本地收件箱，沿用原 conversationId | 外部 adapter（飞书 DM） |
| conversation 记录 | 会记 | 不记（recorder 只读收件箱与发送方副本） |
| GUI | `message.history` 能读到 | 读不到 |

发送方副本也没有：`sender_copies` 只有一个写入点（`inbox/service/core.py:711`，在收件箱提交路径内），`user:` 分支不经过它。

术语表 squire 词条写明「`user:<owner>` 是 owner 适配器 thread 的别名」，即 `user:<owner>` 在定义上被当作 adapter 端点，而不是本机上的人。这是上表右列行为的来源。

## 4. PR #2882 原方案的问题【已核】

- 「加一个 `kind` 取值，跟随面 = 0」不成立：OrgFS 节点 `NodeKind = Literal["dir","doc","blob"]`，`document/operations/` 下二十余处 `kind != "doc"` 判断，`open_doc` 自身要求 `kind == "doc"`（`content.py:683`）。
- 「载体已有」只在库层成立：`open_doc` 在 `src/` 无调用方，`orgfs.*` 请求面无结构化写方法，GUI 侧无 orgfs 透传。
- 「既有 ACL 直接覆盖」不成立：`_ensure_writable`（`projection.py:409`）只判断 space 成员是否 `rw`，没有按文档的权限。
- 现行模型已规定「先投递、后记录」（11-03 设计稿 §6）：消息走投递队列并唤醒对方，记录是送达后的追加。把文档作为主写入路径会丢掉唤醒。

## 5. 目标分层【提议 · 待 owner】

| 概念 | 含义 | 包含 | 存放 |
|---|---|---|---|
| cell | 组合体，对外表现为一个单元；按 11-12 的方向，将来每个 cell 对应一个 keyed actor | agent、department | OrgFS 组织目录 space |
| PAC | 执行结构 | workflow（编写形态）、graph（运行形态）、routine（触发） | PAC 与 routine 的本地库，不进 OrgFS |
| conversation | 交互记录的空间 | 它自己 | OrgFS owner-home space |

理由：

- 与 owner 原本的 cell 定义一致。
- 与 11-12 的方向一致：cell 若成为执行单元，记录类对象不应一并变成 actor。cell-mode handoff §2 的一份运行快照显示 1,552 个 cell 中 1,551 个是 conversation，`department`/`routine`/`graph` 为 0（转引，未一手复核）。
- conversation 不再受「须有 agent 作为组合主体」的约束，人↔人与群聊在这一层没有障碍。

保留现有的通用存储运行时（`CellRuntime`、`cells.sqlite3`、`cells/` 路径）作为三类共用的底座，只是不再把它等同于 cell。

## 6. 施工步骤【提议】

### A 线：对话（承接 #2882）

| 步 | 内容 | 依赖 |
|---|---|---|
| A1 | 取消 `message.send` 里 `user:` 目标的专用分支。发给 `user:<owner>` 的消息与其他消息一样：归入一个 conversation、进收件箱、被记录。飞书等外部 adapter 是该成员的投递端点，在这之后执行，不再是替代路径 | 端点策略待裁（Q3） |
| A1b | 每条消息都必须属于一个确定的 conversation。现在主动 `message.send` 不带 conversationId 时取 `uuid4`，而 GUI 用的是 `uuid5(sender, target)`；只做 A1，每条主动消息会各自成为一个新对话。按「不允许绕过」的裁定，`uuid4` 默认值不能保留。提议：不带 conversationId 的 1:1 发送归入这两端的默认 conversation，id 由两端地址派生 | **待裁**（Q7）；与 §8 的定键方式一并决定 |
| A2 | GUI 改读 `conversations.*` 并订阅新记录 | cell model phase B 第二批已排 |
| A3 | 人↔人：允许两端都是 user 的对话（已裁，§12），仍每个 owner 各记一份，不建跨 owner 共享的记录 | 改 11-03 第 4 条 |
| A4 | `route:` 目标的发送改为先进 conversation 再由 adapter 投递（§13 第 2 行） | 与 A1 同一改法 |
| A5 | 清理 recorder 的跳过条件（§13 第三类）：没有对话归属的消息要么获得一个 conversation，要么列为明确的豁免 | Q12、Q14 |

### B 线：名词整理

| 步 | 内容 | 风险 |
|---|---|---|
| B1 | 删除 `graph` cell kind 与 `composition/pac_mirror/` | 低。模块自述为默认关闭、phase A 无读取方；`src/` 检索未见读取方。删除前核对开关配置与线上是否有数据 |
| B2 | `routine` cell 从 kind 中移除；`agent-default` 配置改放 agent 或 department 的参数 | 中。它是 owner 写入的数据，需兼容读取旧 id；先确认是否有线上数据 |
| B3 | cell kind 收回到 `agent`、`department` | 依赖 B1、B2 与 B4 |
| B4 | conversation 在术语上与 cell、PAC 并列；存储路径与 `c.` id **先不动** | 改名代价高：投递队列保存指向记录路径的引用，`c.` id 已经由 `conversations.list` 暴露给 GUI。单独评估 |
| B5 | 修订 GLOSSARY：cell、conversation、routine 三行改定义；link、session、channel 各补一句区分 | 低。随裁定同步 |

A 线与 B 线互不依赖。A1 与 B1 可以最先做。

## 7. thread id 与 conversation id

（本节开头的表是早先的分析。owner 已裁定只保留一层，见 §12；§7.1 是现行提议。）

两者的原有分工：

| | thread | conversation |
|---|---|---|
| 回答的问题 | 谁能和谁说话、按什么流控 | 这些消息记到哪里 |
| 粒度 | 一对端点 | 一个话题空间，可含多条 thread |
| id | 由两端地址派生，无需协调 | 应独立于端点 |

现状的问题是 conversation 没有独立于 thread：记录的键里同时含 thread 与旧 conversationId，而 GUI 的 conversationId 又是由（sender，target）推导的。结果是每个 conversation 实际只含一条 thread，11-03 第 5 条「一系列 thread 组合成 conversation」尚未实现。

### 7.1 thread 是否多余【提议 · 待 owner】

owner 2026-11-16：「为什么我感觉thread 模型有点多余？只用conversation 不够吗？」

thread 今天实际承担三件事【已核，`daemon/impl/threads/service.py` 的决策注记】：

1. 由一对地址派生一个稳定 id，无需协调。
2. 发送准入：`ThreadAuthority` 是 send-link 引擎（`LinkService`）的外壳，授权与预算仍由后者判定。
3. 把 `user:<owner>` 解析到 owner 的 adapter thread。

采用 §8 的群聊形态后，thread 就是「成员在某个 conversation 里的席位」，与 conversation 的成员表重复。可以收成：

| 现在由 thread 承担 | 收拢后 |
|---|---|
| 一对端点的稳定 id | 1:1 conversation 的 id 直接由两端地址派生 |
| 成员↔群的通道 | conversation 成员表中每个成员的一行，只记「谁在里面」 |
| 流控参数（k、W、cwnd） | 留在 send-link，不放进 conversation（见 §12.5） |
| 「谁可以联系谁」，在任何对话之前就存在，且跨对话计量 | 保留为一条成对的许可关系，沿用 send-link 这个名字，不再另称 thread |

即对外只保留 conversation；成对的许可是它的准入条件，不是另一层对话模型。

thread 在后端的实际使用【已核】：

- **不参与寻址与投递。** `message.send` 的参数里没有 thread，agent 用 `to` 加 `conversationId` 通信；投递、转发、传输各模块都不读 thread id。
- **准入名义上经过它，默认不生效。** 发送与回复调用 `ThreadAuthority.admit`，它把判定原样交给 send-link 引擎；而 `links.bypass` 默认开启（`composition/links.py`："default on"），开启时所有 link 状态的拦截都放行。
- **真正的消费者是记录与读取。** thread id 写入信封，recorder 用它组成记录的键，`conversations.*`、`threads.*` 与 `hyprial message threads` 用它做读投影。

即 thread 目前是一个派生出来的标签，不是通信机制。

代价与冲突：

- thread 已实现并对外暴露：`threads.get/list` 已是 HTTP 方法，记录的键与消息信封里都有 thread id。
- 现行方向相反：`threads/service.py` 把「调用方自选 conversationId」记为待弃用的旧面，即打算让 conversation 由 thread 决定。群聊需要独立于端点的 conversation id，两者要先定一个。
- 11-03 设计稿用 thread 收拢五处分散的概念（send-link、`route:`、owner link、squire 投递、GUI 会话）。改为用 conversation 的成员关系收拢，结论相同，名字不同。

## 8. 群聊【提议 · 待 owner】

### 8.1 组织方式

群聊是一个独立的 conversation，持有成员表；每个成员与这个 conversation 之间有一条 thread。这样 thread 仍是「一个参与者加一个单一对象」，对象是 conversation 本身；conversation 就是这些 thread 写入的空间，对应 11-03 第 5 条。先例是飞书群：agent 与群之间是一条 thread，而不是与群里每个人各一条。

没有采用「成员两两之间各一条 thread」的原因：N 个成员需要 N(N-1)/2 条；一条群消息会同时消耗发送者在 N-1 条 thread 上的流控额度；人与人之间还得先有 user↔user 的 thread。

1:1 对话不变：两端之间的那条 thread 自带一个默认 conversation（见 A1b）。

### 8.2 例子

成员 A、B、C，conversation 记为 G，thread 为 tA=(A,G)、tB=(B,G)、tC=(C,G)。

```
A: msg-1
B: msg-2
C: >A:msg-1 @A msg-3
B: @A msg-4
```

| 消息 | 发送者的席位 | 记录中的字段 | 投递并唤醒 |
|---|---|---|---|
| msg-1 | A | from=A | B、C |
| msg-2 | B | from=B | A、C |
| msg-3 | C | from=C，replyTo=msg-1，mentions=[A] | A、B |
| msg-4 | B | from=B，mentions=[A] | A、C |

- 发送者只向 G 发一次。daemon 按成员表扇出，每个接收者经自己的那条 thread 收到。
- 记录里 G 有四条，按时间排列。引用是 `replyTo`，@ 是 `mentions`，两者都是记录的字段，不改变消息归属。
- 流控与准入不由 conversation 负责，仍由 send-link 判定（§12.5）。群聊里按什么对象计额度待定。

### 8.3 需要新增或改动的地方

1. **conversation 端点。** thread 的对端目前只有 agent、user、adapter 三种（`threads/ids.py`），需要增加 conversation。成员是人时，这条 thread 没有 agent 一端，与 A3 是同一个定义问题。
2. **按 conversation 定键。** 记录现在按（owner，thread，conversationId）分格，G 的四条消息会散在三格里。改为按 conversation 定键，thread 作为记录内的属性。
3. **成员表与扇出。** 现在一次 `message.send` 可带多个目标并共用一个 conversationId，但回复只发回原发送者（`message.reply` 取 `original.sender`）。需要由 conversation 的成员表决定投递对象。
4. **一条消息一个 id。** 现在每个目标各生成一个 message id（`send.py` 按目标派生）。群消息在各接收者处需要同一个 id，否则记录无法去重，引用也指不到同一条。
5. **唤醒规则【已裁，§12】。** 所有 agent 成员都被唤醒；harness 是否处理由该 agent cell 内的 handler 决定。`mentions` 与 `replyTo` 随消息交给 handler 作为判断依据。这依赖 #2712 的 cell 内前置成员（判断 harness）；在它实现之前，唤醒全部成员意味着每个 agent 都会进入一个 turn。
6. **成员表的权威与顺序。** 记录每个 owner 各一份，跨 owner 的群没有唯一的成员表，也没有全局顺序。需要指定权威，例如创建者那一份；并发消息在不同 owner 处的先后可能不同，是否需要统一顺序待定。

## 9. 待裁

| # | 事项 |
|---|---|
| Q1 | §5 的三组分层是否成立；PAC 是与 cell 并列的一类，还是 cell 的一种 kind |
| Q2 | B1：是否删除 `graph` cell 与 `pac_mirror` |
| Q3 | A1：消息进入 conversation 之后，向成员的哪些端点投递。例如 GUI 在线时是否仍同时发飞书。这是每个成员的端点策略，不再是「走哪条分支」的问题 |
| Q4 | A3：是否允许 user↔user 的 thread |
| Q5 | §8：是否做群聊；成员表的权威放在哪里 |
| Q7 | A1b：不带 conversationId 的消息归入哪个对话。按 §12 的裁定，「不归入任何对话」已不是选项；提议归入两端的默认 conversation |
| Q8 | §8：群聊的 thread 形态（成员↔conversation，或两两成对）；未被 @ 的 agent 成员是否唤醒 |
| Q9 | §11：routine 是 cell 的一种，还是 agent cell 内的一个成员；B 线与 #2712 的 S1 谁先 |
| Q10 | §7.1：是否取消 thread 这一层，只保留 conversation 与成对的发送许可 |
| Q11 | §13：PAC 派单、worker 对派单的回复、daemon 自己发出的通知与告警，是否属于「所有通信」。它们进收件箱但按 SYSTEM 类不记录，这是此前的有意裁定（`inbox/envelope/rules.py` 引 Q4） |
| Q12 | §13：hook 交换（harness 与 daemon 之间经收件箱的一问一答）和 #2712 规定的 cell 内交付，是否属于「所有通信」 |
| Q13 | §13：GUI 的 `progress.list` 进度流把 agent 的中间输出送到用户，不经过消息。是否属于「所有通信」 |
| Q14 | §13：记录被脱敏钩子拒绝、超过大小上限时，消息已送达但不入记录。保留这两种例外，还是改为拒绝发送 |
| Q6 | #2882 未决项 1：11-16 关于「GUI 给 user-proxy 身份」的转述是否取代 11-08。本会话中 owner 的表述是 GUI 以 `user:<owner>` 参与、不绑 user-proxy，请书面确认 |

user-proxy 的去留本稿不裁。它不再是 GUI 的前提；它的两条路径是否绕过 conversation 见 §13.1 第 4 行与 §13.4。

另有一个相邻但不同的问题：`links.bypass` 默认开启，绕过的是发送准入（send-link），不是 conversation。本稿不把它并入「不允许绕过」的裁定，是否关闭需另行决定。

## 10. 未验证的部分

- 全部结论来自读 `origin/dev` 的代码，没有运行验证。
- 「无读取方」来自对 `src/` 的检索；「默认关闭」来自模块自述，未核对开关配置。
- §2.4 的 Rust `Cell`、§5 的运行快照数字转引自 cell-mode handoff，未一手复核。
- agent 发给 `user:<owner>` 的消息在 A1 之后能否被分到 thread 并记录，是按 `threads/ids.py` 的规则推断的，未实测。
- 目标 `user:` 在另一个节点上时的路径（`adapters/user_delivery/receiver.py` 自述经 Zenoh 路由）没有读，A1 对它的改法未定。

## 11. 与 #2712（cell-mode 执行模型）的关系

#2712 是 `docs/cell-mode-post-actor-rustify-2026-11-12` 分支的 Draft PR。它记录的 owner 目标（T1–T4）是：agent 本身是一个 cell，可以没有 harness；cell 内 harness 可动态增删；cell 之间并行；每个 cell 一个 keyed actor。它的现状结论是今天的 cell 只是数据文档加一个全局线程，执行在 cell 之外，agent cell 固定单成员（该 PR 的 G1–G12）。2026-11-14 的增补在 agent cell 内加入判断与缓冲两个前置成员，并规定 cell 内交付不是消息。

这与 §1 中 owner 对 cell 的定义一致：cell 是把 harness 等部件组合起来、对外表现为一个单元的执行体。今天的实现没有做到这一点。

本稿与 #2712 的交点【提议】：

| #2712 的条目 | 若采纳 §5 的三组分层 |
|---|---|
| §4.4 给五种 kind 各分配一个执行角色 | 只剩 agent、department 两种需要执行角色 |
| D1：1,551 个 conversation cell 是否各有一个 actor | 不再是问题。conversation 不是 cell，记录的保序由 recorder 自己负责 |
| D8：conversation cell 的保留与清理 | 移出 cell-mode，归入 conversation 记录的策略 |
| G6、S3：conversation 写入 actor 化 | 移出 cell-mode |
| G7、S4：department/routine/graph 的 kind handler | 只做 department。graph 与 routine 都留在 PAC |
| §4.8：Rust `Cell` 与 OrgFS `Cell` 同名 | 仍需处理；cell 的含义收窄后更容易定名 |

routine 的归属【已裁，§12】：routine 属于 PAC，不是 cell，也不作为 cell 的成员。本节早先「routine 更像 agent cell 内的一个成员」的提议撤回。

顺序建议：B1–B3 在 #2712 的 S1 之前或同时完成，避免为将被移除的 kind 编写 actor 处理逻辑。

未核对的一点：记录发生在消息送达之后，#2712 增补中的判断发生在进入主 harness 之前。据此推断，被暂缓或归档的入站消息仍会出现在 conversation 记录里；缓冲成员合并后交给主 harness 的内容不是消息，不会被记录。这是推断，未验证。

## 12. owner 裁定（2026-11-16）

原话：

- 「cell / pac/ conversation 为什么是三层？感觉这实际上三个完全不同也不相关的概念，cell表达的是实体怎么组织，PAC表达的是任务如何运行，conversation表达的是主体之间如何交互。routine我们已经强调了是PAC，不是cell。」
- 「我建议取消thread这一层，或者，也可以取消conversation这一层，只是名词不同。」
- 「在群聊中我认为简单的操作方式是唤醒所有agent（cell模式下），但harness是否处理应该由cell中的handler来决定。」
- 「两端都是 user 的 thread 是我们需要的」

| 待裁项 | 结果 |
|---|---|
| Q1 | 已裁。cell、PAC、conversation 是三个互不相关的概念，不是层次，也不是同一机制的三种 kind：cell 是实体如何组织，PAC 是任务如何运行，conversation 是主体之间如何交互。§5 中「分层」「三组」的说法按此理解 |
| Q4 | 已裁。需要两端都是 user 的对话 |
| Q8 | 后半已裁：群聊唤醒所有 agent 成员，是否处理由 cell 内的 handler 决定。前半随 Q10 |
| Q9 | 前半已裁：routine 属于 PAC。后半（与 #2712 的先后）未裁 |
| Q10 | 已裁方向：thread 与 conversation 只保留一层。保留哪个名字未定 |
| 新增 | 已裁。所有通信都走 conversation 机制，不允许绕过。原话：「在我本来的计划中，所有通信都应该走 conversation 机制，不应该出现绕过的情况」 |

对前文的影响：

- B2：`routine` cell kind 移除；`agent-default` 配置的去处归 PAC 一侧决定，不放进 cell。
- A3：改述为「允许两端都是 user 的对话」，不再依赖 thread 的定义。
- §8：成员与群之间不再称 thread，是 conversation 成员表中的一行。
- A1、A1b、A4、A5：按「不允许绕过」改写或新增。本稿对这条裁定的理解是**入口唯一**：每次发送都归入一个 conversation、进收件箱、被记录，外部 adapter 只是成员的投递端点。11-03 规定的「先投递、后记录」顺序不变。如果 owner 的意思是把记录改为主写入路径，需要另行说明。

关于保留哪个名字【提议】：保留 conversation。`conversationId` 已经在消息、收件箱表、`message.send` 接口、GUI 与各 adapter 中使用；thread id 是派生值，不在 agent 的通信接口上；「thread」还与 Codex thread、飞书话题、操作系统线程重名。成对的发送许可继续叫 send-link。

### 12.1 通信的定义与 user 的地位（owner，2026-11-17）

原话：

- 「通信应该被定义为：任何 N个有确定地址的 actor 之间发送信息的行为，另外这样看实际上 user 不能作为通信的对象，因为 user 没有完整地址？」
- 「user 同意方式一」

已裁：

- 通信的定义：N 个有确定地址的 actor 之间发送信息的行为。
- user 按「方式一」处理：`user:<owner>` 有确定的身份，没有确定的位置。它可以是 conversation 的成员；投递时解析到它的端点（GUI、adapter 账号、user-proxy），端点才有完整地址。没有采用的「方式二」是只让端点作为 actor、user 仅以 `onBehalfOf` 出现；它会把同一个人在 GUI 与飞书里的对话拆成两个，并与 11-08、11-16 的两条裁定冲突。

按这个定义对 Q11–Q13 的推论【提议】：

| 待裁项 | 推论 |
|---|---|
| Q12 cell 内交付 | 不算通信：cell 内成员没有地址 |
| Q13 GUI 进度流 | 不算通信：是读取，不是向某个地址发送 |
| Q12 hook 交换 | 算通信，除非排除保留 actor |
| Q11 PAC 派单与 worker 回复 | 算通信，除非排除保留 actor |

保留 actor 指 daemon 自己占用的两个 agent 名：`dispatch-coordinator`（PAC 通知与跨机派单的发送身份）和 `_turn-hooks`（hook 交换的发送与回收身份），定义在 `kernel/impl/contracts/reserved_actors.py`。它们是否算通信主体见 §12.2。

### 12.2 保留 actor 也是通信主体（owner，2026-11-17）

原话：「我认为也应该算作通信主体，在 GUI 等地方展示时再过滤，不应该在代码和架构上做特殊化」

已裁：

- 保留 actor（`dispatch-coordinator`、`_turn-hooks`）是通信主体。它们收发的消息与其他消息一样归入 conversation 并被记录。
- 机制流量与对话的区分放在展示一侧（GUI 等处过滤），不放在代码和架构里。

据此，Q11 与 Q12 的结果：

| 项 | 结果 |
|---|---|
| Q11 PAC 派单、worker 对派单的回复 | 算通信，要记录 |
| Q12 hook 交换 | 算通信，要记录 |
| Q12 cell 内交付 | 不算通信（成员没有地址，§12.1） |
| Q13 GUI 进度流 | 不算通信（§12.1 的推论，owner 未单独确认） |

这条裁定取代此前「SYSTEM 类不记录」的做法（`inbox/envelope/rules.py` 引 cell model 计划的 Q4）。

需要改的地方【提议】：

1. recorder 不再跳过 SYSTEM 类的行（§13.2）。消息类别保留为记录上的一个字段，供展示一侧过滤；它不再决定是否记录。
2. 保留 actor 可以派生对话归属，不再落入 `unthreaded`（§13.3 第 8、8a 行）。
3. 发送者为 `system:hyprial:<node>` 的 daemon 通知：按「不做特殊化」的原则推断也应记录。但 `system:` 不是 agent 形式的地址，它是否算「有确定地址的 actor」owner 没有明说，**待确认**。
4. 展示一侧需要一个过滤依据。提议用第 1 条的类别字段，加上发送者是否为保留 actor。

带来的问题：

- **数量。** 每次 hook 交换现在各用一个一次性的 conversationId（`turn-hook:<随机 id>`），每次 PAC 派单也是独立的。全部记录后，conversation 的数量会由机制流量主导。可以让它们归入「保留 actor 与对方」的默认对话（与 Q7 的提议同一规则），但 hook 目前靠这个一次性 id 匹配回复，改动需要另找匹配依据。
- **保留期。** 机制流量的记录是否与对话同样长期保留，需要定策略。这与 #2712 的 D8 是同一个问题。
- **相邻的特殊化。** 保留 actor 还豁免发送许可（send-link）。按「不做特殊化」的原则它也在讨论范围内，但那是准入而不是 conversation，与 `links.bypass` 一样另行决定。

### 12.3 保留 conversation 这个名字（owner，2026-11-17）

原话：「保留 Conversation 这个名字。」

已裁：只保留的那一层叫 conversation，取消 thread 这个名词。成对的发送许可继续叫 send-link。

### 12.4 与 11-03 原始设计的一处冲突【待 owner 确认】

conversation 在 11-03 设计稿里的来由是拆分收件箱：投递队列留在本地且不留正文，人与 agent 的交互正文写入 conversation，「系统运行信息只走本地队列，不进 OrgFS」（该稿 §0 第 4 条、§6）。当时的 owner 原话是「应该拆分收件箱，区分系统运行的信息和人类/agent 交互的信息」。

§12.2 裁定保留 actor 的流量也要记录，这与上面第三条相反：PAC 内部派单、workflow 节点通知原本被明确列为不进 OrgFS 的系统运行信息。两条都是 owner 的裁定，需要确认以哪条为准，或者如何划线（例如：进 conversation 的是 actor 之间的派单与回复，看门狗、TTL 到期这类没有对端 actor 的通知仍只留本地）。

### 12.5 职责边界【已裁：owner 2026-11-17「确认」】

owner 2026-11-17：「难道现在 Conversation 要管谁能和谁通话以及如何投递吗？」

不管。取消 thread 这个名词，不等于把它的职责交给 conversation。三件事各有归属：

| 问题 | 归属 | 是否变化 |
|---|---|---|
| 谁可以给谁发、按什么流控 | send-link | 不变 |
| 怎么送达：确认、重试、离线暂存、唤醒 | 投递队列（本地收件箱） | 不变 |
| 这条消息属于哪次交流、正文记在哪 | conversation | 新增「属于哪次交流」，即每条消息都有确定的 conversation 归属 |

「所有通信都走 conversation」按此理解为：每条消息都有 conversation 归属并被记录，不是由 conversation 来执行投递或准入。

群聊给 conversation 增加的只有一份成员表。它是数据，conversation 自己不拿它做任何事。群消息的投递方式见 §14.5 的裁定：成员各自从群的 space 同步后在本地投递，这时群文档先于投递被写入，与 1:1 的顺序相反。本稿 §7.1 早先把流控参数也写进成员表，已改回。

仍未裁：Q2、Q3、Q5（成员表的权威）、Q6、Q7、Q14、§12.4 的划线（含 `system:` 发送者是否算 actor）、机制流量的归并与保留期、B 线与 #2712 的先后。

## 13. 不经过 conversation 的通信路径【已核，另注者除外】

读数基线：`origin/dev` @ `1dddd5325`。只读代码，未运行验证。

一条消息完整地走 conversation，要经过三步：进本地收件箱、被 recorder 写入记录、唤醒接收方。下表按缺哪一步分类。

### 13.1 不进收件箱

| # | 路径 | 实际去向 | 记录 | 出处 |
|---|---|---|---|---|
| 1 | `message.send` 发给 `user:<owner>` | agent-bot 或 `_user_delivery.deliver`，到飞书 DM | 无 | `inbox_surface/send.py:249-386` |
| 2 | `message.send` 发给 `route:<adapter>:<route>` | `gateway.send_chat` 等直接发到外部群 | 无 | `send.py:387-399`；`delivery/sends.py:589` 起。只读了该函数的发送段，未见收件箱提交 |
| 3 | daemon 的告警（watchdog、收信告警、升级）发给 owner | 经 Squire 的 DM 路径 | 无 | `delivery/sends.py:189-300`。docstring 自述「不写收件箱消息」，函数体未逐行读 |
| 4 | user-proxy 转发到外部群（`route:` 目标） | 同第 2 行 | 无 | `delivery/sends.py:478-497` |

### 13.2 进收件箱，但不交给 recorder

recorder 只读 INTERACTION 类的行；SYSTEM 类被跳过（`conversations/record/recorder.py` 的决策注记；分类规则在 `inbox/envelope/rules.py`）。SYSTEM 类包括：

| # | 路径 | 判定依据 |
|---|---|---|
| 5 | daemon 自己发出的通知、告警、回复提醒 | 发送者为 `system:hyprial:<node>` 且 `intent == "system"` |
| 6 | PAC 通知、workflow 请求、actor 告警、routine 任务 | message id 带派单前缀 |
| 7 | worker 对 PAC 派单的回复 | idempotency key 为 `reply:workflow-<uuid>` |

这是此前的有意裁定，不是遗漏。owner 2026-11-17 已裁定改为记录（§12.2）。

### 13.3 到了 recorder，但被跳过

| # | 跳过原因 | 含义 | 出处 |
|---|---|---|---|
| 8 | `unthreaded` | 两端都不是 agent、发送者是 `system:` 或裸名字、或一端是 daemon 保留的 agent 名（dispatch 服务、turn-hooks；`kernel/impl/contracts/reserved_actors.py`） | `recorder.py:382`；`threads/ids.py` 的决策注记 |
| 8a | 同上，经 user-proxy 的两个方向 | 发给 `user:<owner>` 的消息交给在线的 user-proxy（`delivery/sends.py:317-385`），以及 user-proxy 代为转发给 agent（`sends.py:455-506`），都以 dispatch 服务为发送者经 `message.send` 进收件箱。dispatch 服务是保留名，按上一行的规则没有 thread。据此推断这两类消息不被记录；推断依据是 thread 按 `from` 而不是 `onBehalfOf` 派生，这一点未核对 | 同上 |
| 9 | `redacted`、`redaction-invalid` | 脱敏钩子返回空或非字节内容 | `recorder.py:399-401` |
| 10 | `too-large` | 超过记录的大小上限 | `recorder.py:423` |
| 11 | `cell-<code>`、`cell-id-mismatch` | conversation 文档无法建立或 id 不一致 | `recorder.py:462-467` |
| 12 | 没有 cell 运行时 | 整个 daemon 不记录任何消息。什么情况下没有运行时未核对 | `recorder.py` 的决策注记 |

### 13.4 走了完整路径的

- agent 对 agent 的 `message.send` 与 `message.reply`。
- agent 回复来自 `user:<owner>` 的消息：`message.reply` 提交并被记录（P1 测试证实）。
- 四段式 `adapter:<owner>:<machine>:<name>` 发来的入站消息：被记录（P1 测试证实）。
- **更正（P1，2026-11-17）**：本小节早先把飞书入站与 agent 对飞书消息的回复列在这里，并注明「记录为推断」。测试表明推断是错的，两者今天都**不被记录**，见 13.7。

### 13.5 范围未定的

以下不是消息，或是否算「通信」需要 owner 判断：

- hook 交换：harness 与 daemon 之间的一问一答，经收件箱，conversationId 带专用前缀（`harnesses/turn_delivery/`）。turn-hooks 是保留名，按 13.3 第 8 行推断不被记录，未核对（Q12）。
- cell 内交付：#2712 的 11-14 增补规定它不是消息（Q12）。
- GUI 的 `progress.list` 进度流（Q13）。

### 13.6 按裁定需要做的

| 类别 | 改法 | 对应步骤 |
|---|---|---|
| 13.1 | 发送一律先进 conversation；外部 adapter 改为成员的投递端点 | A1、A4；第 3 行随 Q11 |
| 13.2 | 已裁（§12.2）：SYSTEM 类也记录，类别只用于展示过滤。归入哪个对话随 Q7 | A5 |
| 13.3 第 8 行 | 取消 thread 后改为「没有对话归属」。user 对 user 给 conversation（已裁；它现在先落在 13.1 第 1 行的分支，到不了 recorder）；经 user-proxy 的两个方向（8a）改为以真实发送者归入对话；`system:` 发送者与内部 agent 随 Q11 | A3、A5 |
| 13.3 第 9–11 行 | 等 Q14。第 11 行是故障，应当报错而不是静默跳过 | A5 |
| 13.3 第 12 行 | 没有运行时就不记录，等于整体绕过。提议改为启动条件 | A5 |

### 13.7 P1 特征测试的更正（2026-11-17）

P1 把本节的推断写成了测试（PR #2903，`tests/daemon/conversations/bypass/`，在 `HYPRIAL_ACTOR_BACKEND=reference-abi` 下运行）。与本节原文不符或需要补充的：

- **飞书入站不被记录。** Lark worker 实际使用的发送者是三段式 `adapter:lark:<name>`（旧写法 `channel:lark:<name>` 同理）。thread 的派生只认四段式的 adapter 地址，三段式得不到 thread，于是收件箱行与发送方副本都被 recorder 以「无 thread」跳过，也不出现在 `conversations.list` 里。它应归入 13.3，而不是 13.4。
- **agent 对飞书消息的回复同样不被记录。** 已提交，但没有 thread。
- **13.2 的 `system:` 通知被排除了两次。** `delivery/authority.py` 在查看消息类别之前，先丢弃 intent 不是 `request` 或 `reply` 的行。只改类别判断，这些通知仍然到不了 recorder。
- **13.3 第 8a 行的推断成立。** 经 user-proxy 的两个方向，发送者都是 `dispatch-coordinator`，真实发送者只在正文的 `origin.onBehalfOf` 里；thread 按发送者派生，两份都被跳过。
- **13.5 的推断成立。** hook 交换的请求与回复都因没有 thread 被跳过。
- **每条持久提交的消息有两行会交给 recorder**：收件箱行和发送方副本。同一 owner 的 1:1 因此在记录里有两个条目；`conversations.list/history` 读的是队列，按消息 id 去重，只显示一条。
- 目标 `user:` 在另一节点时（只读代码，未测试）：发送节点不写任何队列行；消息经 Zenoh 发到拥有该用户的节点，那里有在线的 user-proxy 就交给它，否则由 Squire 的 adapter 发 DM。

## 14. 归属规则：参考 Telegram 的简化【提议 · 待 owner】

owner 2026-11-17：「"一条消息怎么得到它的 Conversation" 我建议你参考 telegram 的设计简化实现，实际上，我认为 conversation 本质上和一个 crdt 文档没有区别」

对 Telegram 的描述来自记录者的记忆，未查阅其文档。代码读数基线 `origin/dev` @ `1dddd5325`。

### 14.1 从 Telegram 借用的四点

| Telegram 的做法 | 借用为 |
|---|---|
| 消息发给一个 peer（用户、群、频道）。发给谁就属于和谁的对话 | 不传 conversationId 时，发送的目标决定 conversation |
| 私聊中双方各有一份记录；频道和超级群只有一份共享的记录 | 1:1 每个 owner 各记一份（现状）；群聊是一份成员共享的文档 |
| 回复用被回复消息的 id | `replyTo`，是记录上的字段 |
| 一个账号有多个设备会话，更新推送给所有会话 | user 是成员，端点是它的各个会话（§12.1 已裁） |

### 14.2 规则（2026-11-17 修订）

owner 2026-11-17：「message send 为什么要去掉 conversationid？actor 对话隔离不在本次范围，一个群一个 space」

已裁：

- 同一对 actor 之间多条对话的隔离不在本次范围。
- 一个群一个 OrgFS space。

本节初稿提议取消 `message.send` 的 conversationId 并引入 `topic`。这不是必需的，已撤回。修订后的规则【提议】：

1. **`conversationId` 保留，接口不变。** 传了就归入它指的那个 conversation。
2. **不传时归入默认对话。** 默认对话是发送者与目标之间的 1:1 conversation，id 由两端地址派生。这取代现在的随机 `uuid4`，是唯一的行为改动。
3. **群聊用 conversationId 寻址。** 群在创建时得到一个 conversationId 和一个 space。向群发消息就是带上这个 id 发送，接收者由群的成员表决定。
4. **记录是一份 CRDT 文档。** 1:1 的发送路径不变：send-link 准入、投递队列送达、送达后写入记录（§12.5）。群聊按 §14.5 的裁定，先写入群文档，成员同步后再本地投递。

记录的键：1:1 对话仍是（owner，两端，conversationId），与现状相同，只是「两端」不再对外称为 thread。保留「两端」是必要的，因为现在有多处使用固定字符串作 conversationId（如 `workflow`、`quota-watchdog`），不同的两端之间会重名。群聊的键是群的 conversationId，记录在群自己的 space 里。

### 14.3 现有 conversationId 的用法【已核其来源】

全部保持不变，只有第一行改动：

| 现在的 conversationId | 来源 | 处理 |
|---|---|---|
| 随机 `uuid4` | `message.send` 未传时 | 改为两端的默认对话 |
| `uuid5(sender, target)` | GUI | 不变。可改为不传，由 daemon 派生 |
| `pac-<graphId>`、`workflow` | PAC 派单与通知 | 不变 |
| `routine:<名字>` | routine 执行 | 不变 |
| `quota-watchdog`、`inbox-watchdog`、`daemon-<kind>` 等 | daemon 告警 | 不变 |
| `turn-hook:<随机 id>` | hook 交换 | 不变。每次交换一个 conversation 的数量问题仍在，见 14.5 |
| adapter 给出的 id | 飞书入站 | 不变 |

### 14.4 「conversation 是一份 CRDT 文档」

存储上已经是这样【已核，`conversations/record/segments.py`】：记录放在 OrgFS，每个写入节点只追加自己的 `rec-<节点>/` 分段，读取时取所有分段的并集，条目按键去重。这是一个只增不改的 CRDT。

按这个看法可以去掉的东西：

- **成员表的权威（Q5）。** 成员表是群 space 里文档的一个字段，随文档同步与合并，不需要指定「以谁的那份为准」。
- **跨节点的顺序。** 没有全局先后，合并后按（时间，节点，序号）排出一个各方一致的顺序。并发消息在同步完成前各方看到的先后可能不同。Telegram 靠中心服务器给出唯一顺序，这里没有中心，这一点不能照搬。
- **群的权限。** 一个群一个 space，现有的按 space 判断读写的权限直接可用，不需要按文档的权限。
- **owner-home。** owner 2026-11-17 裁定按参与者划分 chat space，每个 conversation 一份共享文档；owner-home 只是「成员只有一个 owner」时的 chat space。详见实施计划 §2.1。

仍然不同于普通文档的地方：文档是被动的，写入文档不会唤醒对方，也不经过准入。1:1 对话因此仍然先走队列。群聊按 §14.5 的裁定以写群文档为发送入口，唤醒由各成员的 daemon 在同步到新条目后完成；准入放在哪里尚未设计，11-03 的规则「闸门必须由 daemon 代码执行」仍需满足。

### 14.5 这套规则解决与留下的问题

解决：Q7（归属）、Q5（成员表权威）、群的权限。`message.send` 的接口不变，记录的键不变。

留下：

- hook 交换每次一个 conversation。保留 actor 的流量开始记录之后（§12.2），这类 conversation 会很多。可以只改 hook 自己：不传 conversationId，落入 `_turn-hooks` 与该 agent 的默认对话，用 `replyTo` 匹配回复。这是 hook 内部的改动，不涉及接口。
- 群的 space 何时创建、成员增减如何反映到 space 的成员上，未设计。
- 群消息的投递【已裁，owner 2026-11-17：「最后一项我也建议用第二种」】：成员各自从群的 space 同步到新消息后，在本地投递并唤醒。不采用发送者的 daemon 按成员表扇出。space 的读写成员可以不经 `message.send` 直接写文档，准入检查的位置需要另行设计（实施计划的 P3）。

### 14.6 收口替代说明（2026-11-22）

上文 §14.2、§14.5 中关于「一个群一个 space」与「成员各自从群 space 同步新消息后在本地投递唤醒（R12）」的早期裁定，已由 2026-11-18 的单机群聊决定及已合入 `dev` 的 GC1–GC4（#2956/#2968/#2972）取代：当前实现将群文档放在 owner 的 chat space 内，被唤醒成员的收件箱行经本地投递传输层接收端进入（`app._local_delivery.deliver` → `inbox.receive`，见 `src/hyprial/daemon/impl/application/messaging/inbox_surface/groups/delivery.py`），沿用去重、入站准入、容量限制与重启重放语义。另：临时唤醒规则随后在 GC4-M（#3046）与 GC-R（#3054）中收敛为仅唤醒被显式 `@` 提及的 agent 成员（群回复默认 `@` 被回复消息作者）。
