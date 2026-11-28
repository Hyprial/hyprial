# OrgFS 测试分拣（C7，2026-11-25）

状态：只读调研产出，属计划 `orgfs-minimal-plan-2026-11-24.md` §4 阶段 C 的工作包 C7。本文不改任何测试或源码。
C4 据本文决定哪些测试参数化到新旧两套实现；阶段 E2 据本文删除 journal 专属测试。

基线：分支 `design/orgfs-test-triage`，head `ef30e69ed9`。测试 id 与条数来自
`uv run pytest --collect-only -q`（`tests/daemon/orgfs` 共 787 条；orgfs 之外的范围共 599 条）。没有运行测试。

## 证据与方法

- **读法**：每个文件都读了“骨架”，即导入、类/函数定义、所有 `assert`/`pytest.raises`、构造调用与 `_db.execute`
  一类的行；拿不准的用例再读完整函数体。表中的行号都是读代码确认的 **[码]**。
- **分类**是按计划 §1 的决定与 §3 的删除清单作出的判断。某条依赖阶段 B 尚未定下的设计时，依据栏会写明，
  分类记为“待定”或注明 **[推]**。
- **条数**是 pytest 收集到的 id 数，参数化的每组参数各算一条。行数是 `wc -l`。一个文件拆成多行时，汇总表按条数比例把行数
  分给各类（估算）。
- **“驱动层”词汇**：facade = `LocalOrgFs`/`OrgFs`；runtime = `OrgFsRuntime`（同时用到 `runtime.facade`）；
  daemon 请求 = `DaemonApplication.handle/_handle_orgfs("orgfs.*")`；store = `LocalSpaceStore`/`FileSpaceStore`/SQLite/envelope；
  mesh 内部 = `OrgFsMesh` 及其私有字段；replica = `ReplicaStore`/`serve`。
- **范围**：`tests/daemon/orgfs/` 下全部 98 个有内容的 `.py`（其中 6 个是非测试的脚本或支持文件；空的 `__init__.py` 不计），另含 orgfs 之外的 56 个文件（其中 4 个支持文件），
  按两条规则选取：(a) 路径含 `orgfs` 的测试与支持文件（评估文档所说约 5.6k 行指的就是这些）；
  (b) 简报点名的 `tests/daemon/{organizations,org_membership,cells}`、`tests/daemon/ipc/contracts/org`、
  `tests/shell/cli/{orgfs,org_membership}` 中直接导入 OrgFS 实现、调用 `orgfs.*` 请求或使用 OrgFS 假件的文件。
  其他只把 OrgFS 当后端顺带用到的测试（约 43.6k 行，例如 `tests/daemon/org/**`、`conversations/**`、`pac/**`、
  `contract_mock/**`、`golden/**`）**未读**，见 §6。

## 0. 摘要

| 类别 | `tests/daemon/orgfs` 条数 | 其外条数 | 合计 |
| --- | ---: | ---: | ---: |
| 契约-保留 | 176 | 504 | 680 |
| 契约-需改写 | 280 | 54 | 334 |
| journal专属-删除 | 312 | 39 | 351 |
| 待定 | 19 | 2 | 21 |
| 合计 | 787 | 599 | 1386 |

最重要的发现：

1. **目前没有可以“对新旧实现参数化”的接缝。** `tests/daemon/orgfs` 没有任何 `conftest.py`。各测试自己内联构造：
   `LocalOrgFs(` 在 `tests/` 出现 126 次（46 个文件），`OrgFsRuntime(` 265 次（77 个文件），`LocalSpaceStore(`/`FileSpaceStore(` 85 次（29 个文件）。
   facade 现有的持久化接缝是 `stores.commit(doc_id, mutate, *, author, actor)`（`document/effects.py:595-604`）。
   这个接缝假设 facade 与 store 各持一份 Doc，目标形态（计划 §2 差别 2）不再有这种分工，所以**不能**拿它当 C4 的接缝。
   C4 要先新建一个 facade/runtime 级的工厂 fixture（见 §5.1）。唯一现成的“同一批测试对两种后端参数化”样板是
   `tests/daemon/provider_keys/storage/test_orgfs_owner_store.py:23-35`。
2. **安全规则几乎只在 store/envelope 层有测试。** 远端写入准入、被移除成员的写、受保护目录行（devices/people/invites/leaves）
   的作者规则共 68 条“需改写”用例，全部靠伪造 envelope 或调用 `store.admission(envelope)` 驱动。
   没有一条经过 facade 或 sync 的入站接口。B.3（远端准入规则）定稿后，C2 要先做一个“向节点注入一条（伪造的）Yjs update”的测试工具，
   否则这一批无法改写（见 §5.3）。
3. **`restore(node, version)` 实际是按版本恢复历史快照。** 实现取的是 `space.snapshots[version]`
   （`src/hyprial/daemon/impl/orgfs/document/operations/tree.py:332`）。而目标形态的 trash 只保存被删节点的最后状态（10-29 决定 2，Q5）。
   因此凡是用 `restore` 的 facade 测试都要在 B.6 定下语义后改写，包括 `storage/tree/test_orgfs_tree.py` 与
   `document/test_scoped_rollback.py` 的 `restore` 分支、`addressing/test_orgfs_uri.py` 的 trash/restore、
   `mesh/test_orgfs_p1_integration.py` 的 restore-to-empty。

其他值得注意的：
- 多节点测试大多用**真 Zenoh 走 TCP loopback**，不是假传输。进程内假 session 约 10 个，都只实现
  `transport/api.py:32-73` 的 session 协议，与 journal 帧无关，可以复用。耦合在 `KeySpace.orgfs_log/orgfs_snapshot` 等键
  （`transport/keys.py:186-231`），以及 `_receive`/`_deliver_all`/`_exchange_since` 这些直接读 `commits` 表搬运的辅助函数（§5.2）。
- `tests/integration/actor_preservation/orgfs/` 是 `tests/daemon/orgfs` 若干文件的近似副本（§4.8）。C4 只需参数化其中一份。
- 发现一个此前未列出的用户可见行为：`read_text_snapshot().content_revision` 跨重开保持、内容改回原值后仍会变化（ABA）。
  它只有 `tests/daemon/org_membership/migration/test_revision.py` 一条测试，而且要直接构造 `FileSpaceStore`。

## 1. 分类口径

| 分类 | 判据 |
| --- | --- |
| 契约-保留 | 只经 facade、runtime、daemon 请求或 CLI，断言用户可见结果。新实现原样通过即可（可能只需换构造入口）。 |
| 契约-需改写 | 被测行为要保留，但搭建或断言碰了 journal 内部（commits 行、envelope、writer/seq、frontier、`unbroadcast()`、client id 登记、私有字段）。依据栏写明碰了什么、怎么改。 |
| journal专属-删除 | 被测对象在目标形态中不存在。依据栏写对应的计划 §3 条目或 §1 决定（D0、Q2-Q7、R1）。“§2 差别 1/2/3”指计划 §2 列出的三处根本差别。 |
| 待定 | 取决于阶段 B 尚未定下的设计。依据栏写卡在哪个问题上。 |

工作包取值：C1 state store / C2 成员与准入 / C3 sync / facade 通用 / blob / checkout / webserver / 本地空间清除 / import-export。
另用两个标注：“（消费方）”表示这是其他域把 OrgFS 当后端的测试；“C5”表示随 C5 调用方迁移一起改。

## 2. 汇总表

格式为“文件数 / 测试条数 / 行数”。一个文件若跨类，会在每个类里各计一次文件数，行数按条数比例分摊（估算）。0 条的支持文件，行数整份计入所属类。

### 2.1 `tests/daemon/orgfs/`（按一级子目录）

| 目录 | 契约-保留 文件/条/行 | 契约-需改写 文件/条/行 | journal专属-删除 文件/条/行 | 待定 文件/条/行 |
| --- | --- | --- | --- | --- |
| `addressing/` | 2 / 35 / 394 | — | 1 / 1 / 37 | — |
| `authority/` | 2 / 10 / 390 | 7 / 24 / 1144 | 7 / 30 / 1642 | 1 / 3 / 142 |
| `blobs/` | 2 / 9 / 187 | 1 / 5 / 196 | 2 / 5 / 248 | — |
| `commands/` | 1 / 15 / 384 | — | — | — |
| `contracts/` | 3 / 31 / 646 | 2 / 4 / 97 | 1 / 12 / 384 | — |
| `document/` | — | 2 / 44 / 450 | 3 / 10 / 149 | — |
| `hydration/` | 1 / 5 / 246 | 6 / 9 / 376 | 4 / 7 / 310 | 1 / 1 / 49 |
| `instrumentation/` | — | — | — | 1 / 2 / 158 |
| `join/` | — | 1 / 8 / 163 | — | — |
| `lifecycle/` | — | 2 / 20 / 558 | 1 / 11 / 221 | — |
| `mesh/` | 5 / 10 / 393 | 13 / 76 / 4620 | 9 / 38 / 2244 | 3 / 4 / 299 |
| `presence/` | 1 / 2 / 87 | 4 / 34 / 1709 | — | 2 / 8 / 274 |
| `purge/` | — | — | 4 / 35 / 1830 | — |
| `replication/` | — | 1 / 1 / 62 | 6 / 49 / 1692 | — |
| `server/` | 4 / 51 / 1611 | — | — | — |
| `storage/` | 2 / 8 / 137 | 7 / 20 / 922 | 21 / 112 / 4814 | 1 / 1 / 16 |
| `（顶层）` | — | 5 / 35 / 1442 | 1 / 2 / 152 | — |
| **合计** | **23 / 176 / 4474** | **51 / 280 / 11738** | **60 / 312 / 13725** | **9 / 19 / 938** |

### 2.2 orgfs 之外（按目录）

| 目录 | 契约-保留 文件/条/行 | 契约-需改写 文件/条/行 | journal专属-删除 文件/条/行 | 待定 文件/条/行 |
| --- | --- | --- | --- | --- |
| `tests/daemon/acceptance/` | 1 / 2 / 166 | — | — | — |
| `tests/daemon/attach/` | 1 / 3 / 116 | — | — | — |
| `tests/daemon/cells/` | 7 / 41 / 1718 | — | — | — |
| `tests/daemon/ipc/` | 6 / 179 / 2422 | 1 / 33 / 286 | 1 / 2 / 17 | — |
| `tests/daemon/org_membership/` | 17 / 142 / 3425 | 4 / 4 / 214 | 3 / 27 / 387 | — |
| `tests/daemon/organizations/` | 1 / 15 / 562 | 1 / 2 / 75 | — | — |
| `tests/daemon/path_authz/` | 1 / 8 / 205 | — | — | — |
| `tests/daemon/provider_keys/` | 1 / 69 / 300 | 1 / 2 / 9 | — | — |
| `tests/integration/` | 6 / 32 / 970 | 5 / 13 / 1068 | 1 / 1 / 39 | 1 / 2 / 212 |
| `tests/scripts/` | — | — | 1 / 9 / 295 | — |
| `tests/shell/` | 3 / 9 / 294 | — | — | — |
| `tests/support/` | 2 / 0 / 513 | — | — | — |
| `tests/tooling/` | 1 / 4 / 33 | — | — | — |
| **合计** | **47 / 504 / 10725** | **12 / 54 / 1651** | **6 / 39 / 738** | **1 / 2 / 212** |

## 3. 逐文件清单：`tests/daemon/orgfs/`

路径相对 `tests/daemon/orgfs/`；“条”是该行覆盖的 pytest id 数；“全部”指整个文件；“其余”指该文件中不属于同文件其他行的用例。

### 3.1 `addressing/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `addressing/test_orgfs_uri_grammar.py`（133） | 全部 | 28 | 纯函数（`kernel.impl.primitives.uri`、`orgfs_web_url`） | URI 语法、web host/URL 派生（用户可见） | 契约-保留 | facade 通用 / webserver | 不触及存储（:30-132） |
| `addressing/test_orgfs_uri.py`（298） | 其余 | 6 | daemon 请求（`DaemonApplication.handle("orgfs.*")`，:24-41） | URI 往返、`cross-space-uri`/`invalid-uri`/`unknown-space`/`unknown-doc` 错误码、move/trash/restore 后 URI 稳定（用户可见） | 契约-保留 | facade 通用 | 只经请求入口，断言线上字段与错误码（:58-117、:187-214） |
| `addressing/test_orgfs_uri.py`（298） | `test_history_and_stat_at_carry_the_uri` | 1 | daemon 请求 | `orgfs.history`/`orgfs.stat_at` 返回带 URI（已决定删除的能力） | journal专属-删除 | — | §3 第 1、3 行（A1 `stat_at`、A3 `history`）；:219-242 |
| `addressing/test_orgfs_uri.py`（298） | `test_orgfs_error_code_registry_covers_every_raised_code` | 1 | 源码扫描（:261-298） | 所有被 raise 的 orgfs 错误码都登记在 `ORGFS_ERROR_CODES`（用户可见） | 契约-保留 | facade 通用 | 静态门；新包的错误码同样需要登记。需把扫描范围扩到新包目录 |

### 3.2 `authority/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `authority/admission/drain/test_drain.py`（727） | `test_idle_runtime_tick_expires_pending_without_an_import` | 1 | runtime + 直接写 `pending` 表（:100-143） | 空闲 tick 也会让过期 pending 失效并报告 org/space/count/最老时间（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 10-29“pending 保留并收窄（有 TTL）”；行写入与事件字段 `reason=age-after-revalidation` 随新 pending 结构改 |
| `authority/admission/drain/test_drain.py`（727） | 其余（含 `test_undecodable_envelopes_are_rejected_not_pending` 3 参数） | 12 | store（`authority/admission/support.py:_store`）+ `_db` pending/commits | meta 确认等待 writer 登记 envelope、`pending-meta` 在 meta 到达后重试、本地 meta 提交排水、批量登记投递、updateBlob 缺失时 pending 保留、pending 删除失败延后（实现细节） | journal专属-删除 | — | §3 writer 登记 + Q2（`pending-meta`/`pending-dependency` 来自 meta frontier 与 writer 次序）+ envelope 解码 |
| `authority/admission/membership/test_membership.py`（524） | `test_removed_and_read_only_authors_are_rejected_and_meta_exception_is_narrow`、`test_acl_space_id_meta_is_owner_only_on_every_replica`、`test_read_only_member_cannot_overwrite_own_membership_on_nonempty_replica`、`test_reinvite_allows_new_writer_again` | 4 | store `commit`/`import_envelope`（:176-311） | ro/被移除作者写被拒；`aclSpaceId` meta 只 owner 可写；ro 成员不能改自己的成员行；重新邀请后可写（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 这四条正是“当前成员表 + 写入时校验”的规则；全部在 store 层，需改成新 membership 模块的本地写/远端写入口 |
| `authority/admission/membership/test_membership.py`（524） | 其余（`test_admission_is_pinned_to_meta_frontier_not_receiver_current_state`、冷接收方重建 frontier、首个 meta 才能空 frontier、duplicate 先于重判、full frontier 投影比较、frontier 缓存 ×2） | 7 | store + `meta_frontiers` 表 | 准入钉在 envelope 携带的 meta frontier 上，而非接收方当前状态（实现细节） | journal专属-删除 | — | Q2 明确改为“按当前成员表”，第一条断言的恰好是被否决的语义（:20-115） |
| `authority/admission/membership/test_removal_wins.py`（435） | `test_removed_member_post_removal_write_is_refused_whoever_supplies`、`test_content_grid_for_removed_author_is_refused_with_zero_owner_change`、`test_update_existing_doc_path_refused_for_removed_author`、`test_merged_write_survives_later_removal_as_duplicate` | 4 | store×2-3 + `_sync`/`_reseat` 搬运 envelope（:83-120） | 被移除成员的写无论谁转发都被拒且 owner 状态零变化；已合并的写在之后移除时保留（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 与 Q2 一致的部分；断言 `_commit_count`/`_pending_count` 读 SQLite，需改为读新 state |
| `authority/admission/membership/test_removal_wins.py`（435） | `test_forged_pre_removal_frontier_is_refused_future_frontier_stays_pending`、`test_readd_epoch_restores_only_frontiers_covering_the_readd`、`test_relay_restamped_with_owner_post_removal_frontier_is_refused` | 3 | store + 改写 envelope 的 `metaFrontier` | frontier 伪造、重新加入 epoch（实现细节） | journal专属-删除 | — | Q2（不判断写入是否产生于移除之前）+ §2 差别 3 |
| `authority/admission/support.py`（138） | 0（支持代码） | 0 | `_store()` 构造 `LocalSpaceStore`（:21-52）并给 commit 包上物理 tree 映射；`_envelopes()` 读 `commits`（:79-86） | —（实现细节） | journal专属-删除 | — | 只服务 store 层准入测试；其中被标为 C2 需改写的用例改写后不再需要它 |
| `authority/admission/validation/test_validation.py`（154） | `test_envelope_pinned_after_remove_is_rejected_without_journal_write` | 1 | store + 改 envelope `origin.author`（:15-54） | 冒充已移除成员作者的写被拒 `removed-author`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | R2：新形态下作者字段由谁声明、如何校验是 B.3 的开放问题；这条是 B.3 的直接验收用例 |
| `authority/admission/validation/test_validation.py`（154） | `test_fresh_key_payload_tamper_is_rejected_without_partial_apply` | 1 | store + 非 Yjs 的 update 字节 | 坏 update 被拒且文档无部分应用（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | “非法 update 原子拒绝”保留；writer/seq 篡改部分删 |
| `authority/admission/validation/test_validation.py`（154） | `test_writer_row_content_must_match_origin_and_actual_client_id` | 1 | store + 伪造 `writers` 行 | writer 登记内容与 client id 一致（实现细节） | journal专属-删除 | — | §2 差别 2：无 client id 登记/`peerIds` |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_watchers_run_after_space_authority_lock_is_released`、`test_committed_watcher_effects_keep_space_commit_order`、`test_effect_lane_overload_rejects_mutation_before_commit` | 3 | facade（`LocalOrgFs()`；第三条 monkeypatch `_FACADE_EFFECT_CAPACITY`） | watch 回调在锁外执行、按提交顺序送达；效果通道满时拒绝写且不落地（用户可见） | 契约-保留 | facade 通用 | :25-128；只读 `facade._lock` 判锁是否空闲 |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_membership_meta_watch_runs_after_commit_and_can_be_closed` | 1 | facade `watch_meta` | 成员变更回调（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | A4 把 `watch_meta` 收为内部接口，测试需随之改到内部入口（:45-63） |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_one_blob_authority_serializes_cross_space_reference_lifecycle`、`test_blob_close_waits_for_accepted_cross_space_effect`、`test_blob_authority_does_not_repeat_an_effect_after_actor_restart` | 3 | `BlobAuthority` 直接 | blob 引用生命周期串行、关闭等待、actor 重启不重复副作用（用户可见） | 契约-保留 | blob | blob 存储与 `blob_authority.py` 保留（:131-232） |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_checkout_io_is_per_space_and_close_drains_accepted_reconcile`、`test_checkout_watch_burst_coalesces_while_reconcile_is_blocked`、`test_checkout_request_admitted_during_followup_scan_gets_its_own_scan` | 3 | runtime + checkout 私有 owner/cell 打桩 | 检出 IO 按空间隔离、关闭排水、watch 突发合并（用户可见） | 契约-保留 | checkout | checkout 模块保留；打桩点在 checkout 自身（:235-392） |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_space_authority_restarts_without_repeating_an_admitted_commit`、`test_slow_space_commit_does_not_hold_other_space_facade_lock`、`test_runtime_lazy_store_registry_is_singleton_under_cross_space_admission`、`test_runtime_close_retries_after_a_partial_drain_timeout` | 4 | runtime + `OrgSpaceAuthority` 打桩 `commit`、`runtime.stores` | 每空间串行执行者：actor 重启不重复已接纳的提交；慢空间不阻塞别的空间；store 注册单例；关闭可重试（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | “每个 space 一个串行执行者”在目标形态保留（计划 §2）；接缝是 `storage/space_authority.py` 与 `runtime.stores` |
| `authority/test_orgfs_actor_boundary.py`（637） | `test_mesh_creation_custody_coalesces_same_space_but_not_other_spaces`、`test_directory_effect_overload_defers_without_blocking_completion`、`test_broadcast_runs_after_space_authority_lock_is_released` | 3 | runtime / facade + 假 `Mesh.broadcast_pending` | mesh 创建合并、目录效果过载延后、广播在锁外（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 行为保留，打桩点是旧 mesh/目录组件 |
| `authority/test_orgfs_authority_corrections.py`（366） | `test_space_total_custody_bounds_deferred_and_rebases_restart`、`test_facade_total_custody_bounds_deferred_and_rebases_restart` | 2 | `OrgSpaceAuthority`（假 store `commit_with_outbox`）/ facade 私有 owner | 串行执行者总量有界 `resource-exhausted`、重启重定基（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store / facade 通用 | 有界保留；`commit_with_outbox` 是 journal outbox 接口（:58-146） |
| `authority/test_orgfs_authority_corrections.py`（366） | `test_replica_total_custody_bounds_deferred_and_rebases_restart`、`test_concurrent_serve_constructs_one_replica_per_space`、`test_purge_and_writer_projection_use_the_space_authority`、`test_purge_plan_authority_preserves_json_container_shapes` | 4 | `ReplicaAuthority` / runtime `serve`/`purge`/`writer_attributions` | replica 与内容清除、writer 投影经空间权威（实现细节） | journal专属-删除 | — | Q3 + Q4 + §3 `writer_attributions`（C5） |
| `authority/test_orgfs_authority_corrections.py`（366） | `test_public_checkout_uses_lifecycle_command` | 1 | runtime + 观察 `_CheckoutSpace` 命令 | `runtime.checkout` 走生命周期命令（用户可见） | 契约-保留 | checkout | :328-341 |
| `authority/test_orgfs_ingress_custody.py`（283） | `test_directory_bounds_total_custody_and_drains_deferred_effect`、`test_directory_mailbox_preserves_the_reserved_overflow_envelope`、`test_full_directory_mailbox_retains_rebuild_catchup` | 3 | runtime 私有 `_directory_deferred`、`directory_ingress_*`、`_pending_announces` | 目录入站邮箱有界、溢出时保留重建补拉（机制细节，去留未定） | 待定 | — | `composition/directory.py`（announce/目录 ingress）在 B.4 之前去留未定（计划 R3 称未读） |
| `authority/test_orgfs_ingress_custody.py`（283） | `test_wrapped_transport_uses_bounded_mesh_ingress_and_close_keeps_custody`、`test_mesh_close_budget_retains_active_sync_and_retry_does_not_repeat_it`、`test_blob_queue_limit_is_total_even_for_existing_digests` | 3 | `OrgFsMesh` + `TransportSessionAuthority` + 假 `Native` | mesh 入站有界、关闭预算保留进行中的 sync、blob 队列总量上限（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 有界入站保留；全是旧 mesh 私有计数 |
| `authority/test_orgfs_optional_store_boundary.py`（53） | 全部 | 3 | facade + `CommitOnlyStore`（:8-25） | `purge_plan`/`purge`/`replacement` 需要持久权威（已决定删除的能力） | journal专属-删除 | — | Q4 + §3 retirement |

### 3.3 `blobs/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `blobs/test_orgfs_blob_chunks.py`（120） | 全部 | 6 | 直接调 `storage.blobs` 编解码 | 分块往返、篡改拒绝、范围错误（用户可见） | 契约-保留 | blob | blob 存储保留（计划 §2“保留不动”）；:24-119 |
| `blobs/test_orgfs_blob_fetch_replier.py`（196） | 全部 | 5 | mesh 内部（`OrgFsMesh` + `_LatencyBus` 假 session，:29-90） | 慢持有方胜过快速“没有”；无人持有 → `blob-unavailable`；`purged` 是决定性错误；日志字段（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 行为是用户可见的错误码，但经 `OrgFsMesh(...)` 构造与 `network.protocol._error`、`storage.store.StoreError`（:19-22）。改成新 sync 的 blob 拉取入口；`purged` 决定性错误随 Q4 封禁删除（:141-157 那条删） |
| `blobs/test_orgfs_blobs.py`（89） | 其余 | 3 | 直接操作 `BlobStore`（含 `blob_refs` 表计数 :40） | 内容寻址、按 space 引用、篡改拒绝、`unknown-blob` 线上码（用户可见） | 契约-保留 | blob | blob 存储整体保留，`blob_refs` 是 blob 模块自己的表 |
| `blobs/test_orgfs_blobs.py`（89） | `test_purge_checker_is_space_specific_and_does_not_delete_bytes` | 1 | `BlobStore` purge checker | 被封禁摘要按 space 拒绝读取（`BlobPurged`）（已决定删除的能力） | journal专属-删除 | — | Q4：不做封禁（:79-89） |
| `blobs/test_orgfs_resident_blobs.py`（226） | 全部 | 4 | runtime×2 + 真 Zenoh loopback（:58-78） | `serve` 后 resident replica 持有/pin blob（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `fs space serve` 与 `storage/replica*`；断言 `replica.pinned_blobs()`、`backend_get`（:144-219） |

### 3.4 `commands/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `commands/test_orgfs_checkout.py`（384） | 全部 | 15 | facade（`LocalOrgFs(blobs=...)` + `CheckoutManager`，:60-66）+ `ChangeEvent` | 检出文件字节、只读权限、增量应用、名字冲突、越界防护、blob 不可用记状态（用户可见） | 契约-保留 | checkout | 只经 facade 与 checkout 公共类，断言磁盘上的检出结果（:80-328） |

### 3.5 `contracts/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `contracts/test_orgfs_acl_gate.py`（382） | 全部 | 20 | daemon 请求（`application.handle`，:57-84） | ACL space 写入授权、grant/revoke、错误码 `ORG_ACL_WRITE_DENIED` 等（用户可见） | 契约-保留 | facade 通用（org ACL 门） | 只断言请求结果与错误码（:144-378）；`test_member_replica...`（:276）的 “replica” 指成员节点上的 `OrgFsRuntime`，不是 resident replica |
| `contracts/test_orgfs_api.py`（311） | `test_members_status_blobs_watch_and_store_commit` | 1 | facade（`LocalOrgFs(FakeStore, FakeBlobs)`，:32-55、:88-112） | invite/members/watch/blob 读写 + `open_doc` + `status.unconfirmed_commits`/`durable_holders_seen` + `store.commits` 中有 `tree-` 行（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用 / C1 | 前半是用户可见行为；后半摸 `open_doc`（Q6）、`unconfirmed_commits`（Q3）和假 store 的 commit 行。改写：删掉 :105-111，状态只断言粗粒度“是否已同步” |
| `contracts/test_orgfs_api.py`（311） | `test_failed_durable_commit_rolls_back_facade_state` | 1 | facade + `FakeStore.fail`（:210-220） | 持久化失败时 facade 状态回滚、`unknown-doc`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | 行为保留；失败注入点是旧 store 的 `commit`/`commit_many` 签名。改成对新 state store 注入写失败（需要 C1 暴露同名或等价的失败注入接缝） |
| `contracts/test_orgfs_api.py`（311） | `test_facade_serializes_inbound_apply_without_swapping_local_identity` | 1 | facade 内部（`fs._finish`、`fs.apply_envelope`、手搓 envelope JSON，:223-283） | 入站应用与本地写串行化、本地 author/actor 不被替换；最后用 `fs.history` 看作者（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / facade 通用 | 串行化行为保留；envelope 与 `history` 不存在（§2 差别 1、Q5）。改成经新 sync 入站接口投递 Yjs update，作者用文档内作者字段（Q1）断言 |
| `contracts/test_orgfs_api.py`（311） | 其余（watch 签名、递归 listdir、非 owner 不能改成员/join 需持有方、元数据不可变、4 MiB 上限、re-invite 保留 added_at） | 10 | facade（`LocalOrgFs(author=...)`，部分用 `FakeJoinMesh` 改 `_spaces`） | 签名、错误码 `not-owner`/`no-holder-online`/`too-large`、成员 added_at（用户可见） | 契约-保留 | facade 通用 / C2 | 只经 facade；`FakeJoinMesh.join` 直接写 `joiner._spaces`（:57-67），新实现若改内部字段名需同步调整这一假件 |
| `contracts/test_orgfs_structured.py`（384） | 全部 | 12 | `StructuredOrgDoc`/`open_doc` + 自制 `_Replica`（envelope） | 结构化文档的 map/list/text 合并、transact 回滚（已决定删除的能力） | journal专属-删除 | — | Q6 / 计划 A2 删除 `open_doc`（:14、:224-326） |
| `contracts/test_orgfs_surface_rework.py`（50） | `test_daemon_wraps_serve_and_checkout_results_as_orgfs_json` | 1 | daemon 请求（`_handle_orgfs` + `SimpleNamespace` runtime） | `orgfs.serve`/`orgfs.checkout` 结果包装（用户可见；搭建/部分断言摸实现） | 契约-需改写 | checkout | checkout 部分保留；`orgfs.serve` 随 Q3 删除（:9-32） |
| `contracts/test_orgfs_surface_rework.py`（50） | `test_daemon_routes_local_space_purge_without_content_plan_semantics` | 1 | daemon 请求（假 runtime） | `orgfs.space_purge` 路由（用户可见） | 契约-保留 | 本地空间清除 | :35-50 |

### 3.6 `document/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `document/test_post_commit_async.py`（357） | `test_write_ipc_releases_capacity_while_real_mesh_broadcast_is_black_holed` | 1 | daemon 请求 + runtime + `_ControlledSession`（:30-99） | 网络黑洞时写请求 1.5s 内返回、锁已释放；另断言 `stores[...].unbroadcast()` 非空（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | “写不被网络阻塞”是用户可见行为；`unbroadcast()` 是 journal outbox（:173）。改成断言新 sync 有待发送状态或只断言延迟 |
| `document/test_post_commit_async.py`（357） | `test_restart_binding_drains_the_durable_commit_journal` | 1 | runtime 重启（:183-218） | 重启后 unbroadcast 的提交各发布恰好一次（`_publication_records`）（实现细节） | journal专属-删除 | — | §2 差别 1：没有逐提交 outbox/回放；“重启后未同步的本地写最终到达对端”需由 C3 的多节点测试另行覆盖 |
| `document/test_post_commit_async.py`（357） | `test_a_burst_with_a_black_holed_mesh_keeps_the_network_queue_bounded` | 1 | runtime + facade 私有字段（`_network_active_spaces`、lane snapshot，:224-287） | 网络效果队列有界（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 有界背压要保留；断言全是私有字段与 `_FACADE_NETWORK_EFFECT_CAPACITY`，需换成新 sync 的公开计数 |
| `document/test_post_commit_async.py`（357） | `test_forged_protected_alias_rejects_while_broadcast_is_black_holed`（2 参数） | 2 | runtime + 伪造 tree envelope（借 `round4._receive`、`round6._tree_envelope`，:294-352） | 伪造的受保护别名被拒 `not-a-member`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 准入规则保留（B.3）；伪造手段是 envelope，需改成伪造 Yjs update 走新入站准入 |
| `document/test_purge_rollback.py`（65） | 全部 | 6 | runtime（`OrgFsRuntime`）+ monkeypatch commit | 内容清除 plan/acknowledge/unban 失败回滚（已决定删除的能力） | journal专属-删除 | — | Q4 删除内容清除（:13-64） |
| `document/test_scoped_rollback.py`（177） | 40 个参数组合（除 `kind=structured`）：`test_exports_only_participants`、`test_failed_commit_restores_exact_state`、`test_content_commit_failure_preserves_live_document_handle`、`test_batch_callback_failure_restores_only_touched_content`、`test_failed_space_creation_removes_provisional_space` | 40 | facade（`LocalOrgFs(stores=Store())`，失败注入 `commit`/`commit_many`，:11-41） | 任一写操作持久化失败后 facade 状态与 `changes_since` 精确回滚；回滚只导出参与文档（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | 行为保留；接缝是旧 store 的 `commit(doc_id, mutate, author, actor)` 协议，且 `state(space)`（:111）读 `space.contents` 私有结构。`restore` 参数用 `version`（:56），需按 B.6 trash 语义确认 |
| `document/test_scoped_rollback.py`（177） | `kind=structured` 的 3 个参数组合 | 3 | facade `open_doc().transact` | 结构化写失败回滚（已决定删除的能力） | journal专属-删除 | — | Q6（:52） |

### 3.7 `hydration/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `hydration/test_orgfs_await_content_retry.py`（70） | 全部 | 2 | runtime + 打桩 `mesh.request_blob`（:20-44） | `await_content` 重试直到 blob 到达、到期放弃不空转（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob（Q7） | 行为保留（Q7 内容按需）；桩打在旧 mesh 方法上 |
| `hydration/test_orgfs_d4_actor_boundary.py`（221） | `test_hydration_and_structured_commit_enter_space_owner`、`test_structured_detached_mutation_cannot_overwrite_a_newer_owner_write`、`test_structured_detached_transaction_preserves_remote_crdt_merge` | 3 | facade `open_doc` | structured 提交进入 space 串行执行者、`stale-write`（实现细节） | journal专属-删除 | — | Q6；`stale-write` 也来自双实例（§2 差别 2） |
| `hydration/test_orgfs_d4_actor_boundary.py`（221） | `test_read_fetch_coalesces_bounds_and_retains_close_custody` | 1 | mesh 内部（`OrgFsMesh` 私有 `_blob_fetch_pending` 等，:93-135） | blob 拉取合并、有界（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 有界合并保留；全断言私有字段。与 `test_orgfs_d4_fetch_bounds.py` 重复 |
| `hydration/test_orgfs_d4_actor_boundary.py`（221） | `test_hydration_rejects_a_node_rebound_to_another_document` | 1 | runtime + `TreeDocument` 直接改 | `snapshot-barrier` + `replacementDocId`（实现细节） | journal专属-删除 | — | §3 retirement/snapshot barrier（:188-221） |
| `hydration/test_orgfs_d4_d1_seam.py`（151） | `test_creator_reads_after_peer_edit_two_writers`、`test_creator_and_editor_read_after_third_writer` | 2 | runtime×3 + 手动搬运 envelope（`_db.execute` 读 commits，:31-55） | 多写者后各节点读到一致文本（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛结果是用户可见的；搬运方式是 journal 行。改为经新 sync 交换 |
| `hydration/test_orgfs_d4_d1_seam.py`（151） | `test_committed_update_refuses_a_retired_document` | 1 | store | `snapshot-barrier`（实现细节） | journal专属-删除 | — | §3 retirement（:130-149） |
| `hydration/test_orgfs_d4_fetch_bounds.py`（78） | 全部 | 2 | mesh 内部（私有 `_blob_fetch_*`） | blob 拉取队列有界、丢弃计数（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 背压行为保留；全部是私有字段断言 |
| `hydration/test_orgfs_d4_hydration_fence.py`（68） | `test_hydration_rejects_a_node_rebound_to_another_document` | 1 | runtime | `snapshot-barrier`（实现细节） | journal专属-删除 | — | §3（:15-48） |
| `hydration/test_orgfs_d4_hydration_fence.py`（68） | `test_hydration_into_the_captured_document_still_merges` | 1 | facade（`export_content_update`/`hydrate_content_snapshot`） | 水合后读到内容（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync（Q7） | `hydrate_content_snapshot`/`export_content_update` 是 facade 上的同步接缝而非 Protocol 方法；新 sync 的按需内容入口确定后改写 |
| `hydration/test_orgfs_d4_verified_content.py`（393） | `test_blob_pending_reports_expected_size_and_typed_daemon_error`、`test_blob_wait_actively_fetches_when_bytes_arrive`、`test_corrupt_supplier_and_local_copy_are_never_returned` | 3 | daemon 请求 + runtime×2 + 真 Zenoh loopback（:46-88）；停/启 blob 处理器打桩 | `ORGFS_CONTENT_PENDING` 及 `expectedSize`/`expectedSha256`/`localState`/`suggestedHolders`；等待后拉到；损坏副本不返回（用户可见） | 契约-保留 | blob / C3 sync（Q7） | 断言请求层错误码与数据（:90-213）；`_stop_blob_handler` 摸 mesh 内部（:78-88）但不影响被测断言 |
| `hydration/test_orgfs_d4_verified_content.py`（393） | `test_text_required_frontier_blocks_stale_read_then_converges` | 1 | store（`_db.execute` 读 commits、`import_envelope` 手动搬运，:221-268） | 缺前驱时读返回 `ORGFS_CONTENT_PENDING` + `requiredFrontier`/`localFrontier`，补齐后收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync（Q7） | “文本内容未到不给陈旧读”是用户可见行为；frontier 字段与 envelope 搬运是 journal 细节。改为经新 sync 扣留一条 update |
| `hydration/test_orgfs_d4_verified_content.py`（393） | `test_legacy_entries_are_honestly_unverifiable_or_unknown_size` | 1 | runtime + 手写旧格式 tree 条目（:271-310） | 旧条目 `content_state=unverifiable`（已决定删除的能力） | journal专属-删除 | — | D0 不兼容旧布局；旧数据走 R1 export/import |
| `hydration/test_orgfs_d4_verified_content.py`（393） | `test_daemon_orgfs_json_uses_content_state_wire_key`、`test_mcp_read_preserves_content_pending_code_and_data` | 2 | 纯序列化 / MCP 假代理 | 线上键 `contentState`；MCP 透传 `ORGFS_CONTENT_PENDING`（用户可见） | 契约-保留 | facade 通用 | :315-393 |
| `hydration/test_orgfs_d4_verified_content.py`（393） | `test_content_frontier_tree_entry_carries_reference_metadata` | 1 | runtime，读 tree 原始条目（:341-352） | tree 条目里的 `contentFrontier`/`refSize`/`refSha256`（机制细节，去留未定） | 待定 | — | `contentFrontier` 是否保留取决于 B.4 选定的“目录先到、内容按需”帧；若退回整空间同步则删除 |

### 3.8 `instrumentation/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `instrumentation/test_announce_events.py`（158） | 全部 | 2 | runtime + mesh 内部（`_DirectoryEffectBatch`、`_holder_snapshot`、`_PutSession`，:24-158） | announce 诊断开关默认关、日志失败不改变流程（机制细节，去留未定） | 待定 | — | 计划 §1/§3 未决定 announce instrumentation（评估 §3 行 29 待 owner）；且断言 `holder.promoted` 事件，holders 角色随 Q3 收窄 |

### 3.9 `join/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `join/test_supplier_presence.py`（163） | 全部 | 8 | runtime×2 + `_Bus`/`_Presence` 假总线（借 `presence/test_orgfs_rebuild_catchup.py`，:14-90）；打桩 `receive` | 供给方中途下线时 join 失败为 `not-a-member`/`supplier-not-online`，无半加入状态；重试后完整；对比 `frontier(doc_id)`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | “join 原子、无半加入”是用户可见行为；分页、`callbackHistoryComplete`、`target.frontier()`（:100-110）是 journal 分页同步细节 |

### 3.10 `lifecycle/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `lifecycle/test_orgfs_journal_crash.py`（221） | 全部 | 11 | 直接操作 `FileSpaceStore` + SQLite（:6、:95-221） | 提交边界崩溃后无重复、回放 journal；`unconfirmed_commits` 结算（实现细节） | journal专属-删除 | — | §3 journal store；§1 Q3（unconfirmed）。C1 需另写 state+delta 崩溃安全测试（B.2） |
| `lifecycle/test_orgfs_late_callback_after_close.py`（90） | 全部 | 1 | runtime×2 + 真 Zenoh；直接调 `mesh._on_applied(envelope)`（:48-85） | close 后迟到回调不重开 store（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 关停后不复活是保留行为；envelope 取自 `store.export_since`（journal） |
| `lifecycle/test_space_purge.py`（468） | 全部 | 19 | runtime（`OrgFsRuntime.purge_space`，:18-33） | 本地空间清除：成对 space 保留、幂等、权限拒绝码、路径围栏、中断续做、blob 释放（用户可见；搭建/部分断言摸实现） | 契约-需改写 | 本地空间清除 | 几乎全部是用户可见行为；但断言 `runtime._replicas`（:131、:224，Q3 删除）、`journal.sqlite3` 文件存在（:294、:466）。改写只需换磁盘布局文件名并删 `_replicas` 断言 |

### 3.11 `mesh/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `mesh/device_rows/test_round10.py`（295） | `test_r10_forged_origin_node_cannot_fill_honest_supplier_bucket`、`test_r10_forged_fill_only_delays_update_until_anti_entropy` | 2 | store（`_store` 来自 `authority/admission/support.py`）+ runtime，`_db.execute` 查 pending（:48-125） | 伪造来源不能占满诚实供给方的 pending 配额；最终经 anti-entropy 收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | pending 有界在 10-29 中“保留并收窄”；但 supplier bucket 按 envelope origin 计，新 sync 的 pending 结构未定 |
| `mesh/device_rows/test_round10.py`（295） | `test_r10_non_meta_empty_frontier_is_rejected_not_parked`、`test_r12_malformed_varint_frontier_is_rejected_by_mesh` | 2 | store `import_envelope` | envelope frontier 字段畸形被拒（实现细节） | journal专属-删除 | — | §2 差别 1：无 envelope/frontier（:128-210） |
| `mesh/device_rows/test_round10.py`（295） | `test_r10_replica_prime_propagates_enospc`、`test_r12_attach_replica_is_atomic_when_prime_hits_enospc` | 2 | `ReplicaStore`/`MemoryReplicaBackend` | replica prime 的 ENOSPC（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*`（:212-293） |
| `mesh/device_rows/test_round4.py`（493） | `test_r4_a_protected_parent_id_is_judged_before_parent_arrives`、`test_r4_b_protected_id_verdict_does_not_consume_pending_capacity` | 2 | runtime×4 + 真 Zenoh（fixture `replicas`，:171-219）；伪造 tree envelope 经 `_receive`→`import_envelope`，`_position`/`_commits_since` 读 `commits` 表（:75-110） | 受保护目录 id 在父节点到达前即判定 `not-a-member`；判定不占 pending 配额（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 安全规则（受保护目录行只能由 owner/编码用户写）必须保留；全部搬运与伪造经 journal。改写需要新 sync 的“入站 Yjs update”测试入口与伪造 update 的工具函数 |
| `mesh/device_rows/test_round4.py`（493） | `test_r4_c_protected_ids_round_trip_through_every_real_key_validator`（4 参数） | 4 | identity 解析 + `KeySpace.orgfs_log/orgfs_snapshot` + `ReplicaStore`（:336-405） | 受保护 doc id 通过所有 key 校验器（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | `parse_protected_directory_doc_id` 往返保留；`orgfs_log`/`orgfs_snapshot` key 与 `ReplicaStore` 随 Q3/§3 删除，改写时只留 identity 解析与新 sync 的 key 校验 |
| `mesh/device_rows/test_round4.py`（493） | `test_r4_c_put_broadcast_and_receiver_replica_store_the_row` | 1 | runtime×4 + `serve` | 设备行广播到接收方；接收方 replica backend 有该 doc 的 log key（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 设备行到达保留；`alice.serve` 与 `_replicas[...].backend_keys`（:408-440）随 Q3 删除 |
| `mesh/device_rows/test_round4.py`（493） | `test_r4_d_protected_path_purge_is_refused_without_leaking_content` | 1 | runtime，内容清除 plan | 受保护路径清除被拒 `protected-path` 且不泄露地址（已决定删除的能力） | journal专属-删除 | — | Q4 删除内容清除 plan（:441-478）；新 `rm --permanent` 是否同样要拒绝受保护路径需在 B.6 定，见“无 facade 覆盖”一节 |
| `mesh/device_rows/test_round4.py`（493） | `test_r5_long_protected_path_is_stored_not_refused` | 1 | facade（:481-494） | 长受保护路径可写可读（用户可见） | 契约-保留 | C1 state store | 只经 facade；新 state 文件也须按哈希命名 |
| `mesh/device_rows/test_round6.py`（573） | `test_r6_protected_doc_alias_link_is_rejected_on_owner_and_receivers`、`test_r6_receiver_rejects_invalid_protected_ids_without_persisting`（参数化）、`test_r6_nonmember_missing_parent_update_is_rejected_not_parked`、`test_pending_forged_alias_is_rejected_when_invite_meta_converges` | 5 | runtime×4 + 伪造 envelope；`_pending_count`/`_meta_commits` 读 SQLite（:96-112）；最后一条 monkeypatch `store.admission`（:537-547） | 别名链接受保护 doc 被拒；非法受保护 id 不落盘；非成员缺父更新被拒不暂存（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 规则保留；最后一条断言 `pending-meta → not-a-member` 的两段判定（:549-555），其中 `pending-meta` 是 meta frontier 机制（Q2 后不存在），改写时只保留最终 `not-a-member` |
| `mesh/device_rows/test_round6.py`（573） | `test_r6_purge_refuses_a_protected_doc_reached_through_an_alias` | 1 | runtime，内容清除 | 经别名清除受保护 doc 被拒（已决定删除的能力） | journal专属-删除 | — | Q4（:179-215） |
| `mesh/device_rows/test_round6.py`（573） | `test_r6_replica_prime_skips_one_bad_document_and_keeps_the_rest` | 1 | `ReplicaStore` prime | replica prime 跳过坏文档（已决定删除的能力） | journal专属-删除 | — | Q3（:256-302） |
| `mesh/device_rows/test_round6.py`（573） | `test_r6_pending_supplier_cap_preserves_budget_for_another_supplier`、`test_r6_pending_drop_warnings_are_rate_limited_with_a_count` | 2 | store pending 表 | pending 按供给方配额、丢弃日志限频（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | pending 有界保留（10-29 “keep but narrow”）；计数读 SQLite、details `{"dropped":True,"reason":"capacity"}` |
| `mesh/device_rows/test_round6.py`（573） | `test_r6_pending_drain_pages_more_rows_than_one_page` | 1 | store + `_PagingConnection` 包 SQLite 连接（:379-444） | pending 排水按 SQL 分页（实现细节） | journal专属-删除 | — | 断言 SQL `LIMIT ?` 分页，§3 journal store |
| `mesh/device_rows/test_round7.py`（316） | `test_r7_every_protected_tree_node_uses_its_deterministic_id` | 1 | facade（多节点 stat） | 受保护 tree 节点使用确定性 node id（用户可见） | 契约-保留 | C2 成员与准入 | 经 facade `stat` 断言（:163-197）；节点间搬运借 round4 fixture（journal），但断言本身只看 facade |
| `mesh/device_rows/test_round7.py`（316） | `test_r7_create_under_protected_parent_requires_deterministic_child_id_and_author`、`test_r7_delete_of_protected_id_is_owner_or_encoded_user_only`、`test_r7_959_repro_converges_in_both_orders_on_two_replicas`（参数化） | 6 | runtime + 伪造 tree envelope（`_tree_envelope` :40-72 直接写 `store._db`） | 受保护父下创建需确定性 id 与作者；删除仅 owner/编码用户；#959 复现双序收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 规则保留；伪造与收尾断言读 `commits`/`pending` 表（:305-311） |
| `mesh/device_rows/test_round7.py`（316） | `test_r7_moves_are_refused_when_any_operation_id_is_protected` | 1 | facade `move` + `_db` pending 计数（:233-252） | 移入受保护路径 `not-a-member`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | facade 断言保留；删去 :249-251 的 pending 表计数 |
| `mesh/device_rows/test_round8.py`（251） | `test_r8_overlong_writer_is_rejected_without_journal_or_replica_residue`、`test_r8_replica_segments_reserve_suffix_margin`、`test_r8_replica_prime_skips_enametoolong_and_continues` | 3 | store/replica | writer 段长度、replica key 段（实现细节） | journal专属-删除 | — | §3 writer 登记 + Q3 replica（:69-163） |
| `mesh/device_rows/test_round8.py`（251） | `test_r8_pending_meta_cap_is_per_supplier_not_mintable_author`、`test_r8_uncovered_pending_meta_rows_are_not_redecoded` | 2 | store pending-meta | `pending-meta` 配额与不重复解码（实现细节） | journal专属-删除 | — | `pending-meta` 由 meta frontier 引起；Q2 改为按当前成员表判定后不存在（:166-251） |
| `mesh/device_rows/test_round9.py`（404） | `test_r9_member_encoded_create_is_member_only_in_every_order`（参数化）、`test_r2_other_member_cannot_link_people_binding_row_on_any_replica`、`test_r9_move_cannot_squat_the_shared_directory_root`、`test_r9_owner_cannot_delete_a_member_encoded_row`、`test_r9_non_json_tree_row_does_not_block_later_updates`、`test_r11_row_rewrite_cannot_rename_an_ordinary_node_to_directory`、`test_r11_non_object_protected_node_write_is_rejected_in_every_order`（参数化） | 13 | runtime×4（借 round4 `replicas`，:26-47）+ 伪造 envelope `_tree_envelope`/`_receive` | people/devices 受保护行的作者规则；任意到达顺序结论一致（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 规则保留，伪造与搬运经 journal |
| `mesh/device_rows/test_round9.py`（404） | `test_a_people_row_written_by_its_member_replicates_to_every_replica`、`test_r9_removed_member_rows_are_inert_for_devices_people_and_peers` | 2 | facade 写 + `_deliver_all` 搬运 commits（:148-292）+ `OrgNetworkService` | 成员自己的 people 行复制到所有节点；移除成员后其设备/people 行在各节点对读者不可见、`directory_peers()` 不含它，但物理节点未删（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 / C3 sync | 断言是用户可见行为（Q2 当前成员表过滤）；搬运借 `_position` 读 `commits`，最后一条读 `facade._space(...).nodes` 私有结构（:289-291） |
| `mesh/replication/blobs/test_blobs.py`（363） | 全部 | 6 | runtime×2 + 真 Zenoh loopback（`support.py:_large_write_with_recovery_spies`），打桩 `OrgFsMesh._fetch_blob_into_store`/`_run_blob_recovery`/`_import_envelope` | >512000 字节文本跨节点最终可读；缺 blob 只拉一次、只重导一次；失败时读到 `content-pending`；blob 队列有界（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 收敛与 `content-pending` 是用户可见的；“更新体超限转 blob + 重导 envelope”是 journal 协议机制，断言 `replica` 引用原因、`assert_client_ids_registered`（support :62）、`commits.envelope_bytes`（:148） |
| `mesh/replication/content/test_atomic_birth.py`（159） | 全部 | 3 | runtime×2 + 真 Zenoh；用 `facade.history` 观察（:33-35） | 跨节点新建文档投影为一个带内容的 `created` 版本（已决定删除的能力） | journal专属-删除 | — | Q5 删除 history；被保护的真实行为“对端不会先看到空文件再看到内容”没有其他 facade 观察点，见缺口一节 |
| `mesh/replication/content/test_content.py`（129） | `test_concurrent_text_edits_from_same_base_do_not_duplicate_base` | 1 | runtime×2 + 真 Zenoh + `runtime.broadcast_pending`（:18-92） | 同基线并发编辑合并后不重复基线（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛结果是用户可见的；`broadcast_pending` 是旧 mesh 的手动推送入口 |
| `mesh/replication/content/test_content.py`（129） | 其余（`_ContentDocument.set` 边界、大文本、无变化保字节） | 6 | `document.model._ContentDocument` 单元 | 文本 diff 写入 CRDT 的正确性（用户可见） | 契约-保留 | C1 state store | `document/model.py` 保留（计划 §2 tree/内容模型）；:94-129 |
| `mesh/replication/content/test_duplicate_projection.py`（835） | 全部 | 12 | runtime×2 + 真 Zenoh；`mesh.receive(envelope)`、`_import_envelope`、`_projection_failures`、`_latest_envelope` 读 `commits`（:42-56） | store 已应用但 facade 投影失败时的 `duplicate` 重试、退避、毒文档有界、重启重建投影（实现细节） | journal专属-删除 | — | §2 差别 2：目标形态每文档单内存实例，没有“store 应用 → facade 投影”两段，也就没有投影失败/`duplicate` 重投；断言字段 `{"docId","writer","seq","attempts"}`（:331）是 writer/seq |
| `mesh/replication/presence/test_presence.py`（592） | `test_round4_f5_buffers_announce_until_announcer_liveliness`、`test_final_f7_pending_announces_are_bounded_counted_and_still_apply` | 2 | runtime + 私有 `_holders`/`_pending_announces`、`composition.directory.ORGFS_ANNOUNCE_*` 常量（:25-218） | announce 在 liveliness 前缓存、缓存有界（机制细节，去留未定） | 待定 | — | 取决于 B.4 新 sync 是否保留 announce/liveliness 发现机制；全是私有字段断言 |
| `mesh/replication/presence/test_presence.py`（592） | `test_round4_f6_liveliness_auto_sync_recovers_dropped_startup_broadcast`、`test_round3_r4a_announces_all_held_spaces_without_evicting_omissions`、`test_round3_r4b_restart_accepts_live_peer_without_join_or_sync`、`test_join_recovers_when_create_time_announce_is_lost` | 4 | runtime×2 + 真 Zenoh + `LivelinessDirectory`（support `_bind_with_daemon_presence`）；`assert_client_ids_registered` | 启动广播丢失后靠 liveliness 自动补齐；重启后无需重新 join；建空间时 announce 丢失 join 仍能完成（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛与 join 结果是用户可见的；`assert_client_ids_registered`（:9）是 client id 登记（§2 差别 2） |
| `mesh/replication/support.py`（227） | 0（支持代码） | 0 | 两节点 runtime + 真 Zenoh、`_bind_with_daemon_presence`（:66-80）、打桩 `OrgFsMesh` 私有方法（:114-210） | —（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | `_bind_with_daemon_presence` 可复用；`assert_client_ids_registered`、`_large_write_with_recovery_spies` 绑旧协议 |
| `mesh/test_orgfs_device_row_owner.py`（808） | `test_genuine_author_content_and_tree_rules_hold_on_every_replica`、`test_org_meta_is_owner_only_on_every_replica` | 2 | daemon 请求（`_daemon(runtime)`，:76-80）+ runtime×3 真 Zenoh + 伪造 envelope（`_forged_*_envelope` 直接写 `store._db`，:122-228） | 成员只能写自己的设备行；org meta 只有 owner 能写；请求层 `not-a-member`；伪造 envelope 在每个节点被拒（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 请求层断言可保留；伪造部分与 `store.admission(envelope)`（:741、:758）需换成新入站准入接口 |
| `mesh/test_orgfs_device_row_owner.py`（808） | `test_owner_member_removal_leaves_rows_inert_on_every_replica` | 1 | daemon 请求 `ipc_org_remove` + `FakeMembershipSidecar`（:457-491） | 移除成员后其行在各节点读为 `unknown-doc`，物理节点未删（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 行为保留（Q2）；末行读 `facade._space(...).nodes` 私有结构 |
| `mesh/test_orgfs_device_row_owner.py`（808） | 其余（目录祖先不可删、devices 根不可重复、非确定性 doc id 链接被拒、双序收敛、leave/invite principal、root/祖先 owner-only ×3） | 8 | runtime×3 + 伪造 tree envelope + `store.admission(envelope)`、`_meshes[...].receive` | 受保护目录结构规则在每个节点被拒（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 规则保留；全部通过 envelope 伪造与 store 准入函数（:494-809） |
| `mesh/test_orgfs_member_purge_ack.py`（127） | 全部 | 1 | runtime×2 + monkeypatch `LocalSpaceStore._local_meta_exception_allowed`（:87-122） | 成员确认内容清除（已决定删除的能力） | journal专属-删除 | — | Q4 |
| `mesh/test_orgfs_mesh.py`（760） | `test_three_replica_reorder_duplicate_drop_and_partition_heal` | 1 | store（`LocalSpaceStore`×4）+ `OrgFsMesh` + `_Bus`（:45-86）；搬运读 `commits` | 乱序/重复/丢包/分区愈合后 meta 与 tree 收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛性质必须保留；断言 `frontier()` 相等（:145-146），改为比较 facade 读结果或新 state vector |
| `mesh/test_orgfs_mesh.py`（760） | `test_ro_supplier_replays_three_original_authors_without_rewriting_origin` | 1 | store×3 + `_db` 读 commits | 只读供给方转发时保留每条提交原作者（实现细节） | journal专属-删除 | — | Q1：不保留逐提交作者归属（:154-220） |
| `mesh/test_orgfs_mesh.py`（760） | `test_sync_pages_are_bounded_meta_first_and_converge` | 1 | store + mesh 查询处理器 | 同步分页不超过 `ORGFS_PAGE_BYTES`、meta 先行、收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 帧大小有界、成员表先于内容到达（准入前提）要保留；分页/cursor 形式随协议重写（:222-290） |
| `mesh/test_orgfs_mesh.py`（760） | `test_r3_two_node_sync_from_supplier_missing_replacement_is_bounded` | 1 | store + `retire_without_snapshot` | `snapshot-unavailable`、`pendingReplacementDocIds`（实现细节） | journal专属-删除 | — | §3 retirement/snapshot（:294-380） |
| `mesh/test_orgfs_mesh.py`（760） | `test_supplier_gate_precedes_store_admission`、`test_import_supplier_gate_refuses_non_member_device_owner`（4 参数） | 5 | store + mesh `receive`/`sync_from`，`_db` 计数 commits | 离线供给方 `supplier-offline`；供给方设备 owner 非成员时 `not-a-member`/`supplier-not-member`，不落盘（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 / C3 sync | B.3 选项“只接受来自当前成员节点的 update”正是这条规则；落盘检查改为新 state 未变化 |
| `mesh/test_orgfs_mesh.py`（760） | `test_review_fix_13_schema_gate_runs_before_supplier_gate`、`test_bad_cursor_and_handler_exception_return_wire_errors`、`test_log_range_blob_fetch_and_frontier_confirmation`、`test_review_fix_14_log_range_errors_use_concrete_record_keys` | 4 | store + mesh 查询处理器 | envelope schema 校验顺序；`bad-cursor`；log range 应答与 `unconfirmed_commits` 结算；log key 形状（实现细节） | journal专属-删除 | — | §2 差别 1（envelope、export cursor、log range）+ Q3（unconfirmed）（:415-650）。`bad-cursor` 用例里的 `not-a-member` 查询拒绝需在 C3 重写 |
| `mesh/test_orgfs_mesh.py`（760） | `test_round3_d3_live_broadcast_uses_liveliness_not_space_announce` | 1 | store + mesh + `_Bus` | 实时广播以 liveliness 判定对端而非空间 announce（机制细节，去留未定） | 待定 | — | 取决于 B.4 是否沿用 liveliness/announce 两套信号（:445-490） |
| `mesh/test_orgfs_mesh.py`（760） | `test_oversized_envelope_is_rejected_before_json_decode` | 1 | mesh `receive` | 超大入站帧在解码前 `too-large` 拒绝（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 帧上限保留；常量 `ORGFS_ENVELOPE_BYTES` 与 commits 计数需换 |
| `mesh/test_orgfs_mesh.py`（760） | `test_online_replication_refreshes_open_facade_and_watch` | 1 | runtime×2 + `_Bus`（:656-695） | 对端写入后本端 facade 读到新文本、`changes_since` 依次给出 created/modified（用户可见） | 契约-保留 | C3 sync / facade 通用 | 只经 facade/runtime；末尾 `_assert_registered`（client id 登记，:691）要删 |
| `mesh/test_orgfs_p1_integration.py`（842） | `test_revocation_offline_window_write_restore_and_reconverge`、`test_revoked_writer_rejected_across_all_three_mesh_injection_paths`、`test_restart_file_first_then_invite_preserves_member_writes_everywhere` | 3 | runtime×3 + 真 Zenoh；`sync_log_range_from`、`_latest_envelope`、`_rewrite_origin`、`_db` 计数（:74-100、:489-556、:800-817） | 被撤销成员离线期间的写入在重连后被拒、在 live/sync/range 三条注入路径都被拒；重启后先写文件再邀请，成员写入到处保留（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | Q2：按当前成员表拒绝（不再判断“移除前产生”）——第一条若断言“移除前的离线写入被保留/被拒”的具体时间语义需按 Q2 重新定期望；三条注入路径是旧协议的 live/sync/log range |
| `mesh/test_orgfs_p1_integration.py`（842） | `test_cold_cache_blob_unavailable_then_fetches_when_holder_returns` | 1 | runtime×2 + 真 Zenoh（:253-325） | 持有方离线 1s 内 `content-pending` 含 `expectedSha256`/`suggestedHolders`，回来后 `await_content` 成功（用户可见） | 契约-保留 | blob / C3 sync（Q7） | facade+runtime 断言；末尾 `_assert_client_ids_registered` 要删 |
| `mesh/test_orgfs_p1_integration.py`（842） | `test_delete_only_commit_catches_up_after_offline_live_broadcast`、`test_restore_to_empty_catches_up_after_offline_live_broadcast` | 2 | runtime×2 + 真 Zenoh；`unconfirmed_commits()`、`_meshes[...].sync_from`（:569-730） | 对端离线错过 live 广播，重启后删除/清空到空也能追上（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛保留；`unconfirmed_commits` 计数删除（Q3），改为断言 facade 读结果 |
| `mesh/test_orgfs_p2_integration.py`（382） | `test_open_doc_concurrent_container_edits_converge_over_zenoh`、`test_open_doc_transact_respects_membership_gate`、`test_open_doc_handle_across_purge_rebuild` | 3 | runtime + `open_doc` | 结构化并发合并、成员门、purge 重建 `snapshot-barrier`（已决定删除的能力） | journal专属-删除 | — | Q6 + Q4 + §3 snapshot（:63-215） |
| `mesh/test_orgfs_p2_integration.py`（382） | `test_reader_never_observes_partial_apply` | 1 | runtime + gate `_finish` + `_db`（:218-260） | 读者不会看到批量应用的中间态（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store / facade 通用 | 原子可见性保留；接缝是 facade `_finish` 与 commits 表 |
| `mesh/test_orgfs_p2_integration.py`（382） | `test_announce_landing_during_liveliness_flip_not_stranded` | 1 | runtime 私有 `_pending_announces`/`_holders` | liveliness 翻转期间 announce 不丢（机制细节，去留未定） | 待定 | — | 取决于 B.4 是否保留 announce（:263-303） |
| `mesh/test_orgfs_p2_integration.py`（382） | `test_anti_entropy_skips_non_holder_spaces`、`test_agent_liveliness_is_not_a_peer_but_host_still_schedules_sync` | 2 | runtime + 假 `Mesh`（`schedule_sync_from`） | 只对持有该空间的对端调度 anti-entropy；agent liveliness 不算对端（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 调度规则保留；假 mesh 接口随新 sync 改（:306-378） |

### 3.12 `presence/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `presence/test_orgfs_holder_catchup.py`（160） | 全部 | 3 | runtime（4 节点用例）+ monkeypatch 调度（`_scheduled_syncs`，:22-30） | 自身持有不遮蔽在线供给方；announce 后拉取新持有空间；四成员顺序 join 收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛/调度是行为；钩子在旧 mesh 方法 |
| `presence/test_orgfs_holder_settlement.py`（279） | 全部 | 2 | runtime×2 + 真 Zenoh + 私有 `_lively_peers` | liveliness 抖动后仍能拉 blob；agent liveliness 不触发 announce/sync（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 行为保留；观察点在私有回调（:55-271） |
| `presence/test_orgfs_holders_online.py`（190） | 全部 | 5 | runtime + `status().holders_online` + 私有 `_lively_peers` | `holders_online` 列出在线持有方，离线/未 announce/迟到 announce 不列入（机制细节，去留未定） | 待定 | — | Q3 只保留粗粒度“是否已同步”；`SpaceStatus.holders_online` 是否保留由阶段 B 的收窄 Protocol 定（:64-184） |
| `presence/test_orgfs_observed_liveliness_admission.py`（84） | 全部 | 3 | runtime 私有 `_holder_snapshot`/`_pending_announces`/`directory_ingress_dropped` | host put 先于共享 presence 时接纳 announce；agent token 不能授权 host announce（机制细节，去留未定） | 待定 | — | agent 不能冒充 host 是安全性质，应保留；但全部观察点是 announce 缓冲，取决于 B.4 |
| `presence/test_orgfs_presence_settlement.py`（357） | 全部 | 8 | runtime + `PresenceAuthority`/`LivelinessDirectory` + `ControlledSession`（:16-40） | presence 删除/确认后 OrgFS sync 通知与 inbox 唤醒各自正确；已提交的供给方删除拦住排队的 sync（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | presence→sync 触发保留；断言 `mesh._sync_pending`、`directory._authority._online_subscribers` 等私有字段 |
| `presence/test_orgfs_rebuild_catchup.py`（1000） | `test_transition_before_complete_recovers_dropped_broadcast`、`test_resident_space_pulls_dropped_broadcast_after_every_rebuild`、`test_one_lost_commit_parks_later_commits_until_rebuild_catchup`、`test_second_rebuild_reruns_sync_after_inflight_catchup`、`test_offline_deferred_supplier_survives_rebuild_drain`、`test_deferred_supplier_rechecks_on_later_online_transition` | 6 | runtime×2 + `_Bus`/`_Session`（带 `begin_rebuild`/`finish_rebuild`）/`_Presence` 假件（:26-124） | 传输会话重建后丢失的广播被补拉；丢一条提交时后续写在追上前暂存，追上后全部可见（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 补齐行为保留；断言 `unbroadcast()`、私有 `_deferred_supplier_online`、`_replicas`（:168、:210、:333） |
| `presence/test_orgfs_rebuild_catchup.py`（1000） | `test_directory_anti_entropy_closes_three_member_late_join_gap`（3）、`test_directory_anti_entropy_deadline_is_jittered`、`test_supplier_online_edge_applies_buffered_announce_before_sync`、`test_creator_restart_recovers_rejected_row_by_anti_entropy`、`test_four_member_sequential_join_earliest_joiner_converges_without_restart`（3）、`test_concurrent_joiners_converge_on_each_other_by_anti_entropy`（2）、`test_anti_entropy_tick_never_raises` | 12 | runtime×3-4 + 假总线 + `OrgFsDirectoryStore` + `runtime.anti_entropy_tick(clock)` | org 目录设备行在有限 tick 内跨成员收敛；被拒的行在创建者重启后靠 anti-entropy 补回；tick 带抖动、永不抛出（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | org 目录收敛是用户可见的（设备互相可拨）；`anti_entropy_tick`、`_directory_sync_due`、`_DIRECTORY_SYNC_INTERVAL_SECONDS` 是旧 runtime 的调度接口 |
| `presence/test_orgfs_rebuild_catchup.py`（1000） | `test_parked_commits_are_reported_until_the_gap_is_pulled` | 1 | runtime + `OrgFsDirectoryStore.parked_commits`（:590-622） | 目录状态报告被暂存的提交（按作者计数），补齐后清空（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync（C5 迁移 `parked_commits`） | 计划 C5 迁移 `parked_commits` 诊断；“按作者 commits 计数”的形状随 Q1 失效 |
| `presence/test_orgfs_rebuild_catchup.py`（1000） | `test_join_against_an_offline_supplier_carries_the_typed_reason`、`test_join_error_message_has_one_code_prefix` | 2 | runtime facade `join`（后者用假 `Mesh.sync_from`） | join 失败 `not-a-member` + `reason=supplier-not-online`，消息只有一个码前缀（用户可见） | 契约-保留 | C3 sync / facade 通用 | 断言 facade 错误码与消息（:826-905）；第二条的假 mesh 需换接口 |
| `presence/test_orgfs_rebuild_catchup.py`（1000） | `test_successful_sync_logs_one_summary_line`、`test_rebuild_catchup_is_deduplicated_and_logged_once` | 2 | runtime + logger 事件 | `orgfs.sync.completed` 一条汇总、重建补拉去重（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 诊断事件名是运维可见的；字段 `residentSpaces`/`onlineHolderPairs` 等（:994-1000）随新 sync 重定 |

### 3.13 `purge/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `purge/test_orgfs_purge_e2e.py`（293） | 全部 | 8 | runtime + `ReplicaStore` + `import_envelope` | 清除后 journal/replica/blob 无残留、`snapshot-barrier`、`purged`、`unban`（已决定删除的能力） | journal专属-删除 | — | Q4 + Q3 + §3 |
| `purge/test_orgfs_purge_receipt.py`（400） | 全部 | 3 | **CLI 子进程**（`_IsolatedCli` 起隔离 daemon，:55-215）+ 直读 SQLite `snapshots` 表（:325） | `fs purge` 回执与持久 retirement 记录一致；失败不报告替换；非 owner 拒绝（已决定删除的能力） | journal专属-删除 | — | Q4。注意：这是 `tests/daemon/orgfs` 中唯一走真实 CLI+daemon 子进程的文件（计划 §10 记它在无原生扩展环境失败 3 条） |
| `purge/test_orgfs_purge_twonode.py`（399） | 全部 | 1 | runtime×3 + 真 Zenoh | 跨节点清除、离线清理确认、新加入者拿不到旧内容（已决定删除的能力） | journal专属-删除 | — | Q4（不做全网历史副本清除） |
| `purge/test_orgfs_purge.py`（738） | 全部 | 23 | runtime + `FileSpaceStore`/SQLite（`retired_residue`、`meta_frontiers`、`_membership_at`、`.loro` 文件，:44-738） | 内容清除 plan/stale-plan/ack/unban/封禁/重建无残留，及 meta clock、成员 token 缓存（已决定删除的能力） | journal专属-删除 | — | Q4（内容清除）+ §3 retirement/snapshot；其中 `test_m2_stale_token_membership_cache_equals_full_rebuild`、`test_retirement_vacuum_resets_the_meta_journal_cursor` 属成员回放（Q2） |

### 3.14 `replication/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `replication/test_orgfs_replica_attach_review.py`（89） | 全部 | 1 | runtime + `ReplicaStore` prime 打桩 | re-serve prime 不阻塞（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*` 与 `fs space serve` |
| `replication/test_orgfs_replica_backend.py`（206） | 全部 | 15 | `FsReplicaBackend`/`MemoryReplicaBackend` 直接 | put 幂等/冲突、key 安全、state root（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*` 与 `fs space serve` |
| `replication/test_orgfs_replica_blob_custody.py`（57） | 全部 | 1 | `OrgFsMesh` + 假 replica_store | replica blob 对账的关停托管（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*` 与 `fs space serve` |
| `replication/test_orgfs_replica_doc_dirs.py`（90） | 全部 | 4 | `FsReplicaBackend` | 长 doc id 目录哈希化与迁移（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*` 与 `fs space serve` |
| `replication/test_orgfs_replica_mesh.py`（989） | `test_replica_down_members_still_converge` | 1 | runtime×3 + `_Bus`（:217-247、:558-576） | resident 关掉后成员之间仍收敛（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 收敛行为保留，搭建里的 `serve` 与 `assert_client_ids_registered` 删掉后即是普通两节点收敛（与 `mesh/test_orgfs_mesh.py` 的 online replication 重复） |
| `replication/test_orgfs_replica_mesh.py`（989） | 其余 | 15 | runtime + `serve`/`_replicas`/`ReplicaAuthority` + `_Bus` | resident replica 克隆、attach 窗口回填、durable 确认、`unconfirmed_commits`/`durable_holders_seen`、retention、版本向量排序（已决定删除的能力） | journal专属-删除 | — | Q3；其中 `test_last_holder_loss_reported_honestly`（:685-715）用 `facade.restore(old_version)`，也随 Q5 失效 |
| `replication/test_orgfs_replica.py`（323） | 全部 | 13 | `ReplicaStore` + `MemoryReplicaBackend` + `_View` 假 retirement | replica 存取、`snapshot-barrier`、retention、purge duty、pin（已决定删除的能力） | journal专属-删除 | — | Q3 删除 `storage/replica*` 与 `fs space serve` |

### 3.15 `server/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `server/http/request_contracts/test_daemon_ipc_seam.py`（104） | 全部 | 2 | webserver `ipc` + 内存 IPC 后端 | daemon 请求帧往返（用户可见） | 契约-保留 | webserver | :76-104 |
| `server/http/request_contracts/test_request_contracts.py`（876） | 全部 | 48 | webserver（真 TLS 服务器）+ `_FakeDaemon` 单元 socket 应答 `orgfs.members/stat/read/ls`（`support.py:86-196`）；1 条经 CLI `fs web check`（:770） | HTTP 状态码、头、成员 403、`content-pending`→503、PROXY v2 身份（用户可见） | 契约-保留 | webserver | 只依赖 daemon 请求的线上形状，不碰存储；新实现只需保持 `orgfs.members/stat/read/ls` 的应答字段 |
| `server/http/support.py`（577） | 0（支持代码） | 0 | `_FakeDaemon` 单元 socket 应答 `orgfs.*`（:132-196）、TLS 证书、PROXY v2 头 | —（用户可见） | 契约-保留 | webserver | 与 OrgFS 存储无关 |
| `server/http/test_unix_listener_seam.py`（54） | 全部 | 1 | webserver server 模块 | unix 监听基类来自接缝（用户可见） | 契约-保留 | webserver | :26-52 |

### 3.16 `storage/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `storage/fuzz/test_orgfs_fuzz.py`（390） | `test_two_replica_randomized_public_api_operations_converge`、`test_three_replica_randomized_concurrent_moves_converge_without_loss`、`test_mutual_move_uses_facade_and_real_updates_without_cycles`、`test_concurrent_text_writes_preserve_both_insertions` | 4 | facade（`LocalOrgFs(ReplicaStore 假件)`，假件把真 pycrdt 全量 update 包成 envelope JSON，:37-115）；交换经 `apply_envelope` | 随机公开 API 操作后两/三副本可见树一致、并发 move 无环不丢、并发文本两侧插入都保留（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / facade 通用 | 最有价值的收敛契约之一；只经 facade 断言（`_visible_snapshot` :194-210）。改写点只有假件：把 envelope 包装换成新 sync 的“文档 id + Yjs update”帧 |
| `storage/fuzz/test_orgfs_fuzz.py`（390） | `test_same_list_item_concurrent_delete_insert_duplicates_observed` | 1 | `open_doc(...)._document.doc` 直接改 pycrdt（:356-390） | pycrdt 列表并发删插的已知重复（已决定删除的能力） | journal专属-删除 | — | Q6 `open_doc`；本质是记录 pycrdt 行为 |
| `storage/scaling/test_orgfs_history_equivalence.py`（164） | 全部 | 1 | store `_db` 读 commits、export 分页对照 | 乱序多写者导出分页等于参考实现（实现细节） | journal专属-删除 | — | §3 journal store（export_since 分页） |
| `storage/scaling/test_orgfs_history_reads.py`（298） | 全部 | 7 | store + `SpaceAuthority` | 导出分页、不重扫历史、`document_authors` 只解码新行、对端拉全量时读仍快、最长临界区报告（实现细节） | journal专属-删除 | — | §3 journal store + `document_authors`（Q1）。“对端同步时本地读不被阻塞 < 250ms”（:227-271）应由 C6 在新 store 上另写 |
| `storage/scaling/test_orgfs_history_safety.py`（210） | 全部 | 2 | store `_db` | 替换目标已有 journal 历史时拒绝；非单调历史全扫（实现细节） | journal专属-删除 | — | §3 retirement/replacement + journal |
| `storage/scaling/test_orgfs_working_cache_safety.py`（99） | 全部 | 2 | store working doc + SQL 回滚 | 提交失败/SQL 回滚丢弃复用的 working doc（实现细节） | journal专属-删除 | — | §2 差别 2（store 自持 Doc 副本）+ SQLite |
| `storage/scaling/test_orgfs_write_cost.py`（374） | `test_a_write_costs_the_same_in_a_small_and_a_large_space`、`test_identical_directory_documents_create_no_tree_commits`、`test_space_authority_lock_hold_stays_below_250ms_during_list_write_mix`、`test_steady_state_commit_does_not_reapply_document_history` | 4 | runtime/facade + 计数 fsync/写字节、`_CommitRecorder` 假 store、`_HoldProbe` 锁探针 | 单次写的成本不随空间规模增长；相同目录文档不产生写；锁持有 < 250ms；稳态提交不重放历史（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store（C6 规模） | 规模性质是 C6 的验收；计数点在 `_SpaceSnapshot`、`store.commits`、`LocalSpaceStore` 内部 |
| `storage/scaling/test_orgfs_write_cost.py`（374） | `test_unchanged_files_share_their_snapshot_entry_across_versions`、`test_a_delete_only_content_change_is_not_shared_from_the_old_version` | 2 | facade 版本快照 `_SpaceSnapshot` + `read_at`（:100-139） | 跨版本快照共享（已决定删除的能力） | journal专属-删除 | — | Q5/A1（`read_at` 与版本快照） |
| `storage/scaling/test_orgfs_write_cost.py`（374） | `test_writes_since_the_last_checkpoint_survive_a_crash` | 1 | runtime + 直接关 `store._db` 模拟崩溃（:355-375） | checkpoint 之后的写在崩溃重开后仍可读（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | “确认的写崩溃后不丢”是核心持久性契约；搭建依赖 `checkpoint()` 与 `_db`，改为对新 state+delta 文件模拟撕裂 |
| `storage/state/test_orgfs_state_hosts.py`（63） | `test_store_aliases_are_one_canonical_class`、`test_purge_subflows_keep_the_durable_publication_sequence` | 2 | 导入期断言 | `LocalSpaceStore` 别名；purge 子流程发布顺序（实现细节） | journal专属-删除 | — | §3 journal store + Q4 |
| `storage/state/test_orgfs_state_hosts.py`（63） | `test_only_canonical_hosts_own_construction` | 1 | 导入期结构断言（`LocalOrgFs`、`LocalSpaceStore`、`OrgFsMesh`、`OrgFsRuntime`） | 只有规范宿主类有 `__init__`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用 | 架构门保留，宿主类清单换成新包 |
| `storage/state/test_orgfs_state_hosts.py`（63） | `test_directory_budget_has_one_configuration_owner` | 1 | runtime 私有 `_directory_capacity` | announce 缓冲预算单一配置源（机制细节，去留未定） | 待定 | — | 随 B.4 是否保留 announce 缓冲 |
| `storage/state/test_orgfs_state_root.py`（30） | 全部 | 3 | `storage.blobs._state_dir` | state 根目录选择、无 shell 导入（用户可见） | 契约-保留 | blob | blob 存储保留 |
| `storage/store/frontiers/benchmark.py`（158） | 0（非测试） | 0 | `FileSpaceStore` + `meta_frontiers` 表 | 成员 token 缓存基准（实现细节） | journal专属-删除 | — | Q2：meta frontier |
| `storage/store/frontiers/probe_delete_sets.py`（88） | 0（非测试） | 0 | `FileSpaceStore` meta_frontiers | 探针脚本（实现细节） | journal专属-删除 | — | Q2：meta frontier |
| `storage/store/frontiers/probe_scale.py`（107） | 0（非测试） | 0 | `FileSpaceStore` meta_frontiers | 探针脚本（实现细节） | journal专属-删除 | — | Q2 |
| `storage/store/frontiers/test_meta_frontiers.py`（360） | 全部 | 13 | store `_membership_at`、`meta_frontiers` 表、`expire_pending` | 成员 token 缓存与 journal 回放等价、预算（实现细节） | journal专属-删除 | — | Q2 + §2 差别 3（无 meta frontier、成员回放） |
| `storage/store/pending_replay/test_pending_replay.py`（377） | 全部 | 4 | store pending 表 + `_membership_at` | 不可解析 pending 第二轮不重放、排水预算、`membership_at` 与原算法等价（实现细节） | journal专属-删除 | — | Q2（pending 是按 meta frontier 暂存） |
| `storage/store/test_member_add_frontier_defer.py`（55） | 全部 | 1 | store `meta_frontiers` | frontier 物化失败时延迟而不抛（实现细节） | journal专属-删除 | — | Q2 `member_add_frontiers` |
| `storage/store/test_member_add_index.py`（989） | 全部 | 21 | store `member_add_frontiers` 表、重建、编解码计数 | 成员加入 frontier 索引与扫描一致、线性重建、缺失 fail-closed（实现细节） | journal专属-删除 | — | Q2 + §1/§2 差别 3 明列 `member_add_frontiers` |
| `storage/store/test_orgfs_doc_file_names.py`（236） | `test_the_production_invite_id_is_stored_read_and_reloaded` | 1 | store 直接 commit + 读 `.loro` 文件名（:93-110） | 253 字符的受保护 invite doc id 可存可重开（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | “超长 doc id 能持久化”保留；断言 `.loro` 文件名长度，需换成新 state 文件命名 |
| `storage/store/test_orgfs_doc_file_names.py`（236） | 其余 | 4 | store 旧文件名迁移 + 崩溃注入 | 旧 id 命名文件迁移到哈希名、并发打开不交错（已决定删除的能力） | journal专属-删除 | — | D0 不兼容旧布局（迁移走 R1 export/import） |
| `storage/store/test_orgfs_store_reader_lock.py`（72） | 全部 | 4 | store `_lock` | 读者等待开放的 meta 事务（实现细节） | journal专属-删除 | — | §3 journal store（store 自身锁结构） |
| `storage/store/test_orgfs_store.py`（736） | `test_failed_commit_never_poisoned_in_memory_document`（3 参数）、`test_import_sql_failure_leaves_live_document_unchanged` | 4 | store 直接（注入 too-large/blob-put/SQL 失败，:279-322、:657-691） | 本地提交或入站导入持久化失败时内存文档不变（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | “持久化失败不污染内存态”是 C1 必须继承的性质；失败注入点是 SQLite/blob 钩子 |
| `storage/store/test_orgfs_store.py`（736） | `test_empty_vv_clone_and_incremental_catchup_are_byte_identical` | 1 | store×3 + export/import（:487-528） | 空向量全量克隆与增量追赶得到字节相同的状态（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | state vector 同步的核心等价性；改为新 sync 的“全量 update vs 差量 update”对照 |
| `storage/store/test_orgfs_store.py`（736） | 其余 | 16 | store + `_db`（commits 行、writer/seq、`.loro`/`journal.sqlite3` 布局、cursor、updateBlob、peerId、snapshot manifest、unconfirmed、admission 锁） | journal 冻结布局、幂等导入与 `log-key-conflict`、opaque cursor、大更新转 blob、broadcast 标记、writer client id、manifest、删除时钟标记（实现细节） | journal专属-删除 | — | §2 差别 1/2 与 §3 journal store（:59-656、:693-737） |
| `storage/store/test_round19_convergence.py`（374） | 全部 | 20 | store `_membership`、frontier 戳、pending、重建 | 按 envelope 戳的 frontier 判定作者；乱序 meta 前缀暂存；部分交换收敛（实现细节） | journal专属-删除 | — | Q2（判定只看当前成员表，不按戳记 frontier）；`test_seeded_partial_exchange_converges`（12 种子）的“成员表最终一致”由 C2/C3 另写 |
| `storage/test_journal_checkpoint.py`（194） | 全部 | 3 | store `.loro` checkpoint + 计数解码 | 重放从 checkpoint 之后开始、版本不符回退全量（实现细节） | journal专属-删除 | — | §2 差别 1：无 checkpoint/启动回放 |
| `storage/tree/test_orgfs_tree_docid.py`（279） | `test_r1_rw_member_shadow_tree_envelope_cannot_touch_active_tree_or_reopen` | 1 | runtime + 伪造 envelope（:151-205） | rw 成员发往影子 tree doc 的更新不改变活动 tree、重启后仍如此（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 安全性质（成员不能另起一棵 tree）保留；“活动 tree 指针”在无 retirement 后是否存在由 B 定 |
| `storage/tree/test_orgfs_tree_docid.py`（279） | 其余 | 6 | runtime + store `_db` | tree doc id 映射、`snapshot-barrier`、retirement 记录不可改、字面量 `tree` wire id 拒绝（实现细节） | journal专属-删除 | — | §3 retirement/snapshot barrier |
| `storage/tree/test_orgfs_tree.py`（236） | `test_ambiguous_paths_require_stable_id`、`test_id_anchored_child_ref_bypasses_ambiguous_parent_paths`、`test_path_constraints_and_stale_write`、`test_path_negatives_reject_empty_segments_unknown_ids_and_cross_space_move` | 4 | facade（`LocalOrgFs()` 无 store） | `ambiguous-path` 及候选、`id:` 锚定、路径约束、`expect_version` 的 `stale-write`、跨空间 move（用户可见） | 契约-保留 | facade 通用 | :34-80、:151-169 |
| `storage/tree/test_orgfs_tree.py`（236） | `test_move_log_replay_skips_cycle` | 1 | `TreeDocument` 单元 | 并发 move 成环时跳过（用户可见） | 契约-保留 | C1 state store（tree 保留） | :81-95；计划 §2 保留 TreeDocument 移动语义 |
| `storage/tree/test_orgfs_tree.py`（236） | `test_public_tree_api_restore_and_history`、`test_restore_recovers_deleted_parent_chain`、`test_directory_restore_respects_recursive_flag` | 3 | facade `remove`→`trash`→`restore(node, version)`（:11-32、:97-149） | 从 trash 恢复、恢复缺失父链、recursive 标志；第一条另断言 `history`（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用（B.6 trash） | trash 恢复保留；但 `restore` 现以 `space.snapshots[version]` 取历史版本快照（`document/operations/tree.py:332`），目标形态 trash 只存被删节点最后状态，`version` 参数语义要在 B.6 定；`history` 断言删 |
| `storage/tree/test_orgfs_tree.py`（236） | `test_restore_reverts_rename_and_move_to_earlier_version`、`test_restore_to_empty_is_atomic_and_repeated_restore_is_noop`、`test_restore_snapshot_parent_cycle_fails_instead_of_looping` | 3 | facade `restore` 到早期版本 + SQLite 触发器 | 回退到任意早期版本（已决定删除的能力） | journal专属-删除 | — | Q5 不做历史；10-29 决定 2：trash 只对应被删节点最后状态 |

### 3.17 `（顶层）`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `test_linear_anti_entropy.py`（340） | `test_due_round_schedules_one_pull_per_member_per_org_space`（3）、`test_rotation_reaches_last_holder_within_bounded_ticks`、`test_a_continuously_online_holder_is_reached_while_others_flap` | 5 | runtime + `_RecordingMesh` + `anti_entropy_tick` | 每轮每成员每空间恰一次拉取、有界 tick 内轮到最后持有方（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 调度规模线性是 C3/C6 性质；接口是旧 runtime 的 tick 与假 mesh |
| `test_linear_anti_entropy.py`（340） | `test_left_org_spaces_stay_out_across_restart_and_rejoin_restores_them`、`test_the_daemon_wires_durable_left_orgs_into_the_runtime`、`test_require_orgfs_runtime_tolerates_a_partially_built_application` | 3 | runtime + `OrgNetworkService` 离开 org + `DaemonApplication` | 离开的 org 的空间重启后仍不同步、重新加入后恢复（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 用户可见（离开即停止同步）；观察点是 tick 计数与私有 `_excluded_orgs()` |
| `test_orgfs_storm_local_reads.py`（116） | 全部 | 1 | daemon 请求 + 假持有的 `OrgFsMesh`×20 + `SupplierSyncHealth` | 20 个空间供给方被 hold 时本地读 < 1s（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | “同步风暴不拖慢本地读”保留；搭建依赖旧 mesh |
| `test_orgfs_supplier_health.py`（342） | 全部 | 15 | `_bare_mesh`（`OrgFsMesh`）+ `SupplierSyncHealth` | 跨空间失败聚合 hold、只一个空间探测、成功减半、hold 上限、事件带 spaceId、空间本地拒绝不污染全局（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 退避在 C3 范围内（计划 §4 C3“退避”）；`SupplierSyncHealth` 若原样保留，其中纯类断言可直接沿用，经 `schedule_sync_from` 的需换接口；`test_lifecycle_injects_the_daemon_wide_health` 是源码字符串检查 |
| `test_orgfs_supplier_hold.py`（112） | 全部 | 4 | `_bare_mesh` + `SupplierSyncHealth` | 已知不可达供给方 hold、低于阈值仍重试、成功清 hold、hold 上限（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 同上 |
| `test_orgfs_sync_scaling.py`（684） | `test_sync_timeout_reasons_are_distinct_and_measured`、`test_sync_events_include_wire_progress_and_distinct_failure_reason`、`test_large_space_sync_answers_each_page_within_transport_timeout`、`test_scheduled_large_space_sync_resumes_durable_cursor_and_converges`（2）、`test_sync_page_builders_reject_budget_below_empty_response`、`test_document_page_budgets_the_final_nullable_cursor` | 7 | store + mesh + `_MeasuredBus`/`_StreamingSession` | 超时原因可区分；每页在传输超时内应答；大空间（1500/4000 文档）分多次有界尝试后收敛；页不超预算（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync（C6 规模） | 2.5 万文档规模下“分次有界、进度单调、最终收敛”是新 sync 必须满足的；cursor 与帧类型（`orgfs-sync-documents-page`）随协议重写 |
| `test_orgfs_sync_scaling.py`（684） | `test_replica_sync_pages_budget_the_final_wire_type`、`test_new_requester_bounds_old_peer_fallback_and_new_supplier_accepts_old_request` | 2 | mesh + 假 replica_store / 旧版对端 | replica 分页；旧对端回退（已决定删除的能力） | journal专属-删除 | — | Q3（replica）+ D0（不兼容旧协议） |

## 4. 逐文件清单：orgfs 之外

路径相对 `tests/`。

### 4.1 `tests/daemon/acceptance/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/acceptance/test_orgfs_unavailable_acceptance.py`（166） | 全部（参数化 `OUTAGE_MODES`） | 2 | daemon 请求（golden daemon）+ OrgFS 故障注入 | OrgFS 不可用时消息照常、记录在恢复后补齐（用户可见） | 契约-保留 | facade 通用（消费方） | 只经请求层与 recorder；对 OrgFS 只要求“不可用时抛错”的形状（:91-165） |

### 4.2 `tests/daemon/attach/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/attach/test_attach_orgfs_down.py`（116） | 全部 | 3 | daemon 请求 + `orgfs_fakes.join_test_org` | OrgFS 不可用时 attach/detach/记录本地后补写（用户可见） | 契约-保留 | facade 通用（消费方） | 经 `tests/support/orgfs_fakes`（:30） |

### 4.3 `tests/daemon/cells/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/cells/actor/test_org_cell_actor.py`（194） | 全部 | 3 | `LocalOrgFs(author=...)` 内存 facade 作为 cell 后端（:154-194） | org cell actor 状态投影（用户可见） | 契约-保留 | facade 通用（消费方） | 只用 facade 读写 |
| `daemon/cells/model/test_cell_seams.py`（48） | 全部 | 2 | 导入图检查 | cells 公共面不导入 orgfs runtime（用户可见） | 契约-保留 | —（与 OrgFS 实现无关） | 静态导入门；新包路径若改名需同步模块名单 |
| `daemon/cells/params/test_param_seams.py`（69） | 全部 | 3 | 导入图检查 | params 不加载 orgfs（用户可见） | 契约-保留 | —（与 OrgFS 实现无关） | 同上 |
| `daemon/cells/reconcile/startup/test_startup_adopt.py`（583） | 全部 | 15 | `LocalOrgFs` 内存 facade（:137）+ cell runtime | 启动收养、成员重启/漂移（用户可见） | 契约-保留 | facade 通用（消费方） | “store” 命中是 `StateDatabase`/lifecycle 的 SQLite（:396-408），不是 OrgFS journal |
| `daemon/cells/reconcile/test_daemon_reconcile_e2e.py`（321） | 全部 | 2 | `LocalOrgFs` 内存 facade（:146） | revision 变化增删改成员（用户可见） | 契约-保留 | facade 通用（消费方） | 同上，SQLite 命中属 desired-state |
| `daemon/cells/runtime/cell_runtime_support.py`（127） | 0（支持代码） | 0 | `org_fs()` 构造 `LocalOrgFs(author=...)` 内存 facade（:107-112） | —（用户可见） | 契约-保留 | facade 通用（构造入口） | cells 测试统一经这里拿 facade |
| `daemon/cells/runtime/test_cell_persistence.py`（376） | `test_documents_not_authored_by_their_authority_are_ignored` | 1 | facade：切换 `fs.author` 冒充作者写入（:148-167） | 非 authority 作者写的 cell 文档被 mirror 拒收（用户可见） | 契约-保留 | C2 作者字段 / C5 调用方迁移 | 完全经 facade；依赖 `document_authors`（`cells/runtime/mirror.py:162`）。是 C5 迁移 cell 校验时的现成验收用例，新实现必须让“谁写的”在 facade 上仍可判定（Q1） |
| `daemon/cells/runtime/test_cell_persistence.py`（376） | 其余 | 15 | `LocalOrgFs` 内存 facade + `_RecordingFs` 代理 | `create_only`/`expect_version` 写选项、离线缓存、多设备收敛、owner home 空间、离开的 org 不写（用户可见） | 契约-保留 | facade 通用（消费方） | `expect_version`/`create_only` 是 facade 写语义，新实现必须保留（:65-80）；`seeded._connection` 是 cell 缓存 SQLite |

### 4.4 `tests/daemon/ipc/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/ipc/contracts/fallback_guard/test_orgfs_prefix.py`（131） | 全部 | 12 | daemon 请求路由 + CLI（:62-128） | 未知 `orgfs.*` 方法、restore/worker 通道拒绝；CLI 输出与错误码跨前缀退役一致（用户可见） | 契约-保留 | facade 通用（请求层） | 不碰存储；只依赖 `orgfs.read`/`orgfs.spaces`/`orgfs.create` 存在 |
| `daemon/ipc/contracts/org/test_org_network_specs.py`（337） | 全部 | 13 | 请求目录（`request_catalog` 规格、schema 校验、假 app 转发） | 方法规格、错误码与 schema（用户可见） | 契约-保留 | facade 通用（请求目录） | 不碰 OrgFS 存储；但 `registry.names == (...)` 钉死了含 `orgfs.history/read_at/stat_at/purge*/serve/unban` 的完整方法清单（如 `test_org_specs.py:144-197`），A1/A3/Q3/Q4 删除方法时由集成者同步收窄 |
| `daemon/ipc/contracts/org/test_org_removal_specs.py`（276） | 全部 | 10 | 请求目录（`request_catalog` 规格、schema 校验、假 app 转发） | 方法规格、错误码与 schema（用户可见） | 契约-保留 | facade 通用（请求目录） | 不碰 OrgFS 存储；但 `registry.names == (...)` 钉死了含 `orgfs.history/read_at/stat_at/purge*/serve/unban` 的完整方法清单（如 `test_org_specs.py:144-197`），A1/A3/Q3/Q4 删除方法时由集成者同步收窄 |
| `daemon/ipc/contracts/org/test_org_specs.py`（472） | 全部 | 43 | 请求目录（`request_catalog` 规格、schema 校验、假 app 转发） | 方法规格、错误码与 schema（用户可见） | 契约-保留 | facade 通用（请求目录） | 不碰 OrgFS 存储；但 `registry.names == (...)` 钉死了含 `orgfs.history/read_at/stat_at/purge*/serve/unban` 的完整方法清单（如 `test_org_specs.py:144-197`），A1/A3/Q3/Q4 删除方法时由集成者同步收窄 |
| `daemon/ipc/contracts/org/test_org_write_specs.py`（842） | 全部 | 46 | 请求目录（`request_catalog` 规格、schema 校验、假 app 转发） | 方法规格、错误码与 schema（用户可见） | 契约-保留 | facade 通用（请求目录） | 不碰 OrgFS 存储；但 `registry.names == (...)` 钉死了含 `orgfs.history/read_at/stat_at/purge*/serve/unban` 的完整方法清单（如 `test_org_specs.py:144-197`），A1/A3/Q3/Q4 删除方法时由集成者同步收窄 |
| `daemon/ipc/contracts/org/test_orgfs_read_specs.py`（303） | `test_read_at_requires_a_version`、`test_purge_status_requires_a_plan_id` | 2 | 请求目录 schema | `orgfs.read_at` 需 version；`orgfs.purge_status` 需 planId（已决定删除的能力） | journal专属-删除 | — | A1（read_at）+ Q4（purge_status） |
| `daemon/ipc/contracts/org/test_orgfs_read_specs.py`（303） | 其余 | 33 | 请求目录 + 假 `_handle_orgfs` | 读方法只读 profile、转发到桥、结果 schema、MCP 会话信封（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用（请求目录） | `_READ_METHODS`（:20-34）与参数表含 `read_at/stat_at/history/purge_status`，参数化行要删；`orgfs.status` 的结果样例含 `unconfirmed`/holders 字段需随 Q3 收窄 |
| `daemon/ipc/contracts/org/test_orgfs_write_specs.py`（364） | 全部 | 55 | 请求目录 | 写方法 profile、转发、schema、`createOnly`、MCP 会话信封（用户可见） | 契约-保留 | facade 通用（请求目录） | `_WRITE_METHODS` 含 `orgfs.serve`/`orgfs.purge*`/`orgfs.unban`（serve 3 处、purge 9 处命中），删除方法时参数化行同步收窄；不碰存储 |

### 4.5 `tests/daemon/org_membership/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/org_membership/context/support.py`（33） | 0（支持代码） | 0 | `org_daemon` fixture：真 `DaemonApplication` + `orgfs.create`（:13-33） | —（用户可见） | 契约-保留 | facade 通用（构造入口） |  |
| `daemon/org_membership/context/test_current.py`（77） | 全部 | 6 | daemon 请求（`org_daemon` fixture，`context/support.py:13-33` 经 `orgfs.create`/`orgfs.mkdir`） | current org 失效/损坏 fail-closed（用户可见） | 契约-保留 | facade 通用（消费方） | 只经请求层 |
| `daemon/org_membership/context/test_documents.py`（46） | 全部 | 2 | daemon 请求 `orgfs.write`/`orgfs.read` | 按 org 选择空间读写（用户可见） | 契约-保留 | facade 通用（消费方） |  |
| `daemon/org_membership/context/test_error_vocabulary.py`（23） | 全部 | 10 | `validate_method_spec` | 小写 orgfs 错误码仍可声明（用户可见） | 契约-保留 | facade 通用 | 不碰存储 |
| `daemon/org_membership/invites/test_access.py`（193） | 全部 | 14 | `OrgFsRuntime` + `OrgNetworkService`（:27-67） | 接受邀请的崩溃续做、被撤销成员 `not-a-member`+`status=partial`（用户可见） | 契约-保留 | C2 成员与准入（消费方） | 只经 runtime facade；`orgs._store.list_devices` 打桩的是 org 目录 store，不是 OrgFS journal |
| `daemon/org_membership/invites/test_confirmation.py`（79） | 全部 | 14 | `InviteService` + 假 orgs | 确认绑定、join 错误码透传（用户可见） | 契约-保留 | facade 通用（消费方） | 不碰 OrgFS 实现 |
| `daemon/org_membership/invites/test_recovery.py`（302） | 全部 | 22 | `OrgNetworkService` + 手写 `Fs` 假件（:21-58，实现 join/spaces/members/space_meta/read_text） | 邀请接受失败续做、共享 join 预算（用户可见） | 契约-保留 | facade 通用（消费方） | 只依赖 OrgFs Protocol 的 5 个方法；新 Protocol 收窄时假件需同步 |
| `daemon/org_membership/invites/test_selection.py`（470） | 全部 | 19 | `InviteService` + `_Orgs` 假件 | 邀请列表/接受语义（用户可见） | 契约-保留 | facade 通用（消费方） | 不碰 OrgFS 实现 |
| `daemon/org_membership/migration/test_migration.py`（115） | 全部 | 9 | `LocalOrgFs(author=...)` 内存 facade | org 成员迁移步骤（用户可见） | 契约-保留 | facade 通用（消费方） |  |
| `daemon/org_membership/migration/test_resume.py`（92） | `test_durable_work_space_migration_survives_reopen` | 1 | `LocalOrgFs(stores=FileSpaceStore(...))`（:37-55） | 持久空间上的迁移重开后验证通过（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | 直接构造旧 `FileSpaceStore` 注入 facade；改用新 state store 的构造入口 |
| `daemon/org_membership/migration/test_resume.py`（92） | 其余 | 4 | `LocalOrgFs` 内存 facade + 迁移 journal（org 迁移自身的 journal 文件，非 OrgFS） | 报告私有、旧 journal 兼容（用户可见） | 契约-保留 | facade 通用（消费方） | “journal” 指 `org/membership/migration/journal.py` |
| `daemon/org_membership/migration/test_revision.py`（27） | 全部 | 1 | `LocalOrgFs(stores=FileSpaceStore(...))` + `load_space`（全文 28 行） | `read_text_snapshot().content_revision` 重开后不变，内容改回原值后仍变化（ABA）（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C1 state store | 持久内容修订号是迁移围栏的依据，只有这一处测试；构造入口要换成新 store |
| `daemon/org_membership/migration/test_runtime.py`（62） | 全部 | 4 | `LocalOrgFs` 内存 facade | 迁移运行时读写 org-context（用户可见） | 契约-保留 | facade 通用（消费方） |  |
| `daemon/org_membership/reads/test_agent_org_labels.py`（964） | 全部 | 13 | `FakeFacade` 子类 `_DirectoryFacade`（`storage/support.py:30-172`），桩实现 `document_authors`（:74） | agent 的 org 标签、排除表文件安全、成员 agent 发布（用户可见） | 契约-保留 | C5 调用方迁移（消费方） | 不碰 OrgFS 实现；C5 把 member agent 清单从 `document_authors` 换成作者字段时，假件 `document_authors` 需同步改 |
| `daemon/org_membership/reads/test_member_directory.py`（253） | 全部 | 12 | `FakeFacade` 子类（桩 `document_authors` :41）；最后一条用真 `OrgFsRuntime`（:240-254） | 成员 agent 目录聚合、过期设备排除、partial 状态（用户可见） | 契约-保留 | C5 调用方迁移（消费方） | 同上；`test_real_orgfs_accepts_owner_published_inventory` 是 C5 的真实现验收 |
| `daemon/org_membership/rework/test_legacy_receivers_guard.py`（290） | 全部 | 24 | `LocalOrgFs(stores=FileSpaceStore)` + 打补丁模拟旧版 doc id + `StoreAdmission` + 读 `commits` | 旧版 `receivers.json` 文档 id 的本地/对端写被拒（已决定删除的能力） | journal专属-删除 | — | D0/R1：旧 store 不再被新实现打开，旧数据经 export/import 进入后不会带旧 doc id（:43-290） |
| `daemon/org_membership/rework/test_receivers_ordinary_path.py`（137） | `test_the_path_has_no_protected_section`、`test_the_identity_vocabulary_has_no_receivers_record` | 2 | identity 模块 | `receivers.json` 不是受保护路径（用户可见） | 契约-保留 | —（identity） | 不碰 OrgFS |
| `daemon/org_membership/rework/test_receivers_ordinary_path.py`（137） | 其余 | 2 | `FileSpaceStore` + envelope 搬运 | 旧 receivers 文档在副本上可读、重开可复制（已决定删除的能力） | journal专属-删除 | — | D0/R1 |
| `daemon/org_membership/rework/test_replica.py`（69） | 全部 | 1 | `LocalOrgFs(stores=FileSpaceStore)`×2 + `target.admission(forged)`（:22-64） | 受保护 tree 持久往返、伪造的被拒、成员 agent 目录在副本可读（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 准入规则保留；接缝是旧 store `admission(envelope)` |
| `daemon/org_membership/storage/support.py`（172） | 0（支持代码） | 0 | `FakeFacade` 手写假件（:30-172） | —（用户可见） | 契约-保留 | facade 通用（假件） |  |
| `daemon/org_membership/storage/test_admission.py`（100） | 全部 | 1 | `FileSpaceStore` + envelope 决策（:35-73） | 受保护 id 与路径不匹配的行被拒（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 规则保留，接缝是 store 准入 |
| `daemon/org_membership/storage/test_owner_store.py`（353） | 全部 | 6 | `LocalOwnerStore`（org 成员域自己的本地文件存储） | CAS、权限、符号链接、退役术语（用户可见） | 契约-保留 | —（不属 OrgFS 实现） | 只借用 `OrgFsError` 错误码 `stale-write` |
| `daemon/org_membership/structure/test_seams.py`（169） | `test_the_pending_queue_left_admission_without_a_second_host` | 1 | 导入 `LocalSpaceStore`/`StoreAdmission`/`StorePending`（:77-117） | pending 队列类结构（实现细节） | journal专属-删除 | — | §3 journal store |
| `daemon/org_membership/structure/test_seams.py`（169） | 其余 | 5 | IPC 路由/CLI 模块结构 | 迁移表、facade 导出表、CLI 子包（用户可见） | 契约-保留 | —（不属 OrgFS 实现） |  |

### 4.6 `tests/daemon/organizations/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/organizations/context/test_org_context_orgfs_migration.py`（637） | `test_morg_2_publish_failure_keeps_adoption_and_restart_retry_is_idempotent` | 1 | daemon 请求 + CLI + `restarted.facade.history`（:240-306） | 发布失败保留采用；重启重试幂等——用 `history` 不变来证明“没写新版本”（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用（消费方） | Q5 删 history；改用 `read_text_snapshot().content_revision`/版本号不变来断言幂等（:292-296） |
| `daemon/organizations/context/test_org_context_orgfs_migration.py`（637） | `test_morg_6_node_and_user_sources_keep_result_keys_and_offline_resolution` | 1 | daemon 请求 + monkeypatch `runtime.writer_attributions`（:410-447） | 按节点/用户指定来源的 org.fetch 结果键（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C5 调用方迁移 | `writer_attributions` 在计划 C5 中迁移（`org/orgfs_migration.py:307`），打桩点随之改 |
| `daemon/organizations/context/test_org_context_orgfs_migration.py`（637） | 其余 | 15 | daemon 请求 + CLI + facade 读写 | org context 经 OrgFS 发布/抓取、候选来源 URI、损坏字节拒绝（用户可见） | 契约-保留 | facade 通用（消费方） | 只经请求层与 facade（`stat().uri`、`read_text`） |

### 4.7 `tests/daemon/path_authz/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/path_authz/test_orgfs_gate.py`（205） | 全部 | 8 | daemon 请求 `orgfs.import`/`orgfs.export` | 会话导入导出路径授权、大小上限、硬链接拒绝（用户可见） | 契约-保留 | import-export | 不碰存储（:39-205） |

### 4.8 `tests/daemon/provider_keys/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `daemon/provider_keys/storage/test_orgfs_owner_store.py`（309） | `test_stale_write_from_backend_is_local_stale_write[error1]`、`test_unknown_doc_is_absence[error1]`（`StoreError` 参数） | 2 | 假 fs 抛 `StoreError`（导入自 `orgfs.storage.store.vocabulary`，:12） | 后端 `stale-write`/`unknown-doc` 映射为本地语义（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用（消费方） | 换成新实现的错误类型即可 |
| `daemon/provider_keys/storage/test_orgfs_owner_store.py`（309） | 其余 | 69 | `OrgFsOwnerStore` 在 `OrgFsRuntime` 与 `LocalOrgFs` 两种后端上参数化（`store` fixture :23-35）+ 假 fs | `create_only`/`expect_version` CAS、读写删列、错误映射（用户可见） | 契约-保留 | facade 通用（消费方） | 已经是“同一批测试对两种后端参数化”的现成样板（:23-35），C4 可照此加第三个参数“新实现” |

### 4.9 `tests/integration/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `integration/actor_preservation/orgfs/mesh/support.py`（182） | 0（支持代码） | 0 | `OrgFsRuntime` 两节点 + `bind_admitted`；`assert_client_ids_registered`（:14、:63-66） | —（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 与 `tests/daemon/orgfs/mesh/replication/support.py` 同构；client id 登记断言随 §2 差别 2 删除 |
| `integration/actor_preservation/orgfs/mesh/test_announcement.py`（423） | `test_round4_f5_buffers_announce_until_announcer_liveliness`、`test_final_f7_pending_announces_are_bounded_counted_and_still_apply` | 2 | runtime×2 + `bind_admitted`（`tests/support/tailnet_daemon`）+ 私有 `_holders`/`_pending_announces` | announce 缓冲与上限（机制细节，去留未定） | 待定 | — | 与 `tests/daemon/orgfs/mesh/replication/presence/test_presence.py` 同名用例重复；随 B.4 announce 去留 |
| `integration/actor_preservation/orgfs/mesh/test_announcement.py`（423） | 其余 | 2 | runtime×2 + `bind_admitted` + `assert_client_ids_registered` | liveliness 自动补齐启动广播；重启后重试失败的追赶再接受实时投递（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 重复 `presence/test_presence.py`；client id 登记断言要删 |
| `integration/actor_preservation/orgfs/mesh/test_blob_custody.py`（533） | `test_round3_r4a_announces_all_held_spaces_without_evicting_omissions`、`test_round3_r4b_restart_accepts_live_peer_without_join_or_sync` | 2 | runtime×2 + `bind_admitted` | 重启后接受在线对端、announce 全部持有空间（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 重复 presence 用例 |
| `integration/actor_preservation/orgfs/mesh/test_blob_custody.py`（533） | 其余 | 5 | runtime×2 + `bind_admitted`；`_db` 读 commits（:111）、打桩 `OrgFsMesh` 私有方法 | blob 队列有界、零长度应答有界失败、拉 blob 时入站继续、缺 update blob 只拉一次只重导一次（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync / blob | 重复 `tests/daemon/orgfs/mesh/replication/blobs/test_blobs.py` |
| `integration/actor_preservation/orgfs/mesh/test_document_merge.py`（180） | `test_concurrent_text_edits_from_same_base_do_not_duplicate_base`、`test_review_fix_6_fetches_missing_update_blob_and_retries_once` | 2 | runtime×2 + `bind_admitted` + `broadcast_pending` | 同基线并发编辑不重复；>512000 字节文本跨节点可读（断言 `replica` 引用原因）（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C3 sync | 重复 `mesh/replication/content/test_content.py` 与 `blobs/test_blobs.py` |
| `integration/actor_preservation/orgfs/mesh/test_document_merge.py`（180） | 其余（`_ContentDocument` 单元） | 6 | `document.model._ContentDocument` | 文本 diff 写入（用户可见） | 契约-保留 | C1 state store | 重复 `test_content.py` |
| `integration/actor_preservation/orgfs/test_actor_preserved_orgfs_uri.py`（270） | `test_history_and_stat_at_carry_the_uri` | 1 | daemon 请求 | history/stat_at 带 URI（已决定删除的能力） | journal专属-删除 | — | A1/A3；重复 `addressing/test_orgfs_uri.py` |
| `integration/actor_preservation/orgfs/test_actor_preserved_orgfs_uri.py`（270） | 其余 | 6 | daemon 请求 + 源码扫描 | URI 往返与错误码、错误码登记（用户可见） | 契约-保留 | facade 通用 | 重复 `addressing/test_orgfs_uri.py` |
| `integration/actor_preservation/orgfs/test_actor_preserved_uri_construction_guard.py`（86） | 全部 | 3 | 源码 AST 扫描 | 不拼接 agent URI、不硬编码 web host（用户可见） | 契约-保留 | —（静态门） |  |
| `integration/e2e/test_orgfs_redial_fixture.py`（309） | 全部 | 14 | forwarding 边车夹具 + 假 daemon `command` | 设备行夹具经 `org fs mkdir/write`；边车协议（用户可见） | 契约-保留 | —（只用 CLI 命令名） | 不碰 OrgFS 实现 |
| `integration/orgfs/commands/test_orgfs_checkout_cli.py`（161） | 全部 | 2 | CLI + `DaemonApplication` + `OrgFsRuntime`×2 + 真 Zenoh（`bind_admitted`） | 检出 CLI 端到端、重启后对账（用户可见） | 契约-保留 | checkout | 只经 CLI/runtime/文件系统断言（:77-160） |
| `integration/orgfs/commands/test_orgfs_cli_daemon.py`（144） | `test_daemon_orgfs_surface_and_local_ro_gate` | 1 | daemon `_handle_orgfs` + 改 `facade.author` + 读 `commits` 表（:16-56） | create/write/read/watch；ro 成员写被拒 `not-a-member`；journal 中无该作者提交（用户可见；搭建/部分断言摸实现） | 契约-需改写 | C2 成员与准入 | 前半保留；最后断言读 `stores[...]._db` 的 `commits`，改为断言文件不存在/状态未变 |
| `integration/orgfs/commands/test_orgfs_cli_daemon.py`（144） | `test_fs_cli_exposes_frozen_commands_and_version_flags` | 1 | CLI（打桩 `_daemon_request`） | `fs write --base-version/--expect-version` 参数、`fs --help` 含 `history`/`purge` 等命令、`member invite` 发邀请消息（用户可见；搭建/部分断言摸实现） | 契约-需改写 | facade 通用（CLI） | `--help` 断言列表含 `history`（Q5）与 `purge`（内容清除，Q4）；`--base-version` 是否保留由 B.1 定 |
| `integration/orgfs/commands/test_orgfs_cli_daemon.py`（144） | `test_runtime_restart_rehydrates_current_space_state` | 1 | runtime 重启（:128-145） | 重启后空间与文本仍在（用户可见） | 契约-保留 | C1 state store | 最小的持久化 facade 契约，只经 runtime/facade |

### 4.10 `tests/scripts/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `scripts/test_orgfs_compact_journal.py`（295） | 全部 | 9 | `scripts/orgfs_compact_journal.py` + `LocalSpaceStore` + SQLite | journal 压缩脚本 dry-run/apply/备份/回滚；非 UUID 空间目录拒绝（实现细节） | journal专属-删除 | — | 10-29 删除清单第 2 条（compaction 脚本），§3 journal store；`test_startup_rejects_a_non_uuid_directory_under_spaces` 的“启动拒绝非法空间目录”应由 C1 另写 |

### 4.11 `tests/shell/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `shell/cli/org_membership/test_defaults.py`（70） | 全部 | 3 | CLI 打桩请求 | `work` 默认 org 解析、`org.use`（用户可见） | 契约-保留 | —（CLI） | 只校验发出的 `orgfs.ls` 请求 |
| `shell/cli/orgfs/test_orgfs_uri_cli.py`（146） | 全部 | 3 | CLI 打桩请求 | `fs resolve` URI 形式、web URL、导入导出只带 daemon 解析的身份（用户可见） | 契约-保留 | facade 通用 / import-export（CLI） |  |
| `shell/cli/orgfs/test_space_purge_cli.py`（78） | 全部 | 3 | CLI 打桩请求 | `fs space purge` 需 `--yes`、只调 `orgfs.space_purge`（用户可见） | 契约-保留 | 本地空间清除（CLI） |  |

### 4.12 `tests/support/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `support/orgfs_fakes/app.py`（94） | 全部（支持代码） | 0 | `OrgFsWithMeta(FakeOrgFs)`、`join_test_org`、`stub_app` | —（用户可见） | 契约-保留 | facade 通用（假件） |  |
| `support/orgfs_fakes/org.py`（419） | 全部（支持代码） | 0 | `FakeOrgFs` 手写内存假件（:144-420） | —（用户可见） | 契约-保留 | facade 通用（假件） | 实现 `document_authors`（:278）；C5 后需改成作者字段。消费方测试大量使用（`orgfs_fakes/app.py:45-94`） |

### 4.13 `tests/tooling/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `tooling/architecture/refactor/test_orgfs_refactor_layout.py`（33） | 全部 | 4 | 源码布局扫描（`orgfs` 包） | 目录/文件规模限制、无 `part0*` 模块（用户可见） | 契约-保留 | —（静态门） | 新包放在 `orgfs/` 下也受其约束 |

## 5. 问题回答

### 5.1 `LocalOrgFs` 与 store 的构造入口，以及最小参数化接缝

现有构造入口（都读代码确认）：

| 入口 | 形状 | 位置 | 使用者 |
| --- | --- | --- | --- |
| 内存 facade | `LocalOrgFs(author=..., actor=...)`，`stores=None`，无持久化、无网络 | 例：`contracts/test_orgfs_api.py:82`、`storage/tree/test_orgfs_tree.py:12`、`authority/test_orgfs_actor_boundary.py:25`、`hydration/test_orgfs_d4_hydration_fence.py:51-57`；消费方：`tests/daemon/cells/runtime/cell_runtime_support.py:107-112` | 大多数 facade 契约测试与 cells/org_membership 消费方 |
| facade + 假 store | `LocalOrgFs(FakeStore(), FakeBlobs(), ...)` 或 `LocalOrgFs(stores=Store())`，假件实现 `commit(doc_id, mutate, *, author, actor)`/`commit_many` | `contracts/test_orgfs_api.py:32-55,91`、`document/test_scoped_rollback.py:11-28`、`storage/fuzz/test_orgfs_fuzz.py:37-160`、`storage/scaling/test_orgfs_write_cost.py:141-155,210`、`authority/test_orgfs_optional_store_boundary.py:8-30`、`authority/test_orgfs_actor_boundary.py:615-620` | 失败注入、回滚、fuzz |
| facade + 真 store | `LocalOrgFs(stores=FileSpaceStore(path, space_id, node_id=...))` + `load_space` | `tests/daemon/org_membership/migration/test_revision.py:8-9,17-20`、`test_resume.py:40-41`、`rework/*.py` | 持久化相关的消费方 |
| runtime | `OrgFsRuntime(state_dir, node_id=, author=, trust_tailnet_members=)`。内部用 `_StoreRegistry` 按 space 懒建 `LocalSpaceStore`（`orgfs/composition/vocabulary.py:142-170`），再 `LocalOrgFs(self.stores, self.blobs, self, ...)`（`orgfs/runtime.py:119-128`） | 77 个测试文件 | 绝大多数集成类测试 |
| daemon | `DaemonApplication(state_dir=..., **daemon_dependencies())` → `_require_orgfs_runtime()` | 例：`addressing/test_orgfs_uri.py:24-34`、`contracts/test_orgfs_acl_gate.py:57-84`、`org_membership/context/support.py:13-33` | 请求层测试 |
| 纯 store | `LocalSpaceStore(path, space_id, node_id=, blob_store=)` | 辅助函数：`authority/admission/support.py:21`、`storage/store/test_member_add_index.py:28`、`storage/store/test_orgfs_store.py:34` | journal 层测试 |
| 纯 mesh | `OrgFsMesh(session, store, node_id, ...)` | `orgfs/composition/lifecycle.py:459`；测试中 `blobs/test_orgfs_blob_fetch_replier.py:70`、`test_orgfs_supplier_health.py:25`（`_bare_mesh`）、`mesh/test_orgfs_mesh.py` | sync 内部 |

facade 选 store 的逻辑在 `document/effects.py:595-604`：`stores` 若有 `commit` 属性就当作单一 store 用，若是 Mapping 就按 space_id 取。

**最小接缝（只描述，不实现）**：

1. **不要选 store 协议作接缝。** `commit(doc_id, mutate, author, actor)` 是给“store 自持一份 Doc、facade 再投影一份”的双实例结构用的（计划 §2 差别 2）。
   新 state store 预期是单实例，很难套进这个协议。
2. 在 `tests/daemon/orgfs/contracts/conftest.py`（C4 独占目录）新增一个参数化 fixture，例如 `orgfs_impl`，参数取 `legacy` 与 `state`。它提供三个工厂：
   - `make_facade(author, actor=None)`：内存 facade。对应今天的 `LocalOrgFs(author=...)`。
   - `make_runtime(state_dir, node_id, author)`：带持久化、可 `close()` 的 runtime。对应今天的 `OrgFsRuntime(...)`，同时提供 `reopen()`，用于
     `tests/integration/orgfs/commands/test_orgfs_cli_daemon.py:128-145` 与 `org_membership/migration/test_revision.py` 这类重开测试。
   - `inject_write_failure(runtime_or_facade)`：上下文管理器，令下一次持久化失败。它替代今天各测试里的 `FakeStore.fail`/`Store.fail`
     （`contracts/test_orgfs_api.py:32-47`、`document/test_scoped_rollback.py:11-22`）。
3. 契约-保留的 facade 测试只需把 `LocalOrgFs(author=...)` 换成 `make_facade(...)`。其中 `contracts/`、`storage/tree/`、
   `authority/test_orgfs_actor_boundary.py` 的 watch 类等可以搬进 `contracts/`。
4. 多节点 sync 契约另需第四个工厂 `connect(*runtimes)`，把它们接到同一个进程内假 session 上（§5.2）。这一项属 C3，不必放进 C4 的首批。
5. 构造参数里有两处需注意：`trust_tailnet_members=True`（多节点测试普遍用，见 `mesh/device_rows/test_round4.py:182-193`），
   以及 `bind_transport(transport, supplier_online=..., holder_candidates=...)`。新 runtime 若改了这些名字，工厂要做适配。

### 5.2 多节点测试怎样把节点连起来

| 方式 | 位置 | 与 journal 协议的耦合 |
| --- | --- | --- |
| 真 Zenoh，TCP loopback（`ZenohConfig(listen=("tcp/127.0.0.1:<随机端口>",))` + `connect`），`runtime.bind_transport` | `mesh/device_rows/test_round4.py:171-219`（4 节点）、`mesh/replication/support.py:66-80`、`mesh/test_orgfs_device_row_owner.py:301-365`、`mesh/test_orgfs_p1_integration.py`、`mesh/test_orgfs_p2_integration.py`、`presence/test_orgfs_holder_settlement.py`、`presence/test_orgfs_holders_online.py`、`purge/test_orgfs_purge_twonode.py`、`hydration/test_orgfs_d4_verified_content.py:52-75`、`lifecycle/test_orgfs_late_callback_after_close.py`、`blobs/test_orgfs_resident_blobs.py`；集成目录用 `tests/support/tailnet_daemon.py:28-37` 的 `bind_admitted` | 传输本身无耦合。但这些测试大多随后用 `_receive`/`_deliver_all`/`_position`（`test_round4.py:75-110`）、`_exchange_since`（`test_orgfs_device_row_owner.py:90-120`）直接读写 `commits` 表来搬运或伪造，这部分深度耦合 |
| 进程内假 session：`_Bus` | `mesh/test_orgfs_mesh.py:45-86`、`replication/test_orgfs_replica_mesh.py:31-72` | 只实现 `put/subscribe/declare_query_handler/query` 与 fnmatch 路由，**协议无关，可复用**。键用 `KeySpace` |
| 进程内假 session + 重建 + presence：`_Bus`/`_Session`/`_Presence` | `presence/test_orgfs_rebuild_catchup.py:26-124`（另被 `join/test_supplier_presence.py:14` 引用） | 协议无关，并模拟传输会话 `begin_rebuild/finish_rebuild` 与 presence 上线事件，**可复用** |
| 计量/流式假 session：`_MeasuredBus`、`_StreamingSession` | `test_orgfs_sync_scaling.py:55-98,332-413` | `_StreamingSession` 直接调 supplier mesh 的流式处理器，断言 `orgfs-sync-stream`/`orgfs-sync-documents-page` 帧类型，**与旧协议耦合** |
| 延迟假 session：`_LatencyBus` | `blobs/test_orgfs_blob_fetch_replier.py:29-68` | 只用于 blob 查询，按 `KeySpace.orgfs_blob` 键；blob 帧保留，**可复用** |
| 受控 session：`_ControlledSession`、`ControlledSession`、`_PutSession`、`Native` | `document/test_post_commit_async.py:30-69`、`presence/test_orgfs_presence_settlement.py:16-40`、`instrumentation/test_announce_events.py:24-38`、`authority/test_orgfs_ingress_custody.py:150-165` | 协议无关，用来黑洞或计数 |
| 不经传输，直接调 `import_envelope`/`apply_envelope` | `storage/fuzz/test_orgfs_fuzz.py:171-186`（假件把真 pycrdt 全量 update 包进 envelope JSON）、`authority/admission/membership/test_removal_wins.py:95-100`、`hydration/test_orgfs_d4_d1_seam.py:43-55` | fuzz 的假件实际只传“文档 id + Yjs update”，**稍改即可复用**；其余直接搬 envelope，不可复用 |

结论：

- 新 sync 只要仍用 `hyprial.daemon.impl.transport` 的 session 协议（`transport/api.py:32-73`），`_Bus`、`rebuild_catchup` 的 `_Session/_Presence`、
  `_LatencyBus` 和真 Zenoh loopback 都能原样复用。这一点是 **[推]**：要等 B.4 确认 sync 帧仍走该 session 协议。
- 与 journal 耦合的不是假传输，而是三类东西：
  (1) `KeySpace` 的 `orgfs_log`/`orgfs_log_writer`/`orgfs_log_any`/`orgfs_snapshot` 键（`transport/keys.py:186-223`）；
  (2) 读 `commits` 表、按 rowid 搬运的辅助函数；
  (3) 伪造 envelope 的辅助函数（`_tree_envelope`、`_forged_*_envelope`）。
  C3 要提供替代品：`deliver(source, target)`（经新 sync 交换）与 `forge_update(doc, author=...)`。
- 伪造 envelope 的辅助函数被跨文件复用（`document/test_post_commit_async.py:300-301` 从 `device_rows/test_round4`、`test_round6` 导入）。
  改写时先把它们收拢到 C2/C3 的 support 模块。

### 5.3 只有 store 级测试在保证、没有 facade 级测试的用户可见行为

这是重建时最容易丢的部分。“facade 级覆盖”指经 facade、runtime、请求或 CLI 的断言。

| # | 用户可见行为 | 现在由谁保证（文件:行） | facade/请求级覆盖 | 归属 |
| ---: | --- | --- | --- | --- |
| 1 | 只读成员、已移除成员从**远端**发来的写被拒，且接收方状态零变化（无论哪个节点转发） | `authority/admission/membership/test_membership.py:176-311`、`test_removal_wins.py:126-337,393-408`、`validation/test_validation.py:15-54` | 只有本地写被拒（`tests/integration/orgfs/commands/test_orgfs_cli_daemon.py:16-56`、`contracts/test_orgfs_api.py:137-180`）；远端一条也没有 | C2（B.3） |
| 2 | 受保护目录行（devices/people/invites/leaves）只能由 owner 或路径编码的用户创建、改写、删除、移动；任意到达顺序结论一致 | `mesh/device_rows/test_round4/6/7/9.py`、`mesh/test_orgfs_device_row_owner.py:494-809`、`org_membership/storage/test_admission.py`、`rework/test_replica.py` | 本地写被拒有部分覆盖（`test_round7.py:233-252`、`test_orgfs_device_row_owner.py:366-454,709-765` 的请求层断言）；远端伪造全部只在 envelope 层 | C2 |
| 3 | meta（成员表、`aclSpaceId`、owner）只有 owner 能改，只读成员不能改自己的成员行 | `test_membership.py:205-270` | 本地有 `contracts/test_orgfs_api.py:152-193`；远端没有 | C2 |
| 4 | 成员不能另起一棵 tree 覆盖活动 tree | `storage/tree/test_orgfs_tree_docid.py:151-205` | 无 | C2 |
| 5 | 已确认的写在崩溃（未正常 close）后不丢 | `lifecycle/test_orgfs_journal_crash.py`（store）、`storage/scaling/test_orgfs_write_cost.py:355-375`（要直接关 `store._db`） | 只有正常 close 后重开（`test_orgfs_cli_daemon.py:128-145`） | C1（B.2） |
| 6 | 持久化或入站导入失败时，内存状态不被污染 | `storage/store/test_orgfs_store.py:279-322,657-691` | facade 级只有用假 store 的回滚（`document/test_scoped_rollback.py`、`contracts/test_orgfs_api.py:210-220`），没有真 store | C1 |
| 7 | 非法、篡改的 update 被整体拒绝，不发生部分应用 | `authority/admission/validation/test_validation.py:57-92`、`mesh/test_orgfs_mesh.py:493-506`（超大帧） | 无 | C3 |
| 8 | 空节点全量克隆与增量追赶得到同一状态 | `storage/store/test_orgfs_store.py:487-528` | 无 | C3 |
| 9 | 乱序、重复、丢包、分区愈合后收敛 | `mesh/test_orgfs_mesh.py:112-152`（store+mesh） | fuzz 只经 facade 断言，但交换是全量 update（`storage/fuzz/test_orgfs_fuzz.py:171-186`），没有覆盖丢包和分区 | C3 |
| 10 | pending 有界，伪造来源不能占满诚实供给方的配额，最终由 anti-entropy 收敛 | `mesh/device_rows/test_round10.py:48-125`、`test_round6.py:328-377` | 无 | C3 |
| 11 | 超长 doc id（253 字符的受保护 invite id）可持久化并重开 | `storage/store/test_orgfs_doc_file_names.py:93-110` | 只有 200 字符的文件名经 facade（`test_round4.py:481-494`），且没有重开 | C1 |
| 12 | `read_text_snapshot().content_revision` 跨重开不变；内容改回原值后仍会变化（ABA），迁移围栏依赖它 | `org_membership/migration/test_revision.py`（facade，但要直接构造 `FileSpaceStore`） | 仅此一条 | C1 |
| 13 | 跨节点新建文档不会先以“空内容”出现再补内容 | `mesh/replication/content/test_atomic_birth.py`（用 `history` 观察） | Q5 删除 history 后没有观察点；可改用 watch 事件序列断言 | C3 |
| 14 | 只删除的变更（例如恢复为空）在对端离线错过广播后仍能追上 | `storage/store/test_orgfs_store.py:530-575`；`mesh/test_orgfs_p1_integration.py:569-730` 在 runtime 级，但断言 `unconfirmed_commits` | runtime 级有，断言要换 | C3 |
| 15 | 本地写在节点重启后仍会送达对端（重启前没来得及广播的） | `document/test_post_commit_async.py:183-218`（outbox 回放） | 无 facade 级；新 sync 靠 anti-entropy，需另写多节点测试 | C3 |
| 16 | 大空间（1500/4000 文档）在有界的多次尝试内收敛，进度单调 | `test_orgfs_sync_scaling.py:415-461`（mesh 层） | 无 | C3 / C6 |
| 17 | 每个 space 的串行执行者在 actor 重启后不重复已接纳的写；慢空间不阻塞其他空间 | `authority/test_orgfs_actor_boundary.py:394-485`（打桩 `OrgSpaceAuthority`） | runtime 级但靠私有打桩 | C1 |
| 18 | 启动时拒绝 `orgfs/spaces/` 下的非 UUID 目录（`ORGFS_INVALID_SPACE_DIRECTORY`） | 只在 `tests/scripts/test_orgfs_compact_journal.py:266-272` | 无 | C1 |
| 19 | 删除受保护路径（经别名也一样）被拒且不泄露内容 | 只在内容清除路径：`test_round4.py:441-478`、`test_round6.py:179-215`（Q4 删除） | 新 `rm --permanent` 没有对应测试 | C2（B.6） |
| 20 | 离开 org 后其空间在重启后仍不同步，重新加入后恢复 | `test_linear_anti_entropy.py:224-285`（读 tick 计数与私有 `_excluded_orgs`） | 无 | C3 |

### 5.4 重复用例

`tests/integration/actor_preservation/orgfs/` 与 `tests/daemon/orgfs/` 下列文件近似一一对应：
`mesh/test_announcement.py` 对 `mesh/replication/presence/test_presence.py`；`mesh/test_blob_custody.py` 对 `mesh/replication/blobs/test_blobs.py` + presence 两条；
`mesh/test_document_merge.py` 对 `mesh/replication/content/test_content.py`；`test_actor_preserved_orgfs_uri.py` 对 `addressing/test_orgfs_uri.py`；
`mesh/support.py` 对 `mesh/replication/support.py`。
另有 `hydration/test_orgfs_d4_actor_boundary.py` 中的 `test_read_fetch_coalesces_bounds_and_retains_close_custody` 与 `hydration/test_orgfs_d4_fetch_bounds.py` 重复，
`hydration/test_orgfs_d4_actor_boundary.py` 与 `test_orgfs_d4_hydration_fence.py` 的 `test_hydration_rejects_a_node_rebound_to_another_document` 重复。
C4 与 E2 只需处理每组中的一份。

## 6. 未读与限制

- **只读骨架、未逐行读函数体的**：表中大多数“journal专属-删除”行，因为导入与断言已足以判定。分类为“需改写”或“保留”的行，都读过关键函数体或关键行。
- **范围外、未读**：只把 OrgFS 当后端、不在简报点名目录里的测试，共约 43.6k 行，见本文开头的“范围”。主要有：
  `tests/daemon/org/**`（含 `network/test_org_directory_store.py`、`test_org_acl_space.py`、`cell_actor/test_sub_org.py`，它们直接用 `LocalOrgFs`/`OrgFsRuntime`），
  `tests/daemon/conversations/**`、`tests/daemon/pac/projection/**`、`tests/daemon/contract_mock/**`、`tests/daemon/golden/**`、
  `tests/daemon/channels/mcp/**`、`tests/integration/request_http/**`、`tests/shell/request_http/**`、`tests/tooling/architecture/ipc/**`。
  其中 `tests/daemon/org/network/**` 很可能含有 `document_authors`/`parked_commits` 的消费方断言，属 C5，建议 C5 自己分拣。这一句是 **[推]**。
- 没有运行任何测试。“可复用”“可原样通过”都是读代码得出的判断，没有在新实现上验证过。
- 分类依赖阶段 B 的 4 个开放点，相关条目已标“待定”：B.1（`SpaceStatus.holders_online` 是否保留）、B.4（announce/liveliness 是否保留、`contentFrontier` 是否保留）、
  B.6（`restore` 的 `version` 语义、`rm --permanent` 是否拒绝受保护路径），以及评估 §3 行 29（announce instrumentation）。
