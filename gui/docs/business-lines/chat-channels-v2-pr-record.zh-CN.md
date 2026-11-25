# chat-channels GUI v2 叠加 PR 记录

- 基线：`feat/gui-chat-channels`，已包含 `intg/gui-v1` 的 `f74436363`（Org #2305、Agent #2270）及首批聊天线的布尔 schema、shadow 分派修正和持久会话目录 `message.conversations`；目标仍为该首批分支。
- 本批范围：adapter 启停与重新装载真实生命周期方法、`ADAPTER_NOT_FOUND` HTTP 状态修正。
- C01 `message.status` 全链至 `ed2d1164f` 和 C02 `message.pending.batch` 全链至 `e32c56917` 均沿用基线，不重新实现；C01/C02 仍为 IPC-only，C02 的 `mutates=true`、`retry=never` 不变。

## 方法清单

| 方法 | 接入 | HTTP | GUI |
| --- | --- | --- | --- |
| `message.send.lookup` | 复用首批 MethodSpec/真实 handler | 可信 owner 用户只读 | 聊天控制器继续使用可信会话宿主对账；直接切换生成客户端会在 agent `sessionRef` 发送场景查错身份，待同一发送方/会话证明后接入 |
| `adapter.start` / `adapter.stop` / `adapter.reload` | 本批声明并绑定现有 daemon 生命周期 handler | H-03、local-operator、`mutates=true`、`retry=never` | 生成客户端 → feature API → 渠道状态组件；成功后重新读取列表，模糊结果显示状态未确认 |
| `adapter.list` / `adapter.status` | 复用首批只读方法 | H-01 | 状态刷新；`ADAPTER_NOT_FOUND` 的已派发业务错误映射 HTTP 404 |

## 验证与限制

- TDD：新渠道生命周期动作、detached worker 停止入口、unknown 后禁止重复写及 `ADAPTER_NOT_FOUND` HTTP 404 均先见失败后实现。曾为 `message.send.lookup` 控制器接线写失败测试并实现，但独立评审发现身份不匹配场景后撤回该改动。
- Node 24.20.0：渠道焦点 Vitest 7/7 PASS；dev-03 组件浏览器旅程 1440/760/375px 3/3 PASS（新增渠道停止、启动、重新装载交互）；类型检查 PASS；重放后完整 `npm --prefix gui run verify` 85 文件、907 测试及浏览器/构建链 PASS；agent-provider 浏览器补充检查 1/1 PASS。
- 重放到 `3d3bd2a5d` 后，Python 消息/C01/C02/adapter/MethodSpec/HTTP 组合 114/114 PASS，其中隔离真实 daemon + HTTP 入口文件 11/11 PASS，包含三个 adapter 写方法可用、未授权写请求派发前拒绝、缺失 adapter 已派发 404、真实 `adapter.reload` 返回空装载清单；HTTP 错误焦点 15/15 PASS。全仓 `pytest tests/` 尝试在约 3% 时因期间代码继续修订而中止，未得全量结论。
- OpenAPI 导出检查、reference client 生成检查、结构门、模块边界门、Ruff 全仓、`git diff --check` PASS。
- 隔离真实 daemon/HTTP 生命周期写调用：PASS（无配置的 adapter 缺失错误、空装载和下方补充的已配置 worker 生命周期）；detached worker 的既有 IPC 停止回归 1/1 PASS。真实 Feishu 用户授权：BLOCKED（后端无公开 daemon 方法）；agents 权威 fixture：BLOCKED（基线中不存在）；跨节点发送：UNKNOWN。
- 发送/reply/read_body/ack 的完整 MethodSpec 迁移和组件写链仍需后续 PR，现有正式聊天发送经既有 live host 与真实 daemon IPC；未将 mock 当成 live 验收。
- 本地独立代码评审发现并推动修复 detached 停止、unknown 后重复写两项问题；`codex-daemon-request` 独立方法评审尚待完成，本批方法需在合入前补评审。
- Forgejo 当前把 #2279/#2285 的 CI 汇总标为 failure；`tea` 读取 job 列表时遇到服务端数组与客户端预期对象不匹配，失败原因未确认，不能据本地自检声称远端 CI 通过。

## 已配置 adapter 隔离真实入口复验（2026-11-03）

- member-a 的 doer 对固定 head `99f456f34` 的评审指出原真实 HTTP 测试仅覆盖缺失名称和空配置 reload。新增隔离 `channels.json` 与 `lark-*` 凭据引用，使用测试 worker 发出真实 readiness 信号；daemon、AF_UNIX、HTTP、生命周期 Registry 与 handler 均为真实运行路径，不涉及外部飞书授权。
- 已配置 `ready` 的 start→status→重复 start→stop：重复 start 返回 `changed=false`，worker 启动记录仅一次；失败 worker 的 start 返回已派发的生命周期错误；慢 worker 的 HTTP 客户端超时后只轮询 status 确认，启动记录仍只有一次，不重发写请求。三个场景独立焦点均 PASS；叠加 #2279 后的会话、MethodSpec、真实 HTTP 组合 46/46 PASS。
- 真实飞书 user-auth/scopes 仍 BLOCKED；真实外部 Lark websocket、非空 live 消息和跨节点仍 UNKNOWN。已叠加 #2279 新会话目录 head；组合回归 46/46、OpenAPI `--check`、生成客户端检查、结构门、模块边界、Ruff 均 PASS。Node 24.20.0 的 GUI 全量 verify 退出 0：85 文件、908 Vitest、浏览器 5/5、构建和后续浏览器门通过。

## 最新 GUI 集成基线叠加（2026-11-03）

- 目标基线 `intg/gui-v1` 为 `f74436363536bd7ae03f4db7c23b0ce5c35443bd`，含 Org #2305、Agent #2270；`#2279` 先通过 `d283487048f83443cb9f666c54394c3b4b04b4f3` 合入该基线，再由 `9ab7f5ccd` 修正真实 HTTP 就绪。`#2285` 经 `07cbe5bd3` 与 `0cc8a50a2` 叠加这两个提交；OpenAPI、reference client 与开发示例由完整 MethodSpec 重生成。
- 合并测试冲突时保留已配置 adapter 的真实 start→status→stop、失败和客户端超时不重写三个场景，并将 adapter.start/stop/reload 纳入最新 HTTP 目录及 H-03 断言。同步后的最终门禁结果另记；先前基线上的 PASS 不自动迁移为本次结论。
- Org/Agent 基线扩充了 HTTP 入口测试，叠加 adapter 用例后单文件 1014 行触发结构门。将 adapter 真实入口 fixture 与三个场景移到 `tests/integration/request_http/adapter/test_adapter_http_live.py`；测试语义保留。该目录也避免原目录直接文件数超过 7。
- 最终叠加树的已配置 adapter 真实 daemon/HTTP 三场景 3/3 PASS（ready 的 start→status→stop/重复写、失败 worker、客户端超时后的状态确认且不重复写）。adapter MethodSpec/HTTP 目录组合 25/25 PASS；结构与模块边界门 0 违规、Ruff 焦点、OpenAPI `--check`、reference client `--check`、相对 `intg/gui-v1` 的 diff check 均 PASS。完整 GUI 与全仓 Python 另行记录，不以焦点结果替代。
- 整合远端 `62a747d84` 后最终合并树的 adapter/HTTP/OpenAPI 契约组合 33/33 PASS，C01 status 与 C02 pending.batch 相关焦点 36/36 PASS；结构、模块边界、Ruff 全仓、OpenAPI/client 生成检查及相对基线 diff check 再跑均 PASS。
- 最新叠加树再跑持久 `message.conversations` 目录 4/4 PASS，真实隔离 HTTP 的健康/就绪/目录与只读入口 1/1 PASS。Node `24.20.0` 完整 `npm --prefix gui run verify` 退出 0：96 文件/1084 Vitest、基础浏览器 5/5、构建与生产拷贝、first-run、safe-r1、j2/j3/j4/j5/j6/j7/j10、Org lifecycle/rework 和 linked browser 均 PASS。全仓 Python `pytest tests/` 仍未完成，结论 UNKNOWN。
