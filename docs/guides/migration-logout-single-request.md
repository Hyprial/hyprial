# 迁移说明：`hyprial logout` 改为一次 daemon `auth.logout`

适用：升级到包含 L1（2026-11-21，分支 `refactor/logout-via-auth-logout`）的版本。代码里没有任何迁移命令或自动改写；
下面需要人工（或 agent 照本文）处理的只有调用 `hyprial logout` 的脚本。

## 变化

- `hyprial logout` 不再在 CLI 进程里停止 daemon、证明旧进程退出、取身份锁或删除文件。它确保 daemon 在运行
  （没有时先启动）、按需确认，然后只发一次 `auth.logout`；删除登录产物由 daemon 完成，serving daemon 随后在
  同一进程回到 bootstrap（环境里仍有 owner 时退出）。
- `hyprial logout --json` 的 `daemon` 字段由 `{state, previousEpoch, ...}` 改为 `{stopRequested: bool}`；
  另外新增 `operationId`、`receiptId`。home 目录不存在时结果仍是 `{ok, status: "not-logged-in", home, removed: []}`，
  不含 `daemon`。
- 没有登录状态但 home 存在时，命令也会确保 daemon 并在非交互/`--json` 下要求 `--yes`（以前此情形不要求确认）。
- daemon 记录登出失败时命令以 `LOGOUT_FAILED` 退出 1，`data` 带回执；回复丢失时以 `SUBMIT_OUTCOME_UNKNOWN` 退出 1，不重试。
- `auth.logout` 的错误闭集删除了 daemon 从不抛出的 `DAEMON_STOP_UNPROVEN`；logout 回执的 `failure.reason`
  与 `SUBMIT_OUTCOME_UNKNOWN` 错误数据的 `reason` 枚举删除了 `stop-unproven`（daemon 从不写出该取值）。
- bootstrap daemon 上 `auth.logout` 的 `expectedEpoch` 改为与 `ping` 返回的 `epoch` 比较（原先比较调用方读不到的
  `bootstrap-<epoch>`，任何非空值都会被拒）。

## 需要做的

1. 解析 `hyprial logout --json` 的脚本：把读 `daemon.state`（`stopped` / `not-affected` 等）改为读
   `daemon.stopRequested`；不要再读 `daemon.previousEpoch`。
2. 依赖“logout 返回时旧 daemon 已退出”的脚本：改为自己观察 `hyprial daemon status --json`（daemon 通常仍以同一
   pid 运行在 `phase: bootstrap`），不要假设进程已退出。
3. 在已无登录的 home 上以 `--json` 或非交互方式调用 `hyprial logout` 的脚本：加上 `--yes`。
4. 匹配 `auth.logout` 错误码 `DAEMON_STOP_UNPROVEN` 或 reason `stop-unproven` 的客户端：删除该分支（daemon 从未返回）。

已知问题：设置了 `HYPRIAL_OWNER` 且已没有登录的 home 上重复登出答 `DAEMON_NOT_READY`，见
[KP-120](../reference/known-problems.md#kp-120-hyprial_owner-设置时已登出的-home-上-hyprial-logout-答-daemon_not_ready)。
