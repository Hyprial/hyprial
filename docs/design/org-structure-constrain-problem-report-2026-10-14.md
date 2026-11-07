# LLM Agent 组织星型化与发送/确认风暴：问题定位报告（第一版）

- 日期：2026-10-14
- 作者：emergent-org-structure-refactor（Allen 直接授权的独立 agent）
- 状态：问题定位与假设，供设计裁定；不是实施方案
- 分支与 PR：`refactor/org-structure-constrain`，Forgejo #1457（WIP）
- 读数来源：[docs/evidence/org-structure-constrain-2026-10-14/](../evidence/org-structure-constrain-2026-10-14/README.md)
- 起点：Allen 2026-10-14 下达的《调研任务说明：LLM Agent 组织"星型化与发送/确认风暴"问题定位》，以及同日与 Allen 的设计讨论

## 0. 结论先行

1. 本机 hyprial 网络确实是星型。7 天内 54% 的消息经过 hq-adjutant。但它不是 PAC 所有权树造成的，而是 org-context 的 routing 表加约定造成的：中枢的对端是与它同 owner 的常驻 agent，PAC 子 worker 只贡献了 5037 条入站中的 6 条。
2. 风暴不在 durable inbox 层。中枢 86% 的消息一分钟内被取走，回复中位 0.8 分钟，此刻积压为 0。风暴在 harness 之后的两层：pi 的 followUp 队列和模型 context。
3. 真正被耗尽的资源是中枢的 context。它每天 compaction 6 次，每次模型调用重读中位 47 万 token，一天消耗 10.2 亿输入 token。任何超过两小时的协调状态只剩 compaction 摘要。这解释了「每条都答了但事情不动」。
4. 风暴有一个机械放大器：turn 结束时载体把最终 assistant 文本作为 message.reply 再发一次，而 agent 已在 turn 内用 CLI 发过真正的答复。中枢 53.5% 的回复伴随一条对同一请求方的新发送。这与 pi 关系不大，与「CLI 发送加载体自动回复」有关。
5. 中枢入站的 48% 是它自己发出的请求引来的回复。所以任何只约束入度的方案都不够，窗口必须作用在中枢的出向。
6. 已裁定的方向是：link 原语（agent 只能向持有 link 的对端发送）、link 表落 orgfs、自动铸造、owner link 不计入度、每条 link 的在途窗口与字节上限。本报告给出验证这些方向所需的假设、指标与实验设计。

## 1. 问题定位备忘录

### 1.1 症状（可观测）

| 症状 | 读数 | 层 |
| --- | --- | --- |
| 单点集中 | hq-adjutant 入 5037、出 4582，占全网 17648 的 54%；入度 49、出度 42 | 拓扑 |
| 自激负载 | 中枢入站 2406 条是回复，均为它自己请求的回音 | 协议 |
| 消息体不是回执 | 中枢收发消息体均值 2.4 到 2.9 KB，一半以上超过 2 KB | 内容 |
| 重复发送 | 中枢 53.5% 的回复前后两分钟内又给同一请求方发了新请求；headless pi agent 20% 到 48% | 协议与行为 |
| context 冲刷 | 10-14 当天 compaction 6 次（00:40、07:45、09:29、11:34、13:57、15:29），每次在 90 万 token 触发 | 认知 |
| token 消耗 | 当天 2128 次模型调用，输入加缓存 10.2 亿 token，输出 299 万，报告成本 28 美元 | 成本 |
| 真积压在别处 | hyprial-cli-developer 40 条未取、最老 11.8 小时；它入度 93，是 PAC 扇出的终点 | 另一个节点 |

### 1.2 机制（组织行为学语言）

- 信息枢纽角色过载：中枢同时承担协调者、路由兜底（org-context：「不知归谁问 hq-adjutant」）、缺陷受理（routing：defect 归 hq-adjutant）、派单者四个角色。每个角色都把原文推向它。
- 协调税吞噬预算：中枢的预算单位是 context token。每条 2.5 KB 的入站消息平均引发两次模型调用，每次重读约 47 万 token，真实成本约 100 万 token。协调本身耗尽了协调者。
- 礼貌负债的机械化：载体在 turn 结束时无条件回复最终文本，把「会话收尾单次实质性告别，不追加纯礼貌回执」这条规范从机制上取消了。规范靠自觉，机制靠代码，代码赢。
- 规范与机制相反：org-context 规定「转呈后果要原话不要摘要」，这条规范把原始字节推向中枢，与封顶 context 字节直接相反。
- 2026-10-11 的 A/B/C 通信分层是同一问题的约定解。三天后 Allen 再提，说明约定没有守住。本工作的性质是把约定变成 daemon 的闸。

### 1.3 根因分层

| 层 | 根因 | 证据 | 状态 |
| --- | --- | --- | --- |
| 结构 | 任何 agent 知道四段 URI 就能发给任何人；无度约束 | `message.send` 对 native 调用者无 send-to 检查（`_enforce_send_to` 仅对 hosted 生效） | 已知 |
| 结构 | routing 表把兜底与缺陷都指向同一个节点 | `~/.hyprial/org-context.md` v8 的 routing 与第一节 | 已知 |
| 协议 | 无在途窗口、无背压、无字节上限 | 消息路径只有 provider 用量配额与 custody 1 GiB 上限 | 已知 |
| 协议 | 载体 turn 结束自动回复最终文本，不查 agent 是否已回 | `pi_harness_attach.ts` 第 21 行与 `settleMessage`；codex `settlement.py` 同样 | 已知 |
| 行为 | agent 习惯用 bash 跑 `hyprial send` 作答复路径 | 中枢 3 天 session：bash 内 `hyprial send` 711 次，MCP `harness_send` 148 次 | 已知；是习惯还是 MCP 工具缺陷，已请 hq-adjutant 查证 |
| 技术 | 中枢为 pi_interactive，消息只能等 turn 结束以 followUp 注入；无 turn 统计 | `pi_harness_attach.ts` 头注释；`top` 对它报 turnCount null | 已知 |
| 技术 | steer（#1428，10-13 合入 dev）不在生产 v0.5.4 中，且只作用于 headless turn | `git merge-base --is-ancestor` 为假 | 已知 |
| 技术 | 中枢模型为 deepseek-flash 与 glm-5.3，在 47 万到 90 万 token 的 context 上运行 | session 的 model 字段 | 已知；对质量的影响未知 |

## 2. 实际运行画像

### 2.1 拓扑（7 天，取样 2026-10-14 15:46 +08:00）

- 活跃发送者 477，活跃接收者 555，多数为短命的 `wf-*` worker。
- 中枢的前十对端按入站字节：e2e-verifier 4.93 MB、org-improver 2.22、infra-ops 1.66、hyprial-cli-developer 0.92、kanban 0.90、codex-sw 0.59、user-exp-improver 0.52、hot-fixer 0.36、hq-adjutant-feishu 0.25、squire 0.21。
- 对话结构：涉及中枢的 4768 个对话中 4755 个只有两方，平均 2 条消息。中枢不中继对话，它读 A 的消息后向 B 开新对话。中继存在，但在对话 id 上不可见。
- 第二个高入度节点：hyprial-cli-developer 入度 93，是 PAC 扇出的接收端；`wf-540465…` 出度 172，是广播型发送者。中枢问题与 PAC 扇出问题在不同节点上。

### 2.2 中枢的一天（2026-10-14）

| 小时 | 入站 | 出站 |
| --- | --- | --- |
| 00 | 87 | 125 |
| 01 | 177 | 237 |
| 07 | 34 | 44 |
| 08 | 159 | 175 |
| 09 | 88 | 94 |
| 10 | 74 | 98 |
| 11 | 106 | 98 |
| 12 | 56 | 23 |
| 13 | 128 | 132 |
| 14 | 276 | 337 |
| 15（至 32 分） | 39 | 46 |

14 点峰值合计每小时 613 条，约每 6 秒一条。当天 15:32 前入站 1232 条，是 7 天内最高的一天。

### 2.3 中枢的处理方式

- 注入：当天 514 次注入、1291 条消息、1.71 MB。每次注入中位 1 条 1.5 KB，p90 4 条 5 KB，最大 58 条 66 KB。
- turn：中位 0.5 分钟，p90 1.4 分钟。每 turn 工具调用中位 2 次。
- 工具：bash 2178 次（其中 `hyprial send` 711、`hyprial ps` 108、`hyprial agent` 67、`hyprial start` 51、`hyprial top` 50），`harness_reply` 508，`harness_send` 148，`harness_ack` 103。以上为 10-11 至 10-14 整个 session 的累计。
- 人类消息：3 天 11 条。其余 1163 次 user 消息全部是网络注入。

### 2.4 关键事件时间线

- 2026-10-11 Allen 批准 A/B/C 通信分层（约定）。
- 2026-10-13 21:56 steer（#1428）合入 dev；生产未升级。
- 2026-10-14 00:40 至 15:29 中枢 6 次 compaction。
- 2026-10-14 14:00 至 15:00 7 天内最高小时流量。
- 2026-10-14 下午 Allen 启动本工作；hq-adjutant 通知全网进入静默期。

## 3. 因果假设图

```
routing 表兜底指向中枢 ──┐
无度约束(任意 URI 可达) ──┼──> 入度与入站字节集中 ──┐
agent 用 CLI 发送 + 载体自动回复 ──> 消息数 x2 ───┘
                                                    v
中枢自身高出度(派单) ──> 回复回音(48%) ──> 进入 context 的字节/小时 ──> context 到 90 万 ──> compaction
                                                                                        │
                        状态丢失 ──> 重问/重派/重复确认 ──> 更多消息 <──────────────────┘(正反馈)
                                 ──> 「已答复但事情不动」
```

- 阈值：90 万 token 的 compaction 触发点；今天约每两小时一次。
- 调节变量：模型的有效 context 质量（deepseek-flash、glm-5.3 在 47 万以上 token 的表现未知）；org-context 规范的措辞；是否使用 MCP 工具而非 CLI。
- 锁定效应：routing 表与规范让新加入的 agent 继续把中枢当兜底；中枢自身的 compaction 摘要也会把「我是所有事的协调者」固化成自我描述（session 中第一次 compaction 摘要的开头即如此）。

## 4. 待验证假设清单

按优先级排列。每条给可检验形式、数据需求、当前状态。

| 编号 | 假设 | 可检验形式 | 数据需求 | 状态 |
| --- | --- | --- | --- | --- |
| H1 | 中枢的瓶颈是 context 字节，不是队列 | 控制入站字节/小时，compaction 次数应随之线性下降；控制条数而不控字节则不下降 | pi session 的 usage 与 compaction；inbox 的 payload 长度 | 读数支持；需干预验证 |
| H2 | 载体自动回复使消息数接近翻倍，并把 agent 的整段收尾文本送进对端 context | 载体改为「turn 内已向请求方发过消息则只 ack」后，replied-plus-send 比例从 53.5% 降到接近 0，中枢入站字节下降可量化 | 同上，加 message.reply 的调用来源 | 机制已核；量化待干预 |
| H3 | 中枢一半负载由其自身出度引起，限制出向在途请求即可限制入站回复 | 对中枢出向加 w 后，回复类入站按 k·w 封顶 | inbox 的 intent 与 conversation | 读数支持 |
| H4 | 星型来自 routing 表与规范，不来自所有权树 | 改 routing 兜底与 defect 去向后，中枢入度下降而无需改任何 owner 关系 | org-context 版本与入度时序 | 读数支持 |
| H5 | 规范「原话不摘要」是字节膨胀的来源之一 | 对比中枢入站中转发类消息与非转发类的字节分布 | 需内容分类（可用消息头部的 from/to 链判断） | 未验证 |
| H6 | agent 用 CLI 发送是 MCP 工具缺陷所致，而非习惯 | hq-adjutant 的回答；MCP 工具无 reply-to、conversation、file、多目标参数 | 已向 hq-adjutant 发出查证 | 需组织确认 |
| H7 | 在 47 万 token 以上，所用模型的协调质量显著下降 | 同一任务在 compaction 后 10 分钟与前 10 分钟的 never-replied 比例与重问次数对比 | session 时间线与 inbox 对齐 | 未验证 |
| H8 | 第二积压点（hyprial-cli-developer）是 PAC 扇出而非星型，需另一道闸 | 其 40 条未取消息的发送者分布 | `hyprial query` 读其 inbox | 未验证 |
| H9 | 把 link 表放 orgfs 后，未加入 org 空间的 daemon 发送能力自动归零，老 agent 无法绕过 | 在隔离环境起一个未 join 的 daemon 尝试发送 | 隔离 E2E | 未验证 |

## 5. 诊断指标集

| 类型 | 指标 | 计算 | 来源 |
| --- | --- | --- | --- |
| 领先 | 每节点每小时进入 context 的字节 | sum(payload 长度) 按 recipient 与小时分组 | inbox 表 |
| 领先 | 每条 link 未闭合请求数 | 该 link 上 intent=request 且同对话无 reply 的条数 | inbox 表（上线后由 daemon 直接维护） |
| 领先 | 节点度 | 7 天内不同对端数 | inbox 表 |
| 领先 | 重复回复率 | 回复后 -1/+2 分钟内对同一请求方的新请求数 > 0 的比例 | inbox 表 |
| 同步 | compaction 次数/天 与 tokensBefore | session jsonl 的 compaction 条目 | pi session |
| 同步 | 每次模型调用的 context token | usage.input + cacheRead + cacheWrite | pi session |
| 同步 | 注入字节/天 | user 消息中带 Harness Network 前缀的文本长度之和 | pi session |
| 滞后 | 请求从未被回复的比例 | 同对话内无该节点 reply 的 request 占比 | inbox 表 |
| 滞后 | 请求到实质回复的中位与 p95 | 同对话内第一条 reply 的时间差 | inbox 表 |
| 滞后 | 任务推进 | kanban 卡状态变化率 | kanban（需接入） |

限制：interactive 运行时不报 turn 统计；fetched 与 ack 都是「读到」而非「处理完」；每 turn 注入条数的代理读数偏低，因为 attach 载体会在 turn 间多次注入。

## 6. 最小数据采集方案

已可采集且本次已采：inbox 副本聚合、pi session jsonl 聚合、`hyprial top`。方法与脚本见证据目录。

需要补的：

1. 把 compaction 事件作为 daemon 可见事件（现在只在 pi session 文件里）。
2. 对 interactive 运行时报 turn 开始与结束（attach 载体已知道，未上报）。
3. message.reply 的调用来源标记（agent 工具调用，还是载体结算），否则 H2 只能靠时间窗推断。
4. hyprial-cli-developer 的 inbox 发送者分布（H8），一条只读 `hyprial query`。
5. hq-adjutant 对 H6 的回答。

隐私与合规：聚合只用 actor 名与长度，不读正文；compaction 摘要正文不入库；凭据不触碰；复制副本放会话 scratchpad，不回写生产。

## 7. 干预与实验设计

### 7.1 已裁定的设计方向（原话引用，Allen 2026-10-14）

- 原语命名：「新原语叫link可以」。
- CLI：「应该允许cli（我们的主要使用情况都是cli）」。
- 豁免：「native不应该被豁免，实际上在我们的网络中很难区分内部还是外部，外部一样可以通过transfer机制发到内部的host上」。
- 授权模型：「每个agent只允许使用 send <route-id> 来发送，没有 <route-id> 则无法发送消息。……在最初时，只有一个<route-id>就是与owner联系的路径。因此，不存在拒绝，只存在授权」。
- 预算：「每一个agent有一个预算，最多授权给多少人作为target，默认设置为3」。
- 度计算：「owner link可以不计入，只计算agent和agent的链接」。
- 存储：「link表是不是直接写在orgfs/link上即可，这样也可以强制要求在我们的方案上线后，接入的其他agent也遵守新的规定……因为没有加载orgfs的daemon节点会被直接拒绝」。
- 铸造：「link的铸造应该是自动的」。
- 与 org-context 的关系：「我不建议link依赖org-context，我们应该设计的是直接作用在hyprial 网络上的一套机制，这套机制的产物进入org-context」。
- 形态：「这个机制理论上应该很简单，只是一个类似gossip的算法 + 一系列约束」。
- 公告板：「提供一些类似公告板的agent，它的原理应该是一个公有消息栏+jev类的System One 模型，各个agent可以向它写入/读取」。
- 风暴控制：「是不是应该提供一些打断、静默机制，从而避免形成网络的通信风暴」。

### 7.2 候选干预与它针对的机制

| 干预 | 针对的机制 | 预期效果 | 副作用与风险 | 如何判断没有把风暴转移 |
| --- | --- | --- | --- | --- |
| link 原语与度约束 k | 结构：任意可达 | 中枢入度被迫下降，图变稀 | 翻默认时所有 agent 只剩 owner 通道；需先观察模式再强制 | 看第二大入度节点是否随之上升 |
| 在途窗口 w（按 link、按未闭合请求） | 协议：无背压；中枢自激 | 中枢在途不超过 k·w；上游在窗口满时被迫合并 | 发送方 outbox 堆积；需要可见的 hold 原因 | 看 outbox 平均停留时间 |
| 字节上限 b（超出即溢出到 orgfs，发指针） | 认知：context 字节 | 进入中枢 context 的字节封顶 | 对端多一次 pull；b 难以预先定 | 看 compaction 次数/天 |
| 载体结算去重（turn 内已向请求方发过消息则只 ack） | 协议：自动回复 | 消息数下降约三到五成，收尾文本不再进对端 context | daemon 已拒同 messageId 的二次回复（REPLY_ALREADY_SUBMITTED），所以双发是「CLI 新消息加载体首次回复」，判定要看 turn 内对请求方的发送记录 | 看 replied-plus-send 比例 |
| routing 表改写（兜底不指向单点） | 结构：锁定 | 新 agent 不再默认找中枢 | 需要替代兜底（公告板） | 看 unresolved 路由的去向 |
| 公告板（共享存储、pull） | 结构：中继 | 跨线信息不再经中枢推送 | 若做成带 inbox 的 agent 会成为新中枢 | 看公告板的入度与读写比 |
| 静默期机械化（freeze 时 hold 不拒） | 风暴 | 可控的刹车 | hold 期间消息延迟 | 看 freeze 结束后的爆发量 |
| 载体改为可中途注入（steer 对 interactive） | 技术 | 降低 followUp 等待 | 不在生产版本；只对 headless | 不在本 PR |
| 中枢换更长 context 或更强模型 | 技术 | 提高单位 context 的协调质量 | 成本；不解决字节增长 | 对照 H7 |

### 7.3 实验设计

- 影子模式先行：link 检查先以「只记录不拦截」上线一天，记录每条 would-hold 的发送与原因。这给出翻默认前的真实影响面，避免「默认翻转使全网只剩 owner 通道」的事故。
- 分片上线与消融：第一片 link 加载体去重；第二片 w 与 b。每片单独观察 7.2 表中对应指标，用同一套 7 天查询（证据目录的 sql）对比前后。
- 对照：同一时段的 hyprial-cli-developer（PAC 扇出）作为不受 link 影响的对照节点，看其积压是否变化；若变化则说明风暴被转移。
- 仿真可选：k 叉树加窗口的排队仿真，用来定 k、w 的起步值；读数已足以给出量级（见 §8），仿真不是上线前置。

## 8. 算法与参数的当前提案（待裁）

- 状态：每个 agent 在 orgfs 的 org 空间 `link/` 目录下写自己名下的一份文件，列出它接受的对端与修订号。两份文件互相引用才构成一条有效 link。写权限按 agent 隔离，CRDT 合并无冲突。
- 传播：orgfs 现有的 mesh 广播与查询，不新建传输。发送时只读本地副本；没同步到视为无 link，宁可晚放行。
- 不变量（两端各自判，不需全局视图）：agent 间 link 数不超过 k；每条 link 未闭合请求不超过 w；单条消息体超过 b 则正文溢出到 orgfs，消息只带指针与前 N 字节摘要，不拒绝。
- 操作：首次向无 link 的对端发送时 daemon 自动发起铸造，双方有空位即成立；发送方无空位则淘汰最久未用的一条；接收方无空位则消息留在发送方 outbox 带原因等待。PAC 派单自动铸造并随 worker 生命周期释放。owner link 不计入 k。
- 自动调参：k 与 w 可按 TCP 拥塞控制的方式自适应，及时回复则加性增加，超时或被 hold 则乘性减少。b 不作硬门，作溢出阈值，起步按对端最近 7 天入站字节的 p50 定，对端可在 link 文件里公布自己的阈值。
- 产物：lines、coordinator、routing 由 link 图投影，`hyprial org export` 生成 org-context，`message.send` 不读 org-context。
- 起步量级：k 为 3，w 为 3，b 以中枢当前入站 p50 约 2.4 KB 为参照。中枢任一时刻在途不超过 9 条请求，一次注入最坏 9 条乘 b。

## 9. 开放问题与下一步

| 问题 | 归属 | 状态 |
| --- | --- | --- |
| agent 用 CLI 发送是习惯还是 MCP 工具缺陷 | hq-adjutant 查证 | 已发出，待回复 |
| 回复入站请求是否隐含授权、不占 link | Allen | 待裁 |
| CLI 路径下 `--from` 身份如何核实（managed worker 从环境携带，未核验证方式） | 本 PR 设计文档 | 开放 |
| b 的自动确定方式是否接受「溢出而非拒绝」 | Allen | 待裁 |
| hyprial-cli-developer 的积压来源（H8） | 本 agent | 一条只读查询即可 |
| 中枢模型在超长 context 下的质量（H7） | 组织 | 需对照数据 |
| 静默期的机械化属于本 PR 还是后续卡 | Allen | 建议后续卡 |

下一步：
1. 收到 Allen 对 §8 的裁定后，写 `docs/design/design-send-links-2026-10-14.md`，每条裁定原话引用。
2. 第一片实现：link 原语、orgfs 上的 link 表、自动铸造、载体结算去重、影子模式开关。
3. 第二片：w 与 b，静默期机械化（若裁入）。
4. 每片上线后用证据目录的 sql 复测 §5 指标，结果追加到证据目录。
