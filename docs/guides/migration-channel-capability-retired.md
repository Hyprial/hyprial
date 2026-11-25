# 迁移：`channel` 能力与 `CHANNEL_NOT_AUTHORIZED` 退役（2026-11-24）

I4b 退役 `route:<adapter>:<route>` 目标后（见[route 地址退役](migration-route-uri-retired.md)），
`channel` 能力不再有任何执行入口，`CHANNEL_NOT_AUTHORIZED` 也不再有产生点。owner 2026-11-24
批准一并退役。没有迁移代码；按下面的说明处理调用方与已有授权即可。

## 1. 删除了什么

- 能力词表：`channel` 从授权台账可接受的能力、`GRANTABLE_CAPABILITIES` 与
  `CAPABILITY_ENTRY_POINTS` 中删除；identity 公共面不再导出 `check_channel`。
- 线路错误码：冻结码 `CHANNEL_NOT_AUTHORIZED` 从码表、`message.send` 的错误声明、
  HTTP 状态映射（`contract/http-request/http-contract.v1.json`、
  `contract/daemon-request/http-envelope.schema.json`）与 GUI 客户端中删除。
- `agent.grant.check` 结果里的 `entryPoints` 不再有 `channel` 键。

## 2. 新的调用会怎样

| 调用 | 结果 |
|---|---|
| `hyprial agent grant <actor> --capability channel --scope ...`（`agent.grant`） | `INVALID_ARGUMENT`（unknown capability），不写台账与 journal |
| `hyprial agent grant <actor> check --capability channel ...`（`agent.grant.check`） | `ENFORCEMENT_SCOPE_INVALID`（CLI `--json` 为 `enforcement_scope_invalid`），unknown capability |
| 依赖 `CHANNEL_NOT_AUTHORIZED` 的客户端分支 | 不会再出现；`route:` 目标本身由 I4b 以 `UNSUPPORTED_TARGET` 拒绝 |

## 3. 已存的 `channel` 授权

升级前记录过的 `channel` 授权行留在 `agents.sqlite3` 中，不迁移、不改写：

- 读取台账时跳过：`agent.grants`（`hyprial agent grants`、`agent grant <actor> list`）不再列出它；
  发送、`targets`、组织共享目录读等判定也不再读它。跳过只会收窄判定，不会放宽。
- 仍可按 grant id 撤销：`hyprial agent grant <actor> revoke <grant-id>`，journal 记下原始
  capability 与 scope。
- grant id 可从审计历史查到：`hyprial agent grant <actor> list --audit` 中 `capability: "channel"`
  的 `grant` 行。

- 跳过的行不写日志，也没有跨 actor 的汇总查询：要确认某个 actor 是否还留有旧行，只能逐个
  actor 查 `list --audit`。
- 被跳过的行仍占着它的 grant id：用 `--grant-id` 显式复用该 id 授予新能力时，revision 不高于
  旧行会被拒绝（revision must increase），高于旧行则覆盖旧行，journal 只记新的 `grant`。
  不显式指定 `--grant-id` 时（默认生成新 id）不会遇到。

建议升级前先撤销 `channel` 授权；不撤销也不影响任何功能，只是留一条被忽略的旧行。

此前（升级前的版本）台账里出现不认识的能力行时，读取会整体失败（`agent.grants` 报
`INVALID_ARGUMENT: unknown capability`，hosted 访客的发送等调用内部报错，且该行无法撤销）。
本次把这一读取规则改为跳过，适用于所有不在词表中的能力行。

## 4. 替代

`channel` 退役没有新增替代能力。对外发消息的限制仍由已有的 `send-to` 授权决定（按目标
principal URI 判定，见 `tests/daemon/application/ports/test_daemon_visibility_wiring.py`）；
推到哪个 chat 由发送方 cell 的 `on: "user"` 连接决定（见 route 地址退役迁移说明第 1 节）。
