# 概念稿:PAC = 图文件 + flag 反应器(2026-09-04)

> 本文保留为设计历史。本集成分支的当前实现契约见
> [PAC workflow unification](spec-pac-workflow-unification.md) 与
> [命令和节点观察](../reference/workflow-node-observation.md)。合入集成分支不代表生产已升级。


- 出处:Allen 2026-09-04 对两份 PAC 调研的纠正(「搞混了 Graph 的编写、触发、监控和具体 agent 执行情况的监控」「任务调度机制基本等于对一个图文件进行编辑」「PAC 是基础性机制,存自己的 SQLite;kanban 是具体的 socialware」「loop 在写图时就知道;不通过时重置 flag,不新建/复制节点」)。
- 性质:一页概念稿,定层次与形状;不是 spec。裁定项在 §7。
- 2026-09-14 边界更新：Allen 已裁 PAC owner 存完整 URI、意图不明先询问。下文 §2 的 owner 短名条款保留为历史，不再是新实施依据；落地草案见 [PAC owner URI 设计](design-pac-owner-full-uri.md)。仅替代 PAC owner 表示，不改变 actor_name 等本机名字规则；跨机授权等价仍待裁。

## 1. 三层,各归各

| 层 | 是什么 | 谁动它 | 归谁 |
| --- | --- | --- | --- |
| 图文件(编写) | 节点 = 环节,带 owner(人/agent)与一个 flag;边 = 「前置节点的 flag 置了,轮到后置节点的 owner」;回边 = loop,写图时就定 | 人和 agent 都能编辑(带版本) | PAC(存 PAC 自己的 SQLite) |
| 触发(运行) | 只看 flag 的反应器:某节点 flag 置了 ⇒ 沿出边给后置 owner 发「轮到你」;flag 被改回 ⇒ 给受影响的后置 owner 发「已撤回」;不读回复正文、不判内容 | PAC daemon 侧 | PAC |
| 监控 | 读图与 flag 事件的当前状态 | 任何订阅者 | kanban 等 socialware 做投影(H1 事件挂钩即订阅 flag 事件) |

「agent 执行情况的监控」(worker 活没活、turn 有没有完成)是 harness/daemon 的事,不在这三层里;PAC 不替它做。

## 2. 图文件(PAC 的 SQLite)

最小 schema(名字是草图):
- `graphs(graph_id, name, version, created_by, created_at)`——每次编辑 +1 版本(CAS:改图要带当前版本,冲突即拒绝)。
- `nodes(graph_id, node_id, owner, brief_ref, flag, flag_set_by, flag_set_at, flag_reason_ref)`——`owner` 是人或 agent 的短名(存名字不存派生 URI);`brief_ref` 指向节点的说明(在 inbox/仓/看板里,PAC 不存正文);`flag` 是布尔(置/未置),历史在事件表。
- `edges(graph_id, from_node, to_node, kind)`——`kind ∈ {forward, back}`;back 边就是 loop,在写图时声明(例:pm 确认 → 不通过 → 回到 pm-helper 设计稿)。
- `flag_events(event_id, graph_id, version, node_id, action ∈ {set, reset}, actor, at, reason_ref)`——只追加,带出处;这是唯一的「事实」表,投影(看板)与通知都从它来。
- `notifications(event_id, edge, recipient, message_id, at)`——反应器发过什么,按 (event_id, edge) 幂等。

不存:prompt/回复正文、agent 的中间产物、harness 状态。

## 3. 反应器语义

1. `set(node)`:对每条 `from_node = node` 的出边,若其 `to_node` 的全部前置(所有指向它的 forward 边的 from 节点)flag 都已置 ⇒ 给 `to_node.owner` 发一条「轮到你」(内容 = to_node 的 brief_ref + 触发它的 event_id),按 (event_id, edge) 幂等;发送经 zenoh/inbox,PAC 只记 notifications 行。
2. `reset(node)`(不通过/撤回):记事件;对已因它触发过的后置节点,发「已撤回」通知;后置节点的 flag 不自动改——由其 owner 决定(人或 agent)。⇒ 不需要 exactly-once,也没有「补偿」机制:撤回就是再改一次 flag。
3. loop:`back` 边被触发(例如 pm 置「不通过」这个 flag——不通过本身可以是一个节点)⇒ 给回边目标 owner 发「轮到你(第 n 次)」;不新建节点,同一节点重新置 flag,第 n 次由 flag_events 数出来。
4. 编辑图(加/删节点或边)= 新版本;反应器只对当前版本算出边;进行中的通知带版本号,收件方可据此判断是否过期。
5. 确定性只在这一层:通知是 flag 事件的纯函数;flag 是可核的耐久事实(人点了确认、agent 置了、或 external.expect 那类「PR 在 origin」由一个核验 agent 置)。agent 干的活本身不确定,不在 PAC 里。

## 4. 与 PAC v1 的对应

| v1 | 新形状 |
| --- | --- |
| workflow.yaml 的 targets + task 正文 | 图文件的节点(owner)+ brief_ref;正文不在 PAC |
| run / target 状态机(dispatched/awaiting/done/timed_out) | 节点 flag + flag_events;没有 done——只有「flag 置了」 |
| await: reply 前缀匹配 ⇒ done(D1) | 消失:回复不是 flag;owner 显式置 flag(agent 用 `hyprial workflow complete`,人用看板/CLI) |
| 观察者 ack 共享 pending(D2) | 消失:PAC 不读 inbox |
| report(散文、单收件人、重复投递,D4–D6) | 消失:report = 订阅 flag_events 的投影,谁要谁读 |
| on_timeout / escalate | 反应器的一个时钟节点:到期未置 flag ⇒ 发「逾期」通知给 owner 与上游;仍不替人置 flag |
| hooks 空槽(D7) | flag_events 就是事件流,kanban H1 直接订阅 |
| assign 表 | 不变:通知送达时记 (owner, graph, node),flag 置时 release |

## 5. 第一条竖切

以 Allen 的例子(feat-collector → pm-helper → pm 确认 → dev-help → dev 确认 → 实施 → demo → pm 确认 → deploy-help → deploy 确认 → 上线,含两条回边)建一张图,跑通:①编辑图(CLI 建图、加节点/边、改版本)②置 flag 触发通知(经 hyprial message send)③reset 触发撤回通知④一条回边触发第 2 次「轮到你」⑤kanban 订阅 flag_events 做投影(H1 的第一个消费者)。验收:全部由 flag_events 表能复述整个流程;PAC 从不读任何回复正文(扫描判据)。

## 6. 不做

不做 job 分类(agent.send / verify / merge…这些是节点里的活);不做 exactly-once 与补偿;不做 worker pool;不把图存进看板;不引第三方调度器。

## 7. 待 Allen 裁

- A:flag 是布尔还是小枚举(置/未置 vs 通过/不通过/待定)——布尔 + 「不通过」作为独立节点更简单,枚举更贴人的直觉;我倾向布尔。
- B:「轮到你」通知的内容边界:只给 brief_ref + event_id(收件方自己去读)还是带一段固定模板文字——我倾向前者。
- C:时钟节点(逾期)是否进第一条竖切——我倾向进,因为今天的 timed_out 噪声正是它要替掉的。
- D:图的编辑权限:节点 owner 只能改自己节点的 flag,图结构由图的 owner 改——我倾向如此。

## 8. 立项(Allen 2026-09-04:「这个概念稿可以,开 PR(集成分支 → dev)并排进 h2b-developer 的任务中」)

- 集成分支 `intg/pac-graph`;本文件是它的第一笔;单元 PR 一律进本分支;全绿 + 第一条竖切 E2E 后一次 → dev。
- 协调:h2b-developer(按 phase 细化规划后派 worker,与 login 线 infra-op 同法);hq-adjutant 核任务书。
- §7 四点在 Allen 另裁前按本稿倾向执行:A flag 布尔(「不通过」作独立节点)/ B 通知只带 brief_ref + event_id / C 时钟节点进第一条竖切 / D 节点 owner 只改自己的 flag、图结构由图 owner 改。
- 与 PAC v1 的关系:v1 的 workflow.yaml / executor / registry 保持可用直到竖切能替代今天的派单形态;卡 1302ff17 的缺陷不再在 v1 上修,按 §4 的对应表随新形状消失或转为节点。
