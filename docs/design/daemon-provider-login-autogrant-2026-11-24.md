# Daemon 级 Provider 登录与自动授权：与模型连接方案对齐的设计与施工规划（WIP）

状态：WIP 设计，不改变现行契约，不授权实现或合并。文中新方法、字段、错误原因均为**拟议**。
读码基线：`origin/dev` `b36e97da4bd8deb638c1279b061dcf0dac93a9a8`（2026-11-24）。
对齐对象：模型连接设计 [#3134](https://code.hyprial.com/HyprialOS/hyprial/pulls/3134) head
`d617e660626b3bbac707bc3b593cc49fe27ab6ca`、实施第一片 [#3141](https://code.hyprial.com/HyprialOS/hyprial/pulls/3141)
head `2bac48b84173b84afd0a9f22c38c7ea5c9150d0c`。下文“#3134 §x”指其
`docs/design/model-connections-harness-auth-2026-11-24.md` 的章节。
接手人：Allen（负责人）。第 7 节是待拍板项。
修订：已按独立只读 critic（codex `gpt-6.2-astra`）的 13 条意见逐条核实并修订，处置记录见 §9。

## 0. 结论先行

1. **不另建凭据库。** 任务单设想的 `<HYPRIAL_HOME>/secrets/providers/` 正是现有 user-provider
   明文条目目录（`identity/impl/agents/runtime/secrets.py:257`、`:351`、`:387`），#3141 正把它原地升级为
   `hyprial-model-secret/v2`（#3134 §4.2“不建立第二份 vault”）。本方案的 “Daemon Provider Credential
   Vault” 只是一个**只读登记视图**，覆盖现有存储。新增的持久数据都是非秘密的，且都放在 desired-state
   SQLite：#3134 PR3 `model_bindings` 中的来源字段（§4.3），以及一张按来源记录授权策略的小表
   `source_grant_policies`（§4.2）。策略**不写进** v2 秘密记录，因为 #3141 的 v2 记录是封闭 schema。
2. **自动授权只在创建/配置时生效，启动时不推断。** 策略在 `agent.create`/`agent.model.configure`
   时派生一条**显式 binding**，并记录来源 `origin=policy`。启动只读 binding，与 #3134 §2.2
   “显式绑定流程不调用机器默认凭据 seed”一致。
3. **Magpie = 一种 router 类型的模型连接，不内置。** Magpie 的多账号、分组、限流切换、网关 Key
   发放都留在 Magpie 内部。Hyprial 只精确绑定“一个连接 + 一个手填模型 ID”，不做跨连接 fallback。
   这与 #3134 §6 的结论相同（外部 endpoint 可选，捆绑另案）。
4. **只有一套 `provider login`。** 不新增顶层 `hyprial provider` 命令族。可共享的秘密（厂商 Key、
   网关 Key、Claude setup-token）都存为 user-provider 条目，登录时不再要求 `--actor`。setup-token 改走现有的
   local-operator 方法 `agent.secret.provider-write`，`agent.provider.login` 的契约**不改**（§4.1）。会自动刷新的
   OAuth 文件（Claude `.credentials.json`、Codex `auth.json`、Pi `auth.json`、agy token）仍只写入一个 Agent，
   新流程不复制它们，因为多份拷贝会导致刷新互相冲突（§4.5）。
5. **本 PR 只有文档。** 它只新增本文件一个文件，与 #3134、#3141、#3143、#3120 没有同名文件（§2.4）。
   代码切片在 #3141 合入后按 §6 的顺序开工。

## 1. 现状盘点与痛点

### 1.1 现有四类存储（实读 `origin/dev`）

| 代号 | 存储 | 写入面 | 授权面 | 启动消费 |
|---|---|---|---|---|
| R1 user-provider 条目 | `<HYPRIAL_HOME>/secrets/providers/<entry>.json`（`secrets.py:351-441`，flat/v1；#3141 增加 v2 连接） | `agent.secret.provider-write`、`agent.secret.provider-configure`（GUI） | registry SQLite `agent_secret_grants`（`secret_store/defaults.py:18-58`，含 `route_required`），`agent.secret.grant/list/revoke` | `identity/impl/agents/home/environment.py:47` `resolve_agent_secrets` → `:507` `compose_worker_child_launch`，以环境变量注入 |
| R2 owner-key | `<HYPRIAL_HOME>/state/provider-keys/store/<ownerKey>/`（`daemon/impl/provider_keys/storage/local.py:1-10`），签名信封（`identity/impl/provider_keys/`） | `user.provider.login/logout/rotate/list` | `agent.provider.allow/revoke/status`，签名 grant（`grants.py`、`policy.py:12`） | `daemon/impl/provider_keys/launch.py:105` `launch_resolver`，屏蔽同 grantId 的 R1 行 |
| R3 Agent 原生 Auth | `<agent_home>/secrets/native/<harness>/`（`native_credential.py:59-64` 文件白名单：claude `.credentials.json`、codex `auth.json`、pi `auth.json`+`models-store.json`、agy `antigravity-oauth-token`）；Claude setup-token 写入 owner Agent 的 SQLite `AgentSecretStore` | `agent.provider.login/logout`（必填 `actor`，`provider_credential.py:263-322`、`:366`） | 无独立授权：文件在谁的 home 就归谁；setup-token 走 `agent.secret.grant` 的 `provider-claude`（`:293`） | 各 harness 投影：agy `native_env.py:52` 原子拷贝到 `session_root`；Claude/Codex 按 W1E-1 方案原子拷贝并用 `link_directory`（`claude/runtime.py:1-14`、`codex/native_env.py:8-10`） |
| R4 机器默认 | registry SQLite `agent_secret_defaults`（只有 claude 一行，`defaults.py:52`）；`<state_dir>/provider-default.json`（旧的按名默认，`provider_credential.py:53`） | `agent.provider.login --default`、`agent.provider.default` | 创建时种入：`_grant_default_claude_locked`（`defaults.py:140`，由 `_home_create.py:442/598/682` 调用）；其他 harness 由 CLI `seed_new_agent` 复制宿主 vendor 登录（`commands.py:463`） | 种入后与 R1/R3 相同 |

R2 的门是 `settings.json` 的 `providerKeys.authority`，默认 `legacy`（`provider_keys/storage/gate.py`）。
生产环境的签名器默认关闭，会返回 `USER_KEY_UNBOUND`（`provider_keys/context.py` 头部决策块）。
所以**今天实际可用的只有 R1、R3、R4**。

### 1.2 四条数据流及断点

| 流程 | 数据流 | 断点 |
|---|---|---|
| `agent provider login <h> --actor A` | CLI 在本机读 `~/.claude` 等（`resolve_provider_sources`，`commands.py:56`）→ base64 上传 → daemon 写入 A 的 R3 | 凭据必须挂在某个 Agent 上；operator shell 不传 `--actor` 就报错（`commands.py:248`）；登出只作用于 A，从 A 拷出去的副本无人追踪 |
| `--default` / `agent.provider.default` | Claude：R4 引用“OWNER Agent + entry”；新建 Agent 时在 identity 层拷贝原生文件，或写入 `agent-shared` grant（`defaults.py:140-174`）；其他 harness：CLI 在创建后复制宿主登录 | 依赖一个“OWNER Agent”作为凭据容器；只种新 Agent，不回溯旧 Agent；种入的副本没有来源索引，默认改了已有副本也不会变；#3134 规定新流程不消费它 |
| `agent secret provider-write` + `grant` | 写 R1 条目 → 为每个 Agent 分别手工 `grant`（`grant-key` 可一次给多个 actor，`grant_key.py:42`）→ 启动时注入环境变量 | 每个 Agent 都要手工授权；grantId、revision、环境变量名都由人填；GUI 的连接（#3141 v2）与 CLI 的 flat Key 是同一个文件，但两种用法 |
| agy 原生凭据 | 原生 token：R3 `antigravity-oauth-token` → `sync_agy_oauth_token` 原子拷贝到 `<session_root>/antigravity-cli/`，源不存在时删除副本（`native_env.py:52-96`）；Key：`GEMINI_API_KEY` 走 R1 grant（`model_provider.py:780-806`） | 两条来源互不知晓；Key 缺失时只提示手工 `agent secret grant`（`:801-805`） |

### 1.3 已发现的不一致（记录，待实测）

- `hyprial` 组织网关 provider 从环境变量读 `HYPRIAL_API_KEY`（`model_provider.py:63-68`、`:497`）。
  ops skill 说它“走 `HYPRIAL_API_KEY` 的 secret grant”（`src/hyprial/skills/hyprial-ops/SKILL.md:516`）。
  但 `HYPRIAL_` 是保留前缀，grant 会拒绝这个名字（`secrets.py:55-56`），而 `src/` 里也没有别的地方写入它。
  **疑似断链，UNKNOWN**，实现前要先实测。本方案把它和 Magpie 一样归为 `router` 连接（§3.1），但普通
  R1 grant 无法投影这个保留名。在有一条“已授权连接 → `HYPRIAL_API_KEY`”的类型化投影之前（调用方不能自填
  保留变量名），`hyprial` 网关来源不能使用 `owned-local` 策略（§4.2 条件 2）。
- 交互启动遇到带 route 的 user-provider 条目时直接拒绝（`provider_credential.py:189`
  `route-unsupported`），所以后台和交互两条路径的可用凭据范围不同。#3134 §2.3 首批也拒绝新连接走 TUI，
  本方案沿用。

## 2. 与模型连接方案（#3134/#3141，含 Magpie 取舍）的对齐

### 2.1 Open PR 盘点（2026-11-24 读取）

| PR | 源分支 | HEAD | 作者 | 范围 | 与本题的关系 |
|---|---|---|---|---|---|
| #3134 | `[redacted]/docs/model-connection-plan-20261124`（fork） | `d617e660626b3bbac707bc3b593cc49fe27ab6ca` | [redacted] | 12 个文档文件：设计正文、GLOSSARY 1 行、`docs/notes/model-connection-plan-20261124/`（证据 + review-v1..v7） | **主对齐对象**：§4 provider 公开面逐项处置、§4.2 唯一存储、§5.1 `model_bindings`、§6 Magpie 取舍、§7 六片施工 |
| #3141 | `intg/model-connections-contract` | `2bac48b84173b84afd0a9f22c38c7ea5c9150d0c` | [redacted] | 40 文件：`model_records/connections.py`(+116)、`runtime/secrets.py`(+78)、`request_catalog/setup/*`、`model_probe/*`、`error/codes.py`、`kernel/impl/ipc/cancellation.py`、`request_http/*`、openapi/生成物/mock、测试 | 第一片：v2 连接记录 + 精确探针。**R1 的存储形态由它决定** |
| #3143 | `feat/dr-p01-provider-examples` | `47f5ffb81f78090e056cece7f7e55489e3b3c20a` | [redacted] | 10 文件：给 7 个 owner-key 方法补契约示例（`daemon/impl/provider_keys/examples/__init__.py` 新增，`request_catalog/registry.py`、openapi/生成物） | R2 方法保持原样（keep），不受影响 |
| #3120 | `wip/services-grant-20261123` | `c334cf7f4c427cf82ee3c0cf7f02359f69523428` | agent-emergent-org-structure-refactor | 19 文件：`service.grant` 把秘密密封给远端 daemon（`service_connect/grants.py` 等） | 远程 Magpie 授权的未来通道；目前未实现远端投递（#3134 §6.1），不作为依赖 |
| #3138 | `docs/cell-daemon-request-review-20261124` | `bd06688aa58f6b60346128d9a8308f2af5a20a84` | — | 2 个文档文件 | daemon-request 边界评审；#3134 §7.1 已记录分工 |

搜索方式：`tea api … pulls?state=open`，再按 magpie/provider/login/auth/credential 筛选。
只有 #3134/#3141 实质讨论了 Magpie。目前**没有**任何 PR 实现 Magpie 的集成代码。

### 2.2 #3134/#3141 已定的约束（本方案照此执行）

- 存储：扩展现有 R1，`$schema=hyprial-model-secret/v2`；连接有稳定的 `connectionRef` 和
  `configRevision`；同一凭据槽内换 Key 只改授权修订，不改 `configRevision`（#3134 §4.2、§5.3）。
- 绑定：放在本机 desired-state SQLite 的新表 `model_bindings`（schema 4），以 `(agentId, memberId)` 为键；
  启动只读这张表，不读 Cell 里的秘密（#3134 §5.1）。
- 新流程不消费 `agent.provider.default`，也不做隐式 seed（#3134 §2.2、§4.1）。
- 授权依旧由 `agent_secret_grants`（R1）和 owner-key grant（R2）承担，连接记录不会自动授予（#3134 §4.2）。
- 失败即拒绝：#3141 登记了 `MODEL_AUTH_UNAVAILABLE`，但目前只在探针方法上声明 phase `authorize`、
  reason `missing|unreadable`（#3141 `contract/daemon-request/credential-probe.md`、`request_catalog/setup`）。
  更宽的 `missing/revoked/ambiguous/grant-changed` 只是 #3134 §8.1 错误表中的**计划**，要由解析器所在片（PR4）
  连同 HTTP 状态映射、OpenAPI、生成物与 mock 一起声明。否则 registry 会把不符合声明的错误数据改报为
  `DAEMON_ERROR`（`request_catalog/registry.py:294-316`）。
- 撤销不等于实时吊销：已注入进程的 Key 不会自己消失，需要显式停止进程（#3134 §5.3）。

### 2.3 职责边界：Magpie / 模型连接 / 本方案

| 能力 | 归属 | 说明 |
|---|---|---|
| 多账号池、分组/auto 路由、限流后切换账号、跨 Key fallback | **Magpie 内部** | Hyprial 不复制这些逻辑。在 Hyprial 里，一个 Magpie 网关只是一个 router 连接（地址 + 协议 + 网关 Key）；Agent 绑定这个连接，并手填网关接受的完整模型 ID |
| 网关 Key 发放（远程 Magpie 给每个客户端发 Key） | **Magpie** + 未来 #3120 通道 | 网关 Key 在 Hyprial 里存为 R1 连接的 Key，不分发厂商原始 Key（#3134 §6.1） |
| 打包/托管 Magpie 进程 | **不做**（另案） | #3134 §6 表格第三行 |
| 连接记录、协议预设、手填模型、探针、binding 表、configure/status/cancel、Pi 模板 | **模型连接线**（#3134 PR1–6） | 本方案不改这些，只作为消费方 |
| 不需要 `--actor` 的可共享登录、按策略在创建/配置时派生 binding、来源索引与级联登出、只读来源清单、旧默认机制的退场 | **本方案**（§4、§6） | 都建立在上一行的公共面之上 |
| 厂商直连 Key（anthropic/deepseek/openai/google/kimi 等） | R1 v2 连接，`scope=vendor` | 与 router 连接同一个存储、同一个 resolver；只是预设不同 |
| Harness 原生 OAuth（claude/codex/pi/agy） | R3，`scope=harness-native` | 绑定里记录唯一的 `nativeAuthRef`（#3134 §5.1）；本方案补充“可共享/仅单 Agent”属性（§4.5） |
| 运维侧的 `magpie claude run` 等 harness 路由（hc 工具链） | 仓库外运维工具 | 不进入 daemon 的 Provider Registry，也不由 daemon 管理 |

由此只剩**一条解析链**：binding → 连接或原生 Auth → 授权检查 → 投影。厂商 Key、Magpie/hyprial
网关 Key、原生 OAuth 是这条链上不同的 `scope`，不会出现第二套 login 命令或第二个凭据库。

### 2.4 文件级碰撞矩阵

本 PR 只新增 `docs/design/daemon-provider-login-autogrant-2026-11-24.md`，与下列 PR 均无同名文件，碰撞为 0。
后续切片（§6）预计会碰到的文件如下（“写”=两边都改，需要排序）：

| 文件 | #3141 | #3134 后续片 | #3143 | #3120 | 本方案切片 | 处理 |
|---|---|---|---|---|---|---|
| `identity/impl/agents/runtime/secrets.py` | 写 | PR1 | — | — | A2、A4 读 | 只消费公共面 |
| `identity/impl/agents/runtime/model_records/*`（v2 封闭 schema） | 写 | PR1 | — | — | 只读，**不追加字段** | 策略不进秘密记录（§4.2） |
| `daemon/impl/request_catalog/setup/specs.py`（`agent.secret.provider-write` 等） | 写 | — | — | — | A1 只调用，不改 spec | 无冲突 |
| `daemon/impl/application/actors/agents_admin/provider_credential.py` | — | — | — | — | A2、A4 写 | 无冲突 |
| `daemon/impl/request_catalog/agent/provider.py` | — | PR4 可能触及 | — | — | **不改**（A1 不动 `agent.provider.login`） | 无冲突 |
| `daemon/impl/request_catalog/agent/provider_auth/` | — | — | — | — | A2、A3、A4 新增同目录文件 | 无冲突 |
| desired-state `model_bindings` 表/序列化 | — | PR3 写 | — | — | A3 增加 `origin` 等字段与 `source_grant_policies` 表、A4 的移除操作表 | **并入 PR3 的 schema 4**，否则会再升一次 schema；见 D2 |
| HTTP 状态契约、`shell/impl/request_http/*` 错误映射、`MODEL_AUTH_UNAVAILABLE` 的 reason 枚举 | 写 | PR4 扩 reason | — | — | A3/A4 只消费 | reason 枚举与 HTTP 映射由 PR4 一次声明；本方案若需 `removed` 等新值，须并入该声明（§5.3） |
| `agents_admin/destroys.py`、`agent_start/`、`composition/cells/reconcile.py` | — | PR4 写 | — | — | A3 读 | 只消费 PR4 的 resolver |
| `shell/impl/cli/commands/agent/provider/commands.py` | — | PR4（skill/CLI） | — | — | A1、A5 写 | A5 与 PR4 合并或排在其后 |
| `contract/daemon-request/openapi.json`、`gui/src/client/generated/*`、`gui/src/dev/contract-mock/contract.generated.json` | 写 | 每片 | 写 | 写 | 每片 | 后合入方在合并 dev 后从源声明重新生成（#3134 §7.1），不手选旧快照 |
| `src/hyprial/error/codes.py` | 写 | — | — | — | 只复用已有错误码 | 本方案**不新增错误码**（§5.3） |
| `GLOSSARY.md` | — | #3134 写 | — | — | A1 写 | 本 PR 不碰；术语随 A1 进表 |

### 2.5 推荐的合入/依赖顺序

1. #3134（设计）与 #3141（PR1）先合入；#3143、#3120 独立，与本线无序依赖。
2. **A1**（setup-token/Key 登录不再需要 `--actor`，仅 CLI）、**A2**（只读来源清单）：在 PR1 之后即可并行。
3. #3134 PR2（Pi 模板）、**PR3（binding 表）**：A3 的 `origin` 字段、`source_grant_policies` 表和 A4 的移除操作表
   要在 PR3 的 schema 4 里一起落地（D2）。
4. #3134 PR4（configure/start/Cell）之后合入 **A3**（创建/配置时的策略派生）和 **A4**（级联登出）。
5. **A5**（CLI 门面与 skill）与 PR4 的 CLI/skill 改动合并为一次 `Skill-Impact: updated`。
6. GUI 跟随 #3134 PR5/PR6，本方案不单独开 GUI 片（GUI 只消费 A2 清单和 PR6 的 status/cancel）。

## 3. 概念模型与存储

### 3.1 Provider Registry（只读视图）与 scope

一个“凭据来源”（credential source）是以下三种之一。**来源本身不授予任何 Agent。**

| scope | 物理位置 | 例子 | 是否可共享 |
|---|---|---|---|
| `vendor` | R1 v2 连接 | DeepSeek/Kimi/OpenAI/Anthropic/Google Key | 是：每次启动在内存注入，不落盘到 Agent |
| `router` | R1 v2 连接，预设类别为 router | 外部 Magpie 网关 Key（经 #3134 PR2 的 Pi 受控 loader 注册投影）；`hyprial` 组织网关 | 是；状态里标注“网关内部路由未核验”（#3134 §6）。`hyprial` 网关在类型化投影落地前不可用于策略（§1.3） |
| `harness-native` | R3（Agent home）；Claude setup-token 改存 R1 条目 `claude-setup-token`（§4.1） | Claude `.credentials.json` / setup-token、Codex `auth.json`、Pi `auth.json`+`models-store.json`、agy `antigravity-oauth-token` | 可刷新文件只写入一个 Agent，新流程不复制（唯一性的边界见 §4.5）；setup-token 可共享 |

`sourceRef` 只是展示用的投影键，不是新 ID：v2 连接用 `connectionRef`（#3141），setup-token 等 R1
旧条目用 `entry:<entryId>`，原生 Auth 用 `nativeAuthRef`，即 `(agentId, harness)`（#3134 §5.1）。Registry
本身不保存状态，每次读取都从 R1/R3 和策略表现算，因此不会和底层存储不同步。**R2 暂不纳入**：
owner-key 以 `keyId` 标识，目前没有 `connectionRef` 投影、策略字段或修订映射。等门打开、并定义好
`keyId` → `sourceRef`、provider/model 元数据和撤销/轮换修订的映射之后再接入（D1）。

### 3.2 存储与平台约束

- 没有新目录。新增的持久数据都是非秘密的，都在 desired-state SQLite：`model_bindings` 中的
  `origin`/`policySourceRef`/`policyRevision`/`sourceState` 字段（§4.3、§4.5）、`source_grant_policies` 表（§4.2）
  和 provider 移除操作表（§4.5），都随 PR3 的 schema 4 一起落地。
- 所有写入沿用现有原语：R1 用 `SecretResolver.write_user_provider` → `model_records.write`（`secrets.py:340-357`，
  `ProcessFileLock` + `write_private_file_atomic`，`model_records/__init__.py:101-121`）；R3 用 `native_credential.py`
  的成组 `.incoming/.previous` + `os.replace`。符合 W1E-1/W1E-2：只做 home 根目录的私有检查，
  不逐文件 `chmod`，不用 `os.symlink`，不用 `dir_fd`。不引入 W001–W021 列出的 POSIX 专有调用；
  需要目录链接的地方只走 `manysys.link_directory`（已由 W1E-1 落地）。
- 投影规则不变：Key、网关 Key、setup-token 只经 `compose_worker_child_launch` 放进子进程的 `env`，
  不写文件；OAuth 文件沿用各 harness 的“原子拷贝为普通文件”投影（agy `native_env.py:52`、
  claude `runtime.py` 头部 5A 方案）。

## 4. 授权与解析

### 4.1 不需要 `--actor` 的登录（A1）

- CLI `hyprial agent provider login claude --setup-token`（不带 `--actor`）在本机读入 token，调用**现有的**
  `agent.secret.provider-write`，写入 `entryId=claude-setup-token`、`fieldName=CLAUDE_CODE_OAUTH_TOKEN`（这个名字
  可以授权，`secrets.py:59-70`）。这个方法的 audience 只有 `local-operator`（`request_catalog/setup/specs.py:35-49`），
  所以 worker 或 bound-agent 无法借它写入 daemon 级来源，不需要再加条件授权分支。
  setup-token 不再写进某个 OWNER Agent 的 SQLite，也就不再需要专门养一个 Agent 来存凭据。
- `agent.provider.login` 的参数、结果 schema（`actor` 必填，`request_catalog/agent/provider.py:195-205`）、audience
  和 handler **全部不变**；带 `actor` 的旧调用保持字节级不变（#3134 §4.1 keep）。可刷新的原生文件继续要求 `actor`（D4）。
- 厂商 Key 和网关 Key 的登录就是 R1 连接保存（GUI 用 `agent.secret.provider-configure`，CLI 用 A5 的门面），
  本来就不需要 actor。

### 4.2 `autoGrantPolicy`（A3）

策略挂在**来源**上，不做全局开关。按 Allen 的“运行期数据优于硬编码”原则，它是运行期数据，但**不写进秘密记录**：
#3141 的 `configuration()` 拒绝 `version/endpoint/protocol/profile/preset` 以外的键，`ModelConnection.decode()` 要求
恰好七个键；setup-token 条目也不是 v2 连接。所以策略放在 desired-state SQLite 的非秘密表里：

```text
source_grant_policies(sourceRef PK, grantPolicy, policyRevision)   -- 随 PR3 schema 4 落地（D2）
```

写入用新方法 `agent.provider.source-policy`（§5.1，带 `policyRevision` CAS）。策略变更**不改**连接的
`configRevision`，因为它不改变连接本身。来源删除时，同一移除操作会删除对应的策略行（§4.5）。
原生 Auth 不支持策略。

| 取值 | 含义 |
|---|---|
| `explicit`（默认） | 只有显式选择这个来源的 Agent 才会使用它 |
| `owned-local` | 新建或编辑的 Agent 如果没有显式选择认证来源，并且同时满足下列条件，daemon 会把这个来源写成它的 binding |

`owned-local` 的条件全部来自现有判定，不新增谓词：

1. Agent 的 owner 等于 daemon 的 owner，而且是本机 Agent（`provider_keys/context.py` 的 `is_local`）；
   `hosted_by is None`；不是 user-agent（`_refuse_user_agent_credential`）。
2. 来源的 scope 和协议满足 Agent 的 `(harness, mode)` 需求（PR2/PR4 的组合准入），并且这个来源已经有
   实现并验收过的投影。不支持的组合、或尚无投影的来源（例如需要 `HYPRIAL_API_KEY` 的 `hyprial` 网关），
   直接不派生，不会退而求其次。
3. 同一个 harness 下只有**唯一一个**符合条件的 `owned-local` 来源。多于一个就不派生，启动时报
   `MODEL_AUTH_UNAVAILABLE/ambiguous`，提示用户选择（#3134 §2.2“不能猜测”）。
4. 这次请求没有带 `autoGrant:false`（CLI 用 `--no-auto-grant`）；Agent 的 binding 也不是显式的 `authKind=none`
   （显式拒绝，最高优先级）。
5. PAC 和生命周期创建同样适用（与现有 R4 默认的覆盖范围一致，`commands.py:269-271`）。受限 worker
   通过显式的 `authKind=none` 或 Cell 的 deny 排除，不另设一份白名单（D3）。

**不会回溯旧 Agent。** 把一个来源改为 `owned-local`，只影响之后的创建/编辑。已有 Agent 需要显式批量操作
（A5 的 `--apply-to-unbound`），对每个 Agent 都走一次 `agent.model.configure`（有 CAS、操作回执），
避免“悄悄改所有 Agent”（#3134 §5.3）。

### 4.3 派生 binding 与可审计记录

派生**发生在配置时**，结果写成一条普通 binding，外加三个非秘密字段：

```text
model_bindings 行（#3134 PR3）+ origin ∈ {explicit, policy}
                              + policySourceRef   （派生时选中的来源）
                              + policyRevision    （派生时 source_grant_policies 的修订）
                              + sourceState ∈ {present, removed}（移除墓碑，§4.5）
```

授权仍然只写现有权威：R1 来源经 `agent.secret.grant`（`source=user-provider`，grantId 确定为
`src-<sourceRef 摘要>`，`route_required` 照旧）。R2 在纳入 Registry 之前不参与策略派生（§3.1）。
`model_bindings` 只记录“为什么有这条授权”，不复制授权（#3134 §5.1）。
审计复用 grant 与 configure 操作回执，不新增审计流。

### 4.4 启动时的确定性解析

任务单里的 `resolve_launch_credentials` 就是 #3134 PR4 的统一 resolver（由 `AppLocalMemberFacts` 消费）。
本方案**不**另写一个函数，只规定它的优先级和投影：

1. **显式 binding**（包括 `authKind=none` 拒绝）：按 binding 解析。`sourceState=removed` 报 `revoked`，
   grant 行不存在报 `missing`（现有 resolver 把缺失的 grant 报为 `grant-missing`，`secrets.py:272-276`），
   **不会改用其他来源**。这些 reason 都要由 PR4 先登记（§2.2、§5.3）。
2. **策略派生的 binding**：与第 1 档走同一条路径，只是 `origin=policy`。启动时**不重新评估策略**；
   来源被移除时同样报 `revoked`。
3. **没有 binding**：
   - 旧 Agent（无 binding、无新会话标记）：保持旧路径，行为不变，包括 R4 和手工 grant（#3134 §2.3 legacy 口径）；
   - 新流程的 Agent：报 `MODEL_AUTH_UNAVAILABLE/missing`，`data.next` 只给一条命令，例如
     `hyprial agent provider use <agent> --source <ref>`（A5）。

投影：环境类凭据（Key/setup-token/网关 Key）经 `compose_worker_child_launch` 在内存注入；文件类凭据沿用
§3.2 的 harness 投影。来源被删除时，agy 的 `sync_agy_oauth_token` 已经会删掉 `session_root` 里的副本
（`native_env.py:62-65`）。

### 4.5 登出、轮换与级联；OAuth 多副本刷新冲突

- **登出可共享来源**（A4，拟议方法对 `agent.secret.provider-remove.preview` / `agent.secret.provider-remove`，
  见 §5.1）。现状是：撤销 grant 会直接删行、不留墓碑（`registry/_records.py:776-783`）；R1 文件写入只有逐文件锁
  （`model_records/__init__.py:101-121`）；grant、binding、条目三者之间没有跨权威的 fence。所以“一个 fence 完成”
  在现有原语上做不到。本方案改用一条**持久移除操作**：
  1. preview：读取 `model_bindings` 中 `policySourceRef` 或 `connectionRef` 等于该来源的行，以及
     `agent_secret_grants` 中指向这个条目的行，记录来源当前 revision 与 grant 代次，返回 `operationId`
     和受影响的 Agent 清单（`stopAffected` 在 preview 参数中给出，execute 只收 `operationId`）；
  2. execute 先在 desired-state 写入操作行（`pending`，含 revision/代次 CAS），并在同一事务中把受影响 binding 的
     `sourceState` 标为 `removed`、删除策略行。从这一刻起，这个来源不能再被 configure 或派生；
  3. 依次撤销 grant（现有 revoke 路径）→ 删除 R1 条目 → 按需执行 stop → 操作行标为 `done`。每一步都可以重复执行；
  4. 崩溃或重放时，按 `operationId` 从操作行继续未完成的步骤。preview 之后来源 revision 或 grant 代次变了，
     就以 `MODEL_CONFIGURATION_CONFLICT/revision-mismatch` 拒绝。
  binding 保留，并带有 `removed` 墓碑，所以下次启动报 `revoked` 并提示重新选择；这个状态与“grant 被单独撤销”
  的 `missing` 可以区分。R2 来源不纳入本流程，仍走 `user.provider.logout`（§3.1）。
- **运行中的 worker**：默认不停止，只在状态里标为 degraded（Key 已注入，#3134 §5.3）；加 `--stop-affected`
  才走现有的 stop 流程（D6）。
- **轮换**：同一槽内换 Key 只改授权修订，binding 和 `configRevision` 不变，下次注入时生效（#3134 §5.3），不需要级联。
- **可刷新 OAuth**：Claude/Codex/Pi 在运行中会刷新 token 并写回 `native_root`；如果复制给 N 个 Agent，
  refresh token 轮换会让其余副本失效（CLI 帮助已有提示，`commands.py:282-285`）。所以：
  - 原生文件来源只写入一个 Agent，`owned-local` 不能用于原生文件来源。注意：`nativeAuthRef=(agentId, harness)`
    是按 Agent 定义的，它**无法识别**两个 Agent 拿到了同一个 refresh token。现有安装器会把上传的字节拷进每个目标
    Agent，并按目标记录来源（`native_credential.py:280-344`）。所以本方案只承诺**新流程不新增复制路径**（策略不派生
    原生来源；R4 的拷贝路径按 D5 退场），并不承诺唯一性。用户对多个 Agent 重复执行 `login --actor` 仍然可行，
    此时沿用现有 CLI 警告（`commands.py:276-287`）。真正强制唯一需要受保护的摘要登记表或 auth proxy（D4 选项 b）；
  - 想“登录一次、多个 Agent 共用”，只有三条路：Claude setup-token（不刷新）、厂商 Key，或在网关集中刷新
    （外部 Magpie / `hyprial` 网关），由网关作为唯一刷新点；
  - 后续演进：本机 auth proxy（daemon 持有唯一刷新权，worker 只拿短期访问令牌）。本批**不做**，单列提案；
  - R4 现有的“新建 Agent 时拷贝原生 Claude 文件”（`defaults.py:156-165`）正好会产生多副本冲突。
    A3 落地后，新流程不再走它；旧路径按 D5 退场。

### 4.6 与 Cell 模型的关系

Cell 成员文档携带非秘密的 `kind`/`model`/`adapter` 数据（`cells/structure/parse.py:112-113`），以及可选的
`allow`/`deny` 策略（`parse.py:145-161`、`:283-296`）；`modelVendor` 来自本机 facts（`cells/reconcile/effects.py`）。
本方案**不往 Cell 写入任何凭据值或私有来源引用**（#3134 §5），只用 Cell 已有的 `deny` 排除受限 worker（§4.2 条件 5）。
Agent 跨机落地（teleport）后，目标机按本机来源和策略重新派生 binding；派生不出来（来源不唯一或不存在）
就报 `auth/binding-required`，不回落到机器默认（#3134 §5.1 末段）。按 2026-11-21 的裁定，迁移只发生在同一
用户的多台主机之间，所以 owner 判定不变。

## 5. 契约草案（全部拟议）

### 5.1 Daemon RPC

| 方法 | 新增/修改 | audience | HTTP / IdentityBinding | 幂等 | 说明 |
|---|---|---|---|---|---|
| `agent.provider.login` | **不改** | 不变：`local-operator`、`bound-agent` | 不变 | 不变：`never` | 仍然必须带 `actor`；daemon 级 setup-token 改走下一行 |
| `agent.secret.provider-write` | **不改**，复用 | 不变：只有 `local-operator` | 不变 | 不变：`never` | A1 的 CLI 用它写入 `claude-setup-token`（§4.1） |
| `agent.provider.sources` | 新增，只读 | `local-operator` | 暴露 HTTP，`IdentityBinding("none")` | `retry="safe"`，`IdempotencySpec(mode="safe")` | 汇总 R1/R3 与策略表：`sourceRef`、`scope`、`harness`/协议、`grantPolicy`、`shareable`、引用这个来源的 binding 数、状态；**不返回值、路径或摘要**。放在 `request_catalog/agent/provider_auth/` 目录下 |
| `agent.provider.source-policy` | 新增，可变更 | `local-operator` | 暴露 HTTP，`IdentityBinding("none")` | `retry="never"`，用 `expectedRevision` 做 CAS | 设置 `source_grant_policies` 中一个来源的 `grantPolicy`；不碰秘密记录 |
| `agent.secret.provider-remove.preview` | 新增 | `local-operator` | 暴露 HTTP；`confirmation="preview"`，`PreviewSpec(ttl_seconds=300)` | `retry="safe"` | 参数 `sourceRef`、`stopAffected`；结果必带 `operationId`、`expiresAt`、`summary`、受影响的 Agent（仿照 `agent.destroy.preview`） |
| `agent.secret.provider-remove` | 新增，可变更 | `local-operator` | 暴露 HTTP；`confirmation="execute"`，`preview_of` 指向上一行 | `retry="never"`（与 `agent.destroy` 和 adapter removal 相同） | params **只有** `operationId`，`additionalProperties:false`（`method_spec.py:495-539`）；重放和崩溃后的继续执行由 §4.5 的持久操作行负责 |
| `agent.create`、`agent.model.configure` | 修改：增加可选参数 `autoGrant: bool`（默认 true） | 由 PR4 定 | 由 PR4 定 | 由 PR4 定 | 实施时并入 PR4，不另开方法 |

R2 的 7 个方法与 `agent.provider.logout/auth.list` 保持原样（#3134 §4.1 keep）。如果某个新方法以后要改成
`idempotency-key` 重试，必须同时给出 `IdempotencySpec.key_field`（`method_spec.py:703-716`）。

### 5.2 CLI（不新增顶层命令族）

```text
hyprial agent provider login claude --setup-token [--policy explicit|owned-local]   # 不需要 --actor；= provider-write + source-policy
hyprial agent provider login <harness> --actor A [--force]                           # 原样：原生文件只写入该 Agent
hyprial agent provider sources [--json]                                              # A2 只读清单
hyprial agent provider use <agent> --source <ref> | --none                           # 显式 binding（PR4 configure 的门面）
hyprial agent provider policy --source <ref> explicit|owned-local                   # source-policy 门面
hyprial agent provider logout --source <ref> [--stop-affected]                       # A4 级联登出（preview → 确认 → execute）
hyprial agent provider apply --source <ref> --to-unbound [--dry-run]                 # 显式回溯
hyprial agent create ... [--no-auto-grant]
```

`hyprial user provider …`（R2）在 owner-key 门打开前保持原样。门打开、并定义好 `keyId` 的投影映射后，
再由 A2 的清单把 R2 作为另一种 `vendor` 来源展示，不另起登录命令（§3.1、D1）。

退场周期：`agent provider default` 与 `login --default` 在 A3 合入的版本里先输出弃用提示，下一个版本移除。
`agent_secret_defaults` 与 `provider-default.json` 的旧数据不自动迁移，迁移说明写进 CHANGELOG 链接的指南
（按照“无迁移代码”的裁定）。

### 5.3 错误

- 复用 `MODEL_AUTH_UNAVAILABLE`（phase `authorize`）。本方案需要的 reason 是 `missing`、`revoked`（来源已移除，
  靠 binding 墓碑区分）、`ambiguous`（多个 `owned-local` 来源）。它们**都还没有登记**：#3141 只为探针声明了
  `missing|unreadable`。完整的枚举要在 PR4 里一次声明，同时更新共享错误声明、HTTP 状态映射、OpenAPI、生成物与 mock，
  以及全部 resolver 测试。本方案不另加 `source-removed`，`revoked` 加上墓碑已经能区分。
- 移除冲突复用 `MODEL_CONFIGURATION_CONFLICT/revision-mismatch`（#3141 已登记）。其余拒绝复用 `INVALID_ARGUMENT`、
  `CALLER_NOT_AUTHORIZED`、`AGENT_SECRET_RESOLUTION_FAILED`，**不新增错误码**。

## 6. TDD 施工切片

每片都先写会失败的测试再实现；`src/hyprial/` 有改动时跑焦点测试、全量 `uv run --with libcst==1.9.0 pytest tests/`
和 `uv run ruff check .`，并跑结构、模块边界、Windows 三个门。下表的行数和文件数取自 `b36e97da4b`。

| 片 | 前置 | 目标文件（当前行数；目录直接文件数/7） | 先失败的测试 | 必跑门 |
|---|---|---|---|---|
| A1 不需要 actor 的 setup-token | #3141 | 只改 CLI：`shell/…/agent/provider/commands.py`（555；目录 4/7）调用现有 `agent.secret.provider-write`；GLOSSARY。daemon 契约与生成物**不变** | CLI 测试：不带 `--actor` 的 `--setup-token` 发出 `provider-write(entryId=claude-setup-token, fieldName=CLAUDE_CODE_OAUTH_TOKEN)`；`agent.provider.login` 的契约快照不变；worker 来源调用 `provider-write` 被现有 audience 拒绝（回归） | structural、module_boundaries、windows_portability、`check_daemon_contract_diff.py`（应无差异） |
| A2 来源清单 | #3141 | 在 `request_catalog/agent/provider_auth/`（1/7）新增 `sources.py`；读取逻辑放进新子包 `agents_admin/sources/`（agents_admin 已满 7/7，只能建子包） | R1/R3 各自的 present/absent/unavailable 三态；R2 不出现；输出中没有值、路径、摘要；worker 调用被拒 | 同上 + `check_request_contracts.py` + `native_origins_*.py` 登记新模块（`tests/integration/native_harnesses/resident_metadata/`） |
| A3 策略派生 | #3134 PR3+PR4 | PR3 的 schema 4（`model_bindings` 的新字段 + `source_grant_policies`）；`agent.provider.source-policy`（`provider_auth/`）；PR4 的 configure/create 协调器。**不改** `model_records/connections.py` | 唯一来源→派生；两个来源→ambiguous 不派生；`--no-auto-grant`/`authKind=none`；hosted、非本机、user-agent、没有投影的来源都不派生；只影响之后的创建；启动时不评估策略；策略 CAS 冲突 | 同上 + `tests/tooling/architecture/delivery/test_cell_entry_gate.py`（不往 Cell 写凭据） |
| A4 级联登出 | A3、PR4 的 reason 枚举 | 持久移除操作表（schema 4）；`provider_auth/` 下的 preview/execute 方法对；identity grant revoke 公共面 | 在每个步骤边界注入崩溃后用同一 `operationId` 都能续完；preview 之后来源 revision 变化→conflict；binding 带墓碑、下次启动报 `revoked`，单独撤销 grant 报 `missing`；`stopAffected` 走现有 stop；移除进行中的 configure 被拒 | 同上 |
| A5 CLI 门面与 skill | A1–A4、PR4 | `shell/…/agent/provider/`（4/7，可新增文件）、`hyprial-ops` skill、`docs/guides/` | CLI 输出门（`tests/shell/cli/output/test_output_discipline.py`）；`--json` 恰好输出一个对象；弃用提示走 stderr | 同上；PR 正文写 `Skill-Impact: updated` |

文件数预算：每片 < 50（含生成物）；A1 预计不超过 5 个文件，A2 预计 15–25 个文件。

## 7. 待 Allen 拍板

| # | 决策 | 选项 | 推荐 | 理由 |
|---|---|---|---|---|
| D1 | 是否新建物理凭据库 | a. 不新建，Registry 只是视图，R1 是共享秘密的唯一位置；R2 在定义好 `keyId` 投影后再作为另一种 `vendor` 接入；b. 新建 `<HOME>/secrets/providers/` 之外的目录 | **a** | #3134 §4.2 已定“不建第二份 vault”；任务单说的路径本来就是 R1 |
| D2 | 非秘密新数据（binding 的 `origin`/`policySourceRef`/`policyRevision`/`sourceState`、`source_grant_policies`、移除操作表）何时落地 | a. 并入 #3134 PR3 的 schema 4；b. 之后单独升 schema 5 | **a** | schema 4 落地后不可无损降级（#3134 §5.1），再升一次代价翻倍；需要与 [redacted] 线协调 |
| D3 | 默认策略 | a. 来源默认 `explicit`，用户逐个来源开启 `owned-local`；b. 全局默认 `all_owned_agents` | **a** | 升级后不会出现隐式授权；与 #3134“不隐式 seed”一致；单机用户在登录时加 `--policy owned-local` 即可得到同样体验 |
| D4 | 可刷新 OAuth 的共享 | a. 新流程不复制、不共享，共享只靠 setup-token/Key/网关；仍允许手工 `login --actor` 并保留现有警告；b. 受保护的摘要登记表强制唯一，或 daemon 级来源 + 租约/auth proxy；c. 允许多副本 | **a** | b 需要登记表或租约、回写和崩溃恢复，相当于一个新机制；c 会使 refresh token 互相失效。a 不保证唯一（§4.5），演进方向是网关或本机 auth proxy |
| D5 | 旧默认机制的退场 | a. A3 合入的版本先提示弃用，下一版本删除 `agent provider default`、`--default`、`agent_secret_defaults`、`provider-default.json`；b. 长期保留兼容 | **a** | 按“不写迁移代码”的裁定；#3134 已把它限定为 legacy 专用 |
| D6 | 登出级联时如何处理运行中的 worker | a. 默认标记 degraded 并列出，加 `--stop-affected` 才停止；b. 默认自动停止 | **a** | 与 #3134 §5.3“不当作实时吊销、终止要显式”一致；避免一次登出打断全部 Agent |

## 8. 未验证（UNKNOWN / BLOCKED）

- `HYPRIAL_API_KEY` 的写入方（§1.3）：UNKNOWN，A1 开工前用隔离 home 实测。
- `antigravity-oauth-token` 是否会自动刷新：UNKNOWN。在证实不刷新之前，按可刷新文件处理（不共享，D4）。
- Magpie 的真实运行与网关内部路由：UNKNOWN（同 #3134 §8）。
- R2 在生产中可用（签名器、U2 锚点）：BLOCKED，取决于 owner-key 线，与本方案无关。
- `hyprial` 网关的类型化 `HYPRIAL_API_KEY` 投影：尚未设计，属于 router 策略的前置条件（§1.3）。
- 本文档只做了文档检查和结构门，没有改代码，没有运行产品测试。

## 9. 独立审查记录

审查者：codex `gpt-6.2-astra`，只读，基线 `b36e97da4b`，参照 #3134 文档与 #3141 diff。结论为 BLOCKED（2 个 blocker、
7 个 major、4 个 minor）。下表中每一条都已对照源码核实属实，并已在本文修订。

| # | 级别 | 问题 | 处置 |
|---|---|---|---|
| 1 | blocker | #3141 v2 记录是封闭 schema，放不下 `grantPolicy` | 策略改存 desired-state 的 `source_grant_policies`（§4.2） |
| 2 | blocker | #3141 只登记了 `missing\|unreadable` | 改写 §2.2、§5.3：reason 枚举由 PR4 统一声明 |
| 3 | major | 无 actor 的登录违反结果 schema | `agent.provider.login` 不改，改用 `provider-write`（§4.1） |
| 4 | major | 无 actor 的写入没有 worker 授权闸 | `provider-write` 只有 local-operator，天然把 worker 挡在外面（§4.1） |
| 5 | major | 单个方法的 preview→execute 写法不合法 | 拆成 preview/execute 方法对，execute 只收 `operationId`（§5.1） |
| 6 | major | 级联登出没有跨权威 fence，删行后分不清 `revoked` 和 `missing` | 持久移除操作 + binding 墓碑（§4.5） |
| 7 | major | `nativeAuthRef` 无法保证 OAuth 唯一 | 不再声称唯一，只承诺新流程不复制；强制唯一放到 D4 选项 b |
| 8 | major | router 的 `HYPRIAL_API_KEY` 是保留名 | 没有投影的来源不能使用策略（§1.3、§4.2） |
| 9 | major | `--policy` 没有对应的 RPC 或存储 | 新方法 `agent.provider.source-policy` + 策略表 |
| 10 | minor | Cell 成员字段写得不全 | 补上 `allow`/`deny`（§4.6） |
| 11 | minor | R1 写入原语引错 | 改引 `model_records.write`（§3.2） |
| 12 | minor | 碰撞矩阵缺少 setup specs、HTTP 映射 | 已补行（§2.4） |
| 13 | minor | R2 没有 `sourceRef` 映射 | R2 暂不纳入 Registry（§3.1、D1） |
