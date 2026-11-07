# A：统一 CLI 命令面与最小公共基础（0.5.5.2 提案）

> 2026-10-28 执行更新：用户已批准 A 开工；五卡已建、DeepSeek Flash Max 开发、协调者验收。最新状态以 [执行记录](execution-2026-10-28.md) 为准；下文原审批描述保留作来源，B/C 审批边界不变。

- 状态：**A 实施待用户确认；move 用户面已按 codex-sw 转达的用户选择同步 R4，不再并列两套候选。只有设计，没有产品实现或 PAC 派工。**
- 日期：2026-10-28。
- 负责人：A/C 为 codex-daemon-request；B 为 codex-sw。
- 本文与 [总体设计](design.md)、[CLI 盘点](cli-inventory.md)、[IPC 盘点](ipc-inventory.md) 共同组成 #2010 的设计材料。
- 本 PR 当前仍以 `intg/daemon-request` 为文档审阅基线；这不构成 A 对 C 产品实现的依赖。
  A 的独立集成分支在设计批准后建立，拟名 `intg/cli-command-surface`。

## 1. 用户确认的流程与三线划分

用户要求开发前先有 design PR，之后规划任务、挂 Kanban/OrgFS，再经 PAC 并行处理。
最新明确：**A 完成设计留档后，由用户本人明确同意即可开始，不必等待 Allen。**
这是一条审批流程，不是对本稿或任意后续 head 的预先批准。C 保留 Allen 设计审阅要求；
B 按其用户/组织约定确认，本线不代其批准业务方案。

| 集成线 | 范围 | 目标与交付关系 |
| --- | --- | --- |
| A：统一 CLI 命令面与公共基础 | 统一命令树/参数/交互/输出约定，公共注册与 client，声明/OpenAPI 骨架、棘轮与样板 | 0.5.5.2；本线负责 |
| B：`intg/agent-move` | move 领域业务、授权/生命周期、所需 IPC、move 薄 CLI、实际迁移验收 | 0.5.5.2；codex-sw 负责；消费 A，不等待 C 全量迁移 |
| C：`intg/daemon-request` | 其余命令逐组迁移、完整声明与契约覆盖、全量边界收口、最后搬目录 | 0.5.6；本线负责；消费 A，并同步 B 已交付的成果 |

```mermaid
flowchart LR
  A[A: 公共接口与可消费基线] --> B[B: agent move]
  A --> C[C: 完整 daemon request 迁移]
  B -. 0.5.5.2 合入后同步 .-> C
```

B/C 开始依赖性实施的条件是 A 已交付双方约定、通过检查的最小代码基线。
设计对齐可以并行；不能仅凭 A 文档里写了接口就宣称该依赖已经可用。
B 的 ACL、host 接纳、身份/runtime 等外部依赖另列，不因 A 可用而自动关闭。

## 2. 原交接 a/b/c 的去向

原交接三项均是本次 A 的前置交付范围；总体目标仍完整保留。

| 原项 | A 中的处理 | 验收 |
| --- | --- | --- |
| a：接回 #1967 `feat/daemon-ipc-relocate` | 复用已审传输实现，在获批的 A 公共基线上移植、处理冲突、重新开 PR；原文的 base `intg/daemon-request` 随本次三线划分调整为 A，必须在新 PR 写明来源/新 base | 在新 head 实测；不继承旧 ACK 为当前 PASS；不恢复已删除生成物或旧兼容包装 |
| b：`docs/design/daemon-request/design.md` | 保留完整 IPC/25 命令族清单、方法参数/结果/错误/流式声明、OpenAPI 与预览 operationId；本文补 A/B/C 边界和审批条件 | 用户可独立确认 A 范围；C 总体设计仍交 Allen；move 模型确认不代替具体 schema 冻结或开工批准 |
| c：骨架 PR | 方法声明、OpenAPI 导出、import 棘轮，附 2–3 个简单方法的真实迁移与字节等价证据 | 有真实 client/daemon 调用，参数/错误正反例，文本/JSON 的 stdout、stderr、退出码证据；不能只有空 schema 或假 handler |

用户最新要求先有设计留档并同意开工，因此当前不先执行 a 的代码移植。
批准后执行客户端接回，再执行依赖它的骨架与样板；b 是本轮已经在交付的设计。
具体 PR 名单、任务依赖和验收清单在设计确认后固化到工作项，本文不冒充已建 Kanban/PAC。

骨架样板优先选择同一命令族的 `link.list`、`link.stats`。
如共享接线要求处理该组其他方法，必要的组内改动在同一 PR 收齐；不为凑样板跨多个命令族。
后续继续一命令组一 PR，避免 A 做一半、C 再重复接管同一组的业务迁移。

## 3. A 提供什么，B/C 各自实现什么

| 能力/文件责任 | A | B/C 消费方式 |
| --- | --- | --- |
| 公共 request client | 从 #1967 接回 endpoint、帧、超时和传输错误契约 | 双方只消费同一公共 client；业务写操作不由 client 盲目重试 |
| MethodSpec 与绑定/导出 | 提供参数/结果/错误 schema、可信上下文约束、幂等/逻辑流式/确认元数据及导出框架 | B 声明 move 方法并绑定其 handler；C 逐组补全其他方法 |
| CLI 注册/依赖注入/output | 本线统一公共接线与约定，维护参数/交互和唯一输出层 | B 提交 move 命令薄包装与实际 handler；共享文件变更先协调归 A，不能自行恢复 shim |
| 业务状态与权威 | 不实现另一套通用 move/lifecycle 引擎 | B 持有 move 业务；identity/runtime/ACL 各守其权威；C 不复制 B 已交付实现 |
| 预览确认 | A 定义通用协议声明与绑定规则，沿用总体设计的 operationId 要求 | 实际业务配对由所属命令组接入；move 双方授权不等同于短命预览 |
| 目录搬迁 | 先在现有布局建立公共边界，不全仓 rename | 最后由 C 安排 CLI/入口/打包的冻结窗口；旧 import/alias 不保留 |

声明的覆盖率分阶段报告。A 的全量盘点可指出哪些方法尚未声明，但导出的可用接口必须有完整 schema 和真实绑定；
不能给未迁移方法填 `{}` 或自动回落旧 router，伪称全量声明完成。新增方法必须登记，已纳入声明的方法不可倒退。
C 完成时，全部可达方法与声明/handler 集合一致。

A 不发布尚未实现的 move 命令为可用能力；新命令的接线与对应 B handler 同批生效。
原 CLI 表中的 `transfer.*` 拟议名仅是旧方案线索，不能约束 move 定稿或造成旧名称兼容分支。

## 4. CLI 公共规则与 move R4 用户面

依据为 [#2004 R4@3ac5381d0](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/3ac5381d00f53e275a998b451ac916dfedd23d25/docs/design/agent-move-refactor-proposal-20261028.md)
及 codex-sw 消息 `c0ff05fa-2268-5213-bf3f-3fe02b989c46`。对端转达用户在不可变单/八动作说明后确认
“可以，整理方案，然后可以通知daemon-request了”。本线已核对该固定提交与文档摘要；
此次选择确定用户面的模型，不等于 A/B/C 产品实施已获批或所有技术门已关闭。

共同规则：资源组 → 动作 → MOVE_ID，agent 一律显式 `--agent`。
不让同一位置同时解释 agent 名与动作/单号；不另开独立 `agent invite`。
现有 `agent host-invite` 创建空白访客身份，与 move 接纳不同，须单独列消费者和处置。
按交接新增的 host 术语约定，运行 daemon 的环境统一称 host；用户最新零兼容要求优先于
该历史交接的“保留一版别名”。A-04 梳理对应参数/帮助/字段的目标映射，实际改名随 B/C 各组迁移
同步契约与消费者，不单开全仓改名 PR，也不将旧盘点中的原始名称改成已经实现的新名称。

活动方案为 **8 个用户动作、10 种调用形式**：

| 动作 | 语法 | 语义边界 |
| --- | --- | --- |
| check | `agent move check --agent squire --to B` | 检查，不建单、不停源；结果不替代执行时授权 |
| request 新建 | `agent move request --agent squire --to B` | 源方建单，精确计划与自身意向按既定协议绑定 |
| request 同意 | `agent move request M` | 对已有邀请绑定的不可变计划记录源方同意 |
| invite 新建 | `agent move invite --agent <源完整URI>` | 本机为目标，核实精确源实例；不建空 agent |
| invite 同意 | `agent move invite M` | 对已有请求绑定的不可变计划记录目标方同意 |
| list | `agent move list` | 仅查授权可见单据；过滤选项另定 |
| status | `agent move status M` | 展示计划/同意/执行及恢复证据，包含下一步 |
| start | `agent move start M` | 显式源方开始，复核当前授权与同一固定计划，取得唯一 operation |
| cancel | `agent move cancel M [--reason TEXT]` | 按阶段表达撤回/拒绝或取消请求；受理不等于已安全取消 |
| retry | `agent move retry M` | 仅继续原 operation 可恢复步骤；不复活终态、不重新搬一次 |

moveId 始终绑定唯一 planDigest；双方同意绑定该 ID、摘要、角色及可信主体/实例。
已有单模式在 CLI 和 handler 两层拒绝 `--agent/--to/--ttl` 及其他替换计划字段。
不提供 revise、用户 `--revision`、旧 `--id` alias、approve 或以 `--yes` 替代授权。
角色由可信上下文与实际 host/实例核验，不能由命令动词或自报 owner 授予。

改权限、凭据范围、目标、运行条件或有效期须另建单并重新取得双方同意。
R4 的创建专用 `--replaces M` 是关联语法建议，精确 schema 尚未冻结：
先核验旧单的合法处置和源实例围栏，不隐式 cancel、不继承旧 grant/同意，也不绕过未结束的执行权。
即使不传关联参数，同源实例跨单占用仍有效；核心实例/owner/org 改变不沿用替代关系，跨 org 拒绝。
创建的 `--ttl`、`--idempotency-key` 精确规则随 §5.2 冻结；24 小时默认 TTL 仍是 B 的建议。

schemaVersion 表示格式；stateVersion/各权威游标表示并发或观测，不是可变计划版本或全网总序。
通用 preview operationId、业务 moveId、执行 operation 分别绑定不同事实；不拿 moveId 充当预览 ID，
也不拿短命预览替代持久双方同意。旧九动作讨论仅保留在
[历史快照 @485dab4db](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/485dab4dbaee3c1af8bf8f9adcfc375a46ca7343/docs/design/daemon-request/cli-surface.md)，不再作为活动候选。

`--all` 暂不加入该表：现有 workflow list 中它指 all callers，不能在 move 内含糊地改为“含终态”。
`--follow` 已是现有持续观察用词，但是否增加及输出格式需要独立声明；普通查询不自动变成流。

## 5. 发现、输出与最小 IPC

建立意向、通知对方与传送 bundle 分开授权。单据先在发起方获准的既有持久边界落地，
经既有消息/获准元数据通道通知有权操作者；发送申请不能提前取得目标 bundle space 写权限。
同 org 不表示全员能读。list/status 读取授权可见记录；消息丢失由既有 outbox/收据恢复，
不新增 inbox，通知被接收不等于同意。目标 agent 完成自报是另一项有作者的事实。

观测结果包含固定计划/摘要、双方同意与授权证据、执行绑定及各权威来源/新鲜度；离线旧 ready 不能作为执行许可。
状态权威/journal 与 OrgFS 同步视图的关系由 B 明确，CLI/GUI 不直接读业务数据库。

### 5.1 最小 IPC 对照（方法名/schema 待冻结）

下表同步 R4 §10.2 的八个方法候选。用户面已选择，不代表这些具体方法名、字段或错误码已批准。
A 提供声明/校验/导出框架与共享接线，B 提供每个 move MethodSpec、真实 handler 和业务契约。

| CLI 动作 | 最小 IPC 候选 | 输入与权威边界 | 结果必须区分 |
| --- | --- | --- | --- |
| check | `move.check` | 明确 agent/目标，核验可信主体、host/ACL/runtime；不建单、不保留执行权 | 检查通过、未授权、不可达、不支持或缺条件；检查不是开始许可 |
| request/invite 创建 | `move.intent.create` | 创建角色分支互斥，完整许可条件与固定幂等键；从可信上下文核验角色，首次固定摘要/绝对期限 | 固定 moveId/planDigest、本方意向、通知投递状态；同键异内容冲突 |
| request/invite 同意 | `move.intent.confirm` | moveId 与本方动作；查固定摘要并绑定实际作者/实例，拒绝替换字段 | 本方收据、对方有效意向及授权情况；通知 ACK 不算同意 |
| list/status | `move.list` / `move.status` | 仅授权可见记录；status 指定 moveId，不接受任意 SQL/私表路径 | 固定计划、同意、执行与恢复证据、来源/新鲜度、下一步；不泄露无权记录 |
| start | `move.start` | moveId；源权威复核有效同意/授权，串行取得同源实例唯一执行资格 | 原 operation 或合法拒绝/阻塞；受理不等于迁移完成 |
| cancel | `move.cancel` | moveId、可选原因；主体与真实执行阶段决定允许操作 | withdrawn/rejected、cancel-requested、cancelled、越过交接边界或原终态 |
| retry | `move.retry` | moveId；查原 operation、既定继续/取消目标与步骤收据，复核授权 | 续原步骤、补偿、blocked 或原终态；不创建执行、不清 fence、不复活 |

创建与同意分离，不用无约束可选字段组合。创建幂等键、同键异内容冲突和原结果查询由 B 持久处理；
首次受理前查幂等记录，响应丢失后重试不能重算 TTL 或产生新单。公共 client 不盲重试业务写操作。
默认都是单次 request 返回；持续观察只有另行声明后才提供，不假定需要 streaming transport。

普通 `--json` stdout 为一个带 `ok` 的对象，`ok:false` 退出 1；进度/警告/交互走 stderr。
`ok:true` 可表示查询或请求受理成功，不能冒充迁移 complete；实际业务状态独立、可机器判断。
cancel 请求被受理、尚在取消、已安全停止必须区分；retry 不能清 execution fence、解除 hold 或换 operation。
流式方法按声明单独定义 cursor、结束/取消和输出格式，不暗中改变 v1 单响应传输。

### 5.2 仍需共同冻结的契约

| 缺口 | 归属与关闭证据 |
| --- | --- |
| planDigest 与正式 schema | B 提供字段白名单、canonicalization/摘要算法、空值/顺序/schemaVersion 规则及跨语言语料；identity 核对授权字段，A 接入声明校验。进度/通知/自报不混入不可变摘要 |
| 创建与替代单 | A/B 固定 TTL 范围/默认、幂等键作用域/保存与重试方式、重复手工创建、`--replaces` 的合法旧终态/主体范围/错误；覆盖省略关联参数、双单 start 与旧 hold 负例 |
| 确认与 preview | 逐方法声明是否需要预览，以及预览输入、结果和 execute 字段；适用者只以绑定的 preview operationId 执行，后端解引用固定 moveId/摘要；不再追加可替换计划参数，也不额外制造双方授权 |
| 可信调用与能力探测 | identity/ACL/network 给出实例证明、角色/host 绑定及认证能力端口；A/B 固定 MethodSpec 上下文与拒绝结果。设备凭据不自动替代 agent 实例授权，方法名不代替端口交付 |
| 状态、错误与输出 | B 把 R4 §7.4 阶段表落实成参数/结果/错误 schema、权限和正反例；A 映射唯一输出层。保留受理/完成、离线/陈旧、终态/可恢复的机器可判区别 |
| 跨权威竞争与恢复 | B 协同 ACL/identity/runtime 给 start/cancel/grant/revoke 的排序、围栏、迟到收据与死目标停止证明；包括孤儿 grant、执行超时和非 move 操作争用。不可变计划没有自动解决这些问题 |
| 旧入口与实际消费者 | B 补 R4 §10.5 的 18 个旧协议入口逐文件 argv/hooks/skills/tests/生成清单和替代方法；A 统一共享注册。host-invite、离线 downgrade-state 和 smolvm probe 独立判归属，不按名字误删 |

R4 的 G2/G3/G4、O5–O10、M1–M8 等开发前置及独立复审仍保留；本表不是全部门已关闭的声明。
其中 B 的业务/运行门不阻止 A 在用户批准范围内先交公共基础；A 不能用占位 handler 宣称 B 已可用。

## 6. CI 在 A 先落，C 最终收口

| A 的早期检查 | 必须证明什么 |
| --- | --- |
| 命令树/帮助/参数 | 当前可用命令与声明一致，动作和位置参数不冲突；未实现能力不伪装可用 |
| 契约与 OpenAPI | 参数/结果/错误/逻辑流式完整，离线导出可重复，新方法不得漏登记，内部门面不因导出自动暴露 HTTP |
| 样板运行 | 真实隔离 daemon/client 调用；字段、错误、超时行为和 CLI 字节证据 |
| import 棘轮 | 精确旧引用只减不增，新增/迁移命令零违规，backend 不反向依赖 CLI |
| 门禁负控 | 注入违规 import、未登记方法、错误 schema 或输出差异时能明确失败 |

棘轮记录可审查的精确引用关系，覆盖别名、相对/函数内 import 及可识别动态调用。
移动文件、重导出、复制 helper、TYPE_CHECKING 或动态调用不能掩盖新增业务依赖。
原始基线与后续减少均保留来源；不能添加目录级永久豁免、任意扩容账本或放宽 schema 消除红灯。
它是迁移期间的收敛门，不是向后兼容实现；C 最终清零，白名单仅限逐项批准的 bootstrap 能力。

GUI 可先消费 A 的契约与真实调用检查发现接口问题；本批不实现 GUI 页面或 HTTP 网关，
契约/unit 检查不能写成完整 GUI 功能验收。缺真实环境仍按 UNKNOWN/BLOCKED 报告。

## 7. A 的具体任务与每 PR 不变要求

用户要求本轮给出任务规划与 Kanban 卡数。建议 **5 张 A 实施卡、5 个实现 PR**，均进入拟建的
`intg/cli-command-surface`；以下是待确认计划，尚未创建卡或派工。
#2010 是共同设计前置，不再重复计为实施卡；intg → dev 的汇总 PR 是交付容器，不算第六张实施卡。
B 的 move 业务和 C 的逐组全量迁移另行建卡，不塞进这五张。

| 卡 / 单一 PR | 实际范围 | 前置 | 可判定验收 |
| --- | --- | --- | --- |
| A-01 公共 request client | 接回 #1967 到批准的 A 基线；kernel 公共面承接传输；同步受影响消费者并去掉旧 client shim | 用户批准 A 设计、核对新基线 | 隔离 socket/daemon 下帧、超时、断线、错误契约通过；生产调用方不再用旧 client；无静默写重试；新 head 独立复验 |
| A-02 CLI 公共接线 | 收敛 entry 注册、CliServices/DI 与 request→CliResult/CliStream→唯一 output 通路；提供 B/C 共同接入点。只处理公共框架及受影响消费者，不逐组改业务命令 | A-01 | 既有可用命令注册/帮助/输出不漂移；受影响测试和安装调用方同步改用明确入口，不加测试重导出；后端不反向依赖 CLI |
| A-03 声明/OpenAPI/棘轮与 link 样板 | 一个骨架 PR 交付 MethodSpec、真实绑定、确定性 OpenAPI、import 棘轮及负控；同一 link 组迁移，list/stats 为两个主样板，add/drop 的必要组内依赖一起收齐 | A-02；用户批准的 A 设计包含方法声明 | 参数/结果/错误/流式及确认元数据可导出；真实隔离 daemon 调用；文本/JSON stdout、stderr、退出码逐字节一致；非法 import/未登记方法/错误 schema 必须报错 |
| A-04 命令清单与公共 CI | 在现有 help inventory/输出/边界门上统一可用命令目录、参数互斥与注册核对；覆盖 25 源码族、host 目标术语映射并更新实际基线计数；接契约/OpenAPI 检查供 GUI 自查 | A-02；与 A-03 对齐导出格式，最终依赖 A-03 | 实际注册与清单一致，已退役入口无 alias，未实现 move 不进入可用目录；help/生成物由脚本生成，负控能阻止漂移；不将契约检查当 GUI 功能验收 |
| A-05 安装产物与基线交付 | 补安装后 console script、公共 client/声明包资源与隔离 daemon 的验收路径，固定 B/C 可消费的接口快照和接入说明；在确切集成 head 收口 | A-03、A-04 | 从安装产物运行样板和导出；适用全量、Ruff、结构/边界、contract 隔离语料通过；交付 SHA/产物/实测证据，UNKNOWN/BLOCKED 不记完成 |

A-01 → A-02 → A-03 为核心顺序；A-04 在 A-02 后可并行准备清单/测试，最终接入 A-03 的真实导出。
两者涉及同一 CI/共享入口时由本线串行合入。A-05 验收后形成供 B/C 消费的公共基线，
无需先完成 B handler 或 C 的 0.5.6 全量迁移；B/C 各自获批后可据此并行开发。

实际 move 注册与 handler 同批交付在 **B 的 move 命令组 PR**：A 负责该 PR 的共享接线改动/核对，
B 负责薄 CLI、MethodSpec、领域实现与消费者；不另建一个没有真实 handler 的 A 占位 PR。
因此五张 A 卡不依赖 B 的产品完工，也不会产生 A 等 B、B 又等 A 的循环。
若审阅发现额外业务命令组必须改语义，应明确归 B/C 或修订计划，不暗中扩充公共框架卡。

当前不冻结目录；A 沿现有布局工作，共享入口按上述 owner 协调。整体目录搬迁及短冻结留 C 最后安排。

设计 PR 留档并取得用户对 A 的明确同意后，形成具体任务、依赖、工作项与验收，再派 PAC。
规划可先提出候选，只有批准范围内的任务进入实施；共享文件交叉大的任务应顺序接线，独立领域再并行。
每个任务均在 `group-internal` 登记，关联设计版本/PR、前置、owner 和可判定验收。

- 每个命令组一个 PR；框架骨架明确列出样板所属组及范围。
- PR 写动机、改动、验证命令和实测结果；未测项明确 UNKNOWN/BLOCKED。
- 字段/错误/事件等契约变化同步 `contract/`，Skill-Impact 按实际变更声明并更新相关技能。
- actor-rustify 只改 daemon 内部；共同 contract 快照是方法面边界，不另建业务语义。
- 实现 PR 进入所属 intg；及时同步共同基线和 dev 成果；最终以确切集成 head/产物验收，不自审自并。
- C 中 org 等后置组及目录最后冻结的约束保留；B 已迁完的 move 不在 C 重写。

当前未执行 #1967 rebase、未建立 A 实现分支/工作项、未提交产品代码、未派 PAC。
