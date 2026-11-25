# org 生命周期批2：create / invite / join（实现计划）

- 目标分支：`intg/gui-v1`
- 实现分支：`feat/gui-org-lifecycle`
- 基线：`feat/gui-org-members` @ `1db464d93`（PR #2277，尚未合并）
- PR 形式：stacked PR，目标仍为 `intg/gui-v1`；#2277 合并后 rebase，PR diff 自动收敛为批2改动。
- 优先级：**PR #2277 的任何评审/CI/merge 请求优先于本计划**；本计划暂停让路。
- 状态：计划已落库，等待 PAC 执行；不是验收结论。

## 1. 范围（只做后端确实存在的能力）

完成一条最小真实生命周期：

1. 没有组织 → `org.create` 创建组织
2. 创建者 → `org.invite` 邀请一个用户
3. 被邀请者 → `org.join` 用邀请链接加入

本批只注册这三个方法，并为 GUI 已有动作接线：

| GUI 动作 | daemon method | 现有 handler |
| --- | --- | --- |
| 新建组织 | `org.create` | `ipc_org_create` → `OrgNetworkService.create` |
| 邀请成员 | `org.invite` | `ipc_org_invite` → `OrgNetworkService.invite` |
| 用邀请链接加入 | `org.join` | `ipc_org_join` → `OrgNetworkService.join` |

## 2. 明确不在本批

- **拒绝邀请**：daemon 没有 reject/decline 方法，`org.pending` 是 CLI 侧 Casdoor
  轮询，不是 daemon request。不得用本地删除冒充拒绝；标记 BLOCKED/UNKNOWN，等产品决定。
- **更新组织**：没有 `org.rename` / `org.update` daemon 方法；GUI 的 `renameOrg` 仍保持
  `unavailable`，不伪造成功。
- 收到邀请列表 / claimInvite：没有 daemon 方法；保持未接通。
- leave / remove / delete / repair / acl / fs / work：不在本批。
- 全量 Python：按用户决定留到 intg 定稿后由 coordinator 执行。

## 3. 方法层要求

对每个方法：

- 新建 `MethodSpec`，`mode="write"`、`mutates=True`、`retry="never"`、
  `confirmation="none"`、`http_exposed=True`、`http_binding=HttpBinding(IdentityBinding("none"))`；
  `audience` 按现有 org 方法（local operator）。
- handler 只调用现有 `ipc_org_*`，不复制业务逻辑。
- **逐个审计真实结果与错误**：用现有 handler/service 代码、测试和真实隔离 daemon 调用确认
  params/result 的精确形状；声明的 error code/data 必须与 wire 一致，可达小写码走
  `LEGACY_KEBAB_CODES`，不得放宽语法、不得静默改写。
- 从 `_ORG_NETWORK_METHODS` 删除这三个方法的影子映射（与 list/fetch 的清理一致），确认
  registry 先命中；`MIGRATED_FAMILIES` 不加入 `org`。
- 添加 `MethodExampleSet`（成功、空/不可用、参数拒绝、真实 domain error），示例数据必须通过
  MethodSpec 校验；`dump_daemon_openapi.py --write` + `npm --prefix gui run client:reference`。
- 真实 HTTP 测试：隔离 daemon + 真实认证 Uvicorn，覆盖成功/参数拒绝/未就绪或可达 domain error
  以及 `dispatch` 三态；不用 mock 代替。

## 4. GUI 要求

- `org-request-api.ts` 只经生成 client 调 `org.create` / `org.invite` / `org.join`，映射为
  现有视图模型：`OrgSummary`、`InviteResult`、`JoinStep`。
- `live-source.ts` 实现 `createOrg`、`createInvite`、`joinByLink`，复用现有 `failed()` /
  `resultMessage` / `ORG_FAILURE_REASONS` 语义；不自动重试写操作。
- 创建成功后刷新 `org.list`，不伪造设备发布成功；`published:false` 如实显示失败。
- 邀请成功后显示链接/命令/过期时间；不把链接写入 URL/localStorage。
- 加入成功后按真实 steps 更新进度并刷新；失败/未知按三态展示。
- `claimInvite`、`renameOrg` 等无后端动作保持 `unavailable`。
- 单测覆盖 adapter 映射、live source 成功/失败/unknown、页面动作；浏览器契约验证只发
  `/daemon/request/<method>`，无第二套 REST。

## 5. 验证

- 定向 pytest：方法声明、绑定、HTTP、示例校验、原有 org 测试。
- `uv run ruff check .`、结构门、模块边界门、`dump_daemon_openapi.py --check`、
  `npm --prefix gui run client:reference:check`、`git diff --check`。
- GUI：`npm --prefix gui run typecheck`、`npx vitest run tests/modules/dev04`；
  最终按 PR 门禁 `npm --prefix gui run verify`（由 coordinator 在合并前执行）。
- 真实双 daemon create/invite/join 可用 E2E-025 链路验证；本批优先用真实隔离 HTTP +
  现有 E2E-025 扩展，不能把 mock 当真实 mesh 结论。
- 三态记录：PASS / FAIL / BLOCKED / UNKNOWN，不用 skip 冒充。

## 6. PAC 执行

- 一个 `deepseek-flash` thinking max worker，顺序两个节点：
  1. `methods`：三方法 MethodSpec + binding + router 清理 + 示例 + OpenAPI + 真实 HTTP 测试；
  2. `gui`：生成 client 更新、adapter/live source/页面接线 + GUI 测试。
- worker 禁止全量 pytest、禁止改 PR #2277 工作树、禁止 push、禁止伪造成功。
- coordinator 负责提交、推送、PR、独立评审与 2277 优先级。
