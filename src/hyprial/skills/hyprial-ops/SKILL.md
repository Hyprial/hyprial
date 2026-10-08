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
- 输出约定(所有命令):`--json` 时 stdout 恰好一个 JSON 对象且必带 `ok`;`ok:false` 退出码为 1
  (`doctor` 有 fail 项、`adapter doctor` 缺必需 scope 时也是),参数错误退出 2;进度、提示、确认
  只在 stderr,不要从 stdout 截取设备码或链接。
- `hyprial ps [--json]` — daemon/connectors/interactiveSessions/pendingMessages
- `hyprial log --name <worker> / --actor / --since / --conversation / --correlation-id`
  — daemon 结构化日志查询;原始日志 `~/.hyprial/state/logs/daemon.jsonl`
- `hyprial upgrade` — 普通升级的持久源固定为公网
  `https://github.com/Hyprial/hyprial.git`，与当前安装回执里的来源无关；升级成功后
  自动平滑重启（操作员显式动作，不受 autoUpgrade 开关约束）。每次安装前从同一
  来源抓取解析到的确切 commit，要求其中存在 `uv.lock`，导出 pins 后以
  `uv tool install --constraints` 安装；抓取、锁文件或导出失败都会响亮终止，绝不
  回退到未锁定安装。开发者一次性验证内网 ref 用
  `hyprial upgrade --source forgejo --ref <完整提交或标签>`；该选择不持久化，下一次
  普通升级和自动升级仍回公网。若已装的早期测试版本高于公网版本，普通/自动升级
  都返回 `declinedDowngrade` 并跳过；确需回退时由操作员显式指定公网 `--tag`。
- `hyprial version --json` — 除版本外分别报告 `persistentUpdateSource`（固定公网）、
  `installOrigin`（PEP 610 当前安装来源）和 `dependencyLock`（`match`、带逐包差异的
  `mismatch`，或旧安装/回执不匹配时的 `unknown`）。
- `hyprial config set autoUpgrade true|false` — 自动升级总开关（2026-09-15
  Allen 定案：**缺省 = 关**）。未开启时 daemon 调度器到点只记
  `autoupdate.run.skipped`(reason=disabled)不起子进程，`autoupdate run`
  入口同拦；`hyprial autoupdate status --json` 的 `autoUpgradeEnabled` 读
  出开/关。用户或 agent 要开自动升级就走这条命令，不要手编 settings.json。
- 楔死排障(doctor 超时但进程活着):看 daemon.jsonl 的 `reconcile_overrun`
- `hyprial network expose <port> --target unix:/绝对路径|tcp:127.0.0.1:<port> [--proxy-protocol v2|none]`
  —— 通过 hyprial-tailcat 边车（Tailcat，随 wheel 分发）把本机服务暴露给组织内的设备；默认 PROXY v2，
  首帧带来源地址和对端设备的 nodekey（TLV 0xE0），服务端再经组织目录把 nodekey 映射为用户。
  v2 只允许 unix target（本机任何进程都能连 tcp 回环端口并伪造头）；查不到来访者身份时直接断开，
  不会转给后端。tcp target 必须显式 `--proxy-protocol none`，此时不带任何身份。
  `network unexpose <port>` 删除；`network exposures [--json]` 查看 daemon 持久 desired set；
  `network peer-key <ip:port> [--json]` 只查对端 nodekey。unix target 的父目录必须属于当前用户且
  权限恰为 0700。不要绕过 daemon 直接写 sidecar stdin。
- 组织网络：`hyprial org create <org>` 建组织；`hyprial org invite <org> --user <u>` 生成邀请链接
  （https 主形式，另附可复制的 `hyprial org join '<链接>'`；链接含连接凭据，只发给被邀请人）；
  对方用 `hyprial org join <链接|->` 加入，或在网页上接受后由设备执行 `hyprial org pending`
  拾取（`hyprial login` 成功后会自动拾取一次）。`org list`、`org network [<org>]` 查看；
  `org pending` 只读 Casdoor，不写 `consumedBy`；本机 `org.list` 的 `spaceId` 和
  `leftOrgs` 决定是否已处理。同一 space 不重复加入，主动退出的组织不被旧邀请拉回；
  要重新加入须显式 `org join`，成功后清除本机退出记录。不要为拾取配置 Casdoor 写权限。
  `org leave/remove/delete` 管理成员与组织。设备 key 在 `hyprial login` 时生成；
  `hyprial doctor` 的 `tailcat-sidecar`、`device-key` 两项缺失时为 warn，存在但无效时为 fail。
- `hyprial config set workerProxy.url <url|"">` / `workerProxy.vendors openai,anthropic` / `workerProxy.noProxy <list>`
  —— hyprial 自己的 worker 代理(Allen 2026-09-25 定):列出的模型厂商(默认 openai、anthropic)的 worker 走代理,
  其它厂商(deepseek、智谱、kimi)的 worker **去掉**代理直连;下一个 worker 启动即生效,不用重启 daemon;
  url 置空即清除。没配置时沿用 daemon 自己环境里的代理(含 all_proxy)。⛔ 重启生产 daemon 时不要剥掉代理变量。
- `hyprial config set dispatch.reminder "<text>"` —— `workflow plan` / `workflow run` 附带的派单 tier 提醒
  (Allen 2026-09-28 定:执行节点用 fast、规划设计节点用 strong;只是提醒,不改派单)。
  文本存在 `<HYPRIAL_HOME>/dispatch-policy.json`,文件缺失时用出厂默认;置空字符串即关闭。
  `hyprial dispatch matrix --json` 的 `reminders` 显示当前生效值;文件损坏时 plan/run 照常派单,
  只在 `reminderError` 里报原因,而 `dispatch matrix` 会直接报错,用这条 `config set` 覆盖写即可修复。

### 应用安装（已退役）
- 应用安装体系已于 2026-10-03 退役（Allen 裁决）：catalog、制品下载、manifest 命令挂载均已删除。
- `hyprial install ...` 保留命令名，接受并忽略全部参数与选项（`--yes/--check/--force`），
  统一返回错误码 `COMING_SOON`、消息 "hyprial install is coming soon"、退出码 1；**不会静默成功**。
- 不要再派发 `hyprial install <app>` 或任何已退役的挂载命令；安装能力会在未来版本重新加入。

### GUI 工作空间（独立 product server）
- `hyprial gui` / `hyprial gui start` — 启动捆绑的 `@hyprial/gui` product server（Node 24）；首次 start 会把 wheel 内的 GUI 捆绑物原子解包到 `$HYPRIAL_HOME/apps/gui/`（同版本跳过），无需安装命令。
- 启动须显式配置 transport driver 与可信 principal（`HYPRIAL_GUI_TRANSPORT_DRIVER`、`HYPRIAL_GUI_PRINCIPAL_ID`）；缺失即拒绝，不默认连接 mock 或生产 daemon。真实 backend 的功能验收仍须独立证据。
- `hyprial gui status --json` — 查询独立 GUI 状态；`hyprial gui stop` — 停止归属已核实的 GUI 进程。
- `hyprial gui upgrade [--check|--force]` — 重新解包当前 Hyprial 版本 wheel 附带的 GUI 捆绑物，校验 sha256；若 GUI 在运行，先 stop、解包后按原状态 start；同版本跳过（`--force` 强制重解包）。
- `hyprial upgrade` 获取配套 GUI 组件，但不自动切换或重启已运行的 GUI；随后用 `hyprial gui upgrade --check` 查看待更新组件。
- 先更新 Hyprial CLI 再更新 GUI 包。身份未知时停止操作，禁止直接杀 PID。
- 桌面端是包装同一 server 的 Tauri v2 外壳，不拥有第二份会话、inbox 或 ACK 权威。

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
- `hyprial workflow gc --dry-run [--json]` 只读预览下一次有界回收会删除的 workflow-owned
  worker agent；不带 `--dry-run` 会拒绝。选择规则与 daemon 内独立 PAC GC 服务相同，
  借用 actor（即使名字以 `wf-` 开头）、未关闭图、清理未完成、在线或有 adapter pin
  的 worker 都不会删除；`hyprial doctor` 的 `pac-gc` 项显示上次 pass 与当前 backlog。
- 工作流 YAML format 2 使用 name/nodes/edges/workers/defaults/on_failure。旧 targets/await/retry/report_to 执行格式已退役。
- 节点默认独立临时 worker，显式 worker 键共享上下文，owner 全 URI 显式借用已有 actor。
- 图结束回收所属 worker；借用对象不被接管或停止。完成是显式 flag，不解释回复文本。
- on_failure 为 terminate（默认整图失败回收）、continue（独立分支继续）或 hold（等待负责人处理）。
- 相对 timeout 在派图时确定固定截止，等待依赖计入预算；不自动顺延或重试。报告需显式节点。
- routine format 2 显式选择 mode: scheduled/source。定时模式串行、跳过重叠与错过周期，source 模式按任务 UUID 去重。
- routine 默认创建并持有常驻协调 agent，也可执行工作；rm 回收自己拥有的 actor，pause 不回收。
- `hyprial workflow list [--all]` / `hyprial routine list [--all]`：不加 --json 时输出人读的表格。agent 默认只看到自己派的（或自己拥有的），加 --all 看本节点全部，不需要额外授权；主人在终端里直接运行，默认就能看到全部。--json 每行带 `lastProgressAtMs`（该图最新一条 journal 事件的时间；显式心跳命令 `workflow progress` 尚未提供，计划中，见 #1210）、`currentNode {nodeId,state}`（按依赖顺序第一个未完成的节点）（workflow），以及 `lastDispatchAtMs`、`lastOutcome`（routine）。判断是不是卡住，看这两个字段，别只看 state=running。
- `hyprial worktrees [REPO ...] [--all]`：只读检查本机 Git worktree；目录或其 gitdir 链接已丢失、有未提交文件或有未推送提交都算 leftover。它只为已合并且干净的树建议 remove，只为 git 标记为 prunable 的树建议 prune；默认同时按 origin/HEAD、origin/dev、origin/main 判断是否已合并，不会执行这些命令。
- `hyprial workflow overview show|publish <space>`：PAC 总览页。`publish` 把本机的任务线、Workflow 和 Routine 写成 orgfs 空间里的 `pac-overview/nodes/<节点>.js`（blob，只在内容变化时写），并维护合并所有节点的 `pac-overview/index.html`；它还会拉取其他节点尚未到达的文件、刷新本机 checkout，最后输出可以直接用浏览器打开的 `file://…/index.html`。页面每分钟自动重载。数据只包含运行这条命令的调用方看得到的图，节点输出会脱敏并截断。⚠️ 信任边界：空间里任何有写权限的成员都能往页面上放脚本，所以只发布到所有写入者都可信的空间；`publish` 会开启并保持本机对该空间的只读 checkout（`hyprial fs checkout <space> --disable` 关闭）。
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
- **smolvm 回滚先降状态、后换二进制**：保存过 `executionRuntime` 的机器保持 schema 2；旧版会拒绝整个 daemon restore，连普通 host/Docker 都不会恢复。先用新版停止并清理 smolvm workers，停 daemon 后执行 `hyprial transfer-runtime downgrade-state --output /absolute/new-export --json`；独占锁/残留资源/未完生命周期操作会拒绝。成功导出且降为 schema 1 后才能换旧版，不同时换 owner；agent 身份、home、session、grants 不删除。
- `hyprial start codex --headless --name <name> --smolvm-spec /absolute/runtime.json`
  显式实验运行形态，仅 Darwin arm64 + smolvm 1.19.0 + Codex；只挂选定 P2 home/config/workspace 与绑定 IPC socket。输入 JSON 内容和物料摘要持久化，每次恢复重核；失败不转成 host/Docker。物料由操作者提供，不自动安装。
  崩溃恢复只复用 actor/state/spec 相符且已独占的私有目录，engine 与环境目录须本 uid、0700、非链接；旧实验目录权限不符先保留现场交运维核查。engine 未清完时 owner marker/状态指针必须保留，不能据空的环境对象认定清理完成；禁止全局 stop/prune/kill。
  规格/资源/回滚见 `docs/domains/transfer-smolvm-runtime.md`。启动冒烟不等于已验真实模型、strict resume 或跨机支持；后续验收仍单独执行。Pi/Kimi 的产品准入裁定不变，本实现尚未接通它们。
  P2 DeepSeek 的启动环境缺少有效 agent key 时立即拒绝启动；补齐授权 grant 后显式重启，不靠重连修复，也不回退宿主 key。此处是启动授权检查，不新增运行中撤权保证。
- `hyprial transfer-runtime probe --smolvm /absolute/smolvm --rootfs /absolute/rootfs --resize2fs /absolute/resize2fs --output /private/tmp/at08-1 --json`
  使用显式提供的 smolvm 1.19.0 和本地 Linux rootfs 跑合成文件映射预检，不下载依赖。
  输出目录必须不存在；输出根需足够短以容纳 Unix socket。固定单 VM 2 CPU / 1024 MiB，业务盘和 overlay 各 1 GiB，无网络、真实凭据或 daemon 连接。
  留 stdout/stderr、退出码、权限/哈希、源 rootfs 和宿主目录前后对照及清理结果。
  mapping PASS 仅证明该次合成映射；worker、harness 沙箱、认证及跨机仍为 NOT_RUN。
  实机测试须先有测试资源授权，并经 `contract/e2e/with-machine-lock.py` 串行执行。
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
- **起测试用的隔离 daemon 必须设 `HYPRIAL_NETWORK_ISOLATED=1`**(见 `docs/guides/network-isolation.md`):只允许 loopback
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
- 自动升级(03:17/15:17)固定检查公网源，并与手动升级共用上述 exact-commit
  `uv.lock` 约束路径；缺锁/导出失败不做未锁定安装，高于公网的版本以
  `declinedDowngrade` 跳过。需要升级时它只安装、**不重启**:装好后 daemon 仍跑旧代码,主人会收到"新版本已安装,等待确认后重启"。
  确认切换:`hyprial autoupdate restart [--json]`(主人自己运行,或让任一 agent 代为运行);没有待重启时它什么都不做。
  `hyprial autoupdate status --json` 的 `pendingRestart` 是待重启记录，`pendingRestartState` 表示这条记录现在是否还需要处理：
  `waiting` = 还在跑旧代码，需要重启；`applied` = daemon 在安装之后已经重启过，不用再重启；
  `unverified` = daemon 没有应答，`autoupdate restart` 会重新检查。判断要不要重启看 `pendingRestartState`，
  不要看 `lastRun.restart.awaitingConfirmation`：别的途径重启之后，那个字段会一直停在“等待”。
  不带 `--json` 时输出给人看的摘要。手动 `hyprial upgrade` 仍会直接重启。
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
- daemon 重启时只恢复近期活跃的 agent:空闲超过全局阈值(默认 12h)的会被标成 `idle-suppressed`,
  不再自动拉起;有待处理消息/托管、在 keep-list 里、有当前 PAC 请求、或显式 start 时会被唤醒。
  `hyprial agent restore-threshold <如 12h>` 改全局阈值;`hyprial agent restore-policy <name> active|always|never`
  按 agent 覆盖(`always` 总是恢复,`never` 从不自动恢复)。
- 模型额度耗尽或凭据失效时,agent 会进入持久的 `blocked` 状态(`hyprial ps` 里带原因):
  进程照常恢复,但**投递被挡住**,直到人处理完原因后执行 `hyprial agent unblock <name>`(幂等)。
  主人只收到一次通知;`hyprial doctor` 会列出 blocked 与被抑制的数量和补救命令。⛔ 不要靠重启来解除 blocked。
- `hyprial agent create` — 注册 agent 记录；未传 `--config` 时在该 agent home 的
  `config/` 建立最小显式 C，使 claude/codex/pi 默认启用 agent-home P2；未传
  `--cwd` 时默认使用同一 home 的 `workspace/`。同时绑定 15 分钟一次的默认
  agent-home setup routine。显式 `--config`/`--cwd` 保持其原有含义。确定用途后先用
  `hyprial routine add` 绑定新 routine，或用
  `hyprial routine set <name> <file>` 原地修改；最后一个绑定不能直接 `routine rm`。
  `hyprial agent list --json` 的 `agentsWithoutRoutine` 只读列出尚无绑定的既有 agent。
  managed worker 内的 `hyprial send|reply|ack --from <自身 actor>` 会从环境携带
  daemon session fence；本机人工代发必须用
  `--from user:<owner> --on-behalf-of <actor>`，线上 sender 始终是 user 而不是 actor。
  `hyprial ack <mid> --from <自身 actor>` 只适用于有匹配 session 的 worker。
- `hyprial query <actor> inbox|outbox [--json]` — 人查看某个本机 actor 的收件箱(待处理消息 +
  系统通知,含正文)或发件箱(它发出、仍在排队的消息)。**只读**:不取走、不 ack、不清通知,
  agent 之后照样收到。⛔ 不要拿 MCP 的 harness_read 或 daemon 的 message.pending.list 来"看一眼":
  后者每次调用都会清掉该 actor 的系统通知。
- MCP 会话内:harness_whoami/read/reply/ack/send/targets;
  **回入站消息用 harness_reply(回复+消费一步),新话题才用 hyprial send**
- **发文件/图片到飞书**:`hyprial send --from <URI> --to route:<adapter>:<route> --file <路径> "说明文字"`
  (图片用 `--image`,可重复多个)。adapter 会上传后发成飞书的文件/图片消息,返回里
  `resourceDeliveries[].kind` 为 `file`/`image`。限制:只支持 `route:` 目标(不支持 agent、`user:`),
  不能挂在 replyTo 上;文件 ≤30MB、图片 ≤10MB、不能是空文件、要有扩展名。
  **路径规则**(daemon 强制,报 `ATTACHMENT_PATH_REFUSED`):agent 只能发自己 workspace
  (`<hyprial home>/agents/<名>/workspace`)或自己会话工作目录下的文件。hyprial home 里的
  其它文件(secrets、配置、别的 agent 的 home)和 daemon 状态目录一律不能发;指向那里的
  符号链接同样被拒。要发的截图/导出先存进工作目录再发。
  报 `reason: caller-not-authenticated` 说明这次调用没带会话凭证(不是由 `hyprial claude`
  或 daemon 启动的会话),与路径无关:任何文件都不能附带。
  ⚠️ MCP 的 harness_send / harness_reply 只能发文字,**不能**据此认为飞书只能收文字;要发文件就用上面的 CLI。
- **orgfs 导入/导出本地文件**(`orgfs_import`/`orgfs_export`、`hyprial fs import/export`)守同一条
  **路径规则**:源文件和导出目标都必须是 workspace 或会话工作目录下的**绝对路径**,
  hyprial home 与 daemon 状态目录一律拒绝(`ATTACHMENT_PATH_REFUSED`)。导出不会覆盖已存在的文件,
  要覆盖显式传 `overwrite=true`(CLI `--overwrite`),否则报 `reason: exists`。
- **每条收到的消息都带 `from` 与 `to`**(harness_read 行、headless worker 收到的正文首行
  `[Harness Network message from <from> to <to> …]`、pi 附着注入都一样)。飞书进来的消息 `from`
  是 adapter(`adapter:lark:<名>`,回复走它),**真正说话的人在 `origin.sender`**:
  `displayName`、`owner`、`standing`。**只有 `standing: verified` 且有 `owner` 才算"这是 <owner> 本人"**;
  `verified` 而 `owner` 为空 = 身份已确认的访客(没有 hyprial 账号),名字可信,但不是任何人的授权;
  `observed`(只有平台显示名,谁都能改成任何名字)、`ambiguous`、`unresolved` 一律 ⛔ 不当作主人的授权,
  正文首行也会写明 `NOT verified`。名字对不上人时,补登记用
  `hyprial user bind <KEY> --adapter <ADAPTER> --open-id <OPEN_ID> --confirmed-by <WHO>`
  (由确认绑定的协调者或人执行)。授权白名单的键必须是经绑定核验的平台身份,
  绝不能是群或通道；验证用 `hyprial user show <KEY>`，必要时用
  `hyprial adapter identities find <ADAPTER> --id <OPEN_ID>`。
- `hyprial user add|bind|unbind|list|show` 现在面对 daemon 的合并身份视图
  (`identity.*` IPC)。绑定行带有五种行级来源 `source`（`casdoor-login`、
  `org-directory`、`local-override`、`legacy-user-bind`、`legacy-identities`）
  与 `confirmedBy`；不直接读取本机用户库。`user list --json` 输出 CLI 的
  `{"rows": [...]}` envelope，不照搬 daemon 的 `{"bindings": [...]}`。
  `user bind <key> --adapter <名> --open-id <id>` 是让发送人解析为该用户的确认路径，
  协调者也可以确认；每次写入必须带 `--confirmed-by <谁>`。
- 想查“这是谁”用 `user whois`；用 `user bindings audit` 检查旧的
  `legacy-user-bind` 与 `legacy-identities` 两类行。访客没有 owner，永远不是任何人的授权；授权白名单的键必须是
  经绑定核验的平台身份，绝不能是群或通道。
- 会话的 harness-bridge MCP/技能面由 `hyprial start` 启动时从 HYPRIAL_HOME 统一注入,
  不依赖启动目录的本地注册;额外 MCP server / skill / 扩展在
  `$HYPRIAL_HOME/plugins/plugins.json` 声明,按 harness 能力注入
  (claude: --mcp-config/--plugin-dir;codex: -c mcp_servers.*;pi: --skill/-e)。

## 深挖入口

- 派单编排:`.agents/skills/hyprial-workflow/SKILL.md`(repo 内)
- E2E 验收:`.agents/skills/hyprial-e2e/SKILL.md`(repo 内)
- 本 skill 与实现有出入时:以 `--help` 和源码为准,并在同一 PR 更新本文件
  (CI 要求每个 PR 显式声明 Skill-Impact,见 .forgejo/workflows)。
