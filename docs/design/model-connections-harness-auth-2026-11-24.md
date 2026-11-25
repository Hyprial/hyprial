# 模型连接、手填模型 ID 与 Agent 启动统一方案（WIP）

状态：CLI developer 第七轮设计复核已闭合，用户已授权串行实施；PR 1 已合入 dev，PR 2 产品增量已独立评审通过；最新正式 CI 的 GUI 已通过，Python 分片暴露独立 Lark 收尾竞争；已开小修复 #3155，PR 2 等修复合入后验证，执行状态见 §7.2。
设计结论是实现验收焦点，不代替独立实现评审或合并门禁。未交付阶段的字段、方法和错误码仍为拟议契约。
设计固定基线：`8ccaf513ec23de3bc39a53a1cc23cddf9fb34e15`。
PR 目标为移动的 `dev`；第二轮评审时 base 为 `ef7c2e26173d6e2dda59d71b775265af9fb2deb5`，
相关实现目录无差异，不能将固定设计基线写成实时 dev head。
负责人：codex-gui-orgs-review。来源：2026-11-24 用户讨论与
[调查证据](../notes/model-connection-plan-20261124/README.md)。

## 1. 用户已确认的边界

- 所有模型都手填模型 ID。没有模型下拉、预置模型选择、自动发现或模型目录请求。
- 添加 Key 时选择官方通道预设，或填写自定义地址、协议；创建时选 Harness、认证来源、手填模型 ID。
- 启动复用已保存配置，不再次选择服务商。失败关闭：不支持的组合可以失败，原因必须准确。
- 先提交 WIP 文档 PR，明确 Magpie 取舍和 provider 退役边界；方案确定后由 CLI developer 评审。
- 每个 PR 少于 50 个变更文件，包括生成物、测试、文档；不能为缩小计数省略契约或验证。

本文建议首批补齐 Pi **rpc/headless** 的自定义 OpenAI Chat Completions 连接；其他自定义协议以及
Claude/Codex 通用自定义连接只有在对应适配通过验收后开放。保留已有官方 Auth 和指定通道。
用户已授权按此顺序实施，不把尚未验收的组合标成可用。

## 2. 三个页面应该怎么用

### 2.1 添加 Key

| 输入 | 官方通道 | 自定义通道 |
|---|---|---|
| 名称 | 用户填写 | 例如 `ctyun-test` |
| 通道 | 选择官方预设，如 DeepSeek 官方、Kimi Coding | 选择自定义 |
| API 协议、Base URL | 从版本化预设取得并展示 | 用户选择已支持协议、填写地址 |
| API Key | 写入现有私有凭据存储 | 同左 |
| 模型列表 | 没有此字段 | 没有此字段 |

保存 Key 不要求填写模型，不自动访问服务。官方预设只提供连接参数和适配信息，
不提供或推断模型 ID。预设更新不得无提示改变已保存连接的地址或协议。

用户点击测试时，单独手填本次测试模型 ID；测试不保存 Agent 模型，不把一个模型
测试成功推广为其他模型可用。结果区分配置校验、协议请求、Harness 实测；未执行的阶段
显示未验证。普通凭据测试不创建 Agent、不自动重试、不自动扫描 `/models`。

### 2.2 创建或编辑 Agent

用户填写名称、工作目录，选择 Harness，再选择认证来源：

1. API Key：选择已保存连接，例如 `ctyun-test`。地址、协议作为只读摘要，不再选服务商。
2. 原生 Auth：选择可用于该 Harness 的已有登录材料；缺失时走其受支持登录流程。
3. 模型 ID：始终是必填文本框，例如 `hy4-flash`；不显示“服务商默认”选项。

用户手填的 ID 是上游需要的原样字符串：不改大小写，不剥离命名空间，不按名字
推断厂商，不替换为别名。拒绝空值、控制字符和超出契约长度的输入；首尾空白提示
用户修改，不静默规整后请求另一个 ID。名称合法不证明服务端存在该模型。

原生 Auth 仍受 Harness/服务格式约束；Pi 原生 Auth 如含多个登录目标，必须由所选
认证记录明确目标，不能猜测或随意选第一个。无法唯一解析则拒绝并提示选择/配置认证。
显式绑定流程不调用机器默认凭据 seed：包括 CLI、GUI、PAC 和生命周期创建。
`agent.provider.default` 只为 legacy 创建保留；其值改变不能改写已有显式绑定。
旧创建请求没有绑定时仍可创建未配置身份，但不得让新流程借此跳过认证与模型必填校验。

新建与编辑统一要求显式模型。历史未设置模型的 Agent 不在升级时批量改写；其旧行为
明确标记为 legacy，进入新编辑流程后必须补齐，不伪造一个当前默认模型。

### 2.3 启动

展示已保存的 `Pi · ctyun-test · hy4-flash`，直接启动；修改走同一个编辑入口。
GUI 不重新拼出 provider/model 覆盖。CLI 新入口也使用同一配置服务。已有显式绑定时，
旧 start 的 provider/model/effort/tier 参数只允许与保存配置一致；不一致拒绝并提示先编辑，
不把一次启动暗中变成配置修改。legacy Agent 的旧参数行为保留并单独回归。

“配置已保存”“进程已启动”“模型调用成功”分别报告。模型认证可能在首个请求才完成，
不把 ready/running 当作上游鉴权通过。

首批新自定义连接的 TUI/interactive/attach 入口明确拒绝，不因 rpc/tui 共用 builder
就宣称交互模式已验收；原有原生 Auth 交互能力保持原状。
进入显式绑定流程后，恢复会话时连接身份、配置修订或模型任一不符，拒绝续会话，要求用户明确
新建会话或恢复原配置。不得沿用现有 `session.provider.rebound` 的警告后自动重绑行为。
旧会话缺少可核验绑定来源也拒绝接到新连接；legacy 会话不批量迁移。
legacy 兼容口径固定为：本机没有显式绑定且没有新绑定会话标记，继续按原 legacy 配置
恢复，显示“历史配置，模型来源未核验”，不补写认证快照。升级本身不拒绝全部旧会话。
本机绑定、LaunchSpec 引用或会话快照任一显示已进入新流程，就不允许降回 legacy；
新连接接旧 session 必须新建 sessionId。损坏/未知格式不冒充“旧版缺省”。
同一凭据槽的轮换/重新授权不是模型配置变化，但每次恢复仍验证最新授权（见 §5）。

快照使用现有 Pi 私有 receipt 路径
`<sessionRoot>/.hyprial-loader-receipts/<sha256(sessionId)>.json`，增加版本化 `modelBinding`
字段：schemaVersion=1、bindingRef、bindingRevision、authKind、连接或原生 Auth 引用、configRevision、
model、Harness/profile；不含 Key、token 或 grant 秘密。它记录本会话最初选择，
不是另一份当前配置权威；当前选择仍由 §5.1 本机 binding 表决定。
daemon 从可信本机绑定构造启动 payload，loader 按受管 sessionRoot/sessionId 读取该 receipt。
新 session 在首次上游请求之前原子写入快照，失败即停止；恢复 session 在 SDK 选择模型或
任何请求之前比较快照和 payload。receipt 顶层仍为 `schema=hyprial-pi-loader-v1`；
`modelBinding.schemaVersion=1` 是独立的绑定快照版本，两层未知版本均拒绝。
顶层 v1 无 modelBinding（或旧 session 根本无 receipt）仅能进入上面的 legacy 兼容分支；
对显式绑定 session，快照缺失、损坏或任一层版本未知都拒绝，
不能用当前配置回填成“来源已核验”。同一 session 的资源 receipt 刷新须保留不可变快照；
现有启动串行化有两层：`agent_start._ipc_lifecycle_start` 进入
`lifecycle/lifecycle_coordinator.py`，以 `_resources` 的 actor/agent/harness 资源与 `_held`
排队；`harnesses/actor/behaviors/lifecycle.py` 的 `_lifecycle_effect_resources` 阻止在进行中
生命周期操作之外插入 Ensure。`record.starting` 只是状态位，当前可 supersede 并再次
`_begin_start`，不是互斥；direct restore/reconcile 不因上述入口而自动获得 coordinator 保护。
资源 key 是 harness/member，不能防止不同资源误用同一个 sessionRoot/sessionId。

PR 2 新增 **loader 自己持有的会话独占占用**，所有携带新 modelBinding 的受管 Pi
controlled loader 在读写 session/receipt 或初始化 SDK 前执行；位于
`pi_controlled_loader.mjs` 的公共 run 入口，不依赖是否经 coordinator。
legacy 的模型选择、认证与已有会话恢复规则保持原状，不自动写新 modelBinding。
实现评审修订：legacy 也必须在打开 session 和首次写 receipt 的短区间取得同一占用，
写完不带 modelBinding 的 receipt 后立即释放；不承诺 legacy 整个运行期的单 writer。
理由是 legacy 与显式绑定共用 receipt 路径：只有新绑定取占用时，两个首次启动可同时
检查“receipt 不存在”，随后互相覆盖，绕过来源校验。初始化冲突或该短区间崩溃残留
因此也可能拒绝 legacy 启动；这是明确的兼容成本，不冒充完全无行为变化。
正常 legacy 启动不新增绑定快照；已有绑定 receipt 仍禁止降级。占用路径为 canonical sessionRoot 下
`.hyprial-session-reservations/<sha256(sessionId)>`；用本机文件系统排他 `mkdir` 获得，
父目录可幂等创建；每 session 子目录必须非递归 `mkdir`，EEXIST 一律拒绝，不能用
recursive=true 将已存在空目录视作成功。取得后原子写入本次随机 ownerToken/版本，
禁止跟随符号链接。它是保守的持久占用，
不是已有 OS 锁，也没有 TTL、mtime 心跳或“看见 PID 不在就抢占”的逻辑。
同一路径已存在（包括空目录或损坏记录）一律拒绝第二个 loader，返回
MODEL_SESSION_BUSY、phase=admission、reason=session-reserved；适用于新建和恢复，
不写 receipt，不调用模型。
相同请求也不能在 loader 层复用另一个进程的结果；已有 running 资源可由原 actor 返回 ready。

占用覆盖完整 SDK/session 生命周期。在 `runControlledMode` 先取占用、后
`createControlledRuntime`/`openOrCreateSession`，整个分支用共同 try/finally 收口；
不能保留当前 run-rpc 直接 return、只有 run-tui 才 dispose 的结构。新绑定 rpc 的
stdin EOF/受控 SIGTERM 必须进入同一个异步 shutdown：先停止输入与在途请求、完成
runtime.dispose，再校验 ownerToken、释放占用，最后退出。取得后而 runtime 创建前
失败也须由 finally 释放已取得的占用；owner 记录写失败时，只在确认仍是本次 mkdir
创建的目录后清理，不能删除另一持有者的目录。dispose/释放无法确认时保留占用并准确失败。
信号不能直接 process.exit 绕过 finally；SIGKILL/崩溃属于异常残留，不承诺释放。
两分支共用结构并不开放新自定义 TUI；该组合仍在准入时拒绝。
daemon 的真实 stop 须验证上述退出链及有界超时，不能仅用直接调用 finally 的 unit test。
释放后不再读写该 session。daemon 重启不能释放仍运行 child 的占用；child 已死但残留
时明确提示“上次异常退出留下占用，本版本请新建会话”，不提示再停止一个已死进程。
本批不提供离线/在线强制解锁，也不在实现时自行删除残留目录。
不同 sessionId 不争用此目录，旧快照不覆盖。新进程不能仅因为经过了生命周期
协调器就跳过占用。显式绑定流程只能接入新分配的 sessionId 或有 v1 modelBinding 的旧
session；升级前无快照 session 不能续到新连接，即使占用目录缺失也不准入。
PR 2 将新绑定的 sessionRoot 限定为 identity 权威映射的 Pi 会话目录（当前为
`<agentHome>/state/pi`），不是 home 下任意子目录；不接受工作目录、网络挂载或调用者传入的任意 root。路径必须经过 canonical containment
检查；root allowlist 来自 daemon 的可信启动 payload，不能由浏览器声明。
受管 home 自身也可能在共享文件系统上，因此 PR 2 在 kernel 的 manysys/fs 公共面新增
只读 locality 检查，由 loader.py 构造 payload 前调用：根据平台卷/挂载信息识别本机文件
系统，网络/FUSE/未知类型和探测失败均返回 MODEL_COMBINATION_UNSUPPORTED、
phase=admission、reason=session-storage；不能只跑一次 mkdir 竞态就声称排除了共享存储。
PR 2 包含 macOS/Linux/Windows 本机根通过、共享 home/外部 root/符号链接逃逸/未知探测拒绝
与排他创建回归；未完成的平台不得开放该新组合。此方案不新增当前配置权威。

PR 2 必须验证：两请求同启；direct restore/reconcile 与显式 start 竞争；两个不同
resource key 指向同一 canonical root/sessionId（包括路径别名）；daemon 重启而旧 child
仍运行；child 在 mkdir 后/owner 记录后/receipt 前后崩溃；正常退出后重新获取。
明确保证的是单 session writer，**不是只创建一个 OS 进程**；第二个进程可被拉起后在
准入处退出。测试须记录两个 start 的完整终局：成功方有 actor process/identity 归属，
失败方为 MODEL_SESSION_BUSY 或已有 HARNESS_START_SUPERSEDED，所有失败/过期 generation
产生的 child 均停止并回收，不允许一个成功 writer 留在 actor 记录之外。
若双方都失败，必须两个 child 都退出、actor 为失败状态；不能把 writer=1 当成启动成功。
PR 2 负责新绑定 Pi 路径的必要 stale-start 清理；无法证明不留孤儿就不开放，不更改其他
Harness 的 supersede 行为。失败的占用申请不覆盖快照、不访问上游，错误占用不自动回收。
另列必测负例：新 binding payload + 已存在旧 session 无 receipt/无 modelBinding →
MODEL_RESUME_MISMATCH/provenance-missing，且不补写 receipt、不请求上游；
parent 已存在、空 session 占用目录、owner 写入早失败、真实 stdin EOF/SIGTERM、dispose 失败。
本批不改变所有 Harness 的 starting/supersede 语义，不使用 interactive-only 的
`_fence_interactive_session` 冒充后台启动证明。
bindingRevision 留作追踪，续会话比较实际认证目标/连接修订/模型/Harness/profile；
同槽授权轮换不因此变成换模型。显式新会话取得新 sessionId，不覆盖旧 session 的快照。
此落点及读取/拒绝测试属于 PR 2，PR 4 负责将真实绑定接入 payload。
PR 2/3 期间新连接生产准入仍关闭，PR 2 只在隔离测试中注入带明确 fixture 来源的
typed binding payload；不生成假的生产 bindingRef，也不写新 modelBinding 到 legacy
session。PR 4 接入 §5.1 权威后，bindingRef 才能由已提交的本机记录产生；缺失则拒绝。
中间版本的 legacy session 继续采用上述 legacy 分支；会话独占占用本身不算绑定标记。

## 3. 当前缺口与原因

| 当前行为 | 基线实现 | 需要修复的边界 |
|---|---|---|
| 天翼地址显示为 DeepSeek | `shared-credentials-api.ts`、setup/routes 和 ModelServiceRoute 固定 deepseek | 通道、协议、模型品牌解耦 |
| 模型仅默认/deepseek-flash | AgentForm 与 `harnesses/model_routes` 固定选择 | 手填 ID，按协议能力准入 |
| 测试成功但启动拒绝 | model_probe 接受显式地址/模型，启动有另一个白名单 | 共用连接解析，分开报告验证阶段 |
| 启动报密钥解析失败 | route-unsupported 被泛化为凭据错误 | 保留安全的失败原因与阶段 |
| 启动再次选服务商 | create/start 各自构造参数 | 一次配置，多入口一致消费 |

Pi controlled loader 已有 `ModelRuntime.registerProvider`。Hyprial 目前只注入固定的
DeepSeek 模型定义；仅放开路由 if 条件会在后续模型注册/查找继续失败。
模型 ID 手填不消除 SDK 对运行参数的要求：成本结构、上下文限额、输出限额、
reasoning、图片和工具格式需要协议适配层提供明确值。

### 3.1 Pi 通用模板与支持边界

自定义连接不复制 DeepSeek 的价格、百万上下文和 reasoning 参数。拟议模板
`openai-chat-text/v1` 固定以下配置；这是待实现的适配契约，不是已验证的上游能力。

| 注册字段 | 明确取值 |
|---|---|
| 注册 ID | 由稳定连接引用及配置修订派生的内部 ID，与显示名、模型品牌无关 |
| `api` / `baseUrl` | `openai-completions` / 连接中已校验的地址 |
| `models[].id` / `name` | 用户手填 ID 原样，不生成候选列表；name 只作显示 |
| `reasoning` / `input` | `false` / `["text"]`；不发送推理或图片扩展 |
| `thinkingLevelMap` / `thinkingFormat` | 不设置；不借用 DeepSeek 专用格式 |
| `cost` | SDK 要求的 input/output/cacheRead/cacheWrite 全为数值 0；产品账单与统计标记“未知”，不计为真实零费用 |
| `contextWindow` / `maxTokens` | 默认 32768 / 4096；是本地运行限额，非服务能力声明；高级设置可显式覆盖，要求正整数且 output < context |
| `compat` | supportsStore=false、supportsDeveloperRole=false、maxTokensField=`max_tokens`、requiresReasoningContentOnAssistantMessages=false、supportsStrictMode=false |

所有兼容参数显式传入，不能让域名或模型名称触发品牌推断。SDK 仍可能有隐藏的按名称
自动行为，PR 2 须对发出的 body 做断言；无法抑制的组合拒绝开放。限额不适配上游时准确
失败，不自动缩小/改模型；原始回复可能含敏感信息，只输出契约白名单分类。
文本请求与工具调用分开验收；工具采用该协议的 tool-call 格式，不从一次文本成功推断支持。
成本 0 是 SDK 运行占位，须覆盖 token/费用展示，不能流入“真实消费为 0”的统计。

`loader.py` 的 `PI_SUPPORTED_MAX_VERSION="1.3.0"` 是现有版本上限唯一来源；
CI 使用 0.85.1，本机已有 1.2.0 文本探针，不能将其误写为只支持 0.85.1。
PR 2 验证 0.85.1、1.2.0、1.3.0 及高于上限的拒绝；上限升级只改这个常量并补验收，
不新增 GUI/CLI 各自的版本范围。数值小于上限不自动等于新自定义模板已验收。
模型按启动注入，每 agent 的 native root / `models-store.json` 隔离，不写全局 Pi 配置。

### 3.2 测试请求的期限

基线实际是 `model_probe/network.py` 总期限 10s、GUI transport 默认预算 15s。
仅延长一端仍会把合法出网误报为“密钥错误”。新测试契约增加 `timeoutSeconds`：默认 30、
范围 1–60；daemon 总期限包括连接和响应读取，HTTP client 不得提前使用更短的默认值。
GUI 及 request-http carrier 的该方法预算为总期限加 5s；其他方法默认值不改。
旧请求省略字段保持现行 10s，新的连接测试明确发送 30；协议和期限一起进入 schema。
超过期限返回 `outcome=timeout, phase=probe, reason=deadline-exceeded`，显示“未验证”，
不判 Key 无效，不自动重试。取消须停止本次探针；测试期限不成为 Agent turn 超时。

这是拟议改造，不是现有 carrier 能力：目前 request-http 只有全局
`requestTimeoutSeconds=15`（可配 1–120），MethodSpec 没有按方法期限字段。
**PR 1** 在 `src/hyprial/shell/impl/request_http/` 增加仅匹配
`agent.secret.provider-test` 的预算规则，与该方法 params schema、`model_probe/request.py`
的 closed-object 校验同步。schema 校验及可信 principal 准入后，才读取合法
显式存在的 timeoutSeconds 并设置该方法总预算 `D+5`（D 为实际探针期限，最高 60s）；继续从 HTTP
请求起始时刻减扣已用时间。其他方法沿用原全局预算，不接受请求体伪造“通用预算”。
header/body 接收期限不放宽；规则只作用于已完成解析和准入的 dispatch/等待阶段。
GUI 同样按本次 D 显式传外层预算，不改全局配置；基线无 provider-test CLI 调用方，
本批不借此新增 CLI 探针命令，未来若增加须同样显式传预算。
字段缺省时 carrier 不补 30、不启用特例，legacy 保持原 10s 探针/默认 15s 外层预算。
白名单特例的独立上限为 65s，
由 carrier 固定约束，不能由浏览器请求放大；现有全局预算仍应用于其他方法及解析前阶段。

PR 1 的验收落在 `tests/shell/request_http/test_server.py`、`test_config.py`（同目录）
与 `tests/daemon/model_probe/`：默认新请求获 35s、合法 60s 请求获 65s、legacy 缺省仍
10/15s；非白名单方法仍原预算；超范围/布尔值/未知字段在转发前拒绝；已用时间扣减、
取消和预算耗尽均无重复请求。时钟/延迟注入测试无需真的等待 65s。

## 4. provider 是不是整体退役

结论：退役用户流程中的重复选择和固定厂商驱动逻辑，保留底层必要的服务适配与凭据授权。

| 现有机制 | 处置 |
|---|---|
| 添加 Key/创建/启动反复选择 provider | 新流程移除；连接选择唯一决定地址、协议与认证绑定 |
| 自定义连接必须 deepseek、模型必须 deepseek-flash | 对新通用连接退役；旧记录兼容处理，不静默改写 |
| Harness 的原生 provider ID、官方协议/登录适配 | 保留在内部，由已选连接或 Auth 绑定解析 |
| 现有 Secret、grant、撤销/修订检查与原子写入 | 保留并复用，不另造第二个明文 Key 存储 |
| `agent.provider.*` / `user.provider.*` 等公开方法 | 按 §4.1 逐方法兼容；保留授权权威，不整组删除 |
| launch 序列化的 `provider=harness`、`modelProvider` | 首批保留 wire 语义；内部使用明确类型转换，禁止原地改含义 |
| provider-key 共享权威/远端发行 | 不由此方案替换或启用；KP-030 等已知边界另属原后端工作 |

“模型连接”是本文提出的产品配置概念：稳定引用、地址、协议、认证引用、修订，
不是新模型目录。ID 不以厂商名为唯一键；同名模型的两个 endpoint/Key 必须能隔离。
Pi 内部注册 ID 由连接身份生成，不直接使用显示名或用户模型 ID 做 provider ID。

### 4.1 公开面逐项处置

keep=原语义保留；compat=旧调用保留，新流程使用明确绑定；retire=从新用户流程移除，
不表示当前删除公开 API。下表测试为基线已有落点，相关新增行为在对应目录补回归。

| 方法/入口 | 处置与消费方 | 测试落点（相对仓库根） |
|---|---|---|
| `agent.provider.login` | keep：CLI 登录/GUI 原生 Auth；只安装明确来源，不替用户选择模型连接 | `tests/daemon/harnesses/providers/test_daemon_provider_login.py` |
| `agent.provider.logout` | keep：CLI/GUI 撤销登录材料；显式绑定变为不可启动，不补另一份 Auth | `tests/shell/cli/agents/provider/test_provider_logout_command.py` |
| `agent.provider.auth.list` | keep：GUI 原生 Auth 清单，只返回脱敏目标与状态，不新增模型列表 | `tests/daemon/harnesses/providers/test_native_auth_inventory.py` |
| `agent.provider.default` | compat：CLI legacy read/set/clear 及 captured-Claude default 保留；新绑定创建完全不消费它 | `tests/shell/cli/agents/provider/test_provider_default_command.py`、`tests/daemon/harnesses/providers/test_default_claude_credential.py` |
| `agent.provider.allow` | keep：CLI owner-key 授权，不等于选择连接或模型 | `tests/shell/cli/users/provider/test_agent_access.py` |
| `agent.provider.revoke` | keep：CLI owner-key 撤销；下一次注入重新检查 | `tests/daemon/provider_keys/service/test_grant_flows.py` |
| `agent.provider.status` | keep：CLI 授权状态，不宣称模型可调用 | `tests/daemon/provider_keys/ipc/test_handlers.py` |
| `user.provider.list` | keep：用户 Key 状态清单，与 Secret 通用连接分层 | `tests/shell/cli/users/provider/test_list_logout.py` |
| `user.provider.login` | keep：owner Key 安装，既有密码学/准入边界不变 | `tests/shell/cli/users/provider/test_login_rotate.py` |
| `user.provider.logout` | keep：owner Key 退出，不能触发换 Key/模型 | `tests/shell/cli/users/provider/test_list_logout.py` |
| `user.provider.rotate` | keep：轮换授权材料，不改变连接 configRevision | `tests/shell/cli/users/provider/test_login_rotate.py` |
| `agent.secret.grant` | keep：明确授予 Secret 引用；route_required 和授权修订检查保留 | `tests/identity/agents/secret_storage/test_grant_resolution.py` |
| `agent.secret.list` | keep：当前 Agent grant 清单；GUI 不能由列表可见推导可用 | `tests/daemon/provider_keys/legacy/test_legacy_secret_list.py` |
| `agent.secret.revoke` | keep：收回 grant；配置仍可回读为授权缺失，不隐式重启 | `tests/daemon/provider_keys/legacy/test_legacy_secret_grant.py` |
| `agent.secret.provider-configure` | compat：GUI 保存连接，新增显式版本化 connection 输入，旧 DeepSeek 参数保留 | `tests/identity/agents/secret_storage/provider_routes/test_persistence.py` |
| `agent.secret.provider-list` | compat：GUI 展示安全连接元数据/revision；不返回 Key，不添加模型候选 | `tests/daemon/provider_keys/legacy/test_legacy_secret_list.py` |
| `agent.secret.provider-test` | compat：GUI 单次临时模型测试；新调用以连接引用解析，旧临时输入仍可用，期限见 §3.2 | `tests/daemon/model_probe/test_contract.py`、`test_probe.py`（同目录） |
| `agent.secret.provider-write` | compat：旧 flat Key 写入保留；不可剥除新 route/config revision 回落成普通 Key | `tests/daemon/provider_keys/legacy/test_legacy_secret_write.py` |
| `agent create --provider/--model` 与 default seed | compat：无绑定的旧 CLI 保留；显式绑定创建由 daemon 统一校验，CLI/PAC 均不得额外 seed | `tests/shell/cli/agents/provider/test_create_seeding.py` |
| `agent start --provider/--model/--effort/--tier` | compat：legacy 保留；新绑定只允许与配置一致的断言，冲突先编辑，禁止临时覆盖 | `tests/daemon/harnesses/contracts/test_harness_model_provider.py` |
| `agent runtime tiers --combo harness:provider:model` | compat：既有查询保留；新通用连接的 effort/tier 未验收则明确不支持，不制造默认模型 | `tests/daemon/harnesses/contracts/test_harness_model_provider.py`（扩展 CLI 覆盖） |
| `agent interactive` | compat：原生/已有通道保留；首批新自定义连接在进入 TUI 前拒绝 | `tests/shell/cli/agents/test_cli_pi_interactive.py` |
| 磁盘 `provider` / `modelProvider` | keep：前者仍是 Harness，后者是既有模型适配 ID，绝不重用为连接引用 | `tests/daemon/desired_state/state/sqlite/test_desired_state_sqlite_roundtrip.py` |
| GUI 重复 provider 选择、固定模型白名单 | retire：新流程只选 Harness、认证与手填模型；官方预设不是模型目录 | `gui/tests/features/agents/agent-create-credentials.test.tsx`、`gui/tests/features/connections/model-service/form.test.tsx` |

原生 Auth 登录、owner-key 授权、模型协议适配是不同职责。改变用户表单不代表这些
后端机制退役；已有字段只加版本化新输入，不原地改旧 `provider` 字段含义。

### 4.2 连接记录的唯一存储与迁移

选择 **扩展现有 `ModelServiceRoute` / `model_records` 原子记录**，不建立第二份 vault。
新记录用 `$schema=hyprial-model-secret/v2`；在原文件、原锁、原原子替换内保存 Key 与
类型化连接元数据：稳定 connectionRef、凭据字段引用、endpoint、protocol、profile、
configRevision、预设来源/版本。ID 不因重命名改变，不再要求 vendorId 必为 deepseek。
同一文件有多字段时各连接分别修订；引用标识以现有 entry/field 为基础，不跨 owner 猜测。
替换地址/协议/profile/凭据槽提升 configRevision；同槽 Key 轮换只变授权材料修订。
授权仍由现有 grant 权威持有，连接记录不自动授予 Agent。

dual-read flat/v1/v2，未知 schema 拒绝。旧带 route 的 DeepSeek 记录以原地址、字段、
授权和模型继续可读；显式转换才写 v2，保留引用，原子失败留下完整旧值，不按域名猜厂商。
旧二进制已对未知 `$schema` 拒绝，必须用 v2 回归验证其不会当成 flat Key。
旧 provider-write 不得清掉 route 或降级 schema；普通 flat Key、原生 Auth、已有
Claude/Codex 通道各自回归，不做全库自动迁移。旧 grant 的 route_required 保留，
新绑定除它以外还检查 configRevision；任何不匹配拒绝，不尝试另一份凭据。

## 5. 和 Cell 的关系

核对基线：I6-1B #3105、I6-2 #3109、I6-3 #3117、I6-4 #3119、I6-5 #3127 已合入；
OC4 #3102 将归属及模型凭据授权本机化，S3a #3130 在本轮基线也已合入。
Cell 看板与实际实现均未证明通用自定义 Key 启动已接通。

Cell 成员保存非秘密运行意图（kind/model），allow/deny 控制执行能力，不是模型服务 Auth。
不把 Key、Auth 内容、访问令牌或本机凭据引用放入 Cell、OrgFS、日志；对外状态只返回
调用者有权读取的绑定摘要。本批限定现有单 Harness 成员管理路径，不扩成多 Harness UI。

### 5.1 存储决定与消费路径

选择 **现有本机 desired-state SQLite 中新增 `model_bindings` 表**，按稳定 agent/member ID
索引；它是同一 desired-state 权威中的配置意图，不是新数据库、第二套凭据存储或 Cell 真相。
本文所有 `agentId` 明确定义为 `Agent.agent_id`：由该 incarnation 的 entity_token 派生的
不可变 `a.` typed ID（`identity/impl/agents/registry/_base.py`）。不是 actor 名、显示名或
可重用别名；重建同名 Agent 必有新 agentId。`memberId` 是对应 Cell 成员的稳定 ID，
联合键以二者原值持久化；不能在 purge 时重新按显示名解析成另一个 incarnation。
原因：现有 HarnessLaunchSpec 行是“曾成功运行 + 上次结果”，首次启动失败可能没有行，
不能让创建时的配置依赖一次成功启动。也不把秘密或私有引用写进共享 Cell 文档。

拟议 binding 记录：bindingRef、bindingRevision、authKind、connectionRef+configRevision
或唯一 nativeAuthRef、显式 model、Harness/mode、Cell member 意图摘要、协调阶段和操作 ID。
Key 值仍只在 §4.2 的记录中，grant 权威不复制到此表。bindingRevision 是本机用户编辑版本；
configRevision 是连接地址/协议/profile 版本，两者与授权修订不同。
`HarnessLaunchSpec` 增加可选 `model_binding={bindingRef,bindingRevision}`，仅表示该次
启动使用的快照引用；保存的成功启动行不能反向成为当前配置，也不能覆盖 binding 表。

配置操作回执也保存在同一个 desired-state SQLite 的 `model_binding_operations` 表，
唯一键为 `(agentId, memberId, operationId)`；字段包括 requestDigest、候选摘要、初始期望
revision、阶段、终态结果和结果 revision。requestDigest 来自版本化规范化请求（目标、
候选配置和期望修订），模型 ID 保持原样，不含 Key 内容。它保存跨重启的幂等依据，
不是另一份当前绑定；prepared 消除不删除此行。
finalize/cancel 与终态回执在同一 SQLite 事务提交；后续操作不能覆盖旧 operationId 的
摘要/回执，同 ID 不同摘要拒绝。终态记录保留到下述真实 Agent 销毁清理，不设置
TTL 或容量驱逐后把旧 ID 当新请求。默认 `MODEL_BINDING_OPERATION_LIMIT=10_000`
限定每个 `(agentId, memberId)` 的总操作行数（含 prepared 与所有终态）；PR 3 的同一
条件写事务在插入新 ID 前检查计数，默认第 10,001 个新 ID 返回
MODEL_OPERATION_CAPACITY_EXCEEDED、phase=bind、reason=operation-capacity，未写 prepared、
未改 Cell。先检查已存在 ID：同摘要回读/恢复、不同摘要拒绝、既有操作 cancel/finalize
不占新名额，满额时仍可完成。PR 3 必测 9,999/10,000/10,001 与并发最后一个名额。
本机运维可显式调高容量，无需销毁 Agent：PR 4 扩展现有 `hyprial config set` 的
白名单键 `modelBinding.operationLimit`，写现有 `<HYPRIAL_HOME>/settings.json`；
它是拟议的新配置项，当前版本尚不支持。只接受整数 10,000..2,147,483,647，缺省 10,000；
该键由本方案直接实现，复用现有 settings writer、校验规则和 settings.json；保留已初始化
home 的离线 config set。当前没有已定的通用 config.set IPC，不等待或为此新造远程接口。
后续在线消费者也必须复用同一公共面；daemon-request 可在实现后补充声明与架构梳理。
非法/损坏设置拒绝新配置操作，不能静默放宽上限。复用现有原子 settings 写入与本机权限，
不增加远程 owner 权限，不由模型 configure 请求自行提交上限。
每次 configure 在进入存储事务前读取一次上限及其解析结果并传入条件写；非法值仅在
新 ID 容量判断阶段拒绝，不阻断已有 ID 的回读/恢复及 cancel/finalize。status 返回本次
effectiveLimit 与计数；修改下一请求生效，不需要清空回执或重启。调低不删已有行，
达到/超过新限额只挡新 ID，已存在 ID 的幂等/取消仍可用。测试覆盖满额→调高→新操作
可提交、旧回执仍可读取、调低不删除、非法值拒绝；PR 4 同步 CLI 帮助/运维指南与 skill。
此上限不限制现有 Agent 启动，仅拒绝更多配置操作；不引导用户销毁 Agent 以清空历史。
状态回读同时给出原结果修订
和当前绑定修订，旧成功回执不表示当前仍在使用那份配置。

清理绑定 **现有 `agent.destroy` 的真实实体删除**，不把 stop/deactivate 或
`desired_state.remove_harness` 当作销毁。基线 `agents_admin/destroys.py::_destroy_agent`
在 `_stop_agent_runtime` 后实际调用 `self.agents.destroy(name, expected_entity_token=...)`；
当前没有新增表的清理钩子。标记放在同一 SQLite 新表 **`model_agent_lifecycle`**，
主键是 agentId，值为 `cleanupPending={entityToken,destroyAttempt}`；它独立于 member
和 binding 的存在性，无绑定也必须创建该行，不能只给现存 binding 打标记。
PR 4 已取得 destroy reservation、复核同一 incarnation 与删除资格后，必须在
`before_effect`、`_stop_agent_runtime`、删除路由/消息/home 等破坏性动作之前，通过
PR 3 条件写持久创建该标记；写失败立即中止并回滚可逆 reservation，Agent 实体仍保留。
标记操作明确定义为按 agentId 的 insert-or-takeover：取得现有 destroy reservation 并
确认同一 incarnation 后，允许对该 agentId 已存在标记做条件更新，将 destroyAttempt
转交本次尝试；不是只允许 insert。接管使用旧 destroyAttempt 作为 CAS 预期值，不能抢占
仍被现有 destroy reservation 持有的其他执行；后续 rollback/purge 都必须匹配本次 attempt。
这样 marker 写后崩溃但实体仍在时，configure/start 继续拒绝，用户重试 destroy 可以
接管并完成删除与清理，不会因标记已存在而永久卡住。
所有成员的 configure/finalize/start/restore 在共同本机准入处先检查 agent 级标记；
写 binding 的事务必须再次检查同一 agentId 无标记，不能用事务外预检替代。
destroy 标记写入与 configure 条件写串行化；销毁一旦取得标记，已有在途配置也不能
finalize 重新激活实体。需要回读已存在的操作时可用 status，不允许 configure 绕过标记。
真实实体删除确认后调用 PR 3 提供的
`purge_model_bindings_for_destroyed_agent(agentId, entityToken, destroyAttempt)`，单 SQLite
事务删除该 incarnation 的全部 model_bindings、model_binding_operations 及
model_agent_lifecycle 标记；不会按名称删除同名新 Agent，也不撤销其他 Agent 使用的
连接/Secret/grant。没有绑定仍须完成标记删除；重复 purge 已不存在的旧 ID 才是幂等空操作。

删除 Agent 与清理本机表不是跨库原子事务：删除前失败且旧实体仍在时回退 cleanupPending；
删除已成功而清理失败时保留标记，返回 MODEL_CONFIGURATION_INCOMPLETE、phase=bind、
reason=destroy-cleanup-pending，并报告实体已删除/清理未完成，不声称 destroy 完整成功。
PR 4 在 `application/restore.py::_restore_harnesses` 之前、identity registry 已可权威读取时
执行同一个幂等清理 helper；仅清理带该标记且旧 incarnation 已确定不存在的行，registry
不可读/状态未知时保留并阻断。`_cleanup_destroyed_agent_home` 的恢复路径也调用 helper，
以持久标记定位旧 ID，不能按用户名称猜。误停止/普通 restart、Cell 暂时移除成员、失败
replace 均不清理回执；本批不新增 member 独立 purge 或改变 destroy 的确认权限。
PR 3 负责 agent 级标记/两业务表及标记的同事务清理与 JSON roundtrip，PR 4 负责上述
销毁和启动恢复钩子、preview 的清理范围摘要及故障注入。必测：无 binding 时 destroy
并发 configure；同 agent 两 member 的交叉准入；marker 写失败时实体及破坏性数据保留；
purge 失败时 MCI 与标记保留；registry 不可读时保留并阻断；删除后 purge 前重建同名
Agent，新 agentId 的行不受旧 purge 影响；删除成功后崩溃与重启幂等清理。
PR 4 另须测试 marker→崩溃→重启（旧实体仍在）→configure/start 仍拒绝→重试 destroy
接管成功→两业务表与 agent 标记同事务 purge、JSON 投影清空该 agent 条目并保留三个 JSON 根；同名新 incarnation
的行与标记均不受影响，旧 attempt 的迟到 rollback/purge 不能删除新 attempt 标记。

`AppLocalMemberFacts` 从同一次 desired-state 读取拿到绑定，统一 resolver 校验连接与授权，
生成本次 LaunchSpec；create/edit/start、daemon restore、reconcile、routines 和 teleport
落地都经过该入口。`reconcile.py` 现有 `_persist_document_fields` 不能单独更新 model
并绕过绑定协调；必须同事务维护本机意图或将分歧标为不可启动。
teleport 不搬本机 Secret/grant/ref；目标缺少本机映射时返回 auth/binding-required，
由目标 owner 显式绑定，不能回落机器默认 Auth。

JSON 导出用根 `modelBindings` / `modelBindingOperations` / `modelAgentLifecycle`
表达配置、操作回执与 Agent 级销毁标记；现有整文档保存须完整保留三者，PR 3 做
roundtrip 和混合 writer 回归，未识别该标记的 writer 不得擦掉它。
首个新绑定提交时，SQLite 根 schema 和
对应序列化升级为 **desired-state schema 4**（本基线为 3），迁移与写入同事务。
schema 1–3 没有绑定时继续 legacy；未知字段/版本拒绝，不能仅加可被旧 reader 忽略的字段。
旧二进制必须在读 schema 4 时整体拒绝该 store，不允许忽略绑定后按旧 modelProvider 启动。
代价是启用后不能无损降级；不自动改回 schema 3，不从残留旧 JSON 恢复启动。
PR 3 须用旧 reader 与 SQLite/JSON 两种入口验证这一点，未证明前不开放新绑定写入。

### 5.2 Cell 与本机写入的分歧处理

拟议 `agent.model.configure` / `agent.model.status` 为配置及安全回读入口，
`agent.model.cancel` 为取消未完成配置入口；三者都归 PR 4 的字面量注册/契约/生成物。
cancel 显式指定目标 agent/member、目标 operationId 和 expectedBindingRevision，
不取消一个未指明的“最新操作”。新 `agent.create`
可携带显式配置，由 daemon 调用同一协调器，GUI 不自行编排多份写入。
锁外只做参数格式/可用性预检，预检不能作为修订仍有效的证明。现有 config-rebind fence
仅覆盖单次本机条件写入与证明回读；不跨 Cell CAS、网络或授权外部 I/O 持锁。
prepared 是释放本机锁后跨步骤的持久标记。按以下顺序执行，不宣称跨存储原子提交：

1. 取得本机 fence，在同一事务内条件检查 expected bindingRevision、同一 agent/member 没有其他 prepared，
   再写 prepared 操作、旧绑定及候选绑定；新建使用唯一键的 insert-if-absent。
   该条件写是存储层新操作；不能用现有整文档 write_document 做 Python 先读再写。
   唯一索引约束同一 binding 只有一个进行中操作，不限制其他 agent/member。
   CAS 失败不写 prepared、不改 Cell；成功回读证明后释放 fence。连接 configRevision
   作为候选 pin 保存，授权在最终启动时重验，不能把锁外检查当作跨存储锁。
   启动和 reconcile 必须先查 prepared，不能继续用旧绑定接新工作。
2. **不持本机 fence**，以预期 Cell 修订做 CAS，发布新的非秘密成员意图；只有连接变化时无需伪造 model 变更，
   仍记录本次已核对的成员意图摘要。CAS 冲突保留 prepared/conflict 可回读，不伪造成功。
3. 锁外回读确认目标意图，再取得短本机 fence，事务条件检查操作 ID、候选摘要、旧
   bindingRevision 和 prepared 仍属于本次操作，才置 active、递增 revision、清除 prepared。
   Cell 已写、本机完成写失败时保留 prepared；返回配置未完成，禁止模型启动和 stale launch。
4. `agent.model.status` 回报操作 ID、phase、两端是否确认、期望/观察修订。恢复协调器只对
   确认是同一操作且摘要一致的提交做幂等 finalize；不能重发上游模型请求或猜测另一份 Key。
   无法判定则保持 blocked，用户重读后显式重提或取消。取消涉及 Cell 回写时也须 CAS。

同一 agent/member 的 prepared 存在时，新的 `agent.model.configure` 拒绝，返回
MODEL_CONFIGURATION_INCOMPLETE、phase=bind、reason=operation-in-progress。
唯一例外是同 operationId 且候选摘要一致的幂等重试，只读取/恢复已有操作，不创建第二个
prepared、不重复递增 revision；同 operationId 携带不同摘要返回 MODEL_CONFIGURATION_CONFLICT、
reason=operation-digest-mismatch。每次先读取 §5.1 操作表；已完成操作的同摘要重试返回
持久原回执，不能重新执行 Cell 写入。取消使用上述独立 `agent.model.cancel`，
仅在外部结果已确认并完成必要 CAS 补偿后清理 prepared；遇未知结果不先解锁准入。
配置协调器之间的竞争由本机事务条件写入判定；长时间 Cell I/O 不占全局 writer fence。
cancel 同一已取消操作返回原取消回执；对已 finalized 的操作返回
MODEL_CONFIGURATION_CONFLICT、reason=operation-already-completed，不暗中撤销已提交配置。
取消和恢复 finalize 竞争同一个条件写：只有一个终态能提交，失败方重读终态回执。
对结果未知的 Cell 写入，取消保持未完成/阻断，不假定写入未发生。配置重试读到 cancelled
即返回取消结果，不重建 prepared。PR 3/4 覆盖跨重启同 ID 同/不同摘要及取消竞争。
拒绝优先级固定为：格式与权限 → Agent 级 cleanupPending → 已存在 operationId
的摘要/终态 → 同 binding 的其他 prepared → expected revision → 新 ID 容量。
后四项在存储同一事务内判定；满额且有另一个 prepared 时返回 operation-in-progress，
不是 capacity。满额时同 ID 同摘要返回原结果/恢复、不同摘要返回 digest mismatch，
cancel/finalize 不因容量满被拒绝；但 Agent 级销毁标记始终阻止 finalize。
PR 6 的 Agent 配置详情必须展示进行中操作与“取消未完成配置”，消费同一 status/cancel
契约，沿用管理该 Agent 的权限；按钮显式关联 operationId/期望修订并确认补偿影响。
关闭对话框不是取消。取消遇未知 Cell 结果时保留“未完成”并允许安全刷新，不假装取消成功；
已提交配置只提供编辑，不给误导性的取消按钮。CLI/agent operation 与 GUI 共用该入口。

每次启动比较当前成员意图摘要，不以整份 Cell 的无关成员修改触发重启；配置协调时的
CAS 仍使用真实 Cell revision。崩溃点和并发写入均须注入测试，尤须覆盖
“Cell revision 成功、本机 finalize 写失败 → 重启 daemon 后仍阻断、状态能读、幂等修复”。
成功保存配置不自动承诺进程已切换。切换失败时报告新配置失败/degraded；旧进程若因
既有回滚机制仍在，必须显式呈现，不可代替新配置继续接受新工作。

### 5.3 配置修订与授权修订分开

地址/协议/profile/凭据槽/模型变化是配置变化；同槽 Key 轮换、grant 重新授权或撤销是
授权状态变化。后者不修改配置摘要，不因 reconcile 把它误识别为用户换模型而重启进程。
每次新的注入、恢复和启动重新检查当前 grant；撤销即拒绝下一次解析，缺失显示 degraded。
已注入进程的 Key 不会因数据库撤销自动消失；沿用已有撤销/停止语义，若要终止运行实例须
明确停止，不把本设计描述成实时远端吊销。连接 revision 被他处更新时，旧 pin 拒绝启动，
用户明确编辑接受新 revision；不默默更新所有 Agent。

参考 [Cell 迁移说明](../guides/cell-authority-migration.md) 与
[已知问题 KP-122/KP-131](../reference/known-problems.md)。既有有界恢复语义继续适用，
没有证据表明策略回滚可以直接等价用于切换模型凭据；PR 4 必须专门验证。

## 6. Magpie 比较及建议

infra-ops 已确认用户所指项目为 [yetone/magpie](https://github.com/yetone/magpie)。
本轮只读比较固定源码 `27c27ef66ee65bb3d4e02d4fcc5652660cb60ae1` 的文档，未安装、
启动或导入用户凭据。其 MIT 许可允许按许可使用，是否嵌入仍需维护与分发方案。

官方 [Codex 接入说明](https://usemagpie.ai/docs/codex-models) 描述本机网关转换
Responses/Chat Completions/Anthropic Messages，并写入 Codex 配置。
官方 [远程部署说明](https://usemagpie.ai/docs/remote) 描述共享网关、按客户端发放网关 Key。
这证明上游提供相关能力，不证明 Hyprial 的隔离/恢复/授权已兼容。

| 方案 | 收益 | 新增责任 | 本批建议 |
|---|---|---|---|
| Pi 原生协议直连 | 用现有 loader，最短路径支持 URL+Key+手填 ID | 通用连接解析、模型运行配置、Pi 版本测试 | 首批采用 |
| 将已有 Magpie 当外部模型 endpoint | 利用网关转换或远程共享；Hyprial 仍只管所选连接 | 网关 Key、精确模型路由、网络与服务可用性验收 | 预留为可选连接，未测不宣称支持 |
| 把 Magpie 打包为 Hyprial 必需组件 | 可统一跨 Harness 协议转换 | 进程生命周期、端口、升级、状态/凭据归属、平台包、故障恢复 | 当前不采用，独立后续提案 |

外部 Magpie 和捆绑 Magpie 是两件事。用户已有网关，可在协议匹配且通过验证时把它
当作自定义连接；模型 ID 仍手填网关实际接受的完整 ID，不抓取或展示 Magpie 模型目录。
这不是在本批自动扩大 Claude/Codex 通用自定义支持。

Magpie 提供多 Key、fallback、group/auto 路由及 agent 配置管理，见固定版本
[路由设计](https://github.com/yetone/magpie/blob/27c27ef66ee65bb3d4e02d4fcc5652660cb60ae1/docs/subsystems/gateway-routing.md)
与 [Agent 接线](https://github.com/yetone/magpie/blob/27c27ef66ee65bb3d4e02d4fcc5652660cb60ae1/docs/subsystems/agent-wiring.md)。
要承诺本产品的精确失败关闭，不能直接采用自动 group/fallback 或全局账号接管：

1. 验证网关已绑定指定 upstream/Key/model；关闭非显式授权的替代和跨 Key 回退。
2. 隔离配置/登录状态与进程，不改用户日常 Claude/Codex/Pi 的全局配置。
3. 断网、网关退出、错误模型、鉴权拒绝、流式断开均返回可区分失败，不偷换成功结果。
4. 验证工具调用往返、取消、会话恢复、额度与日志脱敏；支持版本固定可回滚。

Hyprial 无法仅从兼容 HTTP 响应证明外部网关内部没有替换模型；未经上述验收，只能
保证发出的 endpoint/凭据引用/model 精确，不能宣称网关内的端到端精确路由已经认证。

### 6.1 远程 Magpie 与 #3120

HQ 上下文是 Docker Magpie 供另一台 host 使用；这是独立的远程服务授权场景。
已读取 [#3120](https://code.hyprial.com/HyprialOS/hyprial/pulls/3120) 候选
`c334cf7f4c427cf82ee3c0cf7f02359f69523428` 的 WIP 文档：当前只在授予端保存密封信封，
未实现远端投递、设备 grant 公钥发现、签名验签、撤销/轮换/过期或已有 Secret 引用。
因此不能把 #3120 等同为远程 Magpie 已接通，也不让它阻塞本机 Pi 修复。
后续接入只委派作用域受限的网关授权，不默认分发模型厂商原始 Key。

## 7. PR 施工拆分

当前 **PR 0 为持续更新的 WIP 设计文档**，首轮评审后调整为 **6 个实现 PR**；用户已授权按下列阶段串行实施。
各 PR 独立可测、兼容先行，再开放界面；前置未就绪时新组合仍拒绝。

| PR | 范围/主要目录 | 必须交付的验收 | 目标文件数 |
|---|---|---|---|
| 0 | 本设计、术语、调查与评审证据 | 链接/术语检查；六轮意见回应与 daemon-request 边界核对 | 10 |
| 1 | identity model_records/Secret、连接 request_catalog、error/codes、shell/impl/request_http、probe params/期限、contract、生成客户端/mock | v2 原子 route+Key；旧 routed DeepSeek 显式迁移/失败回读；flat/Auth 回归；按方法 carrier 预算与 schema 同步，其他方法不变 | 30–44 |
| 2 | harnesses/model_routes、model_provider、Pi loader/actor、model_probe、kernel manysys/fs 本机存储检查 | 通用模板/版本/隔离；新旧快照负例；跨资源单 writer 与两个 start 终局/无孤儿；RPC 真实 stop/finally 释放；本机 root 准入；错误映射到 start 契约 | 30–44 |
| 3 | kernel LaunchSpec 类型、desired-state 表/序列化/迁移、解析公共面、对应契约 | 首次启动前配置/回执；schema 4 降级；事务条件写/唯一索引；可注入容量与优先级；agent 级 cleanupPending/双表及标记 purge/roundtrip | 25–40 |
| 4 | agents_admin/agent_start/destroy、Cell facts/reconcile、CLI/config、restore/teleport/routines、契约/生成物及 skill | configure/status/cancel；短锁+CAS/prepared；真实绑定；agent 级销毁准入/恢复/preview；operationLimit 运维入口；故障注入/default seed/旧 CLI | 30–44 |
| 5 | GUI connections 共享组件/控制器、mock/source ledger、测试与操作说明 | 官方预设/自定义；无模型目录；测试 ID 临时手填；期限/错误/未验证状态；共享入口实点证据 | 15–30 |
| 6 | GUI agents 共享表单/详情/启动、mock/source ledger、测试及旅程证据 | Harness+认证+手填模型一次保存；启动不重复选择；未支持模式拒绝；未完成配置 status/cancel；新建/编辑/恢复与旧 Auth 旅程 | 20–35 |

PR 1 同步 `contract/daemon-request/openapi.json`、错误码 registry 与字段/phase schema，
核对 A03 import/legacy-method ledgers（只减不增，不扩基线豁免）；执行
`scripts/ci/check_request_contracts.py`、`scripts/ci/check_daemon_contract_diff.py` 和生成客户端/
Contract Mock 校验。PR 4 新方法用模块级字面量注册；每个后续修改契约的片都同步同套生成物。
PR 1 只声明的新模板能力在 PR 2 前不可启动，PR 3 存储在 PR 4 接线前不被用户入口写入。
PR 4 的 CLI/Agent 操作变化须更新相关操作 skill 与指南，并在正文写 `Skill-Impact: updated`；
GUI 两片同步 source ledger、mock conformance 和 UI 交接，不把 mock 通过当真实上游验收。

按上述顺序串行交付到 dev，每片保持旧调用兼容；用现有能力声明控制新入口，不增加
绕过校验的 flag。当前文档 PR 不累积实现变更。若评审决定采用 intg 容器，每个最终
容器 PR 也必须小于 50 文件；不能用多个小 PR 掩盖一个超过限制的总 PR，需重拆阶段。
实际文件清单在每片开工前登记，45 文件时预警，达到 50 必须拆片；超过预算应更新本表，
不承诺仅靠估算就一定是六片。PR 4 开工前按真实文件清单核算，超预算则先拆 CLI 与 Cell，
不削掉恢复/契约/生成物来维持六片。生成物不得移出计数。

### 7.1 与 daemon-request 线的已确认边界

2026-11-24，codex-daemon-request 对照 dev `ea728eebe46981fa53d00e05069abe438f91cf4f`、
本方案 `e885d78d3e` 与 [#3138](https://code.hyprial.com/HyprialOS/hyprial/pulls/3138)
设计回复：**本线 PR 1 不依赖其 P01，可以独立推进**。该回信是依赖判断；随后用户已明确授权本方案先实现，不与其他线互等。
原文保存于 [daemon-request-boundary.md](../notes/model-connection-plan-20261124/daemon-request-boundary.md)，
messageId=`8a1ce56c-225b-5aed-8cab-9918fc470948`。

| 范围 | 负责人/依赖 |
|---|---|
| `agent.provider.allow/revoke/status`、`user.provider.list/login/logout/rotate` | daemon-request P01 只补 7 个已有 HTTP/GUI 方法的成功、失败和权限示例；不重建 IPC/注册/授权。本方案 keep 原行为 |
| `agent.secret.provider-configure/list/test/write`、model_records v2、model_probe/request、provider-test 专属 HTTP 预算、新错误码 | 模型连接 PR 1 负责；不改 owner-key 授权/轮换权威，保留 4 方法旧输入 |
| 原生 Auth 登录/清单、Secret/grant | 复用原公共面，不因新连接另造权威或批量退役 |
| `agent.model.configure/status/cancel`、create/start/restore 与 Cell reconcile | 模型连接 PR 4 引入并负责契约；直接复用当前启动/恢复/Cell 公共面实现，带齐契约与回归；后续 daemon-request 可据具体 diff 接线整理。Pi reservation 不替代现有租约/session 权威 |
| `modelBinding.operationLimit` 的 config set | 模型连接 PR 4 提供新配置键，复用现有 settings writer，保留离线能力，不依赖 P19 或尚未存在的远程 config.set |

共享文件包括 `contract/daemon-request/openapi.json`、
`gui/src/dev/contract-mock/contract.generated.json`；`gui/src/client/generated/index.ts`/
`types.ts` 按实际 schema 差异更新。两个实现各自带齐生成物，后合入方从与 dev 合并后的
源声明重新生成，不能手选一侧旧快照。分别回归 7 个 owner-key 方法准入/行为、4 个连接
方法旧输入、provider-test 专属预算和 GUI Contract Mock。
以上是设计协调时的边界记录。P01 随后的实现 #3143 已合入 dev，PR 2 已同步该提交，
共享生成物从合并后的源声明重生成。不能把“设计兼容、可并行”表述成“永远没有 Git 冲突”。

### 7.2 任务执行对照（2026-11-25）

流程固定为：开发并保持 WIP → cc-gui-orgs 独立实现评审通过 → 去 WIP → 同一 SHA 正式 CI 通过 → e2e-verifier 正常合并 → 下一阶段。
若合入新 dev 或修改实现，记录新 base/head，并由评审判断需要重验的范围。设计评审不替代此流程。

| 阶段 | 实现和固定版本 | 验收与剩余事项 | 状态 |
|---|---|---|---|
| PR 1：连接记录与请求契约 | [#3141](https://code.hyprial.com/HyprialOS/hyprial/pulls/3141)，评审/CI head `cb9eb3bf74df398524030365969239ff0fa50f90`；用户手动合并 `4766a1f1be4de1c8e84e1330dc30fa903946c3da` | v2 Secret/CAS/迁移、旧目录兼容、精确探针、取消、HTTP 预算与契约已交付；Linux CI run 58214 全部选定 lane 成功。GUI/真实厂商不在本片验收范围 | 已合并 |
| PR 2：Pi runtime 与会话保护 | [#3153](https://code.hyprial.com/HyprialOS/hyprial/pulls/3153)，head `e4d2d2ac77f3440648e16a2268f69aa62117ed0c`，base `5fc90df53b46082f6c2a47ad4aebae5a2e578873`，46 文件 | §2.3/§3/§8 产品代码及非回归范围独立 PASS；原始全量39F/2E/31045P仍记FAIL，24项基线已交接，额外16项双方分别串行复跑通过。两处GUI错误文案门遗漏已修并独立PASS。89bda正式重跑Python五分片及unit通过，但GUI启动case N退出1（28P/1F）；本机精确用例及四组合复跑通过，原退出根因UNKNOWN。e4d仅修测试诊断保留stdout/stderr，红2F→绿2P，cc限定本地门PASS（ff6e4c16）；单CPU有界压力三次case N复跑3P | 本地门PASS；e4d正式CI FAIL，独立修复#3155进行中，未交e2e |
| CI 阻断修复：Lark completion | [#3155](https://code.hyprial.com/HyprialOS/hyprial/pulls/3155)，head `e25f726a7c73d70dc2eccb64a4e8c8f0dac05930`，base `3e9b4adf7f119d013e0d4926db7caaa2618c71ec`，2 文件 | #3153 run59162/shard3 原始7101P/1F保留FAIL；Lark代码与PR2基线一致。完成通知对CLOSED仍重试5秒，拖住stop(2s)；独立一行修复保留receipt和原错误，过载重试不变。确定性旧实现2F/1P→修复shutdown19P，原超时/断言未改；完整焦点与独立评审待终态 | WIP；修复合入后再验证PR2 |
| PR 3：绑定存储与事务 | 未开实现 PR | §5.1：schema 4、首次启动前记录、操作回执/容量、Agent 级 cleanupPending、降级与故障回读 | 未开始，依赖 PR 2 合并 |
| PR 4：启动/Cell/CLI 接线 | 未开实现 PR | §2/§5：configure/status/cancel、prepared/Cell CAS/finalize、restore/routines、销毁恢复、生成物与操作说明 | 未开始，依赖 PR 3；新生产绑定仍关闭 |
| PR 5：连接 GUI | 未开实现 PR | §2.1/§3.2：预设/自定义、测试时手填 ID、显式 30s 预算、真实点击及错误/取消旅程 | 未开始 |
| PR 6：Agent GUI | 未开实现 PR | §2.2/§2.3：Harness+认证+手填 ID 一次保存、启动复用、status/cancel、创建/编辑/恢复实点验收 | 未开始 |

PR 2 的本轮设计精确化已随实现复评闭合：legacy 首次 receipt 的短占用属于兼容行为变化；
本机根使用 identity 映射的 `state/pi`；启动错误保留五种闭集分类，不能只转发 busy。
独立评审回执 `308d9c98-d870-5889-bc65-8cfd9d6862a6`：架构 201 passed、Pi/AF_UNIX 96 passed、
Pi 全目录 353 passed、post_install/OpenAPI/inventory 293 passed；Ruff 与结构/模块门通过。
这些集合有重叠，不相加冒充全量结果。评审位独立验证 Pi 1.2.0，另两版仍标为开发位实测。
016aa9与73679的产品src tree完全相同；全量终态保留原始失败，限定复跑不改写全量结果。
PR1的探针预算测试误将ping/catalog握手计为业务派发，已通过强制过期缓存稳定复现并修正测试断言；产品行为未变。
最终回执 `a169eaa3-c5e6-50aa-b9fe-6cb2cc7e1415` 确认016aa9本地门PASS并同意复用全量非回归证据。
#3153曾去WIP，正式GUI run58958出现本片遗漏（3029P/1F/1skip/4todo）：四个新启动错误码缺专属文案。
8a81只增改共享错误映射与语义/脱敏/结果未知测试，290P；类型、模块边界、共享入口静态门通过。
cc回执 `9c467203-331c-5d50-a208-57a286dbb631` 独立复现旧失败并确认8a81增量本地门PASS、Python证据可复用。
8a81正式GUI run59009为3033P/1F/1skip/4todo：新凭据错误文案命中只缩减的不可用词表。
89bda改为“无法取得所选模型连接的凭据”，并更新语义断言；原红1F/4P，修复后文案扫描与Agent295P、台账规则31P通过，扫描器/白名单/门禁未变。
cc回执 `6ae83287-8997-5c8f-a58d-14afdb0dec33` 独立复现旧失败并确认89bda本地门PASS，Python证据复用成立。
已再去WIP、draft=false。89bda正式CI首轮GUI/浏览器、native、mock harness E2E及静态门通过；Python59068仅旧PAC `test_closed_actor_backlog_cannot_starve_an_open_graph` 的0.5s等待失败（9799P/1F），其余分片通过，汇总仍FAIL。
该测试/PAC实现不在本片diff；同SHA不改断言，开发位独立10次、评审位独立5次串行全通过，支持时序敏感但不证明根因（回执1d13cc21）。保留原始FAIL，在全部任务终态后同SHA正式重跑一次；再次失败则继续定位，不skip或放宽断言。重跑全绿后才交e2e。生产绑定在PR4前不开放。
该重跑Python59106五分片及unit全部通过；GUI59103的case N却在startup JSON前退出1（28P/1F），缺失stdout诊断，尚不能归因端口或资源压力。
e4d仅改测试helper分别保留两stream的head/tail（总量<12k），加错误仍拒绝、诊断保留、listener清理回归（原实现2F，修复2P）。独立复评ff6e4c16及补证db8b0ef0均PASS：独立2P+295P、类型/模块/parity通过，Python证据可复用。已去WIP，e4d正式GUI59148、Python59151及相关门运行中；原FAIL保留，不以本机四组合4P替代Linux CI。

24 项 macOS 基线失败已按用户指示交接 CLI developer 作归属判断与择期维护：
发送 `75764cae-2487-5885-807d-9ed6db2e3ff7`，接收确认 `0d672269-558a-5cd5-b342-9a3b6074f37b`。
本线停止跟进其修复，不新增全局 KP、不把已知失败改写为 PASS；后续测试仍报告真实结果及与基线的差异。
归属未确认的部分继续标为 UNKNOWN，不因交接而认定由某一方引入。

## 8. 失败关闭与验收矩阵

| 情况 | 期望 |
|---|---|
| 模型 ID 空/非法 | 保存或启动前准确拒绝，无隐式默认 |
| 合法但服务端不认识的 ID | 保留上游安全错误分类，不换模型 |
| 不支持的 Harness/Auth/协议组合 | 准入拒绝，GUI 可解释；不从“Key 已保存”推断可用 |
| 撤销/缺失/重复授权，绑定版本不符 | 注入或发请求前失败，不尝试其他 Key |
| 同模型 ID、不同连接 | endpoint/Key 严格隔离；更新一份不影响另一份 |
| 测试通过后修改配置 | 测试结果关联旧摘要；新配置显示未验证 |
| HTTP 200 但错误 envelope/空结果/断流 | 不记模型调用成功；失败阶段可见 |
| 协议返回文本但不支持工具调用 | 文本测试可通过，Agent 工具验收单列失败/未验证 |
| 外部网关 HTTP 200 | 只能证明所发 endpoint/凭据引用/model 与响应分类，不能证明网关内部没有替换 |
| 新连接请求 TUI/interactive/attach | 首批在启动前明确拒绝，不继承 rpc 验收 |
| 续会话连接修订/模型不符或旧来源不明 | MODEL_RESUME_MISMATCH；明确新建会话或恢复原配置，绝不警告后重绑 |
| 两个 resource key 使用同一 sessionRoot/sessionId | loader 排他占用仅一个成功，另一方 MODEL_SESSION_BUSY；无上游请求/快照覆盖 |
| loader 异常退出、占用残留 | 不自动解锁或偷取，原会话阻断；明确新建会话，不冒充自动恢复成功 |
| 默认限额下第 10,001 个新配置操作 | MODEL_OPERATION_CAPACITY_EXCEEDED，Cell 未写；既有 ID 幂等/cancel/finalize 可用；本机运维可显式调高 |
| 无 binding 或其他 member 正在配置时销毁 Agent | Agent 级 marker 阻断所有成员新写入/启动，marker 写失败不开始破坏性动作 |
| 实体删除成功、本机清理失败 | destroy-cleanup-pending；重启按旧 incarnation 幂等清理双表，不影响同名新 Agent |
| Cell 已更新而本机 finalize 失败 | prepared 持久存在；MODEL_CONFIGURATION_INCOMPLETE；daemon 重启仍阻断 stale launch |
| 同槽 Key 轮换/重新授权 | 配置摘要不变，不隐式重启；下次注入验证最新 grant |
| 模型测试超过 30s 或用户指定期限 | outcome=timeout，未验证；无重试，不报为无效 Key |
| Cell 重启/daemon restore | 同一个显式绑定；失败为 degraded/failed，不替代授权或模型 |
| 新写入后旧客户端/旧二进制 | 按 schema 拒绝或已定义兼容读取；不能丢 route 回普通 Key |

每个行为变化先失败再通过；Python 产品变更按 AGENTS 跑焦点、全 tests 与 Ruff，
GUI 跑共享组件测试、模块边界及真实浏览器，结构/契约/生成物门不得省略。
基线实证中 Pi 1.2.0 可在 loopback 服务上使用三种自填 ID，但 CI 钉定 Pi 0.85.1；
两版和 1.3.0 上限都必须验，超过上限拒绝。真实天翼、Magpie、生产日志核验仍 UNKNOWN。
最终真实服务验证须使用获授权的隔离实例；不复用生产 daemon 或读取未授权凭据。

### 8.1 拟议安全错误分类

错误码常量加入真实权威 `src/hyprial/error/codes.py`（`hyprial.error.codes`），
其 `ALL_CODES` 统一收录；在 `kernel/impl/contracts/daemon/ipc_errors.py` 显式再导出，
保持公共 `hyprial.kernel.ipc_errors` 兼容。同步
`tests/daemon/ipc/error/test_ipc_error_code_registry.py` 的冻结清单与导出一致性测试；
方法 errors/phase/reason schema 随 `contract/daemon-request/openapi.json` 重生成，
探针语义同步 `contract/daemon-request/credential-probe.md`，生成客户端/mock 同片更新。
下表是新绑定调用的设计，
不是声称当前已有这些码。旧公开方法保持旧 envelope，通过版本化输入或新增安全 data 承载，
不能突然更改老客户端所依赖的语义。phase/reason 都为白名单，不含异常正文、Key 或请求体。

| code / probe outcome | phase / reason | 含义与恢复 |
|---|---|---|
| MODEL_CONFIGURATION_INVALID | validate / invalid-model-id、invalid-connection、operation-limit-setting | 请求未写入；输入错误由用户纠正，非法容量设置由本机运维修复 |
| MODEL_CONFIGURATION_CONFLICT | bind / revision-mismatch、operation-digest-mismatch、operation-already-completed | CAS 冲突、同操作 ID 换内容、取消已提交操作；重读，不覆盖或暗中回滚 |
| MODEL_CONFIGURATION_INCOMPLETE | bind / prepared、cell-unconfirmed、local-finalize-failed、operation-in-progress、destroy-cleanup-pending | 部分提交、其他配置操作或销毁清理未完成，回读并修复，禁止启动或覆盖 |
| MODEL_OPERATION_CAPACITY_EXCEEDED | bind / operation-capacity | 达到有效容量（默认每 binding 10,000）；拒绝新 ID，旧操作可完成；本机运维可显式调高，不能循环自动重试 |
| MODEL_SESSION_BUSY | admission / session-reserved | loader 占用；存活实例正常停止可释放，已死且残留只能新建会话，本批不强解锁 |
| MODEL_COMBINATION_UNSUPPORTED | admission / protocol、mode、sdk-version、runtime-option、session-storage | 组合或会话存储未支持，不能误报凭据失效 |
| MODEL_AUTH_UNAVAILABLE | authorize / missing、revoked、ambiguous、grant-changed | 提示确切授权动作，不尝试其他来源 |
| MODEL_RESUME_MISMATCH | resume / binding-changed、model-changed、provenance-missing | 显式新会话或恢复配置 |
| MODEL_UPSTREAM_ERROR | request / authentication-rejected、rate-limited、model-not-found、request-rejected、invalid-response、unreachable | 仅上游可识别安全码证明 model-not-found，不能把所有 404 猜成模型不存在 |
| outcome=timeout | probe / deadline-exceeded | 有界探针未验证；Agent turn 不使用此期限 |

不支持存储 schema 由存储层拒绝且不触发 launch；新绑定入口可归为
MODEL_COMBINATION_UNSUPPORTED、phase=load、reason=storage-schema。旧二进制的拒绝仍按其
已有 DesiredStateError 报告，不伪称能给旧版本增加新错误码。

PR 1 在 `hyprial.error.codes.ALL_CODES`/kernel 再导出/冻结测试中登记全部上述新码，
包括 MODEL_OPERATION_CAPACITY_EXCEEDED 和 MODEL_SESSION_BUSY，后续片才能消费。
当前 loader/rpc_client 只有单一固定标记 `HYPRIAL_PI_START_FAILURE:MODEL_UNAVAILABLE`
及精确匹配，不是通用 code 白名单。实现评审修订：PR 2 将它扩成固定五码白名单：
MODEL_UNAVAILABLE、MODEL_SESSION_BUSY、MODEL_RESUME_MISMATCH、
MODEL_COMBINATION_UNSUPPORTED、MODEL_AUTH_UNAVAILABLE。后四码分别覆盖会话占用、
来源不匹配、不支持的运行/存储条件与缺少 Key；若只转发 busy，其余 fail-closed 路径会
退化成普通 ConnectionError，无法满足 §8.1 的准确错误。loader 输出固定 stderr 标记，
rpc_client 仅解析这五码，后四码还须匹配固定 phase/reason；busy 不复用“模型不可用”文案。
安全 phase/reason 随 typed failure 传递，harness actor 与 daemon
start 保留该码，不能变成普通 ConnectionError 或“密钥解析失败”。PR 2 同片更新
`agent.start` errors 表与生成客户端/mock，并测试真实 child 拒绝→IPC 返回的整条链；
未知 stderr/堆栈不作为可回传分类。PR 4 的 configure/status/cancel 方法同步容量与
Agent 级清理错误契约。status 查询不因容量设置损坏而失败，返回设置无效的安全状态。

## 9. 评审和决策记录

CLI developer 联系地址已由 infra-ops 于 2026-11-24 确认：
`[redacted]:a.fj6ufosankmbssv5b3gqpgjz7q`。
infra-ops 回执 messageId：`97fc54a6-79fd-51b9-a26d-2686c5c313ee`。

WIP [PR #3134](https://code.hyprial.com/HyprialOS/hyprial/pulls/3134) 首轮提交
`2cadc8e3bb63f67bec25f5902fb7d48ad107e1a3` 已获只读评审；结论“需修改”，
Magpie/#3120 边界 PASS，其余四项待补齐。原文已保存为
[review-v1.md](../notes/model-connection-plan-20261124/review-v1.md)，
messageId=`186ddbac-426b-519c-882f-dc7cd428fba5`。评审人已确认与其当前工作无冲突。

| 首轮要求 | 本次修改 | 复评状态 |
|---|---|---|
| provider 逐方法清单、default/seed、存储选择 | §2/§4 增补各方法消费者与测试；新绑定不 seed；现有原子记录 v2 | 第二轮核实 PASS |
| Pi 具体模板、版本、mode、续会话 | §3 给值及未知成本语义；0.85.1/1.2.0/1.3.0；首批 headless；新绑定拒绝重绑 | 字段/版本核实 PASS；续会话快照本轮补齐 |
| Cell 绑定位置、降级、部分提交、授权分离 | §5 明确同一 desired-state 新表及 schema 4；prepared/CAS/finalize；配置与 grant 修订分开 | 存储与恢复前提核实 PASS；锁/并发本轮定界 |
| Magpie/#3120 | 保留首轮 PASS 边界；§8 加 HTTP 200 证明范围 | 首轮 PASS，未做产品实测 |
| 拆分遗漏、合同/生成物/技能/期限 | §7 改六片，存储独立；§3.2 明确 10s/15s 现状与新期限；§8 错误契约 | 第二轮要求更正错误码模块、指定 carrier 归属 |

第二轮评审对象为 `a6b0123eda1da18a3dbdbb6deb4f174a3cc80acf`，结论“主体可接受、需小修”。
原文保存为 [review-v2.md](../notes/model-connection-plan-20261124/review-v2.md)，
messageId=`e19804ec-0a49-5620-854e-30ea346e0592`。本次仅处理以下限定 diff：

| 第二轮要求 | 本次回应 | 状态 |
|---|---|---|
| A：错误码模块不存在 | §8.1 改为 hyprial.error.codes、ALL_CODES、kernel 再导出及冻结/合同同步 | 待限定复核 |
| B：carrier 预算未归属 | §3.2/§7 归 PR 1，指定 request_http 白名单及 schema/prepare 一起改，列出预算扣减/非法参数/其他方法不变测试 | 待限定复核 |
| C：跨 I/O 持锁、修订 TOCTOU | §5.2 本机事务内条件检查 bindingRevision；Cell CAS 在 fence 外，prepared 持久跨步骤 | 待限定复核 |
| D：续会话快照位置 | §2.3 复用私有 Pi loader receipt 的不可变 modelBinding 字段，写入/比较/缺失拒绝归 PR 2 | 待限定复核 |
| E：prepared 期间并发配置 | §5.2 拒绝新 operation；同 ID 同摘要只幂等恢复，同 ID 不同摘要冲突 | 待限定复核 |

第三轮限定复核对象为 `286a8eefeb3b6517367efd2c49f09877b54f6b2c`，当前移动 base
观察值为 `ea728eebe46981fa53d00e05069abe438f91cf4f`，不改变固定设计基线。
原文：[review-v3.md](../notes/model-connection-plan-20261124/review-v3.md)，
messageId=`461e0c86-a1ce-5698-b7bf-2bc5f3bc52b9`。A/B/C 可接受；D/E 尚需补充。

| 第三轮限定事项 | 本次修订 | 状态 |
|---|---|---|
| B：当前无 CLI probe；legacy 不补 30s | §3.2 只称 GUI 调用方；显式合法字段才启用 D+5，缺省保留 10/15s | 待文字复核 |
| C：条件写原子性/阻断范围 | §5.2/PR 3 明确存储层同事务条件写+唯一索引；prepared 只阻断同一 agent/member | 待文字复核 |
| D：后台 fence 与 legacy 口径 | 当轮曾将 record.starting 误作互斥；第四轮已纠正并替换为 loader 会话占用。两层 schema/legacy 口径保留 | 错误假设已被第四轮修订替代 |
| E：完成态回执/取消契约 | §5.1 操作表按 ID 持久摘要+终态至显式销毁；§5.2 cancel 与 finalize 互斥；§8.1 增补摘要冲突/已提交原因；归 PR 3/4 | 待限定复核 |

第四轮限定复核对象为 `91ee686823507edfb969010ae82b3fa3d5431240`；原文保存为
[review-v4.md](../notes/model-connection-plan-20261124/review-v4.md)，
messageId=`283069ef-3c46-516e-acc8-0705c4336ce3`。B/C 已闭合；D/E 指出两个真实遗漏，
以下为作者修订，尚未取得复核通过：

| 第四轮限定事项 | 本次修订 | 状态 |
|---|---|---|
| D：starting 不是互斥、restore/reconcile 可绕过 coordinator | §2.3 指名真实 coordinator/actor 准入；PR 2 在 loader 公共 run 入口新增会话独占占用，不宣称现有状态位已解决 | 待限定复核 |
| D：不同资源 key 共用 session 与 PR 2/4 接线时序 | canonical root+sessionId 排他 mkdir、无自动抢占、崩溃残留拒绝；跨资源反例；PR 2 仅隔离 fixture，PR 4 才从真实表提供 bindingRef | 待限定复核 |
| E：回执容量、错误契约 | §5.1 每 binding 10,000 行，原 ID 不占新额，§8.1 明确容量 code/phase/reason，PR 3 最后名额并发测试 | 待限定复核 |
| E：销毁钩子与 GUI cancel | §5.1 精确接入 self.agents.destroy 前后及恢复钩子，按 incarnation 单事务清理双表；PR 6 展示 status/cancel；不把 deactivate 当 destroy | 待限定复核 |

第五轮限定复核对象为 `ee86c87aa0870cfbf6061096e2541abddf608324`；原文保存为
[review-v5.md](../notes/model-connection-plan-20261124/review-v5.md)，
messageId=`8f3a5eb2-4d01-5132-b7d8-6036b59d37f5`。D 机制可接受，验收需补；E 的
Agent 级标记落点需改，容量/销毁主体和 GUI cancel 已认可。以下待限定复核：

| 第五轮要求 | 作者修订 |
|---|---|
| D：单 writer 不等于单进程 | §2.3 覆盖两个 start 终局、actor process/identity 归属、失败/过期 child 回收；必要清理归 PR 2，不能只数 writer |
| D：rpc 正常退出释放 | runControlledMode 取占用在 SDK 前，共同 finally；真实 EOF/SIGTERM/早失败/dispose 失败，异常残留明确只能新会话 |
| D：legacy 负例、mkdir、共享 fs、phase | 无快照旧 session 不补 receipt；父目录幂等、子目录非递归；受管私有 root+manysys locality 检查；中性 admission phase |
| E：跨 member/无绑定销毁 | 独立 model_agent_lifecycle 按 Agent.agent_id，所有成员准入同事务检查；真实删除后业务双表及标记同事务清理 |
| E：容量后果与错误优先级 | 默认 10,000，新增本机 config set operationLimit 调高入口；不清历史，已有 ID/其他 prepared 优先于容量，非法设置不堵回读 |
| E：定义与故障测试 | agentId=incarnation 派生 typed ID；无 binding/两 member/marker 写失败/purge 失败/registry 未知/同名重建反例齐列 |
| 错误码接线与旧评审措辞 | PR 1 注册常量、PR 2 loader→rpc_client→IPC 与 start errors；旧 starting 判断标明已纠正 |

本轮新增运维容量入口属于设计建议，仍由用户随整体方案确认，不在本轮修改本机 settings。
第六轮原文保存为 [review-v6.md](../notes/model-connection-plan-20261124/review-v6.md)，
messageId=`52460238-bccc-59f2-943d-f3136900f756`。D 设计层已闭合；E 的 Agent 级标记、
容量及其余验收已接受，仅要求补 marker 写后崩溃时按 agentId 接管 destroy 的反例和
单一固定错误标记的措辞。§5.1 已明确 insert-or-takeover/attempt CAS 与迟到清理保护，
§8.1 已写明从单一标记扩成固定两码白名单。限定修订待最终确认，实际验收归 PR 2/4。
用户提出的本机 provider 接线冲突问题已由 daemon-request 回复，分工/依赖/生成物处理
见 §7.1。用户随后明确：由本 agent 先实现，daemon-request 后续补充接线/架构梳理，不互相等待。

PR 正文已通过工作区既有宿主 fj 登录更新并回读。新增评审记录后同步为十二个文档文件、
六个实施阶段；任何为文件数上限拆出的阶段仍遵守每个最终容器 PR 少于 50 文件。设计固定基线与移动 dev base 分开记录。
评审通过与用户授权分开记录；以上历史轮次的状态不因后续实施授权改写。
2026-11-24 用户已授权按此方向先实施；从 PR 1 开始，保持模块边界、单一权威、fail-closed
及每 PR 少于 50 文件。独立设计复核继续，但不作为开工等待。实现验收另列，合并仍走保护流程。
旧 B 线不恢复。

第七轮限定复核已收到：messageId=707533c6-e2dc-5c86-8097-6b115e08fb2c，
精确对象 c66f53a64aa732427947d1468f0d57584b2d6ea2，两处均闭合，无方案阻断。
原文见 [review-v7.md](../notes/model-connection-plan-20261124/review-v7.md)。
非阻断精确化使用旧 destroyAttempt 为 CAS 预期值；purge 只清该 agent 条目，保留 JSON 根。
daemon-request 已撤回先会签才开工的前置要求，允许本线先实施全部阶段、之后按实际 diff 复核；
回信见 [daemon-request-followup.md](../notes/model-connection-plan-20261124/daemon-request-followup.md)。
实施阶段一容器为 [#3141](https://code.hyprial.com/HyprialOS/hyprial/pulls/3141)，
当前实现/测试状态以该容器及实施 PR 为准，设计闭合不代表运行验收通过。
