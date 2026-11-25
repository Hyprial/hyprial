# 迁移说明：单 `HYPRIAL_HOME` 绑定单一账号（`settings.homeAccount`）与离线盘点

适用于：从引入 L3 之前的版本升级，或在已有 `HYPRIAL_HOME` 上遇到 `IDENTITY_CONFLICT`（`reason` 为 `home-bound-to-another-account` 或 `home-binding-missing`）的管理员或代为操作的 agent。

一句话：**代码不再包含任何自动 owner 改写或迁移命令**（`owner_migration`、`migrate_owner_if_needed`、`hyprial login --switch-account`、`--dry-run`、`--force` 均已删除）。每个 `HYPRIAL_HOME` 永久绑定到一个账号 `(issuer, userId)` 并记录在 `settings.json` 的 `homeAccount` 中；下文全部检查与处置均由人或 agent 离线执行。

> [!IMPORTANT]
> 本说明里的「离线」一律指先执行 `hyprial daemon stop`，再用 `hyprial daemon status` 确认已无运行中的 daemon 进程之后再检查或移动文件。切勿在 daemon 运行期间修改 `settings.json` 或 `state/` 下任何文件。

## 1. 升级前备份

1. 停掉目标 home 的 daemon 并确认停止：

   ```sh
   hyprial daemon stop
   hyprial daemon status
   ```

2. 对整个 `HYPRIAL_HOME` 目录做只读归档备份：

   ```sh
   tar -C <HYPRIAL_HOME 的父目录> -czf hyprial-home-<YYYYMMDD>.tgz <HYPRIAL_HOME 目录名>
   ```

3. 记录 `<HYPRIAL_HOME>/settings.json` 中当前账号字段，留作核对基线：
   - `owner`
   - `username`
   - `userId`
   - `identityIssuer`

## 2. 升级前盘点（只读 `sqlite3 -readonly`）

离线状态下，用只读模式检查 `<HYPRIAL_HOME>/state/` 各业务库中遗留的 owner / actor 地址段（只检查存在的文件；未创建过的文件跳过）：

- **`agents.sqlite3`**（`agents`, `pins`, `agent_harness_args_audit`, `agent_grant_journal`, `agent_secret_defaults`, `agent_secret_grants`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/agents.sqlite3" "
    SELECT DISTINCT owner FROM agents;
    SELECT DISTINCT uri FROM agents;
    SELECT DISTINCT actor, channel_uri FROM pins;
    SELECT DISTINCT actor FROM agent_harness_args_audit;
    SELECT DISTINCT actor FROM agent_grant_journal;
    SELECT * FROM agent_secret_defaults;
    SELECT DISTINCT actor FROM agent_secret_grants;
  "
  ```

- **`adapters.sqlite3`**（`identities`, `reply_routes`, `request_correlations`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/adapters.sqlite3" "
    SELECT DISTINCT uri FROM identities;
    SELECT * FROM reply_routes;
    SELECT * FROM request_correlations;
  "
  ```

- **`inbox.sqlite3`**（`outbox`, `sender_copies`, `inbox`, `custody`, `system_notices`, `dlq`, `delivery_status`, `progress_events`, `link_ledger`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/inbox.sqlite3" "
    SELECT DISTINCT sender, target FROM outbox;
    SELECT DISTINCT sender, target FROM sender_copies;
    SELECT DISTINCT recipient, sender FROM inbox;
    SELECT DISTINCT recipient, actor FROM custody;
    SELECT count(*) FROM system_notices;
    SELECT count(*) FROM dlq;
    SELECT count(*) FROM delivery_status;
    SELECT count(*) FROM progress_events;
    SELECT count(*) FROM link_ledger;
  "
  ```

- **`lifecycle-operations.sqlite3`**（`harnesses`, `interactive_sessions`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/lifecycle-operations.sqlite3" "
    SELECT * FROM harnesses;
    SELECT * FROM interactive_sessions;
  "
  ```

- **`routines.sqlite3`**（`routines`, `routine_address_migrations`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/routines.sqlite3" "
    SELECT DISTINCT actor FROM routines;
    SELECT actor, substr(yaml_text, 1, 200) FROM routines;
    SELECT * FROM routine_address_migrations;
  "
  ```

- **`workflows.sqlite3`**（`workflow_graphs`, `workflow_nodes`, `workflow_deliveries`, `workflow_worktrees`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/workflows.sqlite3" "
    SELECT DISTINCT created_by FROM workflow_graphs;
    SELECT DISTINCT owner FROM workflow_nodes;
    SELECT DISTINCT sender, recipient FROM workflow_deliveries;
    SELECT DISTINCT actor FROM workflow_worktrees;
  "
  ```

- **`pac-graph.sqlite3`**（`graphs`, `nodes`, `notifications`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/pac-graph.sqlite3" "
    SELECT DISTINCT created_by FROM graphs;
    SELECT DISTINCT owner FROM nodes;
    SELECT DISTINCT recipient, sender FROM notifications;
  "
  ```

- **`conversation-records.sqlite3`**（`records`, `segments`）：

  ```sh
  sqlite3 -readonly "<HYPRIAL_HOME>/state/conversation-records.sqlite3" "
    SELECT * FROM records LIMIT 50;
    SELECT * FROM segments LIMIT 50;
  "
  ```

- **结论判定**：如果上述各库及 `desired-state.json.v1`、`users.json` 中出现的所有 owner 都唯一且等于 `settings.json` 中的 `owner`，说明这是正常的单账号 home，直接升级即可（首次再登录或升级后登录会自动写入 `settings.homeAccount`）。

> [!WARNING]
> 升级时仍处于登录态的老 home，`settings.homeAccount` 要等**新版本上的第一次登录**才会写入。在此之前**不要执行 `hyprial logout`**：登出会删除 `owner`、`userId`、`identityIssuer`，而新版本不会在登出时自动补写绑定（不做自动改写），之后同一账号再登录会因目录里已有业务数据被拒（`IDENTITY_CONFLICT`，`reason=home-binding-missing`）。
>
> 正确顺序：升级后先在登录态执行一次 `hyprial login`（同一账号，daemon 驱动的 `auth.relogin`），确认 `settings.json` 已出现 `homeAccount`，之后才可以登出。已经先登出并被拒的，按第 5 节手工补写 `homeAccount` 后重新登录。见 [KP-121](../reference/known-problems.md#kp-121-升级后未重新登录就登出的老-home同一账号再登录被拒)。

## 3. 半迁移 home 的处理

如果第 2 步盘点发现多个不同的 owner 值，或库内 owner 与当前 `settings.owner` 不一致（常见原因：旧版本执行 `--switch-account` 中途中断），**不要手工修改 SQLite 中的 `owner` 列**，仓库也不提供任何修补工具：

1. **若有切换前备份**：离线停掉 daemon，恢复切换前完整备份，先用第 2 步复核单一 owner 一致，再按正常 home 升级。
2. **若无可用备份**：创建一个新的 `HYPRIAL_HOME` 目录并用新版本 `hyprial login` 登录；对照第 2 步盘点结果人工在新 home 上重建 agent、adapter、路由、routine，重新授予 secret grant 并重新登录各模型 provider。旧 home 保持只读归档仅供查阅，不再在其上运行新版本。

## 4. 换账号

**推荐做法**：不要复用旧 home，直接新建并指定一个新的 `HYPRIAL_HOME`（或新的 `hyprial profile`）登录目标账号。

若必须在同一个目录原地重置为新账号（仅当你确认放弃或已导出旧账号业务数据时），严格按顺序离线执行：

1. 按第 1 节完成全量备份。
2. 清理授权并登出、停机：
   - 对旧账号下的 secret grant 执行 `hyprial agent secret revoke ...`，对 provider 执行登出；
   - 执行 `hyprial logout --yes`；
   - 执行 `hyprial daemon stop` 并用 `hyprial daemon status` 确认无运行进程。
3. 把下列全部遗留判定计入项（含 SQLite 辅助文件）**移走**到备份目录（注意同时移走 `-wal` 与 `-shm`；不要只移主库）：
   - `<HYPRIAL_HOME>/state/agents.sqlite3`、`agents.sqlite3-wal`、`agents.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/adapters.sqlite3`、`adapters.sqlite3-wal`、`adapters.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/inbox.sqlite3`、`inbox.sqlite3-wal`、`inbox.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/lifecycle-operations.sqlite3`、`lifecycle-operations.sqlite3-wal`、`lifecycle-operations.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/routines.sqlite3`、`routines.sqlite3-wal`、`routines.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/workflows.sqlite3`、`workflows.sqlite3-wal`、`workflows.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/pac-graph.sqlite3`、`pac-graph.sqlite3-wal`、`pac-graph.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/conversation-records.sqlite3`、`conversation-records.sqlite3-wal`、`conversation-records.sqlite3-shm`
   - `<HYPRIAL_HOME>/state/cells.sqlite3`、`cells.sqlite3-wal`、`cells.sqlite3-shm`（详规则见第 8 节所引文档）
   - `<HYPRIAL_HOME>/state/desired-state.json.v1`
   - `<HYPRIAL_HOME>/state/users.json`
   - `<HYPRIAL_HOME>/state/identity/user-keys/` 下全部子目录/文件
   - `<HYPRIAL_HOME>/state/tailcat/device.json`、`state/tailcat/device-identity.json`、`state/tailcat/machine-binding.json`
4. 从 `<HYPRIAL_HOME>/settings.json` 删除 `homeAccount` 键（若已存在）。
5. 执行 `hyprial login`，以新账号建立全新的绑定与业务状态。

## 5. 被拒且 `reason=home-binding-missing`（含登出态老 home 与缺少 `userId` 的 S4 存量 home）

当旧 home 曾处于登出态、升级后在新版本上未重新登录就执行了 `hyprial logout`（KP-121）、或升级前为缺少 `userId` 的已登录存量 home，且目录中已存在上述计入业务数据时，新版本会拒绝写入并报 `IDENTITY_CONFLICT`（`data.reason = "home-binding-missing"`），错误 `data` 中包含本次认证拿到的 `loginUserId` 和 `issuer`。

> [!WARNING]
> 预防：升级前处于登录态的 home，升级后先以同一账号重新登录一次（写入 `homeAccount`），再执行第一次 `hyprial logout`。新版本不会在登出时替你补写绑定。

核对是否为同一账号后再决定：

1. 查看 `<HYPRIAL_HOME>/state/identity/user-keys/*/key.json` 中的 `casdoorSub` 与 `owner`，同时比对第 2 节盘点的库内 `owner`（若曾缺少 `userId`，请确保盘点到的唯一 owner 即该账号的既有本地名）。
2. **若 `casdoorSub` 与错误 `data.loginUserId` 相同**（确认为同一账号）：
   - 先确认 `hyprial daemon status` 为已停止（`hyprial daemon stop`）；
   - 在 `<HYPRIAL_HOME>/settings.json` 中补写 `homeAccount`（保持合法 JSON 与原有字段不变）：

     ```json
     "homeAccount": {
       "issuer": "<错误 data.issuer>",
       "userId": "<错误 data.loginUserId>",
       "owner": "<key.json 中的 owner>"
     }
     ```

   - 重新执行 `hyprial login` 完成登录或补齐 `userId`；若变更前 daemon 已在 serving，登录或补齐后执行 `hyprial daemon restart` 让 cell 运行时按新 uid 组合。
3. **若 `casdoorSub` 与错误 `data.loginUserId` 不相等**：说明本地遗留数据属于另一个账号，禁止补写 `homeAccount`，改用新 `HYPRIAL_HOME` 或按第 4 节清理旧数据后重新登录。

## 6. 同一 uid 改名预期

若同一 `(issuer, userId)` 账号在上游修改了用户名（`preferred_username`）：

- 再次登录（或 `hyprial login` 触发 `auth.relogin`）时，本机只更新 `settings.username` 和 GUI 中的展示用户名；
- `settings.owner`（以及 `settings.homeAccount.owner`）、已有 agent 地址中的 owner 段、OrgFS 作者和 org 成员名保持首次绑定时的旧值不变；
- 这是为了保住既有 agent、密钥授权与文档所有权的预期行为，不需要任何迁移或修复动作。

## 7. 脚本与自动化适配

- 删除调用中已移除的参数：`--force`、`--switch-account`、`--dry-run`、`--no-daemon`。
- `hyprial logout --json` 不再返回进程退出状态 `daemon.state`/`daemon.previousEpoch`，改读 `daemon.stopRequested`。
- 移除对已删除字段和事件/错误码的依赖：
  - `ping` 不再包含 `migration`；
  - daemon 启动不再记录 `identity.owner_migration.applied`；
  - 三个迁移错误码 `OWNER_MIGRATION_CUSTODY_CONFLICT`、`OWNER_MIGRATION_CUSTODY_UNREADABLE`、`OWNER_MIGRATION_HOSTED_CONFLICT` 已退役。
- `auth.relogin` / `auth.login.begin` 与 `hyprial login --json` 对账号冲突统一返回 `IDENTITY_CONFLICT`（`data.reason` 为 `home-bound-to-another-account` 或 `home-binding-missing`，`data.guide` 指向本文档）。

## 8. `state/cells.sqlite3*` 与 cell cache 互引

本地 `state/cells.sqlite3`、`cells.sqlite3-wal`、`cells.sqlite3-shm` 及其 `cache_meta` 身份校验、阻塞处理和 OrgFS 旧格式文档的离线重建规则，以 [迁移说明：cell authority 改为 uid（`user:u.<sub>`）](migration-cell-authority-uid.md) 为准，不在本文重复。
