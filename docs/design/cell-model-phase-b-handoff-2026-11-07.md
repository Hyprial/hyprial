# Cell model phase B 交接文档

状态：2026-11-08 已完成一次性 rebase 和现状核对，可交给独立 session 施工。WIP PR #2551 的 base 为 `dev`，保持 WIP，不作为一次全量合并的实施 PR。早期 `release/v0.5.5.5` 是历史版本口径，不再作为当前分支目标。

**新 session 从 [cell-model-phase-b-session-prompt-2026-11-08.md](cell-model-phase-b-session-prompt-2026-11-08.md) 开始。** 本文记录当前基线、剩余范围和批次边界。

## 1. 当前基线（2026-11-08 实测）

- 集成分支：`intg/cell-model-phase-b`；目标分支：`dev`。仅含交接文档阶段的 rebase 已完成，当前交接提交落在 `dev@284dcf72942b1b1b2495c9bbddc1e646cfc386a3` 之上。
- #2513 的最终提交 `54d8637680897d65fef4632937f068fc723f99ce` 是当前 HEAD 的祖先。原 `release/v0.5.5.4` 分支已删除，不再要求新 session 对该引用执行不可用的祖先检查。
- `src/hyprial/daemon/impl/{cells,threads,delivery,conversations}/` 均存在。
- `src/hyprial/daemon/impl/squire/` 不存在。squire 已由 `agent_templates/squire` 创建为普通 agent，角色在 cell params，user delivery 在 `adapters/user_delivery/`；**不得恢复 squire 代码特例目录**。
- S8 attach 已在树中：`daemon/impl/composition/cells/attach.py` 存在。`agent.attach/detach` 仍按既定决定只走 IPC。
- `threads.get/list` 已是 HTTP exposed，使用 `local-owner-user-selector`，并具有 examples 和真实 HTTP 测试。
- `conversations.list/history` 已注册 IPC handler，但两个 MethodSpec 仍为 `http_exposed=False`，没有 HTTP examples；这就是第一个子 PR 的起点。
- GUI 仍存在 `sessions.history`、`message.history`、`link.add/list` 消费路径。不要在第一个 HTTP 子 PR 中顺带全局替换 GUI。
- `daemon/impl/links/`、`inbox/service/link_ledger.py`、`network/route_delivery.py`、`route_registration/` 仍存在；`ThreadAuthority` 仍以 LinkService 为引擎。
- `trustMembersForUpperLayers` 仍在 `forwarding/membership/settings.py` 中，默认值为 true。S1 收尾尚未完成，不能机械删除开关而不验证 Zenoh ACL 与目录门禁。

本次基线实测：

| 检查 | 结果 |
|---|---|
| rebase 到最新 `dev` | CLEAN，无冲突 |
| 四个新模块存在、`squire/` 不存在、cell-model 最终提交为祖先 | PASS |
| `uv run --python 3.12 --extra test pytest tests/daemon/conversations tests/integration/request_http/test_http_entry_live.py -q` | **98 passed** |
| GUI vitest / typecheck | **NOT_RUN**；本次交接环境尚未安装 `gui/node_modules`，由实际子 PR 安装并验证 |
| 全量 `tests/`、真实 OrgFS/Lark、跨版本 daemon、proxy-only attach | **NOT_RUN / BLOCKED** |

当前 `dev` 另有已登记的 **KP-028、KP-029、KP-036**：request-contract/import ratchet、方法注册门存在已知失败。2026-11-08 本地 merge-tree 另实测 unbounded-waits（3 项）与 provider 术语 lint（1 failed / 13 passed）为红，Node CI-policy 为绿色；纯 `dev@284dcf729` 复跑得到同一组失败。子 PR 必须保留并归因这些共享基线失败，不得通过重生成 baseline、扩大豁免、改写 SDK 名称或顺手修无关门禁求绿。

## 2. 剩余施工范围与顺序

每项一个或多个独立子 PR，base 均为 `intg/cell-model-phase-b`。

### 第一批：conversations.list/history HTTP 暴露

目标不是简单把 `http_exposed=False` 改为 true，而是复用 `threads.*` 已验证的 HTTP 身份边界，保持 IPC 行为不变：

- 为两个方法设置正确的 `HttpBinding(IdentityBinding(...))`，由宿主注入 `from=user:<owner>`。
- 按 `threads.*` 的 `x-hyprial-http-forbidden` 规则处理宿主专用字段；浏览器提交的 `onBehalfOf` / `sessionRef` 必须在 dispatch 前返回 400 `INVALID_ARGUMENT`，不能伪造 session 或 operator 身份。
- 保持现有 IPC params/result/error 语义、conversation cell id 与 thread 的映射、cursor/limit/mode 和 OrgFS 不可用时可读的保证。
- 新增 schema-validated examples，至少覆盖 success、empty、invalid-params；注册进 `request_catalog/registry.py`，contract mock 不得新增 skip。
- 更新真实 HTTP entry 测试，证明两个方法可调用、host-only 字段被省略/拒绝、身份不能被浏览器选择。
- 更新 `gui/tests/features/user-settings/contract-gap.test.ts` 等手写独立期望：这是有意的契约更新，不是重新生成期望集求绿。
- 按规定顺序重建 openapi、TS client、CLI inventory/surface 和 post-install surface；生成物不手工合并。

### 第二批：GUI 改读 threads/conversations

- 先确认 GUI 线 owner 和当前打开的 PR，再取得 `gui/` 修改窗口。
- 会话列表与历史改读 `threads.*` / `conversations.*`，旧 `sessions.history`、`message.history` 只保留兼容用途。
- `link.add/list` 按实际消费者区分 send-link 授权、网络 link 和组织关系展示，不做全局文本替换。
- 遵守单套 GUI 规则、可信 principal、共享页面入口和 UI 交接模板；mock 不能冒充真实 backend 验收。

### 后续批次

1. **退役 links**：把 `daemon/impl/links/` 的 send-link 授权/预算和 `inbox/service/link_ledger.py` 迁入 `threads/` 后删除旧模块。`daemon/impl/inbox/links/` 是 delivery 传输层，不并入 threads。
2. **退役 route**：拆分 `network/route_delivery.py` 的端点绑定与送达职责，分别进入 `threads/` 与 `adapters/`；`route_registration/` 改名为 `session_delivery/`。`route:` 暂时保留为 thread 别名和弃用日志。
3. **别名下线计划**：为 `route:`、send-link、`sessions.*` 等制定时间表；本阶段只写计划，不执行外部别名删除。
4. **S1 网络层收尾**：删除 `trustMembersForUpperLayers` 与 OrgFS 目录播种名册的过渡路径前，先确认 HQ PAC/网络线现状以及 Zenoh ACL 严格路径，补正反向门禁测试。安全语义不充分时停止并报告 Allen。
5. **真实环境验收**：真实 OrgFS/Lark E2E、跨版本双 daemon 实际收发、proxy-only 出站下 attach 的 batch/reply 闭环。只能使用 Allen 批准的隔离测试环境；跨版本旧版本号在执行前确认，不沿用失效 release 分支猜测。

## 3. 与 #2541 和 GUI 线的协调

- 两条 WIP 线不合并。当前可并行的是本线 conversations HTTP 与 #2541 的 P2c。
- #2541 的 B1/B4/B5 会碰 dispatch、PAC、shutdown、`adapters/user_delivery/{actor,ledger_actor}.py`、`identity/profiles.py`；本线 links/route 退役也可能接近这些区域。修改前通过 Allen 约定唯一 owner 和冻结窗口。
- 修改 `gui/` 前先确认 GUI 线，不与另一个 session 同时写同一组件或生成 client。
- registry、openapi 和生成物在同一时间只能有一个 owner；不得手工拼接生成物。

## 4. 既定但不在本线顺手改变的决定

- 不新增对发送方 `threadId` 与端点关系的额外限制；这是 Allen 在 #2358 的既定决定。
- 不在本线顺带修改 `TARGET_SQUIRE_*` 的跨版本映射、proxy 未声明错误码映射、`HYPRIAL_SENDER_AUTH_MODE=enforce` 下 attach 读取限制。
- `agent.attach/detach` 仍只走 IPC，不在 conversations HTTP 子 PR 中额外暴露。
- `management.squire.prepare` 的注册缺口不属于本批；不要借恢复 squire 特例解决。
- 不加没有明确必要的新安全限制；发现设计与代码冲突时报告 Allen。

## 5. 分支、契约与验证纪律

- 本次交接已经完成唯一一次 rebase。新 session 不再 rebase 或 force-push；需要同步 `dev` 时使用 merge commit。
- 每段从最新集成分支建短分支并开独立子 PR；前序依赖未合入时不堆叠后续批次。未获明确授权不自行合并。
- 生成顺序：
  1. `scripts/verify/dump_daemon_openapi.py --write`
  2. `npm --prefix gui run client:reference`
  3. `scripts/verify/check_cli_help_inventory.py --write`
  4. `scripts/verify/update_cli_surface_snapshot.py`
  5. `scripts/verify/regenerate_post_install_surface.py`
- 手写的完整性列表可以因新增方法而人工增加；派生快照必须重建。二者不能混淆。
- 新 daemon 模块登记 `NATIVE_ORIGINS`；错误码使用 `ipc_errors` 常量；保持结构门、模块边界门和 `contract/run-all.sh --isolated-only`。
- 行为变化先 RED 后 GREEN；测试 home/socket/端口隔离，不读取或修改生产 daemon、Lark App、凭据或用户级配置。
- 每个子 PR 写实际命令和 PASS/FAIL/BLOCKED/UNKNOWN，区分基线失败与本批回归，不用 skip 冒充结论。
