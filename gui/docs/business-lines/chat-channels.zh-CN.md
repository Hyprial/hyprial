# chat-channels — 聊天与平台接入

业务线：聊天目录、消息历史、发送及未知结果；平台接入生命周期与 Agent 消息路由。
原负责人：李震宇；当前集成分支 `intg/gui-agent-journey`。

当前读写能力、方法缺口和验收边界只维护在 [方法对账](../../../docs/design/gui-v0.1-method-plan-2026-11-13.md)。
执行顺序见 [TODO](../../../docs/design/gui-v0.1-lifecycle-todo.md)，术语见 [GLOSSARY](../../../GLOSSARY.md)。
本文件只定义业务范围和组件入口，不另存方法状态表。

## 页面与组件

- `features/chat/`、`features/agent-chat/`、`features/chat-channels/`：共享聊天展示、控制器和正式适配。
- `features/connections/PlatformIntegrationsSection.tsx`、`PlatformIntegrationDetail.tsx`：平台接入列表/详情。
- `features/agents/impl/detail/PlatformBindingsTab.tsx`：Agent 的平台绑定与消息路由入口。
- 不再用“应用内/用飞书沟通”开关；历史、目标可见性与回复归路依后端事实，不意味着当前 HTTP 已支持 route 发送。
- 平台接入管理 adapter；平台绑定是 Agent↔adapter 的 pin；channel 仍指协议层群组会话空间。

## 边界与 fixtures

`message.ack/reply/read_body/pending/settle_hint` 属于 harness consumer-plane，不能为补目录直接开放浏览器。
GUI 历史走已准入读取，未知发送只查询原 operation key，不自动重发。
当前平台 mock 在 `src/dev/mock/platform-integrations.ts`，连续旅程状态在 `src/dev/journey/`；
旧聊天契约场景和测试 fixture 保留，全部复用现行页面，不冻结一份旧 UI。
