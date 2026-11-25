# GUI 设置页 user/org 收敛实施规格

日期：2026-11-09
容器：`intg/gui-setting-fix-109` → `dev`
初始基线：`e70333d9f5568bd027e8707c9565b82692b1d992`；后续已合并 deep-module 改造及 `dev` 的 `59b62d98a2963f6bed4cab0daffc79fc57c837c0`。

## 结果与页面边界

设置页只承载账号、用户身份以及指向设备页和组织页的导航。设备页和组织页是外部主 tab；
连接页、provider 和 channel 不在本容器范围内。

| 页面 | 目标内容 | 明确排除 |
| --- | --- | --- |
| 设置 | `AccountLoginSection`、`UserBindingsSection`、设备链接、组织链接 | provider、设备卡、daemon 状态卡、组织表单、首次设置卡片及本文列出的移除项 |
| 设备（新外部 tab） | 当前设备状态与初始化、`LocalManagementSection`、`UpgradeStatusSection`、`HostsSection`、`DiagnosticSection`、`LogsSection` | `LifecycleSection`、`DoctorSection`、`UserProxySection`、`NetworkProfilesSection`、`AutoUpgradePolicySection` |
| 组织（既有外部 tab） | 既有 `OrgDomainProvider`、`OrgWorkspace` 与共享创建/加入组件 | 设置页内嵌组织表单或第二套组织数据源 |
| 连接 | 不变 | 不修改 `ConnectionsPage.tsx`，不迁移 provider/channel |

从 `SettingsPage` 移除 `ModelSettingsSection`、`LocalManagementSection`、`UpgradeStatusSection`、
`HostsSection`、`DiagnosticSection`、`LogsSection`、`LifecycleSection`、`DoctorSection`、
`UserProxySection`、`NetworkProfilesSection`、`AutoUpgradePolicySection` 及不再使用的引用。
“移除”指退出设置页，不要求删除仍可复用的源码或后端能力。五个未接线组件也不进入设备页，
后续另行设计。

## 真实调用约束

- 设备初始化经 GeneratedClient 调用 `onboarding.device.ensure`；daemon 状态经 `daemon.status`。
- 设备页保留 `autoupdate.status`、`upgrade.steps.status`、`hosts`、`top.snapshot`、`log.page`。
- 组织页使用 `org.list`、`org.create`、`org.join`。隔离本地创建不能写成真实联网创建；加入必须
  经过真实网络验收。
- 账号登录、取消、重新登录、登出和 unknown 状态均有先失败后通过的测试。`auth.logout` 若因
  custody 或后端装配失败，修复真实原因，不用 mock 或前端状态冒充成功。
- 所有浏览器业务调用使用同一 GeneratedClient 与 daemon request；不得调用 IPC-only 方法冒充
  HTTP 可用，不得复制第二套组织、设备或 preview 页面。

若真实测试证明需要新增或暴露契约方法，唯一条件工作包同时负责 request catalog、OpenAPI、
生成客户端、contract 测试和 Contract-Impact。其他工作包不得修改生成物。

## 工作包与所有权

### WP1 设备组件与账号适配（可并行）

- 设备组件 agent：新增设备页展示组件，组合当前设备、`LocalManagementSection` 和四个已接线
  设备组件；不修改 `SettingsPage`、`AppShell`、Rail 或页面路由类型。
- 账号/设备适配 agent：核对并修复账号登录、取消、重新登录、登出、设备初始化及 unknown 投影；
  负责相关 `features/account`、设备适配与测试，不修改页面组合。
- 组织 agent：验收并修复既有组织页的 `org.list`、`org.create`、`org.join`；不修改设置页，
  不新建设置页组织表单。

### WP2 外部 tab 与设置页收敛（主 agent）

- 主 agent 在 WP1 组件就绪后接入设备外部 tab 和路由，再清理 `SettingsPage`。
- 主 agent 独占 `SettingsPage`、`AppShell`、Rail 和页面路由类型，避免子 agent 冲突。
- 设置页仅保留账号、用户身份、设备链接和组织链接。

### WP3 条件后端/契约（仅真实失败证明需要时开启）

- 单独拥有 daemon request 后端、contract 和生成客户端。
- 必须先记录真实失败与最小复现，不得为消除 UI unavailable 而扩大范围。

## 红到绿与集成

每个工作包先记录命名测试的失败结果，再实现并通过焦点测试，及时推送指向容器的 WIP PR。
子 PR 全部以 `intg/gui-setting-fix-109` 为 base；容器 owner 负责集成，只有容器 owner 可以去除
容器 WIP，且不自行合并到 `dev`。

预览与正式入口复用同一展示实现。每个行为变更同步相关组件/页面测试，覆盖 loading、empty、
error、unsupported、unknown、键盘和受影响宽度。跳过测试不能冒充真实结论。

## 容器验收

在最终 `intg` head 上完成：

1. `npm --prefix gui run verify`。
2. `uv run --with libcst==1.9.0 pytest tests/`。
3. `uv run ruff check .`。
4. 结构门、模块边界门、contract 检查及 `git diff --check`。
5. 真实浏览器分别验收登录、设备初始化、daemon 状态、组织创建和加入，记录 PASS/FAIL/BLOCKED。
6. 核对页面边界、无第二套页面、无连接页 diff、无未授权生成物 diff。
7. 所有子工作包合入后重跑上述门禁，确认精确分支、head 和 CI event；去除 WIP 并通知用户审核。

真实登录、组织联网、后端契约或外部服务条件不足时，记录 BLOCKED、缺少条件和复现命令，保留
容器 WIP。需要修改连接页、扩大页面范围、修改生产状态或引入计划外公开契约时，先停止并请求
用户确认。

## 安全边界

不读取、修改或清理生产 daemon、生产 Lark App、真实凭据或用户级配置；不修改旧 `src/gui`、`app`、
分支保护或无关模块。测试 home、socket、端口、缓存与日常环境隔离。

## 实现与真实验收记录（2026-11-10）

### 单套界面交接

- 设置页共享实现为 `gui/src/pages/SettingsPage.tsx`，只组合账号、用户绑定和设备/组织导航。
- 设备页共享实现为 `gui/src/pages/DevicePage.tsx` 与 `gui/src/features/settings/impl/device/`；经
  `gui/src/features/settings/index.ts` 公共出口消费能力，不穿透其他 feature 的 `impl/`。
- 组织页继续使用既有 `OrgDomainProvider`、`OrgWorkspace` 和共享创建/加入组件，未增加第二套组织页面。
- fixture 入口由 `FixtureApp` 组合，正式入口由 `App` 组合；二者复用 `AppShell`、设置页、设备页和组织页。
  正式数据源是 GeneratedClient 和 daemon request，fixture 只覆盖预览组合，不证明真实业务成功。
- `ConnectionsPage.tsx`、provider/channel 实现、旧 `src/gui` 和 `app` 均未修改。

### 已执行的浏览器与 daemon 验收

| 场景 | 对应提交 | 结果与证据边界 |
| --- | --- | --- |
| 设置/设备 tab 与 daemon 状态 | `45c85825f297f51a5ee72596be28c0e532e31f62` | PASS：真实 Chromium、daemon driver；`onboarding.status`、`identity.*`、`daemon.status`、`autoupdate.status`、`hosts`、`top.snapshot` 等真实请求为 200，无 fixture 标记或控制台错误。 |
| 隔离协议登录 | `e19a10a8d53e774f20a67d9d4bcd2abf9d54dce4` | PASS：真实 Chromium 完整执行 discovery、device authorization、token polling、userinfo 和 owner commit。身份服务是仓库 `tests/fixtures/network/fake_oidc.py` 的隔离测试服务，不是生产 issuer。 |
| bootstrap 设备初始化 | `e19a10a8d53e774f20a67d9d4bcd2abf9d54dce4` | 先 FAIL（GUI 会话 catalog 未声明方法，零派发）后 PASS：GeneratedClient 发出 `onboarding.device.ensure`，真实返回 200；UI 设备就绪，journal 有 device/ready 回执，真实 tailcat sidecar 产物落盘。serving 阶段仍按 `BOOTSTRAP_ONLY` 拒绝。 |
| 账号登出 | `e19a10a8d53e774f20a67d9d4bcd2abf9d54dce4` | PASS：`auth.logout` 真实 200 回执，确认后身份凭据移除、daemon 自停，后续 GUI 请求为 503；未由前端或 driver 伪造停止。 |
| 组织创建与真实网络加入 | `45c85825f297f51a5ee72596be28c0e532e31f62` | PASS：两个隔离 daemon、loopback DERP 和两个正式 GUI；`org.create`、`org.invite`、`org.join` 均为真实 200，双向成员视图与 daemon `org.list` 均为 owner/member 两成员，清理完成。另一次 E2E-025 证明目录双向收敛与 OrgFS 双向同步。 |
| 手机账号菜单 | `e28fbb9589fe6469b9cc2aa58d23312604f82f23` | 先 FAIL（production 顶栏 blur 使底部导航覆盖账号菜单）后 PASS：development 与 production 各四个视口通过；375×320 下不再发生导航拦截点击。 |

设备初始化没有新增公开方法或字段：现有 MethodSpec 已声明 HTTP 暴露和 bootstrap/local-bootstrap
权限。修复只调整 GUI runtime host 的目录投影与准入；静态方法表保持仓库方法注册扫描器可解析的
字面量形式。serving 与 `onboarding.initialize` 的既有禁用边界不变。

### 未覆盖与后续问题

- `auth.hyprial.com` 生产登录为 BLOCKED：目前没有已授权的专用测试账号/U4 条件。默认 issuer 由
  `src/hyprial/daemon/impl/configuration/network_profile.py` 的 `DEFAULT_PROFILE` 固定为
  `https://auth.hyprial.com`；GUI 没有交付通用 OIDC 配置功能。fake OIDC 只验证上述隔离协议链路，
  不能写成生产登录 PASS。需要在无自定义 `profile.json` 的全新隔离 home 中启动真实 GUI，由获授权的
  测试账号或用户本人完成官方登录，再读取账号状态；不得读取或记录凭据。
- 原生壳、真机和生产用户接受均为 UNKNOWN；本记录只证明共享 Web 实现及明确列出的 Chromium 场景。
- `org.join` 使用默认 15 秒请求预算时，四轮真实验收中两轮在 daemon 已完成加入前中止并显示
  “结果未知”；另两轮分别在预算内完成，其中一轮为 14.5 秒。已成功加入不得据此推断每次时延都满足
  预算，也不得自动重发写入。后续处理登记为 KP-045。

完整自动门禁必须在最终集成 head 上另行列出实际结果；本节的局部证据不替代完整套件，也不把
环境失败或 skip 改写为 PASS。

Skill-Impact: not-needed: 本容器实施既定 GUI/daemon request 工作流，不改变 agent 操作方式。

## M2 前端 fixture 与方法对齐补充（2026-11-12）

本轮从最新 `origin/dev` 建立 `feat/gui-m2-login-device-settings`，只补充显式开发预览的
隔离数据源，不改变正式入口的 GeneratedClient 接线。`gui/src/dev/mock/settings.ts` 为设置和
设备页注入同一组能力接口，覆盖服务状态、升级状态、主机、诊断、日志以及本地用户/平台绑定。
这些数据只在 fixture composition 内存中存在，写入不会发布组织身份证明，也不代表 daemon 成功。

| GUI 操作 | 正式 MethodSpec / daemon request | 当前正式接线 | 当前 fixture | 结论 |
| --- | --- | --- | --- | --- |
| 登录、刷新、取消、退出本机登录 | `onboarding.status`、`auth.login.begin`、`auth.operation.get`、`auth.operation.cancel`、`auth.logout` | `createConnectedAccountPort` | `createFixtureFirstRunActions`（新增 `logoutAccount`） | PASS（fixture）；真实隔离协议证据见上，生产 issuer 仍 BLOCKED |
| 初始化当前设备 | `onboarding.device.ensure` | `createConnectedDeviceActions` | fixture `initializeDevice` | PASS（fixture）；真实 bootstrap PASS，serving 边界按既有规则拒绝 |
| 用户与绑定读取 | `identity.users.list`、`identity.bindings.list` | `createSettingsReadApis` | `userBindings.list` | PASS（接线）；真实 producer/权限需在隔离 daemon 复跑 |
| 身份解析 | `identity.resolve` | `createSettingsReadApis` | `userBindings.resolve` | PASS（接线）；真实返回未在本轮新跑，UNKNOWN |
| 新建本地用户 | `identity.users.add` | `createGeneratedBindingWrites` | `userBindings.writes.add` | PASS（fixture）；只改本机记录 |
| 设置/清除本地绑定 | `identity.override.set` / `identity.override.clear` | `createGeneratedBindingWrites` | `writes.bind` / `writes.clear` | PASS（fixture）；组织证明发布不在 GUI 此操作内 |
| 本机服务状态 | `daemon.status` | `createSettingsReadApis` | `localManagement.status` | PASS（接线）；真实状态需隔离 daemon 验收 |
| 升级状态与迁移步骤 | `autoupdate.status`、`upgrade.steps.status` | `createSettingsReadApis` | `upgradeStatus.status/steps` | PASS（接线）；不提供未声明的升级写操作 |
| 主机、在线诊断、日志摘要 | `hosts`、`top.snapshot`、`log.page` | `createHostsApiFromClient`、`createDiagnosticApiFromClient`、`createSettingsReadApis` | 对应 fixture API | PASS（接线）；producer 结果仍按真实环境 UNKNOWN |

开发预览新增两个可直接使用的起点：`hyprialScenario=j1=logged-out-init` 表示服务可用但账号未
登录、设备未就绪；`hyprialScenario=j1=logged-in-init` 表示账号已登录且设备已初始化。两者都只在
`HYPRIAL_GUI_DEV_PREVIEW=1` 或 parity 入口生效，不能被当作 M1 真实 daemon 状态。`account-ready`
另作为选择器场景保留，便于在不改变 URL 的情况下复现已初始化起点。

本轮浏览器单元组合验证覆盖了：已登录起点的设置卡片、设备卡片、用户目录以及退出本机登录后
回到登录按钮；`npm --prefix gui run typecheck`、模块边界、生产副本门和目标 Vitest 均通过。真实
M2 登录、设备初始化、身份读写仍应使用仓库已有隔离 daemon/fake OIDC 场景复跑；fixture 结果不能
替代真实 backend 或生产账号验收。
