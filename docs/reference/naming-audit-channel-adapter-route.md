# 命名审计:channel / adapter / route

- 状态:分析文档,**不含任何重命名**;改名方案需 Allen 终审
- 范围:`src/hyprial`、`docs/`、`tests/`、`.agents/`(含 skills)、`.claude/`(本仓无 vendor 内容)、`contract/`、`notes/`、`scripts/`
- 方法:只读代码阅读 + 隔离环境验证(未触碰生产 daemon PID 36687 / `~/.hyprial`)
- 已确认的原始出现量(Allen 提供,直接引用):

  ```
             src/hyprial   docs   tests
  channel      501       77     626
  adapter      901      128     840
  route        356       43     368
  ```

## 0. 结论先行

1. **`channel` 一词在代码里至少对应四个互不相同的概念**(比 PAC 里列的三个还多一个),分别是:
   - **[A]** `channel:<owner>:<machine>:<name>` —— Lark/飞书的**入站身份 / 转发适配器地址前缀**(`hyprial message targets` 里的 `channel:lark:hq-adjutant-feishu` 就是这类)。
   - **[B]** `claude-channel` / `mcp/channel.py` / `contracts/channel.py` / `worker_channel.py` —— **交互式会话的唤醒/心跳协议**,与 IRC、飞书都无关。
   - **[C]** `proto/mapping.py`/`codec.py`/`capabilities.py` —— **真实的 IRCv3 行协议编解码器**,代码是真的,但**没有接入任何生产收发路径**(见 §2)。
   - **[D]** `route:<adapter>:<route>` / `TARGET_KIND_CHANNEL_ROUTE="channel_route"` —— **投递路径**概念,已经是"adapter 上的一条命名会话",但常量名里又混进了 `channel` 字样。
2. **Allen 的裁定(channel=IRC,adapter=飞书转发)方向是对的,但不完整**:它能覆盖 [A] 与 [C] 的冲突,但**没有覆盖 [B]**——`claude-channel` 是一个独立、活跃、生产在用的概念,既不是 IRC 也不是"外部平台接入",简单套用二选一裁定会把它放错桶。这一条建议 Allen 单独裁一次(§6)。
3. **hyprial 目前没有真正意义上的 IRC channel**:IRC 行协议代码是真实、规范的(用 `irctokens` 库),但只被一个**契约测试用的合成适配器**(`contracts/wire.py`)和纯单元测试调用,从未进入生产收发路径;生产路径是 Zenoh 上的 JSON 信封(`hyprial-delivery-frame/v1`)。`hyprial channel list/join/part` 这三个 CLI 子命令是**硬编码桩代码**,不读写任何存储(见 §3)。
4. **`hyprial channel list` 列的既不是 [A] 也不是 [B]**,它是 [C](已退役的 IRC 频道成员管理)的空壳。生产返回 `{'channels': []}` 是**必然结果**,不是数据碰巧为空——handler 里根本没有查询语句。
5. **`channel:lark:*` 的爆炸半径比表面看起来的更聚焦、但不是零**:当前 `dev` 分支的代码里,`channel:lark:*` **不能**用于任意新消息发送(`_require_deliverable_send_target` 会以 `UNSUPPORTED_TARGET` 拒绝);它只在**关联回复(correlated reply)**路径上有效。真正能对飞书发**新**消息的形态是 `route:<adapter>:<route>`。这与生产 daemon 的既有事实(`targets` 里出现了 `channel:lark:hq-adjutant-feishu`)之间存在**未解释的落差**——`cli-consol` 分支已独立发现同一落差并同样未能在只读边界内解释(见 §4)。
6. **`~/.hyprial/config.json` 判定属实**,且是同一类"TS 遗留物仍在制造误解"问题;警告文案确实没有告诉用户"你的 `settings.json` 仍在正常生效",这是一个真实但轻微的 UX 缺口(见 §5)。
7. **与 `cli-consol` 分支的重叠**:该分支已经独立做出与本文档一致的判定(`hyprial channel *` 是空壳、`channel:lark:*` 与 `channel list` 不是同一回事),并给出了退役建议。**两份文档结论一致,未发现矛盾**(见 §7)。

---

## 1. 对照表:出现位置 → 当前用词 → 实际语义 → 按裁定应叫什么

按语义分桶,每类给代表性位置(非穷举全部 ~3500 处)。

### [A] Lark/飞书转发适配器地址(裁定:应改叫 `adapter`)

| 位置 | 当前用词 | 实际语义 | 证据 |
|---|---|---|---|
| `src/hyprial/daemon/identity.py:55-74` | `CHANNEL_URI_PREFIX = "channel:"`, `parse_channel_uri()` | 通用"adapter 地址"前缀,注释自称 *"adapter-address prefix"* | docstring 原文即用 adapter 一词描述自己 |
| `src/hyprial/persistent_config.py:33-146` | `ChannelRouteConfig` / `LarkGatewayConfig` / `ChannelConfiguration` | `channels.json` 的 schema,**100% 是 Lark 专属数据** | `LarkGatewayConfig.from_json` 断言 `provider == "lark"` |
| `src/hyprial/adapter_registration.py:1-92` | `channels.json`,`_load_existing(channels_path)` | 注册/注销 **Lark adapter** 的落盘文件 | 模块本身叫 `adapter_registration.py` 却写 `channels.json` |
| `src/hyprial/adapters/lark/adapter.py:20-34`,`worker.py:800-810` | `channel_actor_id=f"channel:lark:{name}"` | Lark 入站消息的发送方身份铸造点 | — |
| `src/hyprial/adapters/lark/reply_bridge.py:1-40` | `_LARK_CHANNEL_PREFIX`,`lark_reply_adapter()` | "channel actor 是入站身份,不是通用出站地址" | 模块 docstring 原文 |
| `src/hyprial/squire/profile.py:177,221-229,568-574` | 字段 `squire_channel`,只读属性 `squire_adapter` | **代码里已经有一次半吊子改名**:属性对外叫 `adapter` 是"因为 IRC channel 是另一个概念",但底层字段和 URI scheme 仍是 `channel:` | 属性 docstring 原文点破了这次审计要解决的问题 |
| `contract/wire/golden/envelopes.json` | `"channel:alice:mac:feishu-main"` | 测试夹具把 [A] 语义硬编码进 wire-golden | — |
| `docs/reference/p4-squire.md:36,55-88` | `--channel channel:gregpaws:mac-studio:squire` | Squire 绑定 CLI 参数 | "最后一段必须命名一个已配置的 adapter" |

### [B] `claude-channel` 交互式会话唤醒机制(裁定:**待 Allen 单独裁**,不属于 IRC 也不属于 adapter)

| 位置 | 当前用词 | 实际语义 | 证据 |
|---|---|---|---|
| `src/hyprial/cli.py:2869-2924` | `mcp_app.command("claude-channel")`,`claude-channel-recover` | 启动/恢复一个 Claude Code 拥有的 stdio Channel server | docstring: "Run one Claude Code-owned stdio Channel server" |
| `src/hyprial/mcp/channel.py`(全文件) | `CHANNEL_CAPABILITY="claude/channel"`,`ClaudeChannelDriver`,`ClaudeChannelAdapter` | 交互式 Claude 会话的唤醒边缘 + 会话级 daemon 适配器 | 模块 docstring 自己把 "Channel" 和 "adapter" 两个词焊在一起 |
| `src/hyprial/contracts/channel.py`(全文件) | `CHANNEL_PROTOCOL_VERSION`,心跳常量,`channel_generation()` | 交互式 MCP Channel 的心跳/存活遥测契约 | docstring: "Additive, non-secret telemetry contract for interactive MCP Channels" |
| `src/hyprial/daemon/desired_state.py:142-146,262-275,517-534` | `channel_confirmed`,`channel_build_version`,`channel_pins`(已死) | `DesiredState.interactive_sessions` 的字段前缀,与 [A][C] 无关 | — |
| `src/hyprial/harnesses/worker_channel.py` | `WorkerChannel`,`build_worker_channel` | Claude/Codex/Pi 无头 worker 共用的 harness-bridge MCP 身份载体 | — |
| `docs/design/design-harness-capability-abstraction.md:113,121-201`(2026-08-22) | "cc channel" | "cc channel 是 Claude.ai 登录专属的 research preview 机制" | 与 2026-08-07 的"channel=IRC"裁定同名冲突,且是**更晚**写的文档 |

### [C] 真实 IRC 协议代码(裁定:命名正确,但**代码本身未接入生产**)

| 位置 | 当前用词 | 实际语义 | 证据 |
|---|---|---|---|
| `src/hyprial/proto/mapping.py`(全文件) | `envelope_to_lines`/`lines_to_envelope`(别名 `envelope_to_irc`/`irc_to_envelope`),`_channel_target()` | hyprial 信封 ⇄ IRCv3 行的双向映射,`_channel_target` 把 `channel:` 地址转成真实 IRC `#name` | 用 `irctokens` 库,规范、无网络 |
| `src/hyprial/proto/codec.py`,`capabilities.py` | `LineCodec`,IRCv3 `CAP` 词表 | sans-I/O 的行编解码 + 能力协商词表 | — |
| `src/hyprial/cli.py:63-64,5643-5690` | `channel_app = typer.Typer(help="Manage IRC channels.")` | `channel join/part/list` 三个子命令 | 参数强制以 `#` 开头 |
| `src/hyprial/daemon/application.py:3001-3011` | `channel.join/part/list` RPC handler | **硬编码桩**,`list` 直接返回 `{"ok": True, "channels": []}`,`join/part` 只回显参数 | 注释自陈:"IRC channel-pin 分支...被零个生产条目使用" |
| `docs/design/design.md:68` | 命名词表 v2(2026-08-07 12:09) | `channel = IRC 频道`,Allen 方向 | 与同文档 §8(11:56)矛盾,见下 |

### [D] "route" 投递路径(裁定:概念本身正确,常量名需要顺手清理)

| 位置 | 当前用词 | 实际语义 | 证据 |
|---|---|---|---|
| `docs/reference/route-outbound.md` | `route:<adapter>:<route>` | "route: 到达一个会话空间;user: 到达一个人" | — |
| `src/hyprial/daemon/identity.py:115,168-179` | `TARGET_KIND_CHANNEL_ROUTE = "channel_route"` | 分类器给 `route:` 地址的 kind 名——**又把 `channel` 焊回了一个纯 [D]/[E] 概念上** | 同一段注释里专门写"`channel:` reply-bridge 地址不是这个,分类为 unknown" |
| `src/hyprial/adapter_registration.py:46,124-168` | `RouteInput` | "一条直连路由:友好名 → 原生 chat/user id" | — |
| `src/hyprial/daemon/application.py:124-436,1000-1048` | `route_delivery`,`_route_gateways`,`_interactive_routes`,`_persona_routes` | 同一个类里至少三种不同的"route"字典(adapter 路由 / 交互会话路由 / persona 路由) | — |
| `docs/notes/design-logical-addressing.md:33-38` | `route:`(adapter 网关上的命名会话) | 设计层定义,与同句里的 `channel:` 明确区分 | — |

### [E] `adapters/lark` 通用集成点(裁定:命名基本正确,但"adapter"自身也有次要重载)

| 位置 | 当前用词 | 实际语义 | 备注 |
|---|---|---|---|
| `src/hyprial/cli.py:63` | `adapter_app = typer.Typer(help="Manage external-platform adapters.")` | `hyprial adapter add/onboard/.../list/doctor/...` | 与 Allen 的裁定完全一致,是主战场 |
| `src/hyprial/adapters/lark/runtime.py` | `AdapterSupervisor` | Lark 长连接进程生命周期管理 | 一致 |
| `src/hyprial/transport/zenoh.py:1` | "Synchronous Zenoh **adapter**..." | 这是 GoF 意义上的"适配器模式",与飞书无关 | 次要重载,英语通用词义,优先级低 |
| `src/hyprial/contracts/wire.py:1-9` | "Python execution **adapter**" | 契约测试执行器,又是第三种"adapter" | 同上,优先级低 |

### [F] 单点已发现的自相矛盾 / 融合

- `docs/design/design.md` **第 68 行与第 123 行自相矛盾**:命名词表 v2(2026-08-07 12:09)裁 `channel=IRC`;同文档 §8"已裁"记录(2026-08-07 **11:56**,时间更早但排版在后)却写"room=IRC 频道(改名避撞),channel=飞书 app 接入桥(用法不变)"——两条记录一天之内互相打架,而且文档末尾"待裁"清单第 1 条明确写着"命名词表(channel/room/link)确认"仍未定案。**也就是说,这场命名混乱从 2026-08-07 设计稿诞生的那一刻起就没被最终拍板过**,今天(2026-08-24)Allen 的裁定实际上是在替 2026-08-07 遗留的"待裁项"补票,而不是纠正一个曾经明确的决定。
- `src/hyprial/daemon/identity.py:101-108`("Allen's four object types"注释)把第三类对象直接写成 **"adapter (channel route)"**——代码里最权威的一段注释自己就把 adapter 和 channel route 焊在一起。
- `src/hyprial/mcp/channel.py` 模块 docstring 同时用 "Channel" 和 "daemon adapter" 描述同一个东西,类名直接叫 `ClaudeChannelAdapter`——[B] 类概念自己也在用 "adapter" 字样,说明"adapter"一词已经先于 Allen 的裁定被工程师们用来描述"某种协议边缘/胶水层",不是飞书专属。

---

## 2. 我们有没有真正的 IRC channel?—— 没有

**判定基于代码,不基于印象。**

- `src/hyprial/proto/mapping.py`(用 `irctokens` 库)、`codec.py`、`capabilities.py` 是**规范、可用的 IRCv3 行编解码器**——这部分不是摆设,是认真写的 sans-I/O 代码。
- 但全仓库搜索 `envelope_to_lines`/`envelope_to_irc`/`lines_to_envelope`/`irc_to_envelope` 的调用方,只有两处:
  1. `src/hyprial/contracts/wire.py:146-148` —— 一个**契约测试用的合成适配器**,模块 docstring 自陈"这是刻意做成 scenario adapter,不是协议的第二套实现";
  2. `tests/test_proto.py` —— 纯单元测试,内存里喂字节串,不涉及网络。
- 生产收发路径(`src/hyprial/daemon/application.py:1119-1128`、`src/hyprial/inbox/zenoh_link.py:27-51`)构造的是 **JSON 信封**(`{"schema": "hyprial-delivery-frame/v1", ...}`),base64 承载 payload,走 Zenoh pub/sub——**完全不经过 IRC 行编码**。
- 全仓库搜索 `irc.connect`、端口 `6667`/`6697`、`ngircd` 等真实 IRC 服务器/客户端特征,**零命中**。`irctokens` 只被当作行格式解析/构建库使用,从未建立过真实网络连接。
- `NOTICE` 命令(标准 IRC 命令)全仓库零命中——进一步说明这是"挑选 IRCv3 的一个子集来做信封线格式",不是真实现完整 IRC 协议。
- `hyprial channel join/part/list` 这三个唯一可能对外暴露"IRC 频道"语义的 CLI 命令,daemon 侧 handler(`application.py:3001-3011`)是硬编码桩:`list` 直接 `return {"ok": True, "channels": []}`,`join`/`part` 只做参数类型校验后原样回显,**不写任何存储、不调用 `proto.mapping` 的 JOIN/PART 构造器、不影响任何投递路径**。

**结论**:今天的 hyprial 有"IRC 语法层"这一份**独立、规范、但未接线的序列化代码**,以及一组**从未接线的 CLI 桩命令**。没有任何一条生产消息真的以 IRC 行的形式在网络上跑过。如果要重新激活这个方向,需要新写一整条"CLI → daemon handler → 真实成员表 → 投递路径"的链路,而不是简单地"把名字对上"。

---

## 3. `hyprial channel list` 到底列什么

**验证过程**:读 `src/hyprial/cli.py:5684-5690`(`channel_list` 只发 `channel.list` RPC)→ `src/hyprial/daemon/application.py:3001-3011`(handler 硬编码返回空数组,不查任何表)。`cli-consol` 分支的隔离黑盒实测(`docs/cli-command-consolidation.md`)同样证实:先 `join #probe` 再 `list`,`list` 仍然是空——**这不是"暂时没数据",是这条代码路径根本不读写任何状态**。

**它列的是概念 [C](已退役的 IRC 频道成员关系),既不是 [A] 也不是 [B]**:

- 不是 [A]:`classify_target_identity()`(`identity.py:159-179`)的文档注释明确写"`channel:<adapter>:<name>` reply-bridge 地址不是投递目标——发给它们会被拒绝——所以在这里分类为 `unknown`,不是 `channel_route`"。也就是说 `channel:lark:*` 这类地址**从设计上就被排除在任何"channel 枚举"接口之外**,`targets` 用另一套完全独立的分类器(`agent`/`user`/`channel_route` 三种可投递 kind),`channel_route` 对应的实际前缀是 `route:`,不是 `channel:`。
- 不是 [B]:`claude-channel` 走的是完全不同的 daemon 方法(`session.register`,`src/hyprial/mcp/channel.py`),持久化在 `DesiredState.interactive_sessions`,与 `channel_app` 所在的 Typer group、与 `channel.list` RPC 之间**没有任何共享代码或存储**。

**所以 CLI 里的这个"channel"和 targets 里的那个"channel:lark:"确实不是一回事**——这正是 PAC 里怀疑的方向,现已用代码证实。用户看到 `targets` 里有 `channel:lark:hq-adjutant-feishu`,再去跑 `hyprial channel list` 却是空的,产生的困惑是**命名撞车的直接后果**,不是 bug 触发的偶然现象。

**一个尚未解决的旁支发现**:当前 `dev` 分支的分类器和发送网关(见 §4)认为 `channel:lark:*` 不该出现在 `targets` 里(应归 `unknown`,且 `route:<adapter>:<route>` 才是可投递形态),但 PAC 给出的**生产实测**明确显示 93 个 target 里**确实**含 `channel:lark:hq-adjutant-feishu`。这意味着生产 daemon 的 target 形态与当前 `dev` 代码不一致——可能是生产版本落后于 `dev`,也可能是迁移残留。`cli-consol` 分支已独立发现同一落差,同样在只读边界内无法进一步定性(该分支文档 §4.2 原话:"任务给定的生产输出...还暴露了第二个问题:生产 daemon 的 target shape 与当前 dev 不一致")。**建议**:在正式启动任何 `channel:`/`adapter:` 改名工作之前,先确认生产 daemon 实际运行的版本/commit,否则任何"这个前缀现在的行为是 X"的假设都可能对不上生产实况。

---

## 4. `channel:lark:*` 的爆炸半径:比表面更聚焦,但依然是破坏性变更

PAC 原文认为 `channel:lark:*` 是"目前唯一能给飞书发消息的形态"。**读当前 `dev` 分支代码后,需要做一处重要澄清**(不改变"这是破坏性变更"的结论,但改变了破坏的具体形状):

- `src/hyprial/daemon/application.py:5329-5352`(`_require_deliverable_send_target`)是 `hyprial message send` 的准入闸门:对**任意新消息**发送,`channel:lark:*` 会被 `classify_target_identity()` 分类为 `unknown`,直接以 `UNSUPPORTED_TARGET` 拒绝——错误信息原文列出的可投递形态是 `agent:<owner>:<machine>:<agent>`、`user:<owner>`、`route:<adapter>:<route>`,**不包含 `channel:`**。
- `channel:lark:*` 真正生效的唯一路径是 `src/hyprial/adapters/lark/reply_bridge.py` 里的 `LarkReplyBridgeTransport`:它拦截**关联回复**(`message.intent == "reply"` 且带 `replyTo`),把回复投递给"曾经给我发过消息的那个飞书身份",完全绕开上面的准入闸门。模块 docstring 原文:"channel actor 是入站身份,不是通用出站地址;唯一合法的出站操作是一次关联回复"。

**结论**:`route:<adapter>:<route>` 才是"给飞书发一条新消息"的现行形态;`channel:lark:*` 是"回复一条飞书发来的消息"时,系统内部用来记住"该回给谁"的身份标识。这两者当前被 Allen 在 PAC 里并列成"发消息的两种形态",但代码层面它们服务于**不同的操作**(新发 vs 关联回复),不是同一操作的两种拼写。

**这不代表可以放心改名**——恰恰相反,它把破坏面说清楚了:

- 会被打断的是:**所有正在进行中的"agent 回复一条飞书群/DM 消息"的对话**(harness_reply 路径)、`squire_channel`/`squire_adapter` 绑定校验(`squire/profile.py:568-574` 的 URI 前缀正则)、`channels.json` 的磁盘 schema(`persistent_config.py`)、以及**任何硬编码匹配 `channel:lark:` 前缀字符串的 agent/skill/脚本**(reply_bridge.py 的 `_LARK_CHANNEL_PREFIX = "channel:lark:"` 就是一处硬编码字符串比较,不是结构化解析)。
- 不会直接打断的是:`hyprial message send --to route:...` 这条主发送路径本身(它已经用 `route:`,不用 `channel:`)。
- 但**在完全确认生产 daemon 版本、且确认上面的 `dev`/生产落差(§3 结尾)之前,不能排除生产环境里 `channel:lark:*` 承担着比 `dev` 分支更广的职责**(比如生产版本可能还没上线 `route:` 这套投递路径,`channel:lark:*` 是唯一发送形态)。**这正是为什么本轮任务边界只出清单、不动标识符**。

---

## 5. `~/.hyprial/config.json`:复核结果——判定属实

逐条复核 Allen 给出的判定:

- **只读一处,只为警告**:`src/hyprial/updates.py:240-249`(函数 `retired_track_warning`)是全 Python 代码库里唯一读取 `config.json` 的地方。它把文件解析成 dict 后,只检查是否存在 `_RETIRED_TRACK_KEYS = ("updateTrack", "update_track")` 这两个键——**解析出的字典内容本身从未被使用去做任何决策**。测试 `tests/test_updates.py:423-432` 的名字本身就是断言:`test_version_includes_retired_track_warning_without_obeying_it`。
- **一处小澄清**:Allen 列出的 `natsUrl`/`credsPath`/`enrollUrl`/`stateDir`/`autoUpgrade`/`versionCheckIntervalMs` 这些键,在**当前**代码里完全搜不到任何读取逻辑——现有的 `retired_track_warning` 只检测 `updateTrack`/`update_track` 这两个键,不检测其余几个。这些键名确实是 TS 版遗物(git 历史里能找到 `enrollUrl`/`natsUrl` 相关提交),但它们已经从 Python 侧代码里被**完全删除**,不是"读了但忽略",而是"连读取代码都不存在了"。二者结论一致(都是死的),但死法不同,写文档时应精确到"Python 侧只检测 updateTrack/update_track 键的存在性"。
- **活的是 `settings.json`**:`src/hyprial/updates.py:175-203`(`read_update_track`)解析 `settings.json` 的 `updateTrack`,校验合法值(`dev`/`nightly`/`stable`),并在 `src/hyprial/cli.py:4280-4281` 真正驱动 `_perform_upgrade` 的目标 tag 解析。契约测试(`contract/e2e/run-upgrade-continuity.py`)端到端验证了这条链路。
- **警告文案确实有 UX 缺口**:用户能看到的唯一文案是字面量 `"update track keys in config.json are retired and ignored"`。它**只说"config.json 的 track 键被忽略"**,不提"你的 settings.json 的 track 仍在正常生效中"——这句安心话只存在于 `updates.py:209` 的开发者可见 docstring 里(`"settings.json updateTrack is the live track selector; everything else is residue"`),从未透出到用户可见的输出。一个只设置了 `settings.json`、但机器上还残留一个带 `updateTrack` 键的旧 `config.json` 的用户,看到这条警告后完全有理由怀疑"我的 track 是不是没生效"——Allen 的猜测成立。
- **这是刻意设计,不是疏漏**:`docs/design/design.md:91`(2026-08-20,Allen 裁定的三轨升级方案)原文写明"蛇形 `update_track` 与 `config.json` track 键仍为退役残留仅警告"——即"只警告不遵从"是**明确决策**,本次复核只指出**文案措辞**可以更清楚,不是说这个行为本身有问题。
- **旧 TS 状态转换已退役**:2026-09-11 确认旧 TS 机器已无，原转换器与组合清理脚本已经删除。若意外发现旧安装，按当前运维指南归档旧 home 后全新安装；`config.json` 不会进入新 home。

**建议收入文档/后续任务**:改一句警告文案,在提到"config.json 的 track 键被忽略"的同时,补一句"当前生效的 track 来自 settings.json,值为 `<X>`"。这是纯文案改动,风险为零,可以独立于本次 channel/adapter 审计单独走一个小 PR。

---

## 6. 关于"你有权推翻 Allen 的裁定"——部分推翻:[B] 需要单独裁一次

Allen 的原始裁定是二选一:"channel = IRC"、"adapter = 飞书转发"。代码证据支持把 [A] 判给 adapter、把 [C] 判给 channel(这条裁定本身是对的),但**代码里还有一个裁定没有覆盖到的、活跃且生产在用的第三种"channel"**——[B] 交互式会话唤醒机制(`claude-channel`)。

- 它不是 IRC:没有 `#频道`、没有成员语义、不经过 `proto/mapping.py`。
- 它也不是"外部平台接入"意义上的 adapter:它不对接飞书或任何外部平台,对接的是**本机的一个交互式 Claude Code 进程**。
- 它是活代码:`mcp/channel.py`、`contracts/channel.py`、`worker_channel.py`、`cli.py` 的 `claude-channel`/`claude-channel-recover` 命令、`DesiredState.interactive_sessions` 都在为它服务,`.agents/skills/hyprial-e2e/SKILL.md` 里的 E2E-008 网关测试也依赖它的 `channelConfirmed` 字段。

**没有找到"任何带会话语义的传输通道"这种广义抽象**——审计过程中专门检查了 `src/hyprial/transport/` 和 `src/hyprial/contracts/` 有没有 `class Channel`/`ChannelProtocol` 这类通用接口,结果是没有;`ChannelNotifier(Protocol)` 只在 [B] 的唤醒驱动链路里窄用,不是跨概念的基类。所以"channel 是不是一个比 IRC 更一般的抽象"这个问题的答案是:**不是一个正式的通用抽象,但确实是一个独立于 IRC 和 adapter 之外的第三个真实概念**,原裁定的二分法本身需要扩成三分法。

**建议**:请 Allen 单独就 [B] 裁一次——可选项包括"继续叫 channel"(英语里"a channel"本来就有"一条通信/控制通路"的通用含义,只要与 [A][C] 不再用同一个词根冲突即可)、或改叫 `session`/`link`(`docs/design/design.md:68` 的 v2 词表里 `link` 被预留给"zenoh 链路",若要用给 [B] 需要先确认不会跟那个预留冲突)。

---

## 7. 与 `cli-consol` 分支的重叠核对——未发现矛盾

`wk/cli-consol` 分支(`docs/reference/cli-top-level-command-consolidation.md`)独立做了以下判定,与本文档结论一致:

- `hyprial channel *` 三个子命令是无状态桩,`list` 恒为空,`join`/`part` 只回显参数,不接入 `proto.mapping` 的 IRC 构造器。
- `channel:lark:*` 与 `channel list`/`targets` 里的 `channel_route` 不是一回事;`channel:*` reply-bridge 地址被分类器判为 `unknown`。
- 同样发现"生产 daemon 的 target 形态与当前 dev 不一致"这一落差,同样因只读边界未能进一步定性。
- 建议:`hyprial channel list|join|part` 走弃用警告 → 下个发行隐藏 → 观察后删除;如果未来真的要实现 IRC 群组成员关系,换一个不与 `channel:` 地址体系冲突的名字(该分支举例 `hyprial irc room join|part|list`),最终名字留给 Allen。

**结论:两条线的判断相互印证,没有冲突建议**。本文档补充的增量是:(1) 把 [B] 单独提出来作为需要 Allen 另裁的第三概念;(2) 给出 §4 的"新发 vs 关联回复"区分,细化了 `channel:lark:*` 改名的爆炸半径;(3) 覆盖了 `cli-consol` 未涉及的 `config.json`/`settings.json` 警告文案问题。若 `cli-consol` 后续正式提交其弃用 PR,可以直接引用本文档 §2/§3 作为"确实没有真实 IRC 语义在跑"的独立佐证。

---

## 8. 迁移建议(按破坏性分级,均为建议,本轮不执行)

### Tier 0——零风险,可随时做(纯文档/纯内部标识符,不碰任何持久化 schema 或 wire 字符串)

- 把 `docs/design/design.md` 第 68 行与第 123 行的自相矛盾标记为"已被 2026-08-24 裁定取代",避免未来的人再读到互相打架的两条记录。
- 修 `updates.py` 的警告文案,补一句"settings.json 的 updateTrack 仍在生效,当前值为 X"(§5)。
- `docs/design/design-harness-capability-abstraction.md` 里的"cc channel"措辞加一条脚注,说明这是 [B] 概念,不是 IRC。
- 纯 Python 内部重命名且不出现在任何 JSON/CLI/日志字符串里的标识符(例如仅在函数体内部使用的局部变量),风险为零,但收益也小,可以顺手做,不必单开 PR。

### Tier 1——低风险(内部类名/模块名,可能出现在 traceback 或开发者日志里,但不是持久化 schema 的一部分)

- `persistent_config.py` 里的 `ChannelConfiguration`/`ChannelRouteConfig`/`LarkGatewayConfig` **类名**可以改(比如 `AdapterConfiguration`),只要不改它们序列化出的 **JSON 键名**和文件名 `channels.json`。类名改动只影响 Python import 和 traceback,不影响任何持久化文件或 wire 协议。
- `daemon/identity.py` 的 `TARGET_KIND_CHANNEL_ROUTE` **Python 常量名**可以改,但它的**字符串值** `"channel_route"` 是 CLI/API 可见的(`hyprial message targets` 的行的 `targetKind`,以及 daemon `targets` API 的 `kind` 参数);值本身要不要改属于 Tier 2。R2 已退役 CLI 的 `--kind channel_route` 过滤器(路由配置看 `adapter route list`),但行的 `targetKind` 值不变。

### Tier 2——中风险(CLI 参数值、日志字段名、API 响应字段——外部脚本/测试可能已经在匹配这些字符串)

- `hyprial channel list|join|part` 命令组退役(`cli-consol` 已给出弃用→隐藏→删除的三步计划,建议直接采用,不要另起一套)。
- `--kind channel_route` 这个 CLI 过滤值、`targetKind: "channel_route"` 这个 API 字段值,如果要改成 `adapter_route` 之类,需要双写/双收期(先同时接受两个值,发一个版本观察调用方,再收敛)。
- `.agents/skills/hyprial-e2e/SKILL.md` 里 `channelConfirmed` 这类 E2E 断言字段,如果上游概念改名,测试断言要同步改,否则会静默变成"断言了一个不再存在的字段名"式的假绿。

### Tier 3——高风险 / 破坏性变更(需要 Allen 亲自定新名、需要兼容期、需要迁移脚本——**本轮不建议动**)

- `channel:` 地址前缀本身(`CHANNEL_URI_PREFIX`、`channel:lark:*`、`channel:<owner>:<machine>:<name>`)。这是:
  - **持久化数据**:每台机器的 `channels.json`、`desired-state` 里都可能存有这个前缀的字符串;
  - **wire 上的字符串比较**:`reply_bridge.py` 用裸字符串前缀匹配(`_LARK_CHANNEL_PREFIX = "channel:lark:"`),不是走统一的 URI 解析入口,改名必须同时改所有做裸字符串匹配的地方,遗漏一处就是静默失效,不是报错失效;
  - **外部使用者未知**:任何 agent/skill/个人脚本只要硬编码过这个前缀去关联回复,都会在改名瞬间失效,而且大概率是"消息发出去但对方收不到回复"这种没有报错的静默失败,而不是显式报错——这是最危险的失效模式。
  - **落地方式建议**(仅供 Allen 参考,不代表本轮结论):新前缀(例如 `adapter:lark:<name>`)与旧前缀 `channel:` 双写/双收至少一个发行周期,`parse_channel_uri` 同时接受两种前缀并在解析层归一化,`hyprial doctor` 加一条检查项提醒仍在使用旧前缀的持久化文件,设定明确的旧前缀下线日期后再摘除兼容代码。
- 在确认生产 daemon 版本、解决 §3/§4 提到的"生产 target 形态与 dev 不一致"之前,**任何**关于 `channel:lark:*` 当前真实职责范围的结论都应视为"以 dev 分支代码为准,生产可能不同",不要把本文档的 dev 分支读码结果直接当成生产事实来做决策依据。

---

## 附:关键引用位置速查

- IRC 编解码:`src/hyprial/proto/mapping.py`、`codec.py`、`capabilities.py`
- IRC CLI 桩:`src/hyprial/cli.py:63-64,1754-1759,5643-5690`;handler:`src/hyprial/daemon/application.py:3001-3011`
- Lark 转发地址:`src/hyprial/daemon/identity.py:55-74,159-190`、`src/hyprial/adapters/lark/{adapter,worker,reply_bridge}.py`、`src/hyprial/persistent_config.py`、`src/hyprial/adapter_registration.py`
- 交互式唤醒:`src/hyprial/mcp/channel.py`、`src/hyprial/contracts/channel.py`、`src/hyprial/harnesses/worker_channel.py`
- 发送准入闸门:`src/hyprial/daemon/application.py:2619-2713,5329-5352`
- 升级轨道/legacy config:`src/hyprial/updates.py:175-252`、`docs/design/design.md:91`；旧 TS 转换路径已于 2026-09-11 退役
- 设计史:`docs/design/design.md`(2026-08-07)、`docs/notes/design-logical-addressing.md`(2026-08-16)、`docs/reference/p4-squire.md`(2026-08-16)、`docs/design/design-harness-capability-abstraction.md`(2026-08-22)
- 重叠对照:`wk/cli-consol` 分支 `docs/reference/cli-top-level-command-consolidation.md`
