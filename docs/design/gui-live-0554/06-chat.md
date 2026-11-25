# chat 线：开发要求与 prompt

负责人：**lizhenyu**。工作包：**C0/C1**。
统一审核：codex-daemon-request；合入统筹：emergent（coordinator / linyilun）。
先读 [总架构](00-architecture.md)，本线开发遵循 [并行与合入流程](01-coordinator.md)。

## 1. 当前能力、缺口与具体修改文件

路径约定：request_catalog/、application/、ipc/ 均展开为 src/hyprial/daemon/impl/；shell/ 位于 src/hyprial/；user-settings/ 位于 gui/src/features/。表中省略前缀的文件沿用所在 feature 目录，公共链路的具体文件见 [公共线](02-common.md)。

| 现有操作 | GUI / bridge | MethodSpec/HTTP 与实际执行 | 修改 |
|---|---|---|---|
| targets.list | `gui/src/adapters/live/client.ts` → `gui/server/live/index.mjs` → `src/hyprial/shell/impl/gui/adapter/chat.py` | hosts 有 Spec；targets 为 legacy IPC，无对应 Spec | 给真实 targets 结果补声明/最小暴露或改用已足够的 declared agent.list；不能丢目标范围/可信 sender 语义。绑定既有 `daemon/impl/application/messaging/status/views.py` |
| sessions.create | 同上 | 当前桥按 sender/target 派生稳定 session/conversation key，不是已声明 daemon 方法 | session 标识算法/权威放在 daemon 契约里，或证明是纯无状态客户端投影且不存第二份会话；不得在宿主继续拥有业务 session authority |
| sessions.history | live client/chat-controller | message.history 已有 Spec/HTTP | 直接复用生成方法，不保留 bridge alias |
| sessions.send | `gui/src/adapters/live/chat-controller.ts`；`shell/impl/gui/live/{runtime.py,backend.py,journal.py}` | message.send 有真实 IPC handler，未登记对应 Spec/HTTP；handler `daemon/impl/application/messaging/inbox_surface/send.py` | **新增子包** `request_catalog/message/send/{__init__.py,specs.py,examples.py}`；声明可信 sender、幂等 business key、接受/落库/投递语义、回执/unknown；不做无条件重试 |
| sessions.send.lookup | 同上 | **message.send.lookup 已有 HTTP**，声明位于 `request_catalog/daemon/message_lookup.py`；绑定 owner user selector，不接受任意浏览器 actor；结果为 accepted 或 unknown，unknown 的 reason 包括 not-found/invalid-record/request-mismatch/unavailable | 用生成方法，精确保留现有两种状态与 reason；not-found 不等于可以重新发新业务消息，禁止杜撰 pending/confirmed 为该方法已有枚举 |
| events/progress、sessions.list | `shell/impl/gui/adapter/chat.py` | adapter 有 sessions.events→progress.list 与进程内 sessions.list；但 `gui/server/live/index.mjs` 的 OPERATIONS 不暴露这两个操作，不能把 adapter 能力当作普通浏览器已接线 | 需要的 progress 查询补 Spec；无要求不新建推送协议。handler `daemon/impl/application/messaging/inbox_surface/queries.py`。会话列表必须来自 daemon 历史/声明的查询，不能把进程内 bindings 当完整 durable 会话目录 |
| pending/confirm/reload | `shell/impl/gui/live/journal.py` | GUI IntentJournal 维护桥接恢复状态，不是已声明后端事实 | 迁移为 daemon receipt/lookup 可恢复的操作状态；浏览器可保留不含秘密的 operationKey，但不能成为是否投递的权威 |
| Lark list/status | `gui/src/adapters/live/lark-controller.ts`；`gui/src/features/chat-channels/AdapterStatusSection.tsx` | adapter.list/status 已有 HTTP | 使用统一 generated client |
| Lark pins/pin | 同 bridge，`daemon/impl/application/actors/agents_admin/adapters.py` | adapter.pins/pin 是 legacy IPC；写入有 expectedActor/CAS 语义 | 补 Spec，保留 CAS/冲突错误，不降成最后写覆盖 |
| Lark authorization status/start/operation/cancel | `shell/impl/gui/live/channels.py` | 直接调用 `daemon/impl/adapters/lark/credentials/authorization/service.py`；并未声明为这组 HTTP 方法 | 如果属于本轮可用范围，daemon 暴露受控 auth operation 契约；否则清楚 unavailable。凭据仍只在后台，不能给 host 第二份授权权威 |

chat 产品迁移的明确 GUI 文件：`gui/src/pages/live/{runtime.ts,LiveChatPage.tsx,LiveLarkPage.tsx}`、`gui/src/adapters/live/{client.ts,chat-controller.ts,lark-controller.ts,index.ts}`、`gui/src/features/agent-chat/api/{chat-controller.ts,use-contract-runtime.ts}`、`gui/src/features/chat-channels/{message-read-api.ts,api.ts}`。

新增统一 chat 薄适配文件建议为 `gui/src/features/chat-channels/daemon-api/{index.ts,chat.ts,lark.ts}`。不要另起 /api/chat。先迁移并做语义等价验证，再单独 PR 删除旧产品 live bridge 依赖；最终普通入口不能同时启用旧 bridge 与新链路。

## 2. 可以并行开展的工作

C0 的本线业务 Spec/handler/幂等负例与 C1 adapter/控制器迁移同时开展；已有 message.history、message.send.lookup、adapter.list/status 可先接。message.send/targets/progress/pins 与 Lark auth 缺口由本线补本域契约，公共线审核核心接入/身份上下文；不要把所有业务实现排到公共线之后。GUI 可以使用经审核的契约测试替身开发，真实发送验收等待公共 client 与相应后端实现。给 agent 线稳定的 agent-chat 控制器导出，避免其另建 chat 网络层。

## 3. 文件责任与接口交付

本线拥有 pages/live、adapters/live、features/chat-channels、features/agent-chat/api 和本线 request_catalog/message/send、message/directory、adapter 业务契约扩展。核心 MethodSpec/registry/统一 transport 归公共线；本线负责业务协议与 handler 语义、公共线负责核心登记协调。旧 bridge 业务只在迁移与回归完成后单独 PR 删除。

交付 adapter 导出类型、需要的生成方法集合、错误/receipt/phase 依赖、本线方法工厂和必要注册项。新增契约先固定 schema/examples/授权/幂等，再生成 client；不得以页面 DTO 代替 MethodSpec。接口变化写入本线 PR，让 coordinator 更新依赖，不能由消费线私改同名类型。

## 4. 验证与交接

真实发送接受/查询、unknown 后刷新与重试不重复投递、稳定业务 key、lookup unknown reason、不存在目标、跨 sender/home 拒绝、Lark CAS/冲突、权限、会话恢复。不得用 IntentJournal 或 HTTP200 判断投递成功；主 chat 与 agent-chat 共用控制器和证据。

通用 G1–G8 与命令见总架构。daemon 与 Contract Mock 必须使用同一构建、普通产品页面、adapter/client/URL；Contract Mock 与仅测试替身不算真实 backend 证据。先焦点红→绿，再全量适用检查与 CI；外部能力不可访问写 BLOCKED，未测写 UNKNOWN，skip 不算 PASS。完成后提交固定 SHA 给 codex-daemon-request 审核；REWORK 由本线修复，审核通过由 emergent 合入。

## 5. 可直接分派的 prompt

> 负责人 lizhenyu，你承接 chat 线（C0/C1）。以总架构的固定 source SHA 为事实起点，执行从 coordinator 公布的 intg/gui-live-0554 固定 head 建本线短分支，PR base 为该容器。只实现本线范围，不直接推 intg、不发布、不代 merge、不访问日常 home/凭据/daemon。所有后端业务经注入的同一 GeneratedClient → HttpTransport → 同源转发 → HTTP FastAPI → daemon request → daemon 方法；MethodSpec 是契约唯一源。没有方法/契约就明确 unavailable，不能前端 fixture 成功兜底。模式只由宿主 HYPRIAL_GUI_BACKEND_DRIVER=daemon|contract-mock 决定，功能层不配 URL/token/principal。共享文件由公共线装配，生成物由生成器产生。可以在冻结接口上与其他线并行实现并使用仅测试替身；最终 live 验收依赖公共运行时，替身不算 PASS。提交精确 SHA、PR、改动路径、红→绿测试、CI、浏览器证据和未覆盖项给 codex-daemon-request；审核通过后交 emergent 统筹合入，修改 head 后重新复核。
>
> 负责 C0 协作和 C1。先按本文件能力表 逐操作确认 MethodSpec/HTTP/handler，message.send.lookup 已有 HTTP，不要重复造。message.send、targets、progress.list、adapter pins/pin 等缺声明的由本线补本域契约，交 codex-daemon-request 审核，公共线协调核心登记；Lark auth 不允许在 GUI host 直接持有业务服务。把 live client/controller 与主 chat、agent-chat 切到同一生成客户端。必须保留稳定 business key、发送接受与最终状态区分、lookup 确认、unknown 后刷新/重试不重复投递、CAS 与会话身份绑定。不得把 GUI IntentJournal 当投递权威。等同组件浏览器/真实 daemon 验证通过后，另 PR 清理旧 live bridge 产品依赖；具体文件按本文件能力表。
>
> 并行边界：C0 的本线业务 Spec/handler/幂等负例与 C1 adapter/控制器迁移同时开展；已有 message.history、message.send.lookup、adapter.list/status 可先接。message.send/targets/progress/pins 与 Lark auth 缺口由本线补本域契约，公共线审核核心接入/身份上下文；不要把所有业务实现排到公共线之后。GUI 可以使用经审核的契约测试替身开发，真实发送验收等待公共 client 与相应后端实现。给 agent 线稳定的 agent-chat 控制器导出，避免其另建 chat 网络层。文件归属：本线拥有 pages/live、adapters/live、features/chat-channels、features/agent-chat/api 和本线 request_catalog/message/send、message/directory、adapter 业务契约扩展。核心 MethodSpec/registry/统一 transport 归公共线；本线负责业务协议与 handler 语义、公共线负责核心登记协调。旧 bridge 业务只在迁移与回归完成后单独 PR 删除。所有修改依本文件缺口表与总架构，不把缺能力改成成功、不新增第二套业务权威。
