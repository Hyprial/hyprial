# GUI 架构决策记录

本页汇总 `origin/dev` 提交 `b144d854a755c22e440d480af2b624b93be4af44`
已采用的 GUI 决策，不新设产品协议或替代仓库级 ADR。每条给出原因、当前落实路径与代价；
后续改变决策时应在相应源码、契约和本页同步更新。旧计划所写的“目标”需经当前源码与
测试核实后才可改成“已实现”。

## 1. 一套页面，按入口注入能力

**决定：** 普通入口与显式开发预览复用页面、组件和样式；入口只改变数据、控制器与能力注入。
**原因：** 双份页面会让预览验收和产品行为分叉。
**落实：** [main.tsx](../src/main.tsx) 选择入口，正式
[App.tsx](../src/App.tsx) 与开发 [FixtureApp.tsx](../src/dev/FixtureApp.tsx)
使用同一 [AppShell.tsx](../src/app/AppShell.tsx)；
[共享入口映射](shared-ui-entry.zh-CN.md) 列出各业务视图。
**代价：** 每个新状态需在正式能力和预览场景分别检查，不能用一套静态截图代替交互验收。

## 2. 宿主持有身份与上游选择

**决定：** 启动宿主选择 `daemon` 或 `contract-mock`，持有上游 endpoint、bearer 与受信
principal；浏览器只持有受限的同源会话证明。
**原因：** 页面参数与浏览器存储不能成为选择上游或声明可信身份的权威。
**落实：** Python 会话 host（`src/hyprial/shell/impl/gui/host`，契约见
[gui-session](../../contract/gui-session/README.md)）与
[browser-session.ts](../src/app/backend/browser-session.ts)；一次性 launch grant 从 URL fragment
消费并移除，刷新使用独立 view proof。
**代价：** 无受信启动器会话时普通页面明确失败，不能用 fixture 自动恢复。

## 3. 一代连接共用一个客户端

**决定：** 一次浏览器会话 generation 只装配一个 `BackendConnection` 与 generated client，
业务线从该连接取得窄能力，不分别建立 transport。
**原因：** 多个私有通道会产生不同的身份、目录与错误语义。
**落实：** [connection.ts](../src/app/backend/connection.ts)、
[capabilities.tsx](../src/app/capabilities.tsx)、
[generated client](../src/client/generated/) 和 [HTTP transport](../src/client/transport.ts)。
**代价：** 公共目录或连接代际变化时，所有业务能力都要按同一代际重新评估；退役连接拒绝新调用。

## 4. 能力可用性由目录和受信会话确认

**决定：** 方法必须在宿主提供的目录里可调用；写方法还要在已验证会话中获准入。
未准入时返回明确的不可用状态，不用演示数据掩盖缺口。
**原因：** 展示组件存在不等于后端协议、权限和业务结果已就绪。
**落实：** [connection.ts](../src/app/backend/connection.ts) 的 `methodAccess`，
[capabilities.tsx](../src/app/capabilities.tsx) 的能力组合。
**代价：** 页面需保留 unavailable、unknown 与空状态的区别；接线结果依赖运行时目录。

## 5. 预览数据只在显式场景使用

**决定：** fixture 用于组件与交互审阅；普通产品入口不在连接失败时回退到 fixture。
Contract Mock 则是由宿主显式选择、走同一请求链的隔离上游。
**原因：** 模拟成功不能证明真实登录、授权、消息投递或写入。
**落实：** [main.tsx](../src/main.tsx)、[App.tsx](../src/App.tsx)、
[开发预览说明](dev-mode-user-guide.zh-CN.md) 和 [GUI README](../README.md)。
**代价：** 没有后端时普通入口只能呈现真实的不可用状态；演示与真实验收需分开记录。

## 6. 写入结果未知时保留不确定性

**决定：** 请求可能已经发出而响应中断时保留 `dispatch=unknown`；不自动重发写请求，
也不宣称失败已回滚。
**原因：** 网络失败无法证明上游没有执行操作，自动重试可能重复发送或创建。
**落实：** [transport.ts](../src/client/transport.ts) 的单次尝试与错误诊断，
各业务适配器按方法契约处理确认或恢复。
**代价：** 界面需要向用户保留原请求与未知结果，并在有相应查询/回执方法时确认。

## 7. 业务模块用公共入口隔离实现

**决定：** `src/features/<module>/index.ts` 是跨模块公共面，内部实现放在 `impl/`；
正式源码不导入 `src/dev/` 或测试源码，基础组件和 client/platform 层不反向依赖业务模块。
**原因：** 让调用方只依赖稳定接口，避免 fixture、业务实现和底层传输互相穿透。
**落实：** [模块边界检查](module-boundaries.md)、
[模块结构设计](../../docs/design/gui-deep-module-structure-2026-11-08.md) 与
[check-module-boundaries.mjs](../scripts/check-module-boundaries.mjs)。
**代价：** 增加或迁移模块时要维护公共导出，并通过静态边界门；该门不证明运行时行为。

仓库现有的 [GUI 设计与研发规范](ui-development-standard.zh-CN.md)、
[根目录术语表](../../GLOSSARY.md) 与 [GUI 接线规划](../../docs/design/gui-live-0554/00-architecture.md)
提供上下文；规划中的终态不能代替本文所列源码证据。
