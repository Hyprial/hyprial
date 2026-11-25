# C1b：bootstrap promotion / generation 契约（2026-11-13）

本文件是 C1b 的实现设计，承接 C1a（launch nonce/private/contract JSON）。目标：让 A 宿主
能调用 `onboarding.initialize` 并安全接管 promotion，而不把 2727 的 `serve_gui` 原样搬进
A，也不产生双 promotion / stale upstream。主方案见
`docs/design/packaging-host-split-20261113.md`，契约版本见 `NATIVE-HOST-CONTRACT.md`。

## 1. 问题

- 2727 的 `request_http/bootstrap/gui.py` 由 `serve_gui` 拥有前台循环；`enable_promotion`
  只有它调用，`onboarding.initialize` 在 bootstrap 阶段只在 arm 之后 callable。
- A 的 `gui_host/upstream.rs` 用 `serve(config, request_once=GuiRuntimeHost)` 启动子进程，
  不进入 `serve_gui`；因此:
  1. B 的 `initialize` 可能被拒；
  2. 即使 promotion 发生，2727 在 `port=0` 重新发布 HTTP + 新 token/generation，A 仍持有
     启动时的 `server.json` 地址/token → stale relay；
  3. A 的 promotion watcher 还可能重启 runtime → 双生命周期。
- 直接搬 `serve_gui` 会让 A 失去启动/停止所有权，和 A 的 promotion watcher 冲突。

## 2. 推荐方案：target-owned external promotion

保留 target 进程内的 promotion，但把“何时 arm、何时 republish”变成 A 可声明的契约：

1. A 在 local-gui config 中加 `nativeHost.promotion = "external"`（或
   `hostContract.lifecycle.externalPromotion = true`）。
2. target 的 `serve` 检测该字段后，自己 arm `enable_promotion`，但不要求 A 走 `serve_gui`；
   `initialize` 在 bootstrap 阶段 callable。
3. `initialize` 成功回复 flush 后，target 在进程内跑 daemon/promotion，停止旧 listener，
   在**同一个 runtime dir** 原子重写 `server.json`：新 port/token、
   `transportGeneration`、`phase="ready"`、`hostContract` 不变。
4. A 的 `Upstream` 在 initialize 后监听 `server.json` 的 `transportGeneration` 变化，
   重新 mint authority；在此期间 relay 返回 `UPSTREAM_REBINDING`（可重试）而不是静默
   stale。
5. A 的 promotion watcher 看到 generation 已变化时**不再重启 runtime**；只有 target 退出
   或 generation 未出现时才走原恢复路径。
6. `auth.logout` 在 bootstrap/authenticated admit；target 设 `reopen_required`，停止
   serving listener，重写 `server.json` 为空 home bootstrap，A 导航回 setup/login。

不推荐 A 自己停/起第二个 target 进程：会丢失 bootstrap journal/锁，且与 2727 的
`run_promoted` 语义不一致。

## 3. 接口

### 3.1 config

```json
{
  "schemaVersion": 1,
  "principal": {"kind": "local-gui", ...},
  "nativeHost": {
    "promotion": "external",
    "contractVersion": 1
  }
}
```

### 3.2 `server.json` 重发布

```json
{
  "schemaVersion": 1,
  "pid": 1234,
  "endpoint": "http://127.0.0.1:54321",
  "token": "...",
  "launchNonce": "...",
  "httpContractVersion": 1,
  "hostContractVersion": 1,
  "transportGeneration": "gui-<hex>",
  "phase": "ready",
  "principal": {"kind": "local-gui", "owner": "...", "actor": "user:..."},
  "backend": {"kind": "local-daemon", "socket": "...", "owner": "...", "nodeId": "..."}
}
```

重发布必须 temp + fsync + atomic rename；旧 `server.json` 不得在窗口内被读到半写状态。
A 的 `read_authority` 校验 `launchNonce` 不变、`transportGeneration` 单调变化。

### 3.3 A 侧 rebind

- `Upstream` 增加 `transport_generation: Option<String>` 和 `rebind()`：
  - 读新 `server.json`，校验 nonce 不变、privacy、principal/backend 一致；
  - 替换 addr/token，更新 generation；
  - 失败不丢弃旧 authority，但返回 `UPSTREAM_REBINDING`，不把旧 token 当成功。
- `power_pack` promotion watcher：若 `Upstream` 已 rebind 且 target 仍存活，不重启 runtime；
  只有 target 退出/超时才恢复 bootstrap。
- 到期条件：目标分支发布 `transportGeneration` 且 2727 rebase 后，删除 A 的 rebind shim。

## 4. 后端文件清单（C1b）

| 文件 | 改动 |
|---|---|
| `request_http/__init__.py` | `serve` 检测 `nativeHost.promotion=external`，arm promotion，进入 external 循环 |
| 新 `request_http/bootstrap/external.py`（或复用 `gui.py`） | initialize → stop listener → run_promoted → republish server.json |
| `request_http/bootstrap/response.py` | post-reply flush 后触发 promotion |
| `daemon/impl/onboarding/runtime.py` | post-reply/quiesce、generation rotation、`reopen_required`、logout generation |
| `daemon/impl/onboarding/operations/service.py` | lifecycle handoff/generation fencing |
| `request_catalog/onboarding/{specs,state}.py` | bootstrap/authenticated `auth.logout` admission |
| `request_http/config.py` | `nativeHost.promotion`/contractVersion 解析 |
| `contract/native-host/native-host.v1.json` | lifecycle 四项 true；externalPromotion feature true |

## 5. 验证

- `initialize` 在 bootstrap 可调用；回复先 flush 再停止 listener。
- 重发布后 A 读到新 port/token/generation；旧 token 请求被拒，A 返回可重试 rebind。
- 双 promotion 回归：A watcher 在 generation 存在时不重启 runtime。
- logout：bootstrap/authenticated 均 admit；A 导航回 setup；旧 serving listener drain。
- 失败：target 在 promotion 中退出 → A 恢复 bootstrap；不得留下半写 server.json。
- Windows：atomic replace/ACL 在真实 Windows 上验证；未测标 UNKNOWN。

## 6. 风险

| 风险 | 缓解 |
|---|---|
| A 在重发布窗口读到旧/半写 server.json | target 原子 rename；A 校验 generation/nonce；重试 |
| 双 promotion | A watcher 以 generation 存在为准，不重启 |
| initialize 回复已发但 promotion 失败 | target 回退 bootstrap 并重发布；A 显示可重试错误 |
| logout 与 native-bridge 重叠 | 新线不注入 native account；只走 connected logout |
| C1b 拖延正式包 | C1a 先落；2727 包以 C1b 为门，临时包不在此范围 |
