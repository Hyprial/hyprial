# 旧入口追踪附录：61项分层处置建议

S0的主交付是[目标分层图与风险处置](actor-rustify-target-architecture-2026-10-24.md)。本表用于检查遗漏、追踪迁移和删除项，不能代替目标设计；61/61不是S0完成指标。

后续完整替换范围以[S2设计与实施计划](actor-rustify-s2-execution-plan-2026-10-24.md)为准：本表的BLOCKED项必须在S2中关闭或交主团队调整设计，不能留着Pykka、停用功能或排除入口来宣称S2完成。

状态：**61/61入口已有逐项设计建议，尚未完成全部语义/迁移验证**。本表回答每个旧入口的业务留在哪里、使用什么通用能力、删除什么包装、还需证明什么；不把61个入口一对一翻译成61类Rust actor，也不宣称S0整体完成。

核对源码head：`e711499b83575238c750d78b175d519370e0a89f`。产品源码与既有静态清单`bede7aa85`一致，已用git diff确认。行号只用于定位。正在#1892推进的算法/结算改动尚未纳入该源码快照，合入后须复核相关行。

## 1. 目标层次与能力编号

Python应用服务保留领域规则、SDK、协议解释与跨资源saga。native只持有明确的资源/提交边界；普通handler不调用另一资源，也不反向调用Python。

| 编号 | 候选通用能力 | 资源身份与范围 |
|---|---|---|
| S | StateStore：批量Snapshot/CAS | 可序列化状态的明确namespace/key范围；generation和配额不因共享worker混合 |
| D | SqliteResource：Query/Transaction/变化 | 以实际数据库文件确定writer归属；共享数据库不等于合并业务规则，不同文件不伪装一个事务 |
| F | File/Blob/Lease：原子文件、不可变bytes、OS锁 | 按真实文件/root/lease确定所有权；保留fsync/rename/跨文件边界，不擅自更换存储格式 |
| T | TransportSession：声明、put/query/subscribe/reply | 按真实transport session/declaration生命周期；原生句柄不变成Python对象回调 |
| P | ProcessCustody：启动、stdio、观察、停止 | 按受控进程/组的birth与token识别，不按裸PID操作 |
| C | 共享Timer/deadline设施 | 返回数据事件；Python选择业务动作，不向Rust登记Python callable |
| E | 原生I/O执行设施（按需复用Factory） | 只执行可留在Rust的通用I/O；不是把Python SDK包装进Rust worker的借口 |
| O | 操作控制：等待、结果、取消、关闭 | 区分等待者和已接纳责任；durable事实仍以原store/receipt为准 |

这些编号是能力，不是新增actor数量。S/D/F等是否属于同一owner，须由一致性边界决定，不能把能力组合实现成native handler之间的RPC。凡S与D必须共同序列化的路径（如Inbox窗口）都需要先证明方案；不能因为表中写了两个编号就拆成两个独立权威。

资源身份不能直接沿用展示name（例如space_id前12位、path.stem）；使用完整资源scope/key或实际资源身份，短名只作标签。容量列记录源码表达式，未把参数名推定为运行期数值；省略参数时须查ActorSpec定义及生产构造参数。

下表所有目标都是**设计建议**。旧入口与关键代码线索已定位；完整调用链、恢复/并发等价和物理资源别名仍有待验收。“BLOCKED”表示该部分暂不能据表直接移植，不是跳过后宣称完成。

## 01起：Routine与用量

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 01 | ['routine-coordinator'](../../src/hyprial/biz/impl/routine/execution/coordinator/__init__.py#L98) | 128 | Routine创建/删除及补偿应用服务，解释业务步骤和回执 | D(routine-coordinator.sqlite3)+O；需要时S/C | 拆掉专用coordinator actor/私有effect调度，保留Python saga | 与routines.sqlite3是两个库；不得把跨库补偿伪装成单事务；验证重启和重复完成 |
| 02 | ['routine-registry'](../../src/hyprial/biz/impl/routine/execution/service.py#L199) | mailbox_capacity | Routine注册、启停、source/PAC/alarm编排与周期规则 | D(routines.sqlite3)+S/C/O | 保留Python Routine服务，数据库写入和周期设施复用公共资源 | 同routine只允许一个活动effect；跨routine并发、删除预约和晚到完成顺序 |
| 03 | ['usage-cache'](../../src/hyprial/biz/impl/usage_actor.py#L50) | 8 | 用量来源读取、汇总/失效规则 | S（有版本缓存）+O；刷新触发可用C | 删除独立usage-cache actor；Python服务批量更新共享状态资源 | 先确认缓存只是派生数据；保留刷新合并和在途刷新结果归属 |

## 04起：Lark边缘

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 04 | [f'lark-inbound:{name}'](../../src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py#L350) | mailbox_capacity | Lark事件解析、身份/路由和Inbox业务投递 | S/O管理接纳与结果；不把Lark SDK放E | 每adapter inbound actor改为Python入口服务，复用资源配额 | webhook事件顺序、重复投递、满载拒绝及adapter generation；不能绕过Inbox W权威 |
| 05 | ['lark-outbound-gateway'](../../src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py#L86) | capacity | Lark send/reply/upload的SDK适配 | O与明确I/O配额；原生资源操作才使用E | 移除每gateway专用actor；SDK由Python异步应用服务调用 | 保留限流、请求结果和未知发送结果；不能让Rust worker回调Python SDK |
| 06 | [f'lark-reaction-effects:{path.parent.name}'](../../src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py#L733) | mailbox_capacity | reaction的恢复、补偿、late ACK与业务幂等 | D（ReactionEffectStore的SQLite）+O | 改为Python reaction应用服务，共用持久资源；不搬成Rust ReactionActor | store.submit/mark_running/complete/compensate_late_ack不可丢；物理path别名仍需登记 |
| 07 | ['lark-adapter-runtime'](../../src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py#L218) | mailbox_capacity | adapter实例生命周期、reconcile、gateway集合和投递策略 | S+O；进程边缘用P，定时用C | lark-adapter-runtime成为Python管理服务；删除私有runtime/effect lane | 单adapter隔离、spawn回执和旧代完成；SDK客户端不进入native状态 |
| 08 | ['lark-control-io'](../../src/hyprial/daemon/impl/adapters/lark/worker/control.py#L39) | capacity | Lark控制调用与参数/错误映射 | O；按客户端限制Python I/O并发 | 直接复用Python Lark控制服务，删除control-io actor包装 | 追到execute委托，保留控制请求顺序和取消/超时语义 |

## 09起：调度、Dispatch与forwarding

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 09 | ['auto-update'](../../src/hyprial/daemon/impl/autoupdate/actor.py#L88) | 8 | 升级检查、版本判断、下载/安装/重启与通知编排 | C+O；底层文件/进程动作F/P | auto-update成为Python升级应用服务，timer复用共享设施 | 不改升级成功判据或接管其他0.5.5.1修复；一次升级与旧进程/新进程交接 |
| 10 | ['home-lease'](../../src/hyprial/daemon/impl/configuration/home_lease_actor.py#L118) | 8 | home归属/冲突/过期处理策略 | F（锁/原子文件）+P/S/C/O | 拆出通用租约/进程事实，Python保留home策略；删除专用调度层 | 真实OS锁仍是权威；pid/birth/lease token与释放顺序，不能只用内存锁代替 |
| 11 | [bounded_cadence（name）](../../src/hyprial/daemon/impl/correlation/bounded_cadence.py#L75) | mailbox_capacity | 每个cadence回调的具体业务工作 | C+O及资源配额 | BoundedCadenceActor收敛为共享Timer注册；Python消费数据事件 | 原run callable不能交给Rust；coalesce、单次在途、延期与停止须等价 |
| 12 | ['remote-workflow-authority'](../../src/hyprial/daemon/impl/dispatch/remote/__init__.py#L87) | 64 | 远端workflow grant验证、reset/outcome/expansion编排 | D(PAC实际库)+S/O；网络动作T | 删除remote-state专用actor/等待包装，改为Python RemoteWorkflow服务 | 高风险CROSS：_state_call/graph_authority/直接store路径；原receipt去重和超时继续执行必须保留 |
| 13 | ['dispatch-diagnostics'](../../src/hyprial/daemon/impl/dispatch/runtime/diagnostics.py#L51) | capacity | 诊断投影的展示与领域字段解释 | 复用native观测；派生快照可用普通Python对象/局部锁 | 删除dispatch-diagnostics mailbox及独立runtime | 仅删除诊断包装，不删除控制权威；共享worker后CPU口径不能伪造 |
| 14 | ['inbox-hold-refresh'](../../src/hyprial/daemon/impl/dispatch/runtime/holds.py#L50) | capacity | Inbox hold刷新条件与业务调用 | C/S/O；操作仍落Inbox资源边界 | hold-refresh成为Python定时应用服务，删除专用actor/effect线程 | 刷新不得重复授予W席位；stop与在途刷新、无变化不发布的语义 |
| 15 | ['dispatch-offers'](../../src/hyprial/daemon/impl/dispatch/runtime/offers.py#L85) | capacity * 2 | offer筛选、接纳和投递的业务编排 | S/D/O及明确资源fence | dispatch-offers改为Python应用服务；不做Rust OfferActor | slot reservation、失败归还、迟到完成以及同message并发offer |
| 16 | ['dispatch-progress'](../../src/hyprial/daemon/impl/dispatch/runtime/progress.py#L59) | capacity | progress事件归并、对方状态解释与后续动作 | S/D/O；网络发送T | dispatch-progress改为Python应用服务，状态/receipt交公共资源 | 按attempt/generation拒绝旧进度，不能先释放责任再记录完成 |
| 17 | ['dispatch-state'](../../src/hyprial/daemon/impl/dispatch/runtime/state.py#L82) | capacity | DispatchTable/实际值类型的编码与领域façade | S（批量Snapshot/CAS）+O | 直接使用通用StateStore，删除DispatchStateAuthority自有runtime/_Reply | 已有真实P0a子集；仍需全表/真实dataclass/同值CAS版本及batch交错验证 |
| 18 | ['forwarding-sidecar-io'](../../src/hyprial/daemon/impl/forwarding/authority.py#L109) | capacity | forwarding sidecar控制协议与策略适配 | P/T/O；原生I/O可复用E | forwarding-sidecar-io成为Python协议服务，底层资源复用 | 不能把整个backend Python对象塞进Rust；启动/关闭/读写和超时结果的归属 |
| 19 | ['forwarding-supervisor-state'](../../src/hyprial/daemon/impl/forwarding/supervisor.py#L98) | 64 | forwarding重启策略和Python backend对象索引 | S/P/O保存可序列化token、generation与受控进程事实 | 删除独立state actor往返；拆开策略、backend对象和资源状态 | 源码含BEGIN_START/INSTALL/OBSERVE_EXIT真实控制状态，不能当纯诊断直接删掉fence |

## 20起：Harness、Inbox与IPC

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 20 | ['harness-runtime'](../../src/hyprial/daemon/impl/harnesses/actor/actor.py#L255) | mailbox_capacity | Harness启动/恢复/停止及输入输出生命周期应用服务 | P+S/D/O/C | 拆开Python编排、进程资源、持久回执；删除每owner私有runtime | 已追到actor.py:receive，而非静态0分支；核对closing、completion receipt、generation和回放 |
| 21 | [f'interactive-carrier:{name}'](../../src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py#L514) | capacity | 交互carrier的fetch/turn/settlement协议与业务解释 | P/T+S/D(CodexCarrierStore SQLite)/O | 每carrier Python应用服务；字节I/O和状态操作使用通用资源 | fetched/final/kept/turn_started记录顺序、单turn fence、旧carrier完成 |
| 22 | [f'turn:{name}'](../../src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py#L740) | capacity | turn交付/输出判定/完成与取消的业务规则 | S+P/T/O/C | turn actor改成Python Turn应用服务，不创建Rust TurnActor | 同turn顺序、输出parser留Python、超时不等于外部动作撤销 |
| 23 | ['delivery-custody'](../../src/hyprial/daemon/impl/inbox/actor/coordinator.py#L118) | mailbox_capacity | Inbox准入W窗口、receive/ACK/fetch/expiry/ledger业务规则 | 同一已界定owner内的S/D能力+O和scoped变化 | 保留Python Inbox应用服务，持久/窗口状态通过通用资源原子操作 | 高风险：不能把读当纯读搬出权威；W窗口与DB组合顺序尚待证明，不能简单拆成两个可竞争owner |
| 24 | ['fetch-receipt-io'](../../src/hyprial/daemon/impl/inbox/tracking/fetch_receipts.py#L41) | capacity | 构造fetch receipt、业务字段与发送时机 | T/O | 删除fetch-receipt-io独立actor，复用TransportSession的put | 本地发送完成不等于远端持久接收；失效session/generation和重试身份 |
| 25 | ['ipc-requests'](../../src/hyprial/daemon/impl/ipc/ipc_request_owner.py#L60) | capacity * 2 | IPC解析、鉴权、路由和响应编码 | O负责操作等待/结果；必要S只存通用请求状态 | ipc-requests改为Python IPC应用入口；删除其业务决策actor | 鉴权留Python；控制/查询不能被满工作队列困住；断开连接不取消已接纳操作 |

## 26起：生命周期与Org服务

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 26 | ['duplicate-instance-watch'](../../src/hyprial/daemon/impl/lifecycle/duplicate_actor.py#L21) | 128 | 重复实例判断和告警/退出策略 | C+F/P | duplicate-instance-watch改为共享定时任务+Python策略 | 真实lease/process fence不变，不能仅依进程号或计时器决定杀进程 |
| 27 | ['lifecycle-coordinator'](../../src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py#L523) | capacity | 生命周期多步执行、补偿及领域回执解释 | D/S/O与各步骤所需F/P | lifecycle-coordinator成为Python saga服务，删除专用actor包装 | 多资源各有提交点，保留部分提交、补偿和重复完成；无跨资源native调用 |
| 28 | ['maintenance-watchdog'](../../src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py#L248) | 256 | 维护任务健康、卡住判定和处置策略 | C+通用观测/O | maintenance-watchdog成为Python监测服务，共用timer | watchdog自身不应被被监控资源饿死；不改变原关闭/告警时机 |
| 29 | ['session-route'](../../src/hyprial/daemon/impl/network/routing/session_route_coordinator.py#L129) | capacity | session路由绑定、刷新和撤销规则 | S/D/T/O | session-route改为Python路由应用服务，接入公共资源 | 会话epoch、路由身份和失效完成的处理；底层资源共享路径需核对 |
| 30 | ['registry-management'](../../src/hyprial/daemon/impl/operations/management_actor.py#L119) | capacity + 4 | registry管理命令、创建/销毁及跨域编排 | S/D/F/P/O | registry-management改为Python管理应用服务，不保留管理actor | 保留原reserve/submit/ack责任；跨identity/daemon只用公共面，多步失败可恢复 |
| 31 | [f'watchdog-{self._path.stem}'](../../src/hyprial/daemon/impl/operations/watchdog_actor.py#L138) | capacity | 按配置路径的看门/重启业务策略 | C/P/F/S/O | 各watchdog实例变Python策略对象+共享Timer/Process能力 | 实例数量随path变化；重启预算、旧代退出、关闭与计时竞争 |
| 32 | ['org-context-authority'](../../src/hyprial/daemon/impl/org/mesh.py#L158) | _ORG_CONTEXT_CAPACITY | Org上下文、身份/成员/连接对象的业务组装 | S与资源handle；需要的D/T/O按实际资源注册 | org-context-authority改为Python Org应用服务 | SDK/context对象留Python；可序列化事实与活连接分离，发布epoch与生命周期一致 |
| 33 | ['org-peer-access'](../../src/hyprial/daemon/impl/org/network/access/actor.py#L123) | 64 | peer访问策略、ACL/Casbin判断和外部策略同步 | S/D/F保存事实；T/O负责资源动作 | org-peer-access改为Python访问控制服务 | 权限版本与受保护操作的fence需闭合；不能把Casbin规则塞进Rust或只预读后无条件写 |
| 34 | ['org-casdoor-membership'](../../src/hyprial/daemon/impl/org/network/casdoor/sync.py#L213) | 64 | Casdoor SDK调用、成员差异/确认与重试策略 | S/F/O（confirmed为JSON文件） | 删除membership actor/effect线程，Python成员应用服务复用存储能力 | 外部更新与本地confirmed状态不能伪装原子；旧结果、partial failure和回执 |

## 35起：OrgFS

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 35 | ['orgfs-directory-ingress'](../../src/hyprial/daemon/impl/orgfs/composition/directory.py#L124) | self._directory_capacity | 目录事件解析、供应者/成员策略与空间分派 | S/T/O；写入交所属space资源边界 | directory-ingress成为Python目录入口，删除转发mailbox层 | 目录重建/announce顺序与身份；空间入口配额，不对CRDT一致性宣称完成 |
| 36 | ['orgfs-directory-lifecycle'](../../src/hyprial/daemon/impl/orgfs/composition/lifecycle.py#L83) | 64 | 空间join/open/serve/checkout/close的生命周期编排 | S/D/F/T/O | directory-lifecycle成为Python saga服务，复用资源open/close | 多个空间/资源初始化回滚、重复close与在途操作；不整包移为Rust OrgFS actor |
| 37 | ['orgfs-post-commit-effects'](../../src/hyprial/daemon/impl/orgfs/document/effects.py#L178) | _FACADE_EFFECT_CAPACITY | 已提交后的announce/replication、重试和业务完成处理 | T/F/O；提交事实来自所属D/F资源 | post-commit-effects成为Python应用服务，原生I/O可复用E | 不能先announce后commit；跨space/replica/blob的完成与故障责任需逐链核对 |
| 38 | [f'orgfs-space-{space_id[:12]}-state'](../../src/hyprial/daemon/impl/orgfs/document/space_state.py#L199) | _SPACE_STATE_CAPACITY | 空间文档/CRDT操作、业务校验与派生读模型 | 拟S/D/F+O承载受版本保护的数据；CRDT留Python | 拆掉space-state包装；具体Python应用服务和唯一写入边界先验证 | BLOCKED/CRDT：Python Doc到owned数据、journal提交及缓存发布顺序尚未闭合 |
| 39 | [f'orgfs-space-{store.space_id[:12]}-ingress'](../../src/hyprial/daemon/impl/orgfs/mesh.py#L205) | ORGFS_LOG_INGRESS_CAPACITY | 每space mesh envelope解析、校验、去重与导入策略 | T/S/O，持久导入经space写入边界 | space-ingress成为Python网络应用服务，共用TransportSession | 不能直接将Python query/session对象传Rust；保留导入队列配额和generation |
| 40 | [f'orgfs-checkout-{space_id[:12]}'](../../src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py#L118) | _CHECKOUT_CAPACITY | checkout差异规划、路径规则和版本选择 | F/E/O按checkout目标执行有界文件操作 | checkout authority拆为Python规划器与通用文件执行 | 派生文件不能成为第二份CRDT权威；旧快照覆盖、新旧代交错与部分写入恢复 |
| 41 | ['orgfs-blob-authority'](../../src/hyprial/daemon/impl/orgfs/storage/blob_authority.py#L208) | _BLOB_QUEUE_CAPACITY | blob引用、purge与跨space策略/codec | F（不可变bytes）及blob-refs库的D能力/O | 用通用文件/blob与存储组件承接I/O，Python保留引用/清理规则 | BLOCKED/跨提交：blob文件与refs SQLite不是一事务；purge checker跨space读取需版本化而非native回调 |
| 42 | [f'orgfs-replica-{self.space_id[:12]}'](../../src/hyprial/daemon/impl/orgfs/storage/replica_authority.py#L213) | _REPLICA_CAPACITY | replica保留、同步页、frontier及retirement业务规划 | F/T/O，必要数据读取来自source journal能力 | replica actor改成Python复制应用服务+通用object backend | BLOCKED/跨资源：当前source_store/blob_store/RetirementView调用必须拆清；不能在native handler调用另一资源 |
| 43 | [f'orgfs-space-{space_id[:12]}-authority'](../../src/hyprial/daemon/impl/orgfs/storage/space_authority.py#L258) | _SPACE_AUTHORITY_CAPACITY | edit/import/member/frontier/retention的CRDT与领域计划 | D(每space journal)+F/S能力，保留单写入边界 | 将writer机制从Python Space业务实现中抽出；不创建含OrgFS规则的Rust SpaceActor | BLOCKED/CRDT：pycrdt Doc、FULL journal提交点、checkpoint及reply/事件顺序须先设计并验证 |

## 44起：PAC与进程结算

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 44 | ['pac-graph-authority'](../../src/hyprial/daemon/impl/pac/graphs/authority/__init__.py#L150) | mailbox_capacity | PAC图/节点/调度/deadline/alert/remote workflow规则 | D(PAC库)+S/C/O | GraphAuthority改成Python PAC应用服务；DB writer为通用资源 | 核对全部_call与remote-workflow依赖；不得在native内同步调用另一资源；复用算法线新派生索引后再复核 |
| 45 | ['orphan-process-custody'](../../src/hyprial/daemon/impl/processes/orphan_processes.py#L719) | capacity | 孤儿进程的认领/恢复/终止策略与业务回执 | P+F/O（现有CustodyJournal使用atomic JSON） | 通用ProcessCustody承接OS句柄/fence，Python保留adoption规则 | PID复用、进程组、旧token、重启恢复与无关进程保护；不能只迁kill函数 |
| 46 | ['process-io-owner'](../../src/hyprial/daemon/impl/processes/process_owner.py#L163) | capacity | harness输出的业务解释与后续动作 | P（stdio/exit）+O；阻塞原生I/O用E | process-io-owner机制并入通用进程资源，解析留Python | 有限输出buffer、EOF/exit先后、取消和drain；SDK/业务parser不进入Rust |
| 47 | ['harness-result-settlement'](../../src/hyprial/daemon/impl/processes/result_settlement.py#L68) | capacity | harness结果与ACK/回执的领域结算 | D/S/O | harness-result-settlement成为Python结算服务 | 确认/退休/晚到结果责任；与ack-at-harness独立工作线重新对齐，不自动改变完成含义 |
| 48 | ['harness-forward-settlement'](../../src/hyprial/daemon/impl/runtime/settlement.py#L137) | capacity | harness向外转发、业务幂等与结算编排 | D/F/T/O | harness-forward-settlement成为Python应用服务 | 发送与持久回执分阶段；外部结果不明不得盲重放，保留generation和结果查询 |

## 49起：Session、用户与持久状态

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 49 | ['session-authority'](../../src/hyprial/daemon/impl/session_actor/actor.py#L197) | mailbox_capacity | Session注册、绑定、心跳、租期与Agent联动规则 | D(shared lifecycle库)+S/C/O | Session变Python应用服务；复用53的持久资源能力，不复制一个writer | 绑定/Agent incarnation、deferred persistence、效果ACK先后；不能无证明合并全部会话故障域 |
| 50 | ['user-delivery'](../../src/hyprial/daemon/impl/squire/actors/delivery.py#L103) | capacity | 用户地址解析、squire选择和外部投递策略 | S/O；持久结果用51的F能力；网络按T或Python SDK边缘 | user-delivery变Python应用服务，删除专用delivery actor | 投递可能已发生后的unknown、业务去重和代际；Python SDK不伪装native effect |
| 51 | ['user-delivery-ledger-io'](../../src/hyprial/daemon/impl/squire/actors/ledger.py#L83) | capacity | UserDeliveryResult编码、保留/duplicate业务规则 | F（原文件权威的原子读写/CAS）+O | 删除ledger-io actor与_Reply；复用文件资源，不擅自迁SQLite | 已核对是文件store；读/mark_duplicate的retention副作用、receipt查询和落盘顺序 |
| 52 | ['user-profiles'](../../src/hyprial/daemon/impl/squire/actors/profile.py#L121) | capacity | profile字段、owner优先级、绑定与RuntimeCapability规则 | F（UserProfileStore原文件）+S/O派生读模型 | user-profiles变Python应用服务，删除storage effect/mailbox包装 | 该ProfileStore不同于users.sqlite3的UserStore；保留no-op版本、原子保存和歧义检查 |
| 53 | ['state-persistence-authority'](../../src/hyprial/daemon/impl/state_persistence/authority.py#L104) | mailbox_capacity | Desired/Session/Harness模型校验、注册SQL和领域投影解释 | D（desired_store.state_db与journal共用）+O/scoped变化 | 单库通用writer承接持久操作，Python留模型/计划，不造Rust领域命令枚举 | 同一StateDatabase已核对；实际commit、部分失败、late result、snapshot与通知顺序 |

## 54起：Transfer与Transport

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 54 | ['transfer-p2-authority'](../../src/hyprial/daemon/impl/transfer/p2/actor.py#L110) | capacity | P2 transfer的准备/认领/提交/补偿状态机 | S/D/F/P/T/O按原资源边界 | transfer-p2改为Python saga服务，原子步骤使用通用资源 | 跨节点/目录/进程没有单事务；保留原custody、重启和旧generation拒绝 |
| 55 | ['transfer-source-effects'](../../src/hyprial/daemon/impl/transfer/p2/source/runner.py#L41) | capacity | source transfer的业务步骤、manifest与补偿 | F/P/T/O；原生I/O用E | 移除source-effects专用actor，Python编排多个明确资源步骤 | 目录副作用与回执、边界检查、失败后的恢复/删除不能越权或丢责任 |
| 56 | ['presence'](../../src/hyprial/daemon/impl/transport/presence_actor.py#L79) | capacity | presence的online/offline、delete确认和刷新规则 | S/T/C/O | presence变Python应用服务，共用transport与timer | 不是无状态广播：superseded DELETE、旧generation、accepted责任和unknown投影必须保留 |
| 57 | [f'query-io:{name}'](../../src/hyprial/daemon/impl/transport/query_actor.py#L53) | capacity | query业务handler的选择、请求解释与回复内容 | T拥有原生query句柄，O管理reply/关闭 | query-io actor删除，Python生成回复data；native transport只执行reply | 当前有Python query.reply和_handler callable；必须改为owned query ID/数据，不能把callable放Rust |
| 58 | ['transport-session'](../../src/hyprial/daemon/impl/transport/session_actor.py#L403) | capacity | transport配置、应用声明内容和领域事件解释 | T（session/declarations/put/query/subscribe）+O | 抽出通用TransportSession，Python保留策略；共享设施不再每业务建IO actor | session/声明句柄生命周期、关闭与callback-data桥；本地put成功不等于业务送达 |

## 59起：Identity与日志

| ID | 旧注册点（链接到源码） | 旧容量源码表达式 | Python保留 | native能力 | 旧包装如何处理 | 必须补的证明 / 风险 |
|---|---|---|---|---|---|---|
| 59 | ['agent-registry-authority'](../../src/hyprial/identity/impl/agents/actor/_core.py#L251) | mailbox_capacity | Agent创建/销毁/绑定/迁移/权限与home生命周期规则 | D(agents库)+S/F/P/O | AgentRegistry改为Python应用服务/明确saga，事务与资源动作通用化 | 不是全搬Rust RegistryActor；数据库约束、home多步事务、incarnation/receipt及与Session交接 |
| 60 | ['provider-auth'](../../src/hyprial/identity/impl/provider_auth/actor/_authority.py#L113) | self._decision_capacity | provider鉴权、交互挑战、恢复/重试和通知策略 | S/F/P/C/O；SDK仍Python | provider-auth变Python认证应用服务，helper进程和定时使用公共资源 | 不得传SDK/凭据对象作native callable；challenge/restore token、旧完成和用户交互顺序 |
| 61 | ['log-writer'](../../src/hyprial/kernel/impl/log/writer.py#L49) | capacity | 日志内容构造、脱敏与策略 | F/E（按文件顺序append/flush）+必要O | 删除专用log-writer actor；native仅追加opaque bytes | 保留每文件顺序、轮转/flush/close和溢出行为；不能简单并发写或把丢日志当成功 |


## 2. 已核对的资源关系与反例

这些是源码事实，不是读取生产目录或数据库的结果：

| 关系 | 已核对事实 | 对分层建议的影响 |
|---|---|---|
| desired state / lifecycle journal | `state_persistence/authority.py`把`desired_store.state_db`传给`_LifecycleStore` | 53应只有一个对应库的writer；49/20/27是业务客户，不再各造持久权威 |
| Routine两套持久记录 | coordinator使用`routine-coordinator.sqlite3`；execution service使用`routines.sqlite3` | 01/02不能因同属Routine就宣称同一SQLite事务，补偿责任留Python |
| OrgFS space journal | LocalSpaceStore使用`orgfs/spaces/<space>/journal.sqlite3`，synchronous=FULL，并持有pycrdt.Doc缓存与checkpoint文件 | 38/43不能当普通SQL CRUD移植；CRDT/commit/checkpoint边界标BLOCKED |
| Blob与Replica | BlobStore有`orgfs/blob-refs.sqlite3`与blob目录；FsReplicaBackend是`orgfs/replica/<space>`对象文件 | 41/42不等于“再开两个同库SqliteResource”；跨文件/库/回调先拆清 |
| UserProfileStore与UserStore | 52构造的是文件型UserProfileStore；另一个UserStore才是users.sqlite3 | 不能因名字都含user就把profiles并进用户数据库 |
| UserDeliveryLedger | 51委托的UserDeliveryLedger按path加载/保存结果文件及retention | 先保持文件权威和幂等语义，不借Rustify偷偷迁库 |
| AgentRegistry | registry/_core.py构造真实SQLite连接，路径是constructor参数 | 59的业务规则留Python，事务能力归其实际agents库；不能并入session库靠名字猜测 |
| harness-runtime动态入口 | contracts.py的generation wrapper转到actor.py:receive，再进入lifecycle/dispatch | 静态0分支不是空逻辑，20的生命周期/完成回执/fence必须逐支核对 |
| forwarding supervisor | _apply包含安装token、generation、early exit和restart状态 | 19只能移除重复机制，不能把真实控制状态当诊断缓存删掉 |
| remote workflow / query IO | remote有_state_call、graph_authority与直接store路径；query-io持有query对象并执行Python_handler | 12/44是调用环重点，57必须转换为句柄/数据方案，不能原样交给Rust |

## 3. 怎样使用这张表

1. 先用17做完整内存资源切片；13、11、24、61分别验证诊断包装、定时、transport发送与按文件顺序I/O的消除方式，但依赖的资源实现必须先成立。
2. 53先建立真实共享持久边界，再迁49/20/27等Python编排客户；不在native里嵌套调用资源来保留旧调用链。
3. 12/44先画同步等待与回执图；35–43先解决CRDT、文件/库提交和回调数据化，保持BLOCKED可见。
4. 其他领域服务按本表拆出规则/SDK/IO；凡依赖Python库的执行都留Python，不能为了删EffectLane改成Rust回调Python。
5. 所有迁移仍补齐真实值类型、状态/提交/通知顺序、容量与故障测试。当前只完成逐入口**建议覆盖**，未完成全部shared-DB实例核验、旧代码删除清单和S1前置ADR；不能标S0完成或批准全量切换。

基础证据：原handler索引、[消息与语义边界](actor-rustify-message-catalog-2026-10-24.md)、[S0/S1交付定义](actor-rustify-s0-s1-deliverables-2026-10-24.md)。（复现材料已于 2026-11-06 移出仓库，原件见 `cb2584a8b` 及之前的历史）
