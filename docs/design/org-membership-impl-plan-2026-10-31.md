# Org 成员模型：0.5.5.3 实施计划（#2078）

> **组织生命周期及 Agent 归属规则已被 [#2883 Draft](https://code.hyprial.com/HyprialOS/hyprial/pulls/2883) J7/J10/J13 取代（2026-11-19）。**
> 下文关于个人组织、Agent 至少一个组织，以及 D1 必须删除 Casdoor group / 提供 infra delete-group 端口的描述仅保留为历史实施记录，不再作为新开发的前置条件。现行过渡状态见 [Agent 归属修订](agent-org-membership-2026-11-18.md)。

> **Work-related implementation steps superseded (#2214, 2026-11-08).**
> `org.resolve` and current-org selection remain; all `work.*` methods and
> dedicated work writers are retired. `org work ls/show` are ordinary
> `orgfs.ls/read` shortcuts; see
> [the current contract](../../contract/work-items/README.md).
> This annotation does not change the historical membership plan below.

按“个人 org 与迁移 → 命令 → 共享 org 准入 observe → #2077 交接”实施。
设备重试和组织删除各自拆线。**0.5.5.3 不启用 enforce**。
本文供研发负责人拆 implementation PAC；本文没有产品代码或 E2E 通过结论。

## 1. 输入、基线和发布边界

- 已裁设计：[org-membership-model-2026-10-29.md](org-membership-model-2026-10-29.md)，
  输入提交 `2353b62ff12d97cb14714d35e26b8c248aa2315f`。§8 全部推荐已批准，Q7=A。
- 产品事实：`internal/dev` **`178d60063595e031a94d692af6bbde191c55c2ab`**。
  本文现状引用全部固定在这个提交；不沿用设计原文的旧行号。
- #2077 输入：`internal/design/provider-key-ownership` =
  `62a697c49f511ff09c3aab9904e704211daf84b6`，已完整读取其设计和 2026-10-31 实施计划。
  其中 grant 存放位置以 #2078 Q7=A 为准，改为 **key 主人的个人 org**。
- 源码路径以下相对 `src/hyprial/`；`tests/`、`contract/` 是仓库相对路径。
  `[新]` 表示拟建文件。S/M/L 是范围，不是工期；L 必须拆子片派单。

本版固定规则：

1. 用户成员关系由 Casdoor group 决定，OrgFS 保存投影。agent 成员关系保存在 OrgFS。
2. 个人 org 只允许本人及本人的代理；不能邀请其他用户。工作 org 的 agent 加入需 admin/owner 同意。
3. `org member add/remove --agent <agent> [--org <org>]` 管理归属。
   `org member ls` 同列人和 agent。`agent create --org` 是快捷入口。
4. 每个 agent 至少有 1 个 org。用户、squire 和 user-proxy 的默认通信来自个人 org 策略。
5. 新命令和改动命令先声明 MethodSpec，daemon 持有业务，CLI 只请求、确认和渲染。
6. 迁移是 `upgrade/steps/` 下的升级步骤，由 `run_upgrade_steps` 调度。
   每节点幂等执行；`doctor --fix` 可重跑。不得另造一次性 shell 迁移。

### 1.1 验收解释

设计 A3/A7 的“拒绝通信”与 observe 版本有时间差：

- 0.5.5.3：新判定返回 would-refuse；事件可见，实际投递继续。
- 0.5.5.4：启用 enforce 后才关闭 A3/A7 的真实拒绝断言。
- 本版可用隔离的纯策略测试证明拒绝码；不得以测试开关替代生产 enforce 验收。

存在未裁的协议细节时，按 §8 阻塞对应子片。不得把下面拟定的路径和方法名当作已发布契约。

## 2. Reality check：会改变拆片顺序的事实

以下每条证据均指 §1 的产品提交。

| ID | 当前事实和实施影响 | file:line |
|---|---|---|
| C1 | home 已可持多个 org；本机列表排除 left/deleted。无需重写多 org 存储 | `daemon/impl/org/network/service.py:701-709` |
| C2 | work 的默认组织由 CLI 查 `org.list` 后决定；多 org 报 `WORK_ORG_REQUIRED`。默认解析必须移到 daemon | `shell/impl/cli/commands/work/commands.py:68-102` |
| C3 | login 后仍 `accept_all=True, confirm=None`；pending helper 已支持逐条确认和只列 pending | `shell/impl/cli/commands/login/flow.py:683-705`；`shell/impl/invites/pending.py:101-120,160-184` |
| C4 | `org delete` 权限检查后必抛 `LAST_AUTHORITY_REFUSED`。原因涉及 Tailnet 最后 authority，不只是缺一个删 group 调用 | `daemon/impl/org/network/service.py:416-420` |
| C5 | `CasdoorGroups` 有 ensure/add/remove/member_state，没有 delete；缺配置时 require_group 返回 `not-configured`。个人 org 不能用此返回值冒充 Casdoor 已创建 | `daemon/impl/org/network/casdoor/client.py:60-71`；`daemon/impl/org/network/casdoor/membership.py:75-76` |
| C6 | org create 已有本地串行意图和恢复流程，先确保 Casdoor 再建 OrgFS 目录与 ACL 空间，最后建 Tailnet。个人 org 必须复用并补跨节点幂等 | `daemon/impl/org/network/service.py:171-249`；`daemon/impl/org/network/state.py:96-170` |
| C7 | CLI agent.create 先建 identity 再做 routine/路由；另有 `_ensure_agent` 的 get-or-create 路径。PAC lifecycle 直接发 CreateAgentCommand。不能依据旧注释声称已只有一条创建路径 | `daemon/impl/application/actors/agents_admin/registry_ops.py:348-407,479-519`；`daemon/impl/application/actors/agents_registry.py:489-558`；`daemon/impl/lifecycle_manager/steps.py:151-158`；`daemon/impl/pac/actors/daemon.py:172-188` |
| C8 | Agent 记录没有 org；org-context 是另一种文档：members 是人，residents 只有 name/node/role 和可选 serves。不能把 members 中的人名当 agent URI | `identity/impl/agents/registry/_records.py:164-230`；`daemon/impl/org/document.py:230-260` |
| C9 | preferredReceiver/squireChannel 由本地 UserProfile 序列化；actor 写回本地 store，接收端及启动恢复消费本地 profile | `identity/impl/users/profile.py:227-318`；`daemon/impl/squire/actors/profile.py:109-162`；`daemon/impl/squire/addressing.py:411-456`；`daemon/impl/application/startup.py:620-630` |
| C10 | bypass 默认开；store 在 bypass 时关闭 bookkeeping，且成员集合只在 bypass 时注入。不能只改默认值而不补投影 | `daemon/impl/composition/links.py:98-156,168-176,731-738` |
| C11 | sender_known 先按 index.known/本机文件放行，再检查远端 owner 是否属于任一空间。已知 link 也可绕过新的共享 org 判断 | `daemon/impl/links/service/gate.py:152-175,629-631`；`daemon/impl/links/store.py:699-722` |
| C12 | owner 对不仅免准入，还不计 degree/eviction/shrink，且 model 拒写文件。需一起退役 | `daemon/impl/links/policy.py:148-167,192,253,282`；`daemon/impl/links/model.py:181-182,271,286-287` |
| C13 | LinkAdmission 的共同调用条件只管 agent↔agent；所有 user 两端不进入该条件。只改 policy.governed 不能覆盖 R4 | `daemon/impl/dispatch/admission.py:120-153`；`daemon/impl/application/messaging/inbox_surface/link_send.py:329-366` |
| C14 | ingress 当前只返回 bool→LINK_UNKNOWN，异常时接收；dispatch 只读内存且不能做 I/O。新错误码和投影需独立接入 | `daemon/impl/dispatch/admission.py:169-176,237-266`；`daemon/impl/inbox/service/delivery/receive.py:174-181` |
| C15 | upgrade runner 单锁、原子 marker；失败可重试，非 repeatable 的 done 会跳过；返回 data.failed 控制失败状态 | `daemon/impl/upgrade/runner.py:44-62,101-170`；`daemon/impl/upgrade/steps/__init__.py:16-43` |
| C16 | upgrade 和 doctor 传 Hyprial home；IPC upgrade.steps.run 却传 Path.home。doctor fix 又丢弃 fix 返回值重读状态。迁移上下文和报告需统一 | `shell/impl/cli/commands/upgrade/commands.py:691-699`；`shell/impl/cli/commands/doctor/upgrade_steps.py:14-19`；`daemon/impl/ipc/router.py:463-471` |
| C17 | catalog 目前只迁 link family；router 先查 registry，再禁迁移 family 回退。不能直接把 org/agent 整族标 migrated，旧子方法会失效 | `daemon/impl/request_catalog/registry.py:20-22,244-247`；`daemon/impl/ipc/router.py:423-433,245-265` |
| C18 | MethodRegistry 校验 params/result/声明错误；OpenAPI 由纯声明生成，非 HTTP server | `daemon/impl/request_catalog/registry.py:131-199`；`contract/daemon-request/README.md:3-24` |
| C19 | supplier health 跨 space 共用；到 hold 期限继续 probe；成功将计数减半，尚无 dormant。授权失败被区分为空间局部失败 | `daemon/impl/orgfs/network/supplier_health.py:87-126,183-212,229-246` |
| C20 | Zenoh 重连是持久 desired link；sidecar 每条本地 slot 连接都可能再拨远端，失败后关闭本地连接。只停 OrgFS sync 不会停止设备拨号 | `daemon/impl/transport/zenoh/config.py:202-217`；`sidecar/hyprial-tailcat/internal/engine/slots.go:197-246` |
| C21 | slot reconcile 每 tick 重绑 wanted peers；不能用普通 unmap 表示 dormant，再等下个 tick 自动绑回 | `daemon/impl/forwarding/slots/reconcile.py:1-16,155-164,219-238` |
| C22 | Casdoor sync 有 webhook/全量 backstop 和 breaker，但已判 removed 仍走未确认 Tailnet removal；未 complete 时保留 pending。不能声称 A7 已自动关闭 | `daemon/impl/org/network/casdoor/sync.py:14-26,499-530`；`daemon/impl/org/network/tailnet/__init__.py:77-98` |
| C23 | OrgFS 有 create_only/expect_version、watch/watch_meta；准入有 membership frontier/removal-wins 和保护路径。可扩展现有权威，不另建共享库 | `daemon/impl/orgfs/api.py:238-283`；`daemon/impl/orgfs/storage/store/admission.py:436-526` |
| C24 | org 名仅小写字母/数字/连字符、最多 63 字符；保护分区尚无 agents/receivers。需先冻结 userKey→个人 org 和路径编码 | `identity/impl/org_directory/models.py:62-90,470-489` |

### 2.1 结构前置工作

以下为同一提交逐文件计行、`git ls-tree` 直接项计数。

| 临界文件 | 当前行数（也是 `:1-N` 引用范围） | 安排 |
|---|---:|---|
| `daemon/interface.py` | 998 | P0 保持导出名的布局提取后才增公共端口 |
| `identity/interface.py` | 875 | 用单个 OrgMembership 命名空间，预算 ≤950 |
| `identity/impl/agents/registry/_records.py` | 996 | 不在 registry 内增第二份成员权威 |
| `daemon/impl/orgfs/storage/store/admission.py` | 1000 | P0 先提取 pending helper，再委托新保护路径判断 |
| `daemon/impl/org/network/directory.py` | 963 | agent/receiver 放新子包，不继续堆目录扫描 |
| `shell/impl/cli/commands/org/commands.py` | 983 | 提取到 member/context 子包，主文件仅注册和委托 |
| `shell/impl/cli/commands/login/flow.py` | 974 | 邀请展示拆子包，主流程仅调用 |
| `daemon/impl/application/messaging/work_items.py` | 956 | 默认解析委托新服务，不内联另一套 resolver |
| `daemon/impl/composition/links.py` | 924 | 新 admission 装配委托子包 |

满 7 个直接文件的目录包括：
`daemon/impl/{org/network,composition,orgfs/storage/store,application/actors/agents_admin,application/wiring}`、
`identity/impl/{org_directory,agents/registry}`、`shell/impl/cli/commands/{org,agent}`、
`daemon/impl/squire`（包含 `SKILL-template.md`）。
只在这些目录新增子包。新包也计 `__init__.py`，目标单文件 ≤700 行，硬门 ≤1000 行。

## 3. 分片（按依赖顺序）

每片先冻结契约 fixture，再写会因旧行为失败的断言，保存 RED 命令/输出，之后修改产品代码并保存 GREEN。
不得只用“缺 import”作为行为 RED。公共面、router 和主快照由集成节点单写。

### P0 — 结构和声明入口（M；无前置；不关闭 A 用例）

范围：保留现有行为的提取；建立逐方法迁移规则。
使用精确的 `MIGRATED_METHODS` 集合防止新方法回退旧 router。
未全量声明前，不把 org/agent 整族加入 `MIGRATED_FAMILIES`（C17）。

文件：

- daemon：`interface.py`；`impl/request_catalog/{registry,__init__}.py`；`impl/ipc/router.py`。
  `impl/orgfs/storage/store/admission.py`；
  `[新] impl/orgfs/storage/store/admission_support/{__init__,pending}.py`。
- shell：`impl/cli/commands/org/commands.py`；
  `[新] impl/cli/commands/org/member/{__init__,commands,render}.py`；
  `[新] impl/cli/commands/org/context/{__init__,commands}.py`。
- 测试：`[新] tests/daemon/org_membership/structure/test_seams.py`；
  修改 `tests/daemon/ipc/contracts/test_method_specs.py`。

RED：已迁移方法缺 binding 必拒绝，不进入旧 handler；旧 org.acl 等未迁方法仍可调用。
原接口和 pending admission 回归必须始终 GREEN。无新错误码/事件。
契约：现有外部行为不变；`contract/daemon-request/README.md` 记录逐方法迁移纪律。
与 #2077 P0 的 admission_support 提取只能合一次，不派两个 worker 各造一份。

### O1a — 个人 org、agent 归属、接收机存储（L，拆 3 个 M；依赖 P0、§8 R1/R2）

三个顺序子片：个人 org 生命周期 → 受保护 agent 记录及投影 → receiver 消费切换。

范围：

1. 为已认证用户 ensure 唯一个人 org，`kind=personal`；每台设备加入为 host。
   复用 create 恢复流程，但跨设备并发必须返回同一 Casdoor group 和同一 OrgFS 空间绑定。
   缺 Casdoor 权限、空间冲突、远端绑定未同步时报告未完成，不生成第二个同名空间（C5/C6）。
2. agent 归属写 OrgFS 目录；registry 只保留可重建显示信息。
   以规范 agent URI 和化身识别成员，重建同名 agent 不继承旧归属。
   协议初稿沿用设计的 `agents/<agentId>.json`，增加版本/授权来源与 tombstone；编码先过 R1。
3. `receivers.json` 保存 preferred/squire/userProxy；迁移时保留 squireChannel 路由语义。
   所有 receiver 读写经 daemon 的 OrgFS service。identity profile 保留 1 版只读迁移源。
   adapter 凭据、owner open_id 核验规则不随 receiver 迁走，也不把凭据放 OrgFS。
4. 个人 org 拒绝外人邀请；工作 org 的 agent 写入在准入层校验 admin/owner 同意。
   不因调用者与 agent 同 owner 就给予工作 org 写权限。

文件：

- identity：`[新] impl/org_membership/{__init__,models,paths,policy,ports}.py`；`interface.py`；
  `impl/org_directory/models.py`（新保护分区委托）；`impl/users/profile.py`（只读兼容边界）。
- daemon：`[新] impl/org/membership/{__init__,service,store,projection,personal,receivers}.py`；
  `impl/org/network/{service,directory,policy}.py`；`impl/org/network/create/{__init__,recovery}.py`；
  `impl/orgfs/storage/store/admission.py`；`impl/squire/{addressing,setup}.py`；
  `impl/squire/actors/profile.py`；`impl/application/{startup,wiring/transport}.py`；`interface.py`。
- shell：`impl/cli/commands/{common/support,squire/user_proxy}.py`（receiver 改为请求 daemon）。
- 声明：`[新] daemon/impl/request_catalog/org/{__init__,schemas,context,members,lifecycle,invites,network}.py`。
  此片先声明内部客户端用的 `org.personal.ensure`、`org.receivers.get/set`；后续片填同包。

RED：`[新] tests/daemon/org_membership/storage/{test_personal,test_membership,test_receivers,test_admission}.py`；
`[新] tests/identity/org_membership/test_records.py`。
双设备 ensure 同一空间；外人不能加个人 org；伪 author/错误路径/旧化身写入拒绝；
本地 profile 被遮住后仍能解析 receiver；组织间记录不可串读；未知投影不能当空成员写回。

验收：A4/A9 的存储基础；尚不关闭迁移或通信断言。D5 的消费切换在本片，数据导入在 O1b。
错误：沿用 `ORG_ACTION_NOT_AUTHORIZED`、`DAEMON_NOT_READY` 与 OrgFS CAS 原码；新状态字段先入 schema。
事件：`org.agent.membership.changed`；receiver 更新事件拟名 `org.receivers.changed`，需契约评审冻结。
契约：`[新] contract/org-network/membership.md`、`contract/fixtures/org-membership/{records,admission}.json`；
修改 `contract/org-network/README.md`、`contract/daemon-request/openapi.json`。
方法声明包含可信 caller、params/result/errors、幂等性、audience、confirmation 和 http_exposed。

### O1b — 升级迁移和 doctor 续作（M；依赖 O1a；关闭 A9）

范围：按 §6 的状态机迁移所有已登录用户、既有 agent、squire/user-proxy 和 preferredReceiver。
步骤 ID 拟为 `org-membership-v1`，`repeatable=True`；每次执行生成报告。
完成标记不取代实体级幂等检查。远端 owner 不能在本机被代认证；待其设备执行或同步后续作。
报告里未完成项必须留在 failed/pending，不能把空结果标 done。

文件：

- daemon：`[新] impl/upgrade/steps/org_membership.py`；`impl/upgrade/{runner,steps/__init__}.py`；
  `[新] impl/org/membership/migration/{__init__,inventory,journal,apply,report}.py`；
  `impl/org/{document,store,orgfs_migration}.py`（只读来源适配）；`impl/ipc/router.py`。
- shell：`impl/cli/commands/{upgrade/commands,doctor/upgrade_steps}.py`。
  两个 CLI 只调用 daemon 方法，不打开 registry/OrgFS 执行业务。
- 声明：`[新] daemon/impl/request_catalog/upgrade/{__init__,methods}.py`：
  `upgrade.steps.status/run`，保持旧参数兼容；修正 Hyprial home 输入（C16）。

RED：`[新] tests/daemon/org_membership/migration/{test_inventory,test_resume,test_multinode,test_conflicts}.py`；
修改 `tests/daemon/upgrade/test_steps.py`、`tests/shell/cli/test_daemon_upgrade_steps.py`。
逐断电点、同节点重跑、两个节点反序升级；receiver 首写保留；歧义 resident 不猜；
org-context 已有归属保留，否则加入 agent 主人的个人 org；故障期间旧数据不删。

错误：不新增公开错误码；用报告 `failed[]` 的结构化原因标明冲突/权限/投影未就绪。
事件拟名 `org.migration.report`，只含主体/目标/阶段/结果/冲突，不含 credential。
契约：membership.md 加 journal/report schema；
`[新] contract/fixtures/org-membership/migration.json`；更新 upgrade 方法的 OpenAPI、doctor JSON 契约。
A9 仅在报告中所有固定对象均验证后关闭；foreign-owner 待同步就是 BLOCKED。

### O2a — current org 和统一默认解析（M；依赖 O1a/O1b）

范围：`org use <org>`、`org use --clear`；`org list` 加 kind/current。
本地 current 设置由 daemon 写，按 home 隔离；不复制到 OrgFS，不影响通信。
解析顺序固定为：显式 `--org` → current → 唯一可用 org → `ORG_REQUIRED` + 候选。
失效 current 不允许静默选错工作 org；清理/报错策略先在 R6 冻结。
agent create 的默认是另一个已裁规则：显式 → current → 创建者个人 org；不能套“唯一 org”规则。

文件：

- daemon：`[新] impl/org/membership/context/{__init__,settings,resolver}.py`；
  `impl/org/network/{service,ipc,state}.py`；`impl/application/messaging/work_items.py`；
  catalog 的 `org/{context,schemas}.py`；
  `[新] impl/request_catalog/work/{__init__,methods,schemas}.py`。
- shell：`impl/cli/commands/org/{commands,context/commands}.py`；
  `impl/cli/commands/work/commands.py`（删除本地 `_org_space` 业务）；
  `impl/cli/runtime/registration.py`。
- kernel：`impl/contracts/daemon/ipc_errors.py` 注册 `ORG_REQUIRED`。

MethodSpec：`org.use`（set/clear）、`org.resolve`、`org.list`。
work 改动的现有方法全部声明：
`work.add/check/note/status/done/repair/link/keywords/show/resolve/ls`。
daemon 在执行 work 方法时解析 org；不能只让 CLI 调 org.resolve 后自报任意 spaceId。
旧显式 spaceId 参数在 daemon 验证对应可用 org，保留兼容但不作为信任输入。

RED：`[新] tests/daemon/org_membership/context/{test_resolution,test_current,test_work}.py`；
`[新] tests/shell/cli/org_membership/test_defaults.py`。
A/B 两 org，current=B，work/member 默认 B，显式 A 覆盖；清空 current 后歧义报错；
current 切换不改变准入；其它 home 不受影响；非法/left/deleted 候选不可默认选入。

验收：A1 的 resolver/work；member 默认选择等 O2b。
错误/事件：`ORG_REQUIRED`，`org.current.changed`；旧 `WORK_ORG_REQUIRED` 改码同步契约，不能只改提示。
契约：membership.md、`contract/cli/surface.json`、OpenAPI；
`[新] contract/fixtures/org-membership/defaults.json`。

### O2b — agent 成员命令和所有创建入口（L，拆成员 M / 创建集成 M；依赖 O2a）

范围：

- `org member add/remove --agent <agent> [--org <org>]`、`org member ls [--org <org>]`。
  人员邀请/移除的既有服务继续 Casdoor-first；人和 agent 同列并标类型、来源、role。
- `agent create --org` 与同一 daemon 创建路径共用成员服务；`agent list` 加 orgs 数组和人读列。
  创建必须先持久确认目标归属，再暴露可运行 agent；registry 已写但 OrgFS 未确认等阶段故障用操作 journal 续作。
  不得先启动 worker 再异步补归属。
- admin/owner 同意由 daemon 核验；普通 owner 发起不等于已批准。
  未取得批准不得创建“看起来已加入”的记录。申请/批准协议细节见 R2。
- 并发移除两个不同 org 也不能留下零归属。leave/delete/Casdoor 撤成员需要清理有效归属。
  强制清理前先保证个人 org 归属；人工移除最后一个则固定报 `AGENT_LAST_ORG`。
- 覆盖 start、PAC 创建、host-invite、transfer landing、resume/reuse 等非 CLI 入口。
  不能让 registry 反向 import daemon；daemon 编排通过 identity 公共端口执行。

文件：

- daemon：`impl/org/membership/{service,store,projection}.py`；
  `[新] impl/org/membership/creation/{__init__,service,journal}.py`；
  `impl/application/actors/agents_admin/registry_ops.py`（提取/委托 create 分支）；
  `impl/org/network/{ipc,service}.py`；`impl/org/network/casdoor/sync.py`；
  `impl/transfer/landing/registry.py`；`impl/application/actors/{agents_registry,lifecycle_ops}.py`；
  `impl/composition/views.py`；`impl/lifecycle_manager/steps.py`；`impl/lifecycle/atomic_lifecycle_ports.py`；
  `impl/pac/actors/daemon.py`（在 harness 启动前校验归属提交，不能只在 settled 后补）；
  catalog `org/members.py`；`[新] impl/request_catalog/agent/{__init__,methods,schemas}.py`。
- identity：`impl/agents/actor/{ports,_generation_ops,_generation_dispatch}.py`（typed command 的编排接点）；
  `impl/agents/registry/_records.py` 仅按需减代码，不增 org 权威字段；`interface.py`。
- shell：`impl/cli/commands/org/member/commands.py`；
  `impl/cli/commands/agent/{create,admin}.py`；`impl/cli/runtime/registration.py`。
- kernel：`impl/contracts/daemon/ipc_errors.py` 注册 `AGENT_LAST_ORG`。

MethodSpec：`org.member.add/remove/ls/invite`，复用人移除和 agent 移除的互斥字段；
保留旧 `org.invite/remove` 为同服务兼容声明，不保留旧业务副本；`agent.create/list/host-invite`。
其它创建入口即使无新命令，也必须过同一成员编排。

RED：`[新] tests/daemon/org_membership/commands/{test_members,test_create,test_creation_recovery,test_last_org}.py`；
`[新] tests/shell/cli/org_membership/test_members.py`。
最后归属拒绝及跨节点并发；未授权加入工作 org 拒绝；新建默认归属的两个 fallback；
每种创建路径至少一个行为测试；reuse/resume 不改既有归属；混合列表无漏项。

验收：关闭 A1、A5；A9 防止迁后新建零归属。
错误/事件：`AGENT_LAST_ORG`；沿用授权/CAS 码；
`org.agent.membership.changed`、`org.member.changed`（含 Casdoor 同步结果）。
契约：membership.md、OpenAPI、CLI surface、`contract/fixtures/org-membership/commands.json`。
跨节点最后归属协议和 forced removal 的暂停态未过 R2/R4 时，不得称本片完成。

### O2c — login 逐条确认邀请（M；依赖 P0，可与 O2a/O2b 并行）

范围：登录后查询待处理邀请；TTY 下逐条确认，非 TTY/JSON 只报告。
只有显式 `org pending --accept-all` 才批量接受。拒绝本次确认不会伪写 Casdoor consumed 状态。
登录认证已提交后，邀请失败仍是独立结果，不回滚登录（保留 C3 的 best-effort 边界）。

文件：

- daemon：`[新] impl/org/membership/invites/{__init__,service,account}.py`；
  catalog `org/invites.py`；`impl/org/network/ipc.py`。
- identity：`[新] impl/org_membership/invites/{__init__,models,selection}.py`（纯解析/去重）；
  使用 identity 已有登录凭据公共面，不 import shell。
- shell：`impl/cli/commands/login/flow.py`；
  `[新] impl/cli/commands/login/invites/{__init__,prompts}.py`；
  `impl/cli/commands/org/commands.py`；`impl/invites/pending.py`（过渡为请求适配，无业务复制）。

MethodSpec：`org.invites.list/accept`，绑定特定 invite 的确认与幂等 operation；
`org.join` 同时声明，继续调用既有 join 服务；login 只组合这两个方法和 stderr confirm。
OAuth 认证流程本身不在本片搬入 daemon。

RED：`[新] tests/daemon/org_membership/invites/test_selection.py`；
`[新] tests/shell/cli/org_membership/test_login_invites.py`。
2 邀请分别确认 yes/no；无 TTY 0 join；JSON 无提示污染；重复 space 只 join 一次；
已 leave 不自动回来；失效邀请单独失败；原始 bearer/token 不进结果和事件。

验收：关闭 A2；删除 D6。
错误：复用 join/登录原码，MethodSpec 全量声明；不新增公开错误码。
事件：`org.invite.pending`；接受结果包含 pending/accepted/failed，明确本次 declined 不等于远端删除邀请。
契约：CLI surface、OpenAPI、`contract/wire/org-invite-short-link.md`；
`[新] contract/fixtures/org-membership/invites.json`。

### O3 — 共享 org 准入 observe，删除 D1–D4（L，拆策略 M / 边界接入 M / 审计回归 M）

前置：O1a/O1b/O2b 的投影与写入约束可用；§8 R3/R4 有结论。

范围：新准入独立于“有无 link 文件”。

1. 从可信 sender/recipient 取 OrgFS 投影集合，计算 sharedOrgs，再逐 org 检查策略。
   仅允许已核验内部身份和 self 的设计豁免；不得通过相同 owner、任意空间成员或已有 link 放行。
2. 在 send、远端 ingress、dispatch、user delivery 最终边界接入同一判定。
   既检查逻辑 `user:` 收件人，也检查代理投递关系；不能把代理解析成跨 org 通信通行证。
3. 删除 owner 对的免 W/degree/LRU/shrink、拒写 OrgFS 和 add 限制（§5）。
   `owner_of` 若仅用于显示可保留；不得继续参与准入。
4. bypass 默认关；应急 bypass 只放宽 link 状态，仍计算共享 org 准入。
   新增独立的 `org.admissionMode=observe|enforce` 开关草案。
   0.5.5.3 默认并发布 observe；0.5.5.4 的 enforce 激活另派单。
   `links.windowMode` 是窗口控制，不能兼作 org 准入开关。
5. observe 对 `NO_SHARED_ORG`/`ORG_POLICY_DENIED` 记录 would-refuse，继续投递。
   不把 unknown/unavailable 投影假报为“无共同 org”；记录独立的投影故障状态。
   鉴权、reply 关联、化身围栏、reclaimed residue 仍按原安全规则处理。
6. A7 的 Casdoor remove 立即影响“有效成员”投影；保留的 agent 文件不能继续授予已撤用户的通信。
   不反向写 Casdoor。Tailnet 未确认撤设备可继续报告 pending，但不能阻止应用层观察到成员撤销（C22）。

文件：

- daemon：`[新] impl/org/membership/admission/{__init__,policy,snapshot,events,service}.py`；
  `impl/links/{model,policy,store,ports}.py`；`impl/links/service/{gate,views}.py`；
  `impl/composition/links.py`；`impl/dispatch/admission.py`；
  `impl/application/messaging/inbox_surface/{link_send,send}.py`；
  `impl/application/messaging/delivery/sends.py`；`impl/squire/addressing.py`；
  `impl/inbox/service/delivery/receive.py`；`impl/org/network/casdoor/sync.py`。
- identity：`impl/org_membership/{models,policy,ports}.py`；`interface.py`（纯模型，无网络）。
- kernel：`impl/contracts/daemon/ipc_errors.py`；`impl/hooks/events.py`。
- shell：`impl/cli/commands/config/settings.py`，仅新增 `org.admissionMode` key 分支，
  调用下述 MethodSpec 方法；不在 CLI 读写开关，其余旧配置键不在本片改写。
- 声明：catalog `org/context.py` 声明 `org.admission.status/configure`；
  catalog `link.py` 同步 link.list/stats 变动字段；`impl/ipc/router.py` 只绑定。

RED：`[新] tests/daemon/org_membership/admission/{test_policy,test_observe,test_boundaries,test_revocation,test_bypass}.py`；
修改 `tests/daemon/links/{test_model,test_policy,test_store}.py`；
`tests/daemon/links/admission/{test_ingress_link_unknown,test_dispatch_link_hold}.py`；
`[新] tests/integration/org_membership/test_admission.py`。
相同 owner 的 A-only/B-only agent；有旧有效 link 但无 shared org；主人和本人 agent 在个人 org；
user↔agent 双向、user→user、本地/远端 user delivery；explicit bypass；未同步/已删除 org；
Casdoor remove 与并发重加；回复和持久重投不丢 inbox、不二次记账。

验收：A3/A7 的本版 observe 断言；关闭 A4 的按个人 org 放行证据。
A3/A7 真实拒绝继续标 0.5.5.4 NOT_RUN，不能报全量 A1–A9 PASS。
错误：新增已裁 `NO_SHARED_ORG`、`ORG_POLICY_DENIED`；observe 不向成功发送伪造错误退出码。
事件：`org.admission.refused` 保留设计字段 sender/recipient/sharedOrgs/reason，
增加拟定 mode/wouldRefuse/decisionId/policyRevision/projectionState；字段按 §6 冻结。
契约：修改 `contract/wire/send-links.{md,json}`、`contract/org-network/membership.md`；
`[新] contract/fixtures/org-membership/decisions.json`；OpenAPI/CLI surface/错误码金样本。

### N1 — 设备重试预算和 dormant（L，拆预算 M / sidecar M / 需求唤醒 M）

前置：P0；N1 核心可与 O1 并行。命令/视图依赖 O2a，发布前必须三子片一起验证。
固定预算为连续失败 8 次；“约 10 分钟”不是固定时间承诺。

范围：

- 设备维度共享预算，不按 org/space 叠乘。第 8 次失败后 dormant，不再定时拨远端。
- 停 supplier probe 只是其中一条路径（C19–C21）。预算必须覆盖 sidecar 的实际远端拨号，
  slot reconcile 不能复活 dormant，后台文档同步和 outbox 重投不能冒充“新需求”。
- 新消息、仅该设备持有的文档读取、`org network retry <device>`、真实上线 presence 各唤醒一次。
  相同需求/重复 presence coalesce；失败后回 dormant，不重置出另一轮 8 次自动重试。
- reachability 只写本地状态，不发布到 OrgFS devices；`org network` 展示预算/次数/最后尝试时间。
  授权拒绝/坏文档不算设备离线；不得借 wake 扩大允许设备集合。

文件：

- daemon：`[新] impl/org/membership/reachability/{__init__,budget,state,service}.py`；
  `impl/orgfs/network/{supplier_health,recovery,queries}.py`；`impl/orgfs/document/effects.py`；
  `impl/forwarding/slots/reconcile.py`；`impl/forwarding/controller.py`；
  `impl/transport/presence_actor.py`；`impl/org/network/status/view.py`；
  `impl/application/messaging/inbox_surface/send.py`；catalog `org/network.py`。
- sidecar：`sidecar/hyprial-tailcat/internal/engine/slots.go`；
  `[新] sidecar/hyprial-tailcat/internal/engine/reachability/{budget,state}.go`；
  `sidecar/hyprial-tailcat/internal/engine/engine.go`（预算装配）；
  `sidecar/hyprial-tailcat/internal/proto/message.go`（命令解析与状态字段）；
  `[新] sidecar/hyprial-tailcat/internal/proto/reachability.go`（新字段校验）；
  `sidecar/hyprial-tailcat/internal/serve/serve.go`（分发与状态投影）；
  `[新] sidecar/hyprial-tailcat/internal/serve/reachability.go`（新命令委托）。
  engine.go 现为 915 行（:1-915），先提取到 reachability 子包再加装配，不能突破 1000 行。
  当前 map-peer 的 parser/handler 分别在这两个文件 :329-340 和 :158-170；新增 wake/暂停语义先过 R7。
- shell：`impl/cli/commands/org/commands.py`（网络命令委托）；
  `[新] impl/cli/commands/org/context/network.py`。

MethodSpec：`org.network`、`org.network.retry`，retry 只接受经过 org 设备解析的 device，不接受任意地址。
RED：`[新] tests/daemon/org_membership/reachability/{test_budget,test_wake,test_persistence,test_multispace}.py`；
`[新] sidecar/hyprial-tailcat/internal/engine/reachability/budget_test.go`；
`[新] tests/integration/org_membership/test_dormant.py`。
fake clock 8 次后推进 10 分钟拨号为 0；再逐个测试四类唤醒，各恰 1 次；
多 space/并发 wake/daemon 与 sidecar 重启不重置；真实 10 分钟观测含远端 dial 和本地热循环计数。

验收：关闭 A6；仅 mock 时 A6 真实运行仍 BLOCKED。
错误：复用设备不存在/权限原码；dormant 是正常状态，不新增“发送失败”码。
事件：`org.device.reachability`（device/orgs/state/failures/budget/dormantSince/lastAttempt/wakeReason）。
契约：membership.md、`contract/wire/orgfs-sync.md`、`contract/sidecar-tailcat/README.md`、OpenAPI、CLI surface；
`[新] contract/fixtures/org-membership/reachability/cases.json`。
R7 未冻结 sidecar 预算职责和 wake/暂停 wire 语义前，N1b 只写 RED，不开始产品改动。

### D1 — Casdoor-first org delete（M；依赖 O1/O2b；可与 O3/N1 并行）

范围：预览并确认精确 org/group/space/设备集合，先删 Casdoor group，后标 OrgFS deleted。
成员节点收到 tombstone 按 leave 处理，并清理失效 current。
保留数据历史，不直接 purge。失败留 durable 操作阶段，重跑续作；不得重建已删 group。
个人 org 删除会违反“每用户至少 1 个个人 org”，须先过 R5，不能默许普通 delete 删除个人 org。

文件：

- daemon：`[新] impl/org/network/lifecycle/{__init__,delete,journal}.py`；
  `impl/org/network/{service,ipc,directory}.py`；
  `impl/org/network/casdoor/{client,membership,sync}.py`；
  `impl/org/network/tailnet/__init__.py`；`impl/forwarding/membership/maintenance.py`；
  catalog `org/lifecycle.py`。
- shell：`impl/cli/commands/org/{commands,removal}.py`，只显示 daemon preview/确认并 execute。
- 外仓：Casdoor delete-group 最小权限端口，由 infra owner 在隔离环境交付；不借服务 admin token。

MethodSpec：`org.delete.preview/execute`，execute 只消费冻结 operationId，不接受替换 group/设备列表；
旧 `org.delete` 声明为兼容适配并走同一确认机制；`org.leave` 同步成员清理声明。
RED：`[新] tests/daemon/org_membership/deletion/{test_order,test_resume,test_remote_leave,test_authority}.py`；
`[新] tests/shell/cli/org_membership/test_delete.py`。
Casdoor 失败 0 tombstone；删成功后 OrgFS 失败可重跑；不存在 group 与权限失败分开；
不能被旧 create intent 复活；最后 authority 处理经显式确认，不被普通 remove 替代；
agent 至少一归属保持；成员节点列表消失。

验收：关闭 A8；A9 不回退。错误沿 Casdoor/权限/操作冲突原码；若需新码先补协议评审。
事件拟名 `org.deleted`；复用 `org.member.changed`，结果明确 casdoorDeleted/orgfsMarked/tailnetPhase。
契约：membership.md、`contract/org-create-recovery.md`、OpenAPI、CLI surface；
`[新] contract/fixtures/org-membership/deletion/cases.json`。
R5/infra 未交付时为 BLOCKED；不得通过删除 LAST_AUTHORITY_REFUSED 一行宣称完成。

### H1 — 向 #2077 第二段交接（S 接口验证；依赖 O1/O2b/O3，协议 R8 另设门）

#2078 交付：`personal_org_for(user)`、规范 agent/化身归属查询、受保护 OrgFS 读写端口、
可验证的成员/receiver 投影、迁移报告、Casdoor 与空间绑定稳定引用。
由 daemon 公共面提供；provider 不解析 personal 名称，不 import 另一域 impl，不复制目录数据库。

精确接点：`daemon/interface.py`、`identity/interface.py`；
`daemon/impl/org/membership/{personal,store,projection}.py`；
`[新] tests/integration/org_membership/test_provider_handoff.py`；
`[新] contract/fixtures/org-membership/provider/cases.json`。
本片不编写 crypto、grant 或启动取钥代码。

进展（2026-11-03，`intg/org-membership`）：按 #2195 冻结契约 F-4，daemon 公共面先提供过渡适配器所需的两项：
`personal_org_for(owner)` 与只读的 `resolve_personal_org(fs, owner) -> PersonalOrgSpace`
（`state` 为 `ready` / `pending` / `unavailable` 并带 `reason`；不创建、不加入、不绑定、不调 Casdoor；
只有同名空间唯一、属于该 owner、个人 org 元数据已发布时才 `ready`）。创建仍只经 `org.personal.ensure`。
存储访问走可替换端口 `OwnerStorePort.open(owner) -> OwnerScope`（只接受相对路径的 stat/listdir/mkdir/read_text/
条件 write_text），当前唯一适配器为 `PersonalOrgOwnerStore`；非 `ready` 抛 `OwnerStoreUnavailable(reason)`。
#2323 S3 落地后换 owner-home 适配器，provider key 代码不变。
H1 其余项（受保护 OrgFS 读写端口、provider handoff 集成测试与 fixtures、交接验收 1–n）未做，仍依赖 O2b/O3。

交接验收：

1. key owner=A、agent 属多个工作 org、宿主=B 时，grant 权威位置仍只在 A 的个人 org。
   owner/host/current org/agent 工作 org 变动不能悄悄搬动 grant 权威。
2. B 不成为 A 个人 org 的成员；不得因 grant 开放 A 的 receivers、其它 grant 或全部空间。
3. 用户签名/加密钥未交付时，#2078 端口测试可 GREEN，但 provider 生产发布仍 BLOCKED。
4. 多 org grant 位置的 #2077 R3 已由 Q7=A 解决；稳定 agent move 化身迁接证明仍由 provider/transfer 线交付。

对旧计划的明确改写：

- #2077 **E1 纯模型/crypto** 无需等 #2078；若任务单称“E1 生产发布”，应指 OrgFS 适配的启用门。
- E2a（信封存储，旧派单也称 E1 publication）与 G1（grant 发布）消费上述端口。
  O1/O2 解除“没有个人 org/归属”的阻塞。
- **不能立即解除跨用户宿主取回的生产阻塞**：R8 需冻结受限读/分发协议；UK/T7/签名准入/撤销新鲜度也仍独立阻塞。
- #2077 设计 §3.1 和原计划 G1 的“agent 所在 org”路径必须更新为 key-owner personal org。
  实际 provider 子路径由 provider 契约冻结，本计划不另造第二条 grant 记录。

此片不新关闭 A1–A9，不新增错误码/事件。
契约：membership.md 写 handoff 协议；同步 #2077 的 `contract/identity/provider-keys.md` 和 grant fixture；
仅端口调整无新增 IPC 时不增加空 OpenAPI 方法，引用消费方法的生成快照。

## 4. 并行、串行和集成顺序

```text
P0 → O1a(个人 → 记录 → receiver) → O1b(升级迁移)
                              → O2a(current) → O2b(成员 → 创建) → O3(策略 → 边界 → 审计) → H1
P0 → O2c(login) ───────────────────────────────────────────────────────────────┘
P0 → N1a(预算) → N1b(sidecar) → N1c(wake/CLI；等 O2a) ─────── 发布门
                       O1/O2b → D1(delete；等 infra/R5) ─────── 发布门
```

- P0 的 shell 提取、admission 提取可分 worktree 并行；interface/router 单写。
- O2c、N1 核心、Casdoor delete 隔离端口可与 O1a 并行准备。
- O1a 存储协议未定前，O1b 只能写 inventory/故障 fixture，不能启用 apply。
- O2b、O3 可提前写纯 RED；删除 owner 特权与新投影接入必须同版本集成，不能先发布半套。
- H1 的端口适配可在 O1 类型稳定后并行；handoff 接受需 O3 证明没有新增 owner 通信 bypass。
- `org/network/service.py`、catalog/org、squire profile、registry_ops、sidecar engine、OpenAPI 由各自集成 owner 合入。
  worker 分支只改所辖子包；跨片文件通过集成节点串行合并。
- 最终版本门包含 O1–O3、O2c、N1、D1 的证据。H1 只解除已满足的 ORG 前提，不替 #2077 签发 PASS。

## 5. Owner-shortcut 审计

在 §1 产品提交的只读导出上执行了任务指定搜索。
系统 PATH 没有 rg，使用已安装的同一 ripgrep 二进制：

```sh
git archive 178d60063595e031a94d692af6bbde191c55c2ab | tar -x -C /tmp/org-plan-dev-178d60063
cd /tmp/org-plan-dev-178d60063
RG=/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/node_modules/@vscode/ripgrep-darwin-arm64/bin/rg
"$RG" -n 'owner_of|is_owner_pair|same-owner|bare_owner'
"$RG" -n 'owner.*==|==.*owner|owner.* in |in .*owner' src/hyprial/daemon/impl/{links,dispatch,composition}
"$RG" -n 'same_owner|owner_pair|sender_owner|recipient_owner|owner.*bypass|bypass.*owner|owner.*exempt|exempt.*owner|owner.*privilege' src
```

第一条搜索共 **140 个匹配行**。完整 file:line 清单见附录 A，覆盖产品、测试、契约、历史文档。
不是每个同词命中都是授权；保留项必须标明用途。
补充搜索在产品 src 命中 15 行，均为下表已有的 links/dispatch 条目。

| 位置（产品路径相对 src/hyprial/） | 处理片与理由 |
|---|---|
| `daemon/impl/links/model.py:39,98,101,104,181,271,286` | O3 删除 is_owner_pair 特权、导出及调用；self 去重保留 |
| `daemon/impl/links/model.py:43,89`；`links/service/views.py:74` | owner_of 仅显示可保留；加测试证明不参与授权 |
| `daemon/impl/links/policy.py:31,167,192,253,282` | O3 删除 governed/degree/victim/shrink 的 owner 例外 |
| `daemon/impl/links/service/gate.py:105` | O3 删除 owner 的 W 豁免 |
| `daemon/impl/links/service/views.py:134` | O3 删除 add 的 owner 限制，保留规范端点/内部身份检查 |
| `daemon/impl/composition/links.py:235,253,294` | 本机身份与 hosted writer 的 author 选择保留；不是通信用 owner 授权。O3 回归 hosted agent |
| `daemon/impl/forwarding/membership/hardening/authority.py:42-43` | 保留；明确 Tailnet authority 的本地选择，已有独立裁定（该文件 :1-7）。不把通信模型改动扩大成 Tailnet 选主 |
| `identity/impl/org_directory/models.py:256-260` | 保留路径编码；不赋通信权限 |
| `identity/impl/principal.py:139`；`agents/registry/_records.py:699`；`daemon/impl/pac/views/context.py:52` | 保留，恰是禁止同 owner 推断身份/化身的围栏 |
| `shell/impl/login/flow.py:199,441,587` | 保留，同 owner 重登录事务注释；不是 D6。D6 在 CLI login/flow.py:698 |
| `daemon/impl/runtime/forwards.py:535` | 保留通知函数名；O3 检查通知投递也经过新准入或可信内部来源 |

### 5.1 指定模式未命中的授权/绕行点

| 位置 | 处理 |
|---|---|
| `composition/links.py:98-156,168-176,731-738` | O3：D1 默认关闭、去掉全局 owner/member 联集作为准入依据；保留空间 owner 作为组织角色事实 |
| `links/service/gate.py:152-175,629-631`；`links/store.py:63-66,304,709-722` | O3：D2 删除 `_owner_is_member` 与成员联集授权；known/local 快速路径之前先运行共享 org 观察 |
| `links/policy.py:160-165`；`dispatch/admission.py:120-153`；`application/messaging/inbox_surface/link_send.py:329-366` | O3：D4，独立覆盖 user 两端；不得因 link_governed=False 跳过 org admission |
| `application/messaging/inbox_surface/link_send.py:401-416` | O3：只为本 owner 本机 agent 记 ledger 的条件不是授权。核对 hosted agent 不丢记账，不能复用于 orgs_of |
| `squire/addressing.py:411-456`；`identity/impl/users/profile.py:227-318`；`squire/actors/profile.py:109-162` | O1a/O1b：D5，切 OrgFS receiver；profile 的 owner 核验和身份绑定保留 |
| `shell/impl/cli/commands/login/flow.py:698` | O2c：D6，删除默认 accept_all |
| `org/network/policy.py:6,58,82-90`；`orgfs/storage/store/admission.py:495-511` | 保留 org owner 的管理/写入角色；这不等于 agent owner 的通信豁免。O1a 另加受保护记录规则 |
| `dispatch/admission.py:237-266`；`links/service/gate.py:190-209` | O3：不能让 ingress fail-open 或历史 admitted ID 成为“跳过新 org 观察”的理由；原 inbox 幂等/残留处理仍保留 |

O3 合入前重跑审计，检查所有新增/遗留 `is_owner_pair` 行。
历史 WORM 不删除；活动 send-links 设计/contract 的旧特权描述由 O3 更新并链接新裁决。

## 6. 迁移安全和 observe 运维边界

### 6.1 唯一执行路径

`upgrade.steps.run` → `run_upgrade_steps` → `steps/org_membership.py` → daemon 成员迁移服务。
runner 保持锁和 marker 唯一 owner。步骤只通过运行中 daemon/注入端口访问 OrgFS 与 identity。
不得在 step 里另开一份 registry 或第二个 OrgFS replica。
升级 shell、doctor 和 daemon IPC 共用这个入口；daemon handler 不递归请求自身。
外部执行用客户端适配，进程内执行注入同服务。调用上下文必须显式是目标 Hyprial home（C16）。

### 6.2 实体级阶段和报告

状态按 `(migrationVersion, user/agent incarnation)` 记入本地 journal。
共享身份和首写冲突检查不能只依赖这个本地锁。

1. **INVENTORY**：固定登录用户、agent URI/化身、已接受 org-context 摘要及 profile 摘要。
   members 中 squire 字段只做明确代理引用；residents 用 name+node 解析唯一 agent。
   同名/错 node/非规范 owner/缺 accepted context 均报冲突，不猜归属（C8）。
2. **PERSONAL_READY**：Casdoor 确认 personal group，取唯一空间绑定，再加入本机 host。
   其它用户的个人 org 只消费已验证投影；本机无权代建时保留 pending。
3. **MEMBERSHIPS_WRITTEN**：已有明确归属映射到所有有效 org；没有映射才落 agent 主人的个人 org。
   squire/user-proxy 必须有个人 org。迁移不静默删除原工作归属。
4. **RECEIVERS_WRITTEN**：采用 create-only + 同一已确认空间的版本比较；已有相同记录是 no-op。
   首次成功记录保留，另一节点不同值列入 conflict，不覆盖、不按机器时间最后写入胜出。
   异步复制下的并发首次写不能由本地 CAS 证明，必须先关 R1。
5. **VERIFIED**：逐 agent 读回非空归属；逐用户读回个人 org、host、receiver。
   强制遮住旧 profile 的 receiver 字段后，接收机解析仍成功。
6. **REPORTED**：写原子私有报告并放入 step marker data。
   包含 schema/operationId/node/对象/来源摘要/目标 org/阶段/created/reused/conflicts/failed/验证结果。
   `failed[]` 非空留 failed，doctor 可重跑；不得吞异常后标 done。

在 Casdoor ensure、空间绑定、成员写、receiver 写、验证和报告前后逐点注入崩溃。
重跑仅补未完成步骤。已创建的 group/空间不因本地后续失败而盲目删除。
迁移完成后出现人工撤销/新化身/新 revision，续作必须报漂移，不能重放 inventory 复活权限。

### 6.3 observe 记录什么

每个 would-refuse 决策至少记录：

- sender、逻辑 recipient、sharedOrgs（排序）、reason；设计原字段保留。
- mode=observe、wouldRefuse=true、decisionId、message/operation 关联、site=send/ingress/dispatch/user-delivery。
- 投影版本/新鲜度状态、命中的 org 策略版本、实际结果 admitted/held/refused。
  原因未知时记录 unavailable，不能伪造 NO_SHARED_ORG。

事件不能含正文、邀请 token、receiver 凭据或 provider key。
高频重投按 decisionId 合并，记录 suppressedCount；不为每 tick 写一条日志。
持久事件写入走现有异步事件端口，inbox actor 的锁/事务内只读不可变投影（C14）。
GUI 读同一 daemon 事件/方法；本图不编写 GUI，也不承诺 unit 测试等于浏览器验收。

### 6.4 回滚

- observe 前/期间：保留旧 profile 和 org-context 原字节，不删新 OrgFS 记录。
  停止迁移写入，核对 journal 后可回到兼容旧版。已由其它设备更新的共享记录不得回滚覆盖。
  回滚二进制可能恢复旧 owner bypass，必须在回执中标明该安全退让。
- 强制切换后：0.5.5.4 单独验证切回 observe 的演练。设置新 admission mode 回 observe 可恢复应用投递；
  `links.bypass=true` 只解除 link hold，**不能代替 org 开关，也不能绕过 R3**。
  设计 §6 的“只恢复 bypass”不足以覆盖此组合，交负责人在 R3 确认。
- Casdoor 已删 group、成员已撤销、provider 已发布新 grant/revoke 后，不能称“直接二进制回滚即可恢复”。
  不自动重建 group、不撤销 tombstone、不恢复旧授权。只能显式补偿或向前修复。
- dormant 回滚独立于 admission；恢复定时探测不能扩大设备 allow 集。

## 7. 契约、测试和验收矩阵

### 7.1 MethodSpec 的统一完成条件

每个片列出的方法均先加纯声明和 binding，再加 CLI。
至少冻结 params/result/error.data、mutates、retry、idempotency、audience、confirmation、http_exposed。
幂等写入需要精确 operationId/目标/版本语义；preview/execute 不能接受替换参数。

`http_exposed=true` 只是生成投影，不启动 HTTP server（C18）。
受信 caller 和确认不能来自浏览器自报字段。新 read/write 都沿 daemon 原有身份校验边界。
未决定 HTTP 暴露的写方法先声明 false，不用默认放开换取 GUI 可调用。

每个行为片更新 `contract/daemon-request/openapi.json`，命令片同步 `contract/cli/surface.json`：

```sh
uv run python scripts/verify/dump_daemon_openapi.py --write
uv run python scripts/verify/dump_daemon_openapi.py --check
uv run pytest tests/daemon/ipc/contracts/test_method_specs.py tests/tooling/contracts/test_daemon_openapi_contract.py
```

`contract/fixtures/org-membership/` 根固定 7 个文件：
`records.json,admission.json,migration.json,defaults.json,commands.json,invites.json,decisions.json`。
reachability/deletion/provider 各用子目录。
测试根采用 `tests/daemon/org_membership/` 下按片分包；每包最多 5 个测试文件，留 helper 余量。
shell 的 `org_membership/` 直接文件为 defaults/members/login_invites/delete 四个测试，余量 3。

### 7.2 执行纪律

开始产品/测试结构改动前，先跑结构和边界门。
每片先跑上述精确焦点文件，再按集成计划扩大范围。
最终全量测试由负责人排独立集成节点，不在本计划文档图运行：

```sh
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run --with libcst==1.9.0 pytest tests/
./contract/run-all.sh --isolated-only
uv run ruff check .
```

E2E 新文件：`contract/e2e/org-membership/{run.py,scenarios.json,README.md}`；
接入 `contract/e2e/{scenarios.json,validate.py}` 和 `contract/run-all.sh` 的 hermetic 分组。
按 A1–A9 分别输出 PASS/FAIL/BLOCKED，并为 observe 的待 enforce 断言单独报 NOT_RUN。
合成 homes、loopback 传输和隔离 Casdoor；daemon 测试显式 `HYPRIAL_NETWORK_ISOLATED=1`。
真实跨设备/外部 Casdoor 测试仅使用批准的测试账号、设备和网络资源。
不得读取、复制或改动生产 home/daemon/凭据。10 分钟 dormant 观测不能用 skip 代替。

| 验收 | 关闭片 | 必须留的证据 |
|---|---|---|
| A1 | O2a+O2b | 两 org；current B；work/member 默认 B；显式 A 覆盖；daemon 直调与 CLI 一致 |
| A2 | O2c | 2 个 invite 逐条 yes/no；非 TTY 0 join；JSON 输出一个对象 |
| A3 | O3 | observe：NO_SHARED_ORG 事件且仍投递；Y 入 A 后按 A 允许。真实拒绝待 0.5.5.4 |
| A4 | O1+O3 | 主人和 agent 经 personal org 互通；日志 via=personal，零 owner shortcut |
| A5 | O2b | 最后 org 移除报 AGENT_LAST_ORG；并发移除也不产生零归属 |
| A6 | N1 | 第 8 次失败后 10 分钟零远端 dial；消息唤醒恰 1 次；另验文档/manual/presence |
| A7 | O2b+O3 | Casdoor 撤成员后有效投影更新；agent would-refuse；本地 OrgFS 改动不反写。真实阻断待 0.5.5.4 |
| A8 | D1 | Casdoor 先删；tombstone 后写；其它节点按 leave 不再列出；阶段故障续作 |
| A9 | O1b+O2b | 固定对象逐一验证；人人 personal、agent 非空归属、OrgFS receiver；两节点升级幂等及冲突报告 |

发布检查同步 `.agents/skills/hyprial-e2e/SKILL.md`、操作指南和 CLI 帮助。
hyprial-ops 用户级 skill 由其 owner 同步，不能把用户私有目录提交本仓。

## 8. 风险和待关闭问题

以下不重开 Allen 已裁选项，只列影响实施的协议/验收缺口。

| ID / 决策 owner | 问题与阻塞范围 |
|---|---|
| R1 / 研发负责人 + OrgFS owner | personal-<userKey> 的 userKey 编码、Casdoor sub 映射与 63 字符规则需冻结（C24）。两个设备首次 create/receiver 首写怎样得到同一空间和唯一首写者？本地锁/create_only 不能证明跨副本唯一。冻结跨节点绑定/序列化协议，阻塞 O1a/O1b apply。不得用改名、时间最后写入或双空间代替。 |
| R2 / 研发负责人，新增授权选择交 Allen | admin/owner 同意的申请/批准证据如何存储、谁签、怎样让离线申请可续作？agent 同时跨多个 org，两个节点并发移除最后归属如何串行？外部 Casdoor 强制移除时如何先保 personal，再失效工作归属？阻塞 O2b；可先写“未批准则拒绝”的测试，不能暗设主人自动批准。 |
| R3 / 研发负责人 | observe 的精确范围与 link-state 默认关闭组合：旧 link hold 是否仍生效，怎样确保 would-refuse 不因新的 org 判定断信？设计 §6 的 bypass 回滚与 §5“bypass 仍走 R3”有差别，需明确独立 admission mode 的名称、权限和回滚契约。阻塞 O3 发布；不得提前开启 0.5.5.4 enforce。 |
| R4 / 研发负责人 + identity/OrgFS owner | Casdoor 撤用户后 agent 的有效归属失效规则、重加 epoch、agent owner/host/sponsor 不同的映射需固定。members/residents 不能证明管理员已授权。投影分区/未知新鲜度、已经 admitted 的消息和撤销后的回复怎样处理？阻塞 O2b/O3 的 A7；不能用 owner 成员身份直接授权 agent。 |
| R5 / 研发负责人 + infra/Tailcat owner | delete-group API、最小权限、group absent 与 denied 的可区分回执尚需交付（C5）。最后 Tailnet authority 怎样显式停机？personal org 的 delete/leave 如何保持每用户至少一个？阻塞 D1，不能让 Casdoor 已删而 Tailnet 静默残留。 |
| R6 / 研发负责人 | current 指向 left/deleted/sync-problem 时，清空还是返回哪个错误？kind=personal 是否能承载 work 命令？默认候选的 eligibility 要统一。阻塞 O2a 的边界 fixture，不改已裁的显式/current/唯一/ORG_REQUIRED 顺序。 |
| R7 / 研发负责人 + transport/sidecar owner | 设备重试预算在 daemon 和 sidecar 间谁持唯一拨号计数、wire wake/暂停如何传递？协议文件见 N1；必须冻结新增 wire 字段和计数归属，再开 N1b。还需证明 Zenoh 本地 slot 重连没有热循环。文档目前只给已核对的拨号事实（C20/C21），不臆造现有 pause API。 |
| R8 / #2077 owner + 安全评审，新增权限交 Allen | Q7=A 已定唯一 grant org，但非成员 B 怎样只取被授信封/grant？个人 org 仍不能邀请 B。受限密文读取、主人离线时可用性、元数据可见范围、签名准入和撤销新鲜度仍需协议。阻塞 H1 的生产 E2a/G1 交接；不能造 owner 消息 bypass 或把 personal org 变公开。 |
| R9 / 负责人 + transfer/PAC owner | canonical agent URI 含 owner/node；move 与同名重建的归属继承要用稳定身份/化身迁接证明。PAC 临时 worker 默认 current 还是创建者 personal 的可信创建者来源也需核对。阻塞 O2b 对这些创建路径的验收，不能遗漏后声称每个 agent 都有归属。 |

### 8.1 研发负责人裁决（2026-10-31）

- **R1**：个人 org 名为 `personal-<userKey>`（userKey 即 user-key 设计里的键）。超过 63 字符时取 `personal-<前 40 字符>-<完整 userKey 的 sha256 前 8 位 hex>`。跨节点唯一性由 Casdoor 决定：以“创建 group，冲突即加入”的方式建 Casdoor group，创建成功的节点是首写者，其余节点加入同一 group 及其空间。不在节点本地生成第二个空间。receiver 记录首写冲突时只报告、不覆盖（设计 §6.3）。
- **R2**：**agent 永远属于主人的个人 org**，这条归属不能移除（删除 agent 时才消失）。工作 org 是额外归属。因此“移除最后一个 org”不存在并发问题：A5 改为“从个人 org 移除被拒（AGENT_PERSONAL_ORG）”。Casdoor 撤销工作 org 成员时，只失效工作归属。加入工作 org 的申请与批准各是一条 OrgFS 记录，都写在目标 org 的空间：申请由主人节点写，批准由 admin/owner 节点写，按普通 OrgFS 设备签名。申请离线时留在该空间，批准人用 `org member pending` 看到。未批准一律拒绝，主人不能自动批准。
- **R3**：新增独立设置 `admission.mode = observe | enforce`，0.5.5.3 默认 observe，只能由 home 主人在本机 CLI 修改。observe 期间旧的链接逻辑（含现有 bypass 默认值）**原样决定投递**，新的共享 org 判定只记录 would-refuse 事件，不影响投递。D1–D4 的删除与 enforce 一起在 0.5.5.4 生效。0.5.5.3 只落代码路径和 observe 记录，不改现有放行行为。回滚就是把 mode 设回 observe。
- **R4**：Casdoor 移除用户后，投影里该用户的 agent 在该 org 的归属立刻失效（epoch 递增），重新加入要重新批准（新 epoch）。已经投递的消息不追回；撤销之后的回复，在发送时按当时的归属判定。归属只看 agent 的主人 userKey，host 和 sponsor 不参与。
- **R5**：个人 org 不能 delete 或 leave，只随用户账号删除。D1 等 infra 交付 Casdoor delete-group API 和可区分的回执，并明确 Tailnet authority 的停机步骤，在此之前不开工，可以顺延到 0.5.5.4。
  - infra-ops 读数（2026-10-31，只读）：Casdoor 有 `POST /api/delete-group`，但有成员或子 group 时拒删，须先清成员。有权限的调用者能区分 Affected/Unaffected；hyprial 现用的非 admin service user 无权调用，且“无权”与“不存在”返回同一句 `Unauthorized operation`。最小权限能否按 group 收窄尚无实测。Tailnet authority 停机目前没有任何能力（org delete 抛 LAST_AUTHORITY_REFUSED）。
  - **裁定：D1 顺延到 0.5.5.4。** 前置条件：专用 delete 凭据和最小权限 dry-run；删除前先 GET 确认存在；Tailnet authority 显式停机能力。
  - **最小权限实测（infra-ops，2026-10-31T03:47–03:52Z，临时授权，测完已清理，DB 复核残留 0 行）**：
    - Casdoor 原生的 `/api/delete-group` **不能**用 permission 收窄。五种 policy 测试用户全部得到 Unauthorized：API `hyprial/delete-group` POST、API `/api/delete-group` POST、API `delete-group` POST、API `hyprial/*` `*`、按 group 授予 Read+Write。同一凭据访问自定义端点 `/api/hyprial/group-member` 时能通过鉴权（返回的是参数错误），说明权限机制本身有效，只是这条原生路由不受 policy 管辖，只有 admin 能调用。
    - 存在性判定：在 admin 权限下，get-group 返回 `data:null`、删不存在的 group 返回 `Unaffected`、真删返回 `Affected`，三种情况可以区分。非 admin 调用 get-group 同样得到 Unauthorized。
    - **裁定（研发负责人与 infra-ops）**：D1 的删除能力仿照 ensure-org-group，走打过补丁的端点 `/api/hyprial/delete-group`。admin 凭据只由本机 sidecar 持有（仅 127.0.0.1），daemon 不持有可删除的凭据。删除流程沿用 `--yes`、删前 GET 和六字段回执。仍在 0.5.5.4。
- **R6**：current 指向 left、deleted 或 sync-problem 的 org 时，下一条命令自动清除 current，在 stderr 提示，再按“显式、唯一、ORG_REQUIRED”的顺序解析。个人 org 可以承载 work 命令。候选只取 active 的 org。
- **R7**：sidecar 持唯一的拨号计数；daemon 只发 `wake(device)` 请求。新增的 wire 字段先进 `contract/`，版本化，由 transport owner 冻结后再开 N1b。N1a（daemon 侧状态与 GUI 事件）可以先做。
- **R8**：不阻塞 #2078 的 0.5.5.3 分片。B 取授权的方式，在 #2077 第二段里设计：由 key 主人的节点在 agent 启动时把密封信封直接交给 B 的节点，B 不读个人 org。生产发布继续等这一段。
- **R9**：归属跟随 agent 的稳定身份（UUID），不跟随 URI。move 和同名重建都继承 UUID 上的归属；新 UUID 只得到个人 org。PAC 临时 worker 只归属创建者（graph 发送者的主人）的个人 org，不自动加入 current org。

## 9. 本次文档交付验证

- PASS：完整读取任务书、#2078 已裁设计、#2077 两份输入；固定产品提交并只读核对引用。
- PASS：指定 rg 搜索已执行，共 140 行；附录 A 逐路径列行号，§5 区分删除和保留理由。
- PASS：本分支执行结构门与模块边界门，均 exit 0；仅静态检查。
- PASS：`uv sync --extra test` 后 `uv run ruff check .` exit 0；附录 A 与原始搜索 140 行逐项相等。
- UNKNOWN：产品编码、RED/GREEN、Casdoor/跨机、真实 10 分钟 dormant、GUI、升级/回滚均未运行。
- 本文档图按任务书不运行全量 `pytest tests/`。
- Contract-Impact：本提交只新增实施计划，不改已发布字段。每个实施片需同步其所列契约和生成快照。
- Skill-Impact：本提交不改变操作方式；实施发布门负责同步 E2E skill、帮助和指南。

## 附录 A：指定 rg 的完整命中位置

下列每个行号都是 §1 产品提交上的一次匹配；同一路径合并展示。
产品条目的处理见 §5。测试中旧 owner 特权用例在 O3 改为共享 org 用例；
identity/PAC 的“不得按 owner 猜身份”测试保留。contract 的 owner_of helper 只是测试身份读取，保留。
活动设计的旧特权说明由 O3 更新；归档和 WORM 证据不修改。

| 路径 | 匹配行 |
|---|---|
| `contract/e2e/_runner_shared/org.py` | 42 |
| `contract/e2e/_two_machine/image.py` | 60 |
| `contract/e2e/_two_machine/local.py` | 95 |
| `contract/e2e/_two_machine/phases.py` | 14 |
| `contract/e2e/_two_machine/ports.py` | 62 |
| `contract/e2e/run-two-machine-roundtrip.py` | 160, 434, 436 |
| `docs/design/kernel-isolation-and-rust-core-survey-2026-10-23.md` | 534 |
| `docs/design/send-links-impl/wp1.md` | 66, 67, 84, 85, 242, 295 |
| `docs/design/send-links-impl/wp2-composition.md` | 55 |
| `docs/evidence/cifix-1520-2026-10-19/remerge-boundaries.out` | 62350 |
| `docs/evidence/cifix-1521-2026-10-19/remerge-boundaries.out` | 62363 |
| `docs/evidence/daemon-request/a-container-c8d7/boundaries.log` | 68608 |
| `docs/evidence/org-structure-constrain-2026-10-14/wp2/red-rework2.txt` | 75 |
| `docs/evidence/org-structure-constrain-2026-10-14/wp2/red-store.txt` | 433 |
| `docs/evidence/org-structure-constrain-2026-10-14/wp8/c2/gates.txt` | 61112 |
| `docs/evidence/org-structure-constrain-2026-10-14/wp8/r2/r1-e2e-001-org.txt` | 4, 13 |
| `docs/evidence/refactor-c0-2026-10-01/scan-final/exports.json` | 146157, 146159, 146160, 146247 |
| `docs/evidence/refactor-c0-2026-10-01/scan-final/references.json` | 118810, 118824 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/collection-repair/product-collect-final.log` | 6347 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/faces/unified-preview-v15-final-noninbox-preview.json` | 1009, 1086, 1317 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/faces/unified-preview-v16-final-noninbox-preview.json` | 1009, 1086, 1317 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/faces/unified-preview-v17-final-noninbox-ready.json` | 1009, 1086, 1317 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/main-validation/target-main-full-product-r9-events.jsonl` | 14617, 14618, 14619, 14620 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/test-native-startup-final/native-owner-proof-after-final.json` | 4998 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/test-native-startup-final/native-owner-proof-before-final.json` | 4998 |
| `docs/evidence/refactor-joint-2026-10-02/tasks/test-native-startup-final/native-owner-proof.json` | 4986 |
| `docs/evidence/refactor-main-final-2026-10-03/main-collection-final-r2.json` | 3847 |
| `docs/evidence/refactor-main-final-2026-10-03/main-collection-final-r3.json` | 3847 |
| `docs/evidence/refactor-main-final-2026-10-03/main-collection-final-r4.json` | 3847 |
| `docs/evidence/refactor-main-final-2026-10-03/main-collection-final-r5.json` | 3847 |
| `docs/evidence/refactor-main-final-2026-10-03/test-layout-final-audit/expected-final-nodeids.json` | 3847 |
| `docs/evidence/tailcat-wheel-ci-main-2026-10-03/runs/joint-collection.log` | 3788 |
| `docs/evidence/tailcat-wheel-ci-main-2026-10-03/runs/joint-tests-collection-with-tool-deps.log` | 3788 |
| `docs/evidence/tailcat-wheel-ci-main-2026-10-03/runs/joint-tests-collection.log` | 3788 |
| `docs/evidence/tauri-v2-integration-2026-10-03/runs/main-collection-final-r2.log` | 3848 |
| `docs/evidence/tauri-v2-integration-2026-10-03/runs/main-collection-final-r3.log` | 3845 |
| `docs/evidence/tauri-v2-integration-2026-10-03/runs/main-collection-final-r4.log` | 3845 |
| `docs/evidence/tauri-v2-integration-2026-10-03/runs/main-collection-final-r5.log` | 3845 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-final.json` | 379 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-final.log` | 377 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-owner-final.json` | 3844 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-owner-final.log` | 3842 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-r1.json` | 369 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-r1.log` | 367 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-r2.json` | 379 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-r2.log` | 377 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-topic-owner-final.json` | 3847 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-topic-owner-final.log` | 3845 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-topic-owner-r1.json` | 3832 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-after-topic-owner-r1.log` | 3830 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-before-final.json` | 6792 |
| `docs/evidence/test-layout-refactor-2026-10-03/collect-before-final.log` | 6790 |
| `docs/evidence/test-long-files-2026-10-03/outer/preimages/tests/kernel/actors/test_actor_identity.py.txt` | 817 |
| `docs/notes/evidence/pac-owner-uri-mutations/M1-d1-shape-boundary.json` | 6, 11 |
| `docs/notes/harness-t1-steer-2026-10-13.md` | 337 |
| `docs/notes/in-channel-fail-loud-notices-2026-10-13.md` | 34, 39, 136 |
| `docs/notes/rehearsal-login-u5-2026-09-04.md` | 9, 23 |
| `docs/notes/review-agent-transfer-at08-2b-runtime-2026-09-28.md` | 66 |
| `scripts/archived/rehearse_login_switch.py` | 5, 10, 557, 577 |
| `src/hyprial/daemon/impl/composition/links.py` | 235, 253, 294 |
| `src/hyprial/daemon/impl/forwarding/membership/hardening/authority.py` | 42, 43 |
| `src/hyprial/daemon/impl/links/model.py` | 39, 43, 89, 98, 101, 104, 181, 271, 286 |
| `src/hyprial/daemon/impl/links/policy.py` | 31, 167, 192, 253, 282 |
| `src/hyprial/daemon/impl/links/service/gate.py` | 105 |
| `src/hyprial/daemon/impl/links/service/views.py` | 74, 134 |
| `src/hyprial/daemon/impl/pac/views/context.py` | 52 |
| `src/hyprial/daemon/impl/runtime/forwards.py` | 535 |
| `src/hyprial/identity/impl/agents/registry/_records.py` | 699 |
| `src/hyprial/identity/impl/org_directory/models.py` | 256, 257, 258, 260 |
| `src/hyprial/identity/impl/principal.py` | 139 |
| `src/hyprial/shell/impl/login/flow.py` | 199, 441, 587 |
| `tests/daemon/links/composition/test_adapters.py` | 722 |
| `tests/daemon/links/test_model.py` | 19, 23, 73, 74, 75, 76, 77, 78, 79, 80 |
| `tests/daemon/links/test_store.py` | 180, 570, 746, 794 |
| `tests/daemon/pac/graphs/test_pac_graph.py` | 507 |
| `tests/daemon/pac/projection/test_work_items.py` | 90 |
| `tests/kernel/actors/identity/test_resolution.py` | 250 |
| `tests/support/links/org.py` | 544, 643 |
