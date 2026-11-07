# hyprial CLI 重构实施计划（cli refactor plan）

- 日期：2026-10-06。本文是**实施计划**；目标态（改完长什么样）见 [expected-commands.md](expected-commands.md)（下文简称《目标》），
  现状盘点见 [cli-top-level-command-consolidation.md](../../reference/cli-top-level-command-consolidation.md)（《盘点》）。
- 范围：Allen 2026-10-06 指示——§0.2 重叠排查的 A1–A8、B、C **全部要修**，在 #1126 内规划；本文给出每项的 PR、顺序与依赖。
- 代码基线：dev `74c86b5f` 或更新。文中 A/B/C 的 `file:line` 按重叠排查当时的基线 `085fd2cc` 核对；
  `085fd2cc` 不是 `74c86b5f` 的祖先，行号在 dev 上可能漂移（例如 `_restart_daemon_onto_install` 在
  `085fd2cc` 是 `upgrade.py:471`，在 `74c86b5f` 是 `:446`），实施时以函数名为准。
- 路径简写：相对 `src/hyprial/`；`cc/` = `shell/impl/cli/commands/`，`app/` = `daemon/impl/application/`。

规则（每个 PR 都适用）：

1. 旧写法直接删除，不留别名、不设过渡期。
2. 同 PR 更新仓内全部调用者（§4 固定同步清单），并加回退守护测试（旧拼写必须报错）。
3. 本机不跑全量；CI Linux unit 是闸门。本机只跑本 PR 触及的测试文件。
4. 每个 PR 只做一件事：M0 只搬代码、F 只改行为、R 只改命令面。F 可以删除其背后功能在本 PR 被删的命令（F3 的 `agent keep`、F8 的 `autoupdate install/uninstall`），但不改名、不移动命令。

## 1. Phase M0：拆包（前置条件，纯代码移动，行为零变化）

所有 R PR 落在 M0 之后的新结构上。`entry.py` 的 re-export 别名层与冻结基线不动
（`test_transfer_cli_structure.py` 冻结 `cli.<name>` 属性）。模块边界门对 `shell/impl/cli/**` 内部嵌套无限制。

### 1.1 现状（`74c86b5f` 实测）

`cc/` 顶层 7 个文件已满（`__init__`、`daemon_process.py` 977 行、`messaging.py`、`root.py`、`services.py`、
`start.py`、`support.py` 726 行）；子目录 `adapters/`（含 `mcp.py`）、`agents/`（含 `users.py`、`grants.py`、
`admin.py`、`claude.py`、`interactive.py`、`sessions.py`）、`config/`（`lark_auth.py`、`network.py`、`profile.py`）、
`lifecycle/`（`autoupdate.py`、`daemon.py`、`down.py`、`install.py`、`restart.py`、`upgrade.py`）、
`observe/`（`doctor.py`、`logs.py`、`meta.py`、`overview.py`、`status.py`、`worktrees.py`）、`orgfs/`、
`routines/`（`dispatch.py`、`routine.py`、`workflow.py`）、`setup/`（`first_run.py`、`gui.py`、`login.py`、
`login_flow.py`、`squire.py`）、`transfer/`。结构门：每目录 ≤7 个直接文件、每文件 ≤1000 行。

`daemon_process.py` 977 行距门 23 行，M0.1 必须把它拆开（停/启/证明分文件），否则 F5/F11 一改就破门。

### 1.2 PR 拆分

| PR | 内容 |
|---|---|
| M0.1 下沉公共层 | 新建 `cli/common/`，收 `root.py`、`services.py`、`support.py`、`daemon_process.py`（拆为启动 / 停止与证明 / pending-restart 三个文件）；下沉下表 7 组私有函数；`config_app` 从 `lifecycle/autoupdate.py` 抽出 |
| M0.2 组转 package | 按《目标》的 23 个一级建包：`agent/ message/ host/ daemon/ upgrade/ user/ adapter/ org/ fs/ workflow/ routine/ squire/ socialware/ init/ service/ config/ profile/ gui/ meta/` 等；现 `config/` 目录里的 `lark_auth.py`、`network.py`、`profile.py` 分别迁到 `adapter/`、`host/`、`profile/`，腾出 `config/` 给 `config set/get/list`。顺序：`transfer`（已是包）→ `adapter` → `agent` → `org`+`orgfs` → `workflow` → 其余小组。`add_typer` 名字不动（改名是 R 的事） |
| M0.3 叶子拆文件 | `messaging.py` → send/ack/reply/log；`observe/status.py` → ps/top；`observe/overview.py` → targets/hosts；`observe/logs.py` → log/query/trajectory；`setup/first_run.py` → init/onboarding；`setup/squire.py` → squire/user-proxy；`lifecycle/autoupdate.py` → upgrade/config |

大组拆分要点：`agent/` 由 `admin.py` + `grants.py` 拆为 create / grant / admin / secret / home / restore 各一个 module；
`workflow.py`（902 行）拆为 run / inspect / control + overview / missions / misc；`adapter/` 满 7 后新增命令一律走子包；
`transfer/` 的 `session.py` 拆为 move（可见）与 internal（隐藏协议）。

### 1.3 拆包前必须下沉的 7 组共享函数

| 现位置（`cc/`） | 消费者 | 去向 |
|---|---|---|
| `setup/first_run.py::_append_warning` | `lifecycle/restart`、`agents/interactive` | `common/support.py` |
| `setup/squire.py::_user_proxy_launch_params` | `start.py` | `common/support.py` 或 CliServices 字段 |
| `setup/squire.py::_squire_setup_operation` | `setup/first_run.py` | squire 包公共面或 CliServices 字段 |
| `adapters/routes.py::_route_operation` | `setup/first_run.py` | adapter 包公共面或 CliServices 字段 |
| `routines/routine.py::_routine_identity` | `agents/admin.py` | CliServices 字段（owner 归 routine 包） |
| `observe/overview.py::_render_routine_list/_render_workflow_list` | `routines/{routine,workflow}` | `common/support.py` 的 overview 渲染段 |
| `lifecycle/restart.py` 的 pending-restart 读写、`lifecycle/upgrade.py::_restart_daemon_onto_install`、`lifecycle/install.py::_install_locked_requirement` | `lifecycle/autoupdate.py`、`lifecycle/upgrade.py` | `common/` 的 daemon 进程段（F5 会把 restart 的停启块并到这里） |

`routines/workflow.py::_identity` 经 `shell/interface.py` 以 `cli_identity` 对外暴露，迁移时保持该导出路径。

## 2. Phase F：功能修复（每项一个 PR，先于依赖它的改名）

顺序按"最可能给出错误答案"排：A2、A5 → A3 → A6 → A1 → A4 → A7 → A8 → C 与 B 末项。
F 系列可在 M0 之后任意并行，但同一文件上的 PR 按表内依赖串行。

### F1 — A2 "平台账号 → 人"：改为以 login 为准的唯一解析（随 org-improver 身份线落地）

- 问题：`user bind`、`adapter identities upsert`、`squire setup --owner-open-id` 写三处存储（`users.sqlite3`、适配器
  identities 表、`users.json` 的 `owner_open_id`），入站按 users 库 → identities 表兜底，`owner_open_id` 另由三处读取；
  且都是单机的，节点之间可能对同一账号给出不同答案。入口与精确位置见 org-improver 设计分支
  `docs/design/notes-identity-entrypoints-2026-10-06.md`。
- 决定（Allen 10-06 把"以 hyprial login 为唯一身份来源"交给 org-improver，卡 21aafdb0；契约
  `docs/design/design-identity-resolver-2026-10-06.md` @ d1d36662）：键是 **union_id**（各飞书 app 间一致，open_id 每个 app
  不同），权威在 **Casdoor**（`properties.oauth_Lark_unionId`）；daemon 只有一个解析器
  `identity.resolve(lark, union_id) → {user, source, verified} | None`，不回落 open_id；`users.sqlite3` 变成按 union_id 的
  缓存加上需要 login step-up 的本机覆盖。
- **顺序（契约 §3，强制）**：Casdoor 补丁 → 解析器与 `people/*` 发布上线（org-improver 线）→ 本 PR 才删除三条旧路径。
  ⛔ 解析器上线前不得删除旧路径（今天主人自己的飞书消息靠 `user bind` 才是 verified）；还要先解决"无组织节点上非主人
  发送者"的缺口（step-up 或共享组织）。
- 解析器上线前可先做（不改行为）：
  1. §2.3 按 union_id 发送的适配器操作 + 假 SDK 单测；作为闸门，由 hq 在 E2E app 上安排一次真实发送验证，通过后出站才改走 union_id；
  2. 只读迁移清单：列出每条现有绑定（users 库、verified identities 行、`owner_open_id`）有无 union_id、来源，供主人确认；
     迁移时缺 union_id 的行经各适配器自己的 app 通讯录接口回填，失败的列给主人手工确认，绝不按姓名或邮箱自动匹配；每行记来源（api-backfill / owner-confirmed）。
- 测试：契约 §4 的"去掉守卫必须变红"的测试；迁移幂等、冲突可见；入站只走解析器。
- 风险：高（发送者认定错误直接可见）。CLI 形态（`user` 二级）不在本 PR，见 R13，同样等解析器上线。
- 依赖：M0；org-improver 解析器上线（契约 §3 第 2 步）。

### F2 — A5 探测可用性统一一个判定

- 问题：同一探测命令两套判定——`daemon/impl/squire/probe.py:70`（`classify_run`）vs `daemon/impl/dispatch/matrix.py:334`（只看 exit 0 且输出非空）；同一组合可能结论相反。
- 修法：探测收成一个入口 `agent tiers probe`（原 `squire probe`），只用 `classify_run`；`dispatch matrix --probe` 的诊断路径删除，`agent tiers` 只读 UserProfileStore 的记录（Allen 2026-10-07 squire 拆分）。
- 测试：同一组 `ProbeRun` 夹具（超时、非零退出、限流"try again at"、空输出）两边结论一致。
- 风险：中（原 dispatch 诊断读数消失，改看记录）。依赖：M0。

### F3 — A3 keep 名单并入 restore always

- 问题：keep 名单是第二份存储（`identity/impl/agents/state/activity.py` + `keep_actor.py`），在 `app/restore.py:323-351` 优先级最高、会压过 `never`；`registry_ops.py:511` 另有 keep 唤醒；doctor 推荐 keep（`cc/observe/doctor.py:227,240`）。
- 修法：删除 keep 存储、读取路径与 `agent keep` 命令；restore 只看 per-agent 策略；doctor 改推荐 `agent restore-policy <NAME> always`（R1 再改为目标拼写）。
- 迁移：daemon 启动时一次性读 keep 名单，对每个名字写 `always`，然后删除名单文件；名单里的 `never` 冲突按 keep 语义（`always`）处理并记日志。
- 测试：迁移幂等；迁移后重启恢复集合与迁移前一致；`never` + keep 的旧组合迁成 `always`；`agent keep` 报错。
- 风险：中。依赖：M0。被 R1 依赖。

### F4 — A6 squire setup / onboarding 走正式 IPC

- 问题：`daemon/impl/operations/management.py:269,291` 直接 `agents.create` / `agents.pin`，绕过 `agent.create` 的收尾（默认 routine、旧 pin 迁移、persona route）和 `adapter.pin` 的旧 pin 清理。
- 修法：改调 `agent.create` 与 `adapter.pin` 的同一实现。
- 测试：squire setup 后的 agent 与 `agent create` 建出的收尾状态一致（routine、persona route、旧 pin 已清）。
- 风险：中。依赖：F1（同碰 squire setup）。被 R8 依赖。

### F5 — A1 restart 只留一份停启实现

- 问题：`cc/lifecycle/restart.py:249-293` 的停启块几乎逐字复制 `_restart_daemon_onto_install`（`cc/lifecycle/upgrade.py:471-528`）。
- 修法：抽一份 `restart_daemon` 到 `common/`，`restart`、`upgrade`、`autoupdate restart` 共用；命令名不动。
- 测试：三个入口走同一函数（mock 断言）；停启失败路径的输出不变。
- 风险：中。依赖：M0.1。被 F8、F11、R5 依赖。

### F6 — A4 在线节点过滤只写一次

- 问题：`_ipc_hosts` 与 `_online_host_nodes` 同一过滤写了两遍（`app/messaging/status/views.py:742,757`）。
- 修法：`_ipc_hosts` 调 `_online_host_nodes`。
- 测试：同一 presence 夹具下 `hosts` 与 `ps.meshNodes` 输出相同节点集。
- 风险：低。依赖：M0。被 R4 依赖。

### F7 — A7 outbox 只留一份序列化

- 问题：`outbox list` 与 `query <actor> outbox` 同一数据源两套序列化（`app/messaging/inbox_surface/queries.py:384` vs `:156`）。
- 修法：`query … outbox` 改用 `_outbox_entry_json`，按 actor 过滤；JSON 字段以 `outbox list` 为准。
- 测试：同一 outbox 两入口逐字段一致；e2e 契约读到的字段名不变或同 PR 更新。
- 风险：中（JSON 契约）。依赖：M0。被 R2 依赖。

### F8 — A8 删除旧系统定时器

- 问题：`autoupdate install` 装的 launchd/systemd 定时器（`daemon/impl/operations/service.py`、`daemon/impl/autoupdate/__init__.py`）与 daemon 调度器并存；`autoupdate run` 在 daemon 不在时会自己做本地升级。
- 修法：删除定时器安装代码与 `autoupdate install/uninstall`；`autoupdate run` 在 daemon 不在时直接退出、不再本地升级；保留 run 作为 daemon 的升级子进程。
- 迁移：daemon 启动时检测旧单元，存在则卸载并记一条日志（取代 `uninstall` 命令的唯一用途）。
- 测试：有旧单元的 home 启动后单元被移除；`run` 无 daemon 时不升级；`install/uninstall` 报错。
- 风险：中（卸载失败会留残留单元；需要卸载失败可见）。依赖：F5。被 R5 依赖。

### F9 — C 统一"闲置"定义

- 问题：三个独立定义——restore 阈值 12h（`identity/impl/agents/state/worker_state.py:22`）、`agent list --inactive-since`（`app/actors/agents_admin/registry_ops.py:386-390` 按参数过滤）、`top` 写死 30 分钟（`daemon/impl/operations/top.py:79`）。
- 修法：一个"上次活动时间 + 阈值"函数供三处调用。**已定（Allen 2026-10-10 批准按 cli-developer 建议）**：判定基准＝"距最后活跃时间 ≥ 阈值"（不是"有没有 turn 刷新"的布尔）；恢复判定保留 12h；`agent list --inactive-since` 保留为参数；**`top` 保留 30 分钟窗口但展示语义改名为"近期无输出（quiet）"**，"闲置"一词只留给恢复语义。**⛔ 不做 base URL 流量监控 hook**（harness 直连 provider API，流量不过 daemon；观测点不存在，出站代理＝架构大改＋隐私影响）。可选增强：以 harness 会话文件 mtime 作为 `last_activity` 的只读旁证（另立小 PR）。定稿与排程详见 [cli-refactor-implementation-schedule.md](cli-refactor-implementation-schedule.md) §1；实施后回填《目标》§3.1。
- 测试：同一 agent 在三处的闲置判断一致（时间注入夹具）。
- 风险：中（`top` 显示会变）。依赖：F3（同碰 restore）。被 R1 依赖。

### F10 — C `agent grant` help 说真话

- 问题：`cc/agents/admin.py:152` help 写 "Recording does not enforce runtime permissions"，但 see-actors / send-to 被 `app/messaging/visibility.py:498-523` 每次请求强制执行。
- 修法：help 按能力写明哪些被强制；`grants-check/-visibility` help 写明 what-if、不读台账。
- 测试：help 文本断言。风险：低。依赖：无。

### F11 — B 末项 daemon 停止严格度统一

- 问题：`daemon stop` 走 `_stop_daemon_for_operator`（`cc/daemon_process.py:701`，`require_exit=False`），restart / upgrade 走默认 `require_exit=True`。
- 修法：`daemon stop` 改用严格版。注意该函数 docstring 记录"失败式严格曾被测为错误——旧进程此刻通常仍在退出"：严格版必须先等退出再判定，不能一见存活就失败；PR 内要附等待窗口的实测。
- 测试：正常停止不误报；旧进程不退出时报错并给出 PID。
- 风险：中。依赖：F5。被 R5 依赖。

## 3. Phase R：改名与移动（按领域成组，每组一个 PR）

每个 R PR：命令面改到《目标》，删除旧一级/子命令，删除列出的 B 类重复入口，跑 §4 同步清单。

| PR | 领域 | 命令变化 | 删除的 B 类入口 | 依赖 |
|---|---|---|---|---|
| R1 | agent | `start`/`down` → `agent start/stop`；`top` → `agent top`；`agent list` 加运行状态列；transfer 家族 → `agent move` + 隐藏 `agent transfer …`（同 PR 改 `daemon/impl/transfer/orchestrator.py` 生成的远端 argv、冻结基线 `tests/fixtures/transfer_cli/command-graph-declarations-epoch.json`）；`restore-policy/-threshold` → `agent restore`；`home-census`/`migrate` → `agent home …`；`grant` 动词化，删 `revoke`、`grants <ACTOR>`、一级 `grants-check/-visibility` | B1 `ps`/`agent list`/`top`/`targets` 共用 `_agent_status_json` 与 `_actor_status_snapshot`：`agent list`、`agent top` 成为唯一两个读者（`ps` 本身在 R7 删）；B3 `down lark`（`agent stop` 不再接受适配器） | M0、F3、F9 |
| R2 | message（观察面） | `delivery status`、`trajectory`、`outbox`、`query`、`targets` → `message status/trajectory/outbox/inbox/targets` | B7 `delivery status`/`trajectory`/`log` 改为同一读取实现；B9 `targets --kind channel_route` 删除（路由看 `adapter route list`） | M0、F7 |
| R3 | org | **已落地**：`org execute` 删除；`org show/status/fetch/import` → `org context …`（squire 模板两份已同步）；`org join` 保留（B4 另一半另见 R12/后续） | B4 `org join` 与 `org execute` | M0 |
| R4 | host | 一级 `network` → `host`；`hosts` → `host list`；`host status`/`host create` 注册为 `COMING_SOON`，使规划中的入口与拼写错误可区分，且在实现前不会静默成功 | — | M0、F6 |
| R5 | daemon + upgrade | **已落地**：`restart` → `daemon restart`；新增 `daemon start`（复用 `_launch_daemon_process_locked`，`run` help 标注前台/进程管理器用）；`autoupdate status/restart/run` → `upgrade status/apply/run`（`run` 隐藏，daemon 调度器与遗留 launchd/systemd 模板的 spawn argv 已同单改为 `upgrade run`）；`upgrade --source/--ref` 从 help 隐藏；`upgrade` 安装后顺带解包配套 GUI 组件，`gui upgrade` 只作修复。**本单偏差**：F8 未在本分支基线里，`autoupdate install/uninstall` 仍保留，`autoupdate` 一级随 F8 合入后一并消失；本单不做 A8 行为改动（R 只改命令面） | — | F5、F8、F11 |
| R6 | workflow + routine | **已落地**：`status`/`context` 并入 `inspect`（`--node`、`--context`，旧拼写删除）；一级 `worktrees` 的只读建议并入 `workflow gc --dry-run`（`[REPO]...`/`--base`/`--all` 保留），一级 `worktrees` 删除；`routine rm` → `remove`；`history/migration` 迁移期隐藏（help 不可见、仍可调用），迁移结束后删除。`gc` 回收已关闭 graph 与节点 worktree 是新功能，另立设计卡（TODO），不随 R6 实现 | B10 `agent tiers`（原 `dispatch matrix`）与 `start --tier` 共用 `resolve`：已加断言（spy 同一 `matrix.resolve`）证明两边同源；`workflow dispatch` 已于 2026-10-07 删除，不复活 | M0、F2 |
| R7 | service + daemon status | **已落地**：#1199（service connect v0.4）落了 `daemon status`、裸 `service` 改为 help、`service connect/disconnect/list/guide`、`sends.py`/`squire/setup.py` 文案；本 PR 删一级 `ps`，`host expose/unexpose/exposures/peer-key` → `service expose/unexpose/exposures/whois`（§2.13），并按真实能力重写 `service` help | B2 `adapter list` 与 `ps.adapters`（`ps` CLI 删除，`adapter list` 成唯一 CLI 读口）；B8 `service` help | R1（`agent list` 已有运行状态列） |
| R8 | squire + init + tiers | `squire probe` → `agent tiers probe`、`dispatch matrix` → `agent tiers`（`agent start --tier` 共用解析，B10）；`user-proxy` → `user proxy`；`squire` 只留 `setup`；`onboarding plan/apply` → `init --plan`/`init`（同 PR 改 desktop `desktop/src-tauri/src/onboarding/cli.rs` 拼的 `onboarding` argv 与 desktop 契约 §11） | B5 `init` 与首次 `login`（首启只走 `init`）；B6 `start user-proxy` ⊂ `user-proxy setup`（`agent start` 拒收 user-proxy） | F1、F2、F4、R1 |
| R9 | adapter | **已落地**：`lark-auth` → `adapter user-auth`（同步 Feishu/harness runner 与人工完成提示）；`onboard` 并入 `add`（默认 onboarding 流程，带现成 App 参数只登记）；`doctor`/`authorize` → `scopes check/authorize`；删 `pins`，`adapter list` 加 `agent` 绑定列 | — | M0 |
| R10 | socialware | `install` → `socialware install`（恒 `COMING_SOON`） | — | M0 |
| R11 | mcp 出 CLI 树 | channel 服务器改模块私有入口；同 PR 改 spawn argv：`daemon/impl/harnesses/turn_delivery/channel/worker_channel.py:123,138`、`daemon/impl/mcp/channel/loops.py`、`contract/e2e/run-all-harness-lifecycle.py`；删 `mcp claude-turn-ended`（《盘点》AA） | — | M0 |
| R12 | **send/ack/reply + fs（爆炸半径最大）** | **已落地**：`send/ack/reply` → `message send/ack/reply`；`fs` 内部重组（`space`/`member`/`trash`/`purge` 子组；`fs` 保留一级）；一级 `send`/`ack`/`reply` 删除（无别名） | — | M0、R2、R3；单独窗口，建议放最后 |
| R13 | user | F1 之后的命令面跟进：`user` 二级定形；删 `adapter identities upsert`、`squire setup --owner-open-id` 的独立登记入口 | —（A2 本身归 F1） | F1；**阻塞于 Allen 定形态** |
| R14 | agent home provision | 新增 `agent home provision <NAME>`（IPC `agent.home.provision`，卡 905a6523，hyprial-cli-developer 实现）。daemon/IPC 侧先落地；CLI 入口在 M0.3 之后以小 PR 加在目标位置（先建只含 provision 的 `agent home` 子组，R1 再把 census/migrate 迁入）。幂等：已有就绪 home 返回 `provisioned:false`；home 存在但不可用 → `AGENT_HOME_CONFLICT`；未知 agent → `AGENT_NOT_FOUND` | — | M0.3、卡 905a6523 的 daemon 侧 |
| R15 | PAC overview serve | `workflow overview serve` / `routine overview show\|serve`：统一 pac overview 模块（模型、页面、SSE 服务各一份），两个入口只差 `--focus`；默认 127.0.0.1，`0.0.0.0` 显式给出并提示无认证；只读 | — | M0.3（workflow/routine 拆包之后） |
| R16 | workflow progress | `workflow progress <GRAPH> <NODE> --note`（A-cli）与 list/inspect/overview 的 `lastProgressNote` 展示；daemon 侧 `pac.progress` + MCP `workflow_progress`（A-daemon，hyprial-cli-developer）先落地（#1210） | — | M0.3、A-daemon |
| R17 | org work | #1230（hyprial-cli-developer）在 `commands/org/` 下落地 `org work add…repair` 与 `--org` 解析（work 所在 space = 组织目录 space `group-<NAME>`）。随后一个小 PR 把 `workflow missions collect\|finish\|check` 搬成 `org work collect\|finish` 与 `ls --invalid`，删除旧路径（不留别名），同 PR 改 squire `SKILL-template.md` 的调用方（:120/:124/:139/:155），PR 说明里请 hq 在安装后重新生成各 squire 家目录的技能副本，并核对 routine 定义里有无 `workflow missions` | — | M0.2（`org/` 包已建）；work items 设计 §3 @66b132a9 |

R12 的调用面：skill、squire 模板两份、desktop、e2e 契约与 `contract/cli-blackbox/run.py` 里 `send`/`ack` 的字面量都在此 PR 改完；
其中 agent 侧 MCP 工具名（`harness_send` 等）不是 CLI 拼写，不受影响，但其文档里引用的 CLI 拼写要改。

## 4. 固定同步清单（每个 R PR 必做）

1. 实现侧：装饰器名、`shell/impl/cli/runtime/entry.py` 的 `add_typer` 名、transfer 包的 `register_transfer_commands`、`entry.py` re-export 别名层。
2. 测试 argv 字面量：`tests/shell/cli/**`、`tests/integration/transfer/**`。
3. 冻结基线：`tests/fixtures/transfer_cli/command-graph-declarations-epoch.json`（仅 R1）。
4. 概念守护：`tests/shell/cli/presentation/test_cli_help_inventory.py`（钉一级名）、`.../presentation/test_cli_top_level_concepts.py`、`tests/shell/compatibility/test_retired_surface.py`（加旧拼写守护）、`.../presentation/test_cli_help_coverage.py`（参数预算）。
5. 契约：`contract/cli-blackbox/run.py:160-164` 钉 `version ps doctor send ack log targets` 与 sealed `hyprial service`、`hyprial adapter pin`。分到 PR：R2 改 `targets`；R7 改 `ps`，`hyprial service` 重钉或删（视 R7 是否注册 `service` 占位叶而定）；R12 改 `send`、`ack`。其余 e2e runner 按《盘点》§7 表逐项改；否则 PR 正文写 `Contract-Impact:`。
6. 模板与 skill：squire 模板两份（`docs/domains/squire/SKILL-template.md`、`src/hyprial/daemon/impl/squire/SKILL-template.md`）、`.agents/skills/*/SKILL.md`；PR 正文写 `Skill-Impact`。
7. desktop：`desktop/src-tauri/src/**` 里拼出的 argv（R8 必改；其它 PR grep 确认）。
8. 文档：`docs/` 内引用；《目标》若有偏差同 PR 回填。
9. 无需手工同步：`hyprial help` 文本与 `check_cli_help_inventory.py`（渲染自注册树）。

## 5. 顺序与依赖

```text
M0.1 → M0.2 → M0.3
  ├─ F1(A2) → F4(A6) ─────────┐
  ├─ F2(A5) ──────────────────┼→ R8(squire+init) ;  F2 → R6(workflow)
  ├─ F3(A3) → F9(C 闲置) ──────→ R1(agent) → R7(service+daemon status)
  ├─ F5(A1) → F8(A8), F11(B 末项) → R5(daemon+upgrade)
  ├─ F6(A4) → R4(host)
  ├─ F7(A7) → R2(message 观察面) ─┐
  ├─ R3(org) ─────────────────────┴→ R12(send/ack/reply+fs，最后)
  ├─ F10(C grant help)、R9(adapter)、R10(socialware)、R11(mcp) 无前置依赖
  └─ F1 → R13(user，阻塞于 Allen)
```

| # | PR | 前置 |
|---:|---|---|
| 1 | M0.1 下沉公共层 | — |
| 2 | M0.2 组转 package | M0.1 |
| 3 | M0.3 叶子拆文件 | M0.2 |
| 4 | F1 A2 身份存储合一 | M0 |
| 5 | F2 A5 探测判定合一 | M0 |
| 6 | F3 A3 keep → always | M0 |
| 7 | F4 A6 squire 走正式 IPC | F1 |
| 8 | F5 A1 restart 合一 | M0.1 |
| 9 | F6 A4 节点过滤合一 | M0 |
| 10 | F7 A7 outbox 序列化合一 | M0 |
| 11 | F8 A8 删旧定时器 | F5 |
| 12 | F9 C 闲置定义合一 | F3 |
| 13 | F10 C grant help | — |
| 14 | F11 B 停止严格度 | F5 |
| 15 | R1 agent | F3、F9 |
| 16 | R2 message 观察面 | F7 |
| 17 | R3 org | M0 |
| 18 | R4 host | F6 |
| 19 | R5 daemon + upgrade | F5、F8、F11 |
| 20 | R6 workflow + routine | F2 |
| 21 | R7 service + daemon status | R1 |
| 22 | R8 squire + init | F1、F2、F4、R1 |
| 23 | R9 adapter user-auth | M0 |
| 24 | R10 socialware | M0 |
| 25 | R11 mcp 出 CLI 树 | M0 |
| 26 | R12 send/ack/reply + fs | R2、R3 |
| 27 | R13 user 定形 | F1、Allen 裁定 |

推进方式（Allen 2026-10-07）：M0.1 → M0.2 → M0.3 各自单独合入，串行窗口内其它 CLI PR 暂停（2026-10-07 起所有 PR 改走当前 `intg/batch-*` 集成分支，不再直进 dev；M0.2 #1237、M0.3 #1308 均经 intg）；M0.3 落地后，F/R 按本表依赖每次并行 2 个，PR 先合进集成分支（不必每个都跑完整 dev 闸门），攒成几批再整体进 dev。R13（user）与 org-improver 的 daemon 侧身份 IPC 同步，按冻结契约（design-identity-resolver §5）落地。

## 6. 不做

- 不加别名、不设弃用期；旧拼写一律报错（回退守护测试）。
- transfer 协议级命令只隐藏不删除（`agent transfer …`，两端 daemon 与脚本仍在用）。
- `host create`、`host status`、`service connect/…` 只预留入口，本计划不实现其功能。
- 不给 `grant … check/visibility` 加 `--live`（D7，另议）。
- 不改持久化数据值：`hosted_by='transfer-receive'`、`via="hyprial restart"`。
- `fs` 内部分层（`purge …`、`space …`）不在本轮。
- 已核对为确实不同、不动：`worktrees` 与 doctor、`adapter scopes check` 与一级 `doctor`、`agent home-census`、`grants-check` 与 `grants-visibility`、`fs` 与 `org` 的 join/invite、`network exposures` 与 `org network`、`adapter add --route` 与 `adapter route add`。

## 7. §0.2 条目 → PR 对照（每项恰好一次）

| §0.2 条目 | PR |
|---|---|
| A1 `restart` vs `upgrade`/`autoupdate restart` | F5 |
| A2 `user bind` / `adapter identities upsert` / `squire setup --owner-open-id` | F1（R13 为其后续命令面，阻塞于 Allen） |
| A3 `agent keep` vs `restore-policy always` | F3 |
| A4 `hosts` vs `ps.meshNodes` | F6 |
| A5 `dispatch matrix --probe` vs `squire probe` | F2 |
| A6 `squire setup`/onboarding 绕过正式 IPC | F4 |
| A7 `outbox list` vs `query <actor> outbox` | F7 |
| A8 `autoupdate install` 定时器 vs daemon 调度器 | F8 |
| B1 `ps`/`agent list`/`top`/`targets` | R1 |
| B2 `adapter list` 与 `ps.adapters` | R7 |
| B3 `down lark` 与 `adapter stop` | R1 |
| B4 `org join` 与 `org execute` | R3 |
| B5 `init` 与首次 `login` | R8 |
| B6 `start user-proxy` ⊂ `user-proxy setup` | R8 |
| B7 `delivery status`/`trajectory`/`log` | R2 |
| B8 `service`（只读探测，help 写"管理服务"） | R7 |
| B9 `targets --kind channel_route` 与 `adapter route list` | R2 |
| B10 `dispatch matrix` 与 `start --tier` | R8（`agent tiers`） |
| B11 `daemon stop` 与 restart 的 `require_exit` | F11 |
| C1 三种"闲置"定义 | F9 |
| C2 `agent grant` help 写"不强制"但实际强制 | F10 |
