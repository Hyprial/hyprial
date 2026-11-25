# CLI daemon request 收敛执行 goal

状态：ACTIVE。计划 #2763 已由本会话用户于 2026-11-14 批准，第二阶段开始。
目标分支：dev。负责人分工：astra 架构与协调；sol 规划；terra 施工；sol 独立验收。

## Goal 与完成标准

持续将 CLI 收敛到参数解析 → 本地 IPC daemon request → 输出；业务执行于 daemon。
按命令组或一类 import 边拆小 PR，独立源码/测试并行，公共台账/catalog/router/投影串行集成。
终态：import baseline 无未登记业务依赖；当前独立 exceptions 机制不允许与 baseline 重叠，
所以符合精确理由的例外独立登记、其余 import baseline 清空。legacy 方法和前缀清空，
或每条残余另有逐项审定理由；不得仅因存在 MethodSpec 就声称 CLI 本地业务消失。
每个小 PR 均同步契约/示例/准确台账与适用 digest，证明输出、错误和退出码兼容。
`cli-input-output` 仅按文件、模块、符号和次数精确批准客户端输入、传输和呈现职责。
原 16 条客户端职责核查中，此前已精确登记内置白名单模板原文展示与 follow 的本地信号/输出 EOF 两条；
已合入的 #2807 再逐项登记 squire 警告与已解析本地 operator 的 URI 字符串编码、IPC transport 与 socket endpoint 选择、overview 名称展示、OrgFS 与 route 输入语法、用户指定及默认 home 根选择八条。其余六条仍涉及身份、筛选或业务选择，须继续迁移或逐项审查。
这十条仅限各自 manifest 条件，不构成同类函数的通用许可；operator URI 编码不授权 `resolve_node_owner` 读取或身份判定。
另有已由 #2808 合入的两条客户端词汇：org 命令的既有前缀帮助与参数规范化，以及离线列出安装包内置 routine 模板名称；它们不属于上述原 16 条职责核查，且不授权 org 语法规则或模板渲染业务。
本次候选另精确登记九条生命周期请求等待预算常量边：`down`、`agent.start`/`lifecycle.start`/`lifecycle.start-smolvm` 和 user-proxy 的现有 request timeout 表达式各引用三项常量；原 resume 的额外等待预算保持原式。这只登记传输等待词汇，不授权启动或停止策略、代理选择、payload、重试、新 request 或离线 fallback，也不是业务迁移。按固定 dev `d57766c0` 的候选差量，import 为 **332 / 383 → 323 / 374**，exception 为 **105 / 107 → 114 / 116**；合入前不是当前 dev 实数。ratchet 锁定精确 import 与 digest，不证明未来调用点的语义，后续仍须源码审查。
不授权 `render_template`、`load_routine`、任意外部文件读取、业务 store 读写、本地事件投影或读取、daemon 控制，也不作为离线 fallback。
确需行为变化的切片保留先失败后通过证据，并在 PR 正文声明 Contract-Impact、同步 contract/cli。

## 最新授权

用户确认：`#2727已合并，#2763 方案批准。`
用户还明确授权本任务所有 PR 在被无关失败阻塞时强行合入。
执行边界：先尝试正常合并；只有取得真实 job/日志与 base 对照，证明失败不由本批引入，
才允许 force_merge。不能把 pending、没有执行、缺少审查或本批引入的失败自动视为无关。
在该 PR 正文记录 exact head/base、失败归因、已通过检查、UNKNOWN/BLOCKED及强合理由。
保留分支；不做未经本任务授权的发布/部署，不修改分支保护或 CI 准入规则。
批准覆盖原方案的条件内例外；某项条件尚未证明时继续按业务迁移/待删处理，不扩大许可。

## #2727 后的历史执行基线

`2a494edccdee0135b0a4dbb6fd7f7cf755b803f0`，为 #2727 实际 merge SHA。
旧设计附件继续保留 `533ef27c...` 的审计快照，不用它冒充实施时最新 dev。

| 项目 | 当时读数 |
| --- | ---: |
| MethodSpec / HTTP paths | 153 / 124 |
| import triples / occurrences | 417 / 470 |
| 精确 exceptions | 20 |
| legacy 方法 / 前缀 | 175 / 7 |
| legacy 中已同名声明 / 未声明 | 87 / 88 |

与盘点基线比较实际新增15个声明、变更9个声明；新增为 adapter.authorization 四方法、
adapter.onboard 三方法、adapter.identities 两方法、adapter.routes 四方法、adapter.register、
agent.provider.auth.list。HTTP路径由102变124。两个 legacy/import 台账均未缩减。
AGENTS.md 与盘点基线一致；新基线结构门、模块边界门 PASS。
SSH在当前环境不可用；工作副本由已认证Forgejo API归档恢复，Git tree与完整commit SHA
逐字节校验，HEAD等于真实merge SHA。通过API提交PR，不用不同仓库或合成SHA冒充dev。

## 当前 dev 与实施进度（2026-11-14）

本节以实际 dev `089ec7871872a670612c6bbb494f4ba0756d9760` 为固定对象。**Goal 仍为 ACTIVE**；下列 dev 实数不是尚未合入的源码候选读数。

| 指标 | 该 dev 对象实数 |
| --- | ---: |
| import baseline 三元组 / occurrences | **332 / 383** |
| 精确 exception 条目 / occurrences | **105 / 107** |
| legacy 方法 / 前缀 | **97 / 7** |
| MethodSpec / HTTP paths | **153 / 124** |

[#2807](https://code.hyprial.com/HyprialOS/hyprial/pulls/2807) 和 [#2808](https://code.hyprial.com/HyprialOS/hyprial/pulls/2808) 已分别合入 `8cb5097b0655e15b5a86537e210e1c98e44cde16` 和 `2caa56d1f45c8f643a30f78ef6623866ce773848`；其后 #2802 将 dev 推进到 `089ec787`。目前共 85 条 import 边的精确分类登记与 78 条已声明 legacy 方法的 permit 退场，是收紧并明确已有台账，不是将对应业务迁到 daemon；每个 exception 仍仅按自身条件生效。

18 片源码组合的固定测试对象是 `c5a1e12eef0aa79cd96655380e4326d9e004fc72`，不是已合入 dev。原始全量分片 1、2、3 均自然退出失败，分别为 **15、2、4 failed**；其中 **19 项为本任务相关的测试资产失败，2 项在固定 base 对照中复现同症状**。分片 1 的 15 项包含 plan 预加载清单与 org 完整列表断言；分片 2 的相关项为 TOP 样例 metadata guard，另有 Docker pause 的 base 同症状；分片 3 有三项旧测试调用或完整列表断言及 `/private/tmp` base 同症状。base 同症状不等于已经证明环境根因或 CI 必然一致。

针对前四个受影响测试文件，tests-only 修复对象 `3ad5384224bd7866388be87430cc95ff0d1fafee` 已取得 **321 passed** 焦点证据：保留原 293 个节点并增加 28 个 TOP metadata 负控；其源码与契约树同 C5。它不能改写 C5 原始失败或代替其他文件、全量收集与剩余分片。**lane 4、isolated lane、其余四文件预计 93 节点焦点及最终 collection 仍待完成**；任何 pending、skip 或未执行项都不是 PASS。

`ed92bac0c487733e29eeb8ca70217f7089b0631b` 的备份分支 `intg/cli-daemon-request-test-repairs-backup` 的头 `3d12abac8de86b40797eab4a7781a2397ae249e3` 与 `51faa49e16bab0eec6855c6e8377778fe93b2f7e` 的备份分支 `intg/current18-recovery-2caa-51faa` 的头 `7fbc7c45cdf0f2a896acaa063f869f8ae8496c27` 是恢复点，不是合入记录。#2803 仍为 WIP，已从 `089ec787` 重基到 `cd78f9bf3835059dadf66df0cb498cb7efea18c8`，精确涉及 16 个路径；GUI 完整声明数仍硬编码 153，修改禁改路径尚待用户许可，不得将该重基或备份冒充 dev 合入。

18 片组合预期的 **312 / 361 import、70 / 5 legacy、181 / 124 MethodSpec / HTTP** 是候选结果，须由最终实际集成对象、完整验证和逐片合入确认，不能写成当前 dev。源码变化需重新完成适用全量；契约变化须重跑相应契约门；若仅测试资产修复，也须保留 C5 原始 FAIL、证明原节点不丢并以完整受影响文件与负控取得独立证据。

## 18:49 历史进度快照（2026-11-14）

原验证证据快照截至 2026-11-14 18:49 UTC；其中 #2807、#2808 的合入为之后补记，最新状态以上节为准。

当时 dev 为 `9a6030b6f90f5bc95fb70c2ac85ecb838999e2d2`。#2799 后外部依次合入 [#2760](https://code.hyprial.com/HyprialOS/hyprial/pulls/2760)、[#2762](https://code.hyprial.com/HyprialOS/hyprial/pulls/2762)、[#2756](https://code.hyprial.com/HyprialOS/hyprial/pulls/2756)、[#2758](https://code.hyprial.com/HyprialOS/hyprial/pulls/2758)，涉及 GUI、原生客户端与打包线；本任务没有修改这些产品源码。四个外部 merge SHA 依次为 `9b9073276a79dd2afc36ef2e5a8c93fafc98e74b`、`619ca7742d98bd2b56b691f634652d335fa9f500`、`29def2b0b34900295feb21d987771b4cd05a0346`、`9a6030b6f90f5bc95fb70c2ac85ecb838999e2d2`。下表直接读取该 Git 对象的 import、exception、legacy 台账与 OpenAPI，registry 声明数按固定对象的真实绑定统计。上节 #2727 后 `2a494ed` 保留为历史执行基线；第一阶段 `533ef27c` 的 417 边分类是审计快照，不重写为今日实数。

| 指标 | 当时 dev 实数 |
| --- | ---: |
| import baseline 三元组 / occurrences | **342 / 393** |
| 精确 exception 条目 / occurrences | **95 / 97** |
| legacy 方法 / 前缀 | **97 / 7** |
| MethodSpec / HTTP paths | **153 / 124** |
| 架构第三账本 findings / occurrences | **528 / 638**（含外部 PR 变化，不全归本任务） |

已合入的 #2807 以 `eadb831d75fa574921e4b2641d3719b8ba4ea3c4`（包含此前 #2804 文档与 #2801 GUI 合并）为固定基线；上述 `9a6030b` 表是原有进度快照，继续保留。该固定基线的 import / exception 台账分别为 **342 / 393**、**95 / 97**；该 PR 精确转移八条后分别为 **334 / 385**、**103 / 105**。该 PR 是条件性客户端职责登记，不声称这些命令的业务已迁移，也不代表其他候选源码已合入 dev。

本次另两条词汇登记以 #2807 后固定基线 `8cb5097b0655e15b5a86537e210e1c98e44cde16` 为准：import / exception 台账由 **334 / 385**、**103 / 105** 分别变为 **332 / 383**、**105 / 107**。这两条词汇登记现已由 #2808 合入；原 16 条核查的 2 + 8 已登记与剩余六条需业务迁移或逐项审查的结论不变。

从 #2763 批准后至此已合入 #2771、#2776–#2780、#2782，以及下列 #2784–#2799。下表 SHA 均为 `merged-pr-N.json` 的真实 merge commit，与固定 Git 对象核对；协议词汇登记、已声明历史许可退账和实质业务下沉是不同进度，不能互相充数。

| PR | 合入 SHA | 实际范围 |
| --- | --- | --- |
| [#2784](https://code.hyprial.com/HyprialOS/hyprial/pulls/2784) | `fa62ff9ff6dc143909ebf264f5abf7884af86fd4` | TOP 响应字段词汇精确登记；进程采样仍待 daemon 化 |
| [#2785](https://code.hyprial.com/HyprialOS/hyprial/pulls/2785) | `38a6de9e8d78eca6b52adcd4225608b5de0a068e` | adapter 已声明方法的历史许可退账 |
| [#2786](https://code.hyprial.com/HyprialOS/hyprial/pulls/2786) | `9fecf02f46c6da6823ab069ec0a5c39b6dbf3b7e` | agent IPC 错误词汇精确登记 |
| [#2787](https://code.hyprial.com/HyprialOS/hyprial/pulls/2787) | `6f7c1bded954ec4ae12136c35ee17f8facce28a2` | core IPC 错误词汇精确登记 |
| [#2788](https://code.hyprial.com/HyprialOS/hyprial/pulls/2788) | `3c14abd29987c540a43e54387b760c3ac89e47fd` | agent 已声明方法的历史许可退账 |
| [#2789](https://code.hyprial.com/HyprialOS/hyprial/pulls/2789) | `761fcef13e065761aed67d177c67dcf77671401e` | 其余 IPC 错误词汇精确登记 |
| [#2790](https://code.hyprial.com/HyprialOS/hyprial/pulls/2790) | `13feebb65c94908c2b740fb88a68e0bd21b852f6` | identity 已声明方法的历史许可退账 |
| [#2791](https://code.hyprial.com/HyprialOS/hyprial/pulls/2791) | `7cffd313c67c3294314f24c1f735233958a2ea8f` | link 已声明方法的历史许可退账 |
| [#2792](https://code.hyprial.com/HyprialOS/hyprial/pulls/2792) | `778811c223f7ade16a654e2213c3d5ad59ae9b10` | message 已声明方法的历史许可退账 |
| [#2793](https://code.hyprial.com/HyprialOS/hyprial/pulls/2793) | `e2f834cd12de58dcceaf95f99804c18840c05765` | 诊断协议词汇精确登记 |
| [#2794](https://code.hyprial.com/HyprialOS/hyprial/pulls/2794) | `727b3c3fec49269baff447b9a53d3f3917180565` | org 已声明方法的历史许可退账 |
| [#2795](https://code.hyprial.com/HyprialOS/hyprial/pulls/2795) | `3f9edf42db05fc08ce92273d9a309c9a043a526c` | 本地 daemon launch 结果类型的精确边界登记 |
| [#2796](https://code.hyprial.com/HyprialOS/hyprial/pulls/2796) | `8121f3cdab8fc911edb5d00484455884d47883d3` | orgfs 已声明方法的历史许可退账 |
| [#2797](https://code.hyprial.com/HyprialOS/hyprial/pulls/2797) | `71b18385889f7246d9680aab6e46671c4b6ec770` | 本地呈现职责的逐条例外登记 |
| [#2798](https://code.hyprial.com/HyprialOS/hyprial/pulls/2798) | `c72da008d20750edcfbe741e0ab7c3426770d5a2` | routine 已声明方法的历史许可退账 |
| [#2799](https://code.hyprial.com/HyprialOS/hyprial/pulls/2799) | `981e74f62b7719eb41fce600c4557aa91482668e` | views 已声明方法的历史许可退账 |

这 78 条已声明方法的 permit 退场收回历史许可，**不代表增加 MethodSpec 或删除全部 daemon 业务分支**。当时的 97 条 legacy/7 前缀仍须逐项收敛；这一读数在上面的当前 dev 表中延续。`cli-input-output` 类别目前只按已审路径与条件登记，不能据此授权任意业务读写。

### 当时未合并的 12 片产品候选（历史）

固定候选 `9a59766a5dfc7c2a21bb9eead3b80906987908fd` 是以 #2799 merge SHA `981e74f62b7719eb41fce600c4557aa91482668e` 为祖先的 12 个原子提交组合，**尚未合入 dev**，也尚未对当时 dev `9a6030b` 完成集成验收。按候选 Git 对象直接读取，预期 import **337 / 386**、legacy **73 / 5**、MethodSpec **174**、HTTP paths **124**；这些不能列作 dev 实数。12 片分别是 identity binding、network forwarding、service connection、routine audit/pause/resume、adapter configuration、adapter pin 死分支、message.send/progress.list/targets 死回退、maintenance status 死分支、user binding audit、routine planning、upgrade execution 死分支、outbox list 声明。实质 CLI 业务迁移、声明补全与死分支删除须按各片自己的 diff/门禁归账，不把数值差额当行为证明。

当时的 12 片排除 `top.inspect` 与 agent grant what-if；它们其后进入独立施工并纳入上节的 18 片候选。outbox.list 的非对象参数样例与真实 socket 错误 `INVALID_ARGUMENT: params must be an object` 已对齐，并补恢复、worker relay 与缺 binding 负控；这不改变 handler 或 validator。当时要求最终树核 routine schema 错误的精确例外条件和 digest，以及 adapter pin 三项许可的准确差集。

前序 `e8bb` 同源组合焦点记录曾得到 **829 passed、2 warnings**；`9a59766` 的后续差异主要是三条 permit 补删，这 829 仍不是 9a 的复跑结果。9a 相关的独立退账焦点得到 **243 passed、2 warnings**。基于 `981e74f` 的候选门禁记录中，Ruff、import ratchet、method registration、request contract、daemon request architecture、module boundaries、structural limits 的相应检查为 PASS；契约 diff 为 **153 → 174 MethodSpec、breaking 0、compatible 21、notices 0**。这些焦点与静态门不能替代全量 pytest 或最新 dev 集成验收。

**当时的 9a 尚未就绪。** 真实新增的 18 个 daemon 源模块尚未登记到 native resident 清单，固定候选焦点复现 **2 个 RED**：`test_t11_daemon_tree_never_imports_login_or_names_the_credential` 和 `test_daemon_inventory_matches_live_tree`。network 样例中的 CGNAT 地址触发 intranet gate **8 个命中**，归 network 样例片；另两个 network reachability FAIL（`test_checked_in_baseline_keeps_repository_gate_green`、`test_send_and_lookup_remain_visible_to_network_gate`）归 dead-message 片：旧分发删除后留下 **8 条 stale reachability fingerprints**，且旧根追踪断言仍要求 `root.handle` 直达 `_ipc_message_send`。两因各按原子片修正样例、指纹和测试追踪，保留未知拒绝，不能为通过而放宽 scanner。上述修正后再在实际集成树重新验收。

旧 C6 组合 `c6c17fec6409bd64f400f7bc5132bb401ce8f729` 的唯一全量命令 `uv run --with libcst==1.9.0 pytest tests/` 已以退出码 **1** 结束：**29 failed、27,601 passed、173 skipped、8 xfailed、1 xpassed、16 warnings、1 error，耗时 9,619.08s**。29 个 FAILED nodeid 与一个 teardown ERROR 已保存，尚未逐项与固定 base 在同条件对照。C6 不能报告全量 PASS，也不能把这些失败自动归为基线；当时的候选 9a 已知 RED 需独立修复并重新验收。后续 18 片组合与 C5 分片结果见上节，不将旧焦点冒充最终对象的全量结果。

已记录的历史基线失败仅限 [KP-061](../../reference/known-problems.md#kp-061-云工作机并发全量-pytest-的中断与基线归因限制) 的固定 `2a494ed` 对照：去重 11 nodeid 的焦点复跑为 5 PASS / 6 FAIL / 1 teardown ERROR。六项同症状是 QQ token 后台任务/FD、daemon exit backstop 8s 超时、Pi rig unavailable、daemon application cleanup 30s 超时、Lark blocked SDK 两个参数分支；另五项当时归因 UNKNOWN。#2780 的 mock 失配已修并有基线/候选各 555 PASS 的焦点证据。这些旧结论不能自动套给 C6 的 29 个失败；其 nodeid 已有最终清单，仍需逐项对照。`test_real_ipc_socket_routes_service_request_to_manager` 的 `/private` 权限失败另已在固定 base `25835754bab1994d1559fd97b9537ec86b21e7ef` 单节点复现为 **1 FAIL、同症状**；这项证据不能推出其余失败无关。`contract/daemon-lifecycle` 缺 actor_host 按 KP-057 保留失败事实，不把适用门跳过当 PASS。

后续固定 base `25835754bab1994d1559fd97b9537ec86b21e7ef` 对照中，Codex 版本格式与 TEST-NET 黑洞案例也同样失败。agent_home、GUI 权限和 wheel 缺 token 的三个节点在继承 token 且 `umask 0077` 时为 **3 FAIL + 1 teardown ERROR**；测试子 shell 改为 `umask 0022` 并移除 `GH_TOKEN`/`GITHUB_TOKEN` 后为 **3 PASS**。对照没有读取 token 值，也不能据此声称 CI 默认环境如此。DSH 两节点轻负载复跑 PASS，但时序归因仍是 UNKNOWN；hosted 案例在固定 base `25835754bab1994d1559fd97b9537ec86b21e7ef` 的有效本树 venv 单例为 **1 PASS**，旧 C6 长套 FAIL 仍为 UNKNOWN。

结构门另有已知维护债务：`tests/integration/native_harnesses/test_native_resident_tripwires.py` 的 `NATIVE_ORIGINS` 已约 1000 行、同目录 7 文件，当时 9a 的 18 个真实 daemon 模块须按现门逐项登记；后续候选已补登记，最终仍须核实。`audit_actor_rustify_ci` 仍要求单一 dict literal、旧 key/value 源 span 与顺序及字典外字节不变；当时只批准字典内删 8 条旧 inline 注释和少量同类邻项 reflow 腾行，然后逐行加 18 项。未来另设计跨文件静态有限 inventory 审计，保持 append-only、未知项拒绝和旧项逐字守卫；本次不改 scanner/门或扩大准入。

### 当时记录的后续施工顺序（历史）

当时计划在 12 片候选与当时 dev 对齐、完成真实全量归因后，独立推进：`top.inspect` 将 CLI 本地 ps/子树采样迁到 daemon，`--json` 继续原 `top.snapshot`；agent grant check/visibility what-if 保 guest 纯模拟与真实 actor/sessionRef 分离；`agent.resolve` 是下一只读 MethodSpec 薄片，保别名/模糊目标错误与原参数宽容。三条线各自源码、契约、示例、CLI 输出/错误码与恢复/可信身份负控验收，不混入上述 12 片数字。再排 daemon 生命周期、首次 init、升级安装/自动更新的高风险阶段拆分；只对 daemon 不存在、已证实停止或外部安装所需的本机操作审精确边，daemon 可服务时业务经 request。日志筛选与 routine add/remove/set 的动态错误闭包仍需独立设计和 Contract-Impact。

## 不变红线与验收

禁改 gui/、src/hyprial/shell/impl/gui/、contract/daemon-request/contract_mock/、clients/、packaging/，
commands/gui也不抢写。禁止扩大baseline、skip/xfail、kernel/shared helper业务绕门。
`daemon-request-architecture-baseline.json` 是独立于 import 和 legacy 的第三本架构台账，
须单独实跑 `check_daemon_request_architecture.py` 并按真实基线精确减账；不得扩大。
该检查器的 `REVIEWED_BASELINE_SHA256` 是首次 seed 审核锚，不随正常减账重写；
`check_import_ratchet.py` 的 `REVIEWED_EXCEPTION_DIGEST` 则须与审核通过的
exception manifest 内容变化同步重算，两者用途不同。
新方法成功/空/错误示例完整，handler真实，IPC参数容忍/错误顺序/可信身份/恢复期准入保持。
service.list/guide会persist catalog缓存，不能声明纯只读；identity绑定必须保留operator和peer PID校验。
所有 Python 经 uv。每批焦点→适用门，src变更追加全量pytest/Ruff；home/socket/cache隔离。
失败给出真实结果与归因，BLOCKED/UNKNOWN不是PASS。每批由sol验收后进入合并动作。
