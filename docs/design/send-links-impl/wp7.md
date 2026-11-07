# WP7：未回复提醒与 openRequestsFrom（实施规格）

- 设计依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §4.5、§8.2、§14
- 缝：`src/hyprial/daemon/impl/links/ports.py`（WP0）
- 分支：`wp7/send-links`，base `refactor/org-structure-constrain`
- 依赖：WP0（已合）。与 WP3、WP5 无代码依赖；行为上在 WP5 合并后才有完整意义（见 §0.3）。

## 0. 先定三件事

### 0.1 「本 turn 注入过」的来源

派单建议的来源是「turn.started 到 turn.completed 之间 delivery.dispatched 到该 actor 的消息」。本规格不采用这个来源，原因是对现有代码的读数：

- headless（`turn_delivery`）：`runtime/reconcile.py` 先 `harnesses.dispatch()` 再 `mark_dispatched()`，此时消息只进入 worker 的队列。`turn_runtime.py::_start` 在 `_current is None` 时才开下一 turn，否则 `_pending.append`。所以 dispatched 发生在本 turn 的 turn.started 之前；在 turn 运行期间 dispatched 的消息属于**下一** turn。按窗口取 dispatched，会把 agent 还没看到的消息报成「未回复」。
- interactive（pi attach，session 载体）：消息经 `message.pending.list`（fetched=true）进入 context，没有 dispatched 事件。今天 interactive 不向 bus 发任何 `turn.*`（`TurnObserver` 只接在 `HarnessLauncher` 上，`wiring/transport.py:723`）。
- 另外，`DaemonHookService.observe_delivery` 用 `_has_route`（只看 per-agent hooks.json），不看 daemon 级 lane，daemon 级消费者收不到 `delivery.*`。

本规格的定义：一个 turn 注入过的消息集合 I = I1 ∪ I2。

| 分量 | 来源 | 覆盖哪种运行时 |
| --- | --- | --- |
| I1 | `turn.completed.payload.deliveryId`（非空时） | headless：一个 turn 恰好一个 delivery |
| I2 | 该 actor 的 inbox 行中 `fetched_at_ms` 落在本 turn 窗口内的 message id | interactive / session 载体：fetched 戳是注入证据；managed worker 不写这个戳，对 headless 为空 |

窗口 `(start, end]`，其中 end = `turn.completed.payload.endedAtMs`，start 的取法：

1. 消费者在该 actor 上一次 turn 结束后见过 `turn.started`：取该事件的 `payload.startedAtMs`（闭区间起点）。
2. 没有见过 turn.started（interactive 退化）：取消费者记住的该 actor 上一次 `turn.completed` 或 `turn.failed` 的 `endedAtMs`（开区间起点）。
3. 两者都没有（daemon 重启后某 actor 的第一个 turn，且没有 turn.started）：没有窗口，I2 = ∅，只用 I1。

这条规则不读 `turn.completed.payload.startedAtMs` 来补退化窗口，因为 inferred 的 turn 报告可能令 start = end。宁可漏提醒，不发错提醒。

结论：消费者订阅 `turn.started`、`turn.completed`、`turn.failed`，不订阅 `delivery.dispatched`。`observe_delivery` 的路由门不在本包改。

### 0.2 「至今仍 pending 的请求」

一行要同时满足：在 actor 的 inbox 里（`recipient == actor`），`intent == "request"`，`pending_messages(actor)` 返回它（未 consumed、未过期、没有终态失败结算）。`intent == "reply"`、`"system"`、进度事件都不算请求。按 §4.4 排干的回收残余已被 ack，自然不在里面。system notice 在 `system_notices` 表，不在 `inbox` 表，所以提醒 notice 自己触发的 turn 不会再引出提醒，不会形成环。

### 0.3 与 WP5 的时序

WP5 合并前，headless 载体轮末自动回复并 ack，`turn.completed` 时 I1 多半已不 pending，提醒很少触发；interactive 也还没有 turn 事件。WP7 的行为全部用假事件与真 inbox 测，不等待 WP5。WP5 的载体在 interactive 上报 turn 结束时，要发 `turn.completed`，`actor` 为 canonical URI，`payload.endedAtMs` 为真实结束时刻；`deliveryId` 可以为空串。这一条写进 WP5 规格的评审核对项。

## 1. 改哪些文件

结构上限：每个含代码的目录最多 7 个直接文件，每个代码文件最多 1000 行。下表列出改动后的计数。

### 1.1 biz（新建子包）

| 文件 | 新建/修改 | 要点 |
| --- | --- | --- |
| `src/hyprial/biz/impl/hooks/reminder/__init__.py` | 新建 | 导出 `ReplyReminderConsumer`、`REPLY_REMINDER_EVENTS`、`reply_reminder_text` |
| `src/hyprial/biz/impl/hooks/reminder/consumer.py` | 新建 | `ReplyReminderConsumer.observe(event)`：按 §0.1 维护每个 actor 的 turn 状态，在 turn.completed 时调用 port 查询并按 §2.3 的幂等规则写提醒。在 bus lane 线程上运行，所有 port 异常都捕获后记 `reply_reminder.failed`，不向 bus 抛出。状态有上限：最多 1024 个 actor，按 LRU 淘汰；每个 actor 最多记 256 个已提醒 id，按 FIFO 淘汰。只 import `hyprial.kernel` 与 `hyprial.daemon`（公共面） |
| `src/hyprial/biz/impl/hooks/reminder/text.py` | 新建 | `reply_reminder_text(ids, *, max_bytes) -> str`，纯函数，措辞见 §2.4 |
| `src/hyprial/biz/interface.py` | 修改 | 懒导出表、TYPE_CHECKING 与 `__all__` 三处各加 `ReplyReminderConsumer`、`REPLY_REMINDER_EVENTS` |

`biz/impl/hooks/` 的直接文件数不变（1）；`reminder/` 3 个文件。

### 1.2 daemon

| 文件 | 新建/修改 | 要点 |
| --- | --- | --- |
| `src/hyprial/daemon/impl/application/ports.py` | 修改 | 新增 `ReplyReminderPort`（Protocol）与 `ReplyReminderConsumerFactory = Callable[[ReplyReminderPort], Any]`，写法与 `QuotaHookConsumerFactory` 相同，见 §2.2 |
| `src/hyprial/daemon/interface.py` | 修改 | 导出 `ReplyReminderPort`、`ReplyReminderConsumerFactory`（指向 `application.ports`）与 `SUMMARY_MAX_BYTES`（指向 `links.ports`）。三处同步：TYPE_CHECKING、懒导出表、`__all__` |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/reply_reminders.py` | 新建 | (a) `open_requests_by_sender(inbox, *, recipient, senders, now_ms) -> dict[str, tuple[str, ...]]`：读一次 `pending_messages(recipient)`，按 `sender` 分组，只保留 `intent == "request"`，组内按 arrival 顺序。(b) `InboxReplyReminders`：`ReplyReminderPort` 的实现，持有 inbox、`node_id` 与时钟，见 §2.2。目录直接文件数 5 → 6 |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py` | 修改 | `_ipc_message_send`：在 targets 循环前，当 `params.get("replyTo") is None` 时，用 `open_requests_by_sender(self._inbox, recipient=source, senders=requested_targets 中非 route: 的项, ...)` 查一次。每个命中的 delivery 项加 `openRequestsFrom`；返回体顶层加 `openRequestsFrom`，值为各项按 target 顺序合并去重后的列表。两处都只在非空时出现。查询抛 `InboxAuthorityUnavailable`、`InboxAuthorityTimeout` 时省略该字段，记一条 warn `send.open_requests_unavailable`，发送照常进行。727 行，加约 20 行 |
| `src/hyprial/daemon/impl/inbox/authority/read.py` | 修改 | 新增 `fetched_between(recipient, *, since_ms, until_ms, since_inclusive) -> tuple[str, ...]`：只读 SQL，条件为 `recipient = ? AND fetched_at_ms` 落在窗口内，按 `arrival_id` 排序，返回 message id。669 行 |
| `src/hyprial/daemon/impl/inbox/authority/facade.py` | 修改 | 加 `fetched_between` 透传。624 行 |
| `src/hyprial/daemon/impl/inbox/service/projection.py` | 修改 | `InboxService` 加同名同义只读方法，带 `@_synchronized`。测试和单进程路径直接用 `InboxService`。412 行 |
| `src/hyprial/daemon/impl/application/wiring/reply_reminder_hooks.py` | 新建 | `wire_reply_reminder_hooks(*, inbox, node_id, clock_ms, consumer_factory, daemon_hooks)`：`consumer_factory` 为 None 时不做任何事；否则构造 `InboxReplyReminders`，再调用 `daemon_hooks.register_consumer("reply-reminder", consumer_factory(port), REPLY_REMINDER_EVENT_NAMES)`。事件名常量 `("turn.started", "turn.completed", "turn.failed")` 在 daemon 侧单独定义，daemon 不 import biz。目录直接文件数 6 → 7（到上限） |
| `src/hyprial/daemon/impl/application/wiring/transport.py` | 修改 | 在 `wire_quota_hooks(...)` 之后加一次 `wire_reply_reminder_hooks(...)` 调用和一行 import。⚠️ 文件现有 982 行，净增不得超过 10 行；超出时把 quota 与 reminder 两次调用一起移进 `reply_reminder_hooks.py` 的一个 helper，不得压缩其它代码凑行数 |
| `src/hyprial/daemon/impl/application/__init__.py` | 修改 | `DaemonApplication.__init__` 与第二个构造入口（约 892 行，与 `hook_consumers` 并列）各加关键字参数 `reply_reminder_consumer_factory: ReplyReminderConsumerFactory | None = None`，存为 `self._reply_reminder_consumer_factory`。936 行，加约 6 行 |

daemon 级 lane 走 `register_consumer`，不放进 `hook_consumers` 映射。原因：映射里的消费者受各 agent 的 hooks.json 门控（`_ConsumerLane` 只对 `registrations(event)` 中点名的 consumer 回调），而提醒必须对所有 actor 生效。quota 也是同样的处理。`register_consumer` 把事件并入 `_daemon_consumer_events`，所以 `observe_started` 与 `observe_turn` 的 `_publishes` 对任意 actor 为真。

### 1.3 shell（composition）

| 文件 | 新建/修改 | 要点 |
| --- | --- | --- |
| `src/hyprial/shell/impl/daemon_wiring/hooks.py` | 修改 | 新增 `reply_reminder_consumer_factory(port)`，返回 `ReplyReminderConsumer(port=port, logger=...)`。logger 取 port 携带的 logger（见 §2.2 中 `log`），使 shell 不再依赖第二个 deps 对象 |
| `src/hyprial/shell/impl/daemon_wiring/__init__.py` | 修改 | `daemon_dependencies()` 加键 `"reply_reminder_consumer_factory"` |

`shell/impl/daemon_wiring/` 已有 7 个文件，本包**不在该目录新建文件**。

### 1.4 不改的

`kernel` 不改：事件名已在 `EVENT_NAMES` 中。`links/ports.py` 不改。CLI 的 stderr 提示行与 MCP 透传属于 WP6。`contract/` 同步属于 WP8；本包 PR 正文写 `Contract-Impact:`，见 §4。

## 2. 接口

### 2.1 消费者订阅的事件

`REPLY_REMINDER_EVENTS = ("turn.started", "turn.completed", "turn.failed")`（biz 侧），daemon 侧的 `REPLY_REMINDER_EVENT_NAMES` 与它逐字相同（§3 的 W2 测试钉住两者相等）。`actor is None` 的事件与其它事件名一律忽略。

| 事件 | 消费者动作 |
| --- | --- |
| `turn.started` | 记 `started[actor] = payload.startedAtMs`，覆盖旧值（turn 丢失时以新的为准） |
| `turn.completed` | 按 §0.1 算出 I1 与窗口；I1 为空且没有窗口时，不调用 port、直接结束；否则调用 `port.unreplied_requests(...)`，再按 §2.3 决定是否 `port.post_reply_reminder(...)`。最后清 `started[actor]`，记 `last_end[actor] = endedAtMs` |
| `turn.failed` | 不提醒（失败的 turn 会重试或终态结算）。清 `started[actor]`，记 `last_end[actor] = endedAtMs` |

### 2.2 daemon 公共面

新增名字均从 `hyprial.daemon` 导入（`daemon/interface.py`）：

```text
class ReplyReminderPort(Protocol):
    def unreplied_requests(
        self, actor: str, *,
        delivery_ids: Collection[str],
        fetched_since_ms: int | None,      # None = 无窗口，不查 I2
        fetched_until_ms: int,
        since_inclusive: bool,             # 规则 1 为 True，规则 2 为 False
    ) -> tuple[str, ...]: ...
        # 返回 (delivery_ids ∪ fetched_between(...)) ∩ {pending_messages(actor) 中 intent=="request"}，
        # 按 arrival 顺序
    def post_reply_reminder(self, actor: str, request_ids: Sequence[str], text: str) -> bool: ...
        # 写一条 system notice；True = 已写入或同 id 已存在
    def log(self, level: str, event: str, **fields: Any) -> None: ...

ReplyReminderConsumerFactory = Callable[[ReplyReminderPort], Any]
SUMMARY_MAX_BYTES   # 再导出 links.ports.SUMMARY_MAX_BYTES，biz 用它限长提醒文本
```

`InboxReplyReminders.post_reply_reminder` 用以下字段构造 notice，与 `outbox.py::_deliver_system_notice` 的告警 notice 同构：

| 字段 | 值 |
| --- | --- |
| `message_id` | `uuid5(NAMESPACE_URL, f"hyprial:reply-reminder:{actor}:{','.join(sorted(request_ids))}")` |
| `conversation_id` | 同 `message_id` |
| `sender` | `f"system:hyprial:{node_id}"` |
| `recipient` | `actor`（turn 在本机跑，actor 必在本机，直接调用 `inbox.receive_system_notice`，不走 transport） |
| `intent` / `lifecycle` | `"system"` / `DeliveryLifecycle.ONLINE_ONLY` |
| `idempotency_key` | `f"reply-reminder:{message_id}"` |
| `payload` | `{"message": text, "systemNotice": true, "reason": "reply-reminder", "openRequests": [ids…]}` |
| `summary` | `text`（WP0 的字段；text 已由 biz 限在 `SUMMARY_MAX_BYTES` 内） |

注入路径不改：interactive 由下一次 `message.pending.list` drain；headless 由 `reconcile._dispatch_harness_deliveries` 把 notice 作为一个 turn 派出。

使用到的已有方法：`InboxService` 与 `DeliveryCustodyFacade` 上的 `pending_messages`、`receive_system_notice`、`system_notices`、`drain_system_notices`，以及 `DaemonHookService.register_consumer`。新增的是 `fetched_between`。

### 2.3 幂等

设 U 为 port 返回的未回复集合，R[actor] 为该 actor 已提醒过的 id 集合。

- U = ∅：不写。
- U ⊆ R[actor]：不写（同一组或其子集不重复提醒），记 debug 事件 `reply_reminder.suppressed`。
- 否则写一条 notice，列出 U 的全部 id，然后令 R[actor] ∪= U。

两层保证：同一事件被投递两次时，R 拦住第二次；R 丢失时（daemon 重启），notice id 由 (actor, sorted U) 决定，`INSERT OR IGNORE` 在 notice 未被 drain 前拦住重复。已知边界：daemon 重启且 notice 已被 drain 后，同一组可能再提醒一次。这一点写进模块 docstring，不做持久化。

### 2.4 提醒文本

`"{N} 条请求未回复：{id1}, {id2}, …；回复用 reply，无需回复用 ack"`，N = |U|。UTF-8 字节数不得超过 `SUMMARY_MAX_BYTES`（512）：按 arrival 顺序逐个放入 id，直到再放一个会超限为止；有 id 放不下时，在 id 列表后加 `" 等（共 {N} 条）"`。完整列表始终在 payload 的 `openRequests` 中。

### 2.5 `message.send` 返回字段

```jsonc
{
  "ok": true, "operationId": "…", "conversationId": "…",
  "deliveries": [
    {"target": "agent:allen:n:x", "messageId": "…", "accepted": true, "queued": false,
     "openRequestsFrom": ["<id-r1>", "<id-r2>"]},          // 仅非空时出现
    {"target": "agent:allen:n:y", "messageId": "…", "accepted": true, "queued": false}
  ],
  "messageId": "…",                                        // 仍只在单目标时出现
  "openRequestsFrom": ["<id-r1>", "<id-r2>"]               // 各项合并去重；仅非空时出现
}
```

- 数据来源：发送者自己的 inbox（`recipient == source`，source 为 `caller.sender` 的 canonical 形式），条件为 `sender == target`、`intent == "request"`、仍 pending。读的是本机 inbox 库，不跨节点。
- 带 `replyTo` 的发送不计算（它本身是回复）。`route:` 目标不计算。`message.reply` 不加此字段。
- 字段缺省而不是给 `[]`：现有对 send 返回体做整体比较的测试与 golden 不受影响，也与返回体里 `replyPathUnavailable`、`messageId` 等条件键的写法一致。
- 这个字段不改变 `ok`、`accepted`、`queued`，也不阻止发送。

## 3. 测试清单

运行：`PYTHONPATH=$PWD/src uv run --extra test pytest <路径> -q`。「先红」指在实现前提交测试后的失败形状；标为「守护」的条目在实现前就会通过，用来防回归，不计入先红证据。

### 3.1 biz：`tests/biz/hooks/reminder/`（新目录：`__init__.py` + 3 个测试文件）

用假 port 记录调用；事件用 `HookEvent` 与 `turn_event_payload` 构造。

| # | 文件 | 测什么 | 先红形状 |
| --- | --- | --- | --- |
| B1 | `test_reply_reminder_consumer.py` | 有未回复：turn.started(A, d1, t0) → turn.completed(A, d1, t1)；port 返回 `("d1",)`。断言 port 收到 `delivery_ids={"d1"}`、`fetched_since_ms=t0`、`since_inclusive=True`、`fetched_until_ms=t1`；`post_reply_reminder` 被调用一次，参数为 `("A", ("d1",), "1 条请求未回复：d1；回复用 reply，无需回复用 ack")` | `ModuleNotFoundError: hyprial.biz.impl.hooks.reminder` |
| B2 | 同上 | 全部已回复：port 返回 `()`，不调用 post | 同上 |
| B3 | 同上 | 幂等：(a) 同一个 turn.completed 事件投递两次，只 post 一次；(b) 下一 turn 未回复集合为 {d1}（子集），不 post；(c) 再下一 turn 为 {d1, d2}，post 一次，列出两个 id | 同上；实现后若缺 R 集合，(a) 的形状为 `assert 2 == 1` |
| B4 | 同上 | interactive 退化，没有 turn.started：(a) actor 的第一个 turn.completed 带 `deliveryId=""` → 不调用 port，不 post；(b) 第二个 turn.completed(t2) → port 收到 `fetched_since_ms=t1`（上一 turn 的 endedAtMs）、`since_inclusive=False`、`delivery_ids=∅`；(c) 第一个 turn.completed 带 `deliveryId="d9"` → port 收到 `fetched_since_ms=None`、`delivery_ids={"d9"}` | 同上 |
| B5 | 同上 | turn.failed：不调用 port、不 post；之后的 turn.completed 没有 turn.started 时，以 failed 的 endedAtMs 作为窗口起点 | 同上 |
| B6 | 同上 | port 抛异常：`observe` 不抛；logger 收到 `reply_reminder.failed`，其中有 `actor` 字段；该次失败不写入 R，下一次相同集合仍会 post | 同上 |
| B7 | 同上 | 过滤与上限：`actor=None`、其它事件名（如 `delivery.dispatched`）被忽略；1025 个 actor 依次 started 后，最早那个 actor 的 started 被淘汰（按退化路径处理） | 同上 |
| B8 | `test_reply_reminder_text.py` | 1 个 id 时的文本逐字相等；30 个 36 字节 uuid 时 `len(text.encode()) <= 512`，以 `"（共 30 条）"` 结尾，且所列 id 是输入的前缀 | `ModuleNotFoundError` |
| B9 | `test_reply_reminder_wiring.py` | `daemon_dependencies()["reply_reminder_consumer_factory"](fake_port)` 返回 `ReplyReminderConsumer`；`hyprial.biz.REPLY_REMINDER_EVENTS == ("turn.started", "turn.completed", "turn.failed")` | `KeyError: 'reply_reminder_consumer_factory'` |

### 3.2 daemon：`tests/daemon/application/messaging/`（新目录：`__init__.py` + 3 个测试文件）

用真 `InboxService`（临时 sqlite）与 `DaemonApplication`。构造方式沿用 `tests/daemon/inbox/idempotency/test_agent_send_idempotency.py` 的 `LoopbackTransport` 与 `owned_applications` 写法。

| # | 文件 | 测什么 | 先红形状 |
| --- | --- | --- | --- |
| D1 | `test_open_requests_from.py` | 有返回：X 发给 A 的 request r1 在 A 的 inbox 中仍 pending；A 调用 `message.send` 发给 X，不带 replyTo → 顶层 `openRequestsFrom == [r1]`，`deliveries[0]["openRequestsFrom"] == [r1]`，`ok`、`accepted` 为 True，X 的 inbox 中新增这条消息（照常发出） | `KeyError: 'openRequestsFrom'` |
| D2 | 同上 | 无返回（守护）：(a) A 的 inbox 中没有来自 X 的行；(b) r1 已被 `message.reply` 回复；(c) 来自 X 的行 intent 为 `reply`；(d) 带 `replyTo` 发送。四种情况下顶层和每个 delivery 都**没有**该键（`"openRequestsFrom" not in result`） | 守护 |
| D3 | 同上 | 多目标：to=[X, Y]，只有 X 有待回复 r1、r2 → 顶层 `[r1, r2]`（arrival 顺序）；Y 的 delivery 项没有该键 | `KeyError` |
| D4 | 同上 | 查询失败不阻断：monkeypatch `pending_messages` 在 recipient == A 时抛 `InboxAuthorityUnavailable` → send 返回 `ok: True`，没有该键，日志中有 `send.open_requests_unavailable` | 实现前为未捕获的 `InboxAuthorityUnavailable`。发送路径本身不读 A 的 pending，因此实现前该测试的红形状是「日志断言失败」 |
| P1 | `test_reply_reminder_port.py` | `InboxReplyReminders.unreplied_requests`：A 的 inbox 中有 r1（request，pending）、r2（request，已 ack）、r3（reply）、r4（request，fetched 在窗口内）、r5（request，fetched 在窗口前）、r6（request，fetched 恰在窗口起点）。传入 delivery_ids={r1, r2, r3}：开区间返回 `(r1, r4)`；闭区间（since_inclusive=True）返回 `(r1, r4, r6)`；`fetched_since_ms=None` 返回 `(r1,)` | `ModuleNotFoundError: …inbox_surface.reply_reminders` |
| P2 | 同上 | `post_reply_reminder`：`system_notices(A)` 恰有 1 行，字段逐项匹配 §2.2 表格（intent、sender、reason、openRequests、summary）；同一组再次调用返回 True，仍只有 1 行；`drain_system_notices(A)` 后再调用会再写 1 行（说明为什么 biz 需要 R 集合） | 同上 |
| P3 | 同上 | `fetched_between` 在 `InboxService` 与 `DeliveryCustodyFacade` 上结果相同（对同一库调用两者） | `AttributeError: … has no attribute 'fetched_between'` |
| W1 | `test_reply_reminder_flow.py` | 端到端接线：带 `reply_reminder_consumer_factory`（取自 shell 的 `daemon_dependencies()`）构造 DaemonApplication；某 actor 没有 hooks.json；A 的 inbox 中有 X 发来的 pending request r1；经 `daemon_hooks.observe_started` 与 `observe_turn`（delivery_id=r1，COMPLETED）发事件 → 等 bus 排空后，`system_notices(A)` 有一条 reason 为 `reply-reminder` 的 notice；之后 `message.pending.list`（A）返回该 notice，intent 为 `system`，`message` 以 `"1 条请求未回复："` 开头 | `TypeError: … unexpected keyword argument 'reply_reminder_consumer_factory'` |
| W2 | 同上 | 没有环：W1 之后以 notice id 作为 deliveryId 再跑一个 turn → 不产生第二条 notice。另断言 daemon 侧 `REPLY_REMINDER_EVENT_NAMES == hyprial.biz.REPLY_REMINDER_EVENTS` | 同 W1 |
| W3 | 同上 | 未注入工厂（`reply_reminder_consumer_factory=None`）：bus projection 中没有 `reply-reminder` lane，turn 事件照旧（守护：不注入时行为不变） | 守护 |

### 3.3 已有测试（必须保持绿）

`tests/biz/hooks/`、`tests/daemon/links/`、`tests/daemon/inbox/`、`tests/daemon/inbox/idempotency/`、`tests/daemon/delivery/`、`tests/daemon/path_authz/test_send_gate.py`、`tests/daemon/application/composition/`、`tests/tooling/architecture/boundaries/test_module_boundaries.py`。

## 4. 验收标准（可机检）

在 worktree 根目录逐条执行，全部满足才算通过：

1. `PYTHONPATH=$PWD/src uv run --extra test pytest tests/biz/hooks/reminder tests/daemon/application/messaging -q` 全部通过；用例数不少于 §3.1 与 §3.2 的条目数（B1–B9、D1–D4、P1–P3、W1–W3，共 19 个），skip 数为 0。
2. `PYTHONPATH=$PWD/src uv run --extra test pytest tests/biz tests/daemon/links tests/daemon/inbox tests/daemon/delivery tests/daemon/path_authz tests/daemon/application -q` 全部通过。
3. `uv run --with libcst==1.9.0 pytest tests/` 全量通过；若有与本包无关的既有失败，PR 正文逐条列出，并附 base 分支上同样失败的证明。
4. `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` 均无违规。
5. `uv run ruff check .` 干净。
6. `grep -rn "hyprial.daemon.impl" src/hyprial/biz/impl/hooks/reminder` 无输出；`grep -rn "hyprial.biz" src/hyprial/daemon` 无输出。
7. `wc -l src/hyprial/daemon/impl/application/wiring/transport.py` ≤ 1000；`find src/hyprial/shell/impl/daemon_wiring -maxdepth 1 -type f | wc -l` 仍为 7。
8. `uv run python -c "import hyprial.daemon as d; d.ReplyReminderPort; d.ReplyReminderConsumerFactory; d.SUMMARY_MAX_BYTES"` 与 `uv run python -c "import hyprial.biz as b; b.ReplyReminderConsumer; b.REPLY_REMINDER_EVENTS"` 退出码均为 0。
9. 先红证据：提交历史中，测试提交早于实现提交；在测试提交上运行 §4.1 的命令，失败形状与 §3 表中所写一致（守护条目除外）。
10. PR 正文含 `Contract-Impact: message.send 新增可选返回字段 openRequestsFrom（仅非空时出现）；contract/ 同步由 WP8 承担（send-links-impl/README.md 工作包 8）`，`scripts/verify/check_contract_impact.py` 通过。Skill-Impact：none（CLI 提示行与 skill 说明在 WP6/WP8）。

## 5. 要留的证据

全部写进 PR 正文；原始输出另存于 `docs/evidence/org-structure-constrain-2026-10-14/wp7/`（只追加）：

- `red.txt`：在测试提交上运行 §4.1 命令的输出，可看到 §3 所列失败形状。
- `green.txt`：在实现提交上运行 §4.1 与 §4.2 命令的输出（通过数、0 skip）。
- `gates.txt`：结构门、模块边界门、ruff、§4.6 两条 grep、§4.7 行数与文件数的输出。
- `notice-sample.json`：W1 生成的 notice 行（message_id、sender、recipient、intent、summary、payload），用来证明格式符合 §2.2。
- `send-sample.json`：D1 与 D2(a) 的两份 `message.send` 返回体，各一份有键、一份无键。
- PR 正文中列明：未实测项写 UNKNOWN。至少有一项：真实 interactive pi 的 turn.completed 上报属于 WP5，本包只用假事件验证退化路径，状态为 BLOCKED-on-WP5。

## 6. 对缝的建议

1. `SUMMARY_MAX_BYTES` 只在 `daemon/impl/links` 中，biz 读不到。本包在 `daemon/interface.py` 中再导出它。今后其它包也应从 `hyprial.daemon` 取这个值。
2. `DaemonHookService.observe_delivery` 用 `_has_route`，daemon 级 lane 收不到 `delivery.*`。本包不依赖它，因此不改。WP2 的 `links/service.py` 若要「订阅 hook bus 喂样本」并用到 `delivery.*`，需要先把它改成 `_publishes`，并为此补测试。
3. WP5 的规格应写明 interactive 载体上报 `turn.completed` 时的字段要求：`actor` 为 canonical URI，`endedAtMs` 为真实时刻，`deliveryId` 允许为空串。最好同时上报 `turn.started`，使 §0.1 走规则 1 而不是退化路径。

## 7. 评审

结论：有 4 条阻断项、4 条建议项。实施时必须采用下列修正规格。

评审基线：`a37ae8f1c`。核对设计 §4、§5.6、§7、§8.1、§8.2、§14，以及实施目录 README 的缝裁定。

依赖方向未发现越层要求：biz 经 daemon 公共面调用 port，shell 注入工厂，daemon 不导入 biz。`links/ports.py` 的名称与三种发送决策保持不变。提醒不得迁入 links，也不得令 links 导入 inbox service。

### R1【阻断】notice 的 summary 未覆盖持久化，W1 仍依赖正文注入

位置：§1.2、§2.2、P2、W1。

`InboxMessage.summary` 已在 WP0 增加，但数据库读写未随之增加。当前 `system_notices` 表没有 summary 列；`_persist_system_notice_locked` 不写 summary。`InboxService._row_message` 与 `InboxReadProjection._message` 也不恢复 summary。只在构造 notice 时设置 `summary=text`，无法满足 P2。隔离库实测：`input.summary='reminder'; persisted.summary=None`。

W1 还要求 pending 行含 `message` 正文。这与设计 §7、§8.1 的仅注入 summary 契约冲突。WP3-A 的兼容字段不能成为 WP7 的长期验收条件。

**修正后的规格文字：**

- WP7 必须保证 system notice 的 summary 在数据库往返后不变。
- 在 §1.2 增列 `inbox/service/core.py` 的 schema 与旧库迁移。`system_notices.summary` 采用可空 TEXT 列，保留旧 notice 的 payload。
- 增列 `inbox/service/delivery/outbox.py` 的 notice 写入，以及 `inbox/service/delivery/retry.py`、`inbox/authority/read.py` 的行恢复。目录文件数不得增加。
- 如果 WP3-A 已提供这些能力，WP7 复用已有代码，并保留往返测试。不得重复添加迁移或重新定义 summary 上限。
- P2 必须覆盖新库、旧库迁移、关闭后重开、单进程读取、authority 读取和 drain。每条新 notice 的 summary 必须与 text 相同。
- W1 改为断言 `intent == "system"`、`summary == text`、`bodyBytes` 正确。不得通过读取 payload 来生成注入文本。
- §0 的依赖声明必须更正：消费者与 port 测试可独立于 WP3；W1 的新 pending 字段依赖 WP3-A。投影未合入时，W1 记 BLOCKED，不得用旧 `message` 字段冒充摘要验收。pending 投影的产品改动仍归 WP3。
- WP3-B 合入后的验收必须断言 pending 行没有 `message` 键。完整 `openRequests` 列表留在 payload，不随 summary 自动注入。
- 新增迁移与往返测试必须先红。真实载体的仅摘要注入由 WP5/WP8 验证；未执行时记 BLOCKED，不以 W1 代替。

### R2【阻断】写入返回 False 后仍可能永久抑制提醒

位置：§2.1–§2.3、B6。

port 明确返回 bool，但 §2.3 无条件执行 `R[actor] ∪= U`。当写入返回 False 时，请求会被记为已提醒，实际没有 notice。B6 只测抛异常，未覆盖 False。

interactive 还存在重放问题：失败后若已清 started 并推进 last_end，同一个 completed 事件重放只查询 `(end, end]`。假 port 固定返回 U 的测试不能发现这个问题。

**修正后的规格文字：**

- 只有 `post_reply_reminder(...) is True` 时，才能把 U 加入 R。
- 返回 False 或抛异常时，必须记录 `reply_reminder.failed`。不得将 U 记为已提醒，不得向 bus 抛出异常。
- 消费者必须在每个 actor 的状态内保存最近完成事件的有效查询窗口。事件以 `(source, seq)` 标识。同一事件重放时沿用原窗口，不重新从 last_end 推导窗口。
- 上述窗口随 actor 的 LRU 状态淘汰，不增加无界事件历史。成功写入后仍按 §2.3 抑制重复提醒。
- B6 增加查询抛异常、写入抛异常、写入返回 False 三类测试。失败后重放同一 completed 事件，均能再次尝试。
- 增加真 inbox 的 interactive 测试：窗口内 fetch 请求，第一次 post 返回 False，第二次成功。最终恰有 1 条 notice，不能因窗口退化而漏掉请求。

### R3【阻断】缺少 §4 边界与 §5.6 劣化条件的提醒侧断言

位置：D1–D3、P1、W1–W3、§4。

现有用例只证明字段出现和 notice 写入。D1 没有证明原请求仍 pending，也没有证明提醒不释放 W。P1 没有覆盖过期、终态失败和回收排干。等待与副本延迟均没有用例。

**修正后的规格文字：**

提醒与字段查询只读取 daemon 权威状态。不得 ack 请求、自动回复、重发正文、铸造或回收 link，也不得修改 W、cwnd 或控制器样本。

在已有测试文件中增加下表用例，每项必须有独立断言：

| 边界或劣化条件 | 修正后的测试要求 |
| --- | --- |
| §4.5：未带 reply-to 的新发送 | 扩展 D1：原请求仍 pending；新消息是 request；写 notice 不调用 ack、reply 或容量释放接口。显式 reply/ack 后才从提醒候选中消失。 |
| §4.2–§4.3：等待 | 覆盖 `LINK_MINTING`、`LINK_SELF_FULL`、`LINK_PEER_FULL`、`LINK_WINDOW_FULL`。添加字段前后，`ok`、`accepted`、`queued`、`holdReason` 均不变。字段查询不得令等待消息出门。 |
| §4.4：回收排干及 §5.6：link 抖动 | 真 inbox 中先 ack 排干，再处理旧 turn.completed。不得生成 notice 或 openRequestsFrom，不得重新授权。回 reply 的 `LINK_RECLAIMED` 仍归 WP2/发送闸处理。 |
| §4.6、§5.6：分区或过期副本 | pending 请求仅因副本暂不同步，不得被提醒消费者 ack 或当作回收残余。发送结果仍保持 hold。未注入的请求不能仅凭 arrival 或 dispatched 时刻进入轮末提醒。 |
| §5.6：队头阻塞与审查偏差 | P1 增加已过期和 terminal failure 行，两者均排除。对放弃阈值的释放，只消费 daemon 已裁定的状态；提醒不得另设计时器、补造 T/RTT 样本或重发请求。 |
| §3.2、§5.6：owner 优先级 | pending owner request 同样可提醒，不能因为没有 orgfs link 文件而过滤。notice 不消耗 agent 间 k/cwnd，也不新增未闭合 request。 |
| §5.6：冷启动 | 保留无 started、无 last_end 的首次完成退化规则。重启且 notice 尚未 drain 时，同组重放仍只有 1 条 notice。不得用从 0 开始的窗口扫描全部历史。 |
| §5.6：服务率下降、非拥塞慢、后来者公平性 | 参数调整归 WP1/WP2。WP7 增加只读副作用守卫：字段查询和写 system notice 不喂回复样本，不推进控制周期，不修改控制器参数。 |

WP0 阶段可用受控发送结果验证字段保真，用 spy 验证无副作用。实际 gate 未接线时，真实 hold/W 联测记 `BLOCKED-on-WP2/WP4`，交 WP8 验证。不得为让 D1 通过而放宽生产闸；需要放行的测试显式绑定 PermissiveLinkGate。

### R4【阻断】可选提醒查询的失败范围过窄

位置：§1.2 的 send 改动、D4。

`DeliveryCustodyFacade.pending_messages` 直接调用只读 SQLite view。该 view 的 `_connect`、`_messages` 不把 SQLite 读取失败转换成 `InboxAuthorityUnavailable` 或 `InboxAuthorityTimeout`。因此只捕获这两类异常，可能让可选提醒查询阻断原本能成功的发送。

**修正后的规格文字：**

- 提醒读取失败时，必须省略顶层与全部 delivery 的 openRequestsFrom，记录 `send.open_requests_unavailable`，继续原发送路径。
- 在 daemon 提醒查询适配器内，将 SQLite 读取的 `sqlite3.OperationalError` 转换为已有的 `InboxAuthorityUnavailable`，保留异常 cause。不得把 SQLite 类型暴露给 biz，也不新增公共错误类型。
- send 只在提醒查询的 try 块中捕获 `InboxAuthorityUnavailable` 与 `InboxAuthorityTimeout`。不得吞掉身份校验、summary 校验、LinkGate 拒绝或实际提交失败。
- D4 参数化覆盖 authority unavailable、authority timeout、SQLite operational error。各项均断言发送仍调用原提交路径，两个层级都没有提醒字段，日志包含失败类型。
- 增加真 authority 读取适配器测试，证明 SQLite 读取失败确实被转换。仅 monkeypatch 成 authority 异常不能作为该项证据。

### A1【建议】删除已经完成的 SUMMARY_MAX_BYTES 再导出任务

位置：§1.2、§2.2、§6.1。

当前 `hyprial.daemon` 已在 TYPE_CHECKING、懒导出表、`__all__` 三处导出 `SUMMARY_MAX_BYTES`。实施目录 README 的裁定 9、21 已说明公共面由集成者完成。宜将规格改为复用该导出，只新增 ReplyReminderPort 与工厂导出。新增导入测试仍保留。

D/P/W 的新消息与 send/reply 调用宜显式携带有效 summary。照搬旧 idempotency fixture 时，不能沿用缺 summary 的调用，否则 WP3-B 硬切后会失败。

### A2【建议】统一窗口记号并补边界测试

位置：§0.1、P1。

总述写 `(start, end]`，规则 1 实际采用 `[start, end]`。宜直接分别写两种窗口。P1 宜增加 fetched 恰等于 end、晚于 end、recipient 不同和 I1/I2 同 id 去重的断言。

真 fetch 测试宜确认 `fetched_at_ms` 是首次 fetch 戳：后续 fetch 不改写该戳。不得为提醒窗口改成每次 pull 都更新时间。

### A3【建议】截断后的文本仍保留操作提示

位置：§2.4、B8。

§2.4 把省略标记放在 id 列表后，末尾仍有 reply/ack 提示；B8 却要求整个文本以共计标记结尾。宜修改 B8：省略标记位于列表末尾，全文以 `；回复用 reply，无需回复用 ack` 结尾。计算可放入的 id 时，先预留提示与省略标记的 UTF-8 字节数。

### A4【建议】测试完整的内存上限与跨 actor 隔离

位置：§1.1、§2.3、B3、B7。

B7 只测 started 的 actor 淘汰。宜验证 started、last_end、已提醒集合及 R2 的窗口缓存共同遵守 1024 actor 上限。另测单 actor 的 257 个已提醒 id 按 FIFO 淘汰，以及两个 actor 使用相同 id 时互不抑制。

### 评审验证记录

本次只追加规格评审，未改产品、测试或 ports 名称。

- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/test_wp0_seams.py -q`，7 passed、0 skip；有 2 条依赖弃用警告。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`，退出 0。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`，退出 0。
- FAIL（设计要求的隔离复现）：给真 InboxService 写 summary 后重新读取 system notice，summary 为 None。复现使用 TemporaryDirectory 与无网络 transport，未访问生产状态。
- UNKNOWN：上述新增用例尚未编写，未跑产品全量测试。
- BLOCKED：真实 interactive turn 上报与真实 hold/W 联测依赖 WP5、WP2/WP4；本次评审未验证。
- PASS：`uv run --with ruff ruff check --extend-exclude '*.md' --force-exclude docs/design/send-links-impl/wp7.md`，退出 0。Ruff 不检查 Markdown；本次没有 Python 改动。
- PASS：`git diff --check`。
