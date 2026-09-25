---
name: hyprial-ops
description: hyprial(Harness Bridge)完整能力面索引与运维操作:adapter 生命周期(创建App/onboard/pin/路由)、daemon 诊断与恢复、PAC workflow 派单、agent/worker 管理、消息排障。凡是要对 hyprial 做任何操作、或要回答"hyprial 能不能做 X"时使用——先查本索引和 --help,再下结论。
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
人工在后台做";实际 `adapter onboard` 一条命令就能自动创建 App。
**--help 是权威,记忆和本文件都只是索引。**

## 能力面地图(按域)

### daemon 生命周期与诊断
- `hyprial init` — 启动 daemon(已含平滑重启逻辑);`hyprial doctor` — 健康检查
- `hyprial ps [--json]` — daemon/connectors/interactiveSessions/pendingMessages
- `hyprial log --name <worker> / --actor / --since / --conversation / --correlation-id`
  — daemon 结构化日志查询;原始日志 `~/.hyprial/state/logs/daemon.jsonl`
- `hyprial upgrade` — 升级并自动平滑重启（操作员显式动作，不受
  autoUpgrade 开关约束）
- `hyprial config set autoUpgrade true|false` — 自动升级总开关（2026-09-15
  Allen 定案：**缺省 = 关**）。未开启时 daemon 调度器到点只记
  `autoupdate.run.skipped`(reason=disabled)不起子进程，`autoupdate run`
  入口同拦；`hyprial autoupdate status --json` 的 `autoUpgradeEnabled` 读
  出开/关。用户或 agent 要开自动升级就走这条命令，不要手编 settings.json。
- 楔死排障(doctor 超时但进程活着):看 daemon.jsonl 的 `reconcile_overrun`

### 应用安装(hyprial install <app>)与取源诊断
- `hyprial install <app> [--yes] [--check] [--force]` — 按 catalog 钉住的精确
  commit 取源并安装;取源走 git。
- ⚠️ `code.hyprial.com` 已关闭匿名读(`info/refs` 匿名 401),需要登录才能读取,
  取源会以**认证失败**结束:请用**你自己的 Forgejo token** 为本机配置 git 凭据
  (credential helper 或系统钥匙串)。安装器不打印、也不保存 token。
- ⚠️ 该地址的 https 经公网 Cloudflare 隧道,慢是常态,取源可能**超时**。
- **应急**(仅 tailnet 内)改走 ssh 内网:这是**机器全局的 git 改写**
  (`url.<base>.insteadOf`),会影响本机所有 git;安装回执里记录的仍是 catalog 地址。
- 永久(写进 `~/.gitconfig`):
  ```sh
  git config --global url."ssh://git@git.internal.hyprial.com/".insteadOf https://code.hyprial.com/
  ```
- 单次(不改任何配置文件):
  ```sh
  env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.ssh://git@git.internal.hyprial.com/.insteadOf GIT_CONFIG_VALUE_0=https://code.hyprial.com/ hyprial install <app> --yes
  ```
- 本机设了 `HTTP(S)_PROXY` 时,确认 `NO_PROXY` 包含 `code.hyprial.com`。
- 超时与认证失败的报错里会直接带上上面这些提示;其它非零退出的 git 失败逐字不变。

### GUI 工作空间（安装 GUI 包后动态挂载）
- `hyprial gui` / `hyprial gui start` — 启动 DSH 工作空间。
- `hyprial gui status --json` — 查询 DSH 状态；`hyprial gui stop` — 停止 DSH。
- `hyprial gui upgrade [--check|--yes|--force]` — 更新 GUI 包，恢复此前运行的 DSH。
- Dashboard 已退役；`gui dashboard`、`gui dsh`、`gui all` 均不再支持，不派发这些旧指令。
- 先更新 Hyprial CLI 再更新 GUI 包。原 DSH 进程记录与会话保留；有旧 Dashboard 时，
  启动/停止/升级先核对进程身份后停止它，不再重启。身份未知时停止操作，禁止直接杀 PID。

### adapter(飞书网关)生命周期 —— 全部自动化,无需人工建应用
- `hyprial adapter onboard <name> [--new|--app-id <id>]` — **自动创建或收养飞书 App**
  并注册为 adapter。设备授权流,需 TTY:无人值守时用 tmux 伪终端代跑
  (`tmux new-session -d -s x '<cmd>'` → capture-pane 取 URL → 发给人点批准)。
  人只需点一次授权链接。
- `hyprial adapter add <name> --app-id ... --secret-file ...` — 注册已有凭据的 App;
  也用于给已有 adapter 补路由(`--route <name>=<chat_id> --force`)
- `hyprial adapter authorize <name>` / `adapter doctor <name>` — 租户 scope 申请/盘点。
  已知缺失 scope 时,用可重复的 `hyprial adapter authorize <name> --scope <scope>`
  只生成 `open.feishu.cn/page/scope-apply` 预选权限链接;它不调用飞书写接口,也不改
  App 已声明能力。请人开权限一律给这个预选页,不要给需要用户自己查找权限的开发者后台页。
  当前「对未声明 scope 有效」的实证样本数仅为 1,不可据此断言它总有效。
  `--scope` 与 `--interactive` / `--capability` 互斥;缺失 scope 无法可靠解析时不猜,
  保留命令给出的开发者后台兜底及原因说明。
  运行时配置为 Larksuite 且权限错误未带官方链接时,不生成 Feishu 预选页;
  输出 `authorizationUrlReason=larksuite_console_url_missing` 说明原因。
- `hyprial adapter reload` — **增量热加载**新/改 adapter 进运行中 daemon
- `hyprial adapter start/stop/status/list/remove`
- `hyprial adapter pin <adapter> <canonical-actor-uri>` — 入站绑定到 agent;
  `pins`/`unpin`;**pin 变更后要 stop+start 该 adapter 才穿透在跑的 worker**
- `hyprial adapter identities` — 平台身份 ↔ hyprial owner 映射(白名单校验源)
- 路由前提:机器人必须先被发过消息才有 chat id;新 App 让对方先 DM 一句

### PAC workflow 与 routine
- workflow plan/run/status/list/inspect/cancel 是图的高级入口；complete/fail 携带当前 request-id 与 reason-ref。
- 工作流 YAML format 2 使用 name/nodes/edges/workers/defaults/on_failure。旧 targets/await/retry/report_to 执行格式已退役。
- 节点默认独立临时 worker，显式 worker 键共享上下文，owner 全 URI 显式借用已有 actor。
- 图结束回收所属 worker；借用对象不被接管或停止。完成是显式 flag，不解释回复文本。
- on_failure 为 terminate（默认整图失败回收）、continue（独立分支继续）或 hold（等待负责人处理）。
- 相对 timeout 在派图时确定固定截止，等待依赖计入预算；不自动顺延或重试。报告需显式节点。
- routine format 2 显式选择 mode: scheduled/source。定时模式串行、跳过重叠与错过周期，source 模式按任务 UUID 去重。
- routine 默认创建并持有常驻协调 agent，也可执行工作；rm 回收自己拥有的 actor，pause 不回收。
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
  one managed graph with frozen structure. `workflow status/list` are the read surface.
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
  strict cursor-based resumption. `workflow context GRAPH NODE --json` is read-only,
  uses one cursor, and never reads reference bodies or inbox messages.
- Cursor resync, immutable refs, assignment projection, follow termination, and
  stderr-only stream errors retain the public contract in the contract documents.

### agent / worker / 消息
- `hyprial agent host-invite <name> --owner <visitor-owner> [--cwd ...] [--preferred-harness ...] --json`
  — 由受信 host 操作员创建访客的身份记录与 agent-home；owner 属于访客，machine 属于本机。
  同名记录不收养、不覆盖；本机 owner 应用普通 `agent create`。owner 非空、无首尾空白、无冒号。
  此入口仅创建记录，不启动 worker，不证明访客身份已由登录服务认证，也不提供进程隔离。
  仅用于组织内受信访客（L0），不据此对外开放。
  建好后可用 `hyprial start pi --name <name> --headless`（或其它已支持的 headless harness）启动；
  启动与重启恢复沿用记录中的访客 owner，host 只提供运行位置。不得用 start 改访客归属。
  此路径不扩大 transfer-receive 的启动权限；凭据、工具权限和进程隔离仍按各自能力验收。
- `hyprial agent grant <actor> --capability <cap> --scope <scope> [--grant-id <id>] [--revision <n>] --json`
  记录当前 agent 化身的授权；新 id 默认 UUID、revision 默认 1，更新同一 id 必须递增。
  `agent revoke <actor> <id>` 撤活动记录；`agent grants [actor]` 查看活动记录，`--audit` 需 actor，查看含销毁前的历史。
  scope 闭集：agent-home=`self`（不可撤）、isolation=`directory|container`、org-context=`accepted`；
  see-actors/send-to 为 principal URI JSON 数组；tool-surface/channel 为名称 JSON 数组；shared-path 为 `{"path":"/absolute/path","mode":"ro|rw"}`。
  本片只记账与格式校验，不限制运行权限，不代替 secret grant，也不证明调用方身份；执行者记为本机 host owner。
- `hyprial start claude|codex|pi|jev --name ... [--headless] --cwd ... -- <harness args>`
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
- **起测试用的隔离 daemon 必须设 `HYPRIAL_NETWORK_ISOLATED=1`**(见 `docs/network-isolation.md`):只允许 loopback
  endpoint,不做监听推导、对端发现、转发、gossip、用量抓取;设了但为空或拼错 ⇒ 拒启。起来后看
  `hyprial ps --json` 的 `zenoh.isolated` 必须为 true(它由 daemon 实际生效的状态算出,⛔ 不是回显环境变量)。
  只靠 `HYPRIAL_PEER_DISCOVERY=0` ⛔ 不算隔离。
- `hyprial start user-proxy --name <person>-proxy -- --route route:<adapter>:<dm-route>`
  —— 一人一个的消息中转 agent(打包的转发程序,无模型、无凭据、串行保序)。别人发给它的消息
  转进该人的飞书 DM(帖子标明"转述自 <原发送者>");该人在 DM 里回复时**必须以 `@收件人` 开头**
  (agent 名、agent URI 或 `route:<adapter>:<route>`),转发时去掉标记、以 user-proxy 身份发出;
  不写收件人 ⇒ 不转,回一条格式说明,⛔ 不猜。收件人解析不到 ⇒ `FORWARD_TARGET_UNKNOWN`(不重投,
  原发送者收到失败通知);发送故障照常重投。`--route` 的 adapter 必须是**该人专用**的 adapter
  (它发来的消息才算"本人")。不开飞书时用 `hyprial query <user-proxy> inbox` 看。
- 自动升级(03:17/15:17)只安装、**不重启**:装好后 daemon 仍跑旧代码,主人会收到"新版本已安装,等待确认后重启"。
  确认切换:`hyprial autoupdate restart [--json]`(主人自己运行,或让任一 agent 代为运行);没有待重启时它什么都不做。
  `hyprial autoupdate status --json` 的 `pendingRestart` 显示是否有待重启。手动 `hyprial upgrade` 仍会直接重启。
- `hyprial start --tier fast|strong|super --name ... --headless` —— 由 tier 选定 harness、模型厂商与模型
  (daemon 解析并审计)。⛔ 不要再同时写 harness 名或厂商/模型参数:`start pi --tier super` 以前会
  **静默丢掉 --tier**,起一个没有模型的 pi(落到全局默认);现在直接 `INVALID_ARGUMENT`,什么都不创建。
  要指定模型就用显式写法:`hyprial start pi` 加厂商与模型两个选项(见 `hyprial start --help`)。
- `hyprial start claude|pi|codex --headless --resume <session-id> --name ... --cwd ...` —— 恢复一个既有会话
  (不加 `--resume` 仍是新会话)。id 取自 start/transfer 返回的 `sessionRef`,或 transcript 文件名
  (claude `~/.claude/projects/<编码cwd>/<id>.jsonl`)。要么续上那个会话,要么拒绝,⛔ 不会悄悄换新会话:
  找不到 transcript ⇒ `RESUME_SESSION_NOT_FOUND`(什么都不起);起了却没续上 ⇒ `STRICT_RESUME_FAILED`(worker 已停)。
  `--cwd` 要与原会话一致(pi 只在当前 cwd 的会话目录里找)。
- `hyprial start claude --tmux ...`(交互式 TUI 放进 detached tmux)可能返回 `CLAUDE_CONFIRMATION_REQUIRED`:
  Claude Code 自己的启动确认在等人按(`data.prompt` = `folder-trust` 信任工作目录,或 `development-channels`
  开发通道警告)。这两个确认是 CC 故意留给人的,没有受支持的预先同意 ⇒ 会话**保留**,按 `data.attach`
  里的命令 attach、确认、detach,之后它自己注册。⛔ 不要当作失败重起(重起还是停在同一处)。
  (headless codex 带 `-- -a on-request -s workspace-write`;hyprial 客户端只批准 worker 自己的 harness-bridge MCP 工具调用,其它 elicitation 与命令/文件审批一律拒;reviewer 在 worker 线程级为 user,任何提供方下一致。批准≠授权,授权仍在 daemon 侧)
- `hyprial agent create` — 注册 agent 记录;`hyprial send --from <四段canonical URI>`
  (裸名派单=回报全丢);`hyprial ack <mid> --from <注册身份URI>`
- `hyprial query <actor> inbox|outbox [--json]` — 人查看某个本机 actor 的收件箱(待处理消息 +
  系统通知,含正文)或发件箱(它发出、仍在排队的消息)。**只读**:不取走、不 ack、不清通知,
  agent 之后照样收到。⛔ 不要拿 MCP 的 harness_read 或 daemon 的 message.pending.list 来"看一眼":
  后者每次调用都会清掉该 actor 的系统通知。
- MCP 会话内:harness_whoami/read/reply/ack/send/targets;
  **回入站消息用 harness_reply(回复+消费一步),新话题才用 hyprial send**
- **每条收到的消息都带 `from` 与 `to`**(harness_read 行、headless worker 收到的正文首行
  `[Harness Network message from <from> to <to> …]`、pi 附着注入都一样)。飞书进来的消息 `from`
  是 adapter(`adapter:lark:<名>`,回复走它),**真正说话的人在 `origin.sender`**:
  `displayName`、`owner`、`standing`。**只有 `standing: verified` 且有 `owner` 才算"这是 <owner> 本人"**;
  `verified` 而 `owner` 为空 = 身份已确认的访客(没有 hyprial 账号),名字可信,但不是任何人的授权;
  `observed`(只有平台显示名,谁都能改成任何名字)、`ambiguous`、`unresolved` 一律 ⛔ 不当作主人的授权,
  正文首行也会写明 `NOT verified`。名字对不上人时,补登记用
  `hyprial adapter identities upsert`(由持有花名册的协调者做)。
- 会话的 harness-bridge MCP/技能面由 `hyprial start` 启动时从 HYPRIAL_HOME 统一注入,
  不依赖启动目录的本地注册;额外 MCP server / skill / 扩展在
  `$HYPRIAL_HOME/plugins/plugins.json` 声明,按 harness 能力注入
  (claude: --mcp-config/--plugin-dir;codex: -c mcp_servers.*;pi: --skill/-e)。

## 深挖入口

- 派单编排:`.agents/skills/hyprial-workflow/SKILL.md`(repo 内)
- E2E 验收:`.agents/skills/hyprial-e2e/SKILL.md`(repo 内)
- 本 skill 与实现有出入时:以 `--help` 和源码为准,并在同一 PR 更新本文件
  (CI 要求每个 PR 显式声明 Skill-Impact,见 .forgejo/workflows)。
