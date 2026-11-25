# user 与 IM 身份绑定的清除：第二档（会改变运行行为的部分）

状态：**设计草案（第 8 节），以 Allen 2026-11-19 的更正与裁定（第 7 节）为前提；出站第 2 种情况归 #2712 的 cell 切片；第 9 节目前没有待裁项。清单（第 3 节）据 GUI 线的只读清查填入，并在当前 dev 上复核。第一档已合入（#2955）；实施排在群聊之后。**
日期：2026-11-18 建，2026-11-19 填写清单与设计，2026-11-20 按后续裁定与实际落地修订（见下一节）。负责：conversation 线。清查与第一档：GUI 线。

## 2026-11-20 修订：后续裁定与实际落地

本稿的设计部分写于 2026-11-19，其后 owner 又做了几项裁定，R1、R2、R4 也已合入 dev。下文凡与本节冲突之处，以本节为准。正文里被取代的原文没有删，各处加了「已被取代」注记，原来的决定仍可读。

状态（2026-11-21）：本轮四片全部合入 dev：R1（#2970）、R2（#2989）、R3（#3002，删除身份解析器与绑定证明链）、R4（#2991，迁移说明）。留给后续的事项见本节末的「仍未完成」，都不属于本轮。

### 后续裁定（Allen，2026-11-19）

- 关于 daemon 发给用户的消息：
  > 「我建议直接删掉daemon发送到用户的这条路径，全部改为走log就好，需要的时候，agent使用log去获取」
- 关于 workflow、routine 与升级失败提醒：
  > 「workflow和routine等PAC本来就有agent owner，走owner的通道即可。daemon起不来时的升级失败提醒我建议也删除。这个口子没必要开，容易误导，有需要可以未来在daemon log上再增加常规的IMAP等转发渠道，现在不做」
- 关于批量读取时来源信息里不认识的字段：
  > 「同意，来源信息里有不认识的字段时，把那些字段丢掉」

另有 2026-11-19、11-20 的几项裁定，本稿只记结论：每台机器的用户 agent 各自独立，OrgFS 建立链接之后再合并，合并这一步尚未设计；send-link 组件与 thread 这个名词由 cell 模型线删除；读主机名的架构测试已删除（#2982），node id 改读 home 里的文件（#3008）。

### 被取代的陈述

| # | 本稿原文所在位置 | 原来的说法 | 现在的做法 | 实现 |
|---|---|---|---|---|
| 1 | 7.6 第 3 条；8.1「升级失败提醒」；8.5 迁移说明规格；8.7 R2 行；9.2 第 3 行 | daemon 起不来时的升级失败提醒发到各 adapter 的默认路由 | 删除（Allen 推翻原裁定）：daemon 起不来时不向任何 IM 发消息；失败看 `hyprial daemon upgrade` 的输出与退出码、`upgrade status` 的 `lastRun`、失败标记文件 | #2989 |
| 2 | 8.1「daemon 自己发给用户的消息」；8.5 表「daemon 自己的通知改走入队路径」；8.7 R2 行 | daemon 自己的通知改走入队路径，有 user-proxy 交给它，没有只在 GUI/CLI | 删除 daemon 到用户的这条推送路径。监控类通知（配额与收件箱看门狗、运维告警、OrgFS 通知、agent 被阻塞、模型提供方需重新登录、其他 owner 告警）只写日志：`warn` 级、component `daemon`，带全文 `text` 与 `idempotencyKey`；不入队、不进对话、不推送。事件名见迁移说明第 5 节 | #2989 |
| 3 | 8.1（同上一段） | routine、workflow、PAC 报告由 daemon 直接发 | 由负责的 agent 作为普通 agent 到 `user:<owner>` 消息发出（幂等键 `workflow-escalate:<id>`），推送按 8.1 的情况判定；找不到本机负责 agent 时不发送，只记 `workflow.user_report.unsent` | #2989 |
| 4 | 8.1「daemon 自己发给用户的消息」末句 | 重启通知「不再确认送达，只确认已记录」 | 重启前通知（IPC 方法 `autoupdate.notify`）删除；重启只在日志里留 `daemon.stopping`、`daemon.ready` 与 `autoupdate.run.*` | #2989 |
| 5 | 8.1 第 3 种情况的判定句 | 「没有 adapter 绑定」包括「发送方不是本机 agent（GUI/CLI、daemon 自己的发送）」 | 「daemon 自己的发送」一项不再存在，daemon 不发送；GUI/CLI 发送者仍属此类 | #2989 |
| 6 | 8.3「不受影响」一句 | 没有 pin 的 agent、GUI/CLI 发送者与 daemon 自己的通知都属第 3 种，有 user-proxy 就到飞书 | 去掉「daemon 自己的通知」，daemon 通知只在日志里。pin 了 adapter 的 agent 代发的 routine、workflow 报告同样落在空档里（测试 `test_a_report_from_an_adapter_pinned_agent_lands_in_the_accepted_gap`，登记为 KP-113） | #2989 |
| 7 | 8.3「投递条目的形状」 | 没推时只有 `pushCode` | 没推时日志 `send.user_push` 另带 `pushSkipped: sender-pinned \| no-user-proxy` | #2989 |
| 8 | 8.4 | 发送者块的字段名与批量读取 | origin 用 `chatKind`（取代 `chatType`）、`sender{kind, platformId, displayName}`、`provider`、`platformMessageId`、`platformCreatedAt`、`via`；批量读取（`message.pending.batch`）把存储的 origin 里当前 schema 不认识的字段丢掉，只改发布视图、不改存储；批量 origin schema 另加了 `mechanism` | #2970 |
| 9 | 8.4 转述证据；8.6 | 删除 `inbox/relay/`、`message.relay.verify`、`relay` 参数 | 同上，另删 MCP 的 `relay_message_id` 与 CLI 的 `--relay-message-id` | #2970 |
| 10 | 8.5 迁移说明规格（1 至 6 条）与 3.1 | 本稿列出的迁移说明要点 | 以 `docs/guides/migration-im-push-by-agent-binding.md` 为准。差别：`identityClaims` R1 之后不再写入、登出也不再清理，但解析器在 R3 之前仍读它，所以列在「现在还不要删」；`identity.sqlite3`、`users.sqlite3` 与两个缓存文件同理，不在「可删除」里；不再有升级提醒一项；「升级前在旧版本运行 `hyprial user bindings withdraw --all`」已核实 0.5.5.3.4 有此命令，R3 之前当前 dev 也有；迁移说明另有第 13 节 node id 文件，与本稿无关 | #2991 |
| 11 | 8.5 表「T2-E squire 私信……」行 | 删除 squire 私信、`--owner-open-id --binding-code` | 另删了 `hyprial squire setup --channel`（超出 R2 原定最小范围，#2989 的说明里已写明） | #2989 |
| 12 | 8.6 | 已发布接口的删除项 | 另删：IPC 方法 `autoupdate.notify`；升级结果的 `ownerAlert`、`restart.followUp.alert`、`survivorAlert`；失败标记的 `alert`；last-run 的 `notification`；启动警告 `SQUIRE_PROFILE_UNREADABLE`、`SQUIRE_RECEIVERS_UNAVAILABLE`、`SQUIRE_NOT_CONFIGURED` 改为 `SQUIRE_USER_PROXY_NOT_CONFIGURED`；`message.send` 响应里的 `IDENTITY_CONFLICT`、`IDENTITY_SOURCE_UNAVAILABLE`、`IDENTITY_UNBOUND`；delivery entry 去掉 `adapter` | #2989 |
| 13 | 3.3 第 3 条；5；9.2 末段「KP-110」 | 每台登录过的机器一个零 harness 的 `hyprial-user`（原文没有写它的 id 从哪来；代码里原为由 owner 派生） | 每台机器的用户 agent 是独立 agent：id 由 registry 像普通 agent 一样铸造，不再由 owner 派生；本机这一个靠 registry 里的标记识别，发给它的消息仍落到 `user:<owner>`；别的机器的用户 agent 的 id 形态地址按普通 agent 地址处理。合并这些实例留到 OrgFS 链接建立之后，尚未设计 | #2992 |
| 14 | 4（约束）；3.3 | 此前有读主机名的架构测试、按名字冻结 agent 名 | 读主机名的测试已删除（#2982）；node id 只读 `HYPRIAL_NODE_ID` 或 `<HYPRIAL_HOME>/node-id`（#3008）；冻结 agent 名的台账已删除（#3006）。这三项不改变本稿的设计，只改变上文提到的地址与名字的前提 | #2982、#3008、#3006 |
| 15 | 本稿多处提到 send-link、thread、squire | 作为现状引用 | send-link 组件、thread 这个名词与 squire 整体由 cell 模型线删除；本稿不展开，也不再改 | cell 模型线（未合入） |
| 16 | 8.6 末段 GLOSSARY | 随 R4 改 `user`、`squire`、`platform identity sync`、`platform identity lookup` | R4 只改了 `user` 与 `squire`（现状与两条私信路径那一句）；`platform identity sync`、`lookup` 在 R1 之后仍成立，未改，R3 时再看；`route` 随 cell 切片 | #2991 |

另有两项已登记的后续：`hyprial log --event` 事件名过滤与 GUI 日志页 `log.page` 的事件白名单（KP-114）。

### 仍未完成

- GUI 里依赖已删除方法的面板：由 GUI 线删除（R3 的契约已随 #3002 合入，迁移说明也已同步）。
- **出站第 2 种情况**（推到 agent 绑定的聊天）：属于 cell 模型线，连同 `channel_pins`、squire 的删除。落地之前，pin 了 adapter 的 agent 什么也不推。
- **已接受的空档**：pin 了 adapter 的 agent（含它代发的 routine、workflow 报告）发给 `user:<owner>` 的消息只在 GUI、CLI 可见，即使有 user-proxy 也不推；owner 2026-11-19 两次确认，KP-113。
- 多台机器的用户 agent 合并：未设计（见第 13 行）。
- daemon 日志的邮件式转发：owner 说以后再加，现在不做。

## 1. 这份文档是什么

Allen 要求把 user 与 IM 身份的绑定全部清除。GUI 线已派出只读清查，并把清除分成两档：

- **第一档**：删了不改变任何运行行为的（注释、文档、死分支、没人读的字段、已取消命令的设计文字）。由 GUI 线做，已合入（#2955）。
- **第二档**：删了会改变行为的。由 conversation 线做，本文档承载它的清单与之后的设计。

本节原文还写道：第二档的替代形态应当在 UA3（「把飞书做成用户 agent 的界面成员」）里一次设计。**这个前提已被 Allen 2026-11-19 更正（第 7 节）**：adapter 绑定的是 agent，不是用户；飞书不是用户 agent 的界面。

### 1.1 标记与依据

- **【已核对】**：本文作者在 current dev 上读到（本分支合入 origin/dev `d34b8d107` 之后）。行号会漂移，所以多写文件与函数名；与访问控制相关的代码（发送方认证、会话拒绝、转述校验、验证码流程）只写结论，不写行号与细节。
- **【取自清查】**：转引 GUI 线清查（dev `564337f810`，PR #2950 的评论），未在当前代码上重读。清查自己的标记照录：[R] 读到该行，[R-grep] grep 命中，[I] 推断，[U] 未核实。
- 清查之后合入的切片改变了的地方，在 3.3 逐条列出。

## 2. 范围与不在范围

在范围内：清查归为「把 user 和 IM 身份绑在一起、删除会改变行为」的每一项，以及本文发现的、清查漏掉或归类不同的两项（T2-G、T2-H）。

不在范围内：

- adapter 自己的 IM 配置与处理（保留）。若它今天依赖上面的绑定，依赖关系在第 3 节逐项列出。
- 第一档。
- `docs/evidence/**`、`docs/notes/**`（WORM，只追加）与历史设计文档里的提及：保留为记录。
- 多机：同一 owner 的多台机器之间对话文档不同步（`docs/design/chat-space-two-node-probe-2026-11-18.md`），本轮按单机交付；组织线已裁定多台设备不自动互通（#2883 J13）。

## 3. 清单

列「是否有落盘或跨机器同步的数据要迁移」在下表简写为「数据」。「按 2026-11-19 的裁定」一列给出每项的去向（删除、保留或改变），细节在第 8 节；代码里不留迁移或兼容逻辑（原则 3），旧数据的手工处理写进迁移说明（8.5）。

| # | 位置 | 今天的行为 | 谁在读、谁在写 | 删掉会怎样 | 按 2026-11-19 的裁定 | 数据 | 需要 Allen 回答的问题 |
|---|---|---|---|---|---|---|---|
| T2-A | `identity/impl/org_directory/models.py` 的 `EXTERNAL_ACCOUNT_CLAIM_KEYS`、`external_account_claims`；`identity/impl/onboarding/identity_record.py`；`shell/impl/login/oidc.py`、`login/flow.py`【已核对：常量与两处读取仍在；登录侧调用点取自清查 R-grep】 | 每次登录把 owner 的飞书 union id 投影进 `settings.json` `identityClaims`（S1） | 写：登录。读：解析器的 `casdoor-login` 来源（`IdentityResolver.is_owner_union`）、发布器守卫（`messaging/identity_binding.py`）、`daemon/impl/identity/bindings.py`【已核对】 | 本节点再也认不出「owner 本人从飞书发来」：入站标注里没有人是 owner（功能失效，非放行） | 删除，无替代：推送只看发送方 agent 的绑定，入站不判定 owner（8.1、8.4） | S1 | 无 |
| T2-B | `messaging/identity_binding.py` 全文件；`daemon/impl/identity/{bindings.py,jwks/,mutation/suppression.py}`；`org/network/directory.py`、`org/network/ipc.py` 的加入后发布；OrgFS 发布时间门；kernel 的六个 `IDENTITY_BINDING_*` 预算；启动阶段【文件存在已核对；行级取自清查】 | 把持有的证明发布到每个已加入 org 的 `directory/people/<slug>/binding.json`（S3）；其他成员验证后缓存（S4）；`identity.binding.publish|withdraw`、`identity.bindings.audit` | 写：本机 daemon。读：其他成员的 daemon（解析器 `org-directory` 来源）【取自清查 R】 | 持有旧行的成员在别的节点上不再被识别；#2943 之后本来就没有新证明的产生方 | 删除，无替代：IM 发送者就是拥有该账号的人，不加第二层证明 | S2、S3、S4 | 无 |
| T2-C | `daemon/impl/identity/{resolver,store,models,ipc}.py`、`mutation/overrides.py`、`audit/`；`identity/impl/users/store/**`；`cli/commands/user/commands.py` 的 `bind`、`unbind`、`whois`、`bindings audit|publish|withdraw`；GUI `PlatformIdentity{Configure,Lookup}.tsx`【已核对：文件与命令仍在；`identity.resolve`、`identity.override.set|clear`、`identity.users.*` 在 `request_catalog/identity/` 与 `openapi.json` 中仍在】 | 合并五个来源（`casdoor-login`、`org-directory`、`local-override`、`legacy-user-bind`、`legacy-identities`）把飞书账号解析成 hyprial user | 写：操作者（`identity.override.set`、`user bind`）。读：入站发送者标注、出站取 open_id（`owner_open_id`、`profile_open_id`）、转述证据（经发送者标注）【已核对】 | 入站没有人是 owner；出站四条路径取不到收件人；转述全部不成立 | 解析器、覆盖库、账号表 API 与绑定命令删除；只有名字的人员登记（`user add|show|list`、`identity.users.*`）保留（8.5） | S5（`identity.sqlite3`）、S6（`users.sqlite3` `user_accounts`、`profile.json` `accounts[]`） | 无 |
| T2-D | `adapters/lark/inbound/adapter.py` `_resolved_sender`；`inbound/pipeline.py` 把它放进 `providerMetadata.sender`；`daemon/impl/api.py` `describe_sender`；`messaging/visibility.py` 的发送者块校验与 `_message_origin`；`request_catalog/message/batch.py` 的 origin 结构；`inbox/batch/render.py`；`hyprial-ops/SKILL.md` 关于 `standing: verified` 的指引【已核对】 | 每条飞书入站带一个发送者块（`owner`、`standing`、`userKey`、`unionId` 等）；agent 看到「owner X, verified」或「NOT verified」一行 | 写：adapter worker（调用解析器）。读：agent 提示、批量渲染、转述证据（T2-G）【已核对】 | 所有人读作 `unresolved`；按 skill 行事的 agent 不再把任何人当作 owner（功能失效，非放行） | 改：发送者块只留 adapter 给出的通用事实（adapter、账号 id、显示名、聊天种类），去掉 `owner`、`standing`、`unionId`、`userKey` 与 verified 标注（8.4） | 无落盘；已发布字段变化，需同步 `contract/` 与 `Skill-Impact` | 无（随新设计） |
| T2-E | `inbox_surface/agent_bot.py`、`agent_bot_attempts.py`；`adapters/user_delivery/receiver.py` 的 squire 分支；`messaging/status/views.py`、`messaging/visibility.py` `_reply_path_unavailable`；`autoupdate/alert.py` `_owner_binding`；`cli/commands/squire/squire.py` 与 `agent_templates/setup.py` 的 `--owner-open-id --binding-code`【已核对】 | 发给 `user:<owner>` 的推送从 S5/S6/S7 取 open_id（`owner_open_id`、`profile_open_id`、`users.json` `ownerOpenId`） | 写：操作者经 `squire setup`。读：四条出站路径与两处视图【已核对】 | 往飞书推给用户全部失败；消息仍入队并记录（UA2 之后），只是不推送 | 删除按人的 IM 账号私信的两条路径（squire 私信、agent 机器人按 open_id 私信）与 `ownerOpenId`、squire 的 `--owner-open-id --binding-code`、升级提醒私信；推送改为 8.1 的三种情况（第 2 种归 cell 切片，期间有空档，7.5），user-proxy 保留 | S7 `ownerOpenId`、`pendingBindings` | 无（空档已裁定，7.5） |
| T2-F | `adapters/lark/state/{schema,records,store}.py` 的 `identities.hyprial_owner`、`standing`；`request_catalog/adapter/identities.py` 的 `hyprialOwner`；`cli/commands/adapter/data.py`；GUI `PlatformIntegrationDetail.tsx`【已核对；`upsert_identity` 在 `src` 里没有调用方，已核对】 | 历史行带 hyprial owner，经 `adapter.identities.list` 展示，并作为解析器的 `legacy-identities` 来源 | 写：无。读：解析器、列表展示【已核对】 | 历史行的 owner 不再显示；解析少一个来源 | 从读模型删除；表结构不改（`standing` 是非空列【已核对】，改表本身就是迁移） | `adapters.sqlite3` 历史行的两列 | 无 |
| **T2-G**（清查漏项） | `inbox/relay/verdict.py`、`inbox/relay/surface.py`；`message.relay.verify`；`message.send` 的 `relay` 参数；`contract/owner-relay/README.md`【已核对】 | 一个 agent 把 owner 的飞书私信转述给另一个 agent 时，daemon 只在原始入站行是飞书私信、发送者块为 `verified` 且带 owner 时生成并承认转述证据；提供方写死为飞书 | 读：发送路径（生成证据）、`message.relay.verify`（校验）。数据来自 T2-D 的发送者块；转述包整段拷贝发送者块【已核对】 | 所有转述不成立（失败即拒绝，功能失效，不是放行）；这是唯一一个按「是否 owner」做决定的代码门 | 它依赖「飞书私信来自 verified owner」，新前提下这个判断不存在；退役（7.6 第 1 条，8.4） | 无落盘 | 无 |
| **T2-H**（清查列为保留） | `org/membership/receivers.py`（`receivers_route`、`owner_receivers_view`、`ReceiversService`）；读取方：`user_delivery/receiver.py` 的 `owns` 与路由、`application/startup.py` 挂接 squire adapter、`application/owner_routing`、`messaging/visibility.py`、`UserProfileAuthority`、`send_targets/user.py` `push_evidence`、`org.receivers.get|set`、`squire setup` 的写入、CLI `common/support.py`【已核对】 | 个人 org 的 `receivers.json` 记「这个人在哪台机器、哪个 squire adapter、哪个 user-proxy 上可达」；本地 profile 作一个版本的迁移来源 | 写：`squire setup`、`org.receivers.set`。读：上列各处【已核对】 | 推送找不到 squire 与 user-proxy；`push_evidence` 退化为 `UNKNOWN`（每次推送等 3 秒）——这条退化路径今天已写好【已核对：`receivers.py` 文件头】 | squire 部分删除；「用户绑定了 user-proxy」改为本机事实（8.2）；`push_evidence` 与 3 秒等待删除（8.3）；文件随个人 org 删除（W1f） | 个人 org 里的 `receivers.json` | 无（与组织线 W1f 同步） |

### 3.1 五个问题

**1. 入站：靠什么判断 owner 本人、别人、无法判断；判断不出来时怎么处理。**

- 目标 agent 由 adapter 的 pin 决定，不由发送者决定；worker 以 `message.send` 提交，`actor` 是 `adapter:lark:<名>`，并显式带上由飞书会话派生的 `conversationId`【已核对：`inbound/pipeline.py`、`lark/worker/daemon_link.py` `send_request`】。
- 发送者块在 worker 里由 `_resolved_sender` 生成：没有 union id 即 `unresolved`；没有本机用户库且不是 owner 自己的 union id 时只查 adapter 的表；否则走 `IdentityResolver.resolve_sender` 合并五个来源【已核对】。
- 判断不出来时既不拒绝也不当作 owner：标为 `unresolved`（或 `ambiguous`、`lookupFailed`）并照常投递【已核对】。
- **更正清查**：清查说「没有任何代码据此放行或拒绝」[I]。实际有一处：owner 转述证据（T2-G）只承认 `verified` 且带 owner 的飞书私信【已核对】。其余用处是 agent 提示里的一行字与 skill 指引【已核对】。SKILL 里「授权白名单的键必须是经绑定核验的平台身份」一句没有找到代码读者：转述的 `allowlist` 是动作类别白名单，默认空，不按身份【已核对：`inbox/relay/verdict.py`】。
- 结论不变：单纯删除的后果是所有人都不是 owner（功能失效），不是任何人都被当成 owner。

**2. 出站：收件人从哪里取，三条路径各自怎么取。**

- UA2 之后的变化【已核对：`send_targets/user.py` 文件头与 `push_recorded_user_message`】：发给本机 owner 的 `user:<owner>`、本机有用户 agent 时，消息先走普通队列并被记录，然后才推送；推送失败不撤销消息。「选收件人」现在只决定推送，不决定消息是否存在。
- 推送顺序：先试发送方 agent 自己 pin 的飞书机器人（`_deliver_user_via_agent_bot`，open_id 来自 `owner_open_id`：S5 覆盖 → 绑定行 → 带 owner 的历史 adapter 行）；不成再经 user delivery 交给「拥有该用户的机器」，那里先找在线 user-proxy，再由 squire adapter 私信（open_id 来自 `profile_open_id`：S6 覆盖 → 绑定行 → S7 `ownerOpenId`）【已核对】。
- 推送前用 `push_evidence` 把本机所知分成三类：`CONFIRMED_ABSENT` 不推送；`DESTINATION_KNOWN` 推送，失败上抛；`UNKNOWN` 推送，若 3 秒内无人回执则答 `pushed: false` 而不报错。三类都只读 `owner_receivers_view`（个人 org 的 `receivers.json` 加本地 profile）【已核对】。
- squire 与 user-proxy 是同一条 receiver 路径上的两种端点；receivers 记录给出的是 adapter 端点，不是 IM id【已核对】。
- 另有直接调用 user delivery、不入队也不记录的旁路：工作流与 PAC 报告、配额与收件箱看门狗告警、OrgFS 完整性通知、操作员告警【已核对：`user.py` 文件头列举】；升级提醒直接读 S7【已核对：`autoupdate/alert.py` `_owner_binding`】。审批类消息没有单独的收件人路径【取自清查 U】。
- **已被取代（2026-11-20）**：本条列的旁路里，daemon 自己发起的各项（看门狗告警、OrgFS 通知、操作员告警、升级提醒）已不再推送，只写日志；工作流与 PAC 报告改由负责的 agent 发出（#2989，修订节第 2、3 行）。
- 删掉 S5/S6/S7 之后：推送全部失败，但消息仍在对话里（UA2 之后）。

**3. 组织功能是否读用户的 IM 身份。** 不读。仅有的接触点是加入后的尽力发布与目录里的绑定行，删掉后组织功能不受影响【取自清查 R-grep】。但 receivers 记录（T2-H）是个人 org 的数据、被出站读取，随个人 org 的删除一起处理。

**4. 是否发布到别的节点或别的用户可读的位置。** 是：S2 那一行被发布到每个已加入、未屏蔽 org 的 `directory/people/<slug>/binding.json`，其他成员的 daemon 验证后写入 S4【取自清查 R】。`hyprial user bindings withdraw` 会删除自己在所选 org 里的那一行【已核对：`_withdraw_identity_binding` 调用 `remove_binding`】。另外，个人 org 的 `receivers.json` 会复制到持有该空间的节点【已核对：`receivers.py` 文件头】，它不含 IM id，但含 squire adapter 端点。

**5. 落盘或同步出去、需要迁移或失效的数据。** S1–S7 全部（3.2）；已复制的 `people` 段文档：OrgFS 收到同步更新与读取副本键时都校验受保护文档 id，段名不在词表里即拒绝【已核对：`orgfs/network/protocol.py`、`orgfs/storage/replica.py`】，所以词表里的 `people` 不能随手删；`users.sqlite3`、`identity.sqlite3`、`profile.json`；`settings.json` 的 `identityClaims`、`identityBinding` 由登出删除，并列在身份残留检查的字段表里【已核对：`identity/impl/onboarding/logout.py`、`identity_record.py`】；`users.json` profile 校验只按已知字段重建记录【已核对：`persistent_config.py` `_validate_user_profile`】；个人 org 的 `receivers.json`【已核对】；启动事件日志里可能出现的启动阶段名【取自清查 U】。

### 3.2 S1–S7 速查

| # | 存储 | 内容 |
|---|---|---|
| S1 | `settings.json` `identityClaims` | owner 的飞书 union id（每次登录写） |
| S2 | `settings.json` `identityBinding` | 身份服务的断言（#2943 后无写入方） |
| S3 | OrgFS `directory/people/<slug>/binding.json`（复制） | S2 那一行 |
| S4 | `state/verified-org-bindings.json`、`state/identity-binding-suppression.json` | 其他成员绑定的已验证缓存；屏蔽名单 |
| S5 | `state/identity.sqlite3` | union id 或（adapter, open_id）→ user key |
| S6 | `state/users.sqlite3` `user_accounts`，镜像到 `profile.json` `accounts[]` | 确认属于某 user 的平台账号 |
| S7 | `state/users.json` `ownerOpenId`；`state/squire-setup.json` `pendingBindings` | owner 在 squire 应用里的 open_id；十分钟验证码 |

（全部取自清查 [R]；S7 的读取方与验证码流程已核对。）

### 3.3 当前代码与清查不一致之处

1. **出站已在队列之后**（UA2、UA2b、UA2c、UA2d 合入）：发给本机 owner 的消息先入队并记录，推送是附加的；清查的「取不到收件人就以 `IDENTITY_*` 拒绝」今天只对不入队的路径（另一 owner、本机没有用户 agent）原样成立【已核对】。
2. **`push_evidence` 三分类与 3 秒无人认领的吞掉**是清查之后新增的，且已把 receivers 读取集中到 `owner_receivers_view` 一处，注明随 W1f 删除【已核对】。
3. **用户 agent 已存在**：每台登录过的机器一个零 harness 的 `hyprial-user`（`daemon/impl/user_agent/`）；`user:<owner>` 在本机注册为本地 actor，信封里始终是 `user:<owner>`，机器本地的用户 agent 地址不进信封【已核对：`local_target.py` 文件头】。
   - **已被取代（2026-11-20，#2992）**：每台机器的用户 agent 现在是独立 agent，id 由 registry 铸造，不再由 owner 派生；见修订节第 13 行。
4. **`adapter.pin` 到用户 agent 被拒**，错误码 `USER_AGENT_IMMUTABLE`，理由是它不运行、消息无人取走；`session.register` 同样拒绝以用户 agent 身份登记会话【已核对】。
5. **转述证据是一个读「是否 owner」的代码门**（T2-G），清查未列【已核对】。
6. **发送者块的 schema 位置**（清查标 [U]）：`request_catalog/message/batch.py` 的 origin 结构，`inbox/batch/render.py` 的渲染【已核对】。
7. **路径与行号漂移**：清查写的 `status/views.py` 在 `application/messaging/status/views.py`；`receiver.py` 的路由段下移约 6 至 30 行；`pipeline.py` 构造请求处下移约 1 至 4 行【已核对】。
8. **现有验证码不经过飞书**：`squire setup` 生成的码只在本机输出里出现，open_id 由操作者从 daemon 日志里抄写，码从不与私信内容比对【已核对】。
9. **`receiver.py` 的中文指引** `UNCONFIGURED_SQUIRE_MESSAGE` 仍写 `--owner-open-id`【已核对】，随 T2-E 改。
10. **GLOSSARY 的 `user` 条目**「现状」一栏仍写「发给 `user:<owner>` 的消息……不进本地收件箱，也不进记录」，UA2 之后已不成立；定义一栏「投递时解析到它的端点（GUI、adapter 账号、user-proxy）」也带着第 7 节更正掉的前提【已核对】；要改的 GLOSSARY 条目见 8.6。
11. **通用 adapter 契约已有发送者类型但未接入**：`adapters/contracts/values.py` 的 `PrincipalRef`（`provider_subject_id`、`display_name`、`is_self`，以及承载旧绑定模型的 `hyprial_principal`、`proof_ref`、`standing`、`resolution`）与 `contracts/content.py` 的 `ChatContext.sender`，被各驱动构造（slack 已读：`hyprial_principal` 为空、`resolution` 为 `unresolved`；matrix、discord、telegram、whatsapp 等为 grep 命中，未逐个读）；在当前代码里没有找到把通用 `ChatMessage` 送进 `message.send` 的路径（只按类型名 grep），接入 agent 的看来只有飞书。

## 4. 约束

- **安全优先于清除。** 入站身份判断的任何替换，在替代形态就位并有测试之前不删除原有判断。「删掉之后任何人发的都被当成 owner」是不可接受的中间状态。
- **需要真实飞书环境的验证。** 出入站行为的改变不能只靠单元测试合入；验证方式在实施前定。
- **与在途工作的文件冲突。** 以下文件曾在 conversation 线的未合入分支上有改动（UA2；#2941）。第一档请避开它们，或等这两片合入后再动：
  - `src/hyprial/daemon/impl/application/messaging/inbox_surface/send_targets/user.py`
  - `src/hyprial/daemon/impl/adapters/user_delivery/receiver.py`
  - `src/hyprial/daemon/impl/org/membership/receivers.py`
  - `src/hyprial/daemon/impl/user_agent/`
  - `src/hyprial/daemon/impl/application/messaging/delivery/sends.py`
  （2026-11-19 注：#2941 与 UA2 系列均已合入，#2955 未碰这些文件；这条约束对第二档自身不再构成冲突。）
- **不添加复杂机制**（Allen 2026-11-18）。不新造并行的身份体系。
- **结构测试的范围**（#2955 扩大后）：`daemon/impl/{conversations,delivery,links,threads,dispatch,forwarding,session_actor,transport,lifecycle,lifecycle_manager,onboarding}` 里不得出现绑定相关词或 `lark`；新设计若要在这些目录里提到 adapter，先与 GUI 线商量。

## 5. 与其他线的交集

- **组织线 #2883**：接收设备记录（`receivers.json`）记的是「这个人在哪台机器、哪个 squire、哪个 user-proxy 上可达」，与出站推送直接相关；个人 org 正在删除（J10、W1f）。本设计删除它的 squire 部分，user-proxy 改为本机事实（8.2），文件随 W1f 删除（8.7）。
- **发送方认证的 observe 模式**（KP-110）：在把 owner 的对话路由到用户 agent 自己的地址之前必须收紧。Allen 已定本轮不动。
- **群聊**（`conversations/groups/`）：群的人类成员 id 是 `user:<owner>`，不带机器段【已核对：`groups/model.py` 文件头】。

## 6. 顺序

1. ~~GUI 线：只读清查 → 结果填入第 3 节~~：已完成（PR #2950 评论；本文第 3 节）。
2. ~~GUI 线：第一档~~：已合入（#2955）。
3. conversation 线：#2941、UA2 已合入；**群聊进行中**。
4. ~~conversation 线：据第 3 节与第 7 节重写设计~~：即第 8 节，裁定记录在第 7 节，第 9 节目前没有待裁项。
5. Allen 裁定后，**群聊合入之后**按 8.7 的「本轮」切片实施，含真实飞书环境的验证；出站第 2 种情况等 #2712 的 cell 切片；`receivers.json` 文件的删除与组织线 W1f 同步。

## 7. 2026-11-19 的更正与裁定

### 7.1 更正

Allen（2026-11-19）：

> 「我觉得问题1和2是不是都是因为你有一个错误的前提：一个人必须要有IM账号？…GUI和飞书按道理也没有任何的关系，如果用户在GUI中绑定了飞书的端口，那也是为他所属的agent绑定的，和用户本身没有关系」

含义：adapter 绑定的是 **agent**，不是用户。IM 是 IM 上的人与那个 agent 之间的通道。一个人不必有 IM 账号；IM 账号只是人员登记里的可选信息。没有「adapter 主人的 IM 账号」这个概念，没有绑定时的验证码，飞书也不是用户 agent 的界面。此前建立在错误前提上的设计草案已撤下。

### 7.2 裁定：发给 `user:` 的消息还推不推送到飞书

问：「发给 user:<你> 的消息，是否从此只进对话，不再推送到飞书？」Allen（2026-11-19）：

> 「显然不是：
> 1. 经squire等agent 私信的方式，应该删掉，这是历史遗留
> 2. 但是，比如一个agent绑定了adapter的一个lark chat，发给user:<xx> 的消息，会通过这个lark chat 推送到飞书给用户看到
> 3. 完全没有绑定adapter的agent，发给user:<xx> 的消息，如果这个用户绑定了user-proxy，那么会在飞书转发，如果没有绑定，则只能在GUI/CLI中看到」

**协调者的读法**（超出原话的部分是读法）：每条发给 `user:<owner>` 的消息都入队并记进对话（PR #2954 起已如此），在 GUI、CLI 可见。推送到 IM 是额外的一份拷贝，由**发送方 agent 自己的绑定**决定，从不由用户的任何 IM 身份决定：(a) 发送方 agent 绑定了 adapter 的某个 chat → 拷贝经该 adapter 发进**那个 chat**，目的地是 chat 不是人；(b) 发送方 agent 没有 adapter 绑定、而用户绑定了 user-proxy → 交给 user-proxy，由它经自己的 adapter 绑定转发；(c) 两者都没有 → 只在对话里，`pushed: false`，不等待。删掉的是一切按**人的** IM 账号寻址的东西；user-proxy 概念与它的交接保留。（(a) 的具体规则后由 7.4 改为归 cell 模型。）

### 7.3 仍然有效的裁定（Allen 2026-11-19）

- adapter 对 agent 透明：agent 知道消息来自哪个 adapter、该经哪个 adapter 发送，但不按 adapter 种类特殊处理。
- 每个 IM 发送者就是拥有那个 IM 账号的人（「当然是读法一」）。
- 代码里不保留 legacy 或迁移逻辑；CHANGELOG 链接一份迁移说明，用户安装新版时手工一次性迁移。
- 只有名字的人员登记保留（「当然，IM账号只是一个用户的可选信息」）。
- 不添加复杂机制（2026-11-18）；本轮单机。

### 7.4 出站第 2 种情况归 cell 模型（Allen 2026-11-19）

> 「'agent 绑定了多个聊天时' 等问题实际上跟cell-model的实施有关」

协调者提出「先只做不依赖 cell 模型的部分（删除、入站通用化、迁移说明），出站推到 agent 绑定的聊天等 #2712 的相应切片」，Allen：

> 「你的理解是对的，先做不依赖 cell 模型的部分」

同时确认：本轮取消的是 thread 这个名词，「agent 加一个 adapter 端点」的结构仍在。

依据（只读）：cell handoff §4.3「成员与连接」建议成员角色至少区分 harness、agent、adapter、user、member，连接先作为 cell params 里的数据 `(from, to, role, policy)`，不另设拓扑存储（`origin/docs/cell-mode-post-actor-rustify-2026-11-12` 上的 `cell-mode-post-actor-rustify-handoff-2026-11-12.md`）；同分支 `cell-front-members-judge-buffer-2026-11-14.md` §5 是这份连接数据的第一个消费者；dev 上 `cell-thread-conversation-model-2026-11-03.md` §2–§4 把「agent 与一个 adapter 端点之间的通道」列为一个概念，`route` 不再作为独立的寻址与配置概念。所以「agent 绑定了哪个聊天」应当由 agent cell 的 adapter 成员与连接数据回答，不由本稿另立规则。

### 7.5 空档：先删除（Allen 2026-11-19）

问：squire 私信与 agent 机器人私信 owner，是与 #2712 的第 2 种情况同时删，还是先删并接受空档。Allen：

> 「先删除并接受空档」

得知「空档期间即使有 user-proxy，钉选了 adapter 的 agent 的消息也到不了飞书」之后，Allen 再次确认（2026-11-19）：

> 「维持原决定，接受这个更大的空档」

### 7.6 第 9 节五项（Allen 2026-11-19）

协调者逐项列出五条建议，Allen 原话：

> 「同意」

下面五条是协调者的表述，不是 Allen 的原话：

1. 转述证据退役：删除 `message.relay.verify`、`message.send` 的 `relay` 参数与 `contract/owner-relay/`。
2. 发给另一 owner 的 `user:` 目标保留今天的 Zenoh 交接，直到跨 owner 对话（R8）；接收方只用自己的 user-proxy。
3. daemon 起不来时的升级失败提醒发到各 adapter 的默认路由。**已被取代（2026-11-20）**：Allen 推翻此条，升级失败提醒删除，daemon 起不来时不向任何 IM 发消息（#2989，修订节第 1 行）。
4. 保留 OrgFS 受保护段名 `people`（无读者的词表项）。
5. 人员登记里的可选 IM 账号本轮不用于显示发送者。

### 7.7 2026-11-19 之后的补充裁定

见本文开头「2026-11-20 修订」一节：daemon 到用户的推送路径删除、通知只写日志；升级失败提醒删除（推翻 7.6 第 3 条）；批量读取丢掉不认识的 origin 字段。send-link 组件、thread 这个名词与 squire 整体由 cell 模型线删除，每台机器的用户 agent 各自独立，本稿不展开。

## 8. 设计

一句话：**发给 `user:<owner>` 的消息照旧入队、记录。往 IM 发拷贝按 owner 裁定的三种情况：第 1 种（squire 等 agent 私信）删除；第 2 种（agent 绑定了某个聊天就推到那个聊天）由 agent cell 的 adapter 成员与连接数据决定，属于 #2712 的切片，本稿不设计、不实现；第 3 种（没有 adapter 绑定的 agent，用户有 user-proxy 就交给它，否则只在 GUI/CLI）本轮做。入站发送者只呈现 adapter 给出的平台事实，不再判定谁是 owner。按人的 IM 账号寻址的代码与绑定证明删除，不留兼容；每项删除属于哪一组见 8.7。**

### 8.1 出站

**第 1 种：经 squire 等 agent 私信 owner**——删除（Allen：「这是历史遗留」）。今天它在两处【已核对】：

- squire 私信：`user_delivery/receiver.py` `_deliver_unrecorded` 的 squire 分支，open_id 来自 `profile_open_id`（S6 覆盖 → 绑定行 → S7 `ownerOpenId`），正文前加「来自我的侍从，转述自 …」。受众是「agent-bot 没有接走」的发送：没有 pin 的 agent、GUI/CLI 发送者、daemon 自己的通知，以及 pin 了飞书 adapter 但 agent-bot 没接走（owner open_id 解析不出、网关不在运行等）的 agent。
- agent 机器人按 open_id 私信：`inbox_surface/agent_bot.py` `_deliver_user_via_agent_bot`，open_id 来自 `owner_open_id`（S5 覆盖 → 绑定行 → 带 owner 的 adapter 历史行）。受众是 pin 了飞书 adapter 的 agent。它今天实际承担的，正是第 2 种情况的位置。

一个 agent 今天最多 pin 一个 adapter：注册表 `pins` 表的 adapter 与 agent 两列都是 `UNIQUE`，`pin()` 一对一，第二个 adapter 钉同一 agent 报 `PinConflictError`；`desired_state` 的 `channel_pins` 只是旧数据的迁移来源【已核对 origin/dev：`identity/impl/agents/registry/_base.py`、`_records.py`】。所以「agent 绑定了多个聊天」今天只能是同一 adapter 上的多条 route；`_agent_bot_adapter` 里处理「多个 pin」的分支实际走不到 [I]。

**第 2 种：agent 绑定了某个聊天** → 推到那个聊天。由 agent cell 的 adapter 成员与连接数据决定（7.4），属于 #2712 的切片，本稿不设计、不实现；输入将写进 #2712 的增补稿 `docs/design/cell-conversation-refactor-inputs-2026-11-19.md` 中关于出站绑定的条目（该文件在本分支尚不存在）。「绑定了多个聊天时用哪个」「绑定的是群时拷贝对全群可见」「`message.reply` 给 `user:` 是否同样推送」都由那里的 cell 数据回答，不在本稿提问。

**第 3 种：没有 adapter 绑定的 agent**——本轮做，不依赖 cell：

- 判定「没有 adapter 绑定」：发送方 `pinned_adapters` 为空（今天的数据，【已核对：`AgentRegistry.pinned_adapters`】），或发送方不是本机 agent（GUI/CLI、daemon 自己的发送）。**已被取代（2026-11-20）**：「daemon 自己的发送」一项不再存在，daemon 不发送（#2989，修订节第 5 行）。
- 判定「用户绑定了 user-proxy」：本机 desired state 里恰好一个 harness 为 `user-proxy` 的 agent（8.2）。有 → 交接给它；没有 → 不推送，`pushed: false`、`pushCode: TARGET_UNRESOLVED`，不等待。
- 本 owner 的这条路径全部在发送节点本机判定，不再经 `ZenohUserDeliveryTransport`，不再由接收方选机器。
- 发给**另一 owner** 的 `user:` 目标保留今天的 Zenoh 交接（`send_to_user_target`），直到跨 owner 对话（R8）；接收方只用自己的 user-proxy，不再退回 squire 私信（7.6 第 2 条）。3 秒回执等待只留在这条路径上。

**有 adapter 绑定的 agent 在第 2 种情况落地之前怎么办**：见 8.3「空档」。

**已被取代（2026-11-20，#2989；修订节第 2 至 4 行）：下面这一段整段作废。** 现在的做法：监控类通知只写 `warn` 级日志（component `daemon`，带全文与幂等键），不入队、不进对话、不推送；routine、workflow、PAC 报告由负责的 agent 作为普通消息发出，找不到本机负责 agent 时只记 `workflow.user_report.unsent`；重启前通知（`autoupdate.notify`）删除。以下为 2026-11-19 的原文：

**daemon 自己发给用户的消息**（没有发送方 agent，今天直接调 user delivery、不入队不记录【已核对：`send_targets/user.py` 文件头 UA2b 第 9 条】）：`_workflow_deliver_user`（运行与 PAC 报告、工作流升级、`DispatchAlarm`）、`_deliver_report_to_user`、配额与收件箱看门狗、OrgFS 完整性通知、操作员告警、自动升级重启通知。它们属于第 3 种：改走入队路径，有 user-proxy 交给它，没有只在 GUI/CLI。与今天相比，没有 user-proxy 的用户不再经 squire 私信收到这些通知；重启通知不再「确认送达」，只确认已记录。

**已被取代（2026-11-20，#2989；修订节第 1 行）：升级失败提醒删除，daemon 起不来时不向任何 IM 发消息，下面的「改发默认路由」不再适用。** 以下为 2026-11-19 的原文：

**升级失败提醒**（`autoupdate/alert.py`，daemon 起不来时直接读 `ownerOpenId`、经 adapter 私信【已核对】）：此时没有队列。改发到各 adapter 的默认路由（7.6 第 3 条）：adapter worker 已用默认路由发自己的运维告警【已核对：`lark/worker/entry.py` `notify_operator`】，目的地是聊天不是人。

### 8.2 user-proxy（第 3 种，本轮）

**今天**【已核对】：`hyprial user proxy setup --adapter <专用 adapter> --route <路由>`（`shell/impl/cli/commands/squire/user_proxy.py`）把专用 adapter pin 给 proxy agent、以 `--route` 启动 `user-proxy` harness，并经 `org.receivers.set` 写 `receivers.userProxy`；它读 `receivers.squire` 以拒绝 squire 的 adapter。交接在 `_deliver_to_live_user_proxy`：proxy 不在运行时返回 None，让 receiver 退回 squire 私信。proxy 往 IM 发的目的地是它自己的路由（`_forward_as_actor` 直接调 `_deliver_route_target`），从来不是 open_id；它转发给 `user:` 的那份带 `FORWARD` 标记、不再推送【已核对：`sends.py` 文件头 UA2b 第 1 条】。

**本轮：**

- 「用户绑定了 user-proxy」改为本机事实：desired state 里恰好一个 harness 为 `user-proxy` 的 agent；多于一个时不选并在 `user proxy status` 报出。`receivers.userProxy` 不再读写。
- 交接不再要求 proxy 正在运行：交接是写进 proxy 收件箱的普通入队，proxy 启动后取走；不再退回 squire。
- `setup` 不再读 `receivers.squire`、不再写 receivers。
- 不会回环：proxy 发到 IM 走路由，发给 `user:` 的那份带 `FORWARD` 标记不推送。

**能否不依赖 cell 先做**：能。它只用三样今天就有的东西：发送方 `pinned_adapters`（判定「没有 adapter 绑定」）、desired state 的 harness 种类、现有的交接与转发。proxy 自己发到 IM 用的是它的 `--route`，这条路径与第 2 种情况将要读的 cell 连接数据无关。日后 cell 模型若把 proxy 的 adapter 也表达为成员与连接，proxy 的转发目的地随之换来源，第 3 种情况的判定不变。

### 8.3 空档（已定：先删除，7.5）

squire 私信与 agent 机器人按 open_id 私信 owner 都在**本轮**（群聊之后）删除，不等 cell 切片。

**空档范围已经 Allen 确认（7.5 第二段原话），不加临时规则。**

**空档期间对用户的实际影响**（从本轮发布到 #2712 第 2 种情况的切片发布）：pin 了飞书 adapter 的 agent 发给 `user:<owner>` 的消息只在 GUI/CLI 可见，不推送到飞书任何地方——第 2 种情况未实现，第 3 种情况按定义不适用于有 adapter 绑定的 agent，所以即使用户有 user-proxy 也不交给它。消息本身照常入队、记录。具体受影响的是：

- 今天走 agent 机器人私信的：pin 了飞书 adapter、且能解析出 owner open_id 的 agent【已核对：`_agent_bot_adapter`、`owner_open_id`】；
- 今天 pin 了飞书 adapter、但 agent-bot 没接走（owner open_id 解析不出、网关不在运行等）而落到 squire 私信的 agent。

不受影响：没有 pin 的 agent、GUI/CLI 发送者——它们属于第 3 种情况，有 user-proxy 就照旧到飞书。（原文还列了「daemon 自己的通知」，**已被取代（2026-11-20）**：daemon 通知只在日志里，不再推送。由 pin 了 adapter 的 agent 代发的 routine、workflow 报告同样落在空档里，测试 `test_a_report_from_an_adapter_pinned_agent_lands_in_the_accepted_gap`，登记为 KP-113。）对用户的过渡办法：设置 user-proxy 覆盖这一部分；对 pin 了 adapter 的 agent，用户若要在空档期内在飞书看到它的主动消息，只能解除这个 agent 的 pin（它随即按第 3 种情况交给 user-proxy，但飞书里发给该机器人的消息也不再进这个 agent），或等 cell 切片。本稿不为空档另加临时规则（「不添加复杂机制」）。

两个删除对目标状态并不对称：squire 私信的受众几乎全在第 3 种情况里，删掉后它们去 user-proxy 或只在对话里，正是裁定的目标状态；agent 机器人私信的受众全在第 2 种情况里，删掉即进入空档，直到 cell 切片补上。

**`push_evidence` 与 3 秒等待**本轮删：它们只服务「交给拥有该用户的机器，那里先找 user-proxy 再退回 squire 私信」这条接收方路径；本轮第 3 种情况在本机判定、两条私信路径删除，本 owner 的推送不再走接收方路径【已核对：`push_recorded_user_message` 先试 agent-bot，未接走才读 `push_evidence`】。

**投递条目的形状**（`user:` 目标，`accepted`、`queued: true`、`messageId` 不变）：`pushed: true` 时带 `via: "user-proxy"`；`pushed: false` 时带 `pushCode`（`TARGET_UNRESOLVED` 表示没有可用的推送）。不再有 `receiverAdapter`，也不再有 agent-bot 的字段。保留的规则（UA2b 已定）：确定没发出或没有配置 → 答 ok、`pushed: false`；结果未知或暂时不可用 → 上抛，调用方同键重试。as-built（#2989）：没推时日志 `send.user_push` 另带 `pushSkipped: sender-pinned | no-user-proxy`。

### 8.4 入站：通用的发送者，与转述证据（本轮）

- **agent 看到什么**（对所有 adapter 相同）：`from` 是 adapter 地址（回复经它回去，与今天相同）；origin 带 adapter 给出的平台事实：平台账号 id、平台显示名（来自 adapter 自己的 `identities` 观测表，可能为空）、聊天种类（通用值 `dm`/`group`，契约已有 `CHAT_KINDS`【已核对】）、平台消息 id 与时间。提示行为「<显示名或账号 id>（经 adapter:<种类>:<名>，账号 <id>）」。不再有 `owner`、`standing`、`unionId`、`userKey`、`candidate*`、`lookupFailed` 与 `verified`/`NOT verified`。
- **删除的位置**：`lark/inbound/adapter.py` `_resolved_sender` 不再调用解析器，只读 adapter 自己的观测表；`credentials/identities.py` `resolve_sender_identity` 去掉读用户库（S6）的分支；`api.py` `describe_sender`、`inbox/batch/render.py`、`messaging/visibility.py` 的发送者块校验、`request_catalog/message/batch.py` 的 origin 结构随之收窄；`hyprial-ops/SKILL.md` 第 716–736 行一段改为通用说法：「经 adapter 来的消息，说话人是拥有该平台账号的人；hyprial 不判定他是不是 owner；owner 的指令以 `user:<owner>` 的身份到达（GUI、CLI）」。
- **as-built（#2970，2026-11-20）**：origin 的字段是 `chatKind`（取代 `chatType`）、`sender{kind, platformId, displayName}`、`provider`、`platformMessageId`、`platformCreatedAt`、`via`；提示行为 `<显示名> (via <adapter 地址>, account <平台账号 id>)`。批量读取（`message.pending.batch`）把存储的 origin 里当前 schema 不认识的字段丢掉，只改发布视图、不改存储（Allen：「同意，来源信息里有不认识的字段时，把那些字段丢掉」）；批量 origin schema 另加了 `mechanism`。
- **转述证据（T2-G）**：用途是让一个 agent 把「owner 在飞书私信里说的话」带着 daemon 的证明转给另一个 agent（`contract/owner-relay/README.md`）；判据「飞书、私聊、`verified` 且带 owner」【已核对】在新前提下无法成立。退役（7.6 第 1 条）：删除 `inbox/relay/`、`message.relay.verify`、`message.send` 的 `relay` 参数与 `contract/owner-relay/`，归 R1。as-built（#2970）：已随 R1 删除，另删 MCP 的 `relay_message_id` 与 CLI 的 `--relay-message-id`。

### 8.5 逐项去向、数据与迁移说明

| 项 | 去向 | 组（8.7） |
|---|---|---|
| T2-A 登录投影 | 删除 | 本轮 |
| T2-B 证明发布与验证 | 删除 | 本轮 |
| T2-C 解析器与绑定 | 删除解析器、覆盖库、`user_accounts` 读写 API、`identity.resolve`、`identity.override.*`、`identity.bindings.list`、CLI `user bind|unbind|whois|bindings *`、`user list --source`、GUI 两个面板（GUI 线）。**保留** `user add|show|list`、`identity.users.add|list`、`identity.whoami` 与用户表 | 本轮 |
| T2-D 发送者块 | 改（8.4） | 本轮 |
| T2-E squire 私信、`profile_open_id`、`ownerOpenId`、`squire setup --owner-open-id --binding-code`、`UNCONFIGURED_SQUIRE_MESSAGE` | 删除（as-built，#2989：`squire setup --channel` 也一并删除，超出原定最小范围） | 本轮 |
| T2-E agent 机器人按 open_id 私信、`owner_open_id`、尝试账本（`agent_bot*.py`） | 删除（7.5，接受空档） | 本轮 |
| T2-E 状态视图与 `_reply_path_unavailable` 的 open_id 读取 | 改为「本机有没有 user-proxy」 | 本轮 |
| `push_evidence`、3 秒等待、本 owner 推送的 Zenoh 交接与接收方选机器 | 删除（8.3） | 本轮 |
| user-proxy 改为本机事实（第 3 种） | 改（8.2） | 本轮 |
| daemon 自己的通知改走入队路径 | 改（8.1）。**已被取代（2026-11-20，#2989）**：daemon 通知只写日志；报告由负责的 agent 发出 | 本轮 |
| T2-F `hyprialOwner` | 从读模型删除，表结构不改；解析器的 `legacy-identities` 来源随 T2-C | 本轮 |
| T2-G 转述证据 | 退役：`inbox/relay/`、`message.relay.verify`、`relay` 参数、`contract/owner-relay/` | 本轮（R1） |
| T2-H `receivers.json` | squire 与 userProxy 两部分本轮不再读写；文件与 `org.receivers.get|set` 随 W1f 删除 | 本轮（读写）／W1f（文件） |
| 第 2 种情况（推到绑定的聊天） | 新增 | cell 组（#2712） |

新版本遇到旧数据【已核对】：`settings.json` 的两个键从字段表里去掉后不再被读或清理，残留检查只看表内字段，不会因它们拒绝准入；`users.json` profile 的校验按已知字段重建记录，`ownerOpenId` 被忽略；`identity.sqlite3`、`squire-setup.json`、两个缓存文件没有代码打开；`adapters.sqlite3` 的两列留着，观察写入照旧写 `observed`。已复制的 `people` 段文档：OrgFS 收到同步更新与读取副本键时校验受保护文档 id，段名不在词表里即拒绝（`orgfs/network/protocol.py`、`orgfs/storage/replica.py`）【已核对】；词表里的 `people` 若删掉，旧成员复制来的文档会被拒，影响范围未核实 [I]。所以保留 `people` 这个段名（7.6 第 4 条），它只是词表项，没有读者。

**迁移说明**（`docs/guides/migration-im-push-by-agent-binding.md`，本轮写，CHANGELOG 链接它；cell 切片发布时补一节）应包含：

（**已被取代（2026-11-20，#2991）**：迁移说明已写成，以该文件为准；下面 1 至 6 条是写之前的规格，与成文的差别见修订节第 10 行：`identityClaims`、`identity.sqlite3`、`users.sqlite3` 与两个缓存文件在 R3 之前仍被读取，所以不在「可删除」里；不再有升级提醒一项。）

1. **不再有的**：squire 私信你；agent 机器人按你的 open_id 私信你；`squire setup --owner-open-id --binding-code`；`hyprial user bind|unbind|whois|bindings …`；GUI 的「配置绑定」「查询绑定」。
2. **空档**（必须写明）：在 agent 绑定聊天的推送（cell 切片）发布之前，pin 了飞书 adapter 的 agent 发给你的消息只在 GUI、CLI 里，不会出现在飞书——即使你设置了 user-proxy。没有 pin 的 agent 与 daemon 通知不受此限。
3. **过渡**：用 `hyprial user proxy setup` 绑定 user-proxy，没有 pin 的 agent 与 daemon 通知会经它到飞书；对 pin 了 adapter 的 agent，要么在 GUI、CLI 里看，要么等 cell 切片（或解除 pin，代价是飞书里发给该机器人的消息不再进这个 agent）。
4. **被忽略、可选清理**：`settings.json` 的 `identityClaims`、`identityBinding`；`state/identity.sqlite3`；`state/users.json` 的 `ownerOpenId`；`state/squire-setup.json`；`state/verified-org-bindings.json`、`state/identity-binding-suppression.json`。
5. **组织空间里的绑定行**：新版本不读。想让还在旧版本上的成员不再据它识别你，**升级前**在旧版本上运行 `hyprial user bindings withdraw --all`（它删除你在各 org 的那一行【已核对】）。
6. **跳过会怎样**：消息都在 GUI、CLI 的对话里；只是不会出现在飞书。

旧节点仍在做什么（不在代码里处理）：旧版本成员继续验证已复制的绑定行；旧版本节点继续按 `receivers.json` 与两条私信路径推送，只影响它自己。

### 8.6 已发布接口的变化（本轮）

| 类别 | 删除 | 改变 |
|---|---|---|
| 请求方法 | `identity.resolve`、`identity.override.set|clear`、`identity.bindings.list|audit`、`identity.binding.publish|withdraw`；`message.relay.verify`；（随 W1f）`org.receivers.get|set` | `message.send` 投递条目的推送字段（8.3）与 origin 结构（8.4）；删除 `relay` 参数；`adapter.identities.list` 去掉 `hyprialOwner` |
| CLI | `user bind|unbind|whois`、`user list --source`、`user bindings audit|publish|withdraw`；`squire setup --owner-open-id --binding-code` | `user proxy setup|status` 不再读写 receivers；`adapter identities list|find` 输出列 |
| GUI（GUI 线做） | 「配置绑定」「查询绑定」、身份绑定列 | 消息的说话人显示 |
| 错误码 | `IDENTITY_OVERRIDE_FORBIDDEN` 无人使用（`FROZEN_CODES` 删一项需 Allen 批准）；user 推送上的 `IDENTITY_*` 不再出现；`IDENTITY_UNBOUND` 在 onboarding 另有含义，保留 | `UNCONFIGURED_SQUIRE_MESSAGE` 改为指向 user-proxy |
| skill | `hyprial-ops/SKILL.md` 的 verified 指引、`user bind` 补登记、五种来源、授权白名单一句 | 通用的发送者说法（8.4）；发给 `user:` 何时推送、空档 |

- **as-built 补充（2026-11-20，#2989）**：另删 IPC 方法 `autoupdate.notify`；升级结果的 `ownerAlert`、`restart.followUp.alert`、`survivorAlert`；失败标记的 `alert`；last-run 的 `notification`；启动警告 `SQUIRE_PROFILE_UNREADABLE`、`SQUIRE_RECEIVERS_UNAVAILABLE`、`SQUIRE_NOT_CONFIGURED` 改为 `SQUIRE_USER_PROXY_NOT_CONFIGURED`；`message.send` 响应里的 `IDENTITY_CONFLICT`、`IDENTITY_SOURCE_UNAVAILABLE`、`IDENTITY_UNBOUND`；delivery entry 去掉 `adapter`。后续项：`hyprial log --event` 过滤与 GUI 日志页 `log.page` 的事件白名单（KP-114）。
- `Contract-Impact`：上表各项；删除 `contract/owner-relay/`；`Skill-Impact`：`hyprial-ops/SKILL.md`、squire 相关文字。
- 会动的登记【取自清查 R-grep，实施时逐项复核】：`contract/daemon-request/openapi.json` 与生成的 GUI 客户端、`contract.generated.json`；`contract/cli/surface.json`、`docs/reference/cli-command-inventory.json`、`post_install_surface.json`；`scripts/ci/daemon-request-architecture-baseline.json`、`scripts/ci/import-ratchet-exceptions.json`；`tests/tooling/architecture/ipc/test_legacy_permit_retirement.py` 的方法表；kernel 的六个 `IDENTITY_BINDING_*` 预算；`contract/e2e/09-squire-setup.md`、`run-squire-setup.py`；golden 快照。都只减不增；结构测试 `test_login_names_no_adapter.py` 可随删除扩大范围（收紧）。
- **已被取代（2026-11-20，#2991）**：下面的 GLOSSARY 清单里，R4 只改了 `user`、`squire`；`platform identity sync`、`lookup` 在 R1 之后仍成立，未改，R3 时再看；`route` 随 cell 切片。
- GLOSSARY 要改的条目（不在本文改，R4 里改）：`user`（端点说法、「不进记录」的现状）、`squire`（「经 user delivery 送到 owner 的 adapter 端点」）、`platform identity sync`（`verified` 一句）、`platform identity lookup`（随 GUI 面板删除）；随 cell 切片：`route`（第 2 种情况按 cell 连接数据改写）。

### 8.7 切片

没有迁移片、没有兼容片。替代先于删除只在同组切片之间成立。

**本轮（群聊合入之后，不依赖 cell）：**

（2026-11-20 状态：R1 已合入 #2970，R2 已合入 #2989，R4 已合入 #2991；2026-11-21：R3 已合入 #3002，迁移说明已随它同步。）

| 片 | 内容 | 测试 | 需要真实飞书 |
|---|---|---|---|
| R1 入站通用化 | 8.4 的发送者块、提示行与 skill；转述证据退役（`inbox/relay/`、`message.relay.verify`、`relay` 参数、`contract/owner-relay/`）；T2-A 删除 | 块里没有 owner/standing/unionId；不同 adapter 种类同一形状；`relay` 参数与 `message.relay.verify` 不再存在 | 单聊、群各收一条 |
| R2 出站：两条私信路径删除与第 3 种情况 | 删 squire 私信、agent 机器人按 open_id 私信、`owner_open_id`/`profile_open_id`、尝试账本、`ownerOpenId`、squire 两个选项；user-proxy 本机事实与交接；`push_evidence`、3 秒等待、本 owner 推送的 Zenoh 交接删除；daemon 自己的通知改走入队路径；投递条目新形状；升级失败提醒改发各 adapter 的默认路由（**已被取代（2026-11-20）**：实际是「daemon 通知只写日志」「升级失败提醒删除」，见修订节第 1、2 行） | 无 pin 的 agent：有 proxy → 交接一次（proxy 不在运行也入队）、无 proxy → `pushed: false` 且无等待；pin 了飞书 adapter 的 agent → `pushed: false`（空档）；setup 不再写 receivers | proxy 转发一次 |
| R3 解析器与证明链删除 | T2-B、T2-C（含方法、CLI）、T2-F；契约与快照重生 | 删除后 R1、R2 的测试仍通过；结构测试扩大 | 不需要 |
| R4 文档 | 迁移说明（含空档）与 CHANGELOG 链接；GLOSSARY 本轮条目；KP-105 改写 | 文档测试、GLOSSARY 锚点 | 不需要 |

- R1、R2 可并行；R3 在 R1、R2 之后（入站与出站都不再调用解析器）；R4 与 R2 同一发布，空档的说明必须与 R2 同时到达用户。GUI 线在 R3 的契约合入后删面板。`receivers.json` 文件与 `org.receivers.*` 的删除与组织线 W1f 同步。

**等 cell 切片（#2712）：**

| 项 | 内容 |
|---|---|
| 第 2 种情况 | 按 agent cell 的 adapter 成员与连接数据推到绑定的聊天（增补稿中关于出站绑定的条目）；落地即结束空档；迁移说明补一节 |

- 明确留到以后：跨 owner 的人与人消息（R8）；按人员登记里的 IM 账号把发送者显示为某人（7.6 第 5 条：本轮不用）；其他种类 adapter 接入 agent 路径；多机。

### 8.8 风险与无法在本地验证的

- **空档**（7.5、8.3；已登记为 KP-113）：pin 了飞书 adapter 的 agent 在 cell 切片之前不推送到飞书；依赖它在飞书里收 agent 主动消息的用户会察觉。迁移说明必须写明，并与 R2 同时发布。
- **用户通知变少**：今天经 squire 私信收到的 daemon 通知与无 pin agent 的消息，没有 user-proxy 的用户升级后只能在 GUI、CLI 看到。这是裁定的目标状态。**已被取代（2026-11-20）**：daemon 通知现在任何用户都只在日志里；`hyprial log` 没有按事件名过滤（KP-114）。无 pin agent 的消息仍按本条。
- **飞书侧**：user-proxy 转发需要应用在 proxy 路由所指的聊天里；要在真实飞书里测。
- 本文未运行任何测试或 daemon。

## 9. 需要 Allen 裁定

### 9.1 待决定

目前没有待裁项。

### 9.2 已裁定

此前的第 9 节五项，Allen 2026-11-19 回答「同意」，均按推荐裁定（7.6）：

| # | 问题 | 裁定 | 落在哪里 |
|---|---|---|---|
| 1 | 转述证据（`message.relay.verify`、`relay` 参数） | 退役 | 8.4；R1 删除 `inbox/relay/`、`message.relay.verify`、`relay` 参数、`contract/owner-relay/` |
| 2 | 发给另一 owner 的 `user:` 目标 | 保留今天的 Zenoh 交接直到跨 owner 对话（R8），接收方只用自己的 user-proxy | 8.1 |
| 3 | daemon 起不来时的升级失败提醒 | ~~发到各 adapter 的默认路由~~ **已被取代**：删除（Allen 2026-11-19 推翻原裁定） | 修订节第 1 行；#2989 |
| 4 | OrgFS 受保护段名 `people` | 保留（无读者的词表项） | 8.5 |
| 5 | 人员登记里的可选 IM 账号用于显示发送者 | 本轮不用 | 8.7「留到以后」 |
| 6 | daemon 通知 | 只写日志，不推送（Allen 2026-11-19） | 修订节第 2 行；#2989 |
| 7 | routine、workflow、PAC 报告 | 由负责的 agent 发出；无本机负责 agent 时记日志 `workflow.user_report.unsent` | 修订节第 3 行；#2989 |
| 8 | 批量读取遇到不认识的 origin 字段 | 丢掉，只改发布视图 | 修订节第 8 行；#2970 |

其余：清查的问题 1（入站是否告诉 agent「这是 owner」）：不判定（7.1、7.3）。问题 2（adapter 怎么得知 owner 的 open_id）：不需要，推送按 agent 的绑定（7.2、7.4）。问题 3、4（S5–S7 与已复制绑定行）：不迁移，新版本不读，手工步骤写进迁移说明（原则 3）。问题 5（人员登记）：保留。问题 6（T2-A 的时机）：本轮与入站通用化一起删。空档：先删除并接受（7.5）。KP-110：没有任何东西以用户 agent 的地址收发，不触及。`receivers.json`：本轮不再读写，文件随 W1f 删除。
