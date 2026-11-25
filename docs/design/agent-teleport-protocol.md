# agent teleport：切片 4–6 的协议、状态机与模块划分

日期：2026-11-12。配套 [handoff](agent-teleport-handoff.md) 与 [切片检查标准](agent-teleport-acceptance.md)。
本文冻结切片 4（目标侧）、5（源侧）、6（routine）共同依赖的契约，使两侧可以并行开发。与检查标准文档冲突时，本文更具体的条目优先。本文是计划，不是实现或验收结论。

## 0. 范围决定

- `--to HOST` 的值直接作为目标 **node id**（`HYPRIAL_NODE_ID`，缺省为主机名）。仓库里没有 host→node 的解析器，首版不新建；目标是否存在由 prepare 是否有应答判定。
- 只迁移已有 agent home、headless、harness 属于 `claude`/`codex`/`pi` 的 agent。没有 agent home 的 worker 在源侧预检拒绝，提示先运行 `hyprial agent ensure-home`。
- agent 的 cwd 必须是其 home 内的 `workspace`（或未设置）。外部 cwd 的 agent 拒绝；旧链路单独 rsync 工作树的做法不保留。目标侧 cwd 一律取落地后 home 的 `workspace`。
- 带 pinned adapter 的 agent 拒绝，提示先解除 pin；首版不迁移 pin。
- 不迁移未读 inbox；源侧沿用现有“保留 inbox 行”的移除语义。
- 落地使用 `SignaturePolicy(allow_unsigned=True)`：不做 bundle 归属签名校验（与 handoff §6、KP-047 一致）。不运行 sandbox smoke 的否决逻辑以外的新检查。
- 凭据沿用现有 X25519 envelope：prepare 返回目标公钥，源用它封装后随 bundle 传输。

## 1. 流格式

导出的 bundle 目录打成一个**未压缩 tar** 作为唯一字节流：

- 只含普通文件；按相对路径的字节序排序；`uid=gid=0`、`uname=gname=""`、`mtime=0`；mode 取 bundle manifest 记录的 mode；PAX 格式。相同目录产生相同字节。
- 解包规则：只接受普通文件；拒绝绝对路径、含 `..` 的路径、符号链接、硬链接、设备、FIFO、重复条目；解出的总字节数不得超过流的 `total`；目录按需创建为 0700。任一违规返回 `TELEPORT_DIGEST_MISMATCH`，不留下半解的目录。
- 完整性分两层：`streamSha256` 覆盖 tar 字节；解包后用现有 `validate_bundle` 逐文件核对，并比较 manifest 的 `manifest_digest` 与 receive 携带的 `manifestDigest`。

## 2. 各操作的头部字段

公共字段见 wire 约定：请求 `v`、`op`、`operationId`；应答另有 `ok`，失败时 `error.code`、`error.message`。下表只列各操作新增字段。除 chunk 外帧体为空。

### prepare

| 请求字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `sourceNode` / `targetNode` | string | 是 | 源、目标 node id；`targetNode` 必须等于 key 中的节点 |
| `owner` | string | 是 | 源 daemon 的 owner（自报，一致性检查） |
| `agent` | string | 是 | agent 名称（actor key） |
| `sourceAgentUri` | string | 是 | 源 agent 的规范 URI，供 routine 改写 |
| `harness` | string | 是 | `claude` / `codex` / `pi` |
| `routines` | string[] | 是 | 将迁移的 routine 名称，可为空 |
| `dryRun` | boolean | 否 | 为 true 时只检查，不登记 operation、不建 staging |

应答字段：`targetNode`（string）、`recipientPublicKey`（base64 的 32 字节 X25519 公钥）、`dryRun`（boolean）。

检查顺序与错误码：节点不符 `WRONG_TARGET`；owner 不符 `OWNER_MISMATCH`；harness 不受支持或本机不可用 `RUNTIME_UNSUPPORTED`；agent 名称已存在 `AGENT_EXISTS`；任一 routine 名称已存在 `ROUTINE_CONFLICT`；operationId 已登记 `OPERATION_CONFLICT`。

### receive

| 请求字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `manifestDigest` | string | 是 | bundle manifest 的 `manifest_digest` |
| `streamSha256` | string | 是 | tar 流的 SHA-256（64 位小写十六进制） |
| `harness` | string | 是 | 与 prepare 相同 |
| `args` | string[] | 否 | 源 launch spec 的 harness 参数 |
| `sessionRef` | string \| null | 是 | 期望恢复的 session；null 表示无会话，跳过 strict resume 核对 |
| `strictTimeoutSeconds` | number | 否 | 就绪等待上限，缺省 60 |
| `routines` | object[] | 是 | 每项 `{name, yaml, enabled}`；切片 4 只接受空数组 |

应答字段：`agentUri`（string，目标 URI）、`sessionRef`（string \| null）、`routines`（每项 `{name, restoreEnabled}`）。

### commit / abort

- commit 请求无新增字段。应答 `routines`：每项 `{name, enabled, resumed}`；`resumed=false` 表示本应恢复但失败，整体仍为 `ok:true`。
- abort 请求可带 `reason`（string，仅记录日志）。应答无新增字段。

### 超时与重试（源侧）

| 操作 | 单次超时 | 次数 | 无应答时 |
| --- | --- | --- | --- |
| prepare | 10 s | 1 | `WRONG_TARGET`（“节点无应答”） |
| chunk | 15 s | 3（同一 offset） | `INCOMPLETE` |
| receive | `strictTimeoutSeconds + 60` s | 1 | `UNRESOLVED` |
| commit | 15 s | 2 | 结果仍为成功，附 commit 警告 |
| abort | 10 s | 1 | 忽略（尽力而为） |

routine 在途等待上限缺省 120 s。

## 3. 目标侧状态机

每个 operationId 的状态只在内存中；磁盘上只有 staging 目录和（receive 之后）已落地的 agent 与 routine。

```text
(无) --prepare--> prepared --首个chunk--> staging --receive成功--> received --commit--> committed
prepared / staging --abort 或 receive失败--> (无)   received / committed --abort--> 拒绝
```

- prepare：`dryRun` 不改变状态。重复 prepare 同一 operationId 返回 `OPERATION_CONFLICT`。
- chunk：按 wire 约定的顺序与幂等规则。
- receive 的步骤：完成校验 → 解包 → manifest 校验 → 落地 home（铸造新 incarnation，cwd 取 home 的 workspace）→ 激活 runtime context →（切片 6）以 paused 创建 routine → 启动并 strict resume。任一步失败按相反顺序撤销（停止、删除已建 routine、反激活、撤销落地），删除 staging，operation 回到（无），应答对应错误码。**`ok:false` 的 receive 应答保证目标没有留下运行中的 agent 或新建的 routine。**
- receive 的错误码：字节不足 `INCOMPLETE`；任何哈希、解包或 manifest 不符 `DIGEST_MISMATCH`；名称在 prepare 之后被占用 `AGENT_EXISTS` / `ROUTINE_CONFLICT`；strict resume 不成立 `STRICT_RESUME_FAILED`；落地或激活失败 `LANDING_FAILED`。
- 对处于 `received` 的 operation 重复 receive，原样返回首次的成功应答，不重复执行。
- commit：恢复 `restoreEnabled` 为 true 的 routine，删除 staging，进入 `committed`；重复 commit 返回同一应答。未知 operation 返回 `UNKNOWN_OPERATION`。
- abort：`prepared` / `staging` 下删除 staging 并遗忘；未知 operation 幂等成功；`received` / `committed` 下返回 `OPERATION_CONFLICT`，不撤销已启动的 agent。
- daemon 启动时清空 staging 根目录。目标在 `prepared` / `staging` 期间重启：后续 chunk 或 receive 得到 `UNKNOWN_OPERATION`，源按失败处理并恢复。目标在 `received` 后、commit 前重启：agent 已在目标运行、routine 保持 paused，commit 得到 `UNKNOWN_OPERATION`，由 operator 在目标手工 `routine resume`。
- receive 与 commit 的长操作在目标业务 service 的工作线程执行，不占用 transport 的 query 回调线程之外的资源超过各自超时；同一时刻每个目标只处理一个 receive。

## 4. 源侧步骤与失败处理

| # | 步骤 | 失败时 |
| --- | --- | --- |
| 0 | 本地预检：agent 存在且有 home、headless、harness 受支持、cwd 合规、无 pin、会话可恢复、目标不是本机；列出执行主体为该 agent 的 routine 及其 enabled 状态 | 直接报错，未发出任何请求。无 home、外部 cwd、pin：`RUNTIME_UNSUPPORTED`；目标是本机：`WRONG_TARGET` |
| 1 | prepare（`dryRun` 时到此为止并返回计划） | 报目标的错误码；源未改动 |
| 2 | 暂停原本 enabled 的归属 routine，等待在途清零（切片 6） | 恢复原 enabled 状态，发 abort，`IN_FLIGHT_TIMEOUT` |
| 3 | quiesce agent，要求静止证明成立 | 尽力重新启动源 agent，恢复 routine，发 abort，返回 `SOURCE_NOT_QUIESCED` |
| 4 | 封装凭据、导出 bundle、打成 tar | 同 3 |
| 5 | 逐块发送 | 同 3，错误码取发送器给出的码 |
| 6 | receive | 收到 `ok:false`：同 3（不必再发 abort）。无应答或应答无法解析：**不恢复源**，agent 保持停止、routine 保持暂停，返回 `UNRESOLVED` |
| 7 | 源清理：删除已迁移的 routine，移除源 agent 及其 home | 不发 commit，返回 `SOURCE_CLEANUP_FAILED`，结果带目标 URI；目标 routine 保持 paused |
| 8 | commit | 整体仍成功，结果 `commit.ok=false` 并列出目标上仍 paused 的 routine |

- 每个 agent 同时只允许一个 teleport（内存锁）；并发请求返回 `OPERATION_CONFLICT`。
- 导出与 tar 的临时目录在任何结果下都删除。
- `UNRESOLVED` 的结果数据：`operationId`、`targetNode`、`agent`、`sourceAgentUri`、`bytesSent`、`sourceState="stopped"`、`pausedRoutines`，以及人工检查提示（在目标查看该 agent 是否存在；存在则在源手工移除，不存在则在源手工启动并恢复 routine）。

### `teleport.run`

- params：`{"agent": string, "to": string, "dryRun"?: boolean}`，`additionalProperties: false`。
- result：`{"ok": true, "operationId", "dryRun", "agent", "from": {"node", "uri"}, "to": {"node", "uri"|null}, "bytes", "routines": [{"name", "enabled"}], "commit": {"ok", "pausedRoutines": string[]}}`。dry-run 时 `to.uri` 为 null、`bytes` 为 0、无 `commit`。
- errors：声明全部会从 `teleport.run` 浮出的 `TELEPORT_*` 码，另加 `INVALID_ARGUMENT` 与 agent 不存在时仓库既有的错误码。
- `mutates=True`，不可安全重试，`audience` 为本机 operator，`http_exposed` 按 GUI 约定声明；`confirmation` 与预览字段取 `MethodSpec` 已有的枚举值，dry-run 与正式执行是同一方法的同一 handler。
- `--yes` 只是 CLI 的交互确认；CLI 的 IPC 等待时间要覆盖 §2 各超时之和。

## 5. 模块划分

`src/hyprial/daemon/impl/teleport/` 顶层已有 7 个文件，新增代码全部进子包：

| 子包 | 归属 | 内容 |
| --- | --- | --- |
| `protocol/` | 切片 4a（先行） | prepare / receive / commit / abort 的请求与应答 dataclass 及编解码；非法字段 `BAD_FRAME` |
| `stream/` | 切片 4a（先行） | `pack_bundle(bundle_dir, dest) -> PackedStream(path, total, sha256)`、`unpack_stream(path, dest, *, total)` |
| `target/` | 切片 4b | `TeleportTargetService` 与 query handler 绑定 |
| `source/` | 切片 5 | `AgentTeleportService`、`TeleportLink`（基于 `TransportSession` 的五类请求客户端） |
| `wiring/` | 4b 与 5 各自的文件 | 把端口接到真实的 landing、activation、lifecycle primitives、routine port；daemon 启动时调用 `purge()` 并注册 handler |

4a 很小，先完成；之后 4b 与 5 并行，互不改对方子包。切片 6 在两侧各自的子包内补 routine 部分。

目标侧端口（`target/` 只依赖这些 Protocol，单测用 fake）：

```python
class TargetHost(Protocol):
    node_id: str
    owner: str
    def agent_exists(self, name: str) -> bool: ...
    def runtime_supported(self, harness: str) -> bool: ...
    def recipient_public_key(self) -> bytes: ...
    def land(self, bundle_dir: Path, *, name: str, harness: str) -> Landed: ...
    def undo_landing(self, landed: Landed) -> None: ...
    def activate(self, landed: Landed) -> None: ...
    def deactivate(self, landed: Landed) -> None: ...
    def start(self, landed: Landed, *, args: Sequence[str], session_ref: str | None, timeout: float) -> str | None: ...
    def stop(self, landed: Landed) -> None: ...

class TargetRoutines(Protocol):
    def existing(self, names: Sequence[str]) -> list[str]: ...
    def create_paused(self, *, yaml_text: str, source_uri: str, target_uri: str) -> str: ...
    def resume(self, name: str) -> None: ...
    def remove(self, name: str) -> None: ...
```

`Landed` 含 `name`、`uri`、`entity_token`、`home`、`cwd`、`harness`。`start` 内部完成启动与 strict resume，不成立时抛 `STRICT_RESUME_FAILED`。

源侧端口：

```python
class SourceHost(Protocol):
    node_id: str
    owner: str
    def describe(self, agent: str) -> SourceAgent: ...
    def require_resumable(self, agent: SourceAgent) -> None: ...
    def quiesce(self, agent: SourceAgent) -> bool: ...
    def restart(self, agent: SourceAgent) -> None: ...
    def export_stream(self, agent: SourceAgent, *, recipient_public_key: bytes, workdir: Path) -> ExportedStream: ...
    def remove(self, agent: SourceAgent) -> None: ...

class SourceRoutines(Protocol):
    def executed_by(self, agent_uri: str) -> list[RoutineRef]: ...
    def export(self, name: str) -> RoutineExport: ...
    def pause(self, name: str) -> None: ...
    def resume(self, name: str) -> None: ...
    def wait_drained(self, names: Sequence[str], timeout: float) -> list[str]: ...
    def remove(self, name: str) -> None: ...
```

`SourceAgent` 含 `name`、`uri`、`harness`、`headless`、`args`、`session_ref`、`cwd`、`home`（无 home 时为 None）、`pinned_adapters`。`ExportedStream` 含 `path`、`total`、`sha256`、`manifest_digest`。`quiesce` 返回静止证明是否成立。

## 6. 细化的检查标准

### 切片 4a

- `pack_bundle` 对同一目录两次打包字节相同；`unpack_stream` 还原出逐文件相同的内容与 mode。
- §1 的每条解包拒绝规则各有测试，拒绝后目标目录不存在。
- 四类消息的请求与应答编解码往返一致；缺字段、类型错、未知 `op` 均 `BAD_FRAME`。

### 切片 4b（目标侧，`routines` 只接受空数组）

- prepare 的六项检查按 §2 的顺序各有测试；dry-run 不建 staging、不登记 operation；成功应答含 32 字节公钥。
- receive 成功路径按 §3 的顺序调用端口，应答含目标 URI；重复 receive 不二次调用端口。
- receive 在每一步失败时：按相反顺序撤销、staging 被删除、operation 被遗忘、应答码符合 §3，且 `stop` 之后没有运行中的 agent。
- `sessionRef` 为 null 时不核对会话；非 null 且不符时 `STRICT_RESUME_FAILED`。
- commit、abort 的幂等与拒绝规则各有测试；同一目标同时只处理一个 receive。
- 用两个真实 `ZenohTransport` 和 fake 端口跑通 prepare → chunk → receive → commit。
- daemon 启动路径注册 handler 并清空 staging；更新切片 2 中“包外无导入”的测试，只放行 daemon 组合层。

### 切片 5（源侧，routine 列表为空）

- §4 步骤 0 的每项预检拒绝各有测试，且未发出任何请求。
- 用 fake 端口与 fake link 断言 §4 每一行的失败处理：是否重启源、是否发 abort、返回的错误码。
- receive 无应答与应答无法解析都得到 `UNRESOLVED`，源未被重启，结果数据字段齐全。
- 源清理失败不发 commit；commit 失败整体仍成功并带警告。
- dry-run 与正式执行进入同一 handler；dry-run 只调用预检与 prepare。
- `teleport.run` 的 MethodSpec、OpenAPI 快照、CLI `hyprial agent teleport` 及其 `--json` 输出；架构测试禁止 shell 与 GUI 导入 `daemon.impl.teleport`。
- 真实双 daemon 场景（`contract/e2e/_daemon_runner.py` 的 `IsolatedDaemon`，真实 Zenoh）：
  1. 多块 agent home 迁移成功，目标 strict resume，源 agent 与 home 已移除，全程无 SSH 进程。
  2. 目标已有同名 agent：prepare 失败，源完全不变。
  3. 发送途中目标 daemon 被停止：源 agent 恢复运行。
  4. dry-run：两侧均无变化。

### 切片 6（routine）

- 源：只选执行主体为该 agent 的 routine；原本 disabled 的不暂停也不恢复，但随迁且在目标保持 disabled。
- 目标：routine 以 paused 创建；YAML 中等于 `sourceAgentUri` 的 URI 改写为目标 URI，其他 URI 不变；receive 失败时已建 routine 全部删除。
- commit 只恢复 `restoreEnabled` 为 true 的；源清理失败时目标 routine 全部保持 paused。
- 真实双 daemon 场景：
  5. 一个 enabled、一个 disabled routine 随迁，commit 后状态与源一致，源 routine 已删除。
  6. 目标已有同名 routine：`ROUTINE_CONFLICT`，源完全不变。
  7. 在途任务未清零：`IN_FLIGHT_TIMEOUT`，源 routine 恢复原状态，home 未被触碰。

## 7. 已核对的代码事实（`dev` `c9b2f33d`）

- 现有 agent-home 链路的顺序是取目标公钥 → quiesce → 封装凭据并导出 → 传输 → receive → 源完成；receive 发出后失败只保持、不回滚：`daemon/impl/transfer/agent_home_chain.py:283-600`。
- 该链路把工作树作为第二个传输单元单独 rsync：`agent_home_chain.py:428-447`。
- `export_bundle` 的参数与“导出后复查源树未变”的约束：`transfer/archive/bundle/_export.py:41-74`；manifest 自校验字段 `manifest_digest`：`bundle/_base.py:62,195`。
- 落地入口 `land_bundle_into_registry` 强制要求 `SignaturePolicy`，并校验 owner 与同名冲突：`transfer/landing/registry.py:353-415`；`allow_unsigned` 存在，生产构造函数不会产生它：`landing/signature_gate.py:109-171`。
- agent home 的拓扑是 `state`、`config`、`secrets`、`workspace`：`landing/registry.py:82`。
- 现有 P2 目标只落地并激活，不启动 agent；启动与 strict resume 在 P0 的 receive 中，经 `LifecycleOperation.create` 后比对 session ref：`application/actors/transfer.py:617-664`，`transfer/p2/orchestration/receive.py:152-276`。
- 源侧移除用 `LifecycleOperation.remove`，保留 inbox 行：`application/actors/transfer.py:929-997`。
- 目标公钥由 `ensure_recipient_key(recipient_key_path(home))` 取得：`application/actors/transfer_p2.py:705-723`；envelope 相对路径 `secrets/transfer/envelope.json`：`transfer/archive/envelope.py:770`。
- node id 取 `HYPRIAL_NODE_ID` 或主机名：`kernel/impl/primitives/node_identity.py:13-38`。
- routine YAML 的 URI 改写已有实现可参照：`biz/impl/routine/storage/agent_uris.py:39-117`。

## 8. 未解决项

- **已裁决（协调者，2026-11-12）：新增两个错误码。** `TELEPORT_LANDING_FAILED`（目标落地或激活失败）与 `TELEPORT_SOURCE_NOT_QUIESCED`（源无法静止、封装凭据或导出失败；源已按步骤 3 恢复）。冻结错误码由 18 个增至 20 个，切片 4a 同步 `errors.py`、对应测试与 `contract/agent-teleport/README.md`。
- UNKNOWN：对一个刚由 `land_bundle_into_registry` 落地并激活的行执行 `LifecycleOperation.create` 再 strict resume，现有代码没有这样组合过（P0 启动、P2 落地各走各的）。需要切片 4b 在真实 daemon 上验证。
- UNKNOWN：目标 home 路径与源不同时，`claude` 与 `pi` 的会话文件定位是否仍成立（它们的会话路径可能与 cwd 相关）。现有双 daemon 证据使用的是协议替身，不能回答这一点。
- UNKNOWN：`LifecycleOperation.remove` 是否同时删除 agent home 目录；若不删，源清理要另调 home 销毁。
- UNKNOWN：`MethodSpec.confirmation` 与 `retry` 中适合“不可重试的破坏性操作”的具体枚举值，由切片 5 按现有声明选取。
- 真实 harness 二进制（`codex`、`claude`）在开发机上不存在；双 daemon 场景只能用仓库的协议替身，真实 vendor 二进制的迁移记 NOT_RUN。
