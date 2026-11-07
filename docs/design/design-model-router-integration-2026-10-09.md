# 将模型路由接入 Hyprial agent 启动

日期：2026-10-09。状态：**提案，待评审；本文的 `--router`、profile 和新 IPC 字段尚未实现。**
本 PR 交现状核查、接口方案与实施/验收拆分，不改变已发布命令或运行中的 agent。

## 1. 目标、依据与文档位置

用户希望保留 `hyprial start <harness> --name ...`，通过一个启动参数和一份配置启用
router，不再分别启动 llmrouter、准备两套配置、再由其 wrapper 启动客户端。
选择：**Hyprial 管 agent 生命周期和接线，llmrouter 管模型请求与选择；Jev 等决策
执行者复用 Hyprial harness，但先补齐有截止时间且不重试的调用合同。**

核查基线：

- 本机 `hyprial version --json` 为 0.5.4；目标仓 `dev` 为
  `53de6496adaa9000cbdf179cd97fcb71645aa3fa`。旧 checkout 落后，核查和提交均在从
  最新 `origin/dev` 建立的独立 worktree 进行。
- llmrouter 已评审 M2 基线为
  [`b48a86c4f764946402368fbbb32ba2f0a0d5b3f6`](https://code.hyprial.com/devusr/llmrouter/src/commit/b48a86c4f764946402368fbbb32ba2f0a0d5b3f6)。
  下文“已有”以代码为准，“设计要求”不冒充实现。
- 已阅读 [AGENTS](../../AGENTS.md)、[贡献约定](../../CONTRIBUTING.md)、
  [架构入口](design.md)、[深模块方案](refactor-plan-deep-modules-2026-10-01.md)、
  [agent home](design-agent-home-dirs.md)、[provider 接线](../reference/harness-model-providers.md)、
  [turn hooks](../reference/turn-hooks.md)、[CI 路由](../reference/ci-integration-routing.md)。

这是跨启动/运行时的未来设计，归 `docs/design/`；现有 provider 参考仅增加提案入口。
实施通过后，使用方法进 `docs/guides/`，发布字段进 `docs/reference/` 与 `contract/`，
实测进 `docs/evidence/`。不新增根目录设计稿，不改 `docs/notes/` 历史材料。
llmrouter 的 `docs/concetp/` 是该应用的设计目录，不能成为 Hyprial 新公共接口的唯一规格。

## 2. 当前能力核查

代码路径相对本仓，均按上述 Hyprial 基线核对；不外推到未发布分支。

| 能力 | 事实与代码入口 | 对本需求的结论 |
|---|---|---|
| 启动分档 | [`start`](../../src/hyprial/shell/impl/cli/commands/agent/runtime/start.py) 的 `--tier fast/strong/super` 调用 `dispatch.matrix.resolve`；[`matrix`](../../src/hyprial/daemon/impl/dispatch/matrix.py) 取静态池首项，不探测、不故障换候选 | 启动时选择一次 harness/provider/model，不是逐模型请求路由；不能改其已裁定语义 |
| 显式选型 | [`model_provider.py`](../../src/hyprial/daemon/impl/harnesses/model_provider.py) 翻译 Claude 环境、Codex Responses 配置、Pi 参数、DSH 会话选择 | 接线可复用；provider 闭集与 Codex/DeepSeek 限制不能用假冒 DeepSeek 绕过 |
| 端点覆盖 | 同文件接受 `HYPRIAL_OPENAI_BASE_URL` / `HYPRIAL_ANTHROPIC_BASE_URL`，用于相应 provider 翻译路径 | 不是完整 router 功能，也不是所有原生 provider 的通用开关；没有 profile、资源/绑定/就绪合同 |
| 配置/凭据/会话 | [`HarnessLaunchSpec`](../../src/hyprial/kernel/impl/facts/launch_spec.py) 保存非秘密启动意图；[`SecretResolver`](../../src/hyprial/identity/impl/agents/runtime/secrets.py) 按实体与 grant 交付；agent home 保留原生会话 | 直接复用，不能再建 llmrouter 的第二套客户端 home |
| Jev harness | `hyprial start jev --name ...` 强制 headless，不接受启动级 model/provider/tier，模型由每次 JSON 请求决定 | **已经存在**；有独立 TypeSafe 凭据与推理成本 |
| Jev worker | [`python worker`](../../src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py) 使用 SDK 0.7.0、pool=10、state/questions/model 输入与 answers/usage 输出 | 可做决策后端，但缺 router 所需的单次调用/总截止约束，见 §4 |
| hooks/PAC | [hooks](../reference/turn-hooks.md) 观察 delivery/turn；[missions](design-missions.md) 已记录 Jev 无工具，不能自行 complete PAC 节点 | turn 不等于内部每次模型请求；不能靠 turn hook 声称逐请求拦截，不能把 Jev 当普通 PAC 节点 owner |
| llmrouter | [config](https://code.hyprial.com/devusr/llmrouter/src/commit/b48a86c4f764946402368fbbb32ba2f0a0d5b3f6/src/llmrouter/config.py) 实际仅接受 rules/jev；[client](https://code.hyprial.com/devusr/llmrouter/src/commit/b48a86c4f764946402368fbbb32ba2f0a0d5b3f6/src/llmrouter/client.py) 是隔离新会话 wrapper，不支持 resume/session_bound | 设计里的 `hyprial_agent` 尚未实现，不能只改一个配置值就宣称已接好 |

所以现版本能静态选型，也能调用 Jev，**没有一体化 `start --router` 功能**。
`service connect` 与网络路由处理服务可达性，不是业务模型选择器。

## 3. 用户入口与唯一配置来源（拟议）

```sh
# 提案命令：当前版本尚不能执行
hyprial start claude --name coder --cwd /workspace/project --router coding
hyprial start codex --name reviewer --headless --router coding
hyprial start claude --name coder --router coding --resume <existing-session-id>
```

没有路由 opt-in 时保留旧行为，不读 profile、不起 router、不增加分类开销。第一版规则：

- `--router` 要求显式 harness，与 `--tier`、`--provider`、`--model` 互斥；不改变 tier。
- `--` 后以及 agent/project 配置中的 model/provider/base_url/认证覆盖也须检测冲突，
  在起子进程前拒绝，不能悄悄覆盖或绕开 router。正常项目 skills/hooks 仍保留。
- Claude/Codex 的 interactive/headless **逐格**验证后开放；Pi/DSH、container/smolvm、
  跨机 transfer 未验组合拒绝，不类推为支持。
- 新 CLI 先协商拟议 `model-routing-v1` 能力，旧 daemon 缺失就拒绝且零写入。
  当前 CLI 允许 extra args，必须防止 `--router` 被旧路径泄漏到原生 CLI。
- 继续用既有 `hyprial stop --name ...`，按资源属主/引用释放；日常不再要求用户跑
  `llmrouter serve/run`，fleet 也不成为产品配置的事实源。

### 3.1 profile

profile 存在 agent 已授权 `configDir` 的 `model-routing.json`，沿用单路径规则。
常用位置是 `H/agents/N/config/model-routing.json`；`--router coding` 只选名字，不能
读任意绝对路径、执行脚本或取远端配置。可显式复制无秘密模板，但凭据和状态按实体分开。

优先级：`--router <name>` > 配置 `defaultProfile` > 关闭；拟议 `--no-router` 显式覆盖
默认值，两旗标同时给则拒绝。默认 profile 生效时又给 provider/model/tier，报冲突并
提示显式 `--no-router`，不能暗中切走。所有新字段须 closed-schema 验证。

```json
{
  "schemaVersion": 1,
  "defaultProfile": null,
  "profiles": {
    "coding": {
      "protocols": ["responses", "messages"],
      "gateway": {
        "endpoint": "https://api.example.invalid",
        "credentialGrant": "model-gateway"
      },
      "candidates": [
        {"id": "economy", "model": "MODEL_ID_A", "contextWindow": 64000},
        {"id": "reasoning", "model": "MODEL_ID_B", "contextWindow": 64000}
      ],
      "rules": [
        {"id": "routine", "prefer": ["economy"]},
        {"id": "complex", "prefer": ["reasoning"]}
      ],
      "executor": {
        "kind": "harness",
        "actorRef": "agent:owner:node:router-decider",
        "resultContract": "router-choice-v1",
        "deadlineMs": 3000,
        "maxAttempts": 1,
        "input": "metadata_only"
      },
      "continuity": "session_pinned",
      "onDecisionFailure": "reject",
      "recovery": {"autoRestore": false}
    }
  }
}
```

这是待实现 schema 的说明样本，**不是可加载的 llmrouter v1 配置**。模型名/地址/窗口
都是占位，64k 不代表任何真实模型保证；实际还需分类说明和候选能力证据/版本，证据不全
不开放 auto。第一版每 profile 仅接一个获授权 API 网关身份；订阅登录与 API key 是
不同权限/计费面，不能把“Hyprial 能用某 harness”推导成“网关能调用其模型”。

`executor.kind=rules` 时本机选择，不需要 Jev key；`kind=harness` 必须引用有授权、且
支持 §4 合同的执行者。首批借用已就绪执行者，可先用现有命令
`hyprial start jev --name router-decider` 启动；不会隐式创建/停止别人 agent。
未来模板自动 provisioning 须显式声明资源归属和调用授权。独立 llmrouter 的直连 Jev
仍可作为另一显式模式，但不能在 harness 失败后偷偷追加一次直连分类。

### 3.2 持久化与用户可见状态

拟议 `HarnessLaunchSpec.modelRouting = {profile, revision}` 保存选择与不可变配置戳。
历史 `provider` 仍代表 harness，`modelProvider` 仍代表模型供应商，不塞入 router 名。
配置是策略来源，receipt 是解析结果，不得形成两份可独立编辑的权威。自动恢复必须
读取同一 revision；配置更新需显式确认，不能按同名 profile 重新选模型。
旧 daemon 不能忽略新字段后按原生默认模型启动。

revision 用版本化 canonical JSON 的语义快照哈希：包含候选/规则/能力证据、网关地址、
grant 引用及修订、执行者与调用策略；不包含秘密值、临时目录/端口/PID。运行实例身份
另记在 receipt，不用跨运行变化的进程指纹冒充策略版本。存储完整无秘密快照供核对；
grant 撤销仍由权限检查立即生效，旧快照不能冻结授权。

`ps/status --json` 拟议增加 `modelRouting` 投影：profile/revision、phase、
resolvedCandidate/resolvedModel、bindingVersion、executorActor、routerGeneration、lastError。
尚未选择时 resolvedModel=null，不能把 `router/auto` 当真实模型；sessionId/lastSessionId
保留原义。输出不含 key、完整 prompt、秘密路径。实施时同步 schema、CLI golden、renderer
与 contract；本文不提前修改当前 reference 的字段承诺。

## 4. Jev 和其他决策是否能走自带 harness

**执行后端可以复用，当前普通消息调用还不符合 router 的全部运行约束。**

### 4.1 输入输出适配

worker 的 `parse_payload` 只接受 state/questions/model。根据
[TypeSafe API](https://docs.typesafe.ai/api) 和 [Choice](https://docs.typesafe.ai/primitives/choice)，
适配器生成闭集选择，`answers.route.choice` 映射到规则 ID，再由 router 在允许集合里
选模型；答案不是 shell 命令、自由模型名或授权凭据。

```json
{
  "model": "jev-latest",
  "state": {"protocol": "responses", "has_tools": true, "estimated_tokens": 3000},
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "按给出的请求特征选一个任务类别。",
      "criteria": {"routine": "普通独立请求", "complex": "包含工具或复杂输入"}
    }
  }
}
```

上例只说明 worker 格式。实际保持 llmrouter `JevRequestFeatures` 的白名单：
protocol/requested_model/has_tools/tool_count/has_images/estimated_tokens/stream。
不附对话、工具正文、仓库内容或网关 key；元数据分类不承诺理解任务正文。要增加语义
输入，另审信息范围/授权/质量，不能沿用旧测试声称等价。work_id、配置戳、截止时间放
受控 envelope，不硬塞到 Jev payload 顶层（当前会拒绝未知字段）。

worker 返回 model/answers/usage，usage 使用 `inputTokens/outputTokens`，与 HTTP API
的 snake_case 不同，需薄适配器。结果校验包含预期 sender、correlation、唯一回复、未
超时、原 revision、有限数值 confidence、合法 choice；迟到/重复/非法答案不能改变绑定。

### 4.2 必补差额

1. **三层重试**。worker 创建 `TypeSafeClient(api_key=..., model=...)`，未设置 retry；
   本次读取锁定 SDK 0.7.0，`RetryPolicy` 默认 max_retries=2、retry budget=30s。
   [`turn_runtime`](../../src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py)
   也有 delivery 重试。ONLINE_ONLY 只表示在线投递，不保证模型只调用一次。
   新请求合同须同时令 SDK max_retries=0、该次 delivery 不重试、router 不重复分类；
   **按单次请求禁用 delivery 重试的公共合同是 P3 新增接口缺口**，不是当前普通消息的
   保证；底层已有重试控制分支也不等于已开放此调用面。不全局关闭其他 agent 的可靠
   投递。SDK 零重试配置见[官方说明](https://docs.typesafe.ai/sdk/python/api/retries)。
2. **总截止与容量**。预算包含准入、等待、worker/SDK 网络、结果解析；健康检查不推理。
   Hyprial 的10槽不等于 llmrouter 的8槽/2×deadline看门狗。本集成先限制8个决策准入，
   满即拒绝、不排队；worker 需受控 socket 超时、响应上限、禁止带凭据重定向。caller
   停止等待不是底层请求已终止，须实测残留线程/socket总数与存活时间。
3. **重启与过期**。持久 request ID/状态，先记开始再调用；崩溃时 started 按 unknown
   拒绝，不自动再次计费。重送同 ID 只返回已有结果或明确状态。到期结束等待并尽力取消
   网络，不能保证供应商撤销计费；不能杀共享 worker 来取消一条调用。版本化扩展控制
   envelope，不改普通 Jev payload，更不能只以传输层去重冒充远端调用幂等。
4. **公共调用端口**。现有
   [online-only exchange](../../src/hyprial/daemon/impl/harnesses/turn_delivery/service.py)
   有 correlation/迟到清理先例，但属于内部且 cleanup 是 best-effort；应用不可 import
   此 impl 或伪装 `turn-hook:*`。新增受授权的有界 harness 请求公共面，由 daemon 适配
   原消息/worker，明确过期和消费权，不建立第二份 durable inbox。到期清理未完成时须
   阻止重新执行，不能把 best-effort ACK 当支付副作用的保证。
5. **授权与成本**。TypeSafe key 只交给 Jev agent 的 grant，网关 key 留在调用方 router。
   URI/同用户/同机不自动取得调用权；复用 harness 不会拿 Claude/Codex 额度代付 TypeSafe。
6. **防循环**。执行者不得是调用方自己，也不得依赖本次路由选择自己的模型；对多个
   profile/执行者做环检查。首批只允许本机已就绪执行者，远端另验权限与迟到预算。

Claude/Codex/Pi 可承载实现 `router-choice-v1` 的专用决策 agent，但自由文本不天然符合
协议，排队/工具调用也没有3秒保证。首批只开 rules 与补齐合同的 Jev；其他后端未验就
拒绝，不自动换后端或宽松猜答案。[TypeSafe 的意图路由模式](https://docs.typesafe.ai/patterns/intent-routing)
可用于后续有授权正文输入的方案，不作为本期 metadata_only 分类质量的证据。

## 5. 生命周期、会话与故障

### 5.1 只有一个 agent 生命周期属主

```text
hyprial start <harness> --router coding
  → shell 校验 + daemon 能力协商
  → identity 解析 agent home / profile / grants
  → daemon 启动事务准备 router 与客户端侧恢复控制进程、准入和绑定 receipt
  → 原有 harness connector 启动原生客户端（保留配置、项目 cwd、原生 session）
       ├─ 模型请求 → 本机受管 llmrouter → 已授权 API 网关
       └─ Hyprial 消息/工具 → 原有 daemon inbox/channel

llmrouter 选型 → 有界 harness 请求公共端口 → 原生 Jev harness → TypeSafe
```

不嵌套 `llmrouter run` supervisor，不复制其空 config/MCP 策略。agent-home 已裁定项目
配置照常读取，不能为了 router 屏蔽项目 skills/hooks。模型转发在独立 router 进程，不
阻塞 daemon tick，监听仅回环。第一版**每 agent 一个 router 实例**：复用 profile 不
等于共享密钥；不把多身份送进当前单 key RouterApp。共享池后续另验授权分区和公平性。

daemon 所属生命周期组件持有附属进程 PID/启动身份/receipt，复用原子启动与 rollback；
模型业务规则仍在 llmrouter。失败只回滚本次临时资源，不清历史、不停借用 Jev/其他 agent。
ready 必须验证 router identity/profile revision，不能只测端口；原生 PTY/信号/退出码
仍由既有 connector 承载。router 制品必须固定版本、验证兼容协议，不能依赖开发者工作
目录或启动时拉 mutable master。llmrouter 尚无正式制品，打包/版本握手是实施前置。

### 5.2 会话连续性

默认拟议 `session_pinned`：新原生会话首次模型请求选择一次，持久固定到具体候选；
工具后续、压缩、内置子请求不得悄悄换模型。原生 harness 继续保存对话。
`independent_requests_only` 只给明确的一次性请求，不能拿 M2 wrapper 默认值承诺长会话。
不能可靠区分主会话/分支/子 agent 时，限制该组合或只允许显式固定候选。

首次并发选型须 CAS：用 launch generation 建 provisional 绑定，拿到真实 native session
ID 后原子关联。绑定含 agent incarnation、session/branch、候选、上游模型、语义 profile
revision、单调 binding version；PID/端口不作永久身份。resume 验证同一绑定与最新授权，
缺失/落后/不兼容就拒绝，不重新选一个模型接着旧会话跑。

客户端侧副本由 Hyprial 的该 harness 接入组件独立于 router 保存，router 负责校验。
M2-06 的 version/CAS 可复用，但原生客户端不会逐请求回传动态版本头。因此需要 connector
注册受认证的 binding lease：静态客户端头只携带 lease 引用，router 解析到已验证版本；
不允许自报 sessionId 扩权，失效须重新验证。这是新增合同，须在 llmrouter 实施 PR
单列评审，**不是已经具备的逐请求控制组件**。

client_id 是进程准入，session_id 是对话，operation_id 是单次业务操作；不能混用。
禁止静态 operation_id；自动生成 HTTP ID 也识别不了原生重试，不能声称 exactly-once。
所有本机头转发前剥除。本机 client token 与网关 key 分离：子客户端拿本 agent 的本机
准入凭据，router 拿获 grant 的网关 key；认证、轮换、撤销与串用负例必须一起交付。

已有 agent 加/去 router 或换 profile 都是显式连接变更；只有绑定映射和兼容性证据成立
才允许 resume，否则保留历史并提示新会话。不能静默 fork/重建摘要后称完整续接，
也不能用 monitor 等生产会话做接线实验。

### 5.3 失败可见、不自动重放

保持 M2“自动探测 + 用户确认后切换”，**自动降级仍未实现**。内置 Hyprial 不会补齐
逐请求拦截能力。关栅栏、在途排空、未知结果、显式新会话切换仍须验；ready 不自动换
key/model，不重跑原 turn。

Claude 必须同时注入并验证 `CLAUDE_CODE_MAX_RETRIES=0` 和
`CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1`。Codex 原生重连，以及 Hyprial headless
SDK reconnect/turn redelivery，也不能导致未知提交再次执行；这是独立 wrapper 测试
没有覆盖的**集成准入阻塞项**，需要假网关计数和真实客户端证据。

不照搬 M2 wrapper 的“router 消失就 SIGTERM/SIGKILL 客户端”：本仓 HarnessLaunchSpec
明确 turn timeout 不杀 turn。路由故障先拒绝新模型请求、保留原生会话和工作区、走既有
turn failure 告警，并禁止相关 delivery 自动重跑；如何终结原生无界 reconnect，要逐
harness 验证受控 interrupt。若只能杀进程才能结束，该组合保持不支持，另审其代价，
不能偷偷扩张正常 stop 的语义。

daemon 重启使旧 router generation lease 失效，先恢复验证原绑定再开放准入，未决操作
不重送。探测预算先持久扣再发、跨重启不清零；router 退出和探测属主退出仍是两种失效。
用户消息 inbox 的可靠性与模型请求的不可重放性分开处理，unknown 明确展示，不能以
成功 ACK 掩盖；有限探测不发模型调用。

### 5.4 探测属主与预算的承载位置

选择保留 M2-07b 的探测/预算机制，由 **Hyprial 的该 harness 接入组件中的客户端侧
恢复控制器**承接。它作为 connector 所属的独立控制进程运行，独立于 router 和原生
客户端子进程存活；daemon 只负责其生命周期注册/启停，不在通用调度循环里探测或扣账。
复用 llmrouter 的 `RecoveryController` / `ProbeOwner` 状态机和持久化机制，适配为
无客户端启动职责的控制入口；不嵌套 `llmrouter run`，不把 router 进程当探测属主。
这属于 P2 新接线，当前 Hyprial 尚未提供此入口。

令 `A` 为已由 identity 授权的 agent home，`I` 为该 agent 受管 router 的稳定实例 ID。
实例 ID 持久登记在生命周期 receipt 中，普通 stop/start、进程重启及 profile 更新不
自动另分配；PID、临时端口、launch generation 或 profile revision 不作为预算目录名。
拟议的唯一落点及生成配置关系如下（生成配置使用经过验证的绝对路径）：

| 对象 | 落点 / 对应 llmrouter 字段 | 读写者 |
|---|---|---|
| 客户端控制状态目录 | `A/state/model-routing/I/client/` → `resources.client_connection.state_path` | 本实例获授权接入组件；router 只访问其所需的准入栅栏 |
| 实例探测锁 | 同目录 `probe-owner.lock` → `resources.client_connection.lock_path`；`max_active_probe_owners=1` | 控制进程竞争 OS `flock`，记录实例 ID、PID、启动时间 |
| 故障预算账本 | 同目录 `recovery.json`，由 `RecoveryController` 从 `state_path` 派生 | 仅持锁属主写；其余组件只读，不由 router 或 daemon 代写 |
| 准入栅栏 | 同目录 `clients.sqlite3` | router 与接入组件按准入协议读写；它不是探测预算账本 |

目录 0700、文件 0600，并校验属主和符号链接；不放在配置 manifest、临时运行目录或
router 可随停启清理的目录。探测前先持久扣预算，再发零模型调用的就绪请求；提交失败
不发探测。读回失败、损坏、配置指纹不匹配均保留原文件并告警，不能按空账本重建。
既有 `recovery.json.config_version` 与生成实例配置的绑定须在 P2 映射/迁移测试中明确，
不能直接用 §3.2 的语义 revision 替换并声称兼容；更新不以换目录或清文件重置预算。

同一实例只有持锁组件能探测与写预算。未获锁的组件验证属主记录后作为只读跟随者，
不因正常竞争失败退回 MANUAL；接管需先取得 OS 锁并确认旧 PID/启动身份已退出或明确
释放，随后读回同一账本、故障代次和已扣次数。身份不确定则告警并拒绝接管。

- router 退出：控制进程仍在，按原预算继续探测；不重放未知业务请求，不自动切换。
- 控制进程退出：router 不能代替它探测；其余获授权接入组件或生命周期管理器重建的
  控制进程按上述规则接管。第一版每 agent 一个实例，通常由重建的控制进程接任。
- daemon 重启：即使控制进程一并退出，也先恢复同一实例/账本、验证旧属主后才能续探；
  不承诺组件全部停止期间仍有探测。无法确认属主或读回状态时拒绝受管准入并告警。
- 显式 stop：取消未发探测、释放锁、保留账本；再次 start 不补预算。仅用户明确重试
  恢复探测才可记名创建新预算代次。ready 只通知用户，仍由用户确认安全的新会话切换。

## 6. 分期交付与模块边界

| 阶段 | 交付 | 退出条件 |
|---|---|---|
| P0 接入证明 | Claude/Codex × interactive/headless 实际版本，项目配置、子请求、首次绑定、恢复、原生/daemon 两层不重放支持表 | 至少一个组合能保留会话、故障不重复调用；失败格不放行 |
| P1 配置与启动意图 | kernel 非秘密值类型；identity 配置/grants；shell 参数/冲突/输出；daemon 公共能力/状态；profile 到实例配置的规范映射 | 默认关闭旧行为不变，新旧混用具名拒绝，预检零副作用；生成配置经 llmrouter 加载器校验 |
| P2 受管 router 与接线 | llmrouter 固定制品/握手、每 agent 实例、凭据/状态、binding lease，既有 connector 叠加接线；§5.4 独立恢复控制进程、预算路径与接管 | P0 支持格的启停/恢复留证，消息/MCP/项目配置/权限不丢；router/属主/daemon 分别退出不重置预算 |
| P3 有界 harness 决策 | 新增单次请求禁止 delivery 重试的公共合同、worker 控制 envelope、SDK 零重试/总截止、幂等过期；llmrouter `hyprial_agent` 适配器 | fake TypeSafe 计数+并发/迟到/重启证明单次派发；凭据分离；真实分类读数 |
| P4 小范围发布 | reference/contract/operator guide/操作 skill；一名测试 agent opt-in | 新会话与原会话恢复各留证，可显式回退，不批量迁移 fleet |

P1/P2 可先用 rules 开发，正式开放 harness profile 必须等 P3。批准设计不表示验收已过。
PAC/routine 的 launch 将来可加同一引用，本期先做 start/agent 配置；不改 requires.tier
静态语义与现 schema 白名单。

三个实施前问题按阶段列为开工条件，设计评审通过不代表它们已解决：

1. P0/P2：补齐 binding lease 的创建、进程内换会话（Claude `/clear`、TUI 新建、Codex
   新会话）、resume、过期、撤销五类转换与各自失败动作，并固定可观测的会话边界；
   §5.2 的“不可靠则拒绝”不能代替状态机决定。
2. P1/P2：先给出 profile → llmrouter 实例配置规范映射表，覆盖 `protocols`、
   `candidates[].contextWindow`、`executor.deadlineMs`、`continuity`、
   `recovery.autoRestore` 到准确的 `resources.*` / `policy.*` 字段。当前不支持的值
   明确拒绝或与加载器扩展一起交付；用生成配置调用实际加载器验证，未知字段和悬空引用
   必须在加载期报错，不能只测两份 JSON 各自可解析。
3. P3：把单次 delivery 禁止重试贯穿公共入口、持久 envelope、恢复与 worker 调用，
   用故障注入计数证明不追加推理调用；不把现成 ONLINE_ONLY 当成完成此项的依据。

规则/候选/分类验证在 llmrouter；daemon 提供中立生命周期和请求机制，identity 负责实体
配置和秘密授权，shell 组装。跨域只走 interface，不从应用 import daemon impl，不新增
第六个 Python 顶层域，不复制 durable inbox。公开字段实施时同步 contract/Skill-Impact，
代码仍受7文件/1000行结构限制。

## 7. 验收与保留项

| ID | 必须证明 | 当前状态 |
|---|---|---|
| R01 | 无 opt-in 行为不变；冲突/未知 profile/旧 daemon 启动前拒绝 | 未实现 |
| R02 | 单命令启动，router ready 才起 harness；rollback 不碰历史和借用资源 | 未实现 |
| R03 | resume 同一 session、版本不落后；配置/候选/权限变化明确拒绝 | 未实现 |
| R04 | 项目 skills/hooks、MCP 消息、PTY/信号/退出码保持；逐模式验收 | 未实现；跨机首批不支持 |
| R05 | 两 agent 两 key 并发不串用；配置/log/Choice无秘密/正文；撤权后拒绝 | 未实现 |
| R06 | Jev 满容量不排队；三层不重复提交；坏/伪造/重复/迟到回复拒绝 | 未实现 |
| R07 | router/daemon/Jev 退出、快速重启、drop/partial SSE/5xx 后网关至多一次；unknown不重跑turn | 未实现，阻断支持声明 |
| R08 | 窗口与能力取可达候选已验证下界，分支/子 agent/工具关联不静默换模 | 未实现 |
| R09 | 分类总墙钟、IPC、本机处理、上游连接/TTFB、并发、空间分列；先冻后测 | 集成路径未测 |
| R10 | §5.4 属主/路径与唯一写者；router/属主/daemon 分别退出后零模型探测、预算不重置、ready不切换、人工新会话切换不重放 | 集成未实现，M2仅作输入 |

M2 的225测试不能给上表盖 PASS。其
[性能读数](https://code.hyprial.com/devusr/llmrouter/src/commit/b48a86c4f764946402368fbbb32ba2f0a0d5b3f6/docs/plan/m2-09-performance-readout.md)
中4MiB相对吞吐未过、8并发p95未过、真实主动断流未验、真实TTFB秒级尾部未解释继续保留。
连接复用 `e138ebc2-548a-414f-9370-c904411b176b`、栅栏争用
`b00bc330-c810-436e-b1cf-50716be36b7d` 不被本设计消项。若以后注入真正逐请求 operation_id，
成功派发/墓碑永久保存的空间代价也须报告，不允许静态头冒充去重或为容量私删账本。
自动降级要另审客户端逐请求接入点，本提案不借“内置”宣布完成。

## 8. 本 PR 验证范围

现版本/help 已核；Jev payload、SDK retry 默认值、tier resolver、provider 闭集及缺少
hyprial_agent 实现均来自固定源码/SDK。隔离无凭据测试：

```sh
uv run pytest -q tests/daemon/dispatch/matrix/test_dispatch_matrix.py \
  tests/daemon/harnesses/python/test_python_worker.py \
  tests/daemon/harnesses/contracts/test_harness_model_provider.py
# 48 passed；两条现有 lark_oapi deprecation warnings。
```

这里只验证被复用的既有能力；未来路由支持矩阵/性能全是未验。没有启动生产 router，
没有收费模型调用，没有改用户 fleet/凭据/session/daemon。文档链接、静态检查及 PR CI
结果见 PR 正文；纯文档检查不能代替将来的功能验收。
