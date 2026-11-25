# CLI → daemon_request 当前树施工规划（design-only）

基线 `dev@533ef27ca42e53230224d55e58ba074f93d0eb15`。以下仅用于 WIP PR 设计；owner 确认前不实施。禁改 `gui/`、`src/hyprial/shell/impl/gui/`、`contract/daemon-request/contract_mock/`、`clients/`、`packaging/`；不扩 import baseline、不绕门。

## 口径

[imports.json](imports.json) 保存当前 417 条精确 import 三元组、470 次出现、69 个唯一源文件；全部完成静态逐边分类，并经 astra 复核与 sol 对账。24 个非 GUI 家族中有 60 个唯一涉边文件，公共 `common/output/runtime` 还有 9 个。下表 `E/F` 是当前家族“涉边文件数/整个家族递归文件数（含 __init__）”，不是命令数。`ΔCLI` 以 E 为需审阅的 import 文件下限，以 F 为组内可触及上限；精确例外可能无需改源码，此时实际 diff 可低于 E；`ΔD` 是预计 daemon handler/编排文件，`ΔC` 是 MethodSpec/catalog/contract 文件，`ΔT` 是焦点测试文件，`ΔL` 是棘轮账本/检查文件。Δ 是施工估算范围，不是已核实 diff。若已有声明/handler 可复用，估算可向下界收敛；若新契约横跨多个领域，先拆设计问题，不默默扩大 PR。每组另需按消费者实际 grep 决定额外文件，不在此伪造精确数。

| 家族 | E/F | ΔCLI | ΔD | ΔC | ΔT | ΔL | 输出/错误风险与新声明判定 |
|---|---:|---:|---:|---:|---:|---:|---|
| adapter | 5/7 | 5–7 | 1–4 | 1–3 | 2–7 | 1–6 | 配置、路由、授权轮询、媒体输出；已确认 `management.adapter.remove` 在当前 88 未声明中。`adapter.*` 8 项已声明；其余本地业务需在 daemon 侧确认方法形状，不能沿旧候选名建空 schema。 |
| agent | 14/27 | 14–27 | 3–8 | 2–5 | 5–15 | 1–6 | start/终端、secret、destroy、provider、授权与会话退出风险最高。当前 24 个 `agent.*` 有 legacy 分派证据、无同名真实绑定（另有 lifecycle/session/dispatch/down）；`agent.start` 已声明但不覆盖全部 `lifecycle.start`，不能视作替代完成。preview operationId/凭据脱敏/EOF/Ctrl-C 逐项测。 |
| config | 1/2 | 1–2 | 1–2 | 1–2 | 1–3 | 1–6 | `config set` 的校验、持久化与离线语义；当前无同名 MethodSpec。需先决定 daemon operation/参数和明确的离线错误，名称待审。 |
| daemon | 4/5 | 4–5 | 1–3 | 0–2 | 3–7 | 1–6 | B03/B04 进程和服务 bootstrap，`ping/ps/shutdown` 仍无同名真实绑定；`daemon.status` 只覆盖 ping 三字段。KP-005 supervisor/exit proof，启动和停机不可纯 RPC 化。 |
| doctor | 2/12 | 2–12 | 1–3 | 0–2 | 2–6 | 1–6 | B06 离线只读/在线检查来源与退出码；已有 `routine.audit` legacy 分派但无同名真实绑定。不得给修复写操作泛白名单。 |
| fs | 1/2 | 1–2 | 0–2 | 0–2 | 2–6 | 1–6 | 文件、游标、取消、URL 权限；`orgfs.*` 已有大批声明，先核请求结果能否支撑文本渲染。旧 `orgfs.web` 仅提议，是否确需新方法待决。 |
| gui（范围外） | 0/2 | 0 | 0 | 0 | 0 | 0 | 只记录，不触碰 GUI 文件或入口。 |
| host | 0/2 | 0–2 | 0–1 | 0–1 | 1–2 | 0 | 当前无违规边；注册/占位与帮助保持，是否需迁移业务按实际命令回调定。 |
| init | 2/3 | 2–3 | 1–3 | 0–2 | 2–6 | 1–6 | B01 最小 home/首启 bootstrap；onboarding 在线阶段须用 request。先复用当前已绑定的相关能力；旧 `onboarding.plan/apply` 不是获准方法名，在线阶段若不能由现有方法表达则提新声明。 |
| link | 2/3 | 2–3 | 0–1 | 0–1 | 1–3 | 1–6 | 四个 link MethodSpec 已声明，A 样板已落；优先清剩余 import 边与输出等价，不重复造方法。 |
| log | 2/3 | 2–3 | 0–1 | 0–1 | 1–3 | 1–6 | `log.page` 已声明但只含 limit/cursor 与四个字段，不等价于 CLI 的 name/actor/conversation/time/correlation 筛选及完整记录；需扩现有读模型或新声明，owner 审定形状，不能直接替换。 |
| login | 2/6 | 2–6 | 1–4 | 0–2 | 3–8 | 1–6 | B02 OIDC、凭据、身份切换和停启；online org 操作仍 request。身份/取消/失败恢复高风险，后置。 |
| message | 6/19 | 6–19 | 2–6 | 1–4 | 4–12 | 1–6 | 当前 6 个 `message.*` 有 legacy 分派证据、无同名真实绑定；`message.send`/lookup 等已声明。read=fetched、reply/ack、丢响应未知结果、附件/观察；旧 trajectory 命名待决。 |
| meta | 1/2 | 1–2 | 0 | 0 | 1–2 | 1–6 | help 纯展示、version B08 只读安装事实；不虚构 IPC，现有输出/错误不变。 |
| org | 2/10 | 2–10 | 2–6 | 1–4 | 4–12 | 1–6 | org/orgfs 已声明大量方法；context/import、成员和 ACL 正在外线变化。KP-041 `org.join` 错误 data 失配须保留红灯。历史 context 候选名待决，等 owner 和相关线冻结。 |
| profile | 1/2 | 1–2 | 0–2 | 0–1 | 2–4 | 1–6 | B07 四个离线 home/profile 路径；use 不造第二持久 selector。无同名 RPC 是白名单评审题，不默认补方法。 |
| routine | 1/2 | 1–2 | 1–3 | 1–3 | 2–6 | 1–6 | 当前 `routine.list/status` 已声明、另 6 个 routine legacy 无同名真实绑定；YAML plan/templates 是否新增方法需核当前业务与离线行为。 |
| service | 0/4 | 0–4 | 0–2 | 1–3 | 2–5 | 1–6 | 无违规 import 边，但当前 `service.*` 4 个与 `network.*` 4 个 legacy 无同名真实绑定。网络暴露/撤销和错误码需验；不能以 E=0 判完成。 |
| socialware | 1/2 | 1–2 | 0 | 0 | 1–2 | 1–6 | 保留 COMING_SOON 与失败退出，不造假 handler。 |
| squire | 2/3 | 2–3 | 1–3 | 1–3 | 2–6 | 1–6 | 依赖 agent/adapter/identity；`management.squire.ensure` 当前无同名真实绑定，`management.squire.prepare` 虽有 SPEC 源但未绑定，不能算可用。聚合 operation 的字段/幂等待决。 |
| transfer | 1/2 | 1–2 | 0–2 | 0–1 | 1–4 | 1–6 | 当前仅 `runtime.py` 与 `__init__.py`，旧表 19 入口已失效。downgrade-state 操作 durable runtime intent，需单独停机/锁边界审查；当前迁移职责按现有入口核实，旧 transfer.* 候选不复活。 |
| upgrade | 2/3 | 2–3 | 1–4 | 0–2 | 3–8 | 1–6 | B05 安装替换/退出证明/失败恢复；`autoupdate.trigger/notify` 当前无同名真实绑定。`upgrade.steps.*` 已声明。KP-039 旧进程显式 forwarding target 仍未验，后置。 |
| user | 1/4 | 1–4 | 0–2 | 1–2 | 2–5 | 1–6 | identity 可信 caller、绑定发布/撤回；当前 `identity.binding.publish/withdraw/whoami` 无同名真实绑定。保留 handler 参数 admission 与错误先后顺序。 |
| work | 0/2 | 0–2 | 0–2 | 0–2 | 1–4 | 1–6 | 无违规边；旧 `work.collect/finish` 候选须先核当前命令是否仍可达及是否已有权威替代，不因历史表新增。 |
| workflow | 7/10 | 7–10 | 2–6 | 2–5 | 4–12 | 1–6 | 当前 13 个 `workflow.*` legacy 无同名真实绑定，另有 pac.*；plan/控制/events/overview/cursor/worker 业务须在 daemon。旧新方法名均待当前 CLI 用例和 MethodSpec 评审。 |
| **公共 common/output/runtime** | **9/—** | **9（单独串行 owner）** | 0–2 | 0–1 | 2–5 | 1–6 | `CliServices` 54 字段逐组消退，`runtime.request/execute` 与唯一 output 保持；不可与各组在共享文件并行合入。 |

`E=0` 仅表示 import baseline 无该家族路径，仍须审业务调用与旧方法。`E>0` 不等于全是业务：完整分类为 a=173、b=192、c=52；b 中服务于本地业务的异常/类型随 a 迁移删除，不能批量登记例外。公共文件按真实消费者逐组移除依赖，避免与命令组重复计算。

## 输出与新声明决策

默认**用户可见输出、JSON 字段、退出码、已发布错误码及错误顺序不变**。仅设计已明确的离线行为变化（如 `config set`、routine plan）须先由 owner 决定具体新结果/错误与 Contract-Impact，不能靠迁移顺手改。`--json` stdout 恰好一个带 `ok` 的对象，`ok:false` 退出 1；进度/警告/交互走 stderr。固定 clock/ID 后逐字节比较 text/JSON stdout、stderr、退出码。冻结的小写 OrgFS 错误码原样保留，`org.join`/`org.invites.accept` 的 `data` 应按现有声明校验，不放宽 schema 遮盖 KP-041。

当前 router ledger 175 个显式方法中 87 已同名声明，**88 个有 legacy 分派证据、无同名真实绑定**；另有 7 个前缀检查/分派与授权入口，逐项见 methods.md。88 是跨家族复核池，不能按表中一组多次求和；每项先证明仍有消费者及不可由现有方法等价覆盖，再决定补 MethodSpec/真实 handler，或带证据删除、合并。需要保留者逐项审参数/结果/错误/可信上下文/写入与逻辑流式。`agent.start`、`top.snapshot` 在旧附录的“新方法候选”目前已声明，但各自仅覆盖部分原行为。`log.page`、大量 org/orgfs、link 四项已声明。历史新名并非最终需求；**新增方法只在现有方法不能表达当前 CLI 本地业务且 owner 审定形状后建立**，例如 config set、adapter 路由/授权、workflow events 等。已存在但未绑定的 SPEC 不能当可用。

`MethodSpec.ipc_param_validation=handler` 的 adapter.pin/pins、identity.users.add/override.set/override.clear、agent.secret.provider-write/grant、message.send/send.key_lookup、targets、progress.list 保留旧 handler 的未知字段、转换和错误次序（没有批准就不改变）；HTTP-only `paramsConstraints` 不反向收紧 IPC。`message.send` 写响应丢失保持 `dispatch=unknown`，用 key lookup 恢复，绝不通用自动重发。

## 分批与并行门禁

1. W0 design-only：完成 417 条边的人工分类、当前注册树与 138 个真实 MethodSpec/88 项无同名绑定的 legacy/7 前缀的双向矩阵，先定 b 类类型/常量端口及精确 bootstrap 清单，列旧 CLI 输出字节样本。owner 确认 WIP 设计后才实施。
2. W1 最薄切片：link 协议余边、socialware 占位错误词汇；meta/version 等 owner 本地例外裁决；log 保留筛选缺口移至 W2，另以 service 的 4 个 legacy 方法作为“无 import 边但仍须声明”样板。host 在真实回调核定后决定是否有产品修改。每组独立 PR。
3. W2 普通领域：user/identity、fs/orgfs、message、workflow、routine、config、log；fs→work、workflow→routine 的业务依赖先满足。写请求的重复/未知结果和 cursor 另建明确证据。
4. W3 复合领域：adapter、agent、org，随后 squire。org 等相关组织线落定，agent 先有 preview/终端/会话契约；transfer runtime 与其他迁移入口分别审，不扩大历史任务归属。
5. W4 高风险生命周期后置：daemon/doctor/profile/init/login/upgrade；daemon→init/upgrade，user+profile+daemon→login。B01–B09 只按“命令路径+公开能力+运行条件”允许，不整组豁免。KP-005、KP-039 等真实场景未验保持 BLOCKED。
6. W5 收口：内部非 CLI 方法声明/handler 补齐，`CliServices`/入口消费者退场，import ledger 只减至零非白名单，最终 intg head 和安装产物验证。当前不安排会修改 `packaging/`、`clients/` 或 GUI 的物理目录/入口搬迁；若确需，另行范围裁决。

独立组 PR 可并行准备，astra 根据实际文件和接口交集分配并发；允许的并发数随无冲突切片数量决定；若触同一 handler/catalog/contract snapshot、`runtime/registration.py`、`runtime/services.py`、`commands/common/services.py`、output 或 import ledger，必须串行合入并重放后合者焦点门。每组 PR 必有真实 daemon handler/MethodSpec/薄 CLI、同批消费者和契约更新；不允许 placeholder、兼容 shim、历史整组豁免、baseline 扩容、盲写重试。gate：结构≤7文件/目录且≤1000行/文件、模块方向、import 精确棘轮、registry 可达=声明=handler、OpenAPI 可重复生成、契约 diff、CLI 输出纪律、负控。行为变更先失败再通过；隔离 home/socket/cache 的焦点→`src/hyprial/` 改动时 `uv run --with libcst==1.9.0 pytest tests/` + `uv run ruff check .`→适用 `./contract/run-all.sh --isolated-only`→临时隔离目录的安装 wheel/daemon/harness 真实场景（不修改 packaging/clients）；FAIL/BLOCKED 不记 PASS，也不为 KP 存量放宽门。

ΔL 包含适用的两台账、精确 exception manifest、架构 baseline 与相应 reviewed digest 源文件，实际仅改需要的项；A03 immutable seed 永不随缩账重写。纯文档本 PR 的产品 diff 为零。

## 每组已发布输出/退出码/错误码的变更判定

本表是实施约束，不是已测试的兼容结论。全部行为测试当前 UNKNOWN；b词汇等价迁移用既有测试证明，其余按每个小PR提供实测。标“待owner”的潜在改变禁止在等价迁移中直接实施。

| 组 | JSON/已发布文本 | 退出码 | 错误码与数据 |
| --- | --- | --- | --- |
| adapter | 不变：配置/授权/媒体结果形状和轮询最终对象 | 不变：拒绝/取消与成功退出 | 不变：原领域错误码和 data；直接异常转 request 映射需回归 |
| agent | 不变：启动/secret/provider/会话结果；新确认字段另批待决 | 不变：TTY、EOF、Ctrl-C、启动失败需逐场景固定 | 不变：授权/恢复/凭据拒绝；operationId语义未获批准 |
| config | 默认不变；daemon不可用时的离线写能力待owner裁决 | 默认不变；若取消离线写需先定失败退出 | 默认不变；不得自行新增DAEMON_UNAVAILABLE覆盖旧错误 |
| daemon | 不变：状态与服务结果；ping/ps字段不能被精简 | 不变：already-running/停止证明/supervisor退出 | 不变：启动锁、服务总线、恢复期拒绝 |
| doctor | 不变：检查项、事实来源及诊断聚合 | 不变：检查失败与daemon不可用不伪造成功 | 不变：在线transport拒绝与离线缺项须区分 |
| fs | 不变：OrgFS结果/游标/文本与流式边界 | 不变：权限、取消、watch终止 | 不变：冻结小写码与data；不得通用包装掩盖 |
| gui | 本批不变，范围外 | 本批不变，范围外 | 本批不变，范围外 |
| host | 不变：现有查询/占位返回；零import不代表已验收 | 不变：占位能力失败退出 | 不变：已有占位/参数错误 |
| init | 默认不变：首启和在线onboarding结果分开 | 不变：缺前提、取消和初始化失败 | 不变：bootstrap拒绝；阶段切换的新错误须owner先定 |
| link | 不变：现有list/stats/add/drop返回 | 不变：成功及拒绝退出 | 不变：精确协议词汇，使用既有输出契约 |
| log | 不变：完整记录、筛选、跟随输出；不能直接换精简log.page | 不变：无匹配/非法筛选/中断 | 不变：原参数及查询错误顺序 |
| login | 默认不变：登录/身份切换结果与取消 | 不变：取消、凭据失败和停启失败 | 不变：身份锁/凭据/网络错误，在线与bootstrap分别测 |
| message | 不变：send/lookup/read/ack及线程视图 | 不变：unknown不伪装失败后重发或成功 | 不变：fetched副作用、权限、超时及旧alias回退 |
| meta | 不变：help/version与安装事实 | 不变：版本展示及安装诊断失败 | 不变：安装信息错误，不新增虚构IPC错误 |
| org | 不变：成员/ACL/context和migration输出 | 不变：加入/授权/冲突拒绝 | 不变：org.join typed data与KP-041现状，另需真实验证 |
| profile | 不变：list/show/use/create的现有选择结果 | 不变：重复/非法home等失败 | 不变：不覆盖已有profile；本地保留条件待owner |
| routine | 默认不变：list/status/plan/templates；离线plan取舍待owner | 默认不变：计划拒绝与daemon不可用需先裁决 | 不变：YAML/domain/授权错误的先后及词汇 |
| service | 不变：connect/disconnect/guide/list及网络结果 | 不变：网络拒绝与解除失败 | 不变：旧handler的错误code/data；零import仍需声明测试 |
| socialware | 不变：现有占位错误对象 | 不变：COMING_SOON非零退出 | 不变：保留COMING_SOON，不能伪造成功 |
| squire | 不变：跨agent/adapter聚合结果 | 不变：offline lease/设置失败 | 不变：授权、冲突和部分失败；去掉离线fallback需先裁决 |
| transfer | 不变：当前runtime probe/downgrade结果 | 不变：探测失败与停机不满足 | 不变：durable intent与锁拒绝，不复活历史命令 |
| upgrade | 不变：安装、步骤、状态及重启结果 | 不变：失败恢复/旧进程退出证明 | 不变：安装/服务/auto-update错误，保留恢复提示 |
| user | 不变：identity/绑定查询和写结果 | 不变：trusted caller/权限拒绝 | 不变：handler admission和错误顺序 |
| work | 不变：现有可达入口结果，先核注册/消费者 | 不变：零边不代表无需退出码回归 | 不变：既有契约；旧候选方法名不自动新增 |
| workflow | 不变：PAC plan/status/events/overview/cursor | 不变：授权/任务失败、观察取消 | 不变：owner/caller与远端history错误，不能CLI读库代替 |
| common/output/runtime | 不变：唯一CliResult/CliStream输出与envelope映射 | 不变：ok:false退出1；传输/进程退出分别测 | 不变：RequestPortError/IPC词汇；共享端口逐消费者验收 |
