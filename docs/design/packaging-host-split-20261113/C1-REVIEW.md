# C1 后端契约审核说明（2026-11-13）

本文件给 backend owner 复核 C1a（launch/private/contract）使用。C1b
（initialize/promotion/auth.logout）在 C1a 之后接续；正式 2727 包以 C1b 为门。

## 1. 分支与提交

- 分支：`feat/native-host-contract-20261113`（从 `origin/dev` 起）
- C1a：`431012be3 feat(native-host): add C1a launch ownership contract to request_http`
- C1b：`d8fed9925 feat(native-host): add C1b external promotion contract`
- 已 rebase 到 `origin/dev 0415ddfad`；committer 为包装线负责人本人（标识已脱敏）。
- 已 push 分支；未创建 PR、未合入。本文件只是审核说明。

## 2. C1a 内容

| 区域 | 文件 | 说明 |
|---|---|---|
| 启动证明 | `request_http/__main__.py` | `--launch-nonce-stdin`；首行 nonce；stdin `shutdown`/EOF → stop |
| 启动证明 | `request_http/server.py` | 32 hex nonce 校验；`server.json.launchNonce`；catalog/server 报 `hostContractVersion` + `hostContract` |
| 生命周期 | `request_http/__init__.py` | `serve(..., launch_nonce=None, stop=None)`；未提供时保持原行为 |
| 私有文件 | `request_http/config.py` | Windows private config/runtime ACL/reparse/fsync；POSIX 行为保留 |
| 契约 | `contract/native-host/native-host.v1.json` | launch 三项 true；lifecycle/features 暂 false；requiredMethods/phases |
| 契约 | `contract/http-request/http-contract.v1.json` | catalog schema 增加 hostContract 字段 |
| 一致性 | `request_http/app.py`、`catalog.py`、`http_contract/transport.py` | FastAPI/OpenAPI 模型与 catalog 同步 |
| Kernel | `kernel/impl/platform/privacy/private_access.py`、`interface.py`、`kernel/__init__.py` | 导出 `private_config_file_violation` |
| 测试 | `tests/shell/request_http/launch/test_launch_contract.py` | 20 个 launch/contract 测试 |

### C1b（`d8fed9925`）

| 区域 | 文件 | 说明 |
|---|---|---|
| external promotion | `request_http/bootstrap/external.py` | bootstrap listener → initialize reply flush → 同进程 promotion → 原子重发 `server.json`（新 port/token/generation/owner） |
| post-reply | `request_http/bootstrap/response.py` | 仅在成功回复 flush 后触发 promotion |
| 配置 | `request_http/config.py` | `nativeHost.promotion=external` + `contractVersion=1`，仅 local-gui |
| server | `request_http/{__init__,server,catalog,app}.py` | external 分支、`phase`/`transportGeneration`/`capability_phase` |
| initialize | `daemon/impl/application/onboarding/gui_host.py` | 只有 `enable_promotion` 后 admit `onboarding.initialize`；`run_promoted`、`take_post_reply` |
| logout | `onboarding/runtime.py`、`operations/service.py`、`request_catalog/onboarding/{specs,state}.py` | bootstrap/authenticated `auth.logout` admission、`reopen_required`、generation 校验 |
| 契约 | `contract/native-host/native-host.v1.json` | lifecycle 四项 true；`features.externalPromotion=true`；OpenAPI 重生 |

C1b 测试：`tests/shell/request_http/launch/test_external_promotion.py` 覆盖 initialize 回复先 flush、重发新 generation/port/token、旧 token 401、失败保留可读 bootstrap capability。

设计依据：`NATIVE-HOST-CONTRACT.md`、`BACKEND-CONTRACT.md`。

## 3. 验证

| 命令 | 结果 |
|---|---|
| `uv run --with pytest pytest tests/shell/request_http tests/integration/request_http/onboarding/test_gui_admission.py tests/daemon/ipc/contracts/onboarding tests/daemon/onboarding/test_onboarding_runtime.py -q` | **253 passed** |
| `uv run --with pytest pytest tests/shell/request_http -q`（C1a 基线） | **141 passed** |
| `uv run python contract/http-request/validate.py` | PASS |
| `check_structural_limits.py` / `check_module_boundaries.py` | PASS |
| `ruff check`（C1b 涉及目录） | PASS |
| `check_contract_impact.py --base origin/dev --head HEAD` | candidates=0, impacts=0 |
| `check_inline_comments.py --base origin/dev --head HEAD` | 0 new inline comments |
| 真实 C1b wheel preflight（W6） | **PASS**：`hostContractVersion=1`、`externalPromotion=true`、isolated daemon `ownerSeen=true`、required methods callable |

## 4. UNKNOWN / 待 backend owner 确认

1. **Windows 行为**：ACL/reparse/fsync 路径未在真实 Windows 上执行；代码是从
   15c0b3707 的 kernel seam 适配，需 Windows 验证。
2. **`requiredPhases` 的 `ready` 标签**：契约层写 `["bootstrap","ready"]`，而 dev
   daemon 的 phase 值可能是 `bootstrap|restoring|serving|reconciled`。请确认 `ready`
   是契约层标签，还是应改成实际 phase 值。
3. **`hostContract` 的位置/形状**：当前放在 `server.json` 与 `/daemon/request` 根级；
   A 的 `upstream.rs` 同时校验两者。请确认这是可接受的稳定位置。
4. **`contract/http-request` schema 变更**：catalog response 新增两个字段；是否需要
   Contract-Impact 之外的额外发布说明。
5. **C1b 测试边界**：`test_external_promotion.py` 用 FakeHost，不启动真实 daemon；A 的
   `HostState` rebind 已单测，但“C1b 真实 republish → A 页面已有 session/catalog digest
   连续性”尚未端到端验证。正式包前必须补。
6. **全量 suite**：C1b 的全量 pytest 未由子代理完成；本次独立跑了 253 个 focused tests，
   未宣称全量 `pytest tests/` 通过。后端 owner 合并前建议跑全量。

## 5. 后续

- C1a+C1b 已完成并推送 `d8fed9925`；本文件可作为 backend owner 的审阅说明。
- A 侧已具备 `hostContract` 校验、`transportGeneration`/`Upstream::rebind()` 和
  owner transition；A config 已声明 `nativeHost.promotion=external`。
- 建议：backend owner 先复核 C1 分支并跑全量 pytest；确认 `requiredPhases` 的 `ready`
  标签、`hostContract` 位置和 OpenAPI 变更。
- 合入仍走 backend owner / verifier，不自行合入；PR 在 owner 确认后创建。
