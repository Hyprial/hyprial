# Handoff prompt — hyprial agent teleport

> 将本文件作为开发者的接手提示。目标是从 `dev` 以小片 PR 实现 `hyprial agent teleport`，不是继续完成旧 agent-move B01 清单。
>
> 当前状态：设计交接，不代表 teleport 已实现、已接线或已通过真实双机验收。
>
> 2026-11-12 修订（Allen 当日决定）：对端认证不再是前置门（§6）；开发改为从 `dev` 切小片 PR 快速合入，不再在 #2147 旧分支上清理（§1、§9、§11）。本文首版来自 #2147 head `2067a47eb`。

## 1. 仓库与当前基线

- Forgejo 仓库：`HyprialOS/hyprial`
- 开发基线：`dev`。2026-11-12 就绪度核对所用 head 为 `c9b2f33d466e2e1a87ef83ce6bbbb486b58d3c6d`。
- 历史 PR：<https://code.hyprial.com/HyprialOS/hyprial/pulls/2147>（head 分支 `feat/agent-move-b01-contract`，旧 agent-move B01 工位）。

#2147 的 `contract/agent-move/`、`src/hyprial/daemon/impl/move/`、`tests/daemon/move/` 与 `docs/evidence/` 过程材料**不在 `dev` 上**，也不带入 teleport。它们和 `dev` 上的 P2 custody 一样只是历史工作材料，不是 teleport 的需求或验收依据。不要继续补 B01、B02…B18，不要围绕旧 immutable plan、intent、TTL、receipt 或 OrgPolicy 设计新实现。

2026-11-12 核对时 #2147 分支落后 `dev` 616 个提交、有 3 个文件冲突，diff 中约三分之二是过程 evidence。因此 teleport 不从该分支切出，也不需要“清理旧 B01”的提交；该 PR 的去留由 Allen 决定。WORM `docs/notes/` 不篡改。

## 2. 已确认的产品决策（必须遵守）

1. **业务语义**：teleport 只是把 agent home 和执行主体绑定在该 agent 上的 routine 迁移到另一个 host，并在目标 runtime 重新启动。
2. **最终命名**：公开命令是 `hyprial agent teleport`。旧 `agent move` 和 `agent transfer` 全部退场，不保留 alias。
3. **唯一业务边界**：跨机编排在源 daemon。CLI 与 GUI 都只是 daemon request 的包装，不实现业务。
4. **传输通道**：源 daemon 直接通过 Zenoh 与目标 daemon 协作，不使用 SSH，也不提供 SSH 自动回退。
5. **首版复制语义**：接受全量复制，不做增量复制、断点续传或 rsync 优化。
6. **未知结果语义**：接受 receive 后结果未知时由 operator 人工检查，不为此建设自动跨机恢复账本。
7. **最小功能优先**：删除 move/transfer/P2/B01 的复杂控制面，不建设通用订单、通用授权、通用 receipt、跨机事务或第二套 inbox。

任何新方案如果重新引入上述复杂能力，应先停下来与 Allen 确认，不得以“兼容旧设计”为由恢复。

## 3. 公开面与 ownership

### 公开命令

```text
hyprial agent teleport AGENT --to HOST [--dry-run | --yes] [--json]
```

首版只支持已有 agent home 的注册 agent。旧 non-agent-home worker 先走现有 `agent ensure-home`，不在 teleport 内保留第二条 P0 文件/session 搬运路径。

不公开以下参数或动作：

- `--remote-hyprial`、container、image、外部 cwd 映射；
- request/invite/confirm/start/cancel/retry/list/status；
- moveId、TTL、idempotency-key 业务订单；
- SSH 用户、目标 socket 或 shell 命令。

`--dryRun` 与正式执行都调用同一个 daemon request。`--yes` 仅是 CLI/GUI 的确认交互，不是授权证明。

### daemon request

只新增一个公开业务方法：

```text
teleport.run
```

- 在 request catalog / OpenAPI 中声明真实 MethodSpec、params/result schema 和错误。
- CLI 与 GUI 调用同一方法、同一 handler、同一结果与错误语义。
- 不注册返回 `not implemented` 的占位方法；真实 handler 接通后再公开。
- Zenoh 内部的 prepare/chunk/receive/commit/abort 不是新的公开 CLI/GUI 业务方法。

### 分层

```text
CLI / GUI
  → teleport.run
  → 源 daemon 的 AgentTeleportService
  → 现有 TransportSession / Zenoh
  → 目标 daemon 的 teleport target service
```

建议代码位置：

```text
src/hyprial/shell/impl/cli/commands/teleport/
src/hyprial/daemon/impl/teleport/
contract/agent-teleport/
```

现有 `daemon.impl.transport` 是网络层，继续保留。业务命名使用 `AgentTeleportService` / `AgentTeleportOperation`，不要与网络 `TransportSession` 混为一类。

源 daemon 拥有 preflight、routine 暂停与等待、quiesce、导出、远端协调、失败处理、源清理和 commit。目标 daemon 拥有目标检查、分块落盘、home landing、routine 重建、strict resume 和最终 routine 恢复。

CLI/GUI 不得选择下一步、调用 rollback、推断执行状态或直接 import daemon 私有实现。

## 4. 当前代码事实与可复用部分

当前 dev 已有：

- `daemon.impl.transport.api.TransportSession` 的 bytes `put/query`、queryable 和订阅；
- `TransportSessionAuthority` 的 session 生命周期与恢复；
- OrgFS blob 的分块（`ORGFS_BLOB_CHUNK_BYTES = 768_000` 字节）、digest 校验和 assemble 模式；
- agent-home bundle、landing、runtime context activation 和 credential envelope；
- 节点级 `state/routines.sqlite3` 与 `RoutineService`。

可复用：

- bundle manifest、文件 digest、拓扑校验和目标 home receipt 重建；
- `landing/receive.py`、`landing/registry.py`、`landing/owner.py` 的有效部分；
- 从旧 `p2/orchestration/target.py` 抽出的 `RuntimeContextActivation`；
- 目标公钥加密凭据的 envelope 内部实现；
- OrgFS chunk 的参数与校验思路，但不是 OrgFS namespace、成员语义或业务存储。其编解码绑定 `spaceId` 与 `orgfs-blob` 类型名，只作模式参考，不直接复用。

不复用：

- SSH `RemoteRunner` 和目标机隐藏 CLI；
- P2 admission/journal/custody/override；
- 离线 trust-source enrollment、bundle attribution gate 和 prototype credential CLI；
- B01 的 move order/intent/receipt/OrgPolicy/TTL/消息通知体系。

2026-11-12 对 `dev` 的静态核对（未运行测试）另外确认了以下缺口，实施时需要补：

- routine 没有“等待 in-flight 清零”的接口，只能读 `inFlight`；`yaml_text` 已入库但不随 payload 输出；`routine.add` 的 IPC 路径会在添加后自动 resume，service 层 `add(enabled=False)` 可用；按执行主体列 routine 只有私有 helper。
- quiesce 与 strict resume 的逻辑嵌在 `daemon/impl/application/actors/transfer.py` 的 `transfer.*` handler 内，删除旧面之前要先抽出。
- `RuntimeContextActivation` 所在模块依赖 P2 journal 与 `AdmissionStore`，需要搬离 `p2/`。
- 现有跨节点 Zenoh query 都是拉取式；teleport 的“payload 随 query 推送、落盘后确认”没有先例。接收侧 query 队列有上限，session 重建期间会丢 query，源侧必须按 offset 幂等重试。
- `dev` 上没有 `move.*` daemon 方法；`transfer.*` 只在 legacy router 中，没有 MethodSpec，也不在 OpenAPI 里。
- GUI 后端仍是 HTTP 501 占位，GUI 端到端验收在后端接线前只能报 BLOCKED。

## 5. 最小 Zenoh 协议

内部仅保留五类操作：

```text
prepare
chunk
receive
commit
abort
```

使用独立的 `agent-teleport` keyspace，不经过 inbox，不把 home 当作一条大消息发布。

### prepare

源发送 operationId、sourceHost、targetHost、owner、源 agent、manifest digest、routine 名称和 runtime 要求。

目标比对 owner（一致性检查，不是身份证明，见 §6），检查 agent 名称、routine 名称和 runtime 能力，返回目标凭据加密公钥。此阶段不得停止源 agent，也不得启动目标 agent。

### chunk

源暂停 routine、等待 in-flight 清零并 quiesce agent 后，导出 home。

每个 chunk 是独立 Zenoh query：

```text
operationId + manifestDigest + offset + total + bytes
```

目标校验 operation 和 digest，将 chunk 写入私有 staging，确认该 offset 已持久化；源收到确认后再发下一块。这样用 request/response 获得背压，避免无界 `put()` 和内存堆积。

首版全量复制。实现时不得偷偷引入增量同步或复杂续传来扩大范围。

### receive

全部 chunk 到齐后，目标：

1. 验证 manifest 与完整内容 digest；
2. 落地 agent home 和 runtime context；
3. 以 paused 状态重建 routine，并重写源 agent URI 到目标 URI；
4. strict resume 目标 agent；
5. 返回目标身份与 routine 结果。

目标可以创建新的 incarnation；不要求源、目标 `agentId` 相同，但必须显式重建 routine 中的源 URI 引用。

### commit

源收到 receive 成功结果后：

1. 删除源 routine；
2. 清理源 agent/home；
3. 向目标发送 commit；
4. 目标恢复原本 enabled 的 routine。

源清理失败时不发送 commit，目标 routine 保持 paused，避免双端同时派发。

### abort

仅用于 receive 成功前：清理目标 staging 或尚未启动的落地内容，源恢复 agent 和 routine。

receive 已发出但结果未知时，不盲目恢复源。源 agent 与 routine 保持停止，返回 `UNRESOLVED` 和人工检查所需信息，不自动判成功，也不自动判回滚。

## 6. 对端认证：首版不做，后续由 Zenoh ACL 拦截

**决定（Allen，2026-11-12）：首版 teleport 不实现对端认证。mesh 内任一 peer 都可以向目标 daemon 发起 prepare/chunk/receive，在目标落地 agent home 并 strict resume；后续由 Zenoh ACL 拦截启动信息。** 本节取代首版 handoff 中“对端认证是前置门，不得跳过”的要求。该状态同时登记为 [KP-047](../reference/known-problems.md)。

决定的依据是 2026-11-12 对 `dev` 的核对，首版 handoff 给出的两条认证路径都不成立或缺接线：

- tailcat device 密钥是 Curve25519（Noise/WireGuard）密钥，只能做密钥交换，不能签名；私钥只在 sidecar 内，不进入 Python；`daemon/impl/transfer/archive/signature.py` 文件头明确禁止把它用于签名。成员记录只登记 nodekey，没有每设备的签名公钥。
- sidecar 确实校验入站 nodekey，但 Zenoh 入站是不带 PROXY header 的直接转接，Python 的 query handler 只收到 selector 与 payload，拿不到来源身份。
- Zenoh ACL 未实现（`zenohAcl: not-implemented-0.5.5.1`，`completeInterOrgIsolation: False`）。

由此接受的影响：

- 目标侧：mesh 内 peer 可在目标机上落地并启动其提供的 agent home。
- 源侧：prepare 返回的凭据加密公钥无法证明来自真实目标；冒充目标的 peer 可以收到 home 与随迁凭据。
- key 或 payload 中的 node/host/owner 都是自报值。prepare 的 owner 比对只防误操作，不作为安全措施，文档与结果字段不得把它表述为认证。

2026-10-28 的 [Zenoh ACL 与 agent move 职责边界](zenoh-acl-scope-and-move-boundary-2026-10-28.md) 记录了当时“未获授权的搬迁应该立刻拒绝”和 invite 授权流程。那是 agent-move 阶段的设计；teleport 首版按本节执行，不实现 invite、业务授权或签名，拒绝能力留给后续 ACL。

为让后续 ACL 能接上，首版必须做到：

- keyspace 把目标节点与操作类型放在 key 中，形如 `hyprial/v1/agent-teleport/<targetNode>/<op>/<operationId>`，使 ACL 可以按目标和操作分别放行，例如单独拦截 `receive`，或限制谁可以为某节点声明该 queryable。
- prepare 的操作描述保留 operationId、源/目标 host、owner、agent、manifest digest 和 routine 列表，chunk 与 manifest digest 关联，后续接入认证或 ACL 时不改 wire 结构。
- 不新增密钥、信任库或授权账本。

已知的后续工作（不属于首版）：Zenoh 原生 ACL 按链路身份（TLS 证书 CN、用户名或网络接口）匹配 subject，而当前 Zenoh 链路不携带这类身份；按 owner 或设备拦截之前，要先让链路带上可信身份。`identity/impl/device/transport_credential.py` 的 authority 签发凭据（绑定 owner 与 deviceUid）是候选，尚未验证。

控制 handler 不应在 Zenoh native callback 线程里直接执行长文件复制或 strict resume；应交给目标业务 service，并为每类操作设置明确的超时。

## 7. routine 的最小迁移语义

只迁移执行主体属于源 agent 的 routine（按现有 `actor` / `produces` / owner 语义判定）。仅出现在 escalation/coordinator 中的引用，不能把其他 agent 的 routine 一并搬走。

迁移内容是 YAML 定义和原 enabled 状态：

- 暂停并等待 in-flight 清零；超时则恢复原状态，尚未触碰 home；
- 不复制 `routines.sqlite3`；
- 不迁移 in-flight、cycle、effect、outcome 或 PAC 历史；
- 目标以 paused 状态创建；
- 只重写已迁移 routine 中指向源 agent 的 URI；
- commit 后恢复原 enabled 状态，原本 disabled 的保持 disabled。

routine 名称冲突、在途任务未清零或定义无法重建时，必须在源 home 被触碰前或目标 strict resume 前失败。

## 8. 明确删除范围

实现过程中删除，不保留兼容 alias：

- `hyprial agent move` 与整个隐藏 `hyprial agent transfer`；
- `transfer.*` daemon 方法（`dev` 上没有 `move.*`）；
- P2 custody/journal/override 和 daemon restore/shutdown 中的相关 wiring；
- `agent_home_chain.py` 的双 daemon 编排；
- non-agent-home P0、container、外部 cwd/session 专用路径；
- 离线 trust/signature/prototype CLI 与无其他消费者的实现；
- 旧 `move`/`transfer` 的公开错误、OpenAPI、文档和无效导出。

历史数据库表只停止使用，不主动删除用户数据。`agent home migrate` 是本机 layout 管理，不属于跨机 teleport；`orgfs.move`、通用 `TransportSession` 和其他同名普通词汇不做全局替换。

## 9. 实施切片（每片一个小 PR，直接进 `dev`）

每片独立可评审、可回滚，自带焦点测试；未接通真实 handler 之前不公开 CLI、MethodSpec 或 OpenAPI 条目。顺序可按依赖调整，不合并成大 PR。

0. **决策与 handoff 入库**：本文、`GLOSSARY.md` 的 teleport 术语、KP-047。
1. **routine 能力补齐**：YAML 定义导出、以 paused 状态创建、按执行主体列出、等待 in-flight 清零（带超时）。只加内部能力，不改已发布方法语义。
2. **teleport wire 与分块**：`agent-teleport` keyspace、prepare/chunk/receive/commit/abort 的编解码、目标私有 staging 与 digest 校验、`contract/agent-teleport/`。用两个真实 Zenoh transport 验证错误 digest、错误目标、重复块和中断重试。
3. **抽出生命周期原语**：把 quiesce、strict resume 和 `RuntimeContextActivation` 从 `transfer.*` handler 与 `p2/` 中抽成可复用单元，行为不变。
4. **目标侧 service**：五类操作的 handler、home landing、runtime context 激活、strict resume；先不含 routine。
5. **源侧 service 与公开入口**：`AgentTeleportService`、`teleport.run` 的真实 MethodSpec 与 OpenAPI、`hyprial agent teleport` CLI、禁止 shell/GUI 导入 daemon 私有 teleport 实现的架构测试、单 agent-home 的真实双 daemon 流程。
6. **routine 全链路**：暂停与等待、目标 paused 重建与 URI 重写、commit 后恢复 enabled。
7. **删除旧面**：`agent move`、隐藏 `agent transfer`、`transfer.*`、P2 custody/journal/override 及其 restore/shutdown 接线、SSH 总控，不留 alias；同步 skill、文档与导出面。
8. **最终验收**：结构门、焦点测试、全量测试、ruff 和真实双 daemon/Zenoh 验收。GUI 入口在 GUI 后端接线后另行补上。

## 10. 验收要求

至少覆盖：

- 两个隔离的真实 daemon 和真实 Zenoh；整个路径不调用 SSH；
- 多 chunk agent home，含 enabled 与 disabled routine；
- 无 trust-source 预配置；对端认证项按 §6 记为未实现，不写 PASS；
- 目标 strict resume 和目标 URI 下的 routine 重建；
- prepare 失败时源完全不变；
- chunk 中断时源恢复；
- receive 结果未知时源不恢复、无双活，返回人工检查信息；
- 源清理失败时目标 routine 仍 paused；
- CLI 与 GUI 使用同一 params/result/error 契约（GUI 后端未接线前，GUI 端到端记 BLOCKED）；
- CLI help 和 daemon catalog 中不存在 `agent move`、`agent transfer`、`transfer.*`；
- 全量复制，无增量/断点续传；未实测项明确写 NOT_RUN 或 BLOCKED，不称 PASS。

常用检查：

```bash
uv sync --locked --python 3.12 --extra test
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run --with libcst==1.9.0 pytest tests/
uv run ruff check .
```

测试产生的 home、socket、端口、缓存必须与日常环境隔离，不读取、改写或清理生产 daemon、凭据和用户级配置。

## 11. Git / Forgejo 与交付纪律

- 每个切片从最新 `dev` 切 `<type>/<slug>` 短分支，PR 目标为 `dev`（2026-11-08 起的 dev-first 流程，见 `.agents/skills/forgejo-ops/SKILL.md`）。
- 不使用 `gh`。Forgejo 操作与 git 传输按 forgejo-ops skill 执行；凭据不出现在任何 URL 中。
- 提交 author/committer 邮箱必须属于已注册 Forgejo 用户；agent 身份写 `Co-Authored-By` trailer。
- PR 首行是 `Submitted-by: agent:<URI>`，按 diff 填 `Skill-Impact:` 和必要的 `Contract-Impact:`。
- 实测事实、推断、NOT_RUN 和 BLOCKED 分开写，不用 skip 冒充结论。
- 不自签、不合并，由 Allen 决定合并。

## 12. 开发者第一项行动

先核对 `dev` 当前 head 与 §4 的代码事实是否仍成立，再按 §9 从切片 1、2 开始。目标侧 handler 与公开入口只在真实实现接通后注册，不公开只能返回成功占位的 CLI/GUI。

## 13. 当前进度（2026-11-14）

本节记录实现状态，供失联后接手；逐项证据见 [验收记录](../evidence/agent-teleport-acceptance-2026-11-12.md)。

已进入 `dev`：

| 切片 | 内容 | PR |
|---|---|---|
| 0 | 决策、handoff、检查标准、协议 | #2711 |
| 1 | routine 能力补齐 | #2713 |
| 2 | wire、分块、目标 staging | #2714 |
| 3 | 生命周期原语 | #2715 |
| 4a | 协议消息与 bundle 流 | #2716 |
| 4b–6 | 目标侧与源侧 service、`teleport.run`、CLI、routine 全链路、双 daemon 验收 | #2721 |
| 7 | 删除 `agent move`、`agent transfer`、`transfer.*`、P2 custody、SSH 总控 | #2723 |
| 8 | 验收证据 | #2724 |

`hyprial agent teleport AGENT --to HOST [--dry-run | --yes] [--json]` 是唯一的迁移入口。#2147 原有的 B01 实现、
`contract/agent-move/` 与 `docs/evidence/agent-move-*` 不进入文件树，保留在该分支的 git 历史中。

未完成或未验证：

- 对端认证未实现（KP-047）；mesh 上任何 peer 都能让目标 daemon 落地并启动 agent，后续由 Zenoh ACL 拦截。
- 真实 vendor harness（claude / codex / pi）与两台物理主机之间的迁移：NOT_RUN，目前只有协议替身。
- GUI 端到端：BLOCKED（后端 501）。
- “receive 结果未知”“源清理失败”两种场景只有组件级覆盖，双 daemon 下 NOT_RUN。
- #2721、#2723、#2724 合入时 CI 未全绿，由产品负责人知情后手动合入。当时的三个未决问题：
  pytest worker 原生崩溃（KP-048）；teleport 双 daemon 用例所在分片的 worker 被杀（推断为 CI 内存上限，未证实）；
  routine 在途场景在 CI 偶发失败（本地未复现，原因未知）。
- `agent transfer runtime downgrade-state|probe` 已改挂到 `hyprial agent runtime …`，不留 alias。

后续工作：

1. （已处理，2026-11-14）CI 上 teleport 双 daemon 用例所在分片的 worker 被杀、routine 在途场景偶发失败，
   原因是该分片内存越过 runner 的 4 GB 上限：当日含这些用例的 6 个分片全部丢失 worker 并伴随 19–101 GB
   磁盘读，其余分片没有。`tests/integration/teleport/` 改由 `gate-python.yml` 的 `pytest-shard (isolated)`
   单独运行，`scripts/ci/pytest_impact.py` 保证它不进入任何分片。
2. shard 任务本身距 4 GB 上限仍然很近（两个长寿命 pytest worker 各自可达约 1 GB），今后新增重型集成测试
   可能再次触发；需要时提高 runner 内存上限或把新用例放入 isolated 通道。
3. Zenoh ACL 设计与实现，关闭 KP-047。
4. 真实 harness 与跨主机验收。
5. `registry.create_transfer_hosted`、`CreateTransferHostedAgentCommand` 已无生产调用方，可在后续切片清理；
   `hosted_by='transfer-receive'` 是已发布的数据值，保留。
