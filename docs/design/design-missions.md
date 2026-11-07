# Missions：由 squire 维护的任务线，jev 负责关联 PAC

> 存储与 CLI 路由已于 2026-10-08 被 work item 设计取代：当前数据位于所选组织的
> 目录空间 `work/` 下，统一通过 `hyprial org work` 操作。下文关于旧专用空间与
> 目录的引用保留为历史设计记录；不执行数据迁移。

状态：**定稿**（2026-09-29，user-exp-improver 起草；按 hq-adjutant 的分派裁定、cli-developer 和 org-improver 的核对、kimi 评审 701 修订，见第 8、9、10 节）。**不需要改 daemon，因此不受 #976 冻结的限制**（逐步核对见 §10 B3）；T7 验收依赖两项 orgfs 缺陷修复（第 9 节）。
范围：只做 missions 和 PAC 总览。⛔ 不涉及 kanban socialware（Allen 2026-09-29：«不用管kanban socialware，mission+pac dashboard完成后，我们需要基于orgfs完整重做kanban socialware»）。

## 0. Allen 的裁定（原话）

| 时间 | 原话 | 含义 |
|---|---|---|
| 09-29 | «这里混淆了任务线（要完成哪些功能点）和人，我建议分两层泳道，最外面是人/agent… 里面是具体的任务线» | 任务线（要完成的功能点）≠ 执行者（人/agent） |
| 09-29 | «任务线的维护交给squire，所有用户的squire应该在orgfs上共同维护一个missions的文件，jev用于在pac启动的时候自动关联，或者提醒squire增加/关闭/修改 任务线» | squire 写 missions；jev 在 PAC 启动时做关联，或者提醒 squire |
| 09-29 | «missions 用单独的 org 空间» | 专用 orgfs 空间，不和 dashboards 混放 |
| 09-29 | «不用管kanban socialware» | 本设计不考虑看板 |

## 1. 现状（已核）

- PAC workflow YAML（version 2）顶层字段只有 name、summary、on_failure、escalate_to、defaults、workers、nodes，节点上也没有归属字段（src/hyprial/pac/workflow_schema.py）。⇒ 没有任何地方声明任务线。
- routine 的 source.kind 只有 taskwarrior、pac-journal（停滞的图）、scheduled 三种（src/hyprial/routine/schema.py）。⇒ **没有“PAC 创建”这个触发源。**
- jev（python_worker）只能回复本次投递：PROACTIVE_SEND 为 unsupported，子进程不调 daemon（src/hyprial/harnesses/capabilities.py 里 jev 那一行；docs/design/design-user-proxy-harness.md R10）。⇒ **jev 不能自己读写 orgfs，也不能主动发消息。**
- **jev 是打包好的 TypeSafe 分类器，会调用模型**（`src/hyprial/harnesses/_python_worker.py`：DEFAULT_MODEL `jev-latest`，凭据 `TYPESAFE_API_KEY`）。它不执行自定义脚本。一次调用的输入是 `{state, questions}`：`questions` 里每一项是带类型的问题（`choice` 从给定选项里选一个，`noul` 是 0–1 的概率，`score` 是分数），输出是对应类型的 answers（`contract/workers/python-worker/fixtures/success.json`）。投递给 jev 的消息正文会按 JSON 解析成这个 payload，解析不了就作为失败的一轮（`src/hyprial/harnesses/python_worker.py` 的 enqueue）。
- PAC 总览（#996 已合；#1009 两层泳道已评审通过）目前是按关联推断任务线：同一 PR、同一主题、重跑。

## 2. 数据：missions 文件

- 位置：专用 orgfs 空间 `missions`（org owner 创建，全员 rw；上线跟踪见 PAC wf-64926d66），**每个 mission 一个文件** `missions/M-allen-0007.md`（hq 裁定，见第 8 节）。
- 格式：md，文件开头有一个机器可读的 YAML 块，后面是自由文本。人和 squire 可以直接读改，页面和 jev 可以解析。

````md
## M-allen-0007 PAC 总览上线 orgfs

```yaml
id: M-allen-0007
status: active            # active | done | paused
owner: agent:…:user-exp-improver     # 负责的人或 agent（完整 URI）
opened: 2026-09-28
closed: null
keywords: [pac-overview, overview, "#996", "#1009"]
pacs:                     # 块列表，每行一个；由 jev 建议、squire 追加
  - wf-2fa2f403…
  - wf-441a4a3b…
```

背景：……（自由文本）
目标：……（自由文本）
````

- 只有 squire 写这些文件（各用户各自的 squire 都可以写）。每个 mission 一个文件，冲突面小，最后修改者也按文件区分。
- 写入协议以 squire skill（#1012）为准：用 `hyprial fs write --expect-version`，遇到 stale-write 就重读后重试，连续 3 次失败就停下来告诉主人。⛔ 不用 `--base-version`：它走三路合并，从不拒绝写入（org-improver 实测）。
- `pacs` 只追加，不删除；mission 关闭时状态改为 done，不删文件，保留历史。
- id 格式为 **`M-<主人短名>-NNNN`**（例如 `M-allen-0007`），每个 squire 只递增自己主人的序号，因此**不同主人之间不会撞号**（org-improver 核对：全局顺序号在多机同时新建时按构造必撞）。**同一个主人**在两台机器上各有一个 squire 时仍可能撞号，这种情况由下一条的 name_conflict 兜底。
- 兜底：orgfs 对同名新建不会静默覆盖，两份都保留，`ls` 里标 `name_conflict`，按路径访问报 `ambiguous-path`。这属于需要人工处理的错误状态，squire 定期扫描 `name_conflict` 并提醒主人。
- 规模：每个 mission 一个文件，几百个的量级 orgfs 没有问题（org-improver 核对）。

## 3. 角色与流程

**同名冲突处理（org-improver，2026-10-08）**：squire 不掌握目录空间 UUID，因此巡检只用
`hyprial org work`：`collect` 的 `conflicts` 与 `ls --invalid` 都展示每份副本的 nodeId；
`show <id> --copy id:<nodeId>` 逐份读回原文。未指定 `--copy` 时不得从冲突中任选一份。主人选定
保留项后，`resolve <id> --keep …` 由 daemon 验证 owner（或标记未验证的本机 operator），保留选中
副本的原 id，把每个其余副本重命名为按 `add` 规则分配的新 id，并在两侧追加互指日志和本机账本记录；
不删除任何副本。后续合并仍使用 `note`、`check`、`status dropped` 等普通命令。

**运行时给定的形状（已核，dev）**：scheduled routine 每触发一次，就建**一张图，其中只有一个工作节点 `work`**（`src/hyprial/routine/pac_dispatch.py`：“one task, one graph”，`WORK_NODE = "work"`），节点 owner 是 routine 的 actor；另一个节点是 escalate_to 持有的截止时钟节点（`DEADLINE_NODE`，kind clock，受 work 守护，`pac_dispatch.py:279-283`），它不做事。routine 不能派出多个工作节点。所以“收集”和“处理”不是两个节点，而是**同一个 `work` 节点里、由同一个 squire 顺序执行的两步**。

**范围：每台机器只处理本机发起的图。** 每台机器的 routine 只看本机 daemon `workflow.list` 里的图，由本机的 squire 处理。本机图的发起者就是本机的主人，因此不需要跨机委派，也不存在“远端 squire 离线”的问题。

```
每 15 分钟（scheduled routine，actor = 本机 squire 的完整 URI，role: dispatch）
  ──▶ daemon 建一张图（work 节点归 squire，另有截止时钟节点）
  work 第 1 步「收集」（squire 执行 `hyprial missions collect …`，T4 helper）：
     1. 取上一轮状态：找本 routine 最近一张已完成的图（workflow.list 按 routineName 过滤，
        取 createdAtMs 最大的），执行
        `hyprial workflow inspect <上一张图> --node work --json`，读它的 outputText
        （= 上一轮的 {asked[], declined[]}）。首次运行没有上一张图，按空状态处理。
     2. 用 orgfs read_at 按版本导出 missions/ 下的 mission 文件
     3. 求新图集合 = 本机 workflow.list 里 createdAtMs 在最近 7 天内的图
        − routineName 非空的图（routine 自己派的图，包括本 routine 的每一轮）
        − mission 文件 pacs 里已记录的 graphId
        − 上一轮 asked[] 和 declined[] 里的 graphId
        每轮最多处理 20 个，多出的留到下一轮（按 graph id 幂等，不会漏）
     4. 确定性预筛：每个新 PAC 取前 5 个候选；零候选直接判为 none
     5. 对有候选的 PAC，每个 PAC 用 `hyprial message send` 给 jev actor 发一条**纯 JSON** {state, questions}，
        从自己的收件箱按 messageId 读回复（CLI 没有同步的“发送并等待”）
  work 第 2 步「处理」（同一个 squire、同一次执行，结果就在手里）：
     · 满足自动关联条件 ⇒ 把 graphId 追加到该 mission 的 pacs（不问人）
     · 低置信、多个候选或没有命中 ⇒ 问主人（“属于哪个 mission？还是新建一个？”或“是否新建”，
       附建议的标题和关键词），把这个 graphId 记进 asked[]
     · 主人之后在对话里回答时，squire 写 mission 文件（关联或新建），或把它记进 declined[]；
       这些变更在下一轮的 outputText 里体现
  完成：`hyprial workflow complete <本图> work --output-text '<本轮状态>'`，
        本轮状态 = {asked[], declined[], linked[], failed[]}（≤64 KiB，见下）
  jev 失败（例如 decode 失败表现为一轮失败）时，该 PAC 记进 failed[]，下一轮重试；
  jev actor 整体不可用时，本节点显式 fail 并附上失败引用，不要干等。节点时限要覆盖若干次 jev 往返。

定期（例行，比如每天一次）「巡检」（squire 执行，需要判断时同样用 hyprial message send 问 jev）：
  · 某 mission 下所有 PAC 都已结束，且 N 天没有新 PAC ⇒ 建议关闭
  · 某 mission 的 keywords 长期匹配不到 ⇒ 建议修改
  结果交给 squire，squire 向主人确认后再改文件
```

- **跨轮状态只经 `workflow inspect` 传递（B1）**：后继节点或下一张图的投递里只有引用，没有前一节点的 outputText（`pac/store.py:145-165`、`pac/workflow_runtime.py:224-241`）；outputText 只能用 `hyprial workflow inspect <图> --node <节点> --json` 读到（`workflow_runtime.py:1066-1067`）。这需要 shell 和 CLI：squire 有，jev 没有。每一轮的 outputText 都带上**完整的** asked/declined 集合（不是增量），所以只需要读上一轮这一张图。条目超过 7 天窗口就丢弃；接近 64 KiB 时从最旧的开始丢，被丢的 PAC 下一轮会重新进入候选，最坏情况是再问一次主人。
- **约束：每一轮的 `work` 节点必须由同一个 squire 担任 owner。** `workflow.node.inspect` 只允许三类调用者：本机用户、图的发起者、该节点的 owner（`daemon/application.py:6998-7006`）。routine 的 actor 固定为本机 squire 时，每一轮的 work 节点都归它，它读上一轮没有问题。如果 routine 的 actor 换了，新 squire 读旧图会得到 `CALLER_NOT_AUTHORIZED`：这时**必须显式 fail 本节点**，并告诉主人。不能把它当成“没有上一轮状态”处理，否则会把 asked 里的 PAC 全部重问一遍。T7 有对应的负控用例。
- **不跨机、不读别人的数据（N4）**：不采用“读各节点发布到 dashboards 的 overview 文件、获取全 org 的图”的方案，理由有三：那些文件是派生数据，最多滞后一个发布周期；里面没有派单人的对话上下文；而且会让一个 squire 替别的主人做关联，绕开对方向主人确认的环节。代价是：没有运行这个 routine 的成员，他们的 PAC 在总览上只有推断出的任务线。
- **间隔 15 分钟**：每一轮都是一张图。5 分钟一轮每台机器每天是 288 张图，会淹没 journal 和总览；15 分钟是 96 张。这些图的 routineName 非空，总览把它们归到 routine 任务线（#996 已有），不会被当成新 PAC。schedule.interval 的下限是 60 秒（`routine/schema.py` 里的 MIN_INTERVAL_SECONDS），15 分钟不受限制。
- **routine 定义要点**：`mode: scheduled`，`actor:` 填本机 squire 的完整 URI，`role: dispatch`（借用交互式 actor 只能是 dispatch、plan 或 review；用 execute 会在 admission 被拒）。
- **两段式匹配**：
  - 第 1 步先做确定性预筛（纯 python，打包进 hyprial，squire 通过 CLI 调用）：比 PR 号，再比 keywords，再比主题词二元组和重跑（这三项和总览的推断规则一致），最后看同一派单人近期活跃的 mission。每个新 PAC 取前 5 个候选。
  - jev（模型）只在候选里做选择：每个候选一道 `noul` 题（“这个 PAC 属于该 mission 的概率”）。TypeSafe 的 `choice` 题只有说明和判据，没有选项列表字段，所以不用 choice。helper 把 answers 换算成 `match`（最高概率的候选）和 `confidence`（该概率）。
  - 预筛零候选时不调 jev，直接判为 none，交给 squire 问主人是否新建。
- **jev payload 的大小（N4c）**：payload 走 `hyprial message send`，不是 workflow output，所以不受 64 KiB 的限制；harness 这一侧没有找到比 python_worker 的单行 8 MiB（`harnesses/_python_worker.py` 的 MAX_LINE_BYTES）更小的上限。为了控制成本，helper 自己给每条 payload 设 **32 KiB** 上限：每个候选 mission 只放 id、标题、keywords、status，PAC 只放 graphId 和名称（T4 #1014；派单人 URI 含邮箱，按 jev 的脱敏规则由 #1032 去掉；不放任务正文）。截断之后仍然超出时，**不调 jev，也不传引用**（jev 不能解引用），这个 PAC 直接按“低置信”处理，交给 squire 问主人。
- **数据出站**：每次调用 jev，payload（PAC 的 graphId、名称；候选 mission 的 id、标题、keywords、状态）都会发到外部的 TypeSafe API，也就是**离开 org**。这是 Allen 定“由 jev 做关联”带来的固有代价。只有预筛出候选时才调用。如果以后要收紧，就只发 mission 的 id、标题、keywords，PAC 只发名称。
- **workflow.list 的 500 条上限**（org-improver 核对）：`workflow.list` 只有 limit（最多 500），没有 offset 或 since，所以没法翻页；routine 自己每天就建 96 张图。它按创建时间倒序返回，每张新图在创建后的下一轮（15 分钟内）就会被看到，每轮最多处理 20 个，因此只有 routine 长时间暂停或者持续积压时，较老的图才会掉出 500 条。list 返回满 500 条时，collect 报告 `coveredSinceMs`（覆盖到的最早创建时间），由 squire 在每日汇总里告诉主人；asked/declined 里掉出范围的条目会被修剪，最坏情况是再问主人一次。按 graph id 幂等，所以不会关联错。
- **轮次重叠和缺失**：上一轮还没结束时，本轮 collect 返回 `skipped`，squire 以 `--output-text '{"skipped":true}'` 完成本轮；读上一轮时会跳过这类轮次，以及失败或被取消的轮次（它们没写状态），往前找最近一个已完成的轮次。因为每一轮都写完整集合，跳过不会丢状态。已完成但 outputText 缺失或不是状态对象的轮次按“读不到”处理，显式 fail，不当成空状态。
- **自动关联的判定**由 squire 做（org-improver 的 squire skill v3，PR #1012）：`match` 非空、`candidates` 恰好只有一个，并且 `confidence ≥ <missions-auto-threshold>`（squire profile 里的值，默认 0.9）。三条都满足才自动写入，否则问主人。
- **解析规则**（T4）：`pacs` 同时接受块列表和行内列表两种写法，因为人可能手改文件。squire 按块列表写（实测两台机器同时追加时，两行都会保留；同时改同一行会乱码，例如 done 和 paused 合成 `status: pdonused`）。某个文件的 YAML 解析失败时（乱码合并就是这种情况），helper 把它报告为 unparseable，**不静默跳过**，由 squire 修复并告诉主人。
- jev 只做判断，不做写入；所有写入都由 squire 完成。这样也符合 jev“只能回复”的能力边界。
- **不用游标，按 graph id 幂等关联**（cli-developer 核对）：routine store 没有可写的游标字段，加字段属于 schema 变更。每轮都从“最近 7 天的本机图”减去已记录、已问、已拒的 graph id，崩溃或重跑都能自愈。mission 文件仍是“哪个 PAC 属于哪个 mission”的唯一真相源；outputText 只记录“问过主人、还没答”和“主人说不归入”这两类不属于任何 mission 的状态。

## 4. PAC 总览的变化（user-exp-improver 负责）

- `workflow overview publish` 在收集数据时，也读 `org` 空间里 missions/ 目录下的 mission 文件（只读）。
- 有 mission 的 PAC 按 mission 分组：里层泳道显示 mission 的 id、标题、状态；外层仍然是人/agent，按 mission 的 owner 归组。**不复制背景和目标**：总览写在 dashboards 空间，两个空间的成员不一定相同，背景和目标只留在 missions 空间，页面上给出 mission 文件的路径。
- 没有被任何 mission 收录的 PAC，继续用现在的推断，放在“未归入 mission”一组，页面标“推断”。
- 数据文件里加 `missions[]`，**只放 id、标题、状态、owner、pacs 这五个字段**，页面据此分组。没有 missions/ 目录时完全退回现在的行为。

## 5. 任务拆分（经 hq-adjutant 分派）

| # | 内容 | 建议归属 | 依赖 |
|---|---|---|---|
| T1 | 创建 `org` orgfs 空间，邀请全员，并建好 missions/ 目录和一个示例 mission 文件 | Allen（owner 操作），org-improver 协助 | 发版 |
| T2 | ~~routine 新增触发源 `pac-created`~~：已裁定用 scheduled 轮询；**不用游标**，按 graph id 幂等，跨轮状态经 `workflow inspect` 读上一轮 outputText（第 3 节）。无剩余工作 | — | — |
| T3 | ~~daemon 给 jev 注入文档~~：已定为 squire 用 read_at 导出，helper 构造 payload，用 `hyprial message send` 发给 jev（≤32 KiB，第 3 节），并入 T4 | — | — |
| T4 | 两部分：① 预筛和 payload 构造的 CLI helper（`hyprial missions …`，纯 python，含 read_at 导出、读上一轮 outputText、按 graph id 求新图集合）；② jev 的 TypeSafe 问题定义（每个候选一道 noul）和巡检问题。加 fixture 测试 | user-exp-improver | §6.6 |
| T5 | squire 的职责和 skill：处理 jev 的结果、向主人确认、写 mission 文件、id 分配和冲突处理 | org-improver（hq 已定） | T1 |
| T6 | 总览读取 missions，并按 mission 分组（第 4 节） | user-exp-improver | T1 |
| T7 | 端到端测试：两个隔离 daemon、**两个不同主人**的 squire 各自关联本机 PAC 并发写同一个 missions 空间；一个新 PAC 的完整关联链路（含第二轮读到第一轮的 asked 不重问）；**`hyprial message send` 给 jev 后回复落在发送方收件箱**的实测；**负控**：routine 换了 actor 后，新 squire inspect 上一轮得到 CALLER_NOT_AUTHORIZED，本节点显式 fail 而不是按空状态重问 | user-exp-improver 加 e2e-verifier | T1–T6，以及第 9 节的两项 orgfs 修复 |

## 6. 需要各线确认的技术点

1. ~~T2 用 A 还是 B~~。已定用 B（scheduled 轮询，15 分钟），不用游标（§8.1、§8.7、第 3 节）。
2. ~~T3：jev 看不到 orgfs~~。已定（第 3 节）：squire 用 read_at 导出，helper 构造 payload，用 `hyprial message send` 发给 jev。更正：早先写的“作为 workflow output 传给 jev 节点”不成立，后继投递里没有前一节点的 outputText（B1）。
3. ~~跨节点的 squire 能否当 PAC 节点 owner~~。已不需要：每台机器只处理本机发起的图，由本机 squire 处理（第 3 节）。
4. mission 文件的并发写：已裁定每个 mission 一个文件（第 8 节）。仍请 org-improver 确认两个 squire 同时改同一个 mission 文件时，合并结果可读。
5. 权限：`org` 空间全员 rw，意味着所有成员（和他们的 agent）都能写 missions。是否要求“只有 squire 写”通过约定保证，还是要做成权限？建议先靠约定，页面上显示每一段的最后修改者。

6. **jev 不能当 PAC 节点 owner（cli-developer 按源码核对，dev 04dd2ffc）**：
   - 节点请求正文固定是文字：`pac/workflow_runtime.py:239-252` 总是拼接 task、前序节点引用、roster 和 complete/fail 说明，没有原样传 JSON 的模式，jev 的 `json.loads` 必然失败。
   - 节点只有 owner 显式调用 workflow complete/fail 才算完成，回复不算；而 jev 没有工具注入，也没有 shell，永远关不了自己的节点。
   - 更正：早先写的“下游节点能把前一节点的 output 当作 input evidence 看到”不成立。后继节点的投递里只有引用，没有 outputText（`pac/store.py:145-165`、`pac/workflow_runtime.py:224-241`）；outputText 只能用 `hyprial workflow inspect <图> --node <节点> --json` 读到（`workflow_runtime.py:1066-1067`），这需要 shell 和 CLI，jev 没有。
   - ⇒ 采用上面第 3 节的做法：由 squire 的 work 节点用 `hyprial message send` 问 jev、读回复、写 output。不建议给 daemon 加“原样输入”模式：那既要改 daemon，也解决不了完成的问题。
   - **设计验收时要实测一次**（推断，尚未端到端跑过）：给 jev 发一条消息，确认回复落在发送方的收件箱，并记录回复的结构。列入 T7。

7. **（新增）jev 的凭据**：jev 调用 TypeSafe 需要 `TYPESAFE_API_KEY`（有成本）。用哪一份 key、由谁持有、放在哪台机器，由 hq-adjutant 按 grant 流程发放：只给文件路径或 grant id，不给值。成本控制上，只有预筛出至少一个候选时才调 jev，每个新 PAC 最多调一次。

## 7. 不做的事

- 不改 PAC workflow YAML：关联记在 missions 文件里，派单不需要额外填字段。
- 不做 kanban 同步：kanban 以后基于 orgfs 重做。
- ~~jev 不调用模型~~（更正：jev 是 TypeSafe 模型分类器，见第 1 节）。可解释性来自两段式：确定性预筛决定候选，jev 只在候选里选，并给出置信度。

## 8. hq-adjutant 的分派与裁定（2026-09-29）

1. §6.1：**先用 B（scheduled 轮询）**，不改 daemon。A（`pac-created` 触发源）等真有需要再做。⇒ T2 取消。~~游标状态放在 routine 或 jev 一侧~~：已被 §8.7 取代，不用游标，按 graph id 幂等。
2. §6.4：**直接用每个 mission 一个文件**（`missions/M-allen-0007.md`），不赌同一段文字的 CRDT 合并结果可读。仍请 org-improver 确认 orgfs 在这种用法下没有坑。
3. **（已被 §8.7 取代）** ~~T3、§6.2（jev 怎么拿到文档）、§6.3（跨节点 squire 能否当 PAC 节点 owner）：交给 cli-developer 和 org-improver 一起定。这两件都要改 daemon，属于 #976 的冻结范围：设计现在先定，代码等 #976 合进 dev 之后再动。~~ §8.7 核对后，这两件都不需要改 daemon，不受 #976 冻结限制。
4. T5（squire 的职责和 skill）：归 **org-improver**（组织结构这一线）。
5. §6.5 权限：先靠约定，页面显示每个 mission 文件的最后修改者。
6. T1：Allen 以 owner 身份建空间，前提是发版，并且发版要带上 #996、#1009。时间到了由 user-exp-improver 直接和 Allen 约，事先告诉 hq-adjutant。
7. cli-developer 核对（dev 04dd2ffc）：§6.1、§6.2、§6.3 都**不需要改 daemon** ⇒ missions 不受 #976 冻结的限制。具体做法按 kimi 评审 701 修订后写在第 3 节：按 graph id 幂等；read_at 导出后用 `hyprial message send` 发给 jev（不是经 workflow output）；每台机器只处理本机的图（不用远程委派）。
8. org-improver 核对（dev）：id 改为 `M-<主人短名>-NNNN`；每个 mission 一个文件在几百个的量级没问题（第 2 节）。

## 9. 已知限制与前置依赖（orgfs）

以下两项 orgfs 缺陷会直接影响 missions：修复前的表现都是“别人改了 mission，我这边看不到”。它们是 **T7 验收的前置依赖**；修复前属于已知限制。两项修复都要等 #976 合进 dev。

| 缺陷 | 现象 | 修复卡 |
|---|---|---|
| 1b85fb06 这一类 | 某个写者的一次推送丢了，读者卡在这个缺口上，直到这个写者下一次重连或重启（在稳定的链路上可能要好几个小时）才会补齐：写者的 liveliness 重新出现时，读者会跑 schedule_sync_from 补上缺口。生产上 09-29 09:03 在约 16.5 小时后自愈。修复方向是定期同步，或者发现缺口时触发同步 | 267703c2 |
| 80a6e231 | 空间的 announce 丢了之后不会补发，join 一直报 `no-holder-online` | 80a6e231 |

另外，checkout 在 blob 迟到时不会重新落盘（卡片 c825f0ea）。它只影响用 checkout 打开总览页，不影响 squire 和 jev 通过 orgfs 读写 mission。

## 10. kimi 评审 701 的答复（2026-09-29）

| 项 | 结论 | 改在哪里 |
|---|---|---|
| B1 | 接受。后继投递里没有 outputText，只能 `workflow inspect` 读。另外核实到 scheduled routine 只建单节点图（`WORK_NODE = "work"`），所以“收集”和“处理”改成同一个 work 节点里的两步；inspect 用来读**上一轮**的 outputText（asked/declined 状态），避免重问 | 第 3 节、§6.2、§6.6 |
| 同 owner 约束 | 接受。inspect 只对本机用户、发起者、节点 owner 开放（`daemon/application.py:6998-7006`）；routine actor 必须固定为同一个 squire，换了 actor 就显式 fail。T7 加负控 | 第 3 节、T7 |
| B2 | 已在之前的推送里解决 | 第 1 节 |
| B3 | §8.3 标为已被 §8.7 取代，§8.1 的游标同样标注。逐步核对是否需要改 daemon：scheduled routine 借用交互式 actor（已有，role: dispatch）；`workflow.list` 已返回 createdAtMs、routineName；`workflow inspect --node` 已有；`workflow complete --output-text` 已有；`hyprial message send` 给 jev 并从收件箱读回复（已有，T7 实测）；orgfs read_at、`fs write --expect-version` 已有。新增代码只有 CLI helper（T4）和总览（T6），都不在 daemon 里 | 文件开头、§8 |
| N1 | T2 改为“不用游标、按 graph id 幂等”，T3 同步改 | 第 5 节 |
| N2 | 改为“不同主人之间不会撞号”；同一主人两台机器仍可能撞号，由 name_conflict 兜底 | 第 2 节 |
| N3 | 更正：`contract/e2e/run-pac-cross-node.py:39,59` 用的是 e2e 和 e2e-other 两个 owner，跨 owner 委派已有覆盖。新设计不再走跨机委派，T7 改为“两个不同主人的 squire 并发写同一个 missions 空间” | 第 3 节、T7 |
| N4 (a) | 不再有远端 squire：每台机器只处理本机发起的图，由本机 squire 处理，不需要跳过和计数器 | 第 3 节 |
| N4 (b) | 时间窗口是 createdAtMs 在最近 7 天内（和总览的 window_days 一致），数据源是本机 `workflow.list`，排除 routineName 非空的图，每轮最多 20 个。不读全 org 的 overview 文件，理由见第 3 节；没运行 routine 的成员只有推断出的任务线 | 第 3 节 |
| N4 (c) | jev payload 走 `hyprial message send`，不受 64 KiB 限制；helper 自设 32 KiB 上限，截断后仍超出就不调 jev、不传引用，按低置信交给主人 | 第 3 节 |
| N5 | 只复制 id、标题、状态、owner、pacs 五个字段，不复制背景和目标 | 第 4 节 |
