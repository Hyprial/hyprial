# hyprial 内部协议规范(proto.md)

- 状态:**规范性文档**(normative)——不是描述现状的笔记,是剔除 IRC 之后系统唯一的锚点。含糊的句子会被当成许可,所以本文严格区分「规范」(MUST/SHOULD,RFC2119 语气)与「现状描述」(陈述句,标注"目前如此",不代表应当如此)。
- 范围:hyprial 的三个独立协议面——Zenoh 线协议、daemon IPC、交互式会话的 channel protocol——以及跨越三者的演进纪律。
- 拆分决定:**不拆分**,单文件。理由:`docs/design/design.md` 把命名裁定分散在第 68 行和第 123 行两处,同一天写下两条互相矛盾的记录,17 天里没人发现——这正是"多文档互相不参照"导致漂移的活案例(细节见 §5.4)。协议规范作为防漂移锚点,首要要求是"只有一处可查",拆分只会重演这个问题。
- 与 PR #264(`docs/reference/naming-audit-channel-adapter-route.md`)的关系:那份是**审计**(channel/adapter/route 三词在全仓的实际语义盘点),这份是**规范**(协议本身的字段级契约 + 演进规则)。两者共享部分代码引用(尤其 channel protocol 一节),但目的不同,请勿把本文当成审计的复述——凡是审计已经覆盖的内容(如"channel 一词有几种语义"),本文只在必要时交叉引用,不重复论证。

---

## 0. 为什么现在写这份文档

Allen 的原话逻辑链(直接记录,作为本文档存在的依据):

1. IRC 词汇表当年的目的是**防开发漂移**——用一套"确定且成熟的协议"当锚点,防止层级错配、随意添加关键词;
2. 不需要 IRC 互通(已裁定);
3. 剔除 IRC 影响的前置是:内部协议自己要有文档——**文档接替 IRC,成为新锚点**。

这意味着本文档的义务和 IRC 规范曾经承担的义务是同一件事:任何人往协议里加字段、加 schema、加子命令之前,先来这里查"这样加合不合规矩",而不是凭直觉抄旁边代码的写法。§5 是这份义务的核心,其余章节是它的事实基础。

---

## 1. 总览:三个独立的协议面

hyprial 目前有三个**互相独立、版本空间互不重叠**的协议面。这是代码核实过的事实,不是设计意图的转述——三者之间没有任何一处代码互相比较过对方的版本号(下文各节分别给出取证)。

| 协议面 | 载体 | 版本标识 | 覆盖范围 |
|---|---|---|---|
| Zenoh 线协议 | Zenoh key-value/pub-sub/queryable | 每个 schema 各自的 `/v1` 后缀(5 个独立家族,互不联动;第 6 个 `hyprial-resource-manifest/v1` 已随 IRC codec 于 2026-08-25 整体删除,见 §6) | 跨机器的消息投递、进度事件、状态查询、用户投递 |
| daemon IPC | 本机 Unix domain socket | 请求信封里的整数字面量 `"version": 1` | CLI/MCP ↔ daemon 的本机控制面(56 个 method) |
| channel protocol | daemon IPC 之上的 `session.register`/`session.heartbeat` 等 method | `CHANNEL_PROTOCOL_VERSION`(当前 `2`) | 交互式 Claude Code 会话的握手/心跳/租约 |

**MUST**:三者的版本字段永远不共用同一个名字或同一段比较代码。历史上把"channel"一词用在四个不相关的层上已经制造过一次真实的命名事故(PR #264 §1);版本号如果重演同样的模式(比如某天把 daemon IPC 的 `version` 和 channel protocol 的 `channelProtocolVersion` 合并成一个字段),会让"发现协议不兼容"这件事从"读一个整数就知道"退化成"读代码才知道"。

---

## 2. Zenoh 线协议

### 2.1 Key space

`src/hyprial/transport/keys.py`(163 行,`class KeySpace`)是 key 命名的唯一权威。**MUST**:任何新 key 前缀都经过这个类,不得在别处手写 `f"hyprial/v1/..."` 字符串。

- `prefix = "hyprial/v1"`(`keys.py:12`)——整个 key space 目前只有一个版本代。
- `encode_identity`/`decode_identity`(`keys.py:15-31`):identity 段若匹配 `^[A-Za-z0-9._:-]+$` 直接透传,否则做 URL-safe base64(去 padding)并加 `~` 前缀转义。**MUST**:任何写入 key 的 identity 字符串都先过 `encode_identity`,不得假设 identity 天然安全。
- 已声明的 key 族(逐一列出,均见 `keys.py`):

  | 方法 | Key 形状 | 用途 |
  |---|---|---|
  | `inbox(actor, message_id)` / `inbox_all(actor)` | `hyprial/v1/inbox/<actor>/<msgid>` | 投递信封落点 |
  | `receipt(sender, message_id)` / `receipt_all` / `receipt_any` | `hyprial/v1/receipt/...` | 收件回执 |
  | `fetch_receipt(...)` / `fetch_receipt_any()` | `hyprial/v1/fetch-receipt/...` | "已被拉取消费"的 queryable 证明 |
  | `custody(mailbox, message_id)` / `custody_all` | `hyprial/v1/custody/...` | mailbox 托管移交 |
  | `custody_receipt(...)` / `custody_receipt_any()` | `hyprial/v1/custody-receipt/...` | 托管签收回执 |
  | `system_notice(node, message_id)` / `system_notice_all` | `hyprial/v1/system-notice/...` | 无回执的告警(寄给发送方自己的 daemon) |
  | `progress(node, message_id)` / `progress_all` | `hyprial/v1/progress/...` | 无回执的进度事件(同上,寄给发送方自己的 daemon) |
  | `registry(actor)` | `hyprial/v1/registry/<actor>` | agent 实体注册 |
  | `actor_liveliness(actor)` | `hyprial/v1/liveliness/actor/<actor>` | zenoh liveliness token |
  | `mailbox_liveliness(node)` / `mailbox_liveliness_all()` | `hyprial/v1/liveliness/mailbox/...` | mailbox 角色的 liveliness token |
  | `user_delivery(owner, message_id)` / `user_delivery_any()` | `hyprial/v1/user/...` | 用户投递请求(§2.2 第 4 项) |
  | `user_receipt(sender, message_id)` / `user_receipt_any()` | `hyprial/v1/user-receipt/...` | 用户投递回执(§2.2 第 5 项) |
  | `org_context(node)` / `org_context_any()` | `hyprial/v1/org/context/...` | 节点已采信的 org-context 文档广播 |
  | `org_request(node)` / `org_request_any()` | `hyprial/v1/org/request/...` | org-context 的 queryable 拉取 |
  | `message_status_root(sender)` / `message_status(sender, message_id)` / `message_status_any()` | `hyprial/v1/msg/status/...` | 投递终态查询(§2.2 第 3 项) |

  `keys.py:141-144` 的注释是一条已成文的层级隔离规则,直接抄录:*"delivery terminal state (msg/ namespace)...Owned by the delivery line. The agent-entity line owns registry/; these two namespaces are disjoint by construction so the lines cannot collide."* **MUST**:新增 key 族之前,先确认它属于哪条"line"(delivery / agent-entity / org-context / liveliness...),放进对应的顶层前缀,不得因为"看起来都是消息"就塞进已有前缀下面。

### 2.2 五个 schema 家族——逐字段规范

**格式说明**:每个字段标 `file:line` 出处;"必选"指旧编码器不写会导致新解码器缺 key、"可选"指字段可以整体不出现(不是以 `null` 占位)。稳定性标签见每节末尾,含义见 §8。

#### ① `hyprial-delivery-frame/v1` —— 投递信封(canonical JSON)

- 定义/生产:`src/hyprial/inbox/zenoh_link.py`,`encode_delivery_frame()`(`:28-51`);消费:`decode_delivery_frame()`(`:54-71`)。
- schema 字符串是内联字面量(未提升为具名常量),出现在 `:31`(构造)和 `:56`(校验)。

| 字段 | 类型 | 必选? | 语义 | 出处 |
|---|---|---|---|---|
| `schema` | string | 必选 | 固定值 `"hyprial-delivery-frame/v1"`,不匹配即拒绝解码 | `zenoh_link.py:31,56-57` |
| `message_id` | string | 必选 | `InboxMessage.message_id` | `:36`,解码 `:60` |
| `conversation_id` | string | 必选 | 会话/对话标识 | `:37`,`:61` |
| `sender` | string | 必选 | 发送方 | `:38`,`:62` |
| `recipient` | string | 必选 | 接收方地址(任意 hyprial 地址 scheme) | `:39`,`:62` |
| `intent` | string | 必选 | 自由字符串;`"progress"` 是进度事件复用本信封时的取值(见 ②) | `:39`,`:62` |
| `lifecycle` | string | 必选 | `DeliveryLifecycle` StrEnum:`"online_only"` / `"durable_service"`(定义于 `src/hyprial/inbox/api.py:15-17`);未知值解码 raise `ValueError` | `zenoh_link.py:40`,`:64` |
| `idempotency_key` | string | 必选(键总是写出,值可能为空) | 幂等键 | `:41`,解码 `.get()` 默认 `None` |
| `created_at_ms` | integer | 必选 | 创建时间戳(毫秒) | `:42`,`:65` |
| `payload` | string(base64) | 必选 | **不透明字节**,见下方"payload 不透明性"证明 | `:40`(编码),`:63`(解码) |
| `origin_node` | string 或**整键缺省** | **可选,additive** | 见 §5.2 的演进范例 | `:41-47`,`:68-70` |

**payload 不透明性证明**:`zenoh_link.py:40` 做 `base64.b64encode(message.payload).decode("ascii")`,`:63` 做 `base64.b64decode(...)`。`InboxMessage.payload` 类型为 `bytes`(`inbox/api.py:26`),模块 docstring(`api.py:1-4`)原文:*"Callers hand this layer opaque encoded bytes."* 换句话说,**这一层的 payload 不是结构化 JSON**,它是任意字节;结构化内容(比如 ② 的 `ProgressEvent`)是上层协议自己在这段字节里塞了另一层 JSON,delivery-frame 本身不解释它。

**生产方/消费方全量清单**:
- 生产:`zenoh_link.py:108`(`deliver`)、`:125`(`transfer_custody`)、`:139`(`deliver_notice`)、`:157`(`deliver_progress`)。
- 消费:`zenoh_link.py:240`(`_on_inbox`)、`:248`(`_on_custody`)、`:252`(`_on_system_notice`)、`:255`(`_on_progress_event`)。

**编码约定**:`sort_keys=True`,`separators=(",", ":")`,`ensure_ascii` 未设置(默认 `True`)——`zenoh_link.py:49-50`。

**稳定性**:基础帧 2026-08-07(`5e349bbc`);`origin_node` additive 扩展 2026-08-19(`43fc4d75`)。**标签:STABLE**——已经扛过一次真实的 additive 扩展且没有破坏旧解码器。

#### ② `hyprial-progress-event/v1` —— 进度事件

- 定义:`src/hyprial/inbox/progress.py`,`PROGRESS_SCHEMA = "hyprial-progress-event/v1"`(具名常量,`:34`)。
- **重要分层事实**:这不是独立的 zenoh 信封,是塞进①`payload`字段里的结构化 JSON——一条进度事件在线上的真实字节序列是「delivery-frame 信封(base64 payload)→ base64 解出来是 progress-event 的 JSON」。生产/消费:`ProgressEvent.to_payload_dict`/`encode_progress_event`(`:143-177`,`:222-254`)与 `from_payload_dict`/`decode_progress_event`(`:179-219`,`:257-264`)。

| 字段(wire) | 类型 | 必选? | 语义 | 出处 |
|---|---|---|---|---|
| `schema` | string | 必选 | `PROGRESS_SCHEMA` | `:190` |
| `progressEvent` | bool | 必选 | 恒为 `True` 的自描述标记;docstring 明说"结构隔离(独立表、独立 keyspace)才是真正的防线,这个标记不是" | `:151`,`:192` |
| `message` | string | 必选 | **与 `summary` 冗余**——解码时若 `message != summary` 直接拒绝(`:194-198`)。存在原因:通用 payload 读取器(`runtime._message_text`、`message.pending.list`)只认 `body["message"]` | `:194-198` |
| `summary` | string | 必选 | 人类可读文本,`__post_init__` 强制 ≤200 字符(`SUMMARY_LIMIT`,`:110-113`) | — |
| `deliveryId` | string | 必选 | 非空 | — |
| `conversationId` | string | 必选 | 非空 | — |
| `actor` | string | 必选 | 非空 | — |
| `harness` | string | 必选 | 非空 | — |
| `phase` | string | 必选 | 自由字符串;`KNOWN_PHASES`(`:58-69`)只是一份已知值参考表,**解码不校验**(注释原文承认这是刻意的向前兼容,`:47-48`) | — |
| `seq` | int | 必选 | 非负;**跳号是正常的**(docstring 明说这是契约的一部分,`:12-14`) | — |
| `emittedAtMs` | int | 必选 | 正整数时间戳 | — |
| `droppedSinceSeq` | int | **可选**,零值不写出 | 解码缺省 `0` | `:167-168`,`.get(..., 0)` |
| `toolCallId` | string | **可选** | 仅非 `None` 时写出 | `:169-170` |
| `toolName` | string | **可选** | 同上 | `:171-172` |
| `detail` | object | **可选** | 仅非 `None` 时写出;超长截断为 `{"truncated": True}` | `:173-174`,`:233-249` |
| `terminal` | bool | **可选**,仅 `True` 时写出 | 解码 `record.get("terminal") is True` | `:175-176` |

**生产方/消费方**:生产 `InboxService.submit_progress_event`(`src/hyprial/inbox/service.py:1250-1281`,内部调用 `encode_progress_event` 于 `:1260`,再交给同机 `receive_progress_event` 或跨机 `deliver_progress`);消费 `InboxService.receive_progress_event`(`service.py:1284-1296+`)与 `daemon/application.py:2547`(`progress.list` method 的读取路径)。

**编码约定**:`separators=(",", ":")`,`ensure_ascii=False`,**`sort_keys` 未设置**(`progress.py:268`)——五个家族里唯一不排序 key 的一个,见 §9 发现的问题。

**稳定性**:**整个文件只有一次提交**——`f812b05`(2026-08-20,"hyprial-progress-event/v1 contract — codec, shape validation, consumer-rule golden tests"),写作本文档时(2026-08-24)刚好 4 天。**标签:EXPERIMENTAL**,见 §8 的明确说明——本文档故意不把它的字段表当成"已验证过 additive 演进能力"的稳定契约,只是"目前如此"的现状记录。

#### ③ `hyprial-delivery-status/v1` —— 投递状态帧

- 定义:`src/hyprial/inbox/pull.py`,`STATUS_FRAME_SCHEMA = "hyprial-delivery-status/v1"`(`:50`)。
- **不经过①**:这是 Zenoh queryable 的应答载荷,不走 `encode_delivery_frame`。生产:`DeliveryStatus.to_json`/`encode_status_frame`(`:94-105`,`:432-440`);消费:`from_json`/`decode_status_frame`(`:107-119`,`:443-447`)。

信封形状:`{"schema": ..., "holder": ..., "records": [...]}`。当前 endpoint 对合法查询即使没有记录也返回带 `holder` 的空信封；旧信封没有该字段仍可解码。

| 字段 | 类型 | 必选? | 语义 |
|---|---|---|---|
| `schema`(信封级) | string | 必选 | `"hyprial-delivery-status/v1"`,不匹配 `ValueError` |
| `holder`(信封级) | string | 新生产方必选；旧信封可缺 | 回答本次查询的业务 node id；让空 `records` 也能证明该节点已应答 |
| `records`(信封级) | array | 必选 | — |
| `messageId` | string | 必选 | — |
| `sender` | string | 必选 | — |
| `recipient` | string | 必选 | — |
| `state` | string | 必选 | `TerminalState` StrEnum:仅 `"fetched"`/`"expired"` 合法(`:63-67`),未知值 raise |
| `holder` | string | 必选 | 观测到终态的节点 |
| `reason` | string | 必选 | 自由字符串,参考 `HoldReason` StrEnum(`:70-77`)但解码不做枚举校验 |
| `recordedAtMs` | int | 必选 | — |
| `conversationId` | string | **可选**,默认 `""` | `.get(...) or ""` |
| `idempotencyKey` | string 或 null | **可选** | — |

**生产/消费**:生产 `DeliveryStatusEndpoint._answer`(`pull.py:558-570`,注册在 `keys.message_status_any()` 的 queryable 回调,`:555`);消费 `query_delivery_status()`(`:585-642`,逐条 `decode_status_frame`,`:624`)。

**编码约定**:`sort_keys=True`,`separators=(",", ":")`,`ensure_ascii` 默认 `True`。

**查询投影**:上述 record 与 daemon 对外查询 JSON 都只使用 `fetched` / `expired` 终态。recipient node 未应答、应答不可解码或 recipient URI 无法解析时，合并后的 `expired` 仍保持原 state/reason；`missingRecipientHolders`、`undecodableHolders` 与 responder/error 计数只存在于顶层 `diagnostics`。混合版本中，旧查询端仍可能执行自己的旧投影，直到该端升级。

**收件方查询自己收到的消息**(2026-09-25):调用方是某条消息的**收件方**、且本地没有按发送方命中的记录时,daemon 用本地 `delivery_status` 中 `message_id + recipient` 匹配的那条记录作答,**不再向 mesh 广播** `msg/status` 查询。此前收件方(例如 ack 不可用时做结算核对的 carrier)用自己的身份查入站消息,按发送方键永远查不到,只能反复向所有 holder 广播并得到空结果。第三方查询不受影响(不会读到别人的收件记录),发送方查询路径不变。

**稳定性**:2026-08-14 引入(`d9d1671`),之后有多次跟进提交(`b48fa59`/`f09ffb4`/`85eedd3`/`f4f4787`)——比 progress-event 经历过更多真实迭代。**标签:MODERATELY STABLE**。

#### ④ `hyprial-user-delivery/v1` —— 用户投递请求

- 定义:`src/hyprial/squire/addressing.py`,schema 字符串内联(`:306` 构造,`:315` 校验),未提升为具名常量。
- 构造方式与①③不同:`{"schema": ..., **asdict(request)}`(`:306`)——**直接 spread 整个 dataclass**,字段名是 dataclass 的 snake_case 原名,不转 camelCase。

| 字段 | 类型 | 必选? | 语义 |
|---|---|---|---|
| `schema` | string | 必选 | `"hyprial-user-delivery/v1"` |
| `message_id` | string | 必选,非空 | `_string()` 校验(`:427-430`) |
| `idempotency_key` | string | 必选,非空 | 同上 |
| `owner` | string | 必选,非空 | 接收用户的 owner id |
| `sender` | string | 必选,非空 | — |
| `message` | string | 必选,非空 | 纯文本正文(不是嵌套 content 对象) |
| `conversation_id` | string | 必选,非空 | — |
| `attempt_id` | string 或 null | **可选** | 由传输层在发送时填入(`ZenohUserDeliveryTransport.deliver`,`:407-408`),用于关联异步回执查询,不是调用方原始意图的一部分 |

**生产/消费**:生产 `ZenohUserDeliveryTransport.deliver`(`:406-412`,`session.put`);消费 `ZenohUserDeliveryEndpoint._on_delivery`(`:372-378`,订阅 `keys.user_delivery_any()`)。

**编码约定**:`ensure_ascii=False`,`separators=(",", ":")`,`sort_keys=True`(`:307-309`)。

**稳定性**:2026-08-07 引入(`d9af3b9`),一次跟进(`096d692`)。**标签:STABLE**。

#### ⑤ `hyprial-user-delivery-result/v1` —— 用户投递结果

- 同文件 `addressing.py`,schema 字符串内联(`:330`,`:339`)。构造同样是 `{"schema": ..., **asdict(result)}`(`:330`)。

| 字段 | 类型 | 必选? | 语义 |
|---|---|---|---|
| `schema` | string | 必选 | `"hyprial-user-delivery-result/v1"` |
| `message_id` | string | 必选,非空 | — |
| `accepted` | bool | **解码宽松**:缺失 key 视为 `False`,不报错 | `.get("accepted") is True` |
| `native_message_id` | string 或 null | **可选** | 成功时对端(如飞书)自己的原生 message id |
| `duplicate` | bool | 同 `accepted`,缺失视为 `False` | — |
| `code` | string 或 null | **可选** | 机器可读失败码,如 `TARGET_SQUIRE_UNCONFIGURED`/`TARGET_SQUIRE_ADAPTER_UNAVAILABLE`(`addressing.py:238,248,265,280,422`) |
| `message` | string 或 null | **可选** | 人类可读失败文案(中文 UX 字符串,`:19-27`) |
| `definitely_not_sent` | bool | **可选**,缺失视为 `False` | 只有确认请求未到原生发送边界时为 `True`; `accepted=False` 或回执超时本身不能证明未发送 |

**⚠️ 命名陷阱**:`UserDeliveryLedger`(`addressing.py:109-197`)把 `UserDeliveryResult` 落盘到本地 JSON 文件,用的是**另一套完全不同的 envelope**——`{"schemaVersion": 1, "deliveries": {...}}`(`:171-178`)。**这不是 `hyprial-user-delivery-result/v1`**,是同一份数据的本地持久化格式,巧合地也叫"schema"但版本号语义不同(`schemaVersion` 是纯整数,不是 `"hyprial-.../v1"` 字符串族的一员)。**MUST**:写代码/写文档时明确区分"线上 wire schema"和"本地落盘 schema",不要因为都叫 schema 就假设是同一个东西——这本身就是一个小号的"随意用词导致混淆"案例,记录在此作为反面教材,不在本轮处理。

**重试确定性**（全 actor 改造）：`USER_DELIVERY_TIMEOUT`、旧版本缺失的确定性字段和原生 SDK 异常均保持 `definitely_not_sent=False`。Watchdog 对这些未确认结果沿用原幂等键；只有明确的发送前拒绝才可以创建新的尝试标识。 新告警批次使用独立、先持久化的业务标识；同一批次的重试和进程重启沿用该标识。升级前尚未完成的旧 outbox 保留原键，不因升级重新命名。该字段同样保存在本地 ledger 中，旧记录缺失时按未知处理。原生发送成功后，actor 保留原生结果与该幂等键的执行归属，写盘失败只重试 ledger 提交，完成提交前不发布成功回执。进程内保留不等于跨进程崩溃后的原生 exactly-once 证明；该情形仍依赖通道的幂等语义。

**生产/消费**:生产 `ZenohUserDeliveryEndpoint._receipt`(queryable 回调,`:380-385`);消费 `ZenohUserDeliveryTransport.deliver` 内部轮询(`:413-418`,`receipt_timeout` 默认 3.0s)。

**编码约定**:与④相同(`ensure_ascii=False`,`separators=(",", ":")`,`sort_keys=True`,`:331-333`)。

**稳定性**:与④同一 commit 谱系。**标签:STABLE**。

#### ⑥ `hyprial-resource-manifest/v1` —— **已整体删除(2026-08-25,PAC NAMT3 664a7d314947)**

原定义于 `src/hyprial/proto/mapping.py`(`_MANIFEST_SCHEMA`),生产/消费均内嵌在 IRC 信封编解码函数 `envelope_to_lines()`/`lines_to_envelope()` 里。§6 记录了删除前的核实过程与结论(零生产调用方)和具体执行内容——本节不再重复字段表,历史存档请查 git 历史(该文件已随 `src/hyprial/proto/` 整包删除,`git log --diff-filter=D -- src/hyprial/proto/mapping.py` 可取回)。**标签:REMOVED**(不再是"当前协议"的一部分,也不再是"待剔除"的一部分——已经剔除完毕)。

### 2.3 Canonical JSON 编码约定

**规范(MUST,适用于新 schema)**:线上任何结构化 JSON 载荷,一律 `json.dumps(obj, sort_keys=True, separators=(",", ":"))`。理由:sort_keys 保证同一逻辑对象在任意生产者上产出逐字节相同的序列化结果——这是任何"对 payload 做哈希/签名/去重比较原始字节"的消费者能工作的前提(五个家族里,①的 `message_id`+`idempotency_key` 去重、⑤的落盘持久化都隐含依赖这一点);`separators=(",", ":")` 去掉默认的空格,减小体积、避免空白差异造成字节不一致。

**现状(不是规范,是本轮审计发现的事实)**:现存五个家族中,**②`hyprial-progress-event/v1` 是唯一不设置 `sort_keys` 的一个**(`progress.py:268`,默认按插入顺序),且 `ensure_ascii` 的取值在五者之间也不统一(①③省略即默认 `True`;②④⑤显式 `False`)。这是一个真实的不一致,**收进 §9「发现的问题」,本轮不修**——它目前之所以没有可观测后果,是因为没有任何消费者对②的 payload 做跨副本字节比较;但只要有人未来加一个基于哈希的进度事件去重(参照①的模式),这个不一致就会从"没影响"变成"隐性 bug"。

### 2.4 payload 不透明性约定(跨家族总规则)

**MUST**:「传输层」(①`hyprial-delivery-frame/v1`)只承诺搬运不透明字节,不解释 `payload` 内容。任何新的"结构化消息类型"(参照②的做法)都应该在**自己的模块**里定义 schema、编解码函数,把结果作为 `bytes` 交给①的 `payload` 字段,而不是在①里新增专用字段去表达"这是一条什么类型的消息"。①目前唯一用于区分载荷类型的字段是 `intent`(自由字符串,如 `"progress"`),**这是故意留的一个弱耦合口子,不是强类型的 discriminator**——新增消息类型时复用这个模式(约定一个新的 `intent` 值 + 自己的内层 schema),不要往①的信封本身加类型专属字段。

---

## 3. Daemon IPC 协议

PAC workflow delegation uses the separately versioned
`hyprial/v1/pac-workflow` request/reply namespace documented in
`contract/pac/remote/README.md`. Expansion v1 is an additive signed feature on
that protocol: the durable outcome carries authored text, canonical digest,
and attempt number, while the generic transport continues to treat the JSON
frame as opaque bytes. The full encoded request remains capped at 256 KiB.

自述位置:`src/hyprial/cli.py:525`,原文"the daemon's version-1 newline-delimited JSON IPC protocol"。三套独立客户端实现共享同一套线格式:`cli.py:522`(`_daemon_request`,同步 CLI 客户端)、`mcp/unix.py:17`(`UnixDaemonConnection._request_sync`,MCP 门面客户端)、`daemon/application.py:1707-1801`(服务端)。

### 3.1 传输

- **Unix domain stream socket**(`AF_UNIX`/`SOCK_STREAM`),服务端绑定于 `application.py:1316`,客户端连接于 `cli.py:492`。**MUST**:不得引入 TCP 回环端口作为这个协议的替代传输——当前实现里没有,不要以"方便远程调试"为由加。
- socket 文件路径:`<state_dir>/daemon.sock`。客户端发现顺序(`cli.py:365-389`):`HARNESS_STATE_DIR`/`HYPRIAL_HOME` 若设置,始终用 `<state_dir>/daemon.sock`(隔离边界优先于任何 override);否则看 `HARNESS_SOCKET_PATH`;否则回退 `~/.hyprial/state/daemon.sock`。
- 权限:父目录 `mode=0o700`(`:1314`),bind 后 `chmod 0o600`(`:1319`)——**MUST** 保持 owner-only,这是本机信任边界的唯一防线(参照 §4 channel protocol 同样依赖"同 OS 用户"边界,注释原文见 §4.4)。
- 并发上限:`_IPC_MAX_CLIENTS = 64`,超限连接直接关闭并记 `daemon.ipc.client_capacity_exhausted`。事件带 `clientCount`、`clientLimit`、`saturated` 与 `activeClients`;每个活动行带 `peerPid`、`connectedSinceMs`、`lastRequestMs`、`currentMethod`,事件顶层同名四字段描述被拒 peer。`ps.daemon.ipcClients` 以 `active`、`limit`、`saturated` 暴露同一实时占用。
- client slot 只归 transport,不延伸到 domain settlement。完整请求在独立 `hyprial-daemon-ipc-request` 线程结算;transport worker 每 100ms 观察结果及 Unix peer hang-up。peer 关闭时立即释放 slot 而不发响应;90 秒 fallback 到期时回既有 `IPC_TIMEOUT` typed error 后释放 slot。两种情况都不取消已 admission 的 domain mutation,后者继续结算并仍计入 `ps.daemon.ipcRequests.active`。完整契约见 `contract/daemon-lifecycle/ipc-client-slots.md`。
- **单发连接,非多路复用**:每次逻辑 RPC 都是"新开 socket → 发一帧 → 收一帧 → 关闭",两端都不循环读第二帧(客户端 `finally: client.close()`,`cli.py:600-601`;服务端 `_serve_connection` 处理完第一帧就 `return`,`application.py:1761`)。`cli.py:577-579` 有一段关于"event frame"的注释,读起来像是为服务端主动推送预留的设计,但**当前 `application.py` 没有任何代码路径会在一个连接上发第二帧**——记为"注释意图与当前实现不符",不代表未来不会做,只是现状如此。

### 3.2 帧格式

**MUST**(现状即规范,不建议偏离):一个 JSON 对象,`\n` 结尾,无长度前缀。

- 客户端写:`client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")`(`cli.py:547`,`mcp/unix.py:46` 同构)。
- 服务端写:handler 只调用一次,以 `json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"` 序列化一次。响应写入有独立的 **15 秒绝对预算**,从序列化之后开始,部分进度不会重置;这不是 receive idle 预算,也不是整个 RPC/handler 的总上限。Unix 按 `send` 已确认字节数推进,Windows 在同一 OVERLAPPED 上轮询结算;100ms 轮询只用于观察强制关闭,不得在超时后重发整帧。停止标记和 admission closing 只拒绝新请求;已接纳请求(含 shutdown 自身)仍可在既有 0.1 秒 grace 内完成响应,grace 耗尽后才触发写入取消,既有 2 秒 join 上限不变。Windows 取消未结算时继续保有 operation/event/buffer,关闭报告未完成,不假定取消已经生效。**注意**:请求侧 `ensure_ascii` 默认 `True`,响应侧显式 `False`,线格式保持不变。
- 读循环:两端都是"累积 `bytearray`,按 `b"\n"` 切,取第一个完整帧"。**8 MiB 硬上限**:客户端 `cli.py:553`(超限 `CliError("IPC_RESPONSE_TOO_LARGE", ...)`,`:592`),服务端 `application.py:1762-1764`(`DaemonRequestError("IPC_REQUEST_TOO_LARGE", ...)`)。**不一致**:`mcp/unix.py` 的读循环(`:47-52`)**没有**这个上限,只依赖 `self.timeout` 和 OS 级背压——由于服务端本身强制 8 MiB,实际不会发生响应超限,但这是三套客户端实现里唯一的一处不对称,记入 §9,本轮不改。

### 3.3 请求形状

```
{"version": 1, "id": "<uuid4>", "method": "<dotted.method.name>", "params": {...}}
```

- `cli.py:527-530`:`request_id = str(uuid4())`;`params` 为 `None` 时**整键省略**,不写 `null`。
- 服务端解析入口是 `_response()`(`application.py:1766-1800`):`json.loads` 后必须是 `dict`,否则 `INVALID_REQUEST`;`version != 1` → `VERSION_MISMATCH`;`method` 经 `_required_string` 校验非空字符串;`params` 缺省为 `{}`,类型必须是 `dict` 否则 `INVALID_ARGUMENT`。
- **特殊注入**:若 `method == "message.send"` 且请求带非空字符串 `id` 且 `params` 里没有 `idempotencyKey`,服务端会把 `request_id` 塞进 `params["idempotencyKey"]`(`:1783-1789`)——请求信封的关联 id 在这一个 method 上兼任幂等键。**这是当前唯一一处"IPC 层字段被悄悄挪用成业务字段"的地方**,新 method 如果也想要这个行为,应显式复用这个模式并在这里补一行注释说明,不要在别处发明新的隐式注入。

### 3.4 响应形状

- 成功:`{"version": 1, "id": <request_id>, "result": <any>}`(`application.py:1791`)。
- 失败:`{"version": 1, "id": <request_id-or-null>, "error": {"code": "...", "message": "...", "data": <可选>}}`(`:1794-1800`)。
- `DaemonRequestError`(`application.py:170-174`):`RuntimeError` 子类,携带 `code: str` 和 `data: Any | None`。**捕获边界是通用 `except Exception`**(`:1794`),但显式放过 `(NameError, ImportError)` 不转成客户端错误(意图:程序员的编程错误应该炸得响亮,不该被悄悄包成一个"看似合法"的错误码返回给客户端)——**MUST**:新代码不要为了"让 IPC 层显得健壮"而收窄这个例外列表,程序员错误必须继续崩溃。
- 客户端对称解析:`cli.py:580-586`(`CliError`,`:352-358`,字段形状与 `DaemonRequestError` 一致)。

### 3.5 method 清单

`handle(self, method: str, params: JsonObject) -> Any` 是唯一的分发入口,由显式 method 分支和 `agent.*` 二级分发组成。具体数量由代码与静态门禁给出,本文不复制一个会随业务增删而漂移的计数。核心/数据面(挑重点列出,不逐一给参数表——那超出本文档当前这一轮的范围):

- **投递/会话核心**:`message.send`、`message.reply`、`message.status`、`message.ack`、`message.pending.list`、`message.pending.wait`、`outbox.list`、`outbox.prune`、`session.register`、`session.refresh`、`session.heartbeat`、`session.unregister`(后四个即 §4 channel protocol 的载体)。
- **发现/状态**:`ping`、`ps`、`targets`、`hosts`、`top.snapshot`、`identity.whoami`。`ping` 是阶段①的轻量就绪探测(只回进程内已有字段,不碰 desired state/actor/lark);restore 完成前它是唯一随时放行的读方法——重型方法在闸门未开时立即收到 `DAEMON_RESTORING`,不在 listen backlog 里排队(`shutdown` 同属放行集,保证恢复卡死时仍可停)。
- **外围管理**:`org.publish`/`org.fetch`、`routine.*`、`workflow.*`、`progress.list`、`lifecycle.start`、`down`、`transfer.*`、`adapter.*`、`agent.*`(二级分发)、`upgrade.steps.status`/`upgrade.steps.run`(daemon upgrade/doctor --fix 的共享步骤面)、`shutdown`。已确认无状态且无调用方的 IRC `channel.list/join/part` 空壳随 codec 一并删除。

**一处值得记录的显式兼容性决策**:`lifecycle.start` 的注释(`application.py:3013-3014`)说明其参数键在 wire 上刻意保留 `"provider"` 这个名字,为的是让新旧 CLI/daemon 组合在双向都能兼容——**这是当前代码里唯一一处显式为"跨版本兼容"写注释的地方**,应该被当成正例:字段名一旦上线,即使内部换了叫法(比如从 provider 改叫 harness),wire 上的 key 名不跟着动,除非走 §5 的破坏性变更流程。

### 3.6 版本协商

**现状(不是规范,是诚实的现状描述)**:`version` 是双端各自硬编码的字面量 `1`,**没有握手、没有能力协商、没有版本范围**——它只是逐请求比较"是不是恰好等于 1"。今天任何两个 hyprial 构建之间都不会触发 `VERSION_MISMATCH`(因为两端永远都写死 `1`);真正会在新旧构建之间发生的漂移,是 method 名 / 参数形状不匹配——这种情况**不会**被 `version` 字段挡住,只会在调用时表现成 `METHOD_NOT_FOUND` 或 `INVALID_ARGUMENT`,客户端没有任何提前查询能力表的手段。**这是一个已知的、本轮不修的协议短板**,列入 §9;如果未来真的要支持"CLI 和 daemon 版本可以不同"这件事(比如滚动升级期间),`version` 这个字段目前不够用,需要专门设计。

### 3.7 错误码分类

`OWNER_MIGRATION_HOSTED_CONFLICT`：别名迁移的 old/new 与托管 agent owner
碰撞时写前拒绝。daemon 启动日志、启动器及 login preview 保留同一码；具名数据
限于 `old`、`new`、`actor`、`hostedOwner`，恢复方式见运维指南的宿主改名说明。

**集中注册表:`src/hyprial/contracts/ipc_errors.py`**(本节初版记录的"没有注册表"现状已按 §9 第 4 条修复)。proto 面错误码——daemon 在 IPC error 响应里发出的 code、CLI/MCP 客户端在同一 `.code` 通道上铸的传输层 code(`DAEMON_UNAVAILABLE`/`IPC_TIMEOUT`/`DAEMON_START_FAILED`/`DAEMON_START_TIMEOUT`)、以及经 `DaemonRequestError` 透传上线的内层 code——每个一条具名常量(引入时 46 个),值一经发布即冻结:旧节点继续发同样的字符串,新接收端拿常量比较天然识别所有历史对端;改值是协议破坏,只能加新码并继续识别旧码。发送端与接收端 import 同一常量(`SESSION_SUPERSEDED_CODE` 惯例的全面化;该名字保留在 `mcp/api.py`,值指向注册表)。**入表判定线**:code 字符串离开铸造进程、进入另一进程的**机器可读判定面**才入表 —— daemon IPC `error.code`(铸或透传)、zenoh 跨节点载荷(如 `TARGET_SQUIRE_*`)、或被另一进程解析并分支的持久化 JSON 字段(如启动日志里的 `HYPRIAL_HOME_IN_USE`,CLI 对它做分支)。**client-side-only 码不入表**:仅在本进程渲染给人 / 落 `--json` 输出、无任何他进程分支的 `CliError` 码(`INVALID_RESPONSE` 之类)是展示不是协议;Lark 平台码与纯进程内异常码同理。边界裁定:`UPGRADE_RESTART_FAILED`/`UPGRADE_NOTIFICATION_FAILED` 落 autoupdate last-run JSON 并被 daemon `autoupdate.status` 原样投影展示,但无进程对字符串分支 —— 展示性透传,暂不入表;一旦出现跨进程分支即按判定线入表。**防再生守卫**:`tests/test_ipc_error_code_registry.py` 冻结注册表成员,并用 AST 扫描保证 `src/hyprial` 下任何文件都不得把注册码写成字符串字面量——绕过注册表直接敲字面量会当场红。历史背景(修复前):`application.py` 108 处构造点、92 处字面量、40 种 code,仅 `SESSION_SUPERSEDED_CODE` 一个具名常量;约 18 处透传内层 `.code` 维持原状。

`CODEX_CREDENTIAL_MISSING` 是 interactive Codex 的启动前拒绝：Agent-home P2 的原生 OpenAI credential 缺失，或不是非空、0600 的普通文件时，CLI 在 app-server / TUI 启动前返回该码。机器可读 `data` 包含 `reason`、`path` 与 `remediation`；`reason` 按现场为 `missing`、`empty`、`permissions`、`not-regular`、`shared-target`（检查路径本身失败时为 `inaccessible`）。`remediation.kind=command` 时 `command` 可直接执行；`kind=manual` 时只给 `instruction`，明确说明没有可安全修复该现场的 hyprial CLI 命令。普通缺失用 provider login，空文件须加 `--force`，权限错误只需 `chmod 600`；非普通文件与配置的外部 shared target 不再推荐会被拒绝或写错目标的 provider login。输出只含路径与诊断，不读取或输出 credential 内容。

`message.send` 在 actor mailbox 接纳后仍没有 durable receipt 时，按 §3.7.1 返回 `SUBMIT_OUTCOME_UNKNOWN` error/code/data；CLI 与 MCP 保留稳定 messageId 和 outcomeKnown:false。不能将 mailbox admission 当作 durable accepted 收据，也不能据此认定未送达或换键重发。
`SENDER_NOT_AUTHENTICATED` 表示消息方法收到一个 `agent:` 身份声明，但 IPC
请求没有携带 `sessionRef`（仅 `enforce` 模式拒绝；`observe` 只记录）。携带了
`sessionRef` 却不属于该 actor 时，沿用 session 围栏原有的 `SESSION_SUPERSEDED`
/ `STALE_SESSION`，两种模式都拒绝，错误文本不含当前持有者的 sessionRef。
两者都在任何消息创建、读取、回复或 ack 之前返回；错误 data 的
`claimedIdentity` 是被拒绝的身份。`user:<owner>` 本机 operator、adapter/channel
入口和 daemon 内部入口不使用这个错误形状，完整规则见
`contract/wire/message-sender-auth.md`。

---

OrgFS URI 校验经 daemon IPC 原样返回两个冻结码：`ORGFS_INVALID_URI` 的 wire
值为 `invalid-uri`（URI 格式无效），`ORGFS_CROSS_SPACE_URI` 的 wire 值为
`cross-space-uri`（owner 或 space 与请求上下文不匹配）。小写拼写保持兼容；
它们与 `ORGFS_CONTENT_PENDING = content-pending` 一样注册于中央错误目录，
不能只登记在进程内的 `ORGFS_ERROR_CODES` 中。校验拒绝发生在节点变更前。

组织目录使用 Casdoor group 的完整名字：`group-NAME` 既是 `org.*` 参数也是
目录 space id；配套 ACL space id 是 `group-NAME-acl`。`org.create` 的 IPC
结果分别以 `spaceId` / `aclSpaceId` 返回二者，邀请 payload 也携带二者，以便
加入方同时 join 两个副本。ACL space 的 `policy/role-grants.json` 是角色到权限
映射的执行权威；缺失、不可读或结构无效时权限检查 fail closed。

ACL 的权威身份不是可重复、可变更的 space 名字。创建时 daemon 把实际
`aclSpaceId` 写入目录 space 的 owner-only OrgFS meta 文档，`directory/org.json`
只保留生命周期状态；此后策略读取、
成员同步与重复创建都只按该 ID 解析，并校验 ACL owner 与目录 owner 相同。
缺少绑定、未知 ID、owner 不匹配或目录名存在多个候选时都视为策略不可用，
记录 `org.policy.grants-unavailable` 后拒绝。成员对 `directory/org.json` 写入同名
`aclSpaceId` 字段不会改变策略来源；同名 ACL space 也不能替换绑定目标。
旧 `org-NAME` space 不自动迁移。`org.create` 的多步提交由本机
`state/org/create-intents.json` 记录 `org/nodeId/owner` 和已建的 directory/ACL
space ID；directory space 建立后、ACL space 建立前先写 `state=creating`，全部步骤（含设备
发布尝试）结束后才写 `state=active`。客户端超时或断开不取消 daemon handler，同一创建者
同名重试会恢复，其他 owner 仍以 `ORG_ACTION_NOT_AUTHORIZED` 拒绝。只有显式
`state=creating` 或 `state=partial` 表示创建未完成。生命周期文档缺失、缺少 `state` 或 JSON
无法解析时，均按旧组织分类：有 `aclSpaceId` 绑定报告 `legacy-active`，无绑定报告 `partial`。
无法识别的显式状态按 `partial` 拒绝，并报告原值。`org.create` 结果同时返回 `published`
和兼容字段 `devicePublished`；发布失败时两者为 false，
CLI 输出 `ok:false` 并退出 1。`org.repair {org?, apply?}` 是不完整
创建检查面，覆盖失效 ACL 绑定，以及缺失的 grant / directory 生命周期文档；
`apply` 缺省 false；返回 `apply`、`candidates`（`org/spaceId/reason/repairable`）和
`repaired`。只有显式 `apply:true` 才恢复当前 owner 且 intent 属于当前 node 的
candidate，不删除 space。
完整契约见 `contract/org-create-recovery.md`。

无参数 `org.network` 对每个 org 独立授权；不可用的 org 保留在 `orgs` 与
`directory`（目录行为空），并追加到 `unavailableOrgs`。`reason` 按生命周期与权限分别为
`creating`、`partial` 或 `policy-denied`，不把半成品误报成缺 ACL grant；`missing-acl-binding`
只用于 `org.repair` 候选，不作为 network 不可用原因。JSON 无法解析或状态值无法识别时，
`dataProblems` 报告问题，对象形状固定为 `{org, path, reason, value?}`：`path` 为
`directory/org.json`，`reason` 为 `invalid-json` 或 `unknown-state`，`value` 仅在
`unknown-state` 时携带原状态值。分类顺序：无法解析 JSON → 按 `space_meta.aclSpaceId`
绑定走 legacy；可解析但无/空 `state` → 同一 legacy 分类；未知显式状态 → `partial`
并把原值写入 `dataProblems`；显式 `creating`/`partial` 拒绝；显式 `deleted` 保持
dev 行为。daemon 写 warn 事件，`hyprial doctor` 显示 `org-metadata` 警告；
其他健康 org 仍正常返回。指定单个 org 时仍 fail closed 并返回授权错误，错误消息包含半成品状态。
三个状态 pane（`directory`、`mappedPeers`、`sidecar`）独立读取本地 projection，
各自受 1 秒 deadline 约束；`paneStatus` 逐 pane 返回 `available`，不可用时另带
`reason`（`timeout`、`capacity`、`not-configured` 或 `error`）。一个 pane 超时或
失败不再使整个 `org.network` 超时；其他 pane 仍正常返回。`directory` 只按本地
OrgFS 已记录设备生成，sidecar presence 波动不得删减目录行；presence 独立放在每个
设备的 `presence` 字段，没有独立本地投影时为 `unknown`。读取不会触发 sidecar
刷新、mesh query、org join 或 OrgFS sync。完整机器契约见
`contract/org-network/README.md`。

`org.delete` 是唯一修复例外：当 `aclSpaceId` 缺失、指向未知 space，或绑定 ACL
owner 与目录 owner 不符时，复用 OrgFS 的 space-owner author 检查，仅允许目录
space owner 删除，并记录 `org.delete.unbound-owner-fallback` 及原始原因。有效绑定
仍完全由 ACL policy 决定；策略文档缺失/无效、目录歧义或绑定不可读都没有例外。
此例外不消除 G4（OrgFS author 当前为自声明）或本地 `via=operator` 尚未证明的问题。

ACL space 的外部变更一律在共享 `orgfs.*` IPC bridge 以
`ORGFS_ACL_SPACE_EXTERNAL_MUTATION` 拒绝；唯一例外是 **owner 节点上的内容写入
入口闸**（见下）。创建、邀请、加入、成员移除、purge/serve/unban/checkout 等
生命周期与成员变更在任何节点都保持该拒绝，只由 daemon 内部 `org.*` 生命周期
路径执行。

**ACL 入口闸（owner 节点）**：当 ACL space 的 owner 就是本 daemon 的 owner 时，
内容写方法（`orgfs.write` / `orgfs.import` / `orgfs.mkdir` / `orgfs.move` /
`orgfs.remove` / `orgfs.restore`）改为经入口闸判定，复用 #1166 的已认证调用方：

- 放行：本机 operator（`user:<owner>`，无 `onBehalfOf`）；owner 的 user-proxy
  —— 按 daemon 自己在 worker 启动时登记的 harness 种类（launch spec）认定，
  session 自报的 `runtime` 字符串（如 `user-proxy_interactive`）不参与判定；
  以及 `org.acl.grant` 显式授权且未过期的 agent。
- 其余已认证 agent 会话、adapter 入口与未认证调用一律
  `ORG_ACL_WRITE_DENIED`（`data` 含 `method`、`org`、`reason`，被拒 agent 另含
  `caller`）。非 owner 节点不设入口闸，所有变更仍是
  `ORGFS_ACL_SPACE_EXTERNAL_MUTATION`。

**`org.acl.*`（owner 节点本地授权）**：`org.acl.grant {org, agent, untilMs?}`、
`org.acl.revoke {org, agent}`、`org.acl.ls {org}`，CLI 为
`hyprial org acl grant <org> <agent> [--until <ISO 8601>]` / `revoke` / `ls
[--json]`。三者都只接受 owner 终端或 owner 的 user-proxy 调用，否则
`ORG_ACL_GRANT_FORBIDDEN`；在非 owner 节点调用同码拒绝（`reason:
"not-owner-node"`）。`grant` 的 agent 只能是本节点 owner 名下、daemon 登记在册
的 agent（短名经本地 alias 表解析，或本节点的 canonical agent URI），否则
`ORG_ACL_GRANT_NOT_OWNED`。授权存 owner 节点本地
`state/org/org-acl-grants.json`（org 状态，非 tailcat 状态），不进 orgfs、不同步；`untilMs` 到期后的条目
在读取时即失效。`grant` 返回 `{org, agent, grantedAtMs, untilMs}`，`revoke`
返回 `{org, agent, revoked}`，`ls` 返回 `{org, grants: [{agent, grantedAtMs,
untilMs}]}`。

### 3.7.1 未确认的 Inbox submit 与 Lark 请求 custody

**补充错误契约：`SUBMIT_OUTCOME_UNKNOWN`（Contract-Impact: updated）。**
Inbox submit 命令已获 coordinator mailbox 接纳、但同步等待超时且没有可核验的
durable submission receipt 时，daemon 返回该 error code，而不是宣称投递失败。
若相同 correlation/message ID 的回执已经落盘，返回回执的真实结果，包括明确拒绝；
不把已接纳、超时或无回执本身转换成成功。原有 2 秒等待预算不变。

`error.data` 包含 `commandAdmission: "accepted"`、`outcomeKnown: false`、
`messageId`、`submissionCorrelationId`、`idempotencyKey`。commandAdmission
只指内存 mailbox 接纳，**不是 durable commit、对端接收或执行证明**。
这里的 idempotencyKey 是 Inbox 的每目标键（可能带 target/index 后缀），不可
替换调用者原始 `message.send.idempotencyKey`。

该错误不进入全局 transient registry；其它客户端仍收到显式 error/code/data，
不得对任意操作泛化自动重试。Lark worker 单独识别它，仅重试已冻结的同一
目标、正文、metadata 和原始幂等键。DAEMON_ERROR、断连和不可验证的回执不证明
未提交；普通 DAEMON_ERROR 不自动重试。未确认窗口耗尽保留请求，不能发
`terminal.failure` 或“未送达”的确定性告警。只有明确首次前置拒绝或结构化
单目标 `accepted: false` 才能释放拒绝的请求；已有未知历史不能被后续拒绝抹掉。

Lark 的 `pending_inbound_submissions` 是新增有界持久化 custody：每 adapter
1024 条、16 MiB UTF-8 request/message-ID/event-ID 字段合计，事务内前置拒收，
未决项不淘汰；成功入站关联、seen 标记和释放同事务提交。拒收状态为
`submission-capacity-exhausted`，发生在 daemon IPC 之前。

每条 pending 的 `forward-outcome-unknown` 原生提醒采用持久 `notice_claimed` CAS，
在 native I/O 前单独写事务提交。语义是 **at-most-once attempt，不保证提醒送达**；
提醒失败、节流或claim后的中断不撤销claim，也不故意重发。audit淘汰、worker重建
和时间窗口推进均不重置它。成功结算删除pending行时同事务删除该标志，没有孤儿
claim表。每行只增加固定布尔元数据；1024条和16MiB的原三个UTF-8字段计量不变。
旧表的PRAGMA检查和ADD COLUMN在同一个BEGIN IMMEDIATE中串行；历史行无法
证明此前是否提醒，保守视为已claim，新实现新建请求显式从未claim开始。

回执必须匹配本次请求：IPC envelope 的 version/id、send result 的 operationId、
conversationId、单一 target、按原幂等键派生的 messageId，以及布尔 accepted/queued
必须一致；错帧、混合 error/result、缺字段或明确 outcomeKnown=false 仍为 UNKNOWN。
有效 daemon 回执之后本地入站关联写入失败，也不能倒退成 terminal.failure；冻结
custody 留到本地原子结算成功。

恢复直接枚举冻结 custody，不依赖可淘汰 audit、历史 lookback 或平台正文仍存在。
每个 reconcile 的 pending pass 最多16条，共用5秒单调deadline，每项只尝试一次；
既有 inbound actor 等待和真实 IPC 都使用剩余deadline。游标持久化、逐尝试推进，
长期未知项不能占住每轮首位。ReconcileReport 增加 pending_attempted、
pending_remaining、pending_held；它们单独报告业务custody，不能把 websocket健康
等同于这些请求已经送达。旧历史遍历不会再让 pending 项消耗长重试窗口。
本地SQLite仍使用既有busy timeout；这不是对操作系统磁盘延迟的硬实时保证。

冻结数据不是新的平台会员证明：retired chat 的 pending 仍可发现，但保持 held，
不自动unretire或重投；后续真实平台事件才能走既有会员恢复路径。已知ID的显式
recover优先冻结记录，不要求重新下载原生正文。关闭后不启动恢复或新的IPC。

**升级/降级：** 旧 worker 不理解新 error 或新表，不能认为它忽略新表就可无损
rollback。未决 submission 存在时须继续使用支持此契约的 worker 完成核验/处理，
不能换旧版后重新解析 pin。安全降级条件是停止新入站接纳并确认所有相关 adapter
的未决表为空，再配套切换 daemon/worker；不得删除未决行来满足此条件。

### 3.8 Agent runtime launch custody（全 actor 改造）

`agent.runtime-context` 仍由 daemon 解析 Agent home。交互式 Codex 调用可带
`credentialPreflight: true` 与可选 `modelProvider`；原生 OpenAI 路径会在
runtime-home 物化与 Codex preparer 之前检查有效凭据，并以
`CODEX_CREDENTIAL_MISSING` 及结构化 `reason/path/remediation` 拒绝。
新增投影字段 `authorityPrepared: true` 表示目录、配置与工具投影已经由 Agent authority
准备；原生启动消费者 MUST 只校验这些文件，不得再次在 authority 外改写。
`launchToken` 是不透明的上下文引用，不暴露 entity/resource token，也不代表
进程已经启动。`notices` 是非秘密的类型化操作员通知数组；例如
`CODEX_CUSTOM_PROVIDER_AUTH_BACKUP` 必须携带 Agent、说明和 Agent 私有 state 下的
备份绝对路径，不得携带凭据内容。旧的 legacy 投影不带此授权边界，沿用原有启动路径。

| method | params | 成功响应（除 `ok` 外） |
|---|---|---|
| `agent.runtime-launch.acquire` | 必选 `launchToken`；可选 `operationId`（调用方 SHOULD 预先生成并保留） | `operationId`, `actor`, `leaseToken`, `expiresAtMs: null`, `secretEnvironment`, `secretGrants` |
| `agent.runtime-launch.custody` | 可选 `operationId` 过滤条件 | `custody`: `{operationId, actor, leaseToken}` 数组 |
| `agent.runtime-launch.release` | 必选原 acquire 的 `operationId` 和 `leaseToken` | `actor`, `leaseToken`, `released` |

`secretEnvironment` 是唯一允许凭据值跨越 Agent runtime IPC 的响应字段：daemon
在 incarnation 与 grant revision 都通过 acquire fence 后解析完整 grant inventory，CLI
仅将该映射合入该 lease 下启动的 child environment。该值不进入
`agent.runtime-context`、custody/recovery 视图、argv、CLI 自身环境、日志或持久化状态；
`secretGrants` 仅包含 `[grantId, revision]` 非秘密元数据。旧 daemon 缺少字段时 CLI
按空映射处理。

启动方 MUST 在原生 spawn 前 acquire，并在受保护调用结束后释放精确的
operation/lease 对。Agent 在提交 lease 前重新确认 entity 与 home resource
身份；旧上下文以 `AGENT_RUNTIME_CONTEXT_STALE` 拒绝，不能用于重建已撤销的
目录。活动 lease 阻止 destroy/cleanup，报 `AGENT_LAUNCH_IN_PROGRESS`。
原生调用可能超过 IPC 等待时间；等待超时 MUST NOT 被当作取消或再次 acquire
的依据。上下文保护范围由消费者决定：spawn 保护围绕实际 spawn；认证 helper
保护覆盖完整 helper 调用及其写盘、回调和进程清理。

release 的 operation/lease 不匹配时以 `AGENT_RUNTIME_LAUNCH_MISMATCH`
拒绝，并保留两者的活动归属。已释放的 lease 可重复释放。acquire 响应丢失时，
调用方可用原 `operationId` 查询 custody，确认原生工作已结束或没有开始后，
显式释放查询得到的精确对；查询本身不取消或重新派发任何工作。

活动 lease 不按时间过期，`expiresAtMs: null` MUST NOT 被解释成默认 TTL。
实现保留至显式释放，最多 64 个活动 lease；上下文表最多 1024 项，仅未消费
的 offer 可按 LRU 淘汰。已消费上下文为 reconnect 保留，容量耗尽时明确拒绝
新准备，不撤销正在使用的上下文。关闭必须保留未完成的 lease、相关事件与
registry，返回未完成并允许重试。

custody 是 daemon 当前进程内的归属投影，不是持久化的原生进程终止证明。
daemon 意外丢失后，查询为空也不能证明此前的原生工作已经取消；需要独立的
进程身份/孤儿进程恢复证据。上述新增 method 不改变 IPC 信封版本。

### 3.9 IPC CPU 归因（全 actor 改造）

`ps` 的 `daemon.ipcStats` 保留 `methods`、`sessionActor`、`agentActor`、
`effectAdmission`，新增 `sessionPersistenceIo` 与 `statePersistence`。
旁边的 `daemon.ipcClients` 是 transport slot 的即时 `active`/`limit`/`saturated`
投影，不是 CPU 统计，也不包含已脱离 socket 继续结算的 domain handler。
新增两组按固定的 `<operation>/<origin>` 或 `<CommandType>/<origin>` 键统计
`calls`、`errors`、`threadCpuMs`；未知键仍折叠到有界的 `(other)` 桶。

每个执行线程只用自己的 `thread_time` 记录其执行区间，等待其他 owner 的
墙钟时间不计入 CPU。`instrumentedCpuMs` 包含六组总和；`ipcPathCpuMs`
保留原四组的来源规则，并仅加入两组新计数中的 `/ipc` 部分。
`/background` 仍计入 instrumented 总量，但不计入新增的 IPC-path 份额。
来源由类型和调用上下文确定，随不可变请求及精确 effect custody 传播，
不得从 correlation ID 的拼写猜测。已有活动 custody 的迟到完成保留原来源；
恢复扫描重新接管的工作标记为 background。

计数归 authority 实例所有，actor generation 重启不清零；daemon 重启后
重新计数。`processCpuSeconds` 仍只用于同一测量窗口的进程总量分母，不能
代替各线程计数。原验收门槛保持：归因覆盖至少 70%，且 coverage CPU
不得超过同窗口进程 CPU；未覆盖部分不得自动归为 IPC 工作。完整计算规则
见 `src/hyprial/daemon/impl/ipc/ipc_stats.py`，并由 IPC counter、后台工作、迟到完成和
generation 重启控制测试约束。

全 actor/effect 补充层新增 `runtime`，在通用 `ActorRuntime` 消息派发、通用
`EffectLane` effect 执行和 maintenance phase 三个 choke point 读取当前线程的
`thread_time`。键为 `<declared-owner>/<work-type>`；owner 的末尾数字实例后缀
会被去掉，不能使用 Pykka 私有线程名；`turn:<agent>` 这类 `kind:实例` 名只保留
kind，orgfs 车道去掉嵌入的 12 位 space id（记为 `*`），使每类 owner 只占一行。动态键
至多 512 个，其中预留一行：其余键用满后，所有新键折叠到保留行 `_overflow/_other`
（不会记到其他 owner 名下）并累计在 `runtimeOverflowFolds`。已有六组计量 owner 在通用层显式
排除，避免重复计数；因此原 `instrumentedCpuMs` 与 `ipcPathCpuMs` 语义不变，
`coverageCpuMs` 等于原 instrumented 六组加 `runtime`，是新的全覆盖分子。

daemon 每个至少 30 秒的窗口按 owner 计算单核份额，默认上限 0.5，可由
`HYPRIAL_CPU_OWNER_BUDGET_SHARE` 调整。超限时同一 owner 每 300 秒至多记录一次
`daemon.cpu.owner_over_budget`；`_overflow` 保留行永远不会告警。告警输入是全部
owner：`runtime` 块加上原六组计量各自归到其 owner 名下（`sessionActor`→
`session-authority`、`agentActor`→`agent-registry-authority`、`statePersistence`→
`state-persistence-authority`、`sessionPersistenceIo`→`desired-state-io`、
`effectAdmission`→`session-agent-effects`、`methods`→`ipc`），各组互不重叠，计数不变。
事件字段包含 `owner`、`windowMs`、
`threadCpuMs`、`share` 与按 CPU 排序的前三个 work type。

### 3.10 Resident Agent 与 Routine 结果契约

这些字段是 daemon IPC 的稳定 additive JSON 形状；调用方必须按“字段缺失”而不是空值识别未降级的成功：

| method | 成功结果字段 | 语义 |
|---|---|---|
| `routine.list` | `routines: object[]` | 当前 Routine 投影；每项至少含 `name`, `owner`, `enabled`, `actor`, `actorOwnership`, `inFlight`。 |
| `agent.create`, `agent.host-invite` | 可选 `routineWarning: {code, message}` | Agent 已创建，但默认 Routine 无法建立。当前成功警告码仅为 `AGENT_DEFAULT_ROUTINE_UNAVAILABLE`；这不是 `error.code`。正常绑定成功时字段缺失。 |
| `agent.list` | `agentsWithoutRoutine: string[]` | 当前可见 resident Agent 中没有任何绑定 Routine 的 canonical URI；正常无缺口时为空数组。 |
| `agent.destroy` | `removedRoutines: string[]` | 在同一销毁流程中已结算并删除的 Routine 名称；无绑定时为空数组。 |

默认 Routine 的添加一经 coordinator 接受，等待超时只返回错误 envelope
`error.code = AGENT_DEFAULT_ROUTINE_PENDING`，其 `error.data` 至少含 `actor`，并在可用时含
`operationId`；这不是回滚证明，Agent 与 durable operation 均继续存在。后续成功保留该 Agent；
后续终态失败由 coordinator 以 `entityToken` CAS 仅删除原 incarnation。同步或最终失败返回
`AGENT_DEFAULT_ROUTINE_FAILED`，`error.data` 含 `actor` 与 `routineError`。Routine 修改/删除的
机器码为 `ROUTINE_NAME_IMMUTABLE`、`ROUTINE_BINDING_IMMUTABLE`、
`ROUTINE_LAST_BINDING`；这些与前述两个 Agent 错误码均由
`hyprial.contracts.ipc_errors` 注册并冻结。

### 3.11 Agent migration admission

`AGENT_CREATE_COMMIT_FAILED` 表示 Agent home 文件效果已完成，但创建记录的
SQLite 提交失败。Host invite 边界只在同一 `entityToken` 的 typed destroy settlement
完成后返回此错误；若回收仍未结算，调用方不得把失败当作 home 已清理。
`AGENT_CREATE_ROLLBACK_PENDING` 表示同一创建 correlation 的回收仍未结算；
`error.message` 保留创建与回收阶段的错误摘要，下一次必须按原 incarnation
继续结算，不能按名字删除一个后继 Agent。

`AGENT_MIGRATION_BUSY` 表示迁移容量已满，或目标 Agent 的迁移租约仍持有冲突操作。
此拒绝发生在新操作的变更准入之前；不代表已经接受的迁移被取消。其他 Agent 的
不相关操作与只读投影继续可用。已接受迁移的调用方超时不撤销文件效果或 owner 提交；
清理必须等同一操作的结算，不能用新请求失败推断旧请求未执行。该错误码由
`hyprial.contracts.ipc_errors` 注册并冻结。

## 4. Channel protocol(交互式会话)

### 4.1 版本常量

`CHANNEL_PROTOCOL_VERSION`,定义于 `src/hyprial/contracts/channel.py:9`,当前值 `2`(纯 `int`,非字符串非 semver;显式排除 `bool`,见 `channel.py:34-35` 与 `application.py:5416-5423`)。历史:`1`(2026-08-11,`de5846a`)→`2`(2026-08-13,`929a8fe`,"fix(mcp): expire dead channel registrations")。docstring 原文(`channel.py:7-8`):*"Increment only when the registration/status telemetry contract changes in a way that operators must distinguish from an already-running stdio child."*——即这是一个"运维需要区分新旧运行实例"时才递增的粗粒度世代标记,不是逐字段协商的 wire 版本号。

### 4.2 握手序列

没有独立的"版本协商" RPC——协议版本是塞在 `session.register` 请求参数里的一个字段,注册一次,定终身(见 §4.3)。完整时序:

1. MCP 传输层握手(与本节协议版本无关,见 §4.6):`_install_legacy_discover_handler`(`mcp/channel.py:204-230`)固定应答 `HANDSHAKE_PROTOCOL_VERSIONS`,让 Claude Code 继续走 stdio 主动通知路径(注释指明这是 CC ≥2.1.227 的 E2E-008/#94 修复)。
2. `notifications/initialized` 触发后(`:1131-1136`),`adapter.start()` 被调用恰好一次(`:573-577`)。
3. `ClaudeChannelAdapter.start()`(`:293-318`)发出注册 RPC:

   ```python
   method="session.register",
   params={
       "cwd": self.cwd, "command": list(self.command),
       "source": "claude-channel", "runtime": RuntimeKind.CLAUDE_INTERACTIVE,
       "channelConfirmed": True,
       "channelBuildVersion": __version__,
       "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
       "ownerFence": self.owner_fence,
       "channelLeaseToken": self._lease_token,
       ...
   }
   ```
   (`mcp/channel.py:294-315`)——`channelProtocolVersion` 就是发起方进程当前内存里的常量值,**握手内容仅此一次,后续 `session.refresh`/`session.heartbeat` 都不再携带它**(只带 `channelLeaseToken`,`:392-398,416-421`)。
4. 服务端 `session.register`(`application.py:2155-2278`)构造 `InteractiveSession`,计算 `channel_lease_digest`(见 §4.3),做 last-writer-wins 的 actor 绑定(`_bind_agent`,`:2205-2218`),持久化,返回 `daemonEpoch`(`:2267`)。
5. `adapter.start()` 立即做一次 `poll_once()`(`message.pending.list`,`:318`)。
6. 两条常驻循环随后并行启动(`:659-674`):`_run_channel_poll_loop`(消息轮询,失联时用 `session.refresh` 重连)与 `_run_channel_heartbeat_loop`(心跳,见 §4.4)。
7. 轮询循环每轮干净结束后调用 `message.pending.wait`(门铃,2026-10-05):参数 `knownMessageIds` 为上一次 `message.pending.list` 看到的消息 id;daemon 在出现未见过的待处理消息或系统通知时立即返回 `changed: true`,否则最多保持 5 秒(`MESSAGE_WAIT_HOLD_SECONDS`)后返回 `changed: false`。它只读,不消费通知、不提交 custody。`held: true` 表示 daemon 确实等了,循环据此立即进入下一轮 `message.pending.list`;`held: false`(等待名额满)、任何错误或旧 daemon 的 `METHOD_NOT_FOUND`(只探测一次)都回退到原来的固定间隔轮询,门铃只决定下一次轮询**何时**发生,不决定**是否**发生。

### 4.3 "握手时代钉版本"的确切机制

**不是**"每次心跳都比对版本"。它是注册时的一次性闸门,结果被冻结进 desired-state,整个会话生命周期内不再重算。

核心函数 `_channel_lease_digest_for_registration`(`application.py:5443-5458`):

```python
def _channel_lease_digest_for_registration(
    *, source: str, protocol_version: int | None, token: object
) -> str | None:
    if source != "claude-channel":
        return None
    if protocol_version != CHANNEL_PROTOCOL_VERSION:
        # Protocol-v1 and older registrations remain readable and routable, but
        # cannot claim lease-backed operational liveness.
        return None
    lease = _channel_lease_token(token)
    return hashlib.sha256(lease.encode()).hexdigest()
```

- `protocol_version` 是**客户端注册时**发来的值;`CHANNEL_PROTOCOL_VERSION` 是**服务端此刻正在跑的**常量值——两者在注册这一瞬间比较一次。
- 匹配 → 计算租约 token 的 SHA-256 摘要,存进 `InteractiveSession.channel_lease_digest`(`desired_state.py:146`),解锁"租约支持的存活证明"。
- 不匹配 → `channel_lease_digest` 终生为 `None`——会话依然注册成功、依然可路由,但后续每个依赖 `channel_lease_digest is not None` 的判定(心跳续约 `:2344-2345`、refresh 续约 `:2306-2307`、`_channel_alive`、`_expire_stale_channel_routes`、`_interactive_session_status`)都会把它当作"活性未经证实",而不是"存活"。
- **这个判定不会被重新评估**:`session.heartbeat`/`session.refresh` 只做租约 token 的 HMAC 比对(`_verify_channel_lease`,`:5461-5473`),**不重读 `CHANNEL_PROTOCOL_VERSION`,不重比 `protocol_version`**。也就是说这个"代钉"结果连 daemon 重启都能扛过去——重连走的是 `session.refresh`(明确不是 `session.register`,`mcp/channel.py:544-548` docstring 原文:"child uses `session.refresh` ... rather than invoking last-writer-wins registration")。
- 另有一个**语义完全不同、容易与上面混淆**的读取路径:`channel_generation()`(`contracts/channel.py:27-39`)——每次查状态(`hyprial daemon status`)时**现算**,拿存的 `channel_protocol_version` 和服务端**此刻**的 `CHANNEL_PROTOCOL_VERSION` 比,得到 `"current"`/`"legacy_or_unknown"` 标签,纯展示用途,不影响任何实际行为。**MUST**(文档纪律,非代码变更):后续任何人读代码/写文档提到"版本匹配"时,必须说清楚是在说"注册时冻结的 lease 判定"还是"每次查询现算的展示标签"——这两者当前容易被混为一谈,已经是一个潜在的措辞漂移点。

### 4.4 心跳/存活常量

全部定义于 `contracts/channel.py:13-17`:

| 常量 | 值 | 含义 |
|---|---|---|
| `CHANNEL_HEARTBEAT_INTERVAL_SECONDS` | `1.0` | 客户端 `session.heartbeat` 循环周期 |
| `CHANNEL_HEARTBEAT_MISS_BUDGET` | `6` | 容忍的连续丢失心跳数 |
| `CHANNEL_LIVENESS_TTL_SECONDS` | `6.0`(= 上两者相乘) | daemon 侧判定"最近观测到过"的最大时间窗 |

注释原文(`channel.py:10-12`):*"Heartbeat cadence is deliberately independent from message polling and stdio notification delivery. The same-OS-user boundary is trusted: this lease is an operational misuse fence, not a hostile-local authentication mechanism."*——**这条边界假设(同 OS 用户即信任)与 §3.1 daemon IPC 的 `0o600` socket 权限是同一个信任模型,两处应该保持一致,不要有一处收紧却没有同步另一处**。

### 4.5 失败模式

- **注册时版本不匹配**:不拒绝注册,只是 `channel_lease_digest=None`(见 §4.3)——`channelLiveness: "unverified"`。**没有硬错误**。
- **心跳超时/过期**:`_channel_alive` 判 `False`;`_expire_stale_channel_routes`(`ps` method 触发,`:1808`)主动拆除该 actor 的路由;persisted `InteractiveSession` 记录不删除,后续 refresh/heartbeat 可复活。
- **被顶替(superseded)**:同一 actor 的更新会话注册后,旧会话的轮询/心跳循环永久静默退出(`mcp/channel.py:593-605,636-640`),**不重新注册**——docstring 明说这是刻意的"防抢跑"设计。这是唯一的终态、不可重试判定。
- **daemon 世代不匹配**:心跳先于 refresh 证明当前 epoch → `DaemonRequestError("STALE_DAEMON_GENERATION", ...)`(`:2346-2351`),要求先 `session.refresh`。
- **租约 token 无效/缺失**:`_verify_channel_lease` 报 `INVALID_CHANNEL_LEASE`(HMAC 不匹配)或 `CHANNEL_LIVENESS_UNAVAILABLE`(会话本无 digest)。

**总结一句**:`channelProtocolVersion` 不匹配从来不是硬拒绝,只会降级活性证明能力——这一点在写迁移/升级预案时必须准确引用,不能简单说成"版本不对就连不上"。

### 4.6 与另外两个协议面的关系

已核实**没有任何代码同时读取/比较 channel protocol 版本、daemon IPC `version`、zenoh schema 版本中的两个以上**——三者是完全独立的版本空间(见 §1 的总表)。`session.register` 等 method 本身是**装在** daemon IPC 信封里的 `method`/`params`,但信封的 `version:1` 和载荷里的 `channelProtocolVersion` 从未互相引用。

### 4.7 稳定性

`contracts/channel.py` 最后改动 2026-08-13(`929a8fe`),写作本文档时(2026-08-24)**11 天未变**,相对已经沉淀。`mcp/channel.py` 本身仍在活跃变动(最近一次 2026-08-20,`a645710`,tmux 分离会话的 owner-fence 特性),但改动集中在 owner-fence/进程身份识别逻辑,**没有触碰 `CHANNEL_PROTOCOL_VERSION` 本身或 §4.3 的代钉逻辑**。**标签:MODERATELY STABLE**(版本常量和代钉机制稳定;宿主模块仍在演进,但演进没有波及本节描述的握手/代钉契约)。

---

## 5. 演进规则(防漂移核心)

这一节是本文档存在的直接原因(§0)。以下规则适用于**全部三个协议面**,不只是 zenoh schema。

### 5.1 additive vs breaking 的判据

**ADDITIVE**(可以直接加,不需要 bump `/v2` 或走兼容期)当且仅当**以下全部成立**:

1. 新字段**整体可选**——旧编码器可以完全不写这个 key(不是写 `null` 占位),新解码器对缺失 key 有明确、合理的默认值。
2. 旧解码器遇到**未知字段时忽略**,不报错、不拒绝整个消息。
3. 不改变任何既有字段的类型或取值域。
4. 不修改 schema 字符串本身。
5. 不改变 canonical JSON 编码约定(§2.3)。

**worked example**(①`origin_node`,`zenoh_link.py:41-47,68-70`):

```python
# Optional and additive: pre-stamp decoders ignore the key, and
# pre-stamp frames simply decode to origin_node=None.
**(
    {"origin_node": message.origin_node}
    if message.origin_node is not None
    else {}
),
```
这是本仓目前**唯一**一处对"additive 字段"的注释显式点名的代码,应该被当成模板复制,而不是每次重新发明写法。

**BREAKING**(必须 bump `/v2`,或走双读双写兼容期——本轮不建议任何具体改名,这里只定义判据):

- 新增**必传**字段——不论这个字段看起来多小、多"内部"。**这条规则不只适用于 zenoh schema**,同样适用于内部工具链契约,见下面的反例。
- 修改既有字段的类型或取值域。
- 删除字段(**除非有代码证据确认零生产调用方**——参照 §6 实际执行过的核实方法:全仓搜索字面量 + 追踪每个生产者/消费者调用点,且在真正删除前**重新核一遍**,因为审计和执行之间可能已经有新代码落地,不能凭一次性的旧结论直接动手,更不能凭感觉判断"应该没人用")。
- 修改 schema 字符串。
- 修改 canonical JSON 编码约定——**这一条容易被忽视,但后果是字节级的**:任何做哈希/签名/去重比较原始字节的消费者会在编码约定变化后悄无声息地失败,不会报"版本不兼容"，只会报"签名对不上"或"重复消息没被去重"这类看起来无关的错误。

**反例(真实事故,已发生,记录在案)**:2026-08-20 提交 `a645710` 给交互式 TUI 加了 `--tmux` 分离会话支持,`ClaudeChannelAdapter.start()` 的 CLI 调用参数里新增了 `tmux_session`。这个新参数在**一处被冻结签名的 E2E 桥接契约**(`bridge_frozen_channel_cli_contract`)里没有同步更新——桥接补丁当时只检查"旧文本还在不在"(`before.count(old) != 1`),不检查"冻结签名是否接受当前 CLI 传的每一个 kwarg"。后果:场景跑到第 4 分钟才裸抛 `TypeError`,代价是**四天 CI 红 + 一夜取证**(commit `786d5ab` 的原话)。事后修复方案是 `assert_frozen_signature_accepts_cli_call`(`contract/e2e/run-upgrade-continuity.py:583`):用 AST 直接比对 `cli.py` 调用点的 kwargs 和冻结签名,缺失字段在 seed 期(几秒内)就报错并点名"缺 `tmux_session`,请加进 `bridge_frozen_channel_cli_contract` 的 `new` 文本",而不是让它在运行时深处裸炸。

这个事故不是 zenoh wire schema 事故,是"内部测试桥接契约"事故——但**判据完全一致**:给一个已冻结的契约新增一个未经过渡期就被要求必须提供的参数,就是 breaking,不管这个契约是不是对外的 wire protocol。**MUST**:任何"要求现有调用方立即改动才能继续工作"的变更,都不算 additive;新增参数/字段要么整体可选、要么先加一轮"可选 + 有默认值"的过渡期,内部工具链契约和生产 wire schema 一视同仁。

### 5.2 新增字段的默认值纪律

**MUST**:
- 编码器侧:只在值非默认(通常是非 `None`)时才写出该 key,不要写 `null` 占位(参照 §5.1 的 `origin_node` 模板)。
- 解码器侧:对缺失 key 用 `.get(key, 合理默认)` 处理,不能因为缺一个可选字段就 raise。②`droppedSinceSeq` 的 `record.get("droppedSinceSeq", 0)` 和⑤`accepted`/`duplicate` 的 `.get(...) is True` 都是这个规则的现成例子,可以直接引用。
- **CI/契约测试也要同步这条纪律**——`786d5ab` 的教训是"文本存在性检查"不能替代"字段级/参数级兼容性检查"。新协议字段落地时,如果有配套的契约测试桥接层,**必须**在同一个 PR 里更新桥接层对新字段的接受规则,不能指望"以后有人会想起来"。

### 5.3 新 schema 家族的准入

- **谁批**:Allen(现状——本轮没有发现任何一个新 schema 是绕过设计稿/PAC 直接落地的,现存五个家族都能在 `docs/design/design.md` 或对应 commit 找到裁定依据)。
- **放哪**:**MUST** 放在"拥有这份数据"的模块里(如 `inbox/`、`squire/`、未来可能的新模块)。历史上曾有 `proto/` 包专属 IRC 编解码层——它已随 IRC 一起于 2026-08-25 整体删除(见 §6);这段历史本身就是"层级错配"要害的反面教材:把新 schema 放进一个专属另一个协议方言的包,后来只能连着那个方言一起拆,不要重演。
- **怎么进契约测试**:**现状缺口,本轮不修,但准入新 schema 时必须正视**——§7 会说明,当前 27 个 wire-golden 契约用例**没有一个**覆盖五个 tagged schema 里的任何一个字段级正确性。新 schema 落地时不能假设"反正有契约测试兜底",因为目前这套契约测试考的是另一层东西(`InboxService` 状态机,不是 zenoh wire 字段)。新 schema 的作者应该自己在对应模块下补字段级单测(参照②`progress.py`、③`pull.py` 现有的单测风格),不要指望 `contracts/wire.py` 自动覆盖。

### 5.4 随意添加关键词 / 层级错配——禁止的具体形态

Allen 点名的两种漂移,给出代码里已经发生过的真实案例做反例(详细审计见 PR #264,这里只摘录与"如何避免复发"直接相关的部分):

**随意添加关键词**的具体样子:同一个词根(`channel`)在没有互相参照、没有统一裁定的情况下,被工程师在四个不相关的子系统里各自使用了一次——飞书转发地址前缀、交互式会话唤醒机制、真实的 IRC 编解码、以及 `route` 投递路径分类器的常量名。四处用法互不知道对方存在,直到专门审计才被发现。

**MUST**(新增协议专有名词时):
1. 引入一个新词之前,`grep -rn` 一下这个词根在仓库里是否已经被别的层用过。
2. 如果用过且语义不同,**必须**显式换一个新词,或者在这份文档(或它未来的分节)里显式写一条消歧记录——不能假设"反正上下文能看出来是哪个意思"。
3. 不要因为"英语里这个词听起来贴切"就直接用——`channel` 在英语里对交互式会话、对 IRC 群组、对飞书网关都"听起来贴切",这正是问题所在。

**层级错配**的具体样子:`daemon/identity.py` 的 `TARGET_KIND_CHANNEL_ROUTE = "channel_route"` 把"投递路径/route 层"的常量取名用了"channel"这个属于"adapter 入站身份层"的词根,与同一份注释里紧挨着的、真正表示入站身份的 `CHANNEL_URI_PREFIX` 语义完全不同却共享词根——读代码的人必须靠专门的一段消歧注释(`identity.py:172-175`)才能分清两者,而不是靠命名本身。

**MUST**(新增常量/字段名时):
1. 一个常量/字段属于协议里的哪一层(§1 的三个协议面之内,再往下比如 zenoh 的"传输层 vs 结构化载荷层"、daemon IPC 的"信封层 vs 业务参数层"),它的名字词根就要跟这一层锁定,不能借用另一层已经在用、且代表不同含义的词根。
2. 如果两个不同层级"看起来在说同一件事"所以想共用一个词——那通常是信号,说明这两层之间缺一层清晰的边界定义,应该先把边界定义写清楚(参照 `keys.py:141-144` 那条"delivery line 和 agent-entity line 由 namespace 前缀强制隔离"的模式),而不是直接共用命名让边界变得模糊。

---

## 6. IRC codec 剔除执行记录(`hyprial-resource-manifest/v1` 含在内)

本节曾是"处置建议"(供 Allen 派单参考,本轮不执行);**2026-08-25(PAC NAMT3 664a7d314947)执行完毕,现在是执行记录**。

**⚠️ PR #268 时期(PAC b8d2b52b6358)对 `mapping.py:73`/`:374` 下的"不做双写"排除令,随本节执行而失效——不是撤销,是兑现**:那条排除令的前提是"这两行在无生产调用方的死代码里,给死代码做双写没有收益";现在死代码本身已经整体删除,排除令的对象不复存在,不再需要专门记录"为什么跳过它们"。

### 6.1 删除前的重新核实(未直接沿用旧结论)

`docs/reference/proto.md` §2/§7 原有的"零生产调用方"结论写于 2026-08-24(dev@`ef66f2b` 附近),执行删除前(dev@`f1614bf`,已合入 #265/#266/#267/#268/#280)按 §5.1 的规则重新核实了一遍,而不是直接信旧结论:

- `src/hyprial/proto/{mapping,codec,capabilities}.py` 的每一个公开符号(`__init__.py` 的 `__all__`)全仓库 grep,命中仅限 `src/hyprial/proto/*.py` 自身、`tests/test_proto.py`(纯单元测试)、`src/hyprial/contracts/wire.py`(唯一的生产侧 import 点,且仅在 `_message()` 一处调用)。
- `irctokens` 包的 import 全仓库 grep,命中仅限 `src/hyprial/proto/{mapping,codec,capabilities}.py`;`pyproject.toml` 声明为硬依赖,`contract/python-resolver.sh` 只做存在性预检查(不是功能调用)。
- **发现一处需要一并处理的耦合(不是新的生产调用方,是加载时副作用)**:`src/hyprial/contracts/__init__.py` 无条件 `from .wire import evaluate_case`,而 6 个生产模块(`daemon/application.py`、`daemon/desired_state.py`、`cli.py`、`adapters/lark/runtime.py`、`mcp/channel.py`、`mcp/server.py`)都会 import `hyprial.contracts.channel`/`.session`/`.lark` 等兄弟子模块——按 Python 包语义,这会先执行 `contracts/__init__.py`,从而把 `hyprial.proto`/`irctokens` 拉进 `sys.modules`。**这不是调用链,是加载链**:没有一个生产函数实际执行过 `hyprial.proto` 的代码,但如果只删 `proto/` 不同时改 `wire.py`,daemon/CLI/MCP 会在启动时炸 `ModuleNotFoundError`。处理方式:见 §6.2,先改 `wire.py` 让它不再依赖 `hyprial.proto`,删除顺序因此是"先改依赖方,后删被依赖方",不是同时动。

结论:**再次确认零生产调用方**,且额外确认了删除顺序上的一处真实约束(上面这条),不是走过场的重复审计。

### 6.2 实际执行了什么

1. **`src/hyprial/contracts/wire.py` 重写,不再依赖 `hyprial.proto`**:`_message()` 从"构造 envelope → `envelope_to_lines` → `lines_to_envelope` 往返一次 IRC 编解码"改成直接从 JSON fixture 取值构造 `InboxMessage`(`message_id`/`conversation_id`/`sender`/`intent` 等字段直接读 envelope dict,`payload` 改为 envelope 本身的 canonical JSON 编码,不再是 IRC 行字节)。`InboxService` 状态机部分的验证力**不受影响**——那部分从来不经过 IRC,经过的是 `_message()` 构造出的 `InboxMessage` 对象本身,构造方式换了,构造出的对象形状没变。
2. **三个仅由 IRC 守卫支撑、无生产对应实现的契约用例整体退役**(原 WG-026「correlated reply 可以指向 ChannelGateway」、WG-027「未关联的 request 不可指向 ChannelGateway」、WG-030「hop count 超过 8 被拒绝」)——这两条守卫(ChannelGateway 目标守卫、hop-limit 守卫)只存在于被删除的 `envelope_to_lines()` 内部,**从未被任何生产发送/接收路径调用过**,所以退役这三个用例不是"移除了曾经保护过生产流量的防线",防线从来就只保护测试自己。`contract/wire/golden/expectations.json` 的 30 例减到 27 例(`contract/wire/golden/envelopes.json` 相应移除只被 WG-030 引用的 `hop-overflow` fixture;`gateway-request`/`gateway-reply` 两个 fixture 仍分别被 WG-019/WG-021 使用,保留)。`contract/wire/golden/validate.py` 的 `REQUIRED_COVERAGE` 集合不含 `channel-gateway`/`loop-prevention` 标签,退役这三例不违反结构校验(已实跑 `validate.py` 确认:`RESULT PASS 21 envelope fixtures, 27 expectation cases, 30 coverage tags`)。
3. **`src/hyprial/proto/` 整包删除**(`__init__.py`/`mapping.py`/`codec.py`/`capabilities.py`)+ `tests/test_proto.py` 删除。
4. **`irctokens` 依赖移除**:`pyproject.toml` 的 `dependencies` 摘掉;`uv lock` 重新解析,`uv.lock` 里 `irctokens` 条目消失;`contract/python-resolver.sh` 的模块预检查列表同步摘掉。
5. **防再生守卫升级为终态规则**(`tests/test_channel_lark_prefix_guard.py`):不再是"过渡期"措辞,直接声明入站双收永久保留(§ Tier 3 决定,见下)、出站永久禁止 `channel:lark:` 新写。

### 6.3 Tier 3 摘旧的范围裁定:入站收留,出站禁产

**裁定(风险评估后确定,倾向已在 PAC 里给出,这里是执行落地):`channel:lark:`/`channel:<owner>:<machine>:<name>` 的入站解析双收永久保留,不设版本周期终止;出站(新写)自 PR #280 起已经彻底改为 `adapter:` 形态,本轮没有新增出站切换工作(已在上一轮完成)。**

理由:生产环境的 `desired-state`/`channels.json`/inbox 历史记录里已知存有 `channel:lark:*` 字面量(PAC 52128299015a 给出的生产 `targets` 实测证据);这些记录**零迁移零改写**是硬约束(§ 验收判据 4)——唯一让旧记录继续可用的办法就是解析层继续认得这个前缀。摘除入站解析不会让旧记录"自然消亡",只会让它们从"能解析但没有新实例"变成"直接读不出来",这是主动破坏,不是自然淘汰。**旧形态的消亡路径是:没有任何代码再写出它(§6.2 第 5 点的守卫保证),历史记录随正常的数据生命周期(TTL、手动清理、迁移脚本)逐渐减少,解析代码本身不必也不应该主动清场。**

---

## 7. 契约测试 vs 本文档的对照

`contract/wire/golden/`(`expectations.json` + `envelopes.json`)+ `src/hyprial/contracts/wire.py`(`_WireEvaluator`,`evaluate_case` 分发)+ `tests/test_wire_contract_adapter.py` 构成 27 例(`WG-001`~`WG-025`、`WG-028`~`WG-029`;原 `WG-026`/`WG-027`/`WG-030` 已随 §6 的 IRC codec 剔除退役)冻结契约套件。

### 7.1 这 27 例实际证明了什么(2026-08-25 更新:IRC codec 剔除后)

`contracts/wire.py` 模块 docstring 现已更新为:*"there is no wire-format conversion step to cross any more... this adapter's job is durable-inbox semantics, not envelope serialization."* `_message()` 不再经过任何编解码往返,直接从 JSON fixture 的字段构造 `InboxMessage`(`message_id`/`conversation_id`/`sender`/`intent` 等逐字段读取,`payload` 是 envelope 本身的 canonical JSON)。这改变了"真实协议逻辑被驱动"这一类的构成,但**没有削弱**对 `InboxService` 的验证力度——分两类:

- **真实生产逻辑被驱动**(能提供真实保障):所有调用 `InboxService.submit/receive/ack/fail/retry_due/...` 的用例(WG-001~016、028、029)驱动的是 `src/hyprial/inbox/service.py` 真实的 SQLite 状态机(真实去重窗口、FIFO/容量/DLQ/退避重试、hold-TTL)——这部分证明力与 IRC codec 剔除前完全一致,因为它从来不经过 IRC,经过的只是 `_message()` 构造出的 `InboxMessage` 对象。
- **fixture 模拟,不是真实实现**(证明力弱,IRC 剔除前后都是如此):WG-017/018(registry 解析——结果完全由 `context["registryMatches"]` 决定,没有调用任何 registry 模块)、WG-019(gateway 默认路由——同样由 `context` 直接决定)、WG-020/021(回复路由持久化——靠 `wire.py` 内部私有的 `_RouteIndex` sqlite 表模拟,仓库里目前**没有对应的真实生产模块**)、WG-022~025(飞书引用/pin 兜底路由——靠 `context["pinnedActor"]` 和对 fixture 文本的字面量子串匹配)。这些用例目前只证明"fixture 声明的 `context` 值能原样透传",不证明任何真实实现的正确性,因为对应的真实实现在这个仓库里还不存在。
- **已退役,不再存在**(原属"真实协议逻辑被驱动"一类,但驱动的是已删除代码):原 WG-026/027(ChannelGateway 目标守卫)、WG-030(hop-limit 守卫)。这两条守卫此前是这套契约里"真实性"最强的部分之一(直接命中 `mapping.py` 源码里的错误文案字符串),但它们保护的是一段从未被生产调用过的代码——退役它们不是把"强证明力"换成"弱证明力",是把"对已删除代码的强证明力"换成"没有这三条了"。§6.2 已说明为什么这不是覆盖率倒退。

### 7.2 与五个 tagged schema 的覆盖对照——**零覆盖(IRC 剔除前后都是零,不是本轮引入的新缺口)**

**核实结论:27 个用例里没有一个触达 §2.2 五个 schema 家族中的任何一个。** `wire.py` 用 `MemoryDeliveryTransport`(`src/hyprial/inbox/memory.py`)而非 `ZenohDeliveryTransport`,从不调用 `encode_delivery_frame`/`decode_delivery_frame`(①);不触碰 `pull.py` 的 `DeliveryStatusStore`/`encode_status_frame`(③);不调用 `submit_progress_event`/`receive_progress_event`(②);完全不 import `squire/addressing.py`(④⑤)。`_message()` 现在直接把 envelope 序列化成 `InboxMessage.payload`,这个 payload 形状本身**也不是**①`hyprial-delivery-frame/v1` 的形状(缺 `schema`/`lifecycle` 等信封级字段)——契约测试的 payload 从"IRC 行字节"换成了"envelope 的 JSON 编码",两者都不是任何一个生产 wire schema,零覆盖这个结论没有因为本轮改动而变化,只是构造 payload 的具体方式变了。

`contract/wire/golden/validate.py` 的 `REQUIRED_COVERAGE` 集合(`:17-20`)只检查行为标签(`send`/`reply`/`ack`/`dedup`/`offline`/`R10`/…),**没有 schema-family 维度的覆盖检查机制**——也就是说这个缺口目前连"有没有被覆盖"这件事本身都没有自动化手段去发现。

**这仍是 §9 记录的已知缺口,本轮不修:契约测试考的是 `InboxService` 状态机这一层,不是 §2 描述的 zenoh wire schema 字段层,这两层目前仍是两套完全不相交的测试资产。IRC codec 剔除只是移除了"考已删除代码"的那三个用例,没有新建也没有填补 schema 层的契约测试缺口——"谁来对五个 tagged schema 做字段级契约测试"这个问题依然悬着,留给未来单独立项。**

### 7.3 用例内部发现的不一致(按任务要求列出,不修)

- **WG-020**("historical native reply remains correlated after old cache window"的标题声称测试缓存窗口过期后的行为)——实现里 `age_ms` 从 `context["replyRouteAgeDays"]` 算出并存成负的 `created_at_ms`,但 `_RouteIndex.get()`(`wire.py:79-84`)是裸 `SELECT ... WHERE message_id = ?`,完全没有时间过滤。把 `replyRouteAgeDays` 改成任意值(0、8、10000)结果都一样。**这个用例的标题声称的行为,当前实现完全没有测到。**
- **WG-022 与 WG-023 逻辑完全相同**——分别声称测"己方 bot 缺失回复记录回退到 DM pin"和"外部 bot 引用路由到 DM pin"两种不同情形,但两者用同一个 fixture(`quote-external`),方法体(`wire.py:495-510`)都只读 `context["pinnedActor"]` 和对 fixture 文本做字面量子串匹配——`context["quotedSender"]`、`context["replyRouteMissing"]` 两个字段两个方法都不读。**这两个用例区分不出彼此,测的是同一件事。**
- **投递重试已改为 ACK-or-TTL**：recipient 离线时不按 timer 重试，行的 due
  直接停在 TTL；online/no-ACK 使用 `DEFAULT_POLICIES[EXTERNAL_IO]` 的指数曲线、
  jitter 与 max-backoff，pump 按 outbox/custody 的最早 due 等待（无 due 时等待
  max-backoff）。attempts 只作观测，不再有 headless/interactive 最大次数或
  confirmation budget；WG-014 钉住这一行为。

---

## 8. 实验性/尚不稳定的部分——诚实标注

按 Allen 给出的原则("把不稳定的部分诚实地标出来,比假装全部稳定更符合这份文档的目的"),明确给出稳定性判断:

- **②`hyprial-progress-event/v1`:标记为 EXPERIMENTAL。** 整个实现只有一次提交(`f812b05`,2026-08-20),写作本文档时刚好 4 天,从未经历过一次真实的 additive 演进(不像①已经扛过 `origin_node` 的扩展)。§5 的演进规则对它同样适用,但**建议在它至少经历过一轮真实的字段新增/调整之前,不要把本文档给出的它的字段表当成"已验证过流程管用"的范例引用**——目前只是"现状描述",不代表这个字段集合已经定型。`sort_keys` 缺失(§2.3)这个不一致也集中在这个最年轻的 schema 上,某种程度上印证了它还没经过"多写手实现"的打磨。
- **④⑤`hyprial-user-delivery(-result)/v1`、①`hyprial-delivery-frame/v1`、③`hyprial-delivery-status/v1`:标记为 STABLE / MODERATELY STABLE。** 均有 10 天以上历史,①和③都经历过至少一轮真实的跟进变更而没有破坏既有消费者。
- **channel protocol(§4)的版本常量与代钉机制:标记为 MODERATELY STABLE。** `CHANNEL_PROTOCOL_VERSION` 本身 11 天未变;宿主模块 `mcp/channel.py` 仍在活跃开发,但改动没有触及本文档描述的握手/代钉契约本身(§4.7 已详述)。
- **⑥`hyprial-resource-manifest/v1`:不评级——已随 IRC 于 2026-08-25 整体删除(§6),不再是本文档描述的协议的一部分。**
- **daemon IPC(§3)的帧格式/信封形状:标记为 STABLE。** 三套客户端实现虽有 §3.2/§3.7 提到的小不一致,但核心帧格式(newline-delimited JSON、`version`/`id`/`method`/`params`)自协议存在以来没有变过。
- **§3.6 的版本协商能力:明确标记为不足**——不是"实验性会变",而是"当前设计范围本来就没打算支持跨版本协商",如果未来要支持滚动升级,这里需要专门重新设计,不是"再等等就稳定了"的问题。

---

## 9. 发现的问题(不在本轮修)

按任务要求,协议里的真缺陷记录在此,不顺手修:

1. **Canonical JSON 编码约定五个家族不完全一致**(§2.3):②`progress-event` 不设 `sort_keys`,且是五者中唯一一个;`ensure_ascii` 在①③(默认 `True`)和②④⑤(显式 `False`)之间也不统一。目前没有可观测后果(没有消费者做跨副本字节比较),但只要未来有人给②加基于哈希的去重,这会变成隐性 bug。
2. **daemon IPC 请求/响应两个方向的 `ensure_ascii` 也不一致**(§3.2):请求侧默认 `True`,响应侧显式 `False`。
3. **`mcp/unix.py` 客户端读循环没有 8 MiB 上限**,是三套 IPC 客户端实现里唯一的一处(§3.2)。目前无实际风险(服务端强制上限),但不对称本身是维护负担。
4. ~~**daemon IPC 没有集中的错误码注册表**(§3.7):108 处构造点,39+ 种字面量 code,只有一个(`SESSION_SUPERSEDED_CODE`)被提升成跨模块共享常量。没有机制防止两处不小心拼错同一个 code 字符串导致客户端分支失效。~~ **已修复**:`hyprial/contracts/ipc_errors.py` 注册表 + 冻结/AST 双守卫,见 §3.7 更新。
5. **daemon IPC 的 `version` 字段目前不具备真实的协商能力**(§3.6):双端硬编码同一个字面量,新旧构建之间的方法名/参数不匹配不会被这个字段拦下,只会在调用时报 `METHOD_NOT_FOUND`/`INVALID_ARGUMENT`。
6. **`hyprial-user-delivery-result/v1`(wire schema)与 `UserDeliveryLedger` 的本地落盘格式共用"schema"这个词,但版本语义不同**(§2.2 ⑤):前者是 `"hyprial-.../v1"` 字符串族成员,后者是纯整数 `schemaVersion`。命名巧合容易造成误读,建议未来有人碰这块代码时留意,不建议现在专门为此发起改名。
7. **wire-golden 27 例对五个 tagged schema 的覆盖率为零**(§7.2),且没有自动化机制能发现这个缺口(`validate.py` 只查行为标签,不查 schema 覆盖)。IRC codec 剔除后依然如此,不是本轮改动引入的新缺口。
8. **WG-020 的标题声称测试"缓存窗口过期"行为,但实现里从未做时间过滤,声称的行为没有被真正测到**(§7.3)。
9. **WG-022/WG-023 两个用例逻辑完全相同,区分不出彼此**(§7.3)。
10. **`RetryPolicy` 的两套退避表中,冻结契约只测了代码自己标注为"legacy"的那一套**(§7.3)——不算错误,但值得下次改动 `service.py` 重试逻辑时留意。

---

## 附:关键引用位置速查

- Zenoh key space:`src/hyprial/transport/keys.py`
- 现存五个 schema:`src/hyprial/inbox/zenoh_link.py`(①)、`progress.py`(②)、`pull.py`(③)、`src/hyprial/squire/addressing.py`(④⑤)。⑥`hyprial-resource-manifest/v1` 已删除,历史存档见 git 历史(`src/hyprial/proto/mapping.py`,已随整包删除)
- daemon IPC:`src/hyprial/cli.py:361-601`、`src/hyprial/mcp/unix.py`、`src/hyprial/daemon/application.py:1707-1801,1802-3387`
- channel protocol:`src/hyprial/contracts/channel.py`、`src/hyprial/mcp/channel.py`、`src/hyprial/daemon/application.py:2155-2365,5443-5528`、`src/hyprial/daemon/desired_state.py:142-219`
- 演进反例(786d5ab 事故):`contract/e2e/run-upgrade-continuity.py:544-673`、`tests/test_upgrade_continuity_runner.py:68-`
- 契约测试:`contract/wire/golden/{expectations,envelopes}.json`、`src/hyprial/contracts/wire.py`、`tests/test_wire_contract_adapter.py`、`contract/wire/golden/validate.py`
- 相关既有产出:`docs/reference/naming-audit-channel-adapter-route.md`(PR #264,channel/adapter/route 命名审计)
