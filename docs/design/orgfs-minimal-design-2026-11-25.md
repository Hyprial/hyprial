# OrgFS 精简后的目标设计（草稿，2026-11-25）

状态：**定稿（第 2 版，2026-11-25）**。本文是 `orgfs-minimal-plan-2026-11-24.md` 阶段 B 的产出，
也是阶段 C 各工作包的接口冻结点。第 1 版经 codex `gpt-6.2-sol` 审查（owner 2026-11-25 指示由它
审查、不再逐项请示 owner），审查报告全文在 `orgfs-minimal-design-review-2026-11-25.md`；8 条
“必须改”与 7 条“应该改”的处理见 §8。阶段 C 中发现本文有错时，改本文并在 §8 追加一行，不在
各工作包里各自变通。

路径均相对仓库根；`orgfs/` 指 `src/hyprial/daemon/impl/orgfs/`。基线：`intg/orgfs-minimal`
`ef30e69ed9`。证据等级：**[码]** 读代码确认；**[推]** 推断。

## 0. 读码结论：`network/`、`mesh.py`、`composition/directory.py`

读完的文件：`network/protocol.py`、`network/ingress.py`、`network/sync.py`、`mesh.py`、
`composition/directory.py` 全文；`network/queries.py`、`network/recovery.py`、
`network/supplier_health.py` 读了全部函数签名与入站三道门（`_receive`、`_finish_receive`）、
查询应答入口（`_handle_sync`、`_handle_log_range`、`_frontier_reply`）、调度入口
（`schedule_sync_from`）。`queries.py` 的分页游标编码、`recovery.py` 的 projection 修复重试
没有逐行读。

### 0.1 传输层不提供对端身份 [码]

- `daemon/impl/transport/api.py:15` 的 `TransportSample` 只有 `key`、`payload`、`kind`、
  `generation`，没有来源身份。
- `network/ingress.py:173-176` 明写：“TransportSample exposes no Zenoh source identity. The
  trusted-tailnet fallback is origin.node”。订阅收到的 update，其 supplier 取自信封里**自报**的
  `origin.node`。
- 拉取（`session.query`）发往 `orgfs_sync(space_id, supplier)` 这个含节点名的 key，应答里的
  `supplier` 字段同样是自报的；请求方身份是请求体里自报的 `requester.{node,author}`
  （`ingress.py:410-418` `_requester_allowed`）。
- 结论：计划 B.3 的选项“只接受来自当前成员节点的 update（依赖传输层已认证的对端身份）”的前提
  **不成立**。现状能依赖的只有 tailnet 的网络层准入加自报身份。

### 0.2 现状的入站门是三道，全部基于自报身份 [码]

`network/recovery.py:486-550` `_receive`：

1. 帧格式与大小（`_decode_update_header`，`ORGFS_ENVELOPE_BYTES`）。
2. supplier 门：`_supplier_allowed`（该节点在线，由 daemon 的 presence 判定）与
   `_supplier_member_allowed`（`mesh.py:300-327`：节点的设备记录所属用户是当前成员；
   `trust_tailnet_members=True` 时整道门被绕过）。节点到用户的映射来自 org 目录空间的设备记录
   （`composition/directory.py:648-713` `_device_owner_index`，含已删除记录的 trash 历史，靠
   `watch_meta` 失效）。
3. store 准入（`storage/store/admission.py`）：按信封里的 `origin.author` 对照成员表、受保护
   目录路径规则、移除优先。文件头明写该边界“assumes `origin.author` is genuine; signatures are
   a separate…”；`storage/store/__init__.py:124` 同样写“Authors are self-asserted”。

结论：**现状没有任何密码学意义上的作者归属**。`document_authors` 返回的是“自报作者的集合”。
所以“换成作者字段后安全性质是否等价”的基线是：tailnet 内任何能发帧的节点都能冒充任何作者。

### 0.3 仓库里有签名原语，但生产上未启用 [码]

- `identity/impl/user_key/crypto.py`：Ed25519 `generate`/`sign`/`verify`，带 `Purpose` 的
  签名输入与确定性 JSON（`canonical_bytes`）。
- `identity/impl/user_key/binding.py`：`verify_binding`/`verify_revocations`，按 issuer 的 JWKS
  校验“用户 ↔ 公钥”绑定与吊销。
- `daemon/impl/user_key/`（service、verifier、login_key、policy、ipc）。
- **已核实（2026-11-25）：生产上不能用来强制校验。** `daemon/impl/user_key/service.py` 文件头：
  “U1 ships no active production purpose or trust source”；`Signer(enabled=False)`，
  `_require_trusted` 在未启用或未绑定时一律 `USER_KEY_UNBOUND`。`login_key.py` 文件头：
  “trusted binding issuance is not available yet, so a bound bit alone is not evidence”，登录后
  状态恒报 `unbound`。`purposes.py` 里 `orgfs.commit-as-user`、`org.leave` 等只是保留名，
  `fields=None`，不能签。`verifier.py` 的 `OwnerKeyPort` 需要注入 issuer、JWKS、binding 与吊销
  来源，生产装配未接（“production composition stays off”）。
- 结论：本次重建**不能**把签名作为强制准入条件。B.3 的做法是在安全记录的格式里预留签名槽位、
  把校验做成可注入的端口（今天注入的实现返回“不可用”，此时只执行 §0.7 的 (a)）；binding 签发
  （U2）就绪后再打开强制，不需要改磁盘格式与协议。

### 0.4 `composition/directory.py` 与 journal 基本无关，应保留 [码]

它做的是：订阅 `org/fs/announce/*` 与 actor liveliness，维护 `_holders`（谁持有哪个空间）、
离线节点公告的有界缓冲、presence 上线沿触发的补拉、transport 重建后的补拉、每 60 s ±20% 的
org 目录 / ACL 空间反熵（按持有方名字轮转）、设备归属索引。它对同步层的全部依赖是：

- `mesh.schedule_sync_from(peer) -> bool`
- `mesh.announce()`
- `store.expire_pending()`、`store.take_drained()`、`facade.apply_envelope(...)`
  （`directory.py:448-451`，反熵 tick 里清 pending；新模型下换成 state store 的 pending 清理）
- `self._replicas`（resident 角色，Q3 已定删除；`_post_rebuild_effects` 与
  `_announcement_spaces` 里的 `resident` 分支随之删除）
- `facade.watch_meta`（A4 收为内部接口）

所以新 sync 只要提供 `schedule_sync_from` 与 `announce` 两个入口，`RuntimeDirectory` 可以原样
复用。评估文档 §2 把它列为“保留”是对的。

### 0.5 `network/` 里与 journal 无关、新 sync 应当沿用的部分 [码]

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| supplier 健康与退避 | `supplier_health.py` 全文 | 按节点的失败计数、指数 hold、全局 / 空间本地失败分类、每 supplier 一个合并的调度任务；不碰 journal |
| 入站背压 | `mesh.py:191-232`、`ingress.py:107-160` | 订阅回调只入队（容量 128），由 actor + effect lane 串行处理；满了计 `ingress_dropped` |
| 单工作线程与关闭排空 | `mesh.py:184-188,338-393` | sync 与 blob 任务共用一个 worker；close 有截止时间 |
| blob 拉取 | `sync.py:688-774`、`recovery.py:552-755`、`queries.py:757-816` | 按摘要广播查询、分块、非持有方的“没有”不算决定性应答；与文档模型无关 |
| 公告 | `ingress.py:233-279` | 节点级公告，列出本节点是成员的空间 |
| key 布局 | `transport/keys.py:186-231` | `orgfs_sync(space, node)`、`orgfs_blob(digest)`、`orgfs_announce(node)` 可沿用；`orgfs_log*`（含 writer、seq）随 journal 删除 |

### 0.6 `network/` 里随 journal 删除的部分 [码]

- `sync.py`：`_stream_sync_from`（按 journal rowid 游标分页，游标落盘在 `sync-cursors/`）、
  `_legacy_sync_from`（按 version vector，但回的是信封）、`_install_sync_snapshot`
  （retirement / snapshot barrier）、`sync_log_range_from`（按 writer/seq 取范围）、`probe`
  （holder frontier，Q3 删）。
- `queries.py`：`_stream_sync_page`、`_sync_page`、`_document_page`、`_replica_sync_page`、
  `_handle_log_range`、`_frontier_reply` 以及两套游标编码。
- `recovery.py`：projection 修复（`_repair_projections` 等，约 430 行）。它存在的原因是 store 与
  facade 各持有一份 Doc：信封先进 store，再“投影”到 facade，投影会失败、要重试。目标形态每个
  文档只有一个实例，这一整层没有对象。
- `ingress.py`：`broadcast_records`（按 writer/seq 发 journal 记录）、replica 挂接窗口
  （`attach_replica`、`refresh_replica`、`_complete_replica_attach`、`_store_replica_envelope`、
  `_mirror_replica_envelope`，Q3 删）。
- `protocol.py`：`_decode_update_header`（writer、seq、origin、metaFrontier）、与 log range /
  replica / projection 相关的常量。

### 0.7 对阶段 B 各项的直接影响

- **B.3**：不存在“已认证的对端”。可选的只有 (a) 沿用现状强度：节点门（设备记录所属用户是当前
  成员）加对**结果状态**的校验；(b) 在此之上对单作者的安全记录做内容签名。owner 2026-11-25 已同意
  (a)+(b) 的方向；按 §0.3，(b) 本次只能做到“格式与端口就位、默认不强制”。
- **转发使帧级作者失效**：按 state vector 回补的差量里混有多个作者的修改，转发节点无法也不应
  替原作者署名。因此授权判断只能落在两处：节点门（这一跳的对端是不是当前成员的设备）与
  **应用后的结果状态**（文档内作者集合、受保护路径规则、成员表不变量）。帧里不再携带
  `origin.author` 这类逐更新的作者。这与现状强度相同（现状的 `origin.author` 也是自报的）。
- **B.3 的一个此前没列出的对象**：meta 文档里的成员表本身。成员增删只应由 owner 做；现状靠自报
  的 `origin.author == owner` 判断，同样可伪造。Q2“按当前成员表拒绝”的可信度取决于成员表不被
  普通成员节点改写，所以成员表应与四类安全记录同等对待（owner 签名）。
- **B.4**：`RuntimeDirectory` 的调度与发现、supplier 退避、入站背压、blob 通道都保留；新协议只
  需要定义 sync 请求 / 应答与实时广播两种帧。
- **B.1**：`OrgFs` 之外，orgfs 包内还需要一个窄的内部接口供 `RuntimeDirectory` 使用
  （`watch_meta`、pending 清理），A4 正在做前者。

## 1. 收窄后的公共面（B.1）

### 1.1 `OrgFs` Protocol

保留（签名不变，除非另注）：`create_space`、`spaces`、`space_meta`、`update_space_meta`、
`invite`、`remove_member`、`members`、`join`、`resolve`、`stat`、`listdir`、`read_text`、
`read_text_snapshot`、`write_text`、`read_bytes`、`write_bytes`、`mkdir`、`move`、`watch`、
`trash`、`checkout`、`document_authors`。

改动：

| 方法 | 改动 |
| --- | --- |
| `remove(space_id, node, *, permanent=False)` | 新增 `permanent`；语义见 §6 |
| `restore(space_id, node, *, recursive=True)` | 去掉 `version` 参数；只恢复被删节点的最后状态 |
| `status(space_id) -> SpaceStatus` | `SpaceStatus` 改为 `space_id`、`synced: bool \| None`、`holders_online`、`content_pending: int`、`rejected_updates: int`。`synced` 的定义见 §4.6 |
| `read_text_snapshot().content_revision` | 保留。取值为内容文档 state vector 的规范编码（按 client 排序）的摘要；跨重开不变，内容改回原值后仍然变化（§3.3 的提交时钟保证），迁移围栏依赖这一点（C7 清单 §5.3 第 12 项）|
| `document_authors(space_id, node)` | 保留名字与返回类型，语义改为“文档内作者集合的当前值”（§3.3）。四个调用方因此不用改调用点 |

删除：`open_doc` 及 `OrgDoc`/`DocMap`/`DocList`/`DocText`（A2）；`history`、`HistoryEntry`
（A3）；`journal_history`、`read_at`、`stat_at`、`JournalHistory`、`JournalCommit`（A1）；
`watch_meta`（A4，收为内部）；`serve`（A5）；`parked_commits`（并入 `status.rejected_updates`
与日志）；`document_covers_member_add`（§5）；`purge_plan`、`purge`、`purge_status`、`unban` 及
`PurgeTarget`/`PurgePlan`/`PurgeResult`/`PurgeStatus`（§6 就位后）。

`NodeInfo` 字段不变。`version` 仍是不透明字符串，只用于 `expect_version` / `base_version` 的
相等比较。现状它由“进程内的 tree 修订号 + 内容 state vector”组成（`document/model.py:438`），
修订号重启后从 0 重新计数，所以 daemon 重启后第一次带旧 version 的条件写必然 `stale-write`
（C4 实测）。新实现要求 **`version` 跨正常重开不变**：由该节点 tree 行的规范序列化与内容 state
vector 的规范编码共同取摘要，不含任何进程内计数。同一节点状态在不同节点上得到同一个 version。`content_state` 保留 `arrived` / `pending`，去掉 `unverifiable`（它只在 retirement
路径产生）。

错误码：删除 `snapshot-barrier`、`log-key-conflict`、`purged`、`stale-plan`、
`no-holder-online` 中只由被删路径产生的那些（逐个在 E 阶段按实际引用确认后删）；新增
`ORGFS_LAYOUT_UNSUPPORTED`（§2.5）。它只在线路层（请求方法的错误声明）声明，不进 `ORGFS_ERROR_CODES`：那张表只收小写的
OrgFS 内部错误码。

### 1.2 请求方法、CLI、MCP

| 现有 | 处置 |
| --- | --- |
| `orgfs.read_at`、`orgfs.stat_at` | 删（A1） |
| `orgfs.history`、CLI `fs history`、GUI 历史面板 | 删（A3） |
| `orgfs.serve`、CLI `fs space serve` | 删（A5） |
| `orgfs.purge_plan`、`orgfs.purge`、`orgfs.purge_status`、`orgfs.unban`、CLI `org fs purge plan` / `status` / `unban` 与 `purge run` 的 `PLAN_ID` 参数 | 删（D4a，已完成）。`org fs purge run SPACE [--yes]` **保留**：它调用的是 `orgfs.space_purge`，是本机空间清除唯一的 CLI 入口（第 1、2 版写“`fs purge *` 全删”有误，D4a 更正）。`contract/orgfs/local-space-purge.md` 里的命令写法早已过时，阶段 E 更新 |
| `orgfs.remove`、CLI `fs rm` | 增加 `permanent` 参数 / `--permanent` |
| `orgfs.restore`、CLI `fs trash restore` | 去掉 `version` |
| `orgfs.status`、CLI `fs space status` | 结果字段按 §1.1 |
| 其余 `orgfs.*` | 不变 |

### 1.3 包内接口（不属于 `OrgFs`）

- `MetaWatch`：`watch_meta(space_id, callback) -> Registration`，供 `RuntimeDirectory` 的设备
  归属索引失效使用。
- `StateStore`（§2.6）、`Admission`（§3.6）、`SpaceSync`（§4.7）：阶段 C 三个工作包各自实现的
  接口，也是 C1、C2、C3 之间唯一的耦合点。

## 2. 磁盘布局与持久化（B.2）

### 2.1 目录

```
state/orgfs/spaces/<spaceId>/
  space.json                 {"layoutVersion":3,"spaceId":…,"createdAt":…}
  batch.pending              只在一次多文档提交进行中存在（§2.4）
  meta.state   meta.delta
  tree.state   tree.delta
  docs/<stem>.state   docs/<stem>.delta      每个内容文档一对；stem 沿用 document_file_stem(doc_id)
  blobs/…                                     不变
```

没有 `journal.sqlite3`、`doc_checkpoints`、`sync-cursors/`、`*.loro`。每个空间只有一个 tree
文档，doc id 固定为 `tree`（现状的多代 `tree-<uuid>` 来自 retirement，随之消失；“成员另起一棵
tree 覆盖活动 tree”因此不再可能）。`spaces/` 下的非 UUID 目录在启动时照旧以
`ORGFS_INVALID_SPACE_DIRECTORY` 拒绝。

### 2.2 文件格式

- `*.state`：8 字节魔数 `ORGFSST3`，后跟一个 Yjs v1 update（整个文档的状态）。只通过“写临时
  文件、fsync、rename、fsync 目录”整体替换。
- `*.delta`：8 字节魔数 `ORGFSDL3`，后跟若干条记录；每条 = `u32 长度（大端）` + `u32 CRC32`
  + `长度` 字节的 Yjs v1 update。只追加。
- `batch.pending`：8 字节魔数 `ORGFSBT3`，后跟一个 `u32 长度` + `u32 CRC32` + JSON
  （`{"appends":[[docId, b64 update], …], "deletes":[docId, …]}`）。整体写入、rename 生效。

### 2.3 读取与合并

- **读取（打开文档）**：读 state，再顺序重放 delta 中 CRC 正确的记录。遇到长度越界或 CRC 不符的
  记录，视为撕裂的尾部：在该记录起始处截断 delta、fsync，并记 `orgfs.state.delta-truncated`。
  `load()` 返回的是“state 与全部有效 delta 合并后”的一个 update。
- **合并**：某文档的 delta 达到 500 条或 1 MiB（10-29 决定 1）时：把完整状态写成新的 state
  （临时文件 → fsync → rename → fsync 目录），再把 delta 截断为只剩魔数并 fsync。两步之间崩溃时
  delta 里的记录已全部包含在新 state 里，重放是幂等的。合并与提交都在该空间的串行执行者上，
  不存在并发。
- **完整状态从哪来**：store 不持有 `Doc`。合并时由调用方（facade 的提交协调者，§2.5）把权威实例
  的 `get_update()` 交给 `compact(doc_id, state)`；文档当前未加载时，store 用一个一次性 `Doc`
  （只 `apply_update`、只导出，从不产生操作）自己算。权威实例与一次性 Doc 都开启 GC。审查实测
  25,000 行、19 MB 的合成 tree 加载并导出约 12 ms、峰值内存增量约 49 MiB；真实数据的数字由 C6 测。
- **启动**：不回放、不校验 checkpoint、不重写文件。打开空间时先只读地校验 `space.json` 与布局
  （§2.7，被拒绝的目录不做任何改动），通过后做 §2.4 的批次恢复，再读 meta、tree；内容文档在第一次被读写或被同步时才加载。启动成本与内容文档数无关。

### 2.4 提交批次（唯一的写入口）

所有写入都经 `StateStore.commit(batch)`，一个批次 = 若干 `(doc_id, update)` 追加 + 若干
`doc_id` 删除。返回即已持久化，且**整批原子**：

1. 批次只有一条追加、没有删除：直接追加到该文档的 delta 并 fsync。
2. 其他情况：把整个批次写成 `batch.pending`（临时文件 → fsync → rename → fsync 目录）；按顺序
   执行各追加（各自 fsync）与各删除（unlink 两个文件、fsync 目录）；最后 unlink
   `batch.pending` 并 fsync 目录。
3. **恢复**：打开空间时若存在 CRC 正确的 `batch.pending`，把其中的追加与删除全部重做一遍再
   unlink。Yjs update 幂等，重复追加无害；删除一个已不存在的文件无害。CRC 不对的
   `batch.pending` 说明 rename 之前就崩溃了，批次未生效，直接删除。

由此：新建文件（内容 + tree）、写已有文件（内容 + tree 行里的 `contentFrontier` / `refSize`）、
永久删除（tree 更新 + 内容文件删除）都不会留下半个批次。`commit` 抛错时一条都没有生效或将在
恢复时全部重做，调用方按“未确认”处理（§2.5）。

**提交失败后的进程内语义**：`batch.pending` rename 之前失败（或单条追加失败且已回滚）时什么都
没有生效，store 继续可用。rename 之后失败、或单条追加的回滚本身失败时，批次可能已部分落盘：
store 进入失效状态，之后的所有调用抛 `StateStoreBroken`，直到关闭并重新打开（恢复会重做整批）。
`deletes` 只能是内容文档，含 `meta`、`tree` 或空串时整批以 `ValueError` 拒绝。

### 2.5 提交协调者（facade 内，阶段 D 实现）

本地写入与被接受的远端更新走同一条路径，全部在该空间的串行执行者（现有 `_SpaceStateOwner`）上：

1. 在权威 `Doc` 上执行修改（本地写入）或 `apply_update`（远端，已过准入）。本地修改在同一个
   Yjs 事务里把该文档的提交时钟 `__orgfs__.c` 加一（§3.3），得到增量 update。
2. `StateStore.commit(batch)`。失败：本地写入按现有回滚快照（`document/projection.py:119-201`）
   恢复内存状态并向调用方报错；远端更新则丢弃该空间受影响文档的内存实例，下次使用时从磁盘重新
   加载。遇到 `StateStoreBroken` 时，协调者丢弃该空间的**全部**权威 `Doc` 与暂存副本，关闭并
   重新打开 store，再从 `load()` 重建。
3. 成功后才：更新物化视图、发 watch 事件、调用 `SpaceSync.publish`（仅本地写入）。广播因此不可能
   先于持久化。

合并由协调者在 `needs_compaction()` 为真后触发，store 不自动合并。传给 `compact(doc_id, state)`
的 `state` 必须是权威实例在“没有未提交修改、且已整合全部已提交 update”时的 `get_update()`；
store 无法廉价校验这一点，给了过期的 state 会丢掉 delta 里多出的记录。

每个空间每次进程启动生成一个随机 53 位 client id，facade 持有的所有权威 `Doc` 都用它；不落盘、
不登记。`writers` / `peerIds` 不再存在。代价：每个写过某文档的 client 在该文档的 state 里约占
37 字节，并进入它的 state vector（meta、tree 的向量随每次同步请求发送），所以 tree 的向量随
“曾经写过该空间的进程启动次数”增长（C6 实测）。不复用 client id：崩溃或从备份恢复后复用同一个
id 会与对端已有的同 id 操作冲突，损坏文档；这点增长记入 known-problems。

**单实例约束的范围**：会产生操作的 `Doc` 每个文档只有一个，由协调者持有，放在按空间的文档
注册表里（内容文档 LRU，初值 256；正在执行的操作 pin 住它用到的文档，pin 住的不淘汰）。其他
读者（checkout、webserver、watch 回调、准入的暂存副本）拿到的是不可变的值（文本、字节、物化
行），或只应用、不产生操作的副本。

### 2.6 `StateStore` 接口（C1 的冻结点）

```python
@dataclass(frozen=True, slots=True)
class Batch:
    appends: tuple[tuple[str, bytes], ...] = ()   # (doc_id, Yjs v1 update)，按给定顺序
    deletes: tuple[str, ...] = ()                 # 永久删除的内容文档

class StateStore(Protocol):
    space_id: str
    recovered_batch: bool                              # 打开时是否重做过 batch.pending
    def load(self, doc_id: str) -> bytes | None: ...   # state 与有效 delta 合并后的一个 update
    def commit(self, batch: Batch) -> None: ...        # 整批原子；返回即持久化
    def needs_compaction(self, doc_id: str) -> bool: ...
    def compact(self, doc_id: str, state: bytes | None = None) -> None: ...
    def close(self) -> None: ...
```

`doc_id` 取 `"meta"`、`"tree"` 或内容文档 id。线程模型：一个空间的所有调用都来自该空间唯一的
串行执行者，store 自身不加锁、不起线程。没有 `sweep_orphans`（§6）。没有 `document_ids()`：内容
文件名是 `document_file_stem(doc_id)`（单向），文件里不存 doc id，store 不能也不需要反向枚举；
空间里有哪些文档以 tree 为准，store 只按给定的 doc id 定位文件。

### 2.7 旧布局

有 `space.json` 且 `layoutVersion == 3` 才打开。目录里存在 `journal.sqlite3` 而没有 v3 的
`space.json`，以 `ORGFS_LAYOUT_UNSUPPORTED` 拒绝该空间（其他空间照常），详情指向迁移说明
`docs/guides/migration-orgfs-minimal.md`（E4 编写）。不写迁移代码，不改动旧目录。详情里的
`reason` 区分原因：`legacy-journal`（旧布局）、`unknown-space-json`（`space.json` 不是 v3 对象）、
`space-id-mismatch`（`spaceId` 与目录名不符）、`corrupt-file`（魔数不认识，或 CRC 正确但形状不对
的 `batch.pending`；带 `path`）。这几种情况同样只拒绝该空间、不改写任何文件。`ORGFS_LAYOUT_UNSUPPORTED`
在阶段 D 接入 daemon 时登记进错误码表（`src/hyprial/error/codes.py` 与 kernel 的再导出），并重生
相关生成物。

## 3. 成员、作者与远端准入（B.3）

### 3.1 约束与保证强度

1. 没有已认证的对端身份；节点名与用户名都是自报的，现状亦然（§0.1、§0.2）。
2. 按 state vector 回补的差量混有多个作者且经过转发，帧级作者没有意义。
3. 签名今天不能强制（§0.3）。

**保证的表述（第 2 版按审查更正，C2 报告后再限定）**：对“如实自报身份”的节点，本设计拒绝的
写入与现状相同，但执行的位置分两类：

- 非路径所属用户对受保护记录的写、非 owner 对成员表与 `space` 根的改动：在写入节点本地被拒
  （§3.4），并在每个接收节点依据结果状态再被拒一次（§3.6）。
- 只读成员与非成员的写：只在写入节点本地（§3.4）与第一跳的节点门（§3.5，只看节点所属用户是否
  是成员，不看 `mode`）被拒。接收节点**不**依据内容里的作者再查当前成员资格（§3.6“不做的校验”），
  所以这类写入一旦被某个成员节点合并，转发后不再被拦。如实的只读成员节点本地就不会产生写入；
  剩下的情形是“尚未得知自己被移除的节点”，即 Q2 已接受的后果。C7 清单 §5.3 第 1 项因此只在
  本地与节点门两处验收。
- **被降为只读的成员**在得知降级之前写的内容会被接受：它的节点仍是成员的设备，过得了节点门。
  旧实现同样接受（C4 在旧实现上实测），不是回归；阶段 E 记入 known-problems。被**移除**的成员在
  分区期间的写入由节点门拒绝，与旧实现一致。对“伪造自报身份”的节点，现状与本设计同样不设防。两者的
差别只在证据的位置：现状是每个信封里的 `origin.author`，本设计是写进文档与 tree 行、随状态
传播的作者字段。要让这个等价成立，§3.3 的作者字段必须覆盖每一次本地写入，§3.6 的准入必须
从真实基线出发比较；这两点是 C2 的验收条件。

### 3.2 成员表

meta 文档的 `members` 根是 Map：`user -> {"mode","addedBy","addedAt"}`；`space` 根含 `owner`、
`kind`。成员增删只由 owner 在本地做（`invite` / `remove_member` 校验调用者是 owner，否则
`not-owner`）。没有 meta frontier、成员回放、`member_add_frontiers`。

### 3.3 作者字段与提交时钟（Q1）

- **内容文档**有两个保留根：`__orgfs__`（Map），其中 `c` 是整数提交时钟；`__orgfs_authors__`
  （Map），键是 `principal`、值为 `true`。作者集合用独立的根而不是嵌套在 `__orgfs__` 里：两个
  副本并发创建同一个嵌套 Map 时合并只留一个、另一边写入的作者会丢（C2 实测），根级类型没有这个
  问题。
- **tree 行**的线路格式新增 `author` 字段（最近一次修改该行的本地作者）。现状 tree 行不保存
  作者（`document/model.py:657-678`），`NodeInfo.modified_by` 来自信封、重开后被置为 owner
  （`operations/spaces.py:291-313`）；之后 `modified_by` 改为读这个字段，重开后保持。二进制文件
  的作者就是它所在 tree 行的 `author`。
- **tree 与 meta 文档**各有 `__orgfs__.c`。meta 的 `space` 根另有 `modifiedBy`（最近一次修改
  `space` 根的本地作者）。
- **tree 的根行**由 `create_space` 的创建者经提交 helper 写入；加入空间的节点打开空 tree 时不得
  自行写根行（现状 `TreeDocument.__init__` 会在任何空 tree 上写一个不带作者的根行，阶段 D 改掉），
  否则加入方与 owner 会对同一个键并发写。
- **时钟只在需要时才加。** 无条件地每次提交都改写 `c`，会在文档里每次留下约 13–17 字节合并也
  收不回的墓碑（与正文覆盖交错时 Yjs 无法合并相邻的删除记录），随提交数线性增长、没有上界
  （C6 实测）。普通写入本身就产生新结构、必然推进 state vector，不需要时钟；只有纯删除的提交
  （把文本删短、删除 Map 键）不推进。因此 helper 在完成修改后比较该文档的向量（解码后）：
  **没有前进时才把 `c` 加一**。增长于是只与纯删除提交的次数成正比。
- **唯一的本地提交 helper**（§2.5 第 1 步）对它修改的每个文档：按上一条在需要时把 `c` 加一；
  内容文档再把本地作者加入 `__orgfs_authors__`；tree 文档把被修改的行的 `author` 置为本地作者；
  meta 文档在修改 `space` 根时把 `modifiedBy` 置为本地作者。时钟的数值不用于比较（并发写入会让
  它停在同一个值甚至变小），它的作用只是保证每次提交都产生结构、从而推进 state vector。facade 的
  所有写入口（`write_text`、`write_bytes`、`mkdir`、`move`、`remove`、`restore`、
  `update_space_meta`、`invite`、`remove_member`、`create_space` 的初始化）都只能经它产生 update。
  C4 用一条遍历这些入口的契约测试钉住“每次写入后被修改文档的 state vector 严格前进”。
- 作者集合只增不减。`document_authors()` 返回 `__orgfs_authors__` 的键（排序）。
- state vector 的比较一律在解码后的 `{client: clock}` 上做，不比较原始字节（计划 §10）。

### 3.4 本地写入校验（Q1、Q2）

在 `_SpaceStateOwner` 上、产生 update 之前：调用者是当前成员且 `mode == "rw"`，否则
`not-a-member`；受保护目录路径（identity 域的 `protected_directory_author_allowed`）要求调用者是
该路径允许的作者，否则 `protected-path`；成员表与 `space` 根只有 owner 能改，否则 `not-owner`。
`remove(permanent=True)` 对受保护路径执行同一条规则。

### 3.5 节点门（Q2 在这里执行）

每个入站帧先过节点门，`peer` 是这一跳的对端节点名（拉取时是被拉的 supplier，广播时是帧里自报的
`node`）。沿用现状：节点在线（presence），且其设备记录所属用户是当前成员
（`_device_owner_index`）；`trust_tailnet_members` 时跳过后者。被移除成员的设备在每个已得知移除
的节点上都过不了门。**不判断某次写入是否产生于移除之前**（Q2）：移除前已被某个现任成员节点合并
的内容会随该节点的状态继续传播；这是 Q2 决定的直接后果，写入 known-problems。

### 3.6 结果状态准入（`Admission`，C2 的冻结点）

**基线加差量，不用空探针。** 审查实测：差量 update 应用到空 `Doc` 后，对已有 Map 键的替换与
删除都读不出来。因此准入在**暂存副本**上做：

- 协调者为 meta 与 tree 各维护一个暂存 `Doc`（只应用、从不产生操作），不变量是“内容等于权威
  实例”。内容文档的暂存副本按需从权威实例导出，用完即弃。
- 准入步骤：记下暂存副本的 state vector 与相关物化值 → 把 update 应用到暂存副本 → 得到
  “之前 / 之后”两份物化值 → 按下表判断。
- **缺前驱**：帧里带着发送方应用该 update 之后的 state vector（§4）。应用后暂存副本的向量没有
  覆盖它，说明 update 引用了本地没有的结构（Yrs 只把它放进内存 pending，状态导出不包含）。此时
  结果是 `Pending("dependency")`：不应用到权威实例、不落盘，暂存副本从权威实例重建，由
  `SpaceSync` 立即向同一对端按本地向量拉取完整差量。**不设持久 pending 队列**：未整合的 update
  从不被接受，因而不会在合并或重启时丢失。
- **内容先于 tree 行到达**：内容文档的 update 若在本地 tree 里没有对应的文档行，结果是
  `Pending("unknown-doc")`，同样不落盘，等 tree 同步后重试。磁盘上因此不会出现没有 tree 行的
  内容文件。
- `Accepted`：协调者把同一个 update 应用到权威实例并提交（§2.5）。`Rejected` /
  `Pending`：暂存副本被污染，从权威实例重建（meta、tree 的重建成本即一次导出加一次加载）。
- **导出即换实例。** 缺前驱只能靠帧里自报的结果向量判定，发送方可以谎报一个较小的向量，让含
  缺前驱结构的 update 被接受；这些结构停在 `Doc` 的内存 pending 里，导出不包含。为了让它等同于
  “从未被接受”，并保持内存、磁盘、暂存副本三者一致：凡是导出权威实例的状态时（重建暂存副本，
  或为合并导出，§2.5），协调者同时把权威实例换成从这份导出加载的新实例（同一个 client id）。
  这样滞留的结构在三处同时消失，其前驱日后到达时不会绕过准入被整合。

| 文档 | 比较的内容 | 规则 | 结果 |
| --- | --- | --- | --- |
| meta | `members` 各行的之前 / 之后 | 新增、修改的行格式合法且 `addedBy == space.owner`；`space.owner` 自己的那一行不得被删除；其他行的删除不限制（只有 owner 的 facade 会产生，伪造者不设防，见 §3.1）| `Rejected("not-owner")` |
| meta | `space` 根的之前 / 之后 | `owner`、`kind`、`spaceId`、`createdAt` 的已有值不得改变；`space` 根有任何变化时，变化后的 `modifiedBy` 须等于 `owner`（`name`、`aclSpaceId` 等因此只有 owner 能改）| `Rejected("not-owner")` |
| tree | 发生变化的节点行 | 行格式合法（现有 `_tree_rows(strict=True)`，且行里的 `node_id` 等于它的键）；行的 `author` 非空；变化涉及受保护目录（变化前或变化后的路径在其下）时，行的 `author` 须是该路径允许的作者；合并后无环（现有规则：把后到的移动判负，不拒绝整个 update）| `Rejected("protected-path")` / `Rejected("invalid-argument")` |
| tree | 被删除的行（键消失）| 对应路径在受保护目录下时，同上按最后一个 `author` 的规则不可判断，因此受保护目录下的行**不允许被远端永久删除**，只能软删除（`deleted: true`，行仍在、`author` 可查）| `Rejected("protected-path")` |
| 内容 | `__orgfs_authors__` 的之前 / 之后 | 不得减少；文档位于受保护目录时，新增的作者须是该路径允许的作者 | `Rejected("protected-path")` |
| 内容 | state vector 的之前 / 之后（解码后）| 正文或作者集合有变化时，向量须严格前进。如实节点每次提交都写 `__orgfs__.c`，必然满足；不前进说明是纯删除或没有经过提交 helper。**不比较 `c` 的数值**：两个节点在同一基线上各写一次都会把它写成同一个值（C2 实测），按数值比较会拒绝并发的如实写入 | `Rejected("invalid-argument")` |
| 内容 | 正文 | 不超过 `ORGFS_TEXT_MAX` | `Rejected("too-large")` |
| 任何 | — | update 无法解码 | `Rejected("invalid-argument")` |

不做的校验：不要求 tree 行的 `author` 或内容的 `authors` 是**当前**成员。新加入的节点第一次同步
拿到的是整个状态，其中必然有已离开成员的历史内容；按当前成员表校验内容作者会让空间永远同步
不下来。成员资格只在节点门与本地写入处校验。

```python
@dataclass(frozen=True, slots=True)
class Accepted: ...
@dataclass(frozen=True, slots=True)
class Pending:
    reason: Literal["dependency", "unknown-doc"]
@dataclass(frozen=True, slots=True)
class Rejected:
    code: str                       # OrgFsError 的错误码
    detail: Mapping[str, object]

class AdmissionView(Protocol):      # 协调者提供的一致只读视图，取自同一个串行执行者
    space_id: str
    space_name: str                 # 受保护目录规则只在 org 目录空间启用
    owner: str
    def member_mode(self, user: str) -> str | None: ...
    def tree_row(self, node_id: str) -> Mapping[str, object] | None: ...
    def node_for_doc(self, doc_id: str) -> Mapping[str, object] | None: ...
    def path_of(self, node_id: str) -> str | None: ...

class Admission(Protocol):
    def admit(self, doc_id: str, update: bytes, expected_vector: bytes,
              staged: "StagedDoc", view: AdmissionView) -> Accepted | Pending | Rejected: ...
```

`StagedDoc` 是 C2 定义的小接口：`vector()`、`apply(update)`、按文档种类读物化值。C2 同时交付
一个测试工具 `forge_update(kind, mutate, *, author, client_id)`，用来构造如实节点不会产生的
update（改别人的行、不加作者、不推进时钟、删成员行），C7 清单 §5.3 第 1–4 项与 68 条“需改写”
用例据此改写为经准入入口的测试。

### 3.7 被拒绝的更新

拒绝时不应用、不落盘，计入该空间的 `rejected_updates`，记 `orgfs.update.rejected`（`docId`、
`code`、`peer`）。差量同步下对端每次都会再次提供同一段内容，所以：

- 协调者保留一个有界 LRU（每空间 1024 项），键为 `(doc_id, sha256(update), 策略代次)`。命中时
  静默丢弃，不重复告警、不重复计数。
- **策略代次** = meta 文档当前 state vector 的规范编码摘要。成员表或 owner 一变，代次就变，
  旧的拒绝自动失效、重新评估；tree 里受保护行变化引起的结论变化，靠 update 本身的摘要不同来
  区分。
- 被拒绝的内容使该文档在本节点上无法与该对端收敛；`status.rejected_updates > 0` 让 owner 看得见；
  根治手段是在对端修正或永久删除该文档。这是没有逐提交历史的代价，已知并接受。

### 3.8 签名槽位（今天不强制）

单作者、整篇替换的安全记录（设备记录、leave 请求、member agent 清单、cell 镜像）与成员表行，
格式上允许携带 `sig` 字段：`identity.user_key` 的信封，payload 为
`{owner, spaceId, docId, contentSha256, issuedAtMs}`，purpose 用已保留的 `orgfs.commit-as-user`。
准入与读取方通过注入的 `RecordVerifier.verify(record) -> "verified" | "unsigned" | "invalid" |
"unavailable"` 判断。今天注入的实现恒返回 `unavailable`，此时只执行 §3.5–§3.6。binding 签发（U2）
上线后换成真实实现并把策略改为“安全记录必须 `verified`”，不需要改磁盘格式与同步协议。本次只做：
字段保留名、端口、默认实现、端口被调用的测试。预留的 `sig` 不用来论证今天的安全强度。

## 4. 同步协议（B.4，含 Q7）

### 4.1 key 与版本

`KeySpace`（`transport/keys.py`）新增三个方法，沿用 `_orgfs_segment` 校验，sync 实现不自己拼
字符串：

- `orgfs3_sync(space_id, node)` → `<prefix>/org/fs3/sync/<spaceId>/<node>`（query）
- `orgfs3_update(space_id)` → `<prefix>/org/fs3/update/<spaceId>`（put / subscribe）
- 公告 `orgfs_announce(node)` 与 blob `orgfs_blob(digest)` 沿用。公告体加 `"protocol": 3`；
  `RuntimeDirectory` 的公告解析只接受 `protocol == 3`（现状只校验 `schemaVersion`、`type`、
  `node`、`spaces`），其余公告忽略。

`orgfs_log*`、`orgfs_snapshot`、旧 `orgfs_sync` 的 key 方法在阶段 E 删除；切换后旧 key 无人
订阅、无人应答，由 C3 的契约测试钉住。所有帧是 JSON，`schemaVersion: 3`。

### 4.2 三类文档与“目录先到”

- **目录**：meta 与 tree。每次同步都交换。
- **内容**：`doc-*`。tree 的每个文档行携带该文档最近一次写入后的内容 state vector
  （现状已有的 `contentFrontier`）。本地内容的向量未覆盖它，就是 `content_state == "pending"`。

同步 tree 之后本节点就知道哪些内容落后，内容按两种方式补齐：

1. **后台补齐**：每轮同步在目录之后，按 tree 顺序取最多 128 个 pending 文档向同一 supplier
   请求；还有剩余就立即排下一轮（沿用现有的 rerun 机制）。空间最终整体到齐。
2. **读取优先**：读到 pending 的内容时，把该文档插到队首并触发一次同步；调用方得到现有的
   `ORGFS_CONTENT_PENDING` 或等待至超时（现有语义）。读取优先只针对内容文档（`doc-*`）。二进制
   文件的 blob 维持现状：facade 的 `read_bytes` 读到尚未取回的远端 blob 只返回 `content-pending`
   （带候选持有方），拉取由 runtime 的 `await_content` 与请求层驱动（经 `SpaceSync.request_blob`），
   facade 自己不触发。

**frontier 修正（必须，否则并发编辑会永久分叉）。** tree 行是 Map 里的一个整值，`contentFrontier`
按最后写者胜合并：两个节点并发编辑同一文档时只留下一方的 frontier，赢的一方本地内容覆盖它、
永远不认为自己落后，也就拉不到对方的编辑；广播丢一次就永久分叉，而 meta、tree 向量却相等
（C3 实测复现）。因此协调者做 frontier 修正：

1. **触发点**：应用远端**内容** update 之后检查该文档；应用远端 **tree** update 之后检查本次发生
   变化的文档行。
2. **条件**：本地内容向量未被该行的 `contentFrontier` 覆盖。
3. **写入值**：行的 `contentFrontier` 与本地内容向量的逐 client 最大值（两者之并），以一次本地
   tree 写入完成。这次写入不改 `author`，照常广播。写入的 frontier 恰好等于本地向量时，同时把
   `refSize`、`refSha256` 写成本地内容的值；否则（行里有本地还没有的内容）清空这两个字段，由
   之后拿到完整内容的节点在它自己的修正或写入里补上。

- 为什么是这三条（第 1 版只在应用远端内容后触发、写入本地向量，C3 用确定性复现与随机故障测试
  证明它不收敛：4 节点、80 次随机故障里 26–31 次分叉）：修正写与 tree 行的任何写一样按最后写者
  胜合并，写“本地向量”可能把行的 frontier 倒退到某个写者之下，而被倒退的写者恰好不会再收到远端
  内容、规则不再触发；只加 tree 触发而仍写本地向量则活锁（两个互不覆盖的节点反复互相覆盖）。
- 收敛与终止：行的值只因修正而单调增大（并），有上界（全部内容的并）。任何节点看到行超出自己
  → pending → 拉取；本地超出行 → 写并。所有节点最终本地向量都等于全部内容的并，此时条件不成立，
  不再写。C3 实测：同样的 80 次随机故障 0 次不收敛，收敛后再跑 5 轮没有新的 tree 写入。
- 代价：只在真正发生并发编辑并合并时多一次 tree 写。
- 只读成员的节点不做修正（它不能写）；并发编辑的双方都是 rw 成员，其中持有合并结果的一方会做。
- 没有选“请求里带全部内容文档的分桶摘要”：它需要所有内容文档的向量，要么启动时全部加载，
  要么另存一份索引，破坏“启动成本与内容文档数无关”。

两条相关的不变量：每次本地内容提交必须在同一个批次里更新 tree 行的 `contentFrontier`，否则
对端永远不会拉这次修改；新建文件的批次提交后先 `publish("tree")` 再 `publish(内容)`，否则每个
接收方都会先得到一次 `Pending("unknown-doc")` 并多拉一轮（不影响正确性）。

预算判断（Q7）：这是现有 hydration 行为的延续，新增的只是“pending 列表加分页请求”，**采用**，
不退回整空间同步。

### 4.3 拉取：请求与应答

请求：

```json
{"schemaVersion":3,"type":"sync","spaceId":"…","round":"<uuid>",
 "requester":{"node":"…","author":"…"},
 "vectors":{"meta":"<b64 sv>","tree":"<b64 sv>","doc-…":"<b64 sv>"},
 "maxBytes":<ORGFS_PAGE_BYTES>}
```

`vectors` 总是含 `meta` 与 `tree`，另加至多 128 个内容文档（本地没有的给空向量）。

应答：

```json
{"schemaVersion":3,"type":"sync-reply","spaceId":"…","round":"<uuid>","supplier":"…",
 "updates":[{"docId":"meta","update":"<b64>","vector":"<b64 sv>"},
            {"docId":"doc-…","blob":"<sha256>","size":123456,"vector":"<b64 sv>"}],
 "vectors":{"meta":"<b64 sv>","tree":"<b64 sv>"},
 "omitted":["doc-…"]}
```

- supplier 对请求里的每个文档算 `get_update(requester_sv)`，并附上自己该文档的当前向量
  （接收方用它判断缺前驱，§3.6）。本地向量被请求方的向量完全覆盖时不回该文档。
- 单个 update 超过页预算一半时不内联：supplier 把它作为临时 blob 放进 blob store（引用方标记
  `sync`，10 分钟后回收），应答给摘要与大小，请求方走现有 blob 通道分块取回并校验摘要。
- 装不下的文档列在 `omitted`，请求方下一轮再要。
- supplier 的准入：`requester.author` 是当前成员（现状 `_requester_allowed`），否则
  `not-a-member`。
- 请求方按 meta → tree → 内容的顺序把各 update 交给协调者；内容文档遇 `Pending("unknown-doc")`
  留到下一轮。
- 应答里的 `vectors` 是 supplier 的 meta / tree 向量，用于 §4.6；不做同一次往返里的双向交换，
  本地多出的内容由对方在它自己的下一轮拉取中取走。
- 错误应答：`{"schemaVersion":3,"type":"sync-error","spaceId":"…","round":"…","supplier":"…",
  "code":"…","message":"…"}`；请求帧无法解析时 `round` 为空串。

### 4.4 广播

本地提交持久化之后，协调者调用 `SpaceSync.publish`，向广播 key 发：

```json
{"schemaVersion":3,"type":"update","spaceId":"…","node":"…","docId":"…",
 "update":"<b64>","vector":"<b64 提交后的 sv>"}
```

超过 `ORGFS_ENVELOPE_BYTES` 的 update 不带正文：帧的其余字段照旧，只是没有 `update`。接收方：节点门 →
协调者（准入、应用、提交）。结果为 `Pending`，或帧不带正文且本地向量未覆盖 `vector` 时，对该
节点 `schedule_sync_from`。入站背压沿用现有的有界 ingress（容量 128，满则丢并计数；丢了的靠
拉取补）。节点重启前没来得及广播的本地提交，由对端的下一轮拉取取走（C7 清单 §5.3 第 15 项）。

### 4.5 删除与 state vector

Yjs 的 `get_update(sv)` 总是带上完整的删除集合，所以删除会随任何一次对该文档的差量应答到达；
“向量相等就不回”的优化成立的前提是每次写入都推进向量，由 §3.3 的提交 helper 与 §3.6 的时钟
规则保证。C3 的契约测试必须覆盖：纯删除的编辑在对端离线错过广播后仍能追上（C7 清单 §5.3
第 14 项）。

### 4.6 `status.synced`

协调者持有每空间单调递增的**本地提交代次**（每次本地提交加一）。`SpaceSync` 对每个在线持有方
保存一条观察：`(meta 向量, tree 向量, 观察时的本地提交代次)`，来自最近一次成功的应答；持有方
离线时该观察立即作废。`synced` 由协调者在串行执行者上计算：

- 没有在线持有方 → `None`（未知，不伪报为已同步）；
- 存在一个在线持有方，其观察的 meta、tree 向量都覆盖本地向量，本地的 meta、tree 向量也覆盖每一条
  在线观察（目录没有落后于任何在线持有方），且 `content_pending == 0` → `True`；
- 否则 `False`。

这里的“在线持有方”只算过得了节点门的节点，也就是当前成员的节点。被移除成员的节点即使在线，
它的观察也不参与计算，`holders_online` 也不列它。

`synced` 依据的是本节点已经拿到的观察。一个节点持有别人没有的更新、而它的广播丢了时，其余
节点在下一次与它交换向量之前（至多约一个 60 s 的定时 tick）仍会报 `True`。这个窗口不消除，
记入已知问题。

`content_pending` 只计内容文档（`doc-*`）的待取与未核对数。二进制文件按需取回，不计入，它是否可读
见 `stat().content_state`。

“覆盖本地向量”已经蕴含本地没有对方缺少的提交，所以不需要逐文档的 holder frontier（Q3）。

### 4.7 `SpaceSync` 接口（C3 的冻结点）

```python
class SyncHost(Protocol):                      # 由 facade 的提交协调者实现，SpaceSync 回调它
    def read_vectors(self, doc_ids: Sequence[str]) -> Mapping[str, bytes]: ...   # 不存在的文档给空向量
    def read_update(self, doc_id: str, since: bytes) -> tuple[bytes, bytes] | None: ...  # (update, 当前向量)
    def pending_content(self, limit: int) -> tuple[str, ...]: ...
    def apply_remote(self, doc_id: str, update: bytes, expected_vector: bytes,
                     peer: str) -> Accepted | Pending | Rejected: ...
    def requester_allowed(self, author: str) -> bool: ...
    def requester_removed(self, author: str) -> bool: ...                       # 成员行仍在且已移除：只回 meta
    def supplier_allowed(self, node: str) -> bool: ...                          # 节点门（§3.5）
    def local_member(self) -> bool: ...                                         # 本节点的作者是否是当前成员
    def local_generation(self) -> int: ...                                      # §4.6 的本地提交代次

@dataclass(frozen=True, slots=True)
class PeerObservation:
    node: str
    meta_vector: bytes
    tree_vector: bytes
    local_generation: int

class SpaceSync(Protocol):
    def schedule_sync_from(self, peer: str) -> bool: ...    # RuntimeDirectory 调用，语义同现状
    def announce(self) -> None: ...
    def publish(self, doc_id: str, update: bytes, vector: bytes) -> None: ...   # 只由协调者在持久化后调用
    def request_content(self, doc_id: str) -> None: ...     # 读取优先
    def request_blob(self, digest: str) -> Event: ...       # 二进制文件内容：合并排队的后台拉取，语义同现状 mesh.request_blob
    def fetch_blob(self, holder: str, digest: str, *, size_hint: int | None = None) -> bytes: ...  # 同步拉取并校验摘要
    def maintain(self, now: float) -> None: ...             # RuntimeDirectory 反熵 tick 调用；取代旧的 expire_pending / take_drained / apply_envelope
    def observations(self) -> tuple[PeerObservation, ...]: ...
    def close(self, timeout: float = 5.0) -> None: ...
```

构造参数：`TransportSession`、`KeySpace`、`space_id`、`node_id`、`author`、`SyncHost`、
`BlobStore`、`SupplierSyncHealth`（原样复用）、presence 判定回调、logger，以及两个可选回调
`announcement_source`（节点级公告的空间列表，由 runtime 提供，语义同现状）与 `holder_discovery`
（候选持有方，供读取优先与 blob 拉取选对端）。`KeySpace` 另加 `orgfs_blob_any()`（blob handler 的
通配 key）。supplier 侧只校验 `requester.author`、不对 `requester.node` 过节点门，与现状同强度。`SpaceSync` 自己不持有
`Doc`、不碰 `StateStore`。它负责：订阅广播 key 与声明 sync / blob 的 query handler、transport
重建后的重新声明、有界 ingress 与单工作线程、关闭排空（沿用 `mesh.py:338-393` 的截止时间语义）、
供应方退避、分页与 blob 回退、同步轮次的开始与结束。

`RuntimeDirectory` 需要的改动仅三处：反熵 tick 里的三行旧 pending 处理换成 `sync.maintain(now)`；
公告解析加 `protocol == 3` 过滤；去掉 resident 分支（A5 已做）。其余（公告缓冲、liveliness、
持有方发现、重建后补拉、设备归属索引及其对 trash 中已删设备记录的扫描）不变。

## 5. `document_authors` 与 `document_covers_member_add` 的调用方（B.5）

| 调用方 | 现状 | 之后 | 安全性质 |
| --- | --- | --- | --- |
| 设备记录校验 `org/network/directory.py:244` | `document_authors` 的集合须等于路径所属用户 | 调用点不变；读文档内作者集合（§3.3）| 对如实节点等价（§3.1）；路径规则另在准入处执行（§3.6）|
| member agent 清单 `org/network/member_agents/__init__.py:145` | `authors == {owner}` | 调用点不变 | 同上 |
| cell 镜像 `cells/runtime/mirror.py:162` | `verify(cell, authors=…)` | 调用点不变 | 同上 |
| leave 请求 `org/network/directory.py:454` `leave_request_authors` | 同上 | 调用点不变 | 同上 |
| leave 请求 `directory.py:460` `leave_request_covers_current_add` | `document_covers_member_add`：请求在因果上晚于该成员最近一次被加入 | 见下 | 目的等价，依据从 journal 因果序换成记录内的显式字段 |
| `org/orgfs_migration.py:301-327` `writer_attributions` | **不是旧迁移**（C5 更正）：`org.fetch` 在 `org.fetchSource=orgfs` 下用它把已标 deprecated 的节点形式来源 `--from agent:<node>` 映射到发布过的用户，并生成 `availableSources` 的节点列表 | 阶段 E 改由 org 目录的设备记录提供“节点 → 所属用户”映射（与节点门用的 `_device_owner_index` 同源），`org.fetch` 的对外行为不变 | 来源从“谁在该空间写过”变为“谁登记了该设备”；对如实节点两者一致 |
| 目录状态里的 `parkedCommits`（`org/network/directory.py:147,320`）| `parked_commits` | 字段删除；诊断改读 `status.rejected_updates` | — |

**leave 请求的 `memberAddedAt`（C5 的范围，按审查补全）**：

- 生产者：leave 请求的唯一生产入口（`daemon/impl/org/network/tailnet/__init__.py:137-141`，现有
  字段 `org`、`user`、`deviceId`、`requestedAt`）在写记录时读取本人当前成员行的 `addedAt`，写入
  `memberAddedAt`。
- 生产者读不到本人成员行时省略该字段、离开照常完成（此时设备行已删、tailnet 已 tombstone，
  抛错只会让移除流程卡在半途）；这样的请求不会被自动处理，owner 的清扫记 `org.leaves.stale`。
- 读取方：记录的格式校验要求该字段存在且为非空字符串；`leave_request_covers_current_add` 改为
  “`memberAddedAt` 等于该用户当前成员行的 `addedAt`”。字段缺失、类型不对或不相等都视为不覆盖。
- 这个字段与记录其余内容一样是自报的，强度与 §3.1 所述相同；它防的是把上一次成员期的旧 leave
  请求用到重新加入后的成员身上。
- 同步改：`contract/org-network/` 下描述 leave 请求格式的契约文档与对应契约测试。
- 这个比较依赖两次加入的 `addedAt` 不同。`addedAt` 是 owner 节点的微秒级时间戳，owner 时钟回拨
  到恰好相同的值时旧请求会覆盖新加入。阶段 D 的成员表 helper 在重新加入时保证新的 `addedAt`
  严格大于该用户上一次的值（相同或更小时取旧值加一微秒）；对现任成员改 `mode` 时保留原值。
- 调用方的“拒绝共写记录”测试：新实现下如实节点本地就写不出共写记录（§3.4），带着不允许的作者
  的 update 也过不了准入（§3.6）。所以这类测试的搭建改为用 C2 的 `forge_update` 直接写进
  `StateStore`（绕过准入），用来证明调用方这一层纵深防御仍在。

`document_authors` 在纯内存 facade 下的 `space.history` 兜底（A3b 报告 §4）随 `space.history`
一起删除，内存 facade 同样读文档内作者集合。C5 另需自行分拣 `tests/daemon/org/network/**`
（C7 未读）。

## 6. 永久删除、trash 与 GC（B.6）

- `remove(node)`（默认）：tree 行置 `deleted: true`，内容文档与 blob 引用保留。`trash()` 列出
  这些行，`restore(node, recursive=True)` 把该节点（及随它一起删除的后代）的标志改回。trash 里
  只有被删节点的最后状态；`restore` 不再接受 `version`，不能恢复被覆盖的旧正文。现状的
  `restore(node, version)` 读的是 `space.snapshots[version]`（`operations/tree.py:332`），阶段 D
  连同 `space.snapshots`、`space.history` 一起去掉。“随它一起删除的后代”用 tree 行里的删除批次
  标记判断（`remove` 给同一次删除的所有行写入同一个 `deletedBatch` 值），不依赖历史；GUI 文件夹
  恢复据此改接（计划 §9 的阶段 D 必做项）。
- `remove(node, permanent=True)`：调用者须通过 §3.4 的本地校验（受保护路径同样拒绝，报
  `protected-path`，C7 清单 §5.3 第 19 项），且是 owner，或是该节点 `author` 本人，否则报
  `not-owner`。效果：一个批次里（§2.4）从 tree 的
  `nodes` Map 删除该节点及全部后代的键，并删除这些节点的内容文档；提交后释放 blob 引用（现有
  blob 引用账本负责回收）。已在 trash 里的节点同样可以这样清除。
- **对端如何删除本地内容文件**：协调者在应用一个（已过准入的）tree update 时，比较物化行的之前 /
  之后；对“之前存在、之后键消失”的文档行，把它的内容文档放进同一个提交批次的 `deletes`。删除
  只由这个**被观察到的转变**触发，从不由“当前没有 tree 行引用”推断，所以 tree 不完整、分页
  中途、缺前驱都不会误删（审查必须改 4）。启动时没有孤儿清扫；批次恢复（§2.4）保证不产生
  孤儿，§3.6 的 `Pending("unknown-doc")` 保证没有 tree 行的内容不会落盘。
- GC：所有 `Doc` 用默认设置（GC 开启）。被覆盖的文本与被删除的键的**正文**在下一次合并后不再
  占用 state 文件；每个被删除的键仍留下约 15 字节的墓碑，被多次覆盖过的行删除后留下的更多
  （C6 实测：覆盖过 20 次的行约 137 字节）。这部分随删除次数增长，量级小，记入 known-problems。trash 依赖的是“tree 行还在、内容文档还在”，不依赖 Yjs 的历史结构，所以开启 GC
  不影响 trash / restore（计划 R5）。
- 保证的范围（Q4）：永久删除只作用于各节点的当前状态。离线节点上线、同步到 tree 之后才删除
  自己的副本；已导出的文件、备份、旧版本的 journal 目录不在范围内。不做全网确认与摘要封禁。

## 7. 阶段 C 的工作包与验收

| 工作包 | 实现 | 新包 / 独占目录 | 验收（除各自的单元测试外）|
| --- | --- | --- | --- |
| C1 | `StateStore`、`Batch`（§2.2–§2.6）：文件格式、批次与恢复、合并、撕裂尾部、旧布局拒绝、非 UUID 目录拒绝 | `orgfs/state/`、`tests/daemon/orgfs/state/` | C7 清单 §5.3 第 5、6、11、18 项：已确认的写在任意崩溃点后不丢（用故障注入逐个崩溃点测）；提交失败不留半个批次；253 字符 doc id 可持久化并重开 |
| C2 | `Admission`、`StagedDoc`、成员表读写 helper、本地写入校验函数、作者字段与提交时钟的读写 helper、`RecordVerifier` 端口、`forge_update` 测试工具（§3）| `orgfs/membership/`、`tests/daemon/orgfs/membership/` | C7 清单 §5.3 第 1–4、19 项与 §3.6 表格逐行：每条规则一条“如实节点被拒”和一条“合法写入通过”；缺前驱与内容先到返回 `Pending` |
| C3 | `SpaceSync`、帧编解码、分页、blob 回退、广播、观察（§4）；`KeySpace` 的三个新方法 | `orgfs/sync/`、`tests/daemon/orgfs/sync/`；`transport/keys.py` 只加方法 | 用进程内假 session（`tests/daemon/orgfs/mesh/test_orgfs_mesh.py` 的 `_Bus` 可复用）与一个内存版 `SyncHost` 测：C7 清单 §5.3 第 7–10、13–16、20 项（乱序、重复、丢帧、分区愈合后收敛；纯删除追上；大空间有界轮次内收敛且进度单调）|
| C4 | facade 行为契约测试与工厂：`contracts/conftest.py` 提供 `make_facade`、`make_runtime`（含 `reopen`）、`inject_write_failure`、`connect`，对 `legacy` 与 `state` 两个实现参数化；`state` 实现接好之前该参数标记为预期不可用 | `tests/daemon/orgfs/contracts/` | C7 清单里“契约-保留”中经 facade 的测试迁入并在 `legacy` 上全绿；新增 §3.3 的“每个写入口都推进向量”与 `content_revision` 的契约测试（先在 `legacy` 上按现状断言）|
| C5 | §5：leave 请求的 `memberAddedAt`（生产者、校验、判断）、两处诊断字段、`tests/daemon/org/network/**` 的分拣 | `daemon/impl/org/`、`cells/` 中对应文件及其测试 | 旧 leave 请求不能用于重新加入后的成员；字段缺失视为不覆盖 |
| C6 | 规模测试：打开、提交、status、同步一轮的工作量不随内容文档数与历史增长；2.5 万文档合成空间的打开时间与合并耗时 | `tests/daemon/orgfs/scaling_state/` | 以计数（读写的文件数、加载的文档数、应答的字节数）为断言，不以墙钟时间为断言；墙钟数字只记录 |

C1、C2、C3、C5、C6 互不写同一目录，可同时进行；C4 与它们也不重叠。C6 依赖 C1 的接口，可先按
§2.6 写测试、对一个最小的内存假实现跑通，C1 合入后换成真实实现。

阶段 D 把 `document/*`（facade）从 `OrgSpaceAuthority` + `LocalSpaceStore` + `OrgFsMesh` 改接到
这三个接口上，并实现 §2.5 的提交协调者与 §4.7 的 `SyncHost`。facade 的命令循环
（`_SpaceStateOwner`，含 generation 重启重放、waiter 结算、close 截止时间）、`TreeDocument` 的
移动与环保护与 name conflict、`_ContentDocument` 的三方合并与 `base_version` / `expect_version`
/ create-only、watch 与 post-commit effect、lazy hydrate、checkout、webserver、blob 存储与引用
账本、本地空间清除（含 crash-resumable intent）、import / export 不重写；这些是审查列出的
“必须保留的现有行为”，阶段 D 的验收以 C4 的契约测试在 `state` 实现上全绿为准。

## 8. 审查处理记录

审查：codex `gpt-6.2-sol`，2026-11-25，基线 `de8fdf7079`，报告
`orgfs-minimal-design-review-2026-11-25.md`。

| 审查项 | 结论 | 本文的处理 |
| --- | --- | --- |
| 必须改 1：空探针读不出已有 Map 键的替换与删除（pycrdt 实测）| 接受 | §3.6 改为“暂存副本 = 基线，加差量”，比较之前 / 之后 |
| 必须改 2：不设持久 pending 会丢缺前驱 update | 接受，取审查的第二种方案 | §3.6：未整合即 `Pending("dependency")`，不接受、不落盘，立即向同一对端拉完整差量；帧带发送方的结果向量用于判定（§4.3、§4.4）|
| 必须改 3：多文档追加不是原子的 | 接受 | §2.4 提交批次与恢复；`StateStore` 只留 `commit(batch)` 一个写入口 |
| 必须改 4：tree 不完整时孤儿清扫会误删 | 接受 | §6：只由“观察到的行消失”触发删除；取消孤儿清扫与 `sweep_orphans`；§3.6 内容先到返回 `Pending("unknown-doc")` |
| 必须改 5：tree 行没有持久作者字段；受保护路径准入失去作者证据 | 接受事实；对“安全强度不等价”的结论作了限定 | §3.3 给 tree 行新增 `author` 并由唯一的提交 helper 维护；§3.6 的规则逐行改为比较作者字段的之前 / 之后；§3.1 把保证改写为“对如实自报身份的节点等价，对伪造者两者都不设防”。审查举的例子（Bob 改 Alice 的 leave 请求）在本文下：Bob 的如实节点本地即拒（§3.4）；若绕过本地校验但如实署名，接收方因 `authors` 新增了路径不允许的作者而拒；若伪造署名，现状同样放行 |
| 必须改 6：拒绝记忆的键不稳定、不可失效 | 接受 | §3.7：键为 `(doc_id, sha256(update), 策略代次)`，代次取 meta 向量摘要，有界 LRU |
| 必须改 7：提交时钟没有覆盖所有写入路径 | 接受 | §3.3 唯一的提交 helper 与契约测试；§3.6 增加时钟规则；向量一律解码后比较 |
| 必须改 8：`memberAddedAt` 没有写入路径与格式校验 | 接受 | §5 补全生产者、校验、契约文档 |
| 应该改 9：`RuntimeDirectory` 不能原样复用 | 接受 | §4.7 `maintain()` 与三处改动；§4.1 公告的 `protocol` 过滤 |
| 应该改 10：缺 `KeySpace` 接口变更 | 接受 | §4.1 |
| 应该改 11：`StateStore` 不足 | 接受 | §2.6 |
| 应该改 12：`Admission` 结果被压扁 | 接受 | §3.6 的 `Accepted` / `Pending` / `Rejected` 与 `AdmissionView` |
| 应该改 13：`SpaceSync` 缺入站、轮次、回调类型 | 接受 | §4.7 的 `SyncHost`、`PeerObservation`、构造参数与职责 |
| 应该改 14：`status.synced` 未处理并发与 holder 变化 | 接受 | §4.6 的本地提交代次与观察作废 |
| 应该改 15：单实例约束未覆盖 lazy load、checkout、web | 接受 | §2.5 文档注册表、pin、只读读者拿不可变值 |
| §8-5：一次性 Doc 导出大 tree 的耗时与内存 | 审查实测 12 ms / 49 MiB（合成数据），未证实有问题 | §2.3 记录数字；真实数据由 C6 测 |
| 简化建议：`commit_batch` 取代 append / append_many；`publish` 归入提交协调者；拒绝去重用原始 update 摘要；`peers_state` 返回不可变观察；不留 history 兜底 | 全部采纳 | §2.4、§2.5、§3.7、§4.6、§5 |
| 遗漏的现有行为清单 | 接受 | §7 末段逐项列为阶段 D 不重写、须由契约测试保住的行为 |

阶段 C 中的修订：

| 日期 | 来源 | 修订 |
| --- | --- | --- |
| 2026-11-25 | C1 报告 §4-1 | §2.6 删除 `document_ids()`：文件名是 doc id 的单向哈希，文件实现做不出来；tree 是文档目录，不需要它 |
| 2026-11-25 | C1 报告 §4-2、§4-3 | §2.7 损坏的 v3 文件同样以 `ORGFS_LAYOUT_UNSUPPORTED` 拒绝并用 `reason` 区分；§2.3 先校验布局再做批次恢复 |
| 2026-11-25 | C1 报告 §4-4、§4-5、§4-6 | §2.4 提交失败后的 `StateStoreBroken` 语义与 `deletes` 的范围；§2.5 协调者对 `StateStoreBroken` 的处理与 `compact` 的调用前提 |

| 2026-11-25 | C3 报告 §4-1（测试复现）| §4.2 新增 frontier 修正：`contentFrontier` 整值覆盖会让并发编辑同一文档在丢广播后永久分叉；协调者在合并远端内容后修正 tree 行 |
| 2026-11-25 | C3 报告 §11.1（确定性复现与随机故障测试）| §4.2 frontier 修正的规则改为：应用远端内容或远端 tree 后都检查，写入行值与本地向量之并；第 1 版规则不收敛。`refSize` / `refSha256` 仅在写入值等于本地向量时填写 |
| 2026-11-25 | C3 报告 §4-2、§4-3、§4-4、§4-7 | §4.7：`SyncHost.local_generation()`；`SpaceSync.request_blob` / `fetch_blob`（二进制内容的拉取入口，现状由 `composition/content.py` 调 mesh）；构造参数加 `announcement_source`、`holder_discovery`；`KeySpace.orgfs_blob_any()` |
| 2026-11-25 | C3 报告 §4-5、§4-6、§4-10 | §4.3 错误应答帧；§4.4 只发向量的帧保留信封字段；§4.2 两条不变量 |

| 2026-11-25 | C2 报告 §4-1（实验复现）| §3.3、§3.6：内容的时钟规则从“`c` 须增大”改为“state vector 须严格前进”；`c` 的数值不再比较 |
| 2026-11-25 | C2 报告 §4-2（实验复现）| §3.3：作者集合改为独立的根 `__orgfs_authors__`，不嵌套在 `__orgfs__` 里 |
| 2026-11-25 | C2 报告 §4-3 | §3.1：只读 / 非成员的写只在本地与节点门被拒，接收节点不按内容作者复查成员资格；C7 §5.3 第 1 项的验收范围相应收窄 |
| 2026-11-25 | C2 报告 §4-4、§4-5、§4-6、§4-8、§4-9 | §3.3 / §3.6：`space.modifiedBy` 与 `space` 根的规则；禁止删除 owner 的成员行；`AdmissionView` 加 `space_id`、`space_name`；tree 行的 `node_id` 须等于键；根行只由创建者写；§6 永久删除的错误码 |
| 2026-11-25 | C2 报告 §4-7 | §3.6“导出即换实例”：谎报结果向量可使缺前驱结构滞留内存；导出时同步更换权威实例，使其等同于从未被接受 |

| 2026-11-25 | C5 报告 §4-1 | §5：`writer_attributions` 是在用的 `org.fetch` 节点来源映射，不是旧迁移；阶段 E 改由设备记录提供 |
| 2026-11-25 | C5 报告 §4-2 至 §4-5 | §5：`memberAddedAt` 为非空字符串；生产者读不到成员行时省略字段；重新加入时 `addedAt` 严格递增；调用方纵深防御测试的搭建方式 |

| 2026-11-25 | C4 报告 §7-1（旧实现上实测）| §3.1：被降为只读的成员在得知降级前的写入会被接受，与旧实现相同，记为已知问题；对应契约在两个实现上都标为预期失败 |
| 2026-11-25 | C4 报告 §7-2（旧实现上实测）| §1.1：`version` 跨正常重开不变，不含进程内计数 |
| 2026-11-25 | C4 报告 §7-4 | §4.2：读取优先只针对内容文档，blob 仍由 runtime 驱动 |

| 2026-11-25 | C6 报告 §4-1（实测）| §3.3：提交时钟只在提交本身没有推进向量时才加，避免每次提交约 13–17 字节的无界增长 |
| 2026-11-25 | C6 报告 §4-2、§4-3（实测）| §6：永久删除后留下每键约 15 字节的墓碑；§2.5：每次进程启动一个 client id 带来的向量增长与不复用的理由 |

C6 的规模实测（合成数据，参考实现）在其报告 §3、§5；真实实现的数字在 C1 接入后重跑。

阶段 D 接入契约测试的方法（C4 报告 §13）：只改 `tests/daemon/orgfs/contracts/support/factory.py` 的
`StateImpl`，并去掉 conftest 里 `state` 参数的预期失败标记；测试文件不改。契约读 state vector 用
`SyncHost.read_vectors`。C4 在旧实现上标为预期失败的 7 条即新实现必须新满足的行为：递归恢复
文件夹、`update_space_meta` 即时广播、重开后 `modified_by` 保持、纯删除推进向量（2 条）、新建空
文档的向量非空，以及上面的降级写入（已知问题，两边都保持预期失败）。

阶段 D / E 的待办（来自 C1–C6 报告，不改设计）：`SpaceSync.fetch_blob` 抛 `SyncError`（形状同 `StoreError`），
`composition/content.py:243` 的 `except StoreError` 要相应改；`membership/` 里的提交 helper 现在无条件加时钟，
阶段 D 接入时按 §3.3 改为“向量未前进才加”；旧实现下 `members()` 的顺序在不同
节点上不同，新实现按用户名排序返回；`read_bytes` 对文本文档返回其 UTF-8 字节，保持现状；`tests/daemon/org/cell_actor/` 下三处把
成员节点的 `document_authors` 桩到 owner facade 的测试，换作者来源后复核是否还需要；leave 记录的
格式函数现在在读取方 `org/network/directory.py`（identity 域没有既有的 leave 正文校验）；cell 镜像不在受保护目录下，`RecordVerifier`
如何识别它是安全记录要在阶段 D 接入时定（今天端口恒返回 `unavailable`，不影响行为）；
`ORGFS_TEXT_MAX` 在 `membership/` 另有一份并由测试钉住与 `document/model.py` 相等，阶段 D 让 facade
从一处导入；`BlobStore` 需要按引用原因释放的
`unpin(space, digest, reason)`，否则过期的同步临时 blob 在该摘要另有引用时会多留一行 `sync`
引用；`apply_remote` 的结果在集成后由按结构识别改为按 C2 的类型识别；阶段 E 删 `network/protocol.py`
前把 `supplier_health.py` 依赖的 `ORGFS_SUPPLIER_SYNC_*`、`ORGFS_SCHEDULED_SYNC_*`、
`ORGFS_SYNC_QUEUE_LIMIT`、`ORGFS_SYNC_RETRY_BACKOFF_SECONDS` 移到保留位置；删 `storage/store` 前把
`StoreError`、`document_file_stem` 移到保留位置。

C1 的 pycrdt 实测（0.14.9）：`pycrdt.merge_updates` 不做 GC，合并必须用一次性 `Doc`；
`pycrdt.get_state(update)` 对缺前驱与纯删除的 update 都返回空向量，不能用来检测缺前驱（所以帧里
自带结果向量是必要的）；50 次整篇覆盖的 4 KB 文本，delta 206,330 字节，合并后 state 4,132 字节。

C7（`orgfs-test-triage-2026-11-25.md`）带来的补充：`content_revision` 的语义（§1.1）；`restore`
去掉 `version` 并用删除批次标记（§6）；永久删除对受保护路径的拒绝（§3.4、§6）；C4 的工厂接缝与
C2 的 `forge_update`（§7）；清单 §5.3 的 20 项逐项分给 C1、C2、C3（§7）。

未验证、留给阶段 C 证实的假设：pycrdt 是否提供不经 `Doc` 取 update 的 state vector 的函数（没有
也不影响，帧里自带向量）；`Map` 键删除后经 GC 的 state 体积；真实 tree 数据下暂存副本重建的
耗时；blob 通道承载 16 MB 临时 blob 的耗时。

## 9. 阶段 D 的裁定（2026-11-25）

阶段 D 的施工图是 `orgfs-minimal-phase-d-plan-2026-11-25.md`（只读架构代理读完 facade 全部调用点后
写成，集成者审定）。它的接缝清单（§1）、协调者落点（§2）、工作包与文件所有权（§4）、必须保住的
行为与对应契约（§5）、真实多节点场景（§7）按原文执行。下面是集成者对其中需要裁定之处的决定；
与本文前面各节冲突时，以本节为准。

**并存策略：双宿主（施工图 §3 的方案 c）。** 旧的 `LocalOrgFs` / `OrgFsRuntime` 在切换前不动；新的
`StateOrgFs` / `StateOrgFsRuntime` 复用保留的 mixin，并由新 mixin 覆盖提交、远端应用、空间加载等
方法；契约工厂的 `StateImpl` 构造新类。每一步集成分支都可验证。不采用 facade 内的后端对象接缝
（包不住行格式、tree 文档 id、成员行、远端方向的差异，要多写 600–800 行临时代码），也不原地
一次性改接（中间态不可验证）。

| 施工图风险 | 裁定 |
| --- | --- |
| R1 三方合并的 base 文本来自 `space.snapshots` | 去掉 `space.snapshots`，另设只为 `write_text(base_version=)` 服务的有界缓存：按 `(doc_id, version)` 保存本进程交出过的正文，上限 256 条且合计 16 MiB，最久未用者先淘汰。未命中时报 `stale-write`，调用方重读后重写。“很旧的 `base_version` 仍可合并”不再成立，记入 known-problems |
| R2 `changes_since` 靠 version 里的修订号排序 | 每空间一个单调的事件序号，`changes_since` 的游标改为“进程标识 + 序号”的不透明字符串；进程标识不符（重启后）时从头给出当前状态。`version` 本身不再携带顺序 |
| R3 `version`、`size` 不能靠加载内容文档来算 | 未加载的文档：`version` 用 tree 行里的 `contentFrontier`，`size` 用行里的 `refSize`；已加载的用本地值。稳态下两者相同。`listdir`、`trash` 不因此加载内容文档 |
| R4 `pending_content` 与 `status.content_pending` 需要全部内容文档的向量 | 协调者维护内存里的“待核对”集合：打开空间时全部文档行进入未核对队列；远端 tree 更新改了某行的 frontier 时把该行加入。`maintain` 每次核对有界的一批（内容文件不存在的直接判为 pending，不必加载；存在的加载后比较）。未核对队列清空之前 `status.synced` 不为 `True`、`content_pending` 把未核对的计入。启动本身仍不读任何内容文件 |
| R5 join 的时序 | 临时空间：建 v3 目录与空注册表 → 在本地 meta 还没有 `space.owner` 期间，节点门只查 presence（对端是调用方显式选的持有方，信任程度与现状 join 相同）→ 拉 meta、tree → 确认本人在成员表里且 supplier 过得了正式节点门，否则删除临时空间并报错 → 转正、公告。supplier 侧的 `requester_allowed` 已保证未被邀请的节点拉不到内容 |
| R6 建空间或 join 中途崩溃留下的目录 | 启动扫描把“没有 `space.json`”与“meta 没有 `space` 根”的 v3 目录当作未完成的创建，按本地空间清除的 fence 规则核对路径后删除 |
| R7 根行 | 根行由创建者经提交 helper 写并与 meta 同批提交；tree 里还没有根行时 `space.nodes` 用不落盘的占位根节点，任何写入前要求根行已到，否则 `content-pending` |
| R8 成员移除后 `addedAt` 无从比较 | **成员移除不删键**（改回现状格式）：行保留并写 `removedAt`，`mode` 不变；有 `removedAt` 的行不是成员。重新加入时清掉 `removedAt`，`addedAt` 取严格大于旧值的新时间戳。§3.2、§3.6 里“被删除的行”相应读作“被标记移除的行”：标记移除与重新加入都属于对行的修改，要求 `addedBy == space.owner`；owner 自己的行不得被标记移除；行的键不得被删除（`Rejected("not-owner")`）|
| R9 `NodeInfo.modified_via` 没有来源 | tree 行加可选的 `via`，由提交 helper 与 `author` 同时写（值为本地 actor，可为空）；`modified_via` 读它 |
| R10 两份 tree 物化 | facade 改用 `membership.materialize_tree`，删除 `TreeDocument.materialize` 自己的实现。移动时钟 `ts` 是进程内计数的既有问题记入 known-problems，不在本次修 |
| R12 `SyncHost` 的线程 | `read_vectors` 读协调者在每次提交后更新的不可变向量表，不持空间锁；`apply_remote` 把命令循环的 `resource-exhausted` 映射为 `Pending("dependency")` |
| R13 blob 引用按“空间加摘要”记 | 永久删除（本地与观察到的远端）在提交成功后，用现有的 `retained_blob_digests` 算出仍被引用的摘要，只释放不在其中的 |
| R14 每次写之前全量导出 tree 作回滚快照 | 去掉。提交失败后从 store 重新 `load` 受影响的文档并重新物化（提交没有生效时磁盘就是提交前的状态）|
| R15、R16、R17、R18、R19 | 按施工图的建议：关闭时的空公告带 `protocol: 3`；`outcome_kind` 改为按类型识别；契约 conftest 的两段式改法；降级写入的契约对两个实现都保持预期失败；`StoreError`、`document_file_stem` 在包 S 里搬进 `storage/` 的新文件 |
| R11 | 按施工图：协调者在外层开事务；值不变的写在进入提交之前返回 |

工作包顺序按施工图 §4，其中 P0、D0、D4a 的独占文件互不重叠，三者同时开工（施工图写的是 P0 先于
D0，实际 D0 不依赖 P0 的产物）。P0 另加两项由本节带来的改动：成员行的 `removedAt` 格式与 tree 行的
`via` 字段（都在 `orgfs/membership/`）。

### 9.1 D1（提交协调者）之后的补充裁定

| D1 报告 | 裁定 |
| --- | --- |
| §6-1 `update_space_meta` 不在命令循环里（旧实现因此不广播 meta 变更）| 新宿主把它做成空间命令。旧宿主不动 |
| §6-2 `version` 的取法 | 收窄为**一律用 tree 行里的 `contentFrontier`**（规范编码）与行的规范序列化取摘要，不论内容文档是否已加载。按“已加载用本地值”会让同一节点的 version 因 LRU 淘汰而变化，`expect_version` 无故 `stale-write`。本地写入总会改写行的 frontier，所以每次本地写都换 version；本条取代上表 R3 里关于 version 的那一句，`size` 的取法不变 |
| §6-3 未核对文档的 `content_state` | 报 `arrived`（`size` 取 `refSize`），已知落后的报 `pending`；`status.content_pending` 把未核对的计入。读取时仍会核对并在落后时报 `content-pending` |
| §6-4 远端 tree 接受时逐行加载内容文档 | D2 让新宿主覆盖 `_remote_birth_content_pending`：内容文件不存在或未加载时按 frontier 直接判为 pending，不为此加载文档；用 C6 式的计数测试钉住“应用一个含 N 个新文档行的 tree 更新不读任何内容文件” |
| §6-5 远端接受时提交什么 | 提交“应用前向量之后真正整合进权威实例的差量”，不是原始 update：谎报向量而滞留在内存的结构不落盘；什么都没整合的重复帧不提交。取代 §2.5 第 1 步里“把同一个 update 提交”的说法 |
| §6-6 拒绝记忆命中时的错误码 | 命中时返回 `Rejected("invalid-argument", {"reason": "remembered"})`；sync 只区分三类结果，不需要原码 |
| §6-7 `status` 的返回类型 | 新宿主返回 `StateSpaceStatus`；`SpaceStatus` 与 `OrgFs` Protocol 的正式变更在包 D4b。D2 的 runtime 直接使用新类型 |

### 9.2 D2（runtime 装配）之后的裁定

D2 的报告提出七个设计问题。已裁定的写在这里，待查证的标明由谁查、查完再定。

| 问题 | 裁定 |
| --- | --- |
| 分区期间被移除的成员永远得不到移除通知（§4.3 让 supplier 拒绝非成员的一切拉取，错过移除那一刻的广播后没有途径补上）| 这是 §4.3 的缺口。supplier 对“成员行仍在且带 `removedAt`”的请求方只回 `meta` 一份文档，不回 tree、内容与 blob；从未是成员的请求方照旧拒绝；被移除方的推送照旧被拒。代价：前成员能看到移除之后成员表的变化，接受。被移除方得知后本地写报 `not-a-member`（Q2）|
| 节点门的引导：非 trust 配置下新成员的第一行设备记录到不了 owner；普通空间找不到 org 目录时门拒绝所有对端 | **未裁定，先查证。** 生产配置是非 trust；要先用证据弄清旧实现在生产配置下三类空间（普通、org 目录、ACL）实际怎样工作，再定新门的规则。约束：不比旧实现在生产配置下弱，也不能让任何一类今天能同步的空间同步不了。包 F1 查证，真实多节点验证复核 |
| join 中途、meta 已落盘之后崩溃，重启时空间被正常打开 | 取决于这是否绕过 `complete_join` 的检查。是则加“join 进行中”的磁盘标记，启动扫描按 R6 删除；不是则记入 known-problems。包 F1 查证 |
| `status.synced` 在本地写入后要等下一轮拉取；核对每个反熵 tick 256 个文档 | 先在 2.5 万文档的空间上实测，再定节奏。包 F1 测量 |
| 契约的 `state` 参数改用非 trust 配置，设备归属由工厂按“节点 → 创建它的作者”提供 | 同意：契约应当跑生产配置。它因此不覆盖真实的设备归属解析，那一段由 `runtime_state/test_node_gate.py` 与真实多节点验证覆盖 |
| `writer_attributions` 在新 runtime 上报 `unavailable` | 按 §5，归包 O |
| `StateSpaceSync` 用普通线程池而非 actor host（R12）| 维持 |

另记两条 D2 的清单外发现：网络可达性门只在同名定义不超过 8 个时连边，两套实现并存让若干名字越过 8，门的覆盖面暂时变窄（基线里 20 条变为陈旧、此前的 6 条新条目不再出现）；包 S 删除旧实现后重新核对，届时再处理基线。旧 `_supplier_owner_for_space` 用空间 id 判断 org 目录的问题并入上表第二行的查证。

#### 节点门的裁定（F1 查证之后）

查证结果（F1 的双节点实验，不信任 tailnet、各 runtime 用真实解析器；真实 daemon 上由多节点验证复核）：

- 旧实现的解析器拿空间 id（UUID）去套按空间名判断 org 目录的规则，对任何节点都解析不出主体。2026-10-27 起解析
  不出即拒绝，此后它能同步完全靠“信任 tailnet 成员”绕过所有门；2026-11-22 `b432a31fff` 把这项信任改为恒假之后，
  dev 上的旧实现在生产配置下三类空间都不能 join、不能同步。已发布的 tag 仍是有条件信任，不受影响。**这是 dev 上
  既有的回归，不是本线引入的，已报 owner。**
- 新实现：org 目录与 ACL 空间在设备行到位后双向同步；普通空间拒绝所有对端；新成员的第一行设备记录到不了 owner
  （owner 的门要先有这行才放行该成员的设备，而这行只能经该设备送来）。

裁定，§3.5 的节点门改为：`gate(space, node)` = 在线 ∧ `principal(node)` 是 `space` 的当前成员；`principal(node)` 的来源：

1. **不按空间名找目录**：在本机持有的所有 org 目录空间的设备索引（活行与 trash 历史）里查。普通空间因此与 org
   空间用同一规则。两个目录给出不同主体时视为未解析，拒绝并记日志。
2. **引导来源**：并上装配层注入的 `device_owner_hint(node) -> principal | None`。orgfs 包只定义这个回调，不依赖
   forwarding 或 org cell；装配层（`orgfs_bridge`）用 org cell 名册的设备记录来实现它（OC3：owner 写名册，成员
   持有只读投影），成员侧拿不到名册投影时再评估 tailnet 名册。已撤销的设备不给主体。
3. 设备索引在目录空间任何被接受的更新之后失效，不只是成员表变化时。
4. 两个来源对同一节点给出不同主体时拒绝（`supplier-owner-conflict`）。全局信任开关保留为旁路（现恒假）。

被移除成员的设备行留在 trash 历史里，解析得到主体，再因不在成员表而被拒，与现在相同。保证强度不变（§3.1）：
节点名与作者都是自报的，这道门防的是如实节点，不防伪造者。

#### 准入规则的补充（T1 改写旧测试时发现）

§3.6 的 tree 规则比旧接收方少两条，旧测试把它们钉为要求，补回：

| 文档 | 比较的内容 | 规则 | 结果 |
| --- | --- | --- | --- |
| tree | 空间根行 | 根行创建之后，`kind` 必须保持为目录，不得被删除（软删除或键消失）、改名或移动；其余字段的变化只接受 `author == space.owner` 的。对所有空间生效，不只 org 目录空间 | `Rejected("protected-path")` |
| tree | 受保护目录下发生变化的行 | 行的 `node_id` 必须等于该路径的确定性 id（`protected_directory_node_id(path)`）；文档行的 `doc_id` 必须等于 `protected_directory_doc_id(path)`。受保护根（`directory`、`directory/devices` 等）因此不能被任何人改名或移动 | `Rejected("protected-path")` |
| tree | 带受保护 doc id 的行 | 受保护的 doc id 只能出现在它编码的那条路径上，不得在别处被链接 | `Rejected("protected-path")` |

本地写入校验同步：被允许的作者把 id 不确定的节点移入自己的受保护子树，错误码定为 `protected-path`（此前沿用了
`not-a-member`）。内容文档被拒时 `Rejected.detail` 的 `path` 填路径而不是 doc id。

不改的：并发写同一受保护行时，伪造的一方在 CRDT 合并中输掉、结果状态不变，则被接受（§3.6 比较的是结果状态）；
输掉的结构留在 tree 文档里但不改变任何物化行。`members()` 的顺序改为按用户名排序，不是契约。

#### 真实节点第二轮之后的裁定（包 F4）

| 问题 | 裁定 |
| --- | --- |
| 目录经临时 blob 传输而 blob 拉取失败时，本地树为空，`status.synced` 仍报 `True` | §4.6 收紧：本地还必须覆盖每条在线观察的 meta、tree 向量。维护 tick 另加一条规则：某个在线持有方的观察领先于本地目录时向它拉取，每空间退避（首次立即，之后 60 s 起翻倍，上限 960 s）。应答里的 blob 每块用固定的 10 s 超时，不再用本轮查询的剩余预算 |
| 带 `base_version` 的写入，三方合并的回退分支把字符偏移套错位置，损坏文本 | 改为按行合并。在 base 中不重叠的行编辑都保留；重叠的行取后写者的文本。代价：同一行内两处不重叠的字符编辑也算冲突，先写者在该行的修改被丢弃且不报错。记入已知问题，是否改为报 `stale-write` 留给负责人 |
| 远端二进制文件到达没有 watch 事件 | 取回入库后为引用该摘要的每个活节点发一次 `modified`（同一节点与摘要只发一次）。blob 仍然按需取回，没有人读就不会到达，也就没有事件；不做后台补齐 |
| leave 之后成员资格未撤销 | 不是 OrgFS 的问题：org 层自 2026-10-27 起要求 owner 确认。验收改为包含 owner 确认这一步。待确认状态没有日志，属于 `org/**`，报负责人 |

#### 切换之后的裁定

| 问题 | 裁定 |
| --- | --- |
| 旧布局空间上的 `orgfs.space_purge` 与 `orgfs.checkout` | 两者都报 `ORGFS_LAYOUT_UNSUPPORTED`（与其他方法同一份错误数据），不改动旧目录的任何字节。`space_purge` 此前对旧布局空间静默返回成功而目录不动，现改为明确拒绝；旧目录按迁移指南手工删除。`checkout` 对不存在的空间仍报 `checkout-io`（已知问题，见 known-problems） |
| `directory/user-keys/**` 的保护范围 | 只在 org 目录空间受保护：那里该分区在签名落地之前对任何作者都拒绝写入。其他空间里同名路径是普通路径；旧实现经 facade 在这些空间里同样生成普通节点，且没有任何代码从 OrgFS 读取 `directory/user-keys/**`，因此不构成回归 |
| `org.fetch` 的节点来源 | 只认 org 目录空间里的 live 设备行；trash 历史里的设备行（已移除成员、已撤销设备）不作为可取用的节点来源 |

#### 真实节点第三、四轮之后的裁定（包 F6、F7）

| 问题 | 裁定 |
| --- | --- |
| 被移除成员仍收到 tree、内容广播，并能取到 blob | 被移除成员的节点只保留 meta：丢弃 tree 与内容广播，不发起内容与 blob 的拉取，定时 tick 只比 meta。blob 请求帧加 `requester {node, author}`，供给方按拉取的同一条规则拒绝非成员。这只约束未改动的软件：节点名与作者都是自报的，广播不加密，真正的切断在传输层（移出组织即移出 tailnet）；不属于任何组织的空间没有这一层。记入已知问题 |
| `holders_online` 与 `rejected_updates` 的含义 | `holders_online` 只列当前成员的节点。`rejected_updates` = 准入拒绝的更新数 + 被节点门拒绝的广播数，是进程内计数，重启归零 |
| 一次失败让供给方长时间被搁置 | 一次成功的探测即结束搁置：成功后失败数减半并压到阈值以下 |
| 一个节点持有别人没有的更新、广播丢了（或当时被拒、之后才可接受），没有后续写入就永不收敛 | 三条规则，都复用现有的拉取与退避，不加帧种类：供给方处理拉取请求时，请求方的向量不被本地覆盖就向请求方反向拉取（日志 `orgfs.sync.requester-ahead`）；定时 tick 除了向领先于本地的持有方拉取，也向“落后的持有方”拉取——在线、持有该空间、过得了节点门、且没有任何观察覆盖本地 meta 与 tree 向量的节点；向它发起拉取会让它反向拉回本地多出的更新。两者共用每空间退避（首次立即，之后 60 s 起翻倍，上限 960 s）；重新成为成员的节点在 meta 被接受后向成员拉取。无进展即停，进程内三节点测试钉住“收敛后拉取与公告次数不再增长” |
| 向被移除的节点发起拉取 | 选择供给方之前先过节点门。被移除节点不作候选，不产生失败记录，不计入供给方退避；重新成为成员后恢复为候选 |
| 被移除成员读本地没有正文的内容 | 立即报 `not-a-member`，不给持有方建议。本地已有的内容仍可读，不追回 |
| 被移除但尚不知情时做的本地写入 | 重新邀请之后按当前成员表被接受，作者是该成员。这是结果状态准入的既定语义，不改；是否应当丢弃这类写入留给负责人 |
| 错误消息 | 没有说明的错误，消息只显示错误码，不再出现 `unknown-doc: unknown-doc` |

#### 真实节点第五轮之后的裁定（包 F8、F8b）

| 问题 | 裁定 |
| --- | --- |
| 持有方持续写入时 join 卡死：临时同步关闭时等入站队列排空 | 关闭中的同步丢弃排队的入站广播，不等排空。被丢弃的广播由常驻同步的拉取补回 |
| join 的结局 | 只有两种。已加入：空间已登记、`join.pending` 已删、常驻同步在跑并已排拉取。未加入：空间未登记、目录已丢弃，下一次 join 是首次 join。临时同步关闭失败只记日志，不改变结局 |
| 收尾的顺序 | 校验 owner 与成员 → 删 `join.pending` 并 fsync → 登记。登记之前的失败丢弃临时空间（此时没有调用方能写它）。登记之后空间可写，任何失败都不删目录：报 `unavailable`，`reason` 为 `join-incomplete`，runtime 正在关闭时为 `orgfs-closing`，message 说明空间已加入；再 join 一次或任何一次建常驻同步的调用会补齐。登记时标记已不在，崩溃后重启保留空间和其中的写入 |
| 带 `join.pending` 的空间再次 join | 运行中的 daemon 上续做，不直接返回成功。重启时带标记的目录照旧丢弃 |
| 同一空间的并发或重复 join | lifecycle 同一时刻只执行一次，后来的调用挂在它上面拿同一结局，不各占一个后台 worker |
| 公告失败 | 只记 `orgfs.announce.failed`，不让已完成的操作失败；公告是软状态，下一次存活边沿或空间变化会重发 |
| 错误文本 | 发给调用方的 message 不带本机路径：`OSError` 只给类型名与 errno 文本 |
| 网络可达性门 | join 的快路径不得用 `super()` 覆盖 `join`：检查器不跟这种调用，会把仍然存在的阻塞路径判成已消失。快路径是 lifecycle 的 `join` 询问的钩子 |
| 上一次 join 留在磁盘上的残留 | 再 join 之前按启动扫描的同一条规则识别（不在内存、未被拒绝、带 `join.pending` 或未初始化）并删除；删不掉就报 `join-incomplete`，不在残留上继续。被拒绝布局的目录从不删除 |
| 已登记但没有常驻同步的空间 | 维护 tick 为它补建，不依赖用户再 join。接受邀请时 join 报未完成的空间记入本地 accept 日志，恢复时再 join 一次；崩溃不记录，恢复不重复 join |
| lifecycle 的效果通道已关闭 | 该命令和挂在它上面的全部等待者立即得到失败，不等到超时 |
