# OrgFS 统一 reader：WIP 设计讨论（2026-11-24）

状态：WIP / 讨论稿。本文回答 owner 2026-11-24 的问题——「是否应该统一提供
orgfs 的 reader 来避免这个问题反复出现？」——并记录可评审的候选方案。

本文不是已批准方案，不含实现，不改变任何现有行为，也不代表研发已排期。
`docs/notes/` 是只追加证据；本文是设计讨论，放在 `docs/design/`。

关联：

- `docs/design/orgfs-writer-history-cost-2026-10-20.md`：同一条 store 历史上用
  “增量索引 + 复杂度证明”修掉过读写全量扫描，是本文最重要的先例。
- `docs/notes/orgfs-journal-compaction-2026-10-29/ANALYSIS.md`：checkpoint replay
  与全量 replay 的边界。
- PR #3118（open，未合入 dev）：`orgfs.status` 的物化计数，是本文建议的第一个
  reader tenant。

## 0. 结论（建议）

方向同意：**应该有一个统一的 OrgFS reader / 读模型，以及一套可测的复杂度契约**。
但它不应是第二套存储、第二套权威或一次性大重写。

理由：最近三个现场分属三个不同函数，根因也不同；它们反复出现的共同原因不是
“某段代码写得差”，而是**每条读路径都自行决定如何遍历全量结构，且没有共享的
入口、复杂度声明和回归测试**。只修其中一条，下一条仍会以同样的方式长出来。

建议的最小切片（可独立交付）：

1. 先做**首启与提交路径的 retirement 读模型**：meta 只摊平一次，
   `retired()` 变成 O(1) 查询；
2. status 继续使用 PR #3118 的物化计数；
3. replay 保持 checkpoint 边界，只增加“何时允许全量 replay”的显式守卫；
4. 用同一个复杂度测试脚手架约束后续读路径，再决定是否把剩余读路径迁入 reader。

需要 owner / 研发决定的是：reader 的边界（内部读模型还是 agent 只读面）、
一致性语义、v1 的落点（mixin 还是独立对象）以及负责人。

## 1. 现场：三类重复出现的读成本

以下读数来自 infra-ops 在 2026-11-23 / 11-24 对生产 `state/orgfs` 的只读检查
与候选构建的隔离预演，不是仓库内既有记录；后续应按新读数更新。

| 现场 | 触发 | 当前做法 | 实测 |
| --- | --- | --- | --- |
| `orgfs.status` 超时 | 一次 status 调用 | `storage/store/retirement.py::unconfirmed_commits()` 在 per-space 锁内 `SELECT * FROM commits`，逐行完整解码 envelope（JSON、UUID/orgdir 解析、writer 正则、base64 validate），只为数本地未确认行 | 32.2 万 commit / 1.92 GB envelope：离线全校验扫描 13.010 s；daemon 一次 status 16,489 ms；CLI 上限 15 s |
| 首启 ~10 分钟 | daemon 启动 | `storage/store/__init__.py::_load_documents()` 每读一个 `docs/h-*.loro` 都调用 `retired()`，后者经 `membership.py` 到 `vocabulary.py::_doc_roots()`，把整个 meta 递归摊平成 Python dict | owner-home 空间 24.7k 文档；meta 的 `writers` 根里单条 writer 的 `peerIds` 已积累 24,758 项（约 0.49 MB）；一次 `_doc_roots` 热态约 4 ms、探针冷启动 6–13 ms；候选首启 592 s 才健康 |
| journal replay 71–77 s | 缓存落后 / 缺失 | `storage/store/__init__.py::_replay_journal()` 对 checkpoint 之后的 journal 行逐条解码、逐条 apply，最后重新持久化所有文档 | 2.8 GB / 321,715 commit / 529 文档的全量 apply 71–77 s |

这三条不是同一个 bug：

- status 的成本在 **commit 行数**，且发生在**读调用**里；
- 首启的成本在 **文档数 × meta 大小**（`peerIds` 随文档数增长，因此近似平方），
  且发生在**加载循环**里；
- replay 的成本在 **journal 尾部长度**，正常重启有 checkpoint 兜底，只有缓存
  落后时才全量付。

共同模式只有四条：

1. 成本由**空间总量**（commits / docs / meta）决定，而不是本次请求的变化量；
2. 长活发生在**锁内或单 writer lane 内**，其他调用被饿死；
3. 每个读路径**自己决定怎么扫**，没有共享索引或读接口；
4. 没有**复杂度测试**，回归只能靠生产事故发现。

## 2. 现状与已有局部修复

OrgFS 当前没有一个统一的读面：

- `storage/store/vocabulary.py` 的 `SpaceStore` Protocol 只有写/同步方法
  （`commit`、`export_since`、`import_envelope`、`snapshot` 等），没有读模型。
- `storage/replica.py` 的 `RetirementView` 是一个很窄的读 Protocol，只覆盖
  `retired` / `purge_listed` / `snapshot_point`，且没有复杂度约定。
- status、启动、replay、directory、projection 等读路径直接访问 `_db` / `_docs`，
  或在 facade 锁内调用 store 方法。

已有的局部修复证明“按路径加索引”有效，但也证明每来一个问题就会多一套：

- 2026-10-20 的 writer-history-cost 修复为 `export_since`、
  `document_authors`、`document_covers_member_add` 增加了增量索引，并给出了
  O(1) / O(log n) 的复杂度论证；
- 目录读取增加了按 `(space, path, content version)` 的 quarantine，避免重复解析
  同一份坏行；
- `parked_commits()` 的 docstring 明确要求“只读 pending 表，不扫描 committed
  history”；
- PR #3118 为 status 增加了 `commits.origin_node` 与 `commit_stats`，并配了
  40 / 160 / 640 commit 的常数工作量测试。

这些是 reader 的构件，不是 reader 本身；缺的是统一入口和可执行的边界。

## 3. “reader”指什么

需要先区分三种东西，否则“统一 reader”会被理解成不同的工程：

1. **进程内读模型 / 读 API**（本文讨论的对象）：daemon 内部对某个 space 的
   只读视图，负责把 meta / journal / 文档清单变成有界、可测的查询。
2. **面向 agent / CLI 的只读命令面**：例如让 agent 通过一个稳定的只读接口
   读取 OrgFS 状态。这是另一条产品/契约线；如果 owner 指的是这个，需要单独的
   request catalog、权限和 wire 设计，不能和内部读模型混在一起。
3. **只读快照**：一种实现手段（SQLite read snapshot、CRDT state vector、
   不可变 view），不是目标本身。

本文按第 1 种展开；第 2 种暂不展开，只在开放问题里保留。

## 4. 提案：`SpaceReader`

### 4.1 接口形状

目标是**小而稳定**的读面，不暴露 `Doc`、`Map`、SQL 或任意遍历；返回值是不可变
view / dataclass。候选方法：

| 方法 | 返回 | 目标复杂度 | 说明 |
| --- | --- | --- | --- |
| `facts()` | `SpaceFacts` | O(1) 缓存 | owner、createdAt、kind、name、spaceId；open 时从 meta 构建一次 |
| `members()` | `tuple[MemberView, ...]` | O(members) | 不随 journal 增长；meta frontier 变化时失效重建 |
| `retirement(doc_id)` | `RetirementRecord \| None` | O(1) | 由一次性构建的 retirement 索引回答 |
| `document_ids()` | `tuple[str, ...]` | O(docs) | 来自 `docs/` 清单 + checkpoint，不解码 envelope |
| `document_meta(doc_id)` | `DocumentMetaView` | O(1) / O(log n) | 作者、latest origin、覆盖边界等，来自增量索引 |
| `status()` | `SpaceStatusView` | O(1) + SQL | `unconfirmed` 来自物化计数；holders / parked 来自现有表 |
| `replay_boundary()` | `ReplayBoundary` | O(docs) | 每个 doc 的 checkpoint rowid/version，用于判断是否需要 replay |

约束：

- 不提供 `query(sql)`、`raw_doc()`、`iter_all_envelopes()` 这类逃生口；需要新查询
  时增加一个有复杂度声明的方法，而不是在调用方临时扫。
- 每个方法在 docstring 里写清：复杂度、持有什么锁、读到的是哪个一致性快照。
- 读模型可随时从磁盘重建；它不是权威，不产生独立持久状态。

### 4.2 维护方式

- **open**：加载 meta 一次，构建 `facts`、`members`、retirement 索引、文档清单；
  不再在每个文档加载时重复摊平 meta。
- **commit / import / retire**：在同一个 SQLite 事务里更新计数与索引
  （PR #3118 的 `commit_stats` 是模板）；退休文档时同步删除/失效对应项。
- **legacy 数据**：首次 open 做一次有界分类（PR #3118 的 legacy 分类是模板），
  之后不再逐行解码；分类进度必须可恢复、可重入。
- **meta 变化**：以 meta frontier / journal rowid 作为失效令牌，只重建变化的视图；
  不能在没有令牌的情况下长期缓存 membership。

### 4.3 落点选项

| 选项 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| A（建议 v1） | 在 `LocalSpaceStore` 上增加 `StoreReadModel` mixin，实现 `SpaceReader` Protocol | 改动小，复用现有锁与连接；调用方可依赖 Protocol | reader 仍与 store 同生命周期，容易被绕过 |
| B | 每个 space 一个独立 `SpaceReader` 对象，store 只暴露构建入口 | 边界清晰，便于测试和替换 | 需要定义锁/连接共享方式，迁移面更大 |
| C | daemon 级 reader 服务，跨 space 聚合 | 适合全局视图 | 引入第二层缓存和一致性问题，暂不建议 |

建议 v1 选 A，但**调用方只依赖 `SpaceReader` Protocol**；如果 A 的边界在实践中
不够，再迁到 B，不需要改调用方。

## 5. 复杂度契约与门禁

reader 的价值不在于“多一个类”，而在于把复杂度变成可执行的门：

1. **每个读方法必须有 scaling test**：至少验证 rows fetched、envelope decode 次数、
   SQL statement 数不随 journal / docs 增长。PR #3118 的
   `classify_growth` / `assert_work_growth` 可以直接复用。
2. **禁止在读路径解码全量 envelope**：status、startup、projection 的测试要断言
   decode 次数有界；CI 可用计数器或 monkeypatch 统计。
3. **禁止在持锁期间做长活**：新增一个可观测的 `read_cost` 事件（space、method、
   rows、decodes、lock_wait_ms），或在测试里断言锁内工作量。
4. **静态守卫**：对 `_doc_roots(`、`SELECT * FROM commits`、`_decode_envelope(`
   的直接调用做 AST 扫描，只允许出现在 reader / journal 维护模块；新增例外要写
   理由。仓库已有同类先例（目录 quarantine 的 AST 回归测试）。
5. **PR 模板**：读路径变更必须回答“复杂度是什么、为什么不会随历史增长、哪个
   scaling test 覆盖”。

## 6. 迁移顺序（每步可独立回滚）

1. **盘点 + 测试脚手架**：列出所有读路径及其当前复杂度；引入 decode/rows 计数和
   scaling helper。无行为变化。
2. **status tenant**：PR #3118 的物化计数落在 reader 的 `status()` 后面；调用方
   从 `store.unconfirmed_commits()` 迁到 `reader.status()`。
3. **startup + commit 的 retirement 索引**：open 时构建一次 retirement 索引；
   `_load_documents()` 与 `_commit_one_inner()` 不再逐次摊平 meta。这是首启和写
   延迟的共同修复，收益最大、语义最简单。
4. **replay 边界**：保留现有 checkpoint；增加“只有 checkpoint 不匹配/缺失才允许
   全量 replay”的显式判断与读数；正常重启只 apply 尾部。
5. **收口**：把剩余 ad-hoc 读路径迁入 reader；启用静态守卫；删除重复索引。

## 7. 非目标

- 不替换 SQLite、pycrdt 或 OrgFS 的 wire 协议；
- 不改变已发布字段、错误码、事件名；内部 SQLite 允许 additive schema（如 #3118）；
- 不在本设计里做 retention / compaction；那是独立工作项；
- 不让 reader 成为第二写路径或第二权威；
- 不做一次性大重写；每一步都必须能单独合入、单独回滚。

## 8. 风险与开放问题

风险：

- **一致性**：status 的 `unconfirmed` 必须精确；若 reader 缓存滞后，会把同步健康
  读错。计数必须与 commit 插入/删除同事务更新，不能最终一致。
- **锁**：reader 不能在持写锁时做长扫描；需要明确“锁内复制、锁外计算”或
  SQLite read snapshot 的边界。CRDT 文档有线程绑定的限制，不能简单搬到线程池。
- **内存**：读模型自身不能无界增长；`peerIds` 已经是 24.8k 项，需要定义上限、
  压缩或按需加载，否则只是把 CPU 成本换成内存成本。
- **迁移**：legacy 行分类必须一次性、有界、可重入；失败时不能阻塞启动。
- **接口膨胀**：reader 容易变成 god object；应按 `facts` / `journal stats` /
  `document index` 拆成小 view，而不是一个大类。

需要 owner / 研发回答：

1. owner 说的 “reader” 是第 3 节的第 1 种（内部读模型），还是第 2 种
   （agent / CLI 只读面）？
2. 哪些读必须强一致（status 的 `unconfirmed` 显然是），哪些可以接受快照/最终一致？
3. v1 用 `LocalSpaceStore` mixin（选项 A），还是独立 `SpaceReader` 对象（选项 B）？
4. 是否允许 reader 使用独立的只读 SQLite 连接，还是所有 SQL 必须走现有连接？
5. 首启的 retirement 索引能否先作为独立切片合入，不等完整 reader 设计？
6. 负责人是 OrgFS 存储维护者还是 daemon 平台线？infra-ops 可以提供生产读数与
   隔离预演，但不拥有产品代码决策。

## 9. 验收标准（建议）

- `orgfs.status` 在 40 / 160 / 640 commit 与 30 万级 legacy 空间上，rows / decodes /
  statements 有界；不随 journal 增长（PR #3118 已有模板）。
- 首启在 24.7k 文档的 owner-home 上：meta 只摊平一次；`_load_documents` 的
  `_doc_roots` 调用次数为 O(1)，不是 O(docs)；文档数翻倍时启动时间近似翻倍而不是
  四倍。
- 每次 commit 的 `retired()` 为 O(1)；写 p99 不随 meta 大小增长。
- 正常重启的 replay 行数等于 checkpoint 之后的尾部；只有 checkpoint 不匹配/缺失
  时才允许全量 replay，并记录原因与行数。
- 新增读路径没有 complexity test 与 review checklist 不得合入。

## 10. 参考

- `docs/design/orgfs-writer-history-cost-2026-10-20.md`
- `docs/notes/orgfs-journal-compaction-2026-10-29/ANALYSIS.md`
- `docs/notes/orgfs-pending-replay-fix-2026-11-02.md`
- PR #3118：`fix(orgfs): materialize unconfirmed commit count for status`
- infra-ops 2026-11-23 只读诊断（status 13.010 s / 16,489 ms）与 2026-11-24
  隔离预演（首启 592 s、24.7k 文档、meta `peerIds` 24,758 项）——现场读数，
  随生产变化需重测。
