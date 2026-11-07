# WP5：载体改到 §8.1 契约（实施规格 v2：总览、WP5-A、WP5-D）

- 版本：**v2，2026-10-14**。在 v1（`ae0d24bc5`）上并入 §9.4 集成者裁定（README 56–62）与 §11 评审（gpt-6.1-sol，阻断 B1–B9、建议 A1–A4）。评审项逐条落点见 §0.1；正文已是唯一执行依据，§11 原文只作记录。v2 同时把本包拆成四个子包（§1.1）。
- 段：设计（claude / claude-opus-5-5）
- 分支：`wp5/send-links`，base `refactor/org-structure-constrain`（WP0、WP1、WP7 已合；集成分支已有裁定 63）
- 依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §4.4、§4.5、§7、§8.1、§8.2、§11、§14；本目录 README 裁定 1–63（本包受约束：11、12、19、26、31、37、38、41、43、45、51、53–63）
- 缝：`src/hyprial/daemon/impl/links/ports.py`（`SettleHint`、`IPC_MESSAGE_READ_BODY`、`IPC_MESSAGE_SETTLE_HINT`、`LinkGate.settle_hint`）。⛔ 任何子包都不改此文件。
- 文件组：本文（总览、WP5-A、WP5-D）；[wp5c.md](wp5c.md)（WP5-C：pi、codex、claude 三个 interactive 载体）；[wp5b.md](wp5b.md)（WP5-B：interactive codex 的 thread-scoped MCP，裁定 57）。

## 结论

1. 模型代答只有三处：headless `DaemonEventBridge._reply_and_ack`（`runtime/forwards.py:717`，由 `runtime/results.py:260`、`:415` 调用，服务全部 headless 机制）；pi attach 的 `settleMessage`/`failOutstanding`（`pi_harness_attach.ts:476`、`:534`）；codex interactive 的 `_settle_final` 与 `_settle_reply_unavailable`（`codex/settlement.py:162`、`:251`）。三处全部删除。headless 另有两种**机制应答**按裁定 58/59 保留，但身份、关联与重试按 §3.4 收紧，不得由 payload 标记或 harness 名授权。
2. 契约分两块，都在 daemon 域、都不认识具体 harness：`harnesses/carrier/contract.py`（注入渲染、pending 行解析、轮末结算驱动、`CarrierPort`、turn IPC 名）与 `inbox/contracts/settlement.py`（`settle_hint_for`，IPC 端与 headless 端唯一判定）。turn IPC 不按 runtime 名准入，只按可信 caller 与当前 interactive session 的 fence。新 harness 只写适配。
3. turn 边界：所有 `turn.*` 由 `DaemonHookService` 在同一个发布临界区内盖 seq 并入队；interactive 载体经 IPC 上报，daemon 按 `(actor, sessionRef, reportId)` 去重、首次接受时固定时刻；载体必须持有未确认报告并以同一 reportId 重试。
4. 轮末结算：先报 turn 结束，再对本 turn 注入过的每条消息问 `message.settle_hint`，`ack` 则 ack，`keep` 则什么都不发。非 request 的 pending 行答 ack（裁定 56）。headless 用 `fetched_at_ms` 作「已注入」持久事实（裁定 61），但失败重试资格由 durable 失败记录单独决定（§3.3）。
5. PAC 与 routine 的内部投递带 summary，由最终文本确定性派生（WP5-D）。
6. 拆分：WP5-A（daemon 侧契约与 headless）与 WP5-D（PAC summary）可并行；WP5-C（三个 interactive 载体）在 WP5-A 之后；WP5-B（codex MCP 面）在 WP5-C 之后。集成分支在 WP5-B 合入前不合 dev（裁定 57）。

## 0. 需求映射

| 需求 | 来源 | 子包与节 | 测试 |
| --- | --- | --- | --- |
| (a) 载体不代答，删三处自动回复 | 设计 §4.5、§8.1；README 行 5；裁定 31、62 | A §3.1；C wp5c §2.4、§3.4 | H1、H3、G3；P3、P6、X2、G3p |
| (a') 机制应答保留且受限 | 裁定 58、59、63 | A §3.4 | M1–M6、G3 |
| (b) 注入只带 summary 等字段 | 设计 §7、§8.1；裁定 60 | A §2.3、§3.5；C wp5c §2–§4 | K1–K4、H5、H8、M2；P1、P2、X1、X6、L2 |
| (c) 上报 turn 开始与结束 | 设计 §8.1；裁定 11、12 | A §4；C wp5c 各节 | T1–T5、T7–T10、K8；P4、P5、P9、X4、X5、X7、L1、L3、L4 |
| (c') seq 不回退、不乱序 | wp7-acceptance-2 | A §4.3、§4.5 | T6、T11、T12 |
| (d) 轮末问 settle_hint 并照做，幂等 | 设计 §8.1；裁定 56 | A §2.4、§3.2、§5；C | K5、K6、H2–H4、H7、H9、R1、R2、R7；P3、P7、X2、X3、X8、L2 |
| (d') keep 的积压、释放、恢复 | 评审 B9 | A §7.5；C wp5c §5 | R1–R7、P10、X8、X9 |
| (e) PAC 投递带 summary | 裁定 31 | D §6 | S1–S5、G6 |
| (f) 与 harness 无关 | 设计 §8.1（R18）、§14 | A §2.1 | K7、K8、K9、G1、G2、G7 |
| (g) interactive codex 有回复通道 | 裁定 57 | B wp5b | Y1–Y7 |

## 0.1 评审项落点

| 项 | 内容 | 落点 | 测试 |
| --- | --- | --- | --- |
| B1 | fetched 戳不能单独决定失败重试资格 | §3.3；§1.4 补 inbox 参数链 | H9、H10、R2 |
| B2 | codex KEPT 被 CarrierSettled 删除；按 (actor, messageId) 去重 | wp5c §3.3、§3.4 | X3、X8、X9 |
| B3 | 旧 FETCHED 行不得原文注入 | wp5c §3.5 | X6 |
| B4 | claude 补 turn 开始、轮末 settle | wp5c §4 | L1–L5 |
| B5 | 去 `CARRIER_RUNTIMES`；`settle_hint_for` 移入 `inbox/contracts/settlement.py`；判定用权威元数据 | §2.1、§2.4、§4.1 | K6、K8、G1、G2、G7 |
| B6 | 机制应答的身份、关联、重试；G3 改函数级允许表 | §3.4、§5、§8 | M1–M6、G3 |
| B7 | turn 报告的重试所有者；daemon 按 (actor, sessionRef, reportId) 去重 | §4.2；wp5c §2.3、§3.2、§4.2 | T9、T10；P9、X7、L4 |
| B8 | seq 分配与入队同一临界区 | §4.3 | T11、T12 |
| B9 | keep 的积压、释放、恢复场景 R1–R7 | §7.5；wp5c §5 | R1、R2、R5、R6、R7；P10（R3）；X8、X9（R4） |
| A1 | 预算重测 | §1.4–§1.6（本分支、WP3-A、WP4 三列实测） | 验收 §8 第 8 条 |
| A2 | shell/biz 只用公共面 | §6、§2.1 | G6、G7 |
| A3 | 「同生同灭」写成生命周期不变量 | §4.5 | T6 |
| A4 | WP5-B 独立交付门，测 read_body/ack/summary/fence | wp5b §3、§4 | Y1–Y7 |

## 1. 拆分、范围、预算

### 1.1 子包与合并顺序

| 子包 | 分支 | 范围 | 前置 | 规格 |
| --- | --- | --- | --- | --- |
| WP5-A | `wp5a/send-links` | daemon 侧契约、headless 载体、turn IPC、seq 临界区、失败重试资格、机制应答、keep 恢复场景 | WP3-A、WP4 返工 2（含裁定 63 守卫）合入集成分支 | 本文 §2–§5、§7.1–§7.6、§8 |
| WP5-D | `wp5d/send-links` | PAC 与 routine 内部投递带 summary | WP3-A 合入 | 本文 §6、§7.7、§8 |
| WP5-C | `wp5c/send-links` | pi attach、codex interactive、claude channel 三个适配 | WP5-A 合入 | wp5c.md |
| WP5-B | `wp5b/send-links` | interactive codex 线程挂 `harness-bridge` MCP，能力改 native | WP5-C 合入 | wp5b.md |

合并顺序：`WP3-A、WP4 → {WP5-A ∥ WP5-D} → WP5-C → WP5-B`。WP5-A 与 WP5-D 文件不相交，可任意先后。WP5-C 内部三个载体文件不相交，实施者可再分 `wp5c-pi`、`wp5c-codex`、`wp5c-claude` 三条分支并行，合并次序不限。WP3-B 在 WP5-A、WP5-C、WP5-D 合入后进行（裁定 27）；集成分支合 dev 还需 WP5-B（裁定 57）。

### 1.2 各子包禁区（⛔ 改了即验收不通过）

- 全部：`links/ports.py`、`links/` 下任何文件、`kernel/`、`biz/`（含 `biz/impl/turn_pulse.py` 与 WP7 消费者）、`pi_harness_bridge.ts`（WP6）、`ipc/wire.py`、`contract/`（WP8）、`dispatch/remote/__init__.py` 除 WP5-D 的那一行外。
- WP5-A：不改 `harnesses/pi/`、`harnesses/codex/`、`harnesses/carrier/codex_carrier_store.py`、`harnesses/carrier/interactive_carrier_runtime.py`、`mcp/channel/`、`shell/`、`capabilities.py`、`pac/`、`inbox/links/io.py`。
- WP5-D：只改 §6 列出的文件。不改 `runtime/`、`harnesses/`、`hooks/`、`application/actors/sessions/`。
- WP5-C：不改 `contract.py`、`inbox/contracts/settlement.py`、`application/actors/sessions/turns.py`、`hooks/service.py`、`runtime/`、`inbox/` 的逻辑；契约有缺口时停下报集成者，不在适配里补第二份判定。不改 `capabilities.py`（归 WP5-B）。
- WP5-B：不改结算、注入、turn 上报（WP5-C 的产物）；只改 wp5b §2 的文件。

### 1.3 非目标

- 不改 MCP 工具面（WP6）、CLI（WP6）、`message.pending.list` 行形状与 pending 行 `message` 键（WP3-A/B）。载体只保证不读 `message`。
- 不改 user-proxy 的 `forward` 帧与 `_forward_as_actor`（裁定 37）；不改 fail-loud 通知（`_submit_loud`、`_loud_harness_failure`）；不改 PAC workflow outcome 的 `ack` 分支。
- 不改 wire golden 的 `autoReply`（交 WP8，§10）。`top` 的 interactive turn 计数显示不扩展（`operations/top.py:388`）。
- python_worker 不是模型上下文：照传原文（裁定 60，H8 守护）。

### 1.4 WP5-A 文件与行数预算

三列现行数为 2026-10-14 实测（`wc -l`，本分支 / `wp3/send-links` / `wp4/send-links`）。实施 base 上以合入后的数为准，按净增减核。

| 文件 | 现（本 / 3A / 4） | 动作 | 预算 |
| --- | --- | --- | --- |
| `daemon/impl/harnesses/carrier/contract.py` | 新 | §2.2 | ≤ 240 |
| `daemon/impl/inbox/contracts/settlement.py` | 新 | §2.4 `settle_hint_for` | ≤ 80 |
| `daemon/impl/application/messaging/inbox_surface/queries.py` | 511 / 779 / 563 | `_ipc_message_settle_hint` 改调 `settle_hint_for` | 净减 |
| `daemon/impl/application/actors/sessions/registry.py` | 969 | turn 报告三段移出 | ≤ 880 |
| `daemon/impl/application/actors/sessions/turns.py` | 新 | §4.1、§4.2 | ≤ 320 |
| `daemon/impl/ipc/router.py` | 850 / 860 / 850 | 路由 `session.turn.started` | 净增 ≤ +4 |
| `daemon/impl/hooks/service.py` | 681 | `observe_carrier_turn`；发布临界区（§4.3） | ≤ 760 |
| `daemon/impl/runtime/results.py` | 636 | 普通结果按提示结算；机制应答分派 `_settle_mechanism_answer` | ≤ 782（返工 1 实测 782，含启动结算扫描 `_settle_stranded_rows`；原 ≤ 700） |
| `daemon/impl/runtime/forwards.py` | 897 | 删 `_reply_and_ack`；增 `_answer_hook_request`、`_answer_mechanism_result` | ≤ 977（返工 1 实测 977，含私有 `_confirm_mechanism_answer`；原 ≤ 950） |
| `daemon/impl/runtime/settlement.py` | 483 | `_reply_already_answered` 改名 `_answer_already_committed`（只给机制应答用） | 483 |
| `daemon/impl/runtime/__init__.py` | 41 | 导出改名 | 41 |
| `daemon/impl/runtime/reconcile.py` | 534 / 534 / 545 | 注入按 §3.5 三分；`_pending_reply_results` 改名 | 净增 ≤ +25（返工 1 实测 +25；原 ≤ +15） |
| `daemon/impl/runtime/bridge.py` | 634 / 634 / 641 | 改名 `_pending_settle_results` | 净增 ≤ +1（返工 1 实测 +1：`start()` 调一次启动结算扫描；原 0） |
| `daemon/impl/harnesses/turn_delivery/protocol.py` | 68 | `TURN_HOOK_CALLBACK_ACTOR`、`is_mechanism_hook_request`、`mechanism_of` | ≤ 108（返工 1 实测 108；原 ≤ 100） |
| `daemon/impl/inbox/contracts/ports.py` | 385 / 388 / 415 | `FetchPendingCommand.completed_turn` | 净增 ≤ +4（返工 1 实测 +4；原 ≤ +3） |
| `daemon/impl/inbox/contracts/api.py` | 249 / 249 / 249 | `fetch_pending` 协议签名加 `completed_turn` | 净增 ≤ +3 |
| `daemon/impl/inbox/authority/facade.py` | 641 / 644 / 688 | 透传 `completed_turn` | 净增 ≤ +4 |
| `daemon/impl/inbox/actor/custody/lifecycle.py` | 538 / 539 / 568 | 同一事务清非 terminal 失败结算行 | 净增 ≤ +12 |
| `daemon/impl/inbox/service/projection.py` | 438 / 445 / 569 | 同上（非 authority 路径）；派发查询条件（§3.3） | 净增 ≤ +38（返工 1 实测 +38；原 ≤ +20） |
| `daemon/impl/inbox/authority/read.py` | 699 / 699 / 735 | 派发查询条件（§3.3） | 净增 ≤ +10 |
| `daemon/impl/dispatch/admission.py` | 93 / 123 / 387 | `LinkAdmission.current_gate()` 只读访问器 | 净增 ≤ +5（返工 1 实测 +5；原 ≤ +4） |
| `tests/inbox_authority_exemptions.json` | — | 两条 `fetch_pending`（裁定 61）；删 `_reply_and_ack` 两条；新 owner 两条（§3.4） | 实测 61 → 64（净 +3；返工 1：`_answer_*` 四条并为 `_confirm_mechanism_answer` 两条，另加 `_settle_stranded_rows` ack 一条） |
| `tests/support/gates/authority_migration.py` | — | 删 `_reply_and_ack` 两键，增两个机制应答键 | — |

⛔ 不得压缩其它代码凑行数。`application/wiring/transport.py`（992 行）不改：`_turn-hooks` 的常量化由测试钉住（M1）。

### 1.5 WP5-D 文件与行数预算

| 文件 | 现 | 动作 | 预算 |
| --- | --- | --- | --- |
| `daemon/impl/inbox/links/io.py` | 369 | `deliver(..., summary)` 必填；`derive_summary` | ≤ 390 |
| `daemon/impl/pac/graphs/reactor.py` | 977 | 协议加 `summary`；两处调用 | ≤ 984 |
| `daemon/impl/pac/graphs/authority/__init__.py` | 946 | 一处调用 | ≤ 949 |
| `daemon/impl/pac/workflows/runtime/operations.py` | 819 | 一处调用 | ≤ 822 |
| `daemon/impl/pac/workflows/runtime/types.py` | 372 | 透传 | ≤ 378 |
| `daemon/impl/pac/actors/daemon.py` | 888 | 透传 | ≤ 892 |
| `daemon/impl/dispatch/remote/__init__.py` | **999** | 只加一行 `summary=self.app._pac_notification_io.derive_summary(text),`，不加 import | **= 1000** |
| `daemon/impl/application/actors/routines.py` | 805 | 一处调用 | ≤ 808 |
| `shell/impl/cli/commands/workflow/control.py` | 250 / 252 / 250 | `_WorkflowNotificationSender.send` 只透传 summary | ≤ 256 |

`dispatch/remote/__init__.py` 若因其它包先增行而无空间，停下报集成者，不拆文件、不压代码。

### 1.6 目录直接文件数（`find <dir> -maxdepth 1 -type f | wc -l`，本分支实测；上限 7）

| 目录 | 现 | 后 | 子包 |
| --- | --- | --- | --- |
| `src/.../harnesses/carrier/` | 3 | 4（`contract.py`） | A |
| `src/.../inbox/contracts/` | 3（WP3-A 后 4） | 5（`settlement.py`） | A |
| `src/.../application/actors/sessions/` | 3 | 4（`turns.py`） | A |
| `src/.../ipc/`、`inbox/actor/custody/`、`harnesses/codex/`、`shell/.../agent/runtime/` | 7 | 7 | 只改不增 |
| `src/.../runtime/`、`inbox/service/`、`inbox/authority/`、`mcp/channel/` | 6 | 6 | |
| `src/.../hooks/`、`harnesses/turn_delivery/` | 3 | 3 | |
| `tests/daemon/harnesses/carrier/`（新） | 0 | 4：`__init__.py`、`injection_golden.json`、`test_carrier_contract.py`、`test_carrier_boundaries.py` | A |
| `tests/daemon/harnesses/carrier/headless/`（新） | 0 | 4：`__init__.py`、`test_headless_carrier.py`、`test_mechanism_answers.py`、`test_keep_recovery.py` | A |
| `tests/daemon/harnesses/carrier/turns/`（新） | 0 | 3：`__init__.py`、`test_turn_reports.py`、`test_turn_seq.py` | A |
| `tests/daemon/inbox/io/` | 2 | 3（`test_inbox_io_summary.py`） | D |
| `tests/daemon/harnesses/pi/extension/` | 6 | 6（删 `test_pi_reply_settlement.py`，增 `test_pi_carrier_contract.py`） | C |
| `tests/daemon/harnesses/codex/` | 5 | 6（C：`test_codex_carrier_contract.py`），7（B：`test_codex_interactive_mcp.py`） | C、B |
| `tests/daemon/channels/adapters/` | 4 | 5（`test_channel_carrier_contract.py`） | C |

每个新测试文件 ≤ 1000 行；超出时在同目录内拆，不得超过上表的目录上限。

## 2. 与 harness 无关的契约（WP5-A）

### 2.1 文件边界

```
harnesses/carrier/contract.py        渲染、行解析、结算驱动、CarrierPort、turn IPC 名（无判定）
inbox/contracts/settlement.py        settle_hint_for：唯一判定，只读，不认识载体
        ^                         ^
runtime/（headless 适配）     application/messaging/inbox_surface/queries.py（IPC 端）
application/actors/sessions/turns.py（turn IPC 端；不 import contract.py，方法名由 K9 钉住）
pi / codex / claude 适配（WP5-C）
```

- `contract.py` 只许 import：标准库、`hyprial.kernel`、`hyprial.daemon.impl.links.ports`、`hyprial.daemon.impl.inbox.contracts.api`、`hyprial.daemon.impl.inbox.contracts.summary`（WP3-A）。G1 机检。
- `inbox/contracts/settlement.py` 只许 import：标准库、`hyprial.kernel`、`hyprial.daemon.impl.links.ports`、`hyprial.daemon.impl.inbox.contracts.api`。G1 机检。
- 反方向：`links/`、`inbox/`、`application/` 不 import `harnesses.carrier`（G2）。消息权威入口不依赖载体目录。
- harness 名：`contract.py`、`inbox/contracts/settlement.py`、`turns.py` 中不得出现 `pi_interactive`、`codex_interactive`、`claude_interactive`、`claude-channel` 等任何 runtime 或 harness 名（G7）。具体 runtime 名、启动参数、MCP 配置、hook 信号格式只属于各自适配器或 composition。
- 层：全部在 daemon 域；kernel、identity、biz 不改；shell 只在 WP5-C/D 改两处文字与一处参数透传，且只用 `hyprial.daemon` 公共面（G6）。
- 新 harness 接入只做三件事：用 `render_injection`（或 golden）渲染注入；实现 `CarrierPort` 的四个调用（Python 可用 `IpcCarrierPort`）；按现有 `session.register` 注册 interactive 会话。K8 证明不需要改 contract、links、inbox。

### 2.2 `contract.py` 公共内容

```python
IPC_SESSION_TURN_STARTED = "session.turn.started"
IPC_SESSION_TURN_ENDED = "session.turn.ended"        # 已有方法，语义扩展见 §4
TURN_END_STATUSES = frozenset({"completed", "failed"})
INJECTION_KINDS = frozenset({"message", "notice"})
INJECTION_FORMAT = "summary-v1"                       # 持久化注入文本的载体（codex store）记这个版本

@dataclass(frozen=True, slots=True)
class Injection:
    message_id: str; sender: str; recipient: str; intent: str
    kind: str                 # "message" | "notice"
    summary: str | None       # None：旧行或无 summary 的内部投递
    body_bytes: int

def injection_from_row(row: Mapping[str, Any]) -> Injection | None
    # 只读 messageId/from/to/intent/kind/summary/bodyBytes；⛔ 不读 message、payload、topic。
    # 缺 messageId → None；kind 缺省 "message"；bodyBytes 缺失或非 int → 0。
def injection_from_inbox(message: InboxMessage, *, notice: bool = False) -> Injection
    # summary = message.summary；body_bytes = inbox.contracts.summary.body_bytes(message)；
    # notice=True：kind="notice"、summary=WP3 的 notice 全文规则、body_bytes=0。
def render_injection(item: Injection) -> str            # 逐字见 §2.3

class CarrierPort(Protocol):
    def settle_hint(self, message_id: str) -> SettleHint: ...
    def ack(self, message_id: str) -> bool: ...          # True：已 ack 或已不在（ACK_UNAVAILABLE）
    def turn_started(self, report_id: str, *, delivery_id: str = "") -> None: ...
    def turn_ended(self, report_id: str, *, status: str, delivery_id: str = "") -> None: ...

@dataclass(frozen=True, slots=True)
class SettleOutcome:
    acked: tuple[str, ...]; kept: tuple[str, ...]
    deferred: tuple[str, ...]   # 传输或 authority 暂不可用，下次重试

def settle_injected(port: CarrierPort, message_ids: Sequence[str], *,
                    already: Collection[str] = ()) -> SettleOutcome
    # 对每个 id（跳过 already）：hint = port.settle_hint(id)；ack → port.ack(id)；keep → 不发。
    # 单个 id 的异常只把它记入 deferred。从不回复。

class IpcCarrierPort:       # Python 载体通用适配；方法名用本模块与 ports 的常量
    def __init__(self, request: Callable[[str, dict[str, Any]], Mapping[str, Any]],
                 signed: Callable[[dict[str, Any]], dict[str, Any]]) -> None
```

没有 `CARRIER_RUNTIMES`，没有任何 runtime 白名单。

### 2.3 注入文本（逐字；Python 与 TS 共用 golden）

`kind == "message"` 且 summary 非空：

```
summary: <summary>
(intent: <intent>, kind: message, messageId: <id>, bodyBytes: <n>)
Full body: harness_read_body("<id>") or `hyprial read <id>`. Reply with harness_reply("<id>", …); if no reply is needed, harness_ack("<id>").
```

summary 为 None：首行为 `summary: (none; the sender gave no summary)`，其余不变。`kind == "notice"`：只有一行 `notice: <summary>`。

- 只用 §8.1 允许的字段；发送者与收件人在现有头里（`api.delivery_prompt`、TS `formatBatch` 照旧）；块内不出现正文任何字节。
- 末行不承诺任何自动行为。pi 批量头改为 `Each item below is a summary only; read a body on demand and reply or ack each item explicitly.`（WP5-C 实施）。
- `tests/daemon/harnesses/carrier/injection_golden.json` 至少 6 例：普通请求、reply intent、无 summary、notice、summary 含换行与非 ASCII、bodyBytes 为 0。每例 `row` 带 `message: "BODY-SENTINEL-…"`，`expected` 为块文本。K2、P2 读同一文件。

### 2.4 `inbox/contracts/settlement.py`：唯一判定

```python
def settle_hint_for(*, pending: InboxMessage | None, gate: LinkGate | None,
                    actor: str, message_id: str,
                    log: Callable[..., None] | None = None) -> SettleHint:
    # pending：调用方按调用者自己的 consumer keys 在权威 inbox 中查到的那一行；查不到为 None。
    # 1. pending is None                    → "ack"（已回复、已 ack、已排干，或不是调用者的行）
    # 2. pending.intent != "request"        → "ack"（裁定 56：reply/event 不欠回复）
    # 3. gate is None                       → "keep"
    # 4. gate.settle_hint(actor=, message_id=)；不在 {"ack","keep"} → "keep" 并
    #    log("warn", "daemon", "message.settle_hint.invalid", messageId=, hint=str(x)[:64])
```

- intent 只取权威行，⛔ 不接受载体参数里的 intent。
- 外人 id：不在调用者 keys 下 → 规则 1 答 `ack`（WP3-A 现行身份规则，K6 钉住）。载体只会对自己注入过的 id 发问；对外人 id 的 ack 会得到 `MESSAGE_ACK_UNAVAILABLE`，不改他人状态。
- IPC 端：`queries.py::_ipc_message_settle_hint` 保留 WP3-A 的 caller 认证、`_require_body_fence`、consumer keys，只把判定换成本函数；返回体字段不变（`hint`、`pending`）。
- headless 端：§3.2。门取 `LinkAdmission.current_gate()`（WP4 对象上新增只读访问器）；IPC 端取 `DaemonApplication._link_gate`（WP3-A）。两者必须是 WP2 composition 绑定的同一实例：K6 在 WP2 合入前以两端都为 None 运行，合入后集成者补同一性断言。
- 本函数取代 WP3-A 的判定；WP3 T15 的「reply 行答 ack」用例由 K6 覆盖（裁定 56）。

## 3. headless（WP5-A；覆盖 turn_delivery 下全部 headless 机制）

### 3.1 删除

- `runtime/forwards.py::_reply_and_ack`（717–743 行，`uuid5(NAMESPACE_URL, f"hyprial-agent-sdk-reply:{id}")` 的 reply）整段删除，不留兼容壳。
- `runtime/results.py` 中 254–266 行与 409–424 行两处 `try: if original.intent == "reply": … else: self._reply_and_ack(...)` 块，由 §3.2/§3.4 替换。
- `results.py::_reply_already_settled` 改名 `_mechanism_answer_settled`，`runtime/settlement.py::_reply_already_answered` 改名 `_answer_already_committed`（`runtime/__init__.py:28` 导出同步）；两者只在 §3.4 的机制应答路径调用。
- 门禁数据：`tests/support/gates/authority_migration.py` 第 39、40 行两条 `_reply_and_ack` 键删除；`tests/inbox_authority_exemptions.json` 中 owner 为 `DaemonEventBridge._reply_and_ack` 的两条（ack、submit）删除；`_submit_loud` 条目只改 reason 措辞。

### 3.2 普通模型结果：盖章、提示、照做

适用：COMPLETED、非 forward、非已处理的 workflow outcome、且不属于 §3.4 两种机制的结果。两处调用点写同一段（写在原所有者方法内，authority 门的 owner 不变）：

```python
self.inbox.fetch_pending(original.recipient, message_id=original.message_id,
                         completed_turn=True)                  # §3.3：盖首次戳 + 清非 terminal 失败结算
pending = next((m for m in self.inbox.pending_messages(original.recipient)
                if m.message_id == original.message_id), None)
hint = settle_hint_for(pending=pending, gate=self._link_admission.current_gate(),
                       actor=original.recipient, message_id=original.message_id, log=self._logger)
acknowledged = (self.inbox.ack(original.recipient, original.message_id).acknowledged
                if hint == "ack" else True)                    # keep：结算完成，行保持 pending
```

- 三步任一步抛 `InboxAuthorityTimeout`/`InboxAuthorityUnavailable`：结果放入 `_pending_settle_results`（由 `_pending_reply_results` 改名：`bridge.py:224`、`:367`，`reconcile.py:264`，`results.py:114`、`:268`、`:298`、`:315`），下一 tick 从第一步重跑同一段，⛔ 不重跑模型 turn；reconcile 不派发其中的 id。日志 `harness.settle.deferred`。
- 重跑时行已不 pending（上次 ack 已提交但确认丢失）：第一步对不 pending 的行无操作，提示为 `ack`，`ack` 返回未确认或 `MESSAGE_ACK_UNAVAILABLE` 一律视为已结算，移出 `_pending_settle_results`。⛔ 不得因异常把 keep 改成 ack。
- reply/event 行：同样走这段，按裁定 56 得 `ack`（H4）。
- 三步都不写任何 reply（G3）。

### 3.3 失败重试资格（B1）

事实：WP3-A `queries.py::_ipc_message_read_body`（`wp3/send-links` 第 448–450 行）在返回正文前调用单条 `fetch_pending`。模型读正文后 turn 失败时，行已有 fetched 戳；若派发查询只认 `fetched_at_ms IS NULL`，失败重试永远不会发生。

规则：

1. `fetched_at_ms` 只写一次（COALESCE），是首次注入或读取的持久事实。
2. 两份派发查询（`inbox/service/projection.py` 与 `inbox/authority/read.py` 的 `dispatchable_messages`；若 WP4 把派发候选改到别的查询，改那一条）保留现有 TTL、consumed、terminal、backoff 过滤，增加：
   ```sql
   AND (inbox.fetched_at_ms IS NULL
        OR EXISTS (SELECT 1 FROM harness_failure_settlements s
                    WHERE s.message_id = inbox.message_id
                      AND s.terminal = 0 AND s.next_attempt_ms <= ?))
   ```
   只有 fetched 戳、没有到期失败记录的 pending 行不可派发。
3. `FetchPendingCommand` 增加 `completed_turn: bool = False`；`fetch_pending(recipient, *, message_id=None, completed_turn=False)` 在 `contracts/api.py`、`contracts/ports.py`、`authority/facade.py`、`actor/custody/lifecycle.py`、`service/projection.py` 一并更新。`completed_turn=True` 只许与 `message_id` 同用，在同一 inbox 事务内：盖首次 fetched 戳；`DELETE FROM harness_failure_settlements WHERE message_id = ? AND terminal = 0`。⛔ 不删 append-only 的 `harness_failure_attempts`，不删 terminal 行。公共读取与 `read_body` 用默认 False。
4. 经 inbox authority：`results.py` 两个 owner 的 `fetch_pending` 即裁定 61 的两条豁免（operation `fetch_pending`，reason「WP5：headless 轮末对单条已注入行盖 fetched 章并清非 terminal 失败结算，经 DeliveryCustodyFacade 的 FetchPendingCommand(message_id, completed_turn=True)」），不新增绕过 authority 的 SQL 写。
5. FAILED 结果不盖章、不清记录，原失败重试路径不变；permanent（terminal）失败不再派发。

### 3.4 机制应答（裁定 58/59，B6）

两种例外，条件与注入例外（§3.5）**同一判定**，只在 COMPLETED、非 forward、非已处理 workflow outcome 的分支执行：

| 例外 | 判定（全部为真） | 应答函数 | id 前缀 |
| --- | --- | --- | --- |
| hook 交换 | 权威 `original.intent == "request"`；`is_hook_request(original)`；`parse_agent_uri(original.sender)` 的 actor 段等于 `TURN_HOOK_CALLBACK_ACTOR`（`"_turn-hooks"`，裁定 45/51；注册守卫见裁定 63） | `_answer_hook_request` | `hyprial-turn-hook-reply:` |
| python_worker | 权威 `original.intent == "request"`；`mechanism_of(self._harness_kind(recipient actor)) == "python_worker"`，其中 `mechanism_of(h) = support(h, headless=True, capability=HEADLESS_EXEC).mechanism`。user-proxy 的 `result` 帧属此类；`forward` 帧仍走原 forward 路径 | `_answer_mechanism_result` | `hyprial-mechanism-reply:` |

- `turn_delivery/protocol.py` 新增 `TURN_HOOK_CALLBACK_ACTOR`、`is_mechanism_hook_request(message) -> bool`（上表第一行三条）与 `mechanism_of`。`reconcile.py` 与 `results.py` 只调这两个函数，⛔ 不信 `delivery.hook_request` 布尔、不按 harness 名猜机制。
- 应答消息：`message_id = str(uuid5(NAMESPACE_URL, prefix + original.message_id))`；`conversation_id = original.conversation_id`；`sender = original.recipient`、`recipient = original.sender`；`intent = "reply"`；payload `{"message": output, "replyTo": original.message_id, "origin": {"via": "daemon", "mechanism": "turn-hook" | "python_worker", "onBehalfOf": original.recipient}}`；`summary = summary_from_text(output)`（空输出时 `summary_from_text("(empty result)")`，正文仍为空串）；`idempotency_key = f"reply:{original.message_id}"`；`lifecycle = DURABLE_SERVICE`。
- 顺序：先 durable submit 并确认，再 ack 原请求。submit 抛 authority 超时：结果留在 `_pending_settle_results`，下一 tick 以**同一结果、同一 id** 重试；`_answer_already_committed(error)`（receipt conflict）表示已提交，直接 ack。⛔ 不重跑 handler 或脚本，不改成 ack。
- 分派集中在 `results.py::_settle_mechanism_answer(original, result) -> bool | None`（None 表示不属于机制例外，走 §3.2）。两处结果路径各调用它一次。
- reply/event 行、非 request 行永不触发机制应答（M6）。
- authority 门：两个新 owner（`DaemonEventBridge._answer_hook_request`、`_answer_mechanism_result`）的 submit 与 ack 各一条豁免，替代删除的 `_reply_and_ack` 两条；`authority_migration.py` 同步。净豁免数：+2（裁定 61）+4 −2 = +4，PR 正文列出。
- 依赖：身份判定的安全性依赖裁定 63 的注册守卫（WP4 返工 2）。守卫未合入前 WP5-A 不合入。

### 3.5 注入

`reconcile.py` 构造 `HarnessDelivery.message` 时三分：

1. `is_mechanism_hook_request(message)` → 原文（`self._message_text(message)`），handler 需要完整事件 JSON。
2. 收件 actor 的机制为 `python_worker` → 原文（裁定 60；user-proxy 转递比对原文）。
3. 其余 → `render_injection(injection_from_inbox(message))`；system notice 分支 → `render_injection(injection_from_inbox(notice, notice=True))`。

普通发送者伪造 `turnHookRequest: true` 只得到第 3 种（M2）。`api.delivery_prompt` 不改。

### 3.6 headless 的 turn 事件

headless 的 `turn.started`/`turn.completed` 已由 `TurnObserver`（`hooks/service.py:636` 起）发出，经 §4.3 的同一发布路径盖 seq。本包不改这条链的事件形状。

## 4. turn 上报（WP5-A daemon 侧）

### 4.1 IPC

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `session.turn.started`（新） | `actor`、`sessionRef`（必填）、`reportId`（1–128 字符）、`deliveryId`（可选，≤ 128） | 可信 caller + `_fence_interactive_session`；会话必须是该 actor 当前注册的 interactive session（任何 runtime），否则 fence 错误或 `INVALID_ARGUMENT`；按 §4.2 去重；返回 `{"ok": true, "startedAtMs": <首次接受时刻>}` |
| `session.turn.ended`（已有，扩展） | 原 `reportIds`；新增可选 `status`（`completed` 默认 / `failed`）、`deliveryId` | 去掉 `session.runtime != "claude_interactive"` 检查，改为同上的当前会话检查；原计数与 `top` 统计不变；按 §4.2 去重与发布；返回体原字段不变，增加 `endedAtMs`（本次第一个 reportId 的固定时刻） |

`router.py` 以字面量登记两个方法（`_IPC_STATS_METHODS` 一并登记）；K9 钉住它们等于 `contract.py` 的常量。kernel `worker_channel.METHOD_PARAMS` 不加：三个 interactive 载体走 daemon socket。实现放新文件 `application/actors/sessions/turns.py`（mixin）；`registry.py` 的 `_ipc_session_turn_ended`、`_accept_interactive_turn_report`、`_record_interactive_turn_reports` 三段整体移入。

### 4.2 记账、幂等与关联（B7 daemon 侧）

- 状态按 `(actor, sessionRef)` 存于现有 `_interactive_turn_stats_lock` 保护的记账内：`started: OrderedDict[reportId, startedAtMs]`、`ended: OrderedDict[reportId, endedAtMs]`，各上限 1024，LRU。会话注销、换代或被取代时整份清除（沿用 `_reset_interactive_turn_stats`）；旧 sessionRef 的报告被 fence 拒绝，不能改变新 session 的状态。
- started：`reportId` 已在 `started` → 返回已记录时刻，不发事件。否则记录 `clock_ms()` 读数并发一个 `turn.started`。一个新 reportId 的 started 不覆盖另一个 reportId 的记录。
- ended：对 `reportIds` 中每个不在 `ended` 的 id，按数组顺序：记录 `endedAtMs = clock_ms()`（首次接受即固定），`startedAtMs = started.get(id)`（无则 None），发一个 `turn.completed`（`status: failed` 时 `turn.failed`）。已在 `ended` 的 id 不再发事件；返回体的 `endedAtMs` 取第一个 id 已固定的时刻。原 `turnCount` 照旧按新 id 数累加，时刻改用同一 `clock_ms()`。
- 时刻：`at_ms` 取 `DaemonHookService.clock_ms()`。载体参数里的 `seq`、`source`、`startedAtMs`、`endedAtMs`、`emittedAtMs` 一律不读（T1 传入假值验证）。
- 发布在释放记账锁之后进行（§4.3）；记账锁内只做判定与记录。

### 4.3 发布与 seq 临界区（B8）

`DaemonHookService` 新增：

```python
def observe_carrier_turn(self, *, actor: str, phase: Literal["started", "completed", "failed"],
                         at_ms: int, started_at_ms: int | None, delivery_id: str = "") -> None
```

- `self._publishes(actor, f"turn.{phase}")` 为假不发（与 headless 同一门）。payload 用 `kernel.turn_event_payload`：`delivery_id`、`started_at_ms = started_at_ms or at_ms`、`ended_at_ms = at_ms`、`status = phase`、`sender/conversation/output/prompt = ""`、`tool_names = tool_calls = ()`、`fidelity = "observed"`、`failure_code = None`。`actor` 为 fence 返回的规范 URI。
- `_publish` 改为：在一个新锁 `_publish_lock` 内**先分配 seq、再构造事件、再 `self._bus.publish(event)`**，然后释放。`HookBus.publish` 只对每条 lane 做非阻塞 `offer`，不等消费者。临界区内不持有 inbox 事务、会话 registry 锁或记账锁；不按消费顺序倒推 seq。
- `sequence()` 对外接口保留（`hooks/intercept.py:254` 的 `delivery.prepared`，source `daemon.delivery`）；它与 `_publish` 共用计数器，`sequence()` 在 `_publish_lock` 外调用也只会拿到更大的号，不影响 `daemon.turns` 内的递增。
- 所有 `turn.*`（headless `observe_turn`/`observe_started` 与 interactive `observe_carrier_turn`）只经 `_publish`，source 只有 `daemon.turns`。

### 4.4 窗口覆盖规则（与 WP7 §0.1 的接口）

WP7 I2 用闭区间 `[turn.started.startedAtMs, turn.completed.endedAtMs]` 收 `fetched_at_ms`。**载体必须在对本 turn 将注入的消息做 fetched 读之前报 `turn.started`**，并在收到 daemon 的接受后再做 fetched 读。一条消息一个 turn 且 turn 晚于 fetched 读的载体（codex），`turn.ended` 必须带 `deliveryId`，走 WP7 I1。T8 守护顺序要求。

### 4.5 seq 与纪元：生命周期不变量（A3）

- 不变量：`DaemonHookService` 与 WP7 消费者由 `wire_reply_reminder_hooks` 在同一个 application 上构建，同生同灭。⛔ 不得只重建 emitter 而保留旧 consumer。
- 由此：完整 daemon 重启时新消费者高水位为空，接受从 0 开始的 seq；pi/codex/claude 载体重启不重置 daemon 的 seq。
- 若将来允许单独重建 emitter，必须改用 source 纪元或持久单调 seq，并加旧事件拒绝、新纪元接受的测试。本包不做。
- T6 的重启用例必须关闭旧 bus、用新 consumer 工厂、复用同一 inbox 文件，不得只换一个假事件对象。

## 5. settle 的时机与幂等（全部载体）

- 时机：先报 turn 结束（§4），再结算本 turn 注入的消息。结束报告未确认不阻塞结算，但报告本身由载体持有并重试（wp5c §2.3、§3.2、§4.2）。
- 范围：只问本 turn 注入过的消息（pi 的 outstanding 批、codex 的该 delivery、claude 本 turn `harness_read` 返回的 id、headless 的该 delivery）。以前 turn 被 keep 的消息不再问。
- 照做：`ack` → ack；`keep` → 不发任何消息。⛔ 除 §3.4 两个机制应答函数外，任何载体在任何分支都不调 `message.reply`、不 submit intent 为 reply 的消息（G3、G3p）。
- 只读：`settle_hint` 不改状态；同一 id 重复问，答案只随状态变。
- 幂等：第二次 ack 得 `MESSAGE_ACK_UNAVAILABLE` 按已结算处理。载体记住 `completed`/`kept`，同一会话内对同一 id 只结算一次。
- `settle_hint` 本身失败（传输、`STALE_SESSION`、authority 超时）：记 deferred，按载体现有退避重试；⛔ 不得因失败改为 ack。
- fence：IPC 的 `settle_hint` 必须带有效 `sessionRef`（WP3-A）；headless 进程内直接调 `settle_hint_for`。

## 6. WP5-D：PAC 与 routine 投递带 summary

- `inbox/links/io.py`：`InboxDeliveryIoAdapter.deliver(*, effect_id, sender, target, conversation_id, text, summary: str, expires_at_ms=None)`，`summary` 必填关键字，进入 `InboxMessage(..., summary=check_summary(summary, required=True))`。`user:` 目标走 `_deliver_to_user`，先校验 summary，DM 路径不变（裁定 37）。新增 `@staticmethod derive_summary(text) -> str`（即 `summary_from_text(text)`）。
- `NotificationSender.send`（`pac/graphs/reactor.py` 的 Protocol）加必填 `summary: str`。`DaemonPacNotificationSender.send`、`WorkflowSender.send` 透传；`send_workflow_request` 远端路径不带 summary（跨节点协议不改），由接收节点 `dispatch/remote/__init__.py:748` 的 `deliver` 用 `derive_summary(text)` 生成。
- shell `_WorkflowNotificationSender.send` 只透传收到的 summary；若须派生，只用 `hyprial.daemon` 公共导出的 `summary_from_text`。⛔ shell 不 import `hyprial.daemon.impl`（G6）。
- 调用点（用最终发送的文本派生，同一 idempotency key 重发时 summary 相同）：`reactor.py:483`、`reactor.py:954`、`authority/__init__.py:897`、`operations.py:55`（rework 后缀之后再派生）、`routines.py:79`、`dispatch/remote/__init__.py:748`。

## 7. 测试清单（WP5-A、WP5-D；WP5-C 见 wp5c §6，WP5-B 见 wp5b §3）

运行：`PYTHONPATH=$PWD/src uv run --extra test pytest <路径> -q`。所有新测试**先单独提交并确认失败**，再提交实现。「守护」在实现前即通过，不计入先红证据，也不得记为失败。测试的 home、socket、时钟、inbox 一律隔离在 `tmp_path`。

### 7.1 契约：`tests/daemon/harnesses/carrier/test_carrier_contract.py`

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| K1 | `test_injection_from_row_never_reads_message` | 行带 `message: "BODY-SENTINEL-1"`、`summary: "S"`、`bodyBytes: 15`：`summary == "S"`、`body_bytes == 15`；渲染不含 `BODY-SENTINEL` | `ModuleNotFoundError: …carrier.contract` |
| K2 | `test_render_matches_golden` | golden 每例 `render_injection(injection_from_row(row)) == expected` | 同上 |
| K3 | `test_missing_summary_renders_read_hint_not_body` | summary None：首行 `summary: (none; the sender gave no summary)`，含 `bodyBytes: N` 与 `harness_read_body("<id>")` | 同上 |
| K4 | `test_notice_renders_one_line` | notice：结果恰为 `notice: <text>` | 同上 |
| K5 | `test_settle_injected_obeys_hint_and_is_idempotent` | 假 port {a: ack, b: keep, c: 抛 TimeoutError}：只对 a 调 ack；`SettleOutcome == (("a",), ("b",), ("c",))`；`already={"a"}` 再跑不再调 ack；被调方法名 ⊆ {settle_hint, ack} | 同上 |
| K6 | `test_settle_hint_for_matches_ipc_handler` | 真 `InboxService`：pending request、pending reply、pending event、已 ack、未知 id、他人消息、假 gate 答 ack、假 gate 答非法值八种输入，gate=None 与假 gate 两组下 `settle_hint_for(...)` 与 `handle("message.settle_hint")` 的 `hint` 逐一相等；期望：reply/event 无 gate 也为 ack，他人消息为 ack，非法值为 keep | `ModuleNotFoundError: …inbox.contracts.settlement` |
| K7 | `test_carrier_port_has_no_reply_member` | `CarrierPort` 公开成员恰为 {settle_hint, ack, turn_started, turn_ended}；`IpcCarrierPort` 无名字含 reply 的属性 | `ModuleNotFoundError` |
| K9 | `test_turn_ipc_names_match_router` | `contract.IPC_SESSION_TURN_STARTED`/`ENDED` 都在 router 的已登记方法集合内 | `ModuleNotFoundError` |

### 7.2 边界：`tests/daemon/harnesses/carrier/test_carrier_boundaries.py`

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| G1 | `test_contract_and_settlement_import_only_allowed_modules` | AST：两文件的 import 只在 §2.1 允许表内 | `FileNotFoundError` |
| G2 | `test_links_inbox_application_never_import_carriers` | `links/`、`inbox/`、`application/` 下无 `harnesses.carrier` import | 守护 |
| G3 | `test_only_mechanism_functions_build_a_reply` | AST 扫 `runtime/*.py`、`harnesses/carrier/*.py`：构造 `intent="reply"` 的 `InboxMessage` 只出现在 `_answer_hook_request`、`_answer_mechanism_result` 两个函数体内；这两个函数只在 `_settle_mechanism_answer` 内被调用；`_settle_mechanism_answer` 在 `results.py` 恰被调用 2 次；`"message.reply"`、`_reply_and_ack`、`hyprial-agent-sdk-reply` 零命中 | `AssertionError`：列出 `_reply_and_ack` |
| G7 | `test_contract_names_no_harness` | `contract.py`、`inbox/contracts/settlement.py`、`application/actors/sessions/turns.py` 不含 `pi_interactive`、`codex_interactive`、`claude_interactive`、`claude-channel`、`CARRIER_RUNTIMES` | `FileNotFoundError` |

### 7.3 headless：`tests/daemon/harnesses/carrier/headless/test_headless_carrier.py`

沿用 `tests/daemon/harnesses/actors/test_harness_reply_settlement.py` 的 bridge 组装与真 `InboxService`。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| H1 | `test_completed_request_submits_no_reply` | X→A request r1，COMPLETED "ANSWER"：X 无 reply 行；`inbox.submit` spy 无 intent=reply | `AssertionError`：存在 `uuid5(…hyprial-agent-sdk-reply:r1)` |
| H2 | `test_keep_leaves_request_pending_stamped_and_not_redispatched` | 无 gate：r1 仍 pending、`fetched_at_ms` 非空；再两个 tick 不再派发 | `AssertionError`：r1 已被 ack |
| H3 | `test_ack_hint_acks_without_reply` | 假 gate 恒 ack：r1 被 ack；无 reply 行 | 同 H1 |
| H4 | `test_reply_and_event_rows_are_acked_without_a_gate` | pending 的 reply 行与 event 行 COMPLETED：无 gate 也被 ack（裁定 56） | 守护（今天无条件 ack）；实现中途删分支后由 K6 先红 |
| H5 | `test_dispatched_prompt_is_summary_only` | 派出 `HarnessDelivery.message == render_injection(...)`，不含 `BODY-SENTINEL` | `AssertionError`：message 等于正文 |
| H6 | `test_failed_turn_is_not_stamped_and_retries` | FAILED（未读正文）：`fetched_at_ms` 为 NULL；退避到期再派发 | 守护 |
| H7 | `test_settle_deferral_retries_without_rerunning_the_turn` | 第一步 `fetch_pending` 抛 `InboxAuthorityTimeout`：id 进 `_pending_settle_results`；下一 tick 不派发、只重跑结算；成功后移出 | `AttributeError: '_pending_settle_results'` |
| H8 | `test_python_worker_still_receives_the_body` | python_worker 派发 `message` 等于正文 | 守护 |
| H9 | `test_body_read_then_transient_failure_still_retries` | 参数化 `service`/`authority`：`message.read_body` 盖戳 → transient FAILED → 退避期内重建 bridge 与 inbox → 到期再派发 → COMPLETED keep → 后续三个 tick 不再派发；`harness_failure_settlements` 无非 terminal 行，`harness_failure_attempts` 行数不减 | `AssertionError`：到期未再派发（查询只认 `fetched_at_ms IS NULL`）或 `TypeError: completed_turn` |
| H10 | `test_permanent_failure_is_not_redispatched_after_body_read` | read_body 后 permanent FAILED：之后不派发 | 守护 |

### 7.4 机制应答：`tests/daemon/harnesses/carrier/headless/test_mechanism_answers.py`

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| M1 | `test_hook_request_still_gets_its_mechanism_answer` | 真 `InboxTurnHookInvoker.invoke` + 真 headless delivery/result 路径 + 真 inbox authority；handler 为隔离的确定性测试 connector（把收到的 prompt 原样作为 output）：handler 收到完整事件 JSON；应答在 timeout 前返回；原请求与应答都被清理；`parse_agent_uri(invoker callback actor)[2] == TURN_HOOK_CALLBACK_ACTOR` | 见下「复现先红」 |
| M2 | `test_forged_hook_marker_gets_summary_only_and_no_answer` | 普通 agent 发 payload 带 `turnHookRequest: true` 的 request：派发 message 为渲染块；COMPLETED 后无 reply 行 | `AssertionError`：收到原文 |
| M3 | `test_user_proxy_format_hint_reaches_the_person` | 人经 adapter 发给 user-proxy 缺收件人的消息：user-proxy（python_worker）的 `result` 帧作为 `hyprial-mechanism-reply:` 应答投到 `user:` 路径；人收到的文本含 `MISSING_RECIPIENT_HINT` 原文 | 删除 `_reply_and_ack` 后红（同 M1 的复现顺序） |
| M4 | `test_mechanism_answer_confirm_timeout_retries_same_id_once` | submit 已提交但首次确认抛 `InboxAuthorityTimeout`：下一 tick 同 id 重试，最终只 1 条 reply，worker 未再起 turn；另一例 submit 真未提交时重试后提交 1 条 | `AttributeError`：`_answer_mechanism_result` |
| M5 | `test_mechanism_answer_carries_reply_to_and_summary` | 应答 payload `replyTo == original.message_id`；`summary` 非空且 ≤ 512 字节；conversationId 与原请求相同；id 为确定性 uuid5 | 同 M4 |
| M6 | `test_reply_or_event_to_a_mechanism_gets_no_answer` | 发给 python_worker 的 reply 行与 event 行 COMPLETED：无新 reply，按裁定 56 ack | 同 M4 |

复现先红（B6）：M1、M3 在 base 上通过（机制交换今天靠 `_reply_and_ack` 成功），**记为守护**。实施提交顺序：① 测试提交（M1、M3 绿，其余红）；② 删除 `_reply_and_ack` 的提交（M1 因 handler timeout 红、M3 红，记入 `red.txt`）；③ 机制应答提交（全绿）。不得把 ① 中的守护记为失败。

### 7.5 keep 的恢复与释放：`tests/daemon/harnesses/carrier/headless/test_keep_recovery.py`

| # | 测试名 | 断言 | 先红 / 状态 |
| --- | --- | --- | --- |
| R1 | `test_keep_survives_restart_without_redispatch` | 同一 inbox 文件上关闭并重建 bridge 与 authority：三条 keep 的 request 仍 pending 且 fetched；无 reply；不再派发；新的未 fetched request 正常派发 | `AssertionError`：keep 的被 ack |
| R2 | `test_settle_interruption_resumes_settlement_only` | 参数化三断点：盖戳后、提示返回后、ack 已提交但确认丢失；恢复后只重跑结算，worker turn 计数不变，keep 不被误 ack，第三例最终无异常移出 | `AttributeError: '_pending_settle_results'` |
| R5 | `test_kept_request_holds_the_window_until_explicit_close` | 真 WP2 gate：keep 的请求继续占接收方 W；同对端不带 replyTo 的 send 不闭合它；显式 reply、ack 各只释放对应事项；未准入请求不注入 | **BLOCKED-on-WP2**（未合时 `pytest.skip` 不允许；以 `xfail(strict=True, reason="BLOCKED-on-WP2")` 标记并在验收中列出） |
| R6 | `test_drain_and_abandon_release_kept_requests` | keep 后 link 回收：reply 得 `LINK_RECLAIMED`，原事项释放；放弃阈值与 TTL 到期后容量释放，原请求不被重发；过期行不阻塞新工作 | 同 R5（TTL 部分可先行，为真实断言） |
| R7 | `test_many_requests_do_not_starve_each_other` | 同 actor：request a 结算 deferred、request b keep、reply c、event d、notice e：分别处理；c、d ack（裁定 56）；e 不成为欠回复事项 | `AssertionError` |

R3（pi）、R4（codex）在 wp5c §5。

### 7.6 turn 上报：`tests/daemon/harnesses/carrier/turns/`

`test_turn_reports.py`：真 `DaemonApplication`（沿用 WP7 `tests/daemon/application/messaging/test_reply_reminder_flow.py` 的组装），`daemon_hooks.register_consumer` 挂记录 lane，假时钟。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| T1 | `test_started_and_ended_publish_daemon_stamped_events` | pi_interactive 会话：started（夹带 `seq: 0`、`source: "pi-extension"`、`startedAtMs: 1`）后 ended：两个事件；规范 URI；`source == "daemon.turns"`；时刻等于假时钟，不等于参数 | `DaemonRequestError(METHOD_NOT_FOUND)` |
| T2 | `test_turn_reports_are_fenced` | 过期 sessionRef → `STALE_SESSION`；被取代 → `SESSION_SUPERSEDED`；未注册 → 错误；都不发事件 | 同 T1 |
| T3 | `test_turn_reports_are_idempotent_per_report_id` | 同 reportId 的 started 两次、ended 两次：各一个事件；第二次返回的时刻等于第一次 | 同 T1 |
| T4 | `test_claude_stop_pulse_publishes_completed` | claude_interactive 只带 reportIds 的 ended：发 `turn.completed`；`turnCount` 等原字段不变 | `AssertionError`：没有事件 |
| T5 | `test_failed_status_publishes_turn_failed` | `status: "failed"` → `turn.failed`；非法 status → `INVALID_ARGUMENT` | 同 T1 |
| T7 | `test_interactive_turn_drives_reply_reminder` | started → `pending.list fetched=true` → ended：A 的 system notice 列出 r1；A 显式 reply 后下一 turn 不再提醒 | 同 T1 |
| T8 | `test_started_after_fetch_misses_the_window` | 先 fetched、后 started、再 ended：不提醒 | 守护（随 T1 一起红） |
| T9 | `test_ended_retry_after_lost_response_publishes_once` | daemon 已接受 ended 但响应丢失，载体同 reportId 重试：恰 1 个 `turn.completed`、WP7 恰 1 条提醒、两次返回 `endedAtMs` 相同 | 同 T1 |
| T10 | `test_started_dedup_is_per_session_and_report` | 同 actor：reportId x 的 started 后 y 的 started 不覆盖 x 的时刻；x 的 ended 用 x 的时刻；会话换代后旧 sessionRef 的 started/ended 被拒且不改新会话状态 | 同 T1 |
| K8 | `test_a_new_runtime_needs_no_contract_change` | 用 runtime 名 `zz_test_interactive` 注册 interactive 会话：summary 注入（`render_injection`）、started/ended、settle_hint/ack 全通；未注册或已失效会话被 fence 拒绝 | `AssertionError`：今天 `turn reports require the current claude_interactive session` |

`test_turn_seq.py`：

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| T6 | `test_seq_survives_full_restart` | (a) headless `observe_turn` 与 interactive IPC 交错四次：seq 严格递增、source 同为 `daemon.turns`；(b) 第一个 application 发到 seq ≥ 50 后关闭（关旧 bus），新 application 用新 consumer 工厂、复用同一 inbox 文件；A 有 pending r1：started → fetched → ended 后 A 有一条提醒，日志无 `reply_reminder.stale_replay` | 同 T1 |
| T11 | `test_concurrent_publishes_keep_seq_order_per_lane` | 双线程 barrier：在 seq 分配与 `bus.publish` 之间强制交错（monkeypatch 构造钩子），同 actor 两个 `turn.completed`：记录 lane 的 seq 递增；两个 deliveryId 都进入 WP7 判定；无 stale_replay | `AssertionError`：lane 观察到 `[1, 0]` |
| T12 | `test_mixed_emitters_under_threads_stay_monotonic` | 4 线程混合 headless observer、interactive IPC、`delivery.prepared` intercept 各 100 次：每条 lane 上 `daemon.turns` 的 seq 严格递增 | 同 T11 |

### 7.7 WP5-D：`tests/daemon/inbox/io/test_inbox_io_summary.py`

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| S1 | `test_deliver_requires_summary` | 无 `summary` → `TypeError`；`""` → `SUMMARY_REQUIRED`；513 字节 → `SUMMARY_REQUIRED`，`data == {"summaryBytes": 513, "maxBytes": 512}` | `AssertionError`：未抛 |
| S2 | `test_delivered_row_carries_summary` | 落库行与 pending.list 行 `summary == "PAC S"` | `TypeError: unexpected keyword argument 'summary'` |
| S3 | `test_pac_senders_pass_a_deterministic_summary` | 规划发送与 `resend_undelivered` 对同一 `(event_id, edge)` 的 summary 相同且等于 `summary_from_text(text)`；`DaemonPacNotificationSender` 原样透传 | `TypeError` |
| S4 | `test_routine_and_remote_paths_pass_summary` | routines 投递与 `dispatch/remote` offer 投递的 inbox 行 summary 非空 | `AssertionError`：None |
| S5 | `test_user_target_keeps_the_dm_path` | `user:` 目标走 `deliver_user`，不写 inbox 行；summary 仍校验 | 校验部分同 S1 |
| G6 | `test_shell_workflow_sender_uses_public_face` | AST：`shell/impl/cli/commands/workflow/control.py` 不 import `hyprial.daemon.impl` | 守护 |

### 7.8 需改写或删除的现有测试（WP5-A）

实施前在实施 base 上重跑，按实际失败清单改；本分支可定位的：

- 改写：`tests/daemon/harnesses/actors/test_harness_reply_settlement.py`（4 条）、`tests/daemon/turns/test_consumed_result_settlement.py`（改名）、`tests/daemon/delivery/dedup/test_reply_dedup_outlives_the_retry_schedule.py`、`tests/integration/e2e/e2e006/test_e2e006_deepseek_approval.py`。
- §9.3 实测的 9 条：`test_daemon_agent_sdk.py::test_daemon_dispatches_pending_delivery_then_replies_and_acks_result`、`test_forward_seam.py::test_an_ordinary_result_still_replies_to_the_sender`、`test_harness_reply_settlement.py` 三条、`test_agent_send_idempotency.py::test_retried_send_yields_exactly_one_reply_end_to_end`、`test_progress_end_to_end.py::test_worker_progress_reaches_sender_without_becoming_a_prompt`、`test_progress_publish.py::test_terminal_result_still_uses_the_authoritative_reply_path`。
- 现有 `session.turn.ended` 测试中断言 `turn reports require the current claude_interactive session` 的改为 K8 的语义。
- 门禁数据：§1.4 两个文件。

## 8. 验收（WP5-A、WP5-D；可机检）

在各自分支 worktree 根目录执行，全部满足才算通过。

1. WP5-A：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses/carrier -q`：通过；以下 grep 计数成立：`grep -rhoE "def test_[a-z0-9_]+" tests/daemon/harnesses/carrier | sort -u | wc -l` ≥ 46（K1–K9 共 9，G1–G3、G7 共 4，H1–H10 共 10，M1–M6 共 6，R1、R2、R5、R6、R7 共 5，T1–T12 共 12；参数化只计一次）；R5、R6 的 BLOCKED 部分只以 `xfail(strict=True, reason="BLOCKED-on-WP2")` 出现：`grep -c 'BLOCKED-on-WP2' tests/daemon/harnesses/carrier/headless/test_keep_recovery.py` ≥ 2。
2. WP5-D：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/inbox/io -q` 通过，S1–S5、G6 共 6 条存在。
3. 两个子包都跑：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses tests/daemon/turns tests/daemon/delivery tests/daemon/pac tests/daemon/inbox tests/daemon/links tests/biz/hooks tests/daemon/application tests/tooling/architecture -q` 全部通过。
4. `uv run --with libcst==1.9.0 pytest tests/` 报真实结果；无关失败逐条列出并附实施 base 上同样失败的证明；缺 Node 24、GUI 依赖或 pi SDK 时报 BLOCKED。
5. `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `… check_module_boundaries.py` 输出 `"violations": []`；`uv run ruff check .` 输出 `All checks passed!`。
6. WP5-A 以下 grep **无输出**：
   - `grep -rn "_reply_and_ack\|hyprial-agent-sdk-reply\|_reply_already_answered\|_reply_already_settled\|_pending_reply_results" src/hyprial`
   - `grep -rn '"message.reply"' src/hyprial/daemon/impl/runtime src/hyprial/daemon/impl/harnesses/carrier`
   - `grep -rn "CARRIER_RUNTIMES\|claude_interactive\|pi_interactive\|codex_interactive" src/hyprial/daemon/impl/harnesses/carrier/contract.py src/hyprial/daemon/impl/inbox/contracts/settlement.py src/hyprial/daemon/impl/application/actors/sessions/turns.py`
   - `grep -rn "turn reports require the current claude_interactive session" src/hyprial`
   - `grep -rn "harnesses.carrier" src/hyprial/daemon/impl/links src/hyprial/daemon/impl/inbox src/hyprial/daemon/impl/application`
   - `git diff --name-only <base>..HEAD -- src/hyprial/daemon/impl/links src/hyprial/kernel src/hyprial/biz src/hyprial/shell src/hyprial/daemon/impl/harnesses/pi src/hyprial/daemon/impl/harnesses/codex src/hyprial/daemon/impl/mcp src/hyprial/daemon/impl/application/wiring/transport.py`
7. WP5-A 以下 grep **恰有输出**：
   - `grep -c "def _answer_hook_request\|def _answer_mechanism_result" src/hyprial/daemon/impl/runtime/forwards.py` → `2`
   - `grep -c "_settle_mechanism_answer(" src/hyprial/daemon/impl/runtime/results.py` → `3`（一处定义、两处调用）
   - `grep -c "fetched_at_ms IS NULL" src/hyprial/daemon/impl/inbox/service/projection.py` 与 `…/inbox/authority/read.py` → 各 ≥ 1，且同文件 `grep -c "s.terminal = 0 AND s.next_attempt_ms <= ?"` ≥ 1
   - `grep -n "completed_turn" src/hyprial/daemon/impl/inbox/contracts/ports.py` → 1 行
   - `grep -n "def settle_hint_for" src/hyprial/daemon/impl/inbox/contracts/settlement.py` → 1 行；`grep -c "settle_hint_for" src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py src/hyprial/daemon/impl/runtime/results.py` 两文件各 ≥ 1
   - `grep -n "_publish_lock" src/hyprial/daemon/impl/hooks/service.py` → ≥ 2 行（定义与使用）
   - `grep -c '"operation": "fetch_pending"' tests/inbox_authority_exemptions.json` 比 base 多 2
8. WP5-D：`grep -n "summary: str" src/hyprial/daemon/impl/inbox/links/io.py` 位于 `def deliver(` 参数内；`wc -l < src/hyprial/daemon/impl/dispatch/remote/__init__.py` ≤ 1000；`git diff <base>..HEAD -- src/hyprial/daemon/impl/dispatch/remote/__init__.py | grep -c '^+[^+]'` → `1`；`grep -rn "hyprial.daemon.impl" src/hyprial/shell/impl/cli/commands/workflow/control.py` 无输出。
9. 预算：`wc -l` 满足 §1.4/§1.5；`find src/hyprial/daemon/impl/harnesses/carrier -maxdepth 1 -type f | wc -l` → 4；`find src/hyprial/daemon/impl/inbox/contracts -maxdepth 1 -type f | wc -l` → 5；`find src/hyprial/daemon/impl/application/actors/sessions -maxdepth 1 -type f | wc -l` → 4。
10. 先红证据：测试提交早于实现提交；在测试提交上跑第 1、2 条，失败形状与 §7 一致（守护除外）；M1/M3 按 §7.4 的三段提交顺序取证。
11. PR 正文含 `Contract-Impact:` 与 `Skill-Impact:`（§10），`scripts/verify/check_contract_impact.py` 通过；列出净增的 authority 豁免条目与理由。

证据（只追加，`docs/evidence/org-structure-constrain-2026-10-14/wp5a/` 与 `wp5d/`）：`red.txt`、`green.txt`、`gates.txt`（结构门、边界门、ruff、第 6–9 条输出）、`turn-events.json`（T1、T9 记录的事件全文）、`blocked.txt`（R5、R6 与真实 TUI 联测）。

## 9. 依赖与冲突

### 9.1 依赖

| 依赖项 | 来自 | 用处 |
| --- | --- | --- |
| pending.list 行 `kind`、`summary`、`bodyBytes` | WP3-A | 注入（C） |
| `message.settle_hint` 与 fence；`_ipc_message_settle_hint` | WP3-A | 判定换成 `settle_hint_for`（A）；C 的结算 |
| `message.read_body` 及其单条 `fetch_pending` | WP3-A | 注入里指给 agent 的路径；§3.3 的事实前提 |
| `fetch_pending(recipient, *, message_id=…)`、`FetchPendingCommand.message_id` | WP3-A | §3.3 在其上加 `completed_turn` |
| `inbox.contracts.summary` 与 `hyprial.daemon.summary_from_text` | WP3-A | 渲染、机制应答 summary、PAC |
| `InboxMessage.summary` 三表持久化；`DaemonApplication._link_gate` | WP3-A | headless 注入；IPC 端门 |
| `LinkAdmission`、准入后派发查询、内部 actor 保留名与注册守卫 | WP4（含返工 2、裁定 63） | 门的访问器；§3.4 身份判定的安全前提 |
| `harness_read_body` 工具 | WP6（与 WP3-A 同批，裁定 41） | 注入末行指向的工具 |
| 真 gate | WP2 | R5、R6 |

### 9.2 v1 冲突与裁定落点（历史；正文 v2 已按 §9.4 写）

| # | 问题 | 裁定 | 正文落点 |
| --- | --- | --- | --- |
| C1 | 仍 pending 的 reply/event 行答 keep 会堆积 | 56：答 ack | §2.4 规则 2；H4、K6 |
| C2 | interactive codex 无回复通道 | 57：WP5-B 挂 thread-scoped MCP | wp5b.md |
| C3 | hook handler 交换依赖自动回复 | 58：机制应答 `_answer_hook_request` | §3.4、§3.5；M1、M2 |
| C4 | python_worker 与 user-proxy 结果即回复 | 59：`_answer_mechanism_result` | §3.4；M3 |
| C5 | python_worker 注入范围 | 60：照传原文 | §3.5；H8 |
| C6 | headless keep 的持久事实 | 61：`fetched_at_ms` 盖章 + 两条豁免 | §3.2、§3.3 |
| C7 | README 行 5 两项是同一函数 | 62：已更正 | 结论 1 |

附带边界（C6，保留）：某 actor 先被 interactive 会话 fetched 但未结算、之后改为 headless 运行时，这些行不再被 headless 派发，只在 pending 与 W 计数里，直到 agent 处理、回收或放弃阈值。

### 9.3 实测：删除 headless 自动回复会碰到的现有测试（v1 记录）

2026-10-14 在 scratch 副本上把 `_reply_and_ack` 换成只 ack，与 `0ec2bd8fe` 各跑 `uv run --extra test pytest tests/daemon tests/integration tests/shell -n 8 -q`：原分支 8 failed、10011 passed、12 skipped；副本 69 failed、9944 passed、16 skipped、8 errors。差集 61 条中 52 条为副本环境造成（无独立 .venv、计时比例断言），与删除回复直接相关的 9 条列在 §7.8。该实验只模拟 ack-only，不覆盖 keep、pi、codex、`tests/biz`、`tests/kernel`、`contract/`，不能作为 keep 的验收（B9）。

### 9.4 集成者裁定（2026-10-14，README 裁定 56–62）

- C1 采纳替代：`settle_hint_for` 对 `intent != "request"` 且仍 pending 的行答 `ack`；ports `SettleHint` 注释已改；H4 改为「无 gate 也被 ack」；WP3 T15 的 reply 行用例由本包补。
- C2 不选 b。采纳 a，单列子包 WP5-B（interactive codex 挂 thread-scoped MCP，`capabilities.py` 两项改 native，加 interactive 线程调 `harness_reply` 的测试）。本包仍删除 `codex/settlement.py` 的自动回复；X2、G3 按正文；集成分支在 WP5-B 合入前不合 dev。
- C3 采纳替代：hook 请求豁免两条，走 `_answer_hook_request`；实施前先补真 headless handler 的复现测试（先红）。
- C4 采纳替代：python_worker `result` 帧经 `_answer_mechanism_result` 回请求方；加 `test_user_proxy_format_hint_reaches_the_person`。
- C5 确认：只注入 summary 限于模型上下文，python_worker 照传原文，H8 保留。
- C6 采纳 `fetched_at_ms` 盖章与两条 authority 豁免，不用内存集合。
- C7 README 已更正。
- 本节与正文不一致处以本节为准；§7 测试清单按上述增补（C1 +1、C3 +1、C4 +1），实施节点在实施前把 §7 表更新到位。（v2 注：已在正文 v2 中更新到位，不再需要实施节点改表。）

### 9.5 v2 残留（评审未并入、解释性落地、新冲突）

评审 9 条阻断的事实依据均已对代码复核成立，无驳回项。以下为并入时需集成者知悉或裁定的点；正文按现行裁定与本节的落地方式写。

1. **B1 依据的提交号已过期**：评审引用 WP3-A `4eb154e1a`；当前 `wp3/send-links` 为 `b616e4a58`，`queries.py:448–450` 仍对单条调用 `fetch_pending`，结论不变。
2. **B4 的 turn 开始信号**：正文用 Claude Code `UserPromptSubmit` hook 调用现有 `hyprial.biz.impl.turn_pulse`（只换 `--signal-dir`），不改 biz。channel 推送触发的模型 turn 是否触发 `UserPromptSubmit` 为 UNKNOWN；正文以 `harness_read` 同步补报 start 兜底（wp5c §4），真实 TUI 联测 BLOCKED-on-WP8。若集成者要求更强的开始信号，需另改 hook 配置或 biz 模块。
3. **B7 第 4 点对 pi 的「进程恢复保留未确认报告」**：pi 扩展没有 durable 存储；扩展进程重启即重新注册、得到新 sessionRef，旧报告必被 fence 拒绝（`SESSION_SUPERSEDED`/`STALE_SESSION`），持久化后也只能丢弃。正文规定 pi 在进程内保留并重试，扩展重启时的丢失在 R3/P10 明确记录，不冒充 headless 保证。若集成者仍要求持久：pi 在会话状态目录写 `turn-reports.json`，重启后对新会话不重放、只记诊断（改动约 +40 行 TS 与一条测试）。
4. **B6 的身份判定依赖裁定 63**：保留名注册守卫在 WP4 返工 2 中；守卫合入前，任何人可注册 `_turn-hooks` 名伪造 hook 请求。正文把 WP4 返工 2 列为 WP5-A 的合入前置（§1.1）。
5. **B5 去掉 runtime 白名单后的副作用**：`_record_interactive_turn_reports` 的计数从只记 claude 扩到所有 interactive 会话；`top` 的显示仍只对 claude（非目标，不改）。无冲突，仅告知。
6. **`capabilities.py:167` 的归属**：v1 让 WP5 改注释、WP5-B 改同一处为 native；v2 全部归 WP5-B，WP5-C 不碰，避免同行冲突。
7. **授权豁免净增 +4**：裁定 61 只批了 `fetch_pending` 两条；B6 要求机制应答保留 submit 与 ack，需要两个新 owner 各两条，替代删除的 `_reply_and_ack` 两条（净 +2）。这是裁定 58/59 的直接后果，正文照写；若集成者要求不增 owner，替代是机制应答继续用 `_reply_and_ack` 原名原 owner（但 G3 与验收第 6 条需改为允许该名）。
8. **WP3-B 的前置**：README 写「WP3-A、WP5、WP6 合并后」。v2 建议解释为 WP5-A、WP5-C、WP5-D；WP5-B 不阻塞 WP3-B，只阻塞合 dev（裁定 57）。待集成者确认。
9. **裁定 57 的「thread-scoped」可能不可直接实现**：interactive codex 的 thread/start 由 TUI 经 `--remote` 发出，不经 hyprial；sessionRef 在 app-server 启动后才发现（`sessions.py::_start_interactive_codex`）。wp5b §1 设第 0 步实测：方案 T（carrier 对已发现线程 `thread/resume` 带 MCP config）可行则照裁定；只有方案 P（app-server 进程级 `config_args` + 延迟读取 sessionRef）可行时，先报集成者确认再实施。

## 10. Skill-Impact 与 Contract-Impact（交 WP8）

- Skill-Impact: updated（交 WP8）。hyprial-ops skill：载体只注入 summary；正文用 `harness_read_body` 或 `hyprial read`；请求不会被自动回复，必须 `reply` 或 `ack`；轮末未回复的请求会收到提醒（WP7）。pi 与 codex interactive 说明删去「轮末自动回复」。
- Contract-Impact（各子包 PR 正文写此行；`contract/` 同步由 WP8 承担）：
  - 新 IPC `session.turn.started`；`session.turn.ended` 新增可选 `status`、`deliveryId`，接受任何当前 interactive 会话，返回体新增 `endedAtMs`；按 `(actor, sessionRef, reportId)` 幂等。
  - `turn.started`/`turn.completed`/`turn.failed` 覆盖 interactive 载体，`collectionMethod` 为 `daemon`、`fidelity` 为 `observed`、`sender`/`conversation`/`promptExcerpt`/`outputExcerpt` 为空串。
  - 注入文本格式（§2.3）是新的人读约定，golden 在 `tests/daemon/harnesses/carrier/injection_golden.json`；WP8 决定是否搬进 `contract/`。
  - wire golden `WG-001` 的 `autoReply: true` 字段名与新语义不符；本包不改，WP8 决定。
  - headless 回复 id 前缀 `hyprial-agent-sdk-reply:` 不再出现；机制应答改用 `hyprial-turn-hook-reply:`、`hyprial-mechanism-reply:`，payload 带 `replyTo` 与 `origin.mechanism`。
  - `message.settle_hint` 对仍 pending 的 reply/event 行答 `ack`。
  - PAC 投递行开始带 summary（`contract/pac/context/README.md` 中「PAC 投递 summary 可能为 null」由 WP8 删除）。

## 11. 评审（2026-10-14，pi / gpt-6.1-sol）

已并入正文 v2，见各小节

**BLOCKING:9 ADVISORY:4。规格需要修改。**

评审基准：本分支 `064ee4352`、设计 §4.5、§7、§8.1、§8.2、§11、README 裁定 1–62、ports.py，以及当前产品代码。§9.4 的 7 项裁定均接受，不重复请求裁定。下列修正规格落实这些裁定；未列出的要求仍按正文与 §9.4 执行。

### 11.1 阻断项与修正规格

#### [阻断 B1] fetched 戳不能单独决定失败重试资格

- 位置：§3.1 的 `AND fetched_at_ms IS NULL`、H6、§9.4 C6。
- 依据：WP3-A 分支 `4eb154e1a` 的 `queries.py::_ipc_message_read_body` 在返回正文前调用单条 `fetch_pending`。因此，模型按提示取正文时就已经盖戳。当前 `results.py::_settle_failed_result` 把可重试失败写入 `harness_failure_settlements`。
- 问题：模型取正文后失败，原规格的查询永远排除该行。退避到期也不能重试。H6 只测未读取正文的失败，不能发现此错误。裁定 61 要求持久去重，但没有要求丢弃失败重试。

**修正规格：**

1. 保留 `fetched_at_ms` 作为首次注入/读取的持久事实。不得清空或改写首次时间戳。
2. 两份 `dispatchable_messages` 查询必须保留现有 TTL、terminal 与 backoff 过滤。额外条件改为：`fetched_at_ms IS NULL`，或该行有 `terminal = 0` 且 `next_attempt_ms <= now_ms` 的 durable 失败重试记录。只盖 fetched 戳的普通 pending 行不能满足第 2 个分支。
3. 成功 headless 结果必须在结算前消除旧的非 terminal 重试资格。扩展单条 `FetchPendingCommand`/`fetch_pending`，增加可选 `completed_turn: bool = False`。仅 headless 成功结果传 `True`。同一 inbox 事务内盖首次 fetched 戳，并删除该 id 的非 terminal `harness_failure_settlements` 行。保留 append-only `harness_failure_attempts`，不得删除 terminal 证据。公共读取与 read_body 继续用默认 `False`。
4. 以上动作必须经过 inbox authority。§3.1 的两条 `fetch_pending` 豁免仍是裁定 61 的两条，不增加绕过 authority 的 SQL 写入。
5. 两个结果路径都必须执行同一成功转换。转换超时后只重试结算，不重跑模型。成功 keep 后，即使此前有失败记录，也不能再次派发。
6. 增加 H9：真正文读取盖戳 → transient FAILED → 退避到期 → 重试成功 keep → 后续 tick 不再派发。分别覆盖 service 与 authority read projection，并在退避期间重建 bridge/inbox 验证恢复。另测 permanent FAILED 不再派发。

所需文件补入 §1：inbox `contracts/api.py`、`contracts/ports.py`、`authority/facade.py`、`actor/custody/lifecycle.py`。两份查询与 WP3-A 的单条 fetch 参数必须一起更新。

#### [阻断 B2] codex 的 KEPT 会被现有 CarrierSettled 删除

- 位置：§3.3 的“store 置 KEPT，然后 CarrierSettled 照发”、X3、§1.3。
- 依据：`carrier/interactive_carrier_runtime.py:241–252` 的 `_settled` 无条件调用 `store.settle_and_delete`。`_recover` 只认识 FETCHED、TURN_STARTED、FINAL_OBSERVED、REMOTE_SETTLED、SETTLED。`CodexCarrierStore.load` 跨 session 只加载 FINAL_OBSERVED/REMOTE_SETTLED。
- 问题：照规格增加 store 状态但不改状态所有者，KEPT 会立即消失。重启或换 session 后，去重也失效。`kept_ids(actor, session_ref)` 不能解决同 actor 换 session 的问题。

**修正规格：**

1. 在 `interactive_carrier_runtime.py` 新增 generation-fenced 的 `CarrierKept` 命令。该命令由 carrier actor 写 KEPT、清 final_output，并释放运行中投影。不得先由适配器写 KEPT 再发会删除行的 `CarrierSettled`。
2. `CarrierSettled` 保持仅用于 ack 已确认的清理。keep 的状态转换、写库和释放投影必须属于同一个 actor 命令。
3. KEPT 去重按 `(actor, message_id)`，不按 sessionRef。新 session 必须继承同 actor 的 KEPT id。恢复时 KEPT 不生成 enqueue/supply-final 效果，不恢复为 FINAL_OBSERVED。
4. `known` 不仅用于 `_apply_inbox_list`，也必须用于 `_list_may_hold`。仅有 KEPT pending 行时，poll lane 可进入 pending.wait，不能持续全量 list。
5. 只有权威 pending.list 成功且确认 id 消失后，才能删除对应 KEPT 行。传输失败、`ok:false` 或消息数组无效时不能删除。pending.wait 未返回全量列表时不能当作消失证据。
6. X3 增加 3 个断点：写 KEPT 后 actor 清理前、carrier 进程重启、同 actor 换 sessionRef。每个断点后仍为 pending、无自动回复、无新 turn；显式 reply/ack 后 KEPT 能清理。

§1.3 必须补入 `carrier/interactive_carrier_runtime.py`（本分支 530 行，实施后 ≤1000）。不能把 store 当作另一个 turn 状态所有者。

#### [阻断 B3] 旧 FETCHED 行原文注入一次违反硬切

- 位置：§3.3 的“升级前已落库、状态为 FETCHED 的旧行仍带正文，按原样注入一次”。
- 依据：设计 §7 的 summary-only 与 §11 的硬切。裁定 58/60 的正文例外仅限 hook 机制与 python_worker，不包含旧 codex 模型上下文。

**修正规格：**

1. 删除旧 FETCHED 行原文注入的例外。codex store 的模型注入文本必须带可识别的格式版本。
2. 恢复未开始 turn 的旧 FETCHED 行时，必须先从 daemon 的 metadata 投影取得 summary/bodyBytes，再按 §2.3 重建注入文本并持久化。不得把旧 `message` 当作 summary，也不得由旧 body 派生 summary。
3. metadata 暂不可用时保留 FETCHED 并重试，不启动 turn。metadata 已确认不可读时，不注入旧 body；用无 summary 的固定提示或权威终态清理该行。
4. 旧 TURN_STARTED 行可对已经运行的 turn 查状态，但不得重发旧正文。旧 FINAL_OBSERVED 行只按 settle_hint 结算，不提交已保存的最终文本。REMOTE_SETTLED 只做本地清理。
5. 新增 X6：用升级前 store schema/状态建立包含 BODY-SENTINEL 的 FETCHED、TURN_STARTED、FINAL_OBSERVED 行，再启动新 carrier。所有新注入均不含 sentinel，所有结算均无 message.reply。

#### [阻断 B4] claude interactive 未满足完整载体契约

- 位置：§3.4 的“不改代码”、§4.3、T4。
- 依据：`mcp/channel/adapter.py:344` 只发 Stop reportIds，没有 start；没有轮末 settle_hint。WP7 `reminder/consumer.py::_window` 在既无 deliveryId、又无 started/last_end 时返回 None。
- 问题：会话第 1 个 turn 即使读了 pending 请求，也不会提醒。首次 fetched 戳不再更新，这批请求在后续窗口也可能永远不提醒。收到的 reply/event 也没有轮末 ack 机会。设计 §8.1 与裁定 11 要求载体上报开始和结束。

**修正规格：**

1. claude channel 必须增加薄适配。模型 turn 开始时上报 `session.turn.started`，Stop 时上报 `session.turn.ended`。使用同一 turn reportId；不得把 SessionStart/compact 当成模型 turn。
2. 在 `shell/impl/cli/commands/agent/runtime/claude.py` 的会话级 hook 配置增加 turn-start 信号。channel 转发信号时必须保持 start/end 次序。若 fetched 工具读先于 start 信号到达，`ClaudeChannelAdapter.harness_read` 必须在 fetched 读前同步补报同一 turn 的 start。
3. adapter 必须记录本 turn `harness_read` 实际返回的注入 id。Stop 上报完成后，对这些 id 调 settle_hint；ack 时调用 message.ack，keep 不做写入，异常留到重试。门铃观察与 pending.wait 不计为注入。
4. 请求显式 reply/ack 后，轮末重复询问必须幂等。notice 无欠回复；无正文可读的 notice 不加入需要保留的请求集合。
5. 增加 C1-test：新会话第 1 个 turn，start → 真 fetched 读 → Stop，能提醒未回复请求。增加 C2-test：同一 turn 包含 request/reply/event 时只按 daemon 提示 ack，且没有代答。增加 C3-test：start 信号延迟到 fetched 读之后，adapter 的同步补报仍覆盖该行。

§1 的范围与预算补入 claude hook 配置、`mcp/channel/adapter.py`、`loops.py` 及对应测试。WP7 消费者不改，不能用已有 last_end 的 fixture 掩盖首 turn 缺口。

#### [阻断 B5] 公共 turn IPC 仍按 harness 名分支

- 位置：§2.1、§2.2 的 `CARRIER_RUNTIMES`，§4.1 的 runtime 白名单，T4。
- 问题：新增 harness 仍须修改 contract.py 的白名单，公共 IPC 根据 `pi_interactive/codex_interactive/claude_interactive` 决定准入。daemon 的载体契约漏入了具体 harness 名，违背“新 harness 只接线”。此外，将 inbox 的 settle_hint 判定放在 `harnesses/carrier`，让消息权威入口反向依赖载体目录。

**修正规格：**

1. 从 `contract.py` 删除 `CARRIER_RUNTIMES`。turn IPC 只核实可信 caller、当前 interactive session 及 sessionRef fence。若需能力检查，消费会话接线时注册的通用载体能力，不在 IPC 或 link/inbox 判定中枚举 harness 名。
2. `CarrierPort`、Injection 与渲染/结算驱动继续保持 harness-neutral。具体 runtime 名、启动参数、MCP 配置与 hook 信号格式仅属于各自适配器或 composition 注册。
3. `settle_hint_for` 移到 `daemon/impl/inbox/contracts/settlement.py`。IPC 端与 headless 端调用同一纯判定。该函数按权威消息元数据执行：非 pending → ack；pending 且 intent 非 request → ack；其余无 gate → keep；有 gate 则调用 gate，非法答案 → keep 并记录警告。不得相信载体自报的 intent。
4. 保持 ports 名称与 `LinkGate.settle_hint` 签名不变。判定模块只读，不引入 harness 类型。
5. K6 增加 gate=None 下 request/reply/event 的一致性断言，并覆盖外人 id 的现行身份规则。G1/G2 的允许依赖按此修改。
6. 增加 K8：接入一个测试载体，用未出现在 3 个现有 harness 名中的 runtime 名。通过可信接线建立 interactive session 后，完成 summary 注入、turn 上报与结算。测试不修改 contract、links、inbox 的逻辑。未注册或已失效的 session 仍被 fence 拒绝。

本分支 `inbox/contracts/` 有 3 个直接文件；WP3-A 增 summary.py 后为 4 个，本条增 settlement.py 后为 5 个，未超预算。

#### [阻断 B6] 机制应答例外的身份、关联与重试规格不完整

- 位置：§9.2 C3/C4 被 §9.4 采纳的替代文字、§5、G3、§8.6。
- 依据：`turn_delivery/protocol.py::is_hook_request` 当前只查 payload 的 `turnHookRequest: true`，不核实 sender。`_harness_kind` 返回 harness 名，如 `user-proxy`；返回值不包含执行机制 `python_worker`。
- 问题：仅以 `delivery.hook_request` 授权会让普通请求伪装成 hook，获得全文注入与自动应答。按 `harness_kind == "python_worker"` 实施又会漏掉 user-proxy。删除旧回复重试判定后，新机制函数的 durable 超时、summary 与 replyTo 尚无明确规格。G3 的零 `intent="reply"` 要求也与保留机制应答冲突。

**修正规格：**

1. `_answer_hook_request` 与原文注入的条件必须相同：权威 original 为 request、`is_hook_request(original)` 为真、sender 的规范 agent URI 的 actor 段为保留名 `_turn-hooks`。不能只信 payload 标记或载体布尔值。
2. python_worker 例外按可信启动声明的执行机制判断，使用 `support(harness, headless=True, capability=HEADLESS_EXEC).mechanism`。不得按 harness 名猜测。`user-proxy` 的 result 帧必须进入该分支；forward 帧继续走原 forward 路径。
3. 两个例外只在 completed、非 forward、非已处理 workflow outcome 的分支执行。普通模型结果始终走 settle_hint，不使用最终文本。reply/event 行不得触发新的机制应答链。
4. 新应答必须带确定性 messageId、`replyTo=original.message_id`、由输出确定性派生的合法 summary，并保持原 conversationId。先确认 durable submit，再 ack 原请求。确认未知时保留同一结果/同一 id 重试，不重跑 handler 或脚本，不改成 ack。必须区分已提交后的确认超时与真正未提交。
5. G3 改为函数级允许表：只有 `_answer_hook_request`、`_answer_mechanism_result` 能构造机制 reply；只允许上述受保护的调用点。普通模型路径的 message.reply 与旧 `hyprial-agent-sdk-reply` 前缀仍为零。§5 的无回复要求增加这 2 个机制例外，§8.6 同步调整。不得整文件豁免。
6. 裁定 58 的复现必须使用真 `InboxTurnHookInvoker.invoke`、真 headless delivery/result 路径与真 inbox authority；handler 可为隔离的确定性测试 connector，不能用直接把 reply 塞进 inbox 的假 handler。验证 handler 收到完整事件 JSON，应答在 timeout 前返回，原请求与回执都清理。另测普通 sender 伪造 hook 标记时仍只收到 summary，且不生成自动回复。
7. 增加 submit 已提交但首次确认超时的机制应答测试。重试只产生 1 条 reply，不再启动 turn。`test_user_proxy_format_hint_reaches_the_person` 必须穿过 python_worker 与用户投递边界，并检查人的提示原文。

复现先红的形状必须诚实记录：原 base 的机制交换本来就成功。先保留正常路径作守护，再在隔离复现中移除旧模型代答路径，证明 handler 会 timeout；加入新机制应答后恢复通过。不得把 base 上通过的守护测试记录为失败。

#### [阻断 B7] turn 结束上报失败后没有重试所有者

- 位置：§3.2 的 endTurn、§3.3 的“上报失败只记日志，不阻塞结算”、§5。
- 问题：pi 清空 openTurn 后，轮末结算重试未承诺重发 ended。codex 只记日志后释放 carrier 状态。一次传输故障就可能永久丢失 turn.completed，WP7 无法提醒。claude 现有 Stop 信号则会保留到上报成功，这 3 个载体的语义不一致。

**修正规格：**

1. 所有载体必须独立保存未确认的 turn report，并以同一 sessionRef/reportId 重试。结算可继续，不得把未确认的结束报告与 outstanding/final 状态一起丢弃。
2. start 与 end 的发送顺序按 reportId 保持。start 未确认时不得改用另一个 id。end 报告第一次被 daemon 接受时固定 endedAtMs；确认丢失后的重复报告不得发布第 2 个事件，也不得把时刻改成重试时刻。
3. daemon 的 started 去重按 `(actor, sessionRef, reportId)`，不能只保存“最近一次 started 时刻”。结束报告必须关联对应 start。重复 start 不得覆盖当前另一 turn 的窗口，注销/换代后旧报告不能改变新 session。
4. 已获确认的报告可移除。明确 SESSION_SUPERSEDED 的报告结束重试并记诊断；传输或 authority 确认未知时继续退避重试。进程恢复必须保留载体的未确认报告/信号，不另建业务 turn 权威。
5. 新增 T9/P9/X7：daemon 已接收 ended 但首次响应丢失；重试仅有 1 个 completed，WP7 恰有 1 条提醒。另测 ended 在到达 daemon 前失败后能重试，以及 carrier 恢复后未确认报告不丢失。

#### [阻断 B8] seq 分配锁不能保证发布顺序

- 位置：§4.4、T6。
- 依据：`hooks/service.py::_publish` 在 `sequence()` 返回后才调用 bus.publish。`sequence` 只锁自增。kernel HookBus 不按 seq 重排事件。
- 问题：线程 A 取得 seq=0 后暂停，线程 B 取得 seq=1 并先发布，A 随后发布 0。WP7 会把 A 的合法完成事件当 stale。单线程交错 4 次的 T6 不能证明真实并发时的严格递增。

**修正规格：**

1. 所有 daemon `turn.*` 必须由同一发布路径分配 seq。序号分配与 bus 入队必须处于同一发布临界区，保证每条订阅 lane 看到的 daemon.turns 序号递增。
2. 临界区仅包含序号分配和非阻塞 bus.offer，不等待消费者执行，不持有 inbox 事务或会话 registry 锁。不得用 bus 消费顺序倒推修改 seq。
3. T6 增加双线程 barrier 测试：同 actor 的 2 个 completed 事件在分配与发布之间强制交错。记录 lane 的 seq 必须递增，2 个 deliveryId 均进入 WP7 的判定，不能出现 stale_replay。再与 interactive IPC/headless observer 混合运行。

完整 daemon 重启的高水位判断见 A3。此项修复同一纪元内乱序，不必修改 WP7。

#### [阻断 B9] keep 的积压、释放与恢复验收缺失

- 位置：H2/H7、P7、X3、§7.8、§9.3 未覆盖项。
- 问题：2 个 tick 的假 gate 测试不能证明 pending/W 长期占用，也不能证明重启后没有放大。§9.3 的 ack-only 实验明确不能作为 keep 的验收。

**修正规格：**

在 §7 增加下表中的命名场景。测试必须使用隔离 home、socket、时钟与 durable inbox：

| 新场景 | 必须断言 |
| --- | --- |
| R1 headless keep 后重启 | 同一 inbox 文件上关闭并重建 bridge/authority；成功 keep 的多个 request 仍 pending/fetched，无自动 reply、无再次派发；新增未 fetched 请求仍能派发 |
| R2 结算中断 | 分别在成功盖戳后、hint 返回后、ack 已提交但确认丢失时恢复；只重试结算，不重复模型 turn，不误 ack keep |
| R3 pi 的两类重启 | 仅 daemon epoch 改变时按 P7 允许 1 次 rewake；同 epoch 反复 poll 不重复注入。扩展进程重启后的 pending 保留与提醒行为必须明确记录，不能冒充 headless 的零再派发保证 |
| R4 codex 恢复 | B2 的 3 个断点，外加只有 KEPT 行时的 pending.wait/有界轮询。暂时不可见或失败的 list 不能删除去重记录 |
| R5 pending 与 W | 真 gate 下成功 keep 的请求继续占接收方 W；向同一对端不带 replyTo 的 send 是新请求，不能闭合旧请求；显式 reply、ack 分别只释放对应事项；未准入请求不注入 |
| R6 排干与放弃 | keep 后 link 回收，reply 得 LINK_RECLAIMED，原事项释放；放弃阈值与 TTL 到期后 pending/容量可释放，不能自动重发原请求；过期行不再阻塞新工作 |
| R7 多条请求不互相饿死 | 同 actor 的 2 条 request 一条 deferred、一条 keep，另有 reply/event；结算分别处理，reply/event 按裁定 56 ack，notice 不变成欠回复事项 |

R5/R6 在 WP2 未合时明确写 BLOCKED-on-WP2，不得用恒定假 gate 的通过代替。WP5 可先提交，但第一片集成验收必须执行这些场景。测试与验收清单按命名场景核对，不能只数“46 条”。

### 11.2 建议项

#### [建议 A1] 原预算读数可信，新增修正必须重算

本次核对了 §1.3/§1.4 的现行数，文件与目录读数相符。`dispatch/remote/__init__.py` 确为 999 行；原方案加 1 行即 1000 行，没有后续增长余量。表中 WP3-A/WP4 的未来数只能在实施 base 上复测。

宜在实施前更新表：补入 B1 的 authority 参数链、B2 的 carrier runtime、B4 的 claude 适配、B5 的 neutral settlement 模块，以及新增测试文件。新增测试可拆到子目录，不得超过 7 个直接文件或 1000 行。remote 文件仍只加那 1 行；依赖合入后若无空间，按 §1.3 停下报告，不压缩代码或自行拆文件。

#### [建议 A2] 五层依赖没有现成越层，机检宜覆盖实际调用方

当前拟定的 contract→kernel、contract→daemon 内部协议均未跨五层。application→harnesses 是同 daemon 域内的职责倒置，B5 给出了修正规格。这种依赖不属于 kernel/biz 越层。

shell 的 `_WorkflowNotificationSender` 必须只透传传入 summary。若 shell 需要派生，使用 `hyprial.daemon` 公共导出的 `summary_from_text`，不能 import `daemon.impl.inbox.contracts.summary`。biz 提醒消费者继续只用 `hyprial.daemon` 公共面。宜为本次改动的 shell/biz 文件增加 import 守护，并保留结构门、模块边界门。ports.py 不改名、不复制。

#### [建议 A3] 完整 daemon 重启能清高水位，但必须保留同生同灭前提

§4.4 的选择在当前接线下成立：完整 application 重建时，DaemonHookService 与新建的 WP7 消费者都重建；高水位为空。仅重启 pi/codex source 不会重置 daemon 的 seq，因此不会卡死。统一盖 seq 不等于任何 source 重启都安全。

宜把前提写成生命周期不变量：不得只重建 emitter 并保留旧 consumer。若未来允许单独重建 emitter，必须改用 source epoch（或持久单调 seq）并测试旧事件拒绝、新 epoch 接受。T6 的重启用例必须关闭旧 bus、使用新 consumer 工厂，并复用真实 inbox 状态；不能只换一个 fake event 对象。

#### [建议 A4] 测试编号与 WP5-B 的交付边界宜明确

§9.4 优先级已解决 C1–C7，不再把正文的旧 H4/G3 规则作为验收。宜在实施测试提交中统一更新 §7/§8 的名称、数量、允许表与先红形状。

WP5-B 仍是独立交付门，不因 WP5 自动回复已删除而视为完成。除 interactive `harness_reply` 外，宜测同一 thread-scoped MCP 的 read_body、ack、summary 参数与 session fence。capabilities 的 native 声明必须有线程工具调用证明。真实 codex/pi TUI 未实测时仍记 BLOCKED-on-WP8，不由 unit/build 代替。

### 11.3 自动回复调用审计

命令：`grep -RnE 'message\\.reply|reply_and_ack|_settle_final|intent[[:space:]]*=[[:space:]]*"reply"|"intent":[[:space:]]*"reply"' src/hyprial --exclude-dir=__pycache__`。另查 `.reply(`、机制 result/forward 路径。

| 命中路径与调用 | 判定与动作 |
| --- | --- |
| `runtime/results.py:260,415` → `forwards.py:717::_reply_and_ack` | 同一个 headless 自动回复函数的 2 个调用点；普通模型路径删除。裁定 58/59 的 2 个机制路径改为 B6 的专用函数 |
| `pi_harness_attach.ts:480::settleMessage`；`failOutstanding` → `completeOutstanding` → `settleMessage` | 正常最终文本、空输出与 abort 的 failure 文本共用此代答路径；全部删除 reply 分支，失败不能改为代答 |
| `codex/carrier.py:640::_settle_finals` → `settlement.py:162::_settle_final` | 第 3 处模型代答；删除 message.reply，保留按 hint 结算 |
| `codex/settlement.py:251::_settle_reply_unavailable` 与其中 6 个 `method="message.reply"` | 同一回复路径的判定/日志/重试参数，不是 6 个新投递入口；删除回复专用判定，保留 ack 证据处理 |
| `shell/.../message/reply.py:34`、`mcp/server.py:165`、`mcp/channel/adapter.py:334`、`pi_harness_bridge.ts:128` | agent 显式 reply 工具；保留，summary 与工具参数由 WP6 负责 |
| `ipc/router.py:106,555`、`application/.../inbox_surface/send.py:655`；send.py:362 | IPC 登记/路由与显式 reply/replyTo 构造；保留，不是载体轮末代答。replyTo 校验与关联按裁定 39/55 |
| `correlation/availability_loud.py:179`；runtime 的 `_submit_loud`、`_loud_harness_failure` | 自动发送的失败/无进展诊断通知；按 §1.2 保留。输出来自失败事实，不闭合原请求 |
| `adapters/lark/outbound/sdk.py:670::_client.im.v1.message.reply` | Lark 平台的线程投递 API；不是 message.reply 载体代答，保留 |
| `adapters/lark/inbound/pipeline.py`、`inbound/reconcile.py` 的 `message.reply_to` | 收到的人类线程引用与路由元数据，无自动 Harness reply 调用，保留 |
| `inbox/tracking/progress.py`、`runtime/settlement.py`、inbox authority/custody 注释 | 文档或旧 helper 的引用；无新发送入口。旧 `_reply_and_ack` 的注释按新契约更新 |
| `ipc/wire.py` 的 errorReply、kernel 的 reply id 与 worker-channel method 名 | wire 错误关联/词汇表；无载体自行发送，保留。Contract-Impact 由 WP8 核对 |

结论：3 处模型代答删除点完整。仓库仍有系统诊断通知、机制交换和平台 reply API，不能用全仓“reply 零命中”证明正确。新增机制应答必须按 B6 限定，不能重新包装成普通模型自动回复。

### 11.4 验证记录与边界

- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/test_wp0_seams.py tests/biz/hooks/reminder tests/daemon/application/messaging/test_reply_reminder_flow.py -q`，28 passed，2 条既有依赖 deprecation warning。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`，均为 `violations: []`。此结果只证明本分支 base 结构，不证明未来实施预算。
- 隔离代码复现（`PYTHONPATH=$PWD/src uv run --extra test python`，内存 port/bus，无生产状态）：首个无 start/deliveryId 的 completed 不查请求；同 consumer 在 seq=50 后拒绝同 source 的 seq=0；新 consumer 接受 seq=0；在 sequence 返回前用双线程 barrier 暂停旧线程，实际发布顺序为 `[1, 0]`。这些读数分别支撑 B4、A3、B8，不是 WP5 实施通过证据。
- PASS：`git diff --check` 无输出；`uv run ruff check .` 为 `All checks passed!`。仅追加本评审节，产品源码未修改。
- UNKNOWN：WP5 的新载体行为未实施，本次未跑新规格焦点测试或全量产品测试。真 headless hook handler 的删除复现必须按 B6 补齐；没有用假 handler 的通过冒充裁定 58 的证据。
- BLOCKED：WP3-A/WP2 接线后的正文读取失败重试、真实 W 计数、放弃阈值与排干验证；真实 pi/codex TUI 联测。以上不得计为 PASS。

### 9.6 集成者对 §9.5 残项的裁定（2026-10-14，README 裁定 64–66）

- 残项 3：接受，pi 不持久化未确认上报（裁定 64）。
- 残项 7：接受净增 4 条 authority 豁免。
- 残项 8：接受，WP3-B 不等 WP5-B（裁定 66）。
- 残项 9：按裁定 65 执行——WP5-B 先探针，app-server 级挂载可接受，条件见裁定；探针结论写进 wp5b.md 后直接实施。
- 残项 4：WP5-A 合入以 WP4 返工 2（裁定 63 注册守卫）为前提，已写入 README 工作包表。
- 其余残项（1、2、5、6）为事实说明，无需裁定。
