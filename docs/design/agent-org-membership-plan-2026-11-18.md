# Agent 组织归属实施步骤（2026-11-18）

> 历史计划。2026-11-20 的当前范围只使用现有 `targets` 提供同组织 Agent 聊天目标；本计划中的 Agent 显式归属 IPC 未公开。

1. **目录与组织只读键对齐。** 用 `agent.list.agentId` 对接 `org.agents.list.agentId`，保留 actor 作为路由/操作键；GUI 组织筛选和标签按 canonical `agentId` 查归属。先写失败测试，再改适配层并执行聚焦测试。
2. **已有 Agent 的安全写契约。** 在 membership service 增加加入/移出，校验 Agent 实体、当前有效组织、审批者与个人组织限制；以 OrgFS 行版本 CAS 写入，拒绝最后归属移除。增加 MethodSpec、声明错误和 HTTP 安全边界，测试权限、并发与未知读取。
3. **创建归属编排。** 将显式/current/personal 默认解析放在 daemon 创建编排层，新增 durable journal 记录创建阶段；reuse 保留现有归属，覆盖 CLI、start、PAC 与 host-invite 入口。逐入口补失败/恢复测试。原计划的“归属持久化成功后才允许运行”门槛已按 2026-11-18 手测反馈撤回，见设计文档的后续决定。
4. **GUI 写入与联调。** 生成客户端，生产网关接 `joinAgentOrg`/`leaveAgentOrg`；把新建表单组织字段传入创建契约；明确审批中、冲突、未知结果与读回状态。用隔离环境和已有 Agent `123` 验证。
5. **收尾。** 执行 Python、GUI、契约聚焦验证；核对差异和文档。提交、推送、重启需按 doer 权限另获授权。

每步以失败测试确认缺口，代码变更后运行对应测试并记录结果。写契约依赖审批/网络模型，不以 GUI 本地状态冒充已批准。
