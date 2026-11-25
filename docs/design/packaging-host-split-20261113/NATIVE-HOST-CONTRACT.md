# Native host 契约版本（P3/C1 设计附录，2026-11-13）

本文件定义 A 宿主与目标分支（C）之间的**可版本化、可预检**契约。它回应 Codex review
第 4 条：`httpContractVersion`/`catalogDigest` 只覆盖 HTTP envelope 与 catalog，不能证明
启动、bootstrap 生命周期和方法准入半兼容。主方案见
`docs/design/packaging-host-split-20261113.md`。

## 1. 两层版本，不混用

| 版本 | 载体 | 覆盖 | 现状 |
|---|---|---|---|
| `httpContractVersion` | `contract/http-request/http-contract.v1.json`、`server.json`、catalog | HTTP envelope、headers、错误码、catalog JSON 形状 | 已有 v1 |
| `hostContractVersion` | 本文件 + `contract/native-host/native-host.v1.json`（C1 落地） | 启动参数、`server.json` 启动证明、bootstrap/promotion 生命周期、方法准入、可选能力 | 本批新增 |

A 的 `upstream.rs` 必须同时检查两者；只检查 `httpContractVersion` 或只检查方法名不够。

## 2. 契约载体

C1 在目标分支提供 `contract/native-host/native-host.v1.json`，并在 `/daemon/request`
catalog 与 `server.json` 中报告同样的 `hostContract` 块：

```json
{
  "version": 1,
  "hostContractVersion": 1,
  "compatible": { "min": 1, "max": 1 },
  "launch": {
    "nonceStdin": true,
    "stdinStop": true,
    "privateServerJson": true,
    "serverJsonSchema": 1
  },
  "lifecycle": {
    "bootstrapInitialize": true,
    "postReply": true,
    "transportGeneration": true,
    "authLogoutBootstrap": true
  },
  "features": {
    "harnessCli": false,
    "windowsPrivateAcl": false
  },
  "requiredMethods": [
    "onboarding.status",
    "auth.login.begin",
    "onboarding.device.ensure"
  ],
  "requiredPhases": ["bootstrap", "ready"]
}
```

- `hostContractVersion` 是整数；破坏性改动递增，A 声明 `min/max` 兼容范围。
- `features` 是能力位，不是方法名；预检必须逐项为 true 才能启用对应旅程。
- `requiredMethods`/`requiredPhases` 是方法名和 phase 的**补充**，不能替代能力位。
- `server.json` 的 `launchNonce`、`httpContractVersion`、`hostContractVersion` 必须来自
  同一次启动。

## 3. A 侧检查

1. 启动 `request_http` 时先送 nonce stdin；
2. 读 `server.json`，校验：
   - `launchNonce` 等于本次 nonce；
   - `httpContractVersion == 1`；
   - `hostContractVersion` 在 `min..max`；
   - `launch.nonceStdin/stdinStop/privateServerJson` 为 true；
3. 读 `/daemon/request` catalog，校验：
   - `catalogDigest` 稳定；
   - `hostContract` 块与 `server.json` 一致；
   - `requiredPhases` 包含 `bootstrap`，`requiredMethods` 在 catalog 中 callable；
   - 目标旅程所需 `features` 为 true；
4. 任何一项失败：拒绝启动/出包，写 `CONTRACT_MISMATCH` 诊断；不回退、不降级。

## 4. 预检与 provenance

`--target` 预检在隔离 home 中安装目标 wheel，启动一次目标 `request_http`，读取
`server.json` + catalog，断言上述字段，并记录：

- `hostContractVersion`、`features`、`catalogDigest`；
- `launchNonce` 是否出现（只记布尔，不记值）；
- 目标 wheel `+g<sha>`、`uv.lock` digest、`targetTree`/`targetGuiTree`。

预检不通过则不进入构建；provenance 记录预检结果和契约版本。

## 5. C1 / C2 的版本边界

- **C1（硬契约）**：`launch`、`lifecycle.bootstrapInitialize/postReply/transportGeneration`、
  `authLogoutBootstrap`、`requiredMethods`、`requiredPhases`；`hostContractVersion=1`。
- **C2a/C2b（能力）**：`windowsPrivateAcl`、`harnessCli` 等 features；未落地时预检可要求
  对应 feature 为 false 并 feature-gate 页面，不用“方法存在”假装可用。
- A 只保留**限期**兼容 shim（如 upstream rebind）；shim 必须写到期条件，不能替代
  `hostContractVersion` 检查。

## 6. 测试

- C1 的单测：`server.json`/catalog 都带 `hostContract`；缺字段、版本越界、feature 为 false
  时 A 的预检失败；
- A 的 Rust 测试：`upstream.rs` 对 `hostContractVersion`/feature 的拒绝路径；
- 契约门：`contract/native-host/` 的 JSON 与目标 catalog 一致，`Contract-Impact` 同步；
- 预检测试：一个故意缺 `launchNonce` 的 fake server 必须失败，不能出包。
