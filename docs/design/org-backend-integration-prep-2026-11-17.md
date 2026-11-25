# 组织后端接入开发准备（2026-11-17）

> 历史准备记录。2026-11-20 的当前范围已收敛为使用现有 `targets` 展示同组织成员 Agent 并打开聊天；本文提及的四个 Agent 归属 IPC 不在当前公开契约中。

基线：`dev@88564fcf442024ce38db07114c74c017de5a9f9c`。以下缺口表记录开发前快照：相关源码在该基线与本地 `4e5787d39c5c92e3fbc31395f3583ab3575d58a1` 间无差异，当时离开确认的本地修复尚未提交、推送或完成双账号复验。本文记录现状、开发顺序和验收边界，不代表功能已验收。

## 先处理已复现的组织生命周期缺口

| 场景 | 已核实结果 | 下一步及验收 |
| --- | --- | --- |
| 邀请链接加入 | 双账号隔离实测中，账号二提交后 GUI 显示“结果未知”；随后两端 `org.list` 和重开 GUI 均确认账号二已经加入。不能将首次 GUI 回执判为通过。 | 查写入成功后响应校验、超时与刷新路径；未知结果只读核对，不自动重复加入。用两账号真实后端复验首次回执和重新打开后的状态。 |
| 成员离开 | 双账号隔离实测中，GUI 确认收到 daemon 拒绝；随后 CLI 读取新预览并 `org leave --yes` 成功。持久化移除操作记录显示 revision 从 3 刷新为 4；daemon 将 `REVISION_CONFLICT` 留作待再次确认阶段。 | 本地 GUI 适配已将 `REVISION_CONFLICT` 视为预览失效，提示重新预览后由用户再次确认，禁止自动重试；焦点测试 84/84、GUI build 通过。该改动仍未提交，双账号 GUI 复验为 **UNKNOWN**。见 `gui/src/features/org/impl/api/org-request-api.ts` 与 `src/hyprial/daemon/impl/forwarding/membership/removal.py`。 |
| 删除组织 | GUI 确认后 daemon 拒绝，组织未删。`OrgNetworkService.delete()` 通过权限检查后固定返回 `LAST_AUTHORITY_REFUSED`，不存在成功删除路径。 | GUI 禁用删除入口并说明原因。真正删除按 [#2883 Draft §5.3](https://code.hyprial.com/HyprialOS/hyprial/pulls/2883) 定义的组织生命周期、OrgFS tombstone、Tailnet authority 关停及持久化恢复设计与验收；保留历史，不直接 purge。当前方案没有 Casdoor group 删除步骤。 |

删除的前置工作按 #2883 Draft §5.3 核对 OrgFS 权威记录、Tailnet 最后 authority 的关停维护责任和失败恢复。删除保持 **BLOCKED**，不能仅移除拒绝代码。早期 D1 文档中的 Casdoor group 删除和 infra delete-group 端口不再是前置条件。

## 后端契约与前端适配的开发顺序

| 顺序 | 现状与缺口 | 建议交付与验收 |
| --- | --- | --- |
| 0. 现有列表适配 | `org.list` 已 HTTP 暴露，结果行已有 `kind/current`；GUI `live-source.ts` 仍把每行映射为 `work`，选择保存在本地。 | 先让 GUI 正确读取并显示 `kind/current`，补契约映射和焦点测试；区分“daemon 当前组织”和“浏览器当前查看组织”。此项无需新增列表接口。 |
| 1. Agent 归属只读 | `OrgMembershipStore.read_agents/read_agent` 与 `OrgMembershipProjection` 已有；投影明确区分 `ok` 与 `unavailable`。未找到相应的 Agent 归属查询 MethodSpec/HTTP。GUI 的 `detail.agents` 为 `null`，`agentOrgs` 为 `null`。 | 设计按组织列 Agent、按 Agent 列组织的只读方法；结果明确规范 Agent ID/URI、owner、审批/活跃状态、行版本，以及“已知为空”和“空间读不到”的不同语义。补授权、schema、OpenAPI/生成客户端、GUI 接线与隔离测试；读不到不得显示成空列表。 |
| 2. Agent 归属写入 | PR #2939 已提供 `org.agent.join/leave` 与 GUI 接线；daemon 中个人组织及 `AGENT_LAST_ORG` 规则是过渡兼容逻辑。 | 继续核对 owner 审批、版本冲突、幂等键、未知结果恢复与权限。按 #2883 W1f 允许 Agent 零归属并移除个人组织规则；当前错误契约随 daemon 行为变更调整。组织成员 Agent 目录按人的组织成员关系汇总，不要求每个 Agent 显式加入组织。 |
| 3. 当前组织同步 | `org.use/resolve` 已有本机 MethodSpec，但 `http_exposed=False`。 | 先由产品确认浏览器切换是否应改变 daemon 的当前组织。若需要，单独设计受会话和身份约束的调用边界、跨标签页一致性与失败反馈，再开放最小方法；若不需要，GUI 保持本地查看选择并明确不等于 daemon 当前组织。 |
| 4. 后续按具体旅程拆分 | `org.invites.list` 已 HTTP 暴露，但要求调用方传入账号邀请快照；`org.invites.accept` 仍为本机方法且需要 `inviteId/link`。`org.fetch` 只暂存配置候选。组织级配置发布、统计/拓扑、改名、模板和 Agent 迁移尚不能视为一组完整 GUI 公共流程。 | 分别确认数据权威、身份与产品旅程后立项。邀请先补安全的账号快照来源及领取边界；配置再设计 adopted/candidate 读与 validate/publish/adopt；统计、改名、模板、迁移各自定义独立契约和验收。已有 `org.invites.list` 不重复新建同名列表。 |

## 第一批建议范围

第一批可并行准备 GUI `kind/current` 适配与 Agent 归属只读契约；离开确认修复需先做真实双账号 GUI 回归。删除组织是单独的后端生命周期项目，按 #2883 §5.3 处理权威记录与 Tailnet 关停。Agent 显式归属写入与组织成员 Agent 目录分别验收；浏览器同步 daemon 当前组织依赖产品决策与安全边界，不应为赶 GUI 接线直接改 `http_exposed`。

每批交付保存固定 dev SHA、MethodSpec/HTTP 与生成客户端的差异、焦点测试结果、真实浏览器与真实后端操作证据，并列明未测项。写请求超时或响应不可校验时，UI 应提示结果未知并先读状态，不自动重复提交。

## 第一批实施记录

在 `fix/org-removal-confirmation-20261117` 工作树实施了以下内容：

- GUI 将 `REVISION_CONFLICT` 与预览过期统一处理，要求重新预览和人工确认；删除组织入口在后端仍固定拒绝时禁用。
- GUI 的组织列表读取后端 `kind/current`；浏览器已有选择仍优先保留，daemon 当前组织只用于初次本地选择。
- 新增 `org.agents.list`、`org.agent.orgs` 两条只读 MethodSpec/HTTP 方法，限制为本机 active 组织，保留 Agent ID、URI、owner、审批状态、版本，以及读不到空间时的 `unavailable` 语义。OpenAPI、生成客户端与同源 Contract Mock 示例随之更新。
- 正式 GUI 数据源读取组织 Agent 归属；选中组织明细和成员关联使用已知的结果。任一组织归属读不到时，跨组织 Agent 映射保持未知，不能显示为已知空列表。

真实双账号 GUI 离开确认回归：**UNKNOWN**。本轮未使用日常 daemon、用户凭据或生产环境；离开确认的焦点自动测试覆盖版本冲突后的重新预览与禁止自动重试，但不代替真实后端和浏览器验收。删除组织仍 **BLOCKED**，Agent 归属写入、owner 审批和浏览器同步 daemon 当前组织均不在本批实现范围。

验证（隔离工作树，未触碰日常 daemon）：

- `vitest` 组织领域 7 个文件：182 passed、4 todo（含“已知空”和“读失败未知”区分测试）。`npm run build` 通过。
- `pytest tests/daemon/org_membership tests/daemon/ipc/contracts/org tests/daemon/contract_mock/example_integration -q`：516 passed；golden manifest 7 passed。`ci-scope`/`ci-plan`：170 passed。
- 结构门、Python 模块边界门、GUI 请求路径门、GUI Mock 来源门、daemon request 架构门、OpenAPI 快照检查、生成客户端检查、Ruff 和 `git diff --check` 均通过。契约对 `dev@88564fc` 的差异为新增两个方法及 HTTP 路径，breaking 0。
- `pytest tests/` 全量曾因原生扩展缺失、继承 agent 会话变量失败；重建扩展并隔离这些变量后重新执行，因套件规模过大在 2% 处中止，**未取得全量结论**。该项和真实双账号浏览器复验须在后续集成门完成。
