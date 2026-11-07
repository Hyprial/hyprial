# WP4：接收方兜底——ingress 的 LINK_UNKNOWN 与 dispatch 的互引/W 准入（实施规格）

- 日期：2026-10-14
- 段：设计（claude / claude-opus-5-5）
- 设计依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §3.2、§4.1、§4.5、§4.6、§5.3、§5.6、§6、§14
- 缝：`src/hyprial/daemon/impl/links/ports.py`（WP0）。本包只调用 `LinkGate.sender_known` 与 `LinkGate.admit_dispatch`，不调用 `admit_send`、`settle_hint`，不 import `links.service` / `links.store` / `links.policy`。
- 分支：`wp4/send-links`，PR 目标 `refactor/org-structure-constrain`。
- 基线（2026-10-14，本 worktree）：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/inbox/admission tests/daemon/delivery/message/test_message_pending_wait.py tests/daemon/delivery/terminal/test_delivery_terminal_state.py -q` → `63 passed`。

## 0. 范围与非范围

范围：
1. ingress：agent 间消息到达接收方 daemon 的 inbox 提交前，调 `sender_known(recipient=, sender=)`；False 拒收，不落 inbox 行，写 delivery_status `state=refused, reason=LINK_UNKNOWN`。
2. dispatch：消息进入 harness 上下文前，调 `admit_dispatch(recipient=, sender=, message_id=, now_ms=)`；False 时消息留在 inbox、不注入、不计"已见"，下次评估再问。两个入口：headless 派发（`DaemonEventBridge._dispatch_harness_deliveries`）与 interactive 的可见集合（`message.pending.list`、`message.pending.wait`）。
3. 注入点与缺省：定义 `LinkAdmission`（一个持有可选 `LinkGate` 的槽），由 composition 创建一次、分给 inbox service、bridge 与 application；WP2 只做 `bind(service)`。

非范围：发送方 `message.send` 的铸造与 hold（WP2/WP3）；`LinkGate` 的真实实现（WP2）；`summary` 与 `settle_hint`（WP3）；carrier 改造（WP5）；`top` 的"入站等待注入"列（WP6）；`contract/` 同步（WP8，本包只在 PR 写 `Contract-Impact`）。

## 1. 判定：哪些消息过闸

一个纯函数决定一条消息是否受 link 机制管辖，ingress 与 dispatch 共用，放在 `dispatch/admission.py`：

```
link_governed(sender, recipient, intent) -> bool
```

全部满足才为 True：
- `parse_agent_uri(sender)` 与 `parse_agent_uri(recipient)` 都非 None（kernel 函数；只认 `agent:<owner>:<machine>:<actor>` 四段）。
- `sender != recipient`（自发不需要 link）。
- sender 不是 daemon 机制身份：actor 段等于 `DISPATCH_SERVICE_ACTOR_NAME`（`dispatch-coordinator`），或以 `_` 开头（如 `_turn-hooks`）。这些身份没有 link 文件，过闸会把跨机 PAC 派单与 turn hook 全部拒掉。
- `intent != "system"`。

由此得到的豁免与设计的对应：

| 发送方 | 是否过闸 | 依据 |
| --- | --- | --- |
| `user:<owner>`（owner 到 agent） | 否 | §3.2：owner link 不写 orgfs，不经接收方 W 准入 |
| `adapter:lark:<name>`、`channel:…`（route 入站） | 否 | §4.1：route 不属于本机制 |
| `system:hyprial:<node>`、system notice、progress 事件 | 否 | 走 `receive_system_notice`、`receive_progress_event`，本来不经 `receive` |
| `dispatch-coordinator`、`_turn-hooks` | 否 | 机制身份，见上 |
| 其他 `user:`（非 owner 的人） | 否（本包） | §4.1 要求按 agent 规则处理，但人没有 link 文件，ingress 无法判"从未加入"。留作 §8 开放项，评审决定是否在 WP2 的 `sender_known` 里处理 |
| `agent:` → `agent:` | 是 | §4.6 |

dispatch 处另有两条只在 dispatch 生效的豁免：
- hook 请求（`turn_delivery.protocol.is_hook_request(message)` 为 True）：机制自身的请求，今天已经越过 FIFO 头阻塞（`reconcile.py` 的 `actor_held` 例外），link 闸同样放过。
- `intent == "reply"`：见 §1.1。

### 1.1 回复为什么在调用点豁免 W

§4.5 规定回复"总是放行、不占 link 名额、不计入 cwnd，并闭合那个未闭合事项"。W 计的是"入站未回 + 出站未收到回复"。如果回复也要过 `admit_dispatch`，一个 W 被出站请求占满的节点会拒绝注入那些正好能闭合出站事项的回复，形成死锁。`admit_dispatch` 的签名只有 `message_id`，没有 `intent` 与 `reply_to`；links 包按 §14 不 import inbox，无法从 id 反查这是不是回复。所以本包在调用点对 `intent == "reply"` 不调 `admit_dispatch`。

回复仍然过 ingress 的 `sender_known`：一个从未加入 org 空间的发送方不能靠标 `intent=reply` 绕过 ingress。

代价：一个已加入、但互引尚未同步的发送方，可以把新请求标成 `reply` 来绕过 dispatch 的互引等待。缓解见 §9 缝建议 1。

## 2. 改哪些文件

所有路径相对 `src/hyprial/daemon/impl/`。行数是 2026-10-14 的现值；"直接文件"按结构门计（含 `__init__.py`、json）。

### 2.1 `dispatch/admission.py`（93 行 → 约 230 行）：槽与判定

`dispatch/` 已有 7 个直接文件（`__init__.py`、`admission.py`、`alarm.py`、`dispatch-policy.json`、`identity.py`、`matrix.py`、`workflow_observation.py`），不能加文件。§14 指定 dispatch 准入落在 `admission.py`，所以新代码放在这里。`links/` 将由 WP1/WP2 填满 7 个文件（`__init__`、`ports`、`model`、`controller`、`policy`、`store`、`service`），本包不往里加。

新增，`__all__` 同步：
- `link_governed(sender: str, recipient: str, intent: str) -> bool`：§1 的判定。只依赖 `hyprial.kernel.parse_agent_uri` 与 `dispatch.identity.DISPATCH_SERVICE_ACTOR_NAME`。
- `class LinkAdmission`：
  - `__init__(self, *, logger: Callable[..., None] | None = None)`。
  - `bind(self, gate: LinkGate) -> None`：`isinstance(gate, LinkGate)` 为 False 时 `TypeError`；已绑定再绑 `RuntimeError`（只绑一次，防两个 service 争写）。
  - `bound: bool` 只读属性。
  - `ingress_refusal(self, *, sender, recipient, intent) -> str | None`：不受管辖返回 None，不调 gate。未绑定返回 None，并记 `warn daemon link.gate_unbound`（按 recipient 限频，每个 recipient 每 60 s 最多一条）。gate 抛异常返回 None，记 `warn daemon link.gate_error`（带 `stage="ingress"`）。`sender_known` 返回 False 时返回 `ipc_errors.LINK_UNKNOWN`。
  - `dispatch_admitted(self, *, sender, recipient, intent, message_id, now_ms, hook_request: bool) -> bool`：不受管辖、`hook_request`、`intent == "reply"` 返回 True，不调 gate。未绑定返回 False，记 `link.gate_unbound`。gate 抛异常返回 False，记 `link.gate_error`（`stage="dispatch"`）。否则返回 `admit_dispatch(...)` 的结果。每个 message_id 第一次被拦时记一次 `info daemon dispatch.link_held`，带 recipient、sender、messageId；由拦到放时记一次 `info daemon dispatch.link_admitted`，带 `heldMs`。这份去重表有上限（4096 项，超出按插入序淘汰），不进持久层。
- 只 import `hyprial.kernel`、`hyprial.daemon.impl.links.ports`（`LinkGate`）、`dispatch.identity`。不 import inbox（`inbox/service` 要 import 本模块，反过来会成环）。

### 2.2 缺省行为（未注入 LinkGate）与论证

"未注入"指槽存在但没有 `bind`。规定：

| 位置 | 未绑定时 | gate 抛异常时 |
| --- | --- | --- |
| ingress（受管辖消息） | 接收并落 inbox（不拒） | 同左 |
| dispatch（受管辖消息） | 不注入，留 inbox | 同左 |
| 不受管辖消息 | 不受影响 | 不受影响 |

论证：
1. 要守的性质是"没有 link 的 agent 消息不进 agent 的 context"（§0、§4.6），不是"不进 inbox"。context 的入口在 dispatch，所以 fail-closed 放在 dispatch。
2. ingress 拒收是终态声明：`refused` 写进 delivery_status，并通知发送方"永不会自愈"（§4.6）。缺 gate 是本机的接线状态（启动顺序、WP2 尚未合并、接线缺陷），可以自愈；用它支撑一个"永不自愈"的终态声明是错的。所以 ingress 只在 gate 明确返回 False 时拒。
3. 留 inbox 不丢消息：行受现有 hold TTL 约束（`HoldPolicy`，默认 12 h），到期写 `expired`，与 §4.3 的"等待有上限时长"一致；gate 绑上后下一次评估自动放行。
4. owner、route、system、机制身份都不受影响，人仍能和 agent 说话（§5.6 优先级反转的缓解在缺省状态下同样成立）。
5. 只有一种缺省，不留"未传槽 = 不设闸"的旁路：`DaemonEventBridge`、inbox service、application 的槽参数缺省都是一个新的未绑定 `LinkAdmission()`，不是 None。

后果：WP4 合入后、WP2 合入前，集成分支上 agent 间消息会进 inbox 但不派发。这是硬切（R14）的一部分，集成分支不发版。现有测试中凡经 bridge 或 application 派发 agent→agent 消息的，都要显式绑定一个放行的假 gate（§4.1 `PermissiveLinkGate`），见 §3 T13。WP2 合入后，未加入 org 空间的测试 daemon 的真实 gate 会让 `sender_known` 返回 False，这些测试同样需要 link 前提，所以这一步不是额外负担。

### 2.3 `inbox/service/core.py`（660 行）：持槽

- `InboxService.__init__` 加关键字参数 `link_admission: LinkAdmission | None = None`，存为 `self._link_admission = link_admission or LinkAdmission(logger=logger)`。
- `DeliveryCustodyCoordinator` 已经把 `service_options` 原样转给 service，不改 coordinator。
- `consumption_state`：`record.state is TerminalState.REFUSED` 时返回 `ConsumptionState.REFUSED`。

### 2.4 `inbox/service/delivery/receive.py`（217 行 → 约 290 行）：ingress 闸

`receive()` 的判定顺序改为：bare sender → 去重（dedup 表命中、terminal 重复）→ **link ingress** → 容量 → 插入。去重放在前面，是为了让已提交消息的重发总能拿到幂等 ACK，不被后来的判定改写。

在 `with self._db:` 事务内、两段去重检查之后、容量检查之前：

```
code = self._link_admission.ingress_refusal(sender=message.sender, recipient=message.recipient, intent=message.intent)
if code is not None:
    return self._refuse_link_unknown(message, now)   # 事务内写 terminal，事务外发通知
```

新方法 `_refuse_link_unknown(message, now_ms) -> ReceiveResult`，仿 `_reject_bare_sender`：
- 不写 inbox 行，不写 dedup 行，不签 receipt。发送方的未签收重试照常进行，每次重新评估；如果对方之后加入了 org，后续重试能被接收。
- 在同一事务内 `self._record_terminal(message, state=TerminalState.REFUSED, reason=HoldReason.LINK_UNKNOWN, now_ms=now)`。holder 是本节点 `node_id`。
- 事务外发一条 system notice 给发送方：`message_id = uuid5(NAMESPACE_URL, "hyprial:link-unknown:<原 id>")`，`idempotency_key = "link-unknown:<原 id>"`，payload 为 `{"message": "...", "originalMessageId": <id>, "reason": "LINK_UNKNOWN", "systemNotice": true}`。投递目标规则与 `_reject_bare_sender` 相同：先用 `origin_node`，再看发送方是否在线，都不行则落本地。确定性 id 让重试在接收端去重，不堆积。
- 记 `warn daemon inbox.ingress_refused`，带 code、sender、recipient、messageId。
- 返回 `ReceiveResult(message.message_id, accepted=False, acknowledged=False, code=ipc_errors.LINK_UNKNOWN)`。

`receive.py` 只在 `TYPE_CHECKING` 下 import `LinkAdmission`，运行时只用 `self._link_admission`。

`gate.sender_known` 在 inbox actor 的串行线程里、SQLite 事务内被调用。对 WP2 的要求：它只读内存里的本地副本索引，不做 I/O、不阻塞（§9 缝建议 3）。

### 2.5 `inbox/links/pull.py`（765 行 → 约 790 行）：refused 终态

- `TerminalState.REFUSED = "refused"`。模块 docstring 的"两种终态"改为三种，并写明 refused 只由接收方写、只表示 ingress 拒收。
- `HoldReason.LINK_UNKNOWN = "LINK_UNKNOWN"`（值等于 `ipc_errors.LINK_UNKNOWN`）。
- `record()` 的 upsert 升级规则从"只允许 expired → fetched"改为按 `expired < refused < fetched` 只升不降：同一 message_id 已有 refused，又来一条 fetched（对方加入 org 之后，重试被接收）则覆盖；refused 重复写是 no-op，保留第一次拒收的时刻。
- 合并排序 `_precedence(record)`（现为 2/1/0 三档）改为四档：接收方观测的 fetched 为 3，镜像的 fetched 为 2，refused 为 1，expired 为 0。依据：一条后来被接收的消息，"落地"比"曾被拒"更接近事实；refused 是接收方的一手观测，比发送方只看到"没收到 ACK"的 expired 信息量大。
- `DeliveryStatus.from_json` 不改；旧版节点解码 `"refused"` 会抛 `ValueError`，被 `status_queries` 计入 `undecodable` 并告警。硬切，接受，写入 PR 的 Contract-Impact。

### 2.6 `inbox/service/state.py`（16 行）与 `inbox/authority/read.py`（669 行）

- `ConsumptionState.REFUSED = "refused"`。
- `read.py` 的 `consumption_state` 现在把非 fetched 的状态一律映射为 EXPIRED，改为显式三分：fetched → CONSUMED，refused → REFUSED，expired → EXPIRED。

### 2.7 带过滤的拉取：`fetch_pending(only=...)`

`message.pending.list` 带 `fetched=true` 时，现在调 `fetch_pending(actor)`，把该 actor 所有未消费行都盖上 `fetched_at_ms`，并为它们回收发送方的 outbox 行。被 link 拦下的行不能盖这个戳：§7 规定"读取是消费动作，记录 fetched"，而 `inbox/tracking/watchdog.py` 用这个戳判断"有没有人拉过"。

在整条 actor 链上加一个可选参数 `only: frozenset[str] | None = None`（None 等于今天的行为）：
- `inbox/contracts/ports.py`：`FetchPendingCommand` 加字段 `only: frozenset[str] | None = None`。
- `inbox/contracts/api.py`：协议 `fetch_pending(recipient, *, now_ms=None, only=None)`。
- `inbox/authority/facade.py`：透传。
- `inbox/actor/custody/lifecycle.py`：透传给 service。
- `inbox/service/projection.py`：SQL 结果按 `only` 过滤后再盖戳、再回收 receipt；`only` 为空集时不写任何行。

### 2.8 `runtime/reconcile.py`（534 行）与 `runtime/bridge.py`（634 行）：headless 派发

- `bridge.py`：`DaemonEventBridge.__init__` 加关键字参数 `link_admission: LinkAdmission | None = None`，存为 `self._link_admission = link_admission or LinkAdmission(logger=logger)`。
- `reconcile.py` 的 `_dispatch_harness_deliveries`：在 `for message in messages:` 循环里，放在 `pending_result_ids` 与 `_pending_reply_results` 两个 `continue` **之后**、`is_retry` / `_note_delivery_seen` **之前**：

```
if not self._link_admission.dispatch_admitted(
    sender=message.sender, recipient=message.recipient, intent=message.intent,
    message_id=message.message_id, now_ms=now, hook_request=hook_request,
):
    continue
```

  `now` 在每个 actor 循环开始时取一次 `self._clock_ms()`，复用 `dispatchable(recipient, now_ms=...)` 那次的值。

  放在这个位置有三个理由：
  1. 被拦的消息不能进 `_note_delivery_seen`，否则可用性看门狗会把它报成"送出去了却没进展"（`availability.report_stalled`）。
  2. 被拦的消息不能进 `delivery_hooks.prepare_delivery`，否则 hook 会为一条不会派发的消息先跑一遍。
  3. 用 `continue` 而不是置 `actor_held`：一个发送方在等互引，不应堵住同一接收方来自其他发送方的消息（§5.6 队头阻塞）。W 满时 gate 对所有受管辖消息都返回 False，效果相同；owner 消息照常通过（§3.2）。

- system notice 循环不过闸（notice 不受管辖）。

### 2.9 `application/messaging/inbox_surface/queries.py`（511 行 → 约 550 行）：interactive 可见集合

- `_ipc_message_pending_list`：
  - 非 fetch 分支：对 `pending_messages(key)` 的结果用 `self._link_admission.dispatch_admitted(...)` 过滤，`hook_request=is_hook_request(item)`，`now_ms=self._clock_ms()`。
  - fetch 分支：先 `pending_messages(actor)`，过滤得到放行 id 集，再 `fetch_pending(actor, only=frozenset(ids))`。不改 `fetch_receipt_publisher` 的逻辑（它只对返回的行发 receipt hint）。
  - notices 不过滤。
  - 返回结构不变，不加新字段。
- `_ipc_message_pending_wait` 的 `unseen()`：`pending_messages(key)` 也用同一过滤。不加的话，一条被拦的消息每次都算"未见"，doorbell 立刻返回 `changed=True`，carrier 列表却是空的，形成忙轮询。

application 上没有统一的 `_clock_ms`（只有 `agent_bot_attempts.py` 自己的一个），`queries.py` 用 `time.time_ns() // 1_000_000` 取 `now_ms`，与 `authority/read.py` 的写法一致。

### 2.10 `application/wiring/transport.py`（982 行）：composition 注入

在构造 `DeliveryCustodyCoordinator` 之前：

```
self._link_admission = LinkAdmission(logger=self._logger)
```

- `service_options` 加 `"link_admission": self._link_admission`。
- 构造 `DaemonEventBridge` 时传 `link_admission=self._link_admission`。
- 净增不超过 6 行（到 988 行左右）。若超过 1000 行，把这三处收进 `wiring/graph.py`（53 行）里的一个小函数，不拆 `transport.py` 其他内容。

`application/__init__.py`（936 行）加只读属性 `link_admission -> LinkAdmission`（3 行），作为 WP2 的绑定点：`app.link_admission.bind(link_service)`。WP4 不调 `bind`。

### 2.11 不改的

`links/ports.py` 与 `links/__init__.py` 不改；`application/ports.py` 不改（那里的端口用于 daemon 向上取 biz 能力，`LinkGate` 是 daemon 域内的缝，由 WP0 定在 `links/ports.py`）；`delivery/sends.py` 的 `_require_deliverable_send_target` 不改（它是发送方 daemon 的地址形状闸，§4.6 的发送方先拒由 WP2/WP3 在 `inbox_surface/send.py` 实现）。

### 2.12 结构门核对

| 目录 | 现直接文件数 | WP4 后 |
| --- | --- | --- |
| `daemon/impl/dispatch/` | 7 | 7（只改 `admission.py`） |
| `daemon/impl/inbox/service/delivery/` | 5 | 5 |
| `daemon/impl/runtime/` | 6 | 6 |
| `daemon/impl/application/messaging/inbox_surface/` | 5 | 5 |
| `tests/daemon/links/admission/`（新） | 0 | 7（`__init__.py` + 6 个测试） |
| `tests/support/links/`（新） | 0 | 2（`__init__.py`、`gate.py`） |

`tests/daemon/links/` 本层只有 `__init__.py` 与 `test_wp0_seams.py`，WP1/WP2 会在这一层加测试，所以 WP4 的测试放子目录 `admission/`，不占本层名额。所有改动文件都低于 1000 行（最大是 `transport.py`，约 988 行）。

## 3. 测试清单

焦点命令：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/admission -q`。

### 3.1 假实现：`tests/support/links/gate.py`

- `ScriptedLinkGate`：四个方法都实现，因此 `isinstance(x, LinkGate)` 为 True。`known: set[str]` 决定 `sender_known`；`admit: dict[str, bool]`（按 message_id）与 `default_admit: bool` 决定 `admit_dispatch`；`calls: list[tuple[str, dict]]` 记录每次调用与参数；`raise_on: set[str]` 让指定方法抛 `RuntimeError`。`admit_send` 返回 `Allow()`，`settle_hint` 返回 `"keep"`，WP4 不用这两个。
- `PermissiveLinkGate(ScriptedLinkGate)`：全部 known、全部 admit。只给现有测试做显式前提，见 T13。

### 3.2 用例

所有 T 先红的形状以 WP4 改动前的代码为准。"先红"要在 PR 里贴出实际输出（§5）。

| # | 路径 | 测什么 | 先红时的失败形状 |
| --- | --- | --- | --- |
| T1 | `tests/daemon/links/admission/test_link_admission_slot.py::test_fakes_satisfy_the_runtime_checkable_protocol` | `isinstance(ScriptedLinkGate(), LinkGate)` 与 `isinstance(PermissiveLinkGate(), LinkGate)` 为 True；一个只实现 `sender_known` 的对象为 False；`LinkAdmission().bind(<后者>)` 抛 `TypeError` | `ImportError: cannot import name 'LinkAdmission' from 'hyprial.daemon.impl.dispatch.admission'` |
| T2 | 同文件 `::test_link_governed_matrix` | 参数化：agent→agent True；`user:<owner>`→agent、`adapter:lark:x`→agent、`channel:lark:x`→agent、`system:hyprial:n`→agent、`dispatch-coordinator`→agent、`_turn-hooks`→agent、agent→自己、agent→`user:o`、`intent="system"` 均为 False | `ImportError`（`link_governed` 不存在） |
| T3 | 同文件 `::test_unbound_slot_defaults` | 未绑定：受管辖消息的 `ingress_refusal` 为 None、`dispatch_admitted` 为 False；不受管辖消息的 `dispatch_admitted` 为 True；记录到的日志含 `link.gate_unbound`，同一 recipient 连续 10 次只记 1 条 | `ImportError` |
| T4 | 同文件 `::test_gate_errors_hold_but_never_refuse` | `raise_on={"sender_known","admit_dispatch"}`：ingress 返回 None，dispatch 返回 False，各记一条 `link.gate_error`，`stage` 分别为 ingress、dispatch | `ImportError` |
| T5 | 同文件 `::test_bind_once_and_call_shape` | 第二次 `bind` 抛 `RuntimeError`；绑定后 `dispatch_admitted` 把 recipient、sender、message_id、now_ms 原样传给 `admit_dispatch`（以 `calls` 断言）；`hook_request=True` 与 `intent="reply"` 不调 gate | `ImportError` |
| T6 | `tests/daemon/links/admission/test_ingress_link_unknown.py::test_unknown_sender_is_refused_and_recorded` | 真实 `InboxService`（tmp sqlite），`link_admission` 绑 `ScriptedLinkGate(known=set())`。`receive(agent→agent)`：返回 `accepted=False, acknowledged=False, code="LINK_UNKNOWN"`；`has_received(id)` 为 False；`delivery_status` 中该 id 为 `state="refused", reason="LINK_UNKNOWN", holder=<node_id>`；`consumption_state(id) == ConsumptionState.REFUSED`；发送方收到一条 notice，`reason == "LINK_UNKNOWN"`，id 是确定性的 | `TypeError: __init__() got an unexpected keyword argument 'link_admission'` |
| T7 | 同文件 `::test_refusal_is_idempotent_and_heals` | 同一消息再投两次：notice 只有一条（id 确定），refused 记录的 `recordedAtMs` 不变；把发送方加进 `known` 后再投：`accepted=True`，状态升为 `fetched`（refused → fetched 升级） | 同 T6 |
| T8 | 同文件 `::test_owner_route_system_and_mechanism_bypass_ingress` | 参数化：`user:<owner>`、`adapter:lark:x`、`dispatch-coordinator`、`_turn-hooks` 发来的消息，在 `known=set()` 下都 `accepted=True`；`calls` 中没有任何 `sender_known`。另：`receive_system_notice` 照常落库 | 同 T6 |
| T9 | 同文件 `::test_known_sender_and_duplicates_pass` | `known={sender}` 时 `accepted=True`，并且调过一次 `sender_known(recipient=, sender=)`；先在 known 下接收一次，把 known 清空后重投同一 id：仍然是 `duplicate=True, acknowledged=True`（去重先于 link 闸） | 同 T6 |
| T10 | `tests/daemon/links/admission/test_dispatch_link_hold.py::test_held_message_stays_then_releases` | 构造 `DaemonEventBridge` 的最小 fake（仿 `tests/daemon/dispatch/runtime/test_dispatch_runtime_state_owner.py` 的组装），`link_admission` 绑 `ScriptedLinkGate(default_admit=False)`，inbox 有一条 agent→agent 消息。连跑 3 次 `_dispatch_harness_deliveries()`：harness 的 dispatch 从未收到；`pending_messages` 仍含该行；`_attempt_generation` 与可用性时钟里没有它；`calls` 里 `admit_dispatch` 每个 tick 一次。然后令 `admit[id]=True` 再跑一次：被派发一次，日志里有一条 `dispatch.link_admitted`，带 `heldMs` | `TypeError: ... unexpected keyword argument 'link_admission'` |
| T11 | 同文件 `::test_one_held_sender_does_not_block_others_or_owner` | 不用 offer coordinator（`_dispatch_offers is None`，走 `harnesses.dispatch` 分支，一个 tick 可派多条）。inbox 依次是 A（agent→agent，被拦）、B（`user:<owner>`→agent）、C（另一 agent→agent，放行）、H（hook 请求，sender 为 agent）：一个 tick 后派发集合恰为 {B, C, H}，A 不在其中 | 同 T10 |
| T12 | 同文件 `::test_unbound_bridge_holds_agent_mail_only` | 不传 `link_admission`：agent→agent 不派发，`user:<owner>`→agent 照常派发；日志有 `link.gate_unbound`。另：`intent="reply"` 的 agent→agent 在绑定 `default_admit=False` 的 gate 时仍被派发，且 gate 没被调 | 改动前：agent→agent 被派发（断言"未派发"失败：`assert dispatched == []` 不成立） |
| T13 | `tests/daemon/links/admission/test_pending_list_link_filter.py::test_list_hides_held_and_fetch_does_not_stamp_them` | 用 `tests/daemon/delivery/message/test_message_pending_wait.py` 的 `_app` 组装 application，`app.link_admission.bind(ScriptedLinkGate(admit={held: False}, default_admit=True))`。投两条 agent→agent（held、ok）：`message.pending.list` 只返回 ok；`fetched=true` 加 sessionRef 时，`has_fetched(ok)` 为 True、`has_fetched(held)` 为 False；令 held 放行后再 list，返回 held | `AttributeError: 'DaemonApplication' object has no attribute 'link_admission'` |
| T14 | 同文件 `::test_wait_does_not_ring_for_held_rows` | 只有一条被拦的消息时，`message.pending.wait(knownMessageIds=[], holdMs=200)` 返回 `changed=False, held=True`；放行后再 wait，立即返回 `changed=True, held=False` | 同 T13 |
| T15 | 同文件 `::test_fetch_pending_only_filter` | facade 层：`fetch_pending(r, only=frozenset({"a"}))` 只返回 a、只给 a 盖戳；`only=frozenset()` 不写任何行；`only=None` 与今天一致 | `TypeError: fetch_pending() got an unexpected keyword argument 'only'` |
| T16 | `tests/daemon/links/admission/test_delivery_status_refused.py` | `TerminalState("refused")` 可解析；`record()` 的升级矩阵（expired→refused 升、refused→fetched 升、fetched→refused 不降、refused→refused 不改时刻）；合并时同一 id 的 fetched 胜过 refused、refused 胜过 expired；`read.py` 与 `core.py` 的 `consumption_state` 对 refused 都给 REFUSED | `ValueError: 'refused' is not a valid TerminalState` |
| T17 | `tests/daemon/links/admission/test_composition_link_slot.py::test_one_slot_is_shared` | 走 `_app` 组装：`app.link_admission` 是一个 `LinkAdmission`，且与 inbox service 持有的槽、bridge 持有的槽是**同一对象**（`is`）。防止任何一处在生产组装里拿到一个永不会被 WP2 绑定的独立槽 | `AttributeError`（同 T13） |

T13 补充（不是新用例，是对现有测试的约束）：WP4 的缺省会让现有"经 bridge 或 application 派发 agent→agent"的测试不再派发。实施的第一步是在只加了槽与缺省的状态下跑全量，列出因此失败的测试，然后在每处显式绑定 `PermissiveLinkGate`：在测试或其 helper 里调 `app.link_admission.bind(PermissiveLinkGate())`，或给 bridge 传 `link_admission=`。不改这些测试的任何断言。不写 autouse fixture，不写目录级 conftest 全局放行。清单写进 PR 正文（§5 E4）。已知至少 `tests/daemon/delivery/message/test_message_pending_wait.py` 在内（driver→reader 是 agent→agent）。

## 4. 验收标准（全部可机检）

在 worktree 根执行：

1. 焦点测试：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/inbox tests/daemon/delivery tests/daemon/dispatch -q` 退出码 0，且 `tests/daemon/links/admission` 收集到的用例数 ≥ 17。
2. 全量：`uv run --with libcst==1.9.0 pytest tests/` 退出码 0。
3. `uv run ruff check .` 退出码 0。
4. 结构门：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` 退出码 0。
5. 缝未改：`git diff --quiet origin/refactor/org-structure-constrain -- src/hyprial/daemon/impl/links/` 退出码 0。
6. 只经协议与 links 交互：`git grep -nE "links\.(service|store|policy|model|controller)" -- src/hyprial/daemon/impl/{dispatch,inbox,runtime,application}` 无输出；`git grep -n "admit_send\|settle_hint" -- src/hyprial/daemon/impl/dispatch src/hyprial/daemon/impl/runtime src/hyprial/daemon/impl/inbox/service` 无输出。
7. 无旁路缺省：`git grep -n "link_admission or LinkAdmission(" -- src/` 恰好 2 行（`inbox/service/core.py`、`runtime/bridge.py`）；`git grep -n "_link_admission is None" -- src/` 无输出；T12 与 T17 通过。
8. 先红证据：PR 正文的 E2 节对 T1、T6、T10、T12、T13、T15、T16 各贴一段改动前的 pytest 失败输出，失败形状与 §3.2 表中一致。
9. 现有测试改动只做绑定：`git diff origin/refactor/org-structure-constrain --stat -- tests/ ':!tests/daemon/links/admission' ':!tests/support/links'` 列出的每个文件，其 diff 只包含 `PermissiveLinkGate` 的 import 与 `bind(` / `link_admission=` 行（评审逐文件核对）。
10. PR 正文含 `Contract-Impact:` 行，列出：delivery status 的新 `state="refused"`、`ReceiveResult.code="LINK_UNKNOWN"`、`ConsumptionState.refused`；写明 `contract/` 同步由 WP8 完成。`scripts/verify/check_contract_impact.py` 通过。

## 5. 要留的证据（写进 PR 正文；不进 `docs/notes/`）

- E1：分支 head sha、base sha、`uv run pytest --version`。
- E2：先红。T1、T6、T10、T12、T13、T15、T16 在只加测试（未改 src）的提交上的失败输出各一段，带该提交 sha。
- E3：转绿。同一批测试在最终 head 上的 `-q` 汇总行；焦点命令（§4.1）与全量（§4.2）的汇总行；ruff 与两个结构门的输出末行。
- E4：§3 T13 补充步骤列出的现有测试清单：每个文件一行，写失败原因（"agent→agent 派发被未绑定 gate 拦下"）与所做绑定。
- E5：一段 T10 运行时的日志摘录，显示 `dispatch.link_held` 一次、`dispatch.link_admitted` 一次（带 `heldMs`）；一段 T6 的 `delivery_status` 行（`state=refused, reason=LINK_UNKNOWN`）与 notice payload。
- E6：§4.5、§4.6 两条 grep 的空输出。
- 未测项：真实两机 ingress（对端 daemon 未加入 org 空间）与真实 orgfs 副本下的互引延迟，均写 BLOCKED（依赖 WP2 的真实 gate 与 WP8 的 E2E），不写 PASS。

## 6. 重评触发

dispatch 处不订阅 hook 事件，靠两个已有的周期性入口重评：

| 入口 | 触发 | 最坏延迟 |
| --- | --- | --- |
| headless | 每个 runtime tick 的 `harnesses.dispatch_deliveries` 步骤，对每个 streaming actor 的 `dispatchable_messages` 逐条调 `dispatch_admitted` | 一个 tick |
| interactive | carrier 每次调 `message.pending.wait`（`unseen()` 带过滤）或 `message.pending.list` | `MESSAGE_WAIT_HOLD_SECONDS`（5 s） |

gate 内部状态的变化（`link.minted`、`link.reclaimed`、W 的周期更新、回复闭合事项）都由 WP2 的 service 从 hook bus 自己消化。WP4 只在下一次评估时读到新答案。

ingress 不重评：refused 不落行，发送方的重试本身就是重评。

## 7. 已知劣化

- 接收时的 wake（`ReceiveResult.wake`，request/reply）对被拦的消息仍会触发：被唤醒的 interactive session 拿到空列表。放行之后不会再补一次 wake，靠 doorbell 在 5 s 内送达。不在本包修，见 §9 缝建议 4。
- 被拦的行占 `max_inbox_items`（默认 10000），到 hold TTL 写 expired。dispatch 不刷新被拦行的 hold（只有已派发的行进 `_refresh_live_queued_holds`），所以等待有上限，符合 §4.3。
- `admit_dispatch` 在每个 tick 对同一条被拦或在途消息重复调用。对 WP2 的要求见 §9 缝建议 2。
- 旧版节点解码不了 `refused`（§2.5）。

## 8. 开放项（交评审）

1. 非 owner 的 `user:` 发送方在 ingress 是否过闸（§1 表）。本包选不过闸，理由是人没有 link 文件；若评审要求过闸，需要 WP2 的 `sender_known` 对 `user:` 定义语义。
2. 回复的调用点豁免（§1.1）会让"标成 reply 的新请求"绕过互引等待。若采纳 §9 缝建议 1，本包改为把 intent 与 reply_to 交给 gate，并删除调用点豁免。

## 9. 对缝的建议

1. `LinkGate.admit_dispatch` 增加只读关键字参数 `intent: str` 与 `reply_to: str | None`（名字不变，只加参数，有缺省值就不破坏 WP0 测试）。这样回复豁免（§4.5）与"reply_to 必须指向本节点发给该发送方的请求"的校验都回到 `policy.py`（§14 指定的位置），WP4 删除 §1.1 的调用点豁免。不改则维持 §1.1。
2. 在 `admit_dispatch` 的 docstring 写明幂等：同一 `(recipient, message_id)` 一旦返回 True，在该消息结算（ack、reply、回收排干）之前保持 True，且重复调用不重复占 W。headless 每个 tick 都会对在途消息重问一次，interactive 的 list 与 wait 也会重问。
3. 在 `sender_known` 与 `admit_dispatch` 的 docstring 写明：在 inbox actor 线程或 SQLite 事务内被调用，必须只读内存、不做 I/O、不阻塞。
4. 可选：给 `LinkAdmission` 留一个 `changed()` 回调，WP2 在 link 成立或 W 增大时调用，由 composition 接到 inbox 的版本号推进上，让 doorbell 立即响，不用等 5 s。不影响正确性，只影响延迟；本包不实现。

## 10. 评审

- 日期：2026-10-14。
- 结论：`BLOCKING:6 ADVISORY:3`。
- 本节的修正规格替代正文中的冲突条款。实施节点必须执行本节。
- 对照依据：主设计 §4、§5.6、§14；README 的缝裁定 18、22–26；当前 `links/ports.py`。
- 依赖审查：正文未提出跨层 import，也未提出 links import inbox service。保留这个边界。以下修改不得把判定迁入 kernel、identity、biz 或 shell。

### B1［阻断］回复豁免与当前缝相反，缺少回复关联的传递

位置：§1.1、§2.1、§2.8–2.9、T5、T12、§8.2、§9.1。

问题：`admit_dispatch` 已有 `intent` 与 `reply_to`。正文仍把这两个参数列为可选提案。按 `intent == "reply"` 直接放行，会绕过互引、回收和 W 判定。主设计 §4.5 只豁免仍 pending、link 仍有效的关联回复。`InboxMessage` 当前没有 `reply_to` 字段，不能假定调用点能直接读取该属性。

修正规格：

1. `LinkAdmission.dispatch_admitted` 必须接收 `reply_to: str | None`。对受管辖消息，必须调用现有 `LinkGate.admit_dispatch`。必须传递 recipient、sender、message_id、now_ms、intent、reply_to。不得在调用点豁免 reply。
2. 回复关联必须来自持久消息数据。沿用 JSON body 的 `replyTo` 键，只有非空字符串能作为 `reply_to`。键缺失、类型不符或 JSON 无法解码时传 None。不得从文本、conversationId 或 messageId 猜测关联。
3. 调用点只传递关联，不查询 inbox 来判定回复豁免。真实请求是否仍 open、是否属于该对端、link 是否有效，由 WP2 的 policy 判定。
4. 当前 agent 回复生产路径不总是写 `replyTo`。集成时必须核对 WP3 的回复生产路径，使显式回复携带原 messageId。未完成前，不得用 reply intent 豁免来掩盖缺失关联。
5. 修改 T5、T12：reply 必须调用 gate。增加无 `replyTo`、错误类型、其他对端的关联、已闭合关联、已回收 link 的用例。假 gate 返回 False 时，headless、list、wait 均不得暴露这些行。增加有效关联回复用例：假 gate 模拟 W 已满并允许该回复，3 个入口必须放行。未绑定时，受管辖 reply 同样留 inbox。

先红形状：调用记录缺少 intent/reply_to，或 gate 明确返回 False 的 reply 仍出现在派发/拉取结果中。

### B2［阻断］消息标记被当成机制身份，形成授权旁路

位置：§1 的 `intent != "system"`、actor `_` 前缀、hook_request 豁免；T2、T5、T11。

问题：主设计 §4.1 不豁免普通 agent。ports 的豁免对象是 system/daemon 内部身份，未授权普通 agent 用 intent 或 body 自报身份。`is_hook_request` 仅检查 JSON 的 `turnHookRequest == true`。普通 agent 的消息也能携带这个键。actor 以 `_` 开头同样不证明它是 daemon 内部身份。

修正规格：

1. 普通 canonical agent→agent 消息必须过 ingress 与 dispatch 闸。修改 intent 为 system、添加 `turnHookRequest`、使用 `_` 前缀均不得扩大豁免。
2. `link_governed` 必须集中处理 README 裁定 25 的豁免。保留 user、adapter、channel、system 身份及自发自收的既定范围。daemon 内部 agent 身份必须由 composition 提供的明确完整 URI 集合识别。不得用全部 `_` 前缀替代该集合。
3. `hook_request` 可保留为 harness 防递归标记，但不得单独作为 link 授权。只有发送者已属于明确的 daemon 内部身份集合，才能绕过 link 闸。
4. 修改 T2/T5/T11：普通 agent 携带 system intent 或 hook 标记，gate 拒绝时必须保持等待。增加普通 `_named-worker` 用例，必须调用 gate。另测明确登记的 dispatch-coordinator 与 turn hook 完整 URI，不调用 gate。不得仅因同名 actor 出现在其他 owner/machine 下就放行。

先红形状：假 gate 拒绝普通 agent 的消息，改标记后却能派发；或 ingress 未询问 sender_known。

### B3［阻断］dispatch 调用线程违背 ports 契约，拉取判定与盖戳分离

位置：§2.7–2.9、§6。

问题：ports 指定 `sender_known` 与 `admit_dispatch` 在 inbox actor 线程调用。正文让 runtime tick 和 IPC 客户端线程直接调用同一个 gate。`DeliveryCustodyFacade.pending_messages` 与 `dispatchable_messages` 当前读只读 SQLite 投影，不在 actor 线程。先在 IPC 线程准入，再发 FetchPendingCommand，会让判定与真实行状态分离。并发 list/wait/tick 也可能同时改变 W 预留。

修正规格：

1. gate 的 ingress 与 dispatch 调用必须在 inbox actor 线程执行。通过 inbox 的类型化 command/facade 增加“准入后的 pending/dispatchable 集合”查询。原有原始 pending 查询保留，供运维与 WP2 的观测使用。不得让 links import inbox。
2. runtime、pending.list、pending.wait 必须经该 actor 命令取得可见集合。runtime 保留准入前的已回答/待结果排除，不能为这些消息新占 W。不得在 runtime 或 IPC 线程直接调用 gate。
3. `fetch_pending(..., only=...)` 必须在 actor 内重新读取存活行并准入。过滤、准入和 fetched 盖戳必须在同一次 actor 命令内完成。only 只是候选集合，不能作为绕过准入的许可。被拦、已消费、已过期、终态失败的行不得盖戳或发布 fetch receipt。
4. ScriptedLinkGate 必须记录调用线程。增加真实 coordinator/facade 测试，断言所有 gate 调用都在 inbox actor 线程。仅测试直接 InboxService 不足以验收这个条件。
5. 增加并发 list/wait/headless 测试。预算假 gate 只提供 1 个新准入名额，多个入口重复访问同一 id 不重复预留；两个不同 id 不能同时占该名额。增加候选查询与 fetch 命令之间发生 ack/expiry 的用例，失效行不得返回。

先红形状：gate 记录到 IPC/runtime 线程；过期候选仍 fetched；同一名额放行两个不同 id。

### B4［阻断］LINK_UNKNOWN notice 会触发生产 actor 的 I/O 保护

位置：§2.4、T6–T9。

问题：正文要求在 SQLite 事务内 `return self._refuse_link_unknown(...)`，又要求该方法在事务外发 notice，执行顺序无法同时成立。生产 `_ActorOwnedInboxService` 使用 `_NoStateActorIo`。仿 `_reject_bare_sender` 调 `is_online` 或 `deliver_notice` 会抛 `external I/O attempted on the delivery state actor`。直接 InboxService 测试不会发现这个失败。

修正规格：

1. 拒收的状态函数只写 refused 记录，并返回 ReceiveResult 与待通知描述。事务成功退出后才能提交通知工作。提交失败或回滚时不得发拒收通知。
2. 生产路径必须由 custody actor 将远端通知交给 DeliveryIoWorker。可复用 custody 的 notice 工作机制。地址在线检查也必须由 I/O worker 执行。不得放宽 `_NoStateActorIo`，不得在 actor 上直接访问 transport。
3. 本地 system notice 必须在拒收记录提交后写入。通知 id 与 idempotency_key 保持 §2.4 的确定性规则。新通知必须携带非空、至多 512 UTF-8 字节的 summary，body 保留 originalMessageId 与 LINK_UNKNOWN。
4. 保留没有 inbox/dedup 行、没有签收 receipt 的拒收结果。增加真实 facade ingress 用例，核对 ReceiveCompleted、refused 记录、通知和无 receipt。模拟通知 I/O 失败，拒收结果与已提交记录仍必须可查询。模拟事务回滚，通知不得发出。
5. 拒收重试的通知去重必须测接收端结果。不得只断言通知构造使用了确定性 id。

先红形状：生产 facade receive 抛出 actor I/O 异常；通知发送时独立 SQLite 连接还读不到 refused；通知失败导致拒收结果丢失。

### B5［阻断］缺少互引、W 与回收边界的接收方测试

位置：§3、§4、§7；主设计 §4.4–4.6、§5.6。

问题：T10–T14 只把静态 admit 布尔值翻转。未验证节点共享 W、窗口收缩、持久等待、回收排干。T11 还明确关闭生产 offer coordinator，不能证明真实派发路径不会形成队头阻塞。

修正规格：新增下列焦点测试，可扩充现有 6 个测试文件，不增加目录直接文件数。

| 场景 | 必须验证的结果 |
| --- | --- |
| 发送者文件存在，互引未同步 | sender_known=True，入库并签收；gate 拒绝 dispatch 时，headless/list/wait 不暴露该行。不得写 refused、盖 fetched 或准备 delivery hook。 |
| 双端互引后来可见 | 不新增消息、不重投正文，仅更新假 gate 状态后，原 id 自动放行。 |
| 分区/过期副本 | gate 持续拒绝时，行在 TTL 内跨重启保留。重启后重新绑定 gate，仍能放行原 id。不得自行 mint/drop 或提前丢行。 |
| 节点 W 共用 | 不同 sender 的新请求共用 1 个接收方预算。增加出站未闭合事项占满 W 的假状态；新请求等待，有效关联 reply 和 owner 消息仍能通过。 |
| W 收缩与重复评估 | 已准入 id 按 ports 的幂等契约保持准入；收缩后新 id 等待。tick/list/wait 重评不得重复计数。释放事项后下一条能放行。 |
| 生产 offer coordinator 的队头阻塞 | 启用 coordinator。队首 agent A 等互引时，owner B 与可准入 agent C 必须能被 offer；A 不得进入 attempt/hold-refresh/hook 状态。 |
| 回收排干 | 模拟 WP2 经 inbox 权威面 ack 已回收对端的残余行。后续 list/wait/tick 不得重新暴露、派发或复活这些行。回复关联的回收判定见 B1。 |
| 等待 TTL 到期 | 假时钟推进到到期前后。到期前不盖 fetched、不刷新派发 hold；到期后不派发，按现有 inbox 过期机制清理并通知。不得自动重发正文。 |
| 等待中的 gate 状态变化 | 启动一次 pending.wait 后，仅改变 link/W 状态，不写 inbox。必须在约定的轮询上限内重新判定，或由 LinkChangeSource 唤醒。不能只测试“变化后另起一次 wait”。 |

上述测试只验证 WP4 接入，不编写 controller。服务率下降、非拥塞慢、后来者公平、审查样本、冷启动和 link 抖动的数值算法归 WP1/WP2。WP4 必须把对应的 gate 拒绝/放行结果传到全部 context 入口。真实 orgfs 与两机验证仍为 BLOCKED，交 WP8。

先红形状：held 行已有 fetched/attempt/hook 状态；重启丢行；生产 coordinator 跳不过 held 队首；重复查询占用多个名额；link-only 变化后等待入口未重评。

### B6［阻断］入站等待注入计数的生产者被划出 WP4

位置：§0 非范围；README 缝裁定 18；ports 的 `TOP_FIELD_INBOUND_AWAITING_INJECTION`。

问题：WP6 负责 top 展示，但 ports 明确规定 WP4 提供 `inboundAwaitingInjection`。正文把这一列整体划出范围，导致 WP6 没有可用数据源。

修正规格：

1. WP4 必须提供按 canonical recipient 汇总的入站等待注入计数。使用现有常量 `TOP_FIELD_INBOUND_AWAITING_INJECTION`，不得另造 JSON 键。
2. 计数必须包含因互引或 W 等待的存活 inbox 行。已消费、过期、终态失败、refused、已 fetched 或已接受注入的行不得计入。system notice 与独立 progress 行不计入。
3. 统计查询不得调用会预留 W 的 admit_dispatch，不得盖 fetched 或 drain notice。复用 inbox 的只读统计/准入状态投影，把计数接入 `top.snapshot` 的 actor 行；WP6 只渲染。
4. 增加混合 inbox 的计数测试，覆盖 held→放行→实际 fetch/接受注入→ack，以及 TTL 清理。连续两次 top 查询不得改变 W、gate 调用记录或 inbox 消费状态。未绑定 gate 的 agent 等待行也必须可见。
5. 将新增统计方法、投影与 top.snapshot 接入文件加入 §2 的改动清单，重新检查目录文件数和行数。

先红形状：top actor 行缺键；值包含已消费/注入行；读取 top 触发准入或消费。

<!-- stc-disable-next-line -->
### A1［建议］已裁定的缝不再列作开放项

README 裁定 22–26 已处理 intent/reply_to、幂等、线程、LinkChangeSource、入站方向和未绑定缺省。宜把 §8–§9 改为已采用的约束，并保留到裁定编号的引用。非 owner 的 user 入站豁免只适用于本包，不改变 §4.1 的出站授权规则。

<!-- stc-disable-next-line -->
### A2［建议］验收 grep 改为核对本包增量

§4.5 对整个 links 目录做差异检查，WP1/WP2 合并后可能因合法文件新增而失败。§4.6 搜索全部 inbox service 的 settle_hint，会与 WP3 的代码冲突。宜记录本包 base SHA，只核对 WP4 未改 ports 与 links 产品文件。import 边界用 AST/结构门验证；不得删除其他工作包的合法调用来满足 grep。

<!-- stc-disable-next-line -->
### A3［建议］证据改存仓库，保留当前基线

本次任务只本地提交，不开 PR。宜把实施先红/转绿输出追加到仓库 evidence 文件，并在规格中写路径与提交 SHA。不得让 E1–E6 仅存 PR 正文。新增测试需有行为断言失败证据；只有 ImportError 不能证明授权边界生效。

评审验证记录：

- 仅追加本节，未改产品、测试或 ports。
- 焦点基线：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/inbox/admission tests/daemon/delivery/message/test_message_pending_wait.py tests/daemon/delivery/terminal/test_delivery_terminal_state.py -q`。
- 实测结果：PASS，`64 passed, 2 warnings`。这只验证现有基线，不代表 WP4 行为验收通过。
- 结构门与模块边界门：按任务指定的 2 条命令执行，均退出 0。
- `git diff --check`：PASS。
- Ruff：`uv run --with ruff ruff check --force-exclude --extend-exclude '*.md' docs/design/send-links-impl/wp4.md` 退出 0。唯一改动为 Markdown，没有 Python 文件可检查；不作为产品 lint 证据。
- STC 检查仅覆盖本节。3 个“建议”标题保留任务指定的分级标签，逐行排除强度词检查。
- 全量产品测试与真实网络场景：未执行。本任务交付评审规格，行为验收交实施与验收节点。
