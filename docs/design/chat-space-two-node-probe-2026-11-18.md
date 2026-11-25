# 同一 owner 两台机器能否读到同一个 chat space：测试探查（GC0）

- 日期：2026-11-18
- 状态：**探查结论，待 owner 审阅**。只加了测试，没有改动产品代码
- 记录：Claude Code（owner 的本机会话的 GC0 子智能体，不是注册 agent）
- 读数基线：`origin/dev` @ `564337f81`
- 前置稿：`docs/design/chat-space-and-group-chat-probe-2026-11-17.md`（P3）
- 标注：【已测】= 本次新增的测试固定了这一行为；【已核】= 读了代码，带 `文件:行`；【推断】= 由前两者推出，没有运行
- 路径省略 `src/hyprial/daemon/impl/`
- 本稿只写统计与分析逻辑，不写访问控制机制

## 0. 结论

1. **机器 B 不会自己持有机器 A 的 owner-home。** B 能收到 A 的 announce，也能记下「A 持有某个 space id」，但不会因此加入这个 space、拉取它，或为它建本地库。daemon 里没有任何代码会自动加入 owner-home；唯一的途径是手动按 id 加入（CLI `fs space join`、MCP 或 `orgfs.join` 请求）。【已测】【已核】
2. **两台机器各自建了 home 时，两个 home 永远互不可见。** 两台机器各读各的，各写各的。「读取取并集、写入取最小 id」的收敛规则要求每台机器都已持有对方的 home，否则不会生效。【已测】
3. **只要按 space id 显式 `join`，B 就能持有 A 的 home，不需要邀请。** 同一 owner 本来就是自己 home 的成员。join 之后两边读到同一个并集，写入同一个 home。【已测】前提：P3 稿 §0 第 4 条所说的默认部署条件，测试按这个条件搭建。条件不成立时的行为本次没有运行（NOT_RUN）。
4. **B 离线期间 A 的写入，在「对端上线」事件时补上。** 实时更新在双方都在线时丢失，60 秒的定时补拉不会补，因为它只覆盖 org 目录 space 和 ACL space。而且一次丢失会卡住同一写入者在这个 home 里的后续结构变更（新分段、新 conversation），直到下一次补拉。补拉由「对端上线」事件触发【已测】；announce 带来新的持有者、传输重建这两种触发只读了代码【已核】。
5. **`conversations.*` 读路径。** join 之前：B 上的 `conversations.history` 返回空，`recordAvailable` 为 true；`conversations.list` 不列出这个 conversation。join 之后：history 立即读得到；list 要等下一次扫描，最多 30 秒（`SWEEP_S`），或者先对这个 conversation 做一次 history 读取。在线期间丢失的那次更新，history 同样读不到，直到「对端上线」事件。【已测】
6. **与计划的出入。** 计划 §2.1 的更正与 §11.1 的末条写的是：信任 tailnet 成员的模式下，owner-home 会在设备之间同步。实际上，即使在这个模式下，home 也只在已经 join 的设备之间同步，而没有任何代码会自动 join。
7. 因此，在第二台机器上投递群消息，前提目前还不成立。缺两样东西：一是 B 怎么得知 A 的 home id 并加入；二是在线期间丢失的更新怎么补（§3）。

## 1. 测试

新增 `tests/daemon/conversations/two_node/`。两个 `OrgFsRuntime` 使用同一个 author（`user:op`）和不同的节点 id，挂在 OrgFS 补拉测试用的同步进程内总线上（`tests/daemon/orgfs/presence/test_orgfs_rebuild_catchup.py` 的 `_Bus`、`_Presence`）。没有 Zenoh，没有真实 daemon。每个节点都把对方视为在线主机和唯一的持有者候选，这是 daemon 能有的最宽的接线。

| 测试 | 固定的行为 |
|---|---|
| `test_second_node_does_not_hold_first_nodes_home_without_join` | 对照：B 已把 A 记为 A 的 home 的持有者，并收到过 A 的上线事件。结果：B 的 space 列表、本地库和 home 并集里都没有这个 home，读这个 conversation 为空 |
| `test_two_homes_created_before_any_join_stay_separate_and_writers_diverge` | 两边各建一个 home 并互相 announce。各自只看到自己的 home，写入目标不同，读到的条目不同 |
| `test_an_explicit_join_needs_no_invite_and_converges_reads_and_writes` | 两边互相 join（没有邀请）后，home 并集相同，写入目标都是最小 id，读到相同的条目；之后 B 写入，A 也能读到 |
| `test_a_write_made_while_the_holder_was_offline_is_pulled_on_its_online_edge` | B 关闭期间 A 追加了一条。B 重启后在收到上线事件之前读不到，收到之后补上。测试里的上线替身在绑定时不重放；生产代码在绑定时会重放已提交的上线状态（`directory.py:611-618` 的说明），所以真实 daemon 重启时，这次补拉应当在绑定时就发生【推断】 |
| `test_a_live_update_lost_while_both_online_is_not_pulled_by_the_periodic_tick` | 双方在线时，丢掉一次更新：home 里的追加，以及对照用 org 目录 space 里的一次写入。到期的定时补拉只安排 1 次，补上了 org 目录 space，home 没有补。之后 home 里新 conversation 的分段也读不到。上线事件之后全部补上 |
| `test_second_node_reads_first_nodes_conversation_only_after_an_explicit_join` | B 是 golden daemon，读模型读 B 的 OrgFS。join 前 history 为空、list 不列出；join 后 list 在扫描之前不列出，扫描之后列出，history 返回 A 记录的消息 |
| `test_second_node_reads_a_later_first_node_write_once_it_holds_the_home` | B 持有 home 后，A 之后追加的条目在 B 的 history 中可读 |
| `test_a_history_read_on_the_second_node_puts_the_cell_into_its_list` | join 后 list 不列出；对这个 conversation 读一次 history 之后，list 列出 |
| `test_second_node_history_misses_a_lost_live_update_until_the_online_edge` | 双方在线时 A 的一次追加丢失，B 的 history 停在旧状态；收到上线事件后补上 |

稳定性：全部测试连续跑 10 遍，结果见 §5。

## 2. 发生了什么，缺了什么【已核】

**announce 只是线索。** 节点的 announce 列出它是成员的每个 space，每项只有 `spaceId`、`roles`、`mode`（`orgfs/composition/directory.py:809-828`）。接收方只把「节点 → space id」记进持有者表（`directory.py:972-992`）。

**补拉只覆盖本地已有的 space。** 三种补拉触发（对端上线、announce 带来新的持有者、传输重建）都只遍历本地已经有库的 space（`directory.py:352-367,380-421`）。

**加入只有显式一种。** `join` 需要 space id 和一个在线的持有者（`orgfs/composition/lifecycle.py:639-675`）。产品代码里 `join` 的调用方只有 org 网络服务（org 目录 space，`org/network/service.py:683,690`）和个人 org（`org/membership/personal.py:281`）。owner-home 没有调用方。

**home 的查找与创建只看本机。** `CellSpaces.home_spaces` 只枚举本机 facade 已有的 space。本机没有 home 时，`private_space` 为本机 author 新建一个（`composition/cells/spaces.py:120-168`）。所以第二台机器第一次写记录时，会建出第二个 home，而不是去找第一个。

**定时补拉的范围。** `_anti_entropy_tick` 只为名字是 org 目录或 ACL 的 space 安排补拉（`directory.py:455-461`）。由 daemon 维护循环每 60 秒（±20%）调用一次（`application/messaging/maintenance.py:675`）。

**为什么一次丢失会卡住后续写入。** 追加一条记录会产生两次提交：一次在分段文档上，一次在 space 的目录树文档上（本次计数：2 次；新建文件为 3 次，含 meta）【已测，调试脚本，未入库】。接收方按（写入者，文档）检查序号连续性，目录树文档缺一次提交，同一写入者之后的目录树变更就全部挂起。所以丢失一次以后，A 新建的分段（新 conversation、换段）在 B 上都看不到，直到一次补拉。

**补拉的限额。** 单次尝试 2 秒，总预算 4.5 秒；连续 3 次失败后暂缓，从 5 秒起翻倍，上限 120 秒（`orgfs/network/protocol.py:37-55`）。上线事件之后，除了限额内的重试，没有周期性的再补拉【推断】：没有逐行读 `orgfs/network/supplier_health.py` 的调度。

**读路径。** `conversations.history` 每次都会列一次这个 conversation 的目录，所以 B 持有 home 之后立刻可读。`conversations.list` 依赖本机记录器的索引和每 30 秒一次的扫描（`conversations/document.py:16-30,63,129-147`）；别的节点写的新 conversation，最多晚 30 秒出现在列表里。

## 3. 可选做法（不实现，由 owner 选）

各项都假设第二台机器属于同一个 owner。跨 owner 的情况见 P3 稿。

| 做法 | 内容 | 需要 OrgFS 层提供什么 | 代价与风险 |
|---|---|---|---|
| 0. 手动加入（今天就能用） | 在 A 上查到 home 的 space id，在 B 上手动按 id 加入 | 无 | 不需要写代码；要靠人操作，新设备每次都要做一遍。两台机器已经各建了 home 时，要互相加入 |
| A. announce 时同 owner 自动加入 home | announce 里每个 space 增加可选字段（名字、owner，或 kind 标记）。接收方见到「owner 是本机 author、名字是 `hyprial-home`、本机没有持有」的 space，就排队一次 `join` | announce 增加可选字段。目录一侧只读 `spaceId`（`directory.py:982-989`），也没有找到其他读 announce 的地方，所以应当可以向后兼容【推断】。注意：announce 超过信封大小时会退化为只带 `spaceId`（`orgfs/network/ingress.py:255-266`），新字段在这种情况下会丢失，需要另行处理；目录 actor 里的一个「待加入」效果，交给生命周期 actor 执行，不在目录 actor 里阻塞 | 小：一处协议字段、一处效果。announce 变大（每项多两个短字段）。不加字段的话，接收方分不出哪个 space 是 home，只能逐个试着加入，成本高，也会在别人的 space 上反复失败 |
| B. 启动或登录时按已知 id 加入 | 把 home 的 space id 写进两台机器本来就同步的位置（例如个人 org space 里的一份文档）；daemon 启动时读出 id 并加入 | 不需要改 OrgFS；需要一个两台机器都已持有的 space | 依赖个人 org 已经建好（它的建立依赖外部组服务）。没有个人 org 的部署仍然看不到。启动时多一次 join |
| C. 由 owner 身份派生唯一的 home id | 两台机器算出同一个 space id。谁先到谁建，另一台加入 | 让同一 space 的两份创世提交可以合并，或者能产生逐字节相同的创世。现在后到的创世在已有 meta 的副本上被拒收（见 P3 §2） | 最大：要改 OrgFS 准入。两台机器同时离线创建时仍要处理 |
| D.（与 A–C 叠加）home 进入定时补拉 | `_anti_entropy_tick` 的筛选同时认 `hyprial-home`（以后加上 chat space） | 筛选条件本身约 5 行；但每轮补拉都要重发空间的全部内容，要让它便宜，还得改一次补拉交换什么内容（见 §3.1） | **更正（2026-11-18）：原稿写「很小」，实测不成立。** 筛选改动小，代价不小：每 60 秒一轮，每轮传输量随历史线性增长，home 有 300 个 conversation 时一轮在预算内做不完。它能修复 §0 第 4 条（在线期间丢失的更新），不解决发现 |

建议（2026-11-18 更正）：原稿写「A 加 D 是最小组合」，D 的代价被低估，该推荐撤回，见 §3.1。A 仍可让 B 自动持有 A 的 home，但在补拉变便宜之前，在线期间丢失的更新没有便宜的补法。B 适合不想改 announce 时用。C 不建议在本轮做。

### 3.1 更正：做法 D 的实测代价（2026-11-18）

原稿说做法 D「很小」，依据是筛选条件只有一行。之后做了测量，结论相反。

**测法。** 两节点进程内、同一套测试总线；节点 B 已经持有 home 且没有缺失；B 向 A 补拉一次。字节数与页数取自 `orgfs.sync.completed` 日志行。耗时在一台忙的共享机器上测得，只作参考。走的是「非副本供给方」分支，副本分支没有运行。

| 情形 | 页数 | 响应字节数 | 耗时 |
|---|---|---|---|
| org 目录 space，一个文件（基线） | 1 | 5,400 | 56 ms |
| home，10 个 conversation，各 1 条消息（12 个文档） | 1 | 61,894 | 0.23 s |
| home，100 个 conversation（102 个文档） | 1 | 642,820 | 3.1 s |
| home，300 个 conversation（302 个文档） | 4 | 2,402,877 | 8.6 s（直接调用，无期限） |
| 一个 conversation，10 条消息 | 1 | 35,097 | 0.14 s |
| 一个 conversation，100 条消息 | 1 | 275,193 | 0.77 s |
| 一个 conversation，300 条消息 | 1 | 810,136 | 4.0 s |

**原因【已核】。** 一轮补拉请求一个同步流，供给方从空状态导出该 space 的全部文档；流结束时请求方把游标清零。所以每一轮都重发整个 space 的内容。请求方已有的页会在它这一侧被跳过，但仍然走完了链路。成本随历史大致线性增长：单个 conversation 约每条消息 2.7 KB，一条消息的 conversation 约 6.5 至 8.8 KB。

**定时路径上的表现。** 300 个文件的 home：一轮在预算内没有做完，以期限到期结束（3 页、1.40 MB、5.2 s），下一次再接着做。这样的 home 永远不会空闲。

**只读了代码，没有运行【推断】。** home 很大时，一页慢了可能被记成供给方失败，而这个计数在整个运行时的所有 space 之间共享，会拖慢定时补拉本来要保证的 org 目录和 ACL 补拉。

**为什么定时补拉只覆盖 org 目录和 ACL space。** 没有找到「其他 space 被有意排除」的说法。范围来自它最初修的 bug（后加入者看不到目录行），后来为了控制负载又收紧了（每个 space、每个节拍只找一个在线持有者）。

**结论。** 筛选改动本身约 5 行，能修复丢失的更新；但要让 home 的定时补拉便宜，必须改变一次补拉交换的内容。可选方向有三个，各自还有未决问题：基于差异的补拉；保留流游标；只在本地发现缺口的证据时才补拉。

**决定（2026-11-18）：本轮不做。** 多机器 home 收敛（自动加入，以及丢失的实时更新的修复）是以后单独的一项工作。本轮的 conversation 与群聊功能只面向单机；手动互相加入过 home 的机器，只靠上线事件收敛。

已有数据【推断】：已经各建了 home 的两台机器，用 A 或 B 互相加入以后，自然进入「读并集、写最小 id」，不需要迁移；已发出的正文引用里带着 space id，仍然可以解析。

## 4. 没有验证的部分

- 测试用的是同步的进程内总线，不是 Zenoh。延迟数字见 P3 稿 §4.2，本次没有重测。
- A 一侧用 `SegmentStore` 直接写记录，没有经过 A 的记录器（队列到记录的路径单节点已有 RM1 测试）。
- golden harness 里两边的 agent 地址相同。真实的第二台机器上，agent 地址带着各自的节点，本次没有建模。
- 做法 A–D 都没有试做。
- 默认部署条件不成立时的 join 行为没有运行。

## 5. 稳定性

9 个测试连续跑 10 遍，10 遍全部通过，单遍 3.9–8.1 秒（同机有其他会话在跑测试）。
