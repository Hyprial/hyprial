# hyprial CLI 一级命令盘点与收拢建议

- 调研日期：2026-10-04（刷新；首版 2026-08-24，基线 `dev@ef66f2b6`）
- 后续状态：R4 已将用户 CLI 的一级 `network` 改为 `host`，并将一级 `hosts`
  改为 `host list`；下文保留旧拼写时是在描述本盘点的历史基线或 daemon IPC 方法。
- 后续状态（2026-10-14）：R5 已把一级 `restart` 移入 `daemon restart`，新增
  `daemon start`，并把 `autoupdate status|restart|run` 改为 `upgrade status|apply|run`
  （`upgrade run` 隐藏）；下文保留 `autoupdate`/一级 `restart` 时是在描述本盘点的历史基线。
  遗留定时器的 `autoupdate install/uninstall` 仍待 F8 删除。
- 代码基线：
  - `dev@bccc754d`（本文件所在分支的基线）；
  - `refactor/reconstruct-modules@d59a58a2`（深模块重构线，将合入 dev）。
  - **清单取自重构线**：它就是即将落到 dev 的命令树，命令实现位于
    `src/hyprial/shell/impl/cli/commands/`（下文简写为 `cc/`），一级注册汇总于
    `src/hyprial/shell/impl/cli/runtime/entry.py:254-282`。下文所有代码 `file:line` 均为重构线路径；
    两棵树的差异单列在 3.1。重构线随后（#1125，已合入）把 `cli_commands/`、`cli_runtime/` 移到
    `shell/impl/cli/{commands,runtime}/`，文件名与行号不变，只是目录前缀不同；本文路径已按新前缀更新。
- 范围：只分析 Python CLI 的命令面（一级为主，完整列出二、三级）；本文件不修改任何命令、IPC 或运行时行为
- 运行边界：只在隔离 `HYPRIAL_HOME=/private/tmp/claude-501/hb-tree` 下 import CLI 并用 Typer
  introspection 导出注册树；没有启动 daemon，没有执行任何命令的业务路径，没有读取或写入真实
  `~/.hyprial`、生产 daemon、Lark App 或凭据
- 后续状态（2026-10-13）：本文正文保留 2026-10-04 基线盘点；D1 已按《目标》落地为
  `hyprial socialware install` 恒返回 `COMING_SOON`，旧一级 `hyprial install` 已删除。
- 后续状态（2026-10-27）：旧独立 GUI（`src/gui`）与 Tauri/Electron 外壳（`app`）已临时
  退场。`gui` 一级命令及 `start|status|stop|upgrade` 外形保留，但统一返回
  `GUI_UNAVAILABLE` 并以退出码 1 失败；不创建、解包或管理旧 GUI。下文 2026-10-04 的
  GUI 行、旧实现路径和调用证据仅作历史盘点；恢复入口与责任见
  `docs/design/gui-retirement-restore-ledger-2026-10-27.md`。

## 1. 先给结论

1. **一级入口从 33 个涨到 59 个：54 可见、5 隐藏；21 个命令组 + 38 个叶子命令**，全树共 193 个
   叶子命令（含 8 个隐藏叶子）。两条基线的一级集合完全相同（59/5/21），差异只在二级（见 3.1）。
   净增 26 个一级：新增 27 个，退役 1 个（`channel`）。
2. **上次建议几乎只落地了“修清单”这一类**：`channel` 已退役（但走的是直接删除，没有走
   deprecation 周期）、`help` 改为由 Typer 注册树生成并有 `cli-help-inventory` CI 门、`squire`
   重复注册已消除。其余 A–J 全部未做，且有四项变得更重：`service` 帮助文案变成更误导的
   “Manage the daemon service lifecycle”；`autoupdate` 新增 `restart`；`mcp` 新增隐藏
   `claude-turn-ended`（且已无产品内调用者）；隐藏 `transfer-*` 从 3 个变成 5 个。
3. **本轮最大的新问题是迁移域铺满一级**：`transfer` 是叶子命令，同时一级还有 13 个
   `transfer-*` 名字（7 个可见 bundle/credential 叶子、5 个隐藏远端协议叶子、1 个
   `transfer-runtime` 组），即 14/59 个一级入口属于同一个领域。
4. **daemon 生命周期入口分散在 8 处**：`init`、`daemon run|stop`、`service`、`restart`、
   `autoupdate restart`、`upgrade`（可能重启）、`ps`（状态）；而 `start/down` 管的是 connector，
   不是 daemon，名字却最像 daemon 生命周期。
5. **新出现的“内部业务/原型”一级**：`grants-check`、`grants-visibility` 是 AT09/AT10 决策核心的
   纯本地求值器（不读真实 grant 台账），仓内只有测试与设计文档调用；`transfer-credential-*` 的
   help 自称 prototype 却是可见一级。
6. **Allen 2026-09-23 的 PAC 裁决是可复用的先例**：PAC 是基础概念、不是一级命令，`hyprial pac`
   直接移除、无 alias、无隐藏注册，并有测试守护（`ccd1ddc6`，
   `tests/shell/cli/presentation/test_cli_top_level_concepts.py:21-29`）。同一裁决把 `workflow`
   与 `routine` 明确为 PAC 的两个操作入口，上次的 `automation workflow|routine` 包装建议因此作废。
7. 排序后的新建议（详见第 5、6 节）：① transfer 家族收进 `transfer` 组；② `grants-*` 下沉到
   `agent`；③ `service` 退役并修正仍在发给用户的文案；④ `autoupdate` 收进 `upgrade`
   （`status/apply`）、`restart` 归入 `daemon`；⑤ 消息观察面收进 `delivery`；⑥ `lark-auth`、
   `mcp`、隐藏 transfer、`autoupdate run` 内部化（调用者是程序生成的 argv，最后做）。
   退役策略已由 Allen 裁决为一律直接删除、不支持从旧版本迁移（见 4.3）。

## 2. 证据口径

本文把证据分成四类，避免把“grep 没找到”写成“没人用”。

| 标记 | 含义 |
|---|---|
| **代码确认** | 实现路径足以确认行为或无行为；不依赖调用者搜索 |
| **本轮实测** | 本轮在独立 `HYPRIAL_HOME`、`HARNESS_STATE_DIR` 下实际运行；**本轮只实测了 Typer 注册树导出**，没有实跑任何命令的业务路径 |
| **仓内调用证据** | 测试、E2E runner、内部 argv 生成器、skill 或文档明确调用；只能证明仓内调用，不能外推全部生产使用 |
| **未发现外部调用者** | 静态搜索、Typer 注册树和 git 历史中没有找到人/脚本调用；**不等于没人用**，尤其不排除动态拼接、仓外脚本或旧版本 |

调用者搜索覆盖 `src/hyprial`（含 argv 生成器、`skills/hyprial-ops/SKILL.md`、
`daemon/impl/squire/SKILL-template.md`）、`contract/`、`scripts/`、`.agents/skills/`、
`desktop/src-tauri/src`（Rust argv）与 `tests/`；同时匹配 shell 字面量（`hyprial message send`）与 argv
列表（`[exe, "outbox", "list"]`、`daemon.command("hosts", ...)`）两种形态。“最近是否使用”
只报告提交记录中的 E2E 证据；本文没有读取生产 shell history、daemon 日志或用户 home。

## 3. 完整命令清单

### 3.1 基线与两棵树的差异

| 指标 | 2026-08-24（`dev@ef66f2b6`） | 2026-10-04（`dev@bccc754d` 与 `refactor@d59a58a2` 相同） |
|---|---:|---:|
| 一级入口 | 33 | 59 |
| 其中可见 / 隐藏 | 30 / 3 | 54 / 5 |
| 一级命令组 / 一级叶子 | 13 / 20 | 21 / 38 |
| 全树叶子命令（含隐藏） | 未统计 | 193（隐藏 8：5 个一级 + `mcp` 2 个 + `autoupdate run`） |

导出方法：`typer.main.get_command(app)` 后对每层 `list_commands/get_command` 递归，读取
`hidden` 与 `invoke_without_command`；dev 从 `hyprial.cli`、重构线从
`hyprial.shell.impl.cli_runtime.entry` 取 `app`。两树一级名与可见性逐行一致；二级及帮助文案差异只有：

| 位置 | dev@bccc754d | refactor@d59a58a2 |
|---|---|---|
| `org` | 只有 `show/status/fetch/import` | 另有 `create/invite/join/execute/leave/remove/delete/list/pending/network`（共 14） |
| `profile` | `list/use/create` | 另有 `show` |
| `network` | `whois`（经内嵌 tailnet node 解析） | 改为 `peer-key`（`817cedb4`，Tailcat，2026-10-03） |
| `install` | 真实的应用安装器 | 已退役占位，恒返回 `COMING_SOON`（`ced60773`，2026-10-03） |
| `gui` | 旧 GUI 已临时退场；命令外形保留，统一返回 `GUI_UNAVAILABLE`（退出 1） | 2026-10-27 退场接线；恢复见 `docs/design/gui-retirement-restore-ledger-2026-10-27.md` |

由于重构线合入后 dev 即为右列，下表一律按重构线描述。

### 3.2 一级命令逐项

按 Typer 注册顺序列出。“组”行给出完整二级与三级面。

| 一级命令 | 可见性 | 实际用途（含完整二级面） | 调用者（仓内证据） | 证据类别 / 边界 |
|---|---|---|---|---|
| `login` | 可见 | 以 OIDC device flow 登录本 home 的用户身份并生成设备 key（`cc/setup/login.py:18`，`c2a73dff` 2026-09-04 引入） | 人；`skills/hyprial-ops/SKILL.md:61-62`、`.agents/skills/hyprial-e2e/SKILL.md:150`；`contract/migration/drill-run.py`。desktop 账户窗口走 `shell/impl/desktop_network.py` Python 模块，不是 CLI argv | **仓内调用证据** |
| `help` | 可见 | 由 Typer 注册树生成一级清单，含“Hidden commands (internal)”段（`cc/observe/meta.py:52`，`shell/impl/cli/inventory.py:20-57`） | `contract/cli-blackbox/run.py:155-161`（sealed schema 要求清单含 `hyprial service`）；CI `cli-help-inventory`（`.forgejo/workflows/gate-python.yml:32,128` → `scripts/verify/check_cli_help_inventory.py`） | **仓内调用证据**；CI 硬门 |
| `version` | 可见 | 本地包版本、安装 revision、update source（`cc/observe/meta.py:70`） | E2E-005 `contract/e2e/run-uv-install-flow.py`、`cli-blackbox`、`daemon/impl/autoupdate/alert.py` 文案、`scripts/build/docker/build-worker-image.sh` | **仓内调用证据** |
| `install` | 可见 | 已退役占位：接受并忽略一切参数，恒失败 `COMING_SOON`、退出 1（`cc/lifecycle/install.py:18-40`）；Allen 2026-10-03 裁决保留名字 | `skills/hyprial-ops/SKILL.md:76-77` 记载占位语义；`gui-workflow`、`hyprial-e2e` skill 提及 | **代码确认**：无业务效果 |
| `ps` | 可见 | daemon、managed connector、adapter、interactive session 概览（`cc/observe/status.py:17`） | 约 30 个 contract 文件（如 `contract/e2e/_daemon_runner.py`）；`daemon/impl/transfer/execution/container.py`；skills。desktop 用的是 IPC `ps`（`desktop/src-tauri/src/runtime/daemon.rs:22`），不是 CLI | **仓内调用证据** |
| `top` | 可见 | agent 是否在工作：turn、队列、verdict、资源与额度（`cc/observe/status.py:464`）；数据层已与 `targets` 同源（`94f0995e`） | 人；只发现测试 | **仓内调用证据**（仅测试） |
| `doctor` | 可见 | 只读健康检查（`cc/observe/doctor.py:951`） | `cli-blackbox`、E2E-010 文档、`hyprial-ops`/`hyprial-e2e` skill | **仓内调用证据** |
| `worktrees` | 可见 | 只读检查本机 git worktree 遗留工作信号（`cc/observe/worktrees.py:476`，`d4cd9720` 2026-09-28） | `skills/hyprial-ops/SKILL.md:128`；测试 | **仓内调用证据**（skill + 测试） |
| `targets` | 可见 | 列出可投递 target（`cc/observe/overview.py:144`） | 约 10 个 contract runner；`squire/SKILL-template.md:89`；pi harness bridge TS | **仓内调用证据** |
| `hosts` | 可见 | 网络上宣告的 node，仅在线状态（`cc/observe/overview.py:184`） | `cli-blackbox/run.py:515`、`run-dual-instance-roundtrip.py:52`、`run-peer-restart-relearn.py:597`、`_two_machine/local.py`。transfer orchestrator 用的是 IPC `hosts`（`daemon/impl/transfer/orchestrator.py:84,284`），不是 CLI | **仓内调用证据** |
| `send` | 可见 | 经 daemon 发文本/附件（`cc/messaging.py:58`） | 约 26 个 contract 文件；`squire/SKILL-template.md:24-28` | **仓内调用证据** |
| `ack` | 可见 | 以 actor 身份确认、不回复（`cc/messaging.py:130`） | 约 16 个 contract 文件；`hyprial-workflow` skill | **仓内调用证据** |
| `reply` | 可见 | 回复一条待处理消息并在持久接收后 ack（`cc/messaging.py:156`，`81d601dc` 2026-09-28） | 只发现测试 | **未发现外部调用者** |
| `query` | 可见 | 只读查看本机某 actor 的 inbox/outbox（`cc/observe/logs.py:485`，`68c00f3c` 2026-09-23） | `skills/hyprial-ops/SKILL.md:225,267`；测试 | **仓内调用证据**（skill + 测试） |
| `log` | 可见 | 查询全部组件的结构化 JSONL 历史（`cc/observe/logs.py:510`） | `run-pi-wake-registration.py`、`cli-blackbox`、`hyprial-workflow` skill | **仓内调用证据** |
| `trajectory` | 可见 | 合并一条消息的本地事件与可拉取投递状态（`cc/observe/logs.py:546`） | `run-dual-instance-roundtrip.py:100`、`run-pi-wake-registration.py:152,168`；`contract/wire/trajectory.md` | **仓内调用证据** |
| `init` | 可见 | 初始化 home 并启动 daemon；无 owner 时先建立身份（`cc/setup/first_run.py:407`） | 约 10 个 contract 文件（含 `_daemon_runner.py`）、安装/E2E 脚本 | **仓内调用证据** |
| `service` | 可见 | **仍只**探测 daemon 是否运行，恒报 `mode=standalone`、`installed=false`（`cc/lifecycle/daemon.py:18-49`）；帮助却写“Manage the daemon service lifecycle” | 升级通知正文让用户运行 `hyprial service --json`（`daemon/impl/application/messaging/delivery/sends.py:628`）；Squire setup 步骤（`daemon/impl/squire/setup.py:634-636`）；`cli-blackbox/run.py:161` | **代码确认**：无生命周期管理能力 |
| `grants-check` | 可见 | AT10：按 `--capability/--resource/--caller/--scope` 求值一次能力使用并给出归属入口；records 来自命令行参数，不读 daemon 台账（`cc/agents/grants.py:10-87`，`eedcac35` 2026-09-30） | `tests/identity/capabilities/test_capability_enforcement.py`；`docs/design/design-at10-capability-entry-map.md:46` | **代码确认**（纯本地求值）；**未发现外部调用者** |
| `grants-visibility` | 可见 | AT09：对给定 caller 求值 see-actors/send-to（`cc/agents/grants.py:90`，`b2fb0c6b` 2026-09-30） | 只有 `tests/identity/capabilities/test_grant_visibility.py` | **代码确认**；**未发现外部调用者** |
| `start` | 可见 | 启动/绑定 claude、pi、codex、dsh、jev、user-proxy；支持 `--tier`（`cc/start.py:20-27`） | 约 16 个 contract 文件；skills | **仓内调用证据** |
| `down` | 可见 | 停止并注销一个或全部 managed connector（`cc/lifecycle/down.py:14`） | 约 11 个 contract 文件（`_harness_lifecycle/phases/recovery.py` 等） | **仓内调用证据** |
| `transfer` | 可见 | 叶子：经 SSH 冷迁移 managed headless worker，strict resume 失败即回滚（`cc/transfer/session.py:17`） | 人；测试；`--to` 帮助现要求取自 `hyprial host list` 的 nodeId | **仓内调用证据**（测试） |
| `transfer-bundle-export` | 可见 | AT06：把 agent home 打成 transfer bundle（`cc/transfer/bundle.py:15`，`f32b2533` 2026-09-30） | 测试 | **未发现外部调用者** |
| `transfer-bundle-validate` | 可见 | AT06：接收前校验 bundle（`cc/transfer/bundle.py:136`） | 测试；`docs/guides/transfer-at12-cross-machine-runbook.md:48` | **仓内调用证据**（runbook） |
| `transfer-bundle-receive` | 可见 | AT07：把已校验 bundle 落到目标 home（`cc/transfer/bundle.py:198`，`e2dbb58e`） | 测试；runbook:48 | **仓内调用证据**（runbook） |
| `transfer-bundle-land` | 可见 | AT07：把收到的 bundle 落为本节点 agent，走 IPC `transfer.land`（`cc/transfer/bundle.py:246,286`） | 测试 | **未发现外部调用者** |
| `transfer-credential-seal` | 可见 | AT06 凭据信封封装，help 自称 prototype（`cc/transfer/credentials.py:15`，`3e0ac8fc`） | 测试 | **未发现外部调用者** |
| `transfer-credential-open` | 可见 | 打开凭据信封，prototype（`cc/transfer/credentials.py:130`） | 测试 | **未发现外部调用者** |
| `transfer-bundle-complete` | 可见 | AT11：按身份 fence 清理源端，可续跑（`cc/transfer/bundle.py:298`，`887fdca5`） | 测试 | **未发现外部调用者** |
| `transfer-precheck` | **隐藏** | 目标端准入检查（`cc/transfer/session.py:176`） | orchestrator 生成 argv（`daemon/impl/transfer/orchestrator.py:373`）；测试 | **内部调用已确认** |
| `transfer-receive` | **隐藏** | 目标端经 stdin 接收 payload 并 strict resume（`cc/transfer/session.py:198`） | `orchestrator.py:547`、`execution/ssh.py:124`；`contract/e2e/run-hosted-transfer.py:158` 直接调用；E2E-019 文档 | **内部调用已确认**；且 `hosted_by='transfer-receive'` 是持久化 CHECK 值（`identity/impl/agents/registry/_base.py:69,403`） |
| `transfer-cred-stage` | **隐藏** | 目标端准备/封存凭据暂存目录（`cc/transfer/session.py:232`） | `orchestrator.py:450,473` | **内部调用已确认** |
| `transfer-container-home` | **隐藏** | 目标端创建 per-worker 容器 home（`cc/transfer/session.py:286`） | `orchestrator.py:491` | **内部调用已确认** |
| `transfer-session-path` | **隐藏** | 目标端解析/创建 session file 路径（`cc/transfer/session.py:311`） | `orchestrator.py:624` | **内部调用已确认** |
| `upgrade` | 可见 | 从官方 release 用 uv 升级，必要时重启或记录 pending restart（`cc/lifecycle/upgrade.py:901`） | E2E-005/010 runner；skills；alert 文案 | **仓内调用证据** |
| `restart` | 可见 | 安全停启 daemon，报告被打断的 turn；有 pending 自动升级时一并确认并清除（`cc/lifecycle/restart.py:205-326`，`e0660b07` 2026-09-25） | `contract/e2e/run-cli-restart.py` | **仓内调用证据** |
| `gui` | 可见 | 叶子但带位置动词 start/status/stop/upgrade；2026-10-27 起所有动作返回 `GUI_UNAVAILABLE`（退出 1），不再安装/解包/管理旧 GUI | 上文为 2026-10-04 历史盘点；旧 desktop 调用路径已随退场删除。当前失败封闭行为由 CLI 测试覆盖，恢复见退场台账 | **当前实现；历史证据仅对照** |
| `adapter` | 可见组 | `add/onboard/remove/start/stop/status/reload/list/doctor/authorize/pin/unpin/pins`；`route list/candidates/add/remove`；`identities sync/list/find`（`upsert` 已由 #1202 删除）；`media get`（`entry.py:254,279-282`） | 约 7 个 contract runner（四 adapter、Lark onboarding 等）；Squire template | **仓内调用证据** |
| `daemon` | 可见组 | `run`（前台运行）、`stop`（`cc/lifecycle/daemon.py:52,111`） | desktop 启动 `daemon run --json`（`desktop/src-tauri/src/runtime/boot.rs:235`）；`contract/e2e/_daemon_runner.py`、docker entrypoint；`init` 子进程 | **仓内调用证据** |
| `mcp` | 可见组 | `claude-channel`、`agent-channel`；隐藏 `claude-channel-recover`、`claude-turn-ended`、`dsh-carrier`（launcher-owned dsh-tui carrier） | argv 生成：`cc/agents/claude.py:175`（channel server）、`:273,316`（hook）、`daemon/impl/harnesses/turn_delivery/channel/worker_channel.py:137`；`contract/e2e/_upgrade_continuity/channel_probe.py` | **内部调用已确认** |
| `squire` | 可见组 | `setup`、`probe`（`cc/setup/squire.py:451,566`） | `contract/e2e/run-squire-setup.py`；onboarding plan | **仓内调用证据** |
| `user-proxy` | 可见组 | `status`、`setup`：配置个人 user-proxy 投递通道（`cc/setup/squire.py:183,212`，`33a925bd` 2026-09-26） | CLI 自身的 setup warning；测试 | **仓内调用证据**（测试） |
| `outbox` | 可见组 | `list`、`prune`（`cc/messaging.py:187,197`） | `contract/e2e/run-outbox-deadletter.py:111,357` | **仓内调用证据** |
| `delivery` | 可见组 | 只有 `status`（`cc/messaging.py:213`） | `contract/e2e/_harness_lifecycle/phases/lark.py:439`、`run-daemon-restart-survival.py` | **仓内调用证据** |
| `autoupdate` | 可见组 | `status`、`restart`（仅在有 pending 时重启）、隐藏 `run`；legacy `install/uninstall` 已删除 | daemon 调度以子进程调 `autoupdate run`；daemon 启动迁移清理旧 launchd/systemd 单元；告警正文要求运行 `autoupdate restart`；`run-inprocess-autoupdate.py`；`hyprial-ops` skill | **内部调用已确认** + **仓内调用证据** |
| `config` | 可见组 | 只有 `set`，8 个键（`autoUpgrade` 等，`cc/lifecycle/autoupdate.py:24,377-395`）；没有 get/list | `run-inprocess-autoupdate.py:351`、`_upgrade_continuity/installation.py`、`run-uv-install-flow.py`、`scripts/verify/verify_gui_profile_recovery.py`；`hyprial-ops` SKILL:44,64,68 | **仓内调用证据** |
| `lark-auth` | 可见组 | `check`、`complete`：lark-cli USER 凭据 watchdog（`cc/config/lark_auth.py:107,183`） | `contract/e2e/feishu/identity.py:93`、`_harness_lifecycle/lark_auth.py:105`；`autoupdate run` 进程内调用（`cc/lifecycle/autoupdate.py:695`）；授权提示文案（`daemon/impl/adapters/lark/credentials/reauth.py:438,454`） | **仓内调用证据** |
| `agent` | 可见组 | `create/host-invite/grant/revoke/grants/list/home-census/restore-policy/restore-threshold/unblock/destroy`；`secret provider-write/grant/list/revoke`；`migrate preflight/execute/rollback`；`keep add/remove/list`（`cc/agents/admin.py:18-39`，`entry.py:251-253`） | E2E-013/018 文档与 runner；`hyprial-ops` SKILL:197 | **仓内调用证据** |
| `org` | 可见组 | 成员：`create/invite/join/execute/leave/remove/delete/list/pending/network`；组织上下文：`show/status/fetch/import`（`cc/orgfs/org.py:30-469`） | desktop deeplink 调 `org join -`、`org pending --accept-all`（`desktop/src-tauri/src/deeplink/mod.rs:171,211,330`）；`run-tailcat-org-invite-join.py:152`；Squire template | **仓内调用证据** |
| `fs` | 可见组 | 27 个扁平叶子：`spaces/create/ls/resolve/read/write/mkdir/mv/rm/history/watch/import/export/invite/remove-member/members/join/web/trash/restore/status/serve/checkout/purge-plan/purge/purge-status/unban`（`cc/orgfs/fs.py:16`，`4b12093c` 2026-09-26） | Squire template（`fs ls/read/write`）；`run-tailcat-org-invite-join.py:282-301`、`run-orgfs-forwarding-redial.py` | **仓内调用证据** |
| `profile` | 可见组 | `list/show/use/create`：网络 profile 即 home 选择（`cc/config/profile.py:71-176`） | `.agents/skills/hyprial-e2e/SKILL.md:49`；guides | **仓内调用证据** |
| `network` | 可见组 | `expose/unexpose/exposures/peer-key`（`cc/config/network.py:15-92`，`f44d1e18` 2026-09-28） | `hyprial-ops` SKILL:50；测试 | **仓内调用证据**（skill） |
| `transfer-runtime` | 可见组 | `downgrade-state`、`probe`：smolvm 映射预检与离线回滚准备（`cc/transfer/runtime.py:10`，`entry.py:270-274`，`c54cd90f`） | `hyprial-ops` SKILL:177,183；测试 | **仓内调用证据**（skill） |
| `workflow` | 可见组 | 12 叶：`plan/run/status/list/inspect/cancel/events/context/reset/complete/fail/gc`；6 个三级组：`history list/status`、`overview show/publish`、`missions collect/finish/check`、`worker stop/restart`、`notify resend`、`migration status`（`cc/routines/workflow.py:31-53`） | `run-pac-graph-slice.py` 等 8 个 runner；`hyprial-workflow` SKILL:52-56；Squire template:120-151（missions） | **仓内调用证据** |
| `dispatch` | 可见组 | 只有 `matrix`（只读，`--probe` 诊断）（`cc/routines/dispatch.py:16`） | `hyprial-workflow` SKILL:172；`hyprial-ops` SKILL:71-72 | **仓内调用证据**（skill） |
| `routine` | 可见组 | `plan/add/list/status/rm/set/pause/resume`；`templates [show]`（组带 callback）（`cc/routines/routine.py:22-29`） | `run-selfdrive-routine.py` 等 4 个 runner；`hyprial-workflow` skill | **仓内调用证据** |
| `onboarding` | 可见组 | `plan`、`apply`：首启步骤（`cc/setup/first_run.py:56,173`，`7ac1f6c8`） | desktop `desktop/src-tauri/src/onboarding/cli.rs:158,192,198` | **内部调用已确认** |
| `user` | 可见组 | `add/list/show/bind/unbind`：本机认识的人及平台账号绑定（`cc/agents/users.py:85-225`，`07d57783` 2026-09-25） | `hyprial-ops` SKILL:286；guides | **仓内调用证据**（skill） |

### 3.3 静态调用搜索的限制

以下用法仍可能看不见：f-string/数组拼装/模板/desired-state 中生成的命令；仓外 skill、
cron/systemd/launchd 与个人脚本；旧版本 daemon 生成、新版本 CLI 执行的混合版本 argv（`autoupdate run`
和隐藏 `transfer-*` 恰是这种形态）；人工终端与 agent 会话内的临时命令。另外 argv 扫描按字面量
匹配，`"agent"`、`"user"` 这类常见词会混入非 CLI 命中，表中只引用人工核对过的位置。

因此“只发现测试”只写成**未发现外部调用者**，不写成“没人用”。

## 4. 上次建议的落地情况

### 4.1 `channel` 的结局

`33cac756`（2026-08-25，`refactor(cleanup): retire IRC codec and disconnected stubs`）一次性删除了
`hyprial channel join|part|list`、daemon 的三个 IPC handler 与整个 IRC codec（`proto/` 包），并加了
回退守护：`tests/shell/compatibility/test_retired_surface.py:35-45` 断言 `channel` 不在注册组中、
`channel list` 非零退出、daemon 源码不含 `channel.list|join|part`。上次建议的
warning → hide → usage gate → remove 周期**没有执行**，是直接删除；结果上没有已知回归，但这也说明
项目实际采用的退役策略比本文建议更激进（PAC 同样“无 alias、无隐藏注册”）。Allen 2026-10-04 已把
这一做法定为本轮所有调整的统一策略，见 4.3。

### 4.3 退役策略裁决（Allen，2026-10-04）

- 本轮命令调整一律**直接删除**旧写法：不留 alias、不留隐藏注册、不做 deprecation 周期，也不做
  运行时调用计数。
- **没有例外**，包括由程序生成、跨版本调用的入口（`mcp ...`、隐藏 `transfer-*`、`autoupdate run`）。
- 本轮是 breaking change，**不支持从旧版本迁移**：不保证新旧版本混跑（例如新版 source 向旧版
  target 发起 transfer），也不保证旧版本留下的 MCP 配置、desired state 中的旧 argv 继续可用。
- 迁移保护因此只剩一条：**改名的同一个提交里，更新仓内所有调用者**（argv 生成器、hook 与
  launcher、skills、SKILL 模板、用户可见文案、contract runner、测试、冻结基线），并加回退守护测试
  （沿用 `test_retired_surface.py`、`test_cli_top_level_concepts.py` 的做法）。
- 下文第 5 节各条中出现的“alias”“兼容期”“双 parser”“协商”“调用计数”等保护措施，均以本裁决为准
  作废；第 6、7 节已按本裁决改写。

### 4.2 逐项状态

| 上次建议 | 状态 | 证据 |
|---|---|---|
| A `autoupdate`+`upgrade` 合一 | **未做**，且分散更多 | 前置 `selfupd` 已合入（`2eee1b5a`，2026-08-24，调度搬进 daemon）；但 `autoupdate` 新增 `restart`（`a38d390e`，2026-09-23），一级新增 `restart`（`e0660b07`，2026-09-25），开关落在 `config set autoUpgrade`（`fbb9b928`，2026-09-15）。`autoupdate run` 仍是 daemon 子进程入口（`daemon/impl/autoupdate/__init__.py:271-278`） |
| B `automation workflow\|routine` | **作废** | Allen 2026-09-23 裁决把 `workflow`、`routine` 定为 PAC 的两个一级操作入口（`ccd1ddc6`；`test_cli_top_level_concepts.py:1-8`）；再包一层与该裁决冲突 |
| C delivery/trajectory/outbox 收拢 | **未做**，面变大 | 新增一级 `query`（只读 inbox/outbox）与 `reply`；`delivery` 仍只有 `status` |
| D `service` 退役 | **未做**，更误导 | 实现未变（`cc/lifecycle/daemon.py:18-49`），帮助改成“Manage the daemon service lifecycle”；升级通知正文仍让用户运行它（`sends.py:628`），Squire 文案未改（`squire/setup.py:636`） |
| E 观察面等 actor-status | **前置已满足，未做** | `94f0995e`（2026-08-24）合入统一 actor-status；观察面又新增 `worktrees`、`query` |
| F `agent` 不并入 `start/down` | **维持** | `agent` 已扩到 11 叶 + 3 个三级组，仍与 runtime binding 分开 |
| G `lark-auth` → `adapter user-auth` | **已做** | R9 已删除旧一级入口，并同步 runner 与人工完成提示 |
| H `mcp` 内部化 | **未做**，面变大 | 新增隐藏 `claude-turn-ended`（`cc/adapters/mcp.py:84`，`17730bc5` 2026-09-30），同日即被 stdlib 模块 `hyprial.biz.impl.turn_pulse` 取代为 Stop hook（`cb3d004a`），仓内只剩测试调用 |
| I 隐藏 `transfer-*` → `transfer internal` | **未做**，面变大 | 隐藏一级从 3 个增至 5 个（新增 `cred-stage`、`container-home`），另有 7 个可见 bundle/credential 一级 |
| J `daemon run` / `autoupdate run` 保留 | **维持** | `daemon run` 新增 desktop 调用者（`runtime/boot.rs:235`） |
| K 退役 `channel` | **已做**（直接删除） | 见 4.1 |
| L `service` 名不副实 | **未做** | 同 D |
| M `help` 清单与 Typer 一致 | **已做** | `f02d40ad`（2026-09-07，help 由 Typer 根生成）、`41b2f10a`（CI 比对）；当前 `cli_inventory.py`、`check_cli_help_inventory.py`、CI job `cli-help-inventory`。注意 help 会把隐藏一级也列出（“Hidden commands (internal)”） |
| N 兼容期 usage telemetry | **未做** | CLI 中没有 deprecated/调用计数机制；`a4b362e0`（2026-09-07）只加了**静态**引用计数脚本，现已归档到 `scripts/archived/count_cli_alias_references.py`，候选表（:23-41）仍列已删除的 `channel` |
| Phase 0-1 清单与可观测性 | **已做** | 同 M |
| Phase 0-2 `squire` 重复注册 | **已做** | `33cac756` 同时加断言 `groups.count("squire") == 1`（`test_retired_surface.py:38`） |
| Phase 0-3 生产 `channel:lark:*` shape 来源 | **UNKNOWN** | 不在仓内可验证范围；本轮未触碰生产 |
| Phase 1 | 仅 `channel` 一项完成 | `service`、`adapter user-auth`、internal namespace 均未做 |
| Phase 2 | 三条在途线全部合入，收口未做 | `selfupd` `2eee1b5a`、`home-guard` `622f91f5`、`actor-status` `94f0995e`（均 2026-08-24） |
| Phase 3 | 未做 | B 已作废；C 未做 |

## 5. 按 Allen 三条方向归类的收拢方案

沿用上次编号 A–N；本轮新增项从 O 开始。每项给出目标形态、原因、破坏性、会被打断的调用者和
最低迁移保护。

### 5.1 同类功能不要有多个一级 command

#### O. transfer 家族 14 个一级 → 一个 `transfer` 组（新，优先级最高）

目标形态：

```text
hyprial transfer move <name> --to <node>             # 现叶子 `transfer` 的冷迁移
hyprial transfer bundle export|validate|receive|land|complete
hyprial transfer credential seal|open                 # prototype，先隐藏
hyprial transfer runtime probe|downgrade-state
hyprial transfer internal precheck|receive|cred-stage|container-home|session-path   # 隐藏
```

原因：14/59 个一级入口属于同一个迁移域，且 AT06/07/11 bundle 流程是按步骤编号的一组动作，天然是
二级资源。现在的扁平拼写同时违反方向 1（同域多个一级）和方向 2（远端协议与原型暴露在一级）。

难点：`transfer` 现在是带位置参数 `<name>` 的叶子。改为组后，旧写法 `hyprial transfer <name> --to`
只能靠组 callback 兜底，且 worker 名若恰为 `move/bundle/credential/runtime/internal` 会与子命令撞名。
所以目标是显式 `transfer move`；按 4.3 直接删除 `transfer <worker>` 的旧写法，不保留组 callback。

破坏性：
- 可见 bundle/credential 七个：**低-中**。仓内只有测试、`transfer-at12-cross-machine-runbook.md:48`
  与冻结基线 `tests/fixtures/transfer_cli/command-graph*.json`（`test_transfer_cli_structure.py:21-31`
  逐字比对路由）。仓外调用未知，这批命令 2026-09-30 才进入。
- `transfer-runtime`：**低**。只有 `hyprial-ops` SKILL 与测试。
- 隐藏五个：**高**，见 I。

最低迁移保护：可见部分双入口 + warning 一个发布周期后隐藏旧名；同步 runbook、ops skill 与冻结基线。
**不得**顺带改持久化值 `hosted_by='transfer-receive'`（`identity/impl/agents/registry/_base.py:69,403`
的 SQLite CHECK 约束）——它是数据，不是命令名。

#### A（更新）. `autoupdate` + `upgrade` → 一个 `upgrade` 一级

`selfupd` 已落地，上次的前置条件满足；但入口又多出 `autoupdate restart` 与一级 `restart`。目标形态：

```text
hyprial daemon upgrade [--tag ...] [--force] [--no-restart]   # 唯一升级入口
hyprial daemon upgrade status                                 # 调度状态
hyprial daemon upgrade apply                                  # 仅在有 pending 时重启
hyprial config set autoUpgrade true|false              # 开关保持在 config
autoupdate run                                         # daemon 子进程入口，隐藏且保留
```

原因：“装什么版本、何时自动装、装完是否切换”是同一能力。`autoupdate restart` 与 `restart` 的差别
只是“无 pending 时什么都不做”（`cc/lifecycle/autoupdate.py:553-575` 对比 `cc/lifecycle/restart.py:240-320`），
后者已经会顺带确认并清除 pending（`restart.py:314-320`）。

破坏性：**高**。会被打断的调用者：发给人的告警正文（`daemon/impl/autoupdate/alert.py:777`）、
`autoupdate status` 输出里的提示（`cc/lifecycle/autoupdate.py:361,364`）、`hyprial-ops` SKILL:229-232、
`run-inprocess-autoupdate.py`、`cli-blackbox`，以及 daemon 子进程 argv
（`daemon/impl/autoupdate/__init__.py:271-278`：版本 N 的 daemon 调用新装版本的 CLI，混合版本必然存在）。

最低迁移保护：先改告警/输出文案为新写法并保留旧写法完全等价；`autoupdate run` 至少跨两个版本保留
parser；E2E-005/010 同时覆盖新旧拼写与“旧 daemon 调新 CLI”。

#### P（新）. daemon 生命周期入口 → `daemon` 组

目标形态：

```text
hyprial init                        # 保持：home 初始化 + 首次启动（home-guard 后唯一能建 home 的入口）
hyprial daemon run|stop             # 保持
hyprial daemon restart [--yes]      # = 现一级 restart
hyprial daemon status               # 取代 service，输出取 ps 的 daemon 段
hyprial ps                          # 保持：daemon + connector + session 总览
```

原因：一级 `restart` 与 `service` 都只作用于 daemon，却不在 `daemon` 组；而 `start/down` 作用于
connector，名字反而最像 daemon 生命周期。把 daemon 动作收进 `daemon` 组，`start/down` 留给 connector。

破坏性：**中**。`restart`：`contract/e2e/run-cli-restart.py`、upgrade 路径里的确认来源字段
`via="hyprial restart"`（`restart.py:316`，字段值属已发布语义，改名要走 Contract-Impact）。
`service`：见 D。

最低迁移保护：`restart` 保留为一级 alias 至少一个发布周期再隐藏；`via` 字段值不随命令改名。
`start/down` 不在本项改名（见 F）。

#### C（更新）. 消息观察面 → `delivery` 组

```text
hyprial delivery status ...
hyprial delivery trajectory <message-id>
hyprial delivery outbox list|prune
hyprial delivery inbox <actor> | delivery query <actor> inbox|outbox   # 名字待裁
```

原因：`delivery status`、`trajectory`、`outbox`、`query` 都在回答“这条/这些消息现在在哪”，
现在分散成 4 个一级，且 `delivery` 是单子命令组。`log`（全组件）与高频动作 `send/ack/reply` 保持一级。

破坏性：**中-高**。`run-outbox-deadletter.py:111,357`、`run-dual-instance-roundtrip.py:100`、
`run-pi-wake-registration.py:152,168`、`contract/wire/trajectory.md`、`hyprial-ops` SKILL:225,267。
最低迁移保护：双入口 + warning，先更新 runner 与 skill，再隐藏旧一级。

#### Q（新）. `grants-check`、`grants-visibility` → `agent` 下

目标形态：`hyprial agent grants-check ...`、`hyprial agent grants-visibility ...`，一级旧名隐藏。

原因：它们与 `agent grant/revoke/grants` 同属能力授权域，却独占两个一级。不用
`agent grants check` 的形态，因为 `agent grants [ACTOR]` 是带可选位置参数的叶子
（`cc/agents/admin.py:175-177`），改成组会与 agent 名撞名。

破坏性：**低**。只发现测试（`tests/identity/capabilities/test_capability_enforcement.py`、
`test_grant_visibility.py`）和 `docs/design/design-at10-capability-entry-map.md:46`。
最低迁移保护：旧名隐藏保留一个发布周期；同步设计文档。另见 Z（它们不读真实台账）。

#### E（更新）. `ps/top/targets/hosts/doctor/worktrees`：仍不合并

`actor-status` 已合入，数据层同源已实现，这支持“共享数据、保留视图”。新增的 `worktrees` 检查的是
本机 git worktree 遗留，不是 hyprial 运行状态，可以考虑未来作为 `doctor` 的一个检查项，但当前
`doctor` 是叶子；不建议现在动。`hosts` 见 S。

#### S（新）. `hosts` / `network` / `org network` / `profile`：等 Tailcat 切换后再定

- `hosts`：zenoh 宣告的 node 在线列表；
- `network expose|unexpose|exposures|peer-key`：Tailcat 入站服务与对端身份；
- `org network`：目录成员/设备、映射 peer 与 sidecar 状态；
- `profile`：选择 home 的网络 profile。

四处都在回答“网络上有谁、我连到哪”。目标形态建议为 `network peers`（合并 `hosts` 与 `org network`
的节点视图）+ `network expose...`，`profile` 保持独立（它选择的是 home，不是网络状态）。但 Tailcat
替换 zenoh 的切换尚在进行（重构线才有 `peer-key`），现在定型会立刻过时。

破坏性：**中**（若动 `hosts`）。CLI 调用者：`cli-blackbox/run.py:515`、`run-dual-instance-roundtrip.py:52`、
`run-peer-restart-relearn.py:597`、`_two_machine/local.py`；`transfer --to` 帮助文案。transfer orchestrator
走 IPC `hosts`，不受 CLI 改名影响。最低迁移保护：切换完成后再设计，`hosts` 长期 alias。

#### T（新）. `squire probe` 与 `dispatch matrix --probe`：同一数据域，需裁决

两者都探测 harness/provider/model 组合可用性：`squire probe` 记录可用性
（`cc/setup/squire.py:566`），`dispatch matrix --probe` 只诊断不选择（`cc/routines/dispatch.py:17-27`）。
`dispatch` 组只有一个子命令。建议先确认二者是否读写同一份可用性记录，再决定是否收成
`dispatch matrix|probe`。破坏性**低-中**（`dispatch matrix` 的调用者为两份 skill；`squire probe`
有 `run-squire-setup.py`）。本轮不给定形，标为**需 Allen 裁决**。

#### U（新）. 单子命令组：`config set`、`delivery status`、`dispatch matrix`

- `delivery status`：由 C 解决。
- `dispatch matrix`：由 T 决定。
- `config set`：组本身合理（8 个键，`cc/lifecycle/autoupdate.py:377-395`），问题是只能写不能读。
  建议**增量**补 `config get|list`，不移动 `set`；顺带把 `config_app` 从 `lifecycle/autoupdate.py:24`
  移到 `cc/config/`（纯代码卫生，零 CLI 破坏）。破坏性：**无**（只增）。

#### R（新）. `user` / `org` / `agent` / `adapter identities`：保留三个名词，`org` 内部再分层

`user`（人与平台账号的确认绑定）、`agent`（agent identity）、`org`（组织成员与上下文）是三类不同
对象，不建议合并。但：

- `org` 混合了两组语义：成员管理（`create/invite/join/leave/remove/delete/list/pending/network`；`execute` 已随 R3 删除）
  与组织上下文采纳（`show/status/fetch/import`）。可考虑 `org context show|status|fetch|import`；
  desktop 只调 `org join`、`org pending`（`deeplink/mod.rs:171,211`），不受影响。Squire template 原用
  `org status/show/import`，已随 R3 改用 `org context …`。破坏性：**中**。
- `adapter identities`（平台目录缓存：谁是谁）与 `user bind`（确认某平台账号是某人）容易混淆但
  不是同一对象。建议只在两边 help 互相指路，不合并。

#### B（作废）、F（维持）

B 见 4.2。F 维持：`agent` 管 identity，`start/down` 管 runtime binding；`down --all` 的跨对象语义
仍未拆开，直接改名为 `agent stop` 仍会说谎。

### 5.2 内部业务不应该暴露为一级 command

#### G（更新）. `lark-auth` → `adapter user-auth`

目标与上次相同：`hyprial adapter user-auth check|complete`。调用者变化：autoupdate 现在是**进程内**
调用（`cc/lifecycle/autoupdate.py:695`），不再经 argv，改名不影响它；argv 调用者为两个 contract
runner（`contract/e2e/feishu/identity.py:93`、`_harness_lifecycle/lark_auth.py:105`）；发给人的提示
文案在 `reauth.py:438,454`。破坏性：**中-高**（下调一档：内部 argv 调用者减少，但人工完成链接仍携带旧拼写）。
按 4.3 的直接删除裁决，R9 在同一提交中改文案与 runner，并删除旧入口，不保留双注册或弃用期。

#### H（更新）. `mcp` → 隐藏 internal process surface

结论不变，破坏性仍为**关键**。新增事实：交互 Claude 的 hook 以 `--settings` JSON 随每次启动传入
（`cc/agents/claude.py:257-347`），其中 `SessionStart` 与 `StopFailure` 两个 hook 调
`hyprial.cli mcp claude-channel-recover`（`claude.py:262-273,301-316`）。hook argv 活在已运行的
Claude 进程里，直到该会话重启才会换成新拼写，所以“launcher 先切”之外还要等存量会话自然轮换。
迁移保护在上次 1-4 条之外增加：E2E-006/012/013 覆盖“旧版本启动的会话 + 新版本 CLI”的 recover 路径。

#### I（并入 O）. 隐藏 `transfer-*` → `transfer internal ...`

现为 5 个，生成方是 `daemon/impl/transfer/orchestrator.py:373,450,473,491,547,624` 与
`execution/ssh.py:124`，另有 `contract/e2e/run-hosted-transfer.py:158` 直接调用 `transfer-receive`。
破坏性**高**：source/target 版本可能不一致。保护不变：目标端先同时接受两套 argv；source 在
destructive quiesce **之前**通过 capability/version 协商选择拼写；持久值 `hosted_by` 不改。

#### V（新）. `workflow` 的三级组

| 三级组 | 性质 | 建议 |
|---|---|---|
| `worker stop|restart` | 人/agent 的操作入口（`ccd1ddc6` 定型） | 保留 |
| `overview show|publish` | PAC 总览页，`hyprial-ops` SKILL:129 | 保留 |
| `notify resend` | 运维修复 | 保留 |
| `missions collect|finish|check` | Squire routine 的三步内部协议（`squire/SKILL-template.md:120-151`） | 属内部业务，但调用者就是 agent；保留，help 标注“squire routine step”，不进人类主文档 |
| `history list|status` | 已归档 legacy 历史只读 | 迁移保留期结束后隐藏 |
| `migration status` | schema 迁移报告（`pac/storage/migrations/upgrades.py:519` 提示它） | 同上，提示文案同步后隐藏 |

破坏性：**低**（只隐藏、不删 parser）。

#### W（新）. 首启/个人设置入口：`init`、`login`、`onboarding`、`squire`、`user-proxy`、`profile`

六个一级都服务首启。`onboarding plan|apply` 已是 desktop 的编排入口（`onboarding/cli.rs:158-198`）。
建议**暂不合并**：desktop 刚以这些拼写接线，且 `home-guard` 规定只有 `init` 能创建 home。可在 desktop
接线稳定后评估 `user-proxy` 并入 `squire`（同属个人代理设置）。破坏性若动：**中**。

#### J（维持）. `daemon run`、`autoupdate run`

`daemon run` 是部署入口（systemd/container/desktop），保持可见；`autoupdate run` 已隐藏，随 A 处理。

### 5.3 看起来没有作用的 command

#### K（已完成）. `channel`

见 4.1。

#### L（更新）. `service`：名不副实，且仍在被推荐给用户

它不是 no-op，但没有任何 service 管理能力，信息是 `ps` 子集；本轮帮助文案更误导。更要紧的是
**升级通知正文**（`sends.py:628`）在 daemon 重启期间让用户运行 `hyprial service --json`，Squire setup
仍把“Run hyprial service”列为 actionRequired（`squire/setup.py:636`）。建议见 P 与第 6 节 Phase 0：
先改文案，再以 `daemon status` 取代。`cli-blackbox` 的 sealed help 断言（`run.py:161`）需同步。
破坏性：**低-中**。

#### Y（新）. `install`：代码确认无业务效果

恒返回 `COMING_SOON`（`cc/lifecycle/install.py:18-40`），这是 Allen 2026-10-03 的显式裁决
（`ced60773`），目的就是让旧脚本响亮失败。建议**仅隐藏**（`hidden=True`，保留 parser），直到新安装故事
出现；是否隐藏需 Allen 确认，因为裁决原文只说“保留名字”。破坏性：**低**（help 清单测试与 skill 文本）。

#### Z（新）. `grants-check` / `grants-visibility`：看起来在判定，其实不读真实状态

两者的 grant records 全部来自命令行 `--scope` 等参数（`cc/agents/grants.py:56-84`），不查询 daemon 台账。
运维若以为它反映“某 agent 现在能不能做 X”，会得到错误结论。处理见 Q；若保留可见，help 需写明
“what-if evaluator, does not read the live ledger”。

#### AA（新）. 隐藏 `mcp claude-turn-ended`：产品内已无调用者

`17730bc5`（2026-09-30）加入它作为 Claude Stop hook；同日 `cb3d004a` 因 `hyprial.cli` 导入耗时超过
hook 超时，把 Stop hook 换成 stdlib 模块 `python -m hyprial.biz.impl.turn_pulse`。当前仓内只有
`tests/shell/cli/agents/claude/test_hooks.py:51-70` 调用它。唯一可能的真实调用者是
`17730bc5`–`cb3d004a` 之间版本启动、至今未重启的 Claude 会话。建议保留 parser 一个发布周期后删除
（它已隐藏，不占 help）。破坏性：**低**。

#### 原型暴露：`transfer-credential-seal|open`

help 自称 prototype（`cc/transfer/credentials.py:15,130`），却是可见一级。并入 O，并在产品化前隐藏。

#### M（已完成）. `help`

见 4.2。剩余小问题：`help` 列出隐藏一级（`cli_inventory.py` 的 “Hidden commands (internal)”），
隐藏只对 Typer `--help` 生效；若要真正“不对人承诺”，隐藏段应只在 `--json` 或调试开关下输出。

#### N（作废）. 调用计数

原建议在兼容期内记录旧写法的调用次数，作为删除旧入口的依据。4.3 裁决直接删除、不设兼容期，
这一前置条件随之取消。

## 6. 推荐的分期迁移

按 4.3 的裁决，各期都是直接删除旧写法；分期只按依赖和风险排序，每一项都在同一个提交里更新仓内
全部调用者并加回退守护测试。

### Phase 0：先修文案与卫生，不改命令

1. 修正仍在推荐 `service` 的文案：升级通知 `sends.py:628`、Squire setup `squire/setup.py:636`
   （在 D/L 落地前先指向 `hyprial ps --json`）。
2. `config_app` 移出 `lifecycle/autoupdate.py`（代码卫生）。
3. 删除 `scripts/archived/count_cli_alias_references.py` 的过期候选表（仍列 `channel`）；按 4.3 不再
   需要 alias 计数，该脚本是否保留另议。
4. 请 Allen 裁决剩余事项：`transfer` 叶子改组的形态、T、Y。

### Phase 1：只有人或测试调用的入口

1. Q：`grants-*` 移到 `agent` 下，删除旧一级名。
2. O 的可见部分：`transfer bundle|credential|runtime`，删除 7 个可见 `transfer-*` 一级和
   `transfer-runtime`；`transfer` 改为组，原叶子行为改为显式 `transfer move`；更新冻结基线。
3. P + D/L：新增 `daemon status|restart`，删除 `service` 与一级 `restart`。
4. U：补 `config get|list`。
5. Y：`install` 的处理（待 Allen 裁决）。
6. AA：删除隐藏的 `mcp claude-turn-ended`。

### Phase 2：程序生成的内部入口

这些入口的调用者是 hyprial 自己生成的 argv，必须在同一提交里改生成方：

1. I/O 的隐藏部分：5 个隐藏 `transfer-*` 改为 `transfer internal ...`，同时改 SSH orchestrator
   生成的远端 argv（`daemon/impl/transfer/execution/orchestrator.py`）。
2. H：`mcp` 内部化，同时改 launcher、Claude hook 与 desired state 中写入的 argv。
3. A 的 `autoupdate run`：改为新拼写，同时改 daemon 启动子进程的 argv。
4. G：`lark-auth` 改为 `adapter user-auth`，同时改 Feishu/harness runner 与人工完成提示。

### Phase 3：高频人类拼写

1. A：`upgrade status|apply`，删除 `autoupdate` 一级；告警正文与 skill 同步。
2. C：`delivery trajectory|outbox|query`，删除对应一级。
3. R：`org context ...`（同步 Squire template）。
4. S：Tailcat 切换完成后再设计 `network peers`。
5. fs 分层（可选）：保留高频节点动词 `ls/read/write/...` 在 `fs` 下，只把
   `purge-plan/purge/purge-status/unban` 收为 `fs purge plan|run|status|unban`、空间管理收为
   `fs space ...`。27 个叶子本身是“大但同域”，不违反方向 1，优先级最低。

## 7. 破坏面总表

“同提交需更新”一列是 4.3 下唯一的迁移保护：仓内调用者全部改掉，并加回退守护测试。仓外脚本与
旧版本不受支持。

| 建议 | 归到哪里 | 破坏性 | 同提交需更新 |
|---|---|---:|---|
| O 可见 transfer 家族 | `transfer bundle/credential/runtime`、`transfer move` | 低-中 | 测试、冻结 command-graph 基线、AT12 runbook、ops skill；`hosted_by='transfer-receive'` 是 SQLite CHECK 值，不随命令改名 |
| I 隐藏 `transfer-*` | `transfer internal` | 高 | SSH orchestrator 生成的 argv、hosted transfer runner |
| A `autoupdate` 收入 `upgrade` | `upgrade status/apply` | 高 | 告警正文、status 文案、ops skill、daemon 子进程 argv、E2E |
| P daemon 生命周期 | `daemon status/restart` | 中 | run-cli-restart runner；`via` 字段值不改 |
| D/L 退役 `service` | `daemon status`（取 `ps` daemon 段） | 低-中 | 升级通知正文、Squire 步骤、cli-blackbox sealed help |
| Q `grants-*` 下沉 | `agent grants-check/-visibility` | 低 | 测试、AT10 设计文档 |
| C 消息观察面 | `delivery trajectory/outbox/query` | 中-高 | outbox、pi-wake、dual-instance runner，ops skill |
| G `lark-auth` 下沉 | `adapter user-auth` | 中-高 | Feishu/harness runner、人工完成提示 |
| H `mcp` 内部化 | 隐藏 internal（名待裁） | 关键 | launcher、Claude SessionStart/StopFailure hook、desired state 写入的 argv、恢复 E2E |
| V workflow 迁移期三级组 | 隐藏 `history`/`migration` | 低 | 迁移提示文案 |
| R `org` 分层 | `org context ...` | 中 | Squire template |
| S 网络视图 | `network peers` | 中 | hosts runner、transfer `--to` 文案 |
| T probe 重叠 | 待裁 | 低-中 | 两份 skill、squire setup runner |
| U `config get/list` | 只增 | 无 | — |
| Y `install` | 待裁 | 低 | help 清单测试、skill 文本 |
| AA 删除 `mcp claude-turn-ended` | 删除 | 低 | 测试 |
| status 观察面合一（可选） | `status ps/top/...` | 很高 | 不建议；如做需单独设计评审 |

## 8. 本 PR 明确不做什么

- 不删除、重命名、隐藏或重接线任何命令，也不改任何文案；第 6 节 Phase 0 的文案修正另开 PR。
- 不把隔离 introspection、历史提交中的 E2E 记录说成当前全量 CI/E2E green；本轮未实跑任何命令业务路径。
- 不把“没有静态找到调用者”写成“没有调用者”。
- 不修改重构线 `refactor/reconstruct-modules` 的代码；只引用其路径作为证据。
- 不审、不批、不合并本 PR；作者只负责提交分析供 Allen 裁决。
