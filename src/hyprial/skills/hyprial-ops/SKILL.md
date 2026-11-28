---
name: hyprial-ops
description: hyprial(Harness Bridge)完整能力面索引与运维操作:adapter 生命周期(创建App/add/pin/路由)、daemon 诊断与恢复、PAC workflow 派单、agent/worker 管理、消息排障。凡是要对 hyprial 做任何操作、或要回答"hyprial 能不能做 X"时使用——先查本索引和 --help,再下结论。
---

# hyprial-ops:能力面索引与运维

## 第零条纪律(本 skill 存在的原因)

**凭记忆断言"hyprial 没有这个能力"之前,必须先枚举命令面**:

```sh
hyprial help [--json]          # Typer 注册树生成的一级清单(含隐藏一级入口)
hyprial --help                 # 面向人的可见顶层域
hyprial <域> --help            # 该域全部可见子命令
hyprial <域> <子命令> --help   # 参数面
```

`hyprial help` 不维护第二份手写快照；命令组注册进 Typer 后会自动进入清单。

实案(2026-08-26):只查了 `adapter add`(要现成凭据)就断言"创建飞书 App 只能
人工在后台做";实际 `adapter add` 默认流程就能自动创建 App。
**--help 是权威,记忆和本文件都只是索引。**

## 命令组迁移（0.5.5.2）

使用 `org service`、`org fs`；
`host` 保持顶层。旧顶层路径不是别名：退出 2，stdout 为空（含 `--json`），
stderr 只输出 `moved: hyprial <旧参数>  →  hyprial <新参数>` 一行。
裸命令组显示帮助并退出 2；例外为 `init`（初始化）、`daemon upgrade`、
`agent tiers`、`routine templates`（默认报告）。

## 能力面地图(按域)

### daemon 生命周期与诊断
- `hyprial init` — 创建 home 并启动 daemon(已含平滑重启逻辑),**不登录**:没有账号的 home 上只启动
  bootstrap daemon 并提示 `hyprial login`;`hyprial login` — 首次登录由 daemon 一次性完成(显示验证 URL 与
  用户码、`--no-open` 不开浏览器、设备登记失败仍退出 0 且 `device.ready:false`);`hyprial doctor` — 健康检查
- 输出约定(所有命令):`--json` 时 stdout 恰好一个 JSON 对象且必带 `ok`;`ok:false` 退出码为 1
  (`doctor` 有 fail 项、`adapter scopes check` 缺必需 scope 时也是),参数错误退出 2;进度、提示、确认
  只在 stderr,不要从 stdout 截取设备码或链接。
- `hyprial daemon status [--json]` — daemon 运行状态（原一级 `ps` 的 daemon 段；`ps` 已随 R7 退役）
- `hyprial daemon service install|uninstall|status` — 管理当前用户的 launchd/systemd daemon 服务。
  install/uninstall 需交互确认或 `--yes`；Windows 返回 `SERVICE_PLATFORM_UNSUPPORTED`。
  wheel/`uv tool install` 没有 post-install hook，首次安装用 `hyprial init --service`，或 init 后
  显式 install。已接管的 `daemon restart`/upgrade 只经 supervisor 重启；socket pid 必须等于
  supervisor 报告的新 job pid。`restart.supervision` 为 `supervised|unsupervised|lost`，`lost`
  是失败。plist/unit 及其目录必须属于当前 uid，且不能由 group/world 写入。label 已加载但文件
  缺失时不得回退为非托管重启；先按 doctor 提示人工卸载旧 label。`doctor --fix` 可修复可归属的
  游离 daemon 和模板漂移并保留旧文件备份；外国 plist/unit 一律不改。
- `hyprial agent list [--json]` — agent/connector 运行状态（原 `ps` 的 connector/session 段）
  Cell diagnostics: `agents[].runState` is the additive local run observation;
  select the canonical `uri`, not a short name. `hyprial agent top [--wide] [--json]`
  shows the same projection under `actors[].runState` (canonical `actor`). Human
  output includes reason, measured turn age, base state, certainty, scope, health
  and permission capability. Top's `LEGACY` column is the prior log/contact
  heuristic, not run state; JSON `state/stateDetail` retain their prior meaning.
  Missing runState means `unknown (unsupported)`, never idle. Failed agent queries
  return an error, not an empty fleet. `daemon status` is still daemon-only, not
  an agent-status substitute; do not restore the retired root `ps` command.
- cell 运行时排障（cell authority 为 `user:u.<sub>`）：无头消息一直 pending、`agent.attach` 的
  `cell.state` 为 `deferred` 时，先检查 `<HYPRIAL_HOME>/settings.json` 是否含 `userId`（缺则运行 `hyprial login`）并查 daemon 日志（`state/logs/daemon.jsonl` 或 `hyprial log`）里
  `cell.runtime_unavailable` 的 `reason`（`keys` 为不符的键名）：`authority-uid-missing`＝没有 uid（用 `HYPRIAL_OWNER`
  部署的补设 `HYPRIAL_USER_ID`，登录态 home 缺 `userId` 的按登录线文档重新登录，再
  `hyprial daemon restart`；uid 每个 serving 代只解析一次）；`cache-identity-mismatch`＝
  `state/cells.sqlite3` 属于旧版本或另一身份；`cache-unreadable`＝读不出（只有 `-wal/-shm`、
  文件损坏，或 daemon 正在 checkpoint，可重试）。日志 `cell.document_blocked`＝OrgFS 上该路径
  有本机不采纳的文档（旧写法、他人改写、另一身份的副本），本机在该 space 的写出被阻塞，其他 space
  照常。代码从不删除、改写或迁移这些文件与文档，也没有修复命令：一律照
  `docs/guides/migration-cell-authority-uid.md` 人工处理（停机后离线备份并移走 cache 三文件、`hyprial fs rm` 移走
  本人的旧文档），文档移走后下一个 pass 自动写出并记 `cell.document_unblocked`。
  `carrier-deliveries` is not whole-terminal coverage. `permission=unsupported`
  means `coverage.permissionWait=false`, not "no permission wait". `turn-age=-`
  is unknown, not zero. `suspected-stuck` is a warning, never proof of deadlock;
  it does not kill/restart a process or approve/deny a request. Provider blocks
  remain separate from permission waits, including on a stopped agent.
  No remote run-state or Lark notifications are added. `scope=carrier-deliveries`
  means the producer did not negotiate whole-session coverage: the row is
  unknown/partial, not idle. Claude Channel and Pi attach negotiate coverage on
  registration and re-publish their current open run when they recover the
  daemon generation, so a disconnect, a lost end pulse or a daemon restart is
  replaced by the real current state without extra inbox traffic, and an
  accepted refresh also acknowledges a contact gap the session owner recorded
  (a heartbeat alone cannot). A failed refresh is retried by the ordinary poll
  cadence; a daemon that does not know the method is cached for that generation
  instead of being retried. A refresh is observation only: no turn event, no
  fetch/ack/reply, no lease renewal.
  Codex whole-TUI and permission evidence require separate E2/E3 acceptance;
  CLI goldens are not native acceptance. See
  `contract/daemon-lifecycle/run-state.md` and
  `contract/daemon-lifecycle/interactive-turn-refresh.md`.
  UUID addressing: 人类视图每行以 `display(a.<id>)` 开头（显示名，否则 actor 名，括号里是
  不可变的 agent id；无 id 的旧记录只显示名字）；`--json` 行新增 `agentId` 与 `displayName`
  （null 表示显示名就是 actor 名），其余键不变。地址取 `uri` 字段，不要手拼
  `agent:<owner>:<machine>:<actor>`。id-form 开关打开后，官方地址是
  `agent:<owner>:<machine>:a.<id>`；开关打开前 `uri` 仍是名字形态。见
  `contract/identity/agent-naming.md`。
- `hyprial agent rename <name|a.id|uri> <new-display-name> [--json]` — 只改显示名；actor、
  id、地址、home、grants、pins 都不变，改回 actor 名会清空 `displayName`。目前任何能调用
  daemon 的人都可以改名（裁定 U2）。错误码：`AGENT_NOT_FOUND`（目标不存在）、
  `AGENT_EXISTS`（名字被在用 agent 占用）、`INVALID_AGENT_NAME`（不是合法的裸名字）；
  已销毁 agent 的名字可以再用，新建的是一个新 agent（新 id，不继承旧 agent 的任何东西）；`AGENT_ERROR` 的消息以底层码
  开头，`DOMAIN_COMMAND_TIMEOUT` 表示结果未知，先 `hyprial agent list` 复查再重试。
- `hyprial log --name <worker> / --actor / --since / --conversation / --correlation-id`
  — daemon 结构化日志查询;原始日志 `~/.hyprial/state/logs/daemon.jsonl`
- `hyprial daemon upgrade` — 普通升级的持久源固定为公网
  `https://github.com/Hyprial/hyprial.git`，与当前安装回执里的来源无关；升级成功后
  自动平滑重启（操作员显式动作，不受 autoUpgrade 开关约束）。每次安装前从同一
  来源抓取解析到的确切 commit，要求其中存在 `uv.lock`，导出 pins 后以
  `uv tool install --constraints` 安装；抓取、锁文件或导出失败都会响亮终止，绝不
  回退到未锁定安装。开发者一次性验证内网 ref 用
  `hyprial daemon upgrade --source forgejo --ref <完整提交或标签>`；该选择不持久化，下一次
  普通升级和自动升级仍回公网。若已装的早期测试版本高于公网版本，普通/自动升级
  都返回 `declinedDowngrade` 并跳过；确需回退时由操作员显式指定公网 `--tag`。
  重启成功只由新 daemon 的 ping 事实裁定：结果中的 `restart.before/after` 都带
  `{pid, epoch, version}`，PID 与 epoch 必须同时变化，after.version 必须等于已安装版本。
  直接启动或 supervisor 托管的替代进程都使用同一 120s 硬上限，不把 30s 无阶段进展
  误报为失败；只有替代进程退出、硬上限耗尽或旧 pid/epoch/version 仍在回答才发失败告警。
- `hyprial version --json` — 除版本外分别报告 `persistentUpdateSource`（固定公网）、
  `installOrigin`（PEP 610 当前安装来源）和 `dependencyLock`（`match`、带逐包差异的
  `mismatch`，或旧安装/回执不匹配时的 `unknown`）。
- `hyprial config set autoUpgrade true|false` — 自动升级总开关（2026-09-15
  Allen 定案：**缺省 = 关**）。未开启时 daemon 调度器到点只记
  `autoupdate.run.skipped`(reason=disabled)不起子进程；隐藏的
  `daemon upgrade run` 只供 daemon 调度器使用，daemon 不在线时直接跳过，不能
  用它做本地升级。旧 launchd/systemd 单元由 `daemon upgrade` 或
  `doctor --fix` 的升级步骤移除，daemon 启动只报告 pending。`hyprial daemon upgrade status --json` 的 `autoUpgradeEnabled` 读出开/关。用户或 agent 要开自动升级就走这条命令，
  不要手编 settings.json。
- 楔死排障(doctor 超时但进程活着):看 daemon.jsonl 的 `reconcile_overrun`
- `hyprial org service expose <port> --target unix:/绝对路径|tcp:127.0.0.1:<port> [--proxy-protocol v2|none]`
  —— 通过 hyprial-tailcat 边车（Tailcat，随 wheel 分发）把本机服务暴露给组织内的设备；默认 PROXY v2，
  首帧带来源地址和对端设备的 nodekey（TLV 0xE0），服务端再经边车的有效成员表把 nodekey 映射为用户，不读组织目录做准入。
  v2 只允许 unix target（本机任何进程都能连 tcp 回环端口并伪造头）；查不到来访者身份时直接断开，
  不会转给后端。tcp target 必须显式 `--proxy-protocol none`，此时不带任何身份。
  `org service unexpose <port>` 删除；`org service exposures [--json]` 查看 daemon 持久 desired set；
  `org service whois <ip:port> [--json]` 只查对端 nodekey（R7 由 `network peer-key` 改名）。unix target 的父目录必须属于当前用户且
  权限恰为 0700。不要绕过 daemon 直接写 sidecar stdin。
- 组织网络：`hyprial org create <org>` 建组织。客户端超时后用同名命令重试。daemon 会按创建者专属 intent 恢复，不会重复建 space。
  只有显式 `creating` / `partial` 状态表示创建未完成。生命周期标记缺失、缺少 state 或 JSON 损坏时按 ACL 绑定回退：有绑定为 `legacy-active`，无绑定为 `partial`。JSON 损坏、未知状态及无效/内容未到达的设备行会出现在 `org network --json` 的 `dataProblems` 与 doctor 警告中；设备行问题不阻断同一空间的其它有效设备。
  设备发布失败时，create 输出 `published:false`、`ok:false` 并退出 1，可同名重试。`org network` 的不可用原因是 `creating`、`partial` 或 `policy-denied`。
  `hyprial org repair [org]` 默认只读列出不完整创建。显式 `--apply` 才恢复当前 owner 的候选。不得用生产运行做测试。`hyprial org invite <org> --user <u>` 生成邀请链接
  （https 主形式，另附可复制的 `hyprial org join '<链接>'`；登录态下会附
  `https://hyprial.ai/j` 短链，`--json` 的 `short` 失败为 null；短链同样是凭据，
  私发勿贴群，短链失败只给长链；链接含连接凭据，只发给被邀请人）；
  对方用 `hyprial org join <链接|->` 加入，或在网页上接受后由设备执行 `hyprial org pending`
  拾取（`hyprial login` 成功后会自动拾取一次）。`org list`、`org network [<org>]` 查看；
  `org pending` 只读 Casdoor，不写 `consumedBy`；本机 `org.list` 的 `spaceId` 和
  `leftOrgs` 决定是否已处理。同一 space 不重复加入，主动退出的组织不被旧邀请拉回；
  要重新加入须显式 `org join`，成功后清除本机退出记录。不要为拾取配置 Casdoor 写权限。
  `org leave <org> --yes` / `org remove <org> <u> --yes` 显式确认退出或移除；
  不带 `--yes` 时交互确认，非交互调用拒绝。预览列出设备、tailnet、authority 和断连影响。
  确认绑定预览的 operationId 和原始 deviceIds，CLI 自动携带这两个字段；直接 IPC 不能只传 confirmed=true。
  `REMOVAL_CONFIRMATION_STALE` 表示确认缺失或预览已变，须重新预览；旧确认不会移除重新入网的设备。
  已完成操作的旧确认也被拒绝，不重复退出副作用。重新预览且没有新目标时，CLI 直接返回原回执。
  目录与 ACL 分别幂等移除，任何一步失败都不提交上层回执。
  先改 OrgFS，再撤销 tailnet；待确认/待 authority/网络失败均不等于已移除。
  `REVISION_CONFLICT` 后重跑同一命令：先看新 revision、仍待移除/已移除/新增但未选中的设备，
  再显式确认。操作 ID 和原始目标不变，不重做 OrgFS；新增设备不会自动纳入。
  已被其他操作移除的目标跳过；目标换了身份 incarnation 时拒绝，须独立新操作。
  未知结果/丢回包仍精确重放原请求，禁止改回执或删 journal 强制重做。
  Casdoor 自动发现只登记待办，不代替操作员确认。`org delete` 拒绝隐式删除最后 authority。
  设备 key 在 `hyprial login` 时生成；
  `hyprial doctor` 的 `tailcat-sidecar`、`device-key` 两项缺失时为 warn，存在但无效时为 fail。
- Tailnet 升级：daemon 与边车须同时升级到 stdio v6，维护窗口先升级固定 authority；
  不支持混合版本或自动选举。旧邀请返回 `INVITE_VERSION_UNSUPPORTED`，须重新签发 v2。
  成员表只由边车维护；Directory 是发布副本，不是准入来源。一次性离线迁移遇到
  `MIGRATION_AUTHORITY_MISSING` 时补齐明确的 owner 设备 pin，禁止任意选择设备（0.5.5.1.1）：
  先 `hyprial org migration-status --json` 看每个 org space 的 pin/回执/accepted-rejected-dropped/
  lastError 与 skipReason；再 `hyprial org pin-authority <spaceId> --device <deviceId> --yes`
  （离线可用，daemon 停或不停都行；只写 settings.json 的 pin；边车成员表已存在时拒绝
  `MEMBERSHIP_ALREADY_INITIALIZED`，此时用 `org repin`）；然后重启 daemon，authority 设备先起。
  成员节点同样要先 pin（authority 是本机事实，远端 owner 行哪怕只有一行也不自动选用）。
  0.5.5.1 留下的 `pending` 回执每次启动都按当前本地文件重算并改写（旧请求在
  `diagnostics.supersededParams`），该跳过的空间回执改为 `skipped`；只有 `complete` 回执是权威的。
  错误信息里列出候选 deviceId（不含地址/密钥）。可选 `forwarding.tailnet.authorityDefault:"self"`
  让本机在多设备 owner 且无 pin 时自选为 authority（默认关闭，不要在成员节点打开）。
  已删除的 org（`directory/org.json` state deleted）、已离开的 org、零设备行的探针 org 会被跳过，
  各记一条 `membership.migration.skipped {spaceId, org, reason}`，不再导致启动失败。
  重命名主机留下的同密钥旧行按 `updatedAt` 保留最新一条，丢弃项写进回执 `diagnostics.droppedDeviceIds`；
  损坏的 `updatedAt` 不再拒绝整行。回执 `lastError:{code,at}` 保存边车最后一次拒绝码。
  启动失败事件 `daemon.start.failed` 带 `code`、`attempt`、`backoffMs`；连续失败计数在
  `state/daemon-start-failures.json`（`hyprial doctor` 的 `daemon-start-failures` 行），受管（launchd/
  systemd）daemon 在第 2 次起失败后按 5s·2^(n-1)（上限 60s）退避再退出，SIGTERM 可打断；`daemon.ready` 清零。
  已完成迁移后丢失成员文件须人工恢复备份；不要删迁移回执来触发重新导入。
  authority 失联时有效 roster 租约最多 300 秒；到期关闭现有业务连接，只保留控制通道，恢复后刷新。
  重启普通成员不能续用磁盘中的旧租约。分别检查 sidecar/control/member/zenoh/orgfs readiness；
  恢复 authority 时 member 可以立即为 true，普通成员须重新获取租约。就绪字段不等于消息已送达。
  首次 create/join 探测空成员表使用不带 tailnetId 的 status；未知 tailnet 的定向 status
  返回 `MEMBERSHIP_NOT_INITIALIZED`，不要把任意成员表错误当作空表。
  `forwarding.tailnet.trustMembersForUpperLayers` 默认 true，仅作用于边车管理的私有 Unix
  transport；它同时绕过 OrgFS supplier/requester 的目录成员检查，不绕过文档校验或管理权限。
  0.5.5.1 未实现 Zenoh ACL，共用 session 不保证跨组织完全隔离；不要宣称设置 false 已补齐 ACL。
  re-pin 使用 `hyprial org repin <tailnetId> --authority <deviceId> [--control-port 7448] --yes`。
  先停止所有节点的写入与 daemon；命令持有本地 daemon ownership fence，运行中拒绝
  `NETWORK_NOT_QUIESCENT`。这里“离线”只指各节点业务停用，不是零网络：边车仍开 control/peer
  listener 并 refresh/announce；本地 fence 不是全网写锁。预览列出旧/新 authority 和受影响设备；
  不带 `--yes` 须交互确认。
  命令由边车保存变更前备份，然后更新 pin；不删除旧 authority 的成员身份。结束必须重启，
  先启新 authority，再启其它节点。命令不复制跨机表；转移前须把完整 authority 表及操作回执
  通过备份恢复到新节点，不能用普通成员的部分状态替代，也不能手改 JSON 重建。
  lost response/中断后以相同 tailnet/device/port 重跑；待决操作不能换目标，禁止删回执强行重做。
  这不是在线转移、自动选举或自动回滚。维护协议与验收见仓库
  `docs/design/tailcat-cycle-fix-contract-2026-10-27.md`。
- `hyprial config set workerProxy.url <url|"">` / `workerProxy.vendors openai,anthropic` / `workerProxy.noProxy <list>`
  —— hyprial 自己的 worker 代理(Allen 2026-09-25 定):列出的模型厂商(默认 openai、anthropic)的 worker 走代理,
  其它厂商(deepseek、智谱、kimi)的 worker **去掉**代理直连;下一个 worker 启动即生效,不用重启 daemon;
  url 置空即清除。没配置时沿用 daemon 自己环境里的代理(含 all_proxy)。⛔ 重启生产 daemon 时不要剥掉代理变量。
- `hyprial config set dispatch.reminder "<text>"` —— `workflow plan` / `workflow run` 附带的派单 tier 提醒
  (Allen 2026-09-28 定:执行节点用 fast、规划设计节点用 strong;只是提醒,不改派单)。
  文本存在 `<HYPRIAL_HOME>/dispatch-policy.json`,文件缺失时用出厂默认;置空字符串即关闭。
  `hyprial agent tiers --json` 的 `reminders` 显示当前生效值;文件损坏时 plan/run 照常派单,
  只在 `reminderError` 里报原因,而 `agent tiers` 会直接报错,用这条 `config set` 覆盖写即可修复。

### 应用安装（已退役）
- 应用安装体系已于 2026-10-03 退役（Allen 裁决）：catalog、制品下载、manifest 命令挂载均已删除。
- `hyprial socialware install ...` 保留占位入口，接受并忽略全部参数与选项（`--yes/--check/--force`），
  统一返回错误码 `COMING_SOON`、消息 "hyprial socialware install is coming soon"、退出码 1；**不会静默成功**。
- 不要把退役的一级 `hyprial install <app>` 当作别名；安装能力会在未来版本重新加入。

### GUI 工作空间（源码模式）
- 旧独立 GUI（`src/gui`）与旧 Tauri 外壳（`app`）已退场；组件化 UI 以仓库根 `gui/` 为唯一主线。最小原生壳在 `clients/native/`，运行 `hyprial gui --no-open --json --port 0 --stdin-stop`，不启动或停止 daemon。
- 当前公开入口只有 `hyprial gui`（源码 checkout 先 `npm run build`，wheel 用内置 `_gui`；
  两者都由同一个 Python 会话 host 提供）和 `hyprial gui dev`（`npm run dev` 的 Vite 热更新，
  Python host 在公开端口前反向代理它；已无 Node host 与 `npm start`）。`start|status|stop|upgrade` 已退役并返回
  `INVALID_ARGUMENT`，没有兼容 shim，也不创建 `$HYPRIAL_HOME/apps/gui/`。
- `--ip`、`--port` 控制前端；默认 `127.0.0.1:4173`，端口冲突明确失败；`--port 0` 让 host 绑定空闲
  loopback 端口，启动记录的 `port`/`url` 是实际端口（dev 模式是 host 的端口，不是 Vite 的）。
  `--stdin-stop`（仅 POSIX）让 stdin EOF 或 `shutdown` 行像 SIGTERM 一样回收子进程但退出码为 0，
  不加则不读 stdin；`--json` 启动记录契约见 `contract/cli/gui-startup-record.v1.json`。普通 `hyprial gui`
  只有两个目标：wheel 内置已校验的 `_gui`，或所在的源码 checkout；传 `--gui-home`
  返回 `INVALID_ARGUMENT`，调用环境中设置 `HYPRIAL_GUI_HOME` 返回
  `GUI_CONFIG_CONFLICT`（该变量只是 launcher 交给子进程的内部变量）。
  `hyprial gui dev` 可用 `--gui-home` 或 `HYPRIAL_DEV_GUI_HOME` 指向另一个工作树的
  `gui/`；两者都未给出时，在源码 checkout 内直接使用其自身 `gui/`（只读，不创建工作树
  或分支、不 `npm ci`；缺 `node_modules` 时自行执行 `npm --prefix gui ci --ignore-scripts`），
  安装态无 checkout 时使用受管的精确 `sourceCommit` 工作树（发布 wheel 不含 GUI 源码：无显式源时 `gui dev` 以 `INVALID_ARGUMENT` 拒绝，请用 `--gui-home` 或源码 checkout）。启动输出中的
  `gui_home`、`git_worktree`、`git_branch` 是所选目录的真实值。
- `--with-dev-tools` 是开发工具层的唯一开关（没有环境变量可替代；`gui dev` 自带；wheel 与源码
  checkout 都接受）。不带时 `/dev-tools/*`、`/docs-assets/*` 为 404。带上后页面右下角出现“开发”浮动球
  （模块索引）；daemon 驱动且有 Swagger UI 文件时面板内有“API 文档”链接，在新标签页打开 host 的
  `/docs`，沿用页面会话，不需要也不显示 bearer（`request_http` 自带的 bearer 文档页已删除，它的 `/docs` 是 404，
  没有环境变量能再打开它）。启动记录的可选布尔键
  `dev_tools`、`api_docs` 说明本次是否开启。
- `--live-source` 是另一个独立开关：页面来自 GUI 源码（Vite 在 host 后面，改动即时生效），需要 Git、
  Node、npm；单独使用时没有工具层（`/docs`、`/docs-assets/*`、`/dev-tools/*` 为 404）。
  `hyprial gui dev` 等价于 `--with-dev-tools --live-source`，与任一参数叠加结果不变。`--gui-home` 与
  `HYPRIAL_DEV_GUI_HOME` 只在 live source 下生效，否则 `--gui-home` 返回 `INVALID_ARGUMENT`（提示加
  `--live-source`）。启动记录的可选布尔键 `live_source` 与 `hot_reload` 相同，不暴露 Vite 端口；
  `mode` 仅在两个开关都开启时为 `dev`。
- `gui dev` 服务就绪后默认只打开一次浏览器，`--no-open` 抑制；opener 抛错不另启服务。
  SIGTERM/SIGINT 在 discovery/受管工作树/npm 安装、阻塞 opener、启动 JSON 前后和运行中
  均按 143/130 退出，只回收本次已记录的进程组（Python host；dev 时的 Vite；contract-mock 时的 Contract Mock），不按端口清理。
- 业务请求经 `request_http` 到 daemon，由会话 host 持有浏览器会话；daemon 驱动下 host 在自己的进程内提供
  `request_http`，`hyprial gui` 只起这一个 Python 子进程（`--json` 的 `backend_pid` 为 `null`，`backend_endpoint` 为空串；
  contract-mock 才另有 Node 子进程）：安装态与源码态都是
  同一个 Python host，`HYPRIAL_GUI_BACKEND_DRIVER` 未设时默认 `daemon`（wheel 上
  `contract-mock` 返回 `GUI_MOCK_UNAVAILABLE`，源码态须显式选择它）；`hyprial gui` 在 home 的每种状态下都先确保 daemon
  （没起就拉起，无 owner 的 home 起 bootstrap daemon；stderr `gui-daemon-ensure`），关闭 GUI 不停 daemon，
  要停用 `hyprial daemon stop`；home 目录不存在返回 `GUI_HOME_NOT_INITIALIZED`，daemon 拉不起时返回启动原语的错误。
  登录、登出时 principal 在原 GUI 进程内换绑，GUI 地址与浏览器 origin 不变。`--no-open` 时一次性登录
  URL 写入 0600 的 `launch.url`（`hyprial gui --json` 的 `handoff_path`），不会打印到
  stdout。旧的 HTTP 501 / `not implemented: hyprial backend not wired` 占位已删除，
  `GUI_INSTALLED_BACKEND_UNSUPPORTED` 不再存在；不得把 fixture、mock、空白页或 HTTP 200
  当作真实登录/会话验收。
- 远程使用（SSH 隧道）：`hyprial gui --no-open --json` 的启动记录给出 `port` 与 `handoff_path`，
  0600 文件（`launch.url`）里是带一次性 `#grant=…` 片段的完整 URL。隧道本地端口必须等于远端端口，
  浏览器用 `127.0.0.1` 而不是 `localhost`（host 严格校验 Host/Origin）：
  `ssh -N -L <port>:127.0.0.1:<port> <host>`。grant 自 handoff 完成起 60 秒内有效且只能用一次，
  过期或使用后文件被删除，需重新启动；直接打开裸 origin 只会看到 SESSION_REQUIRED 页面
  （“这个页面不能直接打开”）。agent 代用户启动 GUI、而用户要从另一台机器打开时，必须在回复里
  立即、直接把 handoff 文件里的 URL 交给该用户（这是唯一入口），不得写入日志、工单、提交信息或共享频道：
  这 60 秒内它就是登录凭据。`scripts/ops/dev_upgrade_smoke.py apply --gui`
  （见 `scripts/ops/README.md`）把 `hyprial gui --no-open --json --stdin-stop` 作为脚本的子进程、为本次升级的目标 home
  启动 GUI，校验启动记录、监听端口与 daemon 身份，检查（及可选的 `--gui-hold-minutes`，此时登录凭据有效期等于保持时长、
  最多 10 分钟）结束时关闭其 stdin 停止 GUI；检查失败则回滚升级。停服务之前它先用已安装的 CLI 短暂启动一次
  `hyprial gui --port 0 --stdin-stop` 探测（不是只读的：会短暂起 GUI host 并写一次性 grant 文件），该 home 已有前台 GUI
  （`GUI_RUNTIME_BUSY`）时直接拒绝（exit 2，不停服务、不升级）；探测之后才出现的 GUI 仍由升级后的检查发现并回滚。
  不读取 handoff，由 owner 自己读取。
- Ctrl-C/正常退出只回收本次启动的 Python host、（dev 时的）Vite 与（contract-mock 时的）Contract Mock 进程组，不按名称或端口清理其他实例。
  Git worktree 只是工作树隔离，不是安全沙箱。
- 操作、参数、环境接线和当前 NOT_RUN 项见 `docs/guides/gui-dev-mode.md`。

### adapter(飞书网关)生命周期 —— 全部自动化,无需人工建应用

- 0.5.5.1 起，adapter 使用独立 desired-state 集合（schema 3），不再是 harness。
  旧状态中的同名 agent/adapter 会迁入各自集合；新建仍拒绝同名。pins 不迁移、不改名。
- **降级先恢复状态，再装旧版本**：先停止 daemon，用新版执行
  `hyprial adapter downgrade-state --json`，成功恢复 schema 2 后再安装、启动旧版。
  此命令持 daemon 独占锁并校验首份迁移前备份的 SHA256；备份不存在、摘要不符或
  迁移后 desired intent 已变更时拒绝。失败时保留新版、状态和备份，请运维核对；
  不手改 schemaVersion，不删除状态。恢复后不要先启动新版，否则会再次迁移。
  备份在状态 SQLite 的 `adapter_model_backup` 表中，重复迁移不覆盖；pins 和 inbox 不回退。
  新建 home 没有旧备份，不能用此命令降级。旧版直接读取 schema 3 会明确拒绝。
- `hyprial adapter add <name> [--new]` — **自动创建或收养飞书 App**
  并注册为 adapter；成功后自动尝试热加载到运行中的 daemon，结果见
  `daemonReload`。设备授权流,需 TTY:无人值守时用伪终端代跑以获取授权链接并发给人点批准。
  人只需点一次授权链接。
- `hyprial adapter add <name> --app-id ... --secret-file ...` — 只注册已有凭据的 App;
  也用于给已有 adapter 补路由(`--route <name>=<chat_id> --force`)
- `hyprial adapter scopes authorize <name>` / `adapter scopes check <name>` — 租户 scope 申请/盘点。
  已知缺失 scope 时,用可重复的 `hyprial adapter scopes authorize <name> --scope <scope>`
  只生成 `open.feishu.cn/page/scope-apply` 预选权限链接;它不调用飞书写接口,也不改
  App 已声明能力。请人开权限一律给这个预选页,不要给需要用户自己查找权限的开发者后台页。
  当前「对未声明 scope 有效」的实证样本数仅为 1,不可据此断言它总有效。
  `--scope` 与 `--interactive` / `--capability` 互斥;缺失 scope 无法可靠解析时不猜,
  保留命令给出的开发者后台兜底及原因说明。
  运行时配置为 Larksuite 且权限错误未带官方链接时,不生成 Feishu 预选页;
  输出 `authorizationUrlReason=larksuite_console_url_missing` 说明原因。
- `hyprial adapter reload` — **增量热加载**新/改 adapter 进运行中 daemon
- `hyprial adapter start/stop/status/list/remove`;`remove` 成功后也会自动尝试热加载，
  `list` 的 `agent` 列显示当前绑定
- 入站绑定写在 agent 的 cell 文档里:`hyprial agent cell show <agent> --json` 读出 cell 与 revision,
  在 `members` 加 `{"ref": "<ref>", "role": "adapter"}`、在 `params."cell.members"."<ref>"` 写
  `{"adapter": "<adapter>"}`,再 `hyprial agent cell apply <agent> --expected-revision N --file CELL.json`;
  去掉该成员即解绑。一个 agent 可绑多个 adapter;同一 adapter 被多个 agent 声明时路由失败关闭(无人接收)。
  旧的 pin/unpin 与 authority 子命令已退役,见 `docs/guides/cell-authority-migration.md`
- `hyprial adapter user-auth check|complete` — lark-cli 用户凭据检查与设备流收尾
- `hyprial adapter identities` — 平台身份 ↔ hyprial owner 映射(白名单校验源)
- 本机 agent 与 adapter 不得同名。adapter create/add/onboard/start 与 agent create/start
  会拒绝冲突；例如 agent 用 `worker`，adapter 用 `worker-feishu`。`--force` 不绕过此检查。
  旧配置中的冲突由 `hyprial doctor` 报 warn；`doctor --fix` 不改名，需人工重建 adapter。
- 路由前提:机器人必须先被发过消息才有 chat id;新 App 让对方先 DM 一句

### PAC workflow 与 routine
- workflow plan/run/status/list/inspect/cancel 是图的高级入口；complete/fail 携带当前 request-id 与 reason-ref。
- `hyprial workflow gc --dry-run [--json]` 只读预览下一次有界回收会删除的 workflow-owned
  worker agent；不带 `--dry-run` 会拒绝。选择规则与 daemon 内独立 PAC GC 服务相同，
  借用 actor（即使名字以 `wf-` 开头）、未关闭图、清理未完成、在线或 cell 绑定了 adapter
  (cell runtime 不可用或 cell 未加载时按已绑定处理,报 `adapter-bindings-unknown`)的 worker 都不会删除；`hyprial doctor` 的 `pac-gc` 项显示上次 pass 与当前 backlog。
- `workflow-worker-cleanup` 的 `attention` 不是终态：daemon 在兜底扫描中自动重试（新 lifecycle
  operation id），本 actor 自己的旧实例（PAC 标记指向本图本节点，或无标记但 entity token 与回执一致）
  会被停掉并收敛到 `complete`；只有外来标记/外来 entity token 持续 attention，需人工核对。
  失败重试按 attempts 指数退避（5 分钟起翻倍，上限 6 小时）；扫描每 30 秒一轮，在途（含 graph authority effect 池）+新放行的重试最多 2 个；只限重试，首次评估（部署时约 186 个回填）、
  worktree-cleanup、expansion-preflight 不计入，开放图不保证有空闲 effect 线程。
  `worker_in_use`：agent 的 cell 绑定了 adapter（绑定未知按已绑定），或有已注册交互会话且在线（presence 缺失/报错按在用处理；
  已接线但未就绪的重启窗口读作离线，可能被 stop、不会被删），GC pass 报告如实记录 destroy 复核拒绝原因，
  cleanup 不停、GC 跳过、destroy 复核拒绝（`worker-in-use`），`lastObservation.inUse` 说明原因；
  释放（从 cell 去掉 adapter 成员/退出会话）后下一轮自动继续，否则由人决定。
- 工作流 YAML format 2 使用 name/nodes/edges/workers/defaults/on_failure。旧 targets/await/retry/report_to 执行格式已退役。
- 节点默认独立临时 worker，显式 worker 键共享上下文，owner 全 URI 显式借用已有 actor。
- 图结束回收所属 worker；借用对象不被接管或停止。完成是显式 flag，不解释回复文本。
- on_failure 为 terminate（默认整图失败回收）、continue（独立分支继续）或 hold（等待负责人处理）。
- 相对 timeout 在派图时确定固定截止，等待依赖计入预算；不自动顺延或重试。报告需显式节点。
- routine format 2 显式选择 mode: scheduled/source。定时模式串行、跳过重叠与错过周期，source 模式按任务 UUID 去重。
- routine 默认创建并持有常驻协调 agent，也可执行工作；rm 回收自己拥有的 actor，pause 不回收。
- routine 的 `launch` 决定其常驻协调 agent 的运行时；省略时 schema 默认 `{tier: fast}`，会解析成 fast 档第一候选 **codex/gpt-5.6-luna**，而不是所属 agent 的 harness/model。注册自驱 routine 时必须显式写与所属 agent 一致的 `launch`（字段与 CLI 的 `--provider/--model` 同名同义；如 `launch: {harness: claude, provider: deepseek, model: deepseek-flash}`，`tier` 与显式 harness/provider/model 二选一）。`routine add --template` 没有 launch 参数，需用带 `launch` 的 routine YAML 执行 `routine add <file>`，或注册后用 `routine set <name> <file>` 替换。
- `hyprial workflow list [--all]` / `hyprial routine list [--all]`：不加 --json 时输出人读的表格。agent 默认只看到自己派的（或自己拥有的），加 --all 看本节点全部，不需要额外授权；主人在终端里直接运行，默认就能看到全部。--json 每行带 `lastProgressAtMs`（该图最新一条 journal 事件的时间；显式心跳命令 `workflow progress` 尚未提供，计划中，见 #1210）、`currentNode {nodeId,state}`（按依赖顺序第一个未完成的节点）（workflow），以及 `lastDispatchAtMs`、`lastOutcome`（routine）。判断是不是卡住，看这两个字段，别只看 state=running。
- `hyprial workflow gc --dry-run [REPO ...] [--all]`：只读检查本机 Git worktree；目录或其 gitdir 链接已丢失、有未提交文件或有未推送提交都算 leftover。它只为已合并且干净、且 24 小时内没有活动的树建议 remove(刚从 origin/dev 新建、还在被 worker 用的树看起来和已完成的一样，所以按该树自己的 index/HEAD 修改时间排除)，建议前会提示先确认没人在用；只为 git 标记为 prunable 的树建议 prune；默认同时按 origin/HEAD、origin/dev、origin/main 判断是否已合并，不会执行这些命令。
- `hyprial workflow overview show|publish <space>`：PAC 总览页。`publish` 把本机的任务线、Workflow 和 Routine 写成 orgfs 空间里的 `pac-overview/nodes/<节点>.js`（blob，只在内容变化时写），维护工作项视图资产 `pac-overview/work_items_view.js`，并维护合并所有节点的 `pac-overview/index.html`；它还会拉取其他节点尚未到达的文件、刷新本机 checkout，最后输出可以直接用浏览器打开的 `file://…/index.html`。页面每分钟自动重载。图数据只包含运行这条命令的调用方看得到的图，节点输出会脱敏并截断；页面另带 org 的全部工作项（状态、负责人/指派人、清单、验收标准、依赖、最近 20 条日志、打开/关闭时间；日志、验收与清单文本脱敏并截断），发布到某个空间即对该空间的读者可见。⚠️ 信任边界：空间里任何有写权限的成员都能往页面上放脚本，所以只发布到所有写入者都可信的空间；`publish` 会开启并保持本机对该空间的只读 checkout（`hyprial org fs space checkout <space> --disable` 关闭）。
- workflow history list/status 只读查询旧任务。切换会终止并归档未结束旧任务；空 home 也有旧写入屏障，不代表存在历史任务。

最小任务书示例（先替换工作目录与任务，再 plan/run）：

```yaml
version: 2
name: example
on_failure: terminate
defaults:
  launch: {tier: strong, cwd: /absolute/worktree}
  timeout: 1h
nodes:
  - {id: work, task: Complete the approved work and record verifiable evidence.}
```

定时协调示例：

```yaml
version: 2
mode: scheduled
name: coordinate
role: dispatch
schedule: {interval: 15m}
launch: {tier: fast}
task: Check current work and dispatch necessary child workflows without duplicating accepted tasks.
on_task_timeout: {action: escalate, escalate_to: 'user:owner'}
```

### Workflow 图与公共订阅
- `hyprial workflow plan FILE` validates a draft and `workflow run FILE` publishes
  one managed graph with frozen structure. `workflow inspect/list` are the read surface.
- `hyprial workflow cancel GRAPH` closes a graph and reclaims only its owned workers;
  borrowed actors remain externally owned.
- `hyprial workflow complete/reset GRAPH NODE` uses the current request and evidence;
  reset is explicit rework and never an automatic retry.
- `hyprial workflow worker stop|restart GRAPH ACTOR` controls only graph-owned workers.
  Stop fails unfinished work immediately; restart keeps it and creates a new incarnation.
- `hyprial workflow notify resend GRAPH` retries only undelivered notifications, and
  `workflow migration status` reports schema-8 owner migration.
- `hyprial workflow events GRAPH --snapshot --json` reads one snapshot cursor;
  `workflow events GRAPH --after CURSOR --journal-id ID [--follow] --json` provides
  strict cursor-based resumption. `workflow inspect GRAPH --node NODE --context --json` is read-only,
  uses one cursor, and never reads reference bodies or inbox messages.
- Cursor resync, immutable refs, assignment projection, follow termination, and
  stderr-only stream errors retain the public contract in the contract documents.

### agent / worker / 消息
- Codex `skills/list` 的技能加载错误只产生提醒，不阻止 agent 启动；有错误的技能不会加载。
  查看启动结果的 `warnings` 或 daemon 日志的 `codex.skills.load_warning`，按脱敏后的错误原因修复。
  Squire 缺少 YAML frontmatter 时，没有自动修复命令。由用户手工补齐 frontmatter；自定义技能也须手工修复。
- `hyprial agent start codex|claude --name <name> --effort <level>` 在 `--` 前设置推理强度；支持交互、headless 与 resume。Codex 接受 `none|minimal|low|medium|high|xhigh|max|ultra`，Claude 接受 `low|medium|high|xhigh|max`；模型支持集由原生 harness 决定。不传时，DeepSeek/hyprial 模型厂商显式注入 Codex `high`、Claude `max`；原生 OpenAI/Anthropic 不注入值。不与尾随原生 effort 参数混用。详见 `docs/reference/harness-model-providers.md`。
- P2 DeepSeek 的启动环境缺少有效 agent key 时立即拒绝启动；补齐授权 grant 后显式重启，不靠重连修复，也不回退宿主 key。此处是启动授权检查，不新增运行中撤权保证。
- `hyprial agent attach <name> --json` / `hyprial agent detach <name> --json`
  — 把一个不由 daemon 托管的外部 harness（如云端 Claude Code 会话）接成 agent：登记身份，
  记录 `custody=external`，agent cell 只有这一个 harness 成员。daemon 不启动、不恢复、不停止它，
  也不绑定默认 routine；`hyprial agent start <name>`（及 lifecycle 启动/恢复/routine 唤醒）对它一律
  `INVALID_ARGUMENT` 拒绝；`agent create` 不接受 `custody` capability（只有 attach 能写）。
  消息照常进它的收件箱。同名重复 attach 不变（`changed:false`）；
  已被 daemon 托管的同名 agent 拒绝（`AGENT_EXISTS`）。detach 只移除 harness 成员，
  身份、id、收件箱与历史保留，消息继续排队到再次 attach；它不是 stop/destroy。
  cell 成员以 agent 记录为准：手工加进这个 cell 的成员会在下次对账时被移除。
  OrgFS 不可用时 attach/detach 与收发照常，`cell.state` 为 `deferred`，恢复后自动补齐 cell；
  `skipped`＝owner 不是 OrgFS 作者、不建 cell；`refused`＝cell 被拒（如已 retire），不自动重试。
  ⚠️ 读写身份（未登记会话的外部 harness）：`message batch --from <name>` / `reply --from <name>` /
  `ack --from <name>` **只在默认 `HYPRIAL_SENDER_AUTH_MODE=observe` 下可用**；`enforce` 下一律
  `SENDER_NOT_AUTHENTICATED`。此时 reply/ack 用操作员形式 `--from user:<owner> --on-behalf-of <name>`；
  读取（batch）目前没有操作员形式，enforce 下未登记会话的 attach harness 暂无受支持的读取路径。
  **0.5.5.4 不支持在 `HYPRIAL_SENDER_AUTH_MODE=enforce` 下使用 attach（默认为 `observe`）**
  （attach is not supported under `HYPRIAL_SENDER_AUTH_MODE=enforce` in 0.5.5.4; the default is `observe`；per Allen 2026-11-07）。
  无会话时 `message read --from <name>` 被会话围栏拒绝（`STALE_SESSION`），正文以 batch 为准。
- `hyprial agent grant <actor> --capability <cap> --scope <scope> [--grant-id <id>] [--revision <n>] --json`
  记录当前 agent 化身的授权；新 id 默认 UUID、revision 默认 1，更新同一 id 必须递增。
  `agent grant <actor> revoke <id>` 撤活动记录；`agent grants` 看全局台账，单个 actor 的台账与含销毁前的历史用 `agent grant <actor> list --audit`。
  scope 闭集：agent-home=`self`（不可撤）、isolation=`directory|container`、org-context=`accepted`；
  see-actors/send-to 为 principal URI JSON 数组；tool-surface 为名称 JSON 数组（`channel` 能力已退役）；shared-path 为 `{"path":"/absolute/path","mode":"ro|rw"}`。
  本片只记账与格式校验，不限制运行权限，不代替 secret grant，也不证明调用方身份；执行者记为本机 host owner。
- `hyprial agent teleport <name> --to <node> [--dry-run | --yes] [--json]`
  — 经 daemon 网络把本机 agent 连同 home 与会话迁到另一个节点：本机停止并导出，目标恢复同一会话，
  成功后本机的 agent 与 home 被移除。`--to` 是目标的 node id（对方 daemon 的 `HYPRIAL_NODE_ID`，未设置时为对方 home 里 `node-id` 文件的值），
  不是 SSH 主机名，也没有 host 解析。只允许在同一用户 owner 的主机之间迁移。只迁已有 agent home、headless、harness 为 claude/codex/pi、无 pinned adapter、
  cwd 未设置或为 `<home>/workspace` 的 agent；没有 home 的先 `agent ensure-home`。以该 agent 为执行主体的 routine（含已停用的）随 agent 迁移：本机先暂停原本启用的 routine 并等在途任务
  结束（上限 120 s，超时为 `TELEPORT_IN_FLIGHT_TIMEOUT`，agent 与 routine 原样恢复），目标先以暂停状态创建（YAML 里指向源 agent 的 URI 改写为目标 agent），
  commit 后只恢复原本启用的；成功后本机的 routine 与 agent 一并删除。先用 `--dry-run` 看预检与目标检查结果（不改动任何一侧）；正式执行须 `--yes`，
  交互终端可改为确认提示，`--json` 或非终端无 `--yes` 返回 `CONFIRMATION_REQUIRED`。这是唯一的迁移命令；旧的 SSH 迁移链路已移除。
  失败时源 agent 会被重启并保持原状，**唯一例外是 `TELEPORT_UNRESOLVED`**：目标对 receive 没有可读应答，
  目标可能已经在跑该 agent，所以源 agent 刻意保持停止。此时按错误 `data.manualCheck` 人工核对：在目标 `hyprial agent list`
  看是否有同名 agent；有且会话完好则在本机移除已停止的源 agent，没有则在本机 `hyprial agent start <name>`。
  `TELEPORT_UNRESOLVED` 时本机不恢复也不删除任何 routine，`data.pausedRoutines` 是本次暂停的 routine，按核对结果处理：agent 在目标则这些
  routine 也已在目标（暂停，待恢复），本机副本随 agent 一起删除；agent 没到目标则在本机逐个 `hyprial routine resume <name>`。
  `TELEPORT_SOURCE_CLEANUP_FAILED` 表示 agent 已在目标运行、本机已停止的副本没移掉（或某个 routine 没删掉），须手工清理；此时没有发 commit，
  `data.pausedRoutines` 是目标上仍暂停、在源侧原本启用的 routine，到目标节点逐个 `hyprial routine resume <name>`。结果里 `commit.ok=false`
  同理（`commit.pausedRoutines` 非空）；原本就停用的 routine 在目标保持暂停，不算警告。
  没有对端认证：任何能在 daemon 网络上到达目标节点的对端都能回应请求，见 KP-047（由独立 docs PR 登记），只在受信网络里用。
- **每个 agent 自己的非模型凭据**:`hyprial agent secret set/rotate/list/history/remove`
  管理 `<agent home>/secrets.sqlite`;值只从非 TTY stdin 输入,list/history 不回显值。v1
  不自动扫描或迁移未授权的外部旧文件,owner 核对归属后逐条导入;无 validator 的服务缺省拒绝,只有显式
  `--no-validate` 才放行。远端撤销与本地删值是 `revoke_remote + remove` 两件事,只有两条
  事件都在且值不在盘上才完成;同 uid 直接读取是已知边界。操作步骤、威胁模型与验收清单见
  `docs/guides/agent-secrets.md`。
  `agent-private` grant 的 `entry-id/field-name` 必须对应 SQLite 的 `service/field`。
  已有 grant 指向的直接旧文件会在首次解析时迁入 SQLite，核对后删除；不保留文件回退或明文备份。
  冲突时拒绝解析并保留源文件，不覆盖 SQLite。新建 agent 不生成空 `secrets/`；
  `secrets/native/` 的第三方 harness 登录态原样保留，只有空目录才删除。
- **新建 Claude agent 的默认凭据**：操作者先在可信终端运行 `claude setup-token`。
  在 host 操作员终端把 token 从非 TTY stdin 传入
  `agent provider login claude --setup-token --actor <owner> --default`。
  值只存 owner 的 SQLite；默认只保存化身和条目引用，不含值。
  新建本机 Claude agent（含 PAC worker）自动获得 `default-claude` grant；不复制值。
  CLI 创建必须指定 `--preferred-harness claude`。
  `agent provider default --json` 查引用，`agent secret ls <consumer> --json` 查 grant。
  `agent secret revoke <consumer> default-claude` 撤 grant；`agent provider default --clear`
  只清默认。默认变更不改已有 agent，不覆盖已有显式 auth grant，不恢复已撤销 grant。
  已有 agent 可用 `agent secret grant <consumer> --source agent-shared --owner-agent <owner>
  --entry-id claude --field-name CLAUDE_CODE_OAUTH_TOKEN --environment-name CLAUDE_CODE_OAUTH_TOKEN
  --grant-id claude --revision 1` 显式授权。轮换或撤销后，消费者必须重启。
  原生复制仍用 `agent provider login claude --actor <owner> --default`。
  复制可刷新的 OAuth 登录给多个 agent，会使刷新相互干扰；共享默认宜用 `--setup-token`。
  旧版 provider-name 默认不支持 PAC 自动复制，必须重跑 `login claude --default --actor <owner>`。
  缺少显式 auth 环境和自有 native 根时，P1 SDK 启动永久失败：`CLAUDE_CREDENTIAL_MISSING`。
  工作流所有或启动时创建的 agent，其 PTY 启动也执行此检查。
  命令覆盖和交互式 CLI 不得绕过检查。不得为每个 agent 运行 `/login`。
  操作员创建的常驻 PTY 不因缺少凭据被拒绝，来源记为 `ambient`。
  daemon 组合的 P1 Claude 环境始终选择 `<agent home>/secrets/native/claude`。
  该根的 `projects` 链接指向 `<agent home>/state/sessions/claude`。
  env grant 使用同一路径，不继承宿主的 `CLAUDE_CONFIG_DIR`。
  新建空根的 `.hyprial-launch-provisioned` 非秘密标记跨重试保留。
  带标记的根须有 env grant，或由显式原生安装清除标记，才能通过检查。
  env grant 不清除标记，撤销后仍拒绝缺少来源的启动。不得手动删除标记。
  常驻交互式 CLI 的凭据限制留待 0.5.5.2 secrets 改造。
  已有的自有私密根即使没有凭据文件，也可通过来源检查，支持 Keychain-only 来源。
  文件存在时仍检查权限；系统不读取 Keychain。操作步骤见 `docs/guides/agent-secrets.md`。
- **给 agent 下发 provider 凭据**（以下旧 `user-provider` 路径归 daemon 登录用户；不回显；每条可撤）。设计:
  `docs/design/provider-login-2026-10-14.md`。
  - API key(默认):`printf %s "$KEY" | hyprial agent secret provider-write <entry> --field apiKey`
    (stdin 非 TTY、一行),再 `hyprial agent secret grant <actor> --source user-provider --entry-id <entry>
    --field-name apiKey --environment-name <ENV> --grant-id <id> --revision 1`。
    多个 agent 同一条目:`hyprial agent provider grant-key --entry-id <entry> --field-name apiKey
    --environment-name <ENV> --grant-id <id> --revision 1 --actor a --actor b ...` —— 逐个 grant,
    遇拒即停,结果列 granted/replaced/alreadyGranted/failed/notAttempted(`ok:false` 退 1);⛔ 没有 `--all`。
    续跑:原命令原样重跑即可,已持有同一授予的 actor 记 alreadyGranted 跳过;同 grant-id 已指向别的
    条目 ⇒ 拒 `PROVIDER_CREDENTIAL_EXISTS`,确要替换加 `--replace` 并调高 `--revision`。
    撤:`hyprial agent secret revoke <actor> <grant-id>`(条目保留)。
  - OAuth 种子(显式选择;参数是 harness 名,不是模型厂商):`hyprial agent provider login claude|codex|pi --actor <name> [--force]
    [--source <file>]` 把本机该厂商 CLI 的登录文件复制进 agent 的 `secrets/native/<harness>/`(0600)。
    pi 两个文件一次种入、全有或全无,`--source` 对 pi 拒 `PROVIDER_SOURCE_AMBIGUOUS`。
    一次性:过期/坏了 ⇒ 厂商 CLI 重新登录后 `--force` 重跑;不做刷新。只报 "seeded",⛔ 不声称有效。
    撤:`hyprial agent provider logout <p> --actor <name>`(运行中的 agent 重启后生效;作废 token 本身
    还需厂商 CLI 登出)。
  - Codex 的 native home 按模型厂商投影凭据策略:`openai` 使用 `cli_auth_credentials_store = "file"`
    并保留/链接 agent 自己的 `auth.json`;DeepSeek、Hyprial 等非 OpenAI 模型厂商使用
    `cli_auth_credentials_store = "ephemeral"`,只接收本次进程的 secret grant,active native home 中不得
    留 `auth.json`。创建、修复、升级迁移和交互启动都执行同一规则。
    旧的非 OpenAI home 若已有 `auth.json`,系统不会删除:它会移到该 agent 的私有 state 路径
    `<HYPRIAL_HOME>/agents/<name>/state/credential-backups/codex/auth.json`(重名加 `.1`、`.2` 等,普通文件且 0600),并发出
    `CODEX_CUSTOM_PROVIDER_AUTH_BACKUP` notice;`hyprial doctor` 的 `codex-credential-backups` 项也会列出
    agent 与绝对 backup path,不含 secret 内容。要把该 agent 切回 OpenAI:先停止/替换非 OpenAI
    启动规格并明确选 `--provider openai`,再执行
    `hyprial agent provider login codex --actor <name> --force --source <doctor 给出的绝对 backup path>`,
    然后重启 agent;不要直接把文件复制回 projection/native home。确认新 OpenAI 启动正常后,可把旧
    backup 移到同权限的批准归档位置;只要文件仍在上述目录,doctor 会继续提示。
  - 新建默认：Claude 使用前述带 `--actor` 的引用流程；Codex/Pi 可用
    `hyprial agent provider login <p> --default` 记录默认名称（回执带 previous）。
    Codex/Pi 的 `agent create` 仍从本机登录复制，结果 `providerSeed.state` 为
    seeded/refused/not-configured，后两者在 stderr 给出修法。
    `hyprial agent provider default [--clear]` 查/清默认。
    ⛔ 不回溯已有 agent;`--preferred-harness` 与默认不同 ⇒ skipped;只有 operator shell 能改默认;
    不给非本机 owner 的 agent 种入。`--provider hyprial`(#1407)走 `HYPRIAL_API_KEY` 的 secret grant,不经此路。
- `hyprial agent start claude|codex|pi|jev --name ... [--headless] --cwd ... -- <harness args>`
  `jev` is a packaged, headless TypeSafe worker: it accepts no script or model-vendor selector,
  model, or positional runtime arguments. It finds `TYPESAFE_API_KEY` the same
  way the user-side `jev` command does: if you can already run `jev` because the
  key is in `~/.config/typesafe/env` (`export TYPESAFE_API_KEY='...'`), nothing
  else is needed. An explicitly granted `hyprial agent secret` still wins over
  the file (write the entry, grant the current actor incarnation, then `hyprial
  start jev`; grants are bound to one agent instance). The ready frame reports
  `credentialSource` as `environment`, `file` or null; with neither, the first
  call fails `PROVIDER_AUTHENTICATION_FAILED` with a message starting
  `TYPESAFE_CREDENTIAL_FILE_ABSENT` / `_KEY_ABSENT` / `_ENV_ABSENT`.
  (headless claude 必带 --dangerously-skip-permissions)
- **起测试用的隔离 daemon 必须设 `HYPRIAL_NETWORK_ISOLATED=1`**(见 `docs/guides/network-isolation.md`):只允许 loopback
  endpoint,不做监听推导、对端发现、转发、gossip、用量抓取;设了但为空或拼错 ⇒ 拒启。起来后看
  daemon 起来后确认 `HYPRIAL_NETWORK_ISOLATED` 生效（由 daemon 实际生效的状态算出,⛔ 不是回显环境变量）:
  一级 `hyprial ps` 已随 R7 退役；测试/工具读 daemon IPC `ps` 的 `zenoh.isolated`,操作员看 `hyprial doctor --json` 的
  `zenoh` 检查项。只靠 `HYPRIAL_PEER_DISCOVERY=0` ⛔ 不算隔离。
  隔离 daemon 的 `org create` 只建本地 org/directory/ACL space，返回 `tailnetInitialized=false` 和 null 发布字段；
  不启动 sidecar，也不能用 `org invite`/`org join`。需要真实 membership 时改用 `tailcat_forwarding=True` 的本机 labrelay 拓扑。
- 自动升级(03:17/15:17)固定检查公网源，并与手动升级共用上述 exact-commit
  `uv.lock` 约束路径；缺锁/导出失败不做未锁定安装，高于公网的版本以
  `declinedDowngrade` 跳过。需要升级时它只安装、**不重启**:装好后 daemon 仍跑旧代码,主人会收到"新版本已安装,等待确认后重启"。
  确认切换:`hyprial daemon upgrade apply [--json]`(主人自己运行,或让任一 agent 代为运行);没有待重启时它什么都不做。
  `hyprial daemon upgrade status --json` 的 `pendingRestart` 是待重启记录，`pendingRestartState` 表示这条记录现在是否还需要处理：
  `waiting` = 还在跑旧代码，需要重启；`applied` = daemon 在安装之后已经重启过，不用再重启；
  `unverified` = daemon 没有应答，`daemon upgrade apply` 会重新检查。判断要不要重启看 `pendingRestartState`，
  不要看 `lastRun.restart.awaitingConfirmation`：别的途径重启之后，那个字段会一直停在“等待”。
  不带 `--json` 时输出给人看的摘要。手动 `hyprial daemon upgrade` 仍会直接重启。
- `hyprial agent start --tier fast|strong|super --name ... --headless` —— 由 tier 选定 harness、模型厂商与模型
  (daemon 解析并审计)。⛔ 不要再同时写 harness 名或厂商/模型参数:`agent start pi --tier super` 以前会
  **静默丢掉 --tier**,起一个没有模型的 pi(落到全局默认);现在直接 `INVALID_ARGUMENT`,什么都不创建。
  要指定模型就用显式写法:`hyprial agent start pi` 加厂商与模型两个选项(见 `hyprial agent start --help`)。
- Pi 的 agent-home P2 启动前置：每次启动都必须在 `--` 后显式传
  `--approve`，且 `--cwd` 必须是 Git working tree 内的目录。未显式 `--cwd` 时，agent 自有的默认
  `workspace/` 会初始化为独立 Git 仓库；绝不自动修改用户指定的 cwd。完整示例：
  `hyprial agent start pi --name worker --headless --cwd /absolute/path/to/git-working-tree -- --approve`。
- `hyprial agent start claude|pi|codex --headless --resume <session-id> --name ... --cwd ...` —— 恢复一个既有会话。
  不加 `--resume` 时，若同一 harness/name 已有 desired-state 行，会接续其已学 sessionRef（708）；
  要显式新会话请先 stop 掉旧规格再 start。id 取自 start/transfer 返回的 `sessionRef`,或 transcript 文件名
  (claude `~/.claude/projects/<编码cwd>/<id>.jsonl`)。要么续上那个会话,要么拒绝,⛔ 不会悄悄换新会话:
  找不到 transcript ⇒ `RESUME_SESSION_NOT_FOUND`(什么都不起);起了却没续上 ⇒ `STRICT_RESUME_FAILED`(worker 已停)。
  交互转 headless 必须显式 `--replace-interactive` 并带 `--resume <sid>`;daemon 会先向带 PID/birth fence 的交互承载发 SIGTERM 并等待退出,超时或无 PID/birth fence/unknown 状态则拒绝切换（`INTERACTIVE_CARRIER_STOP_FAILED`，不是 `STRICT_RESUME_FAILED`）。不带 `--resume` 的 switch 在 CLI 和 daemon 都拒绝。
  `--cwd` 要与原会话一致(pi 只在当前 cwd 的会话目录里找)。
  (headless codex 带 `-- -a on-request -s workspace-write`;hyprial 客户端只批准 worker 自己的 harness-bridge MCP 工具调用,其它 elicitation 与命令/文件审批一律拒;reviewer 在 worker 线程级为 user,任何提供方下一致。批准≠授权,授权仍在 daemon 侧)
- interactive codex(`hyprial agent start codex`,不带 `--headless`)的 harness 工具(`harness_reply/ack/read_body/send`)是 MCP 工具,
  需要 `on-request` 审批才能被调用:启动与 `--resume` 都自动带 `-a on-request`,审批弹窗在 TUI 里由人批准;
  `-a never` 与 `--dangerously-bypass-approvals-and-sandbox` 会被 `INVALID_ARGUMENT` 拒绝(`never` 下 codex 拒绝所有 MCP 调用)。
  `-a never` 只用于 headless。
- daemon 重启时只恢复近期活跃的 agent:空闲超过全局阈值(默认 12h)的会被标成 `idle-suppressed`,
  不再自动拉起;有待处理消息/托管、有当前 PAC 请求、设为 `always`、或显式 start 时会被唤醒。
  `hyprial agent restore --threshold <如 12h>` 改全局阈值;`hyprial agent restore <name> active|always|never`
  按 agent 覆盖(`always` 总是恢复,`never` 从不自动恢复)。
- 模型额度耗尽或凭据失效时,agent 会进入持久的 `blocked` 状态(`hyprial agent list` 里带原因):
  进程照常恢复,但**投递被挡住**,直到人处理完原因后执行 `hyprial agent unblock <name>`(幂等)。
  主人只收到一次通知;`hyprial doctor` 会列出 blocked 与被抑制的数量和补救命令。⛔ 不要靠重启来解除 blocked。
- `hyprial agent create` — 注册 agent 记录；未传 `--config` 时在该 agent home 的
  `config/` 建立最小显式 C，使 claude/codex/pi 默认启用 agent-home P2；未传
  `--cwd` 时默认使用同一 home 的 `workspace/`。同时绑定 15 分钟一次的默认
  agent-home setup routine。显式 `--config`/`--cwd` 保持其原有含义。确定用途后先用带显式
  `launch` 的 routine YAML（见「PAC workflow 与 routine」）执行 `hyprial routine add <file>`
  绑定新 routine，或用 `hyprial routine set <name> <file>` 原地修改（不要依赖省略 launch 的默认值）；
  最后一个绑定不能直接 `routine remove`。
  `hyprial agent list --json` 的 `agentsWithoutRoutine` 只读列出尚无绑定的既有 agent。
  managed worker 内的 `hyprial message send|reply|ack --from <自身 actor>` 会从环境携带
  daemon session fence；`send`/`reply` 还必须带 `--summary`；本机人工代发必须用
  `--from user:<owner> --on-behalf-of <actor>`，线上 sender 始终是 user 而不是 actor。
  `hyprial message ack <mid> --from <自身 actor>` 只适用于有匹配 session 的 worker。
- `hyprial agent config rebind <name> --source <绝对目录> [--dry-run] [--json]` —
  修复既有 P2 agent 的配置根 C。先 `hyprial agent stop <name>`；命令会从 durable desired state、
  supervisor 和运行 lease 证明已静止，不以连接器列表为依据。暂存目录及子目录必须为
  当前用户所有且模式 `0700`，普通文件必须为 `0600`；符号链接、root 文件、credential 路径和
  疑似 secret 内容会被拒绝。成功后保存 durable receipt，并且只 CAS 更新 C binding；agent URI、
  incarnation、home、routine 不变。崩溃后用同一条 rebind 命令恢复。回滚用
  `hyprial agent config rebind <name> --rollback <receipt-id> [--json]`；它重验 receipt、hash 和
  静止状态，恢复原 binding 后清理该次发布的 revision。遇到 `AGENT_CONFIG_CAS_MISMATCH`、
  `AGENT_INCARNATION_MISMATCH` 或 drift 错误时停止，不要覆盖现场。
- `hyprial message inbox <actor> [--json]` — 人查看某个本机 actor 的收件箱(待处理消息 +
  系统通知,含正文)。发件箱看 `hyprial message outbox list --actor <actor> [--json]`(它发出、仍在
  排队的消息)。**只读**:不取走、不 ack、不清通知,
  agent 之后照样收到。⛔ 不要拿 MCP 的 harness_read 或 daemon 的 message.pending.list 来"看一眼":
  后者每次调用都会清掉该 actor 的系统通知。
- MCP 会话内:harness_whoami/read/reply/ack/send/targets;
  **回入站消息用 harness_reply(回复+消费一步),新话题才用 hyprial message send;新话题不是回复**
- **发消息:summary、回复**(2026-10-15 硬切后;2026-11-21 起发送不再经 link 闸门):
  0. 给飞书里的人发文字或文件:发 `--to user:<owner>`,由你所在 cell 的 `on: "user"` 连接选 chat。
     `route:<adapter>:<route>` 地址已退役,发给它报 `UNSUPPORTED_TARGET`(见
     `docs/guides/migration-route-uri-retired.md`)。
  1. 发送不经 link 闸门:向其它 agent、`user:` 目标的发送都直接投递(对方不在线时照常
     进出站队列重试),不会因为 link 被挂起,结果里没有 `holdReason`,也不需要等 link 建好再重发。
     link CLI 命令与 `link.*` / `threads.*` daemon 请求均已退役;见迁移指南
     `docs/guides/send-link-and-thread-removal.md`。
  2. 命令面:
     - `hyprial message send --from <自身> --to <对端> [--to …] --summary "<≤512 字节>" "<正文>"`;
       `--summary` 必填,缺失、全空白、超长都在本地报 `SUMMARY_REQUIRED`(退出 1,`--json` 带
       `data.summaryBytes/maxBytes`),不发出任何消息。
     - `hyprial message reply <messageId> --from <自身> --summary "…" "<正文>"`:唯一能闭合请求的方式。
     - `hyprial message read <messageId> --from <自身>`:取正文;是消费动作(记 fetched),不闭合,仍要
       reply 或 ack。
     - `hyprial message ack <messageId> --from <自身>`:不需回复时结算。
     - `hyprial message outbox list [--json]`:出站队列里尚未送达的消息(该命令不接受 `--from`)。
     - 看会话记录(只读,不取走、不 ack;daemon 方法 `conversations.list` / `conversations.history`):
       - `hyprial message conversation list [--limit N] [--cursor C] [--json]`:
         会话(conversation cell,`c.…`)按最新在前,翻页用上一页给出的 `--cursor`。
       - `hyprial message history --conversation <c.…> [--mode tail|older|after] [--cursor C] [--limit N] [--json]`:
         一个会话的消息记录,往前翻用输出末行给出的 `--conversation … --mode older --cursor …`。
       - 身份:worker 会话内默认读自己(带会话 fence);会话外默认是本机 operator `user:<owner>`,
         看某个 agent 的视角加 `--on-behalf-of <agent>`。`message send --conversation` 仍是旧
         `conversationId`,不是 `c.` id。
     - `hyprial agent top`:显示进程指标与待投递队列计数 `Q`。
  3. 收到的消息:上下文里只有发送者、意图、messageId、summary、正文字节数;正文用 `hyprial message read`
     或 `harness_read_body` 取。
  4. 回复规则:只有带 reply-to 的消息才是回复(`hyprial message reply`、`harness_reply`)。对请求方再 `message send`
     一条新消息不是回复:原请求仍未闭合;`message send` 返回 `openRequestsFrom`
     时 CLI 会在 stderr 提示。载体(pi、codex、claude、headless)不会在轮末替你回复(不承诺自动回复)
     ;轮末未回复的请求会收到一条 system notice「N 条请求未回复」。
  5. 拒绝码(`ok: false`,退出 1):`SUMMARY_REQUIRED`;`MESSAGE_BODY_UNAVAILABLE`
     (不可读或不存在);`MESSAGE_REPLY_UNAVAILABLE`(reply 的原请求已闭合或不存在:用 message ack 结算你这侧的行,
     若仍要对话另发新消息);保留名 `dispatch-coordinator`、`_turn-hooks` 不能注册为 agent
     (`INVALID_AGENT_NAME`)。
  6. MCP:`harness_send(to, summary, message)`,`to` 可为数组;`harness_reply(messageId, summary,
     message)`;`harness_read` 返回 summary 与 bodyBytes;`harness_read_body(messageId)` 取正文。
     读一个会话的历史(尤其是群:没被唤醒的成员靠它补看错过的消息):
     `harness_read(conversationId=<g.… 或 c.…>, mode=tail|older|after, cursor, limit)`,
     往前翻用上一页的 `nextOlderCursor`,配 `mode=older`;它只读不结算任何消息。
     只有群的当前成员能读群历史,其他人(含已被移出的)报 `CONVERSATION_NOT_MEMBER`。
  7. 群聊（`g.…`）唤醒与 @（mention）规则（2026-11-21 裁定）:
     - 一条群消息**只唤醒 `mentions` 里列出的 agent 成员**，不论作者是人还是 agent；没有 `@` 任何人的消息照常写入群历史但不唤醒任何人。
     - **群内 `reply` 默认 `@` 被回复消息的作者**：经 `hyprial message reply` 或 `harness_reply` 在群内回复时，daemon 会默认把被回复消息的作者记入 `mentions`（仅当该作者仍是当前 `agent` 成员且不是自己时；回复人类 owner、回复自己或原发件人已移出群时不加）。可再用 `--mention <成员名或a.id>`（`harness_reply` 为 `mentions`）追加其他成员，或用 `--no-mention`（`harness_reply` 为 `noMention=true`）取消默认的 `@`；两个 agent 互相用 `reply` 回复会持续互相唤醒（无跳数上限），需要停止追问时请传 `--no-mention` / `noMention=true`：
       `hyprial message reply <messageId> --from <自身> [--mention <成员名或a.id>] [--no-mention] --summary "<≤512 字节>" "<正文>"`
       `hyprial message send --from <自身> --conversation <g.…> --mention <成员名或a.id> [--reply-to <messageId>] --summary "<≤512 字节>" "<正文>"`（`--mention` 可重复；群发送不传 `--to`）。
     - 想不被 `@` 也定时跟进并回复一个群，不要指望默认唤醒——给自己加一个定时查看该群历史（`hyprial message history --conversation <g.…>`）的 `routine`，作为运行时数据配置。
  8. 载体审批(裁定 77):interactive codex 以 `on-request` 审批策略启动(有人在场,工具审批交给 TUI
     用户);`-a never` 只用于 headless,该策略下 codex 拒绝全部 MCP 调用。agent 在 interactive
     codex 里调用 `harness_reply`/`harness_read_body` 时出现审批提示是正常流程,由 TUI 前的人批准。
- **发文件/图片到飞书**:`hyprial message send --from <URI> --to user:<owner> --summary "<≤512 字节>" --file <路径> "说明文字"`
  (图片用 `--image`,可重复多个)。文字和附件推到与文字副本相同的 chat(你所在 cell 的 `on: "user"`
  连接;没有 adapter 成员时是本机 user agent cell),返回里 `pushed: true` 且
  `resourceDeliveries[].kind` 为 `file`/`image`。限制:只支持本机 owner 的 `user:<owner>` 目标
  (agent 或别的 owner 报 `UNSUPPORTED_TARGET`),不能挂在 replyTo 上(`INVALID_ARGUMENT`);
  推送只尝试一次,`pushed: false` 时文字和附件都没有发出;`pushed: true` 带 `pushCode` 时文字已到,
  `resourceDeliveries` 只列已发出的附件,其余没发(CLI 重发一次会换新 key,文字会再到一次,只补附件就只发附件);文件 ≤30MB、图片 ≤10MB、不能是空文件、要有扩展名。
  **路径规则**(daemon 强制,报 `ATTACHMENT_PATH_REFUSED`):agent 只能发自己 workspace
  (`<hyprial home>/agents/<名>/workspace`)或自己会话工作目录下的文件。hyprial home 里的
  其它文件(secrets、配置、别的 agent 的 home)和 daemon 状态目录一律不能发;指向那里的
  符号链接同样被拒。要发的截图/导出先存进工作目录再发。
  报 `reason: caller-not-authenticated` 说明这次调用没带会话凭证(不是由 `hyprial claude`
  或 daemon 启动的会话),与路径无关:任何文件都不能附带。
  ⚠️ MCP 的 harness_send / harness_reply 只能发文字,**不能**据此认为飞书只能收文字;要发文件就用上面的 CLI。
- **orgfs 导入/导出本地文件**(`orgfs_import`/`orgfs_export`、`hyprial org fs import/export`)守同一条
  **路径规则**:源文件和导出目标都必须是 workspace 或会话工作目录下的**绝对路径**,
  hyprial home 与 daemon 状态目录一律拒绝(`ATTACHMENT_PATH_REFUSED`)。导出不会覆盖已存在的文件,
  要覆盖显式传 `overwrite=true`(CLI `--overwrite`),否则报 `reason: exists`。
- **注入只含 summary;每条收到的消息都带 `from` 与 `to`**(harness_read 行、headless worker
  注入首行 `[Harness Network message from <from> to <to> …]`、pi 附着注入都一样;首行格式不变,
  正文用 `hyprial message read` 取)。经 adapter 来的消息(任何种类的 adapter 都一样)`from`
  是 adapter 地址(如 `adapter:lark:<名>`,回复走它),说话的平台账号在 `origin.sender`:
  `platformId`(平台账号 id)、`displayName`(adapter 自己观测到的显示名,可能为空);
  `origin.chatKind` 是 `dm` 或 `group`,另有 `platformMessageId`、`platformCreatedAt`。
  提示行写作 `<显示名或账号 id> (via <adapter 地址>, account <账号 id>)`。
  **经 adapter 来的消息,说话人是拥有该平台账号的人;hyprial 不判定他是不是 owner。
  owner 的指令以 `user:<owner>` 的身份到达(GUI、CLI)。**
  查某个账号在 adapter 上记录的显示名用 `hyprial adapter identities find <ADAPTER> --id <ID>`。
- `hyprial user add|show|list` 是本机只记名字的人员登记(member 带 hyprial owner,guest 没有),
  经 daemon 的 `identity.users.add|list`;`add` 必须带 `--confirmed-by <谁>`。
  `user list --json` 输出 `{"rows": [...]}`,每行是一个人(`userKey`、`kind`、`owner`、名字)。
  hyprial 不记录谁拥有哪个 IM 账号:没有 `user bind|unbind|whois|bindings`,
  人员登记也不影响入站消息的说话人呈现。
- 会话的 harness-bridge MCP/技能面由 `hyprial agent start` 启动时从 HYPRIAL_HOME 统一注入,
  不依赖启动目录的本地注册;额外 MCP server / skill / 扩展在
  `$HYPRIAL_HOME/plugins/plugins.json` 声明,按 harness 能力注入
  (claude: --mcp-config/--plugin-dir;codex: -c mcp_servers.*;pi: --skill/-e)。

## 深挖入口

- 派单编排:`.agents/skills/hyprial-workflow/SKILL.md`(repo 内)
- E2E 验收:`.agents/skills/hyprial-e2e/SKILL.md`(repo 内)
- 本 skill 与实现有出入时:以 `--help` 和源码为准,并在同一 PR 更新本文件
  (CI 要求每个 PR 显式声明 Skill-Impact,见 .github/workflows)。
