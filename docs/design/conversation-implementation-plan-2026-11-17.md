# Conversation 收口：实施计划（2026-11-17）

- 日期：2026-11-17
- 状态：**本轮规划已按裁定实施收口（脱敏归档）**（2026-11-17 owner 确认开工；当时进度见 §10，收口附注见 §18.5 末）。§1 是 owner 已裁定的内容；§2 的默认值除 D4 外已获 owner 认可；其余为当时提议与阶段记录。本稿按脱敏标准仅保留统计数字与分析逻辑，已去除内部人员、机器、会话链接与本机路径等细节；最终实现状态以当前 `dev` 代码与 [GLOSSARY.md](../../GLOSSARY.md) 为准
- 记录：实施规划记录（非注册 agent，已脱敏）
- 读数基线：`origin/dev` @ `787037c19`。文件行数与目录文件数是在该基线上数的；其余代码结论来自读代码，**没有运行验证**
- 讨论过程与逐条原话：[terminology-and-conversation-plan-2026-11-16.md](terminology-and-conversation-plan-2026-11-16.md)（下称「讨论稿」）。两份冲突时以本稿 §1 为准
- 相关：PR #2882（起因）、PR #2712（cell-mode 执行模型）、
  [cell-model-phase-b-handoff-2026-11-07.md](cell-model-phase-b-handoff-2026-11-07.md)（phase B 的批次纪律）

## 1. 已裁定的设计（owner，2026-11-16 至 11-17）

| # | 裁定 | 讨论稿 |
|---|---|---|
| R1 | cell、PAC、conversation 是三个互不相关的概念：实体如何组织、任务如何运行、主体之间如何交互 | §12 |
| R2 | routine 属于 PAC，不是 cell | §12 |
| R3 | thread 与 conversation 只保留一层，名字是 conversation。成对的发送许可继续叫 send-link | §12、§12.3 |
| R4 | 通信是 N 个有确定地址的 actor 之间发送信息的行为。所有通信都有 conversation 归属并被记录，不允许绕过 | §12、§12.1 |
| R5 | user 有身份、没有位置：可以是 conversation 的成员，投递时解析到它的端点 | §12.1 |
| R6 | 保留 actor（`dispatch-coordinator`、`_turn-hooks`）也是通信主体；机制流量在展示一侧过滤，不在代码和架构里特殊化 | §12.2 |
| R7 | conversation 不负责准入与投递：准入归 send-link，送达归投递队列，conversation 负责归属与记录 | §12.5 |
| R8 | 需要两端都是 user 的对话 | §12 |
| R9 | 群聊唤醒全部 agent 成员，是否处理由该 agent cell 内的 handler 决定 | §12 |
| R10 | 同一对 actor 之间多条对话的隔离不在本次范围 | §14.2 |
| R11 | 一个群一个 OrgFS space | §14.2 |
| R12 | 群消息的投递：成员各自从群的 space 同步到新消息后，在本地投递并唤醒（历史裁定；后续已被 §18.5 与 GC4 #2972 的「群文档 + 本地 delivery transport」方案取代，见文末 2026-11-22 收口附注） | §14.5 |
| R13 | 每个 conversation 一份共享文档，1:1 也是。原话（2026-11-17）：「为什么不为 1:1 也设立一个独立的文档？我理解应该是每个 conversation 一个共享文档」 | 本稿 §2.1 |
| R15 | GUI 呈现共享文档是本轮的开发目标。原话（2026-11-17）：「GUI 呈现共享文档是本次开发目标」 | 本稿 §11 |
| R16 | 不依赖 Forgejo runner，在本地跑与任务相关的 CI 以加快进度。原话（2026-11-17）：「我建议你不要依赖 forgejo runner 而是在本地跑完 CI（只跑与你任务相关的），从而加速开发进度」 | 本稿 §11.3 |
| R17 | GUI 绑定 user-proxy，取代 2026-11-08 的「GUI 身份统一为 `user:<owner>`、不作为 GUI 前提」。原话（2026-11-17）：「我认为应该 GUI 绑定 user-proxy」 | 本稿 §12 |
| R18 | 用户没有配置任何端点时，发给 `user:<owner>` 的消息算失败。原话（2026-11-17）：「用户没有配置端点时，发消息算失败」 | 本稿 §12 |
| R19 | 本地 CI 通过后可强制合并非 GUI 的 PR；探查稿里的内部细节不推送，只报统计数字与分析逻辑（2026-11-17） | 本稿 §11.3 |
| R20 | 统一 user 与 agent 的逻辑：GUI、CLI 等界面用同一套逻辑，`user:<名字>` 与 `agent:<名字>` 整体一致。原话（2026-11-17）：「感觉这样很hack？按道理，gui也好，cli也好，都应该用类似的一套逻辑」「如果进一步统一 user:<user_name> 和 agent:<agent_name> 整体的逻辑就更加一致了，请先调研当前代码的情况」。R17 的「GUI 绑定 user-proxy」按此理解为：user 有一个自己的 agent，各界面是它的成员 | 本稿 §13 |
| R21 | user 就是一个 agent：没有模型，成员是界面；`user:<名字>` 与 `agent:<名字>` 都是名字，由 daemon 解析。四项配套建议一并同意：每个用户一个用户 agent，放在首选接收机器上；squire 兜底 DM 与 agent-bot 直发并入飞书界面；现有 user-proxy 脚本并入飞书界面；user 的发送者认证日后改为会话制。原话（2026-11-17）：「同意你的建议」 | 本稿 §14 |
| R22 | 四段式地址的退役记为目标方向，本轮不做。原话（2026-11-17）：「好的，那先不做，但在文档中记录此次决策过程，以便未来查看」 | 本稿 §15 |
| R23 | 三项待回复事项按记录者的建议处理。原话（2026-11-17）：「三项待我回复的事项都按你的建议处理」。即：① 被跳过的消息在记录里留不含正文的占位条目；② 「正文只存文档、队列只送通知」记为目标架构，不在本轮；③ #2885 的文档——记录者此前对这一项**没有给出过明确建议**，只提出了问题。这里按 owner 对探查稿的同一指示（R19）理解为：合并前去掉内部细节，只保留统计数字与分析逻辑，在那之前保持 Draft。此理解待 owner 确认 | 本稿 §11、§16 |
| R24 | K2 选「删掉覆盖能力」：`routine` cell kind 移除，默认例行任务固定用内置模板。R23 第 ③ 项的理解一并确认。原话（2026-11-17）：「同意」 | 本稿 §17 |
| R25 | 本轮范围延伸到群聊：尽可能自驱开发，做到可以支持群聊时通知 owner，并说明如何进行群聊（此前从未在 hyprial 上开展过群聊）。原话（2026-11-17）：「请尽可能自驱开发，完成到可以支持群聊时通知我。注意此前我们还从来没在 hyprial 上开展过群聊，你需要告诉我如何进行群聊」 | 本稿 §17 |
| R14 | conversation 按参与者划分 chat space：它存放在一个 chat space 里，这个 space 的成员正好是对话参与者所属的 owner。owner-home 不再是单独的概念。原话（2026-11-17）：「我建议简化设计，所以 conversation 都保存在 chat/ space 下，为什么需要每个 owner 单建一个 owner home ？」「按参与者划分 chat space，你理解准确。」 | 本稿 §2.1 |

## 2. 为开工采用的默认值

owner 2026-11-17：「D4 为什么 1:1 和群聊相反？请举例解释一下。其他几项用你的默认值」。据此 D1、D2、D3、D5、D6、D7 **已认可**；D4 经 owner 追问后按 R13 改写，改写后的版本待确认。

| # | 事项 | 默认值 | 影响的切片 |
|---|---|---|---|
| D1 | `message.send` 的归属规则（讨论稿 §14.2 规则 1–3） | `conversationId` 保留；不传时归入两端的默认对话；群聊用 conversationId 寻址 | C1 及之后全部 |
| D2 | 默认对话 id 用哪个公式 | 由 daemon 按两端稳定 `endpoint_key`（`conversations/ids.py`）派生（不分先后：对 `a.<id>` 形态的 agent URI 取其实体 id，`user:<owner>`、`adapter:`/`channel:`、`route:` 及未解析名称仍按规范地址或原字符串）。GUI 现用的 `uuid5("hyprial:gui:conversation:{sender}:{target}")` 与它**形状相同、数值不同**，不能混用。GUI 改为不传 conversationId，并按两端读取，这样旧公式下的历史仍然可见 | C1、U1、G1 |
| D3 | user 的端点策略（Q3） | 保持现有行为：飞书照发。消息另外进入 conversation，GUI 从记录读取 | U1 |
| D4 | 1:1 与群聊的传输方式 | 存储已按 R13、R14 统一。传输暂不统一：1:1 沿用投递队列，送达后双方写入同一份文档；群聊按 R12 由文档同步送达。是否让 1:1 也改由文档同步送达，等 P3 量出同步延迟后再定（§2.1） | 组 4 |
| D5 | 没有对端 actor 的系统通知（看门狗、TTL 到期等；讨论稿 §12.4） | 暂不改，仍只留本地。actor 之间的派单与回复按 R6 记录 | S1 |
| D6 | 脱敏拒绝与超大消息（Q14） | 暂不改，保留「已送达但不入记录」 | S2 |
| D7 | 删除 OrgFS 侧 `graph` cell 与 `pac_mirror`（Q2） | 删除 | K1 |

### 2.1 R13 与 R14 的落法

存储和传输是两件事。R13、R14 说的是存储，已裁；传输是提议。

**存储：每个 conversation 一份文档，放在按参与者划分的 chat space 里。**

space 表达谁在里面，space 里的每份文档是一次对话。同一组参与者之间的多个对话（例如 `pac-<graphId>`）是同一个 space 里的多份文档。

| 对话的参与者 | 存放的 chat space | 与现状的关系【现状已核，`conversations/record/contracts.py`、`composition/cells/spaces.py`】 |
|---|---|---|
| 都属于 owner X | 成员只有 X 的 chat space | 就是现在的 owner-home（`hyprial-home`）。**更正（P3，2026-11-17）**：它不是「一份文档」。每个写入节点各追加自己的 `rec-<节点>/` 分段文件，见下方「P3 对本节的更正」 |
| 属于 X 和 Y | 成员是 X、Y 的 chat space | 新增。现状是 X、Y 各在自己的 home 里记一份，互不同步 |
| 一个群 | 这个群的 chat space（R11） | 新增 |

需要按 space 划分的原因：OrgFS 的读写权限与同步都以 space 为单位，space 的成员能读到其中全部文档。没有采用「所有对话放进一个共享 space」，因为那样每个成员都能读到所有人的对话。

规则假设 space 的成员按 owner 计。这是从 `spaces.py` 的注记推断的，OrgFS 的成员模型没有读（P3 核对）。

**P3 对本节的更正（2026-11-17；前两条已由记录者对照 `conversations/read.py` 与 `conversations/record/segments.py` 的文件头注记核对，其余转引自 P3 的探查稿，未复核）**

- 本节早先把「同一 owner 内已经是一份文档，各台机器写同一份」标为已核，这是错的。记录是每个写入节点一个分段文件，没有一份合并后的文档。
- `conversations.list/history` **不读记录**。它从本地投递队列派生对话与分页（`read.py` 文件头：「Reads derive that id locally from the delivery queue, so listing and paging a conversation never needs OrgFS」）。记录只用于按引用取回正文。所以「GUI 改读 `conversations.*`」得到的是本机队列的投影，不是一份共享文档。R13 所说的「每个 conversation 一份共享文档」在读取一侧今天不存在。
- 非 org 目录的 space（包括 `hyprial-home`）只在信任 tailnet 成员的模式下才在节点间同步（P3 实测：关闭信任时加入被拒绝）。即同一 owner 的两台设备之间，home space 默认也未必同步。
- space 的成员是 owner 级的主体，agent 不是成员（与本节的假设一致）。群聊因此还需要一份 actor 级的成员表，OrgFS 不提供。
- P3 另有若干关于同步准入与读取权限的发现，属于既有产品的行为，未经运行验证。细节暂不写入本稿，等 owner 决定如何记录。

同一份文档里，发送方副本与接收方收件箱行现在是两个条目（条目按来源、位置与内容摘要定键，不按消息 id）。读取时是否合并成一条未核对（P1 核对）。

**传输：暂时仍是两种。**

| | 传输 | 原因 |
|---|---|---|
| 1:1 | 投递队列。送达后写入共享文档 | 队列已经实现确认、重试、过期与离线暂存；hook 这类带超时的问答依赖它的延迟 |
| 群聊 | 文档同步（R12） | 没有既有实现；一份文档同步给所有成员，省去按成员逐个投递 |

把 1:1 也改成由文档同步送达，等于用文档同步取代投递队列，是本计划里最大的一项改动，而同步延迟没有数据。提议由 P3 顺带量出延迟，届时再定。

## 3. 施工约束【已核】

- **两个文件已到硬上限。** `daemon/impl/application/messaging/inbox_surface/send.py` 正好 1000 行，所在目录正好 7 个文件；AGENTS.md 规定每个代码文件最多 1000 行、每个目录最多 7 个直接文件。任何让它变长的改动都必须先拆分。`shell/impl/gui/adapter/chat.py` 是 988 行，同理。
- **其余相关文件**：`delivery/sends.py` 844 行（目录 5 个文件）、`conversations/record/recorder.py` 572 行（目录 6 个）、`threads/service.py` 367 行、`cells/model.py` 363 行。
- **批次纪律**（phase B handoff §5）：每段从最新 dev 建短分支、开独立子 PR；前序依赖未合入时不堆叠。合并授权见 §8：非 GUI 的切片门禁通过后直接合入 dev。
- **测试纪律**（AGENTS.md）：行为变化要有先失败再通过的测试；`src/hyprial/` 有改动时跑全量 pytest 与 ruff；改已发布字段语义要同步 `contract/` 或在 PR 写 `Contract-Impact`。
- **在途的相邻工作**：GUI 线有 #2878（chat 页面层次）、#2822（session host 并入 request_http）、#2690（GUI 重构 R8）；phase B 第二批原定 GUI 改读 `threads.*` 与 `conversations.*`。动 `gui/` 之前要先和 GUI 线确认。#2712 的 S1 会为各 cell kind 写 actor 处理逻辑。

## 4. 切片

同一个文件只归一个切片；共用文件的切片串行。

### 组 0：准备（互不依赖，可同时开工）

| 切片 | 内容 | 拥有的文件 | 验收 | 契约 |
|---|---|---|---|---|
| P1 特征测试 | 把讨论稿 §13 里标为推断的四项写成测试，记录现状：经 user-proxy 的两个方向是否被记录；飞书入站是否被记录；agent 回复 adapter 是否被记录；目标 `user:` 在另一节点时走哪条路 | 只新增 `tests/` 下的文件 | 测试通过并如实记录现状；与推断不符的地方回写讨论稿 §13 | 无 |
| P2 拆分发送入口 | 纯搬移，不改行为。把 `send.py` 里 `user:` 目标分支、`route:` 目标分支、`message.reply` 各自移到 `inbox_surface/` 下的子包；把 `delivery/sends.py` 里的 user、route、转发三段分开 | `inbox_surface/`、`delivery/sends.py` | 既有测试全部通过；每个文件低于 1000 行，每个目录不超过 7 个文件 | 无 |
| P3 群聊探查 | 只读代码、出一份设计短文，不写产品代码。另在隔离环境量一次 space 的同步延迟与建 space 的成本（§2.1）。回答：daemon 如何得知某个 space 里有新内容（`orgfs/api.py`、`orgfs/composition/lifecycle.py` 里有订阅类函数，未读）；群的 space 何时创建、成员增减如何同步；space 的任何读写成员都能绕过 `message.send` 直接写文档，准入放在哪里 | 新增一份 `docs/design/` 文档 | owner 审阅 | 无 |
| K1 删除 `graph` 镜像 | 删除 `graph` cell kind 与 `composition/pac_mirror/`。删除前核对开关配置与线上是否有数据 | `cells/model.py`、`composition/pac_mirror/` | 先加「`graph` 不再是合法 kind」的失败测试 | 查 `contract/` 是否提到；否则写 `Contract-Impact` |
| T1 术语表 | GLOSSARY 按 §1 改 cell、conversation、routine、thread 四条；link、session、channel 各补一句区分 | `GLOSSARY.md` | 文档门禁 | 无 |

K1 应在 #2712 的 S1 之前或同时合入，避免为将被删除的 kind 写 actor 处理逻辑。

### 组 1：1:1 对话不再绕过（P2 合入后开工，四片可同时进行）

| 切片 | 内容 | 拥有的文件 | 依赖 | 验收 | 契约 |
|---|---|---|---|---|---|
| C1 默认对话 | 新增由两端地址派生默认 conversation id 的纯函数；`message.send` 不传 conversationId 时用它，取代 `uuid4` | 新增 `conversations/ids.py`；发送主文件里取默认值的一处 | P2 | 同一对地址双向发送得到同一个 id；不传 conversationId 的两次发送落在同一个对话 | `message.send` 的默认行为变化，同步 `contract/daemon-request`、`contract/mcp-tools` |
| U1 `user:` 进收件箱 | 发给 `user:<owner>` 的消息提交本地收件箱并被记录；飞书等端点投递保留，改为在此之后执行（D3） | P2 拆出的 user 目标模块；`delivery/` 里的 user 段；`adapters/user_delivery/` | P2；端到端验收还需 C1 | **agent 不带 conversationId 发给 `user:<owner>`，这条消息出现在该用户与这个 agent 的同一个对话记录里** | 同上 |
| U2 `route:` 进收件箱 | 发给 `route:` 外部群的消息先提交收件箱并被记录，再由 adapter 发出；user-proxy 转发到外部群的路径相同 | P2 拆出的 route 目标模块；`delivery/` 里的 route 段 | P2 | 发往 `route:` 的消息在发送方的对话记录里可见 | 同上 |
| S1 机制流量入记录 | 保留 actor 可以得到对话归属，不再落入「无归属」；recorder 读取 SYSTEM 类的行，类别作为记录上的字段保留，供展示过滤。经 user-proxy 的消息按真实发送者归属。没有对端 actor 的通知不改（D5） | `conversations/record/recorder.py`、`conversations/read.py`（对话列表由它从队列派生，同样略过没有归属的分组）、`delivery/authority.py`、`threads/ids.py`、`inbox/envelope/rules.py` | P1（先知道现状） | PAC 派单与 worker 回复出现在记录里并带类别；经 user-proxy 的消息出现在真实发送者的对话里 | 记录条目新增类别字段 |
| K2 移除 `routine` cell kind | `routine` 从 cell kind 移除；`agent-default` 配置的去处由 PAC 一侧决定 | `cells/model.py`、`application/actors/routine_defaults/` | K1（同一个文件） | 兼容读取旧数据；先确认线上是否有数据 | 视 `contract/` 而定 |

### 组 2：收尾 1:1（依赖组 1）

| 切片 | 内容 | 拥有的文件 | 依赖 | 等待 |
|---|---|---|---|---|
| G1 GUI 改读 conversation | GUI 不再自算 conversationId，发送时不传；列表与历史改读 `conversations.*`，按两端读取以保留旧历史；按类别过滤机制流量（R6）。`chat.py` 988 行，先拆再改 | `shell/impl/gui/adapter/`、`gui/` 的 chat 部分 | C1、U1、S1 | GUI 线确认；与 #2878、#2822、#2690 协调 |
| A1 user 对 user | **移出本轮。** 两个 user 必然属于两个 owner，整片归组 4 的第 0 步 | — | 组 4 | — |
| S2 recorder 跳过条件 | conversation 文档建不出来时报错而不是静默跳过；没有 cell 运行时改为启动条件。脱敏与超大消息不改（D6） | `conversations/record/recorder.py` | S1（同一个文件） | Q14 |
| H1 hook 用默认对话 | hook 交换不再每次生成一次性的 conversationId，落入 `_turn-hooks` 与该 agent 的默认对话，用 `replyTo` 匹配回复 | `harnesses/turn_delivery/` | C1、S1 | 无 |

G1、A1、H1 可同时进行。S2 与 A1 都动 recorder 的归属部分，A1 先、S2 后。

### 组 3：取消 thread 这个名词（最后做）

| 切片 | 内容 | 依赖 |
|---|---|---|
| X1 下线 `threads.*` | `threads.get/list` 与 `hyprial message threads` 标记弃用并指向 `conversations.*`；`ThreadAuthority` 的准入外壳并回 send-link；消息信封里的 thread 标记改为内部的「两端」键，旧记录兼容读取 | G1 合入（GUI 不再读 `threads.*`）；需要 `contract/` 变更与过渡期 |

放在最后的原因：`threads.*` 已对外暴露，信封与记录的键里都有 thread id，改动面最大，而它不妨碍前面任何一组。

### 组 4：群聊（等 P3 的结论与 #2712 的判断 harness）

范围先列出，P3 完成后再切片：

0. 多 owner 的 chat space（R13、R14）：按参与者所属的 owner 集合找到或创建 chat space；两个 owner 之间的 1:1 对话改为一份共享文档，传输仍走队列。它与群聊共用建 space、成员同步这部分机制，先做它可以在不改传输的前提下验证这部分。`hyprial-home` 改名为 chat space 涉及既有数据的迁移，也放在这一步。
1. 创建群：建 space、conversation 文档与成员表，得到 conversationId。
2. 发送：`message.send` 带群的 conversationId，发送方 daemon 核对成员资格后把消息追加到群文档。
3. 接收：成员的 daemon 发现群文档有新条目，为本机的成员在本地队列落行并唤醒。
4. 记录字段：`replyTo`、`mentions`；一条群消息在各方是同一个 id。
5. GUI 的群聊界面。

第 3 步唤醒全部 agent 成员（R9）。在 #2712 的判断 harness 实现之前，这意味着每条群消息都会让每个 agent 成员进入一个 turn，所以群聊排在它之后。

## 5. 并行关系

```
组 0   P1   P2   P3   K1   T1          五片同时
        │    │    │    │
组 1    S1   C1 U1 U2  K2              P2 合入后 C1/U1/U2 同时；S1 等 P1；K2 等 K1
        │    │  │
组 2    S2   G1 A1 H1                  G1/A1/H1 同时；S2 在 A1 之后
             │
组 3         X1
组 4   （P3 结论 + #2712 判断 harness）→ 群聊
```

同一时刻最多可以有五片在做。第一个对用户可见的结果是 U1 加 C1：agent 的主动消息进入用户的对话记录；GUI 上看到它要等 G1。

## 6. 等待 owner 的事项

| 事项 | 卡住什么 |
|---|---|
| 1:1 的传输暂不改，等 P3 的数据（D4） | 组 4；不影响组 0 至组 3 |
| Q6：11-16 的转述是否取代 11-08（书面确认） | G1 |
| B 线（K1、K2）与 #2712 S1 的先后 | K1、K2 |
| §8 的结束状态；G1 是否在本轮做出来（§8.5） | 开工 |

## 7. 执行方式（owner 2026-11-17）

原话：「我建议由你负责，使用子智能体并行开发，请和我明确最终交付物」

由负责本次收口的执行会话统筹，用子智能体并行开发，每个切片使用独立的 git worktree，与既有检出目录隔离。

## 8. 本轮结束时的状态【已确认：owner 2026-11-17「GUI 那一片纳入本轮，确认，开工」】

owner 2026-11-17：「除了 GUI 相关 PR，我建议你在规划的时候尽可能快地直接合入 dev，避免因为积压过久导致与 dev 脱节。但我要和你对齐的并非你要交付什么 PR，而是在本 session 结束的时候，功能/架构/用户体验上有什么改变」

合并方式（已裁）：非 GUI 的切片在门禁通过后由本会话直接合入 dev，不建集成分支，不积压。GUI 相关的 PR 只提交、不合并。若分支保护要求他人批准，本会话不绕过，如实报告。

### 8.1 用户体验

| 谁 | 本轮之后 | 说明 |
|---|---|---|
| GUI 用户 | **没有可见变化**，除非 G1 的 PR 被 GUI 线合入 | agent 的主动消息已经进入对话记录，但 GUI 仍按自己的 id 读旧接口，读不到。最初的问题在后端解决了，在界面上没有 |
| 飞书用户 | 不变 | 主动消息照常到飞书 DM（D3） |
| 用 CLI 或 `conversations.*` 查历史的人与 agent | 能看到 agent 发给用户的主动消息、发往外部群的消息 | 这两类此前不在任何本地记录里 |
| agent | 不带 conversationId 连续发给同一对象时，消息落在同一个对话里 | 此前每次各成一个新对话 |
| 查看对话列表的人与工具 | 列表里会出现 PAC 派单、hook 交换这类机制对话，带类别标记 | 展示方需要按类别过滤（R6）。现有 GUI 不读这个列表，不受影响 |

### 8.2 功能

| 变化 | 对应裁定 |
|---|---|
| 发给 `user:` 与 `route:` 的消息进入本地收件箱并被记录，外部投递在其后执行 | R4、R5 |
| 不传 conversationId 时归入两端的默认对话，不再产生随机 id | D1、D2 |
| PAC 派单、worker 回复、hook 交换、经 user-proxy 的消息被记录，记录带类别字段 | R6 |
| hook 交换归入 `_turn-hooks` 与该 agent 的一个对话，不再每次新建 | R6 |
| `graph`、`routine` 不再是 cell kind；OrgFS 侧的 PAC 图镜像删除 | R1、R2 |

### 8.3 架构

| 变化 | 说明 |
|---|---|
| 发送只有一个入口 | 所有目标都先进收件箱；adapter 从「替代路径」变成「投递端点」 |
| recorder 不再按消息类别决定是否记录 | 类别降为记录上的一个字段 |
| cell 的含义收窄 | kind 只剩 agent、department，以及作为存储类别保留的 conversation |
| 发送入口文件拆分 | `send.py` 从 1000 行的单文件拆为按目标类型分开的模块 |

### 8.4 本轮结束时**仍然没有**实现的裁定

| 裁定 | 状态 | 原因 |
|---|---|---|
| R3 取消 thread 这个名词 | 代码与接口里 thread 仍在 | X1 依赖 GUI 不再读 `threads.*`，且对外接口要过渡期 |
| R8 user 对 user | 未实现 | 两个 user 必然属于两个 owner，需要多 owner 的 chat space（组 4）。此前计划里的「同一 owner 内的 user 对 user」不成立，A1 移出本轮 |
| R13、R14 每个对话一份共享文档、按参与者划分 chat space | 代码未动（§8 初始快照） | 当时原以为「同一 owner 内已是一份文档」，经 P3 更正（§2.1）：同一 owner 记录实为各写入节点的 `rec-*` 分段且 `conversations.list/history` 读本地队列，尚无共享文档读模型（后续由 RM1 #2929 与 G1 #2936 补齐单 owner 读模型）；跨 owner 仍是各记一份，`hyprial-home` 也未改名 |
| R9、R11、R12 群聊 | 未实现 | 等 P3 的结论与 #2712 的判断 harness |
| GUI 显示主动消息 | 取决于 G1 | 见 8.5 |

### 8.5 G1 纳入本轮（已裁）

本轮做出 G1 的 PR 但不合并，交 GUI 线评审。它能否在真实 GUI 里验收，取决于本机能否起一个隔离的 daemon 加 GUI；做不到则如实报告 UNKNOWN。

### 8.6 做的顺序与中途结束时的保证

每片合入 dev 之后才开依赖它的下一片，所以任何时刻 dev 上的状态都是完整可用的。按价值排序：

1. P2（拆分）→ C1（默认对话）→ U1（`user:` 进收件箱）。到这一步，起因问题在后端解决。
2. U2、S1、H1。绕过清单里的其余路径。
3. K1、K2、T1、S2。名词与清理。
4. P1、P3 与以上并行，不占主线。
5. G1 的 PR（若纳入）。

该阶段非 GUI 1:1 后端切片数（上列第 1–4 步）由 13 变为 12（A1 移出；该统计口径不含第 5 步 G1、后续 §11.2 新增的 RM1、组 3 X1 以及 §17 纳入的群聊切片 GC1–GC6）。全量测试与 CI 的耗时没有量过，本轮能做到第几步无法预先保证；结束时逐片报告 PASS、FAIL、BLOCKED 或 UNKNOWN。

### 8.7 不做的事

- 不碰生产 daemon、Lark App 与凭据；真实环境验收（真实 daemon 之间收发、飞书、跨机器）报告为 BLOCKED。
- 不合并 GUI 相关的 PR。
- 不在本机现有的检出里开发。

## 9. 未验证的部分

- 除行数与目录文件数外，全部结论来自读代码，没有运行。P1 的作用就是把其中的推断变成测试。
- P2 的拆分边界是按 `send.py` 的分支结构估计的，没有试拆。
- OrgFS 的变更订阅能力没有读，组 4 的可行性取决于 P3。
- `contract/` 下哪些文件涉及 `message.send` 的默认行为与 `threads.*`，只看了目录名，没有逐个读。
- 线上是否存在 `graph`、`routine` cell 的数据没有核对。

## 10. 进度

当默认解释器为 free-threaded 3.14 时构建不了 `pycrdt`，所有命令显式使用 `--python 3.12`。全量 `pytest tests/` 有一万五千个以上的用例，五个切片同时在一台机器上跑不现实：各切片在本地跑焦点测试、结构门与 ruff，全量由 PR 上的 CI 承担，合并前以 CI 结果为准。

| 切片 | 状态 | PR | 备注 |
|---|---|---|---|
| P1 | PR 已开 | #2903 | 15 个特征测试，只在 `reference-abi` 后端下跑过。结论已回写讨论稿 §13.7 |
| P2 | Draft PR 已开 | #2902 | 拆分使一张架构豁免表多出两行，等 owner 裁定（PR 正文的选项 A、B）。干净的 worktree 需先构建原生引擎，否则依赖 OrgFS 的测试全部失败 |
| P3 | 完成，未推送 | | 探查稿已提交在本地分支 `docs/chat-space-group-probe`。含既有产品的权限相关发现，等 owner 决定是否进入 `docs/design/` 后再推送 |
| K1 | PR 已开，CI 红 | #2901 | 本地约 3200 个相关测试通过。CI 失败的门与他人无关 PR（#2893）上失败的是同一组，原因未查明（读不到 CI 日志） |
| T1 | PR 已开，CI 红 | #2900 | 同上；K1 合入后有三处「现状」要跟着改 |

### 10.1 P1 之后对计划的修正

- **飞书入站与对它的回复今天不被记录，计划里没有切片负责。** 这是用户与 agent 之间最主要的通道。需要让三段式 adapter 地址也得到对话归属；暂挂在 S1 名下，是否单列一片待定。
- **S1 的范围比原先写的大**：除类别判断外，还要改 `delivery/authority.py` 里按 intent 的过滤；并且要决定同一条消息的两行（收件箱行与发送方副本）在记录里是否去重。
- **一个未查明的现象**：P1 在测试夹具（内存版 OrgFS）里间歇观察到，同一轮里对同一个 conversation 的第二次追加之后，记录文档只剩一行，而 recorder 的索引显示两行都已记录并已发放正文释放许可。最小探针 900 次里出现 4 次。原因未知，是否在真实 OrgFS 上发生未知。正文释放许可依赖记录完好，所以它若是真问题，后果是正文丢失。是否登记进 `docs/reference/known-problems.md` 由 owner 决定。

### 10.2 一次测试绊线告警（2026-11-17）

P1 的一次早期测试运行以 `REAL USER SERVICE UNIT CHANGED: <user-service-unit-dir>/com.hyprial.hyprial.daemon.<hash>.plist` 结束。这条绊线在每次 pytest 会话的开始与结束各扫一遍真实用户目录下名字含 hyprial 的服务单元（只看名字与修改时间），有出入就报。此后约 30 次运行没有再出现。

记录者事后核对（只读，只看名字）：

- 各切片所基于的 dev（`787037c19`）只有一个固定的单元名 `com.hyprial.hyprial.daemon`，`src/` 里没有带哈希后缀的写法，所以这些 worktree 里的代码生成不了那个文件名。带哈希的命名与在途的 #2830（每个 HYPRIAL_HOME 一个系统服务）相符。据此推断它来自本机上别的会话或构建，不是本轮的测试；这一归因没有证实。
- 核对时真实目录下没有任何 `com.hyprial.hyprial.daemon*` 单元，launchd 里也没有加载这样的服务。
- 没有查明的：是谁创建的；它存在期间是否被加载过。

## 11. 本轮目标修订（2026-11-17，R15、R16 之后）

### 11.1 「GUI 呈现共享文档」在本轮的含义【提议】

今天 `conversations.*` 读的是本机投递队列，记录只用于取回正文（§2.1 的更正）。要让 GUI 呈现的是文档，需要把记录变成读模型：

- `conversations.history` 改为读取该 conversation 在 chat space 里的全部 `rec-*` 分段，按消息 id 去重（同一条消息的收件箱行与发送方副本合为一条），按（`createdAtMs`，消息 id）排序。`RecordEntry` 已有消息 id、发送者、接收者、intent、时间与正文，格式上够用。
- `conversations.list` 改为列出 chat space 里的 conversation 文档。
- GUI 读 `conversations.*`，并在记录有新条目时更新。

本轮的范围是**单个 owner 的 chat space**，即 GUI 最主要的情形：`user:<owner>` 与该 owner 的 agent 之间的对话。它不需要新的同步机制。

本轮**不含**跨 owner 的共享文档。它需要 P3 探查稿里的 CS0–CS4（供应方闸门、多 owner 的 space、不含 owner 的 conversation 键、定时补拉），其中两项要 owner 先裁定：跨 owner 的设备归属从哪里来；是否接受记录条目的作者没有签名。

由此带来的要求：

- **记录必须完整。** 读模型里没有的消息 GUI 就看不到。所以 U1、U2、S1（含飞书入站）是 G1 的前置，不再只是「补记录」。
- **被跳过的行要留占位。** D6 让脱敏拒绝与超大消息「已送达但不入记录」；记录成为读模型之后，这些消息会从 GUI 里消失。提议改为写入一条不含正文的占位条目，注明原因。
- **可见性有延迟。** recorder 在消息提交之后异步写入，GUI 看到一条消息要等 recorder 的这一轮。延迟的大小没有量过。
- **同一 owner 的多台设备**：owner-home 只在信任 tailnet 成员的模式下才在设备间同步（§2.1）。未开启时，每台设备的 GUI 只呈现本机写入的那部分。

### 11.2 切片的变化

| 切片 | 变化 |
|---|---|
| RM1 记录成为读模型（新） | `conversations.history/list` 从记录读取，去重与排序如上。文件：`conversations/read.py`、`conversations/bodies.py`、新增读取模块、`request_catalog/conversations/`。依赖 S1、U1 |
| S1 | 范围扩大：三段式 adapter 地址（飞书入站及其回复）得到对话归属；`delivery/authority.py` 里按 intent 的过滤；记录带类别字段 |
| S2 | 由「清理跳过条件」改为「被跳过的行写占位条目」，其余不变 |
| G1 | 依赖 RM1；GUI 订阅记录的变化 |
| P2 | 改用选项 B：只拆 `inbox_surface/send.py`，`delivery/sends.py` 不拆，架构豁免表不变。代价是 U1 与 U2 都要改 `delivery/sends.py`，两片串行 |

顺序：P2 → C1、U1（并行）→ U2 → S1 → RM1 → G1；K1 → K2、S2、H1 穿插。

### 11.3 合并受阻与本地 CI

- 用 `tea` 读到了 runner 日志。`pr-hygiene` 失败在 `uv run --with pytest` 取不到 `https://pypi.org/simple/pytest/`（连接失败，重试 3 次）；`cli-help-inventory` 失败在安装锁定依赖这一步。都是 runner 访问 PyPI 的问题，与各 PR 的改动无关。
- dev 的分支保护要求四个远端检查为绿：`gate-python / unit`、`gate-python / cli-help-inventory`、`gate-pr-declarations / Skill-Impact`、`gate-pr-hygiene / pr-hygiene`。它们红着时合并请求被服务器拒绝（HTTP 405）。本地 CI 通过不能替代它们。保护规则不适用于管理员，但用管理员强制合并等于绕过分支保护，本会话没有这样做。
- 本地 CI（`local-ci.sh`，在各切片的 worktree 里运行）：六个结构脚本、ruff、CLI 帮助清单、契约影响、Node 的 CI 策略测试、两组树级测试，再加与切片相关目录的 pytest，使用原生 actor 后端。
- 在远端恢复或 owner 另有指示之前，后续切片在本地叠放在已通过本地 CI 的前序切片之上继续开发，不等待合并。

## 12. GUI 绑定 user-proxy 之后（R17、R18；2026-11-17）

### 12.1 现状：GUI 聊天为什么能用【已核，来自 P1 与 U1 的测试】

| 方向 | 今天怎么走 | 需要用户端点吗 |
|---|---|---|
| 用户 → agent | GUI 以 `user:<owner>` 为发送者调用 `message.send`，进 agent 的收件箱 | 不需要 |
| agent 回复用户 | `message.reply` 把收件人为 `user:<owner>` 的一行放进发件队列。没有任何组件取走它；GUI 以 `user:<owner>` 查询历史时读到这一行。12 小时后这一行过期成死信，回复的 agent 收到超时通知，这条回复也从用户的历史里消失 | 不需要 |
| agent 主动发给用户 | `message.send` 走 user-delivery 分支，交给在线的 user-proxy、agent-bot 或 Squire。都没有时报 `TARGET_UNRESOLVED`，什么也不写 | 需要 |

即 GUI 聊天今天靠的是「回复留在队列里，GUI 自己来读」，不是送达。主动消息没有这条退路。

### 12.2 对计划的影响【提议】

- U1 不改收件箱引擎。发给 `user:<owner>` 的消息交给在线的 user-proxy，进的是它的收件箱。没有端点时失败且不写入对话（R18）。
- S1 成为前置：经 user-proxy 的消息现在记在 `dispatch-coordinator` 名下并被 recorder 跳过，要改为按真实发送者归入「agent 与 `user:<owner>`」的对话。
- agent 对用户的回复也应改走 user-proxy，不再留在发件队列里等过期。
- G1 的范围扩大：GUI 经 user-proxy 收发，可信身份的下发方式随之改变。

### 12.3 待 owner

- 用户没有 user-proxy 时，GUI 是自动创建一个，还是不可用。
- U1 已提交的三个提交（`feat/user-target-enters-inbox`）按 11-08 的前提实现，不合并，留作参考。

## 13. user 与 agent 的现状对照（2026-11-17 调研）

三份只读调研，基线 dev @ `88564fcf4`。标【复核】的由记录者对照源码确认，其余转引自调研，未逐条复核。

### 13.1 更正：GUI 的 conversationId

本稿与讨论稿多处写「GUI 用 `uuid5(sender, target)` 推导 conversationId」。这是错的。那段代码在 `shell/impl/gui/adapter/chat.py`，`src/` 与 `gui/src` 里没有任何地方构造这个适配器【复核】。真正在用的是 TypeScript 前端 `gui/src/features/chat-channels/impl/connected-chat.ts`：新对话的 id 是 `crypto.randomUUID()`，之后按目标记住【复核】；历史经 `message.history` 以 `user:<owner>` 读取，每 2 秒轮询一次。D2 相应改为：GUI 新建对话时不再自造 id。

### 13.2 身份

| 方面 | agent | user |
|---|---|---|
| 地址 | `agent:<owner>:<机器>:<a.id>` | `user:<owner>`，没有机器 |
| owner 段 | 登录名 | 同一个值，同一命名空间 |
| 记录 | `agents.sqlite3` 一行，有实体 id | 分散在 `users.json`、`users.sqlite3`、个人组织的 `receivers.json`、`settings.json` |
| cell | 每个 agent 一个 | 没有 |
| 运行体 | harness 可选；`agent.create` 不传 harness 时只创建身份【复核】 | 自己没有，靠 squire 或 user-proxy 这两个另外的 agent |
| 发送者认证 | 会话凭证加会话存活校验 | 本机 socket 上自称 `user:<owner>` 即被当作操作者【复核】，没有会话 |
| 创建 | `agent.create` | `hyprial login` 只写设置与密钥；档案与 squire 由 `hyprial squire setup` 另建 |

### 13.3 投递

| 步骤 | 发给 agent | 发给 user |
|---|---|---|
| 存储 | 发件队列、发送方副本、对方收件箱 | 不存 |
| 对方不在线 | 排队重试到过期 | 当场报错 |
| 是否要有进程 | 本机 agent 登记过即可，不需要 harness 在运行 | 需要在线的 user-proxy 或已注册的飞书 adapter |
| 消费者 | harness 取走并确认 | 飞书里的人，无确认 |

发给 `user:` 的顺序：发送方机器上的 agent-bot；否则经 Zenoh 交给拥有该用户的机器（等 3 秒回执），那里有在线 user-proxy 就以 `dispatch-coordinator` 的名义转成普通的 agent 投递，否则由 squire 的 adapter 发 DM。只因目标是 `user:` 而存在的代码约二十处。

### 13.4 三种界面

| 界面 | 发送身份 | 接收方式 |
|---|---|---|
| CLI | `--from user:<owner>`，人必须显式给出【复核】 | 以 `user:<owner>` 拉取 |
| GUI | 服务端注入的 `user:<owner>` | 以 `user:<owner>` 读历史，每 2 秒轮询 |
| 飞书 | `adapter:lark:<名字>`；人只出现在附带的元数据里 | 平台推送 |
| 飞书经 user-proxy | `dispatch-coordinator` 代 user-proxy agent | 发给 `user:` 的消息转给在线的 proxy，再转到 DM |

同一个人在 GUI 与 CLI 里是 `user:<owner>`，在飞书里是一个 adapter 地址或 proxy agent；三处的对话 id 各不相同。没有任何「界面端点」的现成概念。

## 14. 统一方案：user 就是一个 agent（R20、R21）

1. 每个用户有一个登记在册的「用户 agent」：有 id、收件箱与 cell，没有模型 harness。登录时创建，放在该用户的首选接收机器上。
2. `user:<owner>` 在 daemon 的入口解析成它的地址，与裸的 agent 名字走同一个解析器。之后的发送、接收、历史都是 agent 的现成路径。
3. GUI、CLI、飞书是这个 cell 的成员：以它的身份发送，读它的收件箱与对话。三个界面因此落在同一个对话里。
4. 没有登录过的用户解析不出地址，发送失败（R18）。登录过但没有打开任何界面时，消息留在收件箱里。
5. 旧消息记在 `user:<owner>` 名下，读取时把它作为第二个读取键；agent 改用 id 地址之后读旧名字下的消息，用的是同一种机制。

随之可以删除或并入的：`message.send` 里的 `user:` 分支、user-delivery 模块、agent-bot 直发、`dispatch-coordinator` 为用户转述的逻辑、squire 的兜底 DM、现有的 user-proxy 转发脚本（并入飞书界面）。

对切片的影响：U1 改为「`user:` 解析到用户 agent」，此前按旧前提做出的分支 `feat/user-target-enters-inbox` 作废；S1 不再处理为用户转述的流量；C1、RM1、G1 不变。

## 15. 决策记录：四段式地址为何本轮不退役（R22）

### 15.1 问题的由来

owner 在 2026-11-17 提出：既然 `user:<名字>` 与 `agent:<名字>` 都由 daemon 解析，是否不再需要四段式 URI。随后追问：为什么本轮不做。

### 15.2 四段里装的是什么

| 段 | 含义 | 性质 |
|---|---|---|
| owner | 归谁所有 | 用于权限判断，基本不变 |
| 机器 | 现在在哪台机器上 | 用于路由，会变 |
| actor | 实体的 id | 身份本身，不变 |

问题在第二段：位置被写进了身份。agent 换机器（仓库已有 teleport）时地址随之改变。此前把 actor 段换成 id、为旧名字地址保留读取别名，都是在处理这个问题的一部分。

结论：作为对外的地址形式，四段式不再必要；它承载的三件信息仍然需要，但不必拼成一个字符串。目标形态是对人和接口只用名字，身份是 id，owner 与所在机器是记录上的字段。

### 15.3 判断依据【已核，dev @ `88564fcf4`】

参照物是已经合入的「稳定 id 寻址」（`docs/design/uuid-addressing-handoff-2026-11-04.md`），它只把四段中的最后一段从名字换成 id：

- 从 2026-10-15 做到 2026-11-07，中途交接一次。
- 单向迁移，没有兼容窗；升级后唯一支持的降级是还原升级前的备份。
- 需要备份的存储有七处：`agents.sqlite3`、`lifecycle-operations.sqlite3`、`adapters.sqlite3`、`routines.sqlite3`、`pac-graph.sqlite3`、组织权限文件、`agents/` 目录。
- 带来两处破坏性的契约变更；真实环境的端到端验收当时为未跑。

当前的使用范围：

| 项 | 数量 |
|---|---|
| `parse_agent_uri` | 73 个源文件，188 处 |
| `canonical_user_uri` | 50 个源文件，127 处 |
| `contract/` 下写有四段地址的文件 | 20 个 |
| `tests/` 下写有四段地址的文件 | 563 个 |

### 15.4 不在本轮做的原因

1. 它改的是落盘数据与节点之间传输的内容。收件箱按地址字符串分格，链路文档与历史记录里存的是四段地址。需要迁移方案与升级顺序。
2. 去掉机器段之前要先回答「怎么知道往哪送」。现在看地址即可；去掉之后要查登记表或在线状态，断网时查不到。「失联时节点仍能自行判断」是仓库的第一原则，这需要一个设计，目前没有。
3. 本会话无法验证它。这类改动要在多个真实 daemon 之间、跨版本验证；本会话只能跑单机测试，且不接触真实 daemon。
4. 本轮的目标（主动消息进入 GUI、GUI 呈现共享文档）尚未交付。先换地址，这两件事都要在新地址上重做。

第 1、2 条是实质原因，第 3 条是执行者的限制，第 4 条是优先级判断。记录者第一次回答「本轮不做」时没有量过工作量，上述数字是 owner 追问之后才核对的。

### 15.5 三步走

| 步骤 | 内容 | 时机 |
|---|---|---|
| 1 | 对外只用名字：`agent:<名字>`、`user:<名字>` 都由 daemon 解析；用户成为一个 agent | 本轮 |
| 2 | 本轮新写的代码不依赖四段形式，默认对话按稳定 `endpoint_key` 派生（`a.<id>` 形态的 agent URI 取实体 id） | 本轮。已落实一处：C1 的默认对话 id 按 `endpoint_key`（`conversations/ids.py`）派生，对 `a.<id>` 形态的 agent URI 取其实体 id（agent 换机器时默认对话不分裂）；`user:<owner>`、`adapter:`、`route:` 及未解析名称仍按现行规范地址/原串定键，user-agent 的实体 id 键迁移留待第 3 步统一评估 |
| 3 | 存储与节点间传输改为以 id 为键，owner 与机器成为记录字段，四段字符串退役 | 单独立项；需要迁移方案、路由在失联时的规则、真实环境验收 |

前两步完成后，四段地址只剩内部路由字符串这一个角色。

### 15.6 重新评估第 3 步时要看的东西

- 本节 15.3 的两张表是否仍然成立。
- `docs/design/uuid-addressing-implementation-2026-11-03.md` 的规格与待裁问题 Q1–Q4，以及 `contract/identity/agent-addressing.md`。
- teleport 的现状（`docs/design/agent-teleport-*.md`）：它已经面对「位置变化」的问题，可能已有可复用的做法。

## 16. 2026-11-17 下午的状态

合入 dev：T1（#2900）、P1（#2903）、P2（#2902）、K1（#2901）。四个 PR 都是在本地 CI 通过后强制合并的（R19）；当时远端四个必需检查因 runner 取不到 PyPI 而失败。

进行中（各一个子智能体，各自的 worktree，基线 dev @ `c34c8b0f8`）：

| 切片 | 内容 |
|---|---|
| C1 | 默认对话；id 由实体 id 派生 |
| UA1 | 创建用户 agent（只做身份，不改收发） |
| S1 | 机制流量与飞书入站进入记录，带类别字段 |
| K2 | 移除 `routine` cell kind |
| P3 | 探查稿改写为可入库的版本 |

之后的顺序：UA2（`user:` 解析到用户 agent）→ U2 → RM1 → G1；S2、H1、T2、UA3 穿插。

#2878（GUI 聊天页的呈现调整）已对照本稿检查：只改呈现，不动数据层与 daemon，与本稿一致；建议先合并，G1 在它之上修改。

## 17. 群聊纳入本轮（R25）

### 17.1 范围【提议】

先做**单个 owner 内的群聊**：成员是这个 owner 的 agent 与他的用户 agent。它不需要跨 owner 的 space，也就不依赖探查稿 §5 的访问控制评估。跨 owner 的群聊在评估之后另做。

### 17.2 接口【提议】

owner 2026-11-17 问：是否需要新的 MethodSpec 来支持群聊的创建，例如 CLI 上的 `hyprial chat create`。需要。群聊必须先被创建才有 conversationId 与成员表，现有接口里没有这一步。

daemon 的方法放在已有的 `conversations.*` 之下，与 `conversations.list/history` 并列，名词保持 conversation：

| 方法 | 作用 |
|---|---|
| `conversations.create` | 建群：建 space、写成员表、返回 conversationId |
| `conversations.get` | 读取一个对话的标题、成员与类型 |
| `conversations.members.add` / `.remove` | 增减成员 |
| `message.send` | 不变。带群的 conversationId 发送即为群消息，接收者由成员表决定 |

CLI 增加面向人的一组命令，是上述方法的薄封装：`hyprial chat create`、`chat list`、`chat show`、`chat send`、`chat add`、`chat remove`。

### 17.3 切片

| 切片 | 内容 | 依赖 |
|---|---|---|
| GC1 | `conversations.create/get/members.*` 与 `hyprial chat` 命令；群的 space 与成员文档 | UA1、C1 |
| GC2 | 群消息的记录条目：以消息 id 定键，增加 `replyTo`、`mentions` | S1 |
| GC3 | 发送：带群的 conversationId 时核对成员资格，由 daemon 追加一条到群文档 | GC1、GC2、UA2 |
| GC4 | 接收：发现群文档的新条目，为本机的每个成员落收件箱行并唤醒，发送者自己除外；重启不重复投递 | GC3 |
| GC5 | GUI 的群聊界面（只提 PR，不合并） | GC4、RM1、G1 |
| GC6 | 使用说明：如何建群、拉人、发消息、@ 与引用 | GC4 |

### 17.4 一个必须先处理的风险：agent 之间互相唤醒

R9 规定群消息唤醒全部 agent 成员，是否处理由 cell 内的 handler 决定。这个 handler（#2712 的判断 harness）还不存在。没有它，一个 agent 在群里说一句话会唤醒其余所有 agent，它们的回复又互相唤醒，没有终止条件，token 消耗不可控。

提议在判断 harness 落地之前，由 daemon 提供一个内置的临时规则充当 handler：群消息照常投递给全部成员并进入各自的收件箱；只有在消息来自人、或消息 @ 了该 agent、或引用了该 agent 的消息时，才让它进入一个 turn。其余情况只入收件箱，不唤醒。这是对 R9 的临时实现，判断 harness 落地后由它取代。


## 18. 2026-11-18 的裁定：用户 agent 的位置、去机器字段的约束、群聊唤醒

### 18.1 裁定

- **R26 用户 agent 按方案 A**：每台登录了的机器各有一个用户 agent；daemon 把本机 owner 的 `user:<owner>` 解析为**本机**的那个实例，就地投递。可见性由对话文档的同步保证，不依赖某一台机器在线。被否决的方案 B：每个用户只有一个、放在首选接收设备上（需要首次登录认领流程；那台机器离线时送不到）。
- **R27 机器字段不是身份**：owner 指出这个问题对普通 agent 同样存在；agent 现在带地址，所以代表人的 agent 也带地址是合理的，但后续会把地址里的机器字段去掉，由网络自动寻址。设计必须为此留好位置：
  - 同一 owner 在各机器上的用户 agent 是**同一个逻辑参与者**的多个实例，身份 = owner + 保留的角色/名字。各机器使用同一个保留名；去掉机器字段后地址应当自然合并。
  - 任何持久化的东西（默认对话 id、对话归属、记录里对外呈现的 `from`/`to`、成员名单）都不得把用户 agent 的机器字段当作身份。用户端的对话 key 保持 `user:<owner>`，与 dev 现状一致，各机器算出同一个默认对话。
  - 「是不是人发的」「是不是我发的」按角色/名字判断，不按地址相等判断。
  - 发给另一台机器上用户 agent 实例的回复，按名字解析后就地投递到本机实例，不路由到（可能离线的）来源机器。
  - 不写「用户 agent 地址」的跨机器复制记录（原 UA1 写进 `receivers` 的做法作废，同时消除了新旧版本混跑的隐患）。
- **R28 群聊的临时唤醒规则**（owner 确认）：群里只有来自人的消息、@ 到该 agent 的消息、回复该 agent 的消息会唤醒它；#2712 的判断机制完成后替换。

### 18.2 对切片的影响

- UA1 重切为 UA1b（分支 `feat/user-agent-per-machine`）：沿用模板、幂等创建、保留名、销毁/改名保护、恢复与 worker 选择中的排除；去掉 `receivers` 相关改动与跨机器查找；「第二台机器不再创建」反转为「每台机器各建一个」。原分支 `feat/user-agent-identity` 不合并。
- UA2 先做只读探查（现有 `user:` 发送路径、零 harness agent 的收件箱由谁消费、Lark 推送如何并存、何处做归一化），再定稿实施。
- GUI（#2936）以 `from === user:<owner>` 判断自己的消息：只要记录对外呈现的仍是 `user:<owner>`，该 PR 无需跟进。

### 18.3 本轮已合入 dev 的切片（截至 2026-11-18）

T1 #2900、P1 #2903、P2 #2902、K1 #2901、P3 #2919、K2 #2921、S1 #2922、C1 #2923、T2 #2926、H1 #2928、RM1 #2929、G0 #2930、S2 #2931（占位条目）、E1 #2934（契约示例对齐）、HS1 #2935（同一进程只建一个 home），以及测试修复 #2932。G1 为 #2936，交 GUI 线评审。

### 18.4 GUI 线的反馈与 GC5 的归属（2026-11-18）

- **R29 GC5（群聊界面）由 GUI 线做**（owner 裁定）。后端线负责契约、示例与 CLI；本轮后端线交付的是 CLI 上可用的群聊。
- GUI 线比较的是「记录里用户一端的字符串」与「会话主体的 actor」逐字相等；写进契约说明。
- 用户 agent：不出现在 `targets` 的 agent 结果里；角色标记用新的专用字段，不复用 `role`；每个实例带同一个 `user:<名字>` 作合并键；生命周期方法由 daemon 以稳定错误码拒绝。与用户 agent 同一批合入。
- 群聊接口的五项约束：群的列表条目不得让已发布的 GUI 整页失败（带 `kind`，仅在请求方声明支持时返回）；明确群发送的 `to`；成员显示名与角色标记由服务端给；历史条目附被回复消息的作者与摘要；成员变动为系统条目、拒绝给稳定错误码。合入时同步 Contract Mock 示例，并让第一次发送在示例里走通。
- 去掉机器字段前要通知 GUI 线（GUI 有一处本地存储键用到机器字段）。

### 18.5 更正与对齐（2026-11-18 晚）

- **更正 R26 的附带说法。** R26（每台机器一个用户 agent，`user:<owner>` 解析到本机实例）不变；但「可见性由对话文档的同步保证」今天不成立。GC0（#2947）用测试钉住：同一 owner 的第二台机器只有显式加入之后才持有第一台的 home，没有任何自动加入；两台机器各自建的 home 永远各看各的。§2.1 与 §11.1 里「owner home 在设备间同步」的说法以此为准。
- **本轮按单机交付**（owner，2026-11-18）：agent 主动消息与群聊在一台机器内成立；多机之间 home 的收敛（自动加入、丢失更新的修复）不在本轮。把 home 纳入 OrgFS 周期同步经实测不可取（每轮重发整个空间，开销随历史线性增长，#2949），不做。
- **与 org 线（#2883）的对齐。** org 线已裁定取消个人 org（J10）、多台设备不自动互通、要互通就建普通 org 并给自己发邀请（J13）。这与 GC0 钉住的现状一致，conversation 线不另设「同一主人的设备自动互通」。conversation 线本轮新增的代码不得新建对个人 org 的依赖：UA2 里「确认没有推送渠道」读的是个人 org 里的 receivers 记录，属于过渡写法，集中在一处并注明随 W1f 删除；个人 org 不存在时它退化为「没有证据」分支，行为仍然正确。
- **默认 observe 模式下不带会话凭据的本机请求可以以任意 agent 的名义发送（含用户 agent）**：owner（2026-11-18）定本轮不动，属于发送方认证那条线。
- **群聊设计的决定**（owner，2026-11-18）：本轮的目的是清理和理顺 conversation，不添加复杂机制。群文档放在 owner 的 chat space；收件箱行经投递传输层的接收端进入；不该唤醒的成员不生成行（cell 今天只是文档，没有「送达但不唤醒 harness」的决定点，归 #2712）；群发送不申请发送许可；agent 之间互相回复不设上限；只有 owner 能增减成员；群聊界面由 GUI 线做。

2026-11-20 状态（对上面 18.4–18.5 的补注；正文保持 2026-11-18 的原样）：

- 此后已合入 dev：群聊后端的 #2968（群发送、历史与回复）、#2969（`hyprial message` 下的群聊命令）、#2972（群消息投递给成员 agent）；E2E-031 #2979；R1 #2970；R2 #2989；R4 #2991；UA4 #2992；群发送走 HTTP #3007；冻结 agent 名账本删除 #3006；节点 id 从 home 的 node-id 文件读取 #3008；契约示例里的一条完整群聊故事 #3016。
- 「机器字段不是身份」与「每台机器一个用户 agent」在 #2992 之后的状态以 dev 上的代码与 `GLOSSARY.md` 为准，上文 R26/R27 中与之不一致的说法已被取代。
- 未能在 dev 的合并记录里确认编号的项：GC1–GC5 各自的 PR 编号（GC5 按 R29 由 GUI 线做）、R3 #3002（尚未见合入）。这些不在上面的清单里。

2026-11-22 收口附注（正文中上述历史段落保持当时记录口径，仅做脱敏处理）：

- 本轮 conversation 线余下切片及后续收口 PR 已全部合入 `dev`，包括：GC1（#2956）、GC2（#2961）、IM1（#2955）、G2（#2964）、R3（#3002）、GC-B/U1/U2/U3/U4（#3036/#3038/#3040/#3041/#3045）、GC4-M（#3046）、G11（#3026）、S1–S6 浏览器验收与 Mock 回显（#3050/#3053/#3062/#3081/#3084/#3088）以及 GC-R（#3054）。
- **对历史裁定 R12 与 R28 的替代关系**：早期 §1 R12、§2 D4（群聊行）及 §4 组 4 第 3 步所述的「成员从独立群 space 同步后再本地投递」已由 2026-11-18 决定（§18.5）及已合入的 GC1–GC4（#2956/#2968/#2972）正式取代；当前 `dev` 将群文档存放在 owner 的 chat space 中，并由本地投递传输层接收端写入被唤醒成员的收件箱行（`app._local_delivery.deliver` → `inbox.receive`，见 `src/hyprial/daemon/impl/application/messaging/inbox_surface/groups/delivery.py`）。另，R28 的临时唤醒规则已由 2026-11-21 裁定与 GC4-M（#3046）、GC-R（#3054）进一步收敛为仅唤醒显式 `@` 提及的 agent 成员（群回复默认 `@` 被回复消息作者）。
- 本两份文档作为本轮设计与裁定依据已完成脱敏归档，不涉及内部人员、机器、会话或环境细节；最终实现以 `dev` 代码与 [GLOSSARY.md](../../GLOSSARY.md) 为准。
