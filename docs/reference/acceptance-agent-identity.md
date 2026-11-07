# Agent identity 完整验收方案

> **交付类型：方案，不是实现，也不是产品验收通过报告。**
> 本文定义未来 dev 实现接受的输入、可观测结果、反例和闸门；本单不新增产品代码、测试实现或实现 PR。
> 文档位于 harness-bridge 的 `docs/`：与 `design-agent-identity.md` 同仓、同 PR #434，未来修改实体、daemon、PAC 时可在同一 diff 中核对合同，不依赖另一个工作树的文件位置。

## 0. 对象、裁定、术语与完成条件

### 0.1 内容寻址材料

下文 `D:path`、`P:path`、`H:path` 都是下列**固定 SHA + 仓内路径**的缩写，不是移动分支。

```sh
# 在相应仓库执行；变量花括号保护 zsh 的 :path 解析。
D=09ff4a3248b07626edc2fa832def0d38177b745c # harness-bridge dev 代码基线
P=a270b01c27c393591243f8f50106b985b682fbe6 # harness-bridge PAC 材料基线，不在 D 内
H=90e0f119afadd944741ae1d146a182bfb7c68343 # hq 修订规格
S=ddf3d38b86a09af3e4b0c73fc6fc93bf57c9e465 # harness-bridge 原设计稿 / 本单起点

git show "${D}:src/hyprial/agents/registry.py"
git show "${P}:src/hyprial/pac/lifecycle.py"
# hq 仓库：
git show "${H}:notes/spec-agent-identity-acceptance-2026-09-08.md"
```

| 材料 | 内容句柄 |
| --- | --- |
| 本单修订 spec | `H:notes/spec-agent-identity-acceptance-2026-09-08.md`；blob `7c2962cc36543781e87668a2a6debc8f251eb319` |
| 原设计稿 | `S:docs/design/design-agent-identity.md`；本单仅勘误 §2.4 与 §4 顺序行 |
| PAC 概念稿（先读） | hq blob `e88e0deaac130fde19437be0de9e42f451472672` |
| PAC 相位 | hq blob `5d993e8de6ad80573df878021ca33b6348cf24ab` |
| PAC slice-2（含 F 节后续覆盖） | hq blob `f1a7b89d439931027f54cca4bbe30640cc40fee1` |
| slice-2 历史评审 | hq blob `20e89afa59b2fe3f59c0ac304883234e013c9958`；其中历史待裁不覆盖后来的 Allen 裁定 |

2026-09-08 17:20 +0800 恢复开工时，`git ls-remote origin refs/heads/dev refs/heads/intg/agent-identity refs/heads/intg/pac-graph` 返回 D、S、`573a4376eac211ed3c0f7b0ecaacf7d88605ffbb`。PAC 材料分析仍钉 P，**没有把移动分支或两树直接差当成落地树**。

**目标分支移动已另取读数：**17:38 +0800 再次 `ls-remote`，dev 已是 `N=38a17523fd577ebe44a29d2c07ec91c73dd623c1`（#368 合入）。所以“dev没有PAC”仅是D的历史读数，**不是N的现状**。在N复核，registry/capabilities/lifecycle_manager/orphan_processes/tests workflow的blob与D逐位相同；PAC lifecycle/context/graph/subscription及dispatch matrix的blob与P逐位相同。application的 `_ensure_agent/_seed_agent_bindings` 和CLI的 `_create_agent_for_start` 函数AST与D相同，模块blob及新行号见 `docs/notes/evidence/agent-identity-acceptance/current-dev-readings.txt`。这不是全仓等价证明，旧行号仍只指D。

本单保持S血统，只交文档；不借目标移动夹带实现。对N的合并可行性单独取B；在S代码上跑的既有测试不签N/PAC新合并树的C。未来实现开工须按§12.1重新固定目标树。

### 0.2 已裁边界（不重开）

1. PAC `requires` 只含 `tier`；不复活 `role/context_min/proactive` 匹配字段。职责 prompt、skill 是身份配置/任务说明，不是偷偷恢复的 matcher。
2. PAC 交身份（uri、actor、owner、machine、cwd、config），daemon 管 harness/model/provider、通信、启动和生命周期。PAC 记录 run 持有关系、激活意图与回执关联，不成为第二个进程注册表或启动器。
3. 退避下限由派单的人或 agent 判断；PAC 记录并传递这份判断与实际结果，不自行挑下限。
4. hq-adjutant 采纳回执 `1961004d-8dc3-57a6-a2d7-333758571af1`：先建身份按入口核；已有 managed 顺序保留；`session.register` 不是必经锚点；先建记录与无运行污染分别验收。本单据此勘误原设计稿。

**术语分开：**逻辑身份（稳定实体）、配置修订（任务环境要求）、运行绑定（daemon generation + incarnation/session）、PAC activation（图中本次意图）、运行回执（实际结果）。同名、同 PID、同一 flag 都不能代替这些关联。

### 0.3 本方案的判定规则

- 每个 T-ID 都列【怎么验】【失败时会看到什么】及对应 M-ID 突变。通过需交原始日志、受测 SHA、所用类型/夹具和完整 nodeid；只报 pass 数不计该项完成。
- `PASS / FAIL / NOT_RUN / UNMEASURABLE` 四态分开；未知附原因、开始时刻、下一次观测。`skip/xfail` 不折成通过。
- 本文目标 JSON 字段和 T/M-ID 是**待实现合同**，不是当前 IPC 已有返回值，更不能复制为自造成功桩。
- 不声称所有当前条目都红：已有先建身份、主键/归属、最小 tier 工具及回收机制是保留回归面。每项先取基线，基线已过则不制造重复修法。
- 稳定对象与身份连续性不等于任务输出质量等价。跨 harness 的真实试验验固定产物合同和配置哨兵，不要求自然语言逐字一致。

### 0.4 G6 裁定（2026-09-14）：单路径只约束个性配置层

Allen 2026-09-14 G6 两句原话（逐字）：

1. 「我赞成B，agent自己的独立目录维护的是自己的“个性”，项目相关的内容放在具体的项目cwd中，如何组合则是派发任务时pac编写者的职责」
2. 「是的,“派一个 worker 去 harness-bridge 仓干活”,显然应该自动读取那个仓的配置,“worker 启动时屏蔽” 指的是屏蔽使用系统/user的.claude/.agent 等系统级文件夹,而使用该agent自己的配置文件夹,以免被系统环境内容污染」

⇒ 09-09 的「config 单显式路径」（4adc5a8e）**只约束 agent 个性配置层**：恰好一个显式绝对路径、禁止从用户级 / 系统级全局目录（`~/.claude`、`~/.agents`、`~/.codex`、`~/.pi` 等）隐式加载。**项目配置层**（仓内 `CLAUDE.md`、`AGENTS.md`、`.claude/`、`.pi/`、`.codex/`、`.agents/skills`）**随 cwd 照常自动读**。越出仓根向上到 HOME 的祖先配置文件属于用户级，仍在屏蔽范围内。

⇒ 本合同约束的是 harness 的**自动加载渠道**，不承诺模型永不读仓库文档；普通任务有意读取仓库代码/文档不构成配置发现。选哪个仓、个性与项目内容如何组合，是派发任务时 PAC 编写者的职责。

**出处**：Allen 2026-09-14 G6 裁定；对应 #510 设计 `design/agent-home-dirs@e0e410c2` 的 §4.2（分层与冲突规则）、§4.2.1（任务级项目规则到达方式）、§8 的 T09 / T18。
**本文件的更正点**：§2.1 的 `cwd` / `config` 行、§4.1（含被否方案表）、T-C02 / T-C03 / M-C02 / M-C03、§10.1、§11 E-ID-A、§11 邻近事件表。标注「**G6 之前的形态**」的段落是历史保留，不删。

## 1. 已复核现状及其射程

| 读数 | 冻结出处 / 可复算方法 | 结论边界 |
| --- | --- | --- |
| Agent 有 15 个 dataclass 字段；无 `config` | `D:src/hyprial/agents/registry.py`，blob `8824c8a60c38ad0dffd3d18c06781fd739c738c0`；字段见 §2 | 只描述这份实体，不证明全系统没有任何 skill 输入 |
| `_ensure_agent` 生成、写入/合并 runtime capabilities | `D:src/hyprial/daemon/application.py:6195–6251`，blob `68a5a8421b9fb9381dc1e3f6702423beb37923f4`；`D:src/hyprial/agents/capabilities.py`，blob `a0fb32bb07aa2dac7c9aa4d15483af11cb4cc6f6` | 当前字段包含 harness/interactive/provider/model/tier/proactive_send/tool_injection/updated_at_ms；这与 Agent 的“no runtime state whatsoever”断言冲突 |
| 注册刷新现状已有测试钉住 | `D:tests/test_agent_entity.py:75–110` 的 `test_capmx_*` | 这些旧断言读 Agent.capabilities；未来移动写入权威时需保留其事实检验、改为读 daemon 运行投影，不能直接删掉测试 |
| managed CREATE 先 agent.create、后 harness.ensure | `D:src/hyprial/daemon/lifecycle_manager.py:1179–1209,1247–1249,917–935,1083–1091`；blob `b42a05832846a19d842c5753b5bc2879e503188e` | 静态接线及等待次序，不是全部竞态的实跑证明；§3 单独列入口 |
| CLI headless 与三个 TUI 提前请求 agent.create | `D:src/hyprial/cli.py:5660–5690`；claude `4619/4834`、pi `5126/5217`、codex `5327/5374`；blob `9e1a8590e8c5a1a865ca3616c8603bcda62a3e7d` | 不用 session 注册倒推这些入口的创建时点 |
| 外部 session 接入可补建；恢复也有 seed 接线 | `D:src/hyprial/daemon/application.py:3573–3588,6143–6182` | 补建/更新依赖行是否已存在；不把一个注册样例写成所有入口或所有恢复路径的全称 |
| D 的 `src/hyprial/pac/` 为 0 个条目 | `git ls-tree -r --name-only "${D}" -- src/hyprial/pac/` | 正控：同次读取 D 的 Agent 字段成功；不存在 PAC 测试不能算 PAC 通过 |
| P 的 LaunchSpec 七字段为 harness/provider/model/cwd/args/tier/probes | `P:src/hyprial/pac/lifecycle.py:31–63`；blob `46fbdc3e2fab246bb4763fce067a3cd34c6bf53f` | PAC 当前保存运行配方，与目标的交身份边界有距离 |
| PAC 的指定 registry 名称/直接调用零命中 | P 下全部 13 个 `.py`，parsed=13、parse_errors=0、Name/Attribute 总条目=6354、findings=0；扫描 `AgentRegistry` Name 及 `agents.get/create` 直接属性 | 不处理别名/动态调用，不据此证明所有间接路径都不存在；正向证据是 LaunchSpec 和 ActorRuntime 的实际签名 |
| tier 最小比较工具已在 D | `D:src/hyprial/dispatch/matrix.py`，blob `77d28587a3c4ce9126754ae05607141c7c9ed313`；`meets_tier` 使用 `>=`；`tier_for_model` 返回最高已知档或 None | D 的 `resolve(tier)` 与 P 的 `resolve_minimum_tier` 不混为同一个实现 |
| P 已接最小档解析，但 launch.tier 来自 required_tier | `P:src/hyprial/pac/lifecycle.py:123–158`；`P:src/hyprial/dispatch/matrix.py`，blob `867b78ae25a53916beed02d68161b881e1214908` | actor_up 存 launch/probes 不等于已表达 actualTier、forced/planned 与判断者；需 T-D 验跨层保真 |
| down/旧代收尾机制存在，GC 已有四态与失败可见面 | P lifecycle `_request_down/_reconcile_down/_complete_down/_retire_stale`；`D:src/hyprial/daemon/orphan_processes.py` blob `4cf9bea58bb0a15b515d3350d35e0b6ca39c7214`；`D:tests/test_orphan_process_gc.py` | “机制存在”不升级为目标身份销毁或全部回收竞态已通过 |

完整基线读数及 blob 清单保存于 `docs/notes/evidence/agent-identity-acceptance/source-readings.txt`；该文件随本方案提交，可用交付SHA:path取回。跨仓取 hq 材料使用上面的 blob，不读其他 actor 当前 checkout。

## 2. 实体边界与序列化合同

### 2.1 字段归属：每个现字段都有去处

这里的“实体上”指 Agent 身份权威记录，不指把所有关联信息拼成的 `agent get/ps` 读视图。

| 字段/数据 | 目标权威及为何在那里 | 验收 |
| --- | --- | --- |
| actor、owner、machine | 身份命名/责任/网络承载；跨 harness 不变。创建命名域保持当前 registry 约束，不顺带迁 PK | T-I01、I05 |
| uri | 身份的规范化地址，只由 canonical URI 构造器/明确 hosted 权威取得；消费者不各自拼接第二份身份 | T-I01、I05 |
| cwd | 要在哪个仓工作，属于身份关联的任务配置；不是**个性配置层**（C）的搜索根，也不是 worker 的偶然进程 cwd。**项目配置层随本字段照常自动读**（G6，见 §0.4） | T-I02、C02 |
| config（新增） | **个性配置层**：明确要读哪一个独立路径中的 skill/配置；与 cwd 独立的可审配置输入，形状见 §4。⛔ 不控制项目层；项目层随 cwd（G6，见 §0.4） | T-C01–C06 |
| provider、model、harness_args、preferred_harness | daemon 的持久运行偏好/launch policy；可跨重启保存，但不是逻辑身份不变量。与“实际使用值”分栏，解析失败不能改写身份 | T-I02、I03、D01 |
| capabilities | 当前 runtime 字段移至 daemon 运行观测，绑定 daemonEpoch + actor + binding/incarnation；实体不再接收自动探测产物。旧用户自定义键保留迁移来源，不猜它们都是静态能力 | T-I03、I06、M01 |
| last_harness、last_session_id | daemon 的持久会话历史/交接线索，不是“当前在线”事实；迁移后仍能发 A9 上下文丢失说明 | T-I04、M02 |
| pinned_adapters | pins 关系的读时投影：为何与身份关联，是为了稳定入站路由；更新配置不能清 pin。不是会话运行字段，不用于出站回复相关性 | T-I05、L05 |
| created_at_ms | 身份首次创建出处；启动/绑定不重置为本次启动时间 | T-I01、I02 |
| hosted_by | 明确身份代持来源，不从 owner≠本机或 raw URI 推导授权；本单不增加托管准入（squire-container 另案） | T-I05 |
| pid、online、interactive、当前 harness/provider/model/tier、proactive_send/tool_injection、updated_at_ms | daemon 的实际/已观测运行信息；未知、过期、失败与已知 false 分开。不能经嵌套 capabilities 藏回实体 | T-I03、I06 |
| PAC 持有图、activation、要求链和执行 context | run 的存在理由与任务关联，不复制身份主键或持久 session 状态。run 持有与其他图引用是不同关系 | T-P01–P04、D01–D05 |

### 2.2 具名测试与反例

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-I01 身份恒定 | 真实 registry 创建一行；同 actor 先后绑定两种现有 harness/model，重开 DB；比较 uri/actor/owner/machine/createdAt 与 pins，规范地址一致 | 切模型改变 owner/uri/创建时刻或新增实体。M-I01：把 URI/createdAt 在 bind 时按运行环境重算 |
| T-I02 配置更新与运行变化分离 | 先保存明确 cwd/config 修订与运行偏好；仅运行切换不能改它们；显式配置修订能生效并保留前版本出处 | runtime 注册把 cwd 或偏好覆盖为偶然值；或合法配置更新被静默忽略。M-I02：恢复 `_ensure_agent` 的无条件配置覆盖 |
| T-I03 docstring 真正可失败 | 经真实应用/typed ports 走 create→bind→session refresh→model swap；每步用第二条 DB 连接读权威实体并比完整目标字段集；运行字段只在独立 daemon 视图变化。再测显式嵌套输入 | `Agent`/落盘实体出现 harness、interactive 或嵌套 runtime facts，或者移走后运行面也丢事实。M-I03：把 `runtime_capabilities` 的 harness/interactive 合回实体 capabilities |
| T-I04 历史不冒充当前 | 切 harness 后确认 A9 给 previousHarness/sessionRef 且 contextCarriedOver=false；重启后历史可读，但未新观测的当前绑定不是在线 | 历史丢失、上下文被标成自动继承，或旧 harness 被展示成当前在线。M-I04：用 last_session 直接补 current binding |
| T-I05 唯一性/hosted/pin 不扩权 | 并发同名创建只一行；另一 owner 的同名 URI不能变本地身份；已有合法 hosted 行换运行不改 hosting；配置 save 不夺 pin。沿用 D 的真实 registry/hosted 守卫 | 同名收编、foreign create 成功、hostedBy被任意输入授予、pin消失。M-I05：放宽 native_actor 或把 save 变为 delete+insert |
| T-I06 四格观测保真 | 运行观测已知支持/已知不支持/未知/探测异常各一例；重启旧 epoch、晚到旧 binding 更新、CLI/JSON/下游读取同测 | None/异常变 false，旧 epoch 覆盖新绑定，缺 facade 方法被兜成空列表。M-I06：`unknown→false` 或去掉 generation fence |
| T-I07 反射序列化完整 | 枚举目标增量内 dataclass/NamedTuple 字段变计算属性涉及的 `_asdict/asdict/__dict__/vars/model_dump/**` 消费者；断公共 JSON 的完整字段集合及 null/false 类型，兼容读视图标明来源 | 字段悄悄少一个或默认值伪装已测。M-I07：保留旧 `_asdict()` 展开而省掉新计算属性 |

T-I03 是 docstring 的可执行含义，不是 grep 注释、不只是 `hasattr(agent, 'pid') is False`。

## 3. 创建顺序与归属：分入口矩阵

**目标锚点是身份落盘/域完成回执与受管启动副作用，不是一个总存在的 session 注册事件。**

| 入口 | D 的已核顺序 | session.register 是否存在 | 用来证明时序的观测点 |
| --- | --- | --- | --- |
| managed headless，经 CLI | CLI agent.create → lifecycle.start；saga create/bind → harness.ensure | `LifecycleSpec.session=None` 时不在计划中；不得等待它来证明完成 | CLI创建返回；第二连接读 Agent；launcher.start 进入 barrier；LifecycleMutationCompleted/operation effects |
| managed CREATE，直接 daemon/typed lifecycle | `_create_steps` 第一项 agent.create；逐步等回执 | 仅 spec.session 非空才追加；两种输入分别验 | 真实 AgentActor DB commit与完成回执，HarnessRuntimeActor launcher调用，最终effect列表 |
| CLI-managed TUI：claude/pi/codex | 创建请求返回后 Popen；随后会话接入 | TUI正常接入含 session.register；在进程启动前不可能用该注册当锚点 | CLI RPC/Popen seam和独立DB读取；真实TUI隔离演练确认注册属于同 actor |
| 外部已运行会话直接接入 | 此次服务接入调用 `_ensure_agent` 补建或更新 | 请求本身是 session.register；可能已有记录，不必发生新增 | 接入前外部进程事实与记录存在性分开；接入接受/绑定前读实体；重复请求/错误身份拒绝 |
| daemon 恢复、legacy seed | desired-state 与历史记录驱动 seed/恢复，见 application `_seed_agent_bindings` 等入口 | 不保证出现一条新的公共 session.register RPC；按恢复来源分别检查 | 启动generation、恢复完成/延迟/失败回执、身份DB、当前runtime marker；不能等缺席事件 |

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-O01 durable-before-start | F2 真实四域 composition，在现 launcher.start 注入 barrier；到达时第二SQLite连接必须读到已提交身份。再阻塞 Agent 完成回执，harness start不能越过；测试不替换 lifecycle manager | launcher已进而DB无行/未提交；尚无完成回执却继续。M-O01：交换 agent.create/harness.ensure；M-O01b：提前发完成回执 |
| T-O02 可选session锚点 | 同一真实LifecycleSpec分别session=None及使用已有session类型；断完整completed_effects，None例无session.register且仍能完成 | headless永久等注册、伪造注册事件，或漏非空session步骤。M-O02：把可选步骤改必选/把完成条件绑注册 |
| T-O03 三家CLI托管TUI | 复用现CLI启动测试的RPC/Popen注入点；身份创建失败时Popen调用数=0；成功时Popen barrier内独立读DB。三家参数化，后续注册不得新换身份 | 先spawn后创建；前置失败仍开进程；注册换uri。M-O03：将_create_agent_for_start移到Popen之后 |
| T-O04 外部接入兼容 | 用真实session注册类型，以无记录/已有匹配记录/同名异身份/重复session请求接入；既有外部进程不计为daemon本次spawn | 误套“进程前必须已有行”拒绝所有外部接入；反面是任意外部URI被收编。M-O04：移除创建/匹配校验或无条件覆写身份 |
| T-O05 失败与补偿归属 | 在create各步骤已完成后注入失败；核耐久receipt及补偿。并发外部创建/替换同名行，旧operation只能撤自身创建的资源 | 失败后遗留无主行，或补偿删掉复用/外部替换的incumbent。M-O05：丢created_by_operation/expected_resource_token |
| T-O06 恢复不靠注册推断 | F2/F3 旧desired-state+新epoch，参数化已有行/缺legacy行、restore deferred/成功/失败、close与late start；只对当前授权和意图恢复，旧回执不回写新代 | 缺session事件而永远pending；重复身份/重复进程；已close对象被复活。M-O06：以历史launch或旧session存在直接完成本代 |

“第二SQLite连接可见”证明已提交，不是内存中的 AgentProjection 已更新；真实进程存在由 process/runtime 观测证明，不是只数 mock launcher 被调一次。

## 4. config 的选型、解析与失效合同

### 4.1 本方案选择：单独的显式路径

选择**恰好一个独立配置路径**。派发 PAC 的 agent/人若需要来自别处的材料，先把它们复制进这个路径；Agent config 不提供多路径、列表顶层或优先级解析链。这样来源边界有限且可审，**个性配置层**不会靠用户级 / 系统级全局目录补足。**项目配置层随 cwd 照常自动读，不在本单路径合同的适用面内**（G6，见 §0.4 / #510 设计 §4.2）。

目标配置形状如下（P21 已从 `4adc5a8e` 按源移植到
`src/hyprial/agents/config.py`，并接入当前 registry/ports/CLI/SQLite；本条只说明内核已实现，
不冒称三家 native loader、认证或上线组合已经验收）：

```json
{
  "sources": [
    {"path": "/absolute/agent-config", "required": true}
  ],
  "discovery": "explicit-only"
}
```

- `config` 必须是对象；`sources` 必须是恰好一个元素的列表，元素只含绝对 `path` 与布尔 `required`（默认 true）；`discovery` 必须是 `"explicit-only"`。顶层列表、第二个 source、`precedence`/chain 等解析链字段都拒绝，理由点名违规项。
- 持久 source 为绝对路径，机器归属=identity.machine。相对路径拒绝，不从启动进程 cwd 或 HOME 推断（这是**个性配置层**的路径来源约束；项目层的加载不由本字段推断，它随 cwd，见 §0.4）。
- 这个路径是全部显式配置的容器，不做跨源合并或冲突裁决。派发者负责在运行前把所需公共/项目材料复制进去。
- `required=false` 只允许确认不存在时缺席并在 manifest 明记 missing；无权限、I/O 错误、损坏、循环/越界链接仍是解析失败。
- **用户级 / 系统级**配置不会偷偷追加（含越出仓根向上到 HOME 的祖先配置文件，如 `~/CLAUDE.md`、HOME 级 `.claude`）。**项目配置层随 cwd 照常加载，不在屏蔽范围**（G6，见 §0.4）。若 harness 无法关闭自身对用户级 / 系统级隐式源的加载、或无法兑现显式单路径，daemon 拒绝该运行选择并报告不支持，不能边加载额外配置边报配置一致。
- manifest 在运行前固定，逐项记录实际可加载文件、内容摘要和总摘要，实际加载完成后核对；内容变动（TOCTOU）拒绝此次启动/要求新运行，不把启动后算出的摘要伪装成预期值。引用文件不得自动授予进程控制、托管身份或凭据权限。
- config 只载明确配置/skill引用；不搬运整个 HOME、不把凭据值进实体、日志或PAC context。不同harness的加载适配归daemon，角色prompt可由config/brief引用提供，不进入requires.role。
- P21 的内部映射版本为 `agent-home-p2-native-v1`：`native/<harness>/**` 去前缀投到该家，
  `shared/skills/**` 投到三家的 `skills/**`；碰撞、未知路径及已知 native auth 文件均拒绝。
  `NativeConfigProjection` 与 incarnation-bound `ConfigProjectionReceipt` 是内部类型，不进入公开 Agent/PAC 形状。

「旧 T-C01 顺带挡住的『同名项跨源冲突的确定性』,现在【没有东西挡了】—— 而这是对的」

| 未选形状 | 会怎样失败 | 本方案如何使差异可观测 |
| --- | --- | --- |
| 多路径/顶层列表 | 需要合并、排序和跨源冲突语义，边界随输入展开 | T-C01 在运行前拒绝并点名第二个 source 或顶层 config 形状 |
| 有序解析链 | 优先级与覆盖规则变成 Agent config 的额外机制 | T-C01 在运行前拒绝并点名 `precedence`/chain 字段 |
| 单路径但隐式追加 | **用户级 / 系统级**（含 HOME 级祖先）配置覆盖或补足显式路径，manifest 不含实际加载项。⚠️ **项目配置层随 cwd 加载是 G6 判定的正确行为，不属于本行的「隐式追加」**；G6 之前的形态曾把它一并算入（历史保留，见 §0.4） | T-C03 实际加载哨兵/清单不等式会红 |

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-C01 单路径约束 | 分别提交顶层列表、两个 `sources` 元素、带 `precedence`/chain 的解析链形状；均须在运行前拒绝，理由分别点名 `config`、`config.sources[1]`、违规链字段；一个绝对路径+`explicit-only` 须可持久化 | 任一复杂形状被接受或理由不能定位违规项。M-C01′：让实现接受第二个路径而不报错；完整 T-C01 文件必须只红“第二个 source”这一条 |
| T-C02 cwd/config 正交（G6 更正） | 换 cwd 到另一个仓：**个性配置层 C**（显式 config 路径）不变，C 的 manifest / 加载哨兵逐字不变；**项目配置层的仓内哨兵随新 cwd 生效**（跨仓方向由 T-C03 / T18 细化）；换 C 不移动工作仓；写入只落新 cwd | 个性层被 cwd 改变（config 根被 cwd 顶替或个性哨兵变了），或项目层不随 cwd 生效。M-C02：用 cwd 代替 config 根 |
| T-C03 用户级/系统级禁入 + 项目层照常（G6 更正） | **两向分别点名、互不充数**（参照 #510 设计 T09）：①在隔离 HOME 与仓外祖先放**用户级/系统级诱饵哨兵**（宿主 `~/.claude`、`~/.codex`、`~/.pi` 对应位置与越仓根的 HOME 级祖先 `CLAUDE.md`），仓内 cwd 放**项目哨兵**（`CLAUDE.md` / `AGENTS.md` / `.claude/` / `.pi/` / `.codex/` / `.agents/skills`）；分别用实际支持的两种 harness 启动并完成固定文件产物：用户级/系统级诱饵**不得到达**，项目哨兵与 C 哨兵**必须到达**，manifest 与预先固定的有效哨兵一致。②反向独立负例：关掉项目层发现（如 Claude `settingSources` 去掉 `project`）⇒ 项目哨兵用例红。不能关闭用户级/系统级隐式源的适配器必须拒绝 | 未列出的**用户级/系统级** skill 生效、秘密/宿主配置被读、拒绝被伪装为成功；或项目哨兵被一并关掉（把「屏蔽」误扩到项目层）。M-C03a：去掉一家的用户级配置根重定向（或 Claude `settingSources` 加回 `user` 读宿主）⇒ 用户级诱饵用例红。M-C03b（独立反例）：关掉项目发现 ⇒ 项目哨兵用例红。两向各自预先点名，不互相充数；无法造成预测红先修仪器，不宣称变异成功 |
| T-C04 缺失与错误分开 | required缺文件、optional真不存在、PermissionError、坏配置、链接循环/逃出批准源各一例；必须在启动前拿到具体源和错误 | optional把权限错误吞掉；required缺失仍启动；失败没有来源。M-C04：except OSError统一continue |
| T-C05 修订/摘要围栏 | 解析完成至spawn之间替换源或symlink目标；核预先记录的manifest；重复旧operation不得重解释同一路径新内容 | 实际加载新内容但报告旧digest；旧任务被悄悄升级配置。M-C05：把digest移到执行后生成/移除复核 |
| T-C06 输入权限与迁移 | 非法/无基准相对路径拒绝；源manifest/实体/公开context不含密值；旧无config行须显式迁移确认或拒绝有说明 | 旧行默认为已验证空config；读配置顺手获得新身份或退役权限。M-C06：缺config兜[]或按原始config声明授予控制权 |

可选源已确认不存在，豁免该源的加载，不豁免缺席记录、其他必需源、读取错误处理、实际manifest一致性及权限边界。旧行迁移未确认，豁免声称已完成新config验收，不豁免可见诊断、既有数据保全和后续补验。

## 5. PAC 交身份、daemon 实现运行

### 5.1 边界合同

PAC 的 actor 声明引用稳定身份及期望配置修订，`requires` 只有 tier。身份记录由 daemon 的 Agent 权威创建/校验；PAC 不直接另写 agents SQLite。PAC 的“我持有这个产物”需耐久 run/node/activation 与 daemon创建回执关联，不能只凭短名或 raw launch 输入。

目标适配调用传 identity reference + config revision + 当前请求/operation关联；harness选择、args、probe命令、runtime通信、kill方式都由daemon解析。PAC可以保存和向下游公开**daemon回执中的实际运行事实**，这与持有一份可执行LaunchSpec配方不同：不能拿历史回执重新当启动器输入。

身份URI是可读引用，不自动授予管理权。图中其他task引用同一actor只是assignment，不获得其mandate和退役权。已有常驻同名对象不能被run的reuse路径偷收编。

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-P01 无运行配方穿过PAC边界 | 在目标生产接线记录PAC→daemon请求的实际类型/序列化字段集；输入固定身份、cwd/config/requires；daemon更换可用harness后仍用同身份。静态扫描新PAC路径与动态调用两证合用 | PAC发harness/args/probe命令或直接spawn/kill；换harness必须改actor定义。M-P01：把LaunchSpec重新作为PAC调用输入 |
| T-P02 requires词表守住 | 编辑/导入/CLI分别提交仅tier以及role/context_min/proactive/未知字段；非tier字段拒绝且图版本/DB不变。prompt中的职责文字不触发隐式matcher | 额外需求悄悄存下却不匹配，或task文本被PAC当策略。M-P02：校验器接受未知requires键 |
| T-P03 持有和引用分开 | run R创建w，X/Y仅引用w派活；结束X不stop w，R关闭请求退役；同名已存在无本run receipt拒绝收编；用真实DB并发两个持有请求 | X抢mandate、close X杀R的actor、两个run同时持有。M-P03：用node.owner/名字代替创建归属 |
| T-P04 因果与运行观测不混 | actor flag只由当前启动成功回执写；owner stop/close写意图；网络超时/投递接受/普通回复不自动置flag；反应器不读inbox正文 | 回复文本或presence令任务完成；desired=down被报为已死。M-P04：用消息送达或desired生成完成flag |

## 6. 退避判断与实际档位：完整下游工作包

### 6.1 三份事实不可合成一个 tier

目标context以同一 graph/node/activation/config revision/receipt建立关联：

| 分组 | 最小字段和语义 | 谁产生 |
| --- | --- | --- |
| dispatchDecision | decisionId/revision、decidedBy（可解析的人或agent身份）、decidedAt、reasonRef、workIntent=`draft|final|…`、acceptanceRef、preferredRuntime（可选偏好）、有序授权退避链与终止条件 | 派发者的显式判断；PAC耐久记录。偏好可被环境否定，不是启动配方 |
| attemptRequirement | 此次最小requires.tier、所依据decision修订、授权链位置；原始目标档保留 | 派发者/其明确授权委托给daemon的决策输入；PAC不自行降低requires |
| executionReceipt | operationId、binding/incarnation、daemonEpoch、identity/config摘要、actual harness/provider/model、actualTier、观测来源/时刻、选择读数引用、outcome | daemon返回实际结果；不能以要求档或拟选模型填充actual |
| outcomeContext | `requested-met|forced-fallback|planned-low-tier|unresolved`、原要求与实际差异、对应decisionId、产物ref和下游acceptanceRef；值由结构化事实关联表达，不解析回复散文推断 | PAC记录/投影并传给后继；语义判断出处保留为派发者 |

- 原目标strong、派发者授权不可用后fast：forced-fallback。PAC仅保存授权链；执行方按明确授权尝试或派发者另发修订，不能由PAC反应器凭空生成新下限。一次fast运行的适用minimum也是fast，不能让`meets_tier(fast,strong)`返回真。
- 从一开始指定fast粗稿、由strong/super审查收尾：planned-low-tier；即便实际选到更高档，也不改写原来的粗稿角色/验收合同。
- 同档换harness只是偏好未命中，不必伪造“降档”。更高档满足低要求是minimum语义，不等于要求档自动变高。
- actual model取harness实际确认/运行回执，并记录观测来源；仅请求参数或probe选中结果不能伪装为实际运行。actualTier按该实际model与冻结矩阵版本导出，矩阵版本/digest随receipt保留，未来矩阵改变不能重写旧档位。
- `actualTier=null`、probe失败、daemon只报计划而无实际回执时记unresolved。下游知道缺哪次观测，不把它当已达标或最低档。
- 图结构激活后冻结的既有PAC语义保留；后来的执行context经带关联的追加journal事实传递，不靠修改已激活图requires或读最新Agent状态重写历史。

### 6.2 如何证明真的“传给下游”

用计划图 `producer → reviewer → end`，另加 back 与双前驱 join。producer 的产物引用关联本次execution receipt；reviewer通过公开 `workflow inspect --node --context`/`events`（或目标等价公共出口）取得该次decision及receipt，不读数据库内部表、不读聊天正文。固定快照cursor，断线重连/重复通知/回边新activation均保持关联；通知可只载引用，但该引用必须可解析到**此次**完整context。只在daemon日志里出现actualTier，不满足此项。

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-D01 实际与要求不同 | 用现矩阵里的模型构造请求fast但实际`gpt-6-astra`最高档super的回执；另例请求strong但获授权fast fallback。独立核daemon实际选择与PAC公共JSON | actualTier被要求档覆盖、未知被填fast、daemon事实正确但context缺失。M-D01：actualTier=requestedTier |
| T-D02 forced与planned分开 | 实际相同的fast/model/产物，分别给“strong不可用后授权fast”和“预定fast粗稿”两份decision；reviewer输出可区分且decidedBy/reasonRef保留 | 两份context逐字段相同、forced伪装计划内，或无判断者。M-D02：丢mode/decision身份 |
| T-D03 PAC不定下限 | 首选失败且无授权fallback时，PAC不得发更低要求或自动置完成flag；已有明确授权链时记录每次attempt与停止边界 | PAC自行降档、替派发者批准、无授权仍消费低档产物。M-D03：反应器自动append fast兜底 |
| T-D04 下游黑盒保真 | reviewer只用公开context/events，从固定snapshot@cursor重放；模拟通知重发、断线、双前驱不同receipt、back下一轮，比较每个产物对应decision/actual值 | 下游只见requested、读到最新运行而非产物那次、join丢一前驱、晚到旧完成覆盖新轮。M-D04：投影省receipt关联或读取当前registry替代历史 |
| T-D05 未知和传播失败响亮 | 缺/坏receipt、context查询异常、下游引用不可达分别注入；产物可列但验收状态unresolved、有原因与下一观测；其他独立节点不静默挂死 | 缺字段折成“未降级/已达标”、仅后台日志、无开始时刻与有界升级。M-D05：except返回空context或null→默认成功 |

本合同的可达性只授予读这份任务context的既有权限，**不**授予按原始产物字段退役actor、读取凭据或修改其他run。无法读取引用豁免下游宣称已完成验收，不豁免错误可见、产物/身份保全、授权边界和后续补验。

## 7. tier 匹配的真值表

词表取 `D:src/hyprial/dispatch/matrix.py`：`fast < strong < super`；A/B口头档位不自行映射成新词表。

| actual / minimum | fast | strong | super |
| --- | --- | --- | --- |
| fast | 接受 | 拒绝 | 拒绝 |
| strong | 接受 | 接受 | 拒绝 |
| super | 接受 | 接受 | 接受 |
| unknown/null | 未证明满足 | 未证明满足 | 未证明满足 |

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-T01 最小比较全矩阵 | 九格+unknown分别跑生产matcher及PAC接线；未知required词编辑时拒绝。既有`meets_tier`回归保留 | 高档被等值比较拒绝、低档放行、未知词KeyError裸露或假匹配。M-T01：把>=改==（必须在higher/lower对照上红） |
| T-T02 模型最高档与未知 | 真实TIERS中`fable/gpt-6-astra`跨strong/super出现，返回super；未知model返回None；修改候选排序不改变最高档含义 | 取到第一次出现的strong、未知猜fast。M-T02：取消reverse或unknown默认fast |
| T-T03 可用性与能力分开（**2026-09-21 部分被取代**） | 选型**不读可用性**：矩阵静态取声明序首项，`hyprial dispatch` / PAC `requires.tier` / routine 三入口都不探活、不据标记跳过。取代段的正控：`tests/test_provider_availability_contract.py::test_pc1_selection_reads_no_runtime_fact`（探针调用数必须为 0）与 `::test_pc3_marked_head_is_still_selected_and_mark_survives`（带真实标记仍选首项、标记仍在盘上）。仍有效部分：能力矩阵语义（最高档、minimum 比较）归 T-T01/T-T02；标明不可用必须对原发送者 fail-loud，见 `::test_pc2_failed_attempt_reaches_the_sender_with_this_attempts_evidence` | 旧行「probe=False 却标已探活 / 跳过 probe」已不是坏变异：现在是**探活参与选型**才坏。M-T03：恢复据标记 `continue` 或先探活再选 ⇒ PC1/PC3 红 |

## 8. 回收、所有权及不可观测路径

run因果仍由PAC记录，实际停止和身份副作用由daemon执行。down=停止绑定且保留身份/pins；destroy=显式销毁实体及关联清理。run close要求回收其拥有的运行资源；**不据“回收基本到位”擅自增加删除所有Agent行的政策**。结束后保留的身份及其理由在报告里逐项列明。

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-L01 close与late start | 在start已获runtime回执但PAC写flag前close；当前归属资源交daemon收尾，不能先补flag派下游活；慢stop与另一独立节点并行 | close后actor_up/新assignment、丢晚到资源或阻断整图。M-L01：先_complete_up再检查closed |
| T-L02 down不等于destroy | 真实down成功后身份/pins在、绑定与本次runtime不在；显式destroy才删行及其pins；stop未确认时报告stopping/unresolved | 请求停止即报已死、down清掉身份、destroy留下无主pin。M-L02：由desired=down伪造完成 |
| T-L03 旧代/同名/坏数据不扩权 | 本run旧marker可识别收尾；同名但不归本run的新代/PID复用/被替换resource_token不动；坏原始launch/produces可诊断但不能作kill授权 | 按名字或raw输入误杀incumbent；删一条坏行会停另一个actor。M-L03：删marker/token验证 |
| T-L04 四态GC和边缘失败 | 用D现有ProcessLivenessState、probe异常及TOCTOU fixture；点名清理/回滚/补偿、daemon线程、finally、facade缺方法、最后一次重试、未知else；持久状态与CLI均可见、超预算上报 | unknown/probe-failed折dead；只log无状态；线程异常丢失；缺方法当无孤儿。M-L04：未知→dead或catch后只warning |
| T-L05 重启保留归属并有界收口 | 运行代变化、pending stop已实际absent、重复restore、run closed、孤儿活/死/未知参数化；验pending年龄/尝试数/所有者通知，清理不误删外部replacement | pending永不结束又不暴露、恢复重复计incarnation、旧receipt认领新资源。M-L05：absent直接跳过pending收尾或去epoch幂等 |

预算不是睡眠常数：lifecycle使用目标树既有operation deadline与wait margin配置，GC使用unknown阈值；测试用同一配置导出断言并注入时钟。实现落位时冻结具体配置名/数值及公开错误字段，超限保持可见未完成/失败，不报成功；本方案不新定第二套数值预算。正式实例的观测日志记录每次采样时刻、阈值、已持续时间和下一责任人。

恢复不改写D的既有跨代补偿合同：`D:tests/test_lifecycle_process_manager.py::test_process_manager_restart_compensates_durably_completed_effects` 明确未完成saga跨daemon代执行补偿、desired-state才是重运行权威。PAC的operation关联用于认领/追溯与幂等，不能解释成把旧daemon的未完成forward cursor无条件接着跑。

## 9. 数据迁移、兼容与反射边界

| ID / 目标 | 【怎么验】 | 【失败时会看到什么】 / 固定突变 |
| --- | --- | --- |
| T-M01 真实旧库迁移 | 由D的AgentRegistry建立含runtime capabilities、自定义键、pins、hosted记录的真实SQLite；目标迁移后身份字段白名单、运行历史隔离、来源可查；中断回滚/重开/重复迁移同测 | 只改CREATE IF NOT EXISTS旧库未升级；原行/pins丢失；旧runtime被当当前观测。M-M01：跳过ALTER/迁移版本或把全部旧JSON复制成当前运行事实 |
| T-M02 偏好/历史兼容 | provider/model/harness args/preference/last session分别迁出，A9仍能引用过去session；旧API读视图显式区分identity/policy/runtime来源 | 迁移后不能启动、丢历史，或者为兼容再次把runtime写回实体。M-M02：迁移遗漏lastSession/policy其中一项 |
| T-M03 新旧公共JSON与错误 | AgentProjection、facade、IPC agent get/list/ps/targets、PAC context/events逐字段往返；未知schema/缺config/旧client新server给明确兼容/拒绝行为，不伪成功 | 少键、null→false、错误码未注册或旧客户端成功但看不见实际档。M-M03：一个投影遗漏字段，或未知schema兜当前版本 |
| T-M04 名字与历史寻址 | 更换运行或显式config修订后历史产物仍指向同一身份和旧receipt；destroy后旧引用按既有不可逆边界明确不可解析，不伪装另一个新建同名实体 | 同名重建让旧receipt指向新actor运行；不报不可达。M-M04：只用actor短名查最新binding |

不把旧`capabilities`全部键自动判成静态能力；无法分类的值隔离为legacy出处并给迁移诊断。迁移不得读取用户HOME中的秘密来“补齐”字段。回退只允许保留原始备份并有版本检查的只读恢复；旧二进制不默默覆盖新权威。

## 10. 测试落位、真实类型与变异执行协议

### 10.1 每条修法的测试归属

| 修法/保留项 | T-ID | 目标测试文件（未来新增/扩展，当前不冒称存在） |
| --- | --- | --- |
| 身份、配置、运行权威拆分；docstring兑现 | T-I01–I07 | `tests/test_agent_identity_contract.py`；现 `test_agent_entity.py` 的事实断言迁接线 |
| 分入口顺序与完成围栏，不重排已成立流程 | T-O01–O06 | `tests/test_agent_identity_ordering.py`；扩真实composition、CLI启动与恢复测试 |
| 个性层 config 单路径、C 与 cwd 解耦、项目层随 cwd、加载清单 | T-C01–C06 | `tests/test_agent_config_contract.py`；真实harness隔离场景 |
| PAC交身份不交运行、只tier、持有/引用 | T-P01–P04 | `tests/test_pac_identity_contract.py`；扩P的actor/graph/tripwire |
| 派发判断、实际结果与下游context | T-D01–D05 | `tests/test_pac_execution_context.py`；公共CLI黑盒客户端 |
| 最小tier与可用性分离 | T-T01–T03 | 扩 `tests/test_dispatch_matrix.py` 及PAC matcher接线 |
| 回收保持归属、四态与边缘可见 | T-L01–L05 | 扩actor lifecycle、orphan GC、close-under-live-actor相关状态断言 |
| 旧库/公共JSON/迁移及历史 | T-M01–M04 | `tests/test_agent_identity_migration.py`、既有projection/contract守卫 |

共 **40 个 T-ID**，每条至少一个测试函数；参数化例必须保存各自nodeid。M-I…M-M是固定反例意图，落位时记录确切补丁blob及预测失败集合，不能把意图文字当已执行突变。T-O01另有M-O01b，分别运行。

### 10.2 现有仪器与禁用捷径

| 仪器 | 内容句柄/真实类型 | 能证明什么；不能替代什么 |
| --- | --- | --- |
| F1 真实实体库 | `D:src/hyprial/agents/registry.py` AgentRegistry/Agent；`D:tests/test_atomic_lifecycle_ports.py` | 第二连接可见性、唯一约束/receipt fence；不证明进程实际启动 |
| F2 生产四域装配 | `D:tests/test_final_composition_lifecycle.py::test_production_lifecycle_composition_runs_all_four_domain_ports`；真实 DaemonApplication/LifecycleManager/AgentActor/HarnessRuntimeActor/HarnessPortClient；`D:tests/test_hosted_transfer.py` 有完整装配夹具 | 在现 `_Launcher` 等外部I/O seam加barrier；不把manager换成inline模拟器 |
| F3 operation/恢复 | `D:tests/test_lifecycle_process_manager.py` 的 `_CompletionPorts`/`_CompletionGate`，`D:tests/test_lifecycle_restart_compensation.py`；LifecycleMutationRequest/Completed、MutationProvenance | 精确回执时序与故障控制；最终顺序判据仍需F2+真实DB，不只检桩calls |
| F4 档位/探测 | `D:tests/test_dispatch_matrix.py` 使用 `ProbeRun`/RuntimeCapability/UserProfileStore；P同路径的minimum解析类型 | 可以注入探测结果，但不能自行造一个 `{alive:true}` 代替ProbeRun |
| F5 PAC领域 | `P:tests/test_pac_actor_lifecycle.py` 的 graph/coordinator/FakeRuntime，实际 RuntimeObservation/ResolvedLaunch/LaunchSpec；PacGraphStore | 支撑P的领域时序；新identity port落位时用正式新类型替换，不能仍传旧LaunchSpec假装目标边界成立 |
| F6 公共context | `P:tests/test_pac_events_blackbox.py`、`P:src/hyprial/pac/context.py` 与subscription snapshot@cursor | 外部消费者只公开CLI/JSON，不import内部store来补字段 |
| F7 GC四态 | `D:tests/test_orphan_process_gc.py` 的真实OrphanProcessRegistry/ProcessLivenessState/ProcessLivenessProbeError | 注入identity reader及现process类型；不能把异常改成RuntimeObservation(False) |
| F8 真实运行 | 目标树已有隔离E2E runner与harness驱动，临时HYPRIAL_HOME/state/socket/cwd；不使用生产actor或凭据 | 固定小产物、加载哨兵、真实进程/恢复；不可用harness明确NOT_RUN，不算成功 |

现 `D:tests/test_agent_entity.py` 使用 `_InlineLifecycleManager`，其已有测试绿**不能单独证明T-O的生产顺序**。P的FakeRuntime也不是daemon实际生命周期证明。所有新桩需标出它实现的正式Protocol/返回类型和已有出处；未知端口形状先取生产类型，不靠想象补JSON。

### 10.3 红过绿回（实施阶段，非本单已执行）

每个M-ID独立执行：

1. 固定目标head、测试文件blob、夹具版本、collected清单、突变补丁blob及预测红nodeid集合。首次加断言时另记“②新断言在旧实现上造红”；不与“①突变把实现改坏造红”混记。
2. 正确实现先跑该完整测试文件为绿；施加M原补丁，仍跑同完整文件，**不加 `-k`**；核失败点、异常/断言文本与预测集合完全一致。
3. 若旧守卫在这个M下全绿，补守卫后必须重跑**同一个M**，不能换一个更容易红的变体。红多/红少都先解释碰到的额外分支，不过滤隐藏。
4. 原样撤销M，核受测产品文件恢复到预先记录blob，再跑同一文件绿回。红/绿collected一致；新测试数量变化单独解释，绝不把setup级联算N个独立缺陷。
5. 保留原始stdout/stderr、退出码、collected/failed列表、补丁、恢复blob；复核者能在同树重放，不能只转述“变异成功”。

本单**没做**这些实现突变，不用现有pytest通过冒充它们。

## 11. 端到端验收与实际可观测面

按以下四条隔离场景交证，每条的失败也回连对应T-ID：

1. **E-ID-A 身份不随环境漂移**（I/O/C）：同一 identity/config，两个现有支持的 harness 分别处理同一固定任务：在批准 cwd 生成机器可验 JSON，内含从真实 skill 读取的哨兵。验 DB 身份/pins 等式、运行回执不同、产物模式成立；**用户级/系统级诱饵 skill**、漏配置、错误 cwd（项目层哨兵缺失）必须红，**项目层哨兵必须生效**（G6，见 §0.4）。不能只让模型复述 prompt 里已经给出的哨兵。
2. **E-ID-B 下游读懂粗稿**（D/T/P）：两次实际低档，一次forced一次planned，reviewer通过PAC公共context得到不同decision来源和同样实际档；按预先固定acceptanceRef验产物。拔掉投影中的actual/decision字段必在reviewer验收红，不在producer日志里自证。
3. **E-ID-C 崩溃/旧代**（O/L）：在身份落盘后、harness启动前；harness成功后、PAC收回执前；close与late ready；down pending且runtime已不在四窗口分别中断/恢复。核无错误新身份、无误杀、无重复完成；真实进程观测与durable operation共同作证，缺观测记UNMEASURABLE。
4. **E-ID-D 升级迁移**（M/I）：D生成的真实旧库→目标迁移→启动/切harness→daemon重启→下游读取；历史引用、pins、身份与旧档位出处都可核。坏库不静默重置为空；恢复路径不可观测须公开失败与下一观测，不等待一个永不会到的session事件。

场景只用临时隔离home/socket和明确授权的小测试账号；认证/额度或外部harness不可用时，豁免本次真实调用的执行，不豁免未测标识、风险说明、配置与权限不变量，也不豁免上线前补足该场景。缺这一场景不能签“完整产品验收通过”。

### 邻近事件自检（防规则的字面漏洞）

| 规则族 | 邻近事件及处置 |
| --- | --- |
| 身份不变 | 配置显式修订：有意允许、需版本；destroy后同名重建：不是同一运行历史，M04覆盖；hosted迁入：独立授权边界，不因新字段开放 |
| 先建身份 | 外部已运行会话：有意不同入口，O04；恢复无session事件：O06；create已提交但回执未发：O01b必须补测 |
| config一致 | optional真缺失：有界例外并记录；读取失败：不豁免；解析后换symlink：C05覆盖；用户级/系统级诱饵：屏蔽，且项目层哨兵必须到达（G6，T-C03 两向分别点名） |
| tier最低值 | 同档换harness：不叫降档；高档做低档任务：接受；派发者明确变更最低档：新decision，不改旧历史 |
| context留痕 | 通知只载引用：允许但下游必须可取本次值；join多前驱：逐个receipt；back后旧结果晚到：D04覆盖 |
| 失败响亮 | facade缺方法、daemon线程异常、finally/rollback失败：L04/D05逐处覆盖；不能删catch后让后台任务静默退出 |
| 验收签核 | 文档正文报告勘误：不改变产品树；head/base变化：分别失效相关证据；draft变化：内容复核可保持但不代表能合并 |

## 12. 执行闸门、证据对象与交付状态

### 12.1 未来实现的执行顺序

1. **基线与量尺**：`git ls-remote` 固定目标分支SHA；明示实际受测树（功能树还是合并产物）。比较workflow blob、分叉方向、目标有而head没有的测试文件，并按merge-base区分“新增未带入”与“本分支删除”。
2. **焦点与邻域**：T-ID对应文件全文件跑；邻域按被改分支的事后状态找，不按目录距离；包含现身份/hosted/生命周期补偿/绑定/GC/投影/CLI消费面。
3. **仓级守卫**：新增事件→启动白名单；新增import→import边界；新增身份拼接→URI守卫；新错误码→注册表。不能以焦点+邻域+lint替代；全量包含它们。
4. **lint**：仓根执行 `uv run --locked --python 3.12 --extra test ruff check .`，包括notes下证据脚本；无目录缩窄。
5. **全量**：`uv run --locked --python 3.12 --extra test pytest tests/ -q --no-header --basetemp=<本次唯一临时目录> > <完整日志> 2>&1`。贵的观测直接落盘，不经过tee/head/grep管道。
6. **红的路由**：FAILED先查该断言；ERROR at setup先查共享fixture/构造器并列共用者。同树同命令对照保留；已知共享红不强行归本改动，单独修复落地后merge（不rebase）再取新树读数。观察失败/超时不记绿。
7. **真实场景与变异**：§10.3及§11逐项交证；文档阶段NOT_RUN不伪装成实现免验。

### 12.2 A/B/C 分开签，不用PR绿代替合并绿

- **A 语义**：全部自身增量（不预切src/tests）=`git diff $(git merge-base <target> <head>)..<head>`；记录每个被审文件blob和逐文件patch指纹。迁移A需同自身增量证据；不抹去rebase/force-push造成的血统变化。
- **B 落地树**：固定base/head两个SHA、merge-base是否等于base头、`git merge-tree --write-tree --messages <base> <head>` 的树hash及完整冲突消息。结论关于合并之后时检查产物树，不拿两输入树差异代替。base/head任一动则重取B。
- **C CI**：固定taskId/head_sha及该head的闸门定义。决策时比对task.head_sha与PR当前head；head动则重取C。A可迁移不等于B/C可迁移。
- 合并后由合并操作者按merge SHA查目标分支自己那次CI与dev标签；本方案不执行合并。未知/不可测量写明起始时刻、原因、下一观测和到阈值的责任人，禁止空白挂起。
- 新推送、改base、draft/WIP等改变可合状态后重发DONE，写树是否变化及哪一段失效；不拿旧head读数充数。

### 12.3 本单交付账本（文档阶段）

- 上一单留债：**无已交付的实现债**；继承的产品距离是§1各项，不归本单新引入缺陷。
- 绊线与禁令：只出方案；不改产品/测试代码；不重排managed CREATE；不让PAC取得运行或退避下限决策权；不把其他squire-container评审的准入改动夹进本单；不操作生产生命周期或凭据。
- 已知无关红：**本次正式读数产生前无确认的当前红**。hq历史PAC评审提到的GUI/ChildProcessError/v1契约红来自其他树，不当本树结论。
- 收到的本单更正：分入口顺序、可选session锚点、设计稿两处勘误已落实；三条Allen裁定完整保留。拒单前停工不计90分钟，恢复回执重新给绝对ETA。
- **G6 更正（2026-09-14，独立文档单 `docs/acceptance-agent-identity-g6`）**：本文件 §0.4、§2.1、§4.1、T-C02/C03、§10.1、§11 及 `docs/design/design-agent-identity.md` §3/§4 已按 Allen 2026-09-14 G6 裁定更正：单路径只约束个性配置层，项目配置层随 cwd 照常读，屏蔽用户级/系统级（含 HOME 级祖先）。历史口径保留并标注「G6 之前的形态」。依据与逐条对照见该 PR 正文。
- 当前新增T-ID实现：**没做**；40个验收项及各自反例已经定义。变异红过绿回、E-ID-A…D、未来PAC/daemon新接线：**没做**。
- 当前dev既有pytest/lint的命令、完整读数与日志句柄：以随交付的证据记录为准，分别报告基线与交付树，不把它们解释为本方案40项已经实现。
- 方案完整的判据：规格最低六格、设计稿修法、所有现字段、四个入口族及可选session、边缘失败、迁移/序列化、下游读取、最小tier、每条T对应M与真实仪器均有可定位条目。产品完成的判据：所有适用T/变异/真实场景及全仓闸门取得同对象证据；本单没有宣布后者成立。
