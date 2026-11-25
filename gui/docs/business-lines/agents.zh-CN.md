# agents — Agent 创建与管理

业务线：Agent 创建、列表、搜索/筛选、详情配置、启动/停止、开始对话。
原负责人：林懿伦；当前集成分支 `intg/gui-agent-journey`。

当前读写能力、方法缺口和验收边界只维护在 [方法对账](../../../docs/design/gui-v0.1-method-plan-2026-11-13.md)。
执行顺序见 [TODO](../../../docs/design/gui-v0.1-lifecycle-todo.md)，术语见 [GLOSSARY](../../../GLOSSARY.md)。
本文件只定义业务范围和组件入口，不另存方法状态表。

## 组件与数据

- `pages/ContactsPage.tsx`、`features/agents/impl/`：创建、目录、配置和管理动作；共享展示，不复制预览页。
- `features/agent-provider/`：Key/Auth 共享清单与空状态；原生 Auth 读能力独立注入。
- `agent.list.runState` 是运行状态来源，凭据/平台在线不替代运行观测；筛选在目录结果上进行。
- 创建只写记录及凭据；配置模型后显式启动。点击“开始对话”才打开聊天，真实已有会话仍读后端。
- 原生 Auth 属于对应 Agent/harness；沿用/替换/移除的逻辑与方法见对账，不再维护第二份兼容表。

## fixtures

当前旅程使用 `src/dev/journey/` 的共享内存状态；历史场景保留在 `src/dev/fixtures/agents/` 与归档选择器。
测试辅助位于 `tests/fixtures/agents/`。创建、删除、启动、凭据、平台绑定与首页计数需从同一状态更新，
模拟运行不得标为真实认证通过；正式缺读能力时不注入 mock。
