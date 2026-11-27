# OrgFS 测试分拣：`tests/daemon/org/network/**`（C5，2026-11-25）

状态：C5 的只读分拣产出，补 `orgfs-test-triage-2026-11-25.md`（C7）§6 列为“未读”的
`tests/daemon/org/network/**`。分类口径与 C7 §1 相同（契约-保留 / 契约-需改写 / journal专属-删除 /
待定），工作包取值也沿用 C7 §1。

基线：分支 `refactor/orgfs-callers-current-state`，在 `intg/orgfs-minimal` `f00c6c7c4a` 上加 C5 的
提交。条数来自 `uv run pytest tests/daemon/org/network --collect-only -q`（共 312 条，含 C5 新增的
`records/` 20 条）；行数是 `wc -l`。

## 证据与方法

- 每个文件读了导入、fixture、测试函数名，并用 grep 找出所有碰实现内部的位置：私有字段
  （`facade._space(...)`、`facade._spaces[...]`、`.nodes`、`.contents`）、`runtime.stores[...]`、
  `LocalSpaceStore`/`FileSpaceStore`、journal SQLite、`parked_commits`/`parkedCommits`、
  `document_authors`、`document_covers_member_add`、store 层的 monkeypatch。命中处读了完整函数体。
  表中行号都是读代码确认的 **[码]**。
- **命名陷阱**：本目录里大量出现的 “journal” 绝大多数**不是** OrgFS journal，而是
  `tailnet/removals/*.json`（`forwarding.membership.removal.RemovalJournal`）与
  `state/org/deletions/*.json`（org 删除意图）。只有 `test_org_directory_store.py:698` 一处碰的是
  OrgFS 的 `journal.sqlite3`。`test_device_removal.py` 的 `sqlite3` 是 casdoor 组表的假件。
- 本目录的测试几乎都是 OrgFS 的**消费方**：经 `OrgFsRuntime.facade` 或内存 `LocalOrgFs` 读写，
  或用 `test_org_network_service.py:67` 的手写 `FakeFs`。按 C7 口径，只经 facade 的记“契约-保留”，
  构造入口（`OrgFsRuntime(state_dir, node_id=, author=)`）在阶段 D 若改名，需同步。

## 0. 摘要

| 类别 | 条数 |
| --- | ---: |
| 契约-保留 | 294 |
| 契约-需改写 | 16 |
| journal专属-删除 | 2 |
| 待定 | 0 |
| 合计 | 312 |

要点：

1. **改写集中在两类搭建手法**：(a) 直接改 facade 私有结构制造“对端写来的坏数据”
   （`_spaces[...].contents[...].set`、`nodes[...].doc_id = ...`）或绕过 store 准入
   （`runtime.stores[space]._org_device_update_allowed`）；(b) 断言私有 `facade._space(...).nodes`。
   (a) 需要 C2 的 `forge_update`（设计 §3.6 末段）；(b) 改为经 `stat`/`listdir`/`trash()` 断言。
2. **`parkedCommits` 的消费断言只有 `status/test_network_status.py` 三条**，另有两个假件实现
   `parked_commits`（`test_org_network_service.py:223`，`FakeFs.status` 用旧 `SpaceStatus` 位置参数
   构造，:221）。阶段 D 删字段时一并处理（见 C5 报告的清单）。
3. **`document_authors` 的经 facade 行为测试**：本目录原有 `test_org_directory_store.py:140`（内容伪造，
   作者不变）与 `test_org_lifecycle.py:580`（绕过 store 准入）两条；C5 新增
   `records/test_foreign_author_callers.py` 三条（设备记录、member agent 清单、leave 请求）。
   这三条依赖内存 `LocalOrgFs` 允许第二个成员共写受保护记录；设计 §3.4 的本地写入校验就位后
   这种共写在本地即被拒，因此它们在 `state` 实现上要改用 `forge_update` 制造文档（记“需改写”）。
4. **C5 自身的改动影响了 5 条既有测试**（`test_org_lifecycle.py` 四条手写 leave 记录、
   `cell_actor/test_sub_org.py` 一条桩），已在 C5 提交里同步；它们仍记“契约-保留”。

## 1. 逐文件清单

### 1.1 顶层

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `test_org_acl_space.py`（896） | `test_owner_cannot_delete_an_unbound_org_via_fallback`、`test_owner_cannot_delete_an_unbound_org_that_still_has_members` | 2 | daemon 请求（`ipc_org_delete`）+ `deepcopy(facade._space(id).nodes)` 前后比较（:836、:846、:856、:865） | 拒绝删除未绑定 org 时 tree 不变（用户可见；断言摸私有字段） | 契约-需改写 | facade 通用（消费方） | 私有 `_space().nodes` 在新实现不存在；改为比较 `listdir(..., recursive=True)` 的 `(path, version)` 集合 |
| `test_org_acl_space.py`（896） | 其余 | 27 | daemon 请求（`ipc_org_*`）+ 真 `OrgFsRuntime`（`apps` fixture :70-86）；三条在同一 `state` 目录上换 author/node 重开 runtime（:606、:689、:731） | ACL 绑定、策略失败关闭、创建崩溃续做、repair、unbound org 的删除拒绝（用户可见） | 契约-保留 | facade 通用（消费方） | 只经请求层与 facade；重开同一 state 目录依赖 C1 的重开语义 |
| `test_org_directory_store.py`（741） | `test_directory_scan_does_not_replay_commit_history` | 1 | 直接往 `journal.sqlite3` 插 30 万行 `commits`（:702-722）+ 计数 `LocalSpaceStore._decode_envelope`（:726-733） | 目录扫描不重放提交历史、1 秒内完成（实现细节 + 规模） | journal专属-删除 | C6（规模） | 计划 §3：commits 表与 envelope 不再存在。保留的性质“目录扫描的工作量不随历史增长”归 C6 的规模测试；该测试还断言 `parkedCommits: []`（:739） |
| `test_org_directory_store.py`（741） | `test_reader_skips_prefixed_row_when_path_record_and_authors_disagree` | 1 | 真 runtime；用私有 `facade._spaces[id].contents[doc].set(...)` 改写设备记录正文（:154） | 记录 owner 与路径不符的设备行被跳过并告警，`docAuthors` 字段（用户可见；搭建摸私有字段） | 契约-需改写 | C2 成员与准入 / C5 | 搭建是“对端写来的坏正文”；改用 `forge_update` 构造 update 后经准入入口落地。断言的 `docAuthors` 来自 `document_authors`（`directory.py:244` 调用方），阶段 D 后为文档内作者集合 |
| `test_org_directory_store.py`（741） | `test_reader_ignores_a_non_deterministic_doc_link_with_warning` | 1 | 真 runtime；私有 `facade._spaces[id].nodes[node].doc_id = ...`（:522） | 受保护路径不采用随机 doc id（用户可见；搭建摸私有字段） | 契约-需改写 | C2 成员与准入 | 改用 `forge_update` 构造改 tree 行 `docId` 的 update |
| `test_org_directory_store.py`（741） | `test_put_device_reuses_the_deterministic_document_id` | 1 | 真 runtime + `runtime.stores[id].document_authors(doc_id)`（:555） | 重写设备记录复用确定性 doc id，作者仍是 owner（用户可见；断言摸 store） | 契约-需改写 | facade 通用 | 改为 `facade.document_authors(space, path)`，一行改动 |
| `test_org_directory_store.py`（741） | 其余 | 15 | 真 `OrgFsRuntime`（:70-77）经 facade；`test_leave_sweep_drops_an_unparseable_member_written_name` 用手写 store 假件 | 设备行往返、损坏行隔离与退避、公钥跨 owner 冲突、邀请文档、真实名字（用户可见） | 契约-保留 | facade 通用（消费方） | 只经 facade；monkeypatch 的是 `directory` 模块的时钟与常量 |
| `test_org_lifecycle.py`（752） | `test_remove_drops_membership_and_leaves_user_rows_inert`、`test_remove_of_an_already_gone_member_keeps_inert_device_history`、`test_process_leave_requests_retains_members_until_confirmation` | 3 | 真 runtime + 私有 `facade._space(id).nodes[node].deleted`（:318、:351、:525） | 移除成员 / 等待确认时设备行与 leave 行未被软删除（用户可见；断言摸私有字段） | 契约-需改写 | facade 通用（消费方） | 改为 `stat()` 仍可读、或 `trash()` 不含这些节点 |
| `test_org_lifecycle.py`（752） | `test_process_leave_requests_ignores_a_foreign_author_set` | 1 | 真 runtime；monkeypatch `runtime.stores[id]._org_device_update_allowed` 绕过 store 准入后以 Bob 身份改写 Alice 的 leave 请求（:610-628） | 作者集合不等于路径用户的 leave 请求不被处理（用户可见；搭建摸 store） | 契约-需改写 | C2 成员与准入 / C5 | 绕过点是旧 store 的私有准入函数；改用 `forge_update`。C5 已让该测试只由作者规则拒绝（记录带合法 `memberAddedAt`，断言整个 sweep 结果，C5 提交 `882d6b165f`）。内存 facade 版见 `records/test_foreign_author_callers.py` |
| `test_org_lifecycle.py`（752） | `test_r11_stale_leave_does_not_remove_a_rejoined_member_with_skewed_clocks`、`test_r11_fresh_leave_records_pending_removal_with_skewed_clocks` | 2 | 真 runtime + monkeypatch `hyprial.daemon.impl.orgfs.document.operations.spaces._now`（:657、:680） | 旧成员期的 leave 不移除重新加入的成员；当前成员期的 leave 进入待确认（用户可见；搭建摸实现模块路径） | 契约-需改写 | C5 | 行为保留（C5 起依据是 `memberAddedAt`）；打桩点是 facade 内部模块的 `_now`，阶段 D 生成 `addedAt` 的位置若移动需同步打桩点 |
| `test_org_lifecycle.py`（752） | 其余 | 18 | 真 `OrgFsRuntime` + `OrgNetworkService` + `FakeMembershipSidecar` | leave/remove/delete/list 的权限、确认、left-orgs 本地状态、角色覆盖（用户可见） | 契约-保留 | facade 通用（消费方） | 只经 facade 与 service；“journals” 指 `tailnet/removals` |
| `test_org_network_ipc.py`（561） | 全部 | 28 | `_FakeApp` + 真 `OrgFsRuntime`（:88）；monkeypatch facade 的公开方法（:200、:284）与 `OrgFsDirectoryStore.scan_devices`（:243） | org IPC 处理器的参数校验、错误码、回执先于 reconcile、目录扫描卡住时保留 sidecar 面板（用户可见） | 契约-保留 | facade 通用（消费方） | :241 的桩返回 `{"contentPending": [], "parkedCommits": []}`，阶段 D 删字段时同步 |
| `test_org_network_service.py`（995） | 全部 | 28 | 手写 `FakeFs`（:67-262） | create/invite/join/execute/publish/directory_peers 的服务语义（用户可见） | 契约-保留 | facade 通用（假件） | 不碰 OrgFS 实现；但 `FakeFs` 实现了将删除或改义的面：`status()` 用旧 `SpaceStatus` 五个位置参数（:221）、`parked_commits`（:223）、`document_authors` 由正文 `owner` 推出（:261）。阶段 D 收窄 Protocol 时同步 |
| `test_org_network_state.py`（85） | 全部 | 6 | `org/network/state.py` 本地文件 | left-orgs 与 ACL 授权缓存文件（用户可见） | 契约-保留 | —（不属 OrgFS 实现） | 不碰 OrgFS |

### 1.2 `device_uid/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `device_uid/test_device_uid_output.py`（164） | 全部 | 13 | `DirectoryDevice` 模型 + 服务测试的 `FakeFs` | 设备行 `deviceUid` 的读写、开关翻转重发布（用户可见） | 契约-保留 | —（identity 模型 / 消费方） | 不碰 OrgFS 实现 |

### 1.3 `lifecycle/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `lifecycle/test_org_deletion.py`（358） | 全部 | 11 | 真 `OrgFsRuntime`（:41）+ 服务 | org 删除的预览、确认、续做、墓碑、防复活（用户可见） | 契约-保留 | facade 通用（消费方） | “journal” 是 `state/org/deletions/*.json` 与 `RemovalJournal`；`:116` 手写的 leave 记录无 `memberAddedAt`，但 org 已删除，sweep 在准入前跳过，结论不受 C5 影响 |
| `lifecycle/test_org_deletion_safety.py`（424） | 全部 | 17 | 同上（`conftest.py` 复用 `test_org_deletion` 的 fixture） | 删除的安全边界（陈旧预览、非 authority、开放 tailnet）（用户可见） | 契约-保留 | facade 通用（消费方） | 同上 |
| `lifecycle/test_org_lifecycle_ipc.py`（566） | 全部 | 40 | `_OrgApp`（`cell_actor/test_org_cell_actor.py`）+ org IPC；monkeypatch `facade.read_text`（:458，公开方法） | 删除/离开/子 org 的线路错误码与 org cell 退役（用户可见） | 契约-保留 | facade 通用（消费方） | 只经请求层 |
| `lifecycle/test_org_roster_authority.py`（234） | 全部 | 6 | `_OrgApp` + org IPC | roster 写入跟随 tailnet authority（用户可见） | 契约-保留 | facade 通用（消费方） |  |
| `lifecycle/test_org_unbound.py`（240） | 全部 | 6 | 真 `OrgFsRuntime`（:38）+ 服务 | unbound org 上的成员操作报具名错误（用户可见） | 契约-保留 | facade 通用（消费方） | `:191` 的 leave 记录无 `memberAddedAt`，但 unbound org 在准入前被跳过 |
| `lifecycle/conftest.py`（5） | 0（支持代码） | 0 | 复用 fixture | — | 契约-保留 | — |  |

### 1.4 `status/`

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `status/test_network_status.py`（149） | `test_network_names_an_unknown_parked_author` | 1 | `FakeFs.parked = (("", 3),)` | 作者列为空的 parked 行显示为 `unknown`（已决定删除的能力） | journal专属-删除 | — | 空作者只来自旧 pending 表加作者列之前的行（`directory.py` `parked_commits`）；设计 §5：`parkedCommits` 字段删除 |
| `status/test_network_status.py`（149） | `test_network_reports_directory_rows_that_have_not_arrived`、`test_network_keeps_the_device_listing_when_parked_status_fails` | 2 | 服务 + `FakeFs`（`parked`、替换 `parked_commits` 抛错） | `directoryPending` 的 `contentPending` 与 `parkedCommits`；读失败记 `null` 且不隐藏设备列表（用户可见） | 契约-需改写 | C5（阶段 D） | `contentPending` 保留；`parkedCommits` 删除后改为断言新的诊断来源（`status.rejected_updates`）及其“读失败为未知”的规则。数据源已集中在 `OrgFsDirectoryStore.parked_commits_diagnostic` |
| `status/test_network_status.py`（149） | 其余 | 2 | 服务 + `FakeFs` | 三个面板、sidecar 卡住时目录仍返回（用户可见） | 契约-保留 | facade 通用（消费方） |  |
| `status/test_device_removal.py`（338） | 全部 | 24 | 真 runtime（`test_org_lifecycle` 的 fixture）+ `SQLiteGroups` casdoor 假件 + CLI；`:318` 在同一 state 目录重开 runtime | 按设备移除、确认、重开后成员关系保留（用户可见） | 契约-保留 | facade 通用（消费方） | `sqlite3` 是 casdoor 组表假件，不是 OrgFS；重开依赖 C1 |
| `status/test_directory_quarantine_wiring.py`（364） | 全部 | 5 | 真 `OrgFsRuntime`（:31）；monkeypatch facade 的 `listdir`/`read_text`（公开方法） | 无版本号的坏设备行只解析一次、隔离状态在服务间共享（用户可见） | 契约-保留 | facade 通用（消费方） | 依赖 `NodeInfo.version` 可为空的现状；新实现 `version` 恒有值时这几条仍成立（覆盖的是退化分支） |
| `status/test_membership_sidecar_fixture.py`（228） | 全部 | 13 | `FakeMembershipSidecar` + `RemovalJournal`；两条用服务测试的 `FakeFs` | sidecar 假件与移除日志的契约（用户可见） | 契约-保留 | —（不属 OrgFS 实现） |  |
| `status/test_daemon_membership_fixture.py`（57） | 全部 | 2 | `DaemonApplication` + tailnet 假件 | membership 假件不导入目录（用户可见） | 契约-保留 | —（不属 OrgFS 实现） |  |
| `status/test_removal_recovery.py`（219） | 全部 | 16 | 真 runtime + 服务 + CLI | 移除的重试、确认失效、重新入网不被隐藏（用户可见） | 契约-保留 | facade 通用（消费方） | “journal” 是 `tailnet/removals` |

### 1.5 `records/`（C5 新增）

| 文件（行） | 测试 | 条 | 驱动层 | 断言内容（性质） | 分类 | 工作包 | 依据 |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `records/test_leave_member_added_at.py` | 全部 | 17 | 真 `OrgFsRuntime` + `OrgFsDirectoryStore` + 服务 | leave 请求写入并比较 `memberAddedAt`；旧成员期、缺字段、类型不对、无成员行都不覆盖；不再调用 `document_covers_member_add`（用户可见） | 契约-保留 | C5 | 只经 facade 与 `members()`；建议阶段 D 迁入 C4 契约目录并对两种实现参数化 |
| `records/test_foreign_author_callers.py` | 全部 | 3 | 内存 `LocalOrgFs`，以第二个成员身份共写受保护记录 | 设备行、member agent 清单、leave 请求被共写后调用方拒绝（用户可见） | 契约-需改写 | C2 / C4 | 调用方断言保留；搭建依赖内存 facade 不做 §3.4 本地校验，`state` 实现上改用 `forge_update` 制造共写文档 |

## 2. 给阶段 D / E 的清单

- **E2 删除**：`test_org_directory_store.py::test_directory_scan_does_not_replay_commit_history`、
  `status/test_network_status.py::test_network_names_an_unknown_parked_author`。
- **D 随 `parkedCommits` 删除改写**：`status/test_network_status.py` 两条（见上）；
  `test_org_network_ipc.py:241` 的桩；`test_org_directory_store.py:739` 的断言随所在测试删除。
- **D 随 Protocol 收窄同步假件**：`test_org_network_service.py` 的 `FakeFs`（`status`、`parked_commits`）。
- **需 C2 `forge_update`**：`test_org_directory_store.py` 两条、`test_org_lifecycle.py::test_process_leave_requests_ignores_a_foreign_author_set`、
  `records/test_foreign_author_callers.py` 三条。
- **私有字段断言改为公开读**：`test_org_acl_space.py` 两条、`test_org_lifecycle.py` 三条、
  `test_org_directory_store.py::test_put_device_reuses_the_deterministic_document_id`。

## 3. 未读与限制

- 没有逐行读完的：`test_org_network_service.py` 与 `lifecycle/test_org_lifecycle_ipc.py` 的多数函数体
  （它们不碰 OrgFS 实现，导入、假件与 grep 已足以判定）。
- “新实现上原样通过”是读代码得出的判断，没有在新实现上运行。
- 本文件之外的 `tests/daemon/org/**`（`cell_actor/` 等）不在本次分拣范围内；其中
  `cell_actor/test_org_cell_actor.py:300`、`test_sub_org.py:73`、`test_sub_org_v2_security.py:60-87`
  把成员节点的 `document_authors` 桩到 owner 的 facade，阶段 D 若改作者来源需复核。
