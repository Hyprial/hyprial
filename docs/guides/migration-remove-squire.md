# 迁移说明：去掉 Squire 与外挂 user-proxy 的遗留提醒（S2c）

适用：从含 `SQUIRE_USER_PROXY_NOT_CONFIGURED` 启动提醒的版本升级到本版本。
本版本不带任何迁移命令或自动改写；下面需要做的事都由 agent 或用户手工完成。

> 后续变化（I7b）：下文提到的 `hyprial user proxy setup|status`、`hyprial agent start user-proxy` 与 desired state 里的 `user-proxy` harness 已在同一发布内删除。第 2 节第 2、6、7、8 步只适用于停在 S2c 与 I7b 之间的版本；升级到包含 I7b 的版本时，改按 [user-proxy 已删除](migration-im-push-by-agent-binding.md#14-user-proxy-已删除i7b) 处理（先在旧版本上销毁 user-proxy，再在新版本上配置 user agent cell）。

## 1. 删除的已发布面

- 启动警告 `SQUIRE_USER_PROXY_NOT_CONFIGURED`（含 `data.owner`、`data.userProxies`、
  `data.nextStep`）。`hyprial init`（daemon 已在运行与新启动两条路径）、首次
  `hyprial login` 与 `hyprial daemon restart` 的 `warnings` 里不再出现它，非 JSON 输出也不再向
  stderr 打印对应提醒。没有替代码，也没有改名后的同类警告。其他 warnings
  （`ORG_CONTEXT_ABSENT`、`ZENOH_ENDPOINTS_UNSET`、`DAEMON_BOOTSTRAP_NO_ACCOUNT` 等）不变。
- 配置键 `hints.userProxy`。`hyprial config set hints.userProxy on|off` 现在以
  `INVALID_CONFIGURATION`（unknown config key）拒绝。
- `hyprial user proxy setup` 对 `state/users.json` owner 档案的前置检查：没有档案时不再以
  `NOT_FOUND`（`no local user profile; run hyprial squire setup first`）拒绝；setup 不读、不写、
  不创建该文件。默认名按本机 owner 计算为 `<identity_slug(owner)>-proxy`，不再取自档案的
  `ownerKey`。由产品默认写入的档案，`ownerKey` 就是这个 slug，名字不变；用过
  `hyprial squire setup --owner-key <值>` 或做过 owner 改名的 home，旧 proxy 可能叫
  `<旧 ownerKey>-proxy`，与新默认名不同。
- 不带 `--name` 时，如果 desired state 里已经有另一个名字的 user-proxy，`user proxy setup`
  以 `INVALID_ARGUMENT` 拒绝（`this node already has a user-proxy named <名字> …; rerun with
  --name <名字> to keep using it`），不新建 agent、不改 pin、不写 desired state。带了 `--name`
  时照用该名字。
- `hyprial user proxy setup --json` 回执的 `profile` 字段（它从未写回档案，只是镜子）。

## 2. 手工步骤

1. 升级前备份 `HYPRIAL_HOME`。
2. 解析 `warnings` 的脚本：不要再等待或匹配 `SQUIRE_USER_PROXY_NOT_CONFIGURED`；
   需要知道本机是否有 user-proxy 时，显式运行 `hyprial user proxy status --json`，
   看 `configured` 与 `live`。
3. 调用过 `hyprial config set hints.userProxy …` 的脚本：删掉这一步，否则会失败。
4. `settings.json` 里旧的 `hints` 段（如 `{"hints": {"userProxy": "off"}}`）不再被读取，
   留着无害；想清理时由 agent 手工从 `settings.json` 删除该键，其余键保持不动。
5. 不要在升级脚本里调用已删除的命令（`hyprial squire setup`）或试图恢复这条警告。
6. 不需要为 user-proxy 创建 `state/users.json`。`hyprial user proxy setup --json` 回执已删除
   `profile` 字段；旧脚本若依赖原 `profile` 字段，移除对该字段的读取，改为直接读取回执中的
   `name`、`deliveryAgent` 与 `actor`（若需在重跑 setup 前获知本机现有 user-proxy 名，先按第 7 步
   运行 `hyprial user proxy status --json`）。
7. 重跑 `hyprial user proxy setup` 的脚本：先运行 `hyprial user proxy status --json`。
   - `deliveryAgent` 不为空：在 setup 上加 `--name <deliveryAgent 的值>`，沿用这个 proxy。
   - 有 `userProxies`（本机不止一个）：选定要保留的那个，用 `--name <它的名字>`。
   - `configured` 为 `false` 且没有 `userProxies`：不加 `--name` 即可。
   不加 `--name` 而本机已有另一个名字的 proxy 时，setup 会拒绝并在错误信息里给出该名字，
   照提示加上 `--name` 重跑即可。
8. 已停止的旧 proxy（`hyprial agent stop` 过）不在 desired state 里，`status` 看不到它，
   setup 也不会因它拒绝。它 cell 里的 adapter 成员在停止后仍在：用同一个 adapter 重跑 setup 会以
   `PIN_CONFLICT` 拒绝，并提示先从那个 cell 去掉该 adapter 成员。这时要沿用旧 proxy：该错误信息
   给出当前绑定该 adapter 的 agent URI，在 `hyprial agent list --json` 里找到 URI 相同的那一行，
   以 `--name <该行的 agent 名>` 重跑 setup；要换成新默认名的 proxy，才先用
   `hyprial agent cell apply` 从旧 proxy 的 cell 去掉该成员。
   手工去掉该成员或换了 adapter 后再跑 setup，会按新默认名另建一个 proxy，旧 agent 只留登记、不运行。

## 3. 不变的部分

在 S2c 版本中，`hyprial user proxy setup|status`、其独立 worker 与 desired-state 中的 `user-proxy` harness
保留，除第 1 节所列的前置检查、默认名与拒绝规则外行为不变。它们已在 I7b 删除，见
[user-proxy 已删除](migration-im-push-by-agent-binding.md#14-user-proxy-已删除i7b)。

---

# 迁移说明：去掉 GUI 首启 Squire 与 name-frozen 死引用（S2d）

适用：从包含 GUI 首启 Squire 准备页、本地 `hyprial.gui.squireAgentId` 记录或
`squireState=name-frozen` 分支的版本升级到本版本。

## 1. 删除的已发布面（GUI 内部）

- `FirstRunStepId='squire'`、`SquirePreparationState` / `SquirePreparationProgress` /
  `SquirePreparationResult`、`SquireSetupState`。
- `FirstRunActions.prepareSquire` / `cancelPreparation`、`SetupClient.prepareSquire` /
  `cancel` 与 `SetupProgress`。
- `squireState='name-frozen'`（`SQUIRE_NAME_FROZEN_MESSAGE` / `AGENT_NAME_FROZEN` 在 GUI
  首启中的死分支）、`first-run-progress`、开发场景 `squire-failure`。
- 浏览器 `localStorage` 键 `hyprial.gui.squireAgentId*` 读写与 `SquireIdentityStore` /
  `createStorageSquireIdentityStore` / `findSquireAgent`。

说明：S2d **不删除** 全局 `AGENT_NAME_FROZEN` proto code，也不改写普通 Agent（包括已创建的
普通 `squire` 实例）的名字、聊天或组织显示。

## 2. 手工步骤

1. 旧浏览器中残留的 `hyprial.gui.squireAgentId` 与 `hyprial.gui.squireAgentId:<principal>`
   键已不再被 GUI 读取或写入，仅作为历史记录；由 agent 或用户在浏览器站点存储中核对后手工移除。
2. 已有普通 Agent、模型服务配置及已保存的 API Key / Auth 凭据无需删除、保持不变。
3. 需要首个 Agent 时，在首启「运行配置」步骤或 Agents 页显式输入名称创建；GUI 不会再自动创建固定
   Squire 或自动接管历史实例。

# 迁移说明：去掉 squire 协议标签、冻结码与 owner-DM 死链（S3b）

适用：从仍带 `squire.user.*` 唤醒路径、`TARGET_SQUIRE_*` 错误码或 Lark owner-DM 端口的版本
升级到本版本。本片只删死代码与历史协议残留；不自动改写、重发或删除任何本地状态，没有兼容读取。

## 1. 删除的已发布面

- 诊断日志 `delivery.wake.would_suppress` / `delivery.wake.observe_failed` 的 `path` 字段前缀由
  `squire.user.<site>` 改为 `user.<site>`（如 `user.workflow`、`user.send`、`user.proxy`）。
- 线路错误码 `TARGET_SQUIRE_UNCONFIGURED`、`TARGET_SQUIRE_ADAPTER_UNAVAILABLE` 从
  `hyprial.error.codes`、`ipc_errors` 与冻结注册表中删除（裁决 §12.1.4）。新 daemon 不产生、
  不识别这两个码：0.5.5.3.3 及更早接收端回来的这两个码按普通未知失败处理；其中
  `TARGET_SQUIRE_UNCONFIGURED` 不再让 routine 告警终止，而是与其他未知失败一样继续重试。
- Lark owner 私信端口：`SendOwnerDm`、`send_owner_dm` / `send_owner_dm_settled`（SDK、出站
  gateway、入站 adapter 与 `LarkApiPort` 协议），以及 daemon 内部的 `owner_notice_sender`。
  普通 `send_chat` / `reply` / 文件 / 图片发送不变。

## 2. 手工步骤

1. 执行者：agent。把依赖 `squire.user.` 的日志查询、告警规则或看板过滤改为 `user.`；历史日志不改写，
   无需备份。核对：在 daemon 日志中查找 `"path": "squire.user.`，升级后新写入的行不应再出现；
   需要恢复时把查询改回旧前缀即可（只影响查询，不影响状态）。
2. owner 私信端口在删除前已无任何调用方，没有依赖它的已发布行为，无需操作。
3. 执行者：agent。仍有 0.5.5.3.3 或更早节点参与投递时，先升级这些节点；否则它们回来的
   `TARGET_SQUIRE_*` 拒绝会被当作可重试失败，routine 告警会一直重试到对方升级或人工处理。
   核对：各节点 `hyprial --version` 均不早于本版本。

---

# 迁移说明：删除 receivers、owner 线程别名与 squire-container（S3a）

适用：从仍带 `org.receivers.get` / `org.receivers.set`、OrgFS `receivers.json` 受保护分区、
`users.json` 的 `squireChannel` / `threadBindings` 或 agents 库 `hostedBy=squire-container` 的版本
升级到本版本。本片没有迁移命令、没有兼容读取，也不自动改写或删除任何本地状态；下面的事由 agent
或用户手工完成。

## 1. 删除的已发布面

- daemon 请求 `org.receivers.get`、`org.receivers.set` 及其全部参数与结果字段（`source`、
  `reason`、`receivers`、`preferred`、`squire`、`userProxy`、`updatedBy`、`updatedAt`、
  `clearSquire`、`clearUserProxy`、`expectedVersion`）。调用它们现在得到 `METHOD_NOT_FOUND`。
  OpenAPI、GUI 生成 client 与 contract mock 同步收缩（HTTP 暴露方法 131 → 130）。
- OrgFS `receivers.json`：`receivers` 受保护分区、它的确定性文档 id、整值 register 文档格式、
  写入时的 receiver 记录校验与 `TextReadSnapshot` 的 `content_conflicts` / `content_confirmed`
  均删除。该路径现在是普通文档路径，daemon 不读取它。旧版本写下的 `receivers.json`（带旧的受保护
  文档 id）仍可随所在 space 正常打开和同步，但只读：本节点在任何 space 里都拒绝写入、替换、删除或
  移动该文档及其目录行；对端发来的这类更新在 org 目录 space（`group-*`）里被拒绝，在遗留的非 org
  space（如 OC4 前的 `personal-*`）里，旧版本写下的历史照常同步，不会被拒，以免新副本卡住同一写者
  之后的提交。旧 register 格式的文档只显示其纯文本部分（通常为空），不会被自动改写或删除；需要旧的
  `preferred` / `squire` / `userProxy` 值时，从升级前的 `HYPRIAL_HOME` 备份里取。
- `state/users.json` 的 `squireChannel` 与 `threadBindings`：不再读取；之后任何一次档案写入
  （登录时的 ensure、关联 agent、delivery agent、运行能力探测）都不再写出这两个键。daemon 启动
  不再做 `squireChannel` → `threadBindings` 的迁移改写。`deliveryAgent`、`preferredReceiver` 等
  其余档案字段不变（随 S3c 处理）。`hyprial agent tiers probe --json` 回执中的 `profile` 对象随之
  不再包含这两个键。
- 会话标识：`user:<owner>` 与 agent 之间的会话不再按档案中的 owner 线程绑定改成该 owner 的
  adapter 线程标识。以前被这样改写的会话，新消息落在纯 `user:<owner>` 会话下（会话 id 改变），
  已记录的历史留在旧 id 下，不搬移。
- agents 库 `hostedBy` 取值 `squire-container`：Agent 实体与新建库的 CHECK 均不再接受。旧库中仍有
  该值的行会让注册表读取以 `invalid hosting authority: 'squire-container'` 失败（daemon 无法正常使用
  该库）。`host-invite` 与 `transfer-receive` 不变。
- org-context 成员字段 `squire`：不再是 schema 字段，不再校验、不再有含义；文档中已有的值原样保留。
- 内部：owner 转发兜底（`owner_requester_addresses`、`_owner_alert_notifier`、
  `_notify_owner_of_diverted`）、跨 owner 投递对 receivers 记录的读取（只剩档案的
  `preferredReceiver`，随 S3c 替换）、`org-membership-v1` 盘点中用户行的 receivers 投影与
  `RECEIVERS_WRITTEN` 检查点。

## 2. 手工步骤

1. 执行者：agent。停止 daemon，整体备份 `HYPRIAL_HOME`（例如
   `tar -czf hyprial-home-before-s3a.tgz -C "$(dirname "$HYPRIAL_HOME")" "$(basename "$HYPRIAL_HOME")"`）。
   以下任一步出错时，停止 daemon 并用这份备份整体恢复。
2. 执行者：agent。在启动新版本之前盘点 agents 库：
   `sqlite3 "$HYPRIAL_HOME/state/agents.sqlite3" "SELECT actor, uri FROM agents WHERE hosted_by = 'squire-container';"`。
   没有输出就跳过本步；报 `no such column: hosted_by` 的旧库不可能有这类行，同样跳过。有输出时，和用户确认每一行是否保留：
   - 保留：执行 `sqlite3 "$HYPRIAL_HOME/state/agents.sqlite3" "UPDATE agents SET hosted_by = NULL WHERE hosted_by = 'squire-container';"`，
     这些行成为普通（非代持）agent 记录；该修复由测试固定，其他行不受影响。
   - 不保留：先执行上面的修复让库可读，启动后再用 `hyprial agent destroy <actor>` 删除。
   修复必须在新版本首次启动前做：`hosted_by` 约束里还没有 `host-invite` 的旧库（或
   `entity_token` 可为空的更旧的库）会在打开时重建表，重建以 `CHECK constraint failed` 拒绝这类行并
   回滚，库文件不变；做完上面的修复再启动即可。核对：再次执行盘点查询应无输出，`hyprial agent list --json` 正常返回。
3. 执行者：agent。盘点 `users.json` 中不再生效的键：
   `jq '.users[] | {ownerKey, squireChannel, threadBindings}' "$HYPRIAL_HOME/state/users.json"`。
   这些值不再决定任何路由。需要让发给 owner 的消息到达某个 IM 会话时，按 I7a 的方式给本机
   user agent cell 加 adapter 成员（`hyprial agent cell show hyprial-user`，修改后
   `hyprial agent cell apply`）；不要靠编辑 `users.json` 恢复旧路由。两个键可留着（下一次档案写入
   会去掉它们），也可在确认无用后手工删除；不同 owner 之间不要猜测映射。
4. 执行者：agent。依赖旧 owner 线程会话 id 的脚本或看板：用 `hyprial message conversation list --json`
   找到对应的 `user:<owner>` 会话的新 id 并改用它；旧 id 下的历史保留，不会合并。
5. 执行者：agent。调用过 `org.receivers.get` / `org.receivers.set` 的脚本：删掉这些调用；没有替代
   方法。个人 org 空间已在 OC4 移除（见 [migration-org-no-personal.md](migration-org-no-personal.md)）；
   仍留在旧副本中的 `receivers.json` 不再被读取，留着无害，按 OC4 说明处理旧空间即可，代码不会删除它。
6. 执行者：agent。org-context 文档里成员的 `squire` 字段不再有意义，可保留，也可在下一次编辑
   org-context 时手工删掉。
7. 执行者：agent。本版本的盘点不再含 receivers 投影。升级前已完成 `org-membership-v1` 的节点
   不受影响：比对旧 journal 时去掉已退役的 `receivers` 键，该步骤重跑后仍是 `done`，无需操作。
   只在 `hyprial daemon upgrade status` 显示该步骤升级前未完成、且
   `$HYPRIAL_HOME/state/org-membership-v1/journal.json` 存在时适用：旧 journal 的源摘要不再匹配，
   运行会报告 `source-drift`。先备份该 journal，再把它移出目录（例如改名为
   `journal.json.before-s3a`），然后运行 `hyprial doctor --fix` 重新执行该步骤；本版本的该步骤
   只核对身份与空间状态并记检查点，不写 OrgFS 成员记录。核对：`hyprial daemon upgrade status`
   中该步骤不再是 `failed`。恢复：把备份的 journal 放回原处。

# 迁移说明：删除 users 档案，运行能力改存独立缓存（S3c）

适用：从仍带 `state/users.json` owner 档案（`UserProfileStore` / `UserProfileAuthority`）的版本升级到
本版本。本片没有迁移命令、没有兼容读取，也不自动改写、转换或删除任何本地状态；下面的事由 agent
或用户手工完成。

## 1. 删除的已发布面

- `state/users.json` version-1 档案整体：`owner`、`ownerKey`、`loginName`、`agents`、
  `deliveryAgent`、`preferredReceiver`、`notificationRules`、`runtimeCapabilities` 都不再读取或写入。
  daemon 不再启动、刷新（原 30 秒一次）或关闭档案 actor；持久配置加载（Lark worker 等）不再解析
  `users.json`，该文件损坏也不会再阻断 `channels.json` 的加载。
- Python 门面：`hyprial.identity` 的 `UserProfile`、`UserProfileStore`、`UserProfileError`、
  `PreferredReceiver`、`NotificationRule`；`hyprial.daemon` 的 `UserProfileStore` / `UserProfileError`
  再导出与 `active_profile`、`probe_profile`；kernel 的 `parse_channel_uri`（最后的 `channel:` 读者）。
  新增 `hyprial.identity.RuntimeCapabilityStore` / `RuntimeCapabilityError` 与
  `hyprial.daemon.recorded_capabilities` / `probe_capabilities`；`RuntimeCapability` 的 JSON 形状不变。
- `hyprial agent tiers probe --json` 回执：删除 `profile` 对象（含 `profile.deliveryAgentLive`）；
  `probe` 从 `squire-runtime-capability` 改为 `user-runtime-capability`；新增 `capabilities`（本 owner
  记录在缓存中的全部组合）；`changed` 的值从 `users.runtimeCapabilities` 改为 `runtimeCapabilities`。
  不再要求先有档案：缺省 `--owner-key` 为本机 owner，不再返回 `NOT_FOUND`（"no user profile"）。
  `--combo`、`--dry-run`、`--timeout` 不变。
- 运行能力（显式 probe 与 provider-auth 故障/恢复标记）改存 `state/runtime-capabilities.json`，
  形状为 `{"version": 1, "owners": [{"ownerKey": …, "capabilities": [...]}]}`，每个 ownerKey 一项。
  daemon（provider-auth 标记）与 `hyprial agent tiers` 只读写本机 owner 那一项；`agent tiers probe
  --owner-key <值>` 会把结果记在给定键下，但除本机 owner 外没有任何读者，日常不要传它。缓存损坏时
  读取与写入都明确失败（`RuntimeCapabilityError`），不当作空集合。provider-auth 不再因为没有档案而跳过标记。
- PAC 短名解析不再把 `users.json` 的 owner 当作用户候选（候选来源 `user-profiles` 删除）；完整
  principal URI、agents 注册表与图内候选不变，短名不再按档案猜人。PAC schema-8 历史迁移同样不再以
  档案 owner 作为改写来源（已迁移的库不受影响）。
- 跨 owner `user:` 投递的受理机：不再按档案 `preferredReceiver.machine` 决定。现在只有满足两条的机器
  受理：请求的 owner 与本机 owner 完全相同（不再按显示名回退），且本机 user agent cell 至少有一个
  adapter 推送目标。其余机器不应答，发送方等待回执超时得到 `USER_DELIVERY_TIMEOUT`（与此前没有档案
  的 home 相同）。受理机不另行选择：有 adapter 成员的机器都会受理。同一 owner 多台机器时，这与 I7a
  的本机推送用的是同一个 adapter 成员，只能二选一，见第 2 节第 4 步。本机 owner 相同但 user agent cell
  读不出来时，该机同样不应答，并在 daemon 日志记一行 `user-delivery.not-accepted`
  （`reason: "cell-unavailable"`）。
- `org-membership-v1` 盘点不再读取档案，也不再产生 `profile-agent-unresolved` 问题项。

## 2. 手工步骤

1. 执行者：agent。停止 daemon，整体备份 `HYPRIAL_HOME`（例如
   `tar -czf hyprial-home-before-s3c.tgz -C "$(dirname "$HYPRIAL_HOME")" "$(basename "$HYPRIAL_HOME")"`）。
   以下任一步出错时，停止 daemon 并用这份备份整体恢复。
2. 执行者：agent。盘点旧档案中还想保留的运行能力：
   `jq '.users[] | {ownerKey, runtimeCapabilities}' "$HYPRIAL_HOME/state/users.json"`。
   旧记录不转换。升级后对需要保留的组合重新观测一次：
   `hyprial agent tiers probe --combo harness:provider:model --json`（可重复 `--combo`）。
   核对：回执 `probe` 为 `user-runtime-capability`，`capabilities` 含这些组合；`hyprial agent tiers --json`
   的 `availability` 显示新结果。
3. 执行者：agent。旧档案里的 provider-auth 不可派标记（`reason` 为 `provider-auth-invalid` /
   `provider-auth-account`）不再生效。若怀疑某 provider 凭据仍有问题，按运维指南做一次真实核对
   （本机 `pi` 中 `/login <provider>`，或对相关组合运行一次上面的 probe）；新标记会写入新缓存。
4. 执行者：用户决定，agent 执行。同一 owner 有多台机器时，先在每台机器上看 user agent cell
   （`hyprial agent cell show hyprial-user --json`）哪些有 `role: "adapter"` 成员。这个成员同时承担两件事：
   受理别的 owner 发来的 `user:` 消息（本片），以及把本机上未绑定发送方（operator，以及自己 cell 没有
   adapter 成员的 agent）发给 owner 的消息推送到 IM（I7a，见
   [migration-im-push-by-agent-binding.md](migration-im-push-by-agent-binding.md)）。两者不能分开配置，
   只能在下面二者中选一个代价：
   - 多台都保留 adapter 成员：每台机器的本机推送都正常，但别的 owner 发来的每条 `user:` 消息会被每台
     机器各受理一次，IM 里收到重复推送（各机有各自的受理记录，不会去重）。识别方法：同一 `messageId`
     在两台受理机的 daemon 日志里各有一行 `user-delivery.proxy` 且 `accepted` 为 `true`（发送方只记
     一行 `user-delivery.settled`，取先到的回执，看不出重复）。
   - 只在一台保留：跨 owner 消息只推送一次；但其余机器上未绑定发送方发给 owner 的消息不再有 IM 副本
     （入队的发送回执 `pushed: false`、`pushCode: "TARGET_UNRESOLVED"`，只推送不入队的发送以
     `TARGET_UNRESOLVED` 失败；daemon 日志 `send.user_push` 带 `pushSkipped: "no-user-agent-target"`）。
     入队的消息仍在 durable inbox 里；只推送不入队的发送没有任何副本。自己 cell 里有 adapter 成员的 agent 不受影响。
   选定后，在要去掉成员的机器上从 cell 删掉 adapter 成员并 `hyprial agent cell apply`。
   核对：保留的机器 cell 中有 `role: "adapter"` 成员，去掉的机器没有。两种代价都不想要时，需要新增
   "每个 owner 指定受理机"的运行时数据，本版本没有。
5. 执行者：agent。读取 `profile` / `deliveryAgentLive` 字段或 `squire-runtime-capability` 标识的脚本：
   改读 `capabilities` 与 `user-runtime-capability`。导入 `UserProfileStore` 等已删除 Python 名称的
   代码：改用 `RuntimeCapabilityStore`（只承载运行能力），其余档案语义没有替代。
6. 执行者：用户确认后由 agent 执行。`state/users.json` 与其锁文件 `state/.users.json.lock` 不再被读取，
   不影响已绑定 owner 的 home；确认上面各步不再需要其内容后可手工删除。不要靠编辑它恢复任何路由或能力。
   例外：在还没有账号绑定记录的 home 上，登录前的旧数据盘点仍只看该文件是否存在（不读内容），只剩它时
   登录也会以 `HOME_ACCOUNT_CONFLICT`（`home-binding-missing`）拒绝。要在这样的 home 上登录，先备份再删掉
   （或移出）它，或按 [migration-account-binding.md](migration-account-binding.md) 处理。
