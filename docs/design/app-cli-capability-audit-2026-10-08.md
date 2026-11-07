# App CLI、daemon API 与身份能力审计（2026-10-08）

审计基线：官方 dev `53de6496adaa9000cbdf179cd97fcb71645aa3fa`。
交接：PR #1311，分支 `docs/gui-app-handoff` 的
`206c3b10a26cfa4b64640ed0aaa795c23670c241`。
这是代码/注册树审计，不是生产联调或完整 GUI 验收。

## 方法和结论

隔离 `HYPRIAL_HOME=/tmp/hyprial-app-cli-audit-home`，独立
`UV_PROJECT_ENVIRONMENT=/tmp/hyprial-app-cli-audit-venv`，`uv sync --frozen --no-install-project`，
`PYTHONPATH=src uv run --no-sync python` 导入
`hyprial.shell.impl.cli.runtime.entry.app`，经 `typer.main.get_command` 递归枚举注册树。
未执行命令 callback、未启动 daemon、未读取生产 home。

结果：**59 个一级入口、210 个叶子**（含隐藏内部入口）。`gui` 是带参数的叶子，
其 start/status/stop/upgrade 动作不额外计成 Typer 子命令。
PR #1126 已合并，但最初的 193 叶子、37 一级目标不是当前数字；
基线 `docs/design/cli-surface/expected-commands.md` 的目标已变为 **22 一级**。目标设计不代表已实现：
实际仍有 `send`/`reply`/`query`、`transfer-*`、`autoupdate` 等旧一级命令。

判定：有 = 当前 GUI 接线覆盖该命令；部分 = 只有相关入口/局部动作；无 = 无对应真实入口。
Mock 展示不算“有”。本次没有任何一项获得真实 backend/E2E 完整等价验收。
表内 shell 入口仅证明代码接线，平台运行结果 UNKNOWN。

当前唯一内置 GUI driver 是 `mock-fixtures`：`src/gui/transport/index.mjs:41`。
会话列表/历史/发送/取消/进度虽有 UI，均不能据此计入真实 CLI 覆盖。
Shell 账户 worker 动作为 `status/login/device/forward`
（`desktop/src-tauri/src/account/worker.rs:31`），调用独立 Python worker；
不是把全部 CLI API 化。Onboarding 只转发 `plan/apply`
（`desktop/src-tauri/src/onboarding/cli.rs`）。

## 当前全部叶子对照

命令均省略 `hyprial` 前缀；隐藏标记只表示该叶子自己的 hidden 属性。

| 当前命令 | GUI 入口/证据 | 有/无/部分 | 实现来源 |
| --- | --- | --- | --- |
| `login` | 账户窗口的 login/device 流程；非完整 CLI 等价 | 部分 | `commands/login.commands` |
| `help` | — | 无 | `commands/meta.commands` |
| `version` | — | 无 | `commands/meta.commands` |
| `install` | CLI 本身为 COMING_SOON；无对应入口 | 无 | `commands/socialware.commands` |
| `ps` | — | 无 | `commands/daemon.status` |
| `top` | — | 无 | `commands/daemon.status` |
| `doctor` | — | 无 | `commands/doctor.commands` |
| `worktrees` | — | 无 | `commands/workflow.internal.worktrees` |
| `targets` | 聊天/列表 mock；真实对应入口未接通 | 无 | `commands/host.overview` |
| `hosts` | — | 无 | `commands/host.overview` |
| `send` | 聊天/列表 mock；真实对应入口未接通 | 无 | `commands/message.commands` |
| `ack` | 聊天/列表 mock；真实对应入口未接通 | 无 | `commands/message.commands` |
| `reply` | 聊天/列表 mock；真实对应入口未接通 | 无 | `commands/message.commands` |
| `query` | 聊天/列表 mock；真实对应入口未接通 | 无 | `commands/log.commands` |
| `log` | — | 无 | `commands/log.commands` |
| `trajectory` | — | 无 | `commands/log.commands` |
| `init` | 首启窗口/plan、apply 本地转发 | 部分 | `commands/init.commands` |
| `grants-check` | — | 无 | `commands/agent.grant` |
| `grants-visibility` | — | 无 | `commands/agent.grant` |
| `start` | — | 无 | `commands/agent.runtime.start` |
| `down` | — | 无 | `commands/agent.runtime.down` |
| `transfer` | — | 无 | `commands/transfer.move` |
| `transfer-bundle-export` | — | 无 | `commands/transfer.bundle` |
| `transfer-bundle-validate` | — | 无 | `commands/transfer.bundle` |
| `transfer-bundle-receive` | — | 无 | `commands/transfer.bundle` |
| `transfer-bundle-land` | — | 无 | `commands/transfer.bundle` |
| `transfer-credential-seal` | — | 无 | `commands/transfer.credentials` |
| `transfer-credential-open` | — | 无 | `commands/transfer.credentials` |
| `transfer-bundle-complete` | — | 无 | `commands/transfer.bundle` |
| `transfer-precheck`（隐藏） | — | 无 | `commands/transfer.internal` |
| `transfer-receive`（隐藏） | — | 无 | `commands/transfer.internal` |
| `transfer-cred-stage`（隐藏） | — | 无 | `commands/transfer.internal` |
| `transfer-container-home`（隐藏） | — | 无 | `commands/transfer.internal` |
| `transfer-session-path`（隐藏） | — | 无 | `commands/transfer.internal` |
| `upgrade` | 外壳 payload 更新/状态；不等于全部 CLI 升级语义 | 部分 | `commands/upgrade.commands` |
| `restart` | — | 无 | `commands/daemon.restart` |
| `gui` | Tauri 启停自己的 GUI server；无完整 CLI 管理页 | 部分 | `commands/gui.commands` |
| `adapter add` | — | 无 | `commands/adapter.admin` |
| `adapter onboard` | — | 无 | `commands/adapter.admin` |
| `adapter remove` | — | 无 | `commands/adapter.admin` |
| `adapter start` | — | 无 | `commands/adapter.admin` |
| `adapter stop` | — | 无 | `commands/adapter.admin` |
| `adapter status` | — | 无 | `commands/adapter.admin` |
| `adapter reload` | — | 无 | `commands/adapter.admin` |
| `adapter list` | — | 无 | `commands/adapter.admin` |
| `adapter doctor` | — | 无 | `commands/adapter.admin` |
| `adapter authorize` | — | 无 | `commands/adapter.authorize` |
| `adapter pin` | — | 无 | `commands/adapter.pins` |
| `adapter unpin` | — | 无 | `commands/adapter.pins` |
| `adapter pins` | — | 无 | `commands/adapter.pins` |
| `adapter route list` | — | 无 | `commands/adapter.routes` |
| `adapter route candidates` | — | 无 | `commands/adapter.routes` |
| `adapter route add` | — | 无 | `commands/adapter.routes` |
| `adapter route remove` | — | 无 | `commands/adapter.routes` |
| `adapter identities sync` | — | 无 | `commands/adapter.data` |
| `adapter identities list` | — | 无 | `commands/adapter.data` |
| `adapter identities find` | — | 无 | `commands/adapter.data` |
| `adapter media get` | — | 无 | `commands/adapter.data` |
| `daemon status` | 外壳启动/监督本机 daemon；无完整命令管理页 | 部分 | `commands/daemon.commands` |
| `daemon run` | 外壳启动/监督本机 daemon；无完整命令管理页 | 部分 | `commands/daemon.commands` |
| `daemon stop` | 外壳启动/监督本机 daemon；无完整命令管理页 | 部分 | `commands/daemon.commands` |
| `service connect` | — | 无 | `commands/service.commands` |
| `service disconnect` | — | 无 | `commands/service.commands` |
| `service list` | — | 无 | `commands/service.commands` |
| `service guide` | — | 无 | `commands/service.commands` |
| `mcp claude-channel` | — | 无 | `commands/meta.mcp` |
| `mcp claude-channel-recover`（隐藏） | — | 无 | `commands/meta.mcp` |
| `mcp claude-turn-ended`（隐藏） | — | 无 | `commands/meta.mcp` |
| `mcp agent-channel` | — | 无 | `commands/meta.mcp` |
| `squire setup` | — | 无 | `commands/squire.commands` |
| `squire probe` | — | 无 | `commands/squire.commands` |
| `user-proxy status` | — | 无 | `commands/squire.commands` |
| `user-proxy setup` | — | 无 | `commands/squire.commands` |
| `outbox list` | — | 无 | `commands/message.commands` |
| `outbox prune` | — | 无 | `commands/message.commands` |
| `delivery status` | — | 无 | `commands/message.commands` |
| `autoupdate install` | — | 无 | `commands/upgrade.autoupdate` |
| `autoupdate uninstall` | — | 无 | `commands/upgrade.autoupdate` |
| `autoupdate status` | 外壳 payload 更新/状态；不等于全部 CLI 升级语义 | 部分 | `commands/upgrade.autoupdate` |
| `autoupdate restart` | — | 无 | `commands/upgrade.autoupdate` |
| `autoupdate run`（隐藏） | — | 无 | `commands/upgrade.autoupdate` |
| `config set` | — | 无 | `commands/config.settings` |
| `lark-auth check` | — | 无 | `commands/adapter.lark_auth` |
| `lark-auth complete` | — | 无 | `commands/adapter.lark_auth` |
| `agent create` | — | 无 | `commands/agent.create` |
| `agent host-invite` | — | 无 | `commands/agent.create` |
| `agent grant` | — | 无 | `commands/agent.grant` |
| `agent revoke` | — | 无 | `commands/agent.grant` |
| `agent grants` | — | 无 | `commands/agent.grant` |
| `agent list` | — | 无 | `commands/agent.admin` |
| `agent home-census` | — | 无 | `commands/agent.home` |
| `agent restore-policy` | — | 无 | `commands/agent.restore` |
| `agent restore-threshold` | — | 无 | `commands/agent.restore` |
| `agent unblock` | — | 无 | `commands/agent.admin` |
| `agent destroy` | — | 无 | `commands/agent.admin` |
| `agent secret provider-write` | — | 无 | `commands/agent.secret` |
| `agent secret grant` | — | 无 | `commands/agent.secret` |
| `agent secret list` | — | 无 | `commands/agent.secret` |
| `agent secret revoke` | — | 无 | `commands/agent.secret` |
| `agent migrate preflight` | — | 无 | `commands/agent.home` |
| `agent migrate execute` | — | 无 | `commands/agent.home` |
| `agent migrate rollback` | — | 无 | `commands/agent.home` |
| `agent keep add` | — | 无 | `commands/agent.restore` |
| `agent keep remove` | — | 无 | `commands/agent.restore` |
| `agent keep list` | — | 无 | `commands/agent.restore` |
| `org create` | — | 无 | `commands/org.commands` |
| `org invite` | — | 无 | `commands/org.commands` |
| `org join` | hyprial://join 深链转发 CLI；缺完整组织界面 | 部分 | `commands/org.commands` |
| `org execute` | — | 无 | `commands/org.commands` |
| `org leave` | — | 无 | `commands/org.commands` |
| `org remove` | — | 无 | `commands/org.commands` |
| `org delete` | — | 无 | `commands/org.commands` |
| `org list` | — | 无 | `commands/org.commands` |
| `org pending` | — | 无 | `commands/org.commands` |
| `org network` | — | 无 | `commands/org.commands` |
| `org show` | — | 无 | `commands/org.commands` |
| `org status` | — | 无 | `commands/org.commands` |
| `org fetch` | — | 无 | `commands/org.commands` |
| `org import` | — | 无 | `commands/org.commands` |
| `org work add` | — | 无 | `commands/work.commands` |
| `org work check` | — | 无 | `commands/work.commands` |
| `org work note` | — | 无 | `commands/work.commands` |
| `org work status` | — | 无 | `commands/work.commands` |
| `org work done` | — | 无 | `commands/work.commands` |
| `org work repair` | — | 无 | `commands/work.commands` |
| `org work link` | — | 无 | `commands/work.commands` |
| `org work keywords` | — | 无 | `commands/work.commands` |
| `org work show` | — | 无 | `commands/work.commands` |
| `org work resolve` | — | 无 | `commands/work.commands` |
| `org work ls` | — | 无 | `commands/work.commands` |
| `org work collect` | — | 无 | `commands/work.commands` |
| `org work finish` | — | 无 | `commands/work.commands` |
| `fs spaces` | — | 无 | `commands/fs.commands` |
| `fs create` | — | 无 | `commands/fs.commands` |
| `fs ls` | — | 无 | `commands/fs.commands` |
| `fs resolve` | — | 无 | `commands/fs.commands` |
| `fs read` | — | 无 | `commands/fs.commands` |
| `fs write` | — | 无 | `commands/fs.commands` |
| `fs mkdir` | — | 无 | `commands/fs.commands` |
| `fs mv` | — | 无 | `commands/fs.commands` |
| `fs rm` | — | 无 | `commands/fs.commands` |
| `fs history` | — | 无 | `commands/fs.commands` |
| `fs watch` | — | 无 | `commands/fs.commands` |
| `fs import` | — | 无 | `commands/fs.commands` |
| `fs export` | — | 无 | `commands/fs.commands` |
| `fs invite` | — | 无 | `commands/fs.commands` |
| `fs remove-member` | — | 无 | `commands/fs.commands` |
| `fs members` | — | 无 | `commands/fs.commands` |
| `fs join` | — | 无 | `commands/fs.commands` |
| `fs web` | — | 无 | `commands/fs.commands` |
| `fs trash` | — | 无 | `commands/fs.commands` |
| `fs restore` | — | 无 | `commands/fs.commands` |
| `fs status` | — | 无 | `commands/fs.commands` |
| `fs serve` | — | 无 | `commands/fs.commands` |
| `fs checkout` | — | 无 | `commands/fs.commands` |
| `fs purge-plan` | — | 无 | `commands/fs.commands` |
| `fs purge` | — | 无 | `commands/fs.commands` |
| `fs purge-status` | — | 无 | `commands/fs.commands` |
| `fs unban` | — | 无 | `commands/fs.commands` |
| `profile list` | — | 无 | `commands/profile.commands` |
| `profile show` | — | 无 | `commands/profile.commands` |
| `profile use` | — | 无 | `commands/profile.commands` |
| `profile create` | — | 无 | `commands/profile.commands` |
| `network expose` | — | 无 | `commands/host.network` |
| `network unexpose` | — | 无 | `commands/host.network` |
| `network exposures` | — | 无 | `commands/host.network` |
| `network peer-key` | — | 无 | `commands/host.network` |
| `transfer-runtime downgrade-state` | — | 无 | `commands/transfer.runtime` |
| `transfer-runtime probe` | — | 无 | `commands/transfer.runtime` |
| `workflow plan` | — | 无 | `commands/workflow.run` |
| `workflow run` | — | 无 | `commands/workflow.run` |
| `workflow status` | — | 无 | `commands/workflow.inspect` |
| `workflow list` | — | 无 | `commands/workflow.inspect` |
| `workflow inspect` | — | 无 | `commands/workflow.inspect` |
| `workflow cancel` | — | 无 | `commands/workflow.inspect` |
| `workflow events` | — | 无 | `commands/workflow.inspect` |
| `workflow context` | — | 无 | `commands/workflow.inspect` |
| `workflow reset` | — | 无 | `commands/workflow.control` |
| `workflow complete` | — | 无 | `commands/workflow.control` |
| `workflow fail` | — | 无 | `commands/workflow.control` |
| `workflow gc` | — | 无 | `commands/workflow.misc` |
| `workflow history list` | — | 无 | `commands/workflow.inspect` |
| `workflow history status` | — | 无 | `commands/workflow.inspect` |
| `workflow overview show` | — | 无 | `commands/workflow.overview` |
| `workflow overview publish` | — | 无 | `commands/workflow.overview` |
| `workflow worker stop` | — | 无 | `commands/workflow.control` |
| `workflow worker restart` | — | 无 | `commands/workflow.control` |
| `workflow notify resend` | — | 无 | `commands/workflow.control` |
| `workflow migration status` | — | 无 | `commands/workflow.control` |
| `dispatch matrix` | — | 无 | `commands/workflow.internal.dispatch` |
| `routine plan` | — | 无 | `commands/routine.commands` |
| `routine add` | — | 无 | `commands/routine.commands` |
| `routine list` | — | 无 | `commands/routine.commands` |
| `routine status` | — | 无 | `commands/routine.commands` |
| `routine rm` | — | 无 | `commands/routine.commands` |
| `routine set` | — | 无 | `commands/routine.commands` |
| `routine pause` | — | 无 | `commands/routine.commands` |
| `routine resume` | — | 无 | `commands/routine.commands` |
| `routine templates show` | — | 无 | `commands/routine.commands` |
| `onboarding plan` | 首启窗口/plan、apply 本地转发 | 部分 | `commands/init.commands` |
| `onboarding apply` | 首启窗口/plan、apply 本地转发 | 部分 | `commands/init.commands` |
| `user list` | — | 无 | `commands/user.commands` |
| `user bind` | — | 无 | `commands/user.commands` |
| `user unbind` | — | 无 | `commands/user.commands` |
| `user whois` | — | 无 | `commands/user.commands` |
| `user add` | — | 无 | `commands/user.commands` |
| `user show` | — | 无 | `commands/user.commands` |
| `user bindings audit` | — | 无 | `commands/user.commands` |
| `user bindings withdraw` | — | 无 | `commands/user.commands` |
| `user bindings publish` | — | 无 | `commands/user.commands` |

## 最小聊天可复用 API 与缺口

| 能力 | 当前源码证据 | 对 App 的结论 |
| --- | --- | --- |
| 发送文本 | IPC router 的 `message.send`，`application/messaging/inbox_surface/send.py:64` | 可复用 daemon 投递；必须在可信 adapter/native bridge 绑定身份，不能 WebView 自报 from |
| 重试幂等 | 同文件 `idempotencyKey`，约第 148 行 | 有基础操作键；App 需定义设备命名空间、持久重试及冲突语义，不能随机换键重发 |
| 回复 | `message.reply`，同文件约第 476 行 | 当前为 pending message 回复，不能冒充任意历史消息续聊接口 |
| 状态/进度 | router `message.status` / `progress.list` | 可复用对应读面；不等于已存在 UI turn/session stream |
| 只读观察 | `message.query`，`inbox_surface/queries.py:426` | 只读 pending inbox 与 outbox，不做 fetch/ACK；**不是完整聊天历史** |
| 消费与 ACK | `message.pending.list/wait`、`message.ack` | 属 adapter/agent 消费契约；只看 UI 不能触发，尤其 pending.list 会 drain notices |
| 目标与进程 | `agent.list`、CLI targets/ps、daemon status | 可做目标发现/状态投影，但须加 App 用户授权范围 |
| 历史和多设备游标 | 当前未找到 App 公共 API | daemon/adapter 侧需耐久投影、分页、保留、每设备补拉游标；禁止浏览器另立权威库 |
| 跨端已读/推送 | 交接明确待设计 | 已读与消费 ACK 分开；MVP 可先仅前台补拉，推送只发唤醒信号 |
| 通用 chat cancel | 未发现与 GUI cancel 等价接口 | workflow.cancel 只取消 workflow；不可当取消聊天 turn |
| 远程命令 API | 现有 CLI 多数走本地 IPC | 不可直接把整个 IPC 端口暴露 tailnet；需有界方法白名单、身份、授权、错误映射 |

建议小 PR 顺序：C1 Core 静态入口/连接端口；C2 身份就绪后受限命令桥与目标读取；
C3 App adapter 发送/回复回流；C4 耐久历史、每设备游标与幂等恢复；C5 移动 tailnet
原生绑定与重连。审批、OrgFS、任务等表中剩余无项按需后续 PR，不能承诺首轮全覆盖。
公共 daemon API 由其 owner 协调实施，不在本 GUI 审计中改写。

## 0.5.5 身份依赖：尚未落到审计基线

以下均为 2026-10-08 只读查询的 **Open** PR；只记录读取时 SHA，不声称最终冻结。
实施认证之前必须重读最终合并版本。链接统一在
`https://code.hyprial.com/HyprialOS/harness-bridge/pulls/<编号>`。

| PR | 读取的分支 HEAD | 对 App 的影响 |
| --- | --- | --- |
| #1290 N-A1 + N-A2（标题仍只写 N-A1） | `7ba43fa5671904168efc0bf91d28926f2dc2d188` | invite token 与 orgfs 请求 MAC 在同一个 PR |
| #1293 derive 前置 | `060268eb65cad5be861eca2bcd91ee6f9a0250a5` | sidecar 派生请求密钥；已被 #1290 分支合入，尚非 dev |
| #1294 per-service admission | `545dcb67b0bfff96c6092c9a6074eb8e9de4fc76` | v4 expose admission；join endpoint 与受限服务分开 |
| #1298 S2b peer access | `0386f1f646734606955d7062ecdede39f6be2ebe` | 目录驱动准入、born deny-all；owner 暂有 join-endpoint 宽限，须同批升级 |
| #1284 Casdoor S1 | `abff3d6a841bb8e761eb99074166535016da9d3a` | 组成员权威及组织操作排序；App 不保存服务端凭据 |
| #1287 Casdoor S2 | `2a4c80f69506c310e5239057a73903ab299f1cf7` | owner 同步 actor/webhook，移动端不承担 |

读取 #1290 的 `src/hyprial/daemon/impl/orgfs/network/auth.py`：

- `RequestSigner.sign(space_id, method, payload, supplier=...)`，定向请求
  auth `{deviceId, ts, nonce, mac}`，广播使用 `macs: {deviceId: mac}`。
- `payload_digest` 排除 auth，对 protocol `_json_bytes` 结果 SHA256；
  MAC 是 HMAC-SHA256，文本为 `ts|nonce|space_id|method|digest`。
  跨语言编码、时间浮点表示和 canonical JSON 必须消费最终契约向量，不自行猜测。
- `derive(role, peer)` 的 key 使用 X25519 后 HKDF-SHA256，空 salt，info 为
  `hyprial-orgfs-req-v1` 加原始 clientPub 再 serverPub（固定顺序）。原始 DH 不出 sidecar。
- verifier 从设备行 owner 确认身份，拒绝 author 不匹配、±120 秒以外、nonce 重放；
  没有 derive capability 报 `SIDECAR_DERIVE_UNSUPPORTED`，不降级到未签名。
- 未登记设备的 join 窗口靠 invite token，不用“自报作者”宽限。
- **最新代码与 PR 正文存在过时描述**：`af3995bc7` 已撤销启动自动删除预写成员行，
  改为 `staleInviteeRows` 只读报告和管理员显式处理。不要照旧正文实现自动清理。
- 该实现当前覆盖 orgfs sync/frontier/log-range/blob，**不是现成通用 App 聊天 HTTP 鉴权**。
  App 要复用设备登记/派生/验签权威，需 daemon owner 提供受支持公共边界；
  不跨 Python impl 导入、不在 WebView 放密钥、不把 bearer mock token 当 N-A2。

#1294 仅包含 Go 侧 join endpoint 准入，daemon endpoint 最终接线本次未定位到独立最终 PR，
必须在认证/入组实现前补齐依赖确认。所有上述身份/网络项受 0.5.5 同批升级约束，
不能挑一部分装进冻结的 0.5.4。

`mobile-adapter-sync-brief-2026-10-08.md`：在当前仓库、已知远端 refs 历史、
交接分支、刷新后的 socialware design 分支及本地两处 docs 检索均未找到；
这是未取得的输入，不声称已经阅读。交接中的多设备/已读约束已列为设计缺口。

## 验证范围

PASS：隔离注册树枚举、逐叶子对照、身份 PR 与当前代码只读核对。
UNKNOWN：真实聊天、移动编译/运行、远程认证联调、跨设备恢复。
本变更仅添加审计文档，没有业务代码；不把上游 PR 自报测试当本次亲测。
