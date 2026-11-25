# 进程语义、声明式监督与monitor/link（2026-10-27增补）

状态：**2026-10-27增补设计稿；未实现、未验收**。所有验收项test/evidence=OPEN。本文不改已裁规则；与既有条文的张力列§8，交Allen裁定，执行者不得据本文自行放宽或改写既有裁定。

Allen 2026-10-27原话：

> 「可以直接改进设计文档，进程语义、监督策略、monitor/link 可以直接加入，独立进程请分析可行性，actor 风格的语法糖先不加」

据此：§1–§3直接加入设计；§4只做可行性分析，不进入实施范围；§5记录不加语法糖。

依据：[目标分层图](actor-rustify-target-architecture-2026-10-24.md)、[实施计划](actor-rustify-implementation-plan-2026-10-24.md)、[接口v0.2](actor-rustify-interface-v02-2026-10-24.md)、[G0决定](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)、[G0清单§A3b](actor-rustify-g0-inventory-2026-10-25.md)、[S2设计§4–5](actor-rustify-s2-design-2026-10-24.md)及actor-split P1接口（`actor-split-interface-2026-10-25.md`，以下称“P1接口”）。

## 0. 动机：两次“一处失败、全体停摆、无人知晓”

| 事故 | 结构 | 结果 |
|---|---|---|
| 10-26 投递tick | `runtime/bridge.py` `on_timer`在一轮内顺序对全部常驻agent调`_dispatch_harness_deliveries`（head `679d6d27c` 约L533–535）。一个agent抛`KeyError((“lark”, True))`，整轮中止 | 全机pi/codex投递停摆约10.5小时，无告警；#1983只在循环内加按agent异常隔离止血 |
| org `publish_self` | `org/network/service.py` `publish_self`（head `679d6d27c` L587–602）对全部org循环，只按org捕获`OrgNetworkError`；其他异常逃出循环，由`ipc.py` `publish_self_for_app`（L451–463）记一条warn后返回空结果 | 一个org失败使全部org不发布，持续数日；只有日志 |

共同结构有三点：

1. 多个独立对象（agent、org）共用一个执行域与失败域。
2. 失败只落日志：无计数、无强度上限、无升级、无运维可见告警。
3. 下一tick重放同一失败，没有隔离，也没有人被告知。

本文对应补三件：分键失败域（§1）、声明式监督与强制告警（§2）、可观察的生命周期事件（§3）。三件都在data-only端口内表达：无callable、无handler注册、不新建Python mailbox，不重启业务handler，不重放已提交工作。

## 1. 进程语义

### 1.1 定义

本设计中的“进程”不是OS进程，也不是Python actor，而是**分键资源（keyed resource）**：

| 属性 | 规则 |
|---|---|
| 身份 | `(host_id, group, key)`。`group`为资源组名（如`delivery.agent`、`org.publish`、`session`），`key`为领域给出的稳定规范身份（agent规范id、org id、session id）。不得用截断展示名、可复用的名字或`(adapter, flag)`类元组；同名agent/adapter历史数据不得映射到同一key |
| 有序提交流 | 每key一条接纳序列；接纳序=执行序 |
| 状态版本 | 每key独立`epoch`与`revision`；跨key无共同revision |
| 失败域 | 每key独立的故障计数、退避、隔离状态（§2） |
| 容量 | 每key独立配额；组配额与全局配额只做上限，不形成跨key队列（§1.4） |

一个key就是一个资源实例。资源**种类**仍是目标分层图所列6–8类（State/Operation/IO等）；分键只增加实例，不增加种类，也不按旧组件类名造资源。

### 1.2 在端口中的表达

- 每key经现有`open(spec)`打开一个资源。spec增加两个owned字符串字段`group`、`key`，以及`supervision`、`links`数据字段（§2、§3）。`ResourceHandle`同步携带`group`、`key`。见§7接口增量。
- 打开可惰性进行（首次有工作时由应用服务`open`）。关闭只在该key无outstanding/in_doubt责任时成功；否则`StopResult.complete=False`，与P1接口§4.4一致。
- key队列位于端口注册表，不在Python服务里。Python服务不得为分键而自建`dict[key, deque]`+分派循环——这正是附录B禁止的私有mailbox。
- tick/Timer只发信号：共享Timer按`(resource, key)`发`timer.fired`数据事件（P1接口§5已规定同一resource+epoch+key只有一个活动timer）。全机tick不再在一个调用栈里依次执行所有key的工作。

### 1.3 顺序

- 同key：接纳序=执行序；默认`per_resource_running=1`（沿用v0.2）。`Snapshot`可绕过写队列读已发布快照，语义同v0.2 §4。
- 跨key：**无全局FIFO**，不承诺跨key相对顺序，也不保证严格轮转；调度交给运行时（同目标分层图§4）。
- `order_barrier`（spec数据，按组声明）：key出现`in_doubt`后，
  - `hold`（有序语义组默认，如投递）：该key的后续未开始工作保持排队，直到应用服务对该ticket `acknowledge`（即原receipt已reconcile）；其他key不受影响。
  - `continue`：后续工作照常执行，适用于各工作互相独立的组（如逐org发布）。
- 重启（§2.2）不改变未开始工作的相对顺序，不把`in_doubt`工作重新排入队列。

### 1.4 容量与背压

| 层 | 规则 |
|---|---|
| key | `outstanding`、`running`、字节预算、观察者数各自独立；满则立即`overloaded`，scope=`key`，无副作用 |
| 组 | `group_outstanding`为上限；同时规定`max_keys`。组满时只拒绝超过自身配额的key与新key，已在配额内的key不被其他key挤出 |
| 全局 | 沿用v0.2/P1全局口径；全局满载拒绝scope=`global` |
| 阻塞执行席位 | 一个key同时占用的阻塞worker≤1；一个组同时占用≤`group_blocking_cap`，且必须**严格小于**共享阻塞池大小。卡住的key只能占住自己的席位，不能耗尽整个池 |

拒绝结果携带scope（`key|group|global`）作为数据。持续满载不是故障（不计入监督强度），但超过组声明的`saturation_alarm_after`后发`saturated`告警事件（§2.4）。配额具体数值沿用原实际口径并逐组标定；本文不给生产数值。

### 1.5 失败如何映射到ticket与key

既有ticket状态（P1接口§4.2转移表）不变；本节只规定key层如何计数。

| 观察到的事实 | ticket状态 | key层效果 | 计入强度 |
|---|---|---|---|
| 业务`failed` outcome（如对端拒绝、校验失败） | completed(failed) | 无 | 否；业务失败是数据 |
| `report_unknown` | unknown | 记录；`order_barrier`生效 | 否（由业务reconcile） |
| 操作deadline到期、执行仍未返回（挂起） | in_doubt | `fault(hung)` | 是 |
| `ExecutionLoss`（thread_exited/executor_closed/dispatch_failed/driver_lost） | in_doubt | `fault(execution_lost:<cause>)` | 是 |
| 应用服务在该key边界内捕获未预期异常（prepare/finalize） | 有活动ticket则in_doubt；无则不涉及ticket | `fault(step_raised)`，经`report_fault`以数据上报 | 是 |
| 通用driver丢失/崩溃 | 活动ticket in_doubt | `fault(driver_lost)`，key重启 | 是 |
| 晚到complete/unknown | 原状态不变，追加late facts | 不清零计数、不重新执行 | 否 |
| 满载拒绝 | 未接纳 | `saturated`计量 | 否 |

`in_doubt`仍不自动重试；key重启也不重试它（§2.2）。晚到事实只补充原operation，与G0决定附录F F-SDK-02一致。

### 1.6 Python应用服务侧硬规则

1. 对多key的工作，一个key的一步就是一次独立的数据交互（submit/await/report）；未预期异常在该key边界内转成`report_fault`数据，不得中断对其他key的处理，也不得只记日志。
2. 阻塞I/O不在服务共享线程上同步执行：走通用IO driver的ticket+deadline，或附录B允许的有界SDK executor+ticket。共享线程上只做有界CPU的prepare/finalize。
3. 端口不回调Python，也不重启Python服务。Python只读取数据事件（`timer.fired`、生命周期事件）并调用明确的业务方法。
4. 已知不可收敛项：Python步骤在GIL下的死循环/长时间CPU占用无法被进程内机制抢占，会拖住共享该线程的所有key。按“已测/未测”纪律，此项为**未消除风险**，只能靠§4的独立进程或代码审查。

## 2. 声明式监督

### 2.1 策略是数据

`SupervisionPolicy`是kernel定义的owned记录，在组上声明默认值、可按key覆盖；不含任何callable。

| 字段 | 含义 |
|---|---|
| `restart` | `permanent`（故障后总是重启）、`transient`（仅非正常退出重启）、`temporary`（不重启，只报down），对应OTP三种重启类型 |
| `intensity` | `max_faults=N` in `window=T`：T内故障计数超过N即超强度 |
| `backoff` | `initial`、`max`、`multiplier`：重启前等待，逐次增长至max |
| `order_barrier` | `hold/continue`，见§1.3 |
| `on_exceed` | 固定为`quarantine`；不得声明为“忽略”或“无限重启” |
| `probe` | `manual`或`half_open(interval)`：隔离后是否定期放行单个探测工作 |
| `escalate` | 组层阈值：T_g内被隔离/重启中的key数≥M，或组内故障总数≥N_g |
| `alarm_route` | 告警路由标识（取值见§2.4）；**告警本身不可关闭** |

数值（N、T、退避参数、M）按组标定，本文不提供生产值；首轮由各组owner提出，作为数据进入任务包评审。

### 2.2 资源“重启”是什么

重启针对通用资源实例，不针对业务handler：

1. key转`restart_pending`，发`restarting`生命周期事件；backoff计时（共享Timer）。
2. 该key活动ticket全部转`in_doubt`（未转者按P1接口§4.2转移表），**不重执行已提交或可能已提交的工作**。
3. 未开始的已接纳工作保留在端口注册表，顺序不变；接纳责任不因重启丢失（v0.2 §6）。
4. epoch+1；旧epoch的回报只能形成`StaleEpoch`，不能结算新epoch ticket。
5. 通用driver以新epoch重新打开：State资源从端口注册表保留的数据继续（P1接口§4.1：driver重启不得清空State）；IO资源重新建立文件/连接/进程托管。**PROCESS资源的重启只重新建立托管与观察，不重新spawn harness**——spawn/respawn是Python业务策略（目标分层图§2表），由服务读到`down`事件后决定。
6. 持久资源从原durable权威重新读取，不使用内存缓存充当恢复事实。
7. 只有在旧执行者已停止或写fence有效时才放行新执行（v0.2 §6）。对进程内仍存活的挂起线程无法证明已停止：此时key保持`restart_pending`并告警，不放行可能形成第二writer的工作（见§4.1，这是独立进程的主要收益点）。
8. 发`restarted`事件（含新epoch）；应用服务据此从原store/receipt做领域reconcile，`order_barrier=hold`时再`acknowledge`放行。

### 2.3 升级链与隔离

```text
key故障 → key重启（backoff）
  └ 超intensity → key隔离（quarantined） + 告警
      └ 组阈值命中 → 组degraded + 告警
          └ 服务的关键组degraded → 服务degraded + 告警
              └ 关键服务degraded或多服务degraded → 节点告警；readiness/doctor显示degraded
```

- **隔离**：`ResourceState=quarantined`。该key新提交被拒绝（数据结果，见§7的`Admission`增量）；已排队未开始工作保留，不丢弃、不执行；其他key继续运行。
- **解除**：`probe=manual`时只由运维命令或应用服务在完成durable reconcile后显式`unquarantine`；`half_open`时端口按interval放行一个工作，成功即恢复running并发`recovered`，失败延长隔离。两种解除都发事件，告警状态随之结束。
- **组/服务/节点层不做一并重启**：默认等价于OTP的`one_for_one`。确需“一个坏、一起停”的，只能用§3的link逐对声明，不提供组级`one_for_all`开关。
- **节点层不自动重启daemon**：只发告警与readiness降级。daemon重启仍由运维或既有机制决定；本增补不新增daemon自杀/自重启。
- “服务”与“关键组”的归属以数据声明在服务启动时登记（组→服务名，是否关键）；端口不理解服务的业务含义。

### 2.4 强制告警事件

升级每越过一级（key隔离、组degraded、服务degraded、节点degraded）以及持续`saturated`，端口必须产生`SupervisionAlarm`：

```text
SupervisionAlarm {
  alarm_id, level: key|group|service|node,
  group, key?, service?, epoch,
  transition: quarantined|degraded|saturated|recovered,
  cause: DownCause（§3.4）, faults_in_window, first_fault_at, last_fault_at,
  raised_at, reminder_seq
}
```

- 告警是状态，不只是事件：进入端口快照（`ResourceSnapshot`已有`quarantined`与`failures_in_window`），在`status`/`doctor`类诊断面持续可见，直到`recovered`或运维确认。
- 同一状态只在转移时告警一次，持续期间按`reminder_interval`复告，不刷屏、也不只告一次后沉默。
- 运维可见路由：至少写日志+诊断快照+一条运维通知。**通知落到哪个渠道（既有availability/系统通知路径、`hyprial-all-member`群或Allen私信）未定，OPEN，交Allen裁定**；在裁定前，日志+诊断快照两项为最低硬门，但不满足“有人被告知”的目标。
- 策略数据不能关闭告警；“只记warn日志”不算告警。

### 2.5 执行者

- 监督由端口/kernel执行：计数、退避、隔离、升级、告警都在端口注册表内基于数据完成。P1中由kernel adapter（Python实现的通用resource adapter）实现同一数据语义；P3由native宿主实现，优先复用Ractor的监督与生命周期通知（实施计划§1第3条，不自建监督树框架；张力见§8-T1）。
- Python业务代码不注册`on_fault`/`on_restart`回调；只读事件、只上报数据（`report_fault`、`report_execution_lost`、`acknowledge`）。
- 现有`ResourceState`（`running|restart_pending|quarantined|stopped`）与快照字段`failures_in_window`沿用P1接口§3.2，本文为它们补足转移规则。

### 2.6 两次事故在本设计下如何被收敛

**投递tick**：组`delivery.agent`，每常驻agent一个key，组声明`order_barrier=hold`、`restart=permanent`。

1. Timer按agent key发`timer.fired`；投递服务对每key独立执行一步。agent X的prepare抛`KeyError`→服务在X的边界内`report_fault(X, step_raised)`，循环继续。
2. 其他agent的key、ticket、阻塞席位与X无共享：投递延迟与基线一致（§6 OTP-P-01）。
3. X按backoff重启；同一坏数据使X在T内连续故障超N→X隔离，产生`SupervisionAlarm(level=key)`。停摆范围从“全机10.5小时”收敛为“一个agent，且在一个T窗口内有人被告知”。
4. 若坏数据源是共享的（例如影响全部lark相关agent），组阈值命中→`delivery.agent` degraded→服务/节点告警。

**publish_self**：组`org.publish`，每org一个key，`order_barrier=continue`。任何类型的异常都只在该org的key内变成`fault`，不会逃出循环使其他org不发布；该org超强度后隔离并告警。若全部org同时失败（共享原因，如身份或网络），组阈值触发组/服务告警，而不是一条warn日志。

两者与#1983的差别：#1983只提供隔离；本设计还提供强度计数、隔离状态、升级与告警，且隔离不依赖每个循环作者都记得写try/except（由§6 fault injection验收）。

## 3. monitor/link

### 3.1 生命周期事件

每key的生命周期转移在端口发布为版本化`DataEvent`，`kind`取：

`lifecycle.started`、`lifecycle.restarting`、`lifecycle.restarted`、`lifecycle.down`、`lifecycle.quarantined`、`lifecycle.recovered`、`lifecycle.stopped`、`lifecycle.execution_lost`。

payload为`LifecycleEvent{group, key, epoch, transition, cause: DownCause, ticket_ids_in_doubt, seq, observed_at}`，全部为owned值。

### 3.2 monitor

- monitor=观察另一个key的生命周期流：`observe`现有语义（after_revision→数据事件），目标为被监视key的`lifecycle`命名空间；组级流可一次观察整组。不新增回调，不新增Python mailbox。
- 监视一个已down/已隔离/不存在的key，第一次`observe`即返回当前状态对应的事件（等价OTP对已死进程monitor立即得到DOWN/noproc），不会因“先发生、后订阅”漏掉（v0.2 §8 lost-wake规则）。
- 观察者落后于保留窗口时返回resync-required，观察者通过`snapshot`取当前`ResourceState`，不默认“无变化”。
- monitor是单向、只读的；被监视方不知道也不受影响。观察者计入观察者预算（v0.2 §3）。

### 3.3 link

link是成对失败策略，在`open`的spec中以数据声明：

```text
LinkSpec { peer: (group, key), on: {down, quarantined, execution_lost}, action: stop_peer|restart_peer|notify_only, direction: one_way|both }
```

- action只能是端口自身的生命周期动作（停止、按peer自身策略重启、仅通知），不能是业务动作；业务补偿仍由Python服务读事件后执行。
- 示例：session key ↔ 其harness worker key，`both`+`stop_peer`：session隔离时worker停止接纳并按drain期限结算；worker的PROCESS托管丢失时session收到`lifecycle.down(cause=link)`，由Session服务决定是否respawn。投递key→harness进程key `one_way`+`notify_only`：进程down时投递key得到事件并按`order_barrier`暂停，而不是继续向已死进程写。
- 传播规则：一次传播携带`cause_id`；同一`cause_id`在每条边上只传播一次，环状link不会放大；传播动作仍走被链接方自己的重启/隔离策略和强度计数。
- link不跨host（node-only，同v0.2 §1）；跨节点只能通过数据面观察对方公布的状态。
- link不提供`trap_exit`式回调；“捕获对方退出”就是`notify_only`。

### 3.4 ExecutionLoss的推广

P1接口的`ExecutionLoss(execution_id, cause, observed_at)`保持为**operation级**记录，不改名不改义。key级引入`DownCause`作为超集：

| DownCause | 来源 |
|---|---|
| `normal` | 显式stop/drain完成 |
| `execution_lost:<thread_exited/executor_closed/dispatch_failed/driver_lost>` | 原ExecutionLoss，原样嵌入 |
| `hung` | 操作deadline到期而执行未返回 |
| `step_raised(code)` | 服务`report_fault` |
| `process_exited(code/signal)` | PROCESS托管观察到的退出 |
| `link(peer, cause_id)` | link传播 |
| `quarantined` | 超强度 |
| `shutdown` | 节点关闭 |

ExecutionLoss仍只使ticket转`in_doubt`；是否进一步形成key故障，按§1.5表判断。

## 4. 独立进程可行性分析（只分析，不进入实施范围）

问题：把风险较高的Python代码（Lark SDK、Casdoor/HTTP客户端、harness流式I/O等）放到由native宿主监督的独立OS进程中执行，作用类似BEAM port。

### 4.1 在CPython GIL下能换来什么

| 收益 | 进程内（现设计） | 独立进程 |
|---|---|---|
| 抢占/超时 | deadline只能把ticket标`in_doubt`；挂起线程不能被杀，继续占用线程、可能仍在写 | 宿主可在deadline后终止进程；执行者**可证明已停止** |
| 第二writer禁令 | 挂起线程可能仍在执行⇒按v0.2 §6不能放行替代执行（§2.2第7步），key只能停在`restart_pending` | 进程退出即满足“旧执行者已停止”，key可按策略重启 |
| 崩溃隔离 | C扩展段错误、`os._exit`、未捕获的线程级致命错误会带走daemon | 只带走该进程；宿主产生`process_exited`故障 |
| GIL/CPU隔离 | SDK或纯Python死循环占GIL，拖慢daemon所有Python线程（§1.6第4条） | 不共享GIL |
| 内存隔离 | 泄漏/大对象计入daemon RSS | 可按进程设上限并在超限后重启 |

注意：终止进程不让外部副作用变得确定。被杀时正在进行的Lark发送仍是`in_doubt`，仍需按原协议reconcile；独立进程只解决“执行者是否还活着”，不解决“对方是否已收到”。

### 4.2 代价

| 代价 | 说明 | 状态 |
|---|---|---|
| IPC序列化 | 请求/结果须是owned值；P1接口§3.0的封闭值全集与递归校验已可作为线格式定义。SDK对象、client、凭据句柄不能跨进程 | 设计上可行；编码成本未测 |
| 启动延迟 | 冷启动解释器并import SDK；需要常驻worker而非逐调用spawn | **未测** |
| 进程数/内存 | 每个常驻worker一个解释器的RSS；按组而非按key设worker，否则随agent/org数线性增长 | **未测** |
| 凭据 | 凭据须在worker内取得（继承受控环境或经fd传递），不得经argv或日志；跨进程后泄漏面增加一个进程 | 需逐SDK设计；现有Lark长连接worker有先例 |
| 调试 | 栈跨进程；日志、trace需带`(group,key,epoch,ticket)`关联 | 需新增诊断口径 |
| 进程托管 | 需PID birth/token fence、孤儿回收、平台差异（附录G两硬格+三后续格） | 复用ProcessCustody；Windows未测 |
| 业务状态 | worker只能是无业务状态的SDK执行者；状态与规则仍在daemon内Python服务 | 规则约束 |

本次没有在本机实测任何启动时间、RSS或序列化开销（工作树无已同步环境，未为此创建）。上表“未测”项不得引用为数字；需在两硬格上实测后再定worker粒度。

### 4.3 候选站点（依G0清单与当前源码）

| 站点 | 现状 | 适合度 |
|---|---|---|
| Lark长连接入站 | **已是**daemon托管的子进程（`adapters/lark/worker/process.py`，有ready-fd与`_StdoutDrain` B-09） | 已具备；纳入§2监督语义即可 |
| Lark出站发送/反应 | daemon内：AR-05 `outbound/gateway.py`、`outbound/sdk_base.py`、`outbound/stream.py`；ML-09 `reactions.py`私有executor lane | **首选**：SDK I/O、无领域状态、已有ticket/in_doubt语义、入站worker为先例 |
| Casdoor/org网络HTTP | daemon内：`org/network/casdoor/client.py`等；AR-60类provider认证 | 次选：SDK边缘清晰；先做§1分键即可解决publish_self类问题 |
| 模型/provider调用 | 主要在harness子进程内（claude/codex/pi/dsh、`python_worker`、`agent_sdk` worker）；provider认证有`provider_auth/helper.py`子进程 | 已隔离；不需要再移 |
| harness流式I/O（ML-15）、pi RPC客户端 | daemon内对已是子进程的harness做stdio/RPC泵 | **不建议**再加一跳Python进程；改由P2的native PROCESS/TRANSPORT driver（异步、带deadline）承接 |
| 投递/inbox业务规则（A3b） | daemon内Python业务 | 不适合：需要业务状态与锁；靠§1分键+§2监督收敛，CPU死循环风险保留（§1.6） |

### 4.4 建议

1. 本轮不把独立进程列入实施；§1–§3在进程内即可收敛两次事故的“全体停摆+无人知晓”，独立进程不是其前提。
2. 若做，先做Lark出站（与ML-09同批），复用入站worker的托管方式；worker按adapter（组）粒度常驻，不按key。
3. harness I/O走native driver，不走独立Python进程。
4. 进入实施前需Allen另行裁定，并先在两硬格实测启动延迟、RSS和单次往返开销。

### 4.5 对P1/P2/P3范围与排期的影响

| 阶段（P1接口§11） | 若采纳§1–§3 | 若再采纳独立进程 |
|---|---|---|
| P1 | kernel adapter实现分键、监督计数/隔离/告警、生命周期事件与link；对应§7接口增量与§6验收。范围增加，排期影响**未估算**，按分片实测速率另估 | 不变（不在P1） |
| P2 | native PROCESS/TRANSPORT driver须按key发生命周期事件、支持托管重建 | PROCESS driver增加“帧化owned值请求/回复”命令；需K评审进入封闭union |
| P3 | native宿主用Ractor监督实现同一数据语义 | 宿主托管SDK worker进程，Lark出站迁入；分发需随wheel携带worker入口并满足附录G |

## 5. 不加actor风格语法糖

按Allen裁定，本轮**不加**：不提供`spawn`/`send`/`receive`/`GenServer`式Python API、`@actor`装饰器、行为基类、`handle_call/handle_cast`回调、`link(pid)`式函数或`trap_exit`。

理由（数据边界保持不变）：

- 语法糖要么注册Python callable进端口，要么在Python重建mailbox+分派循环；前者违反P1接口§9 AST gate与附录C，后者违反附录A/B“删除私有mailbox、不以新Python mailbox替代”。
- §1–§3的语义只需owned数据（spec字段、事件、`report_fault`），不需要新调用风格即可获得隔离、监督与monitor/link。
- 先固定语义与验收，再评估是否需要便利层；届时任何便利层只能是组装数据的普通函数，不得持有执行或回调。

## 6. 验收门（故障注入；test/evidence=OPEN）

| ID | 注入与必须断言 |
|---|---|
| OTP-P-01 | 投递组N个agent key，使key X的prepare抛错/挂起/慢I/O：其余key投递延迟分布与无故障基线一致（同一隔离环境对照），X只影响自身 |
| OTP-P-02 | 同key并发提交：执行序=接纳序；跨key无顺序依赖；重启前后未开始工作顺序不变 |
| OTP-P-03 | key X满载：拒绝scope=key，其他key可接纳；组满时配额内key不被挤出；X挂起占用阻塞席位时其他key仍能获得阻塞席位 |
| OTP-P-04 | `order_barrier=hold`：X出现in_doubt后其后续工作不执行，直到acknowledge；`continue`组不受阻 |
| OTP-P-05 | key身份：同名agent/adapter历史数据、展示名相同但规范id不同，映射到不同key；`(“lark”, True)`类元组身份被拒绝 |
| OTP-P-06 | 复现publish_self：一个org抛非`OrgNetworkError`异常，其他org仍发布 |
| OTP-S-01 | T内注入N+1次故障：第N+1次后key为quarantined，产生一次`SupervisionAlarm(level=key)`，并在诊断快照可见；N次以内只重启不告警 |
| OTP-S-02 | 重启：活动ticket转in_doubt且执行次数不增加；未开始工作保留；旧epoch回报`StaleEpoch`；State数据不丢；PROCESS资源不触发respawn |
| OTP-S-03 | 进程内挂起线程不退出：key停在restart_pending并告警，不放行替代执行（第二writer负对照） |
| OTP-S-04 | 组内M个key隔离：组degraded告警；关键组degraded→服务/节点告警与readiness降级；其他组不受影响，无一并重启 |
| OTP-S-05 | half_open探测成功/失败两路；manual解除只能由显式命令完成；两者都发事件 |
| OTP-S-06 | 告警不可关闭：策略数据声明关闭告警被拒绝；持续隔离按reminder_interval复告；“只记日志”实现使本测试RED |
| OTP-S-07 | 端口内无Python callable：故障路径中端口调用Python业务函数次数为0（沿用P1 AST gate+运行时计数） |
| OTP-M-01 | 先down后monitor：首次observe即得到down/当前状态，无lost wake |
| OTP-M-02 | link `both`+`stop_peer`：session隔离→worker停止接纳并按deadline结算；worker down→session收到`down(cause=link)`；不自动respawn |
| OTP-M-03 | 环状link：同一cause_id每边只传播一次，无放大；传播计入被链接方自己的强度 |
| OTP-M-04 | ExecutionLoss四种cause各自产生`lifecycle.execution_lost`，对应ticket in_doubt，late facts不覆盖 |
| OTP-M-05 | 观察者落后：返回resync-required，snapshot给出当前ResourceState |

每项先在当前实现上RED（如OTP-P-01在#1983前的循环、OTP-S-01在当前“只记warn”路径），再在候选上GREEN；BLOCKED不是PASS。

## 7. 接口增量清单（对P1封闭接口，需K评审）

本文不修改P1接口文档；以下为提议增量，进入P1前须经K评审并更新该文档：

1. `ResourceSpec`增加`group`、`key`、`supervision: SupervisionPolicy`、`links: tuple[LinkSpec, ...]`；`ResourceHandle`增加`group`、`key`。
2. 新方法`report_fault(handle, KeyFault{cause, code, detail, observed_at})`：服务以数据上报key边界内的未预期异常；不带callable。
3. 新方法`unquarantine(handle)`：显式解除隔离（运维或服务reconcile后）。
4. `Admission`增加`quarantined`，或在`overloaded`之外以`PortError.code`区分；二选一由K定。拒绝结果带scope。
5. `DataEvent.kind`增加`lifecycle.*`与`supervision.alarm`；`DownCause`、`LifecycleEvent`、`SupervisionAlarm`、`LinkSpec`、`SupervisionPolicy`为kernel定义的`ValueRecord`形状，纳入P1接口§3.0同一校验。
6. `ResourceSnapshot`增加`restart_count`、`quarantined_since`、`alarm`（或等价字段），口径来自真实计量。

## 8. 与既有设计的张力（未裁定，不在此处修改既有文本）

| ID | 既有条文 | 张力 |
|---|---|---|
| T1 | 实施计划§1第3条、[runtime决定](actor-rustify-runtime-decision-2026-10-24.md)末段：不自建监督树框架；接线需要监督树框架须回评审 | §2的强度/退避/隔离/升级/告警是应用定义的策略数据，应复用Ractor监督实现；若Ractor现成机制不足，补足部分可能被视为“自建监督框架”，需Allen裁定边界 |
| T2 | P1接口§3.1 `ResourceSpec`、§3.2 `Admission`为封闭union；§4.2 `ExecutionLoss.cause`封闭 | §7需要扩展前两者；本文选择不改`ExecutionLoss`而用`DownCause`包住它，但`report_fault`仍是新入口 |
| T3 | P1接口§1/§2：保留现有容量、顺序、外部协议；§4 “P1可为每个现有runtime创建端口实例” | 分键改变了投递/发布的执行拓扑（全机tick→每key），属于行为改变；须按RED→GREEN单独提交，不能混入等价迁移 |
| T4 | v0.2 §3：共享阻塞worker原型值4 | §1.4要求组阻塞占用严格小于池大小；若多组各需席位，原型池可能不足，需要重新标定 |
| T5 | 目标分层图§2：进程托管的“重启/补偿业务策略”留Python；AR-19 forwarding“Python重启策略” | §2.2限定端口重启只重建托管、不respawn，与之相容；但forwarding supervisor现有重启策略与§2的端口策略可能重复，需逐站点裁定哪层负责 |
| T6 | v0.2 §6、S2设计§4：旧执行者停止前不得启动第二writer | 与§2.2第7步一致，但意味着进程内挂起SDK线程会让key长期停在restart_pending；只有独立进程（§4）能解除，需Allen决定是否接受此残余 |
| T7 | P1接口§2第4条：服务状态由单服务对象锁保护 | §1.6要求一个服务对多key独立处理；单锁本身不阻止，但持锁时间若包含某key的长步骤会再度串行化，需在服务设计中逐个核对 |
| T8 | G0清单A3b：投递按agent分键由native端口承接 | 一致；A3b未提监督与告警，已按2026-10-27增补补一行引用本文 |

## 9. 裁定记录（2026-10-27，Allen）

原话：「T1 只用 Ractor 原生能力，不自建，其他按你推荐」。

- **T1**：监督只用 Ractor 原生能力（监督、生命周期通知、Factory），不自建监督树框架。§2 的强度/退避/隔离/升级/告警中，Ractor 原生能覆盖的部分按其机制实现；覆盖不了的部分不在端口内自行补齐，逐项列为 OPEN，由 K 分片评审给出"用 Ractor 原生能力如何表达"或"降级为诊断事件"的结论，不得引入自建监督框架。
- **T3**：全机 tick → 按 key 投递属于行为变化，单独一个先红后绿提交，不混入等价迁移。
- **T2/T4/T5/T7**：交 K 分片（actor-split P1 的 kernel 资源与公共接口分片，唯一 kernel 写者）在实现与独立评审中定。
- **阈值**（N/T/退避、组阈值）：由各分片按实测提出，K 评审。
- **告警**（Allen 原话：「告警应该纳入监督树的设计范畴，具体给谁投递是监督树的配置问题，不应该写在代码中」）：SupervisionAlarm 是监督树的一部分——由监督节点在隔离与逐级上报时产生。投递对象与渠道属于监督树配置（按组/服务/节点层级声明接收方与渠道，如所有者私信、全员群、仅诊断），不得在代码中写死任何接收者；现有 daemon 运维告警通道（`operator_alarm.py`，经 Squire 私信所有者）只作为可配置的一种投递渠道。配置缺失时的缺省行为须为"至少进诊断并可见"，不得静默。
- **独立进程**：维持"只分析、不进入实施范围"。

## 10. 增补案例：adapter "应跑而未跑"（2026-10-27，Allen 指派）

Allen 原话（经 hq-adjutant 转述）：「这看起来像是 actor-rustify 需要在监督树去处理的事情，发给 emergent 让它加入。」
Allen 后续裁定（2026-10-27）：「actor 不管具体的业务，只是负责 actor 的生命周期。」

观测（10-27 晚，`hyprial adapter status allen-squire`）：`status quarantined · online false · desired true · processRunning false`，错误 "Lark adapter allen-squire did not become online"。出站正常（发送由 daemon 直调，不经 adapter 进程），入站失效（无进程接收）。`desired ∧ ¬running` 一旦成立不会自行恢复，直到人工 `hyprial adapter start`。与 §0 两次事故同型：一处失败，停在无人知晓的终态。

### 10.1 边界：监督树只管生命周期

监督树对 adapter 只知道三件事：声明的期望状态（`desired`）、进程是否存活或已退出、adapter 自己上报的就绪信号。它不知道、也不处理 adapter 的业务：Lark 连接如何建立、消息如何收发与投递、出站入站各自是否健康。这些仍由 adapter 自身（Python 服务）负责，并通过就绪/存活信号向监督树报告结果。

### 10.2 纳入本设计的部分（按 §9 T1，只用 Ractor 原生能力）

- 每个 adapter 是一个按 key（adapter 名）的生命周期单元；`desired=true` 即声明 `restart=permanent`。启动后未在时限内上报就绪、或进程退出，按 §2 计入强度、按退避重启，不再停在一次性 quarantined。
- 超过强度即隔离该 key，并按 §9 告警配置发出 SupervisionAlarm；隔离是可见的终态。
- 一致性检查：`desired ∧ ¬running` 持续超过 T_converge 视为生命周期故障进入监督，不只是记录在 `adapter list` 里等人查看。

### 10.3 不纳入本设计的部分

- 出站成功、入站进程缺失时如何判定健康：属于 adapter 业务。adapter 必须在入站不可用时撤回自己的就绪/存活信号；监督树只消费这个信号，不自行解读业务状态。
- adapter 连接逻辑本身的缺陷（如连接挂起不退出、不报错）：属于 adapter 实现，按缺陷单独修复，不由监督树兜底。

### 10.4 验收门（OPEN）

- OTP-S-ADP-01：注入 adapter 启动失败一次 ⇒ 退避后自动重启成功。
- OTP-S-ADP-02：持续失败 ⇒ 达到强度后隔离，并按配置渠道告警恰好一次。
- OTP-S-ADP-03：adapter 撤回就绪信号 ⇒ 监督树按生命周期故障处理（重启或隔离）；监督树代码中不出现任何 Lark 或消息投递相关的判断。

同源待评（交 K 评审，不在本节定案）：`agent secret grant` 对运行中 agent 不重解析 env，"库里已授权"与"进程 env 已生效"之间隔一次 restart。候选形态：grant/revoke 作为该 agent key 的 monitor 事件，按其监督策略触发受控重启或明确报告"已授权未生效"；grant 与 revoke 的生效延迟须成对给出。
