# 后端 native-host 契约（P3 分析附录，2026-11-13）

本文件记录 15c0b3707（A 宿主基线）、dev `7d4dc766`、PR #2727 `33ec5390` 三者在后端
native-host 契约上的差异与归属。状态：**分析完成，待落地**。主方案见
`docs/design/packaging-host-split-20261113.md`。

## 1. 归属结论

| 区域 | 归属 | 说明 |
|---|---|---|
| `clients/native/**`、`packaging/clients/**`、native setup 模块 | A | 宿主/打包/安装检测 |
| `request_http/{config,__main__,__init__,server}.py`（15c0b37 的 launch nonce/stdin-stop/Windows 私有文件） | C | A 的 `upstream.rs` 硬依赖 `--launch-nonce-stdin`、`server.json.launchNonce`、stdin shutdown、Windows 私有 config/server |
| `request_http/bootstrap/{gui,response}.py`、`app.py`（2727） | C 契约，B 参考实现 | `onboarding.initialize` admission、post-reply 确认、transport-generation 轮换；`serve_gui` 是前台生命周期实现，A 不得 fork |
| `application/onboarding/{gui_host,service}.py`、`daemon/impl/onboarding/{runtime,operations/service}.py`（2727） | C | 方法/phase 表、bootstrap `auth.logout`、`mark_auth_ended`、promoted-surface 接管、journal 共享 |
| `request_catalog/onboarding/{specs,state}.py`（2727） | C | `auth.logout` bootstrap+serving admission、initialize/device/logout 的 generation 幂等 |
| `gui/backend/lifecycle/{rebind,session,source}.py`、`gui/install/report.py`（2727） | B | 业务 GUI 的 rebind/authority 发布/文案 |
| `shell/impl/onboarding/{harnesses,harness_activation,node_preparation}.py`、`init/onboarding.py`、`kernel/impl/primitives/managed_harness.py`、`agent_templates/setup.py`（15c0b37） | C | setup 页调用目标 wheel 的 `hyprial init onboarding harnesses|harness-select|harness-install`；2727 缺失 |
| `kernel/impl/platform/**`、`harnesses/protocol/{common,windows_pty}.py`、`agent_templates/home_layout.py`（15c0b37） | C | Windows ACL/ConPTY/隐藏控制台/managed-harness 平台修复 |
| `request_catalog/{conversations,message/history}.py` + `openapi.json` | C 契约，B 消费 | `replyToMessageId` 是 daemon schema 字段 |
| `shell/impl/packaging/platform_capabilities.py`、`gui_build_hook.py` | C 构建修复；A 可临时 shim | Hatch 加载 hook 时不 import hyprial；Windows wheel 构建需要 |
| `application/onboarding/http.py`、`account_lifecycle.py`、`provisioning.py`、`lifecycle/**`、`request_catalog/setup/**` | C | 三者无分歧，无需合并 |

### 1.1 契约版本与 C1/C2 分批

- 契约版本载体见 `NATIVE-HOST-CONTRACT.md`：`hostContractVersion` + `features` 能力位，
  与 `httpContractVersion` 分开。C1 必须先提供 `launch`、`bootstrapInitialize`、
  `postReply`、`transportGeneration`、`authLogoutBootstrap` 和 `requiredMethods`/
  `requiredPhases`；C2a/C2b 提供 `windowsPrivateAcl`、`harnessCli` 等 features。
- **C1（硬契约，约 500 行）**：`request_http` launch nonce/stdin-stop/private 契约、
  initialize/post-reply/generation、`auth.logout` admission、双模式 `__init__`、
  OpenAPI 重生。C1 进 dev 后，2727 同步即可被新线打包。
- **C2a（平台，约 840 行）**：POSIX/Windows PTY、private ACL、ports/process；Windows 包
  必背，macOS 可按旅程分批。
- **C2b（harness，约 2,000 行）**：harness CLI + managed harness + Node 准备；安装检测
  功能必背，首包可 feature-gate。
- 旧 native account bridge 不属于 C：它在 A 的 native 层，默认不注入主页面；B 走 connected
  onboarding，C 不提供 native-bridge 登录路径。

## 2. 关键冲突

1. **`request_http/__init__.py` 是唯一直接后端冲突**：15c0b37 的 `serve(path, *, launch_nonce, stop)`
   传 `request_once=host` + nonce；2727 把 local-gui 分派到 `serve_gui(config, host)`。
   合并必须保留两种模式，以“是否提供 launch nonce”区分。
2. **Launch 协议**：A 的 `upstream.rs` 总是加 `--launch-nonce-stdin` 并拒绝没有
   `launchNonce` 的 `server.json`；2727 的 `__main__.py`/`server.py` 两者都没有 → 目标 2727
   后端会在 argparse 退出或被判“does not prove this launch”。
3. **双重 promotion / stale upstream**：2727 的 `bootstrap/gui.py` 自己创建 stop event，
   `onboarding.initialize` 后在进程内跑 daemon 并在 `port=0` 重新发布 HTTP + 新 token；
   A 的 `Upstream` 保留启动时地址/token，不重读 `server.json`；A 的 promotion watcher 也可能
   重启 runtime → 双重生命周期或 stale relay。
4. **Bootstrap catalog/phase**：2727 的 `onboarding.initialize` 只在 `enable_promotion()` 后
   callable，而只有 `serve_gui` 调用它；A 不走 `serve_gui` 则 B 的 initialize 被拒，走
   `serve_gui` 则 A 失去 upstream。A 不能通过 HTTP arm promotion。
5. **`auth.logout` admission**：15c0b37/dev 仍 serving-only；2727 在 bootstrap/authenticated
   也 admit 并设 `reopen_required`。A 的 target 若基于 dev/15c0b37，B Settings 的 logout 会失败；
   A 不得同时驱动 native-bridge logout。
6. **OpenAPI/client**：2727 的投影是大重生（provider/agent 方法）且没有 replyToMessageId；
   15c0b37 的最小投影有它。必须从合并后的 MethodSpec 重生，禁止手合 JSON。
7. **GUI 冲突**：`gui/src/**`、`gui/tests/**` 是 15c0b37 旧业务 vs 2727；B 取 2727，A 不携带。

## 3. 必须落 C 的清单

- launch nonce/stdin-stop/drain refusal；Windows 私有 config/server 发布与 fsync seam；
  app-core control-stdin watchdog（`gui/adapter/lifecycle.py`）。
- bootstrap initialize/post-reply/generation rotation；`auth.logout` bootstrap admission 与
  journal；generation-scoped idempotency。
- harness CLI + kernel managed-harness/primitives + Node preparation/activation。
- kernel platform PTY/privacy/ports/process 修复。
- `replyToMessageId` catalog 与重生 OpenAPI。

## 4. A 可保留的限期兼容补丁

- `upstream.rs` 在 `transportGeneration` 变化后重读 `server.json` 并重新 mint，直到 C 发布
  generation-rotation 事件且 2727 rebase。
- setup 页的 harness 动作在 C 的 harness CLI 落地前 feature-gate。
- 如 C 构建修复未到 P6，可在打包时临时 vendor `platform_capabilities.py`/`gui_build_hook.py`。
- 到期：第一个以“包含 C 的 target”构建的 2727 包。

## 5. 落地顺序

1. 最小 C 落后端/dev：request_http launch/private 契约 → onboarding initialize/logout/specs/state。
2. 把 C 合入 2727（B），合并 `request_http/__init__.py`，重生 client。
3. A 在 `68ed00b4`（相对 dev 的 src/hyprial 干净）上只保留 native/packaging，必要时加限期 shim。
4. `--target origin/pr-2727` 构建 + 预检。

## 6. 对 2727 包的影响

- **临时 demo 包（A 宿主 + 15c0b3707 后端 + 2727 GUI）**：A 宿主与后端契约同源，可直接跑；
  2727 GUI 可能缺若干新方法（methodAccess 拒绝），但登录/设备/聊天可验证。这是最快路径。
- **正式新线打 2727**：必须先把 C 落进 2727/dev，或由 A 带限期兼容补丁；否则 launch nonce、
  promotion、initialize、harness CLI 中任一缺口都会让包在真实机器上失败。

## 7. 风险

| 风险 | 缓解 |
|---|---|
| init 后双重 promotion / stale upstream | 先定 C 的 generation-rotation 事件；A 只做限期 rebind；预检 + smoke |
| native-bridge vs connected `auth.logout` 重叠 | 新线不注入 native account；C 明确 serving/bootstrap admission |
| C scope 拖延 P6 | 临时 demo 用 15c0b3707 后端；正式包以 C 落地为门 |
| OpenAPI/client 漂移 | 从合并 MethodSpec 重生，跑 contract gate + client reference check |
| 缺 nonce/Windows ACL 导致 host 拒绝 | C 的 request_http 契约先落；预检覆盖 launch |
| 缺 harness CLI 破坏 fresh-home setup | C 落 harness CLI；A setup 页 feature-gate |
