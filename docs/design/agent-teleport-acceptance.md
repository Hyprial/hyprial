# agent teleport：切片目标与检查标准

日期：2026-11-12。配套 [handoff](agent-teleport-handoff.md) 与 [协议细化](agent-teleport-protocol.md)（切片 4–6 的 payload、状态机、模块划分与细化检查标准，与本文冲突时以协议细化为准）；handoff 定产品决策与边界，本文定每个切片“做到什么算完成”。
基线：`dev` `c9b2f33d466e2e1a87ef83ce6bbbb486b58d3c6d`。本文是计划，不是实现或验收结论。

## 协作方式（Allen 2026-11-12）

- 规划与检查标准由协调者编写（本文）；每个切片先由测试编写者按本文产出先失败的测试，再由开发者实现到通过。
- 测试编写者不写产品实现；开发者不得为了通过而删改、跳过或放宽测试。测试本身有错时，开发者在交付说明里列出改动与理由，由协调者裁决。
- 每个切片一个小 PR 进 `dev`。全部完成后在 #2147 补 evidence 并合入。

## 所有切片共同的检查标准

每个切片交付前必须满足，并在 PR 正文逐项写 PASS / FAIL / BLOCKED / NOT_RUN：

1. 新增测试在实现前失败、实现后通过（保留失败输出摘要）。
2. 直接覆盖改动的焦点测试通过；受影响的既有测试目录通过。
3. `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `check_module_boundaries.py` 通过。
4. `uv run ruff check <改动文件>` 通过。
5. 新增行没有行内注释（`scripts/ci/check_inline_comments.py`）；决策写文件头决策块或 docstring。
6. 每个含代码目录不超过 7 个直接文件，每个代码文件不超过 1000 行。
7. kernel/identity/daemon/biz 不导入 typer、click、rich；CLI 输出只经 `shell/impl/cli/output`。
8. 改变已发布字段语义时同步 `contract/`；测试产生的 home、socket、端口与日常环境隔离。
9. 不新增 handoff §2、§3 排除的能力：订单、TTL、receipt、通用授权、增量或断点续传、SSH。

## 对 handoff 协议的一处修正

handoff §5 让 prepare 携带 manifest digest，但 home 的导出发生在 prepare 之后（先 prepare，再暂停 routine、quiesce、导出）。因此：

- prepare **不携带**内容 digest，只携带 operationId、源/目标 host、owner、agent、routine 名称与 runtime 要求。
- 导出产物是一个确定性的单一字节流（bundle 的归档）。每个 chunk 的头部携带 `streamSha256` 与 `total`；目标把 operation 绑定到 offset 0 的 chunk 声明的值，之后任何不一致都拒绝。
- receive 携带 `manifestDigest` 与 `streamSha256`，目标对落盘内容重新计算并核对两者。

## wire 约定（切片 2 冻结，后续切片消费）

- key：`hyprial/v1/agent-teleport/<targetNode>/<op>/<operationId>`，`op ∈ {prepare, chunk, receive, commit, abort}`。目标只为本节点的 `<targetNode>` 声明 query handler。
- 请求与应答都是一帧：4 字节大端头长度 + UTF-8 规范 JSON 头 + 原始字节体。只有 chunk 请求有非空体；不使用 base64。
- 头部公共字段：`v`（=1）、`op`、`operationId`。应答头：`ok`（bool）；失败时 `error.code`、`error.message`。业务失败用 `ok:false` 应答表达，不用传输层 reply error。
- chunk 头：`offset`、`total`、`size`、`streamSha256`、`chunkSha256`。chunk 体上限 `TELEPORT_CHUNK_BYTES = 768_000`。
- chunk 应答：`persisted`（已持久化的连续字节数，即下一个期望 offset）。
- 目标 staging：`<hyprial home>/state/teleport/staging/<operationId>/`，目录权限 0700，写入后 fsync 再应答。
- 顺序与幂等：`offset == persisted` 追加；`offset < persisted` 且该段内容哈希一致视为重复块，原样应答当前 `persisted`；`offset > persisted` 拒绝。
- 错误码（前缀 `TELEPORT_`）：`BAD_FRAME`、`WRONG_TARGET`、`UNKNOWN_OPERATION`、`OPERATION_CONFLICT`、`OFFSET_GAP`、`CHUNK_TOO_LARGE`、`CHUNK_DIGEST_MISMATCH`、`STREAM_MISMATCH`、`INCOMPLETE`、`DIGEST_MISMATCH`、`AGENT_EXISTS`、`ROUTINE_CONFLICT`、`RUNTIME_UNSUPPORTED`、`OWNER_MISMATCH`、`STRICT_RESUME_FAILED`、`IN_FLIGHT_TIMEOUT`、`SOURCE_CLEANUP_FAILED`、`UNRESOLVED`，以及 2026-11-12 追加的 `LANDING_FAILED`、`SOURCE_NOT_QUIESCED`（见 [协议细化](agent-teleport-protocol.md) §8）。

## 切片 1：routine 能力补齐

目标：teleport 需要的四项 routine 能力以内部接口提供，不改已发布方法的语义。

检查标准：

- 导出：给定 routine 名称可取回入库时的 YAML 原文与当前 enabled 状态；不存在时报明确错误。
- 按执行主体列出：给定 agent URI，返回执行主体为该 agent 的 routine（沿用现有 `actor or produces or owner` 判定）；只在 escalation/coordinator 中引用该 agent 的 routine 不返回。
- 以 paused 创建：通过内部接口用 YAML 创建 routine 后其状态为未启用，且创建过程不触发任何一次派发或自动 resume。
- 等待清零：对一组已暂停的 routine 等待 in-flight 归零；在超时内归零返回成功，超时返回仍在途的 routine 名称；不轮询忙等（有上限的间隔），不无界等待。
- 既有 `routine.list`、`routine.status`、`routine.add`、`routine.pause`、`routine.resume`、`routine.remove` 的行为与输出字段不变（既有测试原样通过）。

## 切片 2：wire、分块与目标 staging

目标：`daemon/impl/teleport/` 下提供 keyspace、帧编解码、源侧分块发送器和目标侧 staging 接收器，作为不含业务动作的库；`contract/agent-teleport/` 描述 wire。

检查标准：

- key 构造与解析往返一致；节点、operationId 含需转义字符时安全；`KeySpace` 提供对应方法。
- 帧编解码往返一致；截断、头长度越界、非 JSON 头、缺字段、`v` 不为 1 均返回 `TELEPORT_BAD_FRAME`。
- staging 接收器：顺序块落盘且应答 `persisted` 递增；重复块幂等；越位块 `OFFSET_GAP`；超长块 `CHUNK_TOO_LARGE`；块哈希不符 `CHUNK_DIGEST_MISMATCH`；`streamSha256` 或 `total` 与首块不一致 `STREAM_MISMATCH`；未 prepare 的 operation `UNKNOWN_OPERATION`。
- 完成校验：字节不足 `INCOMPLETE`；整体哈希不符 `DIGEST_MISMATCH`；通过后给出可读取的完整流路径。
- abort 删除该 operation 的 staging 目录；对未知 operation 的 abort 幂等成功。进程启动时清空残留 staging。
- 发送器：逐块等待应答后再发下一块；应答超时或无应答时按同一 offset 重试有限次，仍失败则报错；收到的 `persisted` 大于已发送位置时报协议错误。
- 用两个真实 `ZenohTransport`（loopback、动态端口）跑通一个多块流（至少 3 块，末块不满），并覆盖：错误目标节点收不到、重复块、中途中断后 abort。
- 此切片不注册到 daemon 启动路径，不出现在 request catalog、OpenAPI 或 CLI。

## 切片 3：抽出生命周期原语

目标：把 quiesce、strict resume 与 `RuntimeContextActivation` 变成 teleport 可直接调用的单元，现有 `transfer.*` 行为不变。

检查标准：

- 存在不依赖 `transfer.*` 请求参数形状的可调用单元：quiesce（同步 session ref 后停用并给出静止证明）、strict resume（就绪后 session ref 必须等于期望值，否则 `STRICT_RESUME_FAILED`）、resume 前置检查（transcript 存在）。
- `RuntimeContextActivation` 可从不属于 `p2/` 的模块导入；该模块不导入 `p2.journal`、`AdmissionStore`、`RemoteRunner`、`orchestrator`。
- `transfer.*` handler 改为调用这些单元；`tests/` 中既有 transfer 与 lifecycle 测试原样通过，不修改其断言。
- 新增测试直接调用抽出的单元，覆盖成功、session ref 不符、transcript 缺失三种结果。

## 切片 4：目标侧 service

目标：目标 daemon 响应五类操作，完成 home 落地与 strict resume（暂不含 routine）。

检查标准：

- prepare：agent 名称已存在 `AGENT_EXISTS`；runtime 不支持 `RUNTIME_UNSUPPORTED`；owner 不一致 `OWNER_MISMATCH`；成功返回凭据加密公钥并建立 staging；此阶段不启动任何 agent。
- receive：校验通过后落地 home、激活 runtime context、strict resume，返回目标 agent URI；任一步失败则清理已落地内容并返回对应错误，不留下运行中的 agent。
- commit、abort 幂等；abort 只在 receive 成功前有效，receive 成功后的 abort 被拒绝。
- handler 不在 Zenoh 回调线程上做长操作；每类操作有明确超时。
- query handler 随 daemon 启动注册（handoff §6 的决定：无认证、默认开放）。

## 切片 5：源侧 service 与公开入口

目标：`teleport.run` 与 `hyprial agent teleport AGENT --to HOST [--dry-run | --yes] [--json]` 可用，完成单 agent-home 的真实迁移。

检查标准：

- `teleport.run` 有真实 MethodSpec、params/result schema 与错误声明；OpenAPI 快照同步；CLI 只调用该方法，不含业务判断、rollback 或对 daemon 私有实现的导入（架构测试强制）。
- dry-run 与正式执行走同一方法；dry-run 只做 prepare 级检查，不停源、不传输。
- 顺序：prepare → quiesce → 导出 → chunk → receive → 源清理 → commit。
- prepare 失败：源完全不变。chunk 阶段失败：发送 abort，源 agent 恢复运行。
- receive 已发出但结果未知：源保持停止，返回 `TELEPORT_UNRESOLVED` 与人工检查信息（operationId、目标 host、agent、已发送字节数），不自动恢复源。
- 源清理失败：不发送 commit，返回 `TELEPORT_SOURCE_CLEANUP_FAILED`。
- 两个隔离的真实 daemon 加真实 Zenoh 跑通多块 agent home 迁移，全程无 SSH 调用。

## 切片 6：routine 全链路

检查标准：

- 源在触碰 home 之前暂停归属 routine 并等待清零；超时则恢复原 enabled 状态并返回 `IN_FLIGHT_TIMEOUT`，home 未被触碰。
- prepare 携带 routine 名称；目标名称冲突返回 `ROUTINE_CONFLICT`，源完全不变。
- 目标以 paused 重建 routine，YAML 中指向源 agent 的 URI 改写为目标 URI，其他 agent 的 URI 不变；不复制 `routines.sqlite3` 与在途、历史数据。
- commit 后只恢复原本 enabled 的 routine；原本 disabled 的保持 disabled。
- 源清理失败时目标 routine 保持 paused。chunk 阶段失败时源 routine 恢复原 enabled 状态。
- 真实双 daemon 场景包含一个 enabled 与一个 disabled routine。

## 切片 7：删除旧面

检查标准：

- CLI help 与 daemon 方法清单中不存在 `agent move`、`agent transfer`、`transfer.*`；不留 alias。
- P2 custody/journal/override、其 restore/shutdown 接线、SSH 总控与 `agent_home_chain.py` 删除；teleport 仍在用的 bundle、landing、envelope 保留并迁到不叫 `transfer/p2` 的位置或原位保留，由结构门与边界门决定。
- 无引用的测试、契约、文档、导出面同步清理；`.agents/skills/` 中描述旧命令的内容更新（`Skill-Impact: updated`）。
- 历史数据库表只停止使用，不删除用户数据。

## 切片 8：最终验收与 #2147

- 全量 `uv run --with libcst==1.9.0 pytest tests/`、`uv run ruff check .`、两个结构门、`./contract/run-all.sh --isolated-only`。
- handoff §10 的每一项逐条给出 PASS / FAIL / BLOCKED / NOT_RUN 与证据；对端认证与 GUI 端到端按既定结论分别记“未实现”与 BLOCKED。
- 证据写入 `docs/evidence/`，并在 #2147 补充后合入。
