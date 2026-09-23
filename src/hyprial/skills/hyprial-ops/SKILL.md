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

### PAC workflow(声明式派单+跟踪,不执行任务)
- `hyprial workflow plan/run/status/list/cancel`;run 需 `--from <canonical-uri> --yes`
- yaml 骨架:version/name/task/targets[{name,task}]/await{kind,timeout,match}/
  on_timeout{action,max_attempts,backoff,escalate_to}/report_to/limits
- target 一律用逻辑名(nickname),不用含 node 的全 URI
- ⚠️ tick 负载:每个 running run 的未决 target 会进 daemon reconcile 周期;
  大量挂着的 running run 会拖垮 IPC。派发后确认 daemon.jsonl 无
  `reconcile_overrun`;完结的 run 别让它挂在 running。

### PAC v2 图与公共订阅(集成分支增量,不退役 v1)
- `hyprial pac graph create/add-node/add-edge/show` — 先编辑 draft 图;
  `hyprial pac graph activate <graph>` 由本机 graph owner 激活,此后结构冻结,改结构需建新图。
  owner 优先 HYPRIAL_OWNER,否则读取统一 home(HYPRIAL_HOME 或默认 HOME/.hyprial)的 settings.json;
  activate/close 不要求额外设置 HYPRIAL_HOME。
  `hyprial pac graph close <graph>` 由本机 owner 单调关闭 task/clock run;不改 flag、不造 completion,
  不再派发/重发剩余通知。已经在途的真实回执仍会记录,但不会重新打开任务。
- `hyprial pac flag set/reset <graph> <node> [--actor <principal URI>]` — 激活后显式改 flag;
  `--actor` 只可回声已验证身份(本机人为 `user:<owner>`,受管 worker 由载体注入绑定,
  省略即以已验证身份行事);owner 段精确相等,不以短名或 owner 段放行。
  通知送达不等于任务完成。`hyprial pac notify resend <graph>` 重试未确认的通知。
  升级报告:`hyprial pac migration status` 列出 schema-8 改写/保留的 owner。
- 外部消费者唯一受支持的增量合同:
  `hyprial pac events <graph> --snapshot --json` 取同一读事务的 snapshot@cursor;
  `hyprial pac events <graph> --after <cursor> --journal-id <journalId> [--follow] --json`
  取严格 `seq > cursor` 的 typed journal JSON lines。`--snapshot --follow` 可无缝交接。
  事件 version 可是历史决策版本,迟到回执不得降低 snapshot 的结构 version;
  结构失效时重取快照,低于真实迁移 floor 的游标必须 resync。
  follow 可用 SIGINT/SIGTERM 或关闭输出管道结束(含 idle/父进程忽略 SIGINT 的环境);
  stdin=/dev/null 不影响订阅,不靠发送假事件/心跳来检测退出。
- 游标是 seq,允许跨图跳号;消费端忽略重复 seq。`PAC_EVENTS_RESYNC` 及结构失效事件
  要重新取快照;流式错误只在 stderr。不要 import 内部 PAC 类或直接读 SQLite。
- 快照保留 flags/refs、当前 assignments 与通知投递状态。无实际通知的根节点不伪造 assignment;
  back 的下一轮请求即使旧 flag 仍为 true 也可处于待办状态。
- `hyprial pac context <graph> <node> --json` 读取工作 briefRef 和前驱 flag/reasonRef/setBy/setAt,
  所有字段来自同一 cursor。`completedCount` 是已完成 set 次数,`currentActivation` 是当前请求,
  back 请求可为 round=2 而 count=1。不会读引用正文或 inbox。
- deadlineMs 仅出现在 clock 自身的 context,不借给下游 task。无 actor 节点时 `actor` 字段缺席,
  不输出 null/空对象/伪造 actor 信息。actor 起停/对账、end 自动 close 和 v1 真派单迁移仍待第二波。
- `contract/pac-events/README.md`、`contract/pac-context/README.md` 是公共合同;
  `client.py` 是独立 CLI JSON 消费样例。旧 graph-only/--for context 及旧诊断命令路径已移除。
  clock/restate 仅在隐藏的 `pac debug` 内供诊断,不属于公开命令面或常驻 v2 tick 实现。

### agent / worker / 消息
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
- 会话的 harness-bridge MCP/技能面由 `hyprial start` 启动时从 HYPRIAL_HOME 统一注入,
  不依赖启动目录的本地注册;额外 MCP server / skill / 扩展在
  `$HYPRIAL_HOME/plugins/plugins.json` 声明,按 harness 能力注入
  (claude: --mcp-config/--plugin-dir;codex: -c mcp_servers.*;pi: --skill/-e)。

## 深挖入口

- 派单编排:`.agents/skills/hyprial-workflow/SKILL.md`(repo 内)
- E2E 验收:`.agents/skills/hyprial-e2e/SKILL.md`(repo 内)
- 本 skill 与实现有出入时:以 `--help` 和源码为准,并在同一 PR 更新本文件
  (CI 要求每个 PR 显式声明 Skill-Impact,见 .forgejo/workflows)。
