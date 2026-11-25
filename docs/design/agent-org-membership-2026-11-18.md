# Agent 与组织归属写入设计（2026-11-18，2026-11-19 修订）

## 当前边界

Agent 由人拥有，可以没有显式组织归属。`agent.create` 不接受组织参数；GUI 创建表单不提供组织选择。创建、启动和本机 Agent 列表不应以组织归属成功为前提。OrgFS `agents/<owner>/<agentId>.json` 是显式 Agent 归属的权威记录，Agent registry 不缓存可写组织字段。零归属是合法状态，不是需要自动修复的异常。

组织成员 Agent 列表从现有 `targets` 的 `orgs` 投影读取：daemon 按组织的人类成员关系汇总成员发布的 Agent，不要求这些 Agent 逐个加入该组织。`orgDirectoryStatus` 表示各组织清单是否完整。本轮不暴露独立的 Agent 组织归属读写 IPC；列表可见性不赋予 Agent 组织级操作权限。参见 [组织成员 Agent 目录](org-member-agent-directory-2026-11-18.md)。

## 显式归属写入

显式 Agent 组织归属的写入流程保留为后续设计，不属于本轮聊天目标目录。当前 GUI 不调用 Agent 组织归属写方法；成员组织由人的组织成员关系确定。

PR #2939 当前 daemon 仍有“个人组织只能容纳 owner 的 Agent、不能移出”及 `AGENT_LAST_ORG` 拒绝移出最后归属的兼容逻辑。这是待 [#2883 Draft](https://code.hyprial.com/HyprialOS/hyprial/pulls/2883) W1f 调整的过渡限制，不是产品长期不变量。GUI 不预先宣称 Agent 必须属于一个组织；只有当前 daemon 实际返回 `AGENT_LAST_ORG` 时才说明当前版本暂不支持该操作。公共错误契约在 daemon 行为变更时同步调整。

目标方案按 #2883 J10/J13：取消个人组织；Agent 可以零归属；如果将来恢复创建时组织选择，顺序为显式选择、当前组织、无组织。当前 PR 的创建流程没有这个选择，不能把旧的“默认个人组织”写进 UI 或文档。

## 验证与未完成项

自动测试覆盖 Agent 组织查询、加入/移出、CAS 与未知结果、GUI 零归属展示和组织成员目录。组织成员目录另有真实双账号 A1/B3 的 daemon/Tailcat/OrgFS 证据；完整浏览器操作及离线、离开后的收敛仍需单独验收。跨 owner 审批协议、host-invite 创建入口、运行中实例归属回填和 #2883 W1f 的 daemon 行为变更不在本次过渡修订中。
