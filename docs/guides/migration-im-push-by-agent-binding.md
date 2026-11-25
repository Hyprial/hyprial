# 迁移说明：发给你的消息不再按你的 IM 账号推送；daemon 通知只写日志

适用于：从 0.5.5.3.x（或更早）升级到包含 PR #2970（R1）、#2989（R2）与 R3（身份解析器与绑定证明链的删除）的版本。读者是做升级的人，或替人做升级的 agent。

一句话：hyprial 不再把用户和 IM 账号绑在一起。发给你（`user:<你>`）的消息一律进对话，在 GUI 和 CLI 里看；只有在下面第 2 节的条件下，才会另外推一份到飞书。daemon 自己不再给你发任何东西，它的通知写进日志。

本说明只写当前 dev 上已经成立的事。标 UNKNOWN 的是没有在真实环境核实的。

## 1. 不再发生的事

已经删除（当前版本）：

- **squire 私信你**。squire 不再代别的 agent 给你发私信。
- **agent 机器人按你的 open_id 私信你**。agent 自己 pin 的飞书机器人不再按你的账号给你发私信。
- **`hyprial squire setup` 命令及其 `--owner-open-id`、`--binding-code`、`--channel` 选项**。该命令已删除，旧脚本中的调用会失败；没有等价的现行命令。
- **升级前的「即将重启」通知**（IPC 方法 `autoupdate.notify`）与**升级失败告警**。daemon 起不来时，不再向任何 IM 发消息（第 6 节）。
- **转述证据**：`message.relay.verify`、`message.send` 的 `relay` 参数、CLI 的 `--relay-message-id`（第 8 节）。
- **入站消息里的 owner 判定**：飞书消息不再带 `owner`、`standing`（verified 等）、`unionId`、`userKey`，提示行也不再写 `NOT verified`（第 8 节）。
- **登录时记下飞书 union id**：登录不再写 `settings.json` 的 `identityClaims`。
- **`hyprial user bind`、`user unbind`、`user whois`、`user list --source …`、`user bindings audit|publish|withdraw`**，以及它们背后的 daemon 方法 `identity.resolve`、`identity.override.set|clear`、`identity.bindings.list|audit`、`identity.binding.publish|withdraw`（R3）。旧脚本里的这些调用会失败：CLI 报 `No such command`，daemon 答 `METHOD_NOT_FOUND`。
- **身份解析器、本地覆盖库、用户库里的平台账号与绑定证明的发布和核验**（R3）。新版本不读也不写 `identity.sqlite3`、`users.sqlite3` 里的 `user_accounts`、两个绑定缓存文件、`settings.json` 的 `identityClaims` 与 `identityBinding`（第 9 节），也不再往组织空间发布或撤回绑定行（第 10 节）。
- **`hyprial user list` 列的是人，不是绑定行**（R3）：`--json` 仍是 `{"rows": [...]}`，每行是一个人（`userKey`、`kind`、`owner` 与名字）；没有 `--source`。
- **平台身份列表里的「hyprial owner」**（R3）：`adapter.identities.list` 与 `hyprial adapter identities list|find` 不再有 `hyprialOwner`。
- **GUI「平台接入」详情里的「配置绑定」「查询绑定」与身份绑定列**：它们调用的方法已删除，由 GUI 线随 R3 删除。

`hyprial user add|show|list`（只有名字的人员登记）保留。

另：squire 本身、send-link 组件与 thread 这个名词，将由 cell 模型那条线另行删除，本说明不涉及。

## 2. 发给你的消息现在出现在哪里

- **总是**：本机 agent 发给 `user:<你>` 的消息照常入队，并记进该 agent 与你的默认对话。在 GUI 聊天里，或用 CLI 的消息与对话命令都能看到。对话记录只在发送它的那台机器上；同一 owner 的多台机器之间不同步。
- **另外推一份到飞书**：
  1. 发消息的 agent 的 cell **有 adapter 角色成员**：daemon 推到这个 cell 写明的聊天（第 3 节，I3），`via: "cell"`；
  2. 否则（agent 没有 adapter 成员，或发送者是 operator 等不是本机 agent 的发送者），daemon 推到**本机 user agent cell**（`hyprial-user`）写明的聊天（第 4 节，I7a），`via: "user-agent"`。user agent cell 里没有 adapter 成员时不推送。user-proxy 不再收到任何副本。

`message.send` 的结果里能看到推没推：推了是 `pushed: true` 和 `via`；没推是 `pushed: false` 和 `pushCode`，不再等待 3 秒。daemon 日志里对应一条 `send.user_push`，没有目标时带 `pushSkipped`：`sender-cell-silent`（cell 里别的成员负责推送）、`sender-member-unknown`（cell 有多个 harness 成员但没有 entry）、`cell-unavailable`（读不到发送方或 user agent 的 cell）、`no-user-agent-target`（user agent cell 没有推送目标，或本机没有 user agent）或 `sender-is-adapter`（发送方本身是 adapter 地址，从不推回去）。

发给**另一个 owner** 的 `user:` 消息仍走原来的跨机交接；对方那台接收机推到它自己的 user agent cell（I7a），那里没有推送目标就答 `TARGET_UNRESOLVED`；推送结果未知时答 `USER_DELIVERY_FAILED` 并记下，不会再推第二次。不再有 squire 私信，也不再交给 user-proxy。

## 3. 绑定了飞书 adapter 的 agent：推到它自己的聊天（I3）

从 I3 起，agent 的 cell 里有 adapter 角色成员时，daemon 把它发给你的消息直接推到这个 cell 写明的飞书聊天；routine / workflow 报告（第 5 节）也一样。user-proxy 不再收到这类 agent 的副本。

1. **改了什么**：结果是 `pushed: true`、`via: "cell"`。只绑定一个 adapter、且该 adapter 在 `channels.json` 里设了 `defaultRoute` 的 agent，不需要做任何事。
2. **检查 `defaultRoute`**（`hyprial adapter route list`），或者在 cell 里写一条显式连接：

   ```
   hyprial agent cell show <agent> --json
   # 在 params["cell.connections"] 里加一行：
   #   {"from": <发出成员>, "on": "user", "to": <adapter 成员>, "route": <route 名>}
   hyprial agent cell apply <agent> --expected-revision <N> --file CELL.json
   ```

   adapter 没有 `defaultRoute`、cell 里也没有这样的连接时，不推送（`pushCode: TARGET_UNRESOLVED`）。
3. **多个聊天**写多行连接，每行一个聊天。**让某个成员不推**：把 `user` 连接写给另一个成员；cell 里只要有一条 `user` 连接，没有自己那一行的成员就什么也不推。
4. **怎么看结果**：`pushed`、`via`、`pushCode`，以及日志 `send.user_push` 里每个目标一行 `targets[]`（`member`、`adapter`、`route`、`state`、`code`、`reason`）：
   - `TARGET_UNRESOLVED`：成员不是 adapter、成员没写 `adapter`、adapter 不是本机配置的飞书网关、没有 route 也没有 `defaultRoute`、route 不存在；
   - `USER_DELIVERY_UNAVAILABLE`：请求还没发出就失败了（`channels.json` 或密钥文件读不了），或者读不到发送方的 cell；
   - `USER_DELIVERY_FAILED`：飞书拒绝（缺权限、机器人不在群里等）；缺权限时授权链接在这行日志的 `data` 里，不在发送结果里；
   - `SUBMIT_OUTCOME_UNKNOWN`：可能已经到了，也可能没有（网络错误、超时）。
5. ⚠️ **飞书副本只尝试一次**。发送那一刻飞书连不上，这份副本就不会再补发；消息本身照常入队，在 GUI 和 CLI 里看得到，结果是 `pushed: false` 加 `pushCode`，日志里写明每个聊天的原因。daemon 不重试。
6. role 为 `adapter` 的成员必须写 `cell.members.<成员>.adapter`；不写就哪里都不推（`TARGET_UNRESOLVED`），也不会改走 user agent cell。
7. ⚠️ **cell 运行时组合失败期间，本机 agent 发给你的消息被拒绝**（KP-129）。daemon 日志出现 `cell.runtime_unavailable` 之后，本机不再把发给 `user:<你>` 的消息入队，每条都只走推送；读不到发送方的 cell，于是本机**每个** agent（绑定了 adapter 的和没绑定的都一样）发给你的 `message.send` 都以 `USER_DELIVERY_UNAVAILABLE` 失败，日志 `send.user_push` 带 `pushSkipped: "cell-unavailable"`。消息既不入队、不进对话记录，也不推送；发送方拿到错误，可以稍后用同一个 key 重发。I7a 起 operator 等不是本机 agent 的发送者也一样（读不到 user agent cell）。routine / workflow 报告遇到这个错误码会保留并重试（重试有没有上限未核实：UNKNOWN）。
   - 持续多久看 `cell.runtime_unavailable` 的 `reason`：OrgFS 一时不可用这类瞬时原因，daemon 每 30 秒自己重试组合，成功后发送自动恢复；`cache-identity-mismatch`、`cache-unreadable`（持续出现时）与 `authority-uid-missing` **不会自己好**，直到有人修复为止，本机所有 agent 发给你的消息一直被拒。
   - 怎么恢复：按 `docs/guides/migration-cell-authority-uid.md` 第 4 节处理对应的 `reason`（`cache-identity-mismatch` / `cache-unreadable`：停机，人工备份并移走 `state/cells.sqlite3` 三个文件，再启动；`authority-uid-missing`：补齐 uid 后 `hyprial daemon restart`）。之后日志不再出现 `cell.runtime_unavailable`，发送恢复为入队加推送。

## 4. 没有 adapter 的 agent 与 operator：经 user agent cell 推送（I7a）

从 I7a 起，user-proxy 不再收到任何副本。没有 adapter 成员的 agent、operator 发给你的消息，由 daemon 推到本机 **user agent**（`hyprial-user`）的 cell 写明的聊天，`via: "user-agent"`。

1. ⚠️ **升级后、做完第 2 步之前，这类消息只在 GUI 和 CLI 里**。`hyprial user proxy setup` 已删除（I7b，第 14 节）；I7a 与 I7b 之间不单独发版。
2. **把绑定挪到 user agent cell**：先从 user-proxy agent 的 cell 里去掉 adapter 成员，再加到 user agent 的 cell：

   ```
   hyprial agent cell show hyprial-user --json
   # members 里加：{"ref": "im", "role": "adapter"}
   # params["cell.members"] 里加："im": {"adapter": "<adapter 名>"}
   # 可选，params["cell.connections"] 里加一行：{"from": "im", "on": "user", "to": "im", "route": "<route 名>"}
   hyprial agent cell apply hyprial-user --expected-revision <N> --file CELL.json
   ```

   没有 `user` 连接时推到每个 adapter 成员的 `defaultRoute`；多个聊天写多行连接。同一个 adapter 可以同时绑定在一个普通 agent 的 cell 里：user agent cell 只往外推、从不接收（A-8），不会冲突，一个飞书机器人就够。
3. **user agent cell 里只能改这些**：adapter 成员、它们的 `cell.members.<成员>.adapter`、以及 `on` 为 `user` 的连接。其余字段（`cellName`、`agent.template`、`agent.role` 等其它 param）必须原样保留；`cell show` 的输出可以原样 `cell apply` 回去。改了别的会得到 `INVALID_ARGUMENT`；改了或删了 `agent.template` 得到 `USER_AGENT_IMMUTABLE`。只有本机操作者能读这个 cell，agent 调用者读它得到 `USER_AGENT_IMMUTABLE`。
4. **你在那个聊天里写的话不再被转发**（A-2）：如果同一个 adapter 绑定了一个普通 agent，消息交给那个 agent；否则得到 “No agent is bound to this adapter”。引用回复一条推送过来的消息，也不会回到发送它的 agent。`hyprial adapter list` 对只绑定在 user agent cell 里的 adapter 显示没有 agent；删除这个 adapter 时预览里的 `boundAgent` 是 user agent。删除不会被拒绝：删除后经它推给你的副本以 `TARGET_UNRESOLVED`（`adapter-unconfigured`）结束、不会改投别的聊天，重新添加这个 adapter 即恢复。
5. 其他 owner 发给你的消息也推到同一个聊天。
6. ⚠️ 推送只尝试一次（与第 3 节第 5 条相同）。

## 5. daemon 的通知：只写日志

daemon 不再给你发消息，也不往 GUI 对话里写通知。每条通知是一条 `warn` 级、component 为 `daemon` 的日志，带全文 `text` 与 `idempotencyKey`：

| 事件名 | 内容 |
|---|---|
| `quota_watchdog.notice` | 配额看门狗 |
| `inbox_watchdog.notice` | 收件箱（收信）看门狗 |
| `daemon.operator_alarm.notice` | 运维告警 |
| `orgfs.owner_notice` | OrgFS 完整性通知 |
| `agent.blocked.notice` | agent 被阻塞 |
| `provider_auth.notice` | 模型提供方需要重新登录 |
| `owner.notice` | 其他发给 owner 的告警 |
| `workflow.user_report.unsent` | 没有本机 agent 可以代发的报告（见下） |

读取：

```
hyprial log --component daemon --level warn --since 2026-11-19T00:00:00Z --json
```

`--since` 要带时区的 RFC 3339 时间。现在还没有按事件名过滤的选项（没有 `--event`），请在输出里按 `event` 字段筛选。`hyprial log` 直接读本机的日志文件，daemon 不在运行时也能用。

**routine / workflow / PAC 报告**改由负责的 agent 作为普通消息发给你，进它与你的默认对话（推不推飞书按第 2、3 节）。发送者：routine 闹钟依次取 `actor`、`produces`、`owner`；workflow 输出取完成它的 agent。找不到本机 agent 时（例如你自己发起的操作、PAC 超时时钟、来自别的机器的来源、没有 actor），报告不发送，只记一条 `workflow.user_report.unsent`（全文在 `text` 里）。

## 6. 升级失败与重启

- **升级失败**：没有告警。看三处：
  1. `hyprial daemon upgrade` 的输出（`--json` 时是一个对象，失败为 `ok: false`）与退出码（失败为非零）；
  2. `hyprial daemon upgrade status`，其中 `lastRun` 是最近一次运行的记录（文件 `<state 目录>/autoupdate-last.json`）；
  3. 重启失败时的标记文件 `<state 目录>/upgrade-restart-failed.json`（`kind: upgrade-restart-failed`，含 `summary` 与 `detail`；不再有 `alert` 字段）。

  state 目录默认是 `~/.hyprial/state`。
- **重启**：不再提前通知。重启在 daemon 日志里留下 `daemon.stopping` 与 `daemon.ready`（component `daemon`，`info` 级）；自动升级另有 `autoupdate.run.started` 与 `autoupdate.run.completed`（component `autoupdate`，失败时后者为 `error` 级并带 `exitCode`）：

  ```
  hyprial log --component daemon --level info --since <时间> --json
  hyprial log --component autoupdate --since <时间> --json
  ```

## 7. 升级前存下的消息

升级前收下、还没读的消息照常可读。旧消息的来源信息里有当前版本不认识的字段（如 `chatType`、`owner`、`standing`），批量读取（`message.pending.batch`）会把这些字段丢掉，只留当前认识的部分；存储本身不改。

## 8. agent 眼里的发送者

所有 adapter 种类同一个形状，hyprial **不再说谁是 owner**：IM 上的发送者就是拥有那个 IM 账号的人。`from` 是 adapter 的地址（回复经它回去），来源信息里是 adapter 给出的平台事实：

- 私聊：`chatKind=dm, provider=lark, sender={kind=user, platformId=<平台账号 id>, displayName=<显示名>}`，提示行为 `<显示名> (via <adapter 地址>, account <平台账号 id>)`。
- 群聊：形状相同，`chatKind=group`。

显示名来自 adapter 的观测，可能为空（这时提示行用账号 id）；它是平台上任何人都能改的文字。owner 的指令以 `user:<owner>` 的身份到达（GUI、CLI）。

转述证据已删除：agent 之间转述 IM 消息时，daemon 不再出具证明，`message.relay.verify` 不存在了。

## 9. 不再读取、可以删除的文件

state 目录下（默认 `~/.hyprial/state`）：

- `squire-setup.json`：旧的十分钟绑定码，当前版本不再打开它。
- `users.json` 里本机档案的 `ownerOpenId` 字段：不再读取，档案下次写回时自动去掉，不需要手动处理。
- `identity.sqlite3`：旧的本地覆盖库，当前版本不再打开它。
- `verified-org-bindings.json`、`identity-binding-suppression.json`：其他成员绑定行的缓存与发布屏蔽名单，当前版本不再打开它们。

hyprial home 下：

- `settings.json` 的 `identityClaims` 与 `identityBinding` 两个键：不再读取，登出也不再清理；可以手工删掉这两个键（文件里其余的键不要动）。
- `users/<用户 key>/profile.json` 里的 `accounts`：只是镜像，下次写回这个人时自动去掉。

**不要删** `state/users.sqlite3`：它仍是人员登记（`hyprial user add|show|list`）。里面旧的 `user_accounts`、`user_channels` 表不再被读或写，留着无害；新建的库不再有这两张表。

`adapters.sqlite3` 里平台身份表的 `hyprial_owner` 列留着（表结构不改），不再被读出。

## 10. 组织空间里的旧绑定行

旧版本可能把你的绑定行发布到了你加入的每个组织（`directory/people/<你>/binding.json`），别的成员的 daemon 据此识别你的飞书账号。当前版本的入站已不再用它；但还在旧版本上的成员仍会用。想撤回，在**升级之前**、在旧版本上运行：

```
hyprial user bindings withdraw --all
```

这条命令在 0.5.5.3.4（`stable`）里存在；**当前版本已没有它**（R3），升级之后无法再撤回，所以必须在升级之前运行。只撤回某一个组织用 `--org <org>`。不撤回也不影响你自己的新版本：新版本不读这些行。

## 11. 新旧版本混跑

- 你自己的另一台机器若还是旧版本，它发出的消息照旧按旧规则（squire 私信、机器人私信）推送，只影响它自己。
- 旧版本的组织成员继续读已发布的绑定行（第 10 节）。
- 旧版本节点发给你的 `user:` 消息到了新版本节点，推到你的 user agent cell，没有推送目标就答 `TARGET_UNRESOLVED`（I7a）。同一 owner 的各节点请一起升级。

## 12. 如果什么都不做

- 消息都在 GUI、CLI 的对话里，不会丢；只是除了第 2 节的情况，不会出现在飞书。
- daemon 的通知只在日志里，不主动看就不会知道。
- 旧的 `squire setup` 脚本会失败，因为该命令已删除。
- 旧绑定行留在组织空间里，旧版本成员仍会用它。
- 没有第 13 节的 `node-id` 文件（也没有设置 `HYPRIAL_NODE_ID`）时，daemon 不启动。

## 13. node id file（每台已有安装必做一次）

适用于：包含「node id 来自 home 文件」改动（设计说明 `docs/design/node-id-from-home-file-2026-11-20.md`）的版本。与上面各节无关，单独做。

从这个版本起，daemon 不再读主机名来确定本机的 node id（agent 地址 `agent:<owner>:<node>:a.<id>` 里的 `<node>`）。node id 只从 home 里的一个文件读：`<HYPRIAL_HOME>/node-id`（默认 `~/.hyprial/node-id`），一行文本。设置了 `HYPRIAL_NODE_ID` 时仍以它为准。

- **新装**：`hyprial init`（或第一次 `hyprial login`）创建新 home 时自动写入一个随机值 `d.<32 位十六进制>`。不用做任何事。
- **已有安装**：没有转换、没有迁移代码、也不会退回主机名。文件缺失时 `hyprial daemon start` / `daemon run` 拒绝启动，报 `NODE_ID_NOT_CONFIGURED` 并指向本节；需要 node id 的离线命令（如 `adapter remove`、`login`）同样失败。对已有 home 重跑 `hyprial init` **不会**写这个文件。

把本机**现在正在用的** node 值原样写进文件，一次即可。现在用的值是：daemon 环境里设置了 `HYPRIAL_NODE_ID` 就是它，否则是主机名。macOS / Linux，在启动 daemon 的同一环境里运行：

```
printf '%s\n' "${HYPRIAL_NODE_ID:-$(hostname)}" > "${HYPRIAL_HOME:-$HOME/.hyprial}/node-id"
```

Windows（PowerShell，默认 home、未设置 `HYPRIAL_NODE_ID`）：

```
hostname | Set-Content "$env:USERPROFILE\.hyprial\node-id"
```

写之前可以核对：`hyprial agent list --json` 里本机 agent 地址的第三段应与要写的值完全相同。如果之后改了主机名，文件里的值不变。从此它只是一个不透明的字符串，和真实主机名无关。已存储的数据不会被改写；地址照旧可用，混跑的旧版本机器也不受影响，因为线上的值没有变。

两台机器复制了同一个 home（文件相同）时，它们会共用一个 node id，这种情况不处理。

不做这一步的后果：daemon 不启动。

## 14. user-proxy 已删除（I7b）

适用于：包含 I7b 的版本。user-proxy 在 I7a 起已经收不到任何副本（第 4 节）；I7b 删除它剩下的命令和权限。

**已有 user-proxy 的 home，先在旧版本上按这个顺序做，再升级**（user agent cell 只能在新版本上改；销毁一个仍绑定 adapter 的 agent 会被拒绝）：

1. 旧版本上：记下 user-proxy 的 adapter 和 route（`hyprial user proxy status`，或它启动时的 `--route`）。
2. 旧版本上：用该版本自己的命令把 adapter 从 user-proxy agent 上解绑（有 `hyprial agent cell` 的版本改 cell 文档；更早的版本用 adapter pin 命令）。
3. 旧版本上：`hyprial agent stop <proxy>`，再 `hyprial agent destroy <proxy>`。
4. 升级。
5. 新版本上：按第 4 节第 2 步把 adapter 成员（以及可选的 `user` 连接）加到 user agent cell。

第 2 步到第 5 步之间，没有 adapter 的 agent 发给你的消息不推送，只在 GUI 和 CLI 里。

⚠️ **不先销毁就升级，daemon 起不来**（A-4，KP-132）。新版本不认识 `user-proxy` 这种 harness，desired state 里还留着它的启动记录时，daemon 启动即退出。没有兼容代码，也不会自动删掉这条记录。

你在终端上看到的：`hyprial daemon start`、`hyprial init`、`hyprial agent list` 等需要 daemon 的命令都失败，只给出

```
hyprial: daemon exited during startup with status 1
```

加 `--json` 时是 `{"ok": false, "code": "DAEMON_START_FAILED", "error": "daemon exited during startup with status 1", "data": {"logPath": "<HYPRIAL_HOME>/state/daemon-launch.log", ...}}`。终端上不出现原因。原因在 `<HYPRIAL_HOME>/state/daemon-launch.log` 的最后一行，形如：

```
{"ok":false,"error":"providers[0] has an invalid harness; expected codex, claude, pi, jev, or agy",...}
```

它只给出序号（`providers[0]`），不给 agent 名。看到 `DAEMON_START_FAILED` 且这一行写着 `invalid harness` 时，就是这个问题。`hyprial daemon status` 不受影响，照常可用。手工修复：

1. 确认 daemon 没在跑（`hyprial daemon status` 答 `running: false`）。
2. 删掉那一行。记录在 `<HYPRIAL_HOME>/state/lifecycle-operations.sqlite3`（默认 `~/.hyprial/state/`）的 `harnesses` 表里：

   ```
   sqlite3 "${HYPRIAL_HOME:-$HOME/.hyprial}/state/lifecycle-operations.sqlite3" \
     "DELETE FROM harnesses WHERE harness = 'user-proxy';"
   ```

   很旧的 home 如果还有没导入的 `state/desired-state.json.v1`，把它 `providers` 数组里 `"provider": "user-proxy"` 的那一项删掉（这个文件导入成功后会被改名为 `.v1.migrated-<时间>`）。

   同一个库的 `lifecycle_resources` 表里会留下一行 `harness:user-proxy:<proxy 名>`。不要删它：实测它留着时 daemon 能反复启停，`agent list` 与 `agent destroy` 都正常。
3. 启动 daemon（`hyprial daemon start`）。user-proxy 的 agent 记录还在（`agent list` 里仍显示 `user-proxy` 为它上次与首选的 harness，无害；对它 `agent start` 答 `INVALID_ARGUMENT`）：先用 `hyprial agent cell apply <proxy>` 去掉它 cell 里的 adapter 成员，再 `hyprial agent stop <proxy>`、`hyprial agent destroy <proxy> --yes`，然后按第 4 节第 2 步配置 user agent cell。

实测（隔离 home，含一个真实运行过的 user-proxy）：上面的现象、第 2 步删行、第 3 步的启动、`agent list`、`agent stop` 与 `agent destroy`，以及之后再次停启 daemon，都 PASS。BLOCKED：第 3 步的 `agent cell apply` 去掉 adapter 成员，隔离 home 里无法有真实飞书 adapter，没有实测。UNKNOWN：lifecycle 日志里还有该 proxy 未完成操作的 home。

删除的命令与替代：

| 删除的命令 | 现在怎么做 |
| --- | --- |
| `hyprial user proxy setup` | 第 4 节第 2 步（`hyprial agent cell apply hyprial-user`） |
| `hyprial user proxy status` | `hyprial agent cell show hyprial-user` |
| `hyprial agent start user-proxy` | 无。`agent start` 只接受 claude、pi、codex、jev、agy；daemon 的 `agent.start` 对 `user-proxy` 答 `INVALID_ARGUMENT` |

旧脚本里的 `hyprial user proxy …` 以用法错误退出（退出码 2，`No such command`）；`hyprial user-proxy` 不再提示 `moved:`。

**不再有的东西**：user-proxy 的 python worker 与它的 `forward` 帧（daemon 不再代任何 worker「以它的身份发出」），`message.send` 不再出现 `FORWARD_TARGET_UNKNOWN`；worker 协议契约 `contract/workers/user-proxy` 已删除。你在那个飞书聊天里写的 `@收件人 …` 不再被转发（A-2，见第 4 节第 4 步）。

**组织 ACL**（A-3）：ACL 空间的写入与 `org.acl.grant|revoke|ls` 只认 owner 自己的终端和 owner 显式授权的 agent。user-proxy 不再是单独的调用方：它和普通 agent 一样，未授权时写入得到 `ORG_ACL_WRITE_DENIED`，管理授权得到 `ORG_ACL_GRANT_FORBIDDEN`。错误码不变。

**新旧版本混跑**：同一 owner 的旧版本机器仍期待接收机上有 user-proxy；把同一 owner 的机器一起升级。
