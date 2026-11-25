# 迁移指南：send-link 与 thread 公开面退役（T2c）

适用：从仍提供 send-link 与 thread 公开面的版本升级到本版本。本版本明确退役 `link.*`、`threads.*`、`agent link` 与 `message thread`，并同时移除相关外显字段、错误码和 hook 事件。
本版本不提供兼容别名、兼容字段迁移或自动配置文件改写；以下变更需调用方与运维脚本手工适配。

公开面退役后，`link.*` 与 `threads.*` 不再是 daemon 请求，`agent link` 与 `message thread` 不再是 CLI 命令。持久化 `thread_id`/`threadId` 归属戳以及网络传输层 `inbox/links` 仍是内部兼容边界，继续用于投递、信封和记录归属。

## 1. 移除的已发布接口与字段

### 1.1 Daemon 请求与 IPC 路由
- 删除声明与分发路由：`link.add`、`link.drop`、`link.list`、`link.stats`、`threads.get`、`threads.list`。现在调用这些方法会返回 `METHOD_NOT_FOUND`。
- 删除 `readiness` 中的 `link-service` 探针，以及 `MIGRATED_FAMILIES` 中的 `"link"` 家族闭合项与 `request_catalog` 下的 `link.py`、`link_examples/`、`threads/`。
- 删除 `contract/wire/send-links.md` 与 `contract/wire/send-links.json`。
- 删除 `hyprial.daemon`（`daemon.interface` / `facade.types`）对 link 常量与端口协议（`HOLD_REASONS`、`HOLD_REASON_KEY`、`IPC_LINK_*`、`LinkAdmin`、`LinkChangeSource`、`LinkGate`、`LinkQueries`、`LinkSendObserver`、`TOP_FIELD_*`）的公开重导出。

### 1.2 外显响应字段与过滤参数
- `message.send` / `message.query` / `outbox.list`：移除外显 `holdReason`、`detail`、`evicted` 字段，并移除 `message.send` 对 `LINK_WINDOW_FULL`、`LINK_RECLAIMED` 的错误声明。
- `conversations.list`：移除查询参数 `threadId` 与结果行中的 `threadId`（列表游标亦不再编码 `threadId`）。
- `conversations.history`（含 direct 与 group）：移除结果中的 `threadId`；继续保留 `kind`、`peer`、`legacyConversationId`。
- `top.snapshot` / `top.inspect`：移除 actor 行中的 `capacityW`、`outboundInFlight`、`inboundAwaitingInjection`。
- CLI：移除 `hyprial message conversation list --thread` 选项及表格 `THREAD` 列，移除 `hyprial agent top` 表格 `W` / `OUT` / `INJQ` 列；MCP `harness_send` / `harness_reply` 与 Pi bridge 工具说明同步移除 link 挂起与 `LINK_RECLAIMED` 描述。

### 1.3 退役的公开错误码（实际六项）
以下六个错误码已从 `hyprial.error.codes`、`hyprial.kernel.ipc_errors` 与 `ALL_CODES` 注册表完全移除：
- `LINK_MINTING`
- `LINK_PEER_FULL`
- `LINK_RECLAIMED`
- `LINK_SELF_FULL`
- `LINK_UNKNOWN`
- `LINK_WINDOW_FULL`

### 1.4 退役的 Hook 事件（`link.minted` / `link.reclaimed`）
- `"link.minted"` 与 `"link.reclaimed"` 已从 `hyprial.kernel` 的 `EVENT_NAMES` 中移除，旧链路服务亦不再发射这两个事件。
- **拒绝语义**：`HooksConfig.from_json` 在加载配置时若遇到事件 `"link.minted"` 或 `"link.reclaimed"`，会直接抛出 `ValueError`（`unknown hook event`），拒绝包含已退役事件的 hook 订阅，而不是静默忽略。

## 2. `conversations.list` 旧游标拒绝语义

`conversations.list` 列表游标仅编码 `v`、`participant`、`lastMessageAtMs`、`conversationId`（带 `kinds` 过滤时另加 `kinds`）。
升级前签发的旧游标中仍包含 `threadId` 键；在本版本调用 `conversations.list` 传入包含 `threadId` 的旧游标会触发严格键集校验失败，并以 `INVALID_ARGUMENT`（`"cursor is not a conversation list cursor issued for this caller and filter"`）拒绝请求。

处理方式：收到 `INVALID_ARGUMENT` 后丢弃旧游标，不带 `cursor` 重新从首页拉取 `conversations.list`。

## 3. 升级检查清单

1. **Hook 配置**：检查本机或部署环境中的 `hooks.json` 及 hook 安装脚本，删除对 `"link.minted"` 与 `"link.reclaimed"` 的订阅条目，避免启动加载抛出 `ValueError`。
2. **调用方与脚本**：移除对 `link.*`、`threads.*` RPC、`message conversation list --thread`、外显 `holdReason`/`detail`/`evicted`/`threadId`/`capacityW`/`outboundInFlight`/`inboundAwaitingInjection` 及六个 `LINK_*` 错误码的依赖与断言；`message.send` 失败与排队仅依据 `ok`、`accepted`、`queued`、`code`、`reason`。
3. **分页缓存**：清空或重建跨版本持有的 `conversations.list` 游标。

## 4. 保留不变的内部边界

- 网络层 `inbox/links/` 传输与持久层归属戳（`inbox` / `outbox` / 信封与记录中的 `thread_id` / `threadId` 内部归属键与派生键 `keys.py`）保持不变，不代表这些键重新成为公开字段或命令参数。
- `conversations.list` / `conversations.history` 继续保留 `kind`、`peer`、`legacyConversationId`。
- 消息体与答复机制不变：`message.read_body`、`message.settle_hint`、`SettleHint`、`SUMMARY_MAX_BYTES`、`SUMMARY_REQUIRED_DATA_KEYS`（均继续由 `hyprial.kernel` 导出并在 `hyprial.daemon` 保留重导出），以及发送边界的 `replyTo` 校验与回执语义保持不变。
