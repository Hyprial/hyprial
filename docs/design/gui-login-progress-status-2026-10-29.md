# GUI 登录进度与真实状态修复（2026-10-29）

状态：GUI 实现与隔离假事件测试完成；真实账号登录、真实网络检测和浏览器
375/760/1440 截图未验证。实现基于 dev01 基线，尚待独立身份审批后提交。

- 基线：`origin/feat/gui-dev01-pi-first-run-20261029` =
  `0d181043c22497534ecf89c4fab8c09393320afa`
- 独立分支：`fix/gui-login-progress-status-20261029`
- PR 目标：`feat/gui-dev01-pi-first-run-20261029`（PR #2068 的 head 分支；
  #2068 仍以其为 head、以 `feat/gui-dev01-real-login-20261029` 为 base，
  PR #2065 以 `intg/gui-init` 为 base；本分支不直合 dev）
- 范围：只改 `gui/` 前端、GUI 本地认证中间件的安全字段映射、测试和本文档；
  未改 `src/hyprial`、`clients/native`、`.forgejo`/CI、登录协议、绑定规则或
  服务端真实事件定义。

## 真实事件来源与映射

只读安装了 `runtime` 版本 `0.5.5.1`（HEAD
`98b104257485a88236c569d293c50e7acc909fb0`）的公开源码，确认实际序列为：

| 运行时事件 | 真实含义 | GUI 允许展示的字段 | 界面阶段（不冒充成功） |
| --- | --- | --- | --- |
| `device` | 账号设备授权等待用户完成 | 可信 HTTPS `verificationUri`、`userCode`、`expiresIn`、`interval` | 请在浏览器完成账号授权 |
| `authenticated` | 账号授权已确认，尚未完成登录提交 | `owner`（若有） | 账号授权已确认，正在等待后台结果 |
| `binding-device` | 附加设备授权等待用户完成（可能 `restarted`） | 可信 URL、`userCode`、`expiresIn`、`interval`、`restarted` | 需要补充授权：请完成附加设备授权 |
| `binding-proof-failed` | 附加设备绑定未确认；登录流程仍可能继续 | 仅安全错误码 `code` | 附加设备授权未确认，不自动重试、不绕过绑定 |
| `committed` | 登录凭据提交已确认，仍需核验本机状态 | `owner`（若有） | 登录提交已确认，正在核验本机状态 |
| 其他/未来事件 | GUI 不认识 | 仅稳定的 `unknown` 标记 | 未识别的登录进度，不视为成功 |

`gui/server/auth/worker.py` 只把 `desktop_network.status()["profile"]["issuer"]` 附加到
每个进度事件，供本地宿主校验授权 URL 主机；`issuer` 本身不是 token，但也不会被
转发给浏览器（`public-data.mjs` 不在输出中保留它）。`device`/`binding-device` 的
`userCode`/`verificationUri`/`interval`/`expiresIn`/`restarted` 均进入 GUI 类型
（`AuthProgress`、`AccountProgress`）和 server 安全映射，不会被白名单静默丢弃。

GUI 在 `gui/server/auth/public-data.mjs` 的本地中间件边界做白名单和清洗：

- 原始事件名、任意 `message`、设置/凭据路径、`access_token`、`refresh_token`、
  `id_token`、`token`、`secret` 等字段不进入浏览器；
- 授权 URL 必须是 HTTPS、无 userinfo、长度 ≤2048，且其主机必须与配置的
  profile `issuer` 主机一致；缺失 issuer 或不匹配主机的 URL 一律不渲染/不打开，
  `userCode` 仍可独立显示；
- `userCode` 最多 128 字符、禁止控制字符；按敏感设备授权码处理：只在明确的
  二次授权/账号授权 UI 中展示，带有效期/剩余时间，不写日志、不进普通 URL/query、
  不进测试快照或控制台；
- `expiresIn` 限制为 1..3600 秒，`interval` 限制为 1..300 秒，`restarted` 只接受
  布尔值；
- 失败事件只保留匹配 `^[A-Z][A-Z0-9_]{0,79}$` 的 `code`；
- 未知事件统一降级为 `{ event: 'unknown' }`，原始名称和内容不转发。

## UI 行为

`gui/src/features/onboarding/AccountProgressPanel.tsx` 是首次设置和设置页共用的
唯一进度/等待/重试面板，所有分支只由公开事件和最终
`AuthState`/`AuthResult` 驱动：

- 授权等待显示持续旋转状态、已等待秒数、授权码、可信链接和剩余有效期；
  剩余有效期由授权服务返回的 `expiresIn` 减去本地已等待秒数得出，显示为
  “授权剩余约 M 分 S 秒（由授权服务返回，以授权页为准）”；到期显示
  “授权有效期已结束；请取消后重新登录”，不会自动重复登录；
- 二次授权提供用户手势可点的“打开授权页”按钮和安全链接；浏览器拦截弹窗时
  显示明确提示并保留可点击链接，同时提示无需填写密码或访问令牌；
- 附加授权显示 `restarted`（重新发起）、轮询间隔和剩余有效期；
- 等待 30 秒后明确说明后台仍在处理、界面无法确认更细的服务端步骤、不得把
  等待显示为成功；等待 90 秒后提供明确的“重试登录”操作，但不会自动重复
  登录，也不会自动覆盖凭据；
- 取消登录/取消授权只取消本 GUI 拥有的操作，保留已完成的账号/设备阶段；
- `binding-proof-failed` 显示安全错误码、可操作的重试和“不会绕过绑定”说明；
- 未知事件显示安全降级警告，不显示账号已登录，也不触发自动重试；
- 最终账号状态只有收到 `AuthState.authenticated === true` 才显示“账号已登录”；
  设备状态只有收到 `deviceReady === true` 且无 sidecar 错误才显示
  “本地设备身份已初始化”；
- 新增独立“网络”状态，值为“未检测 · 待检测”，提示登录和设备就绪不等于网络
  已连接；`deviceReady`、HTTP 回调或登录成功不被当作网络证据。

首次设置和设置页复用同一套 `FirstRunActions`/`AuthClient` 能力、同一
`AccountProgressPanel` 和同一状态映射，没有第二套登录接口、第二套状态机或
第二份 durable inbox。

## 验证记录

| 命令 | 结果 |
| --- | --- |
| `npm --prefix gui run typecheck` | PASS |
| `npm --prefix gui test` | PASS：219 passed，1 skipped（既有跳过项，不当作本改动通过） |
| 焦点测试（`login-progress`、`dev01-onboarding`、`settings-first-run`、`auth-client`、`dev01-auth-actions`） | PASS：39/39 |
| `node --test gui/tests/auth-host.node.mjs` | PASS：11/11，含 runner 绑定事件清洗、issuer 主机校验、`interval`/`expiresIn` 限制和未知事件降级 |
| `npm --prefix gui run verify:production-copy` | PASS：build + 0 forbidden markers |
| `check_structural_limits.py` | PASS，0 violations |
| `check_module_boundaries.py` | PASS，0 violations |

未验证/阻塞：

- 真实干净账号登录未运行（禁止读取真实 token/凭据，且需要用户操作）；本修复
  不能用隔离假事件替代真实登录验收。
- 真实网络连接/断开检测未实现、未验证；本改动只显示“未检测”。
- 375/760/1440 真实浏览器截图未取得：本机存在 Chrome，但执行浏览器验收命令
  需要工具审批而未能启动；该项记为 BLOCKED，不冒充 PASS。
- 真实绑定失败/二次授权只在假 CLI 事件和公开事件契约层验证，未用真实账号触发。
- 真实 provider 的 `verificationUri` 主机是否与 profile `issuer` 主机一致未实测：
  若供应商使用独立授权主机，现有保守校验会隐藏 URL（只保留 `userCode`），需要
  登录/协议 owner 提供可信授权主机后再放宽；当前不会放行任意主机。

## Fisher 交接项

- 后端/登录 owner 继续持有真实事件定义；GUI 只做安全映射，未来若新增事件，
  需在 `public-data.mjs` 和 `AuthProgress` 类型中显式加入白名单/映射，否则会以
  `unknown` 安全降级。
- 网络状态需要独立的真实探测 API 才能从“未检测”变为“已连接”；不得用
  `deviceReady` 或登录成功代替。
- 若产品希望 `binding-proof-failed` 后禁止继续设备初始化，应先由登录/绑定 owner
  确认规则；本 GUI 只展示失败和重试，不改变绑定规则。
- 身份审批：当前工作树作者/提交者身份仍是 `Allen Woods
  <alicesmith@users.noreply.code.example.com>`，在收到明确 operator/用户审批前
  不提交、不 push、不创建 PR。
