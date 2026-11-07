# dev-01 日常账号、设备与模型配置（前端私有区）

- 任务：PAC `wf-ad7e98d344714b00beaa87539acfebae` / node `settings`
- request：`workflow-request:29e9487ebef8471d95fb49c969511e32`
- worker：`local-linux-worker`（operator 调度）
- 分支：`feat/gui-dev01-settings-20261028`
- worktree：`/home/chloemartin/HyprialOS/.worktrees/gui-dev01-settings-20261028`
- 基线：`e703c6690b3bbcd16f558cb7293503d3c4208bb2`
- 适用决策：`GUI-DEV01-20261028`、`GUI-CONTRACT-CANCEL-20261028`、
  `GUI-COMPONENT-CONVERGENCE-20261026`、`ADR-GUI-003/004/005/006`

## 1. 输入审计与缺失项

已读取：

- 根 `<MEMBER>-GUI-WORKFLOW.md` 当前覆盖段；
- 本 worktree `AGENTS.md`；
- `docs/design/gui-init-tracks.md` 的 dev-01 条目；
- 现有 `SettingsPage`、`FirstRunPage`、`ConnectionsPage`、共享 primitives、
  `workspace-pages.css`、`useStubAction`/stub adapter 和现有测试。

交接引用的以下文件在基线 tree 中**不存在**，不能声称已读取；本次没有据此虚构
接口或视觉规范：

- `docs/design/module-boundaries-r3.md`
- `docs/design/product-handoff-r3.md`
- `docs/design/shared-file-ownership-r1.md`

因此本次只执行 `gui-init-tracks.md` 和现有组件能够确定的无歧义部分；缺失的设计
输入作为 operator 侧待补项记录在本文件末尾。

## 2. 改动范围

独占写入：

- `gui/src/pages/SettingsPage.tsx`
- `gui/src/features/dev01/forms/`（新增）
- `gui/src/features/dev01/model/`（新增）
- `gui/tests/dev01-settings.test.tsx`（新增）
- `gui/tests/dev01-model.test.tsx`（新增）
- 本文件

未修改：

- `src/hyprial`、`contract`、`.forgejo`、根 `scripts`；
- `gui/package.json`、依赖、全局 CSS、`App`、`AppShell`、共享状态、
  adapters、runtime、共享测试；
- `ConnectionsPage.tsx`、`FirstRunPage.tsx`；
- 瑞华 v17 原件或任何 vendor HTML。

验证时用 `npm ci` 按现有 lockfile 安装了 `gui/node_modules`；没有改变
`package.json`/`package-lock.json`，没有新增依赖。

## 3. 设置页

`SettingsPage` 现在只包含三个区域：

1. **账号**：账号状态、日常登录表单、失败提示和重试、取消。
2. **当前设备**：设备状态、设备标识（未知时显示 `未提供`）、实例隔离说明。
3. **普通偏好**：偏好能力尚未接入；不提供会造成“已保存”误解的开关或存储写入。

明确移除：

- 模型服务表单；
- Squire 创建/启动；
- “打开首次设置”入口；
- 任何虚构的已登录、已初始化、已保存或在线状态。

设置页保留现有的 `account.login` not-wired placeholder 调用路径，用于与当前
共享 adapter 的失败/重试状态衔接。这条路径不是真实账号后端；测试中使用
`RecordingStub`，没有网络请求。正式无后端时只显示 `未登录 · 未接线`、失败提示
和 `未提供`，不会升级为成功状态。

## 4. 公共表单导出（供首次使用只读复用）

首次使用 worker 可以**只读消费**以下导出；不要复制后再双写。表单不定义 HTTP、
CLI 或 daemon 契约，`onSubmit` 只是 UI callback。

```ts
// gui/src/features/dev01/forms/index.ts
export {
  AccountCredentialForm,
  type AccountCredentialFormProps,
  type AccountCredentialValues,
} from './AccountCredentialForm';
export {
  ApiKeyForm,
  type ApiKeyFormProps,
  type ApiKeyValues,
} from './ApiKeyForm';
export { SecretField, type SecretFieldProps } from './SecretField';
```

### AccountCredentialForm

```ts
readonly interface AccountCredentialFormProps {
  onSubmit: (values: { account: string; token: string }) => void;
  onCancel?: () => void;
  busy?: boolean;
  submitLabel?: string;
  initialAccount?: string;
  idPrefix?: string;
}
```

- account 保留在表单内；token 提交前清空，然后交给 callback。
- 取消清空 token，保留非敏感 account；不调用 `onSubmit`。
- 显示/隐藏按钮使用 `aria-pressed`/`aria-controls`，密码字段默认 `type=password`。
- 表单本身不发请求、不写存储、不记录秘密。

### ApiKeyForm

```ts
readonly interface ApiKeyFormProps {
  onSubmit: (values: { endpoint: string; apiKey: string }) => void;
  onCancel?: () => void;
  busy?: boolean;
  submitLabel?: string;
  initialEndpoint?: string;
  idPrefix?: string;
}
```

- endpoint 为非敏感输入，失败后保留；API Key 提交前清空，然后交给 callback。
- 取消清空 API Key、保留 endpoint；不调用 `onSubmit`。
- 未接入后端时默认调用方应返回 `unknown` 或 `failed`，不能返回成功。

### SecretField

`SecretField` 是密码输入和显隐控制的私有基础组件。输入值与显隐状态都只在
组件内存中；没有 storage、URL 或日志写入。

## 5. 模型服务私有区

文件：`gui/src/features/dev01/model/ModelServicePanel.tsx`。

导出：

```ts
export type ModelAuthMode = 'account' | 'api-key';
export type ModelConnectionState = 'unknown' | 'connected';
export type ModelValidationRequest =
  | { mode: 'account'; values: AccountCredentialValues }
  | { mode: 'api-key'; values: ApiKeyValues };
export type ModelValidationResult =
  | { status: 'connected'; detail?: string }
  | { status: 'failed'; message: string; retryable?: boolean }
  | { status: 'unknown'; message: string };
export interface ModelServicePanelProps {
  onValidate: (request: ModelValidationRequest) => Promise<ModelValidationResult>;
  onCancel?: () => void;
  onStatusChange?: (state: ModelConnectionState) => void;
  initialMode?: ModelAuthMode;
  className?: string;
}
```

行为：

- 两种方式：账号授权、API Key；切换方式会清空上一方式的内存表单和重试请求。
- 显隐、取消、校验、失败重试、未知状态均已覆盖。
- 只有 `onValidate` 明确返回 `connected` 才显示 `已连接`；初始、提交中、失败和
  未知分别显示未连接/状态未知，不会假成功。
- 失败和未知都保留可用的“重试校验”；重试复用内存中的同一请求，不重新读取
  已清空的密码字段。
- 取消、切换方式和卸载会清空最后请求；异步结果通过 attempt 计数丢弃过期回调。
- `onStatusChange` 只输出 `unknown`/`connected`，供共享连接选择器接入；`failed`
  和 `unknown` 都映射为 `unknown`，不把失败误报为已确认未配置。
- “额度与费用”使用 `StatusCard value={null}` 显示 `未提供`；不编造额度、限制、
  费用或免费状态。
- 组件自身不调用 `fetch`、不访问 daemon、不写 storage。

## 6. ConnectionsPage 接入提案（本任务未修改）

建议集成人后续在 `ConnectionsPage` 的“模型服务”卡片内、现有连接列表之后挂载
`ModelServicePanel`：

1. 先由连接/模型 owner 提供真实 `onValidate`；在真实 adapter 可用前，调用方必须
   返回 `{ status: 'unknown', message: '模型服务尚未接入。' }`，不能返回
   `connected`。
2. `onStatusChange` 可映射到共享连接选择器：`connected → connected`；
   `unknown → 状态未确认`（不要映射成已确认未配置）。
3. `onCancel` 可关闭私有区或返回列表；不改变现有 connection summary/fixture。
4. 不把 `ModelServicePanel` 直接接到现有 `connection.model.add/configure` 的
   stub 上，也不新增 HTTP/daemon 协议；真实接线属于后续契约工作。
5. 消息连接仍由 dev-03 负责，不在本组件范围内。

当前共享 `ConnectionStatusBadge` 只有 `connected/unconfigured/disabled`，没有
`unknown`。因此集成时应使用“状态未确认”的显示或先由共享组件 owner 扩展状态，
不能把 `unknown` 强行标成 `unconfigured`。

## 7. 秘密与状态矩阵

| 场景 | 非敏感输入 | 秘密 | 状态显示 |
| --- | --- | --- | --- |
| 初始 | 保留默认 | 空 | 未连接/状态未知；未提供 |
| 账号提交 | account 保留 | token 提交前清空 | 未登录；失败可重试 |
| API Key 提交 | endpoint 保留 | key 提交前清空 | 失败/未知可重试；不假连 |
| 取消 | 保留非敏感值 | 清空 | 回到未连接/初始化提示 |
| 切换模型方式 | 清空上一方式表单 | 清空 | 状态未知 |
| 卸载 | 组件销毁 | 组件销毁；清理重试引用 | 不保留 |
| 成功 | 仅真实 callback 返回 connected | 清空 | 已连接（仅接入后可能出现） |

源代码中没有 `localStorage`、`sessionStorage`、`fetch`、`document.cookie` 或
秘密日志调用；上述字符串只出现在给用户的“不写入”说明文本中。

## 8. 验证结果

基线 `e703c6690b3bbcd16f558cb7293503d3c4208bb2`，Node `v24.21.0`，
npm `11.19.0`。

通过：

- `npm ci`（按现有 lockfile，104 packages；无 package 变更）
- `npm run typecheck`
- `npm test -- tests/dev01-model.test.tsx tests/dev01-settings.test.tsx`
  — 2 files / 13 tests passed
- `npm run build`
- `npm run verify:production-copy`
- `npm run verify:safe-r1-build`
- `git diff --check`（已跟踪文件）

未通过/阻塞（如实记录，不伪造通过）：

1. **全量 `npm test` 失败 7 项，均来自未修改的共享测试对旧设置页的期望**：
   - `tests/settings-first-run.test.tsx`：仍期望设置页含模型表单、
     `打开首次设置`、两个 `未知 · 未接线`；
   - `tests/app-shell.test.tsx`：仍期望 `账号与设备`、`模型服务`、
     `打开首次设置`；
   - `tests/safe-r1-ui.acceptance.test.tsx`：仍从设置页点击
     `打开首次设置`。

   这些期望与本 track 的硬性要求“设置页不显示模型服务表单、Squire 或打开首次
   设置”直接冲突。共享测试不在本 worker 的写入范围，未修改、未隐藏兼容入口。
   需要集成 owner 在收拢时更新这些共享测试和首个 first-run 入口。

2. **375/760/1440 浏览器证据：BLOCKED，未运行出图**。
   - `npm run verify:safe-r1-browser`：
     `Error: Google Chrome is required at /Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   - `npm run test:browser`：5 个宽度任务均报
     `Failed to launch chromium because executable doesn't exist at /Applications/...`
   - 当前 Linux 环境没有可执行的 Chrome/Chromium；脚本把 macOS Chrome 路径写死，
     且 `safe-r1-browser-contract.mjs`/`dev-overlay-interaction.mjs` 也仍从设置页
     点击 `打开首次设置`。因此不能声称已完成 375/760/1440 浏览器门禁，也没有
     伪造截图。需要 operator/脚本 owner 增加跨平台浏览器入口，并按新 track
     更新脚本中的设置页路径。

## 9. 缺失共享能力与待补项

- 共享 `ConnectionStatusBadge` 缺少 `unknown`；需要由其 owner 定义“状态未确认”
  的映射，避免集成时把未知写成未配置。
- 首次使用流程仍由 `FirstRunPage` 自己维护旧模型表单；后续应由 A 只读消费本
  任务的 `AccountCredentialForm`/`ApiKeyForm`，避免双写和两套秘密生命周期。
- 浏览器 contract 脚本需要跨平台 Chrome 路径（例如显式环境变量入口）并更新
  设置页不再有 `打开首次设置` 的导航。
- 缺失的 `module-boundaries-r3.md`、`product-handoff-r3.md`、
  `shared-file-ownership-r1.md` 需要 operator 补齐后才能做更强的边界验收。
- 设置页仍沿用现有 `account.login` not-wired placeholder；真实账号 adapter 接线
  和正式 unavailable 状态属于后续有 backend 契约的 owner，不在本次虚构。

## 10. 交付与提交

- 实现与测试提交：`dd5f8fa71242e0a689b20d02f54cd92b2658870e`
  （`feat(gui): add dev01 account and model configuration surfaces`）。
- 本说明文件作为后续文档提交；最终 worktree head 在 PAC `outputText` 中记录，
  不把本文件自身的 SHA 写回自身，避免自引用。

本次不创建跨团队 PR、不合并、不推送公共 `feat/gui-dev-01`、不发布/出包/重启
服务。所有结果回 PAC 发起人 operator。
