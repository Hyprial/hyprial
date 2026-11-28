# OrgFS 精简评估（WIP 讨论稿，2026-11-24）

状态：WIP / 讨论稿，不是已批准方案，不含实现。owner 2026-11-24 指示：按“尽可能
精简 OrgFS、删除找不出明确需求支撑的模块、不考虑兼容现有 OrgFS”的方向评估，再
重新实现与修复。本文给出盘点、需求证据与待决问题；§4 的问题回答之前，§5 的目标
形态只是估算。

关联：#3142（读路径成本与生产只读统计）、#3145（meta read model 与 checkpoint
比较）、#3148（新建文档的 client id）、#3118（status 物化计数）。

复核：第 0 节第 3、4 条与第 3 节行 5、16、20、22 的消费者结论已用 grep 在 dev
`b3e4cc69c` 上复查；其余条目未逐条复核。

基线：dev `b3e4cc69c`。只读了代码与文档，没有运行任何测试，没有改动任何文件。
路径均相对仓库根；`orgfs/` 指 `src/hyprial/daemon/impl/orgfs/`。

证据等级：**[码]** 读代码确认；**[文]** 从文档转述；**[推]** 推断，未验证。

## 0. 结论

1. **“去掉 journal 权威”已经是批准过的方向。** `docs/design/orgfs-yjs-defaults-2026-10-29.md` 的状态是 “design approved … not implemented”（第 11 行），决定以当前 Yjs 状态为权威、同步改为 state vector、删除 history / checkpoint / retirement / snapshot barrier **[文]**。它当时最大的前置条件（PAC work repair 依赖历史）已被 #2214 解除：work 文档现在是普通文本，`contract/work-items/README.md:3-7` **[文]**；`journal_history` 在 `orgfs/` 之外已无调用方 **[码]**。
2. **journal 模型撑起来的代码量大约占一半。** `storage/store/` 5,276 行、`network/` 3,576 行、`storage/replica*.py` 1,642 行、`storage/space_authority.py` 810 行，以及 `document/operations/purge.py` 758 行，都以“逐提交 envelope + writer/seq”为前提（`network/protocol.py:1-5` 明写 “The journal remains authoritative”）**[码]**。
3. **唯一找到的、真正依赖“历史”的跨域消费者是 `document_authors`。** org 目录的设备记录校验、leave 请求、member agent 清单、cell 镜像都靠它判断“这份文档是谁写的”（见 §3 行 5）**[码]**。这是安全相关的需求，不能直接删；10-29 设计的答案是“当前作者 / 签名字段”，但签名尚未实现（`orgfs/storage/store/__init__.py` 注释 “Authors are self-asserted until signatures land”）**[码]**。**这是精简方案里最需要 owner 决定的一项。**
4. **没有找到 `orgfs/` 之外消费者的能力**：`open_doc` 结构化文档（511 行）、`journal_history`、`read_at` / `stat_at` 的 CLI 入口。它们只出现在测试、IPC 桥接、请求目录和生成的客户端里（§3）**[码]**。`watch_meta` 在 `orgfs/` 之外没有调用方，但 `orgfs/composition/directory.py` 内部在用。
5. **已发布面很宽。** 31 个 `orgfs.*` 方法全部在 `contract/daemon-request/openapi.json` 和 MCP 工具表里。owner 已说明不考虑兼容，但删除方法仍会触发 `Contract-Impact` 门和 GUI 契约计数测试，需要一并改 **[码]**。
6. **ROADMAP.md 没有任何 OrgFS 条目**（`grep -i orgfs ROADMAP.md` 无结果）**[码]**，即没有登记在案的发版义务约束这次精简。

## 1. 三份设计文档的要点

### 1.1 `orgfs-yjs-defaults-2026-10-29.md`（已批准，未实现）

| 主题 | 决定 | 行 |
| --- | --- | --- |
| 权威 | 文档 = 当前 CRDT 状态；服务端不保留权威的逐提交 journal、版本历史或可回放序列 | 25-28 |
| 持久化 | 每文档一个私有 delta 文件，约 500 次更新或 1 MiB 合并进 state 文件；delta 不是 API | 41-50，决定 1 |
| 同步 | 对端发 per-doc state vector，持有方回 `encodeStateAsUpdate(doc, vector)`；无 snapshot-barrier / retirement / baseline 特例 | 56-60 |
| 删除清单 | `journal.py` 提交与回放、`commits` 表；`doc_checkpoints` 与压缩脚本；`snapshots` / `snapshotPoints` / `retirements` / baseline 协议；`fs history` / `read_at` / `stat_at`；`document_authors` 作为历史查询；`export_since` 游标、sync snapshot manifest、`unconfirmed_commits` 及其 status 字段 | 62-88 |
| 保留并收窄 | `pending`（等前驱的乱序更新，有 TTL 与字节上限）；`outbox`（确认即删）；meta / 成员 / ACL 的当前状态，“never replay old membership to authorize a current write”；trash | 90-99 |
| trash | 保留；新增 `fs rm --permanent`；trash 只对应被删节点的最后当前状态 | 决定 2 |
| 历史 | 现在不做；将来若要，做成可选的异步 audit sidecar，不得成为同步依赖 | “Future history” 节，决定 4 |
| 迁移 | 单向；所有 daemon 停机；备份、物化校验、写 v3 布局、旧 journal 路径放占位文件使旧版本失败关闭 | “Journal folding migration” 节 |
| 兼容 | 0.5.5.3 直接断开；0.5.5.4 起每版提供对 N-1 的只读导出 | 决定 3 |

对照当前 dev 已过时或需要重新判断的假设：

| 假设 | 现状 | 证据 |
| --- | --- | --- |
| PAC work repair 依赖 `fs.history` / `read_at`，是 cutover 的硬前置 | 已解除：work 文档是普通文本，没有 repair 协议 | `contract/work-items/README.md:3-7` [文]；文档自身第 3-9 行的 superseded 说明 [文] |
| 目标版本 0.5.5.3 | 该版本已发布且未包含本设计；当前生产是 0.5.5.4 | 文档第 11-18 行 [文] |
| 需要 N-1 只读导出、迁移维护窗口 | owner 2026-11-24 指示“不用考虑兼容现有 OrgFS” | 会话指示 |
| `document_authors` 换成“当前作者 / 签名字段” | 签名未实现；四个跨域调用方仍在用历史作者集合 | §3 行 5 [码] |
| 成员判定“不回放旧成员关系” | 现状恰恰是回放：`_replay_membership`、`meta_frontiers`、`member_add_frontiers`、`_journal_client_ids` | `orgfs/storage/store/membership.py:716`、`snapshots.py:255` [码] |
| 文档没有提到 | client id 登记（`writers.peerIds`）、resident replica / `serve`、checkout、web server、blob purge 的去留 | 全文无相关条目 [文] |

### 1.2 `orgfs-baseline-snapshot-2026-10-29.md`

状态“设计草案，待定稿”（第 3 行）。它是在 journal 模型内部给“压缩后老节点如何追赶”打补丁：`snapshotPoints` 增加 `baseline: true`。若采用 1.1 的状态模型，这份设计整体失去对象（1.1 的删除清单第 3 条已点名）**[文]**。

### 1.3 `orgfs-local-space-purge.md`

`hyprial fs purge <space_id>`：只删除**本节点**的副本，不产生复制的删除；仅 owner、且空间无其他成员时允许；与 org 绑定的空间需 org 已删除（第 1-22 行）**[文]**。实现在 `orgfs/composition/space_purge.py`（539 行）。它与 journal 无关，是独立的本地清理能力。

注意区分两种 purge：上面是**本地空间清除**；另一种是**内容清除**（`orgfs.purge_plan` / `purge` / `purge_status` / `unban`，`contract/orgfs/content-purge.md`），按 `doc-range` 目标清除内容与 blob，并在全网封禁 blob 摘要。后者深度依赖 retirement / snapshot / writer 清单（返回字段含 `snapshots`、`writers_affected`、`authors_before`）**[文]**。

## 2. 模块盘点

行数来自 `wc -l`。“外部消费者”指 `orgfs/` 之外的 `src/hyprial`、`gui/`、`contract/`。

| 模块 | 行 | 职责 | 外部消费者 | 服务的需求 |
| --- | ---: | --- | --- | --- |
| `api.py` | 356 | 公共 Protocol `OrgFs`、值对象、错误 | 几乎所有消费域（`org/`、`conversations/`、`cells/`、`service_connect/`、IPC 桥）[码] | 公共面 |
| `docs.py` | 173 | `LocalOrgFs`：把 7 个 facade mixin 组合成实现类（`docs.py:112`） | 经 runtime 间接使用 | 组合入口 |
| `runtime.py` | 321 | `OrgFsRuntime`：daemon 内的 orgfs 运行时 | `org/orgfs_migration.py:20`、`app._require_orgfs_runtime()` 多处 [码] | 生命周期 |
| `mesh.py` | 404 | `OrgFsMesh`：组合 sync / queries / recovery / ingress / supplier health（`mesh.py:91`） | 未找到直接导入（经 runtime）[码] | 网络同步入口 |
| `instrumentation.py` | 112 | 开关控制的 announce 归因事件 | 未找到外部导入 [码] | 诊断 |
| `web.py` | 128 | 把 `orgfs:` URI 派生成人可点的 https 链接 | `daemon/interface.py:331` 导出 [码] | web 链接 |
| `composition/directory.py` | 992 | `RuntimeDirectory`：org 目录空间的定时同步与 supplier 选择 | org 域 [码] | org 目录 |
| `composition/lifecycle.py` | 801 | 运行时启停、re-serve / reuse 决策 | 经 runtime | 生命周期 |
| `composition/space_purge.py` | 539 | 本地空间清除（§1.3） | CLI `fs space purge`，`fs/commands.py:574` [码] | 本地清除 |
| `composition/content.py` | 274 | `RuntimeContent` | 经 runtime | 内容装配 |
| `composition/checkout.py` | 143 | `RuntimeCheckout` | CLI `fs space checkout`；`pac/views/overview.py:887` [码] | 只读检出 |
| `composition/vocabulary.py` | 240 | 运行时装配用的类型 | 内部 | — |
| `document/model.py` | 937 | `TreeDocument`、`_ContentDocument`、本地文档与 tree 引擎 | 内部 | tree / 内容 |
| `document/projection.py` | 961 | facade 提交、回滚、节点创建 | 内部 | 写路径 |
| `document/operations/content.py` | 876 | 读写文本 / 字节、`document_authors`、`journal_history`、`status` | 见 §3 | 读写 |
| `document/operations/tree.py` | 490 | `FacadeTreeHistory`：tree 操作与历史 | IPC `orgfs.history` 等 | tree / 历史 |
| `document/operations/spaces.py` | 733 | 空间创建、加入、成员、加载 | org / cells 域 [码] | 空间与成员 |
| `document/operations/purge.py` | 758 | 内容清除（plan / run / status / unban） | CLI `fs purge *`；GUI `orgfs.purge` [码] | 内容清除 |
| `document/effects.py` | 632 | `FacadeEffects`：post-commit 作用域、观察者 | 内部 | 写后副作用 |
| `document/space_state.py` | 539 | `FacadeSpaceState` | 内部 | 空间状态 |
| `document/structured.py` | 511 | `StructuredOrgDoc` / `DocMap` / `DocList` / `DocText`（`open_doc` 的句柄） | **未找到消费者**（§3 行 20）[码] | 结构化文档 |
| `network/protocol.py` | 194 | 复制协议帧；“journal remains authoritative” | 内部 | 同步 |
| `network/sync.py` | 774 | `MeshSync`：分页同步、snapshot 安装、`snapshot-barrier` | 内部 | 同步 |
| `network/queries.py` | 816 | `MeshQueries`：对端查询应答 | 内部 | 同步 |
| `network/recovery.py` | 895 | `MeshRecovery`：追赶与恢复 | 内部 | 同步 |
| `network/ingress.py` | 418 | `MeshIngress`：入站更新接入窗口 | 内部 | 同步 |
| `network/supplier_health.py` | 478 | 跨空间的 supplier 健康与退避 | 内部 | 同步退避 |
| `projection/checkout.py` + `checkout_authority.py` | 798 | 把空间物化成只读目录 | CLI、PAC overview [码] | 只读检出 |
| `projection/purge.py` | 278 | 清除计划的值对象与规范哈希 | 内部 | 内容清除 |
| `storage/store/*` | 5,276 | journal、准入、成员回放、pending、retirement、snapshots、writer 登记 | 内部（经 `space_authority`）| journal 模型 |
| `storage/space_authority.py` | 810 | 每空间串行的持久编辑权威（actor + effect lane） | 内部 | 写串行化 |
| `storage/replica.py` + `replica_authority.py` + `replica_layout.py` | 1,642 | resident replica：按 P2 key 布局存不可变对象、按范围供给日志 | CLI `fs space serve` [码] | 持久持有方 |
| `storage/blobs.py` + `blob_authority.py` | 1,180 | 内容寻址 blob 存储与引用账本 | `write_bytes` / `read_bytes` 调用方 | 二进制文件 |
| `webserver/*` | 1,147 | 只读 HTTPS web 服务 | `daemon/interface.py:139-304` 导出；`mcp/entry.py:36`；CLI `fs space web`（`fs/commands.py:351`）[码] | 浏览器查看 |

合计 25,677 行；测试 `tests/daemon/orgfs` 约 30.9k 行，其外另有约 5.6k 行 orgfs 相关测试。

## 3. 各项能力的需求证据

搜索范围：`src/hyprial`（排除 `orgfs/` 自身）、`gui/src`、`contract/`、`.agents/skills`、`docs/guides`、`ROADMAP.md`、`docs/reference/known-problems.md`。
“桥”指 `daemon/impl/application/messaging/orgfs_bridge.py`，它只是把 IPC 方法转给 facade，本身不算需求证据。

| # | 能力 | orgfs 外的真实消费者 | 已发布契约 | 结论 |
| ---: | --- | --- | --- | --- |
| 1 | 逐提交 journal 作为权威 | 无直接消费者；间接支撑 #5、#6、#11、#15 | 无专门字段 | **用库能力替代**（Yjs 当前状态即权威）；10-29 已批准 |
| 2 | writer / seq / lane、envelope | 无；仅 `org/orgfs_migration.py:307` 调 `writer_attributions()` 做迁移 [码] | 无 | **删除**（envelope 仅留作在途传输帧，10-29 第 64-66 行）|
| 3 | checkpoint、启动回放、`.loro` 对账 | 无 | 无 | **删除**（由 #1 派生）|
| 4 | retirement、snapshot barrier、压缩、baseline | 无；`retirement_records` / `snapshot_point` / `export_since` 外部调用 0 处 [码] | `snapshot-barrier` 错误码；purge 响应里的 `snapshots` | **删除**（由 #1 派生；内容清除见 #11）|
| 5 | `document_authors`（历史作者集合） | `org/network/directory.py:244`（设备记录校验）、`:454`（leave 请求）、`org/network/member_agents/__init__.py:145`、`cells/runtime/mirror.py:162`（cell 校验）[码] | 无 IPC 方法 | **待 owner 决定**：需求真实且安全相关；问题见 §4-Q1 |
| 6 | `document_covers_member_add`（因果覆盖） | `org/network/directory.py:460` [码] | 无 | **待 owner 决定**（同 Q1；Yjs state vector 比较可以表达因果覆盖 [推]）|
| 7 | client id 登记（`writers.peerIds`） | 无；只被 store 内部的 meta frontier 与准入使用 [码] | 无 | **删除**，前提是 #8 不再需要按 client id 投影 [推] |
| 8 | meta frontier、移除优先（`_replay_membership`、`meta_frontiers`、`member_add_frontiers`）| 无直接消费者；服务于“被移除成员之后的写入要拒绝” | `not-a-member` 等错误码 | **待 owner 决定**：需求真实，但 10-29 的原则是“不回放旧成员关系”；问题见 Q2 |
| 9 | 成员与授权（invite / remove / join / members） | CLI `fs member *`（`fs/commands.py:261-342`）；org 成员域多处；GUI `orgfs.members` [码] | 是 | **保留** |
| 10 | holders / `unconfirmed` 持久确认、`serve`、resident replica | CLI `fs space status` / `serve`；GUI `orgfs.status`（`orgfs-request-api.ts:189`）；`unconfirmed_commits` / `holders_seen` 外部直接调用 0 处 [码] | `orgfs.status` 字段 | **待 owner 决定**：见 Q3。10-29 决定删 `unconfirmed_commits`，改报 holders / vector |
| 11 | 内容清除（plan / run / status / unban）| CLI `fs purge *`；GUI `orgfs.purge`（`orgfs-request-api.ts:263,270`）[码] | `contract/orgfs/content-purge.md` | **待 owner 决定**：见 Q4。需求（owner 清除敏感内容）可信，但现实现绑定 retirement / snapshot |
| 12 | 本地空间清除 | CLI `fs space purge`（`fs/commands.py:574`）[码] | `contract/orgfs/local-space-purge.md` | **保留**（与 journal 无关）|
| 13 | pending 导入队列 | `org/network/directory.py:147,320` 读 `parked_commits` 做诊断 [码] | 目录状态里的 `parkedCommits` | **保留并收窄**（10-29 第 92-93 行）；Yjs 自身也会暂存缺前驱的更新 [推] |
| 14 | blob 存储 | `write_bytes` / `read_bytes`；web server；checkout | 是 | **保留** |
| 15 | 历史读取 `history` / `read_at` / `stat_at` | `history`：CLI `fs history`（`fs/commands.py:177`）、GUI（`orgfs-request-api.ts:204`）。`read_at` / `stat_at`：**无 CLI、无 GUI 功能代码**，仅桥、MCP 表、生成客户端 [码] | 是（openapi、MCP）| `read_at` / `stat_at` **删除**；`history` **待 owner 决定**（Q5）|
| 16 | `journal_history` | **未找到消费者**（定义与实现各 1 处，`api.py:326`、`content.py:151`）[码] | 无 | **删除** |
| 17 | trash / restore | CLI `fs trash *`；GUI `orgfs.trash` / `restore` [码] | 是 | **保留**（10-29 决定 2）|
| 18 | tree 移动 | CLI `fs mv`；GUI `orgfs.move` [码] | 是 | **保留** |
| 19 | org 目录与设备记录（`composition/directory.py`）| org 网络域 [码] | 经 org 契约 | **保留**（职责属于 org 域还是 orgfs 可另议）|
| 20 | 结构化文档 `open_doc` / `StructuredOrgDoc` | **未找到消费者**：`src/hyprial` 中 `orgfs/` 之外 `open_doc` 出现 0 次；`gui/src`、`contract/` 0 次；仅 5 个测试文件使用 [码] | 无 IPC 方法 | **删除**，除非 owner 有计划中的消费者（Q6）|
| 21 | `watch` | CLI `fs watch`（`fs/commands.py:192`）；checkout 由 watch 流驱动 [码] | 是 | **保留** |
| 22 | `watch_meta` | `orgfs/` 之外调用 0 处；`orgfs/composition/directory.py` 内部使用 [码] | 无 | **保留为内部接口**，从公共 Protocol 移除 |
| 23 | 只读检出 checkout | CLI `fs space checkout`；`pac/views/overview.py:887` [码] | 是 | **保留** |
| 24 | web server | `daemon/interface.py` 导出；CLI `fs space web`；`mcp/entry.py:36` [码] | 是 | **保留**（与存储模型无关）|
| 25 | import / export（文件级）| CLI `fs import` / `export`；`docs/guides/migration-org-no-personal.md` [码] | 是 | **保留** |
| 26 | presence / holder 上线判定 | 仅 orgfs 内部与 `tests/daemon/orgfs/presence` [码] | `no-holder-online` | 随 #10 决定 |
| 27 | hydration（内容按需拉取）| 仅 orgfs 内部与 `tests/daemon/orgfs/hydration` [码] | `ORGFS_CONTENT_PENDING`（org 目录使用，`directory.py:238`）| **待 owner 决定**：若同步改为整文档 state vector，是否还需要“先同步 tree、内容后到”（Q7）|
| 28 | supplier health / 退避 | 内部 | 诊断事件 | **保留并收窄**（任何对等同步都需要退避）[推] |
| 29 | announce instrumentation | 未找到外部导入 [码] | 诊断事件 | **待 owner 决定**（是否仍在用于排障）|

## 4. 需要 owner 回答的问题

| # | 问题 | 为什么关键 |
| --- | --- | --- |
| Q1 | 没有逐提交历史后，“这份文档是谁写的”由什么保证？选项：(a) 文档内的当前作者字段，由准入在写入时校验；(b) 签名；(c) 按 client id 归属作者（保留某种登记）。| 设备记录、leave 请求、cell 校验都依赖它；选 (c) 会把 `peerIds` 留下来 |
| Q2 | 被移除成员的并发写入，需要多强的保证？“当前成员表里不是成员就拒绝”是否足够，还是必须判断“这次写入是否在移除之前产生”？| 后者是 meta frontier / 成员回放存在的原因，也是 `_journal_client_ids` 启动扫描的来源 |
| Q3 | 是否需要向用户报告“本机有多少修改尚未被其他节点持有”？是否需要专门的 resident replica（`fs space serve`）角色？| 决定 `storage/replica*`（1,642 行）与 `unconfirmed` 的去留 |
| Q4 | 内容清除要达到什么效果：从当前状态删除并让 Yjs GC 回收是否足够，还是必须保证全网节点的历史副本与 blob 都被清除并封禁？| 决定 `document/operations/purge.py` + `projection/purge.py`（约 1,036 行）与 `purgeList` 的去留 |
| Q5 | `fs history` 和 GUI 的历史面板是否仍是产品需求？| 10-29 决定现在不做历史；GUI 目前在调用 `orgfs.history` |
| Q6 | 结构化文档 `open_doc` 是否有计划中的消费者？| 当前 511 行无人使用 |
| Q7 | 是否仍需要“tree 先到、内容按需拉取”？还是空间内所有文档整体同步即可？| 决定 hydration 与 `ORGFS_CONTENT_PENDING` 语义 |

## 5. 精简后的目标形态（估算，[推]）

以下假设 Q1 选 (a)、Q2 选“当前成员表”、Q3 不保留专门的 replica 角色、Q4 选当前状态删除、Q5 不做历史、Q6 无消费者。任何一项答案不同，对应模块就要保留。

| 目标模块 | 职责 | 来源 |
| --- | --- | --- |
| `api.py` | 收窄后的公共 Protocol | 现 `api.py` 去掉 history / journal / structured / purge plan |
| state store | 每文档 state 文件 + delta 文件；原子替换；无 SQLite journal | 取代 `storage/store/*` 的大部分 |
| membership | meta 文档里的 owner / 成员当前状态；写入时校验 | `storage/store/membership.py` 中与回放无关的部分 |
| tree | `TreeDocument` 与移动语义 | `document/model.py` 保留 |
| facade | 读写、mkdir、mv、rm、trash、watch | `document/projection.py`、`operations/content.py`、`operations/tree.py`、`operations/spaces.py` 收窄 |
| sync | state vector 交换 + 差量；pending；退避 | 取代 `network/sync.py`、`queries.py`、`recovery.py` 的大部分 |
| blobs | 内容寻址存储 | `storage/blobs.py`、`blob_authority.py` 保留 |
| checkout / web / local purge / import-export | 不变 | 现有模块 |

按 §2 的行数，明确依赖 journal 模型、在上述假设下可删除或重写的部分约为：`storage/store` 5,276 + `network` 中 sync / queries / recovery / protocol 2,679 + `storage/replica*` 1,642 + 内容清除 1,036 + `structured.py` 511 ≈ **11,100 行，约占 43%**。重写后新增多少无法从阅读估出。

测试目录（`tests/daemon/orgfs`，行数为该目录全部 `.py`）：

| 目录 | 行 | 精简后的去向 [推] |
| --- | ---: | --- |
| `mesh/` | 7,562 | 大部分随同步协议重写 |
| `storage/` | 5,894 | 大部分删除（journal、checkpoint、scaling、pending replay）|
| `authority/` | 3,322 | 准入规则部分保留，writer / 登记部分删除 |
| `presence/` | 2,071 | 随 Q3 |
| `purge/` | 1,831 | 随 Q4 |
| `replication/` | 1,755 | 随同步协议重写 |
| `server/` | 1,614 | 保留（web server）|
| `contracts/` | 1,128 | 保留并收窄（含 `test_orgfs_structured.py`，随 Q6）|
| `hydration/` | 982 | 随 Q7 |
| `lifecycle/` | 780 | 保留 |
| `blobs/` | 632 | 保留 |
| `document/` | 599 | 保留 |
| `addressing/` | 432 | 保留 |
| `commands/` | 385 | 保留并收窄 |
| `join/` | 163 | 保留 |
| `instrumentation/` | 159 | 随 #29 |
| 顶层 5 个文件 | 1,595 | 多为同步规模测试，随协议重写 |

## 6. 风险与未知

1. **`network/` 我只读了模块头和类名**，没有读同步、恢复、接入的实现。“可由 state vector 交换取代”是依据 10-29 设计的转述加推断，没有逐函数核对其中是否有与 journal 无关、必须保留的逻辑（例如 Zenoh 会话与 ACL 的处理）。
2. **`composition/directory.py`、`lifecycle.py`、`storage/replica*.py`、`webserver/` 没有读实现**，职责描述来自 docstring 与调用点。
3. **消费者搜索是文本匹配**：按方法名 grep `.name(`。通过 `getattr`、动态分发或别名调用的消费者会漏掉。`history`、`restore`、`serve`、`checkout`、`trash` 这些常见词有其他对象的同名方法，我只采信了能确认接收者是 orgfs facade 的命中。
4. **GUI 只检查了 `gui/src/features/org/impl/api/orgfs-request-api.ts`** 的方法字面量，没有核实这些调用是否接在真实页面上。AGENTS.md 说明 GUI 当前处于 “backend not wired” 占位状态，所以 GUI 侧的命中是“代码里有调用”，不是“用户在用”。
5. **MCP 工具面没有展开**：`daemon/impl/mcp/server.py` 引用了多个 `orgfs.*` 方法，agent 是否实际依赖其中的历史类方法没有证据。
6. **`document_authors` 的四个调用方我只读了调用行附近十几行**，没有确认换成“当前作者字段”后它们的安全性质是否等价。
7. **Q2 的现状语义没有完整读懂**：`_replay_membership`、`member_add_frontiers` 的精确保证（例如重新加入后的 epoch 处理）只看了入口。
8. **docs/notes/orgfs-impl/ 下的原始决策（G1、G2、P1、P2 rework）没有读**。其中可能记载了 journal 模型当初被选中的理由，以及“按 node id 派生 client id”的原因。
9. **没有运行任何测试**，也没有验证删除某模块后的导入图。§5 的行数是静态相加，不是可行性证明。
10. **跨实现契约**（`contract/run-all.sh`）与 `sidecar/` 的 Go 侧是否消费 orgfs 的线上格式，没有检查。
