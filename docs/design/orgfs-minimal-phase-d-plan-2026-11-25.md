# OrgFS 精简：阶段 D 施工图（2026-11-25）

状态：只读调研产出，属 `orgfs-minimal-plan-2026-11-24.md` §4 阶段 D 的前置。本文不改任何源码与测试。
设计依据是 `orgfs-minimal-design-2026-11-25.md`（含 §4.2 的单调 frontier 修正，`d5ff852cd0`）；本文发现的与设计
冲突或设计未覆盖之处列在 §6，不在本文里自行变通。

基线：分支 `design/orgfs-phase-d-plan`，已合入 `intg/orgfs-minimal` 的 `9ce104fe73`（C1–C5 已在树内）。
路径相对仓库根；`orgfs/` 指 `src/hyprial/daemon/impl/orgfs/`，表中省略该前缀。证据等级：**[码]** 读代码确认；
**[推]** 推断；**[报]** 取自阶段 A / C 报告、本人没有复核。

## 0. 读了什么、没读什么

| 范围 | 读法 |
| --- | --- |
| 设计全文、计划 §4–§10、两份分拣清单、C1–C6 报告的设计问题与“阶段 D 接入须知”、C3 报告 §11、A3b §4、A5 §4–§5 | 全文 |
| `docs.py`、`runtime.py`、`document/{model,projection,effects,space_state}.py`、`document/operations/{content,tree,spaces}.py`、`composition/{lifecycle,content,checkout,vocabulary}.py` | 逐行 |
| `composition/directory.py` | :279-300、:412-490、:566-635、:798-860、:956-976 逐行；其余只看函数表与对 store / mesh / facade 的调用行（grep）|
| `composition/space_purge.py`、`document/operations/purge.py` | 只看函数表与对 store / authority / mesh / replica 的调用行（grep）|
| `storage/space_authority.py`、`mesh.py` | 类与方法表，`mesh.py:1-130`；方法体**未读** |
| `state/{interface,__init__}.py`、`membership/{__init__,contract,authorship}.py`、`sync/{interface,__init__}.py`、`sync/space.py:1-300`、`sync/pull.py:77-130` | 逐行 |
| `state/{files,layout,memory,codec,fileops}.py`、`membership/{admission,policy,staged,rejections}.py`、`sync/{frames,blobs,vectors}.py` 与 `pull.py` 其余 | 只看函数签名；行为取自各自报告 **[报]** |
| `tests/daemon/orgfs/contracts/support/factory.py`、`contracts/conftest.py` | 逐行。契约测试文件本身**未读**，§5 引用的测试名取自 C4 报告 §3、§5 |
| `network/*`、`storage/store/*`、`storage/replica*`、`projection/*`、`webserver/*`、`web.py` | 实现**未读**；checkout / webserver 只 grep 了对 facade 与私有字段的引用 |
| `api.py`、`orgfs_bridge.py`、`request_catalog/**`、`mcp/server.py`、CLI、GUI | `api.py` 只看声明表；其余**未读**，只 grep 了涉及的文件名。§4 的文件清单据此，施工时须逐文件核对 |

没有运行任何测试。

## 1. 接缝清单

结构事实 **[码]**：

- `OrgFsRuntime` 把自己作为 `mesh` 传给 facade（`runtime.py:119-127`），所以 facade 里的 `self.mesh.X` 落在 runtime
  的方法上（`join`、`broadcast_pending`、`known_holders`、`reconcile_replica_blobs`）。
- `self.stores` 是 `_StoreRegistry`（`composition/vocabulary.py:145-193`，按空间懒建 `LocalSpaceStore`）；
  `space_authority()` 懒建 `OrgSpaceAuthority`（`document/effects.py:617-632`）。
- 本地写入**全部**经 `FacadeProjection._commit` / `_commit_many` / `_write_tree`（`document/projection.py:94,233,930`）。
  调用点只有 `operations/spaces.py:116,173,207,231,733`、`operations/content.py:258,302,322,426,454`、
  `operations/tree.py:104,138,308` 与 `operations/purge.py`。写路径只有一个咽喉，这是改接最有利的事实。

下文 `projection.py`、`effects.py`、`model.py`、`space_state.py` 指 `document/` 下的文件；`spaces.py`、`content.py`、
`tree.py` 指 `document/operations/` 下的文件；runtime 侧的写 `composition/…` 全名。

### 1.1 由提交协调者承接

| 位置 | 现在调用什么 | 语义 | 新实现 |
| --- | --- | --- | --- |
| `projection.py:107-109` | `store is None` 时直接执行 `operation()` | 纯内存 facade 不持久化 | 没有“无 store”分支：内存 facade 用 `MemoryStateStore`，走同一条提交路径 |
| `projection.py:113-144` | 取 `before_state`，执行操作，算增量 `update` 与 `full_update` | facade 的 Doc 先改，再把差量交给 store 的另一份 Doc | facade 的 Doc 就是权威实例；操作与 `stamp_commit` 在同一事务，只算增量 |
| `projection.py:119-134,180-201`、`:259-273,330-359` | 提交前导出 tree、meta 全量与涉及的内容文档，失败时重建 | 本地写的回滚快照 | 语义保留（§2.3）。每次写都全量导出 tree 的成本见 §6 R14 |
| `projection.py:146-153,292-300` | `mutate(doc)` 闭包把更新投到 store 的 Doc | 双实例投影 | 删除 |
| `projection.py:158-179` | `authority.commit(doc_id, update, full_update=, author=, actor=, absorb_since=)` / `store.commit_with_outbox` / `store.commit` | 单文档持久化 | `StateStore.commit(Batch(appends=((doc_id, update),)))` |
| `projection.py:304-329` | `authority.commit_many(edits, …)` / `store.commit_many_with_outbox` / `store.commit_many` | 多文档原子持久化 | `StateStore.commit(Batch(appends=…, deletes=…))` |
| `projection.py:212-222,370-376` | `_publication_records` 后 `_defer_effect(_BroadcastPending(space_id, records))` | 提交后广播（outbox 行）| `_defer_effect(_Publish(space_id, items))`，item = `(doc_id, update, 提交后向量)`，tree 在内容之前（设计 §4.2 不变量）|
| `effects.py:254-270` | 网络 lane 执行 `mesh.broadcast_pending(space_id)` | 异步广播，不阻塞写 | 网络 lane 执行 `SpaceSync.publish`；待发项放每空间有界队列（§2.4）|
| `projection.py:223-224,377-378`、`spaces.py:648-649` | `_notify_meta_watchers` | meta 提交后通知 | 不变；远端 meta 被接受后同样触发 |
| `projection.py:793-839` `_finish` | 用进程内时钟与 `space.revision` 生成 version，写 `space.snapshots` / `space.history`，生成事件 | 提交后的物化视图与 watch 事件 | 事件部分保留；version 改为摘要（§2.6）；`snapshots` / `history` 删除 |
| `spaces.py:391-649` `apply_envelope` | 解 journal 信封、应用到 facade 的 Doc、按 `origin.author` 生成事件 | store 已接受后再“投影”到 facade | 协调者 `apply_remote`（§2.5）。其中 tree 前后比较与事件生成（`:469-605`）、`birth_pending`（`:566-590,611-637`）的逻辑保留并抽成共用函数 |
| `content.py:531-532,535-593,596-601,635-645` | `export_tree_update`、`apply_tree_update`、`export_content_update`、`apply_content_update`：不经持久化直接读写 facade 的 Doc | 旧的同步与测试接缝 **[推：src 内未见调用方]** | 删除；入站只有 `apply_remote` |
| `composition/content.py:197-219` | `refresh_space` 后 `authority.read(_ReadStore("snapshot"))` + `facade.hydrate_content_snapshot` | 文本内容按需补齐（lazy hydrate）| `SpaceSync.request_content(doc_id)` 后在期限内等 `content_state == "arrived"`；`hydrate_content_snapshot`（`content.py:604-632`）删除 |
| `projection.py:923-926` | 内容文档的 client id 取自 node id 的哈希 | 每文档一个确定的 client id | 每空间每次启动一个随机 53 位 client id（设计 §2.5），由文档注册表统一给 |
| `spaces.py:67-116` | `create_space` 生成 `tree-<uuid>`，只提交 meta（`:116`）；tree 根行由 `TreeDocument.__init__` 写，**创建时不持久化** | 空间初始化 | 一个批次提交 meta 与 tree（根行经 `stamp_commit`）；doc id 固定 `tree`；meta 的 `docs.tree` 映射删除 |

### 1.2 由 `StateStore` 承接

| 位置 | 现在调用什么 | 新实现 |
| --- | --- | --- |
| `spaces.py:267,286-288,316-321` | `store.snapshot(doc_id, shallow_since=None).snapshot_bytes`：meta、tree、**每一个**内容文档 | `store.load("meta")`、`load("tree")`；内容文档在第一次读写或同步时才 `load` |
| `spaces.py:545-555` | 远端 tree 行出现新文档时读 `store.snapshot` | 不读；注册表按需加载 |
| `runtime.py:141-159` | 启动扫描：`uuid.UUID(name)`，要求 `journal.sqlite3`，`stores.get` + `facade.load_space` | `is_space_directory_name` → `space_directory_kind`：`v3` 打开；`legacy` / `invalid` 把该空间记为 `ORGFS_LAYOUT_UNSUPPORTED`、其余空间照常；`uninitialized` 见 §6 R6。跳过正在清除的空间（`:146`）保留 |
| `composition/vocabulary.py:145-193` `_StoreRegistry` | 懒建 `LocalSpaceStore`；`snapshot_ids` / `snapshot` / `detach` / `clear` | 新注册表持有 `FileStateStore`，方法同名；`get` 不再对未知空间隐式建库 |
| `runtime.py:306-308`、`composition/space_purge.py:312-314` | `store.close()` | `StateStore.close()` |
| `composition/checkout.py:87`、`composition/space_purge.py:458` | `space_id in self.stores` | 注册表的成员判断，语义不变 |
| `content.py:158` | `content_revision = sha256(内容全量导出)` | 内容 state vector 规范编码的摘要（设计 §1.1；`membership.vector_digest`）|

### 1.3 由 `membership`（本地校验、作者字段、`Admission`）承接

| 位置 | 现在 | 新实现 |
| --- | --- | --- |
| `projection.py:404-411` `_ensure_writable`；调用点 `content.py:227,375,443`、`tree.py:65,121,168` | 只查成员与 `rw` | `check_local_write(caller, LocalWrite(...), view, ProtectionPolicy)`：成员、受保护路径、永久删除的 owner / author。行为变严（C2 报告 §8：旧 facade 本地不查受保护路径的作者）|
| `tree.py:88-98` | 移动涉及受保护 id 时报 `not-a-member` | 保留该检查与错误码，再过 `check_local_write` |
| `spaces.py:153,180,214` `require_space_owner` | owner 才能改成员表与 meta | 保留；与 `check_local_write(kind="meta")` 等价，取其一 |
| `spaces.py:186-205` `invite` | 写 `{mode, addedBy, addedAt, removedAt: None}` | `put_member`（三个键）；重新加入时 `addedAt` 严格递增（设计 §5）|
| `spaces.py:218-231` `remove_member` | 把行改写为带 `removedAt` 的墓碑；内存 `removed_members` | `delete_member`（删键）；`removed_members` 删除（§6 R8）|
| `spaces.py:324-340,431-455` | 解析成员行、跳过 `removedAt` | `read_members` |
| `spaces.py:234-238` `members()` | dict 顺序 | 按用户名排序（设计 §8 待办）|
| `content.py:95-113` `document_authors` | `authority.read(_ReadStore("document_authors"))` / `store.document_authors` / `space.history` 兜底 | `membership.document_authors(doc)`，读 `__orgfs_authors__` |
| `spaces.py:312,513-514`、`projection.py:815-816` `modified_by` | 本地写取 `self.author`，远端取信封 `origin.author`，重开后一律置为 owner | 读 tree 行 `author`（`row_author`）；`modified_via` 见 §6 R9 |
| `model.py:644-665` `record_node` | 行里没有 `author` / `deletedBatch` | 行经 `tree_row_wire` 写，`stamp_commit(tree_rows=…)` 置 `author`，软删除用 `with_row_deleted_batch` |
| `mesh.py:294-327` `_supplier_allowed` / `_supplier_member_allowed`（方法体未读）| 节点门 | `SyncHost.supplier_allowed(node)`：`_device_owner_index`（`composition/directory.py:614-704`，保留）给出的所属用户是当前成员；`trust_tailnet_members` 时跳过 |
| `network/ingress.py:410-418` `_requester_allowed`（未读，位置取自设计 §0.1）| supplier 侧查 `requester.author` | `SyncHost.requester_allowed(author)` |
| `network/recovery.py:486-550` 的 store 准入（未读，位置取自设计 §0.2）| 信封级准入 | `ResultStateAdmission.admit`，由 `apply_remote` 调用 |

### 1.4 由 `SpaceSync` 承接

| 位置 | 现在调用什么 | 新实现 |
| --- | --- | --- |
| `composition/lifecycle.py:442-465` | 构造 `OrgFsMesh(session, store, node_id, author=, blob_store=, supplier_online=, supplier_owner=, trust_tailnet_members=, announcement_source=, holder_discovery=, recovery_candidates=, on_applied=, on_replacement=, logger=, supplier_sync_health=, actor_host=, space_authority=)` | 构造 `StateSpaceSync(session, KeySpace(), space_id, node_id, author, host, blob_store, supplier_health, presence, logger, announcement_source=, holder_discovery=)`（`sync/space.py:85-100`）。`on_applied` / `on_replacement` / `space_authority` 没有对应物，入站由 host 的 `apply_remote` 承接；`recovery_candidates` 并入 `holder_discovery`（传 `_holder_snapshot` ∪ `_holder_candidates`）|
| `composition/lifecycle.py:387-416,495-511` `_ensure_mesh_sync` / `_mesh`；`runtime.py:169` `_meshes` | 每空间一个 mesh，创建合并 | 同一套生命周期命令与合并逻辑，值类型换成 `SpaceSync` |
| `composition/lifecycle.py:583-602` `broadcast_pending` / `broadcast_records` | facade 经 runtime 找 mesh 广播 | `publish_updates(space_id, items)`：找到 `SpaceSync` 后逐项 `publish` |
| `composition/lifecycle.py:618-658` `join` / `_join_sync`；facade 侧 `spaces.py:241-253` | `mesh.sync_from(holder)` → `facade.load_space` → `_reconcile_retirements` → `_announce_all` | 时序见 §6 R5：先建临时空间与 host，再 `sync.sync_from(holder)`（`sync/pull.py:77-116`）直到 meta、tree 到齐，确认自己是成员后转正并公告 |
| `composition/content.py:92-124` `refresh_space` | 对每个在线持有方 `mesh.sync_from(holder, timeout=, deadline_monotonic=)`，捕获 `StoreError` | `sync.sync_from` 同签名；捕获 `SyncError` |
| `composition/content.py:188-196` | `mesh.request_blob(digest)` 的事件循环 | `sync.request_blob(digest)`（`sync/blobs.py:142`，语义相同）|
| `composition/content.py:223-274` `fetch_blob` | `mesh.fetch_blob(holder, digest)`，`except StoreError`（`:256`）| `sync.fetch_blob(holder, digest)`，`except SyncError`（设计 §8 待办）|
| `composition/content.py:127-164` `known_holders`；facade 侧 `projection.py:676-683` | `authority.read(_ReadStore("holder_frontiers"))`，按逐文档覆盖排序 | 只按“在线、已知”排序；逐文档 holder frontier 删除（Q3）|
| `composition/directory.py:436-439` | `store.expire_pending()`、`take_drained()`、`facade.apply_envelope` | 对每个已打开的 `SpaceSync` 调 `maintain(now)` |
| `composition/directory.py:484-485,504-510,793-795` | `mesh.schedule_sync_from(peer)` | 同名方法，调用点不改 |
| `composition/directory.py:815-823` `_announce_all` | 取任一 mesh 调 `announce()` | 同；`announcement_source=_announcement_spaces`（`:798-812`）|
| `composition/directory.py:290-291,839-840` | 公告只校验 `schemaVersion == 1`、`type` | 加 `protocol == 3` |
| `runtime.py:247-263` | 关闭时发空公告，不带 `protocol` | 加 `"protocol": 3`；否则对端按新过滤规则忽略它，持有方记录不会撤销 |
| `composition/directory.py:354,597,784` | `self.stores.snapshot_ids()` 当作“本机有哪些空间” | 新注册表的同名方法 |
| `runtime.py:283-288` | `facade.drain_broadcasts(…)` 后 `mesh.close()` | 先排空待发布队列，再 `sync.close()`，再 `facade.close_effects`（C3 报告 §9：先关 sync 再关协调者）|
| `content.py:490-515`、`composition/content.py:39-77` `status` | `store.unconfirmed_commits` / `holders_seen` / `parked_commits`，runtime 叠加在线持有方 | 协调者算 `synced`、`content_pending`、`rejected_updates`（用 `sync.observations()` 与本地提交代次）；`holders_online` 仍由 runtime 算 |

### 1.5 直接删除（journal、retirement、replica 专属）

| 位置 | 内容 |
| --- | --- |
| `effects.py:89-96,99-139,242-252`、`docs.py:171` | `_broadcast_is_enabled`、`_publication_records`、`_space_has_unbroadcast`、`_pending_outbox`：journal outbox，以及“实例覆盖 broadcast_pending 即抑制广播”的测试接缝 |
| `projection.py:381-401`（调用 `:202,360`）| `_absorb_committed_deltas`：store 的 coverage clock 回灌 |
| `projection.py:203-205,353-355,361-363` | `store.take_drained()` → `apply_envelope` |
| `projection.py:215-216,373-374`、`effects.py:229-234`、`composition/lifecycle.py:605-615`、`model.py:117-119` | `_ReconcileReplicaBlobs` 效果与 `reconcile_replica_blobs` |
| `effects.py:617-632,459-463,473,492,531-535`、`docs.py:167` | `space_authority()`、`_space_authorities` 的构造与关闭 |
| `spaces.py:424-428,456-468` | 远端 meta 里的 retirement 处理 |
| `spaces.py:652-733`、`operations/purge.py` 全文、`projection/purge.py`、`projection.py:719-735`、`docs.py:170`、`effects.py:476-480` | `install_replacement_snapshot`、`acknowledge_purge`、内容清除、`_forget_history`、`_purge_plans`（Q4）|
| `space_state.py:74-82,105-125,232-239,264-281`、`model.py:172-189,236-273` | 对应的冻结命令 `_ApplyEnvelope`、`_InstallReplacement`、`_AcknowledgePurge`、`_PurgePlanCommand`、`_Purge`、`_Unban`、`_ApplyTreeUpdate`、`_ApplyContentUpdate`、`_HydrateContentSnapshot`；`_Restore.version`（`model.py:232`）|
| `space_state.py:43-44,147-148` | `_JsonProjection`，只为 purge plan 的还原存在 |
| `content.py:116-140,518-528` | `document_covers_member_add`、`parked_commits` |
| `content.py:358-361,399-402` | `purged` 错误码分支 |
| `projection.py:626-627`、`composition/content.py:198` | `content_state == "unverifiable"` |
| `projection.py:738-790`、`model.py:477-582`、`_Space.history` / `snapshots` / `snapshot_marks` / `revision`（`model.py:871-880`）| 按版本快照与内存历史。**注意** `write_text(base_version=)` 仍读它（§6 R1）|
| `tree.py:163-328` | `restore(version)` 与 `_snapshot_descendant`，重写为 §2.7 |
| `composition/lifecycle.py:428-440,480-489,514-580`（调用 `:430,440,642`、`composition/checkout.py:45`）| `applied` / `replacement` 钩子、replica attach、`_reconcile_retirements` |
| `composition/checkout.py:39-54` | `purge_plan` / `purge` / `purge_status` / `unban` 透传 |
| `composition/content.py:67-70,80-89,147-153,250-254` | `_replicas` 分支、`writer_attributions`（`org.fetch` 改由设备记录，§4 包 O）、holder frontier |
| `runtime.py:47,137-138,282,289-291,296`、`composition/space_purge.py:291-296,451` | `_replicas` 字段与关闭。`space_purge.py:85,447,461` 对 `state/orgfs/replica/<space>` 目录的清理**保留**（旧版本遗留数据，A5 报告 §5）|

### 1.6 保留不动（改接时只换导入或做表中的最小改动）

| 位置 | 内容 | 最小改动 |
| --- | --- | --- |
| `space_state.py:161-220,285-528` | 每空间命令循环：actor cell、单 worker 效果 lane、generation 重启重放、waiter 结算、custody 上限 | 新增冻结命令 `_ApplyRemote`；`:48,514-515` 的 `StoreError` 导入换位置 |
| `model.py:343-422` `_facade_locked`、`effects.py:608-614` `_space_lock` | 读在调用线程上持空间锁执行；写经命令循环，在 lane worker 上持同一把锁（`space_state.py:292-294`）| 不变。这把锁就是设计所说“串行执行者”的互斥（§2.2）|
| `effects.py:142-239,340-420,547-592` | post-commit 效果：预留、提交、回调 lane、等待 | 效果种类去掉 `_ReconcileReplicaBlobs`，`_BroadcastPending` 换成 `_Publish` |
| `effects.py:423-482,485-537` | `detach_space`、`close_effects` 的截止时间语义 | 去掉 authority 关闭，加“关闭该空间的 store 与注册表” |
| `projection.py:414-439,442-664` | `_space`、`retained_blob_digests`、`_validate_path`、`_resolve_ids`、`_member_row_visible`、`resolve`、`_one`、`_path`、`_name_conflict`、`_node_info` | `_node_info` 的 `state_covers`（`:631`）换成 `vector_covers`；`size` 不能再靠加载内容算（§6 R3）|
| `projection.py:842-927` | `_parent_for_new`、`_new_node`：路径解析、受保护的确定性 node id / doc id | 只改 client id 来源 |
| `model.py:607-771` `TreeDocument` | 行 Map、追加式移动日志、带环保护的物化 | 不在空 tree 上自写根行（`:624-635`）；行带 `author` / `deletedBatch`（§6 R10）|
| `model.py:774-858` `_ContentDocument`、`content.py:180-212` `_three_way` | 文本差量写入、三方合并 | 构造时由注册表给 client id（§6 R11）|
| `content.py:61-93,144-177,335-356,461-487`、`tree.py:52-59,143-160` | `stat`、`listdir`、`read_text`、`read_bytes`、`watch`、`changes_since`、`trash` | `changes_since` 的排序依据要换（§6 R2）|
| `composition/checkout.py:57-143`、`projection/checkout*.py` | checkout 只经 `facade.stat` / `watch` / `listdir` / `read_bytes`（`checkout_authority.py:113,339`、`checkout.py:169,243`）| 无。不持有 `Doc` **[码，grep]** |
| `webserver/*`、`web.py` | 对 `_spaces` / `contents` / `tree` 没有任何引用 **[码，grep]** | 无 |
| `storage/blobs.py`、`storage/blob_authority.py` | blob 存储与引用账本 | 新增 `unpin(space, digest, reason)`（设计 §8 待办）；purge checker / projection 的注册（`blob_authority.py:507-525`）随 Q4 删除 |
| `composition/space_purge.py` | 本地空间清除与 crash-resumable intent | `:290-296,312-314,450-458` 换成新注册表与 `SpaceSync` |
| `composition/directory.py` 其余 | 公告缓冲、liveliness、持有方发现、重建后补拉、设备归属索引 | §1.4 所列三处。文件现 976 行，离 1000 行门只差 24 行，**只能净减** |
| `network/supplier_health.py` | 供应方退避；`sync/space.py:44` 已在用 | 阶段 E 把它依赖的常量从 `network/protocol.py` 搬进本文件 |

## 2. 提交协调者的落点

### 2.1 放在哪

结构门的现状 **[码]**：`orgfs/` 根目录 7 个文件、`composition/` 7 个、`state/` `membership/` `sync/` `storage/` `network/` 各 7 个，
都已到上限；`document/` 5 个；`projection.py` 961 行、`model.py` 910 行、`composition/directory.py` 976 行。所以协调者
既不能作为新文件放进 `orgfs/` 根或 `composition/`，也不能写进 `projection.py` / `effects.py`。

落点：新子包 `orgfs/document/coordinator/`（7 个文件），每个文件是 facade 宿主上的一个 mixin 或一个小值对象；
它们**覆盖** `FacadeProjection` 等旧 mixin 里的同名方法，不改写旧方法（并存策略见 §3）。

| 文件 | 内容 | 取代的旧代码 |
| --- | --- | --- |
| `__init__.py` | 导出 `StateOrgFs` 的 mixin 组合 | — |
| `registry.py` | `_DocRegistry`：每空间的权威 `Doc`（meta、tree、内容 LRU 256 加 pin）、meta / tree 的暂存副本、client id、“导出即换实例”、合并触发 | `_Space.contents` 这个普通 dict（`model.py:868`）|
| `commit.py` | `StateCommit`：覆盖 `_commit`、`_commit_many`；回滚；`StateStoreBroken` 处理；发布排队；本地提交代次 | `projection.py:94-378` 的 store 分支 |
| `remote.py` | `StateRemote`：`apply_remote`（拒绝记忆、准入、应用、观察到的行消失、frontier 修正、事件）| `spaces.py:391-649` |
| `host.py` | `SpaceSyncHost`（`SyncHost` 的实现，把调用转到空间执行者）、`_AdmissionView` | `mesh` 的 `on_applied` 钩子与 store 准入视图 |
| `spaces.py` | `StateSpaces`：覆盖 `create_space`、`load_space`、`invite`、`remove_member`、`members`、`status`、`document_authors`、`join` 的临时空间 | `operations/spaces.py:60-365`、`content.py:95-113,490-515` |
| `tree_ops.py` | `StateTreeOps`：覆盖 `remove(permanent=)`、`restore(recursive=)`；version 与 base 文本缓存；`changes_since` 的序号 | `tree.py:118-140,163-328`、`projection.py:738-839` 的版本部分 |

### 2.2 现有机制在新路径上的对应

| 现有机制 | 位置 | 新路径 |
| --- | --- | --- |
| 命令循环（一个空间一个 actor cell 加单 worker lane）| `space_state.py:161-220` | 原样使用。本地写、`apply_remote`、合并都作为命令在这条 lane 上执行 |
| `_space_lock` | `effects.py:608-614`；写在 `space_state.py:292-294` 持锁，读在 `model.py:362-373` 持锁 | 仍是唯一互斥。`StateStore` 与全部 `Doc` 只在持有该锁时访问：写与远端应用在 lane worker 上，读（含 `SyncHost.read_vectors` / `read_update` / `pending_content`）在调用线程上持锁。设计 §2.6 说“来自唯一的串行执行者”，实际形态是“任意线程，但持同一把锁”，与现状一致（§6 R12）|
| `_facade_locked` | `model.py:343-422` | 不变 |
| 回滚快照 | `projection.py:119-134,180-201` | `StateCommit._rollback`：恢复 tree、meta、涉及的内容文档、`space.nodes`、成员缓存。只有本地写用它；远端更新失败按设计 §2.5 丢弃受影响文档的内存实例 |
| post-commit effect 的预留与提交 | `effects.py:547-592`、`space_state.py:464-476,318-323` | 不变；提交成功后才 `_defer_effect` |
| watch | `projection.py:826-839`、`content.py:461-469` | 不变；远端应用同样走 `_finish` 生成事件 |
| lazy hydrate | `projection.py:667-716` `_content_pending`、`content.py:170-176`、`composition/content.py:197-219` | 读到未覆盖 `contentFrontier` 的内容：协调者把该文档排到 `SpaceSync.request_content`，照旧抛 `ORGFS_CONTENT_PENDING`；`await_content` 在期限内轮询 `content_state` |
| 文档注册表（LRU、pin）| 无 | `_DocRegistry.content(doc_id)` 加载并在当前命令期间 pin；命令结束时解 pin，超过 256 的未 pin 文档淘汰 |
| 暂存副本 | 无 | `_DocRegistry` 持有 meta、tree 各一个 `StagedDoc`；内容的暂存副本在 `apply_remote` 里按需 `StagedDoc.from_state`，用完即弃 |
| 导出即换实例 | 无 | `_DocRegistry.export_and_swap(doc_id)`，内部用 `StagedDoc.reset_from_authority`；调用方只有两处：`apply_remote` 得到 `Rejected` / `Pending("dependency")` 后重建暂存副本，以及合并 |
| frontier 修正 | 无 | `StateRemote._repair_frontier`，在 `apply_remote` 接受内容或 tree 之后、同一条命令内调用 |
| 观察到行消失时删内容文件 | 无 | `StateRemote._observed_deletes(before, after)`，结果并入**同一个**提交批次的 `deletes` |
| 合并触发 | 无 | `StateCommit._after_commit(doc_ids)`：对本批次涉及的文档查 `needs_compaction`，为真则 `export_and_swap` 后 `store.compact(doc_id, state)`；失败只记日志，下次提交再试 |

### 2.3 本地提交

```python
class StateCommit:
    def _commit(self, space: _Space, doc_id: str, operation: Callable[[], None],
                *, broadcast: bool = True) -> None: ...
    def _commit_many(self, space: _Space,
                     operations: Iterable[tuple[str, Callable[[], None]]],
                     *, deletes: tuple[str, ...] = ()) -> None: ...
    def _stamp(self, space: _Space, doc_id: str, *, tree_rows: Iterable[str] = (),
               space_root: bool = False) -> None: ...
    def _rollback(self, space: _Space, saved: _RollbackSnapshot) -> None: ...
    def _reopen_after_broken(self, space: _Space) -> None: ...
    def _after_commit(self, space: _Space, doc_ids: Sequence[str]) -> None: ...
```

步骤（全部在空间的命令 lane 上、持 `_space_lock`）：

1. 调用方先做 `check_local_write`（在各 facade 方法里，产生 update 之前）。
2. 记下各涉及文档的 state vector 与回滚快照。
3. 对每个 `(doc_id, operation)`：在该文档的一个 `doc.transaction()` 里执行 `operation()` 与 `_stamp`
   （`stamp_commit`；按设计 §3.3 改为“向量没有前进才加时钟”，见 §4 包 D1）。tree 的 `_stamp` 需要“本次改到的行”：
   `_write_tree` 的 `affected`（`projection.py:957-958`）就是它。
4. `update = doc.get_update(before_vector)`；`store.commit(Batch(appends, deletes))`。
5. 失败：`_rollback` 后抛 `OrgFsError`（沿用 `projection.py:198-201` 的错误码映射）；若是 `StateStoreBroken` 或其后的调用
   抛它，`_reopen_after_broken`：丢弃该空间全部权威 `Doc` 与暂存副本，关闭并重开 store，从 `load()` 重建，再报错。
6. 成功：本地提交代次加一；把同样的 update 应用到 meta / tree 的暂存副本（保持“暂存 = 权威”）；
   `_defer_effect(_Publish(...))`（tree 在内容之前）；meta 则 `_notify_meta_watchers`；`_after_commit`。
7. 调用方随后 `_finish`（事件、version），与现状顺序相同（`projection.py:960-961`、`content.py:269,329`）。

### 2.4 发布

旧实现的网络 lane 每次去读 journal 里未广播的行（`effects.py:254-270`），所以效果本身不带数据。新实现没有 outbox：
`_Publish` 带着 `(doc_id, update, vector)`，由 `_submit_post_commit_effects`（`effects.py:340-373`）放进**每空间一个有界队列**
（建议 256 项，溢出丢最旧并计数；丢了的由对端拉取补齐，设计 §4.4），网络 lane 的执行函数（`effects.py:254`）逐项调用
`runtime.publish_updates`。保留现有“一个空间同时只有一个网络任务”的簿记（`_network_due_spaces` / `_network_active_spaces`，
`effects.py:273-297`），它保证同一空间的发布顺序。`drain_broadcasts`（`effects.py:313-337`）改为等队列清空。

### 2.5 远端更新

```python
class StateRemote:
    def apply_remote(self, space_id: str, doc_id: str, update: bytes,
                     expected_vector: bytes, peer: str) -> Accepted | Pending | Rejected: ...
    def _observed_deletes(self, before: TreeValue, after: TreeValue) -> tuple[str, ...]: ...
    def _repair_frontier(self, space: _Space, node_ids: Iterable[str]) -> None: ...
    def _project_tree_change(self, space: _Space, previous: Mapping[str, tuple],
                             old_paths: Mapping[str, str | None]) -> None: ...

class SpaceSyncHost:                       # 一个空间一个；SyncHost 的实现
    def __init__(self, facade: "StateOrgFs", space_id: str,
                 node_gate: Callable[[str], bool]) -> None: ...
    def read_vectors(self, doc_ids): ...       # 持 _space_lock，在调用线程上
    def read_update(self, doc_id, since): ...  # 同上；需要时经注册表加载内容文档
    def pending_content(self, limit): ...      # 见 §6 R4
    def apply_remote(self, doc_id, update, expected_vector, peer): ...  # 经 _submit_space_state
    def requester_allowed(self, author): ...   # 读成员缓存
    def supplier_allowed(self, node): ...      # node_gate，由 runtime 注入
    def local_generation(self): ...
```

`apply_remote` 是 `_SPACE_STATE_METHODS` 的新成员，冻结命令 `_ApplyRemote(space_id, doc_id, update, expected_vector, peer)`。
步骤（C2 报告 §9 的调用顺序）：

1. `gen = policy_generation(meta 向量)`；`RejectionMemory.seen` 命中则返回 `Rejected`，不计数、不告警。
2. 取暂存副本（meta / tree 用常驻的；内容从权威实例导出一个）。内容文档在 tree 里没有行时由准入返回
   `Pending("unknown-doc")`，不加载、不落盘。
3. `admission.admit(doc_id, update, expected_vector, staged, view)`。
4. `Rejected`：`remember`、`rejected_updates` 加一、记 `orgfs.update.rejected`、暂存副本经 `export_and_swap` 重建。
   `Pending("dependency")`：同样重建。原样返回。
5. `Accepted`：记下 tree 的物化值（仅 tree）→ 权威实例 `apply_update` → 对 tree 算 `_observed_deletes` →
   `store.commit(Batch(appends=((doc_id, update),), deletes=…))`。提交失败：丢弃该文档（tree 则连同暂存副本）的内存实例，
   下次使用时从磁盘加载，返回 `Pending("dependency")` 让 sync 稍后重拉。
6. 成功后：刷新 `space.nodes` 与成员缓存；释放被删行的 blob 引用（§6 R13）；从注册表移除被删文档；
   `_project_tree_change` / `_finish` 生成事件（作者取行的 `author`）；`_repair_frontier`（它是一次普通的本地 tree 提交，
   走 §2.3，不改 `author`）；meta 则 `_notify_meta_watchers` 并让 runtime 的设备归属索引失效（已有，经 `watch_meta`）。

### 2.6 version、base 文本、事件序号

| 项 | 现状 | 新实现 |
| --- | --- | --- |
| `NodeInfo.version` | `_version(space.revision, self._clock)`，两者都是进程内计数（`model.py:425-433`、`projection.py:805-809`）；重开后全部节点置为 `_version(0, 0)`（`spaces.py:358-360`）| `_node_version(space, node)`：该行规范序列化与内容向量规范编码的摘要（设计 §1.1）。不加载内容的取法见 §6 R3 |
| `base_version` 的三方合并 | 读 `space.snapshots[base_version][node].content`（`content.py:285-291`）| `_BaseTexts`：每空间有界缓存 `(node_id, version) -> 文本`，在读与写返回时登记；未命中且 `base_version` 等于当前 version 时 base 取当前文本，否则 `stale-write`（§6 R1）|
| `changes_since` | 按 version 里的 tree 修订号比较（`content.py:479-485`、`model.py:436-442`）| 每空间单调事件序号；`since_version` 映射到“最后一个携带该 version 的事件”的序号，找不到则返回全部（§6 R2）|

### 2.7 删除与恢复

```python
class StateTreeOps:
    def remove(self, space_id: str, node: NodeRef, *, permanent: bool = False) -> None: ...
    def restore(self, space_id: str, node: NodeRef, *, recursive: bool = True) -> NodeInfo: ...
```

- 软删除：沿用 `tree.py:126-140` 的受影响集合，但**只给本次由未删除变为删除的行**写同一个新 `deletedBatch`；
  现状的集合包含早已删除的后代（`tree.py:128-132` 不过滤 `deleted`），给它们盖新批次会让之后的递归恢复把更早删除的
  子项一起带回来。
- 恢复：目标行，加上 `recursive` 时与它 `deletedBatch` 相同的后代，加上已删除的父链（现有“不半恢复”规则，
  `tree.py:183-200`）；把 `deleted` 改回并去掉 `deletedBatch`；blob 行先查 `blobs.has`（`tree.py:205-212` 保留）。不改内容。
- 永久删除：`check_local_write(permanent=True, node_author=row_author(row))`；从 `nodes` Map 删除该节点与全部后代的键
  （含早已软删除的）；它们的内容文档进 `Batch.deletes`；提交成功后释放 blob 引用并从注册表移除。事件种类沿用 `removed`。

## 3. 改接的分步与并存策略

### 3.1 三种做法

| | (a) facade 内的后端对象接缝 | (b) 原地一次性改接 | (c) 双宿主：共用 mixin，新旧各一个宿主类（**推荐**）|
| --- | --- | --- | --- |
| 形状 | `LocalOrgFs` 构造时注入 `SpaceBackend`；`_commit` 等调用它；legacy 后端包住 store / authority / mesh | 直接改 `projection.py`、`spaces.py`、`runtime.py` 等，同一批提交里删 journal 测试 | 旧类 `LocalOrgFs` / `OrgFsRuntime` 不动；新类 `StateOrgFs` / `StateOrgFsRuntime` 由**同一批保留的 mixin**加 §2.1 的新 mixin 组成；契约工厂的 `StateImpl` 构造新类 |
| 接缝在哪 | 运行时对象（Protocol）| 无 | 类的方法解析顺序：新 mixin 覆盖 `_commit`、`_commit_many`、`load_space` 等同名方法 |
| 每一步集成分支是否可验证 | 是 | **否**：约 350 条 journal 专属与约 330 条需改写的测试同时变红，直到全部做完 | 是：旧类的测试一条不动、始终绿；新类由 140 条契约与三个新包的测试逐步变绿 |
| 临时代码 | legacy 后端约 600–800 行（见下），加数据格式分叉约 15 处 | 无 | 新旧宿主类的组合声明两处、测试用的实现选择开关一处（§3.4），共几十行；切换时删除 |
| 主要风险 | 接缝包不住数据格式差异，分叉散落在保留代码里，切换后要逐处清 | 中间态不可运行；多 agent 无法并行；出问题无法二分 | 共用 mixin 在切换前要同时满足两个宿主；切换提交本身是一次大删除 |
| 工作量 [推] | 最大（多写一个要丢弃的后端）| 代码量最小，但串行、返工风险最高 | 比 (b) 多一次前置的纯重构（包 D0）|

### 3.2 为什么 (a) 的对象接缝包不住

(a) 的 Protocol 若要让两个后端都能挂在现有 facade 上，至少要有：

```python
class SpaceBackend(Protocol):
    def open_space(self, space_id: str) -> LoadedSpace | None: ...
    def commit(self, space: _Space, edits: Sequence[Edit], deletes: Sequence[str]) -> Published: ...
    def document_authors(self, space: _Space, doc_id: str) -> tuple[str, ...]: ...
    def status(self, space: _Space) -> SpaceStatus: ...
    def publish(self, space_id: str, published: Published) -> None: ...
    def close_space(self, space_id: str, timeout: float) -> bool: ...
```

本地提交这一半确实可以这样切：`_commit` 已经先在 facade 的 Doc 上执行操作、再把 `(update, full_update)` 交出去
（`projection.py:135-171`），新实现只是不再有第二份 Doc。包不住的是下面这些，它们都在接缝的 facade 一侧：

| 差异 | 位置 | 为什么不是后端能决定的 |
| --- | --- | --- |
| 远端更新的方向相反 | 旧：store 先接受信封，再回调 `apply_envelope` 投影（`composition/lifecycle.py:428-431`、`spaces.py:391`）；新：协调者先在暂存副本上准入，再应用、提交 | 入站入口不同，facade 要同时保留两套入口与两套命令 |
| tree 行格式 | `model.py:644-665` | `author` / `deletedBatch` 是否写入是模型层的行为；旧 store 对 tree 行另有自己的准入（未读），不保证接受多出来的字段 |
| tree 文档 id | `spaces.py:67,278-285` 的 `tree-<uuid>` 与 `docs.tree.active` | `space.tree_doc_id` 在 `document/` 里被引用 26 处（不含 `operations/purge.py`） |
| 成员行 | `spaces.py:197-204,221-229` 的 `removedAt` 墓碑 | 新格式删键；`removed_members` 影响 `invite` 的 `addedAt` 规则（`spaces.py:189`）|
| 根行 | `model.py:624-635` | 新实现禁止在空 tree 上自写 |
| 内容文档的持有方式 | `_Space.contents` 是装着全部文档的 dict，44 处引用（`grep`，不含 `operations/purge.py`） | 新实现是惰性注册表 |
| version、restore、snapshots | `projection.py:793-825`、`tree.py:163-315` | 语义不同，不是持久化问题 |

legacy 后端要包的现有代码：`projection.py:107-224,242-378` 的 store 分支（约 250 行）、`_absorb_committed_deltas`、
`effects.py:99-139,242-252,617-632`、`load_space`（`spaces.py:256-365`）、`apply_envelope` 的入口、`status` 与
`document_authors` 的三岔。即便全部包好，上表每一项仍要在 facade 里按后端分叉。结论：(a) 可行但不划算，临时代码最多，
而且分叉点正好落在设计要求“不重写”的那批代码里。

### 3.3 (c) 的确切形状

```python
# 现有，切换前不动
class LocalOrgFs(FacadeEffects, FacadeSpaceState, FacadeProjection, FacadeSpaces,
                 FacadeContent, FacadeTreeHistory, FacadePurge): ...

# 新增，orgfs/document/coordinator/__init__.py
class StateOrgFs(StateCommit, StateRemote, StateSpaces, StateTreeOps,
                 FacadeEffects, FacadeSpaceState, FacadeProjection, FacadeSpaces,
                 FacadeContent, FacadeTreeHistory): ...
```

新 mixin 排在前面，覆盖同名方法；没有被覆盖的方法就是“保留的现有行为”，两个宿主执行同一份代码。`FacadePurge` 不进
新宿主。runtime 同理：`StateOrgFsRuntime(AnnounceInstrumentation, StateLifecycle, StateContent, RuntimeDirectory,
RuntimeLifecycle, RuntimeContent, RuntimeCheckout, RuntimeSpacePurge)`，新 mixin 放在新子包 `orgfs/composition/state/`
（`composition/` 已满 7 个文件）。

共用 mixin 里有少数地方必须先改成“可覆盖的小方法”，两个宿主才能共用，这就是包 D0（纯重构，旧行为逐字不变）：

| 钩子 | 从哪里抽出 | legacy 的实现（原样）| state 的实现 |
| --- | --- | --- | --- |
| `_stamp_versions(space, node_ids, author, actor)` | `projection.py:805-825` | 进程内计数、写 `snapshots` / `history` | 摘要 version、登记 base 文本 |
| `_content_covered(space, node)` | `projection.py:626-635`、`content.py:170-176,341-347` | `state_covers`，无 frontier 为 `unverifiable` | `vector_covers`，经注册表 |
| `_content_doc(space, doc_id)` | 44 处 `space.contents` 引用 | dict 取值 | 注册表加载并 pin |
| `_holders_hint(space, node)` | `projection.py:675-683` | `mesh.known_holders(...)` | 在线与已知持有方 |
| `_project_tree_change(space, previous, old_paths, attribution)` | `spaces.py:469-605` | 由 `apply_envelope` 调用，作者取信封 | 由 `apply_remote` 调用，作者取行 |
| `_record_row(space, node)` | `model.py:644-665`、`projection.py:957-958` | 现有行格式 | `tree_row_wire`，带 `deletedBatch` |
| `_publish_effects(space, …)` | `projection.py:206-224,364-378` | `_publication_records` + `_BroadcastPending` | `_Publish` |
| `RuntimeDirectory._maintain_spaces(now)` | `composition/directory.py:436-439` | 三行旧 pending 处理 | `sync.maintain(now)` |
| `RuntimeDirectory._ANNOUNCE_PROTOCOL` | `composition/directory.py:290-291,839-840` | `None`（不过滤）| `3` |

D0 的验收是“`tests/daemon/orgfs` 全目录与基线逐条相同”，它不引入任何新行为，所以可以单独合入。

### 3.4 分步

| 步 | 做什么 | 集成分支上怎么验证 | 这一步之后的状态 |
| --- | --- | --- | --- |
| 0 | 前置登记：`ORGFS_LAYOUT_UNSUPPORTED` 进错误码表并重生生成物；`BlobStore.unpin`；`stamp_commit` 改为“向量未前进才加时钟” | 各自的焦点测试；`tests/daemon/orgfs/{state,membership,sync}` | 新包可被装配 |
| 1（D0）| §3.3 的钩子重构 | `tests/daemon/orgfs -n 4` 与上一步逐条相同；结构门、边界门 | 旧行为不变 |
| 2（D1）| 协调者与 `StateOrgFs`（内存 store）；`StateImpl.make_facade` / `inject_write_failure` / `restore_last` / `document_vector` | 契约里只用 `make_facade` 的用例在 `state` 上绿；legacy 不变 | 单节点 facade 可用 |
| 3（D2）| `StateOrgFsRuntime`、文件 store、`SpaceSyncHost`、join；`StateImpl.make_runtime` / `connect` | 140 条契约在 `state` 上全绿（降级写入那一条两边保持 strict xfail）；C6 规模测试换真实实现 | 多节点可用，默认装配仍是 legacy |
| 4 | 切换演练：用 §3.4 末段的开关让 `LocalOrgFs` / `OrgFsRuntime` 两个名字指向新类，跑 `tests/daemon/orgfs`、消费方目录与全量 | 把变红的清单与分拣清单对照：应当只有“journal 专属”与尚未改写的“需改写” | 得到切换的精确红单；不合入任何改动 |
| 5（T1–T3，并行）| 按目录把“需改写”的测试改写到新类上；同文件里的 journal 专属用例随手删除 | 每个目录在演练开关打开时全绿 | 红单只剩整文件 journal 专属 |
| 6（D4a，可与 2–5 并行）| 纯删除的公共面收窄：内容清除四个方法、`parked_commits`、`document_covers_member_add` 及其请求方法、CLI、MCP、GUI 入口 | 计划 §10.1 的配方与钉清单的测试 | 新旧两个宿主都不再被要求提供这些方法 |
| 7 | 真实多节点验证（§7）| 用演练开关起真实 daemon | 切换的门 |
| 8（S）| 切换与删除，一个 PR：名字改指新类并去掉开关；删除旧 mixin、`storage/store`、`storage/replica*`、`storage/space_authority.py`、`mesh.py`、`network/` 除 `supplier_health.py` 外的全部、内容清除模块，以及整文件 journal 专属的测试 | 全量 pytest、ruff、结构门、边界门、`contract/run-all.sh --isolated-only` | 树内只有新实现 |
| 9（D4b、G、O，并行）| 语义变化的公共面：`remove(permanent=)`、`restore` 去 `version`、`status` 新字段、`org.network` 去 `parkedCommits`、`org.fetch` 的节点来源、GUI | 各自焦点测试；集成者重生生成物后的 `--check` | 公共面与设计 §1 一致 |
| 10（E3–E5）| 生成物、文档、skill、known-problems、迁移说明、CHANGELOG | 计划 §6 的阶段门 | 可回 dev |

契约 conftest 的改法（第 2 步）：`conftest.py:30-34` 对 `state` 参数加的是 strict xfail，一旦有用例通过就整体变红。
第 2 步把它换成“`StateImpl` 尚未接好的工厂方法抛 `NotImplementedError` 时该用例 xfail（非 strict，`raises=NotImplementedError`）”，
第 3 步工厂全部接好后删掉这个标记。这样第 2、3 步之间的集成分支上 `state` 参数既不是全红也不被跳过。

演练开关：`docs.py` 与 `runtime.py` 末尾各加一个仅测试用的名字选择（环境变量 `HYPRIAL_ORGFS_IMPL=state` 时把
`LocalOrgFs` / `OrgFsRuntime` 绑定到新类）。它只在第 3–8 步存在，第 8 步删除；默认值是 legacy，不进任何发布物。
改写后的测试在第 5 步直接导入新类并取别名，第 8 步用一次机械替换改回正式名字。

## 4. 工作包

文件所有权规则：下表“独占”列里的文件，同一时间只属于一个包；写着“接手”的表示前一个包合入后才交给后一个包。
生成物（`contract/daemon-request/openapi.json`、`gui/src/client/generated/*`、contract mock、golden manifest、CLI surface
与 inventory）只由集成者重生（计划 §5 第 3 条）。改动量是读代码后的估计 **[推]**。

| 包 | 分支 | 目标 | 独占的文件 | 依赖 | 验收 | 改动量 |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | `chore/orgfs-phase-d-prereq` | 错误码登记；`BlobStore.unpin`；提交时钟条件化 | `src/hyprial/error/codes.py`、`kernel/impl/contracts/daemon/ipc_errors.py`、`orgfs/state/interface.py`（改为从错误码表导入）、`orgfs/storage/blobs.py`、`storage/blob_authority.py`、`orgfs/membership/authorship.py`、`orgfs/sync/blobs.py`（改用 `unpin`）及各自测试 | 无 | `tests/daemon/orgfs/{state,membership,sync,blobs}`、`tests/daemon/ipc/error`；因为动了 `src/hyprial/`，跑全量与 ruff | +150 / -30 |
| D0 | `refactor/orgfs-facade-hooks` | §3.3 的钩子重构，旧行为不变 | `orgfs/document/{projection,effects,model}.py`、`document/operations/{spaces,content,tree}.py`、`composition/directory.py` | P0 | `tests/daemon/orgfs -n 4` 与基线逐条相同；两个结构门（`projection.py`、`model.py`、`directory.py` 只能净减行）| ±400，净减 |
| D1 | `feat/orgfs-commit-coordinator` | §2 的协调者与 `StateOrgFs` | `orgfs/document/coordinator/*`（新）、`tests/daemon/orgfs/coordinator/*`（新）、`tests/daemon/orgfs/contracts/{conftest.py,support/factory.py}` | D0 | 契约中只经 `make_facade` 的用例在 `state` 上 PASS；新增单元测试覆盖注册表淘汰与 pin、导出即换实例、`StateStoreBroken` 重开、观察到行消失、frontier 修正的终止 | +1800 |
| D2 | `feat/orgfs-state-runtime` | `StateOrgFsRuntime`、启动扫描、`SpaceSyncHost` 装配、join、`await_content`、关闭顺序；演练开关 | `orgfs/composition/state/*`（新）、`orgfs/docs.py`、`orgfs/runtime.py`（仅开关）、`tests/daemon/orgfs/scaling_state/conftest.py`；**接手** `contracts/support/factory.py` 与 `conftest.py` | D1 | 140 条契约在 `state` 上 PASS（1 条已知问题两边 xfail）；C6 规模测试换 `FileStateStore` 后 PASS；启动遇旧布局目录只拒绝该空间的新测试 | +1200 |
| D4a | `refactor/orgfs-drop-content-purge` | 删除 `purge_plan` / `purge` / `purge_status` / `unban` / `parked_commits` / `document_covers_member_add` 的公共面（Protocol、请求方法、CLI `fs purge *`、MCP、GUI 的内容清除入口）。旧宿主类上的实现留给包 S | `orgfs/api.py`、`orgfs/__init__.py`、`application/messaging/orgfs_bridge.py`、`ipc/router.py`、`request_catalog/org/orgfs/{read,write}_specs.py` 与两个 examples 包、`request_catalog/migration.py`、`mcp/server.py`、`shell/impl/cli/commands/fs/commands.py`、`contract/mcp-tools/run.py`、`contract/orgfs/content-purge.md`、`gui/src/features/org/impl/api/orgfs-request-api.ts` 与 `domain/live-source.ts` 中内容清除的部分、`gui/tests/unified-backend/connection.test.ts`、计划 §10.1 第 5、11 步列的钉清单测试 | 无（可与 D0–D2 并行；与 D4b 串行）| 计划 §10.1 的红测试与钉清单目录；GUI 的 `tsc --noEmit`、请求路径门、mock 来源门 | -900 |
| T1 | `test/orgfs-admission-on-state` | 改写成员与准入类测试（用 C2 的 `forge_update` 经 `apply_remote`）| `tests/daemon/orgfs/{authority,storage/tree}/`、`mesh/device_rows/`、`mesh/test_orgfs_device_row_owner.py`、`tests/daemon/org_membership/{rework,storage}/`、`tests/daemon/org/network/` 分拣为需改写的 16 条所在文件 | D2 | 演练开关打开时这些目录 PASS | 约 60 条改写 |
| T2 | `test/orgfs-sync-on-state` | 改写同步、presence、反熵、blob 拉取类测试 | `tests/daemon/orgfs/{mesh（T1 之外）,presence,join,hydration,blobs,replication,instrumentation}/`、顶层 5 个文件、`document/test_post_commit_async.py`、`lifecycle/test_orgfs_late_callback_after_close.py`、`tests/integration/actor_preservation/orgfs/` | D2 | 同上 | 约 150 条改写；分拣为“待定”的 19 条在此定去留 |
| T3 | `test/orgfs-facade-on-state` | 改写 facade、存储、本地清除与消费方测试 | `tests/daemon/orgfs/{contracts/test_orgfs_*.py,document/test_scoped_rollback.py,lifecycle/test_space_purge.py,storage（tree 之外）,addressing}/`、`tests/daemon/{organizations,provider_keys,org_membership/migration}/`、`tests/integration/orgfs/` | D2 | 同上；与契约重复的用例删除而不是改写（C4 报告 §5 的映射）| 约 110 条改写 |
| V | — | §7 的真实多节点验证 | 只写计划 §9 的证据行与 evidence 文件 | D2 | §7 表逐项 PASS / FAIL / BLOCKED | — |
| S | `refactor/orgfs-switch-to-state` | 切换默认装配；删除旧实现与整文件 journal 专属测试；搬迁被依赖的名字 | `orgfs/docs.py`、`runtime.py`、`mesh.py`、`document/**`（旧 mixin 与钩子的 legacy 一半）、`composition/{lifecycle,content,checkout,vocabulary,space_purge}.py`、`storage/store/**`、`storage/replica*.py`、`storage/space_authority.py`、`network/**`、`projection/purge.py`、`transport/keys.py`（删 `orgfs_log*`、`orgfs_snapshot`、旧 `orgfs_sync`）、`scripts/` 下的 journal 合并脚本与其测试、`resident_metadata` 的登记行、分拣清单里整文件“journal 专属”的测试 | D2、T1–T3、D4a、V | 全量 pytest、ruff、两个结构门、`contract/run-all.sh --isolated-only`；`git grep` 确认无 `journal.sqlite3` 的写入方 | 约 -26000（产品约 -12000：`storage/store` 5263、`storage/replica*` 1642、`space_authority.py` 807、`mesh.py` 404、`network/` 3097、内容清除 1036；测试约 -13700，分拣 §2.1）|
| D4b | `feat/orgfs-surface-minimal` | `remove(permanent=)`、`restore` 去 `version`、`status` 新字段的请求、CLI（`fs rm --permanent`、`fs trash restore`、`fs space status`）、MCP、线路 schema | **接手** D4a 的同一批文件；另加 `request_catalog/org/orgfs/wire.py` | S | `tests/daemon/ipc/contracts/org`、`tests/shell/cli`、`tests/integration/orgfs/commands` | +300 / -200 |
| O | `refactor/org-callers-state-status` | `org.network` 去 `parkedCommits`（C5 报告 §7 的清单）；`org.fetch` 的节点来源改由设备记录；`cell_actor` 三处桩的复核 | `daemon/impl/org/network/directory.py`、`org/network/status/view.py`、`org/orgfs_migration.py`、`request_catalog/org/{network_specs.py,management_examples/examples.py}`、`tests/daemon/org/network/status/`、`tests/daemon/org/cell_actor/` 相关文件 | S | `tests/daemon/org`、`organizations`、`ipc/contracts/org` | +150 / -200 |
| G | `feat/gui-orgfs-minimal` | 文件夹恢复改接新 `restore`（去掉 `ORG_FOLDER_RESTORE_UNSUPPORTED`，`live-source.ts:874`、`dev/org/fixture-gateway.ts:659`）；`status` 新字段；`copy.ts:83` 文案；`listTrash` 的 `limit` | `gui/src/features/org/**`（**接手** D4a 动过的两个文件）、`gui/src/dev/org/**`、`gui/tests/modules/dev04/*` | D4b 合入且集成者已重生客户端 | GUI 单套界面规则与 UI 交接模板；`tsc --noEmit`、模块边界、请求路径门、mock 来源门、相关测试；真实 daemon 加浏览器的文件夹恢复验收，不能做则报 BLOCKED | ±300 |
| E | 集成者 | 生成物重生；`docs/guides/migration-orgfs-minimal.md`；known-problems（设计点名的各项与 §6 中决定不修的）；skill（`hyprial-e2e`、`hyprial-ops`）；CHANGELOG；`tests/daemon/orgfs/addressing/test_orgfs_uri.py` 的错误码扫描范围扩到新包 | 生成物与文档 | 全部 | 计划 §6 阶段门 | — |

顺序：P0 → D0 → D1 → D2 →（T1 ∥ T2 ∥ T3 ∥ V）→ S →（D4b → G）∥ O → E。D4a 与 D0–D2 并行，须在 S 之前合入。
同时存活的写代码 agent 最多 3 个（T1–T3 阶段），其余阶段是 1–2 个。

每个测试目录归谁（分拣清单 §2.1 的一级目录）：

| 目录 | 保留 / 需改写 / 删除（条）| 需改写归 | 整文件删除归 |
| --- | --- | --- | --- |
| `addressing/` | 35 / 0 / 1 | — | 已随 A1、A3 删 |
| `authority/` | 10 / 24 / 30 | T1（其中 mesh 入站与 blob 队列 6 条转 T2）| S |
| `blobs/` | 9 / 5 / 5 | T2 | S |
| `commands/`、`server/` | 66 / 0 / 0 | — | — |
| `contracts/`（旧的顶层文件）| 31 / 4 / 12 | T3 | 已随 A2 删 |
| `document/` | 0 / 44 / 10 | T3（`test_scoped_rollback` 的 40 条由契约 `durability/test_write_failure.py` 取代，删）；T2（`test_post_commit_async`）| S |
| `hydration/`、`join/`、`presence/`、`instrumentation/` | 7 / 51 / 7，待定 11 | T2 | S |
| `lifecycle/` | 0 / 20 / 11 | T3（`test_space_purge` 19 条）、T2（1 条）| S |
| `mesh/` | 10 / 76 / 38，待定 4 | T1（`device_rows/` 与 `test_orgfs_device_row_owner.py` 约 45 条）、T2（其余）| S |
| `purge/`、`replication/` | 0 / 1 / 84 | T2（1 条）| S |
| `storage/` | 8 / 20 / 112，待定 1 | T1（`tree/` 的 1 条）、T3（其余）| S |
| 顶层 5 个文件 | 0 / 35 / 2 | T2 | S |
| `state/`、`membership/`、`sync/`、`scaling_state/`、新 `contracts/` 子目录 | 新包自带 | — | — |

## 5. 不重写、必须保住的现有行为

“契约”列是 `tests/daemon/orgfs/contracts/` 下的文件与用例（取自 C4 报告 §3、§5，本人没有读测试文件）；“缺”表示没有
经工厂参数化、因而不会在 `state` 实现上自动执行的测试。

| 行为 | 实现它的现有函数 | 改接时会不会被波及 | 契约 |
| --- | --- | --- | --- |
| 命令循环：每空间串行、custody 上限、`resource-exhausted` | `space_state.py:439-528`、`:329-396` | 只加一条命令；新 mixin 的方法必须进 `_SPACE_STATE_METHODS` 与 `_make_space_operation`（`space_state.py:59-126`），漏了会绕过命令循环直接在调用线程上写 | **缺**。现有 `authority/test_orgfs_authority_corrections.py::test_facade_total_custody_bounds_deferred_and_rebases_restart` 打桩私有字段，归 T1 改写 |
| generation 重启重放、不重复已接纳的写 | `space_state.py:399-436` | 不波及 | **缺**（`authority/test_orgfs_actor_boundary.py::test_space_authority_restarts_without_repeating_an_admitted_commit` 打桩 `OrgSpaceAuthority`，归 T1）|
| waiter 结算与错误类型还原 | `space_state.py:329-352,504-528` | `:514-515` 还原 `StoreError`，名字搬迁后同步 | `durability/test_write_failure.py`（间接：调用方拿到 `OrgFsError` 或注入的异常）|
| close 截止时间、可重试 | `effects.py:485-537`、`runtime.py:212-310` | 波及：关闭顺序要加 `SpaceSync.close` 与 store 关闭 | `durability/test_reopen.py`（正常关闭后重开）；超时与重试 **缺**（`test_runtime_close_retries_after_a_partial_drain_timeout` 归 T1）|
| `TreeDocument` 移动日志与环保护 | `model.py:667-691,713-771`、`tree.py:52-59,83-86` | 行格式加字段会波及物化的调用方（§6 R10）| `tree/test_move.py`（改名、移入、子树随动、环保护、坏 move 无副作用）；并发互移的日志回放 **缺**（`storage/tree/test_orgfs_tree.py::test_move_log_replay_skips_cycle` 是 `TreeDocument` 单元测试，保留即可）|
| name conflict 与 ambiguous-path | `projection.py:572-578,600-613` | 不波及 | `tree/test_paths.py::test_name_conflict_is_flagged_and_paths_become_ambiguous` |
| id 锚定引用、URI 引用、路径约束 | `projection.py:443-535,842-875` | 不波及 | `content/test_read_write.py::test_id_anchored_child_ref_creates_and_reads_under_the_anchor`、`test_invalid_write_targets_fail_without_mutation`；URI 部分在 `addressing/test_orgfs_uri.py`（请求层，不经工厂，**缺**）|
| `_ContentDocument` 差量写入与三方合并 | `model.py:793-852`、`content.py:180-212` | base 文本来源改变（§6 R1）| `content/test_cas.py::test_base_version_current_is_a_plain_overwrite` 及三方合并、未知 `base_version` 用例；多节点 `multinode/test_convergence.py::test_concurrent_edits_from_one_base_keep_both_changes` |
| `expect_version`、create-only、`stale-write` | `content.py:237-241,275-282,382-393` | version 取值改变（§6 R3）| `content/test_cas.py::test_expect_version_mismatch_is_stale_write_and_keeps_content` 及字节版、create_only 用例 |
| 4 MiB 上限 | `content.py:228-229` | `ORGFS_TEXT_MAX` 改为从 `membership` 一处导入 | `content/test_read_write.py::test_text_over_four_mib_rejects_with_blob_guidance` |
| watch：提交后、锁外、按提交顺序 | `projection.py:826-839`、`effects.py:201-239,340-373` | 不波及；远端事件的生成改由 `apply_remote` 触发 | `watch/test_watch.py`（冻结签名、5 种事件按序、回调内可读 facade、提交顺序、失败写不发事件）；多节点 `test_online_replication_refreshes_reads_and_watch` |
| 效果 lane 过载时在提交前拒绝写 | `model.py:383-400`、`effects.py:561-573` | 不波及 | **缺**（C4 报告 §10：只能 monkeypatch `_FACADE_EFFECT_CAPACITY`）|
| 持久化失败回滚 | `projection.py:180-201,330-359` | 重写为 §2.3 | `durability/test_write_failure.py`（12 种写入口 × facade 与 runtime）、`test_failed_space_creation_leaves_no_space` |
| lazy hydrate：内容未到不给陈旧读 | `content.py:170-176,341-347`、`projection.py:667-716` | 触发方式改变（§1.1）| blob：`multinode/test_convergence.py::test_a_remote_blob_not_yet_fetched_reports_typed_content_pending`；文本的 `content-pending` 与随后收敛 **缺**（现有 `hydration/test_orgfs_d4_verified_content.py::test_text_required_frontier_blocks_stale_read_then_converges` 归 T2）|
| 跨节点新建文档不先以空内容出现 | `spaces.py:369-389,566-590,611-637` | 逻辑保留并抽出；事件作者来源改变 | **缺**（分拣 §5.3 第 13 项；归 T2 用 watch 事件序列写）|
| 成员行可见性（已移除成员的受保护行隐藏）| `projection.py:539-560` | `space.members` 的填充方式改变 | **缺**（`mesh/test_orgfs_device_row_owner.py::test_owner_member_removal_leaves_rows_inert_on_every_replica` 归 T1）|
| checkout | `projection/checkout*.py`、`composition/checkout.py:57-143` | 不波及（只用 facade 公共方法）| **缺**；`commands/test_orgfs_checkout.py` 15 条直接构造 `LocalOrgFs`，切换后自动跑在新实现上 |
| webserver | `webserver/*`、`web.py` | 不波及 | **缺**；`server/http/` 51 条经请求层，切换后自动跑在新实现上 |
| blob 存储与引用账本 | `storage/blobs.py`、`storage/blob_authority.py`；写入点 `content.py:394-403` | 永久删除新增释放路径（§6 R13）| `content/test_binary.py`（往返、hash、重开、尺寸）；引用释放 **缺** |
| 本地空间清除（含 crash-resumable intent）| `composition/space_purge.py` | `_detach_space` 与 readback 引用的资源类型改变 | **缺**；`lifecycle/test_space_purge.py` 19 条归 T3 |
| import / export | CLI 与 `path_authz` 门（未读）| 不波及 [推] | **缺**；`tests/daemon/path_authz/test_orgfs_gate.py`、`tests/shell/cli/orgfs/` 不经工厂 |
| `read_bytes` 对文本文档返回 UTF-8 字节 | `content.py:339-348` | 不波及 | `content/test_binary.py::test_read_bytes_of_a_text_document_returns_its_utf8` |
| 递归 `listdir` 的顺序 | `content.py:79-93` | 不波及 | `content/test_read_write.py::test_recursive_listdir_returns_visible_descendants_in_path_order` |

标“缺”的行有两类：现有测试直接构造 `LocalOrgFs` / `OrgFsRuntime` 的，切换后名字指向新类，自然在新实现上执行，
但**切换前**没有保护，所以第 4 步的演练是它们唯一的前置检查；完全没有测试的（效果 lane 过载、blob 引用释放、
文本 `content-pending` 的收敛、原子出生）由 D1 / T2 补。

## 6. 风险：设计没有覆盖或与代码冲突之处

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| R1 | `content.py:285-291`；设计 §6 | **设计冲突。** 设计要求阶段 D 去掉 `space.snapshots`，但 `write_text(base_version=)` 的三方合并用它取 base 文本，这是设计 §7 点名必须保留的行为。现状每个版本的正文都留在内存里，从不淘汰 | 保留一个只为它服务的有界缓存（§2.6）。未命中时报 `stale-write`，调用方重读重写。上界（条数或字节）需要定；这同时改变“很旧的 base_version 仍可合并”的现状，写进 known-problems |
| R2 | `content.py:472-487`、`composition/lifecycle.py:661-672`、`model.py:436-442` | `changes_since(since_version)` 靠解析 version 里的 tree 修订号排序。设计 §1.1 把 version 改为摘要，没有顺序；设计没有提到 `orgfs.watch` 的轮询形态 | 每空间事件序号（§2.6）。另注意 `self._events` 是无界 list（`projection.py:832-834`），属既有问题，不在本次范围 |
| R3 | 设计 §1.1；`projection.py:616-664` | version 定义含“内容 state vector”，`_node_info` 还要 `size = len(正文)`（`:625`）与 `content_state`。对 2.5 万文档的 `listdir(recursive=True)` 或 `trash()`，这等于加载全部内容文档，注册表的 256 上限失去意义 | 未加载的文档：version 用行里的 `contentFrontier` 代替本地向量，`size` 用行里的 `refSize`，`content_state` 只在已加载时为 `arrived` 的确定值、否则按“文件存在且上次已核对”的缓存位给出。稳态下行 frontier 等于本地向量，两种取法得到同一个 version；只在 pending 或修正尚未写入时不同。需要设计确认 |
| R4 | 设计 §4.2、§4.6；`sync/pull.py:118-128` | `pending_content(limit)` 与 `status.content_pending` 需要“哪些文档的本地向量未覆盖行 frontier”。不加载全部内容文档就无法回答；重启后这份信息也不在磁盘上。C3 的测试 host 全部在内存里，没有暴露这一点 | 协调者维护内存中的“待核对”集合：远端 tree 更新改了某行 frontier 时加入；打开空间时全部文档行进入“未核对”队列，每次 `maintain` 核对有界的一批（加载、比较、移出或标为 pending）。核对完成前 `status.synced` 不得为 `True`。这是本次最需要先定的一点 |
| R5 | `composition/lifecycle.py:622-658`、`spaces.py:241-253`；设计 §3.5、§4.3 | join 的时序设计没有写。新 sync 需要一个 `SyncHost` 才能拉，而此时本地没有这个空间：节点门要按本地成员表判断 supplier（本地 meta 为空，必然拒绝）；`AdmissionView.owner` / `space_name` 未知；拉到一半失败会留下半个空间 | 临时空间：建 v3 目录与空注册表 → 节点门在“本地 meta 没有 `space.owner`”期间只查 presence（对端是调用方显式选的持有方，与现状 `_join_sync` 的信任相同）→ 拉 meta、tree → 确认 `self.author` 在成员表里且 supplier 过得了正式节点门，否则丢弃临时空间并报错 → 转正、公告。supplier 侧的 `requester_allowed` 已保证未被邀请的节点拉不到任何东西 |
| R6 | `runtime.py:141-159`；C1 报告 §12 | 建空间或 join 中途崩溃留下 `uninitialized` 目录，或 v3 目录里 meta 没有 `space` 根。设计只说 `uninitialized` “可重新 create”，没说启动时谁清 | 启动扫描把这两种目录当作未完成的创建，直接删除（它们从未被确认给调用方）；删除前按 `space_purge` 的 fence 规则核对路径 |
| R7 | `spaces.py:82,116`、`model.py:624-635` | 现状建空间时 tree 根行只在内存里，第一次 tree 写才落盘。新实现根行必须由创建者经 `stamp_commit` 写并与 meta 同批提交；加入方打开空 tree 时没有根行，而 `projection.py:870` 等处直接取 `space.nodes["root"]` | `space.nodes` 里的 root 在 tree 没有根行时用一个不落盘的占位节点（现状 `spaces.py:322-323` 已有同样的兜底）；任何写入前要求根行已到，否则 `content-pending` |
| R8 | `spaces.py:186-205,218-231`；`membership/policy.py:3-4` | 成员移除从“留 `removedAt` 墓碑”变为删键，上一次的 `addedAt` 随之丢失。设计 §5 要求“重新加入时 `addedAt` 严格大于该用户上一次的值”，删键之后无从比较 | 两种选一：移除时不删键而写 `mode` 之外的保留字段（要改 C2 的行格式与准入）；或把保证弱化为“严格大于 owner 节点本进程见过的最大 `addedAt`”。需要设计裁定；C5 的 `test_leave_member_added_at.py::_readd_bob` 断言前者的效果 |
| R9 | `projection.py:816`、`spaces.py:418-420,513-514`；设计 §3.3 | `NodeInfo.modified_via`（actor）现在来自信封的 `origin.actor`。tree 行只新增了 `author`，没有 actor；设计说 `NodeInfo` 字段不变 | 行里再加一个可选的 `via`，由协调者与 `author` 同时写；或接受远端与重开后的 `modified_via` 为 `None`。需要设计裁定，并查消费方是否读它 |
| R10 | `model.py:713-759`；`membership/staged.py:201-236` | 两份物化并存：`TreeDocument.materialize` 对非数字 `ts` 抛异常（`:740` 的 `int(...)`），`membership.materialize_tree` 按 0 处理（C2 报告 §8）。准入用后者、facade 用前者，恶意输入下两者对“哪个移动判负”的结论可能不同。另外 `record_move` 的 `ts` 是进程内计数 `self._clock`（`projection.py:945-947`），重启后从 0 开始，跨重启的移动顺序只靠 `peer`、`node` 兜底（既有问题）| facade 改用 `membership.materialize_tree`，删掉 `TreeDocument.materialize` 自己的实现；`author` / `deletedBatch` 是行里多出来的键，两份物化都只取 `parent` / `name`，环保护不受影响 **[码]**。移动时钟的问题登记进 known-problems，不在本次修 |
| R11 | `model.py:774-788`；设计 §3.3 | `_ContentDocument` 只有 `text` 一个根。`__orgfs__` 与 `__orgfs_authors__` 由 `stamp_commit` 在同一个 `Doc` 上写，`set()` 的差量事务（`:844-852`）必须与 `stamp_commit` 处在同一个外层事务里才是一个 update；现有 `set()` 在值不变时直接返回（`:795-796`），此时提交 helper 仍会因“向量未前进”加时钟，产生一次内容没变的提交 | 协调者在外层开事务；值不变的写在进入提交之前就返回（现状 `content.py:293-308` 已如此，保持）|
| R12 | `sync/space.py:132-137,298-355`；C3 报告 §9 | `SyncHost` 会从 transport 回调线程、ingress 线程、sync 工作线程被调用。读方法持 `_space_lock`，一次慢提交（fsync、大 tree 合并约 25–62 ms，C6 实测）期间 supplier 侧的查询处理线程被挡住；`apply_remote` 经命令循环，custody 满时返回 `resource-exhausted`，sync 一侧如何对待这个异常没有约定。pycrdt 方面：全部 `Doc` 不挂订阅，跨线程使用只靠这把锁，与现状相同（计划 §10）| `read_vectors` 读协调者在每次提交后缓存的不可变向量表，不持锁；`apply_remote` 把 `resource-exhausted` 映射为 `Pending("dependency")`。`StateSpaceSync` 用的是普通线程池而不是 actor host（`space.py:132-137`），关闭与监督不经 `OrgFsActorHostCustody`，是否要换由 D2 评估，不阻塞 |
| R13 | `content.py:394-403`、`storage/blobs.py:527`；设计 §6 | blob 引用账本按“空间加摘要”记，`release(space, digest)` 释放该空间对该摘要的**全部**引用（C3 报告 §4-8）。同一空间里两个文件内容相同时，永久删除其中一个不能释放；另外 `write_bytes` 在提交**之前**就 `put`（`:397`），提交失败后引用留着 | 永久删除（本地与观察到的远端）在提交成功后，用 `retained_blob_digests`（`projection.py:427-439`）算出仍被引用的摘要，只释放不在其中的。提交失败遗留的引用是既有问题，登记即可 |
| R14 | `projection.py:119-134` | 回滚快照在**每次**本地写之前全量导出 tree 与 meta。2.5 万文档的 tree 约 14 MB，一次导出十几毫秒（C6 实测载入导出 38 ms）；现状同样如此，但新实现下它成了写路径上唯一与空间大小成正比的步骤，与 C6 的“提交成本与文档数无关”契约冲突 | 回滚改为失败后从 store 重新 `load` 受影响文档并重新物化（提交没有生效时磁盘就是提交前的状态）。需确认 C6 的计数断言是否把内存导出算在内 |
| R15 | `composition/directory.py:290-291,839-840`、`runtime.py:247-263` | 公告过滤改成 `protocol == 3` 之后，runtime 关闭时发的空公告（不带 `protocol`）会被对端忽略，持有方记录不撤销 | 包 D2 同时给它加 `"protocol": 3` |
| R16 | `sync/interface.py:113-120` | `outcome_kind` 按属性识别准入结果。协调者若返回别的带 `code` 属性的对象（例如把异常当返回值）会被当成 `Rejected` | P0 或 D2 把它改成 `isinstance`（设计 §8 待办）|
| R17 | `tests/daemon/orgfs/contracts/conftest.py:30-34`；设计 §8 | 设计说接入时“去掉 `state` 参数的预期失败标记”。按原样做，D1 与 D2 之间集成分支上 `state` 参数会有一批必然失败的用例 | §3.4 的两段式改法 |
| R18 | 设计 §3.1；C4 报告 §6 | `multinode/test_remote_admission.py::test_writes_of_a_member_demoted_while_apart_do_not_change_the_owner` 在两个实现上都要保持 strict xfail。C4 目前只用 `legacy_xfail` 标了 legacy | D2 把它改成对两个参数都 strict xfail，原因写设计 §3.1 |
| R19 | `orgfs/` 根、`composition/`、`state/`、`membership/`、`sync/`、`storage/`、`network/` | 这七个目录都已是 7 个文件。`StoreError`、`document_file_stem` 在包 S 之前没有可以搬去的现成位置 | 这两个名字在包 S 里、删除 `storage/replica*` 腾出名额之后搬进 `storage/` 的新文件；`supplier_health.py` 依赖的常量搬进它自己。`src/hyprial/error/` 按仓库规则将来收纳异常类型，但那是另一件事 |

设计 §4.2 第 1 版 frontier 修正不收敛的问题（C3 报告 §11.1）已由 `d5ff852cd0` 的修订解决，这里不再列；协调者按修订后的
三条规则实现，`refSize` / `refSha256` 只在写入值等于本地向量时填写。

## 7. 真实多节点验证的最小场景

“假传输”指契约工厂的 `FakeNetwork`（`contracts/support/network.py`）或 C3 的进程内 session；“真实”指用演练开关起的
隔离 home 上的真实 daemon 与真实 transport，至少两台机器（一台 Linux、一台 macOS）。结果只写 PASS / FAIL / BLOCKED。

| # | 场景 | 要观察的结果 | 假传输可先验证 | 必须真实 daemon |
| --- | --- | --- | --- | --- |
| 1 | A 建空间、邀请 B；B join | B 的 `spaces()`、`members()`、`listdir` 与 A 相同；B 在 A 的 `holders_online` 里；join 失败的三种情况（无持有方、未被邀请、持有方中途离线）各自的错误码，且 B 的磁盘上不留目录 | 是（契约 `members/` 的 join 用例）| 是：发现、公告、liveliness 与 presence 的时序只有真实 transport 有 |
| 2 | A、B 互相写文本与二进制 | 双方读到对方的写入；watch 事件各一条且先 `created` 后带内容；`status.synced` 在静默后为 `True` | 是 | 是：广播 key 与 16 MB 以上临时 blob 的分块 |
| 3 | 同基并发编辑同一文档 | 双方正文相同且含两边的修改；tree 行的 `contentFrontier` 最终等于两边向量之并；收敛后再静置两个反熵周期，tree 向量不再变化（修正写终止）| 是（C3 `test_convergence.py` 与契约 `test_concurrent_edits_from_one_base_keep_both_changes`）| 建议：验证真实丢广播下靠拉取收敛 |
| 4 | B 断网期间 A 写入、纯删除、移动、软删除后恢复；B 重连 | B 在一个反熵周期内追上，包括纯删除（把文本清空）| 是 | 是：transport 重建后的重新声明与补拉 |
| 5 | A 写入后立刻被杀（来不及广播），重启 | A 重启后数据在；B 经拉取得到该写入 | 部分（契约 `test_a_write_from_before_a_restart_reaches_the_peer` 是正常关闭）| 是：进程被杀 |
| 6 | 提交过程中杀进程（多文档批次：新建文件）| 重启后该文件要么完整存在、要么完全不存在；`orgfs.state.batch-recovered` 或 `batch-discarded` 日志与结果一致；其余空间照常打开 | 否（C1 用故障注入逐点测过 store；这里验证的是装配）| 是 |
| 7 | A 移除 B；分区期间 B 继续写；愈合 | A 的状态不含 B 分区期间的写入；A 的 `status.rejected_updates` 不因反复同步而持续增长（拒绝记忆生效）；B 本地写在得知移除后报 `not-a-member` | 是（契约 `test_writes_of_a_member_removed_while_apart_do_not_change_the_owner`）| 是：节点门依赖设备记录，要用真实的 org 目录空间而不是 `trust_tailnet_members` |
| 8 | org 目录空间：设备记录、leave 请求、成员 agent 清单的写入与读取 | 路径所属用户能写、其他成员本地被拒 `protected-path`；调用方读到的 `document_authors` 只有路径所属用户；leave 流程走完 | 部分（C2、C5 的单元测试）| 是：这是唯一同时经过节点门、受保护路径规则与四个调用方的路径 |
| 9 | 永久删除：A 对一个有内容的文件夹 `remove(permanent=True)` | A、B 的 `docs/` 下对应的 state / delta 文件消失；blob 引用释放；离线的 C 上线同步 tree 之后才删除自己的副本；受保护路径下被拒 | 是（需 D1 新增测试）| 是：观察磁盘文件 |
| 10 | 三节点，A 只与 B 通、B 与 C 通 | A 的写入经 B 到达 C（转发）；C 上的作者字段是 A | 是（C3 的三节点测试）| 建议 |
| 11 | 2.5 万文档的合成空间：冷启动、首次同步、读一个尚未到达的文档 | 启动不读 `docs/` 下的文件；新节点 join 后目录先到、内容分批到；读未到达的文档得到 `ORGFS_CONTENT_PENDING` 并在期限内到达；`status.content_pending` 单调下降到 0 | 部分（C6 规模测试）| 是：真实网络下每轮 128 个文档的节奏与总耗时只记录、不作断言 |
| 12 | 升级：home 里留有旧布局的空间目录 | daemon 能启动；该空间的请求报 `ORGFS_LAYOUT_UNSUPPORTED` 且 `reason = legacy-journal`；其他空间可用；旧目录字节不变 | 否 | 是；可用生产 owner-home 的只读克隆演练（计划 §9 的授权），不碰生产 daemon |
| 13 | GUI：文件夹删除后恢复、文件永久删除入口不存在、空间状态显示 | 与 UI 交接模板一致的双入口证据 | 否 | 是（真实 daemon 加浏览器）；做不了就报 BLOCKED，不以单元测试代替 |

第 1、2、4、7、8、12 项不过，不进入包 S。
