# WP2：link 的存储、服务、hold 放行与接线（实施规格）

- 版本：**v2**，2026-10-14。v1 由设计节点写成；评审节点（§8，gpt-6.1-sol）提出 BLOCKING:17、ADVISORY:4。v2 把评审并入正文：B1、B3、B5–B17 与 A1–A4 全部并入；B2 和 B4 部分并入，与裁定冲突的部分列在 §9，由集成者裁定。逐条落点见 §0.1。实施节点只按 §0–§7 与 §9 执行。§8 是评审原文，只作记录。
- 日期：2026-10-14
- 段：设计（claude / claude-opus-5-5）；v2 修订（plan / claude-opus-5-5）
- 分支：`wp2/send-links`。实施 base 取 WP4、WP3-A、WP6 合入后的集成分支 `refactor/org-structure-constrain` head（裁定 49）。实施开始时把该 SHA 写进证据文件。
- 依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §2–§6、§8.1、§9、§14；[README](README.md) 缝的裁定 1–52（重点 37、43–51）；`links/ports.py`（WP0）；`links/{model,controller,policy}.py`（WP1，集成分支 `a28d280fc`）；[wp1.md](wp1.md) 评审 B1–B12；WP4 规格与返工实现（分支 `wp4/send-links` @ `8c720faf6`，含 `wp4-acceptance-1.md`；接线面以合入集成分支的版本为准）；WP3 规格（`_link_gate`、`settle_hint`、`read_body`）；WP6 规格（`link.*` 路由与返回形状）；WP7 规格（`application/wiring/` 已占满）。
- 范围：新建 `links/store.py` 与 `links/service.py`，`links/` 由此到 7 个文件；在 inbox SQLite 中加 outbox 的 link hold 列与 link 记账表（ledger）；`message.send` 与 `message.reply` 调 gate；composition 接线；`top` 的两个字段；`link.minted` 与 `link.reclaimed` 事件；`policy.governed` 对 `user:` 目标的修正（裁定 44）。
- 非范围：W 与 cwnd 的算法（WP1）；ingress 与 dispatch 的调用点（WP4）；summary、`read_body` 与 `settle_hint` 的 IPC（WP3）；CLI 与 MCP 的渲染（WP6）；提醒 hook（WP7）；`contract/` 的同步（WP8）。

## 0. 硬约束（实施前先读）

1. `links/` 加 `store.py`、`service.py` 后恰为 7 个直接文件。⛔ 不再加文件。`links/__init__.py` 不改，只导入 `ports`（裁定 32，`test_wp0_seams.py` 守卫）。
2. 依赖方向：
   - `store.py` 只许依赖标准库、`hyprial.kernel` 公共面、`links.ports`、`links.model` 与 `orgfs.api`。
   - `service.py` 只许依赖标准库、`hyprial.kernel` 公共面与 `links.{ports,model,controller,policy,store}`。
   - ⛔ 两者都不 import `inbox`、`application`、`dispatch`、`delivery`、`composition`、`pac`、`biz`、`shell`。
   - inbox、在线状态与 PAC 的事实经 §1.2 的 Protocol 进入 service，由 composition 实现。
   - inbox 向 links 的回调只经 inbox 自己定义的 `LinkLedgerSink` Protocol（`inbox/contracts/ports.py`）。inbox 不 import `links.service`。
   - 由 S-B1（AST）机检。
3. 每个文件 ≤ 1000 行。行数预算见 §1.3。`service.py` 超出预算时，把 `list_links`、`stats` 的 JSON 形状与事件 payload 的构造移到 `composition/links.py`（它们是呈现，不是判定）。⛔ 不在 `links/` 加文件。
4. 接线（A4）：
   - `application/wiring/transport.py` 本包 **0 行改动**。它在 WP4 与 WP7 合入后约 996 行，没有余量。
   - 调用点放在 `application/wiring/services.py::_startup_adopt_domain_services` 的开头（+3 行）。该处早于 `_pac_graph_authority` 构造，满足 W10。
   - `application/wiring/` 不加文件。接线代码放 `daemon/impl/composition/links.py`（该目录现有 6 个文件，加后 7 个）。
5. `ports.py` 不改：`LinkSendObserver` 已由裁定 43 落地并导出。`model.py`、`controller.py` 不改。`policy.py` 只改 `governed` 一处（裁定 44，§2.2）。其余缝的需求列在 §7。
6. 时刻一律取注入的 `clock_ms`（composition 传墙钟）或调用参数 `now_ms`。抖动取注入种子的 `random.Random`。测试用假时钟与固定种子。
7. 合并顺序（裁定 49）：WP2 实施在 WP4、WP3-A、WP6 合入集成分支之后开始。接线要用 `app.link_admission`、`app._link_gate`、`app._link_service` 与 router 的 `link.*` 分支。若届时尚未合入，先提交与接线无关的部分（store、service、inbox ledger、单元测试）；接线提交在 rebase 后做。
8. durable 权威（B14）：开放事项、已准入、审查、放弃、排干与 PAC 绑定一律记在 inbox SQLite 的 ledger 表（§3.9），与对应的消息状态变化同事务提交。links 自身不保存 durable 状态（不再有 `state.json`）。orgfs 的 link 文件仍是 link 授权的唯一事实源。

### 0.1 评审并入对照

| 评审 | 处理 | 落点（正文） | 测试 |
| --- | --- | --- | --- |
| B1 已提交的缝裁定 | 并入 | §0.5、§2.2 步骤 0–3、§2.3、§7 | G2、G4、P1 |
| B2 发送授权豁免 | 部分并入；gate 未绑定时发送方 hold 一项列 §9 C2 | §2.2 步骤 0、§2.4、§4.9 | G3、W2、W6 |
| B3 回复校验与 intent | 并入 | §2.2 步骤 1、§2.2 `admit_dispatch`、§3.1 | G5、G10 |
| B4 非 owner 用户的 W | 部分并入（计数）；hold 列 §9 C1 | §3.8 | G2 |
| B5 held 回复与排干码 | 并入 | §2.4 `message.reply`、§3.6 | H11、L8、W4 |
| B6 幂等先于 gate | 并入 | §2.2 `submitted`、§2.4 | G7、W5 |
| B7 T/RTT 时刻与快回复 | 并入 | §3.1、§3.9 | SA2–SA4、L10 |
| B8 豁免来源的注入登记 | 并入（扩展见 §9.2） | §3.1、§3.9 | SA5、L11 |
| B9 审查记录 | 并入 | §1.2 `CensoredRecord`、§3.1 | SA8 |
| B10 回收残余范围 | 并入 | §3.3、§3.6 | R2、R3、R11、L12 |
| B11 副本暂缺不是回收 | 并入 | §1.1、§3.3 | ST8、ST10、H16、R5 |
| B12 多空间快照 | 并入 | §1.1、§3.10 | ST3 |
| B13 单写者与写成功 | 并入 | §1.1、§2.1、§3.4 | ST4、ST6、ST9、G8 |
| B14 durable 记账 | 并入 | §0.8、§3.9 | G9、L14 |
| B15 held 元数据与更新结果 | 并入 | §1.2 `HeldRow`、§3.2、§3.6 | H12–H15、L7 |
| B16 PAC 回收与多任务 | 并入 | §3.7 | R7–R9、W11、W12 |
| B17 劣化场景与契约测试 | 并入 | §4 | G13–G15、SA11–SA16 |
| A1 无基线进展限制 | 采纳 | §4.5、§6 | SA16 |
| A2 不阻塞断言 | 采纳 | §4.2 | G13 |
| A3 满额 add | 采纳（返回「未写入」，不排队） | §2.5 | R10 |
| A4 结构预算与路径 | 采纳 | 依据行、§0.4、§1.3、§5 | 验收 5、6 |

## 1. 改哪些文件

### 1.0 总表

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `src/hyprial/daemon/impl/links/store.py` | 新建 | `LinkStore`：在 OrgFs 上读 `link/` 的本地副本，按最高修订号选每个 actor 的完整快照，校验作者，只写本节点 actor 的文件，订阅变化，发布不可变索引。另承载 service 的外部端口类型（§1.2 的 Protocol 与行数据类） |
| `src/hyprial/daemon/impl/links/service.py` | 新建 | `LinkService`：实现 `LinkGate`、`LinkSendObserver`、`LinkQueries`、`LinkAdmin`、`LinkChangeSource`，并以鸭子类型满足 inbox 的 `LinkLedgerSink`；常驻内存的每 actor 状态；组装 `NodeView` 调 policy；控制器周期；hold 重评与放行；回收与残余；PAC 绑定；事件 |
| `src/hyprial/daemon/impl/links/policy.py` | 改 | `governed`：`uri_scheme(target) == "user"` 时返回 False（裁定 44） |
| `src/hyprial/daemon/impl/composition/links.py` | 新建 | `compose_link_service(app, graph)`：`LinkInboxPort`、在线 agent、PAC 生命周期、本节点判定、内部 actor 名的适配器；创建 store 与 service、`start()`、`link_admission.bind`；设 `_link_gate`、`_link_service`、`_link_task_binder`；`inbox.bind_link_ledger_sink(service)`；注册 bus 消费者；`top` 字段的读函数 |
| `src/hyprial/daemon/impl/application/wiring/services.py` | 改 | `_startup_adopt_domain_services` 开头调 `compose_link_service(self, graph)`（函数内 import） |
| `src/hyprial/daemon/impl/application/shutdown.py` | 改 | 关停：在 orgfs runtime 与 inbox 关闭之前调 `self._link_service.close()`（取 `getattr(self, "_link_service", None)`） |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/link_send.py` | 新建 | 模块级函数（不是 mixin，不改 application 的类基表）：重放识别、gate 询问、决策到 submit 参数的映射、delivery 行的形状、`user:` 目标的 ledger 开闭、回复 hold 的返回。该目录现有 5 个文件，加后 6 个 |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py` | 改 | `message.send` 与 `message.reply` 在 §2.4 的位置调 `link_send` 的函数；`message.reply` 对 agent 目标在 body 写 `replyTo` |
| `src/hyprial/daemon/impl/application/messaging/status/views.py` | 改 | `top.snapshot` 传 `link_fields` |
| `src/hyprial/daemon/impl/operations/top.py` | 改 | `build_actor_row`、`build_top_snapshot` 加参数 `link_fields`；actor 行加 `TOP_FIELD_CAPACITY_W`、`TOP_FIELD_OUTBOUND_IN_FLIGHT` |
| `src/hyprial/daemon/impl/pac/actors/daemon.py` | 改 | `DaemonPacNotificationSender.send`：投递前调 `application._link_task_binder`（存在时）；投递成功后以真实 message id 调 binder 返回的回调 |
| `src/hyprial/daemon/impl/inbox/contracts/ports.py` | 改 | `SubmitMessageCommand` 加 `link_hold`、`link_open`、`link_reply_close`；新命令 `UpdateLinkLedgerCommand` 与完成事件 `LinkLedgerUpdated`；`LinkLedgerSink` Protocol |
| `src/hyprial/daemon/impl/inbox/contracts/api.py` | 改 | `OutboxItem.link_hold`；`LinkLedgerRow`、`LinkLedgerOpen`、`LinkLedgerResult` |
| `src/hyprial/daemon/impl/inbox/service/core.py` | 改 | outbox 列 `link_hold TEXT`；表 `link_ledger`、`link_task_bindings` 与迁移；`_insert_outbox(..., link_hold=None)`；投递成功删除 outbox 行的同一事务写 ledger 的 `sent_at_ms`；`bind_link_ledger_sink` 槽（未绑定时不回调） |
| `src/hyprial/daemon/impl/inbox/actor/custody/submit.py` | 改 | `_stage_submit`：`link_hold` 非空时只入库、回执为 queued、不发 I/O；同事务写 `link_open` 与 `link_reply_close` |
| `src/hyprial/daemon/impl/inbox/actor/custody/retry.py` | 改 | `_claim_retry` 跳过未到期的 hold 行，到期照旧写 `TTL_EXPIRED`；`_apply_inflight_wakes` 的 UPDATE 加 `AND link_hold IS NULL` |
| `src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py` | 改 | 分派 `UpdateLinkLedgerCommand`（§3.6、§3.9） |
| `src/hyprial/daemon/impl/inbox/actor/custody/outcomes.py` | 改 | 投递成功删除 outbox 行时，同事务写 ledger 的 `sent_at_ms` |
| `src/hyprial/daemon/impl/inbox/service/delivery/retry.py`、`outbox.py` | 改 | 非 actor 路径：retry 与 wake 同样跳过 hold 行；投递成功同样写 `sent_at_ms` |
| `src/hyprial/daemon/impl/inbox/service/delivery/receive.py` | 改 | 入站回复落库的事务内闭合匹配的出站 ledger 行；提交后调 `sink.reply_received` |
| `src/hyprial/daemon/impl/inbox/service/projection.py` | 改 | 注入点（pull 的 fetch 标记、push 与 headless 的派发确认）同事务写入站 ledger 行；提交后调 `sink.injected`；事务回滚时调 `sink.discard` |
| `src/hyprial/daemon/impl/inbox/authority/facade.py` | 改 | `submit(..., link_hold=None, link_open=None, link_reply_close=False)`；`update_link_ledger(...)`；`bind_link_ledger_sink(...)`；读：`link_ledger_snapshot`、`link_held_outbox`、`link_inbound_live`、`link_inbound_row`、`submission_result` |
| `src/hyprial/daemon/impl/inbox/authority/read.py` | 改 | 上面各个读的 SQL；`outbox_items` 带 `link_hold` |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py` | 改 | `_outbox_entry_json` 在 `link_hold` 非空时加 `HOLD_REASON_KEY` |
| `tests/support/links/org.py` | 新建 | `FakeOrgFs`（OrgFs 子集：`spaces`、`listdir`、`read_text_snapshot`、`write_text`、`document_authors`、`watch`、`mkdir`，可注入失败与阻塞）、`FakePresence`、`FakePacPort`、`make_service(...)`、`establish(a, b)` |
| `tests/support/links/ledger.py` | 新建 | `FakeInboxPort`：内存 ledger、held 行、入站行，可注入失败、阻塞与部分成功 |
| `tests/daemon/links/test_policy.py` | 改 | 非 owner `user:` 目标的旧断言改为 `Allow(None)`（P1） |
| `tests/daemon/links/test_store.py`、`tests/daemon/links/service/*.py` | 新建 | 见 §4 |

inbox 各目录只改不加文件（`inbox/actor/custody` 已有 7 个文件）。`dispatch/admission.py`、`hooks/service.py` 不改：样本不走 bus（裁定 46），WP4 的槽不改，只读取它暴露的属性（§7 S1）。

### 1.1 `links/store.py`

```python
@dataclass(frozen=True, slots=True)
class LinkIndex:
    """Immutable committed view, swapped atomically; read without a lock."""
    spaces: frozenset[str]                 # live link spaces seen at the last refresh
    writable: frozenset[str]               # live spaces whose last write/mkdir succeeded
    files: Mapping[str, LinkFile]          # actor -> highest-revision committed snapshot (B12)
    revisions: Mapping[str, int]           # actor -> highest revision ever observed (B11)
    absent: frozenset[str]                 # observed before, no readable copy now (B11)
    conflicted: frozenset[str]             # same top revision, different content (B12)
    def known(self, actor: str) -> bool     # actor in files or absent or conflicted
    @property
    def org_available(self) -> bool         # bool(writable)

class LinkStore:
    def __init__(self, fs: OrgFs, *, spaces: Callable[[], tuple[str, ...]],
                 author_of: Callable[[str], str | None],
                 is_local_agent: Callable[[str], bool],
                 on_change: Callable[[], None],
                 logger: Callable[..., None] | None = None) -> None
    def index(self) -> LinkIndex                       # any thread; no I/O, no lock
    def intents(self) -> Mapping[str, LinkFile]        # any thread; pending local writes (B13)
    def refresh(self) -> LinkIndex                     # service thread only
    def write(self, file: LinkFile) -> None            # service thread only; records an intent
    def flush(self) -> tuple[str, ...]                 # service thread only; returns actors committed now
    def close(self) -> None
```

规则：

- 空间：composition 的 `spaces()` 返回本节点每个存活 org 的 directory 空间 id（`resolve_org_directory_space`，排除已离开与已删除的 org）。`refresh()` 比较新旧集合。新空间：建 `link` 目录（`mkdir`，已存在忽略），再 `watch(space, "link/*.json", cb)`。消失的空间：关闭 watch，并从各 actor 的候选快照中去掉该空间的贡献。
- 同名目录：离线并发创建产生多个根级 `link` sibling 时，按稳定 node id 访问全部 sibling 并读取全部文件；node id 字典序最小者是 canonical write directory。新文件以 `id:<directory-node-id>/<filename>` 锚定父目录创建，已有文件按自己的 node id 更新。不能按副本到达顺序选目录，也不能让 `ambiguous-path` 单独把 org 判为不可用。
- 可写（B13）：每次 `refresh()` 都重新以 `mkdir` 探测空间，而不只在 attach 时探测。一个空间在最近一次探测或写入成功时进入 `writable`。返回只读、`not-a-member`、`closed` 或探测/写入失败时移出；从可写转为不可写时记 `link.space_unwritable`。`org_available` 只在 `writable` 非空时为 True，临时故障恢复后不需要重启 daemon。
- 读：只读本地副本（`listdir`、`read_text_snapshot`），不做远程拉取。`document_authors(space, node)` 必须非空，且全部等于 `author_of(actor)`（actor 的 owner 的规范 `user:` URI）。下列情况跳过该文件，并记 `link.file_rejected`（每个 (space, node, version) 记一次）：作者不符、JSON 非法、`model.from_document` 抛错、文件名与 `actor` 字段不一致。
- 选快照（B12）：一个 actor 的文件是单写者的一次完整快照。
  - 跨空间取文件 `revision` 最大的快照。⛔ 不按 peer 合并不同快照。
  - 最大修订号的多份副本内容一致：用它。内容不一致：actor 进入 `conflicted`，不进 `files`，记 `link.file_conflict`。⛔ 不按空间顺序挑能放行的那份。
  - 已观测的最高修订号记在 `revisions`。低于它的快照到达时忽略，不回退。
- 暂缺（B11）：曾被观测的 actor 当前没有可读副本时（文件消失、读错、内容未到、空间离开），从 `files` 移除，加入 `absent`。`revisions` 保留。重新出现且修订号 ≥ 已观测最高值时，回到 `files`。
- 写（B13）：
  - `write(file)` 先断言 `is_local_agent(file.actor)`，且 `author_of(file.actor)` 等于本节点 owner。不符抛 `ValueError`，不做任何 I/O。同 owner 的远端 agent 与 `user:` URI 都不能写。
  - 断言通过后把 `file` 放进待写意图 `intents()`。⛔ `index().files` 此时不变。
  - `flush()` 把每个意图以 `model.to_document(file)` 的紧凑、键排序 JSON 写入每个可写空间的 `link/<actor>.json`。已有节点带 `expect_version`（取自最近一次读）。
  - 一个意图在**所有**存活可写空间都写成功后，才进入 `index().files`（已提交），并从 `intents()` 删除。部分失败或全部失败时意图保留，下次 `refresh()` 或 `flush()` 重试。代价：一个持续失败的空间会挡住新铸造；已提交的旧修订继续有效。
  - `stale-write` 时：重读；再次确认 `is_local_agent(file.actor)`；确认意图的修订号仍大于已观测最高值；然后以意图内容重写，并记 `link.file_conflict`。不满足时丢弃该意图，记 `link.intent_dropped`。
  - 镜像重试写的是完整的最新意图，删除的引用随之传播。
- 订阅：watch 回调运行在 orgfs 线程，只调 `on_change()`（service 的 `kick`），不读文件，不持锁。
- 未加入 org：`spaces()` 为空时，`index()` 的各集合为空，`org_available` 为 False。副本未到的文件视为不存在。

### 1.2 外部端口类型（放在 `store.py`）与 `links/service.py`

service 经下列 Protocol 取得外部事实。实现全部在 composition，service 不 import inbox 或 PAC。

```python
@dataclass(frozen=True, slots=True)
class HeldRow:       message_id: str; sender: str; target: str; intent: str; reply_to: str | None
                     hold_reason: str; created_at_ms: int; expires_at_ms: int
@dataclass(frozen=True, slots=True)
class InboundRow:    message_id: str; sender: str; recipient: str; intent: str; reply_to: str | None
                     received_at_ms: int; fetched_at_ms: int | None; injected: bool
@dataclass(frozen=True, slots=True)
class LedgerRow:     actor: str; message_id: str; direction: Literal["in", "out"]; peer: str
                     intent: str; reply_to: str | None; state: str; opened_at_ms: int
                     sent_at_ms: int | None; event_at_ms: int | None; threshold_ms: int | None
                     sample_pending: bool; expires_at_ms: int
@dataclass(frozen=True, slots=True)
class CensoredRecord: actor: str; message_id: str; requester: str; received_at_ms: int
                      threshold_ms: int; expires_at_ms: int                       # B9
# defaults written inline for brevity; implement Mapping/tuple defaults with field(default_factory=...)
@dataclass(frozen=True, slots=True)
class LedgerUpdate:  release: tuple[str, ...] = (); reasons: Mapping[str, str] = {}
                     fail: Mapping[str, str] = {}; drain: Mapping[str, tuple[str, ...]] = {}
                     cancel_outbound: Mapping[str, str] = {}; release_outbound: tuple[tuple[str, str], ...] = ()
                     censor: tuple[CensoredRecord, ...] = (); sampled: tuple[tuple[str, str], ...] = ()
                     open_outbound: tuple[LedgerRow, ...] = (); close_inbound: tuple[tuple[str, str, int], ...] = ()
                     bind_tasks: tuple[tuple[str, str, str, int], ...] = ()
                     unbind_tasks: tuple[tuple[str, str, str], ...] = (); purge_before_ms: int | None = None
@dataclass(frozen=True, slots=True)
class LedgerResult:  released: tuple[str, ...]; expired: tuple[str, ...]; failed: tuple[str, ...]
                     drained: tuple[tuple[str, str], ...]; cancelled: tuple[str, ...]

class LinkInboxPort(Protocol):
    def ledger_snapshot(self, now_ms: int) -> tuple[LedgerRow, ...]: ...      # every unexpired row
    def task_bindings(self) -> tuple[tuple[str, str, str, int], ...]: ...      # (actor, peer, task_id, bound_at_ms)
    def held_outbox(self) -> tuple[HeldRow, ...]: ...
    def inbound_live(self, actor: str, peer: str | None = None) -> tuple[InboundRow, ...]: ...  # incl. not yet injected
    def inbound_row(self, message_id: str) -> InboundRow | None: ...          # live row or None
    def apply(self, update: LedgerUpdate, *, now_ms: int) -> LedgerResult: ...  # one inbox transaction

class LinkPresencePort(Protocol):
    def online_agents(self) -> tuple[str, ...]: ...       # canonical agent URIs currently online

class LinkPacPort(Protocol):                              # B16; read-only PAC durable state
    def task_states(self, task_ids: frozenset[str]) -> Mapping[str, Literal["active", "ended", "unknown"]]: ...
    def worker_active(self, task_id: str, worker: str) -> bool | None: ...   # None = unknown
```

服务：

```python
class LinkService:   # LinkGate, LinkSendObserver, LinkQueries, LinkAdmin, LinkChangeSource; LinkLedgerSink by duck typing
    def __init__(self, *, store: LinkStore, inbox: LinkInboxPort, presence: LinkPresencePort,
                 pac: LinkPacPort | None,
                 is_local_agent: Callable[[str], bool],
                 local_agents: Callable[[], tuple[str, ...]],
                 internal_actor_names: frozenset[str],
                 publisher: HookEventPublisher | None,
                 clock_ms: Callable[[], int], message_ttl_ms: int,
                 constants: ControllerConstants = DEFAULTS, seed: int | None = None,
                 tick_interval_ms: int = TICK_INTERVAL_MS,
                 logger: Callable[..., None] | None = None) -> None
    # lifecycle
    def start(self) -> None          # synchronous: ledger snapshot, link scan, held rows, first pass; then the thread
    def close(self, timeout: float = 5.0) -> bool
    def kick(self) -> None           # any thread; request a pass soon; never blocks
    def tick(self, now_ms: int | None = None) -> None   # one pass; the thread calls it; tests call it directly
    # LinkGate (ports)
    def admit_send(self, *, sender, target, intent, reply_to, now_ms) -> SendDecision
    def sender_known(self, *, recipient, sender) -> bool
    def admit_dispatch(self, *, recipient, sender, message_id, now_ms, intent="request", reply_to=None) -> bool
    def settle_hint(self, *, actor, message_id) -> SettleHint
    # LinkSendObserver (ports, ruling 43)
    def submitted(self, *, sender, target, message_id, reply_to, hold_reason, now_ms) -> None
    # LinkLedgerSink (inbox/contracts/ports.py; inbox actor thread, after commit, memory only)
    def injected(self, *, actor, message_id, sender, intent, reply_to, received_at_ms) -> None
    def reply_received(self, *, actor, reply_message_id, reply_to, sender, received_at_ms) -> None
    def discard(self, *, actor, message_ids) -> None
    # LinkQueries / LinkAdmin / LinkChangeSource (ports)
    def list_links(self, actor) -> Mapping[str, Any]
    def stats(self, actor) -> Mapping[str, Any]
    def add(self, *, actor, peer, now_ms) -> Mapping[str, Any]
    def drop(self, *, actor, peer, now_ms) -> Mapping[str, Any]
    def subscribe(self, callback) -> Callable[[], None]
    # composition-only
    def bind_task(self, *, principal: str, worker: str, task_id: str, now_ms: int) -> Callable[[str], None]
    def release_task(self, *, task_id: str, now_ms: int, worker: str | None = None) -> None
    def observe_hook(self, event: HookEvent) -> None    # bus lane: worker.cleanup.complete, graph.closed (wake-up only)
    def top_fields(self, actor: str) -> Mapping[str, int | None]
```

模块常数（工程常数，不是控制器常数，不进 `ControllerConstants`）：`TICK_INTERVAL_MS = 1000`、`RESERVATION_TTL_MS = 30_000`、`SAMPLE_CAP = 1024`。`SAMPLE_CAP` 是每 actor、每类、每 peer 的样本上限，按时刻淘汰最旧的；淘汰只动样本队列，不动在途记录或审查标记（B17）。保留期一律取 `message_ttl_ms`（composition 传 inbox 的 durable TTL）。

内存状态（每个本节点 actor 一份 `_ActorState`，由内存锁 `_mem` 保护，见 §3.5；重启时由 `ledger_snapshot` 重建，§3.9）：

| 字段 | 内容 | 来源 |
| --- | --- | --- |
| `controller` | `ControllerState` 与最近一次 `ControllerOutput` | `initial_state` / `evaluate` |
| `admitted` | 已准入、未结算的入站 id | `admit_dispatch` 首次 True；`injected`；ledger 的 `in` 行；`discard` 与结算时删除 |
| `pending_inbound` | `message_id -> PendingRequest`：无 `reply_to` 的已准入或已注入入站（B3），`received_at_ms` 取 inbox 列（B7） | 同上 |
| `censored_inbound` | `message_id -> CensoredRecord`（B9）。不占 W；仍 pending 时可回复 | `policy.censored_inbound` 经 ledger 提交后 |
| `outbound` | `OutboundRequest` 列表：已接受、未收到回复、未放弃。`sent_at_ms` 取实际发出时刻，未发出时取接受时刻（B7） | `submitted`、hold 放行结果、ledger 的 `out` 行 |
| `reservations` | `(target, reserved_at_ms)` 列表 | `admit_send` 对请求返回 Allow 时预留；`submitted` 消耗；过 `RESERVATION_TTL_MS` 丢弃 |
| `held` | `message_id -> (HeldRow, detail)`。行来自 durable outbox；detail 取本次重评结果，重启后为空串，直到下次重评（B15） | `held_outbox()`；`submitted(hold_reason=...)` 只登记 id 并 `kick()` |
| `reply_credits` | `reply_message_id -> (peer, reply_to)`：已在接收事务里闭合出站的受管回复 | `reply_received`；该回复结算后删除 |
| `reclaimed` | `peer -> reclaimed_at_ms`：回收屏障 | 回收；两份引用都**明确**撤回后清除（B11） |
| `recent_reclaims_ms` | 近期回收时刻 | 回收（按 `period_ms` 修剪） |
| `drained` | 本 actor 的排干 id（按 `(actor, message_id)`，B10） | 回收残余；保留至 TTL |
| `released_outbound` | 已放弃或已释放的出站 id | `abandoned`、残余；保留至 TTL；迟到回复据此忽略 |
| `task_bindings` | `peer -> {task_id}` | `bind_task`；`task_bindings()`；任务结束 |
| `established` | 当前已成立的 peer 集合（用于发事件与识别对端撤回） | 每次索引变化重算 |
| `last_used` | `peer -> last_used_ms` | Allow 与回复；`model.touch` 只在下次结构性写入时随文件落盘 |
| 样本 | `DwellSample`、`RttSample`、`ReplySample` 的有界双端队列 | §3.1 |

全局：`_seen: dict[agent, last_seen_ms]`（N_active，24 小时窗口）、`_subscribers`、`_rng`、`_observed: set[(sender, message_id, kind)]`（`submitted` 的幂等表，保留至 TTL）。

### 1.3 行数与文件数预算

行数以 `wp4/send-links` @ `8c720faf6` 为参照。实施开始时在集成分支 head 重测，把实测值写进证据文件（A4）。

| 文件 | 参照 | 本包净增（估） | 上限 |
| --- | --- | --- | --- |
| `links/store.py` | 0 | ≈600 | 1000 |
| `links/service.py` | 0 | ≤950 | 1000（超出按 §0.3 外移） |
| `links/policy.py` | 487 | +3 | — |
| `composition/links.py` | 0 | ≈400 | 1000 |
| `application/wiring/services.py` | 475 | +3 | — |
| `application/wiring/transport.py` | 986 | **0** | 机检：diff 为空 |
| `application/__init__.py` | 990 | **0**（属性一律 `getattr(app, name, None)` 读取） | 机检：diff 为空 |
| `application/shutdown.py` | 904 | +8 | — |
| `inbox_surface/send.py` | 727 | ≈+40 | — |
| `inbox_surface/link_send.py` | 0 | ≈300 | — |
| `status/views.py` | 934 | +15 | — |
| `operations/top.py` | 510 | +12 | — |
| `pac/actors/daemon.py` | 888 | +12 | — |
| `inbox/contracts/ports.py` | 415 | ≈+90 | — |
| `inbox/contracts/api.py` | 249 | ≈+30 | — |
| `inbox/service/core.py` | 675 | ≈+80 | — |
| `inbox/actor/custody/submit.py` | 296 | ≈+35 | — |
| `inbox/actor/custody/retry.py` | 502 | +8 | — |
| `inbox/actor/custody/lifecycle.py` | 568 | ≈+120 | — |
| `inbox/actor/custody/outcomes.py` | 303 | +10 | — |
| `inbox/service/delivery/retry.py` | 838 | +12 | — |
| `inbox/service/delivery/outbox.py` | 330 | +4 | — |
| `inbox/service/delivery/receive.py` | 345 | ≈+30 | — |
| `inbox/service/projection.py` | 543 | ≈+50 | — |
| `inbox/authority/facade.py` | 671 | ≈+80 | — |
| `inbox/authority/read.py` | 705 | ≈+130 | — |
| `inbox_surface/queries.py` | 563 | +3 | — |

composition 接线的净增：`composition/links.py` 新建约 400 行；`application/` 下净增约 66 行（`services.py` +3、`shutdown.py` +8、`send.py` 约 +40、`views.py` +15）；`transport.py` 与 `application/__init__.py` 为 0。WP3-A、WP6、WP7 合入后若任何一个参照文件加本包增量超过 1000，先在证据文件写明，再把本包新增代码移进 `link_send.py` 或 `composition/links.py`。⛔ 不压缩别人的代码来腾行数。

文件数：`links/` 7；`composition/` 7；`application/wiring/` 不变；`inbox_surface/` 6；`tests/daemon/links/` 直接文件 6（现有 5 个加 `test_store.py`；子目录 `admission/`、`service/` 不计）；`tests/daemon/links/service/` 7（`__init__.py` 加 6 个测试文件）；`tests/support/links/` 4。结构门以 `check_structural_limits.py` 为准，它把文档与数据也计入（A4）。

## 2. 接口怎么变

### 2.1 与 WP1 的对接

`NodeView` 由 `_view(actor, now_ms, *, for_reply=False)` 在持 `_mem` 时组装，纯内存：

| `NodeView` 字段 | 取值 |
| --- | --- |
| `actor` | 本节点 actor |
| `links` | 先取 `model.links_of(actor, mine, index.files, reclaimed=state.reclaimed.keys())`，其中 `mine` 为 `store.intents().get(actor)`，没有意图时用 `index.files.get(actor)`。再按 B13 降级：对端在意图里、但不在已提交的本 actor 文件里的 link，`state` 一律改为 `"pending"`。这样待写引用计入 `degree`（预占 k，防并发超额），但不 `effective` |
| `pending_inbound` | `state.pending_inbound`。`for_reply=True` 时并入 `censored_inbound` 中仍 pending 的记录，以 `PendingRequest(message_id, requester, received_at_ms)` 构造（B9：审查后仍可回复） |
| `drained` | 该 actor 的排干 id |
| `outbound` | `state.outbound`，加上每个未过期预留合成的 `OutboundRequest(message_id="reservation:<n>", peer=target, sent_at_ms=reserved_at)` |
| `limits` | 最近一次 `ControllerOutput`。周期 P 只取 `limits.period_ms`，service 不自算周期 |
| `recent_reclaims_ms` | `state.recent_reclaims_ms` |
| `rtt_max_ms` | `{peer: limits.links[peer].t_max_ms}`（非 None 者） |
| `org_available` | `index.org_available` |
| `admitted_ids` | `state.admitted` |
| `peer_full` | 见 §3.3 |

对端在 `index.absent` 或 `index.conflicted` 中时，其文件不进 `links_of` 的输入，link 不 effective。⛔ 这不触发回收（§3.3）。

调用映射：

| 入口 | 调用 |
| --- | --- |
| `admit_send` | 前置步骤（§2.2）后 `policy.admit(view, ...)` |
| `admit_dispatch` | 前置步骤（§2.2）后 `policy.admit_dispatch(view, ...)`；首次 True 时记账 |
| `settle_hint` | 见 §2.2 |
| 被铸造方回写 | `policy.accept_reference(view, requester=R, now_ms, lifecycle=R 条目的 lifecycle)` |
| 自动淘汰 | 只执行 `Mint.evict` 给出的对象（裁定 5），不再调 `select_victim` |
| k 收缩 | `policy.shrink_to_k(view, now_ms)` 返回的每个 peer 走回收 |
| 回收残余 | `policy.residue(view, peer=...)`，再并上 `inbox.inbound_live(actor, peer)` 的全部行（B10，§3.3） |
| 出站放弃 | `policy.abandoned(view, now_ms)`；每个 id 记 1 个 `RttSample(censored=True, rtt_ms=阈值)` |
| 入站审查 | `policy.censored_inbound(view, now_ms)`；每个 id 只记 1 个 `DwellSample(censored=True)`，以 `CensoredRecord` 经 ledger 提交后移入 `censored_inbound` |
| 控制器 | 每 tick 对每个本节点 actor 调 `controller.evaluate(state, ControllerInput(...), now_ms, jitter=rng.uniform(-1, 1), constants)`；`evaluate` 自己按 `next_eval_ms` 判断是否到期 |

`ControllerInput` 的组装：

- `n_active = |{a ∈ _seen, now − last_seen ≤ 24h} − {actor}|`。`_seen` 只收 `agent:` URI；owner 是 `user:`，自然不含。
- `links` 取已成立的 peer。
- `unanswered = {peer: 最老在途请求的 sent_at_ms}`。
- `held_peers` 只取**本次 pass 重评**中仍判为 `Hold(LINK_WINDOW_FULL, peer, "cwnd")` 的 peer（B15）。W、minting、org 不可用、副本延迟与重启后未重评的行都不算。

`evaluate` 返回后，若 `k` 下降则执行 `shrink_to_k`。

### 2.2 `LinkGate` 与 `LinkSendObserver` 的实现

`admit_send(sender, target, intent, reply_to, now_ms)` 按下列步骤执行：

0. **发送者**（B2）：
   - `sender` 不是 `agent:` URI（`user:`、`adapter:`、`system:` 等）：返回 `Allow(peer=None)`。这些不是受管发送者。
   - `sender` 是 `agent:` 但 `is_local_agent(sender)` 为 False（他节点 agent，或同 owner 的远端 agent）：抛 `DaemonRequestError(CALLER_NOT_AUTHORIZED, data={"sender": sender})`。⛔ 不返回 `Allow` 再记日志。
   - `is_local_agent` 按本节点 registry 判：node 段等于本 daemon，且该规范 URI 属于本节点 registry 持有的 actor。不看 owner、在线状态；托管（hosted，owner 是访客）在本节点运行，因此是本地，本 daemon 写它的 link 半文件；同 owner 但 node 段不同的 agent 仍是非本地。本节点 agent 第一次出现时建 `_ActorState`。（2026-10-17 修正：原「owner 与 node 段相等」把 hosted worker 判成远端，导致 driver 的引用永远停在 pending / `LINK_MINTING`。）
1. **回复**（`reply_to` 非空，B3）：先校验，再豁免。
   1. `reply_to` 在该 actor 的排干集合中：抛 `LINK_RECLAIMED`。
   2. 取原请求：先查内存 `pending_inbound` 与 `censored_inbound`。不在内存时，在锁外调 `inbox.inbound_row(reply_to)`（IPC 线程，可做读 I/O）。行存在、`recipient == sender` 且无 `reply_to` 时，作为 `PendingRequest` 临时并入视图。原请求不存在：抛 `MESSAGE_REPLY_UNAVAILABLE`。
   3. requester 与 target 必须相等，否则抛 `INVALID_ARGUMENT`。`route:` 目标例外：route 地址不等于原发送者的 `channel:`/`adapter:` URI，此处只校验存在与归属，传输能力由现有 route 检查负责。
   4. 校验通过后再应用豁免：target 为 `user:`、`route:`、自身或内部 actor 名时返回 `Allow(peer=None)`。闭合与 T 样本照常记账（`submitted` 与 ledger，§3.1）。
   5. 其余情况以 `for_reply=True` 的视图调 `policy.admit`。`DaemonRequestError` 原样抛出。
2. **非回复的豁免目标**：`target == sender`、`target` 为 `route:`、`parse_agent_uri(target)` 的 actor 段在 `internal_actor_names` 中（不限节点，裁定 45、51，B1）：返回 `Allow(peer=None)`，不记账。
3. **`user:` 目标**（裁定 37、44）：`policy.governed` 已对 `user:` 返回 False，`policy.admit` 返回 `Allow(peer=None)`。service 按 §3.1 为该请求预留，并计入发送方的 W 与 `outboundInFlight`。W 满时的行为见 §3.8 与 §9 C1。
4. **同对端 FIFO**：该 (sender, target) 已有 held 请求时，直接返回与队首同原因、同 detail 的 `Hold`。
5. 调 `policy.admit`。
6. **决策后处理**（持锁）：
   - `Allow(peer=P)` 且为请求：预留。
   - `Mint(peer, evict)`：有 `evict` 时先回收它（原因 `evicted`），再用 `model.with_reference`（lifecycle `regular`）生成本 actor 的新文件，交给 `store.write`（只成为意图，B13），然后 `kick()`。
   - `Hold`：不改状态。

`sender_known(recipient, sender)`：只读内存，不加锁。以下任一成立时为 True：

- `sender` 的 actor 段在 `internal_actor_names` 中（裁定 51）；
- `store.index().known(sender)`：已提交、暂缺或冲突都算，B11：暂缺不是「从未加入」；
- `sender` 是本节点 agent、`index.org_available`，且 `sender` 的文件已提交。只有意图、补建空文件尚未写成功时为 False（B13）。

`admit_dispatch(recipient, sender, message_id, now_ms, intent, reply_to)`：持 `_mem`，组装视图（不做 I/O）。

1. `sender` 或 `recipient` 的 actor 段在 `internal_actor_names` 中：返回 True，不记账（裁定 51；WP4 的 `link_governed` 尚未按名判时的兜底，见 §7 S2）。
2. `(recipient, message_id)` 在 `reply_credits` 中，且 peer 与 `reply_to` 都相符：返回 True，加入 `admitted`，不进 `pending_inbound`，不占 W。原因：该回复已在接收事务里闭合了出站，见 §3.1。
3. 其余情况调 `policy.admit_dispatch`。首次返回 True（id 不在 `admitted`）时，加入 `admitted`。`reply_to` 为空时，不论 intent，同时加入 `pending_inbound`，`received_at_ms` 暂取 `now_ms`，由 ledger 与 reconcile 校正（B3）。`reply_to` 非空的不进 `pending_inbound`。
4. 返回 policy 的结果。

`policy.admit_dispatch` 对 `intent="reply"` 且 `reply_to` 命中同一 peer 的开放出站时免 W。命中错误 peer 时照常受 W 约束，不消耗他人的出站（B3）。

`settle_hint(actor, message_id)`：WP3 只在 inbox 判定仍 pending 时才问（WP3 §2.7）。`message_id` 在该 actor 的排干集合时返回 `"ack"`，否则返回 `"keep"`。⛔ 不用 `policy.settle_hint` 的「不在 pending_inbound 即 ack」分支：reconcile 有滞后，那会让载体 ack 一条仍待回复的请求。

`submitted(sender, target, message_id, reply_to, hold_reason, now_ms)`（裁定 43）只改内存，按 `(sender, message_id, kind)` 幂等（kind 取 `held`、`reply` 或 `request`），重复调用不再计数、采样或闭合（B6）。durable 记账已由发送边界在 submit 事务内写入（§2.4、§3.9）。

- `hold_reason` 非空：只登记 id 并 `kick()`。完整的 `HeldRow` 由下一次 pass 从 `held_outbox()` 读取（B15）。不预留，不加 `outbound`。
- `reply_to` 非空，且回复已 durable 接受、未被 hold：从 `pending_inbound`/`censored_inbound` 删除该请求。未审查过的，记 `DwellSample(at_ms=now, dwell_ms=max(1, now − received_at_ms))`，其中 `received_at_ms` 取 inbox 列（内存值来自 `injected` 或 ledger，B7）。再记 `ReplySample(direction="sent")`，然后 `kick()`。
- 其余（请求已 durable 接受）：消耗一个该 target 的预留，追加 `OutboundRequest(message_id, peer=target, sent_at_ms=now)`。`sent_at_ms` 之后由 ledger 的实际发出时刻校正（B7）。

`injected(actor, message_id, sender, intent, reply_to, received_at_ms)`（B8）：inbox actor 线程在注入事务提交后调用，只改内存。`reply_to` 为空、且（`sender` 受管，或 `intent == "request"`）时，加入 `admitted` 与 `pending_inbound`（`received_at_ms` 取参数），已在内的只校正 `received_at_ms`。owner 的请求由此计入节点负载，但不经 W 准入（设计 §3.2）。豁免来源的非请求（如 system notice）不计。

`reply_received(actor, reply_message_id, reply_to, sender, received_at_ms)`（B7）：接收事务提交后调用，只改内存。`reply_to` 命中该 actor 的开放出站、且 peer 等于 `sender` 时：

- 闭合该出站；
- 记 `RttSample(peer=sender, at_ms=received_at_ms, rtt_ms=max(1, received_at_ms − sent_at_ms))` 与 `ReplySample(direction="received", peer=sender)`；
- `touch` last_used；
- 写 `reply_credits[reply_message_id]`；
- `kick()`。

命中 `released_outbound` 的迟到回复忽略。

`discard(actor, message_ids)`：注入或准入事务回滚时调用，从 `admitted`、`pending_inbound` 删除这些 id（B14）。

### 2.3 与 WP4 `LinkAdmission` 的绑定

- `compose_link_service` 在 `app.link_admission.bound` 为 False 时创建 service，并调 `app.link_admission.bind(service)` 一次。
- 已绑定时（隔离测试或 E2E 开关 `HYPRIAL_E2E_PERMISSIVE_LINK_GATE=1` 已绑放行门）：不创建 `LinkService`，记 `link.service_skipped`，`_link_service` 保持 None，并把 `app._link_gate` 设为同一个已绑定的门（`app.link_admission.gate`，§7 S1）。理由（B2）：显式测试放行门必须同时作用于发送与接收路径，不能靠 `_link_gate is None` 放行。
- `bind` 之前先 `service.start()`：同步读 ledger 快照、扫描 link 文件、读 hold 行、做一次 pass。这样绑定后第一次 `admit_dispatch` 已能看到重启前的已准入集合（ports 的幂等契约跨重启成立）。
- 内部 actor 名（B1，裁定 45、51）：composition 取 `app.link_admission.internal_actors`（WP4 暴露的只读属性），对每个 URI 取 `parse_agent_uri(uri).actor`，得到 `internal_actor_names` 传给 service。⛔ 不复制 WP4 的构造表达式，links 不 import dispatch。
- `is_local_agent`：composition 以 node 段等于 `app.node_id` 且 URI 属于 `app.agents.local_actors()`（registry 权威）判定；owner 不参与。hosted agent 因此是本地。
- `LinkChangeSource.subscribe` 由本包实现。composition 当前不订阅（inbox 没有对外的 pending.wait 唤醒入口），WP4 的 `pending.wait` 继续以 5 s 轮询重评。见 §7 S4。
- `app.inbox.bind_link_ledger_sink(service)`：在 `start()` 之后、`bind` 之前设置。

### 2.4 与 WP3 的对接：`message.send` 与 `message.reply`

composition 设 `app._link_gate = service`。WP3 的 `settle_hint` 与 `read_body` 经它调 gate。WP3 的 `read_body` 可能在 IPC 线程调 `admit_dispatch`；service 的 `_mem` 使该调用线程安全。

`message.send` 对每个目标（`link_send.py`，B6 的顺序）：

1. 按现有规则算出稳定的 message id（agent 目标用 `idempotencyKey` 派生的 uuid5，`user:` 目标用 `target_key` 派生的 uuid5）。
2. **重放识别**（B6）：
   - 以 `inbox.submission_result(message_id)` 查已有回执（facade 没有该读时本包加；`user:` 目标沿用 `user_delivery` 的 `duplicate` 语义）。
   - 已有回执：返回原操作的 durable 结果；原行仍 held 时带 `queued: true` 与 `holdReason`。⛔ 不再问 gate，不预留，不铸造，不投递。
   - digest 冲突沿用现有冲突行为。
3. gate 为 None：沿用现状直接 submit（裁定 26 的缺省，§9 C2）。
4. 调 `gate.admit_send`，异常原样抛出（多目标时整次 send 失败，与现有错误路径一致）。
5. 按决策：
   - `Allow`：`submit(message, link_open=...)`。受管请求与 `user:` 请求带 `LinkLedgerOpen`。`accepted` 后调 `submitted(hold_reason=None)`。
   - `Mint`：`submit(message, link_hold=LINK_MINTING, link_open=...)`，再调 `submitted(hold_reason=LINK_MINTING)`。delivery 行为 `{"accepted": true, "queued": true, "holdReason": "LINK_MINTING", "detail": "minting"}`；`evict` 非空时加 `"evicted": <peer>`。
   - `Hold(reason, peer, detail)`：`submit(message, link_hold=reason, link_open=...)`，再调 `submitted(hold_reason=reason)`。delivery 行为 `{"accepted": true, "queued": true, "holdReason": reason, "detail": detail}`。
   - submit 被拒、抛错或回执为 `SUBMIT_OUTCOME_UNKNOWN`：⛔ 不调 `submitted`。预留在 `RESERVATION_TTL_MS` 后自动释放（B6）。
6. `user:` 目标：不入 outbox。
   - 投递前，经 `inbox.update_link_ledger(open_outbound=...)` 写 ledger 的 `out` 行；投递失败时在同一请求内删除该行（`release_outbound`）。
   - 投递成功后调 `submitted(hold_reason=None)`。
   - 带 `replyTo` 的 `user:` 回复：投递成功后经 `close_inbound` 闭合原请求。
7. 键名一律用 `HOLD_REASON_KEY`（裁定 15），⛔ 不写 `reason` 键。单目标时顶层也带 `queued: true` 与 `holdReason`（与 WP6 的 `_lift_hold` 一致）。

`message.reply`（agent 目标，B5、B6）：

1. 查原请求：先查 `pending_messages`。找不到时，查确定性回复 id 的已有回执；有回执则返回既有回执（B6：原请求已结算也一样），digest 冲突沿用现有行为。
2. 两者都没有时，调 `gate.admit_send(reply_to=messageId)`：排干的 id 由它抛 `LINK_RECLAIMED`，其余抛 `MESSAGE_REPLY_UNAVAILABLE`（B5）。⛔ 不要求已 ack 的排干请求仍出现在 `pending_messages`。
3. 原请求在：调 `gate.admit_send(reply_to=messageId)`，异常原样抛出，⛔ 不 ack 原请求，不入 outbox。
4. `Allow`：`submit(reply, link_reply_close=True)`，同事务闭合 ledger 的入站行。之后沿用现有「accepted 即 ack」路径，再调 `submitted(reply_to=...)`。
5. `Hold`：`submit(reply, link_hold=reason)`。⛔ 不走「accepted 即 ack」分支，原请求保持 pending。返回 `{"ok": true, "messageId", "replyMessageId", "replied": false, "acknowledged": false, "queued": true, "holdReason": reason, "detail": detail}`。⛔ 不用表示失败的 `REPLY_SETTLEMENT_PENDING` 代替 link hold。放行见 §3.6。
6. reply body 对 agent 目标也写 `replyTo: messageId`（现状只对 adapter 写）。接收方的 dispatch 关联（WP4 从 payload 的 `replyTo` 取）与接收事务的出站闭合（§3.1）都依赖它。升级前后 digest 不同属于接受的行为（同裁定 39 的理由）。

adapter 与 channel 的回复不经 gate，但仍经 `link_reply_close` 闭合原事项、记 T（B3）。

### 2.5 与 WP6 IPC 的对接

WP6 router 的 `link.*` 分支取 `getattr(self, "_link_service", None)`，为 None 时返回 `DAEMON_NOT_READY`。`list`/`stats` 调 `list_links(actor)`/`stats(actor)`；`add`/`drop` 调 `add(actor=, peer=, now_ms=)`/`drop(...)`。结果展开为 `{"ok": True, **mapping}`。composition 设 `app._link_service = service`。返回形状（WP6 §2.1，CLI 容忍缺键）：

- `list_links(actor)`：`{"actor", "orgAvailable": bool, "k", "W", "inFlight", "owner": {"peer": <owner user URI>}, "links": [{"peer", "state", "cwnd", "inFlight", "lifecycle", "taskId", "taskIds", "mintedAtMs", "lastUsedAtMs", "revision", "mine", "theirs", "committed"}], "held": [{"messageId", "target", "holdReason", "detail", "heldMs"}]}`。
  - `state` 取 §2.1 降级后的 `Link.state`。
  - 未成立的 link，`cwnd` 为 None。
  - `committed` 为 False 表示本侧引用仍是待写意图。
- `stats(actor)`：`{"actor", "nActive", "k", "W", "wTarget", "tMinMs", "tMaxMs", "tBarMs", "muMax", "diff", "grad", "periodMs", "links": [{"peer", "rttMinMs", "rttMaxMs", "rttMs", "cwnd", "inFlight"}]}`，取自最近一次 `ControllerOutput`。`muMax` 换算为每秒。
- `add(actor, peer, now_ms)`：
  - `peer` 不是 `agent:`、等于 actor、与 actor 为 owner 对、或 actor 段在 `internal_actor_names` 中：抛 `DaemonRequestError(INVALID_ARGUMENT)`。
  - 已有我侧引用（已提交或意图）：返回 `{"actor", "peer", "state", "revision", "committed"}`。重放幂等。
  - 否则按 `policy.admit` 的新请求分支求决策（`target=peer`，无 reply_to）：
    - `Mint`：执行（含 `evict`），返回 `{"state": "pending", "committed": false, "revision", "evicted"?}`；
    - `Allow`（已成立）：返回 `{"state": "established"}`；
    - `Hold`（A3）：返回 `{"actor", "peer", "state": "absent", "written": false, "queued": false, "holdReason", "detail"}`。不写引用，不保存待执行意图；运维稍后重试。
- `drop(actor, peer, now_ms)`：
  - 有我侧引用或已成立：回收（原因 `dropped`），返回 `{"actor", "peer", "state": "reclaimed", "drained": n, "released": m, "cancelled": c}`。
  - 没有：返回 `{"actor", "peer", "state": "absent"}`。
  - PAC 绑定的 link 也可 drop（运维显式操作），同时清除其全部任务绑定。

### 2.6 `top` 字段

`service.top_fields(actor)` 返回 `{TOP_FIELD_CAPACITY_W: limits.w, TOP_FIELD_OUTBOUND_IN_FLIGHT: len(outbound)}`。actor 不是本节点 agent、或 service 未接线时，两值为 None（WP6 渲染为 `—`）。`outbound` 不含预留。`views.py` 以 `link_fields={actor: service.top_fields(actor) for actor in ...}` 传入。`top.py` 的 `build_actor_row` 加参数 `link_fields: Mapping[str, int | None] | None = None`，把两个键写入 actor 行（缺省 None）。读取 `top` 不改任何状态，不调 gate。

### 2.7 事件 payload

经 `HookEventPublisher(app._hook_bus, source="daemon.links")` 发布。`actor` 为本节点 actor，`emitted_at_ms` 为事件时刻。字段只用经 kernel 的 `safe_turn_identifier` 处理过的字符串与整数。

- `link.minted`：本节点 actor 首次在已提交的本地副本里看到与 peer 的两份互引。每次成立发一次；启动时已成立的不补发。payload：`{"actor", "peer", "lifecycle": "regular"|"pac_task", "taskId": str|None, "revision": int, "mintedAtMs": int, "atMs": int, "side": "minter"|"acceptor"}`。
- `link.reclaimed`：本节点 actor 回收一条 link。payload：`{"actor", "peer", "lifecycle", "taskId", "reason": "evicted"|"dropped"|"task_ended"|"k_shrink"|"peer_withdrew", "drained": int, "drainedMessageIds": [str…]（至多 32 个）, "released": int, "cancelled": int, "atMs": int}`。副本暂缺不发此事件（B11）。

两端 daemon 各自对自己的 actor 发事件。事件不是判定依据：service 自己的状态从不靠事件回读。

## 3. 关键决策

### 3.1 样本与计数的来源：边界直喂加 ledger 对账，不走 bus（裁定 10、46）

样本与计数由下列边界直接喂入 service。service 不订阅 `delivery.*`，`DaemonHookService.observe_delivery` 不改。

| 事实 | 喂入点（内存） | durable 记账（同事务） | 线程 |
| --- | --- | --- | --- |
| 本节点接受一条请求（出站在途 +1） | `submitted(reply_to=None)`；hold 放行时由 service 按 `apply` 结果自记 | submit 事务写 `out` 行（`held` 或 `open`）；放行事务把 `held` 改为 `open` | IPC / service |
| 请求实际发出（RTT 起点） | ledger 对账校正 `sent_at_ms` | outbox 行投递成功删除的同一事务写 `sent_at_ms` | inbox actor |
| 本节点发出回复（T 样本、`ReplySample(sent)`、入站闭合） | `submitted(reply_to=...)`；hold 放行时由 service 自记 | submit 事务（`link_reply_close`）或放行事务把 `in` 行改为 `closed` | IPC / service |
| 入站被注入（入站在途 +1，含豁免来源，B8） | 受管：`admit_dispatch` 首次 True；全部：`injected` | 注入事务写 `in` 行（`INSERT OR IGNORE`） | inbox actor（`read_body` 可能在 IPC 线程） |
| 回复到达（RTT 样本、`ReplySample(received)`、出站闭合，B7） | `reply_received` | 接收事务把匹配的 `out` 行改为 `closed`，写 `event_at_ms = received_at_ms`、`sample_pending = 1` | inbox actor |
| 结算（ack、过期、终态失败、排干） | 每 tick 对账 | ack 或过期事务把 `in` 行改为终态；排干由 `apply` 写 | service |

理由：

1. bus 的 `delivery.*` 只覆盖 headless 派发。interactive 的 `pending.list/wait`、回复的发出与到达、ack 都不在其中。
2. bus 的通道有界且可丢，而 W 与 cwnd 用精确计数，计数丢一次就永久漂移。边界调用是同步的，不丢。
3. 回复关联只在发送边界（`reply_to` 参数）与接收事务（payload 的 `replyTo`）可靠可见。
4. ledger 与消息状态同事务提交，所以重启、崩溃、tick 前的快结算都不丢账（B14）。内存回调只是让计数即时生效；每 tick 的对账以 ledger 与 inbox 存活行为准。

时刻（B7）：

- 所有 T 都用 inbox 的 `received_at_ms`。
- 所有 RTT 都用回复的 `received_at_ms − sent_at_ms`，`sent_at_ms` 为实际发出时刻。延迟的 dispatch、读 body 与 service tick 都不计入。
- held 不算已发出。普通 outbox 排队期间计入 W，但 RTT 从实际发出算起。

方向（裁定 3，B6）：

- `ReplySample(direction="sent")` 只在本节点发出回复时记。
- `ReplySample(direction="received", peer=P)` 只在本节点在 link P 上收到回复时记。
- `DwellSample` 只来自本节点回复入站请求。
- `RttSample` 只来自本节点的请求收到匹配回复。
- 审查样本只记一次，不生成 `ReplySample`，不进 T_max 与 RTT_max 的基线（裁定 34）。

每 tick 的对账规则：

- 读 `ledger_snapshot(now)`，与内存逐条比对：
  - `in` 行终态（`closed`、`acked`、`expired`、`drained`）：从 `admitted`、`pending_inbound`、`censored_inbound` 删除。ack 与过期不记样本（ack 不是回复）。
  - `in` 行 `open` 而内存缺：补入 `admitted` 与 `pending_inbound`，`received_at_ms` 取行值。
  - `out` 行：`open` 的以行值校正 `sent_at_ms`。`closed` 且 `sample_pending` 的：若内存未经 `reply_received` 闭合（回调丢失、重启、快回复），以行值闭合并记 RTT；然后把该行加入下一次 `apply` 的 `sampled`，保证只采样一次（B7）。
  - `in` 行 `closed` 且 `sample_pending`：若内存未经 `submitted` 记过 T，以 `event_at_ms − opened_at_ms` 记 T；同样回写 `sampled`。
- `censored` 记录（B9）：内存以完整 `CensoredRecord` 保存。对账时 inbox 行仍存活，不会把它加回 `pending_inbound`。行一旦终态，审查记录只留去重标记（保留至 TTL），不再可回复。
- `reply_to` 命中 `released_outbound` 的迟到回复：接收事务不闭合（行已是 `released`），service 也不记样本（wp1 B9）。
- 内存 `admitted` 中有、ledger 中没有、且已超过一个 tick 的 id：视为回滚，删除（`discard` 丢失时的兜底，B14）。

### 3.2 hold 重评的触发与四种原因的放行条件（裁定 47）

触发（任一发生即 `kick()`，service 线程合并为一次 pass）：

| 触发 | 来源 |
| --- | --- |
| link 文件变化 | orgfs watch 回调 → `kick()` |
| 回复到达、入站结算、出站放弃 | `reply_received`、`submitted(reply_to)`、对账结果 |
| 新 hold 登记 | `submitted(hold_reason=...)` |
| 周期 tick | service 线程每 `TICK_INTERVAL_MS` 一次，覆盖时间型条件：最短存活期、抖动窗口、控制器周期、org 加入、预留过期、PAC 生命周期核对 |
| 运维操作 | `add`、`drop`、PAC 绑定与释放 |

一次 pass 的顺序：

1. `store.refresh()`，再 `store.flush()`（写待写意图）。
2. 刷新 `_seen`。
3. 读 `ledger_snapshot`、`held_outbox()`、`task_bindings()`（锁外），对账（持锁）。
4. 处理文件差异：回写引用、识别成立与撤回、发事件。
5. 控制器：`evaluate`、`shrink_to_k`、`abandoned`、`censored_inbound`。
6. PAC 生命周期核对（§3.7）。
7. 重评 hold。
8. 一次 `inbox.apply(LedgerUpdate)`：放行、改原因、失败、排干与取消、审查、放弃释放、`sampled`、任务绑定、清理过期记录。
9. 按 `LedgerResult` 合并内存。
10. `store.flush()`（本次 pass 新增的意图）。
11. 通知订阅者（有放行相关变化时）。

重评：按 `created_at_ms` 升序遍历每个 actor 的 held 行。

- 同一 (sender, target) 的请求保持 FIFO：前一条仍 hold 时，后一条不放行。回复不受此约束（回复不占 cwnd）。
- `expires_at_ms ≤ now` 的行先判过期（B15）：不再求决策，不 Mint，不重写引用，不加在途。它由 retry claim 写 `TTL_EXPIRED`，或在本次 `apply` 中以 `fail={id: TTL_EXPIRED}` 终结。两条路径只产生一个终态，见 §3.6。
- 其余行以当前视图调 `policy.admit`：
  - `Allow`：加入 `release`，并在视图中**暂记**为在途（请求加到 `outbound`；回复标为已闭合），再评下一行，避免一次放行超出 W/cwnd。暂记只在本次重评内有效。`apply` 返回后，只有 `LedgerResult.released` 中的 id 进入内存 `outbound`，或记 T 并闭合（B15）。未成功的 id 下次 pass 重评，不重复计数。
  - `Mint`：执行铸造（含 `evict`），原因改为 `LINK_MINTING`。
  - `Hold`：原因变化时加入 `reasons`。detail 只存内存（B15）。
  - `DaemonRequestError`：加入 `fail`，码取异常码；inbox 把该行转 DLQ。例如回复的请求已排干时为 `LINK_RECLAIMED`；原请求已 ack 或过期、且未排干时为 `MESSAGE_REPLY_UNAVAILABLE`。
- `apply` 抛错或超时：内存不变，下次 pass 重做。

四种原因的放行条件：

| 原因 | 何时被判 | 放行条件 |
| --- | --- | --- |
| `LINK_MINTING` | 我侧引用已写或待写，对端引用在已提交副本不可见，且对端不在 `peer_full` | 我侧引用已提交，且对端已提交的文件引用了 sender（互引成立）。然后照常查 cwnd 与 W，可能转为 `LINK_WINDOW_FULL` |
| `LINK_PEER_FULL` | 同上，但对端在 `peer_full`（§3.3） | 对端常规引用数降到估计 k 以下（转回 `LINK_MINTING`），或对端回写引用（成立） |
| `LINK_SELF_FULL` | `degree ≥ k` 且无可淘汰对象（detail `k`）；回收过频（`churn`）；本节点无可写 org 空间（`org-unavailable`） | `k`：某条常规 link 过最短存活期且空闲（出现 victim，转为 `Mint(evict)`），或一条 link 被回收，或 k 因 N_active 上升而增大；`churn`：近一个周期内回收数降到上限以下；`org-unavailable`：`store.refresh()` 发现存活且可写的 link 空间 |
| `LINK_WINDOW_FULL` | link 已成立，`inflight(peer) ≥ cwnd`（`cwnd`）或 `unclosed ≥ W`（`W`） | `cwnd`：该 link 的在途请求被回复闭合、被放弃释放、被回收释放，或 cwnd 增大；`W`：任一入站请求结算（回复、ack、过期、排干、审查）或任一出站闭合，或 W 增大 |

所有 hold 行到 outbox 的 `expires_at_ms` 时转 DLQ，码 `TTL_EXPIRED`（设计 §4.3）。下一次对账发现行已不在 `held_outbox()`，从内存删去。某对端的最后一条 held 行离开 outbox、且 link 仍未成立时，撤回我侧单边引用（`without_reference`，经 `store.write`，不发事件），不长期占 k。

### 3.3 两份互引的一致性、副本延迟与回收

- 成立 = `model.effective(...)`：已提交的本地副本里，我的文件引用对端，对端文件引用我。只读本地副本，副本未到即不成立（设计 §6「宁可晚放行」）。待写意图不算（B13）。
- 发送方：铸造先成为意图，计入 `degree`；写成功后才提交。对端回写经副本延迟到达。两者完成前，发送方 hold 为 `LINK_MINTING`，消息不出门（设计 §4.6）。
- 接收方：本 actor X 在已提交副本中被发送方文件引用，X 未引用对方，且 X 对对方没有回收屏障时，调 `accept_reference`（lifecycle 取对方条目的值）。
  - 返回 True：写回引用（意图，提交后成立，发 `link.minted`，`side="acceptor"`）。
  - 返回 False：不写。
  - 接收方一般先于发送方成立，所以发送方放行后的消息到达时多已可派发；仍有延迟时由 WP4 的 dispatch hold 兜底。
- `peer_full`（裁定 48）：对端文件在已提交副本中可见、不引用 sender，且其中常规未回收引用数 ≥ 本节点该 actor 的 k，则判为 `LINK_PEER_FULL`。k 不在网络中广播，用本节点的 k 估计对端（两端从同一 liveliness 推算 N_active，ln N 量级相同）。对端文件不可见、暂缺、冲突，或引用数低于 k 时，判为 `LINK_MINTING`（wp1 B3：不猜满）。
- 副本暂缺（B11）：对端在 `absent` 或 `conflicted` 中，或其所在空间离开，都只让新的消息与新的准入等待。
  - ⛔ 不排干，不清出站，不发 `link.reclaimed`，不清回收屏障。
  - 已准入的 id 按 ports 的幂等仍为 True。
  - 等待的消息按 TTL 到期。
  - 对端恢复且修订号 ≥ 已观测最高值、内容仍互引时，自动恢复成立。此时不重发 `link.minted`，因为 `established` 未曾移除。
- 对端撤回（B11）：只有权威事实才算撤回。
  - 一是 X 与 P 曾成立，之后出现 P 的**更高修订号**的合法完整文件，且不再引用 X：X 侧回收（原因 `peer_withdrew`，含残余排干）。
  - 二是本节点的明确生命周期操作（drop、淘汰、任务结束、k 收缩）。
  - 低修订号的旧版本出现是回滚，忽略。
- 回收屏障：回收后 `reclaimed[peer]` 一直保留。两份引用都在已提交副本中**明确**撤回后才清除，即两侧各自出现不含对方的更高修订号快照。两份文件只是暂缺时不清（B11）。屏障期间 `derive_link` 判 `reclaimed`，接收方不重新接受对方的旧引用。屏障清除后可重新铸造。排干集合不随屏障清除，也不因重新铸造解除（wp1 B2、B10）：重新成立后回复旧请求仍得 `LINK_RECLAIMED`。
- 回收残余（B10）：对 peer P 回收时，在一次 `apply` 中：
  - **排干**：该 actor 来自 P 的全部存活待回复请求，取 `policy.residue` 与 `inbox.inbound_live(actor, P)` 的并集。范围包括未注入的等待行、已 fetched 行、已审查行与尚未对账的行。排干记录按 `(actor, message_id)` 写入 ledger，与 ack（consumed）同事务。
  - **释放出站**：该 actor 到 P 的全部开放出站与预留。
  - **取消重试**：已放行、但仍在普通 outbox 等待重试的到 P 的请求，以 `cancel_outbound={id: LINK_RECLAIMED}` 转 DLQ，⛔ 不再出门。
  - 从未放行的全新 held 请求不属残余，留在 outbox，按 §3.2 重评（通常转为铸造或 `SELF_FULL`）。重新铸造后 ⛔ 不自动重发已取消的旧请求。
  - 排干标记在调 `apply` **之前**已写入内存，回复资格立即失效。`apply` 失败时下次 pass 重试排干，⛔ 不恢复回复授权。
- 分区：两份对不上只 hold，不丢（设计 §5.6）；hold 到 TTL 为止。

### 3.4 org 空间未加入或不可写时

- `index.org_available` 为 False（没有可写空间，B13）。
- `admit_send` 对受管目标经 policy 返回 `Hold(LINK_SELF_FULL, target, "org-unavailable")`（wp1 B12），消息留 outbox。owner、`user:`、`route:`、自发、内部 actor 不受影响。
- `sender_known` 对从未观测过的 agent 发送者为 False（本地没有表）。WP4 的 ingress 据此拒收 `LINK_UNKNOWN`（设计 §6 门）。曾观测、现暂缺的仍为 True（§2.2）。
- `admit_dispatch` 对受管来源为 False（无成立的 link）。已准入的 id 按幂等仍为 True。
- `accept_reference` 为 False，不回写。
- `list_links` 返回 `orgAvailable: false`。
- 加入后：下一次 `refresh()` 发现可写空间，扫描文件，为每个缺文件的本节点 agent 写意图 `LinkFile(actor, revision=0, entries={})`。写成功（提交）后，这就是「已加入」的可见证据，对端的 `sender_known` 由此为 True。补建失败时意图保留重试，本节点该 agent 的 `sender_known` 保持 False（B13）。`org-unavailable` 的 hold 在重评后转为铸造。
- 内部 actor 不建文件：它们按名豁免，见 §2.2 与裁定 51。

### 3.5 线程模型

| 线程 | 调用 | 允许 |
| --- | --- | --- |
| inbox actor | `sender_known`、`admit_dispatch`、`injected`、`reply_received`、`discard` | 只读 `store.index()`（原子引用，不加锁）；持 `_mem` 做纯内存读写。⛔ 不做 I/O，除 `kick` 外不等待，不调回调 |
| IPC | `admit_send`、`submitted`、`settle_hint`、`list_links`、`stats`、`add`、`drop`、`top_fields`；WP3 的 `read_body` 可能调 `admit_dispatch` | 可在锁外做读 I/O（`inbound_row`）；持 `_mem` 只做内存。写文件与 ledger 更新一律交给 service 线程（`kick`）。例外：`message.send` 的 `user:` 目标由 IPC 线程经 facade 直接写 ledger（§2.4，同步命令，不持 `_mem`） |
| service（`link-service`，daemon 线程） | `tick`：orgfs 读写、对账、`apply`、事件发布、订阅回调 | I/O 一律在锁外：先在锁内拷出所需状态，锁外做 I/O，再在锁内合并结果 |
| orgfs watch | 回调 | 只 `kick()` |
| hook bus lane | `observe_hook` | 只把事件放入 service 队列并 `kick()` |
| PAC sender | `bind_task` 及其回调 | 只改内存并 `kick()`；ledger 与文件写入由 service 线程完成 |

`_mem` 是一把 `threading.Lock`。持锁区只有字典与列表操作，长度与本 actor 的 link 数、在途数同阶。⛔ 持锁区内不调 store、inbox、presence、pac、publisher、logger、订阅回调，也不获取其他锁。所以 inbox actor 线程上的调用最坏只等 service 线程的一段内存操作，不等 I/O，也不会与 inbox actor 互等：service 线程从不在持 `_mem` 时调 inbox。订阅回调在锁外调用，回调里可以安全地回调 `list_links` 等查询（B17）。`kick()` 只对一个 `threading.Event` 调 `set()`。由 G12、G13 机检。

`admit_dispatch` 的原子性（wp1 B1）：首次准入的判定与记入在同一次持锁内完成。inbox actor 串行调用，IPC 线程的并发调用（`read_body`）也经同一把锁，所以两个不同 id 不会同时占最后一个名额（G14）。注入登记（`injected`）在 inbox actor 线程上同步完成，先于同线程的下一次受管准入（B8）。

### 3.6 hold 的持久化放在 outbox 列（裁定 47）

被 hold 的消息写入发送方 outbox（设计 §4.3），带新列 `link_hold`（原因码）。

- 写入时 `next_attempt_ms = expires_at_ms`。retry claim 只在到期时选到它，并按现有路径写 `TTL_EXPIRED`。未到期的 hold 行在 claim 中跳过。在线唤醒的 `MIN(next_attempt_ms, ?)` 不作用于 hold 行。
- 放行只是清列并把 `next_attempt_ms` 设为当前时刻，之后由现有 retry 泵投递。

这样：重启不丢；`outbox list` 能显示原因与等待时长；TTL 与 DLQ 语义不变；links 不持有消息正文。备选「service 自持一份 hold 队列，放行时才 submit」被否：重启丢消息，且 `outbox list` 看不见。

`UpdateLinkLedgerCommand` 在 inbox actor 上以一个事务执行，按条件更新，并返回实际结果（B15）：

- `release`：只对仍在 outbox、`link_hold IS NOT NULL`、且 `expires_at_ms > now` 的行生效。效果：清列，`next_attempt_ms = now`；ledger 的 `out` 行由 `held` 改为 `open`。回复行另在同一事务中：把原请求的 ledger `in` 行改为 `closed`（`sample_pending = 1`），并 ack 原请求（consumed），即现有回复结算协议（B5）。原请求已不存活时，该回复行改按 `fail` 处理：排干记录存在时码为 `LINK_RECLAIMED`，否则为 `MESSAGE_REPLY_UNAVAILABLE`。
- `reasons`：只更新仍 held 的行。
- `fail`：held 行转 DLQ，码为给定码；ledger 的 `out` 行改为 `released`。
- 已到期的行一律只出现在 `LedgerResult.expired`，⛔ 不放行。retry 泵与本命令都在 inbox actor 上串行执行，所以同一行只产生一个终态：投递、过期或失败三者之一。
- `cancel_outbound`：未 held、仍在 outbox 的普通重试行转 DLQ，码为给定码（回收残余，§3.3）。

### 3.7 PAC 任务绑定的铸造与释放（裁定 7、48，B16）

- **铸造**：PAC 的通知走 `DaemonPacNotificationSender.send`，绕过 `message.send`（裁定 31）。该方法在 `delivery_io.deliver` 之前调 `application._link_task_binder(sender=, recipient=, conversation_id=)`（为 None 时跳过）。composition 把它设为：`conversation_id` 以 `pac-` 开头、两端都是 `agent:` 且不是 owner 对时，调 `service.bind_task(principal=sender, worker=recipient, task_id=conversation_id, now_ms)`，并把返回的回调交回 `send`。
- **`bind_task`**：
  - 对两端中每个本节点 actor：若尚无到对端的我侧引用，以 `LinkEntry(lifecycle="pac_task", task_id=task_id)` 写意图。已有常规或 PAC 引用时不改条目。
  - 无论条目是否新写，都在 `task_bindings[peer]` 加入 `task_id`，并在下次 `apply` 中以 `bind_tasks` 写入 durable 表 `link_task_bindings(actor, peer, task_id, bound_at_ms)`。
  - 意图提交后，同节点两端的 link 即成立。他节点的对端 daemon 看到 `pac_task` 条目后，`accept_reference(lifecycle="pac_task")` 恒为 True，回写同一 `taskId`。
  - 条目上的 `task_id` 只是当前的代表绑定；完整的绑定集合以 `link_task_bindings` 为准。
- **计数**：PAC link 不计入 k（`degree` 只数 regular，wp1 B4），其在途请求计入 cwnd 与 W。PAC 投递不经 `admit_send`。`bind_task` 返回的回调由 `send` 在**投递成功后**以真实 message id 调用。回调把这条投递记为 principal 的出站请求：内存 `outbound`，下次 `apply` 写 `open_outbound`。按 `(principal, message_id)` 幂等；没有成功回执时不调用。已有 regular link 时同样计入。PAC 投递不在发送方 hold，是裁定 48 的明确例外，不扩展到普通 `message.send`；W 满时由接收方 dispatch 挡住。
- **释放**：
  - bus 事件只作唤醒：service 作为 daemon 级 bus 消费者注册 `("worker.cleanup.complete", "graph.closed")`（`register_consumer("links", ...)`），收到即 `kick()`。
  - 回收的判据是每 tick 的核对：对 `task_bindings` 中的每个 task 调 `pac.task_states(...)`。`ended`：该任务的全部绑定解除。`active` 时再对本节点 worker 调 `pac.worker_active(task_id, worker)`，False 时只解除该 worker 的绑定。`unknown` 或 None：不动。
  - 一个 peer 的**最后一个**任务绑定解除，且条目是 `pac_task` 时，才回收该 link（原因 `task_ended`，照常执行残余）。条目是 `regular` 时只删绑定，不回收。
  - 丢事件、hooks 关闭、重启后，下次 tick 的核对仍会回收（启动时由 `task_bindings()` 重建内存）。
  - 对端 daemon 看到更高修订号的撤回后，按 `peer_withdrew` 回收自己一侧。
- **注册与注销**：composition 注册 bus 消费者。`start()` 失败或 `close()` 时注销（W11、W12）。PAC 生命周期读取与事件适配都在 composition，service 不 import PAC。

### 3.8 `user:` 目标

所有 `user:` 目标（owner 与非 owner）都不查 link、不占 k、不铸造（裁定 37，`policy.governed` 按裁定 44 修正）。

- 带 `replyTo` 时，先按 §2.2 步骤 1 校验（B3）。
- 请求计入发送方的出站在途（W 与 `outboundInFlight`），经 §2.4 第 6 步写 ledger。闭合有两种：带 `replyTo` 的回复（接收事务或对账识别），或到放弃阈值时释放。两类基线都没有时（冷启动），以消息 TTL 为上界释放，避免永久占 W。
- 发送方 W 已满时，向人的新请求**照常投递**，不 hold。本条与评审 B4 冲突，待裁，见 §9 C1。
- `route:` 目标不计入。

### 3.9 durable 记账：inbox SQLite 中的 ledger（B14）

两张表随 outbox 列一起在 `core.py` 迁移（旧库补表，重复打开不重复 ALTER）：

```sql
CREATE TABLE IF NOT EXISTS link_ledger (
  actor TEXT NOT NULL, message_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  peer TEXT NOT NULL, intent TEXT NOT NULL, reply_to TEXT,
  state TEXT NOT NULL,          -- in: open|censored|closed|acked|expired|drained ; out: held|open|closed|released
  opened_at_ms INTEGER NOT NULL, -- in: received_at_ms ; out: accepted moment
  sent_at_ms INTEGER,            -- out: actual send (outbox row delivered)
  event_at_ms INTEGER,           -- closing / draining / releasing moment (reply received_at_ms for out-closed)
  threshold_ms INTEGER,          -- censored: the threshold value (B9)
  sample_pending INTEGER NOT NULL DEFAULT 0,
  expires_at_ms INTEGER NOT NULL,
  PRIMARY KEY (actor, message_id));
CREATE TABLE IF NOT EXISTS link_task_bindings (
  actor TEXT NOT NULL, peer TEXT NOT NULL, task_id TEXT NOT NULL, bound_at_ms INTEGER NOT NULL,
  PRIMARY KEY (actor, peer, task_id));
```

写入点（都在既有事务内，无第二份消息正文）：

| 事务 | 写什么 |
| --- | --- |
| submit（`SubmitMessageCommand.link_open`） | `out` 行，`state` 为 `held` 或 `open` |
| submit（`link_reply_close`） | 原请求的 `in` 行 `open`/`censored` → `closed`；`open` 的置 `sample_pending = 1` |
| outbox 投递成功删除行（`outcomes.py`、`delivery/retry.py`、`core.py`） | `out` 行的 `sent_at_ms` |
| 入站回复落库（`receive.py`） | 匹配 `(recipient, reply_to)`、`peer == sender` 且 `state = 'open'` 的 `out` 行 → `closed`，`event_at_ms = received_at_ms`，`sample_pending = 1` |
| 注入（`projection.py`：fetch 标记、派发确认） | `in` 行 `INSERT OR IGNORE`（仅当 §2.2 `injected` 的计入条件成立） |
| ack、过期、终态失败 | 对应 `in` 行 → `acked`/`expired` |
| `UpdateLinkLedgerCommand` | §3.6 的 hold 更新；`drain`（`in` → `drained`，同事务 ack）；`release_outbound`；`censor`（`in` → `censored`，写 `threshold_ms`）；`sampled`；`open_outbound`；`close_inbound`；`bind_tasks`/`unbind_tasks`；`purge_before_ms`（删 `expires_at_ms` 已过的终态行） |

终态行保留至 `event_at_ms + message_ttl_ms`，供迟到回复、排干码与去重使用。

恢复：`start()` 以 `ledger_snapshot` 与 `task_bindings()` 重建 `admitted`、`pending_inbound`、`censored_inbound`、`outbound`、`drained`、`released_outbound`、`task_bindings`。控制器样本与 `ControllerState` 不持久化，重启即冷启动（设计 §5.6）。

失败语义：

- 写 ledger 的事务失败时，消息状态与 ledger 一起回滚，不对外确认完成。
- inbox actor 上的 gate 判定只改内存，durable 记录由调用方在已有事务写入。该事务回滚时，inbox 调 `sink.discard`，撤销本次内存准入。`discard` 丢失时，由对账兜底（§3.1）。

### 3.10 多个 org 空间（裁定 50，待 Allen 裁）

本节点在多个 org 时，本地 actor 的文件以同一完整快照镜像写入每个存活可写 org 的 directory 空间（裁定 50 的选项 a，裁定前照此实施）。读时按 §1.1 取最高修订号的完整快照（B12）。⛔ 不做跨空间的授权并集。单 org 是常态，此规则在单 org 下退化为「一份文件」。

## 4. 测试清单

运行：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/inbox -q`。

先落骨架：类与方法存在，方法体为 `raise NotImplementedError`。记录每条测试的行为红，再实现到绿。收集期的 `ModuleNotFoundError` 只算第一道红，不能代替行为红（WP4 评审 A3）。先红记录放在只含测试的提交里，该提交位于实现提交之前（WP4 验收第 1 条的教训）。

命名：每个测试函数以 `test_<编号小写>_` 开头，例如 `test_st3_highest_revision_snapshot`。参数化展开不另编号。验收 11 以此计数。

URI 固定：

| 名字 | URI |
| --- | --- |
| `A`、`B`、`C` | `"agent:o:n1:a"`、`"agent:o:n1:b"`、`"agent:o:n1:c"` |
| `X`（他节点） | `"agent:p:n2:x"` |
| `XS`（他节点，同 owner） | `"agent:o:n2:s"` |
| `OWNER` | `"user:o"` |
| `U` | `"user:p"` |
| `COORD` | `"agent:o:n1:dispatch-coordinator"` |
| `RCOORD` | `"agent:p:n2:dispatch-coordinator"` |
| `RHOOK` | `"agent:p:n2:_turn-hooks"` |

服务一律经 `tests/support/links/org.py` 的 `make_service(...)` 构造，依赖 `FakeOrgFs`、`FakeInboxPort`、`FakePresence`、`FakePacPort`、假时钟与固定种子。`tick()` 由测试直接调用，不起线程；线程测试除外。

目录：`tests/daemon/links/` 现有 5 个直接文件，加 `test_store.py` 到 6 个。子目录 `service/` 含 `__init__.py` 与 6 个测试文件，共 7 个。

### 4.1 `tests/daemon/links/test_store.py`

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| ST1 | 无空间：`index()` 各集合为空，`org_available` 为 False；`write` 不抛，只进 `intents()` | `AssertionError`（骨架返回 None） |
| ST2 | 扫描：两个合法文件，外加非法 JSON、作者不符、文件名与 `actor` 不符、`from_document` 抛错各一个 → 索引只含两个合法文件；每类拒收各记一次 `link.file_rejected` | 索引含非法文件 |
| ST3 | 多空间快照（B12）：空间 1 rev=2 含 B，空间 2 rev=3 已删 B → `files[A]` 不含 B；同 rev 同内容 → 用它；同 rev 不同内容 → A 在 `conflicted`，不在 `files`，记 `link.file_conflict`；先到 rev=3 再到 rev=2 → 仍为 rev=3；离开空间 2 → 回到空间 1 的 rev=2 的前提是它 ≥ `revisions[A]`，否则 A 在 `absent` | 结果含 B（按 peer 合并） |
| ST4 | 写（B13）：内容为 `to_document` 的紧凑键排序 JSON；带 `expect_version`；写入每个可写空间；写前 `intents()` 已反映，`index().files` 不变；全部空间写成功后 `files` 更新、意图删除 | 写前 `files` 已变 |
| ST5 | `stale-write`：重读，复核本地写者与修订号后重写成功，记 `link.file_conflict`；修订号已被超越时丢弃意图，记 `link.intent_dropped` | 抛 `OrgFsError` |
| ST6 | 单写者（B13）：`is_local_agent` 为 False 的 `XS`（同 owner 远端）与 `OWNER` → `ValueError`，`FakeOrgFs` 零写入 | 未抛，或已写 |
| ST7 | watch 回调只调 `on_change`，不读文件（FakeOrgFs 记录调用线程与读次数） | 回调中有读 |
| ST8 | 空间消失（B11）：关闭 watch；只在该空间可见的 actor 进入 `absent`，`known()` 仍为 True；`revisions` 保留 | 旧文件仍在 `files`，或 actor 被遗忘 |
| ST9 | 写失败（B13）：全部镜像失败 → 意图保留、`files` 不变、下次 `refresh` 重试；只读空间与 `not-a-member` → 移出 `writable`，`org_available` 为 False；两空间一成一败 → 不提交 | 失败仍提交 |
| ST10 | 暂缺与修订单调（B11）：rev=5 可见后文件消失或读错 → A 在 `absent`，不在 `files`；rev=3 出现 → 忽略；rev=6 出现 → 回到 `files` | 旧版本回退或暂缺被删 |
| ST11 | 真实 `LocalOrgFs`（`tmp_path` 下 `OrgFsRuntime` 的 facade，`create_space`）：写 A 的文件，`watch` 收到变化，`refresh` 后索引含 A；绝不读生产 home | 同 ST4 |

### 4.2 `tests/daemon/links/service/test_gate.py`

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| G1 | 无 org 空间：A→B 请求得 `Hold(LINK_SELF_FULL, "org-unavailable")`；A→OWNER、A→U、A→`route:` 均为 `Allow(None)`；`sender_known(B, X)` 为 False；`admit_dispatch` 对 X→A 为 False | 返回 `Mint` |
| G2 | `user:` 目标（裁定 37、44）：org 已加入，k=2 且 A 已有 2 条常规 link → A→U 与 A→OWNER 为 `Allow(None)`；A 的文件与意图都不新增条目；`degree` 不变；A→U 请求经 `submitted` 后 `top_fields(A)["outboundInFlight"]` +1，并占 W：W 由此满后，A→B 新请求得 `LINK_WINDOW_FULL/W`；W 满时 A→U 仍为 Allow（§9 C1 的现行行为） | 返回 `Mint(peer=U)` |
| G3 | 发送者（B2）：`admit_send(sender=X)` 与 `sender=XS` 抛 `CALLER_NOT_AUTHORIZED`，无状态变化；`sender=OWNER`、`adapter:lark:x` 为 `Allow(None)`；本节点 agent `C` 不在线、未托管时仍受管：C→B 得 `Mint` | 返回 `Allow`，或抛 policy 的 `ValueError` |
| G4 | 内部 actor 按名（B1，裁定 45、51）：A→COORD、A→RCOORD、A→RHOOK 为 `Allow(None)`；A→X 为 `Mint`；`sender_known(A, RCOORD)` 为 True；`admit_dispatch(sender=RCOORD, recipient=A)` 为 True 且不计 W | 远端返回 `Mint` 或 `sender_known` 为 False |
| G5 | 回复校验先于豁免（B3）：A→U 带未知 `reply_to` → `MESSAGE_REPLY_UNAVAILABLE`；A→OWNER 回复 U 的请求 → `INVALID_ARGUMENT`；A→U 回复已排干 id → `LINK_RECLAIMED`；A→`route:` 回复存活请求 → Allow（不比 requester）；m1 不在内存但 `inbound_row(m1)` 存活 → Allow（锁外读 1 次）；OWNER 请求的有效回复 → `Allow(None)` | 用户目标的未知 id 得 Allow |
| G6 | 同对端 FIFO：A→B 已有 held 请求时，新请求返回同原因的 `Hold`，即使此刻 W 有空 | 返回 `Allow` |
| G7 | 预留与 observer 幂等（B6）：W=2、无在途，连续两次请求 Allow，第三次 `Hold(LINK_WINDOW_FULL, "W")`；30 s 后未 `submitted` 的预留过期，第四次 Allow；同 id 重放 `submitted`（请求、回复、held 三种）不重复计数、采样或闭合 | 第三次 Allow，或重放多计 |
| G8 | `sender_known`（B11、B13）：B 已提交 → True；B 暂缺 → True；从未观测 → False；本节点 A 只有补建意图未提交 → False，提交后 → True；调用期间 `FakeOrgFs` 与 `FakeInboxPort` 零调用 | 意图即 True，或暂缺即 False |
| G9 | `admit_dispatch` 幂等与恢复（B14）：W=1，两个不同 id 依次首次询问，第一个 True、第二个 False；重复问第一个仍 True 且不重复计数；W 收缩后已准入 id 仍 True；**不调 `close()`**，丢弃 service，以同一 `FakeInboxPort`（ledger 含该 `in` 行）重建后仍 True | 第二个 True，或重建后为 False |
| G10 | 回复豁免与 intent（B3）：A 的 W 被 2 条到 B 的出站占满；`reply_received` 后 B 的回复（`intent="reply"`、`reply_to` 命中）`admit_dispatch` 为 True；`reply_to` 指向 A 到 C 的出站、sender 为 B → 受 W，且 C 的出站不闭合；W=2 时 3 条无 `reply_to` 的 reply-intent 消息 → 前 2 条 True、第 3 条 False，`pending_inbound` 增 2 | 错误 peer 闭合他人出站，或 reply-intent 不计数 |
| G11 | `settle_hint`：排干集合内 → `ack`；其余（含对账尚未看到的 pending 请求）→ `keep` | 未知 pending 得 `ack` |
| G12 | 线程（S-T1）：真实 `DeliveryCustodyCoordinator`/`DeliveryCustodyFacade`（照 WP4 `tests/daemon/links/admission` 的组装），`LinkAdmission.bind` 一个记录线程的 `LinkService` 代理（仿 `tests/support/links/gate.py`）；ingress、dispatch 与 sink 的调用都在 inbox actor 线程；`FakeOrgFs`/`FakeInboxPort` 在 inbox actor 线程被调用时抛错，测试结束无该错 | 代理记录到非 actor 线程，或端口在 actor 线程被调用 |
| G13 | 不阻塞（S-T2，B17、A2）：参数化 4 处阻塞点（orgfs 读、orgfs 写、`ledger_snapshot`、`apply`），各以 `threading.Event` 卡住 service 线程；以 `Barrier` 确认 service 已进入阻塞点后，另一线程调 `admit_dispatch` 与 `sender_known`，断言两者在释放 Event **之前**已返回；总超时 5 s；耗时只记入证据，不作断言 | 释放前未返回（持锁做 I/O） |
| G14 | 并发（B17）：两线程以 `Barrier` 同时首次准入两个不同 id 争最后 1 个名额 → 恰一个 True；多目标 send A→[B, C] 在 W 余 1 时 → 一个 Allow、一个 `Hold(…, "W")` | 两个 True，或两个 Allow |
| G15 | 契约（B17）：`isinstance(service, X)` 对 `LinkGate`、`LinkSendObserver`、`LinkQueries`、`LinkAdmin`、`LinkChangeSource` 均为 True；`subscribe` 的取消函数调用后不再回调；回调中调 `list_links` 不死锁（回调在锁外） | `isinstance` 为 False，或死锁超时 |

### 4.3 `tests/daemon/links/service/test_hold_release.py`

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| H1 | 铸造：A→B 首次请求得 `Mint(B)`；A 的意图立即含 B，`files` 不含；`tick()` 后 `FakeOrgFs` 收到 A 文件的写；`submitted(hold_reason=LINK_MINTING)` 只登记 id，下一 `tick` 后 `held` 含 `held_outbox()` 的完整行 | 无写入，或由回调参数伪造 HeldRow |
| H2 | 单边引用：只有 A 引用 B → A→B 保持 `LINK_MINTING`，`apply` 无放行；B 侧 `sender_known(B, A)` 为 True，但 B 侧 `admit_dispatch` 对 A→B 为 False（B 未回写） | 单边即放行 |
| H3 | 互引成立后放行：B 侧 service（第二个实例，共享 `FakeOrgFs`）`tick()` 回写；A 侧 `tick()` 后 held 行进入 `release`，`outbound` +1，`link.minted` 发出（两端 `side` 各为 minter 与 acceptor） | held 仍在 |
| H4 | PEER_FULL：B 的文件已有 k 条常规引用且不含 A → A 的 held 原因改为 `LINK_PEER_FULL`；B 撤掉一条后转 `LINK_MINTING`；B 回写后放行 | 一直是 `LINK_MINTING` |
| H5 | WINDOW_FULL（cwnd）：A–B 已成立，cwnd=1，1 条在途；第二条 held 为 `LINK_WINDOW_FULL/cwnd`；`reply_received` 闭合后 `tick()` 放行第二条 | 未放行 |
| H6 | WINDOW_FULL（W）：W 被入站请求占满；对其中一条 `submitted(reply_to=...)` 后放行；另测对账发现 ledger 的 `in` 行变 `acked` 后放行 | 未放行 |
| H7 | SELF_FULL（k）：A 有 k 条常规 link 且都在途 → `Hold(LINK_SELF_FULL, "k")`；一条闭合且过最短存活期后 → `Mint(target, evict=该条)`，旧 link 被回收（`link.reclaimed`，`evicted`），新引用成为意图 | 无 `evict` 或仍 Hold |
| H8 | SELF_FULL（churn）：一个周期内回收数超过 k → 新目标 `Hold(..., "churn")`；推进假时钟过一个周期后转为铸造 | 返回 `Mint` |
| H9 | org 加入后放行：G1 的 held 行在 `FakeOrgFs` 增加可写空间后的 `tick()` 中转为铸造；本节点每个 agent 的空文件意图在提交后可见 | 仍 `org-unavailable` |
| H10 | 放行顺序：同一 (A, B) 三条 held，cwnd=2 → 只放行前两条；回复与请求混排时，回复不被前面的请求挡住 | 放行第三条，或回复被挡 |
| H11 | 回复 hold 与恢复（B5、B11）：A–B 已成立后，B 的文件暂缺 → A 回复 B 得 `Hold(LINK_MINTING, "awaiting-peer")`，原请求不 ack，无 `link.reclaimed`；B 的文件恢复后放行，`apply` 的同一事务 ack 原请求，记 T 一次；被排干请求的 held 回复 → `fail` 含 `LINK_RECLAIMED`；原请求已 ack（未排干）的 held 回复 → `fail` 含 `MESSAGE_REPLY_UNAVAILABLE` | 抛异常、未放行或提前 ack |
| H12 | 到期优先（B15）：`expires_at_ms ≤ now` 的 held 行不进 `release`，不 Mint，不写引用，不加在途；行离开 `held_outbox()` 后内存删去；无其他 held 时 A 撤回到 B 的单边引用（无事件） | 到期行被放行或计数 |
| H13 | 更新结果（B15）：`FakeInboxPort.apply` 只回报部分 id 放行 → 内存只加这些；`apply` 抛错 → 内存不变，下次 `tick` 重做且不重复计数 | 未成功者计入 `outbound` |
| H14 | 放行与过期竞争（B15）：行恰在本次 pass 中到期 → 只出现在 `expired`；真实竞争见 L7 | 同时放行与过期 |
| H15 | `held_peers`（B15，wp1 B7）：只有本次重评仍为 `LINK_WINDOW_FULL/cwnd` 的 peer 进入 `ControllerInput.held_peers`；W hold、`LINK_MINTING` 与重启后未重评的行不进入 | W hold 缩 cwnd |
| H16 | 暂缺不是回收（B11）：A–B 已成立，B 的文件整个缺失、读错、内容未到、旧版本出现 → 均无 `link.reclaimed`，已准入 id 仍 True，新 A→B 请求 held；B 的更高修订号快照不含 A → 回收（`peer_withdrew`）并排干 | 暂缺即回收 |

### 4.4 `tests/daemon/links/service/test_reclaim.py`

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| R1 | 淘汰只执行 `Mint.evict`：打桩 `policy.select_victim` 计数，铸造路径调用次数为 0（裁定 5） | 调用次数 > 0 |
| R2 | drop 与残余（B10）：A–B 成立；B 来的请求有已准入 2 条、已审查 1 条、未注入等待 1 条（仅在 `inbound_live`）；A 到 B 有 1 条在途、1 个预留、1 条已放行未投递的普通 outbox 行、1 条从未放行的 held 新请求 → `drop` 返回 `drained: 4, released: 1, cancelled: 1`；`apply` 含这 4 个排干（与 ack 同事务）与 `cancel_outbound={id: LINK_RECLAIMED}`；预留清除；held 新请求留在 outbox；`link.reclaimed`（`dropped`）payload 含计数与 id | 未注入行留 pending，或旧 outbox 行仍投递 |
| R3 | 排干码（B10，wp1 B2）：drop 后回复已排干 id → `LINK_RECLAIMED`；重新铸造成立后再回复同一 id 仍 `LINK_RECLAIMED`；回复从未存在的 id → `MESSAGE_REPLY_UNAVAILABLE`；排干记录过 `message_ttl_ms` 后由 `purge_before_ms` 清除；排干按 `(actor, id)` 记，C 对同一 id 的查询不受影响 | 重铸后得 Allow |
| R4 | 迟到回复：残余释放的出站 id 收到回复 → `admit_dispatch` 不得回复豁免，计数不为负，无 RTT 样本 | 计数为负或有样本 |
| R5 | 回收屏障（B11）：B 以更高修订号移除对 A 的引用 → A 侧回收（`peer_withdrew`）并排干；屏障期间 B 的旧引用重新出现不被 A 接受；两份文件只是暂缺 → 屏障不清；两侧都以更高修订号明确撤回后 → 屏障清除，可重新铸造 | 暂缺即清屏障 |
| R6 | k 收缩：N_active 从 55 降到 7（k 由 5 到 2），A 有 4 条常规（全部在途）与 1 条 PAC → 回收 2 条常规（`k_shrink`），PAC 不动 | 回收 0 条 |
| R7 | PAC 绑定铸造（B13、B16）：`bind_task(principal=A, worker=B, task_id="pac-g1")` → 两份 `pac_task` 意图，`taskId` 一致，提交后成立；提交前 `admit_dispatch` 为 False；`degree(A)` 不变；常规满时仍可绑定；他节点 worker X 时只写 A 的条目，X 侧 service 看到后回写；`link_task_bindings` 写入 | 计入 degree，满时拒绑，或意图即成立 |
| R8 | PAC 计数（B16）：回调以真实 message id 调用后，该投递占 cwnd 与 W（达到即 `LINK_WINDOW_FULL`）；同 id 回调两次只计一次；未调用回调（投递失败）不计；已有 regular link 时同样计入 | Allow，或重复计数 |
| R9 | PAC 释放（B16）：不发任何 bus 事件，`FakePacPort` 报 `ended` → 下次 `tick` 回收（`task_ended`，残余排干）；`worker_active` 为 False → 只解除该 worker；同一对端两个 task，第一个结束 → link 保留；regular link 加任务，任务结束 → 只删绑定不回收；以同一 `FakeInboxPort` 重建 service 后，已结束任务在首个 `tick` 回收；`unknown` → 不动 | 丢事件后永存，或误回收另一任务 |
| R10 | `add`（A3）：新 peer → 意图、`state: pending, committed: false`；重放 → 幂等；自身满且无 victim → `{"written": false, "queued": false, "holdReason": "LINK_SELF_FULL"}`，无意图；`user:`、自身、owner 对、内部 actor 名 → `INVALID_ARGUMENT` | 对 `user:` 写了引用，或满时返回 `queued: true` |
| R11 | 排干的 durable 重试（B10、B14）：`apply` 在排干时抛错 → 回复该 id 仍得 `LINK_RECLAIMED`，下次 `tick` 重发同一排干；成功后 `FakeInboxPort` 有排干记录与 ack | 失败后回复被 Allow |

### 4.5 `tests/daemon/links/service/test_samples.py`

每条都驱动真实的 `service.tick` 与假时钟，不打桩 `evaluate`（B17）。

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| SA1 | 方向（wp1 B6）：A 发回复只增 `ReplySample(sent)`；A 收到 B 的回复只增 `ReplySample(received, B)`；`stats(A)` 的节点吞吐与 link 吞吐互不影响 | 方向混用 |
| SA2 | T 从到达算起（B7）：`injected(received_at=t0)`，`admit_dispatch` 在 t0+30 s，回复在 t0+40 s、早于任何 `tick` → `DwellSample.dwell_ms == 40_000` | 用准入时刻 |
| SA3 | RTT 不含派发延迟（B7）：A 的请求在 s 被接受、在 s+5 s 实际发出（ledger `sent_at_ms`）；B 的回复 r 时 `reply_received`，30 s 后才 dispatch → `rtt_ms == r − (s+5 s)` | 多出 30 s，或以接受时刻计 |
| SA4 | 快回复只采一次（B7）：回复落库且被 consumed 发生在两次 `tick` 之间，`reply_received` 回调被丢弃 → 下次 `tick` 由 ledger 的 `sample_pending` 闭合并记 RTT 1 次，`sampled` 回写；再次 `tick` 不再记 | 永不闭合，或重复记 |
| SA5 | 豁免来源的注入（B8）：managed owner 请求 `fetched_at_ms` 为 NULL，经 `injected` 后立即计入 W，同线程下一条受管准入看到它；同一消息经 pending.list、wait、read_body、push 重试多次 → 只计 1 次；未注入的等待行不计；OWNER 的 system notice（intent 非 request）不计 | owner 请求不计 W，或最后名额被再借 |
| SA6 | 非受管回复：U 带 `replyTo` 的消息经接收事务（ledger `out` 行 `closed`）闭合 A→U 的出站，RTT 以 `received_at_ms − sent_at_ms` 计；回复在 tick 前已 consumed 仍闭合 | 未闭合 |
| SA7 | 出站放弃：有 RTT_max 时超 4 倍阈值即释放，记 1 个审查 RTT 样本；无 RTT 有节点 T_max 时按 T_max 回退；两者都无时，`user:` 出站按 TTL 释放，agent 出站不释放（wp1 A4） | 不释放，或重复记样本 |
| SA8 | 入站审查（B9）：超阈值入站 → 以 `CensoredRecord`（含 requester、received_at、阈值）提交，只记 1 个审查样本；重复 `tick` 与对账不把它加回 W；重建 service 后迟到回复仍 Allow 且不再记 T；ack 后不可回复（`MESSAGE_REPLY_UNAVAILABLE`）；link 回收时被排干；连续多个窗口阈值不自我放大（裁定 34） | 重占 W、重复采样，或旧请求错误 Allow |
| SA9 | 控制器周期：`tick` 每次调 `evaluate`；P 只取 `limits.period_ms`（打桩 `controller.period_ms` 在 service 内的调用次数为 0）；同种子同输入两次运行 `stats` 相同 | service 自算周期，或结果不可复现 |
| SA10 | N_active：`FakePresence` 依次报告 30 个 agent，24 小时后只剩 5 个仍在线 → `nActive` 从 30 降到 5；不含 actor 自身 | 未过期或含自身 |
| SA11 | 场景·服务率随负载下降（B17）：多个周期喂入实际 sent 与 received 样本，令 T_bar 随在途上升 → 每周期 W 增量最多 1，只在 grad ≤ 0 时增，W 不低于 k | 一步多增，或 grad > 0 时增 |
| SA12 | 场景·非拥塞慢与后来者公平（B17）：旧的快样本过窗后，两条 link 都用新基线；向一个 peer 灌入超过 `SAMPLE_CAP` 的样本，另一 peer 的在途记录与审查标记不受影响 | 沿用旧基线，或误删他人记录 |
| SA13 | 场景·队头阻塞（B17）：一条出站被放弃释放后，下一条 held 新请求自动放行，被放弃的旧请求不重发；审查只记阈值一次，且不进 T_max 与 RTT_max | 旧请求重发，或阈值进基线 |
| SA14 | 场景·link 抖动（B17）：`last_used` 实际决定 LRU 淘汰对象；最短存活期内不淘汰；churn 退出后恢复铸造；两个实例用不同种子时评估时刻不同，同种子可复现 | LRU 不受 last_used 影响，或种子无效 |
| SA15 | 场景·冷启动（B17）：无样本时 W 停在 k；第 9 个样本时仍为冷启动；第 10 个样本后进入正常估计 | 少于 10 个样本即离开冷启动 |
| SA16 | 无基线进展限制（A1）：A 的 k 条 agent link 的对端永不回复，且无任何基线 → 断言 A 无法淘汰也无法替换目标（新目标保持 `SELF_FULL/k`）；行为断言 PASS，恢复进展在证据中记 UNKNOWN | 编造超时常数而放行 |

### 4.6 `tests/daemon/links/service/test_ledger.py`（inbox 层，真实 SQLite，`tmp_path`）

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| L1 | 迁移：旧库 outbox 无 `link_hold` 列、无两张表 → 打开后补齐，原行为 NULL；再次打开不重复 ALTER | `'link_hold' not in columns` |
| L2 | hold 入库：facade `submit(message, link_hold=LINK_MINTING, link_open=...)` → `queued=True`，无 I/O，`next_attempt_ms == expires_at_ms`；`outbox_items()` 的 `link_hold` 为码；`outbox.list` 行含 `holdReason`；ledger `out` 行 `held` | 发生了投递 I/O |
| L3 | retry 跳过：`retry_due(now)` 不选 hold 行；对同一收件人触发在线唤醒后仍不选（actor 与非 actor 两条路径） | hold 行被投递 |
| L4 | 到期：推进到 `expires_at_ms` 后，retry 把 hold 行转 DLQ，码 `TTL_EXPIRED`；ledger `out` 行 `released` | 行留在 outbox |
| L5 | 放行与发出时刻（B7）：`update_link_ledger(release=(id,))` 清列，`next_attempt_ms = now`，ledger `held → open`，结果含该 id；随后 `retry_due` 投递，同事务写 `sent_at_ms` | 未投递，或无 `sent_at_ms` |
| L6 | 改原因与失败：`reasons={id: LINK_PEER_FULL}` 更新列；`fail={id: LINK_RECLAIMED}` 转 DLQ，码为 `LINK_RECLAIMED` | 列未变或码错 |
| L7 | 条件更新与竞争（B15）：对已过期行与已不在 outbox 的行 `release` → 不在结果中，进 `expired`；放行命令与 retry 泵并发（两线程向同一 facade 提交）→ 该行恰有一个终态，投递 I/O 至多 1 次 | 重复投递，或到期行被放行 |
| L8 | 回复放行（B5）：held 回复 `release` → 同一事务：清列、原请求 ledger `in` 行 `closed`（`sample_pending = 1`）、原请求 consumed；事务失败（注入 SQLite 错误）→ 三者都不变 | 原请求提前 consumed |
| L9 | 回复直接接受（B5）：`submit(reply, link_reply_close=True)` → 同事务闭合 `in` 行；已审查的 `in` 行闭合后 `sample_pending = 0` | 未闭合，或审查行再采样 |
| L10 | 接收回复（B7）：入站回复的 `replyTo` 命中本地 `out` 行且 `peer == sender` → 同事务 `closed`，写 `event_at_ms = received_at_ms`、`sample_pending = 1`，提交后 `sink.reply_received` 调用 1 次；错误 peer → 行不变；回复随后被 consumed，`ledger_snapshot` 仍含该行 | 只扫 live 行，漏掉快回复 |
| L11 | 注入（B8）：受管准入与豁免请求在 pull fetch 与 push 派发两处都写 `in` 行，重复注入不重复写；`sink.injected` 在提交后调用；注入事务回滚 → 无 `in` 行，`sink.discard` 被调用 | managed 行无记录，或回滚仍计数 |
| L12 | 排干与取消（B10）：`drain={A: (m1,)}` → `in` 行 `drained` 与 m1 consumed 同事务；`cancel_outbound={m2: LINK_RECLAIMED}` → 普通重试行转 DLQ、不再投递 | 排干与 ack 分事务，或旧行仍投递 |
| L13 | 读：`link_held_outbox()` 只返回 hold 行，并解析 `replyTo`、intent、`expires_at_ms`；`link_inbound_live(A, B)` 含未注入行，并带 `received_at_ms`、`fetched_at_ms`、`reply_to`；`purge_before_ms` 只删已过期的终态行 | 返回已消费行，或误删存活行 |
| L14 | 崩溃恢复（B14）：真实 SQLite、composition 的 `LinkInboxPort` 适配器与真实 `LinkService`（`FakeOrgFs`）。分别在四处中断：submit 后、observer 前；准入后、tick 前；清 hold 后、内存合并前；排干 ack 后、内存合并前。中断方式是丢弃 service，不调 `close()`。以同一库重建 → 出站与已准入计数正确，旧排干 id 回复得 `LINK_RECLAIMED` | 丢出站，或旧排干 id 被当普通请求 |

### 4.7 `tests/daemon/links/service/test_wiring.py`

| 编号 | 测什么 | 先红形状 |
| --- | --- | --- |
| W1 | 组装：照 WP4 T17 的 `_app` 走 `_start_runtime`（`tmp_path` home，隔离 orgfs）→ `app._link_service is app._link_gate`，`app.link_admission.bound`，且绑定的就是该 service；`inbox` 的 sink 已绑定 | `AttributeError` 或未绑定 |
| W2 | 预先绑定（B2）：`_start_runtime` 前 `app.link_admission.bind(PermissiveLinkGate())` → 不创建 service，不重复 bind（无 `RuntimeError`），记 `link.service_skipped`；`app._link_gate` 是同一个放行门（经 `link_admission.gate`） | `RuntimeError`，或 `_link_gate` 为 None |
| W3 | `message.send` 接线：无 link 时 A→B 的 delivery 为 `queued: true, holdReason: LINK_MINTING`，顶层同样带；outbox 有 hold 行，ledger 有 `held` 行；无 `reason` 键；A→U 照常投递，ledger 有 `out` 行 | 消息被直投 |
| W4 | `message.reply` 接线（B5，真实 IPC）：A–B link 被移出副本时，A 回复 B 得 `ok: true, queued: true, holdReason`，原请求仍在 `pending_messages`；排干后回复（原请求已 ack）得 `LINK_RECLAIMED`，不是 `MESSAGE_REPLY_UNAVAILABLE`；agent 目标的 reply body 含 `replyTo` | 原请求提前 consumed，或码错 |
| W5 | 重放（B6）：W 满后以同一 `idempotencyKey` 重放已接受的 send → 返回原结果，不问 gate（记录门调用次数为 0）；已 ack 的 reply 重放 → 返回既有回执；held send 重放 → 返回同一 `holdReason`；digest 冲突沿用现有行为；submit 抛错时 `submitted` 未被调用 | 重放被 W 挡住、reply 不可用，或在途多 1 |
| W6 | 发送来源（B2）：CLI（owner 以 `onBehalfOf` 代 A）、interactive session、adapter 转发与 daemon 内部调用四种来源发 A→B，都询问 gate（记录到 4 次 `admit_send`）；`onBehalfOf` 为 `XS` → `CALLER_NOT_AUTHORIZED`，无投递 I/O；关停中（`close()` 后）发送 → 现有的 `DAEMON_NOT_READY` 类错误，无投递 | 某来源绕过 gate |
| W7 | `link.*` IPC：经 router 调 `link.list`、`link.stats`、`link.add`、`link.drop`，返回 `ok: true` 与 §2.5 的键 | `DAEMON_NOT_READY` |
| W8 | `top.snapshot`：actor 行有 `capacityW`（int，等于 `stats.W`）与 `outboundInFlight`（等于在途数）；非本节点 actor 行两值为 None；连续两次 `top` 不改变 gate 调用记录与 W | 缺键 |
| W9 | 事件：在 `app._hook_bus` 注册测试 lane（`link.minted`、`link.reclaimed`），铸造成立与 drop 各收到 1 条，`source == "daemon.links"`，payload 键为 §2.7 | 未收到 |
| W10 | PAC binder：`app._link_task_binder` 存在；`DaemonPacNotificationSender.send` 对 `conversation_id="pac-g1"` 先绑定、再投递、再以真实 id 回调；`app._link_service` 在 `_pac_graph_authority` 构造前已设 | 绑定晚于投递 |
| W11 | 关停（B16）：`_close` 调 `service.close()`；线程退出；watch 关闭；bus 消费者 `links` 已注销 | 线程或消费者残留 |
| W12 | 启动失败（B16）：令 `service.start()` 抛错 → `compose_link_service` 注销已注册的消费者，`_link_gate`、`_link_service` 保持 None，记 `link.service_failed`；daemon 其余部分继续启动 | 消费者泄漏 |

### 4.8 机检测试

| 编号 | 测什么 | 位置 |
| --- | --- | --- |
| S-B1 | AST：`links/store.py`、`links/service.py` 的 import 只在 §0 第 2 条的允许集合内；`links/__init__.py` 仍只导入 `ports`（沿用 `test_wp0_seams.py`）；`inbox/` 下无文件 import `hyprial.daemon.impl.links.service` 或 `links.store` | `test_store.py` 末尾 |
| S-B2 | `links/` 直接文件恰为 7 个 `.py`（官方结构门另见验收 3） | 同上 |

### 4.9 现有测试的改动与前提

- P1（`tests/daemon/links/test_policy.py`，裁定 44）：把「非 owner `user:` 目标得 `Mint`」的旧断言改为 `governed(A, "user:p")` 为 False、`admit(...)` 为 `Allow(None)`。先红：改断言后、改 `policy.governed` 前，现有实现返回 `Mint`。
- WP2 绑定真实 gate 后，未加入 org 的测试 daemon 中，agent 间发送会 hold（`org-unavailable`）。实施第一步，在只完成接线的状态下跑全量，列出因此失败的测试。修法只许二选一，⛔ 不改断言：
  - (a) 在 `_start_runtime` 之前调 `tests/support/links/gate.py` 的 `bind_permissive(app)`。它同时 `app.link_admission.bind(PermissiveLinkGate())` 并设 `app._link_gate` 为同一实例（B2：发送与接收两条路径一起放行）；composition 见已绑定即不创建 service（同 W2）。
  - (b) 用 `tests/support/links/org.py` 的助手给涉及的 agent 建立 link。
- ⛔ 不写 autouse fixture，不写目录级 conftest 全局放行。清单写进证据文件。

### 4.10 总数

| 文件 | 编号 | 条数 |
| --- | --- | --- |
| `test_store.py` | ST1–ST11、S-B1、S-B2 | 13 |
| `service/test_gate.py` | G1–G15 | 15 |
| `service/test_hold_release.py` | H1–H16 | 16 |
| `service/test_reclaim.py` | R1–R11 | 11 |
| `service/test_samples.py` | SA1–SA16 | 16 |
| `service/test_ledger.py` | L1–L14 | 14 |
| `service/test_wiring.py` | W1–W12 | 12 |
| `test_policy.py`（改） | P1 | 1 |
| 合计 | | **98** |

## 5. 验收标准（可机检）

1. 焦点：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/inbox tests/daemon/ipc tests/daemon/delivery tests/shell/cli/lifecycle/test_cli_top.py -q` 全部通过。
2. 全量：按 AGENTS.md 先装 GUI 依赖（Node 24），再跑 `uv run --with libcst==1.9.0 pytest tests/`，报告真实结果。缺依赖记 BLOCKED，不记 PASS。
3. 结构门：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` 退出 0。
4. `uv run ruff check` 对所有改动的 `.py` 文件退出 0。
5. 文件数：
   - `find src/hyprial/daemon/impl/links -maxdepth 1 -type f -name '*.py' | wc -l` 为 7。
   - `find src/hyprial/daemon/impl/composition -maxdepth 1 -type f | wc -l` ≤ 7。
   - `find tests/daemon/links -maxdepth 1 -type f | wc -l` ≤ 7。
   - `find tests/daemon/links/service -maxdepth 1 -type f | wc -l` ≤ 7。
   - `application/wiring/` 与 `inbox/` 各目录不新增文件。
6. 行数：`wc -l` 对每个改动与新建的 `.py` 文件 ≤ 1000；`git diff --stat <base> -- src/hyprial/daemon/impl/application/wiring/transport.py src/hyprial/daemon/impl/application/__init__.py` 为空（A4）。
7. 缝不动：
   - `git diff <base> -- src/hyprial/daemon/impl/links/{ports,__init__,model,controller}.py` 为空。
   - `git diff <base> -- src/hyprial/daemon/impl/links/policy.py` 只改 `governed` 一处（裁定 44，B1）。
8. `git diff <base> -- src/hyprial/daemon/impl/hooks/service.py src/hyprial/daemon/impl/dispatch/admission.py` 为空（样本不走 bus，WP4 的槽不改）。例外：集成者已按 §7 S1 落地的只读属性。
9. S-B1、S-B2、G12、G13、G14 通过（越层、线程与并发）。
10. `grep -n '"reason"' src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py src/hyprial/daemon/impl/application/messaging/inbox_surface/link_send.py` 的新增行中不含 hold 码（hold 只用 `HOLD_REASON_KEY`）。
11. 编号全覆盖：`grep -rhoE 'def test_(st|g|h|r|sa|l|w|p)[0-9]+_' tests/daemon/links | sort -u | wc -l` 为 96（98 条减去 S-B1、S-B2 两条机检；这两条以 `test_s_b1_`、`test_s_b2_` 命名，另行 `grep -c` 为 2）。
12. 无旁路状态文件：`git grep -n 'LinkStateFile\|state\.json' -- src/hyprial/daemon/impl/links` 为空（B14）。
13. ledger 同事务：`git grep -n 'link_ledger' -- src/hyprial/daemon/impl/inbox` 的写入点覆盖 §3.9 表中的每个事务（submit、outcomes、delivery/retry、core、receive、projection、lifecycle），在证据文件逐行列出。
14. 先红提交：分支上存在只含测试与骨架的提交，位于实现提交之前；证据文件含它在 base 上的行为红输出（WP4 验收第 1 条）。

## 6. 要留的证据

写入 `docs/evidence/wp2-send-links-implementation-2026-10-14.md`（仓库内，不只在 PR 正文）：

1. base SHA 与各提交 SHA；§1.3 各参照文件在 base 上的实测行数。
2. 先红：骨架状态下 §4 每个文件的失败输出摘录（行为红，不只是 `ModuleNotFoundError`）。
3. 转绿：焦点命令的完整尾部输出与通过数；验收 11 的计数输出。
4. 结构门、模块边界门与 ruff 的命令和退出码；`links/`、`composition/`、`tests/daemon/links/`、`tests/daemon/links/service/` 的文件计数；改动文件的 `wc -l`。
5. 线程证据：G12 记录的线程 id 与 inbox actor 线程 id 一致的断言输出；G13 各阻塞点的耗时数值（只作记录）。
6. 端到端日志（`tmp_path` 下两个 service 共享 `FakeOrgFs`，真实 SQLite ledger）：A→B 首次发送得 `LINK_MINTING` → A 意图提交 → B 回写 → A 放行 → 两条 `link.minted` → drop → `link.reclaimed` → 回复旧 id 得 `LINK_RECLAIMED`。
7. L14 的四个中断点的重建输出。
8. §4.9 的受影响测试清单，以及每处的修法（a 或 b）。
9. 未实测项显式标注：
   - BLOCKED（交 WP8 E2E）：真实两机 orgfs 副本延迟、跨节点 PAC 绑定、多 org 空间。
   - UNKNOWN，记为已知的进展限制：冷启动无基线时 agent 出站永不放弃（wp1 A4，SA16 的行为断言为 PASS）。
10. 验收 13 的 ledger 写入点清单。

## 7. 对缝的需求（不在 WP2 改 ports；供集成者裁定）

v1 的 S1（`LinkSendObserver`）、S2（`governed`）、S5（跨节点内部 actor）、S7（内部 actor 集合）、S8（PAC 不 hold）与 S9（`PEER_FULL` 估计）已由裁定 43、44、45、51、48 落定，并入正文，不再列出。

- **S1〔WP4 前置〕`LinkAdmission` 暴露两个只读属性**：
  - `internal_actors: frozenset[str]`：裁定 45 要求，WP2 据此取保留名。
  - `gate: LinkGate | None`：W2 与 §4.9 (a) 需要让 `_link_gate` 与已绑定的门是同一对象（B2）。
  - WP4 返工分支 `8c720faf6` 只有私有的 `_internal_actors` 与 `_gate`。若合入时仍未暴露，由集成者在 `dispatch/admission.py` 加这两个 property（各 3 行），验收 8 以此为例外。⛔ WP2 不读私有属性。
- **S2 WP4 的 `link_governed` 与裁定 45、51 不一致**：返工实现按完整 URI 集合判定（只含本节点的两个 URI），而裁定 45、51 要求按 actor 保留名、不限节点。WP2 在 `admit_dispatch` 与 `sender_known` 里按名兜底（§2.2），但 ingress 的 `sender_known` 只在 WP4 判为受管时才会被问到，dispatch 同理。WP4 修正前，远端内部 actor 的消息仍可能被 WP4 侧挡住。需 WP4 按裁定修正。
- **S3 `LinkGate.settle_hint` 的文档**：写明「只在 inbox 判定仍 pending 时被问；gate 只答该请求是否已是回收残余」（WP3 §7.7），避免实现者照 `policy.settle_hint` 对不在内存的请求答 `ack`。
- **S4 `LinkChangeSource` 无接收端**：inbox 没有对外的 pending.wait 唤醒入口，WP2 的 `subscribe` 当前无人订阅，interactive 的重评延迟最坏 5 s（WP4 §6）。建议 WP4 或 inbox 暴露 `poke_waiters()`，由 composition 订阅。
- **S5 E2E 开关的删除**：WP4 的 `HYPRIAL_E2E_PERMISSIVE_LINK_GATE` 注释写明「WP2 接线后删除」，但验收 8 要求 WP2 不改 `dispatch/admission.py`，且该开关的读取点在 WP4 的 `application/__init__.py`（990 行）。WP2 保留它，按 W2 的行为兼容。删除由集成者在 WP8 前做。
- **S6 多 org 空间的可见性**：裁定 50 待 Allen 裁；本规格按选项 a 实施（§3.10）。

## 8. 评审

> 已并入正文 v2，见各小节（对照表 §0.1；与裁定冲突的两项见 §9）。本节保留评审原文，不再修改。

结论：`BLOCKING:17 ADVISORY:4`。正文存在授权绕过、误回收与恢复丢账的问题。实施必须采用 B1–B17 的修正规格；正文与评审冲突时，以评审为准。A1–A4 的分类为 `建议`。

评审依据：设计 §3–§6、§8.1、§14；本目录 README 的裁定 1–50；当前 `ports.py`；WP1 已合入代码。基线为 `49231c994`。README 第 7、50 条仍待 Allen 确认，本评审沿用当前实施指令，不把扩展写成已获确认。

### B1〔阻断〕正文未采用已经提交的缝裁定

位置：依据行、§2.2 第 3 步、§2.3、§3.4、§7 S1/S2/S5/S7、G4、验收第 7 条。README 第 43–46 条已裁定，不能继续按“待裁定”实施。

**修正规格：**
- `LinkSendObserver` 已存在并从 daemon 公共面导出。按现有签名实现与调用，不再采用 S1 的未采纳分支，不改 `ports.py`。
- WP2 必须修改 `policy.governed`：所有 `user:` 目标均不受 link 授权检查。修改 `test_policy.py` 中非 owner 用户的旧断言。service 的短路不能代替此修正。验收第 7 条允许这一处已裁定的 policy 改动。
- 内部 actor 判定必须用 `parse_agent_uri` 取 actor 段。`dispatch-coordinator` 与 `_turn-hooks` 跨节点同样豁免。从 WP4 的只读 `internal_actors` 属性取配置，不复制构造表达式，也不在 links 中 import dispatch。
- 删除 G4 的“他节点同名 URI 不豁免”断言。补测本地、远端两个保留名与普通远端 actor；后者仍受管。
- 样本不走 hook bus，采用 README 第 46 条。多 org 写入暂按第 50 条镜像；不等待 S4 再作实施选择。

**先红：**现有 policy 对 `user:p` 返回 `Mint`；远端内部 actor 返回 `Mint` 或被误判未知。

### B2〔阻断〕缺 gate 与非本节点 agent 变成发送授权豁免

位置：§2.2 第 1 步、§2.4、G3。依据：设计 §4.1、§4.6，native 与 hosted 不豁免。

**修正规格：**
- 在发送入口完成规范身份解析后，所有受管 agent 间发送必须询问 gate。不得因 CLI、adapter、daemon 内部调用或发送者未被 `is_local_agent` 识别而直投。
- 已认证为本机 agent 的发送者按规范 URI 创建或获取节点状态，不以在线或托管状态决定授权。不能绑定本机身份的受管发送，返回既有身份校验错误或 `DAEMON_NOT_READY`；不得 `Allow` 后只记日志。远端投递的接收仍走 ingress，不冒充本机新发送。
- gate 未绑定时，受管新请求留发送方 outbox，返回 `LINK_SELF_FULL`，detail 为 `gate-unavailable`；gate 绑定后重评。豁免目标沿用现有能力检查。显式测试放行门必须同时绑定到发送与接收路径，不能靠 `_link_gate is None` 放行。
- 补测 CLI/session/adapter/daemon 四种来源与本地 native/hosted。另测未知本地 agent、远端 agent 冒作本机发送者、启动未绑定与关停期间发送。

**先红：**G3 及 gate=None 的受管发送发生投递 I/O。

### B3〔阻断〕目标短路绕过回复校验，intent 又绕过入站计数

位置：§2.2 第 2–4 步、`admit_dispatch` 记账、§3.1。依据：设计 §4.5；WP1 评审 B2/P10。

**修正规格：**
- 本节点 agent 带非空 `reply_to` 时，先查排干记录，再校验原请求存活、属于该 actor、requester 与 target 匹配；随后才应用 owner、用户、内部 actor、自发与 route 的授权豁免。错误码沿用 `LINK_RECLAIMED`、`MESSAGE_REPLY_UNAVAILABLE`、`INVALID_ARGUMENT`。
- 来自豁免来源的有效回复仍必须闭合原入站事项，并记本节点的 T 与 sent 吞吐样本。adapter/channel 的传输能力检查不得绕过此记账。
- 对受管消息，无 `reply_to` 一律按新请求计 W，不能仅以 `intent == "request"` 创建 `PendingRequest`。`intent="reply"` 但无关联 ID，不得在首次准入后漏账。
- 带关联 ID 的入站回复，仅在 actor、peer 与开放出站请求均匹配时闭合。错误 peer 不得消耗他人的出站计数。
- 补测 owner/非 owner 用户的未知 ID、已 ack ID、错误 target；普通 send 不闭合原入站；连续多个无 reply-to 的 reply-intent 消息只能占用剩余 W。

**先红：**用户目标的未知 reply-to 得 `Allow`；reply-intent 连续准入却没有增加未闭合计数。

### B4〔阻断〕非 owner 用户的新请求获得了未裁定的 W 豁免

位置：§2.2 第 3 步、§3.8、G2。依据：设计 §2、§4.1；README 第 37 条只豁免 link 检查与 k。

**修正规格：**
- 向非 owner `user:` 发新请求时，不查 link、不铸造、不占 k，但必须检查发送方 W。W 满时返回 `Hold(LINK_WINDOW_FULL, target, "W")`，消息留 durable outbox。
- 放行时仍经已有 squire/user-proxy 或 adapter 通道投递，不把人改成 agent link endpoint。composition 负责把 held 用户投递接回该通道。
- 有效回复不新增事项，W 满也放行。owner 通道沿用现有 owner 豁免，不扩展到其他人。
- 把 G2 的“W 满时 A→U 仍 Allow”改为 hold；补测到 U 的在途释放后自动放行、重启保持 hold、TTL 到期、不产生用户 link 文件。

**先红：**W 已满时 A→U 仍接受立即发送，并使发送方开放事项继续增长。

### B5〔阻断〕held 回复会被现有 reply 路径提前 ack，排干错误码也被提前覆盖

位置：§2.4、H11、W4；现有 `send.py::_ipc_message_reply`。现有实现只要 submit accepted 就可能 ack 原请求；找不到原请求则先抛 `MESSAGE_REPLY_UNAVAILABLE`。

**修正规格：**
- reply 入口在普通 pending 查找失败前，查询该 actor 的排干记录；排干 ID 必须返回 `LINK_RECLAIMED`。不得要求已 ack 的排干请求仍出现在 `pending_messages`。
- 因 link 原因 held 的回复虽已 durable accepted，但原请求仍 pending；不得走当前“accepted 即 ack”分支。返回 `ok:true, queued:true, holdReason`，不使用表示失败的 `REPLY_SETTLEMENT_PENDING` 代替 link hold。
- 回复放行且 durable 更新成功后，才闭合原事项、记 T，并按现有结算协议 ack 原请求。失败、过期或排干的 held 回复不得投递，也不得当作已成功回复。
- H11/W4 必须走真实 SQLite 与真实 message.reply IPC，不只使用 FakeInboxPort。

**先红：**held 回复的原请求提前 consumed；排干后 reply 得 `MESSAGE_REPLY_UNAVAILABLE`。

### B6〔阻断〕gate 在幂等回执前检查，重复 send/reply 会再次占位或报错

位置：§2.2 的预留与 submitted、§2.4。依据：现有稳定 messageId、submission receipt 契约及 ports 的幂等要求。

**修正规格：**
- 发送边界先计算稳定 messageId，并按既有 digest/receipt 规则识别重放，再对新操作调用 gate。重放返回原操作的 durable 结果，不重新授权、预留、铸造或投递。
- 相同 ID、相同内容的 reply 重放，在原请求已结算后仍返回既有回执。相同 ID、不同内容沿用既有冲突行为，不改成新的回复。
- `submitted` 对请求、回复与 held 登记均按 `(sender, message_id)` 幂等。不得仅对 outbound 追加做去重；重复回复不能重复记 T、吞吐或闭合。
- submit 被拒、抛错或回执不确定时，不得登记为成功发出；未成功操作的预留必须能释放。成功 submit 后、observer 前崩溃的恢复见 B14。
- 补测 W 满后的 send 重放、已 ack 后的 reply 重放、held 重放、digest 冲突、重复 observer、提交失败与并发同 ID。

**先红：**重放被 W 挡住、reply unavailable，或样本/在途多 1。

### B7〔阻断〕T/RTT 使用准入时刻，live-only 对账又漏掉快结算回复

位置：§1.2 的 InboundRow/LinkInboxPort、§2.2、§3.1、SA2/SA4。依据：设计 §5.1、§5.6。

**修正规格：**
- 所有 T 均用 inbox 的 `received_at_ms`；所有 RTT 均用回复的 `received_at_ms - sent_at_ms`。延迟 dispatch、读 body、service tick 不得计入网络 RTT。
- 回复在下一 tick 前发出时，也必须从权威原行取得到达时刻；不能依赖 reconcile 曾经校正过暂存的 `now_ms`。读取发生在 IPC/service 的锁外，gate 在 inbox actor 上仍只读内存。
- 端口必须提供尚未处理的 durable 回复观察，包含已 consumed 的回复，或在接收事务中保存待处理的关联记录；不能只扫描 live rows。确认闭合后按 ID 标记已处理，重复 tick 不重复采样。
- held 不算已发出；普通 outbox 排队也必须区分 durable 接受与实际放行/发出。实际发出的计时事实由 inbox 经 composition 注入，不由 links 查询传输模块。
- 补测到达后延迟 30 s 才 dispatch 的回复与 tick 前 reply/ack 的入站。另测 tick 前 consumed 的用户回复、错误 peer 的关联、普通传输排队与 link hold。

**先红：**RTT 多出 30 s、T 从准入算起，或快回复永远没有闭合出站。

### B8〔阻断〕豁免来源只看 fetched_at_ms，漏掉 managed 注入与即时容量

位置：§3.1 reconcile、SA3。依据：设计 §3.2、§5.3、§5.6；`inbox/authority/read.py::unfetched_recipient_stats` 已说明 push 路径不写 fetched_at_ms。

**修正规格：**
- 豁免 ingress/dispatch 的消息仍必须在实际注入时登记未闭合入站。owner 不受 W 准入，但 owner 引发的待回复事项计入节点负载。
- composition 必须适配 pull 的 fetch 与 managed/headless 的接收确认。新增本地纯内存通知方法或注入回调，传 actor、messageId、sender、reply-to、received_at_ms；不得让 service import inbox，也不得用可丢 hook 代替精确计数。
- 注入登记必须先于下一条受管新请求的准入。每秒 reconcile 是恢复兜底，不能允许同一 tick 内把 owner 已占的名额再次借给 agent 请求。
- 同一消息多次 pending.list/wait、read_body 与 push 重试只计 1 次；尚未注入的 inbox 等待行不计 W。
- 补测 managed owner 请求 fetched_at_ms 为 NULL 仍计数、interactive fetch 后立即 agent 准入、无注入等待行、同消息跨两条观察路径。

**先红：**managed owner 请求不计 W，或最后 1 个名额被再次准入。

### B9〔阻断〕审查集合只存过期时刻，不能支持迟到回复与恢复

位置：§1.1 LinkLocalState、§2.1 for_reply、§3.1、SA6。依据：设计 §5.1、§5.6；README 第 34 条。

**修正规格：**
- 审查记录必须保存 actor、messageId、requester、received_at_ms、审查阈值与保留期限。不能只存 `id -> expires_at_ms` 后再要求组装 PendingRequest。
- 审查后的请求不计 W，但在仍 pending 时可显式回复。reconcile 不得因 fetched 行仍在而重新加回 pending_inbound；回复不再补记第 2 个 DwellSample。
- 排干/ack/TTL 后清除活请求资格；保留的审查去重标记不得被当作可回复请求。
- 阈值回退只用未审查的 T_max/RTT_max。没有基线时保留冷启动状态，不把审查值当新基线扩大阈值。
- 补测审查后重复 reconcile、重启后迟到回复、审查后 ack、审查后 link 回收、跨窗口阈值不自我放大。

**先红：**视图没有 requester、审查行重占 W、重启后重复采样或旧请求错误 Allow。

### B10〔阻断〕回收仅处理已准入视图，漏掉 inbox 等待残余与 outbox 重试

位置：§2.1 residue 映射、§3.2、R2。依据：设计 §4.4，回收后不得重新授权残余。

**修正规格：**
- 回收必须排干来自该 peer 的全部存活待回复请求。范围包括未准入等待行、已 fetched 行、已审查行与尚未 reconcile 的行。`policy.residue` 只覆盖内存视图中的请求；composition 注入的 inbox 端口必须补齐权威行。
- 按 `(actor, messageId)` 保存排干记录并移除 admitted/pending/censored。不得仅按全局 messageId 判断另一个 actor 的回复资格。
- 同步清除该 peer 的出站开放事项及预留。已放行但仍在普通 outbox 重试的残余不得继续出门；通过 inbox 命令取消/终结对应重试，记录 `LINK_RECLAIMED`。不得重铸后自动重发旧请求。先前仅等待铸造、从未放行的全新 held 请求继续按 §4.2 重评，不把全新请求误作在途残余。
- 排干记录在 ack 前必须 durable；端口失败时重试排干，不恢复回复授权。新铸 link 不解除旧 messageId 的排干标记。
- 补测 W 满而未注入的残余、已审查残余、drop 与并发准入。另测 drop 时普通 outbox 尚未实际投递、重新铸造后的旧回复。

**先红：**未注入行留 pending；drop 后旧 outbox 行仍发生投递。

### B11〔阻断〕副本文件消失被当作明确回收

位置：§3.3“P 的文件整个消失同样处理”、H11、R5。依据：设计 §4.6、§5.6：副本对不上只 hold 不丢。

**修正规格：**
- 文件暂缺、内容 pending、读失败、空间观察不完整均只使未新准入的消息等待。不得据此排干、清出站或发布 link.reclaimed。
- 对端撤回必须有权威事实。依据为同一 actor 的较新合法完整文件明确不再引用本 actor，或明确的生命周期回收操作。旧版本回滚不得解释为撤回。
- 索引保留已观测最高 revision 与未知/暂缺状态的区别。已准入幂等 ID 仍按 ports 放行，新的消息等待；TTL 沿用原语义。
- 两份引用都明确撤回后才能清回收屏障；仅两份文件暂缺不得清屏障再接受旧引用。
- 补测 established 后整文件缺失、读错误、内容未到、旧版本出现、恢复原互引，均不发回收事件；较新明确撤回才排干。

**先红：**暂移出 B 文件就 ack 原请求，H11 无法恢复放行。

### B12〔阻断〕按 peer 合并多空间文件会复活已删除的授权

位置：§1.1 合并、ST3、§3.10。依据：设计 §6 的单写者与修订号；README 第 50 条镜像不意味着授权并集。

**修正规格：**
- 一个 actor 的完整文件是该单写者的一次快照。多空间中选择最高文件 revision 的完整快照；不得把旧快照中的 peer 合并回新文件。
- 最高 revision 的多个副本内容一致时使用该快照。相同最高 revision 内容冲突时，标为不可确定，受影响授权只 hold，并记冲突；不得按空间顺序选能放行的一份。
- 本地保存已见最高 revision，低版本到达不能回退；writer 镜像重试必须传播删除后的完整文件。
- ST3 增加“空间 1 rev=2 有 B，空间 2 rev=3 已删 B”，结果不得含 B；再测同 rev 冲突、乱序到达、离开空间、写失败后重试删除。

**先红：**撤掉 B 后旧镜像仍让 A–B established。

### B13〔阻断〕owner 相等不能证明本节点写权，失败写入却能形成有效互引

位置：§1.1 LinkStore 构造/写、ST4/ST6、§3.3/§3.4。依据：设计 §6。

**修正规格：**
- LinkStore 增加注入的 `is_local_agent` 检查。只允许本节点规范 agent URI；同 owner 的远端 agent 与 user URI 均不得写。owner 作者校验继续用于读取，不代替节点单写者检查。
- 区分待写意图与已 durable 成功的本地文件。待写引用可预占 k、防并发超额，但不能作为 effective 或 sender_known 的文件证据。
- 单边写成功前，消息一直 held。本地空文件补建失败时，也不能让 sender_known 返回 True。`stale-write` 重试前仍须检查本地写者与当前期望 revision。
- `org_available` 必须反映存活且可写的空间；只读、not-a-member、closed 或写入失败不得被当作成功建立授权。多空间按 B12 发布已提交快照。
- 修正 ST4 为“待写意图立即可见，但有效索引仅在写成功后更新”。补测同 owner 远端 actor、只读空间、所有镜像写失败、对端已引用但本侧写失败、补建失败。

**先红：**写远端同 owner 文件未抛错；本侧写失败仍放行消息。

### B14〔阻断〕每 tick 才保存旁路状态，不能兑现崩溃后的精确计数与排干契约

位置：§3.2 pass 顺序、§3.9、ST10/G9。依据：设计 durable outbox；ports 的已准入幂等；AGENTS.md 的 daemon 权威边界。

**修正规格：**
- 开放出站、已准入入站、审查/放弃去重、排干标记必须从 durable 事实恢复。不得在 state.json 损坏时直接清空后继续新准入。
- 在现有 inbox SQLite 权威中保存 link 记账元数据，不保存第二份消息正文。SubmitMessageCommand、首次准入事务、UpdateLinkHoldsCommand 与排干 ack 事务分别写入对应记账记录；记录按 `(actor, messageId)` 唯一。
- composition 的 LinkInboxPort 负责读写这些记录。links 只消费注入 Protocol，不 import inbox service。`LinkStateFile` 改为可重建缓存；控制器样本仍可在重启时冷启动。
- hold 放行的开放出站记录与清 link_hold 同事务提交。排干标记与 consumed 更新同事务提交。事务失败保留原 hold/排干待处理状态，不能对外确认完成。
- inbox actor 的 gate 只作内存判定，durable 记录由调用方在已有事务写入；提交失败必须回滚本次内存预留/准入，或在继续准入前完成对账。
- 补测进程中断于 submit 后 observer 前、准入后 tick 前、清 hold 后缓存保存前、ack 后缓存保存前；损坏/删除 state.json 后重启仍不漏账、不丢 LINK_RECLAIMED。

**先红：**仅优雅 close 的 G9 能过；强制重建丢出站或把旧排干 ID 当普通请求。

### B15〔阻断〕held 行缺 detail 与完整元数据，失败/到期更新也会留下幽灵在途

位置：§1.2 HeldRow/端口、§2.2 submitted、§3.2、SA9、O5/O6。ports 的 observer 没有 intent/detail/expires_at_ms，不能凭回调参数构造完整 HeldRow。

**修正规格：**
- submitted 只按真实 ID 登记待核对操作；完整 held 元数据从 durable outbox 读取。HeldRow 增加 detail，持久化或在每次重评时以当前 policy 结果重算，不能猜所有 WINDOW_FULL 都是 cwnd。
- ControllerInput.held_peers 仅取已确认的 cwnd hold。W、minting、org 不可用与副本延迟不得触发该 link 的容量缩窗。
- UpdateLinkHoldsCommand 必须按原 hold 状态、messageId 与未过期条件更新，并返回实际成功的 ID/状态。内存仅对成功放行者新增 outbound/闭合回复；失败重试不能重复计数。
- 到期行优先 expired，不再 Mint、重写引用或新增在途。release 与 expire 竞争只产生一个终态；link hold 与普通重试泵均不得重复投递。
- 补测重启后区分 cwnd/W detail、update_holds 超时/回滚、放行前 TTL 到期、reply 原请求先 ack/过期、重试泵与放行并发。

**先红：**W hold 误缩 cwnd；更新失败或到期行被计入 outbound。

### B16〔阻断〕PAC 回收只靠可丢 bus，单 taskId 也不能覆盖多任务绑定

位置：§3.7、R7–R9。依据：设计 §4.4；README 第 7、48 条。

**修正规格：**
- bus 的 worker.cleanup.complete/graph.closed 只作 wake-up。composition 注入只读 PAC 生命周期快照端口，启动与 tick 核对任务和 worker 的 durable 状态；丢事件、hooks 关闭、重启均必须最终回收结束任务的 PAC link。
- 任务绑定必须在本地 durable 元数据中记录 `(principal, worker, taskId)`。同一对端参与多个活动任务时，最后一个绑定结束才回收；原本 regular link 不得因任务结束而回收。单条 LinkEntry 的 taskId 只作当前代表绑定，不替代完整绑定集合。
- PAC 直接投递的每个真实 messageId 只登记一次开放事项。没有成功投递回执时不回填成功；已有 regular link、同节点与远端路径均计入 W/在途。
- PAC 发送方不 hold 是 README 第 48 条的明确例外，不把该例外扩展到普通 message.send。接收方仍用 W 准入。
- 注册和释放所有 bus consumer；startup 失败及 close 必须取消订阅。service 不 import PAC，生命周期读取与事件适配均在 composition。
- 补测同对端两个 task、regular 加任务、遗漏两个终态事件、关 hooks、跨重启已结束任务、重复投递回填；远端实测仍交 WP8，不能以假实例声称已完成。

**先红：**丢 graph.closed 后 PAC link 永存；第一个 task 结束就回收另一个任务的授权。

### B17〔阻断〕缺少 service 级 §5.6 劣化测试与 ports 完整契约测试

位置：§4/§5。WP1 的纯函数测试不能证明 service 正确组装输入、回收样本和调度周期。

**修正规格：**
- 在现有 service 六个测试文件中增加确定性集成场景，不新增第 8 个直接文件。每条必须驱动真实 service.tick 与假时钟，不只打桩 evaluate。
- 服务率随负载下降：多周期喂实际 sent/received 样本，断言增量最多 1、仅非正梯度增长、W 不低于 k。
- 非拥塞慢与后来者公平：旧快样本过窗后两条 link 使用新基线；SAMPLE_CAP 淘汰不能把另一个 peer 的在途记录或审查标记一并删掉。
- 队头阻塞：放弃释放后下一 held 新请求自动放行，旧请求不自动重发；审查只记阈值一次，且不进入 T_max/RTT_max。
- link 抖动：最近使用事实实际影响 LRU；最短存活期内不淘汰，churn 退出后恢复铸造。多实例随机抖动采用不同种子，同种子可复现。
- 分区恢复采用 B11/B12；冷启动覆盖无样本、少于 10 个样本与第 10 个样本；owner 优先级采用 B8。
- 以 runtime_checkable 检查 service 满足 LinkGate、LinkQueries、LinkAdmin、LinkChangeSource、LinkSendObserver。subscribe 取消后不再回调；回调在锁外，允许安全回入查询。
- G13 扩展为 service 阻塞在 orgfs read/write、inbox 读取/更新、旁路保存时，sender_known/admit_dispatch 仍不等待 I/O；测试并发两条首次准入争最后名额与多目标 send 预留。

**先红：**缺场景测试；错误方向、长期基线、锁内 I/O 或过量放行使场景断言失败。

### A1〔`建议`〕无基线时的进展限制宜有场景证据

WP1 A4 允许冷启动不编造超时常数。宜测试首次 k 条 agent link 永不回复、W 无基线时无法替换目标的组合。行为断言可 PASS，恢复进展仍写 UNKNOWN；不得把限制藏在全量绿后面。若引入绝对超时，须先裁定并放入 ControllerConstants。

### A2〔`建议`〕50 ms 不阻塞断言易受宿主调度影响

宜用 Event/Barrier 证明 service I/O 阻塞期间 gate 能独立完成，并设有界总超时。50 ms 可记录为耗时证据，不宜作为唯一正确性断言。测试不得用 sleep 猜线程已进入阻塞区。

### A3〔`建议`〕admin add 的 queued 需要说明恢复义务

§2.5 满额 add 返回 queued，但没有 messageId 或 durable 命令。宜保存待执行的 admin 意图并在容量变化后重评；或明确返回“未写入引用”的状态，不能暗示已经排队。相同 add 重放、drop 取消意图与重启行为宜加入测试。

### A4〔`建议`〕结构预算与证据入口需要按集成后的树复核

宜把规格中另一个 `/private/tmp/` worktree 的参考改为仓库路径与提交 SHA。接线前复核 WP3-A/WP4/WP6/WP7 合入后的 transport 行数与各目录文件数。S-B2 的 `.py` 数不能替代计入文档/数据的官方结构门。

### 8.1 评审验证记录

- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links -q`，87 passed，2 个依赖 deprecation warnings，退出 0。仅证明现有 WP0/WP1 基线；不证明 WP2 尚未实施的行为。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `check_module_boundaries.py`，均退出 0，`violations: []`。
- PASS：新增评审节的 STC 检查，0 errors、无待复核 Anti-Echo 疑点。
- N/A：只改 Markdown。`uv run --with ruff ruff check --force-exclude --extend-exclude '*.md' docs/design/send-links-impl/wp2.md` 退出 0，提示未发现 Python 文件；不把排除后的结果当作 Python lint 验证。
- 依赖审查：§0 第 2 条及 composition 注入方向符合设计 §14。B7/B8/B14/B16 的补充仍必须保持 kernel→identity→daemon→biz→shell 单向，links 不 import inbox/PAC，biz/shell 只消费 daemon 公共面。
- UNKNOWN：WP2 运行行为、全量产品测试、真实两机副本延迟、远端 PAC 与多 org E2E。本次只追加规格评审，不改产品、测试或 ports。

## 9. 与裁定的冲突（v2 新增，待集成者裁定）

下列两条评审意见与已提交的裁定冲突。v2 不自行裁决：正文按现行裁定写，并给出采纳评审时的改动清单，裁定后由集成者通知实施节点切换。

### C1 · B4（非 owner 用户的新请求受 W 约束）

- 评审原意：向非 owner `user:` 发新请求时不查 link、不占 k，但必须检查发送方 W。W 满时返回 `Hold(LINK_WINDOW_FULL, target, "W")`，消息留 durable outbox；放行时仍经 squire/user-proxy 或 adapter 投递，由 composition 把 held 的用户投递接回该通道。G2 的「W 满时 A→U 仍 Allow」改为 hold。
- 冲突的裁定：
  - **37**：落地写明「所有 `user:` 目标……投递走 squire/user-proxy 或 adapter 既有路径」，对 W 只写了「计入发送方的 W」，没有写「受 W 挡住」。设计 §4.1 的目标表同样只写「未闭合请求计入发送方 W」。
  - **47**：hold 的机制是「留在发送方 outbox 的 `link_hold` 列，放行即清列」，由 retry 泵投递。现有的 `user:` 投递不经 outbox（`send.py` 直接调 agent bot 或 `user_delivery`），retry 泵不能投递 `user:` 目标。采纳 B4 需要一条新的「outbox → 用户通道」投递桥，或一个独立的用户 hold 队列，两者都不在裁定 47 的范围内。
- 正文现行：计入 W 与 `outboundInFlight`，不 hold（§2.2 步骤 3、§3.8、G2）。
- 建议：采纳 B4 的语义。它与设计 §2 的不变量「节点未闭合总数不超过 W」一致，而现行写法让向人的请求可以无限超出 W。需补一条裁定，选定投递桥的形式：建议用 outbox 行承载 `user:` 目标，放行时由 composition 取出改走既有用户通道，约增 150 行，在 `link_send.py` 与 retry 泵的分支中。owner 目标是否同样受 W 约束也请一并裁定；评审只要求非 owner，设计 §3.2 只豁免 owner 发往 agent 的方向。
- 采纳后的改动：
  - §2.2 步骤 3：W 满时返回上述 `Hold`。
  - §3.8：删去「照常投递」一句。
  - G2：最后一项断言改为 hold。
  - test_hold_release 增一条：到 U 的在途释放后自动放行、重启后仍 hold、TTL 到期、不产生用户 link 文件。
  - 总数 +1。

### C2 · B2 的一项（gate 未绑定时，受管新请求在发送方 hold）

- 评审原意：gate 未绑定时，受管新请求留发送方 outbox，返回 `LINK_SELF_FULL`，detail 为 `gate-unavailable`；gate 绑定后重评。不能靠 `_link_gate is None` 放行。
- 冲突的裁定：
  - **26**：「未绑定 gate 时的缺省：agent 间消息入库但不派发，其他不受影响；ingress 不因缺 gate 而拒收。」它把未绑定的缺省定在接收方（入库、不派发）。评审在发送方再加一层 hold，形成第二个缺省。WP4 验收第 3 条刚以「唯一缺省」为由拒收过第二套缺省。
  - 另：`gate-unavailable` 是新的 detail 值，WP6 的渲染与 WP8 的 contract 都需知悉。
- 正文现行：gate 为 None 时沿用现状直接 submit（§2.4 第 3 步）；接收方按裁定 26 不派发。B2 的其余部分已并入：非本节点发送者报错（§2.2 步骤 0）、各来源一律询问 gate（W6）、放行门同时绑定两条路径（§2.3、§4.9）。
- 建议：采纳评审，并把裁定 26 修订为「发送方 hold（`LINK_SELF_FULL/gate-unavailable`），接收方不派发」。理由是设计 §4.6 的「发送方先拒」：远端接收方若已有真实 gate，现行写法会让未授权的消息先出门再被挡。接线后 gate 为 None 只出现在启动窗口与 W12 的启动失败，代价小。
- 采纳后的改动：
  - §2.4 第 3 步：以 `app.link_admission.governs(sender, target)` 判受管（WP4 已有该只读谓词）。受管新请求 `submit(link_hold=LINK_SELF_FULL)` 并返回 `detail: "gate-unavailable"`。`service.start()` 读 `held_outbox()`，绑定后的第一次 pass 即重评。
  - test_wiring 增一条：未绑定时发送 hold，绑定后放行。
  - 总数 +1。

### 9.1 未冲突，但需集成者知悉的扩展

- **B8、B7 的 inbox 侧回调**：`injected`、`reply_received` 与 `discard` 是 inbox 在注入与接收事务提交后对 service 的同步内存回调，经 inbox 自有的 `LinkLedgerSink` Protocol。裁定 46 列了三个样本来源（send 边界回调、`admit_dispatch`、每 tick 对账），这是同类的第四个边界直喂，不走 bus。请确认它在裁定 46 的意图之内。
- **B14 的 ledger 表**：新增 inbox SQLite 表 `link_ledger` 与 `link_task_bindings`。它们是内部 schema，不是已发布字段；`outbox list` 的 `holdReason` 才是已发布字段，归 WP8 同步 `contract/`。
- **§2.4 `message.reply` 写 `replyTo`**：v2 在并入 B7 时发现，`message.reply` 对 agent 目标不写 body 的 `replyTo`（现状只对 adapter 写），接收方据此无法关联回复。v2 把它列入 WP2 范围；它不是评审条目，digest 变化的理由同裁定 39。
- **WP4 的 `link_governed` 与裁定 45、51**：见 §7 S2。

### 9.2 建议（A1–A4）的取舍

四条全部采纳，落点见 §0.1。A3 在两个选项中选了「明确返回未写入」：`add` 没有 messageId，保存待执行的 admin 意图需要新的 durable 命令与取消语义，超出 WP2 范围；运维可以重试。不采纳的建议：无。

### 9.3 集成者裁定（2026-10-14，README 裁定 53–55）

- C1 不按 B4 的 hold 落地，按裁定 53：非 owner `user:` 目标在 W 满时**同步拒绝**（`ok:false`、`LINK_WINDOW_FULL`、detail `W`），不入 outbox、不建投递桥；owner 目标计入 W 但永不被挡。§2.2 步骤 3、§3.8、G2 据此改写；§9 C1 列的 hold 测试改为一条同步拒绝测试（test_gate）与一条 owner 不受挡测试（test_gate），总数 +2。
- C2 采纳，按裁定 54：§2.4 第 3 步按「采纳后的改动」执行，裁定 26 同步修订。test_wiring +1。
- §9.1 四项按裁定 55 确认；`message.reply` 补写 `replyTo` 留在 WP2 范围。
- 实施节点以本节为准；正文中与本节不一致处，以本节为准。

### 9.4 集成者补充（2026-10-15，实施开始前）

- 基线：集成分支已合入 WP0、WP1、WP4（四次验收）、WP7；WP3-A 与 WP6 仍在返工/待合，与本包并行。本包**不改** `inbox_surface/queries.py`（WP3-A 的 read_body/settle_hint/pending.list 归它），只改 §1.3 列出的文件。
- README 裁定 67：gate 只有一个绑定槽 `DaemonApplication.link_admission`（WP4 `LinkAdmission`，已合入，含只读 `internal_actors`、`gate` 属性与 `governs(sender, recipient)`）。§9.1 所列 `_link_gate` 依赖按此读：本包通过 `app.link_admission.bind(gate)` 绑定一次，不新设 `_link_gate`；发送边界的受管判定用 `app.link_admission.governs`。
- README 裁定 63：内部 actor 保留名常量在 kernel `RESERVED_AGENT_ACTOR_NAMES`，本包不复制。
- README 裁定 53/54/55 已在 §9.3；裁定 56（settle_hint 对非 request 行答 ack）属 WP3-A/WP5，本包的 `LinkGate.settle_hint` 实现只按 ports 注释答。
- 合入时集成者删除 `admission.py` 的 `E2E_PERMISSIVE_LINK_GATE_ENV` 读取点与 `tests/support/links/gate.py` 的替身（WP4 验收待办），本包的真 gate 接线必须让依赖该替身的隔离 E2E 场景改走真 gate；请在 --output-text 列出受影响的测试。

### 9.5 集成者补充（2026-10-15 02:xx，README 裁定 68）：拆为 WP2-A / WP2-B

第一次实施（提交 aef0f729f…db48f0eb6）交付了 store 与 service 引擎，未交付接线与 inbox ledger，且 `links/service.py` 2011 行超 1000 行硬门。拆分如下，正文 §1–§8 的要求不变，只是分到两个分支交付。

**WP2-A（本分支 wp2/send-links，返工）**
1. 把 `src/hyprial/daemon/impl/links/service.py` 拆为包 `links/service/`：`__init__.py` 只重导出 `LinkService`（及现有公共名），保持 `from hyprial.daemon.impl.links.service import LinkService` 不变；其余文件建议 `core.py`（__init__/start/close/kick/tick/_loop/_state/_emit/_notify/_log）、`gate.py`（admit_send/sender_known/admit_dispatch/settle_hint/submitted/injected/reply_received/discard 及 _after_decision/_mint/_lookup_reply_request/_queue_head*/_view/_peer_full/_exempt_target）、`holds.py`（_evaluate_holds/_merge_apply/_merge_held/_merge_bindings/bind_task/_task_delivered/release_task/_drain_*）、`reclaim.py`（_reclaim/_withdrawn/_clear_barriers/_retract_single_sided/_reconcile/_sync_references/_emit_established/_ensure_local_files/_rebuild）、`samples.py`（_run_controllers/_sampling_writeback/_trim_samples/_ensure_controller/_n_active/_refresh_seen/_pac_lifecycle/_entry_lifecycle）、`views.py`（list_links/stats/add/drop/subscribe/top_fields 与模块级 helper）。≤7 个文件，每个 ≤1000 行（目标 ≤600）。mixin 或模块函数均可；**不改任何公共行为**，拆分前后 `tests/daemon/links` 全绿且用例数不减。`links/` 顶层仍 ≤7 个直接文件（service.py 变目录后为 6 + 目录）。
2. 补 G12、SA1–SA16（service 级劣化场景，用假时钟喂 tick）。
3. 为 G/H/R 组补独立先红日志（在对应测试提交上 checkout 实现前的 src 跑一次，输出存 wp2/ 证据目录）。
4. §8 验收标准中属于引擎的条目（1、3–9、11 的 ST/G/H/R/SA 部分、12、14）在本分支核；其余归 WP2-B。

**WP2-B（新分支 wp2b/send-links，自 db48f0eb6 派生，worktree /private/tmp/hf-wp2b-send-links）**
1. `composition/links.py`（compose_link_service、LinkInboxPort/Presence/PAC 适配器、`app.link_admission.bind(gate)`、bus 消费者、top 读取、link.service_skipped/failed 事件）与 `application/wiring/services.py`、`shutdown.py`、`operations/top.py`、`messaging/status/views.py`、`pac/actors/daemon.py` 的接线（行数预算见 §1.3；transport.py 与 application/__init__.py diff 为空）。
2. inbox ledger：`link_ledger`、`link_task_bindings` 表、`outbox.link_hold` 列、retry 泵跳过 hold、`UpdateLinkLedgerCommand`、`LinkLedgerSink`、facade/read 读接口；L1–L14。
3. `inbox_surface/link_send.py`：message.send/message.reply 发送边界接 gate（§2.2 含 §9.3 的同步拒绝）、replyTo 补写（裁定 55）；W1–W12。
4. 删除 `admission.py` 的 `E2E_PERMISSIVE_LINK_GATE_ENV` 读取点、`application/__init__.py` 的对应分支与 `tests/support/links/gate.py` 中的 E2EPermissiveLinkGate/permissive_admission_slot，把第一次实施 --output-text 列出的受影响测试改走真 gate（用 `tests/support/links/org.py` 的 establish 助手）。
5. 只通过 `hyprial.daemon.impl.links.service` 的公共方法使用引擎；**不改 `links/` 下任何文件**（WP2-A 在并行拆分它）。若需要引擎暴露新方法，在 --output-text 写明并先用现有方法绕过。
6. §8 验收标准中的 2、10、11 的 L/W 部分、13 在本分支核。

### 9.6 集成者补充（2026-10-15 03:xx）：WP2-B 再拆为 B1（已交付）/ B2 / B3

WP2-B 第一次实施交付了 inbox ledger（§9.5 WP2-B 第 2 条，L1–L13，提交 5158949da…a68bafb96），其余未做。deepseek 单会话能稳定交付的量约为一个功能簇，再拆：

**WP2-B2（本分支 wp2b/send-links，下一节点）**：§9.5 WP2-B 第 3 条——`inbox_surface/link_send.py` 与 message.send/message.reply 发送边界接 gate（§2.2 全部步骤含 §9.3 的同步拒绝与 §9.4 的 `app.link_admission.governs`/`.gate`；gate 未绑定时按裁定 54 hold `LINK_SELF_FULL`/`gate-unavailable`，hold 行写 ledger 用已交付的 `UpdateLinkLedgerCommand`/`link_hold`）、replyTo 补写（裁定 55）、W1–W12 中不依赖 composition 的条目（W3–W6 及发送边界相关）；L14 延后到 B3。不改 `links/` 下文件，不改 `application/__init__.py`、`transport.py`、`inbox_surface/queries.py`。

**WP2-B3（B2 之后）**：§9.5 WP2-B 第 1 条与第 4 条——`composition/links.py` 与接线、删 E2E permissive-gate 读取点与替身并改受影响测试走真 gate、L14、剩余 W。接线所需的 app 侧缝（orgfs 空间句柄、目录空间解析、presence 来源、hook bus、PAC 生命周期快照）由集成者先出 `docs/design/send-links-impl/wp2-composition.md` 写明属性与文件清单，B3 据此实施。**修正 §9.4 的硬约束**：B3 允许改 `application/__init__.py`，但只限删除 `HYPRIAL_E2E_PERMISSIVE_LINK_GATE` 分支（净减行），其他接线仍放 `wiring/services.py`。
