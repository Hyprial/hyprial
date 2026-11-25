# GUI 当前架构

事实基线：`origin/dev` 的 `b144d854a755c22e440d480af2b624b93be4af44`（2026-11-10）。
本文是该提交的源码导览，记录已存在的组合与边界；后续提交应重新核对路径和能力状态。
术语见 [GUI 术语表](../GLOSSARY.md)，决策依据见 [架构决策记录](architecture-decisions.md)。

## 入口与请求链

```text
启动器 / 同源宿主（选 daemon 或 contract-mock，保管上游凭据）
  ├─ 提供同一份 GUI 资源与浏览器会话
  └─ 转发 /daemon/request/<method> 到所选上游
浏览器 main.tsx
  → browser bootstrap（grant / view proof / generation）
  → 正式 App 组合层（一个 BackendConnection）
  → GuiCapabilities → feature 适配器 → 共享页面和组件
  → generated client → HttpTransport → 同源宿主 → 所选上游（daemon 或 Contract Mock）
```

普通入口由 [main.tsx](../src/main.tsx) 调用
[connectBrowserBackend](../src/app/backend/browser-session.ts)；建立连接失败时显示
`BootstrapUnavailable`，不会注入 fixture。宿主在
Python 会话 host（`src/hyprial/shell/impl/gui/host`，由 `hyprial gui` 启动）中运行，
选择 `daemon` 或 `contract-mock` 驱动并限制监听地址；`hyprial gui dev` 时它反向代理
[serve.mjs](../scripts/serve.mjs) 启动的 Vite dev server。浏览器只访问同源端点；上游 endpoint、bearer 与受信
principal 留在宿主。`contract-mock` 是宿主上游驱动，前端 fixture 则只用于显式预览或测试。

浏览器会话把一次性 launch grant 从 URL fragment 取出并移除；刷新时保留独立
view proof，更新会话 generation。连接由
[connection.ts](../src/app/backend/connection.ts) 构建：一代连接只暴露一个
generated client；目录缺失或未准入的写方法在发送前返回不可用。旧代连接退役后拒绝新请求。

## 前端职责分布

| 路径 | 当前职责 |
| --- | --- |
| [`src/main.tsx`](../src/main.tsx) | 选择普通入口或显式开发预览；普通入口先完成 bootstrap |
| [`src/App.tsx`](../src/App.tsx)、[`src/app/`](../src/app/) | 组合连接、能力、状态与页面；不由页面自行选择上游 |
| [`src/pages/`](../src/pages/)、[`src/components/`](../src/components/) | 共享页面、导航、表单和展示组件；预览与正式复用视图 |
| [`src/features/`](../src/features/) | 业务展示模型和薄适配器；各模块用 `index.ts` 公开接口、`impl/` 隐藏实现 |
| [`src/client/`](../src/client/) | generated client、HTTP transport、请求诊断与错误分类 |
| [`src/dev/`](../src/dev/) | 显式场景、演示数据和组件审阅；不是产品后端兜底 |
| [`src/styles/`](../src/styles/) | 应用样式、共享 token 与 primitives 样式 |
| [`scripts/`](../scripts/) | Vite 开发服务器（不承载数据）、客户端生成和产物检查 |

[AppShell](../src/app/AppShell.tsx) 按当前页组合页面；业务能力由
[capabilities.tsx](../src/app/capabilities.tsx) 从同一连接生成。正式
[App](../src/App.tsx) 只组合 connected/live/unavailable，显式开发场景由
[FixtureApp](../src/dev/FixtureApp.tsx) 组合，并复用同一共享壳和页面。
开发场景与受信后端可提供不同数据源和控制器，具体映射见
[共享入口说明](shared-ui-entry.zh-CN.md)。
跨 feature 只消费公共 `index.ts`；正式源码不反向依赖开发组合或测试。
具体规则、静态门与当前例外见 [模块边界检查](module-boundaries.md)。

## 状态与权威边界

| 情况 | GUI 当前处理原则 |
| --- | --- |
| 后端未建立浏览器会话 | 显示 bootstrap 失败页；不显示 fixture 成功状态 |
| 宿主目录没有方法或写方法未获准入 | 在调用前显示明确不可用；不选择另一个隐式通道 |
| 读取失败或资料尚未确认 | 保留 loading/error/unknown 与空列表的区别 |
| 写请求超时、断线或响应不完整 | 保留 `dispatch=unknown`，不自动重发、不声称回滚 |
| 显式开发预览 | 注入标记为 fixture 的数据；不据此宣称真实业务已接线 |

这些原则由 [browser-session.ts](../src/app/backend/browser-session.ts)、
[connection.ts](../src/app/backend/connection.ts)、
[transport.ts](../src/client/transport.ts) 和各 feature 的适配器实现。
具体业务是否可用还取决于目标版本的 MethodSpec、宿主目录、身份和运行证据；
目录存在、构建通过或页面可见均不能单独证明真实后端写入成功。

## 当前交付边界

当前普通入口已装配受信连接和若干 Agent、组织、设置与消息能力；
每项仍由宿主目录和已验证写方法逐一决定可用性。
[capabilities.tsx](../src/app/capabilities.tsx) 默认关闭首次设置向导；
登录、设备初始化与完整首次旅程不能由 fixture 测试推定完成。
原生壳、移动正式认证与发行包也不由此源码导览宣称验收。
更细的页面现状见 [共享入口映射](shared-ui-entry.zh-CN.md)，
启动与打包边界见 [GUI README](../README.md) 和
[仓库开发模式指南](../../docs/guides/gui-dev-mode.md)。
