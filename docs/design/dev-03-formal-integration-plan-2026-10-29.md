# dev-03 正式聊天与飞书接线计划

交付状态：实现与本轮验证执行完成，三个草稿 PR 均面向 intg/gui-init、未合并。完整门禁仍有保留失败；勾选表示执行完成，不表示所有检查通过。最终证据见 ../notes/dev03-gui-live-2026-10-29/completion-report.md。

执行顺序：使用 executing-plans，逐项验证、逐项通过 Squire 通知；不自行合并。

目标：在根 `gui/` 中接通真实聊天、未知发送结果查询，以及已有飞书 App 的权限授权和渠道绑定。

## 边界与组合

- GUI 分支为 `feat/gui-dev-03`，后端拆为 `feat/gui-dev03-chat-backend`、
  `feat/gui-dev03-channel-backend`。三个 PR 均面向 `intg/gui-init`。
- 临时探针 `/tmp/dev03-live-probe-xBeRHQ` 不进入产品提交。新实现消费既有 Python
  AppChatAdapter 和 daemon 请求客户端，不执行 CLI 业务命令、不复制 IPC 协议。
- 启动必须显式选择 `HYPRIAL_GUI_BACKEND_DRIVER=daemon`、可信
  `HYPRIAL_GUI_PRINCIPAL`、`HYPRIAL_HOME` 和 Python 可执行文件。
  身份须匹配本机 owner；浏览器只能选择 daemon 目录中的目标，不能自报 principal。
- Vite development/preview 同源桥仅暴露固定操作列表。Node 监督 Python 子进程，
  使用有界 JSON 行传输；不开放任意 daemon 方法。请求检查 loopback Host、Origin、
  本次启动 CSRF token、体积、超时。关闭服务时回收子进程。
- `/api/gui/v1/live/bootstrap` 返回 enabled/token；POST `/api/gui/v1/live`
  接受 `{operation, params}`，返回 `{ok,result}` 或 `{ok:false,error}`。
  该边界是可替换的本地宿主适配器，不宣称姚升悦统一 HTTP 网关已经交付。

## 1. 聊天与未知结果

涉及：daemon 只读 `message.send.lookup`；shell AppChatAdapter 的
`send_lookup`；`gui/src/platform/live/` 控制器与正式 AppShell 入口；
`src/hyprial/shell/impl/gui/live/` 宿主；`gui/server/live/` HTTP 桥。

- [x] 写失败测试：daemon 接收后响应丢失、重启、outbox 删除后查询仍接受，
  身份/会话/文本不匹配不得返回接受。
- [x] 使用 sender_copies 查询原 idempotencyKey；仅精确匹配证明持久接收。
  缺记录、读失败、格式异常均 unknown，不证明没有发送，不触发重发。
- [x] 宿主发送前保存待确认 UI 请求元数据（原 key、冻结文本、目标），以 owner-only
  文件和事务保护。此记录不是 inbox/聊天记录/投递队列，从不调度重发。
  浏览器或宿主重启后恢复此元数据，只向 daemon 对账。
- [x] 正式聊天页面选择真实目标、读取权威历史、去重与翻页；切换目标丢弃迟到响应。
  接收回执不能冒充 Agent 回复；确认接受时只清除仍匹配原文本的草稿。
- [x] 未知结果保留草稿和原 key；只允许查询。明确拒绝且标记可重试时，用户主动重试。
- [x] 验证真实 daemon + 浏览器闭环与接受后丢响应；记录准确源码/运行时和未验收项。

## 2. 飞书 App 授权与切换

涉及：daemon 公共 `create_lark_authorization_service`；identity pin CAS；
shell 飞书投影；GUI `LarkChannelView` 控制器。

- [x] 写失败测试：授权成功后重读权限、拒绝/取消/过期、无效 inventory、并发绑定冲突。
- [x] 复用已有 App 的 scope 授权接口。同步 SDK 在有截止时间的 owned 子进程运行；
  URL/过期时间可返回，device_code/secret 不返回。这里只做机器人 App 权限，不做用户 OAuth。
- [x] pin/unpin 加可选 expectedActor，比较和更新在同一事务；GUI 必传，旧 CLI 兼容。
  比较当前值，不宣称具备 ABA 版本检查。
- [x] 切换前展示旧/新 Agent 及确认；未知结果只重读绑定。已连接必须同时满足
  权限已授权、绑定为所选 Agent、adapter 在线；三种事实分别显示。
- [x] 隔离假 SDK/真实本机协议测试；真实飞书用户授权需用户完成浏览器流程，
  未实测不得用 mock 结果替代。

## 验证与交付

- [x] 先直接焦点测试，再 GUI verify、Python 全套、Ruff、结构/模块边界、contract impact。
- [x] 用独立组合候选验证三个分支；保留各 PR 的确切 commit 和依赖顺序。
- [x] 独立审查、修复发现，更新 #2062 并创建独立后端 PR；不合并。
- [x] 每个可验收交付完成后 Squire 通知，并核对实际飞书送达结果。

已知后续项：通用 message.send 相同 key 的跨 sender/conversation 重放及并发语义
需要独立修复；本轮 GUI 冻结请求、不自动重发未知操作，不以本机互斥代替全局幂等。
统一公共客户端 A05 与 HTTP 网关 ready 提交未获协调确认，不能写成已完成依赖。

## 后续验收（未完成）

- [ ] 真实飞书租户审批与生产等价渠道切换。
- [ ] Windows/macOS 原生进程生命周期。
- [ ] 由独立验收方处理保留的上游/环境门禁失败，确认合并条件。
