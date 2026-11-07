# App 连接、多设备同步与 infra 接口提案

日期：2026-10-08。基线：官方 dev `53de6496adaa9000cbdf179cd97fcb71645aa3fa`。
状态：供 App、daemon/identity 与 infra-ops 对齐的设计提案，**不是已发布 API，也不是现有能力声明**。依据 #1311；其中引用的 `mobile-adapter-sync-brief-2026-10-08.md` 尚未取得，取得后须核对冲突。首轮实现只交付静态 Core 与注入端口；本文件不授权更改其他 owner 的服务。

## 权威与依赖

Core 聊天模块仅依赖 ConnectionPort；组合根按能力注入可选的本机管理 UI 和 PowerPackPort，两个业务模块不互相依赖。主机 daemon 保有消息、执行状态、授权与持久历史的权威；Core 仅持有显示缓存、尚未确定结果的发送标识和补拉位置。缓存清空不丢服务器已接受的聊天，也不使旧请求重新执行。

| 层 | 输入/职责 | 禁止承担 |
| --- | --- | --- |
| Core | 文本输入、会话选择、查询投影、展示结果；只调用具名操作 | 启动 daemon、加载 harness、保存签名密钥、创建业务会话权威 |
| 原生组合根 | 绑定节点/设备、受限 IPC、平台密钥存储、连接生命周期 | 接受网页提供的任意 URL、身份、shell 命令或文件路径 |
| Local IPC 连接 | 连接被显式选定的隔离/用户 daemon，验证进程端点归属，映射有限操作 | 用“本地”跳过用户和会话权限 |
| Tailnet 连接 | 用户态网络拨号、目标绑定、调用正式身份签名器和认证节点 API | 假定 WebView fetch 自动使用嵌入 tailnet；将网络成员资格当业务授权 |
| Power Pack | 本机 runtime/daemon/harness 的存在、状态与受控生命周期 | 移入 Core 必需依赖，或把停止 Agent 解释为取消一轮聊天 |
| 节点 App adapter | 每 Agent 的 App adapter 实例，统一路由、历史投影与设备补拉 | 从请求体的 principal/sender 获得信任 |

本地与远程连接返回相同的归一化数据和错误，不要求二者使用相同 wire protocol。Core 调用不得直接透传任意 daemon command；新增命令必须逐项映射其权限、参数、幂等性和验证证据。

## Core 内部连接公共面

首轮沿用 GUI 的 session port 词汇：`capabilities.get`、`sessions.list`、`sessions.history`、`sessions.events`、`sessions.send`。这是进程内接口命名，**不能据此声称 daemon 已有同名路由**。真实 adapter 负责映射已批准的节点 API。浏览器参数不得包含可信 principal、device owner 或 harness 进程句柄。

- 连接状态：disconnected → connecting → ready；失败进入 unavailable 或 authentication-required。连接代次变化使旧响应失效。
- 查询分页必须有界；历史页的 cursor 是服务器生成、不透明且绑定会话/身份/保留代次的值。Core 不从消息条数构造 cursor。
- 发送：会话、文本和 client message ID（GUI 兼容字段 `idempotencyKey`），不允许 sender/context 覆盖。受理只表示服务端持久接受，不表示 Agent 完成。
- 失败区分权限拒绝、连接不可用、结果未知、服务器拒绝。结果未知时保留原 ID 与原文本；再次提交仍用原 ID，修改内容需要新 ID。
- 不提供未实现的普通聊天 cancel。Power Pack 的进程停止是独立能力与明确操作。
- 会话切换、断开或新连接开始后，旧请求不得写入当前视图；原会话的未知发送仍可在重新选择后恢复查询。
- 原生层移除 Power Pack 时，Core 仍可启动并远程连接。App 默认不启用演示；任何测试演示都要醒目标记且不覆盖已有真实连接。

## 节点侧多设备语义（待 daemon owner 实施）

1. 一个 Agent 的 App adapter 实例可服务同一用户的多个已授权设备；每次操作独立核对当前设备和会话权限。adapter ID、业务 conversation ID 与本地 UI session ID 的映射由服务器提供。
2. 每会话维护单调事件序列和不可变事件 ID。事件包含消息 ID、来源 adapter/设备、时间、正文或状态变更；来源展示字段不参与授权。
3. 发送去重键至少包含认证用户、认证设备、conversation ID、client message ID；同键同载荷返回原受理结果，同键不同载荷拒绝。持久化去重记录与受理记录必须原子关联；重试不得因 UI 重启重新分配 ID。
4. history/event log 必须是独立的持久投影，而非查询 pending inbox 当历史。daemon 消费 ACK 仅表示实际消费成功；客户端补拉/展示不能代替该 ACK。
5. 每设备、每会话独立 `applied cursor`，只在页已成功应用到缓存后推进；若只有内存缓存，下次冷启动重新读取快照，不能沿用已丢失缓存的游标跳过历史。服务器设备检查点如有更新，必须单调且验证归属。
6. 分页响应包含记录、next cursor、has more；后续页失败不提前跨页推进。旧游标超出保留窗口时明确返回 reset-required，客户端重新获取快照，不静默跳过消息。
7. 去重窗口不得比允许客户端重试的窗口短。首版不做离线发送队列；超出去重保留期的未知发送先查询/人工明确重发，不自动当作新消息执行。具体保留天数待 owner 给容量和清理策略后冻结。
8. 并发多设备均可发送，节点决定顺序。补拉先后不改变发送因果；如需要回复关联，显式引用 parent message ID，不能依赖页面当前最后一条。

首版不宣称跨 harness 崩溃边界 exactly-once；应分别验证“API 受理去重”和“执行恢复是否可能重复”。两者不能共用一条 mock PASS。

## 已读与推送语义

首版只实现前台补拉，无跨端人已读。`delivered`（设备已拿到）、`applied`（缓存应用）、`read`（人在前台可见位置阅读）是三个不同事件。页面进入后台、收到推送或获取历史都不得标记人已读。未来 read 可按用户/会话聚合已读位置，但须保留每设备补拉位置，不能用全用户 read cursor 代替另一设备的同步 cursor。

推送仅是“可能有新内容”的唤醒提示，允许丢失、重复、延迟和乱序。前台恢复始终从已应用游标补拉，不能依赖推送数量、最后一次通知或通知正文重建聊天。第一版即便完全没有推送，也应在前台按需/有界轮询补拉。

## App 侧 infra 接口需求（供 infra-ops 确认）

以下为字段语义提案，不自定生产 endpoint/认证协议；infra-ops 负责中继和 DERP 的实现运营。

| 接口 | 最小数据/行为 | App 约束 |
| --- | --- | --- |
| 设备推送注册/刷新 | 版本、认证设备绑定、platform、provider、push token、App 环境/包身份、注册 ID 与失效时间 | token 由系统获取；注册/轮换需鉴权；不将 token 放 URL/普通日志；同设备换 token 撤销旧值 |
| 注销/撤销 | 按注册 ID 撤销；设备撤销同时使投递注册失效；操作幂等 | 退出登录主动撤销，但服务端不得依赖客户端必定在线 |
| 节点发布唤醒 | 使用节点被授权的 adapter/设备收件关联；限额、幂等去重 | 节点不得任意指定其他用户设备；具体节点到relay认证待 infra/identity 定义 |
| 唤醒载荷 | schema version、随机 notification ID、不透明 subscription ID、issued/expires 时间、固定 kind=`sync-hint` | 不含消息正文、用户姓名、组织名、密钥或可直接执行的命令；通知点击只触发受限同步 |
| DERP 地图获取 | map version、过期时间、官方 tailnet 客户端可消费的地图、可信来源验证、回退规则 | 通过固定受信任配置源/控制面下发；不能从聊天消息/推送接收任意地图 URL；不另造地图 schema |

注册接口如何复用统一身份、APNs/Android provider 选择、地图签发/撤销方式和保留限额均待 infra-ops 确认。App 只消费既定接口，不持有推送服务端密钥。推送不可用不会阻止前台聊天；地图更新失败不得静默降低节点请求鉴权。

## 身份依赖与阶段门

截至本次调查，#1290 将 N-A2 纳入 OrgFS 请求鉴权；#1293 提供请求密钥 derive 前置。这些尚不是本基线已合入的通用 App 聊天网关。应读取最终合入 SHA 和正式签名 canonicalization、nonce/时钟/撤销语义后，再编写手机端适配，不复制未冻结算法作为永久协议。

阶段门：正式设备注册/入组 → 受限 tailnet 拨号 → 合法请求通过 → 伪造身份/重放/撤销设备拒绝 → 持久聊天 send/reply → 两设备分页恢复。只完成纯 Core 单测时真实聊天状态仍是 BLOCKED。私有测试替身可验证注入边界，不能用永久静态 token 替代 N-A2 验收。

## 验收矩阵

| 场景 | 必须证明 |
| --- | --- |
| 无 Power Pack 启动 | 不读取 payload、不 spawn Node/Python/daemon，能展示连接入口 |
| 会话 A 请求晚于切换 B 返回 | 不污染 B；A 的未知发送 ID 未丢 |
| 受理后网络断开 | 同 ID 重试返回原结果，无新受理/重复派发 |
| 双设备独立分页 | A 推进不使 B 漏页；事件重放无重复气泡 |
| 节点重启 | 已受理消息、历史、去重记录仍可查 |
| 游标过期 | 明确 reset-required，完整重建快照 |
| 设备伪造/撤销/重放 | 请求拒绝且不产生消息或副作用 |
| 推送丢失/重复 | 前台补拉最终一致，不提前已读 |
| 本地管理与远程聊天并存 | 停止本地 Agent 不误断开远程 Core；不把进程停止冒充单轮取消 |

所有结果绑定精确 head、独立 home、平台、连接实现和真实/模拟范围。iOS、Android、Windows 分别验收，不能互相抵扣。
