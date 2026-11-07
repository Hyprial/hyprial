# agent move 重构与双向握手方案（R4.3：线性开发，Pi → SDK）

状态：**2026-10-30 用户已明确批准开始线性开发。沿用不可变迁移单、八个动作、move 管单据/领域模块管事实、取消后新建、源端一次只选一单、多目标可邀请的模型。先 B01 契约与责任收口，再按依赖推进；具体公共端口的技术缺口仍逐项验证。**

本文替代 #2004 R3.1/R4/R4.2 的活动方案。2026-10-28 的“整理方案”授权与 2026-10-29 的业务简化决定保留为历史依据；2026-10-30 用户进一步明确“可以开始线性开发”。现按既定 Kanban/PAC、WIP PR 和独立评审流程推进，不再逐片重复询问开发许可；部署、真实迁移、自签和自合仍不在该开发授权内。

本轮验收范围的直接裁决为：“pi -> sdk，其他都不是必须真机验证的，后续支持可以持续补齐。”因此必需真机顺序为 Squire/headless Pi → DeepSeek SDK；Claude/Codex/其他形态列为后续支持，未测试状态继续如实记录，不再作为当前开工或真机验收的必过门。该范围决定关闭 G7/M8 的用户选择问题，不把任何 NOT_RUN 改记 PASS。

R3.1 的独立复核仅绑定 `275573d88d86b1e06572d39344dfddbe3fdfe250`：P1–P5 满足送组织评议条件，仍非实施许可；R2 仅绑定 `4c79ff0639d3d7de0656b80deffe50f44bb60e3b`。R4 保留这些修正与 #2006 要求，新增命令/不可变单/跨线分工交精确 SHA 复核，不继承旧 PASS，不自签。与 #2006 的差异和未决接口见 §1、§13。

2026-10-29 设计收简：Q1 的六项确认信息已获 cc-sw-review 对 `9d5110cafd10d1d506d3181ebe2c63cbe424af1e` 独立复核成立。本次按用户新裁决补 Q2 的唯一业务归属、领域事实归属及待定责任的确定人/时间，撤掉 Q3 的跨单替代机制。`--replaces` 是作者在 `3ac5381d0` 草稿中增加、未经用户单独选定的参数；当前不提供 `--replace`/`--replaces` 或 `replacesMoveId` 字段，也不管理后继链。只管理每张单自己的生命周期，以源端唯一选择和既有执行围栏处理竞争。本 head 另交精确 SHA 复核，不能沿用旧判词；其余领域接口和实施前置仍开放。

目标：**0.5.5.2，集成分支 `intg/agent-move`**。先交付真实 Squire/headless Pi 的完整迁移，再交付 DeepSeek SDK，最终覆盖所有支持通过 hyprial 入网的 agent 形态。Pi 使用 DeepSeek 模型不等于 DeepSeek SDK。

## 1. 审核基准：#2006 不能被讨论记录替代

本次逐项复读 [#2006 主设计 §0–§8](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/c9242a9a4321dd748387ba1115d10f2b54890160/docs/design/design-agent-move-2026-10-27.md) 和 [handoff](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/c9242a9a4321dd748387ba1115d10f2b54890160/docs/notes/handoff-agent-move-devusr-2026-10-27.md)。该 PR 后文有明确撤销前文的条款，不能把被撤销的邀请链接、新 key 族、旧 ACL 分支、bundle 总量上限继续实现。

| 审核方要求 | 本案处理 | 对应位置／状态 |
|---|---|---|
| §0/§2：move 命名，transfer 退场；同级不同时解释 agent 名与子命令 | 迁移命令统一为纯 `agent move` 命令组，执行使用 `start` | **命名原则遵循，具体命令树需审核方确认修订**，§4 |
| §0.1/§3：不扩大为目标 host 的 SSH 权限 | 只通过已有 OrgFS/Zenoh，目标不提供迁移专用 shell 权限 | 遵循；§8、§12 S6 |
| §0.3：复用 orgfs_blob/snapshot/sync 等现有族 | 续传/完整性缺口归 OrgFS，move 不造新网络族或分片协议 | 遵循；§8 |
| §4.0：ACL grant + operation 幂等 + 目标 agent 自报，无链接令牌 | request/invite 是有作者的双方同意证据，不是新的 bearer 授权；权限仍由 ACL/身份权威判定 | 原则遵循，**双向发起交互须确认**；§5–§7、§9 |
| §4.4/§4.45/O2：host 接收策略两处求值，设备钥以 #1982 为准 | 字节接纳与 agent 落地分别核验；未签名策略仅是提示；0.5.5.1 的授权场景只记 FAIL / NOT-EVALUABLE{acl_not_enforced} | 遵循判据，公共接口仍阻塞；§5、§12–§13 |
| §4.5/§8.1：0.5.5.2、intg/agent-move、无跨 org、无 bundle 总量上限 | 同 org、同获准 space；通过稳定观测、容量检查和有界内存处理大包 | 遵循；§8、§11 |
| §4.2：真实 owner 确认，列出变更清单 | 沿用 `user:` 身份解析核验 owner，逐项展示 §4.1 的完整变更清单；不使用 `--yes` 证明身份 | Q1 已在 9d5110caf 获独立复核；本轮保留清单，§4–§5 |
| §4.3：自报作者必须是目标 agent，落地未恢复不能说已完成 | 自报保存原作者；启动失败时 daemon 只报告失败事实，不伪造 agent 自报 | 遵循作者约束；对“根本不能自报”异常的补充须确认，§9 |
| §3.3：SSH 的 delete_when | S1–S4 在 PATH 无 ssh 时通过、S5 真双机 PASS、runbook SSH 前置整节删除并留记录；附强制损失证据清单 | 按 §11 补全承接；条件尚未实际达到 |
| handoff：不改 kernel/actor-runtime/P2 执行模型，不造第二真值 | 复用现有 actor/journal；公共端口缺口交所属模块，不在 move 复制数据库权威 | 遵循；§10、§13 O10 |
| §8 O5–O10、handoff S1–S7 | 每项列责任、完成条件和负例；未跑保持 NOT_RUN | §12–§13，不能用文档存在销账 |

**明确请求审核方确认的变更，不能暗中视为已批准：**

1. **F1／命令树**：#2006 §2.1 是 `agent move <name>` 加顶层 `hyprial move`；用户要求所有动作归 `hyprial agent move`。本案改成纯组 + `start`，消除原位置参数冲突，移除独立顶层协议组。协议细节原则上留 daemon；确需用户诊断的仍放本组。
2. **握手**：用源 request、目标 invite 对同一不可变计划确认，允许任一先发起，替代单向 approve；不恢复已取消的邀请链接。双方齐备只表示可申请开始，不自动搬迁。R4 按用户选择固定 moveId→planDigest；改变批准条件另建单，不继承旧同意。
3. **兼容探测实现需修订，版本门保持**：#2006 §2.3 引用的旧 runtime probe 实际是本地 smolvm 实验，并非远端版本探测。本案经认证的网络能力端口核对版本及所需能力，不通过试执行迁移动作或猜测错误来判版本；不可达、未授权与协议不支持分开。最低 host 版本采用单一契约常量 `MIN_MOVE_HOST_VERSION`，提议值为 `0.5.5.2`，最终随组织确认的首发版本钉定；版本号不能代替 ACL/所需能力实际可用。可认证地判定低于下界/缺必要协议时返回 `MOVE_PROTOCOL_UNAVAILABLE`，正文含对端版本及需升级下界。先升级目标 B、再升级源 A；无旧动词 alias、无静默试旧协议或 SSH 回退。端口 owner/schema/信任锚及防枚举仍待 §13 M2 关闭，不能把替换探测实现说成已完成。
4. **失败自报**：目标不能启动时不能由 CLI/daemon 冒名自报；如实显示“落地但未恢复、自报缺失”，保留源 hold。S4 应覆盖这个不能伪造成功的分支。
5. **历史材料**：当前源码、错误码、帮助、活动指南和测试完成改名；仓库 WORM 历史证据保持原字节，不能用全仓 grep 为零逼迫篡改审计材料。§11.1 枚举固定路径、内容哈希和替代检查，§13 G6 是批准门；此范围例外仍待组织接受，旧名称不作为活动 alias。
6. **S5 范围已由用户裁定**：#2006 原集合 `{claude, pi}` 的历史要求保留在原件；本轮按 2026-10-30 直接裁决改为 S5-Pi → S5-SDK。S5-Claude/Codex/其他形态保留 NOT_RUN 的后续支持记录，不是本轮必需真机行。Pi 使用某模型供应商不能代替 SDK harness 实测；未测形态也不能据此宣称支持。

用户已批准按修订方案线性开工；未决公共接口仍是对应实现片的技术依赖。独立评审与实际运行结果分别记录，开发授权不等于全部接口已交付或全部验收已通过。

## 2. 当前基线、已有成果及重构原因

### 2.1 固定核查坐标（2026-10-28）

| 对象 | 核查 SHA | 含义 |
|---|---|---|
| 本轮 fetch 的 dev | `9d0f5a5e7ae7e0e23b24586e23055b5f26f101e1` | 相对 3252 主要是 GUI 重建；daemon、identity、transfer CLI 及 transfer 集成测试范围无差分 |
| intg/agent-move | `3252c112c2ddc29374ce04c3fda1463334d36f65` | 尚未吸收上述 dev 新头；本计划 PR 的目标 |
| #2006 | `c9242a9a4321dd748387ba1115d10f2b54890160` | 比转发中的 da673b0ee 更新；设计和 handoff 两文件，未合并 |
| intg/daemon-request | `f7cd7b5f5a58ccfddf83e77dd15bbde09acc1895` | 当前为设计/交接；新增术语统一 host，整线目标 0.5.6 |
| #2010 设计与 CLI 公共基础 | `485dab4dbaee3c1af8bf8f9adcfc375a46ca7343` | 已读 cli-surface.md / coordination；A/B/C 分工见 §10.4。该快照仍列九动作候选，须随本次用户选择更新，不能与 R4 同时作活动契约 |
| #1982 | `6f6ad4c6afe190eef0a58ab1b1a6c47d34573e12` | 0.5.5.2 设备凭据/ACL 权威候选，Draft，不能当作已发布能力 |
| #1994 累计 WIP | `b50d930d5da37f378ea54cd24225c6baf8120792` | 只作来源及审计证据；实际产品整合提交仍是 `5744f673851091f87fb8d4af459b2100659650c5` |

本计划分支只提交一份文档，不把最新 dev 的整个 GUI 差分带入仍停在 3252 的 move 集成线。会对精确 head 做虚拟 merge-tree 检查；这不等于已经合并 dev。产品开工前须由集成线协调同步最新 dev；后续按组织要求 merge 新 dev、重跑 merge-tree，不直接向共享集成线或 dev 写实现。

### 2.2 已合并与未合并的区别

| 范围 | 已有代码／主要增量 | 仍欠什么 |
|---|---|---|
| dev：身份和生命周期 | #778/#781/#784/#785 的托管、授权、启动、升级/回滚/销毁基础 | 不是完整 move 验收；旧 host-invite 创建访客身份，与新 invite 含义不同 |
| dev：打包和落地 | #1061/#1062/#1065/#1076/#1084/#1095/#1096/#1400/#1410 的 manifest、签名、凭据信封、home/registry 落地 | 旧凭据布局与新 secrets.sqlite 不兼容；组合写入缺完整可恢复端口 |
| dev：P2 安全基础 | #1334/#1339/#1356/#1360/#1364/#1367 的持久意图、源停写、custody、重启恢复、防双活 | 保留行为和负例，收清执行权、恢复与成功的不同边界 |
| dev：用户入口及修复 | #1456/#1503/#1505、#1830、#1459；有 agent move、TUI/工作树/启动相关代码 | 仍含 SSH 迁移；类型允许或替身测试通过不代表真实双机支持 |
| WIP：OrgFS 传输 | 打包、offer、trigger、receipt、目标拉取，删除 SSH 的候选改动 | 整包保密、流式内存、权限与恢复、真双机尚待验证；删除顺序受 §11 约束 |
| WIP：申请/自动处理 | request/decision、TTL、撤回/消费、claim/lease/重试、自动接收 | 旧模型是 request→approve，不是双向握手；幂等和生命周期需重做边界 |
| WIP：凭据与通用接线 | native auth、grant/env、投影刷新、user_reply、OrgFS refresh、harnessArgs | 归属分开，不能在 move 内任意改核心；四处结构超限是 WIP 新引入 |

旧 #1528/#1529/#1531/#1532/#1538/#1573 已关闭未合并，#1738 是历史候选，#1994 不整体合并。历史 10-18 Pi + DeepSeek provider 双机为 PARTIAL；当时 Squire 是对话对端，不是被搬迁的 agent。固定候选的完整 Squire/Pi 双机旅程仍是 **NOT_RUN**。

### 2.3 为什么 dev 和 WIP 要一起整改

架构要求现在是命令经 IPC 进入 daemon request，CLI 只负责解析和显示，身份/凭据/runtime 的私有状态由所属模块处理。旧实现存在 CLI 直接迁移编排、签名和读 trust store，application mixin 厚重，落地链跨接口组合写入以及事后手动刷新投影等问题。

只改 WIP 新文件仍会调用这些已合并旧路径；只清理 dev 也不会自动获得 OrgFS、握手和自动接收。因此统一设计、分片改造现有基础并移植必要 WIP，形成一套实现。**不是同时维护 dev/WIP 两份状态机，也不是把旧分支整包先合进来再修。**

新 daemon request 线已提供统一 kernel 客户端/声明机制的可消费基线。move 从第一条新命令起遵守其 IPC 薄包装契约；缺公共客户端时由该线提供最小接口，不能 import 内部实现临时绕过，也不能自行造第二套 RPC。已与该线对齐：公共命令面及最小基础 A、move B 面向 0.5.5.2，完整 daemon request C 面向 0.5.6；A 的可消费代码基线先交付，B/C 分别消费，不等 C 全量结束。具体接口交付按 §10.4 核验；B 已获线性开发授权。2026-10-30 按 A 线协调消息 d1e25826-88ce-578d-b5d7-57b8b75b502d 固定后续开发依赖 A 为 4ad16740b38b374c1608bae974db1482e204ae57；原 P2 源码核查仍绑定 e0b75e324，原安装验收仍绑定 278a53a8，不自动覆盖新头。已实读新消费说明并核出归组等 46 个源码/验证文件变化。A 尚未合 dev、冻结后的同步/冲突/独立验收由 codex-daemon-request 跟进；B 不修改共享 A 分支。

## 3. 用户到底要完成什么

A 上已经在工作的 agent，保留角色、可恢复的会话、工作目录和经授权的自有凭据，通过 OrgFS 搬到 B。B 自动接收、严格恢复旧会话并运行；agent 用自己的身份报告新地址，同 org 的其他 agent 能找到它并继续对话，用户收到真实回复；A 的旧实例退休，不双活。

源 owner 决定“我的 agent 愿不愿意搬”；B 的有权管理员决定“我是否接收这一个实例，给它什么运行条件”。agent 的 owner 不因换 host 变更。不同 owner 管理两台 host 的托管，需要双方合法授权；不能把它偷换为跨 org，或默认为转让 agent 所有权。

首轮验收为 **Squire/headless Pi**，Pi 原生凭据和 `hyprial secret` 两条来源分别验证。第二优先级为 **DeepSeek SDK**。最终按已注册 harness 的能力适配扩展 headless/TUI 等形态，不以现在白名单里的一行字宣称全部支持。

安全范围遵照用户澄清：防其他普通用户和 agent 读取或篡改秘密、上下文、工作目录；root/管理员不在防护范围内。host 控制面可完成受控落地和必要的运行绑定，不允许操作者脚本随意改 agent 的 token、会话或工作树；不以“修复信任”为由 `git init`。整包保密与运行隔离分别验收。

## 4. 命令、操作人和效果（用户已选择模型，尚未实现）

规则是 **资源组 → 动作 → MOVE_ID**。`agent move` 是纯命令组；`request`、`invite` 是动作，`M1` 是示例单号。位置参数只接收 moveId；agent 用 `--agent`，目标 host 用 `--to`。一个 moveId 绑定一份不可变计划，单号不是文件名、requestId、授权令牌或秘密；知道单号不能越权。

八个动作、十种调用形式如下；表内 A/B 指源机/目标机，§10.4 的工作线另行标明。

| 动作／形式 | 完整语法 | 操作主体和效果 |
|---|---|---|
| check | `hyprial agent move check --agent squire --to B` | 源有权操作者/A：查身份、协议、接收策略、运行/隔离能力、凭据处置和资源；不建单、不停车、不导出秘密 |
| request 创建 | `hyprial agent move request --agent squire --to B` | 源有权操作者/A：原子确认本实例未选其他未结束单，固定计划、建 M、记源意向及唯一选择，通知目标 |
| request 同意 | `hyprial agent move request M` | 源有权操作者/A：原子选择已有邀请 M 并对固定计划记源意向；已有其他未结束选择则冲突 |
| invite 创建 | `hyprial agent move invite --agent <source-agent-uri>` | 有权接收者/B：以当前 host 为目标，核实精确源实例并建 M，记目标意向及权威授权引用；多目标可邀请，不占源选择、不建空 agent |
| invite 同意 | `hyprial agent move invite M` | 有权接收者/B：对已有申请的固定计划记目标接纳及权威授权引用 |
| list | `hyprial agent move list` | 有读权限的参与方：发现已授权可见的迁移单，不扫描全 org 私有记录 |
| status | `hyprial agent move status M` | 有读权限的参与方：看计划摘要、双方同意、授权/进度/证据、新鲜度和下一步 |
| start | `hyprial agent move start M` | 源有权操作者/A：复核当前双方同意与授权，绑定唯一 operation；取得目标接纳后停源、传送，B 自动恢复 |
| cancel | `hyprial agent move cancel M [--reason TEXT]` | 有权参与方：按实际阶段记录本人撤回、对方提议拒绝或执行取消请求；结果见 §7 |
| retry | `hyprial agent move retry M` | 有恢复权限者：查询收据并继续原 operation 的允许步骤，复核授权；不另起执行、不强解锁 |

### 4.1 参数、身份与确认边界

request/invite 无 MOVE_ID 时为创建，必须有上述创建参数；带 MOVE_ID 时只表示对既有计划的本方同意。CLI 与 handler 两层均拒绝在已有单模式同时传入 `--agent`、`--to`、`--ttl` 或其他替换计划的字段。角色由可信 principal、实际 host 和单据绑定判断，不能由 CLI 自报 owner 或调用动词授予。

创建可用 `--ttl DURATION` 在策略允许范围内指定开始窗口；由 daemon 首次受理时固定绝对 expiresAt，重试不重新计时。`--idempotency-key KEY` 用于自动化重试关联，不等于 moveId；首次 CLI 发送前固定键，原结果仍在 daemon。字段白名单、默认 TTL 和共同参数 schema 随 §10.4 统一表冻结，不接受任意计划 JSON patch。

新建和已有单确认都必须按 #2006 §4.2 展示下面的变更清单；源先申请、目标先邀请采用同一要求。CLI 展示来自领域公共端口的事实，不用调用者填入的 owner、host 或路径字符串替代权威校验。

| 必须展示的内容 | 来源与判据 |
|---|---|
| owner 本人身份证明 | 展示规范的 `user:` 身份、身份核验结果及可信证明引用；与 `user:` 投递使用同一套身份解析。仅在源机执行命令、自报 owner 或传 `--yes` 不构成证明；委托范围按 G2 的权威契约核验 |
| 旧 URI、新 URI 与 host 段变化 | 展示完整旧/新 URI、精确 agent 实例和源/目标 host，明确标出 #2006 所称的 machine 段变化；以 identity/host 可信绑定为准，不凭同名匹配 |
| harness 与版本 | 展示源/目标实际 harness 名称和版本，以及所需 runtime 条件；只写 harness 标签或模型供应商不够 |
| 凭据是否随迁、哪些需要重新认证 | 展示授权范围内的随迁、必须重新认证、禁止携带分类及原因；不展示秘密值，不用整库复制代替语义选择 |
| 旧 URI 的去向 | 展示迁移后旧 URI 的可达性、退休状态及是否存在既有别名，并给出路由/身份权威的依据；不为填满此项新增兼容 alias 或承诺旧地址继续可达 |
| 回滚条件与回滚后 URI | 按 §7 的执行阶段列出允许、禁止或需证明才能回滚的条件，以及恢复后实际使用的 URI。执行权可能已到目标时，缺少有效撤销/停止证明不能许诺恢复源；不得把 landing、TTL 或 lease 到期当作回滚许可 |
| 预计停机窗口 | 展示估计时长或范围、估计依据和观测时间，说明从停源到目标严格恢复的影响；无法估计时明确 `UNKNOWN` 及原因，不填零或承诺不停机 |

缺失或无法核实的内容必须显示 `UNKNOWN`、原因和下一责任方，不能用猜测值宣称已满足 #2006 的确认清单。缺必需可信实例/运行条件时返回待核验或缺项结果，不发出可执行的有效同意。记录一旦有固定计划，就不能用后补文件把它换成别的摘要；动态路由、资源与估计值注明新鲜度，start 仍重新核验当前事实。

采用现有输出层和可信 request 上下文。适用 IPC 预览的动作只提交其绑定的 preview operationId，不重传可替换计划；短命预览 ID 不替代持久 moveId/双方同意/执行 operation。无需额外 `--yes` 或等价强制弹窗，明确动作承载意图，后端仍校验授权和固定计划。

R4.2 不提供 `revise`、用户 `--revision`、跨单替代参数或旧 `--id` 同义写法；创建和确认 schema 都不接受跨单关联字段，不能把它保留为隐藏高级选项。撤回/拒绝统一到 cancel，用户恢复入口为 retry。没有独立 `agent invite`、顶层 `hyprial move`、approve 或 `--request`。包、凭据、落地、resume、receipt 是后端步骤，不逐项暴露用户命令。旧入口按 §10.5 处置，实际消费者同批更新，无 alias/shim。

`--json` 服从统一输出；筛选与持续观察暂不额外发明参数。现有 `--all` 含义并不统一，本稿不把它定义为“含终态”；若采用 `--follow`，需与公共层明确流式声明、游标、终止和输出格式，普通查询不自动变流。

### 4.2 源先申请：谁在哪里执行，如何知道单号

1. A 的有权操作者执行 `hyprial agent move check --agent squire --to B`；检查不保留运行位置，也不代替 start 时复核。
2. A 执行 `hyprial agent move request --agent squire --to B`，源权威原子登记 M1/固定 planDigest、源意向及本实例唯一选择后安排通知。显示“待目标同意”，投递未完成另列 delivery 状态；此时已不能另向 C 发起第二张申请，但 agent 继续正常运行至 start。
3. B 的有权接收者通过既有通知得知 M1，也可用 `hyprial agent move list` 找到已授权收到的单，执行 `hyprial agent move status M1` 阅读 §4.1 的完整变更清单。
4. B 执行 `hyprial agent move invite M1`；接纳/ACL 权威收据有效后，双方条件齐备才显示 ready。收到通知的 ACK 不替代此同意。
5. A 执行 `hyprial agent move start M1`；A/B 当前事实重新核验后才开始。B 自动接收、落地、严格恢复；双方用 `hyprial agent move status M1` 查目标作者自报、网络续答和源退休证据。

### 4.3 目标先邀请

1. B 的有权接收者执行 `hyprial agent move invite --agent <A上Squire的完整URI>`，核实精确实例与必需条件后建 M2，记目标意向并通知源方。
2. A 的有权操作者通过通知或 `hyprial agent move list` 发现 M2，执行 `hyprial agent move status M2` 阅读 §4.1 的完整变更清单，再以 `hyprial agent move request M2` 原子选择并同意；本实例已有其他未结束选择则拒绝，不覆盖旧选择。
3. 双方有效同意绑定同一计划且当前授权满足后，A 执行 `hyprial agent move start M2`；后续同源先申请流程。
4. A 若需要不同 runtime/权限/凭据范围，必须按 §7.1 处置旧单、独立建新单，双方重新确认；不能把新增条件塞回 M2 或沿用旧 invite。

多个目标 B/C 可以分别建邀请 M2/M3，均为 `pending-source`，不会锁住源 agent。源只选择一张；选中 M2 后，M3 保留待源确认/本次未选中，不能变成 ready 或启动，也不自动伪造拒绝或替用户选择。源可按权限拒绝其余邀请，邀请方可撤回，或按各单期限到期。成功迁移后，旧源实例/host 绑定的其他邀请不可在新位置继续使用。

若 A 已向 B 申请 M1，而 B 独立创建了邀请 M2，双方应继续在 M1 上以 `invite M1` 补齐，或先安全关闭 M1 再由源以 `request M2` 选择邀请；不按名字/相似内容自动合并，不允许同时选择两单。这些规则复用既定 request/invite/start 流程，不增加用户动作。

### 4.4 发现通道与状态权威

创建方在既有获准 journal/事件持久边界记录计划与本方意向，通过既有 messaging/outbox 和获准 OrgFS 元数据同步通知对端有权操作者。通知包含单号、必要摘要/受限引用和下一步，缺省不广播全 org；不能为发申请提前取得向目标 bundle space 写入的权限。目标通知接收人须来自可信 host/owner 配置，不能让来信自填任意 recipient 获得管理权限。

通知失败由原 outbox/收据重送；list/status 通过 request 读取获准记录和可恢复投影，不新增 inbox，不直接扫私表。远端离线或单据尚未同步显示待送达/信息陈旧；本地没找到不能自动新建同号，未授权访问不泄露存在性。发现通知与最终目标 agent 自报分开认证。

固定计划由建单节点首次持久提交并绑定摘要；move 是单据业务与存储适配的唯一归口，具体记录与责任见 §10.4.1。源意向及唯一选择由源有权主体经源 journal 原子记录/撤回，目标同意与接纳由目标权威记录/撤回，ACL/identity/runtime 仍各守原权威。源执行资格和执行权由既有源 journal/fencing 串行决定；目标落地、运行和收据由其所属公共端口负责。OrgFS 副本与 ready 是这些事实的投影，不能反向授予权限。

内部 `stateVersion`/游标仅表达所属权威节点/epoch 的状态进度，不是可修改计划的版本或全网总序。status 展示观察时间、各权威证据/游标及新鲜度；同一 moveId 出现不同摘要一律冲突，不按最后写入者覆盖。start/cancel/retry 必须在权威端口复核和认领，离线旧 ready 不能放行。跨权威撤销/执行竞争仍按 §13 M7 补契约，不能假称分布式事务。

## 5. 双向握手：共同计划、真实身份和 host 控制

### 5.1 一张迁移单包含什么

OrgFS 可呈现 request/invite 记录供交换与审计，但文件存在不是权限。授权真值仍是 ACL grant/epoch、identity 的主体与委托、B 的接收策略；迁移事实进入既有持久事件/journal。不新增令牌表、第二 inbox 或独立授权库。

| 类别 | 必须绑定的内容 |
|---|---|
| 单据 | schemaVersion、moveId、规范化 planDigest、固定有效期；计划不可原地改写，状态/证据追加到既有事件，无跨单替代字段 |
| 身份 | org、获准 space、稳定 owner 身份、完整源/目标 URI、可信的具体 agent 实例代际、源/目标 host 的设备绑定及版本 |
| 权限 | 源迁出者与目标接纳者的真实身份/授权证明；ACL grant/epoch、host 策略版本；同一个 agent 的范围约束 |
| 运行条件 | harness/runtime 与版本、严格恢复要求、目标隔离身份和权限、工作目录映射、资源/并发条件 |
| 材料范围 | 会话/工作树的许可范围、凭据选择引用、随迁/重签/重认证/禁止搬运分类；不存秘密正文 |
| 双方意向 | request 与 invite 各自作者、序号、签名/证明、有效期及撤销记录，分别确认同一个计划摘要 |
| 源端选择 | 可信 org/agent 实例代际所绑定的唯一未结束 moveId、planDigest、选择序号及关闭证据；由源权威写，属于追加状态而非可编辑计划或新授权 |
| 执行事实 | 后续唯一 operation、冻结快照的 packId/manifest 摘要、执行权 epoch、落地/恢复/网络/源退休收据 |

invite 不需要引用尚不存在的 request hash；双方同意均绑定 moveId、同一 planDigest、角色和可信实例/作者。改变目标、具体实例、权限/凭据许可范围、有效期或已约定运行条件必须另建单，旧意向不继承。schemaVersion 只是记录格式，不是用户选择的计划版本；stateVersion 只反映状态/观察进度。

正常对话/编辑造成的内容变化不使计划每秒失效；计划绑定许可范围和恢复条件，开始停写后另行冻结 packId/manifest 内容摘要。重新打包生成新 packId 和密钥，不能拿旧内容摘要覆盖新包，也不能借此扩展批准范围。原生凭据合法轮换/实例连续性按 identity 契约判断，不把本案“不变计划”当作复制旧失效秘密的要求。

### 5.2 防假身份及邀请被套用

1. 字符串 URI、owner 字段、自带公钥都不是可信身份；数学验签成功仍须核对该钥与实际主体、权限的权威绑定。
2. identity 提供可验证、非 bearer 的 agent 实例描述及委托验证。当前内部 `entity_token` 不得明文发布到共享 OrgFS；#1982 设备 TLS 证明也不能自动当作 agent 实例证明。缺端口就阻塞，不读私表补一个。
3. B 邀请后源 destroy/recreate 同名，实例代际已变，旧邀请失效；源 host 重新绑定、owner 变化、跨 org 同样拒绝。合法密钥轮换按 identity 连续性规则验证，无证明时暂停并重新确认。
4. invite-first 只批准已核验的精确实例和明确条件。仅知道名称或未验证配置时显示 pending-verification；源补充新条件必须由 B 再确认，不能自动吃进旧邀请。
5. 首版不做通配邀请、不支持拿到单号即可迁移。知晓 moveId、复制 invite 文件或拥有 space 写权限，均不足以处置别人的 agent。

### 5.3 B 保留哪些决定权

B 可限制允许的 owner/具体 agent、harness、隔离方式、宿主能力、网络和 secret 权限、目录映射、并发与可用容量。源不能借迁移请求 sudo、任意宿主路径或其他主体秘密。身份真实不代表代码无害，仍须最小授权与运行隔离。

授权有两个执行点：**入站 OrgFS 数据接纳**、**identity 落地/运行接纳**。从同一权威 host 策略求值；通过其一不替代另一处。运行前 runtime 还须核验隔离和执行权。host 策略由 #1982 定义的设备钥签名，签名/授权能力未交付前只能显示运维提示。

先取得 B 对本 operation 的最新接纳和保留，再停写 A；B 的邀请不强制 A 迁出，A 的 start 也不能迫使 B 落地。检查、意向受理、start、实际落地/启动逐阶段核验；离线时不能凭缓存 ready 放行。权限拒绝不泄露 actor 是否存在，按 #2006 S2/S7 的同形错误契约验证。

ACL grant 的创建/撤销与 invite 记录可能跨持久边界：使用相同操作幂等键和授权收据恢复，不假称分布式 ACID；未取得权威授权收据的 invite 不进入有效状态。握手不复制 ACL 判定器。

## 6. 幂等：重复命令、重复消息与竞争都要考虑

| 层次 | 规则 | 不能发生什么 |
|---|---|---|
| 首次创建 | 提交前固定客户端幂等键；按主体+键保存输入摘要及返回 M，响应丢失后用原键重试。自动化可传 `--idempotency-key` | 每次重试重新取时间/nonce 变成新单；同键换内容必须冲突 |
| 双方意向 | moveId/planDigest/party/action+输入摘要返回同一收据；作者/实例由可信上下文核验 | 重复 invite 多授一次权限、重试自动续期、同号换计划或把旧同意继承到新单 |
| 源端选择 | request 创建或 request M 确认邀请，在既有源 journal 同一持久边界核验空闲/本单，并原子写源意向与选择；冲突不写第二份有效源意向 | 同一实例同时申请两个目标，或同时接受两个邀请；先查询空闲再分开写入造成双选 |
| start | 源权威核验本单仍是唯一有效选择，原子绑定唯一 operation/执行资格；目标原子保留名称/实例位置；两边分别有持久收据 | M1→B 与 M2→C 同时取得同一源的执行资格；覆盖 B 已有实例 |
| 操作步骤 | `(operationId, phase, resourceKey)`、输入摘要和 epoch 固定；重放查证已有效果、补收据或继续原步骤 | 重建身份、重复导入凭据/grant、第二 worker、重复退休 |

没有原幂等键的另一次手工新建不能一概猜成重试：若同主体/实例/目标已有同内容的未结束单，返回原单信息及冲突/继续指引，不静默新建或延长许可。客户端仅持有重试关联信息，权威结果在 daemon，CLI 退出不丢业务状态。

**同一源实例只允许一张源端已申请或已接受的未结束迁移单**，约束在源确认时生效，不拖到 start 才选目标。唯一性键使用 identity 解析的 org/具体实例代际，不能使用易变的文件内容摘要、session 快照或裸 agent 名；正常运行写文件不会获得第二个名额，同名重建也不能继承旧单。实例的正常运行不受仅意向阶段的选择阻断，已有运行权模型与 M6 的生命周期争用契约仍须遵守。

多个目标的未接受邀请可以并存，不占源选择或源执行权。源端两个不同 request/接受动作并发时，由上述原子边界只接受一个；同键同输入重试取原结果，另一单返回冲突及权限允许的当前单/取消与查询指引，不能最后写入覆盖。源权威离线或状态不可核实时不能确认选择，远端文件/通知不能代替该写入。

选择在安全终止前持续占用；只按当前实例、moveId 和选择序号匹配释放，迟到旧单撤销不得清掉新单。未启动时先原子关闭旧单启动资格，再按 §7 收敛属于本单的接纳/授权等清理；已有 operation 或可能已发执行权时须完成相应补偿/停止证明，`cancel-requested`、failed、TTL/lease 到期均不能直接释放。进入 complete 后保留终态与源退休证据，再次迁移在当前权威位置重新核验。具体事务方法及与 P2 围栏的接合由 B01 与 runtime/identity 所属方确认，不造第二生命周期锁。

start 与取消仍须在副作用前竞争；限制建单/选择不能代替防重复运行的执行围栏，不能沿用 WIP “落地后才 consumed”作为唯一防重。attempt 是调度次数，不改变业务幂等键。

网络不保证 exactly-once；靠持久幂等、步骤证据和围栏处理重复投递。保留撤销/消费/终态墓碑，过期文件不能复活旧授权；已完成单重试只返回原结果。再次迁移另建单，恢复未结束操作继续原 operation。

## 7. 生命周期：同意、执行、调度分开

### 7.1 意向生命周期

`pending-source` / `pending-target` → 源唯一选择、双方同意、身份、固定计划和授权核验 → `ready`；既有计划的当前证明尚不可核实显示 `pending-verification`；同号出现不同摘要视为 `plan-conflict`，不能覆盖或自动选最新。拒绝、撤回、过期、授权撤销阻止该单再启动；ready 是权威事实的投影，不是第三份授权真值。

request/invite 均有限有效期，不能晚于计划固定的 expiresAt，并受当前 ACL/host 策略裁剪。创建参数 `--ttl` 只用于首次受理，daemon 先查幂等记录再计算一次绝对期限；重试不续期。提议默认 24 小时、策略可缩短或限制范围，该默认值尚待公共 schema/产品确认；不提供永不过期邀请。有效期是开始窗口，不是已发执行权的自动释放计时器。

改变条件或延期的用户流程：先 `cancel M1`，通过 `status M1` 确认旧单已安全关闭、源选择已释放且没有未结束执行权，再以新建 request/invite 提交完整条件；也可在旧选择关闭后接受另一张仍有效的目标邀请。不关联旧新单、不继承同意/秘密/ACL grant、不隐式取消旧单。新建得到新 moveId/planDigest，双方重新确认；旧单记录照常保留供独立审计，已完成后再次迁移也是新旅程。

旧单仍在取消/执行中时，第二次源申请或接受邀请返回冲突及原单状态查询指引；目标单方邀请仍可按 §4.3 留在 pending-source，不获得源意向或执行权。取消未选中邀请只关闭该邀请，不释放其他单的选择。更换具体实例/owner/org 按新主体另行核验，跨 org 仍拒绝；新建与取消竞争由 §6 的源权威序列化，start 仍须取得真实执行资格。

不可信时钟或无法核实授权时暂停。有效期由双方权威端口按可验证时钟边界保守判断，不相信 CLI 时间。旧 schema 1–3 的永不过期记录不得自动变成新授权。

### 7.2 执行事实与回退边界

| 阶段 | 完成事实 | 失败／撤回处理 |
|---|---|---|
| `admitting` | A 将本单唯一选择绑定到执行 operation/围栏，B 核验并持久保留本 operation | 失败仅清理本 operation 的保留；源选择须按安全终止规则释放，不停车硬搬 |
| `quiesced` / `transferring` | A 停写并持有 custody；冻结并传送密封快照；B 先记接纳意图再收片 | 同 operation 续传；尚未签发执行权时，可原子关闭签发资格后安全补偿 |
| `prepared` | B 的 home/凭据/身份保留/必需绑定准备齐全，仍不可运行 | 准备成功不等于 active/worker 存活；无执行权时可按步骤收据补偿 |
| `execution-granted` | A 持久签发绑定 B、实例、operation、epoch 的唯一运行权，并维持源 hold | 可能送达即不能凭超时复活源；撤回转取消处理，等待目标可验证撤销/停止证据 |
| `published` | B 通过 identity 权威命令发布完整身份/运行配置 | 不是会话已恢复；缺绑定、凭据或投影收据不得 warn 后继续 |
| `resumed` | runtime 证明原会话严格恢复、实际 worker 启动和预期身份/epoch 注册，持久收据 | **拟定交接 commit point**；源据此幂等退休，丢收据查询/重发，不冷启伪造恢复 |
| `network-ready` / `source-retired` | 目标真实自报和同 org 实际调用成立；源收到可信恢复收据后退休 | 两项可独立收敛；源退休不冒充网络就绪，网络失败在目标修复 |
| `complete` | 恢复、目标自报/真实会话及用户投递、源退休证据全部满足 | 不可撤回成“从没搬过”；回迁须新单和新授权 |

源不能只凭目标 `active` 退休；offer/land/publish 都不是 strict resume。执行权签发后的不可单方回退边界，与恢复收据的 commit point 是两个不同事实。

### 7.3 撤销竞争、长时间 hold 和恢复命令

B 的接纳与撤销在 B 的权威端口串行判定；A 的 start/cancel/签发 grant 在 A 的权威端口原子互斥。不存在一个跨两机的简单 SQL 事务。迟到 prepare 不能让已关闭的源 operation 再签发 grant。

意向 TTL、执行超时、调度 lease、ACL 主动撤销不是同一件事。到期/撤销阻止后续未经授权的受保护副作用；已经发生的效果仍须记录、对账或补偿。lease 过期不释放执行权，后台 GC 不删除进行中恢复材料。

`cancel M` 返回 `cancel-requested` 后，后台或有权操作者的 `retry M` 查询原收据，按原 operation 已记录的目标（取消或继续）推进；不能把取消中的 retry 解释为重新向前搬迁。每次动作写入主体、operation/epoch、证据、结果和下一责任方：

- 源账本可证明尚未签发 grant：先持久关闭签发资格，再通知 B 取消/补偿，依源围栏许可恢复 A。
- grant 可能已送达：源保持 hold；B 持久关闭该 epoch 的启动资格，停止并由 runtime 证明无对应 worker，双方确认后才能提升 epoch、恢复源。
- 目标已 strict-resumed：重取原收据，完成源退休；后续问题在 B 修复或另作回迁。
- 目标永久失联且拿不到停止证据：显示 `blocked-target-proof`，交 runtime/身份负责人依独立灾难恢复流程处置。本命令不单方强制解锁，不以删目录/改库证明没有 worker。

这提供了一条可执行诊断/恢复入口，**不宣称 O8 的死目标自动恢复已解决**。死目标权威处置与权限、证明接口须在实现批准前落定；没有证据就明确等待，不能假报告已安全取消。

### 7.4 cancel/retry 的可观察结果与允许状态

| 当前事实 | cancel | retry |
|---|---|---|
| 等待本方/对方同意、尚无 operation | 本方已有意向记 withdrawn；对方提议记 rejected；审计保留实际作者；选中单按 §6 安全关闭后匹配释放，未选中邀请不改其他单选择 | 没有执行可恢复；给合法确认/start 或消息投递状态指引，不创建 operation |
| pending-verification / plan-conflict | 有权主体可撤回/拒绝；不能自动选摘要，已持有的选择/执行权仍按实际证据关闭 | 可重查本单权威证明；不自动选择、不改计划，不为解决冲突另建执行 |
| ready、尚无 operation | 与 start 在源资格边界竞争；胜出则关闭启动资格，不能只删一份同步文件 | 不代替 start；不会自动开始迁移 |
| admitting/quiesced/transferring/prepared，证明未发执行权 | 持久关闭签发资格后补偿本 operation；未补偿完仍是 cancel-requested | 仅在后端标记可恢复时重取收据、补原步骤；复核必要授权 |
| execution-granted/published，运行事实未收敛 | 保持源 hold，等待目标撤销/停止证明；受理不等于已取消 | 查原 epoch/收据并继续允许步骤，不能另发第二执行权或冷启第二 worker |
| 已 strict-resumed，网络/源退休未收敛 | 返回已越过交接边界及下一步；不暗中逆向迁移 | 查询原恢复收据，推进原 operation 的源退休/网络验证，不重复启动 |
| cancel-requested / blocked-target-proof | 返回原取消/阻塞状态 | 仅核查新证据或继续原补偿，无法证明时继续 blocked；不清围栏 |
| complete / withdrawn / rejected / expired / cancelled / 不可恢复终态 | 返回原终态或已越过边界，不删除审计 | 返回原结果或明确不可恢复；不复活、不创建新单/新 operation |

可恢复性由领域状态表和步骤证据决定，不能只看错误字符串中的 failed。超时、CLI Ctrl-C、观察断开都不等于业务 cancel；客户端不拼接 cancel+新建+start。精确错误码/状态字段与 A 公共声明共同冻结，表中权限和围栏约束不得放宽。

## 8. OrgFS、凭据和普通主体间隔离

### 8.1 搬运与大包

只复用现有 OrgFS blob/snapshot/sync/log 等族，底层由 Zenoh 传输。move 负责经批准的快照、manifest、operation 与授权绑定；OrgFS 负责已有内容块、完整性、已收集合、续传及传输收据。必要扩展在 OrgFS 公共能力中评审，不把应用续传写成第二套 Zenoh 分片/重组协议，不加入 tailcat 备用旁路或 SSH fallback。

现有 `orgfs_blob(digest)` key 不带 space/host；payload 自填 spaceId 不是授权证明。因此 ACL/OrgFS 接收端须从可信 principal 和获准操作上下文绑定 space、目标和 actor 范围，不能单凭 topic 或文件字段放行。**先持久 write-intent/幂等占用，再收字节**；全部清单校验、可用标记与 journal 发布遵循同一可恢复完成边界，未完整时禁止落地启动。文件和数据库不能假称一条跨系统 ACID 事务。

不设 bundle 产品总量上限，但不意味着无限 RAM/磁盘：移除 WIP 的整包 BytesIO、分块列表及整体 join 路径，采用有界缓冲、背压、流式落盘和断点续传。bridge 单次载荷限制只约束一次调用，不应变成整个 bundle 上限。磁盘不足、配额/并发耗尽、无法估算剩余时间都明确显示；不编造速度或剩余时长。

整个 manifest、上下文、工作树和凭据载荷均做目标绑定的认证加密，复用现有加密库。每次不可变 pack 使用独立内容密钥与唯一分块 nonce，AAD 绑定双方身份、实例、moveId/planDigest/schemaVersion、operation/packId 和块序号。续传重发原密文；重新打包换 packId/密钥；recipient key 必须来自可信绑定，不能从共享文件自报取得。

共享 space 的读者只见必要元数据和密文；加密不取代 ACL、不防拒绝服务。明文 staging、临时文件、崩溃残留、备份和日志都受同样访问限制；完成/补偿后按操作证据清理，不能因暂时收不到回执提前删恢复材料。

### 8.2 O5：三类凭据及安装规则

| 分类 | 判据与操作 | 负例／阻塞 |
|---|---|---|
| 可授权随 agent 搬运 | 该 agent 合法使用的 provider/native 凭据，owner 明确选择，导出/安装由 identity 权威端口完成 | 多带未选择项、其他 agent/owner 的凭据即失败；Pi native 与 hyprial secret 分别实测 |
| 需目标重新签发或重新认证 | host/设备绑定或不可导出的身份材料，按所属 identity/provider 的连续性规则处理 | 禁止复制源设备私钥冒充 B；缺重签/reauth 时显示阻塞，不能冷启冒充续接 |
| 禁止携带 | 宿主凭据、其他主体秘密、原 secrets.sqlite/HMAC/WAL 及整库内容 | 合成植入禁带文件/条目，必须在导出和落地两侧拒绝；测试不使用生产秘密 |

“旧 owner 凭据不得随迁”在本案解释为不得搬运 owner 的广域登录/宿主/其他 agent 凭据；agent 所属 owner 不变，获批准的本 agent 模型凭据仍可随迁。这一分类需 identity/ACL 负责人确认，不能靠复制整个 home 偷渡。

凭据由语义导出、目标密封和语义安装保留使用能力，不复制原数据库/HMAC。CLI 和 move 编排不读明文 secret。无法恢复的 native 设备凭据在 check 就列 reauth，负责人及动作清楚可见；秘密不出现在命令参数、清单、错误日志和自报中。

### 8.3 首轮隔离配置与依赖

提议首轮 Linux A/B 使用每 agent 独立 OS UID/GID；控制面与 worker 分离，禁止共享补充组、调试 capability 或可读控制面目录挂载。runtime 的受限启动端口绑定 actor/实例/epoch 与隔离句柄，清理无关 FD/环境/权限，agent IPC 仅开放该实例有权使用的动作。

**这是 runtime/运维前置能力，现有 Pi 路径没有完整实现。** move 不自行 sudo、创建系统用户、开放任意 UID/命令 helper 或更改 P2 执行模型。由 runtime 所属线提供端口，运维按批准的部署配置预置；不接受此依赖时须重新评审替代方案，不能退回同 UID chmod。

B 控制面在准备期间拥有隔离 staging；home 属主交接由 identity/runtime 的受控接口完成，记录可重放的权限/所有权收据后再启动 worker。具体目录边界、恢复期间谁持有访问权、安装后控制面如何保持必要管理能力，须 runtime/identity owner 共同确认，不能以一句 chown 收尾。

共享 UID、隔离能力未知或主机配置变化使证明失效时，check/接纳/启动拒绝。真实负例验证其他普通用户和其他 agent 不能读写 home/session/worktree、读 worker env、ptrace/发控制信号或调用控制面管理 IPC。整包加密通过不能替代这些运行负例。

## 9. 自报、恢复收据与一个查询入口

### 9.1 O6：谁报告、记录在哪里

agent 自报必须由 **B 上实际迁移后的 agent 身份** 通过既有 messaging 发送。CLI 不能代写 author；daemon 的 operation 状态也不能冒充 agent 说“我已搬好”。消息权威仍是原 messaging durable inbox/outbox 与其作者认证；move 事件只保存 messageId/作者/operation/摘要引用，查询经消息公共读取端口取得原文，不建第二 inbox。

自报包含旧/新 URI、host 绑定、harness/runtime 版本、strict resume 结果引用、landedAt、未完成标记、需 reauth 项及 `hyprial agent move status M`。作者必须匹配当前目标实例与 epoch，同 org 且在该单读权限范围内传播，不公开凭据或会话内容。

目标只有落地、无法恢复或无实际 worker：daemon 可以报告 `landed-unverified`、`resume-unverified` 和 `agentReport: missing`，**不得强行启动空 agent 凑自报**。若真实目标 agent 能发送失败自报，也只反映其已验证事实。S4 明确分这两种情况，交审核方确认补充。

### 9.2 O7：唯一恢复收据的提议归属

提议由 **runtime 已有 lifecycle/journal 的持久边界** 保存唯一 `StrictResumeReceipt`，identity 验证目标身份绑定；move journal 仅引用其 receiptId/内容摘要，网络发布通过既有 outbox 重试。准确的现有记录位置、方法签名和授权签名主体由 runtime/identity owner 落定，当前不是已交付端口。

拟定写入顺序：identity prepared → A 持久执行权 grant/保持 hold → B 验 grant、完成必需 binding 并 publish → runtime 幂等启动、验证原 session/注册身份/epoch → runtime 持久写 resumed → 目标权威经既有 outbox 发布收据 → A 验证后退休。崩溃窗逐步可查证，不能先发收据后验证恢复。

runtime 的恢复收据和 agent 的网络自报不是同一证据。前者证明运行事实，后者证明实际目标作者与消息链路；二者都绑定 operation/目标实例/epoch。一个缺失不能由另一个代填，投递 ACK 更不能代替它们。

### 9.3 O9：`status M` 必须回答全部判断

输出权限过滤后的观察快照：moveId/固定 planDigest/格式版本、双方同意所绑定摘要与有效期、本单是否被源选中及权限允许的冲突/待选择状态、ACL/host 核验结果、唯一 operation/epoch、当前阶段、已传/总字节及文件、速率/最近进展/重试原因、准备/恢复/自报/网络/源退休各项证据、是否 complete、阻塞原因和带参数的下一步命令。另给观察时间、各权威节点/epoch 的状态游标、新鲜度和必要的待同步标记；不是跨节点原子快照，不将缓存 ready 作为执行授权。不能只显示百分比。

人类显示和 JSON 都明确“不完整”：例如 `completion: incomplete` 与“已落地，原会话未恢复；源保持暂停”，加 `agentReport: missing`；不把 active/landed 写为整体成功。不以是否出现“成功”两个字的子串充当机器断言，状态枚举与用户文案分别测试。

遵守现有 CliResult/CliStream 与哨兵行契约；`--json` 一次结果为一个带 `ok` 的对象。`ok:true` 表示查询执行成功，**不表示迁移完成**，必须另有 completion 字段；不可读或操作失败按约定 `ok:false`/非零退出。长操作从 IPC 返回 operationId，CLI 断开后继续持久处理，随时用本命令复查。

## 10. 代码结构与公共端口

### 10.1 业务聚合，核心只提供所属能力

```text
CLI agent move（现有 shell 命令目录中的薄包装；未来整体搬迁由 daemon-request 线负责）
  → kernel 公共 IPC client → daemon 方法声明/鉴权/路由
  → daemon/impl/move/
      service.py       业务用例；手工和后台路径共用
      api/             move 参数/结果/错误及 IPC 适配
      intents/         request/invite、不可变计划、源端唯一选择、期限、撤销规则
      lifecycle/       源停写、准备、交接、恢复、退休；复用 P2 journal/fencing
      agents/          已注册 harness 的迁移能力与会话/凭据适配
      archive/         快照、manifest、密封与校验
      orgfs/           现有 OrgFS 端口适配、operation 与传输收据关联
      landing/         暂存/校验，调用 identity/runtime，不能写其私表
      scheduling/      有界调度、lease/attempt/backoff，不授予权限或决定成功
      persistence/     move 唯一存储适配归口；复用既有操作/事件边界，不另起权威表体系
  → identity / runtime / ACL / OrgFS / messaging 公共端口
```

模块树是职责设计，逐片迁移，每目录最多 7 个直接文件、每代码文件最多 1000 行。已有 transfer 目录并非完全不存在：问题是边界未闭合；不能只改目录名后继续在 router、actor 核心或 CLI 追加业务函数。

harness 类型来源复用现有注册/能力矩阵，move/agents 集中表达 session 可携带性、entrypoint/runtime、凭据来源、隔离要求及真实验收状态。check、意向、start、receiver 消费同一结果，不再各有 TUI/native-auth/PROVEN_CHAINS 白名单。

### 10.2 高层 IPC 与内部权威接口（名称为提议）

最小高层能力按下表与 A 对齐，具体方法拼写由共同注册表冻结，旧 `transfer.*` 占位不形成兼容要求。创建与同意必须是不同方法或明确互斥的 schema；不能以一组无约束 optional 字段让 handler 猜动作。下表方法名为提议，不是已注册能力。

| 用户动作 | 最小 IPC 候选 | 业务归属及关键约束 |
|---|---|---|
| check | `move.check` | B/move 调用可信身份/host/ACL/runtime 端口；只检查，不保留执行权 |
| request/invite 创建 | `move.intent.create` | 明确 source-request/target-invite 输入，完整参数+幂等键；B 核验角色、固定计划和 expiresAt；source-request 原子写唯一选择，target-invite 不占源；无跨单替代字段 |
| request/invite 同意 | `move.intent.confirm` | moveId+本方动作；服务器查固定 planDigest 并绑定真实作者，源接受邀请须原子登记唯一选择；schema 不接收替换计划/TTL/跨单关联 |
| list/status | `move.list` / `move.status` | 权限过滤、固定摘要、独立业务状态/新鲜度、下一步；观察不是消费/ACK |
| start | `move.start` | moveId；在权威边界复核并取得唯一 operation，不允许外部重传计划 |
| cancel | `move.cancel` | moveId+原因；本方意向与实际执行阶段决定结果，不越权清其他主体事实 |
| retry | `move.retry` | moveId；按原 operation/目标和状态表恢复，当前授权与证据缺失时阻塞 |

方法声明包括输入/输出 schema、可信调用上下文、鉴权、幂等、错误、逻辑流式和确认元数据，由 A 公共机制导出；B 交付真实 handler。写操作断线不等于失败未执行，公共 client 不盲重试。普通 `--json` 单对象带 ok，ok:false 退出 1；进度/交互走 stderr；请求受理、迁移完成和恢复失败分别可判定。CLI/GUI 只经 request 查询，不直接读 journal；没有任意 SQL 或计划编辑接口。

| 所属端口 | 输入／返回 | 事务、幂等与失败规则 |
|---|---|---|
| identity：Describe/VerifyMoveSubject | 可信 agent 实例、owner 委托、host 绑定；返回非 bearer 描述与验证结果 | 不输出 entity_token/私钥；绑定版本与连续性；同名重建不继承 |
| ACL/host：Evaluate/ReserveMoveAdmission | 实际 principal、实例、计划、策略/grant epoch、operation | grant/撤销由 ACL 权威写；目标接纳/撤销序列化；同键同输入同收据，不自建求值器 |
| identity：ExportMoveMaterial | 停写代际、批准的凭据选择、可信 recipient、operation | 在 identity 内读取秘密，输出密封句柄/语义清单；不输出 DB/HMAC，不反向授权更多材料 |
| identity：EnsureMoveLanding | 已验证 staged payload、实例/计划/输入摘要、目标接纳 | 按 operation 保留身份并串行调用 actor；阶段收据可重放；返回 prepared，不启动 |
| identity：PublishMoveLanding | prepared、执行权 grant、必需 binding 收据 | identity 自己事务提交可见身份/配置版本与事件；投影不完整返回 pending，禁止 warn 后放行 |
| identity：Reconcile/AbortMoveLanding | operation、期望实例/epoch、输入摘要、取消证明 | 只补偿本 operation 的 staging/grant/身份保留；已有执行权时需双边停止证明，不覆盖已有 agent |
| runtime：EnsureStrictResume | 目标实例、执行权 epoch、sessionRef/摘要、隔离句柄 | 单 worker 所有权；真实旧会话核验后持久收据；不得冷启作为回退 |
| runtime：RevokeAndProveStopped | operation/epoch 与经授权取消请求 | 关闭旧启动资格、验证无 worker，出可验证收据；失联不能伪造结果；接口待 owner 确认 |
| messaging/adapter：EnsureMoveBindings | 经批准绑定、expected holder/version、operation | 版本化预留/应用收据；先齐备再 publish/start，不能 activate 后裸 pin |

统一写键为 operation+phase+resource，校验实例/计划/输入摘要；不同输入拒绝。内部持久 SQL 仅在所属存储适配器，move 不读 identity/runtime/凭据私表；daemon 内部调用公共端口，无须绕回本机 socket。

identity 落地按 `reserved → materializing → credentials-installing → bindings-preparing → prepared → publishing → published` 记录步骤。文件临时落盘、校验和持久化先于收据；凭据数据库语义安装与本模块幂等收据在同一事务，文件与 DB 各有可恢复证据。重启分类 absent/partial/complete 后补齐或补偿，不能把 pending 永远改成 unresolved 并让人改库。补偿也持久记录，禁止清理别的 operation 或既有实例。

这些端口复用现有 AgentActor、home/materialise、runtime/session、P2 journal 等能力；并不宣称现有函数组合已经提供完整事务。核心扩展由对应 owner 独立审，move 不调整 actor mailbox、执行所有权或 P2 模型。

### 10.3 具体哪些文件要处理

#1994 的固定产品差分涉及 **53 个 Python 源路径**：daemon/transfer 30、transfer CLI 7、application/actors/transfer* 3、其他核心/共享 13；另有 1 skill、50 测试、51 文档/证据、1 契约、1 脚本，共 157 路径。transfer 模块快照有 62 文件，33 沿用、12 修改、17 新增；另有 SSH 删除。不是本计划 PR 的改动量。

| 对象／问题 | 处置 |
|---|---|
| `shell/.../transfer/move.py` 直接 run_transfer；requests/trust 在 CLI 签名、读 store | 改为 move 薄包装；完整用例、作者校验和存储移至 daemon |
| `application/actors/transfer.py`、`transfer_p2.py`、`transfer_auto.py` 厚 mixin | 业务回 move；application 只组合公共端口、注册调度入口 |
| `transfer/orgfs/receiver.py` 混入授权、落地、目录及 Git 修改 | 分传输与业务落地；保留用户文件，不自动 git init |
| `transfer/landing/registry.py` 多步 registry 组合；identity actor 手动 refresh_projection | 用 identity 权威命令/事件驱动投影；不追加刷新补丁 |
| ipc/router、ipc/params、CLI runtime/entry、messaging/maintenance | 只保留方法注册/必要 tick，参数和判断移至 move |
| daemon/interface、kernel ipc_errors | 收窄公共服务/契约登记，移除向 CLI 导出的 store/sign/pin 等内部 helper |
| messaging replies/__init__、user_reply、send | 通用消息修复独立 PR，迁移通过依赖使用，不夹带进 move |
| messaging/orgfs_bridge、agent/runtime/start | refresh / harnessArgs 分别按 OrgFS、runtime 归属独立审 |
| squire/setup | 更新活动操作说明中的旧 SSH 前提 |

四处超限均是 WIP 新增：daemon/interface **968→1027**、messaging/maintenance **1000→1003**、ipc/router **1000→1008**、identity/actor/_core **999→1014**。**先在正常基线提供职责拆分/公共接口，再移植 WIP**；不先提交超限补丁再申请豁免，不把删空行作为架构修复。

### 10.4 与 daemon-request 的开发分工和节奏

依据 #2010@485dab4db 的 A 提案和双方已结清的协调消息（d9f4f654、8f991359、76ad20e2、d29f4fa7），下列是设计分工；本次用户选择将其旧九动作候选更新为本稿八动作，不变更各工作线的审批条件。

| 工作线（与源机 A/目标机 B 无关） | 负责人／目标 | 内容与依赖 |
|---|---|---|
| A：CLI 命令面及最小公共基础 | codex-daemon-request／0.5.5.2 | 接回 #1967 公共 client；方法声明/错误/逻辑流式/预览元数据、OpenAPI 骨架、import 棘轮及真实样板；统一 CLI 注册、依赖注入、output |
| B：agent move | codex-sw／0.5.5.2，PR 指向 intg/agent-move | 本文八动作薄包装、move handler/单据与状态机、领域端口调用、消费者迁移和真实旅程；消费 A 的公共能力，其他核心能力由所属方交付 |
| C：完整 daemon request | codex-daemon-request／0.5.6 | 其余命令逐组迁移、全量声明及边界收口、最终目录搬迁；消费 A，并同步 B 已交付的 move，不再重写 |

1. 先把本稿的语法/互斥字段/主体/状态/幂等/输出与最小 IPC 合入双方统一设计表；A 的 #2010 活动方案应同步删除旧九动作候选，保留固定 SHA 历史引用即可。
2. A 设计留档并取得用户对 A 的明确实施同意后，按其约定进行任务/Kanban/OrgFS 登记和 PAC；不额外推断 Allen 审批是 A 的前置，也不把流程说明当本轮开工许可。B/C 仍遵照各自批准范围。
3. A 先提供经验证、可消费的公共客户端/方法登记/输出基线与真实样板；文档中的接口名不等于已交付代码。B 已获线性开发授权；可以先做不依赖 A 接线的契约工作，依赖性实现固定消费 A 的可用基线，不要求 A 先合 dev。
4. B 的命令注册与真实 handler 同批生效，C 完整迁移独立推进；不为赶 0.5.5.2 先在 CLI 直调内部实现，也不等待 C 的全量 0.5.6 才迁 move。
5. identity/ACL/host 策略、隔离/凭据、resume/停止证明等依赖单独关闭；A 完成不能代替它们。Pi 北极星通过后再 SDK；其他形态保留后续支持记录，不作为本轮必需真机门。

共享 `shell/impl/cli/runtime/entry.py`、公共 services/DI/output、注册和导出框架由 A 统一接线；B 提供 move 命令模块、MethodSpec/handler 和消费者清单，不能双方重复编辑后恢复 shim。move 业务修改归 B；identity/runtime 私有实现归各自所属方。公共结构拆分、领域实现、生成 surface.json/post_install 清单分步审查；生成物由脚本重建。CLI 物理目录全迁移留 C，不把搬目录与本轮命令语义混成一项工作。

### 10.4.1 Q2：单据存储、领域事实及责任确认

用户选择 **move 管迁移单据，各领域模块保留自身事实权威**。B/codex-sw 是 move 计划、意向、源选择、状态推进和存储适配的唯一业务负责人；`persistence/` 必须在既有 daemon 操作/事件 journal 上给出统一读写接口、重放与失败语义，不能把“适配”当成无人负责。计划在建单节点只写一次；源选择/意向由源节点写，目标接纳由目标写，分别在各自既有持久边界恢复。统一业务归口不意味着把跨机事实搬进一个全局数据库或声称分布式事务。

| 对象 | 权威记录与负责边界 | 具名责任／待定安排 | 交付与验收 |
|---|---|---|---|
| 计划、意向、源唯一选择、流程与重试 | move 定义记录/schema，`move/persistence` 复用既有 journal；源确认与选择同一持久边界 | **codex-sw** 负责方案、存储接口及业务实现，**cc-sw-review** 独立业务评审 | B01 冻结前给准确接口/写入节点/事务边界；B05 实现前确认幂等、恢复、条件释放；不另建授权库或第二生命周期锁 |
| 身份、凭据导出/安装、落地步骤 | identity 持久保存本域步骤/补偿收据，move 仅持收据引用/摘要 | 技术承接人**待定**；由 **codex-sw 与现有 identity 维护位确认并具名登记** | B01 契约冻结前、最迟 B04/B10 对应实现批准前确认；未确认不写其私表或称端口已交付 |
| ACL/host 策略、grant/撤销、接纳 | ACL/host 权威求值与写授权，move 不复制求值器 | 技术承接人**待定**；由 **codex-sw 与现有 identity/ACL 维护位确认并具名登记** | B01 契约冻结前、最迟 B04/B05 实现批准前确认；交身份/host 绑定、授权收据及孤儿 grant 清理契约 |
| 执行围栏、严格恢复、停止证明 | runtime/P2 既有权威保存执行 epoch 与恢复/停止事实；move 引用，不能伪写 resumed | 技术承接人**待定**；由 **codex-sw 与现有 runtime/P2 维护位确认并具名登记** | B01 契约冻结前、最迟 B11/B12 实现批准前确认；准确 journal 位置/方法、重启顺序、撤销证明及独立领域评审 |
| 目标 agent 自报与投递结果 | messaging 原 durable inbox/outbox 和作者认证；move 仅记 messageId 等引用 | codex-sw 负责消费者；所需端口缺口的承接人由 codex-sw 在 B01 中与现有 messaging 维护位确认 | 自报片实施前落实 author/读取权限/保留与重发，不建第二 inbox |

上述“待定”是明确依赖，不表示对方已接单。codex-sw 负责找出既有维护/任务归口并取得具名确认；若期限前仍未落实，在 B01 记录缺项、归口对象和阻塞，不能靠跨线 A 的交付或自己兼写领域真值关闭。实施采用混合交付：所属方交付公共端口；必要的小补丁可由 codex-sw 在另获该实现范围授权后起草，交对应领域独立评审。业务评审不替代领域评审，A 只负责公共 client/声明/接线/output。

确认清单的缺项使用固定领域归口，随后绑定本次实例的具体操作者/已确认技术承接人：owner 证明→源有权操作者＋identity；URI/machine/旧址去向→identity＋相应 host 控制面；harness 版本/运行与隔离→相应 host 控制面＋runtime；凭据选择/reauth→凭据所属操作者＋identity；回滚条件/恢复 URI→move 编排＋runtime/identity；停机估算→move（codex-sw）汇集传输/runtime 观测。`UNKNOWN` 必带原因与下一责任方；技术承接未具名时，明确回到 **codex-sw 协调确认**，不能显示空白或把模块标签冒充已接单人员。投递缺项另归 messaging，数据块/传输回执缺项另归 OrgFS；move 负责跟踪、展示和恢复编排。

### 10.5 旧入口到新归属（活动消费者必须同步迁移）

#2010 固定盘点的 transfer 族共 19 个入口：1 个用户 move、18 个隐藏协议/诊断入口。以下逐项给出目标归属；它是开发处置表，不宣称已经删入口或验收。实际脚本、argv、hooks、skills、测试 import 与帮助/生成清单的逐文件清册由 B 提供给 A，共享接线由 A 收齐；未找到调用者不能当作无人使用。

| 现有入口 | 目标归属／用户替代 | 交付核对 |
|---|---|---|
| `agent move <name>` | §4 纯 agent move 组，check/双方意向/start | 旧直接 run_transfer/SshRunner 编排移走；无同位置名字/动作冲突 |
| `agent transfer bundle-export` | move archive，start 后端阶段 | 导出/原 argv 调用者改公共端口；测试保持材料范围负例 |
| `agent transfer bundle-validate` | move archive/landing 校验 | status 出失败原因，业务校验不留 CLI |
| `agent transfer bundle-receive` | OrgFS 接纳/目标调度阶段 | 既有 write-intent/幂等及授权门；B 自动接收 |
| `agent transfer bundle-land` | move landing→identity 公共落地端口 | stage 收据和失败恢复测试，不由 CLI 组合私表写入 |
| `agent transfer bundle-complete` | 源生命周期退休阶段 | 可信 strict resume 收据前不可销毁源；不能替换成用户手工 destroy |
| `agent transfer credential-seal` | identity 语义导出/密封 | CLI 不读明文 home/secret；拒绝整库导出 |
| `agent transfer credential-open` | identity 语义安装 | 不返回明文给 CLI；凭据及幂等收据按所属事务落地 |
| `agent transfer trust-source-add` | identity/ACL 正式授权管理 | 不在 move 建第二 trust store；明确合法授权操作的替代入口后退役 |
| `agent transfer trust-source-list` | identity/ACL 公共查询，move status 仅显示本单所需证明 | 查询权限和脱敏；不通过 CLI 直读 store |
| `agent transfer trust-source-retire` | identity/ACL 撤销及 epoch | 旧信任记录如何退出由所属方确认，不能以删文件代替撤权 |
| `agent transfer precheck` | agent move check／同一领域检查能力 | 真实消费者和错误词汇迁移，start 仍重验 |
| `agent transfer receive` | 目标自动接单/落地编排 | 用户不手工调用 receive；派发与手工 start 共用 service |
| `agent transfer recipient-key` | identity 可信 recipient 公共端口 | 验身份/host 绑定，不接受任意自报公钥 |
| `agent transfer cred-stage` | landing/identity 受限 staging | 明文不进入 CLI 日志，清理与恢复有收据 |
| `agent transfer container-home` | runtime 受限 home/隔离句柄 | 不暴露任意宿主目录；smolvm 专用调用按 §11 条件退役 |
| `agent transfer session-path` | runtime session 引用/恢复端口 | 不由 shell 猜路径；真实 harness 消费者逐项迁移 |
| `agent transfer runtime downgrade-state` | runtime 独立离线维护能力 | 保留停机/锁/存储兼容约束；所属方定维护入口后迁移消费者，不能伪装在线 request 或随目录误删 |
| `agent transfer runtime probe` | 网络兼容检查归 check 的认证端口；本地 smolvm probe 另行处置 | 不将本地实验 probe 当对端版本证据，遵守 §1 升级/拒绝规则 |

另外核对 `agent host-invite`：它是访客建号，既不等于 move invite，也不能自动删掉真实业务；由 A 与 identity 所属方核查后独立归属/替代或退役。内部方法无需按旧协议命令机械一一变成新 CLI；原 18 项的具体替代方法、离线入口与逐文件消费者证据仍按 §13 M3 收口。

## 11. 实施切片、退役和旧看板

本表 B/move 的切片按 2026-10-30 授权线性推进，PR 目标为 intg/agent-move；A/C 及核心公共能力进入所属工作线，不把其实现一律塞进 move 集成分支。#1994 作为取材和证据来源，每片登记“原文件/提交 → 新归属 → 后继 PR → 验证”，不整包合并、不双线复制维护。按本次新授权先登记/认领 B01，后续工作逐依赖推进；未完成的领域端口不得冒称可用。

| 切片 | 内容与依赖 | 可放行的结果 |
|---|---|---|
| P0 设计与契约对齐 | #2006 修订项、F1 命名、§13 接口 owner、最新 dev 与集成基线协调 | 所有开工门有明确回答；用户批准具体实现计划 |
| P1 IPC／模块与结构 | 消费 A 交付的公共 client/声明/输出；B 交付八动作薄包装、move handler/模块；A 统一共享接线；保留原安全负例 | 无新增核心内部 import，结构门通过；新命令从首版走 daemon request；更新契约/skill/生成消费者 |
| P2 OrgFS 搬运 | 复用 OrgFS，write-intent、manifest/续传/幂等、流式内存、整包保密 | 无新 key 族/传输模型；断线恢复与 S6 正反控通过；此时不对外宣称可迁移 |
| P3 双向握手与准入 | 不可变计划/摘要、双方意向、源唯一选择、多目标邀请、独立新建、发现/状态投影、真实身份、ACL/host 接纳、TTL/cancel/retry、持久幂等；依赖 #1982 与实例描述端口 | 假身份/同号换计划/旧同意继承/双选/跨单绕 hold/撤销竞争均拒绝；未经授权不能落地 |
| P4 Squire/headless Pi 端到端 | 凭据语义端口、实际隔离、可恢复落地、strict resume、目标自报、源退休、真实 user 回路 | 两种 Pi 凭据分别真实 A→B，S1–S7 及同步负例均有证据；Allen 独立验收 |
| P5 负例与架构门 | **贯穿 P1–P4，并非最后补**；每片先红后绿、故障注入、import/结构/契约验证 | 尤其防双活、隔离、凭据泄露和权限绕过，未通过不放行北极星 |
| P6 DeepSeek SDK | 复用共同流程，仅增加类型/会话/凭据适配及 SDK 真机验收 | 独立 SDK 运行证据，不拿 Pi 的 provider 记录替代 |
| P7 扩展与退役收尾 | 其他已支持形态按能力接入；SSH 条件删除、旧 host-invite 安置、smolvm 条件退役 | 每形态有真实矩阵行；无未经批准的 VM/内存快照迁移投入 |

P2 先建立传输和幂等基础，P3 再接完整握手，遵循 #2006 D1→D2→D3→D4 的依赖目的；隔离、凭据和失败负例从相关片就开始。生产可用前所有授权门必须齐全，不能以切片顺序为由先发布无 ACL 的迁移。

**SSH 删除条件完整承接 #2006 §3.3**，三项同时成立后才删：① S1–S4 均在 Zenoh-only 下通过，**PATH 中没有 ssh 二进制也能运行**；② S5 真双机在 Zenoh 下 PASS、经确认的验收范围对应矩阵行更新且附原始读数，不能用未获批准的 harness 缩减绕过；③ runbook 的 SSH 前置（`--remote-hyprial`、SSH 免密、SSH 控制面）**整节删除**并留下“已退役”记录。

退役 PR 的证据还必须逐项写“退役会丢什么”，不能只有改名或删除行数：

- `src/hyprial/daemon/impl/transfer/execution/ssh.py` 及其专用测试，包括 `tests/integration/transfer/runtime/test_transfer_ssh.py`；若先改名则列旧→新路径映射，再记录删除。
- runbook “运维窗口”中依赖远端 shell 的预检步骤；列删除的实际条目及替代的 Zenoh/ACL 诊断。
- “SSH 是否可用”类诊断器的旧意义、入口与消费者；说明删除或转为网络路由/ACL 诊断的结果。

保留期仅隔离对照使用，新的生产路径绝不 fallback；不删除其他业务的 SSH 用途。以上是尚待实测达到的门，不能按 WIP 已删除便记作退役完成。

旧 host-invite 是访客身份登记，不能只改名变成迁移 invite。先查调用者、持久字段、恢复和文档，再给独立能力的归属/替代或有序退役方案；不把原语义藏进新 invite。

**smolvm（用户称 solvm）**：现有为实验载体，不是已经完成的 Docker 式整机/内存快照迁移；当前代码也明确拒绝部分跨 host 路径。本期不投入这类功能，普通 agent 状态迁移和安全围栏继续保留。请 cli-developer/运行时负责人确认是否有明确后续计划、承接者和范围；**如没有后续实现安排或决定放弃，继续推进专用功能删除，不再扩展投入**。先核查存量实例/执行状态和调用者，提供停止、旧数据可读、导出/清理兼容出口，再删除 carrier/probe/专用选项及接线；不假设线上无人使用，不静默 fallback，不连带删除共享 identity/runtime/fencing。已合并离线降级/回滚工具归还 runtime 维护及其存储适配器，不能跟目录一起误删。

看板不整批归档为完成。cc-kanban 的 10-21 快照有 13 张未完成/搁置、18 张 done 但清单未勾；同步曾持续 502，不能当成当前实时板。经用户批准且服务恢复后由 cc-kanban 重新核对：真完成补证据；被替代者注明后继；放弃项注明未验收；跨线任务保留责任人。归档会计入 done，不能用它美化进度。新卡按 P0–P7/接口依赖建立并关联旧 UUID，不重复派仍在办 workflow。

dev 前进时，产品分支 merge 最新 dev、处理冲突、重跑精确 SHA 的 merge-tree 和适用检查；surface.json/post_install 等生成物用仓内脚本重建，不手改或沿用旧生成结果。全仓 CLI 目录迁移归 daemon-request 线，GUI 最新在根 `gui/`，不继续按旧交接的 src/gui/app 路径指导实施。

### 11.1 历史材料例外的精确范围与替代改名判据（待 G6 批准）

固定 `3252c112c2ddc29374ce04c3fda1463334d36f65` 下命中旧命令/错误前缀的 WORM 材料如下。路径均相对仓根；**仅允许下表路径且内容哈希完全相同**，不是整个 docs/notes 或 docs/evidence 的无限豁免。其中历史 CHANGELOG 的 12 处命中位于表内 `docs/notes/changelog/CHANGELOG-v0.5.5.md`，不是另加一项可任意变更的根 CHANGELOG 豁免。

| 路径 | SHA-256（完整文件） |
|---|---|
| `docs/notes/at07-i4-run.txt` | `e9778bac221d81ce86f87e6073093522ae82f75ca7756550055418a8942528ae` |
| `docs/notes/c3-claude-envkey-evidence.md` | `a3726e0ec5eeb81fea0ea54610c75f69a264a5119066d787bfa046baf2187823` |
| `docs/notes/c3-claude-run.txt` | `e97d51dac4ae11bc83bb39c373c28e865df6d3a96ae71ec83bdd206be5784709` |
| `docs/notes/c3-codex-first-proof-evidence.md` | `64302fcdd9f184c2443259f763c6baaf438a731ceb8b9fd8d1c4f6c839676dd3` |
| `docs/notes/c3-codex-run.txt` | `7ddda2f284ff7b868d0e6d934ad0b33e4ba7807c343ac098c5f5f8b5865fba77` |
| `docs/notes/c3-followups-evidence.md` | `2f860c7fb203ebb26b64b9cf0d201d838172bb16061340a4c5ec49c9ca33a418` |
| `docs/notes/changelog/CHANGELOG-v0.5.5.md` | `024f7a8bc9770cd150c2eaaac122dadef3334d10eddd571d4c78d902d058b6d3` |
| `docs/notes/design-h2b-transfer.md` | `1ec193295f3d2a6a4dec2488c96e0e3d20772836a857bac9286f9c31065bdd45` |
| `docs/notes/design-t6-smolvm-transfer.md` | `0dc0df9904205868199d8dd7856ea303ae1458e4cb01dc83a1fcbe37848f75a4` |
| `docs/notes/design-tui-transfer.md` | `043586e1d4e84108a499034fe70c36c2904d7bd4578900827d953b9613c58e02` |
| `docs/notes/evidence/agent-home-p2-c3-codex-20261015/support-matrix.json` | `f1236141c7097816b6b2435dde3143f3b962177479da817dc3d9fa8400ca4708` |
| `docs/notes/evidence/agent-home-p2-cross-host-20261010/support-matrix.json` | `a74073d10642da4dc072e3060fdc1e0e5a4961de66225d8005c4bcba21eccdb1` |
| `docs/notes/evidence/agent-home-p2-cross-host-20261010/verification-report.md` | `53e72d01b719cf897f71b7e8ecee52af32c2d02d45230a5245d0758148bbc438` |
| `docs/notes/f10-native-settlement-20261016/full24-at-049eef736-failures.log` | `4a082a12bb682f9b6bf6a95ba541f330c0f9eed4050c75070d5bdfef0c3772df` |
| `docs/notes/f10-native-settlement-20261016/full24-at-5fa913555-failures.log` | `242aaf9992a57ecbb7dfda04d53fa345183e711650aeb8a793ac48cbbebbaea8` |
| `docs/notes/t3-firsthop-autodl-20261016.md` | `2c706f37fd0afd255337ec654c91d2b7b053166442139facb1f24cf24f888f0c` |
| `docs/notes/t5-implementation-evidence.md` | `6e3b670d1d3b95c4d7ed895e72d974e11c663d67da67094bcde62c752ac55f48` |
| `docs/notes/transfer-cards3-evidence.md` | `4f99da2ef04e2a03b1c0f9fdf18bbdab743354225c14ec24e53f6424ab0e296c` |
| `docs/notes/transfer-preflight-and-probe-evidence.md` | `fe364fb5e797dc3b470121d6d9d0436a549e4c1bb7c8a425d0facdaa692132f6` |

拟替代 handoff 的全仓零命中判据（以下不是本轮已经通过的实现门）：

1. 对精确候选树枚举所有跟踪文件命中，不受本地 ignore 影响：`git grep -I -n -E 'TRANSFER''_|agent[[:space:]]+transfer' "$candidate" -- src tests docs scripts contract sidecar .agents CHANGELOG.md`；退出 1 表示无命中，其他工具错误不能记 PASS。
2. 仅减去上表**路径且整个 blob SHA-256 均匹配**的历史命中，剩余命中必须为 0；同时断言 19 文件全部存在且哈希不变，删除/改写同样失败。
3. 活动源码/测试/契约/脚本/skill/帮助，以及其他活动 docs 一律没有路径豁免；另按载体清单核查旧模块路径、注册/标识符/生成 argv 消费者已迁移，无旧 CLI alias。grep 不是这些语义检查的替代。
4. 新增历史材料或 dev 前进产生额外命中时，逐路径、固定提交、哈希和原因送审更新表；不能自动放宽目录或改表吞掉产品残留。活动支持矩阵/runbook 必须更新，不能挪到 WORM 逃避改名。

G6 未获批准前，此替代判据不能冒充 #2006 的既定零命中门；必须先协调修改审核契约，再实现对应检查。

## 12. 验收：#2006 S1–S7 加用户业务负例

所有运行场景目前是方案要求，**不是已实现 PASS**。S1–S4/S6/S7 用隔离的双 daemon 验证，S5 用真实两台 host、真实 harness/provider；两类证据分开。每行保存确切 HEAD/base、命令、rc、断言、host/OS/架构/harness 版本、时间和原始证据摘要；不能拿协议替身称真实搬迁。

**O2 的版本限制**：在 0.5.5.1 上，S2/S3/S7 等依赖强制 ACL 的授权读数只能是 `FAIL` 或 `NOT-EVALUABLE{acl_not_enforced}`，不能记 PASS；不能用该版本上的连通/传输成功推导授权成立。其余场景的结果也不能替代这些安全门。

| #2006 场景 | 本案验收出口 |
|---|---|
| S1 happy path | 双方真实授权、同计划 ready、源 start；B 自动严格恢复，目标 agent 作者自报旧/新 URI，无未完成标记，A 退休，另一 agent 与用户实际收到续接回复 |
| S2 未授权写入 | 未授权 principal 写 orgfs_blob 被拒；与 actor 不存在的拒绝形状逐字节同形；含存在性不泄露反例 |
| S3 grant epoch 与 operation 重放 | 撤销后旧 epoch 拒绝；重复原 operation 不多账、不多 grant、不多 worker |
| S4 落地但未恢复 | 源 hold，completion 不完整；能发自报者原作者如实报告，不能启动者明确自报缺失；CLI/daemon 不能冒名；文案和状态分别断言 |
| S5 real two machine | 本轮必需 S5-Pi（真实 Squire/headless Pi，两种凭据）→ S5-SDK（真实 DeepSeek SDK）。Claude/Codex/其他形态为后续支持，当前均 NOT_RUN，不是必过真机行；记录上下文续答、工作目录与新地址对话，不用本机双 daemon 或 Pi provider 顶替 |
| S6 Zenoh-only | 摘 SSH 或进程审计证明无 SSH；注入仅 SSH 实现时此测试必须红 |
| S7 host 策略拒绝 | space 可写但 B 不接此 owner/agent，落地拒绝；数据入口与落地两处均求值，不把空间权限当运行权 |

额外业务负例同步进入 P1–P4：

| 场景 | 必须结果 |
|---|---|
| request-first / invite-first | 两种顺序同样有效；单方意向不停车、不启动、不创建空 agent |
| 假 owner、自签公钥、复制 URI/单号/邀请 | 无可信身份/委托，拒绝，不泄露目标存在性 |
| invite 后源同名重建或 host 身份替换 | 实例/绑定不一致，旧单不能启动 |
| invite-first 后补另一 harness、更多权限或凭据 | 原单不可编辑；合法处置后另建单并由双方重新同意，不静默放宽 |
| 同 moveId 不同摘要、已有单确认夹带 agent/target/TTL | 两层拒绝；不以最后写入覆盖、不选择所谓最新版 |
| 取消后独立新建、旧单仍 hold 或取消中 | 只有安全关闭并释放源选择后才接受新申请/另一邀请；不继承旧同意/授权，不自动撤销旧单 |
| 同源同时 request 两个目标，或同时 request 两个邀请单 | 源确认阶段原子只接受一个；输家无第二份有效源意向，授权内给冲突/原单指引，不等 start 才仲裁 |
| 多目标 invite、源已有选择、未选邀请被取消或迟到撤销 | 邀请可并存、不占源、不停源；取消未选单不影响选中单，旧序号不能清掉后来选择 |
| request 提交前后崩溃/响应丢失，正常文件写入后再 request | 原键重放原选择与单据；不能留下无绑定源意向，不能因内容摘要变化另占一单 |
| 创建/确认传入已撤掉的替代参数或关联字段 | CLI/handler schema 拒绝；不保留隐藏高级模式或后继链 |
| 未送达/重复通知、陈旧 ready、GUI/CLI 断线 | 既有 outbox/投影恢复发现；通知 ACK 非同意，当前授权不可查则拒绝执行 |
| A/B 同时建不同单、跨 org/space、另一目标盗用 | 不自动匹配；跨范围拒绝；内容单据无法换目标使用 |
| 创建/确认响应丢失；同幂等键不同输入 | 原键返回原单/收据；换输入明确冲突，不自动续期 |
| M1/M2 对同源同时 start、B 已有同名实例 | 仅一单得源资格；目标保留原子冲突，不覆盖、不双活 |
| TTL/撤回/拒绝/ACL 撤销与 start/grant 并发 | 按权威持久排序；缓存文件不复活许可；取消处理中不报告已安全回滚 |
| prepare、凭据安装、binding、publish、resume、receipt 每步崩溃 | 原 operation 恢复、只补自身效果；无 active+失败收据却被当成完成，无重复 worker |
| B 已恢复但 A 丢收据；B 长期失联 | 源 hold，查询/重发或 blocked-target-proof；超时和 lease 不解锁 |
| 大包、磁盘满、断网、坏块、错误 recipient、路径穿越 | 内存有界；可诊断/可续传；校验失败不创建可运行目标，不能 fallback |
| 合成禁带 secret、原 secrets.sqlite/HMAC/WAL | 导出/落地拒绝；目标只有经选择的语义凭据，日志无秘密 |
| 普通用户或其他 agent 访问 home/env/IPC、信号/ptrace | 实际运行隔离拒绝；共享 UID 配置拒绝启动，不拿 chmod 过门 |
| CLI 退出/重连、重复 query/self-report、旧 epoch 自报 | 操作持续可查、原作者可验证、旧实例不能伪造当前完成 |

每片按仓库要求做结构/模块边界、焦点、契约/CLI 输出、适用全套和 ruff；基线失败按组织 CI 差集规则在 head/base 各复跑，不含糊写“基线问题”。本次仅文档修订，做文档一致性、单文件差分与合并兼容核查，不宣称产品测试或真机验收。

## 13. 线性开发中的技术依赖与审核结论

下表的责任是**所需模块归属**；具名业务责任、领域承接待定项的确定人/期限及缺项归口已在 §10.4.1 明确，其他核心接口不冒称负责人已接单。用户现已批准线性开发；公共接口未确认不能把核心私表访问作为暂时方案。

| 门／#2006 要点 | 本案设计选择 | 关闭条件 |
|---|---|---|
| G1 命令/交互修订 | 用户已选择 §4 纯组/八动作/不可变单/双向意向，以及取消后新建、源端唯一选择、多目标邀请；TTL 默认与最低版本常量仍为建议，历史门/范围另见 G6/G7 | 将选择同步 A；独立评审核对 R4.2 与 #2006 约束、Q2 责任和源选择竞争，不继承旧 PASS；技术接口按对应片冻结；线性开发已获用户批准，不保留九动作或跨单替代活动契约 |
| G2 身份/ACL/host 接纳 | 非 bearer 实例描述、owner 委托、精确 actor 范围；#1982 设备签名/两处策略 | identity/ACL owner 给端口/schema、签名与轮换/时钟规则、actor 范围方案和实施责任；0.5.5.1 不能顶替 |
| G3 daemon request 衔接 | §10.4 已对齐 A/B/C 设计分工；A 公共客户端、方法声明、鉴权、OpenAPI/契约与 import 棘轮 | 冻结 §10.2 最小 schema/错误/幂等/可信上下文，A 交付实际可用基线；分工确认不是接口已实现，B 不等待 C 全量 0.5.6 |
| O5 凭据搬运 | §8 三类规则、语义导出/安装，禁止其他主体秘密 | identity owner 确认选择和 reauth 契约；三类判据及负控实测；不凭文档销账 |
| O6 自报存储/读取 | 原 messaging 持久消息，move 仅引用；status 同时显示 completion 与 agentReport | messaging owner 确认 author 绑定/读取权限/保留规则；审核接受不可运行者不伪造自报；正负例通过 |
| O7 resumed 收据 | §9 runtime 既有 journal 唯一权威，move 引用，先验证后持久后发布 | runtime/identity 给出准确持久位置、签名主体、顺序/重启契约；不能继续只有抽象端口名 |
| O8 半完成/死目标 | §7 cancel/retry 状态与结果，持久执行权后无单方 unhold | runtime owner 确定可验证停止/撤销接口、死目标处置权限及审计路径；正反例表明不会双活 |
| O9 单命令观测 | status 一次出双方意向、执行、恢复/自报/网络/源退休及下一动作 | 稳定结果 schema、哨兵行/权限测试、缺证据不显示完成 |
| O10 rustify 边界 | 不变更执行模型；提议钉住：单一状态权威、命令串行化、持久副作用、唯一执行权四项 | runtime owner 给出“既定四条性质”的权威契约并核对本提议；逐文件 import lint 命中为 0，不能假称这四项已获其确认 |
| G4 隔离与 home 属主交接 | §8 Linux 独立 UID/GID 与受控启动；独立加密验收 | runtime/identity/运维接受实际方案，落定权限交接/恢复端口及部署配置；不能仅凭同 UID chmod |
| G5 smolvm 去向 | 无明确后续或确认放弃后有序退役专用实现 | cli-developer/runtime 明确是否继续及承接者；删除片前存量核查、兼容退出和用户批准 |
| G6 WORM 与改名验收例外 | §11.1 的 19 路径/固定哈希及“扣除原字节历史后零命中”替代判据 | cli-developer/Allen 确认该例外与新门，保留仓库 WORM 纪律；之后才能改实现检查，不得自行宣称全仓 grep 为零 |
| G7 S5 harness 范围与阶段门 | 2026-10-30 用户已决定必需真机 Pi → SDK，其他形态后续补齐 | 范围选择已关闭；实际 Pi/SDK 验收仍 NOT_RUN。SSH 删除仍须 S1–S4 无 SSH、按本轮 S5 必需行通过及 runbook/损失证据，不因授权立即删除 |

这里的 actor-rustify 指另一条将 actor 运行时迁向 Rust、收敛执行模型的核心工作线；本案只消费其公开边界，不定义或变更该线的执行语义。O10 的“四条性质”仍需其权威契约确认，不能用此释义或本案建议代替。

### 13.1 R3 复审缺口的当前处置（M8 范围已决定，其余逐片收口）

线性开发现已获批。这些技术缺口须在相关副作用接入前补成可执行契约并由所属方核对；B01 先交契约与负例，后续逐片实现。可以按 §10.4 协调设计与依赖，不能将未关闭项当作已可消费代码。

| 项 | 待补问题／影响 | 所属与关闭条件 |
|---|---|---|
| M1 planDigest | R4 已固定每 moveId 唯一 planDigest、计划与状态分离，status 必含摘要；规范化算法、字段覆盖、空值/顺序/schemaVersion 处理及正式 schema 仍未定 | move 契约与 identity 审核：给 canonicalization、摘要算法/owner、跨语言正反语料；digest 覆盖不可变批准条件，排除进度/投递/自报等追加状态；双方确认和执行均绑定同值 |
| M2 能力探测 | 认证网络端口的 owner/schema/信任锚未定，未授权探测可能泄露实例 | daemon-request/network/identity：给明确方法与参数结果/鉴权、可信 host 绑定、版本/能力常量，区分不可达/未授权/不支持且拒绝不枚举 agent；按 §1 第3项做升级/错误契约 |
| M3 旧协议动词安置 | §10.5 已给 18 个协议/诊断入口逐项目标归属；精确替代方法、逐文件消费者和离线维护入口尚未收齐，不记已关闭 | A/CLI、B/move、runtime：核对原动词、真实调用者、在线 IPC/诊断/退役、生成 argv/hooks/测试；特别确认离线 runtime downgrade-state 的锁与运维入口，不伪装在线业务 |
| M4 执行超时 | 未定义时长、计时权威、各阶段超时转移和恢复动作 | move/runtime：给 phase deadline/stall 规则与可配置范围、权威时钟、停滞告警/下一步；超时始终不能自动解除执行权 |
| M5 孤儿 ACL grant | grant 已创建而 invite 持久化失败，何时重试/撤销/到期清理未定 | ACL/move：同键查原授权收据，定义孤儿判定、恢复窗口、回收/撤销责任与审计，确保不误撤其他授权、不遗留可复用宽权 |
| M6 非 move 争用 | destroy/upgrade/rollback/人工 start 与 move 同实例竞争未定 | identity/runtime：经既有 actor 所有权/epoch 规定互斥、允许组合、拒绝结果及负例；不新造第二生命周期锁或修改执行模型 |
| M7 跨权威竞争 | B 撤销与 A grant 的因果/顺序、输家结果和停滞处置上限未完整 | ACL/identity/runtime/move：明确可证明的排序与迟到消息处理，无法判定时保持 hold；给超时升级/人工处置时限和双方可观察结果，不能假称全局事务或到时自动复活 |
| M8 harness 集合 | 已按 2026-10-30 用户裁决收口，见 G7 | 必需真机 Pi → SDK；其他形态后续支持，不增加本轮真机门；未测保留 NOT_RUN，不以 Pi 的模型供应商替代 SDK |

O5–O10 是验收输入，方案描述仅关闭“没有设计说明”，不会自动关闭接口交付与运行验收。G7/M8 的范围选择已由用户决定；其余接口和安全规则在相关副作用接入前取得可执行结论，不据实施授权宣称技术交付完成。真双机目前未排，保持 NOT_RUN，不承诺未经确认的排期。

## 14. 证据、复审和交付流程

本计划 PR 保持单文档；历史审计证据在另一条分支，必须通过下面的**固定提交链接**访问，不能假设它们在本 PR 树内：

- [逐文件差分与处置 @d85c6252d](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/d85c6252d558e2f4ea81b1ea2ef537f04c09502f/docs/evidence/transfer-structure-audit-20261027/files-and-disposition.md)，同目录 changed-files.tsv、kanban-map.md。
- [可复现凭据/边界/结构及已知失败对照 @69941b81b](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/69941b81bec3a9e496580e6d6dd188370278db1b/docs/evidence/transfer-plan-revision-20261027/README.md)。仅合成凭据；不代表生产已发生泄露。
- [R2 复审与 #1994 回读证据 @b50d930d5](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/b50d930d5da37f378ea54cd24225c6baf8120792/docs/evidence/transfer-plan-r2-20261027/README.md)。该 commit 不在计划分支祖先中；R2 PASS 不覆盖 R3。
- [daemon-request 交接与 host 术语 @f7cd7b5f5](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/f7cd7b5f5a58ccfddf83e77dd15bbde09acc1895/docs/design/daemon-request/handoff-2026-10-27.md)。它提到的目标 design.md/公共机制不能当作已经实现。
- [A 公共命令面与三线分工 @485dab4db](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/485dab4dbaee3c1af8bf8f9adcfc375a46ca7343/docs/design/daemon-request/cli-surface.md)。分工为本次衔接依据；其九动作/显式版本段落是本次用户选择之前的快照，活动命令以 R4 为准，并通知该线同步。
- [#1982 设备凭据契约 @6f6ad4c6a](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/6f6ad4c6afe190eef0a58ab1b1a6c47d34573e12/contract/identity/transport-credential.md)。设备凭据定义与 agent 实例授权分开。

旧读数：焦点 1220 passed/1 skipped 及约 17% 中断的全套日志绑定不充分，只作历史记录；结构四处 FAIL 已复算；三项指定失败在固定 dev 产品代码与 WIP 都有复现，不能推论其他测试全绿；smolvm 与完整真双机 NOT_RUN。本次没有实施产品或更新这些运行结论。

本轮流程：记录 2026-10-30 线性开发与 Pi → SDK 裁决，沿用已获独立复核的 R4.2 单据模型 → 推送更新 #2004（准确 head、目标分支、单文件 diff、证据入口）→ 通过 Harness 通知 codex-daemon-request 同步最小声明，不请求其承接 B 业务评审；交 cc-sw-review 对本 head 独立复核。Q1 原清单保留，旧 Q3 的替代关联需求撤销；新的并发/释放规则须核验，不据用户方向确认自签评审通过。用户已取消本次飞书发送，本轮不发送、不自动排队恢复旧发送。

B01 当前产物：[WIP #2147](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/2147)，分支 `feat/agent-move-b01-contract`，从 B 的 `intg/agent-move@3252c112c` 起步，仅新增可执行契约候选与证据。卡 `71769467-5afb-43dd-b4a0-b9207d3aa32a` 已 doing、0/3；不把候选 schema/参考实现当作产品方法已接线，具名领域承接仍须落实。

实施流程：本次用户已批准线性开发 → 由标准 Kanban agent 去重并登记/认领 B01 → 契约与对应领域责任收口、精确 head 独立评审 → 按依赖顺序推进后续工作项与 PAC 实施 → Pi、SDK 精确集成 head/产物独立验收；move 做完交 Allen。A 的消费按 §10.4，不增加等待 A 先合 dev 的门。作者不自签、不合并自己的 PR；新 head 的实现通过与否以新证据为准。

| 用户问题 | 本文回答位置 |
|---|---|
| 是否符合 #2006，是否记住审核意见 | §1 逐条对照与差异；§12 S1–S7；§13 O5–O10 |
| 之前完成什么，dev/WIP 是否一起改 | §2、§10.3、§11；已有基础保留，越界部分共同整改 |
| 操作人、命令、效果，invite/request 谁先 | §3–§4 两条完整旅程与发现；同单号、固定摘要、源显式 start |
| M1/revision 是什么，怎样改条件 | §4 单号/参数与不可变计划；§7.1 安全取消后独立新建、双方重新同意，不管理后继链 |
| 单个 agent 能否开多单、多目标邀请如何处理 | §4.3、§6；邀请可多份，源申请/接受只能选一张，安全关闭后才能改选 |
| 幂等、过期、撤回及迁移中断 | §6–§7；持久幂等、源唯一选择与执行围栏、分阶段取消与恢复 |
| 假身份/同名重建，host 能否把控源 agent | §5；精确实例、真实委托、可信绑定与 B 两处准入 |
| 凭据/上下文/工作目录隔离 | §8；语义凭据搬运、整包密封、实际 OS/IPC 隔离 |
| daemon request、独立模块、核心侵入与开发节奏 | §10 目录/端口、A/B/C 分工及旧入口逐项处置；私表不开放给业务，公共依赖单独评审 |
| smolvm 是否删除、旧 PR/看板怎么办 | §11；条件退役，保留历史，被替代不冒充完成 |
| 还差哪些开发、先做什么 | §11 P0–P7、§13 开工门；先 Pi，后 SDK，测试负例同步 |
