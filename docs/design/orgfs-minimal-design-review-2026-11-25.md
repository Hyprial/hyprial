# OrgFS 精简目标设计审查报告

审查基线是当前工作树的 de8fdf7079。目标文档第 7–8 行写的是 ef30e69ed9，和当前 HEAD 不同。以下代码证据均取当前 HEAD。

## 必须改

### 1. 空探针不能识别已有 Map 键的差量

**位置**

设计 §3.5 第 303–316 行。

**问题**

把差量 update 单独应用到空 Doc 后，不能可靠读出它触及的 Map 条目。对已有 Map 键的替换，差量只有对既有结构的更新。对删除，差量可以只有删除集合。空探针看不到本地已有键，因此成员表、owner、受保护目录和作者字段的准入规则会被绕过，或者把合法差量误判成没有变化。

当前实现已经采用基线加差量的方式。storage/store/admission.py:59-68 先应用 baseline 再应用 update，storage/store/admission.py:133-141 的 tree 校验也一样。

**依据**

pycrdt 实验输出如下。

~~~
delta_replace {'bytes': 39, 'probe': {'m': {}, 'text': '', '__orgfs__': {}}}
delta_delete {'bytes': 13, 'probe': {'m': {}, 'text': '', '__orgfs__': {}}, 'state_equal_before_after_delete': True}
~~~

实验脚本如下。

~~~
from pycrdt import Doc, Map

def roots(doc):
    result = {}
    for name, kind in (("m", Map), ("__orgfs__", Map)):
        value = doc.get(name, type=kind)
        result[name] = None if value is None else value.to_py()
    return result

source = Doc()
mapping = source.get("m", type=Map)
mapping["a"] = "one"
before = source.get_state()
mapping["a"] = "two"
replace = source.get_update(before)
probe = Doc()
probe.apply_update(replace)
print("delta_replace", len(replace), roots(probe))

before_delete = source.get_state()
del mapping["a"]
delete = source.get_update(before_delete)
probe = Doc()
probe.apply_update(delete)
print("delta_delete", len(delete), roots(probe),
      before_delete == source.get_state())
~~~

**建议**

准入探针必须从当前权威文档状态开始，再应用 update。需要同时保留缺前驱结果。缺前驱时进入明确的 pending 或 full-state recovery 路径，不能把空探针读不到条目当成合法。

### 2. 不设持久 pending 会在合并或重启时丢失缺前驱 update

**位置**

设计 §4.4 第 418–422 行，§2.3 第 216–221 行。

**问题**

Yrs 会把缺前驱结构暂存在接收方的内存 pending 集合里，但状态导出不包含这些暂存结构。设计让 SpaceSync 在应用之后直接调用 StateStore.append，却又允许定期把当前 Doc 导出为完整 state。一次合并会丢掉仍未整合的 update。若进程在合并后重启，或者唯一持有前驱的 supplier 离线，这次已经被接受的 update 没有本地可恢复副本。

**依据**

实验先应用后继 update，再查看文档状态和导出结果。

~~~
missing_predecessor_before {'u1': 24, 'u2': 39, 'roots': {'m': {}, 'text': '', '__orgfs__': {}}, 'state_len': 1, 'export_len': 2}
missing_predecessor_after {'roots': {'m': {'a': 'two'}, 'text': '', '__orgfs__': {}}, 'state_len': 10, 'export_len': 46, 'pending_export_same': False}
~~~

storage/store/admission.py:685-693 也明确把这种 update 作为 pending 处理，并指出它只存在接收 Doc 的内存 pending 中。

**建议**

二选一并写入接口契约。把未整合 update 放入有大小、数量和 TTL 上限的持久 pending 文件。或者只要 update 不能在当前权威状态整合，就不接受、不追加，立即请求同一 supplier 的完整状态或带前驱的差量。若采用第二种方案，Admission 必须返回 pending-dependency，SpaceSync 必须把它映射为可重试的同步状态。

### 3. 多文档追加不是崩溃安全的事务

**位置**

设计 §2.3 第 207–219 行，§2.6 第 244–249 行。

**问题**

append_many 只承诺按给定顺序追加，没有原子提交、失败回滚或批次恢复标识。内容先、tree 后只覆盖了新建文件的一种崩溃点。

- 现有文档写入时，内容追加成功而 tree 追加失败，会留下内容状态已前进但 tree 的 contentFrontier、refSize 和 refSha256 仍旧的状态。
- 永久删除时沿用内容先、tree 后，会先删除内容文件，再遇到崩溃，tree 仍然引用不存在的内容。
- 当前实现的 OrgSpaceAuthority.commit_many 在单个 effect worker 内完成多文档 durable 提交，见 storage/space_authority.py:347-369、711-736。设计删除这一层后没有提供等价的批次原子性。

**建议**

增加批次提交记录或等价的两阶段协议。批次至少要包含文档 id、update、预期 state vector、操作类型和完成标记。启动恢复必须能重做或回滚整批，并重新计算 tree 的内容引用。若不做批次协议，至少按操作类型明确不同顺序并提供恢复扫描。

### 4. 不完整 tree 状态下的 orphan sweep 会删除有效内容

**位置**

设计 §2.3 第 220–223 行、§6 第 477–480 行、§8 第 512–513 行，StateStore.sweep_orphans 接口在 §2.6 第 248 行。

**问题**

live_doc_ids 只表达当前已经物化的 tree 行，没有表达 tree 是否完整、是否仍有缺前驱 update、是否处在分页同步中。缺前驱实验表明 tree 的 Doc 可以暂时返回空 Map。内容 update 可以先到，tree update 也可以只整合一部分。此时按当前 tree 行删除所有未引用内容，会把仍在 Yrs pending 集合或后续分页中的内容文件删掉。

**建议**

orphan sweep 必须接收并验证 tree 同步代次、完整状态标记和 pending-empty 证明。只有在完整 tree state 已整合且对应同步轮次完成后才允许删除。StateStore.sweep_orphans 不应由一个可能不完整的 live_doc_ids 自行推断删除。

### 5. 二进制作者字段的事实依据不存在，新协议也无法保留现有受保护路径准入

**位置**

设计 §3.3 第 283 行、§3.5 第 310–316 行、§3.5 第 320 行、§3.1 第 263–264 行。

**问题**

设计称二进制文件作者记在 tree 行的 modified_by。当前 tree CRDT 行并不保存这个字段。document/model.py:657-678 写入的字段没有 modified_by。远端投影只在内存里从 envelope 的 origin.author 设置它，见 document/operations/spaces.py:487-515、597-604。重新加载空间时又把所有节点的 modified_by 设置成 owner，见 spaces.py:291-313。因此该字段不是当前 tree state 的 durable author record。

当前受保护目录准入依赖每个 envelope 的 origin.author。storage/store/admission.py:394-462 将作者传给 protected_directory_author_allowed，vocabulary.py:302-341 据此拒绝非路径作者。新广播帧删除了可验证的本次作者。结果状态中的 modified_by 或 authors 不能证明是谁产生了这次更新。

具体场景是 Bob 仍是当前成员，但向 Alice 的 directory/leaves/alice.json 发送正文修改。新表格只检查正文大小，或者看到已有 authors 等于 Alice 就放行。当前实现会按 envelope 作者 Bob 经过 protected path 规则并拒绝。设备记录、member agent 清单和 cell 镜像同样成立。

**建议**

受保护记录必须保留本次写入的作者证据。可行方式是把作者和 operation 绑定到独立的安全记录，并在 U2 签名可用时强制验证。若当前阶段继续沿用自报作者，也必须保留当前 envelope 作者和当前成员检查，不能宣称安全强度等价。二进制 author 要么写入 tree CRDT 并纳入准入，要么改成明确的 durable sidecar。

### 6. 拒绝记忆没有稳定且可失效的语义，会永久阻止后来合法的 update

**位置**

设计 §3.5 第 323–326 行。

**问题**

拒绝键写成 doc_id、peer、探针内容哈希，没有定义哈希输入。空探针对已有 Map 替换和删除读不到条目，多个不同 update 可能得到相同探针内容。即便哈希实际取原始 update，拒绝结果仍可能依赖当前成员表和 tree。一个 peer 因暂时离开被拒绝，重新加入后同一 update 仍会被静默丢弃。

**建议**

键至少使用原始 update 的 SHA-256、doc id、peer 和 admission policy generation。成员表、owner、tree 受保护路径规则变化时递增 generation，使旧拒绝自动重新评估。记录应有有界 LRU 和明确的状态转移。

### 7. __orgfs__.c 的前进没有覆盖所有写入路径

**位置**

设计 §3.3 第 274–280 行、§4.5 第 426–430 行，当前写入代码在 document/operations/content.py:444-460、548-565，tree.py:136-142，spaces.py:168-207、218-231。

**问题**

Yjs 删除不推进 state vector。实验中纯删除前后 state vector 字节相等。设计依赖每次写入同时推进 __orgfs__.c，但只给了内容文档的叙述，没有规定 facade 的统一事务入口。新实现若漏掉任一 tree、meta 或 blob 行为，§4.5 的向量相等即内容相等就不成立。

当前 journal 之所以有覆盖时钟，是 storage/store/journal.py:733-735 的 _advance_commit_clock。删除 journal 后，write_bytes、mkdir、move、remove、永久删除、restore、内容写入伴随的 tree frontier 更新、update_space_meta、invite、remove_member、创建空间和初始化 meta/tree 都要明确触碰 clock。

**建议**

把 clock 更新放进唯一的本地提交 helper，并要求每个被接受的远端 update 也满足 clock 或等价删除摘要不变量。同步判定必须比较解码后的 state vector map，不能比较未经规范化的原始字节。现有代码用 decode_state_vector 做值比较，见 document/operations/content.py:248-257 和 storage/store/vocabulary.py:431-463。

### 8. leave request 的 memberAddedAt 没有写入路径和格式校验

**位置**

设计 §5 第 464 行，当前写入方 daemon/impl/org/network/tailnet/__init__.py:137-141。

**问题**

设计要求 leave 请求记录写入 memberAddedAt，但当前 self-leave 记录只有 org、user、deviceId、requestedAt。设计列出的 C5 只提到一处判断逻辑和两处诊断，没有提到更新记录生产者、schema 校验和成员表读取的一致性。新字段若只是客户端自报，不能替代旧的因果覆盖关系。

**建议**

在 leave request 的唯一生产入口读取当前成员行的 addedAt 并写入。读取方必须要求字段存在、类型正确、等于当前 active member row 的值。调用方迁移、协议文档和契约测试要一起列入 C5。

## 应该改

### 9. RuntimeDirectory 不能原样复用，内部接口描述互相冲突

**位置**

设计 §0.4 第 67–82 行、§4.1 第 345–355 行、§2.6 第 237–253 行，当前 composition/directory.py:448-451。

**问题**

设计说 RuntimeDirectory 可以原样复用，但 §0.4 同时保留了 store.expire_pending、store.take_drained 和 facade.apply_envelope。新的 StateStore 没有这些方法，§4.4 又删除了 pending 表。当前目录维护 tick 仍会调用这三个 seam。新公告增加 protocol 3 后，目录还必须只接受 protocol 3。当前 announce 解析只校验 schemaVersion、type、node 和 spaces，没有 protocol 过滤。

**建议**

把目录 tick 改成调用 SpaceSync 的维护入口，或删除旧 pending drain。补充 protocol 3 的公告解析和旧公告隔离。StateStore、SpaceSync 和 RuntimeDirectory 的连接关系要写成一个完整的内部接口。

### 10. 新 key 布局缺少 KeySpace 接口变更

**位置**

设计 §4.1 第 347–355 行，当前 transport/keys.py:186-235。

**问题**

设计使用 /org/fs3/sync 和 /org/fs3/update，当前 KeySpace 只有 /org/fs/.../log、sync、blob 和 announce 方法。若只在 sync 实现里拼字符串，会绕过统一 segment 校验。设计没有列出新增方法、通配订阅、查询声明和旧 key 拒绝策略。

**建议**

在 C3 冻结点加入 v3 key 和 wildcard 方法，沿用 _orgfs_segment 校验。把旧 key 无订阅、无应答和公告 protocol 过滤写成契约测试。

### 11. StateStore 接口不足以表达批次恢复和内容完整性

**位置**

设计 §2.6 第 239–249 行。

**问题**

接口没有批次 id、原子提交、提交状态查询、pending 状态、完整 state vector 摘要、恢复未完成批次或回滚方法。load 的可直接 apply update 也没有规定是否包含 delta 中尚未整合的 bytes。compact 没有说明正在 compact 时 append 的排他关系和失败结果。

**建议**

至少补充批次提交和恢复语义。若保持单空间串行执行者，接口可以简化成一个 commit_batch，同时完成 ordered append、fsync、批次标记和恢复。

### 12. Admission 接口不够表达现有准入判定

**位置**

设计 §3.5 第 291–321 行，§7 第 490–492 行。

**问题**

Admission.admit 没有定义当前目标文档、meta 和 tree 的一致快照、缺前驱与可重试 pending 的区别、受保护路径的本次作者证据、signature verifier、update hash、policy generation、blob 引用和树环检查结果。当前 admission.py 至少区分 pending-meta、pending-dependency、not-a-member、unknown-doc 和 invalid-argument。新接口只写 Accept 或 Reject(code) 会把这些状态压扁。

**建议**

定义带状态和详情的结果类型，例如 Accepted、Pending(reason, retry_key)、Rejected(code, update_hash, policy_generation)。输入应绑定一致 read view。对受保护安全记录增加作者或签名证明参数。

### 13. SpaceSync 接口缺少入站、同步轮次和关闭后的重试语义

**位置**

设计 §4.7 第 439–454 行。

**问题**

接口只有 schedule_sync_from、announce、publish、request_content、peers_state、close。没有入站 update、query handler 注册、transport rebuild、完整同步轮次、pending recovery、blob retry、供应方选择和目录 tick 连接。apply_remote 和 read_state 只出现在文字说明，没有冻结为类型。

peers_state 也不足以实现 §4.6。status.synced 需要最近一次每个在线 holder 的 meta/tree vector、本地未覆盖提交、content pending 和 supplier 是否仍在线。

**建议**

冻结构造参数和 callback 协议，至少包括 apply_remote、read_state、transport bind/unbind、sync round result、pending recovery 和 peer state。把 peers_state 定义成可计算 synced 的不可变值对象。

### 14. status.synced 没有处理并发提交和 holder 集合变化

**位置**

设计 §4.6 第 432–437 行。

**问题**

“自上次成功同步后没有未被任何持有方向量覆盖的本地提交”需要一个提交代次或每文档 state vector。设计没有定义该代次如何与 publish、远端 apply 和 holder 删除原子关联。holder 从在线变离线后，保存的旧向量还能否继续用于 synced 也没有写清。

**建议**

定义 per-space monotonic local commit generation 和每个 holder 的 observation generation。holder 离线立即使对应观察失效。synced 只由一个串行 state owner 计算。

### 15. tree 与内容的单实例约束没有覆盖 lazy load、checkout 和 web 读取

**位置**

设计 §2.3 第 216–223 行，§7 第 497–499 行。

**问题**

设计要求每个文档只有一个内存 Doc，同时允许内容按需加载和 LRU。现有 checkout、webserver、watch 和 facade 读取都可以在不同 effect 或 actor 路径触发内容读取。设计没有说明 LRU 淘汰时如何禁止另一个读者保留旧 Doc，也没有说明 checkout 的只读快照是否会算作第二个实例。

**建议**

把 per-space document registry、pin/unpin 和读取快照语义写入 C1/C4 契约。长期读者必须拿不可变 bytes snapshot，不能持有可继续产生操作的第二个 Doc。

## §8 七个重点问题的结论

### 1. 空探针是否能读出差量触及的 Map 条目

结论是**需要改设计**。替换和删除实验都在空探针中不可见。见“必须改 1”。

### 2. 被拒绝 update 重复出现是否造成不可恢复分叉，拒绝键是否稳定

结论是**需要改设计**。拒绝本身不会自动产生 CRDT 不可恢复分叉，但当前方案会让拒绝永久阻止后来合法的相同 update，且探针内容哈希没有稳定定义。见“必须改 6”。

### 3. 向量相等即内容相等是否有漏网写入路径

结论是**需要改设计**。纯删除不推进 state vector，clock 只能在所有写入入口统一执行时成立。见“必须改 7”。

### 4. 无 pending 队列时，缺前驱内容是否会丢失

结论是**需要改设计**。Yrs 内存 pending 不会出现在 get_update，合并或重启后可丢失。见“必须改 2”。

### 5. 一次性 Doc 导出 16 MB tree 的耗时与内存

结论是**不成立为已证实错误，但需要补设计预算**。本机用 25,000 行合成 tree，生成约 19.1 MB update。加载后再次导出耗时约 12 ms，峰值内存增量约 49 MiB。该数据不代表生产 JSON 结构、并发空间数量或最慢机器，不能替代 C6 的真实测试。

~~~
{'rows': 25000, 'row_bytes': 748, 'state_bytes': 19138904,
 'load_export_s': 0.012, 'maxrss_delta': 51429376,
 'same_state': True}
~~~

实验脚本如下。

~~~
from pycrdt import Doc, Map
import json, resource, time

row = json.dumps({
    "node_id": "x" * 24, "parent": "root", "name": "n" * 140,
    "kind": "doc", "doc_id": "doc-" + "y" * 36, "blob_hash": None,
    "deleted": False, "contentFrontier": "A" * 220, "refSize": 1234,
    "refSha256": "z" * 64, "extra": "q" * 100,
}, separators=(",", ":"))
doc = Doc()
nodes = doc.get("nodes", type=Map)
for index in range(25000):
    nodes[str(index)] = row
state = doc.get_update()
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
started = time.perf_counter()
copy = Doc()
copy.apply_update(state)
same = copy.get_update() == state
elapsed = time.perf_counter() - started
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(len(state), elapsed, after - before, same)
~~~

### 6. 多文档追加顺序和崩溃点

结论是**需要改设计**。内容先 tree 后只避免了新建时 tree 指向不存在内容，不能处理已有文档的 stale tree metadata，也不适合永久删除。见“必须改 3”。

### 7. 分页中依据无 tree 引用删除内容是否会误删

结论是**需要改设计**。缺前驱、广播乱序和内容先到仍会使物化 tree 不完整。sweep_orphans 没有完整性证明输入。见“必须改 4”。

## 设计遗漏的现有行为

以下行为与 journal 权威无关，切换时必须保留或由新接口明确承接。

- RuntimeDirectory 的 announce、liveliness、在线 holder 集合、离线 announce 有界缓冲、重建后的补拉、60 秒抖动反熵和设备归属索引。设备归属索引还会扫描 trash 中的删除设备记录，见 composition/directory.py:648-713。
- supplier health 的共享失败计数、指数 hold、空间级失败分类、同步队列上限和重试调度，见 network/supplier_health.py。
- ingress 有界背压和 actor/effect lane 排空，见 mesh.py:184-232 和 network/ingress.py:107-160。关闭时的 deadline、未完成 worker 处理也必须保留，见 mesh.py:338-393。
- transport 的 query、subscribe、liveliness、blob chunk 请求和错误边界。TransportSample 不带来源身份，见 transport/api.py:14-20，新协议不能把它误当成已认证 peer。
- blob 内容寻址、哈希校验、per-space 引用账本、pin/release、空间清理投影和 purged 拒绝，见 storage/blobs.py:317-580。
- RuntimeSpacePurge 的 crash-resumable intent、删除 fence、org deleted 约束和 detach 顺序，见 composition/space_purge.py。
- checkout 的只读物化、watch 驱动 reconcile、持久 checkout 配置和关闭路径，见 composition/checkout.py 与 projection/checkout*。
- webserver 的 PROXY/TLS peer identity、未知 peer fail-closed、只读文件投影和 timeout，见 webserver/identity.py 与 webserver/server.py。
- facade 的 per-space actor、effect lane、generation restart replay、space lock、waiter 结算和 close deadline，见 document/space_state.py:296-539。
- write_text 的三方合并、base_version、expect_version、create-only 和 stale-write 行为，见 document/operations/content.py:348-465。
- tree 的 move 记录、确定性排序、环保护、name conflict、trash/restore 当前状态语义，见 document/model.py:620-784 和 document/operations/tree.py:64-142。
- lazy content hydrate、ORGFS_CONTENT_PENDING 详情、contentFrontier 和读取优先请求，见 document/projection.py:616-716。
- watch/change event、meta watch 回调和 post-commit effect。watch_meta 可以收为内部接口，但不能删除其注册、失效和线程归属语义。
- protected document id、protected node id、路径作者规则、设备和 leave 行格式校验。
- 文件级 import/export、CLI/MCP/GUI 请求目录、OpenAPI 和生成客户端的删改联动。

## 阶段 C 三个接口是否足够

### StateStore

**不足。** 现有草案覆盖了基本 load、append、delete、compact 和 close，但缺少批次原子性、批次恢复、pending 原始 update、完整性状态、compaction 排他性和失败回滚。document_ids 排除 meta/tree 也要明确由 facade 保证这两个文档的固定加载和状态快照。

### Admission

**不足。** 缺少一致 read view、作者证据、签名 verifier、pending 结果、重复结果、策略代次、update hash、完整错误详情和安全记录的特殊规则。只返回 Accept 或 Reject(code) 无法承接现有 admission 的多种 pending 和拒绝路径。

### SpaceSync

**不足。** 缺少入站 update、query handler 注册、transport rebuild、完整同步轮次、pending recovery、blob fallback、peer state 结构、directory tick、关闭后的任务所有权和 apply_remote/read_state 的正式类型。publish 与 facade 的持久提交之间也没有原子边界。

## 并发与崩溃安全空洞

- append_many 没有说明部分成功后的返回值和恢复动作。
- state rename 后 delta truncate 的 fsync、目录 fsync 和 torn truncate 处理没有完全写出。
- facade 在内存 Doc 已经修改后，StateStore append 失败时如何恢复没有接口承诺。当前实现有 rollback snapshot，见 document/projection.py:119-201 和 233-359。
- SpaceSync 广播必须在 append fsync 后发生，但设计没有把这个顺序作为不可绕过的单一提交入口。
- compaction、LRU 淘汰、读取优先 hydration 和 close 可能并发触发，接口没有 pin 计数、取消和 deadline 语义。
- status.synced、content_pending、rejected_updates 和 peer observations 由 facade 与 sync 分散持有时会出现读到不一致快照。
- tree 完整性和 pending-empty 没有成为 orphan cleanup 的前置条件。
- 永久删除后的 blob release 与 tree 批次提交没有同一恢复记录。

## 安全强度差异

设计 §3.1 声称与现状相同，这一结论不成立。

1. 当前受保护文档准入使用 envelope origin.author 和路径作者规则，见 admission.py:394-462。新帧删除逐更新作者，结果状态只检查正文大小或 authors 集合。Bob 可以修改 Alice 的 leave request 或设备记录，造成现状会拒绝而新设计可能放行的具体场景。
2. 当前 meta admission 使用真实 baseline 和 update 的差异，见 admission.py:49-101。新空探针无法看见已有 members[user] 或 space.owner 的替换，普通成员可以伪造 owner-only 行。
3. 当前 document_authors 的 durable 集合虽然是自报作者，但每条已接受 envelope 仍经过当前成员和受保护路径门。新 authors Map 由 update 自己携带，缺少本次写入作者证据。把自报值放进文档不会自动保留现有门。

在 U2 签名未接入前，不能用预留 sig 字段抵消这些差异。必须保留现有自报作者门，或者把受保护记录改成默认拒绝的签名路径。

## 可以考虑的简化

以下简化不改变已定的 Q1–Q7 和 R1 方向。

- 用一个 commit_batch 取代 append、append_many 和 facade 自己拼接的持久化顺序。单一批次入口可以同时完成 clock、fsync、恢复标记和广播准备。
- 把拒绝去重改成原始 update SHA-256 加 policy generation 的有界 LRU，删除探针内容哈希这一层。
- 让 SpaceSync 只负责网络和 peer state，把 publish 合并到 facade 的 durable commit coordinator。这样广播永远不能绕过 append fsync。
- 若 status.synced 由一个串行 state owner 计算，可以让 peers_state 只返回不可变 observation，而不是让 facade 再复制一份 holder frontier。
- 不保留没有调用方的 document_authors history fallback。当前四个跨域调用方应直接依赖文档内的当前作者字段和受保护记录准入。

## 未读或未验证

- 未运行完整 pytest、Ruff、结构门、模块边界门、contract/run-all 或真实多节点场景。
- 未在删除实现后验证导入图、OpenAPI、MCP、GUI 生成物和 sidecar/Go 消费者。
- network/queries.py 的分页游标内部、network/recovery.py 的 projection 修复重试细节和 network/sync.py 的全部边界没有逐行复现，只核对了设计 §0 引用的入口、准入和状态相关路径。
- 未测真实生产 tree 的 25,000 文档 compaction、并发空间数量、最慢磁盘、Windows 文件系统或进程崩溃注入。
- 未验证 pycrdt 不同版本、不同 Rust/Yrs 构建下 state vector 原始字节是否始终同序。报告建议按解码后的 vector map 比较。
- 首次实验未指定 Python 3.12 时，uv 选择了 Python 3.14t，pycrdt 构建因 Rust feature 失败。随后按仓库要求使用 Python 3.12、隔离 CARGO_HOME 和 UV_CACHE_DIR 成功完成上述实验。