# CLI 命令面与 agent move 跨线协调记录

- 日期：2026-10-28（Asia/Shanghai）。
- 状态：**A 已交付固定消费基线；用户确认 B 保持独立容器，可基于 A 继续后续工作，分支关系见 §11。**
  §1–9 为历史协调记录；A 当前执行状态以 §11 及交接开放项为准。B 业务方案和评审仍由其本线负责。
- 本记录补充 [design.md](design.md) 及两份盘点附录，保留它们对旧基线的事实记录。
- daemon request 设计：[#2010](http://git.internal.example.com:3000/HyprialOS/harness-bridge/pulls/2010)，
  原审阅 head `7dc18fadbb8077975483ca9f78c6fd2d7d0b0103`。

## 1. 本轮用户明确的要求

用户转述 Allen 的建议：单独梳理 CLI 命令面改造的集成线，再分别协调 daemon request 和 move。
用户认可这个方向，并要求先与 agent-transfer 线的 codex-sw 核对。

同步边界是：只接收用户已确认、讨论定稿的结果；未定结论不能作为开发依据。
等对方提供确认后的完整汇总，再与用户统一讨论两线的开发安排和节奏。
这不等于已经批准具体分支名、owner、合入顺序、版本承诺、任务派发或产品实施。

## 2. 已完成的沟通与材料核查

已通过 Harness 普通消息直接联系 codex-sw；没有创建 PAC 派单图。

- 对端：`agent:amy.williams@example.com:devusr-dev:codex-sw`。
- 请求：`0df7ca7e-7fec-5391-80f8-41626b1712ef`，服务端已接受投递。
- 回复：`507659e8-a72b-529b-b230-3fe5fbf38038`，已读取并 ACK。
- 对端明确结论：**agent move 尚未形成可供两线排期、冻结消费者的完整定稿。**
  #2004 R3 是待审提案，reviewer PASS 不能代替用户批准；完整汇总待用户确认后再发。

核对材料：

| 材料 | 固定坐标与本轮用途 |
| --- | --- |
| [#2004](http://git.internal.example.com:3000/HyprialOS/harness-bridge/pulls/2004) | `design/agent-transfer-refactor-plan-20261027` → `intg/agent-move`；`e4a29f40e91815bf4711aae1e52bae678a442a14`；仅核对当前 R3 状态，不能当作已批准接口 |
| [#2006 主设计](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/c9242a9a4321dd748387ba1115d10f2b54890160/docs/design/design-agent-move-2026-10-27.md) | 已完整读取；用户指定的审核基准。按 §0 的更正、§4.45、§4.5、§8.1 理解，不能照早期段落或旧 PR 正文实现已撤回的链接令牌、新传输族 |
| [#2006 handoff](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/c9242a9a4321dd748387ba1115d10f2b54890160/docs/notes/handoff-agent-move-devusr-2026-10-27.md) | 已完整读取；核对既有任务/验收基准。新命令树与其差异仍须评审，不将旧任务顺序自动当作两线的新排期 |

## 3. 已知边界与未决问题

codex-sw 转述的用户已确认方向包括：daemon request 为业务调用出口；CLI 负责参数、交互与呈现，
move 领域负责业务，identity/runtime 状态由各自权威端口处理。迁移动作归 `agent move` 族，
但具体 request/invite/start 命令树、参数、输出及旧 host-invite 去向尚未定稿。

#2006 的已记录裁决包括：同 org，复用 OrgFS/Zenoh，不扩大 SSH 权限，不新建 move 传输族或链接令牌；
授权与 operation 幂等、目标 agent 本人自报、节点接收策略与 #1982 协调。
这些方向不能代替公共接口定义或真实验收。SSH 退役仍受 §3.3 的 delete_when 约束。
smolvm 的条件退役是对端转述的计划约束，不是本轮立即删除授权。

| 待确认内容 | 对双方的影响 |
| --- | --- |
| move/request/invite/start 的最终命令树、确认与输出，旧 host-invite 的处置 | 公共 CLI 注册、帮助、错误、生成命令、skills 和测试必须使用同一份确认后的命令面 |
| move 所需的最小 daemon request 方法与公共 client | 决定两线实际依赖；本设计的拟议 `transfer.*` 名称不能抢先成为契约 |
| IPC 预览 operationId 与 move 双方授权/业务 operation 的关系 | 不能机械视为同一个确认；需明确各自权威、绑定与生命周期 |
| actor 实例证明、ACL/host 策略、凭据隔离、resume 收据及恢复、rustify 契约 | 影响能否真实迁移，不得用占位接口或仅 CLI 测试当作完成 |
| move 的既有 0.5.5.2 目标与 daemon request 的 0.5.6 目标如何衔接 | 最小依赖、联合版本与节奏尚未统一；不自行改版本承诺 |
| 独立 CLI 集成线与两条功能线的具体分支关系、共享文件 owner、合入顺序 | 本轮只记录方向，待确认汇总齐备后与用户讨论 |

## 4. 对 #2010 的直接影响

原 [CLI 清单](cli-inventory.md) 已包含 `agent move`、19 个 transfer 族入口和 `agent host-invite`。
缺口是尚未单列两线共用的 CLI 命令面、公共入口的修改归属与集成顺序。

在 #2010 的基线上，`runtime/entry.py` 统一注册 move 与隐藏 transfer，注入其依赖并导出旧测试绑定；
`commands/transfer/move.py` 仍直接编排后端迁移。这些是共同受影响的现有代码，
不是只把 DR-28 标为“最后迁”就能解决的协调问题。

DR-18、DR-28、DR-30/31 和对应拟新增方法须在定稿后一起复核。
33 个任务 PR 的表仍是 daemon request 旧提案，不作为联合实施台账；不按旧清单固化即将改变的入口。
零兼容层、单向依赖等已确认目标仍保留；目录搬迁时点及联合节奏的调整留到统一讨论。

## 5. 下一次汇总所需输入

等待 codex-sw 按用户确认结果提供：命令面、领域/公共接口归属、最小 IPC 契约与确认边界、
保留/删除范围及验收，以及每项的确认依据和准确文档/PR/commit。
收到后形成一份“现有入口 → 定稿入口 → 实现归属 → 消费者 → 验收 → 前置”的对照，
再与用户讨论分支和节奏。本轮没有改产品代码、分支拓扑或派发实现任务。

## 6. 用户最新确认：进入 CLI 命令树设计讨论

用户随后明确：“agent-transfer 线功能已经基本定稿”，并要求我按既有总结与 codex-sw
讨论 CLI 命令树设计和规则。因此现在以已基本定稿的功能为输入共同设计命令面，
不再把“CLI 命令树尚未定稿”当成不能开始讨论的条件。

已向 codex-sw 发送消息 `d84f7d26-0a1e-53ed-aa8d-f3ef9ea1157a`，服务端接受投递，内容包括：

- 已有硬边界：CLI 参数/交互/渲染、request 业务出口、领域权威、零兼容层、单向依赖、统一输出。
- 命令树规则建议：资源组/动作/标识分明，不让 agent 名与子命令争同一位置；
  move 族内具体命令按最新功能确定，不自行将 host-invite 等同于 move invite。
- 分清本地交互确认、IPC 预览 operationId 与 move 双方授权，不用 `--yes` 替代授权，
  也不强加与功能定稿重复的确认步骤。
- 逐命令对齐参数、执行主体、最小 IPC、状态前置、幂等/取消、输出/退出码、
  旧入口处置与 argv/hooks/skills/tests 消费者；内部 IPC 不必全部暴露成 CLI 命令。
- 公共注册/依赖注入/output/helper 的改动归属要统一；命令面改造与物理搬目录分别安排。

等待对方提供最新功能文档/commit 和确认范围并讨论具体命令树。
原 `transfer.*` 拟议方法与旧命令表不约束新方案；功能基本定稿不等于双方已经完成接口或排期确认。

## 7. A/B/C 归属、abc 前置与用户开工门

用户随后确定 A/C 由本线负责，A（统一 CLI 命令面）和 B（move）面向 0.5.5.2，
C（完整 daemon request）面向 0.5.6。A 提供公共基线后 B/C 并行，B 不等待 C 全量完成。
原交接 a（#1967 公共 client）、b（完整设计）、c（声明/OpenAPI/棘轮/样板）纳入 A 前置交付，
b 同时保留 C 总体设计。具体范围和验收已留在 [cli-surface.md](cli-surface.md)。

用户先强调“请不要开始开发”，随后说明顺序：design PR → 规划任务/挂 Kanban → PAC 并行。
最新明确：“完成留档之后，intg-a 可以先行，不用等 allen，符合 pr 要求即可”，“我同意后就可以开始”。
因此 **A 设计留档后，必须再取得用户对方案的明确同意；无需额外等待 Allen。**
C 原设计审阅要求保留。本轮仍无产品实施或派工授权，不能将这次流程确认记成具体方案已获准开工。

codex-sw 已提供 R3.1 固定 head `275573d88d86b1e06572d39344dfddbe3fdfe250` 和具体命令讨论：

- 首封 `e0a0172d-6201-5975-916a-f1420cf0dc7d` 是回复消息，已 ACK；实质答复另发
  `d9f4f654-61a9-5a9f-a114-befe248d55b0`，同步 A/C 归属与最新审批条件。
- 请求 `368a796a-1c92-5b8c-9267-a005b525e1a2` 已回复为
  `8f991359-8b28-5e72-a462-a03d0709e6f0`：发现意向不能预支 bundle ACL，筛选选项不能含糊复用。
- 请求 `b610c22a-8d2d-5e56-a709-4bf884f52eea` 已回复为
  `76ad20e2-da1d-5b0a-b2f7-0f73071fcf11`：先以显式 revision + 独立 revise 作为与 R3.1 对照的保守提案，
  不可变计划/重开单只保留备选；两者均不冒称用户已逐项批准。

该轮的 move 命令候选为 9 动作、11 调用形式，已由 §8 的 R4 选择取代；
原稿保留在 [#2010@485dab4db](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/485dab4dbaee3c1af8bf8f9adcfc375a46ca7343/docs/design/daemon-request/cli-surface.md)。
公共 CLI/client/声明/output 归 A，move handler 与状态机归 B，其他组迁移和最终目录/边界收口归 C。
本次仅同步和留档设计；没有 rebase #1967、建立 A 实现容器、登记实施工作项或启动 PAC。

## 8. R4：用户选择不可变迁移单与八动作

codex-sw 通过请求 `c0ff05fa-2268-5213-bf3f-3fe02b989c46` 转达：在其推荐不可变单/八动作并解释
改条件另建单、不带 --revision、不提供 revise 后，用户确认“可以，整理方案，然后可以通知daemon-request了”。
本线核对的仓库依据为：

- [#2004 R4](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/3ac5381d00f53e275a998b451ac916dfedd23d25/docs/design/agent-move-refactor-proposal-20261028.md)，
  head `3ac5381d00f53e275a998b451ac916dfedd23d25`，base `intg/agent-move@3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 文件 `docs/design/agent-move-refactor-proposal-20261028.md`：621 行，
  SHA-256 `4046363fd73ab25d204a8f76df777c9f67f6d6bc2714fc1595044808933c863b`。
- 固定提交只新增该设计文档；服务器 PR 仍为 Draft/PLAN ONLY。
  R3.1 的评审结论不继承到 R4；本线的同步核对不代替独立评审或真实迁移验收。

活动命令为 check/request/invite/list/status/start/cancel/retry：8 动作、10 形式。
moveId 固定绑定唯一 planDigest；已有单确认和 start 不再有用户版本参数。
改条件另建单并重新取得双方同意；不继承旧授权，不绕过旧执行围栏。
不再把九动作/显式版本或不可变单作为并列待选项；此前消息和固定 SHA 仅供追溯。

[A 提案 §4–5](cli-surface.md) 已同步活动命令表和八个最小 IPC 方法候选。
方法名/schema、canonical digest、TTL/幂等/替代单、preview 配对、可信身份/能力探测、
阶段错误/状态与跨权威竞争仍须共同冻结；`--replaces` 仍是 R4 的关联语法建议，不冒称已批准的稳定参数。
R4 §10.5 的旧 18 入口处置表是设计归属，逐文件消费者与精确替代方法仍待补齐。

这次选择没有改变三线分工与实施批准条件。用户另要求给出 A 的具体任务规划和 Kanban 卡数，
因此 [A 提案 §7](cli-surface.md) 列出 **5 张实施卡/5 个 PR** 的候选范围、依赖与验收；
#2010 是共同前置，不重复计卡，intg 汇总 PR 不另算实施卡。
本轮只更新设计并回传契约缺口，没有创建卡、登记实施工作项、改产品或派 PAC。

## 9. A/B 任务计数与依赖去重（规划，未建卡）

codex-sw 请求 `301dc388-786d-5cdd-91cd-55ece219d955` 对齐公共依赖卡粒度，已回复
`08df5f92-e810-5b92-9224-879ec0f2f29a`：A 仍为 5 张实施卡，A-01…A-05 是规划编号，尚无 Kanban UUID。
client、公共注册/DI/output、声明/导出框架、样板和棘轮由 A 交付；B 引用这些卡，不重复建设。
move 专属 MethodSpec、handler、状态和消费者归 B；公共接线改动在 B 的真实命令组 PR 同批完成，A 负责接线核对。
A-01/02/03 构成最小功能集合；当前计划在 A-04/05 补齐 CI/安装验收后固定正式消费 SHA，不拿文档或传输层单独可用代替完整基线。

对端补充请求 `ee5e3244-9e60-5ecc-aa85-84017cc36823` 报告 B 候选 18 个工作包：
14 个覆盖首轮 Squire/Pi 闭环，另 4 个为 SDK、原 Claude S5、SSH/旧入口条件退役、smolvm 决策与条件退役。
这是 B 的规划口径，尚非用户批准或净新增卡数；各域公共端口须核对既有卡/owner，不都塞入 move 分支。
去重前不能将 A 5 + B 18 宣称为要新建 23 张卡，首轮 14 也不能替代全部发版门；C 后续迁移另计。

同一消息转述 R4 reviewer 的 `660ad02f` 判词仍需三项设计补件：Q1 六项确认展示、
Q2 ACL/runtime 具名承接及单据存储责任、Q3 替代单关联与竞争。
本线尚未收到判词全文与绑定 SHA，以上仅标为对端报告，不宣称独立复审 PASS。
补件须留在设计/契约收口并获得相应确认，不能仅因列进首张实施卡便算关闭；不改变 A 的 5 卡范围或各线开工门。

## 10. 2026-10-29：A 可消费基线与 B 的 Q1 补件同步

本节更新前述规划阶段的执行状态；§7–9 的“尚未实施/未交付”保留为历史记录。
A-01…A-05 已实施并整合，固定产品基线
`278a53a8cae67a73ff664d720aad069033e97ea7` 已具备安装验证和独立可消费结论。
这不等于 #2015 已获准合入 dev：热修不可变 tag、随后 dev 同步、五项命令树归组及最终候选独立验收仍按既定次序推进。
历史全量 FAIL 与 OrgFS full-only UNKNOWN 保留，详见
[当前交接与开放项](../../evidence/cli-command-surface/a05-final-handoff-open-items.md)。

codex-sw 在请求 `c61a6e68-fd91-5081-b8c3-600b5bd9a2f0` 中报告：

- 已阅读 A 在 `03f253e9408ad4c1163926e0898e4e4b08498f1b` 的消费基线与交接记录，
  接受上述固定安装基线，不再以“A05 未交付”作为 B 的当前状态。
- [PR #2004 的 Q1 补件](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/9d5110cafd10d1d506d3181ebe2c63cbe424af1e/docs/design/agent-move-refactor-proposal-20261028.md)
  已推送，精确提交为 `9d5110cafd10d1d506d3181ebe2c63cbe424af1e`，已交 cc-sw-review 复核。
  对端报告仅原设计文档 +18/−4：§4.1 六项确认信息与原凭据分类共用一份清单，供两种发起顺序引用；
  缺信息仍为 UNKNOWN，不新增 alias、`--yes` 或第二套授权。
- Q2（具名责任与单据存储）及 Q3（replaces 一致性）仍开放；B 的 18 个工作包仍是规划，未建实施卡、未启动实施。

上述首次同步仅登记对端报告；当时 Q1 尚待指定 reviewer 判词。最新结论见下文补充。
A 公共输出、声明和接线的归属保持不变；B 的领域授权与技术前置不因 A 可消费而自动关闭。
没有新增产品任务、重复派发 A 工作或启动 B/C 实施。

### Q1 独立判词补充（2026-10-29）

codex-sw 随后以请求 `fd3a85c9-4274-5e29-aec0-bcf167982e58` 同步 cc-sw-review 的
判词消息 `8cee8e65-1531-5606-a327-e0aed54a8d07`。本线已实读判词文件
`plan-review-2004-r41-20261029.md`，并核对其 SHA-256 为
`c9c5d9c8479d0c4a97b213ae0a9eae78b97d21d1294727351fdb285a92fef657`，与申报一致。
判词明确绑定上述 `9d5110cafd10d1d506d3181ebe2c63cbe424af1e`，确认六项清单齐备，
两种发起顺序共用清单，且无新增阻断项。因此当前状态更新为：**Q1 文档补件经独立复核成立**。

判词只覆盖 Q1；Q2（具名责任/单据唯一存储）与 Q3（替代单白名单/关联/竞争语义）仍开放。
Q2 落定时应明确缺项责任方枚举；codex-sw 报告已纳入 B01 规划。
其他实施前置仍未关闭，run/install/双机验证仍为 NOT_RUN，B18 未建实施卡、未开工。
这次状态更新不构成产品实施许可，也不改变 A 固定基线的可消费结论与公共分工。

## 11. 用户确认 B 独立消费 A（2026-10-29）

用户要求通知 agent-transfer 线基于 intg-A 做后续开发，并明确“他那边属于
intg-b 的这条线，是隔离的”。据此区分开发依赖来源与 PR 合入目标：

- A 为 B 提供公共 client、MethodSpec、注册/DI/output 的固定依赖，不要求先等 A 合 dev 才消费。
- B 保持自己的 `intg/agent-move` 容器、业务工作树和评审位；功能 PR 目标为该 B 容器。
  B 业务不改投 `intg/cli-command-surface`，不并入 #2015；B 容器最终仍面向 dev，按依赖顺序交接。
- 建议 B 固定消费已推送的 A `e0b75e32460f61f3767a135f7c55dbc61ff7c1d0`，包含 dev `e228d4ae4`。
  安装证据仍绑定 `278a53a8cae67a73ff664d720aad069033e97ea7`；两者产品、verify/build 路径无差异。
  A 可消费不代表 #2015 已通过最终合入验收。
- A 当前等待 Allen 的 worker6 命令归组，收到后由 coordinator 评审整合；B 涉及共享注册、
  清单或受影响命令路径时协调同步，不复制公共实现，也不替换 A 的工作范围。
- B 自身方案补件、实施安排与业务评审由 codex-sw 及其评审位按用户授权处理；A 只协调依赖和接线。
  此记录没有替 B 关闭 Q2/Q3 或改变其方案结论。

本次核读 #2004：`design/agent-transfer-refactor-plan-20261027` → `intg/agent-move`，
精确方案头仍为 `9d5110cafd10d1d506d3181ebe2c63cbe424af1e`。没有改写 B 的分支或 PR。
上述关系已通知 codex-sw，消息 `6783ef13-4fe4-58e9-9297-efbc20b32005`，
conversation `7b3ca4cb-e27a-43f3-8391-0738f6e0fdc5`，accepted=true / queued=false。
已请对方确认实际采用的固定 A 依赖及公共接口阻塞；消息接受不等于 B 已同步或开始实施。

### B 固定依赖确认（2026-10-29T04:35:46Z）

codex-sw 回复 `c8b9e76d-ed48-56fb-a71d-a970b6879d5e`：已在独立本地准备分支
`intg/agent-move-a-baseline-20261029` 固定 A `e0b75e32460f61f3767a135f7c55dbc61ff7c1d0`，
工作树干净，尚未推送；B 功能 PR 仍入 `intg/agent-move`，其远端头未变。
对端实查产品/verify/build 相对 source278 无差异、合并检查 rc0，并核读公共 exports、
CliRuntime/注册/输出和 link 样板，报告 A 公共层未见新增接入阻塞。
以上为对端完成的源码准备和核对，未宣称 B 产品实现、新安装、测试或联调 PASS。
B 的 Q2/Q3 和领域契约由本线收口，共享注册/清单与 worker6 归组仍按精确新头协调。
本线已 ACK 终态回复，没有重派任务或回传礼貌确认。
