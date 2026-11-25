# G0机械清单、Python并发白名单与SQLite写者附录（草案）

状态：**INVENTORY DRAFT；不是最终BASE冻结版，不得作为S2完成证据**。②–⑩、⑫–⑭规则已获Allen裁定；本清单可用于不等待#1892的G0全部任务包、S1及S2非重叠工作，但最终BASE产生后必须重生成并复核。

- 本草案唯一源码快照为 `INVENTORY_SHA=5b9eb6530beaec8016becfd1793b913b6f247eb4`，分支 `intg/actor-rustify`。行号只定位该SHA。
- 按[决定①](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)，最终 `BASE_SHA` 必须是#1892合入`dev`后的`dev` head。#1892当前仍是Draft，因此 **`BASE_SHA=OPEN`**；这不阻塞上述G0/S1及S2非重叠工作，最终BASE与Allen复核只挡S2.6。
- BASE产生后，必须在干净worktree按本文同一AST规则重生成附录A/B/D及机器JSON，与本草案只做调查对照，不沿用旧计数；逐项diff并交Allen裁定后，才能写入精确BASE、裁定人、裁定日期并冻结。
- 当前所有迁移测试nodeid和证据链接均为 **OPEN**。本文的“去向”是迁移要求草案，不是完成记录或测试PASS。

计数基线：`ActorRuntime=61`，`EffectLane=48`，手写lane/mailbox评审根=16组，`Thread/Queue/Condition=102/36/35`，`Timer=5`，`ThreadPoolExecutor=8`，`asyncio.to_thread=7`，`multiprocessing spawn=1`，`SimpleQueue/PriorityQueue=1/1`，`deque=45`，`create_task/ensure_future=11/0`，`Lock/RLock=215/59`，旧actor协议符号引用文件=78，`sqlite3.connect=40(W=27/R=13)`，`PacGraphStore=57/16文件`。第四轮评审所述`asyncio.to_thread=8`在本SHA不能复现；A4d列出实际7点，禁止为迎合旧计数伪造第8点。

## 附录A：旧引擎与并发站点机械清单

### A0. AST口径与共同追踪字段

A1–A5扫描范围仅为 `src/hyprial/**/*.py`，不扫描tests、docs、dist、构建产物，不import或执行产品模块；D3另按明确文件集扫描CLI与四个contract runner，不扩大为整个`contract/`。用`ast.parse`；构造点是`ast.Call`，其`func`为`Name`或`Attribute`时取末段符号（`Name.id`/`Attribute.attr`），同时解析直接import及简单赋值别名；解析失败或冲突必须保留为`unresolved`并保守失败/人工复核，不得漏成PASS。`Thread`、`Queue`、`Condition`是保守的语法口径，包括直接导入和属性调用，`Queue`也包括`asyncio.Queue`；`SimpleQueue`、`PriorityQueue`独立计数；`asyncio.to_thread`要求解析到该完整符号；`sqlite3.connect`额外要求`Attribute(value=Name("sqlite3"), attr="connect")`。位置为`path:lineno`，路径相对仓库根，先按path再按line排序；白名单身份不使用行号，见附录B。

78文件口径使用`Name`/`Attribute`末段匹配A5列出的17个旧actor协议符号，范围排除`src/hyprial/kernel/`，并排除`daemon/impl/pac/actors/coordinator/core.py`：后者从PAC自己的`coordinator.types`导入`ActorRuntime` Protocol，不是旧kernel runtime。`DaemonActorRuntime`同样不是旧符号。`AdmissionResult`不计入这个78文件评审口径。该聚合只是调查口径；A6最终门必须按模块解析，只有解析为`hyprial.kernel*`旧模块的指定符号才计入，不能要求PAC Protocol或`DaemonActorRuntime`定义为0，最终门仍覆盖`AdmissionResult`及A6全集。

A类所有行共同继承 `test_nodeid=OPEN`、`evidence=OPEN`。最终每一行必须补：实际去向、至少一个真实测试nodeid、精确head下的证据链接；仅删除实现细节测试不够，必须有行为/故障语义替代证据。A类所需StateStore、SqliteResource、File/Blob/Lease、TransportSession、ProcessCustody、Timer、I/O和Operation控制均为**native能力**；纯Python锁例外只能走附录B并须Allen逐项裁定。

机械去向总规则：`ActorRuntime`、`EffectLane`和A3手写lane/mailbox全部删除，不以另一个Python mailbox替换；业务规则留Python应用服务，资源/提交/等待边界迁native。`Thread/Queue/Condition`默认删除或迁native，只有附录B最终BASE白名单的精确站点可保留。AR编号与[61项分层处置建议](actor-rustify-entry-layering-proposal-2026-10-24.md)同编号对应，能力缩写S/D/F/T/P/C/E/O沿用该文档。

### A1. ActorRuntime构造点（61/61）

| ID | inventory SHA构造点 | 去向草案；测试/证据均OPEN |
|---|---|---|
| AR-01 | `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:91` | 删专用actor；Python Routine补偿saga；native D(routine-coordinator.sqlite3)+O，按需S/C |
| AR-02 | `src/hyprial/biz/impl/routine/execution/service.py:181` | 删actor；Python Routine编排；D(routines.sqlite3)+S/C/O |
| AR-03 | `src/hyprial/biz/impl/usage_actor.py:47` | 删usage-cache actor；Python汇总/失效；S+O，刷新可用C |
| AR-04 | `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:309` | Python Lark入口与Inbox投递；S/O；不得绕过Inbox W权威 |
| AR-05 | `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:84` | Python SDK适配；O与明确I/O配额；SDK不进native |
| AR-06 | `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:692` | Python reaction恢复/补偿；D(ReactionEffectStore)+O |
| AR-07 | `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:159` | Python adapter管理服务；S/O，进程P、定时C |
| AR-08 | `src/hyprial/daemon/impl/adapters/lark/worker/control.py:37` | Python Lark控制服务；O与有界Python I/O；删control-io包装 |
| AR-09 | `src/hyprial/daemon/impl/autoupdate/actor.py:83` | Python升级saga；C+O，文件/进程F/P |
| AR-10 | `src/hyprial/daemon/impl/configuration/home_lease_actor.py:115` | Python home策略；F真实OS锁+P/S/C/O |
| AR-11 | `src/hyprial/daemon/impl/correlation/bounded_cadence.py:61` | 收敛共享C+O；Python消费数据事件，不传callable给native |
| AR-12 | `src/hyprial/daemon/impl/dispatch/remote/__init__.py:65` | Python RemoteWorkflow；D(PAC)+S/O，网络T；保留receipt/fence |
| AR-13 | `src/hyprial/daemon/impl/dispatch/runtime/diagnostics.py:43` | 删诊断mailbox/runtime；复用native观测，派生快照留Python |
| AR-14 | `src/hyprial/daemon/impl/dispatch/runtime/holds.py:43` | Python hold刷新；C/S/O，操作仍落Inbox资源边界 |
| AR-15 | `src/hyprial/daemon/impl/dispatch/runtime/offers.py:75` | Python offer编排；S/D/O与资源fence；不建Rust OfferActor |
| AR-16 | `src/hyprial/daemon/impl/dispatch/runtime/progress.py:50` | Python progress归并；S/D/O，网络T；旧代拒绝与先记录后释放 |
| AR-17 | `src/hyprial/daemon/impl/dispatch/runtime/state.py:72` | 通用S批量Snapshot/CAS+O；删DispatchStateAuthority包装 |
| AR-18 | `src/hyprial/daemon/impl/forwarding/authority.py:107` | Python sidecar协议；P/T/O，通用原生I/O可用E |
| AR-19 | `src/hyprial/daemon/impl/forwarding/supervisor.py:96` | Python重启策略；S/P/O；保留真实控制状态/fence |
| AR-20 | `src/hyprial/daemon/impl/harnesses/actor/actor.py:141` | Python Harness生命周期；P+S/D/O/C；保留completion receipt/generation |
| AR-21 | `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:498` | Python carrier协议；P/T+S/D(CodexCarrierStore)+O |
| AR-22 | `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:718` | Python Turn服务；S+P/T/O/C；不建Rust TurnActor |
| AR-23 | `src/hyprial/daemon/impl/inbox/actor/coordinator.py:69` | Python Inbox规则；同一owner内S/D+O/scoped变化；不得拆开W与DB竞争 |
| AR-24 | `src/hyprial/daemon/impl/inbox/tracking/fetch_receipts.py:39` | Python构造receipt；T/O；删独立fetch-receipt-io actor |
| AR-25 | `src/hyprial/daemon/impl/ipc/ipc_request_owner.py:50` | Python IPC鉴权/路由；O，必要S；断连不撤已接纳责任 |
| AR-26 | `src/hyprial/daemon/impl/lifecycle/duplicate_actor.py:19` | 共享C+Python策略；F/P保留真实lease/process fence |
| AR-27 | `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:522` | Python多步saga；D/S/O及各步骤F/P；保留部分提交/补偿 |
| AR-28 | `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:233` | Python维护监测；共享C+观测/O；watchdog不被被测资源饿死 |
| AR-29 | `src/hyprial/daemon/impl/network/routing/session_route_coordinator.py:112` | Python路由服务；S/D/T/O；保留epoch与失效完成 |
| AR-30 | `src/hyprial/daemon/impl/operations/management_actor.py:117` | Python管理saga；S/D/F/P/O；保留reserve/submit/ack责任 |
| AR-31 | `src/hyprial/daemon/impl/operations/watchdog_actor.py:136` | Python看门策略；C/P/F/S/O；保留重启预算和旧代fence |
| AR-32 | `src/hyprial/daemon/impl/org/mesh.py:156` | Python Org组装；S与资源handle，按实际资源D/T/O |
| AR-33 | `src/hyprial/daemon/impl/org/network/access/actor.py:121` | Python访问控制；S/D/F+T/O；权限fence闭合 |
| AR-34 | `src/hyprial/daemon/impl/org/network/casdoor/sync.py:210` | Python成员同步；S/F/O(confirmed JSON)；外部与本地不伪原子 |
| AR-35 | `src/hyprial/daemon/impl/orgfs/composition/directory.py:122` | Python目录入口；S/T/O；写交所属space资源 |
| AR-36 | `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:81` | Python空间生命周期saga；S/D/F/T/O |
| AR-37 | `src/hyprial/daemon/impl/orgfs/document/effects.py:153` | Python post-commit effects；T/F/O；提交事实来自所属D/F |
| AR-38 | `src/hyprial/daemon/impl/orgfs/document/space_state.py:197` | CRDT留Python；拟S/D/F+O；Doc/journal/缓存顺序未闭合前BLOCKED |
| AR-39 | `src/hyprial/daemon/impl/orgfs/mesh.py:203` | Python mesh ingress；T/S/O；导入经space写边界 |
| AR-40 | `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:116` | Python规划+F/E/O执行；派生文件不成第二CRDT权威 |
| AR-41 | `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:182` | Python blob策略；F+D(blob-refs)+O；跨文件/库提交BLOCKED |
| AR-42 | `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:211` | Python复制规划；F/T/O；source/retirement跨资源边界BLOCKED |
| AR-43 | `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:256` | Python CRDT/领域计划；D(space journal)+F/S；CRDT提交点BLOCKED |
| AR-44 | `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:137` | Python PAC应用服务；D(PAC)+S/C/O；通用native DB writer |
| AR-45 | `src/hyprial/daemon/impl/processes/orphan_processes.py:705` | Python adoption策略；P+F/O(CustodyJournal JSON)；防PID复用 |
| AR-46 | `src/hyprial/daemon/impl/processes/process_owner.py:161` | P(stdio/exit)+O，阻塞原生I/O可E；业务parser留Python |
| AR-47 | `src/hyprial/daemon/impl/processes/result_settlement.py:62` | Python结果结算；D/S/O；晚到结果与退休责任不丢 |
| AR-48 | `src/hyprial/daemon/impl/runtime/settlement.py:127` | Python转发/结算；D/F/T/O；unknown不盲重放 |
| AR-49 | `src/hyprial/daemon/impl/session_actor/actor.py:122` | Python Session规则；D(shared lifecycle)+S/C/O；与AR-53共writer |
| AR-50 | `src/hyprial/daemon/impl/squire/actors/delivery.py:101` | Python用户投递；S/O，持久结果用AR-51 F，网络T或Python SDK |
| AR-51 | `src/hyprial/daemon/impl/squire/actors/ledger.py:81` | F原子文件/CAS+O；不擅自迁SQLite；保留retention副作用 |
| AR-52 | `src/hyprial/daemon/impl/squire/actors/profile.py:119` | Python profile规则；F(UserProfileStore文件)+S/O |
| AR-53 | `src/hyprial/daemon/impl/state_persistence/authority.py:89` | 单库D(desired_store.state_db/journal)+O/scoped变化；模型/计划留Python |
| AR-54 | `src/hyprial/daemon/impl/transfer/p2/actor.py:109` | Python transfer saga；S/D/F/P/T/O按原资源边界 |
| AR-55 | `src/hyprial/daemon/impl/transfer/p2/source/runner.py:40` | Python source saga；F/P/T/O，原生I/O可E；保留补偿 |
| AR-56 | `src/hyprial/daemon/impl/transport/presence_actor.py:77` | Python presence规则；S/T/C/O；保留superseded/accepted/unknown |
| AR-57 | `src/hyprial/daemon/impl/transport/query_actor.py:51` | T拥有query句柄+O；Python只生成reply data，不传handler callable |
| AR-58 | `src/hyprial/daemon/impl/transport/session_actor.py:396` | 通用T(session/declarations)+O；Python保留策略和事件解释 |
| AR-59 | `src/hyprial/identity/impl/agents/actor/_core.py:157` | Python AgentRegistry/saga；D(agents)+S/F/P/O；不建Rust领域RegistryActor |
| AR-60 | `src/hyprial/identity/impl/provider_auth/actor/_authority.py:111` | Python认证与SDK；S/F/P/C/O；凭据/SDK对象不作native callable |
| AR-61 | `src/hyprial/kernel/impl/log/writer.py:47` | 删log-writer actor；F/E按文件追加opaque bytes+必要O |

### A2. EffectLane构造点（48/48；41文件）

每行全部构造点的去向均为：**删除EffectLane及其完成重试/custody包装；业务留Python，对应资源操作和等待迁native；test/evidence=OPEN**。同一文件多行是不同构造点，不得合并计数。

| 文件 | 全部行号 | 点数 |
|---|---:|---:|
| `src/hyprial/biz/impl/usage_actor.py` | 56 | 1 |
| `src/hyprial/daemon/impl/application/messaging/operator_alarm.py` | 93 | 1 |
| `src/hyprial/daemon/impl/autoupdate/actor.py` | 95 | 1 |
| `src/hyprial/daemon/impl/configuration/home_lease_actor.py` | 126,132 | 2 |
| `src/hyprial/daemon/impl/correlation/bounded_cadence.py` | 81 | 1 |
| `src/hyprial/daemon/impl/desired_state_io.py` | 116 | 1 |
| `src/hyprial/daemon/impl/dispatch/runtime/holds.py` | 56 | 1 |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py` | 91 | 1 |
| `src/hyprial/daemon/impl/dispatch/runtime/progress.py` | 65 | 1 |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py` | 259 | 1 |
| `src/hyprial/daemon/impl/ipc/ipc_request_owner.py` | 66 | 1 |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py` | 255 | 1 |
| `src/hyprial/daemon/impl/network/routing/session_route_coordinator.py` | 139 | 1 |
| `src/hyprial/daemon/impl/operations/management_actor.py` | 127 | 1 |
| `src/hyprial/daemon/impl/operations/watchdog_actor.py` | 144 | 1 |
| `src/hyprial/daemon/impl/org/mesh.py` | 165 | 1 |
| `src/hyprial/daemon/impl/org/network/access/actor.py` | 129 | 1 |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py` | 219 | 1 |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py` | 136 | 1 |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py` | 102 | 1 |
| `src/hyprial/daemon/impl/orgfs/document/effects.py` | 187,194 | 2 |
| `src/hyprial/daemon/impl/orgfs/document/space_state.py` | 215 | 1 |
| `src/hyprial/daemon/impl/orgfs/mesh.py` | 212 | 1 |
| `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py` | 132 | 1 |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py` | 215 | 1 |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py` | 229 | 1 |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py` | 265 | 1 |
| `src/hyprial/daemon/impl/processes/orphan_processes.py` | 727 | 1 |
| `src/hyprial/daemon/impl/processes/process_owner.py` | 170 | 1 |
| `src/hyprial/daemon/impl/processes/result_settlement.py` | 73 | 1 |
| `src/hyprial/daemon/impl/runtime/settlement.py` | 144 | 1 |
| `src/hyprial/daemon/impl/service_connect/io.py` | 64 | 1 |
| `src/hyprial/daemon/impl/squire/actors/delivery.py` | 110 | 1 |
| `src/hyprial/daemon/impl/squire/actors/profile.py` | 127 | 1 |
| `src/hyprial/daemon/impl/transfer/p2/actor.py` | 117 | 1 |
| `src/hyprial/daemon/impl/transfer/p2/source/runner.py` | 45 | 1 |
| `src/hyprial/daemon/impl/transport/presence_actor.py` | 87 | 1 |
| `src/hyprial/daemon/impl/transport/session_actor.py` | 198,410,418 | 3 |
| `src/hyprial/identity/impl/agents/state/worker_state.py` | 239,331 | 2 |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py` | 119,125,134 | 3 |
| `src/hyprial/kernel/impl/hooks/bus.py` | 81 | 1 |

### A3. 手写lane/mailbox（结构口径；当前16组）

这些包装全部删除；其中与A2/A4重叠的构造仍各在原计数中只计一次。业务状态可改成普通Python值对象，但不得保留私有命令分派循环；test/evidence=OPEN。追踪不得只按`_Lane`等类名：R1必须找出循环内无位置参数的`get()`/`get_nowait()`/`popleft()`（包括`to_thread(queue.get)`）以及其后的调用/await分派，并反向关联生产者、构造与Thread/Executor/task。类或文件改名不能使候选项消失；动态receiver、间接alias或数据流无法证实时标`unresolved/OPEN`并保守失败、人工复核。当前16组是最低全集而非“已证明无其他 mailbox”；S2.0冻结前必须完成结构扫描与人工差集裁定。

| ID | 根与全部关键站点 | 去向草案 |
|---|---|---|
| ML-01 | `daemon/impl/lifecycle/lifecycle_coordinator.py`：`_Mailbox`408；Queue496,568；Condition499；Thread527,532；loops663,726,767 | 删除mailbox/effect/recover分派；AR-27 saga+native D/S/O/F/P |
| ML-02 | `daemon/impl/inbox/actor/worker.py`：`DeliveryIoWorker`33；deque70；Condition71；Thread79；loops134,378 | 删除DeliveryIoWorker；I/O、结果和receipt由对应native资源/O承接 |
| ML-03 | `identity/impl/agents/home/effects.py`：`AgentHomeEffectLane`146；deque166；Condition167；Thread171；loop230 | 删除私有lane；Agent home saga与native资源操作，旧代/receipt语义保留 |
| ML-04 | `daemon/impl/dispatch/remote/__init__.py`：Queue67；Thread95,104；`_remote_effect_loop`653及outcome pump | 删除effect/outcome私有循环；AR-12+D-PAC，禁止以Python新队列替代 |
| ML-05 | `kernel/impl/hooks/bus.py`：`_Lane`60；EffectLane81 | 删除_Lane/EffectLane；hook发布变成数据事件与通用有界资源 |
| ML-06 | `daemon/impl/inbox_watch.py`：`_Lane`148；poll/heartbeat实例172,173及`_issue/_settle`343,347 | 删除双lane包装；客户端协议状态可留普通Python对象，调度/等待迁公共能力 |
| ML-07 | `daemon/impl/org/network/status/__init__.py`：Queue27；Thread28；loop43 | 删除projection私有queue/thread；状态投影由S/O和版本化数据事件承接 |
| ML-08 | `daemon/impl/composition/links.py`：`_LinkHookLane`664；构造/注册823–825；detach838–843 | 删除hook lane包装；LinkService业务留Python，事件接入公共数据面 |
| ML-09 | `daemon/impl/adapters/lark/outbound/reactions.py`：`_NativeLane`520；ThreadPoolExecutor537；submit551–552；lanes596–599 | 删除两条私有executor lane；AR-06 SDK边缘+持久D/O；当前形状不在B白名单 |
| ML-10 | `daemon/impl/hooks/service.py`：`_ConsumerLane`79；`_HandlerLane`93；注册158,403；配置Thread455 | 删除lane与私有配置刷新线程；hook配置/交付改公共数据事件与C/O |
| ML-11 | 历史 @5b9eb6530b（已于 2026-11-21 W1B 整包删除未接线死码，原位于 daemon 下的快照工作池包装 lines 198, 203–205） | 已整包移除，无需迁移；原草案：删除专用单lane worker |
| ML-12 | `daemon/impl/session_actor/worker.py`：Queue50；Condition51；Thread60；loops114,158 | 删除session-agent effect worker；AR-49应用服务+共享native资源 |
| ML-13 | `daemon/impl/service_connect/io.py`：`_BoundedLane`60；EffectLane64；三lane154–158；close183–184 | 删除_BoundedLane及catalog/protected/control三lane；Python SDK/协议+O |
| ML-14 | `daemon/impl/pac/workflows/runtime/core.py`：delivery PriorityQueue声明92/构造93；delivery Thread108；`get(timeout=0.2)`151及`_drain`分派157 | 删除双delivery线程、PriorityQueue与命令分派；通知/receipt归D-PAC+O，不用新Python queue替代 |
| ML-15 | `daemon/impl/harnesses/streaming/process.py`：`_run`循环471；`to_thread(self._io_requests.get)`491；delivery构造/分派509起 | 删除私有I/O mailbox；Harness协议留Python，阻塞I/O与等待迁通用native/E/O |
| ML-16 | `daemon/impl/application/__init__.py`：SimpleQueue725；`daemon/impl/application/messaging/maintenance.py`：producer259、`get_nowait`316及`record_activity`分派321 | 删除agent-activity私有mailbox；活动记录由同一Inbox/agents资源边界与O承接 |

路径前缀均为`src/hyprial/`。区间行号用于定位同一包装，不新增构造计数。ML-14的92是类型声明、93才是构造点，两者都保留以对应评审定位并避免误改构造计数。

### A3b. 全机共用投递tick（2026-10-27增补，非构造计数）

`daemon/impl/runtime/bridge.py` 的 `on_timer`（约1.5秒）在一轮内按顺序对全部常驻agent执行 `_dispatch_harness_deliveries`，随后才是重试泵唤醒与清理。它不是mailbox构造，A1–A5的AST口径数不到，但它是事实上的全机唯一投递分派点：10-26 一个agent在该循环内抛出 `KeyError((“lark”, True))`，整轮中止，全机pi/codex投递停摆约10.5小时（#1983在现有循环内加了按agent的异常隔离，只止血）。

去向（Allen 10-27批准"改为逐个agent投递"；按本清单总规则不新建Python mailbox/actor）：

- 投递规则（注入原文/摘要、链路闸门、hold、batch资格）留Python应用服务。
- 提交与等待按收件agent分键：各自有序、各自的失败/挂起/慢I/O只影响该agent；由native资源端口承接，tick只发信号与到期唤醒。
- 归属：inbox/IPC分片（actor-split的I），与harness生命周期分片（S）对接；与ML-02/ML-15同一迁移批次核对，避免两处投递路径。
- 验收（test/evidence=OPEN）：故障注入使任一agent投递抛错或挂起时，其余agent投递延迟与基线一致；同名agent/adapter历史数据不影响他人投递。
- 2026-10-27增补：每agent key带声明式监督（强度超限即隔离并发运维可见告警）与生命周期事件，`order_barrier=hold`；见[OTP语义增补](actor-rustify-otp-semantics-2026-10-27.md)§1–§2，验收OTP-P-01/OTP-S-01。

### A4. Thread构造点（102/102；71文件）

除标有B-01…B-09的精确行外，全部默认**删除或迁native**。即使Thread在B白名单，关联Queue/Condition、guardian、retry、receipt或命令分派也不因此获豁免。

| 文件（前缀`src/hyprial/`） | 行号 |
|---|---:|
| `biz/impl/hooks/recap/writer.py` | 84 |
| `biz/impl/routine/execution/coordinator/__init__.py` | 106,115 |
| `biz/impl/routine/execution/service.py` | 206,212 |
| `biz/impl/usage.py` | 670 |
| `daemon/impl/adapters/lark/outbound/gateway.py` | 207 |
| `daemon/impl/adapters/lark/runtime/effects.py` | 151,476 |
| `daemon/impl/adapters/lark/runtime/health.py` | 134,474 |
| `daemon/impl/adapters/lark/runtime/runtime.py` | 97,533 |
| `daemon/impl/adapters/lark/worker/entry.py` | 389,501 |
| `daemon/impl/adapters/lark/worker/process.py` | 153,564(B-09) |
| `daemon/impl/adapters/lark/worker/sweep.py` | 91,168 |
| `daemon/impl/application/messaging/status/refresh.py` | 108 |
| `daemon/impl/application/netendpoints/services.py` | 20 |
| `daemon/impl/application/restore.py` | 745 |
| `daemon/impl/application/shutdown.py` | 311 |
| `daemon/impl/autoupdate/__init__.py` | 122 |
| `daemon/impl/configuration/home_guard.py` | 235,242 |
| `daemon/impl/dispatch/remote/__init__.py` | 95,104 |
| `daemon/impl/dispatch/remote/wire.py` | 198 |
| `daemon/impl/forwarding/controller.py` | 129(B-01),132(B-02),143 |
| `daemon/impl/harnesses/actor/io_port.py` | 334 |
| `daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py` | 507 |
| `daemon/impl/harnesses/codex/carrier.py` | 257 |
| `daemon/impl/harnesses/dsh/api.py` | 69 |
| `daemon/impl/harnesses/dsh/process.py` | 354,449(B-03),452(B-04) |
| `daemon/impl/harnesses/protocol/common.py` | 170(B-05),175(B-06) |
| `daemon/impl/harnesses/python_worker/__init__.py` | 423(B-07),424(B-08) |
| `daemon/impl/harnesses/runtime/tmux.py` | 69 |
| `daemon/impl/harnesses/streaming/process.py` | 136,201 |
| `daemon/impl/harnesses/turn_delivery/channel/worker_relay.py` | 110,134 |
| `daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py` | 268 |
| `daemon/impl/hooks/service.py` | 455 |
| `daemon/impl/identity/bindings.py` | 491 |
| `daemon/impl/inbox/actor/worker.py` | 79 |
| `daemon/impl/inbox/links/wake.py` | 103 |
| `daemon/impl/ipc/server.py` | 330,541 |
| `daemon/impl/lifecycle/atomic_lifecycle_ports.py` | 65 |
| `daemon/impl/lifecycle/health/database.py` | 137 |
| `daemon/impl/lifecycle/lifecycle_coordinator.py` | 527,532 |
| `daemon/impl/lifecycle/maintenance_watchdog.py` | 167,325 |
| `daemon/impl/lifecycle_manager/process.py` | 137 |
| `daemon/impl/links/service/core.py` | 127 |
| `daemon/impl/mcp/entry.py` | 259 |
| `daemon/impl/org/network/casdoor/webhook.py` | 174 |
| `daemon/impl/org/network/status/__init__.py` | 28 |
| `daemon/impl/orgfs/webserver/server.py` | 457 |
| `daemon/impl/pac/actors/daemon.py` | 373,380,385 |
| `daemon/impl/pac/actors/gc.py` | 86 |
| `daemon/impl/pac/graphs/authority/__init__.py` | 158,163 |
| `daemon/impl/pac/workflows/runtime/core.py` | 108,115 |
| `daemon/impl/processes/orphan_processes.py` | 591 |
| `daemon/impl/route_registration/io.py` | 117,126 |
| `daemon/impl/runtime/bridge.py` | 445 |
| `daemon/impl/runtime/forwards.py` | 69,568 |
| `daemon/impl/session_actor/actor.py` | 235,448 |
| `daemon/impl/session_actor/worker.py` | 60 |
| `daemon/impl/transport/session_recovery.py` | 189 |
| `daemon/impl/transport/zenoh/dispatchers.py` | 68,233 |
| `daemon/impl/transport/zenoh/presence.py` | 228 |
| `daemon/impl/transport/zenoh/transport.py` | 199 |
| `identity/impl/agents/home/effects.py` | 171 |
| `identity/impl/agents/state/read_port.py` | 127 |
| `identity/impl/provider_auth/actor/_authority.py` | 141 |
| `identity/impl/provider_auth/coordinator/_coordinator.py` | 501 |
| `identity/impl/provider_auth/helper.py` | 159 |
| `kernel/impl/actor_runtime/effects.py` | 81 |
| `kernel/impl/hooks/intercept.py` | 206 |
| `shell/impl/cli/commands/agent/runtime/dsh/launch.py` | 569 |
| `shell/impl/gui/adapter/host.py` | 443 |
| `shell/impl/gui/adapter/lifecycle.py` | 345,382 |
| `shell/impl/gui/adapter/mobile/pairing/server/controller.py` | 219,294 |

### A4d. 其他并发构造与后台任务（S2.0冻结前强制全集）

以下均为当前SHA的机械评审超集，默认删除、迁native或按附录B重新裁定；除B-01…B-09外没有隐含的Timer/Executor/to_thread/task/spawn白名单。`deque=45`不等于45个mailbox，是否构成lane由A3结构口径判定；普通局部值语义也须保留在diff中，防止改成私有队列后绕过门。

| 类别 | 文件（前缀`src/hyprial/`） | 行号/说明 |
|---|---|---|
| Timer=5 | `daemon/impl/harnesses/actor/behaviors/desired.py` | 571 |
| | `daemon/impl/harnesses/actor/behaviors/process_events.py` | 98 |
| | `daemon/impl/harnesses/streaming/turn_io.py` | 342 |
| | `identity/impl/agents/state/worker_state.py` | 466；`_schedule_retry`，属于禁止的retry调度 |
| | `shell/impl/cli/commands/doctor/commands.py` | 491 |
| ThreadPoolExecutor=8 | `daemon/impl/adapters/lark/inbound/inbound_runtime.py` | 149 |
| | `daemon/impl/adapters/lark/outbound/reactions.py` | 537；ML-09 |
| | `daemon/impl/adapters/lark/runtime/effects.py` | 55 |
| | 历史 @5b9eb6530b（已于 2026-11-21 W1B 整包移除） | 203；ML-11 |
| | `daemon/impl/harnesses/actor/io_port.py` | 80 |
| | `daemon/impl/harnesses/python_worker/_python_worker.py` | 500 |
| | `daemon/impl/orgfs/mesh.py` | 171 |
| | `shell/impl/cli/commands/workflow/internal/worktrees.py` | 370 |
| asyncio.to_thread=7 | `daemon/impl/harnesses/codex/carrier.py` | 122,142,176 |
| | `daemon/impl/harnesses/dsh/api.py` | 174 |
| | `daemon/impl/harnesses/streaming/process.py` | 491,791 |
| | `kernel/impl/platform/windows_owned_process.py` | 150 |
| multiprocessing spawn=1 | `daemon/impl/harnesses/dsh/api.py` | 81 `get_context("spawn")`；84 `context.Process`为同一评审根 |
| SimpleQueue=1 | `daemon/impl/application/__init__.py` | 725；ML-16 |
| PriorityQueue=1 | `daemon/impl/pac/workflows/runtime/core.py` | 93；ML-14 |
| create_task=11 | `daemon/impl/harnesses/agent_sdk/__init__.py` | 495,496 |
| | `daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py` | 571,573 |
| | `daemon/impl/harnesses/codex/client.py` | 367,368,369 |
| | `daemon/impl/harnesses/pi/rpc_client.py` | 291,294,377,382 |
| ensure_future=0 | — | 当前无构造；最终仍须扫描 |
| Thread子类定义=0 | — | 当前无定义；最终仍须扫描所有base，不能只扫描`Thread(...)` |

`deque`构造全集（45/45；24文件）：

| 文件（前缀`src/hyprial/`） | 行号 |
|---|---:|
| `biz/impl/hooks/recap/writer.py` | 197 |
| `daemon/impl/adapters/lark/runtime/handler.py` | 705 |
| `daemon/impl/adapters/lark/runtime/health.py` | 77 |
| `daemon/impl/application/messaging/status/refresh.py` | 148 |
| `daemon/impl/forwarding/controller.py` | 101,116,282 |
| `daemon/impl/forwarding/supervisor.py` | 91,320 |
| `daemon/impl/harnesses/actor/actor.py` | 187,239 |
| `daemon/impl/harnesses/carrier/interactive_carrier_runtime.py` | 166 |
| `daemon/impl/harnesses/runtime/observation.py` | 47 |
| `daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py` | 203,348,530 |
| `daemon/impl/inbox/actor/internal/receipts.py` | 28 |
| `daemon/impl/inbox/actor/worker.py` | 70 |
| `daemon/impl/inbox/service/core.py` | 88 |
| `daemon/impl/links/service/core.py` | 406,410 |
| `daemon/impl/mcp/wake.py` | 143,183,197,242,271,375,392,421,430,439,452,457 |
| `daemon/impl/network/routing/session_route_coordinator.py` | 123,289 |
| `daemon/impl/org/network/casdoor/sync.py` | 345 |
| `daemon/impl/route_registration/io.py` | 110,392 |
| `daemon/impl/session_actor/actor.py` | 132 |
| `daemon/impl/state_persistence/alarm.py` | 95,207 |
| `daemon/impl/transport/session_actor.py` | 383 |
| `identity/impl/agents/home/effects.py` | 166 |
| `kernel/impl/actor_runtime/effects.py` | 74 |
| `shell/impl/gui/adapter/mobile/pairing/server/limits.py` | 140 |

### A4b. Queue构造点（36/36；27文件）

全部默认删除或迁native；**本草案没有Queue保留项**。

| 文件（前缀`src/hyprial/`） | 行号 |
|---|---:|
| `biz/impl/hooks/recap/writer.py` | 54 |
| `biz/impl/routine/execution/coordinator/__init__.py` | 88 |
| `biz/impl/routine/execution/service.py` | 161,167 |
| `daemon/impl/adapters/lark/runtime/runtime.py` | 96 |
| `daemon/impl/application/messaging/status/refresh.py` | 71 |
| `daemon/impl/autoupdate/__init__.py` | 94 |
| `daemon/impl/dispatch/remote/__init__.py` | 67 |
| `daemon/impl/forwarding/controller.py` | 115 |
| `daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py` | 506 |
| `daemon/impl/harnesses/codex/client.py` | 253 |
| `daemon/impl/harnesses/python_worker/__init__.py` | 147 |
| `daemon/impl/harnesses/streaming/process.py` | 133,147,151 |
| `daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py` | 265 |
| `daemon/impl/lifecycle/atomic_lifecycle_ports.py` | 61 |
| `daemon/impl/lifecycle/lifecycle_coordinator.py` | 496,568 |
| `daemon/impl/lifecycle_manager/process.py` | 116 |
| `daemon/impl/org/network/status/__init__.py` | 27 |
| `daemon/impl/pac/actors/daemon.py` | 331,334,337 |
| `daemon/impl/pac/graphs/authority/__init__.py` | 129 |
| `daemon/impl/pac/workflows/runtime/core.py` | 89 |
| `daemon/impl/route_registration/io.py` | 88,89 |
| `daemon/impl/runtime/bridge.py` | 290,298 |
| `daemon/impl/session_actor/worker.py` | 50 |
| `daemon/impl/transport/zenoh/dispatchers.py` | 38,174 |
| `daemon/impl/transport/zenoh/transport.py` | 188 |
| `identity/impl/agents/state/read_port.py` | 120 |
| `kernel/impl/hooks/intercept.py` | 106 |

### A4c. Condition构造点（35/35；31文件）

全部默认删除或迁native；本草案不预批任何现有Condition为“单服务对象锁”例外。最终若申请附录B锁例外，须按精确行Allen裁定。

| 文件（前缀`src/hyprial/`） | 行号 |
|---|---:|
| `biz/impl/routine/execution/coordinator/__init__.py` | 83 |
| `biz/impl/routine/execution/service.py` | 158,168 |
| `daemon/impl/adapters/lark/runtime/effects.py` | 50 |
| `daemon/impl/adapters/lark/runtime/health.py` | 76 |
| `daemon/impl/application/actors/sessions/routes.py` | 67 |
| `daemon/impl/application/netendpoints/services.py` | 17 |
| `daemon/impl/composition/events.py` | 56 |
| `daemon/impl/forwarding/controller.py` | 100,281 |
| `daemon/impl/harnesses/actor/io_port.py` | 86 |
| `daemon/impl/inbox/actor/internal/projection.py` | 20,78 |
| `daemon/impl/inbox/actor/internal/receipts.py` | 25 |
| `daemon/impl/inbox/actor/worker.py` | 71 |
| `daemon/impl/inbox/links/io.py` | 84 |
| `daemon/impl/inbox/links/wake.py` | 95 |
| `daemon/impl/lifecycle/atomic_lifecycle_ports.py` | 62 |
| `daemon/impl/lifecycle/lifecycle_coordinator.py` | 499 |
| `daemon/impl/lifecycle_manager/process.py` | 117 |
| `daemon/impl/orgfs/storage/blob_authority.py` | 171 |
| `daemon/impl/orgfs/storage/space_authority.py` | 217 |
| `daemon/impl/route_registration/io.py` | 91 |
| `daemon/impl/session_actor/worker.py` | 51 |
| `daemon/impl/transport/query_actor.py` | 46 |
| `daemon/impl/transport/session_actor.py` | 190 |
| `daemon/impl/transport/zenoh/dispatchers.py` | 177 |
| `daemon/impl/transport/zenoh/transport.py` | 163,170 |
| `identity/impl/agents/actor/_base.py` | 262 |
| `identity/impl/agents/home/effects.py` | 167 |
| `identity/impl/provider_auth/actor/_authority.py` | 101 |
| `kernel/impl/actor_runtime/effects.py` | 75 |
| `kernel/impl/actor_runtime/scheduler.py` | 26 |
| `kernel/impl/log/writer.py` | 41 |

### A5. 旧actor协议符号引用文件（78/78）

17符号集合为：`ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorState, DrainReport, ExpectedActorError, STATE_AUTHORITY, ActorSpec, EffectCompleted, EffectLane, EffectRequest, DEFAULT_POLICIES, EXTERNAL_IO, PROCESS_LIFECYCLE, SupervisionPolicy, GenerationScheduler`。下表聚合每个文件的Name/Attribute命中符号；所有引用最终删除或替换，test/evidence=OPEN。

| 文件（前缀`src/hyprial/`） | 命中符号 |
|---|---|
| `biz/impl/routine/execution/coordinator/__init__.py` | ActorRuntime, ActorSpec |
| `biz/impl/routine/execution/service.py` | ActorRuntime, ActorSpec |
| `biz/impl/usage_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `daemon/impl/adapters/lark/inbound/inbound_runtime.py` | ActorHandle, ActorRuntime, ActorSpec |
| `daemon/impl/adapters/lark/outbound/gateway.py` | ActorRuntime, ActorSpec |
| `daemon/impl/adapters/lark/outbound/reactions.py` | ActorHandle, ActorRuntime, ActorSpec |
| `daemon/impl/adapters/lark/runtime/handler.py` | SupervisionPolicy |
| `daemon/impl/adapters/lark/runtime/runtime.py` | ActorHandle, ActorRuntime, ActorSpec, DEFAULT_POLICIES, PROCESS_LIFECYCLE |
| `daemon/impl/adapters/lark/worker/control.py` | ActorRuntime, ActorSpec |
| `daemon/impl/application/__init__.py` | GenerationScheduler |
| `daemon/impl/application/messaging/operator_alarm.py` | EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/application/netendpoints/endpoints.py` | DEFAULT_POLICIES, EXTERNAL_IO |
| `daemon/impl/autoupdate/actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `daemon/impl/configuration/home_lease_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `daemon/impl/correlation/bounded_cadence.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/desired_state_io.py` | EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/dispatch/remote/__init__.py` | ActorRuntime, ActorSpec |
| `daemon/impl/dispatch/runtime/diagnostics.py` | ActorRuntime, ActorSpec |
| `daemon/impl/dispatch/runtime/holds.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/dispatch/runtime/offers.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/dispatch/runtime/progress.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/dispatch/runtime/state.py` | ActorRuntime, ActorSpec |
| `daemon/impl/forwarding/authority.py` | ActorRuntime, ActorSpec |
| `daemon/impl/forwarding/supervisor.py` | ActorRuntime, ActorSpec, DEFAULT_POLICIES, GenerationScheduler, PROCESS_LIFECYCLE, SupervisionPolicy |
| `daemon/impl/harnesses/actor/actor.py` | ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane |
| `daemon/impl/harnesses/actor/behaviors/delivery.py` | EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/harnesses/carrier/interactive_carrier_runtime.py` | ActorRuntime, ActorSpec, ExpectedActorError, STATE_AUTHORITY |
| `daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py` | ActorEvent, ActorEventKind, ActorRuntime, ActorSpec, ExpectedActorError, PROCESS_LIFECYCLE |
| `daemon/impl/inbox/actor/coordinator.py` | ActorHandle, ActorRuntime, ActorSpec, ActorState, DrainReport, STATE_AUTHORITY |
| `daemon/impl/inbox/service/policy.py` | DEFAULT_POLICIES, EXTERNAL_IO |
| `daemon/impl/inbox/tracking/fetch_receipts.py` | ActorRuntime, ActorSpec |
| `daemon/impl/ipc/ipc_request_owner.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/lifecycle/duplicate_actor.py` | ActorRuntime, ActorSpec |
| `daemon/impl/lifecycle/lifecycle_coordinator.py` | ActorRuntime, ActorSpec |
| `daemon/impl/lifecycle/maintenance_watchdog.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/network/routing/session_route_coordinator.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/operations/management_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/operations/watchdog_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/org/mesh.py` | ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/org/network/access/actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `daemon/impl/org/network/casdoor/sync.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `daemon/impl/orgfs/composition/directory.py` | ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/composition/lifecycle.py` | ActorEvent, ActorEventKind, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/composition/vocabulary.py` | ActorHandle, ActorRuntime, EffectLane |
| `daemon/impl/orgfs/docs.py` | ActorHandle, ActorRuntime, EffectLane |
| `daemon/impl/orgfs/document/effects.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/document/model.py` | ActorHandle, ActorRuntime, EffectLane |
| `daemon/impl/orgfs/document/space_state.py` | ActorEvent, ActorEventKind, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/mesh.py` | ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/network/ingress.py` | EffectCompleted, EffectRequest |
| `daemon/impl/orgfs/projection/checkout_authority.py` | ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/runtime.py` | ActorHandle, ActorRuntime, EffectLane, EffectRequest |
| `daemon/impl/orgfs/storage/blob_authority.py` | ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/storage/replica_authority.py` | ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/orgfs/storage/space_authority.py` | ActorEvent, ActorEventKind, ActorHandle, ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/pac/graphs/authority/__init__.py` | ActorRuntime, ActorSpec |
| `daemon/impl/processes/orphan_processes.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/processes/process_owner.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/processes/result_settlement.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/runtime/settlement.py` | ActorRuntime, ActorSpec, DEFAULT_POLICIES, EXTERNAL_IO, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/service_connect/io.py` | EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/service_connect/manager.py` | EffectCompleted |
| `daemon/impl/session_actor/actor.py` | ActorRuntime, ActorSpec, ActorState, DrainReport, EffectCompleted |
| `daemon/impl/session_actor/generation.py` | EffectCompleted |
| `daemon/impl/squire/actors/delivery.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/squire/actors/ledger.py` | ActorRuntime, ActorSpec |
| `daemon/impl/squire/actors/profile.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/state_persistence/authority.py` | ActorRuntime, ActorSpec |
| `daemon/impl/transfer/p2/actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/transfer/p2/source/runner.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/transport/presence_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |
| `daemon/impl/transport/query_actor.py` | ActorRuntime, ActorSpec |
| `daemon/impl/transport/session_actor.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest, GenerationScheduler |
| `identity/impl/agents/actor/_core.py` | ActorRuntime, ActorSpec, DrainReport, EffectCompleted, EffectLane, EffectRequest |
| `identity/impl/agents/actor/_generation_dispatch.py` | EffectCompleted |
| `identity/impl/agents/actor/_generation_ops.py` | EffectRequest |
| `identity/impl/agents/state/worker_state.py` | EffectCompleted, EffectLane, EffectRequest |
| `identity/impl/provider_auth/actor/_authority.py` | ActorRuntime, ActorSpec, EffectCompleted, EffectLane, EffectRequest |

### A6. 最终静态删除门

最终候选树必须同时满足以下AST硬断言；**S2.0冻结前**必须在`tests/kernel/actors/test_legacy_engine_ast_gate.py`实现（当前SHA没有该测试，本文不声称其存在或PASS），直接消费干净树R1 JSON而非复制一套宽松匹配：

1. 解析到旧kernel模块的`ActorRuntime`、`EffectLane`构造均为0（R1 `legacy_actor_constructors`；A1/A2的61/48仍是调查全集），A3包装定义及其私有分派循环均为0。旧actor符号全集为A5的17个加`AdmissionResult, ActorSnapshot, ActorBackend, EffectLaneSnapshot, TimerHandle, TimerFactory, EventSink, FailureSink, CommandStartedSink, freeze_policy_catalog, ActorGuardian, create_backend, handler_factory, guardian, scheduler`。从`hyprial.kernel*`导入这些符号、在旧kernel模块定义/导出这些符号、以及解析到这些模块的Name/Attribute残留均为0；import alias也必须解析。仅同名但未解析到旧kernel模块的符号不计入，PAC自己的`ActorRuntime` Protocol与`DaemonActorRuntime`不得被此门误杀。无法解析的引用是`unresolved`并保守失败、人工复核，不得当成0。新`CommandHandler`是保留的owned数据公共面，不属于删除集合。
2. `src/hyprial/kernel/impl/actor_runtime/`目录不存在，且没有任何`hyprial.kernel.impl.actor_runtime*`导入（包括alias）。Pykka旧backend不因目录改名而获得豁免。
3. Thread/Timer/ThreadPoolExecutor/to_thread/create_task/ensure_future/spawn及Thread子类定义与最终BASE附录B逐项diff为0；Queue/SimpleQueue/PriorityQueue无白名单；`Executor.submit`、Timer callback和task coroutine符号必须进入差异，递归Timer与`create_task(loop())`不能绕过。Lock/RLock只要求`candidate_key_multiset - BASE_key_multiset = ∅`（同键第二个构造也计新增）；存量不逐个补裁定，但每个新增或申请保留的锁例外仍须结构检查（单服务对象字段、无分派、无持锁跨资源等待）并由Allen裁定。Condition仍逐项裁定。动态alias、重复白名单键、无法识别target/callback或数据流不明均保守失败，不得静默PASS。

该测试的负对照至少覆盖：PAC Protocol与`DaemonActorRuntime`不误报、旧kernel alias import/引用必报；actor_runtime目录/import；`T = threading.Thread; T(...)`；`class Loop(threading.Thread)`；`ThreadPoolExecutor(1).submit(loop)`；直接及间接递归Timer；`create_task(loop())`/`ensure_future(loop())`；白名单原行target改成分派循环后added/missing各非0；只增删前置行导致行号漂移时不误报；重复键和无法解析target保守失败。另加`if not read_only: sqlite3.connect(...)`必须判W，以及Lock/RLock仅BASE后新增项触发。上述负对照均为**待实现要求，OPEN至S2.0冻结**。

## 附录B：Python并发白名单草案

**本附录同样是草案，最终BASE必须重核并由Allen冻结。** 白名单是上限，不是必须保留的实现，也不豁免其调用方/关联队列的审查。

允许类别仅限：

1. **单服务对象Lock/RLock/Condition**：只保护该对象内存字段，无命令分派，不持锁等待另一资源，不承担跨进程互斥、DB事务或durable fence。Condition不得形成mailbox。Lock/RLock以最终BASE键多重集合为存量基线（保留同键出现次数），只对BASE后新增键/新增出现次数要求Allen逐项裁定，不追溯要求当前215/59个存量逐个补裁定；Condition及任何申请例外的新锁仍须逐项裁定和结构检查。
2. **有界concurrent.futures**：只允许执行SDK调用；必须有硬并发/在途上限、关闭和unknown处理。不得有自有接纳、重试、receipt、completion-custody或业务命令循环。当前没有预先放行的`ThreadPoolExecutor`；ML-09/ML-11及Lark inbound/runtime现有executor均须删除、改造后重审或迁native。
3. **SDK内部线程**：由第三方SDK自己创建且产品不持有/分派其工作线程；SDK对象和业务调用留Python。产品侧包装不因此自动白名单化。
4. **逐项列名的纯stdio I/O线程**：只阻塞读取/写入stdin/stdout/stderr或对应pipe到EOF，解析/缓冲/数据通知可在该I/O对象内完成；不得分派业务命令、运行guardian/generation/timer/retry/receipt循环。

禁止：私有`queue+thread+命令分派`；guardian/generation/timer/retry/receipt循环；以“SDK”名义包装整个业务handler；以“纯I/O”名义保留pump、轮询、health、watchdog或完成回执循环。

### B-STDIO：当前仅有的9个候选保留站点

白名单键硬性格式为`文件 | 所在类/函数qualified scope | Thread的target符号`；行号只用于当前SHA定位，不是身份。target取`target=`关键字，缺省时取第二位置参数；target缺失、动态无法解析或同键多次构造均为失败。改写target会同时产生added/missing，前置行漂移不改变键。其它并发构造也以文件+scope+构造/callback符号形成稳定键；不能只查`Name("Thread")`。

| ID | 当前构造点 | 白名单键（键内分隔符用`｜`展示；机器JSON用`|`） | 限定行为 |
|---|---|---|---|
| B-01 | `src/hyprial/daemon/impl/forwarding/controller.py:129` | `src/hyprial/daemon/impl/forwarding/controller.py｜ForwardingSidecarController.__init__｜self._read_stdout` | sidecar stdout读取；不含143 exit monitor，不含115 Queue豁免 |
| B-02 | `src/hyprial/daemon/impl/forwarding/controller.py:132` | `src/hyprial/daemon/impl/forwarding/controller.py｜ForwardingSidecarController.__init__｜self._drain_stderr` | sidecar stderr有界tail drain至EOF |
| B-03 | `src/hyprial/daemon/impl/harnesses/dsh/process.py:449` | `src/hyprial/daemon/impl/harnesses/dsh/process.py｜DshHarnessProcess._read_banner｜drain_stdout` | DSH stdout/banner读取与脱敏；不含354 generation watcher |
| B-04 | `src/hyprial/daemon/impl/harnesses/dsh/process.py:452` | `src/hyprial/daemon/impl/harnesses/dsh/process.py｜DshHarnessProcess._read_banner｜drain_stderr` | DSH stderr有界tail/log drain |
| B-05 | `src/hyprial/daemon/impl/harnesses/protocol/common.py:170` | `src/hyprial/daemon/impl/harnesses/protocol/common.py｜PtyHarnessProcess.__init__｜self._read_stdout` | PTY/stdout读取；不含命令分派 |
| B-06 | `src/hyprial/daemon/impl/harnesses/protocol/common.py:175` | `src/hyprial/daemon/impl/harnesses/protocol/common.py｜PtyHarnessProcess.__init__｜self._read_stderr` | stderr读取与有界缓冲 |
| B-07 | `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:423` | `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py｜PythonWorkerTurnAdapter._start｜self._read_stdout` | worker stdout帧读取；业务处理留原协议对象 |
| B-08 | `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:424` | `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py｜PythonWorkerTurnAdapter._start｜self._read_stderr` | worker stderr有界读取至EOF |
| B-09 | `src/hyprial/daemon/impl/adapters/lark/worker/process.py:564` | `src/hyprial/daemon/impl/adapters/lark/worker/process.py｜_StdoutDrain.__init__｜self._consume` | `_StdoutDrain`仅消费stdout、记录last line/log并关闭流 |

`agent_sdk/_agent_sdk_worker.py:507`不在草案白名单：其stdin reader直接向私有`asyncio.Queue`喂命令，须按完整命令链重审，不能截取“read”一词放行。其余93个Thread构造全部默认删除或迁native。BASE重生成时`B_STDIO`新旧集合的新增项和消失项都必须显式diff；冻结后新增任何Python并发站点均要求`new_sites - BASE_WHITELIST = ∅`且Allen另行修订白名单，禁止静默追加。Executor还扫描所有`.submit`及首参数callback，Timer扫描callback与直接递归，create_task/ensure_future扫描首参数coroutine符号；完整跨函数数据流不能静态证明时标`OPEN`并人工复核/失败，不得因脚本只识别直接形状而PASS。

## 附录D：迁移追踪与每物理库写者草案

### D0. 行级追踪与写者政策

D类每行同样继承`test_nodeid=OPEN`、`evidence=OPEN`，最终必须填实际native writer/只读替代去向、真实测试nodeid和精确head证据链接。A/D行不得只写“已重构”；证据须覆盖对应资源的故障、交错、重启与关闭语义。

1. **每进程每物理库一个native writer**。物理身份用规范化真实path；尚不能静态展开时用constructor identity（store类型+构造参数scope）作为保守键。不同别名指向同一文件时必须合并，不能把展示名/path.stem当身份。
2. 所有非显式read-only连接均列为 **W=write-capable**，即使当前函数看似只查询；`mode=rw`、普通path、临时/备份目标、健康检查和参数化`read_only`均不猜成只读。R只表示该构造点显式`mode=ro`/`read_only=True`，不表示从writer清单删除其物理库。
3. CLI等跨进程写者保留SQLite协议和`busy_timeout`。`SQLITE_BUSY`对外是**可重试错误**，不得静默成功、丢receipt/fence、把未提交当已提交，也不得由无限私有retry循环隐藏背压。
4. native前置条件必须与真实DB事实检查及写入处于**同一`BEGIN IMMEDIATE`事务**；禁止Python预读后无条件native写入。跨库/跨文件没有单事务，saga和恢复证据留Python。
5. 迁移、健康、预览、临时库和参数化连接均单列；writer归属闭合前均OPEN。每物理库至少补并发写、busy、崩溃恢复和receipt/fence反例；PAC另需附录F的F-PAC-01/02。

### D1. sqlite3.connect全量（40/40；22物理store/参数化连接组）

去向列适用于全组：W归该进程该物理库唯一native writer；R可保留为只读查询或迁native Query，但须共享同一物理身份映射。所有测试/证据OPEN。R只认构造调用本体内字面`mode=ro`；URI变量、外层函数唯一连接、`if read_only`/`if not read_only`分支均不做数据流猜测，因此原4个R已保守改W。

| ID | 物理store / constructor identity | 全部构造点（W/R） | 去向草案 |
|---|---|---|---|
| D-01 | RoutineCoordinator `self.database`（routine-coordinator.sqlite3） | `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:70(W)`；`src/hyprial/biz/impl/routine/execution/coordinator/generation.py:203(W)` | 同一native writer；不可与D-02伪装单事务 |
| D-02 | RoutineStore `database`（routines.sqlite3） | `src/hyprial/biz/impl/routine/storage/store.py:178(W)` | 独立native writer；Routine saga留Python |
| D-03 | ReactionEffectStore `path` | `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:131(W),766(W)` | 单writer；snapshot普通连接也保守列W |
| D-04 | LarkStateStore `path` / readers同path | `src/hyprial/daemon/impl/adapters/lark/state/store.py:71(W)`；`src/hyprial/daemon/impl/adapters/lark/state/readers.py:75(W),145(W)` | 每进程单writer；URI未在调用点显式证明mode=ro，两个reader保守W |
| D-05 | CodexCarrierStore `self.path` | `src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_store.py:74(W)` | 单native writer，carrier receipt/fence不丢 |
| D-06 | IdentityResolver `_legacy_path` | `src/hyprial/daemon/impl/identity/resolver.py:162(R)` | 只读legacy query；writer须由实际legacy owner另行闭合，OPEN |
| D-07 | OverrideStore `self.path` | `src/hyprial/daemon/impl/identity/store.py:52(W)` | 单native writer |
| D-08 | Inbox `database`及两个projection同库 | `src/hyprial/daemon/impl/inbox/service/core.py:66(W)`；`src/hyprial/daemon/impl/inbox/actor/coordinator.py:414(R)`；`src/hyprial/daemon/impl/inbox/authority/read.py:97(R)` | 同一Inbox writer；W窗口与DB顺序按决定⑤，不得拆成竞争owner |
| D-09 | IPC `_RouteIndex(path)` | `src/hyprial/daemon/impl/ipc/wire.py:76(W)` | 独立route-index物理库单writer |
| D-10 | DatabaseHealth `root/name`，`mode=rw` | `src/hyprial/daemon/impl/lifecycle/health/database.py:51(W)` | 参数化健康连接保守列W；映射到每个受检真实库，不能漏别名 |
| D-11 | OrgFS BlobStore `refs_path`（blob-refs.sqlite3） | `src/hyprial/daemon/impl/orgfs/storage/blobs.py:341(W)` | 单writer；与blob文件同串行owner但非单事务 |
| D-12 | OrgFS LocalSpaceStore `_journal_path`（每space journal） | `src/hyprial/daemon/impl/orgfs/storage/store/__init__.py:135(W)` | 每space单writer；CRDT/cache/checkpoint边界OPEN |
| D-13 | agents.sqlite3及所有直接读者 | `src/hyprial/identity/impl/agents/registry/_base.py:625(W)`；`src/hyprial/daemon/impl/pac/contracts/principals.py:111(R)`；`src/hyprial/daemon/impl/pac/graphs/edits.py:95(R)`；`src/hyprial/daemon/impl/pac/storage/migrations/upgrades.py:454(R)`；`src/hyprial/daemon/impl/owner_migration/planning.py:357(R)`；`src/hyprial/daemon/impl/owner_migration/apply.py:136(R),183(R)` | daemon/offline CLI每进程一个writer；保留busy_timeout和真实agents schema |
| D-14 | pac-graph.sqlite3直接连接（另见D2全部57构造） | `src/hyprial/daemon/impl/pac/storage/store.py:275(W),278(W)`；`src/hyprial/shell/impl/cli/commands/workflow/control.py:149(W)`；`src/hyprial/identity/impl/agents/state/worker_state.py:596(R)` | 每进程单native writer；URI/分支参数不猜R；CLI/remote/authority跨进程协议见D0/D2 |
| D-15 | legacy workflows.sqlite3 | `src/hyprial/daemon/impl/pac/storage/legacy.py:251(W),410(R)` | cutover writer与只读archive；sealed写屏障不可绕 |
| D-16 | AssignStore `database` | `src/hyprial/daemon/impl/pac/storage/assignments.py:169(W)` | 单writer；assign release与调用方同事务边界需证据 |
| D-17 | StateDatabase `_path`（desired/journal/shared lifecycle） | `src/hyprial/daemon/impl/state_db.py:144(W)` | 单native writer；AR-49/53等共享，不能各建持久权威 |
| D-18 | AgentSecretStore `self.path`及CLI同库构造 | `src/hyprial/identity/impl/agents/secret_store/store.py:703(W)`；`src/hyprial/shell/impl/cli/commands/agent/secret.py:69(W)` | **daemon与CLI跨进程写者**，不再宣称单writer；每进程一个native writer，保留busy_timeout、权限/私密文件前置条件 |
| D-19 | UserStore `path`（users.sqlite3） | `src/hyprial/identity/impl/users/store/_store.py:61(W)` | 单writer；不同于文件型UserProfileStore |
| D-20 | mobile pairing `_Core._path` | `src/hyprial/shell/impl/gui/adapter/mobile/schema.py:176(W)` | app进程单writer；epoch/lock/revocation提交语义保留 |
| D-21 | owner migration参数化source及MIGRATION_DATABASES | `src/hyprial/daemon/impl/owner_migration/apply.py:32(W)`；`src/hyprial/daemon/impl/owner_migration/planning.py:171(R)` | apply逐真实库单事务/writer；planning覆盖agents/adapters/inbox/lifecycle-operations/routines/workflows/pac-graph，不漏参数化库 |
| D-22 | login preview任意source与临时destination | `src/hyprial/shell/impl/login/preview.py:300(R),301(W)` | source只读；destination为独立临时物理库write-capable，preview不是commit licence |

以上40个`sqlite3.connect`为W=27、R=13；D-18另列的CLI `AgentSecretStore`构造不是第41个connect，不混入该分母。所有位置均为仓库根相对完整path，机械JSON使用同一形式。D2a/D3写者扩展与物理身份归并最晚在**S2.2**完成；此前保持OPEN，不能据D1旧“单writer”表述放行。

### D2. PacGraphStore构造点（57/57；16文件）

分类只认构造参数`read_only=True`为R；缺省、`read_only=read_only`、`read_only=self._graph_authority is not None`全部W。合计**W=26、R=31**。所有W去向均为D-14同一物理pac-graph库的“每进程一个native writer”，所有R为只读query；测试/证据全OPEN。

| 文件（前缀`src/hyprial/`） | W构造行 | R构造行 | 写者/去向重点 |
|---|---:|---:|---|
| `biz/impl/routine/execution/pac_dispatch.py` | 259,517 | 153,428,489,536 | **biz pac_dispatch进程内写者**；start_idempotent与close，归D-14 |
| `biz/impl/routine/execution/source.py` | — | 63 | FilePacJournal只读snapshot |
| `daemon/impl/application/workflows.py` | 301,569 | 128,250,461 | complete_or_fail与`_handle_pac_write`直接写，归D-14 |
| `daemon/impl/composition/links.py` | — | 373 | PacLifecyclePort只读 |
| `daemon/impl/dispatch/remote/__init__.py` | 75,219,265,323,354,692,806,896 | 161,202,485,510,543,949 | **remote写者全集**：初始化、flush、enqueue、pruned、`_stage_offer_direct`692、`_handle_direct`806/896；direct update不得绕graph fence |
| `daemon/impl/dispatch/remote/expansion.py` | — | 138 | expansion preview只读 |
| `daemon/impl/dispatch/workflow_observation.py` | — | 8 | observation只读 |
| `daemon/impl/pac/actors/daemon.py` | 418(参数化),832,893 | 186,478,533 | provision参数化保守W；clock/actor worker进程内写者 |
| `daemon/impl/pac/actors/gc.py` | — | 147 | 当前select显式R；后续mutation不得据此新增隐藏writer |
| `daemon/impl/pac/contracts/delivery.py` | — | 26,47 | delivery判定只读 |
| `daemon/impl/pac/graphs/authority/__init__.py` | 98 | 191,480,536,858 | **graph authority主写者**；restore/effect/scan读者归同一物理库 |
| `daemon/impl/pac/graphs/authority/expansion.py` | — | 129 | prepare_expansion只读 |
| `daemon/impl/pac/graphs/authority/generation.py` | 115,137,213,379,452,521,599 | 187,417 | **graph authority generation写者**：reconcile/effect completion/cleanup/actor failure/_write；全部同事务真实条件检查 |
| `daemon/impl/pac/workflows/runtime/core.py` | 122(参数化) | — | `_open(read_only=...)`保守W；调用方不能只凭名称宣称只读 |
| `daemon/impl/pac/workflows/runtime/types.py` | — | 233 | WorkflowSender当前只读构造 |
| `shell/impl/cli/commands/workflow/control.py` | **87,131** | — | **CLI跨进程写者**：reset与notify_resend；149直接连接保守W，见D-14 |

PAC必须优先闭合三条交错链：CLI `workflow/control.py:87,131`独立进程写、remote `stage_offer`/direct update（尤其692/806/896）写、graph authority及generation进程内写。三者共用真实pac-graph库时，native writer按进程唯一，SQLite负责跨进程串行；每个前置条件与receipt/fence更新在同一`BEGIN IMMEDIATE`中检查真实DB事实。busy必须可重试地 surfaced，不得静默成功、重复通知、丢outbox/receipt或让旧generation覆盖新事实。

### D2a. WorkflowRuntime._open调用方全集（15/15；W=11/R=4）

`core.py:122`只是一个参数化`PacGraphStore`构造，不能替代以下调用方追踪。仅调用本体显式`read_only=True`列R；缺省及参数表达式全部W。最晚**S2.2**把每个W归并到真实进程/物理库writer并补测试nodeid。

| 文件与scope | W调用行 | R调用行 |
|---|---:|---:|
| `daemon/impl/pac/workflows/runtime/core.py` `_GraphWorkflowServiceCore.__init__` | 103 | — |
| 同文件 `_delivery_worker` | 155 | — |
| 同文件 `recover` | — | 215 |
| 同文件 `start`（replay与compile两个点） | 261,294 | — |
| `daemon/impl/pac/workflows/runtime/operations.py` `_tick` | 104 | — |
| 同文件 `_record_harness_outcome_direct` | 169 | — |
| 同文件 `_record_request_pruned_direct` | 247 | — |
| 同文件 `status` | — | 292 |
| 同文件 `list` | — | 424 |
| 同文件 `routine_last_dispatches` | — | 504 |
| 同文件 `_cancel_direct` | 533 | — |
| 同文件 `_stop_worker_direct` | 598 | — |
| 同文件 `_restart_worker_direct` | 673 | — |
| 同文件 `_fail_direct` | 733 | — |

路径前缀均为`src/hyprial/`；15行是调用点数，表按函数合并而非漏项。

### D3. CLI与contract runner扩展写者（只扫描，不修改contract）

R1的writer文件集必须包含D-18 CLI以及`contract/e2e/run-pac-cross-node.py`、`run-outbox-deadletter.py`、`run-hosted-upgrade.py`、`run-pac-public-events.py`。四个runner共有22个直接`sqlite3.connect`/`PacGraphStore`构造，按同一保守规则**W=20/R=2**；即使SQL看似SELECT，普通连接仍W。以下是当前SHA全集与物理库，最晚**S2.2**完成writer归并、隔离证明和测试nodeid；本附录不要求也不授权修改这些runner。

| runner | 全部构造点（W/R） | 实际物理写入/说明 |
|---|---|---|
| `contract/e2e/run-pac-cross-node.py` | `125(W),180(R),314(W),344(W),375(W)` | origin/executor/daemon的`state/pac-graph.sqlite3`；125/314/344/375为write-capable直接物理库连接，180显式mode=ro |
| `contract/e2e/run-outbox-deadletter.py` | `91(W),137(W),338(W)` | 同一`state/inbox.sqlite3`；91 UPDATE+commit，137多处UPDATE/INSERT+commit，338 write-capable读取 |
| `contract/e2e/run-hosted-upgrade.py` | `52(W),60(W),72(W),92(W),122(W两个),187(W)` | agents/lifecycle-operations及`DATABASES`参数化库；122 `source.backup(dest)`的destination是实际物理写入，两个connect均W |
| `contract/e2e/run-pac-public-events.py` | `PacGraphStore 87(W),255(W),374(W),384(W),398(W),417(W)`；`sqlite3.connect 174(R)` | 参数化`database`的pac-graph物理库；87等构造可写fixture/graph，174显式mode=ro |

这些隔离E2E写者不是生产writer豁免，也不得被“测试”标签从物理身份映射删除。若最终架构要求fixture不再绕native writer，迁移去向与测试均为OPEN；当前只证明构造覆盖，不证明运行时闭合。

## A/B/D共用机械重跑规则

### R1. 一次AST生成脚本（内嵌，不创建脚本文件）

在目标SHA的干净worktree根执行。脚本只读源码和git HEAD，不import产品、不访问home、不运行测试；输出稳定排序JSON到stdout，可重定向到worktree外的调查文件后diff。

```bash
uv run --no-project --python 3.12 python - <<'PY'
import ast
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

assert not subprocess.check_output(
    ["git", "status", "--porcelain"], text=True
).strip(), "R1 requires a clean worktree"
ROOT = Path("src/hyprial")
FILES = sorted(ROOT.rglob("*.py"))
CONTRACT_FILES = [
    Path("contract/e2e") / name for name in (
        "run-pac-cross-node.py", "run-outbox-deadletter.py",
        "run-hosted-upgrade.py", "run-pac-public-events.py",
    )
]
SECRET_CLI = Path("src/hyprial/shell/impl/cli/commands/agent/secret.py")
WORKFLOW_FILES = {
    "src/hyprial/daemon/impl/pac/workflows/runtime/core.py",
    "src/hyprial/daemon/impl/pac/workflows/runtime/operations.py",
}
CALLS = {
    "ActorRuntime", "EffectLane", "Thread", "Queue", "Condition", "PacGraphStore",
    "Timer", "ThreadPoolExecutor", "to_thread", "SimpleQueue", "PriorityQueue",
    "deque", "create_task", "ensure_future", "Lock", "RLock",
}
ACTOR_SYMBOLS = {
    "ActorEvent", "ActorEventKind", "ActorHandle", "ActorRuntime", "ActorState",
    "DrainReport", "ExpectedActorError", "STATE_AUTHORITY", "ActorSpec",
    "EffectCompleted", "EffectLane", "EffectRequest", "DEFAULT_POLICIES",
    "EXTERNAL_IO", "PROCESS_LIFECYCLE", "SupervisionPolicy", "GenerationScheduler",
}
LEGACY_SYMBOLS = ACTOR_SYMBOLS | {
    "AdmissionResult", "ActorSnapshot", "ActorBackend", "EffectLaneSnapshot",
    "TimerHandle", "TimerFactory", "EventSink", "FailureSink", "CommandStartedSink",
    "freeze_policy_catalog", "ActorGuardian", "create_backend",
    "handler_factory", "guardian", "scheduler",
}
PAC_PROTOCOL = "src/hyprial/daemon/impl/pac/actors/coordinator/core.py"
B_STDIO = {
    "src/hyprial/daemon/impl/forwarding/controller.py|ForwardingSidecarController.__init__|self._read_stdout",
    "src/hyprial/daemon/impl/forwarding/controller.py|ForwardingSidecarController.__init__|self._drain_stderr",
    "src/hyprial/daemon/impl/harnesses/dsh/process.py|DshHarnessProcess._read_banner|drain_stdout",
    "src/hyprial/daemon/impl/harnesses/dsh/process.py|DshHarnessProcess._read_banner|drain_stderr",
    "src/hyprial/daemon/impl/harnesses/protocol/common.py|PtyHarnessProcess.__init__|self._read_stdout",
    "src/hyprial/daemon/impl/harnesses/protocol/common.py|PtyHarnessProcess.__init__|self._read_stderr",
    "src/hyprial/daemon/impl/harnesses/python_worker/__init__.py|PythonWorkerTurnAdapter._start|self._read_stdout",
    "src/hyprial/daemon/impl/harnesses/python_worker/__init__.py|PythonWorkerTurnAdapter._start|self._read_stderr",
    "src/hyprial/daemon/impl/adapters/lark/worker/process.py|_StdoutDrain.__init__|self._consume",
}
MANUAL = {
    "daemon/impl/lifecycle/lifecycle_coordinator.py": {"_Mailbox", "_recover_loop"},
    "daemon/impl/inbox/actor/worker.py": {"DeliveryIoWorker"},
    "identity/impl/agents/home/effects.py": {"AgentHomeEffectLane"},
    "daemon/impl/dispatch/remote/__init__.py": {"_remote_effect_loop"},
    "kernel/impl/hooks/bus.py": {"_Lane"},
    "daemon/impl/inbox_watch.py": {"_Lane"},
    "daemon/impl/composition/links.py": {"_LinkHookLane"},
    "daemon/impl/adapters/lark/outbound/reactions.py": {"_NativeLane"},
    "daemon/impl/hooks/service.py": {"_ConsumerLane", "_HandlerLane"},
    "daemon/impl/service_connect/io.py": {"_BoundedLane"},
    "daemon/impl/pac/workflows/runtime/core.py": {"_run", "_delivery_worker"},
    "daemon/impl/harnesses/streaming/process.py": {"_run"},
    "daemon/impl/application/messaging/maintenance.py": {"_flush_agent_activity"},
}

def symbol(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None

def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None

def package_for(path):
    relative = path.relative_to(ROOT)
    parents = relative.parts[:-1]
    return "hyprial" + (f".{'.'.join(parents)}" if parents else "")

def absolute_import(node, package):
    if node.level == 0:
        return node.module
    parts = package.split(".")
    if node.level > 1:
        parts = parts[:-(node.level - 1)]
    base = ".".join(parts)
    return f"{base}.{node.module}" if node.module else base

def aliases(tree, package):
    result = {}
    ambiguous = set()
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    def bind(name, value):
        if name in result and result[name] != value:
            ambiguous.add(name)
        result[name] = value
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                bind(item.asname or item.name.split(".")[0],
                     item.name if item.asname else item.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = absolute_import(node, package)
            if module:
                for item in node.names:
                    if item.name != "*":
                        bind(item.asname or item.name, f"{module}.{item.name}")
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name) and node.targets[0].id in called_names:
                bind(node.targets[0].id, dotted(node.value))
    for name in ambiguous:
        result[name] = None
    return result

def resolved(node, names):
    raw = dotted(node)
    if not raw:
        return "unresolved:" + ast.unparse(node)
    root, _, rest = raw.partition(".")
    if root in names:
        if names[root] is None:
            return "unresolved:" + raw
        raw = names[root] + (f".{rest}" if rest else "")
    return raw

def scope(node, parents):
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"

def callback(node, name):
    if name == "Thread":
        target = next((x.value for x in node.keywords if x.arg == "target"), None)
        if target is None and len(node.args) >= 2:
            target = node.args[1]
    elif name == "Timer":
        target = node.args[1] if len(node.args) >= 2 else None
    elif name in {"create_task", "ensure_future"}:
        target = node.args[0] if node.args else None
    else:
        target = None
    return ast.unparse(target) if target is not None else "unresolved"

def explicit_true(node):
    return any(
        kw.arg == "read_only" and isinstance(kw.value, ast.Constant)
        and kw.value.value is True for kw in node.keywords
    )

def direct_sqlite_ro(node):
    return any(
        isinstance(part, ast.Constant) and isinstance(part.value, str)
        and "mode=ro" in part.value for part in ast.walk(node)
    )

def grouped(rows):
    result = defaultdict(list)
    for path, line in rows:
        result[path].append(line)
    return {path: sorted(lines) for path, lines in sorted(result.items())}

calls = defaultdict(list)
concurrency = []
pac_ro, sqlite_ro = [], []
refs = defaultdict(set)
manual, mailbox = [], []
legacy_imports, legacy_definitions, legacy_references, runtime_imports = [], [], [], []
thread_subclasses, spawns, submits = [], [], []
for path in FILES:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = package_for(path)
    names = aliases(tree, package)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    relative = str(path)
    short = str(path.relative_to(ROOT))
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in MANUAL.get(short, set()):
                manual.append({"path": relative, "line": node.lineno, "symbol": node.name})
            if relative.startswith("src/hyprial/kernel/") and node.name in LEGACY_SYMBOLS:
                legacy_definitions.append({"path": relative, "line": node.lineno, "symbol": node.name})
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and relative.startswith("src/hyprial/kernel/"):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for part in ast.walk(target):
                    if isinstance(part, ast.Name) and part.id in LEGACY_SYMBOLS:
                        legacy_definitions.append({"path": relative, "line": node.lineno, "symbol": part.id})
        if isinstance(node, ast.ImportFrom):
            import_module = absolute_import(node, package)
            for item in node.names:
                if import_module.startswith("hyprial.kernel") and item.name in LEGACY_SYMBOLS:
                    legacy_imports.append({"path": relative, "line": node.lineno,
                                           "module": import_module, "symbol": item.name})
                imported_module = f"{import_module}.{item.name}"
                if imported_module.startswith("hyprial.kernel.impl.actor_runtime"):
                    runtime_imports.append({"path": relative, "line": node.lineno,
                                            "module": imported_module, "symbol": item.name})
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.startswith("hyprial.kernel.impl.actor_runtime"):
                    runtime_imports.append({"path": relative, "line": node.lineno,
                                            "module": item.name, "symbol": "*"})
        if isinstance(node, (ast.Name, ast.Attribute)):
            target = resolved(node, names)
            if target.startswith("hyprial.kernel.") and symbol(node) in LEGACY_SYMBOLS:
                legacy_references.append({"path": relative, "line": node.lineno, "target": target})
        name = symbol(node)
        if name in ACTOR_SYMBOLS and path.parts[2] != "kernel" and relative != PAC_PROTOCOL:
            refs[relative].add(name)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                resolved_base = resolved(base, names)
                if symbol(base) == "Thread" or resolved_base.rsplit(".", 1)[-1] == "Thread":
                    thread_subclasses.append({"path": relative, "line": node.lineno,
                                              "scope": scope(node, parents), "class": node.name,
                                              "target": resolved_base})
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for loop in ast.walk(node):
                if not isinstance(loop, (ast.While, ast.For, ast.AsyncFor)):
                    continue
                consumes = []
                for part in ast.walk(loop):
                    if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute):
                        attr = part.func.attr
                        if attr in {"get_nowait", "popleft"} or (attr == "get" and not part.args):
                            consumes.append({"line": part.lineno, "symbol": attr})
                    if (isinstance(part, ast.Attribute) and part.attr == "get"
                            and isinstance(parents.get(part), ast.Call)
                            and symbol(parents[part].func) == "to_thread"):
                        consumes.append({"line": part.lineno, "symbol": "to_thread(get)"})
                if consumes:
                    mailbox.append({"path": relative, "line": loop.lineno,
                                    "scope": scope(loop, parents), "consumes": consumes})
        if not isinstance(node, ast.Call):
            continue
        name = symbol(node.func)
        target = resolved(node.func, names)
        resolved_name = target.rsplit(".", 1)[-1]
        if name not in CALLS and resolved_name in CALLS:
            name = resolved_name
        row = None
        if name in CALLS:
            calls[name].append((relative, node.lineno))
            cb = callback(node, name)
            row = {"path": relative, "line": node.lineno, "scope": scope(node, parents),
                   "symbol": name, "target": target, "callback": cb,
                   "key": f"{relative}|{scope(node, parents)}|{cb if cb != 'unresolved' or name in {'Thread', 'Timer', 'create_task', 'ensure_future'} else target}"}
            concurrency.append(row)
            if name == "PacGraphStore" and explicit_true(node):
                pac_ro.append(f"{relative}:{node.lineno}")
        elif (isinstance(node.func, ast.Name) and node.func.id in names
                and names[node.func.id] is None):
            concurrency.append({"path": relative, "line": node.lineno,
                                "scope": scope(node, parents), "symbol": "unresolved",
                                "target": target, "callback": "unresolved",
                                "key": f"{relative}|{scope(node, parents)}|{target}"})
        if name == "to_thread" and target != "asyncio.to_thread":
            concurrency.append({"path": relative, "line": node.lineno,
                                "scope": scope(node, parents), "symbol": name,
                                "target": target, "review": "non-asyncio-or-unresolved"})
        if (target == "multiprocessing.get_context" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "spawn"):
            spawns.append({"path": relative, "line": node.lineno, "scope": scope(node, parents)})
        if isinstance(node.func, ast.Attribute) and node.func.attr == "submit":
            submits.append({"path": relative, "line": node.lineno, "scope": scope(node, parents),
                            "target": ast.unparse(node.func),
                            "callback": ast.unparse(node.args[0]) if node.args else "unresolved"})
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "sqlite3"):
            calls["sqlite3.connect"].append((relative, node.lineno))
            if direct_sqlite_ro(node):
                sqlite_ro.append(f"{relative}:{node.lineno}")

thread_rows = [row for row in concurrency if row["symbol"] == "Thread"]
thread_sites = {row["key"] for row in thread_rows}
duplicate_thread_keys = sorted(key for key, count in Counter(row["key"] for row in thread_rows).items() if count > 1)
lock_rows = [row for row in concurrency if row["symbol"] in {"Lock", "RLock"}]
duplicate_lock_keys = sorted(key for key, count in Counter(row["key"] for row in lock_rows).items() if count > 1)
unresolved = [row for row in concurrency if row["target"].startswith("unresolved:")
              or (row["symbol"] in {"Thread", "Timer", "create_task", "ensure_future"}
                  and row["callback"] == "unresolved")]
recursive_timers = [row for row in concurrency if row["symbol"] == "Timer"
                    and row["callback"].split(".")[-1] == row["scope"].split(".")[-1]]
workflow_open, writer_connections = [], []
for path in [Path(item) for item in sorted(WORKFLOW_FILES)] + [SECRET_CLI] + CONTRACT_FILES:
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = symbol(node.func)
        row = {"path": str(path), "line": node.lineno, "scope": scope(node, parents),
               "symbol": name, "call": ast.unparse(node)}
        if str(path) in WORKFLOW_FILES and name == "_open":
            row["read_only"] = explicit_true(node)
            workflow_open.append(row)
        is_sqlite = (isinstance(node.func, ast.Attribute) and node.func.attr == "connect"
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "sqlite3")
        if path in CONTRACT_FILES and (is_sqlite or name == "PacGraphStore"):
            row["read_only"] = direct_sqlite_ro(node) if is_sqlite else explicit_true(node)
            writer_connections.append(row)
        if path == SECRET_CLI and name == "AgentSecretStore":
            row["read_only"] = False
            writer_connections.append(row)

actor_rows = sorted(calls["ActorRuntime"])
result = {
    "inventory_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "scope": {"source": "src/hyprial/**/*.py",
              "writer_files": sorted(WORKFLOW_FILES | {str(SECRET_CLI)} | {str(p) for p in CONTRACT_FILES})},
    "counts": {
        **{name: len(calls[name]) for name in sorted(CALLS | {"sqlite3.connect"})},
        "actor_symbol_files": len(refs),
        "legacy_actor_imports": len(legacy_imports),
        "legacy_actor_definitions": len(legacy_definitions),
        "legacy_actor_references": len(legacy_references),
        "legacy_actor_constructors": sum(
            row["symbol"] in {"ActorRuntime", "EffectLane"}
            and row["target"].startswith("hyprial.kernel.") for row in concurrency
        ),
        "actor_runtime_imports": len(runtime_imports),
        "multiprocessing_spawn": len(spawns),
        "thread_subclasses": len(thread_subclasses),
        "thread_stdio_whitelist": len(thread_sites & B_STDIO),
        "thread_whitelist_added": len(thread_sites - B_STDIO),
        "thread_whitelist_missing": len(B_STDIO - thread_sites),
        "duplicate_thread_keys": len(duplicate_thread_keys),
        "duplicate_lock_keys": len(duplicate_lock_keys),
        "unresolved_concurrency_sites": len(unresolved),
        "mailbox_loop_candidates": len(mailbox),
        "executor_submit_sites": len(submits),
        "recursive_timer_sites": len(recursive_timers),
        "pac_read_only": len(pac_ro),
        "pac_write_capable": len(calls["PacGraphStore"]) - len(pac_ro),
        "sqlite_read_only": len(sqlite_ro),
        "sqlite_write_capable": len(calls["sqlite3.connect"]) - len(sqlite_ro),
        "workflow_open_callers": len(workflow_open),
        "workflow_open_read_only": sum(row["read_only"] for row in workflow_open),
        "workflow_open_write_capable": sum(not row["read_only"] for row in workflow_open),
        "contract_writer_sites": len(writer_connections) - 1,
        "contract_writer_read_only": sum(row["read_only"] for row in writer_connections if row["path"].startswith("contract/")),
        "contract_writer_write_capable": sum(not row["read_only"] for row in writer_connections if row["path"].startswith("contract/")),
        "agent_secret_cli_constructors": sum(row["path"] == str(SECRET_CLI) for row in writer_connections),
    },
    "actor_runtime": [{"id": f"AR-{index:02d}", "path": path, "line": line}
                      for index, (path, line) in enumerate(actor_rows, 1)],
    "constructors": {name: grouped(calls[name]) for name in sorted(calls)},
    "concurrency_sites": sorted(concurrency, key=lambda row: (row["path"], row["line"], row["symbol"])),
    "thread_subclasses": thread_subclasses,
    "multiprocessing_spawn_sites": spawns,
    "executor_submit_sites": submits,
    "recursive_timer_sites": recursive_timers,
    "unresolved_concurrency_sites": unresolved,
    "duplicate_thread_keys": duplicate_thread_keys,
    "duplicate_lock_keys": duplicate_lock_keys,
    "lock_sites": lock_rows,
    "actor_symbols_by_file": {path: sorted(names) for path, names in sorted(refs.items())},
    "legacy_actor_imports": legacy_imports,
    "legacy_actor_definitions": legacy_definitions,
    "legacy_actor_references": legacy_references,
    "actor_runtime_imports": runtime_imports,
    "actor_runtime_directory_exists": Path("src/hyprial/kernel/impl/actor_runtime").is_dir(),
    "manual_lane_definitions": sorted(manual, key=lambda row: (row["path"], row["line"])),
    "mailbox_loop_candidates": mailbox,
    "stdio_whitelist_added": sorted(thread_sites - B_STDIO),
    "stdio_whitelist_missing": sorted(B_STDIO - thread_sites),
    "pac_read_only_sites": sorted(pac_ro),
    "sqlite_read_only_sites": sorted(sqlite_ro),
    "workflow_open_callers": workflow_open,
    "writer_connections": writer_connections,
}
print(json.dumps(result, indent=2, sort_keys=True) + "\n", end="")
PY
```

inventory SHA的预期`counts`（已在评审HEAD `1a9e58174eacf108f842d01e5d8233368c18a7ca`的干净克隆逐字执行；其`src/hyprial`与指定contract文件同`INVENTORY_SHA`无diff）为：

```text
ActorRuntime=61, EffectLane=48, Thread=102, Queue=36, Condition=35
Timer=5, ThreadPoolExecutor=8, to_thread=7, multiprocessing_spawn=1
SimpleQueue=1, PriorityQueue=1, deque=45, create_task=11, ensure_future=0
Lock=215, RLock=59, thread_subclasses=0, PacGraphStore=57, sqlite3.connect=40
actor_symbol_files=78, legacy_actor_imports=516, legacy_actor_definitions=31
legacy_actor_references=1022, legacy_actor_constructors=109, actor_runtime_imports=76
thread_stdio_whitelist=9, thread_whitelist_added=93, thread_whitelist_missing=0
duplicate_thread_keys=0, duplicate_lock_keys=27, unresolved_concurrency_sites=332, recursive_timer_sites=0
mailbox_loop_candidates=71, executor_submit_sites=158
pac_read_only=31, pac_write_capable=26, sqlite_read_only=13, sqlite_write_capable=27
workflow_open_callers=15, workflow_open_read_only=4, workflow_open_write_capable=11
contract_writer_sites=22, contract_writer_read_only=2, contract_writer_write_capable=20
agent_secret_cli_constructors=1
```

另`actor_runtime_directory_exists=true`，这是当前旧引擎事实，不是最终门结果。当前Lock/RLock共274点、223个唯一键、27个重复键（51个额外出现次数），所以锁diff必须保留多重集合计数。`mailbox_loop_candidates=71`是结构复核超集，不等于71个已确认mailbox；`executor_submit_sites=158`是所有`.submit`的保守超集，不等于158个已证明Executor。`recursive_timer_sites=0`只覆盖当前直接语法递归，间接递归与跨函数数据流仍OPEN/人工复核；`unresolved_concurrency_sites=332`是动态赋值后被调用名称的保守复核桶，并不证明332个并发站点，但在完整数据流无法可靠判定时必须保守失败/人工分类，**OPEN至S2.0冻结**，不得漏成PASS。A3的`org/network/status`和`session_actor/worker`没有独立命名class定义，分别由Queue27/Thread28与Queue50/Condition51/Thread60的构造全集追踪，故不伪造AST definition行。

### R2. BASE重生成、diff与冻结

1. 最终冻结前等#1892实际合入`dev`，将`INTEGRATED_1892_SHA`记录为**该PR合入dev时`git rev-parse dev`的完整SHA**，不得使用PR中间head、旧review head或随后其他SHA；未发生前保持`BASE_SHA=OPEN`，但按决定①并行开展草案和S1。从该SHA建立干净隔离worktree，R1开头的clean-tree断言必须通过。
2. 逐字运行R1，保存机器JSON；按同一规则重填A1–A5、B-STDIO、D1–D3。AR按path/line重编号并同时保留与旧设计的映射，不能为了维持61而漏掉新增入口。
3. **独立人工表步骤**：重新整理`B_STDIO`与`MANUAL`两份表，逐键核对target/scope和A3结构候选，不沿用`path:line`或类名硬编码结论；整理结果交**研发负责人复核**并记录精确SHA/日期。未完成前两张表均OPEN，最晚S2.0冻结。
4. 用独立实现的第二扫描器对构造点、78文件口径、结构mailbox候选、稳定白名单键、Lock/RLock键及W/R分类做集合diff，禁止只把R1输出与自身比较。Lock/RLock用`Counter(candidate_keys) - Counter(BASE_keys)`仅diff BASE后新增键/出现次数；存量不逐个补裁定，新增锁例外仍做结构检查。特别审查#1892带来的PAC算法/索引/写者变化，以及D2a/D3所有进程；计数可变化，但每个差异都要有去向。
5. 最终`dev → intg`同步时，按实际merge/squash形状枚举组成#1892整合SHA的全部提交，逐提交运行`git show --name-status --find-renames <sha>`，列出所有触及intg已删除或已移动路径的提交。每项必须记录`commit SHA + 原intg路径 + 移植文件/函数 + 至少一个测试nodeid`；该表当前因`INTEGRATED_1892_SHA=OPEN`而OPEN，最晚在**S2.3/S2.5开工前**闭合。#1892新增的测试nodeid必须随实现移植并执行，**不得经附录H映射为“已替代”后删除**；缺任一项即阻塞相应切片。
6. Allen逐项裁定附录A/B/D和新增锁例外后，才在文档写入最终`BASE_SHA`、裁定人/日期、冻结计数。冻结后候选树再跑R1及`tests/kernel/actors/test_legacy_engine_ast_gate.py`：旧引擎门按A6归零，白名单新增集合必须为0；迁移表每行测试与证据不得仍为OPEN。
7. 本文只证明指定inventory SHA的静态覆盖，不证明运行时写者已闭合、性能、迁移等价或E2E通过；这些均保持OPEN/BLOCKED，不用skip或AST计数冒充结论。
