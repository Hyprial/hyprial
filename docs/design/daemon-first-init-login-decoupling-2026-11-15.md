# daemon 先行：init 与 login 解耦，daemon 是唯一常驻后端

- 日期：2026-11-15
- 目标分支：`dev`
- 调研基线：`f1a4caf86`（`forgejo/dev-g`，#2824 删除 pykka 后端）。前置调研读的是 `9932b2b63`；
  本文引用的源码、测试与契约文件在两者之间逐字节相同，行号已在 `f1a4caf86` 上逐条重核
  （修正见 2.7）。
- 状态：方向已由 owner（Allen）2026-11-15 确认；本文是 Z-0（设计文档）。2026-11-19：Z-0 至 Z-8 已实现并合入
  `dev`，Z-9 是文档收尾 PR，PR 号见第 6 节的“实施状态”。以下各节保持写作时（2026-11-15）的原文，
  现状以代码与 `docs/guides/gui-dev-mode.md` 为准。
- 证据标记：**实测**（调研时在隔离 home 中运行得到）、**读代码**（读源码得到，未运行）、
  **估算**、**UNKNOWN**（未知）、**BLOCKED**（本环境做不到：真实 OIDC、launchd、macOS）。
  路径相对仓库根；`S/` = `src/hyprial/`。
- 前置调研与原型脚本不在仓库内。本文自含：第 2 节是事实与证据，第 4 节足以重建两份原型。
  事实编号 T/G/Y/V 沿用前置调研，仅在此处出现，不再引用外部文件。

## 1. 目标与 owner 决定

### 1.1 目标模型

owner 的原话（2026-11-15）归纳为一个模型：

- 装好 hyprial 之后，像 `brew services start hyprial` 那样启动；否则任意 hyprial 命令第一次运行时
  注册并启动一个 launchd 服务，之后的所有操作都以“这个服务正在运行”为前提。
- CLI 与 GUI 都依赖 daemon request，由已经启动的 daemon 应答；两者都只是客户端。
- `init` 与 `login` 解耦：服务在没有 owner 时也能启动；`init` 只负责注册并启动服务；
  登录是 CLI、GUI 同样发给运行中服务的一个请求。
- OS 服务支持任意 `HYPRIAL_HOME`（单元本来就携带它；每个 home 一个 label）。

### 1.2 已决定（owner，2026-11-15）

| # | 决定 |
|---|---|
| D1 | 方向：daemon 先行，`init` 与 `login` 解耦，服务无 owner 即可启动，登录是对运行中服务的请求 |
| D2 | OS 服务支持任意 `HYPRIAL_HOME`，每个 home 一个 label/unit |
| D3 | 关闭 GUI/app 窗口时 daemon 继续运行；另提供独立的“停止后台服务”动作（不在“退出 app”里隐式发生） |
| D4 | 首个 app 发布用 detached daemon 进程；每 home 的 OS 服务随后跟进 |
| D5 | 运行中的 daemon 若是另一个包版本，自动重启到发起启动的那份安装上（DL-3） |
| D6 | 原生 app（bundle id `com.hyprial.hyprialos`，显示名 “HyprialOS”）使用自己的 `HYPRIAL_HOME`，在网络里是一个独立节点 |
| D7 | wheel 用户与 app 用户行为一致（同一个 ensure，不加开关） |
| D8 | **不做** DL-2（进程内 promotion 之后的 hand-over）。DL-5b 的“clean exit 不重启”策略同样不做 |
| D9 | 保留过渡步骤 DL-1：`hyprial gui` 在准入前对已初始化的 home 确保 daemon |

D5 对应前置调研的 OD-3，D3 对应 OD-8，D6 对应 OD-7 的默认做法；这三项的“默认”现在已是 owner 决定，
但 OD-3、OD-7 的具体实现细节仍列在第 9 节。

### 1.3 需求 R1–R11

| # | 需求 |
|---|---|
| R1 | 每个 home 最多一个常驻后端进程：该 home 的 `hyprial daemon run`。没有别的进程长期持有 `state/daemon.lock` 或 home 的 `.identity-transaction.lock` |
| R2 | 该进程能在没有 owner 的 home（以及不存在的 home，由它创建）上启动并保持运行 |
| R3 | 登录、设备准备、initialize、logout 在任何状态下都是 daemon 请求；前端只展示验证 URL 与 code，可以打开浏览器 |
| R4 | logout 之后服务仍在运行并接受下一次登录 |
| R5 | daemon 由 OS 服务管理器启动；没有单元时由第一条需要它的 `hyprial` 命令启动并（在支持且被允许时）注册服务。之后的命令以其运行为前提 |
| R6 | 服务按 home 注册；非默认 home 有自己的单元并携带自己的 `HYPRIAL_HOME`，两个 home 不冲突 |
| R7 | CLI 命令由已运行的 daemon 应答；CLI 进程不做业务、不取 daemon 锁 |
| R8 | GUI 后端链（session host → `request_http` → daemon）在任何状态下都是纯客户端；关 GUI 永不停 daemon |
| R9 | 显式生命周期命令保持不自启：`daemon status`、`daemon stop`、`daemon service uninstall`、`doctor`、`--help`、`--version` |
| R10 | 测试与 CI 永不注册或启动真实用户服务，并能关闭隐式启动 |
| R11 | `init` 与 `login` 解耦：`init` 创建 home、注册服务、启动它，在没有 owner 的 home 上也成功，且从不运行登录 |

## 2. 现状与实测

### 2.1 为什么进程今天与 owner 耦合（读代码）

- `resolve_node_owner` 在 `HYPRIAL_OWNER` 与 `settings.json` 都没有 owner 时抛出
  “user identity is not set … Run: hyprial login”（`S/identity/impl/onboarding/identity_record.py:86-134`，
  抛出点 `:126-133`）。
- owner 被铸进每个地址 `agent:<owner>:<machine>:<name>`（`S/kernel/impl/primitives/uri.py:3,149`）。
  owner 段是用户身份，**同一个用户的所有机器上是同一个值**，`<machine>` 才负责区分节点。
  因此“没有本地登录名回退”是刻意的：以前回退到 `getpass.getuser()` 会让 owner 变成机器属性
  而不是人的属性（理由写在 `identity_record.py:111-123`）。本设计不改这条规则。
- `DaemonApplication.__init__` 在没有显式 owner 时调用 `resolve_node_owner`
  （`S/daemon/impl/application/__init__.py:314-317`）；`daemon run` 经
  `DaemonApplication.from_environment`（`S/daemon/impl/application/__init__.py:933`；调用点
  `S/shell/impl/cli/commands/daemon/commands.py:363`）构造它。此前还有
  `services.require_initialized_hyprial_home()`（`commands.py:305`）。于是整个进程继承了
  “必须有 owner”这个要求。
- 实际只有业务函数（地址、inbox、路由、mesh）需要 owner。进程、锁、socket 与 onboarding 方法不需要。
  本设计保持“没有 owner 就没有业务功能”与“没有本地登录回退”不变，只把边界从
  “进程无法启动”移到“业务 application 在 owner 出现之前不构造”。
- 实测（T13）：在无 owner 的隔离 home 上 `hyprial daemon start` 5.4 s 后返回
  `DAEMON_START_FAILED`，启动日志是上述 identity 文案。

### 2.2 终端路径（读代码为主，标注实测处）

| # | 事实 | 标记 | 证据 |
|---|---|---|---|
| T1 | 命令：`login`、`logout`、`init`、`daemon status\|start\|run\|stop\|restart`、`daemon service status\|install\|uninstall`、`daemon upgrade …`、`doctor`、`gui`。没有 `up` | 实测、读代码 | `hyprial --help`；`docs/reference/cli-command-inventory.json` |
| T2 | `init`：建 home → org-context → 探测 daemon（0.5 s）→ 无 owner 则**在 CLI 进程内**跑登录 → `resolve_node_owner()` → 启动 daemon → 最后安装用户服务 | 读代码 | `S/shell/impl/cli/commands/init/init.py:95-216`（登录分支 `:129-157`）、`:300-363` |
| T3 | `init` 默认安装服务（`requested = True if service is None else service`）；安装失败非致命，平台不支持则跳过 | 读代码 | `init.py:331-358` |
| T4 | 无 owner 的 home 上 `login` 即首次设置：提交身份，再调同一个 `_complete_initialization` 启动 daemon；不安装服务 | 读代码 | `S/shell/impl/cli/commands/login/commands.py:155-212`（`:205`） |
| T5 | 唯一启动原语 `_launch_daemon_process`：有可归属的服务单元则请求管理器启动；否则 `[sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"]` + `start_new_session=True`，在独占的 `state/daemon-launch.lock` 下重新探测，保证只产生一代。调用者：`daemon start`、`init`、`login`、升级重启 | 读代码 | `S/shell/impl/cli/commands/common/daemon_start.py:339-382`、`:413-429`、`:499-507`；调用点 `daemon/commands.py:94`、`init.py:175`、`login/flow.py:536`、`common/pending_restart.py:336` |
| T6 | 无服务管理器时 daemon 以 detached 进程运行：已初始化隔离 home 上 `daemon start` 5.2 s 到 serving；进程 PPID 1、自有 session id；再次 `daemon start` 返回 `alreadyRunning: true`、同一 pid | 实测 | 隔离运行 |
| T7 | `daemon start` 只需要 home 有 owner，不需要当前进程里跑过 `init`/`login` | 实测 | 隔离 home 只含 `settings.json` |
| T8 | 服务身份：launchd label `com.hyprial.hyprial.daemon`、`RunAtLoad`、`KeepAlive`、`ThrottleInterval 5`；systemd `~/.config/systemd/user/hyprial.service`、`Restart=always`、`RestartSec=5`。单元环境携带 `HYPRIAL_HOME`、`HARNESS_STATE_DIR`、`HYPRIAL_SERVICE_MANAGED=1` | 读代码 | `S/daemon/impl/operations/service.py:41,219-228`；`S/daemon/impl/services/launchd.plist.template:19`、`systemd.service.template:7-18` |
| T9 | 在管理器下单元任何退出都重启；连续启动失败计入 `state/daemon-start-failures.json`，服务托管的 daemon 在退出前等待 5 s → 60 s（翻倍）；没有管理器时没人重启 | 读代码 | `S/daemon/impl/application/start_backoff.py:22-25,32-33,68-78`；`S/daemon/impl/application/startup.py:408-441` |
| T10 | 有单元时 start/stop/restart 走管理器（`kickstart`/`bootstrap`/`bootout`、`systemctl --user`）；避免 CLI spawn 与 `KeepAlive` 竞争。KP-005 记录过游离 daemon | 读代码 | `S/shell/impl/cli/commands/daemon/service.py:420-470,519-571`；`docs/reference/known-problems.md` KP-005 |
| T11 | 所有权：`state/daemon.lock`（`DaemonStateOwnershipFence`，进程文件锁）+ home 的 `.identity-transaction.lock`；监听器是 unix socket `state/daemon.sock`。实测状态目录 `0700`、socket `srw-------`、`daemon.json` 与锁 `0600` | 读代码、实测 | `S/daemon/impl/configuration/ownership.py:17-52`；`commands.py:334-377` |
| T12 | CLI 业务命令经 `daemon_request_once` 连一次、发一帧、收一个响应，**从不自动启动**。无 daemon：`agent list --json` → `{"ok":false,"code":"DAEMON_UNAVAILABLE", …}` exit 1；`daemon status` → `{"ok":true,"daemon":{"running":false}}` exit 0 | 实测、读代码 | `S/kernel/impl/ipc/client.py:203-239`；`S/shell/impl/cli/commands/common/support.py:176-222` |
| T13 | 无 owner 的 home 上 `daemon start` 失败（见 2.1） | 实测 | 隔离运行 |
| T14 | `daemon run --bootstrap` 存在：一个进程冷启动无 owner 的 home，在终端里登录，收到 initialize 后同 PID 变成 daemon。它**没有 socket 也没有 HTTP** | 读代码 | `commands.py:247-252,282-304`；`S/shell/impl/daemon_wiring/onboarding/launch.py:85-203`；`S/daemon/impl/onboarding/runtime.py:1-16` |
| T15 | 自动升级由 daemon 持有（本机 03:17/15:17）；旧 OS 定时器只被升级步骤退役。`autoUpgrade` 默认关；升级安装后记录待重启，`daemon upgrade apply` 重启 | 读代码 | `S/daemon/impl/autoupdate/__init__.py:1-7`；`S/daemon/impl/updates/installation.py` |

### 2.3 GUI 路径

| # | 事实 | 标记 | 证据 |
|---|---|---|---|
| G1 | 准入：无 owner 的 home → `phase="bootstrap"`，不检查 daemon；有 owner 的 home → `verify_ready_daemon`（ping + catalog、身份、版本、epoch、catalog 完整）。准入文档声明“不 spawn、不写状态” | 读代码、实测 | `S/shell/impl/gui/backend/admission.py:212-232,243-312`；实测 `ADMIT bootstrap None` / `ADMIT serving <owner>` |
| G2 | 无 owner：`request_http` 子进程构造 `GuiRuntimeHost`，其中 `BootstrapRuntime.admit` 取两把锁并写 `state/bootstrap.json`（`phase: awaiting-auth`，子进程 pid）。`/readyz` 200，`onboarding.status` → `phase: bootstrap`，`agent.list` → 404 `METHOD_NOT_FOUND` | 实测、读代码 | `S/daemon/impl/application/onboarding/gui_host.py:200-241` |
| G3 | initialize → 进程内 promotion：回复刷出、bootstrap 监听器排空，`run_promoted` 在子进程内构造真正的 `DaemonApplication` 并交接两把锁流；`daemon.ready` 后发布新监听器（新端口、新 bearer、新 generation）。实测 1.1 s；发布 pid == 子进程 pid；`daemon.json` pid == 子进程 pid | 实测、读代码 | `S/shell/impl/request_http/bootstrap/gui.py:59-131`；`gui_host.py:347-408`；`runtime.py:619-687` |
| G4 | promotion 不安装任何服务单元（实测：promotion 后与退出后 `Library/LaunchAgents`、`.config/systemd/user` 都为空）；`serve_gui`/`run_promoted` 没有路径到达 `ServiceManager` | 实测、读代码 | 隔离运行；grep |
| G5 | promotion 出来的 daemon **不**比 GUI 活得久：SIGTERM 子进程 → exit 0，`daemon.sock` 与 `daemon.json` 消失。嵌入式 daemon 在仍有 owner 时停止，`serve_gui` 离开循环、子进程退出 | 实测、读代码 | `bootstrap/gui.py:115-116` |
| G6 | 同一 home 第二次启动：`admit_source` → `REFUSED GUI_DAEMON_UNAVAILABLE the selected daemon is not reachable and ready`。真实 CLI 在已初始化且无 daemon 的 home 上：`{"ok":false,"code":"GUI_DAEMON_UNAVAILABLE","error":"the selected daemon is not reachable and ready","data":{}}`，exit 1 | 实测 | `admission.py:335-342` |
| G7 | 已初始化且 daemon 在跑：serving 子进程不持日志、不持锁，每个方法是对 home socket 的一次 `daemon_request_once`；`agent.list` 经子进程 200；停子进程 daemon 保持（同 pid、同 epoch） | 实测、读代码 | `gui_host.py:262-286` |
| G8 | “借用的 daemon”：logout 由 daemon 持有，它自己停止；子进程看到 owner 消失，关监听器，等 daemon 的锁排空（`reopen_after_logout`，不停也不抢），重开 bootstrap host。对独立 daemon 实测：`loggedOut: true`，1.0 s 后新发布，`onboarding.status` → `bootstrap`，`daemon status` → `running: false` | 实测、读代码 | `bootstrap/gui.py:46-56,117-122`；`S/daemon/impl/application/onboarding/account_lifecycle.py:482-500,607-617` |
| G9 | 经 GUI 初始化、之后由**独立** daemon（`daemon start`）服务的 home，经新附着的子进程报告 `initialized: true, deviceReady: true, nextActions: []`：持久的 initialize 回执跨进程存活 | 实测 | 隔离运行 |
| G10 | 无 daemon 时启动的 serving 子进程仍能启动（`/readyz` 200，来自本地投影），应答 503 `DAEMON_UNAVAILABLE`；daemon 出现后应答 503 `CATALOG_STALE`“daemon contract changed; restart the request-http process”。子进程在启动时固定 catalog 与 epoch | 实测、读代码 | `S/shell/impl/request_http/server.py:427-438` |
| G11 | 启动器有 `GuiSession.replace_backend()` 但无人调用，GUI 也不对 `CATALOG_STALE` 反应；因此 daemon 重启后已打开的 GUI 在任何状态下都不会恢复 | 读代码 | `S/shell/impl/gui/backend/lifecycle/session.py:96`；grep |
| G12 | bootstrap host 持有无 owner 的 home 时，`daemon start` 等 30 s，报 `DAEMON_START_TIMEOUT`，并留下一个阻塞在 identity 锁上的 `daemon run`，直到 bootstrap host 退出 | 实测 | 隔离运行 |
| G13 | 有 owner 但不是经 GUI 初始化的 home 报告 `initialized: false, nextActions: ["device.ensure"]`（伪造 home，无设备记录）。真实 CLI `login` 的 home 报告什么：UNKNOWN；KP-059 记录相关缺口 | 实测（伪造）、UNKNOWN（真实） | `account_lifecycle.py:138-153` |

### 2.4 `hyprial gui` 在 home 三种状态下的实测表

| 状态 | 准入 | 后端子进程 | 今天的结果 | 标记 |
|---|---|---|---|---|
| 1. 无 owner 的 home | `bootstrap`，owner 与 principal 为 `None`，不检查 daemon | 托管 `BootstrapRuntime`，持两把锁；服务 login 与 initialize；收到 initialize 后同 PID 变成 daemon（1.1 s），新端口/bearer/generation | 可用；**daemon 与子进程同生共死**；没有服务单元 | 实测（后端与准入；身份提交为受控端口）；启动器/Node 半段 UNKNOWN |
| 2. 已初始化，daemon 在跑（同版本） | `serving`，principal `user:<owner>` | 经 socket 转发所有方法；logout 令 daemon 自停，子进程 1.0 s 内重开 bootstrap | 可用；停 GUI 子进程不影响 daemon | 实测（后端与准入）；启动器/Node 半段 UNKNOWN |
| 3. 已初始化，无 daemon | **拒绝**：`GUI_DAEMON_UNAVAILABLE`，exit 1 | 不启动 | 拒绝；这是“经 GUI 初始化且没有终端”的用户**之后每一次**启动的结果 | 实测（真实 `hyprial gui --no-open --json`） |

`hyprial gui` 命令本身不调用启动原语（`S/shell/impl/cli/commands/gui/commands.py:27-120`；
`_launch_daemon_process` 仅由 `daemon start`、`init`、`login`、升级重启调用）。
无 owner 的 home 上准入“不检查 daemon”的分支在 `admission.py:212-218`。

### 2.5 两个相关事实

- `nativeHost.promotion=external` / `serve_external` 不是“附着到外部 daemon”的机制：它仍在同一进程内
  promotion，只是前台循环的归属不同；树内已没有任何代码写出 `nativeHost` 块，只剩解析器与测试
  （`S/shell/impl/request_http/bootstrap/external.py` 270 行，`tests/shell/request_http/launch/test_external_promotion.py` 433 行）。读代码。
- OS 服务今天无法用于有自己 home 的 app：`ServiceManager` 拒绝非 `$HOME/.hyprial` 的 home
  （`service.py:588-593`，reason `service-home-nondefault`），label 与 unit 名是常量
  （`service.py:41` 及其余 10 处使用；systemd 名 `hyprial.service` 写在 `service.py:229`、
  `S/shell/impl/cli/commands/daemon/service.py:468,577` 与模板第 15 行）。读代码。

### 2.6 版本匹配与环境事实

| # | 事实 | 标记 | 证据 |
|---|---|---|---|
| V1 | 版本匹配在 GUI 路径两处强制：启动器准入（`ping.version` 与 catalog `packageVersion` 必须等于本包 `__version__`，`GUI_DAEMON_IDENTITY`/`GUI_DAEMON_CATALOG`）与 `request_http` 子进程启动（`VERSION_MISMATCH`） | 读代码 | `admission.py:253-291,359-386`；`server.py:225-232` |
| V3 | 普通 CLI 没有包版本门，IPC 客户端只检查 IPC 协议版本 | 读代码 | `client.py:275-277` |
| V4 | 终端升级：`uv tool install` 一个精确 tag，再经同一启动/管理器路径把 daemon 重启到新安装；拒绝 editable 安装与 uv tool 目录之外的解释器 | 读代码 | `S/shell/impl/cli/commands/upgrade/commands.py` |
| V5 | app 更新时旧 daemon 仍在跑：没有任何东西重启它；更新后的 `hyprial gui` 会被 V1 拒绝；detached daemon 仍执行被替换的文件。未实测 | 读代码、UNKNOWN | V1、T5 |
| Y7 | 两个 home 之间 socket（`<state>/daemon.sock`）、启动锁、OS 自动升级定时器都不冲突；没有固定监听端口（Zenoh 端点不持久化，默认不设） | 读代码、实测 | T11、T15 |
| Y8 | 节点 id 默认是主机名：同一台机器上两个 home 登录同一账号是两个 daemon，`owner` 与 `nodeId` 相同；对 mesh 的影响 UNKNOWN | 读代码、UNKNOWN | `S/kernel/impl/primitives/node_identity.py` |
| Y9 | macOS AF_UNIX 路径上限 104 字节；`~/Library/Application Support/com.hyprial.hyprialos/home/state/daemon.sock` 约 73 字节加用户名；KP-059 记录过 110 字节的真实失败 | 读代码 | KP-059 |
| Y10 | Windows 没有服务支持（`ServicePlatformUnsupported`）；GUI 源码运行时需要 `fcntl` 与 `os.getuid`。daemon 驱动的 `hyprial gui` 在 Windows 上能否运行：UNKNOWN | 读代码、UNKNOWN | `S/shell/impl/gui/backend/platform_support.py` |
| Y11 | 没有 systemd 用户会话的 Linux：安装失败，`init` 报 `service-install-failed` 并保留 detached daemon | 读代码 | `init.py:348-358` |
| Y13 | macOS App Translocation：被隔离的 app 从落地处直接运行时，会从随机只读路径运行；公证不豁免。在 translocation 期间记录 bundle 路径的 plist 或 detached 进程之后会指向不存在的路径。对我们 bundle 的确切行为：UNKNOWN | 文档、UNKNOWN | Apple 文档 |
| Y14 | 从 GUI app 启动的 daemon 继承 app 的环境（`PATH=/usr/bin:/bin:/usr/sbin:/sbin`）；服务模板设置更宽的 `PATH`。detached daemon 是否找得到 harness CLI：UNKNOWN | 读代码、UNKNOWN | `service.py:101-109` |

### 2.7 引用核对（相对前置调研的修正）

前置调研声称其引用在 `9932b2b63` 与 `f1a4caf86` 间字节相同；该点已用 `git diff` 复核成立。
但若干行号本身不准，已在本文改正，无法确认者标 UNKNOWN：

- `identity_record.py`：抛出点是 `:126-133`（返回在 `:134`）；“无回退”的理由在 `:111-123`，不是 `:109-124`。
- 要求 owner 的是 `DaemonApplication.__init__`（`application/__init__.py:314-317`），`from_environment` 只是调用它的工厂，不是解析点。
- bootstrap 分派（Z-1 抽出后）：准入在 `S/daemon/impl/application/onboarding/bootstrap/dispatcher.py:153-196`（`GuiRuntimeHost` 的调用点是 `gui_host.py:197-240`）；bootstrap 可调用集合是 `dispatcher.py:57-67`，只有 **7** 个方法，第 8 个 `onboarding.initialize` 仅在 `enable_promotion` 之后才加入（`dispatcher.py:227-230`、`:298-`）；“8 个方法”是 7 + initialize。
- `service.py` 中 `LAUNCHD_LABEL` 共 **11** 处（不是 12）；`S/shell/impl/cli/commands/daemon/service.py:468,577` 用的是字面量 `hyprial.service`，不是常量。
- `tests/support/runtime/environment.py`：两个单元路径在 `:63-64`（不是 `:65-66`）。
- 设备密钥函数 `_ensure_login_device` 在 `S/shell/impl/cli/commands/login/flow.py:829`，不在 `S/shell/impl/login/flow.py`（那个文件只有 859 行，含 `webbrowser.open` `:408` 与 `open_browser` 参数 `:434`）。
- 前置调研称“无 owner 的启动曾是失败，会喂 `state/daemon-start-failures.json`”：失败计数发生在 `DaemonApplication` 的启动阶段方法内（`startup.py:408-441`），而无 owner 的失败发生在构造函数/`require_initialized_hyprial_home` 里，在它之前。该计数是否覆盖这种失败：Z-1 已实测——**不覆盖**。隔离的无 owner home 上、`HYPRIAL_SERVICE_MANAGED=1` 时 `daemon run --json` 随即以 exit 1 退出，`state/` 下只有 `daemon.lock`，没有 `daemon-start-failures.json`，也就没有 5 s → 60 s 的退避；`KeepAlive` 循环因此是无节流的每 5 s 一次（风险成立，见 3.7）。
- `ping.phase` 的值（`restoring`/`serving`/`reconciled`）在 `S/daemon/impl/ipc/router.py:321-356`；两个 bootstrap host 都回答 `phase: "bootstrap"`（`bootstrap/dispatcher.py:232-240`、`S/daemon/impl/application/onboarding/http.py:46-47`）。
- 其余引用（`daemon/commands.py:305,363,372`、`support.py:176-222`、`daemon_start.py:339-382,413-429,499-507`、`client.py:160-239`、`server.py:131-148`、`params.py:52-72`、`method_spec.py:76-80,149-179`、`specs.py:49-66`、`request_http/catalog.py` 与 `server.py` 的各区间、`launchd_jobs.py:79-93`、`start_backoff.py`）已逐条核对，范围成立。

## 3. 目标设计

### 3.1 进程模型

```
  launchd LaunchAgent / systemd user unit，每个 home 一个        （或：同一启动原语的 detached
  label com.hyprial.hyprial.daemon[.<id>] / hyprial[-<id>].service   spawn，T5）
          │ RunAtLoad / KeepAlive / Restart=always
          ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ hyprial daemon run            每个 home 一个进程                              │
  │   整个生命周期持有 state/daemon.lock + home/.identity-transaction.lock          │
  │                                                                              │
  │   phase bootstrap : BootstrapRuntime + bootstrap 监听器                       │
  │                     ping、daemon.catalog、shutdown、8 个 onboarding 方法        │
  │   phase restoring/serving/reconciled : DaemonApplication，完整 catalog         │
  └───────────────▲──────────────────────────────────────────────────────────────┘
                  │ state/daemon.sock   （AF_UNIX，0600，state 目录 0700，同 uid）
      ┌───────────┼──────────────────────────┬─────────────────────┐
      │           │                          │                     │
  hyprial <cmd>   request_http 子进程         MCP / adapters /      doctor、daemon status
  ensure → 一次    （同一份 MethodSpec 的      workers（不变）        （永不 ensure）
  请求             HTTP 投影，bearer）
                        ▲ loopback HTTP + bearer
                  Python session host  ◄── 浏览器 / 原生壳（HyprialOS，自己的 home）
```

### 3.2 daemon 的阶段

```
            启动（home 无 owner）                      启动（home 有 owner）
                     │                                          │
                     ▼        onboarding.initialize             ▼
   ┌────────────► bootstrap ───────────────────────────►  restoring → serving → reconciled
   │   awaiting-auth → authenticating → authenticated          │
   │                        → promoting                        │ auth.logout（回执已持久、
   │                                                           │ 回复已写出、application 停止）
   └───────────────────────────────────────────────────────────┘
        同一个 PID；若进程改为退出，下次启动落在 bootstrap
```

- `bootstrap` 已经是现有取值，只是获得一个 socket，不是新概念：`ping.phase` 已有 `restoring/serving/reconciled`
  （`router.py:321-356`），两个 bootstrap host 已回答 `phase: "bootstrap"`；`METHOD_PHASES = {"bootstrap", "serving"}`
  与逐方法 `(phase, caller)` 对已在方法声明里（`S/kernel/impl/contracts/daemon/method_spec.py:76-80,149-179`；
  `S/daemon/impl/request_catalog/onboarding/specs.py:49-66`）。bootstrap 的 `ping` 需要放宽到启动代码读取的字段：`_daemon_probe` 的结果由 `_probe_reports_running` 读取 `running`
  （`S/shell/impl/cli/commands/common/support.py:396-436`），其余字段已由 Z-1 核对（读取 `ping` 的全部调用点）：`running`（`_probe_reports_running`、`DaemonLaunchResult`、GUI 准入）、`pid`（`daemon status`、锁等待、服务关停计划、重启身份）、`epoch`（`ping` 与 catalog 同 epoch）、`version`（升级、GUI 准入）、`nodeId`、`owner`、`socket`（GUI 准入核对路径）、`phase`；`restorePending` 缺省按 `true` 读，所以 bootstrap 回答 `false`；`zenoh`、`identityMode`、`identityIssuer`、`storageHealth` 缺省都被容忍。线上 bootstrap 回答 `phase: "bootstrap"`、`owner: null` 并带上述字段（实现：`S/daemon/impl/application/onboarding/bootstrap/`）。
- bootstrap 阶段进程持有的状态：两把持续锁（`S/daemon/impl/onboarding/locks.py`）、`state/bootstrap.json`
  （`pid`、`birthIdentity`、`epoch`、`socket`、`phase`；原型实测）、`state/onboarding/` 下的 onboarding 操作日志
  （`runtime.py:116-148`）、登录进行中的 identity worker（`runtime.py:263-354`）。
- promotion 不变：`accept_initialize(defer_quiesce=True)` 布置回复后动作，监听器在回复完整写出后关闭，
  `run_daemon` 构造 application 并交接两个锁流，`complete_initialize` 在 `daemon.ready` 运行
  （`runtime.py:412-488,619-687`；`gui_host.py:300-365`）。`runtime.py:1-16` 与 `:251-261` 已把“wire host”描述为这些钩子的调用者。
- 只属于 `request_http` 子进程、**不**带过来的部分：触发 post-reply 钩子的 HTTP 响应子类
  （`S/shell/impl/request_http/bootstrap/response.py`，73 行）、监听器与 bearer 轮换
  （`bootstrap/gui.py:24-28,82-116`）、GUI control-session 表面（`GuiRuntimeHost` 传给 `BootstrapDispatcher.admit` 的 `control_session_id` 等参数；`dispatcher.py:178-188` 的 `Surface` 装配）。
  bootstrap 路径其余部分与传输无关。
- 各阶段客户端的行为：

| 客户端 | 无 daemon | bootstrap | restoring | serving |
|---|---|---|---|---|
| CLI 业务命令 | 发送前即连接失败（`client.py:226-239`）→ ensure → 重试一次 | 稳定拒绝“not signed in; run `hyprial login`”（OD-Z5） | 既有 `DAEMON_RESTORING` 等待（`client.py:185-200`） | 一次请求 |
| `hyprial login`（首次） | ensure | `auth.login.begin` → 打印 URL 与 code，除非 `--no-open` 否则开浏览器 → 轮询 `auth.operation.get` → `onboarding.initialize` → 轮询 `ping` 到 `serving` → 既有后续步骤 | — | 账号切换路径，本计划不变 |
| `hyprial init` | 建 home、注册单元（在允许处）、启动 | 成功：报告 `phase: bootstrap` 与 `nextSteps: ["hyprial login"]`，不跑登录 | — | 报告现有状态 |
| `hyprial logout` | ensure | `auth.logout`（初始化前的 bootstrap 变体） | — | `auth.logout`；随后 daemon 回到 bootstrap |
| `hyprial gui` | ensure（所有状态） | 按 `ping.phase` 准入；子进程只转发 | 等待 | 同今天（G7） |
| `request_http` 子进程 | 启动被拒 `BACKEND_UNAVAILABLE`（启动器先 ensure） | 纯转发 | 转发，readiness 为假 | 转发 |
| `daemon status`、`doctor`、`daemon stop`、`daemon service *` | 报告不存在，永不启动 | 报告 `phase: bootstrap` | 同今天 | 同今天 |

### 3.3 socket 访问规则

事实（读代码）：监听器在目录 `0700` 下创建并对 socket `chmod 0600`（`S/daemon/impl/ipc/server.py:131-148`，`:147`）。
请求帧是 `{version, id, method, params}`，没有 caller 字段（`client.py:223`）。服务器把每一帧标记为
`_trusted_message_origin="ipc"`（`server.py:608-611`）。对端 PID 只用于统计（`S/daemon/impl/ipc/params.py:52-72`，
`SO_PEERCRED` 的 uid 被丢弃）；**没有 uid 检查**。onboarding 处理器的 caller 取自进程，不取自参数：
serving daemon 为 IPC 合成 `caller="local-operator"`（`S/daemon/impl/application/onboarding/__init__.py:130-155`）。

bootstrap 阶段的规则：

1. 同一个 socket 路径、同样的权限位。另外校验对端 uid（Linux `SO_PEERCRED`，macOS `LOCAL_PEERCRED`），拒绝其他 uid。这是纵深防御，文件权限本来就已强制同 uid。
2. bootstrap 中每个 IPC 帧都以 `("bootstrap", "local-bootstrap")` 身份运行，落在同一个 daemon 铸造的 operator 表面（既有 `local_operator_surface`，`runtime.py:169-194`）。因此所有本地客户端看到同一个登录操作。
3. 允许的方法：`ping`、`daemon.catalog`、`shutdown`，以及 8 个 onboarding 方法（`onboarding.status`、`auth.login.begin`、`auth.operation.get`、`auth.operation.cancel`、`auth.relogin`、`auth.logout`、`onboarding.device.ensure`、`onboarding.initialize`；注册在 `S/daemon/impl/request_catalog/onboarding/handlers.py:258-266`）。其余一律拒绝。
4. 密钥：device code、access/refresh token 留在 identity worker 与 `secrets/` 内；socket 只传 `verificationUri`、`userCode`、过期时间与操作 id（原型 B1 实测）。

后果：信任边界等于今天的 CLI 登录——任何同 uid 进程今天就能运行 `hyprial login` 或写 `settings.json`。
变化是：同 uid 进程可以观察或取消另一个客户端发起的登录（今天 GUI 的登录绑定在 GUI control session 上，
只有拿到 HTTP bearer 才能触达）。浏览器无法访问 unix socket，HTTP 子进程仍保持自己的 bearer 与 origin 规则。
这一点写入契约（风险 K3）。

### 3.4 catalog 与 digest 规则

事实（读代码）：

- 声明 digest 不含逐方法的 `state`/`callable`（`DECLARATION_FIELDS` 没有它们：`S/shell/impl/request_http/catalog.py:21-35,168-182`），所以它不随阶段变化。
- HTTP 契约 digest 按路由计算；`state` 为 `disabled` 的方法不是路由（`_unsupported_reason` 在 `catalog.py:288-331` 拒绝 `available`/`not-ready` 以外的状态，`build_view` 在 `:362-368` 跳过它）。GUI 策略下 bootstrap 投影里非 bootstrap 方法的 `state` 是 `disabled`（`bootstrap/dispatcher.py:123-125`、`:242-270`），serving 投影里两个仅 bootstrap 的方法（`DISABLED_METHODS = {onboarding.device.ensure, onboarding.initialize}`，`gui_host.py:102`、`:442`）也是 `disabled`。Z-1 的 wire 策略已把 bootstrap 投影改为 `not-ready`（`dispatcher.py:126-128`）。所以两个阶段的路由集合与 HTTP digest **不同**，子进程应答 `CATALOG_STALE`（`server.py:427-438`）；这就是 G10。
- 同版本、同节点、同 owner 的 epoch 变化已被重新握手吸收；owner 变化是 `CATALOG_MISMATCH`（`server.py:440-452`），且子进程的 owner 与 actor 固定在其配置里（`server.py:98-123,236-262`）。
- `not-ready` 加原因已是现有 catalog 状态，表示“已声明、当前不可调用”（枚举在 `S/daemon/impl/request_catalog/daemon/catalog.py:119-123`，判定在 `:243-267`；`gui_host.py:450-455` 与 `bootstrap/dispatcher.py:126-128` 用原因 `ACCOUNT_REQUIRED`）。

规则：

1. 阶段性不可用**一律**是带原因的 `not-ready`（bootstrap 中 `ACCOUNT_REQUIRED`，serving 中 `BOOTSTRAP_ONLY`），绝不是 `disabled`。这样路由集合与两个 digest 在每个阶段都相同；测试断言两个投影的 digest 相等。
2. `ping.epoch` 在每次阶段变化时改变；无状态客户端（CLI）自动跟随。
3. `request_http` 把“phase 变了的 epoch 变化”视为合法的重新握手，即使 `owner` 也变了：用新 principal 重新发布 capability，生成新的 transport generation，**保持端口与 bearer**（Z-6c）；session host 在 generation 变化时撤销业务 session。这对应 `contract/gui-session/README.md` §11（Authority change，`:394-419`）里启动器“可以”发布新 authority；其中没有任何内容被移除，并入后该文件因状态在进程内而消失。
4. promotion 期间 socket 缺席不到 1 s（原型实测 0.37–0.91 s）。initialize 调用方轮询 `ping`；其他客户端得到连接失败并走 ensure，后者在 `state/daemon-launch.lock` 下重新探测（`daemon_start.py:413-429`）并发现 daemon。可选的后续优化：把已绑定的监听器交给 application，让客户端排在 backlog 里（估算约 30 行，`server.py:131-148`）。

契约影响：规则 1 改变既有方法所报告的 `state` 字段值，须带 `Contract-Impact` 行并更新
`contract/daemon-request`；GUI 读取的是 `callable`，Z-2 中须核对 GUI 与 Contract Mock 语料。
`contract/daemon-request` 的文字改动随 Z-2 一起落地（行为改变的同一个 PR），本文（Z-0）不改契约。

### 3.5 logout 在进程内

推荐形态是进程内循环（原型 D2 实测）：`auth.logout` 之后 application 停止，同一进程重新准入 bootstrap。
若 application 改为让进程退出（`owns_process_exit`，`commands.py:372`；它的含义是“shutdown 后仍有线程无法 join 时允许强制退出”，
`S/daemon/impl/application/startup.py:445-`），结果相同，只是晚一次启动：因为无 owner 的启动现在合法，
在 `KeepAlive`/`Restart=always` 下单元于 5 s 节流后重启进入 bootstrap；无管理器时由下一条命令的 ensure 启动。
前置调研的“重启循环风险”存在的原因是无 owner 的启动曾是失败；现在它不是失败。无需改 `KeepAlive` 或 `Restart=`。
`owns_process_exit` 在循环里的行为：UNKNOWN（原型没有设置它，`daemon run` 设置；见 K1）。

### 3.6 崩溃恢复与 KP-059

在 `accept_initialize` 与 `daemon.ready` 之间被 kill，会留下 `promoting`/`unknown` 的 initialize 回执，而此时 home 已有 owner。
下次启动进入 serving，`onboarding.status` 报告 `initialized: false, nextActions: [initialize]`，
而 serving 拒绝 initialize（KP-059；`account_lifecycle.py:771-777`）。Z-3 关闭它：在 `daemon.ready` 时，serving daemon 完成
`account_owner` 等于当前 owner 且设备记录存在的非终态 initialize 回执（OD-Z4）。钩子位置：
`S/daemon/impl/application/onboarding/__init__.py:183-191` 调用端口的 `on_daemon_ready`（`account_lifecycle.py:884`）。

### 3.7 每个 home 的 OS 服务

| 项 | 今天（读代码） | 变化 |
|---|---|---|
| 单元里的 home | 已渲染（`launchd.plist.template:19` 的 `HYPRIAL_HOME`、`HARNESS_STATE_DIR`；`systemd.service.template:11-12`） | 无 |
| 名称 | 常量 `LAUNCHD_LABEL`（`service.py:41`）、`hyprial.service`；见 2.5 | 默认 home（`$HOME/.hyprial`）保留 `com.hyprial.hyprial.daemon` / `hyprial.service`；其他 home：`com.hyprial.hyprial.daemon.<id>` / `hyprial-<id>.service`，`<id>` = home 解析路径 sha256 的前 12 位十六进制（OD-Z11）。单元里的 `HYPRIAL_SERVICE_UNIT` 携带派生名 |
| 非默认拒绝 | `service.py:588-593` | 移除 |
| 测试安全 | (a) 在 operating home 之外的 home 被拒（`service.py:580-587`）；(b) `HOME` 与账号 passwd home 不同时拒绝所有真实管理器命令（`service.py:975-988`，`service-account-home-mismatch`）；(c) 对真实单元目录的 tripwire（`tests/support/runtime/environment.py:86-112`） | (a)(b)(c) 保留。真正保护伪造 `HOME` 的测试的是 (b)，它不依赖被移除的拒绝。新增 `HYPRIAL_SERVICE_AUTOINSTALL=0` 与 `HYPRIAL_DAEMON_AUTOSTART=0`，在 `tests/conftest.py` 的 autouse 环境 fixture（`:64-130`）与 `isolated_environment()`（`tests/support/runtime/environment.py:15-45`）里设置；产品代码永不设置。单元测试继续用 fake runner |
| 程序 | `shutil.which("hyprial") or sys.argv[0]`（`S/shell/impl/cli/commands/daemon/service.py:63`），模板里一个 `{{EXECUTABLE_ARG}}` 后接 `daemon run` | 当没有 console script 解析到本安装时，渲染 `[sys.executable, "-m", "hyprial.cli", "daemon", "run"]`；`argv_runs_daemon`（`S/daemon/impl/configuration/launchd_jobs.py:79-93`，目前只认三种 `<exe> daemon run` 形态）增加该形态，并以单元的 `HYPRIAL_HOME` 匹配 |
| 文件大小 | `service.py` 996 行；`S/daemon/impl/operations/` 有 7 个直接文件（结构门上限） | 先拆成 `operations/service/`：`types.py`（约 95）、`templates.py`（约 80）、`identity.py`（新命名，约 60）、`supervisor.py`（launchctl/systemctl 调用，约 200）、`manager.py`（其余，约 620）；该目录降到 6 个文件（估算） |
| 退出策略 | `KeepAlive true`、`Restart=always` | 不变（3.5） |
| 命令 | `daemon service install`（注册并启动，如 `brew services start`）、`daemon service uninstall`（停止并移除，如 `brew services stop`）、`daemon service status`、有单元时 `daemon start\|stop\|restart` 走管理器（T10） | 不需要新命令；home 来自环境，不需要 `--home` |
| 契约文字 | `contract/daemon-lifecycle/service-supervision.md`（192 行）、`contract/cli-blackbox/run.py` | 随命名规则更新 |

### 3.8 首次运行 ensure，与永不 ensure 的命令

“任意 hyprial 命令首次运行就注册并启动服务”的具体含义：

- 位置：唯一的启动原语里（`daemon_start.py:339-382`）。若已安装单元，经管理器启动（今天的行为）；
  若没有，且平台受支持、未关闭自动安装、`HOME` 是账号 home、程序路径稳定（不是 translocated，Y13），则安装单元并经它启动；否则 detached spawn（今天的行为）。
- 谁隐式调用：CLI 请求函数在连接失败时（`support.py:176-222`）、`hyprial gui`、`hyprial login`、`hyprial init`。
- 谁永不调用：不发 daemon 请求的命令（`--help`、`--version`、补全）；`daemon status`、`daemon stop`、`daemon run`、`daemon service status|uninstall`、`daemon upgrade status`、`doctor`、`profile *`。这些向 `runtime.execute` 传 `ensure_daemon=False`。
- 非交互使用：ensure 从不提示。bootstrap 阶段业务命令以一个稳定错误码失败。CI 设 `HYPRIAL_DAEMON_AUTOSTART=0`。
- 成本：daemon 在跑时为零（ensure 在失败路径上）。不在跑时：到 bootstrap 约 4.4 s（原型 A1 实测），到 serving 5.2 s（T6）。一条 CLI 命令在本沙箱 0.5–0.7 s（原型 A8、C6）。
- 期望“无 daemon”的测试：在 `tests/`、`contract/`、`scripts/` 下共 29 个文件含子串 `DAEMON_UNAVAILABLE`（grep；仅 `tests/` 为 21，其中含 `GUI_DAEMON_UNAVAILABLE`）；全局设置 opt-out 后期望“无 daemon”的测试语义不变。

## 4. 原型测量

两份原型都**不在仓库里**，也不是产品代码。下面写到足以重建。

### 4.1 第一份：promotion 之后的重启（前置调研）

目标：确认“经 GUI 初始化后 daemon 就是 `request_http` 子进程本身”。接线：复用
`tests/integration/request_http/onboarding/test_gui_live_lifecycle.py:149-190` 的子进程入口（真实 HTTP、真实锁、真实
`DaemonApplication`；身份提交与设备准备是 `test_gui_promotion.py` 的受控端口，无账号无网络），驱动脚本依次：
读 bootstrap 状态 → 在 bootstrap 子进程运行时调用真实的 `admit_source()` → `auth.login.begin` 与 `auth.operation.get` →
`onboarding.initialize` → 等待新发布 → 读 `daemon.json` 的 pid → 枚举 `$HOME` 下的服务单元文件 → SIGTERM 子进程 →
确认 `daemon.sock` 与 `daemon.json` 消失 → 对已初始化、无 daemon 的 home 再做一次准入。
隔离：全新 `/tmp/dlc.*`（短路径，因 AF_UNIX 长度限制）、`isolated_environment()`、不设 `HYPRIAL_OWNER`、网络隔离、
**从不**触碰 `$HOME/.hyprial`，从不运行 `init` 或 `daemon service install`。结果即 G3–G6：1.1 s promotion、
pid 相等、无服务单元、子进程退出后 socket 消失、第二次准入 `GUI_DAEMON_UNAVAILABLE`。

### 4.2 第二份：Z 进程（本方案）

接线：一个约 110 行的进程，把 unix-socket 监听器放在**既有**的 bootstrap 分派器前面，并运行**既有**的 promotion。
- 接受循环绑定 `state/daemon.sock`（目录 `0700`、socket `0600`），每连接一个请求，帧格式即 `S/kernel/impl/ipc/client.py:223-301` 的协议（换行 JSON，`{"version":1,"id","method","params"}`），错误形态与 daemon 的稳定错误边界相同。
- 请求交给既有 `GuiRuntimeHost` 的 bootstrap 分派（原型当时是 `host(method, params, socket_path=…)`；Z-1 起即 `bootstrap/dispatcher.py:272-296` 的 `call`，其下是 `METHOD_REGISTRY.invoke`）。
- 回复写完整后取 `host.take_post_reply()`（仅被已接受的 initialize 布置）并执行，从而触发既有的 promotion 路径。
- 外层循环：准入 bootstrap host（`DaemonOwnershipBusy` 时重试，最多 30 s）→ `enable_promotion` → 服务 socket → promotion 事件到达后关闭监听器 → `host.run_promoted(build_daemon_application, …)` 在同一 PID 内构造真正的 `DaemonApplication` 并绑定同一个 socket → 它返回（logout）后 `host.close()` 并回到循环顶部再准入 bootstrap。
- 身份提交与设备准备使用受控端口（`CommittingIdentity`、一个写 `DeviceRecord` 的 provisioner），无账号无网络。
- 驱动脚本对这个进程运行真实的 `daemon_request_once` 与真实 `hyprial` CLI 子进程，隔离同上（`/tmp/dfz.*`，不设 `HYPRIAL_OWNER`，`HYPRIAL_PEER_DISCOVERY=0`，不运行任何服务命令），测完删除进程与目录。

| 步骤 | 结果（实测） |
|---|---|
| A1 | spawn 后 4.37 s socket 应答（解释器与 import；`BootstrapRuntime.admit` 本身 0.01 s）。`ping` → `phase: bootstrap, owner: None` |
| A2 | 状态目录含 `bootstrap.json`、`daemon.lock`、`daemon.sock`（模式 `0600`）、`onboarding/`。RSS 146 MB |
| A4 | `daemon.catalog`：153 个方法，8 个可调用（即 3.3 的 8 个） |
| A6 | `agent.list` → `CALLER_NOT_AUTHORIZED`“method is not available in login bootstrap” |
| A7 | 真实 `hyprial daemon status --json` → exit 1 `CALLER_NOT_AUTHORIZED`（它调用 `ps`，`commands.py:33-35`）。**Z-2 须关闭这个缺口** |
| A8 | 真实 `hyprial agent list --json` → exit 1，同一错误码，0.49 s。**错误文案/错误码决定见 OD-Z5** |
| B1–B2 | `auth.login.begin` 返回 `verification {verificationUri, userCode, expiresAt, intervalSeconds}`；`auth.operation.get` → `authenticated`；owner 由受控 identity 端口写入 `settings.json` |
| C1–C3 | `onboarding.initialize` → `promoting`；回复后 0.91 s 到 serving；同一 PID；`daemon.json` pid 等于进程 pid；RSS 168 MB |
| C4 | 以本地 operator 身份在 socket 上读 `onboarding.status`：`serving, authenticated, initialized: true, deviceReady: true, nextActions: []` |
| C5 | 可调用方法 8 → 124；epoch 改变（bootstrap epoch ≠ serving epoch） |
| C6–C7 | 真实 CLI `agent list` exit 0（0.69 s）；`daemon status` exit 0，`running: true` |
| D1–D3 | socket 上 `auth.logout` → `loggedOut: true`；0.74 s 后同一 PID 回到 bootstrap，新 epoch |
| E1 | 同一 PID 内第二次登录加 initialize：0.37 s 后 serving |
| F1–F2 | SIGTERM → exit 0；socket 与 `daemon.json` 被移除 |

**未覆盖**：真实 OIDC（BLOCKED：需要账号与网络）、GUI 链路对这个进程（UNKNOWN）、循环中 `owns_process_exit()` 的行为（UNKNOWN：原型没有设置它）、
macOS（BLOCKED）、各阶段 kill -9（UNKNOWN）。

## 5. actor-rustify 完成后的影响

读自 `f1a4caf86`；本节没有重新运行任何测量。

### 5.1 变化了什么

- #2824 删除了 pykka 后端与 `ActorRuntime` 门面：`ACTOR_BACKENDS = ("reference-abi", "native")`
  （`S/kernel/impl/actor_runtime/port/config.py:27`），未设置变量时默认 `native`（`DEFAULT_ACTOR_BACKEND`，`config.py:26,36-48`），
  编译扩展缺失或不匹配则 fail closed（`S/kernel/impl/actor_runtime/backends/native.py:54-62`）。
- `crates/` 在 `9932b2b63` 与 `f1a4caf86` 之间完全相同（`git diff --stat` 为空），编译出的引擎是同一份二进制。
- daemon 启动、promotion、bootstrap、IPC、服务管理器与 CLI daemon 命令在两个提交间逐字节相同。

### 5.2 promoted `DaemonApplication` 是否仍可同样在进程内构造

按读代码与既有测量：是。
- 原型运行所在的树上 native 引擎已是默认，且隔离环境会剥掉 `HYPRIAL_*`，所以第 4.2 节**用 Rust 引擎测得**：bootstrap → serving → logout → bootstrap → serving 在一个 PID 内，两个完整 application 生命周期（实测）。
- **没有“每进程一个引擎”的约束**：引擎按 actor host 创建（`S/kernel/impl/actor_runtime/port/host.py:84-91`），每个引擎拥有自己的 tokio 多线程 runtime，4–16 个 worker（`crates/hyprial-py/src/engine.rs:374-381`）。同一进程里第二个 application 构造新引擎；旧的由 application 的 `actor-runtime` 关停阶段关闭。
- 扩展内的进程级状态：native handler 注册表（`crates/hyprial-py/src/handlers.rs:147-150`，模块初始化时填充，之后只读）与解释器退出 attach 闸门（`crates/hyprial-py/src/gate.rs:1-30`）。闸门只在 `atexit` 时关闭一次，引擎 shutdown 不关它，所以不阻止第二个引擎。含义是进程必须经正常解释器退出结束：仍在 Python 内的 handler 有有限宽限，在 finalization 开始后取 GIL 的线程会在 CPython 3.12/3.13 上使进程 SIGABRT。
- 对 logout 循环的后果：在同一进程回到 bootstrap（3.5）完全避开 logout 时的解释器退出，这是更安全的一侧；回退方案（进程退出，下次启动落在 bootstrap）走的是每次 `daemon stop` 今天都走的同一条退出路径。
- 线程与 GIL：handler 在专用 OS 线程上运行，从不在 tokio worker 上（`crates/hyprial-py/src/pump.rs:8-13`）；引擎调用释放 GIL（`engine.rs:563,758,778,886`）。**bootstrap 阶段不构造任何引擎**（它直接服务注册表的 handler，`gui_host.py:305-319`），所以 bootstrap 监听线程与引擎永不共存。
- fork 与 spawn：本方案不 fork。daemon 由 `subprocess` + `start_new_session`（T5）或管理器启动；identity worker 是在 bootstrap 期间、任何引擎存在之前启动的独立进程。持有活的 tokio runtime 的进程不得不经 exec 就 `fork()`；这里没有这种路径。
- 未重新测量（UNKNOWN）：在 `f1a4caf86` 上的同一次双 promotion。Python 侧适配器有改动（`backends/abi.py`：准入追踪、事件日志并入）。Z-2 的第二个测试就是这个测量，应在新基线上最先运行。

### 5.3 打包与发布

- wheel 是平台特定的（`cp312-abi3` 扩展），没有回退后端；`reference-abi` 仍存在，但只是显式选择。**KP-027**（五平台 wheel 发布；安装器与清单仍硬编码 `py3-none`）未关闭并阻断发布（`docs/reference/known-problems.md:262-269`）。本方案开发与测试不依赖 KP-027，但**在 KP-027 关闭前没有任何用户拿到本方案的任何部分**，与 0.5.5.4 的其他改动相同。
- 程序位于 app bundle 内的 OS 服务：单元的解释器与扩展必须是同一次构建。把程序渲染为 `[<bundle python>, "-m", "hyprial.cli", "daemon", "run"]`（3.7）可以保证，因为该解释器解析自己的 `hyprial._core`。指向 `PATH` 上找到的 `hyprial` console script 的单元可能启动另一份安装、另一份或缺失的引擎；因此 `shutil.which("hyprial")`（`daemon/service.py:63`）只能在它解析到正在运行的安装内时使用（Z-8b）。
- 管理器下 fail-closed 启动：扩展缺失或不匹配的 daemon 在启动期间退出。这属于启动失败，计入 `state/daemon-start-failures.json` 并按 5 s → 60 s 节流（T9）。有了生命周期循环，这只可能发生在 promotion 或带 owner 的启动处，不会在 bootstrap，因为 bootstrap 不加载引擎。Z-2 应在进入 bootstrap 前验证引擎可加载（`import hyprial._core.actors` 一次），让损坏的安装在启动时失败而不是用户登录之后。
- uv tool 目录：`uv tool install` 就地替换环境（V4）。运行中的 daemon 保持旧 `.so` 映射；单元路径仍有效；重启（`kickstart -k`/`systemctl restart`，或 detached 重新启动）加载新扩展。升级已拒绝 editable 安装与 uv tool 目录之外的解释器（V4）。新增：升级到本平台没有 wheel 的 tag 会在安装时（重启前）失败，旧 daemon 继续运行——这是正确结果，KP-027 落地后应在升级测试里断言。
- app 更新时旧 daemon 在跑（V5）：不变，现在还涉及引擎。DL-3 是两种方案里共同的修复。

### 5.4 测试

- 隔离 daemon 测试需要编译引擎，或显式 `HYPRIAL_ACTOR_BACKEND=reference-abi`。本方案中启动真实 application 的测试（Z-2 第二个测试、Z-3、Z-5、Z-6a、Z-6c）必须用 native 引擎，因为那才是发布内容；Z-1（wire host）、Z-4（ensure）、Z-8（服务管理器）的单元测试不构造引擎。
- 在 `crates/` 被触碰之前，从 `9932b2b63` 构建的 native 引擎对 `f1a4caf86` 仍然有效；构建戳含 `source_sha256`，应比较它而不是信日期。
- CI：pykka 作业已消失；`native-conformance.yml` 保留 `reference-abi`、`native-linux-amd64`、`native-linux-arm64`、`native-darwin-arm64` 与 `free-threaded-3-15t`。新的红测试不得按名字选择已不存在的后端。
- 内存：原型实测每个 daemon 146 MB（bootstrap）与 168 MB（serving），均为 Rust 引擎下（K11）。

## 6. PR 序列

估算口径：删除行数来自对树的 `wc -l` 与行范围；新增行数由原型（监听器 45 行、循环 45 行）乘以产品关切（边界、超时、peer 检查、日志、信号、文件头决策块）估算，或取自所命名的同类既有代码的规模；测试行数按同类既有测试估算。**所有新增行数都是估算。**
“CLI 收敛区”指另一位开发者正在改的 `S/shell/impl/request_http/**`、`http_contract/**`、kernel IPC、CLI 请求函数；
列为“须协调”的 PR 要与他商定后再动。

| PR | 内容 | 规模（源码 +/−；测试 +/−） | 依赖 | 沙箱中 BLOCKED 的部分 | CLI 收敛区 |
|---|---|---|---|---|---|
| **DL-1** | `hyprial gui` 对有 owner 的 home 在准入前确保 daemon（调 `_launch_daemon_process`，把 `CliError` 原样映射到 GUI 启动记录；无 owner 与 `contract-mock` 不调用）。实现落点（2026-11-15 接手后）：CLI 文件只提供启动原语（`commands.py` 的 `ensure_daemon` 钩子），钩子由启动器在 `admit_source` 的 daemon 分支内、不需要 daemon 的参数与身份校验之后（端口占用检查在准入之后，属已知次序）、验证之前调用（唯一的“无 owner”判断即该分支原有的 `owner is not None`；Z 线删除它），其后对“恢复中”的 daemon 有界等待（60 秒），因为启动原语的就绪只表示 ping 可答、不含 restore 结束。`S/shell/impl/cli/commands/gui/commands.py`、`S/shell/impl/gui/backend/admission.py`；红测试：隔离的有 owner、无 daemon 的 home 今天 `GUI_DAEMON_UNAVAILABLE` | +45/0；+200/0 | GUI PR 2c | 无 | 否 |
| **Z-0** | 本设计文档；加上 `docs/reference/known-problems.md` 一条 KP-064 与 KP-059 的指针；GUI 单一启动设计 7b 交叉引用。契约文字改动不在此，随 Z-2 | 文档 | owner 决定 | 无 | 否 |
| **Z-1** | bootstrap wire host：从 `GuiRuntimeHost` 抽出分派器（ping、catalog 投影、registry invoke，原在 `gui_host.py` 的分派与投影段），加 socket 监听器（帧边界、peer-uid 检查、post-reply 钩子）。`GuiRuntimeHost` 委托给分派器。尚无产品路径使用该监听器。新文件 `S/daemon/impl/application/onboarding/bootstrap/{dispatcher,wire,peer}.py`；红测试：对临时无 owner home 上的监听器 `daemon_request_once("ping")` 返回 `phase: bootstrap`；initialize 的 post-reply 仅在完整写出后触发 | +300/−120；+280/0 | 无 | 无 | 否 |
| **Z-2** | `daemon run` 在无 owner 或不存在的 home 上运行生命周期循环（bootstrap → promote → serve → logout → bootstrap）；`daemon start` 把 `phase: bootstrap` 视为已启动；`daemon status` 与 `daemon stop` 在 bootstrap 可用（`shutdown` 被服务；`status` 回退到 `ping`，关闭 A7）；`--bootstrap` 保留为被接受的 no-op；3.4 规则 1（`not-ready` 取代 `disabled`）及 `contract/daemon-request` 文字与 `Contract-Impact`；进入 bootstrap 前验证引擎可加载。红测试：隔离无 owner home 上 `hyprial daemon start --json` 今天 `DAEMON_START_FAILED`（T13），期望 `ok`，`daemon status` 显示 `running: true, phase: bootstrap`，`daemon stop` exit 0。第二个测试：用受控 identity 端口的自定义子进程入口经产品循环复现 4.2 的 B–E 步，并在 `f1a4caf86` 基线上先跑 | +300/−60；+330/0（可拆成“循环”与“bootstrap 中的 start/status/stop”） | Z-1 | 真实 OIDC | 否（`daemon/impl/onboarding`、`daemon_wiring`、`cli/commands/daemon`） |
| **Z-3** | serving 启动完成当前 owner 的非终态 initialize 回执（关闭 KP-059）；崩溃矩阵：在 `awaiting-auth`、`authenticating`、`promoting` kill -9，下次启动进入合法阶段且 `onboarding.status` 如实。红测试：带 `promoting` initialize 回执与 owner 的日志 → `on_daemon_ready` → 今天 `initialized: false`，期望 `true` | +90/−10；+220/0 | Z-2；OD-Z4 | 无 | 否 |
| **Z-8a** | 把 `service.py` 机械拆为 `operations/service/` | 移动 996 行；0 | 无 | 无 | 否 |
| **Z-8b** | 每 home 的单元名、解除拒绝、module 形式程序、job 扫描器、代理变量（OD-Z7）；退出策略不变 | +200/−70；+240/−20 | Z-8a | 真实 launchd BLOCKED；真实 systemd 用户会话未探测（UNKNOWN） | 否 |
| **Z-8c** | 启动原语在允许时安装单元、否则 detached spawn；opt-out 变量；`conftest.py` 与 `isolated_environment()` 设置它 | +80/0；+170/0 | Z-8b；OD-Z2 | Mac 上重启后存活 BLOCKED | 否 |
| **Z-6a** | `request_http` 的 `local-gui` 子进程在有 daemon 应答时于 bootstrap 转发；启动器在所有状态 ensure 并按 `ping.phase` 准入（`admission.py:212-218` 失去“无 owner 不检查 daemon”分支） | +120/−40；+200/0 | Z-2 | 浏览器半段 UNKNOWN | **须协调**（`request_http/__init__.py`、`server.py`） |
| **Z-4** | CLI 在连接失败时 ensure；R9 命令 `ensure_daemon=False`；opt-out 变量；bootstrap 中业务命令一个稳定拒绝（`support.py:176-222`；`S/shell/impl/cli/runtime/composition.py`；六个命令模块的标志） | +110/−10；+240/0 | Z-2、Z-6a（避免自动启动的无 owner daemon 把 GUI 锁在门外） | 无 | **须协调**（CLI 请求函数，一处约定的小改动） |
| **Z-5** | 首次 `hyprial login` 成为 daemon 客户端（保持已发布的 `--json` 形状）；`hyprial init` 与 login 解耦：home、单元、启动、报告，移除 `init` 的进程内登录分支（R11；改变无 owner home 上的 `init --json`：`Contract-Impact` 与 `contract/cli`） | +300/−150；+320/−80 | Z-2、Z-4 | 真实 OIDC BLOCKED | **须协调，建议由其负责**（即他们的 W4 中的 `login`/`init`） |
| **Z-6b** | 删除 GUI 托管的 bootstrap：`serve_gui` 的 promotion 与轮换、`rotated_config`、`reopen_after_logout`、HTTP post-reply 响应、`GuiRuntimeHost` 的 bootstrap 半部（`bootstrap/gui.py` 131、`bootstrap/response.py` 73、`gui_host.py` 约 230/570、`server.py`/`app.py` 约 30）；被 Z-6a 的测试取代（删除 `test_gui_promotion.py` 303、`test_gui_live_lifecycle.py` 196） | +30/−460；0/−500 | Z-6a、Z-4、Z-5 | 无 | **须协调**（`request_http/**`） |
| **Z-6c** | principal 就地变化：phase 变化时重新握手保持端口与 bearer；新 transport generation；启动器用同一 endpoint 发布 authority（`server.py:427-471`、`S/shell/impl/gui/backend/lifecycle/rebind.py`、`request_http/config.py`） | +110/−40；+160/0 | Z-6b；GUI PR 3；须协调 | 无 | **须协调**（`request_http/**`） |
| **Z-7** | 删除 `serve_external`、`nativeHost`、`BootstrapHttpHost`、`daemon run --bootstrap` 的终端登录循环（扩大的 DL-6）：`bootstrap/external.py` 270、`bootstrap/__init__.py` 36、`http.py` 63、`config.py` nativeHost 约 30、`launch.py` 终端循环约 180、`contract/native-host/`、`contract/wire/daemon-login-http-bootstrap.md` | 0/−600，契约 −70；0/−1,390 | Z-6b；GUI PR 7 | 无 | **须协调**（`request_http/**`） |
| **Z-9** | 文档、skill、KP-059/KP-064 更新、`AGENTS.md` 一段、运维指南 | 文档 +220/−80 | 全部 | 无 | 否 |

仍然需要（两种方案都需要，且 owner 已确认）：
- **DL-3**：ensure 里的版本规则——运行中的 daemon 是另一个包版本时，经既有的重启路径（感知管理器）把它重启到本安装（D5）。位于 `S/shell/impl/cli/commands/gui/commands.py` 与 ensure 路径；必须在第一次 app **更新**发布前完成，不必在首个 app 发布前。
- **DL-4**：已打开的 GUI 能从 daemon 重启恢复：daemon epoch 变化（或 `/readyz` 报 `CATALOG_STALE`）时启动器调用既有的 `replace_backend()`（G11）。涉及 `S/shell/impl/gui/backend/lifecycle/session.py` 与 `rebind.py`，在 GUI PR 3（只剩一个 host 可轮换）之后。
- **DL-7**（停止与移除）：D3 要求的独立“停止后台服务”动作。文档化 `hyprial daemon stop` / `daemon service uninstall`；GUI 动作需要单独设计（daemon 在服务中不能自己卸载自己的管理器）。本方案未把它排进 Z 行，在 Z-8c 之后另行立项。

### 6.0 实施状态（2026-11-19）

| PR | 状态 |
|---|---|
| Z-0 | 已合入 #2829 |
| Z-1 | 已合入 #2850 |
| Z-2 | 已合入 #2858 |
| Z-3 | 已合入 #2861 |
| Z-4 | 已合入 #2925；Z-4b #2944 |
| Z-5 | 已合入：PR 1 #2958（daemon 驱动的首次登录）、PR 2 #2967（`init` 不再登录，`--no-daemon` 移除） |
| Z-6a | 已合入 #2916；Z-6pre #2927 |
| Z-6b | 已合入 #2933 |
| Z-6c | 已合入 #2946 |
| Z-7 | 已合入：A1 #2963、A2 #2976、A3 #2977 |
| Z-8a 至 Z-8c | 已合入 #2830 |
| DL-1 | 已合入 #2845 |
| DL-4 | 被 Z-6c 取代：principal 变化在原进程内换绑，启动器不再替换后端子进程（Z-7 A2 #2976 删除 `replace_backend`） |
| Z-9 | 本文档收尾 PR（`docs/z9-single-launch-wrap-up`） |
| #2822 | session host 并入 `request_http`：步骤 1–4 已合入（#2985、#2995、#2996）；`hyprial gui` 的帮助文字待 owner 决定 |

同一次 PR 把“运维指南”一项落在 `docs/guides/operator-guide.md` 的“关闭 GUI 不停 daemon”一节，覆盖 DL-7 里
`hyprial daemon stop` 与 `daemon service uninstall` 的文档部分；GUI 里的停止动作仍无设计（UNKNOWN）。

### 6.1 顺序

1. 现在：GUI PR 2c 之后做 DL-1，消除已报告的失败（状态 3）。
2. 第 9 节的剩余 owner 决定，然后 Z-0。
3. daemon 侧，与 GUI 2c/3、CLI W1–W3 并行（无共享文件）：Z-1 → Z-2 → Z-3；独立地 Z-8a → Z-8b → Z-8c（验证需要一台 Mac）。
4. 与 CLI 收敛开发者一起：Z-6a → Z-4 → Z-5。
5. 删除与并入准备：Z-6b → Z-6c（在 GUI PR 3 之后）→ Z-7（与 GUI PR 7 一起）→ Z-9。

Z-4 在 Z-6a 之后，是因为在 Z-6a 之前自动启动一个无 owner 的 daemon 会把 GUI 子进程锁在门外（G12 的镜像，K6）。

用户在中间阶段看到的：
- DL-1 到 Z-6a 之间：经 GUI 初始化的用户，那个首次会话里 daemon 在 GUI 子进程内；关窗口会停掉投递，直到下一次 `hyprial gui`，那时会启动一个能存活的独立 daemon。终端用户不受影响。
- 从 Z-2 起：`hyprial daemon start` 在无 owner 的 home 上可用；还没有隐式启动。
- 从 Z-8c 起：受支持平台上第一次 `daemon start`、`init` 或 `gui` 注册每 home 的单元；重启后 daemon 回来。
- 从 Z-4/Z-5 起：任意命令都会启动 daemon，`hyprial login` 由它应答。

若首个 app 发布不能带有“首次会话”这个限制且 Z-6a 赶不上，DL-2 是保险（约一天，之后删除）；这是 OD-Z3，owner 当前决定是**不做** DL-2（D8），默认接受该限制。

### 6.2 成本对照（估算）

| | 分阶段（DL-1、DL-2、DL-5a/b/c、DL-6） | Z + 每 home 服务（DL-1、Z-0…Z-9） |
|---|---|---|
| 新写源码行 | 约 415 | 约 1,715 |
| 新写测试行 | 约 750 | 约 2,360 |
| 删除行（源码+测试+契约） | 约 1,200（仅 DL-6） | 约 3,570 |
| 实施者人日（一个 PR = 0.5–2 天含评审） | 5–7 | 14–19 |
| Mac 验证 | DL-5b 退出策略、DL-5c 重启 | Z-8b/c 的真实 launchd、重启；没有需要验证的退出策略变更 |
| 终态 | daemon 比 GUI 活得久；两套登录实现；GUI 子进程仍托管 bootstrap 并 promotion；监听器轮换；无 owner 的 home 没有 daemon；CLI 从不自动启动 | R1–R10 |

分阶段方案会建出、而 Z 之后删除或回退的东西：DL-2（约 70 行源码 + 130 行测试）、DL-5b 退出策略、DL-1 的“无 owner 不调用”一个条件。
达到 owner 模型相对分阶段终态的增量成本约 9–12 人日，其中约 4 天（Z-5 与请求函数改动）落在 CLI 收敛本来就要做的 W4 里。

### 6.3 Z-1 独立复核留给 Z-2 的清单

Z-1 的抽出与主要安全性质经独立复核成立；以下是复核时发现、不属于 Z-1、须在 Z-2（及 Z-6a，已注明）解决的项，Z-2 开工前逐条认领：

1. **崩溃恢复**：bootstrap host 被 `kill -9` 后，陈旧的 `state/bootstrap.json` 使下一次准入以 “record belongs to another bootstrap instance” 失败（共享代码，GUI 路径同样如此）；管理器下会变成重启循环。须先写红测试再修（已登记 KP-079）。
2. **initialize 回复写失败**：`onboarding.initialize` 的回复无法写出时，claim 停在 `promoting` 而本进程内永远无法完成。Z-1 只做了两件事：`BootstrapWireHost.serve()` 返回可区分的结果 `initialize-unacknowledged`（监听器关闭，锁与 claim 保持到调用者 `close`，后者把未完成工作标为未知），并在 `wire.py` 文件头记录。该结果之后进入哪个阶段（重新准入还是退出）由 Z-2 的循环决定，并要有测试。
   补充（读代码推断，未复现）：initialize 已被持久接受之后，若编码回复或 registry 抛出异常，host 会带着无法完成的 claim 继续服务，且已布防的 post-reply 动作仍留在 runtime 中（下一次调用的 `take_post_reply` 会取走它）。复核者建议的修法：在 respond 路径的 except 分支里取走 post-reply，并把该回复标为未确认（unacknowledged）。
3. **promote 与 shutdown 的竞态**：`shutdown` 与已接受的 initialize 同时到达时的结果（谁赢、claim 与锁如何收尾）未定义，Z-2 须规定并测试。
4. **wire host 缺 promotion 执行者**：`serve()` 返回 `promote` 之后，构造真正 `DaemonApplication` 并交接锁流的 runner 目前只存在于 GUI 路径（`GuiRuntimeHost.run_promoted`）；wire host 尚无调用者，Z-2 的循环要接上它。
5. **把 bootstrap daemon 当 serving daemon 读取 `ping` 的调用方**：
   - `init.py` 把 bootstrap 的 `ping` 当作 `already_running`；
   - `login/flow.py` 把 `owner` 为 `None` 当作 must_stop；
   - `logout` 会把它停掉；
   - `ps`（经 `daemon status`）得到拒绝（A7）；
   - `upgrade` 的 restore 轮询会等满整个预算；
   - `doctor`、`socialware` 在探测到运行中之后继续发业务调用；
   - GUI 准入与 `request_http/server.py` 的 owner 检查：这两项属 Z-6a，不在 Z-2。

## 7. 与 GUI 单一启动项目的关系

本文与 [GUI 启动方式收拢为唯一路径](gui-single-launch-2026-11-14.md) 并行，不取代它：

- **PR 2c / 3 不变**。session host 继续转发给 `request_http`，不应直接对 daemon socket 说话：`request_http` 是 MethodSpec 的唯一 HTTP 投影（参数约束、信封、预览确认：`S/shell/impl/request_http/app.py` 985 行加 `http_contract/`），CLI 收敛方案也保留它（`docs/design/cli-daemon-request-convergence-2026-11-14.md:43-48`）。Z-6a 之后启动器的准入在所有状态询问 daemon。`rebind.py` 与 authority 文件保留到 Z-6c 加上并入之后。
- **PR 6（原生壳）至少依赖 DL-1**：壳仍运行 `hyprial gui --no-open --json`；没有 DL-1，每个“经 GUI 初始化且无终端”的用户的第二次启动都是 `GUI_DAEMON_UNAVAILABLE`（G6）。ensure 会为 app 的 home 与 bundle 解释器（module 形式）注册 `com.hyprial.hyprial.daemon.<id>`（Z-8b）。Y9（socket 路径长度）、Y13（translocation）、Y8（同一账号在两个 home）仍是未决。
- **WIP PR #2822（把 session host 并入 `request_http`）只有 Z-6c 才解除阻塞**。其 blocker 1 是“监听器在 initialize 与 logout 时重开”（GUI 单一启动设计 §7a）。Z 之下 promotion 不再需要重开（Z-6b），Z-6c 去掉 principal 变化时的重开；之后并入没有监听器轮换要吸收。§7a 记录的另一个障碍（`request_http` 的文件与目录大小限制）不受影响。
- **core / extensions**：daemon 由 `hyprial`（wheel）启动，永不由壳启动，保持“壳不持有身份与凭据”的约束，并与 owner 口述一致（core 含通信与 daemon 启动）。`docs/design/core-power-pack-architecture-2026-10-19.md` 把 daemon 启动划给 Power Pack，该行在划分开始时需要对齐（GUI 单一启动设计 §7 第 2 条已记录）。

## 8. 风险与未验证

| # | 项 | 标记 | 如何确认 |
|---|---|---|---|
| K1 | `owns_process_exit()` 可能在 logout 时结束进程而不是回到循环 | UNKNOWN | Z-2 测试里设置该标志；回退方案（退出，下次启动是 bootstrap）已在 3.5 规定 |
| K2 | 在 HTTP 代理后从服务环境运行 device flow：服务单元只有 `PATH`，需要 `HTTPS_PROXY`（KP-001）的机器今天在 shell 里登录，从服务里会失败 | 读代码、UNKNOWN | OD-Z7；在 KP-001 的云电脑上测试 |
| K3 | 同 uid 进程可以观察或取消另一个客户端的登录 | 读代码 | 3.3 规则接受它，写入契约 |
| K4 | `disabled` → `not-ready` 改变一个已发布 catalog 字段的取值 | 读代码 | `Contract-Impact`；Z-2 里核对 GUI 与 Contract Mock 语料 |
| K5 | 已发布的 `hyprial login --json` 形状在客户端流程下（identity、device、daemon 块、resume、`--dry-run`）是否保持 | UNKNOWN | Z-5 的 golden 对比；账号切换与 `--dry-run` 留在旧路径 |
| K6 | 在 Z-6a 之前自动启动的无 owner daemon 会把 GUI 子进程锁在门外（G12 的镜像） | 读代码 | 排序：无 owner 的 ensure 只随 Z-6a 启用 |
| K7 | 真实 launchd：每 home label、签名 bundle 内的 module 形式程序、translocation、卸载后留下每 5–60 s 失败一次的单元 | BLOCKED | 需要 Mac：从 `/Applications`、Downloads、挂载的 dmg 安装单元；移动、删除 app；读 `launchctl print` |
| K8 | 没有 linger 的 systemd 用户会话，或根本没有用户会话（Y11） | UNKNOWN | 容器运行；ensure 必须回退到 detached |
| K9 | Windows：bootstrap 监听器需要完整 daemon 使用的 named-pipe 监听器（`server.py:134-141`）；Windows 上 daemon 驱动的 `hyprial gui` 能否运行未知（Y10） | UNKNOWN | Windows wheel 存在之前不在范围 |
| K10 | 真实 OIDC 经 daemon 从终端登录 | BLOCKED | 需要账号与网络；原型用受控 identity 端口，不是真实登录 |
| K11 | 每 home 的 bootstrap 阶段 daemon 在登录前占 146 MB | 实测 | 单 home 可接受；多 home 机器需注意 |
| K12 | `dev` 基线会在收敛工作之下移动；本文行号已对 `9932b2b63` 与 `f1a4caf86` 核对 | 读代码 | 实施时重新核对 |
| K13 | `f1a4caf86` 上的同一 PID 双 promotion 未重测（Python 适配器变了，引擎二进制相同） | UNKNOWN | Z-2 第二个测试在新基线上最先运行 |
| K14 | 本方案任何部分的发布都在 KP-027 之后 | 读代码 | 本方案内无解 |
| K15 | bootstrap 中真实 `hyprial daemon status` 返回 `CALLER_NOT_AUTHORIZED`（A7，实测）：状态命令在 bootstrap 不可用 | 实测 | Z-2 让 `status` 在 bootstrap 回退到 `ping` |
| K16 | GUI 后端链（session host → `request_http` → 这个 daemon）对原型进程未运行 | UNKNOWN | Z-6a 的集成测试；浏览器半段另需真实浏览器 |
| K17 | macOS App Translocation 与 launchd 的实际行为（Y13） | BLOCKED | 同 K7 |
| K18 | “首次运行即注册服务”在 macOS 上触发“已添加后台项目”系统通知 | UNKNOWN | Mac 上观察；OD-Z2 |

## 9. 待 owner 决定

下表“默认”均为计划推荐的默认值，**尚未经 owner 确认**（除非第 1.2 节已写成决定）。

| # | 决定 | 默认（未经 owner 确认） | 对用户可见 |
|---|---|---|---|
| OD-Z2 | 第一条命令是否不经询问就注册 OS 服务 | 是，在有用户会话的 macOS 与 Linux 上；非致命，stderr 一条提示，写出单元名与 `hyprial daemon service uninstall`；`HOME` 不是账号 home 时永不；有 opt-out 时永不。备选：只有 `init`、`gui`、`daemon service install` 注册，其他命令 detached 启动 | 是（macOS“已添加后台项目”） |
| OD-Z3 | 在 Z-6a 之前是否补上“首次 GUI 会话”缺口 | 否，接受到 Z-6a。备选：做 DL-2（约 1 天，之后删除） | 是 |
| OD-Z4 | serving 启动是否完成同一 owner 的待定 initialize 回执 | 是，当设备记录存在时 | 是（KP-059 症状消失） |
| OD-Z5 | 业务命令在 bootstrap 阶段返回什么 | **已决定（owner，2026-11-15）**：专用稳定码 `ACCOUNT_REQUIRED`（daemon 本地常量，先例 `DAEMON_STOP_UNPROVEN`），exit 1。Z-1 的 wire 拒绝已使用它，数据保留 `phase`、`method` 与同名的 `reason`；未声明的名字回答 `METHOD_NOT_FOUND`，与 serving daemon 一致；GUI 策略仍是 `CALLER_NOT_AUTHORIZED`。HTTP 状态与 HTTP 表推迟到 Z-6a；CLI 文案“not signed in on this home; run `hyprial login`”随 Z-4 | 是 |
| OD-Z6 | 管理器下的 logout | 在同一进程回到 bootstrap；单元选项不变 | 否 |
| OD-Z7 | daemon 侧登录的代理 | 把安装时已有的 `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY` 渲染进单元，如今天的 `PATH`。备选：客户端按次随登录请求传递 | 否 |
| OD-Z8 | `daemon run --bootstrap` 终端登录 | 从 Z-2 起标志被接受并忽略，Z-7 删除终端循环；`hyprial login` 是终端入口 | 是（使用该标志的运维） |
| OD-Z9 | 谁实现 CLI 收敛区内的 Z-4、Z-5、Z-6a–c | CLI 收敛开发者，或本线在其评审下做；在 Z-6a 之前决定 | 否 |
| OD-Z10 | bootstrap 监听器的对端 uid 检查 | 是 | 否 |
| OD-Z11 | 非默认 home 的单元命名 | home 路径的 sha256 前缀（同前置调研 OD-5） | 否 |
| OD-3 | 运行中的 daemon 是另一个包版本 | 自动重启到发起启动的安装，对两个 home 都如此（app 用户没有终端）。**owner 已决定该方向（D5）**；细节待定：备选是拒绝并给出两个版本与命令。运行中的 worker turn 会被重启打断 | 是 |
| OD-6 | app home 的位置 | 保持独立 home，放在短路径，并加 socket 路径长度预检（Y9）。`~/Library/Application Support/com.hyprial.hyprialos` 对正常用户名可接受 | 否 |
| OD-7 | 同一账号同时在 app home 与 `~/.hyprial` 的同一台机器（Y8） | 先测量；在此之前 app 设置一个不同的 `HYPRIAL_NODE_ID`（例如 `<host>-hyprialos`）。**owner 已决定 app 是独立节点（D6）**；node id 的具体取值待定 | 是 |
| OD-8 | 用户显式退出 app 时是否停止 daemon | 否；提供独立的“停止后台服务”动作（DL-7）。**owner 已决定（D3）**；该动作的设计待定 | 是 |

已决定、不再属于开放项：OD-Z1（方向，D1）、前置调研的 OD-1/2/9（D1/D4/D7）与 DL-2 不做（D8）。前置调研的 OD-4（`hyprial gui` 是否默认安装服务）与 OD-5（命名）分别并入开放项 OD-Z2 与 OD-Z11。
前置调研的 OD-10（`serve_external` 删除，并入 Z-7）与 OD-11（选项 Z）随 D1 被取代。
