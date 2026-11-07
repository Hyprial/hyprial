# 升级重启由 launchd / systemd 执行

状态：提案，待 revB2 审阅。本文只定义设计，不代表功能已实现。
基线：dev `4cae23859`。任务：0.5.5.3 designB2。

## 1. 决策

服务管理器持有 daemon 的启动权。升级器只暂停、更新和恢复已核实的服务。
服务管理器失败时，升级器不得回退到 `hyprial daemon start` 或直接派生 daemon。

macOS 的切换顺序固定为：

```text
准备安装物与回滚物
→ launchctl bootout 精确 service-target
→ 等待 print 明确报告服务不存在
→ 核实旧 daemon 与受管子进程退出
→ 安装到活动 tool 目录
→ launchctl bootstrap 原 domain / plist（有界重试）
→ 在 120 s 内核实 running ∧ 目标版本/commit ∧ 新实例 Zenoh opened
```

Linux 使用同一事务，暂停和恢复分别走 `systemctl --user stop` / `start`。
停止期间不禁用 unit，不改变 lingering，也不更换服务归属。

自动升级仍遵守“默认关闭、安装后等待确认”的既有政策。
默认情况下，定时任务只安装到隔离的候选环境，不替换活动 tool，不停止 daemon；
`hyprial daemon upgrade apply` 才执行上述切换事务。
手动 `--no-restart` 使用相同候选安装路径。

Allen 2026-10-31 裁定（原话）：“默认装到隔离的候选目录，但应该允许 hyprial daemon upgrade --autoapply 实现自动升级”。
因此增加显式开关 `hyprial daemon upgrade --autoapply`：打开后，定时任务在候选准备并预检通过后，
自动执行与 `apply` 完全相同的切换事务（同一 preflight、同一 120 s 判据、同一失败回滚），
不另设第二条切换路径。开关默认关闭，状态与当前值由 `daemon upgrade status` 报告；
关闭开关只影响之后的定时任务，不中断正在进行的事务。

警告：候选安装不等于活动安装。实现必须同时更新状态说明与契约，
不得仍把活动 tool 未变化描述成“已经替换，只差重启”。

## 2. 现场依据与当前实现

### 2.1 已报告的现场

2026-10-30 升级现场由本批任务书提供，本文未访问生产环境：

- 安装进程没有获得代理，PyPI 获取超时。
- 脚本的 `daemon start` 与 launchd KeepAlive 重启竞争。
- 现场出现双 daemon、`home-ownership-lost`，Pi 子进程孤立 73 分钟。
- `bootout` 后立即 `bootstrap` 返回 EIO 5。
- 运维等待 `launchctl print` 报告服务不存在后，恢复成功。
- 运维使用 120 s 的运行、版本与 `zenoh.session.opened` 联合判据。

这些事实是设计输入，不能作为本提案的测试 PASS。

### 2.2 基线已具备的能力

| 位置 | 基线行为 | 本提案的变化 |
| --- | --- | --- |
| `src/hyprial/shell/impl/cli/commands/common/pending_restart.py` | `uv tool install` 后调用共享重启逻辑；核实 pid、epoch、版本 | 把服务识别和暂停移到活动安装之前；增加 commit 与 Zenoh 判据 |
| `src/hyprial/shell/impl/cli/commands/daemon/service.py` | 已安装且已归属的服务走 supervisor；归属丢失时拒绝直接启动 | 保留拒绝回退；在事务开始时冻结归属，停机后不重新分类为 unmanaged |
| `src/hyprial/daemon/impl/operations/service.py` | launchd 重启使用 `kickstart -k`；systemd 使用 `restart` | 升级改用明确的暂停、安装、恢复阶段 |
| 同上 | `_unload` 已等待服务不存在，`_load` 已对 EIO 重试 | 改为接收冻结的实际 label/path 和共享 deadline；不能只用规范 label |
| 同上 | launchd 能按解析后的 executable / `daemon run` argv 识别自定义 label | 保留此能力；多个匹配 job 必须拒绝，不能取首个 job 继续 |
| `src/hyprial/daemon/impl/updates/installation.py` | `git_env()` 复制 `os.environ`，再加入无交互 Git 设置 | 不归因为“此函数删除代理”；补齐代理进入 service / installer 的边界 |
| `src/hyprial/daemon/impl/autoupdate/__init__.py` | scheduler 子进程继承 daemon 环境 | 明确自动安装的代理来源与独立协调进程 |
| `src/hyprial/shell/impl/cli/commands/upgrade/autoupdate.py` | 定时安装不重启；apply 确认；当前状态可仅凭 pid 变化判 applied | 保留确认政策；改成核实目标 commit、实例与服务归属 |
| `src/hyprial/daemon/impl/services/` | launchd KeepAlive；systemd Restart=always、KillMode=control-group | 停止后才修改活动安装；协调进程必须在 daemon 管理范围之外 |

`kickstart -k` 本身仍由 launchd 启动 daemon。
问题是原流程在安装期间没有暂停 KeepAlive，也不能证明旧子进程已退出。
不能把现有 supervisor 支持描述成完全缺失。

当前公开命令是 `hyprial daemon upgrade [status|apply]`。
本文中的 autoupgrade 指 daemon 内的自动调度，不新增顶层命令或外部定时器。

## 3. 权威边界与实现职责

### 3.1 只有一个启动方

事务必须保持以下不变量：

1. 服务管理器是服务型 daemon 的唯一启动方。
2. 活动安装变化前，旧服务已经暂停，旧实例已经退出。
3. 从 preflight 判为 managed 起，错误不得转成 unmanaged fallback。
4. 同一活动 tool 同时只有一个升级事务，包括不同 profile / home 的调用。
5. 安装失败或超时，不得用直接启动掩盖失败。
6. 恢复仍使用原 uid、domain、label/unit、home 和 state root。
7. 子进程恢复仍由 daemon 的 desired state 与 custody 机制负责。

daemon 域封装服务识别、暂停/恢复、进程归属和成功判定。
shell 只编排公开能力并通过 `CliResult` 输出。
身份变更继续使用 identity 公共面，不在升级器复制锁或身份逻辑。

普通 `daemon restart` 可以保留既有 supervisor restart 行为。
升级和 apply 必须共用本文的切换事务，不能分别维护两套停止/启动逻辑。
仅在 preflight 证明没有服务配置或 loaded job 时，才可进入原 unmanaged 路径。
本文不改变该路径；manager 查询失败不能作为“没有服务”的证明。

### 3.2 协调进程必须活过 daemon 停止

协调进程必须拥有独立、固定的运行环境，不能从被替换的 tool 延迟 import。
准备阶段在私有事务目录固化协调器代码、依赖和解释器路径，并核实可执行性。
解释器与模块不能位于即将被替换或清理的目录。
协调器运行到结束前，不得回收该环境。
这取代“尽量预加载旧解释器后续 import”作为服务升级的存活保证。

无论调用方是终端还是 daemon 托管的 worker，执行器都交给独立的一次性 job：

- launchd：单独的 `hyprial-upgrade-<transaction-id>` job，不属于 daemon job。
- systemd：`systemd-run --user` 创建独立 transient service / cgroup。
- 先收到执行器的 durable 接管回执，再允许暂停 daemon。
- 无法建立独立执行器时，在停止前报失败。

仅 `Popen(start_new_session=True)` 不能证明逃离 systemd 的 control group。
调用方退出、worker 被停止、IPC 断开，都不能取消已接管的切换事务。
一次性 job 不是新自动升级 timer，结束后只清理本事务的 job。

### 3.3 锁与事务记录

锁的主键是 canonical 活动 tool 目录与 uid。
同一安装服务多个 home 时，preflight 必须枚举并核实全部受影响服务。
第一版只允许一个服务；发现多个服务或无法列清时，在写入前拒绝升级。
不能只锁当前 home 后替换所有服务共用的二进制。

锁使用内核独占锁，不能靠超时删除锁文件抢占。
生命周期写操作、升级、apply、自动安装和 `doctor --fix` 共用此锁。
协调器接管时移交锁的持有权，不能释放后让另一个事务抢入。
manager 启动的 `daemon run` 只检查事务栅栏，不竞争协调器持有的写锁。
竞争调用返回 busy 和 transaction-id，不提交第二次 stop/start。
读取 status 不拿写锁，也不创建目录。

在所选 state root 的 `upgrade-transactions/<id>/` 保存事务记录。
目录为 0700，记录为 0600；原子替换并 fsync 文件及父目录。
协调器是单写者，daemon 和 CLI 只读取结果。
记录只描述升级，不形成第二份 inbox、业务会话或 desired state。

记录至少包含：

- transaction-id、触发方式、阶段、时间、deadline 与最终结果；
- old / candidate 的完整 commit、版本、锁回执摘要和安装物哈希；
- uid、manager、domain、实际 label/unit、unit 路径及内容摘要；
- 活动 executable 路径，以及切换前后的解析目标；
- old / new 的 pid、进程出生标识、daemon epoch；
- 暂停前 loaded / enabled 状态、受管子进程集合与停止结果；
- 每次 manager 操作的脱敏结果、成功判据和回滚结果。

不得记录完整环境、代理 URL、认证头、模型密钥或消息正文。
事务各阶段先写意图，再执行副作用，最后写观察结果。
重入先核实 manager 与进程事实，不能按上次命令退出码重复启动。

## 4. 通用事务

### 4.1 状态机

```text
preflight → prepared → paused → old-exited → installed
          → manager-starting → verifying → committed
                                     ↘ rollback-paused
                                       → rollback-installed
                                       → rollback-verifying
                                       → rolled-back | recovery-required
```

候选安装模式在 `prepared` 后结束，结果为 `awaiting-confirmation`。
候选按完整 commit 不可变保存；相同 commit 的定时检查不重复安装。
apply 确认的是记录中的确切 commit，不重新解析移动 track。
apply 读取该候选并重新 preflight，不沿用旧的进程快照。
确认后 pending 目标若已变化，拒绝切换并要求重新确认。

### 4.2 Preflight 与准备

停止前必须完成以下检查：

1. 核实安装源、确切 commit、`uv.lock` 与导出的 constraints。
2. 保留现有 local-source / tool-dir guard，不允许写入另一个安装。
3. 核实 manager 可访问，服务文件和父目录归当前 uid 且不可由其他用户改写。
4. 解析真实 job 的 argv、HOME、HYPRIAL_HOME、state root 与 executable。
5. 核实 serving pid 等于 manager 主进程，且只有一个匹配服务。
6. 保存 old epoch、进程出生标识和受管子进程清单。
7. 准备候选安装物、完整依赖与可离线使用的回滚物。
8. 预检候选的版本、commit、import 和锁回执，不启动第二个 daemon。
9. 检查新旧版本的持久状态兼容性，决定是否允许自动回滚。
10. 建立独立执行器并保存接管回执。

服务文件存在但未 loaded、文件丢失但 job loaded、归属不明、bus 不可达，
均在修改活动安装前失败。升级不自动收养旧 job，也不安装新服务。

旧进程已不在服务归属下时，报告 `SERVICE_SUPERVISION_LOST`。
不得在升级事务里顺便执行 stray repair。

### 4.3 安装与候选环境

准备阶段可在独立 `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` 安装候选，
但不得修改活动 tool、公开 shim 或服务 argv。
候选目录必须由当前事务派生，不接受浏览器或消息正文提供的任意目标。
原 tool-dir guard 保留；实现增加显式的私有 staging 校验，不整体绕过 guard。

活动安装只在 `old-exited` 后执行 `uv tool install`。
安装使用已准备的本地 wheel / 依赖缓存、确切 commit 与锁约束，且离线执行。
准备和安装都沿用当前解释器 minor version，不把 Python 升级混入本事务。
离线物料不全时必须在停止前失败，不能停机后临时访问 PyPI 补依赖。

候选 wheel 必须带可核实的 source commit，不能仅凭相同版本字符串验收。
安装后核实活动 tool 的 commit、依赖锁与 executable。
若 `uv` 更改 symlink 的解析目标，重新核实原 job 的 argv 仍指向活动安装。
若 unit 固定到了已废弃的 cache executable，须在准备时生成最小 argv 更新计划。
只替换原 executable 字段，保留环境、资源限制和日志路径，并保存原文件备份。
不允许把自定义 plist 重渲染成默认模板而丢掉代理或 `ulimit` 包装。

## 5. macOS launchd

### 5.1 冻结真实 job

以归属检查得到的 `(gui/<uid>, label, plistPath)` 为目标。
规范 label 是 `com.hyprial.hyprial.daemon`；
历史安装可能使用 `com.hyprial.daemon` 或可归属的自定义 label。
不得猜 label，也不得按名字包含 `hyprial` 批量操作。

服务发现必须返回全部匹配项。
发现两个可归属的 daemon job 时，升级拒绝并列出脱敏目标。
watchdog / reinstall 等外部 job 若也能启动同一 daemon，必须先由运维明确停用。
无法证明独占启动权时，preflight 不通过。
外部运维脚本必须委托同一升级事务，不得保留另一个 `daemon start` 循环。

### 5.2 暂停与退出

1. 写入 `pause-intent`。
2. 执行 `launchctl bootout gui/<uid>/<actual-label>`。
3. 轮询同一 target 的 `launchctl print`。
4. 核实旧 daemon 的进程出生标识已消失。
5. 通过既有 custody 公共面核实旧受管子进程已经退出。
6. 写入 `old-exited`。

`bootout` 返回 0 不代表第 3 至第 5 步已完成。
轮询间隔为 250 ms，暂停总预算为 45 s。
每次命令的 timeout 不得超过剩余预算。

只有 manager 的明确不存在响应才能完成第 3 步。
基线识别形状为退出 113，且包含精确的
`Could not find service "<label>" in domain for user gui: <uid>`。
其他 OS 形状必须先有平台 fixture 才能加入识别。
EIO 5、权限错误、超时、domain 不存在均不能当作服务已经卸载。

进程退出必须使用 pid 与出生标识；`kill(pid, 0)` 或 socket 消失不够。
暂停超时或残留进程时，不安装、不 bootstrap、不直接杀不明 PID。
保存现场后进入恢复判断；无法证明旧实例退出时只能 `recovery-required`。

### 5.3 恢复与 EIO 重试

完成活动安装后，执行 `launchctl bootstrap gui/<uid> <original-plist-path>`。
若文件按计划改过 executable，必须先核实新文件摘要与归属。
不调用 `daemon start`，不把 `kickstart -k` 与 bootstrap 叠加使用。

从首次 bootstrap 前开始计算一个 120 s 的启动与验证窗口。
bootstrap、print、IPC、日志读取和 sleep 共用该窗口。

- bootstrap 返回 0：只进入 verifying，不宣告成功。
- bootstrap 返回 EIO 5：先 print 实际 target，确认是否已经 loaded。
- 已 loaded：核实实际 job 与本事务安装相符，再进入 verifying。
- 明确未 loaded：按 250 ms、500 ms、1 s、2 s 的间隔重试。
- 最多执行 5 次 bootstrap，且不得越过 120 s deadline。
- 其他错误立即保留 typed failure，不盲目重试。

若响应丢失后发现已 loaded，不得再发一次 bootstrap 或 kickstart。
KeepAlive 可以在验证阶段按原配置重启失败实例，但不会触发直接启动补救。
达到 deadline 后先暂停失败候选，再决定回滚。

## 6. Linux systemd

### 6.1 暂停使用 stop，恢复使用 start

systemd 路径要求可访问同一 uid 的 user manager。
沿用现有 user-bus 判定，不用 sudo，不切换 system manager。
显式 DBUS 地址不可达时必须报错，不能假定 unit 不存在。

```text
systemctl --user stop <verified-unit>
→ 等待 inactive/dead、MainPID=0、无挂起 start/restart job
→ 核实旧进程与 cgroup 中受管子进程退出
→ 安装活动 tool
→ 必要时 daemon-reload
→ systemctl --user start <same-unit>
→ 验证
```

显式 stop 会抑制 `Restart=always` 的自动重启。
不使用 `disable --now`，不更改原 enabled / lingering 状态。
只有 unit 内容确实变化时才 `daemon-reload`。
manager 的 start 成功或 `Type=simple` 的 active 状态都不等于业务就绪。

停止预算同为 45 s，启动与验证共用 120 s。
命令等待的剩余时间、unit 的 `TimeoutStopSec` 和进程检查必须受同一预算约束。
如果 stop 超时，必须查询 stop job 与进程现状。
不能在 systemd 仍 deactivating 时安装或 start。

当前模板的 `KillMode=control-group` 只覆盖该 unit 的进程。
容器、虚拟机或脱离 cgroup 的进程仍由已有 custody 公共面检查。
不得用全局 `pkill`、容器 prune 或按 harness 名称清理进程。

## 7. 安装器代理环境

### 7.1 来源与优先级

代理快照在 preflight 解析一次，并传给全部联网子进程。
来源固定为：

1. 手动 CLI 显式携带的代理变量；显式空值表示不使用该变量。
2. 受管服务显式配置的升级代理环境。
3. 都未配置时直连，并在开始前说明 `proxyConfigured=false`。

自动任务没有交互 shell，使用 daemon 的服务环境作为第 2 项。
不得读取 `.zshrc` / `.bashrc`，也不得假定 launchd 继承终端 export。
不得把 `workerProxy` 的厂商分流设置当成安装器代理配置。

升级代理配置复用服务的受保护环境来源，不新增默认代理地址。
launchd 可以从经核实的 plist EnvironmentVariables 获得配置；
systemd 使用服务用户已有的受保护 EnvironmentFile / 环境配置。
代理包含凭据时，文件须为 0600，不能把值写到公开 plist 或 unit。
这类部署须由受保护的服务启动包装读取环境；配置后另验 daemon 的有效环境。
只报告键名、来源和是否配置，不返回变量值或包装文件内容。

### 7.2 传递规则

保留并显式传递以下变量及其小写形式：

```text
HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
http_proxy https_proxy all_proxy no_proxy
```

每组大小写变量按来源优先级解析，选中来源后不再从低优先级补同组值。
选中来源含冲突值时，在停止前报配置错误，不让 Git 与 uv 各自挑不同代理。
选中空值时清除同组大小写变量；非空值则把两种写法归一成相同值。
不能在失败后删除代理再偷偷直连重试。
`NO_PROXY` 原样传递，不能为安装扩大成 `*`。

同一快照传到：

- 远端 ref/commit 解析；
- exact-commit fetch；
- `uv export` 与候选 `uv tool install`；
- 准备安装物与回滚物的下载；
- 独立执行器的安装子进程。

继续加入 `GIT_TERMINAL_PROMPT=0`、SSH BatchMode 与公网 extraHeader 清理。
保留 HOME、PATH、UV 目录、CA 信任变量和 systemd user-bus 所需变量。
不得用 `env -i` 丢掉这些前提，也不得继承 worker 的受限厂商环境作为升级配置。

执行器跨停止边界时，从内存或受保护的本地交接文件传递代理快照。
交接文件不写入事务 JSON，事务结束即清理；不得放入 argv 或日志。
错误输出须移除代理 URL 中的凭据与认证头，不能原样回显 uv/Git stderr。

注意：当前 `git_env()` 已复制环境，无法仅凭源码证明现场代理丢失的具体位置。
实现验收必须覆盖“终端有代理、服务无代理”和“服务有代理、installer 丢代理”两种情况。

## 8. 成功判据与状态报告

### 8.1 120 s 内的联合判据

只有同一候选实例满足全部条件，事务才能 `committed`：

1. manager 报告实际 job/unit running。
2. 对选定 home 的 IPC 探针报告 `running=true`。
3. serving pid 等于 manager pid；出生标识与旧进程不同。
4. 新 daemon epoch 与旧 epoch 不同。
5. 运行版本等于候选版本，运行 build commit 等于候选完整 SHA。
6. 活动安装及依赖锁回执与候选一致。
7. 本次新 epoch 已产生 `zenoh.session.opened`，且会话当前仍为 open。
8. 不存在仍运行的旧 daemon 或未处理的旧受管孤立进程。

pid、epoch、commit 和 Zenoh 观察必须来自同一代 daemon。
不能把第 1 次新实例的 opened 事件配上第 2 次实例的 running。
旧日志、仅版本相同、仅 pid 变化、仅 bootstrap 返回 0 均不够。

当前探针与 transport 日志不一定提供上述全部事实。
实现须在 daemon 公共状态里增加运行 build commit、epoch 关联的 session-open 事实。
日志事件继续保留，并携带 epoch / pid / 时间以供复核。
缺少这些字段时结果为 unverified，不能把磁盘安装版本填成运行版本。

日志证据从 bootstrap 前保存的游标读取，处理轮转与截断。
找不到事件但有权威的本 epoch session-open 状态时，可使用该状态附带的事件标识。
只有旧事件或读取失败时不得通过。

### 8.2 恢复与业务可用性单独报告

`committed` 只证明服务切换完成，不证明所有模型或 adapter 已恢复。
沿用现有 restore phase / pending connectors 的后续检查。
报告分开列出：服务切换、restore、孤立进程检查和通知送达。
restore 未完成时不能宣称“全部恢复”。

升级步骤沿用现有 daemon 域 runner 和 marker。
自动候选安装不得对活动状态执行这些迁移。
apply 在成功切换后执行步骤，并单独报告失败；不得因步骤失败再次启动 daemon。

`pendingRestartState=applied` 必须通过本节的目标身份核实。
不同 pid 可能仍是旧版、错误 profile 或失败候选，不能据此清除 pending。
apply 不得在“当前没有 daemon 回应”时直接清除 pending。
需要先核实事务、manager 和活动安装；无法核实时保留记录。

stdout 仍是一个带 `ok` 的 JSON 对象，进度只走 stderr。
拟新增的事务字段及 reason 值必须随实现进入 contract，本文不发布新错误码。
外层安装失败继续使用 `UPGRADE_FAILED`，切换失败使用 `UPGRADE_RESTART_FAILED`；
服务归属错误保留现有码，阶段与原因放入结构化 data。

## 9. 失败、回滚与恢复

### 9.1 回滚前提

停止前必须备齐旧版本的安装物、依赖、解释器和原 unit 文件。
回滚不能依赖当时的 PyPI、代理或仍存在的 uv 缓存。
缺少完整回滚物时，默认拒绝无人值守切换。
不允许假定一次失败的 `uv tool install` 没有改动活动环境。

自动回滚只允许新旧持久状态兼容，或已证明候选未写入持久状态。
本事务不自动把 live inbox / identity / desired state 还原成旧快照。
新 daemon 已接收消息后恢复旧快照会丢消息，因此不得这样回滚。
有不兼容迁移或无法判断兼容性时，候选保持暂停，转 `recovery-required`。

### 9.2 按失败阶段处置

| 失败点 | 处置 |
| --- | --- |
| prepared 前 | 保持原服务不动；返回准备失败 |
| bootout / stop 结果不明确 | 查询同一目标与旧实例；不安装。旧服务仍运行则保留；已暂停且旧实例退出则由 manager 恢复原安装；无法核实则报告待恢复 |
| 已暂停但旧实例残留 | 不安装；保留进程证据并要求恢复处理 |
| 旧实例已退出，活动安装失败 | 从离线回滚物恢复旧 tool，核实原 unit 后由 manager 启动 |
| bootstrap / start 失败或 120 s 未通过 | 先暂停候选并证明候选退出，再恢复旧 tool 与原 unit |
| 候选状态不兼容 | 不降级、不还原业务快照；保留现场，等待受权恢复 |
| 回滚启动仍失败 | 暂停原目标并核实结果；写入 `recovery-required`，不循环安装或启动；无法核实暂停时明确报告 unknown |

自动回滚最多 1 次。
回滚启动也有独立 120 s 验证窗口，使用旧版本/commit 和全新 epoch 判据。
候选启动失败的 120 s 不能被重试或 follow-up 隐式延长。
准备、暂停、安装、启动与回滚分别记录预算与耗时；120 s 不含下载时间。
活动离线安装沿用 300 s 上限，超时视为可能已修改，必须走回滚判断。

`rolled-back` 表示旧服务已核实恢复，不表示升级成功。
原升级命令仍返回 `ok:false` 和退出 1，同时报告恢复状态。
仅在目标候选验收成功后才清除该候选的 pending 记录。
回滚后保留失败候选与原因，避免定时任务立即重复安装同一坏 commit。
再次尝试同一失败 commit 需要操作员显式确认；新 commit 可重新准备。

### 9.3 协调器崩溃与失联

协调器重新进入时，先读取事务意图，再核实 manager、安装与进程事实。
已 loaded 且身份正确的服务只验证，不重复 bootstrap/start。
活动安装可能半完成时，只有证明全部实例退出后才继续安装或回滚。
证据矛盾时停止写操作，不通过删锁、删 pid 文件或清 socket 强行恢复。

主机重启可能使 enabled 服务从半完成的 tool 启动。
实现必须给原服务增加启动前的事务栅栏：
未完成的活动安装阶段不得进入 `daemon run`，应只报告受阻并等待协调器恢复。
栅栏运行文件必须独立于被替换的 tool，且不启动 daemon。
若旧服务无法安装或验证该栅栏，则在 preflight 拒绝可中断的自动切换。
栅栏的部署、升级和删除须纳入原 unit 的可回滚更新计划。

先保存失败记录，再尝试通知 owner。
daemon 不可用时，通知失败不能覆盖事务结果；保留本地待发通知。
CLI status / doctor 通过公开升级能力展示事务与明确恢复动作。
后续运维只允许恢复该 transaction-id；不提供通用直接启动补救建议。

### 9.4 接受旧版 pending 记录

旧版 pending 可能表示活动 tool 已替换，却没有候选目录。
首次读取时必须按记录中的确切 commit 核实活动安装，不能伪造 staged 状态。
apply 可以为该目标补齐准备阶段，但仍须先核实旧运行实例与回滚物。
无法取得旧版本离线物料时，拒绝自动切换并给出阻塞原因。
旧记录只含 pid、未含 epoch 或完整 commit 时，状态为 unverified。
不得仅凭新 pid 判定 applied，也不得删除记录让下次启动隐式承担恢复。

## 10. 证明没有双启动的测试

实现必须先提交失败测试，再实现修复。
本设计提交不新增测试或产品代码；以下是后续实现的验收清单。

### 10.1 确定性测试

使用注入的 manager runner、单调时钟、进程出生标识和安装器。
mock 只能证明命令顺序与判定，不能代替真实服务验收。

| 场景 | 必须断言 |
| --- | --- |
| launchd KeepAlive 在停止请求后尝试 respawn | 活动安装仅在 target 明确不存在且旧实例退出后发生；direct spawn 次数为 0 |
| bootout 返回 0，print 仍 loaded 多次 | bootstrap 与活动安装均未发生；不存在响应后才继续 |
| print 返回 EIO / 权限错误 / timeout | 不认作 unloaded；不安装、不直接启动 |
| bootstrap 返回 EIO，但随后 print 已 loaded | 不再次 bootstrap；只核实当前实例 |
| bootstrap EIO 且明确不存在 | 按固定退避重试，次数与 deadline 有界 |
| 自定义 label / executable 包装 | bootout、print、bootstrap 均使用实际目标；保留原环境与资源限制 |
| 两个匹配 job、foreign unit、缺失文件、user bus 不可达 | preflight 失败；0 次活动安装、0 次 stop/start |
| 旧 pid 仍活、pid 被复用、孤立 Pi 子进程 | 不因 socket 消失或 pid 不同而通过；不启动第二代 |
| manager running，但 IPC 是旧 epoch / 错 commit | 验证失败，pending 不清除 |
| 日志只有旧 opened，或 opened 属于已退出的新实例 | 验证失败；不得拼接不同实例证据 |
| 单次探针消耗全部剩余时间 | 总验证不超过 120 s；后续不再 sleep / probe |
| systemd stop 后 Restart=always | 没有 start 请求就不再运行；安装时 cgroup 已清空 |
| 同时手动、apply、timer、doctor-fix | 只有一个写事务；其他调用 busy，不重复暂停或恢复 |
| worker 发起后退出 / daemon 停止 | 独立协调器继续；结果落盘，不依赖原 IPC |
| 安装器半写失败 / SIGKILL | 不以当前环境推断成功；恢复离线旧安装，再验证 |
| 候选已写不兼容状态 | 不自动降级，不回灌业务快照 |
| 自动升级关闭 / 打开 / no-restart | 关闭不安装；打开只写候选；不停止或重启活动 daemon |
| 自动升级打开且 `--autoapply` | 候选预检通过后走与 apply 相同的事务；失败按 §9 处置，同一坏 commit 不重复切换 |
| 没有 pending / pending 已正确 applied / 只是 pid 变化 | 幂等 no-op / 核实后清除 / 保留等待，三者分开 |

代理测试在 Git、uv export、uv install 的最终 subprocess 边界断言环境。
覆盖大写、小写、ALL_PROXY、NO_PROXY、显式空值、大小写冲突和无配置。
使用含假凭据的代理 URL，断言 stdout、stderr、记录和通知均不含假凭据。
另测服务代理未传给 installer 的红例，不能只测 `git_env()`。

### 10.2 隔离的真实服务测试

必须分别在 disposable macOS 用户/虚拟机和 Linux 用户/容器 user manager 上执行。
生产机器上的固定 label 与测试 label 必须隔离，不能借用日常 daemon。

测试环境必须显式设置：

```text
HOME、HYPRIAL_HOME、HARNESS_STATE_DIR、socket、UV_TOOL_DIR、UV_TOOL_BIN_DIR
HYPRIAL_NETWORK_ISOLATED=1
NO_PROXY=127.0.0.1,localhost,::1
```

HOME 及服务目录都在测试资源内；不读取或修改真实用户的 LaunchAgents / systemd 配置。
使用本地 Git remote、锁定包源和代理记录器，不使用生产凭据或模型账户。

每个平台至少执行以下场景：

1. 在 KeepAlive / Restart=always 下切换 A → B。
2. 阻塞旧 daemon 退出，核实安装器未改活动 tool，且没有第二个 daemon。
3. 延迟 job 卸载，核实没有提前 bootstrap/start。
4. 让候选无法打开 Zenoh，核实 120 s 失败后只回滚 1 次。
5. 在暂停后、安装中、bootstrap 响应丢失后杀死协调器，再恢复事务。
6. 在安装中重启测试主机，核实栅栏阻止半安装实例启动。
7. 由真实服务托管的合成 worker 发起 apply，核实协调器独立存活。
8. 启动合成 Pi 子进程，核实旧子进程清理、desired state 恢复与新进程归属。
9. 经本地代理获取依赖，核实请求到达代理，NO_PROXY 回环请求不经过代理。
10. 连续执行 20 次切换，核实每次最多只有一个活动 daemon，且无残留子进程。

“最多一个”必须从 daemon 进入/退出的事件账本和进程出生标识持续核实。
只在切换前后各跑一次 `ps` 会漏掉短时双启动，不能通过。
证据同时保留 manager 命令序列、实例区间、commit、epoch、Zenoh 事件与清理结果。
cleanup 必须在 finally 中恢复/卸载测试 unit，并检查测试进程与一次性 job 已退出。

## 11. 实现切片、契约与验收状态

按以下顺序实现，每片保留红测与通过记录：

1. **事务与准备**：tool 级锁、独立执行器、代理快照、候选与离线回滚物。
2. **服务切换**：真实目标冻结、暂停/退出、活动安装、恢复和共享 deadline。
3. **验证与恢复**：运行 commit、epoch 关联的 Zenoh 事实、回滚、崩溃栅栏。
4. **CLI 与自动调度**：统一 upgrade/apply 路径，候选等待确认与状态语义。
5. **真实双平台验收**：上述故障注入、连续切换和进程泄漏检查。

不得先交付一个“失败就 daemon start”的临时升级路径。
跨切片期间未具备安全前提的入口必须拒绝服务型切换，不能假装支持。

首次采用新升级器需要单独的部署步骤。
旧版 CLI 执行自身升级时仍运行旧逻辑，不能因目标版本含修复就视为安全。
须由受权运维在 daemon 之外运行经核实的新协调器，完成首次切换。
该次部署也须遵守暂停、退出、安装、manager 恢复和联合验证顺序。
未部署启动栅栏的旧 unit，必须在停止前完成栅栏与回滚预检。
本设计不授权执行生产部署；部署记录须保存所用协调器 SHA 与双平台结果。

实现 PR 必须更新：

- `contract/daemon-lifecycle/service-supervision.md`：升级的暂停/恢复例外、归属与唯一启动方；
- CLI black-box / autoupdate status 语料：候选状态、pending 与失败结果；
- daemon status / 日志契约：运行 commit、epoch 关联的 session-open 事实；
- `.agents/skills/hyprial-e2e/`、运维升级说明与自动升级说明；
- `docs/design/daemon-upgrade-steps-2026-10-20.md` 的执行顺序说明；
- ROADMAP：真实双平台升级与恢复的发版义务和证据入口。

实现验证先跑直接测试，再跑所改目录与全量 `tests/`。
同时运行结构门、模块边界门、ruff 与 `./contract/run-all.sh --isolated-only`。
真实 macOS / Linux 服务验收单独记 PASS / FAIL / BLOCKED。

本提案的验证状态：

| 项目 | 结论 |
| --- | --- |
| 基线源码、公开 help 与现有契约检查 | 已检查，见第 2 节路径 |
| `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` | PASS |
| `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` | PASS |
| `uv run ruff check .` | PASS；先执行 `uv sync --extra test` 补齐检查依赖 |
| 中文文档检查 | 0 处错误；“实现”指代码开发，不是增长类套话，保留该词 |
| 全量 pytest / contract runner | 未执行；本提交仅新增设计文档 |
| 2026-10-30 生产事故复现 | UNKNOWN；只采用任务书提供的现场 |
| 新行为的单元、故障注入、真实服务测试 | UNKNOWN；设计阶段未实现、未执行 |
| 生产升级或服务操作 | 未执行；不属于本任务授权 |

Contract-Impact: design-only；本提交不改变已发布行为，未来实现须同步上述契约。
Skill-Impact: not-needed: 本提交不改变操作入口；实施时必须同步上述技能与运维文档。

## 12. 依据

- 本批任务书：`wt/fixes-0553-pac/brief.md` 的 designB2 现场与目标；
  第 2.1 节已把必要事实保存到本文，任务书路径不作为长期唯一证据。
- 现有服务契约：[service-supervision.md](../../contract/daemon-lifecycle/service-supervision.md)。
- 升级步骤设计：[daemon-upgrade-steps-2026-10-20.md](daemon-upgrade-steps-2026-10-20.md)。
- 升级选源与约束：[upgrade-tracks.md](../guides/upgrade-tracks.md)。
- macOS 本机 `launchctl(1)`：bootstrap/bootout 管理服务定义；
  `kickstart -k` 会杀掉正在运行的实例后启动。print 的实际输出形状仍需平台测试冻结。
- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)：
  `Restart=` 不会覆盖显式 stop；stop/restart 的退出与超时由 manager 管理。
- [uv environment](https://docs.astral.sh/uv/reference/environment/#externally-defined-variables)：
  uv 读取 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY。
  本文的大小写冲突拒绝规则属于产品设计，不是 uv 文档承诺。
