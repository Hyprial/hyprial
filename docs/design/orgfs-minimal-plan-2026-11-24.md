# OrgFS 精简与重建：执行计划（2026-11-24）

状态：WIP 执行计划。本文是 `intg/orgfs-minimal` 集成分支的事实源；新 session 先读
本文与 `orgfs-simplification-eval-2026-11-24.md`，再动手。进度写回本文 §9，不写在
消息里。

目标：把 OrgFS 收敛成“目录同步 + CRDT 协同编辑”，复杂处理交给 pycrdt/Yjs；删除
找不出明确需求支撑的模块；不兼容现有 OrgFS 的磁盘布局与线上协议。

## 1. 已定的决定

| 编号 | 决定 | 来源 |
| --- | --- | --- |
| D0 | 按最大精简方向做；不考虑兼容现有 OrgFS | owner 2026-11-24 |
| Q1 | “文档是谁写的”用文档内作者字段 + 写入时校验，不保留逐提交历史与 client id 归属 | owner 2026-11-24 |
| Q2 | 被移除成员的写入按**当前成员表**拒绝，不判断是否产生于移除之前 | owner 2026-11-24 |
| Q4 | 内容清除 = 从当前状态删除；不做全网历史副本清除与封禁 | owner 2026-11-24 |
| Q7 | 倾向“目录先到、内容按需拉取”；实现复杂则退回整空间同步 | owner 2026-11-24 |
| Q3 | 不报告“多少修改尚未被其他节点持有”，只保留粗粒度“是否已同步”；删除常驻持有方角色 `fs space serve` 与 `storage/replica*` | owner 2026-11-25 |
| Q5 | 不做历史：删除 `orgfs.history`、CLI `fs history` 与 GUI 历史面板 | owner 2026-11-25 |
| Q6 | `open_doc` 没有计划中的消费者，删除 | owner 2026-11-25 |
| R1 | 现有数据用“旧版 `fs export` → 新版 `fs import`”带过去；org 目录空间不迁移，各节点重新 join（全网重新入网，窗口由 owner 安排）。阶段 D 之前先在生产数据的克隆上演练一次约 2.5 万文档的导出导入 | owner 2026-11-25 |

Q1–Q7 与 R1 均已回答，没有待确认项。

## 2. 目标形态

```
调用方（CLI / daemon 请求 / org 域 / cells / GUI）
        │  OrgFs Protocol（收窄后）
        ▼
   facade：读写、mkdir、mv、rm、trash、watch、成员
        │  每个 space 一个串行执行者
        ▼
   space state：每个文档只有一个内存 Doc 实例
   ├─ 持久化：state 文件 + 追加的 delta 文件，定期合并
   ├─ 成员：meta 文档里的当前成员表，写入时校验
   └─ tree：TreeDocument（保留现有移动语义）
        │
        ▼
   sync：按文档交换 state vector，回补差量；目录先到，内容按需
   blobs：内容寻址存储（保留）
```

与现状的三处根本差别：

1. **没有逐提交 journal。** 文档的当前 CRDT 状态就是权威。没有 `commits` 表、
   envelope、writer/seq/lane、checkpoint、启动回放、retirement、snapshot barrier。
2. **每个文档只有一个内存实例。** 现状 facade 与 store 各持有一份 Doc，并各自使用
   不同的 client id，这是 client id 登记、`peerIds`、stale-write 检查的来源。目标
   形态下每个 space 每次进程启动只用一个 client id。
3. **成员判定只看当前状态。** 没有 meta frontier、成员回放、`member_add_frontiers`。

保留不动的：blob 存储、只读检出、web server、本地空间清除、文件级 import/export、
trash/restore、watch、tree 移动。

## 3. 删除清单

| 项 | 位置 | 约行数 | 前置 |
| --- | --- | ---: | --- |
| `journal_history`、`read_at`、`stat_at` | api、tree/content operations、bridge、catalog、MCP、GUI | — | 无 |
| 结构化文档 `open_doc` | `document/structured.py` | 511 | Q6 确认 |
| `history` | `document/operations/tree.py`、CLI `fs history`、GUI | — | Q5 确认 |
| 内容清除 plan/run/status/unban | `document/operations/purge.py`、`projection/purge.py` | 1,036 | 新的 `rm --permanent` 就位 |
| 常驻持有方 | `storage/replica*.py`、`fs space serve` | 1,642 | Q3 确认 |
| journal store | `storage/store/*` | 5,276 | 新 state store 通过契约测试 |
| journal 同步协议 | `network/sync.py`、`queries.py`、`recovery.py`、`protocol.py` | 2,679 | 新 sync 通过多节点测试 |
| `document_authors`、`document_covers_member_add` | api 与四个跨域调用方 | — | 作者字段就位，调用方迁移完 |
| `watch_meta` 的公共入口 | api | — | 改为 orgfs 内部接口 |

## 4. 阶段

每个阶段结束都要在 `intg/orgfs-minimal` 上跑完 §6 的门并把结果写进 §9。

### 阶段 A：无前置的删除（可立即并行）

A1 历史读取（`journal_history`、`read_at`、`stat_at`）。A2 `open_doc`。A3 `history`。A4 `watch_meta` 收为内部。
Q5、Q6 已于 2026-11-25 确认，A2、A3 可以做。每项一个 agent、一个分支。

合并顺序（2026-11-25 复核后修正“可立即并行”）：A1–A4 都要改 `orgfs/api.py`；A1 与 A3 还共用
`orgfs_bridge.py`、`ipc/router.py`、`mcp/server.py`、`request_catalog/org/orgfs/*`、
`request_catalog/migration.py`、`document/operations/tree.py` 与同一批钉方法清单的测试。因此
A1 → A3 串行（A3 的分支从合入 A1 后的集成分支建出），A2、A4 可与它们并行，合入时只在 `api.py`
有小冲突。GUI 历史面板同时调用 `orgfs.history` 与 `orgfs.read_at`
（`gui/src/features/org/impl/domain/live-source.ts`），GUI 侧的删除整体放在 A3；A1 只做后端，
生成物在 A3 合入后一次重新生成。

A1 同时是“删除一个 daemon 请求方法”的试点：做完后把实际要动的文件、生成器与被钉死
的测试整理成配方写进 §10，后续删除照此执行。已知一次方法删除会牵涉约 30 个文件
（参考提交 `0bff53a27`、`42661a687` 的 `git show --stat`）：orgfs api 与实现、
`application/messaging/orgfs_bridge.py`、`ipc/router.py`、`request_catalog/org/orgfs/*`
与 `request_catalog/migration.py`、`mcp/server.py`、`contract/mcp-tools/run.py`、
`contract/daemon-request/openapi.json`、GUI 请求 API 与生成客户端、contract mock、
方法计数与摘要类测试、golden manifest。

### 阶段 B：目标设计定稿（串行，主 session 亲自做）

产出 `docs/design/orgfs-minimal-design-<date>.md`，必须定下并经 owner 确认：

1. 收窄后的 `OrgFs` Protocol 全文（阶段 C 各 agent 以它为接口冻结点）。
2. 磁盘布局：每文档 state 文件 + delta 文件的格式、合并阈值、崩溃安全步骤。
3. **远端写入的准入规则**：收到对端的 Yjs update 时如何执行 Q1、Q2。Yjs update
   不能按作者拆分，所以必须写清是“只接受来自当前成员节点的 update（依赖传输层
   已认证的对端身份）”，还是“先应用到副本、比对差异、校验后再应用到权威实例”。
   这是整个重建里唯一没有现成答案的设计点。
4. sync 协议帧，以及 Q7 的取舍：给出“目录先到、内容按需”的最小方案与工作量；
   超过约定预算就明确退回整空间同步。
5. 四个 `document_authors` 调用方各自换成什么（设备记录校验、leave 请求、member
   agent 清单、cell 校验），并说明安全性质是否等价。
6. `rm --permanent` 的语义与 Yjs GC 的开启方式。

阶段 B 未经 owner 确认，不开始阶段 C。

### 阶段 C：并行重建（新代码与旧代码并存）

新代码放在新包里，旧实现保持可运行，靠同一套 facade 行为测试对两者对照。

| 工作包 | 内容 | 独占的目录 | 依赖 |
| --- | --- | --- | --- |
| C1 state store | state + delta 持久化、合并、崩溃恢复；每文档单实例 | `orgfs/state/`、`tests/daemon/orgfs/state/` | B.1、B.2 |
| C2 成员与准入 | 当前成员表、本地与远端写入校验、作者字段 | `orgfs/membership/`、对应测试 | B.3 |
| C3 sync | state vector 交换、差量、pending、退避；Q7 | `orgfs/sync/`、对应测试 | B.4 |
| C4 行为契约测试 | 把 `tests/daemon/orgfs/contracts` 与 facade 级测试整理成可对新旧实现参数化运行的一套 | `tests/daemon/orgfs/contracts/` | B.1 |
| C5 调用方迁移 | 四个 `document_authors` 调用方、`parked_commits` 诊断、`writer_attributions` | `daemon/impl/org/`、`cells/` 中对应文件 | B.5 |
| C6 规模契约 | 开启、提交、status、同步的工作量不随历史增长的测试（沿用 #3142 的做法）| `tests/daemon/orgfs/scaling/` | B.2 |
| C7 测试分拣 | 把现有 `tests/daemon/orgfs` 逐目录标成：行为契约（保留）/ journal 专属（随旧实现删除）| 只产出清单文档，不改测试 | 无 |

C1、C2、C3、C4、C6、C7 互不写同一目录，可以同时进行。C5 依赖 C2 的接口。

### 阶段 D：切换（串行）

1. facade 改接新 state、membership、sync；旧 store 仍在树内但不再被装配。
2. C4 的契约测试只对新实现运行并全绿。
3. 真实 daemon 多节点验证（§6）。
4. 运行时装配切换；CLI、请求目录、MCP、GUI 同步收窄。

### 阶段 E：删除旧实现与收尾（可并行）

E1 删除 `storage/store`、`storage/replica*`、旧 `network/*`、retirement/snapshot。
E2 删除 C7 标为 journal 专属的测试。E3 `contract/`、openapi、GUI 生成客户端、
`docs/reference/cli-command-inventory.json` 重新生成。E4 更新 skill
（`.agents/skills/hyprial-e2e`、`hyprial-ops`）、`docs/guides`、`known-problems.md`、
CHANGELOG，写迁移说明。E5 关闭被取代的 PR（§7）。

## 5. 多 agent 并行的规则

1. **一个 agent 一个 worktree 一个分支**，分支从 `intg/orgfs-minimal` 的当前 head 建，
   名字 `refactor/orgfs-<slug>` 或 `feat/orgfs-<slug>`。
2. **agent 只改自己工作包“独占的目录”**加上它要删除的调用点。
3. **生成物只由集成者重新生成**：`contract/daemon-request/openapi.json`、
   `gui/src/client/generated/*`、`gui/src/dev/contract-mock/contract.generated.json`、
   各类计数与摘要、golden manifest。并行分支若都去改它们必然冲突。agent 在报告里
   列出“需要重新生成的东西”，集成者合并后统一跑生成器。
4. **集成者（主 session）职责**：按顺序把分支合进 `intg/orgfs-minimal`、跑生成器、
   跑 §6 的门、把结果写进 §9。不把未经自己复核的 agent 结论写成事实。
5. agent 的交付报告必须包含：改了什么、跑了什么命令及结果、没做或没验证的清单。
6. 只读调研用分支代理（继承上下文）；写代码用独立 worktree 里的新代理，简报里写明
   §10 的环境注意事项。

## 6. 验证门

| 门 | 命令或方法 | 何时 |
| --- | --- | --- |
| 焦点测试 | 改动直接覆盖的测试 | 每个分支 |
| orgfs 全目录 | `uv run pytest tests/daemon/orgfs -n 4` | 每次合入集成分支 |
| 结构与边界 | `check_structural_limits.py`、`check_module_boundaries.py` | 每次合入 |
| 契约投影 | `scripts/verify/dump_daemon_openapi.py --check`；`check_daemon_contract_diff.py` | 动到请求方法时 |
| 全量 | `uv run --with libcst==1.9.0 pytest tests/`；`uv run ruff check .` | 每个阶段结束 |
| 跨实现契约 | `./contract/run-all.sh --isolated-only` | 阶段 D、E |
| 真实多节点 | 按 `.agents/skills/hyprial-e2e` 选场景：两节点建空间、互相写、成员加入与移除、断网重连、重启 | 阶段 D，切换前 |
| 规模 | C6 的测试；另用合成数据测 2.5 万文档的开启时间 | 阶段 D |

结果只用 PASS / FAIL / BLOCKED / 未运行 四种写法。真实多节点验证在云端容器里
大概率是 BLOCKED（没有编译原生 actor 扩展，子进程 daemon 起不来），需要 owner 安排
在真实机器上跑。

## 7. 与在途 PR 的关系

| PR | 内容 | 处置 |
| --- | --- | --- |
| #3145 | meta read model、checkpoint 按时钟比较 | 生产止血，可独立合入 dev；重建完成后随旧 store 一起删除 |
| #3148 | 新建文档用一个 facade client id | 同上 |
| #3118 | status 物化计数 | 同上；由原提交方决定是否合入 |
| #3142 | reader 设计讨论与运维统计 | 设计方向被本计划取代；统计数据保留为证据。建议标注后关闭 |
| #3149 | 精简评估 | 内容已包含在本分支；被本 PR 取代 |
| #3144 | dev CI 红灯记录 | 无关，独立处理 |

## 8. 风险

- **R1 现有数据。** 不兼容意味着升级后旧空间不可读。org 目录空间里有设备记录、
  成员、leave 请求；owner-home 里有用户文件。可选做法：升级前用旧版本
  `fs export` 导出文件、升级后 `fs import`；org 目录空间由各节点重新 join 生成。
  这会让所有节点重新入网，需要 owner 决定并安排窗口。
- **R2 远端准入（B.3）没有现成答案。** 如果只能做到“信任已认证的成员节点”，那么
  一个仍是成员的节点可以伪造另一成员的作者字段。是否可接受需要 owner 判断。
  2026-11-25 读码后更正：传输层并不提供已认证的对端身份，现状的 supplier 与作者都是自报的
  （设计草稿 §0.1、§0.2）。owner 同意的方向：普通文档沿用节点门，四类安全记录（以及成员表）
  做内容签名。
- **R3 `network/`、`composition/directory.py`、`storage/replica*`、`webserver/` 没有
  人读过实现。** 阶段 B 之前必须读完 `network/` 与 `composition/directory.py`。
- **R4 dev 的 CI 目前是红的**（#3144）。集成分支的 PR 检查可能因与本工作无关的原因
  失败，判断时对照 #3144 的清单。
- **R5 Yjs GC 与 trash/restore。** 开启 GC 后被删除内容不可恢复；trash 依赖“被删
  节点的最后状态”仍可读，两者的边界要在 B.6 里写清。
- **R6 公共面很宽。** 31 个 `orgfs.*` 请求方法在 openapi 与 MCP 表里；每次删除都要
  过 Contract-Impact 门并重新生成客户端。

## 9. 进度记录

| 日期 | 项 | 状态 | 证据 |
| --- | --- | --- | --- |
| 2026-11-24 | 评估文档 | 完成 | `orgfs-simplification-eval-2026-11-24.md` |
| 2026-11-24 | 执行计划 | 完成，待 owner 确认 Q3、Q5、Q6 与 R1 | 本文 |
| 2026-11-25 | owner 确认 Q3、Q5、Q6、R1 | 完成 | §1 |
| 2026-11-25 | 复核评估文档 | 发现 1 处错误 | 评估 §0-4、§3 行 15 称 `read_at` 没有 GUI 功能代码；实际 `gui/src/features/org/impl/api/orgfs-request-api.ts:224` 与 `domain/live-source.ts:882` 的历史面板在调用 |
| 2026-11-25 | 真实机器基线（`tests/daemon/orgfs -n 4`，head `f3cd47acdb`，原生扩展已编译）| PASS：784 passed、3 xfailed、0 failed，123 s | [redacted] `~/Workspace/HyprialOS/orgfs-min/reports/baseline/orgfs.txt` |
| 2026-11-25 | owner 追加授权 | 完成 | 实现分支合入集成分支由集成者自行合并；由已定决定直接推出的 orgfs 登记项（迁移表、冻结码、GUI 清单）直接删并在本表记 diff；真实多节点验证用 powerstation 与两台 macOS 构建机的隔离 home；R1 演练可对生产 owner-home 做只读 APFS 克隆；阶段 B 设计由 codex `gpt-6.2-sol` 审查后继续，不再逐项请示；`fs space serve` 与 `storage/replica*` 提前作为 A5 删除 |
| 2026-11-25 | 阶段 B 前置阅读（`network/`、`mesh.py`、`composition/directory.py`）| 完成 | `orgfs-minimal-design-2026-11-25.md` §0。要点：传输层不提供对端身份，现状作者是自报的；`directory.py` 与 supplier 退避、blob 通道与 journal 无关，保留 |
| 2026-11-25 | A1 删除历史读取（后端切片）| 完成，已合入（`1ba7deae6b`）| 提交 `33d52dfc78`、`84da05b096`、`bd41911888`，37 个文件，+117 / -439。agent 实测：新“已删除”测试先 FAIL 后 PASS；`tests/daemon/orgfs -n 4` 784 passed、3 xfailed（与基线相同）；`tests/daemon/ipc` 等六个目录 3853 passed、5 failed，5 条都在 `test_request_contract_gate.py`，原因是 `openapi.json` 未重新生成（预期，生成物在 A3 合入后统一重生）；ruff、结构门、边界门、行内注释门 PASS；隔离 `DaemonApplication` 上两个方法返回 `METHOD_NOT_FOUND`。与 A2 合并只有 `CHANGELOG.md` 冲突（两条都保留）。报告：powerstation `orgfs-min/reports/a1-report.md` |
| 2026-11-25 | `request_catalog/migration.py` 登记项变更（A1）| 已删 | `MIGRATED_METHODS` 去掉 `'orgfs.read_at'`、`'orgfs.stat_at'` 两行（owner 2026-11-25 授权：由已定决定直接推出的 orgfs 登记项直接删并在此记录）|
| 2026-11-25 | A2 删除 `open_doc` | 完成，已合入（`b2cb944341`）| 提交 `4ac4cc468c`，15 个文件，-1287 行。agent 实测并经集成者复核：`tests/daemon/orgfs -n 4` 766 passed、3 xfailed、0 failed（比基线少的 18 条 = 删 19 条结构化文档专属测试、加 1 条）；`tests/daemon/organizations tests/daemon/cells tests/tooling/architecture` 2137 passed、1 skipped；ruff、结构门、边界门、行内注释门 PASS。不牵涉任何生成物。未运行：全量 pytest、跨实现契约。报告：powerstation `orgfs-min/reports/a2-report.md` |
| 2026-11-25 | 阶段 B 设计草稿 | 草稿完成，codex `gpt-6.2-sol` 审查中 | `orgfs-minimal-design-2026-11-25.md` §1–§8 |
| 2026-11-25 | A3b 删除 `history`（后端与 CLI）| 完成，已合入（`a05528a4ad`）| 4 个提交，49 个文件，+182 / -369。删 `orgfs.history`、MCP `orgfs_history`、CLI `org fs history`、`OrgFs.history`、`HistoryEntry`、`_snapshot_info`、线路 schema `HISTORY_ENTRY` / `HISTORY_NODE`。改了 3 个 skill 文件与 Squire 模板（PR 需 `Skill-Impact: updated`）。agent 实测：新测试先 FAIL 后 PASS；`tests/daemon/orgfs` 765 passed / 3 xfailed（比起点少 1 条 = 删掉的 `test_history_carries_the_uri`）；第二批（含 `tests/shell` 全部）7483 passed，失败 = 6 条生成物未重生 + 14 条起点上同样失败。未证实：`test_atomic_birth` 换成变更事件作探针后灵敏度是否不变（agent 试的变异新旧写法都抓不到）。`restore` 签名未动。报告：powerstation `orgfs-min/reports/a3b-report.md` |
| 2026-11-25 | A3g 删除 GUI 历史面板与 `readAt` | 完成，已合入（`c5b7fd9ed1`）| 提交 `b162ab5910`、`e7d98511bc`，20 个文件，+77 / -1115。GUI 手写代码不再引用 `orgfs.history`、`read_at`、`stat_at`。agent 实测（node v22.23.4，仓库要求 >=24）：`tsc --noEmit`、模块边界、请求路径门、mock 来源门、unavailable-copy 基线门 PASS；改过的 6 个测试文件 PASS；GUI 全量与起点逐条对比，新增失败只有 1 条账本精确性测试，集成者删掉账本行后 PASS。未运行 / BLOCKED：真实 daemon 加浏览器验收、`test:browser`、生成物重生后的类型检查。报告：powerstation `orgfs-min/reports/a3g-report.md` |
| 2026-11-25 | GUI 账本登记项变更（A3g）| 已删 | `gui/tests/architecture/unavailable-copy.allow.ts` 的 `FilesSection.tsx` 组去掉 `"该版本内容读取失败，正文不可用。"` 一行（`4981ccea59`）|
| 2026-11-25 | **阶段 D 必做项：GUI 文件夹恢复** | 待办 | 删掉历史后 GUI 无法区分“随文件夹一起删除”与“更早已删除”的子项，A3g 让文件夹恢复暂时返回 `ORG_FOLDER_RESTORE_UNSUPPORTED`、不发请求（文件恢复不受影响）。设计 §6 的新 `restore(node, recursive=True)` 就位后，GUI 改为直接调用它；合回 dev 之前必须完成。同时处理：`ORG_COPY.historyAnomalyReason` 文案里的“和历史”、`orgfs.listTrash` 无人使用的 `limit` 参数 |
| 2026-11-25 | A4 `watch_meta` 收为内部 | 完成，已合入（`cc480c5854`）| 提交 `c67027ca9f`，4 个文件，+24 / -7。`OrgFs` 去掉 `watch_meta`，包内 Protocol `_SpaceMetaWatcher` 放在 `composition/vocabulary.py`。agent 实测：新测试先 FAIL 后 PASS；`tests/daemon/orgfs` 785 passed / 3 xfailed；`tests/tooling/architecture` 1536 passed；四个门 PASS。评估结论“包外无调用方”经全仓 grep 核实成立。与 A1、A2 在 `api.py` 冲突（两边各删一个方法，结果两个都删）。注意 `composition/directory.py` 已 993 行，离 1000 行门只差 7 行 |
| 2026-11-25 | C7 测试分拣（只读）| 完成，已合入（`6dbc02586f`）| `orgfs-test-triage-2026-11-25.md`，1386 条测试：契约-保留 680、契约-需改写 334、journal 专属-删除 351、待定 21。只做了 collect-only，没有运行测试；约 43.6k 行只把 OrgFS 当后端的测试未读（清单 §6）。要点：没有现成的参数化接缝（C4 需新建 `contracts/conftest.py` 工厂）；远端准入与受保护目录规则的 68 条用例只在信封层有测试（C2 需先做注入伪造 update 的工具）；`restore(node, version)` 现为按版本恢复；清单 §5.3 列出 20 项只有 store 级保证的用户可见行为，分给 C1、C2、C3 作验收条件 |
| 2026-11-25 | 集成门：A1+A2 合并后（`1ba7deae6b`）| orgfs PASS；第二批仅预期失败 | `tests/daemon/orgfs -n 4`：766 passed、3 xfailed、0 failed。`tests/daemon/ipc`、`channels/mcp`、`contract_mock`、`user_agent`、`organizations`、`cells`、`tests/shell/request_http`、`tests/tooling/architecture`：4454 passed、5 failed、1 skipped；5 条即 §10.1 第 10 步列出的 `test_request_contract_gate.py`（openapi 未重生）。结构门、边界门 PASS。ruff FAIL 1 处：两个分支各删掉 `decode_state_vector` 的一个使用者，合并后该 import 无人使用，已由集成者修复（`e07ba52146`）。报告：powerstation `orgfs-min/reports/gate-a1a2/` |
| 2026-11-25 | 集成分支 `6dbc02586f`（A1、A2、A4、C7 均已合入）| 快速复查 PASS | ruff、结构门、边界门 PASS；`tests/daemon/orgfs/contracts`、`authority/test_orgfs_actor_boundary.py`、`tests/daemon/ipc/retired_work` 74 passed。合入 A4 后的 orgfs 全目录与全量 pytest：未运行，留到 A3b、A3g、A5 合入并重生生成物之后一起跑 |
| 2026-11-25 | A5 删除 `fs space serve` 与 serve 入口 | 完成，已合入 | 2 个提交，47 个文件，+236 / -1809。删 `orgfs.serve`、CLI `org fs space serve`、`OrgFs.serve`、`_ServeSpace` / `_serve_sync` / `_create_or_reuse_replica`、公告里的 `resident` 角色、`_post_rebuild_effects` 的 resident 一半。agent 实测：新测试先 FAIL 后 PASS；`tests/daemon/orgfs` 741 passed / 3 xfailed（减少的 25 条 = 删掉的 25 条）；第二批 7560 passed，失败 = 6 条生成物未重生 + 17 条起点上同样失败。**没删的**：`storage/replica*` 与 mesh / ingress / queries / recovery / lifecycle / purge 中的 replica 分支，它们牵动 purge、blob 引用账本、`durable`、`status`；删 serve 后这些分支已无法进入，残留清单在报告 §5，留给阶段 E。可见行为变化：本机不再出现在自己的 `holders_online` 里；`orgfs.sync.post-rebuild` 日志去掉 `residentSpaces`、`onlineHolderPairs`。`test_orgfs_purge_twonode.py` 末段一条断言变弱（原来断言 replica 服务路径对退役文档报 barrier）。报告：powerstation `orgfs-min/reports/a5-report.md` |
| 2026-11-25 | 登记项变更（A3b、A5）| 已删 | `MIGRATED_METHODS` 去掉 `'orgfs.history'`、`'orgfs.serve'` |
| 2026-11-25 | 合并交互（A1、A3b、A5）| 已由集成者修复 | (1) `tests/shell/compatibility/test_unreachable_surface_retirement.py` 钉着 `mcp-tools: PASS 33/33`，A1 漏改（A1 的批次没跑 `tests/shell` 全部），现为 30/30。(2) `example_integration/test_coverage.py` 的 `len(TARGETS)`：A3b 与 A5 都把 37 改成 36，合并无冲突但正确值是 35，靠合并后的焦点测试发现（`f00c6c7c4a`）。(3) `api.py`、`test_ipc_migration.py`、`test_legacy_permit_retirement.py`、`CHANGELOG.md` 的冲突均为两边各删一项 |
| 2026-11-25 | 集成分支 `f00c6c7c4a`（阶段 A 全部合入，生成物未重生）| 焦点测试：仅预期失败 | `tests/daemon/ipc/contracts`、`ipc/retired_work`、`contract_mock`、`channels/mcp/contracts`、`tests/shell/compatibility`、`tests/tooling/architecture/ipc`、`user_agent/admin`、`tests/shell/request_http`、`tests/shell/cli/presentation`、`forwarding/membership/budgets`：修复后 2324 passed、5 failed（均为 `test_request_contract_gate.py`，等 openapi 重生）。ruff PASS。生成物重生与全量：运行中（powerstation `orgfs-min/reports/regen-a/`）|
| 2026-11-25 | 阶段 B 设计定稿 | 完成（`5219cd88ce`）| `orgfs-minimal-design-2026-11-25.md` 第 2 版；codex `gpt-6.2-sol` 审查报告 `orgfs-minimal-design-review-2026-11-25.md`，8 条“必须改”与 7 条“应该改”全部接受，处理见设计 §8。实质变化：准入改为“暂存副本上基线加差量”（空探针读不出已有键的替换与删除，审查用 pycrdt 实测）；缺前驱的 update 不接受、不落盘、立即回拉；所有写入经原子的提交批次；内容文件只在观察到 tree 行消失时删除；tree 行新增持久的 `author` 字段；安全保证的表述改为“对如实自报身份的节点与现状等价” |
| 2026-11-25 | 阶段 A 全量（集成分支 `756401dc4c`，生成物已重生）| 结果待对照 | 生成物重生：`dump_daemon_openapi.py --check`、`client:reference:check`、`check_cli_help_inventory.py`、golden / CLI surface / request-contract-gate 测试 73 passed、GUI `tsc --noEmit` PASS；`check_daemon_contract_diff.py --base-ref origin/dev` 报 4 个方法、4 个路径的 BREAKING 删除（预期，PR 正文已写 `Contract-Impact`）。全量 `uv run --with libcst==1.9.0 pytest tests/ -n 4`：30643 passed、38 failed、157 skipped、5 xfailed、2 xpassed，1802 s；ruff、结构门、边界门 PASS。`./contract/run-all.sh --isolated-only`：FAIL 11/14（3 条“isolated … must use DAEMON_UNAVAILABLE”）。基线对照（同样的 38 条在未改动的 `f3cd47acdb` 工作树上重跑，`orgfs-min/reports/full-phase-a/base-compare.txt`）：34 条在基线上同样失败（服务托管 systemd / launchd、pi、codex 等，与本工作无关）；4 条只在集成分支失败，其中 2 条是本工作引入并已修复：(1) `test_native_resident_inventory.py::test_daemon_inventory_matches_live_tree`，A2 删除的 `structured.py` 未登记为历史行，已登记；新模块的登记行随各包合入追加；(2) `tests/tooling/release/publish/github/test_snapshot.py` 的内网标记门，本表一行里写了一台构建机的主机名（命中私有标记表），已改写。另 2 条（`test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load`，耗时占比 0.67 < 0.7；`test_authority_client.py::test_not_sent_after_unknown_does_not_erase_unknown`，`AUTHORITY_UNAVAILABLE`）疑为满载下的时序抖动。基线上的 `./contract/run-all.sh --isolated-only` 同样是 FAIL 11/14、同样三条，与本工作无关 |
| 2026-11-25 | 阶段 A 遗留项复跑（集成分支 `a74d0ba6d9`）| PASS | `tests/daemon/orgfs/contracts`、`tests/daemon/ipc/cost/test_ipc_cost_counters.py`、`tests/identity/devices/tailnet/test_authority_client.py`、`tests/tooling/release/publish/github/test_snapshot.py`、`tests/integration/native_harnesses`：893 passed、148 xfailed、0 failed（`orgfs-min/reports/focus-c4-flakes.txt`）。两条疑似抖动的测试单独运行通过；内网标记门与常驻清单通过。**阶段 A 结束**：全量里没有由本工作引入而未修复的失败 |
| 2026-11-25 | C6 规模测试 | 首次交付已复核，收尾中 | 1 个提交，仅 `tests/daemon/orgfs/scaling_state/`；以文件系统操作的次数与字节数为断言，6 种缺陷注入都能让对应测试变红。实测发现无条件加提交时钟使 state 每提交增长约 13–17 字节（设计 §3.3 已改）。收尾：接入 C1 的真实实现并重跑 2.5 万文档合成空间 |
| 2026-11-25 | C1 state store | 完成，已合入（`f83c47191e`）| 3 个提交，新包 `orgfs/state/`（7 个文件）。agent 实测：58 个崩溃点逐一注入加 6 例子进程真实退出，断言整批原子；10 个变异全部被测出；`tests/daemon/orgfs/state` 126 passed；合并后 GC 实测（50 次覆盖的 4 KB 文本：delta 206,330 字节 → state 4,132 字节）。未验证：掉电级崩溃、多进程同时打开、macOS。6 个设计问题已裁定（设计 §8）。报告：`orgfs-min/reports/c1-report.md` |
| 2026-11-25 | C5 调用方迁移 | 完成，已合入（`850e74713a`）| 6 个提交，10 个文件，+684 / -34。leave 请求带 `memberAddedAt`，`leave_request_covers_current_add` 不再调用 `document_covers_member_add`；`parkedCommits` 的读取集中到一处（字段未删，留到阶段 D）；`tests/daemon/org/network/**` 的分拣文档。agent 实测：新测试先 14 failed 后 17 passed；`tests/daemon/org`、`organizations`、`org_membership`、`cells`、`orgfs` 五个目录 1977 passed、0 failed（起点 1957，多的 20 条是新测试）。未运行：全量、真实多节点 leave 流程。契约影响：leave 请求记录新增必需字段，旧版本提出的请求不再被自动处理（`contract/org-network/README.md` 已写）。报告：`orgfs-min/reports/c5-report.md` |
| 2026-11-25 | C3 sync | 完成，已合入（`9ce104fe73`）| 9 个提交，`orgfs/sync/` 与 `KeySpace` 的三个新方法。收尾后 `tests/daemon/orgfs/sync` 132 passed。agent 用确定性复现与 80 次随机故障测试（4 节点、乱序 / 重复 / 丢帧 / 分区）证明设计 §4.2 第 1 版的 frontier 修正不收敛（26–31 次分叉），改进规则 0 次不收敛、三节点并发编辑收敛且终止；设计已改。合入后焦点测试（sync、state、membership、transport、native_harnesses）1141 passed。报告：`orgfs-min/reports/c3-report.md` |
| 2026-11-25 | C6 规模测试 | 完成，已合入（`1f0142114a`）| 2 个提交，`tests/daemon/orgfs/scaling_state/`，66 passed（参考实现与 C1 真实实现两个参数各 33 条）。真实实现上没有性质不满足；4 处断言因测试自身过严而放宽，逐条记录在报告 §10。2.5 万文档合成空间（tree 5 万行、14 MB）在真实实现上：打开 5 ms（读 14 MB、0 写、0 fsync、不碰任何 `docs/` 文件），tree 载入 21 ms，单文档提交 10 ms（1 次 fsync），tree 合并 13–26 ms；只记录、不作断言。对照：生产上同规模空间旧实现首启 155–592 s（#3142）。C1 线索：store 在内存里保留各文档的 delta 扫描结果，随触及的文档数增长，阶段 D 给它加上界。报告：`orgfs-min/reports/c6-report.md` |
| 2026-11-25 | 登记项变更（native resident 清单）| 已加 | 追加 `orgfs/sync/*` 7 行 |
| 2026-11-25 | **阶段 C 结束**；集成门（集成分支 `1f0142114a`）| orgfs PASS；第二批 1 条 FAIL（已解释，待切换时处理）| `tests/daemon/orgfs -n 4`：1338 passed、150 xfailed、0 failed（xfail 中 147 条是契约的 `state` 参数与按设计条款标注的旧实现差异）。`tests/daemon/org`、`organizations`、`org_membership`、`cells`、`transport`、`tests/integration/native_harnesses`、`tests/tooling/architecture`、`tests/tooling/release`：4466 passed、1 failed。ruff、结构门、边界门 PASS。报告：`orgfs-min/reports/gate-phase-c/` |
| 2026-11-25 | **待 owner 过目：网络可达性基线（合回 dev 之前）** | 集成分支上保持 FAIL 至包 S | `tests/tooling/architecture/network/test_network_reachability.py::test_checked_in_baseline_keeps_repository_gate_green`：`scripts/ci/check_network_reachability.py` 报 6 条新的“从 IPC 可达网络调用”：`orgfs.sync.space.StateSpaceSync.announce`、`orgfs.sync.pull.SyncPullMixin._round`、`orgfs.sync.blobs.SyncBlobMixin._fetch_blob` 各自到 `TransportSessionAuthority` 与 `ZenohTransport` 的 `put` / `query`。基线 `scripts/ci/network-reachability-baseline.json` 的 31 条已知项里有 6 条是旧 `orgfs.mesh` / `orgfs.network` 的同类调用。两套实现并存期间不改基线（新增基线条目不在集成者的授权范围内）；包 S 删除旧 mesh 后旧 6 条失效，届时以新 6 条替换，净变化为零，替换的确切 diff 在合回 dev 前交 owner 确认 |
| 2026-11-25 | 阶段 D 施工图 | 完成，已合入（`6ac79dfbf0`）并审定 | `orgfs-minimal-phase-d-plan-2026-11-25.md`（527 行）：接缝清单、协调者落点、三种并存策略的比较、13 个工作包及文件所有权、必须保住的行为与对应契约、19 条风险、13 个真实多节点场景。未跑测试；`network/`、`storage/store`、请求面与 GUI 只 grep 未读。集成者的裁定在设计 §9：采用双宿主并存；R1–R19 逐条决定 |
| 2026-11-25 | D0 facade 钩子重构 | 完成，已合入（`669a0bb9b6`）| 1 个提交，7 个文件，+521 / -346；`projection.py` 持平于 961 行、`directory.py` 976 → 962。agent 实测：重构前后用 junit 逐条比较，`tests/daemon/orgfs` 1488 条结果完全相同（另加 5 条新钩子测试），`tests/daemon/org`、`organizations`、`org_membership`、`cells`、`provider_keys`、`tests/integration/orgfs`、`actor_preservation` 2342 条完全相同；四个门 PASS。全量未运行。比施工图多出的钩子：`_new_content_doc` / `_put_content_doc` / `_drop_content_doc`，`_stamp_versions` 多一个 `changed` 参数。报告：`orgfs-min/reports/d0-report.md` |
| 2026-11-25 | P0 阶段 D 前置 | 完成，已合入（`c1dc0eb9d4`）| 4 个提交，22 个文件，+748 / -195：登记 `ORGFS_LAYOUT_UNSUPPORTED`；`BlobStore.unpin`；提交 helper 改为 `local_commit`（向量未前进才加时钟）；成员行 `removedAt` 与准入规则；tree 行 `via`；`outcome_kind` 按类型识别。每项都有先失败的记录（`orgfs-min/reports/p0/red-*.txt`）。生成物待重生：openapi 与 GUI 生成类型里的错误码枚举（与 D4a 一起重生）。报告：`orgfs-min/reports/p0-report.md` |
| 2026-11-25 | 登记项变更（错误码表，P0）| 已加 | `src/hyprial/error/codes.py` 与 kernel 再导出新增 `ORGFS_LAYOUT_UNSUPPORTED`；`tests/daemon/ipc/error/test_ipc_error_code_registry.py` 的 `FROZEN_CODES` 同步。属新实现引入的新错误码（设计 §2.7），合回 dev 前随其他登记项一并交 owner 过目 |
| 2026-11-25 | D4a 删除内容清除等公共面 | 完成，已合入；生成物已重生（`b4fe6f5d73`）| 2 个提交，47 个文件，+211 / -1037。删 `orgfs.purge_plan` / `purge` / `purge_status` / `unban` 的请求方法、MCP `orgfs_purge_status`、CLI `org fs purge plan|status|unban`、`OrgFs` 上的六个方法（含 `parked_commits`、`document_covers_member_add`）、`contract/orgfs/content-purge.md`、GUI 适配层的内容清除入口；旧宿主类上的实现保留到包 S。agent 实测：新测试 6 条先 FAIL 后 PASS；`tests/daemon/orgfs` 1335 passed / 0 failed；第二批与起点逐条比较，多出的失败只有 6 条生成物未重生加 2 条计划 §10 的既有失败；GUI `tsc`、模块边界、请求路径门、mock 来源门 PASS。生成物重生后各 `--check`、golden / CLI surface / request-contract-gate 73 passed、GUI `tsc` PASS；方法数 175 → 167。报告：`orgfs-min/reports/d4a-report.md` |
| 2026-11-25 | 集成分支 `b4fe6f5d73`（P0、D0、D4a 合入，生成物已重生）| PASS，除已知的可达性基线 | GUI 全量（node v22.23.4）：239 个文件、2990 passed、1 skipped、4 todo、0 failed；`tsc --noEmit`、模块边界、请求路径门、mock 来源门 PASS（`orgfs-min/reports/gui-full-d4a/`）。后端 13 个目录（`tests/daemon/orgfs`、`ipc`、`channels/mcp`、`contract_mock`、`user_agent`、`org`、`golden`、`tests/shell/cli/surface`、`cli/presentation`、`compatibility`、`request_http`、`tests/tooling/architecture`、`tests/integration/native_harnesses`）：6383 passed、151 xfailed、1 failed，失败即上面登记的网络可达性基线（`orgfs-min/reports/focus-d4a.txt`）。全量 pytest：未运行，留到 D2 合入后 |
| 2026-11-25 | 登记项变更（D4a）| 已删 | `MIGRATED_METHODS` 去掉 `'orgfs.purge'`、`'orgfs.purge_plan'`、`'orgfs.purge_status'`、`'orgfs.unban'` |
| 2026-11-25 | **阶段 A 的遗漏（集成者）** | 已修复 | 阶段 A 重生生成物后没有跑 GUI 全量测试，`gui/tests/features/settings/contract-gap.test.ts` 钉着的接口计数（130 / 123 / 175）从那时起就是过时的，由 D4a 的起点对照发现。现改为 122 / 115 / 167 并通过。此后每次重生生成物都跑 GUI 全量（脚本 `orgfs-min/bin/guifull.sh`）|
| 2026-11-25 | D1 提交协调者与 `StateOrgFs` | 完成，已合入（`ba0d50ed81`）| 3 个提交，16 个文件，+3923 / -24：`orgfs/document/coordinator/`（7 个文件）与 62 条单元测试；共用代码只动了一处（`content.py` 新增 `_base_text` 钩子，旧行为逐字不变）。agent 实测：契约 `state` 参数 140 条中 99 PASS、41 条因 `make_runtime` / `connect` 尚未接入而 xfail（属 D2）、0 FAIL；`legacy` 参数逐条不变。`tests/daemon/orgfs` 与起点逐条比较，变化只有那 99 条由 xfail 变 PASS 加 62 条新用例；`tests/daemon/org` 等六个目录 2185 条逐条相同。变异测试：去掉 frontier 修正则 5 条全红；内容暂存不换实例则谎报向量的写入绕过准入。四个门 PASS。未运行：全量 pytest、真实多节点、2.5 万文档下协调者的耗时。7 个设计问题的裁定在设计 §9.1。报告：`orgfs-min/reports/d1-report.md` |
| 2026-11-25 | 登记项变更（native resident 清单）| 已加 | 追加 `orgfs/document/coordinator/*` 7 行 |
| 2026-11-25 | 阶段 D：D2（`StateOrgFsRuntime`、join、演练开关与切换演练）| 完成，已合入（`e60c2fdfbc`，含 5 行新模块登记）| 契约 `state` 参数 140 条：138 PASS、1 条预期失败（降级写入）、**1 条 FAIL**（分区期间被移除的成员得不到移除通知，设计缺口，裁定见设计 §9.2，由包 F1 修）；`legacy` 参数与起点逐条相同；演练开关关闭时 `tests/daemon/orgfs` 与消费方目录逐条与起点相同（D2 实测）。切换演练（开关打开）新增红 317 条：journal 专属 105、需改写 155、待定 9、分拣为保留却变红 14（新实现缺陷 1 条已修，`writer_attributions` 6 条归包 O，7 条需改写）、清单外 34（`legacy` 参数假象 18、cell_actor 8 归包 O、其余归 T）。清单 `orgfs-min/reports/d2-rehearsal.md`。未测：真实 transport、进程被杀后的恢复、2.5 万文档下 join 与核对耗时。合入后的门禁结果见下一行 |
| 2026-11-25 | D2 合入后的门禁（集成者实测，`68c494687f`）| PASS，1 条已知 FAIL | ruff、结构门、边界门 PASS；`tests/daemon/orgfs`：1590 passed、11 xfailed、1 failed（契约 removed-member[state]，待 F1）；`tests/integration/{native_harnesses,actor_preservation/orgfs,orgfs}`、`tests/daemon/{org,organizations,org_membership,cells,provider_keys}`、`tests/shell/login/device/test_boundaries.py`：2854 passed、1 xpassed、0 failed。证据 `orgfs-min/reports/post-d2/`。全量 pytest 未运行（留到 T 包合入后）|
| 2026-11-25 | 阶段 D：T1 ∥ T2 ∥ T3（旧测试改写到新实现）、F1（节点门在生产配置下的查证、被移除成员得知移除、join 崩溃、`synced` 时间测量）| 进行中 | powerstation `orgfs-min/{t1,t2,t3,f1}`；分支 `test/orgfs-admission-on-state`、`test/orgfs-sync-on-state`、`test/orgfs-facade-on-state`、`fix/orgfs-state-followups` |
| 2026-11-25 | 阶段 D：V（真实多节点验证，第一阶段：隔离证明、旧实现基线、新实现 13 项）与 R1 演练 | 进行中 | 三个测试节点各用隔离 home，先证明对端列表只含测试节点再做 org 操作；R1 只克隆生产 home 的 OrgFS 数据目录，不带身份与凭据。做不到隔离即 BLOCKED |
| 2026-11-25 | 提交身份 | 已改 | Forgejo 的 committer-guard（现为告警模式）要求作者与提交者邮箱属于注册用户、agent 身份写在 `Co-Authored-By`。此前本线的提交用了 agent 邮箱作者身份；自 `68c494687f` 之后改用 owner 的注册身份加 trailer。**需要 owner 过目**：集成分支上已有的提交在强制模式下会被拒，合入 dev 的方式（squash 或保持告警模式）由 owner 定 |
| 2026-11-25 | 阶段 D：T3（facade、存储、本地清除与消费方测试改写）| 完成，已合入（`0ed4d70440`）| 只改测试：改写 22 条、删除 49 条（逐条对应的契约见 `orgfs-min/reports/t3-report.md` §2–§3）。开关打开后该包目录剩余红 12 条：journal 专属 4、包 O 6、**新实现缺陷复现 2**（并发互相移动后两个目录在目录列表与 trash 中都不可见；准入层判定正确，新 facade 的树投影未断环；交 F1 修，修好前这两条在集成分支上为红）|
| 2026-11-25 | dev 上与本线无关的既有问题（查证与真实节点验证中发现，**已报 owner，本线不修**）| 记录 | (1) `b432a31fff`（2026-11-22）把“信任 tailnet 成员”改为恒假后，旧 OrgFS 的节点门对任何节点都解析不出归属，三类空间都不能 join 与同步（F1 进程内双节点实测，`orgfs-min/reports/f1-gate.md`）。(2) 真实 daemon 上 `org join` 必然以 `IDENTITY_CONFLICT` 失败：join 要求已验证的 OIDC claims，而 `src/` 里没有任何代码给真实 daemon 提供它（V 三节点实测加读码）。(3) 请求注册表把“已声明错误码但带 data”的错误整体改写为 `DAEMON_ERROR`，原始错误不进日志。影响本线：真实多节点验证里 org join 之后的各项只能在测试钩子注入身份的前提下进行，结果一律标注“身份门已注入”；节点门等 OrgFS 路径不绕行 |
| 2026-11-25 | 阶段 D：T1（成员与准入类测试改写）、T2（同步、presence、blob 类测试改写）| 完成，已合入（`714e7721e9`、`90ddce6332`）| 只改测试。T1：63 条中 53 改写通过、4 删除、1 改名、**6 条留红的缺陷复现**（成员可改空间根行；接收方不强制受保护路径的确定性 id——设计 §3.6 漏掉的两条旧规则，已在设计 §9.2 补回，包 F2 实现）。T2：85 条全部处理（其中 13 条是演练未列出的），待定 21 条逐条定去留，**1 条缺陷复现**（join 失败的错误消息丢了说明，交 F1）；裁定远端新建而正文未到的文档 `stat` 直接报 `pending`。报告 `orgfs-min/reports/{t1,t2}-report.md`。集成分支当前已知红 10 条：契约 removed-member 1、并发互移 2、join 消息 1（F1），准入 6（F2）|
| 2026-11-25 | R1 演练（生产 `state/orgfs` 的 APFS 克隆，旧版导出 → 新版导入）| **普通空间 PASS**；ACL 空间不能经 `fs space create` 重建 | 方法：进程内构造 daemon 应用、不起进程、不监听、沙箱禁网且禁读写生产 home；隔离 home 只含 OrgFS 数据，无身份与凭据。28 个空间（普通 20、ACL 7、org 目录 1），journal 合计 19.7 GB。旧版打开全部空间 340.5 s，导出 26 536 个文件（1007 MB）；244 个 blob 导不出（本地内容已清除 238、无持有方 4、超过 64 MiB 2）。新版导入 20 个普通空间 26 529 个文件 0 失败，新进程重开 0.9 s，逐文件哈希 0 不符、路径与目录集合一致（`fs import` 与文本走 `fs write` 两种做法都验证）。导入后最大空间磁盘 16 MB（原 17.6 GB）。**不随迁移带过去的**：历史、trash、成员表（需重新邀请）、作者与修改时间、空间 id 与节点 id、checkout；文本文档经 `fs import` 会变成不可编辑的 blob，迁移指南须让文本走 `fs write`；7 个 ACL 空间由 org 流程生成，不经此路迁移（与 org 目录空间不迁移一致）。未测：经 CLI 的逐条耗时、成员重建、checkout 重建。报告与证据在 hq 的 `wt/orgfs-min-hq/reports/r1-report.md` 与 `r1/evidence/`；克隆与导出内容（含正文）仍留在 `wt/orgfs-min-hq/r1/`，阶段 E 写完迁移指南后删除 |
| 2026-11-25 | 阶段 D：V 第一轮（三个真实节点，隔离 home，自建测试中继；org join 的身份门经测试钩子注入，其余路径未绕行）| **切换前提的 6 项（施工图 §7 第 1、2、4、7、8、12 项）无一 PASS**，不满足进入包 S 的条件 | 隔离证明 PASS（对端列表只含测试节点，无 adapter）。**节点门在真实 org 里对每个对端都拒绝**（新旧实现相同，归属解析为空），成员之间没有任何 OrgFS 数据同步；第 2、3、10 项及第 4、5、7、11 项的跨节点部分因此 BLOCKED，等规则 G（设计 §9.2，包 F1）合入后重跑。单节点实测 PASS：`kill -9` 三轮已确认写入零丢失、无半截文件、`batch-recovered` 重做；升级时旧布局目录字节不变、只拒绝该空间、daemon 照常启动；2.5 万文档空间重启 3.4–4.1 s 后立即可读，启动不读 `docs/`（strace）。新实现的问题：(a) `orgfs.status` 线上恒为 `DAEMON_ERROR`（结果缺旧 schema 的必填字段）；(b) `ORGFS_LAYOUT_UNSUPPORTED` 未在任何 `orgfs.*` 方法声明，线上变成 `DAEMON_ERROR`——(a)(b) 并入包 S（切换后产品必须可用）；(c) join 只报最后一个持有方的错误（F1）；(d) **新建文档的速率随文档数下降**，417 → 63 docs/s（1k → 25k），与“写入成本与空间大小无关”的目标不符（包 F3 定位并修）。未运行：第 9 项（永久删除无线上入口，待 D4b）、第 13 项（GUI，待包 G）。旧实现对照：约 3.6k 文档时写入降到 1–2 docs/s、每篇约 100 KB，2.5 万未能完成；部分 join 后 daemon 无法再启动。报告 hq `wt/orgfs-min-hq/reports/v-report.md`，证据 `reports/v/` |
| 2026-11-25 | 阶段 D：F2（补回根行与确定性 id 的准入规则）、F3（写入成本随文档数增长）| 进行中 | powerstation `orgfs-min/{f2,f3}`；分支 `fix/orgfs-admission-root-and-ids`、`perf/orgfs-state-write-cost` |
| 2026-11-25 | T 包合入后的全量（集成者实测，`90ddce6332`）| 无新增的未知失败 | 全量 pytest：31 406 passed、46 failed、1 个收集错误。46 = 已知 10（F1、F2 在修）+ 网络可达性门 1 + 阶段 A 基线里的本机既有失败 35。收集错误：`document/test_purge_rollback.py` 导入了 T3 删除的模块，分拣为 journal 专属（内容清除），已删（`6b60e2754b`）。ruff、结构门、边界门 PASS；`contract/run-all.sh --isolated-only` 11/14（与起点相同）。证据 `orgfs-min/reports/post-t/` |
| 2026-11-25 | 阶段 D：F1（两轮）| 完成，已合入（`68c775377e`、`46c4b6c50e`）| 节点门规则 G（引导来源用 tailnet 名册：org cell 名册在成员侧不存在、设备键不是节点名、没有生产路径写入加入者设备）；被移除成员只得到 meta（契约由 FAIL 转 PASS）；join 中途崩溃实测为绕过准入，加 `join.pending` 标记；并发互移断环；join 错误消息与“报最有信息量的错误”；`stat` 对远端新建未到的文档报 `pending`；核对有积压时连续分批（2.5 万文档 98 批 2.37 s，原节奏约 98 分钟）；反熵 tick 对未被观察覆盖的空间补拉（本地写后一个 tick 内 `synced` 回真）。记为已知问题：blob 通道不识别请求方（新旧相同，改它要动线协议）。未验证：真实 sidecar 上的 tailnet 名册形状（V 第二轮）。报告 `orgfs-min/reports/{f1-report,f1-gate}.md` |
| 2026-11-25 | 阶段 D：V 第二轮、包 S（切换与删除旧实现；并入 `orgfs.status` 线上 schema 与 `ORGFS_LAYOUT_UNSUPPORTED` 的声明）| 进行中 | S：powerstation `orgfs-min/s`，分支 `refactor/orgfs-switch-to-state`。**S 的合入以 V 第二轮切换前提各项通过为条件** |
| 2026-11-25 | 阶段 D：F3（写入成本）| 完成，已合入（`9726ab7cc8`、`4696dd9dc0`）| 原因：每次写入扫描或复制整张节点表与向量表（占 CPU 72%）。改为增量节点索引：新建一篇的 CPU 在 1k–25k 文档间持平约 1.3 ms（改前 20k 时 10.8 ms）；join 2.5 万文档空间逐篇收正文的 CPU 35.9 s → 17.8 s，每篇成本不随 n 增长；计数测试钉住。回归 815 passed、0 failed。只读测量：根目录递归 `listdir`（25 100 项）174 ms，与返回项数线性，不加分页；`pending_content` 每 128 篇扫一次节点表（2.5 万共约 0.8 s）未改。真实节点上的速率待 V 第三轮复测。报告 `orgfs-min/reports/{f3-profile,f3-report}.md` |
| 2026-11-25 | 阶段 D：V 第二轮（`68c775377e`，三个真实节点，身份门已注入）| 切换前提 5 项中 **4 项 PASS（第 1、2、4、7 项），第 8 项 FAIL** | 隔离 PASS（全程复核）。**规则 G 在真实 sidecar 上工作**：1580 次门决定、59 次拒绝，其中 52 次是正确的成员资格拒绝，7 次发生在新成员加入后名册修订传播的约 23 s 窗口内并自愈；两个来源从未矛盾。PASS：join 三种情形、双向互写（≤0.31 s，20 MiB 经 import/export 哈希一致）、停机期间的改动重启后追上、写入后 0.6 ms 被杀对端仍拿到正文、移除成员后分区写不进入其他节点。FAIL：第 8 项（leave 请求 owner 不处理、成员资格不撤；受保护目录内移动报裸 `not-a-member`）；第 3 项文本正确性（带 `baseVersion` 的三方合并回退损坏文本，2026-10-06 的存量代码，静默数据损坏）；第 11 项（2.5 万文档空间 join 返回成功但树为空，首次追赶失败后 9 分钟不重试）；第 10 项（A 分区时重启的 C 连不上 B，属传输层的对端发现，OrgFS 之外）。其余新实现问题：远端 blob 到达不产生 watch 事件；重启后核对节奏导致 `synced` 长时间为假（F1 第二轮已改，待复测）。OrgFS 之外、已报 owner：sidecar 停顿约 3 分钟后转发宕到 daemon 重启（`MEMBERSHIP_STORE_INVALID`）；重启节点只经 owner 学到对端；invite 返回 `ok:false USER_DELIVERY_TIMEOUT` 但成员已加入；IPC 请求超过 8 MiB 直接断开不回错误。**证据缺失**：第一轮的 9 个钩子日志在第二轮脱敏时被一个 shell 循环清空，原 home 已删无法再生，第一轮引用它们的结论只剩报告文字（`reports/v/S/LOST-round1-hook-logs.txt`）；第一轮证据里 base64 编码的地址未被脱敏，已全部重做并扫描为 0 命中。未覆盖：member agent 清单（测试 home 目录权限导致未发布）。报告 hq `wt/orgfs-min-hq/reports/v-report-2.md` |
| 2026-11-25 | 阶段 D：F4（真实节点第二轮的 OrgFS 缺陷）| 完成，已合入（`3068fb0e6a`）| 修复四项，均先红后绿：在线持有方目录领先时带退避地再拉取，blob 每块固定 10 s 超时；`base_version` 三方合并改为按行；受保护目录内移动报 `protected-path`；二进制文件取回后发一次 `modified`。裁定后补一项：`status.synced` 在目录落后时为 `False`（设计 §4.6 已改）。leave 查清不是 OrgFS 缺陷（owner 需确认），验收改为包含确认。里程碑批次（orgfs、org、startup、integration/orgfs）2110 passed、0 failed；补充批次 466 passed。留给负责人：同一行并发编辑后写者胜且不报错；leave 待确认没有日志（`org/**`）。报告 `orgfs-min/reports/f4-report.md` |
| 2026-11-25 | 阶段 D：S（切换默认装配并删除旧实现）第一步 | 已回报，未合入 | 新类改名接管 `LocalOrgFs` / `OrgFsRuntime`，演练开关删除；产品删 23 个文件（src 净约 −15 650 行），测试整删 55 个文件；`orgfs.status` 线路 schema 与 `ORGFS_LAYOUT_UNSUPPORTED` 声明先红后绿；网络可达性基线 −20 / +6（旧 orgfs 路径换成新 sync 的同类路径，其余因旧 sync 删除而不可达）。全量（修复提交之前）119 failed / 30 897 passed：本机既有 44、负载敏感 3、包 O 18、生成物未重生 11、已修 17、T1 缺陷复现 6（合入 F2 后应消失）、**切换相关未修 26**（contract mock 例子回放 21、会话双节点 5）。第二步：合入集成分支，例子回放把 `version` 当不透明值归一化，会话双节点探针改写，查证 user-key 路径在非 org 空间的保护。合入条件仍是 V 第三轮通过。报告 `orgfs-min/reports/s-report.md` |
| 2026-11-25 | **事故**：S 子代理误启动指向默认 home 的 daemon | 已停止，已报负责人 | 北京时间 09:15–09:19。子代理用脚本逐个导入 `tests/` 下的非测试模块，命令没有设置 `HYPRIAL_HOME`；`tests/integration/daemon_lifecycle/child_entry.py` 导入即执行 `hyprial daemon run --json`（读码判断，未复现），输出里出现一行飞书长连接已连上。约 4 分钟后被终止。集成者核对：无残留进程，测试机上原有的 daemon 仍在运行。对默认 home 的写入与事件分流未核实（本线不读该 home），交负责人检查。措施：公共任务说明新增硬规则——任何会运行 hyprial 代码的命令必须显式设置隔离的 `HYPRIAL_HOME`，禁止批量导入 `tests/` 模块。说明 `orgfs-min/reports/s/incident-lark.md` |
| 2026-11-25 | 阶段 D：S 第二步；V 第三轮、D4b、O 开工 | S 完成并推送候选（`refactor/orgfs-switch-to-state` @ `2fcad47905`），未合入；其余进行中 | S 合入 F1–F4 无冲突；例子回放把 `version` 按出现顺序绑定为占位符后 369 passed；会话双节点探针改用公开面，8 passed、1 条 strict xfail（会话读取方 `_ABSENT_CODES` 写的是不存在的码，`content-pending` 被当成记录不可用，交包 O）。查证：`directory/user-keys/**` 只在 org 目录空间受保护，旧实现经产品路径也是如此，且没有代码从 OrgFS 读该路径，可接受。全量 30 997 passed、68 failed：本机既有 35、负载敏感 4（单跑 PASS）、包 O 18、生成物未重生 11；切换相关与 T1 缺陷复现均为 0。V 第三轮在候选提交上运行；D4b（`feat/orgfs-surface-minimal`）与 O（`refactor/org-callers-state-status`）基于候选提交并行 |
| 2026-11-25 | （历史行）C2、C3 首次交付 | — | C2：6 个提交，`orgfs/membership/`，9 个设计问题（5 个有实验或测试复现）。C3：6 个提交，`orgfs/sync/`，92 passed、1 条 strict xfail（并发编辑同一文档在丢广播后永久分叉，设计缺陷），12 个设计问题。裁定全部写入设计 §8“阶段 C 中的修订”，两个 agent 正按修订后的设计做收尾提交 |
| 2026-11-25 | 登记项变更（native resident 清单）| 已加 | `resident_metadata/native_origins_i_o.py` 末尾追加 `orgfs/state/*`、`orgfs/membership/*` 各 7 行（`resident`、`new-module`）；`HISTORICAL_ROWS` 加 `daemon/impl/orgfs/document/structured.py`。脚本 powerstation `orgfs-min/bin/ledger.py` |
| 2026-11-25 | C2 成员与准入 | 完成，已合入 | 7 个提交，`orgfs/membership/`。收尾后 `tests/daemon/orgfs/membership` 158 passed；谎报向量的缺前驱 update 经一次“换实例”后等同于从未被接受，有对照测试证明不换实例会绕过准入。报告：`orgfs-min/reports/c2-report.md` |
| 2026-11-25 | C4 facade 契约测试 | 完成，合入中 | 6 个提交，25 个文件全部在 `tests/daemon/orgfs/contracts/`。140 条参数化契约：`legacy` 133 PASS + 7 条按设计条款的 strict xfail；`state` 140 条全部 strict xfail（`NotImplementedError`，阶段 D 接入后自动变红）。连跑 3 次结果一致。未做：3 节点与丢帧（归 C3）、伪造 update 的远端准入（归 C2）。报告：`orgfs-min/reports/c4-report.md` |
| 2026-11-25 | 阶段 C 派工 | 完成 | powerstation `orgfs-min/c1`…`c6`，分支 `feat/orgfs-state-store`、`feat/orgfs-membership-admission`、`feat/orgfs-state-sync`、`test/orgfs-facade-contracts`、`refactor/orgfs-callers-current-state`、`test/orgfs-state-scaling`；简报 `orgfs-min/c<n>-brief.md` |

## 10. 环境注意事项（已踩过的坑）

- **执行环境（2026-11-25 起）**：实现与测试在 [redacted] 的独立 clone
  `~/Workspace/HyprialOS/orgfs-min/`（`repo` 加每个工作包一个 worktree，各自 `.venv`，各自编译
  原生扩展）里进行，不再是云端容器。该机同时是 Forgejo 与 CI runner 的宿主：多文件 pytest 与原生
  编译一律包在 `flock /tmp/cm-test-heavy.lock` 里并用 `-n 4`；`/tmp` 是小 tmpfs，pytest 用
  `--basetemp=/var/tmp/...`。下一条的云端基线只适用于没有原生扩展的环境。
- **powerstation 上与本工作无关的既有失败（2026-11-25，A3b 与 A5 各自在起点提交上复现）**：
  `tests/shell/cli/daemon/test_supervisor_routing.py`、`tests/shell/cli/lifecycle/service/` 下的
  stop 路由与 `test_same_installation_stop.py`、`tests/shell/gui/lifecycle/service/test_gui_never_installs_service.py`
  （断言经 systemd / launchd 管理器启停，共约 14 条）；`tests/shell/cli/agents/test_cli_pi_interactive.py`
  2 条（本机 pi 安装）；`tests/integration/orgfs/commands/test_orgfs_cli_daemon.py::test_fs_cli_exposes_frozen_commands_and_version_flags`
  （`RuntimeError: Could not get a command for this Typer instance`）。原因均未查。GUI 侧：工作树
  没装 Python venv 时 `gui/tests/daemon-request-host/*` 等 78 条因 `No module named 'hyprial'` 失败。
- **云端容器里的测试基线**：未编译原生 actor 扩展，需设
  `HYPRIAL_ACTOR_BACKEND=reference-abi`；即便如此，会拉起子进程 daemon 的测试仍失败
  （`authority/test_orgfs_authority_corrections.py` 一条、`purge/test_orgfs_purge_receipt.py`
  三条），在未改动的 dev 上同样失败。
- **多个 worktree 共用一个虚拟环境时**，editable 安装指向的是建环境的那个 checkout。
  在别的 worktree 里跑测试必须显式 `PYTHONPATH=<worktree>/src`，并核对
  `hyprial.__file__`。不要在测试运行期间对同一工作区做 `git stash` 或切分支。
- **pycrdt 的 `Subscription` 绑定创建线程**，被另一线程释放会 panic。store 是多线程
  使用的，不要用 `Doc.observe` 做跨线程的失效通知。
- **pycrdt 的 `get_state()` 字节顺序不固定**，比较 state vector 必须先解码成时钟。
- **Yjs 的 state vector 不反映删除**，不能单独用作“文档是否变化”的令牌。
- **同一文档的两个 Doc 实例不能共用 client id 各自产生操作**。
- Forgejo：`code.hyprial.com` 的 API 与 git 推送经代理已认证；PR 正文首行必须是
  `Submitted-by: agent:<URI>`，并带 `Skill-Impact:` 行；WIP PR 标题以 `WIP: ` 开头。
  失败 job 的日志用 `GET /repos/{o}/{r}/actions/runs?head_sha=<sha>` 找 run，
  再 `/actions/runs/{id}/jobs`、`/actions/jobs/{job_id}/logs`。
- 结构门：每个含代码目录最多 7 个直接文件，每个代码文件最多 1000 行；新增代码不写
  行内注释，决策写在文件头。

### 10.1 配方：删除一个 daemon 请求方法（A1 实测，2026-11-25）

A1 删 `orgfs.read_at`、`orgfs.stat_at` 共动了 37 个文件。后续删除照此顺序；完整的逐文件表在
A1 报告 §2。

1. **先写红测试。** 在 `tests/daemon/ipc/retired_work/` 新增文件，断言 `METHOD_REGISTRY.names`、
   `MIGRATED_METHODS`、`_IPC_STATS_METHODS`、`orgfs_bridge` 的读 / 写 / org-context 表、MCP
   `list_tools`（`create_mcp_server` 加 `InMemoryTransport`，不需要 daemon）都不含该方法，
   `OrgFs` 与 `LocalOrgFs` 没有对应属性；再断言几个兄弟方法仍在，防止删多。
2. **orgfs。** `api.py` 的 Protocol 与只为它存在的值对象；`document/operations/*.py` 的实现；
   用 ruff F401 清失效 import；逐个 grep 私有 helper 的剩余使用者，没人用了才删，包括 store 层
   的读（`storage/store/*.py`、`space_authority.py` 里 `_ReadStore.kind` 的 Literal 与分派）。
3. **daemon 面。** `application/messaging/orgfs_bridge.py`（读表或写表、
   `_ORG_CONTEXT_READ_METHODS`、`_handle_orgfs` 分支）；`ipc/router.py` 的 `_IPC_STATS_METHODS`；
   `request_catalog/org/orgfs/{read,write}_specs.py`（params、handler、spec、`BATCH*_BINDINGS`）；
   `request_catalog/org/orgfs/{read,write}_examples/__init__.py`；
   `request_catalog/migration.py` 的 `MIGRATED_METHODS`；`mcp/server.py` 的工具。
4. **contract 脚本。** `contract/mcp-tools/run.py` 的用例、`factory.calls` 期望、末尾硬编码的
   `PASS N/N`。
5. **钉清单的测试。** `tests/daemon/channels/mcp/contracts/`（2 个）、`tests/daemon/contract_mock/`
   （3 个）、`tests/daemon/ipc/contracts/`（agent、membership、org 下共 7 个）、
   `tests/daemon/user_agent/admin/test_admin_refusals.py`、`tests/shell/request_http/test_catalog.py`、
   `tests/tooling/architecture/ipc/test_legacy_permit_retirement.py`。容易漏的是带计数的：
   `test_ipc_migration.py` 的 `len(reads)` / `len(writes)`，`example_integration/test_coverage.py`
   的 `len(TARGETS)` 与测试名里的数字，`example_read/producers.py` 的方法名分支。
6. **探针测试。** `git grep -nw '<python 方法名>'` 找出直接调用 facade 的测试，逐条判断换探针
   还是删一条；用 monkeypatch 指向被删属性的测试必须改（`setattr` 对不存在的属性会报错）。
7. **文档。** 源码 docstring；`docs/design/daemon-request/ipc-inventory.md` 删行（手写，无生成器，
   文件头计数按先例不改）；`docs/design/cli-daemon-request-convergence-2026-11-14/methods.{json,md}`
   是钉了基线 SHA 的静态盘点，不改；`CHANGELOG.md` 的 Removed。
8. **生成物（只由集成者做，按此顺序）。** `uv run python scripts/verify/dump_daemon_openapi.py
   --write` → `npm --prefix gui run client:reference`（同时重生 GUI 客户端与 contract mock）→
   golden manifest（`HYPRIAL_GOLDEN_RECORD=1 uv run pytest tests/daemon/golden -k "not manifest"`，
   再 `HYPRIAL_GOLDEN_RECORD=1 uv run pytest tests/daemon/golden/test_golden_manifest.py`；RECORD
   是否会去掉多余条目未验证，不行就手工删条目）→ 各 `--check` 再跑一遍
   （`dump_daemon_openapi.py --check`、`npm --prefix gui run client:reference:check`、
   `scripts/ci/check_request_contracts.py --base-ref <base> --json`、
   `scripts/verify/check_cli_help_inventory.py`）。GUI 手写代码若还引用被删方法，重生后 GUI 构建
   会红，所以 GUI 侧的删除必须先合入。
9. **PR 正文。** 重生后 `check_daemon_contract_diff.py` 会报 BREAKING 的方法删除，
   `check_contract_impact.py` 需要正文里有一行 `Contract-Impact: <理由>`。
11. **A3b、A5 补充（带 CLI 命令的方法）。** 另有两个生成物：`contract/cli/surface.json`
    （`uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py`）与
    `docs/reference/cli-command-inventory.json`（`scripts/verify/check_cli_help_inventory.py --write`）。
    另有三处钉清单的测试：`tests/shell/compatibility/test_unreachable_surface_retirement.py`
    （钉 `contract/mcp-tools/run.py` 的 `PASS N/N`）、`tests/integration/orgfs/commands/test_orgfs_cli_daemon.py`
    （钉 `fs --help` 的命令列表）、`tests/shell/cli/presentation/test_cli_help_inventory.py`。写方法
    的示例计数在 `tests/daemon/contract_mock/example_write/test_write_examples.py`。skill 与
    guide 里的用法要同步改，PR 写 `Skill-Impact: updated`。
12. **并行删除的合并隐患。** 两个分支把同一个计数改成同一个新值时 git 不报冲突，结果却少减
    一次。合并每个删除分支后都要跑钉清单的那批测试（本节第 5 步的目录加 `tests/shell/compatibility`）。
10. **预期的中间态失败。** 生成物重生之前，`tests/tooling/architecture/ipc/test_request_contract_gate.py`
    的 5 条与 `tests/daemon/golden/test_golden_manifest.py` 的 1 条会失败；除此之外不应有新失败。
