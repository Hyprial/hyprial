# M-01：`agent.list` / `agent.secret.list` 声明化（2026-10-31）

卡 `aa146d2c-d501-4001-ae85-b313b4b2ebde`，源分支 `feat/request-agent-read-specs`，
目标容器 `intg/gui-request-examples`。基线 dev `df9d505029b6002560fa732a2d73d8ac15610b74`。
参考规范：协调者冻结包 `gui-http-methodspec-reference.md`、`hm-pac-activation-2026-10-31.md`。

## 1. 范围与结果

只迁移两个只读方法，复用旧 handler，不改业务返回、权限、CLI 输出：

| CLI | method | 参数 | 结果 |
| --- | --- | --- | --- |
| `hyprial agent list [--inactive-since D] [--exclude-wf]` | `agent.list` | `inactiveSince`（秒数或 `<n><ms\|s\|m\|h\|d>`）、`excludeWf`（bool） | `ok`、`agents[]`、`agentsWithoutRoutine[]` |
| `hyprial agent secret ls [ACTOR]` | `agent.secret.list` | `actor`（可省略 / null / 非空串） | `ok`、`grants[]` |

两个方法均为 `unary`、`mutates=false`、`retry=safe`、`confirmation=none`、
`http_exposed=true`、`audience=(local-operator, bound-agent)`，声明在
`src/hyprial/daemon/impl/request_catalog/agent.py`。

`hyprial agent secret list --agent X` 是另一条 CLI 能力（本地 secret store 元数据），
不经过 daemon 的 `agent.secret.list`，本卡不迁移也不改变它。`agent secret ls` 返回的是
**所选 agent 的 grant 清单**（省略 actor 时为该节点所有 agent 的 grant），不是 secret 值，
也不是调用者的身份；目标 agent 只作为业务参数出现，没有 caller 参数。

## 2. 关键决策

1. **精确方法名的部分注册**：`registry._build_registry()` 聚合 `AGENT_BINDINGS`；
   `MIGRATED_FAMILIES` 仍只有 `{"link"}`，`agent` 家族保持开放，因此
   `agent.get`/`agent.secret.grant` 等未迁移方法继续走 legacy router。
   `agent.PARTIAL_MIGRATED_METHODS` 显式记录这两个名字，测试同时钉住“不得把整个 agent
   家族标为 migrated”。
2. **复用旧 handler**：两个绑定都调用 `app._handle_agent`，并复制 router 原本的
   `AgentError`/`DomainCommandError`/`AgentHomeError` → `DaemonRequestError` 翻译，
   使错误码与消息在注册路径上保持一致。
3. **参数 schema 保持旧入参准入，不新增收紧**：实测旧 handler（真实 app）后确认
   - `agent.list`：`inactiveSince` 省略/`null`/数字/时长字符串均成功，布尔、数组、对象
     仍由 `parse_duration` 返回旧 `INVALID_ARGUMENT` 文本；`excludeWf` 只认 `is True`
     （`null`/`"yes"`/`0` 都被忽略）；未知参数被忽略。
   - `agent.secret.list`：`actor` 省略/`null`/非空串成功；空串、数字、布尔、数组返回
     旧 `INVALID_ARGUMENT "actor must be a non-empty string"`；未知参数被忽略。
   schema 因此声明 `additionalProperties` 为显式全 JSON 类型并集（MethodSpec ABI 禁止
   字面 `additionalProperties: true`），`excludeWf` 同样放开为任意 JSON；这是兼容行为的
   诚实声明，不是空 schema。`inactiveSince` 的时长语法仍只有 `parse_duration` 一处权威。
4. **结果 schema 行级闭合**：agent 行在顶层闭合，新增字段必须回到声明里；`capabilities`
   保持开放对象（registry 本就接受任意 JSON facts），`config`、`runState`、
   `sharedCredentials`、recovery 行按真实生产者完整声明。`runState` 的 `epoch`/
   `runtimeIncarnation` 是**契约字段名**而非运行时值，仍属离线投影。
5. **错误**：`agent.list` 声明 `INVALID_ARGUMENT` 与 `AgentError.code`（`AGENT_ERROR`，
   取自真实异常类而非字面量）；`agent.secret.list` 另有 `AGENT_NOT_FOUND`
   （未知 actor 由 `registry.require` 抛出）。错误均无 `data`，与 handler 实测一致。
6. **HTTP 元数据**：只写 `http_exposed=true`，不复制 `HttpBinding`、不导入 FastAPI。
   H 线 ABI 由 H-00/H-03 决定；本卡不等待 H runtime。

## 3. 契约锚点与投影（按协调者裁决修正）

`contract/daemon-request/openapi.json` 是 MethodSpec 的确定性离线业务投影。任何方法进入
catalog 都会改变 head 快照，因此本卡重新生成该文件
（`uv run python scripts/verify/dump_daemon_openapi.py --write`，head 摘要 `181e3731…`）。

历史锚点**保持不动**：`scripts/ci/a03-contract-seed.json` 的 `openapi.sha256` 与
`scripts/ci/check_request_contracts.py::OPENAPI_SHA256` 都是 `8b8a83ba…`，即
`acceptedSha 20269c96…` 里那份 OpenAPI blob 的真实摘要（已用 `git show` 实测）。
先前本卡曾把锚点改成 head 摘要；协调者裁决（`git:1923a5b0b:docs/notes/
m01-seed-and-fj-review-2026-10-31.md`）指出这违反“acceptedSha 不变则其 digest 不变”，
已恢复原值。

真正的缺陷在 `test_seed_digests_are_immutable_and_head_ledger_is_a_reviewed_decrease`：
它错误地要求 **HEAD 快照等于历史 seed**。修正后分别验证：

- 历史：`acceptedSha:<openapi.path>` 的 blob 摘要 == seed 的 `openapi.sha256`；
- 现在：head 快照由生产 `dump_daemon_openapi.py --check` 独立验证（与生成物逐字节一致）。

负控保留并补充：缺历史 commit 时 fail-closed、篡改 `importRatchet`/`acceptedSha`/
`openapi.sha256` 的 seed 在 ledger 检查前被拒、head 投影漂移被生产 gate 判 FAIL。
先红后绿证据：恢复锚点前新测试在 `8b8a83ba…`（历史 blob）与 `181e3731…`（当时 seed）
之间报 `AssertionError`；恢复后 25/25 PASS。未扩大 CI 产品逻辑。

M-02 会触碰同一快照，M-03 汇总后需再生成一次。

## 4. 验证

- 结构/边界门：`check_structural_limits.py`、`check_module_boundaries.py` PASS。
- 焦点：`tests/daemon/ipc/contracts/test_agent_read_specs.py`（真实 DaemonApplication，
  legacy `_handle_agent` 与 registry 路径逐字段对照；非迁移 agent 方法仍可路由；
  **省略/null/额外参数与错误组合的旧 handler 准入矩阵**；空与非空 grant 清单）。
- 真实 IPC + CLI：`tests/daemon/ipc/test_agent_read_ipc.py`（隔离 home 的真实 daemon
  socket；`agent list`/`agent secret ls` 输出与 IPC 结果一致；未知 agent 退出 1 且
  `AGENT_NOT_FOUND`）。
- 契约：`dump_daemon_openapi.py --check`、OpenAPI 快照/目录测试、method-registration
  gate、request-contract seed 测试。
- Ruff `uv run ruff check .` PASS。
- 继承自 dev 的 FAIL（在 `df9d50502` 的干净 worktree 复现，与本卡无关）：
  `test_no_literal_proto_code_outside_the_registry`（`system_notices.py` 字面量
  `AMBIGUOUS_TARGET`）、`test_skipped_rebuild_leaves_ps_and_targets_identical_to_a_fresh_one`、
  `test_ps_agent_rows_frozen_across_the_snapshot_refactor`（期望缺 `creationOrigin`）。
- 未测：真实 HTTP/Uvicorn、TS client、浏览器联调属 H/HM 线；本卡不声称这些通过。
