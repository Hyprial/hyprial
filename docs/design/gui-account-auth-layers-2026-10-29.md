# GUI 真实账号登录：调用分层与集成交接

决策：GUI-AUTH-LAYERS-20261029。用户已确认复用现有认证能力；这不是重新定义业务 HTTP 契约。

## 职责

| 目录 | 职责 | 不承担 |
|---|---|---|
| `gui/src/features/dev01/onboarding/` | V17 账号入口、公开授权进度、取消/重试、账号和设备独立状态 | OIDC、令牌持久化、可信 owner 推导 |
| `gui/src/platform/auth/` | 页面调用接口、Web/原生能力适配 | 业务后端协议和凭证管理 |
| `gui/server/auth/` | Web 本机受限桥、进程与请求生命周期、公开输出过滤 | 任意命令执行、重写认证算法 |
| `clients/native/` | 桌面命令桥和包内运行时选择，独立实例 home | 自动调用系统安装的 CLI |
| `src/hyprial/shell/` | 既有登录、凭证保存、设备初始化的权威实现 | 本次不修改 |

Web 使用启动者配置的当前 CLI 实例，桌面使用包内独立实例。不得从浏览器参数选择 Python、home、owner 或命令。后台调用公开的 `hyprial.shell.desktop_network`，不跨域导入 impl、不复制 OIDC。

## 页面行为

账号设置改为点击官方浏览器授权，不再要求用户粘贴访问令牌。进入页面先读状态，不能自动发起登录。当前已有账号时恢复显示；账号已保存但设备失败时仅重试设备。取消关闭本次等待并忽略迟到结果，不删除已经保存的账号。

本地保存状态仅表示现有凭证与配置符合实现的检查条件，不证明远端 token 尚未失效。账号、设备、网络、模型、Squire 和聊天状态不合并为一个“已连接”。登录接线完成也不代表模型和聊天完成。

## Web 启用和安全边界

默认 `npm run dev` 不启用本机认证。由可信启动端显式设置：

```sh
export HYPRIAL_GUI_AUTH=local
export HYPRIAL_GUI_AUTH_PYTHON=/absolute/path/to/hyprial-runtime/bin/python
export HYPRIAL_HOME=/absolute/path/to/the-selected-hyprial-home
npm --prefix gui run dev -- --host 127.0.0.1 --port 4175
```

解释器必须已安装 Hyprial，并提供上述公开 API。路径由部署端确定，不建议用户猜路径或新装另一份环境。暂不修改公共 CLI launcher；共享集成人后续衔接启动参数。默认演示页面仍保持隔离，不因服务环境开启而自动登录。

开发验收页面为 `/src/features/dev01/onboarding/preview.html?auth=local`（Vite 开发模式）。不带 `auth=local` 保留演示模式。该私有页面尚非正式 AppShell 首次设置入口；生产构建的页面路由由共享集成人接入。

本机桥只监听回环服务并检查 Host、Origin、JSON 请求与进程内随机请求标识；只接受固定认证动作。子进程输出只转发白名单公开字段，不回传原始 stdout/stderr、环境变量或认证凭证。并发登录、超时、取消及服务关闭必须有界收尾。

这不是远程移动认证入口，不对 tailnet 或公网开放。后续统一 HTTP 服务就绪后替换平台适配，页面不依赖本临时传输细节。

## 原生接续

Mini 迁移线负责核查已有 `account_invoke` 和公开进度事件，接入同一页面接口。要覆盖包内解释器路径、独立 home、已登录恢复、设备失败不再登录、关闭窗口回收等待进程。原生能力不存在时明确 unavailable，不自动降级使用外部 CLI。

## 集成与验收

dev01 页面与共享服务接线分别提交，目标由 `work-developer` 按团队 `intg/gui-init` 最新基线收拢。原生改动进入 `intg/tauri-pack`；如发现公共登录缺陷，单独提后端 PR。

本地自动化使用隔离 home 和假 runner，不读生产凭证。真实认证还须用户在浏览器完成授权，验收账号与设备真实状态、取消/超时/刷新恢复；测试成功不等于生产登录已经完成。视觉与流程差异同步瑞华：删除手填令牌，增加官方授权等待/取消/重试，保留账号和设备分开显示。

验证入口：

```sh
npm --prefix gui run typecheck
npm --prefix gui run test -- tests/auth-client.test.ts tests/dev01-auth-actions.test.ts tests/dev01-onboarding.test.tsx
node --test gui/tests/auth-host.node.mjs gui/tests/auth-server-integration.node.mjs
uv run --no-project --python 3.12 python gui/tests/auth-worker.test.py
node gui/tests/auth-browser.mjs
npm --prefix gui run build
```

`auth-browser.mjs` 使用注入的假认证权威，外部 HTTPS 全部拦截，检查浏览器→Web 服务→适配器的接线、弹窗和刷新恢复；不声称真实 OIDC 验收。原生客户端适配当前仅提供注入接口，须待 Mini 的实际 Tauri 桥接验证。已有凭证远端失效后的强制重认证/账号切换未纳入本次首次设置入口，后续账号设置单独接入，不能通过删除凭证来代替。
