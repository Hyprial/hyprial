# App Core 导航与 CLI 能力矩阵（2026-10-17）

## 目的与边界

本文件记录 v11 桌面导航/聊天/通讯录切片开始前的只读核对：CLI 命令、节点侧 API、
App Core 注入端口、GUI 可见面与桌面/移动能力之间的对应关系。核对方法是读取集成
head 的 CLI help 源码与适配器实现，**没有调用任何生产写命令**（未运行 `send`、
`agent create`、`start`、`login` 等）。

结论只描述当前 head 的真实能力。GUI 不自行补协议、不伪造 Agent 资料、不把
“已受理”显示成“已完成”。

## 当前链路

```
CLI / 其它入口
      │
      ▼
daemon IPC（message.send / message.query / progress.list / agent.list …）
      │
      ▼
Python App Adapter（src/hyprial/shell/impl/gui/adapter/chat.py）
  sessions.list / history / events / send + capabilities.get
      │  AF_UNIX（local-ipc）或 TCP facade
      ▼
Tauri 命令 app_core_call（app/src-tauri/src/core/mod.rs）
      │  window.__hyprialAppCore = { port, transport }
      ▼
静态 App Core（src/gui/core）→ 浏览器/WebView 页面
```

浏览器只拿到五个命名操作，没有通用命令通道；身份在浏览器之外绑定，URL 或请求里
携带身份字段一律拒绝（`src/gui/core/connection.mjs`、`adapter/chat.py`）。

## 命令 → 端口 → GUI 能力矩阵

| CLI 命令 | 节点侧能力 | App Core 端口 | GUI 可见面 | 桌面 | 移动 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `agent list` | `agent.list` | 无 | 本机 Agent 卡片（仅管理窗口，经 `__hyprialPowerPack.status` 白名单投影 `actor/status`） | 管理窗口可用；聊天窗口不可用 | 不可用 | 部分支持，字段有限 |
| `agent create --name` | `agent.create` | 无 | 本机 Agent 创建字段（仅管理窗口，经 Power Pack `create`） | 管理窗口可用 | 不可用 | 部分支持 |
| `start <harness> --name` | 启动 harness connector | 无 | 本机 Agent “启动”动作（仅管理窗口，经 Power Pack `start`，固定 `--headless`） | 管理窗口可用；交互式启动不可用 | 不可用 | 部分支持 |
| `agent destroy` / `agent unblock` | 对应 daemon 管理面 | 无 | 无 | 不可用 | 不可用 | 缺口：GUI 不提供 |
| `send --from --to …` | `message.send` | `sessions.send` | 聊天输入框与发送按钮 | 支持（绑定会话内） | 支持（同一端口，传输不同） | 支持；受理不等于完成 |
| `query <actor> inbox/outbox` | `message.query` | `sessions.history` | 聊天历史、加载更多 | 支持（仅固定 sender 与配置会话） | 支持 | 部分支持：只投影仍可读的行 |
| 运行进度（事件流） | `progress.list` | `sessions.events` | 进度文本、终态、自动刷新 | 支持 | 支持 | 支持 |
| `reply` / `ack` | `message.reply` / `message.ack` | 无 | 无 | 不可用 | 不可用 | 缺口：GUI 只发新消息 |
| `ps` / `daemon status` | daemon 查询 | 无（Power Pack 内部使用 `ps`） | 本机服务状态（管理窗口） | 管理窗口可用 | 不可用 | 部分支持 |
| `doctor` | 只读诊断 | 无 | 无 | 不可用 | 不可用 | 缺口 |
| `outbox list` / `delivery status` | 投递查询 | 无 | 无 | 不可用 | 不可用 | 缺口 |
| `login` / `user` / `org` | 身份与组织 | 无 | 无（身份由原生/服务端绑定） | 不可用 | 不可用 | 缺口；浏览器不得自报身份 |
| `gui start|status|stop|upgrade` | 桌面 GUI 生命周期 | 无 | 无 | 桌面安装/启动层 | 不适用 | 页面不控制生命周期 |
| `down --all` | 停止 connector | 无 | 无 | 不可用 | 不可用 | 缺口 |

支持面证据：

- 五个操作与 v0 支持矩阵：`src/hyprial/shell/impl/gui/adapter/chat.py`
  （`V0_OPERATIONS`、`sessions_list`、`history`、`events`、`send`）。
- 请求分发与失败闭合：`src/hyprial/shell/impl/gui/adapter/host.py`
  （`capabilities.get` … `sessions.history`，未知操作 `unsupported-operation`，
  非确定性失败返回 `unavailable`）。
- Tauri 注入与传输：`app/src-tauri/src/core/mod.rs`
  （`window.__hyprialAppCore`、`app_core_call`、桌面 `local-ipc`、移动 `tailnet`）。
- 本机管理窗口：`app/src-tauri/src/power_pack/management.rs` 与
  `app/src-tauri/src/power_pack/bridge.js`（`status/create/start` 白名单，
  固定调用 `agent create` 与 `start --headless`）。
- 浏览器端口校验：`src/gui/core/connection.mjs`（仅五个操作、仅三种传输、
  拒绝身份字段）；`src/gui/core/chat.mjs`（发送/重试/不确定态语义）。
- CLI 入口源码：`src/hyprial/shell/impl/cli/commands/agent/admin.py`、
  `agent/create.py`、`agent/runtime/start.py`、`agent/runtime/down.py`、
  `message/send.py`、`message/reply.py`、`message/ack.py`、
  `log/query.py`、`message/outbox.py`、`message/delivery.py`、
  `daemon/ps.py`、`doctor/commands.py`、`gui/commands.py`。

## 已知缺口（GUI 不得自行弥补）

1. **联系人资料**：五个聊天操作里没有“ Agent 角色/模型/在线状态/可用性”字段。
   `sessions.list` 只有 `{sessionId, title?, updatedAt?, state?}`，且只返回组合根
   配置的绑定；`agent.list` 也不在聊天端口内。通讯录只能显示会话标题、
   `sessionId` 与可选 `state`，其余字段明确标为“接口未提供”。
2. **会话发现**：`sessions.list` 不是 daemon 发现结果；未配置的会话不会出现。
3. **历史完整性**：`sessions.history` 只投影固定 sender 仍可读的 inbox/outbox 行；
   已被消费的历史行没有 IPC 读取面，缺失只能如实显示为空，不能编造。
4. **管理动作**：创建/启动 Agent 只在原生管理的 Power Pack 窗口开放，且启动固定
   `--headless`；聊天主窗口与移动端没有这些动作。
5. **移动端**：端口与传输种类已声明 `tailnet`，但本轮没有真实移动节点验收，
   相关能力保持 UNKNOWN，不以 unit 测试替代。

## 本切片决策

- 桌面导航提供“聊天 / 通讯录”，不存在的“任务”等入口保留为禁用项并说明原因。
- 通讯录数据源只用真实 `sessions.list` 与（可选）`__hyprialPowerPack`；搜索与
  “当前会话”筛选是本地视图操作，不新增端口调用。创建 Agent 按钮在聊天页面禁用，
  管理动作留在已有的本机管理窗口。
- 聊天保留原有五操作、发送/重试/不确定态/仅从历史显示回复的语义；v11 只改变布局、
  导航、焦点与响应式表现。
- 浏览器不再通过 `?demo=1` 选择模拟数据；`createDemoConnection` 仅保留为显式测试
  夹具。没有注入连接时页面明确显示未连接。
- 响应式覆盖 360/390/430px：单面板切换、返回导航、44px 触控目标与
  `safe-area-inset-bottom`；不使用原型里的 `min-width: 980px`。

## 需要父级决策的接口请求

若下一阶段要在聊天/通讯录主窗口显示真实 Agent 资料或执行管理动作，需要新增一个
显式、白名单化的节点端口（例如只读的 `agents.list` 投影，或复用 Power Pack 的
受限管理面），由原生/服务端绑定调用方身份并校验窗口来源。请求应作为独立接口
切片评审，不应在浏览器里复制 CLI 参数或新增第二套命令协议。
