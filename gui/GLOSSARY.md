# GUI 术语表

本表只解释 `gui/` 中的界面组合与接线术语。平台身份、agent、host、thread、session、
adapter、daemon 等产品术语以仓库根目录的 [GLOSSARY.md](../GLOSSARY.md) 为准；
这里的“GUI 适配器”特指前端能力转换层，不改写根术语表中外部平台 adapter 的定义。
现状以 [当前架构](docs/architecture.md) 标注的基线提交为准。

| 术语 | 在 GUI 中的含义 | 容易混淆的边界 | 源码或说明 |
| --- | --- | --- | --- |
| 共享 GUI | `gui/` 中由 React、TypeScript 和 Vite 构建的页面、组件与样式主线 | Web、桌面和移动复用的目标不等于三个平台均已交付 | [package.json](package.json)、[设计规范](docs/ui-development-standard.zh-CN.md) |
| 产品入口 | 普通浏览器入口，先建立受信后端会话，再渲染 `App` | 不能把开发预览当作产品入口 | [main.tsx](src/main.tsx) |
| 开发预览 | 显式选择的组件与场景预览入口 | fixture 是演示数据，不代表 daemon 的状态 | [main.tsx](src/main.tsx)、[预览指南](docs/dev-mode-user-guide.zh-CN.md) |
| 共享视图 | 预览与正式入口共同使用的页面和展示组件 | 可以更换数据/操作适配器，不复制另一套页面 | [共享入口映射](docs/shared-ui-entry.zh-CN.md) |
| App 组合层 | 正式 `App` 组合 connected/live/unavailable；开发 `FixtureApp` 组合显式 fixture，并复用共享壳 | 页面不自行选驱动、凭据或 endpoint | [App.tsx](src/App.tsx)、[FixtureApp.tsx](src/dev/FixtureApp.tsx) |
| GUI capability | 注入页面的类型化读取、写入或状态能力 | “有组件”不表示能力已接通；缺能力应显示不可用 | [capabilities.tsx](src/app/capabilities.tsx) |
| GUI 适配器 | 把业务能力的输入、结果和错误转成视图所需模型 | 不拥有 daemon 权威，也不等同于平台 adapter | [features/](src/features/)、[adapters/](src/adapters/) |
| feature 公共入口 | 每个业务模块的 `index.ts`，向其他模块公开可消费的类型和能力 | 跨模块不穿透 `impl/`；实现细节可独立调整 | [模块边界](docs/module-boundaries.md) |
| 同源宿主 | 提供 GUI 资源、浏览器会话与 `/daemon/request/<method>` 代理的受信进程（Python 会话 host，`src/hyprial/shell/impl/gui/host`） | 浏览器不能选择上游地址、身份或 bearer | [会话契约](../contract/gui-session/README.md)、[GUI README](README.md) |
| backend driver | 宿主选择的 `daemon` 或 `contract-mock` 上游 | 选择权不在页面、query 参数或 feature 内 | [会话契约](../contract/gui-session/README.md) |
| Contract Mock | 使用契约样例的隔离上游驱动，走普通产品接线 | 不等于前端 fixture，也不证明真实 daemon 可用 | [GUI README](README.md) |
| browser bootstrap | 页面打开时用一次性授权建立、恢复或刷新浏览器会话 | 未建立会话时显示失败页，不回退为演示数据 | [browser-session.ts](src/app/backend/browser-session.ts)、[main.tsx](src/main.tsx) |
| launch grant | 启动器交给浏览器的一次性授权值 | 只从 URL fragment 读取并立即移除；不作为业务 bearer | [browser-session.ts](src/app/backend/browser-session.ts) |
| view proof | 浏览器会话跨刷新保留的独立视图证明 | 不能替代宿主保存的上游 bearer | [browser-session.ts](src/app/backend/browser-session.ts) |
| generation | 一次受信会话与传输的代际标识 | 旧代连接退役后不能继续发新请求 | [connection.ts](src/app/backend/connection.ts) |
| BackendConnection | 单代会话注入的描述、方法可用性与 generated client | 描述元数据本身不授予身份或启动宿主 | [types.ts](src/app/backend/types.ts)、[connection.ts](src/app/backend/connection.ts) |
| generated client | 从 HTTP 方法投影生成的类型化调用面 | 方法定义来自后端契约，不能由页面自创 | [generated/](src/client/generated/)、[HTTP 客户端说明](docs/http-request-client.md) |
| method access | 根据宿主目录及已验证写方法判断某方法能否调用 | 不可用不应被 fixture 成功值覆盖 | [connection.ts](src/app/backend/connection.ts) |
| dispatch unknown | 请求可能已到达上游，但响应不足以确认结果 | 不是失败回滚，也不能自动重发写请求 | [transport.ts](src/client/transport.ts)、[决策记录](docs/architecture-decisions.md) |

业务概念在页面里出现时，仍沿用根术语表：例如聊天通道称 **thread**，
agent 进程的运行会话才称 **session**。历史源码或 API 字段若仍使用旧词，
文档应标明是兼容命名，不从字段名推导新的产品定义。
