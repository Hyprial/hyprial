# AT10 能力枚举 → 真实入口映射（SPEC 修订，卡 359 item 0）

- 日期：2026-10-01
- 作者：codex-sw（本线维护者）
- 状态：**SPEC 修订，待 cc-sw-review 评审合入**；评审通过并合入之前，实现不得以本文件之外的解释扩大执行面
- 对应：卡 359（AT10）item 0「把已冻结 capability 枚举对到真实使用入口；现有规格未冻结 tool-surface/channel/org-context 执行接口；由本线维护者先交 SPEC 改版」
- 与 item 3 的声明同批（见 §5）

## 0. 一句话

冻结的六个 capability 里，**只有两个在 daemon 真实入口上有执行点**：`channel`（出站渠道路由）和 `org-context`（组织共享目录读）。`tool-surface` / `shared-path` / `isolation` 三项**当前没有执行接口**，只有台账；`agent-home` 是固有项，永不授给访客。台账不冒充执行，没有入口的三项按 §4 显式豁免。

## 1. 枚举与入口对照

下表由代码单一真相源 `src/hyprial/agents/enforcement.py::CAPABILITY_ENTRY_POINTS` 与 `GRANTABLE_CAPABILITIES` 固定；`tests/test_at10_capability_entry_map_doc.py` 是防漂移守卫（本表与代码对不上会红）。

| capability | 可授予 | 冻结 scope | 真实执行入口 | 本次状态 |
|---|---|---|---|---|
| `agent-home` | 否（固有） | — | agent 自己的 home，天然可写；不是授权对象 | 固有，不变 |
| `channel` | 是 | 精确 `route:<adapter>:<route>` | daemon `message.send` 的出站路由目标 | **已接线**（#1074） |
| `org-context` | 是 | 固定 `accepted` | 组织共享目录读 | **已接线**（#1074） |
| `tool-surface` | 是 | tool 名单 | 无 | 台账 only，豁免（§4） |
| `shared-path` | 是 | 路径 | 无 | 台账 only，豁免（§4） |
| `isolation` | 是 | `directory` / `container` | 无（创建时声明的值） | 台账 only，豁免（§4） |

## 2. `channel`：daemon `message.send` 的出站路由

- 入口只认 `route:<adapter>:<route>` 形状的目标（`classify_target_identity` → `TARGET_KIND_CHANNEL_ROUTE`）；agent 地址与 user 地址**不**走这条闸，由 AT09 的 send-to 台账判。
- 闸只对**已核实的 hosted 调用者**生效（与 `_enforce_send_to` 同一把 fence：hosted 才 fence，native 保持原有视图）。
- 授权是**逐 route 最小授予**：grant 的 scope 必须点名那条 `route:<adapter>:<route>`，不是整个 adapter 放行。
- 每次调用重读台账、不缓存判定：撤回在下一次调用立即生效。
- 拒绝码：`CHANNEL_NOT_AUTHORIZED`（grow-only，`src/hyprial/contracts/ipc_errors.py`）。
- 代码：`src/hyprial/daemon/application.py::_enforce_channel`、`src/hyprial/agents/enforcement.py::check_channel`。
- 用例：`tests/test_daemon_capability_wiring.py`（放行、缺 grant 拒绝、伪造/顶替 binding 拒绝）。

## 3. `org-context`：组织共享目录读

- 作用域固定为 `accepted`（只对已接受的组织文档），**不按空间逐项授予**；scope 形状不对时按 `EnforcementScopeError` 拒绝写入。
- 入口先 fence 再判是否 hosted（`_capability_caller`）：**带了 `sessionRef` 的调用者一律先验证**，带伪造/过期 binding 的 native agent 读内容会以 `STALE_SESSION` / `SESSION_SUPERSEDED` 被拒。只有 binding 有效的 native 调用者、以及完全不带 binding 的 host-local（operator/CLI）调用者不需要 grant。⚠️ 这一点与 §2 channel 和 `targets` 的「native 保持原有视图」**不同**：native 名字豁免只覆盖 targets 的只读列表，不授权拿伪造/过期 `sessionRef` 读组织内容。
- 每次调用重读台账；撤回下次即判。
- 拒绝码：`ORG_CONTEXT_NOT_AUTHORIZED`（grow-only）。
- 代码：`src/hyprial/daemon/application.py::_enforce_org_context`、`src/hyprial/agents/enforcement.py::check_org_context`。

## 4. 没有执行入口的三项：显式豁免

本线**不**为下面三项开工（没有执行接口就不实现，也不让台账冒充执行）；它们保留为台账记录与会话可见性，`hyprial agent grant <ACTOR> check`（输出里的 `entryPoints`）把 `CAPABILITY_ENTRY_POINTS` 原样报给运维：

- **`tool-surface`**：harness 工具 broker 的 allow-list 属各 harness 自身能力面；本线只记账，不做执行强制。落地需另立规格。
- **`shared-path`**：当前没有跨出 agent home 的挂载/读写入口被本线接线（容器挂载属 AT08 范围）；本线只记账。
- **`isolation`**：`directory` / `container` 是创建/邀请时写入的声明值；本线不把它当运行时强制闸。真实隔离效果由 AT08 的容器边界与冒烟判定，不在本文件承诺范围。

豁免不等于安全：上述三项在没有执行入口期间**不构成越权防线**，对外承诺以支持矩阵（卡 361 / `docs/guides/transfer-at12-cross-machine-runbook.md`）为准。

## 5. 已知限制声明（卡 359 item 3）

> **撤回授权只改变后续判定的答案。** 已经把数据读进内存、写进缓存、复制到别处或已经交给模型上下文的秘密，**不会**因为删掉台账记录而消失；本线的撤回语义是「下一次调用按新台账判」，不是「已外泄内容回收」。缓存数据的生命周期由持有它的 harness/模型提供方决定；inbox 与回执的留存语义按 P0 既有约定，不因撤回而改写。

## 6. 改版流程

- 本文件是 SPEC 改版，须经 cc-sw-review 评审合入后，实现才可据此扩大或收窄执行面。
- 新增第三个执行入口（例如把 `tool-surface` 或 `shared-path` 接到真实调用点）必须先改本文件并重新评审，再写实现。
- 防漂移：本文件 §1 的表必须与 `CAPABILITY_ENTRY_POINTS` / `GRANTABLE_CAPABILITIES` 逐项一致，守卫测试 `tests/test_at10_capability_entry_map_doc.py`。
