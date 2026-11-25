# chat space 与群聊探查：OrgFS 现状、同步延迟与切片提议（P3）

- 日期：2026-11-17
- 状态：**探查结论与设计提议，待 owner 审阅**。没有改动产品代码
- 记录：Claude Code（owner 的本机会话及其 P3 子智能体，非注册 agent）
- 读数基线：`origin/dev` @ `787037c19`
- 依据：conversation 收口实施计划（2026-11-17）§1 的 R7、R9、R11–R14 与 §2 的 D4；讨论稿（2026-11-16）§8、§14；本稿写成后 owner 追加的三项裁定（§0.1）
- 标注：【已核】= 读了代码，带 `文件:行`；【已测】= 在隔离环境运行得到（方法见 §4.2）；【推断】= 由已核事实推出，没有运行验证
- 路径省略 `src/hyprial/daemon/impl/`；`orgfs/ops/` 指 `orgfs/document/operations/`
- 本稿只保留统计与分析逻辑。与访问控制有关的若干问题只记「存在、需另行评估」，不写机制（§5）

## 0. 结论

1. **space 的成员是 owner，不是设备，也不是 agent。** 计划 §2.1 的这条假设成立。成员增减只有 space 的 owner 能做，owner 字段不可变。权限只到 space 一级，没有按文档或路径的权限。
2. **实时同步是推送，本机回环上写入到对端 watch 回调的 p50 约 30 ms、p95 约 32–48 ms。** 延迟不构成 1:1 改用文档同步的障碍。
3. **构成障碍的是可靠性。** 广播发出即不管，接收入口有容量上限、满了丢弃，实时路径上没有缺口修复。补拉只在对端上线、收到 announce、传输重建时发生；定时补拉只覆盖 org 目录与 ACL space，不覆盖 `hyprial-home`，也不会覆盖新的 chat space。双方都在线时丢掉的一条更新，要等到下一次这类事件才补上。（2026-11-18 更正：把 home 加进定时补拉并不便宜，每轮重发整个 space 的内容，见 `chat-space-two-node-probe-2026-11-18.md` §3.1。）
4. **默认部署下，org 目录以外的 space 可以跨节点同步。** 这依赖开关 `forwarding.tailnet.trustMembersForUpperLayers`（默认开）。开关关闭时，测试中对端加入被拒绝。
5. **现有记录不是「一份文档」，也不是读模型。** 它是每个写入节点各自追加的分段文件，目前只用于按引用取回正文；`conversations.*` 的列表与历史读的是本地队列。群聊需要先把它变成可读、可被对端消费的记录。
6. **conversation cell 的 id 与校验都绑定单一 owner。** 两个 owner 按现有公式会得到两个不同的目录，共享文档必须换一个不含 owner 的键。
7. **跨 owner 的 space 上线之前，需要先做一次访问控制评估**（§5）。

### 0.1 本稿写成后的裁定（owner，2026-11-17）

| 裁定 | 对本稿的影响 |
|---|---|
| user 是一个 agent，它的成员是各个客户端界面（GUI、CLI、飞书）；`user:<owner>` 解析到这个 agent | 群的 actor 层成员表里只有 agent，其中包括 user agent（§7） |
| 本轮 GUI 的目标只覆盖单个 owner 的 chat space | §9.1、§9.2 的切片都不在本轮；本轮相关的只有 §6 |
| 目标方向（不在本轮）：消息正文只存在 conversation 文档里，投递队列只带通知与引用 | 见 §8.1 |

## 1. 成员模型（问题 1）

| 事项 | 现状 | 依据 |
|---|---|---|
| 成员是什么 | 一个 principal 字符串。daemon 用自己的 owner（`user:<owner>`）作为 OrgFS 的 `author`，一个 daemon 上的全部 agent 以同一个 owner 身份写入 | 【已核】`orgfs/api.py:95-100`、`application/messaging/orgfs_bridge.py:221-227` |
| 角色 | owner（唯一、创建者）；成员模式 `ro` 或 `rw` | 【已核】`orgfs/api.py:19`、`orgfs/ops/spaces.py:61-121` |
| 谁能增减成员 | 只有 space 的 owner。`invite`、`remove_member`、`update_space_meta` 都先过 `require_space_owner` | 【已核】`orgfs/api.py:38-42`、`orgfs/ops/spaces.py:153,180,214` |
| owner 能否变更 | 不能。`name`、`owner`、`createdAt`、`spaceId` 不可改 | 【已核】`orgfs/ops/spaces.py:162` |
| 成员如何传播 | 成员表是 space 的 `meta` 文档里的一个 map，随其他文档一起同步；对端应用后重建本地成员集 | 【已核】`orgfs/ops/spaces.py:85-108,430-455` |
| 写权限 | 每个副本独立判定：作者是 owner，或在该次写入所见的成员表里是未移除的 `rw` 成员 | 【已核】`orgfs/storage/store/admission.py:531-535` |
| 移除后 | 被移除者此后的写入一律拒收（移除优先）；已经同步到它本地的内容收不回 | 【已核】`admission.py:513-522`；后半句【推断】 |
| 权限粒度 | 只到 space。仅 org 目录 space 有额外规则，不适用于其他 space | 【已核】`admission.py:537-552` |
| 读权限与来源核对 | 见 §5，本稿不展开 | — |

agent 不是成员，这意味着群的「成员表」有两层：space 成员（owner，决定谁能同步与读写）和群成员（agent，含 user agent，决定投递给谁）。后一层 OrgFS 不提供。

## 2. 创建 space（问题 2）

| 事项 | 现状 | 依据 |
|---|---|---|
| 谁能建 | 任何 daemon；创建者成为 owner 和第一个 `rw` 成员 | 【已核】`orgfs/ops/spaces.py:61-121` |
| 步骤 | 生成 space id（可指定，必须是 UUID）与 tree 文档，一次提交写入 meta | 【已核】`orgfs/ops/spaces.py:64,96-116` |
| 成本 | 建 space 约 11 ms；写标记约 5 ms；邀请约 5 ms；对端加入约 33 ms | 【已测】 |
| 常驻成本 | 每个 space 一个本地库、一个 mesh（1 个工作线程、1 个 actor、1 条 effect lane、3–4 个网络注册）。测得每多一个 space 多约 4 个线程（`reference-abi` 后端下的数字，native 后端没有测） | 【已核】`orgfs/mesh.py:185-256`；【已测】 |
| 数量上限 | 没有硬上限。节点的每次 announce 列出它所在的全部 space；对离线节点缓存的 announce 每条最多保留 256 个 space | 【已核】`orgfs/composition/directory.py:809-828,905,930`、`orgfs/network/protocol.py:30` |
| 成员如何得知并加入 | 没有自动加入。`join` 需要 space id 和一个在线的持有节点 | 【已核】`orgfs/composition/lifecycle.py:639-656` |

**`hyprial-home` 的查找与竞态**【已核】`composition/cells/spaces.py:8-22,83-119`：

- 只探查名字为 `hyprial-home` 的 space，再读 meta 里的 `kind = hyprial-owner-home`；owner 就是 space 自己的 owner。
- 同一 owner 的两台设备各建了一个时：读取取并集，写入取 space id 最小的那个，两边收敛到同一个写入目标。
- 建完 space、写标记之前崩溃：留下没有标记的 `hyprial-home`，下次补上标记并沿用，不再新建。
- 只为本机的 author 创建，只在写入需要时创建。

**「为 owner 集合 {X, Y} 找到或创建 chat space」**【推断】：

- owner-home 的收敛之所以成立，是因为两个 space 的 owner 相同、两台设备都是成员。{X, Y} 的情况不同：X 建的 space 归 X，只有 X 能邀请 Y；Y 同时建的归 Y。两个 space 并存，双方要各自加入对方的那个，才能套用「读并集、写最小 id」。
- 不能用「由 owner 集合派生固定 space id、谁先到谁建」。两边各写一份 meta 创世提交，后到的创世在已有 meta 提交的副本上被拒收【已核】`admission.py:468-486`，两份永不合并。
- 对端需要 space id 才能加入。1:1 的传输仍走队列（D4），所以 space id 可以随第一条消息的信封带过去，由接收方 daemon 加入。

## 3. 变更观察（问题 3）

| 途径 | 性质 | 粒度 | 依据 |
|---|---|---|---|
| `fs.watch(space, glob, callback)` | 进程内推送。本地写入与对端更新被应用后都会触发 | space 加路径通配；每个受影响的节点一个事件，带路径与版本，不带内容差异 | 【已核】`orgfs/api.py:314-316`、`orgfs/ops/content.py:643-651`、`orgfs/ops/spaces.py:596-646`、`orgfs/document/projection.py:833-844` |
| `fs.watch_meta(space, callback)` | 进程内推送；meta（含成员表）变化时触发 | space | 【已核】`orgfs/ops/spaces.py:138-144,648-649` |
| `runtime.watch_events` | 轮询：返回某版本之后的事件 | 同上 | 【已核】`orgfs/composition/lifecycle.py:678-689` |
| cell 运行时 | 每 5 秒全量扫描一次目录，不用 watch | 目录 | 【已核】`cells/runtime/service.py:97,325-336` |

重启与漏通知：

- 事件列表只在内存里，没有上限裁剪，进程退出即清空【已核】`orgfs/docs.py:171`、`projection.py:838-839`。
- 节点的 `version` 不能跨重启使用。测试中同一个文档重启前后的版本不同，`changes_since` 重启后返回 0 条（重启前 560 条）【已测】。
- 因此接收方不能依赖 watch 或版本做持久游标。watch 只能当作「去看一眼」的信号，游标要自己按条目键持久化。

## 4. 同步路径与延迟（问题 4）

### 4.1 路径【已核】

| 阶段 | 做法 | 依据 |
|---|---|---|
| 实时 | 本地提交后，作为提交后效果逐条发布日志记录；成员节点订阅该 space 的日志。没有批量与去抖 | `orgfs/network/ingress.py:202-230` |
| 入口背压 | 每个 space 的入口队列容量 128，满了直接丢弃并计数 | `orgfs/network/protocol.py:59` |
| 缺口 | 实时路径上没有「序号缺口触发补拉」 | `orgfs/network/recovery.py:869-874` |
| 跨节点同步的前提 | 默认部署下，org 目录以外的 space 可以跨节点同步，依赖开关 `forwarding.tailnet.trustMembersForUpperLayers`：默认开，在 daemon 随 tailcat 边车运行、入站监听全部是受管私有监听时生效 | `forwarding/membership/settings.py:42,77-83` |
| 补拉的触发 | 对端上线、收到对端 announce、传输重建完成。这三项对所有 space 生效 | `orgfs/composition/directory.py:279-327,352-367,380-421` |
| 定时补拉 | 每 60 秒（±20% 抖动）一次，**只针对 org 目录与 ACL space**（2026-11-18 更正：扩到 home 并不便宜，见 `chat-space-two-node-probe-2026-11-18.md` §3.1） | `directory.py:438-500`、`protocol.py:45-48`、`application/messaging/maintenance.py:675` |
| 补拉的限额 | 单次尝试 2 秒、总预算 4.5 秒；连续 3 次失败后按 5 秒起、翻倍、上限 120 秒暂缓 | `protocol.py:37-42,54-55` |
| 正文大小 | 文本上限 4 MiB；更新超过 512 000 字节改走 blob | `orgfs/document/model.py:47`、`orgfs/storage/store/vocabulary.py:34-36` |

### 4.2 测量【已测】

方法：仿照 `tests/daemon/orgfs/mesh/test_orgfs_p1_integration.py` 的搭法，在临时目录里起两个 `OrgFsRuntime`（author 为 `user:x`、`user:y`），经本机回环的 Zenoh 相连，开关按默认值打开。在 y 上注册 watch，x 写入，用 `perf_counter` 取「x 调用写入」到「y 的 watch 回调」的间隔。脚本没有入库。

| 场景 | N | p50 | p95 | 最大 |
|---|---|---|---|---|
| 新建小文档 | 200 | 31 ms | 48 ms | 224 ms |
| 按记录分段的方式追加一行（整份重写、带版本期望），分段增长到 82 KB | 200 | 29 ms | 32 ms | 39 ms |
| 同上，分段约 250 KB（接近 256 KB 的换段阈值） | 20 | 34 ms | 36 ms | 39 ms |
| 仅 x 本地写入调用的耗时（space 内已有约 400 个节点） | 200 | 41 ms | 59 ms | 152 ms |

其他观察：

- 本地写入调用（41 ms）比前几行的端到端（约 30 ms）还慢，原因是测到这一行时 space 里已有约 400 个节点，而前几行开始时只有几个；本地写入的耗时随 space 内节点数增长。这只是一次观察，没有单独做增长曲线。
- 离线补拉：y 离线时 x 写入一个文档，y 重启。按真实 daemon 的方式接入「对端上线」事件（`application/wiring/transport.py:508-510`；搭法取自 `tests/daemon/orgfs/mesh/replication/support.py`）时，y 从重新绑定传输到读到该文档用了 36、77、112 ms（3 次）。不接入这个事件时 15 秒内读不到，说明补拉只由 §4.1 列的事件触发。
- 开关关闭时，y 加入 x 建的 space 被拒绝。这不是默认部署的行为。

局限：同一台机器、回环网络、两个节点、双方在线；actor 后端用的是 `reference-abi`（本机没有编译 native 扩展，生产默认是 native）；另有四个智能体在同一台机器上并行工作。跨机器、经 tailnet、多成员、大量 space 的数字**没有测**，需要两台机器上的隔离 daemon 才能得到。

### 4.3 对 D4 的含义

延迟本身够用，重启后的补拉也快。1:1 若改由文档同步送达，缺的是队列现在提供的东西：送达确认、重试、过期、离线暂存；另外缺双方在线时丢失更新的补救（缺口检测或定时补拉）。hook 这类带超时的问答依赖这些保证。建议 D4 维持现状；是否改由 owner 决定。

## 5. 文档当作传输时的准入（问题 5）

R7 要求闸门由 daemon 代码执行。文档当作传输时，闸门可以放在三处：

| 位置 | 执行什么 | 对谁有效 |
|---|---|---|
| 发送方 daemon，`message.send` 内 | 发送者是群成员、send-link 与预算、脱敏；然后由 daemon 追加条目 | 经 `message.send` 的调用方 |
| OrgFS 副本（已有） | 写入者所属的 owner 是该 space 未被移除的 `rw` 成员 | 所有节点 |
| 接收方 daemon，本地投递之前 | 条目的写入者是当前成员；条目的 sender 属于写入者的 owner 且在群成员表里；本机收件人的本地策略（屏蔽、预算） | 本机的收件人 |

建议发送侧与接收侧都设。接收侧可以复用本地接收里已有的 send-link 入站检查（§8）。

**需要单独做的评估。** 跨 owner 的 space 上线之前，要先做一次访问控制评估并给出结论。评估的范围与结果另行记录，不写入本稿。

与评估结果无关、设计上就做不到的两点【推断】：对别的 owner 的 daemon 无法强制发送侧限流，只能在接收侧丢弃，丢弃后条目仍留在文档里；已同步的条目无法撤回，被移除的成员保有已同步的内容。

## 6. `hyprial-home` 改为 chat space（问题 6）

引用名字或标记的位置【已核】：

| 位置 | 内容 |
|---|---|
| `composition/cells/spaces.py:35-36` | 常量 `OWNER_HOME_KIND`、`OWNER_HOME_NAME` 与全部查找、创建逻辑 |
| `composition/cells/__init__.py:5-6,18-19` | 导出上述常量 |
| `cells/ports.py:43`、`cells/codec.py:147-153` | space 角色 `owner-home`；owner 范围的 cell 要求 space 的 owner 等于 cell 的 authority |
| `composition/conversations/placement.py:30-67`、`wiring.py:159,207` | `OwnerHomePlacement`：记录写到哪个 space |
| `conversations/record/segments.py:109` | 错误码 `no-owner-home-space` |
| `GLOSSARY.md:83` | 术语「owner-home 空间」 |
| `tests/daemon/cells/model/test_cell_codec.py`、`tests/daemon/cells/runtime/test_cell_persistence.py` 等 | 测试 |
| `contract/` | 没有引用这个 space 名或标记 |

既有数据与迁移：

- space 的名字不可改（§1），「改名」只能是新建一个 space 再搬数据，或者不搬。
- 已发放的正文引用里写着 space id 与路径（`rec1 <space> <path> <key>`，`conversations/record/contracts.py:17-19,84-97`），存在各节点本地的队列库里。搬走旧数据会让这些引用失效【推断】。
- owner-home 里除了 conversation 记录，还有其他 owner 范围的 cell 文档（现在是 `conversation` 与 `graph` 两种，`cells/model.py:48-52,344`）。K1 删除 `graph` 之后只剩 conversation，R14「owner-home 不再是单独概念」才完全成立【推断】。

建议：**不迁移，新旧并存。** 查找规则同时认旧的（名字 `hyprial-home`、标记 `hyprial-owner-home`）与新的标记；只有一个 owner 的 chat space 继续使用已有的 `hyprial-home`，新建时才用新名字。读取取并集、写入取最小 id 的规则不变。本轮 GUI 只覆盖单个 owner 的 chat space，按这个建议不需要任何数据改动。

## 7. 群聊的机制（问题 7）

**成员表放在哪。** 两层：

| 层 | 存放 | 谁能改 |
|---|---|---|
| owner（谁能同步、读写） | space 的成员表 | 只有 space 的 owner，也就是建群者所属的 owner |
| agent（投递给谁、唤醒谁；含 user agent） | 群 space 里的一份成员文档 | OrgFS 不区分，需要约定权威并在读取时核对写入者 |

人以 user agent 的身份入群；消息到达 user agent 之后分发到它的哪些客户端界面，是 user agent 内部的事，不进群的成员表。

加入一个 agent：它的 owner 不在 space 里时，先由 space owner 的 daemon 邀请该 owner，再把 agent 写进成员文档。移除一个 agent：改成员文档；该 owner 名下不再有群成员时，从 space 移除这个 owner。后果【推断】：加人、减人都要建群者 owner 的 daemon 在线；群不能转让。讨论稿 §14.4 说成员表「不需要指定以谁的那份为准」，这一点对 agent 层可以成立，对 owner 层不成立。

**记录条目的格式**【已核】`conversations/record/contracts.py:73-76,141-203`、`segments.py:5-9,84-88,155-163`：

- 条目键是 `<source>:<position>:<payload sha256>`，其中 source 与 position 是写入节点本地队列的行位置。同一条消息在两个节点上会得到两个不同的键。
- 每个节点只写自己的 `rec-<节点 id>/<序号>.jsonl`，整份重写并带版本期望；不同写入者之间不会冲突。
- 条目没有 `replyTo`、`mentions`、类别字段。读写都只处理 `RecordEntry` 里固定的那组字段，多出来的字段在解析时被丢掉，所以新字段必须加进 `RecordEntry`；旧版本读到带新字段的行不会报错。

群消息需要的改动【推断】：

- 群条目以消息 id 定键，由发送方的 daemon 写一次；接收方不再各写一条。
- `replyTo`、`mentions` 作为条目上的可选字段，随条目同步。
- 本地收件箱的 `message_id` 是唯一键（`inbox/service/core.py:195-197`），本地接收按消息 id 去重，重复的直接回「已确认、重复」而不落行（`inbox/service/delivery/receive.py:51-88`）。同一个节点上有两个群成员时，第二个成员的那一行会被当成重复而丢掉。需要按（消息 id，收件人）派生本地行 id，或者改去重键。

**跨写入者的顺序。** 没有全局顺序。每个分段内部有序；合并时按（`createdAtMs`，消息 id）排序，各方最终一致，同步完成前各方看到的先后可能不同。`createdAtMs` 取发送方的本地时钟，时钟偏差会影响排序【推断】。

**conversation cell。** id 是 `keyed_cell_id('conversation', <本地 owner>, …)`（`contracts.py:63-70`），recorder 以 owner 范围创建它（`recorder.py:445-457`）；读取时要求文档的全部作者等于 authority，且 space 的 owner 等于 authority（`cells/codec.py:145-148`）。在多 owner 的 space 里，非 owner 一方的 cell 文档通不过校验，双方的目录名也不同。

## 8. 接收方：从「共享文档有新条目」到「本地收件箱行加唤醒」（问题 8）

可以复用的【已核】：

| 组件 | 用途 | 依据 |
|---|---|---|
| `RecordStore` 的游标、状态表与分段号 | 同样的「持久游标加按键幂等」模式；丢了库可以重放 | `conversations/record/store.py:57-79` |
| `SegmentStore.read`、`parse_segment` | 读分段、按键取条目 | `conversations/record/segments.py:186-206`、`contracts.py:206-214` |
| recorder 的线程循环与退避 | 版本变化、`kick`、空闲超时三种唤醒；1 秒翻倍到 60 秒 | `conversations/record/recorder.py:102-108,259-275` |
| 本地接收 `receive` | 落收件箱行；已有按消息 id 与幂等键的去重（重复时不报错）；已有 send-link 的入站检查；返回是否需要唤醒（意图为 request 或 reply 时） | `inbox/links/local_link.py:138-146`、`inbox/service/delivery/receive.py:32-100,205-213` |
| `grant_body_release` | 让本地行引用共享记录里的正文，不再存第二份 | `conversations/record/recorder.py:505-527` |

提议的流程【推断】：

1. 对每个群 space 注册 watch（路径通配到 `rec-*`），回调只做 `kick`。
2. 启动时、收到 `kick` 时、以及每个空闲周期，扫描各成员分段。持久游标按（space，分段路径）记已处理的条目数，另存已处理的消息 id。
3. 对每个新条目执行 §5 的接收侧检查。
4. 为本机承载的每个群成员（agent，含 user agent）落一行并唤醒，**条目的 sender 自己除外**。按节点跳过自己的 `rec-<本节点>` 目录不够：同一 owner 的另一台设备上也可能有需要投递的成员。
5. 落行后直接把这一行标为已记录，引用指向原条目，并发放正文释放许可。否则 recorder 会把这一行再写回群文档，每个成员写一遍。

重启不重复投递靠第 2 步的持久游标与消息 id，游标丢失时靠本地接收的去重兜底。

### 8.1 与目标方向「正文只在文档里，队列只带通知与引用」的关系【推断】

这个方向不在本轮。本稿的发现里与它有关的：

- **通知可能先于文档到达。** 队列与文档同步是两条独立的路径；双方在线时丢失的更新没有补救（§4.1）。接收方拿到引用而条目还读不到时，需要等待并主动补拉一次，再重试；现有的按引用读取在读不到时只回空，不触发补拉（`segments.py:186-206`）。
- **引用要稳定。** 现在的引用由 space id、分段路径和节点本地的条目键组成（§6、§7）。跨节点使用时，键应改为消息 id。
- **正文释放的前提反过来。** 现在是先投递、记录回读成功后才允许队列丢弃正文（`recorder.py:505-527`）；目标方向下是先写文档、再发通知，发送方要在条目写入并回读成功后才发通知。
- **游标与去重**（§3、§8）不变：通知只是比 watch 更可靠的「去看一眼」的信号。

## 9. 设计提议

范围说明：本轮 GUI 只覆盖单个 owner 的 chat space，以下切片都在本轮之外。

### 9.1 (a) 多 owner 的 chat space，1:1 传输仍走队列

| 切片 | 内容 | 涉及的文件 | 依赖 |
|---|---|---|---|
| CS0 来源检查（**有条件**） | 仅当开关 `trustMembersForUpperLayers` 被移除时需要（2026-11-03 的一份设计稿把它列为计划移除）：届时要为 org 目录以外的 space 提供替代的来源检查。开关保留则不做 | `orgfs/composition/`、`orgfs/mesh.py` | §5 的评估 |
| CS1 chat space 的查找与创建 | 新的标记（kind 与排序后的 owner 列表）；按「名字加标记加成员集合」查找；读并集、写最小 id；认旧的 `hyprial-home`；崩溃后沿用无标记的 space | `composition/cells/spaces.py`、`cells/ports.py` | 无 |
| CS2 邀请与加入 | 创建方邀请对端 owner；space id 随队列信封带到对端；对端 daemon 加入，失败时重试 | `composition/conversations/placement.py`、发送入口里带 space id 的一处、`delivery/ports.py` | CS1、P2 |
| CS3 不含 owner 的 conversation 键 | 共享 space 里的记录目录按 conversation 定键，不经过 owner 范围的 cell；或新增一种 cell 范围（待裁 3） | `conversations/record/contracts.py`、`segments.py`、`cells/codec.py` | CS1 |
| CS4 定时补拉覆盖 chat space（2026-11-18 更正：本轮不做，代价见 `chat-space-two-node-probe-2026-11-18.md` §3.1） | 把 60 秒的定时补拉扩展到 chat space | `orgfs/composition/directory.py` | CS1 |
| CS5 记录成为读模型 | 历史读取合并各 `rec-*` 分段，按消息 id 去重；同一条消息的发送方副本与接收方行合为一条（现状见 P1 的结论） | `conversations/read.py`、`conversations/bodies.py`、`conversations.*` 的处理处 | CS3、S1 |
| CS6 记录路径的写入闸门 | 按 §5 的评估结论落实 | 待评估后确定 | CS1 |

验收：owner X 与 Y 的两个 daemon 之间收发后，双方从同一个 space 读到相同的条目集合；任一方重启后仍然一致。

### 9.2 (b) 群聊

| 切片 | 内容 | 涉及的文件 | 依赖 |
|---|---|---|---|
| G0 群的创建与成员 | 建 space、打标记、写成员文档（成员是 agent，含 user agent）、得到 conversationId；加人、减人映射到 space 成员（§7） | 新增 `conversations/groups/`；`composition/cells/spaces.py` | CS1、CS2 |
| G1 条目格式 | `RecordEntry` 增加 `replyTo`、`mentions`、类别；群条目以消息 id 定键 | `conversations/record/contracts.py`、`segments.py` | S1（同一个文件）、CS3 |
| G2 发送 | `message.send` 带群的 conversationId：核对成员资格与 send-link，由 daemon 追加一条 | P2 拆出的发送模块、`conversations/record/` | G0、G1、C1 |
| G3 接收 | §8 的流程：watch 加扫描、持久游标、接收侧检查、本地落行与唤醒、标记已记录 | 新增 `conversations/intake/`；`composition/conversations/wiring.py`；`inbox/service/`（行 id 与去重键） | G1、CS4 |
| G4 唤醒全部 agent 成员 | R9。agent cell 内 handler 的判断交给 #2712 的判断 harness；user agent 如何分发到客户端界面由它自己决定 | 待定：本地接收返回唤醒标志之后的路径没有读 | G3、#2712 |
| G5 GUI | 群聊界面 | `gui/`、`shell/impl/gui/adapter/` | G2、G3、GUI 线 |

### 9.3 待 owner 裁定

1. **开关 `trustMembersForUpperLayers` 的去留。** 保留则跨 owner 的 chat space 沿用现状、CS0 不做；移除则 CS0 成为前置。
2. **§5 的评估由谁做、何时做。** 它是跨 owner space 上线的前置条件。
3. **共享 space 里是否还要 conversation cell（CS3）。** 不要则记录目录直接按 conversation 定键，cell 只留在单 owner 的情况；要则需要新的 cell 范围与校验规则。
4. **两个 owner 同时建了 chat space 时**，是否采用「双方各自加入对方的、读并集、写最小 id」。另一种做法是规定由 principal 排序靠前的一方创建，代价是那一方不在线时对话无处可记。
5. **群的管理权。** 按现状只能是建群者所属的 owner，且不能转让。是否接受。
6. **同一节点上多个群成员收同一条消息**时，本地行 id 用派生 id 还是改去重键（§7）。
7. **space 的数量。** 每对 owner 一个、每个群一个，常驻成本约为每个 space 4 个线程（`reference-abi` 后端）和一个本地库。是否需要上限或按需装载。
8. **D4。** 建议 1:1 继续走队列（§4.3）。

## 10. 没有验证的部分

- 跨机器、经 tailnet 的同步延迟；真实 daemon 之间的补拉耗时。
- native actor 后端下的数字。
- 一个节点同时在几十、几百个 space 里时 announce 的大小与资源占用。
- §2、§6–§9 中标为【推断】的各项，以及全部切片的文件范围，都没有试做。
- `send.py` 按目标派生消息 id 的现状引自讨论稿 §8.3，本次没有重读。
- 本地接收返回唤醒标志之后，agent 是怎样被唤醒的，没有读（影响 G3、G4）。
- §5 的评估没有做；本稿对它不下结论。
- user agent 及其客户端界面成员的实现在本稿读数基线之后裁定，没有对照代码。
