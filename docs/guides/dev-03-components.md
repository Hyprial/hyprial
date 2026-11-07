# dev-03 前端组件接入与验收

本批次是受控前端组件，属于 **研发补齐，待产品规范设计（DEV-UI-003）**。正式 App 仍使用旧页面；不宣称聊天发送、飞书授权或消息历史已接通。接入由 work-developer 在共享入口和状态层完成，组件验收不能替代真实用户旅程验收。

## 组件与边界

| 组件 | 输入与意图 | 职责 |
| --- | --- | --- |
| `gui/src/pages/chat/ChatComposer.tsx` | `ChatComposerProps`：对象、草稿、能力、发送操作；`onDraftChange/onSend/onRetry` | 受控文本、IME/键盘、等待/失败/未知结果提示和动作门控 |
| `gui/src/pages/chat/ChatWorkspaceView.tsx` | `ChatWorkspaceViewProps`：`SessionTarget[]`、活跃对象、`ConversationMessage[]`、历史状态、草稿、信息展开与回调 | 列表、消息、输入、信息；按对象滚动和信息关闭焦点返回 |
| `gui/src/pages/channels/LarkChannelView.tsx` | `LarkChannelViewProps`：选择、步骤、授权/切换能力与操作、当前对象的授权/连接结果、意图回调 | 选择 Agent → 授权 → 切换渠道 → 结果的受控呈现 |

TypeScript 源文件导出的 props 是精确视图接口。这些类型不是 HTTP/daemon 契约，不能据此新增后端方法。没有新增包、状态仓库、网络客户端、OAuth URL、凭据输入或浏览器持久存储。单元测试/预览里的模拟数据仅存在于 `gui/tests/dev-03/`。

### 草稿和操作控制器

- 草稿始终由父控制器提供。组件不因提交、失败、未知或成功投影自行清空文本；失败保留草稿是前端行为，不需要后端草稿服务。
- 父控制器按对象保存草稿。只有正式合同确认受理、且草稿仍是提交时的修订时才清空；旧结果不能清掉用户后续编辑。此共享状态改造未包含在本批。
- `onSend` 提交对象和文本；`onRetry` 只提交原操作 ID。控制器接受意图后必须同步投影 `in-flight`，并在自身层面阻止跨卸载/重挂载的重复写入。组件不维护第二份操作账本。
- 能力缺失、未确认或不可用时不触发业务回调；仍可输入和保留草稿。空对象不可输入。等待/未知禁止发送；明确失败只在 `canRetry` 为真时允许重试。新发送需控制器先重置旧失败操作。
- 操作和历史只作用于匹配的对象；无匹配对象显示空态。消息来自 props，组件不生成消息 ID、回复或送达凭据。
- `success` 是控制器声明的操作结果，不意味着 Agent 已回复或对方已读。消息送达文案另由 `ConversationMessage.delivery` 控制。

### 飞书流程

- `step` 和 `selectedAgentId` 由父级控制，返回只发步骤意图，不清空选择。授权和切换分别提供能力与操作状态。
- 授权状态、操作、渠道结果必须匹配当前 Agent；旧对象结果不能让新对象显示已连接。
- 未授权、授权过期或撤销显示未连接；授权成功本身不代表渠道切换成功。已连接必须同时有当前对象已授权和匹配的连接结果。
- 未知结果禁止重新写入；明确失败重试需显式授权及原 requestId。凭据、可信身份、授权回调、实际渠道绑定交给统一运行时和业务实现。

### 样式与挂载

使用公共 `tokens.css`、`primitives.css`；新组件仅用既有 token。工作区和飞书视图各自导入作用域 CSS。单独使用 ChatComposer 时同时导入 `chat/chat.css`。

聊天宿主提供确定高度，内部管理消息区滚动；窄屏采用单列布局。信息区关闭后焦点回到“信息”按钮。可选 `onManageTarget` 未提供时管理按钮禁用；本批不接管 Agent 管理。

共享 App/AppShell、原 ChatPage、ConnectionsPage 拆分、state/reducer/selectors、adapters/runtime、全局样式和共用测试均未修改。迁移编号 DEV-UI-003：集成人挂载新视图、接入统一控制器并完成正式旅程验收后，旧聊天实现才能退场；不长期维护两个生产入口。

## 开发预览

按当前 dev-mode 文档，在指定 worktree 启动，不使用自动创建工作树：

```bash
CHOKIDAR_USEPOLLING=1 CHOKIDAR_INTERVAL=1000 \
PATH=/home/lenovo/.nvm/versions/node/v24.20.0/bin:$PATH \
hyprial gui dev \
  --gui-home /mnt/d/workspace/hyprial/harness-bridge/.worktrees/dev-03/gui \
  --ip ::1 --port 4174 --no-open --json
```

核对输出 `git_branch=feat/gui-dev-03`。正式根页面仍展示公共基线；本批独立组件验收地址：

```text
http://[::1]:4174/tests/dev-03/preview/index.html
```

该测试 HTML 使用同一份生产组件，未建立生产路由，也未修改 Vite 入口。顶部始终标明“模拟数据 · 未接后端”；切换“模拟状态”只是输入测试 props。默认不可用，操作意图计数应为零。测试 fixture 不被生产入口引用。

本机 WSL 的 127.0.0.1 曾被路由到 `loopback0` 而非本地 `lo`，出现服务已监听但 Linux 浏览器连接拒绝，因此当前预览使用 IPv6 回环地址。已在真实工作树服务上通过 Chromium 打开上述页面并确认无运行时错误；Windows 浏览器可达性尚未单独验证。隔离网络命名空间用于自动化测试，没有修改主机网络路由。

2026-10-29 实际操作验收发现：该挂载盘上长期运行的 Vite 服务曾返回旧的缓存模块，虽然工作树已包含修复。上面的轮询环境变量仅作用于本次预览进程，避免依赖挂载盘的文件变更事件；无需改共享 Vite 配置。重启后已验证服务返回最新模块，并用临时模块变更确认缓存会更新。若页面仍显示旧文案，核对服务返回的模块和工作树，而不只看健康检查。

## 验证方式

```bash
cd gui
npm run typecheck
npx vitest run tests/dev-03 --maxWorkers=1
DEV03_CHROMIUM_PATH=/实际/Chromium可执行文件 \
  node --test --test-concurrency=1 tests/dev-03/chat-browser.mjs
npm run verify
npm run verify:production-copy
npm run verify:safe-r1-build
npm run verify:safe-r1-browser
```

私有浏览器测试覆盖 1440/760/375、根横向溢出、桌面列布局、信息关闭按钮中心命中和真实点击、焦点返回、草稿切换、未知禁止发送、飞书步骤和未接线零请求。截图默认写到 `/tmp/dev03-browser-evidence`，可设置 `DEV03_EVIDENCE_DIR`。

本机 `/mnt/d` 是 9p，曾导致 Vitest worker 启动超时。验证使用 ext4 临时副本时必须复制源码、原 package-lock 与配置，按锁安装依赖，并逐字节核对测试对象；报告明确区分工作树执行失败与一致副本通过。不能更改测试配置或放宽断言来掩盖失败。

共享浏览器门禁写死 macOS Chrome 路径，当前 Linux 主机不具备该路径；私有 Chromium 测试不能替代这两项门禁。最终实测结果、截图与通知凭据见 [实施计划与进度](../design/dev-03-frontend-plan.md)。
