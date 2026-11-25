# Z-8 每 home OS 服务：实施计划（Z-8a / Z-8b / Z-8c）

- 日期：2026-11-15。作者：PAC wf-2d054901dd8c4903aa47d75b6e6286db analyze 节点。
- 基线：`fc240df33f6fadf94dde365ad94082221d33953c`（PR #2830 head，`feat/per-home-os-service`）。
- 范围依据：[Z-8 交接](daemon-first-z8-per-home-service-handoff-2026-11-15.md)；设计依据：
  [daemon 先行设计](daemon-first-init-login-decoupling-2026-11-15.md) §3.7、§3.8、§5.3、§6、§9。
- 证据标记：**实测**（本机 fc240df33 上跑过）、**读代码**、**UNKNOWN**、**BLOCKED**。
- 本文只定实施计划；行为不变式以交接文档为准。`S/` = `src/hyprial/`。

## 1. Z-8a：`operations/service.py` 机械拆分

### 1.1 对本地候选 80d58223 的评估（实测）

候选 `80d5822344df30646adcba969015f4a0c3ebaa4e`（本地分支
`refactor/2830-z8a-local-reference`，fc240df33 的直接子提交，未推送）：

- 拆分形状与交接一致：`__init__.py` 34、`types.py` 90、`templates.py` 89、`identity.py` 56、
  `supervisor.py` 245、`manager.py` 598 行；`operations/` 降到 6 个直接文件，新包 6 个文件，
  全部 ≤1000 行，结构门通过（估算→实测行数）。
- 顶层定义对照（旧 `service.py` 与新包五文件排序 diff）：只有三处结构性差异——
  新增 `SupervisorCommands` 基类（`ServiceManager(SupervisorCommands)`）、
  `_require_service_home` 方法改为 `identity.py` 的模块级函数
  （`require_service_home` / `require_account_home` / `unit_path`）。即**不是逐字纯移动**，
  但行为等价；PR 正文必须披露这两处取舍。
- **发现一处真实回归（实测）**：候选把 `user_bus_state` 的调用点移进 `supervisor.py` 的模块
  命名空间，而 `tests/shell/cli/lifecycle/service/test_daemon_service_cli.py:419` 仍 patch 包
  `operations.service` 上的 `user_bus_state`（`__init__.py` 的再导出对 monkeypatch 无效）。
  候选上跑焦点套件：1 failed（doctor 把 `user-bus-address-unreachable` 误报为
  `user-bus-unavailable`），161 passed。把该 patch 目标改到
  `operations.service.supervisor` 后：162 passed（实测，含 `test_home_isolation`、
  `test_service_supervision`、`test_supervisor_routing`、`test_supervisor_recovery`、
  `test_launch`）。

**结论：复用并修（reuse-with-fix）。** 不推倒重写；也不能原样采信。Z-8a 执行者：
移植该拆分，补上 `test_daemon_service_cli.py` 的 patch 目标修正，PR 正文披露
`SupervisorCommands` 基类与 identity 模块级函数两处非纯移动。

### 1.2 消费方与 monkeypatch 面清单（读代码 + grep 实测）

产品侧只有三处导入 `hyprial.daemon.impl.operations.service`，全部是公开名，包化后路径不变：

- `S/daemon/impl/__init__.py:28-35`：`LAUNCHD_LABEL` / `ServiceConfig` / `ServiceManager` /
  `ServiceOwnershipError` / `ServiceStatus` / `ServiceTemplates`。
- `S/daemon/interface.py:98-101`：惰性映射指向 `operations.service`（包化后仍合法）。
- `S/daemon/impl/facade/types.py:63-69`：`TYPE_CHECKING` 导入。

测试侧模块路径敏感点（monkeypatch 跟随被移动的内部）：

| 文件 | patch 目标 | 拆分后落点 |
|---|---|---|
| `tests/daemon/application/composition/service/test_home_isolation.py:42,59` | `service.subprocess.run` | `operations/service/supervisor.py`（候选已改） |
| `tests/daemon/application/composition/service/test_service_supervision.py:11` | 模块别名上 patch manager 内部 | `supervisor`（候选已改） |
| `tests/shell/cli/installation/restart/test_launch.py:147,159` | `user_bus_state` | `supervisor`（候选已改） |
| `tests/shell/cli/lifecycle/service/test_daemon_service_cli.py:419` | 包上 `user_bus_state` | **候选漏改**，改指 `operations.service.supervisor` |
| 同上 `:180` | 类属性 `ServiceManager._run` | 包再导出即可，不动 |

其余测试只导入公开名（`test_daemon_service.py`、`test_mutation_collisions.py`、
`test_harness_command_pinning.py` 等），无需改动。无测试导入该模块的 `_` 私有名
（grep 实测）。`hyprial.daemon.impl.onboarding.operations.service` 是另一个模块，不在本范围。

### 1.3 Z-8a 验收

现有测试全绿即通过（无新测试）。焦点套件 + 第 7 节全部门禁。

## 2. Z-8b：每 home 单元身份

### 2.1 命名与规范化规则（建议，即 OD-Z11 默认）

- 默认 home 判定：`os.path.realpath(config.hyprial_home) == os.path.realpath(config.home / ".hyprial")`
  （两边都过 realpath；`config.home` 是操作 home）。默认 home 保持
  `com.hyprial.hyprial.daemon` / `hyprial.service` 与现有单元路径，逐字节不变（代理渲染除外，见 2.4）。
- 非默认 home：`<id> = hashlib.sha256(realpath_str.encode("utf-8")).hexdigest()[:12]`；
  launchd label `com.hyprial.hyprial.daemon.<id>`；systemd 单元 `hyprial-<id>.service`。
- 规范化只含 `os.path.realpath`（解析符号链接、消除 `.` / `//` / 尾部斜杠）。**不做大小写折叠、
  不做 Unicode 正规化**：同一解析路径字节串 → 同一 id；大小写不敏感文件系统上仅大小写不同的
  两个写法会得到两个 id（目录是同一个，label 是两个）。别名限制成文并以测试向量锁定
  （`test_case_variant_spelling_is_a_distinct_unit_id`）。**更正（audit C1，2026-11-15 实测）：
  本文件初稿称「macOS 对已存在目录 realpath 返回文件系统存储的写法，已存在目录天然归一」，
  实测为假——`os.path.realpath('/users/<name>')` 原样返回调用方写法，realpath 不改写已存在
  目录组件的大小写，只解析符号链接；因此两个大小写写法就是两个 id，使用方必须对同一 home
  恒用同一拼写，否则同一目录会装出两个单元。**不存在的父链按调用方写法哈希。
  48 bit 前缀的意外碰撞可忽略；label/单元名只含小写十六进制，systemd 无需 `%` 转义。
- 单元内 `HYPRIAL_SERVICE_UNIT` 携带派生名（launchd 模板已用 `{{LABEL}}`；systemd 模板
  `:15` 的硬编码 `hyprial.service` 改为 `{{UNIT_NAME}}` token）。

### 2.2 硬编码点清单（读代码，行号是 fc240df33 上的 `operations/service.py`）

拆分后落点按 Z-8a 的包结构标注：

- `identity.py`：`LAUNCHD_LABEL` 常量保留为默认 label；新增
  `service_label(hyprial_home, home)` / `systemd_unit_name(...)` / 派生 `unit_path`。
- `manager.py`：`unit_path` 属性（`:228-237`）、`status()` 的 systemctl 参数
  `"hyprial.service"`（`:381-431` 两处元组）、`_reports_not_loaded`（`:757-773`，launchd
   stderr 精确行含 label；systemd `Unit hyprial.service could not be found.`）、
  `_is_attributed`（`:775-791`，launchd label 检查；systemd
  `'Environment="HYPRIAL_SERVICE_UNIT=hyprial.service"'`）、`_load` / `_unload` / `restart`
  中的 label 与单元名、`_launchd_jobs` 的正则/label。
- `supervisor.py`：`_run` 的 account-home 守卫不变；无 label 硬编码。
- `S/shell/impl/cli/commands/daemon/service.py`：`:468` 与 `:577` 的
  `("systemctl", "--user", "start|stop", "hyprial.service")` 改用 `status.unit`；
  `:63` 程序路径解析（见 2.3）。
- 模板：`launchd.plist.template`（module 形式 argv token，见 2.3）、
  `systemd.service.template`（`{{UNIT_NAME}}`）。
- `S/daemon/impl/configuration/launchd_jobs.py`：见 2.3。
- 移除的拒绝只有 `service-home-nondefault`（`service.py:588-593`）。
  保留：`hyprial-home-outside-operating-home`（`:580-587`）、
  `service-account-home-mismatch`（`:975-988`，`_run` 内）、
  真实单元目录 tripwire（`tests/support/runtime/environment.py:86-112`）。
- 契约：`contract/daemon-lifecycle/service-supervision.md` `:17-20`、`:30-34` 的
  “固定 label 留给默认 home / 非默认用 --no-service”两段改写为派生命名规则；
  PR 正文写 `Contract-Impact:`。

以下测试硬编码旧名但构造的是默认 home 配置，**默认 home 逐字节不变即无需改**
（Z-8b 执行者用全量焦点套件验证此断言）：`test_supervisor_routing.py`、
`test_supervisor_recovery.py`、`tests/daemon/upgrade/test_steps.py`、
`tests/shell/cli/installation/restart/**`。需要改的是
`test_home_isolation.py::test_nondefault_home_refused_before_runner`（`:17-36`）：
改为“非默认 home 派生独立单元且只触达 fake runner”的正向测试。
保留不动的安全测试：`test_daemon_service.py:230,267`（outside-operating-home）、
`test_home_isolation.py:51` 与 `test_daemon_zenoh_failloud.py:205`（account-home-mismatch）、
`test_tripwire.py`。

### 2.3 程序路径与 launchd 扫描器

- console script 仅当属于本安装时使用：`shutil.which("hyprial")` 读 shebang 解释器，
  与未 resolve 的绝对 `sys.executable` 相等才采用（2026-11-16 起，见 §9：不再 resolve，
  否则 venv 解析到基础 CPython）；否则 module 形式
  `[sys.executable, "-m", "hyprial.cli", "daemon", "run"]`。建议 `ServiceConfig` 增加
  `executable_args: tuple[str, ...] = ()` 字段承载 `-m hyprial.cli`。
- 模板：systemd `ExecStart={{EXECUTABLE_ARG}} daemon run` 不变——module 形式下
  `EXECUTABLE_ARG` 渲染为 `"<python>" -m hyprial.cli`（现有 `_quote_systemd` 逐段加引号）。
  launchd 模板在 `{{EXECUTABLE}}` 与 `daemon` 之间新增 `{{EXECUTABLE_ARGS}}` token
  （渲染为零行或两行 `<string>-m</string>` / `<string>hyprial.cli</string>`）。
- `argv_runs_daemon`（`launchd_jobs.py:79-93`）新增第 4 形态：
  `argv[0]` 同文件匹配 `executable` 且 `argv[1:5] == ["-m", "hyprial.cli", "daemon", "run"]`；
  Program/包装壳形态不变。
- `_launchd_jobs` 扫描器加 home 匹配：`read_launchd_plist` 增取
  `EnvironmentVariables.HYPRIAL_HOME`（新增辅助函数，不改现有三元组形状），
  realpath 与本 manager 的 `config.hyprial_home` 相等才认领。同一安装两个 home 共用一个
  可执行文件时，可执行文件匹配不足以区分，这是防“认领别人家 job”的必要条件。
- App Translocation：路径含 `/AppTranslocation/` 判为不稳定。显式 `daemon service install`
  给出警告字段仍允许；Z-8c 的自动安装跳过（回退 detached）。判定规则需 Mac 实测（§8）。

### 2.4 代理变量渲染与 0644 问题

- 安装时读 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（大写三个，与 OD-Z7 口径一致），
  渲染进单元：launchd 走现有 `_escape_xml`，systemd 走 `_quote_systemd`；变量不存在时
  整个键省略，不渲染空值。
- 记录在案的问题：`_atomic_write`  chmod 0644，代理值若含 `user:pass@host` 凭据，
  单元文件在多用户机器上可读。**owner 2026-11-15 确认选项 (a)**：照样渲染
  （含 userinfo）、保持 0644，不做过滤、不改 0600；作为已接受的已知问题登记为
  KP-081（`docs/reference/known-problems.md`）。注意模板字节变化会让既有默认 home
  安装 `templateMatches=false`，doctor 报漂移行直到重装——PR 正文写明。

## 3. Z-8c：启动原语自动安装

### 3.1 调用者追踪（读代码）

`_launch_daemon_process`（`S/shell/impl/cli/commands/common/daemon_start.py:339-382`）
经 `services._launch_daemon_process` 注册（`runtime/services.py:117,178`），调用方四个：

| 调用方 | 位置 | 性质 | Z-8c 语义 |
|---|---|---|---|
| `daemon start` | `daemon/commands.py:94` | 显式 | 允许安装+管理器启动 |
| `init` | `init/init.py:175`（`_complete_initialization`） | 显式 | 允许安装+管理器启动 |
| login flow | `login/flow.py:536` | 显式（身份提交后） | 允许安装+管理器启动 |
| upgrade 待重启 | `common/pending_restart.py:336` | 显式命令内部的重启 | **不自动安装**（`autoinstall=False`），保持今天的回退 |

未来 Z-4 的隐式 ensure 传 `implicit=True`；Z-8c 只把参数与读值埋好。

### 3.2 原语新流程与环境变量语义

```
supervisor = installed_supervisor()
有可归因已装单元 且 HYPRIAL_DAEMON_AUTOSTART≠0 → start_supervised_daemon
否则：
  implicit 且 HYPRIAL_DAEMON_AUTOSTART=0 → CliError("DAEMON_AUTOSTART_DISABLED", ...)
  autoinstall 允许（参数允许 且 HYPRIAL_SERVICE_AUTOINSTALL≠0）：
      平台支持 且 程序路径稳定 → manager.install()
      install 抛 ServiceOwnershipError / ServiceCommandError → 不致命，stderr 一条
      notice（写单元名与 daemon service uninstall），回退 detached
      成功 → installed_supervisor() → start_supervised_daemon
  其余 → 今天的 detached spawn（_launch_daemon_process_locked）
```

- `HYPRIAL_SERVICE_AUTOINSTALL=0`：原语内永不装单元；显式 `daemon service install` 不受影响。
- `HYPRIAL_DAEMON_AUTOSTART=0`：拦截所有经管理器的启动（实现比本节初稿更严，2026-11-15
  集成更正）：隐式调用返回 `DAEMON_AUTOSTART_DISABLED`；显式命令不报错、不停 daemon——
  绕过管理器（含已装可归因单元），直接今天的 detached spawn，显式启动不是静默 no-op
  （锁定测试 `test_autostart_opt_out_bypasses_even_an_installed_supervisor`）。
  原先「已装单元不被 opt-out 拦截」的初稿与实现不符；`test_supervisor_routing.py:90,133`
  等 patch 内层 `_launch_daemon_process_locked` 的用例已显式 `HYPRIAL_DAEMON_AUTOSTART=1`
  回选管理器路径。
- 两个变量由 `tests/conftest.py:64-130` 的 autouse fixture 与
  `tests/support/runtime/environment.py:15-45` 的 `isolated_environment()` 设置为 `0`；
  产品代码永不设置。`isolated_environment` 目前整批清掉 `HYPRIAL_*` 再重建，两个变量
  加进重建段。
- 单元测试继续 fake runner；三道测试安全保护（§2.2 末）不动。
- 不碰 `shell/impl/cli/commands/common/support.py`（CLI 收敛区）。

### 3.x GUI 边界（2026-11-16 追加，owner 决定）

本计划写成时 GUI 不在范围内；其后 DL-1 落地，`hyprial gui` 的 ensure hook
（`shell/impl/cli/commands/gui/commands.py:_daemon_starter`）直接调用
`_launch_daemon_process`。owner（GUI 线）决定：**`hyprial gui` 永不安装或迁移
OS 服务**。该 hook 传 `autoinstall=False`，不走 Z-8c 自动安装；已安装且可归属的单元
仍由管理器启动（§3 路由保留）。安装服务仍归 `hyprial init`、
`hyprial daemon service install` 与本节定义的显式 CLI 路径。测试：
`tests/shell/gui/lifecycle/service/test_gui_never_installs_service.py`。

同日 owner 决定 `hyprial login` 同样处理：login 的两条启动路径都传 `autoinstall=False`，即
`shell/impl/cli/commands/login/commands.py` 经共用的 `_complete_initialization(autoinstall=False)`
（init 自己调用时保持默认 True），以及 `login/flow.py` 的直接启动调用。登录是身份操作，不改变监管状态。自动安装只保留在 `hyprial init` 与
`hyprial daemon start` 两条显式路径；只运行过 `hyprial login`、从未运行这两条命令的 home，
daemon 以 detached 方式运行，重启机器后不会自动拉起，直到用户运行其中之一。测试：
`tests/shell/login/orchestration/test_generation.py` 与
`tests/shell/cli/installation/test_cli_onboarding.py`（同一用例按入口参数化：init 为 True，login 为 False）。

## 4. Z-8b ∥ Z-8c 并行文件边界

可以同时做。边界：

- **Z-8b 独占**：`S/daemon/impl/operations/service/**`（Z-8a 产物之上）、
  `S/daemon/impl/configuration/launchd_jobs.py`、`S/daemon/impl/services/*.template`、
  `S/shell/impl/cli/commands/daemon/service.py`、`contract/daemon-lifecycle/service-supervision.md`、
  `tests/daemon/application/composition/service/**`、
  `tests/shell/cli/lifecycle/service/test_daemon_service_cli.py`。
- **Z-8c 独占**：`S/shell/impl/cli/commands/common/daemon_start.py`、
  `S/shell/impl/cli/commands/common/pending_restart.py`（仅 `:336` 调用点加参数）、
  `tests/conftest.py`、`tests/support/runtime/environment.py`、
  新测试文件（建议 `tests/shell/cli/daemon/startup/test_daemon_autoinstall.py`，新建无冲突）。
- **接口约定**：Z-8c 只从 `daemon/service.py` 导入既有名 `installed_supervisor`、
  `start_supervised_daemon`、`_service_manager`；Z-8b 不得改这三个名字与签名
  （`_service_manager` 内部解析逻辑可变）。Z-8c 不新增 daemon/service.py 的函数。
- 共同前置：两者都建在 Z-8a 的包结构之上；串行关系是 z8a → (z8b ∥ z8c) → integrate。
- 残余风险：Z-8b 若先落地而 Z-8c 基于旧 `_service_manager` 行为写测试，集成时重跑
  双方焦点套件即可；无文件级冲突。

## 5. 跨平台兼容矩阵

| 项 | 状态 | 依据 / 回退 | 证明测试 |
|---|---|---|---|
| Windows 导入安全（pwd/os.getuid） | addressed | `pwd` 仅 `_run` 内惰性导入（`service.py:980`）；`os.getuid` 只在 `_ownership_reason` 运行时调用；`_service_platform()` 先抛 `ServicePlatformUnsupported`；`current_uid` 兼容无 getuid 平台。Z-8b/c 不新增 Windows 分支 | `test_daemon_service_cli.py:136,251` 的 win32 参数化用例 |
| macOS launchd 经 SSH/headless（gui 域） | UNKNOWN | `bootstrap gui/<uid>` 在无 GUI 登录会话时的真实行为未实测；失败 → typed error → Z-8c 回退 detached | Mac 验收记录实际报错；单元侧 fake runner 覆盖命令形状 |
| bootstrap/bootout/KeepAlive/plist 转义/PATH | 转义 addressed，其余 BLOCKED | XML 转义 `_escape_xml` 有单测；真实 launchd 行为本环境做不到 | `test_service_supervision.py`；Mac 验收 1-3、5 |
| App Translocation | GAP（有默认规则） | 路径含 `/AppTranslocation/` 判不稳定：自动安装跳过、显式安装警告；判定实测待做（K7/K17） | 单元测试覆盖判定函数；Mac 记录 |
| 104 字节 AF_UNIX socket 上限 | addressed（2026-11-16，§9 F4） | 启动原语在安装/spawn 之前预检 socket 路径长度，超限以既有 `DAEMON_START_FAILED` 报出路径、长度与上限；上限用不创建文件的 bind 探测实测得出 | `test_daemon_autoinstall.py` 的 socket 路径用例；Mac 复核记录报错原文 |
| systemd 用户会话在/不在、linger、polkit | addressed | `user_bus_state` 三态与 typed error 已有；install 跑 `loginctl enable-linger`；无会话 → `user-bus-unavailable` → Z-8c 回退 detached | `test_service_supervision.py`、`test_daemon_service_cli.py` doctor 用例 |
| 非 systemd / 容器 | addressed | 管理器命令失败均 typed；`init --no-service` 文档化；容器行为 UNKNOWN 不声称 PASS | fake runner 单测 |
| systemd 单元名字符集 / % 转义 | addressed（构造保证） | 派生名只含 `hyprial-` + 小写 hex；`_quote_systemd` 已转义 `%` | Z-8b 命名单测 |
| 哈希规范化（大小写/Unicode 路径） | addressed（规则） | §2.1：只 realpath，不折叠不正规化；别名限制成文 | Z-8b 规范化测试向量（符号链接、尾斜杠、大小写写法） |
| 无管理器可达时的注册 | addressed | install typed 失败 → Z-8c 回退 detached；doctor 报 unknown（既有） | Z-8c 回退单测 |
| 默认 home 逐字节不变 | addressed（Z-8a）/ 注意（Z-8b） | Z-8a 纯移动；Z-8b 命名对默认 home 不变，但代理渲染改变模板字节（§2.4 末） | Z-8b 黄金字节测试：默认 home 渲染 diff 为空（无代理环境时） |

## 6. 测试计划

### 6.1 Z-8a

无新测试；现有套件全绿为准。焦点：
`tests/daemon/application/composition/service/`、`tests/shell/cli/lifecycle/service/`、
`tests/shell/cli/daemon/`、`tests/shell/cli/installation/restart/`。

### 6.2 Z-8b（先红后绿）

| 行为 | 测试（文件） |
|---|---|
| 默认 home 命名逐字节不变（黄金） | `test_daemon_service.py` 新增 |
| 非默认 home 派生 `<id>` 名、单元内 `HYPRIAL_SERVICE_UNIT` 携带派生名 | `test_daemon_service.py` 新增 |
| id 规范化向量（符号链接、尾斜杠、realpath 一致性） | `test_daemon_service.py` 或新 `test_service_identity.py` |
| 非默认 home 安装只触达 fake runner（替换 `:17-36` 拒绝测试） | `test_home_isolation.py` |
| module 形式渲染（launchd XML / systemd ExecStart）与归属识别 | `test_service_supervision.py` / `test_launchd_jobs.py` |
| `argv_runs_daemon` 认 module 形态、拒绝近似形态 | `test_launchd_jobs.py` |
| 扫描器按 `HYPRIAL_HOME` 匹配：同可执行文件、别家 home 的 job 不认领 | `test_launchd_jobs.py` 或 `test_service_supervision.py` |
| 代理渲染：存在则转义渲染、缺失则整键省略 | `test_daemon_service.py` |
| console script 归属判定（shebang 解释器匹配/不匹配） | `tests/shell/cli/lifecycle/service/test_daemon_service_cli.py` |
| 契约文字更新后 `./contract/run-all.sh --isolated-only` | 门禁 |

### 6.3 Z-8c（先红后绿，全新测试文件）

| 行为 | 测试 |
|---|---|
| 无单元且允许 → 先 install 再经管理器启动（断言 fake runner 命令序列含 bootstrap） | 新 `test_daemon_autoinstall.py` |
| `HYPRIAL_SERVICE_AUTOINSTALL=0` → 不装，走 detached（patch 内层 locked 见证） | 同上 |
| install 抛 ownership/command 错误 → 不致命回退 detached + 一条 notice | 同上 |
| `HYPRIAL_DAEMON_AUTOSTART=0` + `implicit=True` → `DAEMON_AUTOSTART_DISABLED`；显式调用不受影响 | 同上 |
| `pending_restart` 调用点不自动安装 | 同上或 restart 套件 |
| conftest / `isolated_environment()` 设有这两个变量 | 同上（读 fixture 产物断言） |

### 6.4 三道测试安全保护（审查重点，必须仍绿）

1. operating home 之外拒绝：`test_daemon_service.py:230,267`。
2. account-home-mismatch 在任何真实管理器命令前拒绝：`test_home_isolation.py:51`、
   `test_daemon_zenoh_failloud.py:205`。
3. 真实单元目录 tripwire：`test_tripwire.py` + `ServiceUnitTripwire`
   （`environment.py:86-112`，`conftest.py:583-585` 注册）。

### 6.5 门禁（每个执行节点交付前）

`uv run --with libcst==1.9.0 pytest tests/`（与基线固有失败逐项对照）、
`uv run ruff check .`、
`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`、
`.../check_module_boundaries.py`、
`.../check_inline_comments.py --strict`、
`./contract/run-all.sh --isolated-only`（报告退出码与 RESULT 行）、
`uv run python scripts/verify/check_contract_impact.py`。

本机基线（fc240df33，本 analyze 工作区实测）：见 PLAN.md 的基线段；引擎需先
`scripts/build/build_native_engine.sh`（未构建时全量套件大面积失败，属环境而非回归）。

## 7. 待 owner 决定（建议 + 无回复时的安全默认）

| # | 议题 | 建议 | 无回复时的默认实现 |
|---|---|---|---|
| OD-Z2 | 首条命令不询问即注册服务 | 按设计默认：是；非致命，stderr 一条提示含单元名与 uninstall | 是（本计划 §3.2） |
| OD-Z7 | 代理渲染 + 0644 凭据暴露 | owner 2026-11-15 确认选项 (a)：照样渲染（含 userinfo）、保持 0644，不过滤、不改 0600，登记 KP-081 | 同左（已实施；KP-081） |
| OD-Z11 | 哈希命名 | sha256(realpath UTF-8)[:12]，不折叠大小写、不正规化，别名限制成文 | 同左 |
| headless/无管理器回退 | — | detached spawn（今天行为）+ doctor 既有 warn/unknown | 同左 |
| 共享 Mac 重启存活 | — | 验收第 4 项大概率 BLOCKED（共享机不重启） | BLOCKED 并写明 |
| 同账号两个 home | — | 两个 label 共存、各自 socket/锁；网络侧身份归 OD-7/D6，不在本批 | 只记录 |
| App Translocation | — | `/AppTranslocation/` 判定；自动安装跳过、显式安装警告 | 同左，Mac 实测记录 |
| app 卸载后的孤儿单元 | — | 不在本批；记录 | 只记录 |
| upgrade 重启是否自动安装 | 本计划新增 | 否：重启不改变监管状态 | `autoinstall=False`（§3.1） |
| AUTOSTART 语义 | 本计划新增 | 只拦隐式调用；显式命令不受阻 | 同左（§3.2） |

## 8. Mac 验收与安全栏

安全栏（硬性）：只用专门的非默认 home；**绝不**读、写、安装、卸载或重启
`~/.hyprial` 与 `com.hyprial.hyprial.daemon`。由此交接第 1 项（默认 home 实机安装）
在本机只能 BLOCKED，用默认 home 黄金字节测试与 fake-runner 套件替代证明。

交接 §5 八项，逐项 PASS/FAIL/BLOCKED 并附命令与输出摘要：

1. 默认 home 行为一致 —— 本机 BLOCKED（安全栏），替代：§6.2 黄金字节测试。
2. 非默认 home 派生标识、`HYPRIAL_HOME` 正确、与默认 home 服务互不影响 —— 实机做
   （只验证“默认 home 服务不受影响”靠不触碰 + `launchctl print` 只读观察）。
3. kill 后 launchd 拉起；stop/start/restart 经管理器 —— 实机做。
4. 重启电脑后回来 —— 共享机 BLOCKED，写明。
5. uninstall 后单元文件与 job 都消失 —— 实机做。
6. module 形式程序（无 console script 的安装）能启动且加载同构建的 `hyprial._core` ——
   实机做（用 `python -m hyprial.cli` 环境安装）。
7. 全新非默认 home 首次 `daemon start` 注册并启动；`HYPRIAL_SERVICE_AUTOINSTALL=0`
   不注册、回退 detached —— 实机做。
8. 长路径 home 的 socket 104 字节上限 —— 实机做，记录报错原文。

Linux（有 systemd 用户会话的机器，若有）：对应 1、2、3、5、7；没有条件写 UNKNOWN。

## 9. launchd 验收后的修复（2026-11-16，PR #2830 验收报告 D1–D3、F1–F6）

验收见 #2830 评论 “launchd 验收已完成”。本节记录计划原先沉默处的决定。

### D1 module 形式单元无法导入 hyprial

- 根因：渲染与归属检查都 `resolve()` 程序路径；venv 的 `bin/python` 是指向基础 CPython 的
  符号链接，resolve 后单元运行没有 site-packages 的解释器，在管理器下以
  `ModuleNotFoundError` 循环崩溃，`install` 返回 `SERVICE_SUPERVISION_LOST` 后单元仍保持加载。
- 决定 1（程序路径）：单元渲染未 resolve 的绝对 `sys.executable`（`identity.program_path`，
  `os.path.abspath`）。渲染、归属检查（`_is_attributed`）与 launchd 扫描器共用这一个规范化；
  module 形式只按绝对拼写匹配（不再把同一 CPython 上的所有 venv 当作同一文件），console
  script 形式保留符号链接容忍（`~/.local/bin/hyprial` → tool venv 脚本）。
- 决定 2（计划与契约原先沉默的情形：安装后监管丢失）：新装单元没有取得 daemon 的监管，
  不留在原地。`install` 前 `capture_unit_state()` 记录单元文本与是否已加载；监管丢失时
  `revert_install()`：此前没有单元 → 让管理器卸载并删除文件（`removed`）；此前有单元 →
  卸载、写回旧文本、仅当原先已加载才重新加载（`restored`）；install 没有改变任何东西 →
  不动（`unchanged`）。错误仍是 `SERVICE_SUPERVISION_LOST`，消息说明发生了哪一种，数据里带
  `install: {supervision: "lost", reverted: removed|restored|unchanged|failed}`。revert 自己失败
  时 `reverted` 为 `failed`，提示 `hyprial daemon service uninstall`。
- 决定 3（自动安装，Z-8c）：自动安装之后首次受监管启动失败（任意 `CliError`，如启动超时）→
  同一个 `revert_install`，一条 notice 写明原因与结果，回退 detached 启动。revert 失败时不回退
  detached（单元可能仍在加载，会与 detached 争锁），报 `SERVICE_SUPERVISION_LOST`。
  自动安装不在安装后另做 10 秒的监管等待：一个慢启动但健康的 daemon 会被误回退；它用已有的
  就绪预算（`ready_timeout`）。
- 不处理：不是 venv 的系统 Python 下 console script 的 shebang 写 `python3.12` 而
  `sys.executable` 是 `python3` 时，因为不再 resolve，会走 module 形式（仍然正确，只是形态不同）。

### D2 `daemon stop` 的路由

- 根因：`installed_supervisor()` 只看“是否安装了单元”。已装未加载的单元旁边有 detached
  daemon 时，既不 bootout 也不发 IPC。
- 决定：`stop_supervised_daemon` 按谁监管运行中的 daemon 决定：应答的 pid 等于管理器报告的
  本单元 main pid → 走管理器；应答的 pid 不同（detached daemon）→ 发 IPC `shutdown`（带
  `expectedEpoch`，并保留 stop 前读取的 `processIdentity`），单元未加载时不问管理器；没有应答
  的 daemon → 与以前相同（已加载则 bootout/stop）。
- 修复轮补充（2026-11-16）：单元已加载、但应答的 daemon 不是它的 main pid（loaded-other-pid）
  时，先让管理器停掉单元（bootout / `systemctl --user stop`）再发 IPC `shutdown`；否则
  KeepAlive/`Restart=always` 会在 detached daemon 退出后把单元的进程拉起，stop 就没有停住。
  IPC 经 `daemon_stop._request_shutdown` 这唯一的 shutdown 请求点（不新增请求点，不碰
  `common/support.py`）。
- `DAEMON_STOP_TIMEOUT` 的消息不再固定写 “socket closed”：超时时观察控制 socket，报告 gone /
  still present / could not be checked（`daemon_stop.py` 在允许范围内；登记 KP-090 为已修）。

### D3 console script 归属

console script 必须归属本安装：其 shebang 解释器与未 resolve 的绝对 `sys.executable` 是同一文件
（比较时只 resolve 目录、不跟随最后一级符号链接，所以 macOS 的 `/var` 与 `/private/var`、
符号链接的 checkout 目录不会拒绝合法的本安装脚本），或脚本与 `sys.executable` 位于同一个
`bin` 目录；否则走 module 形式（D1 之后该形式是正确的）。同一 CPython 上的另一个 venv 的
脚本两条都不满足，不会被接受。`which` 命中的符号链接（`~/.local/bin/hyprial` → tool venv）
仍先解析到真实脚本再判断。

### F1–F6

| 项 | 处理 |
|---|---|
| F1 单元 `PATH` 以 `~/.local/bin` 开头 | 登记 KP-082，不修 |
| F2 stop 之后 `lost` | doctor 区分：已装、可归属、未加载、无 daemon 服务 → warn `stopped-on-request`（launchd 实测形态）；systemd 无法区分，登记 KP-089；`supervision` 三值与 status JSON 不变 |
| F3 无法隔离管理器启动的 daemon | 登记 KP-093（原 KP-086，合并 dev 后改号），不修 |
| F4 AF_UNIX 溢出 | 已修：启动原语预检，既有错误码 `DAEMON_START_FAILED`，只新增消息与 `data`（`socketPath`、`length`、`limit`） |
| F5 `lastExitCode` 为 null | 登记 KP-087，不查 |
| F6 pytest 收集 0 项 | 登记 KP-088，不修 |
| KP-081 | 追加观察：验收渲染的代理值不含 userinfo |
