# 迁移说明：cell authority 改为 uid（`user:u.<sub>`）

适用于：从 OC1a 之前的版本升级到包含 cell 模型 OC1a（cell authority 统一到 uid）的版本。读者是做升级的人，或替人做升级的 agent。

一句话：cell 文档里的 `authority` 从 `user:<用户名>` 改为 `user:u.<sub>`（Casdoor 登录的 `sub`，即 `settings.json` 的 `userId`）。**代码不做任何迁移**：不改写旧的 `state/cells.sqlite3`，不删除、不重写 OrgFS 上的旧 cell 文档，也没有迁移命令。下面每一步都由人或 agent 照做。

本说明只写当前版本的代码事实。下文的核对读 `<HYPRIAL_HOME>/settings.json`、daemon 日志或只读查询。标 UNKNOWN 的是没有在真实环境核实的。

## 1. 升级后代码做什么、不做什么

- cell 运行时只在有 uid 时组合。uid 的来源依次是 `HYPRIAL_USER_ID` 环境变量、`settings.json` 的 `userId`；只设了 `HYPRIAL_OWNER` 而没有 `HYPRIAL_USER_ID` 时视为没有 uid。没有 uid 时日志记 `cell.runtime_unavailable`（`reason=authority-uid-missing`）。
- uid 在 daemon 每个 serving 代启动时解析一次。同一代内重新登录写入的 `userId`，要到下一个 serving 代（`hyprial daemon restart`，或登出后再登录）才生效。
- `state/cells.sqlite3` 记录它所属的身份（authority、issuer、writer）。旧版本建的 cache 没有这条记录，于是**拒绝组合**：日志 `cell.runtime_unavailable`（`reason=cache-identity-mismatch`），文件原样不动。只有 `-wal`/`-shm` 而没有主文件、或文件读不出时，`reason=cache-unreadable`。
- 拒绝组合期间：daemon 照常运行；发给无头 agent 的消息保持 pending、不丢；`agent.attach` 返回的 `cell.state` 为 `deferred`；cell 参数取默认值。每 30 秒重试一次组合，每次只做只读检查。
- OrgFS 上本机不采纳的 cell 文档（旧写法 `user:<用户名>`、被他人改写、另一身份的副本）一律**只读**：原文与 revision 不变，本机在该 space 的这份写出被阻塞并记日志 `cell.document_blocked`，同一 cell 在其他 space 照常写出，本机索引与无头投递不受影响。文档被移走后，下一个 pass（约 5 秒）自动写出并记 `cell.document_unblocked`。
- 旧版本的自动行为已删除：换 owner 时的 cell 重铸（rehome）与 alias 表、写出时删除并重写不可采纳文档的 repair。

## 2. 升级前，每台设备（只读检查，不改任何东西）

1. **前置条件 A**：升级前检查 `<HYPRIAL_HOME>/settings.json` 是否含非空 `userId`（例如 `jq -r '.userId // empty' <HYPRIAL_HOME>/settings.json`）。缺少 `userId` 时，在登录态 home 上运行 `hyprial login` 补齐后再继续；使用 `HYPRIAL_OWNER` 托管部署时，在第 6 步成对设置 `HYPRIAL_USER_ID`。
2. **前置条件 B**：本机没有未写出的 cell 修改。只读查询：

   ```sh
   sqlite3 'file:<HYPRIAL_HOME>/state/cells.sqlite3?mode=ro' 'SELECT count(*) FROM cells WHERE dirty = 1'
   ```

   结果应为 0。旧版本 daemon 正常运行时会自己写出，这只是一个要满足的条件，不是要执行的步骤。不要为了满足它而依赖旧版本做任何自动迁移。

   **离线退路**（条件不满足，或 daemon 已停）：只读导出未写出的 cell，存进备份目录，第 9 步按导出内容重建参数：

   ```sh
   sqlite3 'file:<HYPRIAL_HOME>/state/cells.sqlite3?mode=ro' 'SELECT cell_id, document FROM cells WHERE dirty = 1' > <备份目录>/dirty-cells.txt
   ```

3. **记录手写过的 cell 参数**（例如 `delivery.bodyRetentionMs`、`threads.policy`）：`hyprial fs ls <spaceId> cells/agent` 列出文档，`hyprial fs read <spaceId> cells/agent/<c.id>.json` 读出，存进备份目录。

## 3. 升级

4. **同一 owner 的全部设备同批停机**。旧版本仍有 repair：混跑时，旧设备会把新设备写出的新格式文档删除并以旧写法重写，新设备随即再次阻塞。代码不处理这件事。
5. **人工离线备份并移走 cache 三文件**：代码从不自动删除、改写或迁移本地 cache。停机后由操作员手动把每台设备的 `<HYPRIAL_HOME>/state/cells.sqlite3`、`cells.sqlite3-wal`、`cells.sqlite3-shm`（以及可能遗留的 `cells.sqlite3.new`）备份并移出到备份目录，使原 `state/` 下不再保留旧 cache 文件（保留备份以便核对）。
6. 升级。用 `HYPRIAL_OWNER` 部署的（测试、托管部署），同时设置 `HYPRIAL_USER_ID`（取值同登录的 `sub`，例如 `ou_` 加 32 位十六进制，或一个小写 UUID）。
7. 启动，核对可观测证据确认 cell 运行时可以/已经正常组合：
   - 核对 `<HYPRIAL_HOME>/settings.json` 中存在有效 `userId`（或用 `HYPRIAL_OWNER` 部署时已设置 `HYPRIAL_USER_ID`）；
   - daemon 日志 `<HYPRIAL_HOME>/state/logs/daemon.jsonl` 中没有 `cell.runtime_unavailable`（只读查看，例如 `grep '"cell.runtime_unavailable"' <HYPRIAL_HOME>/state/logs/daemon.jsonl | tail -5`，或 `hyprial log --since 10m`）；
   - 在 cell 运行时被触发并创建新 cache 后，只读核对 `cache_meta` 中的三行身份记录（或按需要核对/清理冲突的 `state/cells.sqlite3` 见第 5 步）：

     ```sh
     sqlite3 'file:<HYPRIAL_HOME>/state/cells.sqlite3?mode=ro&immutable=1' 'SELECT key, value FROM cache_meta'
     ```

     应有 `authority`（`user:u.<你的 userId>`）、`issuer`、`writer`（`user:<owner>`）三行。

## 4. 日志报 `cell.runtime_unavailable` 时（按 `reason` 字段）

- `authority-uid-missing`：用 `HYPRIAL_OWNER` 部署的补设 `HYPRIAL_USER_ID` 后 `hyprial daemon restart`。登录态 home 缺 `userId` 的，按登录线迁移文档 `docs/guides/migration-account-binding.md` 处理后重新登录，再 `hyprial daemon restart`（uid 每个 serving 代解析一次）。该文档由登录线维护，以它为准。
- `cache-identity-mismatch`（`keys` 字段列出不符的键名）：`state/cells.sqlite3` 属于旧版本或另一身份，代码不会自动删除它；先停机，回到第 5 步人工离线备份并移走 cache 三个文件。
- `cache-unreadable`：daemon 正在写 cache 时可能短暂出现，稍后重试；持续出现时（例如孤立的 `-wal`/`-shm`、遗留的 `cells.sqlite3.new` 或损坏文件）先停机，回到第 5 步人工离线备份并移走这些文件。

## 5. 移走本人的旧格式 cell 文档

8. 代码不删除任何文档。对每个 org space：`hyprial fs ls <spaceId> cells/agent`；对日志 `cell.document_blocked` 点名的每个文档，或 `authority` 字段仍是本人旧写法 `user:<本人用户名>` 的文档：先 `hyprial fs read` 备份，再 `hyprial fs rm <spaceId> cells/agent/<c.id>.json`（软删除，可用 `hyprial fs trash restore` 恢复）。**只处理本人的文档，不碰别人的。** 下一个 pass 本机以新格式写出并记 `cell.document_unblocked`。
9. 把第 3 步（或离线退路）记下的参数在新格式下重建：用 `hyprial fs write` 写一份 revision 更高、作者为本人的新格式文档；有 `agent.cell.update` 的版本用它。

## 6. 失败恢复

10. 没有回滚工具。
    - `cell.document_blocked` 持续出现：回到第 8 步。
    - 同一文档反复被改回旧写法：同一 owner 有设备还在旧版本，把它升级。
    - `cache-identity-mismatch`：先停机，回到第 5 步人工离线备份并移走 cache 三文件。
    - `authority-uid-missing`：第 4 节第一条。

## 7. 已知限制

- 同一 uid 的两台设备 `settings.owner` 不同（例如改名后在新设备上首次登录）时，两台设备写出的文档在对方看来作者不符，双方都阻塞、互不覆盖、都记日志，该 uid 的 agent cell 跨设备不同步。这是登录线与 OrgFS 主体的问题，本版本不处理。
- UNKNOWN：2026-10-21 之前登录、之后从未重新登录的 home 是否缺 `userId`，只能由 operator 在隔离流程里盘点。

## 8. 退役 `department` cell（OC1b）

`department` cell 种类已由子 `org` 取代（J24），手写的 `department` 文档不再解码、不再生效、不被读取也不被删除（`PARAM_SOURCE_KINDS` 仅含 `agent`）。若曾手写过 `department` 文档：

1. **升级前（只读）**：在每个 org space 执行 `hyprial fs ls <spaceId> cells/department`，对每个文档执行 `hyprial fs read <spaceId> cells/department/<c.id>.json` 并存进备份目录，记下其中的参数与成员。
2. **升级后**：这些文档不再生效、不被读取、也不被删除；需要的参数在各 `agent` cell 上重建（有 `agent.cell.update` 之前手写 agent cell 文档的更高 revision，之后用 `agent.cell.update`）。
3. **清理（可选）**：是否用 `hyprial fs rm <spaceId> cells/department/<c.id>.json` 移走旧 `department` 文档由 operator 决定；留着不影响任何行为，代码不处理。
