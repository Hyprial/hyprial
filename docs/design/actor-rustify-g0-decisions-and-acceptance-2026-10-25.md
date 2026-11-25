# G0决定与验收附录

状态：**实施规则已获Allen裁定；最终BASE_SHA和基线实测清单仍OPEN，不可设置/goal**。2026-10-25，Allen批准②–⑩、⑫–⑭、⑪流程及①“并行准备、合入后冻结”，裁定所对输入head为`e80a3347940d7e745015bae0287bacdb96f9968b`。第四轮评审head`1a9e58174eacf108f842d01e5d8233368c18a7ca`后，再按Allen新指示落实R1平台两硬格+三后续格、R2单一总goal连续推进，并落实R3内存ticket和R4 CI-I节点。本文件是规则记录，不代表实施或验收已经PASS。

附录A/B/D在[G0清单与写者附录](actor-rustify-g0-inventory-2026-10-25.md)。附录A/B/D/H仍须在最终BASE重生成/实测后交Allen复核并写入精确SHA；该外部等待只挡S2.6，不挡S1–S2.5非重叠工作。其他附录沿用上述规则裁定。执行者不得为满足判据自行删行或改宽规则。

## 0. 十四项实施前决定

| 编号 | 决定 | 状态与下游门 |
|---|---|---|
| ① 基线 | **不等待#1892即可开展G0全部任务包、S1及S2非重叠工作。** #1892合入`dev`后立即执行`dev→intg`，以当时`dev` head为最终`BASE_SHA`，重生成附录A/B/D/H；重叠迁移须整合#1892或由两线owner书面协调；S2.6/S2.7及最终验收必须基于该BASE | **规则APPROVED；最终BASE_OPEN**。只挡最终BASE、S2.6/S2.7及未协调的重叠迁移，不挡S1–S2.5其他节点 |
| ② 旧引擎范围 | 附录A的全部EffectLane和手写mailbox/lane均属旧引擎；附录B仅允许对象内部锁、受限SDK executor及逐项列名的纯I/O线程；新增“队列+线程+命令分派”一律禁止 | **APPROVED**；实施门：S2.0、S2.6 |
| ③ 写者政策 | 每进程每物理库只有一个native writer；CLI等跨进程写者保留SQLite协议与`busy_timeout`；`SQLITE_BUSY`为可重试错误；前置条件与真实库事实检查处于同一`BEGIN IMMEDIATE` | **APPROVED**；实施门：S2.2、PAC/remote迁移 |
| ④ OrgFS模型 | 选择**(A) Python space锁 + native事务原语**；锁覆盖prepare→一次或多次native事务→缓存/projection发布。授权、CRDT、多事务edit留Python；blob文件与refs归同一串行owner | **APPROVED**；实施门：S2.3。不选(B)，因为(B)会把授权、CRDT与多段业务提交搬进native条件，扩大风险和改动面 |
| ⑤ Inbox W窗口 | Python锁持有内存状态并使用native DB原语；不建立含Inbox规则的native资源 | **APPROVED**；实施门：S2.5 Inbox/IPC |
| ⑥ PAC等待图 | G0画出PAC/remote-workflow等待图及25+个`_call`点去环方案；覆盖满池、旧代完成、超时重放、shutdown | **APPROVED**；实施门：S2.5 PAC/remote-workflow |
| ⑦ 等待API | 同步等待并释放GIL为主；asyncio桥仅用于原本async的adapter；不把daemon改成asyncio | **APPROVED**；实施门：S1-B/C |
| ⑧ 平台矩阵 | **Allen 10-25新指示：本goal硬格仅darwin/arm64（macos-host原生）和linux/amd64（Ubuntu runner上的manylinux Docker）。** darwin/amd64、linux/arm64、windows/amd64移入多平台分发后续门，保留原交叉构建与目标机/Rosetta/qemu证据口径；#1897合入dev或任何包含它的发布前，后续三格必须全部PASS，此前dev/internal/stable不得包含Rustify。Cargo使用固定vendor；允许在macos-host安装固定Rust工具链 | **APPROVED（R1）**；本goal两硬格实测OPEN，后续三格不挡功能替换但硬挡dev合入和发布 |
| ⑨ CI修改规则 | 采用一次性授权：由单独intg PR `CI-I`实施附录I封闭规则，不再逐PR申请。CI-I交付机械审计：`NATIVE_ORIGINS`、`registry-union.json`、`HISTORICAL_ROWS`只接受新增条目或原样移入，workflow只增不删；其他diff一律FAIL | **APPROVED（R4）**；CI-I是S1-D/E和S2.6前置，超范围修改仍须另行Allen裁定 |
| ⑩ 硬E2E | 附录E全部场景均为硬门，包括real-harness loopback、hosted suite、真实Lark测试App、Casdoor及Claude/Codex resident。凭据按HQ规定经协调者申请并使用隔离home；缺条件为BLOCKED而非PASS | **APPROVED**；不得用mock/skip替代真实硬门 |
| ⑪ 基线例外 | 在`BASE_SHA`隔离环境完整跑`tests/`两次；两次均失败才进入附录H候选，单次失败记flaky；由Allen逐条裁定在本goal修复或保留FAIL/NOT_RUN并指定归属 | **流程APPROVED；清单OPEN**。等待最终BASE实测和逐项裁定；只挡S2.6 |
| ⑫ contract变化 | 同值CAS是否增version、`threadCpuMs`口径分别独立RED→GREEN并附Contract-Impact；不在迁移提交中偷改 | **APPROVED**；实施门：相关切片和S2.7 |
| ⑬ 解释器 | CI跑标准GIL CPython 3.12与3.14；free-threaded不支持并明确报错；abi3产物不得声明支持3.13t/3.14t | **APPROVED**；实施门：S1-D/E |
| ⑭ 分支纪律 | 每周至少一次`dev`→`intg/actor-rustify`并通过base-freshness；修改并行团队目录前公布附录J冻结窗口；内部PR只进intg | **APPROVED**；贯穿所有实施与最终交付 |

## 0.1 第四轮L1–L7实施硬要求索引

这些事项不阻塞G0/S1启动，但以下最晚节点是硬门，不是可选建议；详细机械规则见[G0清单与写者附录](actor-rustify-g0-inventory-2026-10-25.md)及[依赖图](actor-rustify-parallel-delivery-2026-10-25.md)。

| 项 | 必须交付 | 最晚节点 |
|---|---|---|
| L1 | `tests/kernel/actors/test_legacy_engine_ast_gate.py`执行A6；78文件口径排除PAC自有Protocol；模块限定符号；Lock/RLock只diff新增 | S2.0冻结前 |
| L2 | Timer/Executor/to_thread/spawn/SimpleQueue/PriorityQueue/deque/create_task等进入A4/B；PAC PriorityQueue等结构mailbox进入A3 | S2.0冻结前 |
| L3 | 白名单键为文件+所在类/函数+target；覆盖别名、Thread子类、Executor/Timer/create_task循环；负对照证明不可绕过 | S2.0冻结前 |
| L4 | 补WorkflowRuntime._open全部调用方、AgentSecretStore CLI跨进程写者及contract/e2e四个runner写者 | S2.2前 |
| L5 | R1 clean-tree断言、保守只读判定、B_STDIO/MANUAL人工表重新整理并复核 | S2.0前 |
| L6 | 使用#1892合入dev时的SHA；dev→intg移植表逐项写位置和测试nodeid；#1892新增测试不得删除 | S2.3/S2.5开工前 |
| L7 | ⑫依赖连接S2.1、相关领域切片和S2.7 | 已落入依赖图，实施时审计 |

## 附录C：分层静态门白名单

1. `crates/hyprial-core`的正常依赖树不得含`pyo3`；`cargo tree -p hyprial-core -e normal`为机械证据。
2. `crates/hyprial-py`中`call`、`call1`、`call_method`及存储的`Py<PyAny>`只允许出现在未来的`crates/hyprial-py/src/await_bridge.rs`。该文件仅可做owned值转换、等待完成通知和异常映射，不得按领域消息分派。新增文件或符号必须先由Allen修订本附录。
3. `hyprial.kernel`公共面签名不得含`Callable`参数；只有`src/hyprial/kernel/impl/`内的native adapter可导入`hyprial._core`。
4. core模块、类型和消息枚举的领域禁用词至少为：`Inbox`、`PAC`、`Workflow`、`OrgFS`、`Lark`、`Casdoor`、`Harness`、`Session`、`Agent`、`Dispatch`、`Routine`、`Settlement`。AST扫描同时比较大小写与 snake_case 形式；通用`session`若用于transport必须改成语义无歧义的`transport_connection`，不得借例外藏领域逻辑。
5. 当前无实现，故本附录是静态门草案而非PASS证据。

## 附录E：硬E2E清单（Allen已裁定⑩）

| ID | 场景 | 隔离与证据 | 状态 |
|---|---|---|---|
| E-01 | installed-wheel真实CLI/IPC启动、状态与关闭 | 干净隔离home/socket/port/db；正式wheel无pykka；native操作轨迹 | **硬门APPROVED；实测OPEN** |
| E-02 | real-harness loopback完整send/turn/fetch/ACK | 假provider但真实harness进程与正式native路径 | **硬门APPROVED；实测OPEN** |
| E-03 | hosted suite | 使用既有隔离hosted环境，不替换`hyprial._core` | **硬门APPROVED；实测OPEN** |
| E-04 | 真实Lark测试App出站/入站/反应与unknown结果恢复 | 测试App凭据经协调者申请；隔离org/home；不接触生产App | **硬门APPROVED；实测OPEN** |
| E-05 | 真实Casdoor成员同步、confirmed状态与部分失败 | 专用测试租户；凭据经协调者申请 | **硬门APPROVED；实测OPEN** |
| E-06 | 真实Claude/Codex resident进程生命周期与恢复 | 受控测试账号/隔离home；birth、PID复用、关闭证据 | **硬门APPROVED；实测OPEN** |
| E-07 | 跨进程PAC CLI+remote+authority并发写入 | 三个独立进程；同一隔离库；receipt/fence不丢 | **硬门APPROVED；实测OPEN** |

全部场景均为完成硬门。凭据、测试租户或其他隔离条件未供给时为BLOCKED；不得用mock、skip、单元测试或编译成功替代。

## 附录F：必须存在的反例测试

下列ID是验收索引；实现时在附录D对应行填写真实nodeid和精确head证据。故障点沿用旧实现`_fault(...)`语义做对照，不能仅验证最终文本一致。

| ID | 反例与必须断言 |
|---|---|
| F-ORG-01 | 成员移除与旧成员edit并发：prepare读取meta M后meta推进到M+1；提交必须以space meta frontier+doc state vector拒绝旧授权，不得TOCTOU提交 |
| F-ORG-02 | 未注册writer首次edit：writer注册提交后、doc提交前崩溃；重启后原intent可恢复/安全重试，无重复writer或丢update |
| F-ORG-03 | 一次edit需要2–3个journal事务：在每个事务边界崩溃；space锁不伪装跨事务原子性，恢复按原协议继续且无双写 |
| F-ORG-04 | `after_journal`故障：SQL已提交、`doc.apply_update`/缓存/checkpoint未完成；禁止另一次import/edit穿过，重启后缓存与journal一致 |
| F-ORG-05 | `before_state`故障：projection/frontier/`_drain_pending`未完成；重启后提交事实、缓存、通知与后续edit顺序正确 |
| F-ORG-06 | blob put/delete交错：文件已存在→delete检查无ref→put pin→delete unlink的序列不得产生悬空ref；文件与refs在同一串行域 |
| F-ORG-07 | purge projection发布前后并发put：`global_checker`改为版本化projection数据；旧projection不得清理新pin对象 |
| F-ORG-08 | import重放与本地edit seq冲突：原seq/causal/fence约束保持，不盲重放、不覆盖新frontier |
| F-ORG-09 | replica `prime_from`读取source journal与本地提交并发：native handler不跨资源RPC，Python步骤看到一致快照并可恢复 |
| F-PAC-01 | CLI `workflow control`子进程、remote `stage_offer`、graph authority tick三方并发：`BEGIN IMMEDIATE`内检查事实，busy可重试，receipt/fence不丢 |
| F-PAC-02 | PAC/remote等待图满池、旧代完成晚到、超时后重放、shutdown：无持锁等待自身后续动作，无死锁或重复提交 |
| F-SDK-01 | **进程内**SDK线程在complete/unknown回报前死亡：当前epoch的native内存ticket转`in-doubt`并发数据事件；业务按原协议reconcile，不自动重试 |
| F-SDK-02 | **进程内**SDK超过deadline但线程仍存活：ticket转`in-doubt`；晚到complete只记录原operation结果，不推翻reconcile事实；旧epoch回报不得结算新epoch ticket |
| F-SDK-03 | await等待者取消/断开：只撤观察，不取消已接纳SDK工作或释放配额 |
| F-SDK-04 | daemon重启建立新epoch后，O没有旧ticket也不执行恢复扫描；业务从原durable store/receipt识别未结算操作，按原协议置`in-doubt`并reconcile，不新增job库、不盲重试 |
| F-RES-01 | 满载、等待者消失、取消、部分提交、worker与daemon重启、PID复用、慢订阅者及关闭期限逐项保留责任、fence和receipt |
| F-DIST-01 | 删除已安装`hyprial/_core*`：daemon非0退出或not-ready并给稳定错误码，不得降级成功 |
| F-DIST-02 | native资源打开失败：`disabledCapabilities`不得被吞成“启动成功”；正式wheel E2E事件计数为0且capability集合与基线diff为0 |

## 附录G：平台与构建证据（Allen R1新裁定）

### G.1 本goal段（两个硬格）

| 目标 | 构建方式 | 运行证据 | 供给/实测状态 |
|---|---|---|---|
| darwin/arm64 | `macos-host`原生构建，Rust target `aarch64-apple-darwin`，`MACOSX_DEPLOYMENT_TARGET=13.0`；允许安装固定Rust工具链 | 原生uv安装、import、公共调用；tag `macosx_13_0_arm64` | 本goal硬门；实测OPEN |
| linux/amd64 | 现有Ubuntu runner上的manylinux_2_17兼容Docker容器原生构建 | 同一manylinux Docker内uv安装、import/公共调用、动态依赖审计；tag `manylinux_2_17_x86_64` | 本goal硬门；实测OPEN |

### G.2 多平台分发后续门（dev合入/发布硬门）

这三格不阻塞本goal的功能替换与两硬格验收，但**#1897合入dev或任何包含它的发布之前必须全部PASS**。在此之前dev、internal和stable不得包含Rustify；缺`_core`的目标平台不得靠fallback安装。

| 目标 | 构建方式 | 运行证据 | 状态 |
|---|---|---|---|
| darwin/amd64 | 优先原生x86_64 runner；缺失时在arm64交叉构建x86_64 | 原生运行，或x86_64目标机/Rosetta冒烟；按实际路径记录 | 后续门；实测OPEN |
| linux/arm64 | 优先原生runner；缺失时manylinux交叉构建 | 原生运行，或qemu-user import/公共调用；按实际路径记录 | 后续门；实测OPEN |
| windows/amd64 | 优先原生Windows runner；缺失时cargo-xwin+PyO3 import-lib交叉构建 | 原生运行，或Windows目标机/VM的pyd安装、调用、关闭；按实际路径记录 | 后续门；实测OPEN |

每格还必须验证wheel tag/ABI/产品版本/API schema一致。`editable`含uv cache-keys重建、worker镜像、公网快照`uv build`均为独立硬门。`HYPRIAL_TAILCAT_ALLOW_MISSING=1`不得生成可安装纯Python wheel。交叉编译成功不等于运行PASS，但上述已裁目标环境冒烟可记PASS。Cargo使用固定vendor，固定工具链和缓存key进入同一目标描述；vendor缺失、依赖不可达或运行证据缺失时对应格BLOCKED。

## 附录H：基线例外与测试迁移映射

状态：**OPEN**。当前没有`BASE_SHA`两次完整测试的JUnit，不填写“无例外”。最终表必须逐行含`nodeid`、两次结果、分类（baseline-fail/flaky）、后续归属、Allen裁定与SHA；失败集合只能是该表子集，skip集合不得超过基线集合，候选diff不得新增skip/xfail/importorskip。

删除测试映射表逐行含旧nodeid、新nodeid、保留的行为/故障断言、精确head证据。删除旧实现细节测试不等于删除行为覆盖；没有替代的删除不允许。

## 附录I：CI文件修改规则（Allen已裁定⑨）

Allen已于2026-10-25一次性批准以下封闭规则，由独立intg PR节点**CI-I**实施，不再逐PR申请；本轮文档修订不实施CI变更。CI-I必须先于S1-D/E完成，并在S2.6再次通过同一审计：

1. `NATIVE_ORIGINS`只允许追加新文件条目；删除文件时把原条目原样移入`HISTORICAL_ROWS`，不得删除历史条目、改窄路径或删除断言。
2. `check_pr_hygiene.py`、`registry-union.json`的union算法和`TREE_LEVEL_TESTS`不改。`registry-union.json`仅按第1项追加/迁移数据条目。
3. `tests/support/gates/actor_boundary_gate.py`只把“只有backend可导入Pykka”反转为“产品没有Pykka和旧引擎”，不得放宽其他边界。
4. `tests.yml`、`wheel-release.yml`、`github-publish.yml`、`gui.yml`仅可新增钉版本的Rust工具链、目标矩阵和验证步骤，受`ci_workflow_policy`约束；原Go/GUI/快照检查不得删。
5. CI-I交付一个机械审计：对`NATIVE_ORIGINS`、`registry-union.json`、`HISTORICAL_ROWS`的diff只接受“新增条目”或“原样移入”，第4项workflow只增不删；其余diff一律FAIL。任何超范围修改需另行Allen裁定，不得逐PR自行放行。

## 附录J：分支同步与冻结窗口

1. 所有实施短分支只合入`intg/actor-rustify`；最终只有#1897面向`dev`，不直接推`dev`、不自行合入或发布。附录G后续三格未PASS前，#1897不得合入dev或进入任何发布，dev/internal/stable不得包含Rustify。
2. 每周至少一次把`dev`合入intg并运行base-freshness；#1892合入`dev`后立即额外同步一次，以**#1892合入dev时的SHA**冻结最终BASE并重生成附录A/B/D/H，该等待只挡S2.6。同步时列出所有触及intg已删除/移动路径的#1892提交，逐项写明移植位置和测试nodeid；#1892新增测试不得经附录H映射删除。合入前可使用当前intg精确head开展G0/S1及S2非重叠工作，但不得称其为最终BASE。
3. Session/Agent/Harness、PAC/remote-workflow/Routine、共享kernel/CI/锁文件分别作为协调组。修改前由集成owner在任务包写明冻结目录、开始/结束时间、精确base和唯一写入owner，并通知并行团队。
4. 同一authority、schema迁移编号、receipt/fence协议只有一个写入owner。冻结未协调或base-freshness失败时对应分支BLOCKED，不以最后解冲突代替语义协调。

## 改判记录

- **2026-11-06（Allen）⑧ 平台矩阵**：上表“两硬格 + 三后续格”被取代。actor-rustify 第一步要求 5 个平台全部默认 native，每个平台都要构建 native wheel 并实际安装、调用通过；缺 `_core` 的平台不发布，也不回退 pykka。附录 G 的构建与运行证据口径仍然适用，只是不再区分硬格与后续格。**⑨ CI-I** 按推荐方案实现（按路径/分支前缀触发、比较基准用 merge-base、step 级 env 加 guard）。**附录 J / ⑭** 的集成路径改为 `intg/actor-rustify` 直接合入 release。详见 [actor-rustify-step1-plan-2026-11-06.md](actor-rustify-step1-plan-2026-11-06.md)。
