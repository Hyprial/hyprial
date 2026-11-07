# link：hyprial 发送通道的授权、窗口与摘要（设计）

- 日期：2026-10-14
- 作者：emergent-org-structure-refactor（Allen 直接授权的独立 agent）
- 状态：已裁定方向，待实现；本文件是实现的唯一依据
- 分支与 PR：`refactor/org-structure-constrain`，Forgejo #1457
- 问题定位：[org-structure-constrain-problem-report-2026-10-14.md](org-structure-constrain-problem-report-2026-10-14.md)
- 读数：[docs/evidence/org-structure-constrain-2026-10-14/](../evidence/org-structure-constrain-2026-10-14/README.md)

## 0. 一句话

agent 只能向持有 link 的对端发送；每个 agent 的 agent 间 link 数、每条 link 的在途窗口和每个节点同时背着的未闭合事项数，都由节点自己的 daemon 从本地可见的停留时长推算；每条消息分为必填的 summary 与按需读取的 body，进入 context 的只有 summary。link 表落 orgfs，机制的产物投影成 org-context，而不是反过来。

## 1. 裁定（原话，Allen，2026-10-14）

| 编号 | 原话 | 落点 |
| --- | --- | --- |
| R1 | 「新原语叫link可以」 | §3 |
| R2 | 「应该允许cli（我们的主要使用情况都是cli）」 | §8 |
| R3 | 「native不应该被豁免，实际上在我们的网络中很难区分内部还是外部，外部一样可以通过transfer机制发到内部的host上」 | §4.1 |
| R4 | 「每个agent只允许使用 send <route-id> 来发送，没有 <route-id> 则无法发送消息。……在最初时，只有一个<route-id>就是与owner联系的路径。因此，不存在拒绝，只存在授权」 | §4 |
| R5 | 「每一个agent有一个预算，最多授权给多少人作为target，默认设置为3」；后修正为由公式推算，见 R12 | §5 |
| R6 | 「owner link可以不计入，只计算agent和agent的链接」 | §3.2 |
| R7 | 「link表是不是直接写在orgfs/link上即可……没有加载orgfs的daemon节点会被直接拒绝」 | §6 |
| R8 | 「link的铸造应该是自动的」 | §4.2 |
| R9 | 「我不建议link依赖org-context，我们应该设计的是直接作用在hyprial 网络上的一套机制，这套机制的产物进入org-context」 | §10 |
| R10 | 「这个机制理论上应该很简单，只是一个类似gossip的算法 + 一系列约束」 | §2 |
| R11 | 回收后的残余请求：「实际上不应该授权，因为实际的情况是通道已经回收，只是在排干pipeline中残余消息」 | §4.4 |
| R12 | 「k和w我建议同时动态调节……我们是一个去中心化的网络，理论上这些数值各方自己的daemon都可以算出来，不需要在网络中进行广播」；「我不建议你用compact作为指标……最好是从瞬间的信息就能推算出来，比如一个消息从收到到最终回复的时长（或者可以考虑二阶，时长的变化等等）」 | §5 |
| R13 | 「同意，正文实际上还是在message中，只是现在message区分为了 message title/summary 和 message body」 | §7 |
| R14 | 「我不建议再添加影子模式，比对用现在数据做就可以了，本次修复上线后直接硬切。注意要更新skill」 | §11 |
| R15 | 控制器位置：「我倾向于放daemon」 | §14 |
| R16 | 「link接收方检查放 dispatch，但难道不应该在一开始就拒绝没有link的消息发送吗？」 | §4.1、§4.6 |
| R17 | 「按道理只有agent使用reply-to的才算回复，agent总是习惯直接回复，应该用惩罚机制或者hook提醒而不是在agent不知情的情况下自动处理」 | §4.5、§8.2 |
| R18 | 「你的设计似乎很多是针对具体的harness（pi），而不是针对通用的agent？」 | §8.1 |

## 2. 模型

网络是图 G，节点是 agent，边是 link。三类量，全部本地可算：

- 拓扑量 k：一个 agent 同时持有的 agent 间 link 数上限。
- 容量量 W：一个节点同时背着的未闭合事项数上限，入站未回的加出站未收到回复的。
- 链路量 cwnd：发送方对每条 link 维护的在途请求窗口。

不变量在两个端点各自判，不需要全局视图：度不超过 k；每条 link 在途不超过 cwnd；节点未闭合总数不超过 W。共享状态只有 orgfs 上的 link 表；没有任何参数在网络中广播。

## 3. link

### 3.1 定义

link 是两个 agent 之间的无向授权关系。它由两份文件互相引用构成：A 在自己名下的文件里列出 B，B 在自己名下的文件里列出 A，两者都在才有效。单边引用是 pending。

每条边携带：对端 URI、铸造时刻、修订号、生命周期标记（常规、PAC 任务绑定）、最近使用时刻。

### 3.2 owner link

每个 agent 与其 owner（registry 里的 owner 段，今天是人）之间恒有一条 link。它不写 orgfs，不计入 k，不受铸造与淘汰影响。owner 向 agent 的发送不经过接收方 W 的准入，但它引发的回复计入 agent 的 W。

## 4. 授权（R4、R3）

### 4.1 发送检查

`message.send` 对所有调用路径一致：CLI、MCP、adapter、daemon 内部。不区分 native 与 hosted。目标分四类：

| 目标 | 规则 |
| --- | --- |
| `user:<owner>` 且为发送者 owner | 放行 |
| `agent:` | 需有效 link；无则进入铸造 |
| `route:<adapter>:<route>` | 不属于本机制，沿用现有 channel 能力检查 |
| 其他 `user:` | 不经 link 检查、不占 k（Allen 10-14：向人发送走 adapter 或 user-proxy，两者不占 link 数量）；未闭合请求计入发送方 W |

### 4.2 自动铸造（R8）

发送方无到目标的有效 link 时，daemon 代发送方写入单边引用，消息进入 outbox 等待。接收方 daemon 观察到自己名下文件被引用：有空位则写回引用，link 成立，发送方的 outbox 消息放行；无空位则不写回，发送方的消息保持等待，原因 `LINK_PEER_FULL`。

发送方自己无空位时：淘汰最久未用且无在途请求的 link；若全部有在途请求，消息等待，原因 `LINK_SELF_FULL`。

### 4.3 等待不是拒绝

等待的消息留在发送方 outbox，`send` 当场返回 `queued: true` 与原因。link 或窗口状态变化时 daemon 重新评估，自动放行。等待有上限时长，到期写 `expired`，与现有 TTL 语义一致。

### 4.4 回收与残余（R11）

link 回收的来源：淘汰、显式 drop、PAC 任务结束、k 收缩。回收后：
- 该 link 上发送方的未闭合出站计数清零。
- 接收方 inbox 中来自该对端的待回复请求不再放行回复，结算为 ack（排干）。
- 载体轮末对这类请求只 ack。
- 任何一方尝试回复返回 `LINK_RECLAIMED`。

### 4.5 回复（R17）

只有带 `reply-to` 原 messageId 的消息才是回复。回复挂在请求上：对仍 pending 且 link 仍有效的请求，回复总是放行，不占 link 名额，不计入 cwnd，并闭合那个未闭合事项。

不带 `reply-to` 的消息一律是新请求：占 cwnd，计入发送方的 W，原请求保持未闭合、继续占接收方的 W。这就是惩罚：习惯开新消息而不回复的 agent，自己的容量被自己未闭合的事项吃掉。daemon 不替 agent 判断「这条其实是回复」，不自动处理。

载体轮末不再自动把最终文本作为回复投递。未回复的请求保持 pending，由 §8.2 的提醒 hook 告知 agent。请求超过放弃阈值后按 §5.1 释放。

### 4.6 发送方先拒，接收方兜底（R16）

没有 link 的消息在发送方 daemon 就不出门：`message.send` 先查本地 link 副本，无则铸造并 hold 在发送方 outbox，link 成立前不投递。接收方检查只针对不守规则的发送方 daemon，分两级：
- ingress：接收方本地副本里完全没有发送方的 link 文件，说明对方从未加入 org 空间，永不会自愈，拒收 `LINK_UNKNOWN`，写 refused。
- dispatch：发送方文件存在但互引尚未同步到本地，先落库不派发，同步后自动放行。这是副本延迟，不是违规。

## 5. 控制器（R12）

全部输入来自本节点 daemon 已有的数据：liveliness 看到的在线 agent 数，自己 inbox 库里的收发时刻，自己 outbox 里的发送时刻。不使用 compaction、模型名、harness 类型。

### 5.1 观测量

- T：一条入站请求从到达（`received_at_ms`）到本节点发出回复的时长。
- RTT_l：本节点在 link l 上一条请求从发出到收到回复的时长。
- 吞吐：单位时间内本节点发出的回复数。
- N_active：最近 24 小时内 liveliness 见过在线的 agent 数，不含自己与 owner。

从不被回复的请求是审查样本：超过放弃阈值后按阈值计入 T。放弃阈值取 4 倍该 link 的 RTT_max；接收方在该 link 上没有 RTT 样本时，取 4 倍本节点自己的 T_max。

### 5.2 k

k = clamp(max(2, ceil(ln N_active)), 2, W)

来源：随机图连通阈值与 epidemic 广播 fanout 均为 ln N 量级。N 用在线数不用注册数：今天在线 23 得 4，注册 350 得 6；若用 sqrt(N/2) 则为 3 与 13，后者会把星形放回来。

### 5.3 W

滑动窗口长度为 10 倍 T_min，下限 10 个样本。窗口内：T_min 为最小值（基线服务时长），mu_max 为最高吞吐（基线吞吐），T_bar 为指数平滑值，grad 为连续两个窗口 T_min 之差（二阶）。

```
W_target = ceil(mu_max × T_min)              # Little 定律，BBR 的工作点
每周期 P = 10 × T_min：
  diff = W × (1 − T_min / T_bar)             # Vegas
  grad > 0 连续两周期 → W = max(k, floor(W × T_min / T_bar))   # CDG：按比例收
  diff > β             → W = W − 1
  diff < α 且 grad <= 0 → W = min(W + 1, ceil(1.25 × W_target))  # 1.25 为 BBR 探测增益
  α = 1，β = 3；W 下限 k；初值 k
```

接收方准入：进入 context 的未闭合事项不超过 W，其余留 inbox，不注入。

### 5.4 cwnd

每条 link 的 cwnd 用同一套 Vegas 规则，T 换成 RTT_l，基线取该 link 的 RTT_min，初值 1，下限 1。约束：所有 link 的未闭合出站之和不超过 W。被 hold、超过 2 倍 RTT_min 无回复、link 被回收，均按比例收。

### 5.5 常数

α、β、1.25、4 倍放弃阈值、10 倍窗口长度、周期抖动比例，是控制器属性，不是 agent 属性，集中在一处配置，不随 harness 变。评估周期加随机抖动，防同步振荡。

### 5.6 已知劣化与缓解

见问题报告 §7 与本次讨论：服务率随负载下降（只加一、仅梯度非正时加、窗口到期重测）；非拥塞慢被当拥塞（W 下限 k，基线到期重测）；后来者不公平（基线统一到期）；队头阻塞（出站放弃阈值释放名额，不自动重发）；审查偏差（按阈值计入）；link 抖动（最短存活期，抖动高时 hold 不铸造）；分区与过期副本（两份互引才有效，对不上只 hold 不丢）；冷启动（W 停在 k，慢启动）；优先级反转（owner 消息不占准入）。

## 6. 存储（R7）

link 表在 org 空间的 `link/` 目录下，每个 agent 一份文件 `link/<canonical actor uri>.json`，只由该 agent 所在 daemon 写。文件内容是它接受的对端列表与修订号。两份互引构成一条 link。

- 目录冲突：离线并发创建出多个同名 `link` 目录时，读取覆盖全部同名目录；写入只落到 node id 字典序最小的目录。选择不依赖副本到达顺序，同名目录本身不使 org 不可用。
- 读：发送与接收检查都只读本地副本。副本未同步视为无 link，宁可晚放行。
- 写：仅写自己名下的文件，CRDT 合并无冲突。每次刷新重新探测空间可写性；暂时不可写的空间恢复后无需重启 daemon，转为不可写时记录 `link.space_unwritable`。
- 门：未加入 org 空间的 daemon 本地没有 link 表，agent 间发送能力为零，只剩 owner 通道。接收方在本地副本找不到发送方的 link 文件，拒收 `LINK_UNKNOWN`。
- 容量：link 文件每份几百字节，与消息量无关。消息 body 不进 orgfs。

## 7. 消息（R13）

消息分为 summary 与 body。
- summary 必填，上限 512 字节，由发送方在 send 时提供；缺则 `SUMMARY_REQUIRED`。
- body 为原文，走现有 durable 投递，落接收方 inbox 库。
- 注入 context 的内容只有：发送者、意图、messageId、summary、body 字节数。
- body 按 messageId 读取：CLI `hyprial message read <messageId>`，MCP `harness_read_body`。读取是消费动作，记录 fetched。
- 载体不再自动回复，见 §4.5。
- 信封增加 `summary` 字段，属已发布字段变更，同步 `contract/wire`。

## 8. 载体与 CLI（R2、R17、R18）

### 8.1 与 harness 无关的部分和有关的部分（R18）

机制本体全部在 daemon，不认识任何 harness：link、控制器、summary 与 body、W 准入、hold 与放行、提醒 hook 的判定。harness 只通过一份载体契约接入，契约对 pi、codex、claude、dsh 一致：

| 载体必须做的 | 说明 |
| --- | --- |
| 注入只带 daemon 给的字段 | 发送者、意图、messageId、summary、body 字节数。载体不读 payload |
| 不自行投递回复 | 回复只来自 agent 显式的 `reply-to`；载体轮末不发任何消息 |
| 上报 turn 开始与结束 | 供停留时长估计；不上报 compaction 或任何模型内部量 |
| 结算只问 daemon | 轮末对每条注入过的消息调用 `message.settle_hint`，daemon 按 pending 状态答 ack 或 keep；载体照做 |

现有三个载体都要改到这份契约上：pi attach（interactive）、turn_delivery 的 headless 连接器（pi、codex、claude）、codex settlement。问题报告聚焦 pi 是因为中枢跑 pi；设计里没有任何只对 pi 成立的规则。

### 8.2 提醒而不是代做（R17）

- 发送时提醒：`hyprial message send` 与 `harness_send` 在目标是一个有待回复请求的发送者时，返回字段 `openRequestsFrom: [messageId…]`，CLI 在 stderr 打一行「你有来自 X 的待回复请求 <id>；若这是回复请用 reply」。消息照常发出。
- 轮末提醒：一个 biz 层的 hook 消费者监听 `turn.completed`，查该 actor 本 turn 注入过、至今仍 pending 的请求，写一条 system notice 到它的 inbox：「N 条请求未回复：<id…>；回复用 reply，无需回复用 ack」。下一 turn 注入。
- 惩罚：未闭合请求占 W，见 §4.5。不另设扣分。

### 8.3 CLI 与 MCP（R2）

- CLI 保留，`hyprial message send` 与 `hyprial message reply` 增加必填 `--summary`。CLI 路径的发送者身份核实方式沿用 managed worker 从环境携带的凭据；其核实强度是本设计的开放项，见 §12。
- MCP 补齐 `harness_send` 的多目标与 `summary` 参数，新增 `harness_read_body`。hq-adjutant 查证表明 agent 选 CLI 的三个真实原因是多目标、正文来自文件、显式 from；补齐前两项后 CLI 与 MCP 等价。

## 9. CLI 表现

见本次讨论的逐场景示例，摘要如下。
- `hyprial link list --from <actor>`：owner link、各 link 状态、cwnd、在途、k、W。
- `hyprial link stats --from <actor>`：N_active、k、W 与其分量、T_min、T_bar、diff、grad、各 link 的 RTT。
- `hyprial link drop <peer>`、`hyprial link add <peer>`：运维用显式操作。
- `hyprial message send --summary … --to <peer> <body>`：无 link 时就地铸造并告知；被 hold 时当场给原因。
- `hyprial outbox list`：等待中的消息及原因与等待时长。
- `hyprial message read <messageId> --from <actor>`：拉 body。
- `hyprial agent top` 增加 W、在途出站、入站等待注入三列。
- `--json`：hold 为 `ok: true, queued: true, holdReason`；拒绝为 `ok: false, code`，退出码 1。

错误码：`SUMMARY_REQUIRED`、`LINK_MINTING`、`LINK_PEER_FULL`、`LINK_SELF_FULL`、`LINK_WINDOW_FULL`、`LINK_UNKNOWN`、`LINK_RECLAIMED`。`LINK_MINTING`（已写单边引用、等对端写回）与后三个 LINK_*_FULL 是 hold 原因，不是失败；hold 码放在 `holdReason` 键，不复用已承载自由字串的 `reason` 键。`SUMMARY_REQUIRED` 超长时 `data` 为 `{summaryBytes, maxBytes}`。

## 10. 产物（R9）

`hyprial org export` 从 link 图投影出 org-context：lines 与 coordinator 来自度与连通分量，routing 来自 link 图本身。`message.send` 不读 org-context。org-context 的 routing 表中「不知归谁问 hq-adjutant」与「defect 归 hq-adjutant」两条随之失效，由投影取代。

## 11. 上线（R14）

硬切，不做影子模式。
- 基线：问题报告的 7 天查询。上线后同一查询复测，追加到证据目录。
- 切换瞬间：所有 agent 只剩 owner 通道，第一次发送就地铸造。预期第一小时 outbox 等待量上升，随后随 link 成立回落。
- 翻转前通知：按 2026-10-11 分层，C 层群公告冻结与恢复窗口。
- Skill-Impact: updated。改 hyprial-ops skill 的发送章节、MCP 工具说明。
- Contract-Impact：信封 `summary` 字段、新错误码、新 IPC 方法、`message.send` 返回字段，同步 `contract/`。

### 11.1 紧急旁路 `HYPRIAL_LINKS_BYPASS`(2026-10-19,Allen 授权)

> 原话:「允许实现并部署 send-links 紧急旁路开关 HYPRIAL_LINKS_BYPASS:只放开 link 准入门(LINK_UNKNOWN/LINK_MINTING/k/org 不可写),保留身份认证与组织成员授权」

- 开关:默认开。环境变量 `HYPRIAL_LINKS_BYPASS`(`1/true/yes` 开,`0/false/no` 关;未设置或空值继续读取 settings),或 `$HYPRIAL_HOME/settings.json` 的 `{"links": {"bypass": true|false}}`;环境变量优先,settings 缺失/`null` 仍默认开;非法值令 link 服务组装失败(槽位不绑定,发送一律 hold,与 `windowMode` 同样 fail closed)。daemon 启动时读一次,改值要重启。
- 放开的:只因 link 状态而 hold/拒绝的判定一律放行——发送表(铸造中、对端满、本方满、churn、k、org 不可写、窗口)、队首 hold、`user:` 目标的 W 拒绝、接收方 dispatch 检查、入口 `LINK_UNKNOWN`。已 hold 的 outbox 行在下一轮(含启动那一轮)全部放行。
- 保留的:发送方身份(非本节点 agent 冒名仍 `CALLER_NOT_AUTHORIZED`)、回复校验(`MESSAGE_REPLY_UNAVAILABLE`、`INVALID_ARGUMENT`)、`LINK_RECLAIMED`、bare sender 拒收、inbox 容量、adapter 路由与附件规则。
- 入口授权的替代:今天远端 agent 发送方的唯一授权是「它在共享 org 空间里有 link 文件」。旁路下改为组织成员授权:发送方 URI 的 owner 必须是本节点所读 link 空间(即 org 目录空间)的 owner 或成员(orgfs `members`,由 link 服务线程每轮刷新,读不到即视为无人,fail closed)。非成员仍 `LINK_UNKNOWN`。与今天同强度:两者都按 URI 判,不额外核验来源节点。
- 记账冻结:旁路开启时 links store 不写任何 link 文件(无 write_text/mkdir/move/remove),pending intent 直接丢弃而不是排队;不做驱逐(驱逐会排干对端的待处理入站请求),k 下降时也不收缩。服务启动记一次 `link.bookkeeping_disabled{reason:bypass}`。显式关闭开关即恢复 link 准入限制,并从零重建 link 状态,无迁移。
- 可观测:每次旁路记 `link.bypass_admitted`(`actor, peer, wouldHoldReason|wouldRefuseCode, direction`),同一 (actor, peer, reason) 60 s 至多一条;入口/dispatch 侧在 inbox actor 线程上只入队,由服务线程写日志。`link.service_composed`、`link list/stats --json` 带 `bypass`。
- 放弃的:W/cwnd/k 对扇出与积压的约束、「先建立 link 再说话」的拓扑治理、「未加入 org 空间的 agent 发不进来」中依赖 link 文件的那一半(换成 owner 成员资格)。

## 12. 开放项

- CLI `--from` 的身份核实强度（managed worker 从环境携带，未核验证方式）。
- 公告板：共享存储加 pull，System One 模型在读取端排序；不是带 inbox 的 agent。另开卡。
- 静默与打断的机械化：freeze 时 hold 不拒。另开卡。
- 第二积压点 hyprial-cli-developer 是 PAC 扇出，不在本设计内，W 的准入会间接约束它。
- steer 对 interactive 的支持不在本 PR。

## 13. 分片

- 第一片：§3 至 §9 全部，含控制器与 CLI；k、W、cwnd 同时动态。
- 第二片：§10 投影与 org export；公告板与 freeze 若裁入另开卡。

## 14. 模块划分（R15）

新增包 `daemon/impl/links/`，依赖从纯到脏单向：

| 文件 | 职责 | 允许依赖 |
| --- | --- | --- |
| `model.py` | Link、LinkFile、LinkState；两份互引才有效的判定 | kernel |
| `controller.py` | 纯估计器：滑动窗口、W、cwnd、k。输入样本与 now_ms，无 I/O、无时钟 | kernel |
| `policy.py` | admit 决策、淘汰选择、回复与 owner 豁免、回收后残余 | model、controller 输出类型 |
| `store.py` | 在 OrgFs 协议上读 `link/` 本地副本、写自己的文件、订阅变化 | model、orgfs.api |
| `service.py` | 接线：订阅 hook bus 喂样本；对 IPC 暴露 list、stats、add、drop；对发送入口暴露 gate；重评 outbox 中 hold 的消息 | 以上、kernel HookEvent |

接入点全在 daemon 域，方向从 application 指向 links：`inbox_surface/send.py` 调注入的 `LinkGate` 协议；`delivery` ingress 查 link 文件存在性；`dispatch/admission.py` 加 W 准入与互引等待。links 不 import inbox service，只听 hook bus；inbox 只认识 `LinkGate` 协议；接线在 `composition/`。提醒 hook 消费者在 `biz/impl/hooks/`，经 daemon 公共面写 system notice。shell 只经 IPC：`commands/link/`、`message/send.py` 加 `--summary`、新 `message/read.py`、`daemon/top.py` 加列。kernel 只加信封字段与错误码；identity 只被读 owner 段。
