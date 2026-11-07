# Multi-thread / Agent Identity — 讨论稿(WIP)

日期:2026-10-15
状态:**WIP,未经 Allen 最终裁定**
来源:Allen 提出,hq-adjutant 检查与讨论

## 问题

当前 hyprial 的 agent 不能稳定区分消息的来源(单聊、群聊、agent-to-agent)。

实测样本:一个 agent 通过 `route:hq-adjutant-feishu:allen-dm` 发消息,落点是 all-member 群。路由名与落点不一致,发送方没有任何一栏能看出。

## Allen 的提议

给 agent 添加 multi-thread 支持:
- agent 绑定 PAC,默认一个 agent 对应一个 thread
- 例:一个 agent 同时有 DM 身份和群聊身份,分身为两个 agent,由 PAC 协调

## hq-adjutant 查到的三条事实

### 1. 来源信息在 adapter 层齐全

`daemon/impl/adapters/lark/inbound/pipeline.py:428-431,502` 读到了 `chatId`、`threadId`、`conversationId`。它们在 reconcile 里也被用作幂等键。

### 2. 消息层没有这一维

`daemon/impl/messaging/` 搜 `source_route|route_id|origin` = 空。pending 消息形状:`messageId · deliveryId · conversationId · from · recipient`(无来源种类)。

结论:来源在 adapter 被读到,在"交给 agent"那一刻被压平成一个收件人。

### 3. 产品已有"身份 ≠ 运行时"抽象

`hyprial agent --help`:"an identity plus its configuration; the harness it runs on is a runtime binding, not part of the agent"。

## hq-adjutant 的判断

1. 问题真正的维是"来源(source)",不是"身份(identity)"。混起来会把成本放大。
2. 最小修法:把 adapter 已知的来源带进消息 + 让回复回指来源。
   - 入站消息带 `source`(chat/thread/route 三元)
   - 出站回复带 `reply-to-source`
   - 效果:同一个脑、同一个身份,"在哪说话"与"回到哪去"不再靠猜
   - 成本:在已有 `chatId/threadId/conversationId` 上加一栏并透传,不必新增运行时
3. 什么时候才该分身为多个 agent:由"权限/凭据/上下文边界"决定,不由"来源分类"决定。
   - 例:DM 身份需 owner 级信任;群身份不该有
   - 权限面相同就拆开,只是把一个脑变成 N 个脑
4. 不建议默认一个来源一个 agent:
   - 主体数会爆(本机已 350+ agent)
   - 状态会分裂
   - 每个 agent 是一个活 worker,N 倍常驻
5. 与 chat/adapter 的配合,分层划清"谁知道什么":
   - adapter = 来源的权威(它写 `source`)
   - 消息层 = 搬运,不丢
   - agent = 消费并回指
6. 对"agent 绑定 PAC"保留意见:PAC 是任务图语义(node/edge/flag)。用它承载"同一个体的多身份",会为了协调而先造一堆任务。更贴的是路由表 + 权限表。

## 建议的下一步(待 Allen 裁)

- 甲:先做来源维透传(入站带 source,出站回指)
- 乙:再定"什么时候拆主体"的判据,提议 = 权限面不同
- 丙:是否立卡由 Allen 定

## emergent-org-structure-refactor 的分析（2026-10-15，owner）

### 结论

1. 问题有两个维，不要合并：**来源（source）**和**上下文并发（per-thread context）**。hq-adjutant 的「来源透传」解决的是"回到哪去"写错；Allen 的「每 thread 一个 harness」解决的是"线性 harness 把 N 个会话压进一个上下文，模型靠注意力维护寻址表"。前者是消息层加一栏，后者是运行时结构。两者都要，但分三步做，第一步不需要新 harness。
2. 它与正在落地的 link 机制（#1457）是互补的两层：link 管 agent 之间的扇出（k、W、cwnd），本文管 agent 内部的并发。合起来 W 有物理含义：同时活着的 thread/harness 数上限。
3. 不建议默认"一个来源一个 agent"（同意 hq-adjutant 第 4 条）；建议**一个 agent、N 个 thread 运行时**：身份不变（同一个 actor、同一份 agent home 记忆），运行时按 thread 拉起与休眠。分身为多个 agent 的判据仍是权限面不同（同意第 3 条）。

### 代价（要先想清楚的四件事）

1. **跨 thread 的综合消失。** hub 类 agent 的价值是把多条线合起来看。需要一个 System Two 综合回合（定时或事件触发），读各 thread 的 summary 流；否则把"回错人"换成"没人看全局"。
2. **共享记忆的写冲突。** agent home / notes / orgfs 成为多个 harness 的共同记忆，写入要串行化（daemon 单写者或 orgfs CRDT 语义），不能让每个 thread 直接改同一份 notes。
3. **生命周期不是 PAC 的 worker 语义。** PAC 的单位是有界任务；thread 是长寿、间歇活跃：来消息唤醒、空闲休眠、恢复时带 thread 转录 + agent 记忆。建议给 PAC 加 thread-actor 生命周期（spawn / hibernate / resume），不要硬套 worker——否则每条消息冷启动一个 harness，成本和延迟都不对。这与 hq-adjutant 第 6 条的保留意见一致：不是用任务图协调身份，是加一种运行时生命周期。
4. **成本上限。** claude 系每 thread 一个进程不便宜；分层解决：owner thread 与决策线用 strong harness，例行 thread 用 pi + 便宜模型，System One 本身用 flash 级模型做分流并直接答掉 ack / 状态类消息。

### 三步落地（可在 #1457 合入后开一条独立线）

- **第一步：来源透传 + 确定性 thread 路由（daemon 侧，无新 harness）。** 入站带 `source`（chat / thread / route 三元，adapter 写）、出站带 `reply-to-source`（即 hq-adjutant 的甲）；daemon 按 `conversationId` / `reply-to` / `source` 把入站分到 thread 队列，注入时一次只给一个 thread 的上下文并显式标注对端；回复只能经 reply-to（#1457 已做）。预计消掉大部分错回复。
- **第二步：System One 分流。** 便宜模型只读 summary（不读 body，#1457 的 summary/body 拆分正为此准备），决定：新 thread 还是续、优先级、能否直接答（ack / 状态）、要不要唤醒 harness。这一步开始压 hub 的 context 消耗。
- **第三步：thread-per-harness。** thread-actor 生命周期 + 共享记忆单写者 + System Two 综合回合；W 从"未闭合请求数"升级为"活跃 thread 数"的预算。

### 需要 Allen 裁的两点

1. 成本上限：每个 agent 最多同时几个活 harness；是否按 tier 分。
2. System Two 综合回合的触发方式：定时还是事件。

### 与 hq-adjutant 建议的对应

甲 = 第一步的前半，建议直接做；乙 = 本文第 3 条结论，判据取"权限面不同"；丙：建议先立第一步的卡，第二、三步等 #1457 合入后按设计文档再立。
