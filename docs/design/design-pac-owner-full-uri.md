# PAC owner 完整 URI 与派单前解析（设计草案）

> 2026-09-14 · astra-pac-owner-uri · **docs-only / draft，不授权实施、迁移或上线。**
> 依据：hq spec `spec-pac-owner-full-uri-design-2026-09-14.md`，含追加 1–5。
> 追加 3：kanban 已按本机名册 fail-loud 定稿，不是本单阻塞方（见 §5.3）。
> 追加 4：h2b owner 审 #515@781d981b 的 owner_migration 部分，方向放行，补两格明确一格（见 §2.2/§2.4/§4.1）。
> 追加 5：Allen 裁 G1 = A 精确相等（见 §5.1）。
> 代码读数固定在 harness-bridge `b3e0e42d24ea2ea727d17bb78a9b124ecf24e880`；
> 本轮（追加 4/5）新增读数仍取该树——分支相对 dev 的自身增量只有 docs，且 dev 其后 4 个提交未触及所引 src 文件。
> 可复核命令、材料指纹、读数射程见 [证据附件](../evidence/design-pac-owner-full-uri-evidence.md)。

- **服务的目标状态**：PAC 图里每个节点的对象唯一可解析、跨机器不撞名；派单意图不明确，在写入前拦下澄清。
- **把既有裁定视为已生效，本单是否仍存在**：存在。完整 URI 已定，基线 `pac/graph.py:128–155` 仍拒 URI，`owner_migration.build_plan` 六库不含 PAC。
- **生效方向**：存完整 URI；不明确先问，再写图。本文回答怎么落，不重开“是否存 URI”。
- **G1 已裁（追加 5，Allen 2026-09-14）**：**A 精确相等** —— 只有验证后 URI 与节点 owner 完全一致的主体能置 flag；同 owner 段其他 agent、同名跨机 agent 均无权；⛔ 不设显式代完成授权（C 不采纳）。owner 自己去请示别的 agent 或人，属它自身处理过程，PAC 不建模。锁定的双向矩阵见 §5.1。
- **本轮验收**：设计 + 每条修法的 NOT_RUN 测试/变异矩阵 + 全仓 Ruff。追加 2 豁免本轮实施实跑，不豁免设计正确性、安全边界、后续实施的真实协议测试/变异/全量。

## 0. 范围与既有裁定的边界

本稿取代 [旧概念稿](design-pac-graph-flag-reactor.md) §2 **PAC owner 短名**规则；不改 `agents.actor`、`nodes.actor_name`、本机目录键、launch name 等存名字的规则。实施时同步旧文链接/CLI help/可分发操作指引，本轮不把未来用法发布成已存在命令。

cc-kanban 的具体故障是 **PAC 拒绝它已有的完整地址**，不是发现方没理解意图。追加 1 给出名册卡 `940c2e03` 的地址 `agent:alicesmith:node-hq-01.example-tailnet.ts.net:kanban`，调用方因 PAC 拒四段而交 owner 段。该故障与“新派单短名可能歧义”是两条输入路径，不能拿后者重新归因前者。

必须区分三个对象：

1. **指派对象**（node.owner）：人或一个具体 agent，决定给谁发通知。
2. **请求的执行身份**（CLI --actor）：调用方声称代表谁，不是凭据。
3. **可信调用身份/授权关系**：决定能否把 flag 写进去；URI 唯一性不提供身份认证。

## 1. owner 形态、校验与存在性（D1）

### 1.1 存储合同

| 对象 | 存储/输出形态 | 边界 |
| --- | --- | --- |
| 人 | `user:<owner>` | 人不绑定机器，接收方自己的 Squire 路由负责送达 |
| agent | `agent:<owner>:<machine>:<actor>` | 四段均保留；机器不同或 actor 不同即是不同地址 |
| 节点 owner | 上述两类之一 | task/clock/actor/end 均同形；actor 节点的 owner 是责任人/agent，不等于其被启动对象 |
| graph.created_by / 新建 --by | 同样的 Principal URI | 避免节点已全限定而图 activate/close/stop 仍比较裸名 |
| nodes.actor_name / agent 目录键 | 仍是本机名字 | 不拿 foreign URI 当本机目录/生命周期操作的键 |

`owner` 保留为真实序列化字段，不改成计算属性；show/snapshot/context 的 `owner` 字段直接输出存值。新增可选显示标签只能作 UI 展示，不参与相等、授权或投递。

### 1.2 校验归属

- agent 拆解唯一使用 `hyprial.uri.parse_agent_uri`，构造唯一使用 `canonical_agent_uri`；不另写 `split(':')`。
- 人的现有严格形状在 `squire.addressing.UserDeliveryTarget.parse`：`user:`、非空 owner、无冒号、无首尾空白。实施把纯 user grammar 提取至 `hyprial.uri`，原类委托；不让 PAC schema 导入带网络/文件依赖的 squire 模块。不改变其它 user 地址的既有语义。
- PAC 边界额外拒绝控制字符、段内空白与首尾空白。注意现有 agent parser 仅保证四个非空段，**不能声称它已经验证这些字符**。不自动 trim/lowercase owner/machine/actor，不对历史 URI 做 DNS 别名归并或邮箱截断；新输入需要修正时返回具体字段。
- 拒绝裸名、`agent:squire`、host、adapter、route 作为**已编译的 owner**。人/agent 是责任主体，route 是投递出口；需要 route 通知是另一能力，不偷塞 owner。
- CLI/模板的裸名只能作为 §3 的输入糖；核心建图 API、daemon 写边界及所有直接调用者只接收完整 Principal URI。验证在 INSERT/版本递增/发通知前完成，不留短名写入后补全。

### 1.3 “合法”“存在”“可达”分别报告

完整 URI 的写入仅做形状及图结构校验，**不要求本机认识，不以在线状态作为持久地址合法性**。允许尚未启动但已完整指定的 actor；这不承诺对应 runtime 存在，更不允许本机启动 foreign actor。

派单工具可在写入前做有界目录检查，报告 `observed / not_observed / unavailable` 与来源/时间；这是建议性诊断，`not_observed` 不是“对象不存在”。完整地址不因远端离线被改派本机同名对象。投递时失败写入耐久通知结果，重发使用同一地址，不回退给人或其它同名 agent。

被否：本机 agents 白名单（拒跨机）、网络 ping 成功才准入（把时效当身份）、截取 URI owner 段（丢人/agent区别）、全部 owner 统一四段（给人捏造机器/actor）。

## 2. 旧图短名迁移（D2）

### 2.1 分两种迁移，不混为一次字符串替换

- **形态迁移**：旧短名 → 确切人/agent URI；可能无法从旧值恢复，需人工确认。
- **身份改名**：已知旧 owner → 已分配最终 shortname；URI 的 owner 段改变，machine/actor 不变。见 §4。

只在获准的维护流程操作生产；本稿没有打开生产库。隔离演练使用真实版本 schema、真实 registry 序列化和受批准的脱敏副本，不能手建一个 `agents(actor)` 就声称完成迁移验证。

### 2.2 计划、确认、应用

1. 停写范围按 §2.1 的两种迁移分开，⛔ 不给形态迁移套全节点停写（追加 4）：**形态迁移**（短名 ⇒ 当前 owner 的 URI，身份不变）只停本机 PAC 子系统 —— PAC 图写入、clock/actor 队列与通知重发，并阻止旧 CLI/进程重新进入，其它子系统照常服务；**身份改名迁移**（owner 段换成最终 shortname）按 §4.1 全局停写、各节点统一执行。两种迁移都先取得 SQLite 一致快照（正确处理 WAL），不只复制主文件。
2. 对**所有图（含已关闭图）的全部节点 owner**及 §2.3 各类字段作只读清点。输出数据库标识/快照摘要/schema/journalId、图/节点/列、原值、候选完整 URI、证据来源、状态。
3. 自动映射必须有能证明该条旧引用含义的耐久来源：例如旧图的明确主体种类和创建时主体绑定、与该图关联的既有 assignment/注册记录。**当前“本机只查到一条”、图上已有同名、现在只在线一个都不证明历史唯一**。同一裸名不同图可对应不同 URI；映射键至少为数据库+图+字段/行，禁止全库裸名字典。
4. 人和 agent 撞名、跨机同名、来源不完整、历史绑定缺失、坏行全部列人工确认；候选可以为空。人工指定完整 URI、理由与确认者，不能点“默认第一项”。声明“指的是 alicesmith 这个人”时应确认 `user:alicesmith`，不能因手里有 kanban 的 agent URI 而把 agent 当人。
5. 所有条目已唯一确定才生成可应用计划：绑定输入快照、原值、确认记录和目标值摘要。应用前重新核摘要/行值/CAS；漂移则废弃计划重新确认，不写部分图。
6. PAC 库内一笔事务完成当前地址迁移、schema 版本、迁移审计及 `migration_baseline`；失败整库回滚，版本不前进。不自动猜、不装双读 fallback、不以空 owner 或“unknown”占位放行。
7. 完成后审计全体当前节点为完整 URI、引用闭合、原 flag/round/activation/key不变；重取 snapshot 后才开 PAC 服务。旧格式库只允许独立迁移工具做只读诊断，业务写入/投递明确拒绝并指向报告。

计划输出必有：`tree / files / parsed / parse_errors / total_items / findings`，另列 db/schema/总图数/总 owner 数/自动确定数/待确认数/损坏数。不可测量不得并入“0 歧义”；空库有显式零记录，库未读到不是空库。

### 2.3 字段分类与历史保真（D3）

**不能只是给通用 migration 清单追加文件名。** PAC journal 有 `journal_no_update` 触发器，旧历史还存整张图/plan。所有 TEXT 上做通用替换既会被触发器挡住，也可能把正好等于 owner 的 node_id 改掉。

| 数据 | 处理 |
| --- | --- |
| nodes.owner、graphs.created_by | 迁为确认后的完整 URI，后者也是当前 graph-owner 授权依据 |
| 当前 nodes.flag_set_by / graphs.activated_by / closed_by | 保留事实来源；新写入分清 Principal 与 system/end-node 来源，历史 `migration`/`reactor`/end node id 不当 URI，不按内容撞名改写 |
| flag_events、journal | 逐字保留旧行，保持追加性、seq/eventId/时间；不删除触发器，不重写历史 |
| notifications recipient / sender / plan_json | 旧发送记录逐字保留为迁移档案；当前可重发信封与 assignment 输入需在迁移中生成**完整的规范地址版本**，逐字段按旧 plan 的主体绑定转换，而非用当前图重新计算过去的前驱/版本 |
| actor_activations.launch_json / launch_digest / identity_marker、operation_id | 不做递归字符串替换；launch 身份若须改名走其生命周期迁移流程并重新验证摘要/归属，否则阻断切换，不能让 launch_digest 与内容分家 |
| graph/node id、edge、brief_ref、reason_ref、正文、路径、actor_name、operation_key | 非身份字段，逐字保留，即使含旧 owner；不增加授权能力 |

**建议实施结构**：迁移将旧通知完整原行写入仅追加的 PAC 迁移档案（含原行主键、旧 schema、plan 原文），现行 notifications 保持原 `(event_id, edge)` 键，保存一次确认生成的规范信封/plan。两者角色不混：档案只用于事实复述，绝不参与当前授权或运行时短名解析；正常重发只读新地址。不新建 alias 服务/动态 fallback。

- 旧 plan 中每个身份字段按该事件自己的确认绑定改；无法解释就停止。通知文本/round/时间/触发 key 不变。迁移档案有旧→新字段差异与确认出处，不能把新地址伪称当时逐字发出的地址。
- 已提交到传输层但 PAC 未记 delivered 的通知，须先核耐久 delivery/effect ledger；`unknown` 阻断该库切换，不先重发。不得更换 effect/idempotency key 假装新通知；重地址与传输缓存是否兼容必须在实施段验证，不兼容则保持停发、人工收口后再迁。
- 新 `migration_baseline` 建立规范当前状态/assignment（完成轮次不变）、当前地址版本和历史边界；cursor floor 使旧消费者重取完整 snapshot。公共投影增加显式身份格式版本，旧消费者不能把 schema 不识别吞成空图。
- `projection.audit/restate` 必须区分迁移前历史复述（档案+当时结构）与迁移后基线继续折叠；不能拿新 owner 对旧通知原文比较、再忽略 recipient drift。历史读取豁免新 owner 写入形状，不豁免事实保真/审计完整性，也不授予运行或授权资格。

被否：整库替换（误改事实/键）、只改 nodes（通知/投影仍短名）、启动时“能迁多少迁多少”（混合状态）、迁移失败仍按旧逻辑跑（隐性回退）。选择“当前规范态 + 明确历史档案 + 基线重同步”，而非删掉历史或牺牲当前可运行性。

⚠️ 与 §2.4 的边界：“启动时能迁多少迁多少”被否的是**混合放行**（迁一半就开 PAC 服务）；§2.4 的无人值守路径是“可证明唯一的全部迁完才开，否则整个 PAC 子系统隔离”，两者不冲突。

### 2.4 无人值守自动升级路径（追加 4 第 1 格）

节点在 03:17/15:17 由 daemon 自动执行升级（`autoupdate/__init__.py:3,59`），升级重启后无人在场。现有身份迁移在 daemon 构造早期运行，**中止即整 daemon 拒启**（`daemon/application.py:659` 调 `owner_migration.migrate_owner_if_needed`，注释明言 abort stops the daemon starting）。PAC **形态迁移**不得挂进这条整机拒启路径，否则一条待确认的 PAC owner 会让自动升级后的节点静默停机：

- **能自动迁的**（该条目有 §2.2 第 3 步要求的耐久绑定证明唯一）：启动时在 PAC 库单笔事务内迁完；失败整库回滚、按下一条隔离。
- **不能自动迁的**（存在任何待人工确认条目、清点失败或库不可测量）：**只隔离 PAC 子系统** —— PAC 写入/flag/派发/通知置 migration required，结构化拒绝并指向清点报告；节点其它服务照常。⛔ 不整 daemon 拒启，⛔ 不带病放行 PAC。
- **告警**：⛔ 不能只在本地显示状态；走现有升级失败告警 + 落盘标记通道的**模式**（先落盘、再发送；`autoupdate/alert.py:write_failure_marker`）。

通道载荷读数（本轮实核，树 b3e0e42d）：`write_failure_marker` 载荷是自由文本 `summary`/`detail` 加固定 `kind="upgrade-restart-failed"`、固定文件名 `upgrade-restart-failed.json`，每次整文件覆写；发送经 `_send_owner_message` 给 owner DM，never-raises，结果由 `record_alert_outcome` 回写 marker 的 `alert` 字段；现有两处调用均带 idempotency_key（`cli.py:6618–6642`、`:6717–6732`）。⇒ 通道**能**承载待确认条目清单（detail 为自由文本），但有两个不能照搬的格子：

1. **独立 marker 文件与 kind**（拟 `pac-owner-migration-pending.json`、`kind="pac-owner-migration-pending"`）。复用 `upgrade-restart-failed.json` 会与升级重启失败**互相整文件覆写**，且 kind 语义错误。复用的是 write-then-send 顺序、owner DM sender、AlertOutcome 回写与 never-raises 合同，不是同一文件。
2. **DM 不当唯一清单载体**。告警正文列出：待确认**条数**、有界前 N 条样本（库/图/节点/字段/原值/候选摘要）、marker 与完整清点报告的本机路径；完整清单以落盘报告为准 —— 消息通道有截断风险，条数与路径必须在正文最前部。

其余合同：idempotency_key 绑定（host、PAC 库摘要、待确认集合摘要）—— 同一集合重复启动不重复告警，集合变化（新增/人工处理减少）再发一条；发送失败时事实已在盘上，下一次启动或下一个 03:17/15:17 周期按同 key 重试发送并回写每次 AlertOutcome；delivered 以 messageId 为据，“调了 send”不算送达。对照组：待确认数为 0 时不写 PAC marker、不发 PAC 告警（通道心跳由既有 upgrade outcome 消息承担，不新增噪声）。

邻近事件（立规矩当场列）：升级重启失败与 PAC 待确认同时发生 —— 已补，两个 marker 文件各自成立互不覆写；DM 长度上限截断长清单 —— 已补，条数+样本+盘上全量，Lark 实际上限留实施段实测；两次告警之间集合被部分人工处理 —— 已补，key 含集合摘要，变化即再发；**身份改名迁移**跨库半完成与 PAC 隔离同时出现 —— 有意不放宽：身份改名半完成仍禁整节点服务（§4.1/§7.3），⛔ 不因形态迁移的隔离规则连带松绑。

### 3.1 唯一解析职责

**派单方 agent 问人；编译器判断是否允许产出；daemon 只守最终写边界。** 不让后台 daemon 自己等聊天回复，不让渲染器挑一个最像的候选。

提议一个 PAC 编写期 resolver 端口供 CLI、模板渲染器、routine 建图器共用（拟新增接口，不是现成命令）。产物包含 `input / kind / candidates / selectedUri / source / observedAt / scope / completeness / reason`；resolved 只在确实唯一且意图类别明确时出现。解析报告不是授权凭据。

1. 输入完整 URI：严格校验后原样固定，不剥 owner，不再按本机白名单过滤。
2. 输入显式 `kind=human`：匹配人物权威名册/已确认人名映射，产物为 user URI；不能凭 agent 的 owner 段反推任务必交给人。
3. 输入显式 `kind=agent` 或裸名：按明确匹配规则（名字精确匹配，不做模糊相似度自动选择）收集全体候选；裸名还需把人物同名候选并列。
4. 候选 URI 去重但保留多来源；0 条=unresolved，多条=ambiguous。任一来源失败/缺页/范围不明=resolution unavailable；不能按已拿到的唯一一条放行。
5. 唯一候选只在**可声明完整的请求范围**内自动补全。当前 `targets` 是 live delivery promises，不是全局登记目录；仅从在线 targets 看见一个，不能证明离线同名不存在。没有足够目录覆盖时先列已知候选，要求完整 URI/人确认，不能自造“全局 complete”字段。
6. graph-local 待启动 actor 可成为候选，但须有明确 owner+执行 machine+actor 的创建声明（用 canonical 构造器），仅有 actor_name 不够。它与外部同名候选冲突照样停，不优先本机。
7. 多字段/多节点整体预解析后才能写图；任何一项不清楚，graph/node/version/journal/outbox 的写入数均为 0。单次 add-node 至少保证该动作无写入；不撤销调用者在前面明确完成的其它动作。

### 3.2 澄清与再次提交

示例交互（四机 squire 的完整候选值由真实目录记录提供，不在本文捏造机器名）：

> “squire”对应四个 agent。请指定完整 URI，或指定人/机器/actor 限定；以下列每个候选的完整 URI、类别、机器、来源及观测时间。此时尚未创建图。

用户选中某 URI 后，把该**具体值**及确认出处写入编译输入；不能保存数字序号而下次重新排序解释。模板变量名宜为 `owner_uri`；渲染成功的 PAC 只含 URI，不把歧义清单写成占位 owner。临近提交再次验证所选值与请求作用域/图版本；目录变动不重选人，绑定失效则再问。取消、超时、非交互入口都返回结构化未完成且零写入，绝不猜。

CLI 的短名解析与既有普通消息 alias 解析边界不同：旧消息发送的“仅本机注册表查名”规则不在此单改写；PAC 编写有跨机意图，不能偷偷复用该 helper 并继承本机优先。render/CLI/routine 调同一编写端口，避免三套候选顺序。

拟新增错误分类：owner shape invalid / ambiguous / unresolved / resolution unavailable / migration required / principal unverified。实施时落实统一 `ipc_errors` 注册和 PAC 错误映射；本稿不声称这些字面量已存在。输出包含候选和原因，不能只写“unknown”。

## 4. owner_migration 与一次 shortname 切换（D5）

### 4.1 清单与顺序

在 `daemon/owner_migration.build_plan` 纳入 **pac-graph.sqlite3 的专用 planner**，不是把文件名放进通用 TEXT 扫描循环。planner 对 schema 中每张表/列归类，未知结构或身份列 fail closed，报告未覆盖数；§2.3 历史排除只针对 PAC，不放宽其它库的残留规则。

建议整体顺序：

1. 完成 shortname 分配/同人跨机同值验证，取得批准的旧 owner→最终值映射（不要先切邮箱再切短名）。停所有参与写者，取得所有库一致备份与 schema/文件摘要。
2. 建一个完整迁移计划：六个既有库/两文件 + PAC 专用计划；所有残留/短名歧义在**任何 apply 前**解决。PAC 形态与 owner 段可在计划里一次得到最终 URI。
3. 既有迁移框架应用每库事务，PAC 同事务落 §2 的规范态/审计/基线。**当前 apply_plan 是逐库事务，不是跨库原子事务**；须记录耐久迁移阶段/输入摘要并在恢复时核各库完成状态。
4. 最后发布新 owner 身份并允许新代运行；PAC schema 和地址版本均达标、未决项为零才开通知/flag。失败后不得只改回 settings 就声称回滚；已提交的库按阶段恢复/续做，未恢复完整前不 serving。

本节的全局停写范围只属于**身份改名迁移**；**形态迁移**的停写范围见 §2.2 第 1 步（只停本机 PAC 子系统），⛔ 不套全节点停写（追加 4）。

仅扩大清单不够：`detect_previous_owner` 只读 `agents.owner`。某机器已经切过身份时，agents 已新而 PAC 仍旧，启动入口会直接 return 0；因此 PAC 形态/地址版本预检必须独立于该检测，补迁使用显式、经确认的旧→新映射和迁移 ledger，不从 peer URI 任意推自己的 previous owner。

**升级前预检（追加 4 第 2 格，P3 验收项）**：`hyprial upgrade --check`（或 doctor 子命令）在安装前只读运行 PAC 专用 planner 的清点段，提前报告「装上这个版本后会有 N 条 PAC owner 需要人工确认」：N、按库/图/字段的分解、以及 N=0 时的显式零记录 —— 带分母（扫了几库/几表/几行、成功解析多少），不可测量条目单列，⛔ 不折进 0。预检独立于 `detect_previous_owner`（agents 已新 PAC 仍旧时照样要报），不写任何库，不要求 daemon 在跑。它与 §2.4 是同一判定逻辑的两个时点：预检在装之前给人留出确认窗口，§2.4 是没人确认时装完之后的兜底。

### 4.2 跨节点引用

本机身份迁移不能写另一台机器的图。切换清单必须枚举每个**持有旧 URI 的图库**（不仅迁 owner 本人的机器），在各节点执行同一批准映射，报告完成/未完成/不可测量。只改 URI 的 owner 段，保 machine/actor；不把 foreign URI 搬到本机。

未完成节点停 PAC 派发/授权并显示 migration required；不设置旧 URI alias 回退。确认过的 shortname “此后不改名”减少未来切换次数，但不免除这一次的远端引用迁移，也不证明 actor 同名重建仍是同一个授权实体。

## 5. 消费方、flag、clock 与通知比较（D6；G1 已裁 = A，追加 5）

### 5.1 两格必答之一：`flag set/reset --actor`

**输入形状定为与 owner 相同的完整 Principal URI**。它是 claimed actor/显式意图，不是身份认证。可信身份拿不到时返回 principal unverified，不以 node.owner、--actor 字面值或“同 OS 用户”补一个已认证 agent。

特别更正读数的作用域：`pac/cli.py:68–78` 的 `_actor_owner()` 用于 graph activate/close/actor stop；`flag_set/reset` 在 :449–473 直接把 `--actor` 传入 `_flag_command`，没有调用它。`reactor.py:473` 只比较字符串。因此“CLI 已验证所有 flag 的 principal”是不成立的。kanban 的 `--actor n["owner"]` 只能证明声明了该值，不能证明调用者拥有它。

实施须把 flag/graph管理的可信 caller 接线单独列出：本机人的既有可信 CLI 边界映射 `user:<resolved owner>`；agent 须取 daemon 已绑定的 actor/session 身份（若拟用现有机制，先核实际调用链与反冒充测试，不以 env 字符串代替）。公开 --actor 至多与已验证身份/授权集合核对，不能成为覆盖身份的开关。系统 reactor 修改 actor-kind flag 继续只走受控内部能力，普通 CLI 即使传 `reactor` 或自造 system URI 仍拒绝。

**G1 已裁 = A（追加 5，Allen 2026-09-14 飞书原话）**：「pac节点显然应该精确匹配，在上面那个例子中，只有 HQ 上那个 kanban 本身 才有权标完成。至于hq节点再去请示其它agent或者我本人，则是它自己的处理过程，不归pac管」。下表保留三候选的比较作裁定依据存档；**B/C 均不采纳**，⛔ 不设显式代完成授权，⛔ 不留灰度开关：

| 候选 | 比较规则及边界 | kanban 影响 | 代价/风险 |
| --- | --- | --- | --- |
| A 精确 Principal URI | 验证后的 caller URI == node.owner；user URI 同值不含机器；agent 每机器/actor分别授权 | kanban 不能仅传别人的 owner 置 flag，需由指定主体操作；人节点须人身份入口 | 最窄但现有代办流程可能需显式调整；同一人也不能当然代 agent |
| B 同 owner 段同主体 | 经可信绑定的 user/agent 解析 owner 后同值即准许；不能比较未认证输入 | 同 owner 的 kanban 可替该人名下所有机器所有 agent 置 flag | 将一名下任一 agent 的权限扩大到全部；主机/actor 粒度只剩路由，不再是授权隔离 |
| C 显式授权 | 以精确主体为基础，节点/图授权列出受托完整 URI、操作范围/有效期/撤销和审计 | kanban 只有拿到相应 grant 才能代办；操作日志区分 caller 与 represented owner | 可表达最小代办但新增授权管理及撤销竞态；grant 配置不是 owner 意图解析器的副产品 |

**锁定的双向矩阵（按 A 逐向写死，⛔ 不留任何一向靠默认落地）**：判定式为「验证后的 caller Principal URI 与 node.owner **逐字相等**」；user URI 同值不含机器段。

| 方向 | 判定 |
| --- | --- |
| 同一四段 URI（经可信绑定验证） | 准许 —— 唯一准许的一格 |
| agent → 同 owner 段另一 agent（同机或跨机） | 拒绝 |
| agent → user 节点 | 拒绝；user 节点只有经可信人身份入口解析出同一 `user:<owner>` 才准许 |
| 人 → 自己名下 agent 的节点 | 拒绝；owner 是那个 agent 不是人，owner 请示人属它自身处理过程，PAC 不建模 |
| 不同 owner / 同名跨机 | 拒绝 |
| 代完成 grant（含撤销、有效期） | 不存在 —— C 不采纳，不建 grant 表、不留受托 URI 字段；「已撤销 grant」这一格随 C 一并消失 |
| 同名重建的 actor | 地址逐字相等即按 A 准许；主体连续性不由 flag 授权层解决（它只比地址），属生命周期/registry 层（#510 marker 围栏），本稿不给 flag 层加第二把身份钥匙 |

graph activate/close/stop 共用同一策略（与 graphs.created_by / graph-owner 精确相等），不在这些邻近入口偷偷按 owner 段放行。裁定不改变 §5.1 前两段的身份前提：--actor 仍不是认证，比较的是**验证后**的 caller URI；可信 caller 接线仍是 P1 待测项，不因裁定完成而免。

裁定之后，**地址等价仍不按 owner 段折叠**，候选列表仍列每个四段 URI。授权检查在 flag 的同一写事务内核图状态与 owner/授权修订，写入真实 caller 与所代表主体出处；拒绝不得留 flag/journal/outbox 副作用。

### 5.2 两格必答之二：reactor 通知发给谁

**定案：通知 target 就是节点 owner URI；agent 发那个 agent，user 发那个人。绝不把 agent URI 自动降为 `user:<owner 段>`。** 若业务目标是人，编写时就明确写 user URI。这个选择与 G1 无关，G1 裁 A 后同样成立：授权与通知是两个量，通知地址永远是 owner URI 本身。

- CLI sender `pac.cli.DaemonNotificationSender._recipient` 删除“本机 agents 判断，否则加 user:”规则，验证后传完整 URI。
- 常驻 sender `daemon.pac_actor.DaemonPacNotificationSender` 已向 typed delivery 传 `target=recipient`；维持透传且在类型边界校验，不加另一套 URI→人推断。两个 sender 的目标值必须逐字一致。
- 发送者身份不由 node.owner 反推；常驻 sender 现用 `_dispatch_service_actor`，CLI 与常驻要统一可信传输发送端。记录里的触发 actor/graph creator是审计归因，不能冒充传输 credential。
- turn / back / withdraw / overdue / launch_failed / actor_alert / resend 全部同一规则；失败仍保留未送达计划与具体错误。后台线程、finally/清理/回滚、facade 缺失、重试最后一次都不得把 unavailable 变成成功或无人可发。
- **裁定 B**：clock 升级接收人由 clock.owner 表达。routine 的 `escalate_to=user:...` 应直接存为 clock.owner，不再剥前缀。既有逾期集合仍为 clock owner **加直接 forward 前驱 owner**；本稿不把它缩成只给 clock owner，不更改 #500 的 guarded 判定/固定截止，不按 URI owner 段合并两个不同 recipient。
- `(event_id, edge)`/effect key 保持原去重粒度；多个逻辑边恰好指同一 URI，不擅自减少通知条数。无新的 retry/顺延策略。

### 5.3 消费清单

| 消费点 | 实施改动/不变量 |
| --- | --- |
| kanban `tools/sw/pac.py:129/138` | add-node --owner 传确认后的完整 URI，不截段 |
| kanban `:171` | graph create --by 传完整创建主体；声明主体与可信操作者分开 |
| kanban `:216` | --actor 同形；不再视 `n["owner"]` 为授权证明，按 G1=A 精确相等及可信接线改 |
| kanban `:116–121` | 同步移除临时“非本机 owner 拒绝”，以 PAC 完整形状闸替代，不删授权闸 |
| subscription/projection blockedOn | 就绪/完成条件保持；人/agent 用 URI 种类，不用 owner 是否碰巧等于 actor_name 或 requires 非空推主体种类 |
| context actor关联 / assignments | 不比较 `owner == actorName`；本机 actor 关联需匹配已冻结执行 owner+machine+actor 的完整绑定；foreign 同名不得关联本机 runtime |
| 生命周期 / #510目录 | `actor_name` 仍本机键；URI 中的 owner不是生命周期 marker，显示关联绝不授权 stop/adopt |
| show/snapshot/context/journal schema | owner仍显式字段；增加身份格式/历史边界合同，规范当前数据与历史来源分层，不少键；消费者重取snapshot，不 silent downgrade |

kanban 行号为追加 1 转述，非本轮直接读取 kanban 源码；实施前须给仓 SHA 与对应文件，确认实际参数协议及授权调用路径。追加 3：kanban 已按「本机名册为主、不在本机就 fail-loud」定稿（其 PR #142），不是本单阻塞方或依赖；上表 kanban 各行是 PAC 收 URI **合入后**的通知性合同 —— 届时通知 cc-kanban 移除其本地拒绝即可，⛔ 不把这些行读成 kanban 在等本单。

## 6. 在飞工作与实施顺序（D7）

| 工作 | 交叉点 | 顺序与明确不做 |
| --- | --- | --- |
| U3 PR-A #500 | schema 6→7、graph create/key、clock/node投影 | 先落 #500，再从新 dev 落 URI 实施，使用下一 schema 版本；不抢占7。保 guarded_by_node_id、operation_key 唯一/重放和原收件集合 |
| 本 URI 实施 | owner新写边界、解析、迁移、通知、投影、授权 | G1 已裁（A，追加 5）；可信入口仍先取证；写者+消费方+迁移作为同一切换合同，禁止新写者配旧sender |
| U3 PR-B | target→task.owner、escalate_to→clock.owner | 建议 URI合同落地后启用 PR-B；task直接存已解析target URI，clock直接存user URI。替代旧spec的“user剥前缀”仅限owner表示，不改升级对象/策略 |
| #510 agent独立目录 | 本机actor_name→目录、同名重建 | 设计可并行；目录键不改URI。执行绑定/foreign引用拒绝与#510会签，不能因owner完整就按URI创建本机目录 |
| hq #103 / harness #499 owner-shortname | 最终owner分配，一次切换，peer图残留 | 分配实现与可信跨机读回先于身份切换；PAC专用迁移必须纳入切换前演练。若URI代码先落，形态可先迁为当前owner URI，但正式改名只一次到最终shortname，不先切邮箱 |

`#499` 是设计，不能拿 PR 状态证明生产已切换；本轮只读固定文稿。各机器先预检/人工确认再统一停写切换，不能让某一节点的自动启动迁移代替整个持有者清单。

`#500` 同 operation_key 重放不能默默接受调用者这次传了不同 owner 的建图意图；URI实施需校验已存图的主体绑定与已编译请求相符，冲突结构化拒绝，不能替换已有图 owner 或建第二图绕幂等。不得改变 routine 的 key组成、circuit breaker 或 max_in_flight。

## 7. 分期、闸门与测试/变异矩阵

### 7.1 分期

- **P0（本轮）**：docs-only；交本稿、证据、矩阵、G1 问题（追加 5 已获裁定并回填 §5.1）；不是实现批准。
- **P1（先决合同）**：G1 已裁（A，追加 5）✅；仍需：确认可信caller端口与kanban真实调用；读取目录可覆盖范围/分页合同；确认#500落地树和迁移版本。缺一不编造协议桩。
- **P2（实施）**：共享纯Principal grammar、编写resolver、核心写边界、所有sender/投影、G1授权；同步kanban/routine合同。完整URI不会授权远程操作本机actor。
- **P3（隔离迁移验收）**：旧版本/WAL副本、人工确认、历史档案/基线、跨库部分提交与重启、已切owner补迁、跨节点引用清单；**升级前预检 `hyprial upgrade --check`（§4.1，T21/M21）与无人值守自动升级路径（§2.4，T20/M20）为 P3 验收项（追加 4）**。测试/变异/全量通过后才申请生产窗口。
- **P4（另行授权）**：备份/停写/全体持有者迁移/重取snapshot/启动/双向收件验证。无授权不做生产。合并后按dev合并SHA核CI/tag轨道，PR绿不代替合并绿。

### 7.2 真实形状与执行纪律

以下 **全部 NOT_RUN**，T/M 是拟议验收编号而非已存在测试名称。每行实施前固定测试文件、输入来源、预测失败集合；同一次 M 在完整相关文件运行，不用 `-k` 隐藏多红；记录 collected/失败断言/副作用集合，恢复该 M 后重跑同文件。未达到预测范围就解释或改判据，不换突变充数。

现有 `tests/tests_helpers_pac.py:register_cast` 手建 `agents(actor TEXT PRIMARY KEY)` 仅供旧短名路径，**不够证明新身份协议**。新测试用 `hyprial.agents.registry.AgentRegistry` 创建真实记录/序列化、真实 PacGraphStore+migrations、UserDeliveryTarget 的严格 parser；targets/typed deliver 使用获准隔离端口的真实响应capture或真实协议实现。未取得新目录/授权capture则 NOT_RUN/BLOCKED，不手填假 `complete=true` 或伪造可信principal。

| T/M | 修法与正反例、要断言的事后状态 | 同一次变异及预测失败点 | 状态 |
| --- | --- | --- | --- |
| T1/M1 D1形状 | human/agent合法；空段/多段/空白/控制符/host/route/裸名拒绝，DB/version/journal/outbox零变更 | 放宽非Principal或空白，非法形状零写入断言失败 | NOT_RUN |
| T2/M2 D1跨机 | 本机未登记的完整foreign agent可建；离线诊断不改地址；声明未启动actor完整绑定可编译 | 恢复本机known-owner条件，跨机准入断言失败 | NOT_RUN |
| T3/M3 D4歧义 | 四机同名squire＋人/agent撞名；所有候选完整列出，整图零写入 | 选第一项/本机优先，候选集与零写入断言失败 | NOT_RUN |
| T4/M4 D4未知 | 0候选、目录超时/缺页/未知覆盖、只在线一条；分别输出原因，不当唯一 | 把来源失败当空集合或在线唯一，unavailable/未写图断言失败 | NOT_RUN |
| T5/M5 D4确认 | 选择具体URI后重排/消失/目录变动、取消、非交互；不换目标，失效再问 | 保存列表索引，固定URI断言失败；先建图再问，零写入失败 | NOT_RUN |
| T6/M6 D2映射 | 已证明唯一自动迁；同裸名跨图不同主体、人/agent不明、坏记录列人工；缺确认整库不apply | 用全局裸名字典/当前本机唯一，目标绑定/原库保真断言失败 | NOT_RUN |
| T7/M7 D2原子 | 确认后改原值/版本；事务中故障、断电恢复、重复运行；不部分迁移，schema不假前进 | 去输入摘要/CAS核对或先升版本，拒绝漂移/原子性断言失败 | NOT_RUN |
| T8/M8 D3历史 | journal触发器仍在；原事件/通知档案逐字同；迁移后assignment/round与历史边界正确，旧cursor要求resync | 通用TEXT替换/忽略audit recipient drift/不设floor，历史保真或重同步断言失败 | NOT_RUN |
| T9/M9 D3投递在飞 | 已提交/未回写ledger、未知delivery、重发同effect；未知停发，不冒称未送达 | 换key或将unknown作未发，重复投递/阻断断言失败 | NOT_RUN |
| T10/M10 D5改名 | 六库两文件+PAC真实schema；同owner字符串出现在node_id/path/body/marker均不误改；URI只改owner段 | 漏PAC/递归replace，覆盖计数或非身份字段保真断言失败 | NOT_RUN |
| T11/M11 D5补迁恢复 | agents已新PAC旧、无agents库但有PAC、peer持有旧URI、跨库中断/清理失败；明确阶段，不直接serving | 仅依赖detect_previous_owner/只改回settings，补迁与恢复闸断言失败 | NOT_RUN |
| T12/M12 G1形状与身份 | --actor完整但伪称他人；缺可信caller；reactor魔法名/actor-kind flag；全拒且无副作用 | 直接信--actor或node.owner，反冒充断言失败 | NOT_RUN |
| T13/M13 G1=A矩阵 | 预期已按追加5锁定为§5.1矩阵：仅同四段URI（user同值）准许；跨机同owner/同机不同actor/agent↔user/不同owner全拒；无grant格；set/reset/activate/close/stop均测 | 换成B同owner段放行或漏任一拒绝向，授权矩阵断言失败 | NOT_RUN |
| T14/M14 D6通知 | user发人、agent发指定四段目标；CLI与常驻sender逐字相同；foreign同名本机无消息 | URI剥owner转user/本机alias，精确收件对象断言失败 | NOT_RUN |
| T15/M15 D6所有出口 | turn/back/withdraw/overdue/launch_failed/actor_alert/resend；失败耐久可见、原effect不变；线程/facade/finally/最后重试失败逐一注入 | 某出口fallback、吞线程失败或成功空messageId，未送达与错误可观测断言失败 | NOT_RUN |
| T16/M16 D6clock | clock owner+直接forward前驱各完整URI；同owner不同agent不折叠；guarded已完成不报，未完成固定期限报 | 只保clock/按owner段去重/改变guard事务，收件集合与截止断言失败 | NOT_RUN |
| T17/M17 D6消费 | owner字段集保留；blockedOn按URI种类；foreign同名不关联/停本机actor；requires不把人变agent | 恢复owner==actorName或反射丢owner，投影字段/关联/权限断言失败 | NOT_RUN |
| T18/M18 D7幂等协作 | #500后升级不丢guard/opkey；同key同主体复用、不同owner拒绝；routine task/clock分别保URI/user；#510目录键不动 | key复用忽略主体/clock剥前缀/目录用foreignURI，主体不漂移断言失败 | NOT_RUN |
| T19/M19 仓级守卫 | 新grammar叶子import、URI产生/拆解唯一、错误码与journal事件schema注册，真实fixture边界 | 引入第二parser/daemon eager import/未注册错误事件，对应全仓守卫失败 | NOT_RUN |
| T20/M20 追加4无人值守 | 耐久绑定唯一者启动事务内迁完；存在待确认⇒仅PAC隔离（写入/flag/派发结构化拒且指向报告）、节点其它服务照常、独立marker先落盘再发、告警含条数/样本/路径且delivered核messageId；0待确认无PAC marker/告警；集合变化再发、同集合不重发 | 隔离改成整daemon拒启或只写本地不发送，节点存活与告警送达断言失败；复用upgrade-restart-failed.json，双故障覆写断言失败 | NOT_RUN |
| T21/M21 追加4预检 | upgrade --check只读报N条待确认+库/图/字段分解+分母；N=0显式零记录；agents已新PAC仍旧时照报；不写任何库、不要求daemon在跑 | 预检改走detect_previous_owner或把不可测量折成0，漏报断言失败 | NOT_RUN |

焦点覆盖现有 PAC graph/cli/reactor/clock/migrations/transactions/subscription/context/projection/owner_home/clock_service/public_event_sync，加 owner_migration、uri_leaf、uri_construction_guard 和裁后授权端口测试；不是仅按文件近邻选。实施段每次全量完整落盘，红先同树对照；FAILED 与 ERROR at setup 分开。不能用本轮 Ruff 绿代替任何一行 T/M。

### 7.3 待裁/待测事项与邻近事件检查

- **G1 已裁 = A 精确相等**（追加 5）：双向矩阵在 §5.1 逐向锁定；B/C 不采纳、不留灰度开关。实施激活闸检查的「已明确选择的合同」即该裁定；可信 caller 接线仍是待测项，不因裁定完成而免。
- **待测（非待裁）**：目录完整范围、真实caller接线、kanban固定源码、重地址与delivery ledger兼容、各库部分提交恢复。已有读数/所差观测见证据附件；不是“已问人所以可上线”。
- 规则“唯一才补全”的邻近事件：只有在线唯一（已补：完整范围门）；用户选完排序变化（已补：保存URI）；真正新建未来actor（有意允许明确完整声明，不证明已在线）。
- 规则“只改owner”的邻近事件：owner同字的node_id/路径（已补：字段分类）；journal原文（已补：档案/基线）；actor同名重建（flag 层按 G1=A 只比地址；主体连续性属生命周期 marker 围栏，见 §5.1 矩阵末行）。
- 规则“迁移失败不运行”的邻近事件：回滚失败/后台异常/启动facade缺失（已补：阶段错误可见、不开PAC副作用）；整节点与PAC子系统故障边界不混，纯形态迁移只隔离PAC（§2.4 已展开成无人值守合同），但身份改名跨库半完成禁止整节点服务；具体状态接线需实施验证。
