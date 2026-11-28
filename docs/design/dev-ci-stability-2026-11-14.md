# dev CI 稳定化：已批准设计与施工验收

日期：2026-11-14。角色：Astra 架构与多线协同；Sol 施工规划与独立验收；Terra 施工。

初始设计基线：Forgejo `dev` `b15393e3933b6eda6fbeffe503761876a77e6b0b`。已重读该基线 AGENTS.md、GLOSSARY.md、known-problems 的相关记录与 [GUI 单启动设计](gui-single-launch-2026-11-14.md)。本稿结合 Astra 架构与 Sol 施工/验收，由用户批准后进入持续施工；已获得的真实job与局部验收见后文，缺失的runner诊断继续补齐。未复现根因均为UNKNOWN，不把假设写成结论。

整合更新：#2727已进入dev，当时基线`2a494edccdee0135b0a4dbb6fd7f7cf755b803f0`；AGENTS未变，GLOSSARY与KP已重核。最新dev已占用KP-049至060（049为GUI自动升级），本设计早先“reference预留049”不再适用；dev其后已占用061–063，本轮条目从064分配，保留原有049–063。历史红绿仍属于原SHA，不重写为新基线结果。持续目标见[ROADMAP G4](../../ROADMAP.md#g4--dev-ci-稳定性闭环与残余问题归属)。

后续选路更新：#2761已进入dev `28d75429e671ae95f7643315b384b56904bcdd58`，仅改变CI计划、pytest影响选择及其测试；AGENTS未变。本设计各基线都是对应审计时刻，不锁定未来dev。实际CI新增Python测试文件、依赖pins和Python runner输入触发，并允许无其他测试引用的变更模块单跑；本地AGENTS纪律与实际CI覆盖不能混称。

用户已批准本方案，并在2026-11-14授权主协调者：本任务已由Sol独立验收的小PR若仅被无关失败卡住，可以强行合入dev；相关失败不能跳过。执行时记录base/head、无关失败的对照证据、独立验收及实际merge结果，不修改保护、不将合入写成CI通过。该授权不扩展到未经本任务验收的其他PR。

用户补充的 C1/C2 失败基线是未修改 dev `b3985d906`（完整 SHA 待核实），与本稿基线不同，必须分别记录。外部建 PR 与写入由主协调者负责。

最新用户裁定优先于下文历史调查计划：不修 external promotion、GUI signals、daemon-request-browser；旧 Node/Rust host 随 GUI 单一启动改造退场。`verify:agent-provider-browser` 与 `tests/integration/request_http/test_gui_reference.py` 的 reference 代理路径同样登记 KP“随 GUI 单一启动改造移除”，不新增 skip。dev-browser-parity 按 #2758 已修只核实。native 壳不等于引擎：`crates/`/`conformance/` 门保留，macOS arm64 必须修；Linux arm64 仅调查、不修、不删，等 owner 平台决定。其他非 GUI 项与 C1 继续修复。对应历史调查只作证据，不再生成上述旧 GUI 路径的修复 PR。

## 1. 目标与硬边界

目标是让 dev 的完整 CI 获得可重复、可解释的终态，并把测试竞态、环境污染、runner 故障与产品缺陷分开修复。每类独立小 PR，base 为最新 dev；先保存WIP设计、再实现的要求已由[#2764](https://code.hyprial.com/HyprialOS/hyprial/pulls/2764)履行，现方案已批准，预期修复仍不得写成已证根因。用户已授权范围内继续施工，不新增用户确认或设计审批前置门槛。

- 真实产品缺陷须同一精确回归用例在未修复基线红、候选绿；用例覆盖错误状态与修复后行为。
- 不使用 skip、xfail、自动 retry、放宽断言、单纯加长预算或改分支保护制造绿灯。调查阶段的重复执行是测量，每次结果均保留，不是失败重试策略。
- 禁止修改 `gui/`、`src/hyprial/shell/impl/gui/`、`clients/`、`packaging/`。GUI 线只允许测试等待改造，且须对齐最新 `design/gui-single-launch-2026-11-14.md` 及 #2756/#2758/#2760/#2762。
- workflow 变更独立列出并交授权维护者合入。设计/实现方不自行放宽 matrix、required check、timeout 或 runner label。
- HOME、socket、端口、缓存、临时 Git config、daemon 均隔离；不读改清理生产状态、凭据或用户级配置。
- Python/pytest 通过 uv 运行，遵守结构门、模块边界、Contract-Impact、Skill-Impact、无新增行内注释等最新 AGENTS 规则。
- 已登记“不修/待测”的问题先核对适用边界；本任务观测若达到条目的升级触发条件，记录理由再重新调查。

## 2. 先冻结事实，再分派实现

协调者继续补齐共同基线：Forgejo URL、获取时间、AGENTS/KP SHA、锁文件摘要、workflows SHA、各 PR 状态/merge SHA。A6 首先查 #2762；A2 首先查 #2757 的独立 job 是否已落地，独立 job 不等于 startup timeout 已修。旧镜像只能用于定位线索，不可用作声称当前缺陷仍存在的依据。

主协调者已读 Forgejo `/branches/dev`：当前 required context 为 `gate-python / unit`、`gate-python / cli-help-inventory`、`pr-declarations`、`pr-hygiene`，没有 native。早期API曾报告`user_can_merge=false`，但不能将该快照当永久阻断；按本轮有条件合入授权执行并记录实际API结果，权限拒绝才如实BLOCKED。最小required集合与完整CI验收不同，不修改保护、不把四项绿称为完整矩阵绿。

每条记录至少包含：现象与原始 job/run URL；head SHA；matrix OS/arch/Python ABI；runner 身份与镜像版本；实际测试命令、分片清单/顺序、xdist 配置；退出状态及信号来源；完整 stdout/stderr/JUnit；失败发生阶段；复现结果；证据缺口；负责人与下一步。无法取得的数据标 UNKNOWN；权限或环境阻断标 BLOCKED。日志不记录秘密值。

只有原始失败文本相同仍不足以合并问题。合并调查需要共享因果链或相同可控制触发条件；反过来，同一测试不同信号/阶段可拆成不同问题。

## 3. 可并行工作包与设计约束

| 工作包 | 先获得的证据 | 合理的修复边界 | 合并/依赖约束 |
| --- | --- | --- | --- |
| A1 worker crash：150520 / 150682 | worker 的进程退出码、signal、xdist/controller 日志；Linux cgroup memory.events/oom_kill 或宿主 OOM 事件；原生 crash/core/faulthandler；资源峰值与测试分配 | 按已证根因拆 runner 资源、测试进程/资源生命周期、引擎 crash；不得凭最后打印的 squire custody / purge replay 测试名定责 | 可与 A5/A6 并行调查；若改共享 fixture，与 A3 串行合入 |
| A2 teleport startup | 30s actor-runtime-inbox-store 等待的开始/截止与各初始化阶段；线程/actor 栈、锁持有者、进程日志；#2757 当前独立 job 内容 | 初始化顺序/死锁是真缺陷则先红后绿；测试 race 用显式握手、就绪事实，不以 sleep 或延长 30s 掩盖 | 保持 #2757 已有隔离，不重复增加工作流；与 A3 共享 actor shutdown 变更需共同设计 |
| A3 shard3 99%：150742 | runner max-time cancellation 的原始事件；最后活动 nodeid 及 setup/call/teardown 阶段；controller/worker/后代进程树、全部线程栈；剩余任务、耗时轨迹 | 区分总体预算用尽、测试挂起、会话 teardown、子 pytest 无界等待；真正修资源持有者与停止协议 | 与 A1/A2 诊断可并行，测试基础设施修改单 owner；workflow 预算调整不能代替根因修复 |
| A4a freshness：150743 | stale 发生时版本/代数、时钟来源、请求与写入确认顺序、重试条件 | 固定时钟/显式事件/提交完成确认；保留 stale 判定与错误路径 | 独立小 PR，防止与 A6 契约形状改动混合 |
| A4b GUI signals 10s records | 保留既有失败与只读调查、关联 GUI PR | 最新用户裁定不修，随旧 host 退场；不得新增 skip | 登记 KP 与 GUI 单一启动 owner，停止该测试等待修改 |
| A4c settlement Codex rebuild | rebuild 完成事实、旧对象销毁/新对象可用、结算持久态与内存投影时序 | 测试等待实际结算/rebuild 边界，或修真实状态机缺陷；不能把“线程存活”当业务完成 | 与 A2 若共享 actor runtime，避免各自修同一接口 |
| A4d CLI group help teardown | help 是否产生本不应产生的状态；fixture 前后资源清单、线程/actor 注册与退出 | 隔离命令入口/fixture 状态，或修产品多余副作用；不能删除 state-empty 断言 | 如涉及 shared conftest，指定唯一 owner，其他线不直接编辑 |
| A4e Go authority outage / lease 10min | lease 的时钟接口与 expiry 边界、outage 持续和重新连接事件；是否真实跑满十分钟 | 单元层用可注入时钟精确走到到期前/到期/到期后；集成层保留真实网络与过期语义；不篡改生产 lease duration | 作为 Go 独立 PR；真实十分钟覆盖若现存必需，不以单元快测冒充它 |
| A5 GIT_CONFIG hygiene/changelog/devcheck | 在清洁与注入 Git env 两种条件下基线/候选的 2×2 结果；所有 GIT_CONFIG 变量名及存在性，不泄露值 | 测试自建 config 与 repo，明确继承/清理策略；覆盖 `GIT_CONFIG_COUNT/KEY_n/VALUE_n`、SYSTEM/GLOBAL/NOSYSTEM 等适用入口 | 优先独立施工；避免全局改 os.environ 或改开发者 config；不把所有 Git env 都无差别删掉 |
| A6 identity shape / external promotion：150084 / 150085 | job150084实际为worker在identity测试现场消失，不是shape断言；#2762的当前状态与退场边界 | identity现场归A1信号/崩溃调查，与KP-048同源UNKNOWN；external promotion按最新裁定不修 | 只保留外部promotion调查证据，不生成其修复；单跑identity绿不关闭worker crash |
| B native / 3.15t | pending 原因/排队时长/可用 runner label；失败阶段、native crash 信号、解释器构建与 ABI、依赖 wheel、Go/cgo；同 head 的全部 fail/pass 样本 | 先分 runner/test/engine；macOS arm64 必须修；Linux arm64 只调查、不修、不删，等待 owner 平台决定 | 引擎 gate 保留，不受旧 native 壳退场影响；workflow 改动由授权维护者合入 |
| C1 isolated contracts | `b3985d906` 完整 SHA、原始 traceback、`actor_host required` 的抛出点及 ManagedHarnessRuntime 调用；最新 dev 同调用形态 | 先判断契约 fixture 缺少必需 actor_host 与正常产品构造是否仍正确；最小修复保持 actor 所有权，不为旧 fixture 弱化产品约束 | 独立 contract 小 PR；分别跑被前序中止遮住的 http-request、mcp-tools、worker，再跑完整 runner |
| C2 daemon-request browser | 保留原始 16 例/8 fail、约 14min 与旧 host 路径证据 | 最新裁定不修，登记随 GUI 单一启动改造移除；精确根因仍 UNKNOWN | 不增加 required、不加 skip、不恢复旧 host；迁移后的有效旅程由 GUI 线负责 |

pending 不是 failing，也不是 passing。3.15t 一红一绿只能证明观测不一致，不能证明 flake 已解决、引擎无缺陷或失败可忽略。SIGKILL/137 本身也不能证明 OOM；SIGSEGV/139 本身不能证明 CPython 缺陷。

最新 KP-048 已记录完整 417 文件分片中的 OrgFS/Zenoh 原生崩溃：dev `34273d9b` 4 次中 1 次，teleport 8 次中 5 次；有栈的三次共同落在 scheduled sync → sync query → Zenoh query，其他原生线程报 segmentation fault。该条明确根因 UNKNOWN、子集不复现、此前实验已撤回；两边文件集还差 11 个文件。A1 先关联该条并向 OrgFS 网络同步/transport 维护者提供新证据，不能凭历史栈把 150520/150682 直接认作同一故障，也不能用隔离用例替代修复。KP-048 原“不在 teleport 工作内修”是旧任务边界，本次独立 CI 整理可调查，但须保留原记录与新证据的区别。

GUI 单启动设计要求唯一 Python session host 与 daemon request；旧 Node host/reference/mock 路径正在分阶段删除，native 壳与 packaging 删除不包括 `crates/`、`conformance/` 和 actor 引擎 CI。B 中的原生引擎 gate 不能与旧 GUI 原生壳 gate 混称 native，更不能因壳删除而判引擎覆盖不必需。C2 同样不得通过恢复被删除的宿主/fixture 路径修绿。

已读基线 `.forgejo/workflows/native-conformance.yml`：`reference-abi` 与 `pykka` 是必须保持绿的后端；原生 release gate 要求 Linux amd64、Linux arm64及 Darwin arm64 各连续三次 `CONFORMANCE VERDICT: PASS`，BLOCKED 作红而非 skip。Linux arm64实际job运行在release-linux-arm64/aarch64容器且明确Assert Linux/aarch64；旧header提及QEMU不能证明当前执行为QEMU。原生任务使用 3.14t 满足 FREE-03；3.15t reference-abi 按现有规则为 job-level continue-on-error 的非阻断探针，其一红一绿仍须保留并归因，不能隐藏 mandatory job 失败。本任务不改变这些现有语义。KP-027 五平台 wheel 发布债与 conformance 分开记录，不能把 wheel 未产出和 conformance 测试失败混为同一原因。

### C 类检查是否必需：当前建议

C1 是 AGENTS 指定的跨实现契约入口。涉及 daemon runtime/actor ownership、请求协议、CLI/worker 跨实现语义的 PR，应把受影响子契约与完整 isolated contracts 作为验收；前序阻断应独立修复并跑完后续所有段。无需为无关修改无差别追加每PR全套，也不据此自动增加branch required。旧fixture直接构造`ManagedHarnessRuntime(launcher)`而缺actor_host的候选修复已取得本地红绿与Sol完整isolated contracts独立PASS；精确候选及远端未测边界见后文。

C2 在当前 `gate-gui.yml` 的 Sol 初查中不属于执行项；该 workflow 跑 npm test、parity/browser 等检查，不能由它们绿推断 daemon-request-browser 也绿。最新用户已决定本轮不修 C2，旧脚本/host 路径随 GUI 单一启动改造移除，故不追加为本轮必需检查。保留 8 fail 为历史事实与 KP 依据，不把退场决定写成测试通过。迁移后的唯一 host/真实 daemon request 旅程仍由 GUI 线确定对应验收；本轮不恢复旧路径、不加 skip。`verify:agent-provider-browser` 与 `test_gui_reference.py` 同决策登记，dev-browser-parity 只核 #2758 修复是否已进入当前 dev。

## 4. 证据与重复测量纪律

每次试验使用唯一 ID，保存不可覆盖的原始输出与摘要。摘要记录 base/candidate SHA、命令/环境指纹、结果（PASS/FAIL/BLOCKED/INCOMPLETE）、时长、失败签名、退出码、collector 总数和 artifact 路径。终止原因区分测试 timeout、外层诊断 deadline、runner cancellation、人工停止；99% 进度不能推算完成测试数。

复现依次升级：精确 nodeid → 所属文件/紧邻前置测试 → 原始分片与 xdist 分配 → 完整 CI。单例绿不能替代原分片，更不能替代全套。已知问题 KP-019 的 99% teardown 与 KP-022 的子 pytest 无 timeout 是 A3 线索；没有原始 job150742 证据前，禁止声称它们同源。KP-007 已修复的 unresolvable fd 误报也不能解释仍打开且可归因的泄漏。

测量分两阶段：探索阶段保留所有样本与改变的变量；确认阶段预先冻结样本数、顺序与环境，避免见绿即停。建议每项候选在已证复现条件下至少执行十次，随机低频故障按初始失效率加样，不把十次通过当数学证明。基线与候选交错或成对执行，控制缓存、runner 负载与测试顺序；资源重的 native/十分钟 lease 场景由 Sol 根据实际成本预先写出次数与理由。

产品修复须有确定性回归用例：基线红必须失败于目标断言，不能只是安装/收集失败；候选绿后再跑相关负例。测试修复则展示在相同可控制竞态/环境干扰下，旧测试不可靠而新测试保留同一业务不变量；不要为制造红而加入产品不可能出现的状态。无法重现时如实登记观测与局限，不编造“先红”。

并发压测在同一 runner 上可能相互污染。调查者并行工作不等于同时跑多个重型全套：同类资源测量串行或分独立 runner，记录 CPU/RAM/fd/process/cgroup limit；不可用因自造超载触发的失败直接解释历史 CI。

## 5. 实现模式

等待只能建立在被测系统的权威事实之上：持久化完成、事件已确认、actor 注册/停止确认、子进程 exit/join 等。采用单调时钟的固定有界 deadline；读取当前状态直到谓词成立，超时输出最后观察值及必要诊断。不能把调用接受、任务已排队、PID 存在当作业务完成。测试 deadline 与产品 SLA 区分，不通过增大 deadline 获得默认修复。

停止协议显式区分“请求停止”“等待停止”“释放权威状态”。共享 fixture 收尾应先停自己创建的资源，再确认退出，再检查无残留；不要先清空注册表后宣称已停止。清理失败必须保留主失败与清理错误，不能吞异常。测试临时状态限定本例所有权，禁止扫描/kill 任意同名进程。

时钟测试优先依赖注入的时钟/调度器，不全局 monkeypatch 多线程程序的 time；真正的跨进程时间契约保留集成覆盖。环境测试对“必须继承”与“必须隔离”的变量显式建模，避免清理变量后顺带改变原测试契约。

诊断若需持久进入 CI，应与修复分开说明成本、artifact 保留与平台能力；不承诺访问宿主 dmesg/core（权限 UNKNOWN）。可用 faulthandler/进程树等替代证据但不能假装等价。workflow 诊断改动依旧走授权维护者。

## 6. 多线协同与 PR 队列

协调板每行：工作包、负责人、冻结 base SHA、分支、设计 PR、实现 PR、允许文件、共享文件冲突、状态、证据链接、阻断与下一步。关键结论落仓库设计/证据或当前工作记录，不能仅停留聊天。当前尚无实际分派的 runner owner：UNKNOWN，不能填虚构姓名。

建议第一波并行取证 A1/A2/A3、A4 各项、A5、A6/B、C1/C2；优先施工 A5 及确认不触碰共享 helper 的独立测试项。A6 先消除已修复/陈旧问题。共享 `tests/conftest.py`、pytest runner/shard 脚本、actor runtime/teardown 只允许单 owner；其他工作包给最小需求，不抢改。每个 Terra 分支独立 worktree 从当前 dev 建立，禁止在共享 checkout 互相切分支。

Sol 的施工序列归并为：D0 本 WIP 设计；T1/T2 为 A1 两个现场（先归因，再决定一个共因 PR 或两个独立 PR）；T3 A2；T4 A3；T5–T9 为 A4 五类（T6 GUI signals 已停止修复，只登记退场）；T10 A5；T11 A6（external promotion 只留证）；T12 B（macOS arm64 修复优先，Linux arm64 只调查）；T13 C1 必须继续；T14 C2 停止修复并交接退场。此编号不是串行锁。释放的 GUI 调查资源优先补 A1/A2/A3 原始证据及 macOS arm64，已证可修的 A5/freshness/C1 先交 Sol 验收。共享基础设施/契约变更按依赖顺序合入后 rebase 与重测。

每个小 PR 的设计记录：已观测问题、证据、根因/UNKNOWN、拟改边界、保留的不变量、确定性红绿步骤、重复测量计划、邻域回归、未测平台、Contract-Impact/Skill-Impact、依赖。方案已批准，评审与补证随施工推进，不额外等待用户确认。优先普通合入；只被无关失败卡住的已独立验收小PR按本轮明确授权强行合入并留审计，workflow仍由授权维护者处理。有阶段耗时、负载和产品预算语义证据支撑时可拟议预算调整，不得无证上调或单凭调高后绿声称根因已修。

Sol 独立验收不能只复述 Terra 日志：检查最终 diff、冻结最终 head、重复关键红绿/环境对照，确认无禁区改动或断言弱化。Astra 处理跨包根因、模块权威边界与共享文件冲突，不能凭架构猜测替代证据。父协调者负责外部 PR、合并队列及维护者交接；本子任务不外部写入。

## 7. 合入门槛与总体验收

单 PR 门槛：根因证据足够或明确只做诊断；基线/候选验证及原始失败签名留存；修改产品或测试结构时先跑结构门与模块边界；任何代码改动均跑相关焦点测试、邻域/原分片；契约与 skill 影响交代；每个未测项 UNKNOWN/BLOCKED；无禁区、retry/skip/xfail/断言放宽；独立评审确认。既有存量 ruff/KP 依据最新 dev 的规定单列，不能把新增失败算存量。

本地AGENTS纪律仍要求只有`src/hyprial/`变化才追加全量pytest/Ruff，普通文档不要求它们；但#2761已拓宽实际CI Python选路到`tests/**/*.py`、pyproject/lock及Python runner输入。变更模块若独立则PR可以单跑，不等于全tests。适用CI策略/结构检查仍要跑；#2757此前pytest全skip不得计作通过。本任务最终五次必须显式full并核实际selection，不能用docs-only或PR子集冒充全套。

多PR顺序合入最新dev，每次记录合并结果SHA和真实checks，不仅分支head；依赖变动先整合、核对diff并重验受影响面，不无差别重做未受影响测量。强行合入的审计必须列出无关失败的具体证据及Sol验收；相关失败不在授权例外内。

最终稳定性窗口冻结同一产品树、依赖锁、测试/工作流配置及 runner 类别，在该窗口连续五次完整 CI 全绿；五次均有 run URL、head/tree SHA、matrix 全清单、全部 job 终态与时长。调查用 rerun 不替代缺失 job；pending/cancelled/skipped 均不能填绿。若窗口内有产品变更，从新产品基线重启五次计数；测试、workflow、依赖或 runner 配置变化也要重启可比窗口，避免跨配置拼凑。

如果五次全绿不可达，走用户给定的残余KP验收分支：逐项记录现象、影响、已验证规避（无则写无）、决定、证据、owner，明确剩余FAIL/BLOCKED/UNKNOWN与未完成次数。KP登记不等于check PASS；本轮合入授权来自用户明确裁定而非KP存在。没有owner/依据的条目不算完成验收。

当前总目标未验收。已有局部候选通过Sol独立验收，可按本轮合入条件继续；五次完整CI窗口未建立，macOS实机conformance和若干crash/startup根因仍BLOCKED/UNKNOWN，不能把局部验收或合入宣称为整体稳定。

## 8. 施工中的架构决定与证据更新

- A1 诊断独立小 PR：runtime 测试线唯一持有 `tests/conftest.py` 的 `_CiTestProfile.pytest_testnodedown` 修改。仅在 CI profile 开启且 error 非空时，读取 execnet 自有 Popen 的 pid/returncode，EOF 与退出时序不同则最多等待1秒；POSIX 负返回码才解释为 signal，正137不得推为SIGKILL/OOM。非Popen、接口缺失或未退出记UNKNOWN，观测不得覆盖原失败、重启worker或输出环境/命令/敏感错误正文。隔离SIGTERM回归必须保留xdist原有失败状态。cgroup聚合前后数据另交workflow维护者，不能将容器OOM delta归因到特定worker。
- Darwin下载策略候选 `38ece11e0` 仅对Darwin小manifest将低速检测由16KiB/s调为1KiB/s，Linux与大archive不变；保留60s总限时、30s低速窗、原retry及摘要校验。真实curl回环10KiB/s对照旧配置exit28、候选完整下载且SHA256相同，证明局部策略行为；官方HTTPS/Darwin同runner与原生三连conformance仍BLOCKED，不冒充平台修复验收。
- Linux arm64有明确正证据：run57247/job150330在SHA `0c39912e8917496e37f3f89bbf7abf142a7799cf` 已三遍各397项PASS。run57321对应arm64 jobs取消、run57418快照waiting均分别记录，不能覆盖既有PASS或外推为当前所有head通过；按用户决定仍只调查、不修、不删。
- 3.15t新现场：本设计WIP head `1a31baf77b` job151132在ask native-handler得到iterations90，未过>100，396passed/1failed。已有stop/drain低计数发生在不同SHA，不合并成同SHA频率。源码的helper在counter增量>200时即结束，而断言另要求handler自身>100，存在退出条件不匹配假设；contracts测试线已承接受控红绿，原断言与inside-call/并发进度见证不降低。
- Go outage本轮已读run57372/job150738为PASS（同SHA仅一次）；真实lease仍270s流量/310s期限。10min来自旧Go默认timeout，当前workflow有20m Go timeout/25m job预算；650.313s包耗时解释了为什么600s预算不足，但不能据单次通过宣布五次稳定。
- Codex settlement的failed ack开始不等价于durable commit完成；测试应以真实存储返回同步。SQLite connection上下文管理器并不自动close，显式连接所有权与finally关闭需检查。row None与fd泄漏当前不直接归因产品；禁止增加时间代替持久化与停止协议证据。
- WIP的pr-hygiene job151117失败已定位为提交声明遗漏并由主协调者补齐，不作为存量KP；后续gate结果另验。残余KP统一按最新dev分配：049–060已被#2727占用，新条目从061开始；GUI单启动设计旧KP049引用存在历史冲突，不能覆盖现有自动升级条目。
- Sol已独立验收的旧基线候选：C1 `42e48f03ff1d0acca570ff70184fbbfe01f209d2`完整isolated contracts exit0；A4 freshness `21283051945d2534ccc41af9c51a37eaa9fb30bd`自然1PASS及无关sleep干扰1PASS；A5 `34a6b1a0e941ded258de52024505fbb75e5109bc`Git环境污染4焦点PASS；Darwin代码`38ece11e0`/含notes头`8d2f9ef72`本地62PASS；C3 `1849d97f501d9410baaa89802f071b9ec878764d`在3.15.1t、GIL=false下LIV-03为14PASS。远端相应CI仍单独核对，真实Darwin/官方HTTPS与三连conformance仍BLOCKED，CI3.15.0a8结果不得沿用本地3.15.1t。整合新dev后按文件/依赖差异决定重验范围。
- 2026-11-15 更新：当天 `dev` 上全部红项（CI push、定时 lane、本地环境、GUI 浏览器脚本）的分类、原因、归属与修复顺序见 [dev 红色基线 2026-11-15](../reference/dev-red-baseline-2026-11-15.md)；该文同时记录本节部分裁定（external promotion、daemon-request-browser）之后的实际 CI 影响。#2775 的台账未合入，不作为依据。
