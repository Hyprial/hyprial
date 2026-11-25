# agent teleport wire 契约（切片 2）

冻结 agent teleport 的 key、帧、chunk 语义与错误码；后续切片只消费，不改这里已冻结的部分。
实现：`src/hyprial/daemon/impl/teleport/`；检查标准见
`docs/design/agent-teleport-acceptance.md`「wire 约定」。本契约只描述线上可观察行为。

## 范围与已知缺口

- 本文定义公共头、五个操作的头字段（`prepare`、`chunk`、`receive`、`commit`、`abort`）与 bundle
  流格式。目标侧（切片 4b）已处理全部五个 op，见下文「目标侧状态机」；routine 随 agent 迁移（切片 6），见下文「routine 迁移」。
- 本版没有对端认证：任何能在 Zenoh 上到达目标节点的对端都可发起请求。见
  `docs/reference/known-problems.md` KP-047（由独立 docs PR 登记）。
- wire 库本身不注册到 daemon 启动；源侧经切片 5 的 `teleport.run` 与 `hyprial agent teleport` 暴露（见文末「源侧」），目标侧 handler 由切片 4b 接入。
- daemon 启动时注册目标 handler（`DaemonApplication._start_teleport_target`，失败只记日志、不阻止启动）；
  不进 request catalog、OpenAPI 或 CLI。落地使用不签名策略（不做 bundle 归属校验，KP-047）。

## key

```
hyprial/v1/agent-teleport/<targetNode>/<op>/<operationId>
```

- `op ∈ {prepare, chunk, receive, commit, abort}`。
- `<targetNode>`、`<operationId>` 经 `KeySpace.encode_identity`：仅含 `[A-Za-z0-9._:-]` 时原样，
  否则为 `~` + 无填充 urlsafe base64(UTF-8)。因此 `* ? $ # /` 与空白不会出现在 key 中。
- 目标只为本节点声明 `hyprial/v1/agent-teleport/<localNode>/**`；发往其他节点的 key 无应答。
- 请求用 Zenoh query 携带 payload；应答必须以**具体被查询 key**（去掉 `?` 选择器参数）为 key。
- 解析器拒绝：非本前缀/版本、段数不为 6、未知 op、含通配符的 key。

## 帧

请求与应答都是一帧：

```
[4 字节大端无符号：头长度 N][N 字节 UTF-8 规范 JSON 头][原始字节体]
```

- 规范 JSON：键排序、分隔符 `,` `:` 无空白、`ensure_ascii=false`；同一头必得同一字节。
- 体是原始字节，不使用 base64；只有 `chunk` 请求体非空。
- 公共头字段：`v`（整数 1，布尔与 `1.0` 不算）、`op`、`operationId`（非空字符串）。
- 应答头另有 `ok`（布尔）；`ok:false` 时有 `error: {code, message}`（均为字符串）。
- 业务失败用 `ok:false` 应答表达，不用传输层 reply error。
- 任何畸形帧（长度前缀截断、头长度为 0 或越界、非 UTF-8/JSON、头非对象、缺公共字段、
  `v` 不为 1、`op` 未知、字段类型错误）都是 `TELEPORT_BAD_FRAME`。
- 请求头的 `op`、`operationId` 必须等于 key 中的值，否则 `TELEPORT_BAD_FRAME`。

## 各 op 的头字段

### chunk

请求头：`v op operationId offset total size streamSha256 chunkSha256`；体为该段字节。

| 字段 | 类型 | 含义 |
|---|---|---|
| `offset` | 非负整数 | 本块在流中的起始位置 |
| `total` | 非负整数 | 整个流的字节数 |
| `size` | 非负整数 | 必须等于体长度 |
| `streamSha256` | 64 位小写 hex | 整个流的 SHA-256 |
| `chunkSha256` | 64 位小写 hex | 本块体的 SHA-256 |

- 体上限 `TELEPORT_CHUNK_BYTES = 768000`。
- 应答头：`ok:true` 时带 `persisted`（已持久化的连续字节数，即下一个期望 offset）。

### 通用解码规则（prepare / receive / commit / abort）

- 体必须为空；`op` 必须等于该消息的 op；任何缺字段、类型错误都是 `TELEPORT_BAD_FRAME`。
- 解码忽略未知的额外头字段（向前兼容）。
- 标识字符串（节点、owner、agent、URI、harness、routine 名）必须非空；`sessionRef` 为非空字符串或 `null`。
- `ok:false` 应答解码为携带远端 `error.code` / `error.message` 的 `TeleportError`。

### prepare

请求头：`v op operationId sourceNode targetNode owner agent sourceAgentUri harness routines dryRun`。

| 字段 | 类型 |
|---|---|
| `sourceNode` `targetNode` `owner` `agent` `sourceAgentUri` `harness` | 非空字符串 |
| `routines` | 非空字符串数组（可为空数组） |
| `dryRun` | 布尔，缺省 `false` |

应答头：`v op operationId ok targetNode recipientPublicKey dryRun`；`recipientPublicKey` 是 32 字节
X25519 公钥的标准带填充 base64（解码后必须恰为 32 字节且为规范编码），`dryRun` 必填布尔。

### receive

请求头：`v op operationId manifestDigest streamSha256 harness args sessionRef strictTimeoutSeconds routines`。

| 字段 | 类型 |
|---|---|
| `manifestDigest` `streamSha256` | 64 位小写 hex |
| `harness` | 非空字符串 |
| `args` | 字符串数组，缺省 `[]` |
| `sessionRef` | 非空字符串或 `null`（必填） |
| `strictTimeoutSeconds` | 正的有限 JSON 数（非布尔），缺省 60 |
| `routines` | `{name 非空字符串, yaml 字符串, enabled 布尔}` 数组（必填） |

应答头：`v op operationId ok agentUri sessionRef routines`；`agentUri` 非空字符串，`sessionRef` 非空字符串或
`null`，`routines` 为 `{name, restoreEnabled 布尔}` 数组。

### commit

请求头只有公共字段。应答头：`v op operationId ok routines`，`routines` 为
`{name 非空字符串, enabled 布尔, resumed 布尔}` 数组。

### abort

请求头：公共字段，外加可选 `reason`（字符串；为空值时省略该字段，`null` 非法）。应答
`{v, op:"abort", operationId, ok:true}`。对未知或已清理的 operation 幂等成功。

## bundle 流格式

`chunk` 传输的流是一个未压缩的 PAX tar（`ustar\0` + `00` 魔数），由 bundle 目录确定性生成：

- 只含普通文件，无目录条目；成员名为相对 bundle 目录的 POSIX 路径，无 `./` 前缀，按名字 UTF-8
  字节序排列。
- uid、gid 为 0，uname、gname 为空，mtime 为 0。payload 文件的 mode 取自 bundle manifest；
  `manifest.json` 固定 `0600`。同一目录两次打包字节完全一致。
- 打包拒绝 bundle 目录中的符号链接、设备、fifo 等非普通文件，以及 manifest 未登记的文件；
  `dest` 已存在则拒绝（`O_EXCL`，不覆盖）。
- `total`（chunk 头）是 tar 文件字节数，`streamSha256` 是整个 tar 的 SHA-256。

### 解包规则

- 不使用 `tarfile.extractall`；逐成员先校验再写入。目标目录必须不存在，创建为 `0700`，所有中间目录
  `0700`。
- 文件以 `O_EXCL|O_NOFOLLOW` 创建，并用 `fchmod` 恢复 tar 头中的权限位（仅 `0777`，不含 setuid/setgid/
  sticky），不受 umask 影响。
- 拒绝（均为 `TELEPORT_DIGEST_MISMATCH`，并删除已创建的目标目录）：非普通文件成员（符号链接、
  硬链接、设备、fifo、目录条目）、绝对路径、含 `..`、`.`、空段或反斜杠的名字、重复成员、成员路径穿过
  已有文件、无成员、tar 损坏或截断，以及成员大小之和超过声明的 `total`（恰好等于允许）。

## chunk 与应答语义

- 目标 staging：`<daemon state dir>/teleport/staging/<operationId>/`（缺省 state dir 为 `<hyprial home>/state`），目录 0700；
  数据 fsync 之后才应答。operationId 只有经 `prepare` 才为目标所知；未 prepare 一律
  `TELEPORT_UNKNOWN_OPERATION`。进程启动时清空残留 staging。
- offset 0 的首块把 `streamSha256` 与 `total` 绑定到该 operation；之后任何块的这两项与
  绑定值不一致，或 `offset + size > total`，都是 `TELEPORT_STREAM_MISMATCH`，且不改动磁盘。
- 顺序：`offset == persisted` 追加；`offset > persisted` 为 `TELEPORT_OFFSET_GAP`；
  `offset < persisted` 时与已落盘字节逐字节比较，一致视为重复块并应答当前 `persisted`，
  不一致为 `TELEPORT_STREAM_MISMATCH`。部分重叠且重叠部分一致时只追加尾部。
- 被拒绝的块不推进 `persisted`。
- 完成校验（`finalize`，供后续切片的 receive 使用）：字节不足 `TELEPORT_INCOMPLETE`；
  对落盘字节重新计算 SHA-256，与首块绑定值及调用方给出值任一不符为 `TELEPORT_DIGEST_MISMATCH`。

## 源侧发送

- 逐块发送，收到 `ok:true` 后才发下一块；应答的 `persisted` 必须等于刚发块的结尾。
- 无应答（超时、无 queryable、传输层 reply error）按同一 offset 重试，每块最多
  `max_attempts`（默认 3）次 query；耗尽报 `TELEPORT_INCOMPLETE`。
- `ok:false` 不重试，原样抛出其 code。`persisted` 与块结尾不等按 `TELEPORT_STREAM_MISMATCH`
  立即停止。空流发送一个长度为 0 的块以绑定 `total` 与 `streamSha256`。

## 错误码

前缀 `TELEPORT_`，冻结集合：`BAD_FRAME`、`WRONG_TARGET`、`UNKNOWN_OPERATION`、
`OPERATION_CONFLICT`、`OFFSET_GAP`、`CHUNK_TOO_LARGE`、`CHUNK_DIGEST_MISMATCH`、
`STREAM_MISMATCH`、`INCOMPLETE`、`DIGEST_MISMATCH`、`AGENT_EXISTS`、`ROUTINE_CONFLICT`、
`RUNTIME_UNSUPPORTED`、`OWNER_MISMATCH`、`STRICT_RESUME_FAILED`、`IN_FLIGHT_TIMEOUT`、
`SOURCE_CLEANUP_FAILED`、`UNRESOLVED`、`LANDING_FAILED`、`SOURCE_NOT_QUIESCED`（共 20 个）。其中
`AGENT_EXISTS` 起的各码由后续切片使用；`LANDING_FAILED` 表示目标落地失败，`SOURCE_NOT_QUIESCED`
表示源侧未静默。

本切片中的来源：`WRONG_TARGET`（key 的节点不是本节点）、`BAD_FRAME`、`UNKNOWN_OPERATION`、
`OPERATION_CONFLICT`（重复 prepare）、`OFFSET_GAP`、`CHUNK_TOO_LARGE`、`CHUNK_DIGEST_MISMATCH`、
`STREAM_MISMATCH`、`INCOMPLETE`、`DIGEST_MISMATCH`。

## 源侧：`teleport.run`（切片 5）

实现：`src/hyprial/daemon/impl/teleport/source/`（服务与 `TeleportLink`）、
`src/hyprial/daemon/impl/request_catalog/teleport/`（声明与 handler）、
`src/hyprial/daemon/impl/teleport/wiring/source.py`（接到 daemon 的 registry、lifecycle、routine port）。

### 方法声明

- 名称 `teleport.run`，owner `daemon.teleport`，unary，`mutates=true`，`retry="never"`，
  `confirmation="none"`，audience `local-operator`，**`http_exposed=false`**：GUI 后端仍是 HTTP 501
  占位，HTTP 暴露、示例与生成的 GUI 客户端待 GUI 接线时再加。dry-run 与正式执行是同一方法。
- `--to` 的值直接是目标 node id（`HYPRIAL_NODE_ID`，未设置时为目标 home 的 `node-id` 文件）；没有 host → node 解析器。
- 已发布于 `contract/daemon-request/openapi.json` 的 `x-hyprial-methods`；不在 `paths` 中。

### params

`{"agent": string(非空), "to": string(非空), "dryRun"?: boolean}`，`additionalProperties:false`。

### result

```json
{"ok": true, "operationId": "…", "dryRun": false, "agent": "w1",
 "from": {"node": "node-a", "uri": "agent://…"}, "to": {"node": "node-b", "uri": "agent://…"},
 "bytes": 4096, "routines": [{"name": "…", "enabled": true}],
 "commit": {"ok": true, "pausedRoutines": []}}
```

- dry-run：`to.uri` 为 `null`，`bytes` 为 0，没有 `commit`。
- `commit.ok=false` 表示目标没有确认 commit，整体仍是成功；`pausedRoutines` 列出目标上仍处于 paused 的
  routine，需由 operator 在目标手工 `routine resume`。
- `routines` 列出随 agent 迁移的 routine 及其在步骤 0 时的 `enabled`（含已停用者），顺序与 `executed_by` 一致；dry-run 同样列出但不触碰任何 routine。详见「routine 迁移」。

### errors

声明全部 20 个 `TELEPORT_*` 码，另加 `INVALID_ARGUMENT`（参数不合法）与 `AGENT_NOT_FOUND`（源节点无此
agent）。只有两个码带 `data`（封闭对象）：

- `TELEPORT_UNRESOLVED`：`operationId`、`targetNode`、`agent`、`sourceAgentUri`、`bytesSent`、
  `sourceState`（恒为 `"stopped"`）、`pausedRoutines`、`manualCheck`（人工检查步骤文本）。
  含义：receive 无应答或应答不可读，目标可能已落地该 agent；源 agent 保持停止、不自动恢复。人工检查：
  在目标查看该 agent 是否存在；存在且会话完好则在源移除，不存在则在源手工 `agent start`。
- `TELEPORT_SOURCE_CLEANUP_FAILED`：`operationId`、`agent`、`targetNode`、`targetAgentUri`、
  `pausedRoutines`。含义：agent 已在目标运行，源侧停止的副本未能移除，未发 commit。

### 源侧步骤与失败处理

| # | 步骤 | 失败时 |
|---|---|---|
| 0 | 预检：agent 存在且有 home、headless、harness ∈ {claude, codex, pi}、无 pinned adapter、cwd 为未设置或恰为 `<home>/workspace`、会话可恢复、目标不是本机；列出执行主体为该 agent 的 routine | 未发出任何请求；无 home、非 headless、harness 不支持、pin、外部 cwd、会话不可恢复为 `RUNTIME_UNSUPPORTED`；目标是本机 `WRONG_TARGET`；无此 agent `AGENT_NOT_FOUND` |
| 1 | prepare（`dryRun` 到此为止） | 报目标的错误码，源未改动；无应答为 `WRONG_TARGET` |
| 2 | quiesce | 重启源 agent、发 abort、`SOURCE_NOT_QUIESCED` |
| 3 | 封装凭据、导出 bundle、打成 tar | 同上 |
| 4 | 逐块发送（每块 15 s ×3，同一 offset） | 重启源、发 abort、报发送器给出的码（耗尽为 `INCOMPLETE`） |
| 5 | receive（`strictTimeoutSeconds + 60` s ×1，从不重试） | `ok:false`：重启源、不发 abort、报其码。无应答或应答不可读或意外异常：**不重启源**，`UNRESOLVED` |
| 6 | 源清理：移除源 agent 及其 home | 不发 commit，`SOURCE_CLEANUP_FAILED` |
| 7 | commit（15 s ×2） | 整体仍成功，`commit.ok=false` |

- 重启失败不会替换原错误码，失败原因追加到消息里。
- 每个 agent 同时只允许一个 teleport（含 dry-run，进程内锁）；并发请求 `TELEPORT_OPERATION_CONFLICT`。
- 导出与 tar 的临时目录在任何结果下都删除；它们位于 `<state dir>/teleport-source/`，daemon 启动后首次使用
  时清空残留。
- 源 agent 的移除走 `LifecycleOperation.remove`；其最后一步 `registry.destroy` 会吊销 home receipt 并
  同步删除 home 目录，所以不另调 home 销毁；移除后会复查 agent 行与 home 目录确已消失，否则报
  `SOURCE_CLEANUP_FAILED`。

### CLI

`hyprial agent teleport AGENT --to NODE [--dry-run | --yes] [--json]` 恰好发一个 `teleport.run` 请求，
参数 `{"agent", "to", "dryRun"}`（`dryRun` 总是出现）。无 `--yes` 且非 dry-run：`--json` 或 stdin 非终端
时 `CONFIRMATION_REQUIRED`（不发请求）；交互时在 stderr 询问，拒绝为 `CANCELLED`。`--dry-run` 与
`--yes` 同时给出为 `INVALID_ARGUMENT`。IPC 等待 600 s，严格大于 daemon 对 `teleport.run` 的 IPC 期限
`TELEPORT_BUDGET.ipc_seconds`（560 s）。该期限由各步骤预算之和（535 s：prepare 10、routine 等待 120、
停止与导出预留 60、长流 120 加最后一块的重试 45、receive 120、commit 30、abort 10、余量 20）加 25 s
得到，常量集中在 `kernel/impl/contracts/channels/teleport_budget.py`。长流阶段由服务自身限 120 s：超过则在下一块前
停止并以 `TELEPORT_INCOMPLETE` 回滚，因此 daemon 总在 IPC 期限前给出 `TELEPORT_*` 结论。
## 目标侧状态机（切片 4b）

每个 operationId 的状态只在内存中：`prepared` → 首个 chunk 后 `staging` → receive 成功 `received` → commit
`committed`；`state` 为无表示未知或已遗忘。重启后 staging 被清空，所有进行中的 operation 都回到无。

| op | 规则 |
|---|---|
| prepare | 按序检查：key/头的 `targetNode` 不是本节点 `WRONG_TARGET`；owner 不符 `OWNER_MISMATCH`；harness 不在 `claude`/`codex`/`pi` 或本机不可用 `RUNTIME_UNSUPPORTED`；agent 名已存在 `AGENT_EXISTS`；routine 列表非空而本节点没有 routine 运行时 `RUNTIME_UNSUPPORTED`；任一 routine 名已存在 `ROUTINE_CONFLICT`；operationId 已登记 `OPERATION_CONFLICT`。`dryRun:true` 跑完全部检查，不建 staging、不登记。operationId 含路径字符 `BAD_FRAME`。 |
| chunk | 未知 operation `UNKNOWN_OPERATION`；`received`/`committed` 或 receive 进行中 `OPERATION_CONFLICT`（状态不变）；其余按上文 chunk 语义，首块被接受后进入 `staging`。 |
| receive | 未知 `UNKNOWN_OPERATION`；已 `received`/`committed` 原样返回首次成功应答、不再调用端口（幂等）；另一 receive 正在运行 `OPERATION_CONFLICT`（立即返回，不影响本 operation）；`harness` 与 prepare 不同、或 `routines` 的名字集合与 prepare 的不完全相同（缺、多、重复）`BAD_FRAME`（任何端口调用之前）。步骤：`existing` 复查（名字被占 `ROUTINE_CONFLICT`，尚未落地）→ `finalize` → 解包 → `validate_bundle` 与 `manifestDigest` 比对（任何不符 `DIGEST_MISMATCH`）→ 落地 → 激活 → 逐个 `create_paused`（按请求顺序）→ 启动并 strict resume。`create_paused` 的 `TeleportError` 保留其码，其他异常为 `LANDING_FAILED`。落地端口的 `TeleportError` 保留其码，其他异常为 `LANDING_FAILED`；启动的其他异常为 `STRICT_RESUME_FAILED`；非空 `sessionRef` 与启动结果不符 `STRICT_RESUME_FAILED`，`sessionRef` 为 `null` 时不核对、回显启动结果。任一步失败：按相反顺序撤销已进入的步骤（启动失败为 `stop`、按创建的相反顺序删除已创建的 routine、`deactivate`、`undo_landing`；激活失败为 `deactivate`、`undo_landing`；落地失败不撤销，落地自身原子），某撤销步骤抛错不影响其余步骤与应答码，随后删除 staging 并遗忘 operation。 |
| commit | 未知 `UNKNOWN_OPERATION`；`prepared`/`staging` `OPERATION_CONFLICT`（状态不变）；`received` 时删除 staging，按应答顺序恢复 `restoreEnabled` 为 true 的 routine（`resume` 抛错只记 `resumed:false`，其余照常恢复），进入 `committed`；重复 commit 返回同一应答且不再恢复。 |
| abort | 未知幂等成功；`prepared`/`staging` 删除 staging 并遗忘；`received`/`committed`（或 receive 进行中）`OPERATION_CONFLICT`，不撤销已启动的 agent。 |

- receive 与 commit 在目标 service 的工作线程执行，不占 query 回调线程。receive 最长等待
  `strictTimeoutSeconds + 60` 秒、commit 最长 15 秒；超时则不应答（源侧记 `UNRESOLVED`），工作线程继续，
  结果保留，之后重复的 receive/commit 返回已存的应答。
- 同一时刻每个目标只处理一个 receive。
- 非 teleport key 无应答；畸形帧、头与 key 的 `op`/`operationId` 不符为 `BAD_FRAME`，应答以具体 key 为 key。
- 落地：`home = <hyprial home>/agents/<name>`，`cwd = <home>/workspace`，铸造新 incarnation；owner 不符等
  落地拒绝为 `LANDING_FAILED`，同名被占为 `AGENT_EXISTS`。

## routine 迁移（切片 6）

以该 agent 为执行主体的 routine（源侧 `executed_by(agentUri)`，不论启用与否）随 agent 一起迁移。

- prepare 的 `routines` 携带全部迁移 routine 名；目标记住 `sourceAgentUri` 与名字。receive 的 `routines`
  是每个迁移 routine 一项 `{name, yaml, enabled}`：`yaml` 来自源 `export`，`enabled` 是步骤 0 时的启用状态
  （不是暂停之后的状态）。
- 目标用 `create_paused` 以 disabled 创建（owner 为 daemon owner 的 user URI）；创建时把 YAML 中等于
  `sourceAgentUri` 的 `actor`、`produces`、`policy.routes[*].escalate_to`、`on_task_timeout.escalate_to`
  改写为目标 agent URI，其他值与自由文本不动。receive 阶段不恢复任何 routine；应答的 `routines` 是
  `{name, restoreEnabled}`，`restoreEnabled` 即请求里的 `enabled`。
- 源侧步骤 2（prepare 之后、quiesce 之前）：按序暂停原本启用的 routine，对**全部**迁移 routine 调一次
  `wait_drained`（上限 `ROUTINE_DRAIN_TIMEOUT_SECONDS = 120` 秒，`drain_timeout_seconds` 可按服务覆盖），
  再 `export` 每一个。非空返回或抛错为 `TELEPORT_IN_FLIGHT_TIMEOUT`（消息列出仍忙的 routine）；`pause` 或
  `export` 失败为 `TELEPORT_SOURCE_NOT_QUIESCED`。这些失败恢复本次暂停的 routine、发 abort、不触碰 agent
  （无 quiesce、无 restart），`data` 为空。
- 之后的失败（原有码不变）：先重启源 agent，再恢复本次暂停的 routine；停用的 routine 不会被恢复；某个
  恢复失败不改变错误码，其名字追加到消息。`TELEPORT_UNRESOLVED`：不恢复也不删除任何 routine，
  `pausedRoutines` 列出本次暂停的 routine（按列出顺序）。
- 源清理（步骤 6）：先按列出顺序删除每个迁移 routine，再删除 agent；首个失败即停止（agent 不再删除），
  为 `TELEPORT_SOURCE_CLEANUP_FAILED`，不发 commit/abort，不重启，不恢复。其 `pausedRoutines` 是 receive
  应答中 `restoreEnabled` 为 true 的 routine，即目标上仍暂停的、在源侧原本启用的 routine。commit 无应答时，
  结果 `commit.pausedRoutines` 同样如此。
- `RoutineOutcome.enabled` 是目标打算恢复的状态（`restoreEnabled`），`resumed` 仅在确实恢复后为 true。结果
  `commit.pausedRoutines` 列出 `enabled` 为 true 而 `resumed` 为 false 的 routine；在源侧本来就停用、在目标保持
  暂停的 routine 不是警告。
- 手工恢复：在目标节点 `hyprial routine resume <name>`。
