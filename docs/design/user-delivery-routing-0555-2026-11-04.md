# `user:` 投递与多 daemon 可见性（0.5.5.5 WIP）

状态：调查与方案，未实施。目标集成线：`intg/cell-model-phase-a`，最终发版目标 `release/v0.5.5.5`。本稿核对该集成线 `0b6415f2b` 与 [PR #2351](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/2351) 的 `5fad3fe4d`；PR #2351 只实现外部 custody agent attach/detach，其 phase B brief 描述的 S10 普通 agent 化尚不在该 PR 内。这里不把 brief 当成已落地行为。

## 结论

**不把 `user:<owner>` 默认改为“每台同 owner daemon 各写一份普通 inbox，并各自二次处理”。** 现有 Zenoh 帧已经发布到 `.../user/<owner>/<messageId>`，所有 daemon 订阅 `.../user/**`；当前真正只让 `preferredReceiver.machine` 处理，其他 daemon 不发回执。改变这一点会把一次逻辑投递变成多个本地权威：本地幂等账本不能跨机阻止重复 Lark 私聊，任一机器的“已接收”也不能证明人已收到消息。

建议将 `user:` 定义为一个**逻辑用户收件对象**，与 agent inbox 和具体渠道分离。发送方交付一次并得到一份持久接收凭据；有权限的设备可以读取同一逻辑消息视图，但仅一个明确选定的执行端点可触发外部副作用（adapter 或绑定的普通 agent）。多设备可见性通过 owner 范围的 conversation/read model 或显式复制实现，不能靠多个 daemon 独立执行来暗示。网络层的既有多播不等于持久收件箱复制。

## 当前路径与演进核对

| 环节 | 当前事实 | 影响 |
| --- | --- | --- |
| `message.send` | `application/messaging/inbox_surface/send.py` 对 `user:` 先尝试本机 agent-bot，再调用 `UserDeliveryPort.deliver`；工作流、PAC 报告走 `delivery/sends.py::_workflow_deliver_user` | 两个入口共享部分路径，但本机 agent-bot 是一个必须保留的旁路和幂等域 |
| 网络 | `squire/addressing.py::ZenohUserDeliveryTransport` 发布 user frame 并等待 attempt receipt；`transport/keys.py` 的 `user_delivery_any()` 是全局订阅 | 已有网络扇出；没有持久多机投递保证 |
| 接收 | `UserDeliveryActor` 调 `ReceiverUserDelivery.owns()`，只认 profile 的 `preferredReceiver.machine`；非 owner daemon 不出回执 | `user:` 当前是单接收机语义，机器不在线会超时 |
| 转发 | profile 可存 `deliveryAgent`，但 `_deliver_to_live_user_proxy` 仅接受本机运行且 `harness == "user-proxy"`；否则走 `squireChannel`/`ownerOpenId` 的 adapter DM | 配置字段能写任意 agent 名称不代表任意 agent 已可接手；S10 须改此执行条件和绑定模型 |
| 幂等 | 接收机 `user-deliveries.json`、发送机 agent-bot attempt ledger 分开；接收机 ledger 按 idempotency key 去重 | 多接收机各有 ledger，不能直接放开所有机器执行 |
| 历史 | phase A 的 `threads.ids` 已将 `(agent, user)` 定义为 user thread，agent 交互的 conversation body 写入 owner-home OrgFS；`ReceiverUserDelivery` 当前只执行代理或 adapter 转发，没有为接收方建立用户 inbox/conversation 记录。GUI `agent-chat` 仍调 `sessions.*`，由 GUI adapter 维护 target/session 绑定 | 现有 agent 侧历史不等于接收方用户收件箱；GUI 需要新的用户收件视图/读模型 |

早期“固定 squire”已演进为可选 user-proxy，phase B 的 [S10 brief](cell-model-phase-b-brief-2026-11-03.md) 又计划让 squire 成为普通 agent，并把主人私聊建成普通 adapter thread。S10 的目标不应被解释成“任意运行 agent 自动有权接收所有 `user:` 消息”：绑定、owner 身份、可见性及执行权限仍需明确。

## 三种选择

1. **保持单接收机和单渠道（推荐用于 0.5.5.5 首步）。** 保留 `preferredReceiver` 作为一次投递的权威；把执行目标从特殊 user-proxy/squire 改为受 owner 授权的普通 agent 或 adapter thread。独立建立用户收件读模型供 GUI 和其他设备查看。改动小，能沿用现有 receipt 和幂等语义；接收机离线仍需切换或重试。
2. **所有同 owner daemon 各写 inbox 并处理。** 多机在线时容易立即可见，但必须先解决跨机 claim、全局幂等、回执聚合、消息删除/已读同步和重复通知。直接替换当前 `owns()` 不可接受。
3. **共享逻辑用户收件箱，设备作为副本（后续演进）。** 一份全局 message id 与用户收件状态，多设备消费同一记录；外部投递由带租约的唯一执行者完成。支持离线恢复，但要设计 owner 身份证明、持久复制、租约 epoch、分区时的 fail-closed、副本游标及跨版本迁移。建议在首步的读模型和事件语义稳定后单独实施。

## 建议的 0.5.5.5 边界

1. **地址与渠道分离。** `user:<owner>` 始终表示人；发送者可显式用 `route:<adapter>:<route>` 指定会话空间。用户 profile 或 owner 授权的 cell 数据只选择一个 delivery endpoint，不改变 `user:` 地址含义。`squire` 是可绑定的普通 agent/别名，不再是隐式兜底身份。
2. **单次副作用。** 同一个逻辑 message id 只由一个选定 receiver/agent/adapter 做 native send；选择结果及其版本先持久化，再发起副作用。超时或 native outcome unknown 时不自动切换至另一个渠道或机器；恢复原 attempt 或明确进入人工核查。每次重试保留同一业务幂等键。
3. **回执分层。** 分别表示 `accepted`（逻辑收件对象持久接管）、`routed`（执行端点接管）、`providerAccepted`（外部平台确认）、`read`（用户在 GUI 或渠道中确认）。`accepted` 不承诺人已看到；旧 `UserDeliveryResult.accepted` 在兼容期仍只表示原接收路径结果，不偷换含义。新字段需要 contract 与双版本互通测试。
4. **多设备只读投影。** 先为接收方建立受 owner 授权的逻辑用户消息记录，再允许同 owner 的已认证 daemon 读取其 conversation 投影；不能假设现有 agent 侧 conversation 已覆盖接收方。每台 GUI 只持本地游标/展示状态，不把副本写入普通 agent inbox，也不因读取触发 Lark 发送。若 owner-home OrgFS 断线，已有投递仍按当前路径运行，GUI 明示历史同步滞后。
5. **线程归属。** `agent -> user:` 是 user thread；普通 agent 到主人 DM 的实际 adapter leg 是另一个 adapter thread。两段共享逻辑 message id 和 causation，不能复用一个 `threadId` 假装同一对端。`conversationId` 保持 cell id 语义；不要把旧 `sessions.*` 的字符串直接当 cell id。SYSTEM 类型的 PAC/告警不因显示在用户收件视图而变成 INTERACTION。
6. **兼容迁移。** 先加入不改变 `user:` 投递的只读查询和观察指标；再在 S10 普通 agent 化后把绑定解析迁出 `squire` 特例；最后按单独迁移决策决定是否做复制或 failover。旧 profile 的 `preferredReceiver`、`deliveryAgent`、`squireChannel` 可读且行为不变，迁移成功后才写新绑定。混合 0.5.5.3/0.5.5.4 节点仍走旧单接收机路径。

## GUI、cell、link、thread 的约束

- GUI：当前 `gui/src/features/agent-chat/api/chat-controller.ts` 和 `shell/impl/gui/adapter/chat.py` 使用 `sessions.*`，只表示一个 GUI 到 agent 的会话。新增用户收件视图须经可信 daemon 方法读取，并明确来源、投递状态、重复折叠与跨设备游标；不能由浏览器自报 owner 或扫描各 daemon 的私有 inbox。PR #2351 仅更新生成 client 以反映 attach/detach，不证明 GUI 已支持 user 收件箱。
- Cell：用户消息的共享历史应挂 owner 范围 conversation cell；绑定资料和 ACL 与历史分开。cell 写入失败不能回滚已成功的外部投递，也不能导致重复执行。
- Link：现有 owner/user 目标不占 agent↔agent link 名额；绑定普通 agent 后也不应因为中转路径而把人的消息算成新的 agent↔agent link。agent 的回执及其内部协作仍按自身 link 规则。
- Thread：`threads.ids` 对 agent/user 和 agent/adapter 分别产生稳定 `t.` ID；绑定变化只改变执行路径，不重写既有对话身份。跨机读取靠 owner 授权，不能只凭 URI 里的 owner 字符串。

## 实施前的可验证判据

| 场景 | 期望 |
| --- | --- |
| 两台同 owner daemon 同时在线，重复发送同一业务键 | 一条逻辑用户消息；至多一次 native side effect；接收凭据可查 |
| 选定执行端点离线或 native 结果未知 | 不由第二台机器静默补发；明确 `queued`/`unknown` 与恢复路径 |
| receiver 在交付后崩溃，发送方重试 | 从持久 attempt/receipt 恢复；不产生第二条 DM |
| user-proxy、普通 agent、squire 别名及 adapter 绑定各自切换 | 旧 attempt 不跨渠道重复；新消息按绑定版本走唯一端点 |
| 非 owner daemon 伪报 `user:<owner>`，或 GUI 自报身份 | 拒绝读取/执行，不泄露正文；只用 daemon 已验证的 owner 身份 |
| 两设备 GUI、OrgFS 暂时不可用、旧版节点混跑 | 本地投递继续；历史同步状态可见；旧 API、帧和错误码保持兼容 |
| PAC 告警与普通互动同时进入用户视图 | 分类、thread、conversation 与通知策略各自正确，不误唤醒或丢正文 |

## 下一步

本 PR 只提交调查和裁定建议，不改产品行为。实施应拆成可独立评审的两段：先完成 S10 绑定解析与单接收路径、GUI 只读用户视图及契约测试；再由单独方案决定是否采用共享逻辑 inbox/复制和自动故障转移。目标分支 `release/v0.5.5.5` 尚未创建；按 phase B 的现行集成约定，WIP PR 先指向 `intg/cell-model-phase-a`，由集成线进入该 release，绝不直接推送 release 分支。
