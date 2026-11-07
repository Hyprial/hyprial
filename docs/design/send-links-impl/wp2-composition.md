# WP2-B3：composition 接缝（compose_link_service 如何拿到 orgfs、presence、bus 与 PAC）

- 日期：2026-10-15
- 段：设计（集成者委派，claude-opus-5-5）
- 适用：WP2-B3 实施（分支 `wp2b/send-links`，B2 之后）。本文件是 wp2.md §9.6 承诺的接缝说明，与 wp2.md §1–§9 冲突处以本文件为准。
- 依据：README 裁定 26、37、46、47、49、50、53、54、63、67、68；wp2.md v2 §1.2、§2.3、§2.6、§2.7、§3.4、§3.5、§3.7、§3.10、§4.7、§9.4–§9.6；design §6、§14。
- 代码参照：集成分支 `refactor/org-structure-constrain` @ `aab662594`（行数均为该提交实测）；引擎与 ledger 取 `wp2b/send-links` @ `a75cccec0`（单文件 `links/service.py`）与 `wp2/send-links` @ `e341edccb`（包 `links/service/`，导入路径不变）。

## 1. 结论

五个依赖全部已经在 `DaemonApplication` 上可达，不需要构造 `OrgNetworkService`，也不需要给 `application/__init__.py` 加任何属性。上次实施卡住的原因是误判了「活跃 org 的来源只在 org network service 里」：`OrgNetworkService._orgs()` 本身就是三个可直接调用的公共函数的组合，composition 照抄这个组合即可。

| 依赖 | 精确来源 | 新增 |
| --- | --- | --- |
| orgfs（OrgFs 句柄） | `app._require_orgfs_runtime().facade`（`LocalOrgFs`）。`_startup_compose_delivery_plane` 在 `wiring/transport.py:507` 已调用过它，所以到 `_startup_adopt_domain_services` 时 runtime 一定已构造 | 无 |
| link 空间（哪些 space 放 `link/`） | `OrgFsDirectoryStore(fs).orgs()` 减 `read_left_orgs(Path(app.hyprial_home))` 减 `org_state(org) == "deleted"`，再对每个 org 调 `resolve_org_directory_space(fs, org)` 取唯一 directory 空间。与 `OrgNetworkService._orgs()`（`org/network/service.py:826`）逐项相同 | composition 内一个适配器类 `OrgLinkSpaces` |
| presence / N_active | `app._presence.raw_online_actors()`（`_LocalPresence`，`messaging/status/views.py:45`），在 `wiring/transport.py:971` 赋值，早于 adopt。每次调用时读属性，为 None 时返回空 | composition 内 `PresencePort` |
| hook bus | 发布：`HookEventPublisher(app._hook_bus, source="daemon.links")`；订阅：`app._daemon_hooks.register_consumer("links", lane, events)`（`hooks/service.py:157`）。两者都在 `wiring/transport.py:645–646` 赋值，早于 adopt | composition 内 `_LinkHookLane` |
| PAC 生命周期 | 每次调用开一个只读 `PacGraphStore(default_database_path(app.state_dir), read_only=True)`，读 `graph(graph_id)` 与 `nodes(graph_id)`。先例：`DaemonActorRuntime._interruption_reason`（`pac/actors/daemon.py:129`）。不依赖 `app._pac_graph_authority`（它在 compose 之后才构造，W10） | composition 内 `PacLifecyclePort` |

其余事实的来源：

| 事实 | 来源 |
| --- | --- |
| inbox ledger 端口 | `app._inbox`（`DeliveryCustodyFacade`，`wiring/transport.py:972` 赋值）上 WP2-B1 已交付的 `link_ledger_snapshot`、`link_task_bindings`、`link_held_outbox`、`link_inbound_live`、`link_inbound_row`、`update_link_ledger` |
| LedgerSink 绑定 | 不需要调用。inbox 从唯一的槽 `link_admission.gate` 取 sink（wp2b `inbox/service/link_ledger.py:95`，`isinstance(gate, LinkLedgerSink)`）。wp2.md §2.3 的 `bind_link_ledger_sink` 一步作废 |
| 内部 actor 名 | `app.link_admission.internal_actors`，已经是名字集合（`RESERVED_AGENT_ACTOR_NAMES`），原样传入。wp2.md §2.3 的「对 URI 取 actor 段」一步作废 |
| 消息 TTL | `graph.hold_policy.ttl_ms`（`_StartupGraph.hold_policy`）；缺省 `DEFAULT_HOLD_TTL_MS` |
| 本节点 agent | `app.agents.local_actors()`（`composition/views.py:169`）并 `app._registered_interactive_actors()`（会话可无 registry 行注册，见 README WP4 第三次验收记录），过滤为本节点、去掉保留名 |
| 时钟 | `lambda: time.time_ns() // 1_000_000` |
| 日志 | `app._log`，domain 一律 `"daemon"`（与引擎、WP4 一致） |

`compose_link_service` 的唯一调用点：`wiring/services.py::_startup_adopt_domain_services` 开头，+3 行。关停：`shutdown.py::_close` 在 PAC graph authority 关闭之后，+4 行。`application/__init__.py` 只删不加（−28 行）。`wiring/transport.py` 0 行。

## 2. 端口到适配器

全部适配器放在新文件 `src/hyprial/daemon/impl/composition/links.py`，预算 ≤400 行（硬上限 1000）。`composition/` 现有 6 个文件，加后 7 个，⛔ 不再加。`composition/__init__.py` ⛔ 不导入 `links.py`（避免启动时提前导入 PAC 存储与 links 引擎），调用方一律 `from hyprial.daemon.impl.composition.links import ...`，且放在函数体内。

| 引擎端口（`links/store.py`） | 适配器类 | 来源对象 | 测试替身 |
| --- | --- | --- | --- |
| `OrgFs`（`LinkStore(fs, ...)`） | 无适配，直接传 | `app._require_orgfs_runtime().facade` | `tests/support/links/org.py::FakeOrgFs`；需要 `read_text` 时用 §10 的 `OrgFsWithMeta` |
| `spaces: Callable[[], tuple[str, ...]]` | `OrgLinkSpaces` | fs、`app.hyprial_home`、`app._log`、时钟 | 同上 + tmp home 下的 `state/tailcat/left-orgs.json` |
| `author_of`、`is_local_agent`、`local_agents` | `LocalIdentity` | `app.owner`、`app.node_id`、`app.agents`、`app._registered_interactive_actors` | 直接构造 `LocalIdentity(owner, node_id, local=lambda: (...))` |
| `LinkInboxPort` | `FacadeLinkInboxPort` | `app._inbox` | `tests/support/links/ledger.py::FakeInboxPort`（实现的就是引擎端口，可绕过适配器直接注入） |
| `LinkPresencePort` | `PresencePort` | `app._presence` | `FakePresence` |
| `LinkPacPort` | `PacLifecyclePort` | `app.state_dir` | `FakePacPort`；适配器自身用注入的 `open_store` 测 |
| `HookEventPublisher` | 无适配 | `app._hook_bus` | `RecordingPublisher` |
| bus 唤醒 lane | `_LinkHookLane` | `app._daemon_hooks` | 真 `HookBus` + `DaemonHookService` 或记录 `register_consumer` 的桩 |
| PAC binder | `pac_task_binder(app, service)` 返回的闭包 | `app._resolve_agent_alias` | `make_service` 的真 service |
| top 读取 | `link_top_fields(app, actor_statuses)` | `app._link_service` | 真 service 或 None |

各适配器的要求（实施逐条照做）：

1. `LocalIdentity`
   - `bare_owner = app.owner.removeprefix("user:")`。
   - `is_local(uri)`：`parse_agent_uri(uri)` 非 None、`parsed[1] == app.node_id`，且 URI 属于 `app.agents.local_actors()`（registry 权威，hosted 也算本地）；owner 不参与。
   - `author_of(uri)`：解析失败返回 None；同一 node 的 actor 返回本节点 owner（hosted 文件由本 daemon 写，author 不随 URI 里的访客 owner 变），其他 node 返回 `canonical_user_uri(parsed[0])`。它必须等于 orgfs runtime 的 author（`orgfs_bridge.py:179`），由测试 A5 钉住。
   - `local_agents()`：`sorted({*app.agents.local_actors(), *app._registered_interactive_actors()})`，只保留 `is_local` 为真、且 actor 段不在 `internal_actors` 内的 URI。读失败返回 `()` 并记 `link.local_agents_failed`（每分钟至多一次）。
2. `FacadeLinkInboxPort(facade, *, local_agents)`
   - `ledger_snapshot(now_ms)`：`LinkLedgerRow` 逐字段映射为 `LedgerRow`。
   - `task_bindings()`：`facade.link_task_bindings()` 原样返回。
   - `held_outbox()`：`OutboxItem` 映射为 `HeldRow`。`message_id`、`sender`、`target=message.recipient`、`intent` 取 `message` 上的字段；`hold_reason=item.link_hold`；`created_at_ms=message.created_at_ms`；`expires_at_ms=item.expires_at_ms`；`reply_to` 从 `message.payload` 的 JSON 取 `replyTo`（非空字符串才算），解析失败为 None。⛔ 不 import inbox 的私有 `_link_reply_to`，在适配器里写 6 行同义函数。
   - `inbound_live(actor, peer)`：`LinkInboundRow` 映射为 `InboundRow`，丢掉 `state`、`expires_at_ms`。
   - `inbound_row(message_id)`：引擎只给 id，facade 要 `(actor, id)`。对 `local_agents()` 逐个调 `facade.link_inbound_row(actor, message_id)`；恰好一个命中时返回它；零个返回 None；多个返回 None 并记 `link.inbound_row_ambiguous`。见 §11 G1。
   - `apply(update, now_ms)`：构造 `UpdateLinkLedgerCommand(correlation_id=f"link:apply:{uuid4().hex}", now_ms=now_ms, ...)`，字段一一对应。`censor` 里每个 `CensoredRecord` 映射为 `LinkLedgerCensor(actor, message_id, threshold_ms, expires_at_ms)`；`open_outbound` 里每个 `LedgerRow` 映射为 `LinkLedgerOpen(actor, message_id, peer, intent, reply_to, state, opened_at_ms, expires_at_ms)`。调 `facade.update_link_ledger(command)`，把 `LinkLedgerResult` 的五个元组原样装进引擎的 `LedgerResult`。异常不吞，交给引擎（引擎记 `link.apply_failed` 并整轮重试）。
3. `PresencePort(app)`：`online_agents()` 每次读 `getattr(app, "_presence", None)`，None 时返回 `()`；否则返回 `raw_online_actors()` 中 `uri_scheme(x) == "agent"` 的项。⛔ 不调 `online_actors()`：后者对每个 actor 走 interactive liveness 判定，在请求快照之外会退化为每 actor 一次 supervisor 往返（`views.py:524` 的注释），每秒调一次不可接受。见 §4。
4. `PacLifecyclePort(state_dir, *, open_store=None)`：见 §6。
5. `_LinkHookLane(service)`：`observe(event)` 在 `_target` 非 None 时调 `_target.observe_hook(event)`；`detach()` 把 `_target` 置 None。见 §5。

## 3. org 空间的解析规则

`OrgLinkSpaces.__call__() -> tuple[str, ...]`，返回放 `link/` 的 space id，升序：

1. `orgs = OrgFsDirectoryStore(fs, logger=...).orgs()`（`org_from_space_name` 能解析的空间名去重排序）。
2. 去掉 `read_left_orgs(home)` 中的 org。
3. 对剩余每个 org 调 `resolve_org_directory_space(fs, org)`：
   - 返回 `(space, None)`：候选。
   - `missing-directory-space`：跳过。
   - `ambiguous-directory-space`：跳过，记 `link.space_ambiguous`（每个 org 每次状态变化记一次）。⛔ 不按顺序挑一个：同名空间没有权威（`binding.py` 的原则）。
4. 对候选调 `OrgFsDirectoryStore.org_state_details(org)`。生命周期读取先检查显式 marker，再在 marker 缺失或损坏时读取 ACL metadata 作 legacy 分类。仅 `active` 与没有 data problem 的 `legacy-active` 可用；`creating`、`partial`、`deleted`、未知状态和损坏 marker 都跳过。读取异常也跳过，不回退 active。
5. 每个生命周期跳过项记稳定事件 `link.space_skipped`；`reason` 来自 contract 的 `orgSpaceSkipReasons`，只附 `org`、`errorType` 或 `dataReason`，不记录异常文本。同一 org 和原因不变时不重复记录。
6. 结果按 `SPACE_REFRESH_MS = 5_000` 缓存（用 composition 的时钟）。引擎每个 tick 调一次，缓存把 `spaces()` 与 org meta 读取压到每 5 秒一次。
7. 结果集合变化时记 `link.spaces_changed`（`count`、`spaceIds`，最多列 8 个，以及 `skippedOrgSpaces`）。从非空变为空时另记 `link.org_unavailable`，从空变为非空时记 `link.org_available`。

只用 directory 空间，不用 ACL 空间：ACL 空间是 owner 独写的策略空间；directory 空间是成员都能写自己路径的空间，也是 `resolve_org_directory_space` 的唯一权威解析。

三种情形：

| 情形 | 行为 |
| --- | --- |
| 未加入任何 org | service 照常组装并绑定。`spaces()` 为空，`index.org_available` 为 False。受管新请求按 wp2.md §3.4 在发送方 hold（`LINK_SELF_FULL`，detail `org-unavailable`），接收方 `sender_known` 对未观测的发送者为 False。owner、`user:`、`route:`、自发、内部 actor 不受影响。⛔ 不记 `link.service_skipped`。加入 org 后，最迟 `SPACE_REFRESH_MS` 加 1 个 tick（约 6 秒）被下一次 `refresh()` 发现，引擎补建空文件、重评 hold |
| 恰好一个 org | 常态。一份 `link/<actor>.json` |
| 多个 org | 裁定 50 待 Allen 裁，按选项 a：同一完整快照镜像写入每个存活可写空间；读时按最高修订号取完整快照（wp2.md §1.1、§3.10）。适配器不做任何选择，只返回全部空间 |

`link.service_skipped` 只有一个语义：组装时 `app.link_admission.bound` 已为 True（测试预先绑了 `PermissiveLinkGate` 或其他脚本门）。字段 `reason="slot-bound"`、`gateType=type(gate).__name__`。删 E2E 开关后，生产路径上不会出现该事件；出现即说明有代码在 composition 之前绑了门。

`link.service_failed`（`warn`）：组装中途失败，字段 `stage` 为 `orgfs`（`_require_orgfs_runtime` 抛错）、`construct`（构造 store 或 service 抛错）、`start`（`service.start()` 抛错）、`bind`（`bind` 抛 `RuntimeError`/`TypeError`）之一，外加 `errorType`、`detail`（截 300 字符）。失败时槽保持未绑定，`app._link_service`、`app._link_task_binder` 保持 None，不注册 lane，daemon 其余部分继续启动。此时按裁定 54：受管新请求在发送方 hold（detail `gate-unavailable`），接收方不派发。

`link.service_composed`（`info`）：成功时记一次，字段 `spaces`（个数）、`skippedOrgSpaces`（生命周期拒绝数）、`localAgents`（个数）、`orgAvailable`。

## 4. presence 与 N_active

- 来源：`app._presence.raw_online_actors()`。它是本节点身份加 zenoh liveliness 投影的并集（`views.py:61`），纯内存，排好序。agent 的 liveliness key 用规范 URI 声明（`lifecycle_ops.py:210` 的 `keys.actor_liveliness(actor)`，`actor` 为 `_canonical_harness_uri`），所以过滤 `agent` scheme 后就是规范 agent URI；主机 id 被过滤掉。
- 节奏：引擎每个 tick（`TICK_INTERVAL_MS = 1000`）在 `_refresh_seen` 调一次，把在线 agent 记入 `_seen`，N_active 取 24 小时窗口（`ACTIVE_WINDOW_MS`）。适配器不缓存，也不需要。
- 不用 interactive liveness 判定：N_active 是网络规模的估计量，进入控制器的 k 与 W 推导（设计 §5.1）。陈旧的 interactive 会话令牌会让它略偏大，偏差方向是更宽松，不影响安全不变量（W 上限仍由样本决定）。见 §11 G9。
- 关停后 `app._presence` 被置 None（`shutdown.py:673`），适配器返回 `()`；此时 service 已先关闭（§7），不会再调。

## 5. bus 订阅

- 订阅 API：`app._daemon_hooks.register_consumer("links", lane, LINK_WAKE_EVENTS)`。这是 daemon 级消费者入口，与 `reply_reminder_hooks.py:48`、`quota_hooks.py:28` 相同。⛔ 不直接调 `app._hook_bus.register`，⛔ 不 import biz。
- 事件：`LINK_WAKE_EVENTS = ("graph.closed", "worker.cleanup.complete")`，常量放 `composition/links.py`。两者都在 kernel `EVENT_NAMES` 中，且确有发布者：`graph.closed` 在 `pac/storage/store.py:343`，`worker.cleanup.complete` 在 `pac/actors/coordinator/core.py:322`，都经 `HookEventPublisher(self._hook_bus, source="daemon.pac")`。
- ⛔ 不订阅 `actor.up`、`actor.down`：它们在 `EVENT_NAMES` 里，但 daemon 内没有任何发布点（全仓 grep 只在 `events.py`）。PAC 的 `actor_up`/`actor_down` 只是 PAC SQLite journal 的行类型，不上 bus。
- ⛔ 不订阅 `delivery.*`、`turn.*`：样本与计数走边界直喂和 ledger 对账（裁定 46、55）。
- 语义：事件只作唤醒。lane 收到即调 `service.observe_hook(event)`，引擎只做 `kick()`；回收判据在每 tick 的 PAC 核对（§6）。丢事件只造成最多 1 个 tick 的延迟。
- 注册时机：`service.start()` 与 `bind` 都成功之后才注册。启动失败时从未注册，因此 wp2.md W12 的「注销」没有对象。
- 注销：`HookBus` 没有 unregister（`kernel/impl/hooks/bus.py` 只有 `register`、`publish`、`projection`、`close`）。所以 lane 是一个可断开的转发器：`close_link_service` 调 `lane.detach()`，之后 lane 收到的事件全部丢弃；lane 本身随 `hook_bus.close()` 在关停后段退出。wp2.md W11 的「bus 消费者已注销」改读为「lane 已 detach，且之后发布的事件不再到达 service」。见 §11 G3。
- 发布：`link.minted`、`link.reclaimed` 由引擎经 `publisher.publish(...)` 发出，`source="daemon.links"`。`app._hook_bus` 为 None（单元组装）时 publisher 传 None。

## 6. PAC 生命周期快照

引擎在每个 tick 的 `_pac_lifecycle` 中，只在存在任务绑定时调用 PAC 端口（无绑定时零开销）。

- task id 的形状：PAC 通知的 `conversation_id` 恒为 `f"pac-{graph_id}"`（`pac/graphs/reactor.py:487`、`:958`，`pac/graphs/authority/__init__.py:900`，`pac/workflows/runtime/operations.py:59`）。适配器以 `task_id.removeprefix("pac-")` 得 `graph_id`；不以 `pac-` 开头的 task id 一律 `unknown`。
- `task_states(task_ids)`：开一次只读 store，对每个 id 读 `store.graph(graph_id)`。
  - 行不存在：`unknown`。
  - `closed_at` 非 None：`ended`。
  - 否则：`active`。
  - 打开或读取抛任何异常（库不存在、锁、迁移中）：全部 `unknown`，记 `link.pac_unreadable`（每分钟至多一次）。
- `worker_active(task_id, worker)`：开一次只读 store。
  - graph 不存在或读失败：None。
  - graph 已关闭：False。
  - graph 存活：在 `store.nodes(graph_id)` 中找 `kind == "actor"` 且 `actor_name == parse_agent_uri(worker)[2]` 的节点。找到为 True，找不到为 False。
- 连接：每次调用开、用完 `close()`。⛔ 不跨调用持有连接：第一次调用发生在启动线程（`start()` 内同步 tick），之后在 `link-service` 线程，sqlite3 连接不能跨线程。
- `open_store` 参数缺省为 `lambda: PacGraphStore(default_database_path(state_dir), read_only=True)`，测试注入假 store。
- 节点级结束（`actor_down` journal 行）v1 不读：一个 worker 的 link 在 graph 关闭时才回收。见 §11 G8。
- PAC binder（铸造侧，wp2.md §3.7）：`pac_task_binder(app, service)` 返回 `binder(*, sender, recipient, conversation_id) -> Callable[[str], None] | None`：
  1. `conversation_id` 不以 `pac-` 开头：返回 None。
  2. 两端各用 `app._resolve_agent_alias(normalize_agent_recipient(x))` 规范化（与 `InboxDeliveryIoAdapter` 的 `resolve_target` 相同，`wiring/transport.py:595`）；任一端规范化失败或不是 `agent:` URI：返回 None。
  3. 两端中有一端是本节点 owner 的 `user:`：返回 None（owner 对不铸造）。
  4. 调 `service.bind_task(principal=sender, worker=recipient, task_id=conversation_id, now_ms=clock())`，把返回的回调包一层 try/except 后返回。
  5. 以上任何一步抛错：记 `link.pac_bind_failed`，返回 None。binder 与回调都 ⛔ 不抛异常进 PAC 投递路径。

## 7. 关停顺序

插入点：`shutdown.py::_close` 中 `_pac_graph_authority` 关闭块（当前 519–525 行）之后、`_transfer_source` 之前。

理由，按依赖逐条：

1. 在 PAC actor service 与 graph authority 之后：二者关闭前仍可能调 `DaemonPacNotificationSender.send`，进而调 binder。service 先关会让最后几次投递的在途登记丢在内存里。
2. 在 inbox（`shutdown.py:615`）之前：tick 调 `inbox.apply`，要求 inbox actor 仍在。
3. 在 hook bus（`:575`）之前：tick 可能发布 `link.*` 事件；lane 先 detach，再由 bus 关闭收尾。
4. 在 orgfs runtime（`:644`）之前：`store.flush()` 写 orgfs；`store.close()` 关 watch。
5. 在 `_presence` 置 None（`:673`）之前：自然满足。

`close_link_service(app)`（`composition/links.py`）：

```python
def close_link_service(app: Any) -> None:
    service = getattr(app, "_link_service", None)
    lane = getattr(app, "_link_hook_lane", None)
    app._link_service = None
    app._link_task_binder = None
    app._link_hook_lane = None
    if lane is not None:
        lane.detach()
    if service is not None and not service.close(LINK_SERVICE_CLOSE_TIMEOUT_S):
        raise RuntimeError("link service did not stop before deadline")
```

`LINK_SERVICE_CLOSE_TIMEOUT_S = 5.0`。gate 仍留在 `link_admission` 槽里（槽没有 unbind），关停期间到达的 IPC 照旧由它从内存作答，不做 I/O。见 §11 G4、G5。

## 8. 逐文件改动

行数为 `aab662594` 实测。

### 8.1 `src/hyprial/daemon/impl/composition/links.py`（新建，≤400）

公共名：`compose_link_service`、`close_link_service`、`link_top_fields`、`pac_task_binder`、`OrgLinkSpaces`、`LocalIdentity`、`FacadeLinkInboxPort`、`PresencePort`、`PacLifecyclePort`、`LINK_WAKE_EVENTS`、`SPACE_REFRESH_MS`、`LINK_SERVICE_CLOSE_TIMEOUT_S`。

```python
def compose_link_service(
    app: Any, graph: Any, *, fs: Any = None, inbox_port: Any = None,
    clock_ms: Callable[[], int] | None = None, seed: int | None = None,
) -> LinkService | None:
```

步骤（每步失败按 §3 记 `link.service_failed` 并返回 None，已建对象先 `close()`）：

1. `app.link_admission.bound` 为 True：记 `link.service_skipped`，返回 None。
2. `fs = fs or app._require_orgfs_runtime().facade`（stage `orgfs`）。
3. 构造 `LocalIdentity`、`OrgLinkSpaces`；用一个单元素列表做前向引用，`LinkStore(fs, spaces=..., author_of=..., is_local_agent=..., on_change=lambda: cell[0] and cell[0].kick(), logger=app._log)`。
4. `LinkService(store=..., inbox=inbox_port or _inbox_port_for(app, identity), presence=PresencePort(app), pac=PacLifecyclePort(app.state_dir), is_local_agent=identity.is_local, local_agents=identity.local_agents, internal_actor_names=app.link_admission.internal_actors, publisher=..., clock_ms=clock, message_ttl_ms=getattr(getattr(graph, "hold_policy", None), "ttl_ms", DEFAULT_HOLD_TTL_MS), seed=seed, logger=app._log)`，然后 `cell[0] = service`（stage `construct`）。
5. `service.start()`（stage `start`）。
6. `app.link_admission.bind(service)`（stage `bind`）。
7. `app._link_service = service`；`app._link_task_binder = pac_task_binder(app, service)`。
8. `daemon_hooks = getattr(app, "_daemon_hooks", None)`，非 None 时注册 lane 并设 `app._link_hook_lane`；注册抛错只记 `link.lane_failed`（`warn`），不回滚已绑定的 service（tick 核对兜底）。
9. 记 `link.service_composed`，返回 service。

`_inbox_port_for(app, identity)` 是模块级函数：`FacadeLinkInboxPort(app._inbox, local_agents=identity.local_agents)`。它是测试的 monkeypatch 点（§10 C1）。

`link_top_fields(app, actor_statuses) -> dict[str, Mapping[str, int | None]]`：`service = getattr(app, "_link_service", None)`；对每个 status 按 `views.py::_inbound_awaiting_injection` 的同一表达式得规范 actor（`status["actor"]` 是字符串时用它，否则 `canonical_agent_uri(app.owner, app.node_id, str(status.get("name")))`）；service 为 None 时值为两个键都为 None 的映射，否则 `service.top_fields(actor)`。不改状态，不调 gate。

### 8.2 `application/wiring/services.py`（475 → 478，+3）

在 `_startup_adopt_domain_services` 的 import 块之后、`pac_hook_publisher` 之前插入：

```python
        from hyprial.daemon.impl.composition.links import compose_link_service

        compose_link_service(self, graph)  # WP2: before PAC (W10); never raises
```

`compose_link_service` 内部捕获全部异常，所以这里不加 try。

### 8.3 `application/shutdown.py`（904 → 908，+4）

在 `_pac_graph_authority` 块之后插入：

```python
        if getattr(self, "_link_service", None) is not None:
            from hyprial.daemon.impl.composition.links import close_link_service

            attempt(lambda: close_link_service(self), "link-service")
```

### 8.4 `operations/top.py`（510 → 约 519，+9）

- 第 31 行的 import 扩为三个常量：`TOP_FIELD_CAPACITY_W`、`TOP_FIELD_INBOUND_AWAITING_INJECTION`、`TOP_FIELD_OUTBOUND_IN_FLIGHT`。
- `build_actor_row` 加参数 `link_fields: dict[str, int | None] | None = None`；row 字典在 `TOP_FIELD_INBOUND_AWAITING_INJECTION` 之后加两项：`TOP_FIELD_CAPACITY_W: (link_fields or {}).get(TOP_FIELD_CAPACITY_W)` 与 `TOP_FIELD_OUTBOUND_IN_FLIGHT: (link_fields or {}).get(TOP_FIELD_OUTBOUND_IN_FLIGHT)`。缺省两值为 None（WP6 渲染为 `—`）。
- `build_top_snapshot` 加参数 `link_fields: dict[str, dict[str, int | None]] | None = None`，调 `build_actor_row` 时传 `link_fields=(link_fields or {}).get(status_actor)`。

### 8.5 `application/messaging/status/views.py`（934 → 937，+3）

`_ipc_top_snapshot` 中 `build_top_snapshot(...)` 调用前加函数内 import，调用里加一个关键字参数：

```python
        from hyprial.daemon.impl.composition.links import link_top_fields
        ...
            link_fields=link_top_fields(self, actor_statuses),
```

### 8.6 `pac/actors/daemon.py`（888 → 896，+8）

`DaemonPacNotificationSender.send`，在 `delivery_io.deliver(...)` 之前与之后：

```python
        binder = getattr(self.application, "_link_task_binder", None)
        on_delivered = (
            binder(sender=sender, recipient=recipient, conversation_id=conversation_id)
            if binder is not None else None
        )
        delivered = delivery_io.deliver(...)          # unchanged
        if on_delivered is not None:
            on_delivered(str(delivered.message_id))
```

投递抛错时不调回调（没有成功回执就不计在途，wp2.md §3.7）。

### 8.7 `application/__init__.py`（990 → 962，−28）

唯一允许的改动（wp2.md §9.6）：

- 删第 141–142 行导入里的 `E2E_PERMISSIVE_GATE_EVENT,` 与 `E2E_PERMISSIVE_LINK_GATE_ENV,`，保留 `LinkAdmission`。
- 删第 770–795 行整个 `if os.environ.get(E2E_PERMISSIVE_LINK_GATE_ENV) == "1":` 分支（到 `except` 块的最后一个 `)` 为止；796 行 `if keep_migration.migrated:` 保留）。
- `os` 仍被第 472 行使用，不删 `import os`。
- 机检：`git diff` 中该文件只有删除行。

### 8.8 连带删除

- `dispatch/admission.py`（421 → 412，−9）：删第 113 行 `E2E_PERMISSIVE_GATE_EVENT`、第 114–119 行注释与 `E2E_PERMISSIVE_LINK_GATE_ENV`，删 `__all__` 中第 412–413 行两项。
- `tests/support/links/gate.py`（217 → 174，−43）：删第 108–150 行 `E2EPermissiveLinkGate` 类。`ScriptedLinkGate`、`PermissiveLinkGate`、`BudgetedLinkGate`、`permissive_admission_slot` 保留（§9、§11 G6）。
- `tests/daemon/channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py`：删环境变量与它上面的三行注释，按 §9 改。
- 删后 `git grep -n "E2E_PERMISSIVE\|E2EPermissiveLinkGate\|HYPRIAL_E2E_PERMISSIVE_LINK_GATE" -- src tests scripts contract` 必须为空（`docs/` 历史记录不改）。

### 8.9 不改

`wiring/transport.py`（997，diff 必须为空）、`links/` 下全部文件（WP2-A 在拆）、`inbox_surface/queries.py`（WP3-A）、`hooks/service.py`、kernel `daemon_teardown.py`（见 §11 G5）。

## 9. E2E 替身的移除

`wp2b/send-links` 上 grep（`E2EPermissiveLinkGate|HYPRIAL_E2E_PERMISSIVE_LINK_GATE|permissive_admission_slot|PermissiveLinkGate`）共命中 18 个测试文件，与第一次实施列出的约 18 个一致。读取 E2E 环境变量的只有 1 个；其余 17 个用脚本门，不经 E2E 开关。

处理只有两种：(a) 建真 link；(b) 保留脚本门，经 `app.link_admission.bind(...)`（有 app 时，必须在 `_start_runtime` 之前）或 `LinkAdmission().bind(...)`（只有 coordinator 时）绑定。⛔ 不加 autouse fixture，不加目录级 conftest。

| # | 文件 | 现状 | 处理 |
| --- | --- | --- | --- |
| 1 | `tests/daemon/channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py` | 子进程 daemon，设 `HYPRIAL_E2E_PERMISSIVE_LINK_GATE=1` | (a)：删环境变量。换 owner 前经 IPC `org.create`（org 名 `group-links-e2e-a`；org 名必须以 `group-` 开头，见 `org_space_name`）；daemon 以 owner B 重启后再 `org.create`（`group-links-e2e-b`），之后再发 post-switch 消息。现有的轮询等待覆盖铸造延迟（两三个 tick）。若 owner B 下仍 hold，按 §11 G7 处理 |
| 2 | `tests/daemon/delivery/providers/test_provider_failure_settlement.py` | coordinator 级，`permissive_admission_slot()` | (b)，不改 |
| 3 | `tests/daemon/harnesses/actors/test_harness_reply_settlement.py` | 同上 | (b)，不改 |
| 4 | `tests/daemon/inbox/admission/test_inbox_dispatch_ttl_refresh.py` | 同上（4 处） | (b)，不改 |
| 5 | `tests/daemon/inbox/authority/full_writer/support.py` | 同上 | (b)，不改 |
| 6 | `tests/daemon/links/admission/test_ingress_link_unknown.py` | 同上 | (b)，不改 |
| 7 | `tests/daemon/progress/test_progress_end_to_end.py` | 同上；另有一处 `_start_runtime` 探针 | (b)，不改。探针的假 inbox 没有 ledger 方法，compose 会在 `start` 失败并记 `link.service_failed`，启动照常完成；该测试不断言槽状态，不受影响 |
| 8 | `tests/identity/providers/lifecycle/availability/test_receiver_drill.py` | 同上 | (b)，不改 |
| 9 | `tests/integration/actor_preservation/delivery/test_actor_preserved_provider_failure_settlement.py` | 同上 | (b)，不改 |
| 10 | `tests/daemon/inbox/idempotency/test_agent_send_idempotency.py` | app 级，构造后立即 `bind(PermissiveLinkGate())`，未调 `_start_runtime` | (b)，不改 |
| 11 | `tests/daemon/transport/first/test_local_first_self_node_delivery.py` | 同上 | (b)，不改 |
| 12 | `tests/kernel/actors/identity/support.py` | 同上 | (b)，不改 |
| 13 | `tests/shell/cli/communication/test_cli_query.py` | 同上 | (b)，不改 |
| 14 | `tests/daemon/delivery/message/test_authenticated_message_sender.py` | app 级 `bind(gate)` | (b)，不改 |
| 15 | `tests/daemon/delivery/message/test_message_pending_wait.py` | slot 级 `bind(PermissiveLinkGate())` | (b)，不改 |
| 16 | `tests/daemon/delivery/origin/test_inbound_origin.py` | app 级 `bind` | (b)，不改 |
| 17 | `tests/daemon/links/admission/test_link_admission_slot.py` | slot 级；另有 T17 探针跑 `_start_runtime` | (b)，不改。T17 只断言同一个槽对象，compose 失败不影响；C1 复用该探针并让 compose 成功 |
| 18 | `tests/daemon/links/admission/test_pending_list_link_filter.py` | app/slot 级 `bind(gate)` | (b)，不改 |

另有 `tests/daemon/links/admission/test_top_inbound_awaiting_injection.py` 在 app 上 `bind(gate)`，同属 (b)。

实施第一步（沿用 wp2.md §4.9）：只提交接线（§8.1–§8.6）后跑一次全量，把**新**失败的测试逐个列进证据文件，并逐个注明 (a) 或 (b)。预期新失败只来自既不绑门、又依赖 agent 间派发、且走真 `_start_runtime` 与真 inbox 的测试；它们在未加入 org 的 tmp home 中会 hold `org-unavailable`。修法：
- 测的是 link 之外的行为：(b)，在 `_start_runtime` 之前 `app.link_admission.bind(PermissiveLinkGate())`。
- 测的是真实派发链路：(a)，`tests/support/links/app.py::join_test_org(app, "group-links-test")`（§10），让真 service 自己铸造。

## 10. 测试

### 10.1 位置与文件数

- `tests/daemon/links/service/`：WP2-A 合入后有 `__init__`、`test_gate`、`test_hold_release`、`test_reclaim`、`test_samples`；WP2-B1 加了 `test_ledger`，共 6 个；B2 若加 `test_wiring.py` 则为 7 个，已到上限。B3 ⛔ 不往这里加文件。
- B3 的测试放新目录 `tests/daemon/links/composition/`（4 个文件）：`__init__.py`、`test_compose.py`、`test_adapters.py`、`test_restart.py`。`tests/daemon/links/` 的直接文件数不变（子目录不计）。
- 测试助手 `tests/support/links/app.py`（新，该目录由 4 个文件变 5 个）：
  - `OrgFsWithMeta(FakeOrgFs)`：补 `read_text(space, path) -> (text, version)`，供 `org_state` 使用。
  - `join_test_org(app, org)`：`app._require_orgfs_runtime().facade.create_space(org_space_name(org))`，再 `OrgFsDirectoryStore(fs).put_org_meta(org, {"state": "active"})`。
  - `stub_app(tmp_path, **overrides)`：一个 `SimpleNamespace`，带 `link_admission=LinkAdmission()`、`owner`、`node_id`、`hyprial_home`、`state_dir`、`agents`（`local_actors()`）、`_registered_interactive_actors`、`_presence`、`_hook_bus`、`_daemon_hooks`、`_inbox`、`_log`（`LogRecorder`）、`_resolve_agent_alias`、`_require_orgfs_runtime`。
  - ⛔ 不改 `tests/support/links/org.py`（WP2-A 并行在用）。

### 10.2 `test_compose.py`（W-composition）

先红形状：`composition/links.py` 尚不存在时，模块级 `from hyprial.daemon.impl.composition.links import ...` 即 `ModuleNotFoundError`。每条另有下列语义先红（在只有空函数体的 links.py 上跑）。

| 编号 | 测什么 | 先红 |
| --- | --- | --- |
| C1（W1） | 复用 `test_link_admission_slot.py` T17 的探针跑 `_start_runtime`，另 monkeypatch `composition.links._inbox_port_for` 返回 `FakeInboxPort()`。断言：`app.link_admission.bound`；`app.link_admission.gate is app._link_service`；`isinstance(app._link_service, LinkService)`；`callable(app._link_task_binder)`；日志含 `link.service_composed` 且 `orgAvailable is False`（tmp home 无 org）；`app._hook_bus.projection()` 中有名为 `links` 的 lane | `bound is False` |
| C2（W2） | 同探针，`_start_runtime` 之前 `app.link_admission.bind(PermissiveLinkGate())`。断言：不抛 `RuntimeError`；`link.service_skipped` 一条，`reason == "slot-bound"`；`getattr(app, "_link_service", None) is None`；无 `links` lane | `RuntimeError: link gate is already bound` |
| C3（W12） | `stub_app` + `inbox_port` 的 `ledger_snapshot` 抛 `RuntimeError`。断言：返回 None；`link.service_failed` 的 `stage == "start"`；槽未绑定；`register_consumer` 未被调用；store 的 watch 已关（FakeOrgFs 的注册全部 closed） | 槽被绑定，或 lane 泄漏 |
| C4 | `_require_orgfs_runtime` 抛错 → `stage == "orgfs"`，槽未绑定，不抛出 | 异常冒出 compose |
| C5（W10） | 探针下 monkeypatch `PacGraphAuthority.__init__` 记录构造时的 `getattr(app, "_link_service", None)` → 非 None | 记录到 None |
| C6（W9） | `stub_app`，真 `HookBus` 与 `DaemonHookService`，`FakeOrgFs` 带一个 org 空间，两个本节点 agent。另注册测试 lane 订阅 `link.minted`、`link.reclaimed`。经 `service.admit_send` 发起铸造并推进 tick 至两份互引已提交 → 收到 1 条 `link.minted`，`source == "daemon.links"`；`service.drop(...)` 后收到 1 条 `link.reclaimed`；payload 键等于 wp2.md §2.7 | 未收到 |
| C7（W11） | 组装成功后调 `close_link_service(app)`：`service._thread` 不再存活；FakeOrgFs 注册全部 closed；`app._link_service`、`_link_task_binder`、`_link_hook_lane` 为 None；之后向 bus 发布 `graph.closed`，service 的 `kick` 计数不变（lane 已 detach）；再调一次不抛 | 线程存活或仍被唤醒 |
| C8 | 关停顺序：探针 app 上 monkeypatch `close_link_service`、inbox 的 `shutdown`、`hook_bus.close`、orgfs runtime 的 `close` 记录调用序，跑 `_close()` → link 在 PAC 之后、在其余三者之前 | 顺序不符 |
| C9（W8） | `build_top_snapshot(..., link_fields={A: {capacityW: 3, outboundInFlight: 1}})` → A 行两键为 3、1，其他行两键为 None；`link_top_fields(stub_app_without_service, statuses)` → 全 None；有 service 时 `capacityW == service.stats(A)["W"]`；连续两次调用后 service 的 gate 调用记录不变 | 缺键 |
| C10（W10 续） | `DaemonPacNotificationSender(app).send(...)`，`conversation_id="pac-g1"`，app 上有记录型 binder 与记录型 `_pac_notification_io`：调用序为 binder → deliver → 回调，回调参数为 deliver 返回的 message id；deliver 抛错时回调未被调用；无 binder 属性时行为与改前相同 | 回调早于投递，或投递失败仍回调 |

### 10.3 `test_adapters.py`

| 编号 | 测什么 | 先红 |
| --- | --- | --- |
| A1 | `OrgLinkSpaces`：`OrgFsWithMeta` 上建 `group-a`、`group-b`、`group-left`、`group-gone`（meta `state: deleted`）、两个同名的 `group-dup`、一个 `-acl` 空间、一个普通空间；tmp home 的 left-orgs 写 `["group-left"]` → 返回 `group-a`、`group-b` 的 space id 升序；`link.space_ambiguous` 记 1 次；5 秒内第二次调用不触发 `spaces()`（计数），推进时钟 5 秒后触发；从有到无记 `link.org_unavailable` | 返回含 group-left、group-gone 或 group-dup |
| A2 | `PresencePort`：`_presence` 为 None → `()`；`raw_online_actors()` 返回主机 id、agent URI、`user:` → 只留 agent；`online_actors` 被调用即失败（桩抛错） | 调了 `online_actors` |
| A3 | `FacadeLinkInboxPort`：记录型 facade。`held_outbox` 从 payload 取 `replyTo`，非法 JSON 为 None；`apply` 产生的命令字段逐一相等（含 censor、open_outbound 映射）；结果映射回 `LedgerResult`；`inbound_row` 零命中 None、单命中返回、双命中 None 并记 `link.inbound_row_ambiguous` | 字段错位 |
| A4 | `PacLifecyclePort`：注入假 store。不存在的 graph → `unknown`；`closed_at` 非空 → `ended`；存活 → `active`；非 `pac-` id → `unknown`；`open_store` 抛错 → 全 `unknown` 且记一次日志；`worker_active` 按 actor 节点名判 True/False，graph 关闭为 False，读错为 None；每次调用后 store 已 close | 连接泄漏或状态错 |
| A5 | `LocalIdentity`：本节点 agent 为真，同 owner 他节点、他 owner 本节点、`user:` 为假；`author_of` 等于 `OrgFsRuntime` 对同一 owner 计算的 author（`owner` 带与不带 `user:` 两种）；`local_agents` 合并 registry 与 interactive、去重、去掉 `dispatch-coordinator`、`_turn-hooks` | 含保留名或他节点 |
| A6 | `pac_task_binder`：`pac-g1` 两端本节点 agent → `bind_task` 收到规范 URI；`wf-x` → None；一端 owner `user:` → None；`_resolve_agent_alias` 抛错 → None 且记 `link.pac_bind_failed`；回调内部抛错不外泄 | 异常外泄 |

### 10.4 `test_restart.py`（L14）

wp2.md §4.6 把 L14 放在 `test_ledger.py`；它需要 composition 的 `FacadeLinkInboxPort`，属接线测试，改放这里，`test_ledger.py` 保持 inbox 层。

- 组装：照 `tests/daemon/inbox/authority/full_writer/support.py` 起真 `DeliveryCustodyCoordinator` 与 `DeliveryCustodyFacade`（tmp SQLite），`service_options["link_admission"]` 用一个新 `LinkAdmission()`；`make_service(fs=FakeOrgFs(...), inbox=FacadeLinkInboxPort(facade, local_agents=...))` 之后 `slot.bind(service)`。
- 四个中断点，每个一条参数化用例：submit 后、`submitted` 前；`admit_dispatch` 准入后、tick 前；清 hold 后、内存合并前（`apply` 返回后丢弃 service）；排干 ack 后、内存合并前。中断方式：丢弃 service 对象，不调 `close()`（线程是 daemon 线程，用 `tick()` 驱动，不 `start()` 线程，或 `start()` 后以 `_stop.set()` 停线程但不调 `close`）。
- 以同一库重建 service（新 `LinkAdmission`）：出站在途数等于中断前；已准入 id 再问 `admit_dispatch` 仍为 True 且不多占名额；对已排干 id 的回复得 `LINK_RECLAIMED`。
- 先红：在 `FacadeLinkInboxPort.ledger_snapshot` 返回 `()` 的实现上，第一与第二个中断点的在途数为 0。

### 10.5 不变的守卫

- `tests/daemon/links/test_wp0_seams.py` 的 `links/__init__.py` 只导入 `ports`：不受影响。
- wp2.md S-B1（AST）：再加一条 `composition/links.py` 不 import `biz`、`shell`；`links/` 下无文件 import `composition`。放 `test_adapters.py` 末尾。
- `tests/tooling/isolation/test_teardown_guarantee.py`：`_close` 新增的 `attempt(..., "link-service")` 不含 `drain`/`drained`/`shutdown` 字面量调用，扫描结果不变；见 §11 G5。
- 焦点命令：`uv run --with libcst==1.9.0 pytest tests/daemon/links tests/daemon/delivery tests/daemon/progress tests/daemon/channels/mcp/lifecycle tests/tooling/isolation tests/shell/cli/lifecycle/test_cli_top.py -q`；之后全量；结构门与模块边界两条脚本；`uv run ruff check .`。

## 11. 需要集成者裁定的缺口

只列，不定；每条给缺省，裁定前按缺省实施。

- **G1 `inbound_row` 的签名不对齐。** 引擎端口 `inbound_row(message_id)`，facade 是 `link_inbound_row(actor, message_id)`。缺省：适配器遍历本节点 agent，唯一命中才返回，多命中返回 None 并记日志。代价：同一 message id 投给多个本地 agent 时，回复按「不在册」处理（`MESSAGE_REPLY_UNAVAILABLE`）。根治是 WP2-A 在端口上加 `actor` 参数（引擎调用点已有 actor），WP2-B3 不改 `links/`。
- **G2 跨节点写 `link/` 的 orgfs 准入未验证。** directory 空间的准入（`orgfs/storage/store/admission.py::_device_tree_update_allowed`）对根下非 `directory` 子树的创建按「非保护路径」放行，但非 owner 成员在根下 `mkdir link` 并写 `link/<uri>.json` 在对端是否通过准入、两个节点并发创建 `link` 目录是否产生重名节点，本次没有实测。缺省：照常实施；WP8 的双节点 E2E 必须覆盖，失败时回到 store 设计。
- **G3 bus 没有 unregister。** 缺省：lane 用 detach 代替注销（§5）；wp2.md W11/W12 的表述按本文件改读。备选是给 `HookBus` 加 `unregister`，属 kernel 改动，不在本包。
- **G4 槽没有 unbind。** service 关闭后 gate 仍在槽里，从冻结的内存作答，直到进程退出。缺省：接受（只发生在关停段）。
- **G5 关停预算未登记。** `close_link_service` 最长 5 秒，没有登记进 kernel `DAEMON_CLOSE_STEP_BUDGETS`；先例是 PAC 的 `close(5.0)` 同样未登记。缺省：不改 kernel 表。若要登记，加 `("link-service", 5.0)` 会抬高 `TEARDOWN_BUDGETED_SECONDS` 与重复实例观察窗，属跨线改动。
- **G6 `permissive_admission_slot` 是否删。** wp2.md §9.5 第 4 条写「删 E2EPermissiveLinkGate/permissive_admission_slot」。后者是给 coordinator 级测试绑脚本门的助手，与 E2E 开关无关，删掉需要给 9 个文件各写一份等价的 3 行。缺省：保留，只删 `E2EPermissiveLinkGate`。
- **G7 owner 迁移后的 org 成员身份。** `test_mcp_channel_owner_migration_e2e` 换 owner 后 daemon 的 orgfs author 变为新 owner，旧 owner 建的 org 空间对它可能不可写（`not-a-member`），新 owner 的 agent 发送会 hold `org-unavailable`。缺省：迁移后以新 owner 再 `org.create` 一个 org（§9 第 1 行）。UNKNOWN：`org.create` 在没有 Casdoor 配置的隔离 home 中是否成功（`_casdoor_require_group` 预期为空操作）。若该路径仍红，报 BLOCKED 并附证据，不加回 E2E 开关。生产上「换 owner 之后原 org 的 link 全部失效」本身是一个产品语义问题，需 Allen 知悉。
- **G8 PAC worker 的单节点释放。** v1 只在 graph 关闭时回收 PAC link；节点级 `actor_down` 不读（它只在 PAC journal，不上 bus）。缺省：接受，长 graph 里已结束的 worker 会占着 PAC link 直到 graph 关闭（PAC link 不计入 k，只影响 cwnd 视图）。
- **G9 N_active 用原始 presence。** 不经 interactive liveness 判定（§4），陈旧会话令牌让估计偏大。缺省：接受。
- **G10 裁定 50 仍待 Allen。** 多 org 按选项 a 镜像；适配器只返回全部空间，规则改 b 时只改 `OrgLinkSpaces`（取 owner 所在 org 的 directory 空间）。
- **G11 `LinkChangeSource` 仍无订阅者**（wp2.md §7 S4）。缺省：composition 不订阅，WP4 的 `pending.wait` 继续 5 秒轮询。

UNKNOWN（本文件未实测、实施时须给出 PASS/FAIL/BLOCKED）：G2 的跨节点准入；G7 的 `org.create` 离线可用性；§9 第 1 行改后能否在现有轮询窗口内完成铸造；全量下新增失败测试的确切清单（§9 实施第一步产出）。
