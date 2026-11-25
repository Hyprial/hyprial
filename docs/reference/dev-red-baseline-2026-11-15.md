# dev 分支红色基线（2026-11-15）

本文是一份工作清单：列出 2026-11-15 当天 `dev` 上所有处于红色状态的测试、门禁与 CI job，
给出每项的分类、原因、归属、修复方向和验证命令，供独立开发者逐项修复。本文只做调查与登记，
没有修改任何产品代码、测试或 workflow。

- 调查基线：`dev` 的 `086070982`（#2831 合入后）。调查期间 `dev` 前进到 `2c1befe8e`（#2832），
  该提交的 CI 结果已并入第 2 节，但本地实测全部在 `086070982` 上完成。
- 既有设计与决定：[dev CI 稳定化设计](../design/dev-ci-stability-2026-11-14.md)
  （含 owner 2026-11-14 对 external promotion、GUI signals、daemon-request-browser “不修、随旧宿主退场”的裁定）。
  #2775（“record dev stability evidence and residual owners”）已关闭且未合入，其台账文件不在 `dev` 上，本文不依赖它。
- 已登记问题见 [known-problems](known-problems.md)；本文新增条目为 KP-065 至 KP-078。

## 0. 证据标记与取证条件

每条结论带一个标记：

- **实测**：在本地工作树执行，写明命令与次数。
- **CI 日志**：取自 CI 服务器 job 日志，写明 job 名、job 编号、运行时间（UTC+8，均为 2026-11-15）与引用行。
- **读代码**：由源码或 `git log` 得出，未执行。
- **推断**：由若干事实推出，尚未直接验证。
- **UNKNOWN**：未能确定。BLOCKED 不是 PASS，skip 不是结论。

本地环境（影响所有“实测”的可信度）：

- Linux 沙箱，4 核 / 约 15 GiB，与其他 agent 共用。取证期间 `/proc/loadavg` 为 2.4 至 16.3
  （另有他人的全量 pytest 在跑），因此“单独运行”只表示单个 pytest 进程，不表示空闲机器。
- 原生 actor 引擎使用预先构建的 `actors.abi3.so`（`git diff --stat 9932b2b63 HEAD -- crates` 为空）。
- 沙箱缺少：`pi` SDK、`ssh` / `ssh-agent` / `ssh-add`；到 TEST-NET 地址的连接被立即拒绝；
  环境里带有 `GH_TOKEN` 与 `GITHUB_TOKEN`。
- **Python 版本**：`uv sync --extra test` 在本机默认选中 CPython 3.14.6（仓库只声明 `requires-python >= 3.12`，
  没有 `.python-version`）；CI 固定 `--python 3.12`（日志显示 3.12.13）。本文先在 3.14.6 下跑了一轮，
  发现差异后把同一个 venv 重建为 3.12.3，其余实测都在 3.12.3 下完成。两轮结果分别标注。
- 本地命令模板（下文简称“模板”）：

  ```bash
  uv run --python 3.12 --with libcst==1.9.0 pytest <nodeid 或文件> -q --no-header -p no:cacheprovider --basetemp=/tmp/<独立目录>
  ```

CI 取证方法：读取 `dev` 最近提交的 combined status、当天 2311 条 job 记录（push 499 条、pull_request 1808 条、schedule 4 条），
并下载了全部 push 事件失败 shard 的日志（00:10 至 01:05 之间的 19 条因 job 编号未定位没有下载，见第 7 节）。

## 1. 汇总表

分类含义：

- **REAL**：CI 的 push 运行确定性失败（每次或在固定条件下每次）。
- **FLAKY-CI**：CI 的 push 运行间歇失败。
- **LOCAL-ENV**：只在本地环境失败（Python 版本或沙箱缺件），CI 为绿。
- **FLAKY-LOCAL**：只在本机负载下间歇失败，当天 CI push 失败日志中没有出现。
- **STALE-SCRIPT**：GUI 浏览器脚本与当前界面不一致，且不在任何 workflow 中。
- **OTHER-LINE**：别的工作线正在处理或 owner 已裁定不修，本清单不安排修复。

| 编号 | 项目 | 分类 | 原因（一句话） | 归属 | 修复方向 | 工作量 | KP |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | `test_snapshot.py::test_intranet_gate_on_a_real_repo_snapshot_reports_only_pending_hits` | REAL | #2821 新增文档的一行 clone 命令含内网 git 主机与邮箱形式字符串 | CI / 发布维护者 | 改文档那一行，用占位符 | S | KP-065 |
| R2 | `test_ci_dependency_caches.py::test_real_tree_denominator_and_family_witnesses` | REAL | #2821 新增 `native-conformance / plan` job，未登记其 runner 镜像上下文 | CI / 发布维护者 | 在测试支撑数据中补一条登记 | S | KP-066 |
| R3 | `gate-python / checks` 的 GUI 集成步骤 | REAL（特定 scope） | 该步骤启动真实 daemon，但 `checks` job 不构建原生引擎 | CI 维护者 | 步骤前构建并暂存引擎 | S–M | KP-067 |
| R4 | `gate-gui / gui-unit` 的 `verify:dev-browser-parity` | REAL（仅 CI） | 归档场景 `j5=m11` 组织页开发入口与正式入口高度差 1px | GUI | 先在 CI 条件下定位元素，再修 | M | KP-068 |
| R5 | `pytest-shard (3)` 触发 30 分钟上限 | REAL（约三分之一运行） | 该 shard 成功时平均 28.1 分钟，被取消时进度 99% | CI 维护者 | 刷新 shard 权重使其均衡 | M | KP-069 |
| R6 | `gate-python / unit`（聚合 job） | 结果 | 任一 shard 或 `checks` 失败即失败 | — | 随 R1–R5 及 F 项恢复 | — | — |
| F1 | `qq/test_token.py::test_token_stop_cancels_and_joins_background_task` | FLAKY-CI（7/30） | `stop()` 不等待被 `shield` 的取 token 任务 | QQ adapter | 产品：`stop()` 连带回收；测试：等确定事件 | S | KP-061 已列 |
| F2 | `test_external_promotion.py::test_initialize_reply_flushes_then_republishes_a_new_generation` | FLAKY-CI（5/30）；OTHER-LINE | 客户端读到应答后立即断言服务端计数，存在窗口 | request_http 宿主 | owner 已裁定不修；建议复核该裁定 | S | KP-077 |
| F3 | `test_profile_two_homes.py::test_t11_two_homes_two_isolated_daemons` | FLAKY-CI（4/7，自 #2821 起） | 30 秒内 `daemon.sock` 未出现；根因 UNKNOWN | daemon 生命周期 / 测试基础设施 | 先在超时处采集子进程输出 | M | KP-073 |
| F4 | request-http 集成用例 “did not publish server.json”、xdist worker 被杀 | FLAKY-CI | 60 秒内子进程无任何输出；worker 以信号 9 或 11 退出；根因 UNKNOWN | daemon / 测试基础设施 | 同 F3，并关联 KP-048 | M–L | KP-073 |
| F5 | `test_worker_relay.py`（两条不同用例）`KeyError: 'result'` | FLAKY-CI（2/30） | relay 返回了错误帧，断言方式丢掉了错误内容；根因 UNKNOWN | daemon workers | 先让断言输出完整应答 | S（取证） | KP-073 |
| F6 | `test_mcp_actor_by_name.py::test_owner_migration_relocated_session_refreshes_and_reads_as_same_session` | FLAKY-CI（1 次）+ 本地 2/10 失败 | 旧 owner 为 `op`，随机 agent id 含 `op` 子串时迁移中止 | daemon owner migration | 测试改用不可能出现在 id 字母表中的 owner；产品是否改判定需决定 | S | KP-072 |
| F7 | Lark `test_shutdown.py::test_shutdown_reports_blocked_process_stop_until_custody_releases` | FLAKY-CI（1/30） | `RuntimeError: Lark adapter runtime still has unresolved effect custody`；本地 5/5 通过 | Lark adapter | KP-061 已列为待归因 | UNKNOWN | KP-061 已列 |
| L1 | `test_state_persistence_authority.py::test_desired_and_journal_calls_share_one_owner_thread`、`test_launch_fencing.py::test_agent_home_lifecycle_io_does_not_hold_mailbox_or_registry_lock` | LOCAL-ENV（Python 3.14） | 断言比较 `threading.current_thread().name`；3.13 以后外部线程每次进入解释器得到新的 `Dummy-N` 名 | daemon / identity 测试 | 改比较 `threading.get_ident()`；另议是否固定本地 Python | S | KP-070 |
| L2 | `test_review_e1c.py::test_pi_unsupported_cache_expires_on_new_daemon_epoch`、`test_squire_prepare_http.py` 两例 | LOCAL-ENV（缺 pi SDK） | CI 安装固定版本 pi SDK，本机没有 | 测试基础设施 | 前者已明确报 BLOCKED 文案；后者应同样先检测 | S | KP-071 |
| L3 | `test_peer_reachability.py::test_tcp_probe_reports_timeout_for_test_net_blackhole` | LOCAL-ENV（沙箱网络） | 沙箱对 TEST-NET 地址立即返回 refused | 网络测试 | 先探测对照条件，不成立报 BLOCKED | S | KP-071 |
| L4 | `test_agent_git_ssh_capability_e2e.py` 三例 | LOCAL-ENV（缺 ssh 工具） | `FileNotFoundError: 'ssh-agent'` | identity 测试 | fixture 先检测并报 BLOCKED | S | KP-071 |
| L5 | `test_wheel_assets.py::test_missing_github_token_is_not_evaluable_not_failure` | LOCAL-ENV（环境变量泄漏） | 子进程继承了本机的 `GH_TOKEN` / `GITHUB_TOKEN` | CI / 发布测试 | `_run` 传入清理过的环境 | S | KP-071 |
| L6 | 负载下间歇失败的 Python 用例（第 4 节） | FLAKY-LOCAL | 以墙钟时间等待的断言在高负载下超时 | 各域 | 逐项改为等待事件，见第 4 节 | S–M / 项 | KP-061 部分 |
| B1 | `verify:j3/j5/j6/j7-browser`、`verify:linked-browser` | STALE-SCRIPT | 场景串缺少已登录输入 `j1=account-ready` | GUI 测试 | 一处函数补前缀（已实验验证） | S | KP-075 |
| B2 | `verify:first-run-browser`、`verify:safe-r1-browser`、`verify:j2-browser`、`verify:j10-browser` | STALE-SCRIPT | 断言已退役的常驻开发模块索引与常驻场景选择器 | GUI 测试 | 改经浮动开发工具取场景，或决定退役 | M | KP-025、KP-026、KP-075 |
| B3 | `verify:org-lifecycle-browser`、`verify:org-rework-browser` | OTHER-LINE | GUI 单启动线 PR 1e-4 将删除 | GUI 单启动线 | 不修 | — | KP-063 |
| B4 | `test:daemon-request-browser`（16 例中 10 例失败） | OTHER-LINE | owner 裁定不修，随旧 Node 宿主退场 | GUI 单启动线 | 不修；分组原因见 3.B4 | — | KP-076 |
| C1 | `gui-unit` 未构建原生引擎 | 已缓解；OTHER-LINE | #2824 让该用例改用 `reference-abi` 后端；PR 2b 补构建步骤 | GUI 单启动线 2b | 不修 | — | — |
| C2 | PR 分支合并提交上约 20 个 context 同时 `failure` | 非失败 | 全部是 `cancel-on-pr-close` 触发的取消，状态显示为 failure | — | 无需修；阅读状态时区分 | — | — |
| C3 | `native-conformance / native-linux-arm64` 排队 | 资源 | 单 runner 串行，每次 40–65 分钟，全天首尾相接 | CI 维护者 / owner | 等平台决定（设计稿：只调查） | — | KP-078 |
| C4 | `native-conformance / native-darwin-arm64`（PR 运行 9 次失败） | 基础设施 | 抽查 2 次均为 Rust 工具链清单下载超时 | CI 维护者 | 设计稿已有候选策略 | — | KP-078 |
| S1 | 定时 `e2e-macos-isolated` | REAL（定时，1 个样本） | E2E-010 升级连续性导入错误；E2E-024 转发 zid 重学 | 升级线 / 转发线 | 需 macOS runner 复现 | M | KP-074 |
| S2 | 定时 `e2e-macos-credentialed` | REAL（定时，1 个样本） | E2E-006 调用已不存在的 `start` 命令；E2E-004 等待超时 | E2E 脚本 / Lark 线 | 更新脚本命令；E2E-004 需真实环境 | S + UNKNOWN | KP-074 |

读表提示：

- 对“每个 PR 的 CI”影响最大的是 R1、R2、R4、R3、R5（顺序即第 5 节的优先级）。
  R1 同时让所有 docs-only PR 的 `gate-python / checks` 失败（该 job 的“documentation text gates”步骤直接运行这条用例）。
- 让“本地全量验证”含糊的是 L1–L6：其中 L1–L5 共 10 个用例在未对齐 CI 环境的本机上必红，与改动无关。

### 1.1 修复交接进度（2026-11-15）

- **R1 / KP-065：修复候选已进入 #2833。** `ci-native-conformance-routing.md` 的 clone 示例改用保留既有写法的占位主机；
  最终组合提交上 `tests/tooling/release/publish/github` 为 **161 passed**。未扩大 `PENDING_HITS` 或 allowlist。
- **R2 / KP-066：修复候选已进入 #2833。** 已登记 `native-conformance / plan` 的 `runner-image release-ci` 上下文；
  最终组合提交上 `tests/tooling/ci/acquisition/test_ci_dependency_caches.py` 为 **123 passed**。扫描器与断言未改。
- **R3 / KP-067：独立 PR #2836。** `checks` 的 GUI integration 步骤前复用原生引擎构建三步。
  无引擎时同一文件清单实测 **3 failed, 197 passed**，构建后 **200 passed**；workflow 回归测试 **3 passed**，
  Node `ci-scope` / `ci-plan` **162 passed**。真实 Forgejo 执行结果以该 PR 为准，须授权维护者合入。
- **R5 / KP-069：主修复在独立 PR #2841。** 含 `test_fd_hygiene_collection.py` 的 shard 拆成两波，
  只让探针波使用 `-n 1`，其余文件保持 `-n 2`；不改 timeout、matrix、lane 或测试集合。
  真实执行中探针 **8 passed**、普通文件 **2 passed**，两波 argv 与两份 junit 均核实无漏测/重复；
  合入最新 dev 后 `tests/tooling/pytest/` **110 passed**、Node **162 passed**。
  后续五次 dev push 的 shard 耗时才是稳定性验收。权重刷新仍是独立 BLOCKED 测量项；
  若未来刷新把探针移入 catch-all shard，须先扩展同样的波次约束，不能直接进入该旧路径。
- **L1 / KP-070：独立 PR #2837。** 两条用例改用 `threading.get_ident()`，未增加 `.python-version`。
  最终组合分支在 Python 3.12.16 与 3.14.4 的 native 后端上各 **27 passed**。
  其余 35 处 in-scope `current_thread()` 用法已逐一审查；23 个相关文件在两个解释器上各 **279 passed**，
  支持文件消费者各 **40 passed**，未发现需要批量替换的其他同类断言。Python 3.13.x 本身未单独实测（UNKNOWN）。

## 2. CI job 矩阵（`dev` 上的 push 运行）

### 2.1 最近 17 个 `dev` 合并提交

来源：CI 日志（combined status 与 job 记录，取于 19:40 前后）。`ok` 成功，`FAIL` 失败，`CANC` 被 runner 以超时取消，
`skip` 按变更范围跳过。`gui-unit` 的 `ok*` 表示 job 在 27 至 41 秒内成功，即范围未选中 GUI 步骤，不代表 GUI 门执行过。

| 提交（PR） | 时间 | cli-help-inventory | checks | tailcat-go | shard 1 | shard 2 | shard 3 | shard 4 | isolated | unit | gui-unit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2c1befe8e`（#2832） | 19:34 | ok | ok | ok | FAIL | FAIL | ok | FAIL | ok | FAIL | FAIL |
| `086070982`（#2831） | 19:07 | ok | FAIL | ok | skip | skip | skip | skip | skip | FAIL | FAIL |
| `c35e62b4c`（#2829） | 18:50 | ok | ok | skip | skip | skip | skip | skip | skip | ok | ok* |
| `f9044fd38`（#2828） | 18:45 | ok | ok | ok | FAIL | ok | ok | FAIL | ok | FAIL | FAIL |
| `f1a4caf86`（#2824） | 17:27 | ok | ok | ok | FAIL | FAIL | ok | FAIL | ok | FAIL | FAIL |
| `4306280a3`（#2663） | 16:33 | ok | ok | skip | skip | skip | skip | skip | skip | ok | ok* |
| `406c6e008`（#2800） | 16:19 | ok | ok | ok | FAIL | FAIL | CANC | FAIL | ok | FAIL | FAIL |
| `9932b2b63`（#2826） | 14:30 | ok | FAIL | ok | skip | skip | skip | skip | skip | FAIL | FAIL |
| `c4044b033`（#2825） | 14:17 | ok | ok | ok | FAIL | FAIL | ok | FAIL | ok | FAIL | FAIL |
| `a3e99c6b5`（#2823） | 13:35 | ok | ok | ok | FAIL | ok | ok | FAIL | ok | FAIL | FAIL |
| `b55e09b14`（#2821） | 12:18 | ok | ok | ok | FAIL | FAIL | ok | FAIL | ok | FAIL | FAIL |
| `7158ccad8`（#2726） | 11:56 | ok | ok | skip | FAIL | FAIL | ok | ok | ok | FAIL | ok* |
| `2e7c4e395`（#2816） | 11:36 | ok | ok | ok | ok | FAIL | CANC | ok | ok | FAIL | ok |
| `182c118ab`（#2820） | 10:49 | ok | ok | ok | skip | skip | skip | skip | skip | ok | ok |
| `9099d0411`（#2806） | 09:53 | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok |
| `0ec0ebc15`（#2814） | 09:25 | ok | ok | ok | skip | skip | skip | skip | skip | ok | ok |
| `0b4dd3931`（#2815） | 09:03 | ok | ok | ok | ok | ok | CANC | ok | ok | FAIL | ok |

结论：

1. 最后一次“全部 shard 都运行且 `unit` 为绿”的 push 是 `9099d0411`（09:53）。此后凡运行 shard 的提交 `unit` 全红。
2. 自 `b55e09b14`（#2821）起，shard 1 与 shard 4 每次必红，原因分别是 R1 与 R2（7 次完整运行中 7 次）。
3. `gui-unit` 自 `b55e09b14` 起每次执行 GUI 步骤都红：先是真实 daemon 回环用例缺引擎（至 `406c6e008`），
   #2824 缓解后露出 R4（`f1a4caf86`、`f9044fd38`、`086070982`、`2c1befe8e` 共 4 次，引用行相同）。
4. `checks` 只在“未选中 unit、选中 gui_integration”的范围下失败（`9932b2b63`、`086070982`，2 次中 2 次），即 R3。
5. `tailcat-go`、`cli-help-inventory`、`pytest-shard (isolated)`、`mock-model-harness-e2e / hosted` 在 push 上当天全绿
   （`hosted` 8 次、`isolated` 30 次）。`native-conformance` 不在 push 上运行。

### 2.2 各 shard 当天统计（00:10 至 18:47 的 30 次 push 运行）

来源：CI 日志（job 记录的开始与结束时间）。

| job | 运行 | 成功 | 失败 | 被取消 | 成功时平均耗时 | 成功时最长耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| pytest-shard (1) | 30 | 18 | 12 | 0 | 19.0 分钟 | 22.0 分钟 |
| pytest-shard (2) | 30 | 21 | 9 | 0 | 15.0 分钟 | 16.9 分钟 |
| pytest-shard (3) | 30 | 11 | 8 | 11 | 28.1 分钟 | 29.9 分钟 |
| pytest-shard (4) | 30 | 17 | 13 | 0 | 19.6 分钟 | 21.7 分钟 |
| pytest-shard (isolated) | 30 | 30 | 0 | 0 | 5.9 分钟 | 6.4 分钟 |

### 2.3 push 运行中出现过的失败用例

来源：CI 日志（已下载的近 50 份 push 失败日志；下表“次数”是这些日志中的出现次数）。

| 用例或现象 | 次数 | 首末出现 | 归入 |
| --- | --- | --- | --- |
| `test_snapshot.py::test_intranet_gate_…` | 7 | 12:20 – 19:34 | R1 |
| `test_ci_dependency_caches.py::test_real_tree_…` | 7 | 12:23 – 19:34 | R2 |
| runner 取消 shard 3（“exceeds the maximum run time”） | 11 | 02:09 – 16:22 | R5 |
| `qq/test_token.py::test_token_stop_…`（附带 fd 泄漏 teardown ERROR） | 7 | 02:18 – 14:22 | F1 |
| `test_external_promotion.py::test_initialize_reply_…` | 5 | 03:48 – 11:58 | F2 |
| `test_profile_two_homes.py::test_t11_…` | 4 | 13:37 – 17:30 | F3 |
| `test_daemon_request_architecture.py::test_real_repository_retired_gui_entries_do_not_reseed_the_ledger` | 4 | 03:11 – 04:10 | 已消失（04:10 之后的运行不再出现；修复提交 UNKNOWN） |
| xdist worker 崩溃（5 个不同用例各 1 次，信号 9 四次、信号 11 一次） | 5 | 02:58 – 19:34 | F4 |
| “request-http did not publish server.json” | 2 | 19:34（同一提交两个 shard） | F4 |
| `test_workspace_lifetime.py::…[success]`（一次 `TimeoutExpired`、一次 worker 崩溃） | 2 | 12:21、16:21 | F4 |
| `test_worker_relay.py`（`test_limit_and_unknown_payloads_leave_channel_usable`、`test_enforced_sender_auth_preserves_scoped_relay_identity_and_retry`） | 2 | 11:56、14:20 | F5 |
| `test_mcp_actor_by_name.py::test_owner_migration_…[name-form]` | 1 | 17:30 | F6 |
| Lark `test_shutdown.py::test_shutdown_reports_blocked_process_stop_until_custody_releases` | 1 | 04:03 | F7 |
| `checks`：`test_cli_adapter_add.py` 1 例、`test_cli_init.py` 2 例 | 2 次运行 | 14:37、19:08 | R3 |

汇总表 A 类中的其余本地红项（L1–L6）在上述任何 push 失败日志里都没有出现。

### 2.4 pull_request 运行与取消

来源：CI 日志。

- **取消不是失败**：`2142f4164` 与 `c521dc8d7` 是 PR 分支内的合并提交，推送后 15 秒内 PR 即被合入。
  两个提交各有 20 个 context 显示 `failure`，描述全部是 “Has been cancelled”，更新时间集中在同两秒内，
  同时 `ops-cancel-on-pr-close / cancel` 为 “Successful in 2s”。包括 `gate-pr-declarations / Skill-Impact`、
  `gate-pr-hygiene / pr-hygiene` 在内，没有一个是真实失败。job 记录里它们的状态是 `cancelled`，只有 commit status 把它显示为 failure。
- 当天 pull_request 运行中确属“继承自 dev”的失败只有三类：R1（docs-only PR 的 `checks`，例如 #2830 于 19:03）、
  `gui-unit` 的引擎缺失（至 16:40，已由 #2824 缓解）与 R4。
- 其余 PR 失败是 PR 自身原因：`pr-hygiene` 缺 `Submitted-by` 行或与 `dev` 冲突；`cli-help-inventory` 的台账增长
  （#2816 合入后未合并 `dev` 的分支会报 `base-router-prefix-growth`，合并 `dev` 即消失）；`test_provider_term_lint` 命中 PR 新增文本。
- `native-conformance / native-linux-arm64`：当天 22 次成功、4 次取消、0 次失败；从 00:41 到 19:05 一次接一次，
  单次 40 至 65 分钟，说明只有一个 runner 在串行消化队列。#2821 按变更范围路由后队列仍在。
- `native-conformance / native-darwin-arm64`：PR 上 9 次失败。抽查两次（job 155554 于 16:27、job 155614 于 16:48）：
  “curl: (28) Operation timed out after 60026 milliseconds”与
  “could not download file from … channel-rust-1.93.1.toml”。其余 7 次未读，原因 UNKNOWN。

### 2.5 定时运行

来源：CI 日志。当天 4 次定时运行：`release-nightly-promote / promote` 成功，`gate-gui / gui-unit`（11:17）成功，
`e2e-macos-isolated / isolated-macos`（02:17）失败，`e2e-macos-credentialed / controlled-real-env`（03:47）失败。详见 3.S。

## 3. 分项详情

### R1 发布快照内网门命中（KP-065）

- 现象。CI 日志（`pytest-shard (1)`，job 155711，17:29）：
  “intranet-gate HIT docs/reference/ci-native-conformance-routing.md:124 intranet-git-host”与同一行的 “staff-email”，
  断言 `assert hits == PENDING_HITS`（`PENDING_HITS` 为空集）。`406c6e008` 的日志里行号是 123，#2824 合入后变为 124。
- 实测：模板运行该 nodeid，3.14.6 下 2 次、3.12.3 下 2 次，4 次全部失败，命中行与 CI 相同。
- 原因（读代码）：`docs/reference/ci-native-conformance-routing.md` 第 124 行是一条 `git clone` 示例，
  地址写成了内网 git 主机的 SSH 形式；由 #2821（提交 `d6cc00850`）引入。
- 影响：每次完整 push 的 shard 1；所有 docs-only PR 的 `gate-python / checks`；GitHub 发布快照门同样会拒绝该行。
- 修复方向：把该行改为占位符（例如“用你的 `dev` 远端地址克隆”），不要把命中项加进 `PENDING_HITS` 或 allowlist。
  无需产品决定。
- 验证：`uv run --with libcst==1.9.0 pytest tests/tooling/release/publish/github -q`。

### R2 CI 镜像上下文清单缺一项（KP-066）

- 现象。CI 日志（`pytest-shard (4)`，job 155714，17:30）：“assert not ['unaccounted current image contexts']”。
- 实测：模板运行该 nodeid，两种 Python 下各 2 次，4 次全部失败（单次约 90 秒）。
  另用同一套扫描器直接求差集，唯一未登记项是
  `('.forgejo/workflows/native-conformance.yml', 'plan', 'runner-image release-ci')`。
- 原因（读代码）：#2821 在 `native-conformance.yml` 增加了 `plan` job（`runs-on: [release-ci]`），
  `tests/support/ci_download/release_images.py` 的 `INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS` 仍只有三项。
- 修复方向：在该集合补上面这一条。这是登记事实，不是放宽门禁。无需产品决定。
- 验证：`uv run --with libcst==1.9.0 pytest tests/tooling/ci/acquisition/test_ci_dependency_caches.py -q`。

### R3 `checks` 的 GUI 集成步骤没有原生引擎（KP-067）

- 现象。CI 日志（`gate-python / checks`，job 155390 于 14:37、job 155863 于 19:08）：
  “3 failed, 197 passed”，三例均为
  “ActorBackendUnavailableError: actor backend 'native' cannot use hyprial._core.actors: the compiled extension is not importable”：
  `test_cli_adapter_add.py::test_round_trips_through_daemon_config_loader`、
  `test_cli_init.py::test_init_returns_before_restore_completes`、`test_cli_init.py::test_init_latency_independent_of_fleet_size`。
- 原因（读代码）：`gate-python.yml` 的步骤 “Run Python GUI integration for audited GUI changes”
  （条件 `unit != 'true' && gui_integration == 'true'`）直接跑会启动 daemon 的用例；
  构建引擎的三个步骤只存在于 `pytest-shard` job。#2726 把默认后端切到 native 后，这个步骤在命中条件时必红。
- 与 2b 的关系（读代码）：分支 `feat/gui-python-session-host` 只改了 `gate-gui.yml`，没有改 `gate-python.yml`，所以 2b 不覆盖本项。
- 修复方向：在该步骤前复用 `pytest-shard` 的三步（确保 Rust、构建并暂存、验证可导入）。
  workflow 变更须由授权维护者合入。备选是给该步骤显式选 `reference-abi` 后端，但那会改变这组用例验证的后端，需要决定。
- 本地等价验证：临时移走 `src/hyprial/_core/actors.abi3.so` 后运行该步骤的文件清单应复现 3 例失败，放回后应通过（本文未执行，UNKNOWN）。

### R4 `gui-unit` 的开发入口与正式入口差 1px（KP-068）

- 现象。CI 日志（`gate-gui / gui-unit`，job 155706 于 17:27、155747 于 18:45、155859 于 19:07、155932 于 19:34，四次相同）：
  “✖ archived tools open the same mock input separately and preserve the active journey”，
  “scenario j5=m11 org page: layout.page differs — dev=[216,32,1080,452] production=[216,32,1080,453]”，
  位置 `gui/tests/dev-product-parity-browser.mjs:410`。同一运行里 vitest 全部通过（“Test Files 206 passed”）。
- 实测：`npm --prefix gui run build`、`build:dist-parity` 后 `npm --prefix gui run verify:dev-browser-parity`，1 次，通过（77 秒）。本地不复现。
- 起点（CI 日志）：`2e7c4e395`（11:36）的 `gui-unit` 完整执行并成功；`b55e09b14` 起 vitest 先失败，遮住了后面的 parity 步骤；
  最早一次露出是 #2824 分支在 14:28 的 PR 运行，其基线已含 `c4044b033`。
  因此回归落在 `2e7c4e395` 与 `c4044b033` 之间，其间唯一改动 GUI 与 `gate-gui.yml` 的是 #2825
  （含 `3e6811887`“guarantee a CJK font and fontconfig before the GUI tests”）。
- 原因：UNKNOWN。推断与 #2825 引入的字体保障步骤或其界面改动有关；本机同样装有文泉驿正黑却不复现，所以字体不是充分条件。
- 修复方向：在 CI 等价条件下输出两入口组织页各块的包围盒，找到相差 1px 的元素再修。
  不能通过加容差或删除该断言来变绿（那是削弱门禁）。
- 归属：GUI。工作量 M。需要能在 runner 镜像里复现，否则 BLOCKED。

### R5 `pytest-shard (3)` 触发 runner 的 30 分钟上限（KP-069）

- 现象。CI 日志（job 155526，16:22）：“The runner cancelled the job because it exceeds the maximum run time”，
  取消前 pytest 进度行为 “[ 99%]”。当天 30 次运行里 11 次被取消；成功的 11 次平均 28.1 分钟，其余 shard 为 15 至 20 分钟。
- 原因（读代码 + 推断）：shard 由 `scripts/ci/pytest_impact.py` 按 `scripts/ci/pytest-durations.json` 的权重装箱。
  shard 3 排在最前的是 `tests/tooling/isolation/test_fd_hygiene_collection.py`（本机单文件约 205 秒）。
  权重与实际耗时不符导致 shard 3 偏重。`gate-python.yml` 中“一个 shard 约为全量四分之一”的注释已不成立。
- 修复方向：用近期 `pytest-groups-pytest-shard-*` 产物刷新权重文件，使四个 shard 重新均衡；这不涉及 workflow。
  调整 `timeout-minutes` 属于 workflow 变更，且设计稿明确“预算调整不能代替根因修复”。
- 归属：CI 维护者。工作量 M。验证只能看后续 push 运行的 shard 耗时。

### F1 QQ token 停止与在途取 token 任务竞争

- 现象。CI 日志（`pytest-shard (2)`，job 155308，14:22）：`tests/daemon/adapters/qq/test_token.py:299`
  “assert all(task.done() for task in tasks)”为 False，随后 teardown 报 “test leaked 4 file descriptor(s)”
  （`store.sqlite` 及其 wal、shm，外加一个 socket）。当天 7 次。
- 实测：模板运行该 nodeid 5 次，5 次通过。本机不复现。
- 原因（读代码）：`src/hyprial/daemon/impl/adapters/qq/transport.py` 的 `stop()` 只取消并等待 `_background`；
  `_singleflight_fetch()` 另建名为 `qq-token-fetch-*` 的任务并用 `asyncio.shield` 保护。
  测试在 `start_background()` 后只 `sleep(0.01)` 就 `stop()`，若 HTTP 取 token 尚未返回，该任务仍在运行，断言失败，连接随之泄漏。
- 修复方向：产品侧 `stop()` 同时取消并等待 `_pending`；测试侧把 `sleep(0.01)` 换成等待一次取 token 完成的事件。
- 归属：QQ adapter。工作量 S。KP-061 已把它列为基线项，本文不另开条目。
- 验证：该 nodeid 连续运行，并在取 token 处理函数里人为延迟以稳定复现修复前的失败（先红后绿）。

### F2 external promotion 应答与计数的竞争（KP-077）

- 现象。CI 日志（job 154978，11:58）：`test_external_promotion.py:286` “assert host.post_reply_takes == 1”，实际为 0。当天 5 次。
- 实测：模板运行该 nodeid 共 10 次，1 次失败（同一断言）。
- 原因（读代码）：用例名即“应答先刷出，再重新发布”；客户端读到 HTTP 应答后立刻断言服务端线程里稍后才递增的计数。
- 决定：设计稿记录 owner 2026-11-14 裁定“不修 external promotion”。它仍在 `dev` 上并使 push 的 shard 间歇变红，
  而修复只是把即时断言改成等待，建议向 owner 复核该裁定。未获复核前不安排修复。

### F3、F4、F5 daemon 类子进程在预算内没有就绪（KP-073）

三者症状相近但没有证据证明同因，分别记录：

- F3。CI 日志（job 155714，17:30）：`test_profile_two_homes.py:221` “_wait_for(socket_a.exists)”在 30 秒后
  “condition did not become true”。#2821 之后的 7 次完整运行中 4 次。实测：模板运行 3 次全部通过（用例本体 7 至 10 秒）。
- F4。CI 日志（job 155940，19:34）：`tests/integration/request_http/conftest.py:355`
  “request-http did not publish server.json:”，冒号后的子进程日志为空，即 60 秒内没有任何输出。
  同一提交另一 shard（job 155938）记录 “CI_WORKER_EXIT … returncode: -9, signal: 9”。
  当天 worker 崩溃共 5 次，分布在 5 个不同用例上（`test_threads_cli_e2e.py`、`test_identity_http.py`、
  `test_adapter_http_live.py`、`test_workspace_lifetime.py`、`orgfs/mesh/device_rows/test_round6.py`，最后一例为信号 11）。
  实测：`test_threads_cli_e2e.py`、`test_identity_http.py`、`test_workspace_lifetime.py` 对应用例各 3 次，全部通过。
- F5。CI 日志（job 154977 于 11:56、job 155307 于 14:20）：`request(relay.socket_path, …)["result"]` 抛 `KeyError: 'result'`。
  实测：整个 `test_worker_relay.py` 3 次，每次 30 例全部通过。
- 原因：UNKNOWN。F3 与 F5 首次出现都在 #2726（默认后端改为 native）之后，但这只是时间相关。
  信号 11 的崩溃与 KP-048（OrgFS 定时同步处的原生崩溃）是否同源同样 UNKNOWN。
- 修复方向：先补取证，再谈修复。F3 的 `_start_daemon` 应在超时时输出子进程 stdout/stderr；
  F5 的断言应在缺 `result` 时打印完整应答帧；F4 需在 60 秒超时前抓取子进程线程栈。
  加长等待预算而不取证不算修复。
- 归属：daemon 生命周期与测试基础设施。工作量：取证 S，根因修复 M 至 L。

### F6 owner 迁移的残留检查按子串匹配（KP-072）

- 现象。CI 日志（job 155712，17:30）：“OwnerMigrationAborted: owner migration aborted before writing anything:
  2 value(s) still contain the old owner after the anchored rewrite…”，样例 “agents.agent_id = 'a.zhbq3gcfzpbwds4opzcgr5cs4y'”。
- 实测：模板运行该 nodeid（两个参数）共 10 次，2 次以同一异常失败（一次 `[id-form]`，样例 id 段含 `ops`；一次 `[name-form]`，样例 `a.tnhmpweopdd4hcdokwlbqx27sa`）。
- 原因（读代码）：用例里旧 owner 是 `OWNER_A = "op"`。`src/hyprial/daemon/impl/owner_migration/planning.py`
  用 `LIKE '%old%'` 取候选，再以 `old in rewritten` 判定残留。agent id 是随机的小写 base32 串，
  恰好含 `op` 时被判为“仍含旧 owner”，迁移按设计 fail closed。
- 性质：既是测试的概率性失败，也说明真实用户的短 owner 名可能因随机 id 而无法迁移（推断，未在真实数据上验证）。
- 修复方向：最小且安全的是让测试使用不可能出现在 base32 字母表里的 owner 字面量（含 `0`、`1`、`8`、`9` 或连字符）。
  是否把产品判定收紧到按地址段匹配，需要 daemon 维护者决定；源码注释明确写着不要为了通过而放宽重写。
- 归属：daemon owner migration。工作量 S（测试）或 M（产品）。

### L1 Python 3.13 以后线程名不稳定（KP-070）

- 实测。3.14.6 下两个 nodeid 各 5 次，全部失败：
  “assert (2 == 2 and 2 == 1)”，线程名为 `['Dummy-6', 'Dummy-7']`；另一例 “assert 3 == 1”，集合为 `{'Dummy-6', 'Dummy-7', 'Dummy-8'}`。
  同一 3.14.6 下设 `HYPRIAL_ACTOR_BACKEND=reference-abi` 各 2 次，全部通过。重建为 3.12.3 后默认后端各 2 次，全部通过。
- CI：两例都在当天的 shard 选择清单内且从未失败（CI 为 3.12.13）。
- 原因（实测 + 推断）：原生引擎从 Rust 线程回调 Python。断言比较 `threading.current_thread().name`。
  3.12 下同一 OS 线程的占位线程对象稳定；3.13 以后占位线程随线程状态销毁而清理，每次进入得到新的 `Dummy-N`。
  断言本意是“同一条 owner 线程”，用名字表达在新版本下不成立。
- 影响面（读代码）：`tests/` 下共有 38 处 `current_thread().name`，在 3.14 的本地全量里可能还有同类失败，本文只验证了这两例。
- 修复方向：断言改用 `threading.get_ident()`。另需决定本地是否固定 3.12（增加 `.python-version`，或只在环境说明里要求 `--python 3.12`）。
- 这两例最初被归因于“默认后端改为 native 之后出现”，该归因只对了一半：必要条件是 native 后端加 Python 3.13 以上。

### L2–L5 沙箱缺件导致的失败（KP-071）

| 用例 | 实测（两种 Python 各 2 次，均失败） | 缺少的条件 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| `test_review_e1c.py::test_pi_unsupported_cache_expires_on_new_daemon_epoch` | “Pi protocol rig unavailable; report BLOCKED, not PASS” | `pi` SDK（`which pi` 无结果） | 读代码：`_pi_attach_support.py` 的 `SDK_AVAILABLE`；CI 步骤 “Install the pinned pi SDK” | 文案已正确；保持失败并按 BLOCKED 上报 |
| `test_squire_prepare_http.py` 的 `test_start_over_http_returns_a_ready_receipt_and_projects_the_role_skill`、`test_lost_start_response_is_not_replayed` | “assert (500, 'dispatched') == (200, 'dispatched')”，`SQUIRE_PREPARATION_FAILED`（文件 7 例中 2 例失败） | 同上 | 读代码：文件头说明 start 用例走真实 Pi SDK bridge | 先检测 SDK，缺失时给出与上一行相同的 BLOCKED 文案 |
| `test_peer_reachability.py::test_tcp_probe_reports_timeout_for_test_net_blackhole` | “assert 'refused' == 'timeout'” | 到 TEST-NET 地址的连接应当超时 | 实测：bash `/dev/tcp` 连接同一地址端口立即 “Connection refused” | 用例先做对照探测，环境不满足时报 BLOCKED |
| `test_agent_git_ssh_capability_e2e.py`（t16、t17_commit_push 为 ERROR，t17_partial 为 FAILED） | “FileNotFoundError: … 'ssh-agent'” | `ssh-agent`、`ssh-add` | 实测：`which ssh ssh-agent ssh-add` 均无结果 | fixture 先检测并报 BLOCKED |
| `test_wheel_assets.py::test_missing_github_token_is_not_evaluable_not_failure` | “assert 'gh-token' in 'NOT EVALUABLE: the registry did not answer…'” | 环境中不应有 `GH_TOKEN` / `GITHUB_TOKEN` | 实测：`env -u GH_TOKEN -u GITHUB_TOKEN` 后 2 次通过；读代码：`attach_wheel_assets.py` 的 `--gh-token` 默认取这两个变量 | `_run` 传入去掉这两个变量的环境；这是测试隔离缺陷，应修 |

说明：仓库规则是“不用 skip 冒充结论”。这里建议的是在失败信息里明确写出 BLOCKED 与缺少的条件，不是把用例标成 skip 或 xfail。

### B1–B4 GUI 浏览器脚本

实测：每个脚本运行 1 次，一次一个，Node 24，Playwright 自带 Chromium。

| 脚本 | 结果 | 第一个失败点 | 原因类别 | 在 workflow 中 | 在 `npm run verify` 链中 |
| --- | --- | --- | --- | --- | --- |
| `test:browser` | 通过（97 秒） | — | — | 否 | 是 |
| `verify:dev-browser-parity` | 通过（77 秒）；CI 红，见 R4 | — | — | 是（`gate-gui`） | 是 |
| `verify:first-run-browser` | 失败（7 秒） | `first-run-browser-contract.mjs:431`，开发模块索引列表计数 “0 !== 1” | 断言已退役的常驻开发标记 | 否 | 是（链在此中止） |
| `verify:safe-r1-build` | 通过 | — | — | 否 | 是 |
| `verify:safe-r1-browser` | 失败（126 秒） | `first-run-browser-journey.mjs:23`，等待 `select[data-mock-scenario="j1"]` 120 秒超时 | 场景选择器已移入默认折叠的浮动开发工具 | 否 | 是 |
| `verify:j2-browser` | 失败 | `j2-browser-contract.mjs:46`，同一选择器 30 秒超时 | 同上 | 否 | 是 |
| `verify:j10-browser` | 失败（127 秒） | `first-run-browser-journey.mjs:23`，同一选择器 | 同上 | 否 | 是 |
| `verify:j3-browser` | 失败 | `shared-views-browser.mjs:40`，等待 `.chat-workspace` | 场景串缺已登录输入 | 否 | 是 |
| `verify:j5-browser` | 失败 | `shared-views-browser.mjs:60`，等待 `.org-workspace` | 同上 | 否 | 是 |
| `verify:j6-browser`、`verify:j7-browser` | 失败 | 同 j5 | 同上 | 否 | 是 |
| `verify:linked-browser` | 失败 | 同 j3 | 同上 | 否 | 是 |
| `verify:org-lifecycle-browser` | 失败 | `org-lifecycle-browser.mjs:61`，等待文本“还没有可用的组织。” | 1e-4 删除，不修 | 否 | 是 |
| `verify:org-rework-browser` | 失败 | `org-rework-browser.mjs:93`，等待 `[data-org-mode="ready"]` | 1e-4 删除，不修 | 否 | 是 |
| `verify:j4-browser` | 通过 | — | — | 否 | 是 |
| `verify:agents-second-browser` | 通过 | — | — | 否（`npm test` 明确排除它） | 否 |
| `test:daemon-request-browser` | 16 例中 6 通过、10 失败（824 秒） | 见 B4 | owner 裁定不修 | 否 | 否 |

`npm --prefix gui run verify` 的顺序是：`check:module-boundaries` → `typecheck` → `client:reference:check` →
`verify:dev-product-parity` → `build` → `test` → `test:browser` → `verify:dev-browser-parity` → `verify:production-copy` →
`verify:first-run-browser` → …，因此整条链停在 `verify:first-run-browser`，其后的脚本平时不会被执行到。
除 `verify:dev-browser-parity` 外，这些浏览器脚本都不在任何 workflow 里，属于无人值守的孤儿门。
`dev01-onboarding-browser` 已由另一条线（F6）修复，本文未重测。

**B1（KP-075）**。原因（读代码）：`gui/tests/support/shared-views-browser.mjs` 的 `scenarioFor()` 返回 `'j5=m11'`、`'default'` 等，
不含已登录输入；默认起点自 2026-11-13 起是登录/初始化空状态（KP-054），`dev-product-parity-browser.mjs` 已改用
`'j1=account-ready;j5=m11'`，这组脚本没有跟上。
实测（实验后已还原，工作树无残留）：把 `j5` 的返回值临时改为 `'j1=account-ready;j5=m11'`，`verify:j5-browser` 通过（1 次）；
把默认返回值临时改为 `'j1=account-ready'`，`verify:j3-browser` 通过（1 次）。j6、j7、linked 走同一函数，未单独实验（推断同因）。
修复方向：给 `scenarioFor()` 的各返回值补 `j1=account-ready;` 前缀。工作量 S。

**B2（KP-025、KP-026、KP-075）**。原因（读代码）：四个脚本要求页面上常驻 `.dev-module-index` 列表与
`[data-mock-scenario="j1"]` 选择器。常驻开发标记已按 2026-11-09 的等价改造退役（KP-026 的决定），
场景选择器现在只在浮动开发工具面板里（`dev-product-parity-browser.mjs` 先 `expandTools` 再取）。
修复方向二选一，需要 GUI 负责人决定：按浮动工具重写这四个脚本的取场景与“开发标记不进产品树”断言；
或确认其覆盖已由 `dev-product-parity-browser.mjs` 与 `tests/dev-overlay-interaction.mjs` 取代后从 `verify` 链移除并删除脚本。
后者是删除门禁，必须有明确决定和覆盖对照，不能由修复者自行选择。工作量 M。

**B3**。`org-lifecycle-browser.mjs` 与 `org-rework-browser.mjs` 由 GUI 单启动线 PR 1e-4 删除（KP-063 已写明原因与所需的 daemon 例子）。不修。

**B4（KP-076）**。设计稿记录 owner 裁定不修，随旧 Node 宿主退场。逐例结果（实测 1 次）：

| # | 用例 | 结果 | 第一个失败断言 | 组 |
| --- | --- | --- | --- | --- |
| 1 | actual CLI ordinary App: both drivers share build, page and paths | 失败 | `captureChatLayout`（:550）等待 `getByTestId('chat-layout')` | a |
| 2 | actual CLI Settings reads are wired and read-only for both drivers | 通过 | — | — |
| 3 | actual CLI dev entry uses the same App and consumes its private handoff | 失败 | 同 #1 | a |
| 4 | actual CLI no-open handoff is removed by launch expiry | 通过 | — | — |
| 5 | actual CLI SIGTERM reclaims owned children and private files | 通过 | — | — |
| 6 | isolated daemon local organization creation preserves nullable publication and lists its member | 失败 | :2441 `assert.equal(body.result.tailnetInitialized, false)`，实际为 true | b |
| 7 | actual CLI org.create reaches the shared client for both drivers without a fake success | 失败 | :797 等待文本“还没有可用的组织。” | c |
| 8 | actual CLI real-daemon G6 drops one IPC response without retry | 失败 | :1864 等待 `form.agent-form` 内标签“职责” | d |
| 9 | actual CLI real-daemon Connections page conditionally unbinds Lark | 失败 | :470 等待标题“飞书渠道绑定” | e |
| 10 | actual CLI real-daemon orgfs.write drops one response without retry | 通过 | — | — |
| 11 | actual CLI real-daemon folder restore keeps a partial unknown batch without retry | 通过 | — | — |
| 12 | actual CLI real-daemon org refresh failure keeps the write result and needs manual refresh | 通过 | — | — |
| 13 | actual CLI Contract Mock chat workspace reads, sends and recovers without resending | 失败 | :1188 等待文本“无法确认该操作是否已持久接受” | f |
| 14 | actual CLI real-daemon chat shows an empty-inbox directory and refuses unconfirmed send | 失败 | :1252 同 #8 | d |
| 15 | actual CLI real-daemon org surfaces: invite, join, machines and settings are wired | 失败 | :3010 “the isolated daemon must not initialize a tailnet”，实际为 true | b |
| 16 | actual CLI real-daemon agent link surface preflights list and blocks an unbound add | 失败 | :1727 同 #8 | d |

分组原因：

- a（#1、#3）：读代码。#2820 中的 `4387d0270` 让没有聊天数据源的正式组合把 `chat` 加入隐藏页面，导航项与 `chat-layout` 不再渲染。
- b（#6、#15）：真实 daemon 的 `org.create` 返回 `tailnetInitialized: true`。原因 UNKNOWN。
  Python 侧断言同一字段为 False 的三处用例（`test_org_network_service.py`、`test_org_create_isolated_without_sidecar.py`、
  `test_cli_org_create.py`）当天在 CI 没有失败，所以这可能是该脚本启动 daemon 的方式或本沙箱所致，也可能是只有这条路径能看到的行为变化。
  这是 B4 里唯一可能指向产品的一组，建议 daemon 组织/网络维护者做一次定点核对，即使脚本本身不修。
- c（#7）：读代码。#2820 中的 `e6840ab1f` 让组织入口随目录能力显示，空状态文案不再出现。
- d（#8、#14、#16）：读代码。`AgentForm` 的目录路径不再显示“职责”字段（该字段只保留在 fixture 路径）。
- e（#9）：读代码。`bf223bf79`“delete connection, model and provider sections that no page mounts”删除了该标题所在区块。
- f（#13）：推断。#2800 调整了聊天提交反馈的呈现位置；具体元素 UNKNOWN。

### S 定时 macOS E2E（KP-074）

只有当天各 1 个样本，且本文无法访问 macOS runner，以下全部来自 CI 日志，未复现。

- `e2e-macos-isolated`（job 153167，02:17）：
  “RESULT FAIL GATE isolated-macos … E2E-010=FAIL … E2E-024=FAIL …”，其余 16 项为 PASS。
  - E2E-010：“hyprial upgrade --json exited 1 … cannot import name 'harden_private_path' from 'hyprial.kernel'”，
    路径指向升级测试里旧版本的 site-packages。推断：升级过程中新代码在旧版本的包上执行了导入。
  - E2E-024：“forwarding-zid-relearn RED stale modes: swallow-close-delay-10-0”。原因 UNKNOWN。
- `e2e-macos-credentialed`（job 153752，03:47）：
  - E2E-006：“pi.launch start command failed (2): No such command 'start'.”，codex 同样；后续断言为 BLOCKED。
    读代码：`contract/e2e/run-resident-providers.py` 仍调用 `start` 子命令。脚本过期，移除该命令的提交 UNKNOWN。
  - E2E-004：“feishu-roundtrip verified=7/16 timed out waiting for evidence: harness.request.accepted”，其后 8 条断言为 BLOCKED。原因 UNKNOWN。

## 4. 间歇失败清单与稳定化建议

实测条件：Python 3.12.3；每次是一个独立的 pytest 进程；机器负载 7 至 16（见第 0 节）。
“CI 当天”指 2.3 节的 push 失败日志里是否出现。

| 用例（文件或 nodeid） | 本地通过 / 次数 | 失败断言（本地） | CI 当天 | 建议 |
| --- | --- | --- | --- | --- |
| `tests/shell/gui/lifecycle/test_gui_signals.py`（7 例） | 3 / 3 | — | 未出现 | owner 裁定不修，随旧宿主退场；不动 |
| `test_contract_python_resolver.py::test_key_mutations_are_killed`（8 个参数，单次 167 至 207 秒） | 3 / 3 | — | 未出现 | 该用例本身很重；全量并发下的失败应先看是否是子进程超时，再决定是否移入 isolated lane |
| `tests/shell/cli/agents/codex/test_settlement.py`（7 例） | 3 / 4 | `test_codex_interactive_carrier_recovers_final_after_process_rebuild`：“assert None == ('FINAL_OBSERVED', 'turn-rebuild', 'rebuild final', 1)” | 未出现 | 设计稿 A4c 已有方向：等待真实持久化返回，不以线程存活代表完成 |
| Lark `actor_domain/shutdown/test_shutdown.py`（16 例） | 5 / 5 | — | 1 次（F7） | KP-061 待归因项；需在失败现场取 custody 状态 |
| `transport/resilience/shutdown/test_shutdown.py`（10 例） | 3 / 3 | — | 未出现 | KP-010、KP-011 已登记“不修” |
| `authority/lifecycle/test_late_pid_refresh.py`（2 例） | 1 / 5 | “assert harness is not None and harness.running”，投影里 `running=False, pid=None` | 未出现 | 本清单里本地最不稳定的一项；测试应等待“harness 进入 running”的事件再断言 |
| `codex/test_codex_carrier_settlement.py`（16 例） | 2 / 3 | `test_status_query_budget_terminalises_without_further_queries`：“assert 0 == 3”（attempts） | 未出现 | 等待尝试计数落库的事件，而不是固定时长 |
| `dsh/test_dsh_doctor_probe.py`（16 例） | 3 / 5 | `[localhost-blocked]`：“assert [] == [('/api/host.describe', …)]”；`[localhost-success]`：“DshApiError: DSH HTTP transport was cancelled”，`timeout_seconds=0.5` | 未出现 | 0.5 秒的真实 HTTP 预算在负载下不够；要么注入时钟，要么给出有依据的更大预算 |
| Lark `test_configuration_and_status.py::test_exit_75_worker_is_restarted_with_supervisor_backoff` | 1 / 3 | “assert supervisor.reconcile() == 0”，实际为 1 | 未出现 | 用可注入的退避时钟驱动 reconcile |
| `test_pac_graph_authority.py::test_timed_out_graph_command_retires_capacity_and_keeps_bounded_receipt` | 5 / 5 | — | 未出现 | 本轮无失败证据，保留观察 |
| `test_worker_relay.py -k expired_binding_never_renews`（4 例） | 5 / 5 | — | 未出现（F5 是同文件的另两例） | 同上 |
| `tests/identity/devices/tailnet/test_retire_recovery.py`（19 例） | 5 / 5 | — | 未出现 | 同上 |
| `test_hosted_readiness_capture.py` | 3 / 3 | — | 未出现 | KP-015 已登记 |
| `tests/tooling/isolation/test_fd_hygiene_collection.py`（8 例，单次约 205 秒） | 3 / 3 | — | 未出现 | 重型用例，是 shard 3 偏重的来源之一（R5） |
| `tests/tooling/refactor/engine/test_pipeline.py::PipelineTests::test_post_completion_external_edit_refused_as_stage_drift` | 3 / 3 | — | 未出现 | 本轮无失败证据 |
| `test_external_promotion.py::test_initialize_reply_…` | 9 / 10 | “assert 0 == 1”（`post_reply_takes`） | 5 次（F2） | 见 F2 |
| `test_mcp_actor_by_name.py::test_owner_migration_…`（2 个参数） | 8 / 10 | `OwnerMigrationAborted`（两次失败的 id 分别含 `ops` 与 `op`） | 1 次（F6） | 见 F6 |
| `qq/test_token.py::test_token_stop_…` | 5 / 5 | — | 7 次（F1） | 见 F1 |
| `test_profile_two_homes.py::test_t11_…` | 3 / 3 | — | 4 次（F3） | 见 F3 |
| GUI vitest `tests/live/live-pages.test.tsx`（KP-053） | 3 / 3 | — | PR 运行 1 次（07:12） | KP-053 已登记 |
| GUI vitest `tests/client/transport.test.ts` | 3 / 3 | — | 未出现 | 本轮无失败证据 |

稳定化策略（不使用 skip、xfail，不删除用例，不加自动重试）：

1. **先分三类再动手**。有代码层面明确竞争的（F1、F2、F6）直接修竞争；
   只有负载相关超时而没有现场的（F3、F4、F5、late pid、lark exit 75）先补“超时即输出现场”的取证，再改为等待事件；
   本轮没有任何失败证据的保持不动，不为它们改代码。
2. **等待事件，不等待时间**。把 `sleep(固定值)` 后断言改成等待被测对象发布的完成事件或持久化返回；
   确需墙钟预算时，预算要有实测依据并写进文件头决策块，不能只是加大数字。
3. **隔离而不是隔离区**。对其他用例负载敏感、自身又启动子进程或嵌套 xdist 的重型用例
   （`test_key_mutations_are_killed`、`test_fd_hygiene_collection.py`、request-http 集成组）可以移到已有的 isolated lane，
   它仍然是必须通过的门，只是不与两条长寿命 worker 争资源。移动 lane 属于 CI 维护者的决定。
4. **每次重复测量都留痕**。调查阶段的重复运行是测量，不是重试策略；每次结果（包括通过的）都保留。
5. **KP-061 的五项“待归因”**中，本轮只有 Lark `test_shutdown` 一项在 CI 再次出现；其余四项本文没有新证据。

### 4.1 2026-11-16 新观察到的负载间歇失败（只记现象，不声称根因）

来源：CI 日志（`dev` push 运行）。下表所有条目的原因均为 UNKNOWN；它们只说明哪些用例在 CI 共享主机的负载窗口里失败过，
不构成“因负载而失败”的结论。第 4 节“稳定化策略”同样适用：先补取证，再谈修复，不加长预算、不重试、不 skip。

| 用例（nodeid） | 提交与 shard | 现象 | 本地 | 备注 |
| --- | --- | --- | --- | --- |
| `tests/daemon/harnesses/pi/headless/test_pi_headless_database_locked_e2e.py::test_lifecycle_start_survives_a_writer_past_the_busy_timeout` | `86c08aeed`，shard 2 | teardown ERROR，`subprocess.TimeoutExpired` | 未复现（本条未在本轮单独重跑） | 同一 push 的同一 shard 另有下一行失败 |
| `tests/shell/cli/installation/restart/post_install/test_workspace_lifetime.py::test_guard_workspace_is_removed_at_test_teardown[copy-failure]` | `86c08aeed`，shard 2 | `subprocess.TimeoutExpired` | 未复现（本条未在本轮单独重跑） | 同文件 `[success]` 参数在 F4 中已出现过 `TimeoutExpired` |
| `tests/integration/request_http/onboarding/test_gui_relaunch.py::test_gui_starts_the_owned_daemon_and_exit_leaves_it_running[driver-unset]` | `aafa045ef`，shard 4 | GUI 启动返回 `GUI_READY_TIMEOUT`：“request-http did not publish its private capability in time”（当时预算 15 秒） | 本机单独运行 2 passed（每个参数约 26 至 27 秒） | #2854 把该预算改为 60 秒并加了慢启动提示 |
| 同上 | `86c08aeed`，shard 4 | 第一次 GUI 启动返回 `{'code': 'GUI_READY_TIMEOUT', 'error': 'the source GUI did not become ready'}`；该参数 call 耗时 73.9 秒，同文件 `[driver-daemon]` 通过，64.3 秒 | 本机单独运行 2 passed | 该消息来自等待 host 子进程就绪行的 30 秒预算；失败报告没有任何子进程输出。随后的 `de4a50eb5` push 全部通过 |
| `tests/shell/cli/agents/codex/test_settlement.py::test_m4_codex_interactive_rebuild_regression` | `6622fcc18`，shard 1（job 159458） | `assert False`：一个 `threading.Event` 在 `wait(timeout=2.0)` 内未被置位 | 未在本轮重跑 | 不在该 push 合入的 #2827 的改动范围内；同一 shard 其余 8878 条通过 |
| `tests/daemon/application/adapters/test_daemon_adapter_start_nonblocking.py::test_adapter_start_does_not_wedge_the_daemon_dispatcher` | `6622fcc18`，shard 2（job 159459） | 失败（断言细节见该 job 日志） | 未在本轮重跑 | 不在 #2827 的改动范围内；同一 shard 其余 6566 条通过 |
| `tests/shell/gui/host/app/test_routes.py::test_the_session_response_carries_the_published_shape_and_the_pinned_catalog` | `32bc74fbf`，`gate-gui / gui-unit`（job 159497） | 期望 `Max-Age=60`，得到 `Max-Age=59` | 原因已知，不属于 UNKNOWN：用例跑在真实时钟上，cookie 携带的是剩余时间并向下取整到秒 | 已由 #2857 修复（改用假时钟，断言不变）；列在此处只为与当次红对应 |

补充记录（实测，沙箱 4 核，期间另有他人的 `pytest -n 3` 在跑）：host 子进程从启动到打印就绪行单独测量，空闲时 0.3 至 0.45 秒；
`taskset -c 1` 加 8 个 `setsid` 的 CPU 空转进程时 2.8 至 3.9 秒（12 次）。整条首启动在 4 个空转进程下用 66 秒（其中 host 约 2 秒）。
即 host 阶段在这些约束下只占整条启动的几个百分点，与 CI 观察到的 30 秒不成比例；这一点没有被解释，见 `test_gui_relaunch.py` 失败时现在附带的现场输出。

## 5. 建议的 PR 拆分

按优先级排列。每个 PR 只做一件事，base 为最新 `dev`。P0 是“让每个 PR 的 CI 能变绿”，P1 是“让 push 的 shard 不再间歇变红”，
P2 是“让本地全量验证结论清楚”。

| 顺序 | PR | 归属 | 内容 | 需要决定 | 验证 |
| --- | --- | --- | --- | --- | --- |
| P0-1 | `docs(ci): …` 去掉内网 clone 地址 | CI / 发布 | R1：改 `ci-native-conformance-routing.md` 第 124 行 | 否 | `uv run --with libcst==1.9.0 pytest tests/tooling/release/publish/github -q` |
| P0-2 | `test(ci): …` 登记 `plan` 的镜像上下文 | CI / 发布 | R2：`INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS` 补一条 | 否 | `uv run --with libcst==1.9.0 pytest tests/tooling/ci/acquisition/test_ci_dependency_caches.py -q` |
| P0-3 | `ci: …` `checks` 的 GUI 集成步骤前构建引擎 | CI 维护者（workflow 须授权合入） | R3 | 构建引擎还是显式 `reference-abi` | 一个只触发 gui_integration 范围的 PR 的 `checks` 为绿；`uv run --with libcst==1.9.0 pytest tests/tooling/ci -q` |
| P0-4 | `fix(gui): …` 归档场景组织页两入口等高 | GUI | R4：先在 CI 条件下定位，再修 | 否（但需 runner 镜像复现） | `npm --prefix gui run build && npm --prefix gui run build:dist-parity && npm --prefix gui run verify:dev-browser-parity`，并以 `gate-gui / gui-unit` 结果为准 |
| P0-5 | `ci: …` 刷新 shard 权重 | CI 维护者 | R5：更新 `scripts/ci/pytest-durations.json` | 是否同时调整 lane | 之后 5 次 push 的 shard 3 耗时与其余 shard 相当；`/opt/node22/bin/node --test --test-concurrency=2 scripts/ci/*.test.mjs` |
| P1-1 | `fix(adapters): …` QQ token 停止时回收在途取 token 任务 | QQ adapter | F1 | 否 | `uv run pytest tests/daemon/adapters/qq/test_token.py -q`，并提供先红后绿的延迟注入用例 |
| P1-2 | `test(daemon): …` owner 迁移用例改用不与随机 id 冲突的 owner | daemon | F6（测试部分） | 产品判定是否收紧另议 | `uv run pytest tests/daemon/channels/mcp/contracts/test_mcp_actor_by_name.py -q` 连续 20 次 |
| P1-3 | `test(daemon): …` 子进程就绪超时输出现场 | daemon / 测试基础设施 | F3、F4、F5 的取证部分，不改预算 | 否 | `uv run pytest tests/integration/profiles/homes tests/daemon/workers/test_worker_relay.py -q`；随后观察 CI |
| P1-4 | 依据 P1-3 的现场修根因 | daemon | F3、F4、F5 | 视根因 | 同一回归用例在修复前红、修复后绿 |
| P2-1 | `test: …` 线程身份断言改用线程 id | daemon / identity 测试 | L1 两例；评估其余 36 处 | 是否固定本地 Python 版本 | 在 3.12 与 3.14 两个解释器下各跑两例 |
| P2-2 | `test: …` 环境前置条件明确报 BLOCKED，清理继承的 token 变量 | 测试基础设施 | L2–L5 | 否 | L5：`uv run pytest tests/support/ci/attach/test_wheel_assets.py -q`（带与不带 `GH_TOKEN` 各一次）；其余在缺件环境确认失败信息含 BLOCKED 与缺少的条件 |
| P2-3 | `test(gui): …` 共享视图脚本使用已登录场景 | GUI 测试 | B1 | 否 | `npm --prefix gui run verify:j3-browser`，j5、j6、j7、linked 同理 |
| P2-4 | `test(gui): …` 四个首跑类脚本对齐浮动开发工具，或经决定后退役 | GUI | B2 | 是 | `npm --prefix gui run verify` 能越过 `verify:first-run-browser` |
| P2-5 | 逐项稳定第 4 节里有本地失败证据的用例 | 各域 | late pid、codex settlement、carrier settlement、dsh probe、lark exit 75 | 否 | 各 nodeid 在负载下连续 20 次 |
| 另行 | macOS 定时 E2E | 升级线、转发线、E2E 脚本 | S1、S2；E2E-006 的脚本更新可独立做 | 需 macOS runner | 定时 job 的 GATE 行 |

不在本清单内（别的线在做或 owner 已裁定）：

- `gui-unit` 构建原生引擎：GUI 单启动线 PR 2b（分支 `feat/gui-python-session-host`）。
- `org-lifecycle-browser`、`org-rework-browser` 与 reference 代理：PR 1e-4。
- `dev01-onboarding-browser`：已由 F6 修复。
- `test:daemon-request-browser`、GUI signals、external promotion：owner 2026-11-14 裁定不修。
  其中 external promotion 仍在使 push 变红，建议复核（F2）。

提交本文的 PR 自身会受 R1 影响：它是 docs-only PR，`gate-python / checks` 会因既有命中而失败，直到 P0-1 合入。

## 6. 不要做的事

- 不用 `skip`、`xfail`、自动重试、删除用例或把用例移出收集范围来变绿；不以“隔离区”为名删除或停用门禁。
- 不放宽断言、不加容差、不扩大 baseline、allowlist、`PENDING_HITS` 或任何只减不增的清单。
  R2 的修复是登记一个真实存在的新 job，这与放宽不同；R1 的修复是改文档，不是加豁免。
- 不单靠加长超时或 `timeout-minutes` 来消除间歇失败；预算调整必须有实测依据，并且不能代替根因修复。
- 不修改 `docs/notes/**`（WORM，只追加）。
- 不自行修改分支保护、required check、matrix 或 runner label；workflow 变更交授权维护者合入。
- 不恢复已退役的旧宿主、reference 路径或常驻开发标记来让旧脚本通过。
- Windows 专属问题按 owner 决定只登记、暂不处理；现有 Windows 检查（例如 `check_windows_portability.py`）保持不动，不删不改。
- 不把单次通过写成“已稳定”，不把 BLOCKED 写成 PASS。

## 7. 未能确定的事项

- R4 的根因元素，以及它与 #2825 的具体关系。本地不复现。
- F3、F4、F5 的根因，三者是否同因，是否与 native 后端有关，信号 11 的崩溃是否与 KP-048 同源。
- B4 组 b：真实 daemon `org.create` 返回 `tailnetInitialized: true` 是脚本或沙箱所致，还是产品行为变化。
- B4 组 f 的具体元素；B1 中 j6、j7、linked 未单独实验。
- R3 的本地等价复现未执行。
- `test_daemon_request_architecture.py::test_real_repository_retired_gui_entries_do_not_reseed_the_ledger`
  在 03:11 至 04:10 之间 4 次失败后消失，由哪个提交修复未查。
- 00:10 至 01:05 之间 19 条 push 失败记录（11 条 shard、2 条 `gui-unit`、其余为 `unit`）的日志未下载，
  它们早于本文关注的 15 个提交。
- `native-conformance / native-darwin-arm64` 9 次失败中 7 次未读日志。
- macOS 定时 E2E 四项只有 1 个样本，未复现；E2E-006 所用 `start` 命令何时移除未查。
- `tests/` 下其余 36 处 `current_thread().name` 在 Python 3.14 下是否失败未验证；本机 3.14 全量的失败集合未取得。
- 第 4 节各项在空闲机器上的通过率：取证期间机器始终有负载，没有空闲对照。
- `checks` 的 “documentation text gates” 步骤除 R1 外的四组文本门在 docs-only PR 上是否全绿，未单独核对。
