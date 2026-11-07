# Harness 能力抽象设计(实施版,今晚交付)

- 日期:2026-08-12
- 作者:设计者(worker,branch `design/harness-capability-abstraction` off `dev`)
- 状态:**待 hq-adjutant 评审**,通过后今晚实施 + e2e + 发版 v0.4.0.dev1
- 今晚交付:**(1) provider→harness 术语重命名(含向后兼容)** + **(2) 能力抽象 MVP(枚举+声明表+单一真相源)**;channel/rpc/app-server 三机制归一只给目标形态与后续阶段
- 硬约束:协调者跑在 cc channel 上(pid 64447 / session-ref 135609b5),**`_start_interactive_claude` 的可观察行为今晚零变化**;生产 desired-state 现有 9 个 provider 条目,升级后必须原样加载

---

## 0. 术语与重命名总决策

| 术语 | 定义 |
|---|---|
| **harness** | agent 附着的运行时:cc / pi / codex |
| **provider** | 模型服务商:anthropic / openai / deepseek / kimi |
| **adapter** | 外部平台网关:lark |

**重命名分三个面,今晚只动第一个面:**

| 面 | 今晚 | 后续 |
|---|---|---|
| ① Python 符号(包名/类名/字段名/变量名) | **全量改,不留别名**(全仓消费方同库,测试可验证) | — |
| ② on-disk JSON 键 + daemon IPC 键 + CLI `--json` 输出键 | **不改写、只加读取别名**(dual-read) | schema v2 统一切换(§5) |
| ③ 值域 `"claude"`→`"cc"`、lark 剥离为 adapter | 不动 | 随 schema v2 |

② 不改的硬性理由:

- **生产回滚安全**:desired-state 写盘保持 `"provider"` 键 + `schemaVersion=1` 不变 ⇒ 新版写的文件旧二进制能读,旧版写的文件新版能读;生产 9 个条目零迁移、零人工编辑。
- **新旧进程混部**:升级 uv tool 不会重启在跑的 daemon,**新 CLI 会和旧 daemon 通 IPC**。`lifecycle.start` 的 `params["provider"]`(application.py:1121)、`down` 的 `params["provider"]`(cli.py:1214 → application.py:1147)等键今晚保持,新旧任意组合不出错。
- **外部消费者**:`hyprial start --json` 输出(cli.py:1139 的 `"provider": "claude"`)、squire setup 输出(squire/setup.py:374)、33 处测试断言锁定这些键。

⚠️ **绝不误伤:squire 的 `provider` 是正确术语**(模型服务商,squire/profile.py:76,109;probe.py 的 `harness:provider:model`)。重命名清单中的每一处都必须先判定语义是 harness 还是模型服务商,后者一律不动。

---

## 1. 今晚交付 1:重命名清单(可执行)

### 1.1 包目录改名

`src/hyprial/providers/` → `src/hyprial/harnesses/`,import 面(机械替换 `hyprial.providers` → `hyprial.harnesses`):

- src:daemon/api.py、daemon/application.py(6 处)、daemon/__init__.py、daemon/runtime.py、daemon/supervisor.py、migration.py、squire/setup.py、adapters/lark/runtime.py
- tests:test_providers.py(顺带改名 test_harnesses.py)、test_agent_sdk_driver.py、test_agent_send_idempotency.py、test_daemon_agent_sdk.py、test_daemon_desired_state.py、test_daemon_runtime.py、test_pi_rpc_process.py、test_worker_identity.py

### 1.2 符号改名表(harness 语义项)

| 现状 | 改为 | 位置 |
|---|---|---|
| `ProviderLaunchSpec`(含字段 `.provider`→`.harness`) | `HarnessLaunchSpec` | daemon/desired_state.py:23;消费方:launcher.py、{claude,pi,codex}.py、agent_sdk.py、pi_rpc.py、application.py、runtime.py、supervisor.py、migration.py |
| `_PROVIDERS` 值集 | `_HARNESSES` | desired_state.py:14(值不变:`{"claude","pi","codex","lark"}`) |
| `DesiredState.providers` / `upsert_provider` / `remove_provider` | `.harnesses` / `upsert_harness` / `remove_harness` | desired_state.py;消费方:application.py、migration.py |
| `ProviderLauncher`(协议 + 实现,两处同名沿用) | `HarnessLauncher` | daemon/api.py、providers→harnesses/launcher.py |
| `ManagedProviderProcess` / `StreamingProviderProcess` | `ManagedHarnessProcess` / `StreamingHarnessProcess` | daemon/api.py |
| `ProviderDelivery` / `ProviderResult` / `ProviderResultStatus` | `HarnessDelivery` / `HarnessResult` / `HarnessResultStatus` | daemon/api.py;消费方:streaming.py、runtime.py |
| `ProviderStartError` / `ProviderOperation` / `PtyProviderProcess` | `HarnessStartError` / `HarnessOperation` / `PtyHarnessProcess` | harnesses/common.py |
| `_STREAMING_PROVIDERS` / `is_streaming_spec` | `_STREAMING_HARNESSES` / 不变 | launcher.py:23,26(**P2 中被能力表取代,见 §2.3**) |
| `provider_selector` | `harness_selector` | application.py:362、runtime.py |
| cli.py 局部 `connector_kind` | `harness_kind` | cli.py:1149-1186(IPC 键名不变) |
| `StreamingTurnProcess(provider=...)` 构造参数 | `harness=...` | streaming.py、agent_sdk.py、pi_rpc.py |
| 各连接器内 `spec.provider != "claude"` 守卫 | `spec.harness != ...` | {claude,pi,codex}.py、agent_sdk.py、pi_rpc.py |
| 错误消息文案里的 "provider"(指 harness 处) | 改 "harness" | desired_state.py:39-41 等(纯文案,非键名) |

### 1.3 明确不改(今晚)

| 项 | 位置 | 理由 |
|---|---|---|
| desired-state 读写键 `"provider"` / `"providers"` | desired_state.py:37,75 | §0-②;dual-read 见 §1.4 |
| 回滚守卫记录 `"provider": "__hyprial_desired_state_schema_v1__"` | desired_state.py:408,429;migration.py:24 | 与旧二进制共用的哨兵格式 |
| daemon IPC 键(`lifecycle.start`/`down`/`ps` 输出) | application.py:1121,1147;cli.py:1176,1214 | 新旧 CLI↔daemon 混部 |
| CLI `--json` 输出键 | cli.py:1139;squire/setup.py:374 | 外部脚本/测试契约 |
| lark 相关 `"provider": "lark"` 写入 | adapter_registration.py:180、adapters/lark/adapter.py:126、adapters/lark/runtime.py:559 | 落进 desired-state 的 wire 键,随 ② 一起后续改 |
| channels.json 的 `"provider": "lark"` | persistent_config.py:91,130 | 用户磁盘配置,需独立迁移 |
| squire 全部 `provider` 字段 | squire/profile.py、probe.py、features.py | **正确术语(模型服务商),永不动** |
| 值域 `"claude"` / 环境变量 `HARNESS_CLAUDE_BIN` | desired_state.py:38;cli.py:1068 | 值改名是 wire 变更;env 名已正确 |

### 1.4 desired-state 兼容方案(dual-read,不升 schema 版本)

```text
from_json: raw = record.get("harness") or record.get("provider")   # harness 优先,provider 兜底
           (沿用既有 "cc"→"claude" 值别名,desired_state.py:38)
to_json:   仍写 "provider"(schemaVersion=1 不变)
```

- 生产 9 条目:旧格式,读取路径逐字节等价 ⇒ 零迁移。
- 新版写的文件旧 daemon 能读 ⇒ **可回滚**(这是今晚不升 v2 的唯一原因,也是充分原因)。
- 新增测试:仅 `"harness"` 键可读、仅 `"provider"` 键可读、两者并存 harness 优先、写盘仍是 `"provider"`。

---

## 2. 今晚交付 2:能力抽象 MVP

### 2.1 能力枚举(新文件 `src/hyprial/harnesses/capabilities.py`)

```python
class Capability(StrEnum):
    INTERACTIVE_ATTACH  = "interactive_attach"   # TUI 附着并向 daemon 注册(actor+sessionRef)
    HEADLESS_EXEC       = "headless_exec"        # 托管执行:投递→一轮→结构化终态
    WAKE_PUSH           = "wake_push"            # daemon 主动唤醒活着的会话
    PROACTIVE_SEND      = "proactive_send"       # 会话内 agent 主动出站(harness_send 等)
    SESSION_LIFECYCLE   = "session_lifecycle"    # 固定 session id / resume / fork
    TOOL_INJECTION      = "tool_injection"       # 启动时注入 hyprial 工具集并预授权
    TURN_CONTROL        = "turn_control"         # interrupt(steer/follow_up 为可选超集)
    EVENT_STREAM        = "event_stream"         # 结构化事件流(完成判定/可观测)
    MODEL_SELECT        = "model_select"         # 指定/切换 provider+model 与端点

class SupportLevel(StrEnum):
    NATIVE / DEGRADED / UNSUPPORTED     # UNKNOWN 今晚不引入:声明表只写已证实级别

@dataclass(frozen=True)
class CapabilitySupport:
    level: SupportLevel
    mechanism: str | None = None        # "channel" | "agent_sdk" | "rpc" | "pty" | None
    note: str | None = None             # degraded 时必须写明损失什么
```

### 2.2 声明表(今晚的诚实值;按 (harness, headless) 两行/harness)

| 能力 | cc 交互 | cc headless | pi headless | pi 交互 | codex headless | codex 交互 |
|---|---|---|---|---|---|---|
| INTERACTIVE_ATTACH | native(channel) | — | — | native(extension)【2026-08-19 解锁】 | — | native(app_server)【PR1】 |
| HEADLESS_EXEC | — | native(agent_sdk) | native(rpc) | — | degraded(pty;只跑启动任务,**不收 daemon 投递**) | — |
| WAKE_PUSH | native(channel) | native(agent_sdk 直投) | native(rpc 直投) | native(extension;轮询+followUp 注入) | unsupported | native(app_server;PR2 结构就绪，待真实回链验收) |
| PROACTIVE_SEND | native(MCP 工具) | native(WorkerChannel) | native(WorkerChannel → pi_harness_bridge.ts 扩展;pi 无 MCP,载体是 `-e` 扩展直说 daemon IPC) | native(extension;harness_* 工具集) | unsupported | unsupported |
| SESSION_LIFECYCLE | native | native | native | native(extension;launcher 拥有 --session-id) | degraded(连接器不管会话,codex.py:14-16) | degraded(app_server;PR2 完成 daemon restart recovery) |
| TOOL_INJECTION | native | native | native(同 C4 载体) | native(extension;-e pi_harness_attach.ts) | unsupported | unsupported |
| TURN_CONTROL | native(SDK interrupt) | native | native(abort) | — | unsupported | unsupported |
| EVENT_STREAM | — | native | native(agent_settled) | — | unsupported | — |
| MODEL_SELECT | native(HYPRIAL provider/model → Anthropic-compatible env) | native(同左) | native(--provider/--model;RPC set_model) | native(session.selectModel) | native(HYPRIAL provider/model → Responses config) | native(同左) |

("—" = 该模式不适用,声明表可省略该键。)

**Codex 交互能力边界**：Codex attach 当前是应答式 carrier——支持
`INTERACTIVE_ATTACH`、`WAKE_PUSH` 和对入站 hyprial 消息的自动 reply；不支持
`PROACTIVE_SEND` 或 `TOOL_INJECTION`，因此 Codex 模型侧没有
`harness_send`/`harness_reply` MCP 工具，不能主动向任意新目标发消息，也不能
像 Pi interactive 一样作为协调者。只有 Codex headless 具备 native
app-server tool injection/proactive send。

**一句话回答"谁能当协调者"** = INTERACTIVE_ATTACH∨HEADLESS_EXEC + WAKE_PUSH + PROACTIVE_SEND + TOOL_INJECTION 全 native:cc(交互/headless)、受管 pi headless、**pi 交互(2026-08-19 起,extension 机制)**均可;codex 交互目前不满足协调者条件，Codex headless 可以。

> 2026-08-19 解锁注记:pi 交互的 extension 机制 = 进程内 carrier 扩展
> `pi_harness_attach.ts`(注册/观察轮询/followUp 注入/settled 后 reply-ack)+ daemon
> 载体协议泛化到 SESSION_CARRIER_SOURCES 封闭集合(refresh/heartbeat/世代凭证;
> liveness 租约仍 claude 专属)。attended 人工验收(信任提示、followUp 不打断手头工作、
> reply 回链)属三层模型的 layer-2/3。

### 2.3 消费点改造(消除 if 散点;行为等价)

1. **launcher.py:83-113 双轨分派** → 查表:
   `mechanism = declare(spec.harness, headless=True)[HEADLESS_EXEC].mechanism`
   → `"agent_sdk"` 走 AgentSdkProcess、`"rpc"` 走 PiRpcProcess、`"pty"` 走连接器。
   `_STREAMING_PROVIDERS` 删除;`claude/pi command overridden` 回退 PTY 的语义保留(launcher.py:87-101 原逻辑,只是判断来源换成声明表+override)。
2. **cli.py:1167 谓词**:`connector_kind == "claude" and not headless` →
   `not headless and declare(harness_kind, headless=False).get(INTERACTIVE_ATTACH).mechanism == "channel"`。
   **`_start_interactive_claude` 函数体(cli.py:1016-1148)一个字节不动**——channel 接入留在 cli.py 是今晚的明确取舍(搬走风险高,后续阶段做)。
3. **fail fast 改进(唯一有意行为变化)**:非 headless 的 pi/codex `hyprial start` 今晚起在 CLI 本地报错,命名缺失能力("pi 不支持 interactive_attach;用 --headless 或换 cc"),不再等 daemon 用 desired-state 校验拒绝(desired_state.py:48-51)。错误路径仍是错误,JSON 契约不变。
4. daemon 侧 `lifecycle.start` 行为不变(仅符号改名)。

### 2.4 声明表自身的测试

- 每个 (harness, mode) 行完整(9 能力齐全或有"—"约定);mechanism 字符串 ∈ 已知机制集。
- 声明 native 的能力必须有对应实现符号存在(表→代码的链接测试,防声明漂移)。
- 金身断言:cc 交互行含 channel、cc headless 含 agent_sdk、pi headless 含 rpc、codex 全行无 native 的 C2/C3/C4/C6——锁住今晚的诚实基线,后续阶段每解锁一项必须改表+改测试,**能力变化从此显式可审**。

---

## 3. 今晚明确不做(后续阶段,按序)

| 阶段 | 内容 | 为什么不在今晚 |
|---|---|---|
| F1 | `_start_interactive_claude` 从 cli.py 搬进 harnesses/(channel 装配归位) | 协调者生产路径,需要金身测试先行+单独评审 |
| F2 | pi 补 C4/C6:PiRpcProcess 接 WorkerChannel(对照 agent_sdk.py:103-117;载体 spike:pi extension `registerTool` 优先,MCP 备选)⇒ **pi 可当协调者,Allen 目标最小兑现** | 需要 spike 验证载体(pi extension 的 `sendUserMessage`/`registerTool` 原语已证实存在,本机 pi docs extensions.md:898/78;TUI 内生命周期未验证) |
| F3 | ModelProfile 一等配置:`{provider, model, base_url, auth_ref}` 入 desired-state;收敛 cli.py:1104 的 `env=dict(os.environ)` 整包透传为白名单+profile 翻译;退役死字段 `endpoint`(desired_state.py:31,全仓无消费者) | 依赖 schema v2 |
| F4 | schema v2:写键切 `"harness"`、lark 从 harness 枚举剥离为 adapter 记录、值域 `"claude"`→`"cc"`、IPC/CLI 输出键切换(双写一个窗口期);生产 ~/.hyprial 迁移演练后执行 | 必须先在生产副本演练;有守卫+备份机制(desired_state.py:396 的 `_migrate_legacy` 模式)托底 |
| F5 | codex app-server `TurnClient`(thread/start+turn/start+turn/steer+interrupt;复用 streaming.py 泵)⇒ codex 收投递 | PR1 已落生命周期/注册/观察面;PR2 继续接入 durable inbox、FIFO 与恢复 |
| F6 | 角色能力要求注册表(`coordinator: requires(...)`),`hyprial doctor`/squire 输出能力矩阵;能力声明演进为 (harness, model_profile) 的函数(cc+非 anthropic 端点 ⇒ channel 系能力降级,因 cc channel 是 Claude.ai 登录专属的 research preview) | 依赖 F3 |

**目标形态(一段话)**:统一的是语义不是协议——custody 转移(deliver 只表示传输接受,终态只能由结构化结果或 harness_reply/ack 给出,daemon inbox 永远是真相源)、结构化终态(各机制映射到统一 Result 状态机)、身份注入(WorkerChannel 契约推广到所有 harness)、resume_token(cc session id / pi session id / codex thread id)。PTY 永久 degraded、禁止承载正式投递;不可归一项(通知胖瘦、steer 超集、codex TUI 注入)显式声明,不假装统一。

---

## 4. 验证与发版门禁(今晚)

1. **G1 金身(改谓词前必建)**:快照 `_start_interactive_claude` 全部可观察输出——argv 序列、写盘 mcp-config JSON、settings JSON、注册轮询参数(test_cli_claude_interactive.py 补齐到逐字节)。§2.3-2 改造后快照必须不变。这是"协调者不被改挂"的机器保证。
2. **重命名等价**:全量 pytest 绿;`git grep -n "provider" src/` 剩余命中必须逐条是 §1.3 白名单或 squire 正确术语(把这个 grep 做成 lint 测试,防回潮)。
3. **dual-read 契约测试**:§1.4 四条。
4. **能力表测试**:§2.4。
5. **e2e**:`HYPRIAL_HOME=/tmp/e2e HARNESS_STATE_DIR=/tmp/e2e`、`unset HYPRIAL_ZENOH_LISTEN` 下跑全链路(start headless cc/pi、投递、回复、ack、down)。
6. **发版**:pyproject 0.4.0.dev0 → **0.4.0.dev1**;changelog 注明"纯内部重命名 + 能力声明,wire/on-disk 全兼容,可回滚"。

**生产影响评估(明确写出)**:今晚的改动不要求重启生产 daemon;新旧 CLI↔daemon 任意混部安全(IPC 未变)。若 operator 选择重启 daemon 换新码:9 个 managed 条目走既有 recovery 恢复;协调者的 channel stdio child 按既有设计自动重注册(poll loop,channel.py:332-376;test_mcp_channel_restart.py 覆盖)——两条路都不碰 channel 会话本身。

---

## 5. 风险与最脆弱假设

1. **重命名 diff 大但机械**,风险不在正确性在评审遗漏——用 §4-2 的 grep lint 测试兜底;若评审时间紧,包目录改名(§1.1)可单独推迟而不阻塞能力 MVP(符号改名与目录改名无依赖)。
2. **fail-fast 改进(§2.3-3)是今晚唯一的行为变化**:`hyprial start pi`(无 --headless)的报错文案/时机变化;已有脚本若解析该错误文本会受影响——评估为可接受(错误仍是错误),但请在评审中明确签字。
3. **声明表的诚实基线靠人工维护**——§2.4 的链接测试只能防"声明了没实现",防不了"实现了没声明";后续 F2/F5 解锁能力时必须同 PR 改表。
4. **最大遗留赌注(不在今晚范围,记录在案)**:pi extension 替代 channel 的可行性(F2 spike)、codex app-server 的 per-worker MCP 隔离(F5)、cc channel 的 preview 身份与 Claude.ai 登录专属(与"任意服务商"在 cc 上结构性互斥)。这三项决定"同一角色任意 harness"的最终成色。
